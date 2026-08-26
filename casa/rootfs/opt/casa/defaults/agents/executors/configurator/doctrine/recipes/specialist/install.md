# Recipe: install a specialist from a repository

A specialist component is a distributable package living in its own repository. This is the ONLY
way to add a specialist — the legacy hand-authored path is retired (the loader refuses
hand-created directories; see recipes/specialist/create.md). Installed specialists are managed:
their identity, persona binding, and runtime files are all derived from the component, never
hand-edited.

A component may declare bundled or repo-sourced plugin dependencies (e.g. a weather specialist's `weather` plugin).
These are NEVER installed separately: `specialist_install_inspect` resolves and validates the whole
dependency closure (persona, corpus, plugins) in one pass, ONE consent DM covers the specialist AND
every dependency together, and `specialist_install_commit` activates all of it atomically. Do not
call `plugin_add` for a specialist's declared plugin — see `recipes/plugin/add.md`.

## Ask the user

1. **Repository locator** (`owner/repo` + a ref — branch/tag/sha).
2. Nothing else up front — `specialist_install_inspect` reports the component's own declared
   mission, default persona, dependencies (including any bundled/declared plugin), and required
   config/secret names; ask the operator to supply THOSE by name once inspection returns.

## Steps

1. `specialist_install_inspect(repo=..., ref=...)`. On any `ok: false`, report the `kind`/`detail`
   verbatim and stop — do NOT retry with fabricated inputs. On `ok: true` the result carries a
   `receipt_id` for this exact inspected closure — hold onto it verbatim; `specialist_install_commit`
   requires it back unchanged.
2. Summarize the inspection result as a plain message in the topic (mission, default persona,
   dependencies — including what any bundled plugin needs — required config/secret names) so the
   operator sees it BEFORE the DM consent prompt fires — the DM keyboard (posted server-side by
   `prompt_specialist_install_consent`, not by this recipe) is the actual approval gate, and it
   already covers the WHOLE install (the specialist AND every dependency) in ONE consent; this step
   is purely informational context in-topic. There is no separate consent, and no separate
   `plugin_add`, for a dependency plugin — ever.
3. Wait for the operator's DM tap (Approve/Deny) to resolve. There is no polling tool — the
   `specialist_install_commit` call in the next step will itself refuse with `kind:
   "consent_missing"` if the tap has not landed yet; on that specific error, tell the operator you
   are waiting for their DM response and then stop (do not loop-retry). After Approve the install
   normally continues automatically — a synthetic resume turn carries this recipe forward — so you
   do NOT ask for a second message by default; only if that automatic resume fails to deliver
   would the operator need to send any message in the topic to continue.
4. Once approved: `specialist_install_commit(component_id=..., version=..., root_digest=...,
   slug=..., staged_dir=..., receipt_id=..., config={...}, secret_names_provided=[...])` using the
   EXACT values `specialist_install_inspect` returned, `receipt_id` included. Omitting it (or
   passing a stale one from an earlier inspect) refuses with `kind: "receipt_required"` — re-run
   inspect and retry with the fresh id; never fabricate one. If the result carries
   `plugin_data_note` — the commit's owned-set swap replaced a stale owned entry, which
   is rare but is a removal — on ANY outcome, including an `ok:false` result that
   carries no `state` at all, relay it verbatim with the names in
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
5. If `state == "pending-configuration"`: report which config/secret names are still missing; the
   operator supplies them via a follow-up `specialist_install_commit` call with the SAME
   `staged_dir` and `receipt_id` (re-inspect if `staged_dir` has been cleaned up — staging is not
   guaranteed durable across a restart, and a re-inspect mints a fresh `receipt_id` too).
6. If `state == "active"`: wire delegation by applying ONLY the edit steps of
   `recipes/delegate/wire.md` (edit `delegates.yaml` idempotently + ensure the
   delegate tool is allowed). Do NOT run wire.md's own commit/reload/emit_completion
   — steps 8–10 below perform the single commit + reload + completion for the
   whole install.
7. **Wire bundled plugins' env vars — part of THIS install, not a follow-up.** For each bundled
   plugin the inspection listed with a non-empty `env_names` (mirrored as `required_env_vars` in
   the commit result, keyed by the SCOPED registry name `<slug>.<plugin>` — use that exact key as
   the plugin identifier below), run the `recipes/plugin/secrets.md` flow now: ask the operator
   for each value or 1Password reference you cannot derive (e.g. a vault name is the installer's
   choice — never guess it), `set_plugin_env_reference(plugin="<slug>.<plugin>", ...)` once per
   var, then `casa_reload(scope="plugin_env")`, and confirm via
   `verify_plugin_state(plugin_name="<slug>.<plugin>")` that no `secrets[*].status: unresolved`
   remains. An unresolved required var withholds the plugin from
   session builds entirely: the specialist's very first tool call would be refused with
   "required env unresolved", costing the operator a second configurator engagement (#499).
   A plugin with empty `env_names` needs nothing here.
8. `config_git_commit(message="install specialist <slug> from <repo>@<ref>")`.
9. `casa_reload(scope="agents")` (mandatory — see `completion.md`; an `active` install is on disk
   but not in the live registry until reload runs).
10. `emit_completion(status="ok", text="Installed specialist <slug> from <repo>@<ref>; reloaded and
    wired for delegation.")`.

## Common mistakes

- Calling `specialist_install_commit` before the operator has actually tapped Approve — it will
  correctly refuse (`kind: "consent_missing"`); this is not a bug to work around, it IS the consent
  gate.
- Calling `specialist_install_commit` without `receipt_id`, or with one carried over from an
  EARLIER inspect — it refuses with `kind: "receipt_required"`; always use the id the LATEST
  `specialist_install_inspect` call returned.
- Calling `plugin_add`/`plugin_assign` for a dependency the component already declares — it never
  needs a separate add; inspect + commit install it as part of the SAME bundle. Once active, those
  tools (and `plugin_update`/`plugin_unassign`/`plugin_remove`) refuse the owned entry with `kind:
  "owned_by_specialist"` — use `specialist_upgrade`/`specialist_uninstall` on the SLUG instead.
- Forgetting `casa_reload(scope="agents")` — an `active` install is on disk but not in the live
  registry until reload runs.
- Completing the engagement with a bundled plugin's `env_names` unwired (#499) — the install
  reports success, but the unresolved var keeps the plugin withheld and the specialist's first
  use hits the requires gate. Do not describe such a var as something the specialist's own setup
  tool will provision: a var in `env_names` is the installer's to wire, in step 7, here.
- Relaying a bundled plugin's OWN consent (a trigger/callback/event
  `*_pending_ack` after commit) through this engagement's ask, or asking the
  assistant to `ask_user` it (#494): those surfaces accept the Approve tap and
  commit NOTHING — the routing ledger stays empty and setup wedges. The
  server-posted DM keyboard is the only committing surface; when it expired or
  was missed, call `consent_reprompt` and relay its result.
- Re-approving a DIFFERENT re-fetch under the same slug: a second `specialist_install_inspect` call
  yields a NEW `root_digest` (and a NEW `receipt_id`) if the repo moved (or any bundled
  persona/corpus/plugin dependency changed), which requires a NEW consent DM — the old approval
  never carries over (see `install_consent_identity`'s four-field binding, now keyed on
  `root_digest`).
