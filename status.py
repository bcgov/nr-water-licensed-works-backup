"""The status object every run writes, and the helpers both jobs share.

A backup or check run writes one status object at the end, pass or fail, to
status/<job>-<timestamp>.json. A Jenkins job on the GIS server polls that
prefix hourly, reads `status`, picks the recipients for it from the routing
table in config.yml and sends the mail (DESIGN.md 8.1). All judgement stays
in checks.py: nothing here interprets a result, it carries one across.

    summary becomes the body of that email

and is read by someone who does not work with the pipeline. BackupResult and
CheckResult are already written that way, so their summary is carried through
as it stands rather than reworded here.

This module also holds the small helpers backup.py and checks.py both need.
They are here because checks.py must not import backup.py: that module pulls
in pyogrio to read a File Geodatabase, and a GDAL dependency has no business
on the NRIDS server just to run a few queries in Phase 2. status.py is the
one module both jobs can safely import, which is what makes it the place for
anything they share.

That only holds while this file imports nothing but storage.py and the
standard library. If it ever imports backup.py, checks.py inherits pyogrio
through the back door and Phase 2 breaks - tests/test_status.py asserts it.

Needs the object storage credentials, and reads config.yml for the status
prefix. Writing is the caller's decision: run_backup.py and run_checks.py
each write one, and run_checks() itself does not, so the Phase 2 gate can
call it without publishing a status object nobody asked for.
"""

import datetime
import json
import os
import subprocess
from zoneinfo import ZoneInfo

import storage

# The two job names, and the whole of the set. A contract, not a label.
#
# backup.parse_status_key matches keys beginning 'checks-' and nothing else,
# so that backup.check_status_on and backup.latest_check_status see check
# statuses only. Those two decide monthly promotion - promote only a set whose
# paired check was PASS - and the pruning pause on DATA_FAIL.
#
# So a backup run's own status object must never be named checks-*. Its PASS
# means the export worked, not that the data is good, and mistaken for a check
# it would promote a corrupt set to the monthly anchor that the whole month's
# trend comparison is then measured against. Named backup-*, both functions
# correctly ignore it.
#
# The reverse typo is just as quiet. A check status written as 'check-...'
# would be invisible to parse_status_key, check_status_on would return None
# every day, and nothing would ever be promoted. Neither failure raises
# anything or looks wrong in a log, which is why run_id refuses a job name it
# does not recognise rather than trusting the caller.
BACKUP_JOB = "backup"
CHECKS_JOB = "checks"
JOBS = (BACKUP_JOB, CHECKS_JOB)


# ---------------------------------------------------------------------------
# Shared with backup.py and checks.py
#
# Pure standard library, which is what lets both import them from here
# without either importing the other. An edit to one of these is now an edit
# in one place - before Step 4 they were duplicated in both modules.
# ---------------------------------------------------------------------------


def safe_reason(exc):
    """What to say about a failure in a log or an alert.

    The errors this project raises deliberately are written for a maintainer
    to read and are safe to repeat. Anything else is reported by type only:
    this repository is public, its workflow logs are world readable, and
    GitHub masks a secret only on an exact string match - it will not catch a
    token embedded in a URL inside an arcgis or botocore message. preflight.py
    is the tool for getting at the detail, run from a machine where the output
    is private.
    """
    if isinstance(exc, (ValueError, TimeoutError, RuntimeError)):
        return str(exc)
    return f"{type(exc).__name__} - run preflight.py on a private machine for detail"


def resolve_code_version():
    """Which code produced this run, for the manifest, the metrics file and
    the status object.

    The whole purpose is to let a future reader tell "the data changed" from
    "our code changed" (DESIGN.md 7.7). Actions sets GITHUB_SHA to the commit
    being run; a run from someone's machine falls back to git.

    The -dirty suffix is the part that earns its place. With uncommitted
    edits in the tree HEAD still names the last commit, but what actually ran
    was that commit plus changes nobody can reconstruct afterwards. A hand run
    during the baseline period with a half-finished edit would otherwise
    produce an authoritative-looking record that quietly poisons the
    distribution the thresholds are derived from. Marked, it can be excluded -
    which is what the two metrics files deleted on 2026-08-17 were (DESIGN.md
    13).
    """
    actions_sha = os.getenv("GITHUB_SHA")
    if actions_sha:
        return actions_sha[:7]

    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        uncommitted = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        # Not a git checkout, or git is not on the path - the normal case for
        # the Phase 2 call from the NRIDS server. Worth recording as unknown
        # rather than failing a run over it.
        return "unknown"

    return f"{head}-dirty" if uncommitted else head


def utc_now():
    """Everything is stored and compared in UTC."""
    return datetime.datetime.now(datetime.timezone.utc)


def utc_stamp(moment):
    """ISO 8601 with a trailing Z - the form used in every key and record.

    backup.parse_status_key reads a status key back with this same format, so
    the two are one contract written in two places and have to stay in step.
    """
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def local_date_stamp(config):
    """Today's date in the configured timezone, e.g. '2026-08-14'.

    Local rather than UTC because this names both the dated backup prefix and
    the day's metrics file, and decides which calendar month a set is promoted
    into. A run at 19:00 on a Vancouver Friday is already Saturday in UTC,
    which would file it under the wrong day and, on the last day of a month,
    the wrong month.

    Both jobs take it from here rather than each stamping its own, because
    promotion pairs a rotating set with the check that covered it by date.
    """
    return datetime.datetime.now(ZoneInfo(config["timezone"])).strftime("%Y-%m-%d")


def schema_fingerprint(layer_properties):
    """A comparable summary of one layer's schema.

    Recorded per layer in the manifest by backup.py and in the metrics file by
    checks.py, and compared by the check job's exact_match rule - the one
    threshold in config.yml that is not a placeholder, because any schema
    change should be looked at. One implementation, so a manifest fingerprint
    and a metrics fingerprint describe the same thing by construction rather
    than by two copies being kept in step.

    Sorted throughout so that the service reordering its own JSON is not
    mistaken for a change.

    Per layer, never shared: points has no DISPLAY_COLOUR, and although both
    domains are named LWL_FCODES they are different domains with
    non-overlapping code sets.
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
# The status object
# ---------------------------------------------------------------------------


def run_id(job, moment):
    """The identifier for one run: 'checks-2026-08-14T23:05:00Z'.

    Also the name of the object it is written to, so the identifier inside the
    file and the key it lives at cannot drift apart. See status_key.
    """
    if job not in JOBS:
        raise ValueError(
            f"'{job}' is not a job name. It must be one of {', '.join(JOBS)}. "
            f"backup.py pairs a rotating set with its check by matching the "
            f"'{CHECKS_JOB}-' prefix on a status key, so a job named anything "
            f"else is either invisible to promotion or mistaken for a check."
        )
    return f"{job}-{utc_stamp(moment)}"


def status_key(config, job, moment):
    """The key one status object is written to: status/<run_id>.json.

    The timestamp in the name is UTC. backup.check_status_on converts it back
    to the configured zone to pair a check with a rotating set, rather than
    matching on the text of the key: a check that ran at 16:05 in Vancouver is
    stamped 23:05Z the same day, but one at 19:00 is stamped 02:00Z the next.
    """
    return f"{config['storage']['paths']['status']}{run_id(job, moment)}.json"


def workflow_run_url():
    """A link to the Actions run that produced this status, or None.

    Actions sets all three of these on every job, and the notify job puts the
    link in the email so that a developer can go straight to the log.

    A run started by hand, and the Phase 2 call from the NRIDS server, set
    none of them - hence null rather than a URL that would 404, and hence all
    three being required rather than a URL assembled from whichever of them
    happens to be present.
    """
    server = os.getenv("GITHUB_SERVER_URL")
    repository = os.getenv("GITHUB_REPOSITORY")
    actions_run_id = os.getenv("GITHUB_RUN_ID")
    if not (server and repository and actions_run_id):
        return None
    return f"{server}/{repository}/actions/runs/{actions_run_id}"


def build_status(job, result, moment):
    """The status object for one finished run, as DESIGN.md 8.3.

    Takes either a BackupResult or a CheckResult - the fields read here are
    the ones both carry.

    CheckResult.failures is deliberately not copied in. run_checks already
    puts every violation message into details, so a separate list would be the
    same text twice and the notify job would have to decide which of the two
    to believe when they disagreed. CheckResult.metrics is not copied either:
    the measurements have their own file under metrics/, and this object is
    read hourly by a job whose whole point is that it stays small.

    `rules` is the exception to that, added 2026-09-02, and it is the names
    only. The drill found that two genuinely different failures - a map cell
    emptied on one run, the feature count collapsing on the next - are
    indistinguishable to the notify job, which deduplicates on the status and
    therefore sent nothing for the second. The names are what tell them apart,
    and putting them here rather than parsing them back out of the prose in
    `details` is the difference between a contract and a guess. They carry no
    numbers, so they do not move while an incident is open, which is what
    keeps this from becoming a daily email. DESIGN.md 8.5.

    The notify job reads this shape directly, so a field added here is a field
    jenkins/notify.py may come to depend on.
    """
    return {
        "run_id": run_id(job, moment),
        "job": job,
        "status": result.status,
        "timestamp_utc": utc_stamp(moment),
        "summary": result.summary,
        "details": list(result.details),
        "rules": broken_rules(result),
        "code_version": result.code_version,
        "workflow_run_url": workflow_run_url(),
    }


def broken_rules(result):
    """The names of the rules a check run broke, sorted and without repeats.

    Names only, and taken from the structured failures in the metrics rather
    than from the prose: `feature_count` fires against the previous run and
    against the trend median in the same run, and both are the same rule for
    the purpose of asking whether the character of a failure has changed.

    Empty for a backup run, which measures nothing about the data, and for any
    result whose metrics never got as far as being collected - a SYSTEM_FAIL
    returns before that, and an absent field reads the same as no rules broken,
    which for a run that reached no verdict is the honest answer.
    """
    metrics = getattr(result, "metrics", None) or {}
    return sorted({
        failure["rule"]
        for failure in metrics.get("failures", [])
        if isinstance(failure, dict) and failure.get("rule")
    })


def write_status(store, config, job, result):
    """Write the status object for one finished run and return its key.

    Called after every run, whatever the outcome, because silence is what the
    staleness rule in DESIGN.md 8.2 turns into an alert: an expected slot
    passing with nothing new under status/ means the pipeline has stopped, and
    that is only true if a run that did happen always leaves one.

    Which is also why this raises rather than handling a problem of its own.
    If the run failed because object storage was unreachable then this write
    fails too and no object appears - the designed behaviour, and the caller's
    to log without losing the result it was reporting.
    """
    moment = utc_now()
    key = status_key(config, job, moment)
    payload = build_status(job, result, moment)
    # A few hundred bytes of JSON with nothing on disk to stream, so
    # write_bytes rather than the temporary-file dance upload_file needs.
    # storage.py logs every write with the full key, so this does not repeat it.
    storage.write_bytes(
        store, key, json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    )
    return key
