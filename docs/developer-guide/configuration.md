---
title: Configuration and secrets
parent: Developer Guide
nav_order: 2
---

# Configuration and secrets

## `config.yml` is the single source of settings

There are no magic numbers in the code. Every schedule, retention count,
threshold, and routing rule lives in `config.yml`, read once at the start
of a run and passed explicitly to the functions that need it.

| Section | Controls |
|---|---|
| `layers` | Item IDs and layer indexes for the lines and points services. |
| `storage` | The bucket and this project's prefix within it. |
| `backup` | Export timeouts and artifact validation. |
| `retention` | How many rotating, monthly, and yearly copies are kept. |
| `promotion` | Which check outcomes make a backup eligible to become a monthly copy. |
| `schedule` | The days and grace periods the notification job uses to judge a run overdue. |
| `checks` | The spatial grid definition and other check mechanics. |
| `thresholds` | Per-layer values that decide `PASS` / `WARN` / `DATA_FAIL`. |
| `notifications` | Routing: which outcome reaches which role. |
| `logging` | Log verbosity. |

**A tunable value's reasoning lives in the comment beside it, not in a
separate document.** Several thresholds started as placeholders and were
later replaced with values derived from measurement — read the comment
before changing a number, since it usually explains what the value was
set *from*, and changing it without updating that reasoning leaves the
next reader with a number and no way to judge it.

The spatial grid definition (`checks.spatial_grid`) is the one setting that
is explicitly **not** a threshold to tune: changing it invalidates every
previously stored measurement, because grid cells are only comparable
against cells of the same size in the same place.

## Secrets never live in `config.yml`

This is a public repository, so credentials come from environment
variables only, in one of three independent places:

- **GitHub Actions repository secrets**, for the backup and check jobs.
- **The Jenkins credential store**, for the notification job.
- **A developer's own machine**, to run anything by hand.

These three sets are not the same, and nothing propagates between them.
The full list of variables, and which of the three each one belongs to, is
in
[README.md](https://github.com/bcgov/nr-water-licensed-works-backup/blob/main/README.md#environment-variables) —
this page won't repeat it, since a second copy is a copy that can drift
out of date.

Two things worth knowing that aren't obvious from the table alone:

- **Recipient addresses are environment variables too**, one per
  notification role. `config.yml`'s `notifications.roles` maps a role name
  (for example, the data owner) to the name of the variable holding that
  address — never the address itself — so the routing logic is reviewable
  in a public repository without publishing anyone's inbox.
- **The restore tool authenticates separately from everything else**, with
  its own `AGO_USERNAME_RESTORE` / `AGO_PASSWORD_RESTORE` credentials
  rather than the pipeline's own. Nothing that runs on a schedule reads
  those two variables, and nothing the restore tool reads is available to
  the scheduled pipeline.
