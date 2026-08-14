# Recipe: remove an installed persona

Removing a persona deletes its bytes from `/config/personas` and revokes its install approval, so
a later install of the same persona prompts the operator again. It is refused while anything is
still bound to it — that refusal is the feature, not an obstacle: a resident whose binding names a
persona that is gone fails to load, and a resident load failure is fatal to the whole boot.

Image-shipped personas (the three resident defaults) are part of the image and are never
removable; `persona_remove` reports them as `not_installed`.

## Ask the user

1. **Which persona** — id and version (`persona_list` shows both, with what refers to each).

## Steps

1. `persona_list`. Report the entry for the persona: its display name, whether anything refers to
   it, and whether it is `removable`. If `ok: false` with `kind: "references_unavailable"`, a
   bundle transaction is unresolved — report the detail verbatim and stop; the next restart's
   journal reconciliation clears it.
2. `persona_remove(persona_id=..., version=...)`.
3. On `kind: "persona_pinned"`: report the `referenced_by` list verbatim and stop. The persona is
   still bound. To free it:
   - a resident (`resident:<slot>`) — `resident_persona_reset` (or `resident_persona_swap` to a
     different persona), then `casa_restart_supervised`. The old binding is only released once the
     restart has actually committed the new one;
   - a specialist (`specialist:<slug>`) — apply a different persona with `persona_apply`, or
     `specialist_uninstall` the component. A specialist's retained rollback tuple also counts as a
     reference until it is superseded;
   - a `journal:*` referrer — an unfinished install/upgrade transaction. Do not work around it;
     report it and stop.
4. On `kind: "ack_revoke_failed"`: nothing was removed and the bytes are intact. Report the detail
   verbatim.
5. On `ok: true`: tell the operator the persona is gone and that installing it again will ask for
   their approval afresh. No `config_git_commit` is needed — `/config/personas` is not tracked by
   the config repository — and no reload: nothing was bound to it.

## Common mistakes

- Treating `persona_pinned` as something to retry or force. There is no force; re-run only after
  the binding that names the persona is genuinely gone.
- Telling the operator a resident is free of a persona the moment `resident_persona_reset`
  returns. The reset is staged; it is the restart that releases the old persona.
- Reaching for `Write`/`Edit` on `/config/personas` when a removal is refused — the hooks deny it,
  correctly. The refusal is a statement about the system's state, not about the tool.
