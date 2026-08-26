---
last_reviewed: 2026-08-23
---

# Engagement completion gate

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The admission side of ending an engagement: what a *successful* completion is refused over,
how "unread" and "in flight" differ and why the gate needs both, what each driver counts, and
how a message that dies with the engagement is disclosed rather than swallowed. The terminal
transition itself, the strictness that keeps the persisted and in-memory records agreeing, the
finalization side effects behind the flip and topic output ordering are in
[`architecture/engagement-finalization.md`](engagement-finalization.md). The record, how one is
launched and how a turn is admitted to it are in
[`architecture/engagements.md`](engagements.md).

## Mental model

**Completion is gated on unread input.** A *successful* completion is refused while inbound
messages are unread or reserved — an agent cannot declare victory over a question it has not
read. Failure and cancellation deliberately bypass that gate, because something going wrong
must always be able to end.

**"Unread" and "in flight" are different questions, and the gate needs both.** A message that
has left the queue but not yet reached a turn — written into the engagement's stdin FIFO,
with no turn-start evidence back — is invisible to the unread accounting, deliberately: that
accounting also answers the ask gate's question, *is the operator still waiting to be
answered?*, and a delivered message may well have been read. The terminal question is a
different one, *is there operator input in flight that will only be taken up after I commit?*,
and the same exclusion is wrong for it. So the driver answers both separately rather than
changing what "unread" means. The completion gate refuses on either, and the annotation
described below discloses both. There is no third state to track for a message being written
right now: it is still queued throughout its own write, so the unread accounting already
holds it.

## Contracts & invariants

**INV-ENG-003**: A successful completion is refused while unread inbound messages, inbound messages in flight to the engagement's CLI, or inbound reservations exist, when the driver exposes its inbound state.

Enforced both as a pre-check and again as a hook inside the transition itself, so the
condition is re-evaluated at the moment the state changes rather than only before it. The
hook is where the invariant actually lives: delivery can happen during the finalize path's
own awaits, long after the pre-check found the spool clean.

The in-flight half of the refusal is bounded in time, and that asymmetry is deliberate. A
delivered message normally reaches a turn within a second; one that has not after far longer
is evidence that no turn is coming, and a veto on a state nothing will clear would make a
successful completion impossible for the rest of the engagement's life — the opposite of this
hook's fail-open contract. Past the bound the completion proceeds and the message is still
disclosed. Each expiry is logged, so the bound is answerable to production rather than
permanently a guess.

What it does not cover: failed and cancelled outcomes intentionally skip the gate, and so
does the operator's own complete command — only the completion *tool* arms the gate, so an
operator marking an engagement complete finalizes past unread input deliberately. The gate
exists where the driver implements the inbound accessors — today that is both drivers. The
claude-code driver counts its durable spool, its in-flight envelopes and its ingress
reservations. The in-casa driver counts admission tickets: a turn is unread from its
synchronous admission at the Telegram entry seam until the embedded client takes the prompt,
then disclosure-only until the first model-evidence frame; the ticket ledger is in-memory
and dies with the process, and it has no in-flight veto, no reservations and no
forced-boundary valve — the refusal ending the turn releases the per-turn lock, which is
what delivers the queued turn, and a ticket whose delivery fails is discharged only after
one bounded failure notice, never retried into a permanent veto. Accessor failures fail
open with a warning rather than wedging termination. The claude-code-only
escalation that forces a turn boundary after repeated refusals stays scoped to the queued
population: a queued message cannot move until a respawn re-arms delivery, which is what the
escalation forces, while an in-flight one is already past that boundary and killing its epoch
could destroy a message a turn had just consumed.

**A message that dies with the engagement is disclosed, not swallowed.** Every terminal
outcome posts the messages no turn ever took up into the topic — both text populations, at
any age, excerpted and counted, plus a count of pending ingress reservations. The claim it
makes is what the system can evidence: that no turn start was *recorded* for them before the
engagement ended. It does not claim they were never read, because that is not provable for a
message already handed to the CLI — the CLI can read the line and emit its init frame before
the relay processes it, and a cancellation landing in that interval would otherwise assert
something false about a message the agent did see.

**A reservation contributes a count, and the count is an upper bound.** A message still
inside its ingress-reservation window has no text anywhere — the reservation is a bare
counter — so it cannot be excerpted, only counted. And because the reservation is anonymous,
it can alias a text the disclosure already excerpts: a message is durably spooled before its
reservation is released, and a terminal landing inside that window sees the same message in
both populations. A total that includes reservations therefore reads "up to N", which is
true in that window; a text-only total keeps the exact claim. One reservation is excluded
by construction: the one a recognized command (`/cancel`, `/complete`, `/silent`) holds for
itself while the handler processes it — classified at the reservation's birth, so the
exclusion holds under *every* terminal winner, not only the command's own finalize — still
counts toward the completion veto but is never disclosed as lost, because a command is
consumed by the handler and never delivered to the model. The operator's ungated complete
command finalizes past unread input deliberately (above), and it does so *disclosing* —
the topic post counts what it committed past, including foreign reservations.
The launch-death reporter folds the same projection into its own disclosure.

## Failure behavior

**Completion is refused for unread input.** The transition is vetoed, the record stays live,
and the caller gets a retryable outcome naming the condition. This is a precondition failure,
not an error state.

**An inbound accessor raises.** The gate fails open with a warning rather than wedging
termination: a driver that cannot answer the question does not get to make an engagement
unendable.

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
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.inbound_unread_depth`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.inbound_unread_texts`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.inbound_in_flight_texts`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.inbound_in_flight_blocking`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.inbound_reservations`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.inbound_message_reservations`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.record_completion_refusal`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.force_completion_turn_boundary`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.inbound_unread_depth`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.inbound_unread_texts`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.inbound_in_flight_texts`

**Tests**
- `tests/test_emit_completion_tool.py`
- `tests/test_claude_code_driver.py`
- `tests/test_answer_reservation.py`
- `tests/test_in_casa_inbound_admission.py`

**Related**
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
<!-- END SOURCEMAP -->
