# Recipe: edit a prompt file

**Which prompt file it is decides everything, including whether the edit is
possible at all.** Three kinds, three different answers:

| File | Editable here | When it takes effect |
|---|---|---|
| an executor's `prompt.md` / `doctrine/*.md` | yes | on that executor's next COLD session, not necessarily the next turn |
| a resident's `prompts/<trigger>.md` | yes | after `casa_reload_triggers(role=<role>)` — NOT on the next turn |
| a resident's `prompts/system.md` | **no — the hook denies it** | never; see `recipes/prompt/resident.md` |

## Executor prose

`prompt.md` and the executor's own `doctrine/*.md` are composed into what that
executor is served, so an edit is real. It is picked up when the executor's
options are next built, which happens on a COLD pool connect — a warm client is
reused without rebuilding its options. So "next turn" is not a promise you can
make; "next time that executor is engaged fresh" is.

## A resident's per-trigger prompt

`prompts/<trigger>.md` IS served — but its prose is resolved when the agent
loads and then captured inside the scheduled job, so editing the file changes
nothing until the triggers are reloaded. Always follow it with
`casa_reload_triggers(role=<role>)`. (Webhook triggers carry no stored prompt at
all; the schema refuses one. See `recipes/trigger/add.md`.)

## A resident's `prompts/system.md`

There is no edit to make. A persona-bound resident is served its COMPILED
BUNDLE, and this file is not one of its inputs — the write is denied, and it
would not have worked. Read `recipes/prompt/resident.md` before answering the
user, because what to offer instead depends on what they actually asked for.

## Still commit

Audit trail and rollback:

    config_git_commit(message="update <role>/<file>.md prompt")
    casa_reload_triggers(role="<role>")     # trigger prompt files ONLY
    emit_completion(status="ok", text="Updated <role>'s <file>.md. Commit <sha>.")

## Only consideration

If the file is referenced by a `prompt_file:`/`card_file:` pointer in a YAML,
verify it still exists at boot time (editing in place is fine).
