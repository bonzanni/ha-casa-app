---
last_reviewed: 2026-09-17
---

# Background jobs

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Plugin-declared background jobs: the `casa.jobs` declaration, the `<jobs>` block a resident
sees, `start_job`, the batch loop that runs a job as a specialist engagement, the
`report_job_progress` tool, and resuming a job after a restart. The engagement a job runs
in — its topic, turn admission, completion gate and finalization — is described by
[`engagements.md`](engagements.md) and the documents it routes to; the per-role engagement
slot is in [`delegation.md`](delegation.md).

## Mental model

A job belongs to the plugin that declares it, not to a specialist. A plugin's manifest lists
jobs under `casa.jobs`, each naming one of its skills, a title, a batch cap (`unlimited` or
a positive integer) and an optional per-batch turn limit. Any specialist whose resolved
plugin set includes that plugin can host the job; the qualified job name is
`<plugin manifest name>:<job name>`.

The assistant sees the jobs its delegates can host in a `<jobs>` block and starts one with
`start_job(job, task, context)`. The job runs as an ordinary interactive engagement of the
host specialist, with three differences: the engagement record carries the job's counters
under `origin["job"]`, the specialist's client is opened with the job's per-batch turn limit
and the `report_job_progress` grant, and the launch prompt asks only for a one-line
acknowledgement.

Casa then drives the job itself. Every batch is a system turn (`Batch <n> of "<title>":
continue the job.`) delivered through the same path as an operator message. A batch ends
when the specialist ends its turn or runs out of turns — running out is not a failure. The
specialist reports each batch with `report_job_progress`, which posts one line in the topic.
Messages the operator writes in the topic are delivered as their own turns, between
batches. The job ends when the specialist calls `emit_completion`, when the operator
`/cancel`s it, or when Casa fails it (a batch that raised or was cut off, no progress, or
the batch cap).

## Contracts & invariants

**INV-BGJOB-001**: A job engagement's next batch is started only after a turn has ended, only while the record is live, the ended turn was not cut off, no turn is queued and no turn delivery is in progress; the batch is counted, the previous batch judged and the batch cap checked in the same synchronous step that admits it.

`background_jobs.job_after_turn` runs after every turn of a job engagement — the launch
turn (from the launch owner), a batch or an operator message (from the channel's turn
delivery). It does not care which kind of turn ended; `start_next_batch` is the only place
a batch is counted or started. "In progress" is a per-engagement count of live turn-delivery
owners (`turn_owner_started` / `turn_owner_finished`), not the driver's per-turn lock,
which is released before the turn's final stream delivery. Because the batch's system turn
is admitted as unread input synchronously at entry, a later quiet check sees it, so at most
one batch is queued or running. A restart resume calls `start_next_batch` directly.

What it does not cover: an operator message that arrives after a batch's quiet check and
before that batch is admitted queues behind the batch and is read one batch later.

**INV-BGJOB-002**: A job fails through the engagement finalize funnel, with the reason and its last reported progress, when three consecutive batches make no progress, when a batch would exceed its declared batch cap, when a batch's delivery raises, or when a batch is cut off before finishing.

A batch makes progress when it called `report_job_progress` and its `remaining` count went
down, or when there is no count to compare (none given, or no previous one). Every
`cancelled` or `error` ending of a job engagement appends `Last progress: <summary>` to the
text the topic and the resident receive, so `/cancel` also reports what the job had done.

The declaration is strict: `plugin_store.manifest_jobs` refuses a non-list, an unknown
entry field, a bad or duplicate name, an empty skill, an over-long or multi-line title or
summary, and a `batches` or `turnsPerBatch` that is not a positive integer (a boolean is not
one), with `jobs_invalid`; install validation also refuses a job whose
`skills/<skill>/SKILL.md` is missing. An already stored artifact failing the same checks is
excluded from resolution with that reason.

The `<jobs>` block is rendered only for a resident whose allowed tools include `start_job`
(the assistant), over its currently available delegates in declared order, one line per
job: qualified name, title, summary (or title) and the host's display name. A job two
delegates can host is listed once, under the first; `start_job` resolves it the same way.
A job declared by a plugin installed mid-conversation does not appear in a conversation
already open: a session's system prompt is fixed when the session is created. The resume
gate is what resolves it — the conversation's structural surface no longer matches what it
was registered with, so the next turn starts a fresh session carrying the new block
(INV-TURN-012, `architecture/turn-loop.md`).

An unknown job returns `job_not_declared` naming the startable jobs; a voice caller gets
`job_needs_text_channel`. Everything else — ACL, depth, availability, the spawn cap, the
engagement slot and channel checks — is the shared interactive launch that
`delegate_to_agent(mode='interactive')` uses, and a successful start returns its `pending`
envelope plus the job, title, agent and a one-line message.

`report_job_progress(summary, done=None, remaining=None)` is granted only to a job
engagement's session. Outside a live job it returns `not_a_job`; a negative or boolean count
returns `invalid_arguments`. It posts `📊 Batch <n>: <summary> · <done> done · <remaining>
left` (the summary's first line, at most 300 characters; counts only when given), records
the report and persists `origin["job"]`.

Resume options rebuild the job's per-batch turn limit and its progress grant from
`origin["job"]`, so a resumed job session keeps both.

## Failure behavior

**A restart during a job.** Boot re-idles live engagements as for any engagement. Once
channels and residents are up, `casa_core._resume_background_jobs` posts `↻ Resuming
"<title>" after a restart.` in each live job's topic and calls `start_next_batch`; the batch
cut by the restart is judged like any other. A failure resuming one job is logged and the
others still resume. Engagement permits are not restored at boot.

**A batch handoff is refused.** `deliver_system_turn` returns false when the engagement is
terminal or cannot be resumed; its resume-failure path has already surfaced that, and the
loop only logs it.

**A plugin's jobs cannot be listed.** An error resolving one specialist's plugins omits that
specialist's jobs; the other delegates' jobs are still listed.

## Extension points

A new job field belongs in `manifest_jobs` and `JobDecl`. Jobs declared by a plugin
installed on a resident rather than a specialist are a planned extension: host resolution
(`find_job_host`) and the loop key on the job and the engagement record, not on the
specialist, so a new host kind adds a branch there rather than a second loop.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/background_jobs.py`
- `casa/rootfs/opt/casa/plugin_store.py::manifest_jobs`
- `casa/rootfs/opt/casa/agent.py::_render_jobs_block`
- `casa/rootfs/opt/casa/tools.py::start_job`
- `casa/rootfs/opt/casa/tools.py::report_job_progress`
- `casa/rootfs/opt/casa/casa_core.py::_resume_background_jobs`

**Tests**
- `tests/test_background_jobs_declaration.py`
- `tests/test_start_job.py`
- `tests/test_background_jobs_loop.py`

**Related**
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/engagement-turn-admission.md`](../architecture/engagement-turn-admission.md)
<!-- END SOURCEMAP -->
