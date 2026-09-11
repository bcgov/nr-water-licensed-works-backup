---
title: Requesting a restore
parent: User Guide
nav_order: 3
---

# Requesting a restore

A restore puts a whole layer back to an earlier saved copy. It is a
decision, not a button — nobody, including the maintainer, can start one
without the data owner's approval.

## What it costs

**A restore is not a repair.** It replaces every feature in the layer with
the features from the chosen copy, so every edit made since that copy was
taken is discarded along with whatever went wrong. Backups are taken three
times a week, so that can mean several days of real work.

Often the right answer is not a restore at all. If the problem is a
handful of records, correcting them directly is faster, and it keeps
everything else exactly as it is.

## Before asking for one

1. **Read the alert.** It names the check that fired and the numbers behind
   it.
2. **Weigh what would be lost against what is wrong.** A restore is worth
   it when the damage is larger than the edits it would discard — not
   automatically whenever a check fails.
3. **Decide, and say so explicitly.** Tell the maintainer to proceed with a
   restore. That approval is recorded as part of the process.

## What happens next

The maintainer picks the most recent saved copy from before the problem —
usually the most recent one whose own check passed — and runs the restore.
It keeps the layer's identity: its web address, its sharing settings, and
how it's styled all stay exactly as they are. Only the features themselves
are replaced.

Restoring a layer with tens of thousands of features takes minutes, not
hours, once it starts. The slow part of a recovery is deciding to do it,
not doing it.

For exactly what the maintainer runs, see the
[developer guide's restore runbook]({{ site.baseurl }}{% link developer-guide/restore-inplace.md %}).

## What a restore doesn't do

It doesn't fix why the problem happened. If the cause was a process rather
than a one-off accident, the same problem can happen again. And it doesn't
immediately fix the copy of the data used further downstream — that
corrects itself on its own next scheduled update, not the moment the
restore finishes.
