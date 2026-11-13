---
last_reviewed: 2026-11-12
---

# Agents with their own chat

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How a specialist that declares its own chat talks to the operator from its own Telegram
bot: the capability rule that keeps every agent's surface in its own chat, the agent's
conversation there, the tasks it runs for others, and how a message in an agent's chat
finds its destination. The bot's lifecycle (offer, creation, binding, self-setup, opening,
a move to a new bot, token changes, dormancy, release) is [`own-voice-lifecycle.md`](own-voice-lifecycle.md). The
per-bot transport these bots run on is [`telegram-bots.md`](telegram-bots.md); a plugin's delivered posts are
[`plugin-delivered-slots.md`](plugin-delivered-slots.md); the run card and its admission
are [`plugin-operator-calls.md`](plugin-operator-calls.md); the stored-call settler and the
agent's lane are [`stored-call-tap.md`](stored-call-tap.md) and
[`agent-lane.md`](agent-lane.md); a job's own reply route is
[`background-job-direct-chat.md`](background-job-direct-chat.md).

## Mental model

**Two kinds of agent, one rule.** A specialist whose committed component declares an
`own_chat` block (its `role.yaml`; only a `kind: specialist` may carry one, the role and
runtime schemas refuse it elsewhere) gets a bot of its own, and its chat there holds two
things: its **conversation** with the operator, and every **task** it runs. Every other
agent stays behind the resident and has no surface at all — no capability post, no card,
no file, no run card, no Stop, no job line. The resident's chat is its own and always
open. `own_voice.require_open_chat` is the one function every surface asks.

**The greeting counts as open.** Casa's greeting turn tells the agent the operator just opened
its chat, and the chat opens with that greeting, so while it is on its way the rule admits the
agent's surfaces: a tool Fido checks during his hello is not refused as an unopened chat. The
endpoint marks it (`own_voice.greeting_started`) before handing the turn over and clears it
wherever the greeting window ends (`TelegramChannel._end_greeting`: delivered, refused, silent,
unsendable or out of time, which bounds it) and when the endpoint stops; the mark is in
memory only. A greeting that reached no one leaves the chat unopened, and a surface that posted
meanwhile met Telegram's own refusal.

**The declaration.** `own_chat` carries the bot's two profile texts:
`short_description` (1–120 characters) and `description` (1–512), both required. The
optional persona image is the pack's `avatar.jpg` ([`persona-lifecycle.md`](persona-lifecycle.md)).
A role is eligible for its own chat while it is installed and its committed active
component declares one (`own_voice.declared`): the committed declaration is truth.

**The bot's lifecycle** — the records and phases that drive it, the offer, creation,
binding and self-setup, the chat's opening, the held journal, dormancy, recovery and
release — is [`own-voice-lifecycle.md`](own-voice-lifecycle.md).

**The resident's tool for an agent's chat.** `offer_own_chat` (`tools.offer_own_chat` →
`own_voice.offer_own_chat`) takes the agent and a `mode`: `check` reports on up to five
candidate handles — `invalid_format`, `owned_by_other`, `used_before`, `taken`,
`probably_free` or `unknown` — and gives the context to choose one (the role, its persona's
name, the resident's own bot, the role's current and previous handles, the other agents'
handles, Telegram's username rules); `offer`, the default, posts the
card that creates the chat with the handle the resident chose (`username`), or, once the
bot exists, the card that opens it, and answers `already_open` with the link when the chat
is open; `replace` offers a new bot with a new handle in place of the current one, and,
asked again after a switch was held back, requests the restart that finishes it; `drop`
keeps the current bot, while the new one does not exist yet; `rename` gives the agent's bot
the display name `name`. For the handle of a bot the agent was moved away from (`check` says
`used_before`), `offer` and `replace` move it back to that bot. The handle is always the
resident's: the tool's description carries the guidance (the role, not the persona;
`Casa<Role><digits>Bot` by default; the operator's pattern and wishes first), and code
only checks it. An install, upgrade or rollback that leaves a declared agent without a bot
says `own_chat: "offer_due"` in its result, and the configurator's prompt has it say so,
so the resident makes the offer; Casa posts none by itself.

**What still waits on a bot.** `own_voice.open_on_bot(role, bot_id)` lists, one short
phrase per kind in a fixed order, what Casa still holds that waits on an agent's bot; an
agent moves to a new bot only when it is empty. Durable kinds: a delegation asked on the
bot or delivering there that is accepted or running, waiting for input it can still take,
or still owing its requester an announcement; a live or idle job started on the bot or
posting there; an ended job whose keyboard post on the bot is not retired; a scheduled
question stored as posted through it; a browsing request that still holds its run card and
its Stop key — admitted, neither stopped nor ended, its binding there
(`run_admission.Confirmations.open_cards_on_bot`); an update held for it. Memory-only
kinds: a live question or card asked on it (`verdict_broker.VerdictBroker.live_on_bot`), a
continuation queued on it (`plugin_continuations.Continuations.pending_on_bot`), and a turn
of the agent being answered now — any of its turns, since turns are not tracked per bot.
`durable_only` reads the durable kinds alone, which is all that exists at boot. A store
that is not there holds nothing; one that fails to read is logged and reported as
"something Casa could not check", so the change waits. Reminders and triggers are not
counted: they pick their bot when they fire.

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
(`task_input.open_pending`) first, then posts its start line there — "Ellen asked me: …",
the brief as written; a task born on a Casa-started plugin setup or erase turn posts none —
then runs in its own session, whose delegation context says that its closing message is
posted to the operator (`answer_shown`). The start line and the answer are filed under
slots of their own (`task-start`, `task-answer`), so each is its own post. The message it
closed its turn with
(`tools._closing_text`), whole, is posted there in pages — never its working notes before
it, never the requester's bounded copy, and nothing when it closes with `<silent/>` — and
the requester gets today's bounded result. That result's Casa notes name the agent's chat:
a post echo says the posts went to the person there and asks the requester to close the
loop in its own words (`OWN_CHAT_ECHO_NOTE`), a closing silence is said without steering it
to silence (`OWN_CHAT_SILENT_CLOSE_NOTE`), and a closing message that posts is said to be
already read there, so the requester closes in a line or two instead of retelling it
(`OWN_CHAT_ANSWER_NOTE`). A delegation still running when its call returns (`async`, or a
`sync` one past its 60-second wait) carries `own_chat_note` on its `pending` result
(`OWN_CHAT_PENDING_NOTE`): the person sees the work and will get the answer in that chat,
so the requester owes no "still on it" line — one written while the answer was landing
reached the person after it. That prediction is the post's own rule: an endpoint still serves
the task's delivery (`tools._task_endpoint`, the lookup the post makes, so an answer whose
bot was released meanwhile is not called read), and the whole closing message is no
silence — the same text both silence notes judge, so the notes never contradict each other
or the chat. What the operator wrote into the task while it ran reaches the
requester as one more line after them, saying the agent already had those words and worked
with them, so they are not the requester's to pass on or to start again as a new task
(`OPERATOR_WORDS_NOTE`, from `task_input.take_said`);
a continuation that declares progress only is announced for those words alone.
When the requester is not the executor's conversation, the conversation gets a body-free
note (`chat_notes`) at its next turn: who asked what, and how it ended ("Ellen asked you:
… — you answered"). Jobs and
continuations leave the same note. A continuation of a run in the agent's chat opens its
index and its input the same way, right after its record is registered and before its
turn runs; its session attaches the input after its first message, and the input closes
when the work ends, however it ends. A hand-off inside a task stamps the target's own
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
turns come from the operator's direct messages in its chat, from his summons of its bot in a
chat it is not a member of — a turn of this same conversation whose final text alone is
shown there ([`guest-summons.md`](guest-summons.md)); a friend's summons in a group he allowed
is not one of them but a guest run of its own, with none of this conversation's memory — and
from results of work it started; work the resident started reaches it only as a `(Casa) ` note line, under one
frame line that hands those notes to it as its own memory (`agent.CHAT_NOTES_FRAME`).

**Own-chat routing, on every endpoint.** In any chat, task first:
- a text reply to a live task's post goes into that task — a job's through its driver
  ([`background-job-direct-chat.md`](background-job-direct-chat.md)), a delegation's
  through `task_input.submit`, prefixed `[replying to «<first line>»]`; while the task is
  pending its replies queue in order and are written after its brief. The operator's own
  words (a file's as its caption and name) are also kept apart from the input, so closing
  it leaves them, for the requester to be told once (`task_input.take_said`): the last five
  per task, 300 characters each, for the 64 latest tasks, memory-only;
- a file replying to a live task's post, or after a 📎 tap on a live task's card, is stored
  in the executor's inbox, granted to that task alone (`agent_inbox.grant_task_file`,
  asked at each Read), and handed to the task as a `[casa file] <name> at <path>` line;
- a reply to an ended task's post, or anything else, is this chat's agent's turn — the
  ended task named in its reply note — in the resident's chat as in an agent's own.

The files of one album share the destination its first file resolved. Every landed post
of a task — each page, proposal, link and file of a plugin, a `send_media` file, a run
admission's question and status card, the Stop's line — is filed under it through one
bridge (`TelegramChannel._record_post` → `task_posts.record`): a job's on its engagement
record, a delegation's (or continuation's) on its job row, keyed by bot, chat and message,
rebuilt at load. The task rides on the post record's owner: a plugin call's job or
delegation (`result_broker._post_owner`), a file's bound engagement or run
(`tools._media_post`); the Stop's line is filed as the card it answers. A job's start line,
question and parking line are filed as its keyboard posts.

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

**INV-OWN-003**: A task's result goes to its requester, the agent whose turn started it; an agent's conversation receives the results of work it started and only a body-free note of work another agent started, and a task in an agent's open chat posts there the message it closed its turn with (nothing when that is silence) while the requester gets its bounded result.

**INV-OWN-004**: Every landed post of a task is filed under that task, keyed by bot, chat and message, found again after a reload; an operator's reply or file quoting a live task's post goes into that task — queued in order while it is pending, never to a conversation — and a reply to an ended task's post, or any other message, is the chat's own agent's turn.

A reply from anyone but the operator never reaches a task; a job's post must also be one
the operator started in this chat (INV-BGJOB-012).

**INV-OWN-005**: A file sent to a live task is stored in the executor's own inbox and readable by that task alone — the grant is one exact path, asked at each Read, so a file granted while the task runs is readable at once — and a file whose task ended meanwhile stays in the inbox and the operator is told.

**INV-OWN-009**: A tap on the resident's bot on a card whose agent now declares its own chat is answered with one line naming that chat and runs, edits, claims and reserves nothing; a Casa card, the resident's own card and any tap on an agent's own bot are untouched.

**INV-OWN-010**: An agent that declares its own chat keeps one conversation, on the bus under its role, built with the specialist's surface and session extras — never the resident's builder — fed by the operator's direct messages in its chat; `/new` there ends only that conversation's session and its pad.

**INV-OWN-017**: While an agent's greeting is on its way — from the moment its endpoint hands the greeting turn over until the greeting window ends — the capability rule admits that agent's surfaces as if its chat were open; once the window ends without the chat opening — the reply refused by Telegram or silent, a reply `send_response` cannot send, the endpoint stopped, or at the latest `GREETING_IN_FLIGHT_S` after the turn was handed over — they are refused again as an unopened chat.

## Failure behavior

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
- `casa/rootfs/opt/casa/own_voice.py::greeting_started`
- `casa/rootfs/opt/casa/own_voice.py::greeting_ended`
- `casa/rootfs/opt/casa/own_voice.py`
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
- `casa/rootfs/opt/casa/tools.py::_with_operator_words`
- `casa/rootfs/opt/casa/task_input.py::take_said`
- `casa/rootfs/opt/casa/tools.py::_media_delivery`
- `casa/rootfs/opt/casa/tools.py::_closed_own_chat`
- `casa/rootfs/opt/casa/tools.py::_after_own_chat_change`
- `casa/rootfs/opt/casa/agent_registry.py::keeps_own_conversation`
- `casa/rootfs/opt/casa/agent.py::Agent._build_specialist_session_options`
- `casa/rootfs/opt/casa/casa_core.py::_own_conversation_agents`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._quoted_task`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._route_task_reply`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._route_task_file`
- `casa/rootfs/opt/casa/channels/telegram.py::TelegramChannel._refuse_moved_card`

**Tests**
- `tests/test_own_voice_state.py`
- `tests/test_capability_rule.py`
- `tests/test_own_voice_tasks.py`
- `tests/test_own_voice_conversation.py`
- `tests/test_own_chat_routing.py`
- `tests/test_callback_ingress_q7.py`
- `tests/test_task_posts.py`
- `tests/test_task_input.py`
- `tests/test_task_file_grant.py`
- `tests/test_task_model.py`
- `tests/test_chat_notes.py`
- `tests/test_own_chat_manifest.py`
- `tests/test_own_voice_invariants.py`

**Related**
- [`architecture/own-voice-lifecycle.md`](../architecture/own-voice-lifecycle.md)
- [`architecture/telegram-bots.md`](../architecture/telegram-bots.md)
- [`architecture/telegram.md`](../architecture/telegram.md)
- [`architecture/plugin-delivered-slots.md`](../architecture/plugin-delivered-slots.md)
- [`architecture/plugin-operator-calls.md`](../architecture/plugin-operator-calls.md)
- [`architecture/stored-call-buttons.md`](../architecture/stored-call-buttons.md)
- [`architecture/background-job-direct-chat.md`](../architecture/background-job-direct-chat.md)
- [`architecture/inbound-files.md`](../architecture/inbound-files.md)
- [`architecture/delegation.md`](../architecture/delegation.md)
- [`architecture/persona-lifecycle.md`](../architecture/persona-lifecycle.md)
- [`architecture/callback-delivery.md`](../architecture/callback-delivery.md)
<!-- END SOURCEMAP -->
