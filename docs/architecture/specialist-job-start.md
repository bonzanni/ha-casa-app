---
last_reviewed: 2026-11-12
---

# A specialist starts its own job

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a specialist starts a background job its own plugins declare, from its own turn: which
turns are offered `start_job`, which jobs a specialist's call can resolve, which launch gates
apply to it, the origin the job runs with, and the turn the specialist gets when such a job
it started for someone else ends (`job_owner_turn`). The job itself — its declaration, its one long
session, its one-per-manifest-name rule and the resident's own `start_job` — is
[`background-jobs.md`](background-jobs.md); the ACL and the depth cap a delegation passes are
[`delegation.md`](delegation.md).

## Mental model

**A specialist owns its plugins' work.** When the operator asks a specialist for something
its plugin does as a job — in a turn the resident delegated to it — the
specialist starts the job itself, on itself, with `start_job(job, task, context)`. The job is
exactly the job the resident would have started on that specialist: the same session, direct
chat posts, log, refusals and end notice.

**Offered by the build, decided at the call.** A specialist's delegated or tap build is
offered `start_job` when the plugins it loads (after env withholding) declare at least one job;
any other build of that specialist is unchanged. That is visibility only. The call itself
resolves the job among the jobs declared by the plugins assigned to that specialist and
loadable at that moment — the set the job's own session will load — each hosted by the
specialist. It never looks at the resident's scope or at another agent's plugins. A plugin
withheld when the calling session was built, whose secret is wired before the call, is the
specialist's own and loadable, so its job starts. A job or engagement build is never offered
the tool, and the #541 dispatch ceiling refuses it there in any case: `start_job` is outside
it.

**Not a delegation.** Starting its own job targets no other agent, so the call skips exactly
two launch gates: the delegation ACL and the depth cap. The job is stamped at depth 1, as a
resident-started job is. Every other gate applies: the input bounds, the voice and setup-turn
mode gates, the requires gate, `engagement_busy`, the permit (the `<role>:engagement` scope
and the global cap, never waited for), the agent-spawn cap (a specialist's turn is agent
context, never operator-exempt), and the one-job-per-manifest-name claim. A start from inside
a delegated turn takes the `<role>:engagement` scope, different from the delegation's own
permit, without waiting.

**The resident is pointed at the delegate (#1228).** The resident keeps `start_job` for
every job it lists; what changes is what it is shown and told. Its `<jobs>` block marks a
delegate's job as that delegate's own (`Alex's own job: in text, ask Alex with a sync
delegation; Alex starts it itself and runs it`) only when the delegate's own
start would accept it now, the same `own_job_hosts` resolution the call makes, so a job
whose plugin is withheld keeps its plain line. The assistant's text doctrine sends such a
request to the delegate through a sync delegation, says a
delegation does run the job, and keeps `start_job` for such a job as the fallback when the
delegation reports that it could not start it. A delegation starts fresh and a job's end is
told to the resident, not to a later delegation of the delegate, so the text doctrine also has her pass on what she
already knows of a job's state — whether it ended, how, and when — in the `context=` of a
delegation that asks the delegate about that job. Its text doctrine says a job lives in the
direct chat — its questions and its result come there — and names no topic.
Its voice doctrine says no job starts on a call. The
`start_job` description names the delegation route.

**The work's origin is the turn's.** The job records the calling turn's origin, without the
markers that identify the calling turn rather than the work: the turn's quota and echo key,
its conversation, and the calling task's delivery (the job is a task of its own, born at its
launch). It keeps the turn's clearance, chat and operator, and its `role` stays the role of the
turn that asked — the chat's resident, or the specialist's own conversation in its own chat —
which is the job's requester: its end notice, with its post echoes, goes there. Where the job
posts is its own delivery, decided at launch: the specialist's own chat when that is open; with
no open chat `start_job` is refused ([`own-voice.md`](own-voice.md)). The job's state records
that its host started it itself (`self_host`, `background_jobs.initial_job_state`).

**The specialist hears its job's end first** (`job_owner_turn`). A job its host started on
itself for someone else — its `role` is not the host, so the start came from a delegated or
handed-off turn, not from the specialist's own chat — and that completed is handed, before
anyone is told, to that specialist in a delegated turn of its own, under its own lane
(`plugin_continuations._lane_turn`, bounded by the same ten minutes as a continuation) and with
no engagement bound. The turn's task (`OWNER_TASK`) carries the job's title and result, says
the requester has not been told yet, and asks it to check what is still open, hand a gap to a
colleague in one batch, and say plainly what could not be done. Its origin
(`owner_origin`) is rebuilt as a turn of whoever asked the specialist, in the same chat and
route, with nothing of the message that started the job; it carries Casa's operator-task proof
only when the job's recorded starter is `operator`, and the count of such turns along the task
(`specialist-handoff.md`). What that turn ends with decides the end notice
(`tools._finalize_engagement_tail`):

- an answer replaces the job's result in the notice to the requester, owed and acknowledged as
  any notice is (INV-ENG-018);
- a closing message that is exactly `<silent/>` declares that more work will come back to the
  specialist (a next job, a colleague still working): nothing is sent and the owed telling is
  acknowledged;
- a turn that could not start (its role no longer loaded, its lane busy past the bound), raised,
  did not complete, or was cancelled sends the job's own result followed by
  `FOLLOW_UP_FAILED_LINE`, "(The follow-up on this result did not run.)"; a cancellation is
  raised again once that notice is sent.

A job the resident started, one started in the specialist's own chat, and every end other
than `completed` get no such turn and are told as before. After `MAX_OWNER_PASSES` (five) such
turns along one task, a job's end goes straight to the requester.

## Contracts & invariants

**INV-BGJOB-008**: A specialist's `start_job` starts only a job declared by a plugin assigned to that specialist and loadable when it is called, hosted by that specialist; it is offered only to a delegated turn whose session loads a job-declaring plugin (a tap's pin refuses it, a job or engagement session never holds it), and the job launches with the calling turn's origin — its clearance, chat and resident — at depth 1, under every launch gate a resident's start of that job applies except the delegation ACL and the depth cap.

The two exceptions travel to the one prelaunch call through a context variable set and reset
around that call only, so nothing the launch later spawns inherits them.
Because the job keeps the calling turn's origin, its starter line
([`background-job-session.md`](background-job-session.md)) is read from that origin too: a delegated turn's start reads the token of the resident's turn that
delegated (`operator` when that turn was the operator's own or answered by them, `agent` for a
scheduled or webhook turn), whether the resident waited for the delegation or not; a turn that
carries Casa's operator-task proof (a specialist's turn at the end of a job the operator
started, and what descends from it) reads `operator` unless it is a schedule's fire, and a job
started from it keeps the proof and its pass count. The specialist
branch is chosen only when no engagement is bound and the calling role's loaded `kind` is
`specialist`; every other caller resolves hosts exactly as before.

What it does not cover: a plugin unassigned from the specialist while the launch awaits —
the launch then records the post-unassign resolution for a specialist with no
`requires:` block, and for one that declares `requires:` keeps every plugin its requires gate
admitted, the environment filter aside ([`plugin-runtime.md`](plugin-runtime.md),
INV-PLUG-008) — in both cases exactly as a resident's start of the same job does
([`background-job-occupancy.md`](background-job-occupancy.md), INV-BGJOB-006). A tap never starts a job
([`stored-call-buttons.md`](stored-call-buttons.md)).

**INV-BGJOB-009**: The resident's `<jobs>` listing marks a job as a delegate's own, to be started through a sync text delegation to that delegate, only when that delegate's own `start_job` would accept it at render time; the assistant's served text projection tells it to delegate such a job and that a delegation runs it, and its voice projection that no job starts on a call; it keeps `start_job` for every job it lists; and a specialist's own start returns no engagement id, while the resident's results still carry it.

The listing and the call share one resolution, `own_job_hosts`. One withholding is measured
at render time and another at the call, so a secret wired or lost between them changes what
the call accepts but not the line already in the prompt. The prompt surface's digest covers the
block, so a changed line starts a fresh session.

What it does not cover: whether the model follows the doctrine. That is a live behaviour, and
the unit tests pin only what the assistant is shown and told.

**INV-BGJOB-027**: A job that its host specialist started on itself from a turn whose `role` is another agent, and that completed, is first handed to that specialist in a delegated turn of its own — at most `MAX_OWNER_PASSES` times along one task — whose answer replaces the job's result in the requester's notice, whose closing message of exactly `<silent/>` sends nothing and clears the owed telling, and whose failure to run or complete, or cancellation, sends the job's own result with the follow-up-failed line; every other job end is told as before, with no such turn.

Enforced by `job_owner_turn.owner_role` (the outcome, the literal `self_host` mark, the
`role` against the host, the pass count), by `run_owner_turn`, which turns every failure but a
cancellation into a `failed` result and clears the engagement binding around the turn, and by
the notify step of `tools._finalize_engagement_tail`, which shields the send from a cancelled
turn and acknowledges the obligation only on a silent close.

What it does not cover: whether the specialist's answer is right or its `<silent/>` is honest —
a silent close that starts no further work leaves the requester untold, which is the model's
judgement the task text steers; and a Casa restart during the turn, which the boot replay of
the still-owed telling reports with the job's fact, not the specialist's answer (INV-ENG-018).

## Failure behavior

**A job the specialist's plugins do not declare** — another specialist's, the resident's own,
an unknown name, a plugin withheld for an unresolved secret — returns `job_not_declared`
naming only the specialist's own startable jobs, and nothing is launched. **A voice
delegation** returns `job_needs_text_channel`. Every other refusal is the launch's own, with
the kinds a resident's start returns (`job_busy`, `busy`, `engagement_busy`,
`agent_spawn_cap_exceeded`, a launch failure), minus the engagement id those carry for the
resident (#1229).

## Extension points

A `<jobs>` block in a specialist's prompt is not rendered: the plugin's own skill names its
job, and `job_not_declared` lists the startable ones.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/background_jobs.py::own_job_hosts`
- `casa/rootfs/opt/casa/background_jobs.py::offers_start_job`
- `casa/rootfs/opt/casa/tools.py::start_job`
- `casa/rootfs/opt/casa/agent.py::_render_jobs_block`
- `casa/rootfs/opt/casa/job_owner_turn.py`

**Tests**
- `tests/test_specialist_start_job.py`
- `tests/test_delegate_owned_jobs.py`
- `tests/test_job_owner_turn.py`

**Related**
- [`architecture/background-jobs.md`](../architecture/background-jobs.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/stored-call-buttons.md`](../architecture/stored-call-buttons.md)
<!-- END SOURCEMAP -->
