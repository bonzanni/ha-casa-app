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

Optionally delete agents/<role>/prompts/<trigger-name>.md if unused — that one
IS an ordinary edit.

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
