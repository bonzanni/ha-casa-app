# Enable an executor

Use when the operator wants to switch a bundled-but-disabled
executor (e.g. `plugin-developer`) to enabled.

## Steps

1. Read `agents/executors/<type>/definition.yaml` to confirm the
   executor exists and currently has `enabled: false`.
2. Edit the file: flip `enabled: false` → `enabled: true`.
3. `config_git_commit(message="enable executor: <type>")`. It
   reloads `executors`: its `reloads` entry shows
   `rebuild_executor_registry` among the actions.
4. `emit_completion(text="...", status="ok")`.

## What the `executors` reload picks up

- `enabled: false ↔ true` flips.
- `permission_mode` changes.
- `tools.allowed` / `tools.disallowed` edits.
- `model` changes.
- `mcp_server_names` additions/removals.
- `prompt_template_file` path changes (file content is lazy-read
  per session, so a prompt-only edit reloads nothing).

## What it does NOT pick up

- New `prompt.md` / `observer.yaml` / `doctrine/*` content (lazy-read
  per turn). (`hooks.yaml` is NOT editable by you — hook-policy files
  are denied unconditionally; policy changes are an operator/image
  action.)
- New executor TYPES (those need a fresh add of the directory —
  also handled by `executors`).
