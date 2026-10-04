---
last_reviewed: 2026-10-03
---

# A specialist starts its own job

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a specialist starts a background job its own plugins declare, from its own turn: which
turns are offered `start_job`, which jobs a specialist's call can resolve, which launch gates
apply to it, and the origin the job runs with. The job itself — its declaration, its batch
loop, its one-per-manifest-name rule and the resident's own `start_job` — is
[`background-jobs.md`](background-jobs.md); the desk a swipe-reply turn runs in is
[`specialist-desk.md`](specialist-desk.md); the ACL and the depth cap a delegation passes are
[`delegation.md`](delegation.md).

## Mental model

**A specialist owns its plugins' work.** When the operator asks a specialist for something
its plugin does as a job — in a desk turn, or in a turn the resident delegated to it — the
specialist starts the job itself, on itself, with `start_job(job, task, context)`. The job is
exactly the job the resident would have started on that specialist: the same topic, batch
loop, refusals and end notice.

**Offered by the build, decided at the call.** A specialist's desk, delegated or tap build is
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
a desk turn takes a scope different from the desk's own permit and never touches the desk's
lock.

**The resident is pointed at the delegate (#1228).** The resident keeps `start_job` for
every job it lists; what changes is what it is shown and told. Its `<jobs>` block marks a
delegate's job as that delegate's own (`Alex's own job: in text, ask Alex with a sync
delegation; Alex starts it itself and it runs in Alex's topic`) only when the delegate's own
start would accept it now, the same `own_job_hosts` resolution the call makes, so a job
whose plugin is withheld keeps its plain line. The assistant's text doctrine sends such a
request to the delegate through a sync delegation, never an interactive one, says a
delegation does run the job, and keeps `start_job` for such a job as the fallback when the
delegation reports that it could not start it. A sync delegation's reported job start counts
as a topic the assistant may point to. Its voice doctrine says no job starts on a call. The
`start_job` description names the delegation route.

**The work's origin is the turn's.** The job records the calling turn's origin, without the
two markers that identify the calling turn rather than the work: the desk use and the turn's
quota and echo key. It keeps the turn's clearance, chat and operator, and its `role` stays
the chat's resident, so the job posts into the operator's chat as a DM-started job does, and
its end notice, with its post echoes, goes to the resident.

## Contracts & invariants

**INV-BGJOB-008**: A specialist's `start_job` starts only a job declared by a plugin assigned to that specialist and loadable when it is called, hosted by that specialist; it is offered only to a desk or delegated turn whose session loads a job-declaring plugin (a tap's pin refuses it, a job or engagement session never holds it), and the job launches with the calling turn's origin — its clearance, chat and resident — at depth 1, under every launch gate a resident's start of that job applies except the delegation ACL and the depth cap.

The two exceptions travel to the one prelaunch call through a context variable set and reset
around that call only, so nothing the launch later spawns inherits them. The specialist
branch is chosen only when no engagement is bound and the calling role's loaded `kind` is
`specialist`; every other caller resolves hosts exactly as before.

What it does not cover: a plugin unassigned from the specialist while the launch awaits topic
creation — the launch then records the post-unassign resolution for a specialist with no
`requires:` block, and for one that declares `requires:` keeps every plugin its requires gate
admitted, the environment filter aside ([`plugin-runtime.md`](plugin-runtime.md),
INV-PLUG-008) — in both cases exactly as a resident's start of the same job does
([`background-job-occupancy.md`](background-job-occupancy.md), INV-BGJOB-006). A tap never starts a job
([`stored-call-buttons.md`](stored-call-buttons.md)).

**INV-BGJOB-009**: The resident's `<jobs>` listing marks a job as a delegate's own, to be started through a sync text delegation to that delegate, only when that delegate's own `start_job` would accept it at render time; the assistant's served text projection tells it to delegate such a job and that a delegation runs it, and its voice projection that no job starts on a call; it keeps `start_job` for every job it lists; and a specialist's own start returns no engagement or topic id, while the resident's results still carry them.

The listing and the call share one resolution, `own_job_hosts`. One withholding is measured
at render time and another at the call, so a secret wired or lost between them changes what
the call accepts but not the line already in the prompt. The prompt surface's digest covers the
block, so a changed line starts a fresh session.

What it does not cover: whether the model follows the doctrine. That is a live behaviour, and
the unit tests pin only what the assistant is shown and told.

## Failure behavior

**A job the specialist's plugins do not declare** — another specialist's, the resident's own,
an unknown name, a plugin withheld for an unresolved secret — returns `job_not_declared`
naming only the specialist's own startable jobs, and nothing is launched. **A voice
delegation** returns `job_needs_text_channel`. Every other refusal is the launch's own, with
the kinds a resident's start returns (`job_busy`, `busy`, `engagement_busy`,
`agent_spawn_cap_exceeded`, a topic or launch failure), minus the engagement and topic ids
those carry for the resident (#1229).

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

**Tests**
- `tests/test_specialist_start_job.py`
- `tests/test_delegate_owned_jobs.py`

**Related**
- [`architecture/background-jobs.md`](../architecture/background-jobs.md)
- [`architecture/specialist-desk.md`](../architecture/specialist-desk.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/stored-call-buttons.md`](../architecture/stored-call-buttons.md)
<!-- END SOURCEMAP -->
