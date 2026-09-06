---
last_reviewed: 2026-09-01
---

# Engagement terminal telling

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a terminal engagement's topic and its engager are told, and what may be claimed about
it: the outcome mark the topic wears only once it has been told, the single plain
disclosure that stands in for an unconfirmed completion post, and the durable obligation to
tell the party that asked for the work — armed in the terminal write itself, cleared only by
delivery, replayed at boot. The terminal transition that wins, its strictness, the side
effects behind the flip and the ordering of topic output are in
[`architecture/engagement-finalization.md`](engagement-finalization.md); what a terminal
outcome discloses about inbound messages nobody read is in
[`architecture/engagement-inbound-disclosure.md`](engagement-inbound-disclosure.md); what a restart
replays is in
[`architecture/engagement-failure-and-restart.md`](engagement-failure-and-restart.md).

## Mental model

**A terminal status is not proof that anyone was told, so the telling carries its own
evidence.** The topic's outcome mark is withheld until the wire acknowledged the post that
explains it, and the engager's notification is an obligation the record keeps until a
delivery acknowledgement clears it — never the bus accepting a message, never the process
merely having tried. Both directions fail toward telling again rather than toward silence.

## Contracts & invariants

**INV-ENG-018**: An engagement outcome committed by the finalization funnel carries, in the same durable write as its terminal status, the obligation to tell the party that asked for the work. That obligation is cleared only by the delivery acknowledgement of the notice that carries it, never by the bus accepting the message and never by another record's acknowledgement; a record still carrying it is exempt from terminal-retention expiry, so the obligation outlives the process. Casa's boot owner replays every record still owing one, addressed from that record's own persisted origin and reporting the FACT of the outcome, never a retained answer the record does not hold; startup awaits that owner unguarded, once the channels and the resident loops are running. A rolled-back transition owes nothing, a row written before the field existed owes nothing, and no other terminal writer arms it.

The obligation follows the WRITER, not the record. This funnel and the launch-death reporter
reach the same registry method with the same outcome on the same kind of record, and only the
first announces over the bus — so no predicate over a record could tell them apart, and the
one caller that announces opts in explicitly while every other terminal writer is correct by
default. Arming it inside the transition rather than after it is the whole safety property:
one durable write carries both facts, so there is no window in which the outcome is committed
and nobody owes the telling. The strict rollback restores it with the rest of the snapshot,
because a transition that did not reach disk announced nothing.

Clearing it is deliberately harder than sending it. `bus.notify` reports only that a message
was ACCEPTED onto a queue: an unregistered target role is dropped with no exception and no log
line, an enqueued message dies with the process at shutdown, and a dispatched turn can still
fail to reach the transport. None of those is a telling. The discharge is the same
delivery-acknowledgement seam every durable announcement uses, and it is deliberately
generous in the retain direction — a turn that reported a generic failure rather than the
engagement's news leaves the obligation owed, and so does an acknowledgement that itself
raises. The cost of not clearing is one duplicate; the cost of clearing early is silence.

**INV-ENG-013**: A terminal engagement's topic is never marked with an outcome the topic was never told. The completion post counts as delivered only when the wire acknowledged it; an acknowledgement that did not come withholds the outcome mark and produces exactly one bounded plain disclosure in its place. A post that failed part-way is never replayed. The topic is closed exactly once either way, and no topic operation can strand the post-topic tail behind it.

Until this release the completion post's failure was caught and logged, and the funnel then
marked the topic with its outcome emoji regardless. A record could therefore be durably
`completed` over a topic that received nothing, wearing a ✅. The mark is the defect, not the
missing summary: the missing summary is a delivery failure, while the mark is Casa asserting
that the delivery happened.

**Confirmation is the message id the sender returned, not the fact that the call returned.**
Both direct topic senders are typed to return an id and to raise on failure, so a non-`None`
return is a wire acknowledgement — which is all it claims, and never that a person read it.
The distinction matters because of the sequencer route: the sequencer's completion seam
reports a *definite* send failure by returning nothing, without raising, and the driver hook
in front of it used to discard that answer and report success. On that route the funnel
skipped its own direct send and painted and closed with no exception raised anywhere for
anyone to catch. A predicate built on "did the call raise?" is blind to it entirely.

A sequencer attempt that *raises* is treated differently from one that reports nothing sent.
A raise can come part-way through a paginated send, so some pages may already be in the
topic; replaying the whole summary through the direct sender would duplicate content and
then paint and close over the duplicate. A definite "nothing sent" is safe to follow with a
direct attempt; a raise is not, and gets only the disclosure.

The rule is uniform across outcomes. A cancellation emoji over a topic that never heard why
is the same false assertion as a completion emoji, so `completed`, `cancelled` and `error`
are all gated the same way.

The disclosure says the summary could not be *confirmed*, never that it was not posted: a
transport timeout can lose the acknowledgement of a message the wire accepted. It asserts
only what the funnel can see at that point — the outcome the transition committed, that the
post was not confirmed, and that the mark is being withheld — and nothing about the close
below it, the retention ledger, the engager notification or what the record holds, all of
which are best-effort or downstream. Three successive reviews found the same shape in this
prose, each time a sentence claiming state its own site could not see, so the class of claim
was removed rather than reworded a fourth time. **Whether a notice obeys that rule is a
review question, not a test one.** The suite pins each notice by whole-sentence equality, so
none of them can change silently; what no test can settle is whether a NEW sentence, shipped
with its expectation updated alongside it, is true. A blacklist of forbidden phrases was
tried and cut — it was bypassed three times by respelling the same false claims — so the
rule lives here and at the production sites, where the person making that edit reads it. It counts as a telling in its own right — if it lands, the topic has been told why the engagement ended,
which is what a follow-up turn's owner reads (INV-ENG-012) — and its confirmation is the
returned id, on the same rule as the summary's.

**The close is unconditional, and bounded.** Withholding the close as a second failure
signal was considered and rejected on review. It is not authoritative: the mark and the
close are independent best-effort operations, so a *confirmed* post whose mark and close both
fail leaves exactly the state a withheld close was meant to distinguish. And it is not free:
retention admission happens earlier in this funnel and is not gated on delivery, so the sweep
deletes the topic and every message in it on its deadline — while a terminal topic left open
still accepts messages, each answered with a refusal, so anything typed there is deleted with
it. The withheld MARK carries the whole signal, and the disclosure names the retention
deadline. The close is bounded like every other topic operation here, because it is the one
thing standing between an already-terminal record and its tail: a close that never returned
would strand the driver teardown, the notification and the retains, and with them any
follow-up turn waiting on the teardown to end its stream.

Three things this invariant does *not* do. It does not re-open a completed record because a
channel failed — the work did complete, and reverting the transition would restore tool
authority to an engagement already declared finished. It does not change the funnel's return
value: the transition won, and only the telling failed. And it does not shield, detach or
reorder anything, so INV-ENG-010's guarantee is untouched — which is precisely what makes
this arm separable from the still-open cancellation exposure described under INV-ENG-010.

## Failure behavior

What fails after the transition — topic sends, driver teardown, retention — and how the
notification stopped being among the best-effort losses are stated as failure rows in
[`architecture/engagement-finalization.md`](engagement-finalization.md); the two invariants
above are what those rows point at.

## Extension points

**A new terminal writer that announces an outcome** must arm the obligation explicitly in
its own terminal write, as the finalization funnel does; the obligation follows the writer,
not the record, and no predicate over a record can infer it.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::_finalize_engagement`
- `casa/rootfs/opt/casa/tools.py::_finalize_engagement_tail`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.try_transition_terminal`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.records_owing_terminal_notification`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.ack_terminal_notification`
- `casa/rootfs/opt/casa/casa_core.py::_notify_recovered_engagement_outcomes`

**Tests**
- `tests/test_finalize_engagement.py`
- `tests/test_engagement_terminal_notification.py`
- `tests/test_engagement_terminal_notification_replay.py`

**Related**
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/engagement-completion-gate.md`](../architecture/engagement-completion-gate.md)
- [`architecture/engagement-inbound-disclosure.md`](../architecture/engagement-inbound-disclosure.md)
- [`architecture/engagement-failure-and-restart.md`](../architecture/engagement-failure-and-restart.md)
- [`architecture/engagements.md`](../architecture/engagements.md)
<!-- END SOURCEMAP -->
