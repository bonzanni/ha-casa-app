"""S8 plugin access profiles — requirements as recommendations.

A plugin's ``casa.requires`` rows say which OTHER plugin, and which of its
profiles, the plugin would like its targets to hold. Casa never enforces
them at run time and never writes on their account: the configurator is
TOLD (``requirement_candidates`` on add/assign, ``dependents`` on
unassign/remove, ``leftover_requirements`` on remove, a ``requirement_unmet``
health warning) and the operator decides. "Widest access wins" (operator
ruling 2026-10-02): a requirement only ever ADDS access, a target that
already holds the plugin full or with a covering profile is ``satisfied``
and nothing changes.

Everything here reads the REGISTRY DOCUMENT (``raw``) and the artifacts in
the store root — never a resolution, so it works on the mutation tools'
synchronous path before any reload. Nothing here raises into a tool: an
unreadable manifest is an empty one.
"""
from __future__ import annotations

import json
from pathlib import Path


REASON_UNMET = "requirement_unmet"

# The states a requirement can be in for one target. Only ``satisfied``
# means nothing is owed; none of the others blocks anything.
SATISFIED = "satisfied"
OFFER = "offer"                       # registered, assigned nowhere: one tap
REGISTERED_ELSEWHERE = "registered_elsewhere"  # one tap, sign-in shared
NOT_INSTALLED = "not_installed"
HELD_NARROWER = "held_narrower"       # a profile that does not cover; no auto-widening
PROFILE_MISSING = "profile_missing_in_plugin"  # the required name does not exist


def _entries(raw) -> list:
    return [e for e in ((raw or {}).get("plugins") or []) if isinstance(e, dict)]


def entry_named(raw, name: str):
    return next((e for e in _entries(raw) if e.get("name") == name), None)


def manifest_at(store_root, name: str, artifact_id) -> dict:
    """The stored artifact's ``plugin.json`` as a mapping, ``{}`` when it
    cannot be read. The LIVE manifest of a registered plugin is the one at
    its entry's current ``artifact_id``."""
    try:
        m = json.loads((Path(store_root) / str(name) / str(artifact_id)
                        / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return m if isinstance(m, dict) else {}


def _servers_at(store_root, name: str, artifact_id) -> list:
    from plugin_store import mcp_servers_map
    return sorted(mcp_servers_map(
        Path(store_root) / str(name) / str(artifact_id) / ".mcp.json"))


def profile_bare_tools(manifest: dict, profile: str):
    """The bare tool list ``profile`` declares in ``manifest``, or ``None``
    when the manifest declares no such profile."""
    casa = (manifest or {}).get("casa")
    profiles = casa.get("profiles") if isinstance(casa, dict) else None
    tools = profiles.get(profile) if isinstance(profiles, dict) else None
    if not isinstance(tools, list):
        return None
    return [t for t in tools if isinstance(t, str) and t]


def expanded_profile(store_root, entry: dict, profile: str):
    """The fully qualified tool set of *profile* on *entry*'s live artifact,
    or ``None`` when the live manifest declares no such profile."""
    from plugin_store import expand_tool_names
    manifest = manifest_at(store_root, entry.get("name"), entry.get("artifact_id"))
    bare = profile_bare_tools(manifest, profile)
    if bare is None:
        return None
    rname = entry.get("manifest_name") or entry.get("name") or ""
    servers = _servers_at(store_root, entry.get("name"), entry.get("artifact_id"))
    return {fq for b in bare for fq in expand_tool_names(rname, servers, b)}


def held_profile(entry: dict, target: str):
    profiles = entry.get("profiles")
    return profiles.get(target) if isinstance(profiles, dict) else None


def requires_of(store_root, entry: dict) -> list:
    """The validated ``casa.requires`` rows of *entry*'s live manifest; an
    unreadable or malformed declaration is no requirement at all."""
    from plugin_store import StoreError, manifest_requires
    try:
        return manifest_requires(
            manifest_at(store_root, entry.get("name"), entry.get("artifact_id")))
    except StoreError:
        return []


def requirement_state(raw, store_root, req: dict, target: str) -> str:
    """Where ONE requirement stands for ONE target — see the module docstring.
    An omitted requirement profile means FULL access and is satisfied only by
    an unprofiled assignment; a named profile must exist on the required
    plugin's live artifact before any containment is computed, and
    containment is computed over expanded live tool sets."""
    entry = entry_named(raw, req["plugin"])
    if entry is None:
        return NOT_INSTALLED
    targets = entry.get("targets") or []
    held = held_profile(entry, target) if target in targets else None
    # Full access satisfies every requirement — decided BEFORE the named
    # profile is resolved, so a requirement naming a profile the plugin does
    # not declare is still satisfied by a full holder (diff round 1).
    if target in targets and held is None:
        return SATISFIED
    need = None
    if req.get("profile") is not None:
        need = expanded_profile(store_root, entry, req["profile"])
        if need is None:
            return PROFILE_MISSING
    if target not in targets:
        return REGISTERED_ELSEWHERE if targets else OFFER
    if need is None:
        return HELD_NARROWER
    have = expanded_profile(store_root, entry, held) or set()
    return SATISFIED if need <= have else HELD_NARROWER


def requirement_candidates(raw, store_root, requires: list, targets) -> list:
    """One row per (requirement, target) for the configurator to act on."""
    return [{"plugin": req["plugin"], "profile": req.get("profile"),
             "why": req["why"], "target": target,
             "state": requirement_state(raw, store_root, req, target)}
            for req in requires for target in targets]


def dependents(raw, store_root, name: str, targets) -> list:
    """Other registered plugins whose requirements name *name* on one of
    *targets* — stated once on unassign/remove, never blocking."""
    rows = []
    affected = set(targets)
    for e in _entries(raw):
        if e.get("name") == name:
            continue
        for req in requires_of(store_root, e):
            if req["plugin"] != name:
                continue
            for t in e.get("targets") or []:
                if t in affected:
                    rows.append({"plugin": e.get("name"), "target": t,
                                 "profile": req.get("profile"), "why": req["why"]})
    return rows


def leftover_requirements(raw, store_root, entry: dict) -> list:
    """What a removed plugin's own requirements leave assigned on its former
    targets — mentioned on remove, never unassigned automatically."""
    rows = []
    for req in requires_of(store_root, entry):
        other = entry_named(raw, req["plugin"])
        if other is None:
            continue
        for t in entry.get("targets") or []:
            if t in (other.get("targets") or []):
                rows.append({"plugin": req["plugin"], "target": t,
                             "profile": held_profile(other, t)})
    return rows


def unmet_requirement_issues(raw, store_root) -> list:
    """The ``requirement_unmet`` WARNING rows for the health report: one per
    (plugin, requirement, target) that is not ``satisfied``. Recomputed on
    every regeneration, so a later assignment clears it on its own."""
    from plugin_registry import PluginIssue
    rows = []
    for e in _entries(raw):
        for req in requires_of(store_root, e):
            for t in e.get("targets") or []:
                state = requirement_state(raw, store_root, req, t)
                if state == SATISFIED:
                    continue
                rows.append(PluginIssue(
                    name=e.get("name"), target=t, stage="requires",
                    reason_code=REASON_UNMET, artifact_id=e.get("artifact_id"),
                    detail={"plugin": req["plugin"], "profile": req.get("profile"),
                            "why": req["why"], "state": state}))
    return rows
