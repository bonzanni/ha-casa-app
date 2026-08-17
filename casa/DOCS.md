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

Two more tiers extend the fleet without changing the residents. **Specialists** are ephemeral agents a resident delegates focused work to — finances, a mailbox, a hobby domain — installed from git repositories. **Executors** are task-bounded agents that work in a dedicated Telegram topic: the *configurator* changes Casa's own configuration for you, and the *plugin-developer* builds new plugins. Specialists, plugins and personas all install the same way, by asking in chat.

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
| `telegram_engagement_supergroup_id` | Chat ID of the dedicated Telegram forum supergroup used for interactive engagements (Tier 2 Specialist interactive mode; Tier 3 Executor types, Plan 3+). Must be a negative integer. Leave at 0 to disable engagements. |

### Optional -- Memory

Short-term conversation continuity always works via the Claude Agent SDK
session. **Long-term** memory (cross-session recall) is off by default and
is enabled by pointing Casa at a self-hosted **Hindsight** app.

| Option | Description |
|--------|-------------|
| `hindsight_api_url` | Internal base URL for the self-hosted Hindsight app (e.g. `http://5884eb17-hindsight:8888` or its IP), reached via the app's hassio network alias/IP — not the bare host `hindsight`. **This is the single toggle for long-term memory: set it to turn long-term semantic memory ON** (the app auto-derives `MEMORY_BACKEND=hindsight`) — both **save** (the freshness reaper retains ended conversations, each item tier-classified) and **recall** (a mental-model overlay + relevance-ranked recall on the read path, plus a `recall_memory` pull tool). **Leave empty to keep long-term memory disabled** (short-term continuity still works via the SDK session). |

**Wiping long-term memory** (v0.194.0): one supported operation deletes the whole
bank, drops any pending durable retry records, and forgets every conversation
pointer without saving it — closing the "cleared it by hand but items kept
reappearing" gap. Two ways to run it, both explicit: ask the assistant (the
`wipe_memory` tool posts an Approve/Cancel keyboard to the configured operator's
DM and executes only on the operator's own tap, reporting exactly what it
removed), or from the add-on terminal run `casactl memory-wipe --yes` (refuses
without the flag). With `telegram_chat_id` empty nobody is the operator, so the
assistant-side wipe is denied for everyone. A conversation or engagement already
in flight when the wipe runs may still contribute one item afterwards; everything
durable is removed.

The following env var is **auto-derived** from `hindsight_api_url` and rarely needs
setting by hand:

| Env var | Purpose | Default |
|---|---|---|
| `MEMORY_BACKEND` | Long-term memory backend: `hindsight` or `noop` (disabled). **Auto-set to `hindsight` by the app whenever `hindsight_api_url` is non-empty**; otherwise unset → `noop`. Any unrecognized value → `noop`. You normally just set `hindsight_api_url`. | derived from `hindsight_api_url` |

### Optional -- Agents

| Option | Description |
|--------|-------------|
| `primary_agent_model` | Model for the primary agent: `opus`, `sonnet`, or `haiku`. Default: `opus`. |
| `voice_agent_model` | Model for the voice agent. Default: `haiku`. |

### Optional -- Features

| Option | Description |
|--------|-------------|
| `enable_terminal` | Enable a web terminal accessible via the ingress panel. Default: `false`. |
| `webhook_secret` | Optional manual override for the HMAC secret that authenticates webhook, invoke and voice requests. Leave empty and Casa generates one in `/data/webhook_secret`, retrievable through the web terminal. Authentication is always on. |
| `context7_api_key` | API key for the bundled Context7 documentation plugin (plugin-developer toolbox). Optional; an entry in the plugin env store overrides it. |
| `engagement_reap_days` | Auto-close engagements after this many days without activity (daily sweep cancels them and closes their Telegram topic; the engaging agent is notified). Set `0` to disable. Default: `7`. |
| `log_level` | Log verbosity: `debug`, `info`, `warning`, or `error`. Default: `info`. Flip to `debug` for verbose troubleshooting without rebuilding the image. |
| `specialist_max_concurrency` | Max specialist delegations in flight fleet-wide at once (see [Delegation limits](#delegation-limits)). Range 1-20. Default: `2`. |
| `specialist_cost_alert_threshold` | Cumulative per-specialist USD spend past which Casa logs a warning on further delegations (see [Delegation limits](#delegation-limits)). Default: `5.0`. |

## How it works

1. **Startup**: The app validates your OAuth token, copies default agent configs (if first boot), and starts nginx + the Casa core process.
2. **Message flow**: Incoming messages (Telegram, webhook, voice) are routed through an async message bus to the appropriate agent based on the originating channel.
3. **Agent processing**: Each agent builds a system prompt (personality + memory context), queries the Claude Agent SDK, stores the conversation in memory, and sends the response back through the originating channel.
4. **Home Assistant integration**: Agents interact with HA via the official HA MCP server, allowing them to control devices, read states, and create automations.
5. **Per-agent triggers**: Each agent declares scheduled triggers (cron, interval or a one-off date) in its own `agents/<role>/triggers.yaml`. The TriggerRegistry registers them at boot, and fires them via the agent's normal turn loop.
6. **Reminders**: Ellen can set her own reminders, which are ordinary triggers written to her `triggers.yaml` — so they survive restarts and updates. One-off reminders remove themselves after firing, and any reminder whose time fell while Casa was down is delivered on the next sweep rather than lost.

## API endpoints

All endpoints are accessible through the ingress proxy.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/healthz` | Health check |
| `GET` | `/api/voice/agents` | Discover enabled Home Assistant voice residents (authenticated) |
| `POST` | `/webhook/{name}` | Fire-and-forget named webhook |
| `POST` | `/invoke/{agent}` | Synchronous agent invocation (returns response) |

Webhook and invoke endpoints accept JSON bodies (capped at 64 KiB). Auth is
always on and **fail-closed**: a webhook trigger whose secret is missing
returns `401` rather than serving open.

**Per-trigger webhook auth.** Each webhook trigger declares an `auth` mode:

- `hmac_body` (default) — `X-Webhook-Signature` = HMAC-SHA256 hex of the body,
  using the global secret.
- `static_header` — a shared secret compared against a request header (default
  `X-API-Key`), for services that can only send static headers. The per-trigger
  secret lives at `/data/webhook_secrets/<trigger-name>`.
- `timestamped_hmac` — a `t=<unix>,v0=<hex>` signature (default header
  `ElevenLabs-Signature`) within a tolerance window.

The per-trigger secret is generated when the trigger is registered — at startup
and on any reload that installs triggers — so it exists before the first call
rather than being created by it. Casa only ever creates a file that is absent,
so a trigger recreated under a name that was used before keeps the old secret
rather than being given a fresh one. If you would
rather supply the value yourself, declare
`secret_owner: provider` on a `timestamped_hmac` trigger and place the file by
hand: Casa never writes a slot it does not own. Set that at creation time —
changing the owner of an existing trigger is not supported, so delete it and
create it under a new name instead.

A turn started by a `/webhook/{name}` trigger is treated as **untrusted
third-party content**: it runs in a restricted runtime (no shell, filesystem,
network, plugins, or hooks — only public-clearance memory recall and an
operator-bound notification), reads memory at a reduced clearance, and writes no
memory. Operator-signed `/invoke` keeps full trust.

**Who a triggered turn is attributed to (0.129.0).** Both endpoints are opened
by a shared secret, which proves the caller holds a credential — never that a
particular person wrote the request. So neither is ever recorded as you. An
`/invoke` turn is attributed to `invoke_caller`; a webhook turn to
`webhook:<trigger name>`, so two triggers are never mistaken for each other.
Both are recorded as **automations** rather than as people, and a recalled
automation memory is introduced to the assistant as "An automation reported: …"
— never as something you said, and never as something Casa concluded itself.
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
installed together with it — the consent message lists each bundled plugin's
tools and the secrets it will need, so one tap covers the whole package.
Bundle-installed plugins belong to their specialist: they are private to it and
removed automatically when the specialist is uninstalled. Plugins you install
yourself with `plugin_add` are operator-owned — a specialist's lifecycle never
touches them, and plugin management tools refuse to modify a specialist's
bundled plugins (manage those through the specialist's own upgrade/uninstall).

## Voice pipeline

Casa exposes two transports for Home Assistant voice / generic voice clients. The HA-side integration that consumes them ships separately in `ha-casa-integration` (phase 2.4).

> **Voice is authenticated.** Both transports below are fail-closed: without a
> webhook secret every voice request is rejected with `401` — the same
> treatment as `/invoke` and `/telegram/update`. This matters because the
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
that file or logged by the publisher. Disabling webhook authentication removes
the published discovery record.

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
- **Engagements won't open (`engagement_not_configured`)**: See the "Troubleshooting engagements" subsection under [Engagements (v0.11.0)](#engagements-v0110) — most common cause is the bot missing "Manage topics" admin permission in the engagement supergroup.
- **Long-term memory not working**: Long-term recall requires the `hindsight_api_url` option to be set to a reachable Hindsight app (which auto-enables `MEMORY_BACKEND=hindsight`). If recall is empty, check that `hindsight_api_url` is set and the Hindsight app is running, and check container logs for Hindsight connection errors. With `hindsight_api_url` empty, only short-term per-session continuity works.
- **502 errors on ingress**: The Python process may still be starting. Wait up to 60 seconds after app start.

## DM button questions (v0.76.0)

Ellen (and the specialists she delegates to) can pause a turn to ask you a
quick multiple-choice question, posted as inline buttons right in your 1:1
Telegram DM — the same tap-to-answer pattern engagements use, without
opening a topic. The full answer choices are spelled out (numbered) in the
message itself, with short labels on the buttons underneath, so long options
are always readable and never truncated to the point of being unpickable
(v0.81.0). Tap an option and the agent picks up from there; a plain-text
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

## Engagements (v0.11.0)

Casa supports **engagements** — bounded conversational threads where a
specialist (Tier 2) or executor (Tier 3, Plan 3+) works with you on a
specific task, separate from your 1:1 chat with Ellen. Each engagement
lives in its own Telegram forum topic inside a dedicated supergroup.

The setup is a one-time Telegram configuration. Skip this section to
keep Casa running in 1:1-only mode (Ellen delegates synchronously and
returns a single response; `delegate_to_agent(mode="interactive")`
will return `engagement_not_configured`).

### Delegation limits

Every `delegate_to_agent` call, interactive or synchronous, is bounded by
two app options:

- `specialist_max_concurrency` (default `2`) — the total number of
  specialist delegations allowed in flight fleet-wide at once. Once the cap
  is reached, further delegation attempts are denied as "busy" until a slot
  frees up. A given calling scope (one voice session or chat) may also only
  have one active delegation to the same specialist at a time — that
  per-scope cap is fixed at 1 and is not configurable.
- `specialist_cost_alert_threshold` (default `5.0`, in USD) — once a
  specialist's cumulative delegated spend passes this figure, every further
  delegation to it logs an operator-visible warning (spend is not blocked,
  only flagged).

### Setup

#### 1. Create a dedicated forum supergroup

This must be a **different** chat from your 1:1 DM with the Casa bot.
Engagement topics live here, not in your personal chat.

1. In Telegram, tap the **✏️ pencil icon** (top right, most clients) → **New Group**.
2. Pick any co-owners you want (or just yourself) and give the group a name
   (e.g. "Casa Engagements"). Confirm.
3. Open the group's settings (tap the group name at top). Telegram will
   usually auto-convert small groups to a supergroup on first edit;
   if you see a **"Convert to supergroup"** button, tap it.
4. In group settings, find **"Topics"** (sometimes under "Group Type" or
   "Permissions"). Toggle it **ON**. The chat now shows individual topic
   threads instead of a single linear feed.

#### 2. Add the Casa bot as a topic-managing admin

1. Open the group → tap the group name → **Add Members** → search for your
   bot's `@username` → add it.
2. Tap the bot in the members list → **Promote to admin**.
3. Turn ON **"Manage topics"**. This permission is **required** —
   Casa refuses to enable engagements without it and logs
   `bot lacks can_manage_topics; engagements disabled`.
4. Other permissions ("Delete messages", pin, etc.) are optional —
   Casa does not require them; topic cleanup works with "Manage
   topics" alone (see step 6). Leave unused ones off to minimise the
   bot's surface.
5. Confirm. The bot is now a topic-managing admin.

#### 3. Find the supergroup's chat ID

Casa needs the **negative integer** Telegram assigns to the supergroup.

1. Post any message in the supergroup (e.g. "setup probe").
2. In a browser, open:
   `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
   (substitute your bot token from @BotFather — same one you put in
   `telegram_bot_token`).
3. Find the most recent `message` object. `message.chat.id` is your
   supergroup chat ID. It will be a negative integer starting with
   `-100`, e.g. `-1001234567890`.

Alternatives:
- Add a helper bot like `@getidsbot` or `@RawDataBot` to the supergroup
  temporarily; it will reply with the chat ID.
- If you already run Casa on the 1:1 chat, the app log also echoes
  the ID under the `CHANNEL_IN` traces when the bot sees any message
  in the supergroup.

#### 4. Configure Casa

1. Home Assistant → **Settings** → **Apps** (formerly Add-ons) → **Casa** → **Configuration**.
2. Set `telegram_engagement_supergroup_id` to the negative integer from step 3.
   Leave it at `0` to disable engagements (Casa still boots; interactive
   mode returns `engagement_not_configured`).
3. **Save** → **Restart** the app.

#### 5. Verify

Once the app has restarted, check the log for:

```
Engagement supergroup -100…: commands registered (['cancel', 'complete', 'silent'])
```

If you see this line, engagements are live. If you see:

```
Engagement supergroup -100…: bot lacks can_manage_topics; engagements disabled
```

the bot wasn't promoted correctly — go back to step 2 and re-check the
**Manage topics** toggle. The log line uses Ellen's level-ERROR — it's
easy to grep for.

In Telegram, inside the engagement supergroup, type `/` in any topic.
The autocomplete should list `/cancel`, `/complete`, and `/silent`.
These commands are registered via Telegram's `setMyCommands` scoped to
the supergroup only — they do NOT appear in your 1:1 DM with Ellen.

#### 6. Automatic topic cleanup (works out of the box)

Since v0.65.0 Casa deletes a finished engagement's topic automatically
**7 days after the engagement ends** — the same retention window as its
workspace — so the sidebar doesn't fill up with dead topics.

**No extra admin right is needed** (v0.65.1 correction): Telegram lets a
topic's *creator* delete it with the "Manage topics" right the bot
already has from step 2, and every engagement topic is created by the
bot. The **"Delete messages"** admin right is optional insurance — if a
deletion is ever refused for rights reasons, Casa keeps the topic
scheduled, retries at the next sweep, and Ellen asks you once per boot
to grant it.

Notes:

- **Deletion is irreversible.** Deleting a topic removes the topic and
  **all its messages for every member**. Casa's durable record of each
  engagement is its memory summary plus Ellen's completion message in
  your 1:1 chat (and the workspace artifacts during the 7-day window).
- **On-demand cleanup:** ask Ellen to *"clean up the engagement group"* —
  she delegates to the configurator, whose `cleanup_engagement_topics`
  tool purges known finished topics immediately (optionally as a dry
  run) without waiting out the retention window. Only topics Casa has
  on record are deleted; active engagements are never touched.
- **Topics from before v0.65.0** are unknown to Casa (the Telegram Bot
  API cannot enumerate a group's topics), so the existing pile needs
  **one manual cleanup** in the Telegram UI. From this release on, Casa
  keeps the sidebar clean automatically.

### Starting an engagement

Ask Ellen in the main 1:1 chat for something multi-turn, e.g.
*"let's work through Q2 invoicing with Alex"*. Ellen may open an
engagement — if so she'll tell you which topic to head to. The
specialist is waiting there, already primed with context.

You do **not** post in the main supergroup feed — always open an
existing topic thread. Casa automatically creates new topics as new
engagements start; they're named `#[<role>] <task> · <engagement-id>`.
If you accidentally post in the main feed, the bot will reply once
per boot with a redirect hint, then silently ignore further main-feed
messages.

### While the engagement runs (v0.81.0)

A pinned summary message sits at the top of the topic for the whole
engagement, leading with the current status — `⚙️ working` (with what it's
doing right now and elapsed time) or `⏳ waiting for your reply` while it's
your turn — followed by a short 2-3 word title, plan progress, and any
questions still waiting on you. The topic itself is named after that same
short title, so the topic list and the pinned summary always agree. Everything
else — the agent's narration and your exchange with it — reads below the
summary in strict chat order: as the agent works, its running narration
streams live and rolls to a new message rather than editing above anything
newer, so the topic always reads top-to-bottom in the order things actually
happened.

Every message you send gets an instant receipt, and the agent's reply is
quoted back to yours so it's always clear what it's responding to. If it
needs a decision from you, it asks one question at a time — via tappable
inline buttons when the options are enumerable, or a numbered free-text
question otherwise — and won't ask another while your last message is still
waiting to be read. The full answer choices are always spelled out in the
message itself (numbered), with short labels on the buttons underneath, so
long options are never truncated to the point of being unpickable. Once you
answer, the question visibly settles (its buttons disappear) instead of just
showing a toast.

Need to interrupt? Send `STOP` as the first line of a message to make the
agent drop what it's doing and check in with you, or prefix a message with
`redirect:` to both interrupt and tell it what to do instead.

### In-topic slash commands

| Command | What it does |
|---------|--------------|
| `/cancel` | End this engagement now. Topic is closed, the engaged agent's client is torn down, Ellen is notified in the main chat. |
| `/complete` | Mark this engagement complete without requesting an agent summary. Same cleanup as `/cancel` but with a neutral status. |
| `/silent` | Stop the observer from interjecting to Ellen about this engagement. The specialist keeps working in the topic. |

The engaged agent can also end the engagement itself by calling the
`emit_completion` MCP tool — that produces a structured summary
(text + artifacts + next_steps) which Ellen relays to you.

### Idle reminders

Engagements have no hard timeout. If an engagement sits idle for 3 days
(specialists) or 7 days (executors, Plan 3+), Ellen will nudge you in the
main 1:1 chat. Reminder re-fires weekly.

Suspend/resume is automatic — after 24 hours of inactivity Casa tears
down the underlying SDK client to free resources. It resumes seamlessly
on your next message in the topic (the conversation-session state is
persisted and reloaded). If two consecutive resume attempts fail (e.g.
the SDK session was rotated server-side), Casa marks the engagement
as errored and tells you to start a fresh one.

### Troubleshooting engagements

- **"Engagements disabled" / `engagement_not_configured` on delegate call.**
  Most common: the bot wasn't promoted to admin with **Manage topics**.
  Re-check step 2 of Setup. Also check `telegram_engagement_supergroup_id`
  is not `0` and the app was restarted after the option was set.
- **`/cancel` / `/complete` / `/silent` don't appear in autocomplete.**
  They're scoped to the supergroup only. Make sure you're typing `/` in
  a topic inside the engagement supergroup, not your 1:1 DM with Ellen.
  Also: Telegram caches `setMyCommands` client-side — restart the
  Telegram client if they don't show up within 30 seconds of app boot.
- **"No active engagement in this topic" reply in a topic.**
  The engagement for that topic has already completed, cancelled, or
  errored (registry status transition). Start a fresh engagement from
  your 1:1 DM with Ellen — do not reuse old topics.
- **"Could not resume this engagement" reply after 24h+ idle.**
  The suspended SDK session rotated before you came back. The
  engagement is marked as errored after two failed resumes. Start a
  new one; your prior conversation is still in Ellen's long-term
  memory.
- **Ellen doesn't narrate completion in the main chat.**
  Ellen receives the `ENGAGEMENT_COMPLETION` notification but chooses
  how to surface it based on her system prompt. If you want louder
  narration, edit `prompts/system.md` in Ellen's agent folder and
  restart.

## Configurator (v0.12.0)

The `configurator` is the first Tier 3 Executor - knows Casa's configuration surface and can CRUD it on your behalf. Ask Ellen for a configuration change; she opens a dedicated engagement topic where you talk directly to the configurator.

### What's supported

| Surface | Create | Read | Update | Delete |
|---|---|---|---|---|
| Specialist agents (Tier 2) | yes | yes | yes | yes |
| Specialists from a repository (install/upgrade/rollback/uninstall) | yes | yes | yes | yes |
| Resident agents (Tier 1) | rare | yes | yes | blocked by default |
| Per-agent YAMLs | yes | yes | yes | yes |
| Per-agent prompts | yes | yes | yes | yes |
| Triggers (cron, interval, webhook) | yes | yes | yes | yes |
| Delegate wiring | yes | yes | yes | yes |
| Policies (disclosure) | - | yes | yes | - |
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

- Eval running (configurator can shell to casa_eval, but no first-class recipe).
- Creating new executor types - waiting on Plans 4/5.

### Invocation

Ask Ellen for a configuration change in 1:1 chat. Examples:

- "Make a new specialist called fitness using sonnet"
- "Add a morning briefing trigger to yourself at 7am on weekdays"
- "Change Alex's prompt to be more concise"
- "Remove the garbage_reminder trigger"
- "Wire Alex into your delegates"

> **Reminders do not need the configurator.** Just ask Ellen directly — "remind me tomorrow before 9am to put the bins out", "remind me every Thursday at 7 to go to the gym" — and she sets it herself. She confirms the exact time, asks whether it should repeat when that is unclear, and can cancel it later.

Ellen opens a topic `#[configurator] <short task>` in your engagement supergroup. The configurator reads its doctrine, asks questions as needed, edits YAMLs, commits, and reloads Casa.

### Reload behavior

- hard - Supervisor app restart (~10-15s). Agent-shape changes, runtime, policy corpus.
- soft - In-process casa_reload_triggers(role). Trigger-only edits; no downtime.
- none - Prompt and doctrine edits take effect on the next turn. (How a resident
  writes or speaks is not a config edit at all — that comes from its persona.)

Hard reload: Ellen verifies the reload landed on her resumed turn, then narrates.

### Recipe discovery

Configurator reads short markdown recipes from its own doctrine tree at `/config/agents/executors/configurator/doctrine/`. Edit these recipes to customize per-instance (e.g., add house rules).

### Troubleshooting

- **"engagement_not_configured"** - you haven't set telegram_engagement_supergroup_id, or the bot doesn't have can_manage_topics.
- **Configurator stalls after first message** - check engagement's driver log. If you see prompt_template_missing, restart the app.
- **Hook denied, configurator cancelled** - expected for resident deletion. To override: edit configurator's hooks.yaml, commit, reload, retry.
- **Soft reload didn't take effect** - casa_reload_triggers requires the role to exist before the edit.
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
Claude Agent SDK. Executor engagements (e.g. plugin-developer) pin their exact
artifacts at launch and load them via `--plugin-dir`, so a plugin update never
changes the code a running engagement is already executing.

### Managing plugins (via the Configurator)

Ask Ellen for a plugin change; she opens a configurator engagement that uses
these tools:

- `plugin_add(name, repo, ref, subdir, targets, expected_revision?)` — publish
  a plugin's pinned artifact, install any system requirements it declares,
  assign it to targets, then reload and verify.
- `plugin_update(name, new_ref, expected_revision?)` — re-pin to a new release
  and re-verify. Plugin releases are identified by an annotated `vX.Y.Z` tag
  (v0.74.0); passing `expected_revision` (the commit the release was built at)
  makes a tag that moved afterwards abort before anything changes. The version
  is always read from the plugin's own manifest — you never supply it, so the
  stale-version class of bug is gone. Both tools report phase-aware outcomes
  (`activation_committed` / `runtime_ready`) so a "pin landed, reload pending"
  state is actionable.
- `plugin_assign(name, target)` / `plugin_unassign(name, target)` — change which
  agents load a plugin. Targets look like `resident:ellen`, `specialist:finance`,
  or `executor:plugin-developer`.
- `plugin_remove(name)` — drop a plugin from the registry (its artifact is left
  on disk for now; see disk usage).
- `plugin_list()` / `verify_plugin_state(name)` — inspect the registry and check
  that the running agents actually agree with it.

### Secrets

Unchanged from prior releases: a plugin declares required environment variables
via `${VAR}` references in its `.mcp.json`. When you add a plugin, the
configurator reports the required variables and asks for a 1Password reference
(`op://…`) for each, stored in `plugin-env.conf`. Secret values never appear in
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
silently. Declared names must use the reserved `CASA_PLUGIN_` prefix:
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
`ONEPASSWORD_DEFAULT_VAULT`, `CONTEXT7_API_KEY` — the fix is the matching
**app option**, not `plugin-env.conf`; the message names the option.

### Who runs the setup tool (v0.161.0)

**Casa does, and only Casa.** You never need to ask an agent to run a setup
tool, and no agent will offer to. When a plugin declares `casa.setupTool`, Casa
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
4. **Register the exact redirect URI with the provider.** The plugin shows you
   the precise URI to paste into the provider's app settings (it is your
   `public_url` joined with `/callback/<effective-name>`). Providers match this
   value byte-for-byte, so register exactly what the plugin displays.

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
bit on the artifact tree. For a `claude_code` executor, that is now a real
filesystem/privilege boundary rather than only defense-in-depth: its subprocess
runs under its own dropped, capability-stripped OS user (see [Executor
isolation](#executor-isolation-v01700)), which can neither write those files
nor `chmod` them back writable. It remains best-effort for any caller class
outside that isolation.

### Fresh install & rollback

The registry (`/config/plugins/registry.json`) is the single source of truth. On
a fresh install Casa seeds it with the bundled default plugins; a newer release
adds any newly-introduced defaults on the next boot, and a default you remove is
never re-added. Rollback is safe — the registry format is stable across
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
from casa-org/casa-specialist-finance"*.

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
You can also swap which persona an installed specialist uses (its bundled
default, or another installed persona) by asking Ellen to apply a
different persona to it.

## Claude Code driver (v0.13.1)

Plan 4a infrastructure — does not change user-facing behavior by itself.
Enables Tier 3 executors (plugin-developer, ha-developer) to run inside
real Claude Code CLI sessions, driven through their Telegram engagement
topics.

### Architecture

Each `driver: claude_code` engagement becomes its own s6-rc-supervised
service inside the app container. s6 owns supervision; Casa orchestrates
lifecycle via `s6-rc-compile` + `s6-rc-update`. Engagement subprocesses
outlive Casa-main restarts (service dependencies are ordering-only, not
lifetime-coupled).

- **Workspace:** `/data/engagements/<id>/` — CLAUDE.md, `.mcp.json`,
  isolated `$HOME`, named FIFO for Casa → CLI turn delivery. Assigned plugins
  are pinned at launch and loaded from the immutable store via repeated
  `--plugin-dir` flags (see [Plugins](#plugins-v0710)), not symlinks.
- **Service dirs:** `/data/casa-s6-services/engagement-<id>/` — `run` script
  + `type: longrun` + ordering dependency on `init-setup-configs` — plus a
  sibling logger service `engagement-<id>-log/` wired to it via
  `producer-for`/`consumer-for` (an s6-rc pipeline). Both are created and
  removed together; don't delete one without the other.
- **Logs:** the CLI's stdout+stderr land in `/var/log/casa-engagement-<id>/`
  (`s6-log`, 1 MB × 20 rotation), survive per-turn respawns, and are removed
  with the workspace when retention expires.
- **Auth:** `CLAUDE_CODE_OAUTH_TOKEN` flows via s6-overlay's `/command/with-contenv`.
  No `ANTHROPIC_API_KEY` path.

### Executor isolation (v0.170.0)

Each `claude_code` engagement is allocated its own dedicated, never-reused OS
user (a monotonic uid counter, never reset or reused across restarts). Its
workspace is `chown`ed to that user before anything is planted in it, and the
run script's final `exec` drops privilege via `setpriv` — clearing the
supplementary groups, emptying the capability bounding set, and setting
`no_new_privs` — before handing off to the real `claude` CLI, which therefore
never runs as root and can never regain a capability. An engagement is refused
outright (never started as root, never left to crash-loop under its
supervisor) if any part of that chain — `setpriv` itself, a valid allocated
uid, correct workspace ownership, a matching system-user entry, or a
readable plugin directory — cannot be established. One consequence: a
same-container engagement can no longer read another engagement's workspace
or credential file directly, closing that off at the filesystem level rather
than relying only on the token check below.

### Security caveat

The `block_token_exfiltration` and `block_credential_file_reads` hook
policies are speedbumps against casual prompt injection, not
defense-in-depth. They do not stop determined malicious prompts (indirect
env reads via `/proc/self/environ`, Write-then-exec, variable obfuscation,
HTTP exfil via any allowed tool). The real perimeter is **trust in the
executor's prompt scope and minimal `tools.allowed` list**. Do not engage
a `claude_code` executor with a prompt from an untrusted source.

### MCP HTTP bridge (v0.13.1)

Real `claude` CLI subprocesses reach Casa's MCP tools via
`POST /mcp/casa-framework` — stateless JSON-RPC 2.0 over HTTP, no SSE.
Engagement identity propagates through the `X-Casa-Engagement-Id` header
plus the per-engagement secret `X-Casa-Engagement-Token` (both written
into `.mcp.json` by `provision_workspace`); the id claim is honored only
when the token matches the engagement record's credential, and then the
bridge binds `tools.engagement_var` for the tool call's duration. A known
id with a missing/wrong token is rejected outright
(`engagement_auth_failed`) — the id alone is endpoint-visible and must
never confer authority. Missing / unknown header → bound to `None` and
engagement-gated tools return `not_in_engagement`. GET returns 405. The
same `CASA_TOOLS` tuple backs both the SDK path and the HTTP path, so
every in-process tool is automatically reachable from real CLI
engagements.

### Hook enforcement (v0.13.1, credential-bound since v0.139.0)

`/hooks/resolve` is the CC-side counterpart to the in-process hook layer.
`hook_proxy.sh` POSTs the CC hook payload (policy name + payload) to the
resolver, presenting the engagement credential pair from its own
workspace `.mcp.json` as the same `X-Casa-Engagement-Id` /
`X-Casa-Engagement-Token` headers the MCP bridge uses. The resolver
authenticates any identity claim against the engagement record before
selecting per-executor hook parameters (from the executor's `hooks.yaml`)
or invoking an identity-consuming policy such as the engagement
permission relay; the payload's `cwd` is only cross-checked, never
trusted as identity. An unauthenticated request falls back to the
default-configured policy callbacks. Callback exceptions deny (not
fail-open). Unknown policy denies. Matcher mismatch returns an empty
`{}` (CC allow).

### Workspace lifecycle (v0.13.1)

- **Provisioning:** `/data/engagements/<id>/.casa-meta.json` is written
  with `status: "UNDERGOING"` on engagement start.
- **Termination:** `_finalize_engagement` updates status to
  `COMPLETED` / `CANCELLED` / `ERROR` and writes `retention_until =
  now + 7 days`.
- **Sweeper:** APScheduler job (id `workspace_sweep`) runs every 6 hours,
  deletes terminal workspaces past `retention_until`. Missing `.casa-meta.json`
  or missing `retention_until` → leave alone (operator prunes explicitly
  via the MCP tool). The N150 has > 30 GB free so disk-pressure aggressive
  mode is not implemented.

### Workspace inspection MCP tools (v0.13.1)

All three are exposed on both the SDK and HTTP paths:

- `list_engagement_workspaces(status?)` — enumerate `/data/engagements/`
  entries with status, size, created/finished/retention timestamps.
  Truncates at 100. Optional status filter.
- `delete_engagement_workspace(engagement_id, force=false)` — delete a
  workspace. Refuses UNDERGOING without `force=true`; with `force=true`
  cancels + finalizes first, then rmtrees.
- `peek_engagement_workspace(engagement_id, path?, max_bytes?)` —
  read-only. Empty path returns a 3-deep tree listing; otherwise reads
  file contents up to `max_bytes` (default 64 KB, hard cap 512 KB).
  Path-traversal guarded via resolved-path containment check.

### Boot-replay heal (v0.13.1)

When a UNDERGOING engagement's s6 service dir is missing but the workspace
still exists AND the executor type is in `executor_registry`, boot replay
re-renders the run + log/run scripts and re-plants the service dir. Missing
workspace still warns and skips — operator must cancel manually or let the
sweeper collect after retention. Missing executor in registry also warns
and skips.

### Known limitations

- Per-executor hook parameters (e.g. `casa_config_guard.forbid_write_paths`)
  on the HTTP path use factory defaults rather than the executor's
  `hooks.yaml` values. The Configurator's defaults happen to match what
  it wants; wiring YAML params into the HTTP path is tracked as a later
  item.

### Boot replay

On Casa boot, `replay_undergoing_engagements` reconstructs the s6
supervision tree: sweeps orphan service dirs (engagements not UNDERGOING),
compiles + updates once, starts each remaining service, and spawns
URL-capture + respawn-poller tasks. Self-healing: a finalize that died
mid-teardown leaves an orphan that the next boot removes.

### Idle + resume

Engagement subprocesses idle via s6-supervision (no idle timeout in the
driver). On HA host reboot, `/data/` persists; boot replay re-launches
every UNDERGOING engagement's service, and the `run` script reads the
persisted `.session_id` to resume the CLI conversation exactly where it
left off.

## svc-casa-mcp service (v0.14.0)

Casa runs the `casa-framework` MCP server as a separate s6-supervised
service called `svc-casa-mcp`, listening on `127.0.0.1:8100` inside the
app container. Engagement subprocesses connect to it via the URL
written into their `.mcp.json` at provisioning time.

**Why it exists.** Engagement subprocesses survive casa-main restarts
(app updates, container respawns, HA reboots) because they run under
their own s6 services. Before v0.14.0, the MCP server lived inside
casa-main, so a casa-main restart dropped every live MCP connection
and any mid-turn tool call would fail with a connection error. With
the extracted service, the engagement's MCP TCP connection stays open
across casa-main restarts; mid-restart tool calls return JSON-RPC
`-32000 casa_temporarily_unavailable` (a clean recoverable error the
model can handle), not a connection drop.

**On-disk artifacts.**
- `/run/casa/internal.sock` (mode 0600) — Unix socket created by
  casa-main; svc-casa-mcp connects to it for every forwarded tool call.
- New engagement workspaces' `.mcp.json` points at
  `http://127.0.0.1:8100/mcp/casa-framework` and includes the
  `X-Casa-Engagement-Id` + `X-Casa-Engagement-Token` header binding
  (the token authenticates the id claim; boot replay refreshes the file
  from the engagement record before the CLI respawns).
- New engagement workspaces' hook proxy script POSTs to
  `http://127.0.0.1:8100/hooks/resolve`.

**Operational env-var overrides.**
- `CASA_FRAMEWORK_MCP_URL` — overrides the default URL that gets baked
  into newly-provisioned workspaces' `.mcp.json`. Leave unset for the
  shipped default.
- `CASA_HOOK_RESOLVE_URL` — overrides where `hook_proxy.sh` POSTs hook
  decisions.

## Plugin consumer infrastructure (v0.71.0)

Plugins are managed through the unified registry + immutable store — see
[Plugins](#plugins-v0710) for the full model. There is no marketplace and no
`enabledPlugins`: the registry (`/config/plugins/registry.json`) is the single
assignment authority, and each pinned plugin resolves to an immutable artifact
under `/config/plugins/store/<name>/<artifact-id>/`. The five defaults
(superpowers, plugin-dev, skill-creator, mcp-server-dev, context7) are seeded
from the app image and assigned to the plugin-developer executor.

## 1Password integration (v0.14.1)

Set `onepassword_service_account_token` (from https://developer.1password.com/docs/service-accounts/)
as a plaintext app option — it's the single root of trust and cannot
self-reference. Set `onepassword_default_vault` to the vault name (default
"Casa"). Every other password-typed option (`claude_oauth_token`,
`telegram_bot_token`, `webhook_secret`, `context7_api_key`) accepts either
plaintext OR an `op://` reference.

Plugin env vars resolved via `plugin-env.conf` (`/config/plugin-env.conf`),
managed by Configurator.

## Plugin-developer (v0.14.1)

Ask the primary assistant to build a plugin. It engages plugin-developer in
a dedicated Telegram topic. Plugin-developer asks public/private, authors
the plugin in its own GitHub repo, pushes, and emits completion. Assistant
relays; on your confirm, Configurator adds it to the registry (`plugin_add`)
and assigns it to the target agents + asks for secrets via 1P Q&A.

Prerequisites:

- `onepassword_service_account_token` set (plaintext).
- 1P item titled exactly `GitHub` in the vault named by
  `onepassword_default_vault` (default `Casa`), with a field labeled
  `credential` holding a GitHub PAT with `repo` scope. Plugin-developer
  resolves `op://${onepassword_default_vault}/GitHub/credential` at
  engagement spawn — no separate app option.
