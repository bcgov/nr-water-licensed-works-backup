"""Backup of the WATER_LICENSED_WORKS hosted feature layers.

Runs Mon/Wed/Fri after business hours and before the nightly staging push.
One run exports both layers to File Geodatabases, validates the artifacts,
writes a manifest and a service definition, uploads the set under a new
date-stamped prefix, promotes to the monthly and yearly tiers where
eligible, and prunes old sets last.

    python run_backup.py

Read-only with respect to the production feature layers. The one write of
any kind is item.export(), which creates a temporary item in the backup
account's own content folder and is deleted again as soon as it has been
downloaded. Nothing here deletes, appends to, updates, calculates or
truncates a single feature, and there is no restore path in this module -
recovery is a manual decision made by the data owner.

Two rules shape the order of everything below:

Nothing old is touched until the new set is uploaded and verified, so a run
that dies part way leaves the previous set whole.

A run is all-or-nothing across the two layers. They are independent feature
services exported separately, so one can succeed while the other fails.
Nothing is published unless both artifacts are in hand and valid, because a
half-populated dated prefix would look complete to a future restore.

Needs config.yml, the AGOL credentials and the object storage credentials.
"""

import datetime
import hashlib
import json
import logging
import os
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import pyogrio
from arcgis.features import FeatureLayerCollection
from arcgis.gis import GIS

import storage
# Shared with checks.py. They live in status.py because checks.py must not
# import this module - pyogrio is a GDAL dependency with no business on the
# NRIDS server in Phase 2 - and status.py is the one module both jobs import.
from status import (
    local_date_stamp,
    resolve_code_version,
    safe_reason,
    schema_fingerprint,
    utc_now,
    utc_stamp,
)

logger = logging.getLogger("backup")

MANIFEST_NAME = "manifest.json"
SERVICEDEF_NAME = "servicedef.json"

# Bumped if the shape of manifest.json changes, so a future reader can tell
# an old manifest from a malformed one.
MANIFEST_VERSION = 1

# Hash and copy in 1 MB blocks rather than reading a 14.5 MB artifact into
# memory whole.
BLOCK_BYTES = 1024 * 1024


@dataclass
class BackupResult:
    """The outcome of one backup run.

    status is PASS or SYSTEM_FAIL and nothing else. The five statuses in
    DESIGN.md 7.1 are shared with the check job, but a backup run cannot
    reach a verdict about the data - it copies whatever is there. Whether
    the data is good is the check job's question, which is exactly why
    promotion needs a PASS from that job rather than a successful export
    from this one.

    summary is one line written for a non-technical reader, because it
    becomes the body of the alert email.
    """

    status: str
    summary: str
    details: list
    date_stamp: str
    code_version: str


def connect_to_agol(config):
    """Authenticate and return the GIS. Raises if the credentials are refused."""
    return GIS(
        config["agol"]["url"],
        os.getenv("AGO_USERNAME_WINS"),
        os.getenv("AGO_PASSWORD_WINS"),
    )


def sha256_of_file(path):
    """SHA-256 of a file, read in blocks so an artifact is never held in
    memory in full. Recorded in the manifest and re-checked after upload."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        block = handle.read(BLOCK_BYTES)
        while block:
            digest.update(block)
            block = handle.read(BLOCK_BYTES)
    return digest.hexdigest()


def wait_for_export(export_item, job_id, layer_key, settings):
    """Poll an export job until it finishes, or give up at the configured
    timeout.

    The timeout is ours because the arcgis package has none: export(wait=True)
    polls every two seconds forever, so an AGOL queue that never drains would
    hang a scheduled run indefinitely.
    """
    deadline = time.time() + settings["export_timeout_minutes"] * 60
    started = time.time()

    while True:
        state = export_item.status(job_id=job_id, job_type="export")
        status = str(state.get("status", "")).lower()
        elapsed_minutes = (time.time() - started) / 60

        if status == "completed":
            logger.info("%s: export completed after %.1f min", layer_key, elapsed_minutes)
            return
        if status == "failed":
            raise RuntimeError(
                f"AGOL reported the {layer_key} export as failed after "
                f"{elapsed_minutes:.1f} minutes. Nothing has been published and "
                f"the previous backups are untouched. Run preflight.py to "
                f"confirm the account can still export both items."
            )
        if time.time() >= deadline:
            raise TimeoutError(
                f"The {layer_key} export was still '{status}' after "
                f"{settings['export_timeout_minutes']} minutes. A slow export is "
                f"normal on this service - 54.5 minutes was measured, and the "
                f"wait is AGOL queue time rather than data volume - so raise "
                f"backup.export_timeout_minutes in config.yml if this recurs "
                f"rather than treating it as a fault."
            )

        logger.info("%s: export %s, %.0f min elapsed", layer_key, status, elapsed_minutes)
        time.sleep(settings["export_poll_seconds"])


def export_layer(gis, item, layer_key, config, work_dir):
    """Export one layer to a File Geodatabase, download it, and return the
    path to the zip along with the time the export was requested.

    FGDB because it is the only offered format that preserves field types,
    nulls, coded-value domains and subtypes. It does not capture the hosted
    service's own configuration - symbology, sharing, sync, item ID - which
    is what servicedef.json is for and why a restore keeps the existing
    hosted item rather than replacing it.

    Asynchronous on purpose: see wait_for_export. Duration cannot be
    predicted from the data. The two measured exports took 22.6 and 54.5
    minutes on the same afternoon, and the larger layer was the faster one.
    """
    settings = config["backup"]
    requested_at = utc_now()

    # The backup account already holds 19 File Geodatabase items left over
    # from manual exports going back to 2022, titled exactly like the layers.
    # The run stamp keeps ours identifiable for the minutes it exists.
    title = f"{item.title}_backup_{utc_stamp(requested_at).replace(':', '')}"

    logger.info("%s: requesting File Geodatabase export", layer_key)
    job = item.export(title=title, export_format="File Geodatabase", wait=False)
    if "exportItemId" not in job:
        raise RuntimeError(
            f"AGOL accepted the {layer_key} export request but returned no "
            f"export item to collect. Nothing has been published."
        )

    export_item = gis.content.get(job["exportItemId"])
    try:
        wait_for_export(export_item, job.get("jobId"), layer_key, settings)
        downloaded = export_item.download(
            save_path=str(work_dir), file_name=f"{layer_key}.gdb.zip"
        )
        if not downloaded or not Path(downloaded).exists():
            raise RuntimeError(
                f"The {layer_key} export completed on AGOL but the download "
                f"produced no file. Nothing has been published."
            )
    finally:
        # Always, including after a timeout or a failed download. Deleted by
        # the item ID this export returned and never by searching for a title
        # or a type - a search would find the account owner's own 19 manual
        # exports and destroy them.
        try:
            export_item.delete()
            logger.info("%s: deleted the temporary AGOL export item", layer_key)
        except Exception as exc:
            # A leftover export item is clutter in the account, not a problem
            # with the backup. Say so loudly enough that someone can remove
            # it by hand, but do not fail a good run over it, and do not let
            # it replace whatever error brought us here.
            logger.warning(
                "%s: could not delete the temporary AGOL export item %s (%s). "
                "Delete it by hand - it is the only item with that ID.",
                layer_key, job["exportItemId"], type(exc).__name__,
            )

    return Path(downloaded), requested_at


def validate_artifact(zip_path, live_count, layer_key, config, work_dir):
    """Prove the downloaded artifact is intact and a faithful snapshot.

    What this establishes, and what it does not: an artifact that passes
    every step below is *intact*, not *correct*. A perfect snapshot of
    already-corrupt data passes all of it cleanly. That is the reason
    promotion to the monthly tier additionally requires a passing data check
    (DESIGN.md 6.4), and the reason this function is not called a data check.
    """
    validation = config["backup"]["validation"]

    with zipfile.ZipFile(zip_path) as archive:
        # testzip() checks the CRC of every member. This is what proves the
        # download completed rather than stopping part way - a size check
        # alone passes a truncated transfer that happens to end on a
        # plausible boundary.
        corrupt_member = archive.testzip()
        if corrupt_member:
            raise ValueError(
                f"The {layer_key} artifact is corrupt: '{corrupt_member}' fails "
                f"its checksum inside the zip. The download did not complete "
                f"intact. Nothing has been published."
            )
        extract_dir = Path(work_dir) / f"{layer_key}_extracted"
        archive.extractall(extract_dir)

    # The exported geodatabase is named with a GUID -
    # c9ad3cfa-5f69-47f7-a38d-ece32a4f80a4.gdb - and not with anything derived
    # from the layer, so it is found by extension. Never look for a name.
    geodatabases = [path for path in extract_dir.rglob("*.gdb") if path.is_dir()]
    if len(geodatabases) != 1:
        raise ValueError(
            f"The {layer_key} artifact holds {len(geodatabases)} .gdb "
            f"directories, expected exactly 1. AGOL may have changed what an "
            f"export contains. Nothing has been published."
        )

    # pyogrio reads the geodatabase through GDAL's OpenFileGDB driver, which
    # needs no arcpy - unavailable on a GitHub runner.
    layers_in_gdb = pyogrio.list_layers(geodatabases[0])
    if len(layers_in_gdb) != 1:
        raise ValueError(
            f"The {layer_key} geodatabase holds {len(layers_in_gdb)} feature "
            f"classes, expected exactly 1. Nothing has been published."
        )

    feature_class = str(layers_in_gdb[0][0])
    exported_count = int(pyogrio.read_info(geodatabases[0], layer=feature_class)["features"])

    if live_count <= 0:
        raise ValueError(
            f"The live {layer_key} layer reported {live_count} features, so "
            f"there is nothing to compare the artifact against and no backup "
            f"worth taking. This is a data emergency rather than a backup "
            f"fault - look at the layer in AGOL before anything else."
        )
    if validation["require_nonzero"] and exported_count == 0:
        raise ValueError(
            f"The {layer_key} export contains no features at all, while the "
            f"live layer reported {live_count:,}. Publishing it would put an "
            f"empty set where a restore would later look for data. Nothing "
            f"has been published."
        )

    # A live count and an export taken up to an hour apart will legitimately
    # differ by an edit or two: lines was seen at 142,522 and 142,523 within
    # one hour. Hence a tolerance rather than equality.
    drift_percent = 100.0 * abs(exported_count - live_count) / live_count
    if drift_percent > validation["count_tolerance_percent"]:
        raise ValueError(
            f"The {layer_key} artifact holds {exported_count:,} features against "
            f"{live_count:,} live, a difference of {drift_percent:.2f}% which is "
            f"beyond the {validation['count_tolerance_percent']}% allowed by "
            f"backup.validation.count_tolerance_percent. That is too large to be "
            f"editing during the export, so the artifact is not a faithful "
            f"snapshot. Nothing has been published."
        )

    logger.info(
        "%s: artifact valid - %s features in '%s', %s bytes, %.2f%% from live",
        layer_key, f"{exported_count:,}", feature_class,
        f"{zip_path.stat().st_size:,}", drift_percent,
    )
    return {
        "feature_class": feature_class,
        "exported_feature_count": exported_count,
        "live_feature_count": live_count,
        "count_drift_percent": round(drift_percent, 4),
        "artifact_bytes": zip_path.stat().st_size,
        "sha256": sha256_of_file(zip_path),
    }


def write_json(path, payload):
    """Write one of the small JSON side files, sorted and indented so that a
    person can read it and a diff between two runs is legible."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return path


def verify_uploaded(store, key, local_path, work_dir):
    """Confirm what is in the bucket matches what was sent: size, then bytes.

    The size comes from a head request. The checksum needs the object back
    again - this storage offers no server-side SHA-256, and an ETag is an MD5
    only for a single-part upload, which the 14.5 MB lines artifact is not.
    At a few seconds against an export measured in tens of minutes, reading
    it back is free, and it is the difference between "the bytes were sent"
    and "the backup can be read".
    """
    local_size = local_path.stat().st_size
    stored_size = storage.key_size(store, key)
    if stored_size != local_size:
        raise ValueError(
            f"Upload of '{key}' stored {stored_size:,} bytes against "
            f"{local_size:,} sent. The set is incomplete and must not be "
            f"treated as a backup."
        )

    readback = Path(work_dir) / "verify" / local_path.name
    storage.download_file(store, key, readback)
    if sha256_of_file(readback) != sha256_of_file(local_path):
        raise ValueError(
            f"Upload of '{key}' is the right size but the wrong bytes - its "
            f"SHA-256 does not match what was sent. The set must not be "
            f"treated as a backup."
        )
    readback.unlink()


def publish_set(store, config, work_dir, date_stamp, file_names):
    """Upload the set under its dated prefix and confirm every object.

    Nothing old is touched here. Uploading and verifying before anything is
    promoted or pruned is what makes a failed run harmless (DESIGN.md 6.5).

    manifest.json goes last on purpose: its presence is what makes a dated
    prefix complete, so a set interrupted half way can be recognised as one
    rather than mistaken for a backup.
    """
    rotating = config["storage"]["paths"]["rotating"]
    published = []
    for name in file_names:
        key = f"{rotating}{date_stamp}/{name}"
        local_path = Path(work_dir) / name
        storage.upload_file(store, local_path, key)
        verify_uploaded(store, key, local_path, work_dir)
        published.append(key)
    logger.info("Published %d objects to %s%s/", len(published), rotating, date_stamp)
    return published


def set_names_in(store, tier_path):
    """The set folders under a tier, oldest first: ['2026-08-12', '2026-08-14'].

    Date-stamped names sort chronologically as text, which is what pruning
    and promotion both rely on. The 0-byte placeholder object that lives at
    the project prefix is already dropped by storage.list_keys; counting it
    here would make every "how many sets are there" answer one too many.
    """
    names = set()
    for key in storage.list_keys(store, tier_path):
        relative = key[len(tier_path):]
        if "/" in relative:
            names.add(relative.split("/", 1)[0])
    return sorted(names)


def copy_set(store, source_prefix, destination_prefix):
    """Copy every object of one set to another prefix, server side."""
    copied = []
    for key in storage.list_keys(store, source_prefix):
        name = key[len(source_prefix):]
        storage.copy_key(store, key, f"{destination_prefix}{name}")
        copied.append(name)
    return copied


def parse_status_key(key):
    """The UTC time a status object was written, read from its own key.

    Keys look like 'status/checks-2026-08-14T23:05:00Z.json'. Returns None
    for anything that does not, so a stray object cannot derail a run.
    """
    name = key.rsplit("/", 1)[-1]
    if not name.startswith("checks-") or not name.endswith(".json"):
        return None
    stamp = name[len("checks-"):-len(".json")]
    try:
        return datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        logger.warning("Ignoring status object with an unreadable name: %s", key)
        return None


def read_status(store, key):
    """The status value recorded in one status object."""
    return json.loads(storage.read_bytes(store, key)).get("status")


def check_status_on(store, config, date_stamp):
    """The check outcome paired with a rotating set, or None if none ran.

    Status objects are named with a UTC timestamp while a rotating set is
    stamped with the local date, so the two are matched by converting the
    timestamp back to the configured zone. A check that ran at 16:05 in
    Vancouver is stamped 23:05Z the same day, but one that ran at 19:00 is
    stamped 02:00Z the next - matching on the text of the key would file that
    run under the wrong date and promote against the wrong day's verdict.
    """
    zone = ZoneInfo(config["timezone"])
    paired = []
    for key in storage.list_keys(store, config["storage"]["paths"]["status"]):
        written = parse_status_key(key)
        if written and written.astimezone(zone).strftime("%Y-%m-%d") == date_stamp:
            paired.append((written, key))
    if not paired:
        return None
    return read_status(store, max(paired)[1])


def latest_check_status(store, config):
    """The most recent check outcome, whichever day it ran on.

    Pruning pauses while this is DATA_FAIL, so that an unresolved incident
    cannot quietly evict the last good copy while the alert goes unactioned.
    """
    written = [
        (parse_status_key(key), key)
        for key in storage.list_keys(store, config["storage"]["paths"]["status"])
    ]
    written = [pair for pair in written if pair[0]]
    if not written:
        return None
    return read_status(store, max(written)[1])


def promote_monthly(store, config, date_stamp, details):
    """Promote the month's first checked rotating set to the monthly tier.

    The rule is the first rotating set of the calendar month whose paired
    check status was PASS - not merely the first set with a valid artifact.
    Artifact validation proves the zip is intact; a faithful snapshot of
    corrupt data passes it cleanly. Promoting on that basis would make
    corrupt data the monthly anchor that the check job's comparison is
    measured against for the rest of the month, so the reference for "normal"
    would itself be wrong.

    A copy, not a second export: exporting again would produce a different
    snapshot taken at a different moment.
    """
    month = date_stamp[:7]
    monthly_path = config["storage"]["paths"]["monthly"]
    rotating_path = config["storage"]["paths"]["rotating"]
    require_pass = config["promotion"]["require_check_pass"]

    if storage.list_keys(store, f"{monthly_path}{month}/"):
        details.append(f"monthly/{month} was already promoted")
        return

    for set_name in set_names_in(store, rotating_path):
        if not set_name.startswith(month):
            continue
        status = check_status_on(store, config, set_name)
        if status == "PASS" or not require_pass:
            copy_set(store, f"{rotating_path}{set_name}/", f"{monthly_path}{month}/")
            details.append(f"promoted rotating/{set_name} to monthly/{month}")
            logger.info("Promoted rotating/%s to monthly/%s", set_name, month)
            return

    # Not a failure. Promotion simply retries on the next backup run, which
    # is why the tier needs no scheduled job of its own.
    details.append(f"no rotating set of {month} has a passing check yet, monthly not promoted")
    day_of_month = int(date_stamp[8:10])
    if day_of_month >= config["promotion"]["no_candidate_alert_days"]:
        message = (
            f"{month} is {day_of_month} days in with nothing promoted to the "
            f"monthly tier - no rotating set has a paired PASS from the check job"
        )
        logger.warning(message)
        details.append(message)


def promote_yearly(store, config, date_stamp, details):
    """Promote the year's first monthly set to the yearly tier.

    Promoted from a monthly set rather than from a rotating one, so the
    yearly artifact inherits the passing check the monthly promotion already
    required. This tier is never pruned automatically.
    """
    year = date_stamp[:4]
    yearly_path = config["storage"]["paths"]["yearly"]
    monthly_path = config["storage"]["paths"]["monthly"]

    if storage.list_keys(store, f"{yearly_path}{year}/"):
        return

    for set_name in set_names_in(store, monthly_path):
        if set_name.startswith(year):
            copy_set(store, f"{monthly_path}{set_name}/", f"{yearly_path}{year}/")
            details.append(f"promoted monthly/{set_name} to yearly/{year}")
            logger.info("Promoted monthly/%s to yearly/%s", set_name, year)
            return


def prune_tier(store, tier_path, keep, details):
    """Delete whole sets from one tier until only the newest `keep` remain.

    keep of None means never prune, which is what the yearly tier is set to.
    """
    if keep is None:
        return
    sets = set_names_in(store, tier_path)
    if len(sets) <= keep:
        return

    for set_name in sets[: len(sets) - keep]:
        for key in storage.list_keys(store, f"{tier_path}{set_name}/"):
            storage.delete_key(store, key)
        details.append(f"pruned {tier_path}{set_name}")
    logger.info(
        "Pruned %d set(s) from %s, keeping the newest %d",
        len(sets) - keep, tier_path, keep,
    )


def prune_metrics(store, config, details):
    """Drop metrics files past their retention horizon.

    Metrics are a few KB each and the horizon is far beyond the 30-day trend
    window the check job compares against, so this cannot remove history a
    check still needs.
    """
    days = config["retention"]["metrics_days"]
    if not days:
        return

    metrics_path = config["storage"]["paths"]["metrics"]
    # Metrics files are named with the local date, so the horizon is counted
    # in the same zone rather than in the runner's.
    today = datetime.datetime.now(ZoneInfo(config["timezone"])).date()
    cutoff = (today - datetime.timedelta(days=days)).isoformat()
    removed = 0
    for key in storage.list_keys(store, metrics_path):
        stamp = key[len(metrics_path):].removesuffix(".json")
        try:
            datetime.date.fromisoformat(stamp)
        except ValueError:
            logger.warning("Ignoring metrics object with an unreadable name: %s", key)
            continue
        # ISO dates compare correctly as text.
        if stamp < cutoff:
            storage.delete_key(store, key)
            removed += 1
    if removed:
        details.append(f"pruned {removed} metrics file(s) older than {days} days")


def prune(store, config, latest_status, details):
    """Delete sets beyond the retention counts.

    Runs last, after the new set is published and verified, so a failure here
    can never cost the last good copy.

    All deletion in this project happens here and in storage.delete_key. The
    bucket issues a single full-access key pair, so there is no permission
    boundary between this code and the backups - keeping the logic in one
    place, explicit about what it removes, is the mitigation that is actually
    available (DESIGN.md 6.6).
    """
    retention = config["retention"]

    if latest_status == "DATA_FAIL" and retention["pause_prune_on_data_fail"]:
        # An unresolved incident must not quietly evict the last good copy
        # while nobody is acting on the alert. The ceiling keeps that from
        # becoming unbounded growth.
        keep_rotating = retention["rotating_sets_max"]
        message = (
            f"pruning paused: the most recent check status is DATA_FAIL, so "
            f"rotating sets are kept up to the ceiling of {keep_rotating} "
            f"rather than the usual {retention['rotating_sets']}"
        )
        logger.warning(message)
        details.append(message)
    else:
        keep_rotating = retention["rotating_sets"]

    prune_tier(store, config["storage"]["paths"]["rotating"], keep_rotating, details)
    prune_tier(store, config["storage"]["paths"]["monthly"], retention["monthly_sets"], details)
    # The yearly tier is deliberately absent: yearly_sets is null because the
    # last-resort tier is never pruned automatically.
    prune_metrics(store, config, details)


def run_backup(config):
    """Run one backup and return a BackupResult.

    The order is fixed by DESIGN.md 6.5: export and validate both layers,
    then publish, then promote, then prune. Nothing before the publish step
    writes to the bucket, and nothing before the prune step deletes anything.
    """
    started = utc_now()
    code_version = resolve_code_version()
    date_stamp = local_date_stamp(config)
    details = []
    logger.info("Backup run for %s, code version %s", date_stamp, code_version)

    try:
        store = storage.connect_to_storage(config)
        gis = connect_to_agol(config)
    except Exception as exc:
        reason = safe_reason(exc)
        logger.error("Backup could not start: %s", reason)
        return BackupResult(
            "SYSTEM_FAIL",
            "The backup job could not start because it was unable to sign in to "
            "ArcGIS Online or reach object storage. No backup was taken and the "
            "previous backups are untouched.",
            [reason], date_stamp, code_version,
        )

    with tempfile.TemporaryDirectory(prefix="wlw_backup_") as work_dir:
        work_dir = Path(work_dir)
        layer_facts = {}
        service_definitions = {}

        try:
            for layer_key, layer_config in config["layers"].items():
                item = gis.content.get(layer_config["item_id"])
                if item is None:
                    raise ValueError(
                        f"The {layer_key} item_id in config.yml is not visible to "
                        f"this account. Confirm the ID and that the account is "
                        f"still in the QuickWins sharing group."
                    )
                layer = item.layers[layer_config["layer_index"]]

                # Live, not the cached property: infoInEstimates declares the
                # cached count an estimate, and an estimate compared against a
                # real artifact count would fail validation for no reason.
                live_count = layer.query(where="1=1", return_count_only=True)

                zip_path, exported_at = export_layer(gis, item, layer_key, config, work_dir)
                facts = validate_artifact(zip_path, live_count, layer_key, config, work_dir)

                layer_facts[layer_key] = dict(
                    facts,
                    item_id=layer_config["item_id"],
                    layer_id=layer_config["layer_index"],
                    name=layer_config["name"],
                    geometry_type=layer.properties.get("geometryType"),
                    spatial_reference=layer.properties.get("extent", {})
                        .get("spatialReference", {}).get("latestWkid"),
                    artifact=zip_path.name,
                    exported_utc=utc_stamp(exported_at),
                    schema_fingerprint=schema_fingerprint(layer.properties),
                )
                # The service-level block is kept as well as the layer one:
                # syncEnabled and the capability set live on the service and
                # are absent from the layer JSON entirely.
                service_definitions[layer_key] = {
                    "service": dict(FeatureLayerCollection.fromitem(item).properties),
                    "layer": dict(layer.properties),
                }
        except Exception as exc:
            reason = safe_reason(exc)
            logger.error("Backup failed before anything was published: %s", reason)
            return BackupResult(
                "SYSTEM_FAIL",
                "The backup job could not produce a complete set of both layers, "
                "so nothing was saved this run. The previous backups are "
                "untouched and can still be restored from.",
                [reason], date_stamp, code_version,
            )

        # Both layers are in hand and valid. Only now does anything get written.
        write_json(work_dir / MANIFEST_NAME, {
            "manifest_version": MANIFEST_VERSION,
            "date_stamp": date_stamp,
            "run_started_utc": utc_stamp(started),
            "run_completed_utc": utc_stamp(utc_now()),
            "code_version": code_version,
            "portal": config["agol"]["url"],
            # Two independent feature services, exported one after the other:
            # there is no instant at which both artifacts are consistent with
            # each other. Each layer carries its own export time so that a
            # reader can see the gap rather than assume there is none.
            "layers": layer_facts,
        })
        write_json(work_dir / SERVICEDEF_NAME, {
            "captured_utc": utc_stamp(utc_now()),
            "code_version": code_version,
            # Reference material for a manual service rebuild if the hosted
            # service itself is ever lost. Not a restore input - a restore
            # keeps the existing hosted item.
            "layers": service_definitions,
        })

        try:
            rotating_path = config["storage"]["paths"]["rotating"]
            if storage.list_keys(store, f"{rotating_path}{date_stamp}/"):
                logger.warning(
                    "%s%s/ already holds a set; this run replaces it",
                    rotating_path, date_stamp,
                )
            # Taken from the manifest rather than rebuilt from the layer
            # names, so that what is uploaded and what the manifest says was
            # uploaded cannot drift apart.
            artifacts = [facts["artifact"] for facts in layer_facts.values()]
            publish_set(
                store, config, work_dir, date_stamp,
                artifacts + [SERVICEDEF_NAME, MANIFEST_NAME],
            )
            details.append(f"published rotating/{date_stamp} with {len(artifacts) + 2} objects")
        except Exception as exc:
            reason = safe_reason(exc)
            logger.error("Backup failed while publishing: %s", reason)
            return BackupResult(
                "SYSTEM_FAIL",
                "The backup was taken but could not be saved to storage "
                "completely, so this run must not be relied on. The previous "
                "backups are untouched.",
                [reason], date_stamp, code_version,
            )

    # Everything below runs only because the new set is uploaded and verified.
    try:
        promote_monthly(store, config, date_stamp, details)
        promote_yearly(store, config, date_stamp, details)
    except Exception as exc:
        reason = safe_reason(exc)
        logger.error("Promotion failed: %s", reason)
        return BackupResult(
            "SYSTEM_FAIL",
            f"The backup for {date_stamp} was saved successfully, but copying it "
            f"to the monthly or yearly archive did not finish. The backup itself "
            f"is safe.",
            details + [reason], date_stamp, code_version,
        )

    try:
        prune(store, config, latest_check_status(store, config), details)
    except Exception as exc:
        reason = safe_reason(exc)
        logger.error("Pruning failed: %s", reason)
        return BackupResult(
            "SYSTEM_FAIL",
            f"The backup for {date_stamp} was saved successfully, but tidying up "
            f"older backups did not finish. The backup itself is safe and no "
            f"data has been lost.",
            details + [reason], date_stamp, code_version,
        )

    saved = " and ".join(
        f"{facts['exported_feature_count']:,} {layer_key}"
        for layer_key, facts in layer_facts.items()
    )
    return BackupResult(
        "PASS",
        f"Backup completed for {date_stamp}: saved {saved}.",
        details, date_stamp, code_version,
    )
