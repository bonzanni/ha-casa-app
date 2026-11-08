# Completion order

## The canonical order — MANDATORY

Every change ends with the same two tool calls, in this order:

1. **Commit.** Call `config_git_commit(message="<imperative sentence>")`. Skip only if nothing you did changed a file — a tool that writes one counts (`config_trigger_upsert`, `specialist_install_commit`, …), not just your own edits. When unsure, call it: a commit with nothing to record returns an empty SHA, which is harmless. (The `plugin_*` tools commit `plugins/registry.json` themselves.) The commit also reloads what it changed, so the change is live when it returns; its result lists those reloads under `reloads`. When it carries a `warning`, a reload did not fully succeed: deal with it before completing, and say so in your summary.
2. **Emit completion.** Call `emit_completion(...)` with a structured summary.

You do not call a reload after a commit. See `reload.md` for the few reloads you still call yourself.

A change made in several commits goes live commit by commit, so commit when the
change is whole enough to run, not after every file.

## A task that only asks for information

A task that asks you to check, review or report, and to change nothing, still
ends with `emit_completion`, and it is finished once you have gathered the
answer: there is nothing to commit, so step 1 is skipped. Unless you have a
real question for the operator, call `emit_completion` in the same turn. Do not
end the turn with the report as text and wait: the engagement stays open until
you complete it, and Ellen reads only the completion's `text`, never the topic.
That `text` carries the findings themselves, in plain words, even when you have
already written them in the topic.

`emit_completion` is the terminal action. After it lands, the engagement closes and Ellen reads the summary.

## After the commit — do not linger

The commit's reload is in-process: no reload restarts the addon (only
`casa_restart_supervised` does, and it is not part of this order). But do NOT
read that as "your turn is guaranteed to survive the reload": a reload that
touches an agent replaces it and schedules its client pool to close, and your
engagement's turn is hosted inside the very turn that committed. Call
`emit_completion` promptly after the commit rather than interposing work you do
not need. A Read in between is fine; a long stretch of further work is not. If
your turn does die before you report, the platform says so in this topic
instead of leaving the engagement silent — but the outcome of your work is lost
either way, so do not rely on it.

## emit_completion payload

    emit_completion(
        status="ok" | "partial" | "failed" | "cancelled",
        text="Free-form narrative - one paragraph.",
        artifacts=[
            {
                "kind": "commit",
                "repo": "/config",
                "sha": "<from config_git_commit>",
                "files_changed": ["agents/specialists/fitness/character.yaml", ...],
            },
        ],
        next_steps=[],
    )

Write `text` for the operator. Make it:

- Factually complete.
- Plain words: what you found or changed, whether you made it take effect (your
  recipe says what does), and anything he still has to do (a setting to
  give, a caveat such as conversations that keep the old version).
- Free of machinery: no commit SHA, no tool names, no result fields such as
  `state=active`, no reload scopes — they are how you work, not news for
  him. The commit SHA and the files changed go in `artifacts` above.
- Not rhetorical. No "I have successfully completed your request" fluff.
- Terse. A change takes one paragraph, 2-5 sentences; findings take the
  room they need and no more.

E.g. `"I upgraded Fido to 0.1.28. Nothing for you to set up."`.

`next_steps` is empty for the configurator. In particular a plugin's `setup_*`
MCP tool is NEVER handed back — Casa owns a declared `casa.setupTool` and runs
it itself (`recipes/plugin/add.md` / `update.md`). Report in `text` that setup
is queued; routing it to an agent would give one job two runners.

## Status semantics

`status` reflects the engagement-task outcome, NOT a sub-action outcome.

When the engagement's task is to test a security probe (e.g. "verify
path_scope denies a write to /etc/foo"), a successful denial means the
task succeeded — emit `status="ok"`. Use `status="failed"` only when the
engagement itself failed to complete its objective (subprocess crashed,
required tool unavailable, contradictory state preventing progress).
Use `status="partial"` if some objectives were met but others were not.

The valid enum values are exactly `"ok" | "partial" | "failed" |
"cancelled"`. Do NOT pass other strings (e.g. `"error"`) — the
engagement registry will record an `emit_completion_error` outcome
even though your sub-action narrative was correct.

## Cancellation

If the user says /cancel or you decide to abort, emit completion with `status="cancelled"`. Do NOT call `config_git_commit` if you made zero edits; DO call it if you made edits before the abort. Every commit reloads what it changed, so whatever you committed — before the abort or in that last commit — is live and stays live after the cancel: say in `text` what is now in effect, so the operator can decide whether to keep it.
