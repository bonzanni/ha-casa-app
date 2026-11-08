---
last_reviewed: 2026-11-08
---

# Quiet scheduled job runs

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

A plugin job declaring `"quietWhenScheduled": true`, and what changes for a run of it that
a schedule started: no topic, no acknowledgement, no progress line, and nobody told when it
completes — until it interacts with the operator, which ends its quietness. What a job is and
how it runs in batches is
[`background-jobs.md`](background-jobs.md). How a schedule starts a job is
[`job-triggers.md`](job-triggers.md). How a plugin's delivered slots reach the operator is
[`plugin-delivered-slots.md`](plugin-delivered-slots.md).

## Mental model

**A scheduled run that usually finds nothing should say nothing.** A job such as a weekly
accounting check posts, for every run, a launch acknowledgement, a topic and a progress
line per batch. When the job declares `quietWhenScheduled`, a run its schedule started (the
record's starter is `scheduled`, `Started by: scheduled` in its prompts) does none of that.
The plugin speaks for itself instead: its `operator_message`, `operator_proposal`,
`operator_file` and `operator_link` slots still reach the operator's DM. A run that finds
something new can therefore still say so.

**Quiet is decided once, at launch, and recorded.** `background_jobs.initial_job_state`
stamps `origin["job"]["quiet"]` when the declaration has the option and the starter is
`scheduled`. Everything after the launch reads that flag through
`background_jobs.is_quiet_run`, or reads the record's missing topic. Runs that the operator
or an agent starts behave as before, and so do jobs without the option, even when a
schedule started them.

**Interaction ends quietness.** A quiet run that asks the operator something
(`ask_operator`), or that receives their reply to one of its posts, a tapped answer or an
approval's continuation, stops being quiet before the question is posted or the turn delivered:
`EngagementRegistry.promote_from_quiet` clears the stamp under the registry lock with the strict
writer, rolling it back when the write fails, and the interaction is then refused — the tool
returns an error, the reply or tap gets its one notice. The lock also waits out a pending
terminal write: a completion that commits wins, so the promotion is refused and the end stays
told to no one (a terminal job receives nothing); one that rolls back lets the promotion go
through. Whether the end is told is decided inside the terminal transition, from the stamp as
the transition commits it, so a promotion that commits first is always seen. Promoted, the run
is told at its end like any job, and a cut-off reply turn is no longer skipped as quiet. Its
turns' words are still withheld: it has no topic.

**A quiet run has no topic.** The launcher creates its record with `topic_id` unset. The
driver gives its turns a stream that withholds the words (`_WithheldStream`) and reports
them as delivered: they were not shown by design, not lost, so no launch-death or
not-delivered reading fires. Every engagement notice is skipped for a record with no topic.
That covers the progress line, the restart's resume line, the step-limit line and the
turn-failure lines. The topic send primitives refuse a missing topic outright, because the
Bot API would otherwise post the message into the group's General topic. The per-engagement
handler lock is keyed by the record's id when it has no topic, so two quiet runs never share
one. The plugin's posts target the origin's chat, the live operator stamped by the schedule's
fire, so the grant identity admits a quiet run without a topic. A record with no topic that
is not a quiet run is still refused.

## Contracts & invariants

**INV-BGJOB-010**: A run of a job declaring `quietWhenScheduled` that the scheduler started opens no topic and posts nothing to Telegram except its plugin's delivered slots and Casa's tellings that need the operator — an approval challenge, a cancelled or failed end; a completed end is told to no one and arms no end notification; every other run, and every job without the option, opens its topic and is told as before.

A cancelled or failed quiet run is told as any scheduled job's end is: through the resident's
notification, with its durable obligation and boot replay unchanged. A launch that fails on a
named fault before the run started is the exception, here as for every scheduled job: it is told
by the schedule's own "did not start" line, which is not retried
([`job-triggers.md`](job-triggers.md)). A completed one is not, since its
plugin has already posted whatever the operator needed to see. A resume that fails while the
launch is still registered takes the job's own arms (the sweep, or the finalize funnel), never
the direct error arms that tell only in a topic. A second start while a quiet run is live is
refused with `job_busy`, saying the run is in progress and to wait. There is no topic to
`/cancel` it in.

What it does not cover: the operator cannot `/cancel` or `/complete` a quiet run, since both
happen in a topic, and it has no start line to cancel it from (it was scheduled); they can ask
the resident to cancel it. A reply to one of its plugin's posts, or a tap on its question, goes
into it and ends its quietness (INV-BGJOB-015).

**INV-BGJOB-015**: A quiet run that asks the operator something, or receives their reply to one of its posts, a tapped answer or an approval's continuation, stops being quiet before the question is posted or the turn is delivered — through a strict write under the registry lock that waits out a pending terminal write and is rolled back, with the interaction refused, when it fails or the record has gone terminal; whether a run's end is told is decided inside its terminal transition from the record's stamp, so an interacting run is told at its end and a run nobody interacted with has its completed end told to no one.

## Failure behavior

A topic send attempted for a record without a topic raises `ValueError` instead of posting,
so nothing reaches the General topic. The topic paths of the launch aborts, the launch-death
report, the finalize funnel and the topic-state paint already skip a record without a topic.

The turn that completes a quiet run ends without a result and without a telling, which is
how an interrupted turn looks. The incomplete-turn check in `channels/telegram.py` logs that
end at INFO as deliberately not told, and posts nothing. A cancelled or failed quiet run's
end still logs the WARNINGs that it is telling the operator.

## Extension points

Another reason to run a job quietly would stamp the same `quiet` flag at launch. Every later
reader keys on that flag or on the missing topic, never on how the run was started.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/background_jobs.py::is_quiet_run`
- `casa/rootfs/opt/casa/background_jobs.py::is_quiet_origin`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::_WithheldStream`
- `casa/rootfs/opt/casa/channels/telegram.py::_handler_lock_key`
- `casa/rootfs/opt/casa/channels/telegram.py::_require_topic`
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
