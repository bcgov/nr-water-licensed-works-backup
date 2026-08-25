"""Alert mail for the Water Licensed Works backup and check jobs.

Runs hourly on the GIS server under Jenkins. One poll reads the status
objects the GitHub Actions jobs wrote to object storage, works out what has
changed since the last poll, and sends mail through the internal SMTP relay.

    python notify.py --dry-run     report what would be sent, send nothing
    python notify.py               send it

GitHub-hosted runners cannot reach the relay and the GIS server can, which is
the whole reason this job exists on a second platform (DESIGN.md 8.1). Actions
does all the substantive work and writes a status object every run; this job
reads `status`, looks the recipients up in config.yml, and builds the message
around `summary`. All judgement stays in checks.py - with two deliberate exceptions,
both marked where they happen. no_monthly_candidate is reported in the details
of a run whose status is PASS, and PASS routes to nobody. features_outside_bc
is kept out of the status on purpose, because a status that can never be PASS
would permanently block promotion to the monthly tier (DESIGN.md 7.6.1), so
its routing row widens who the run's own email goes to instead.

ONE EMAIL PER RUN, NOT ONE PER KIND OF PROBLEM. Everything a check run found
travels in that run's own message: the status, the summary, the validity
findings and the details. A finding was briefly given a message of its own and
it was the wrong shape - the recipient got two emails about one check, and had
to put them together to know what the check had said.

WRITTEN FOR THE PERSON WHO OPENS IT, NOT FOR US. Most of these go to a data
owner and a shared mailbox, and neither has any reason to know what a run id
is. So every message says what happened in plain language first, then what
needs a person, then that nothing has been touched - and the run identifiers
sit at the very bottom under a heading everybody else can stop at. Each one is
sent twice over, as formatted HTML and as plain text, both rendered from one
list of blocks so they cannot come to say different things. See `The messages`
below before rewording anything: the language there is the product.

ONE WRITE, TO ONE KEY. This job lists and reads under status/, and the only
thing it writes anywhere is its own deduplication state, to notify/state.json.
It never writes to a backup tier and it deletes and copies nothing at all. That
cannot be *enforced* by a permission - the bucket issues one full-access key
pair (DESIGN.md 6.6) - so it is enforced by shape instead: save_state takes no
key argument, the destination is a module constant, and tests/test_notify.py
asserts that the only write in this file goes to that key and that no delete or
copy call exists in it.

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
import textwrap
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
import yaml
from botocore.exceptions import ClientError

logger = logging.getLogger("notify")

# config.yml sits at the repository root, one level above jenkins/. Resolved
# from this file rather than from the working directory so that the Jenkins
# job can run it from anywhere in the workspace.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yml"

# Bumped if the shape of the state object changes, so a future reader can tell
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

# Three conditions this job has to read out of a status object's details,
# because none of them has a status of its own.
#
# The first two are the exception to "all judgement stays in checks.py"
# flagged in the module docstring. backup.promote_monthly logs the monthly
# candidate warning against a run whose status is PASS, and PASS routes to
# nobody, so without this the alert that promotion.no_candidate_alert_days
# has been asking for since the beginning reaches no one - configured,
# logged, written into the status object and unreachable, which is exactly
# what happened on the first production backup (DESIGN.md 8.4).
#
# The third is there for a stronger reason than routing. checks.py reports a
# feature that cannot be in British Columbia as a finding rather than as a
# rule violation, because a violation carries a severity and any permanently
# non-PASS status permanently blocks monthly promotion - two known-bad
# records would cost every monthly restore point until somebody corrected
# them (DESIGN.md 7.6.1). So the status field stays clean and the condition
# travels in the details, and this is where it is picked back up.
#
# All three strings are a contract with the file that writes them. If a
# message there is reworded, reword it here too - tests/test_notify.py builds
# the real detail lines from backup.py and checks.py and asserts these still
# match, so the contract fails a test rather than failing silently in a
# month's time.
MONTHLY_CANDIDATE_MARKER = "days in with nothing promoted to the monthly tier"
PRUNE_PAUSED_MARKER = "pruning paused"
OUTSIDE_BC_MARKER = "outside British Columbia"

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

    body and html are the same message written twice - both are rendered from
    one list of blocks, so they cannot say different things. The mail carries
    both and the recipient's client picks; body is also what --dry-run prints,
    because it is the version a person can read in a terminal.
    """

    key: str
    value: str
    roles: list
    subject: str
    body: str
    html: str = ""


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
    """Does this run's details carry the marker for one of the three
    conditions that have no status of their own? See MONTHLY_CANDIDATE_MARKER."""
    return any(marker in str(line) for line in record.payload.get("details", []))


def outside_bc_lines(record):
    """The lines checks.py wrote about features outside the province.

    All of them, because there is one per layer and both layers can have
    them. They are the message as well as the signal, so unlike the other two
    markers this one wants the text rather than a yes or no.
    """
    return [
        str(line) for line in record.payload.get("details", [])
        if OUTSIDE_BC_MARKER in str(line)
    ]


def outside_bc_signature(lines):
    """What the out-of-province situation currently is, as one short string.

    checks.py writes each line as '<layer>: <count> features are outside
    British Columbia (OBJECTID ...)', so the layer and the count are the front
    of it and everything that follows is detail. Reading only those
    two is what makes a run that names the same records produce no second
    email, while a third bad record appearing produces one.

    A line this cannot parse is used whole rather than discarded, for the
    same reason unpromoted_month falls back: a reworded message should still
    produce one email rather than none, and the line is just as stable while
    the situation is. tests/test_notify.py builds these lines from checks.py
    so a rewording that breaks the parse fails a test first.
    """
    found = []
    for line in lines:
        layer, separator, rest = line.partition(":")
        # The count is written with thousands separators, as every count in
        # this project is.
        count = rest.strip().split(" ", 1)[0].replace(",", "")
        if separator and layer and count.isdigit():
            found.append(f"{layer} {int(count)}")
        else:
            found.append(line)
    return ", ".join(sorted(found))


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
            f"no_monthly_candidate, features_outside_bc and weekly_summary."
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
#
# A message is a list of blocks, and every block renders twice: once as plain
# text and once as HTML. Writing it that way keeps the wording in one place,
# so the formatted email and the fallback cannot drift apart.
#
# Outlook is the client here, so it gets headings, bullets and small tables;
# anything that cannot show HTML falls back to the text version, which has to
# stay readable on its own. **Double asterisks** mark bold and are the only
# markup - they read as emphasis in the text version too, so nothing is lost.
#
# WHO EACH PART IS FOR. The data owner and the shared inbox want to know what
# happened and whether anything needs a person. Run identifiers, commit hashes
# and workflow links are the maintainer's, and they sit at the foot of the
# message under their own heading, where everybody else can stop reading. A
# commit hash in the middle of a message about somebody's data is noise that
# makes the part they can act on harder to find.
# ---------------------------------------------------------------------------

# First line of every message. The first question an automated email raises is
# whether a person sent it, and the highlight is there so that answer is read
# before anything else rather than found at the bottom.
BANNER = "**Sent automatically** by the Water Licensed Works notification job."

# Last line of every message, before the maintainer's section. The recipients
# include people who do not work on the pipeline, and the question an alert
# raises for them is "has something been done to my data" - to which the
# answer is always no.
FOOTER = (
    "**Nothing has been changed.** This job only reads the data and reports "
    "on it. It never edits ArcGIS Online and it never deletes a backup. "
    "Restoring data is always the data owner's decision, and is done by hand."
)

TECHNICAL_HEADING = "Technical details - for the pipeline maintainer"

# Where the plain text version wraps. Nothing here is read in a terminal wider
# than a mail client's reading pane, and an unwrapped paragraph arrives as one
# very long line in the clients that do not wrap it themselves.
TEXT_WIDTH = 76

# What each status means, in the words somebody who has never seen this
# pipeline would use. The status word on its own is jargon - "BASELINE" tells
# the data owner nothing - and a five-word vocabulary is not something anybody
# should have to learn in order to read their own alerts.
STATUS_MEANINGS = {
    "PASS": "nothing changed since the last run",
    "BASELINE": "first run, nothing to compare against yet",
    "WARN": "something moved more than expected",
    "DATA_FAIL": "the data changed in a way that needs a person",
    "SYSTEM_FAIL": "the job itself could not finish",
}

# The lead-in checks.summarise puts in front of a validity finding when it
# appends one to a run's summary. The finding gets a section of its own a few
# lines further down every email, so the summary is cut here rather than
# printing it twice. One contract written in two files, like the three markers
# above; tests/test_notify.py builds a real summary from checks.py and asserts
# that this still splits it.
FINDING_LEAD_IN = " Also flagged: "


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


def heading(text):
    """A section title."""
    return ("heading", text)


def paragraph(text):
    """One paragraph of prose."""
    return ("paragraph", text)


def bullets(items):
    """A list. Anything that is really several facts should be one of these."""
    return ("bullets", [str(item) for item in items])


def table(rows, headers=None):
    """A small table, one tuple per row.

    With no headers and two columns per row the first column is read as a
    label and shaded, which is the shape most of these are: a handful of facts
    a reader can take in without having to read a sentence.
    """
    return ("table", (headers, [tuple(str(cell) for cell in row) for row in rows]))


def technical(blocks):
    """The maintainer's section. Always rendered last, however it is passed."""
    return ("technical", [block for block in blocks if block])


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def plain(text):
    """One string for the text version: the bold markers taken back out."""
    return str(text).replace("**", "")


def escape(text):
    """The three characters that would otherwise be read as HTML."""
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def rich(text):
    """One string for the HTML version: escaped, with **bold** marked up.

    Split on the marker and emphasise the odd-numbered pieces, which are the
    ones that sat between a pair. An unpaired marker can only over-emphasise
    the tail of a line and never produce broken HTML, because every piece is
    escaped before any tag is added to it.
    """
    pieces = escape(text).split("**")
    return "".join(
        f"<strong>{piece}</strong>" if index % 2 else piece
        for index, piece in enumerate(pieces)
    )


def text_table(headers, rows):
    """A table as padded columns, each one as wide as its longest cell."""
    if not rows:
        return ""
    measured = ([tuple(headers)] + rows) if headers else rows
    widths = [
        max(len(plain(row[column])) for row in measured)
        for column in range(len(measured[0]))
    ]
    lines = []
    if headers:
        lines.append(text_table_row(headers, widths))
        lines.append("  " + "  ".join("-" * width for width in widths))
    lines.extend(text_table_row(row, widths) for row in rows)
    return "\n".join(lines)


def text_table_row(cells, widths):
    padded = [plain(cell).ljust(width) for cell, width in zip(cells, widths)]
    return ("  " + "  ".join(padded)).rstrip()


def render_text(blocks):
    """The plain text version of a list of blocks.

    A heading sits directly on top of what it introduces and is underlined, so
    that the text version has the same structure the HTML one does rather than
    arriving as one undifferentiated wall.
    """
    rendered = ""
    previous = None
    for kind, content in blocks:
        if kind == "banner":
            piece = f"*** {plain(content)} ***"
        elif kind == "heading":
            piece = plain(content) + "\n" + "-" * len(plain(content))
        elif kind in ("paragraph", "footer"):
            piece = textwrap.fill(plain(content), TEXT_WIDTH)
        elif kind == "bullets":
            piece = "\n".join(
                textwrap.fill(
                    plain(item), TEXT_WIDTH,
                    initial_indent="  - ", subsequent_indent="    ",
                )
                for item in content
            )
        elif kind == "table":
            piece = text_table(*content)
        elif kind == "technical":
            piece = "\n".join(
                ["-" * 70, TECHNICAL_HEADING, "", render_text(content)]
            )
        else:
            raise ValueError(
                f"Unknown message block '{kind}'. The kinds are the ones the "
                f"block helpers above return."
            )

        if not piece.strip():
            continue
        if rendered:
            rendered += "\n" if previous == "heading" else "\n\n"
        rendered += piece
        previous = kind
    return rendered


# Inline styles, because the clients here are Outlook and a shared mailbox and
# both drop a stylesheet in the head. Held as constants rather than repeated
# through the markup so that a colour is changed in one place.
BODY_STYLE = (
    "font-family:'Segoe UI',Calibri,Arial,sans-serif;font-size:14px;"
    "line-height:1.5;color:#1f2328;max-width:660px;"
)
BANNER_STYLE = (
    "background-color:#fff6cc;border-left:4px solid #e0aa00;padding:10px 14px;"
    "font-size:13px;color:#5a4600;"
)
HEADING_STYLE = "font-size:15px;font-weight:600;color:#1f2328;margin:22px 0 8px 0;"
PARAGRAPH_STYLE = "margin:0 0 10px 0;"
LIST_STYLE = "margin:0 0 12px 0;padding-left:22px;"
ITEM_STYLE = "margin:0 0 5px 0;"
TABLE_STYLE = "border-collapse:collapse;margin:0 0 14px 0;font-size:13px;"
CELL_STYLE = "border:1px solid #d8dee4;padding:6px 11px;vertical-align:top;"
HEADER_CELL_STYLE = (
    CELL_STYLE + "background-color:#eef1f5;font-weight:600;text-align:left;"
)
LABEL_CELL_STYLE = (
    CELL_STYLE + "background-color:#f6f8fa;color:#57606a;white-space:nowrap;"
)
FOOTER_STYLE = "margin:18px 0 0 0;color:#57606a;font-size:13px;"
TECHNICAL_STYLE = (
    "border-top:1px solid #d8dee4;margin-top:26px;padding-top:14px;"
    "font-size:12px;color:#57606a;"
)
LINK_STYLE = "color:#0b5cad;"


def html_cell(text):
    """One table cell. A URL becomes a link, anything else is ordinary text."""
    value = str(text)
    if value.startswith("http://") or value.startswith("https://"):
        return f'<a href="{escape(value)}" style="{LINK_STYLE}">{escape(value)}</a>'
    return rich(value)


def html_table(headers, rows):
    if not rows:
        return ""
    parts = [
        f'<table role="presentation" cellspacing="0" cellpadding="0" '
        f'style="{TABLE_STYLE}">'
    ]
    if headers:
        cells = "".join(
            f'<th style="{HEADER_CELL_STYLE}">{rich(cell)}</th>' for cell in headers
        )
        parts.append(f"<tr>{cells}</tr>")
    # Two columns and no header row is the label-and-value shape.
    labelled = not headers and all(len(row) == 2 for row in rows)
    for row in rows:
        cells = ""
        for index, cell in enumerate(row):
            style = LABEL_CELL_STYLE if labelled and index == 0 else CELL_STYLE
            cells += f'<td style="{style}">{html_cell(cell)}</td>'
        parts.append(f"<tr>{cells}</tr>")
    parts.append("</table>")
    return "".join(parts)


def render_html(blocks):
    """The HTML version of a list of blocks."""
    parts = []
    for kind, content in blocks:
        if kind == "banner":
            # A single-cell table rather than a styled div: Outlook renders a
            # background colour on a table cell reliably and on a div it is a
            # coin toss, and a banner that is not highlighted is the one thing
            # this block exists to avoid.
            parts.append(
                f'<table role="presentation" cellspacing="0" cellpadding="0" '
                f'width="100%" style="border-collapse:collapse;margin:0 0 20px 0;">'
                f'<tr><td style="{BANNER_STYLE}">{rich(content)}</td></tr></table>'
            )
        elif kind == "heading":
            parts.append(f'<div style="{HEADING_STYLE}">{rich(content)}</div>')
        elif kind == "paragraph":
            parts.append(f'<p style="{PARAGRAPH_STYLE}">{rich(content)}</p>')
        elif kind == "footer":
            parts.append(f'<p style="{FOOTER_STYLE}">{rich(content)}</p>')
        elif kind == "bullets":
            items = "".join(
                f'<li style="{ITEM_STYLE}">{rich(item)}</li>' for item in content
            )
            parts.append(f'<ul style="{LIST_STYLE}">{items}</ul>')
        elif kind == "table":
            parts.append(html_table(*content))
        elif kind == "technical":
            parts.append(
                f'<div style="{TECHNICAL_STYLE}">'
                f'<div style="font-weight:600;margin:0 0 8px 0;">'
                f'{escape(TECHNICAL_HEADING)}</div>'
                f'{render_html(content)}</div>'
            )
        else:
            raise ValueError(
                f"Unknown message block '{kind}'. The kinds are the ones the "
                f"block helpers above return."
            )
    return "".join(parts)


def html_document(blocks):
    return (
        "<html><body>"
        f'<div style="{BODY_STYLE}">{render_html(blocks)}</div>'
        "</body></html>"
    )


def full_message(blocks):
    """Banner, what the message says, the reassurance, then the technical bit.

    The maintainer's section is moved to the end wherever the caller put it,
    so that a message builder can keep it next to the run it describes while
    the reader still gets the part that concerns them first.
    """
    kept = [block for block in blocks if block]
    ordinary = [block for block in kept if block[0] != "technical"]
    maintainer = [block for block in kept if block[0] == "technical"]
    return [("banner", BANNER), *ordinary, ("footer", FOOTER), *maintainer]


def notification(key, value, roles, subject, blocks):
    """One email, with both versions of its body rendered from the blocks."""
    prepared = full_message(blocks)
    return Notification(
        key=key,
        value=value,
        roles=list(roles),
        subject=subject,
        body=render_text(prepared) + "\n",
        html=html_document(prepared),
    )


# ---------------------------------------------------------------------------
# The parts the messages are built from
# ---------------------------------------------------------------------------


def local_text(config, moment):
    """A UTC instant written the way a reader in Victoria expects it."""
    return moment.astimezone(ZoneInfo(config["timezone"])).strftime("%Y-%m-%d %H:%M %Z")


def result_cell(status):
    """The status with its meaning beside it, for the table at the top."""
    meaning = STATUS_MEANINGS.get(status)
    return f"**{status}** - {meaning}" if meaning else f"**{status}**"


def at_a_glance(config, record):
    """The three facts every recipient needs: which job, how it went, when."""
    return table([
        ("Job", JOB_LABELS[record.job]),
        ("Result", result_cell(status_of(record))),
        ("When", local_text(config, record.moment)),
    ])


def summary_of(record):
    """The run's own plain-language summary, without any finding on the end.

    checks.py appends a validity finding to the summary so that the line is
    complete wherever it is read - in the status object, in a metrics file, in
    a log. In an email that finding has a section of its own a few lines
    below, so carrying it here as well would print it twice.
    """
    summary = record.payload.get("summary") or "(the run wrote no summary)"
    return summary.split(FINDING_LEAD_IN, 1)[0].strip()


def technical_blocks(config, record):
    """The run identifiers, and the raw lines the run logged.

    All of it is for whoever has to look a run up: the run id, the commit that
    produced it, the workflow log, and the check messages in the words the
    code wrote them in. None of it is the data owner's, which is why it sits
    behind its own heading at the foot of the message.
    """
    payload = record.payload
    url = payload.get("workflow_run_url")
    blocks = [table([
        ("Run", payload.get("run_id", record.key)),
        ("Code", payload.get("code_version", "unknown")),
        # Null for a run started outside Actions - a hand run, or the Phase 2
        # call from the NRIDS server. Saying so beats printing an empty field.
        ("Log", url if url else "not a GitHub Actions run"),
    ])]
    details = payload.get("details") or []
    if details:
        blocks.append(paragraph("**What the run reported**"))
        blocks.append(bullets(details))
    return blocks


def status_value(run_status, findings):
    """The dedup value for a status: entry.

    The run status on its own when the check found nothing invalid, which is
    what it has always been - so every state entry already in the bucket and
    every count test written for Step 6 compares exactly as before.

    With a finding it also carries what that finding currently is. That is
    what makes one email go out when the status changes AND one go out when a
    new bad record appears on a day the status did not move. The second of
    those is not a nicety: a healthy run is a PASS, PASS routes to nobody, so
    without it a third bad record on a passing day would reach no one, which
    is the silence DESIGN.md 7.6.1 was written about.
    """
    if not findings:
        return run_status
    return f"{run_status} + {outside_bc_signature(findings)}"


def status_part(value):
    """The run status out of a status: value that may also carry a finding.

    Used wherever a recorded value is compared against the five statuses. A
    status never contains ' + ', so the split is unambiguous.
    """
    return str(value).split(" + ", 1)[0]


def finding_roles(config, findings):
    """Who a run's validity findings add to its recipients, if any.

    A finding says a record cannot be right, and only the data owner can
    correct it, so it widens the list beyond whoever the run status alone
    would reach. Which matters most on the status that reaches nobody: PASS.
    """
    return routing_for(config, "features_outside_bc") if findings else []


def finding_bullet(line):
    """One finding line as a bullet, with its layer name in front.

    checks.outside_bc_finding writes '<layer>: <count> features are outside
    ...'. A line that does not have that shape is used exactly as written, for
    the same reason outside_bc_signature falls back to the whole line: a
    rewording should still tell somebody rather than tell nobody.
    """
    layer, separator, rest = line.partition(":")
    if not separator or not rest.strip():
        return line
    return f"**{layer.strip()} layer** - {rest.strip()}"


def finding_blocks(findings):
    """What an email says about features that cannot be where they are.

    Here rather than in checks.py because checks.py writes one line for a
    metrics file and a status object, and this is the part that only means
    anything to somebody reading an email.
    """
    if not findings:
        return []
    return [
        heading("Needs attention"),
        bullets(finding_bullet(line) for line in findings),
        paragraph(
            "These features sit well outside the province, so the records are "
            "wrong wherever they came from. Only the data owner can correct them."
        ),
        paragraph(
            "**Nothing has happened today.** The records have been like this "
            "all along, and this section will appear on every email until they "
            "are corrected or removed. It does not affect the result above, and "
            "it does not stop backups being promoted to the monthly tier."
        ),
    ]


# What the subject says when a run reports a validity finding. There is one
# kind of finding today and naming it beats a category word: "data quality
# findings" told the reader nothing they could act on.
FINDING_HEADLINE = "features recorded outside British Columbia"

# The statuses that are themselves news. On these the status leads the subject
# and the finding follows it. On PASS and BASELINE there is no competing
# headline, so the finding is the whole of it - see status_subject.
HEADLINE_STATUSES = ("WARN", "DATA_FAIL", "SYSTEM_FAIL")


def status_subject(record, run_status, findings):
    """The subject line for one run's email.

    PASS means "nothing changed since the last run". It does not mean "the
    data is good" - keeping those two apart is the whole of DESIGN.md 7.6.1 -
    but nobody reading an inbox knows that, so a subject reading
    'PASS: daily integrity check, with data quality findings' is a plain
    contradiction, and a subject that contradicts itself gets the mail filed
    unread. It said exactly that until 2026-08-19.

    So on a run whose status is not itself news, the finding is the news and
    leads alone; the status is still in the body, in the table at the top. On
    a run that failed, the failure leads and the finding follows it, because a
    DATA_FAIL must be visibly a DATA_FAIL in an inbox.
    """
    label = JOB_LABELS[record.job]
    if not findings:
        return f"{SUBJECT_PREFIX} {run_status}: {label}"
    if run_status in HEADLINE_STATUSES:
        return f"{SUBJECT_PREFIX} {run_status}: {label}, and {FINDING_HEADLINE}"
    return f"{SUBJECT_PREFIX} {label.capitalize()}: {FINDING_HEADLINE}"


# ---------------------------------------------------------------------------
# The messages themselves
# ---------------------------------------------------------------------------


def status_notification(config, record, findings=()):
    """The alert for one run's status, and everything else that run found.

    One email per run rather than one per kind of problem. The summary is
    carried through as checks.py and backup.py wrote it - both write it for a
    non-technical reader precisely so that it can be the email body, so
    rewording it here would only lose the work (DESIGN.md 8.3) - minus the
    finding checks.py appends to it, which gets a section of its own below.
    """
    run_status = status_of(record)
    return notification(
        key=f"status:{record.job}",
        value=status_value(run_status, findings),
        roles=merge_roles(
            routing_for(config, run_status), finding_roles(config, findings)),
        subject=status_subject(record, run_status, findings),
        blocks=[
            at_a_glance(config, record),
            heading("What happened"),
            paragraph(summary_of(record)),
            *finding_blocks(findings),
            technical(technical_blocks(config, record)),
        ],
    )


def resolution_notification(config, record, previous, escalated, findings=()):
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

    It records the same composite value a status notification would, because
    an incident clearing while a validity finding is still open must leave the
    state describing the finding as well. Recording a bare PASS here would
    make the next poll see a changed value and send a second email about a
    situation nobody's inbox has changed.
    """
    roles = merge_roles(
        previous.get("roles") or routing_for(config, status_part(previous["value"])),
        (escalated or {}).get("roles"),
        finding_roles(config, findings),
    )
    label = JOB_LABELS[record.job]
    was = status_part(previous["value"])
    return notification(
        key=f"status:{record.job}",
        value=status_value(status_of(record), findings),
        roles=roles,
        subject=f"{SUBJECT_PREFIX} Resolved: {label}",
        blocks=[
            at_a_glance(config, record),
            heading("Resolved"),
            paragraph(
                f"The {label} is back to **{status_of(record)}**. The previous "
                f"alert reported **{was}**, and nothing further is needed."
            ),
            paragraph(summary_of(record)),
            *finding_blocks(findings),
            technical(technical_blocks(config, record)),
        ],
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
    return notification(
        key=f"escalation:system_fail:{job}",
        value="escalated",
        roles=roles,
        subject=f"{SUBJECT_PREFIX} The {label} has been failing for {days} days",
        blocks=[
            table([
                ("Job", label),
                ("Result", result_cell("SYSTEM_FAIL")),
                ("Failing for", f"**{days} days**"),
                ("Runs failed", f"{len(episode)} in a row"),
                ("First failure", local_text(config, episode[0].moment)),
            ]),
            heading("What this means"),
            paragraph(
                "This is an operational problem - signing in, object storage "
                "or the export service - rather than a problem with the data."
            ),
            paragraph(
                f"**No new backups have been taken for {days} days.** That is "
                f"why this message goes to more people than the first alert did."
            ),
            technical(technical_blocks(config, episode[-1])),
        ],
    )


def prune_paused_notification(config, episode, now):
    """Pruning held open past retention.paused_prune_alert_days.

    Same one-shot shape as the escalation above and for the same reason.
    """
    days = int(episode_days(episode, now))
    return notification(
        key="prune_paused",
        value="paused",
        roles=routing_for(config, "prune_paused"),
        subject=f"{SUBJECT_PREFIX} Backup pruning has been paused for {days} days",
        blocks=[
            table([
                ("What has stopped", "deleting old backups"),
                ("Paused for", f"**{days} days**"),
                ("Why", "the most recent integrity check reported DATA_FAIL"),
            ]),
            heading("What this means"),
            paragraph(
                "Pruning pauses while a data problem is open, so that a live "
                "incident cannot quietly remove the last good copy. That is "
                "deliberate, but it is not meant to last: the daily backups "
                "keep building up until they reach the limit in config.yml."
            ),
            paragraph(
                "**Fixing the check failure resumes normal pruning** on the "
                "next backup run."
            ),
            technical(technical_blocks(config, episode[-1])),
        ],
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
    return notification(
        key="no_monthly_candidate",
        value=month,
        roles=routing_for(config, "no_monthly_candidate"),
        subject=f"{SUBJECT_PREFIX} Nothing promoted to the monthly backup tier for {month}",
        blocks=[
            table([
                ("Month", month),
                ("Promoted to the monthly tier", "**nothing**"),
                ("Daily backups", "unaffected, still being taken"),
            ]),
            heading("What this means"),
            paragraph(
                f"A backup is promoted to the monthly tier only when its "
                f"paired integrity check returned PASS, and no set from "
                f"{month} has one."
            ),
            paragraph(
                "What is missing is the longer-term monthly copy. That is "
                "normally only noticed when somebody goes looking for a "
                "restore point from a month ago that was never created."
            ),
            technical(technical_blocks(config, record)),
        ],
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
    return notification(
        key=f"stale:{job}",
        value="stale",
        roles=routing_for(config, "stale"),
        subject=f"{SUBJECT_PREFIX} The {label} did not run",
        blocks=[
            table([
                ("Job", label),
                ("Was due at", local_text(config, slot)),
                ("Result written", "**none**"),
                ("Last reported", last),
            ]),
            heading("What this means"),
            paragraph(
                "A scheduled job that stops running looks exactly like one "
                "that keeps passing, so this is checked rather than waited "
                "for. The usual causes are a failed or disabled GitHub Actions "
                "workflow, expired credentials, or GitHub pausing the schedule "
                "after a long stretch with no commits."
            ),
            paragraph(
                "**No data is at risk.** The backups already taken are "
                "untouched. What has stopped is the taking of new ones."
            ),
        ],
    )


def weekly_summary_notification(config, records, history, now, summary_day):
    """Monday morning reassurance that the control is alive.

    Not a failure-detection mechanism - staleness does that (DESIGN.md 8.2) -
    so this reports rather than judges. It fires once because the dedup value
    is the date of the summary slot, which every poll on that Monday computes
    identically.
    """
    since = now - datetime.timedelta(days=SUMMARY_DAYS)
    rows = []
    for job in JOBS:
        label = JOB_LABELS[job]
        recent = [
            record for record in records_for(history, job) if record.moment >= since
        ]
        if not recent:
            rows.append((label, "0", "no runs", "-"))
            continue
        counted = {}
        for record in recent:
            counted[status_of(record)] = counted.get(status_of(record), 0) + 1
        breakdown = ", ".join(
            f"{count} {name}" for name, count in sorted(counted.items())
        )
        rows.append((
            label, str(len(recent)), breakdown, local_text(config, recent[-1].moment)
        ))

    overdue = []
    for job in JOBS:
        slot = last_due_slot(config, job, now)
        if slot and is_stale(records, job, slot):
            overdue.append(
                f"**{JOB_LABELS[job]}** - nothing since the slot at "
                f"{local_text(config, slot)}"
            )

    blocks = [
        paragraph(
            f"How the Water Licensed Works backups have been getting on over "
            f"the last **{SUMMARY_DAYS} days**."
        ),
        table(rows, headers=("Job", "Runs", "Results", "Most recent")),
    ]
    if overdue:
        blocks.append(heading("Overdue"))
        blocks.append(bullets(overdue))
    else:
        blocks.append(
            paragraph("**Nothing is overdue.** Every expected run has reported.")
        )

    return notification(
        key="weekly_summary",
        value=summary_day,
        roles=routing_for(config, "weekly_summary"),
        subject=f"{SUBJECT_PREFIX} Weekly summary, {summary_day}",
        blocks=blocks,
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
        # Whatever this run found that is invalid rather than merely changed.
        # It rides in the run's own email rather than in one of its own, so a
        # recipient gets one report of what the check found and not two
        # (DESIGN.md 7.6.1.1). Always empty for the backup job, which measures
        # nothing about the data.
        findings = outside_bc_lines(newest)
        previous = state.get(f"status:{job}")
        if (
            previous
            and status_part(previous.get("value")) in RESOLVING_STATUSES
            and status_of(newest) == "PASS"
        ):
            notifications.append(
                resolution_notification(
                    config, newest, previous,
                    state.get(f"escalation:system_fail:{job}"), findings,
                )
            )
        else:
            notifications.append(status_notification(config, newest, findings))

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
# THE ONE THING THIS JOB WRITES, AND THE ONLY PLACE IT WRITES IT.
#
# It lives in the bucket, under its own notify/ prefix - a sibling of status/
# and metrics/, never inside a backup tier. Decided 2026-08-18, reversing an
# earlier decision to keep it on the Jenkins host, which had assumed a
# filesystem this project does not really have: the Jenkins instance is a
# shared service administered by another team, the job is not pinned to one
# agent, and a state file on the agent that happened to run last poll is
# invisible to the one that runs the next. Silently, and the symptom is the
# hourly flood the whole design exists to prevent.
#
# The cost is that "the notify job never writes to the backup bucket" becomes
# "it writes one small object to one constant key and nothing else". That is a
# weaker sentence but the same actual risk, because the bucket issues a single
# full-access key pair (DESIGN.md 6.6) and this job has always held one.
#
# So the narrower claim is made enforceable rather than promised:
# save_state takes no key argument, STATE_KEY is a module constant, and
# tests/test_notify.py asserts that the only write in this file goes to that
# key and that no delete or copy call exists anywhere in it.
# ---------------------------------------------------------------------------


def state_key(config):
    """The one key this job writes, under the project's notify/ prefix."""
    return f"{config['storage']['paths']['notify']}state.json"


def load_state(bucket, config):
    """What was notified last time, or an empty state on the first run.

    A missing object is the first run and is not an error. The cost is one
    duplicate email per open condition, which DESIGN.md 8.5 accepts.

    An unreadable one is treated the same way and said so loudly, because the
    alternative - failing every poll until somebody deletes it by hand - is
    silence, and silence is what this job exists to prevent.
    """
    key = state_key(config)
    try:
        raw = bucket.client.get_object(Bucket=bucket.name, Key=bucket.prefix + key)
        return json.loads(raw["Body"].read().decode("utf-8")).get("signals", {})
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            logger.info("No %s yet, so this poll starts from nothing", key)
            return {}
        # Bad credentials, wrong bucket, endpoint unreachable. Not "there is no
        # state" - reporting it as that would send every open alert again.
        raise
    except ValueError as exc:
        logger.warning(
            "%s could not be read (%s), so this poll starts from nothing and "
            "may repeat an alert that has already been sent. It is rewritten "
            "on the next send.", key, exc,
        )
        return {}


def save_state(bucket, config, signals):
    """Record what was sent.

    Deliberately takes no key: the destination is state_key and nothing else,
    so no caller can point this at a backup. A put replaces the whole object in
    one call, so there is no half-written state to guard against the way a
    local file would need.
    """
    payload = {"version": STATE_VERSION, "signals": signals}
    bucket.client.put_object(
        Bucket=bucket.name,
        Key=bucket.prefix + state_key(config),
        Body=json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
    )


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def send_email(config, addresses, subject, body, html=""):
    """Send one message through the internal relay.

    Plain SMTP with no authentication and no TLS: this is the government
    relay on the internal network, reachable from the GIS server and from
    nowhere else, and it is what the existing staging script has always used.
    If it ever starts requiring credentials that is a change to make
    deliberately rather than a fallback to add now.

    Both versions of the body travel in one message. set_content writes the
    plain text part and add_alternative adds the formatted one after it, which
    is the order multipart/alternative requires - a client shows the last part
    it understands, so an HTML part added first would be the one nobody sees.
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
    if html:
        message.add_alternative(html, subtype="html")

    settings = config["notifications"]
    with smtplib.SMTP(
        host, settings["smtp_port"], timeout=settings["smtp_timeout_seconds"]
    ) as relay:
        relay.send_message(message)


def deliver(config, notifications, state, addresses, dry_run, html_out=None):
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
            if html_out:
                write_html_preview(notification, html_out)
            continue

        if recipients:
            try:
                send_email(
                    config, recipients, notification.subject,
                    notification.body, notification.html,
                )
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

    The plain text version is what gets printed, because it is the one a
    person can read in a terminal. --html-out writes the formatted version to
    files, which is how the layout is checked without mailing anybody.
    """
    print("=" * 72)
    print(f"WOULD SEND   {notification.subject}")
    print(f"to           {', '.join(recipients) or '(nobody - routed to no role)'}")
    print(f"roles        {', '.join(notification.roles) or '(none)'}")
    print(f"dedup key    {notification.key} = {notification.value}")
    print("-" * 72)
    print(notification.body)


def write_html_preview(notification, directory):
    """Save one message's formatted body where a browser can open it.

    Only ever called from a dry run. The formatted version is the one the
    recipients actually see, and reading it as markup in a terminal is not
    reading it at all - so wording and layout are reviewed by opening these
    files rather than by mailing somebody a draft.
    """
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    # The dedup key names the situation and is unique within one poll, which
    # is exactly what a file name here wants to be.
    name = notification.key.replace(":", "-") + ".html"
    written = folder / name
    written.write_text(notification.html, encoding="utf-8")
    print(f"html         {written}")


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


def poll(config, bucket, state, now, dry_run, html_out=None):
    """One poll: read the bucket, decide, send. Returns (state, failures)."""
    records = list_status_records(bucket, config)
    history = load_history(bucket, records, now)
    logger.info(
        "%d status object(s) under status/, %d read for this poll",
        len(records), len(history),
    )

    notifications = collect_notifications(config, records, history, state, now)
    return deliver(
        config, notifications, state, role_addresses(config, dry_run),
        dry_run, html_out,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Poll the Water Licensed Works status objects and send alert mail. "
            "Reads object storage, and writes one small state object and "
            "nothing else."
        )
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH),
        help="Path to config.yml (default: the copy alongside this repository).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Print what would be sent and send nothing. The state object is "
            "not written either, so a dry run cannot suppress a real alert."
        ),
    )
    parser.add_argument(
        "--html-out", default=None, metavar="DIRECTORY",
        help=(
            "With --dry-run, write the formatted version of every message to "
            "this directory as .html files, to be opened in a browser. Sends "
            "nothing and writes nothing to object storage."
        ),
    )
    arguments = parser.parse_args(argv)

    if arguments.html_out and not arguments.dry_run:
        parser.error("--html-out is a dry run option. Add --dry-run.")

    config = load_config(arguments.config)
    logging.basicConfig(
        level=config.get("logging", {}).get("level", "INFO"),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    bucket = connect_to_bucket(config)
    state = load_state(bucket, config)
    now = datetime.datetime.now(datetime.timezone.utc)

    updated, failures = poll(
        config, bucket, state, now, arguments.dry_run, arguments.html_out
    )

    if arguments.dry_run:
        # Writing it would record as sent something that was never sent, and
        # the real alert would then be deduplicated away.
        logger.info("Dry run: nothing sent and %s not written", state_key(config))
    elif updated == state:
        # Most polls change nothing, and rewriting an identical object every
        # hour would leave hundreds of noncurrent versions a month behind it -
        # the bucket keeps them for 60 days (DESIGN.md 6.6). Writing only on a
        # change keeps this to roughly one write a week.
        logger.info("Nothing changed, so %s is left as it is", state_key(config))
    else:
        save_state(bucket, config, updated)
        logger.info("Recorded what was sent in %s", state_key(config))

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
