---
last_reviewed: 2026-11-12
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
agent: its offer card, its current suggestion (the handle offered and the display name
chosen for its bot, if any), every handle it was ever offered, its bot, a
pending change to a new bot, the handle it moved from, when its chat opened; one per bot:
its phase, role, username, name, the open question about it, the digest of its last
self-setup). A bot's phases are `seen → unbound → binding → bound`, with `recovering`,
`dormant`, `releasing`, `released` and `switching` beside them. Every transition persists
the phase that names its effect before the effect, every effect is idempotent or reconciled
by lookup, and every phase has a boot step (`BOOT_STEPS`) that completes or rolls it back.
A per-bot lock covers intake, every phase change and the journal's release; a registry lock
covers what spans bots, taken after the per-bot one. The store is validated strictly: a
record without the agent fields above is set aside like a corrupt one.

**Offer and creation.** Casa posts no offer by itself. A committed install, upgrade or
rollback of an agent that declares a chat it has no bot for says `own_chat: "offer_due"` in
its result; the configurator says so in its answer, and the resident offers the chat with
`offer_own_chat` — then, or whenever the operator asks. The handle (the bot's username) is
the resident's choice, steered by the tool's description (name the role, not the persona;
by default `Casa<Role><digits>Bot`; the operator's existing pattern and wishes first); no
code derives it. Mode `check` answers, for up to five candidates, `invalid_format`,
`owned_by_other`, `used_before` (below), `taken`, `probably_free` or `unknown`, with what
the choice needs: the role, its persona's name, the resident's own bot, the role's current
and previous handles, the other agents' handles and Telegram's username rules. An offer is refused, naming the reason, when
the handle is not a bot username (`valid_username`: 5–32 of `[A-Za-z0-9_]`, ending in
`bot`), when it was ever offered to another agent (each agent's `handles`, kept across new
offers and restarts), or when the probe shows it taken: `TelegramHost.handle_taken` reads
`https://t.me/<handle>` (5 s) and finds an existing account by the page's
`tgme_page_title` block. A probe that cannot complete refuses nothing; it proves a handle
taken, and a reserved or recently freed one looks unused. The offer is an ordinary Casa
card in the resident's chat: "Alex can get a separate chat with you. Tap below, then
Create." with one URL button opening Telegram's pre-filled "Create Bot" form
(`https://t.me/newbot/<resident bot>/<handle>?name=<display name>`); the suggestion and the
handle's ownership are persisted before the card is posted. The operator confirms the form;
Telegram then sends the `managed_bot_created` service message to the resident's bot and a
`managed_bot` update. Only the first registers a bot, and only when it comes from the
operator in his chat with Casa's bot; the update alone registers nothing and is read only
as a token change of a known bot. Casa's bot must have bot management turned on in
BotFather once; Casa never gates on `getMe.can_manage_bots`, which lags that switch.

**Binding: match or ask, one bot per agent.** A new bot binds to the waiting agent (one
with no bot and no pending change) whose current suggested username it has, in any case;
the display name is not compared, since self-setup sets it. A bot whose username is a
pending change's current suggestion starts that agent's switch instead (below). With no
single match Casa asks in the resident's chat ("A new bot @… was just created. Which agent
is it for?"), offering the waiting agents, then the agents whose pending change has no bot
yet — never an agent with a bot and no change — and "None — I'll delete it", so a bot
created from an edited form or from a card naming an older handle is asked about. Picking
an agent with a pending change starts its switch exactly as a matching username would; a
pick whose agent no longer fits (bound meanwhile, change dropped or claimed) binds or moves
nothing, and the question is asked again without it. An agent has one bound bot at a time and a bot is
never rebound to another agent. The token is fetched with `getManagedBotToken`
through the resident's bot's request layer, never the library's Bot method, whose DEBUG
trace logs the return value; it is registered for log redaction before the bot's own first
call logs its URL. It is stored per role — in its own 1Password item
(`Telegram <Role>`, tag `casa-agent-bot`, created once then edited in place), or without
1Password in `agent-voices/tokens/<role>`; before binding it is kept in memory and fetched
again at boot. A stored token is used only for the bot whose id it begins with
(`token_belongs_to`); a token of another bot is never started, and the right one is
fetched. Binding then runs the bot's **self-setup** — its display name (the name chosen
for it, else the persona's), description, short description, the one `/new` command, access
restricted to its owner, the persona's picture (set, changed or removed) — each call
idempotent, re-run whenever the digest of the values it applies changes (a persona apply or
a rename included). A refused picture never fails the binding.

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
waits for may be awaiting it. At boot `casa_core._apply_own_chat_switches`
(→ `Lifecycle.apply_switches`) runs once the durable stores and the tools are loaded and
before the plugin outbox, the agents, Telegram, the workers, the HTTP server, the channels
and the scheduler start; it reads only Casa's records and the token store. For each
pending switch it asks `open_on_bot` again, durable kinds only: if anything still waits on
the old bot the switch stays pending, the old bot boots as usual, and the card says Alex
still had something open there; asking again with `replace` once it is done requests
another restart, with no new bot to create. Otherwise it **retires** the old bot — its held
journal and its stored token (1Password item or file) go, and its record stays, `released`,
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
whose bot does not exist yet; a pending switch whose bot exists still applies at boot, and
the new bot then goes dormant through the ordinary binding.

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

**The bot's name.** Mode `rename` stores the operator's chosen display name as the agent's
suggestion name (1–64 characters, `setMyName`'s limit) — a suggestion name equal to the
persona name recorded beside it counts as none, so an older offer's stored persona name
follows the persona. Self-setup applies it, so a bound bot takes it at once with its own
token, with no new bot and no restart, and a refused call is made again at the next boot;
later offers keep it for the Create form and the next bot. Cards keep the persona's name.

**The agent speaks first; the chat opens once Telegram takes a turn's reply or the operator
writes.** The card becomes "Alex's chat is ready: @…. Open it and say hi." (after a move,
"Alex's new chat is ready: …") with an "Open Alex's chat" URL button. A bot may write to the
operator only once he has pressed Start in its chat, and the `/start` he sends at creation
reaches no endpoint (the bot is not bound yet). So at the end of every binding — a first
bot, a move bound at boot after `apply_switches`, a dormant bot bound again — the lifecycle
spawns one greeting (`Lifecycle._speak_first` → `TelegramHost.speak_first` →
`TelegramChannel.speak_first`), never awaited under the bot's lock; the boot of a bot that is
already bound greets nobody, and nothing retries. Under the chat's serial lock the endpoint
reads again whether the chat is unopened (a held message the binding released may have
opened it, and an operator message holding the lock first opens it instead) and hands its
agent a turn Casa starts: content `/start`, the reserved `synthetic: "chat_opening"` marker
and no operator identity, so it is no direct message and carries none of his grants
(`provenance.turn_provenance` classifies it `other`), and the same `_chat_opening` key the
operator's first turn would carry. It thinks privately (`NoStream("chat_opening")`): the
reply goes out whole, once. The chat opens only when that reply reached Telegram — the
message's `on_delivery`, which the agent calls once a delivery landed, marks it
(`own_voice.mark_open`). Telegram refusing it (Forbidden, or a BadRequest such as "Chat not found":
the operator never pressed Start) is one info line in `TelegramChannel.send_response`, and
the chat stays unopened; the agent's session keeps the unsent greeting. A greeting turn the
agent answers with silence opens nothing either: the agent's acknowledgement of a chosen
silence skips a `chat_opening` turn. Meanwhile its surfaces may post
([`own-voice.md`](own-voice.md)); a completed binding leaves the resident a memory line
([`memory-capture.md`](memory-capture.md)).

Otherwise the chat counts as **open** from the operator's first turn there that its agent's
queue accepts, whatever it says (a held one released counts). Whether an operator's turn
opens the chat is read when the channel builds it (`own_voice.chat_opening`: no
`chat_open_at` yet; `own_voice.chat_moved`: the agent moved to this bot); such a turn
carries the reserved `_chat_opening` key — `True`, or `"moved"` in a new chat after a move —
which only the channel stamps, and its prompt gets Casa's line that the operator just opened
the chat (`agent.CHAT_OPENING_NOTE`, or `agent.CHAT_MOVED_NOTE`: the new chat, the previous
one closed), so the agent says hello. For an operator's turn `chat_open_at` is written only
once the queue took it (`TelegramChannel._send_turn` → `own_voice.mark_open`, on a first
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
after delivery goes back to the lifecycle (`_hand_back`), never dropped. An unbound or
switching bot answers the operator once per boot that the chat is being set up.

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

**INV-OWN-008**: Every lifecycle effect finds its phase on disk, every phase has a boot reconciliation, and a recovering bot is released only on the token fetch failure that establishes its deletion; every other failure keeps it recovering.

**INV-OWN-011**: A binding that completes greets once: Casa hands the bound agent one turn of its own, marked `synthetic: "chat_opening"` with no operator identity, carrying the opening line, unless the chat is open by then — read under the chat's serial lock — and the boot of an already-bound bot greets nobody; that turn opens the chat only once its reply reached Telegram, and a reply Telegram refuses leaves it unopened, and an operator `/start` that arrives while that greeting is on its way is held: the delivered greeting answers it, and a greeting refused, ended in silence or undelivered when its window runs out replays it as his own turn. Otherwise the operator's first turn in an agent's own chat that the agent's queue accepts, whatever it says, opens the chat: that turn alone carries Casa's line that the operator just opened it — in an agent's new chat after a move to a new bot, the line that it is the new chat — and `chat_open_at` is persisted only after the acceptance, on a first send and on a retry after a hand-back alike; a delivery refused or interrupted, across a restart included, and a file sent without a caption open nothing, and the chat's next accepted operator turn carries the line.

**INV-OWN-012**: A bot is registered only from the operator's own `managed_bot_created` message to Casa's bot in his chat, and bound to an agent only when its username equals that agent's current suggestion (a waiting agent's first bot, or a pending change) or the operator names a waiting agent or one with a pending change; a bot is never rebound to another agent, and an agent has one bound bot at a time.

**INV-OWN-015**: A pending switch is applied only by the boot step that runs before Casa's HTTP server, channels, workers and scheduler start, and only when no durable record still waits on the old bot; it drops the old bot's held journal and stored token and keeps its record as `released`, no agent's bot, before the new bot enters binding, and a stored token is used only for the bot whose id it begins with.

**INV-OWN-016**: An offer's handle is refused when it is not a valid bot username, when it was ever offered to another agent, or when the t.me probe shows it taken — except the handle of a released bot of the same agent, which records that bot as the agent's pending change only once its token was fetched; a released bot of another agent is never offered, and a probe that cannot complete refuses nothing.

## Failure behavior

**A lifecycle failure at intake.** Logged; the update goes no further. Delivering it
could hand the agent a message before its chat is ready.

**Casa restarts mid-transition.** Boot runs each bot's step: a bot reconciles against the
committed declaration before its endpoint delivers, an unbound bot is asked about again, a
token is fetched again, and a held journal is released once the conversation's queue is
registered (`_reconcile_own_chats`, after the channels start and before replays and job
resumes). Pending switches are applied earlier, before anything can start a bot. The
graceful stop cancels pending retries before the channels stop.

**Something opened on the old bot before the restart.** The boot step finds a durable
record still waiting on it: the switch stays pending, the old bot boots as usual, and the
card says Alex still had something open and to ask again once it is done. A boot step that
fails is logged and never stops the boot; the switch is tried again at the next one.

**The restart request fails.** The card says "Alex moves to @new when Casa next restarts.",
the tool answers `restart_failed`, and the switch completes at the next restart.

**The old bot's token cannot be deleted.** Logged; the switch goes on. The binding writes the
same 1Password item in place, and a stored token of another bot is never used.

**The handle probe cannot reach t.me** (network, egress, a status other than 200). The check
says `unknown` for that candidate, and an offer goes ahead.

**An earlier bot Telegram will not hand back.** Nothing moves and no card is posted; the
result says the bot is gone or not reachable, and the resident tells the operator.

**A store `open_on_bot` reads fails.** Logged, and reported as "something Casa could not
check": the change waits rather than going ahead blind.

**A turn taken but the mark not written.** A crash after the agent's queue accepted the opening
turn and before `chat_open_at` was written leaves the chat unopened, so its next operator turn
opens it again and the agent may say hello twice. A crash after the mark and before the turn
ran loses the greeting only together with the message it rode on. A mark that fails to
write is logged and leaves the chat unopened the same way; a failure to read whether the
turn opens the chat is logged and the turn goes on without the opening line, and one to read
whether it opens a moved chat is logged and opens it as a first chat.

**Telegram refuses the greeting.** The operator has not pressed Start in the bot's chat: one
info line, the chat stays unopened, and his first message there opens it with the opening
line as before (the agent's session already holds the greeting it could not send). A
greeting whose turn fails or ends in silence opens nothing either. While the greeting is on
its way (taken by the agent's queue, not yet delivered, refused or ended silently, and for at
most `GREETING_IN_FLIGHT_S`), an operator's bare `/start` in that chat adds no turn of its
own: it is held, the delivered greeting answers it, and a greeting refused, ended in silence
or still undelivered when the window runs out replays it as his turn
(`TelegramChannel._end_greeting`, which every end of the window goes through). Any other operator message that reaches the agent in that window may still carry
the opening line too, but the chat is marked open, and its cards posted, once.

## Extension points

**A new phase** persists its name before its effect and adds its boot step to `BOOT_STEPS`.
**A new kind of open item on a bot** (something that waits on an agent's current bot) joins
`own_voice.open_on_bot`, marked durable when it survives a restart.
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
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.speak_first`
- `casa/rootfs/opt/casa/own_voice.py::chat_opening`
- `casa/rootfs/opt/casa/own_voice.py::mark_open`
- `casa/rootfs/opt/casa/own_voice.py::chat_moved`
- `casa/rootfs/opt/casa/own_voice.py::open_on_bot`
- `casa/rootfs/opt/casa/own_voice.py::apply_switches_at_boot`
- `casa/rootfs/opt/casa/casa_core.py::_apply_own_chat_switches`

**Tests**
- `tests/test_own_voice_lifecycle.py`
- `tests/test_managed_bot_signals.py`
- `tests/test_own_chat_routing.py`
- `tests/test_own_voice_conversation.py`
- `tests/test_own_chat_open_items.py`
- `tests/test_own_chat_switch_boot.py`
- `tests/test_own_chat_move_back_and_rename.py`

**Related**
- [`architecture/own-voice.md`](../architecture/own-voice.md)
- [`architecture/telegram-bots.md`](../architecture/telegram-bots.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/persona-lifecycle.md`](../architecture/persona-lifecycle.md)
- [`architecture/persistent-state.md`](../architecture/persistent-state.md)
<!-- END SOURCEMAP -->
