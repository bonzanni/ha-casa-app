# Reload granularity

`config_git_commit` reloads what its commit changed, so a change is live when the
commit returns. You do not call a reload after committing. This file says what a
commit reloads, what it cannot make live, and the few reloads you still call yourself.

None of the reloads restart the addon. For changes that genuinely need a process
restart, use `casa_restart_supervised` (rare).

## What a commit reloads

| Committed change | The commit reloads |
|---|---|
| A resident's `triggers.yaml`, or a `prompts/<trigger>.md`, and nothing else of that resident | `triggers` for that resident — a trigger prompt's prose is captured in the scheduled job at load, so this reload is what makes the edit count |
| Any other file of a resident: `character.yaml`, `runtime.yaml`, `delegates.yaml`, `disclosure.yaml`, `voice.yaml` | `agent` for that resident (`disclosure.yaml` changes no served prompt today; see `recipes/disclosure/edit.md`) |
| A new resident directory, or a removed one | `agents` |
| A specialist install, upgrade, rollback or uninstall (its link under `agents/specialists/` changed) | `agents` |
| An edit inside an installed specialist, such as its `enabled` flag | `agent` for that specialist |
| `policies/disclosure.yaml` | `policies` |
| An executor's `definition.yaml`, or creating or deleting an executor | `executors` |
| An executor's `prompt.md` or `doctrine/*` | none — effective on that executor's next COLD session; a warm client is reused without rebuilding its options, so do not promise "next turn" |
| `plugins/registry.json` alone | none — the `plugin_*` tools activate their change and commit the registry themselves; a `plugin_update` returning `unchanged:true` changed nothing |
| A resident's staged persona binding under `bindings/` | none — only a restart activates it (see `recipes/persona/apply.md`) |
| `schema/`, or any other path | `full` |
| Changes of more than one kind in one commit (say a specialist install and the resident `delegates.yaml` that wires it) | one `full` |

What you cannot change, whatever you commit:

- A resident's `prompts/system.md` — Write/Edit of it are refused; it is not served to a bundle-bound resident. See `recipes/prompt/resident.md`.
- `response_shape.yaml` — the hook denies it; see `recipes/response-shape/edit.md`.
- Any agent's `hooks.yaml` — hook-policy files are denied unconditionally; policy changes are an operator/image action.

Flipping a specialist's `enabled` flag to false tears the specialist down when the
commit's reload reads it (no agent, no bus queue, no jobs or routes, not delegatable),
and reports `teardown_disabled_specialist`. The teardown also closes the specialist's
open conversations (each topic is told; a close that failed is reported as
`teardown_incomplete_close_engagements`, the operator is told, and the next `agents`
reload retries it) — say so before flipping the flag.

## Changes no commit can see

Some writes land outside what the config repository tracks, so a commit sees nothing
of them. Each is made live by the tool that writes it, or by the one reload you call:

- `persona_apply` on a specialist — its override lives in the specialist's instance
  files; the tool reloads the specialist itself and returns that `reload`.
- `specialist_install_commit`, `specialist_upgrade`, `specialist_rollback`,
  `specialist_uninstall` — they reload the specialist themselves; the commit after them
  records the link they rewrote.
- `plugin_add` / `plugin_update` / `plugin_assign` / `plugin_unassign` / `plugin_remove` —
  they reload, verify and commit their own change.
- `set_plugin_env_reference` / `remove_plugin_env_reference` — `plugin-env.conf` is
  gitignored: call `casa_reload(scope="plugin_env")` yourself (`recipes/plugin/secrets.md`).
- `persona_install_commit`, `persona_remove`, `persona_prune` — the persona library is
  not tracked, and nothing loads a persona until it is applied: no reload.

## Reading the commit's reloads

The commit returns each reload it ran under `reloads`: `{status, scope, role, ms,
actions}`, plus `failures` when a part of it did not take. When any reload failed or
reported failures, the commit also carries a `warning`: the commit stands, but part of
the change is not live. Tell the operator in plain words, then fix the cause and commit
again, or retry the reload yourself (below). A `restart_required` kind means a
resident's identity changed and only a restart makes it live: that is the operator's
call (a restart costs conversations; see `recipes/persona/apply.md`), so ask before
`casa_restart_supervised`.

A reload that touches an agent replaces it and schedules its client pool to close, so
its next turn starts a cold client, and your own turn is hosted inside the call that
committed. Report promptly after the commit.

## Reloads you call yourself

| `scope` | Tool | Required `role` | When |
|---|---|---|---|
| `plugin_env` | `casa_reload(scope='plugin_env')` | no | after `set_plugin_env_reference` or a `plugin-env.conf` edit — that file is gitignored, so no commit reloads it |
| `config_sync` | `casa_reload(scope='config_sync')` | no | re-run the default-sync reconciler live (adds/updates from image defaults), then cascade `agents` + `policies` |
| any scope | `casa_reload(scope=..., role=...)` | for `agent`, `triggers` | a retry, when a commit's reload failed and you fixed the cause without a new commit, or when a tool's result asks for one |

The scopes are `agent` and `triggers` (both take a `role`; `casa_reload_triggers(role=...)`
is the same as `triggers`), `policies`, `plugin_env`, `agents`, `executors`,
`config_sync` and `full`. `full` is always safe: policies + agents + executors +
every agent; add `include_env=True` to also re-source `plugin-env.conf`.

`role` is the agent's directory name — `assistant`, `butler`, `concierge`, or a
specialist's slug. The tier-prefixed forms the plugin tools use for the same role
(`resident:assistant`, `specialist:<slug>`, as in `plugin_add`'s `targets` and its
`reloaded:` list) are accepted too: the prefix is stripped before the reload runs, and
the reply's `role` names the directory the reload ran for.

`casa_restart_supervised` (~10-15s) is reserved for s6 service-tree changes, addon
options.json mutations, a `restart_required` reload, or kernel concerns.
