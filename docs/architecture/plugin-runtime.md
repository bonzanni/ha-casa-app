---
last_reviewed: 2026-08-08
---

# Plugin runtime attachment

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

The environment a validly installed plugin's MCP servers need before an agent can use it,
and the two side channels a plugin reaches the rest of Casa through. WHO runs the setup
tool that provisions that environment, and what must hold before it runs, is its own
subject: [`plugin-setup.md`](plugin-setup.md). Installation, artifact identity and
per-call authorization are [`plugins.md`](plugins.md).

## Mental model

**A valid install is not a usable plugin.** Two things stand between them, and they fail in
opposite directions. An MCP server launched with an unresolved `${VAR}` does not fail — it
runs, on the literal string, and reports success against placeholder credentials. So an
unresolved reference *withholds* the plugin (INV-PLUG-008). But a plugin whose setup tool
exists to *create* those credentials would then be withheld for exactly the variables its
tool would produce, so one manifest declaration converts the withhold into a loud
not-ready state instead (INV-PLUG-009).

Two more attachment paths are easy to miss. **Plugin environment values live in a
mode-0600 conf file** re-sourced into the process only by the plugin-env reload scope —
deleting an entry from the file changes nothing until that reload runs. **Plugin media
flows through an outbox** — the shared directory (operator-relocatable by environment
variable) for an ordinary engagement, or a private per-uid directory for a uid-dropped
one, since that producer can no longer write the shared, non-group-writable tree — with
atomic claim semantics, size and type gates, and periodic orphan reaping; consumption is
destructive by design.

## Contracts & invariants

**INV-PLUG-008**: A plugin whose parseable `.mcp.json` references an environment variable that is unresolved in the effective environment, and that the plugin's manifest does not declare as setup-provisioned or optional, is withheld from resident and specialist session builds, and its automatic setup episode does not dispatch until those secrets resolve and the executing agent can load it.

References are collected from every string value of each declared server's *launch
fields* — `command`, `args`, `url`, `headers` and `env`, the positions the CLI expands
`${VAR}` in; tolerated unknown extension fields are not scanned — and unresolved means
absent, empty, or still an `op://` reference (a failed secret resolution falls back to
the raw value). The two manifest declarations that carve out of it are INV-PLUG-009 below.
Enforced at three points: both session builders — the resident/specialist
Agent's resolution and the delegated-specialist options builder — filter the resolved
plugin set before anything derives from it, so SDK plugins, grants, the protected map and
(for residents) the recorded binding all reflect what the session actually loads; the
setup-episode worker holds a settled episode until the secrets resolve, because a
trigger-consent round can settle while the installing engagement is still wiring them; and
for a *resident* execution target the worker additionally holds until that agent's next
session build will carry the episode's exact artifact — a binding published while the
plugin was withheld keeps excluding it until an agent reload, and a dispatch into that
session would consume the one automatic setup against a session without the tool. A
specialist execution target needs no such hold: specialists build their options fresh per
delegation against the current environment. Every successful reload — plugin-env landing
the secrets, or any agent-reconstructing scope — kicks the episode worker.

What it does not cover: the executor path, whose options builder hands out plugin paths
without this gate (the same asymmetry as INV-PLUG-006 — a configurator or plugin-developer
executor must be able to work on a plugin whose secrets are not wired yet). A *malformed*
declaration yields no requirements and passes this gate deliberately: the shared parser
gives the CLI nothing to spawn a server from, so no placeholder-credential path exists,
and malformed-ness is reported on the verification surface instead. The withhold decision
is evaluated when an agent publishes its binding snapshot and refreshed by the reload
seams, not continuously — and the check is admission control, not a fence: an environment
mutation between an agent's check and its MCP process spawn can still produce a stale
server, and a credential *rotation* leaves a warm session's already-spawned MCP process
on the old value even though the binding passes the gate. Both heal through the reload
seams, and neither is something verification can fully see: a plain-value rotation shows
as reload-pending only until the plugin-env reload lands, and an unchanged `op://`
reference cannot be compared at all — a warm session on a rotated credential reports
ready. An interactive specialist engagement records the plugin set *admitted when the
delegation was validated* (one filter feeding the requires gate where declared, the
record, and the launch), and every later build — including resume — re-applies
current-environment admission control; an environment change after that admission
point — even one that RESOLVES a variable moments later — is not re-admitted into this
engagement, and a change during the engagement can still make a build differ from the
record. Wiring a
secret mid-engagement does not make the plugin appear on resume when it was withheld at
creation — a new engagement picks it up.

**INV-PLUG-009**: An environment variable a plugin's manifest declares in `casa.setupProvides` does not withhold that plugin, and while it is unresolved the session build passes it to the CLI as an explicit empty string rather than letting the reference expand to a literal placeholder.

Without this, INV-PLUG-008 is a deadlock for an entire class of plugin: one whose setup
tool exists to *create* its credentials — forging a private key into the vault, registering
an application and learning its id — can never run that tool, because the plugin is
withheld for exactly the variables the tool would produce, and nothing re-kicks it (the
gate retries on plugin-env and agent reloads, neither of which can supply a value only
setup makes). The specialist that requires such a plugin is gated off with it.

`casa.setupProvides` is the ONLY such declaration, and its value is **readiness**, not the
withhold exemption. It says *my setup tool provisions this*: the plugin loads so setup can
run, but still verifies **not ready** with reason `setup_env_unprovisioned` until the value
actually lands, so a setup run that never happened stays loud rather than passing as
configured on empty credentials. Declaring it without a `casa.setupTool` is refused —
there would be nothing to be unprovisioned by.

A merely *optional* variable needs no declaration at all, and Casa deliberately offers none
(#431): `${VAR:-}` is documented Claude Code syntax, the CLI substitutes the default, and
the requirement extractor does not match that form — so it neither withholds nor leaks a
placeholder, with no manifest field and no reserved name. It is also strictly more
expressive, since a default may be a real value rather than only empty. What a default
cannot express is readiness, which is precisely why `setupProvides` survives as the one
declaration. Both are read
strictly on both artifact-verification paths (install-time validation and resolution-time
verdict), because a declaration that relaxes a gate must never be guessed at; a malformed
one excludes the artifact from resolution, and the runtime readers fail closed to "no
declaration", which leaves the plugin withheld exactly as before.

A declared name must live in a **reserved declaration namespace**, `CASA_PLUGIN_<NAME>`.
The binding is process-wide for the session's CLI subprocess rather than scoped to the
declaring plugin, so declaring a name is the difference between "absent" and "empty" for
everything in that session — including Casa's own reads, the CLI's knobs, and every other
attached plugin. A deny-list of what a plugin may *not* declare cannot be finished over an
open namespace, so the rule is inverted: everything outside the reserved prefix is
excluded by construction. Only *declared* names are fenced — a plugin may still reference
any `${VAR}` in `.mcp.json`, and an undeclared one withholds the plugin exactly as before,
binding nothing. This is a distinct rule from the reserved-key check on a server's own
`env` block, which is about shadowing a value the CLI injects per plugin.

The pinning is driven by the **declaration**, not by the `.mcp.json` reference set: a
server that reads its provisioned credential from the inherited environment rather than
naming it in its launch config still gets a binding. Without that it would see whatever
the environment happens to hold — including a leftover unresolvable `op://` reference,
which an idempotent setup tool can easily read as "already provisioned" and skip the
creation over.

It covers every path that attaches a plugin: the three in-process options builders
(resident/specialist Agent, delegated specialist, executor — including its by-path resume
branch) and the engagement run script, which hands recorded artifacts to a *supervised*
CLI via `--plugin-dir` and so sits outside the option builders entirely. The run script's
overlay is derived inside the renderer from the plugin directories being attached rather
than assembled by each caller — the driver's start path and boot reconciliation both
render that same service pair, and a per-caller contract is how one of them gets
forgotten.

What it does not cover: the exemption is not phase-scoped. An exempt plugin loads in
*ordinary* sessions too, on empty credentials, not only in the session that runs its
setup tool — Casa has no per-session plugin phase, and a resident's session is long-lived.
The compensating control is visibility, not exclusion: the unprovisioned row and the
health issue it generates persist until setup lands the value. An empty string is also
not the same as an unset variable to every server implementation; the contract Casa
offers is "never a literal `${VAR}`", and a plugin that declares these fields owns
failing clearly on an empty credential.

## Failure behavior

**A required environment variable is unresolved.** The plugin is withheld from resident and
specialist session builds — excluded from the SDK plugin list, its server grants, and the
recorded binding, and surfaced as an `env_unresolved` resolution issue — and any pending
setup obligation holds. Wiring the value and running the plugin-env reload makes the plugin
loadable; the agents that should carry it still need their own reload to rebuild sessions.

**A `casa.setupProvides` variable is unresolved.** The plugin loads anyway, with the
variable passed to the CLI as an explicit empty string rather than a literal placeholder,
and verification reports not ready with reason `setup_env_unprovisioned` until the value
lands. A setup run that never happened stays loud rather than passing as configured.

## Extension points

**Declaring that setup provisions a variable** means listing it in `casa.setupProvides`.
The name is then fenced for the whole session, so the declaration namespace is reserved;
declaring it without a `casa.setupTool` is refused, because the field means "my setup tool
provisions these". For a genuinely optional variable use `${VAR:-}` in `.mcp.json` instead
and let the CLI's own default expansion cover it.


## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/plugin_store.py::manifest_setup_provides`
- `casa/rootfs/opt/casa/plugin_env_conf.py`
- `casa/rootfs/opt/casa/plugin_outbox.py`

**Tests**
- `tests/test_plugin_store_setup_env.py`
- `tests/test_plugin_env_conf.py`
- `tests/test_plugin_outbox.py`

**Related**
- [`architecture/plugin-setup.md`](../architecture/plugin-setup.md)
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/configuration.md`](../architecture/configuration.md)
<!-- END SOURCEMAP -->
