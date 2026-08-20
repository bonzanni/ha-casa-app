# Completion order

## The canonical order — MANDATORY

Every completion path is the same three tool calls, in this order:

1. **Commit.** Call `config_git_commit(message="<imperative sentence>")`. Skip only if you made zero file edits.
2. **Reload (if needed).** Call `casa_reload()` (hard) or `casa_reload_triggers(role)` (soft). Skip ONLY for none-reload changes (prompts, doctrine). Consult `reload.md` to pick the right scope.
3. **Emit completion.** Call `emit_completion(...)` with a structured summary.

**Never invert step 1 and step 2/3. Never call `emit_completion` BEFORE the reload step when a reload is needed.**

`emit_completion` is the terminal action. After it lands, the engagement closes and Ellen reads the summary. If you fire `emit_completion` without first running the prescribed reload, the artifact is **COMMITTED BUT INERT** — the YAML on disk is correct, but the running Casa keeps the prior runtime state. The trigger does not fire; the new agent does not load; the new tool does not surface. From Ellen's point of view (and the operator's), you "succeeded" — but the change has no effect. This is the highest-leverage doctrine violation in this executor.

## Reload before completion — and do not linger

`casa_reload_triggers(role)` is in-process and returns immediately.

`casa_reload()` is also in-process. It returns `{status: "ok", ms: <int>,
actions: [...]}` — it never POSTs to Supervisor and never returns a
`deferred` field. That vocabulary belongs to `casa_restart_supervised`,
which is a different tool and is not part of this order.

There is therefore no Supervisor restart to be killed by. But do NOT read
that as "your turn is guaranteed to survive the reload": an agent-touching
scope (`agent`, `agents`, `executors`, `policies`, `config_sync`, `full`)
replaces every resident and schedules its client pool to close, and your
engagement's turn is hosted inside the very turn that called the reload.
Call `emit_completion` promptly after the reload rather than interposing
work you do not need. If your turn does die before you report, the platform
now says so in this topic instead of leaving the engagement silent — but
the outcome of your work is lost either way, so do not rely on it.

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

`text` is what Ellen READS. Make it:

- Factually complete.
- Not rhetorical. No "I have successfully completed your request" fluff.
- Terse. One paragraph, 3-6 sentences.

When you have already invoked the reload before this call (i.e. the
canonical order), say so factually in the text — e.g.
`"Committed SHA abc123, soft-reloaded triggers for assistant."` — so
Ellen's narration to the operator is accurate.

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

## Hard-reload note

There is no such thing here: no reload scope restarts the addon. See
"Reload before completion — and do not linger" above for what
`casa_reload()` actually returns and why you should still not linger
between it and `emit_completion`. A Read between the two is fine; a long
stretch of further work is not.

## Cancellation

If the user says /cancel or you decide to abort, emit completion with `status="cancelled"`. Do NOT call `config_git_commit` if you made zero edits; DO call it if you made edits before the abort. A cancelled engagement does NOT need a reload — the artifact is either uncommitted (no edits) or operator-pending (the operator decides whether to keep the partial commit).
