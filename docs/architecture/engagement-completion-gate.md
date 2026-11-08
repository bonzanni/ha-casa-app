---
last_reviewed: 2026-10-02
---

# Engagement completion gate

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The admission side of ending an engagement: what a *successful* completion is refused over,
why a message in flight is disclosed but never vetoed, and what the driver counts.
Completion owes no reload: each configuration commit reloads what it changed (INV-TOOL-014, in
[`architecture/tools-interface.md`](tools-interface.md)).
What a terminal outcome then discloses about the messages that died with the engagement is in
[`architecture/engagement-inbound-disclosure.md`](engagement-inbound-disclosure.md). The terminal
transition itself, the strictness that keeps the persisted and in-memory records agreeing, the
finalization side effects behind the flip are in
[`architecture/engagement-finalization.md`](engagement-finalization.md). The record and how one
is launched are in [`architecture/engagements.md`](engagements.md); how a turn is admitted to
it is in [`architecture/engagement-turn-admission.md`](engagement-turn-admission.md).

## Mental model

**Completion is gated on unread input.** A *successful* completion is refused while inbound
messages are unread or reserved — an agent cannot declare victory over a question it has not
read. Failure and cancellation deliberately bypass that gate, because something going wrong
must always be able to end.

**"In flight" is disclosed, never vetoed.** The in-casa driver counts admission tickets: a
turn is unread from its synchronous admission at the Telegram entry seam until the embedded
client takes the prompt, then in flight — disclosure-only — until the first model-evidence
frame. A blocking count of the in-flight population could only veto the carrying turn's own
completion, so the driver deliberately implements none; the annotation described in
[`architecture/engagement-inbound-disclosure.md`](engagement-inbound-disclosure.md) still
quotes what was in flight when a terminal committed.

## Contracts & invariants

**INV-ENG-003**: A successful completion requested through the completion tool is refused while unread inbound messages or inbound reservations exist, when the driver exposes its inbound state.

Enforced both as a pre-check and again as a hook inside the transition itself, so the
condition is re-evaluated at the moment the state changes rather than only before it. The
hook is where the invariant actually lives: delivery can happen during the finalize path's
own awaits, long after the pre-check found the inbox clean.

What it does not cover: failed and cancelled outcomes intentionally skip the gate, and so
does the operator's own complete command — only the completion *tool* arms the gate, so an
operator marking an engagement complete finalizes past unread input deliberately. The gate
exists where the driver implements the inbound accessors, which the in-casa driver does. Its
ticket ledger is in-memory and dies with the process, and it has no forced-boundary valve —
the refusal ending the turn releases the per-turn lock, which is what delivers the queued
turn, and a ticket whose delivery fails is discharged only after
one bounded failure notice, never retried into a permanent veto. Accessor failures fail
open with a warning rather than wedging termination.

**The in-casa driver also reserves, and the reason is a message that does not exist yet.** Its
other counts are all backed by a text: a ticket carries the exact prompt from the moment it is
admitted. A broker-driven *system continuation* — the resume turn dispatched after an operator
answers an engagement-origin tool authorization — has no text anywhere until the delivery seam
admits it, and the interval before that is not short: a whole message-edit round trip to
Telegram, because the DM edit is awaited before the continuation is dispatched. (An install
consent or an erase-data answer continues no engagement: it continues the configurator on its
desk, [`configurator.md`](configurator.md).) A successful
completion landing inside it used to read an empty inbox, commit, and take the engagement
terminal, and the approved continuation was then dropped with only a log line to show for it.

So the reservation is taken at the one *synchronous* instant inside that window — the tap's
commit step, which runs in the Telegram callback with no await after the answer is committed,
before the finish hook's task has been scheduled at all — and it is released in the same
synchronous step in which the seam admits the ticket. Admit first, release second, no await
between: the gate can never observe both populations empty, and a completion racing the
hand-over is refused by whichever of the two it happens to read.

Release is guaranteed by an idempotent lease rather than by discipline. The lease owns its own
held bit, so the seam's release and the finish hook's whole-body release are both correct and
the second is a no-op — which matters because the arms that never reach the seam (a raising
edit, for one) have only the hook to dispose of theirs. An
unreleased reservation would make a successful completion permanently impossible, which is
exactly what this hook must never do; the in-casa driver has no forced-boundary valve to
relieve one, so release is the only exit.

Two things it deliberately is not. It is *not* the reservation the operator-message path takes:
that path already admits a text-bearing ticket at handler entry, so reserving there too would
count one message twice in the veto and inflate the lost-message disclosure. And its counters
have no attach step and no detach step — nothing tears them down and nothing but a release
removes an entry — so an absent key means *nothing is held*, never *the answer is unavailable*.
Adding a lifecycle teardown to a reservation counter would break that: a teardown would read
as "nothing held" for a reservation still owed.

**The delivery seam reports its hand-off, and only that.** The seam a continuation goes through
now returns whether it handed the turn to a delivery task — false when the resume gate refused
or shutdown had begun — which is what the operator-facing approval message is selected from. It
is deliberately not the admission decision. The admission happens inside the engagement's
per-turn lock, and that lock is held for a whole turn; waiting for it would park a tap callback
behind an unbounded model turn and leave the approval keyboard unedited for its duration, which
was measured rather than supposed. So one residual stays open and is stated rather than
implied: an ungated terminal writer can still terminalize between the hand-off and the
admission, after the operator has been told a continuation was requested. Requested is what the
message says.

**One decision, asked at two different instants, and the difference is the guarantee's actual
strength.** The registry answers it synchronously — terminal refuses, `idle` delivers and
becomes `active` without re-stamping the last-turn time, `active` delivers, and an unknown
record delivers rather than refusing. What matters is where the answer can be placed.

For `in_casa` there is no instant at which "a turn is about to be delivered" and "the
engagement has seen nothing of it" are both true, because the first thing the engagement sees
is an awaited call into the SDK client. The admission is therefore taken inside that engagement's own
per-turn lock, immediately before that call, with only synchronous statements between:
acquiring the lock awaits, but once it is held no other coroutine on Casa's loop — no inbound
tool call, no terminal transition — runs between the decision and the call. Placing it earlier,
beside the driver's liveness check, would put the awaiting lock acquisition between the two and
reopen exactly the window it closes.
A fresh job's turn is the one place something awaits after that admission: its conversation
reset runs under the same lock (INV-BGJOB-005, [`background-job-fresh-sessions.md`](background-job-fresh-sessions.md)), so
the admission is asked again, synchronously, once the reset is confirmed, and that second
answer is the one immediately before the call.

**That claim is bounded, and the bound is worth stating exactly rather than blurring.** The
client call is awaited, and whether it reaches its transport write before its first internal
suspension is the SDK's business, not Casa's. So a terminal transition CAN commit while the
hand-off is in progress and the prompt still arrives — measured, not supposed. The admission
fences the decision to deliver, not the delivery, and a
terminal landing once a turn has begun cannot revoke it because there is nothing to revoke it
with. Stopping an in-flight turn is the driver teardown's job on the finalize path, which is
why that teardown exists. What the admission buys is that a turn is never *begun* against a
record already known terminal — which is the reachable case, since the window it closes spans
a lock acquisition and every await before it, while this one spans a single client call.

A refused follow-up needs no new machinery and gets none. The refusal is raised as a *kind of*
"this driver has no live client", which every caller on that path already handles: the delivery
task re-reads the record, sees a terminal status, stays quiet rather than announcing a failure
the terminal writer is already announcing, and settles the turn's admission ticket exactly once
— one bounded attempt to tell the operator their message was not delivered, then release, never
a retry. Both halves of the invariant's last sentence are that one path.

## Failure behavior

**Completion is refused for unread input.** The transition is vetoed, the record stays live,
and the caller gets a retryable outcome naming the condition. This is a precondition failure,
not an error state.
A fresh job, whose next turn starts with no memory of the refusal, also records the refused
completion so that turn's brief names it ([`background-job-fresh-sessions.md`](background-job-fresh-sessions.md),
INV-BGJOB-007); the reply is the same.

**An inbound accessor raises.** The gate fails open for THAT accessor, with a warning rather
than wedging termination: a driver that cannot answer one question does not get to make an
engagement unendable — and does not get to answer for the other reads either. Each of the
terminal hook's reads is guarded on its own and contributes only its own value, so a completion
is still refused over the unread depth or reservations the gate could still read, and every terminal still discloses the populations that did read — including reservation
texts read beside a count that raised, which are posted under a sentence naming no number. That
arm needs the observed failure as well as a text: a raising count read with nothing else read
posts no paragraph, and a count that answered zero, is absent or answers a non-integer beside a
reservation text is a driver answering off its own clamp, which the renderers do not correct and
this document does not cover.

**An inbound accessor is absent.** The same contribution, without the warning: an optional
accessor a driver never implements answers empty because nothing failed, so there is nothing to
report. `InCasaDriver` implements every accessor the gate reads, so absence arises only for a
driver that does not.

**A disclosure population fails to read.** Those populations are read by the same terminal
hook and under the same per-accessor rule as the reads above; what a terminal then quotes,
counts or declines to number is in
[`architecture/engagement-inbound-disclosure.md`](engagement-inbound-disclosure.md).

## Extension points

**A new driver that owns inbound state** should implement the inbound accessors to arm this
gate for its engagements. A driver that implements none of them is not gated — the refusal is
scoped to what a driver can evidence, which is why the accessors are the seam.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.try_transition_terminal`
- `casa/rootfs/opt/casa/engagement_registry.py::TerminalPreconditionFailed`
- `casa/rootfs/opt/casa/tools.py::_finalize_engagement`
- `casa/rootfs/opt/casa/tools.py::emit_completion`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.inbound_unread_depth`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.reserve_inbound`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.release_inbound_reservation`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.inbound_reservations`

**Tests**
- `tests/test_emit_completion_tool.py`
- `tests/test_answer_reservation.py`
- `tests/test_in_casa_inbound_admission.py`
- `tests/test_c1_continuation_admission.py`

**Related**
- [`architecture/engagement-inbound-disclosure.md`](../architecture/engagement-inbound-disclosure.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/engagement-turn-admission.md`](../architecture/engagement-turn-admission.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
<!-- END SOURCEMAP -->
