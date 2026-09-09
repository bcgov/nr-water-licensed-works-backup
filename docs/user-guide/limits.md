---
title: What this does not cover
parent: User Guide
nav_order: 4
---

# What this does not cover

**This detects; it does not prevent.** A bad edit reaching the BC
Geographic Warehouse (BCGW) is not stopped by any of this. It is made
visible within a day and recoverable from a backup, which is a different
and smaller claim than prevention.

**A restore has a lag as well as a cost.** The downstream copy of the data
is corrected on its own next scheduled update, not the instant a restore
finishes. And as covered in
[Requesting a restore]({{ site.baseurl }}{% link user-guide/requesting-a-restore.md %}),
restoring an older copy discards every edit made since it was taken.

## Changes these checks cannot see

- **A bulk edit that rewrites the works-type field.** This is the largest
  gap. Feature counts, the map grid, the field structure, and shapes are
  all unchanged by an edit that only changes what a feature is coded as, so
  nothing here would notice it happening.
- **A feature moved a short distance**, if it stays within an area that
  already has other features nearby.
- **Equal numbers of features deleted and added** in the same place.
- **A moderate deletion in the busiest areas of the map**, where there are
  already thousands of features. The grid check that catches a localized
  change is naturally less sensitive wherever the map is already dense.

## Who made a change

Editor tracking is switched off on both layers, so nothing here can say who
made a given edit — only that the data changed. Turning tracking on is a
decision for the data owner, and would let a "who edited what" check be
added later.

## What can only be confirmed by hand

Three things can only be confirmed by actually looking, after a real
restore: that QuickWins still opens and saves normally, that the nightly
push to BCGW still runs cleanly, and that direct editing still works as
expected. The restore
process keeps a layer's identity — its web address, sharing, and styling —
exactly as it was, so there's no obvious way any of those three would
break. But confirming is a short check, and it costs nothing to do.
