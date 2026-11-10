---
last_reviewed: 2026-11-10
---

# Quiet scheduled job runs

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

A plugin job declaring `"quietWhenScheduled": true`, and what changes for a run of it that
a schedule started: nothing in the direct chat, and nobody told when it completes — until it
interacts with the operator, which ends its quietness. What a job is is
[`background-jobs.md`](background-jobs.md); how its session runs is
[`background-job-session.md`](background-job-session.md). How a schedule starts a job is
[`job-triggers.md`](job-triggers.md). How a plugin's delivered slots reach the operator is
[`plugin-delivered-slots.md`](plugin-delivered-slots.md). Its agent log, when one is
configured, is [`agent-logs.md`](agent-logs.md).

## Mental model

**A scheduled run that usually finds nothing should say nothing.** A job such as a weekly
accounting check runs on its schedule, and most runs find nothing new. When the job declares
`quietWhenScheduled`, a run its schedule started (the record's starter is `scheduled`,
`Started by: scheduled` in its prompts) says nothing in the direct chat, and its completed end
is told to no one. The plugin speaks for itself instead: its `operator_message`,
`operator_proposal`, `operator_file` and `operator_link` slots still reach the operator's DM. A
run that finds something new can therefore still say so. Its agent log, when a log group is
set up, is written like any job's — the log is read-only and needs no one.

**Quiet is decided once, at launch, and recorded.** `background_jobs.initial_job_state`
stamps `origin["job"]["quiet"]` when the declaration has the option and the starter is
`scheduled`. Everything after the launch reads that flag through
`background_jobs.is_quiet_run` (or `is_quiet_origin` over a frozen origin). Runs that the
operator or an agent starts behave as before, and so do jobs without the option, even when a
schedule started them.

**Interaction ends quietness.** A quiet run that asks the operator something
(`ask_operator`), or that receives their reply to one of its posts, a tapped answer or an
approval's continuation, stops being quiet before the question is posted or the message is
accepted for the job:
`EngagementRegistry.promote_from_quiet` clears the stamp under the registry lock with the strict
writer, rolling it back when the write fails, and the interaction is then refused — the tool
returns an error, the reply or tap gets its one notice. The lock also waits out a pending
terminal write: a completion that commits wins, so the promotion is refused and the end stays
told to no one (a terminal job receives nothing); one that rolls back lets the promotion go
through. An interaction with a run whose stamp another promotion has already cleared in memory
still goes through the promotion: it waits for that write and promotes again if the write rolled
back, so a question or reply never reaches a run that then goes quiet again. Whether the end is
told is decided inside the terminal transition, from the stamp as
the transition commits it, so a promotion that commits first is always seen. Promoted, the run
is told at its end like any job. A quiet
run that Casa parks (it stopped twice without finishing) needs the operator too, so it is
promoted the same way before its parking line is posted
([`background-job-session.md`](background-job-session.md), INV-BGJOB-017).

**A quiet run needs nothing a job does not already have.** It was scheduled, so it has no
start line. The plugin's posts target the origin's chat, the live operator stamped by the
schedule's fire, and a protected call binds to that operator as every job's does
(INV-BGJOB-023).

## Contracts & invariants

**INV-BGJOB-025**: A run of a job declaring `quietWhenScheduled` that the scheduler started posts nothing in the operator's direct chat except its plugin's delivered slots and Casa's tellings that need the operator — an approval challenge, a cancelled or failed end; a completed end is told to no one and arms no end notification; its agent log is written like any job's; every other run, and every job without the option, is told as before.

A cancelled or failed quiet run is told as any scheduled job's end is: through the resident's
notification, with its durable obligation and boot replay unchanged. A launch that fails on a
named fault before the run started is the exception, here as for every scheduled job: it is told
by the schedule's own "did not start" line, which is not retried
([`job-triggers.md`](job-triggers.md)). A completed one is not, since its
plugin has already posted whatever the operator needed to see. A job that cannot be resumed
ends through the finalize funnel, which tells a cancelled or failed end through the resident.
A second start while a quiet run is live is refused with `job_busy`, saying the run is in
progress on its schedule and to wait.

What it does not cover: a quiet run has no start line, so no Cancel button; the operator can
ask the resident to cancel it. A reply to one of its plugin's posts, or a tap on its question,
goes into it and ends its quietness (INV-BGJOB-015).

**INV-BGJOB-015**: A quiet run that asks the operator something, or receives their reply to one of its posts, a tapped answer or an approval's continuation, stops being quiet before the question is posted or the message is accepted for the job — through a strict write under the registry lock that waits out a pending terminal write and is rolled back, with the interaction refused, when it fails or the record has gone terminal; whether a run's end is told is decided inside its terminal transition from the record's stamp, so an interacting run is told at its end and a run nobody interacted with has its completed end told to no one.

## Failure behavior

A cancelled or failed quiet run's end still logs the WARNINGs that it is telling the
operator.

## Extension points

Another reason to run a job quietly would stamp the same `quiet` flag at launch. Every later
reader keys on that flag, never on how the run was started.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/background_jobs.py::is_quiet_run`
- `casa/rootfs/opt/casa/background_jobs.py::is_quiet_origin`
- `casa/rootfs/opt/casa/tools.py::_told_nowhere`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.promote_from_quiet`

**Tests**
- `tests/test_quiet_scheduled_jobs.py`
- `tests/test_job_dm_attention.py`
- `tests/test_job_dm_posts_registry.py`

**Related**
- [`architecture/background-jobs.md`](../architecture/background-jobs.md)
- [`architecture/job-triggers.md`](../architecture/job-triggers.md)
- [`architecture/plugin-delivered-slots.md`](../architecture/plugin-delivered-slots.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
<!-- END SOURCEMAP -->
