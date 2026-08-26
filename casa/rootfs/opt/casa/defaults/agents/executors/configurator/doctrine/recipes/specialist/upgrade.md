# Recipe: upgrade an installed specialist

1. `specialist_install_inspect(repo=..., ref=<new ref>, mode="upgrade", target_slug=<slug>)` against
   the SAME repo, a newer ref. **Always pass `mode="upgrade"` + `target_slug`** — plain
   `specialist_install_inspect(repo=..., ref=...)` with no `mode` will refuse with `kind:
   "slug_collision"` because the slug is already installed (that refusal is correct for a FRESH
   install; upgrade mode is the only sanctioned way past it for the SAME slug). The result carries a
   fresh `receipt_id` for THIS closure — hold onto it verbatim for step 3. The new version's
   dependency closure (including any bundled/declared plugin) can differ from what is currently
   active — a plugin may be added, dropped, or repointed to a new digest by the new version; the
   consent DM in the next step covers the FULL new closure, not a diff against the old one.
2. Same consent flow as `recipes/specialist/install.md` steps 2-3 — an upgrade re-consents exactly
   like a fresh install (the identity binds `root_digest`, which changes with every version).
3. `specialist_upgrade(slug=..., component_id=..., version=..., root_digest=..., staged_dir=...,
   receipt_id=..., config={...}, secret_names_provided=[...])` using the EXACT `root_digest` and
   `receipt_id` `specialist_install_inspect` returned. Omitting `receipt_id` (or passing a stale
   one) refuses with `kind: "receipt_required"`.
4. If `state == "pending-configuration"`: the OLD version — and its OLD owned plugin set — is still
   live and answering delegations; tell the operator exactly which new config/secret names the new
   version needs. Nothing broke.
5. If `state == "error"`: report the validation failure; the OLD version, and its owned plugin set,
   are still live, unchanged.
6. If `state == "active"`: `config_git_commit`, `casa_reload(scope="agents")`, `emit_completion`
   (canonical commit -> reload -> emit order — see `completion.md`). The new version's owned plugin
   set REPLACES the old one atomically as part of `specialist_upgrade` itself (a plugin the old
   version owned but the new one no longer declares is removed; anything newly declared is
   activated) — no separate plugin step here. If the result carries
   `plugin_data_note` — it does when an owned plugin ended up removed and the
   rollback could not undo it — relay it verbatim with the names in
   `plugin_data_plugins`, exactly as `recipes/plugin/remove.md` step 4
   describes.

## Common mistakes

- Calling `specialist_install_inspect` without `mode="upgrade"` for an already-installed slug — it
  will correctly refuse with `kind: "slug_collision"`; this is not a bug to work around.
- Omitting `receipt_id` on `specialist_upgrade`, or reusing one from an earlier inspect call — it
  refuses with `kind: "receipt_required"`; always use the id the LATEST inspect returned.
- Forgetting `casa_reload(scope="agents")` after `state == "active"` — the new tuple is committed on
  disk but the live registry keeps running the old compiled bundle until reload runs.
- Treating `state == "pending-configuration"` or `state == "error"` as a failed upgrade that needs
  retrying blind — in BOTH cases the previously-active version, and its owned plugin set, is still
  running unchanged; report the specific gap (missing config, or the validation error) and let the
  operator decide next steps.
- Trying to `plugin_add`/`plugin_remove` the owned plugin set yourself to "help" an upgrade along —
  the owned-set swap is atomic and part of `specialist_upgrade` itself; those tools refuse an owned
  entry outright with `kind: "owned_by_specialist"` anyway.
