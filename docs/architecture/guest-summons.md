---
last_reviewed: 2026-11-12
---

# A specialist summoned in a group

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What happens when a specialist's own bot is summoned by @mention, or by a reply to its
message, in a chat it is not a member of — Telegram's guest mode: which summonses are
answered, how the operator's summons runs as a turn of the agent's own conversation, the one
inline message the bot owns in that chat, the `guest_access` tool and the record of the
groups the operator allowed. The bot itself and its lifecycle are
[`own-voice-lifecycle.md`](own-voice-lifecycle.md); the agent's conversation and own chat
are [`own-voice.md`](own-voice.md); the per-bot transport is
[`telegram-bots.md`](telegram-bots.md).

## Mental model

**What Telegram gives a bot.** A bot with guest mode turned on can be summoned in a chat it
is not a member of: it receives a `guest_message` update (with a `guest_query_id`), and may
answer that query exactly once, with one inline message it can edit afterwards
(`answerGuestQuery`). Guest mode is off by default and is turned on per bot by the operator
in BotFather's Mini App — the operator's own summons needs it too. Casa never reads
`getMe.supports_guest_queries` to decide anything: it lags the BotFather switch by tens of
minutes.

**Only an agent's own bot hears a summons.** An agent's bot asks Telegram for
`guest_message` beside `message` and `callback_query` (`ALLOWED_UPDATES_AGENT`); Casa's bot
does not, and registers no handler for it. On an agent's bot `_IntakeQueue.put` queues a
guest update straight for the guest handler: a summons is not a message of the agent's chat,
so it never passes the own-chat lifecycle's intake nor enters its held journal. The handler
(`MessageHandler(filters.UpdateType.GUEST_MESSAGE, …)`, `TelegramChannel._on_guest_message`)
checks the bot's phase itself.

**Who is answered** (`TelegramChannel._guest_decision`). A bot that is not bound to its
agent (`own_voice.is_bound`, a plain read of the bot's phase that creates and journals
nothing) drops every summons — a dormant, recovering, retiring or released bot included, so
the bot of an uninstalled specialist answers no one. The operator's summons is answered, in any chat. Anyone else's
is dropped, unless the chat is a group the operator allowed for the specialist installed in
that role now (`guest_groups.store().allowed`, matched on the component id
`guest_groups.component_of` reads): that summons is logged as not answered — a summons from
anyone but the operator is never answered. Nothing dropped or unanswered makes a Telegram
call. The operator's summons is rate-limited under the same key as his direct messages to
that bot (his user id), so summoning across groups shares one allowance; a refused one is not
answered in the group.

**The operator's summons is a turn of the agent's own conversation**
(`TelegramChannel._dispatch_summoned_turn`). The placeholder goes out first — the inline
message "**Name** · thinking…" — then ONE bus message goes to the agent, built as the
operator's direct message is: his chat as `chat_id`, the summoned bot as the route, his
identity, `_operator_turn`, his words as `_operator_words` (an empty text for a bare
mention), and `_chat_opening` when this turn opens the chat (`_opening_marker`, shared with
the direct-message dispatcher), so a summons can open the chat. Two reserved markers make it
a summoned turn: `_guest_chat` (the chat's id, title and type) and `_guest_query` (the key the
inline message is bound under, the turn's cid). When the summons replied to the bot's own
earlier message there, `_guest_reply_note` quotes that answer, clipped as a reply's quote is.
The turn runs on the agent's own session, memory and notes; its prompt carries
`agent.GUEST_CALL_NOTE` — the group's name, that the text answer is posted there and that a
card, a file or anything else goes to the operator's chat with the agent — and then the reply
note. The group is never a chat id, a route or a delivery: no typing lease is taken, nothing
streams (`create_on_token` is `None` for a bound turn), and a turn the agent's queue cannot
take fails the placeholder there and then, with no hand-back.

**Only the final text reaches the group.** `Agent.handle_message` hands a bound turn's final
text to the channel's `guest_finish` (route-bound through the transport) inside the same
delivery guard as any reply. A classified error, or text that is not admitted, edits the
message with Casa's failure line instead; the error's own wording never reaches the group. A
pending plugin-health notice is not prepended: once the text has landed it goes to the
operator's chat as its own send. A turn that ends empty or silent takes the `turn_finished`
path: the group gets "Not something I can help with here.", or, when the turn asked the
operator a question that outlives it (`TurnScope.detached_ask`, set by `ask_user` once the
question is live), "I've asked you something in our chat — I'll answer there." Everything
else the turn sends — `send_message`, `send_media`, a card, a question, a protected call's
challenge, Casa's own lines — goes to the operator's chat with the agent as on any turn.
When a keyboard reaches that chat during the turn (`post_dm_keyboard`), the group's message
shows "I've asked you something in our chat." until the turn ends.

**The inline message** (`channels/guest_reply.py`, `GuestReply`). One per summoned turn,
held in the endpoint by its key until its terminal edit. A ticker rotates a few thinking words
every `GUEST_TICK_S` (3 s), stops on the first edit error, and ends by itself after twice the
terminal budget (10 minutes). Every edit is serialized on one lock, and only the first
terminal call — finish, decline, fail, asked elsewhere — edits; a later one returns that
outcome. The terminal edit honours Telegram's full `RetryAfter`, backs off on network errors
from one second, doubling, and gives up after `GUEST_TERMINAL_BUDGET_S` (5 minutes) as
not delivered; "not modified" counts as delivered, "message to edit not found" as not. Text is
cut before rendering to fit `GUEST_TEXT_LIMIT` (4096 UTF-16 units) with an ellipsis — at the
last whitespace when that keeps at least three quarters of it, otherwise at the limit — and
the rendered form falls back to plain text when it still does not fit or its entities are
refused. `meta` holds the facts the log line reads (role, caller, group).

**Derived work was not summoned.** `_guest_chat` and `_guest_query` are in
`provenance.RESERVED_CONTEXT_KEYS` (no ingress can set them, nor `_guest_reply_note`) and in
`agent.COPIED_CONTEXT_MARKERS`, so they reach the turn's origin. `provenance.drop_guest_markers`
removes them wherever a run is derived from that origin — a delegated run
(`tools._run_delegated_agent`) and a launch brief (`tools._launch_note`) — and the narration
turn of a delegation's completion carries neither.

**Allowing a group** (`tools.guest_access`, action `allow`, `revoke` or `status`). A Casa
tool the specialist builds are granted in code (`tools._guest_grants`: a specialist's
own-chat, delegated and pinned builds, never an engagement's or the resident's), so it is
present in the agent's own chat as well and refuses there itself. The group comes only from
the origin's `_guest_chat`; the arguments carry the action alone. Outside a summoned turn of
the operator ("I can only do that from inside the group concerned."), and in a run another
agent executes, it refuses; `allow` records only a `group` or `supergroup` ("I answer others
in groups only.") and only for an installed component. It answers the group's title and its
state: `allowed`, `previous_specialist` or `none`. The operator asks in the group, in his own
words ("you can answer everyone here"), and the agent calls it.

**The allowance record** (`guest_groups.py`). `/data/agent-voices-guests.json` holds
`{role: {chat_id: {title, type, component_id, allowed_at}}}` (`GuestStore.allow`, `revoke`,
`allowed`, `status`, `entries`), saved whole and atomically, owner-only, after each change. An
entry is bound to the role and to the component installed when it was made: a replacement
specialist in the same role starts without it (`status` names it `previous_specialist`).

## Contracts & invariants

**INV-GUEST-001**: A summons of an agent's own bot from anyone but the operator, in a chat that is not a group allowed for the specialist installed in that role now, makes no Telegram call and queues no turn; a bot not bound to its agent drops every summons the same way.

A summons from someone else in an allowed group is logged and also not answered, and so is
an operator's summons the rate limit refuses or one that arrives with no operator identity.

**INV-GUEST-004**: `guest_access` acts only on the chat of its turn's server-stamped `_guest_chat`, ignoring any chat its arguments name, and only on the operator's summoned turn of the summoned specialist itself; in a direct-message turn, without the operator marker, or in a run another agent executes it refuses and records nothing, and `allow` records only a group or a supergroup.

**INV-GUEST-005**: `guest_access` is granted only to a specialist's own-chat, delegated and pinned builds — never to an engagement or job build nor to the resident — and Casa's own bot neither asks Telegram for guest updates nor registers a handler for them.

**INV-GUEST-006**: The operator's summons answers the guest query with its placeholder before its turn is queued — a placeholder Telegram refuses queues no turn — and every way the turn ends makes the reply's one terminal edit: its final text, Casa's failure line for a classified error, an unadmitted text or a turn the queue refuses, a decline for an empty or silent end, or the pointer to the operator's chat when it ended on a question asked there; only the first terminal call edits.

The waiting line a keyboard puts up is not terminal: the answer still follows it.

**INV-GUEST-007**: A guest update on an agent's own bot never passes the own-chat lifecycle's intake nor enters its held journal — `_IntakeQueue` queues it for the guest handler — while every other update the bot receives still passes the intake.

**INV-GUEST-009**: `_guest_chat`, `_guest_query` and `_guest_reply_note` are reserved context keys only the channel stamps; the first two are copied into the summoned turn's origin and dropped from every run derived from it — a delegated run, a launch brief — and a delegation's completion narration carries neither.

**INV-GUEST-010**: A summoned turn is a turn of the agent's own conversation in the operator's own chat, routed through the summoned bot, and only its final text reaches the group: a `send_message`, a card, a question, a file or a health notice goes to the operator's chat with the agent, and the group is never a turn's chat id, route or delivery.

## Failure behavior

**The placeholder is refused** (the query expired, guest mode off). Logged; no turn is
queued, nothing is said anywhere.

**The terminal edit cannot be made in time.** After the delivery budget the reply is not
delivered and logged once; the ticker has already stopped. A turn's other sends are
unaffected.

**The allowance file is unreadable or not of its shape.** It is moved aside to `.corrupt`
and the store starts empty: every group is unallowed until the operator allows it again. A
failed save returns `save_failed` and the agent says it did not go through.

**Casa restarts mid-turn.** The binding is in memory; the inline message keeps its last
thinking word or waiting line.

## Extension points

**A new kind of send from a summoned turn** goes to the operator's chat through the turn's
ordinary route; only `guest_finish` and `turn_finished` touch the group's message. **A new
run or turn derived from an origin** passes it through `drop_guest_markers` unless it is the
summoned turn itself.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/channels/guest_reply.py`
- `casa/rootfs/opt/casa/guest_groups.py`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._guest_decision`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._on_guest_message`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._dispatch_summoned_turn`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._guest_reply_note_for`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.guest_finish`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._guest_waiting`
- `casa/rootfs/opt/casa/channels/telegram_transport.py::TelegramTransport.guest_finish`
- `casa/rootfs/opt/casa/own_voice.py::is_bound`
- `casa/rootfs/opt/casa/provenance.py::drop_guest_markers`
- `casa/rootfs/opt/casa/tools.py::guest_access`
- `casa/rootfs/opt/casa/tools.py::_guest_grants`

**Tests**
- `tests/test_guest_intake.py`
- `tests/test_guest_summons.py`
- `tests/test_guest_reply.py`
- `tests/test_guest_agent_delivery.py`
- `tests/test_guest_access_tool.py`
- `tests/test_guest_groups.py`

**Related**
- [`architecture/own-voice.md`](../architecture/own-voice.md)
- [`architecture/own-voice-lifecycle.md`](../architecture/own-voice-lifecycle.md)
- [`architecture/telegram-bots.md`](../architecture/telegram-bots.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
- [`architecture/plugin-health.md`](../architecture/plugin-health.md)
<!-- END SOURCEMAP -->
