# Recipe: unassign or remove a plugin

Two tools, two scopes:

- **`plugin_unassign(name, target)`** — drop ONE target's assignment; the
  plugin stays registered (and assigned to its other targets). Use this to stop
  a specific agent from loading a plugin.
- **`plugin_remove(name)`** — remove the plugin from the registry entirely. The
  immutable artifact is retained on disk for GC (`artifact_retained: true`); a
  removed default stays removed across upgrades (the registry's `seeded_defaults`
  bookkeeping is intentionally untouched — no resurrection).

**Neither one deletes the plugin's data, and neither revokes anything at the
provider.** A removal drops Casa's registry entry. Casa also tears down its own
authorizations for the plugin — artifact grants, trigger consents, persisted
callback consents — but that teardown is internal, best-effort and not reported
in the result, so tell the operator nothing about it in either direction. What
the disclosure is about is the other side of Casa, which no Casa operation
reaches. The plugin's CLI-managed persistent data directory
(`CLAUDE_PLUGIN_DATA`) is NOT deleted — it may hold stored authorizations such
as OAuth tokens, whatever it holds survives the removal, and reinstalling the
same plugin re-attaches to it. Casa cannot see whether the plugin stored
anything there, so every wording is "may", never "did". Casa performs no provider-side revocation. The tool's own
result says so, in `plugin_data_may_remain`, `provider_revocation_performed`
and `plugin_data_note`.

## Do it

1. `plugin_list()` to confirm the name + its current targets.
2. `plugin_unassign(name, target)` or `plugin_remove(name)` (see "A plugin that
   can erase its own data" below when it answers `erase_choice_pending`).
   If it returns `ok: false, kind: "open_conversations_unconfirmed"`: NOTHING
   changed (a `plugin_remove` withdrew any erase question and asked none) — a
   specialist the change reaches has open conversations. Relay the result's
   `warning` to the operator VERBATIM (it lists each specialist's conversations
   and says what the change means for them) and ask whether to go ahead. Only
   on a yes, repeat the same call — the same `erase_data` choice — with
   `acknowledged_conversations` set to the ids the result lists; on a no, stop
   and tell the operator nothing changed — a declined warning voids those ids,
   so never re-pass them without asking again. The calls Casa later tells you
   to make after the erase question already carry them: make them exactly as
   given. If a later result carries `opened_after_confirmation` (or
   `opened_while_this_change_ran`), tell the operator about those
   conversations too, with the result's `open_conversation_notice` verbatim.
3. The tool reloads the affected in-casa agents and verifies the plugin is GONE
   from their bindings (an `absent` postcondition). A non-ok result means an
   agent still binds it — surface it.
4. **Only when the result carries `plugin_data_note`** — a `plugin_remove`
   that committed; a `plugin_unassign` never carries it, and there is no data
   note to give after one — report `plugin_data_note` to the operator
   verbatim, alongside the outcome. It is the only place they learn that
   stored authorizations may have survived — "may", because Casa cannot see
   whether the plugin ever stored any. Do NOT restate it as a deletion or a
   revocation at the provider: Casa performed neither of those. If they want
   the external access to end, they revoke it at the provider.

## Requirements: state the consequence once, never block

The result may carry `dependents` — other plugins whose declared requirements
name this one on the targets you just unassigned or removed — and, on a
`plugin_remove`, `leftover_requirements`: what the removed plugin's own
requirements leave assigned to its former targets. Say each once, plainly
("quarterly recommends Gmail read-only for finance; finance no longer has
it"), and do NOT unassign or reassign anything on their account: a leftover
grant is the operator's to keep or drop.

## A plugin that can erase its own data (`erase_data`)

Some plugins declare an eraser: a tool of their own that erases their data.
There are two kinds. **Erase everything** erases all of it, sign-ins included,
and revokes what it can at the providers. **Erase data, keep sign-ins** erases
the data but keeps what a reinstall needs to carry on without signing in again.
For those plugins, Casa asks the operator ONE question itself, as a keyboard in
their DM: **Keep data**, the erase options the plugin declares, and **Cancel**.
You never ask it and never choose for them. The operator's tap decides which
eraser runs; your call is the same `erase_data=true` either way.

- Call the removal WITHOUT `erase_data`. If the result is
  `kind: "erase_choice_pending"`, nothing was removed: tell the operator the
  question is in their DM and stop. Casa continues this topic with their
  choice and tells you exactly which call to make next.
- `erase_data=false` is the ordinary removal (the data stays, as below).
  `erase_data=true` works only after the operator's Erase tap: the first such
  call returns `kind: "erasure_running"` — the plugin's eraser runs and
  nothing is removed yet; wait, Casa sends the eraser's result into this topic.
  `kind: "erase_not_confirmed"` means there was no erase tap for this version:
  call again without `erase_data` to ask. `kind: "erase_unavailable"` means the
  plugin no longer declares the eraser the operator chose (it was updated):
  call again without `erase_data`.
- When the eraser reports the erasure complete, Casa tells you to call
  `erase_data=true` again; that call removes the plugin and its result carries
  `erasure: "complete"` and the plugin's report (`erase_report`, or
  `erase_reports` for a specialist), and `erasure_kind` says which eraser ran.
  Relay the report verbatim, and the `plugin_data_note`: Home Assistant backups
  taken earlier still contain the data.
- After **Erase everything** (`erasure_kind: "everything"`), Casa has already
  cleared the plugin's plugin-env.conf references (`env_references_cleared`)
  and reloaded the plugin environment — do not clear them again. Only if the
  result carries `env_reload_ok: false`, run `casa_reload(scope="plugin_env")`.
  If it carries `env_references_not_cleared`, Casa could not clear those: tell
  the operator, remove each with `remove_plugin_env_reference`, then run
  `casa_reload(scope="plugin_env")`.
  After **Erase data, keep sign-ins** (`erasure_kind: "data_only"`), the
  references stay on purpose — a reinstall uses them — so do not clear them.
- When the erasure did NOT complete, nothing was removed. Relay the plugin's
  report verbatim — never summarise it as success — and ask the operator
  whether to try again later (run the uninstall again, which asks again) or to
  uninstall anyway keeping whatever is left (`erase_data=false`). Removing the
  plugin would destroy the one tool that can finish the erasure, so never do
  that without their answer.

If a removal raises instead of returning a result, the registry may already
have committed. Say that: the removal may have taken effect, and if it did,
the same survival applies — no plugin data was deleted and nothing was revoked
at the provider. Run `plugin_list()` to see which.

After an ordinary removal (a plugin without an eraser, or Keep data), if the
plugin required secrets, clear its plugin-env.conf entries afterward (see `secrets.md`) — and that clearing DOES need its own
`casa_reload(scope="plugin_env")`, exactly as `secrets.md` instructs; clearing
an entry is neither credential deletion nor provider revocation. **For the
removal itself no separate casa_reload is needed** — reload + verify happen
inside the tool. Report the outcome and `emit_completion(...)`.
