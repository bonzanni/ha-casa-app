<div align="center">

<img src="assets/casa-banner.png" alt="Casa — helpful AI for everyday life" width="356">

# Casa — Claude-powered agents for Home Assistant

**An always-on fleet of AI agents that help keep your life manageable — an assistant, a butler, a concierge, and specialists you add for whatever else you need. Reach them on Telegram or by voice. Home Assistant is where they live; your home is one of the things they look after.**

[![Open your Home Assistant instance and show the app store with this repository pre-filled.](https://my.home-assistant.io/badges/supervisor_store.svg)](https://my.home-assistant.io/redirect/supervisor_store/?repository_url=https%3A%2F%2Fgithub.com%2Fbonzanni%2Fha-casa-app)

[![QA](https://github.com/bonzanni/ha-casa-app/actions/workflows/qa.yml/badge.svg)](https://github.com/bonzanni/ha-casa-app/actions/workflows/qa.yml)
[![Version](https://img.shields.io/badge/dynamic/yaml?url=https%3A%2F%2Fraw.githubusercontent.com%2Fbonzanni%2Fha-casa-app%2Fmain%2Fcasa%2Fconfig.yaml&query=%24.version&label=version&color=blue)](casa/CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Supports aarch64 Architecture](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64 Architecture](https://img.shields.io/badge/amd64-yes-green.svg)

</div>

Casa packages the [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview)
as a Home Assistant app (formerly known as an add-on). Home Assistant is the vehicle —
always-on hardware you already own, a Supervisor to keep the container alive, and a voice
pipeline reaching every room. What runs inside is broader than the house: agents that
answer questions, remember things, run errands on a schedule, and hand specialised work
to specialists you install.

---

## Why Casa

2026 made the case for the personal AI agent: OpenClaw and the wave of harnesses behind
it showed how much people want an always-on assistant they can message, one that
remembers and acts rather than just answers. It also showed the cost of getting the
architecture wrong: one agent holding its owner's entire digital life,
[running unsandboxed with full user privileges](https://arxiv.org/html/2603.12644v1),
extended by
[marketplace skills that turned out to include hundreds of malicious packages](https://www.acronis.com/en/tru/posts/openclaw-agentic-ai-in-the-wild-architecture-adoption-and-emerging-security-risks/).
That trade-off may be acceptable on a tinkerer's laptop. It is the wrong shape for a
household — a place with family members, guests, kids, and a home the agent can
physically act on. Casa is built from the opposite premise: **trust is tiered, not
total**. A private assistant, a house butler, and a guest-safe concierge are different
agents with different permissions — not one mind with everything. Extensions install
pinned by checksum after explicit consent; executor sessions that run code are dropped
to unprivileged, never-reused identities; the most powerful tools answer to a single
operator, on an authenticated channel. And instead of living as a process on your laptop, Casa is a native Home
Assistant app: installed from the app store as a signed image, kept alive by the
Supervisor, updated like the rest of your home, speaking through the same Assist voice
pipeline as every satellite in the house — contained, always on, and looked after, like
the appliance a household agent should be.

---

## Contents

- [Highlights](#highlights)
- [Getting started](#getting-started)
  - [1. Install the app](#1-install-the-app)
  - [2. Get your Claude token](#2-get-your-claude-token)
  - [3. Connect Telegram](#3-connect-telegram)
  - [4. Enable engagements](#4-enable-engagements)
  - [5. Add voice *(optional)*](#5-add-voice-optional--talk-to-the-fleet-through-home-assistant-assist)
  - [6. Turn on long-term memory *(optional)*](#6-turn-on-long-term-memory-optional--the-fleet-works-without-it-but-forgets-between-conversations)
  - [7. Reference secrets from 1Password *(optional)*](#7-reference-secrets-from-1password-optional--for-the-security-conscious)
  - [Requirements recap](#requirements-recap)
- [The fleet](#the-fleet)
- [Growing your own fleet](#growing-your-own-fleet)
- [The Casa ecosystem](#the-casa-ecosystem)
- [Documentation & support](#documentation--support)
- [Development](#development)

---

## Highlights

### 🤖 Agents

- **Three residents out of the box** — an assistant you chat with, a voice-first butler
  for the house, and a guest-safe concierge that visitors and kids can talk to.
- **Specialists you add** — install an agent for [the family finances](#specialists),
  your health and training, a hobby; then "ask Ellen when the car insurance renews"
  just works.
- **Executors for real work** — a configurator that changes Casa's own settings when you
  ask in chat, and a plugin-developer that builds new plugins for you.
- **Personas** — swap how an agent sounds and behaves without changing what it's allowed
  to do; [five demo characters](#personas) are ready to try.

*Deep dive: [agent taxonomy](docs/architecture/agent-taxonomy.md) · [personas](docs/architecture/personality.md)*

### 💬 Channels

- **Telegram** — streaming replies, slash commands, tappable buttons, and dedicated forum
  topics for longer-running work.
- **Voice** — talk to the fleet from any room with an Assist satellite; slow answers still
  reach the right speaker after the fact.
- **Not just humans** — an authenticated invoke API lets Home Assistant automations and
  outside services send a message to an agent, same as you would.

*Deep dive: [Telegram](docs/architecture/telegram.md) · [voice](docs/architecture/voice.md) · [HTTP surface](docs/architecture/http-surface.md)*

### 🏠 Home Assistant

- **Control your home in plain language** — lights, climate, locks, media, sensors,
  through the entities you've already exposed to Assist; no second allowlist to maintain.
- **Voice built in** — the [companion integration](#companion-apps--integrations) plugs
  the fleet into Home Assistant's Assist pipeline, so any voice satellite in the house
  reaches an agent.
- **A well-behaved app** — built the way Home Assistant wants apps built: prebuilt signed
  multi-arch images from the app store, Supervisor-managed updates, ingress-only UI, a
  custom AppArmor profile, and entity exposure honoured from HA's own settings rather
  than reinvented.

*Deep dive: [Home Assistant control](docs/architecture/home-assistant-control.md)*

### ⏰ Autonomy

- **Reminders that keep their promise** — "remind me to take the bins out Tuesday
  evening" works mid-conversation; reminders are durable, surviving restarts and updates.
- **Routines** — interval, cron and date schedules let agents run recurring errands — a
  morning briefing, a weekly spending summary — without being asked.
- **Reacts to the world, not just to you** — webhook triggers and plugin events wake an
  agent when something happens: an automation fires, a new email lands, an outside
  service calls back.
- **Work survives the box** — long-running jobs and their results are persisted; a
  restart mid-task means the answer still arrives afterwards, on the channel it was
  meant for.

*Deep dive: [triggers](docs/architecture/triggers.md) · [reminders](docs/architecture/reminders.md) · [jobs & delivery](docs/architecture/jobs-and-delivery.md) · [plugin events](docs/architecture/plugin-events.md)*

### 🧠 Memory

- **Long-term memory** *(optional)* — agents remember what matters across conversations —
  names, preferences, the plumber's number — backed by a
  [Hindsight](https://github.com/vectorize-io/hindsight) server,
  [itself installable as a Home Assistant app](#companion-apps--integrations).
- **Memory that understands, not just stores** — Hindsight extracts durable facts from
  conversation and recalls them by meaning, so "what did we decide about the kitchen?"
  works without magic keywords.
- **Inspectable** — Hindsight's control-plane UI lets you see and curate what the fleet
  has remembered about your household.
- **Privacy-tiered recall** — what your assistant knows privately is not what a
  guest-facing voice agent can repeat to whoever is in the room.

*Deep dive: [memory and recall](docs/architecture/memory.md)*

### 🧩 Extensibility

- **Everything installs from git** — specialists, plugins and personas come from
  repositories you name ([a growing set is public today](#the-casa-ecosystem)), pinned by
  checksum, installed only after you consent.
- **The fleet extends itself** — need a capability that doesn't exist? The
  plugin-developer agent scaffolds, tests and ships a new plugin from a conversation;
  personas and specialists can be authored the same way.
- **Conversational administration** — describe what you want; the configurator makes the
  change and reloads Casa.
- **Plugins bring tools** — Claude Code plugins add tools, skills and MCP servers to
  specific agents, like [Gmail for the household mailbox](#plugins).

*Deep dive: [plugins](docs/architecture/plugins.md) · [specialist lifecycle](docs/architecture/specialist-lifecycle.md)*

### 🔒 Security

- **Agents are contained, not trusted** — each agent runs with its own tool permissions,
  an installed specialist's tool surface is clamped by a code-owned ceiling it cannot
  grant its way past, and executor sessions that run code are dropped to an
  unprivileged, never-reused identity.
- **Powerful tools are gated** — protected tools require authorization from the one
  configured operator; guest-facing agents get no house control and no private data by
  construction.
- **OAuth without the plumbing** — a built-in authorization-callback facility lets
  plugins complete OAuth flows to outside services, redirect leg included, with tokens
  handled for you and kept out of your config.
- **Secrets stay out of config** — reference credentials as 1Password `op://` URIs
  instead of pasting tokens; secrets are redacted from logs.
- **Defence in depth** — custom AppArmor profile, ingress-only UI, authenticated
  webhooks (HMAC or static header), HMAC-authenticated voice routes, Cosign-signed
  images.

*Deep dive: [tools & authorization](docs/architecture/mcp-and-tools.md) · [authorization callbacks](docs/architecture/callbacks.md)*

---

## Getting started

Strictly speaking, Casa boots with just a Home Assistant installation that can run apps
and a Claude token — but the fleet is meant to be reached, so a real install includes
Telegram and engagements (steps 1–4). Voice, memory and secrets are optional extras you
can add later, one step at a time.

### 1. Install the app

1. Click the button at the top of this page, or add the repository manually:
   **Settings → Apps → App Store → ⋮ → Repositories** (on older Home Assistant versions:
   **Settings → Add-ons → Add-on Store**) → `https://github.com/bonzanni/ha-casa-app`.
2. Find **Casa** in the store and click **Install**. Installs pull a prebuilt,
   Cosign-signed container image from GHCR — no on-device build.

### 2. Get your Claude token

Casa's agents run on your Claude Max subscription.

1. On your workstation, install [Claude Code](https://claude.com/claude-code) if you
   don't have it.
2. Run `claude setup-token` and follow the browser login; copy the token it prints.
3. In **Settings → Apps → Casa → Configuration**, paste it into `claude_oauth_token`
   and **Save**.

You can start the app now to check it boots — but carry on: Telegram is how you'll
actually talk to the fleet.

### 3. Connect Telegram

1. Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, pick a name
   and a username; copy the bot token it gives you.
2. Put it in `telegram_bot_token`.
3. Send your new bot any message, then set `telegram_chat_id` to your own chat ID (ask
   [@userinfobot](https://t.me/userinfobot), or read it from Casa's log). **Don't leave
   this empty**: this ID is the operator identity — it decides who can authorize
   protected tools and read private memory. Empty means an open bot where nobody is the
   operator.

### 4. Enable engagements

Longer-running work — the configurator, the plugin-developer, interactive specialist
sessions — happens in dedicated topics inside a Telegram forum supergroup:

1. Create a new Telegram group (separate from your 1:1 chat with the bot) and enable
   **Topics** in its settings — that makes it a forum supergroup.
2. Add your Casa bot to it and promote it to admin with **Manage topics** — the one
   permission Casa requires.
3. Find the group's chat ID (a negative number starting `-100…`): post a message in the
   group and check `https://api.telegram.org/bot<TOKEN>/getUpdates`, or add
   [@getidsbot](https://t.me/getidsbot) temporarily.
4. Set `telegram_engagement_supergroup_id` to that ID.

The [full walkthrough](casa/DOCS.md#engagements-v0110) covers each step in detail,
including verification and automatic topic cleanup.

### 5. Add voice *(optional — talk to the fleet through Home Assistant Assist)*

1. Install the companion
   [`ha-casa-integration`](https://github.com/bonzanni/ha-casa-integration) via HACS.
2. Add the **Casa** integration under **Settings → Devices & Services** — Casa publishes
   its authenticated endpoint through Supervisor discovery, so the integration finds the
   running app and its signing secret on its own.
3. Pick a Casa agent as the conversation agent in your Assist pipeline; every voice
   satellite using that pipeline now reaches the fleet.

Details, transports and troubleshooting: [voice pipeline](casa/DOCS.md#voice-pipeline).

### 6. Turn on long-term memory *(optional — the fleet works without it, but forgets between conversations)*

1. Install [`ha-hindsight-app`](https://github.com/bonzanni/ha-hindsight-app) — a
   Hindsight memory server packaged as a Home Assistant app — and start it.
2. Set `hindsight_api_url` to its internal address (e.g. `http://<app-hostname>:8888`).
   That single option is the switch: set it and agents start remembering across
   sessions; leave it empty and short-term continuity still works.

### 7. Reference secrets from 1Password *(optional — for the security-conscious)*

Instead of pasting tokens into the configuration, set
`onepassword_service_account_token` (from a
[1Password service account](https://developer.1password.com/docs/service-accounts/)) and
write any secret option as an `op://vault/item/field` reference. Casa resolves them at
startup; plaintext never lands in your config. Details:
[1Password integration](casa/DOCS.md#1password-integration-v0141).

### Requirements recap

- Home Assistant OS or a Supervised installation (the app needs the Supervisor),
  Home Assistant 2025.4 or newer, on an **amd64** (x86-64) or **aarch64** (arm64)
  machine.
- A **Claude Max subscription** for the OAuth token the agents run on.
- A **Telegram bot** (free, two minutes with @BotFather) and a forum supergroup for
  engagements.
- Optional, per the steps above: the companion voice integration, a Hindsight server, a
  1Password service account.

Full configuration reference, every option explained: [Casa documentation](casa/DOCS.md).

---

## The fleet

Three long-lived **residents** ship in the image, each with a
[persona](docs/architecture/personality.md), a voice, a scope, and its own tool
permissions:

| Resident | Default persona | What it is for |
|---|---|---|
| **Assistant** | *Ellen* | The one you chat with on Telegram. General help, orchestration, delegation, reminders, memory. |
| **Butler** | *Tina* | Voice-first house control — lights, climate, locks, media, sensors. Short spoken answers, no small talk. |
| **Concierge** | *Gary* | A medium-trust voice agent for anyone in the room: general questions and delegated lookups, no house control and no private data. |

Beyond them, two tiers exist so the fleet can grow without the residents growing:

- **Specialists** — ephemeral, role-keyed agents a resident delegates to for focused work
  (finances, a hobby domain, a research area). They have no channel of their own; they
  answer the resident that called them, and they are
  [installed, upgraded, rolled back and uninstalled](docs/architecture/specialist-lifecycle.md)
  from git repositories — [several are public today](#specialists).
- **Executors** — task-bounded agents that get a dedicated Telegram topic to work in,
  through [engagements](docs/architecture/engagements.md). Two ship: the
  **configurator**, which edits Casa's own configuration on your behalf and reloads it,
  and the **plugin-developer**, which builds new plugins from scratch.

The full tier model — what each tier may do and what it takes to add an agent — is
specified in the [agent taxonomy](docs/architecture/agent-taxonomy.md).

---

## Growing your own fleet

The residents are deliberately a fixed, small set — the interesting growth happens around
them. Casa treats three things as installable components, each fetched from a git
repository you name (`owner/repo@ref`) and each acknowledged before anything is written:

- **Specialists** — a whole agent: prompt, scope, tools, delegation rules. Install one for
  a domain you care about and wire it into a resident's delegates, and from then on "ask
  Ellen about the invoices" reaches the finance specialist.
- **Plugins** — [Claude Code](https://claude.com/claude-code) plugins that add tools,
  skills and MCP servers, assigned to specific agents rather than the whole fleet. Tools a
  plugin marks as protected can only be authorized by the configured operator.
- **Personas** — the character and voice an agent wears, swappable without touching what it
  is allowed to do, and resettable to the shipped default. A persona changes how an agent
  sounds; capability comes from the role and the tool layer, never from prose.

Because the configurator is itself an agent, all of this is conversational: you describe
what you want, it opens a topic, makes the change, and reloads Casa. Building a *new*
plugin is the same shape — the plugin-developer executor scaffolds, tests and ships one.
The install and identity mechanics are documented in the
[architecture corpus](docs/README.md); feature direction is tracked in the open —
[issues](https://github.com/bonzanni/ha-casa-app/issues) are where it is discussed.

---

## The Casa ecosystem

Casa is one repository in a small constellation. Everything below is public and
installable today — specialists, plugins and personas install straight from these repos
by asking the configurator in chat.

### Companion apps & integrations

| Repository | What it gives you |
|---|---|
| [`ha-casa-integration`](https://github.com/bonzanni/ha-casa-integration) | The voice bridge — routes Home Assistant's Assist pipeline to the fleet, so any voice satellite reaches an agent. |
| [`ha-hindsight-app`](https://github.com/bonzanni/ha-hindsight-app) | Long-term memory as a Home Assistant app — a Hindsight server (API, control-plane UI, embedded Postgres) running next to Casa. |

### Specialists

| Repository | What it gives you |
|---|---|
| [`casa-specialist-finance`](https://github.com/bonzanni/casa-specialist-finance) | A household-finance specialist — reads your bank accounts (read-only, PSD2), keeps a local transaction ledger you can tag and annotate, and answers spending questions with arithmetic rather than recollection. |
| [`casa-specialist-mtg`](https://github.com/bonzanni/casa-specialist-mtg) | A Magic: The Gathering rules judge — citation-backed answers over an offline rules corpus. |

### Plugins

| Repository | What it gives you |
|---|---|
| [`casa-plugin-gmail`](https://github.com/bonzanni/casa-plugin-gmail) | Gmail for the assistant — read and act on the household mailbox, authorized per-user with OAuth. |

### Personas

Five demo packs that reskin an agent's character without touching its permissions
(fan-made, unaffiliated):

| Persona | Character |
|---|---|
| [`casa-persona-bruce`](https://github.com/bonzanni/casa-persona-bruce) | A Batman-inspired presentation layer. |
| [`casa-persona-marvin`](https://github.com/bonzanni/casa-persona-marvin) | A planetary intellect assigned to the light switches. |
| [`casa-persona-yoda`](https://github.com/bonzanni/casa-persona-yoda) | Patient inverted-syntax teacher of household chores. |
| [`casa-persona-gollum`](https://github.com/bonzanni/casa-persona-gollum) | Whispery hoarder who argues with himself and helps anyway. |
| [`casa-persona-terminator`](https://github.com/bonzanni/casa-persona-terminator) | Relentless mission machine reassigned to your groceries. |

---

## Documentation & support

- [Casa documentation](casa/DOCS.md) — setup, configuration reference, channels, memory,
  troubleshooting.
- [Architecture corpus](docs/README.md) — how the system actually works, one small
  document per subsystem, written for contributors and for the agents that help build it.
- [Changelog](casa/CHANGELOG.md)
- Found a bug or have a feature request?
  [Open an issue](https://github.com/bonzanni/ha-casa-app/issues).

---

## Development

Run `make setup` once on a fresh checkout (Linux/WSL2), then `make test-unit` for
the fast gate and `make test-docker` for the container-backed tiers. Changes land
via squash-merged pull requests; every release bumps `casa/config.yaml` and
adds a `casa/CHANGELOG.md` entry.

### AI-assisted development

Casa is a Claude-powered agent, and it is largely built with one: development
happens with [Claude Code](https://claude.com/claude-code), with every change
reviewed, tested, and shipped by the maintainer, who takes full responsibility
for the code. AI assistance is disclosed with `Assisted-by:` trailers in the
commit history.

---

## License & disclaimer

[MIT](LICENSE). This project is not affiliated with, endorsed by, or sponsored by
Anthropic, Nabu Casa, or the Home Assistant project. *Claude* is a trademark of
Anthropic, PBC.
