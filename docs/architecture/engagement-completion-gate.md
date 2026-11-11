---
last_reviewed: 2026-11-09
---

# Engagement completion gate

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The admission side of ending an engagement: what a *successful* completion is refused over,
why a message written into the session still counts until the job takes it up, and what the
driver counts.
Completion owes no reload: each configuration commit reloads what it changed (INV-TOOL-014, in
[`architecture/tools-interface.md`](tools-interface.md)).
How a message that died with the job is told — once, by the seam or the job's message journal
— is in [`architecture/engagement-inbound-disclosure.md`](engagement-inbound-disclosure.md). The terminal
transition itself, the strictness that keeps the persisted and in-memory records agreeing, the
finalization side effects behind the flip are in
[`architecture/engagement-finalization.md`](engagement-finalization.md). The record and how one
is launched are in [`architecture/engagements.md`](engagements.md); how a write is admitted to
it, and how far that admission's fence reaches, is in
[`architecture/engagement-turn-admission.md`](engagement-turn-admission.md).

## Mental model

**Completion is gated on unread input.** A *successful* completion is refused while inbound
messages are unread or reserved — an agent cannot declare victory over a question it has not
read. Failure and cancellation deliberately bypass that gate, because something going wrong
must always be able to end.

**Written is not read.** The in-casa driver counts the operator's messages as admission
tickets: a message is unread from its synchronous admission at the seam that hands it to the
job, through its write into the job's session, until the CLI echoes its uuid back as it takes
the message into a turn — the take-up, which discharges the ticket. A message the delivery
worker has written and the CLI has not yet taken up still vetoes: the job has not read it. Casa's
own writes (the brief, a nudge, the continue and restart lines) hold no ticket. A ticket that a
terminal outcome strands is told by the job's message journal
([`architecture/engagement-inbound-disclosure.md`](engagement-inbound-disclosure.md),
INV-ENG-029).

## Contracts & invariants

**INV-ENG-003**: A successful completion requested through the completion tool is refused while an operator message to the engagement is admitted, or written and not yet taken up, or an inbound reservation is held, when the driver exposes its inbound state.

Enforced both as a pre-check and again as a hook inside the transition itself, so the
condition is re-evaluated at the moment the state changes rather than only before it. The
hook is where the invariant actually lives: delivery can happen during the finalize path's
own awaits, long after the pre-check found the inbox clean.

What it does not cover: failed and cancelled outcomes intentionally skip the gate — only the
completion *tool* arms it, so an operator's cancellation finalizes past unread input
deliberately. The gate
exists where the driver implements the inbound accessors, which the in-casa driver does. Its
ticket ledger is in memory; after a restart or a session death it is rebuilt from the job's
message journal for every message the session transcript lacks (INV-BGJOB-021, in
[`background-job-message-journal.md`](background-job-message-journal.md)). It has no
forced-boundary valve and needs none: the refusal is a tool result, so the message waiting for
the job is taken up at the job's next step, and a message the job never reads is discharged
after its one notice, never retried into a permanent veto. Accessor failures fail open with a
warning rather than wedging termination.

**The in-casa driver also reserves, and the reason is a message that does not exist yet.** Its
other counts are all backed by a text: a ticket carries the exact prompt from the moment it is
admitted. A broker-driven *system continuation* — the message dispatched after an operator
answers an engagement-origin tool authorization, or taps an answer to a job's question — has no
text anywhere until the delivery seam admits it, and the interval before that is not short: a whole message-edit round trip to
Telegram, because the DM edit is awaited before the continuation is dispatched. (An install
consent or an erase-data answer continues no engagement: it continues the configurator,
[`configurator.md`](configurator.md).) A successful
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
count one message twice in the veto. And its counters
have no attach step and no detach step — nothing tears them down and nothing but a release
removes an entry — so an absent key means *nothing is held*, never *the answer is unavailable*.
Adding a lifecycle teardown to a reservation counter would break that: a teardown would read
as "nothing held" for a reservation still owed.

**The delivery seam reports acceptance, and only that.** The seam a continuation goes through
returns whether the job accepted the message — journaled, admitted and queued, in one
synchronous step with no wait on the session — and false when the record was terminal, the
journal could not be written or Casa was stopping, which is what the operator-facing approval
message is selected from. It is not a delivery receipt: the delivery worker writes the message
later, and the job can end before it does. Such a message is the job's message journal's to
tell, once, and the delivery worker's own admission keeps it out of the terminal session
([`engagement-turn-admission.md`](engagement-turn-admission.md), INV-ENG-009). Accepted is
what the message says.

## Failure behavior

**Completion is refused for unread input.** The transition is vetoed, the record stays live,
and the caller gets a retryable outcome naming the condition (`unread_inbound`). This is a
precondition failure, not an error state. The refusal tells the job what happens next: "An
operator message reaches you at your next step. Read it, then call emit_completion again." In
the job's continuous session that is literally true — the refusal is a tool result, and the
message waiting for the job is taken up right after it.

**An inbound accessor raises.** The gate fails open for THAT accessor, with a warning rather
than wedging termination: a driver that cannot answer one question does not get to make an
engagement unendable — and does not get to answer for the other reads either. Each of the
terminal hook's reads is guarded on its own and contributes only its own value, so a completion
is still refused over the unread depth or reservations the gate could still read.

**An inbound accessor is absent.** The same contribution, without the warning: an optional
accessor a driver never implements answers empty because nothing failed, so there is nothing to
report. `InCasaDriver` implements every accessor the gate reads, so absence arises only for a
driver that does not.

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
