---
title: Known limitations
parent: Developer Guide
nav_order: 5
---

# Known limitations

## Residual detection risk

Every check in this project asks some version of "has this changed?". A few
kinds of change do not trip any of them:

- **A bulk edit that rewrites the works-type field.** Every distinct code
  and its per-code count are measured on every run, but no rule acts on
  them — the works-type data has enough pre-existing, uncorrected typos
  that any rule tight enough to catch a bad bulk edit would also fire on
  routine data entry, forever. This is the single largest gap in the check
  set: feature count, extent, the spatial grid, the schema, and geometry
  are all unchanged by an edit that only changes what a feature is coded
  as. The per-code counts are already stored, so closing this needs no new
  measurement — only a threshold, decided from enough history to set one
  sensibly.
- **A feature moved a short distance within an area that already has
  other features.** The spatial grid catches a localized change in an
  otherwise-quiet area; it cannot see a move that stays inside a cell that
  was already populated.
- **Equal numbers of features deleted and added in the same location.**
  Feature count and the grid are both blind to a swap that nets to zero.
- **A moderate deletion in the densest grid cells.** Sensitivity is not
  uniform across a fixed grid — a deletion that would be obvious in a
  sparse cell can be a small percentage of a busy one. An adaptive grid
  would address this and was judged not worth the added complexity; it is
  accepted risk rather than an oversight.
- **No attribution.** Editor tracking is off on both layers, so nothing
  here can say who made a change, only that one happened. Enabling it is a
  service-level toggle and a data-owner decision; it would make an
  edits-by-user check possible afterward, but only going forward — it
  doesn't backfill history.

## Not verified by this project

Three things can only be confirmed against a real production restore, and
are out of scope here:

- That QuickWins opens and edits normally afterward.
- That the nightly push to the BC Geographic Warehouse (BCGW) runs cleanly
  afterward.
- That direct editing in desktop GIS tools is unaffected.

The residual risk is judged low: the restore path never changes a layer's
item ID, service URL, sharing, or symbology, so there's no obvious
mechanism by which a dependent application would break — but "no obvious
mechanism" is a reason to expect it to be fine, not a substitute for
checking once after an actual restore.

## Service reconstruction is not a written runbook

[Restoring a layer in place]({{ site.baseurl }}{% link developer-guide/restore-inplace.md %})
covers putting data back into an existing hosted layer, which is the
normal case. Rebuilding the hosted *service* itself — if the item were
lost rather than just its data, a much lower-probability event — is not a
step-by-step procedure today. `servicedef.json`, captured in every backup
set, is the raw material such a rebuild would start from, but turning that
into a runbook is still open.
