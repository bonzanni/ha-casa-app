# Recipe: update an existing resident

Residents own channels and are long-lived. Updating prompt/model/voice fields is safe; touching channels/session.strategy has wider implications.

## Low-risk updates

- Persona prompt (`prompts/system.md`) - **you cannot edit it**; Write/Edit of
  it are refused and it reaches nothing a resident is served. See
  `recipes/prompt/resident.md` for what to offer instead.
- Model - `casa_reload(scope='agent', role=<role>)`.
- Response shape - **you cannot edit it** either, for the same reason; see
  `recipes/response-shape/edit.md`. Voice - `casa_reload(scope='agent', role=<role>)`.
- Adding a trigger - see recipes/trigger/add.md. `casa_reload_triggers(role=<role>)`.
- Adding a delegate - see recipes/delegate/wire.md. `casa_reload(scope='agent', role=<role>)`.

## Higher-risk updates (ask user explicitly)

- Changing channels - requires matching HA config for TTS/STT.
- Changing session.strategy - material behavior change.

For any of these, do a dry-run summary in the topic first.

## Always — MANDATORY order

1. Commit via `config_git_commit`.
2. Reload per `reload.md` — **before** emit_completion (canonical
   order). Skip only for the rows `reload.md` marks as needing none.
3. `emit_completion` with status=ok, text citing the SHA + the reload
   that ran (or "no reload — none-scope change" if applicable).

See `completion.md`.
