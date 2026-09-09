---
title: User Guide
nav_order: 2
has_children: true
---

# User Guide

For the data owner and the GIS team. No technical background is needed.

You and the Water Business shared inbox receive the same messages from this
system. This guide is written for both.

## What this protects

Two datasets: a **layer** of lines and a **layer** of points, both recording
licensed water works. Both are edited constantly through a web application
(QuickWins) and by GIS staff working directly in the data.

The lines and points are copied — **backed up** — three times a week, and
measured every evening. Before this existed, neither layer had a backup, and
nothing checked what the nightly push into the BC Geographic Warehouse
(BCGW) carried downstream. A large mistaken edit would have gone unnoticed
and could not have been undone.

## What is checked

Each evening, both layers are measured and compared against how they looked
before. A **feature** is one record — one line or one point. The checks
look at:

- **How many features** each layer holds.
- **Where the features are.** The overall bounding box, and a finer check
  that divides British Columbia into a grid of 50 km squares and counts
  features in each square — this catches a change confined to one small
  area even when the total count barely moves.
- **The field structure** (the **schema**) — the names and types of the
  columns each layer has, and the list of valid codes for the works type.
- **Blank values** in the two fields that identify a work.
- **Features with no shape at all** — a record that exists but does not
  draw anywhere on a map.

Every measurement is compared against the previous check, the last 30 days,
and the most recent monthly copy, so both a sudden change and a slow drift
can be seen.

## What's kept, and for how long

| Copies | How many are kept | Roughly | Kept in |
|---|---|---|---|
| Recent (rotating) | 8 | 2.5 weeks | `config.yml`, under `retention` |
| Monthly | 12 | 1 year | `config.yml`, under `retention` |
| Yearly | All of them | Indefinitely | `config.yml`, under `retention` |

A recent copy is never deleted while a serious data problem is open, so an
unresolved issue can't quietly cost the team its last good copy. The
measurements themselves — not the data, just the numbers from each day's
check — are kept for over a year, which is what lets a slow drift be
recognised as a drift rather than as an ordinary day.

## Pages in this guide

1. [What arrives in your inbox]({{ site.baseurl }}{% link user-guide/alerts.md %}) —
   the kinds of messages this system sends, and what silence means.
2. [What to do when a message arrives]({{ site.baseurl }}{% link user-guide/what-to-do.md %}) —
   for each kind of message, what it means and whether it needs you.
3. [Requesting a restore]({{ site.baseurl }}{% link user-guide/requesting-a-restore.md %}) —
   how to ask for a layer to be put back, and what it costs.
4. [What this does not cover]({{ site.baseurl }}{% link user-guide/limits.md %}) —
   the limits of these checks, stated plainly.
