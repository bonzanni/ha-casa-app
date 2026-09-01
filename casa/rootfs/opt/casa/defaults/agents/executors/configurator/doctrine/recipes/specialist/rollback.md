# Recipe: roll back an installed specialist

Use after a bad upgrade — or to undo a specialist's first persona override, which rotated its
component-default binding into the same retained prior. No re-fetch, no consent prompt — the
retained prior tuple's bytes are already pinned (in CAS, or under the installed personas root)
from when it was active.

1. Confirm WHICH specialist and that the operator wants the immediately-prior tuple specifically —
   a version, or the pre-override binding (rollback is one generation back, not "pick a
   version"). Confirm the owned-plugin consequence in step 2 with them too, BEFORE the call.
2. `specialist_rollback(slug=...)`. This republishes the retained prior's owned plugin set too, in
   the SAME call: a plugin the CURRENT version owns but the prior generation did not is removed,
   and anything the prior generation owned is restored — atomically with the tuple itself, no
   separate plugin step. After a persona override the retained owned-plugin generation may be
   older or absent (the override apply rotates the tuple but not the owned-plugins sidecar), so
   undoing an override this way can remove every plugin the specialist owns; the
   `plugin_data_note` relay below names what was dropped, and `plugin_list()` afterwards shows
   what is left. If it returns `kind: "no_prior_tuple"`, nothing was ever retained (the
   specialist was never upgraded or overridden). If the result carries
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
3. `config_git_commit`, `casa_reload(scope="agents")`, `emit_completion` (canonical
   commit -> reload -> emit order — see `completion.md`).

## Common mistakes

- Calling `specialist_rollback` more than once expecting it to keep stepping further back — it
  exchanges the active tuple with the SINGLE retained `active.prior.yaml` (and the owned plugin
  set with it); it is not a version history. A second call after a successful rollback
  swaps them back again, re-applying what the first call undid; `kind: "no_prior_tuple"` is
  returned only when nothing was ever retained.
- Forgetting `casa_reload(scope="agents")` — same as every other install/upgrade path, the committed
  tuple is not live until reload runs.
- Trying to `plugin_add`/`plugin_remove` the bundled plugin set yourself to "help" a rollback along
  — the owned-set swap is atomic and part of `specialist_rollback` itself; a manual edit first is
  just refused (`kind: "owned_by_specialist"`), and one attempted after is reverted by the rollback.
