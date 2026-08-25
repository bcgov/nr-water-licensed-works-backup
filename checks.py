"""Daily integrity checks on the WATER_LICENSED_WORKS hosted feature layers.

Runs late afternoon, after the day's editing and with time to act before the
nightly staging push. One run collects a set of measurements from live
queries, compares them against the metrics history, and returns a
CheckResult whose status is one of PASS, BASELINE, WARN, DATA_FAIL or
SYSTEM_FAIL.

    python run_checks.py

Read-only with respect to the production feature layers. This module
authenticates, reads layer definitions and issues count, extent, statistic
and envelope queries. It never deletes, appends to, updates, calculates or
truncates a feature, and it holds no restore path of any kind. A failing
check raises an alert and does nothing else - recovery is a manual decision
made by the data owner.

One thing it reports is not a comparison at all. A validity finding - a
feature that could not be right on any day, whatever yesterday looked like -
is carried in the run's details and never in its status, so that a
known-bad record cannot block monthly promotion for as long as it goes
uncorrected. DESIGN.md 7.6.1, and the section comment above
outside_bc_finding.

Three constraints shape what is here.

run_checks(config) must be importable and callable from outside this
repository. Phase 2 calls it from the staging script on the NRIDS server, so
nothing here may assume GitHub Actions, and nothing may import arcpy.

For the same reason this module must not import backup.py, which pulls in
pyogrio to read a File Geodatabase. That dependency has no business on the
NRIDS server, so the handful of helpers the two jobs share live in status.py,
which both can import because it is standard library and storage.py.

Every AGOL call lives in one of the thin query functions below, each doing
nothing but issue a query and return a plain Python value. The rule
functions further down operate only on those values and on dicts loaded from
storage, never on an arcgis object. If the transport ever has to change,
that is a handful of functions rather than a rewrite - and it is what makes
the rules testable against hand-written fixtures, which DESIGN.md 12 treats
as a requirement rather than a nicety.

Needs config.yml, the AGOL credentials and the object storage credentials.
"""

import datetime
import hashlib
import json
import logging
import math
import os
import statistics
import time
from dataclasses import dataclass

from arcgis.gis import GIS

import storage
# Shared with backup.py, which cannot be imported here - see the module
# docstring. status.py carries these so that one edit is one edit.
from status import (
    local_date_stamp,
    resolve_code_version,
    safe_reason,
    schema_fingerprint,
    utc_now,
    utc_stamp,
)

logger = logging.getLogger("checks")

# Bumped if the shape of a metrics file changes, so that a future reader can
# tell an old file from a malformed one.
METRICS_VERSION = 1

# A status recorded against a run whose measurements are trustworthy enough
# to compare a later run against. DATA_FAIL is deliberately absent: DESIGN.md
# 7.3 compares against the previous *successful* day, because comparing
# tomorrow against today's anomaly is how a mass deletion becomes the new
# normal overnight and the alert stops firing.
COMPARABLE_STATUSES = ("PASS", "WARN", "BASELINE")

# These layers are edited through QuickWins while the check is running, and
# the measurements below are separate queries with no snapshot between them.
# A feature or two of disagreement between two counts is a concurrent edit; a
# large disagreement means a query is malformed and no number from it can be
# trusted. Same value and same reasoning as preflight.py.
LIVE_EDIT_TOLERANCE = 5

# A spatial bin alert names the cells that changed. Past a handful the list
# stops being something a person can act on and starts being a wall of text
# in an email, so the rest are summarised as a count.
MAX_CELLS_NAMED = 5

# The same idea for the validity finding below, at twice the number. The
# OBJECTIDs are what the recipient actually works from - they are the whole
# point of that alert, because whoever reads it is whoever would correct the
# records - so more of them earns its place where more cell names would not,
# and ten short integers still fit on one line.
MAX_OBJECTIDS_NAMED = 10

# groupByFieldsForStatistics returns one row per distinct value, and a null
# comes back as JSON null. Rendered as this rather than through str(), which
# would turn it into the string "None" and make it indistinguishable from a
# feature code that happened to read "None".
NULL_VALUE_LABEL = "(null)"


@dataclass
class CheckResult:
    """The outcome of one check run.

    status is one of the five in DESIGN.md 7.1 and nothing else. The check
    job is the only thing that produces the whole set: promotion in backup.py
    and the routing table in jenkins/notify.py both switch on these values,
    so a sixth cannot be introduced without changing both.

    summary is one line written for a non-technical reader, because it
    becomes the body of the alert email.

    metrics is what gets written to metrics/<date>.json, and is empty when the
    run failed before it had a complete set of measurements.
    """

    status: str
    summary: str
    failures: list
    details: list
    metrics: dict
    date_stamp: str
    code_version: str


@dataclass
class Violation:
    """One rule that was broken.

    severity is FAIL or WARN, taken from config.yml rather than decided here,
    because only FAIL gates the nightly push in Phase 2.

    comparison records what the measurement was judged against - the previous
    day, the 30-day median, the monthly anchor, or nothing at all for a rule
    that needs no history. An alert that says a count fell 12% is not
    actionable until the reader knows 12% against what.
    """

    layer: str
    rule: str
    severity: str
    comparison: str
    message: str


# ---------------------------------------------------------------------------
# Thin query functions
#
# One per metric. Each issues a query and returns a plain value. No rule
# logic below this line touches an arcgis object.
# ---------------------------------------------------------------------------


def connect_to_agol(config):
    """Authenticate and return the GIS. Raises if the credentials are refused."""
    return GIS(
        config["agol"]["url"],
        os.getenv("AGO_USERNAME_WINS"),
        os.getenv("AGO_PASSWORD_WINS"),
    )


def open_layer(gis, layer_config):
    """The feature layer named by one entry in config.yml's layers block."""
    item = gis.content.get(layer_config["item_id"])
    if item is None:
        raise ValueError(
            f"The item_id '{layer_config['item_id']}' in config.yml is not "
            f"visible to this account. Confirm the ID and that the account is "
            f"still in the QuickWins sharing group."
        )
    return item.layers[layer_config["layer_index"]]


def query_feature_count(layer):
    """How many features the layer holds, right now.

    Live rather than the cached property: infoInEstimates declares the cached
    count an estimate, and every threshold in config.yml is calibrated
    against real counts.
    """
    return int(layer.query(where="1=1", return_count_only=True))


def query_extent(layer):
    """The live bounding box, as a plain dict of four numbers.

    There is deliberately no centroid alongside this. DESIGN.md 7.2 lists a
    CentroidAggregate metric to catch a distribution shift that extent misses,
    and on these services it cannot: measured on both layers 2026-08-14, the
    statistic returns the centre of the aggregate envelope rather than the
    mean of the feature coordinates, reproducing (xmin + xmax) / 2 and
    (ymin + ymax) / 2 to within 2e-8. It is the extent restated, so recording
    it would put a number in every metrics file that looks like an
    independent spatial signal and is not - which during the baseline period
    is worse than not having it, and is the exact failure this project keeps
    being bitten by. The fixed grid below is what actually covers localized
    change. Any stored extent yields the centre if it is ever wanted.
    """
    extent = layer.query(where="1=1", return_extent_only=True).get("extent", {})
    return {corner: float(extent[corner]) for corner in ("xmin", "ymin", "xmax", "ymax")}


def query_total_length(layer, length_field):
    """Sum of the shape length field, in planar metres.

    Metres because these services report geometryProperties.units as
    esriMeters in BC Albers, so this needs no conversion. Lines only - points
    carry no Shape__Length, which is what has_length_field in config.yml
    records.
    """
    result = layer.query(
        where="1=1",
        out_statistics=[{
            "statisticType": "sum",
            "onStatisticField": length_field,
            "outStatisticFieldName": "total_length",
        }],
    )
    if not result.features:
        raise RuntimeError(
            f"The SUM query on {length_field} returned no rows, so total "
            f"length could not be measured."
        )
    return float(result.features[0].attributes["total_length"])


def query_null_count(layer, field_name):
    """How many features have no value in this field.

    IS NULL only, which is what DESIGN.md 7.2 specifies. Worth knowing when
    reading the number: both layers also carry blank-string values in
    FEATURE_CODE, and those are not nulls and are not counted here.
    """
    return int(layer.query(where=f"{field_name} IS NULL", return_count_only=True))


def query_value_counts(layer, field_name, feature_count):
    """How many features carry each distinct value of a field.

    Collected with groupByFieldsForStatistics rather than
    returnDistinctValues, for a reason worth recording. The arcgis wrapper's
    query(return_distinct_values=True) does not apply the distinct at all
    against these services - it came back with 6,717 rows for a field holding
    29 distinct values, no error and no warning. The raw REST parameter works
    correctly, but grouping is better still: it returns the same distinct
    values *and* a count per value whose total has to come back to the live
    feature count, so the query proves itself on every run.

    That guard also covers truncation. maxRecordCount is 1,000 on both
    layers, so a schema change that exploded the number of distinct values
    would silently return a short answer; a short answer cannot sum to the
    total.
    """
    response = layer._con.post(
        f"{layer.url}/query",
        {
            "f": "json",
            "where": "1=1",
            "returnGeometry": "false",
            "groupByFieldsForStatistics": field_name,
            "outStatistics": json.dumps([{
                "statisticType": "count",
                "onStatisticField": layer.properties["objectIdField"],
                "outStatisticFieldName": "value_count",
            }]),
        },
    )

    value_counts = {}
    for feature in response.get("features", []):
        attributes = feature.get("attributes", {})
        value = attributes.get(field_name)
        label = NULL_VALUE_LABEL if value is None else str(value)
        value_counts[label] = int(attributes.get("value_count") or 0)

    grouped_total = sum(value_counts.values())
    if abs(grouped_total - feature_count) > LIVE_EDIT_TOLERANCE:
        raise RuntimeError(
            f"The grouped counts on {field_name} total {grouped_total:,} against "
            f"{feature_count:,} features in the layer. Too large a gap to be "
            f"editing during the run, so the grouping is either truncated or "
            f"malformed and no value from it can be trusted. No metrics file "
            f"has been written."
        )
    return value_counts


def count_in_envelope(layer, bounds, wkid, spatial_rel="esriSpatialRelIntersects"):
    """Count the features touching a rectangle.

    One definition, used by every envelope query here, because the filter
    shape is the easy thing to get wrong: the geometry has to be a JSON
    *string* inside the filter, and the server has to be told the rectangle's
    spatial reference with inSR. Get either wrong and the query returns a
    plausible number rather than an error, which is the failure this whole
    project is most wary of.
    """
    envelope = {
        "xmin": bounds["xmin"], "ymin": bounds["ymin"],
        "xmax": bounds["xmax"], "ymax": bounds["ymax"],
        "spatialReference": {"wkid": wkid},
    }
    return int(layer.query(
        where="1=1",
        geometry_filter={
            "geometry": json.dumps(envelope),
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": spatial_rel,
            "inSR": wkid,
        },
        return_count_only=True,
    ))


def query_objectids_outside_envelope(layer, bounds, wkid):
    """The OBJECTIDs of the features that fall outside the envelope entirely.

    The count of them is already collected - it is the disjoint half of the
    grid partition below - and this asks the same question again for the
    identifiers, because a validity alert that says "2 features are outside
    British Columbia" is worth much less to the person who would correct them
    than one that says which two.

    Issued as a raw REST call for the same reason query_value_counts is: the
    arcgis wrapper has twice now returned a well-formed wrong answer from a
    query parameter this project depends on, so the parameter that matters
    here goes to the service directly and the response shape is checked
    rather than assumed. returnIdsOnly is not subject to maxRecordCount, and
    the caller compares the number of identifiers against the count it
    already has, so a short answer cannot pass unnoticed.
    """
    envelope = {
        "xmin": bounds["xmin"], "ymin": bounds["ymin"],
        "xmax": bounds["xmax"], "ymax": bounds["ymax"],
        "spatialReference": {"wkid": wkid},
    }
    response = layer._con.post(
        f"{layer.url}/query",
        {
            "f": "json",
            "where": "1=1",
            "returnIdsOnly": "true",
            "returnGeometry": "false",
            "geometry": json.dumps(envelope),
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelDisjoint",
            "inSR": wkid,
        },
    )
    if "objectIds" not in response:
        raise RuntimeError(
            f"The disjoint query on {layer.url} returned no objectIds field, "
            f"so the features outside the grid envelope could not be named. "
            f"No metrics file has been written."
        )
    return sorted(int(object_id) for object_id in response["objectIds"] or [])


def grid_cells(grid):
    """The fixed grid, as a list of (cell_id, bounds) pairs.

    The grid is anchored to the envelope in config.yml and is deliberately
    NOT derived from the live data extent: the two uncorrected point records
    in DESIGN.md 4 put the points extent 4,000 km west, which would generate
    a grid spanning the Pacific. Fixed anchoring is also what makes a cell
    mean the same place between runs, which is the entire basis of the
    comparison - the grid is structural, not a threshold, and changing it
    invalidates every stored metrics file.

    A cell is identified by its south-west corner in BC Albers kilometres, so
    that '1450_600' is somewhere a maintainer can find on a map rather than
    an opaque index.
    """
    size = grid["cell_size_metres"]
    cells = []
    x = grid["xmin"]
    while x < grid["xmax"]:
        y = grid["ymin"]
        while y < grid["ymax"]:
            cells.append((
                f"{int(x) // 1000}_{int(y) // 1000}",
                {"xmin": x, "ymin": y, "xmax": x + size, "ymax": y + size},
            ))
            y += size
        x += size
    return cells


def query_spatial_bins(layer, grid, layer_key):
    """Count the features in every cell of the fixed grid.

    Geohash binning, which this check was originally designed around, does
    not exist on these services - queryBins bins on numeric and date fields
    only, and although the layers advertise supportsLod with geohash they
    carry no lodInfos and reject every lod value (DESIGN.md 7.2.1). So the
    grid is ours: 1,020 cells at the configured 50 km, one count query each,
    measured at about 2.8 minutes per layer.

    Only populated cells are returned. Roughly 30% of the grid holds anything
    at this cell size, so storing the empty 70% every day would triple the
    size of the metrics history to record that nothing is somewhere nothing
    has ever been. A cell absent from the result held no features.
    """
    cells = grid_cells(grid)
    started = time.time()

    populated = {}
    for cell_id, bounds in cells:
        count = count_in_envelope(layer, bounds, grid["wkid"])
        if count:
            populated[cell_id] = count

    elapsed = time.time() - started
    logger.info(
        "%s: spatial grid %d/%d cells populated at %d km, %.0fs (%.2fs per query)",
        layer_key, len(populated), len(cells),
        grid["cell_size_metres"] // 1000, elapsed, elapsed / len(cells),
    )
    return populated


def assert_grid_partitions_layer(layer, layer_key, spatial_bins, feature_count, grid):
    """The known-answer guard, run on every pass before anything is recorded.

    This is what makes the grid numbers trustworthy, and DESIGN.md 7.2.2
    makes it a requirement rather than a refinement. Two things have to hold.

    The cells tile the grid envelope exactly, so their counts must add back
    up to a single count taken over the whole envelope. Points land on it
    exactly. A polyline crossing a cell boundary is counted in both cells, so
    lines come in slightly over - 0.51% when measured at 50 km - which is why
    the tolerance is one-sided: coming in *under* the envelope total is
    impossible under a correct filter and always means the filter is wrong.

    And intersects plus disjoint must partition the layer, so those two have
    to come back to the live feature count. That is the check that
    independently re-derives how many features sit outside the grid entirely,
    by a completely different route from the extent query - on points it is
    the two known bad records of DESIGN.md 4, every time.

    Returns the envelope total and the number of features outside the grid,
    both recorded as metrics in their own right.
    """
    tolerance_percent = grid["max_sum_overcount_percent"]
    inside = count_in_envelope(layer, grid, grid["wkid"])
    outside = count_in_envelope(
        layer, grid, grid["wkid"], spatial_rel="esriSpatialRelDisjoint"
    )

    partition_drift = abs((inside + outside) - feature_count)
    if partition_drift > LIVE_EDIT_TOLERANCE:
        raise RuntimeError(
            f"The {layer_key} geometry filter is not returning a partition: "
            f"inside the grid {inside:,} plus outside {outside:,} is "
            f"{partition_drift:,} away from the layer total {feature_count:,}. "
            f"Too large to be a concurrent edit, so no spatial number from "
            f"this run can be trusted. No metrics file has been written."
        )

    if not inside:
        raise RuntimeError(
            f"Not one {layer_key} feature falls inside the grid envelope in "
            f"checks.spatial_grid, so the grid does not cover the data at all. "
            f"No metrics file has been written."
        )

    cell_total = sum(spatial_bins.values())
    overcount_percent = 100.0 * (cell_total - inside) / inside
    if cell_total < inside:
        raise RuntimeError(
            f"The {layer_key} cells total {cell_total:,} but the whole grid "
            f"envelope holds {inside:,}. The cells tile the envelope, so they "
            f"cannot come to less than it - the geometry filter is wrong. No "
            f"metrics file has been written."
        )
    if overcount_percent > tolerance_percent:
        raise RuntimeError(
            f"The {layer_key} cells total {cell_total:,} against {inside:,} in "
            f"the grid envelope, {overcount_percent:.2f}% over the "
            f"{tolerance_percent}% allowed by "
            f"checks.spatial_grid.max_sum_overcount_percent. That is too much "
            f"to be features crossing a cell boundary. No metrics file has "
            f"been written."
        )

    logger.info(
        "%s: grid sum guard passed - cells %s against %s in the envelope "
        "(%+.2f%%), %s outside the grid",
        layer_key, f"{cell_total:,}", f"{inside:,}", overcount_percent, f"{outside:,}",
    )
    return inside, outside


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def checked_outlier_objectids(layer_key, object_ids, outside):
    """The identifiers to record for the features outside the grid envelope.

    A known-answer guard, and it is here rather than inside the query above
    because it is arithmetic and not transport - which also makes it testable
    against hand-written values, where anything issuing a query is not.

    The identifiers and the count come from two different queries: this list,
    and inside-plus-outside against the layer total. Two measurements of the
    same thing that disagree mean one of them is wrong, and a plausible wrong
    number is the failure this project has hit three times (DESIGN.md 12), so
    the run stops rather than naming records that may not be the offending
    ones. The tolerance is the usual one - these layers are edited while the
    check is running and the two queries are moments apart.

    Only as many as an alert will name are kept. The count is the metric; this
    is the part a person acts on, and a metrics file is not the place to
    accumulate an unbounded list.
    """
    if abs(len(object_ids) - outside) > LIVE_EDIT_TOLERANCE:
        raise RuntimeError(
            f"The {layer_key} disjoint query counted {outside:,} features "
            f"outside the grid envelope but named {len(object_ids):,} of them. "
            f"Too large a gap to be editing during the run, so one of the two "
            f"queries is wrong. No metrics file has been written."
        )
    return object_ids[:MAX_OBJECTIDS_NAMED]


def collect_layer_metrics(gis, layer_key, layer_config, config):
    """Every measurement for one layer, all from live queries.

    Ordered so that the cheap scalar metrics are in hand before the 1,020
    grid queries, which are most of the run time. The grid guard raises
    rather than returning, so a run whose spatial numbers cannot be trusted
    never reaches the point of writing a metrics file.
    """
    layer = open_layer(gis, layer_config)
    grid = config["checks"]["spatial_grid"]
    logger.info("%s: collecting metrics", layer_key)

    feature_count = query_feature_count(layer)
    metrics = {
        "item_id": layer_config["item_id"],
        "layer_id": layer_config["layer_index"],
        "name": layer_config["name"],
        "feature_count": feature_count,
    }

    # Everything below assumes there is something to measure. An empty layer
    # is a data emergency rather than a set of interesting statistics, and
    # every rate and percentage here would divide by zero.
    if feature_count == 0:
        logger.error("%s: the layer reports zero features", layer_key)
        metrics["schema_fingerprint"] = schema_fingerprint(layer.properties)
        return metrics

    metrics["extent"] = query_extent(layer)
    metrics["schema_fingerprint"] = schema_fingerprint(layer.properties)

    # A layer-wide "something changed" signal with no attribution or volume,
    # because editor tracking is off on both services. Phase 2 uses it as the
    # time-of-check / time-of-use guard described in DESIGN.md 13.
    last_edit = layer.properties.get("editingInfo", {}).get("lastEditDate")
    metrics["last_edit_utc"] = utc_stamp(
        datetime.datetime.fromtimestamp(last_edit / 1000, datetime.timezone.utc)
    ) if last_edit else None

    if layer_config["has_length_field"]:
        metrics["total_length"] = query_total_length(layer, "Shape__Length")

    metrics["null_counts"] = {
        field_name: query_null_count(layer, field_name)
        for field_name in layer_config["null_check_fields"]
    }
    metrics["null_rates_percent"] = {
        field_name: round(100.0 * count / feature_count, 4)
        for field_name, count in metrics["null_counts"].items()
    }

    # Measured every run and ruled on by nothing. DESIGN.md 7.2 proposed a
    # rule here to catch a domain violation, and measurement on 2026-08-14
    # showed there is no usable rule to write. Lines carries 29 distinct
    # FEATURE_CODE values against a 15-value domain and points 35 against 10,
    # but the excess is 263 and 192 features - 0.18% and 0.36% of each layer -
    # spread across codes that are overwhelmingly carried by a handful of
    # records each, several by exactly one: 'EA0610200' and 'EA6100200' are
    # single mistyped records of 'EA06100200'. That is years of accumulated
    # slips, not an event. A rule on membership of the domain fails every run
    # forever; a rule on a value appearing since the last check fires on the
    # next typo, at a severity that pauses pruning and blocks promotion.
    # Neither is worth having, so the numbers are recorded for the baseline
    # period to reason about and nothing alerts on them.
    #
    # The uncovered risk this leaves is in DESIGN.md 7.6: a bulk edit that
    # rewrites the works type is invisible to every check here, whether it
    # moves features onto a new code or between two existing ones. The count,
    # extent, grid, schema, null rate and total length are all unchanged by
    # it. value_counts is stored per run, so a comparison could be added later
    # without re-collecting anything.
    distinct_field = layer_config["distinct_value_field"]
    metrics["distinct_value_field"] = distinct_field
    metrics["value_counts"] = query_value_counts(layer, distinct_field, feature_count)

    domain_codes = (
        metrics["schema_fingerprint"]["domains"].get(distinct_field, {}).get("coded_values", [])
    )
    metrics["domain_coded_values"] = domain_codes
    metrics["values_outside_domain"] = sorted(
        value for value in metrics["value_counts"]
        if value not in domain_codes and value != NULL_VALUE_LABEL
    )

    metrics["spatial_bins"] = query_spatial_bins(layer, grid, layer_key)
    inside, outside = assert_grid_partitions_layer(
        layer, layer_key, metrics["spatial_bins"], feature_count, grid
    )
    metrics["spatial_bins_populated"] = len(metrics["spatial_bins"])
    metrics["features_inside_grid"] = inside
    metrics["features_outside_grid"] = outside

    # Which ones. Queried only when there are any, because the answer is
    # normally none on lines and the two records of DESIGN.md 4 on points, and
    # a query that returns nothing is not worth issuing 364 days a year.
    metrics["objectids_outside_grid"] = []
    if outside:
        metrics["objectids_outside_grid"] = checked_outlier_objectids(
            layer_key,
            query_objectids_outside_envelope(layer, grid, grid["wkid"]),
            outside,
        )
        logger.warning(
            "%s: %s feature(s) fall outside the grid envelope entirely - "
            "OBJECTID %s", layer_key, f"{outside:,}",
            ", ".join(str(object_id) for object_id in metrics["objectids_outside_grid"]),
        )

    logger.info(
        "%s: %s features, %d populated cells, %d value(s) outside the domain",
        layer_key, f"{feature_count:,}", metrics["spatial_bins_populated"],
        len(metrics["values_outside_domain"]),
    )
    return metrics


def config_fingerprint(config):
    """A short hash of the whole configuration.

    Recorded alongside the measurements so that a threshold or grid change is
    distinguishable from a change in the data (DESIGN.md 7.7). default=str
    because PyYAML parses the suppression dates into date objects, which json
    will not serialise on its own.
    """
    canonical = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Metrics history
# ---------------------------------------------------------------------------


def metrics_key(config, date_stamp):
    """The key one day's metrics live at.

    metrics/<YYYY-MM-DD>.json and nothing else. backup.prune_metrics reads
    the date straight back out of this name to decide what is past its
    retention horizon, and skips anything it cannot parse - so a file named
    any other way is never pruned and never found again.
    """
    return f"{config['storage']['paths']['metrics']}{date_stamp}.json"


def load_history(store, config, before_stamp):
    """Earlier metrics files, newest first.

    Reads only the trend window - thirty files at the configured setting -
    rather than the whole retention horizon, which is 400 days. That bounds a
    check run to a few dozen small reads, and the window is by definition
    everything the comparisons in DESIGN.md 7.3 can use.

    Anything earlier than today only. A check re-run on the same day must not
    compare against its own earlier result, which would report no change and
    pass whatever the data was doing.
    """
    metrics_path = config["storage"]["paths"]["metrics"]
    window = config["checks"]["trend_window_days"]

    dated = []
    for key in storage.list_keys(store, metrics_path):
        stamp = key[len(metrics_path):].removesuffix(".json")
        try:
            datetime.date.fromisoformat(stamp)
        except ValueError:
            logger.warning("Ignoring metrics object with an unreadable name: %s", key)
            continue
        # ISO dates compare correctly as text.
        if stamp < before_stamp:
            dated.append((stamp, key))

    history = []
    for stamp, key in sorted(dated, reverse=True)[:window]:
        history.append(json.loads(storage.read_bytes(store, key)))
    return history


def previous_run(history):
    """The most recent run worth comparing against, or None.

    Skips a run recorded as DATA_FAIL. Comparing today against yesterday's
    anomaly is how a mass deletion becomes the new normal overnight: the
    count is stable against the broken baseline, the rule stops firing, and
    the alert that mattered is the only one anybody ever sees.
    """
    for entry in history:
        if entry.get("status") in COMPARABLE_STATUSES:
            return entry
    return None


def trend_medians(history, layer_key):
    """Median feature count and total length over the trend window.

    The daily comparison alone misses a slow drift that stays under the daily
    threshold every single day and adds up to something large over a month.
    Measured against a median rather than a mean so that one bad day in the
    window does not drag the reference with it.
    """
    counts = []
    lengths = []
    for entry in history:
        if entry.get("status") not in COMPARABLE_STATUSES:
            continue
        layer_metrics = entry.get("layers", {}).get(layer_key, {})
        if isinstance(layer_metrics.get("feature_count"), (int, float)):
            counts.append(layer_metrics["feature_count"])
        if isinstance(layer_metrics.get("total_length"), (int, float)):
            lengths.append(layer_metrics["total_length"])

    if not counts:
        return None
    medians = {"feature_count": statistics.median(counts), "sample_size": len(counts)}
    if lengths:
        medians["total_length"] = statistics.median(lengths)
    return medians


def monthly_anchor(store, config):
    """The metrics of the set promoted to the newest monthly tier, or None.

    The monthly prefix is named by month while the metrics file is named by
    day, so the two are joined through the manifest backup.py copied into the
    tier when it promoted the set - it carries the date stamp of the rotating
    set it came from.

    Returns the metrics and the key they were read from, so the run can
    record what it compared against.
    """
    monthly_path = config["storage"]["paths"]["monthly"]
    months = sorted({
        key[len(monthly_path):].split("/", 1)[0]
        for key in storage.list_keys(store, monthly_path)
        if "/" in key[len(monthly_path):]
    })
    if not months:
        return None, None

    manifest_key = f"{monthly_path}{months[-1]}/manifest.json"
    if not storage.key_exists(store, manifest_key):
        logger.warning("monthly/%s has no manifest, so it cannot anchor a comparison",
                       months[-1])
        return None, None

    anchor_date = json.loads(storage.read_bytes(store, manifest_key)).get("date_stamp")
    if not anchor_date:
        return None, None

    key = metrics_key(config, anchor_date)
    if not storage.key_exists(store, key):
        # The metrics horizon is 400 days and the monthly tier keeps 12
        # months, so an anchor can legitimately outlive its metrics file.
        logger.warning("No metrics file for the monthly anchor %s", anchor_date)
        return None, None
    return json.loads(storage.read_bytes(store, key)), key


# ---------------------------------------------------------------------------
# Rules
#
# Pure functions from here down: dicts in, Violations out, no queries and no
# storage. This is what tests/test_checks.py exercises with hand-written
# fixtures, and DESIGN.md 12 treats those tests as mandatory - during design a
# geometry query returned 52,986 of 53,986 features, a plausible-looking
# number that was entirely wrong.
# ---------------------------------------------------------------------------


def threshold_exceeded(rule, percent_change, absolute_change):
    """Apply the percent/absolute combination semantics from config.yml.

    fail_when 'both' means percent AND absolute must be exceeded; 'any' means
    either is enough. Only the keys actually present are tested, so a rule
    configured with a percent and no absolute behaves the same under both.
    A rule with neither configured can never fire.
    """
    tests = []
    if "percent" in rule:
        tests.append(percent_change > rule["percent"])
    if "absolute" in rule:
        tests.append(absolute_change > rule["absolute"])
    if not tests:
        return False
    return all(tests) if rule.get("fail_when") == "both" else any(tests)


def check_zero_features(layer_key, current, thresholds):
    """An empty layer is a failure whatever the thresholds say.

    DESIGN.md 7.5 makes this unconditional, and it is the one rule that needs
    no history: there is no percentage of a previous count that makes zero
    features acceptable.
    """
    if current.get("feature_count", 0) > 0:
        return []
    return [Violation(
        layer_key, "zero_features", thresholds["zero_features"]["severity"], "absolute",
        f"The {layer_key} layer returned no features at all. Do not restore "
        f"anything on the strength of this alert - look at the layer in ArcGIS "
        f"Online first, because a query fault and a mass deletion look "
        f"identical from here.",
    )]


def check_feature_count(layer_key, current, reference, thresholds, comparison):
    """Feature count against one reference value.

    Applied three times per layer - previous day, 30-day median and monthly
    anchor - with the same thresholds each time, which is what makes the
    trend comparison catch a drift that stays under the daily threshold every
    day. The comparison label travels into the alert so the reader knows
    which of the three fired.
    """
    previous_count = reference.get("feature_count")
    current_count = current.get("feature_count")
    if not previous_count or current_count is None:
        return []

    rules = thresholds["feature_count"]
    change = current_count - previous_count
    percent = abs(100.0 * change / previous_count)

    direction = "decrease" if change < 0 else "increase"
    if change == 0 or direction not in rules:
        return []

    rule = rules[direction]
    if not threshold_exceeded(rule, percent, abs(change)):
        return []

    moved = "fell" if change < 0 else "rose"
    return [Violation(
        layer_key, "feature_count", rule["severity"], comparison,
        f"{layer_key.capitalize()} feature count {moved} {percent:.1f}% "
        f"({previous_count:,} to {current_count:,}, a change of {change:+,}) "
        f"against the {comparison}.",
    )]


def check_total_length(layer_key, current, reference, thresholds, comparison):
    """Total line length, which catches a geometry change the count misses.

    Lines only. Points carry no Shape__Length, so config.yml gives that layer
    no total_length threshold at all and this returns nothing.
    """
    rule = thresholds.get("total_length")
    previous_length = reference.get("total_length")
    current_length = current.get("total_length")
    if not rule or not previous_length or current_length is None:
        return []

    percent = abs(100.0 * (current_length - previous_length) / previous_length)
    if percent <= rule["change_percent"]:
        return []

    return [Violation(
        layer_key, "total_length", rule["severity"], comparison,
        f"Total {layer_key} length changed {percent:.1f}% "
        f"({previous_length / 1000:,.0f} km to {current_length / 1000:,.0f} km) "
        f"against the {comparison}, while the feature count did not move "
        f"enough to explain it.",
    )]


def extent_corner_drift(current_extent, previous_extent):
    """The largest displacement of any of the four bounding box corners, in metres.

    Fixed by DESIGN.md 7.2.2 as the definition of extent drift, chosen over
    area or diagonal change because it detects a translated extent and an
    expanded one equally, and because "the extent moved 4,200 m" is something
    a maintainer can reason about immediately.
    """
    corners = [("xmin", "ymin"), ("xmin", "ymax"), ("xmax", "ymin"), ("xmax", "ymax")]
    return max(
        math.hypot(
            current_extent[x] - previous_extent[x],
            current_extent[y] - previous_extent[y],
        )
        for x, y in corners
    )


def check_extent(layer_key, current, reference, thresholds, comparison):
    """Extent drift against the comparison run.

    A weak signal on points until the two records in DESIGN.md 4 are
    corrected: they hold that layer's extent 4,000 km west on their own, so
    the only thing this rule can currently detect there is those two records
    being edited. The grid is doing that layer's spatial detection unaided.
    """
    rule = thresholds.get("extent")
    current_extent = current.get("extent")
    previous_extent = reference.get("extent")
    if not rule or not current_extent or not previous_extent:
        return []

    drift = extent_corner_drift(current_extent, previous_extent)
    if drift <= rule["drift_metres"]:
        return []

    return [Violation(
        layer_key, "extent", rule["severity"], comparison,
        f"The {layer_key} extent moved {drift:,.0f} m at its furthest corner "
        f"against the {comparison}, beyond the {rule['drift_metres']:,} m "
        f"allowed. Something has been placed well outside where it was.",
    )]


def check_spatial_bins(layer_key, current, reference, thresholds):
    """The fixed-grid comparison - the check that catches a localized change
    the feature count and the extent both miss.

    Two rules, and both apply only to cells that held at least min_features
    in the comparison run. That floor is mandatory rather than a preference
    (DESIGN.md 7.2.2): at 50 km the lower quartile of populated cells holds
    seven features and the minimum holds one, so without it every legitimate
    deletion of a lone feature in a sparse cell raises a FAIL. On roughly 300
    populated cells that is a daily occurrence, and a check that cries wolf
    from the first week is filtered into a folder by the second.

    A cell missing from either run held nothing, which is why an absent cell
    reads as zero rather than as missing data.
    """
    rules = thresholds.get("spatial_bins")
    current_bins = current.get("spatial_bins")
    previous_bins = reference.get("spatial_bins")
    if not rules or current_bins is None or not previous_bins:
        return []

    violations = []

    disappeared_rule = rules.get("bin_disappeared")
    if disappeared_rule:
        floor = disappeared_rule["min_features"]
        emptied = sorted(
            cell_id for cell_id, count in previous_bins.items()
            if count >= floor and current_bins.get(cell_id, 0) == 0
        )
        if emptied:
            violations.append(Violation(
                layer_key, "bin_disappeared", disappeared_rule["severity"], "daily",
                f"{len(emptied)} map cell(s) that held at least {floor} "
                f"{layer_key} features now hold none: {describe_cells(emptied)}. "
                f"Cell names are the south-west corner in BC Albers kilometres.",
            ))

    change_rule = rules.get("bin_count_change")
    if change_rule:
        floor = change_rule["min_features"]
        moved = []
        for cell_id, previous_count in previous_bins.items():
            if previous_count < floor:
                continue
            current_count = current_bins.get(cell_id, 0)
            # A cell emptying completely is the rule above; reporting it twice
            # turns one incident into two alerts.
            if current_count == 0:
                continue
            percent = abs(100.0 * (current_count - previous_count) / previous_count)
            if percent > change_rule["percent"]:
                moved.append((cell_id, previous_count, current_count, percent))
        if moved:
            moved.sort(key=lambda entry: -entry[3])
            named = ", ".join(
                f"{cell_id} ({previous_count:,} to {current_count:,})"
                for cell_id, previous_count, current_count, _ in moved[:MAX_CELLS_NAMED]
            )
            extra = f" and {len(moved) - MAX_CELLS_NAMED} more" if len(moved) > MAX_CELLS_NAMED else ""
            violations.append(Violation(
                layer_key, "bin_count_change", change_rule["severity"], "daily",
                f"{len(moved)} map cell(s) changed by more than "
                f"{change_rule['percent']}%: {named}{extra}.",
            ))

    return violations


def describe_cells(cell_ids):
    """Name a handful of cells and count the rest."""
    named = ", ".join(cell_ids[:MAX_CELLS_NAMED])
    if len(cell_ids) <= MAX_CELLS_NAMED:
        return named
    return f"{named} and {len(cell_ids) - MAX_CELLS_NAMED} more"


def check_schema(layer_key, current, reference, thresholds):
    """Field names and types, domain coded values and subtypes, exact match.

    The one threshold in config.yml that is not a placeholder. Any schema
    change should be looked at by a person: a renamed or retyped field breaks
    the staging script and changes what an FGDB artifact contains, and a new
    coded value means a new works type that nobody told us about.
    """
    rule = thresholds.get("schema")
    current_fingerprint = current.get("schema_fingerprint")
    previous_fingerprint = reference.get("schema_fingerprint")
    if not rule or not current_fingerprint or not previous_fingerprint:
        return []
    if rule.get("rule") != "exact_match" or current_fingerprint == previous_fingerprint:
        return []

    changes = []
    current_fields = {f["name"]: f["type"] for f in current_fingerprint.get("fields", [])}
    previous_fields = {f["name"]: f["type"] for f in previous_fingerprint.get("fields", [])}
    for name in sorted(set(previous_fields) - set(current_fields)):
        changes.append(f"field '{name}' removed")
    for name in sorted(set(current_fields) - set(previous_fields)):
        changes.append(f"field '{name}' added")
    for name in sorted(set(current_fields) & set(previous_fields)):
        if current_fields[name] != previous_fields[name]:
            changes.append(
                f"field '{name}' changed type from {previous_fields[name]} "
                f"to {current_fields[name]}"
            )

    if current_fingerprint.get("domains") != previous_fingerprint.get("domains"):
        changes.append("the coded-value domain changed")
    if current_fingerprint.get("subtypes") != previous_fingerprint.get("subtypes"):
        changes.append("the subtype list changed")

    return [Violation(
        layer_key, "schema", rule["severity"], "daily",
        f"The {layer_key} schema changed: {'; '.join(changes) or 'see the metrics file'}.",
    )]


def check_null_rate(layer_key, current, reference, thresholds):
    """Null rate per field, in percentage points.

    Points rather than a relative change on purpose: a field going from 0.1%
    null to 5.1% null is a failed field calculation worth waking up for,
    while the same move expressed relatively is a 5,000% increase that tells
    the reader nothing about how much data is affected.
    """
    rule = thresholds.get("null_rate")
    current_rates = current.get("null_rates_percent")
    previous_rates = reference.get("null_rates_percent")
    if not rule or not current_rates or not previous_rates:
        return []

    violations = []
    for field_name, current_rate in sorted(current_rates.items()):
        previous_rate = previous_rates.get(field_name)
        if previous_rate is None:
            continue
        increase = current_rate - previous_rate
        if increase > rule["increase_percent"]:
            violations.append(Violation(
                layer_key, "null_rate", rule["severity"], "daily",
                f"The share of {layer_key} features with no {field_name} rose "
                f"from {previous_rate:.2f}% to {current_rate:.2f}%, an increase "
                f"of {increase:.2f} percentage points.",
            ))
    return violations


# There is deliberately no rule on the works-type field. See the note in
# collect_layer_metrics: the distinct values and their counts are measured
# every run, and measurement showed there is no rule worth writing on them
# yet. DESIGN.md 7.6 carries the risk that leaves uncovered.


def evaluate_layer(layer_key, current, previous, trend, anchor, thresholds):
    """Every rule for one layer, against every comparison available.

    Rules needing no history run first and always. The rest run only against
    whichever of the three comparisons could be loaded, so a first run
    produces no comparison violations at all rather than inventing a
    reference to measure against.
    """
    violations = check_zero_features(layer_key, current, thresholds)
    if violations:
        # Nothing else is meaningful about an empty layer, and every rate
        # below would be measured against zero.
        return violations

    if previous:
        violations.extend(check_feature_count(
            layer_key, current, previous, thresholds, "previous check"))
        violations.extend(check_total_length(
            layer_key, current, previous, thresholds, "previous check"))
        violations.extend(check_extent(
            layer_key, current, previous, thresholds, "previous check"))
        violations.extend(check_spatial_bins(layer_key, current, previous, thresholds))
        violations.extend(check_schema(layer_key, current, previous, thresholds))
        violations.extend(check_null_rate(layer_key, current, previous, thresholds))

    if trend:
        violations.extend(check_feature_count(
            layer_key, current, trend, thresholds,
            f"{trend['sample_size']}-run median"))
        violations.extend(check_total_length(
            layer_key, current, trend, thresholds,
            f"{trend['sample_size']}-run median"))

    if anchor:
        violations.extend(check_feature_count(
            layer_key, current, anchor, thresholds, "monthly anchor"))
        violations.extend(check_total_length(
            layer_key, current, anchor, thresholds, "monthly anchor"))

    return violations


# ---------------------------------------------------------------------------
# Validity findings
#
# NOT RULES, AND DELIBERATELY NOT VIOLATIONS.
#
# Every rule above asks "has this moved?". A finding asks "is this valid?" -
# a question with an answer on the first run, before there is anything to
# compare against, and with the same answer on the four hundredth. DESIGN.md
# 7.6.1 records how that gap was found: the two point records sitting 4,000
# km outside BC (DESIGN.md 4) are measured on every single run and were
# reported by nothing, because they predate every measurement and so drift
# is zero.
#
# A finding is a plain string and never a Violation, and that is the whole
# design rather than a shortcut. Violations carry a severity, severities
# reach status_from, and any permanently non-PASS status permanently blocks
# monthly promotion (backup.promote_monthly). Two known records nobody
# disputes would then cost every monthly restore point for as long as they
# went uncorrected, which is a worse outcome than the silence being fixed
# here. So a finding travels in the run's details with its own routing row in
# config.yml, exactly as no_monthly_candidate does, and reaches the people
# who would act on it without touching the status.
#
# If a severity is ever added to one of these, monthly promotion stops. There
# is a test for that in tests/test_checks.py, and it is the most important one
# in the set.
# ---------------------------------------------------------------------------


def outside_bc_finding(layer_key, current):
    """The features that cannot be in British Columbia, as one line, or None.

    The bound is the grid envelope in checks.spatial_grid and is deliberately
    not a second envelope of its own. The count being reported here is the
    disjoint half of the grid partition, so it arrives already carrying its
    own arithmetic proof - inside plus outside equals the live feature count,
    asserted on every pass - where a separate validity envelope would be a
    fresh unguarded number of exactly the shape this project has been bitten
    by three times. The grid envelope is also the one value in config.yml
    that already carries a written warning against tuning it, which is the
    right property for a validity bound to have. Reasoning at DESIGN.md 7.6.1.

    It is generous by 25 to 75 km on every side of the province, so a feature
    outside it is not near a border and is not arguable.

    Both layers. Nothing had ever looked at lines, which is its own reason to.
    """
    outside = current.get("features_outside_grid")
    if not outside:
        return None

    object_ids = current.get("objectids_outside_grid") or []
    named = ", ".join(str(object_id) for object_id in object_ids)
    if not named:
        which = "this run could not name them"
    elif outside > len(object_ids):
        which = f"OBJECTID {named} - the first {len(object_ids)} of {outside:,}"
    else:
        which = f"OBJECTID {named}"

    # Written as the sentence a reader would say out loud, because this line
    # is read by the data owner in an email and not only by us in a log.
    subject = "feature is" if outside == 1 else "features are"

    # The layer and the count lead the line, and jenkins/notify.py reads both
    # back out of it to decide whether this is the same situation it has
    # already mailed about. That parse is a contract between the two files and
    # tests/test_notify.py builds this line from here to prove it holds.
    return (
        f"{layer_key}: {outside:,} {subject} outside British Columbia ({which})."
    )


# ---------------------------------------------------------------------------
# Suppressions and status
# ---------------------------------------------------------------------------


def suppression_date(entry, key):
    """One end of a suppression window, as a date.

    PyYAML parses an unquoted 2026-09-14 into a date and a quoted one into a
    string, and a config edited by hand will contain both over time.
    """
    value = entry.get(key)
    if value is None:
        raise ValueError(
            f"The suppression {entry!r} in config.yml has no '{key}' date. Both "
            f"'from' and 'until' are required so that a suppression cannot be "
            f"open-ended in either direction."
        )
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(
            f"The suppression '{key}' value {value!r} in config.yml is not a "
            f"date. Write it as 2026-09-14."
        )


def apply_suppressions(violations, config, date_stamp):
    """Set aside violations the data owner has approved in advance.

    A suppression covers one rule on one layer between two dates, both
    required. Every suppressed violation is returned separately and logged
    rather than dropped, because a suppression that quietly hides a real
    failure is worse than no check - the reader of a status object has to be
    able to see what was not counted.
    """
    today = datetime.date.fromisoformat(date_stamp)
    windows = []
    for entry in config.get("suppressions") or []:
        if "rule" not in entry or "layer" not in entry:
            raise ValueError(
                f"The suppression {entry!r} in config.yml needs both a 'rule' "
                f"and a 'layer'."
            )
        windows.append((
            entry["rule"], entry["layer"],
            suppression_date(entry, "from"), suppression_date(entry, "until"),
            entry.get("reason", "no reason recorded"),
        ))

    counted = []
    suppressed = []
    for violation in violations:
        match = next(
            (window for window in windows
             if window[0] == violation.rule
             and window[1] == violation.layer
             and window[2] <= today <= window[3]),
            None,
        )
        if match:
            logger.warning(
                "Suppressed %s on %s until %s (%s): %s",
                violation.rule, violation.layer, match[3], match[4], violation.message,
            )
            suppressed.append(violation)
        else:
            counted.append(violation)
    return counted, suppressed


def status_from(violations, has_comparison):
    """The single status the whole run reports.

    BASELINE is the first-run case and sits below the two failure levels on
    purpose: a run with nothing to compare against can still find a layer
    empty, and reporting that as "first run, nothing to see" would be the
    worst possible answer.
    """
    if any(violation.severity == "FAIL" for violation in violations):
        return "DATA_FAIL"
    if any(violation.severity == "WARN" for violation in violations):
        return "WARN"
    if not has_comparison:
        return "BASELINE"
    return "PASS"


# What separates a run's verdict from a validity finding in the summary
# line. Short and plain on purpose: "Separately, and needing no comparison to
# be true" was accurate and nobody could read it.
FINDING_LEAD_IN = " Also flagged: "


def summarise(status, date_stamp, metrics, violations, findings=()):
    """One line for a non-technical reader, which becomes the email body.

    A validity finding is appended whatever the status is, PASS and BASELINE
    included, and does not change it. It is the one thing reported here that
    needs no earlier run to be true, so leaving it out of a summary that says
    "no verdict is possible until the next run" would restate the silence
    DESIGN.md 7.6.1 was written about.
    """
    counts = " and ".join(
        f"{layer_metrics.get('feature_count', 0):,} {layer_key}"
        for layer_key, layer_metrics in metrics.items()
    )

    if status == "BASELINE":
        text = (
            f"First integrity check, {date_stamp}. The layers hold {counts}, "
            f"recorded as the starting point for future comparisons. There is "
            f"nothing earlier to compare against yet, so no verdict on the data "
            f"is possible until the next run."
        )
    elif status == "PASS":
        # "found no unexpected change" rather than "passed", because passed is
        # what a PASS is not. The status answers "has this moved?" and nothing
        # else, so a summary claiming the check passed sits badly next to a
        # validity finding appended below - and worse, it overclaims even when
        # there is no finding. DESIGN.md 7.6.1.
        text = (
            f"The integrity check found no unexpected change for {date_stamp}. "
            f"The layers hold {counts}, in line with recent runs."
        )
    else:
        ranked = [v for v in violations if v.severity == "FAIL"] or violations
        headline = ranked[0].message
        others = len(violations) - 1
        tail = f" ({others} other issue(s) were found - see the details.)" if others else ""
        if status == "DATA_FAIL":
            text = f"{headline}{tail} The backups are untouched and no data has been changed."
        else:
            text = f"{headline}{tail}"

    if findings:
        # Worded to sit correctly after either sentence above. BASELINE says
        # no verdict is possible until there is something to compare against,
        # and PASS says nothing changed; a validity finding is the exception
        # to both, and has to read as one rather than as a contradiction.
        #
        # FINDING_LEAD_IN is also where jenkins/notify.py cuts the summary,
        # because an email gives the finding a section of its own and would
        # otherwise print it twice. Reword it in both files or in neither -
        # tests/test_notify.py builds a real summary from here and asserts
        # that the split still works.
        text += FINDING_LEAD_IN + " ".join(findings)
    return text


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def write_metrics(store, config, date_stamp, payload):
    """Write one day's metrics file.

    The name is the contract backup.prune_metrics reads, so it is built by
    metrics_key and never assembled here.

    Sorted and indented so that a person can read the file and a diff between
    two days is legible.
    """
    key = metrics_key(config, date_stamp)
    storage.write_bytes(
        store, key, json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    )
    return key


def run_checks(config):
    """Run one set of integrity checks and return a CheckResult.

    Importable and callable from outside this repository: Phase 2 calls it
    from the staging script on the NRIDS server, immediately before the
    staging geodatabase is deleted, so that raising there leaves the previous
    staging data intact. Nothing here assumes GitHub Actions.

    The caller decides what to do with the result. Phase 1 alerts. Phase 2
    alerts, and aborts on DATA_FAIL only - a transient API error must not
    halt the nightly pipeline for a reason unrelated to data quality. Fail
    closed on data, fail open on system.
    """
    started = utc_now()
    code_version = resolve_code_version()
    date_stamp = local_date_stamp(config)
    details = []
    logger.info("Check run for %s, code version %s", date_stamp, code_version)

    try:
        store = storage.connect_to_storage(config)
        gis = connect_to_agol(config)
    except Exception as exc:
        reason = safe_reason(exc)
        logger.error("Checks could not start: %s", reason)
        return CheckResult(
            "SYSTEM_FAIL",
            "The daily integrity check could not start because it was unable to "
            "sign in to ArcGIS Online or reach object storage. The data has not "
            "been checked today, and the backups are untouched.",
            [], [reason], {}, date_stamp, code_version,
        )

    # Every measurement first. The grid guards raise rather than returning, so
    # a run whose numbers cannot be trusted stops here without writing
    # anything - DESIGN.md 7.2.2 requires the sum guard to run before
    # anything is recorded.
    try:
        measurements = {
            layer_key: collect_layer_metrics(gis, layer_key, layer_config, config)
            for layer_key, layer_config in config["layers"].items()
        }
    except Exception as exc:
        reason = safe_reason(exc)
        logger.error("Checks failed while collecting metrics: %s", reason)
        return CheckResult(
            "SYSTEM_FAIL",
            "The daily integrity check could not collect a complete and "
            "trustworthy set of measurements, so it has recorded nothing rather "
            "than record numbers that may be wrong. The data has not been "
            "checked today and the backups are untouched.",
            [], [reason], {}, date_stamp, code_version,
        )

    try:
        history = load_history(store, config, date_stamp)
        previous = previous_run(history)
        anchor, anchor_key = (
            monthly_anchor(store, config) if config["checks"]["use_monthly_anchor"]
            else (None, None)
        )
    except Exception as exc:
        reason = safe_reason(exc)
        logger.error("Checks could not read the metrics history: %s", reason)
        return CheckResult(
            "SYSTEM_FAIL",
            "The daily integrity check collected today's measurements but could "
            "not read the earlier ones to compare them against, so no verdict "
            "was reached. The backups are untouched.",
            [], [reason], {}, date_stamp, code_version,
        )

    previous_stamp = previous.get("date_stamp") if previous else None
    if previous_stamp:
        gap_days = (
            datetime.date.fromisoformat(date_stamp)
            - datetime.date.fromisoformat(previous_stamp)
        ).days
        details.append(f"compared against {previous_stamp}")
        if gap_days > 1:
            # DESIGN.md 7.5: compare against the most recent available and say
            # so, rather than skipping the comparison.
            details.append(
                f"a gap of {gap_days} days since the last comparable check - "
                f"a change of this size may have accumulated over several days"
            )
    else:
        details.append("no earlier metrics to compare against, so this run is the baseline")

    violations = []
    for layer_key, current in measurements.items():
        trend = trend_medians(history, layer_key)
        anchor_metrics = (anchor or {}).get("layers", {}).get(layer_key)
        violations.extend(evaluate_layer(
            layer_key,
            current,
            (previous or {}).get("layers", {}).get(layer_key),
            trend,
            anchor_metrics,
            config["thresholds"][layer_key],
        ))
        if trend:
            details.append(
                f"{layer_key}: median of {trend['sample_size']} run(s) is "
                f"{trend['feature_count']:,.0f} features"
            )

    try:
        violations, suppressed = apply_suppressions(violations, config, date_stamp)
    except Exception as exc:
        reason = safe_reason(exc)
        logger.error("The suppressions block in config.yml is not usable: %s", reason)
        return CheckResult(
            "SYSTEM_FAIL",
            "The daily integrity check stopped because the suppressions section "
            "of its configuration could not be read. No verdict was reached and "
            "the backups are untouched.",
            [], [reason], {}, date_stamp, code_version,
        )

    # Deliberately outside the violation list and outside status_from. A
    # finding says the data is invalid, not that it changed, and it must not
    # gate monthly promotion - see the section comment above
    # outside_bc_finding.
    findings = []
    for layer_key, current in measurements.items():
        finding = outside_bc_finding(layer_key, current)
        if finding:
            logger.warning("%s", finding)
            findings.append(finding)

    status = status_from(violations, has_comparison=previous is not None)
    details.extend(violation.message for violation in violations)
    details.extend(findings)
    details.extend(f"suppressed: {violation.message}" for violation in suppressed)

    metrics = {
        "metrics_version": METRICS_VERSION,
        "date_stamp": date_stamp,
        "status": status,
        "code_version": code_version,
        "config_fingerprint": config_fingerprint(config),
        "collection_started_utc": utc_stamp(started),
        "collection_completed_utc": utc_stamp(utc_now()),
        "previous_metrics_file": metrics_key(config, previous_stamp) if previous_stamp else None,
        "monthly_anchor_file": anchor_key,
        "trend_window_runs": len(history),
        "validity_findings": findings,
        "failures": [
            {"layer": v.layer, "rule": v.rule, "severity": v.severity,
             "comparison": v.comparison, "message": v.message}
            for v in violations
        ],
        "suppressed": [
            {"layer": v.layer, "rule": v.rule, "message": v.message} for v in suppressed
        ],
        "layers": measurements,
    }

    try:
        written = write_metrics(store, config, date_stamp, metrics)
        details.append(f"wrote {written}")
    except Exception as exc:
        reason = safe_reason(exc)
        logger.error("Checks could not write the metrics file: %s", reason)
        details.append(reason)
        if status != "DATA_FAIL":
            return CheckResult(
                "SYSTEM_FAIL",
                f"The daily integrity check ran and found nothing wrong, but "
                f"could not save its measurements to storage, so tomorrow's "
                f"check has nothing to compare against.",
                [], details, metrics, date_stamp, code_version,
            )
        # A storage fault must not bury a data emergency. The verdict stands
        # and is reported; the failed write is in the details.
        logger.error("Reporting DATA_FAIL despite the storage failure above")

    logger.info("%s for %s, %d rule(s) broken", status, date_stamp, len(violations))
    return CheckResult(
        status,
        summarise(status, date_stamp, measurements, violations, findings),
        [violation.message for violation in violations],
        details,
        metrics,
        date_stamp,
        code_version,
    )
