# Casa

An always-on fleet of Claude-powered AI agents inside your Home Assistant.

- **Ellen** — the primary agent: chat with her on Telegram (streaming replies,
  interactive engagements in forum topics, inline permission prompts), let her
  control the home through the Home Assistant APIs, and delegate bigger jobs to
  specialist subagents.
- **Tina** — the voice agent: fast, concise responses tuned for Home Assistant
  Assist voice pipelines (SSE / WebSocket).
- **Specialists** — on-demand subagents for focused tasks (building automations,
  research, configuration work), each with scoped tool permissions.

Also on board: cron-style scheduling with real timezone handling, optional
built-in long-term memory, secrets as 1Password `op://` references,
a web terminal in the ingress panel, HMAC-authenticated webhooks, and a custom
AppArmor profile.

Requires a **Claude Max subscription** — run `claude setup-token` on your
workstation and paste the OAuth token into the app configuration — and an
**amd64** or **aarch64** machine.

Setup guides, the full configuration reference, and troubleshooting live in the
**Documentation** tab ([DOCS.md](DOCS.md)).
