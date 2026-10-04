---
last_reviewed: 2026-10-04
---

# Output scope properties: streaming, closing silence and the webhook destination

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a turn's scope decides about the turn itself: whether it streams, whether its final
reply is closing silence — including the operator's #1075 rule for a closing `<silent/>`
after earlier text, and the facts admission records for the durable-announcement discharge
— and where an untrusted webhook turn's output goes. How the scope is minted, how model text
is admitted and the read-before-describe disclosure are
[`output-boundary.md`](output-boundary.md); the INV-TURN-009 stream hold is
[`turn-loop.md`](turn-loop.md); a webhook trigger's `deliver` route is
[`webhook-delivery.md`](webhook-delivery.md).

## Mental model

**Three decisions belong to the turn, not to the call site that acts on them.** Whether the
turn streams and where an untrusted webhook turn's output goes are registered on the scope
when it is minted, from the message's own facts; whether a final reply is closing silence is
judged inside final-reply admission, on the unannotated text. Every reader asks the scope, so
one turn gets one answer wherever it is asked.

## Contracts & invariants


**INV-OUT-006**: Whether a turn streams, whether its final reply is closing silence, and where an untrusted webhook turn's discrete send goes are properties of its scope — the first and third registered at mint from the message's own facts, the second intrinsic to final-reply admission: a scheduled turn or an event wake never receives a token callback, a final reply that strips to nothing but `<silent/>` sentinels is suppressed by admission while prose after a sentinel is delivered whole — except that when a scheduled turn's or an event wake's last text-bearing message strips to a `<silent/>` after earlier text, the reply is suppressed if the turn made at least one Casa send, every one confirmed delivered, every send call resolved without failure, and exactly one attempt ran with no retry, and is otherwise the earlier messages without the closing ones — and an untrusted webhook turn's `send_message` is bound to Telegram whatever channel it named, while its final reply is delivered only when its route declares `deliver: operator` or `operator_always`, and then on every output path only to the operator's Telegram with a fresh delivery context.

The three used to be inline checks — the two-clause callback condition and the sentinel
gate in `handle_message`, the egress clamp in `send_message` — and are now `NoStream`
(read as `TurnScope.streaming_allowed`), the silence judgement inside
`admit(FINAL_REPLY, …)` on the unannotated text (the predicates `strips_to_silence` and
`may_still_be_silence` live in `output_boundary` and are the ones the #650 resume-health
classification and the #666 stream hold use), and `DestinationOperatorOnly` (applied by
`TurnScope.resolve_channel`). The behaviour is unchanged: the tests that pinned the three
checks keep their assertions, and a grep test refuses the old inline forms coming back.

The delivered webhook reply (#1142) is `TurnScope.delivers_to_operator`, read once where
`handle_message` picks the turn's one channel-and-context pair; the error line, the reply and
teardown all use it. The context is fresh because the execution context's `chat_id` keys the
session, and a numeric one would override the channel's chat. The rest of the route is
[`webhook-delivery.md`](webhook-delivery.md)'s INV-TRIG-018; its `operator_always` fallback
for a silent reply (INV-TRIG-021) is a separate Casa send, so admission is unchanged.

What it does not cover: `send_media` still requires a Telegram origin of its own, so a
webhook turn's media is refused before the binding matters; the #650 retry-tainted-silence
reclassification runs before admission and is not an output decision.

The exception is the operator's ruling on #1075, for the turns that do not stream. A closing
`<silent/>` in its own message after the turn's narration used to reach the operator as
`Done.\n\n<silent/>`, because silence was judged only on the joined text. Admission now
also reads the turn report `_process` fills: the winning attempt's text-bearing messages
(`reply_messages`, from the same fold that joins them), the number of attempts that ran and
the consumed retries. It acts only on a `NoStream` scope, only when those messages join to
exactly the text being admitted, and only when the LAST of them strips to silence and
contains a sentinel (`closing_silence_prefix`); the closing run it drops is then every
trailing message that strips to silence. Rule 1 then needs no error (only an error-free
reply is admitted here), one attempt, no retries and `TurnScope.closing_silence_earned`: at least one `OperatorSend`, every one delivered,
and every `SendAttempt` resolved `ok`. The reply is suppressed with `chosen_silence` left
False, so the discharge below still reads only a reply of nothing but sentinels. Otherwise,
rule 2 admits the earlier messages verbatim, and the disclosure line goes on after the
strip. A single message carrying prose and a sentinel, prose in the last message, a trailing
whitespace message (even after a sentinel), a streaming turn and a reply with no per-message fact are judged as
before. Rule 1 drops all the earlier text, and the operator accepted two residuals: no-send
narration still arrives, untagged, and a real message written as plain text after a
confirmed send is dropped. The verdict is taken at
admission: a synchronous delegate that timed out and sends later belongs to its own
completion notice.

Two facts ride out of admission for the durable-announcement discharge (#1079,
INV-JOB-010). A suppressed final reply says whether the silence was *chosen* —
`Admitted.chosen_silence` is set in the same silence arm, on the same unannotated text, when
at least one sentinel was there, so an empty answer is suppressed but not chosen. And every
admission of model text the operator is meant to see outside the final reply — `DISCRETE`,
`CAPTION`, `KEYBOARD` — opens an `OperatorSend` record on the admitting scope
(`TurnScope.operator_sends`), undelivered until the sender calls `mark_delivered()` on its own
positive evidence; a caption-less `send_media` opens one with `open_send`, and
`TurnScope.for_child` hands a synchronous delegate the launching scope's list itself (an
async one, which outlives the launching turn, gets its own). Because the channel
refuses model text that was not admitted, no model text reaches the transport without leaving
a record. `handle_message` reads `TurnScope.operator_sends_delivered`, never the text. A send
refused before admission leaves no `OperatorSend`, so every call of `send_message`,
`send_media` or `ask_user` also opens a `SendAttempt` (`TurnScope.send_attempts`) through
one wrapper outside the registered handler: `open` at entry, then `failed` on an error
result or a raise and `ok` otherwise. It is shared exactly as `operator_sends` is and read
only by the #1075 rule, so the discharge's meaning is unchanged on every turn.

## Failure behavior

**The turn report is absent.** A final reply admitted without the report `_process` fills is
judged by the whole-text sentinel rule alone; the #1075 rule never acts on it.

## Extension points

**A new property a scope decides** is an `Obligation` registered at mint from the message's
own facts, as `NoStream` and `DestinationOperatorOnly` are, and read through one `TurnScope`
predicate rather than re-derived inline where it is acted on; the grep test in
`tests/test_output_boundary_relocation.py` refuses the old inline forms coming back.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/output_boundary.py::strips_to_silence`
- `casa/rootfs/opt/casa/output_boundary.py::closing_silence_prefix`
- `casa/rootfs/opt/casa/output_boundary.py::may_still_be_silence`
- `casa/rootfs/opt/casa/output_boundary.py::NoStream`
- `casa/rootfs/opt/casa/output_boundary.py::DestinationOperatorOnly`
- `casa/rootfs/opt/casa/output_boundary.py::OperatorSend`
- `casa/rootfs/opt/casa/output_boundary.py::SendAttempt`
- `casa/rootfs/opt/casa/output_boundary.py::TurnScope.closing_silence_earned`

**Tests**
- `tests/test_output_boundary_relocation.py`
- `tests/test_buffered_closing_silence.py`
- `tests/test_buffered_closing_silence_pins.py`

**Related**
- [`architecture/output-boundary.md`](../architecture/output-boundary.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/webhook-delivery.md`](../architecture/webhook-delivery.md)
- [`architecture/scheduled-prompt-endings.md`](../architecture/scheduled-prompt-endings.md)
- [`architecture/delegation-announcements.md`](../architecture/delegation-announcements.md)
<!-- END SOURCEMAP -->
