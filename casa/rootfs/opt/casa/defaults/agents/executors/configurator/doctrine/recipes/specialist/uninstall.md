# Recipe: uninstall an installed specialist

1. Find all delegate references (Grep tool: pattern `<slug>` across
   `/config/agents/*/delegates.yaml`) and unwire them first by applying ONLY the
   edit step of `recipes/delegate/unwire.md` (do NOT run its commit/reload/emit_completion
   — steps 4–6 below do that once) — an uninstall does NOT auto-unwire delegates.
2. `specialist_uninstall(slug=...)`. This cascades: any plugin registry entry the slug's
   install/upgrade OWNS (registry name `<slug>.<name>`) is removed automatically as part of this
   ONE call — never `plugin_remove` it yourself (it refuses an owned entry with `kind:
   "owned_by_specialist"` anyway). An OPERATOR-installed plugin that merely TARGETS the slug (its
   `targets` list names `specialist:<slug>`) is a DIFFERENT thing and is left alone — it is not
   removed, just left with a target nothing currently answers (surfaces as `pending_targets` the
   next time it is reloaded/verified).
3. If the result carries `plugin_data_note` (it does whenever step 2 cascaded
   owned plugins out) — on ANY outcome, including an `ok:false` result that
   carries no `state` at all — report it to the operator verbatim with the list in
   `plugin_data_plugins`: those plugins' CLI-managed persistent data
   (`CLAUDE_PLUGIN_DATA` — possibly stored OAuth authorizations) was NOT
   deleted and no revocation was performed AT THE PROVIDER, so a reinstall
   re-attaches. Say nothing about Casa's own grants and consents for those
   plugins: their teardown is internal and this result does not report it. Say
   "may have survived", never "survived": Casa cannot see whether those plugins
   stored anything.
   Do not restate it as a deletion or a revocation.
   If the uninstall RAISES instead of returning a result, the cascaded
   registry removals may already have committed with no envelope to carry that
   caveat. Say so: the removals may have taken effect, and if they did, the
   same survival applies — no plugin data was deleted and nothing was revoked
   at the provider. Run `plugin_list()` to see which entries are gone.
4. `config_git_commit(message="uninstall specialist <slug>")`.
5. `casa_reload(scope="agents")` — evicts the removed agent from the live
   registry (canonical commit → reload → emit order, see `completion.md`).
6. `emit_completion(...)`.

CAS blobs are NOT deleted by uninstall (retained for a possible future re-install at the same
digest, and GC sweep execution is out of this plan's scope — see Task N1d's CAS pin/reference
model).

## Common mistakes

- Uninstalling before unwiring delegates — a stale `delegates.yaml` entry left pointing at a
  removed specialist will fail to resolve on the next delegation attempt.
- Skipping `casa_reload(scope="agents")` — the on-disk instance is gone but the live registry still
  holds the (now orphaned) loaded agent until reload runs.
- Assuming an operator-installed plugin that targeted the removed slug is gone too — it is NOT
  cascaded out; only slug-OWNED entries are. Tell the operator it survived as a `pending_targets`
  entry, and let them decide whether to retarget it or reinstall the specialist.
