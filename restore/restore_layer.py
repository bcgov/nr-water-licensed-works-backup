"""Restore one hosted feature layer from a backup artifact.

Run by a person, by hand, after the data owner has approved the restore. It
is not part of the pipeline: nothing imports it, no workflow references it,
and no scheduled job can reach it. It is here because an unrehearsed recovery
path is the risk this whole project exists to guard against, and a restore
tool that has never been run is not a recovery path.

    python restore/restore_layer.py --item-id <id> --layer-index 0 \
        --fgdb path/to/points.gdb.zip

That describes what it would do and stops. Nothing is changed without
--execute, and --execute still asks for typed confirmation.

WHAT IT DOES, IN THIS ORDER, AND THE ORDER IS THE POINT:

    1. verify the artifact's SHA-256 against the manifest beside it
    2. read the target layer and report what is about to be destroyed
    3. ask for confirmation
    4. delete_features(where="1=1")
    5. append the artifact, preserving GlobalIDs
    6. verify the count, extent and schema against the manifest

Nothing is deleted until the replacement is in hand and proven intact. The
failure this refuses to allow is a half-verified artifact discovered to be
wrong after the layer is already empty.

WHY IT READS NOTHING FROM config.yml. Every other module in this repository
is told what to touch by that file. This one is told by its arguments, every
time, because a tool that can delete 142,000 features must not be aimable by
a file somebody edited last month. There is no default target and no default
artifact.

WHY THE CREDENTIALS HAVE THEIR OWN VARIABLE NAMES. It authenticates with
AGO_USERNAME_RESTORE and AGO_PASSWORD_RESTORE and with nothing else. The
pipeline's own AGO_USERNAME_WINS / AGO_PASSWORD_WINS are deliberately not
read, so a shell set up to run a backup cannot run a restore by accident.
Restoring takes a separate, deliberate act of putting credentials somewhere
they are not otherwise kept.

WHY IT HOLDS NO STORAGE CREDENTIALS. It takes a local file. The operator
downloads the artifact from object storage first, as a separate step they can
see. That way this script cannot delete a backup either - only the layer it
was explicitly pointed at.

THE PRODUCTION ITEMS ARE FENCED, NOT FORBIDDEN. The two live item IDs are
hard-coded below and refused before any other argument is read. Restoring one
of them is a real operation this tool exists to support, so the refusal lifts
- but only for somebody who passes --production, names who approved it, and
types the layer name back. DESIGN.md 7.8.4 originally specified an outright
refusal, which was right while the only conceivable use was a drill; the
reversal and its reasoning are recorded there.

Needs the arcgis package, already in requirements.txt.
"""

import argparse
import datetime
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

from arcgis.gis import GIS

logger = logging.getLogger("restore")

# The live items, hard-coded so that knowing which layers are production is a
# property of this file rather than of whatever configuration it was handed.
# They are already public in config.yml, so naming them here publishes
# nothing new.
PRODUCTION_ITEMS = {
    "a938dd3fcafa4b98baee3fcb5ab59fe8": "WATER_LICENSED_WORKS_LINES",
    "848f3c3900da4b1b907a415154a53042": "WATER_LICENSED_WORKS_POINTS",
}

# Deliberately not AGO_USERNAME_WINS / AGO_PASSWORD_WINS. See the docstring.
USERNAME_VARIABLE = "AGO_USERNAME_RESTORE"
PASSWORD_VARIABLE = "AGO_PASSWORD_RESTORE"

DEFAULT_PORTAL = "https://governmentofbc.maps.arcgis.com"

# A file geodatabase inside a zip, which is what item.export() produces and
# what backup.py stores. The append operation is told this format explicitly;
# it has no way to work it out.
UPLOAD_FORMAT = "filegdb"

# The temporary uploaded item is deleted in a finally block, but a run killed
# between the upload and the delete leaves one behind. The prefix is what
# makes it findable and obviously ours.
UPLOAD_TITLE_PREFIX = "RESTORE_SOURCE_"


def sha256_of_file(path):
    """The checksum the manifest records, computed the same way backup.py
    computed it. Read in blocks because these artifacts are tens of MB."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def schema_fingerprint(layer_properties):
    """A comparable summary of one layer's schema.

    THIS IS A COPY OF status.schema_fingerprint AND THE COPY IS DELIBERATE.
    This module imports nothing from the repository, so that a restore can be
    run from a directory holding only this file and an artifact, and so that
    no future edit can make a pipeline module depend on it. The duplication is
    load-bearing - the manifest's fingerprint was written by that function and
    is compared against this one - so it is held by a test rather than by good
    intentions: tests/test_restore.py asserts the two agree on the same input.
    Change one and the test fails.
    """
    fields = [
        {"name": field["name"], "type": field["type"]}
        for field in sorted(layer_properties.get("fields", []), key=lambda f: f["name"])
    ]

    domains = {}
    for field in layer_properties.get("fields", []):
        domain = field.get("domain")
        if domain and domain.get("type") == "codedValue":
            domains[field["name"]] = {
                "name": domain.get("name"),
                "coded_values": sorted(
                    str(value["code"]) for value in domain.get("codedValues", [])
                ),
            }

    subtypes = sorted(
        f"{entry.get('id')}:{entry.get('name')}"
        for entry in layer_properties.get("types", [])
    )

    return {"fields": fields, "domains": domains, "subtypes": subtypes}


# ---------------------------------------------------------------------------
# The interlocks
#
# Everything here is a pure function of its arguments, so tests/test_restore.py
# can exercise every refusal without a network, a credential or a layer.
# ---------------------------------------------------------------------------


def check_target_allowed(item_id, production, approved_by):
    """Refuse a production item unless the run is explicitly unlocked.

    Called before credentials are read and before anything is opened, so the
    refusal cannot depend on being signed in or on the artifact being valid.

    A restore of the live layers is a real operation and this tool exists to
    support it. What the unlock buys is that it cannot happen by mistyping an
    item ID or by pasting the wrong line from a runbook: it takes a flag, a
    named approver, and typing the layer name back (confirmation_phrase).
    """
    name = PRODUCTION_ITEMS.get(item_id)
    if name is None:
        return

    if not production:
        raise ValueError(
            f"Item {item_id} is the live {name} layer, and this run is not "
            f"authorised to touch it. A production restore discards every "
            f"edit made since the artifact was taken and cannot be undone, so "
            f"it needs the data owner's approval first. With that approval, "
            f"re-run with --production --approved-by \"<who approved it>\"."
        )

    if not approved_by or not approved_by.strip():
        raise ValueError(
            f"--production was given for the live {name} layer but "
            f"--approved-by was not. Name the person who approved this "
            f"restore; it is written into the log so that the record of who "
            f"authorised it survives the terminal window."
        )


def confirmation_phrase(item_id, production):
    """What the operator has to type to proceed.

    The item ID for a test copy, and the layer name for a production layer.
    Two different phrases on purpose: a person who has restored a test copy a
    dozen times has learned to type an item ID without reading, and the one
    run where that matters is the one where the phrase is different.
    """
    if item_id in PRODUCTION_ITEMS and production:
        return f"RESTORE {PRODUCTION_ITEMS[item_id]}"
    return item_id


def manifest_entry_for(manifest, artifact_name):
    """The manifest's record of one artifact, found by file name.

    The name is the link between the two: backup.py writes one manifest
    covering both layers, each entry carrying the name of its own zip. Looking
    the entry up by artifact rather than by layer key means the operator never
    has to say which layer they are restoring - the file they passed says it.
    """
    layers = manifest.get("layers") or {}
    for layer_key, entry in layers.items():
        if entry.get("artifact") == artifact_name:
            return layer_key, entry

    known = ", ".join(sorted(
        str(entry.get("artifact")) for entry in layers.values()
    )) or "nothing"
    raise ValueError(
        f"The manifest does not mention '{artifact_name}'. It covers {known}. "
        f"Check that the manifest.json passed with --manifest came from the "
        f"same backup set as the artifact."
    )


def verify_artifact(fgdb_path, entry):
    """Prove the artifact is the one the manifest describes, before anything
    is destroyed.

    A checksum mismatch means the file was truncated, modified, or came from a
    different set - and any of those makes it the wrong thing to rebuild a
    layer from. There is no --force for this. If the artifact cannot be
    trusted, the correct next step is to fetch another copy, not to empty a
    layer and hope.
    """
    expected = entry.get("sha256")
    if not expected:
        raise ValueError(
            f"The manifest entry for '{entry.get('artifact')}' has no sha256, "
            f"so the artifact cannot be verified and must not be used. This "
            f"manifest was not written by backup.py."
        )

    actual = sha256_of_file(fgdb_path)
    if actual != expected:
        raise ValueError(
            f"{fgdb_path.name} does not match the manifest.\n"
            f"  manifest: {expected}\n"
            f"  file:     {actual}\n"
            f"The file is truncated, altered, or from a different backup set. "
            f"Nothing has been touched. Download the artifact again."
        )

    logger.info("Artifact verified against the manifest: %s", actual)


def compare_schema(current, expected):
    """Differences between two schema fingerprints, as readable lines.

    Reported rather than enforced. A restore that leaves the schema slightly
    different is something a person needs to see and judge; refusing to finish
    at that point would leave the layer empty, which is worse than any schema
    difference.
    """
    differences = []

    current_fields = {field["name"]: field["type"] for field in current["fields"]}
    expected_fields = {field["name"]: field["type"] for field in expected["fields"]}
    for name in sorted(set(expected_fields) - set(current_fields)):
        differences.append(f"field '{name}' is in the artifact and not in the layer")
    for name in sorted(set(current_fields) - set(expected_fields)):
        differences.append(f"field '{name}' is in the layer and not in the artifact")
    for name in sorted(set(current_fields) & set(expected_fields)):
        if current_fields[name] != expected_fields[name]:
            differences.append(
                f"field '{name}' is {current_fields[name]}, "
                f"the artifact recorded {expected_fields[name]}"
            )

    if current["domains"] != expected["domains"]:
        differences.append("the coded-value domains differ")
    if current["subtypes"] != expected["subtypes"]:
        differences.append("the subtypes differ")

    return differences


# ---------------------------------------------------------------------------
# The operations
# ---------------------------------------------------------------------------


def connect_to_agol(portal):
    """Authenticate with the restore-only credentials.

    Missing variables are a hard stop with the names in the message, because
    the alternative - an anonymous connection - fails later with something
    that reads like a permissions problem.
    """
    username = os.getenv(USERNAME_VARIABLE)
    password = os.getenv(PASSWORD_VARIABLE)
    if not username or not password:
        raise ValueError(
            f"{USERNAME_VARIABLE} and {PASSWORD_VARIABLE} must both be set. "
            f"This tool deliberately does not read the pipeline's own ArcGIS "
            f"Online credentials, so that a shell set up to run a backup "
            f"cannot run a restore. Set them for this session only."
        )
    return GIS(portal, username, password)


def open_layer(gis, item_id, layer_index):
    """The target layer, with a clear message if it is not reachable."""
    item = gis.content.get(item_id)
    if item is None:
        raise ValueError(
            f"Item {item_id} is not visible to this account. Either the ID is "
            f"wrong or the account signed in through {USERNAME_VARIABLE} has "
            f"no access to it."
        )
    try:
        return item, item.layers[layer_index]
    except IndexError:
        raise ValueError(
            f"Item {item_id} has {len(item.layers)} layer(s), so there is no "
            f"layer at index {layer_index}."
        )


def describe_layer(layer):
    """What is about to be destroyed, read live.

    Live rather than from the cached properties: infoInEstimates declares the
    layer's own count an estimate, and an estimate is not good enough to put
    in front of somebody deciding whether to empty a layer.
    """
    return {
        "feature_count": layer.query(where="1=1", return_count_only=True),
        "geometry_type": layer.properties.get("geometryType"),
        "supports_truncate": layer.properties.get("supportsTruncate"),
        "schema": schema_fingerprint(layer.properties),
    }


def delete_all_features(layer, chunk_size):
    """Empty the layer, in one call if the service will take it.

    truncate is unavailable on these services because sync is enabled
    (DESIGN.md 11.1), so this is delete_features with a where clause that
    matches everything. At 54,000 and 142,000 features that is a bulk
    operation that may run long or time out, which is exactly what the restore
    drill was run to find out - and why the chunked path below exists.

    return_delete_results is off: it would return one row per deleted feature,
    so a successful delete would come back as a 142,000-element list nobody
    reads. The count is re-queried afterwards instead, which is a better proof
    anyway - it asks the service what is there rather than what it says it did.
    """
    started = time.time()
    try:
        layer.delete_features(
            where="1=1", rollback_on_failure=True, return_delete_results=False
        )
    except Exception as exc:
        logger.warning(
            "The single delete_features call failed after %.0fs (%s). Falling "
            "back to deleting in chunks of %s.",
            time.time() - started, type(exc).__name__, f"{chunk_size:,}",
        )
        delete_in_chunks(layer, chunk_size)

    remaining = layer.query(where="1=1", return_count_only=True)
    if remaining:
        raise ValueError(
            f"The layer still holds {remaining:,} features after the delete. "
            f"It is now in a partly emptied state and must not be left there: "
            f"re-run the delete, or append the artifact back with "
            f"--append-only once it is empty."
        )
    return time.time() - started


def delete_in_chunks(layer, chunk_size):
    """Delete a batch of OBJECTIDs at a time when the bulk call will not go
    through.

    The IDs are listed explicitly rather than expressed as a range. A range
    would be shorter to read, but it would depend on the service returning
    the IDs in order - and if it did not, 'OBJECTID between the smallest and
    largest of this batch' could quietly cover the whole layer, which is the
    single call that has just failed. Listing them says exactly what is being
    deleted.

    The guard at the end is the important part: if a call reports success and
    the count has not moved, this stops rather than looping forever against a
    service that is accepting the request and doing nothing with it.
    """
    while True:
        before = layer.query(where="1=1", return_count_only=True)
        if not before:
            return

        response = layer.query(where="1=1", return_ids_only=True)
        object_ids = response.get("objectIds") if isinstance(response, dict) else response
        batch = sorted(object_ids or [])[:chunk_size]
        if not batch:
            raise ValueError(
                f"The layer reports {before:,} features but returned no "
                f"OBJECTIDs to delete. Stopping rather than looping."
            )

        logger.info(
            "Deleting %s features, OBJECTID %s to %s, %s remaining",
            f"{len(batch):,}", batch[0], batch[-1], f"{before:,}",
        )
        layer.delete_features(
            deletes=",".join(str(object_id) for object_id in batch),
            rollback_on_failure=True, return_delete_results=False,
        )

        if layer.query(where="1=1", return_count_only=True) >= before:
            raise ValueError(
                f"A chunked delete of {len(batch):,} features left the count "
                f"at {before:,}. The service is accepting the delete and not "
                f"applying it, so this has stopped rather than repeating. The "
                f"layer still holds its data."
            )


def append_artifact(gis, layer, fgdb_path, feature_class, preserve_globalids):
    """Upload the artifact and append it into the empty layer.

    The append operation reads its source from an item, so the zip is uploaded
    to the signed-in account's own content first and deleted in the finally
    block below. That temporary item is the only thing this tool creates
    anywhere, and it is the same pattern backup.py uses for its exports.

    ON preserve_globalids: DESIGN.md 11.1 records the restore path as
    append(preserveGlobalIds=True). That is the REST parameter name; the
    arcgis package exposes it on FeatureLayer.append as use_globalids, and the
    two are documented differently enough that whether GlobalIDs actually
    survive is a question for the restore drill to answer rather than for this
    comment to assert. The drill records what was observed. Nothing in this
    project currently depends on GlobalIDs either way.
    """
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    upload = None
    started = time.time()
    try:
        logger.info("Uploading %s to ArcGIS Online as a temporary item", fgdb_path.name)
        upload = gis.content.add(
            {
                "title": f"{UPLOAD_TITLE_PREFIX}{stamp}",
                "type": "File Geodatabase",
                "snippet": "Temporary upload for a layer restore. Safe to delete.",
            },
            data=str(fgdb_path),
        )
        logger.info("Appending from '%s'", feature_class)
        layer.append(
            item_id=upload.id,
            upload_format=UPLOAD_FORMAT,
            source_table_name=feature_class,
            use_globalids=preserve_globalids,
            rollback=True,
        )
    finally:
        if upload is not None:
            try:
                upload.delete()
                logger.info("Deleted the temporary upload item")
            except Exception as exc:
                # Not fatal: the restore itself may have succeeded, and an
                # orphaned upload in somebody's content folder is tidy-up
                # rather than damage. Say so loudly enough to be tidied.
                logger.warning(
                    "Could not delete the temporary upload item %s (%s). "
                    "Delete it by hand from the account's content.",
                    upload.id, type(exc).__name__,
                )
    return time.time() - started


def verify_restore(layer, entry):
    """Check the layer against what the manifest says the artifact held.

    Every line of this is reported rather than raised. By the time it runs the
    data is already back; a mismatch is something a person has to look at, and
    exiting non-zero without saying what matched would be less useful than
    saying it plainly.
    """
    findings = []

    expected_count = entry.get("exported_feature_count")
    actual_count = layer.query(where="1=1", return_count_only=True)
    if expected_count is None:
        findings.append("the manifest records no feature count to compare against")
    elif actual_count == expected_count:
        logger.info("Feature count matches the manifest: %s", f"{actual_count:,}")
    else:
        findings.append(
            f"the layer holds {actual_count:,} features and the artifact held "
            f"{expected_count:,}"
        )

    expected_schema = entry.get("schema_fingerprint")
    if expected_schema:
        differences = compare_schema(schema_fingerprint(layer.properties), expected_schema)
        if differences:
            findings.extend(differences)
        else:
            logger.info("Schema matches the manifest")
    else:
        findings.append("the manifest records no schema fingerprint to compare against")

    extent = layer.properties.get("extent", {})
    logger.info(
        "Extent now: xmin %s ymin %s xmax %s ymax %s",
        extent.get("xmin"), extent.get("ymin"), extent.get("xmax"), extent.get("ymax"),
    )
    return findings


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def parse_arguments(argv):
    parser = argparse.ArgumentParser(
        description=(
            "Restore one hosted feature layer from a backup artifact. Run by a "
            "person, after the data owner has approved it. Describes what it "
            "would do and stops unless --execute is given."
        )
    )
    parser.add_argument("--item-id", required=True, help="The ArcGIS Online item to restore into.")
    parser.add_argument(
        "--layer-index", required=True, type=int,
        help="Which layer of that item. 0 for both of this project's services.",
    )
    parser.add_argument(
        "--fgdb", required=True,
        help="Path to the downloaded .gdb.zip artifact on this machine.",
    )
    parser.add_argument(
        "--manifest", default=None,
        help="Path to manifest.json (default: the copy beside the artifact).",
    )
    parser.add_argument("--portal", default=DEFAULT_PORTAL, help=f"Default: {DEFAULT_PORTAL}")
    parser.add_argument(
        "--feature-class", default=None,
        help=(
            "The feature class inside the geodatabase. Taken from the manifest "
            "unless given; the export names it with a GUID, so never guess it."
        ),
    )
    parser.add_argument(
        "--chunk-size", type=int, default=2000,
        help="Features per call if the single delete fails and it falls back to chunks.",
    )
    parser.add_argument(
        "--no-preserve-globalids", action="store_true",
        help="Append without asking the service to keep the source GlobalIDs.",
    )
    parser.add_argument(
        "--append-only", action="store_true",
        help=(
            "Skip the delete and append into the layer as it stands. For "
            "retrying after an append failed and left the layer empty."
        ),
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually do it. Without this, the run describes and stops.",
    )
    parser.add_argument(
        "--production", action="store_true",
        help="Required to address one of the live production items.",
    )
    parser.add_argument(
        "--approved-by", default=None,
        help="Who approved this restore. Required with --production, and logged.",
    )
    return parser.parse_args(argv)


def confirm(phrase):
    """Block until the operator types the phrase back, or refuse.

    Deliberately not a y/n. The phrase names the thing being restored, so
    answering it requires having read which layer this run is about.
    """
    print(f"\nType {phrase} to proceed, or anything else to stop: ", end="", flush=True)
    try:
        typed = input().strip()
    except EOFError:
        raise ValueError(
            "There is no terminal to confirm on. This tool is run by a person "
            "and has no unattended mode."
        )
    if typed != phrase:
        raise ValueError("Not confirmed. Nothing has been changed.")


def main(argv=None):
    arguments = parse_arguments(argv)
    logging.basicConfig(
        level="INFO", format="%(asctime)s %(levelname)-7s %(message)s",
    )
    logging.getLogger("arcgis").setLevel(logging.WARNING)

    try:
        # First, before credentials, before the artifact, before anything is
        # opened. A refusal must not depend on the rest of the run being
        # sound.
        check_target_allowed(arguments.item_id, arguments.production, arguments.approved_by)

        fgdb_path = Path(arguments.fgdb)
        if not fgdb_path.is_file():
            raise ValueError(f"No artifact at {fgdb_path}.")
        manifest_path = (
            Path(arguments.manifest) if arguments.manifest
            else fgdb_path.parent / "manifest.json"
        )
        if not manifest_path.is_file():
            raise ValueError(
                f"No manifest at {manifest_path}. Every backup set has one "
                f"beside its artifacts; pass it with --manifest if it is "
                f"somewhere else. Without it the artifact cannot be verified, "
                f"and an unverified artifact is not something to empty a layer "
                f"for."
            )

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{manifest_path} is not readable as JSON ({exc}). It is "
                f"damaged or is not a backup manifest, and either way the "
                f"artifact beside it cannot be verified."
            )
        layer_key, entry = manifest_entry_for(manifest, fgdb_path.name)
        verify_artifact(fgdb_path, entry)
        feature_class = arguments.feature_class or entry.get("feature_class")
        if not feature_class:
            raise ValueError(
                "The manifest entry has no feature_class and --feature-class "
                "was not given. The exported geodatabase names it with a GUID, "
                "so it has to come from one of the two rather than be guessed."
            )

        gis = connect_to_agol(arguments.portal)
        item, layer = open_layer(gis, arguments.item_id, arguments.layer_index)
        current = describe_layer(layer)

        logger.info("Restore plan")
        logger.info("  target        %s (%s), layer %s", item.title, arguments.item_id,
                    arguments.layer_index)
        logger.info("  holds now     %s features, %s", f"{current['feature_count']:,}",
                    current["geometry_type"])
        logger.info("  artifact      %s (%s in the manifest)", fgdb_path.name, layer_key)
        logger.info("  taken         %s", entry.get("exported_utc"))
        logger.info("  will restore  %s features from '%s'",
                    f"{entry.get('exported_feature_count', 0):,}", feature_class)
        logger.info("  method        %s then append",
                    "append only (delete skipped)" if arguments.append_only
                    else "delete_features(1=1)")
        if current["supports_truncate"]:
            logger.info("  note          this layer reports supportsTruncate: true, so "
                        "sync is off here - production has it on")
        if arguments.production:
            logger.info("  PRODUCTION    approved by %s", arguments.approved_by)

        if not arguments.execute:
            logger.info(
                "Described only. Nothing has been changed. Add --execute to do it."
            )
            return 0

        logger.warning(
            "This will delete %s features. Every edit made since %s will be lost.",
            f"{current['feature_count']:,}", entry.get("exported_utc"),
        )
        confirm(confirmation_phrase(arguments.item_id, arguments.production))

        started = time.time()
        delete_seconds = 0.0
        if arguments.append_only:
            logger.info("Skipping the delete, as asked")
        else:
            delete_seconds = delete_all_features(layer, arguments.chunk_size)
            logger.info("Layer emptied in %.1f minutes", delete_seconds / 60)

        try:
            append_seconds = append_artifact(
                gis, layer, fgdb_path, feature_class,
                preserve_globalids=not arguments.no_preserve_globalids,
            )
        except Exception:
            # The dangerous window, and the one moment this tool must be
            # loudest: the layer has been emptied and the replacement did not
            # go in. Say what state it is in and exactly how to retry, because
            # whoever is reading this is having a bad afternoon.
            logger.error(
                "THE APPEND FAILED AND THE LAYER IS EMPTY. The artifact is "
                "intact at %s. Retry the append alone with the same command "
                "plus --append-only; do not re-run the delete.", fgdb_path,
            )
            raise

        logger.info("Append finished in %.1f minutes", append_seconds / 60)

        # Re-opened rather than reused. A FeatureLayer caches the properties
        # it was built with, so the extent and the schema read off the object
        # that has just been emptied and refilled would be the ones from
        # before the restore - a well-formed wrong answer of exactly the kind
        # DESIGN.md 12 catalogues, and it would be reported as a verification.
        _, refreshed = open_layer(gis, arguments.item_id, arguments.layer_index)
        findings = verify_restore(refreshed, entry)
        total = time.time() - started
        logger.info(
            "Restore complete in %.1f minutes (delete %.1f, append %.1f)",
            total / 60, delete_seconds / 60, append_seconds / 60,
        )
        if findings:
            logger.warning("The restore finished, and these need looking at:")
            for finding in findings:
                logger.warning("  %s", finding)
            return 1
        logger.info("Everything checked matches the manifest.")
        return 0

    except ValueError as refusal:
        # Every deliberate stop in this file raises ValueError with a message
        # written for the person reading it, so naming the type on the way out
        # would add a word that means nothing to them and slightly undermines
        # a message that is meant to be read as an instruction.
        logger.error("%s", refusal)
        return 1
    except Exception as exc:
        # Anything unforeseen. A traceback is not what somebody restoring a
        # layer under pressure should be reading, but the type is the only
        # clue there is, so it stays.
        logger.error("%s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
