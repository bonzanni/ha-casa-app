# Edit an executor's `definition.yaml`

Use for any structural change to an executor's definition file —
`permission_mode`, `tools.allowed`, `model`, `mcp_server_names`,
`idle_reminder_days`, `extra_dirs`, `mirror_chat_to_topic`,
`description`, etc.

## Steps

1. Read `agents/executors/<type>/definition.yaml`.
2. Make the edit. The schema (`defaults/schema/executor.v1.json`)
   accepts these `permission_mode` values: `acceptEdits`, `auto`,
   `bypassPermissions`, `default`, `dontAsk`, `plan` (CC CLI 2.1.119
   parity per v0.37.1 B-1).
3. `config_git_commit(message="edit executor <type>: <field>")` — it
   reloads the executors.
4. `emit_completion(text="...", status="ok")`.

## What you must NOT do

- Do not edit `type:` — the executor's identity is its directory
  name AND `type:` field; they must match.
- Do not flip `schema_version:` — it's pinned at `1`.
- Do not edit `driver:` from `in_casa` to `claude_code` (or vice
  versa) without also reviewing the executor's `prompt.md` and
  `hooks.yaml` (read-only for you — hook-policy changes are an
  operator/image action) — the two drivers have different MCP / tool
  surface conventions.

## Per-file isolation (v0.37.1 B-1b)

If your edit accidentally produces a schema-invalid YAML, only
this one executor fails to load; siblings remain available. The
commit's reload reports it under `failures` (and the log has it):
`Executors: loaded=[...] failed=['<type>: <reason>'] disabled=[...]`.

Recovery: read the registry's error message, fix the typo, repeat
step 3.
