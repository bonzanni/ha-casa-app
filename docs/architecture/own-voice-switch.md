---
last_reviewed: 2026-11-23
---

# An agent's own bot: moving to a new bot

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How an agent whose chat is open moves to a new bot, returns to an earlier one, or has its bot renamed: the pending change, the pending switch applied at boot, the retired old bot, the move's cards, and the failures that leave a switch pending. The records and phases, the offer, binding and self-setup, the chat's opening, the held journal, dormancy and release are [`own-voice-lifecycle.md`](own-voice-lifecycle.md); what the chat is for once open is [`own-voice.md`](own-voice.md).

## Mental model

**A new bot for an agent.** The operator asks the resident for a new handle and she calls
`offer_own_chat` with mode `replace`. It is refused while anything still waits on the
agent's current bot (`own_voice.open_on_bot`, which names each kind it finds; see
[`own-voice.md`](own-voice.md)), so she can say what is open and that the change can happen
once it is done. Otherwise the handle meets the same refusals as an offer, the agent's
record gains a pending `change` (the old bot and its username), and the card reads "Alex
can move to a new chat with you, replacing @old. Tap below, then Create." A new handle
replaces the suggestion and keeps the change; mode `drop` cancels it while its bot does not
exist yet ("Alex keeps @old."). A created bot whose username is exactly that change's
current suggestion turns it, under the registry lock, into a **pending switch**: the bot is
recorded in phase `switching` and is never bound in this process; its endpoint holds what
it receives and says once that the chat is being set up. Casa edits the card ("Alex is
moving to @new and says hi there in a moment.", which never mentions the restart) and asks the Supervisor for a restart
(`supervisor_restart`), which answers only once Casa has stopped, so nothing Casa's stop
waits for may be awaiting it — after the restart step's own busy check. At boot `casa_core._apply_own_chat_switches`
(→ `Lifecycle.apply_switches`) runs once the durable stores and the tools are loaded and
before the plugin outbox, the agents, Telegram, the workers, the HTTP server, the channels
and the scheduler start; it reads only Casa's records and the token store. For each
pending switch it asks `open_on_bot` again, durable kinds only: if anything still waits on
the old bot the switch stays pending, the old bot boots as usual, and the card says Alex
still had something open there; asking again with `replace` once it is done requests
another restart, with no new bot to create. Otherwise it **retires** the old bot — its held
journal and its stored token (vault item or file) go, and its record stays, `released`,
with its role and handle and no setup digest — and hands the new bot to the ordinary
binding as `binding`; every step is persisted and idempotent, so a
crash re-runs it from what is on disk. The agent keeps the old handle (`moved_from`) until
its new chat opens. The move's cards are new messages, since the offer card is far up the
resident's chat by then and an edit there goes unnoticed: the switch clears the agent's
offer card, so the binding posts the ready card ("Alex's new chat is ready: @…. Open it and
say hi.") as a new message, which later restarts edit; a bound agent whose chat is not
open and whose card is missing (a post that failed) gets it posted at the next restart. The opening turn tells the agent the
chat is its new one, and Casa posts "Alex has moved to @new. You can delete @old now." with
a "Delete @old" button opening BotFather's delete flow, and edits the ready card to "Alex's
new chat is open." (a post that fails puts that text and button on the ready card instead). Nothing moves from the old chat: its history stays there, and what
was sent to either bot around the restart may be lost. An uninstall drops a pending change
whose bot does not exist yet, and retires a pending switch's new bot together with the old
one ([`own-voice-lifecycle.md`](own-voice-lifecycle.md)).

**Back to an earlier bot.** A released bot is no agent's bot: `bot_of`, the chat-opening
reads (`_role_of`), the boot steps and intake all pass it by. For the agent it was moved
away from, `check` reports its handle `used_before` (unless the handle was ever offered to
another agent); for every other agent nothing changes, so a bot is never offered to another
agent. Offering that handle (`replace`, after the same busy check, or `offer` for an agent
with no bot) posts no card: Casa fetches the bot's token with `getManagedBotToken`. Refused,
no agent or card changes and the result says so — `bot_gone` when Telegram reports the bot
deleted (Casa then forgets its record, so the handle is an ordinary one again) or
`bot_unreachable`. Fetched, the bot becomes the pending change's new bot in phase
`switching`, under the registry lock (unless a bot created from an earlier offer claimed
the change meanwhile), and Casa asks for the restart as for a created bot; the switch binds
it and its self-setup runs afresh.

**The bot's name.** Mode `rename`, or a name typed over the current Create form's pre-filled one,
stores the operator's chosen display name as the agent's suggestion name (1–64 characters, `setMyName`'s limit) — a suggestion name equal to the
persona name recorded beside it counts as none, so an older offer's stored persona name
follows the persona. Self-setup applies it, so a bound bot takes it at once with its own
token, with no new bot and no restart, and a refused call is made again at the next boot;
later offers keep it for the Create form and the next bot. Cards keep the persona's name.

## Contracts & invariants

**INV-OWN-015**: A pending switch is applied only by the boot step that runs before Casa's HTTP server, channels, workers and scheduler start, and only when no durable record still waits on the old bot; it drops the old bot's held journal and stored token and keeps its record as `released`, no agent's bot, before the new bot enters binding, and a stored token is used only for the bot whose id it begins with.

## Failure behavior

**Something open on the old bot.** When the new bot is created or picked (any kind) or at
the boot step (durable kinds): no restart, the switch stays pending, the old bot runs as
usual, and the card says the move waits until it is done. A boot step that
fails is logged and never stops the boot; the switch is tried again at the next one.

**The restart request fails.** The card says "Alex moves to @new when Casa next restarts.",
the tool answers `restart_failed`, and the switch completes at the next restart.

**The old bot's token cannot be deleted.** Logged; the switch goes on. The binding writes the
same vault item in place, and a stored token of another bot is never used.

**An earlier bot Telegram will not hand back.** Nothing moves and no card is posted; the
result says the bot is gone or not reachable, and the resident tells the operator.

**A store `open_on_bot` reads fails.** Logged, and reported as "something Casa could not
check": the change waits rather than going ahead blind.

## Extension points

**A new kind of open item on a bot** (something that waits on an agent's current bot) joins
`own_voice.open_on_bot`, marked durable when it survives a restart.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/own_voice.py::open_on_bot`
- `casa/rootfs/opt/casa/own_voice.py::apply_switches_at_boot`
- `casa/rootfs/opt/casa/casa_core.py::_apply_own_chat_switches`

**Tests**
- `tests/test_own_chat_open_items.py`
- `tests/test_own_chat_switch_boot.py`
- `tests/test_own_chat_move_back_and_rename.py`

**Related**
- [`architecture/own-voice-lifecycle.md`](../architecture/own-voice-lifecycle.md)
- [`architecture/own-voice.md`](../architecture/own-voice.md)
- [`architecture/telegram-bots.md`](../architecture/telegram-bots.md)
<!-- END SOURCEMAP -->
