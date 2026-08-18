"""Alert mail for the Water Licensed Works backup and check jobs.

Runs hourly on the GIS server under Jenkins. One poll reads the status
objects the GitHub Actions jobs wrote to object storage, works out what has
changed since the last poll, and sends mail through the internal SMTP relay.

    python notify.py --dry-run     report what would be sent, send nothing
    python notify.py               send it

GitHub-hosted runners cannot reach the relay and the GIS server can, which is
the whole reason this job exists on a second platform (DESIGN.md 8.1). Actions
does all the substantive work and writes a status object every run; this job
reads `status`, looks the recipients up in config.yml, and sends `summary` as
the body. All judgement stays in checks.py - with one deliberate exception,
marked where it happens: no_monthly_candidate is reported in the details of a
run whose status is PASS, and PASS routes to nobody.

READ-ONLY AGAINST THE BUCKET. This job lists and reads under the status/
prefix and writes nothing anywhere. It cannot be *restricted* to that - the
bucket issues one full-access key pair (DESIGN.md 6.6) - so it is a convention
held in code rather than a permission boundary. There is no put, copy or
delete call in this file and tests/test_notify.py asserts it stays that way.

THIS FILE SHARES NO IMPORTS WITH THE REST OF THE REPOSITORY, AND THAT
DUPLICATION IS DELIBERATE. It does not import storage.py, status.py,
backup.py or checks.py. It builds its own boto3 client, joins the project
prefix itself, and carries its own copies of the few small helpers it needs -
the folder-marker filter, the status key parser, the timestamp format. Please
do not "fix" it by importing storage.py: jenkins/ is isolated so that the GIS
server needs nothing installed beyond jenkins/requirements.txt, and the
argument for the hybrid design is precisely that this job needs no arcgis, no
arcpy and no GDAL (DESIGN.md 8.1 and 12). The cost is three short functions
kept in step by hand; the alternative is an ArcGIS Pro dependency chain on a
server that only has to poll and send mail.

THE FAILURE MODE THIS FILE IS WRITTEN AGAINST IS A FLOOD OF EMAIL. Polling
hourly does not mean emailing hourly: the check job writes one status object
a day and mail goes out only when the situation changes, so a DATA_FAIL that
takes five days to fix produces two emails - one alert, one resolution -
whether the poll is hourly or daily (DESIGN.md 8.5). Everything here that
could get that wrong is deduplicated through one mechanism, described at
`collect_notifications`. The rule that matters: dedup is keyed on the job and
the situation, never on a run_id or a timestamp. Every poll sees a new run_id
once a day, so keying on that emails daily.

Needs the object storage credentials (S3_NRS_ENDPOINT, S3_GSS_GEODRIVE_KEY_ID,
S3_GSS_GEODRIVE_SECRET_KEY), SMTP_HOST and SMTP_SENDER, and one ALERT_*
variable per recipient role named in config.yml. All of them live on the
Jenkins host and none of them are in this repository.
"""

import argparse
import datetime
import json
import logging
import os
import smtplib
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
import yaml

logger = logging.getLogger("notify")

# config.yml sits at the repository root, one level above jenkins/. Resolved
# from this file rather than from the working directory so that the Jenkins
# job can run it from anywhere in the workspace.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yml"

# Where the dedup state is remembered between polls, and the only thing this
# job writes anywhere. It must NOT live in the Jenkins workspace: a wiped or
# fresh workspace looks like "nothing has ever been notified", and every open
# condition mails again. Nor in JENKINS_HOME, which belongs to whoever
# administers that shared service. The Jenkinsfile sets NOTIFY_STATE_FILE to a
# directory the project owns on the GIS server. The fallback below is the home
# directory of whoever ran it by hand, for the same durability reason.
# DESIGN.md 8.5.
STATE_FILE_VARIABLE = "NOTIFY_STATE_FILE"
DEFAULT_STATE_PATH = Path.home() / ".water-licensed-works" / "notify-state.json"

# Bumped if the shape of the state file changes, so a future reader can tell
# an old file from a corrupt one.
STATE_VERSION = 1

# The two job names, and the whole of the set. Same contract as status.py -
# duplicated here rather than imported, see the module docstring.
BACKUP_JOB = "backup"
CHECKS_JOB = "checks"
JOBS = (BACKUP_JOB, CHECKS_JOB)

JOB_LABELS = {BACKUP_JOB: "backup", CHECKS_JOB: "daily integrity check"}

# The timestamp format in every status key, from status.utc_stamp. The two are
# one contract written in two places.
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Zero-byte objects the storage backend writes to stand for a folder. Filtered
# here for the same reason storage.list_keys filters them: they are the
# bucket's bookkeeping, not ours, and a caller that does not know about them
# logs a warning about an unreadable name on every single poll.
FOLDER_MARKER_SUFFIX = "_$folder$"

# The five statuses in DESIGN.md 7.1 are the complete set and the routing
# table in config.yml has a row for each. These three are the ones a return to
# PASS resolves, so that whoever was told about the problem is told it is
# over. BASELINE is deliberately absent: BASELINE to PASS is the first run
# followed by the second, not an incident clearing.
RESOLVING_STATUSES = ("WARN", "DATA_FAIL", "SYSTEM_FAIL")

# Two conditions this job has to read out of a status object's details,
# because neither has a status of its own.
#
# The first is the exception to "all judgement stays in checks.py" flagged in
# the module docstring. backup.promote_monthly logs it against a run whose
# status is PASS, and PASS routes to nobody, so without this the alert that
# promotion.no_candidate_alert_days has been asking for since the beginning
# reaches no one - configured, logged, written into the status object and
# unreachable, which is exactly what happened on the first production backup
# (DESIGN.md 8.4).
#
# Both strings are a contract with backup.py. If the message there is
# reworded, reword it here too - tests/test_notify.py builds the real detail
# lines from backup.py and asserts these still match, so the contract fails a
# test rather than failing silently in a month's time.
MONTHLY_CANDIDATE_MARKER = "days in with nothing promoted to the monthly tier"
PRUNE_PAUSED_MARKER = "pruning paused"

# How far back the episode walks read. An episode longer than this is
# truncated, which is harmless: the walks are only ever asked whether an
# episode has lasted *at least* the configured number of days, and a truncated
# one is longer still. Deduplication does not depend on where an episode
# started, for exactly this reason.
EPISODE_LOOKBACK_DAYS = 14

# The weekly summary reports the last seven days.
SUMMARY_DAYS = 7

# Slots older than this are not worth asking about: if the pipeline has been
# down for a fortnight the alert went out on day one.
SLOT_LOOKBACK_DAYS = 14

# Indexed by datetime.weekday(), so that reading a day name out of config.yml
# does not depend on the machine's locale the way strftime('%A') does.
WEEKDAY_NAMES = (
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
)

SUBJECT_PREFIX = "[Water Licensed Works]"

# Every message ends with this. The recipients include people who do not work
# on the pipeline, and the first question an alert raises is "has something
# been done to my data" - to which the answer is always no.
FOOTER = (
    "Nothing has been changed in ArcGIS Online and no backup has been deleted.\n"
    "This pipeline only reads and reports. Recovering data is a decision for\n"
    "the data owner and is carried out by hand.\n"
    "\n"
    "Sent by the Water Licensed Works notification job on the GIS server."
)


@dataclass
class Bucket:
    """A boto3 client bound to the bucket and prefix this job may read.

    The three travel together so that every read below joins the project
    prefix on without the caller passing it. gssgeodrive is shared with other
    GSS projects and this job has no business anywhere outside its own prefix.
    """

    client: object
    name: str
    prefix: str


@dataclass
class StatusRecord:
    """One status object: what its key says, and its contents once read.

    payload stays None for objects that were listed but not read. The listing
    alone answers "did a run happen after the expected slot", which is the
    staleness question, so most polls read two objects and list the rest.
    """

    job: str
    moment: datetime.datetime
    key: str
    payload: dict = None


@dataclass
class Notification:
    """One email, and what has to change before it is sent again.

    key and value are the whole of the deduplication. key names a situation -
    'status:checks', 'prune_paused' - and value is what that situation
    currently is. Mail goes out when value differs from the value recorded for
    that key on a previous poll, and at no other time.

    Neither may carry a run_id or a timestamp. Every poll sees a new run_id
    once a day, so a key or a value built from one turns a five-day incident
    into five emails, and a threshold re-evaluated on every poll into one an
    hour. DESIGN.md 8.5.
    """

    key: str
    value: str
    roles: list
    subject: str
    body: str


# ---------------------------------------------------------------------------
# Object storage
#
# A private copy of the three things this job needs from storage.py, which it
# deliberately does not import. See the module docstring before consolidating.
# ---------------------------------------------------------------------------


def connect_to_bucket(config):
    """Build the boto3 client from the environment, bound to the project prefix.

    endpoint_url is not optional. This is NRS S3-compatible storage, not AWS:
    left unset, boto3 resolves an AWS endpoint instead and fails somewhere
    that reads like a credentials problem rather than a missing setting.
    """
    endpoint = os.getenv("S3_NRS_ENDPOINT")
    key_id = os.getenv("S3_GSS_GEODRIVE_KEY_ID")
    secret_key = os.getenv("S3_GSS_GEODRIVE_SECRET_KEY")

    missing = [
        name
        for name, value in [
            ("S3_NRS_ENDPOINT", endpoint),
            ("S3_GSS_GEODRIVE_KEY_ID", key_id),
            ("S3_GSS_GEODRIVE_SECRET_KEY", secret_key),
        ]
        if not value
    ]
    if missing:
        raise ValueError(
            f"Object storage settings are missing from the environment: "
            f"{', '.join(missing)}. On the Jenkins host they come from the "
            f"credentials store through withCredentials in the Jenkinsfile - "
            f"check the credential IDs there match the ones in Jenkins."
        )

    prefix = config["storage"]["prefix"]
    # Without the trailing slash the prefix concatenates onto the first key
    # segment - 'water_licensed_worksstatus/...' - which is a perfectly valid
    # key that nobody would ever go looking for.
    if not prefix.endswith("/"):
        prefix += "/"

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret_key,
    )
    return Bucket(client=client, name=config["storage"]["bucket"], prefix=prefix)


def parse_status_key(key):
    """(job, moment) read out of a status key, or None if it is not one.

    Keys look like 'status/checks-2026-08-14T23:05:00Z.json'.

    This reads both job prefixes, which backup.parse_status_key deliberately
    does not - that one matches 'checks-' only, so that monthly promotion sees
    check verdicts and never a backup run's own PASS. Here both matter: the
    check job's status is the data verdict and the backup job's is whether
    the snapshot was taken at all.
    """
    name = key.rsplit("/", 1)[-1]
    if not name.endswith(".json"):
        return None
    job, separator, stamp = name[: -len(".json")].partition("-")
    if not separator or job not in JOBS:
        return None
    try:
        moment = datetime.datetime.strptime(stamp, STAMP_FORMAT)
    except ValueError:
        return None
    return job, moment.replace(tzinfo=datetime.timezone.utc)


def list_status_records(bucket, config):
    """Every status object in the bucket, oldest first, unread.

    list_objects_v2 returns at most 1,000 keys per call without saying so - no
    error, just a short answer - hence the paginator. status/ passes 1,000
    objects after about nine months of daily checks and thrice-weekly backups,
    so this is not hypothetical.
    """
    status_path = config["storage"]["paths"]["status"]
    records = []
    paginator = bucket.client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket.name, Prefix=bucket.prefix + status_path):
        for entry in page.get("Contents", []):
            relative = entry["Key"][len(bucket.prefix):]
            # The 0-byte placeholder whose key is the prefix itself, the
            # sub-folder placeholders ending in '/', and the backend's own
            # folder markers. None of them are ours and none are status
            # objects.
            if not relative or relative.endswith("/"):
                continue
            if relative.endswith(FOLDER_MARKER_SUFFIX):
                continue
            parsed = parse_status_key(relative)
            if parsed is None:
                logger.warning(
                    "Ignoring an object under %s whose name is not "
                    "<backup|checks>-<UTC timestamp>.json: %s",
                    status_path, relative,
                )
                continue
            job, moment = parsed
            records.append(StatusRecord(job=job, moment=moment, key=relative))

    return sorted(records, key=lambda record: record.moment)


def read_status_object(bucket, key):
    """One status object, in the shape DESIGN.md 8.3 fixes."""
    response = bucket.client.get_object(Bucket=bucket.name, Key=bucket.prefix + key)
    return json.loads(response["Body"].read().decode("utf-8"))


def load_history(bucket, records, now):
    """Read the status objects the rules need, and return them oldest first.

    Not all of them. The rules look at the newest run of each job, at the
    unbroken episode of failures behind it, and at the last week for the
    summary, so a fortnight of history covers every question asked - roughly
    twenty small objects an hour rather than the whole archive.

    The newest run of each job is read whatever its age, so that a pipeline
    that stopped a month ago still reports the status it stopped on rather
    than looking like a job that has never run.
    """
    horizon = now - datetime.timedelta(days=EPISODE_LOOKBACK_DAYS)
    wanted = [record for record in records if record.moment >= horizon]

    for job in JOBS:
        for job_record in reversed(records):
            if job_record.job == job:
                if job_record not in wanted:
                    wanted.append(job_record)
                break

    for record in wanted:
        record.payload = read_status_object(bucket, record.key)

    return sorted(
        (record for record in wanted if record.payload is not None),
        key=lambda record: record.moment,
    )


# ---------------------------------------------------------------------------
# Reading the history
# ---------------------------------------------------------------------------


def records_for(history, job):
    """One job's status objects, oldest first."""
    return [record for record in history if record.job == job]


def newest_for(history, job):
    """One job's most recent status object, or None if it has never run."""
    found = records_for(history, job)
    return found[-1] if found else None


def status_of(record):
    """The status a record reports, or None if the object is malformed.

    A status object missing its own status field should not take the poll
    down: the other job's alert still needs to go out.
    """
    return record.payload.get("status")


def reports(record, marker):
    """Does this run's details carry the marker for one of the two conditions
    that have no status of their own? See MONTHLY_CANDIDATE_MARKER."""
    return any(marker in str(line) for line in record.payload.get("details", []))


def system_fail_episode(history, job):
    """The unbroken run of SYSTEM_FAILs at the end of one job's history.

    Empty unless the most recent run failed, which is what makes it an
    episode rather than a count: a SYSTEM_FAIL that cleared and came back is
    a new incident and gets its own alert.
    """
    episode = []
    for record in reversed(records_for(history, job)):
        if status_of(record) != "SYSTEM_FAIL":
            break
        episode.append(record)
    return list(reversed(episode))


def prune_paused_episode(history):
    """The unbroken run of backups that reported pruning paused.

    Pruning pauses while the most recent check status is DATA_FAIL, so that a
    live incident cannot quietly evict the last good copy (DESIGN.md 6.4).
    Past retention.paused_prune_alert_days that becomes an alert of its own.

    A backup that failed before it reached the prune step reported nothing
    either way, so it neither extends the episode nor ends it. Treating it as
    the end would restart the clock and send a second email for one unresolved
    incident.
    """
    episode = []
    for record in reversed(records_for(history, BACKUP_JOB)):
        if reports(record, PRUNE_PAUSED_MARKER):
            episode.append(record)
            continue
        if status_of(record) == "SYSTEM_FAIL":
            continue
        break
    return list(reversed(episode))


def episode_days(episode, now):
    """How long an episode has been running, in days.

    Measured from the first run in it, so a threshold of three days means
    three days of failing runs rather than three runs.
    """
    if not episode:
        return 0.0
    return (now - episode[0].moment).total_seconds() / 86400.0


# ---------------------------------------------------------------------------
# The expected slots
#
# DESIGN.md 8.2. Staleness is measured from the slot a run was DUE in, never
# as hours since the last run: the Friday-to-Monday backup gap is 72 hours by
# design and hours-since would false-alarm every weekend.
# ---------------------------------------------------------------------------


def expected_slots(config, job, now):
    """Every moment this job's cron has fired recently, as UTC instants.

    The cron expressions are UTC and this reads them as UTC, which is what
    makes the arithmetic exact across daylight saving rather than an hour out
    for half the year. schedule.checks_cron_utc_hour and
    schedule.backup_cron_utc_hour hold the hour from the workflow files.

    The backup's day filter is the part worth reading twice.
    schedule.backup_days holds LOCAL days - monday, wednesday, friday - while
    the cron's own day list is UTC Tue/Thu/Sat, one ahead, because 21:00 on a
    Vancouver Monday is already 04:00 Tuesday in UTC. So a candidate slot is
    kept when the LOCAL weekday of that UTC instant is one of the configured
    days, which reproduces the cron exactly without this file having to
    restate the offset.
    """
    schedule = config["schedule"]
    zone = ZoneInfo(config["timezone"])

    if job == CHECKS_JOB:
        if not schedule["checks_daily"]:
            return []
        hour = schedule["checks_cron_utc_hour"]
        local_days = None
    else:
        hour = schedule["backup_cron_utc_hour"]
        local_days = [str(day).lower() for day in schedule["backup_days"]]

    slots = []
    for days_back in range(SLOT_LOOKBACK_DAYS, -1, -1):
        utc_day = (now - datetime.timedelta(days=days_back)).date()
        slot = datetime.datetime(
            utc_day.year, utc_day.month, utc_day.day, hour,
            tzinfo=datetime.timezone.utc,
        )
        if local_days is not None:
            local_weekday = WEEKDAY_NAMES[slot.astimezone(zone).weekday()]
            if local_weekday not in local_days:
                continue
        slots.append(slot)
    return slots


def grace_hours(config, job):
    """How late a run may be before its slot counts as missed.

    Six hours on the backup because a run is two sequential exports and the
    status object is written at the end of it; two on the check, which takes
    about six minutes. The two-hour figure is only meaningful because this job
    polls hourly - a job that looked once a day could not enforce it at all
    (DESIGN.md 8.5), so do not weaken either without the other.
    """
    schedule = config["schedule"]
    return schedule["backup_grace_hours"] if job == BACKUP_JOB else schedule["checks_grace_hours"]


def last_due_slot(config, job, now):
    """The most recent slot whose grace period has also passed, or None.

    None when the job has no slot in the lookback window - a checks_daily of
    false, or a backup_days list that has just been changed.
    """
    grace = datetime.timedelta(hours=grace_hours(config, job))
    due = [slot for slot in expected_slots(config, job, now) if now >= slot + grace]
    return due[-1] if due else None


def is_stale(records, job, slot):
    """Did the job write nothing for its last due slot?

    A run writes its status object when it finishes, so anything at or after
    the slot instant is that slot's run - Actions can start a cron late but
    never early. Read from the listing rather than from the loaded history, so
    that a pipeline stopped for longer than the history window still answers
    this correctly.
    """
    return not any(
        record.job == job and record.moment >= slot for record in records
    )


def most_recent_weekly_summary_day(config, now):
    """The date of the most recent weekly summary slot that has passed.

    The dedup value for the summary, and the reason it fires once on a Monday
    rather than twenty-four times: every poll that day computes the same date,
    and only the first one differs from what the state file remembers.

    All of the arithmetic is on naive local wall-clock values. Subtracting
    seven days from a zone-aware datetime moves the wall clock by an hour
    across a daylight saving boundary, which would land the summary on Sunday
    23:00 once a year.
    """
    settings = config["notifications"]["weekly_summary"]
    wanted_day = WEEKDAY_NAMES.index(str(settings["day"]).lower())

    local = now.astimezone(ZoneInfo(config["timezone"])).replace(tzinfo=None)
    slot = local.replace(
        hour=settings["hour_local"], minute=0, second=0, microsecond=0
    )
    slot -= datetime.timedelta(days=(slot.weekday() - wanted_day) % 7)
    if slot > local:
        slot -= datetime.timedelta(days=7)
    return slot.date().isoformat()


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------


def routing_for(config, outcome):
    """The roles config.yml routes one outcome to.

    Raises rather than defaulting to nobody. A missing row is a configuration
    mistake that would otherwise present as an alert that was never sent, and
    silence is the one failure mode this whole job exists to remove.
    """
    routing = config["notifications"]["routing"]
    if outcome not in routing:
        raise ValueError(
            f"config.yml has no notifications.routing row for '{outcome}'. "
            f"Add one - an empty list means nobody is mailed, which is a "
            f"decision worth writing down rather than arriving at by accident. "
            f"The rows are the five run statuses plus stale, prune_paused, "
            f"no_monthly_candidate and weekly_summary."
        )
    return list(routing[outcome])


def role_addresses(config, dry_run):
    """Every role's address list, read from the environment variable
    config.yml names for it.

    The routing table is policy and lives in the repository; the addresses are
    not, and live only on the Jenkins host. That is also what lets the whole
    thing be pointed at one test address for a few days without a code or
    config change.

    A role with no variable set is fatal in a real run - an alert with nowhere
    to go is worse than a failed build, because the failed build is at least
    visible. In a dry run it is reported instead, so that the routing can be
    read back before the variables are in place.
    """
    addresses = {}
    missing = []
    for role, variable in config["notifications"]["roles"].items():
        raw = os.getenv(variable, "")
        parsed = [part.strip() for part in raw.split(",") if part.strip()]
        if not parsed:
            missing.append(f"{role} ({variable})")
            parsed = [f"<{variable} not set>"] if dry_run else []
        addresses[role] = parsed

    if missing and not dry_run:
        raise ValueError(
            f"No address is set for: {', '.join(missing)}. Each role in "
            f"config.yml's notifications.roles names the environment variable "
            f"holding its address list, comma separated, and those variables "
            f"are set on the Jenkins host only. Set them and re-run, or run "
            f"with --dry-run to see the routing without sending anything."
        )
    if missing:
        logger.warning("No address set for: %s", ", ".join(missing))
    return addresses


def merge_roles(*groups):
    """One role list from several, in order and without repeats."""
    merged = []
    for group in groups:
        for role in group or []:
            if role not in merged:
                merged.append(role)
    return merged


def addresses_for(roles, addresses):
    """The address list for a set of roles, in order and without repeats.

    A DATA_FAIL goes to the data owner, the shared inbox and the developer,
    and one person can hold two of those roles while it is being tested.
    """
    resolved = []
    for role in roles:
        for address in addresses.get(role, []):
            if address not in resolved:
                resolved.append(address)
    return resolved


# ---------------------------------------------------------------------------
# The messages
# ---------------------------------------------------------------------------


def local_text(config, moment):
    """A UTC instant written the way a reader in Victoria expects it."""
    return moment.astimezone(ZoneInfo(config["timezone"])).strftime("%Y-%m-%d %H:%M %Z")


def run_block(config, record):
    """The facts about one run, under the summary, in every message about it."""
    payload = record.payload
    lines = [
        f"Job:     {JOB_LABELS[record.job]}",
        f"Status:  {status_of(record)}",
        f"Run:     {payload.get('run_id', record.key)}",
        f"When:    {local_text(config, record.moment)}",
        f"Code:    {payload.get('code_version', 'unknown')}",
    ]
    url = payload.get("workflow_run_url")
    # Null for a run started outside Actions - a hand run, or the Phase 2 call
    # from the NRIDS server. Saying so beats printing an empty field.
    lines.append(f"Log:     {url}" if url else "Log:     not a GitHub Actions run")

    details = payload.get("details") or []
    if details:
        lines.append("")
        lines.append("What the run reported:")
        lines.extend(f"  - {line}" for line in details)
    return "\n".join(lines)


def message_body(paragraphs):
    """Assemble a plain text body with the standard footer.

    Empty paragraphs are dropped rather than rendered as a gap: a run that
    wrote no summary should not leave a blank line where one was expected.
    """
    kept = [paragraph for paragraph in paragraphs if paragraph and paragraph.strip()]
    return "\n\n".join([*kept, FOOTER]) + "\n"


def status_notification(config, record):
    """The alert for one run's status.

    The summary is carried through exactly as checks.py and backup.py wrote
    it. Both write it for a non-technical reader precisely so that it can be
    the email body, so rewording or shortening it here would only lose the
    work (DESIGN.md 8.3).
    """
    run_status = status_of(record)
    return Notification(
        key=f"status:{record.job}",
        value=run_status,
        roles=routing_for(config, run_status),
        subject=f"{SUBJECT_PREFIX} {run_status}: {JOB_LABELS[record.job]}",
        body=message_body([
            record.payload.get("summary", "(the run wrote no summary)"),
            run_block(config, record),
        ]),
    )


def resolution_notification(config, record, previous, escalated):
    """The second of the two emails a five-day incident produces.

    It goes to whoever was told about the problem, which is why the state file
    records the roles alongside the value: a DATA_FAIL reaches the data owner
    and the shared inbox, and leaving them with an alarm they never hear the
    end of is how a channel gets rule-filtered into a folder - the failure
    DESIGN.md 8.4 spends its length avoiding.

    Including anyone an escalation added. A SYSTEM_FAIL past three days told
    the data owner and the shared inbox that backups had stopped; the original
    alert went to the developer alone, so without this they would be the two
    people never told it was over.
    """
    roles = merge_roles(
        previous.get("roles") or routing_for(config, previous["value"]),
        (escalated or {}).get("roles"),
    )
    label = JOB_LABELS[record.job]
    return Notification(
        key=f"status:{record.job}",
        value=status_of(record),
        roles=roles,
        subject=f"{SUBJECT_PREFIX} Resolved: {label}",
        body=message_body([
            f"The {label} is back to {status_of(record)}. "
            f"The previous alert reported {previous['value']} and nothing "
            f"further is needed.",
            record.payload.get("summary", ""),
            run_block(config, record),
        ]),
    )


def escalation_notification(config, job, episode, now):
    """SYSTEM_FAIL past system_fail_escalation_days, which adds the client.

    This is the single worst bug available in this file, so it is worth being
    explicit about what makes it safe. The condition "has it been more than
    three days" is true on every poll for as long as the failure lasts.
    Evaluated on its own that is one email an hour, to the data owner and the
    shared inbox, until somebody intervenes. What prevents it is that the
    dedup value below is a constant: the situation is 'escalated' and stays
    'escalated', so it differs from the recorded value exactly once. The entry
    is dropped when the episode ends, which is what lets a later failure
    escalate again. DESIGN.md 8.5.
    """
    roles = merge_roles(
        routing_for(config, "SYSTEM_FAIL"),
        config["notifications"]["system_fail_escalation_adds"],
    )
    days = int(episode_days(episode, now))
    label = JOB_LABELS[job]
    return Notification(
        key=f"escalation:system_fail:{job}",
        value="escalated",
        roles=roles,
        subject=f"{SUBJECT_PREFIX} The {label} has been failing for {days} days",
        body=message_body([
            f"The {label} has failed on every run for {days} days - "
            f"{len(episode)} runs in a row, the first at "
            f"{local_text(config, episode[0].moment)}.",
            "This is an operational failure rather than a problem with the "
            "data: authentication, object storage or the export service. It "
            "does mean that for the last few days the backups have not been "
            "running, which is why this message goes further than the "
            "original alert did.",
            run_block(config, episode[-1]),
        ]),
    )


def prune_paused_notification(config, episode, now):
    """Pruning held open past retention.paused_prune_alert_days.

    Same one-shot shape as the escalation above and for the same reason.
    """
    days = int(episode_days(episode, now))
    return Notification(
        key="prune_paused",
        value="paused",
        roles=routing_for(config, "prune_paused"),
        subject=f"{SUBJECT_PREFIX} Backup pruning has been paused for {days} days",
        body=message_body([
            f"Old backups have not been pruned for {days} days, because the "
            f"most recent integrity check reported DATA_FAIL and pruning "
            f"pauses while a data incident is open.",
            "That is deliberate - it stops a live incident from quietly "
            "evicting the last good copy - but it is not meant to be the "
            "steady state. The rotating tier keeps growing until the ceiling "
            "in config.yml is reached. Resolving the check failure resumes "
            "normal pruning on the next backup run.",
            run_block(config, episode[-1]),
        ]),
    )


def monthly_candidate_notification(config, record, month):
    """The monthly tier has stopped filling.

    Read out of a backup run's details rather than from its status, which is
    the one place this job looks past the status field. It contradicts the
    rule everywhere else in this file and it has to: the condition is reported
    by a run whose status is PASS, and PASS routes to nobody, so it was
    configured, logged, written into the status object and unreachable
    (DESIGN.md 8.4).

    The dedup value is the month, so this is one email per month that fails to
    fill rather than one per backup run.
    """
    return Notification(
        key="no_monthly_candidate",
        value=month,
        roles=routing_for(config, "no_monthly_candidate"),
        subject=f"{SUBJECT_PREFIX} Nothing promoted to the monthly backup tier for {month}",
        body=message_body([
            f"{month} is well under way and no backup has been promoted to "
            f"the monthly tier. Promotion needs a rotating set whose paired "
            f"integrity check returned PASS, and no set this month has one.",
            "The rotating backups are unaffected and are still being taken. "
            "What is missing is the longer-term monthly copy, which is only "
            "noticed when somebody goes looking for a restore point from a "
            "month ago that was never created.",
            run_block(config, record),
        ]),
    )


def stale_notification(config, job, slot, records):
    """An expected run slot passed with nothing written.

    The dedup value is a constant for the same reason as the escalation: the
    condition stays true every hour until the pipeline runs again. One alert
    while it is stale, and the resolution comes as the next run's own status.
    """
    label = JOB_LABELS[job]
    seen = [record for record in records if record.job == job]
    last = local_text(config, seen[-1].moment) if seen else "never"
    return Notification(
        key=f"stale:{job}",
        value="stale",
        roles=routing_for(config, "stale"),
        subject=f"{SUBJECT_PREFIX} The {label} did not run",
        body=message_body([
            f"The {label} was due at {local_text(config, slot)} and has "
            f"written no result. The last one to report was {last}.",
            "A scheduled job that stops running produces the same silence as "
            "one that keeps passing, so this is checked positively rather "
            "than waited for. The usual causes are a failed or disabled "
            "GitHub Actions workflow, expired credentials, or GitHub "
            "suspending the schedule after a long period of no commits.",
            "No data is at risk. The backups already taken are untouched; "
            "what has stopped is the taking of new ones.",
        ]),
    )


def weekly_summary_notification(config, records, history, now, summary_day):
    """Monday morning reassurance that the control is alive.

    Not a failure-detection mechanism - staleness does that (DESIGN.md 8.2) -
    so this reports rather than judges. It fires once because the dedup value
    is the date of the summary slot, which every poll on that Monday computes
    identically.
    """
    since = now - datetime.timedelta(days=SUMMARY_DAYS)
    lines = []
    for job in JOBS:
        recent = [
            record for record in records_for(history, job) if record.moment >= since
        ]
        label = JOB_LABELS[job]
        if not recent:
            lines.append(f"  {label}: no runs in the last {SUMMARY_DAYS} days")
            continue
        counted = {}
        for record in recent:
            counted[status_of(record)] = counted.get(status_of(record), 0) + 1
        breakdown = ", ".join(f"{count} {name}" for name, count in sorted(counted.items()))
        lines.append(
            f"  {label}: {len(recent)} run(s) - {breakdown}. "
            f"Most recent {local_text(config, recent[-1].moment)}."
        )

    overdue = []
    for job in JOBS:
        slot = last_due_slot(config, job, now)
        if slot and is_stale(records, job, slot):
            overdue.append(f"  {JOB_LABELS[job]}: nothing since the slot at "
                           f"{local_text(config, slot)}")

    paragraphs = [
        "Weekly summary for the Water Licensed Works backups.",
        f"In the last {SUMMARY_DAYS} days:\n" + "\n".join(lines),
    ]
    if overdue:
        paragraphs.append("Overdue:\n" + "\n".join(overdue))
    else:
        paragraphs.append("Nothing is overdue. Every expected run has reported.")

    return Notification(
        key="weekly_summary",
        value=summary_day,
        roles=routing_for(config, "weekly_summary"),
        subject=f"{SUBJECT_PREFIX} Weekly summary, {summary_day}",
        body=message_body(paragraphs),
    )


# ---------------------------------------------------------------------------
# What this poll would send
# ---------------------------------------------------------------------------


def collect_notifications(config, records, history, state, now):
    """Everything that has changed since the last poll, as a list of messages.

    One mechanism for all of it. Each situation this job watches produces at
    most one Notification per poll, carrying a key naming the situation and a
    value saying what it currently is. Mail goes out when that value differs
    from the value the state file recorded for the same key, and never
    otherwise, so:

      - a DATA_FAIL lasting five days is one value for five days: one email
      - the return to PASS is a different value: one more, the resolution
      - a threshold crossed - SYSTEM_FAIL past three days, pruning paused past
        seven - has a constant value, so it fires once and not once an hour
      - a situation that has ended produces no Notification at all, its state
        entry is dropped by `deliver`, and a recurrence is a fresh alert

    Nothing here may put a run_id or a timestamp in a key or a value. Every
    poll sees a new run_id once a day.
    """
    notifications = []

    # The five run statuses. A status the routing table sends to nobody - PASS
    # - still produces a Notification, because the state has to record it or a
    # DATA_FAIL returning next week would look unchanged and go unreported.
    for job in JOBS:
        newest = newest_for(history, job)
        if newest is None or status_of(newest) is None:
            continue
        previous = state.get(f"status:{job}")
        if (
            previous
            and previous.get("value") in RESOLVING_STATUSES
            and status_of(newest) == "PASS"
        ):
            notifications.append(
                resolution_notification(
                    config, newest, previous,
                    state.get(f"escalation:system_fail:{job}"),
                )
            )
        else:
            notifications.append(status_notification(config, newest))

        # An empty episode measures zero days, so the emptiness is checked
        # rather than relying on the threshold being above zero.
        episode = system_fail_episode(history, job)
        if episode and episode_days(episode, now) >= config["notifications"]["system_fail_escalation_days"]:
            notifications.append(escalation_notification(config, job, episode, now))

    paused = prune_paused_episode(history)
    if paused and episode_days(paused, now) >= config["retention"]["paused_prune_alert_days"]:
        notifications.append(prune_paused_notification(config, paused, now))

    newest_backup = newest_for(history, BACKUP_JOB)
    if newest_backup is not None and reports(newest_backup, MONTHLY_CANDIDATE_MARKER):
        notifications.append(
            monthly_candidate_notification(
                config, newest_backup, unpromoted_month(config, newest_backup, now)
            )
        )

    for job in JOBS:
        slot = last_due_slot(config, job, now)
        if slot and is_stale(records, job, slot):
            notifications.append(stale_notification(config, job, slot, records))

    summary_day = most_recent_weekly_summary_day(config, now)
    notifications.append(
        weekly_summary_notification(config, records, history, now, summary_day)
    )

    return notifications


def unpromoted_month(config, record, now):
    """Which month the monthly-candidate warning is about.

    backup.py writes the detail line as '2026-08 is 18 days in with nothing
    promoted...', so the month is the front of it. Falling back to the current
    local month rather than raising, because a reworded message should still
    produce one email a month rather than none - the test that keeps the two
    in step is in tests/test_notify.py.
    """
    for line in record.payload.get("details", []):
        text = str(line)
        if MONTHLY_CANDIDATE_MARKER not in text:
            continue
        candidate = text[:7]
        if len(candidate) == 7 and candidate[4] == "-" and candidate[:4].isdigit():
            return candidate
    return now.astimezone(ZoneInfo(config["timezone"])).strftime("%Y-%m")


# ---------------------------------------------------------------------------
# The dedup state
#
# A small JSON file on the Jenkins host, not in the bucket. It is notifier
# state rather than project state, and keeping it local is what preserves this
# job's read-only relationship with the backups (DESIGN.md 8.5).
# ---------------------------------------------------------------------------


def state_path(argument):
    """Where the state file lives: --state, then NOTIFY_STATE_FILE, then home.

    It must be outside the Jenkins workspace. A wiped workspace looks exactly
    like a notifier that has never sent anything, so every open condition
    would mail again on the next poll.
    """
    if argument:
        return Path(argument)
    from_environment = os.getenv(STATE_FILE_VARIABLE)
    return Path(from_environment) if from_environment else DEFAULT_STATE_PATH


def load_state(path):
    """What was notified last time, or an empty state on the first run.

    A missing file is the first run on a new Jenkins host and is not an error.
    The cost is one duplicate email per open condition, which DESIGN.md 8.5
    accepts as the price of not giving this job a reason to write to the
    backup bucket.

    A corrupt file is treated the same way and said so loudly, because the
    alternative - failing every poll until somebody deletes it by hand - is
    silence, and silence is what this job exists to prevent.
    """
    if not path.exists():
        logger.info("No state file at %s yet, so this poll starts from nothing", path)
        return {}
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "The state file at %s could not be read (%s), so this poll starts "
            "from nothing and may repeat an alert that has already been sent. "
            "Delete it if this recurs.", path, exc,
        )
        return {}
    return stored.get("signals", {})


def save_state(path, signals):
    """Record what was sent, atomically.

    Written to a neighbouring temporary file and then moved into place, so a
    poll that dies mid-write leaves the previous state intact rather than a
    truncated file that reads as "nothing has ever been notified".
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": STATE_VERSION, "signals": signals}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def send_email(config, addresses, subject, body):
    """Send one message through the internal relay.

    Plain SMTP with no authentication and no TLS: this is the government
    relay on the internal network, reachable from the GIS server and from
    nowhere else, and it is what the existing staging script has always used.
    If it ever starts requiring credentials that is a change to make
    deliberately rather than a fallback to add now.
    """
    host = os.getenv("SMTP_HOST")
    sender = os.getenv("SMTP_SENDER")
    if not host or not sender:
        raise ValueError(
            "SMTP_HOST and SMTP_SENDER must both be set. They live on the "
            "Jenkins host only and are never in the repository."
        )

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(addresses)
    message["Subject"] = subject
    message.set_content(body)

    settings = config["notifications"]
    with smtplib.SMTP(
        host, settings["smtp_port"], timeout=settings["smtp_timeout_seconds"]
    ) as relay:
        relay.send_message(message)


def deliver(config, notifications, state, addresses, dry_run):
    """Send what has changed, and return the state to record next.

    Three rules hold this together.

    A notification whose value matches the recorded one is not sent, and its
    entry is carried forward unchanged. That is the deduplication.

    A key with no notification this poll is dropped from the state, because
    the situation it named has ended. That is what lets the same condition
    alert again if it comes back.

    A send that fails keeps the OLD entry rather than recording the new one,
    so the next poll tries again. Recording it would lose the alert
    permanently, which is worse than sending it twice.
    """
    kept = {}
    failures = 0

    for notification in notifications:
        previous = state.get(notification.key)
        if previous and previous.get("value") == notification.value:
            kept[notification.key] = previous
            continue

        recipients = addresses_for(notification.roles, addresses)
        if dry_run:
            report_dry_run(notification, recipients)
            continue

        if recipients:
            try:
                send_email(config, recipients, notification.subject, notification.body)
            except Exception as exc:
                # Not swallowed and not fatal to the rest of the poll: one
                # unreachable recipient must not stop the other alert going
                # out. The build goes red so Jenkins mails the developer.
                failures += 1
                logger.error(
                    "Could not send '%s' to %s: %s - it will be retried on the "
                    "next poll", notification.subject, ", ".join(recipients), exc,
                )
                if previous:
                    kept[notification.key] = previous
                continue
            logger.info(
                "Sent '%s' to %s", notification.subject, ", ".join(recipients)
            )
        else:
            # PASS routes to nobody by design. The state is still recorded, or
            # a DATA_FAIL next week would look unchanged and go unreported.
            logger.info(
                "%s is now '%s', which config.yml routes to nobody",
                notification.key, notification.value,
            )

        kept[notification.key] = {
            "value": notification.value,
            "roles": list(notification.roles),
            # Read by a person looking at the file, never by this code. A
            # decision taken on a timestamp is how hourly polling becomes
            # hourly email.
            "sent_utc": datetime.datetime.now(datetime.timezone.utc).strftime(STAMP_FORMAT),
        }

    return kept, failures


def report_dry_run(notification, recipients):
    """Print one message that would have been sent.

    The point of --dry-run is the few days the whole thing is pointed at one
    test address, so this shows the routing and the body in full rather than
    summarising them.
    """
    print("=" * 72)
    print(f"WOULD SEND   {notification.subject}")
    print(f"to           {', '.join(recipients) or '(nobody - routed to no role)'}")
    print(f"roles        {', '.join(notification.roles) or '(none)'}")
    print(f"dedup key    {notification.key} = {notification.value}")
    print("-" * 72)
    print(notification.body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def load_config(path):
    """Read config.yml.

    config.yml is the single source of truth for the routing table, the
    schedule and the grace periods, which is why PyYAML is in
    jenkins/requirements.txt. Generating a JSON copy for this job would keep
    the dependency list at boto3 and leave two routing tables to drift apart -
    the worse of the two outcomes. DESIGN.md 9.1.
    """
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def poll(config, bucket, state, now, dry_run):
    """One poll: read the bucket, decide, send. Returns (state, failures)."""
    records = list_status_records(bucket, config)
    history = load_history(bucket, records, now)
    logger.info(
        "%d status object(s) under status/, %d read for this poll",
        len(records), len(history),
    )

    notifications = collect_notifications(config, records, history, state, now)
    return deliver(config, notifications, state, role_addresses(config, dry_run), dry_run)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Poll the Water Licensed Works status objects and send alert mail. "
            "Reads object storage and writes nothing to it."
        )
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH),
        help="Path to config.yml (default: the copy alongside this repository).",
    )
    parser.add_argument(
        "--state", default=None,
        help=(
            "Path to the deduplication state file. Defaults to "
            f"${STATE_FILE_VARIABLE} and then to {DEFAULT_STATE_PATH}. It must "
            "not be in the Jenkins workspace - a wiped workspace re-sends every "
            "open alert."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Print what would be sent and send nothing. The state file is not "
            "written either, so a dry run cannot suppress a real alert later."
        ),
    )
    arguments = parser.parse_args(argv)

    config = load_config(arguments.config)
    logging.basicConfig(
        level=config.get("logging", {}).get("level", "INFO"),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    path = state_path(arguments.state)
    state = load_state(path)
    bucket = connect_to_bucket(config)
    now = datetime.datetime.now(datetime.timezone.utc)

    updated, failures = poll(config, bucket, state, now, arguments.dry_run)

    if arguments.dry_run:
        # Writing it would record as sent something that was never sent, and
        # the real alert would then be deduplicated away.
        logger.info("Dry run: nothing sent and %s not written", path)
    else:
        save_state(path, updated)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
