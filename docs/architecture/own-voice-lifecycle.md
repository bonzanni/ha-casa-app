---
last_reviewed: 2026-11-11
---

# An agent's own bot: its lifecycle

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The life of the Telegram bot an agent that declares its own chat talks from: the records and
phases that drive it, the offer and the bot's creation, binding and self-setup, the chat's
opening, the journal that holds updates until the agent can take them, dormancy, recovery and
release. What the chat is for once open — the capability rule, a task's delivery and
requester, the agent's conversation, own-chat routing — is [`own-voice.md`](own-voice.md); the
per-bot transport these bots run on is [`telegram-bots.md`](telegram-bots.md).

## Mental model

**The lifecycle is a record per agent and per bot, driven through phases.**
`own_voice_lifecycle.Lifecycle` keeps `agent-voices.json` under `/data` (one record per
agent: its offer card, its bot, when its chat opened; one per bot: its phase, role,
username, name, the open question about it, the digest of its last self-setup). A bot's
phases are `seen → unbound → binding → bound`, with `recovering`, `dormant`, `releasing`
and `released` beside them. Every transition persists the phase that names its effect
before the effect, every effect is idempotent or reconciled by lookup, and every phase has
a boot step (`BOOT_STEPS`) that completes or rolls it back. A per-bot lock covers intake,
every phase change and the journal's release; a registry lock covers what spans bots, taken
after the per-bot one.

**Offer and creation.** Casa offers an eligible agent its chat once — after a committed
install, after an upgrade or rollback that newly declares one, at a boot that finds an
eligible agent with no record — and on demand when the resident calls `offer_own_chat`.
The offer is an ordinary Casa card in the resident's chat: "Alex can get a separate chat
with you. Tap below, check the name, then Create." with one URL button opening Telegram's
pre-filled "Create Bot" form (`https://t.me/newbot/<resident bot>/<suggested username>?name=…`).
The suggestion is a string operation on the resident's own bot: its name and username
with the resident's name replaced by the agent's (`suggest`), a clash between two waiting
agents broken with `_<role>`. The operator confirms the form; Telegram then sends the
`managed_bot_created` service message to the resident's bot and a `managed_bot` update.
Only the first registers a bot, and only when it comes from the operator in his chat with
Casa's bot; the update alone registers nothing and is read only as a token change of a
known bot. Casa's bot must have bot management turned on in BotFather once; Casa never
gates on `getMe.can_manage_bots`, which lags that switch.

**Binding: match or ask, one bot per agent.** A new bot binds to the waiting agent whose
persisted suggestion it matches exactly (name and username); with no single match Casa
asks in the resident's chat ("A new bot @… was just created. Which agent is it for?"),
offering the waiting agents and "None — I'll delete it". An agent never holds two bots and
a bot is never rebound to another agent. The token is fetched with `getManagedBotToken`
through the resident's bot's request layer, never the library's Bot method, whose DEBUG
trace logs the return value; it is registered for log redaction before the bot's own first
call logs its URL. It is stored per role — in its own 1Password item
(`Telegram <Role>`, tag `casa-agent-bot`, created once then edited in place), or without
1Password in `agent-voices/tokens/<role>`; before binding it is kept in memory and fetched
again at boot. Binding then runs the bot's **self-setup** — description, short
description, the one `/new` command, access restricted to its owner, the persona's picture
(set, changed or removed) — each call idempotent, re-run whenever the digest of the
declared values changes (a persona apply included). A refused picture never fails the
binding.

**Open only once the operator has written.** A bound bot cannot speak first, so the card
becomes "Alex's chat is ready: @…. Open it and press Start." with an "Open Alex's chat"
URL button. The chat counts as **open** from the operator's first turn there that its
agent's queue accepts, whatever it says (a held one released counts). Whether an
operator's turn opens the chat is read when the channel builds it (`own_voice.chat_opening`:
no `chat_open_at` yet); such a turn carries the reserved `_chat_opening` key, which only the channel stamps,
and its prompt gets Casa's line that the operator just opened the chat
(`agent.CHAT_OPENING_NOTE`), so the agent says hello. `chat_open_at` is written only once
the queue took that turn (`TelegramChannel._send_turn` → `own_voice.mark_open`, on a first
send or a retry after a hand-back alike); neither intake nor a release writes it, so a
refused or interrupted delivery opens nothing and its later delivery opens the chat then. A
file sent without a caption starts no turn: it is stored and acknowledged, and the chat
stays unopened until a text or a caption. Once open it stays open: a later failed send
is that send's failure. `own_chat_state` reads one of `not_declared`, `no_bot`,
`not_open`, `open`, `dormant`.

**Held updates leave the journal only through intake.** An agent bot's update queue is
`_IntakeQueue`: every update is offered to `own_voice.intake` before it is queued, which is
before the webhook answers. It is delivered only when the bot is `bound`, reconciled in
this process, its role declared and its agent's queue registered, and nothing older is
held; otherwise it is appended to `agent-voices/held/<bot>.jsonl` (fsynced) and
acknowledged. A release hands the held records, in arrival order, to the endpoint's normal
intake (`TelegramChannel.release_update`); a record is marked released once accepted, and a
refused one keeps itself and every later record held. A text the agent's queue cannot take
after delivery goes back to the lifecycle (`_hand_back`), never dropped. An unbound bot
answers the operator once per boot that the chat is being set up.

**Dormancy, recovery, release.** A bound bot whose role stops being eligible (uninstalled,
or an upgrade that drops the declaration) holds its intake and goes `dormant`: each
operator message gets one line — "Alex no longer has a separate chat — talk to Ellen." or
"Alex isn't installed right now." — and the journal is dropped; eligible again, it is bound
again. A `managed_bot` update for a known bot, or a token Telegram no longer accepts, puts
it in `recovering`: any successful fetch is authoritative, and the bot returns to `bound`
or `dormant`. Only `getManagedBotToken` failing with 403 Forbidden "user is deactivated"
(`RELEASE_ON`, what Telegram answers for a bot deleted in BotFather) releases a bot: its
endpoint stops, its journal goes, the agent waits for a new bot and its card says so. Every
other failure is retried with backoff (30 s to one hour). While the resident's own bot is
not authorized no fetch is tried and the log says so once. An uninstall's result carries
`own_chat: {bot, delete_link}` so the operator can delete the bot in BotFather.

## Contracts & invariants

**INV-OWN-006**: An update that reaches an agent's own bot passes the lifecycle's intake before it is queued; it is delivered only to a bound, reconciled, declared agent whose queue is registered and with nothing older held, otherwise journaled before Telegram is acknowledged, and held records are released in arrival order, a refused one holding itself and every later one.

**INV-OWN-007**: A bot is registered only from the `managed_bot_created` message the operator's own creation sends to Casa's bot in his chat, and bound only to the one waiting agent whose suggestion it matches exactly or the agent the operator names; an agent never holds two bots and a bot is never rebound.

**INV-OWN-008**: Every lifecycle effect finds its phase on disk, every phase has a boot reconciliation, and a bot is released only on the token fetch failure that establishes its deletion; every other failure keeps it recovering.

**INV-OWN-011**: The operator's first turn in an agent's own chat that the agent's queue accepts, whatever it says, opens the chat: that turn alone carries Casa's line that the operator just opened it, and `chat_open_at` is persisted only after the acceptance, on a first send and on a retry after a hand-back alike; a delivery refused or interrupted, across a restart included, and a file sent without a caption open nothing, and the chat's next accepted operator turn carries the line.

## Failure behavior

**A lifecycle failure at intake.** Logged; the update goes no further. Delivering it
could hand the agent a message before its chat is ready.

**Casa restarts mid-transition.** Boot runs each bot's step: a bot reconciles against the
committed declaration before its endpoint delivers, an unbound bot is asked about again, a
token is fetched again, and a held journal is released once the conversation's queue is
registered (`_reconcile_own_chats`, after the channels start and before replays and job
resumes). The graceful stop cancels pending retries before the channels stop.

**A turn taken but the mark not written.** A crash after the agent's queue accepted the opening
turn and before `chat_open_at` was written leaves the chat unopened, so its next operator turn
opens it again and the agent may say hello twice. A crash after the mark and before the turn
ran loses the greeting only together with the message it rode on. A mark that fails to
write is logged and leaves the chat unopened the same way; a failure to read whether the
turn opens the chat is logged and the turn goes on without the opening line.

## Extension points

**A new phase** persists its name before its effect and adds its boot step to `BOOT_STEPS`.
**A new path that hands an operator's turn to an agent's queue** goes through
`TelegramChannel._send_turn`, so a turn that opens the chat marks it open once it is taken.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/own_voice_lifecycle.py`
- `casa/rootfs/opt/casa/casa_core.py::_reconcile_own_chats`
- `casa/rootfs/opt/casa/channels/telegram.py::_IntakeQueue`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.release_update`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._send_turn`
- `casa/rootfs/opt/casa/own_voice.py::chat_opening`
- `casa/rootfs/opt/casa/own_voice.py::mark_open`

**Tests**
- `tests/test_own_voice_lifecycle.py`
- `tests/test_managed_bot_signals.py`
- `tests/test_own_chat_routing.py`
- `tests/test_own_voice_conversation.py`

**Related**
- [`architecture/own-voice.md`](../architecture/own-voice.md)
- [`architecture/telegram-bots.md`](../architecture/telegram-bots.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/persona-lifecycle.md`](../architecture/persona-lifecycle.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
<!-- END SOURCEMAP -->
