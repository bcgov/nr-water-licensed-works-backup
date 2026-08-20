"""Known-answer tests for the retention decisions in backup.py.

    python -m pytest tests -q

Two questions decide what survives a year: which rotating set is copied into
the monthly archive, and when old sets stop being deleted. Both are answered
from a check outcome, and both had no test of their own until 2026-08-19 -
they were exercised only in passing, by a contract test in test_notify.py.

The bucket is a hand-written stand-in holding a dict of keys, so nothing here
touches the network. Deleting refuses outright: neither function under test
has any business deleting, and the bucket they run against in production holds
every backup this project has.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backup
import storage

PREFIX = "authorizations/backups/water_licensed_works/"


def load_real_config():
    """The config.yml the backup job will actually read.

    A fixture would let these pass while promotion.promote_on said something
    else, which is the one thing they exist to prove.
    """
    with open(ROOT / "config.yml", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


CONFIG = load_real_config()

SET_FILES = ("lines.gdb.zip", "points.gdb.zip", "manifest.json", "servicedef.json")


def bucket(rotating=(), monthly=(), checks=()):
    """A store holding whole rotating sets, monthly sets, and check statuses.

    rotating and monthly are date-stamped set names. checks maps a local date
    to the status a check recorded that day; the status object is keyed with a
    UTC timestamp two hours after the local evening slot, which is what makes
    the timezone conversion in check_status_on part of what is tested rather
    than assumed.
    """
    objects = {}
    for name in rotating:
        for file_name in SET_FILES:
            objects[f"rotating/{name}/{file_name}"] = b"x"
    for name in monthly:
        for file_name in SET_FILES:
            objects[f"monthly/{name}/{file_name}"] = b"x"
    for date_stamp, status in dict(checks).items():
        # 02:06 UTC is 19:06 the previous evening in Vancouver, which is the
        # real check slot - so the key's own date is a day AHEAD of the set it
        # belongs to. Matching on the text of the key would pair it wrongly.
        day = int(date_stamp[8:10]) + 1
        key = f"status/checks-{date_stamp[:8]}{day:02d}T02:06:00Z.json"
        objects[key] = json.dumps({"status": status}).encode("utf-8")

    copied = []

    def paginate(**kwargs):
        wanted = kwargs.get("Prefix", "")
        listed = [PREFIX + key for key in objects]
        return [{"Contents": [{"Key": key} for key in listed if key.startswith(wanted)]}]

    def get_object(Bucket, Key):
        body = objects[Key[len(PREFIX):]]
        return {"Body": SimpleNamespace(read=lambda: body)}

    def copy_object(Bucket, CopySource, Key, **kwargs):
        source = CopySource["Key"][len(PREFIX):]
        destination = Key[len(PREFIX):]
        objects[destination] = objects[source]
        # Both ends. Which set was chosen is the source, and asserting on the
        # destination instead is how this helper was wrong the first time.
        copied.append((source, destination))

    def refuse(*args, **kwargs):
        raise AssertionError("promotion must never delete from the bucket")

    client = SimpleNamespace(
        get_paginator=lambda name: SimpleNamespace(paginate=paginate),
        get_object=get_object,
        copy_object=copy_object,
        delete_object=refuse,
    )
    store = storage.Storage(client=client, bucket="gssgeodrive", prefix=PREFIX)
    return store, copied


def promoted_set(copied):
    """Which rotating set the copies came from, or None if nothing moved."""
    return copied[0][0].split("/")[1] if copied else None


def promoted_into(copied):
    """Which monthly set the copies landed in."""
    return copied[0][1].split("/")[1] if copied else None


# ---------------------------------------------------------------------------
# What blocks the monthly archive, and what only gets reported
#
# The rule the client asked for on 2026-08-19: only a verdict that the data is
# actually wrong should cost the month its archive copy. A less serious issue
# may go unfixed for weeks - nobody is scheduled to correct a stray null - and
# a fortnight of refused promotions is a worse outcome than archiving a set
# that warned.
# ---------------------------------------------------------------------------

def test_a_passing_check_is_promoted():
    store, copied = bucket(rotating=["2026-08-03"], checks={"2026-08-03": "PASS"})
    details = []

    backup.promote_monthly(store, CONFIG, "2026-08-03", details)

    assert promoted_set(copied) == "2026-08-03"
    assert promoted_into(copied) == "2026-08"
    # The whole set, not just the artifacts: the manifest travels with it,
    # which is what later lets a monthly set be paired back to its metrics.
    assert sorted(dest.split("/")[-1] for _, dest in copied) == sorted(SET_FILES)


def test_a_warning_does_not_block_the_archive():
    """THE ONE THIS SECTION EXISTS FOR.

    WARN is defined as below the action threshold - it routes to the developer
    alone precisely because it is not worth the client acting on. Treating it
    as unfit to archive contradicted what it means everywhere else, and the
    cost was real: feature_count is compared against the 30-day median and the
    monthly anchor as well as yesterday, so one legitimate bulk load holds a
    WARN for weeks and every run in that window was refused."""
    store, copied = bucket(rotating=["2026-08-03"], checks={"2026-08-03": "WARN"})
    details = []

    backup.promote_monthly(store, CONFIG, "2026-08-03", details)

    assert promoted_set(copied) == "2026-08-03"
    assert any("WARN" in line for line in details), details


def test_a_data_failure_does_block_the_archive():
    """The verdict that means the data is wrong, and the only one that costs
    the month its copy. A faithful snapshot of corrupt data passes artifact
    validation cleanly, and the monthly set becomes the anchor the rest of the
    month is compared against."""
    store, copied = bucket(rotating=["2026-08-03"], checks={"2026-08-03": "DATA_FAIL"})
    details = []

    backup.promote_monthly(store, CONFIG, "2026-08-03", details)

    assert copied == []
    assert any("not promoted" in line for line in details), details


def test_a_run_that_could_not_be_checked_is_not_promoted():
    """SYSTEM_FAIL and a day with no check at all are not verdicts, they are
    the absence of one. Both clear within a day or two and both raise their own
    alert, so neither has the weeks-long backlog problem the WARN change was
    made for."""
    for checks in ({"2026-08-03": "SYSTEM_FAIL"}, {}):
        store, copied = bucket(rotating=["2026-08-03"], checks=checks)
        backup.promote_monthly(store, CONFIG, "2026-08-03", [])
        assert copied == [], checks


def test_the_first_eligible_set_of_the_month_wins_not_the_newest():
    """Oldest first, so the archive copy is taken as early in the month as
    something qualifies. The failed run at the start of the month is skipped
    rather than ending the search."""
    store, copied = bucket(
        rotating=["2026-08-03", "2026-08-05", "2026-08-07"],
        checks={
            "2026-08-03": "DATA_FAIL",
            "2026-08-05": "WARN",
            "2026-08-07": "PASS",
        },
    )

    backup.promote_monthly(store, CONFIG, "2026-08-07", [])

    assert promoted_set(copied) == "2026-08-05"


def test_a_month_already_archived_is_left_alone():
    """Promotion runs on every backup, three times a week. Without this it
    would overwrite August's archive copy with a later one every run."""
    store, copied = bucket(
        rotating=["2026-08-03", "2026-08-05"],
        monthly=["2026-08"],
        checks={"2026-08-05": "PASS"},
    )
    details = []

    backup.promote_monthly(store, CONFIG, "2026-08-05", details)

    assert copied == []
    assert any("already promoted" in line for line in details), details


def test_a_set_from_another_month_is_never_promoted_into_this_one():
    store, copied = bucket(
        rotating=["2026-07-29", "2026-08-04"],
        checks={"2026-07-29": "PASS", "2026-08-04": "DATA_FAIL"},
    )

    backup.promote_monthly(store, CONFIG, "2026-08-04", [])

    assert copied == []


def test_the_eligible_outcomes_are_the_ones_config_names():
    """The list is a judgement, not a mechanism, so it is read from config.yml
    rather than written here. This asserts the two files agree - and that the
    two verdicts which mean trouble are not in it."""
    promote_on = CONFIG["promotion"]["promote_on"]

    assert promote_on == ["PASS", "WARN"]
    assert "DATA_FAIL" not in promote_on
    assert "SYSTEM_FAIL" not in promote_on


# ---------------------------------------------------------------------------
# Pruning, which pauses on the same verdict
# ---------------------------------------------------------------------------

def test_pruning_pauses_only_on_a_data_failure():
    """A data problem does the opposite of costing backups: pruning stops, so
    an unresolved incident cannot quietly evict the last good copy while the
    alert goes unactioned. A WARN must not trigger that - it would hold the
    rotating tier open for weeks over something nobody is acting on."""
    keeps = {}
    for status in ("PASS", "WARN", "DATA_FAIL", None):
        details = []
        store, _ = bucket()
        backup.prune(store, CONFIG, status, details)
        keeps[status] = any("pruning paused" in line for line in details)

    assert keeps["DATA_FAIL"] is True
    assert keeps["WARN"] is False
    assert keeps["PASS"] is False
    assert keeps[None] is False


def test_the_pause_is_bounded_by_the_ceiling():
    """So a failure nobody resolves cannot grow the tier without limit."""
    details = []
    store, _ = bucket()
    backup.prune(store, CONFIG, "DATA_FAIL", details)

    ceiling = CONFIG["retention"]["rotating_sets_max"]
    assert any(str(ceiling) in line for line in details), details
    assert ceiling > CONFIG["retention"]["rotating_sets"]
