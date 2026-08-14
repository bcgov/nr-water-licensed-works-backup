"""Preflight checks for the Water Licensed Works backup pipeline.

Answers one question: is this environment able to run the pipeline, and do
the facts the design depends on still hold?

Run it by hand - after a credential rotation, after an AGOL upgrade, when a
new maintainer sets up their environment, and as the first step in
diagnosing a SYSTEM_FAIL:

    python preflight.py

Read-only against AGOL. It authenticates, reads layer definitions and runs
count and extent queries. It does not export, and it writes nothing to AGOL
at all - not even a temporary export item. In object storage it writes and
deletes one small test object under this project's own prefix, because that
is the only way to prove the credentials can actually write.

Needs config.yml and the five environment variables in REQUIRED_ENV.
Exits 0 if every required check passed, 1 if any of them failed.

Nothing here prints a secret, and nothing prints the AGOL username - this
repository is public and may run in a workflow whose logs are world
readable. Presence and outcomes only.
"""

import datetime
import json
import logging
import os
import sys
import time
from dataclasses import dataclass

import boto3
import yaml
from arcgis.features import FeatureLayerCollection
from arcgis.gis import GIS
from botocore.exceptions import ClientError

CONFIG_PATH = "config.yml"

REQUIRED_ENV = [
    "S3_NRS_ENDPOINT",
    "S3_GSS_GEODRIVE_KEY_ID",
    "S3_GSS_GEODRIVE_SECRET_KEY",
    "AGO_USERNAME_WINS",
    "AGO_PASSWORD_WINS",
]

# The facts DESIGN.md section 3 asserts about the sources. They live here
# rather than in config.yml because they are assertions this tool exists to
# re-verify, not settings anyone should be tuning. If one of them comes back
# wrong, DESIGN.md is wrong: report it and stop, do not code around it.
EXPECTED_LAYER_FACTS = {
    "lines": {
        "geometry_type": "esriGeometryPolyline",
        "fields": ["OBJECTID", "TWRK_TAG", "FEATURE_CODE", "DISPLAY_COLOUR",
                   "GlobalID", "Shape__Length"],
        "domain_coded_values": 15,
        "subtypes": 14,
    },
    "points": {
        "geometry_type": "esriGeometryPoint",
        "fields": ["OBJECTID", "TWRK_TAG", "FEATURE_CODE", "GlobalID"],
        "domain_coded_values": 10,
        "subtypes": 10,
    },
}

EXPECTED_WKID = 3005

# The grid envelope in config.yml doubles as a generous outline of British
# Columbia, so the same bounds answer "is anything wildly outside the
# province" - which is how the two known bad point records (DESIGN.md
# section 4) show up. Reading it from config rather than repeating it here
# keeps preflight and checks.py agreeing by construction.

# A full pass at the configured 50 km is 1,020 queries per layer. Preflight
# is verifying that the mechanism works, not the production cell size, so it
# uses a deliberately coarse cell - 72 cells, about 13 seconds a layer.
PREFLIGHT_CELL_SIZE_METRES = 200000

# DESIGN.md section 4. Read only - the data owner is correcting these, and
# GeoBC does not touch them. Delete this constant and the check that uses it
# once section 4 is closed.
KNOWN_BAD_POINT_OBJECTIDS = [150984, 150985]

# These layers are edited through QuickWins while preflight is running, and
# a count and the two partition counts below it are three separate queries
# with no snapshot between them. A feature or two of disagreement is a
# concurrent edit; a large disagreement means the geometry filter is
# malformed and no number from it can be trusted.
LIVE_EDIT_TOLERANCE = 5

logger = logging.getLogger("preflight")


@dataclass
class Result:
    """One line of the preflight report.

    status is OK (as expected), INFO (a measurement, nothing to pass or
    fail), WARN (differs from DESIGN.md but does not stop the pipeline) or
    FAIL (the pipeline cannot be trusted until this is resolved).
    """

    name: str
    status: str
    detail: str


def load_config(path):
    """Read config.yml. Everything the pipeline is told lives in there."""
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def count_in_envelope(layer, bounds, wkid, spatial_rel="esriSpatialRelIntersects"):
    """Count the features touching a rectangle. One definition, used by every
    envelope query here, because the filter shape is the easy thing to get
    wrong: the geometry has to be a JSON *string* inside the filter, and the
    server has to be told the rectangle's spatial reference with inSR. Get
    either wrong and the query returns a plausible number rather than an
    error, which is the failure this whole project is most wary of.
    """
    envelope = {
        "xmin": bounds["xmin"], "ymin": bounds["ymin"],
        "xmax": bounds["xmax"], "ymax": bounds["ymax"],
        "spatialReference": {"wkid": wkid},
    }
    return layer.query(
        where="1=1",
        geometry_filter={
            "geometry": json.dumps(envelope),
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": spatial_rel,
            "inSR": wkid,
        },
        return_count_only=True,
    )


def check_environment_variables():
    """Report which required variables are set. Presence only, never values."""
    results = []
    for name in REQUIRED_ENV:
        if os.getenv(name):
            results.append(Result(f"env {name}", "OK", "set"))
        else:
            results.append(Result(
                f"env {name}", "FAIL",
                "not set. The five variables in REQUIRED_ENV must all be "
                "present; on a developer machine they mirror the GitHub "
                "Actions secrets of the same name.",
            ))
    return results


def connect_to_agol(config):
    """Authenticate and return the GIS. Raises if the credentials are refused."""
    return GIS(
        config["agol"]["url"],
        os.getenv("AGO_USERNAME_WINS"),
        os.getenv("AGO_PASSWORD_WINS"),
    )


def check_export_rights(gis, item, layer_key):
    """Can this account export this item to a File Geodatabase?

    Two ways to be allowed: own the item, or have 'Allow others to export to
    different formats' enabled on it - which is what puts Extract in the
    service capabilities. Export also creates a temporary item in the
    account's own content, so it needs the createItem privilege too.
    """
    results = []

    # item.owner and the username are both withheld from the log on purpose;
    # the comparison is what matters and this may run in a public workflow.
    is_owner = item.owner == gis.users.me.username
    capabilities = str(item.layers[0].properties.get("capabilities", ""))
    can_extract = "Extract" in capabilities
    can_create_items = "portal:user:createItem" in gis.users.me.privileges

    if is_owner or can_extract:
        results.append(Result(
            f"{layer_key} export rights", "OK",
            f"owner={is_owner}, Extract capability={can_extract}",
        ))
    else:
        results.append(Result(
            f"{layer_key} export rights", "FAIL",
            "this account neither owns the item nor has Extract on the "
            "service. Ask the item owner to enable 'Allow others to export "
            "to different formats' on the item.",
        ))

    if can_create_items:
        results.append(Result(f"{layer_key} createItem privilege", "OK", "present"))
    else:
        results.append(Result(
            f"{layer_key} createItem privilege", "FAIL",
            "export() creates a temporary item in this account's content and "
            "cannot run without portal:user:createItem.",
        ))

    export_formats = str(
        FeatureLayerCollection.fromitem(item).properties.get("supportedExportFormats", "")
    )
    if "filegdb" in export_formats:
        results.append(Result(f"{layer_key} filegdb export format", "OK", "supported"))
    else:
        results.append(Result(
            f"{layer_key} filegdb export format", "FAIL",
            f"'filegdb' missing from supportedExportFormats ({export_formats}). "
            "FGDB is the only format that preserves domains and subtypes.",
        ))

    return results


def check_layer_facts(item, layer_key):
    """Re-verify the layer definition facts the design is built on."""
    results = []
    layer = item.layers[0]
    properties = layer.properties
    expected = EXPECTED_LAYER_FACTS[layer_key]

    # The entire restore path in DESIGN.md 11.1 is delete_features then
    # append, so this one is not optional.
    if properties.get("supportsAppend"):
        results.append(Result(f"{layer_key} supportsAppend", "OK", "true"))
    else:
        results.append(Result(
            f"{layer_key} supportsAppend", "FAIL",
            "append is unavailable, so the documented restore path cannot "
            "work. DESIGN.md section 11.1 needs revisiting before any more "
            "code is written.",
        ))

    # Expected false because sync is enabled. If it ever turns true the
    # restore path gets simpler, which is worth knowing but breaks nothing.
    if properties.get("supportsTruncate"):
        results.append(Result(
            f"{layer_key} supportsTruncate", "WARN",
            "true, but DESIGN.md 11.1 says false and uses delete_features. "
            "Sync may have been disabled on the service.",
        ))
    else:
        results.append(Result(f"{layer_key} supportsTruncate", "OK", "false, as designed"))

    if properties.get("supportsRollbackOnFailureParameter"):
        results.append(Result(f"{layer_key} rollback on failure", "OK", "supported"))
    else:
        results.append(Result(
            f"{layer_key} rollback on failure", "WARN",
            "not supported; a partially applied restore could not be rolled back.",
        ))

    # syncEnabled is a service-level property, not a layer-level one - the
    # layer JSON does not carry it at all.
    service = FeatureLayerCollection.fromitem(item).properties
    results.append(Result(
        f"{layer_key} syncEnabled", "INFO",
        f"{bool(service.get('syncEnabled'))} (service level)",
    ))
    results.append(Result(
        f"{layer_key} capabilities", "INFO", str(properties.get("capabilities", "")),
    ))

    geometry_type = properties.get("geometryType")
    if geometry_type == expected["geometry_type"]:
        results.append(Result(f"{layer_key} geometry type", "OK", geometry_type))
    else:
        results.append(Result(
            f"{layer_key} geometry type", "FAIL",
            f"{geometry_type}, expected {expected['geometry_type']}.",
        ))

    field_names = [field["name"] for field in properties.get("fields", [])]
    if field_names == expected["fields"]:
        results.append(Result(
            f"{layer_key} field list", "OK", f"{len(field_names)} fields, unchanged",
        ))
    else:
        results.append(Result(
            f"{layer_key} field list", "FAIL",
            f"{field_names} does not match the expected {expected['fields']}. "
            "A schema change fails the check rules from day one and would "
            "also change the FGDB artifact.",
        ))

    coded_value_count = 0
    domain_name = None
    for field in properties.get("fields", []):
        domain = field.get("domain")
        if domain and domain.get("type") == "codedValue":
            domain_name = domain.get("name")
            coded_value_count = len(domain.get("codedValues", []))
    if coded_value_count == expected["domain_coded_values"]:
        results.append(Result(
            f"{layer_key} domain coded values", "OK",
            f"{domain_name}: {coded_value_count}",
        ))
    else:
        results.append(Result(
            f"{layer_key} domain coded values", "WARN",
            f"{domain_name}: {coded_value_count}, expected "
            f"{expected['domain_coded_values']}. A new works type may have "
            "been added - confirm with the data owner.",
        ))

    subtype_count = len(properties.get("types", []))
    if subtype_count == expected["subtypes"]:
        results.append(Result(f"{layer_key} subtypes", "OK", str(subtype_count)))
    else:
        results.append(Result(
            f"{layer_key} subtypes", "WARN",
            f"{subtype_count}, expected {expected['subtypes']}.",
        ))

    wkid = properties.get("extent", {}).get("spatialReference", {}).get("latestWkid")
    if wkid == EXPECTED_WKID:
        results.append(Result(f"{layer_key} spatial reference", "OK", f"BC Albers ({wkid})"))
    else:
        results.append(Result(
            f"{layer_key} spatial reference", "FAIL",
            f"wkid {wkid}, expected {EXPECTED_WKID}. Every distance threshold "
            "in config.yml is in BC Albers metres.",
        ))

    # No editFieldsInfo means editor tracking is off, so there are no
    # last_edited_user / last_edited_date fields to check against.
    editor_tracking_on = properties.get("editFieldsInfo") is not None
    results.append(Result(
        f"{layer_key} editor tracking", "INFO",
        "ON - the edits-per-user check in DESIGN.md 7.2 is now possible"
        if editor_tracking_on else "off, as DESIGN.md records",
    ))

    if properties.get("enableNullGeometry"):
        results.append(Result(
            f"{layer_key} null geometry", "WARN",
            "enabled, so null geometry checks would now be worth adding.",
        ))
    else:
        results.append(Result(f"{layer_key} null geometry", "OK", "disabled, as designed"))

    last_edit = properties.get("editingInfo", {}).get("lastEditDate")
    if last_edit:
        when = datetime.datetime.fromtimestamp(
            last_edit / 1000, datetime.timezone.utc
        ).isoformat()
        results.append(Result(f"{layer_key} lastEditDate", "INFO", when))

    return results


def check_live_counts_and_extent(item, layer_key, grid):
    """Live queries only. Cached count and extent are declared estimates."""
    results = []
    layer = item.layers[0]

    feature_count = layer.query(where="1=1", return_count_only=True)
    results.append(Result(f"{layer_key} live feature count", "INFO", f"{feature_count:,}"))

    extent = layer.query(where="1=1", return_extent_only=True).get("extent", {})
    results.append(Result(
        f"{layer_key} live extent", "INFO",
        f"xmin={extent.get('xmin'):,.0f} ymin={extent.get('ymin'):,.0f} "
        f"xmax={extent.get('xmax'):,.0f} ymax={extent.get('ymax'):,.0f}",
    ))

    inside = count_in_envelope(layer, grid, grid["wkid"])
    outside = count_in_envelope(
        layer, grid, grid["wkid"], spatial_rel="esriSpatialRelDisjoint"
    )

    # Known-answer guard. During design a malformed geometry filter returned
    # 52,986 of 53,986 features - a plausible number that was simply wrong.
    # Intersects and disjoint must partition the layer, so their sum has to
    # come back to the total give or take a concurrent edit.
    drift = abs((inside + outside) - feature_count)
    if drift > LIVE_EDIT_TOLERANCE:
        results.append(Result(
            f"{layer_key} extent sanity", "FAIL",
            f"geometry filter is not returning a partition: inside {inside:,} "
            f"+ outside {outside:,} is {drift:,} away from the total "
            f"{feature_count:,}. Too large to be a concurrent edit, so do "
            "not trust either number.",
        ))
    elif drift:
        results.append(Result(
            f"{layer_key} features outside BC", "INFO",
            f"{outside:,} outside; counts moved by {drift} during the run, "
            "which is editing in progress rather than a problem",
        ))
    elif outside == 0:
        results.append(Result(
            f"{layer_key} features outside BC", "OK",
            "none - the extent sits inside the province",
        ))
    else:
        results.append(Result(
            f"{layer_key} features outside BC", "WARN",
            f"{outside:,} feature(s) fall outside the BC envelope, so the "
            "extent metric is polluted and extent-based baselining should "
            "wait. DESIGN.md section 4.",
        ))

    return results


def check_known_bad_records(item, layer_key, grid):
    """DESIGN.md section 4: are the two bad point records corrected yet?

    Baseline collection is not supposed to start until they are. Read-only -
    GeoBC does not edit this data. Remove once section 4 is closed.
    """
    if layer_key != "points":
        return []

    where = f"OBJECTID IN ({', '.join(str(oid) for oid in KNOWN_BAD_POINT_OBJECTIDS)})"
    features = item.layers[0].query(
        where=where, out_fields="OBJECTID", return_geometry=True
    ).features

    still_outside = [
        feature.attributes["OBJECTID"]
        for feature in features
        if not (grid["xmin"] <= feature.geometry["x"] <= grid["xmax"]
                and grid["ymin"] <= feature.geometry["y"] <= grid["ymax"])
    ]

    if not features:
        return [Result(
            "points known bad records", "OK",
            f"OBJECTIDs {KNOWN_BAD_POINT_OBJECTIDS} no longer present",
        )]
    if still_outside:
        return [Result(
            "points known bad records", "WARN",
            f"OBJECTIDs {still_outside} are still outside BC. DESIGN.md 13 "
            "says baseline collection starts after they are corrected.",
        )]
    return [Result(
        "points known bad records", "OK",
        f"OBJECTIDs {KNOWN_BAD_POINT_OBJECTIDS} now fall inside BC",
    )]


def check_spatial_grid(item, layer_key, grid):
    """Does the fixed-grid spatial bin check in DESIGN.md 7.2.2 still work?

    Geohash binning, which this check was originally designed around, does
    not exist on these services - see DESIGN.md 7.2.1. The grid is ours
    instead: a lattice of envelopes over BC, one count query per cell.

    Run at a coarse cell size. What is being verified is that the mechanism
    works and the filter is well formed, neither of which depends on how
    finely the grid is cut, and a full pass at the configured size is 1,020
    queries per layer.

    The guard is the reason this check earns its place. The cells tile the
    grid envelope exactly, so their counts have to add back up to a single
    count taken over the whole envelope. Points land on it exactly. A
    polyline crossing a cell boundary is counted in both cells, so lines come
    in slightly over - 0.51% at 50 km cells when this was measured. Anything
    outside that is a malformed filter returning numbers that look fine and
    are not.
    """
    results = []
    layer = item.layers[0]
    tolerance_percent = grid["max_sum_overcount_percent"]

    cells = []
    x = grid["xmin"]
    while x < grid["xmax"]:
        y = grid["ymin"]
        while y < grid["ymax"]:
            cells.append({
                "xmin": x, "ymin": y,
                "xmax": x + PREFLIGHT_CELL_SIZE_METRES,
                "ymax": y + PREFLIGHT_CELL_SIZE_METRES,
            })
            y += PREFLIGHT_CELL_SIZE_METRES
        x += PREFLIGHT_CELL_SIZE_METRES

    started = time.time()
    counts = [count_in_envelope(layer, cell, grid["wkid"]) for cell in cells]
    elapsed = time.time() - started

    total_in_envelope = count_in_envelope(layer, grid, grid["wkid"])
    cell_sum = sum(counts)
    populated = [count for count in counts if count > 0]

    results.append(Result(
        f"{layer_key} spatial grid", "INFO",
        f"{len(populated)}/{len(cells)} cells populated at "
        f"{PREFLIGHT_CELL_SIZE_METRES // 1000} km, {elapsed:.0f}s "
        f"({elapsed / len(cells):.2f}s per query)",
    ))

    if not populated:
        results.append(Result(
            f"{layer_key} spatial grid sum", "FAIL",
            "every cell came back empty, so the grid envelope in "
            "checks.spatial_grid does not cover the data at all.",
        ))
        return results

    overcount = cell_sum - total_in_envelope
    overcount_percent = 100.0 * overcount / total_in_envelope
    if overcount < 0:
        results.append(Result(
            f"{layer_key} spatial grid sum", "FAIL",
            f"cells total {cell_sum:,} but the whole envelope holds "
            f"{total_in_envelope:,}. The cells tile the envelope, so they "
            "cannot come to less than it. The geometry filter is wrong.",
        ))
    elif overcount_percent > tolerance_percent:
        results.append(Result(
            f"{layer_key} spatial grid sum", "FAIL",
            f"cells total {cell_sum:,} against {total_in_envelope:,} in the "
            f"envelope, {overcount_percent:.2f}% over the "
            f"{tolerance_percent}% allowed by max_sum_overcount_percent. "
            "Too much to be boundary-crossing features.",
        ))
    else:
        results.append(Result(
            f"{layer_key} spatial grid sum", "OK",
            f"cells total {cell_sum:,} against {total_in_envelope:,} in the "
            f"envelope ({overcount_percent:+.2f}%, within {tolerance_percent}%)",
        ))
    return results


def check_object_storage(config):
    """Prove the credentials can write under the project prefix, and report
    whether the bucket versioning named in DESIGN.md 6.6 is actually on."""
    results = []
    bucket = config["storage"]["bucket"]
    prefix = config["storage"]["prefix"]

    # endpoint_url is mandatory: this is NRS object storage, not AWS, and
    # boto3 will quietly try to reach AWS without it.
    client = boto3.client(
        "s3",
        endpoint_url=os.getenv("S3_NRS_ENDPOINT"),
        aws_access_key_id=os.getenv("S3_GSS_GEODRIVE_KEY_ID"),
        aws_secret_access_key=os.getenv("S3_GSS_GEODRIVE_SECRET_KEY"),
    )

    try:
        versioning = client.get_bucket_versioning(Bucket=bucket).get("Status")
    except ClientError as exc:
        versioning = None
        results.append(Result(
            "bucket versioning", "WARN",
            f"could not be read ({exc.response['Error'].get('Code')}). "
            "DESIGN.md 6.6 relies on it as the mitigation for the single "
            "full-access key pair.",
        ))
    if versioning == "Enabled":
        results.append(Result("bucket versioning", "OK", "Enabled"))
    elif versioning is not None:
        results.append(Result(
            "bucket versioning", "WARN",
            f"{versioning}. DESIGN.md 6.6 names versioning as the only "
            "mitigation for one key pair with delete rights.",
        ))

    # Versioning only protects a deletion for as long as the noncurrent
    # versions survive, so the lifecycle rule bounds the mitigation.
    try:
        rules = client.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", [])
        expiry_days = [
            rule["NoncurrentVersionExpiration"]["NoncurrentDays"]
            for rule in rules
            if "NoncurrentVersionExpiration" in rule
        ]
        if expiry_days:
            results.append(Result(
                "noncurrent version expiry", "INFO",
                f"{min(expiry_days)} days - a deleted object is recoverable "
                "only within that window",
            ))
    except ClientError:
        # A missing or unreadable lifecycle policy is not a failure.
        pass

    # Round-trip a small object under this project's prefix and nowhere else.
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"{prefix}_preflight_{stamp}.txt"
    body = b"preflight round trip - safe to delete"
    try:
        client.put_object(Bucket=bucket, Key=key, Body=body)
        head = client.head_object(Bucket=bucket, Key=key)
        fetched = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        if head["ContentLength"] == len(body) and fetched == body:
            results.append(Result("storage round trip", "OK", "put, head and get match"))
        else:
            results.append(Result(
                "storage round trip", "FAIL",
                "the object came back different from what was written.",
            ))
        client.delete_object(Bucket=bucket, Key=key)
        results.append(Result("storage delete", "OK", "test object removed"))
    except ClientError as exc:
        results.append(Result(
            "storage round trip", "FAIL",
            f"{exc.response['Error'].get('Code')} writing under {prefix}. "
            "Check the endpoint and key pair.",
        ))

    return results


def report(results):
    """Print the report and return the process exit code."""
    width = max(len(result.name) for result in results)
    for result in results:
        logger.info("%-6s %-*s  %s", result.status, width, result.name, result.detail)

    failures = [result for result in results if result.status == "FAIL"]
    warnings = [result for result in results if result.status == "WARN"]
    logger.info(
        "%d checks: %d FAIL, %d WARN", len(results), len(failures), len(warnings)
    )
    if failures:
        logger.info("Preflight failed. Do not build on these assumptions:")
        for failure in failures:
            logger.info("  %s: %s", failure.name, failure.detail)
        return 1
    return 0


def main():
    config = load_config(CONFIG_PATH)
    logging.basicConfig(
        level=config.get("logging", {}).get("level", "INFO"), format="%(message)s"
    )
    # The arcgis package narrates its own portal calls at INFO, which buries
    # the report this tool exists to print.
    logging.getLogger("arcgis").setLevel(logging.WARNING)

    results = check_environment_variables()
    if any(result.status == "FAIL" for result in results):
        logger.info("Required environment variables are missing; stopping here.")
        return report(results)

    try:
        gis = connect_to_agol(config)
        results.append(Result("AGOL authentication", "OK", config["agol"]["url"]))
    except Exception as exc:
        # Deliberately no exception message and no username: this repository
        # is public and the text of an auth failure can echo credentials.
        results.append(Result(
            "AGOL authentication", "FAIL",
            f"could not sign in ({type(exc).__name__}). Check AGO_USERNAME_WINS "
            "and AGO_PASSWORD_WINS, and that the account is not locked.",
        ))
        return report(results)

    grid = config["checks"]["spatial_grid"]
    for layer_key, layer_config in config["layers"].items():
        item = gis.content.get(layer_config["item_id"])
        if item is None:
            results.append(Result(
                f"{layer_key} item", "FAIL",
                f"item_id in config.yml is not visible to this account. "
                f"Confirm the ID and that the account is in the sharing group.",
            ))
            continue
        results.append(Result(f"{layer_key} item", "OK", layer_config["name"]))
        results.extend(check_export_rights(gis, item, layer_key))
        results.extend(check_layer_facts(item, layer_key))
        results.extend(check_live_counts_and_extent(item, layer_key, grid))
        results.extend(check_known_bad_records(item, layer_key, grid))
        results.extend(check_spatial_grid(item, layer_key, grid))

    results.extend(check_object_storage(config))
    return report(results)


if __name__ == "__main__":
    sys.exit(main())
