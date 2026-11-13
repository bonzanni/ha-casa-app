---
last_reviewed: 2026-11-12
---

# Background jobs

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Plugin-declared background jobs: the `casa.jobs` declaration, the `<jobs>` block a resident
sees, `start_job`, specialist and resident-plugin worker launches, the `report_job_progress`
tool, and what Casa ends a job for. How a running job's session works — the brief, writes as
they arrive, turn ends, parking, resuming after a restart or a CLI death, compaction and cost
— is [`background-job-session.md`](background-job-session.md); how an operator message
survives a restart is [`background-job-message-journal.md`](background-job-message-journal.md).
How many jobs of one plugin may run at once is
[`background-job-occupancy.md`](background-job-occupancy.md). The engagement a job runs in —
its admission, completion gate and finalization — is described by
[`engagements.md`](engagements.md) and the documents it routes to; the per-role engagement
slot is in [`delegation.md`](delegation.md). What a job puts in the operator's direct chat is
[`background-job-direct-chat.md`](background-job-direct-chat.md); what it writes in its
agent's log, when a log group is set up, is [`agent-logs.md`](agent-logs.md).

## Mental model

A job belongs to the plugin that declares it, not to a specialist. A plugin's manifest lists
jobs under `casa.jobs`, each naming one of its skills, a title, an optional summary, an
optional `host` and an optional `quietWhenScheduled`. Any specialist whose resolved plugin
set includes that plugin can host the job, and so can a resident the plugin is installed on
unless the job declares `"host": "specialist"`; the qualified job name is
`<plugin manifest name>:<job name>`.

The assistant sees the jobs its delegates can host in a `<jobs>` block and starts one with
`start_job(job, task, context)` — except a job a delegate can start itself, which she asks
that delegate for with a sync delegation
([`specialist-job-start.md`](specialist-job-start.md)). The job runs as an engagement of the
host specialist, with three differences: the record carries the job's state under
`origin["job"]` (`background_jobs.initial_job_state`: name, title, skill, the brief's uuid,
the launch context, the last progress, the starter, the quiet stamp, and `self_host` when the
host started the job itself), the specialist's
session is built as a job session (`background_jobs.job_session_options`) with the
`report_job_progress` and `ask_operator` grants, and the first thing written into it is the
job's brief.

A resident-hosted launch uses a dedicated `kind="plugin"` engagement. Its
`role_or_type` names the resident, while `origin["plugin_job"]` pins the plugin registry
name and launch model, and the record keeps one declaring artifact. The worker loads that
artifact's skills and MCP servers, plus only `report_job_progress` and `emit_completion`
from Casa. Its native grants are `Skill` and `ToolSearch`; `Agent`, `Task`,
`AskUserQuestion` and `Bash` are denied, as are the CLI's cross-session tools (INV-MCP-013
in [`mcp-and-tools.md`](mcp-and-tools.md)). `Bash` has to be denied outright: the pinned
CLI runs a read-only command without asking the fail-closed callback, and when a
backgrounded command finishes, the live CLI runs a turn Casa never sent. Empty settings
sources, default permission mode and the fail-closed callback keep the resident's settings
and tools out. The Casa-owned prompt tells the worker the job is one long session, to set
aside items needing answers — it has no question tool, and the operator answers by replying
to its posts in their direct chat — and that their messages reach it between its steps; it
supplies no resident persona or memory. Plugin authorization and result-contract hooks still
apply. Its working directory is `/data/engagements/<id>/plugin-job`.

Casa then writes the brief and lets the job work: one long session that keeps going until
the job calls `emit_completion`. It posts progress with `report_job_progress` when it has
something worth telling, and a specialist's job asks the operator through `ask_operator`.
The operator's replies in their direct chat to the job's posts are written into the running
session as they arrive. When a turn ends without the job
finishing, Casa continues it, nudges it once, or parks it to wait for the operator
([`background-job-session.md`](background-job-session.md)). The job ends when it calls
`emit_completion`, when the operator cancels it (a Cancel button in their direct chat, or by
asking an agent, whose `stop_work` or the resident's `cancel_engagement` cancels it), when the stale-job reap
ends it, or when its session cannot be resumed (INV-BGJOB-022). Its end is told to its
requester, except that a completed job a specialist started on itself for someone else is first
handed to that specialist, whose turn decides what the requester hears (INV-BGJOB-027 in
[`specialist-job-start.md`](specialist-job-start.md)). A job declaring
`quietWhenScheduled` says nothing in the direct chat when its schedule started it, until it
interacts with the operator
([`background-job-quiet-runs.md`](background-job-quiet-runs.md)).

**A job's lines need its host's open chat.** A job reports to the operator through its host's
surface, so `start_job` refuses a host whose own chat is not open with the capability rule's
typed result before the start claim (the resident's chat always is), and a job's record keeps
the delivery and requester its launch owner stamped at birth ([`own-voice.md`](own-voice.md)).

## Contracts & invariants

**INV-BGJOB-004**: A resident-hosted job's session is offered exactly its declaring plugin's tools, `Skill`, `ToolSearch`, the two job tools and its scratchpad — never a delegation, messaging, memory or question tool, and never the resident's own settings, prompt or plugins.

Its authorization identity is bound to the host resident and the pinned artifact, so the
plugin's protected tools reach Casa's ordinary approval challenge rather than being refused
for want of a specialist record. For the same reason the declaring plugin's
result contract applies unchanged: a tool whose result declares an operator link still has
that link delivered by Casa into the operator's chat. Casa delivers it, as it posts the
approval challenge — the worker itself holds no tool that reaches the operator.

How many jobs of one plugin may be live or starting at once — one per installed plugin
and one per plugin manifest name — is
[`architecture/background-job-occupancy.md`](background-job-occupancy.md) (INV-BGJOB-006).

**INV-BGJOB-022**: Casa never ends a live job on its own judgement of the work — stopping without finishing, the step ceiling, API faults and cost continue or park it; the job runner ends a job only when its session cannot be resumed after a restart or after its first unexpected end, or ends unexpectedly a second time, and then `error` through the engagement finalize funnel with its reason; every `cancelled` or `error` end of a job appends `Last progress: <summary>` to the text its resident receives, once.

The reasons are "could not be resumed after a restart" and "its session ended unexpectedly"
(INV-BGJOB-018 in [`background-job-session.md`](background-job-session.md)). The summary is
the first line of the job's latest `report_job_progress`, so a cancellation also reports
what the job had done. What it does not cover: the job itself may finish `failed` or `cancelled`
through `emit_completion`, the operator may cancel it, the stale-job reap cancels a job idle
and waiting past its expiry, and a launch that fails before its session is registered takes
the launch-abort paths ([`engagement-launch-detach.md`](engagement-launch-detach.md)).

The declaration is strict: `plugin_store.manifest_jobs` refuses a non-list, an unknown
entry field (the only fields are `name`, `skill`, `title`, `summary`, `host` and
`quietWhenScheduled`), a bad or duplicate name, an empty skill, an over-long or multi-line
title or summary, a `host` other than the string `"specialist"`, and a `quietWhenScheduled`
that is not a boolean, with `jobs_invalid`; install validation also refuses a job whose
`skills/<skill>/SKILL.md` is missing. An already stored artifact failing the same checks is
excluded from resolution with that reason.

The `<jobs>` block is rendered only for a resident whose allowed tools include `start_job`
(the assistant), over the jobs its own plugins declare first and then its currently
available delegates in declared order, one line per job: qualified name, title, summary (or
title) and the host's display name, marked as a plugin job where the resident itself hosts
it, and as that delegate's own job where the delegate can start it itself. A job two hosts can run is listed once, under the first; `start_job` resolves it the
same way. A job declaring `"host": "specialist"` is skipped in the resident's own scope
without claiming its name, so it is listed under, and started on, the first delegate whose
plugins declare it; when no delegate does, it is not startable and `start_job` returns
`job_not_declared`.
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
plus the job, title, host role and a one-line message, which names the agent's log
(`<Agent>'s log`, or its `plugins log` for a resident's plugin worker) only while the log is
on.

`report_job_progress(summary, done=None, remaining=None)` is granted only to a job
engagement's session and may be called at any time in a live job. Outside one it returns
`not_a_job`; a summary that is not a string, or a negative or boolean count, returns
`invalid_arguments`. It records the summary's first line (at most 300 characters) as the
job's `last_summary`, marks progress for the step-ceiling rule, logs
`📊 <summary> · <done> done · <remaining> left` in the agent's log (counts only when given —
they are the worker's own unit and decide nothing) and persists `origin["job"]`. Nothing is
posted in the direct chat.

Resume options rebuild the job session and its grants from `origin["job"]`, so a resumed
session runs exactly as the launched one did. Plugin workers additionally rebuild from the
recorded artifact and launch model, independent of later host configuration or plugin
assignment changes — except the access profile the host holds for the plugin, which every
build reads live and the record stores as built
([`plugin-access-profiles.md`](plugin-access-profiles.md)), and a launch model recorded
before Casa's move to the 5.5 models, which resumes on its family's successor
(`current_resident_model`). A recorded resolution that loses the declaring plugin, including
environment withholding, refuses resume rather than building an empty worker.

## Failure behavior

**A plugin worker cannot launch.** Environment withholding is checked before the record is
created; the same admitted resolution supplies the record and options. Options-building
failures after record creation use the shared named launch-abort path. Once the session is
registered, a failure — the CLI dying before or during the brief's write included — is a
session loss, recovered once ([`background-job-session.md`](background-job-session.md)).

**A restart during a job.** Boot re-idles live engagements as for any engagement. Once
channels and residents are up, `casa_core._resume_background_jobs` resumes each live job
through the job runner's boot path; a failure resuming one job does not stop the others.
Engagement permits are not restored at boot. A job whose specialist is disabled and whose
boot close failed is not resumed.

**A plugin's jobs cannot be listed.** An error resolving one specialist's plugins omits that
specialist's jobs; the other delegates' jobs are still listed.

## Extension points

A new job field belongs in `manifest_jobs` and `JobDecl`. Host-specific session options
belong in the launch and resume builders, and every job session goes through
`job_session_options`; both host kinds share the launch owner and the job runner.

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

**Tests**
- `tests/test_background_jobs_declaration.py`
- `tests/test_job_session_declaration.py`
- `tests/test_start_job.py`
- `tests/test_plugin_job_launch.py`
- `tests/test_background_jobs_loop.py`
- `tests/test_job_tools.py`
- `tests/test_specialist_job_host.py`

**Related**
- [`architecture/background-job-session.md`](../architecture/background-job-session.md)
- [`architecture/background-job-message-journal.md`](../architecture/background-job-message-journal.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/background-job-occupancy.md`](../architecture/background-job-occupancy.md)
<!-- END SOURCEMAP -->
