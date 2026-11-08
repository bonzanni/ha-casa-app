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
| - | Configurator | You. Ships with Casa (image-owned), not configurable from `/config`. A delegate of the assistant (Ellen) through her `delegates.yaml`; runs in the operator's DM, each run starting fresh. | /opt/casa/defaults/configurator/ (read-only) |

Any resident may delegate (`delegate_to_agent`) to any other agent listed in its `delegates.yaml`. Your prompt, doctrine and hooks ship with Casa: nothing in `/config` changes them.

## Key files per tier

| File | Resident | Specialist |
|---|---|---|
| character.yaml | required | required |
| runtime.yaml | required | required |
| delegates.yaml | required | forbidden |
| disclosure.yaml | required | forbidden |
| response_shape.yaml | required | required |
| voice.yaml | required | required |
| triggers.yaml | optional | forbidden |
| hooks.yaml (NOT editable by you — operator/image action) | optional | optional |
| prompts/system.md | required (but inert — see above) | required (materialized output) |
| prompts/<name>.md | per-trigger | - |

agent_loader.py enforces these rules. Adding a forbidden file or removing a required file makes the agent fail to load.

## Memory wiring per tier (v0.16.0 — Memory M4)

Residents (Tier 1) carry long-term memory: each session is served a view
of its ledger at its start (clearance-gated by the channel's trust). Specialists (Tier 2) were
stateless per delegation through v0.16.0 — *Specialist memory (M4b,
v0.17.0)* below documents the per-`(role, user_peer)` memory opt-in. You
carry no memory: each run starts fresh, with only the exchanges its task
brings.

## Specialist memory

Memory is Casa's own store, built in: each turn's words are captured as
they happen, and a writer files them into ledgers by sensitivity tier.
There is no per-role memory and no external memory server.

Trust filtering happens one level up: a specialist is callable from a
channel iff some resident on that channel lists it in `delegates`.
Once invoked, the specialist reads at the clearance its caller's context
carries.

Enabling memory on a specialist: set `memory.token_budget > 0` in
`runtime.yaml`. Each `delegate_to_agent` call then serves it the view of
its ledger, with the brief. Disabling: set `token_budget: 0` (back to
stateless; its output is then kept out of memory too).

## Plugin infrastructure — unified plugin architecture (v0.71.0)

ONE mechanism for residents and specialists. The marketplace, the version-keyed cache,
and the `claude plugin install` machinery are gone.

### Registry — the single assignment authority

`/config/plugins/registry.json` (config, git-tracked) pins each plugin to an
immutable content-addressed artifact and lists its targets
(`resident:`/`specialist:`/`executor:`). It is the ONLY place plugin
assignments live — there is no `enabledPlugins`, no per-agent `plugins.yaml`.

**Workers take no plugin.** No worker, you included, loads plugins today. An
`executor:` target belongs only to a `bundled` entry an older Casa seeded (Casa
seeds none today), and nothing serves it; an operator-installed plugin cannot be
given one (`plugin_add` and
`plugin_assign` refuse it, `plugin_update` refuses to repoint a bundled plugin
that serves a worker). A worker target already stored on an
operator-installed entry stays in the file but is IGNORED: `plugin_list` shows
it under `ignored_targets`, and the health report says so. `plugin_unassign`
clears it.

### Store + resolver

- Store: `/config/plugins/store/<name>/<artifact-id>/` — immutable artifacts,
  content-addressed by `SHA-256(repo\nrevision\nsubdir\nname)`. An update is a
  NEW artifact + a registry pointer change; old artifacts are retained.
- Resolver: `plugin_registry.resolve_for(target)` turns the registry into one
  `ResolutionResult` that feeds EVERYTHING — the SDK `plugins=[…]` list
  (residents/specialists), tool grants, secrets,
  system requirements, and verification. No split brain.
- Casa ships no bundled plugins. The `init-plugin-store` boot oneshot still
  imports a bundled catalog when an image carries one, and a `bundled`
  registry entry an older Casa seeded keeps working.

### How each tier loads

- Residents/specialists: `agent.py` passes `plugins=[{"type":"local","path":
  rp.path}]` from the resolver; server-level grants
  (`mcp__plugin_<name>_<server>`) are auto-derived (P-5).

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

Wired into your tool allow-list (it ships with Casa) and exercised through the
recipes under `recipes/plugin/`. Every mutating tool publishes/updates the
registry, then reloads + verifies internally — NO separate `casa_reload`.

Registry:
- `plugin_add(name, repo, ref, subdir?, targets)` — publish artifact → install
  system requirements → activate → reload → verify. Version DERIVED from the
  manifest (never supplied).
- `plugin_update(name, new_ref)` — re-publish from a new ref, repoint, reload,
  verify. New commit ⇒ new artifact_id (stale-code bug impossible). Already on
  that release ⇒ `unchanged:true`: no registry change and no reload, and verify
  reports its current state.
- `plugin_assign(name, target, profile?)` / `plugin_unassign(name, target)` —
  add/drop one target's assignment. `profile` names one of the plugin's declared
  access profiles for a NEW assignment only (omit = full); an existing
  assignment is never changed. See `recipes/plugin/add.md` ("Profiles and
  requirements").
- `plugin_remove(name)` — remove the entry (artifact retained for GC).
- `plugin_list()` — enumerate registered plugins + presence + seeded-default.
- `verify_plugin_state(name)` — tier-aware readiness: desired-vs-active binding,
  artifact validity, tools, secrets.

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
