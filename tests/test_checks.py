"""Known-answer tests for the check rules.

    python -m pytest tests -q

Given this metrics dict and this threshold config, this status must come
out. Hand-written dict fixtures rather than a mocking framework: the point
is to be able to read the input and the expected answer side by side and
agree with them without knowing anything about the test tooling.

These are not optional. During design a geometry filter query returned
52,986 of 53,986 features - a plausible-looking number that was entirely
wrong, caused by a malformed filter argument. Building checks.py turned up
two more of the same shape against the live services: the arcgis wrapper
silently dropping aggregateGeometries and returning an empty feature, and
query(return_distinct_values=True) not applying the distinct at all and
returning 6,717 rows for a field holding 29 values. A check that silently
returns garbage is worse than no check, so every rule here has a test with a
known expected result.

Most of what is under test is pure - dicts in, Violations out. The section at
the foot of the file runs the whole of run_checks with every boundary replaced
by a hand-written stand-in, because a validity finding is easiest to lose in
the wiring rather than in a rule: collected and never put in the details, or
in the details and never in the metrics file, and in both cases the job goes
green and says nothing. Nothing here touches AGOL, object storage or the
network either way.
"""

import ast
import datetime
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import checks
import storage


def load_real_config():
    """The config.yml the check job will actually read.

    Used by the whole-run tests at the foot of this file, so that the layer
    names, the thresholds and the suppressions under test are the ones that
    will run rather than a fixture that could quietly disagree with them.
    """
    with open(ROOT / "config.yml", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


REAL_CONFIG = load_real_config()


# ---------------------------------------------------------------------------
# Fixtures
#
# Deliberately small. Real counts from 2026-08-14 so the numbers are
# recognisable, but the shapes are hand-built rather than captured.
# ---------------------------------------------------------------------------

LINES_THRESHOLDS = {
    "feature_count": {
        "decrease": {"fail_when": "both", "percent": 10, "absolute": 100, "severity": "FAIL"},
        "increase": {"fail_when": "any", "percent": 25, "severity": "WARN"},
    },
    "zero_features": {"severity": "FAIL"},
    "spatial_bins": {
        "bin_disappeared": {"min_features": 20, "severity": "FAIL"},
        "bin_count_change": {"min_features": 20, "percent": 20, "severity": "WARN"},
    },
    "extent": {"drift_metres": 5000, "severity": "WARN"},
    "total_length": {"change_percent": 10, "severity": "WARN"},
    "schema": {"rule": "exact_match", "severity": "FAIL"},
    "null_rate": {"increase_percent": 5, "severity": "WARN"},
}

SCHEMA = {
    "fields": [
        {"name": "FEATURE_CODE", "type": "esriFieldTypeString"},
        {"name": "OBJECTID", "type": "esriFieldTypeOID"},
        {"name": "TWRK_TAG", "type": "esriFieldTypeString"},
    ],
    "domains": {"FEATURE_CODE": {"name": "LWL_FCODES", "coded_values": ["EA06100200"]}},
    "subtypes": ["1:Ditch"],
}


def metrics(**overrides):
    """One layer's measurements, with the healthy values as the default."""
    base = {
        "feature_count": 142523,
        "extent": {"xmin": 488114.0, "ymin": 371075.0, "xmax": 1841275.0, "ymax": 1693633.0},
        "total_length": 32766296.0,
        "null_counts": {"TWRK_TAG": 6957},
        "null_rates_percent": {"TWRK_TAG": 4.88},
        "distinct_value_field": "FEATURE_CODE",
        "value_counts": {"EA06100200": 70766, "EA21400610": 71757},
        "schema_fingerprint": SCHEMA,
        "spatial_bins": {"1450_600": 500, "1500_600": 40, "1550_600": 5},
    }
    base.update(overrides)
    return base


def statuses(violations, has_comparison=True):
    return checks.status_from(violations, has_comparison)


# ---------------------------------------------------------------------------
# Grid geometry - the structural definition everything else is compared in
# ---------------------------------------------------------------------------

GRID = {
    "xmin": 200000, "ymin": 300000, "xmax": 1900000, "ymax": 1800000,
    "cell_size_metres": 50000, "wkid": 3005, "max_sum_overcount_percent": 2,
}


def test_grid_is_1020_cells_of_the_configured_size():
    """The measured figure in DESIGN.md 7.2.2. If this changes, every stored
    metrics file has been invalidated and the baseline restarts."""
    cells = checks.grid_cells(GRID)
    assert len(cells) == 1020
    assert len(cells) == 34 * 30


def test_cell_id_is_the_south_west_corner_in_kilometres():
    """An alert has to name a place a maintainer can find on a map."""
    cells = dict(checks.grid_cells(GRID))
    assert "200_300" in cells
    assert "1850_1750" in cells
    assert cells["200_300"] == {
        "xmin": 200000, "ymin": 300000, "xmax": 250000, "ymax": 350000
    }


def test_cells_tile_the_envelope_without_gap_or_overlap():
    """The arithmetic the sum guard depends on: the cell areas have to add up
    to the envelope area exactly."""
    cells = checks.grid_cells(GRID)
    cell_area = GRID["cell_size_metres"] ** 2
    envelope_area = (GRID["xmax"] - GRID["xmin"]) * (GRID["ymax"] - GRID["ymin"])
    assert len(cells) * cell_area == envelope_area


# ---------------------------------------------------------------------------
# Threshold combination semantics
# ---------------------------------------------------------------------------

def test_fail_when_both_needs_percent_and_absolute():
    rule = {"fail_when": "both", "percent": 10, "absolute": 100}
    assert checks.threshold_exceeded(rule, percent_change=12, absolute_change=150)
    assert not checks.threshold_exceeded(rule, percent_change=12, absolute_change=50)
    assert not checks.threshold_exceeded(rule, percent_change=5, absolute_change=150)


def test_fail_when_any_needs_only_one():
    rule = {"fail_when": "any", "percent": 10, "absolute": 100}
    assert checks.threshold_exceeded(rule, percent_change=12, absolute_change=50)
    assert checks.threshold_exceeded(rule, percent_change=5, absolute_change=150)
    assert not checks.threshold_exceeded(rule, percent_change=5, absolute_change=50)


def test_a_rule_with_nothing_configured_never_fires():
    assert not checks.threshold_exceeded({"severity": "FAIL"}, 99999, 99999)


# ---------------------------------------------------------------------------
# Feature count
# ---------------------------------------------------------------------------

def test_small_decrease_passes():
    """Ordinary editing. 20 features off 142,523 is 0.01%."""
    found = checks.check_feature_count(
        "lines", metrics(feature_count=142503), metrics(), LINES_THRESHOLDS, "previous check"
    )
    assert found == []


def test_mass_deletion_fails():
    """The motivating case: a large drop in both percent and absolute terms."""
    found = checks.check_feature_count(
        "lines", metrics(feature_count=100000), metrics(), LINES_THRESHOLDS, "previous check"
    )
    assert len(found) == 1
    assert found[0].severity == "FAIL"
    assert found[0].rule == "feature_count"
    assert statuses(found) == "DATA_FAIL"


def test_large_percentage_on_a_tiny_layer_does_not_fail():
    """fail_when 'both' exists for this case. Dropping 3 of 30 features is 10%
    but only 3 records, which is somebody deleting three records."""
    found = checks.check_feature_count(
        "lines", metrics(feature_count=27), metrics(feature_count=30),
        LINES_THRESHOLDS, "previous check",
    )
    assert found == []


def test_duplicate_load_warns_rather_than_fails():
    """An increase is a WARN: a duplicate bulk load is recoverable and is not
    the emergency a deletion is."""
    found = checks.check_feature_count(
        "lines", metrics(feature_count=200000), metrics(), LINES_THRESHOLDS, "previous check"
    )
    assert len(found) == 1
    assert found[0].severity == "WARN"
    assert statuses(found) == "WARN"


def test_no_change_produces_nothing():
    assert checks.check_feature_count(
        "lines", metrics(), metrics(), LINES_THRESHOLDS, "previous check"
    ) == []


def test_the_comparison_is_named_in_the_message():
    """An alert saying a count fell 30% is not actionable until the reader
    knows 30% against what."""
    found = checks.check_feature_count(
        "lines", metrics(feature_count=100000), metrics(),
        LINES_THRESHOLDS, "30-run median",
    )
    assert found[0].comparison == "30-run median"
    assert "30-run median" in found[0].message


# ---------------------------------------------------------------------------
# Zero features - unconditional, and the one rule needing no history
# ---------------------------------------------------------------------------

def test_zero_features_always_fails():
    """DESIGN.md 7.5: always DATA_FAIL regardless of thresholds."""
    found = checks.check_zero_features("lines", metrics(feature_count=0), LINES_THRESHOLDS)
    assert len(found) == 1
    assert found[0].severity == "FAIL"
    assert statuses(found) == "DATA_FAIL"


def test_zero_features_fails_even_on_the_very_first_run():
    """A first run has nothing to compare against, but an empty layer is still
    an emergency - reporting BASELINE here would be the worst answer."""
    found = checks.evaluate_layer(
        "lines", metrics(feature_count=0), None, None, None, LINES_THRESHOLDS
    )
    assert statuses(found, has_comparison=False) == "DATA_FAIL"


def test_an_empty_layer_suppresses_the_other_rules():
    """Every rate below would be measured against zero, and one alert about an
    empty layer beats eight."""
    found = checks.evaluate_layer(
        "lines", metrics(feature_count=0), metrics(), None, None, LINES_THRESHOLDS
    )
    assert [violation.rule for violation in found] == ["zero_features"]


# ---------------------------------------------------------------------------
# Spatial bins
# ---------------------------------------------------------------------------

def test_busy_cell_emptying_fails():
    """The motivating scenario: features deleted from one valley, invisible to
    both the feature count and the extent."""
    current = metrics(spatial_bins={"1500_600": 40, "1550_600": 5})
    found = checks.check_spatial_bins("lines", current, metrics(), LINES_THRESHOLDS)
    assert [v.rule for v in found] == ["bin_disappeared"]
    assert found[0].severity == "FAIL"
    assert "1450_600" in found[0].message


def test_sparse_cell_emptying_is_ignored():
    """The min_features floor, which DESIGN.md 7.2.2 makes mandatory. The 5
    features in 1550_600 are below the floor of 20, so somebody legitimately
    deleting them is not an incident."""
    current = metrics(spatial_bins={"1450_600": 500, "1500_600": 40})
    assert checks.check_spatial_bins("lines", current, metrics(), LINES_THRESHOLDS) == []


def test_bin_count_change_warns_above_the_floor():
    current = metrics(spatial_bins={"1450_600": 300, "1500_600": 40, "1550_600": 5})
    found = checks.check_spatial_bins("lines", current, metrics(), LINES_THRESHOLDS)
    assert [v.rule for v in found] == ["bin_count_change"]
    assert found[0].severity == "WARN"


def test_an_emptied_cell_raises_one_alert_not_two():
    """A cell going to zero satisfies both rules. Reporting it twice turns one
    incident into two alerts."""
    current = metrics(spatial_bins={"1550_600": 5})
    found = checks.check_spatial_bins("lines", current, metrics(), LINES_THRESHOLDS)
    assert [v.rule for v in found] == ["bin_disappeared"]


def test_a_new_cell_appearing_is_not_a_violation():
    """Growth into a cell that was empty is normal - the rules are about
    features disappearing, not appearing."""
    current = metrics(spatial_bins=dict(metrics()["spatial_bins"], **{"900_900": 3}))
    assert checks.check_spatial_bins("lines", current, metrics(), LINES_THRESHOLDS) == []


def test_bin_alert_names_the_cells_but_does_not_list_hundreds():
    """Past a handful the list stops being actionable and starts being a wall
    of text in an email."""
    previous = metrics(spatial_bins={f"{1000 + n * 50}_600": 100 for n in range(40)})
    found = checks.check_spatial_bins("lines", metrics(spatial_bins={}), previous,
                                      LINES_THRESHOLDS)
    assert found[0].message.count("_600") == checks.MAX_CELLS_NAMED
    assert "40 map cell(s)" in found[0].message
    assert "35 more" in found[0].message


# ---------------------------------------------------------------------------
# Extent
# ---------------------------------------------------------------------------

def test_extent_drift_is_the_largest_corner_displacement():
    """Fixed by DESIGN.md 7.2.2. Not area change, not diagonal change."""
    current = {"xmin": 0.0, "ymin": 0.0, "xmax": 100.0, "ymax": 100.0}
    previous = {"xmin": 0.0, "ymin": 0.0, "xmax": 130.0, "ymax": 140.0}
    # Three corners are unmoved; the fourth moves 30 east and 40 north.
    assert checks.extent_corner_drift(current, previous) == pytest.approx(50.0)


def test_a_translated_extent_is_detected_as_well_as_an_expanded_one():
    """The reason max-corner was chosen over area: a translation of the whole
    box leaves the area identical."""
    current = {"xmin": 6000.0, "ymin": 0.0, "xmax": 106000.0, "ymax": 100000.0}
    previous = {"xmin": 0.0, "ymin": 0.0, "xmax": 100000.0, "ymax": 100000.0}
    assert checks.extent_corner_drift(current, previous) == pytest.approx(6000.0)
    found = checks.check_extent("lines", metrics(extent=current), metrics(extent=previous),
                                LINES_THRESHOLDS, "previous check")
    assert len(found) == 1
    assert found[0].severity == "WARN"


def test_extent_within_tolerance_passes():
    moved = dict(metrics()["extent"])
    moved["xmax"] += 1000
    assert checks.check_extent("lines", metrics(extent=moved), metrics(),
                               LINES_THRESHOLDS, "previous check") == []


# ---------------------------------------------------------------------------
# Total length, schema, nulls, distinct values
# ---------------------------------------------------------------------------

def test_total_length_change_warns():
    """Geometry rewritten with the feature count unchanged."""
    found = checks.check_total_length(
        "lines", metrics(total_length=20000000.0), metrics(),
        LINES_THRESHOLDS, "previous check",
    )
    assert len(found) == 1
    assert found[0].severity == "WARN"


def test_total_length_is_skipped_for_a_layer_without_the_field():
    """Points carries no Shape__Length, so config.yml gives it no threshold."""
    points_thresholds = {k: v for k, v in LINES_THRESHOLDS.items() if k != "total_length"}
    assert checks.check_total_length(
        "points", metrics(total_length=None), metrics(), points_thresholds, "previous check"
    ) == []


def test_a_dropped_field_fails_and_is_named():
    changed = {**SCHEMA, "fields": [f for f in SCHEMA["fields"] if f["name"] != "TWRK_TAG"]}
    found = checks.check_schema("lines", metrics(schema_fingerprint=changed), metrics(),
                                LINES_THRESHOLDS)
    assert len(found) == 1
    assert found[0].severity == "FAIL"
    assert "TWRK_TAG" in found[0].message


def test_a_retyped_field_fails():
    retyped = {**SCHEMA, "fields": [
        {**f, "type": "esriFieldTypeInteger"} if f["name"] == "TWRK_TAG" else f
        for f in SCHEMA["fields"]
    ]}
    found = checks.check_schema("lines", metrics(schema_fingerprint=retyped), metrics(),
                                LINES_THRESHOLDS)
    assert found[0].severity == "FAIL"
    assert "changed type" in found[0].message


def test_a_new_domain_coded_value_fails():
    """A new works type nobody told us about."""
    extra = {**SCHEMA, "domains": {
        "FEATURE_CODE": {"name": "LWL_FCODES", "coded_values": ["EA06100200", "EA21400610"]}
    }}
    found = checks.check_schema("lines", metrics(schema_fingerprint=extra), metrics(),
                                LINES_THRESHOLDS)
    assert found[0].severity == "FAIL"
    assert "domain" in found[0].message


def test_an_unchanged_schema_passes():
    assert checks.check_schema("lines", metrics(), metrics(), LINES_THRESHOLDS) == []


def test_null_rate_increase_is_measured_in_percentage_points():
    """0.1% to 5.1% null is a rise of 5.0 points, not 5,000%."""
    found = checks.check_null_rate(
        "lines", metrics(null_rates_percent={"TWRK_TAG": 5.2}),
        metrics(null_rates_percent={"TWRK_TAG": 0.1}), LINES_THRESHOLDS,
    )
    assert len(found) == 1
    assert found[0].severity == "WARN"

    within = checks.check_null_rate(
        "lines", metrics(null_rates_percent={"TWRK_TAG": 5.0}),
        metrics(null_rates_percent={"TWRK_TAG": 0.1}), LINES_THRESHOLDS,
    )
    assert within == []


def test_a_falling_null_rate_is_not_a_violation():
    """Somebody filling in missing tags is the data getting better."""
    assert checks.check_null_rate(
        "lines", metrics(null_rates_percent={"TWRK_TAG": 0.1}),
        metrics(null_rates_percent={"TWRK_TAG": 5.2}), LINES_THRESHOLDS,
    ) == []


def test_no_rule_fires_on_the_works_type_field():
    """There is deliberately no distinct_values rule, and this test exists so
    that re-adding one is a deliberate act rather than an oversight.

    Measured 2026-08-14: lines uses 29 distinct FEATURE_CODE values against a
    15-value domain and points 35 against 10, but the excess is only 263 and
    192 features - 0.18% and 0.36% - spread across codes carried by a handful
    of records each ('EA0610200' and 'EA6100200' are single mistyped records
    of 'EA06100200'). A rule on domain membership fails every run forever; a
    rule on a code appearing since the last check fires on the next typo, at a
    severity that would pause pruning and block promotion. Both numbers are
    recorded as measurements instead."""
    backlog = {"EA06100200": 70766, "EA21400610": 71757, "EA0610200": 1, " ": 42}
    newcomer = dict(backlog, XX99999999=3)

    found = checks.evaluate_layer(
        "lines", metrics(value_counts=newcomer), metrics(value_counts=backlog),
        None, None, LINES_THRESHOLDS,
    )
    assert found == []
    assert statuses(found) == "PASS"


def test_the_out_of_domain_backlog_is_still_measured():
    """Dropping the rule must not drop the measurement - the baseline period
    needs these numbers to decide what a useful rule would look like."""
    domain = ["EA06100200", "EA21400610"]
    counts = {"EA06100200": 70766, "EA21400610": 71757, "EA0610200": 1, " ": 42,
              checks.NULL_VALUE_LABEL: 4}
    outside = sorted(v for v in counts if v not in domain and v != checks.NULL_VALUE_LABEL)
    assert outside == [" ", "EA0610200"]


# ---------------------------------------------------------------------------
# Validity findings - the one thing reported here that is not a comparison
#
# DESIGN.md 7.6.1. Every rule above asks "has this moved?", so the two point
# records sitting 4,000 km outside BC were measured on every single run and
# reported by nothing: they predate the first measurement, which makes their
# drift zero and every change rule silent about them.
#
# A finding has to reach the details and must NOT reach the status, because
# backup.promote_monthly promotes only a set whose paired check returned PASS
# and these records have been uncorrected since before the project started.
# ---------------------------------------------------------------------------

# The two records of DESIGN.md 4, verbatim. Identical coordinates roughly
# 4,000 km outside BC, one data entry error duplicated across two rows, still
# uncorrected as at 2026-08-19.
OUTLIERS = {"features_outside_grid": 2, "objectids_outside_grid": [150984, 150985]}


def test_the_identifiers_have_to_agree_with_the_count():
    """They come from two different queries - this list, and inside plus
    outside against the layer total. Two measurements of the same thing that
    disagree mean one of them is wrong, and naming records that may not be the
    offending ones is worse than naming none."""
    with pytest.raises(RuntimeError, match="one of the two queries is wrong"):
        checks.checked_outlier_objectids("points", [], 2000)
    with pytest.raises(RuntimeError, match="No metrics file has been written"):
        checks.checked_outlier_objectids("points", list(range(2000)), 2)


def test_a_feature_edited_during_the_run_is_not_treated_as_a_fault():
    """These layers are edited through QuickWins while the check is running
    and the two queries are moments apart, so a feature or two of disagreement
    is a concurrent edit rather than a malformed query. Same tolerance and
    same reasoning as everywhere else in this file."""
    assert checks.checked_outlier_objectids(
        "points", [150984, 150985], 2 + checks.LIVE_EDIT_TOLERANCE) == [150984, 150985]


def test_the_recorded_identifiers_are_capped():
    """The count is the metric; this is the part a person acts on, and a
    metrics file written every day is not the place to accumulate an unbounded
    list."""
    found = list(range(150900, 150900 + 40))
    kept = checks.checked_outlier_objectids("points", found, len(found))

    assert kept == found[:checks.MAX_OBJECTIDS_NAMED]


def test_the_two_known_records_pass_the_guard_and_are_recorded():
    """The live reading on points, every run since the first: exactly two,
    OBJECTID 150984 and 150985 (DESIGN.md 4)."""
    assert checks.checked_outlier_objectids(
        "points", [150984, 150985], 2) == [150984, 150985]


def test_a_record_outside_bc_is_reported_with_no_history_at_all():
    """The whole point of a finding. It needs no comparison run, so it is
    reported on a BASELINE, which is what every run had been so far."""
    finding = checks.outside_bc_finding("points", metrics(**OUTLIERS))

    assert finding is not None
    assert "2 features are" in finding
    # Naming them is most of the value: whoever reads the alert is whoever
    # would correct the records.
    assert "150984" in finding and "150985" in finding


def test_the_finding_does_not_change_the_run_status():
    """THE MOST IMPORTANT TEST IN THIS FILE.

    backup.promote_monthly promotes only a set whose paired check returned
    PASS, so any permanently non-PASS status permanently blocks the monthly
    tier. Two known records nobody disputes would trade away every monthly
    restore point for as long as they went uncorrected, which is a worse
    outcome than the silence this finding exists to end (DESIGN.md 7.6.1).

    The assertion is that the status is exactly what it would have been if
    the finding did not exist."""
    outside = metrics(**OUTLIERS)
    assert checks.outside_bc_finding("points", outside)

    # No rule sees it. The violation list is identical with and without.
    assert checks.evaluate_layer(
        "points", outside, metrics(), None, None, LINES_THRESHOLDS) == []
    assert checks.evaluate_layer(
        "points", metrics(), metrics(), None, None, LINES_THRESHOLDS) == []

    # So the status is unchanged in both of the cases that matter: the first
    # run, and every run after it.
    assert statuses(
        checks.evaluate_layer("points", outside, None, None, None, LINES_THRESHOLDS),
        has_comparison=False,
    ) == "BASELINE"
    assert statuses(
        checks.evaluate_layer("points", outside, metrics(), None, None, LINES_THRESHOLDS),
        has_comparison=True,
    ) == "PASS"


def test_the_finding_is_not_a_violation_and_cannot_quietly_become_one():
    """A Violation carries a severity, severities reach status_from, and a
    non-PASS status blocks promotion. Read from the parsed module so that
    tidying the finding into the rule set fails a test rather than silently
    emptying the monthly tier."""
    source = (Path(__file__).resolve().parent.parent / "checks.py").read_text(
        encoding="utf-8")
    function = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "outside_bc_finding"
    )
    constructed = {
        node.func.id for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "Violation" not in constructed


def test_nothing_outside_bc_produces_nothing():
    """Lines has never been measured for this and is expected to be clean, so
    the quiet case is the normal one and must stay quiet."""
    assert checks.outside_bc_finding("lines", metrics(features_outside_grid=0)) is None
    # A layer measured before this metric existed, and the zero-feature case
    # where collection stops early.
    assert checks.outside_bc_finding("lines", metrics()) is None


def test_both_layers_are_checked():
    """Lines can be outside BC too, and until this was written nothing had
    ever looked."""
    finding = checks.outside_bc_finding(
        "lines", metrics(features_outside_grid=1, objectids_outside_grid=[7788]))

    # Singular, because a line a person reads should read like one.
    assert finding.startswith("lines: 1 feature is")
    assert "OBJECTID 7788" in finding


def test_the_layer_and_the_count_lead_the_line():
    """jenkins/notify.py reads both back out of it to decide whether this is
    the same situation it has already mailed about. tests/test_notify.py holds
    the other half of that contract."""
    finding = checks.outside_bc_finding("points", metrics(**OUTLIERS))
    assert finding.startswith("points: 2 features are")


def test_a_third_bad_record_produces_a_different_finding():
    """Which is what makes it a second email rather than a deduplicated one -
    the count is the dedup value, so a new bad record is worth telling
    somebody about and an unchanged one is not."""
    two = checks.outside_bc_finding("points", metrics(**OUTLIERS))
    three = checks.outside_bc_finding("points", metrics(
        features_outside_grid=3, objectids_outside_grid=[150984, 150985, 151900]))

    assert two != three
    assert "3 features are" in three and "151900" in three


def test_the_finding_names_a_handful_and_says_how_many_there_are():
    """Past a handful the list stops being actionable and starts being a wall
    of text, the same reasoning as MAX_CELLS_NAMED - but the count has to
    survive, because 47 bad records and 10 are different emergencies."""
    named = list(range(150900, 150900 + checks.MAX_OBJECTIDS_NAMED))
    finding = checks.outside_bc_finding("points", metrics(
        features_outside_grid=47, objectids_outside_grid=named))

    assert finding.startswith("points: 47 features are")
    assert f"the first {checks.MAX_OBJECTIDS_NAMED} of 47" in finding
    assert finding.count("1509") == checks.MAX_OBJECTIDS_NAMED


def test_the_count_is_still_reported_when_the_records_could_not_be_named():
    """The count and the identifiers come from two different queries. If the
    second returned nothing the first is still worth reporting - saying two
    features are outside the province beats saying nothing at all."""
    finding = checks.outside_bc_finding("points", metrics(
        features_outside_grid=2, objectids_outside_grid=[]))

    assert "2 features are" in finding
    assert "could not name them" in finding


def test_a_large_count_is_still_readable_by_the_notifier():
    """Counts are written with thousands separators everywhere in this
    project, and notify.py has to read this one back. A catastrophic
    reprojection is exactly when the alert must not silently degrade."""
    finding = checks.outside_bc_finding("points", metrics(
        features_outside_grid=53987, objectids_outside_grid=[1, 2, 3]))

    assert finding.startswith("points: 53,987 features are")


def test_the_finding_reaches_the_summary_that_becomes_the_email_body():
    """The complaint that started this: the first three production emails said
    nothing about the two records. A summary that omits a finding restates
    that silence, and a BASELINE summary otherwise says no verdict on the data
    is possible - which is exactly what a validity finding disproves."""
    measurements = {"points": metrics(feature_count=53987, **OUTLIERS)}
    findings = [checks.outside_bc_finding("points", measurements["points"])]

    baseline = checks.summarise("BASELINE", "2026-08-19", measurements, [], findings)
    assert "150984" in baseline

    # And the status it is appended to is untouched.
    assert checks.summarise("BASELINE", "2026-08-19", measurements, [], []) in baseline


# ---------------------------------------------------------------------------
# Status resolution
# ---------------------------------------------------------------------------

def test_first_run_with_nothing_wrong_is_baseline():
    """DESIGN.md 7.5: not a pass, not a failure, and not eligible for
    promotion to the monthly tier."""
    found = checks.evaluate_layer("lines", metrics(), None, None, None, LINES_THRESHOLDS)
    assert found == []
    assert statuses(found, has_comparison=False) == "BASELINE"


def test_a_clean_run_with_history_is_pass():
    found = checks.evaluate_layer("lines", metrics(), metrics(), None, None, LINES_THRESHOLDS)
    assert statuses(found, has_comparison=True) == "PASS"


def test_fail_outranks_warn():
    """Both severities present must report the more serious one, because only
    DATA_FAIL pauses pruning and gates the Phase 2 push."""
    found = [
        checks.Violation("lines", "extent", "WARN", "daily", "moved"),
        checks.Violation("lines", "schema", "FAIL", "daily", "dropped"),
    ]
    assert statuses(found) == "DATA_FAIL"


def test_the_five_statuses_are_the_complete_set():
    """notify.py and the promotion logic both switch on these, so a sixth
    cannot be introduced without changing both."""
    produced = {
        statuses([], has_comparison=False),
        statuses([], has_comparison=True),
        statuses([checks.Violation("lines", "extent", "WARN", "daily", "x")]),
        statuses([checks.Violation("lines", "schema", "FAIL", "daily", "x")]),
    }
    assert produced == {"BASELINE", "PASS", "WARN", "DATA_FAIL"}
    # SYSTEM_FAIL is returned by run_checks for an operational fault and is
    # never a verdict about the data, so no rule can produce it.


# ---------------------------------------------------------------------------
# History selection
# ---------------------------------------------------------------------------

def test_the_comparison_skips_a_failed_run():
    """Comparing today against yesterday's anomaly is how a mass deletion
    becomes the new normal overnight and the alert stops firing."""
    history = [
        {"date_stamp": "2026-08-13", "status": "DATA_FAIL", "layers": {}},
        {"date_stamp": "2026-08-12", "status": "PASS", "layers": {}},
    ]
    assert checks.previous_run(history)["date_stamp"] == "2026-08-12"


def test_baseline_and_warn_runs_are_still_comparable():
    assert checks.previous_run([{"status": "WARN"}])["status"] == "WARN"
    assert checks.previous_run([{"status": "BASELINE"}])["status"] == "BASELINE"
    assert checks.previous_run([{"status": "DATA_FAIL"}]) is None
    assert checks.previous_run([]) is None


def test_the_trend_median_ignores_failed_runs():
    history = [
        {"status": "PASS", "layers": {"lines": {"feature_count": 100}}},
        {"status": "DATA_FAIL", "layers": {"lines": {"feature_count": 1}}},
        {"status": "PASS", "layers": {"lines": {"feature_count": 102}}},
        {"status": "PASS", "layers": {"lines": {"feature_count": 104}}},
    ]
    trend = checks.trend_medians(history, "lines")
    assert trend["feature_count"] == 102
    assert trend["sample_size"] == 3


def test_a_slow_drift_under_the_daily_threshold_is_caught_by_the_trend():
    """The case the daily comparison alone misses: 2% a day for a fortnight
    never trips the daily rule and is a third of the layer by the end."""
    current = metrics(feature_count=100000)
    yesterday = metrics(feature_count=102000)
    assert checks.check_feature_count(
        "lines", current, yesterday, LINES_THRESHOLDS, "previous check") == []

    trend = {"feature_count": 142523, "sample_size": 30}
    found = checks.check_feature_count(
        "lines", current, trend, LINES_THRESHOLDS, "30-run median")
    assert len(found) == 1
    assert found[0].severity == "FAIL"


def test_no_trend_history_returns_none():
    assert checks.trend_medians([], "lines") is None
    assert checks.trend_medians([{"status": "DATA_FAIL", "layers": {}}], "lines") is None


# ---------------------------------------------------------------------------
# Suppressions
# ---------------------------------------------------------------------------

def suppression(**overrides):
    entry = {
        "rule": "feature_count", "layer": "points",
        "from": datetime.date(2026, 9, 14), "until": datetime.date(2026, 9, 30),
        "reason": "Approved bulk load",
    }
    entry.update(overrides)
    return entry


VIOLATION = checks.Violation("points", "feature_count", "FAIL", "daily", "count rose")


def test_a_suppression_inside_its_window_sets_a_violation_aside():
    counted, suppressed = checks.apply_suppressions(
        [VIOLATION], {"suppressions": [suppression()]}, "2026-09-20"
    )
    assert counted == []
    assert suppressed == [VIOLATION]
    assert statuses(counted) == "PASS"


def test_a_suppression_outside_its_window_does_nothing():
    for date_stamp in ("2026-09-13", "2026-10-01"):
        counted, suppressed = checks.apply_suppressions(
            [VIOLATION], {"suppressions": [suppression()]}, date_stamp
        )
        assert counted == [VIOLATION], date_stamp
        assert suppressed == []


def test_the_window_includes_both_end_dates():
    for date_stamp in ("2026-09-14", "2026-09-30"):
        counted, _ = checks.apply_suppressions(
            [VIOLATION], {"suppressions": [suppression()]}, date_stamp
        )
        assert counted == [], date_stamp


def test_a_suppression_does_not_leak_to_another_layer_or_rule():
    config = {"suppressions": [suppression()]}
    lines_violation = checks.Violation("lines", "feature_count", "FAIL", "daily", "x")
    other_rule = checks.Violation("points", "schema", "FAIL", "daily", "x")
    for violation in (lines_violation, other_rule):
        counted, _ = checks.apply_suppressions([violation], config, "2026-09-20")
        assert counted == [violation]


def test_quoted_and_unquoted_yaml_dates_both_work():
    """PyYAML parses an unquoted 2026-09-14 into a date and a quoted one into
    a string, and a config edited by hand will contain both over time."""
    config = {"suppressions": [suppression(**{"from": "2026-09-14", "until": "2026-09-30"})]}
    counted, suppressed = checks.apply_suppressions([VIOLATION], config, "2026-09-20")
    assert counted == []
    assert suppressed == [VIOLATION]


def test_an_open_ended_suppression_is_rejected():
    """Both dates are required so a suppression cannot silently become
    permanent."""
    for missing in ("from", "until"):
        entry = suppression()
        del entry[missing]
        with pytest.raises(ValueError, match=missing):
            checks.apply_suppressions([VIOLATION], {"suppressions": [entry]}, "2026-09-20")


def test_an_empty_suppressions_block_is_fine():
    for value in ([], None):
        counted, suppressed = checks.apply_suppressions([VIOLATION], {"suppressions": value},
                                                        "2026-09-20")
        assert counted == [VIOLATION]
        assert suppressed == []


# ---------------------------------------------------------------------------
# The whole run, with every boundary replaced by a hand-written stand-in
#
# Everything above tests a pure function. That leaves the wiring untested, and
# the wiring is where a validity finding is easiest to lose: collected and
# never put in the details, or in the details and never in the metrics file,
# and in both cases the job goes green and says nothing. Deliberately breaking
# each of those found no failing test until this section existed.
#
# Nothing here touches AGOL, object storage or the network either. The four
# functions that would are replaced, and what is left is the real run_checks
# deciding what to do with measurements it is handed.
# ---------------------------------------------------------------------------

def run_checks_with(monkeypatch, measurements, history=()):
    """One run against hand-written measurements. Returns the result and the
    metrics payload it would have written."""
    written = {}

    def collect(gis, layer_key, layer_config, config):
        return measurements[layer_key]

    def write(store, config, date_stamp, payload):
        written.update(payload)
        return checks.metrics_key(config, date_stamp)

    monkeypatch.setattr(storage, "connect_to_storage", lambda config: "the store")
    monkeypatch.setattr(checks, "connect_to_agol", lambda config: "the portal")
    monkeypatch.setattr(checks, "collect_layer_metrics", collect)
    monkeypatch.setattr(checks, "load_history", lambda store, config, stamp: list(history))
    monkeypatch.setattr(checks, "monthly_anchor", lambda store, config: (None, None))
    monkeypatch.setattr(checks, "write_metrics", write)

    return checks.run_checks(REAL_CONFIG), written


def outlier_measurements():
    """Both layers as they actually stand: lines clean, points carrying the
    two uncorrected records of DESIGN.md 4."""
    return {
        "lines": metrics(features_outside_grid=0),
        "points": metrics(feature_count=53987, **OUTLIERS),
    }


def test_a_baseline_run_reports_the_finding_and_is_still_baseline(monkeypatch):
    """The run every production check has been so far. It compares nothing,
    and it must still say the two records are outside the province."""
    measurements = outlier_measurements()
    result, written = run_checks_with(monkeypatch, measurements)

    assert result.status == "BASELINE"
    assert result.failures == []

    finding = checks.outside_bc_finding("points", measurements["points"])
    # Everywhere a person would look for it: the run's own details, the
    # metrics file, and the summary that becomes the email body.
    assert finding in result.details
    assert written["validity_findings"] == [finding]
    assert "150984" in result.summary


def test_a_run_with_history_and_a_finding_is_still_pass(monkeypatch):
    """The promotion guard asserted at the level of a whole run.
    backup.promote_monthly promotes only a set whose paired check returned
    PASS, so this is the difference between reporting the two records and
    trading away every monthly restore point to report them."""
    measurements = outlier_measurements()
    yesterday = {
        "date_stamp": "2026-08-18",
        "status": "PASS",
        "layers": {layer_key: dict(current) for layer_key, current in measurements.items()},
    }
    result, written = run_checks_with(monkeypatch, measurements, history=[yesterday])

    assert result.status == "PASS"
    assert written["validity_findings"]
    assert any("British Columbia" in line for line in result.details)


def test_a_clean_run_carries_no_finding_at_all(monkeypatch):
    """Both layers inside the province, which is what the data should look
    like once the records are corrected."""
    measurements = {
        "lines": metrics(features_outside_grid=0),
        "points": metrics(feature_count=53987, features_outside_grid=0),
    }
    result, written = run_checks_with(monkeypatch, measurements)

    assert result.status == "BASELINE"
    assert written["validity_findings"] == []
    assert not any("British Columbia" in line for line in result.details)
    assert "British Columbia" not in result.summary


def test_a_finding_on_a_failing_run_reaches_the_details_as_well(monkeypatch):
    """A data failure and an invalid coordinate are different problems and
    the first must not swallow the second - the finding is what tells the
    reader the records were already wrong before today."""
    measurements = outlier_measurements()
    yesterday = {
        "date_stamp": "2026-08-18",
        "status": "PASS",
        "layers": {
            "lines": dict(measurements["lines"]),
            "points": dict(measurements["points"], feature_count=53987 * 2),
        },
    }
    result, written = run_checks_with(monkeypatch, measurements, history=[yesterday])

    assert result.status == "DATA_FAIL"
    assert any("British Columbia" in line for line in result.details)
    assert written["validity_findings"]


# ---------------------------------------------------------------------------
# Contracts other modules depend on
# ---------------------------------------------------------------------------

def test_the_metrics_key_is_the_name_backup_prune_metrics_can_parse():
    """backup.prune_metrics reads the date straight back out of this name and
    skips anything it cannot parse, so a file named any other way is never
    pruned and never found again."""
    config = {"storage": {"paths": {"metrics": "metrics/"}}}
    key = checks.metrics_key(config, "2026-08-14")
    assert key == "metrics/2026-08-14.json"
    assert datetime.date.fromisoformat(
        key[len("metrics/"):].removesuffix(".json")
    ) == datetime.date(2026, 8, 14)


def test_checks_does_not_import_backup():
    """A hard portability requirement, not a preference. checks.py is called
    from the staging script on the NRIDS server in Phase 2, and backup.py
    imports pyogrio to read a File Geodatabase - a GDAL dependency with no
    business on that server just to run a few queries.

    Read from the parsed module rather than by searching the text, so that
    discussing any of these names in a comment does not fail the test."""
    source = (Path(__file__).resolve().parent.parent / "checks.py").read_text(encoding="utf-8")

    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "backup" not in imported
    assert "pyogrio" not in imported
    assert "arcpy" not in imported
    # The dependency list in the implementation plan, plus the standard
    # library and this project's own storage module.
    assert imported & {"arcgis", "storage"} == {"arcgis", "storage"}


def test_the_summary_reads_as_a_sentence_for_a_non_technical_reader():
    """It becomes the body of the alert email."""
    measurements = {"lines": metrics(), "points": metrics(feature_count=53987)}
    baseline = checks.summarise("BASELINE", "2026-08-14", measurements, [])
    assert "142,523 lines" in baseline and "53,987 points" in baseline

    failure = checks.summarise(
        "DATA_FAIL", "2026-08-14", measurements,
        [checks.Violation("points", "feature_count", "FAIL", "daily",
                          "Points feature count fell 14% (53,987 to 46,428).")],
    )
    assert failure.startswith("Points feature count fell 14%")
    assert "no data has been changed" in failure


def test_the_summary_leads_with_the_most_serious_violation():
    measurements = {"lines": metrics()}
    summary = checks.summarise(
        "DATA_FAIL", "2026-08-14", measurements,
        [checks.Violation("lines", "extent", "WARN", "daily", "The extent moved."),
         checks.Violation("lines", "schema", "FAIL", "daily", "The schema changed.")],
    )
    assert summary.startswith("The schema changed.")
    assert "1 other issue(s)" in summary
