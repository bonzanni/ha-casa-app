# Casa architecture (what you're configuring)

## Directory layout

Everything you edit lives under `/config/`:

    /config/
    agents/
      <resident-role>/               # flat - tier 1 (e.g. assistant, butler)
        character.yaml
        runtime.yaml
        delegates.yaml
        disclosure.yaml
        response_shape.yaml
        voice.yaml
        triggers.yaml              # optional
        hooks.yaml                 # optional
        prompts/
          system.md                # REQUIRED to exist, read at load, NOT
                                   # served to a bundle-bound resident — not
                                   # editable; see recipes/prompt/resident.md
          <trigger-name>.md        # one per scheduled/webhook trigger
      specialists/
        <slug>/                    # tier 2 (e.g. finance) — MANAGED, do NOT hand-edit:
          character.yaml           # materialized/symlinked by the install pipeline
          runtime.yaml             # from /config/specialists/<slug>/ (see note below)
          response_shape.yaml
          voice.yaml
          hooks.yaml               # optional
          prompts/system.md
      executors/
        <type>/                    # tier 3 (e.g. configurator) - this is you
          definition.yaml
          prompt.md
          hooks.yaml               # optional
          observer.yaml            # optional
          doctrine/                # your own knowledge base
    policies/
      disclosure.yaml
    schema/
      *.v1.json                    # READ-ONLY - editing breaks loaders

Read-only to you (hook-blocked): `/data/**` (runtime state), `/config/schema/**`, `/opt/casa/**`.

**Installed specialists are managed, not hand-authored.** A specialist you
install lives in a separate content-addressed tree at `/config/specialists/<slug>/`
(an `active.yaml` tuple pinning component-id / version / root-digest, plus the
compiled bundle in the store). The install pipeline materializes/symlinks it into
`agents/specialists/<slug>/` for the loader — you never create or edit those files
by hand. `managed_component_guard` denies raw Write/Edit/Bash writes under
`/config/agents/specialists/`, `/config/specialists/`, `/config/personas/`, and
`/config/plugins/`. Add, upgrade, or remove a specialist ONLY through the pipeline
tools (`recipes/specialist/install.md`, `recipes/specialist/upgrade.md`,
`recipes/specialist/uninstall.md`). Legacy hand-authored specialist directories are
refused by the loader — `recipes/specialist/create.md` is a retired stub.

## Tier taxonomy

| Tier | Name | What it is | Where it lives |
|---|---|---|---|
| 1 | Resident | Long-lived agent owning a channel (Ellen=telegram+voice, Tina=voice). Has memory budget, delegates (to residents and specialists). | agents/<role>/ |
| 2 | Specialist | Role-keyed helper (e.g. finance/Alex). Called by residents via delegate_to_agent. No channel, ephemeral session. Installed from a repository via the pipeline; managed, not hand-edited. | agents/specialists/<slug>/ (materialized from /config/specialists/<slug>/) |
| 3 | Executor | Task-bounded, ephemeral agent (e.g. you - configurator). Engaged via engage_executor. Runs in a dedicated Telegram topic. | agents/executors/<type>/ |

Any resident may delegate (`delegate_to_agent`) to any other agent listed in its `delegates.yaml`. Only the assistant (Ellen) may engage executors via `executors.yaml`.

## Key files per tier

| File | Resident | Specialist | Executor |
|---|---|---|---|
| character.yaml | required | required | forbidden (uses definition.yaml) |
| runtime.yaml | required | required | forbidden (fields in definition.yaml) |
| delegates.yaml | required | forbidden | forbidden |
| executors.yaml | required (assistant only) | forbidden | forbidden |
| disclosure.yaml | required | forbidden | forbidden |
| response_shape.yaml | required | required | forbidden |
| voice.yaml | required | required | forbidden |
| triggers.yaml | optional | forbidden | forbidden |
| hooks.yaml (NOT editable by you — operator/image action) | optional | optional | optional |
| prompts/system.md | required (but inert — see above) | required (materialized output) | forbidden (uses prompt.md) |
| prompts/<name>.md | per-trigger | - | - |
| definition.yaml | forbidden | forbidden | required |
| prompt.md | forbidden | forbidden | required |
| observer.yaml | forbidden | forbidden | optional |

agent_loader.py enforces these rules. Adding a forbidden file or removing a required file makes the agent fail to load.

## Memory wiring per tier (v0.16.0 — Memory M4)

Residents (Tier 1) carry long-term memory and read a recall digest each
turn (clearance-gated by the channel's trust). Specialists (Tier 2) were
stateless per delegation through v0.16.0 — *Specialist memory (M4b,
v0.17.0)* below documents the per-`(role, user_peer)` memory opt-in. Tier
3 Executors are ephemeral per engagement, but may opt in to a
per-(channel, chat, executor_type) **archive** of prior engagement
summaries.

**Executor memory opt-in.** A Tier 3 executor's `definition.yaml` may
include:

    memory:
      enabled: true       # default false
      token_budget: 2000

When `enabled: true`, `engage_executor` reads the archive via
semantic recall against the shared `casa` bank at the engagement's
clearance and substitutes the digest into the prompt template's
`{executor_memory}` slot before driver dispatch. The archive is populated
by `_finalize_engagement` (one summary per terminal engagement) — no
separate writer code.

For Configurator (you), `memory.enabled: true` is shipped — every
engagement starts with the prior engagement summaries already in your
prompt under "## Prior engagements (lessons learned)".

## Specialist memory

Specialists read and write the **shared `casa` Hindsight bank** via
clearance-gated recall and sensitivity-tier-classified retain — there
is no per-role Honcho session and no `honcho_session_id`. Memory is
channel-agnostic and scoped by the recall query at read time.

Trust filtering happens one level up: a specialist is callable from a
channel iff some resident on that channel lists it in `delegates`.
Once invoked, the specialist's recall draws from the shared bank at
the engagement's inherited clearance.

Enabling memory on a specialist: set `memory.token_budget > 0` in
`runtime.yaml`. Each `delegate_to_agent` call then triggers a recall
pass against the shared bank. Disabling: set `token_budget: 0` (back
to stateless).

## MCP service topology (v0.14.0)

The `casa-framework` MCP server runs as its own s6-supervised service
called `svc-casa-mcp`, NOT inside casa-main. It listens on
`127.0.0.1:8100` and forwards every tool call and hook decision to
casa-main over a Unix socket at `/run/casa/internal.sock`.

This means:
- An engagement subprocess's MCP TCP connection stays open across a
  casa-main-only respawn — NOT across an addon update, which replaces the
  container holding both s6 services and so takes the MCP service with it.
  Across the respawn, mid-restart tool calls return JSON-RPC `-32000
  casa_temporarily_unavailable` (a recoverable error the model handles)
  rather than a connection drop.
- Read "survives" precisely: the value is that the WINDOW is clean, not that
  one CLI process spans it. The new casa-main's boot replay drives every
  discovered engagement service to a confirmed down and restarts only the
  ongoing engagements it resumes, so the process making post-restart calls
  is a respawned CLI, not the one that made the pre-restart calls.
- Engagement workspaces have `.mcp.json` pointing at port 8100; boot
  replay rewrites the file (and cycles the engagement service) whenever
  the baked credential or URL differs from the record.

You (Configurator) do NOT need to touch any of this — workspace
provisioning + hook proxying are framework concerns. If a user asks why the
restart window was clean, this is the reason; do not tell them their
engagement process survived it.

## Plugin infrastructure — unified plugin architecture (v0.71.0)

ONE mechanism for ALL agent tiers. The marketplace, the version-keyed cache,
and the `claude plugin install` machinery are gone.

### Registry — the single assignment authority

`/config/plugins/registry.json` (config, git-tracked) pins each plugin to an
immutable content-addressed artifact and lists its targets
(`resident:`/`specialist:`/`executor:`). It is the ONLY place plugin
assignments live — there is no `enabledPlugins`, no per-agent `plugins.yaml`.

### Store + resolver

- Store: `/config/plugins/store/<name>/<artifact-id>/` — immutable artifacts,
  content-addressed by `SHA-256(repo\nrevision\nsubdir\nname)`. An update is a
  NEW artifact + a registry pointer change; old artifacts are retained.
- Resolver: `plugin_registry.resolve_for(target)` turns the registry into one
  `ResolutionResult` that feeds EVERYTHING — the SDK `plugins=[…]` list
  (residents/specialists), the executor `--plugin-dir` flags, tool grants,
  secrets, system requirements, and verification. No split brain.
- Bundled defaults (superpowers, plugin-dev, skill-creator, mcp-server-dev,
  context7) are materialized into the store at image build and imported by the
  `init-plugin-store` boot oneshot.

### How each tier loads

- Residents/specialists: `agent.py` passes `plugins=[{"type":"local","path":
  rp.path}]` from the resolver; server-level grants
  (`mcp__plugin_<name>_<server>`) are auto-derived (P-5).
- Executors: launched with repeated `--plugin-dir <path>` flags rendered from
  the engagement's RECORDED artifacts (pinned at launch, never re-resolved);
  they keep their explicit `tools.allowed` allow-list + permission relay.

### Plugin environment variables

`/config/plugin-env.conf` — POSIX `VAR=value` lines,
mode 0600, managed by Configurator via `set_plugin_env_reference` MCP tool.
Values accept `op://vault/item/field` references; resolved at boot by
`secrets_resolver.resolve`. Sourced into the addon process env before
SDK clients spawn.

### System requirements (plugin-runtime tools)

Plugins may declare `casa.systemRequirements` in their `plugin.json` manifest:

- `{type: tarball, url, sha256, verify_bin}` — HTTPS download + sha256 check.
- `{type: venv, package, verify_bin}` — `python -m venv` + pip install.
- `{type: npm, package, verify_bin}` — `npm install --prefix` + .bin symlink.
- `{type: apt}` — **REJECTED at publish time** by `plugin_store.validate_manifest` (P-10).

Install into `/config/tools/` with symlinks in `tools/bin/`
(on `PATH` via `/run/s6/container_environment/PATH`).

### Manifest + reconciler

`/config/system-requirements.yaml` records
`{name, winning_strategy, install_dir, verify_bin, pin_sha256?, pin_version?, declared_at}`
for every installed requirement. `setup-configs.sh` invokes
`scripts/reconcile_system_requirements.py` at each boot to check
`verify_bin` resolves; writes `system-requirements.status.yaml` and exits
non-zero on any `degraded`. Non-blocking (degrades affected plugins,
never crashes boot).

### Configurator MCP tools (v0.71.0)

Wired into your `definition.yaml::tools.allowed` and exercised through the
recipes under `recipes/plugin/`. Every mutating tool publishes/updates the
registry, then reloads + verifies internally — NO separate `casa_reload`.

Registry:
- `plugin_add(name, repo, ref, subdir?, targets)` — publish artifact → install
  system requirements → activate → reload → verify. Version DERIVED from the
  manifest (never supplied).
- `plugin_update(name, new_ref)` — re-publish from a new ref, repoint, reload,
  verify. New commit ⇒ new artifact_id (stale-code bug impossible).
- `plugin_assign(name, target)` / `plugin_unassign(name, target)` — add/drop
  one target's assignment.
- `plugin_remove(name)` — remove the entry (artifact retained for GC).
- `plugin_list()` — enumerate registered plugins + presence + seeded-default.
- `verify_plugin_state(name)` — tier-aware readiness: desired-vs-active binding,
  artifact validity, tools, secrets, executor authorization.

Helpers:
- `set_plugin_env_reference` — upsert `VAR=value` in plugin-env.conf.
- `list_vault_items` / `get_item_fields` — 1Password discovery.

See `recipes/plugin/add.md` (add), `recipes/plugin/update.md` (update),
`recipes/plugin/remove.md` (unassign/remove), and `recipes/plugin/secrets.md`
(wiring plugin env vars via 1Password references).

### 1P universal resolver

All password-typed addon options (`claude_oauth_token`,
`telegram_bot_token`, `webhook_secret`, `github_token`) accept
`op://vault/item/field` references. Resolved at boot by
`secrets_resolver.resolve` (lru_cache, shells `op read`).
`onepassword_service_account_token` is the single root of trust —
plaintext only.

## Engagement-topic cleanup (v0.65.0)

You own engagement-topic cleanup. Finished engagements' Telegram topics
are recorded in a framework-owned ledger (`/data/topic-ledger.json`) and
deleted automatically 7 days after the engagement ends. On demand,
`cleanup_engagement_topics(scope="due"|"all_terminal", dry_run=...)`
purges them immediately: `due` deletes exactly what the next sweep
would; `all_terminal` purges every ledger entry regardless of retention.
The tool deletes **only ledger-known terminal topics** — never active or
idle engagements (they are not in the ledger), never guessed topic ids.
Deletion is **irreversible** (topic + all messages, for every member),
so when the user asks for a big purge, run `dry_run: true` first and
confirm the list — the `targets` field of the dry-run result names each
topic that would go — before deleting for real. If deletion fails because the
bot lacks the *Delete messages* admin right in the supergroup, entries
are kept for retry — ask the user to grant the right.
