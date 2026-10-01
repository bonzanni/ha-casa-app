# Recipe: uninstall an installed specialist

1. Find all delegate references (Grep tool: pattern `<slug>` across
   `/config/agents/*/delegates.yaml`) and note them — READ ONLY for now: an uninstall does NOT
   auto-unwire delegates, but nothing may be edited before the uninstall has committed (a
   declined warning or a cancelled erase question in step 2 must leave everything as it was).
2. `specialist_uninstall(slug=...)`.
   If `ok: false, kind: "open_conversations_unconfirmed"`: nothing was removed (a pending
   erase-data question, if any, was withdrawn). Relay the result's `warning` to the operator
   VERBATIM and ask whether to go ahead. Only on a yes, repeat the call with the SAME `erase_data`
   choice plus `acknowledged_conversations` set to the ids the result lists, and keep passing
   those ids on every later call of this uninstall (an Erase continuation and the finishing call
   too). On a no, stop — nothing was removed, and a declined warning voids those ids. This cascades: any plugin registry entry the slug's
   install/upgrade OWNS (registry name `<slug>.<name>`) is removed automatically as part of this
   ONE call — never `plugin_remove` it yourself (it refuses an owned entry with `kind:
   "owned_by_specialist"` anyway). An OPERATOR-installed plugin that merely TARGETS the slug (its
   `targets` list names `specialist:<slug>`) is a DIFFERENT thing and is left alone — it is not
   removed, just left with a target nothing currently answers (surfaces as `pending_targets` the
   next time it is reloaded/verified).
   If a bundled plugin declares an eraser, this call first answers
   `kind: "erase_choice_pending"` and removes nothing: follow "A plugin that can
   erase its own data" in `recipes/plugin/remove.md` with
   `specialist_uninstall(slug=..., erase_data=...)` in place of `plugin_remove`.
   The specialist is uninstalled only when every bundled eraser reported its
   erasure complete; relay each report in `erase_reports` verbatim.
   When it succeeds, Casa has closed the specialist's open conversations: relay
   `closed_conversations` (name any flagged `opened_after_confirmation` — opened after the operator
   confirmed), and if `conversations_still_open` is present, tell the operator those could not be
   closed and are still open.
3. Only now, unwire the delegate references step 1 found by applying ONLY the edit step of
   `recipes/delegate/unwire.md` (do NOT run its commit/reload/emit_completion — steps 5–7 below
   do that once).
4. If the result of step 2 carries `plugin_data_note` (it does whenever step 2 cascaded
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
5. `config_git_commit(message="uninstall specialist <slug>")`.
6. `casa_reload(scope="agents")` — evicts the removed agent from the live
   registry (canonical commit → reload → emit order, see `completion.md`) — THEN
   `casa_reload(scope="agent", role="<resident>")` once for each resident whose
   `delegates.yaml` step 3 edited: the `agents` sweep never re-reads a live resident, so
   without it the resident keeps advertising the removed delegate until its next reload.
7. `emit_completion(...)`.

CAS blobs are NOT deleted by uninstall (retained for a possible future re-install at the same
digest, and GC sweep execution is out of this plan's scope — see Task N1d's CAS pin/reference
model).

## Common mistakes

- Unwiring delegates BEFORE the uninstall has committed — a declined warning or a cancelled
  erase question would leave `delegates.yaml` edited for a specialist that is still installed.
  Unwire right after the uninstall succeeds (step 3), before the commit and the resident reloads;
  skipping it leaves a stale `delegates.yaml` entry that fails to resolve on the next delegation.
- Skipping `casa_reload(scope="agents")` — the on-disk instance is gone but the live registry still
  holds the (now orphaned) loaded agent until reload runs.
- Assuming an operator-installed plugin that targeted the removed slug is gone too — it is NOT
  cascaded out; only slug-OWNED entries are. Tell the operator it survived as a `pending_targets`
  entry, and let them decide whether to retarget it or reinstall the specialist.
