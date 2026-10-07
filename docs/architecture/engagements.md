---
last_reviewed: 2026-09-19
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
lives in [`architecture/delegation.md`](delegation.md). What an `in_casa` launch or
follow-up turn must leave behind, and who reports one that did not, is
[`architecture/engagement-turn-outcomes.md`](engagement-turn-outcomes.md). The OS boundary a `claude_code`
engagement runs inside — its uid, workspace ownership, the privilege drop, and the
confirmed-down sweep boot replay requires — is
[`architecture/engagement-containment.md`](engagement-containment.md); root's access into
that workspace is
[`architecture/engagement-workspace-access.md`](engagement-workspace-access.md). It does not cover the
turn loop itself, nor what a driver's underlying runtime does once started.

## Mental model

**A delegated turn and an engagement are different things.** Delegation in its ordinary form
is a task handed to a specialist that runs and returns — ephemeral. An engagement is a
durable record with its own topic, which outlives the call that created it.

Three launch paths exist and they are not symmetrical. Ordinary specialist delegation runs
ephemerally. *Interactive* specialist delegation creates an engagement. Engaging an executor
always creates one. Both engagement-creating paths pass the agent-spawn gate first
(INV-ENG-008). `start_job` launches through the same interactive launch owner, as a
`specialist` record when a delegate hosts the job and as a third record kind, `plugin`,
when the declaring plugin is installed on the calling resident — a worker record whose
`role_or_type` is that resident and whose session carries neither the resident's tools nor
its prompt; what Casa then does with a job's engagement is
[`architecture/background-jobs.md`](background-jobs.md).

**A specialist's engagements are its open conversations, and a change to the specialist
does not reach them.** One keeps the personality and the plugin versions it started with
and reads the specialist's current settings when it resumes. So a persona apply, an upgrade,
a rollback or an uninstall warns before it changes a specialist that has any (INV-SPEC-022),
and removing a specialist closes the ones still open — after an uninstall commits and its
reload removes the specialist, and when a reload or a boot reads it disabled (INV-CFG-013) —
through the terminal funnel, outcome `cancelled`; a close that fails leaves that one open.

**Much less survives a restart than the word "durable" suggests.** The record persists;
concurrency permits, live drivers, output sequencers, inbound reservations and various
in-flight maps do not. A record found `active` at startup is rewritten to `idle`, because no
live driver survived to make `active` true.

**A turn being handed to the CLI is what makes a `claude_code` record live again.** An
operator turn that was queued but never consumed is redelivered to the respawned CLI after a
restart, and that delivery — not the restart, and not the respawn — is what returns the
record to `active`. The distinction is load-bearing rather than bookkeeping: the tool
authority an engagement holds over the internal dispatch path is bound to a record that is
`active` (INV-MCP-001), so a turn delivered against an idled record would run stripped of
every non-terminal tool the engagement owns, and the refusal would blame its grant. The same
decision refuses the delivery outright once the record is terminal. It covers the first byte
only: a terminal transition landing after a turn has begun cannot revoke it, because a pipe
has no rollback — stopping an in-flight turn is the finalize path's driver teardown.

**Durable is not indefinite, and engagements can speak up unprompted.** A daily sweep
suspends a live session after a day idle and posts recurring idle reminders (three days for
a specialist record, seven for every other kind, refiring weekly); terminal tombstones age
out after thirty days, bounding duplicate-task protection (one whose completion time is not a
finite number has no known age and is kept). Separately, an observer watches
engagement events and may post a bounded LLM interjection into the resident chat — capped
at three per engagement and suppressible with `/silent`. The cap holds under concurrent dispatch: a
budget slot is reserved before evaluation and returned if nothing is posted.

**A `claude_code` record carries an OS uid, and what that uid means is not this document's
subject.** The allocation, the ownership it implies, and the boundary built on it are in
[`architecture/engagement-containment.md`](engagement-containment.md).

## Contracts & invariants

**INV-ENG-014**: Once a `claude_code` launch's rollback has been entered, every removal it was entered to run — the s6 service directory, the workspace tree, the control directory that holds `.casa-meta.json`, the uid's passwd/group identity and its private outbox — is attempted; a cancellation delivered at one of the rollback's own awaits skips none of those attempts. That cancellation is never swallowed: it is re-raised once the attempts have run, carrying the failure it interrupted rather than replacing it.

**What it does not cover.** *Attempted* is not *succeeded*: every removal keeps the
best-effort floor it always had, so an I/O or permission failure leaves that artifact behind,
and two of the five do not even say that they did — see
[`architecture/engagement-failure-and-restart.md`](engagement-failure-and-restart.md). It says
nothing about **which** removals a rollback is entered to run for which cause; that is
INV-ENG-015 below, which extends this one through the *entered to run* clause rather than
qualifying it. A rollback entered for a stop-caused abort is entered to run fewer removals
and still skips none of them. The removal order, the mechanism, and why the removals being
synchronous is what makes the guarantee cheap are in the same document.

**INV-ENG-015**: A `claude_code` launch that Casa's graceful-stop cleanup finds registered is cancelled by a stop that recorded its cause against that launch first, so the cause is carried rather than inferred from the cancellation: a terminal write that observes that cause carries `origin.shutdown_reason = "casa_shutdown"` as a signal separate from the outcome, which it never replaces, and the reason it records states that Casa was stopping rather than attributing the end to a cancelled tool call; a rollback that observes that cause before its removals removes the s6 service source and recompiles but retains the workspace tree, the control directory, the uid's identity and its private outbox; the operator-facing notice claims retention only where that retention was actually recorded; the cleanup does not return until each such launch and its death report have run to completion; and a terminal `.casa-meta.json` carrying a retention deadline is written for that retained workspace only after a strict terminal transition has durably committed that engagement's record, and exactly once for that workspace, so such metadata never precedes the durable terminal record it describes and a retained workspace is never reaped while a live record still owns it.

**What it does not cover** is stated where the behaviour is: the enrolment window, the
attempted-not-guaranteed notice, the ordered-not-complete stamp, and the three paths that
leave a retained workspace unreaped are under *A graceful stop cancels a launch* in
[`architecture/engagement-failure-and-restart.md`](engagement-failure-and-restart.md). An
ordinary abort and a creator or barge-in cancellation are outside this invariant entirely:
both keep the full five-removal set, unchanged.

## Failure behavior

What a launch abort rolls back and how far it gets, which launch-failure arms answer the
calling turn rather than the operator's topic, and what a restart replays or refuses, are in
[`architecture/engagement-failure-and-restart.md`](engagement-failure-and-restart.md). The
invariants that behaviour is measured against are INV-ENG-014 and INV-ENG-015 above, under
Contracts & invariants, and INV-ENG-011 in
[`architecture/engagement-turn-outcomes.md`](engagement-turn-outcomes.md).

## Extension points

**A new driver** implements the driver protocol: start, send, cancel, resume, liveness —
plus the downgrade-recovery seams (invalidate the live session with confirmed teardown;
rebuild fresh at the record's current clearance). `start()` — or `open()`, for a driver
that declares the split launch of INV-ENG-021 — may raise `StaleLaunchError` at its last
suspension point; the launcher then aborts rather than deliver a prompt rendered from
pre-downgrade materials. A driver without the split keeps its whole first turn inside the
launching tool call. A driver that runs its agent as a separate OS
process, or that reaches into a workspace as root, owes the rules in
[`architecture/engagement-containment.md`](engagement-containment.md) and, for that reach,
[`architecture/engagement-workspace-access.md`](engagement-workspace-access.md) as well — the protocol
says nothing about identity or filesystem reach.

**`start()`'s `prompt` is the first turn, for every driver.** For an in-process engagement
that is the opening user message; for one that runs its agent in a workspace it is the text
enqueued to the inbound spool (an empty one is suppressed rather than delivered as a blank
turn). It is *not* the workspace's standing instructions, which the driver renders
separately from the executor's own template. The two are therefore distinct surfaces, and a
launcher that interpolates something into one has not put it in the other — which is why a
memory-enabled launch fetches the prior-engagement archive on exactly one of those paths
per driver rather than both (see [`architecture/memory-scoping.md`](memory-scoping.md)
for which, and why the duplicate mattered).

Nothing after the launch fetches it again. A replay that re-renders the workspace reuses the
block the launch cached, and a clearance rebuild *clears* that block rather than refetching
— a refetch there would read at the clearance the rebuild has just left, which is the thing
the rebuild exists to stop.

**A new durable field** must be added to the record, its load path and its write path
together — otherwise it exists at runtime and silently vanishes across a restart. A field that
records an OBLIGATION owes three more decisions: what arms it (and, if the answer depends on
which caller wrote the terminal rather than on any property of the record, it is an explicit
argument to the transition, not a predicate), whether the strict rollback restores it, and
whether a record carrying it survives the terminal-retention expiry. Two do so today, for the
same reason: expiring the row would delete the obligation along with the state needed to
discharge it. `quiesce_pending` is the kill a terminal `claude_code` record still owes
(INV-CONT-006); `terminal_notification_pending` is the telling a finalized outcome still owes
the party that asked for the work (INV-ENG-018, in
[`architecture/engagement-terminal-telling.md`](engagement-terminal-telling.md)). A legacy row missing
either key decodes as owing NOTHING — the opposite default would discharge the whole
tombstone's worth of obligations at the first boot after an upgrade.

**A new origin value that may hold a live object** must be registered as non-persistable, or
serialization will either fail or persist something meaningless.

**A new terminal path, and a new topic output**, belong with the finalize funnel and the
per-engagement sequencer in
[`architecture/engagement-finalization.md`](engagement-finalization.md).
Whether a terminal writer needed authority to assert *that* outcome — and why neither
persisting ledger checks it — is answered in the same document.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRecord`
- `casa/rootfs/opt/casa/drivers/driver_protocol.py::DriverProtocol`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver`
- `casa/rootfs/opt/casa/tools.py::engage_executor`

**Tests**
- `tests/test_delegate_to_agent.py`
- `tests/test_delegate_to_agent_interactive.py`
- `tests/test_claude_code_driver.py`
- `tests/test_engagement_registry.py`
- `tests/test_observer.py`
- `tests/test_boot_replay.py`
- `tests/test_in_casa_inbound_admission.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/sdk-client-pool.md`](../architecture/sdk-client-pool.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/engagement-containment.md`](../architecture/engagement-containment.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/engagement-completion-gate.md`](../architecture/engagement-completion-gate.md)
- [`architecture/engagement-turn-admission.md`](../architecture/engagement-turn-admission.md)
- [`architecture/engagement-turn-outcomes.md`](../architecture/engagement-turn-outcomes.md)
<!-- END SOURCEMAP -->
