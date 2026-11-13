---
last_reviewed: 2026-11-13
---

# A specialist summoned in a group

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What happens when a specialist's own bot is summoned by @mention, or by a reply to its
message, in a chat it is not a member of — Telegram's guest mode: which summonses are
answered, how the operator's summons runs as a turn of the agent's own conversation, how a
friend's summons in a group the operator allowed runs as a bounded guest run, the one
inline message the bot owns in that chat, the `guest_access` tool and the record of the
groups the operator allowed. The bot itself and its lifecycle are
[`own-voice-lifecycle.md`](own-voice-lifecycle.md); the agent's conversation and own chat
are [`own-voice.md`](own-voice.md); the per-bot transport is
[`telegram-bots.md`](telegram-bots.md); the `guest` member a plugin tool declares is read
with the rest of its result contract ([`plugin-result-contract.md`](plugin-result-contract.md)).

## Mental model

**What Telegram gives a bot.** A bot with guest mode turned on can be summoned in a chat it
is not a member of: it receives a `guest_message` update (with a `guest_query_id`), and may
answer that query exactly once, with one inline message it can edit afterwards
(`answerGuestQuery`). Guest mode is off by default and is turned on per bot by the operator
in BotFather's Mini App — the operator's own summons needs it too. Casa never reads
`getMe.supports_guest_queries` to decide anything: it lags the BotFather switch by tens of
minutes, and only the resident's list shows it, as a hint.

**Only an agent's own bot hears a summons.** An agent's bot asks Telegram for
`guest_message` beside `message`, `callback_query` and the draft Stop update
(`ALLOWED_UPDATES_AGENT`); Casa's bot does not, and registers no handler for it. On an agent's bot `_IntakeQueue.put` queues a
guest update straight for the guest handler: a summons is not a message of the agent's chat,
so it never passes the own-chat lifecycle's intake nor enters its held journal. The handler
(`MessageHandler(filters.UpdateType.GUEST_MESSAGE, …)`, `TelegramChannel._on_guest_message`)
checks the bot's phase itself.

**Who is answered** (`TelegramChannel._guest_decision`). A bot that is not bound to its
agent (`own_voice.is_bound`, a plain read of the bot's phase that creates and journals
nothing) drops every summons — a dormant, recovering, retiring or released bot included, so
the bot of an uninstalled specialist answers no one. The operator's summons is answered, in
any chat, as a turn of the agent's own conversation. Anyone else's is answered only in a
group the operator allowed for the specialist installed in that role now
(`guest_groups.store().allowed`, matched on the component id `guest_groups.component_of`
reads), as a guest run; everywhere else it is dropped. Nothing dropped makes a Telegram call.
The operator's summons is rate-limited under the same key as his direct messages to that bot
(his user id), so summoning across groups shares one allowance; a friend's under the group's
id, so one group shares one allowance whoever asks. A refused summons is not answered in the
group (`guest_dropped` at DEBUG for a friend's).

**The operator's summons is a turn of the agent's own conversation**
(`TelegramChannel._dispatch_summoned_turn`). The placeholder goes out first — the inline
message "**Name** · thinking…" — then ONE bus message goes to the agent, built as the
operator's direct message is: his chat as `chat_id`, the summoned bot as the route, his
identity, `_operator_turn`, his words as `_operator_words` (an empty text for a bare
mention), and `_chat_opening` when this turn opens the chat (`_opening_marker`, shared with
the direct-message dispatcher), so a summons can open the chat. Two reserved markers make it
a summoned turn: `_guest_chat` (the chat's id, title and type) and `_guest_query` (the key the
inline message is bound under, the turn's cid). When the summons replied to the bot's own
earlier message there, `_guest_reply_note` quotes that answer, clipped as a reply's quote is,
and names who asked for it when Telegram says. The turn runs on the agent's own session,
memory and notes; its prompt carries `agent.GUEST_CALL_NOTE` — the group's name, that the
text answer is posted there and that a card, a file or anything else goes to the operator's
chat with the agent — and then the reply note. The group is never a chat id, a route or a
delivery: no typing lease is taken, nothing streams (`create_on_token` is `None` for a bound
turn), and a turn the agent's queue cannot take fails the placeholder there and then, with
no hand-back.

**A friend's summons is a guest run** (`TelegramChannel._dispatch_guest_turn`). The same
placeholder and markers, and the same helpers (`_open_summons_reply`, `_stamp_summons`,
`_queue_summons`), but the friend's turn, not the operator's: the bus message goes out on the
guest channel (`guest_run.GUEST_CHANNEL`, `"telegram_guest"`) with the `telegram_guest`
ingress route — a Telegram person named by their own sender id, at public clearance
whoever sent it ([`http-surface.md`](http-surface.md)) — the group as its `chat_id`, the
friend's first name as `user_name` ("Someone" without one), and none of the operator's
markers. `Agent._process` takes an early branch on that channel, before the turn's identity,
session gate, memory or pool are reached, into `Agent._guest_turn`: one
`ManagedSdkClient`, opened for this run and closed after it, never the client pool nor the
session registry. Its system prompt is the guest projection of the specialist's compiled
bundle — the core doctrine and the doctrine's `Guest projection` section only, with the short
persona body ([`personality.md`](personality.md)); the plain system prompt for a specialist
without a bundle — followed by Casa's guest frame (`guest_run.guest_frame`): who called and in
which group, that the caller is not the person the agent works for, a public reply within its
subject, one short message, nothing about Casa, its tools, memory or other agents. The
caller's name and the group's title are chosen by people in the group, so each is quoted on
one line with angle brackets swapped for guillemets and cut (64 and 128 characters): nothing
in a name can close the frame early. The query is the time envelope, then the reply note
when the summons replied to the bot's earlier answer, then the friend's words. No memory view
is served, no correction note read, nothing captured, no chat notes drained, none of a
specialist conversation's session extras applied. Transient SDK faults retry as on any turn;
the whole run, retries included, is bounded by `GUEST_RUN_TIMEOUT_S` (120 s), past which it
fails as a timeout with its client closed.

**A guest run's options are built apart** (`tools._build_guest_options`). Nothing of the
specialist's own grant set is read — not its allowed tools, hooks document or server list,
nor the hand-off, inbox or job grants. The allow-list is the tools of the specialist's
resolved plugins whose author marked them `guest` and that are not protected
(`guest_run.guest_tool_names`), narrowed by its access profile and never widened by it: the
intersection is computed last. Every other tool the plugins name, the setup tool included, is
also in `disallowed_tools`, out of the model's view. No Casa MCP server (`mcp_servers={}`), no
built-in tool (`tools=[]`), no skill, no setting source — which keeps an ambient `.mcp.json`
out; the plugin's own server loads through `plugins=`, which a strict MCP config would drop,
so `strict_mcp_config` stays off. The CLI's own hooks are disabled, `permission_mode` is
`dontAsk` with a fail-closed `can_use_tool`, `max_turns` is the lesser of the specialist's
own and `GUEST_MAX_TURNS` (12), nothing is resumed, and `--no-session-persistence` keeps the
run's transcript off disk. Two admission layers hold the list independently, because the CLI
runs matching PreToolUse hooks concurrently and one deny does not stop its siblings: the guest
pin (`hooks.guest_pin_matcher`, [`hook-resolution.md`](hook-resolution.md)) is the first
matcher and denies every tool outside the list, plugin tools included; the result broker's
admission hook, given the same list as `guest_tools`, denies a plugin tool outside it before
anything else ([`plugin-result-contract.md`](plugin-result-contract.md)).

**Only the final text reaches the group.** `Agent.handle_message` hands a bound turn's final
text to the channel's `guest_finish` (route-bound through the transport) inside the same
delivery guard as any reply. A guest-channel turn always ends there, its reply bound or not:
one whose reply is gone is refused there, never sent to the group's chat id. A classified
error, or text that is not admitted, edits the message with Casa's failure line instead; the
error's own wording never reaches the group. A pending plugin-health notice is not
prepended: on the operator's turn it goes to his chat as its own send once the text has
landed; on a guest run it is neither shown nor consulted. A guest run's turn-limit line goes
to the log only (`Agent._limit_line_route`). A turn that ends empty or silent takes the
`turn_finished` path: the group gets "Not something I can help with here.", or, when the
operator's turn asked him a question that outlives it (`TurnScope.detached_ask`, set by
`ask_user` once the question is live), "I've asked you something in our chat — I'll answer
there." Everything else the operator's turn sends — `send_message`, `send_media`, a card, a
question, a protected call's challenge, Casa's own lines — goes to his chat with the agent
as on any turn. When a keyboard reaches that chat during the turn (`post_dm_keyboard`), the
group's message shows "I've asked you something in our chat." until the turn ends.

**A few friends at a time.** Each bot answers at most `GUEST_RUNS_IN_FLIGHT` (3) friends'
summonses at once, counted as its bound replies of the guest kind (`_guest_runs_live`; the
operator's own do not count). The reply is bound before the query is answered, with no await
between the count and the binding, so summonses handled side by side cannot all pass it. A
summons past the cap runs nothing: its query is answered at once with `BUSY_LINE`, "Busy right
now — ask me again in a moment." (`GuestReply.open_final`, a reply terminal from the start).

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
refused. `meta` holds the facts the log line reads (kind, role, caller, group).

**One log line per summons** (`GUEST_SUMMONS_LOG`): role, caller id, chat id and title,
outcome and milliseconds. The outcome is `<kind>_answered`, `_declined`, `_asked_elsewhere`
or `_failed`, the kind being `operator` or `guest`, written by the step that learns it;
`guest_busy` for the busy line (`guest_failed` when it could not be said); `guest_dropped`
and `dropped` at DEBUG.

**Derived work was not summoned.** `_guest_chat` and `_guest_query` are in
`provenance.RESERVED_CONTEXT_KEYS` (no ingress can set them, nor `_guest_reply_note`) and in
`agent.COPIED_CONTEXT_MARKERS`, so they reach the turn's origin. `provenance.drop_guest_markers`
removes them wherever a run is derived from that origin — a delegated run
(`tools._run_delegated_agent`) and a launch brief (`tools._launch_note`) — and the narration
turn of a delegation's completion carries neither.

**Allowing a group** (`tools.guest_access`, action `allow`, `revoke`, `status` or `list`). A
Casa tool the specialist builds are granted in code (`tools._guest_grants`: a specialist's
own-chat, delegated and pinned builds, never an engagement's), so it is present in the
agent's own chat as well and refuses there itself. In a summoned turn the group comes only
from the origin's `_guest_chat`; the arguments carry the action alone. Outside a summoned turn
of the operator ("I can only do that from inside the group concerned."), and in a run another
agent executes, it refuses; `allow` records only a `group` or `supergroup` ("I answer others
in groups only.") and only for an installed component. It answers the group's title and its
state: `allowed`, `previous_specialist` or `none`. The operator asks in the group, in his own
words ("you can answer everyone here"), and the agent calls it.

**The resident's face of the same tool** (`_guest_access_from_resident`). The resident has
`guest_access` from its shipped role files, not from `_guest_grants`. On its own operator turn
— no `_guest_chat`, executed by the resident itself, `_operator_turn` set — it does two
things: `list`, every group any specialist may answer in (`_guest_list`: role, display name,
chat id, title, type, `allowed_at`, state, and `telegram_reports_guest_mode` — `True`, `False`
or `"unknown"`, asked of each role's bot side by side, bounded at 5 s, a hint and nothing
more), and `revoke` with the `role` and integer `chat_id` the list shows. `allow` and `status`
are refused there, as in any chat that is not the group. `list` also works in the group face.

**The allowance record** (`guest_groups.py`). `/data/agent-voices-guests.json` holds
`{role: {chat_id: {title, type, component_id, allowed_at}}}` (`GuestStore.allow`, `revoke`,
`allowed`, `status`, `entries`), saved whole and atomically, owner-only, after each change. An
entry is bound to the role and to the component installed when it was made: a replacement
specialist in the same role starts without it (`status` names it `previous_specialist`).

## Contracts & invariants

**INV-GUEST-001**: A summons of an agent's own bot from anyone but the operator makes no Telegram call and queues no turn unless the chat is a group allowed for the specialist installed in that role now — where it is answered by a guest run, its group's rate limit permitting; a bot not bound to its agent drops every summons the same way.

An operator's summons the rate limit refuses, or one that arrives with no operator identity,
is not answered either; a friend's summons past the cap is answered with the busy line and
runs nothing.

**INV-GUEST-002**: A guest run's options offer only the guest-flagged, non-protected tools of the specialist's own plugins, narrowed by its access profile and never widened by it: no Casa MCP server and so no casa-framework tool — no hand-off, delegation, inbox or job grant — no built-in tool, skill or setting source, at most `GUEST_MAX_TURNS` turns, no session resumed and none persisted; the guest pin, the first PreToolUse matcher, and the result broker's admission hook each deny every plugin tool outside that list, and the pin every other tool.

What it does not cover: what an offered tool itself does with its arguments; that is the
plugin author's contract, declared with the `guest` member (INV-GUEST-008).

**INV-GUEST-003**: Nothing a guest run hears or says reaches memory or a session: `writes_to_bank("telegram_guest")` is false, and the run serves no memory view, reads no correction note, captures nothing, drains no chat notes, applies no session extras and touches neither the client pool nor the session registry.

**INV-GUEST-004**: In a summoned turn `guest_access` acts only on the chat of the turn's server-stamped `_guest_chat`, ignoring any chat its arguments name, and only on the operator's summoned turn of the summoned specialist itself; on the resident's own operator turn it only lists every allowed group or revokes the one its role and chat id name; in a specialist's direct-message turn, without the operator marker, or in a run another agent executes it refuses and records nothing, and `allow` records only a group or a supergroup.

**INV-GUEST-005**: `guest_access` is granted in code only to a specialist's own-chat, delegated and pinned builds — never to an engagement or job build — and the resident has it only from its shipped role files, for `list` and `revoke`; Casa's own bot neither asks Telegram for guest updates nor registers a handler for them.

**INV-GUEST-006**: The operator's summons answers the guest query with its placeholder before its turn is queued — a placeholder Telegram refuses queues no turn — and every way the turn ends makes the reply's one terminal edit: its final text, Casa's failure line for a classified error, an unadmitted text or a turn the queue refuses, a decline for an empty or silent end, or the pointer to the operator's chat when it ended on a question asked there; only the first terminal call edits.

The waiting line a keyboard puts up is not terminal: the answer still follows it.

**INV-GUEST-007**: A guest update on an agent's own bot never passes the own-chat lifecycle's intake nor enters its held journal — `_IntakeQueue` queues it for the guest handler — while every other update the bot receives still passes the intake.

**INV-GUEST-008**: A `guest` member of a `casa.resultContract` tool entry is accepted only as the literal true, and never on a `capability` tool, the plugin's `casa.setupTool`, a tool that declares `callsCasa` or a protected tool; any other declaration fails the contract as `result_contract_invalid`, and an entry without the member carries no guest flag.

**INV-GUEST-009**: `_guest_chat`, `_guest_query` and `_guest_reply_note` are reserved context keys only the channel stamps; the first two are copied into the summoned turn's origin and dropped from every run derived from it — a delegated run, a launch brief — and a delegation's completion narration carries neither.

**INV-GUEST-010**: The operator's summoned turn is a turn of the agent's own conversation in the operator's own chat, routed through the summoned bot, and only its final text reaches the group: a `send_message`, a card, a question, a file or a health notice goes to the operator's chat with the agent, and the group is never a turn's chat id, route or delivery.

## Failure behavior

**The placeholder is refused** (the query expired, guest mode off). Logged; no turn is
queued, nothing is said anywhere.

**The terminal edit cannot be made in time.** After the delivery budget the reply is not
delivered and logged once; the ticker has already stopped. A turn's other sends are
unaffected.

**A guest run outlasts its bound.** It is cut at `GUEST_RUN_TIMEOUT_S`, its client
interrupted, drained and closed; the group gets Casa's failure line. The plugin resolution and
option build before the run, and the close after a cut, fall outside the bound.

**The allowance file is unreadable or not of its shape.** It is moved aside to `.corrupt`
and the store starts empty: every group is unallowed until the operator allows it again. A
failed save returns `save_failed` and the agent says it did not go through.

**Casa restarts mid-turn.** The binding is in memory; the inline message keeps its last
thinking word or waiting line.

## Extension points

**A new kind of send from a summoned turn** goes to the operator's chat through the turn's
ordinary route; only `guest_finish` and `turn_finished` touch the group's message. **A new
run or turn derived from an origin** passes it through `drop_guest_markers` unless it is the
summoned turn itself. **A new step of the turn loop** that reads memory, a session or the pool
sits after the guest branch in `_process`, so a guest run never reaches it; one a guest run
must also take is added to `_guest_turn` deliberately. **A tool a friend may use** is a
plugin tool its author marks `"guest": true`; nothing of Casa's is offered to a guest run.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/channels/guest_reply.py`
- `casa/rootfs/opt/casa/guest_groups.py`
- `casa/rootfs/opt/casa/guest_run.py`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._guest_decision`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._on_guest_message`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._dispatch_summoned_turn`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._dispatch_guest_turn`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._guest_runs_live`
- `casa/rootfs/opt/casa/agent.py::Agent._guest_turn`
- `casa/rootfs/opt/casa/tools.py::_build_guest_options`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._guest_reply_note_for`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.guest_finish`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._guest_waiting`
- `casa/rootfs/opt/casa/channels/telegram_transport.py::TelegramTransport.guest_finish`
- `casa/rootfs/opt/casa/own_voice.py::is_bound`
- `casa/rootfs/opt/casa/provenance.py::drop_guest_markers`
- `casa/rootfs/opt/casa/tools.py::guest_access`
- `casa/rootfs/opt/casa/tools.py::_guest_grants`
- `casa/rootfs/opt/casa/tools.py::_guest_access_from_resident`
- `casa/rootfs/opt/casa/tools.py::_guest_list`

**Tests**
- `tests/test_guest_intake.py`
- `tests/test_guest_summons.py`
- `tests/test_guest_reply.py`
- `tests/test_guest_agent_delivery.py`
- `tests/test_guest_access_tool.py`
- `tests/test_guest_groups.py`
- `tests/test_guest_run.py`
- `tests/test_guest_options.py`
- `tests/test_guest_pin.py`
- `tests/test_guest_end_to_end.py`

**Related**
- [`architecture/plugin-result-contract.md`](../architecture/plugin-result-contract.md)
- [`architecture/personality.md`](../architecture/personality.md)
- [`architecture/hook-resolution.md`](../architecture/hook-resolution.md)
- [`architecture/own-voice.md`](../architecture/own-voice.md)
- [`architecture/own-voice-lifecycle.md`](../architecture/own-voice-lifecycle.md)
- [`architecture/telegram-bots.md`](../architecture/telegram-bots.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
- [`architecture/plugin-health.md`](../architecture/plugin-health.md)
<!-- END SOURCEMAP -->
