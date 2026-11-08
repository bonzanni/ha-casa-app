# Disable an executor

Use when the operator wants to take an executor out of service
without removing it from disk (e.g. preserving its plugins,
doctrine, and registered MCP servers for a future re-enable).

## Steps

1. Read `agents/executors/<type>/definition.yaml` to confirm
   `enabled: true`.
2. Edit: flip `enabled: true` → `enabled: false`.
3. `config_git_commit(message="disable executor: <type>")`. Its
   `executors` reload takes the executor out of Ellen's `<executors>`
   registry, and `engage_executor` returns `unknown_executor`.
4. `emit_completion(text="...", status="ok")`.

## Effects

- Any in-flight engagement run by this executor continues —
  ExecutorRegistry tracks types, not running engagements. The
  registry update only affects FUTURE `engage_executor` calls.
- Bundled doctrine, hooks, plugins remain on disk; re-enabling is
  a single flip + commit.
- Disabling does NOT stop the executor's s6 service tree on the
  next boot — the boot path checks `enabled: false` and skips the
  service.
