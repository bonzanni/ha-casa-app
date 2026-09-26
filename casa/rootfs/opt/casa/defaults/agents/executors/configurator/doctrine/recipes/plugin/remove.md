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
3. The tool reloads the affected in-casa agents and verifies the plugin is GONE
   from their bindings (an `absent` postcondition). A non-ok result means an
   agent still binds it — surface it.
4. **Only when the result carries `plugin_data_note`** — a `plugin_remove`
   that committed; a `plugin_unassign` never carries it, and there is nothing
   to warn about after one — report `plugin_data_note` to the operator
   verbatim, alongside the outcome. It is the only place they learn that
   stored authorizations may have survived — "may", because Casa cannot see
   whether the plugin ever stored any. Do NOT restate it as a deletion or a
   revocation at the provider: Casa performed neither of those. If they want
   the external access to end, they revoke it at the provider.

## A plugin that can erase its own data (`erase_data`)

Some plugins declare an eraser: a tool of their own that erases everything they
hold and revokes what they can at their providers. For those, Casa asks the
operator ONE question itself, as a keyboard in their DM: **Keep data / Erase
data / Cancel**. You never ask it and never choose for them.

- Call the removal WITHOUT `erase_data`. If the result is
  `kind: "erase_choice_pending"`, nothing was removed: tell the operator the
  question is in their DM and stop. Casa continues this topic with their
  choice and tells you exactly which call to make next.
- `erase_data=false` is the ordinary removal (the data stays, as below).
  `erase_data=true` works only after the operator's Erase tap: the first such
  call returns `kind: "erasure_running"` — the plugin's eraser runs and
  nothing is removed yet; wait, Casa sends the eraser's result into this topic.
  `kind: "erase_not_confirmed"` means there was no Erase tap for this version:
  call again without `erase_data` to ask.
- When the eraser reports the erasure complete, Casa tells you to call
  `erase_data=true` again; that call removes the plugin and its result carries
  `erasure: "complete"` and the plugin's report (`erase_report`, or
  `erase_reports` for a specialist). Relay the report verbatim, and the
  `plugin_data_note`: Home Assistant backups taken earlier still contain the
  data.
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

If the plugin required secrets, clear its plugin-env.conf entries afterward (see
`secrets.md`) — and that clearing DOES need its own
`casa_reload(scope="plugin_env")`, exactly as `secrets.md` instructs; clearing
an entry is neither credential deletion nor provider revocation. **For the
removal itself no separate casa_reload is needed** — reload + verify happen
inside the tool. Report the outcome and `emit_completion(...)`.
