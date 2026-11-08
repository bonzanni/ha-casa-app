# Recipe: update an existing specialist — RETIRED

Hand-editing files under `agents/specialists/<slug>/` is no longer supported
(hooks deny it): those files are materialized from the installed component, and
any manual edit would be overwritten by the next reconcile.

- New component version: `recipes/specialist/upgrade.md`.
- Different persona: `recipes/persona/apply.md`.
- Different settings — the specialist's own config values, the names its
  inspection lists: an upgrade to the version it already has, passing the new
  settings. Follow "Change an installed specialist's settings" at the top of
  `recipes/specialist/upgrade.md`. Its model tier and memory budget are not
  settings: they come with the component version.
- A secret one of its bundled plugins reads: `set_plugin_env_reference` with the
  plugin's scoped name `<slug>.<plugin>`, following `recipes/plugin/secrets.md`.
- Bad upgrade: `recipes/specialist/rollback.md`.
