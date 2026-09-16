---
last_reviewed: 2026-09-16
---

# Engagement launch detach

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How an `in_casa` engagement's launch turn is separated from the tool call that launched it:
the two-call launch (open the client, then hand the turn to an anchored owner), what that
owner does with every outcome of the turn, how the graceful stop drains it, and the one
envelope every engagement outcome travels in. What a launch turn must leave behind and how
its death is reported are in [`engagements.md`](engagements.md) (INV-ENG-011); which
launch-failure arms answer the caller rather than the topic is in
[`engagement-failure-and-restart.md`](engagement-failure-and-restart.md); the durable
obligation to tell the engager is in
[`engagement-terminal-telling.md`](engagement-terminal-telling.md) (INV-ENG-018).

## Mental model

**The engager is not held.** Until v0.314.0 `engage_executor` awaited the driver's `start()`
inline, so the whole first turn of the engagement ran inside the engaging resident's own tool
call. On the 2026-09-15 install the assistant's tool call was held for three minutes while the
configurator worked, the reload the configurator ran replaced the assistant, and the pool's
drain fuse then cut the launch turn from under it. The operator saw the assistant silent, then
the configurator's output relayed by the assistant, and no result. Now the tool call opens the
client, hands the launch turn to an owner Casa anchors, and answers `pending` at once; the
engager's turn ends, and the engagement's result reaches it the way every result does, as a
notification. Whatever tears the engager's client down no longer reaches the engagement.

**Every outcome of the turn has an owner, and the owner tells the engager.** The tool call
used to be the one answering for the turn: a death or a fault came back in its envelope. With
the tool call gone before the turn runs, nobody is waiting for an envelope, so the owner tells
the engager over the bus instead, on the same envelope the finalization funnel and the boot
replay use, with the terminal write armed so a telling lost with the process is replayed at
boot.

## Contracts & invariants

**INV-ENG-021**: An `in_casa` launch answers `pending` to the tool call that launched it as soon as the driver's client is open and the launch turn has been handed to an anchored owner, in one synchronous step that also enrols that owner with the launch ledger; the launch turn never runs inside the engager's turn, and cancelling the tool call after it returned does not touch the turn. Every outcome of the turn is told to the engager exactly once, live over the one engagement-outcome envelope with the obligation armed in the terminal write: a turn that died, an owner cancelled before or after its first step, and a stop that latched while the client was opening. Telling ownership is never inferred from the record: a compensating owner that loses the terminal race tells nobody, however armed the record is; an inline arm acknowledges on return only a transition it won, and hands its telling to its own abort only when the launcher was cancelled before returning the envelope. A turn whose terminal write did not persist — the owner's or the cancellation owner's — leaves the record live and its topic open, posts one bounded notice saying so, and tells the engager under its own non-terminal kind, which the consumer never narrates as a failure. The graceful stop drains the owners, the reporters and the pre-record topic aborts together to joint quiescence, and once it has marked those drains complete neither a never-started owner's cancellation nor a cancelled launcher's compensation mints anything; each is logged.

**The launch is two calls, and the handoff is one synchronous block.** The driver splits
`start()` into `open()` — build the client, bind the engagement, run the stale-launch gate,
register the running engagement — and `run_launch_turn()`, the first turn. The tool call
awaits `open()` inline, so the gate that refuses a prompt rendered from pre-downgrade
materials still answers the caller with `clearance_changed_during_launch`, and a client that
would not open still answers with `driver_start_failed`. Then, with no `await` between its
first statement and its last, it creates the owner task, enrols it with the launch ledger
under the engagement's id, drops its own ledger handle, attaches the owner's done callback and
returns `pending`. The block is synchronous so that the graceful stop's ledger walk sees
either the tool call or the owner, never a gap between them, and so that a stop which began
during `open()` finds the ledger latched: an owner enrolled after the stop began is cancelled
before it runs, and a tool call that resumes from `open()` to find the latch set mints no owner
at all — it reports the cancellation inline, armed, and acknowledges on return like every
other inline abort. A driver without the split — a fake, or a protocol consumer that has not
adopted it — keeps the pre-detach shape, the whole first turn inside the tool call; the
`claude_code` driver's launch is a spool write and needs nothing.

**What the owner does is what the tool call did, after `start()` returned.** The API-fault
arm, the generic arm and the INV-ENG-011 observation arm are the same three, and they reach
the same launch-death reporter with the same strict terminal transition; what changed is who
they answer. Each reports through the reporter with the obligation ARMED and then tells the
engager the fact of the outcome — status `error`, the fault's kind and detail, no result —
with the delivery acknowledgement that clears the obligation. The API-level fault, which the
inline launch returned to its caller as a named fault with the topic aborted silently, now
takes the death path with the others: the turn ran after the tool call returned, so there is
no caller to hand the kind to, and an operator's topic that says nothing is what the bounded
notice exists to explain. A compensating second reporter that finds the record already
terminal tells nobody: the winner owns the telling, and the record's armed bit is not
permission to send, because a surviving finalization funnel leaves it armed while its own
delivery is pending (measured in review: reading the bit as permission told the engager
twice). The one case where a winner cannot tell — an inline abort whose launcher was
cancelled before it returned the envelope — is handed over explicitly by that launcher: its
cancellation arm attaches the telling to the abort's own completion, and the abort tells
live only if it won. The owner's own telling runs shielded and anchored, so a cancellation
landing inside it neither cuts it nor spawns a second one.

**Cancellation has an owner at every point.** An owner cancelled mid-turn takes the
cancellation arm the tool call had: the cancellation owner reports `launch_cancelled` with
the stop's recorded cause, armed, and tells the engager what the report's outcome allows —
the fact of the outcome on a win, the uncommitted notice on a rolled-back persist, nothing on
a lost race. An owner cancelled after its one transactional question was answered spawns no
cancellation owner at all, whatever the answer: its telling is already in flight, and a
compensator on a rolled-back persist would re-ask the question, get the same rollback and tell
the uncommitted case twice (measured in review). An owner cancelled before its first
step — enrolled after the stop's ledger walk began — runs no statement, no handler and no
`finally` (measured), so its done callback is the only place it can be reported: the callback
drops the ledger handle and spawns the same cancellation owner. After the stop has marked its
drains complete nothing is minted there — nor by a launcher cancelled while still creating
its topic, which the ledger walk could not see — because the interpreter's final sweep is
next and would destroy the report pending; the residual is logged and boot re-idles the live
record.
The stop's order is the ledger drain, then the three anchors — the owners, the reporters with
their acks and aborts, and the pre-record topic aborts — drained TOGETHER to joint quiescence,
then the mark. Joint, because a task in one anchor can mint into another (a finished topic
abort releases a launcher whose named abort then mints a telling into the reporter set), so
three drains in sequence let work into an already-drained set; and quiescence is confirmed
only after one yield to the loop, because a task that has just completed reads as done while
the done callback that mints its successor is still queued (both measured in review: a stop
that marked on the first empty snapshot, or after sequential drains, left one telling to be
minted after it).

**A persist failure is not a failure.** A launch turn that ended, whose strict terminal write
rolled back, leaves a live record and an open topic — an open topic over a live record is
recoverable and a closed one is not, exactly as INV-ENG-019 reasons for the inline aborts. The
inline launch answered its caller with the death's kind anyway, since the launch really had
died for that reason. The owner cannot: telling the engager "error" over the bus would have
the consumer narrate a failure for an engagement that is still open and resumable. So it posts
one bounded notice into the topic saying the outcome could not be recorded, retires the dead
client, and sends the engager an UNARMED notice of kind `launch_outcome_uncommitted` — there is
no terminal write to carry an obligation. The consumer has a branch for that kind that says the
engagement is still open and did not fail, and never routes it through the failure narration.

**One envelope.** The finalization funnel, the boot replay and the launch owner each build
their own `DelegationComplete` — they differ in text, result availability, elapsed time and
message, measured in review — and hand it to one sender that carries only the routing: the
target role from the origin with the caller's fallback (the boot replay passes the configured
assistant role, as it always did; a hard-coded fallback sent a role-less record to a queue
that need not exist, and the bus drops an unknown target silently), the channel, the
cid/chat-id/engagement-id context merged with whatever the caller adds (the funnel's next
steps), and the delivery acknowledgement. Three copies of that routing had drifted once
already; a fourth was not written.

## Failure behavior

| Failure | Behaviour |
|---|---|
| `open()` raises, or the stale-launch gate refuses | The tool call answers its named fault as before, the abort armed and acknowledged on return when it won its transition; a lost transition leaves the winner's obligation untouched; no owner exists. |
| The tool call is cancelled inside an inline abort | The abort finishes anchored; if it won, its completion tells the engager live with the acknowledgement; the cancellation owner finds the record terminal and tells nobody. |
| The stop latches while `open()` is suspended | The ledger cancels the tool call; if the client's connect consumed that cancellation, the latch check reports `launch_cancelled` inline, armed, acknowledged on return, and mints no owner. |
| The turn dies after `pending` | The owner reports through the reporter (strict transition, bounded notice, driver teardown, topic aborted) and tells the engager live with the acknowledgement that clears the obligation. |
| The bus drops the engager's role | The message is dropped silently by the bus; the obligation stays armed; boot replays it (INV-ENG-018). |
| The terminal write of the death rolls back | Record live, topic open, one bounded notice, client retired, unarmed `launch_outcome_uncommitted` to the engager; the consumer says "still open". |
| The owner is cancelled mid-turn | The cancellation owner reports and tells, as the tool call's cancellation arm did; a rolled-back persist gives the uncommitted notice, a lost race nothing. |
| The owner is cancelled inside its telling | The telling completes, shielded and anchored; no cancellation owner is spawned. |
| The owner is cancelled before its first step | Its done callback drops the handle and spawns the cancellation owner; after the drains are marked complete it logs and leaves the record live for boot. |
| A launcher is cancelled after the drains are marked complete | Its compensation is not minted; logged; boot re-idles the live record. A named-fault arm it reaches before the cancellation lands spawns no abort either: it is refused as the cancellation it is. |
| A launcher is cancelled between creating its topic and its record | Its topic abort is anchored and the stop drains it (it is in no ledger, having no record); after the drains are marked complete it is not minted and the topic left open without a record is logged. |
| The tool call's task is cancelled after it returned | Nothing: the turn belongs to the owner. |

## Extension points

**A new driver that wants its launch detached** declares `supports_split_launch` and splits
`start()` into `open()` and `run_launch_turn()` with `start()` kept as their composition;
everything the tool call must still answer for belongs in `open()`. A driver without the
marker keeps the inline shape, and a marker inferred from method presence was tried and cut —
a mock answers `hasattr` for anything.

**A new outcome the owner must tell** decides first whether the record is terminal. If it is,
the telling is armed in the transition and cleared by delivery; if it is not, the notice is
unarmed and carries a kind the consumer narrates as non-terminal, with its own branch.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::_hand_off_launch_turn`
- `casa/rootfs/opt/casa/tools.py::_own_in_casa_launch`
- `casa/rootfs/opt/casa/tools.py::_launch_turn_done`
- `casa/rootfs/opt/casa/tools.py::_tell_engager_launch_outcome`
- `casa/rootfs/opt/casa/tools.py::send_engagement_outcome`
- `casa/rootfs/opt/casa/tools.py::_schedule_terminal_ack`
- `casa/rootfs/opt/casa/tools.py::drain_launch_turns`
- `casa/rootfs/opt/casa/tools.py::stop_engagement_launches`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.open`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.run_launch_turn`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.launch_shutdown_active`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.mark_launch_drains_complete`

**Tests**
- `tests/test_in_casa_launch_detach.py`
- `tests/test_launch_death_reporter.py`
- `tests/test_in_casa_launch_terminal_artifact.py`

**Related**
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/engagement-failure-and-restart.md`](../architecture/engagement-failure-and-restart.md)
- [`architecture/engagement-terminal-telling.md`](../architecture/engagement-terminal-telling.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/sdk-client-pool.md`](../architecture/sdk-client-pool.md)
<!-- END SOURCEMAP -->
