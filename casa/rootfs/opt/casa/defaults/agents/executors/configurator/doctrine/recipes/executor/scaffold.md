# Recipe: create a new executor type

**Tightly bounded.** Creating a new Tier 3 executor type is a
framework-level change, not a tenant configuration. Configurator may
NOT scaffold a new executor type without explicit authorization;
ExecutorRegistry loads every subdirectory and a half-filled one will
break load.

## If the user asks for a new executor type

1. Explain the scope — executor types ship from the framework
   (`/opt/casa/defaults/agents/executors/<type>/`). The default set
   today: configurator (you).
2. Check if the user's actual need is a **specialist** (Tier 2,
   role-keyed helper). Usually it is — see
   `recipes/specialist/install.md`.
3. If they genuinely want a new executor type, emit
   `emit_completion(status="partial")` describing the deferral and
   pointing at the framework-level work needed.

## What an executor `definition.yaml` looks like (reference only)

If a future framework PR ever does scaffold a new executor type, the
shape is (schema v1):

```yaml
schema_version: 1
type: <executor_type>           # lowercase, [a-z][a-z0-9_-]*
description: |
  At-least-twenty-character description of what this executor does.
model: sonnet                   # alias resolved at boot
driver: in_casa                 # the only driver
enabled: true
tools:
  allowed: [Read, Write, Bash, ...]
  permission_mode: acceptEdits
mcp_server_names: [casa-framework]
idle_reminder_days: 7
prompt_template_file: prompt.md

# M4 (v0.16.0+): opt-in cross-engagement archive.
# Set enabled: true for executors where the prior-engagement context
# is materially useful (long-lived, configuration-heavy work).
# Leave enabled: false for one-shot smoke or single-turn drivers.
memory:
  enabled: true
  token_budget: 2000
```

The `memory:` block is optional. When omitted, the executor runs
without prior-engagement context (the default for test fixtures and
any future smoke executor).
