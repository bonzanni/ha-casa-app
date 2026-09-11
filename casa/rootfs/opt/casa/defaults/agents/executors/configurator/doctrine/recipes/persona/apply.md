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
   Tell the operator what the restart COSTS, in the same breath and BEFORE they agree to
   it. **Relay the result's `conversation_notice` VERBATIM.** Do not paraphrase it, do not
   summarise it, and above all do not work out for yourself whether this particular
   staging will change anything — you cannot, and neither can this tool. It says: on the
   restart that promotes this binding, if the resident's persona identity ends up
   different from the one it is running, **every conversation of this resident starts
   fresh on every channel**, because the persona is part of the resident's session
   identity; **voice history is not carried** at all, and Casa promises nothing about
   recovering what any of those conversations held. Do NOT offer long-term memory as a
   consolation: it is off by default, and even switched on it can discard a transcript
   behind a memory wipe or give up retaining it. Say what is lost and let the operator
   decide.
   Boot decides which happens, after this call has returned. A resident mid-way through
   something on voice is a reason to WAIT before restarting, and the operator can only
   weigh that if you pass the notice on.
6. `config_git_commit` first, then — if `ok: true` and `restart_required: false` (specialists) —
   `casa_reload(scope="agents")` activates it immediately, then `emit_completion`
   (canonical commit -> reload -> emit order, see `completion.md`).
   - SPECIALIST only — the way back. A specialist's FIRST override rotated its
     component-default binding into the retained prior tuple, and `specialist_rollback`
     restores it: follow `recipes/specialist/rollback.md` (it carries the owned-plugin-set
     relay the rollback owes; never call the tool inline from here). The retained prior is a
     single generation — a later override or an upgrade replaces it (an upgrade deliberately
     preserves an override) — so after either, the bundled persona is no longer one rollback
     away. There is no `persona_apply` back to a bundled persona (it is not resolvable by
     ref), and `resident_persona_reset` is residents-only.

## Common mistakes

- Treating `ok: true` for a specialist target as immediately live without the follow-up
  `casa_reload(scope="agents")` — the binding is committed to disk but the live registry keeps
  running the old compiled bundle until reload runs.
- Forgetting that a resident swap is restart-to-swap, never hot-swapped — do not tell the operator
  the resident's voice changed until AFTER `casa_restart_supervised` actually runs.
- Reporting a staged resident binding as though the restart were free. When it does
  change the resident's identity it is not: the promotion starts every one of that
  resident's conversations fresh, and voice history is not carried. Relay the result's
  `conversation_notice` BEFORE the operator agrees to the restart, never after it has run
  — afterwards it is not a warning, it is an apology. `resident_persona_swap` and
  `resident_persona_reset` have no recipe of their own and carry the same
  `conversation_notice`; the same duty applies there.
- The opposite mistake, and it is the one that has been made three times here:
  REASSURING the operator that nothing will change. You cannot know that. `prior_persona`
  and the ref the operator asked for can both read identical while boot still promotes a
  different binding — an installed pack can shadow an image-default ref, and the binding
  digest moves for non-persona inputs (a role checksum among them) as well. Never tell
  the operator a restart is free. Relay the notice as written and let boot be the one
  that decides.
