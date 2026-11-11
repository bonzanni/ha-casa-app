---
last_reviewed: 2026-11-11
---

# Agents with their own chat

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a specialist that declares its own chat talks to the operator from its own Telegram
bot: the bot's lifecycle (offer, creation, binding, self-setup, opening, token changes,
dormancy, release), the capability rule that keeps every agent's surface in its own chat,
the agent's conversation there, the tasks it runs for others, and how a message in an
agent's chat finds its destination. The per-bot transport these bots run on is
[`telegram-bots.md`](telegram-bots.md); a plugin's delivered posts are
[`plugin-delivered-slots.md`](plugin-delivered-slots.md); the run card and its admission
are [`plugin-operator-calls.md`](plugin-operator-calls.md); the stored-call settler and the
agent's lane are [`stored-call-buttons.md`](stored-call-buttons.md) and
[`specialist-desk.md`](specialist-desk.md); a job's own reply route is
[`background-job-direct-chat.md`](background-job-direct-chat.md).

## Mental model

**Two kinds of agent, one rule.** A specialist whose committed component declares an
`own_chat` block (its `role.yaml`; only a `kind: specialist` may carry one, the role and
runtime schemas refuse it elsewhere) gets a bot of its own, and its chat there holds two
things: its **conversation** with the operator, and every **task** it runs. Every other
agent stays behind the resident and has no surface at all — no capability post, no card,
no file, no run card, no Stop, no job line. The resident's chat is its own and always
open. `own_voice.require_open_chat` is the one function every surface asks.

**The declaration.** `own_chat` carries the bot's two profile texts:
`short_description` (1–120 characters) and `description` (1–512), both required. The
optional persona image is the pack's `avatar.jpg` ([`persona-lifecycle.md`](persona-lifecycle.md)).
A role is eligible for its own chat while it is installed and its committed active
component declares one (`own_voice.declared`): the committed declaration is truth.

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
through the resident's bot and stored per role — in its own 1Password item
(`Telegram <Role>`, tag `casa-agent-bot`, created once then edited in place), or without
1Password in `agent-voices/tokens/<role>`; before binding it is kept in memory and fetched
again at boot. Binding then runs the bot's **self-setup** — description, short
description, the one `/new` command, access restricted to its owner, the persona's picture
(set, changed or removed) — each call idempotent, re-run whenever the digest of the
declared values changes (a persona apply included). A refused picture never fails the
binding.

**Open only once the operator has written.** A bound bot cannot speak first, so the card
becomes "Alex's chat is ready: @…. Open it and press Start." with an "Open Alex's chat"
URL button. The chat counts as **open** from the operator's first message there (a held
one released counts); a bare `/start` also leaves the agent Casa's note that the operator
just opened the chat, so it introduces itself. Once open it stays open: a later failed send
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

**A task's two destinations, decided once.** A task is work that is not a conversation's:
a delegation, a plugin job, a plugin continuation. Its launcher stamps, at its birth:
- its **executor**, the agent that runs it (which lane, fault and capability rule apply);
- its **delivery**, where every post of it goes (`task_model.delivery_for`): the
  resident's chat when the resident runs it; the executor's own endpoint and the
  operator's chat there when that chat is open at this moment; `None` otherwise — and
  `None` stays `None` even if the chat opens while the task runs;
- its **requester**, who receives its result (`task_model.Requester`): the agent whose
  turn started it, on that turn's route — the resident, or the executor's own
  conversation for work that conversation started.

The fields ride on the records (`VoiceJob`, `EngagementRecord`, the continuation's
`DelegationRecord` and binding), persisted, `"delivery": null` included, and on the
task's origin as the reserved `_task_delivery` key (`provenance.task_delivery_marker`),
which no ingress can set and `carry_route` does not copy. A turn that is not a task — the
resident's own, an agent's conversation — delivers in its own chat, and only when that
chat's endpoint is the executing agent's own (`authz_grants.resolve_grant_identity`): a turn
running another agent there has no delivery, so its surfaces refuse and post nothing.

**The capability rule.** A call of an agent's plugin that posts (a capability with a
delivered slot, a call that asks Casa: run card, Stop) is refused at admission, before the
authorization decision and before the plugin runs, unless the agent's own chat is open
(`result_broker.surface_refusal`) and the call has a delivery; the delivery checks again
before it posts. The same rule refuses `send_media`, `ask_operator` and `start_job` for a
host whose chat is not open; `send_media` asks it whenever the file is bound for an agent's
own bot (`tools._closed_own_chat`), so a task born while the chat was open refuses once it
has closed. A scheduled job whose host's chat is not open is not started
(logged), and nothing of it appears in the resident's chat. The typed refusals:
`own_chat_missing` ("Finance has no chat of its own: return what you have as text."),
`own_chat_not_open` (tap Open … in the resident's chat), `own_chat_unreachable` (the bot is
recovering), and, for a task born with no delivery, "… was not open when this task
started …; put what matters in your answer". A refused plugin call is told the line after
`not executed: `, naming the agent plainly, without the `📊` glyph. In an agent's own chat
its posts carry no `📊` label (`result_broker.label_at`): the chat is the agent.

**A task in an agent's chat.** A delegation to an agent whose chat is open at its birth
opens its post index (`task_posts.open`) and its input destination
(`task_input.open_pending`) first, then posts its start line there — "Ellen asked: «…»" —
then runs in its own session. Its whole answer, as the agent wrote it, is posted there in
pages (never the requester's bounded copy), and the requester gets today's bounded result.
When the requester is not the executor's conversation, the conversation gets a body-free
note (`chat_notes`) at its next turn: who asked what, and how it ended. Jobs and
continuations leave the same note. A hand-off inside a task stamps the target's own
delivery, never the handing agent's chat.

**The conversation.** Each loaded specialist that declares its own chat is an `Agent` on
the bus under its role, built at boot before the channels start and on every reload
(`agent_registry.keeps_own_conversation`). It runs on the resident's session machinery —
the scoped session key of its own chat, the pool, the resume decision, the sweep, `/new`,
the serial gate — with the specialist's surface: its options come from
`tools._build_specialist_options` (its inbox, its own `start_job` and hand-off grants, its
plugins under its profile), plus the session's extras every specialist session gets
(`specialist_session.session_env_and_pad`: the long-run compaction window, its pad, its
run in the agent's log). What it writes is captured with speaker kind `specialist`. Its
turns come from the operator's direct messages in its chat and from results of work it
started; work the resident started reaches it only as a `(Casa) ` note line.

**Own-chat routing, on every endpoint.** In any chat, task first:
- a text reply to a live task's post goes into that task — a job's through its driver
  ([`background-job-direct-chat.md`](background-job-direct-chat.md)), a delegation's
  through `task_input.submit`, prefixed `[replying to «<first line>»]`; while the task is
  pending its replies queue in order and are written after its brief;
- a file replying to a live task's post, or after a 📎 tap on a live task's card, is stored
  in the executor's inbox, granted to that task alone (`agent_inbox.grant_task_file`,
  asked at each Read), and handed to the task as a `[casa file] <name> at <path>` line;
- a reply to an ended task's post, or anything else, is this chat's agent's turn — the
  ended task named in its reply note;
- the specialist desk's reply route runs only on the resident's bot.

The files of one album share the destination its first file resolved. Every landed post
of a task — each page, proposal, link, file, run card — is filed under it through one
bridge (`TelegramChannel._record_post` → `task_posts.record`): a job's on its engagement
record, a delegation's on its job row, keyed by bot, chat and message, rebuilt at load.

**A card left behind in the resident's chat.** A tap on the resident's bot whose card
records an agent that now declares its own chat (a proposal, a question, a run card's
Stop) is answered "This card belongs to Alex's chat now — open it there." at callback
ingress, before any handler claims, edits or reserves anything.

**A plugin callback answers the sign-in it follows.** When an agent's capability delivers
an operator link, `callback_owner.note_link` records the agent and where it was delivered,
for the link's life; the plugin's callback then nudges that agent, on that chat's bot
([`callback-delivery.md`](callback-delivery.md)). The latest unexpired link decides.

## Contracts & invariants

**INV-OWN-001**: A surface of an agent — a plugin call that delivers a slot or asks Casa, `send_media`, `ask_operator`, a job start — posts only at its task's delivery, in that agent's own open chat (the resident's chat being its own); with no open own chat, or a task born without a delivery, the call is refused with a typed line before the plugin runs, and nothing is ever posted in another chat instead.

What it does not cover: a chat that closes between admission and the post is caught by the
delivery's own check and the result is withheld; a capability that posts nothing is not a
surface and runs as before.

**INV-OWN-002**: A task's delivery is decided once, by its launcher, at its birth — the resident's chat for the resident, the executor's own chat when it is open at that moment, otherwise none — and is carried unchanged by its origin, its records and its binding for the task's whole life; a task born without one posts nothing even after the chat opens.

**INV-OWN-003**: A task's result goes to its requester, the agent whose turn started it; an agent's conversation receives the results of work it started and only a body-free note of work another agent started, and a task in an agent's open chat posts its whole answer there while the requester gets its bounded result.

**INV-OWN-004**: Every landed post of a task is filed under that task, keyed by bot, chat and message, found again after a reload; an operator's reply or file quoting a live task's post goes into that task — queued in order while it is pending, never to a conversation — and a reply to an ended task's post, or any other message, is the chat's own agent's turn.

A reply from anyone but the operator never reaches a task; a job's post must also be one
the operator started in this chat (INV-BGJOB-012).

**INV-OWN-005**: A file sent to a live task is stored in the executor's own inbox and readable by that task alone — the grant is one exact path, asked at each Read, so a file granted while the task runs is readable at once — and a file whose task ended meanwhile stays in the inbox and the operator is told.

**INV-OWN-006**: An update that reaches an agent's own bot passes the lifecycle's intake before it is queued; it is delivered only to a bound, reconciled, declared agent whose queue is registered and with nothing older held, otherwise journaled before Telegram is acknowledged, and held records are released in arrival order, a refused one holding itself and every later one.

**INV-OWN-007**: A bot is registered only from the `managed_bot_created` message the operator's own creation sends to Casa's bot in his chat, and bound only to the one waiting agent whose suggestion it matches exactly or the agent the operator names; an agent never holds two bots and a bot is never rebound.

**INV-OWN-008**: Every lifecycle effect finds its phase on disk, every phase has a boot reconciliation, and a bot is released only on the token fetch failure that establishes its deletion; every other failure keeps it recovering.

**INV-OWN-009**: A tap on the resident's bot on a card whose agent now declares its own chat is answered with one line naming that chat and runs, edits, claims and reserves nothing; a Casa card, the resident's own card and any tap on an agent's own bot are untouched.

**INV-OWN-010**: An agent that declares its own chat keeps one conversation, on the bus under its role, built with the specialist's surface and session extras — never the resident's builder — fed by the operator's direct messages in its chat; `/new` there ends only that conversation's session and its pad.

## Failure behavior

**A lifecycle failure at intake.** Logged; the update goes no further. Delivering it
could hand the agent a message before its chat is ready.

**Casa restarts mid-transition.** Boot runs each bot's step: a bot reconciles against the
committed declaration before its endpoint delivers, an unbound bot is asked about again, a
token is fetched again, and a held journal is released once the conversation's queue is
registered (`_reconcile_own_chats`, after the channels start and before replays and job
resumes). The graceful stop cancels pending retries before the channels stop.

**A task's input after a restart.** `task_input` and `chat_notes` are memory-only: a
restart ends the run they belong to, and a note not yet drained is lost.

**A post of a task fails.** Each task post is bounded (30 s) and logged; it never fails the
work or holds the requester's result.

## Extension points

**A new surface** asks `own_voice.require_open_chat` (or `result_broker.surface_refusal`)
and posts at the task's delivery, never the turn's route. **A new task kind** stamps
executor, delivery and requester at its birth and spreads `carry_task_delivery` when it
derives an origin from another task's.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/own_voice.py`
- `casa/rootfs/opt/casa/own_voice_lifecycle.py`
- `casa/rootfs/opt/casa/task_model.py`
- `casa/rootfs/opt/casa/task_posts.py`
- `casa/rootfs/opt/casa/task_input.py`
- `casa/rootfs/opt/casa/chat_notes.py`
- `casa/rootfs/opt/casa/callback_owner.py`
- `casa/rootfs/opt/casa/provenance.py::task_delivery_marker`
- `casa/rootfs/opt/casa/provenance.py::task_delivery`
- `casa/rootfs/opt/casa/result_broker.py::surface_refusal`
- `casa/rootfs/opt/casa/result_broker.py::label_at`
- `casa/rootfs/opt/casa/tools.py::offer_own_chat`
- `casa/rootfs/opt/casa/tools.py::TaskBirth`
- `casa/rootfs/opt/casa/tools.py::_task_birth`
- `casa/rootfs/opt/casa/tools.py::_settle_task`
- `casa/rootfs/opt/casa/tools.py::_media_delivery`
- `casa/rootfs/opt/casa/tools.py::_closed_own_chat`
- `casa/rootfs/opt/casa/tools.py::_after_own_chat_change`
- `casa/rootfs/opt/casa/agent_registry.py::keeps_own_conversation`
- `casa/rootfs/opt/casa/agent.py::Agent._build_specialist_session_options`
- `casa/rootfs/opt/casa/casa_core.py::_own_conversation_agents`
- `casa/rootfs/opt/casa/casa_core.py::_reconcile_own_chats`
- `casa/rootfs/opt/casa/channels/telegram.py::_IntakeQueue`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel.release_update`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._quoted_task`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._route_task_reply`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._route_task_file`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._refuse_moved_card`

**Tests**
- `tests/test_own_voice_lifecycle.py`
- `tests/test_own_voice_state.py`
- `tests/test_capability_rule.py`
- `tests/test_own_voice_tasks.py`
- `tests/test_own_voice_conversation.py`
- `tests/test_own_chat_routing.py`
- `tests/test_callback_ingress_q7.py`
- `tests/test_managed_bot_signals.py`
- `tests/test_task_posts.py`
- `tests/test_task_input.py`
- `tests/test_task_file_grant.py`
- `tests/test_task_model.py`
- `tests/test_chat_notes.py`
- `tests/test_own_chat_manifest.py`
- `tests/test_own_voice_invariants.py`

**Related**
- [`architecture/telegram-bots.md`](../architecture/telegram-bots.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/plugin-delivered-slots.md`](../architecture/plugin-delivered-slots.md)
- [`architecture/plugin-operator-calls.md`](../architecture/plugin-operator-calls.md)
- [`architecture/stored-call-buttons.md`](../architecture/stored-call-buttons.md)
- [`architecture/specialist-desk.md`](../architecture/specialist-desk.md)
- [`architecture/background-job-direct-chat.md`](../architecture/background-job-direct-chat.md)
- [`architecture/inbound-files.md`](../architecture/inbound-files.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/persona-lifecycle.md`](../architecture/persona-lifecycle.md)
- [`architecture/callback-delivery.md`](../architecture/callback-delivery.md)
<!-- END SOURCEMAP -->
