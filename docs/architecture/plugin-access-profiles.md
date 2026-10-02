---
last_reviewed: 2026-10-02
---

# Plugin access profiles

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How an assignment can give an agent a SUBSET of a plugin's tools: the registry's `profiles`
map beside `targets`, the manifest's `casa.profiles` and `casa.requires` declarations, the
plan every options build computes and the PreToolUse barrier that enforces it, what the
engagement record keeps, and what the configurator is told about a plugin's
recommendations. It does not cover the grant merge itself or artifact identity
([`plugins.md`](plugins.md)), the mutation envelope's other fields
([`plugin-mutation-tools.md`](plugin-mutation-tools.md)), or per-call authorization of a
protected tool ([`plugin-authorization.md`](plugin-authorization.md)).

## Mental model

**A profile is a name, declared by the plugin, held by one assignment.** A plugin's manifest
declares `casa.profiles: {<name>: [<bare tool>, …]}` — bare tool names, validated at install
against the fully qualified `casa.provides_tools` list, which becomes required the moment a
profile is declared. The registry entry holds only the NAME, in a sibling map
`profiles: {"<target>": "<name>"}` beside `targets`. `targets` keeps its type and meaning,
which is why every consent identity computed over sorted targets is unchanged by a profile,
and the schema version stays 1: an absent key is full access, which is what every
assignment was before. `full` is reserved and never stored.

**Widest access wins.** This is the operator's ruling and it shapes every tool surface. A
profile is written only when `plugin_assign` CREATES a target; an assignment that already
exists is the no-op it always was, whatever `profile` says, and the result reports the
access the target already holds. There is no operation that narrows or changes an existing
assignment's profile — if that is ever wanted it is an unassign and a reassign, and it
applies to sessions built afterwards. A plugin's `casa.requires` rows only ever ADD access:
a target that holds the required plugin full, or with a profile whose expanded tool set
covers the requirement's, is `satisfied`, and nothing is written.

**Enforcement is deny-unless-allowed inside the loaded artifact's namespaces.** Every options
builder — the resident's, the specialist's (which also serves sync delegations,
specialist-hosted jobs and every resume), and the resident-hosted plugin worker's — computes
a plan at construction: for each profiled plugin, the namespaces `mcp__plugin_<p>_<s>__` of
every server the LOADED artifact declares, and the profile's bare tools expanded over those
same servers. A code-side PreToolUse matcher over those namespaces denies any call whose
exact name is not in that set, beside the settings guard and before any permission rule
runs; it holds whatever the allow and deny lists say and whichever way the CLI orders them.
The lists are only hygiene for visibility: the server grant is dropped, every allow inside a
guarded namespace that is not in the profile is stripped (a `runtime.yaml` server allow
included), the profile's names are allowed tool by tool, and the declared tools outside the
profile are denied so they leave the model's context where a deny beats an allow. An
unprofiled plugin produces no plan entry and its build is byte-identical to before.

**The plan is read live, at construction, and the record stores what was built.** The
profile NAME comes from the live registry entry for the build's own `tier:role`; the bare
list from that entry's live artifact manifest; the namespaces and the expansion from the
artifact actually loaded, which on a resume is the recorded one. Nothing is read from a
resolution captured before an await. When the live registry no longer assigns the plugin to
the target, the plan falls back to the build's prior binding — the recorded effective
profile on a resume, the captured one on a fresh launch — and a prior profile denies the
whole namespace rather than widening. Every builder hands the plan it applied back to its
caller, and the caller persists exactly that: an interactive launch writes the plan's
effective profile onto the record's `plugin_artifacts` rows after the build and before the
client starts, and a resume or fresh reopen does the same through the driver's persister
before the client opens — never from a second read a mutation could slip between. The
write is strict: a record that cannot say what its build enforces does not start — the
launch aborts with `profile_persist_failed`, a resume fails closed and is retried on the
next turn. Only the plugins the build LOADED are written; a plugin withheld for an
unresolved secret keeps the profile its last loading build enforced, so a withheld build
never widens the next one's fallback. An update that shrinks a profile's list applies to
sessions built after it, and its result names what the new list dropped: the old list
expanded over the live artifact's servers minus the new list expanded over the new
artifact's, so a server the update removes counts too.

**A running session keeps what it was built with.** A resident's pool client until the
reload reconstructs the role, an engagement client until it is next resumed or opened fresh,
a job batch until its next batch opens a session. No flag, no teardown and no quiesce is
added for a profile; the confirmations say "from its next session".

**Operator config denies are never removed.** A `tools.disallowed` entry naming a tool
inside the profile keeps denying it: config can only remove capability, and a plugin's
manifest must never widen an operator's explicit deny. The overlap is disclosed as
`profile_tools_denied_by_config` on the assignment result.

## Contracts & invariants

**INV-PLUG-043**: A session built after an assignment that names a profile permits exactly that profile's tools inside the plugin's loaded namespaces — in every options builder, resume included — while an assignment without a profile builds exactly as before.

Enforced by `profile_plan`, which every builder calls at construction, and by the guard
matcher it injects: a call under a guarded namespace is denied unless its exact name is in
the plan, so an undeclared tool, a renamed server's tool and a config-level allow are all
denied before permission rules are evaluated. Pinned over the three builders, the recorded
resume, a launch whose assignment is created during its topic await, and an unprofiled build
whose option fields are compared with today's.

What it does not cover: a session built BEFORE the assignment or before an update — by
ruling it keeps its tools until it is next built — and the CLI's own precedence between a
tool-level deny and a server-level allow, which the barrier is specified not to depend on.
No unit test exercises the CLI's permission evaluation; every count is over option shapes
and hook results.

**INV-PLUG-044**: Casa writes a profile only when an assignment is created, changes no existing assignment on a requirement's account, and refuses an update whose manifest drops a held profile name before the registry is touched.

Enforced in the assign core (an existing target is a no-op that reports held access; an
undeclared profile is refused before any write), the unassign core (the `profiles` key goes
in the same save as the target, keeping keys ⊆ targets), the update core (`profile_missing`
after the publish and tag guard, before system requirements and the repoint), and the
requirement helpers, which compute and report but never write.

What it does not cover: `profile_missing` guards only the NAME. A profile that stays declared
with a smaller list is applied to later builds and reported in the update result's
`profile_tools`; that is the plugin author's contract.

**The requirement states.** For one requirement and one target: `satisfied` (the target
holds the plugin full, or with a profile whose expanded live tool set contains the
requirement's); `offer` (registered, assigned nowhere); `registered_elsewhere` (one tap,
the sign-in is shared); `not_installed`; `held_narrower` (a profile that does not cover,
including any profile against a requirement that names none — an omitted requirement
profile means full, and only an unprofiled assignment satisfies it); and
`profile_missing_in_plugin` (the required name does not exist on the required plugin's live
artifact, so no containment is computed). Only `satisfied` owes nothing; none of the others
blocks anything.

## Failure behavior

**A malformed `profiles` map.** The entry is invalid — `bad_profiles`, the posture
`bad_targets` has — and the registry is not. An owned bundle entry carrying one is
`owned_invariant`: a bundle's own plugins are full by construction.

**A malformed `casa.profiles` or `casa.requires`.** The install or update is refused with
`profiles_invalid` or `requires_invalid`, strict like `casa.jobs`: a reserved or malformed
name, an empty, duplicated or non-bare tool entry, a missing `provides_tools`, a plugin with
no MCP server, or a bare name whose expansion `provides_tools` does not declare.

**A profile named by the registry but absent from the live manifest.** The plan has no
allowed names, so the namespace is fully denied. Update refuses to create this state; it can
only arise from a hand-edited registry or store.

**A live manifest that cannot be read at build time.** It reads as declaring no profiles,
which denies the namespace — never as full access.

**A requirement helper that fails.** A recommendation never fails the mutation: the envelope
field is empty and the failure is logged.

## Extension points

**Declaring a profile** is a manifest change plus the `provides_tools` entries it needs;
nothing in Casa lists profiles by name. **Recommending one** is a `casa.requires` row with a
`why` the configurator repeats. **A new state** for a requirement is added in one place, the
requirement helper, and rendered by the health phrase for `requirement_unmet`.

**Another options builder** must call `profile_plan` for its target, apply the list hygiene
and inject the guard matcher, and must not add a `ClaudeAgentOptions` construction site
without extending the cross-session sweep ([`mcp-and-tools.md`](mcp-and-tools.md),
INV-MCP-013).

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/plugin_requirements.py`
- `casa/rootfs/opt/casa/plugin_grants.py::profile_plan`
- `casa/rootfs/opt/casa/plugin_grants.py::apply_profile_plan`
- `casa/rootfs/opt/casa/hooks.py::profile_guard_matcher`
- `casa/rootfs/opt/casa/plugin_store.py::manifest_profiles`
- `casa/rootfs/opt/casa/plugin_store.py::manifest_requires`
- `casa/rootfs/opt/casa/plugin_store.py::expand_tool_names`
- `casa/rootfs/opt/casa/engagement_registry.py::EngagementRegistry.update_plugin_profiles`
- `casa/rootfs/opt/casa/drivers/in_casa_driver.py::InCasaDriver._persist_applied_profiles`
- `casa/rootfs/opt/casa/tools.py::_requirement_candidates_for`
- `casa/rootfs/opt/casa/tools.py::_dependents_for`
- `casa/rootfs/opt/casa/tools.py::_profile_tools_denied_by_config`

**Tests**
- `tests/test_plugin_profiles_registry.py`
- `tests/test_plugin_profiles_manifest.py`
- `tests/test_plugin_profiles_plan.py`
- `tests/test_plugin_profiles_builders.py`
- `tests/test_plugin_profiles_record.py`
- `tests/test_plugin_profiles_tools.py`

**Related**
- [`architecture/plugins.md`](../architecture/plugins.md)
- [`architecture/plugin-mutation-tools.md`](../architecture/plugin-mutation-tools.md)
- [`architecture/plugin-health.md`](../architecture/plugin-health.md)
- [`architecture/plugin-authorization.md`](../architecture/plugin-authorization.md)
- [`architecture/mcp-and-tools.md`](../architecture/mcp-and-tools.md)
- [`architecture/background-jobs.md`](../architecture/background-jobs.md)
<!-- END SOURCEMAP -->
