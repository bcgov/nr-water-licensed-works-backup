---
title: Developer Guide
nav_order: 3
has_children: true
---

# Developer Guide

For whoever runs, fixes, or extends this pipeline.

The people who maintain this repository are GIS professionals who know
Python, ArcGIS, and spatial data — not necessarily software developers. The
code is written to be read top to bottom in a single file, without
following abstractions across a dozen others: functions rather than
classes, flat modules, and no magic numbers — every tunable value lives in
`config.yml`, with the reasoning for it in a comment beside the value.

## Pages in this guide

1. [How it runs]({{ site.baseurl }}{% link developer-guide/how-it-runs.md %}) —
   the two platforms this runs on, and why.
2. [Configuration and secrets]({{ site.baseurl }}{% link developer-guide/configuration.md %}) —
   `config.yml`, and where credentials live.
3. [Operating the pipeline]({{ site.baseurl }}{% link developer-guide/operating.md %}) —
   reading a run's output, what a failure means, and running things by hand.
4. [Restoring a layer in place]({{ site.baseurl }}{% link developer-guide/restore-inplace.md %}) —
   the restore runbook.
5. [Known limitations]({{ site.baseurl }}{% link developer-guide/limitations.md %}) —
   gaps in the check set, and what hasn't been verified.

## Technology

- **Python 3.11**, dependencies pinned in `requirements.txt`.
- **`arcgis`** for ArcGIS Online access, **`boto3`** for object storage,
  **`PyYAML`** for configuration. Nothing else without a reason.
- **GitHub Actions** for the scheduled backup and check jobs.
- **Jenkins**, on a separate internal server, for email notification only —
  see [How it runs]({{ site.baseurl }}{% link developer-guide/how-it-runs.md %})
  for why that's a second platform rather than one more Actions job.
- **`pytest`** for the test suite, built on known-answer tests rather than
  mocking.

The full repository layout is in
[README.md](https://github.com/bcgov/nr-water-licensed-works-backup/blob/main/README.md#repository-layout).
