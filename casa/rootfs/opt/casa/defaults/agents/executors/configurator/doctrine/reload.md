# Reload granularity

Casa supports in-process reload at eight scopes. None of them restart the
addon. For changes that genuinely need a process restart, use
`casa_restart_supervised` (rare).

## Eight reload scopes

| `scope` | Tool | Downtime | Required `role` | When to use |
|---|---|---|---|---|
| `agent` | `casa_reload(scope='agent', role=...)` | <1s | yes | character/runtime/delegates/disclosure/voice/hooks edits for ONE role; plugin install/uninstall on ONE role |
| `triggers` | `casa_reload_triggers(role=...)` | <1s | yes | triggers.yaml edits for an EXISTING agent (legacy alias for `casa_reload(scope='triggers', role=...)`) |
| `policies` | `casa_reload(scope='policies')` | <1s | no | `policies/disclosure.yaml` edits |
| `plugin_env` | `casa_reload(scope='plugin_env')` | <1s | no | `set_plugin_env_reference` calls / `plugin-env.conf` edits |
| `agents` | `casa_reload(scope='agents')` | <1s | no | a specialist install/uninstall (pipeline) materialized or removed a specialist directory under `agents/specialists/`; also a persona-apply on a specialist |
| `executors` | `casa_reload(scope='executors')` | <1s | no | executor `definition.yaml` edits; create/delete an executor; flip `enabled` / `permission_mode` / `tools.allowed` on an executor |
| `config_sync` | `casa_reload(scope='config_sync')` | <1s | no | re-run the default-sync reconciler live (adds/updates from image defaults), then cascades `agents` + `policies` reloads |
| `full` | `casa_reload(scope='full')` | <1s | no | catch-all when unsure or multiple categories edited |

`casa_restart_supervised` (~10-15s) is reserved for s6 service-tree
changes, addon options.json mutations, or kernel concerns.

## What requires what

| Change | Reload |
|---|---|
| Edit a resident's prompts/system.md | **you cannot** — Write/Edit of it are refused. It is not served to a bundle-bound resident; see `recipes/prompt/resident.md` |
| Edit a resident's prompts/<trigger>.md | `triggers` — the prose is captured in the scheduled job at load, so an edit is inert until the reload |
| Edit response_shape.yaml | **you cannot** — the hook denies it. Nothing reads it for a bundle-bound agent; see `recipes/response-shape/edit.md` |
| Edit executor's doctrine/*.md | none — but effective on that executor's next COLD session, not guaranteed on the next turn |
| Edit existing agent's triggers.yaml (no other change) | `triggers` |
| Edit character.yaml | `agent` for that role |
| Edit runtime.yaml | `agent` for that role |
| Edit delegates.yaml | `agent` for that role |
| Edit disclosure.yaml | `agent` for that role — but it changes no served prompt today; see `recipes/disclosure/edit.md` |
| Edit voice.yaml | `agent` for that role |
| Flip a specialist's `enabled` flag | `agent` for that role — the reload that reads `enabled: false` tears the specialist down (no agent, no bus queue, no jobs or routes, not delegatable) instead of swapping it; the file-reading scopes (`triggers`, `policies`, `agents`, and so `config_sync`/`full`) honour the flag the same way, and each reports `teardown_disabled_specialist` |
| hooks.yaml (any agent's) | NOT editable by you — hook-policy files are denied unconditionally; policy changes are an operator/image action |
| Edit policies/disclosure.yaml | `policies` — same caveat as the row above |
| Edit an executor's `definition.yaml` | `executors` |
| Create or delete an executor | `executors` |
| Edit an executor's `prompt.md` / `observer.yaml` / `doctrine/*` | none — effective on that executor's next COLD session; a warm client is reused without rebuilding its options, so do not promise "next turn". `hooks.yaml` is NOT editable by you (see above) |
| Install a specialist (pipeline) — new `agents/specialists/<slug>/` | `agents` — REQUIRED explicitly after `config_git_commit`; `specialist_install_commit` does NOT reload (see `recipes/specialist/install.md`) |
| Uninstall a specialist (pipeline) — removed `agents/specialists/<slug>/` | `agents` — REQUIRED explicitly after `config_git_commit`; `specialist_uninstall` does NOT reload (see `recipes/specialist/uninstall.md`) |
| `plugin_add`/`plugin_update`/`plugin_assign`/`plugin_unassign`/`plugin_remove` | none — the tool self-sequences its own reload + verify (§3.9) |
| `set_plugin_env_reference` | `plugin_env` |
| Multiple categories edited in one engagement | `full` |
| Unsure | `full` |

## Order of operations — MANDATORY

1. Make your file edits.
2. Call `config_git_commit(message=...)`.
3. Call the appropriate `casa_reload(scope=...)` tool **before** `emit_completion`.
4. Call `emit_completion(...)` with the summary.

**Never call `emit_completion` BEFORE the reload step.** The model
treats `emit_completion` as the terminal action; once it fires, the
engagement closes and you do not get another chance to call the reload.
A skipped reload leaves the artifact **committed but inert**. See
`completion.md`.

`casa_reload(...)` returns immediately with `{status: "ok", ms: <int>,
actions: [...]}`. There is no Supervisor restart. That removes one race,
not every race: an agent-touching scope replaces every resident and
schedules its client pool to close, and your turn is hosted inside the
call that asked for it. Report promptly after the reload.

## When in doubt

- Touched only triggers, or a `prompts/<trigger>.md`, for one agent → `triggers`.
- Touched a single role's other YAMLs → `agent` for that role.
- Touched policies/*.yaml → `policies`.
- Installed or uninstalled a specialist via the pipeline → call `casa_reload(scope="agents")` yourself after `config_git_commit` (the install/uninstall tools do NOT reload; only the `plugin_*` tools self-sequence their own reload).
- Edited an executor's `definition.yaml` (enable / permission_mode / allowed tools / model) → `executors`.
- Created or deleted an executor → `executors`.
- Touched `plugin-env.conf` (via `set_plugin_env_reference`) → `plugin_env`.
- Touched anything else, or multiple of the above → `full`.
- Need a process restart (rare) → `casa_restart_supervised`.

`full` is always safe — does policies + agents + per-role agent. Add
`include_env=True` to also re-source plugin-env.conf.
