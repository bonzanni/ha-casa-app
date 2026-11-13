# Casa

An always-on fleet of AI agents running as a Home Assistant app (formerly add-on), powered by the Claude Agent SDK.

## What it does

Casa runs a fleet of Claude agents inside your Home Assistant instance. They help keep your life manageable — answering questions, keeping track of things, acting on a schedule, and running your house when that is what you need. Home Assistant is where they live; the house is one of the things they look after.

Three long-lived **residents** ship with Casa:

| Resident | Default persona | What it is for |
|---|---|---|
| Assistant | Ellen | The agent you chat with on Telegram: general help, orchestration, delegation, reminders, memory. |
| Butler | Tina | Voice-first house control (lights, climate, locks, media, sensors), in short spoken answers. |
| Concierge | Gary | A medium-trust voice agent for anyone in the room: general questions and delegated lookups, no house control and no private data. |

Two more tiers extend the fleet without changing the residents. **Specialists** are ephemeral agents a resident delegates focused work to — finances, a mailbox, a hobby domain — installed from git repositories. The **configurator** ships with Casa and changes Casa's own configuration for you: ask Ellen, and its answers come to your chat. Specialists, plugins and personas all install the same way, by asking in chat.

A fresh install ships with no specialists installed; see [Installing a specialist from a repository](#installing-a-specialist-from-a-repository).

## Prerequisites

- **Claude Max subscription** with an OAuth token (run `claude setup-token` on your local machine to obtain one)
- **Home Assistant 2025.4+** on an amd64 or aarch64 system
- A Telegram bot token (optional, for Telegram channel)

## Configuration

### Required

| Option | Description |
|--------|-------------|
| `claude_oauth_token` | Your Claude OAuth token from `claude setup-token`. Required. |

### Optional -- Channels

| Option | Description |
|--------|-------------|
| `telegram_bot_token` | Telegram bot token from @BotFather. Enables the Telegram channel. |
| `telegram_chat_id` | Telegram chat ID to restrict messages to. Leave empty to accept all chats. **Setting it is a security control**, not just a filter: this id is the operator identity, so only the sender whose Telegram user id matches it is attributed as the operator and reads memory at private clearance — any other accepted sender is recorded under its own `telegram:<id>` identity and reads at public clearance only, as does any engagement they start. Protected plugin tools (v0.139.0) can only be authorized by the configured operator: a non-operator sender's protected call is denied outright, and with the option empty (accept-all) no sender is the operator, so protected tools are denied for everyone — the add-on logs a warning at startup in that mode. Set this id unless you specifically want an open bot. |
| `telegram_engagement_supergroup_id` | Optional forum group where each agent keeps a read-only log (see [Optional: agent logs](#optional-agent-logs)). A negative integer. `0` = no logs; everything else works without it. |

### Memory

Short-term conversation continuity works via the Claude Agent SDK session,
with one exception worth knowing: changing a resident's identity — its persona,
or the model it runs on — starts that resident's conversations fresh at the
next restart (see *Changing a resident's persona costs you its conversations*,
below).

**Long-term memory is built in** (v0.347.0). There is nothing to install and
no option to set: Casa keeps its own memory store under `/data/memory`.

- **What is said is kept as it is said.** Each turn of a Telegram
  conversation — your messages, Ellen's replies, what she asks a specialist
  and its answer — is saved while the turn runs, so a restart does not lose
  it. Not saved: voice conversations, turns started by a webhook trigger or
  by `/invoke`, and anything a specialist set to keep no memory writes (its
  answers).
- **Memory-enabled conversations start knowing what matters.** In the
  background, Casa files what was said into notes. A new conversation of an
  agent with a memory budget (`memory.token_budget` above 0: Ellen and the
  butler as shipped, and a specialist given one, when Ellen delegates to it)
  starts with them: all of them while they fit that budget, and past that an
  index of them that the agent can open. Within one conversation that view
  does not change. An agent whose budget is 0 (the concierge as shipped), a
  conversation with someone other than you at `public` clearance, and a
  specialist's background job start with no notes.
- **Correct me.** When you tell Ellen that something she remembers is wrong,
  she marks your message as a correction. Her notes are refiled from it in the
  background, and from its next turn every open conversation of a
  memory-enabled agent — a specialist's background job included — is told
  about the correction, where that conversation's clearance may read it (the
  butler is not told a private one).
- **Search, browse and open.** Ellen can search what was said by its words,
  read back a day or a stretch of up to a week ("what did we say last
  Tuesday?"), and open entries of her notes. Each message a search or browse
  returns comes with the day and time it was said, who said it, and whether
  one of Casa's scheduled tasks started that turn. Finding nothing is not
  proof it was never said, and every search or browse result tells the agent
  so.
- **Privacy tiers, and the butler's shared view.** Each saved message is
  classified into a sensitivity tier — `public`, `friends`, `family` or
  `private` — and every read checks it. Your own conversations read
  everything. The butler, which answers on voice, is served notes of its own,
  written only from what was said at `friends` and below, so it knows what a
  guest in the room may hear and never your private matters. Someone else
  messaging the bot reads `public` memory only.

Conversation transcripts on disk are kept for a while after their
conversation ends: an assistant or butler conversation's for 30 days after it
can no longer be resumed, a delegation's for 30 days after its run ended, and
an in-Casa specialist engagement's for 30 days after the
engagement ends. A transcript is deleted only once
every run recorded in its folder is confirmed — that is, once everything Casa
captured from those runs is durably stored — so it can be kept longer, never
shorter.

**Wiping memory.** From the add-on terminal, run `casactl memory-wipe --yes`
(without the flag it refuses and prints what it would erase). Casa records the
request and restarts itself; the wipe runs as Casa starts, before anything else,
and erases everything Casa remembers: saved conversations, its notes, the
search index, the memory exports kept under `/data/memory/exports/`,
conversation transcripts and conversation pointers, so every conversation
starts fresh. A background job that was open cannot pick up where it was — its
conversation is gone — so it ends and you are told. **Your reminders,
triggers, settings and prompts, pending tasks, plugin data and logs stay, and
so do your files** — though a kept item can
still mention something that was forgotten. Anything said between the request and the
restart is erased too. Copies outside Casa (a Home Assistant backup, an export
you copied elsewhere) are not touched. When Casa is back it tells you on
Telegram: "Done. I've forgotten everything. Your reminders, triggers, files and
settings are still here." If the wipe fails while Casa starts, Casa tells you
why on Telegram and takes no messages until a restart lets it finish.

Casa also carries a consent-gated agent door for the same operation — the
`wipe_memory` tool, which posts an Approve/Cancel keyboard (saying what goes,
what stays, and that Casa restarts) to the configured operator's DM and acts
only on the operator's own tap — but **no shipped agent is granted it**, so
asking your assistant to wipe memory will get you a plain "I can't do that, run
`casactl memory-wipe --yes`" rather than a wipe. (If you have granted the tool
by hand in your own agent configuration, the tool's own gates apply: with
`telegram_chat_id` empty nobody is the operator, so the wipe is then denied for
everyone.)

**Exporting and restoring memory.** From the add-on terminal,
`casactl memory-export` writes a verified copy of memory into
`/data/memory/exports/`; Casa also keeps one from each of the last seven
nights there, so a Home Assistant backup carries them, and makes one before it
upgrades the store's format. `casactl memory-restore <export> --yes` replaces
**all** of memory with that export's — including anything said or wiped since
it was made — and Casa restarts to do it; without `--yes` it only checks the
export and tells you what it would do. `<export>` is an export's directory, or
just its name inside `/data/memory/exports/`.

### Optional -- Agents

| Option | Description |
|--------|-------------|
| `primary_agent_model` | Model for the primary agent: `opus` (Claude Opus 5.5), `sonnet` (Claude Sonnet 5.5), or `haiku` (Claude Haiku 5.5). Default: `opus`. |
| `voice_agent_model` | Model for the voice agents (butler and concierge), same choices. Default: `haiku`. |

### Optional -- Features

| Option | Description |
|--------|-------------|
| `enable_terminal` | Enable a web terminal accessible via the ingress panel. Default: `false`. |
| `webhook_secret` | Optional manual override for the HMAC secret that authenticates webhook, invoke and voice requests. Leave empty and Casa generates one in `/data/webhook_secret`, retrievable through the web terminal. Authentication is always on. |
| `engagement_reap_days` | Auto-close background jobs after this many days without activity (a daily sweep cancels them; the assistant is told). Set `0` to disable. Default: `7`. |
| `log_level` | Log verbosity: `debug`, `info`, `warning`, or `error`. Default: `info`. Flip to `debug` for verbose troubleshooting without rebuilding the image. |
| `specialist_max_concurrency` | Max specialist delegations in flight fleet-wide at once (see [Delegation limits](#delegation-limits)). Range 1-20. Default: `2`. |
| `specialist_cost_alert_threshold` | Cumulative per-specialist USD spend past which Casa logs a warning on further delegations (see [Delegation limits](#delegation-limits)). Default: `5.0`. |

## How it works

1. **Startup**: The app validates your OAuth token, copies default agent configs (if first boot), and starts nginx + the Casa core process.
2. **Message flow**: Incoming messages (Telegram, webhook, voice) are routed through an async message bus to the appropriate agent based on the originating channel.
3. **Agent processing**: Each agent builds a system prompt (personality + memory context), queries the Claude Agent SDK, stores the conversation in memory, and sends the response back through the originating channel. In a private Telegram chat you watch the reply to a message you send being written, word by word, with Telegram's **Stop** button under it; the finished reply then arrives as an ordinary message. The bubble and its Stop are there from the moment the agent starts working, empty until the first word, so you can stop a turn before it has written anything; nothing then arrives. Stop ends the reply and keeps what was already written, and the agent remembers it. It stops only the reply: an approval card, a background job or a browsing request the agent had already started keeps running; to stop a job or a browsing request, ask the agent. A reply in a group arrives whole, and so do scheduled messages, reminders and an agent's greeting.
4. **Home Assistant integration**: Agents interact with HA via the official HA MCP server, allowing them to control devices, read states, and create automations.
5. **Per-agent triggers**: Each agent declares scheduled triggers (cron, interval or a one-off date) in its own `agents/<role>/triggers.yaml`. The TriggerRegistry registers them at boot, and fires them via the agent's normal turn loop.
6. **Reminders**: Ellen can set her own reminders, which are ordinary triggers written to her `triggers.yaml` — so they survive restarts and updates. One-off reminders remove themselves after firing, and any reminder whose time fell while Casa was down is delivered on the next sweep rather than lost.
7. **Weekday morning briefing**: Ellen's 08:00 weekday briefing is meant to be silent on most days. It sends a message only for something you must act on today and have not already been told, checked against a live source that morning or marked as unverified. Memory and Casa's own schedule are not treated as confirmation of today's state, and an empty or failed check is not a reason to message you. These are instructions to the model, so an occasional stray line is still possible.
8. **Dated, marked memories**: When Ellen searches or browses memory, each saved message comes with the day, weekday and time it was said, who said it, and whether one of Casa's scheduled tasks (a trigger or a reminder) started that turn. Memory is still history, not confirmation of how things are now: Ellen is told to check changeable state live, or say it is unchecked, before stating it as current.
9. **Step limit**: Each turn has a step limit — one step is one model call, and parallel tool calls in one message count as one. Ellen's is 80; a turn started by an untrusted webhook always gets 20, and the voice agents keep 10 (butler) and 6 (concierge). When a turn reaches its limit, whatever it already said stays, Casa never retries or continues on its own, and Casa adds one line: in a chat, "say 'continue'" picks up where it stopped; a scheduled task, reminder or follow-up to a scheduled question runs on its own, so its line names the task and offers to redo it on request; a trusted `/invoke` gets the line at the end of its response; a voice agent that has already spoken says so aloud; an untrusted webhook turn is only logged. Every stop logs a WARNING with the agent, the channel and the step count. A background job is different: reaching the step limit (a high one, 1000 steps) is never a stop — Casa continues the job when it has reported progress since the last time, and parks it for you otherwise.

### Writing a scheduled trigger prompt by hand

A scheduled turn delivers whatever the agent says at the end of it, unless it
ends with the silence sentinel. So a prompt that tells the agent to send you a
message, and says nothing about how the turn ends, costs you two messages: the
message itself, and then the agent's own closing "Sent." to the same chat. Casa
does not suppress that closing text on your behalf — a scheduled turn that has
something real to say must still be heard — so the prompt is where you say it.
If the turn's last message is the sentinel `<silent/>` after earlier text, Casa
drops that text when the turn made at least one send, every send was confirmed
delivered, and the turn finished on its first attempt with no error and no
retry; otherwise it delivers that text without the sentinel.

For interval/cron/date prompts whose turn delivers its own message, keep
the send instruction first and unconditional, and end the prompt with:
After the send, output the sentinel `<silent/>` and nothing else.

Casa writes that clause into the prompts it generates itself that have this
shape — a reminder's prompt is the plain example — and the Configurator writes
it into the scheduled prompts it authors for you that have it. It is a
convention, not an enforced rule: **nothing rejects a hand-written prompt that
omits the clause** — such a prompt still delivers twice, and the fix is to add
the clause to the prompt.

Which prompts the clause belongs to is decided by where the operator's copy of
the message comes from, never by whether the turn calls a tool: a tool call that
is not a delivery decides nothing here. There are only two answers. Either your
copy arrives from a delivery tool call (`send_message`, `send_media`, or the
question `ask_user` posts), which puts it in the chat by itself and leaves the
turn with nothing left to say — that shape needs the clause. Or it arrives as the turn's
own final text, which Casa delivers when the turn ends — that shape must NOT be
given the clause, because the sentinel would then be the whole final text and
you would get nothing. The shipped heartbeat and morning-briefing triggers are
the second shape: they tell the agent to output only the final message text, and
they name the sentinel only for the case where the turn has nothing worth
saying. A turn that looks something up and then reports what it found is the
second shape too, however many tools it called on the way.

A turn that asks you something with `ask_user` is the first shape: whatever else
it has to tell you, on any branch, it sends with `send_message`. A turn that asks with `ask_user` has put its question in the chat only when the
ask reports that it is awaiting the operator's answer; when it reports anything
else, the turn outputs what the ask reported as its final text instead of the
sentinel.
A turn that sends with `send_message` has put its message in the chat only when
the send reports that the message was sent; when it reports anything else, the
turn outputs what the send reported as its final text instead of the sentinel.

A Home Assistant notification that carries this turn's message to the operator
is a delivery, including when it is reached through the Home Assistant proxy, so
that prompt takes the clause; a Home Assistant read or device action whose result
the turn then reports is not a delivery, because the operator's copy is still the
turn's own final text.

Decide which shape your prompt is before writing it:

```yaml
# shape A — your copy arrives from a DELIVERY tool call: send_message,
# send_media and ask_user are the ones Casa declares, and a Home Assistant
# notification is one too. The turn has nothing left to say.
prompt: >-
  Send this exact message via telegram: "Bins out tonight." After the send,
  output the sentinel `<silent/>` and nothing else.

# shape B — your copy arrives as the turn's OWN final text. No closing clause;
# the sentinel appears only as the way to say nothing at all.
prompt: >-
  Output today's forecast as the final message text, with no preamble. If there
  is nothing worth sending, output the sentinel `<silent/>` and nothing else.
```

Webhook triggers have no prompt at all, so none of this applies to them.

## API endpoints

All endpoints are accessible through the ingress proxy.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/healthz` | Health check |
| `GET` | `/api/voice/agents` | Discover enabled Home Assistant voice residents (authenticated) |
| `POST` | `/webhook/{name}` | Fire-and-forget named webhook |
| `POST` | `/invoke/{agent}` | Synchronous agent invocation (returns response) |
| `POST` | `/telegram/update/{bot_id}` | Telegram updates for one bot, in webhook mode only. Casa registers this URL with Telegram itself (your `public_url` joined with the path), signed with a secret derived for that bot; you never enter it anywhere |

Webhook and invoke endpoints accept JSON bodies (capped at 64 KiB; a plugin
trigger may declare a larger cap, see below). Auth is
always on and **fail-closed**: a webhook trigger whose secret is missing
returns `401` rather than serving open.

**Per-trigger webhook auth.** Each webhook trigger declares an `auth` mode:

- `hmac_body` (default) — `X-Webhook-Signature` = HMAC-SHA256 hex of the body,
  using the global secret.
- `static_header` — a shared secret compared against a request header (default
  `X-API-Key`), for services that can only send static headers. The per-trigger
  secret lives at `/data/webhook_secrets/<trigger-name>`.
- `timestamped_hmac` — a `t=<unix>,v0=<hex>` signature (default header
  `X-Webhook-Signature`) within a tolerance window.

No default header names a particular service. If the caller can only sign under
a header of its own choosing, set the trigger's `header` explicitly — an
explicit value is always used as written.

A Casa-owned per-trigger secret is generated when its trigger is registered — at
startup and on any reload that installs triggers — so it exists before the first
call rather than being created by it, and it lives exactly as long as the trigger's
route: the reload or restart that removes the trigger retires the secret, so a trigger
recreated under a name that was used before is given a fresh secret (hand it to the
caller again). A Casa-owned trigger's route accepts only a secret Casa can prove it
generated for that trigger; a value Casa cannot prove — including a secret generated by
a version of Casa that kept no proof — is refused (`401`) but never deleted: the reload
report shows the trigger as `unproven_blocked`, and the remedy is to delete
`/data/webhook_secrets/<trigger-name>` on the host and reload, then point the caller at
the new secret. If you would
rather supply the value yourself, declare
`secret_owner: provider` on a `timestamped_hmac` trigger and place the file by
hand: Casa never writes a slot it does not own. Set that at creation time —
changing the owner of an existing trigger is not supported, so delete it and
create it under a new name instead.

A turn started by a `/webhook/{name}` trigger is treated as **untrusted
third-party content**: it runs in a restricted runtime (no shell, filesystem,
network, plugins, or hooks — only a memory search at its reduced clearance and an
operator-bound notification), and writes no memory. Operator-signed `/invoke` keeps full trust.

**Who a triggered turn is attributed to (0.129.0).** Both endpoints are opened
by a shared secret, which proves the caller holds a credential — never that a
particular person wrote the request. So neither is ever recorded as you. An
`/invoke` turn is attributed to `invoke_caller`; a webhook turn to
`webhook:<trigger name>`, so two triggers are never mistaken for each other.
Both are recorded as **automations** rather than as people, so nothing either
sends is ever remembered as something you said.
The trigger's own name stays private to Casa. This is separate from the trust
question above: holding the secret decides what the assistant may *disclose*;
it says nothing about who *spoke*.

> Webhook trigger `path` is removed in 0.97.0 — triggers are served at
> `POST /webhook/<name>`.

**Plugin-declared triggers (0.98.0).** A plugin may declare webhook triggers
in its manifest (`casa.triggers`); they are served at
`POST /webhook/plg-<plugin>--<name>` (the `plg-` prefix is reserved for
plugins). Such a trigger routes only after the plugin is installed and
assigned to the target resident, the resident declares the `webhook`
channel, and you approve a one-time consent message in Telegram — until
then the endpoint returns 404 and the reason shows in plugin health. A
plugin update re-asks for consent and rotates the trigger's secret;
`trigger_ack_revoke` switches a plugin's triggers off immediately and
retires their secrets (re-approval mints fresh ones).

**Plugin trigger replies (0.333.0).** A webhook turn's reply goes nowhere
unless the plugin's trigger declares `deliver: operator`. Then each fire's
reply, or the error message if the turn fails, is sent to you on Telegram, and
the approval card tells you so. A reply that is only `<silent/>` sends
nothing. Triggers that do not declare it behave as before.

**Every fire, no silence (0.335.0).** A trigger that declares
`deliver: operator_always` reaches you on every fire. The assistant is not
invited to stay silent, and if its reply is `<silent/>` or empty anyway, Casa
sends one line of its own instead: "The '<webhook>' webhook fired; I had
nothing to add." An error or a step-limit notice counts as that message, so a
fire with no reply still sends you exactly one message. The approval card
says so.

**Provider-generated secrets and larger bodies (0.334.0).** A plugin trigger
using `static_header` or `timestamped_hmac` may declare
`secret_owner: provider` when the calling service generates the signing secret
itself (ElevenLabs post-call webhooks, for example); the plugin must declare a
setup tool. Casa then generates no secret: after you approve, the plugin's
setup tool registers the webhook with
the service and stores the secret the service returns. Until it has, the
endpoint answers `401`. A plugin update, or a revoke followed by a new
approval, clears the stored secret, and the setup tool runs again once you
approve. A plugin trigger may also declare
`max_body_kib` (64–1024, default 64) to accept larger bodies, such as a long
call transcript. The approval card covers both.

The target of `/invoke/{agent}` must declare the `webhook` capability in its `channels:` list to be invoke-reachable; a request for an agent that does not (for example the voice butler, which declares only `ha_voice`) returns `404 {"error": "unknown agent"}` — the same response as for an agent that does not exist, so the endpoint reveals nothing about which agents are configured. The default `assistant` (Ellen) declares `webhook` and stays reachable.

### Invoke example

```bash
curl -X POST http://homeassistant.local:8080/invoke/ellen \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is the temperature in the living room?"}'
```

### Reinstalling Casa changes its container name

Home Assistant derives an installed add-on's container name from **both** the
repository URL and the slug:

```
addon_<sha1(repository_url)[:8]>_<slug>
```

The Docker network alias follows the same pattern (`<hash>-<slug>`). Both
therefore change whenever either input changes — including a reinstall from a
different repository URL. In v0.127.0 the rename moved them from
`addon_c071ea9c_casa-agent` / `c071ea9c-casa-agent` to `addon_91d4d4c8_casa` /
`91d4d4c8-casa`.

Anything outside Home Assistant that reaches Casa **by container name or IP**
breaks at that moment. In a typical setup that is a reverse proxy in front of
the external API port (`18065/tcp`), because that port is not host-published by
default — the proxy reaches the container directly over the `hassio` Docker
network.

The symptom is unhelpful: every external request returns a bare `502 Bad
Gateway` from the proxy. `/healthz`, `/invoke`, the voice endpoints and the
Telegram webhook all fail together, while Casa itself is healthy and its logs
are clean — nothing in them indicates the cause.

After any reinstall or repository change, update whatever points at Casa by
name. To find the current one:

```bash
docker ps --format '{{.Names}}' | grep casa
```

## Agent configuration

Agent YAML files are stored in `/config/agents/`. Default configs are created on first boot and never overwritten. You can edit them freely.

Each agent config supports: `name`, `role`, `model`, `personality`, `tools`, `mcp_server_names`, `memory`, `session`, `channels`, `tts`, `voice_errors`, and `cwd`. See the default `assistant.yaml` for a full example.

### Installing specialists (v0.105.0)

Specialists install from their repository in one flow: ask the assistant to
install one, approve the single consent message, done. If the specialist's
repository bundles plugins (or declares them by repository reference), they are
installed together with it — the approval card describes what the bundled
plugins can do and the secrets they will need (see "Approval cards" below), so
one tap covers the whole package.
Bundle-installed plugins belong to their specialist: they are private to it and
removed automatically when the specialist is uninstalled. Plugins you install
yourself with `plugin_add` are operator-owned — a specialist's lifecycle never
touches them, and plugin management tools refuse to modify a specialist's
bundled plugins (manage those through the specialist's own upgrade/uninstall).

### Agents with their own chat (v0.360.0)

Some specialists talk to you from their own Telegram bot, in a chat of their
own: the browsing specialist and the finance specialist do. A specialist's
repository says so in its `role.yaml` with an `own_chat` block (a short and a
long description for its bot). Every other specialist stays behind the
assistant: you talk to it through her, and it shows you nothing directly — no
cards, buttons, links, files or job lines. When such a specialist has something
for you, it gives it to the assistant as text and she tells you.

**Once: let the assistant's bot manage bots.** In BotFather's Mini App, open
your assistant's bot, go to its Mode Settings and turn on **Bot Management
Mode**. Casa needs this to receive the bots you create for your
specialists. Telegram can take several minutes to report the change; Casa does
not wait for it, and simply tries again later.

**The offer.** When you install a specialist that can have its own chat (or
upgrade to a version that adds one), the assistant offers it: her chat shows a
card, "Alex can get a separate chat with you. Tap below, then Create." Tap the
button: Telegram opens its "Create Bot" form, filled in with the specialist's
name and a username (its handle) the assistant chose — named after the
specialist's role with a few digits, such as `CasaFinance4821Bot`, following
the pattern of your other specialists' handles if you have one, and checked
on Telegram before she shows it. Tap **Create**. You need not discuss the
handle, but you can: ask for a different one and she offers it instead. You
can also ask for the chat later ("give Alex his own chat"). Telegram's form
still lets you edit the username, but a bot created under a username the
assistant did not offer is not recognised: her chat asks which specialist the
new bot is for; answer "None — I'll delete it" to keep it out of Casa.

**Open and say hi.** Casa sets the bot up (the specialist's name, its
description, a `/new` command, the specialist's picture when its persona has
one, and access limited to you) and the card changes to "Alex's chat is
ready: @…. Open it and say hi." Once the bot is ready, the specialist says
hello by itself — if you pressed **Start** in the new bot's chat when you
created it. Telegram lets a bot write to you only after that; if it does not
let the specialist yet, tap **Open Alex's chat** and say hi (any message
works): the chat then opens with your first message, and whatever you write
first, the specialist says hello as it answers. A file sent on its own,
without a caption, is kept and acknowledged but does not open the chat yet. Until then the specialist cannot show you
anything. Messages you send while the chat is being set up are kept and
answered once it is ready.

**Changing a specialist's handle.** Ask the assistant ("I'd like Alex on a
different handle"). The change waits until nothing is open in the
specialist's current chat — a running task or job, a card or question
awaiting your answer, a scheduled question — and she tells you what is
still open if something is. Otherwise she settles the new handle with you
and posts a card, "Alex can move to a new chat with you, replacing @….";
tap it, then **Create**. Casa restarts for a moment to move the specialist
to the new bot. The specialist greets you in the new chat by itself when
Telegram lets it (otherwise open the new chat and say hi) and tells you it is
its new chat, and the assistant's chat gets a new message, "Alex has moved
to @…. You can delete @… now." with a button that opens BotFather's delete flow, where you pick
the old bot. The old chat keeps its history, but its buttons stop working,
and messages sent to either bot around the move may be lost. Changed your
mind before tapping Create? Ask the assistant to drop the change. If
something opened in the old chat in the seconds before the restart, the
move waits and the card says so; ask again once it is done, and the move
finishes with another short restart.

**Going back to an earlier handle.** Casa remembers the bots a specialist
moved away from. Ask the assistant to move it back ("put Alex back on
@…"): if you have not deleted that bot, there is nothing to create or tap —
Casa restarts for a moment and the specialist is back in that chat. If the
bot was deleted in BotFather, the assistant tells you, and can offer a new
bot under that handle if Telegram has freed it. Bots a specialist left before
v0.365.0 are not remembered.

**Renaming a specialist's bot.** Ask the assistant for a different name
("call Alex's bot Alex the Accountant"). The name at the top of the chat
changes right away; the handle, the chat and its history stay as they are.
Casa's own cards keep using the specialist's name.

**What lives where.**

- **The specialist's own chat** holds your conversation with it — write to it
  there directly, and `/new` starts a fresh conversation with that specialist
  only — and everything it shows you: cards and their buttons, links you must
  open, reports, files, its job lines and run cards. To stop a job of yours or
  its browsing, ask it there, as you would ask the assistant. Its messages
  there carry no `📊` label: the chat is the specialist.
- **Work the assistant hands over** also appears there: a line "Ellen asked
  me: …" when it starts (her request as she wrote it; work Casa itself starts,
  such as a plugin's setup after an update, shows no such line), then the
  specialist's closing message when it ends (its working notes stay out of
  the chat, and a specialist with nothing more to say posts nothing). The assistant still gets the result and closes the
  loop in her own chat, briefly when you have already read the answer. If you
  reply to the task's messages while it works, your words reach it, and the
  assistant is told what you wrote and that the specialist already has it.
  The specialist's own conversation learns in one line that it did this work.
- **The assistant's chat** keeps your conversation with her, the offer cards,
  and her own plugins' posts. A button on a specialist's card from before its
  chat existed answers "This card belongs to Alex's chat now — open it there."
  and does nothing else.

**Replying to a task's post.** While a piece of work is running, swipe-reply on
any message it posted in the specialist's chat — its start line, a page of its
answer, a card — and your words go into that running work, not into the
conversation. A file sent as such a reply, or after tapping 📎 on its card, goes
to that work too: it is stored in the specialist's inbox and that work is
given that one file to read. Once the work has ended, a reply is an ordinary message to the
specialist, told which ended work you were answering.

**If a specialist is uninstalled** its bot goes quiet: a message there gets one
line saying the specialist isn't installed. Reinstall it and the same bot comes
back. To get rid of the bot, delete it in BotFather
(`https://t.me/BotFather?start=deletebot`); the uninstall result names the bot
and that link. When Casa next checks the bot (at the latest at its next
restart) it notices the deletion, and the specialist waits for a new bot: ask
the assistant for a new chat.

Not backward compatible: a specialist's plugin that posts to you now needs that
specialist's own chat to be open.

**Changing the assistant's own bot.** This is a manual procedure. The
assistant's bot is the one whose token is in `telegram_bot_token`, and it
manages every specialist's bot. Telegram documents no way to move managed
bots to another manager, so after a change the old specialist bots keep
working on the tokens Casa stored, but Casa can no longer recover their
tokens if they change. To change it:

1. Create the new bot in BotFather.
2. In BotFather's Mini App, turn on **Bot Management Mode** for it.
3. Put its token in `telegram_bot_token`.
4. If you use agent logs, add the new bot to the agent logs group as an admin
   with **Manage topics** (see [Optional: agent logs](#optional-agent-logs)).
5. Restart Casa, then open the new bot's chat and say hi, so it can write to
   you.
6. Ask the new assistant to give each specialist a new handle (above), then
   delete the old bots, the assistant's old bot included, in BotFather.

### Guests: asking a specialist in a group (v0.370.0)

A specialist with its own chat can be asked in a chat its bot is not a member
of, such as a group with friends: mention its bot (`@…`), or reply to its
earlier answer there.

**Once per specialist: turn on Guest Mode.** In BotFather's Mini App, open the
specialist's bot and turn on **Guest Mode**; it is off by default, and Telegram
passes the bot nothing from other chats without it. Telegram can take a while
to report the change; Casa does not wait for it.

**Your own questions work in any chat.** The specialist answers right there: a
message "**Alex** · thinking…" appears and becomes its answer. It is the same
conversation as in its own chat, so it remembers, and you can carry on in
either place. Only its text answer is shown in the group: cards, buttons,
questions and files still come to your chat with the specialist. When it needs
to ask you something, the group's message says "I've asked you something in
our chat." and it asks you there.

**Letting everyone in a group ask.** In that group, tell the specialist so:
"@… you can answer everyone here" (or "stop answering everyone here"). The
group is remembered for that specialist; one installed in its place starts
without it.

**What your friends get (v0.372.0).** In a group you allowed, anyone can
mention the specialist or reply to its answer, and it answers there in one
short message, in its own voice. It can use only the tools its plugin's
author opened to guests — nothing of Casa's, no memory of you, no history of
earlier questions, no files — and it is told the person asking is not you.
What friends say is not remembered. A bot answers at most three friends'
questions at a time; the next one gets "Busy right now — ask me again in a
moment." Elsewhere, and in groups you did not allow, other people's mentions
stay unanswered.

**Seeing and closing groups from the assistant's chat.** Ask the assistant
"who may ask my specialists?" for every group a specialist answers in, with
whether Telegram reports Guest Mode on for that bot. To close one without
going to the group, say so: "stop the judge answering in «Game Night»".

**For plugin authors.** A tool is offered to friends only when its entry in
`casa.resultContract.tools` says `"guest": true`, for example
`"lookup_rule": {"result": "safe", "guest": true}`. Casa refuses the flag on a
`capability` tool, the setup tool, a tool with `callsCasa` and a protected
tool. To give the specialist instructions for these public answers, add a
`## Guest projection` section to its doctrine where the `## Text projection`
heading stands; a guest answer gets the core doctrine and that section only.

### Background jobs (v0.321.0)

Some plugins declare long work — classifying a year of transactions, say — as a
background job. Ask the assistant for it as usual: she starts the job and replies
straight away. The job runs as one long session: it keeps working until it is
finished. Everything that needs you happens in your DM; if you set up a log group,
you can also watch the job work there (see [Optional: agent logs](#optional-agent-logs)).
A job you asked for puts a 👍 on
your message that asked for it (v0.373.0); if Telegram does not take the 👍, you get one
silent line instead — "📊 Finance started «…»." When a specialist's job
needs a decision from you it asks in your DM, with a button per choice: tap one,
or reply to the question in words. To steer a running job, reply in your DM to any
of its messages there (a question, something its plugin posted, its start line if it
has one); your words reach the job while it works, at its next step. To stop it, ask
the assistant to cancel it. When the job ends you get one message: the answer, as a
reply to your request, opening "Done." (or "Stopped.", or "Couldn't finish:" and
why), with how long the job ran and what it cost. If the job ended before it read your
message, Casa answers it once: "The job had just finished, so your message didn't
get through. Could you send it again?" A reply to a message of a job that has already ended goes to the assistant,
with a note saying which job it was and how and when it ended.

If a job stops without finishing, Casa nudges it once to carry on. If it stops
again, or hits trouble (repeated errors from Claude, or a very long run with no
progress reported), Casa parks it: it waits for you, and you get one line in your
DM — "«…» stopped without finishing. Reply to continue it, or Cancel." — with a
**Cancel** button. Reply to that line to continue the job. Casa never ends a job
on its own because it is slow or expensive; the cost is shown, never capped.

When the job ends its DM buttons go away. A specialist runs one job at a time, but quick questions to it
still work while a job is open. A job that is not working and has heard nothing from
you for `engagement_reap_days` is cancelled. After a restart an open job continues where it was; a message you
sent that it had not read yet reaches it after the restart, marked as sent before
the restart. If the job's own process stops unexpectedly, Casa restarts it once the
same way; if that fails, or it stops again, the job ends and you are told.

Jobs come from two places (v0.323.0). A specialist's plugins run their jobs on the
specialist, as above. Plugins you installed on a resident directly run
theirs as a worker of their own, with nothing but that plugin's own tools — so the
resident stays free for you meanwhile; its log is named after the resident
("Ellen · plugins"). Such a
worker does not ask questions: it sets aside what needs an answer and reports it, and
you answer by replying to its messages in your DM. One job
runs per installed plugin.

A plugin can mark a job quiet when scheduled (v0.344.36). A run of it that a
schedule started posts nothing in your DM and tells you nothing when it completes; the plugin's own messages still reach your DM, so a run
that finds something new can still say so. An approval it needs still reaches your
DM, and a run that is cancelled or fails is still reported. A quiet run that asks you
something, or that you answer — a reply to one of its messages, a tap on its question
— stops being quiet, and its end is then reported like any job's. A quiet run puts
nothing in your DM when it starts: to stop it, ask the assistant to stop it. Runs you or the
assistant start are unchanged.

A specialist can also start its own plugins' jobs itself (v0.344.0). When you reply
to one of its posts, or the assistant passes it your request, and the work is a job
one of its own plugins declares, it starts that job on itself — never another
agent's job — and the assistant still tells you how it ended. Asked in text for such
a job, the assistant passes the request to that specialist rather than starting it
herself; she starts it only if the specialist reports it could not. Jobs do not
start from a voice call: ask in text. A button on a specialist's proposal never starts
a job: ask for the work in words. When the specialist has already posted its answer
to your chat, the assistant does not repeat it.

When the assistant passed the request on and the specialist started the job,
the specialist hears the job's result first (v0.366.0). It checks what is still
open, can finish the work itself — asking another specialist for what it cannot
do, then starting the next job if needed — and only then is the assistant told,
with the specialist's answer in place of the bare result. If that follow-up
cannot run, you still get the job's result, with a line saying the follow-up did
not run. A job started in the specialist's own chat ends as before. While a
specialist works on a task you started this way, its request to the browsing
specialist starts without a start card: the status card still appears (to stop the
browsing, ask), and the card and the browsing specialist's progress messages arrive without
a notification. Links, files, questions and cards that need you still notify,
and every step that changes, pays or sends something is still asked.

**A scheduled job (v0.343.0).** A schedule can start a plugin job directly instead of
asking the assistant to. In the assistant's `triggers.yaml`, a `cron`, `interval` or
`date` entry carries `job: <plugin>:<job>` in place of a prompt, with an optional `task`
and `context`:

```yaml
- name: monthly-books
  type: cron
  schedule: "0 7 1 * *"
  channel: telegram
  job: finance-plugin:month-close
  task: Close last month's books.
```

When it fires the job simply starts, as if you had asked for it: no assistant turn runs
first. The job runs with your own access, as usual. A job entry
must use `channel: telegram` and may not carry `clearance`. The Configurator refuses a job
the assistant cannot start. If the job cannot start when its time comes — its plugin was
unassigned, the same job is still running, Casa was at its work limit — you get one
labelled line saying it did not start and why, and it tries again at the next occurrence.

### Delegation limits

Every `delegate_to_agent` call, and every specialist's background job, is
bounded by two app options:

- `specialist_max_concurrency` (default `2`) — the total number of
  specialist delegations allowed in flight fleet-wide at once. Once the cap
  is reached, further delegation attempts are denied as "busy" until a slot
  frees up. A given calling scope (one voice session or chat) may also only
  have one active delegation to the same specialist at a time — that
  per-scope cap is fixed at 1 and is not configurable. When a specialist
  hands part of its work to another specialist, the hand-off runs inside
  the delegation that started it: a chain of hand-offs counts once against
  the fleet-wide cap, while the per-scope rule still applies to each
  specialist in it. A hand-off a specialist makes from its own chat has no
  delegation above it and counts as one.
- `specialist_cost_alert_threshold` (default `5.0`, in USD) — once a
  specialist's cumulative delegated spend passes this figure, every further
  delegation to it logs an operator-visible warning (spend is not blocked,
  only flagged).

### A script over a specialist's notes

A specialist or a job keeps notes in a scratchpad for the conversation. When it has put a
page or an export there and needs to compute from it — a paste for a site's bulk field, a
total, a filtered list — it can run a short Python script over its notes: the script sees
the pad's sections and nothing else, in a separate process that has no network, no files,
no other process, 30 seconds and 512 MiB, and its result is written back to the pad all at
once, only when the script finishes cleanly. The box is enforced by the app's AppArmor
profile, so it works on x86_64 installs only; elsewhere, and whenever the profile has not
loaded (after an update, the app must be updated, not just restarted, for the profile to
load), the operation is unavailable and Casa tells you once, with the reason in its log.

### Sending files to the assistant (v0.325.0)

Send the assistant a PDF, a photo, or a text or CSV file in your direct chat and she
keeps it: Casa puts a 👍 on the file to say it is saved, and keeps it for seven days.
Nothing is read on arrival. Ask about the file whenever you like ("what does that
invoice say?") and she opens it then; if several files could match, she asks which.

**A caption is an instruction (v0.344.65).** Write a caption on the file — "add it to the
invoices", "what's the total?" — and she acts on it straight away, exactly as if you had
sent the caption as a message about that file: Casa keeps the file and gives it a 👍 as
usual, and her reply is the answer. If she cannot take it up right then, Casa says in words
what it saved instead. She decides what the caption asks for; Casa does not
guess from its words. In an album the caption is about the whole album: she waits for every
file to arrive and then acts on all of them in one reply.

**Several files at once (v0.344.68).** Each file you send gets its own 👍; an album gets one
👍 for the whole album. A file Casa could not keep gets a short line instead, and files sent
together — an album, or several files within a few seconds — share one such message. If
Telegram does not take the 👍, Casa says what it saved in words, as before ("Got 3 files: …").

Files can be up to 8 MB, and she holds up to 50 files and 200 MB in all. When that is
full a new file is refused rather than an old one dropped — the oldest go after seven days.
Anything else you send, such as a sticker, a voice message, a `.zip` or a `.docx`, gets
a short reply saying what she can take. Files work only in the direct chat, not in
the agent-log group, and only the assistant can read them — except a file you send to a
specialist on purpose, which goes to that specialist instead (see *A file for a specialist*
below).

Plugins pass files to one another through a folder Casa manages, where each file is kept
seven days and then removed; when the folder is full, a new file is refused rather than an
old one dropped. If a plugin needs a file you sent in Telegram — to attach it to an email,
say — the assistant can hand it a copy without asking you first; the copy is kept seven
days and her own is unchanged.

Reading a document puts its pages in front of the model, so a long PDF costs more to
read than a short one — roughly a few thousand tokens per page.

**She says so when she answered without opening a file (v0.327.0).** If she lists the
files you sent and opens none, everything she sends in that turn — the reply, a message
she posts, a question with buttons — starts with a line from Casa, *(Ellen hasn't opened
“invoice.pdf”.)* (with your assistant's name). The line is on
the text as it streams and goes away if she opens the file before finishing. A reminder
she sets, or a task she hands to another agent, without opening the file carries the line
with it, so the message that fires later is prefixed *(Ellen wrote this without
opening “invoice.pdf”.)* Opening any one of the listed files — or handing one to a plugin,
for example to file it — clears it for the rest of the turn. With several files listed and
none opened, the line counts them: *(Ellen hasn't opened any of the 3
files Ellen listed.)* A file you send with a caption is that turn's own file, and the line
names only it; a captioned album makes every
kept file of the album the turn's own, and the line counts those. The line never holds back or changes her own words — it
is added, nothing is taken away. A reply about a file she neither listed nor tried to open carries no line;
that case is still down to her instructions.

## Voice pipeline

Casa exposes two transports for Home Assistant voice / generic voice clients. The HA-side integration that consumes them ships separately in `ha-casa-integration`.

> **Voice is authenticated.** Both transports below are fail-closed: without a
> webhook secret every voice request is rejected with `401` — the same
> treatment as `/invoke` and `/telegram/update/<bot id>`. This matters because the
> external API port can be published, and an unsigned voice turn reaches an
> agent that can drive Home Assistant. Casa always has a secret, and the
> companion integration signs every request.

- `POST /api/converse` — Server-Sent Events, per-request. Body:
  `{"prompt", "agent_role", "scope_id", "context"}`. Stream: `event:
  block` frames then `event: done`. HMAC via `X-Webhook-Signature`
  (same scheme as `/invoke`).
- `/api/converse/ws` — persistent WebSocket. Conversation frames include
  inbound `stt_start`, `utterance`, `stage`, and `cancel`, with outbound
  `block`, `done`, and `error`. A Home Assistant integration may also register
  an authenticated protocol-3 delivery route. Concierge specialist handoff
  requires that exact socket to acknowledge the complete coordinated capability
  set: `background_jobs`, `endpoint_delivery`, and `voice_handoff`. Missing
  protocol 3 or any one capability fails closed before Casa creates a job;
  there is no legacy handoff fallback.

> **Block frames concatenate.** A turn's `block` frames are consecutive slices
> of one reply, and each frame's `text` carries the whitespace that separated it
> from the previous block — so joining every `block` of a turn, in order and
> with nothing in between, reproduces the reply exactly. A client that speaks
> blocks one at a time should strip each frame's text; a client that displays or
> re-assembles the turn must not insert its own separator, because a block cut
> at the length limit can land mid-word and no separator is invented there.

### Voice-agent discovery

The companion Home Assistant integration discovers enabled residents through
`GET /api/voice/agents`. The response contains `schema_version` 1 and only each
enabled `ha_voice` resident's stable role and display name. The request is
signed over an empty body with `X-Webhook-Signature`. Casa signs with the
optional `webhook_secret` override or the generated secret stored in
`/data/webhook_secret`. Discovery **and both voice transports** return the same
generic `401` response for a missing or invalid signature — voice is
fail-closed, so unsigned turns are never accepted.

For Supervisor-based setup, Casa also publishes the authenticated endpoint to
the companion integration through the `casa` discovery service. This requires
Home Assistant Supervisor support; Casa obtains its
runtime hostname from Supervisor and refreshes the registration if the secret
changes. The only local registration state is the returned UUID in
`/data/casa-supervisor-discovery.json`; the webhook secret is never stored in
that file or logged by the publisher.

The catalog path is fixed, is mounted only while at least one voice transport
is enabled, and returns `Cache-Control: no-store`. It does not expose prompts,
tools, delegates, specialist configuration, secrets, or other private agent
settings. Voice request rate limits are isolated by agent role and scope, so
activity through one resident cannot consume another resident's allowance even
when both requests use the same scope identifier.

Toggle the transports via environment variables on the app:

| Variable | Default | Purpose |
|---|---|---|
| `VOICE_SSE_ENABLED` | `true` | Enable `POST /api/converse` |
| `VOICE_WS_ENABLED`  | `true` | Enable `/api/converse/ws` |
| `VOICE_SSE_PATH`    | `/api/converse` | Override SSE path |
| `VOICE_WS_PATH`     | `/api/converse/ws` | Override WS path |
| `VOICE_IDLE_TIMEOUT_SECONDS` | (butler.session.idle_timeout, 300) | Session pool eviction timeout |

A synchronous specialist delegation started mid-voice-turn (`delegate_to_agent`
from the voice butler) is bounded by a fixed 27-second turn budget, derived
from the voice transport's own 30s timeout so a turn always has room to return
before that timeout fires.

### Background specialist voice jobs

Concierge (Gary) can acknowledge a specialist hand-off quickly, end the current
voice turn, and keep its own resident context free of the result. Speak the
specialist request, hear the acknowledgement, then continue speaking or cancel
normally; Casa delivers the completed result when the originating satellite is
idle. The specialist runs in an isolated session. When it finishes, Casa passes
only a policy-approved spoken summary to the Home Assistant companion
integration; full output, citations, and private detail are never injected back
into Gary's SDK session. Butler (Tina) keeps its direct, immediate home-control
behaviour and does not use this asynchronous specialist handoff.

Where the answer arrives is decided when the question is asked, from the asking
device itself. A device with an announce-capable Assist satellite is answered
aloud on it. A device with only a Companion notification entity — a phone or a
tablet — is sent the answer as a notification instead. A device with neither is
not offered a deferred answer at all: Gary says up front that he cannot follow
up, rather than promising an answer that cannot be delivered. The
acknowledgement is worded to match, so "I'll read it out when it lands" is only
ever said where the answer can be read out.

Home Assistant announces a queued summary immediately when the originating
satellite is already idle. If it is listening, processing, or responding, the
integration waits until that interaction finishes and the satellite reaches
stable idle. Results are FIFO per device; other devices continue independently.
The user can keep speaking to Gary or Tina while a specialist works.

The voice job status surface reports execution (`pending`, `running`,
`succeeded`, `failed`, or `cancelled`) separately from delivery (`ready`,
`waiting_for_route`, `claimed`, `authorized`, `playing`, `delivered`,
`cancelled`, or `expired`). Gary can check status, cancel work before playback,
continue one unambiguous clarification, or request available detail. Private
results normally announce only that a result is ready; an explicit detail
request is still identity/clearance checked and does not add result tokens to
Gary's context.

Background handoff requires a currently or recently registered,
HMAC-authenticated protocol-3 WebSocket route with all three coordinated
capabilities: `background_jobs`, `endpoint_delivery`, and `voice_handoff`.
SSE supports synchronous turns but never handoff; an older or incomplete
WebSocket registration is refused before job creation rather than falling back.
Delivery is intentionally at least once: if audio succeeds but its
acknowledgement is lost, a concise summary can repeat after restart rather than
disappear.

The HMAC signature over the empty HTTP upgrade request body authenticates the
Home Assistant client only when the WebSocket is established. It does not
authenticate individual WebSocket frames, does not encrypt payloads, and does
not cryptographically authenticate the server. The Casa-to-Home Assistant link
is therefore plaintext and must remain on a trusted LAN/private network or
travel through an encrypted, server-authenticated tunnel or reverse proxy.

Tina uses an eager, role-scoped Home Assistant facade: Casa discovers the
Assist tools at boot, keeps that upstream connection resident, and gives only
Tina the ready-to-call proxy surface. If the eager facade cannot initialize,
Casa still boots in a degraded raw-fallback mode; other agents' raw Home
Assistant access is unchanged.

Per-agent voice config (`butler.yaml`):

```yaml
tts:
  tag_dialect: square_brackets   # square_brackets | parens | none

voice_errors:
  timeout:       "[apologetic] Hm, that took too long. Try again?"
  rate_limit:    "[flat] My brain is busy — give me a minute."
  sdk_error:     "[apologetic] I couldn't reach my brain. Try again?"
  memory_error:  ""                    # silent degrade
  channel_error: "[flat] Something went wrong sending that."
  unknown:       "[flat] Sorry, something went wrong."
  empty_turn:    "[apologetic] Sorry, I lost my train of thought — could you ask that again?"
  turn_limit:    "[flat] I ran out of steps — ask me to continue."   # after partial speech
```

`tag_dialect` selects how inline emotion tags (`[confident]`, `[warm]`,
etc.) are rendered before Casa hands text off to HA's TTS. Use
`parens` for engines that expect `(tag)` and `none` to strip tags
entirely for plain-TTS providers like Piper. Voice and engine
selection itself is Home Assistant pipeline config — Casa does not
override it.

## Web terminal

When `enable_terminal` is enabled, a web terminal is available at the `/terminal/` path in the ingress panel. This gives you shell access to the app container for debugging and manual operations.

The terminal is an unauthenticated **root** shell, so it is reachable only through the Home Assistant ingress panel — it is bound to an internal socket that nothing else in the container can reach, never a network port. Leave `enable_terminal` off unless you are actively debugging.

## Troubleshooting

- **App won't start**: Check the log for "claude_oauth_token is required". You must set the token before starting.
- **No Telegram messages**: Verify `telegram_bot_token` and `telegram_chat_id` are correct. The bot must have been started (`/start` in Telegram).
- **No agent logs**: see [Optional: agent logs](#optional-agent-logs). Background jobs work without them.
- **Old Telegram records ignored after an upgrade**: Casa stores every Telegram message it keeps
  (a job's posts and buttons, a pending scheduled question, a plugin's delivery records) together
  with the bot that sent or received it, and it does not convert records an older version wrote
  without the bot. This is not backward compatible: at startup each such record is logged as
  refused and skipped; a pending scheduled question written that way, for example, is not
  restored.
- **Ellen doesn't tell you a job ended**: how she narrates a job's result comes from
  the prompt she is actually served, compiled from her persona and her role — not from
  any file in her agent folder. Editing `prompts/system.md` there does nothing (Casa
  refuses the edit through its file tools); how loudly she narrates is part of her
  persona.
- **502 errors on ingress**: The Python process may still be starting. Wait up to 60 seconds after app start.

## DM button questions (v0.76.0)

Ellen (and the specialists she delegates to) can pause a turn to ask you a
quick multiple-choice question, posted as inline buttons right in your 1:1
Telegram DM — the same tap-to-answer pattern a background job's questions
use. Short answer choices appear on the buttons in their own
words. Longer ones are spelled out (numbered) in the message itself, and the
buttons carry a few words for each ("1 · Plumber today"), written by the
agent; only when it gives none does a button read "Option 1", "Option 2", ….
Long options are always readable and never truncated to the point of being
unpickable. Tap an option and the agent picks up from there; a plain-text
reply in the same DM answers it too. An unanswered question expires after a
few minutes, and starting a fresh session (`/new`) cancels any question
still pending.

Since v0.206.0 a turn fired by one of an agent's own schedules can ask too —
the weekly pass that sends you an invoice can ask *Confirm / Wrong / Later*
about it. A scheduled question yields to you in both directions: it is not
asked at all while you already have a question or an approval waiting, and an
approval request raised later retires it. It is recorded on disk, so its
buttons still work after a restart; if it expires, is superseded, or the
schedule that asked it is edited away, the agent is told what happened instead
of waiting on an answer that will never arrive. Your tap is reported to the
agent as part of that scheduled run, not as a message from you.

## Approval cards (v0.363.0)

Every approval Casa asks you for — a protected plugin tool call, installing or
updating a specialist or persona, a plugin's webhook, callback or event, and
the erase question when a plugin is uninstalled — arrives as the same short
card: a bold question saying what is being allowed and by whom ("🔐 Allow
Ellen to send an email to Marco?"), a one- or two-sentence summary, and a few
labelled lines with the details worth checking. At most two lines carry a ⚠️,
for what you should not miss: something unexpected, unusually risky, or a wipe
that cannot be undone.

A separate model writes the card from the request's own facts, with no access
to the conversation and no tools, in plain words: addresses, amounts, dates,
file names and versions are shown exactly, technical names are translated. It
is told to include every recipient, amount, deletion, granted power and secret;
like any model it can occasionally leave a detail out. If it cannot write the
card in time (usually 15–20 seconds, at most about 25), Casa posts its own plain card instead: one line per
argument for a tool call, or the full consent text for an install, webhook,
callback or event.

The card no longer shows the raw request (the arguments as JSON, the tool's
internal id, install digests). What you approve is still bound to exactly the
request that was made: approving lets that one call, or that one install, go
ahead and nothing else. Nothing written inside a request — an email subject, a
plugin's description — can turn into a link or formatting on the card.

## Optional: agent logs

If you want to watch your agents work, Casa can keep a read-only log for each of
them in a Telegram forum group: one topic per specialist, one for the configurator,
and one per resident that runs plugin jobs ("Ellen · plugins"). Each line names the
run it belongs to — what it says, each step it takes, how it ended:

```
Q3 accounting · ▶ started
Q3 accounting · 🔧 Searching memory
Q3 accounting · 📊 Classified 25 · 25 done · 16 left
Q3 accounting · ✅ completed — Tagged 41 rows; 2 need your decision.
```

Everything that needs you stays in your direct chat: a job's questions, approvals,
its result. Reply to a job's message
there to steer it. Nothing you write in the log group reaches an agent: anyone who
writes there gets one pointer back to the direct chat.

To set it up:

1. Create a Telegram group, turn **Topics** on in its settings (Telegram makes it a
   supergroup).
2. Add your Casa bot and make it an admin with **Manage topics**.
3. Find the group's chat ID — a negative number starting with `-100`. Post a message
   in the group and open
   `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`; it is `message.chat.id`.
4. Set `telegram_engagement_supergroup_id` to that number and restart the app.

Casa creates the topics as agents start working. Each post disappears about two days
after it was sent. The logs are visible to every member of the group, and an agent's
words can mention private things, so choose who is in it. Leave the option at `0` and
there are no logs; everything else works the same.

If no logs appear, check the app log for `agent log: logging is off:` — it says why
(most often, the bot lacks **Manage topics**). Casa checks again every half hour.

## Configurator (v0.12.0)

The `configurator` knows Casa's configuration surface and changes it on your behalf. It ships with Casa (you cannot edit it in `/config`) and needs no log group: it works in your 1:1 chat with Ellen.

### What's supported

| Surface | Create | Read | Update | Delete |
|---|---|---|---|---|
| Specialist agents (Tier 2) | yes | yes | yes | yes |
| Specialists from a repository (install/upgrade/rollback/uninstall) | yes | yes | yes | yes |
| Resident agents (Tier 1) | rare | yes | yes | blocked by default |
| Per-agent YAMLs | yes | yes | yes | yes |
| Per-agent prompts (per-trigger prompts) | yes | yes | yes | yes |
| A resident's `prompts/system.md` | - | yes | **no** (not served; a file-tool edit is refused) | - |
| Triggers (cron, interval, webhook) | yes | yes | yes | yes |
| Delegate wiring | yes | yes | yes | yes |
| Policies (disclosure) | - | yes | yes (but reaches no served prompt today) | - |
| Plugins (registry + store) | yes | yes | yes | yes |

Plugin management uses the registry tools (`plugin_add`, `plugin_update`,
`plugin_assign`, `plugin_unassign`, `plugin_remove`, `plugin_list`,
`verify_plugin_state`) — see [Plugins](#plugins-v0710). Asking the assistant
why a plugin is not working needs none of them: `plugin_status` (0.197.0) is
read-only, granted to the assistant, and reports both what is currently
blocking each plugin and what a past automatic setup last failed on.
Repository-installed
specialists use their own recipe — see
[Installing a specialist from a repository](#installing-a-specialist-from-a-repository).

Not yet supported:

- Eval running (no recipe).

### Invocation

Ask Ellen for a configuration change in 1:1 chat. Examples:

- "Make a new specialist called fitness using sonnet"
- "Add a morning briefing trigger to yourself at 7am on weekdays"
- "Change Alex's prompt to be more concise"
- "Remove the garbage_reminder trigger"
- "Wire Alex into your delegates"

> **Reminders do not need the configurator.** Just ask Ellen directly — "remind me tomorrow before 9am to put the bins out", "remind me every Thursday at 7 to go to the gym" — and she sets it herself. She confirms the exact time, asks whether it should repeat when that is unclear, and can cancel it later.

Ellen passes your request on. The configurator works by itself — a line in your chat
shows its steps while it works and goes away when it is done — and Ellen tells you its
answer.

- **It asks you something?** Ellen tells you; answer her in her chat — a reply there is
  hers too — and she passes your answer on.
- **An approval card** (installing or updating a specialist, or installing a persona): the
  card describes the settings the install will end up with and the secrets provided (by
  name, never their values); Casa's own fallback card, shown when the card cannot be
  written, lists every setting, marking the ones kept from the current install. Tap Approve
  and Casa carries the configurator on with exactly those settings. One tap is one attempt:
  the approval is used up when the work starts, so if the work fails, or you want the same
  update again after a rollback, you get a new card. If the configurator could not be
  started, the card says so and the approval is kept: ask Ellen again and it carries on
  without a new card. A newer request replaces the older card. Deny or letting it expire
  only updates the card; ask again for a fresh one.
- **Nothing to change?** Asking to install a specialist that is already installed, or to
  update one to the version it already has with the same settings and no secrets involved,
  changes nothing: you are told it is already done, and there is no card to tap.
- **Applying a persona** to an installed specialist needs no tap.
- **It needs a Casa restart?** Its answer is followed by a **Restart now** button. Casa
  restarts when you tap it, never in the middle of the work; the button works once and
  expires after a day.
- **One task at a time.** A second request while it works waits up to a minute, then you
  are told `Configurator is busy with «…»; try again shortly.`
- **Casa restarted while it was working?** You get one message naming the task: nothing
  was replayed, so ask again if it is still needed.
- Install time (downloading and setting up a plugin or specialist) does not count toward
  the ten-minute limit of a run.

### Reload behavior

Every commit the configurator makes reloads what it changed, in-process and
without a restart, so each change is live as soon as it is committed — a task
that commits twice reloads twice, and a task you cancel keeps whatever it had
already committed. The reload is as narrow as the change: one agent's triggers,
one agent, the specialists or the policies; a commit that changes
more than one of those reloads everything. If a reload fails,
the configurator tells you what is not live yet.

- How a resident writes, speaks or behaves is not a config edit at all — that
  comes from its persona and from the role doctrine Casa ships.
- A resident's new persona waits for an app restart: the **Restart now** button.

### Recipe discovery

The configurator reads short markdown recipes from its doctrine, which ships with Casa (`/opt/casa/defaults/configurator/doctrine/`) and changes only with a release.

### Troubleshooting

- **"Configurator is busy"** - another configuration task is running; ask again when its answer arrives.
- **Hook denied** - expected for resident deletion and other protected changes; the configurator says what it could not do. Its hooks ship with Casa and cannot be overridden in `/config`.
- **Doctrine references stale fields** - file an issue; maintainer forgot to sync doctrine with Casa-core change.

## Plugins (v0.71.0)

Casa loads Claude Code plugins for every agent tier from **one registry**. There
is no marketplace to browse and no per-agent install step — you pin a plugin
once, assign it to the agents that should have it, and Casa materializes an
immutable copy that every tier loads the same way.

### Layout

- `/config/plugins/registry.json` — the single source of truth: which plugins
  exist, the exact commit each is pinned to, and which agents each is assigned
  to. Tracked in the config git repo, so every change is snapshotted.
- `/config/plugins/store/<name>/<artifact-id>/` — the materialized plugin
  content. Each `<artifact-id>` is a content hash of the plugin's source
  (repo + commit + subdirectory), so a given artifact directory is immutable:
  a new commit produces a new artifact, never an in-place overwrite.

Residents and specialists load their assigned plugins directly through the
Claude Agent SDK. A specialist's background job pins its exact artifacts at
launch and loads those, so a plugin update never changes the code a running
job is already executing.

### Managing plugins (via the Configurator)

Ask Ellen for a plugin change; she passes it to the configurator, which uses
these tools:

- `plugin_add(name, repo, ref, subdir, targets, expected_revision?)` — publish
  a plugin's pinned artifact, install any system requirements it declares,
  assign it to targets, then reload and verify.
- `plugin_update(name, new_ref, expected_revision?)` — re-pin to a new release
  and re-verify. Plugin releases are identified by a `vX.Y.Z` tag, annotated
  or lightweight (v0.74.0); passing `expected_revision` (the commit the release was built at)
  makes a tag that moved afterwards abort before anything changes. The version
  is always read from the plugin's own manifest — you never supply it, so the
  stale-version class of bug is gone. Both tools report phase-aware outcomes
  (`activation_committed` / `runtime_ready`) so a "pin landed, reload pending"
  state is actionable.
- `plugin_assign(name, target)` / `plugin_unassign(name, target)` — change which
  agents load a plugin. Targets look like `resident:ellen` or
  `specialist:finance`. **You cannot give a plugin of your own to a
  worker** (an `executor:<type>` target, which nothing loads): adding, assigning or re-pinning one is
  refused with the reason stated, before anything is installed. A worker target
  that is already stored on a plugin you installed stays in the registry file
  but is ignored — `plugin_list` shows it under `ignored_targets`, the plugin
  health report says so, and `plugin_unassign` clears it.
- **Access profiles (v0.338.0).** A plugin can declare named subsets of its
  tools (`casa.profiles` in its manifest), and `plugin_assign(name, target,
  profile)` gives an agent exactly that subset: a session built for the agent
  afterwards can use only those tools of that plugin. Leaving `profile` out
  gives full access, as before. Widest access wins: a profile is set only when
  an assignment is created, an existing assignment is never narrowed by a
  later request, and a plugin update that shrinks a profile applies to
  sessions started after it — a running session keeps what it started with.
  A plugin can also recommend another plugin and one of its profiles
  (`casa.requires`); Casa never enforces it, but the configurator tells you
  whether it is already covered or can be granted with one tap, and
  `plugin_status` shows an unmet recommendation as a warning.
- `plugin_remove(name)` — drop a plugin from the registry (its artifact is left
  on disk for now; see disk usage). The plugin's own data stays behind and a
  reinstall picks it up again — unless the plugin can erase its data itself (see
  "Erasing a plugin's data" below).
- `plugin_list()` / `verify_plugin_state(name)` — inspect the registry and check
  that the running agents actually agree with it.

### Secrets

A plugin declares required environment variables via `${VAR}` references in its
`.mcp.json`, and the configurator writes them to `plugin-env.conf`. It follows
one rule: "Search 1Password for every unresolved required variable not
declared in `casa.setupProvides`, except a variable that configures how
1Password itself is used (which vault)." A vault name is never mapped to a
vault item. Any other value — an API key, but also a region, a host, an
account or an email — is wired as a 1Password reference (`op://…`) when an
item the search found actually holds a field for it; you are asked which item
or field it is only when a found item holds a field the search cannot settle,
never about an item that merely matched by title. When nothing found holds a
variable, the rule is: "If nothing found holds a variable, use the plugin's
documentation or ask the operator for its value only for a non-secret
variable; report a secret unwired." A value the plugin's own setup tool
creates (`casa.setupProvides`) is not searched for: the setup run reports what
to wire and the configurator wires it. Secret values never appear in
transcripts.

### Protected plugin tools (v0.76.0)

A plugin can declare that one of its tools requires your approval before
Casa will run it (`casa.protectedTools` in the plugin's manifest). When an
agent tries to call a protected tool, Casa refuses the call and posts a
one-tap Approve/Deny button in your DM instead of running it blind. Approve
mints a grant that is:

- **single-use** — consumed the moment the retried call succeeds, so a
  second call needs a second approval;
- **argument-bound** — the grant only covers the exact arguments you
  approved; any change to the call needs a fresh approval;
- **time-limited** — a grant you never act on expires after 5 minutes.

You'll see this for actions a plugin author has flagged as consequential.
Deny leaves the call refused with no grant issued. An approval keyboard you
never answer expires after 10 minutes (the message says so when it does);
asking the agent to retry the action posts a fresh one.

A plugin author can also upgrade the approval prompt's headline to a
plain-language action sentence (the exact arguments and tool id always
remain visible below it), by pairing the tool name with a `summary`
template in the manifest:

```json
"casa": {
  "protectedTools": [
    {"name": "invoice_reset", "summary": "Delete the invoice draft for {period}"}
  ]
}
```

`{period}` is filled in from that call's own arguments, so the prompt reads
"Alex (finance) wants to: Delete the invoice draft for 2025-05" — the exact
arguments still always appear below, unabridged.

### The result contract: capabilities never pass through the chat

A plugin tool can return a live capability — a sign-in link, a one-time
code, a token. Before this release nothing but the assistant's judgement
stood between such a value and your chat history. Now a plugin **declares**
which of its tools return one, and hands the value to Casa instead of
returning it:

```json
"casa": {
  "resultContract": {
    "version": 1,
    "tools": {
      "list_accounts":    {"result": "safe"},
      "fetch_login_link": {"result": "capability", "provides": ["login_link"]},
      "complete_login":   {"result": "safe", "consumes": {"link": "login_link"}}
    }
  }
}
```

- `tools` must list **every** tool the plugin's MCP servers expose, except
  its `casa.setupTool`; a call to an unlisted tool is refused before it runs.
- A `safe` tool's result is delivered unchanged — the author asserts it
  carries no live capability.
- A `capability` tool deposits each value it `provides` with Casa **during
  the call** and returns the reference Casa hands back, in that field:
  `POST` `{"client": $CASA_BROKER_CLIENT, "slot": "login_link", "value": "…"}`
  to `http://localhost/internal/broker/deposit` over the Unix socket named by
  `$CASA_BROKER_SOCKET` (both are in the server's environment), then return a
  JSON object such as `{"account": "ops", "login_link": "<reference>"}`. The
  reference looks like `casa-cap-<32 hex>`; the value itself never enters the
  tool result, the assistant's context or the transcript.
- A tool that `consumes` a reference receives it in the named parameter as
  `<reference>:<ticket>` and redeems both:
  `POST {"client": $CASA_BROKER_CLIENT, "reference": …, "ticket": …}` to
  `/internal/broker/redeem`, which answers `{"value": …}` exactly once.
  A reference is single-use, expires after fifteen minutes, is bound to the
  operator and agent that fetched it, and is lost when Casa restarts.
- Return a JSON **object** from a capability tool (a `dict` in FastMCP); a
  plain string has no fields to carry a reference. Never place a capability
  in an error message — an error is shown to the assistant unchanged.
- The setup tool is exempt unless the plugin declares it as a capability
  tool that delivers its link (below).

#### Links you must open arrive in your chat with the assistant (v0.318.0)

A bank's approval page, a Google sign-in — a link only you can act on. A
plugin says so by adding `delivers` to the capability tool that produces it:

```json
"link_bank": {"result": "capability", "provides": ["approval_link"],
              "delivers": {"approval_link": "operator_link"}}
```

The tool deposits the `https` URL as any capability, optionally with a
`label` (up to 40 characters) and a `caption` (up to 200), and returns the
reference. Casa itself then posts **one message in the chat of the agent whose
plugin made it** — your chat with the assistant for her own plugins, a
specialist's own chat for its plugins (see [Agents with their own
chat](#agents-with-their-own-chat-v03600)), never an agent's log — as a link
whose text is the label and the real destination host, with the caption
beneath. The assistant's tool result is replaced by a receipt saying the link
was delivered; if Casa could not confirm delivery, the result is withheld and
the assistant tells you a fresh link is needed. The plugin's own text never
claims the link arrived, so an assistant that never received the receipt
reports it as unconfirmed. A delivered link is consumed by the delivery — no
tool may `consumes` it — and a plugin's `casa.setupTool` may adopt the same
declaration for its sign-in link, provided it delivers every slot it
provides. A plugin declaring `delivers` needs Casa 0.318.0 or later; an
older Casa refuses its manifest at install.

Since v0.346.0 the link message starts with a label line — `📊` and the name
of the agent whose plugin sent it, as on a specialist's other posts — and
Telegram shows no link preview under it: the link text already names the
host.

Since v0.347.3 the link is a button: the message is the label line and the
caption (the label when there is no caption), with one button to tap whose
text is the label and the real destination host, the way cards carry their
buttons. Only if Telegram refuses the button does Casa send the link text as
before.

A tool that delivers a link may find it has no link to make this time — the
account is already connected, or an earlier link is still outstanding. It
says so by returning every slot it provides as `null` and depositing
nothing, for example `{"auth_url": null, "status": "already_connected", …}`.
Casa passes that result to the assistant unchanged and sends nothing, and a
setup run that ends this way counts as run. Any other result without its
reference is still withheld. This needs Casa 0.319.0 or later.

#### A specialist's report or file arrives in your chat, labelled (v0.339.0)

A plugin can also hand you its output directly — a quarterly summary, an
export — instead of having the assistant retell it. It declares the slot as
`operator_message` (text, up to 12,000 characters, with no control characters
but newline and tab) or `operator_file` (a file it wrote to its outbox, with
the media `kind` it is — `document`, `photo`, `audio`, `voice`, `zip` or
`text` — an optional one-line `caption`, and, since v0.344.0, an optional
`filename` for the name you see when the plugin declares `"filename": true` on that
tool), and deposits it like any
capability. Casa posts it in that specialist's own chat (the assistant's chat
for her own plugins), headed in the assistant's chat by `📊` and the agent's
name — the plugin cannot change or hide that line — and the
assistant gets a receipt instead of the text, so nothing is paraphrased on the
way. A message is split into pages like any long reply; one that would need
more than six pages, or that could not be sent whole, is refused and nothing
is posted. A file goes through the same checks as the `send_media` tool and
is consumed whether or not it arrived; the label line plus the caption must
fit the media caption limit of 1,024 characters. If Casa could not confirm
delivery the result is withheld, as for a link. The assistant's own
conversation learns only that the specialist posted something and how large
it was, never what it said — ask the specialist if you want the detail
repeated. Needs Casa 0.339.0 or later.

Since v0.344.39 a plugin can give a post a `key`, its own name for that
post. Casa remembers each key it delivered to you, for thirty days. If the
plugin delivers the same key again (for example, because its job was stopped
after posting but before it noted the post), Casa sends nothing and hands the
plugin the first delivery's receipt, so you do not get the message twice. A
card with buttons counts as already delivered only while its buttons still
work; otherwise you get a fresh card.

#### A specialist's answer, told by the assistant (v0.358.0)

When a specialist the assistant asked answers you, the assistant tells you
that answer herself, in her own words: the specialist's "I" becomes its name
("Alex found €420 in October…"), and every amount, date, name and question it
gave you reaches you as it gave it. Casa no longer posts a specialist's answer
under its name. What a specialist posts itself — its cards, links and
files — arrives in its own chat (since v0.360.0).
A specialist that ends its turn silently has nothing for you: Casa tells the
assistant so, and she does not say it is still working. When a specialist's
answer is too long for what the assistant reads, Casa keeps the message the
specialist ended its turn with and hands it to her beside the rest, so the
answer is not lost to the cut.

#### A reply in the assistant's chat is hers (v0.361.0)

In your chat with the assistant, a reply (quote) on any message is a message to her,
whoever posted it — her own message, a Casa card, or a specialist's post from before it had
its own chat — and Casa tells her which message you answered and what it said. A `/new`
sent as a reply resets her conversation, as any `/new` does. The one exception is a post of
work that is still running: a reply to it goes into that work. To talk to a specialist
directly, write in its own chat.

#### Buttons on a specialist's post (v0.342.0)

A specialist's plugin can offer you a decision as buttons under its message —
"Pair invoice 17 with the Adobe payment? [Yes] [No] [More]" — by declaring a
slot of the kind `operator_proposal` and depositing the text with one to six
buttons, each naming one of the plugin's own tools with fixed arguments. Casa
posts it labelled, like any specialist post. When you tap, Casa runs that one
call and nothing else: a short specialist turn in which only that exact tool
with those exact arguments is allowed — any other call is denied — and, when
the call runs and returns a result, the plugin's own answer comes back to you,
labelled, as the receipt (a plugin that answers with a JSON object can give
the sentence you see as its `receipt`; otherwise you see its answer as it is). That turn is told to
make the call even if the proposal looks out of date, because the plugin
decides whether the tap still applies, and it is not given earlier exchanges
or its memories. It is told that the tap is yours, on its own
proposal, so it does not weigh the call against what its role covers
(v0.344.38). It can still end without making the call; then
Casa tells you it could not apply your tap (`no_call`), and the tap is not
retried. A tap by
anyone else, on an old or restarted keyboard, or a second tap, does nothing
but show a short toast; a button whose plugin has since been updated,
unassigned, narrowed or is being erased is refused with the reason. If the
turn hits its time limit, Casa tells you at once, ends the turn's processes
and frees the specialist; in the rare case it cannot confirm they ended, that
specialist is put out of service ("faulted") until the
next Casa restart, and the health report says so. If an installed hook changed
the call's arguments on the way, the receipt still arrives with a warning line
above it. A proposal expires after an hour; at most 32 live ones per chat. The
agent whose chat the card is in learns only that you tapped and whether it
applied. You can also type your answer as a reply to the card: it goes to the
work that posted it while that work runs, otherwise it is an ordinary message
to the chat's agent. Needs Casa 0.342.0 or later.

A tap can also bring the next decision (v0.344.35). When the tapped tool's JSON
answer carries, beside its `receipt`, a `next` object of the same shape as a
proposal (text, buttons, optional `revision`), Casa posts the receipt and then
that card, so a list of decisions is walked one tap per card with no **Next**
button. The card is checked like any proposal — its buttons may only name the
same plugin's own tools — and its buttons run only when you tap them. If the
card cannot be shown, the receipt stands and one line says why; if the receipt
itself could not be posted, no card follows. A card with the
same `revision` as a live one replaces it.

A button can also send you a file (v0.344.37): a plugin tool whose one slot
delivers a file may be stored on a button, so a tap on, say, **Get package**
sends the freshly built file, and the file itself is the answer to your tap. If
it cannot be sent, Casa tells you it could not apply your tap, with the reason.
Such a button may also leave its card as it was (v0.344.58): with `"keep_card": true`
beside its call, a tap on, say, **See PDF** sends the file and the card keeps its text and
every button, so you can still answer it. You can tap it again; any other button answers
the card as before.
A card can also carry a **Close** button (v0.344.64): a button with `"close": true` in place
of a call, at most one per card, with the plugin's own label. Tap it, and the card's buttons
disappear at once while its text stays; nothing runs and nothing is sent, and a later tap on
the same card does nothing. Casa never adds one by itself.
A card can also come with a list longer than one message before it (v0.344.67): with a
`"pages"` list of one to six texts, each fitting one message, Casa posts every page in order
as its own labelled message and then the card with its buttons, as one delivery. A reply to
any page carries that page's text, so the agent knows which page you meant. If a page cannot be
posted, the card is not posted either, and the specialist is told how many pages arrived.
A plugin that sends pages needs Casa 0.344.67 or later: an older Casa posts the card alone.
A card can also be one message with a picture (v0.346.0): with a `"photo"` naming a picture
in the plugin's outbox (and no `pages`), Casa posts the picture with the card's text as its
caption and the buttons under it — a caption has less room than a message — and every later
edit of the card is an edit of that caption. If the picture cannot be sent, the card is not
posted either. One button may be a link, `{"label", "url"}` with
an `https` address, when the plugin also declares an `operator_link` slot: tapping it opens
the address, and nothing runs. And beside a `safe` tool's `receipt` (with no `next`),
`"on_card": true` writes the receipt as the tapped card's last line instead of a new message
— unless an installed hook changed the call's arguments (the receipt then comes as a message,
with the warning above it) or the card's line cannot be written (then it comes as a message
too).
These need Casa 0.346.0 or later.

#### A file for a running piece of work (v0.343.0)

A file follows the same rule as a reply. Send a PDF or photo as a reply on a post of work
that is still running, and it goes into that work: it is stored in the inbox of the agent
doing it, and that work is given that one file to read. A plugin can also put a
`📎 Add a document` button on its card (a button with `"arm_file": true` instead of a
call); tap it, and the next file you send in that chat within ten minutes goes into the
work the card belongs to, the same way, while that work runs. The tap only ticks the
button; no message follows it. A later 📎 tap replaces the earlier one, a text message
leaves it waiting, the next file uses it up even when it is a reply to another post (the
reply decides where that file goes), and a restart forgets it.

Any other file is the chat's own agent's, as any file you send there is: a file replying to
a post of work that has ended, to a specialist's post in the assistant's chat, or sent
after a 📎 tap on a card whose work is not running. A specialist can read and share only its
own inbox; the assistant keeps hers.

#### Links a plugin needs from you: the vault drop-off (v0.330.0)

Some sign-in steps run the other way: the provider emails you a single-use
link and the plugin needs it back. When you paste that link to the assistant,
or ask it to read that one email for you, it never passes the link on in a
message to the specialist. It stores it in a **drop-off** the plugin declares,
then tells the specialist only that a link is waiting:

```json
"casa": {"dropOffs": ["signin_link"]}
```

The assistant's `vault_drop_off` tool writes the value to one item in Casa's
default 1Password vault, titled `Casa drop-off <plugin> <name>` (for a plugin
a specialist owns, `<plugin>` is the plugin's own manifest name, for example
`bank-feed`), in its `password` field, and tagged `casa-drop-off`. It writes
nothing else: an item with that title that Casa did not create stops the
write. The plugin reads
`op://<default vault>/Casa drop-off <plugin> <name>/password` when its step
runs, checks the item's `created_at` against when it asked for the link, and
deletes the item after the attempt. The link you pasted stays in your own
chat history. Declaring `dropOffs` needs Casa 0.330.0 or later; an older Casa
ignores it.

**What changes for an installed plugin.** A plugin that has not adopted the
contract has every tool except its setup tool refused, from this release on,
until the plugin is updated; the assistant is told to ask you to update it.
There is no grace period — a permissive window is exactly what would stop
this being a boundary. Skill-only plugins (no MCP server) are unaffected.
Casa validates the declaration's shape and never guesses one for a plugin
that lacks it; a plugin whose declaration is malformed is refused at install.

### Plugins that set themselves up (v0.154.0)

Casa refuses to start a plugin whose `.mcp.json` needs an environment
variable it can't resolve — otherwise the plugin's server would start
"successfully" against a placeholder instead of a real credential. That is
the right default when the credential is something *you* supply.

Some plugins are different: their setup tool exists to **create** the
credential — forging a private key into your vault, registering an
application and learning its id. Waiting for those would deadlock the
plugin, since setup can't run until they exist and they don't exist until
setup runs. A plugin author says so in the manifest:

```json
"casa": {
  "setupTool": "setup_bank_feed",
  "setupProvides": ["CASA_PLUGIN_BANKFEED_PRIVATE_KEY",
                    "CASA_PLUGIN_BANKFEED_APP_ID"]
}
```

The plugin then loads without them — Casa passes them as empty, never as a
literal placeholder — so setup can run. `verify_plugin_state` still reports
it **not ready** with `setup_env_unprovisioned` until the values actually
land, so a setup run that never happened stays visible rather than passing
silently. A plugin cannot write its own settings: the setup tool reports the
reference or value to wire, the assistant hands that report to the
configurator, and the configurator wires it. Make the setup tool's result
name each value to wire exactly. Declared names must use the reserved `CASA_PLUGIN_` prefix:
declaring a name binds it for the whole session, so the namespace is fenced.

**A merely optional variable needs no declaration.** Write
`${MY_TOKEN:-}` in `.mcp.json` — Claude Code substitutes the default, so
nothing is missing and no placeholder leaks, and Casa never withholds the
plugin for it. A default can be a real value too
(`${MY_MODE:-production}`). Use `setupProvides` only when you want the
not-ready reporting, which a default cannot express.

Anything *not* declared and *not* defaulted still blocks the plugin as
before.

If a plugin reports a Casa-owned variable missing — `OP_SERVICE_ACCOUNT_TOKEN`,
`ONEPASSWORD_DEFAULT_VAULT` — the fix is the matching
**app option**, not `plugin-env.conf`; the message names the option.

### Who runs the setup tool (v0.161.0)

**Casa does.** You do not need to ask an agent to run a setup tool for it to
happen. When a plugin declares `casa.setupTool`, Casa
records that the plugin is owed a setup run and performs it itself, dispatching
to one of the plugin's own target agents (its tools exist nowhere else).

Approval is what *clears* the run; it is not the only thing the run waits for.
Casa clears it:

- immediately, when Casa can establish the plugin needs nothing approved;
- when you tap **Approve**, if it declares a trigger or callback consent — and
  only once **every** consent it declares is approved;
- never, if you **Decline**. Casa says so, and approving later runs it.

A cleared run then waits until it can actually succeed: the plugin's webhook or
callback routes must be live, every environment variable its server needs must
be resolved, and the agent that will run it must already be able to load it. So
"cleared" is usually followed by the run within seconds, but a plugin waiting on
a secret you have not wired yet — or on a public URL that is not valid — waits
as long as that takes.

Anything Casa cannot yet establish — no chat to prompt you in, or a permission
it cannot currently ask about because the plugin is unassigned or its target
lacks the right channel — leaves the run **pending** rather than guessing. An
unaskable permission is never treated as one that isn't needed.
Every pending run appears in the plugin health report and stays there until it
happens, whichever of these it is waiting on, so a setup step that never ran is
visible rather than silent.

You will see the setup outcome as **its own message** once the run happens. It
carries the setup tool's own words: Casa does not translate that into a verdict
about whether the connection works, because it cannot see the other side.

If an agent runs a plugin's setup tool itself after the run was cleared — for
instance because Casa's own run landed in a session that had not finished
loading the plugin and the agent then ran it on its next turn, or because you
asked for it — Casa counts
that run and does not ask for it again (v0.315.0). The plugin health report and
the status tool then say the setup ran, by the assistant's own hand.

One limit worth knowing: a plugin whose setup tool is named only in its README
or in a developer handoff, with no `casa.setupTool` in the manifest, has no
automatic path — nothing runs it. Casa will say the plugin declares no setup
hook rather than guess a tool name.

### Plugin authorization callbacks

Some plugins connect to an external service that hands back an authorization
result through a **browser redirect** — the return leg of an OAuth-style
"Connect your account" flow. Casa exposes a public `GET /callback/<name>` URL
for this: the provider redirects the browser to it, Casa parks the result in a
short-lived on-disk spool, and the plugin's agent collects it. The URL is
unauthenticated by design (a browser redirect carries no login), produces no
agent turn, and the deposited authorization code expires within minutes.

To use a plugin that needs one:

1. **Set `public_url`** to the public HTTPS origin external providers reach Casa
   at — for example `https://casa.example.com`. It must be a clean `https://`
   origin: no IP address, no path, no embedded credentials. Every callback
   redirect URI is built from it, so if it is unset or malformed the plugin
   reports its callback as unavailable.
2. **Publish the external API port (`18065/tcp`) and forward `/callback/` to
   it.** That port is not host-published by default; in a typical setup a
   reverse proxy (for example Nginx Proxy Manager) terminates TLS for your
   `public_url` origin and forwards to the container's `18065` over the `hassio`
   Docker network.
3. **Approve the callback in Telegram.** When a plugin declares a callback, Casa
   sends you a one-tap consent DM — "Plugin '<name>' wants to receive browser
   redirects at GET /callback/…". Until you tap Approve the route serves the
   same neutral response as any other and publishes no result — nothing
   distinguishes an unapproved route from an approved one. Consent is bound to
   the callback's declared name, so a routine
   plugin update that does not change the declaration keeps your approval; a
   rename asks again. You can withdraw it at any time, which darkens the route
   until you re-approve.
4. **Register the exact redirect URI with the provider.** The URI to paste into
   the provider's app settings is your `public_url` joined with
   `/callback/<effective-name>`, and the plugin's setup reports it. The setup
   message you see can be one short line that leaves it out; to get it, ask the
   assistant to run the plugin's setup again and tell you the redirect URI.
   Providers match this value byte-for-byte, so register exactly what the plugin
   reports.

**Reverse-proxy recommendations.** The callback query string carries a bearer
credential, so configure your proxy to **redact the query for `/callback/`
paths in its access log** — Casa already suppresses it in its own logs and in
the container's nginx access log, but your outer proxy's logs are yours to
configure. Optionally, apply a **proxy-level rate limit** to `/callback/`:
Casa answers every callback identically whatever the load (there is no
distinguishing error response to probe), so any throttling of abusive traffic
belongs at the proxy.

### Plugin events

Some plugins react to what another installed plugin does — "notify me when
finance records a new invoice" — entirely inside Casa, no external service
involved. A plugin declares the events it may raise (`casa.emits`) and
another plugin declares the ones it wants delivered to it
(`casa.subscribes`); an event carries no data, it is a pure "something
happened" nudge, so the receiving plugin re-checks its own state to see
what changed.

Because a delivery reaches into one of your agents (unlike a callback,
which grants nothing), Casa asks for your approval before routing one.
When both sides are correctly declared and installed, you get a one-tap
consent DM — "Plugin '\<subscriber>' wants delivery of '\<event>' from
'\<emitter>' → \<resident>". Until you tap Approve, nothing is delivered
and plugin health names the reason. The approval is bound to the exact
subscriber, its version, the emitter, the event, and which resident
receives it — updating the **subscriber** plugin, or reassigning it to a
different resident, invalidates the old approval silently and re-prompts;
it is never carried forward. Updating the **emitter** does not re-ask, as
long as its new version still declares the event — only the subscriber's
own version and target selection are part of what you approved.

Once approved, a delivery dispatches as a quiet, headless turn to the
assigned resident — you will not see it in Telegram unless the resident's
own handling of it produces a normal message. If a delivery goes
unanswered, Casa retries on a widening schedule (immediately, then after
5 minutes, 30 minutes, 2 hours, 6 hours, and 24 hours) before giving up
after the sixth attempt; you get a DM either way — an operator note if it
had nowhere to go, or a "still hasn't been handled" notice once retries are
exhausted. `event_ack_revoke` switches a plugin's event access off
immediately (one subscription, or all of a plugin's, depending on what you
ask for); re-approving later re-consents.

### Disk usage

The store lives on `/config` (the `addon_config` volume), so artifacts persist
across app updates. Because artifacts are content-addressed, updating a plugin
adds a new artifact directory rather than replacing the old one, and removing a
plugin leaves its artifact in place. Automatic garbage collection is written but
**ships disabled in this release**; unreferenced artifacts can be pruned in a
later version. Typical plugins are small (skills + a small MCP server), so this
is not a concern for normal use.

### Integrity model

Artifact integrity rests on **content-addressing + checksum detection**: each
artifact directory is named by a hash of its source, and its bytes are checksum-
verified whenever the plugin snapshot is (re)loaded — a mismatch is reported
(`corrupt_artifact`) so a tampered or damaged artifact is never silently loaded. The write guards on
`/config/plugins` and the read-only freeze of published files clear every write
bit on the artifact tree. They are best-effort defense-in-depth, not a
filesystem privilege boundary.

### Fresh install & rollback

The registry (`/config/plugins/registry.json`) is the single source of truth. A
fresh install starts with an empty registry: the app image ships no default
plugins. Rollback is safe — the registry format is stable across
releases, so a downgraded app image reads the same registry as before —
with one boundary: a plugin version whose manifest uses the object form of
`casa.protectedTools` (with a `summary`, introduced in 0.78.0) is rejected
by pre-0.78.0 releases as invalid and excluded from loading; downgrade the
plugin to its last string-form release before (or after) downgrading the
app past 0.78.0.

### Troubleshooting

- **A plugin isn't loading / an agent complains it's missing** — run
  `verify_plugin_state(<name>)` (ask Ellen). It compares the registry's desired
  state against what each running agent has actually loaded and reports the
  reason (`artifact_missing`, `corrupt_artifact`, `reload_required`,
  `authorization_missing`, unresolved secret, …).
- **`reload_required`** — the registry was updated but the agent hasn't been
  reconstructed yet; a reload (or the configurator's own post-update reload)
  clears it.
- **Health at a glance** — `/data/plugin-health.json` summarizes current plugin
  issues; Casa also DMs the operator when a *new* issue appears, and remembers
  only the problems that message actually named. Your agents can prepend a
  one-line notice to a reply about a blocking problem of their own that the
  direct message did not name — because it was behind the "and N more" tail, or
  because the message never got through — so the same warning does not normally
  arrive on both. Nothing that was not named is treated as told, so it can be
  named the next time Casa checks (a restart, a plugin change, a reload). For
  the complete list at any moment, ask your assistant.

## Installing a specialist from a repository

Specialists are no longer bundled with the app image — each one (the
`finance` specialist, a Magic — The Gathering rules judge, and any future
ones) lives in its own repository and is installed on demand through the
configurator. Ask Ellen, for example: *"Install the finance specialist
from bonzanni/casa-specialist-finance"*.

What happens:

1. The configurator inspects the repository and reports the specialist's
   mission, its default persona, and any configuration or secrets it
   needs.
2. You approve the install from a DM Approve/Deny prompt — nothing is
   installed without that tap.
3. Once approved (and any required configuration supplied), the
   specialist activates and Casa reloads so residents can delegate to it.

The same repository-driven flow covers **upgrading** to a newer version,
**rolling back** to the previous one, and **uninstalling** a specialist —
just ask Ellen (e.g. *"Upgrade the finance specialist"*, *"Roll back
finance to the previous version"*, *"Uninstall the finance specialist"*).

**Erasing a plugin's data.** Some plugins can erase their own data — the
finance specialist's bank feed, for example, erases its ledger and backups.
When you uninstall such a plugin, or a specialist that bundles one, Casa asks
you first in a DM, offering the choices the plugin supports:

- **Keep data** uninstalls as before, and a reinstall picks the data up again.
- **Erase data, keep sign-ins** erases the data but keeps what a reinstall
  needs to carry on without signing in again (for a bank feed, its bank
  sessions).
- **Erase everything** erases all of it, sign-ins included, and Casa also
  clears its own references to the plugin's secrets — as if the plugin had
  never been installed.
- **Cancel** removes nothing.

An erase runs the plugin's own eraser first, and the uninstall goes ahead only
if the plugin reports the erasure complete. If it does not (a bank would not
confirm withdrawing its consent, say), nothing is removed and you get the
plugin's own report, so you can try again or uninstall anyway. While the
plugin's data is being erased, and after a complete erasure until the
uninstall finishes, Casa refuses every other use of the plugin's tools, so
nothing is written behind the erasure. A plugin may
also leave vault items it cannot prove it created — ones that could be yours,
such as items from before it recorded what it creates. It does not delete
them and does not hold up the uninstall for them; Casa names them when it
reports the uninstall, so you can delete them by hand. Home Assistant
backups taken earlier still contain the data either way.
You can also ask Ellen to apply another installed persona to an installed
specialist, but a specialist wears only the personas its own role lists: anything
else, a demo persona included unless its role lists it, is refused with nothing
changed. To go back to its bundled persona, roll the specialist back; that works
while the bundled persona is still the one kept for rollback, so not after a second
persona or an upgrade.

**Changing a specialist while you are talking to it.** A conversation already
open with a specialist — a background job it is running — is
not switched by a change to that specialist. Before a persona is applied to it,
or it is upgraded or rolled back, Casa holds the change back and gives the
configurator a list of its open conversations, which the configurator's
instructions say to show you before asking you to confirm; of each one it says:

> When it resumes, it picks up Casa's updated settings, but it keeps its
> personality and the plugin versions it started with. To get everything new,
> cancel it (ask Ellen) and start it again.

Saying no changes nothing. New conversations get the new setup once Casa has
loaded it: an upgrade or a rollback that completes loads it during the change,
and applying a persona reloads the specialist right away. An **uninstall** asks the same
way, and once the specialist is removed Casa closes the conversations still
open, and Ellen is notified. A specialist you **disable**
(its `enabled: false`) cannot be confirmed beforehand: Casa closes its open
conversations when the disable takes effect — at the reload that reads it, or at
the next start if Casa was down — and Ellen is notified. If a close fails, Casa
tells you which conversation is still open and tries again at the next reload
of the agents.

**Changing a plugin a specialist uses, while you are talking to it.** Adding a
plugin to a specialist, assigning or unassigning one, updating one or removing
one asks the same way when a specialist the change reaches has open
conversations — an update or a removal reaches every specialist the plugin is
assigned to. The list shows each specialist's open conversations, followed by
what the change means for them:

- adding or assigning: "Its open conversations keep the plugins they started
  with and will not get *the plugin*."
- unassigning or removing: "Its open conversations keep *the plugin* loaded but
  lose its approvals, so a protected call asks again and earlier references to
  it stop working."
- updating: "Its open conversations keep the previous version and lose its
  approvals."

Saying no changes nothing. Saying yes goes ahead for every specialist the change
reaches; a conversation that was not on the list — one opened while you were
deciding, say — is named after the change instead of asking again. A confirmed
add or update can still stop afterwards, for instance when the version cannot be
fetched or does not match its tag; the plugin is then not added or updated and
nothing is activated, though a version Casa had already fetched may stay in its
plugin store. Removing a plugin that can erase its
data asks the confirmation first, then the erase question.

If an upgrade fails after the new version was already kept but before the
upgrade itself tried to load it, Casa says that re-running the upgrade finishes it (or, in the
rarer case where cleaning up the previous version also failed, that you should
restart Casa and then re-run the upgrade). It compares the version the
specialist is running with the one the upgrade kept and the one it replaced,
the same way for a specialist that has had a persona applied to it.
When it is the kept one (a reload of the specialist during the upgrade can load
it), Casa says the upgrade did not finish and that the specialist was running
the new version when the upgrade returned. For a specialist whose running version it
cannot read (no copy of it is running), Casa says the upgrade did not finish and
that which version the specialist was running when the upgrade returned could
not be established; it
does not say that Casa has not loaded the new version, because such a reload may
have. Otherwise it says the upgrade is not active yet: when the running version
is the replaced one, that new and open conversations still use the previous
version; when it is another one (an earlier failed upgrade can leave an older
version running), that the specialist was not running the new version when the
upgrade returned. If Casa did
try to load the kept version and reloading the specialist failed, Casa reports
that failure; when it read that the specialist was not running the new version
when that reload returned, it says so instead of saying the new version is
active. It does not say which older version was running. When it could not read
which version was running, it says
that which version was running when that reload returned could not be
established, rather than that the new version is active.

**Changing a resident's persona costs you its conversations.** Giving Ellen,
the butler or the concierge a different persona — or resetting one back to its
built-in default — takes effect at the next restart, and if that changes who
the resident is, the restart starts fresh every conversation with that
resident that was last held under a different persona — in practice, all of
the ones you have going — on Telegram and on voice alike. Voice conversations never go into
long-term memory, so anything said there and not repeated is gone. What was said in a
Telegram conversation is already in long-term memory, saved as it was said, so
the assistant can still look it up; the fresh conversation just does not carry
it in its context.

A resident can wear its own persona, at any version from 0.1.0 up to but not including
1.0.0, or a persona whose id starts with `demo/` in the same range: the
[five public demo personas](https://github.com/bonzanni/ha-casa-app#personas) are of
that kind. Install one first, then ask for it to be applied. Anything else is refused with
nothing changed, and that includes another resident's persona. If you go back to a version
of Casa older than 0.347.14 while a resident is wearing a demo persona, Casa will not
start, so reset that resident to its default and restart before going back. If you have
already gone back, stop Casa, delete `active.yaml` and `active.prior.yaml` from that
resident's `bindings/resident-<assistant|butler|concierge>/` folder in Casa's config, and
start it again: it comes up on its default persona.

When you install a persona and name no version, Casa takes its newest published release and
tells you which one. A repository with no release or version tag is refused unless you name
a branch or a commit.

Casa's configurator is instructed to spell this out in the answer that carries the
**Restart now** button, and the tools that stage a resident's persona return the warning
with the result — but nothing *forces* it into the sentence you read. **If you are offered
a restart and were not told what it costs, ask before you tap.**
Treat it as a real possibility rather than a certainty either way: what a
restart activates is settled at start-up, not when you ask for the change.

## Plugin consumer infrastructure (v0.71.0)

Plugins are managed through the unified registry + immutable store — see
[Plugins](#plugins-v0710) for the full model. There is no marketplace and no
`enabledPlugins`: the registry (`/config/plugins/registry.json`) is the single
assignment authority, and each pinned plugin resolves to an immutable artifact
under `/config/plugins/store/<name>/<artifact-id>/`.

## 1Password integration (v0.14.1)

Set `onepassword_service_account_token` (from https://developer.1password.com/docs/service-accounts/)
as a plaintext app option — it's the single root of trust and cannot
self-reference. Set `onepassword_default_vault` to the vault name (default
"Casa"). Every other password-typed option (`claude_oauth_token`,
`telegram_bot_token`, `webhook_secret`) accepts either
plaintext OR an `op://` reference.

Plugin env vars resolved via `plugin-env.conf` (`/config/plugin-env.conf`),
managed by Configurator.
