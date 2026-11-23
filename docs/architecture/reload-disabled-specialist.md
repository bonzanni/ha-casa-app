---
last_reviewed: 2026-11-23
---

# Reload: a specialist read as disabled

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

What a reload, an `agents` sweep or a boot does when it reads a specialist's `enabled: false`: the shared retirement every file-reading scope calls, the named steps it reports, and the close of the specialist's open conversations. The reload scopes, their locks and what reload cannot pick up are [`configuration.md`](configuration.md).

## Mental model

A disable cannot be confirmed beforehand, so it takes effect when a reload or a boot reads the file; the contracts below say what each reader does then.

## Contracts & invariants

**INV-CFG-012**: A reload that reads a specialist's `enabled: false` constructs and registers nothing for it and retires none of its Casa-minted secret slots; and for each of — its runtime agent, its bus queue, its scheduled jobs and webhook routes, its agent-registry entry, its delegation-map entry — the reload either removes it or names the step that failed in its report or its error; whichever scope read the file.

Enforced by one shared retirement that every file-reading scope calls once its loader
returns a disabled specialist config: the agent scope's own arm, the policies cascade's
per-role swap (which retires instead of constructing), the triggers scope (which retires
instead of registering, under the role's agent lock), and the agents sweep, which retires
the runtime agent of a role its own committed re-scan reports disabled — re-validated under
that role's lock, never a same-named resident — so the full and config-sync scopes inherit
through their composition. The retirement is a sequence of named best-effort steps (grant
purge, the open-conversation close of INV-CFG-013, bus unregister, scheduled-ask revoke,
route unwind), each failure a
`teardown_incomplete_<step>` row; the registry re-scan and the agent-registry rebuild that
follow raise a reload error whose kind names the step (`specialist_reload_failed`,
`agent_registry_rebuild_failed`), which the single-role scopes return as their error
envelope and the policies cascade reports as `failed:<role>:<kind>` beside the rows already
earned; a delegation-map refresh that fails is a `refresh_role_map_failed` row.

What it does not cover: a scope that reads no agent file (the plugin-environment scope); a role no reload has read since the flag changed — the flag is honoured
when a scope reads it, not when it is written; and the agent scope's own swap window, in
which a flip between its load and its re-scan installs an agent the registry calls disabled
until the next reload that reads the file or the registry. The personality maps refreshed
alongside the delegation map are not among the five states named.

**INV-CFG-013**: A specialist read as disabled — by the reload scope that retires it, by an `agents` sweep whether or not it has a runtime Agent, or at boot after the channels start and before background jobs resume — has a close attempted through the terminal funnel, as a named retirement step, for each of its open conversations; a close that fails leaves that conversation open, is told to the operator, is named in the reload's report (at boot, in the log) and is retried by the next `agents` sweep that still reads the specialist disabled; a specialist that failed to load has nothing closed.

A disable cannot be confirmed beforehand — no tool disables a specialist, and Casa notices
only when a reload or a boot reads the file — so its conversations are closed when it takes
effect: the retirement's `close_engagements` step finalizes each through the same funnel an
uninstall uses (outcome `cancelled`; its end is logged and the engager notified).
Enforced by that step in `reload.py::_teardown_disabled_specialist`, which every retiring
scope reaches; by the `agents` sweep also admitting a disabled role that has open
conversations but no runtime Agent — boot constructs none for a specialist, so before any
sweep backfilled one it is neither live nor residual — under the same disabled re-check and
the same role lock; and by `casa_core.py::_close_disabled_specialist_engagements`, the boot
pass for a specialist disabled while Casa was down, placed after the channels start (a
closure armed in this process is told live, INV-ENG-018) and before background jobs resume,
so a disabled specialist's job is never resumed — the pass hands the resume the
conversations it failed to close, and the resume skips them. A failed close is told to the
operator at once, because that conversation got no closing telling. Keyed on "read as disabled", never
on "absent from the registry": a load failure is not a disable and closes nothing. The step
awaits the funnel inline under the locks its caller holds; the funnel takes neither the
reload lock nor the plugin-tools lock.

## Failure behavior

**A specialist's reloaded config is disabled.** A `scope=agent` reload that reads a
specialist's `enabled: false` tears the role down rather than swapping it: that reload
purges the role's authorization grants, removes its live agent, unregisters its bus
consumer and cancels every dispatch the role itself has in flight — each waiting caller is
resolved with `handler error: cancelled: <id>` — revokes its scheduled asks, unwinds its
triggers and webhook routes, and rebuilds the agent registry without it; the arm itself
constructs nothing and registers nothing. The role's Casa-minted webhook secrets stay on
disk through this teardown, so a later reload that reads it enabled re-registers the same
credentials. This entry describes the `scope=agent` arm only.

**A specialist's `enabled: false` is read by another reload scope.** The same retirement,
secrets included, whichever scope read the file (INV-CFG-012). The `policies` cascade
retires the role instead of constructing a replacement from the disabled config; the
`triggers` scope retires it instead of registering its jobs and routes — and unwinds them
through the same route unwind a teardown uses, never through the registration path, so no
retirement hook sees the role; the `agents` sweep retires the runtime agent of a role whose
fresh re-scan reports it disabled, alongside its registry-eviction row; `config_sync` and
`full` inherit through the scopes they compose. Each reports
`teardown_disabled_specialist` (suffixed with the role in the multi-role scopes), and a
step that failed is named rather than absorbed: `teardown_incomplete_<step>` for a
teardown step, an error envelope whose kind names the step in the single-role scopes, a
`failed:<role>:<kind>` row in the cascade, `refresh_role_map_failed` for the map. A sweep
whose own re-scan failed reports `specialist_scan_failed` and retires nothing from the
stale generation; a sweep whose agent-registry rebuild failed returns
`agent_registry_rebuild_failed`, which `config_sync` reports as `agents:error:<kind>` rather
than swallowing. A retirement that left a step failed is remembered for the life of the
process, and the next sweep retries it — whether the role is still disabled on disk or its
directory has since gone — and reports its own outcome. A resident that shares a disabled
specialist's name is never touched: the sweep excludes it, and a single-role scope whose
role a resident came to own while it ran refuses with `role_conflict`.
The single-role scopes read and decide under the role's agent lock, so a disabled read and
a concurrent enabled swap of the same role serialize: the last file read wins, truthfully.

## Extension points

A new reload scope that reads agent files reaches the same retirement (INV-CFG-012); the rest of what a new scope needs is [`configuration.md`](configuration.md)'s.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/tools.py::close_disabled_specialist_engagements`
- `casa/rootfs/opt/casa/casa_core.py::_close_disabled_specialist_engagements`

**Tests**
- `tests/test_reload_disabled_specialist_scopes.py`
- `tests/test_pin_1095_removal_close.py`

**Related**
- [`architecture/configuration.md`](../architecture/configuration.md)
- [`architecture/engagement-finalization.md`](../architecture/engagement-finalization.md)
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
<!-- END SOURCEMAP -->
