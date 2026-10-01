---
last_reviewed: 2026-10-01
---

# Webhook delivery: where a plugin webhook fire's reply goes

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What happens to the reply of a turn started by a plugin webhook trigger: whether it reaches
the operator, which tools such a turn is offered, and whether it may end in silence. What a
plugin's trigger must satisfy before it routes at all — declaration, consent, secrets — is
[`plugin-triggers.md`](plugin-triggers.md). The admission rules that judge any final reply,
silence included, are [`output-boundary.md`](output-boundary.md). The line Casa adds when a
turn stops at its turn limit is [`turn-limits.md`](turn-limits.md).

## Mental model

**A webhook fire's reply goes nowhere unless its trigger says otherwise.** No channel is
registered under `webhook`, so an untrusted fire's final text is simply dropped. A plugin
opts a trigger in with `deliver`, an enum in its manifest entry: `none` (the default),
`operator`, or `operator_always`. Both operator values deliver the reply, a classified
error and the limit-stop line to the operator's Telegram. They differ only on silence.

**`operator` lets the model decide; `operator_always` does not.** Under `operator`, Casa's
line on the turn invites `<silent/>`, and a silent reply is suppressed like any other: the
model's judgement decides whether the operator hears about the fire. Under
`operator_always` the operator has decided instead: every accepted fire reaches them. The
line drops the invitation, and a reply that is silent anyway is replaced by one line of
Casa's own, naming the webhook.

## Contracts & invariants

**INV-TRIG-018**: A plugin webhook trigger's final reply reaches the operator only when its manifest entry declares `deliver: operator` or `deliver: operator_always` — an enum whose third value, `none`, is the default — and the route record carries it to ingress, which stamps it on the turn as a reserved marker no request can set. Such a turn delivers its reply, including a classified error and the line Casa adds when it stops at its turn limit (INV-TURN-014), to the operator's Telegram, and is not offered `send_message`.

A webhook turn's reply used to go nowhere: no channel is registered under `webhook`, and
nothing said so. The opt-in is per trigger and travels in the one route snapshot ingress
reads (#620's seam), never re-read from the registry later. A resident webhook route reads
`none`. One predicate, `TurnScope.delivers_to_operator`, decides everything downstream. It
picks the reply's channel and context (INV-OUT-006 in
[`output-boundary.md`](output-boundary.md)). It builds the restricted runtime's tool set,
whose allowlist and tool listing both lose `send_message`. And it makes the `send_message`
handler refuse, as defence in depth. A prompt line could not stop a tool send and an
ordinary final reply from both arriving; removing the tool does. The turn's content also
ends with one constant line saying where the reply goes. It is guidance only, and it is
Casa's text (INV-TRIG-013 in [`triggers.md`](triggers.md)).

What it does not cover: a failed Telegram send is logged, not retried (the request was
already answered), and with no Telegram channel registered the reply is dropped with a
warning.

**INV-TRIG-021**: A `deliver: operator_always` fire never ends in a chosen silence: Casa's line on the turn never invites `<silent/>`, and a final reply that admission leaves empty — sentinels, whitespace, or no text at all — is followed by one fixed line of Casa's own, naming the webhook, sent to the operator's Telegram. A classified error — retry-tainted silence and a silence that came with an SDK error result among them — and the limit-stop line each count as that message and are not followed by the fallback, so a fire sends exactly one message unless the model writes a reply and the turn also stops at its limit.

`deliver: operator` cannot promise this, for two reasons that both sit in Casa rather than
the plugin. The line Casa appends invites silence, and a trigger carries no prompt to
counter it (INV-TRIG-013). And admission suppresses a reply that strips to silence
(INV-OUT-006), so the model's judgement decides whether a fire is heard. The new value
changes the line and adds the fallback; it does not touch admission. A silent reply is still
judged silent, and the fallback is a separate send after it, with the same operator
addressing the limit-stop line uses. So nothing the model writes is rewritten, and the
fallback is Casa's text (`casa_text`, beside INV-OUT-001), not the model's. Its wording says there
was nothing to add, which is why a failed turn must never reach it. Webhook fires carry a
trusted origin, so a silence after consumed retries is already reclassified as the error it
is (INV-TURN-008 in [`turn-loop.md`](turn-loop.md)). An error result the SDK client returns
rather than raises is not, on any other turn; on this one, a silence that came with one is
reported as the SDK error instead of the fallback.

What it does not cover: duplicates across fires. A provider that posts the same signed event
twice gets two turns and two messages; Casa does not deduplicate redelivered webhooks. And
the limit-stop exception named above: a model that writes a reply and is then cut at its
limit sends that reply and the limit line, as on any delivered turn (INV-TURN-014).

## Failure behavior

**No Telegram channel is registered.** The reply, the error line, the limit line and the
fallback are each dropped with a warning; nothing is retried.

**The fallback's send fails.** It is logged, as the limit line's is, and the turn has
already ended; the webhook request was answered before the turn ran.

## Extension points

**Delivering a fire to the operator** is the entry's `deliver: operator`, or
`deliver: operator_always` when every fire must be heard. A Casa that predates a value
rejects it as an invalid `deliver`, and rejects the whole set with it, so a plugin that
declares one ships only after a Casa that knows it. The consent prompt names the value, and
the value is not in the consent identity: the checksum-validated artifact id binds it
(INV-TRIG-004 in [`plugin-triggers.md`](plugin-triggers.md)).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/output_boundary.py::TurnScope.delivers_to_operator`
- `casa/rootfs/opt/casa/output_boundary.py::TurnScope.silence_forbidden`
- `casa/rootfs/opt/casa/agent.py::build_restricted_webhook_options`

**Tests**
- `tests/test_webhook_deliver_operator.py`

**Related**
- [`architecture/plugin-triggers.md`](../architecture/plugin-triggers.md)
- [`architecture/output-boundary.md`](../architecture/output-boundary.md)
- [`architecture/turn-limits.md`](../architecture/turn-limits.md)
- [`architecture/triggers.md`](../architecture/triggers.md)
<!-- END SOURCEMAP -->
