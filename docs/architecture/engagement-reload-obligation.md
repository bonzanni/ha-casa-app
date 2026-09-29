---
last_reviewed: 2026-08-23
---

# Engagement reload obligation

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The reload a completed engagement owes for configuration it committed, which reload
discharges which committed path, and why a plugin mutation's plugins-only persist commit owes
none. The admission side of ending an engagement — what a *successful* completion is refused
over — is in [`architecture/engagement-completion-gate.md`](engagement-completion-gate.md). The
reload envelope, and the `failures` list this obligation reads, are in
[`architecture/configuration.md`](configuration.md).

## Mental model

**The obligation is a ledger of committed paths.** `config_git_commit` records what an
engagement committed; the configurator's own `casa_reload` or `casa_reload_triggers` discharges
the committed paths its scope covers and no failure it reports keeps owed;
`casa_restart_supervised` takes the whole obligation over; and `emit_completion` reads what is
left. INV-TOOL-011 is when a commit arms nothing, INV-TOOL-012 is what a reload discharges, and
what is still owed at a successful completion is force-reloaded (the forced `full`,
INV-TOOL-012).

## Contracts & invariants

**INV-TOOL-011**: A non-empty `config_git_commit` whose engagement holds a plugin mutation's pre-activation credit — set when that mutation's reload and verification fully succeed, cleared when a later mutation's begins — arms no reload obligation when every path the commit changes is under `plugins/`, including while specialists are active and were re-materialized from unchanged inputs since the previous commit. The first non-empty commit spends the credit, exempt or arming, so a later commit with no new activation arms.

An engagement can owe a reload for configuration it committed. A non-empty
`config_git_commit` arms that obligation for its engagement unless the exemption below applies;
a successful reload discharges the committed paths its scope covers and no failure it reports
keeps owed (INV-TOOL-012, below), and
`casa_restart_supervised` takes the whole obligation over, deferring a restart to finalization
instead; and `emit_completion` with outcome `completed` that finds any path still owed calls
`casa_reload` with `scope="full"` before finalizing, because a model can skip, or mis-scope, the
reload the doctrine asks for. A plugin mutation tool reloads and verifies its
own targets, and on full success credits the engagement, so the commit that merely persists
the registry change it already activated is exempt — only when every changed path, read back
from git, is under `plugins/`. A path read that fails yields no paths and arms. The credit is
cleared when a mutation's reload-and-verify step begins and set again only if that step fully
succeeds (its postcondition holds and the trigger reconcile ran); finalizing the engagement
clears it too; and the first non-empty commit spends it, so it can never exempt a later commit that no activation produced. An empty
commit changes nothing and keeps it. The exemption is reachable at all because an unchanged
re-materialization writes nothing (INV-SPEC-019, in
[`architecture/specialist-lifecycle.md`](specialist-lifecycle.md)): without that, boot and
every specialist-tier reload left specialist operational files in the next commit.

What it does not cover: which reload discharges the obligation — that is INV-TOOL-012. A
commit that mixes plugin and other paths arms, even when the other paths are derived files that
a changed input rewrote once.

**INV-TOOL-012**: Inside an engagement, the reload obligation is discharged path by path, only by the configurator's own successful `casa_reload` or `casa_reload_triggers`, and only for committed paths its scope covers, that were committed before that call began, and that no subordinate failure it reports keeps owed: `triggers` covers the role's `triggers.yaml` and trigger prompts; `agent` the role's directory and the plugin registry; `policies` covers `policies/`; `agents` covers `agents/specialists/`, `specialists/` and the plugin registry; `executors` covers `agents/executors/`; `config_sync` covers what `agents` and `policies` do; `plugin_env` covers nothing. `full` covers everything, including a commit whose paths could not be read, and a reload a tool runs internally discharges nothing. A failure the reload names for one resident, specialist or executor keeps owed the resident or specialist directory of that name (for an installed specialist, its link and the content directories named for it), the plugin registry and the executor directory of that name; a failure it cannot pin on one unit, or whose unit is `specialists` or `executors` (the directory of every specialist or executor), keeps every path owed; and any failure keeps a commit whose paths could not be read.

The table is the configurator doctrine's "What requires what" (`reload.md` under the
configurator's doctrine), and an engagement that picks a scope that does not cover its commit is
force-reloaded. Following that table does not always leave nothing owed: its rows that name no
reload for a committed file — an executor's `prompt.md`, `observer.yaml` or `doctrine/` — still
arm, and only `executors` or `full` covers them, so an engagement that follows those rows is
force-reloaded at completion; and the plugin mutations' "none" row owes nothing only under
INV-TOOL-011's exemption. On the `triggers` row, `casa_reload_triggers`
does discharge a commit confined to its role's trigger inputs — and only that: a
commit that also edited the role's `character.yaml` stays owed until an `agent` reload (or
`full`) runs. The plugin registry sits on the `agent` and `agents` rows because those are the
reloads the doctrine names after a plugin assignment on one role and after a specialist bundle
whose plugins changed. Each committed path is recorded with a sequence number, so a commit that
lands while a reload is running — a new path, or the same path again — stays owed; and a path
read that fails records an entry only `full` discharges. The reloads a plugin or specialist
mutation runs for its own targets, and the reloads a `full`, `executors` or `config_sync` cascade
composes, discharge nothing of their own: the tool's persist commit follows its reload, and the
explicit reload the configurator calls is the one the rule reads. Only `casa_reload` and
`casa_reload_triggers` discharge; no reload handler touches the obligation. The failures are
the ones the reload reports in its envelope's `failures` (configuration.md, "A reload handler
catches a failure and carries on"). One that names a unit other than those two keeps owed only
the paths above for that name, so a specialist that was already broken costs no forced reload for a commit that
touches none of them.
One that does not — a scan that failed as a whole, a role that could not rebuild inside a
`policies` or `executors` cascade and so did not take the shared input, a map refresh, a plugin
overlay reconcile — keeps the whole commit owed. The forced `full` is one retry: when its result
still reports a failure, the guard logs that result at WARNING, and the obligation still ends at
completion.

What it does not cover: discharge certifies that the explicit reload ran and that no failure it
reported keeps the discharged paths owed, and nothing more. A failure the reload treats as
best effort and does not report is not seen: closing a replaced agent, scheduling a prompt
refresh, regenerating plugin health, the setup-episode kick, an unresolved plugin-environment
reference (that scope covers no path), and a defaults reconcile that failed inside
`config_sync`, whose cascades still apply the committed files. A failure that persists is
retried once, by the forced `full`, and then left, with the guard's WARNING as its record. And
the obligation certifies nothing about an engagement that was already open when the change was committed.

## Failure behavior

**The obligation's failure behaviour is stated with its invariants above.** What a reported
reload failure keeps owed, what a path read that fails records, and what happens when the
forced `full` still reports a failure are in INV-TOOL-012; what a failed path read at commit
time does to the plugins-only exemption is in INV-TOOL-011.

## Extension points

**A new reload scope** discharges nothing until `_reload_scope_covers` says which committed
paths it covers: a scope that function does not name covers no path, so an engagement that
reloads only through it is force-reloaded at a successful completion.

**A new place that arms, discharges or drops the obligation** fails
`tests/test_reload_obligation_coverage.py::test_reload_obligation_has_only_authorized_mutation_sites`
until that test's `_AUTHORIZED` table names it: the table counts every site in `tools.py`, and a
use in any other module fails it outright.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::_finalize_engagement`
- `casa/rootfs/opt/casa/tools.py::emit_completion`
- `casa/rootfs/opt/casa/tools.py::config_git_commit`
- `casa/rootfs/opt/casa/tools.py::_reload_scope_covers`
- `casa/rootfs/opt/casa/tools.py::_ReloadObligations`
- `casa/rootfs/opt/casa/tools.py::casa_reload`
- `casa/rootfs/opt/casa/tools.py::casa_reload_triggers`
- `casa/rootfs/opt/casa/config_git.py::changed_paths`

**Tests**
- `tests/test_plugin_persist_commit_real_repo.py`
- `tests/test_plugin_persist_commit_regressions.py`
- `tests/test_config_git_commit_tool.py`
- `tests/test_emit_completion_defensive_reload.py`
- `tests/test_reload_obligation_coverage.py`

**Related**
- [`architecture/engagement-completion-gate.md`](../architecture/engagement-completion-gate.md)
- [`architecture/configuration.md`](../architecture/configuration.md)
- [`architecture/plugin-mutation-tools.md`](../architecture/plugin-mutation-tools.md)
- [`architecture/specialist-lifecycle.md`](../architecture/specialist-lifecycle.md)
- [`architecture/tools-interface.md`](../architecture/tools-interface.md)
<!-- END SOURCEMAP -->
