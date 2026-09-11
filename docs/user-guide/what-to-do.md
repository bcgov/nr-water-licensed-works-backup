---
title: What to do when a message arrives
parent: User Guide
nav_order: 2
---

# What to do when a message arrives

Each kind of message is covered below: what it means, whether it needs
action, and who else has already seen it. See
[What arrives by email]({{ site.baseurl }}{% link user-guide/alerts.md %})
for how often these are sent.

## A data problem

**What it means.** A check found a change big enough to need a person — for
example, a drop in feature count well beyond ordinary editing, or a map
area that emptied out. The message names the check that fired and the
numbers behind it.

**Does this need action?** Yes. Start by reading the numbers in the
message. Often the right response is correcting a small number of records,
which is faster than a restore and keeps everything else intact. See
[Requesting a restore]({{ site.baseurl }}{% link user-guide/requesting-a-restore.md %})
if a restore does look like the right call.

**Who else knows.** The Water Business shared inbox and the maintainer
received the same message.

![Example data-problem alert, showing the job, result, and what happened]({{ site.baseurl }}/assets/img/alert-data-problem.png)

*An example of what this looks like. The numbers here are illustrative, not
a current finding.*

## A warning

**What it means.** Something moved more than usual, but not by enough to
call a problem — a bulk load of new records, or an area of the map
thinning out.

**Does this need action?** Not urgently. It is worth a glance, mainly so a
warning that keeps recurring can be raised as a real problem rather than
noticed only in hindsight.

**Who else knows.** The Water Business shared inbox and the maintainer.

## A record that cannot be right

**What it means.** A feature whose coordinates place it outside British
Columbia, or one with no shape at all. Unlike the two above, this is not
about something that changed today — it is a record the checks can tell is
wrong no matter when it was created, and it will keep appearing until it is
corrected or removed. The message names the affected records directly, so
whoever corrects them can find them.

The number and identity of these records will change over time as they get
corrected — this guide won't try to keep a running count, since each
message carries the current one.

**Does this need action?** Eventually, yes, but rarely urgently — these are
typically long-standing data issues rather than something that just broke.
Correcting them removes them from every future message.

**Who else knows.** The Water Business shared inbox and the maintainer, on
whichever message happens to carry the finding.

## A weekly summary

**What it means.** Sent every Monday morning, whatever happened that week.
It exists so that silence isn't the only sign the system is working.

**Does this need action?** No. It's confirmation, not a call to action.

## If something is unclear

If a message names an unfamiliar check, or it's unclear whether something
needs a response, contact the maintainer.
