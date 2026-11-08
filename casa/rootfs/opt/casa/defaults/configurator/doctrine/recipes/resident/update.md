# Recipe: update an existing resident

Residents own channels and are long-lived. Updating prompt/model/voice fields is safe; touching channels/session.strategy has wider implications.

## Low-risk updates

- Persona prompt (`prompts/system.md`) - **you cannot edit it**; Write/Edit of
  it are refused and it reaches nothing a resident is served. See
  `recipes/prompt/resident.md` for what to offer instead.
- Model - `runtime.yaml`.
- Response shape - **you cannot edit it** either, for the same reason; see
  `recipes/response-shape/edit.md`. Voice - `voice.yaml`.
- Adding a trigger - see recipes/trigger/add.md.
- Adding a delegate - see recipes/delegate/wire.md.

## Higher-risk updates (ask user explicitly)

- Changing channels - requires matching HA config for TTS/STT.
- Changing session.strategy - material behavior change.

For any of these, show the operator a dry-run summary first and end your run asking him to confirm.

## Always — MANDATORY order

1. Commit via `config_git_commit` — it reloads the resident (`reload.md` says
   what each change reloads).
2. End with your answer, saying in plain words what you changed for the
   resident.

See `completion.md`.
