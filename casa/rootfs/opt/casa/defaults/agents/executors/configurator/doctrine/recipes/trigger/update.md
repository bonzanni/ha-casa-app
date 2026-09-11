# Recipe: update an existing trigger

User wants to change schedule, prompt, or channel for an existing trigger.

## Ask the user

1. **Which trigger, on which agent?**
2. **What specifically?**

## Update the trigger — `config_trigger_upsert`, never a hand edit

**You cannot Edit or Write `agents/<role>/triggers.yaml`. The hook denies it**
— the resident's reminder tools write the same file from inside Casa, and a
hand edit silently discards whatever landed since you read it.

`config_trigger_upsert` REPLACES the entry of the same name, in place, so pass
the trigger's full shape — every field it should end up with, not just the ones
that changed. Read the file first to see what it currently has; reads are fine.

    config_trigger_upsert(role="<role>", name="<trigger_name>",
                          type="cron", schedule="0 7 * * 1-5",
                          channel="telegram",
                          prompt="<imperative, ended as its shape requires — see below>")

Per-trigger prompt in prompts/<trigger_name>.md — that one IS an ordinary edit.

## Every scheduled prompt says how the turn ends

The same convention `recipes/trigger/add.md` states, and it applies whenever you
rewrite a prompt here: a scheduled turn that sends a message and then narrates
delivers the narration as a SECOND message to the same chat, because Casa never
suppresses a turn's closing text.

For interval/cron/date prompts whose turn delivers its own message, keep
the send instruction first and unconditional, and end the prompt with:
After the send, output the sentinel `<silent/>` and nothing else.

A prompt that does NOT send with a tool must not be given the clause: its own
final text is the delivery, so a closing sentinel suppresses the whole turn.
Both endings, written out:

    # shape A — the turn SENDS with a tool, then has nothing left to say.
    prompt="Send this exact message via telegram: \"Bins out tonight.\" After the send, output the sentinel `<silent/>` and nothing else."

    # shape B — the turn's OWN final text is the delivery. No closing clause;
    # the sentinel appears only as the way to say nothing at all.
    prompt="Output today's forecast as the final message text, with no preamble. If there is nothing worth sending, output the sentinel `<silent/>` and nothing else."

Because an upsert replaces the whole entry, a prompt you rewrite is a prompt you
re-end: give it the ending its shape requires, even when the prompt is not what
you were asked to change. An existing shape-A prompt that lacks the clause is
worth mentioning to the operator; adding it is a prompt change like any other,
so say what you changed.

## Reload — MANDATORY before emit_completion

**Soft** - casa_reload_triggers(role). Canonical order:

    config_git_commit(message="update <trigger-name> on <role>: <what>")
    casa_reload_triggers(role="<role>")
    emit_completion(status="ok", text="...committed SHA <sha>, reloaded triggers for <role>.")

Skipping the reload leaves the change committed but **inert** — the
old trigger keeps firing on its old schedule. See completion.md.
