# Recipe: remove a trigger

## Ask the user

1. **Which trigger?** Confirm by name.
2. **Confirm:** stops firing immediately after reload.

## Remove the trigger — `config_trigger_delete`, never a hand edit

**You cannot Edit or Write `agents/<role>/triggers.yaml`. The hook denies it**
— see add.md for why.

    config_trigger_delete(role="<role>", name="<trigger_name>")

It leaves every other entry untouched (including the empty `triggers: []` case)
and refuses a reminder the resident owns (`managed_by: agent`) — those are the
resident's to cancel, not yours.

### The trigger's prompt file goes with it

If the trigger named a prompt file (`prompt_file: prompts/<trigger-name>.md`),
`config_trigger_delete` deletes that file too, in the same step, and the commit
records the deletion. Do not empty, rewrite or recreate it yourself. The
result's `prompt_file` says what happened:

- `outcome: removed` — gone; nothing is left over, and the removal is complete
  (`status="ok"`).
- `outcome: kept` — the file was left in place, and `reason` says why: another
  trigger or the resident's character still uses it, it is the resident's own
  `prompts/system.md`, it is a symlink or lies outside the role's `prompts/`
  directory, or it was already gone. A file something still uses is not a
  leftover; any other kept file is one you cannot delete, so name it to the
  operator in your completion.

## Reload — MANDATORY before emit_completion

**Soft** - casa_reload_triggers(role). Canonical order:

    config_git_commit(message="remove <trigger-name> from <role>")
    casa_reload_triggers(role="<role>")
    emit_completion(status="ok", text="...removed; reloaded triggers for <role>.")

Skipping the reload leaves the deleted trigger still registered in the
live scheduler — it keeps firing until the next addon restart. See
completion.md.

That reload also retires the webhook secret Casa generated for a deleted
`static_header` / `timestamped_hmac` trigger. A trigger later created under
the same name gets a FRESH secret — read it and give it to the caller again;
the old value no longer authenticates.
