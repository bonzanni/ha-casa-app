# Safety - destructive ops and what hooks block

Hooks run BEFORE your tool call. If a hook denies, you'll see a message starting with the policy name (casa_config_guard, commit_size_guard, path_scope, managed_component_guard). Denials come in two classes:

- **Confirmation-gated** (destructive-adjacent ops below): ask the user in the engagement topic; if they agree, retry and the framework may let it through.
- **Non-overridable** (managed component trees, hook-policy files): there is NO retry path - no user agreement lifts them, and editing hook files is itself denied. Use the typed tool or recipe the denial message names.

You have no shell: Bash is not in your toolset. Edit files with Write/Edit, search with Grep/Glob, and mutate managed components only through the typed tools.

## Fully blocked (no override)

- Write/Edit anywhere under /data/** - runtime state; touching it corrupts memory.
- Write/Edit under /config/schema/** - authoritative code; breaks load path.
- Write/Edit under /opt/casa/** - addon source tree.
- Write/Edit of managed component state (agents/specialists/**, specialists/**, bindings/**, personas/**, plugins/**) and of any agent's hooks.yaml - managed_component_guard.

## Destructive-adjacent (ask the user first)

- Anything that would remove a resident - residents are fixed (the resident/create and resident/delete recipes are retired stubs); hooks deny resident deletion.
- Any change to policies/scopes.yaml - classifier is trained on this corpus. Show the user the diff before committing.
- Changes that touch more than 20 files in one commit - commit_size_guard will deny.

## Things that LOOK destructive but aren't

- Editing prompts/*.md - no reload, file is read per-turn.
- Editing doctrine/*.md (your own doctrine) - authorized.
- Removing a specialist - common, but it goes through `recipes/specialist/uninstall.md` (the typed pipeline). Raw deletion under `agents/specialists/` is denied by managed_component_guard, and the denial is not overridable by editing hook files - hooks.yaml edits are denied too.
- Deleting an executor (not a resident) - allowed.

## Rollback

config_git_commit creates a proper commit. If something goes wrong, Ellen or the user can roll back via git checkout <prev-sha> -- <path>. The repo is local-only - no propagation concern.

You CALL the reload tool before emit_completion (see completion.md). No reload scope restarts the addon, so a reload that goes badly still leaves you able to report - but the turn hosting your reload is not guaranteed to survive it, so report promptly rather than assuming you have unlimited time afterwards.

## One rule you shouldn't forget

**Commit THEN reload.** Always.
