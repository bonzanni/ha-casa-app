---
last_reviewed: 2026-11-09
---

# Engagements

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Durable engagements: their records, how one is launched, and the driver protocol. How a
turn is admitted to a live one is
[`architecture/engagement-turn-admission.md`](engagement-turn-admission.md). What a launch
abort rolls back and what survives a restart are in
[`architecture/engagement-failure-and-restart.md`](engagement-failure-and-restart.md). How an
engagement *ends* — the single-winner
terminal transition, the strictness that keeps creation and the terminal flip from leaving
the persisted and in-memory records disagreeing, the finalization side effects and topic
output ordering — is
[`architecture/engagement-finalization.md`](engagement-finalization.md); the completion gate
that refuses a success over input nobody read is
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md). How agents
address and launch one another — the delegation ACL, the depth cap, the agent-spawn cap —
lives in [`architecture/delegation.md`](delegation.md). How a launch answers `pending` before
the job's first turn, and who reports a launch that ends before that answer, is
[`architecture/engagement-launch-detach.md`](engagement-launch-detach.md); what a running job's
session does at each turn end and when it ends unexpectedly is
[`architecture/background-job-session.md`](background-job-session.md). It does not cover the
turn loop itself, nor what a driver's underlying runtime does once started.

## Mental model

**A delegated turn and an engagement are different things.** Delegation in its ordinary form
is a task handed to a specialist that runs and returns — ephemeral. An engagement is a
durable record with its own topic, which outlives the call that created it.

One launch path creates an engagement: `start_job` launches a background job through the
one job launch owner (`tools.py::_launch_interactive_engagement`), as a `specialist` record
when a delegate hosts the job and as a second record kind, `plugin`, when the declaring
plugin is installed on the calling resident — a worker record whose `role_or_type` is that
resident and whose session carries neither the resident's tools nor its prompt; what Casa
then does with a job's engagement is [`architecture/background-jobs.md`](background-jobs.md).
The launch passes the agent-spawn gate first (INV-ENG-008). Specialist delegation, sync or
async, runs ephemerally and creates none, and so does the configurator: it is a delegated
run in the operator's DM, not an engagement
([`architecture/configurator.md`](configurator.md)). An `executor` record an older install
left behind loads like any other, and a resume of it fails closed
([`architecture/engagement-failure-and-restart.md`](engagement-failure-and-restart.md)).

**Interactive specialist engagements are retired; the desk replaced them.** A
`delegate_to_agent` call naming `mode='interactive'` is refused with `interactive_retired`
before any gate or side effect, and its message names the alternative: a `sync` delegation,
after which the person carries on with the specialist by replying to its message
([`architecture/specialist-desk.md`](specialist-desk.md)). The value stays in the tool
schema's enum only so a stale call — a model's, or a stored scheduled prompt's — reaches
that refusal rather than the SDK's bare validation error. An interactive engagement an older
install left open loads as any `specialist` record does: it is not resumed at boot (only
jobs are), it stays one of its specialist's open conversations (a job on that specialist
is refused `engagement_busy` meanwhile), the operator may still write in its topic, and
`/complete`, `/cancel` or the stale-engagement reap ends it.

**A specialist's engagements are its open conversations, and a change to the specialist
does not reach them.** One keeps the personality and the plugin versions it started with
and reads the specialist's current settings when it resumes. So a persona apply, an upgrade,
a rollback or an uninstall warns before it changes a specialist that has any (INV-SPEC-022),
and removing a specialist closes the ones still open — after an uninstall commits and its
reload removes the specialist, and when a reload or a boot reads it disabled (INV-CFG-013) —
through the terminal funnel, outcome `cancelled`; a close that fails leaves that one open.

**Much less survives a restart than the word "durable" suggests.** The record persists;
concurrency permits, live drivers, inbound reservations and various
in-flight maps do not. A record found `active` at startup is rewritten to `idle`, because no
live driver survived to make `active` true.

**Durable is not indefinite.** A daily sweep suspends a live session after a day idle (a job
waiting on the operator is resumed by their next input);
terminal tombstones age out after thirty days, bounding the post-mortem record (one whose
completion time is not a finite number has no known age and is kept, and an `in_casa` one
only once its transcripts are reaped, per `architecture/engagement-transcript-reaping.md`).

## Contracts & invariants

The record's own contracts are defined where the behaviour is: the terminal transition and
creation strictness in [`architecture/engagement-finalization.md`](engagement-finalization.md),
turn admission in [`architecture/engagement-turn-admission.md`](engagement-turn-admission.md),
the completion gate in
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md), and the launch
and its death report in
[`architecture/engagement-launch-detach.md`](engagement-launch-detach.md).

## Failure behavior

What a launch abort rolls back and how far it gets, which launch-failure arms answer the
calling turn rather than the operator's topic, and what a restart replays or refuses, are in
[`architecture/engagement-failure-and-restart.md`](engagement-failure-and-restart.md). A launch
cancelled before it answered is reported by the launch-death reporter (INV-ENG-030, in
[`architecture/engagement-launch-detach.md`](engagement-launch-detach.md)); a job's session that
ends after it is resumed once (INV-BGJOB-018).

## Extension points

**A new driver** implements the driver protocol: `open`, `cancel`, `resume` and `is_alive`.
`open` only opens the session; the launch queues the session's first input after it, so a
failure in `open` is a launch abort and nothing it runs belongs to the launching tool call
(INV-ENG-021). The protocol says nothing about OS identity or filesystem reach: a driver that
runs its agent as a separate OS process owes its own containment.

**The brief is the job's first input.** For the in-process driver it is the opening user
message, written by the job's delivery worker like every later input; what the job may read
from memory is clearance-gated (see [`architecture/memory-scoping.md`](memory-scoping.md)).

**A new durable field** must be added to the record, its load path and its write path
together — otherwise it exists at runtime and silently vanishes across a restart. A field that
records an OBLIGATION owes three more decisions: what arms it (and, if the answer depends on
which caller wrote the terminal rather than on any property of the record, it is an explicit
argument to the transition, not a predicate), whether the strict rollback restores it, and
whether a record carrying it survives the terminal-retention expiry. One does so today:
expiring the row would delete the obligation along with the state needed to discharge it.
`terminal_notification_pending` is the telling a finalized outcome still owes the party that
asked for the work (INV-ENG-018, in
[`architecture/engagement-terminal-telling.md`](engagement-terminal-telling.md)). A legacy row
missing the key decodes as owing NOTHING — the opposite default would discharge the whole
tombstone's worth of obligations at the first boot after an upgrade.

**A new origin value that may hold a live object** must be registered as non-persistable, or
serialization will either fail or persist something meaningless.

**A new terminal path, and a new topic output**, belong with the finalize funnel in
[`architecture/engagement-finalization.md`](engagement-finalization.md).
Whether a terminal writer needed authority to assert *that* outcome — and why neither
persisting ledger checks it — is answered in the same document.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRecord`
- `casa/rootfs/opt/casa/drivers/driver_protocol.py::DriverProtocol`
- `casa/rootfs/opt/casa/tools.py::_launch_interactive_engagement`

**Tests**
- `tests/test_engagement_stays_open.py`
- `tests/test_delegate_to_agent.py`
- `tests/test_delegate_to_agent_interactive.py`
- `tests/test_engagement_registry.py`
- `tests/test_idle_sweep.py`
- `tests/test_in_casa_inbound_admission.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/sdk-client-pool.md`](../architecture/sdk-client-pool.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/engagement-completion-gate.md`](../architecture/engagement-completion-gate.md)
- [`architecture/engagement-turn-admission.md`](../architecture/engagement-turn-admission.md)
- [`architecture/engagement-launch-detach.md`](../architecture/engagement-launch-detach.md)
- [`architecture/background-job-session.md`](../architecture/background-job-session.md)
<!-- END SOURCEMAP -->
