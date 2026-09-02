"""Known-answer tests for the notification job.

    python -m pytest tests -q

Every test here is a hand-written sequence of status objects, polled hourly
the way Jenkins polls, with an assertion on the NUMBER OF EMAILS. That is the
point of the file. The client's question about hourly polling was whether it
would mean hourly email, and DESIGN.md 8.5 answers it in prose; this is the
same answer as arithmetic, and DESIGN.md 14 makes it an acceptance criterion:
a failure persisting three days produces one email, not three.

The five ways of getting that wrong all have tests of their own, because each
of them sends the client one email an hour until somebody intervenes:

  - a SYSTEM_FAIL escalation re-evaluated as "is it more than three days"
  - the paused-pruning alert, the same shape
  - no_monthly_candidate, which is read from a PASS run's details
  - features_outside_bc, which is true on every run until somebody edits the
    data, and is the one condition here that is not about a change at all.
    It rides in the check run's own email rather than one of its own, so its
    dedup value is folded into that run's - which is where two of the traps
    in this file live
  - the weekly summary, which must fire once on a Monday and not 24 times

Staleness has the other flavour of the same bug. Measured as hours since the
last run it false-alarms every weekend, because the Friday-to-Monday backup
gap is 72 hours by design, so it is tested across exactly that gap.

The real config.yml is loaded rather than a fixture, so the routing table,
the schedule and the grace periods under test are the ones that will run.

Nothing here touches the network or the relay. The bucket is a hand-written
stand-in whose write methods refuse to be called, which is also how the
read-only rule (DESIGN.md 9.1) is asserted rather than merely intended.
"""

import ast
import datetime
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "jenkins"))

import backup
import checks
import notify
import storage

PREFIX = "authorizations/backups/water_licensed_works/"
STATUS_PATH = "status/"

UTC = datetime.timezone.utc


def load_real_config():
    """The config.yml the job will actually read.

    A fixture would let the tests pass while the routing table said something
    else, which is the one thing they are meant to prove.
    """
    with open(ROOT / "config.yml", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


CONFIG = load_real_config()

# Addresses, one per role. The mapping from role to variable name is in
# config.yml; the addresses live only on the Jenkins host, which is what lets
# all three be pointed at one test inbox for a few days without a code change.
ADDRESSES = {
    "data_owner": "owner@example.gov.bc.ca",
    "shared_inbox": "water.business@example.gov.bc.ca",
    "developer": "developer@example.gov.bc.ca",
}


@pytest.fixture(autouse=True)
def alert_variables(monkeypatch):
    for role, variable in CONFIG["notifications"]["roles"].items():
        monkeypatch.setenv(variable, ADDRESSES[role])


@pytest.fixture
def sent(monkeypatch):
    """Every email a test would have sent, recorded instead of relayed."""
    recorded = []

    def record(config, addresses, subject, body, html=""):
        recorded.append({
            "to": list(addresses), "subject": subject, "body": body, "html": html,
        })

    monkeypatch.setattr(notify, "send_email", record)
    return recorded


# ---------------------------------------------------------------------------
# The world these tests run in
# ---------------------------------------------------------------------------


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def at(day, hour, minute=0, month=8):
    """A UTC instant in August 2026, which is the month these tests use."""
    return datetime.datetime(2026, month, day, hour, minute, tzinfo=UTC)


def run(job, moment, run_status, summary=None, details=None, rules=None):
    """One status object exactly as status.build_status writes it."""
    payload = {
        "run_id": f"{job}-{stamp(moment)}",
        "job": job,
        "status": run_status,
        "timestamp_utc": stamp(moment),
        "summary": summary or f"The {job} run finished with {run_status}.",
        "details": list(details or []),
        "rules": list(rules or []),
        "code_version": "46d2f13",
        "workflow_run_url": None,
    }
    return moment, f"{STATUS_PATH}{payload['run_id']}.json", payload


STATE_KEY = "notify/state.json"


def bucket_at(entries, now, extra_keys=(), written=None):
    """A Bucket holding every status object written at or before `now`.

    Objects appear as their runs happen, so polling the same entries an hour
    later sees exactly what Jenkins would have seen an hour later.

    Writes go into `written`, keyed by full key, so a test can assert both what
    was written and - just as important - that nothing else was. Deleting and
    copying refuse outright: this job has no business doing either, and the
    bucket it is pointed at holds every backup this project has.
    """
    visible = {key: payload for moment, key, payload in entries if moment <= now}

    def paginate(**kwargs):
        wanted = kwargs.get("Prefix", "")
        listed = [PREFIX + key for key in visible] + [PREFIX + key for key in extra_keys]
        return [{"Contents": [{"Key": key} for key in listed if key.startswith(wanted)]}]

    def get_object(Bucket, Key):
        relative = Key[len(PREFIX):]
        if relative in visible:
            payload = visible[relative]
        elif written is not None and Key in written:
            # `written` outlives one Bucket, so a state object put by an
            # earlier poll is still there for the next one. That is the whole
            # point of it being in the bucket rather than on an agent.
            payload = written[Key]
        else:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        body = json.dumps(payload).encode("utf-8")
        return {"Body": SimpleNamespace(read=lambda: body)}

    def put_object(Bucket, Key, Body):
        if written is None:
            raise AssertionError(f"this test did not expect a write, and got one to {Key}")
        written[Key] = json.loads(Body)

    def refuse(*args, **kwargs):
        raise AssertionError("notify.py must never delete from or copy in the bucket")

    client = SimpleNamespace(
        get_paginator=lambda name: SimpleNamespace(paginate=paginate),
        get_object=get_object,
        put_object=put_object,
        copy_object=refuse,
        delete_object=refuse,
    )
    return notify.Bucket(client=client, name="gssgeodrive", prefix=PREFIX)


def quiet_state(start):
    """State as it stands after an ordinary week.

    The weekly summary is recorded as already sent for the current week.
    Without it every test would open with the summary that an empty state file
    correctly produces on its first poll, and a test counting alerts would be
    counting that as well. The summary has its own tests below.
    """
    return {
        "weekly_summary": {
            "value": notify.most_recent_weekly_summary_day(CONFIG, start),
            "roles": [],
            "sent_utc": stamp(start),
        }
    }


def poll_hourly(entries, start, end, state=None, extra_keys=()):
    """Poll once an hour from start to end, as the Jenkins cron does.

    Returns the state the last poll recorded. What was sent is collected by
    the `sent` fixture.
    """
    state = quiet_state(start) if state is None else state
    now = start
    while now <= end:
        bucket = bucket_at(entries, now, extra_keys)
        state, failures = notify.poll(CONFIG, bucket, state, now, False)
        assert failures == 0
        now += datetime.timedelta(hours=1)
    return state


def count(sent, fragment):
    """How many of the emails sent carry this fragment in their subject."""
    return sum(1 for message in sent if fragment in message["subject"])


def flat(text):
    """One body with its line breaks taken back out.

    The plain text version is wrapped to a reading pane, so a sentence in it
    is not a line. These assertions are about the words and not the wrapping.
    """
    return " ".join(text.split())


def only(sent, fragment):
    """The one email whose subject carries this fragment."""
    matching = [message for message in sent if fragment in message["subject"]]
    assert len(matching) == 1, [message["subject"] for message in sent]
    return matching[0]


# A week of scheduled runs that all pass, so that a test about one thing is
# not also a test about everything else being overdue. Backup slots are local
# Mon/Wed/Fri at 21:00, which is 04:00 the next day in UTC.
def passing_week():
    entries = []
    for day in range(18, 27):
        entries.append(run("checks", at(day, 2, 6), "PASS"))
    for day in (18, 20, 22, 25):
        entries.append(run("backup", at(day, 4, 40), "PASS"))
    return entries


# ---------------------------------------------------------------------------
# The acceptance criterion: a persisting failure is one email, not one a day
# ---------------------------------------------------------------------------


def test_a_failure_persisting_three_days_produces_one_email(sent):
    """DESIGN.md 14, and the whole reason the notifier keeps state.

    Three daily check runs report DATA_FAIL. The job polls 74 times across
    them. One email.
    """
    entries = [
        run("checks", at(19, 2, 6), "DATA_FAIL", "Points feature count fell 14%."),
        run("checks", at(20, 2, 6), "DATA_FAIL", "Points feature count is still 14% down."),
        run("checks", at(21, 2, 6), "DATA_FAIL", "Points feature count is still 14% down."),
    ]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (20, 22)]

    poll_hourly(entries, at(19, 3), at(22, 3))

    assert count(sent, "DATA_FAIL") == 1


def test_the_resolution_produces_one_more_and_reaches_the_same_people(sent):
    """The other half of DESIGN.md 8.5's five-day table: one alert, one
    resolution. It goes to whoever got the alert, because an alarm nobody
    hears the end of is how a shared inbox gets rule-filtered into a folder."""
    entries = [
        run("checks", at(19, 2, 6), "DATA_FAIL", "Points feature count fell 14%."),
        run("checks", at(20, 2, 6), "DATA_FAIL"),
        run("checks", at(21, 2, 6), "DATA_FAIL"),
        run("checks", at(22, 2, 6), "PASS", "Every integrity check passed."),
    ]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (20, 22)]

    poll_hourly(entries, at(19, 3), at(23, 3))

    assert count(sent, "DATA_FAIL") == 1
    assert count(sent, "Resolved") == 1

    alert = only(sent, "DATA_FAIL")
    resolved = only(sent, "Resolved")
    assert sorted(alert["to"]) == sorted(ADDRESSES.values())
    assert sorted(resolved["to"]) == sorted(alert["to"])


def test_dedup_is_keyed_on_the_status_and_not_on_the_run_id(sent):
    """Every poll sees a new run_id once a day. A key or a value built from
    one turns a five-day incident into five emails, which is the mistake
    DESIGN.md 8.5 names explicitly."""
    entries = [run("checks", at(day, 2, 6), "WARN") for day in range(19, 24)]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (20, 22)]

    state = poll_hourly(entries, at(19, 3), at(24, 3))

    assert count(sent, "WARN") == 1
    # And what was recorded carries no run identifier of any kind.
    recorded = json.dumps(state)
    assert "checks-2026-08" not in recorded
    assert state["status:checks"]["value"] == "WARN"


def test_a_second_incident_after_a_resolution_alerts_again(sent):
    """Deduplication must not become suppression. The state records what the
    situation is, so a return to it is a change and mails again."""
    entries = [
        run("checks", at(19, 2, 6), "DATA_FAIL"),
        run("checks", at(20, 2, 6), "PASS"),
        run("checks", at(21, 2, 6), "DATA_FAIL"),
    ]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (20, 22)]

    poll_hourly(entries, at(19, 3), at(22, 3))

    assert count(sent, "DATA_FAIL") == 2
    assert count(sent, "Resolved") == 1


def test_a_pass_is_recorded_even_though_it_mails_nobody(sent):
    """PASS routes to an empty list. If that meant no state was recorded, the
    next DATA_FAIL would compare against the previous DATA_FAIL, look
    unchanged, and never be reported."""
    entries = [run("checks", at(19, 2, 6), "PASS")]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (20,)]

    state = poll_hourly(entries, at(19, 3), at(20, 3))

    assert count(sent, "PASS") == 0
    assert state["status:checks"]["value"] == "PASS"


# ---------------------------------------------------------------------------
# The threshold escalations, which are the ones that flood
# ---------------------------------------------------------------------------


def test_the_system_fail_escalation_fires_once_not_once_an_hour(sent):
    """The single worst bug available in this file.

    'Has it been more than three days' is true on every poll for as long as
    the failure lasts, and the escalation adds the data owner and the shared
    inbox. Re-evaluated per poll this is one email an hour to the client.
    """
    entries = [run("checks", at(day, 2, 6), "SYSTEM_FAIL") for day in range(18, 24)]

    poll_hourly(entries, at(18, 3), at(24, 3))

    assert count(sent, "SYSTEM_FAIL") == 1
    assert count(sent, "has been failing for") == 1

    escalation = only(sent, "has been failing for")
    assert sorted(escalation["to"]) == sorted(ADDRESSES.values())


def test_the_escalation_waits_for_the_configured_number_of_days(sent):
    """Two days of SYSTEM_FAIL is the developer's problem alone.
    system_fail_escalation_days is 3."""
    entries = [run("checks", at(day, 2, 6), "SYSTEM_FAIL") for day in (18, 19)]

    poll_hourly(entries, at(18, 3), at(20, 1))

    assert count(sent, "SYSTEM_FAIL") == 1
    assert count(sent, "has been failing for") == 0


def test_the_escalated_recipients_are_told_when_it_ends(sent):
    """The original SYSTEM_FAIL alert went to the developer only, so the two
    people the escalation added would otherwise never hear it was over."""
    entries = [run("checks", at(day, 2, 6), "SYSTEM_FAIL") for day in range(18, 24)]
    entries.append(run("checks", at(24, 2, 6), "PASS", "Every integrity check passed."))

    poll_hourly(entries, at(18, 3), at(25, 3))

    resolved = only(sent, "Resolved")
    assert sorted(resolved["to"]) == sorted(ADDRESSES.values())


def test_a_later_system_fail_escalates_again(sent):
    """The state entry is dropped when the episode ends, which is what makes
    a recurrence a fresh incident rather than a suppressed one."""
    entries = [run("checks", at(day, 2, 6), "SYSTEM_FAIL") for day in range(18, 23)]
    entries.append(run("checks", at(23, 2, 6), "PASS"))
    entries += [run("checks", at(day, 2, 6), "SYSTEM_FAIL") for day in range(24, 29)]

    poll_hourly(entries, at(18, 3), at(29, 3))

    assert count(sent, "has been failing for") == 2


def test_the_paused_prune_alert_fires_once(sent):
    """Same shape as the escalation. Pruning pauses while a DATA_FAIL is open
    and past retention.paused_prune_alert_days that is its own alert."""
    paused = ["pruning paused: the most recent check status is DATA_FAIL, so "
              "rotating sets are kept up to the ceiling of 20 rather than the usual 8"]
    entries = [run("checks", at(day, 2, 6), "DATA_FAIL") for day in range(15, 28)]
    entries += [
        run("backup", at(day, 4, 40), "PASS", details=paused)
        for day in (15, 18, 20, 22, 25, 27)
    ]

    poll_hourly(entries, at(15, 6), at(28, 3))

    assert count(sent, "pruning has been paused") == 1


def test_a_backup_that_never_reached_the_prune_step_does_not_restart_the_clock(sent):
    """A SYSTEM_FAIL backup reported nothing either way about pruning.
    Treating it as the end of the episode would restart the seven days and
    send a second email for one unresolved incident."""
    paused = ["pruning paused: the most recent check status is DATA_FAIL, so "
              "rotating sets are kept up to the ceiling of 20 rather than the usual 8"]
    entries = [run("checks", at(day, 2, 6), "DATA_FAIL") for day in range(15, 28)]
    entries += [
        run("backup", at(day, 4, 40), "PASS", details=paused) for day in (15, 18, 20)
    ]
    entries.append(run("backup", at(22, 4, 40), "SYSTEM_FAIL"))
    entries += [
        run("backup", at(day, 4, 40), "PASS", details=paused) for day in (25, 27)
    ]

    poll_hourly(entries, at(15, 6), at(28, 3))

    assert count(sent, "pruning has been paused") == 1


# ---------------------------------------------------------------------------
# no_monthly_candidate, the one condition read from details rather than status
# ---------------------------------------------------------------------------

# The line backup.promote_monthly wrote on the first production backup,
# 2026-08-18, verbatim.
NO_CANDIDATE = (
    "2026-08 is 18 days in with nothing promoted to the monthly tier - "
    "no rotating set has a paired PASS from the check job"
)
NO_CANDIDATE_BENIGN = "no rotating set of 2026-08 has a passing check yet, monthly not promoted"


def test_the_monthly_candidate_warning_is_reachable_at_all(sent):
    """It is carried in the details of a run whose status is PASS, and PASS
    routes to nobody. Configured, logged, written into the status object and
    unreachable - which is what happened on 2026-08-18 (DESIGN.md 8.4)."""
    entries = [run("checks", at(day, 2, 6), "PASS") for day in (18, 19, 20)]
    entries.append(
        run("backup", at(20, 4, 40), "PASS",
            details=["published rotating/2026-08-19 with 4 objects",
                     NO_CANDIDATE_BENIGN, NO_CANDIDATE])
    )

    poll_hourly(entries, at(18, 3), at(21, 3))

    alert = only(sent, "monthly backup tier")
    assert "2026-08" in alert["subject"]
    assert alert["to"] == [ADDRESSES["developer"]]


def test_the_monthly_candidate_warning_is_one_email_per_month(sent):
    """Backups run three times a week and each one repeats the warning. The
    dedup value is the month, so August is one email however many runs carry
    it, and September is a second."""
    august = ["2026-08 is 20 days in with nothing promoted to the monthly tier - x"]
    september = ["2026-09 is 12 days in with nothing promoted to the monthly tier - x"]
    entries = [run("checks", at(day, 2, 6), "PASS") for day in range(18, 30)]
    entries += [
        run("backup", at(day, 4, 40), "PASS", details=august) for day in (18, 20, 22, 25)
    ]
    entries += [run("checks", at(day, 2, 6, month=9), "PASS") for day in (10, 11, 12)]
    entries += [
        run("backup", at(day, 4, 40, month=9), "PASS", details=september)
        for day in (10, 12)
    ]

    # The same state carries into September, so the second email is a
    # genuinely new month rather than a forgotten one.
    state = poll_hourly(entries, at(18, 3), at(25, 3))
    poll_hourly(entries, at(10, 6, month=9), at(13, 3, month=9), state=state)

    assert count(sent, "monthly backup tier") == 2
    assert [message["subject"] for message in sent if "monthly backup tier" in message["subject"]] == [
        f"{notify.SUBJECT_PREFIX} Nothing promoted to the monthly backup tier for 2026-08",
        f"{notify.SUBJECT_PREFIX} Nothing promoted to the monthly backup tier for 2026-09",
    ]


def test_the_ordinary_not_promoted_yet_line_is_not_an_alert(sent):
    """Every backup that promotes nothing logs that it promoted nothing. Only
    the line past promotion.no_candidate_alert_days is the alert - reading the
    benign one would mail three times a week for ever."""
    entries = [run("checks", at(day, 2, 6), "PASS") for day in (18, 19, 20)]
    entries.append(
        run("backup", at(20, 4, 40), "PASS", details=[NO_CANDIDATE_BENIGN])
    )

    poll_hourly(entries, at(18, 3), at(21, 3))

    assert count(sent, "monthly backup tier") == 0


# ---------------------------------------------------------------------------
# features_outside_bc, carried in the check run's own email
#
# DESIGN.md 7.6.1 and 7.6.1.1. checks.py keeps this out of its status on
# purpose, because a status that can never be PASS permanently blocks monthly
# promotion. That also makes it invisible to the routing table, so the
# features_outside_bc row widens who the run's own message goes to.
#
# One email per run, not one per kind of problem. The dedup value therefore
# carries both - the status AND what the finding currently is - so mail goes
# out when either moves, and when neither does, nothing goes out at all.
# ---------------------------------------------------------------------------

# The lines checks.outside_bc_finding writes for the two records of DESIGN.md
# 4, and for a third appearing. The contract test at the foot of this file
# builds them from checks.py rather than trusting these copies.
OUTSIDE_BC = (
    "points: 2 features are outside British Columbia "
    "(OBJECTID 150984, 150985)."
)
OUTSIDE_BC_THIRD = (
    "points: 3 features are outside British Columbia "
    "(OBJECTID 150984, 150985, 151900)."
)

# What the subject carries when a run reports one. The counts and the
# OBJECTIDs stay out of it and go in the body, where there is room to say what
# they mean - and so does the run status on a run that found no change, since
# "PASS" next to a data problem reads as a contradiction (notify.status_subject).
FINDINGS_SUBJECT = "outside British Columbia"


def test_a_finding_rides_in_the_run_s_own_email(sent):
    """One email, carrying the status and the finding together. Every
    production run so far returned BASELINE, which compares nothing, so a
    finding that waited for a comparison would have said nothing on any of
    them."""
    entries = [
        run("checks", at(day, 2, 6), "BASELINE", details=[OUTSIDE_BC])
        for day in (18, 19, 20)
    ]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (18, 20)]

    poll_hourly(entries, at(18, 3), at(21, 3))

    alert = only(sent, FINDINGS_SUBJECT)
    # No status word: BASELINE is not news and would only compete with the
    # thing the reader can act on. It is still in the body, in the run block.
    assert alert["subject"] == (
        f"{notify.SUBJECT_PREFIX} Daily integrity check: "
        f"features recorded outside British Columbia")
    assert "BASELINE" not in alert["subject"]
    assert "BASELINE" in alert["body"]
    assert "2 features are" in alert["body"]
    assert "150984" in alert["body"] and "150985" in alert["body"]
    # The context that only makes sense in an email, and the sentence that
    # stops a reader assuming their backups have stopped being promoted.
    assert "does not stop backups being promoted" in flat(alert["body"])
    assert "will need to be corrected" in flat(alert["body"])
    # The status alone would have reached the developer. The finding widens it
    # to the people who would correct the records.
    assert sorted(alert["to"]) == sorted(
        [ADDRESSES["data_owner"], ADDRESSES["shared_inbox"], ADDRESSES["developer"]]
    )


def test_a_finding_reaches_somebody_on_a_run_that_passed(sent):
    """The case that decides the whole design. PASS routes to nobody, so a
    finding carried only by the status would reach no one on any healthy day -
    and a healthy day is what the project is working towards."""
    entries = [
        run("checks", at(day, 2, 6), "PASS", details=[OUTSIDE_BC])
        for day in (18, 19, 20)
    ]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (18, 20)]

    poll_hourly(entries, at(18, 3), at(21, 3))

    alert = only(sent, FINDINGS_SUBJECT)
    # And emphatically not "PASS: daily integrity check, with data quality
    # findings", which is what it said until 2026-08-19. PASS means nothing
    # moved since yesterday, not that the data is good.
    assert "PASS" not in alert["subject"]
    assert ADDRESSES["data_owner"] in alert["to"]


def test_an_uncorrected_backlog_is_one_email_and_not_one_a_day(sent):
    """The objection that kept this a measurement with no rule for as long as
    it was one: an always-true violation mailing the data owner daily about a
    backlog nobody is scheduled to fix. The value carries the count, so a week
    of runs reporting the same two records is one email."""
    entries = [
        run("checks", at(day, 2, 6), "PASS", details=[OUTSIDE_BC])
        for day in range(18, 27)
    ]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (18, 20, 22, 25)]

    poll_hourly(entries, at(18, 3), at(26, 12))

    assert count(sent, FINDINGS_SUBJECT) == 1


def test_a_third_bad_record_is_a_second_email(sent):
    """A new invalid record is worth telling somebody about even while the
    first two are still there, and even though the run's status has not moved.
    Two emails, and the second names the third record."""
    entries = [
        run("checks", at(day, 2, 6), "PASS", details=[OUTSIDE_BC])
        for day in (18, 19, 20)
    ]
    entries += [
        run("checks", at(day, 2, 6), "PASS", details=[OUTSIDE_BC_THIRD])
        for day in (21, 22, 23)
    ]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (18, 20, 22)]

    poll_hourly(entries, at(18, 3), at(24, 3))

    bodies = [
        message["body"] for message in sent if FINDINGS_SUBJECT in message["subject"]
    ]
    assert len(bodies) == 2
    assert "2 features are" in bodies[0] and "151900" not in bodies[0]
    assert "3 features are" in bodies[1] and "151900" in bodies[1]


def test_a_status_change_while_a_finding_is_open_is_one_email_not_two(sent):
    """The point of merging. The run goes BASELINE then PASS with the same two
    records throughout: one email for the finding appearing, one for the status
    moving, and never two describing the same run."""
    entries = [
        run("checks", at(day, 2, 6), "BASELINE", details=[OUTSIDE_BC])
        for day in (18, 19)
    ]
    entries += [
        run("checks", at(day, 2, 6), "PASS", details=[OUTSIDE_BC])
        for day in (20, 21, 22)
    ]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (18, 20, 22)]

    poll_hourly(entries, at(18, 3), at(23, 3))

    # Two emails, one per run that reported something new, and never two
    # about the same run. Neither subject carries a status, so they read the
    # same and the table at the top of the body tells them apart.
    bodies = [
        message["body"] for message in sent if FINDINGS_SUBJECT in message["subject"]
    ]
    assert len(bodies) == 2
    assert "BASELINE" in bodies[0]
    assert "PASS" in bodies[1] and "BASELINE" not in bodies[1]


def test_a_clean_run_says_nothing_about_findings(sent):
    """Lines is expected to hold nothing outside the province, and the quiet
    case has to stay quiet or the report is worth nothing when it fires. A
    PASS with no finding routes to nobody, exactly as it always has."""
    entries = [run("checks", at(day, 2, 6), "PASS") for day in (18, 19, 20)]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (18, 20)]

    poll_hourly(entries, at(18, 3), at(21, 3))

    # Nothing in a subject and nothing in a body: a clean check is silent, so
    # the merged design must not have made a PASS reach anybody.
    assert count(sent, FINDINGS_SUBJECT) == 0
    assert not any("British Columbia" in message["body"] for message in sent)
    assert not any("integrity check" in message["subject"] for message in sent)


def test_correcting_the_records_is_quiet_and_a_relapse_alerts_again(sent):
    """Once corrected the run is an ordinary PASS, which routes to nobody, so
    nothing is sent - and the state records the bare status, so the same
    records reappearing is a fresh alert rather than one deduplicated away
    against a value from months ago."""
    entries = [
        run("checks", at(day, 2, 6), "PASS", details=[OUTSIDE_BC]) for day in (18, 19)
    ]
    entries += [run("checks", at(day, 2, 6), "PASS") for day in (20, 21)]
    entries += [
        run("checks", at(day, 2, 6), "PASS", details=[OUTSIDE_BC]) for day in (22, 23)
    ]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (18, 20, 22)]

    poll_hourly(entries, at(18, 3), at(24, 3))

    assert count(sent, FINDINGS_SUBJECT) == 2


def test_both_layers_appear_in_one_email(sent):
    """One situation, one message. Lines going bad while points already is
    changes the value, so it is a second email rather than a silent addition
    to one already sent."""
    lines_too = "lines: 1 feature is outside British Columbia (OBJECTID 7788)."
    entries = [
        run("checks", at(day, 2, 6), "PASS", details=[OUTSIDE_BC]) for day in (18, 19)
    ]
    entries += [
        run("checks", at(day, 2, 6), "PASS", details=[OUTSIDE_BC, lines_too])
        for day in (20, 21)
    ]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (18, 20)]

    poll_hourly(entries, at(18, 3), at(22, 3))

    bodies = [
        message["body"] for message in sent if FINDINGS_SUBJECT in message["subject"]
    ]
    assert len(bodies) == 2
    assert "lines:" not in bodies[0]
    # Both layers, each as its own line, so the OBJECTIDs stay next to the
    # layer they belong to.
    assert "lines layer - 1 feature is" in bodies[1]
    assert "points layer - 2 features are" in bodies[1]


def test_an_incident_clearing_while_a_finding_is_open_sends_one_resolution(sent):
    """The trap in merging the two. A DATA_FAIL clearing to PASS is a
    resolution email, and the state it records has to describe the finding as
    well - recording a bare PASS would make the very next poll see a changed
    value and send a second email about a situation nobody's inbox has
    changed."""
    entries = [
        run("checks", at(day, 2, 6), "DATA_FAIL", details=[OUTSIDE_BC])
        for day in (18, 19)
    ]
    entries += [
        run("checks", at(day, 2, 6), "PASS", details=[OUTSIDE_BC])
        for day in (20, 21, 22)
    ]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (18, 20, 22)]

    poll_hourly(entries, at(18, 3), at(23, 3))

    assert count(sent, "DATA_FAIL") == 1
    assert count(sent, "Resolved") == 1
    # And nothing after it. The resolution has to record the finding in its
    # own state entry: recording a bare PASS would leave the very next poll
    # seeing a changed value and sending a second email about a situation
    # nobody's inbox has changed. That extra message is not another
    # resolution, so counting resolutions alone would miss it.
    assert count(sent, FINDINGS_SUBJECT) == 1

    # The resolution names the status that was alerting, not the composite
    # value the state happens to store it under.
    resolution = only(sent, "Resolved")
    assert "previous alert reported DATA_FAIL" in flat(resolution["body"])
    # And the finding is still in front of the reader, because it is still
    # true - the incident cleared, the invalid records did not.
    assert "150984" in resolution["body"]
    assert "does not stop backups being promoted" in flat(resolution["body"])


def test_the_dedup_value_is_the_bare_status_when_there_is_no_finding():
    """Every state entry already in the bucket and every count test written
    for Step 6 depends on this. A composite value on a clean run would make the
    first poll after the deploy see a change and mail about it."""
    assert notify.status_value("PASS", []) == "PASS"
    assert notify.status_value("DATA_FAIL", ()) == "DATA_FAIL"
    assert notify.status_value("PASS", [OUTSIDE_BC]) == "PASS + points 2"

    # And it splits back apart wherever a recorded value is compared against
    # the five statuses.
    assert notify.status_part("PASS + points 2") == "PASS"
    assert notify.status_part("DATA_FAIL") == "DATA_FAIL"


# ---------------------------------------------------------------------------
# Staleness, measured from the expected slot
#
# DESIGN.md 8.2. Backup slots are local Mon/Wed/Fri at 21:00, which is 04:00
# the next day in UTC; the check slot is 02:00 UTC daily.
# ---------------------------------------------------------------------------


def test_the_friday_to_monday_backup_gap_does_not_false_alarm(sent):
    """72 hours with no backup, by design. Measured as hours-since this fires
    every single weekend, and a weekly false alarm is how an alert channel
    stops being read.

    The Friday backup is at 21:00 local on 2026-08-21, which is 04:40 UTC on
    the Saturday. The next slot is 21:00 local on Monday 2026-08-24, which is
    04:00 UTC on the Tuesday and not overdue until 10:00 UTC with the
    six-hour grace. This polls all 77 hours in between.
    """
    entries = [run("checks", at(day, 2, 6), "PASS") for day in (22, 23, 24, 25)]
    entries.append(run("backup", at(22, 4, 40), "PASS"))

    poll_hourly(entries, at(22, 6), at(25, 9))

    assert count(sent, "did not run") == 0


def test_a_missed_backup_slot_alerts_after_the_grace_period(sent):
    """And the same weekend with the Monday backup missing does fire, once,
    however many times it is polled afterwards."""
    entries = [run("checks", at(day, 2, 6), "PASS") for day in (22, 23, 24, 25, 26)]
    entries.append(run("backup", at(22, 4, 40), "PASS"))

    poll_hourly(entries, at(22, 6), at(26, 12))

    assert count(sent, "backup did not run") == 1
    assert only(sent, "backup did not run")["to"] == [ADDRESSES["developer"]]


def test_the_backup_grace_period_is_respected(sent):
    """Six hours, because a backup run is two sequential exports and the
    status object is written at the end of it. Nothing fires before then."""
    entries = [run("checks", at(day, 2, 6), "PASS") for day in (22, 23, 24, 25)]
    entries.append(run("backup", at(22, 4, 40), "PASS"))

    # Up to 09:00 UTC on the Tuesday, one hour short of the 04:00 slot plus
    # six hours of grace.
    poll_hourly(entries, at(25, 5), at(25, 9))

    assert count(sent, "did not run") == 0


def test_a_missed_check_slot_alerts_after_the_grace_period(sent):
    """The check grace is four hours, which is only enforceable because this
    job polls hourly. A job that looked once a day could not detect it at all
    (DESIGN.md 8.5).

    The boundary is pinned from both sides on purpose. It was two hours until
    2026-08-28, when a run that started 165 minutes late tripped this and
    then passed; the test that stood here polled from 04:00 and would have
    kept passing at any grace up to eight hours, so it asserted the alert but
    not the threshold. This one stays quiet up to 05:30 and fires at 06:00 and
    no later, so the setting cannot move in either direction without failing
    here. The boundary is inclusive - last_due_slot asks now >= slot + grace.
    """
    entries = [run("checks", at(day, 2, 6), "PASS") for day in (22, 23)]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (22, 25)]

    # The 2026-08-24 02:00 UTC slot is overdue from 06:00 UTC.
    poll_hourly(entries, at(23, 3), at(24, 5, 30))
    before = count(sent, "integrity check did not run")
    poll_hourly(entries, at(24, 6), at(24, 6, 30), state=quiet_state(at(23, 3)))
    after = count(sent, "integrity check did not run")

    assert before == 0
    assert after == 1


def test_a_check_run_late_but_inside_the_grace_is_not_stale(sent):
    """The case that produced the change. A run for the 02:00 slot that does
    not write its status object until 04:47 is late, not stopped, and the
    hourly polls across that window must stay quiet."""
    entries = [run("checks", at(23, 2, 6), "PASS"), run("backup", at(22, 4, 40), "PASS")]
    # The 2026-08-24 slot, covered at 04:47 - past the old two-hour grace.
    entries.append(run("checks", at(24, 4, 47), "PASS"))

    poll_hourly(entries, at(24, 3), at(24, 12))

    assert count(sent, "integrity check did not run") == 0


def test_staleness_is_one_email_however_long_the_pipeline_stays_down(sent):
    """A stopped pipeline is one situation, not one a day. The resolution
    arrives as the next run's own status object."""
    entries = [run("checks", at(18, 2, 6), "PASS"), run("backup", at(18, 4, 40), "PASS")]

    poll_hourly(entries, at(18, 6), at(28, 6))

    assert count(sent, "integrity check did not run") == 1
    assert count(sent, "backup did not run") == 1


# ---------------------------------------------------------------------------
# The weekly summary
# ---------------------------------------------------------------------------


def test_the_weekly_summary_fires_once_on_monday(sent):
    """Monday 08:00 local, which is 15:00 UTC in PDT. Polled 24 times that
    day it must produce one email, not 24."""
    entries = passing_week()
    state = {
        "weekly_summary": {"value": "2026-08-17", "roles": [], "sent_utc": stamp(at(17, 15))}
    }

    poll_hourly(entries, at(24, 0), at(25, 0), state=state)

    assert count(sent, "Weekly summary") == 1
    summary = only(sent, "Weekly summary")
    assert "2026-08-24" in summary["subject"]
    assert sorted(summary["to"]) == sorted(ADDRESSES.values())
    assert "Nothing is overdue" in flat(summary["body"])


def test_the_summary_does_not_fire_before_the_configured_hour(sent):
    """08:00 local is 15:00 UTC. Polling the Monday morning up to 14:00 UTC
    must produce nothing."""
    entries = passing_week()
    state = {
        "weekly_summary": {"value": "2026-08-17", "roles": [], "sent_utc": stamp(at(17, 15))}
    }

    poll_hourly(entries, at(24, 0), at(24, 14), state=state)

    assert count(sent, "Weekly summary") == 0


def test_the_summary_reports_what_ran_and_what_is_overdue(sent):
    """It is reassurance rather than detection (DESIGN.md 8.2), so it reports
    rather than judges - but a pipeline that has stopped should not be
    reported as healthy."""
    entries = [run("checks", at(18, 2, 6), "PASS"), run("backup", at(18, 4, 40), "PASS")]
    state = {
        "weekly_summary": {"value": "2026-08-17", "roles": [], "sent_utc": stamp(at(17, 15))}
    }

    poll_hourly(entries, at(24, 14), at(24, 16), state=state)

    summary = only(sent, "Weekly summary")
    assert "Overdue" in summary["body"]
    assert "Nothing is overdue" not in summary["body"]


# ---------------------------------------------------------------------------
# The routing table drives every recipient
# ---------------------------------------------------------------------------


def test_the_routing_table_drives_recipients_for_all_five_statuses(sent):
    """DESIGN.md 8.4 is the complete routing specification and config.yml is
    where it lives. Nothing in notify.py hardcodes a role for a status."""
    expected = {
        "PASS": [],
        "BASELINE": [ADDRESSES["developer"]],
        "WARN": [ADDRESSES["developer"]],
        "DATA_FAIL": sorted(ADDRESSES.values()),
        "SYSTEM_FAIL": [ADDRESSES["developer"]],
    }
    for run_status, recipients in expected.items():
        sent.clear()
        entries = [run("checks", at(19, 2, 6), run_status)]
        entries += [run("backup", at(day, 4, 40), "PASS") for day in (20,)]
        poll_hourly(entries, at(19, 3), at(19, 8))

        matching = [message for message in sent if run_status in message["subject"]]
        if not recipients:
            assert matching == [], run_status
        else:
            assert len(matching) == 1, run_status
            assert sorted(matching[0]["to"]) == recipients, run_status


def test_every_routed_outcome_has_a_row_in_config():
    """The five statuses plus the five this job raises itself. A missing row
    would present as an alert that was never sent."""
    routing = CONFIG["notifications"]["routing"]
    assert set(routing) == {
        "PASS", "BASELINE", "WARN", "DATA_FAIL", "SYSTEM_FAIL",
        "stale", "prune_paused", "no_monthly_candidate", "features_outside_bc",
        "weekly_summary",
    }
    for outcome in routing:
        assert notify.routing_for(CONFIG, outcome) == list(routing[outcome])


def test_an_outcome_with_no_row_is_refused():
    with pytest.raises(ValueError, match="no notifications.routing row"):
        notify.routing_for(CONFIG, "SOMETHING_NEW")


def test_a_role_with_no_address_is_refused_in_a_real_run(monkeypatch):
    """An alert with nowhere to go is worse than a failed build, because the
    failed build is at least visible."""
    monkeypatch.delenv("ALERT_DEVELOPER", raising=False)
    with pytest.raises(ValueError, match="No address is set for"):
        notify.role_addresses(CONFIG, dry_run=False)

    # A dry run reports it instead, so the routing can be read back before the
    # variables are in place.
    addresses = notify.role_addresses(CONFIG, dry_run=True)
    assert addresses["developer"] == ["<ALERT_DEVELOPER not set>"]


# ---------------------------------------------------------------------------
# The summary is the email body, unedited
# ---------------------------------------------------------------------------


def test_the_summary_is_the_body_and_is_not_reworded(sent):
    """checks.py writes it for a non-technical reader precisely so that it can
    be the body. Paraphrasing it here would only lose that work."""
    summary = (
        "Points feature count fell 14% (53,987 to 46,428). The backups are "
        "untouched and no data has been changed."
    )
    entries = [run("checks", at(19, 2, 6), "DATA_FAIL", summary)]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (20,)]

    poll_hourly(entries, at(19, 3), at(19, 8))

    body = flat(only(sent, "DATA_FAIL")["body"])
    assert summary in body
    assert "no data has been changed" in body.lower()


def test_the_details_and_the_run_facts_are_carried_into_the_body(sent):
    entries = [
        run("checks", at(19, 2, 6), "WARN", "Lines extent moved 6,200 m.",
            details=["extent drift 6,200 m exceeds 5,000 m", "compared against 2026-08-18"])
    ]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (20,)]

    poll_hourly(entries, at(19, 3), at(19, 8))

    body = flat(only(sent, "WARN")["body"])
    assert "extent drift 6,200 m exceeds 5,000 m" in body
    assert "46d2f13" in body
    assert "not a GitHub Actions run" in body


# ---------------------------------------------------------------------------
# How a message reads
#
# The recipients are a data owner, a shared mailbox and a developer, and only
# the last of those is paid to decode a status object. So the shape of the
# message is a requirement rather than a decoration: what happened, then what
# needs a person, then the reassurance, and only then the run identifiers.
# ---------------------------------------------------------------------------


def every_message(sent):
    """Every message a poll sent, which several of these check all of."""
    assert sent, "the fixture sent nothing, so this test asserts nothing"
    return sent


def busy_week():
    """A week with something of every kind in it: a failure, a finding, a
    resolution, a backup, and a gap wide enough to go stale."""
    entries = [
        run("checks", at(18, 2, 6), "DATA_FAIL", "Points feature count fell 14%.",
            details=["points count fell 14%", OUTSIDE_BC]),
        run("checks", at(19, 2, 6), "PASS", "No unexpected change.",
            details=[OUTSIDE_BC]),
    ]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (18, 20)]
    return entries


def test_every_message_opens_by_saying_it_was_sent_automatically(sent):
    """The first question an automated email raises is whether a person sent
    it, and it used to be answered by one line at the very bottom. It is now
    the first thing in the message and it is highlighted, which is the whole
    of why the HTML version exists."""
    poll_hourly(busy_week(), at(18, 3), at(21, 3))

    for message in every_message(sent):
        assert message["body"].startswith("*** Sent automatically by the ")
        assert "Sent automatically" in message["body"]
        # Highlighted rather than merely present. The background colour is
        # the point, so a version of this that dropped it should fail.
        assert notify.BANNER_STYLE in message["html"]


def test_no_message_says_the_gis_server(sent):
    """Which server the job runs on is ours and not the reader's. It said so
    in the footer of every message until 2026-08-25."""
    poll_hourly(busy_week(), at(18, 3), at(21, 3))

    for message in every_message(sent):
        assert "GIS server" not in message["body"]
        assert "GIS server" not in message["html"]


def test_every_message_carries_both_versions_of_its_body(sent):
    """One list of blocks renders twice, so the formatted version and the
    fallback cannot say different things. A client that cannot show HTML gets
    a message that still reads as a message rather than as markup."""
    poll_hourly(busy_week(), at(18, 3), at(21, 3))

    for message in every_message(sent):
        assert message["html"].startswith("<html>")
        # The markers are how bold is written and they are for the renderer,
        # not for the reader. The banner's own asterisks are its decoration in
        # the text version, so the body is checked from after it.
        assert "**" not in message["body"].split("***", 2)[-1]
        assert "<strong>" in message["html"]


def test_the_run_identifiers_come_after_everything_a_reader_needs(sent):
    """The data owner and the shared inbox are on most of these. A run id and
    a commit hash in the middle of a message about their data is noise that
    makes the part they can act on harder to find, so all of it sits at the
    foot behind its own heading."""
    poll_hourly(busy_week(), at(18, 3), at(21, 3))

    body = only(sent, "DATA_FAIL")["body"]
    assert notify.TECHNICAL_HEADING in body
    assert body.index("Points feature count fell 14%.") < body.index(notify.TECHNICAL_HEADING)
    assert body.index("Nothing has been changed.") < body.index(notify.TECHNICAL_HEADING)
    assert body.index("46d2f13") > body.index(notify.TECHNICAL_HEADING)


def test_a_finding_is_reported_once_and_not_twice(sent):
    """checks.py appends the finding to the summary so that the line is
    complete wherever it is read. An email gives it a section of its own, so
    the summary is cut at the lead-in - without which the reader is told the
    same thing twice in four lines."""
    summary = checks.summarise(
        "PASS", "2026-08-20",
        {"points": {"feature_count": 53993}}, [], [OUTSIDE_BC],
    )
    entries = [run("checks", at(19, 2, 6), "PASS", summary, details=[OUTSIDE_BC])]
    entries += [run("backup", at(20, 4, 40), "PASS")]

    poll_hourly(entries, at(19, 3), at(19, 8))

    body = only(sent, FINDINGS_SUBJECT)["body"]
    assert "in line with recent runs" in body
    assert checks.FINDING_LEAD_IN.strip() not in body
    # Once in the section that explains it, once in the raw lines at the foot
    # of the message, and nowhere else.
    assert body.count("150984") == 2


def test_the_finding_lead_in_matches_what_checks_py_writes():
    """The other half of the contract, built from the real function rather
    than from a copy of its wording. A rewording in one file and not the other
    leaves the finding printed twice, which is not silent but is not right
    either."""
    assert notify.FINDING_LEAD_IN == checks.FINDING_LEAD_IN

    summary = checks.summarise(
        "BASELINE", "2026-08-20",
        {"points": {"feature_count": 53993}}, [], [OUTSIDE_BC],
    )
    record = notify.StatusRecord(
        job="checks", moment=at(19, 2, 6), key="status/x.json",
        payload={"status": "BASELINE", "summary": summary},
    )
    cut = notify.summary_of(record)
    assert cut.endswith("possible until the next run.")
    assert "150984" not in cut


def test_html_is_escaped_rather_than_pasted_in():
    """Nothing in these messages comes from a stranger, but a summary is free
    text written by another module and an ampersand in one should not be able
    to break the markup."""
    record = notify.StatusRecord(
        job="checks", moment=at(19, 2, 6), key="status/x.json",
        payload={
            "status": "WARN",
            "summary": "Counts <fell> sharply & the extent moved.",
            "code_version": "46d2f13",
        },
    )
    message = notify.status_notification(CONFIG, record)

    assert "&lt;fell&gt;" in message.html
    assert "&amp;" in message.html
    assert "<fell>" not in message.html
    # And the reader of the text version sees what was written.
    assert "Counts <fell> sharply & the extent moved." in message.body


def test_the_weekly_summary_is_a_table_a_person_can_scan(sent):
    """It is the one message that reports several things at once, which is
    exactly the shape prose is worst at."""
    entries = passing_week()

    state = {
        "weekly_summary": {"value": "2026-08-17", "roles": [], "sent_utc": stamp(at(17, 15))}
    }

    poll_hourly(entries, at(24, 0), at(25, 0), state=state)

    summary = only(sent, "Weekly summary")
    assert "Job" in summary["body"] and "Runs" in summary["body"]
    assert "daily integrity check" in summary["body"]
    assert "<table" in summary["html"] and "<th" in summary["html"]



# ---------------------------------------------------------------------------
# The dry run, which is how this is tested against a real address
# ---------------------------------------------------------------------------


def test_a_dry_run_sends_nothing(sent, capsys):
    entries = [run("checks", at(19, 2, 6), "DATA_FAIL", "Points feature count fell 14%.")]
    now = at(19, 3)

    state, failures = notify.poll(CONFIG, bucket_at(entries, now), {}, now, True)

    assert sent == []
    assert failures == 0
    printed = capsys.readouterr().out
    assert "WOULD SEND" in printed
    assert "Points feature count fell 14%." in printed


def test_a_dry_run_records_nothing_so_it_cannot_suppress_a_real_alert(sent):
    """Recording it would mark as sent something that was never sent, and the
    real alert would then be deduplicated away.

    The bucket used here refuses every write, so this also asserts that a dry
    run touches object storage no more than a listing does."""
    entries = [run("checks", at(19, 2, 6), "DATA_FAIL")]
    now = at(19, 3)

    state, _ = notify.poll(CONFIG, bucket_at(entries, now), {}, now, True)
    assert state == {}

    state, _ = notify.poll(CONFIG, bucket_at(entries, now), {}, now, False)
    assert count(sent, "DATA_FAIL") == 1


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_a_failed_send_is_retried_rather_than_recorded(monkeypatch):
    """Recording a send that failed loses the alert permanently, which is
    worse than sending it twice."""
    attempts = []

    def refuse(config, addresses, subject, body, html=""):
        attempts.append(subject)
        raise OSError("the relay is unreachable")

    monkeypatch.setattr(notify, "send_email", refuse)
    entries = [run("checks", at(19, 2, 6), "DATA_FAIL")]
    now = at(19, 3)

    state, failures = notify.poll(CONFIG, bucket_at(entries, now), {}, now, False)

    assert failures >= 1
    assert "status:checks" not in state
    assert attempts


def test_one_unreachable_recipient_does_not_stop_the_other_alert(monkeypatch):
    """The poll carries alerts for both jobs. A relay error on one must not
    swallow the other."""
    delivered = []

    def sometimes(config, addresses, subject, body, html=""):
        if "DATA_FAIL" in subject:
            raise OSError("the relay is unreachable")
        delivered.append(subject)

    monkeypatch.setattr(notify, "send_email", sometimes)
    entries = [
        run("checks", at(19, 2, 6), "DATA_FAIL"),
        run("backup", at(20, 4, 40), "SYSTEM_FAIL"),
    ]
    now = at(20, 6)

    state, failures = notify.poll(CONFIG, bucket_at(entries, now), {}, now, False)

    assert failures == 1
    assert any("SYSTEM_FAIL" in subject for subject in delivered)
    assert "status:backup" in state


# ---------------------------------------------------------------------------
# The state object
#
# The only thing this job writes. It lives in the bucket under notify/ rather
# than on the Jenkins host, because the Jenkins instance is a shared service
# this project does not administer and the job is not pinned to one agent - a
# state file on the agent that ran last hour is invisible to the one that runs
# next, and the symptom of that is the hourly flood the design exists to
# prevent.
# ---------------------------------------------------------------------------


def test_the_state_lives_beside_the_tiers_and_not_inside_one():
    """Nobody looking for a restore point should find this."""
    assert notify.state_key(CONFIG) == STATE_KEY
    for tier in ("rotating/", "monthly/", "yearly/"):
        assert not STATE_KEY.startswith(tier)


def test_a_missing_state_object_starts_from_nothing():
    """The first ever poll. One duplicate email per open condition is the
    accepted cost, and it happens once."""
    assert notify.load_state(bucket_at([], at(19, 3)), CONFIG) == {}


def test_a_storage_failure_is_not_read_as_an_empty_state():
    """The distinction that matters. 'There is no state object' means start
    fresh; 'the endpoint is unreachable' must not, or an outage would re-send
    every open alert the moment it cleared."""
    def refuse(Bucket, Key):
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    bucket = notify.Bucket(
        client=SimpleNamespace(get_object=refuse), name="gssgeodrive", prefix=PREFIX
    )
    with pytest.raises(ClientError):
        notify.load_state(bucket, CONFIG)


def test_an_unreadable_state_object_is_reported_and_not_fatal():
    """Failing every poll until somebody deletes an object by hand is silence,
    and silence is what this job exists to prevent."""
    def malformed(Bucket, Key):
        return {"Body": SimpleNamespace(read=lambda: b"{ this is not json")}

    bucket = notify.Bucket(
        client=SimpleNamespace(get_object=malformed), name="gssgeodrive", prefix=PREFIX
    )
    assert notify.load_state(bucket, CONFIG) == {}


def test_the_state_round_trips():
    written = {}
    bucket = bucket_at([], at(19, 3), written=written)
    signals = {"status:checks": {"value": "PASS", "roles": [], "sent_utc": "2026-08-19T03:00:00Z"}}

    notify.save_state(bucket, CONFIG, signals)

    assert list(written) == [PREFIX + STATE_KEY]
    assert notify.load_state(bucket, CONFIG) == signals


def test_the_state_survives_the_job_moving_between_agents(sent):
    """The reason it is in the bucket at all. Two consecutive polls that share
    nothing but the bucket must still deduplicate - which is what a Jenkins job
    on 'agent any' actually does."""
    entries = [run("checks", at(19, 2, 6), "DATA_FAIL", "Points feature count fell 14%.")]
    written = {}

    for hour in (3, 4, 5):
        bucket = bucket_at(entries, at(19, hour), written=written)
        # Nothing carried over in memory. Every poll rebuilds its state from
        # the bucket, the way a fresh agent and a fresh workspace would.
        state = notify.load_state(bucket, CONFIG)
        updated, failures = notify.poll(CONFIG, bucket, state, at(19, hour), False)
        assert failures == 0
        if updated != state:
            notify.save_state(bucket, CONFIG, updated)

    assert count(sent, "DATA_FAIL") == 1


# ---------------------------------------------------------------------------
# Reading the bucket
# ---------------------------------------------------------------------------


def test_folder_markers_and_the_prefix_placeholder_are_skipped(sent):
    """The bucket writes its own zero-byte folder markers into these prefixes.
    notify.py builds its own client, so it filters them itself rather than
    inheriting storage.list_keys' filter."""
    entries = [run("checks", at(19, 2, 6), "PASS")]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (20,)]
    markers = ("_$folder$", "status/_$folder$", "status/")
    now = at(19, 3)

    bucket = bucket_at(entries, now, extra_keys=markers)
    records = notify.list_status_records(bucket, CONFIG)

    assert [record.key for record in records] == [entries[0][1]]


def test_both_job_prefixes_are_read():
    """backup.parse_status_key deliberately matches 'checks-' only, so that
    promotion never reads a backup's own PASS as a data verdict. Here both
    matter, and an unknown prefix is still ignored."""
    assert notify.parse_status_key("status/checks-2026-08-19T02:06:00Z.json") == (
        "checks", at(19, 2, 6)
    )
    assert notify.parse_status_key("status/backup-2026-08-19T04:40:00Z.json") == (
        "backup", at(19, 4, 40)
    )
    assert notify.parse_status_key("status/check-2026-08-19T02:06:00Z.json") is None
    assert notify.parse_status_key("status/checks-not-a-timestamp.json") is None
    assert notify.parse_status_key("metrics/2026-08-19.json") is None


def test_the_status_key_format_matches_what_status_py_writes():
    """One contract written in two files, because jenkins/ shares no imports
    with the repository. This is what keeps the two copies in step."""
    import status

    key = status.status_key(
        {"storage": {"paths": {"status": STATUS_PATH}}}, status.CHECKS_JOB, at(19, 2, 6)
    )
    assert notify.parse_status_key(key) == ("checks", at(19, 2, 6))


# ---------------------------------------------------------------------------
# The contract with backup.py
#
# Two conditions are read out of a status object's details because neither has
# a status of its own. The marker strings are a contract with backup.py, and
# these tests build the real detail lines from that module so that a reworded
# message fails a test rather than silently unhooking an alert.
# ---------------------------------------------------------------------------


def empty_store():
    """A storage.Storage whose bucket is empty and refuses every write."""
    def refuse(*args, **kwargs):
        raise AssertionError("this test must not write to or delete from a bucket")

    client = SimpleNamespace(
        get_paginator=lambda name: SimpleNamespace(
            paginate=lambda **kwargs: [{"Contents": []}]
        ),
        put_object=refuse,
        copy_object=refuse,
        delete_object=refuse,
    )
    return storage.Storage(client=client, bucket="gssgeodrive", prefix=PREFIX)


def test_the_monthly_candidate_marker_matches_what_backup_py_writes():
    details = []
    backup.promote_monthly(empty_store(), CONFIG, "2026-08-18", details)

    matching = [line for line in details if notify.MONTHLY_CANDIDATE_MARKER in line]
    assert len(matching) == 1, details

    record = notify.StatusRecord(
        job="backup", moment=at(19, 4, 40), key="status/x.json",
        payload={"status": "PASS", "details": details},
    )
    assert notify.reports(record, notify.MONTHLY_CANDIDATE_MARKER)
    assert notify.unpromoted_month(CONFIG, record, at(19, 4, 40)) == "2026-08"


def test_the_marker_does_not_match_the_ordinary_not_promoted_yet_line():
    """Early in the month promotion simply has not happened yet, and every
    backup says so. Only the line past no_candidate_alert_days is an alert."""
    details = []
    backup.promote_monthly(empty_store(), CONFIG, "2026-08-05", details)

    assert details
    assert not any(notify.MONTHLY_CANDIDATE_MARKER in line for line in details)


def test_the_outside_bc_marker_matches_what_checks_py_writes():
    """The other half of the contract asserted in tests/test_checks.py, built
    from the real function rather than from a copy of its wording.

    checks.py deliberately keeps this out of its status, so the marker is the
    only thing that carries it to anybody. A rewording there with no rewording
    here would silence it again, which is the failure DESIGN.md 7.6.1 exists
    to have fixed."""
    finding = checks.outside_bc_finding(
        "points",
        {"features_outside_grid": 2, "objectids_outside_grid": [150984, 150985]},
    )
    record = notify.StatusRecord(
        job="checks", moment=at(19, 2, 6), key="status/x.json",
        payload={"status": "BASELINE", "details": ["compared against nothing", finding]},
    )

    assert notify.outside_bc_lines(record) == [finding]
    assert notify.outside_bc_signature([finding]) == "points 2"


def test_the_signature_reads_the_count_a_third_record_changes():
    """The dedup value, and the reason a new bad record is a second email
    while an unchanged backlog is not."""
    two, three = (
        checks.outside_bc_finding("points", {
            "features_outside_grid": count_outside,
            "objectids_outside_grid": [150984, 150985, 151900][:count_outside],
        })
        for count_outside in (2, 3)
    )
    assert notify.outside_bc_signature([two]) == "points 2"
    assert notify.outside_bc_signature([three]) == "points 3"


def test_the_signature_reads_a_count_written_with_thousands_separators():
    """Counts are written with separators everywhere in this project. A
    catastrophic reprojection is exactly when the alert must not degrade into
    the whole line as its own dedup value."""
    finding = checks.outside_bc_finding(
        "points", {"features_outside_grid": 53987, "objectids_outside_grid": [1, 2]},
    )
    assert notify.outside_bc_signature([finding]) == "points 53987"


def test_a_line_the_signature_cannot_parse_still_produces_one_email():
    """A reworded message should produce one email rather than none, and
    rather than one an hour. The line itself is stable while the situation
    is, so it stands in as the value."""
    reworded = "something is outside British Columbia, somehow"
    assert notify.outside_bc_signature([reworded]) == reworded


def test_the_outside_bc_finding_is_not_read_from_a_backup_run():
    """It is the check job that measures this. Reading it from whichever run
    was newest would report a stale finding on days the check did not run."""
    record = notify.StatusRecord(
        job="backup", moment=at(19, 4, 40), key="status/x.json",
        payload={"status": "PASS", "details": ["published rotating/2026-08-19"]},
    )
    assert notify.outside_bc_lines(record) == []


def test_the_prune_paused_marker_matches_what_backup_py_writes():
    details = []
    backup.prune(empty_store(), CONFIG, "DATA_FAIL", details)

    assert any(notify.PRUNE_PAUSED_MARKER in line for line in details), details


# ---------------------------------------------------------------------------
# Isolation, and the read-only rule
# ---------------------------------------------------------------------------


def notify_source():
    return (ROOT / "jenkins" / "notify.py").read_text(encoding="utf-8")


def test_notify_shares_no_imports_with_the_repository():
    """DESIGN.md 12. jenkins/ is isolated so the GIS server needs nothing
    installed beyond jenkins/requirements.txt, which is the whole argument for
    the hybrid design. This is the opposite of the Step 4 decision that gave
    backup.py and checks.py a shared status.py, and deliberately so."""
    imported = set()
    for node in ast.walk(ast.parse(notify_source())):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    # botocore comes with boto3 and is not a fourth thing to install.
    assert imported - set(sys.stdlib_module_names) == {"boto3", "botocore", "yaml"}
    for local in ("storage", "status", "backup", "checks", "arcgis", "pyogrio"):
        assert local not in imported


def bootstrap_source():
    """The fetch script embedded in the Jenkinsfile, pulled back out.

    It is written inline there rather than kept as a file of its own because
    the AGENT never checks the repository out. The Jenkinsfile itself does now
    come from source control, but by a lightweight checkout on the controller,
    which reads that one file and gives the agent no workspace to load a second
    one from. So whatever fetches the code still has to travel inside it.
    Extracting it here is what lets it be parsed and held to a contract like
    any other module.
    """
    text = (ROOT / "jenkins" / "Jenkinsfile").read_text(encoding="utf-8")
    opening = "writeFile file: 'fetch_code.py', text: '''"
    start = text.index(opening) + len(opening)
    return text[start:text.index("'''", start)]


def bootstrap_wanted():
    """The bootstrap's published-name -> local-path mapping, read from source."""
    for node in ast.walk(ast.parse(bootstrap_source())):
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", None) == "WANTED" for target in node.targets
        ):
            return {
                key.value: value.value
                for key, value in zip(node.value.keys, node.value.values)
            }
    raise AssertionError("the Jenkinsfile bootstrap has no WANTED mapping")


def test_the_bootstrap_fetches_exactly_what_the_workflow_publishes():
    """A contract between two files that cannot import each other.

    publish_notify_code.py runs in GitHub Actions and uploads under code/; the
    bootstrap runs on a Jenkins agent that cannot reach GitHub and downloads
    from there. Rename a published file on one side only and the notify job
    stops being able to start - with nothing in the error to say that the two
    halves disagree about what the object is called.

    Compared as a mapping rather than as a set of names, because the direction
    is the part that can go wrong: the publisher's local path is the
    bootstrap's destination, and its published name is the bootstrap's key.
    """
    import publish_notify_code

    assert bootstrap_wanted() == {
        published: local
        for local, published in publish_notify_code.PUBLISHED_FILES.items()
    }

    for local_path in publish_notify_code.PUBLISHED_FILES:
        assert (ROOT / local_path).exists(), local_path


def test_the_bootstrap_needs_nothing_from_the_repository():
    """It runs before anything has been downloaded, so it has to stand alone -
    boto3 and the standard library, and no import of the code it is fetching."""
    imported = set()
    for node in ast.walk(ast.parse(bootstrap_source())):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported - set(sys.stdlib_module_names) == {"boto3"}
    assert "notify" not in imported


def test_the_bootstrap_verifies_what_it_downloaded():
    """A truncated notify.py fails on import and is obvious. A truncated
    config.yml can still parse as valid YAML with whole sections missing, which
    would leave this job routing alerts by an incomplete table."""
    source = bootstrap_source()
    assert "sha256" in source
    assert "manifest" in source


def test_the_jenkins_requirements_hold_only_what_that_server_needs():
    lines = (ROOT / "jenkins" / "requirements.txt").read_text(encoding="utf-8").splitlines()
    pinned = [line.split("==")[0] for line in lines if line and not line.startswith("#")]
    assert pinned == ["boto3", "PyYAML", "tzdata"]


def test_notify_never_deletes_or_copies_in_the_bucket():
    """Not a permission boundary - the bucket issues one full-access key pair
    (DESIGN.md 6.6), so this job could destroy every backup it is pointed at.
    Held here instead."""
    called = {
        node.func.attr
        for node in ast.walk(ast.parse(notify_source()))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    forbidden = {
        "delete_object", "delete_objects", "copy_object", "upload_file",
        "upload_fileobj", "put_bucket_versioning", "create_multipart_upload",
        "put_bucket_lifecycle_configuration",
    }
    assert called & forbidden == set()


def test_the_only_write_is_the_state_object():
    """put_object is now allowed, and this is what keeps that from widening.

    Exactly one call, inside save_state, and save_state takes no key argument -
    so no caller can point it at a backup. Adding a second write, or giving
    that function a key parameter, fails here."""
    tree = ast.parse(notify_source())

    writers = [
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "put_object"
            for call in ast.walk(node)
        )
    ]
    assert writers == ["save_state"]

    save_state = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "save_state"
    )
    assert [argument.arg for argument in save_state.args.args] == [
        "bucket", "config", "signals",
    ]

    # And the destination is built by state_key, which config.yml points at a
    # prefix of its own rather than at any backup tier.
    keys = {
        node.func.id for node in ast.walk(save_state)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "state_key" in keys


def test_two_different_data_fails_are_two_emails(sent):
    """The drill case, 2026-09-02. A map cell emptied on one run and the
    feature count collapsed on the next. Both are DATA_FAIL, so until the rule
    names went into the value the second sent nothing and the reader was left
    believing nothing had changed since the first.

    DESIGN.md 8.5 still holds either side of this: the same failure repeating
    is one email, which the test below asserts.
    """
    entries = [
        run("checks", at(24, 2, 6), "DATA_FAIL", rules=["bin_disappeared"]),
        run("checks", at(25, 2, 6), "DATA_FAIL", rules=["bin_count_change", "feature_count"]),
    ]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (22, 25)]

    poll_hourly(entries, at(24, 3), at(26, 3))

    assert count(sent, "daily integrity check") == 2


def test_the_same_failure_repeating_is_still_one_email(sent):
    """The property the change had to keep. An unresolved incident compares
    against the same pre-incident baseline every day - previous_run skips
    DATA_FAIL days - so the same rules fire and the value does not move."""
    entries = [
        run("checks", at(day, 2, 6), "DATA_FAIL", rules=["feature_count"])
        for day in (24, 25, 26)
    ]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (22, 25)]

    poll_hourly(entries, at(24, 3), at(27, 3))

    assert count(sent, "daily integrity check") == 1


def test_a_status_object_written_before_rules_existed_still_compares(sent):
    """Every object already in the bucket predates the field. An absent
    `rules` reads as nothing broken, so no migration is needed and a poll
    across the boundary does not invent an email."""
    old = run("checks", at(24, 2, 6), "DATA_FAIL", rules=["feature_count"])
    del old[2]["rules"]
    entries = [old, run("checks", at(25, 2, 6), "DATA_FAIL", rules=["feature_count"])]
    entries += [run("backup", at(day, 4, 40), "PASS") for day in (22, 25)]

    poll_hourly(entries, at(24, 3), at(26, 3))

    # One for the first, one when the rules appear - the value genuinely
    # changed, and erring towards a duplicate beats erring towards silence.
    assert count(sent, "daily integrity check") == 2
