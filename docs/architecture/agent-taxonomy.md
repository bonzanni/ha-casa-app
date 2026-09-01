---
last_reviewed: 2026-08-07
---

# Agent taxonomy and the registry

> Code is the source of truth. This file is a map; when it and the code disagree, the code wins.

## Scope

How an agent is declared, validated and looked up: the tiers, the artifacts a tier requires,
and the registry that answers "who is this?" at runtime. It does not cover what happens
inside a turn once an agent is chosen, nor how personas dress an agent up.

## Mental model

An agent is a **directory of configuration**, not a class: the loader checks a per-tier file
set rather than importing anything. That decision explains most of the shape here.

**The role registry models residents and specialists only** — its tier type admits exactly
those two, and executors load on their own isolated path. Every registry claim below is
scoped to that boundary, and reasoning about executors through it is the likeliest mistake
in this area.

Two names exist for a registered agent and they are not interchangeable. The **role** is the
stable identifier the system routes on. The **name** is what a person sees. Code resolves by
role and translates to a name at the edge, so routing survives a rename — but nothing
enforces that names are unique, and reverse lookup from a name is first-wins, so
name-keyed behaviour is not guaranteed to survive one. Routing is what a rename is safe
for; not everything is routing.

Failure is **not uniform across tiers**, and that is the second thing to internalise.
Residents are gating: a bad one stops boot. Specialists and executors load on isolated paths
whose failures are deliberately boot-non-fatal — one broken specialist does not take the
system down, and the code says so at the loader's walk. A document claiming "a validation
failure is a boot failure" would be wrong for two of the three tiers.

The registry is the read side: a small, already-validated index answering what tier a role
belongs to, what it is called, and whether it exists at all.

**A rescan is never observable half-done, and the tier index a delegation reads is never a
boot fossil.** Both follow from the same rule and each got there the hard way. The specialist
scan runs on a worker thread while the loop stays free to reload another role, and reloads
serialize only per role — so a concurrent role-map sync snapshots the specialist configs
mid-scan. Clearing the map and refilling it made that snapshot the *delegation authority*
missing specialists that are perfectly healthy on disk: refused as unknown, and dropped from
the prompt block rendered from the same map. The scan therefore builds into local containers
and publishes by rebinding, so every snapshot is a whole generation. Separately, the tier
index the delegation session build reads is re-adopted from the runtime on every role-map
sync rather than captured once at boot — a role added after boot was absent from the boot
index, and the fallback then resolved a *resident's* plugin assignment against
`specialist:<role>`, launching the delegation without the plugins that role is assigned.

Residual, stated rather than implied away: the three specialist stores (enabled configs,
disabled names, load failures) are published by three rebinds, so a reader consulting two of
them across the publish can see two whole generations. What it can no longer see is half of
one, which was the defect.

## Contracts & invariants

**INV-AGENT-001**: A role claimed by both a resident and a specialist is refused at boot, when the role registry is built.

Enforced in `_build_role_registry`, which raises naming the duplicated role. That is the only
place a collision is *refused*.

What it does not cover: reload. The reload paths rebuild through `AgentRegistry.build` and
the delegation role map directly, and both *tolerate* a collision rather than raise — a
reload must not brick on something boot would have rejected. They agree on the outcome:
`AgentRegistry.build` registers residents first and skips any specialist whose role is
already taken, and `tools.sync_agent_role_map` does the same, each logging
`role %r exists in both tiers — resident entry wins`. So a collision introduced after boot
degrades consistently, with the resident kept everywhere — but it degrades silently, visible
only as those two warnings.

**INV-AGENT-002**: For residents and specialists, `_check_file_set` refuses a missing required file, a forbidden file, or an unrecognised one. The executor path implements its own weaker check and does not refuse unrecognised files.

All three directions apply only on the first path, and that asymmetry is easy to miss
because both paths talk about required and forbidden files. `_check_file_set` has a single
caller; executors are loaded elsewhere and are checked only for missing-required and
present-forbidden. An unexpected file in an executor directory is not an error.

Where the strict check does run, directories, dotfiles and recognised editor backups are
skipped, so a stray save does not become a half-parsed agent.

**INV-AGENT-003**: Specialist and executor loading is isolated per agent and boot-non-fatal; resident loading is not.

Stated as the asymmetry it is. `validate_config_repo` additionally skips the
pipeline-managed specialists subtree, so it is not a whole-repository gate either. The
specialist replay it does run is validation-only (`binding_commit=False`): a binding the
loader would re-derive at boot is re-derived in memory and nothing is written.

What it does not cover: the isolation catches the loader's typed error plus plain
`ValueError` and `OSError` — the set the load path actually raises on malformed input
(role materialisation raises `ValueError` subclasses, for instance). An exception outside
that set escaping a specialist's load is not confined to that specialist and is boot-fatal
after all. "Non-fatal" is a property of failures the loader converts, not of the tier.

Two qualifiers, both of which invert the naive reading:

The loader's own docstring says collection-level errors still raise — but the registries
above it catch those, so nothing reaches boot. Reading the loader alone tells you the
opposite of what happens. Follow the call up before concluding a raise is fatal.

The resident path fails closed, and it does so in more than one place. An absent agents
directory makes the loader's own walk return empty before its fixed-slot check runs — but
that is one early return, not a clean boot: startup separately refuses to continue without
a primary assistant role. An empty-but-present directory fails the fixed-slot check
directly. There is no arrangement in which a system with no residents comes up.

That distinction is worth stating because reading the loader alone suggests otherwise, and
a correction based on the loader alone was published here and was wrong. Follow the call
chain past the function that returns before concluding what boot does.

**INV-AGENT-004**: The registry performs no filesystem access; it is an index built from already-loaded configuration.

Nothing resolves a role by touching disk at request time.

## Failure behavior

**A required file is missing, forbidden, or unrecognised.** `_check_file_set` raises naming
the role and the files. For a resident this stops boot; for a specialist or executor the
isolated path absorbs it and the rest of the system continues — noting that the executor
check never refuses an unrecognised file in the first place (INV-AGENT-002), so for
executors only missing-required and forbidden files reach this failure at all.

**An agent is disabled.** `enabled: false` makes a specialist or executor valid but
inactive — excluded from normal lookup and new launches, not failed and not unknown. A
disabled *executor's* definition additionally stays available to recovery paths, so an
engagement that already exists can resume after a restart: disabling is not a termination
control for in-flight work.

**A resident and a specialist claim one role.** Registry construction raises, naming the
role. The check is cross-tier because each directory looks fine on its own.

**A role is unknown at runtime.** The registry does not raise: `name_to_role` returns `None`,
`is_known` returns `False`, and `role_to_name` returns the role unchanged. Callers decide
what that means — read the call site rather than assuming it errors.

## Extension points

"A new agent" is not one operation, and the tier decides how much freedom you have.

**Residents are a fixed set.** The slots are enumerated in code and the loader refuses a
resident set that is not exactly those slots — so there is no such thing as adding a fourth
resident without changing that enumeration and everything that assumes it. A new resident is
a design change, not a directory.

**A new specialist** is closer to the simple story: a directory under its tier, the file set
that tier requires, and a role no resident or specialist already claims.

**A new executor** needs the file set, but is outside the cross-tier uniqueness check, since
that check spans residents and specialists only. Do not look to the role registry to tell
you whether an executor's role is free; it does not model executors at all.

A new tier is larger still: the file set, the loading path and the registry's tier type all
move together, and the type currently admits two tiers.

A new required artifact means updating the tier's file set and every agent of that tier in
the same change. What a missing file costs depends on the tier — boot for a resident, that
one agent for a specialist or executor.

**An optional artifact still has to be on the tier's list**, even one no shipped agent
carries. The file set is an allowlist, and the loader refuses a directory containing anything
absent from it — so a file an *agent* creates at runtime, rather than one the image ships,
fails the whole resident the moment it first exists. **Nothing creates a file in a role
directory today**: reminders are entries an agent adds to the role's existing `triggers.yaml`,
marked as agent-owned, rather than a file of their own (`architecture/reminders.md`). Agents do
create files elsewhere — an executor's workspace gets a generated `CLAUDE.md` and settings —
but a workspace is not a role directory and this allowlist does not reach it. That is the safer
shape precisely because of this allowlist — a new runtime-created filename is a boot failure
for the whole resident until it is added here, while a new *entry* in an already-listed file
is not.

## Source & test map

<!-- BEGIN SOURCEMAP -->
<!-- generated by scripts/verify_docs.py --write-nav; do not hand-edit -->

**Source**
- `casa/rootfs/opt/casa/agent_registry.py::AgentRegistry`
- `casa/rootfs/opt/casa/agent_registry.py::KnownAgent`
- `casa/rootfs/opt/casa/agent_registry.py::AgentRegistry.tier_for_role`
- `casa/rootfs/opt/casa/agent_loader.py::validate_config_repo`
- `casa/rootfs/opt/casa/agent_loader.py::LoadError`
- `casa/rootfs/opt/casa/agent_loader.py::_check_file_set`
- `casa/rootfs/opt/casa/casa_core.py::_build_role_registry`

**Tests**
- `tests/test_agent_registry.py::test_role_to_name_basic`
- `tests/test_agent_registry.py::test_name_to_role_unknown_returns_none`
- `tests/test_agent_loader.py::test_duplicate_role_across_residents_and_specialists_fails`

**Related**
- [`architecture/overview.md`](../architecture/overview.md)
- [`architecture/turn-loop.md`](../architecture/turn-loop.md)
<!-- END SOURCEMAP -->
