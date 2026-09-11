---
title: Home
nav_order: 1
---

# Water Licensed Works — Backup and Integrity Checks

The `WATER_LICENSED_WORKS_LINES` and `WATER_LICENSED_WORKS_POINTS` layers in
ArcGIS Online are edited every day through a web application (QuickWins) and
by a number of direct editors, then pushed nightly into the BC Geographic
Warehouse (BCGW).
Before this project, neither layer had a backup, and nothing checked what the
nightly push carried downstream — a bad bulk edit was both undetectable and
unrecoverable.

This site documents what runs now: scheduled backups of both layers, and
daily checks that measure them and raise an alert when something looks
wrong.

## Who this is for

- **The data owner and the GIS team**, who receive the same alerts by
  design — start with the [User Guide]({{ site.baseurl }}{% link user-guide/index.md %}).
- **Whoever runs, fixes, or extends the pipeline** — see the
  [Developer Guide]({{ site.baseurl }}{% link developer-guide/index.md %}).

## What it does

- Exports both layers on a schedule and keeps a rotating, monthly, and
  yearly set of copies.
- Measures both layers every evening and compares each measurement against
  recent history.
- Sends an email when a measurement looks wrong, and stays silent when
  nothing has changed.
- Never changes a feature. Recovery is always a deliberate, manual decision.

## Quick links

- [What arrives by email]({{ site.baseurl }}{% link user-guide/alerts.md %})
- [What to do when a message arrives]({{ site.baseurl }}{% link user-guide/what-to-do.md %})
- [How it runs]({{ site.baseurl }}{% link developer-guide/how-it-runs.md %})
