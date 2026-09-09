---
title: How it runs
parent: Developer Guide
nav_order: 1
---

# How it runs

## Two scheduled jobs, one shared record

**Backup** runs Monday, Wednesday, and Friday. **Checks** run every day.
Both are GitHub Actions workflows, triggered on `schedule` and
`workflow_dispatch` only — never on anything that fires from a pull
request, which matters on a public repository with secrets in it.

Every run of either job writes a **status object** to object storage when
it finishes, whether it passed or failed. That status object is the entire
interface between the two jobs described below — nothing else needs to
know what happened inside a run. See
[Operating the pipeline]({{ site.baseurl }}{% link developer-guide/operating.md %})
for what the object contains.

## Why notification runs somewhere else

Alerts go out by ordinary internal email, and the mail relay they use is
only reachable from inside the organisation's own network — not from a
GitHub-hosted runner. So a small job on an internal server (Jenkins) polls
the status objects once an hour and sends the mail. It has no `arcgis`
dependency and no file geodatabase handling — just enough to read a status
object and send an email — which is deliberate: that server runs a shared,
managed Python environment that this project has no business adding
packages to.

The failure modes stay separate this way. If that internal server is down
overnight, the backup still happened and its result is durably recorded;
only the alert is delayed. Under a single-platform design, the same outage
would mean the backup never ran at all.

**The notification job's own code and configuration are published to
object storage on every push** (`publish_notify_code.py`, wired to
`.github/workflows/publish_notify_code.yml`), and the job downloads and
checksum-verifies them at the start of every run. This exists because the
servers that job runs on cannot always be relied on to fetch straight from
GitHub — the details of why are internal network configuration, not
something this guide needs to carry, but the practical effect is that the
job's code never depends on a manual copy step and can't silently go stale.

## Staleness, not just failure

A scheduled job that silently stops running looks identical to one that
keeps passing: nothing happens either way. The notification job also knows
the expected schedule, so it checks for that directly — if an expected
daily check or an expected backup slot has passed with no result recorded
at all, that itself raises an alert.

Staleness is measured **from the expected slot**, not as hours since the
last run. Measuring hours-since would misfire every weekend, since the gap
between a Friday backup and a Monday one is intentionally wider than the
gap between any two weekdays. A grace period (configured in `config.yml`,
see
[Configuration and secrets]({{ site.baseurl }}{% link developer-guide/configuration.md %}))
absorbs an ordinary late start before anything is treated as a genuine
failure to run.

## One email per situation, not one per poll

The notification job polls hourly, but email volume follows *changes in
status*, not the polling interval. A problem that persists for five days
produces two emails — one when it starts, one when it clears — regardless
of how often the job polls. Polling frequently buys latency (how soon an
alert reaches an inbox), not email volume.

## Repository layout and import direction

The full directory layout is in
[README.md](https://github.com/bcgov/nr-water-licensed-works-backup/blob/main/README.md#repository-layout).
One rule from it is worth restating here because it's easy to violate by
accident: **`checks.py` must never import `backup.py`.** `checks.py` has to
run in a plain Python environment with no file geodatabase support, and
`backup.py` pulls in the library that reads one. Anything the backup and
check jobs need to share lives in `status.py` instead, which imports
nothing but the object storage wrapper and the standard library.
