---
last_reviewed: 2026-11-24
---

# The tool interface

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The mutation interface Casa exposes to its agents: how the tool surface is assembled, which
checks a tool makes past its grant, the configurator's lifecycle, vault and reload tools, and
what a configuration commit reloads. It covers families and contracts, not a
per-tool catalog — the registry tuple is the authority on what exists. Dispatch and
authorization live in `architecture/mcp-and-tools.md`; this file is about what the tools
themselves guarantee. What a tool result promises, what the sending tools admit and report,
and how engagement completion and a delegated run end — which failures roll back versus
which merely report — are in [`tool-outcomes.md`](tool-outcomes.md).
What the plugin and specialist mutation tools guarantee — how a
mutation orders its registry commit against the convergence that follows, and what its
envelope may claim about a plugin's integration — is in
[`plugin-mutation-tools.md`](plugin-mutation-tools.md); what a committed removal discloses
is in [`plugin-removal.md`](plugin-removal.md).
How `ask_user`'s operator-DM arm and `wipe_memory`'s consent keyboard report a question
that cannot be delivered, or that is over before the tool returns, is in
[`tool-questions.md`](tool-questions.md).

## Mental model

**One registry is the whole surface.** A single module-level tuple of handlers drives the
SDK server an agent sees. Adding a tool there exposes it; grant filtering is a separate,
later cut against fully-qualified names —
with one coarse escape: a *server-level* `mcp__casa-framework` grant exposes every tool at
once, and only its absence makes filtering per-tool. The SDK server also loads eagerly
(`alwaysLoad`), so every added tool's schema lands in every SDK session's up-front context
— adding a tool is a prompt-cost decision, not just a dispatch one. The one tool outside this
registry is the [`scratchpad`](scratchpad.md) of a delegated client or a background job's
session: its handler is built per client around the pad's key, on a server of its own that stays
deferred, except for a role with `session.long_runs: true` and a job's session, which get that
server eagerly.

**Argument-schema validation happens on the SDK path.** The SDK path registers the
decorated tools with their schemas, and MCP validation there rejects a missing required
argument or a bad enum before the handler runs — code and tests rely on it.

**A grant can be narrower inside the tool.** Some tools check the caller's role
themselves, past the grant, and read the EXECUTING role (`tools.py::_effective_caller_role`):
the role Casa stamps on a delegated run, else a bound engagement's, else the
origin. The configurator's privileged tools are [`configurator.md`](configurator.md)'s
INV-CFG-017, so an agent the configurator delegated to does not pass as it.

**A lifecycle surface is only finished when it can also undo.** Personas install, apply and
publish through consent-bound tools; removing one, sweeping the versions nothing refers to,
enumerating what is installed and revoking a stored approval are tools too — not filesystem
work, because the tree they live in is denied to the configurator precisely so that the
sequencing stays owned by code. The removal tools refuse rather than force, and a refusal
names its referrers; the sweep reports what it kept and why, so "some were skipped" is never
the whole story an operator gets. What each refusal means, and why removal is decided by
references rather than by use, belongs to [`architecture/personality.md`](personality.md).

**A vault is read by id and role, never repeated by name.** The configurator's two vault
tools, `list_vault_items` and `get_item_fields`, return an item's op id and the query term it
matched, and per field the op id, a role from a closed set and a validated type — never a
title, a label, a section, a value, a reference or a note — through one projection shared
with the exploration `plugin_add` performs. Their `vault` argument is optional and
falls back to the configured default vault, which the world-state block of every
configurator run names (`Default vault:`, beside the real `Addon version:`), so the configurator can
omit it or name it; the search key stays required by schema, so the whole vault is never
enumerated. A failing `op` call yields a fixed classification and the exit code, never the
CLI's own text, because whatever `op` prints on failure would otherwise reach the
configurator's transcript and, from there, the chat. The contract is
[`architecture/plugin-secret-exploration.md`](plugin-secret-exploration.md)'s.
The same schema discipline holds for tools called by literal: `casa_reload` requires only
`scope`, and `emit_completion` nothing at all, because a literal call
(`casa_reload(scope="plugin_env")`, `emit_completion(status=..., text=...)`) omits the
rest, and the shorthand declaration had made every key required — a call the validator
refused before the handler ran.
Where a scope needs a `role`, the handler canonicalises the argument before dispatch:
the value is the agent's directory name (`assistant`, a specialist's slug), and the
tier-prefixed forms every plugin surface uses for the same role — `resident:assistant`,
`specialist:<slug>`, the shape of `plugin_add`'s `targets` and of its `reloaded:` list —
are accepted with the prefix stripped (`tools.py::_reload_role_arg`), so the configurator can
hand back exactly what it just read; a bare prefix is still `role_required`, and any
other prefix is the role as given. The same canonicalisation applies to
`casa_reload_triggers`.

**What a call reports is the other half.** The two-layer result envelope (INV-TOOL-001),
the media policy table, what `send_message`, `send_media`, `ask_user` and `set_reminder`
admit and report, and how engagement completion and a delegated run end (INV-TOOL-006) are
in [`tool-outcomes.md`](tool-outcomes.md).

## Contracts & invariants

**INV-TOOL-014**: A non-empty `config_git_commit` runs, before it returns and whoever called it, the reloads `commit_reload_scopes` names for the paths the commit changed, each through the reload tool's fence and plugin-health refresh, and returns every reload's envelope; a reload that fails or reports failures leaves the commit standing and adds a warning.

The commit reads back the paths it changed and hands them to
`reload.py::commit_reload_scopes`: a live resident's trigger inputs alone reload its
`triggers`, any other file of it its `agent`; a resident or specialist that appears or goes,
or a specialist's link, `agents`; an edit inside a present specialist its `agent`;
`policies/` `policies`; a mix of kinds, an unknown path or unreadable paths one `full`.
Nothing reloads for a plugins-only commit (the plugin tools reload and commit their own
change), a resident's staged binding under `bindings/` (only a restart activates it,
INV-CFG-003), anything left under `agents/executors/` (read by nothing: the configurator
ships with Casa, [`configurator.md`](configurator.md)), or the phone line's prompts under
`phone/` (read for every call, [`phone.md`](phone.md)). Each runs through
`tools.py::_run_reload`, as `casa_reload` does (INV-CFG-011). Two commits reload twice,
and a cancelled task's commits stay live: nothing reads the caller's engagement.

What it does not cover: writes no commit sees — a specialist's instance tuple, which
`persona_apply` and the specialist bundle tools reload themselves, and `plugin-env.conf`,
whose `plugin_env` reload the configurator calls; commits made without the tool (boot's
re-bind, Casa's own registry and trigger-file commits); a hand-edited plugin registry, inert
until `full`; and best-effort failures a reload does not report. A resident whose staged
persona awaits its restart answers every reload of it with `restart_required`, so a `full`
stops there.

## Failure behavior

**An authorizing argument is type-checked in the handler.** Any argument that acts as an
authorization — a `force` that permits a destructive path, a flag that waives a refusal —
must be type-checked in the handler and rejected as a bad request, not coerced: Python's
truthiness makes the string `"false"` indistinguishable from `True` to a `bool(...)`
coercion. Treating the declaration as if something upstream enforced it for every caller is
how a refusal becomes opt-out by typo.

**Memory cannot answer.** The memory tools — `search_memory`, `browse_memory_day`,
`open_memory`, `correct_memory` — report a store they cannot read as `unavailable`, never
as an empty result, and refuse a request they cannot read before reading anything; they
are described in [`memory-tools.md`](memory-tools.md) (INV-MEM-010, INV-MEM-019).

`get_schedule` lists the calling role's own triggers and reminders, not the operator's
calendar.

`find_contact`, `refresh_contacts` and `set_contact_marks` — the assistant's reads and
marks over the operator's synced address book, refused below private clearance, with a
priority contact applied only by the operator's tap on Casa's card — are described in
[`contacts.md`](contacts.md) (INV-CONTACTS-004, INV-CONTACTS-006).
`connect_login`, which shows the assistant the titles of the few vault items that could hold
one of Casa's own logins and stores her choice once Casa's line naming it reached the
operator, is described in [`logins.md`](logins.md); it is the one tool that returns a vault
title. `read_call` and `phone_settings` — the calls the phone line answered and its voices,
models and grants, a wider grant applied only by the operator's tap — are described in
[`phone.md`](phone.md) (INV-PHONE-008).

The assistant's `start_job` and a job session's `report_job_progress` are described in
[`background-jobs.md`](background-jobs.md); a specialist job's `ask_operator`, and the start
line `start_job` posts for a job the operator started, in
[`background-job-direct-chat.md`](background-job-direct-chat.md).
`list_inbound_files`, which lists the files the operator sent in Telegram with the path
the Telegram default agent opens each with, and states that listing is not reading, is
described in [`inbound-files.md`](inbound-files.md).
`share_inbound_file`, which copies one of those files into the plugin handoff folder and
returns the path to pass to a plugin tool, is described in
[`plugin-handoff.md`](plugin-handoff.md).
`vault_drop_off`, the assistant's one vault write, which stores a sign-in link in the
drop-off a plugin declares instead of a delegation brief, is described in
[`vault-drop-off.md`](vault-drop-off.md).
`hand_off`, which a specialist — answering a request, or asked in its own chat — uses to hand part of its work to another
specialist and wait for the answer, is described in
[`specialist-handoff.md`](specialist-handoff.md).
`guest_access` has two faces. With it a specialist the operator summoned in a group lets
everyone there ask it (`allow`), stops that (`revoke`) or reports it (`status`) — the group
taken only from the turn's server-stamped origin, never from the arguments; from the
resident's own chat the operator lists every group any specialist may answer in (`list`)
and revokes one by the role and chat id the list shows. Both are described in
[`guest-summons.md`](guest-summons.md), as is the guest run, which is offered no Casa tool at
all.

## Extension points

**A new tool** is a decorated handler added to the registry tuple — that alone puts it on
the SDK server of every session whose grants reach it, which is why the addition is also a
security decision; grant filtering and
the coverage ledger pick it up from there.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::create_casa_tools`
- `casa/rootfs/opt/casa/tools.py::select_casa_tools`
- `casa/rootfs/opt/casa/tools.py::config_git_commit`
- `casa/rootfs/opt/casa/tools.py::_run_reload`
- `casa/rootfs/opt/casa/reload.py::commit_reload_scopes`
- `casa/rootfs/opt/casa/tools.py::_reload_after_untracked_write`

**Tests**
- `tests/test_casa_reload_role_forms.py`
- `tests/test_config_git_commit_reload.py`
- `tests/test_configurator_doctrine_commit_reloads.py`
- `tests/test_1095_change_load_timing.py`

**Related**
- [`architecture/mcp-and-tools.md`](../architecture/mcp-and-tools.md)
- [`architecture/tool-outcomes.md`](../architecture/tool-outcomes.md)
- [`architecture/plugin-mutation-tools.md`](../architecture/plugin-mutation-tools.md)
- [`architecture/tool-questions.md`](../architecture/tool-questions.md)
- [`architecture/configuration.md`](../architecture/configuration.md)
- [`architecture/configurator.md`](../architecture/configurator.md)
<!-- END SOURCEMAP -->
