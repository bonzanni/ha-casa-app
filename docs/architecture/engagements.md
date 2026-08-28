---
last_reviewed: 2026-08-26
---

# Engagements

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Durable engagements: their records, how one is launched, how a turn is admitted to it, and
the driver protocol. What a launch abort rolls back and what survives a restart are in
[`architecture/engagement-failure-and-restart.md`](engagement-failure-and-restart.md). How an
engagement *ends* — the single-winner
terminal transition, the strictness that keeps creation and the terminal flip from leaving
the persisted and in-memory records disagreeing, the finalization side effects and topic
output ordering — is
[`architecture/engagement-finalization.md`](engagement-finalization.md); the completion gate
that refuses a success over input nobody read is
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md). How agents
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

**INV-ENG-009**: A turn is admitted by the registry immediately before the call that hands it to the engagement, with no suspension point in between — a record found idle is `active` by then, and a terminal record is never handed a turn. For `claude_code` the admission precedes the first BYTE; for `in_casa` it precedes the awaited client hand-off, and covers follow-up turns only — the launch turn is INV-ENG-011's. Neither fences the hand-off itself: a terminal transition landing once a turn has begun cannot revoke it, and stopping an in-flight turn stays `driver.cancel`'s job. A refused turn is never reported as delivered and never told twice.

**One decision, asked at two different instants.** The registry answers it synchronously and
identically for both drivers — terminal refuses, `idle` delivers and becomes `active` without
re-stamping the last-turn time, `active` delivers, and an unknown record delivers rather than
refusing. What differs is *where the answer can be placed*, and that placement is what makes
the invariant's two halves differ in strength: for `claude_code` there is an instant at which
the engagement has provably seen nothing, and for `in_casa` there is not.
[`architecture/engagement-completion-gate.md`](engagement-completion-gate.md) carries that
account, next to the driver inbound surface it belongs to.

The `in_casa` admission covers FOLLOW-UP turns only, which is a decision rather than an
omission. A refusal on the launch turn would surface through the launch owners' error-marking
and death-reporting path, telling the operator a launch died for an engagement that a racing
writer — or their own cancellation — had just deliberately ended, which INV-ENG-013 forbids;
and the only writer racing that window is the coroutine that created the record moments
earlier. The launch turn's owner stays INV-ENG-011.

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

**INV-ENG-012**: A ticketed FOLLOW-UP turn to an `in_casa` engagement that ends without the turn's own terminal artifact — or that finishes holding it while finalization has *established* that its streamed text was wholly undelivered — is never answered with silence: exactly one bounded operator-facing notice attempt is made in the engagement's topic, by the owner of that turn's admission ticket, and one turn's observation can never be consumed or lost by another turn's owner. The single thing that excuses the telling is that the engagement's terminal path has already told that topic why the engagement ended; the record merely being terminal is not that fact, and wherever it is not known that the topic was told, the telling is made. A follow-up turn that ends holding its terminal artifact with no established delivery failure produces no observation and no notice; an *ambiguous* delivery (a lost acknowledgement, or a handle off the delivery contract) is not an established failure and records nothing. The driver records, never raises, and never reads the record's status.

INV-ENG-011 covers the launch turn. The turn *after* it had the same hole and no owner at
all: cut off mid-tool-loop, a ticketed turn raises nothing, records nothing, discharges its
admission ticket and leaves the record live, so an admitted operator message was consumed
and answered with nothing. The driver's zero-frame warning does not fire either — it is
gated on having seen no assistant frame, and a mid-tool-loop cutoff has seen several.

The predicate is the same one INV-ENG-011 uses and for the same reason: the absence of the
turn's result frame. It is emphatically not the evidence latch, which is set by the first
frame of any kind and so reads identically for a cut-off turn and a finished one.

The second arm is the delivery failure. A follow-up turn can finish — its result frame
arrives — with every Telegram operation on its streamed text positively refused, and such a
turn used to read as an ordinary quiet one. The stream handle's finalize now reports a
delivery outcome (INV-TG-006 in [`telegram.md`](telegram.md)), and only an *established*
failure is recorded: a lost acknowledgement may already be on the operator's screen, and
reporting it would tell them about — or, on the launch side, kill — a delivered turn. The
notice for this arm asserts only what its site observed: the turn finished, and its response
could not be delivered to the topic. It names no cause, because the reason carries none — an
absent Telegram application produces the same established failure as a refusal.

**The driver observes; the delivery task adjudicates.** That split is not tidiness, it is
the only place the distinction can be made. A raise from the driver would be
indistinguishable from a healthy `in_casa` self-emit completion, where the finalize funnel's
tail closes the engagement's own client and the response iterator legitimately ends with no
result frame — so a driver that raised would report a failure for a turn that succeeded. The
delivery task can tell the two apart because it can ask the registry, and asking is
something the driver is separately forbidden to do (see the lock-free-read hazard under
INV-ENG-011).

That question is asked of the *settled* record, under the registry's own lock. A lock-free
read can land inside a strict transition that has committed its terminal fields in memory
and not yet survived its tombstone write; if that write fails the transition is fully rolled
back, and a reader that saw the transient value would suppress a notice that was owed. The
read is bounded, and a read that does not return in time results in the notice being posted
anyway: the failure this invariant exists to remove is silence, so the fail-open direction
is toward telling.

**A terminal status is not proof that anything was told, and the suppression turns on the
telling.** The two arms of this release compose into the state that makes the difference
matter: a ticketed turn completes its engagement, the completion post fails, the funnel's
one bounded disclosure fails too, and the tail then ends that turn's stream — so the turn
looks cut off, the record reads `completed`, and a reader that asked only for the status
would stay quiet over a topic that heard nothing at all. The funnel therefore records
whether its own telling reached the topic, and the delivery task asks for the status and
that fact together, in one locked read. Not knowing counts as not told: no record of a
telling, a read that raises, and a read that does not return in time all produce the
notice. The recorded fact is in-memory and is never persisted — it coordinates two tasks
inside one process, and a restart has no surviving turn to adjudicate.

The observation is keyed by the **admission ticket**, not by the engagement. The record is
written after the per-engagement turn lock is released, so two consecutive turns can cross
there: the first writes its reason, the second runs to completion, and one observation is
overwritten or read by the wrong turn's owner. Per-engagement keying does not merely blur
the two, it loses one of them.

**It is one bounded attempt, not an ordering guarantee.** Nothing orders this notice against
a concurrent finalization's topic operations, so a terminal writer that wins its transition
after the settled read can paint and close first, leaving the notice below the paint or
failing against a closed topic. `in_casa` turn admission now exists, and it does not close
this: admission decides whether a turn is *delivered*, not where a notice *lands*. Ordering
the notice would additionally require the finalization's own topic operations to be sequenced
against it, which is not built here.

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
invariants that behaviour is measured against stay above, under Contracts & invariants.

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
Whether a terminal writer needed authority to assert *that* outcome — and why neither
persisting ledger checks it — is answered in the same document.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRecord`
- `casa/rootfs/opt/casa/drivers/driver_protocol.py::DriverProtocol`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver`

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
- `tests/test_in_casa_inbound_admission.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/engagement-containment.md`](../architecture/engagement-containment.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/engagement-completion-gate.md`](../architecture/engagement-completion-gate.md)
<!-- END SOURCEMAP -->
