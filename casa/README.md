# Casa

An always-on fleet of Claude-powered AI agents inside your Home Assistant.

- **Ellen** — the assistant: chat with her on Telegram (streaming replies,
  background jobs you steer from the same chat, inline permission prompts). She passes
  house control to Tina and bigger jobs to specialists and to Casa's configurator.
- **Tina** — the butler: voice-first house control in fast, concise responses,
  tuned for Home Assistant Assist voice pipelines (SSE / WebSocket).
- **Gary** — the concierge: a voice agent for anyone in the room, for general
  questions, with no house control and no private data.
- **Specialists** — agents you add from their own repositories for focused work
  (household finances, a Magic: The Gathering rules judge), each with scoped tool
  permissions. None are installed by default.

Also on board: cron-style scheduling with real timezone handling, built-in
long-term memory, secrets as 1Password `op://` references,
a web terminal in the ingress panel, HMAC-authenticated webhooks, and a custom
AppArmor profile.

Requires a **Claude Max subscription** — run `claude setup-token` on your
workstation and paste the OAuth token into the app configuration — and an
**amd64** or **aarch64** machine.

Setup guides, the full configuration reference, and troubleshooting live in the
**Documentation** tab ([DOCS.md](DOCS.md)).
