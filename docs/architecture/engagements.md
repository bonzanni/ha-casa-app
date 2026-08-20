---
last_reviewed: 2026-08-19
---

# Engagements

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Durable engagements: their records, how one is launched, how a turn is admitted to it, the
driver protocol, and what survives a restart. How an engagement *ends* — the single-winner
terminal transition, the strictness that keeps creation and the terminal flip from leaving
the persisted and in-memory records disagreeing, the completion gate, the finalization side
effects and topic output ordering — is
[`architecture/engagement-finalization.md`](engagement-finalization.md). How agents
address and launch one another — the delegation ACL, the depth cap, the agent-spawn cap —
lives in [`architecture/delegation.md`](delegation.md). The OS boundary a `claude_code`
engagement runs inside — its uid, workspace ownership, root's access into that workspace,
the privilege drop, and the confirmed-down sweep boot replay requires — is
[`architecture/engagement-containment.md`](engagement-containment.md). It does not cover the
turn loop itself, nor what a driver's underlying runtime does once started.

## Mental model

**A delegated turn and an engagement are different things.** Delegation in its ordinary form
is a task handed to a specialist that runs and returns — ephemeral. An engagement is a
durable record with its own topic, which outlives the call that created it.

Three launch paths exist and they are not symmetrical. Ordinary specialist delegation runs
ephemerally. *Interactive* specialist delegation creates an engagement. Engaging an executor
always creates one. Both engagement-creating paths pass the agent-spawn gate first
(INV-ENG-008).

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
a specialist, seven for an executor, refiring weekly); terminal tombstones age out after
thirty days, bounding duplicate-task protection. Separately, an observer watches engagement
events and may post a bounded LLM interjection into the resident chat — capped at three per
engagement and suppressible with `/silent`. The cap holds under concurrent dispatch: a
budget slot is reserved before evaluation and returned if nothing is posted.

**A `claude_code` record carries an OS uid, and what that uid means is not this document's
subject.** The allocation, the ownership it implies, and the boundary built on it are in
[`architecture/engagement-containment.md`](engagement-containment.md).

## Contracts & invariants

**INV-ENG-009**: A `claude_code` turn is admitted before its first byte reaches the engagement — a record found idle is `active` by then, and a terminal record is not written to at all.

**INV-ENG-011**: An `in_casa` LAUNCH turn ends holding the turn's own terminal artifact and either a terminal engagement record or operator-visible topic output — or the launch reports the death: one durable strict `error` transition, one bounded notice into the still-open topic, a bounded driver teardown, and the topic aborted. It is never left `active` behind an ended transport with nothing posted, and the path never writes `completed` and never retains to the shared memory bank.

A driver's `start()` returning has always meant *the first turn ran to its end*, never *the
engagement reported anything*, and that gap is where a launch turn could die unnoticed. The
turn's own terminal artifact is its result frame: the SDK's response iterator returns at that
frame and otherwise iterates indefinitely, so a drained stream with no result frame is a turn
that was cut off in flight — transport EOF, agent-process exit, or the reader being cancelled
— however many frames it produced first. The count of frames is *not* the predicate: a turn
cut off mid-tool-loop is indistinguishable from a finished one by frame evidence, so a check
built on evidence catches only the trivially empty turn and misses the reachable case.

The engagement's terminal artifact is its record. An interactive engagement that ends its
launch turn having posted text is legitimately awaiting the operator and is left alive; one
that posted nothing, or whose turn was cut off, has left no surface anyone can act on.

Three properties make the report safe rather than merely present. The driver only
**observes** — it records what the turn left behind and neither raises nor reads the
record's status, because a lock-free status read can catch the uncommitted window of a
strict terminal transition that a persist failure then rolls back. The launcher asks the
registry exactly **one transactional question** — flip this record terminal, strictly,
unless it already is — and its three outcomes decide everything: a durable win reports, a
lost race performs no side effect at all because the winner owns them, and a rolled-back
persist leaves the record live and its topic *open* while retiring the client, since an open
topic over a live record is recoverable and a closed one is not. And the side effects run in
one **anchored owner** the launcher awaits shielded, because a terminal transition whose
write committed keeps its durable state and re-raises on cancellation: an inline owner could
commit the flip and then never post, while a compensating second owner would correctly lose
the transition and do nothing.

The same owner reports a launch *cancelled* before its driver was confirmed live, which
previously flipped the topic to failed and closed it without posting anything at all.

The admission sits between opening the engagement's stdin FIFO for writing — which succeeds
only once its CLI is reading — and writing the first byte, which is the first thing that CLI
can observe. That is the only instant at which the delivery is certain and the engagement
has seen nothing of it. It is deliberately synchronous: with no suspension point between the
open, the decision and the first write, no inbound tool call and no terminal transition can
interleave, which is what makes the ordering a guarantee rather than a race. Durability
follows behind it — the authority check reads the in-memory record, and a persist that never
lands costs only that a later restart re-idles the record, after which the same redelivery
admits it again.

A turn that fits the pipe is written by a single non-blocking write, so in practice the
admission covers the whole of it rather than only its first byte. That is not luck: a payload
larger than the pipe's capacity would be written in pieces, and the suspension between them
is a real scheduling point at which an ungated terminal transition can commit, leaving the
remainder to be written to a record that is already terminal. Since a delivery cannot be
revoked, the fix is to remove the suspension rather than guard it — the pipe is grown to fit
the payload before the first write. The growth is strictly best-effort: a kernel that refuses
it, or a payload beyond the maximum pipe size, simply falls back to writing in pieces.

What it does not cover: the bytes after the first, when the payload does not fit. A terminal
transition landing mid-turn cannot revoke a delivery — closing the writer is itself an
end-of-input the CLI acts on — so a turn already begun runs until the finalize path's driver
teardown *attempts* to stop it, and what that teardown does and does not establish about the
OS process is in [`architecture/engagement-containment.md`](engagement-containment.md). A *completion*
cannot land in that window, because a message is still counted as unread throughout its own
write and INV-ENG-003 refuses; a cancellation can, and what becomes of the truncated turn it
produces is teardown's business rather than the write path's — INV-CONT-001 states what that
does and does not settle. The
admission also expresses no opinion on a record the registry does not know, which is
unreachable for a live engagement and where the dispatch gate already fails closed.

## Failure behavior

**A delegation is refused at one of its gates.** The ACL, alias, spawn-cap and
plugin-withholding refusals, and what each payload discloses, are described in
[`architecture/delegation.md`](delegation.md).

**A driver fails to start after the record exists.** The engagement is marked errored, topic
cleanup is attempted, and the caller is told the start failed.

**A restart interrupts an engagement.** Persisted records load with `active` rewritten to
`idle`. Replay is attempted only for the driver kind that supports it. A record whose
workspace or recorded plugin artifact is missing is *refused* with a warning — validated
before the intact-service fast path, so an ordinary restart cannot start a service whose run
script would exit-and-respawn forever — and a missing definition is skipped with a warning.
A failed stdin-FIFO recreation and a failed service start are refusals of the same kind:
the record is marked errored and no background spool/relay machinery attaches, rather than
accepting operator messages into an engagement with no consumer (or starting one that would
crash-loop under its supervisor). A record still owing a clearance-downgrade context
rebuild (INV-MEM-011) is never *resumed*: replay drops its session pointer and archive
cache and re-renders the workspace at the clamped floor first, refusing the same way if
that fails. Every one of those decisions sits downstream of preconditions this document does
not own: INV-CONT-004 and INV-CONT-005.

## Extension points

**A new driver** implements the driver protocol: start, send, cancel, resume, liveness —
plus the downgrade-recovery seams (invalidate the live session with confirmed teardown;
rebuild fresh at the record's current clearance). `start()` may raise `StaleLaunchError`
at its last suspension point; the launcher then aborts rather than deliver a prompt
rendered from pre-downgrade materials. A driver that runs its agent as a separate OS
process, or that reaches into a workspace as root, owes the rules in
[`architecture/engagement-containment.md`](engagement-containment.md) as well — the protocol
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
together — otherwise it exists at runtime and silently vanishes across a restart.

**A new origin value that may hold a live object** must be registered as non-persistable, or
serialization will either fail or persist something meaningless.

**A new terminal path, and a new topic output**, belong with the finalize funnel and the
per-engagement sequencer in
[`architecture/engagement-finalization.md`](engagement-finalization.md).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRecord`
- `casa/rootfs/opt/casa/drivers/driver_protocol.py::DriverProtocol`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver`
- `casa/rootfs/opt/casa/casa_core.py::replay_undergoing_engagements`

**Tests**
- `tests/test_delegate_to_agent.py`
- `tests/test_delegate_to_agent_interactive.py`
- `tests/test_claude_code_driver.py`
- `tests/test_engagement_registry.py`
- `tests/test_observer.py`
- `tests/test_boot_replay.py`
- `tests/test_in_casa_launch_terminal_artifact.py`
- `tests/test_in_casa_launch_retention_guard.py`
- `tests/test_launch_death_reporter.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/engagement-containment.md`](../architecture/engagement-containment.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
<!-- END SOURCEMAP -->
