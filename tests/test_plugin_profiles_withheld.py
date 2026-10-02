"""#1186 — a plugin a build WITHHOLDS records the profile its plan gives it.

A plugin captured on an engagement's record but withheld by a build (its
secret unresolved at that moment) was left without a recorded profile. If it
was later unassigned and loaded on a resume, the fallback read ``None`` —
full access, no guard — although every session built after the profiled
assignment must permit only the profile's tools (INV-PLUG-043). A build now
records, for each recorded plugin it withholds, the profile name its plan
gives it: the live assignment's for the build's target, or, when no live
assignment remains, the prior binding's (the captured profile on a launch,
the recorded one on a resume). An unprofiled live assignment or a prior of
none records nothing; loaded plugins are recorded as before.
"""
from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from engagement_registry import EngagementRegistry
from plugin_registry import reload_snapshot
from plugin_fixtures import entry, mk_artifact, mk_registry

FQ_READ = "mcp__plugin_gmail_gmail__search_emails"
FQ_SEND = "mcp__plugin_gmail_gmail__send_email"
SERVER = "mcp__plugin_gmail_gmail"
PROFILE_PATTERN = re.escape("mcp__plugin_gmail_gmail__") + ".*"
VAR = "GMAIL_1186_SECRET"
TARGET = "specialist:finance"


def _publish(tmp_path, *, targets=(TARGET,), profiles=None):
    """Publish a live registry holding the one gmail artifact. The artifact's
    MCP server needs ``${VAR}``, so ``withhold_env_unresolved`` withholds it
    whenever VAR is unset. Returns the artifact path and its id."""
    store = tmp_path / "store"
    e = entry("gmail", list(targets))
    if profiles:
        e["profiles"] = profiles
    art = store / "gmail" / e["artifact_id"]
    if not art.exists():
        mk_artifact(store, "gmail", e["artifact_id"],
                    mcp_servers={"gmail": {"env": {"KEY": "${" + VAR + "}"}}},
                    extra_manifest={"casa": {"provides_tools": [FQ_READ, FQ_SEND],
                                             "profiles": {"read": ["search_emails"]}}})
    reload_snapshot(registry_path=mk_registry(tmp_path, [e]), store_root=store)
    return art, e["artifact_id"]


def _cfg():
    from config import HooksConfig
    return SimpleNamespace(
        role="finance", model="claude-sonnet-4-6", system_prompt="You are Alex.",
        tools=SimpleNamespace(allowed=["Read"], disallowed=[],
                              permission_mode="acceptEdits", max_turns=10),
        mcp_server_names=[], hooks=HooksConfig(), cwd="")


def _bind_specialist(monkeypatch):
    import tools as tools_mod
    spec_reg = MagicMock()
    spec_reg.get = MagicMock(return_value=_cfg())
    monkeypatch.setattr(tools_mod, "_specialist_registry", spec_reg, raising=False)
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    monkeypatch.setattr(tools_mod, "_agent_registry", None, raising=False)
    monkeypatch.setattr("hooks.resolve_hooks", lambda *a, **kw: {})


async def _assert_guarded_fallback(opts):
    """The build loads gmail from the record with a recorded profile and no
    live assignment: S8's fallback (a prior profile set ⇒ no allowed names),
    so nothing in gmail's namespace is allowed and the guard denies every
    call in it — never more than the ``read`` profile permits."""
    assert len(opts.plugins) == 1, opts.plugins
    gmail = sorted(t for t in opts.allowed_tools if t.startswith(SERVER))
    assert gmail == [], gmail
    guards = [m for m in opts.hooks.get("PreToolUse", [])
              if getattr(m, "matcher", "") == PROFILE_PATTERN]
    assert len(guards) == 1, guards
    (hook,) = guards[0].hooks
    for name in (FQ_SEND, FQ_READ):
        denied = await hook({"tool_name": name, "tool_input": {}}, None, None)
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny", name


def _engagement(art, artifact_id, profile):
    return SimpleNamespace(
        id="eng-1186", kind="specialist", role_or_type="finance", origin={},
        plugin_artifacts=({"name": "gmail", "artifact_id": artifact_id,
                           "path": str(art), "manifest_name": "gmail",
                           "profile": profile},))


@pytest.mark.asyncio
@pytest.mark.parametrize("unassigned_before_build", [False, True])
async def test_launch_withheld_profiled_plugin_stays_profiled_after_unassign(
        tmp_path, monkeypatch, unassigned_before_build):
    """The issue's sequence on the real launch path: assigned with ``read``;
    the launch captures gmail (secret set) but the builder withholds it (the
    secret goes away between the two filters); the record says ``read``.
    Then unassigned, secret back, resume: the guarded fallback, not full.
    With ``unassigned_before_build`` the unassign lands between the capture
    and the build (design round 1, Astra S1): the build has no live
    assignment, so it records the captured profile instead."""
    import agent as agent_mod
    import tools as tools_mod
    from config import (AgentConfig, CharacterConfig, DelegateEntry, MemoryConfig,
                        SessionConfig, ToolsConfig)
    try:
        from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
    except ImportError:
        from role_artifact_stub import STUB_ROLE_ARTIFACT

    art, artifact_id = _publish(tmp_path, profiles={TARGET: "read"})
    monkeypatch.setenv(VAR, "set")

    caller = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role="assistant")
    caller.delegates = [DelegateEntry(agent="finance", purpose="p", when="w")]
    alex = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role="finance")
    alex.character = CharacterConfig(name="Alex", archetype="finance", card="",
                                     prompt="You are Alex.")
    alex.enabled = True
    alex.model = "sonnet"
    alex.tools = ToolsConfig(allowed=["Read"], disallowed=[],
                             permission_mode="acceptEdits", max_turns=20)
    alex.mcp_server_names = []
    alex.memory = MemoryConfig(token_budget=0)
    alex.session = SessionConfig(strategy="ephemeral", idle_timeout=0)
    alex.channels = []
    alex.system_prompt = "You are Alex."

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    tch = MagicMock()
    tch.engagement_permission_ok = True
    tch.engagement_supergroup_id = -1001
    tch.open_engagement_topic = AsyncMock(return_value=1186)
    tch.send_to_topic = AsyncMock()
    cm = MagicMock(); cm.get.return_value = tch
    specialist_reg = MagicMock(); specialist_reg.get.return_value = alex
    bus = MagicMock(); bus.notify = AsyncMock()
    tools_mod.init_tools(
        channel_manager=cm, bus=bus, specialist_registry=specialist_reg,
        mcp_registry=None, trigger_registry=MagicMock(), engagement_registry=reg,
        agent_role_map={"assistant": caller})
    driver = MagicMock(); driver.start = AsyncMock()
    monkeypatch.setattr(agent_mod, "active_engagement_driver", driver)

    real_build = tools_mod._build_specialist_options
    built = []

    def build_after_the_secret_went(*a, **kw):
        os.environ.pop(VAR, None)
        if unassigned_before_build:
            _publish(tmp_path, targets=())
        opts = real_build(*a, **kw)
        built.append(opts)
        return opts

    monkeypatch.setattr(tools_mod, "_build_specialist_options", build_after_the_secret_went)
    token = agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram", "chat_id": "c1", "cid": "x",
        "user_text": "hi", "scope": "business"})
    try:
        res = await tools_mod.delegate_to_agent.handler({
            "agent": "finance", "task": "Plan Q2", "context": "",
            "mode": "interactive"})
    finally:
        agent_mod.origin_var.reset(token)
    assert json.loads(res["content"][0]["text"])["status"] == "pending", res
    assert len(built) == 1 and built[0].plugins == [], "the builder withheld gmail"
    rec = reg.by_topic_id(1186)
    assert [(r["name"], r.get("profile")) for r in rec.plugin_artifacts] == [("gmail", "read")]
    reloaded = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    await reloaded.load()
    assert reloaded.get(rec.id).plugin_artifacts[0]["profile"] == "read"

    # Unassigned from the target; the secret resolves again; the engagement resumes.
    monkeypatch.setattr(tools_mod, "_build_specialist_options", real_build)
    _publish(tmp_path, targets=())
    monkeypatch.setenv(VAR, "set")
    _bind_specialist(monkeypatch)
    opts = tools_mod.build_engagement_resume_options(reloaded.get(rec.id), "sess-1")
    await _assert_guarded_fallback(opts)


@pytest.mark.asyncio
async def test_resume_withheld_under_a_profiled_reassignment_stays_profiled(tmp_path, monkeypatch):
    """The resume variant: loaded unprofiled (row None); unassigned and
    reassigned with ``read``; a resume withholds gmail ⇒ the row says
    ``read``; unassigned again; secret back; resume: the guarded fallback, not full."""
    import tools as tools_mod
    _bind_specialist(monkeypatch)
    art, artifact_id = _publish(tmp_path, profiles={TARGET: "read"})
    engagement = _engagement(art, artifact_id, None)
    monkeypatch.delenv(VAR, raising=False)
    opts = tools_mod.build_engagement_resume_options(engagement, "sess-1")
    assert opts.plugins == [], "the withheld plugin must not load"
    assert engagement.plugin_artifacts[0]["profile"] == "read"

    _publish(tmp_path, targets=())
    monkeypatch.setenv(VAR, "set")
    opts = tools_mod.build_engagement_resume_options(engagement, "sess-2")
    await _assert_guarded_fallback(opts)


@pytest.mark.parametrize("live", ["unprofiled", "unassigned"])
@pytest.mark.parametrize("recorded", [None, "read"])
def test_withheld_without_a_live_profile_keeps_the_recorded_row(tmp_path, monkeypatch, live, recorded):
    """An unprofiled live assignment leaves a withheld plugin's row as it
    was; with no live assignment the plan names the prior binding, which on
    a resume IS the recorded row (``None`` names nothing) — so the row is
    unchanged either way: never widened, unprofiled builds unchanged."""
    import tools as tools_mod
    _bind_specialist(monkeypatch)
    if live == "unprofiled":
        art, artifact_id = _publish(tmp_path)
    else:
        art, artifact_id = _publish(tmp_path, targets=())
    engagement = _engagement(art, artifact_id, recorded)
    monkeypatch.delenv(VAR, raising=False)
    opts = tools_mod.build_engagement_resume_options(engagement, "sess-1")
    assert opts.plugins == []
    assert engagement.plugin_artifacts[0]["profile"] == recorded


def test_profile_plan_records_withheld_profiles_without_guarding_them(tmp_path):
    """The plan's entries, ``loaded`` and guard are unchanged by withheld
    plugins: only ``effective_profiles()`` names them, each with its live
    profile — EVERY withheld plugin, beside the loaded ones (diff round 2,
    Astra S2: a build withholding several plugins)."""
    from hooks import profile_guard_matcher
    from plugin_grants import profile_plan
    from plugin_registry import ResolutionResult, resolve_for
    store = tmp_path / "store"
    entries = []
    for name in ("gmail", "mail2", "mail3"):
        e = entry(name, [TARGET])
        e["profiles"] = {TARGET: "read"}
        mk_artifact(store, name, e["artifact_id"], mcp_servers={name: {}},
                    extra_manifest={"casa": {
                        "provides_tools": [f"mcp__plugin_{name}_{name}__search_emails"],
                        "profiles": {"read": ["search_emails"]}}})
        entries.append(e)
    led = entry("ledger", [TARGET])
    mk_artifact(store, "ledger", led["artifact_id"], mcp_servers={"ledger": {}})
    reload_snapshot(registry_path=mk_registry(tmp_path, [*entries, led]), store_root=store)
    by_name = {rp.name: rp for rp in resolve_for(TARGET).plugins}
    withheld = [by_name["gmail"], by_name["mail2"], by_name["mail3"]]

    empty = ResolutionResult(registry_valid=True, plugins=[], issues=[])
    plan = profile_plan(empty, target=TARGET, withheld=withheld)
    assert plan.entries == () and plan.loaded == () and plan.empty
    assert profile_guard_matcher(plan) is None
    assert plan.effective_profiles() == {"gmail": "read", "mail2": "read", "mail3": "read"}

    mixed = ResolutionResult(registry_valid=True, plugins=[by_name["ledger"]], issues=[])
    plan = profile_plan(mixed, target=TARGET, withheld=withheld)
    assert plan.entries == () and plan.loaded == ("ledger",)
    assert profile_guard_matcher(plan) is None
    assert plan.effective_profiles() == {
        "gmail": "read", "mail2": "read", "mail3": "read", "ledger": None}


@pytest.mark.asyncio
async def test_withheld_profile_survives_a_build_that_loads_another_plugin(tmp_path, monkeypatch):
    """A mixed build (diff rounds 1 and 2, Astra S2): gmail, mail2 and
    ledger recorded unprofiled; gmail and mail2 are reassigned with ``read``
    and their secret goes; a resume through the real driver loads ledger and
    withholds BOTH, and the persisted record says ``read`` for each withheld
    plugin, ``None`` for ledger. After a restart, both unassigned and the
    secret back, the next resume loads all three and guards each withheld
    plugin with the fallback while ledger stays full."""
    import tools as tools_mod
    from drivers.in_casa_driver import InCasaDriver
    from test_in_casa_driver import _mk_noop_factory

    store = tmp_path / "store"

    def publish(targets, profiles=None):
        withheld = []
        for name in ("gmail", "mail2"):
            e = entry(name, list(targets))
            if profiles:
                e["profiles"] = profiles
            if not (store / name / e["artifact_id"]).exists():
                mk_artifact(store, name, e["artifact_id"],
                            mcp_servers={name: {"env": {"KEY": "${" + VAR + "}"}}},
                            extra_manifest={"casa": {
                                "provides_tools": [f"mcp__plugin_{name}_{name}__search_emails",
                                                   f"mcp__plugin_{name}_{name}__send_email"],
                                "profiles": {"read": ["search_emails"]}}})
            withheld.append(e)
        led = entry("ledger", [TARGET])
        if not (store / "ledger" / led["artifact_id"]).exists():
            mk_artifact(store, "ledger", led["artifact_id"], mcp_servers={"ledger": {}})
        reload_snapshot(registry_path=mk_registry(tmp_path, [*withheld, led]),
                        store_root=store)
        return (*withheld, led)

    g, m2, led = publish([TARGET], {TARGET: "read"})
    _bind_specialist(monkeypatch)

    class _FakeClient:
        instances: list = []

        def __init__(self, options):
            self.options = options
            _FakeClient.instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def close(self):
            pass

    monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)
    tomb = tmp_path / "e.json"
    reg = EngagementRegistry(tombstone_path=str(tomb), bus=None)
    await reg.load()
    rows = tuple(
        {"name": e["name"], "artifact_id": e["artifact_id"],
         "path": str(store / e["name"] / e["artifact_id"]),
         "manifest_name": e["name"], "profile": None} for e in (g, m2, led))
    rec = await reg.create(kind="specialist", role_or_type="finance", driver="in_casa",
                           task="t", origin={}, topic_id=1, plugin_artifacts=rows)

    monkeypatch.delenv(VAR, raising=False)
    drv = InCasaDriver(topic_stream_factory=_mk_noop_factory(),
                       persist_plugin_profiles=reg.update_plugin_profiles)
    await drv.resume(rec, "sess-1")
    (first,) = _FakeClient.instances
    assert [p["path"] for p in first.options.plugins] == [rows[2]["path"]]
    await drv.cancel(rec)

    reloaded = EngagementRegistry(tombstone_path=str(tomb), bus=None)
    await reloaded.load()
    rec2 = reloaded.get(rec.id)
    assert [(r["name"], r["profile"]) for r in rec2.plugin_artifacts] == [
        ("gmail", "read"), ("mail2", "read"), ("ledger", None)]

    publish([])
    monkeypatch.setenv(VAR, "set")
    opts = tools_mod.build_engagement_resume_options(rec2, "sess-2")
    assert len(opts.plugins) == 3, opts.plugins
    assert "mcp__plugin_ledger_ledger" in opts.allowed_tools
    # One profile guard covers every guarded namespace (hooks.profile_guard_matcher
    # joins the prefixes); it must cover both withheld plugins and not ledger.
    pattern = "|".join(re.escape(f"mcp__plugin_{n}_{n}__") + ".*" for n in ("gmail", "mail2"))
    guards = [m for m in opts.hooks.get("PreToolUse", [])
              if getattr(m, "matcher", "") == pattern]
    assert len(guards) == 1, [getattr(m, "matcher", "") for m in opts.hooks["PreToolUse"]]
    (hook,) = guards[0].hooks
    assert not re.fullmatch(pattern, "mcp__plugin_ledger_ledger__anything")
    denied_calls = 0
    for name in ("gmail", "mail2"):
        prefix = f"mcp__plugin_{name}_{name}"
        assert sorted(t for t in opts.allowed_tools if t.startswith(prefix)) == [], name
        for tool in ("send_email", "search_emails"):
            assert re.fullmatch(pattern, f"{prefix}__{tool}")
            denied = await hook({"tool_name": f"{prefix}__{tool}", "tool_input": {}},
                                None, None)
            assert denied["hookSpecificOutput"]["permissionDecision"] == "deny", (name, tool)
            denied_calls += 1
    assert denied_calls == 4


# Each withheld plugin's assignment state, independently: (assigned live,
# live profile, prior binding) and the row the plan must record for it.
_STATES = {
    "live-profiled":   (True, "live", "prior", "live"),
    "live-unprofiled": (True, None, "prior", None),
    "gone-prior":      (False, None, "prior", "prior"),
    "gone-none":       (False, None, None, None),
}


@pytest.mark.parametrize("target", [TARGET, "resident:butler"])
def test_every_withheld_plugin_is_decided_on_its_own_state(tmp_path, target):
    """Generalises the multiplicity cases of diff rounds 1–3 (Astra S2 ×3):
    three withheld plugins beside a loaded one, every combination of the four
    states in every position (64 builds). Each plugin's profile names are its
    own (``live<i>``/``prior<i>``), so a plan that reads one plugin's
    assignment or prior for another, skips one, or decides by position
    records a wrong row. A live profile wins over a different prior (diff
    round 4, Astra S2); an unprofiled live assignment, or no assignment with
    no prior, records nothing. Run for a specialist and a resident target
    (diff round 4, Terra S2), so the plan reads the build's own target."""
    import dataclasses
    import itertools
    from plugin_grants import profile_plan
    from plugin_registry import ResolutionResult, resolve_for

    store = tmp_path / "store"
    names = ("pa", "pb", "pc")
    base = {n: entry(n, [target]) for n in (*names, "ledger")}
    for n, e in base.items():
        mk_artifact(store, n, e["artifact_id"], mcp_servers={n: {}})
    reload_snapshot(registry_path=mk_registry(tmp_path, list(base.values())),
                    store_root=store)
    resolved = {rp.name: rp for rp in resolve_for(target).plugins}
    loaded = ResolutionResult(registry_valid=True, plugins=[resolved["ledger"]], issues=[])

    builds = 0
    for combo in itertools.product(_STATES, repeat=len(names)):
        entries, withheld, expected = [base["ledger"]], [], {"ledger": None}
        for i, (n, state) in enumerate(zip(names, combo)):
            assigned, live, prior, want = _STATES[state]
            e = entry(n, [target] if assigned else [])
            if live:
                e["profiles"] = {target: f"{live}{i}"}
            entries.append(e)
            withheld.append(dataclasses.replace(
                resolved[n], profile=f"{prior}{i}" if prior else None))
            if want:
                expected[n] = f"{want}{i}"
        reload_snapshot(registry_path=mk_registry(tmp_path, entries), store_root=store)
        plan = profile_plan(loaded, target=target, withheld=withheld)
        assert plan.effective_profiles() == expected, combo
        assert plan.entries == () and plan.loaded == ("ledger",), combo
        builds += 1
    assert builds == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["specialist", "resident"])
@pytest.mark.parametrize("companion", [None, "unprofiled", "profiled"])
async def test_withheld_profile_through_the_real_builder_in_every_build_context(
        tmp_path, monkeypatch, tier, companion):
    """The withheld rule end to end through ``build_engagement_resume_options``
    in every build context the specialist builder serves (diff round 5, Astra
    and Terra S2: a mutant conditional on the build's tier, or on a loaded
    plugin being profiled, survived): a specialist or a delegated-resident
    target, with no companion plugin, a loaded unprofiled one or a loaded
    profiled one. gmail is withheld under a live ``read`` and recorded
    ``read`` beside the companion's own row; after an unassign and the secret
    back, gmail gets the guarded fallback while the companion is unchanged."""
    import tools as tools_mod
    target = f"{tier}:finance"
    _bind_specialist(monkeypatch)
    agent_reg = MagicMock()
    agent_reg.tier_for_role = MagicMock(return_value=tier)
    monkeypatch.setattr(tools_mod, "_agent_registry", agent_reg, raising=False)

    store = tmp_path / "store"
    led_read = "mcp__plugin_ledger_ledger__classify"

    def publish(gmail_assigned):
        g = entry("gmail", [target] if gmail_assigned else [])
        if gmail_assigned:
            g["profiles"] = {target: "read"}
        entries = [g]
        if not (store / "gmail" / g["artifact_id"]).exists():
            mk_artifact(store, "gmail", g["artifact_id"],
                        mcp_servers={"gmail": {"env": {"KEY": "${" + VAR + "}"}}},
                        extra_manifest={"casa": {"provides_tools": [FQ_READ, FQ_SEND],
                                                 "profiles": {"read": ["search_emails"]}}})
        if companion:
            led = entry("ledger", [target])
            if companion == "profiled":
                led["profiles"] = {target: "lr"}
            if not (store / "ledger" / led["artifact_id"]).exists():
                mk_artifact(store, "ledger", led["artifact_id"], mcp_servers={"ledger": {}},
                            extra_manifest={"casa": {
                                "provides_tools": [led_read, "mcp__plugin_ledger_ledger__export"],
                                "profiles": {"lr": ["classify"]}}})
            entries.append(led)
        reload_snapshot(registry_path=mk_registry(tmp_path, entries), store_root=store)
        return entries

    entries = publish(True)
    engagement = SimpleNamespace(
        id="eng-ctx", kind="specialist", role_or_type="finance", origin={},
        plugin_artifacts=tuple(
            {"name": e["name"], "artifact_id": e["artifact_id"],
             "path": str(store / e["name"] / e["artifact_id"]),
             "manifest_name": e["name"], "profile": None} for e in entries))
    companion_row = [("ledger", "lr" if companion == "profiled" else None)] if companion else []

    monkeypatch.delenv(VAR, raising=False)
    opts = tools_mod.build_engagement_resume_options(engagement, "sess-1")
    assert len(opts.plugins) == (1 if companion else 0), opts.plugins
    assert [(r["name"], r["profile"]) for r in engagement.plugin_artifacts] == [
        ("gmail", "read"), *companion_row]

    publish(False)
    monkeypatch.setenv(VAR, "set")
    opts = tools_mod.build_engagement_resume_options(engagement, "sess-2")
    assert len(opts.plugins) == (2 if companion else 1), opts.plugins
    assert sorted(t for t in opts.allowed_tools if t.startswith(SERVER)) == []
    assert [(r["name"], r["profile"]) for r in engagement.plugin_artifacts] == [
        ("gmail", "read"), *companion_row]
    # The profile guard is the one matcher naming gmail's namespace among the
    # joined profiled prefixes (hooks.profile_guard_matcher).
    guards = [m for m in opts.hooks.get("PreToolUse", [])
              if PROFILE_PATTERN in (getattr(m, "matcher", "") or "").split("|")]
    assert len(guards) == 1, [getattr(m, "matcher", "") for m in opts.hooks["PreToolUse"]]
    (hook,) = guards[0].hooks
    for name in (FQ_SEND, FQ_READ):
        denied = await hook({"tool_name": name, "tool_input": {}}, None, None)
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny", name
    if companion == "profiled":
        assert led_read in opts.allowed_tools
        assert await hook({"tool_name": led_read, "tool_input": {}}, None, None) == {}
    elif companion == "unprofiled":
        assert "mcp__plugin_ledger_ledger" in opts.allowed_tools
