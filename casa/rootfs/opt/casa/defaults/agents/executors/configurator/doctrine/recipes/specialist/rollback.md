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
   `plugin_data_note` — it does when an owned plugin ended up removed and the
   rollback could not undo it — relay it verbatim with the names in
   `plugin_data_plugins`, exactly as `recipes/plugin/remove.md` step 4
   describes.
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
