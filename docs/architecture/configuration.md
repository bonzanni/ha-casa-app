---
last_reviewed: 2026-09-02
---

# Configuration, reload and secrets

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

Where configuration comes from, what is version-controlled, what a running system can
pick up without restarting, and how secrets are resolved. The reconciliation of the
config tree against image defaults — the ownership rules, the per-entry merge and the
`${VAR}` placeholder semantics — is
[`architecture/config-reconciliation.md`](config-reconciliation.md). It does not cover
what any individual option means — the app manifest and its translations are the
authority on that.

## Mental model

**There are two configuration worlds and they behave completely differently.**

The first is the **app manifest**: options an operator sets in Home Assistant. These are read
by the supervisor at service start, exported into the environment, and consumed once during
startup. **Changing one requires a restart.** Nothing reloads them.

The second is the **config tree on disk** — agents, policies, bindings, specialists — which
is reconciled against image defaults at boot and can be reloaded in-process afterwards.

**Reload is scope-specific, and it is not a restart.** The registered scopes — triggers,
agent, agents, policies, plugin_env, executors, config_sync, and full (the set INV-CFG-001
pins) — each rebuild a defined slice: executors rebuilds the executor registry with a
resident cascade, and config_sync reruns reconciliation then cascades agents and policies.
There is no
scope that rereads manifest options, reconstructs channels, or re-reads arbitrary files. If
your change is an operator option, reload will not help you, and this is the single most
common wrong expectation in this area.

**A full reload is exclusive but not atomic.** It takes a writer lock that excludes every
other reload, then runs its steps in order — and there is no rollback across them. A failure
partway leaves earlier steps applied. The lock prevents interleaving, not partial
application. It also omits the on-disk reconciliation entirely, and omits plugin environment
unless explicitly asked.

**Three scopes reach plugin state, and they are locked from outside the dispatcher.** The
executors scope and the plugin-environment scope each regenerate the plugin health report,
and the full scope reaches both through its cascade; all three therefore take the
plugin-mutation lock (INV-TOOL-003). The dispatcher holds its own read/write lock across the
handler, so a handler that asked for the plugin lock from in there would be holding one lock
and requesting the other in the order opposite to every plugin mutation — which is a
deadlock with no timeout on either side, not a slow path. The two entry points therefore
acquire the plugin lock for exactly those three scopes *before* dispatching, off one shared
classification rather than one apiece, and the handlers' own acquisitions survive only as
same-task re-entries that serialize a direct, undispatched call. Every other scope stays
unfenced and concurrent, which is what lets a plugin mutation holding that lock finish its
own agent reload.

**A reload that re-derived plugin routing also re-derives the report describing it.** The
scopes that reconcile plugin triggers, callbacks and events after their handler — the
trigger scope, the two agent scopes and the full scope — regenerate the plugin health report
once, and the entry points own that too. It cannot live in the dispatcher for the reason
above, so it runs after the dispatch has returned and released the read/write lock — and
after the entry point's own fence, so that taking the plugin-mutation lock, writing and
releasing it is one unit that sees itself through even when the caller is cancelled. A
caller that goes away neither drops the refresh while queued for the lock nor abandons a
write already in flight for a newer one to lose to. There are three such entry points: the
reload tool, the soft trigger-reload tool and the internal route. Regenerating is all it
does — it announces nothing, and a regeneration that fails is logged rather than failing the
reload it followed. A full reload therefore regenerates more than once: the executors step
inside its cascade regenerates and announces, the plugin-environment step does the same when
the reload asked for it, and this one runs after all of them — so two regenerations and one
announcement ordinarily, three and two when plugin environment is included. Only the last
describes the routing the reload left behind; the earlier ones run before the reconcile that
decides it.

**The config tree is a git repository, but only a whitelist is tracked.** Agents, policies,
bindings, schema, and specific registry files are versioned; plugin stores, staging areas,
the environment file and general working state are not. The whitelist is the authority, and
it is duplicated in the boot script — both must agree.

**Some identity changes cannot be hot-swapped at all.** If a resident's identity changes, the
reload path returns a restart-required outcome *before* mutating live state rather than
attempting a swap — and before promoting the staged binding on disk: an already-live resident
is loaded with the validation-only reconcile, so `active.yaml` keeps naming what the resident
serves until the restart's boot reconcile promotes and activates the staged binding together.

**A specialist reload consumes the roles overlay after the lock that built it.** The
overlay rebuild runs under the personality materialize lock, but the agent load that
consumes it cannot hold that lock (resident loads re-acquire it internally), so a
concurrent install or upgrade can swap the active tuple in the gap. A load that fails in
that gap rebuilds the overlay once and retries before surfacing an error. The overlay
itself stays a shared, destructively rebuilt path — not a per-reload snapshot — so a
mutation committing after a successful load can still advance what the following registry
refresh reads; that state is internally consistent and converges, because reloads
serialize per scope and the mutating flow's own sequencer re-runs the refresh last.

**Secret indirection covers every password-typed option.** Exactly four options resolve an
external `op://` secret reference at startup — the Claude OAuth token, the Telegram bot
token, the webhook secret and the context7 API key. Resolved values are cached for the
process lifetime by the reference string; the plugin-environment reload scope invalidates
that cache first, so rotating a referenced field takes effect on reload for plugin
variables but requires a restart for the four startup options. Every successful reload
dispatch — not just this scope — kicks the plugin setup-episode worker, because both the
secrets landing (this scope) and any agent-reconstructing scope can make a setup episode
held under INV-PLUG-008 dispatchable. The webhook secret is also
resolved by the Supervisor discovery publisher, so the companion integration signs with
the same value the add-on verifies.

## Contracts & invariants

**INV-CFG-001**: Exactly eight reload scopes exist, and none of them rereads the app manifest options.

Maintained by the module-level registration calls in the reload module. The set is not
mechanically closed — the registry is a plain dict and accepts any scope string — so
"exactly eight" is the current, deliberate count of registrations, held by review and the
pinning test rather than by an enum.

What it does not cover, and it is the point of stating it: no scope reloads operator options,
global channel setup, process environment generally, or arbitrary files in the config tree.

**INV-CFG-002**: A full reload excludes every other dispatched reload for its duration.

Enforced by a reader/writer lock, with non-full scopes serialized per scope key.

What it does not cover: the sequence is not transactional. There is no rollback across its
steps, so a mid-sequence failure leaves earlier steps in effect. A handler called directly,
outside the dispatcher, takes no lock for *itself*; the cascading handlers (policies,
executors, config_sync) do take the lock of each scope or role they fan out into, in a
fixed one-directional order, so a cascade cannot interleave with a directly-dispatched
reload of the same role or scope.

**INV-CFG-003**: A live resident identity change is restart-only: an in-process reload neither hot-swaps the resident nor promotes its staged binding on disk before the restart that activates it.

Enforced by the identity guards in the agent, trigger and policies reload paths, checked
before any live runtime mutation, and by the shared loader helper those three paths use, which
loads an already-live resident with the validation-only reconcile — the staged candidate is
resolved, validated and compiled, so the guards see its digest, but `active.yaml` keeps naming
the served binding and `desired.yaml` the staged one until boot commits and activates them
together. Nothing on disk therefore names a persona a resident is not still serving, which is
what keeps the persona reference scan honest between a reset and its restart.

What it does not cover: the policies cascade skips such a resident quietly rather than
surfacing the same refusal, so the outcome depends on which scope you asked for. A staged
candidate that stops compiling after it was staged is diagnosed and discarded by the boot
reconcile, not by the reload that first observes it — the reload retains the live identity and
leaves the staged file in place. And a resident that is not yet live — its first load, or a
bulk-sweep add — commits its first binding in the same load that activates it; the guarantee is
about residents that are already serving.

**INV-CFG-004**: Only an explicit whitelist of the config tree is version-controlled.

Enforced by the ignore file the repository is initialised with, reconciled on every boot, and
mirrored by the boot script.

What it does not cover: the version-controlled set and the set the reconciler owns are
*different*. A path can be tracked without being reconciled, and vice versa.

**INV-CFG-011**: A reload scope whose handler takes the plugin-mutation lock acquires that lock at the entry point, before the reload read/write lock — never underneath it.

Enforced by the shared fence both reload entry points wrap their dispatch in, over the
scope set the handlers' own acquisitions define.

What it does not cover: it is an ordering rule, not an exclusion rule. Two fenced reloads
still serialize against each other only as far as the locks themselves say, and a fenced
scope now waits for an in-flight plugin mutation before it starts rather than partway
through — deliberately, since it had to wait for that lock either way. Nor does it close
the ordering by construction: nothing prevents a future caller from dispatching a fenced
scope from somewhere neither entry point covers, which is why the set and the callers are
pinned statically as well as behaviourally.

**INV-CFG-012**: A reload that reads a specialist's `enabled: false` constructs and registers nothing for it and retires none of its Casa-minted secret slots; and for each of — its runtime agent, its bus queue, its scheduled jobs and webhook routes, its agent-registry entry, its delegation-map entry — the reload either removes it or names the step that failed in its report or its error; whichever scope read the file.

Enforced by one shared retirement that every file-reading scope calls once its loader
returns a disabled specialist config: the agent scope's own arm, the policies cascade's
per-role swap (which retires instead of constructing), the triggers scope (which retires
instead of registering, under the role's agent lock), and the agents sweep, which retires
the runtime agent of a role its own committed re-scan reports disabled — re-validated under
that role's lock, never a same-named resident — so the full and config-sync scopes inherit
through their composition. The retirement is a sequence of named best-effort steps (grant
purge, bus unregister, scheduled-ask revoke, route unwind), each failure a
`teardown_incomplete_<step>` row; the registry re-scan and the agent-registry rebuild that
follow raise a reload error whose kind names the step (`specialist_reload_failed`,
`agent_registry_rebuild_failed`), which the single-role scopes return as their error
envelope and the policies cascade reports as `failed:<role>:<kind>` beside the rows already
earned; a delegation-map refresh that fails is a `refresh_role_map_failed` row.

What it does not cover: scopes that read no agent file (the plugin-environment and
executors scopes); a role no reload has read since the flag changed — the flag is honoured
when a scope reads it, not when it is written; and the agent scope's own swap window, in
which a flip between its load and its re-scan installs an agent the registry calls disabled
until the next reload that reads the file or the registry. The personality maps refreshed
alongside the delegation map are not among the five states named.

## Failure behavior

**The required credential option is missing.** Boot stops at validation — the earliest fatal
gate, and every service is gated behind it. It is not the only fatal configuration failure:
later in startup, malformed policies, a malformed agent configuration, and the absence of the
primary assistant role each raise and stop the process too. "Reconciliation is never
boot-fatal" (INV-CFG-005) is about the config *tree*, not about validation.

**Repository initialisation fails.** Degraded, not fatal. Versioning is unavailable;
everything else proceeds.

**A secret reference fails to resolve.** Absorbed, and what is left behind depends on who
reads the variable. One a *plugin* may reference is left unset — the plugin environment,
and the one plugin-facing option — because the CLI hands whatever it holds to that plugin's
MCP server, and only an unset variable takes a `${VAR:-default}`. The options Casa consumes
keep the raw reference, which fails loudly where absence would be silent; the webhook
secret is blanked rather than used as an HMAC key (a vault path is a predictable string),
and the discovery publisher withdraws any record it published.

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

**A reload handler raises.** The dispatcher returns an error envelope rather than propagating
— a failed reload is a reported outcome, not an exception at the caller. A `config_sync`
reload that changed a known resident's trigger file re-registers that resident's triggers
under its own `triggers` lock after the `agents` and `policies` cascades — a file the pass
could not read on either side counts as changed — and one resident's refusal is reported
in the envelope without aborting another's.

## Extension points

**A new option** means the manifest options block, its schema entry, the translations entry,
and an explicit export or read wherever it is consumed. Nothing picks up an option
automatically.

**Removing an option** leaves its stored value behind: the host warns about the unknown
key at boot until the stored options are cleaned by hand, and Casa itself ignores it.
Pre-1.0 that is accepted — there is no boot-time pruning of removed keys.

**Making an option hot-reloadable** is not a small change: it means a new scope and rebuilding
every consumer, because no generic mechanism exists.

**A new reload scope** needs its handler, a lock key, a decision about whether the full scope
composes it, whether it participates in trigger reconciliation, and what its failure means.
None of those are inferred.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/reload.py::dispatch`
- `casa/rootfs/opt/casa/reload.py::reload_full`
- `casa/rootfs/opt/casa/reload.py::_resident_identity_changed`
- `casa/rootfs/opt/casa/config_git.py::init_repo`
- `casa/rootfs/opt/casa/secrets_resolver.py::resolve`
- `casa/config.yaml::schema`
- `casa/rootfs/etc/s6-overlay/scripts/setup-configs.sh`
- `casa/rootfs/opt/casa/config.py::AgentConfig`

**Tests**
- `tests/test_casa_reload_tool.py`
- `tests/test_config_git.py`
- `tests/test_admin_reload_route.py`
- `tests/test_reload_live_resident_not_promoted.py`
- `tests/test_reload.py`
- `tests/test_reload_disabled_specialist_scopes.py`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/agent-taxonomy.md`](../architecture/agent-taxonomy.md)
- [`architecture/config-reconciliation.md`](../architecture/config-reconciliation.md)
<!-- END SOURCEMAP -->
