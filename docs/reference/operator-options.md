---
last_reviewed: 2026-07-31
---

# Operator options

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The option-by-option contract for the app manifest: each key, the environment variable it
becomes, the code that consumes it, its default, and whether a change needs a restart. The
enumeration itself is machine-checked — the coverage ledger fails when an option exists
that this corpus does not account for — but the semantics columns here are prose, so code
wins where they drift.

## Mental model

**The manifest declares; the boot scripts project.** The service run script reads every
key at container start and exports the startup environment; the setup one-shot handles the
special cases — pruning removed keys' stored values, minting and persisting the webhook
secret when none is configured, and exporting model parity values for config-sync. The
Python configuration module does *not* read add-on options: its only environment use is
generic placeholder substitution, and model options enter through the role-slot layer.

**Every option is restart-required.** No in-process reload scope rereads add-on options
(INV-CFG-001); values are read once by boot scripts and process initialization. The reload
system covers repository and plugin configuration, not the manifest.

**Every option read is "null"-normalized.** bashio returns the literal string "null" for a
key absent from the stored options; every read in the run script guards that sentinel
before exporting. One group exports conditionally — the Hindsight URL, the Telegram API
base, the webhook secret, the Context7 key, the timezone, the log level and the reap TTL
are exported only when set to a real value; the public URL is normalized the same way but
always exported, empty when unset. The rest export unconditionally after normalization —
empty (or the script-side fallback) when the key is deleted, never the "null" literal.
Only the webhook secret is explicitly unset before its export decision, so an inherited
container variable can survive an empty option elsewhere.

## The options

| Key | Env | Consumer | Default | Change needs |
|---|---|---|---|---|
| `public_url` | `PUBLIC_URL` | main startup (webhook transport base); the authorization-callback redirect base, where it must additionally be a clean `https://` origin (no IP literal, userinfo, path or control character) or the callback facility is unavailable | empty | restart |
| `claude_oauth_token` | `CLAUDE_CODE_OAUTH_TOKEN` | main startup secret resolution; inherited by CLI children | empty — but **boot-required**: validation refuses to start without a value | restart |
| `telegram_bot_token` | `TELEGRAM_BOT_TOKEN` | main startup (channel construction) | empty | restart |
| `telegram_chat_id` | `TELEGRAM_CHAT_ID` | main startup → Telegram channel | empty | restart |
| `telegram_engagement_supergroup_id` | `TELEGRAM_ENGAGEMENT_SUPERGROUP_ID` | main startup; engagement configuration check | 0 | restart |
| `telegram_bot_api_base` | `TELEGRAM_BOT_API_BASE` | Telegram channel rebuild | unset | restart |
| `telegram_transport` | `TELEGRAM_TRANSPORT` | main startup | polling | restart |
| `hindsight_api_url` | `HINDSIGHT_API_URL` (the run script derives `MEMORY_BACKEND=hindsight` from it; `hindsight` without a URL is a startup error, an unknown backend value warns to no-op) | memory backend selection | empty → no-op memory | restart |
| `onepassword_service_account_token` | `OP_SERVICE_ACCOUNT_TOKEN` | secret resolver (`op` CLI); setup's token blocks | empty | restart |
| `onepassword_default_vault` | `ONEPASSWORD_DEFAULT_VAULT` | setup's GitHub-token block; the configurator's vault tools fall back to it when their `vault` argument is omitted | Casa | restart |
| `webhook_secret` | `WEBHOOK_SECRET` (else the persisted secret file) | setup's secret block; main startup | generated and persisted when empty | restart |
| `context7_api_key` | `CONTEXT7_API_KEY` | Context7 MCP subprocess (plugin-env loading may overwrite) | empty | restart |
| `primary_agent_model` | `PRIMARY_AGENT_MODEL` | role-slot model options → role model resolution | opus | restart |
| `voice_agent_model` | `VOICE_AGENT_MODEL` | role-slot model options → role model resolution | haiku | restart |
| `enable_terminal` | `ENABLE_TERMINAL` | nginx setup, ttyd service, dashboard | false | restart |
| `casa_tz` | `CASA_TZ` | timekeeping — both scheduler wall-clock and the current-time block every agent turn sees | empty → `TZ` (Home Assistant's zone), then UTC | restart |
| `engagement_reap_days` | `ENGAGEMENT_REAP_DAYS` | engagement reaper | 7 (0 disables) | restart |
| `log_level` | `LOG_LEVEL` | main startup logging; the standalone MCP service's logging | info (runtime falls back to INFO when absent/empty) | restart |
| `specialist_max_concurrency` | `SPECIALIST_MAX_CONCURRENCY` | specialist limiter (clamped 1–20) — the *fleet-wide* cap; a separate hard-coded rule allows exactly one active delegation per scope regardless | 2 | restart |
| `specialist_cost_alert_threshold` | `SPECIALIST_COST_ALERT_THRESHOLD` | specialist telemetry (malformed → default) — a cumulative per-role USD threshold that only *logs* on every result once exceeded; it caps and cancels nothing | 5.0 | restart |

## Failure behavior

**A missing Telegram token** means no Telegram channel — not a startup error. **Webhook
transport without a public URL** warns and falls back to polling; the same unset or
non-`https` origin leaves the authorization-callback facility unavailable, surfacing
`callback_base_url_invalid` on every otherwise-routable plugin. **A non-numeric
supergroup id** fails startup — that conversion is unguarded. **A failed `op://`
resolution** warns and leaves the raw reference in place, so the downstream credential is
rejected rather than the boot. **An invalid timezone** warns and falls back to the
default. **An invalid reap value** falls back to seven days. **The specialist rails** are
the exception to schema-only validation: concurrency is clamped to its range and a
malformed cost threshold falls back to the default at runtime (the schema rejects it at
input time) — the other options trust the schema and the operator.

## Extension points

**Adding or removing an option** means editing `options:` and `schema:` together; a
removal additionally appends the key to the deprecated-keys prune list in the setup
script, or the stored value survives and Home Assistant warns forever. The coverage
ledger enumerates every key mechanically, so a new option that no document claims fails
CI until this file (or a better home) takes it.

**Making an option live-reloadable** is new machinery, not a flag: it needs an explicit
option-reload source and consumer reconfiguration — today's reload dispatcher has no
path that rereads the manifest.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/config.yaml::options`
- `casa/config.yaml::schema`
- `casa/rootfs/etc/s6-overlay/scripts/setup-configs.sh`
- `casa/rootfs/opt/casa/timekeeping.py::resolve_tz`
- `casa/rootfs/opt/casa/specialist_limits.py::SpecialistLimiter`

**Tests**
- `tests/test_run_script_env.py`

**Related**
- [`architecture/configuration.md`](../architecture/configuration.md)
- [`architecture/overview.md`](../architecture/overview.md)
<!-- END SOURCEMAP -->
