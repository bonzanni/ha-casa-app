"""S8 plugin access profiles — the plan an options build enforces.

``profile_plan`` is computed INSIDE a builder from (a) the LOADED artifact —
its servers give the guarded namespaces and the expansion — and (b) the LIVE
registry entry and its live artifact's manifest — which give the profile
name and its bare tool list. Enforcement is deny-unless-allowed inside each
guarded namespace (``profile_guard_matcher``); the allow/deny lists only
decide visibility (``apply_profile_plan``). An empty plan touches nothing.
"""
from __future__ import annotations

import json

from plugin_registry import ResolutionResult, ResolvedPlugin, reload_snapshot, resolve_for
from plugin_fixtures import entry, mk_artifact, mk_registry

FQ_READ = "mcp__plugin_gmail_gmail__search_emails"
FQ_SEND = "mcp__plugin_gmail_gmail__send_email"
PREFIX = "mcp__plugin_gmail_gmail__"
SERVER = "mcp__plugin_gmail_gmail"


def _gmail_manifest(profiles=None):
    casa = {"provides_tools": [FQ_READ, FQ_SEND]}
    if profiles is not None:
        casa["profiles"] = profiles
    return {"casa": casa}


def _live(tmp_path, *, profiles=None, assignment=None, targets=("specialist:finance",)):
    """A live registry + store with one gmail artifact; returns the resolution
    for specialist:finance (the LOADED artifact == the live artifact)."""
    store = tmp_path / "store"
    e = entry("gmail", list(targets))
    if assignment:
        e["profiles"] = assignment
    mk_artifact(store, "gmail", e["artifact_id"], mcp_servers={"gmail": {}},
                extra_manifest=_gmail_manifest(profiles))
    reload_snapshot(registry_path=mk_registry(tmp_path, [e]), store_root=store)
    return resolve_for("specialist:finance")


def test_profiled_live_entry_yields_namespace_and_allowed_names(tmp_path):
    from plugin_grants import profile_plan
    res = _live(tmp_path, profiles={"read": ["search_emails"]},
                assignment={"specialist:finance": "read"})
    plan = profile_plan(res, target="specialist:finance")
    assert not plan.empty
    (p,) = plan.entries
    assert (p.name, p.profile) == ("gmail", "read")
    assert p.prefixes == (PREFIX,)
    assert p.allowed_names == frozenset({FQ_READ})
    assert p.declared_excluded == frozenset({FQ_SEND})


def test_unprofiled_live_entry_yields_an_empty_plan(tmp_path):
    from plugin_grants import profile_plan
    res = _live(tmp_path, profiles={"read": ["search_emails"]})
    plan = profile_plan(res, target="specialist:finance")
    assert plan.empty and plan.entries == ()


def test_profile_name_absent_from_live_manifest_denies_the_namespace(tmp_path):
    from plugin_grants import profile_plan
    res = _live(tmp_path, profiles={"other": ["send_email"]},
                assignment={"specialist:finance": "read"})
    (p,) = profile_plan(res, target="specialist:finance").entries
    assert p.prefixes == (PREFIX,) and p.allowed_names == frozenset()


def test_namespaces_come_from_the_loaded_artifact_not_the_live_one(tmp_path):
    """A recorded (older) artifact serves server ``old``; the live one serves
    ``gmail``. The guarded prefix and the expansion follow the LOADED one; the
    bare list follows the LIVE manifest."""
    from plugin_grants import profile_plan
    _live(tmp_path, profiles={"read": ["search_emails"]},
          assignment={"specialist:finance": "read"})
    old = tmp_path / "old"
    old_root = mk_artifact(old, "gmail", "ffff", mcp_servers={"old": {}},
                           extra_manifest=_gmail_manifest({"read": ["search_emails", "send_email"]}))
    recorded = ResolutionResult(registry_valid=True, plugins=[ResolvedPlugin(
        name="gmail", artifact_id="ffff", path=str(old_root), version="1",
        manifest=json.loads((old_root / ".claude-plugin" / "plugin.json").read_text()),
        profile=None)])
    (p,) = profile_plan(recorded, target="specialist:finance").entries
    assert p.prefixes == ("mcp__plugin_gmail_old__",)
    assert p.allowed_names == frozenset({"mcp__plugin_gmail_old__search_emails"})


def test_fallback_without_live_assignment_uses_the_prior_binding(tmp_path):
    """Not assigned live: a prior profile fails closed (namespace denied), a
    prior None stays full (today's §3.8 behaviour for recorded artifacts)."""
    from plugin_grants import profile_plan
    store = tmp_path / "store"
    other = entry("other", ["specialist:finance"])
    mk_artifact(store, "other", other["artifact_id"], mcp_servers={"other": {}})
    reload_snapshot(registry_path=mk_registry(tmp_path, [other]), store_root=store)
    art = mk_artifact(tmp_path / "rec", "gmail", "eeee", mcp_servers={"gmail": {}},
                      extra_manifest=_gmail_manifest({"read": ["search_emails"]}))
    manifest = json.loads((art / ".claude-plugin" / "plugin.json").read_text())

    def rec(profile):
        return ResolutionResult(registry_valid=True, plugins=[ResolvedPlugin(
            name="gmail", artifact_id="eeee", path=str(art), version="1",
            manifest=manifest, profile=profile)])

    (p,) = profile_plan(rec("read"), target="specialist:finance").entries
    assert p.allowed_names == frozenset() and p.prefixes == (PREFIX,)
    assert profile_plan(rec(None), target="specialist:finance").empty


def test_apply_profile_plan_strips_namespace_allows_and_adds_visibility_denies(tmp_path):
    from plugin_grants import apply_profile_plan, profile_plan
    res = _live(tmp_path, profiles={"read": ["search_emails"]},
                assignment={"specialist:finance": "read"})
    plan = profile_plan(res, target="specialist:finance")
    allowed = ["Read", SERVER, PREFIX + "*", FQ_SEND, "mcp__plugin_other_x", FQ_READ]
    disallowed = ["Bash"]
    out_allowed, out_disallowed = apply_profile_plan(allowed, disallowed, plan)
    assert out_allowed == ["Read", "mcp__plugin_other_x", FQ_READ]
    assert out_disallowed == ["Bash", FQ_SEND]
    assert allowed[1] == SERVER and disallowed == ["Bash"], "inputs are not mutated"


def test_apply_profile_plan_with_an_empty_plan_is_identity(tmp_path):
    from plugin_grants import apply_profile_plan, profile_plan
    res = _live(tmp_path)
    plan = profile_plan(res, target="specialist:finance")
    allowed, disallowed = ["Read", SERVER], ["Bash"]
    assert apply_profile_plan(allowed, disallowed, plan) == (allowed, disallowed)


async def test_profile_guard_matcher_denies_outside_and_permits_inside(tmp_path):
    from hooks import profile_guard_matcher
    from plugin_grants import profile_plan
    import re
    res = _live(tmp_path, profiles={"read": ["search_emails"]},
                assignment={"specialist:finance": "read"})
    matcher = profile_guard_matcher(profile_plan(res, target="specialist:finance"))
    assert re.fullmatch(matcher.matcher, FQ_SEND) and re.fullmatch(matcher.matcher, FQ_READ)
    assert not re.fullmatch(matcher.matcher, "Read")
    assert not re.fullmatch(matcher.matcher, "mcp__plugin_gmail_gmail")
    (hook,) = matcher.hooks
    denied = await hook({"tool_name": FQ_SEND, "tool_input": {}}, None, None)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "read" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    allowed = await hook({"tool_name": FQ_READ, "tool_input": {}}, None, None)
    assert allowed == {}
    undeclared = await hook({"tool_name": PREFIX + "never_declared", "tool_input": {}}, None, None)
    assert undeclared["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_profile_guard_matcher_is_none_for_an_empty_plan(tmp_path):
    from hooks import profile_guard_matcher
    from plugin_grants import profile_plan
    assert profile_guard_matcher(profile_plan(_live(tmp_path), target="specialist:finance")) is None
