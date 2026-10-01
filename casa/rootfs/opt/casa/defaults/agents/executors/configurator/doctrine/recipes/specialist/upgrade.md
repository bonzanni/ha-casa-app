# Recipe: upgrade an installed specialist

1. `specialist_install_inspect(repo=..., ref=<new ref>, mode="upgrade", target_slug=<slug>)` against
   the SAME repo, a newer ref — `ref="latest"` is accepted here too and resolves to the newest
   published release tag (reported as `resolved_ref`; `no_release_found` when the repo has none —
   see `recipes/specialist/install.md`). **Always pass `mode="upgrade"` + `target_slug`** — plain
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
   If `ok: false, kind: "open_conversations_unconfirmed"`: NOTHING changed — the specialist has
   open conversations. Relay the result's `warning` to the operator VERBATIM (it lists each
   conversation and says what happens to it) and ask whether to go ahead. Only on a yes, repeat
   the same call with `acknowledged_conversations` set to the ids the result lists; on a no, stop
   and tell the operator nothing changed — a declined warning voids those ids, so never re-pass
   them without asking again. If a later result carries `opened_after_confirmation` (or
   `opened_while_this_change_ran`), tell the operator about those conversations too, with the
   result's `open_conversation_notice` verbatim.
   Keep the acknowledged ids: the pending-configuration re-commit (step 4) and a re-run of a kept
   upgrade (below) pass the same `acknowledged_conversations` again, so the operator is not asked
   twice in this conversation.
   If `ok: false, kind: "upgrade_kept_new_version"`: the new version is kept on disk but Casa has
   not loaded it. Tell the operator plainly, from the result's `outcome`:
   the upgrade is not active yet, and new and open conversations still use the previous version.
   Then do the finishing step the result names — normally re-run this same `specialist_upgrade`
   call (same arguments, same `acknowledged_conversations`); when it says
   "restart Casa, then re-run the upgrade", ask the operator to restart Casa first
   (`casa_restart_supervised`) and re-run the call after it.
   Key this on the `kind`, never on `kept_new_version: true` alone: a sequencer-failure result
   carries that flag too, and there the new version IS loaded.
   If the result carries
   `plugin_data_note` — on ANY outcome, including an `ok:false` result that
   carries no `state` at all — relay it verbatim with the names in
   `plugin_data_plugins`, exactly as `recipes/plugin/remove.md` step 4
   describes. Do not restate it as a confirmed removal unless the note itself
   says so: the note says which of the three it is — the successful owned-set
   swap dropped those entries, or a failed compensation left them measured
   still removed, or Casa could not read the registry back and does not know. If the call RAISES instead of returning a result,
   its owned-set swap may already have committed with no envelope to carry
   that note. Say so: an owned plugin may have been removed, and if it was,
   the same survival applies — no plugin data was deleted and nothing was
   revoked at the provider. Run `plugin_list()` to see which entries are
   gone.
4. If `state == "pending-configuration"`: the OLD version — and its OLD owned plugin set — is still
   live and answering delegations; tell the operator exactly which new config/secret names the new
   version needs. Nothing broke. The result names the five values the resume takes and, as `tool`,
   the handler that takes them — for an upgrade that is `specialist_upgrade` again, because the
   still-active old version makes `specialist_install_commit` refuse `concurrent_mutation`. Pass
   the five back verbatim with `slug` and the supplied values. A later engagement reads the same
   six from `casactl specialist status <slug>`, and acts on `pending_commit_check` exactly as
   `recipes/specialist/install.md` step 5 describes — including that `not_verified` is never a
   finding that the candidate is unrecoverable, and never grounds to propose an uninstall.
5. If `state == "error"`: report the validation failure; the OLD version, and its owned plugin set,
   are still live, unchanged.
6. If `state == "active"`: `config_git_commit`, `casa_reload(scope="agents")`, `emit_completion`
   (canonical commit -> reload -> emit order — see `completion.md`). The new version's owned plugin
   set REPLACES the old one atomically as part of `specialist_upgrade` itself (a plugin the old
   version owned but the new one no longer declares is removed; anything newly declared is
   activated) — no separate plugin step here.

## Common mistakes

- Calling `specialist_install_inspect` without `mode="upgrade"` for an already-installed slug — it
  will correctly refuse with `kind: "slug_collision"`; this is not a bug to work around.
- Omitting `receipt_id` on `specialist_upgrade`, or reusing one from an earlier inspect call — it
  refuses with `kind: "receipt_required"`; always use the id the LATEST inspect returned.
- Forgetting `casa_reload(scope="agents")` after `state == "active"` — the new tuple is committed on
  disk but the live registry keeps running the old compiled bundle until reload runs. Even after
  it, a conversation already open keeps its personality and plugin versions; only new ones get the
  new version.
- Treating `state == "pending-configuration"` or `state == "error"` as a failed upgrade that needs
  retrying blind — in BOTH cases the previously-active version, and its owned plugin set, is still
  running unchanged; report the specific gap (missing config, or the validation error) and let the
  operator decide next steps.
- Trying to `plugin_add`/`plugin_remove` the owned plugin set yourself to "help" an upgrade along —
  the owned-set swap is atomic and part of `specialist_upgrade` itself; those tools refuse an owned
  entry outright with `kind: "owned_by_specialist"` anyway.
