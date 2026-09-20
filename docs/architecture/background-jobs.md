---
last_reviewed: 2026-09-19
---

# Background jobs

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Plugin-declared background jobs: the `casa.jobs` declaration, the `<jobs>` block a resident
sees, `start_job`, specialist and resident-plugin worker launches, the batch loop, the
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

A resident-hosted launch uses a dedicated `kind="plugin"` engagement. Its
`role_or_type` names the resident, while `origin["plugin_job"]` pins the plugin registry
name and launch model. The record keeps one declaring artifact and the numeric batch
turn limit (the declaration's override, otherwise the resident's configured limit).
The worker loads that artifact's skills and MCP servers, plus only `report_job_progress`
and `emit_completion` from Casa. Its native grants are `Skill` and `ToolSearch`;
`Agent`, `Task` and `AskUserQuestion` are denied. Empty settings sources, default
permission mode and the fail-closed callback keep the resident's settings and tools out.
The Casa-owned prompt asks the worker to park items needing answers and report progress;
it supplies no resident persona or memory. Plugin authorization and result-contract hooks
still apply. Its working directory is `/data/engagements/<id>/plugin-job`.

Casa then drives the job itself. Every batch is a system turn (`Batch <n> of "<title>":
continue the job.`) delivered through the same path as an operator message. A batch ends
when the specialist ends its turn or runs out of turns — running out is not a failure. The
specialist reports each batch with `report_job_progress`, which posts one line in the topic
and says whether the batch moved the job toward completion.
Messages the operator writes in the topic are delivered as their own turns, between
batches. The job ends when the specialist calls `emit_completion`, when the operator
`/cancel`s it, or when Casa fails it (a batch that raised or was cut off, no progress, or
the batch cap).

## Contracts & invariants

**INV-BGJOB-001**: A job engagement's next batch is started only while the record is live, the ended turn was not cut off, no turn is queued and no turn delivery is in progress; the previous batch is judged, the batch number chosen and the batch cap checked in the same synchronous step that admits it, and that batch is counted only once the hand-off has been accepted — a refused hand-off leaves every counter untouched.

`background_jobs.job_after_turn` runs after every turn of a job engagement — the launch
turn (from the launch owner), a batch or an operator message (from the channel's turn
delivery). It does not care which kind of turn ended; `start_next_batch` is the only place
a batch is counted or started. "In progress" is a per-engagement count of live turn-delivery
owners (`turn_owner_started` / `turn_owner_finished`), not the driver's per-turn lock,
which is released before the turn's final stream delivery. Because the batch's system turn
is admitted as unread input synchronously at entry, a later quiet check sees it, so at most
one batch is queued or running. A restart resume and the periodic job sweep call
`start_next_batch` directly. The judgment and next batch number are staged synchronously;
they are committed and persisted only after the channel accepts the hand-off. A refused
hand-off changes no batch or progress counters.

What it does not cover: an operator message that arrives after a batch's quiet check and
before that batch is admitted queues behind the batch and is read one batch later.

**INV-BGJOB-003**: A job that is not progressing is continued by the periodic job sweep — no turn queued, no delivery owner live, no launch enrolled, and no advance for the stall window — and the sweep's own decision to end a job rests only on three of its continuations having been refused, never on a reading of the record's state; a continuation it attempts may still end the job through the paths that own that outcome (an engagement with no session to resume, the batch cap, the no-progress guard), and a hand-off refused because Casa is stopping is not a failed continuation and leaves the record live for boot.

The sweep does not try to decide whether a job is still launching. Seven review rounds established that no
reading of a live record answers that reliably — a record is published before its creation persists, a
launch is enrolled a moment later still, a clearance clamp clears a started job's session pointer, and a
terminal write that rolls back passes through `error` on its way back to `active`. Each reading that looked
sufficient left some live job permanently ignored.

So the sweep reads only enough to stay out of the way — a queued turn, a live delivery owner, an enrolled
launch, a record younger than the stall window — and its one power over a job it cannot prove is running is
to CONTINUE it. Continuing a job that was still launching costs an admitted turn that the driver serialises
behind the launch anyway; nothing is ended on a state read. A job ends here only when three continuations
the sweep itself attempted were refused, and then through the finalize funnel with its last progress. What
this does not cover: a job whose session cannot be resumed at all is ended by that path on the first
attempt, which is the same telling an operator would get from any unresumable engagement.

The sweep is the single owner of a job's liveness, for both host kinds. It exists because
continuation otherwise rides one-shot events — a hook after a turn, one boot pass — so any
failure between them (a resume that raises, a context rebuild that cannot be established, a
restart whose progress notice could not be posted, a refused hand-off) stopped the work
permanently. What it does not cover: a job still inside its launch turn belongs to the launch
owner, and a restart there ends it with the launch failure's own notice rather than resuming
it.

**INV-BGJOB-004**: A resident-hosted job's session is offered exactly its declaring plugin's tools, `Skill`, `ToolSearch` and the two job tools, and the bridge admits exactly the same set — never a delegation, messaging, memory or question tool, and never the resident's own settings, prompt or plugins.

Its authorization identity is bound to the host resident and the pinned artifact, so the
plugin's protected tools reach Casa's ordinary approval challenge rather than being refused
for want of a specialist record. For the same reason the declaring plugin's
result contract applies unchanged: a tool whose result declares an operator link still has
that link delivered by Casa into the operator's chat. Casa delivers it, as it posts the
approval challenge — the worker itself holds no tool that reaches outside its topic.

**INV-BGJOB-002**: A job fails through the engagement finalize funnel, with the reason and its last reported progress, when three consecutive batches report no progress or end without reporting, when a batch would exceed its declared batch cap, when a batch's delivery raises, or when a batch is cut off before finishing.

A batch makes progress when its LAST `report_job_progress` of that batch said so: a batch
that reports twice has changed its mind, and the later word is the one it stands by, which
is why the posted lines carry only the worker's summary and counts. A batch that reported
`progressed=False`, or that ended without reporting at all, did not: the counts are the
worker's own unit — work can be uncountable, or its total unknown — so they are shown to
the operator and decide nothing (#1031). A job launched before that rule carries the older
`reported`/`remaining` counters instead, and its one in-flight batch is judged once under
the count rule it ran under, so an upgrade mid-job neither invents a stuck batch nor
discards one the old rule had credited. Every
`cancelled` or `error` ending of a job engagement appends `Last progress: <summary>` to the
text the topic and the resident receive, so `/cancel` also reports what the job had done.

The declaration is strict: `plugin_store.manifest_jobs` refuses a non-list, an unknown
entry field, a bad or duplicate name, an empty skill, an over-long or multi-line title or
summary, and a `batches` or `turnsPerBatch` that is not a positive integer (a boolean is not
one), with `jobs_invalid`; install validation also refuses a job whose
`skills/<skill>/SKILL.md` is missing. An already stored artifact failing the same checks is
excluded from resolution with that reason.

The `<jobs>` block is rendered only for a resident whose allowed tools include `start_job`
(the assistant), over the jobs its own plugins declare first and then its currently
available delegates in declared order, one line per job: qualified name, title, summary (or
title) and the host's display name, marked as a plugin job where the resident itself hosts
it. A job two hosts can run is listed once, under the first; `start_job` resolves it the
same way.
A job declared by a plugin installed mid-conversation does not appear in a conversation
already open: a session's system prompt is fixed when the session is created. The resume
gate is what resolves it — the conversation's structural surface no longer matches what it
was registered with, so the next turn starts a fresh session carrying the new block
(INV-TURN-012, `architecture/turn-loop.md`).

An unknown job returns `job_not_declared` naming the startable jobs; a voice caller gets
`job_needs_text_channel`. Specialist launches retain the delegation ACL and engagement
slot. Resident-plugin launches keep input bounds, depth, the setup-turn restriction,
spawn cap and channel checks, but do not use the delegate ACL, specialist registry or
specialist slot. Both use `_launch_interactive_engagement` for record creation, driver
opening and the detached launch owner. A successful start returns its `pending` envelope
plus the job, title, host role and a one-line message. Resident-plugin topics persist a
`<resident display name> · <job title>` label, shortened with the existing topic helper,
so later state paints retain the name; they use the default topic bubble.

`report_job_progress(summary, progressed, done=None, remaining=None)` is granted only to a
job engagement's session. Outside a live job it returns `not_a_job`; a `progressed` that is
absent or not a boolean, and a negative or boolean count, return `invalid_arguments`. It
posts `📊 Batch <n>: <summary> · <done> done · <remaining> left` (the summary's first line,
at most 300 characters; counts only when given — the line carries the worker's summary and
makes no claim about the judgment, which a later report of the same batch could contradict
in a line already posted), records that claim as the batch's — a later report of the same
batch replaces it — updates its epoch `last_advance`, and persists `origin["job"]`.

Resume options rebuild the job's per-batch turn limit and its progress grant from
`origin["job"]`, so a resumed job session keeps both. Plugin workers additionally rebuild
from the recorded artifact and launch model, independent of later host configuration or
plugin assignment changes. A recorded resolution that loses the declaring plugin, including
environment withholding, refuses resume rather than building an empty worker.

## Failure behavior

**A plugin worker cannot launch.** Environment withholding is checked before topic
creation; the same admitted resolution supplies the record and options. Options-building
failures after record creation use the shared named launch-abort path. A cancellation or
restart during the opening acknowledgement remains owned by the launch lifecycle and
ends the engagement with its existing notice.

**A restart during a job.** Boot re-idles live engagements as for any engagement. Once
channels and residents are up, `casa_core._resume_background_jobs` posts `↻ Resuming
"<title>" after a restart.` in each live job's topic and calls `start_next_batch`; the batch
cut by the restart is judged like any other. This selects in-casa records carrying a job,
regardless of host kind. A failed resume notice is logged and cannot veto the batch attempt.
A failure resuming one job does not stop the others. Engagement permits are not restored
at boot. A launch interrupted during its opening acknowledgement retains the ordinary
launch cancellation and telling; it does not enter continuation recovery.

**A batch handoff is refused.** `deliver_system_turn` returns false when the engagement is
terminal, cannot be resumed, or Casa is stopping. It consumes no batch number and adds no
no-progress judgment. The loop logs the refusal.

**A job stalls.** The shared scheduler runs `sweep_jobs` every minute. A queued turn or live
delivery owner excludes the job, even when a batch takes longer than the stall threshold.
Otherwise a job with no advance for 180 seconds (or no recorded advance yet) is retried
through `start_next_batch`. Admission and progress reporting update `last_advance`;
admission also clears `stalls`. Three consecutive failed sweep attempts finalize the job
through the ordinary funnel, with its last progress and the durable resident notification.
The sweep does not run while the channel is stopping, and a shutdown-refused hand-off
does not count as a failed attempt.

Job resume failures stay live for that sweep instead of taking the interactive engagement's
two-strike terminal path. A job with no session to resume ends through the finalize funnel.
These rules concern continuation, including an acknowledged job whose first hand-off was
refused and whose `started` counter is still zero; the launch owners remain unchanged.

**A plugin's jobs cannot be listed.** An error resolving one specialist's plugins omits that
specialist's jobs; the other delegates' jobs are still listed.

## Extension points

A new job field belongs in `manifest_jobs` and `JobDecl`. Host-specific session options
belong in the launch and resume builders; both kinds share the batch loop and launch owner.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/background_jobs.py`
- `casa/rootfs/opt/casa/plugin_store.py::manifest_jobs`
- `casa/rootfs/opt/casa/agent.py::_render_jobs_block`
- `casa/rootfs/opt/casa/tools.py::start_job`
- `casa/rootfs/opt/casa/tools.py::_build_plugin_job_options`
- `casa/rootfs/opt/casa/tools.py::_launch_interactive_engagement`
- `casa/rootfs/opt/casa/tools.py::build_engagement_resume_options`
- `casa/rootfs/opt/casa/tools.py::report_job_progress`
- `casa/rootfs/opt/casa/casa_core.py::_resume_background_jobs`

**Tests**
- `tests/test_background_jobs_declaration.py`
- `tests/test_start_job.py`
- `tests/test_plugin_job_launch.py`
- `tests/test_background_jobs_loop.py`

**Related**
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/engagement-turn-admission.md`](../architecture/engagement-turn-admission.md)
<!-- END SOURCEMAP -->
