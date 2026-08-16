# Recipe: apply an installed persona to a resident or specialist

1. Confirm the target (`resident:assistant`/`resident:butler`/`resident:concierge`, or
   `specialist:<slug>` for an INSTALLED specialist — hand-authored specialists have no binding to
   apply to) and the persona id/version (must already be installed — see `recipes/persona/install.md`).
2. `persona_apply(target_role_id=..., persona_id=..., persona_version=...)`.
3. If `ok: false, kind: "incompatible"`: the persona failed the role's compatibility check or its
   compile admission ceiling — report the detail verbatim, do not retry with a different persona
   without asking. **Nothing was written**: the binding is unchanged and no restart is pending, so
   there is nothing to undo and no reason to call `resident_persona_reset` (which would only stage
   a reset over a binding that was never touched).
4. If `ok: false, kind: "not_installed"`: the specialist slug given is not an installed component —
   report this and stop; a hand-authored specialist has no binding to override.
5. If `ok: true` and `restart_required: true` (residents): the new binding is **staged** — written
   to `desired.yaml`, with the resident's current binding still active and still serving. Tell the
   operator it takes effect on the resident's next restart (`casa_restart_supervised`); a
   resident's binding change is NEVER hot-swapped (Plan 1 Task 8's
   `ReloadError("restart_required", ...)` guard). Boot reconciliation promotes the staged binding
   and re-proves it compiles, keeping the last-known-good if it cannot.
6. `config_git_commit` first, then — if `ok: true` and `restart_required: false` (specialists) —
   `casa_reload(scope="agents")` activates it immediately, then `emit_completion`
   (canonical commit -> reload -> emit order, see `completion.md`).

## Common mistakes

- Treating `ok: true` for a specialist target as immediately live without the follow-up
  `casa_reload(scope="agents")` — the binding is committed to disk but the live registry keeps
  running the old compiled bundle until reload runs.
- Forgetting that a resident swap is restart-to-swap, never hot-swapped — do not tell the operator
  the resident's voice changed until AFTER `casa_restart_supervised` actually runs.
