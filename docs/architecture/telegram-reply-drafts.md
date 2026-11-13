---
last_reviewed: 2026-11-13
---

# A reply written into a draft, and its Stop

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a reply in a private Telegram chat is shown while the model writes it — the turn's token
callback, the draft stream and its clock, the one draft call — how it ends before the reply
is sent, and what a press of the draft's Stop button does to the running turn. Which turns
get a token callback at all, and what the stream may carry, is
[`turn-loop.md`](turn-loop.md) (INV-TURN-009) and
[`output-scope-properties.md`](output-scope-properties.md) (INV-OUT-006); how the reply
itself is rendered and paged is [`telegram-rendering.md`](telegram-rendering.md); what the
pool does with an interrupted client is [`sdk-client-pool.md`](sdk-client-pool.md)
(INV-TURN-002).

## Mental model

**The draft shows the reply; the reply is still one ordinary message.** A Telegram turn asks
the Claude Agent SDK for partial messages, as a voice turn does (`agent.py::_PARTIAL_CHANNELS`),
so the token callback receives the accumulated reply text as the model writes it. A
sub-agent's in-flight text deltas are not streamed, and what the reply contains — the fold of
finished messages — is the same as on a turn that never streamed. The callback
(`TelegramChannel.create_on_token`) writes that text into a Telegram draft (the Bot API's
`sendMessageDraft`), rendered as the final reply is, with Telegram's own Stop button
(`can_stop`, `keep_on_stop`) and a fresh non-zero draft id per turn. The draft is there from
the start of the turn: `create_on_token` starts the stream, which shows an empty draft until
the first word, so Stop can be pressed while the model is still thinking or calling a tool.
An empty draft is sent without `keep_on_stop`, so a press removes it; one with text stays in
the chat until the reply arrives. The draft never shows an answer's reply marker: admission
takes it off every stream update and holds one still being written, so the draft stays empty
until the first word after it ([`output-boundary.md`](output-boundary.md)). When the model is done,
`Agent.handle_message` ends the stream and the reply goes out through the ordinary send:
`send_response` for a reply, `send` for Casa's own line. Nothing is sent early and edited
later.

**The model never waits on the network.** The callback releases the turn's typing lease at
its first call — until then "typing…" shows beside the empty draft — and otherwise only
stores the latest text in the turn's `DraftStream` (`channels/telegram_draft.py`). One task
per turn does the sending on its own clock: about every 0.6 s it sends the stored text if it
changed since the last send; with nothing new it re-sends the same text every 10 s so the
draft does not vanish, and stops re-sending once the text has not changed for 10 minutes,
counted from the start of the turn while the draft is still empty. A text that no longer fits one message stops growing:
the last text that fitted keeps being re-sent under the same rules. A flood wait is slept.
The draft decides nothing about the turn.

**Where there is no draft.** A summoned turn gets no callback, because its one inline message
takes only the final text ([`guest-summons.md`](guest-summons.md)); nor does a turn in a group
or supergroup — a chat with a negative id — since a draft exists in a private chat only. A
turn minted `NoStream` (a scheduled turn, an event wake, an own-chat greeting) never asks for
one. Those replies arrive whole.

**Stop ends the reply, nothing else.** A press arrives as the update
`stopped_message_generation`, which both bots ask for (`ALLOWED_UPDATES_CASA`,
`ALLOWED_UPDATES_AGENT`). The client library has no field for it, so a `TypeHandler` in a
handler group of its own (`TelegramChannel._on_stop_update`) reads it from the update's
unparsed fields. The draft id is matched as text, whichever form it arrives in, against the
drafts the channel is writing now. The matched stream is ended and
`sdk_client_pool.interrupt_turns(cid)` asks every pooled turn running under the stream's
correlation id to stop. Nothing is cancelled: the CLI ends the turn with the text written so
far, the pool keeps the client warm and the turn's session is published as for any turn; the
turn report carries `stopped`, and what was written is delivered as an ordinary message, with
no confirmation line. An approval card, a background job or a browsing request the turn
already started keeps running, each with its own way to end; a synchronous hand-off in
flight ends because the SDK cancels the parent's tool call and the hand-off's own cancel path
cancels the child.

## Contracts & invariants

**INV-TG-023**: A turn's draft stream is ended on every way out of the model's turn — a return, a raise and a cancellation — before its reply is delivered, and that reply is one ordinary send.

`handle_message` ends the stream in a `finally` around `_process`; a stream whose end raises
is logged and the reply still goes. The draft therefore never outlives the turn, and nothing
waits on a draft to deliver the answer.

What it does not cover: what the client shows of a draft once it is ended — after a turn that
sends nothing, an empty draft stays until Telegram removes it on its own; a reply too long
for one message is paged by the ordinary send, as any reply is.

**INV-TG-024**: A stop update acts only on a draft the receiving endpoint is writing now, in the chat the update names — it ends that stream and asks the turns under its correlation id to stop; every other update, and every other press, is left alone and nothing is sent.

The live drafts are keyed by draft id, each with its target chat; an entry lives from the
callback being made until its stream ends, so a press on a finished draft finds nothing. A
press that is acted on leaves one log line with the correlation id and the number of turns
interrupted, 0 when it reached none. A failure while stopping is logged and leaves the turn
running.

What it does not cover: a turn `interrupt_turns` cannot reach — the press ends the draft and
the reply arrives whole when the turn finishes. `interrupt_turns` reaches a turn on a client a pool holds: one in its map,
or one an invalidation or the pool's own close has taken out of the map and is waiting on. It
does not reach a turn that runs outside the pool, nor one whose client a reset of its
conversation (`SdkClientPool.close_key`) or an eviction took out of the map while the turn
ran.

**INV-TURN-016**: A turn whose winning attempt was stopped through `interrupt_turns` delivers what it wrote as its reply; when it wrote nothing it ends in silence — never Casa's line for a silence after consumed retries — and records no verdict on the health of the session it resumed.

The CLI closes an interrupted turn with an error result; `_process` reports the attempt as
`stopped` and the error result as none, so no reader takes the stop for a fault. A silence
the operator asked for says nothing about the session either way, which is why the
resume-health note is skipped rather than reset.

What it does not cover: a stop request that lands as the turn ends — the text may then be
complete, and is delivered as written.

## Failure behavior

**A draft call fails.** Logged once per stream as a warning and never retried: the text
counts as sent, and goes out again only as new text or at the next re-send. The turn is
untouched.

**The draft call is refused for unadmitted text, or the channel is not started.** No call
is made (INV-OUT-001 in [`output-boundary.md`](output-boundary.md)); the stream carries on.

**The final send loses its acknowledgement.** As for any reply: the send raises, and a
one-shot notice at its head is released for re-offer (INV-TG-006 in
[`telegram.md`](telegram.md)). Whenever the final send fails, the reply is not delivered: its
text was only in the draft, which Telegram removes on its own, and Casa does not send it
again — after a timeout Telegram may already have taken the message, and a second send would
show the reply twice.

**The interrupt cannot be delivered.** The pool withdraws its mark and the turn goes on; the
draft is already ended, so the reply arrives whole.

## Extension points

**Another channel that streams** joins `_PARTIAL_CHANNELS` and returns a callback from its
`create_on_token` whose `end` attribute stops what it shows; `handle_message` calls it before
delivery.

**Another way to stop a turn** calls `sdk_client_pool.interrupt_turns` with the turn's
correlation id; the turn then ends through its normal end, as a Stop press does.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/channels/telegram_draft.py`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.create_on_token`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._send_draft`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._on_stop_update`

**Tests**
- `tests/test_telegram_draft_stream.py`
- `tests/test_telegram_stop_update.py`
- `tests/test_turn_stop.py`
- `tests/test_sdk_pool_stop.py`
- `tests/test_agent_partial_streaming.py`

**Related**
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
- [`architecture/sdk-client-pool.md`](../architecture/sdk-client-pool.md)
- [`architecture/output-boundary.md`](../architecture/output-boundary.md)
- [`architecture/output-scope-properties.md`](../architecture/output-scope-properties.md)
- [`architecture/guest-summons.md`](../architecture/guest-summons.md)
<!-- END SOURCEMAP -->
