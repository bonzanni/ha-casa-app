# Recipe: unassign or remove a plugin

Two tools, two scopes:

- **`plugin_unassign(name, target)`** — drop ONE target's assignment; the
  plugin stays registered (and assigned to its other targets). Use this to stop
  a specific agent from loading a plugin.
- **`plugin_remove(name)`** — remove the plugin from the registry entirely. The
  immutable artifact is retained on disk for GC (`artifact_retained: true`); a
  removed default stays removed across upgrades (the registry's `seeded_defaults`
  bookkeeping is intentionally untouched — no resurrection).

**Neither one deletes the plugin's data or revokes anything.** A removal drops
Casa's registry entry. The plugin's CLI-managed persistent data directory
(`CLAUDE_PLUGIN_DATA`) is NOT deleted — it may hold stored authorizations such
as OAuth tokens, whatever it holds survives the removal, and reinstalling the
same plugin re-attaches to it. Casa cannot see whether the plugin stored
anything there, so every wording is "may", never "did". Casa performs no provider-side revocation. The tool's own
result says so, in `plugin_data_may_remain`, `provider_revocation_performed`
and `plugin_data_note`.

## Do it

1. `plugin_list()` to confirm the name + its current targets.
2. `plugin_unassign(name, target)` or `plugin_remove(name)`.
3. The tool reloads the affected in-casa agents and verifies the plugin is GONE
   from their bindings (an `absent` postcondition). A non-ok result means an
   agent still binds it — surface it.
4. **Only when the result carries `plugin_data_note`** — a `plugin_remove`
   that committed; a `plugin_unassign` never carries it, and there is nothing
   to warn about after one — report `plugin_data_note` to the operator
   verbatim, alongside the outcome. It is the only place they learn that
   stored authorizations may have survived — "may", because Casa cannot see
   whether the plugin ever stored any. Do NOT restate it as a deletion or a
   revocation: Casa performed neither. If they want the access to end, they
   revoke it at the provider.

If a removal raises instead of returning a result, the registry may already
have committed. Say that: the removal may have taken effect, and if it did,
the same survival applies — nothing was deleted and nothing was revoked. Run
`plugin_list()` to see which.

If the plugin required secrets, clear its plugin-env.conf entries afterward (see
`secrets.md`) — and that clearing DOES need its own
`casa_reload(scope="plugin_env")`, exactly as `secrets.md` instructs; clearing
an entry is neither credential deletion nor provider revocation. **For the
removal itself no separate casa_reload is needed** — reload + verify happen
inside the tool. Report the outcome and `emit_completion(...)`.
