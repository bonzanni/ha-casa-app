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
and dies with the process, and it has no in-flight veto and no
forced-boundary valve — the refusal ending the turn releases the per-turn lock, which is
what delivers the queued turn, and a ticket whose delivery fails is discharged only after
one bounded failure notice, never retried into a permanent veto. Accessor failures fail
open with a warning rather than wedging termination.

**A driver that cannot see its inbound state must not answer as if there were none.** The
claude-code driver's inbound knowledge and its delivery machinery have different lifetimes,
and conflating them is how a terminal came to report nothing about messages that were still
on disk. The durable envelopes, the sequence counter and the operator-message generation
belong to the *engagement*; the FIFO writer, the notice sender and the epoch and turn probes
belong to *one CLI process*. A session teardown ends the process, so it retires the delivery
runtime and keeps the ledger — the accessors keep answering across a respawn, and a message
queued before the teardown still refuses a completion after it. Where this process never
attached an incarnation at all — a replay that refused, an attach that failed, a rebuild that
did not finish — the two *text* accessors answer from the durable spool file instead, so a
terminal path still has something to disclose. That file-sourced answer never refuses
anything: the refusal would depend on machinery whose absence created the state, so nothing
could clear it, and a completion that can never happen also never reaches the disclosure it
was refused for. An engagement nothing was ever enqueued for answers empty, because the spool
file does not exist until the first message is written to it. The claude-code-only
escalation that forces a turn boundary after repeated refusals stays scoped to the queued
population: a queued message cannot move until a respawn re-arms delivery, which is what the
escalation forces, while an in-flight one is already past that boundary and killing its epoch
could destroy a message a turn had just consumed.

**INV-ENG-016**: A `claude_code` engagement's inbound ledger — its durable envelopes and its message generation — outlives the CLI incarnation that serves it: a session teardown retires the delivery runtime and keeps the ledger, so unread and in-flight state stays visible to the completion gate and to every terminal disclosure across a respawn. Where no incarnation of this process ever attached, a terminal disclosure hook still answers from the durable spool file it can read, and that file-sourced answer discloses without ever refusing a completion. An engagement for which nothing was ever enqueued answers empty.

**INV-ENG-017**: An ingress reservation taken for an operator message carries that message's text from the moment the message is accepted until the moment it becomes durable in the engagement's spool — or is known not to have — on a ledger that survives a session teardown and never on the spool; so a terminal disclosure quotes the text of every accepted message that never became durable, quotes a message that did become durable from the spool population alone, and keeps its count and its "up to" hedge exactly as a text-less reservation would have produced them.

**A message that dies with the engagement is disclosed, not swallowed.** Every terminal
outcome posts the messages no turn ever took up into the topic — both text populations, at
any age, excerpted and counted, plus a count of pending ingress reservations. The claim it
makes is what the system can evidence: that no turn start was *recorded* for them before the
engagement ended. It does not claim they were never read, because that is not provable for a
message already handed to the CLI — the CLI can read the line and emit its init frame before
the relay processes it, and a cancellation landing in that interval would otherwise assert
something false about a message the agent did see.

**A reservation carries its message's text, and its count stays an upper bound.** On the
claude-code side the handler is holding the operator's exact words when it reserves, so the
reservation carries them — keyed by the Telegram message id, on the same engagement-lifetime
ledger as the counter, never on the spool — and a terminal quotes what was accepted and never
reached the spool rather than only counting it. The text is dropped the instant the message
becomes durable, at the enqueue's own successful write, so one message is quoted once, from
wherever it currently lives, and a terminal can never see it as both a spooled envelope and a
text-bearing reservation. A message that arrives with no id, and every in-casa reservation, is
counted and not quoted: the in-casa reservation is taken for a system continuation whose text
does not exist anywhere yet, which is the contrast that makes the claude-code case the
achievable one. The count itself is unchanged, and so is its hedge. Because a reservation is
still anonymous to the count, it can alias a text the disclosure already excerpts: a message
is durably spooled before its reservation is released, and a terminal landing inside that
window sees the same message in both populations. A total that includes reservations
therefore reads "up to N", which is true in that window; a text-only total keeps the exact
claim. One reservation is excluded
by construction: the one a recognized command (`/cancel`, `/complete`, `/silent`) holds for
itself while the handler processes it — classified at the reservation's birth, so the
exclusion holds under *every* terminal winner, not only the command's own finalize — still
counts toward the completion veto but is never disclosed as lost, because a command is
consumed by the handler and never delivered to the model. The operator's ungated complete
command finalizes past unread input deliberately (above), and it does so *disclosing* —
the topic post counts what it committed past, including foreign reservations.
The launch-death reporter folds the same projection into its own disclosure.

**The in-casa driver reserves too, and the reason is a message that does not exist yet.** Its
other counts are all backed by a text: a ticket carries the exact prompt from the moment it is
admitted. A broker-driven *system continuation* — the resume turn dispatched after an operator
approves an install consent or an engagement-origin tool authorization — has no text anywhere
until the delivery seam admits it, and the interval before that is not short. On the two
install-consent arms it is the finish hook's task-scheduling gap, one event-loop iteration and
unremovable; on the authorization arm it is a whole message-edit round trip to Telegram,
because the approval edit is awaited before the continuation is dispatched. A successful
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
the second is a no-op — which matters because the arms that never reach the seam (a denial, an
expiry, an unrecorded approval, a raising edit) have only the hook to dispose of theirs. An
unreleased reservation would make a successful completion permanently impossible, which is
exactly what this hook must never do; the in-casa driver has no forced-boundary valve to
relieve one, so release is the only exit.

Two things it deliberately is not. It is *not* the reservation the operator-message path takes:
that path already admits a text-bearing ticket at handler entry, so reserving there too would
count one message twice in the veto and inflate the lost-message disclosure. And its counters
have no attach step and no detach step — nothing tears them down and nothing but a release
removes an entry — so an absent key means *nothing is held*, never *the answer is unavailable*.
That distinction was for a while the whole of a defect on the other driver, where a session
respawn dropped an in-memory spool and its *spool-backed* accessors then reported zero for
messages still durably queued — never its reservation counters, which had no teardown to
zero them, which is exactly why they were the only arm still working. That driver's ledger now
survives the respawn too (above), so the defect is closed; the rule this paragraph states is
not, and adding a lifecycle teardown to a reservation counter would reintroduce the shape by
construction.

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
strength.** The registry answers it synchronously and identically for both drivers — terminal
refuses, `idle` delivers and becomes `active` without re-stamping the last-turn time, `active`
delivers, and an unknown record delivers rather than refusing. What differs is where the answer
can be placed.

For `claude_code` the placement is exact: the FIFO opens only once the agent process is
reading, and that process observes nothing until a synchronous first write, so there is one
instant at which "a turn is about to be delivered" and "the engagement has seen nothing of it"
are both true.

For `in_casa` there is no such instant, because the first thing the engagement sees is an
awaited call into the SDK client. The admission is therefore taken inside that engagement's own
per-turn lock, immediately before that call, with only synchronous statements between:
acquiring the lock awaits, but once it is held no other coroutine on Casa's loop — no inbound
tool call, no terminal transition — runs between the decision and the call. Placing it earlier,
beside the driver's liveness check, would put the awaiting lock acquisition between the two and
reopen exactly the window it closes.

**That is a weaker claim than the `claude_code` one, and the difference is worth stating
exactly rather than blurring.** The client call is awaited, and whether it reaches its
transport write before its first internal suspension is the SDK's business, not Casa's. So a
terminal transition CAN commit while the hand-off is in progress and the prompt still arrives —
measured, not supposed. That is the same class of limit the `claude_code` half has always
carried and states: the admission fences the decision to deliver, not the delivery, and a
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
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.inbound_reservation_texts`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.drop_reservation_text`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.record_completion_refusal`
- `casa/rootfs/opt/casa/drivers/claude_code_driver.py::ClaudeCodeDriver.force_completion_turn_boundary`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.inbound_unread_depth`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.inbound_unread_texts`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.inbound_in_flight_texts`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.reserve_inbound`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.release_inbound_reservation`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.inbound_reservations`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.inbound_message_reservations`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver.inbound_reservation_texts`

**Tests**
- `tests/test_emit_completion_tool.py`
- `tests/test_claude_code_driver.py`
- `tests/test_answer_reservation.py`
- `tests/test_in_casa_inbound_admission.py`
- `tests/test_c1_continuation_admission.py`
- `tests/test_launch_death_reporter.py`

**Related**
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
<!-- END SOURCEMAP -->
