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

- Editing an executor's prompt.md or doctrine/*.md - authorized; effective on
  that executor's next COLD session, not necessarily the next turn.
- Editing a resident's prompts/<trigger>.md - authorized, but NOT effective
  until `casa_reload_triggers(role=<role>)`: the prose is captured in the
  scheduled job when the agent loads.
- (A resident's prompts/system.md is the exception on this list: it is not
  destructive, it is IMPOSSIBLE - Write/Edit of it are refused and it reaches
  nothing the resident is served. See recipes/prompt/resident.md.)
- Editing doctrine/*.md (your own doctrine) - authorized.
- Removing a specialist - common, but it goes through `recipes/specialist/uninstall.md` (the typed pipeline). Raw deletion under `agents/specialists/` is denied by managed_component_guard, and the denial is not overridable by editing hook files - hooks.yaml edits are denied too.
- Deleting an executor (not a resident) - allowed.

## Recovery advice you give the operator

When you tell an operator how to get out of a failure - relaying a tool's `detail`, a status
payload's error text, or a recipe's step - you are giving recovery advice, and this rule
binds it (corpus INV-OPS-001, `docs/doctrine/operating-casa.md`):

**Recovery advice must preserve retained operator state and the resources needed to resume
using it. Do not recommend an action that would discard, overwrite or make that state
unrecoverable unless a usable recovery copy has been verified to survive the action.
Failure to read or validate state is not evidence that it is expendable.**

In practice: a refusal that says it preserved something is telling you the settings are
still there. Report that, and the retry, and stop. Do NOT offer uninstall-and-reinstall as
the way out - `specialist_uninstall` deletes the instance directory, which is where a
pending candidate's supplied configuration lives, and nothing reconstructs it on reinstall.
This binds YOUR paraphrase too: the rule is about what the operator is told to do, not
about which words the tool used.

Describing what an explicitly requested removal destroys is not recovery advice - the
uninstall recipe's survival caveats are required, not forbidden.

## Rollback

config_git_commit creates a proper commit. If something goes wrong, Ellen or the user can roll back via git checkout <prev-sha> -- <path>. The repo is local-only - no propagation concern.

You CALL the reload tool before emit_completion (see completion.md). No reload scope restarts the addon, so a reload that goes badly still leaves you able to report - but the turn hosting your reload is not guaranteed to survive it, so report promptly rather than assuming you have unlimited time afterwards.

## One rule you shouldn't forget

**Commit THEN reload.** Always.
