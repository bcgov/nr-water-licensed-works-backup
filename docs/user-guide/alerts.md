---
title: What arrives by email
parent: User Guide
nav_order: 1
---

# What arrives by email

**Everything below goes to the data owner and to the Water Business shared
inbox.**

## What reaches the data owner

| | What it means |
|---|---|
| **A data problem** | A check found something that needs a person: a drop in feature count, a populated map area emptying, the field structure changing. The message says which check and by how much. |
| **A warning** | Something moved more than expected, but below the level that needs action — a bulk load, an area thinning out, fields going blank. |
| **A record that cannot be right** | A feature whose coordinates place it outside British Columbia, or one with no shape at all. It names the affected records, and it appears on every message until they are corrected. |
| **A weekly summary, Monday morning** | Confirmation the whole system is running, with the week's results. |

## What stays with the maintainer

| | Why |
|---|---|
| **A system failure** | Object storage timed out, or a sign-in failed. Nothing to do with the data itself. If it lasts more than a few days it reaches the data owner too, because at that point backups have stopped running. |
| **A job that did not run** | The pipeline itself has stopped. The maintainer's to fix. |
| **Housekeeping** | Old copies not being cleaned up, or a monthly copy not being created. |

See [What to do when a message arrives]({{ site.baseurl }}{% link user-guide/what-to-do.md %})
for what action, if any, each of these calls for.

## Three things about the volume

**A clean check is silent.** No news is the good outcome. The weekly
summary exists precisely so that silence is not the only evidence the
system is alive.

![Example weekly summary, showing a table of runs and results for the backup and check jobs]({{ site.baseurl }}/assets/img/alert-weekly-summary.png)

*An example weekly summary, for a quiet week with nothing overdue.*

**A problem lasting a week is one email, not seven.** The data owner is
told when a situation starts and when it ends. It repeats only if the
situation genuinely changes — a second bad record, or a different check
failing.

**An alert that says a job did not run is about the pipeline, not about
the data.** Those go to the maintainer only.

If any of this turns out to be noise, say so — routing who gets told about
what is a small configuration change, not a rebuild.
