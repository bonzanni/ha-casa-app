# Recipe: install a persona from a repository

A bare-persona repository is a MUCH smaller artifact than a specialist component — just a
`pack/`+`manifest.json` (no role, no dependency closure, no config schema). Installing a persona
does not apply it to anything by itself; see `recipes/persona/apply.md` for that separate step.

## Ask the user

1. **Repository locator** (`owner/repo` + a ref — branch/tag/sha). When the operator asks for
   the latest version, or names no version at all, pass `ref="latest"`:
   `persona_install_inspect` resolves it server-side to the newest published release tag
   (GitHub's latest release, else the highest `v<semver>` tag) and reports it as `resolved_ref` —
   write `<repo>@<resolved_ref>` in any commit message and in your answer. You have no web
   tool: do not try to browse releases or tags, and never choose a branch on your own — a branch
   or a sha is passed only when the operator names it. A repo with no published release refuses
   with `no_release_found` and nothing is staged; report that and ask the operator whether to
   install a branch or a sha they name.

## Steps

1. `persona_install_inspect(repo=..., ref=...)`. On any `ok: false`, report the `kind`/`detail`
   verbatim and stop — do NOT retry with fabricated inputs.
2. When the result says `consent: "keyboard_posted"`, the approval card is in the operator's DM
   (posted server-side by `prompt_persona_install_consent`, not by this recipe); it is the actual
   approval gate. End your run with a short answer: which persona it is (display name, version)
   and that the card is waiting for his tap. When it says `consent: "pre_authorized"` (he already
   approved this exact persona), go straight to step 4.
3. Approve starts a new run by itself, whose task carries the staged values (`persona_id`,
   `version`, `checksum`, `staged_dir`): continue from step 4 with them. Re-inspect only if you
   need to (the values are missing, or the commit refuses them): a re-inspect of the same version
   returns `consent: "pre_authorized"`. Deny or expiry is shown on the card; no run starts for
   them, and nothing is needed from you. If the commit refuses with `kind: "consent_missing"`, the
   tap has not landed: say the card is still waiting and end the run (do not loop-retry).
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
  `recipes/persona/apply.md` step targets a resident or specialist explicitly.
