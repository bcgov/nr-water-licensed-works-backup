"""Known-answer tests for the status object and the key it is written to.

    python -m pytest tests -q

Two contracts are covered here, and the second one is the reason this file
exists.

The shape of the object, because jenkins/notify.py reads it directly and
carries no interpretation logic of its own: it reads `status`, looks up the
recipients in config.yml and sends `summary` as the email body. A field that
quietly changes name breaks the notifier on a day when something is already
wrong.

And the naming of the key, because backup.py pairs a rotating set with a
check by matching the 'checks-' prefix on a status key. A backup run's own
status object named checks-* would be read as a data check that passed, and
a corrupt set would be promoted to the monthly anchor. Nothing raises, no log
line looks wrong, and the anchor is what the rest of the month's trend
comparison is measured against.

Nothing here touches the network. The listing tests use a hand-written
stand-in for the boto3 client, in the same shape as test_storage.py's.
"""

import ast
import datetime
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backup
import checks
import status
import storage

PREFIX = "authorizations/backups/water_licensed_works/"

CONFIG = {
    "timezone": "America/Vancouver",
    "storage": {"paths": {"status": "status/", "metrics": "metrics/"}},
}

# 16:05 on a Vancouver afternoon, which is the slot the check job runs in.
AFTERNOON = datetime.datetime(2026, 8, 14, 23, 5, 0, tzinfo=datetime.timezone.utc)


def check_result(result_status="PASS", summary="Integrity check passed.", details=None):
    """A CheckResult, built the way run_checks returns one."""
    return checks.CheckResult(
        status=result_status,
        summary=summary,
        failures=[],
        details=details if details is not None else ["compared against 2026-08-13"],
        metrics={"lines": {"feature_count": 142523}},
        date_stamp="2026-08-14",
        code_version="e57da00",
    )


def backup_result(result_status="PASS", summary="Backup completed."):
    """A BackupResult, which carries fewer fields than a CheckResult."""
    return backup.BackupResult(
        status=result_status,
        summary=summary,
        details=["published rotating/2026-08-14/"],
        date_stamp="2026-08-14",
        code_version="e57da00",
    )


def store_holding(objects):
    """A Storage whose bucket holds exactly these relative keys and bodies.

    The stand-in answers get_paginator and get_object, which is all list_keys
    and read_bytes ask of the client. paginate honours the Prefix it is given
    rather than returning everything, so a test cannot pass because the fake
    was more generous than the bucket.
    """
    def paginate(**kwargs):
        wanted = kwargs.get("Prefix", "")
        return [{"Contents": [
            {"Key": PREFIX + key} for key in objects if (PREFIX + key).startswith(wanted)
        ]}]

    def get_object(Bucket, Key):
        body = json.dumps(objects[Key[len(PREFIX):]]).encode("utf-8")
        return {"Body": SimpleNamespace(read=lambda: body)}

    client = SimpleNamespace(
        get_paginator=lambda name: SimpleNamespace(paginate=paginate),
        get_object=get_object,
    )
    return storage.Storage(client=client, bucket="gssgeodrive", prefix=PREFIX)


# ---------------------------------------------------------------------------
# The object the notifier reads
# ---------------------------------------------------------------------------

def test_the_status_object_is_the_shape_the_notifier_reads():
    """DESIGN.md 8.3, field for field. Adding one here is adding one that
    jenkins/notify.py may come to depend on."""
    written = status.build_status(status.CHECKS_JOB, check_result(), AFTERNOON)

    assert set(written) == {
        "run_id", "job", "status", "timestamp_utc",
        "summary", "details", "code_version", "workflow_run_url",
    }
    assert written["run_id"] == "checks-2026-08-14T23:05:00Z"
    assert written["job"] == "checks"
    assert written["status"] == "PASS"
    assert written["timestamp_utc"] == "2026-08-14T23:05:00Z"
    assert written["code_version"] == "e57da00"


def test_the_summary_is_carried_through_and_not_rewritten():
    """It becomes the email body. checks.summarise already writes it for a
    non-technical reader, so status.py must not paraphrase it."""
    summary = (
        "Points feature count fell 14% (53,987 to 46,428). The backups are "
        "untouched and no data has been changed."
    )
    written = status.build_status(
        status.CHECKS_JOB, check_result("DATA_FAIL", summary), AFTERNOON
    )
    assert written["summary"] == summary
    assert written["status"] == "DATA_FAIL"


def test_a_backup_result_and_a_check_result_both_work():
    """status.py reads only the fields both dataclasses carry, so the two jobs
    need no separate paths through it."""
    from_backup = status.build_status(status.BACKUP_JOB, backup_result(), AFTERNOON)
    from_checks = status.build_status(status.CHECKS_JOB, check_result(), AFTERNOON)
    assert set(from_backup) == set(from_checks)
    assert from_backup["job"] == "backup"
    assert from_backup["details"] == ["published rotating/2026-08-14/"]


def test_the_failures_list_is_not_repeated_alongside_the_details():
    """run_checks puts every violation message into details already. Two lists
    of the same text is one the notifier would have to choose between."""
    result = check_result("DATA_FAIL", details=["The schema changed."])
    result.failures = ["The schema changed."]
    written = status.build_status(status.CHECKS_JOB, result, AFTERNOON)
    assert "failures" not in written
    assert written["details"] == ["The schema changed."]


def test_the_object_is_json_and_holds_no_measurements():
    """It is read hourly by a job whose whole point is that it stays small.
    The measurements have their own file under metrics/."""
    written = status.build_status(status.CHECKS_JOB, check_result(), AFTERNOON)
    text = json.dumps(written)
    assert "metrics" not in written
    assert len(text) < 4096


# ---------------------------------------------------------------------------
# workflow_run_url, which has to degrade to null
# ---------------------------------------------------------------------------

ACTIONS_VARIABLES = ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")


def test_the_workflow_url_is_built_from_the_three_actions_variables(monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "bcgov/nr-water-licensed-works-backup")
    monkeypatch.setenv("GITHUB_RUN_ID", "17204518890")
    assert status.workflow_run_url() == (
        "https://github.com/bcgov/nr-water-licensed-works-backup/actions/runs/17204518890"
    )


def test_off_actions_the_workflow_url_is_null(monkeypatch):
    """A hand run and the Phase 2 call from the NRIDS server set none of them.
    Null, rather than a URL assembled from whatever happened to be set, which
    would 404."""
    for name in ACTIONS_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    assert status.workflow_run_url() is None

    written = status.build_status(status.CHECKS_JOB, check_result(), AFTERNOON)
    assert written["workflow_run_url"] is None


def test_a_partial_actions_environment_is_still_null(monkeypatch):
    for name in ACTIONS_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "bcgov/nr-water-licensed-works-backup")
    assert status.workflow_run_url() is None


# ---------------------------------------------------------------------------
# The naming contract
#
# backup.parse_status_key matches 'checks-' and nothing else. Everything in
# this section is about that one prefix being right.
# ---------------------------------------------------------------------------

def test_the_key_is_the_run_id_and_cannot_drift_from_it():
    """The identifier inside the file and the name of the object are the same
    string by construction, so a reader who has one has the other."""
    key = status.status_key(CONFIG, status.CHECKS_JOB, AFTERNOON)
    written = status.build_status(status.CHECKS_JOB, check_result(), AFTERNOON)
    assert key == "status/checks-2026-08-14T23:05:00Z.json"
    assert key == f"status/{written['run_id']}.json"


def test_backup_parse_status_key_round_trips_a_check_key():
    """The other end of the contract. backup.py reads the time back out of the
    name to pair a check with the rotating set it covered."""
    key = status.status_key(CONFIG, status.CHECKS_JOB, AFTERNOON)
    assert backup.parse_status_key(key) == AFTERNOON


def test_a_sub_second_moment_still_produces_a_parseable_key():
    """utc_stamp truncates to whole seconds. A key carrying a fraction would
    parse as None and the run would be invisible to promotion."""
    moment = AFTERNOON.replace(microsecond=123456)
    key = status.status_key(CONFIG, status.CHECKS_JOB, moment)
    assert key == "status/checks-2026-08-14T23:05:00Z.json"
    assert backup.parse_status_key(key) == AFTERNOON


def test_a_backup_status_key_is_ignored_by_the_pairing():
    """The whole point of the job prefix. A backup PASS says the export
    worked, not that the data is good."""
    key = status.status_key(CONFIG, status.BACKUP_JOB, AFTERNOON)
    assert key == "status/backup-2026-08-14T23:05:00Z.json"
    assert backup.parse_status_key(key) is None


def test_a_backup_pass_is_not_read_as_a_passing_check():
    """The failure this guards against, end to end: promotion asks
    check_status_on whether the day's check passed. On a day when the backup
    ran and no check did, the answer must be None - not the backup's own
    PASS, which would promote an unchecked set to the monthly anchor."""
    store = store_holding({
        status.status_key(CONFIG, status.BACKUP_JOB, AFTERNOON): {"status": "PASS"},
    })
    assert backup.check_status_on(store, CONFIG, "2026-08-14") is None


def test_the_days_check_is_found_alongside_the_backup_status():
    """And when a check did run that day, its verdict is the one returned,
    whichever of the two objects was written last."""
    store = store_holding({
        status.status_key(CONFIG, status.BACKUP_JOB, AFTERNOON): {"status": "PASS"},
        status.status_key(
            CONFIG, status.CHECKS_JOB, AFTERNOON + datetime.timedelta(hours=1)
        ): {"status": "DATA_FAIL"},
    })
    assert backup.check_status_on(store, CONFIG, "2026-08-14") == "DATA_FAIL"


def test_the_prune_pause_reads_check_statuses_only():
    """latest_check_status pauses pruning while a data incident is open. A
    later backup PASS must not clear it - that would evict the last good copy
    while nobody is acting on the alert."""
    store = store_holding({
        status.status_key(CONFIG, status.CHECKS_JOB, AFTERNOON): {"status": "DATA_FAIL"},
        status.status_key(
            CONFIG, status.BACKUP_JOB, AFTERNOON + datetime.timedelta(days=2)
        ): {"status": "PASS"},
    })
    assert backup.latest_check_status(store, CONFIG) == "DATA_FAIL"


def test_a_check_status_is_paired_by_local_date_not_by_the_text_of_the_key():
    """A check at 19:00 in Vancouver is stamped 02:00Z the next day. Matching
    on the text of the key would file it under the wrong date and promote
    against the wrong day's verdict."""
    late = datetime.datetime(2026, 8, 15, 2, 0, 0, tzinfo=datetime.timezone.utc)
    store = store_holding({
        status.status_key(CONFIG, status.CHECKS_JOB, late): {"status": "PASS"},
    })
    assert backup.check_status_on(store, CONFIG, "2026-08-14") == "PASS"
    assert backup.check_status_on(store, CONFIG, "2026-08-15") is None


def test_an_unknown_job_name_is_refused():
    """Both directions of the typo are silent at run time. 'check-' would be
    invisible to promotion for ever; 'checks' on a backup would promote
    unchecked data."""
    for bad_job in ("check", "backups", "Checks", "", "checks "):
        with pytest.raises(ValueError, match="not a job name"):
            status.run_id(bad_job, AFTERNOON)
        with pytest.raises(ValueError, match="not a job name"):
            status.status_key(CONFIG, bad_job, AFTERNOON)


def test_the_two_job_names_are_the_complete_set():
    assert status.JOBS == ("backup", "checks")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def test_write_status_writes_the_object_at_the_key_it_returns():
    written = {}
    client = SimpleNamespace(
        put_object=lambda Bucket, Key, Body: written.update({Key: Body})
    )
    store = storage.Storage(client=client, bucket="gssgeodrive", prefix=PREFIX)

    key = storage.full_key(
        store, status.write_status(store, CONFIG, status.CHECKS_JOB, check_result())
    )
    payload = json.loads(written[key])

    assert key.startswith(PREFIX + "status/checks-")
    assert payload["status"] == "PASS"
    assert key == PREFIX + f"status/{payload['run_id']}.json"


def test_a_storage_failure_is_raised_rather_than_reported():
    """The entry points log it and fail the step. Swallowing it here would
    turn a run the notifier never hears about into one that looks fine."""
    def refuse(Bucket, Key, Body):
        raise OSError("the endpoint is unreachable")

    store = storage.Storage(
        client=SimpleNamespace(put_object=refuse), bucket="gssgeodrive", prefix=PREFIX
    )
    with pytest.raises(OSError):
        status.write_status(store, CONFIG, status.CHECKS_JOB, check_result())


# ---------------------------------------------------------------------------
# The import direction
# ---------------------------------------------------------------------------

def module_imports(name):
    """Top-level module names imported by one file, read from the parsed
    source so that naming any of them in a comment does not fail a test."""
    source = (Path(__file__).resolve().parent.parent / name).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def test_status_imports_nothing_but_storage_and_the_standard_library():
    """This is what lets checks.py import status.py at all. If status.py ever
    imports backup.py, checks.py inherits pyogrio and the Phase 2 call from
    the NRIDS server needs GDAL to run a few queries."""
    imported = module_imports("status.py")
    assert "backup" not in imported
    assert "checks" not in imported
    assert imported - set(sys.stdlib_module_names) == {"storage"}


def test_both_jobs_take_the_shared_helpers_from_status():
    """Not duplicated any more. The helpers are load-bearing in both files -
    utc_stamp writes the status key that backup.parse_status_key reads back,
    and schema_fingerprint is written by one job and compared by the other."""
    shared = {
        "safe_reason", "resolve_code_version", "utc_now", "utc_stamp",
        "local_date_stamp", "schema_fingerprint",
    }
    for name in ("backup.py", "checks.py"):
        source = (Path(__file__).resolve().parent.parent / name).read_text(encoding="utf-8")
        imported_from_status = {
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module == "status"
            for alias in node.names
        }
        assert imported_from_status == shared, name
        defined = {
            node.name for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef)
        }
        assert defined & shared == set(), f"{name} still defines its own copy"
