# Recipe: delete a specialist — RETIRED

`rm -rf` of specialist directories is no longer supported (hooks deny it):
installed specialists are managed components with pipeline-owned on-disk state.

- To remove an installed specialist: follow `recipes/specialist/uninstall.md`,
  which owns the correct sequence — unwire delegates FIRST (uninstall does NOT
  auto-unwire), then `specialist_uninstall` and commit.
- If a stray/legacy directory exists under `agents/specialists/` that the loader
  reports as failed, tell the operator; cleaning up non-pipeline state is an
  operator decision, not a configurator mutation.
