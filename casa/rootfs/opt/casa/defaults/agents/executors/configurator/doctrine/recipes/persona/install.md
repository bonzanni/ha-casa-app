# Recipe: install a persona from a repository

A bare-persona repository is a MUCH smaller artifact than a specialist component — just a
`pack/`+`manifest.json` (no role, no dependency closure, no config schema). Installing a persona
does not apply it to anything by itself; see `recipes/persona/apply.md` for that separate step.

## Ask the user

1. **Repository locator** (`owner/repo` + a ref — branch/tag/sha).

## Steps

1. `persona_install_inspect(repo=..., ref=...)`. On any `ok: false`, report the `kind`/`detail`
   verbatim and stop — do NOT retry with fabricated inputs.
2. Post the result (display name, persona id/version, checksum) as a plain in-topic message so the
   operator sees it BEFORE the DM consent prompt fires — the DM keyboard (posted server-side by
   `prompt_persona_install_consent`, not by this recipe) is the actual approval gate; this step is
   purely informational context in-topic.
3. Wait for the operator's DM tap (Approve/Deny) to resolve. There is no polling tool — the
   `persona_install_commit` call in the next step will itself refuse with `kind: "consent_missing"`
   if the tap has not landed yet; on that specific error, tell the operator you are waiting for
   their DM response and then stop (do not loop-retry). After Approve the install normally
   continues automatically — a synthetic resume turn carries this recipe forward — so you do NOT
   ask for a second message by default; only if that automatic resume fails to deliver would the
   operator need to send any message in the topic to continue.
4. Once approved: `persona_install_commit(persona_id=..., version=..., checksum=..., staged_dir=...)`
   using the EXACT values `persona_install_inspect` returned.
5. Tell the operator the persona is installed but NOT applied to anything yet — installing and
   applying are separate steps (`recipes/persona/apply.md`).

If the commit refuses with `kind: "version_content_conflict"` because the local copy is corrupt or
was published concurrently, remove that copy with `persona_remove` (`recipes/persona/remove.md`)
and retry — never by editing `/config/personas`, which the hooks deny.

## Common mistakes

- Calling `persona_install_commit` before the operator has actually tapped Approve — it will
  correctly refuse (`kind: "consent_missing"`); this is not a bug to work around, it IS the consent
  gate.
- Assuming install also applies the persona somewhere — it never does; a separate
  `recipes/persona/apply.md` engagement targets a resident or specialist explicitly.
