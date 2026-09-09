---
title: Operating the pipeline
parent: Developer Guide
nav_order: 3
---

# Operating the pipeline

## Reading a metrics file

Written by every check run to `metrics/<date>.json`, one per layer. This is
the full set of measurements, independent of whatever verdict was reached
about them.

| Field | What it holds |
|---|---|
| `feature_count` | Live count, from the service — never the cached estimate. |
| `extent` | The bounding box, from a live query. |
| `spatial_bins`, `features_inside_grid`, `features_outside_grid` | Counts per grid cell, and the two-way split the grid partitions the layer into. |
| `total_length` | Lines only — total length in metres. |
| `schema_fingerprint` | Field names, types, and coded-value domains. |
| `missing_counts`, `missing_rates_percent` | How many features are blank in the two identifying fields. |
| `value_counts`, `values_outside_domain` | Every works-type code in use, and which ones aren't in the defined domain. |
| `absent_fields` | Any field the artifact or a prior run expected that the layer no longer has. |
| `features_with_null_geometry`, `objectids_with_null_geometry` | Count and identifiers of features with no shape. |

**Three fields were added on 2026-09-08**, all from the same drill:
`features_with_null_geometry` / `objectids_with_null_geometry` (a shape
could be missing entirely and nothing previously measured that),
`absent_fields` (a dropped field previously went unnoticed because a
restore or schema change silently leaves a column out), and
`missing_rates_percent` — renamed from an older, narrower field, because
the query behind it now also counts blank strings, not only true nulls,
which changed what the number means enough to rename it rather than let it
keep an old name with new behaviour underneath.

## Reading a status object

Written by every run — backup or check, pass or fail — to
`status/<run_id>.json`. This is what the notification job reads; it never
opens a metrics file itself.

| Field | What it holds |
|---|---|
| `run_id` | Also the object's key. Prefixed `backup-` or `checks-` — this prefix is load-bearing, since it's how a check status is told apart from a backup status when deciding what's eligible for monthly promotion. |
| `status` | One of `PASS`, `BASELINE`, `WARN`, `DATA_FAIL`, `SYSTEM_FAIL` — the complete set. |
| `summary` | Written for a non-technical reader. This becomes the email body. |
| `details` | The specifics behind the summary, including any validity finding. |
| `rules` | Names only, of whichever checks broke — added so the notification job can tell two different failures apart without parsing prose. |
| `code_version` | The commit (or `<commit>-dirty` for an uncommitted change) that produced this run, so a data change can be told apart from a code change. |
| `workflow_run_url` | Link back to the Actions run. `null` outside Actions. |

## `SYSTEM_FAIL` versus `DATA_FAIL`

These are kept deliberately distinct. An authentication failure or a
storage timeout is an operational problem, not a data anomaly — it should
alert just as loudly, but it must never be treated as a verdict about the
data. Only `DATA_FAIL` means the data itself looks wrong.

This distinction is what a future Phase 2 (calling this check logic
directly from the nightly staging push, to block the push on a bad day) is
designed to rely on: fail closed on a data problem, fail open on a system
problem. A flaky API must never be allowed to hold up the nightly push for
a reason that has nothing to do with data quality.

## A third kind of failure: nothing happened at all

A workflow that stops running produces no status object, so nothing above
applies — there's no `status` to read. The notification job watches for
this separately, by checking whether an expected slot has passed with
nothing written, and alerts only the maintainer:

![Example stale-job notice, showing the job, when it was due, and that no result was written]({{ site.baseurl }}/assets/img/alert-stale-job.png)

*This one never reaches the data owner — it's about the pipeline, not the
data.*

## Running things by hand

- **`preflight.py`** — read-only, and writes nothing to ArcGIS Online. It
  checks that required environment variables are present, that
  authentication works, that the layer facts this project depends on still
  hold, and that object storage round-trips. Run this first when
  diagnosing a `SYSTEM_FAIL`, or after a credential rotation.
- **`run_checks.py --config path/to/config.yml`** — runs the check job
  against any configuration, not only the default. Useful for testing
  against something other than the production configuration.
- **The test suite** — `pytest`. Check rules are tested as known-answer
  tests (a given metrics dict and threshold must produce a given status),
  not with mocking frameworks. 271 tests pass as of 2026-09-08.
