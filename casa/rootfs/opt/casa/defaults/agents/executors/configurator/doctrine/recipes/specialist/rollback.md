# Recipe: roll back an installed specialist

Use after a bad upgrade. No re-fetch, no consent prompt — the prior version's bytes are already
pinned in CAS from when it was active.

1. Confirm WHICH specialist and that the operator wants the immediately-prior version specifically
   (rollback is one step back, not "pick a version").
2. `specialist_rollback(slug=...)`. This restores the prior version's owned plugin set too, in the
   SAME call: a plugin the CURRENT (bad) version owns but the prior version did not is removed, and
   anything the prior version owned is restored — atomically with the tuple itself, no separate
   plugin step. If it returns `kind: "no_prior_tuple"`, there is nothing to roll back to (either
   never upgraded, or already rolled back once). If the result carries
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
  restores the SINGLE retained `active.prior.yaml`, not a version history; a second call after a
  successful rollback has nothing further to restore and returns `kind: "no_prior_tuple"`.
- Forgetting `casa_reload(scope="agents")` — same as every other install/upgrade path, the committed
  tuple is not live until reload runs.
- Trying to `plugin_add`/`plugin_remove` the bundled plugin set yourself to "help" a rollback along
  — the owned-set swap is atomic and part of `specialist_rollback` itself; a manual edit first is
  just refused (`kind: "owned_by_specialist"`), and one attempted after is reverted by the rollback.
