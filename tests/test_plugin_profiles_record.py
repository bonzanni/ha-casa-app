"""S8 plugin access profiles — the engagement record stores what was built.

``plugin_artifacts`` rows carry ``"profile"``: for a plugin the build loaded,
the EFFECTIVE profile it applied (the plan's ``effective_profiles()``) — a
profile captured before a launch await only as the plan's fallback for a
plugin no longer assigned live; a withheld plugin's row is covered by
``test_plugin_profiles_withheld.py`` (#1186). A narrow registry setter persists it, and a resume rebuild
re-applies the live plan onto the record before the client opens.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from engagement_registry import EngagementRegistry
from plugin_registry import reload_snapshot
from plugin_fixtures import entry, mk_artifact, mk_registry

FQ_READ = "mcp__plugin_gmail_gmail__search_emails"
FQ_SEND = "mcp__plugin_gmail_gmail__send_email"


async def _registry(tmp_path):
    reg = EngagementRegistry(tombstone_path=tmp_path / "tomb.json", bus=None)
    await reg.load()
    return reg


async def test_update_plugin_profiles_persists_the_effective_profile(tmp_path):
    reg = await _registry(tmp_path)
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="t", origin={}, topic_id=1,
        plugin_artifacts=({"name": "gmail", "artifact_id": "a", "path": "/p",
                           "manifest_name": "gmail", "profile": None},
                          {"name": "other", "artifact_id": "b", "path": "/q",
                           "manifest_name": "other", "profile": None}),
    )
    await reg.update_plugin_profiles(rec.id, {"gmail": "read"})
    assert [r["profile"] for r in rec.plugin_artifacts] == ["read", None]
    loaded = EngagementRegistry(tombstone_path=tmp_path / "tomb.json", bus=None)
    await loaded.load()
    assert [r["profile"] for r in loaded.get(rec.id).plugin_artifacts] == ["read", None]


async def test_update_plugin_profiles_on_an_unknown_record_is_a_noop(tmp_path):
    reg = await _registry(tmp_path)
    await reg.update_plugin_profiles("missing", {"gmail": "read"})


def test_resume_rebuild_reapplies_the_live_plan_onto_the_record(tmp_path, monkeypatch):
    """Recorded None + live ``read`` ⇒ the rebuilt options enforce ``read``
    and the record's row says so: the live profile wins over a recorded None."""
    import tools as tools_mod
    from config import HooksConfig
    store = tmp_path / "store"
    e = entry("gmail", ["specialist:finance"])
    e["profiles"] = {"specialist:finance": "read"}
    art = mk_artifact(store, "gmail", e["artifact_id"], mcp_servers={"gmail": {}},
                      extra_manifest={"casa": {"provides_tools": [FQ_READ, FQ_SEND],
                                               "profiles": {"read": ["search_emails"]}}})
    reload_snapshot(registry_path=mk_registry(tmp_path, [e]), store_root=store)
    cfg = SimpleNamespace(
        role="finance", model="claude-sonnet-4-6", system_prompt="You are Alex.",
        tools=SimpleNamespace(allowed=["Read"], disallowed=[],
                              permission_mode="acceptEdits", max_turns=10),
        mcp_server_names=[], hooks=HooksConfig(), cwd="")
    spec_reg = MagicMock()
    spec_reg.get = MagicMock(return_value=cfg)
    monkeypatch.setattr(tools_mod, "_specialist_registry", spec_reg, raising=False)
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    monkeypatch.setattr(tools_mod, "_agent_registry", None, raising=False)
    monkeypatch.setattr("hooks.resolve_hooks", lambda *a, **kw: {})
    engagement = SimpleNamespace(
        id="eng-1", kind="specialist", role_or_type="finance", origin={},
        plugin_artifacts=({"name": "gmail", "artifact_id": e["artifact_id"],
                           "path": str(art), "manifest_name": "gmail",
                           "profile": None},))
    opts = tools_mod.build_engagement_resume_options(engagement, "sess-1")
    assert FQ_READ in opts.allowed_tools and FQ_SEND in opts.disallowed_tools
    assert engagement.plugin_artifacts[0]["profile"] == "read"


async def _driver_persists_before_open(monkeypatch, tmp_path, method):
    from claude_agent_sdk import ClaudeAgentOptions
    from drivers.in_casa_driver import InCasaDriver
    from test_in_casa_driver import _mk_noop_factory
    import tools as tools_mod
    order = []

    class _FakeClient:
        def __init__(self, options):
            self.options = options
        async def __aenter__(self):
            order.append("open")
            return self
        async def __aexit__(self, *a):
            pass
        async def close(self):
            pass
    monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

    def _fake_rebuild(engagement, session_id):
        engagement.plugin_artifacts = tuple(
            {**row, "profile": "read"} for row in engagement.plugin_artifacts)
        return ClaudeAgentOptions(resume=session_id, disallowed_tools=["Agent", "Task"])
    monkeypatch.setattr(tools_mod, "build_engagement_resume_options", _fake_rebuild)

    async def persist(engagement_id, profiles):
        order.append(("persist", engagement_id, dict(profiles)))
    drv = InCasaDriver(topic_stream_factory=_mk_noop_factory(),
                       persist_plugin_profiles=persist)
    eng = SimpleNamespace(
        id="eng-1", kind="specialist", role_or_type="finance", origin={},
        sdk_session_id="sess-1", context_generation=0,
        plugin_artifacts=({"name": "gmail", "artifact_id": "a", "path": "/p",
                           "manifest_name": "gmail", "profile": None},))
    await getattr(drv, method)(eng, *(["sess-1"] if method == "resume" else []))
    assert order == [("persist", "eng-1", {"gmail": "read"}), "open"], order


async def test_resume_persists_the_applied_profile_before_opening(monkeypatch, tmp_path):
    await _driver_persists_before_open(monkeypatch, tmp_path, "resume")


async def test_open_fresh_persists_the_applied_profile_before_opening(monkeypatch, tmp_path):
    await _driver_persists_before_open(monkeypatch, tmp_path, "open_fresh")


def test_casa_core_wires_the_profile_persister_into_every_driver():
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "casa/rootfs/opt/casa/casa_core.py").read_text()
    tree = ast.parse(src)
    sites = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", "")) == "InCasaDriver"]
    assert len(sites) >= 1
    for call in sites:
        kws = {k.arg for k in call.keywords}
        assert "persist_plugin_profiles" in kws, kws


async def test_update_plugin_profiles_touches_only_the_named_rows(tmp_path):
    """Only rows named in the mapping change (diff round 2, Astra A): a plugin
    the caller does not name keeps its recorded profile. (A build names a
    withheld plugin only with a profile name — #1186.)"""
    reg = await _registry(tmp_path)
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="t", origin={}, topic_id=1,
        plugin_artifacts=({"name": "gmail", "artifact_id": "a", "path": "/p",
                           "manifest_name": "gmail", "profile": "read"},
                          {"name": "other", "artifact_id": "b", "path": "/q",
                           "manifest_name": "other", "profile": "x"}),
    )
    await reg.update_plugin_profiles(rec.id, {"other": None})
    assert [r["profile"] for r in rec.plugin_artifacts] == ["read", None]


def test_resume_with_a_withheld_plugin_keeps_its_recorded_profile(tmp_path, monkeypatch):
    """An artifact withheld for an unresolved secret is NOT loaded by this
    build and its recorded ``read`` is never erased; the live assignment
    names the same profile, so the row stays ``read`` (#1186 records the
    plan's profile name for a withheld plugin, never ``None``)."""
    import tools as tools_mod
    from config import HooksConfig
    store = tmp_path / "store"
    e = entry("gmail", ["specialist:finance"])
    e["profiles"] = {"specialist:finance": "read"}
    art = mk_artifact(store, "gmail", e["artifact_id"],
                      mcp_servers={"gmail": {"env": {"KEY": "${GMAIL_S8_SECRET}"}}},
                      extra_manifest={"casa": {"provides_tools": [FQ_READ, FQ_SEND],
                                               "profiles": {"read": ["search_emails"]}}})
    reload_snapshot(registry_path=mk_registry(tmp_path, [e]), store_root=store)
    monkeypatch.delenv("GMAIL_S8_SECRET", raising=False)
    cfg = SimpleNamespace(
        role="finance", model="claude-sonnet-4-6", system_prompt="You are Alex.",
        tools=SimpleNamespace(allowed=["Read"], disallowed=[],
                              permission_mode="acceptEdits", max_turns=10),
        mcp_server_names=[], hooks=HooksConfig(), cwd="")
    spec_reg = MagicMock()
    spec_reg.get = MagicMock(return_value=cfg)
    monkeypatch.setattr(tools_mod, "_specialist_registry", spec_reg, raising=False)
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    monkeypatch.setattr(tools_mod, "_agent_registry", None, raising=False)
    monkeypatch.setattr("hooks.resolve_hooks", lambda *a, **kw: {})
    engagement = SimpleNamespace(
        id="eng-1", kind="specialist", role_or_type="finance", origin={},
        plugin_artifacts=({"name": "gmail", "artifact_id": e["artifact_id"],
                           "path": str(art), "manifest_name": "gmail",
                           "profile": "read"},))
    opts = tools_mod.build_engagement_resume_options(engagement, "sess-1")
    assert opts.plugins == [], "the withheld plugin must not load"
    assert engagement.plugin_artifacts[0]["profile"] == "read"


async def _driver_refuses_open_when_persist_fails(monkeypatch, method):
    from claude_agent_sdk import ClaudeAgentOptions
    from drivers.in_casa_driver import InCasaDriver
    from test_in_casa_driver import _mk_noop_factory
    import tools as tools_mod
    import pytest
    order = []

    class _FakeClient:
        def __init__(self, options):
            pass
        async def __aenter__(self):
            order.append("open")
            return self
        async def __aexit__(self, *a):
            pass
        async def close(self):
            pass
    monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

    def _fake_rebuild(engagement, session_id):
        engagement.plugin_artifacts = tuple(
            {**row, "profile": "read"} for row in engagement.plugin_artifacts)
        return ClaudeAgentOptions(resume=session_id, disallowed_tools=["Agent", "Task"])
    monkeypatch.setattr(tools_mod, "build_engagement_resume_options", _fake_rebuild)

    async def persist(engagement_id, profiles):
        raise OSError(28, "No space left on device")
    drv = InCasaDriver(topic_stream_factory=_mk_noop_factory(),
                       persist_plugin_profiles=persist)
    eng = SimpleNamespace(
        id="eng-1", kind="specialist", role_or_type="finance", origin={},
        sdk_session_id="sess-1", context_generation=0,
        plugin_artifacts=({"name": "gmail", "artifact_id": "a", "path": "/p",
                           "manifest_name": "gmail", "profile": None},))
    with pytest.raises(OSError):
        await getattr(drv, method)(eng, *(["sess-1"] if method == "resume" else []))
    assert order == [], "no client may open when the profile write failed"
    assert not drv.is_alive(eng)


async def test_resume_refuses_to_open_when_the_profile_write_fails(monkeypatch):
    await _driver_refuses_open_when_persist_fails(monkeypatch, "resume")


async def test_open_fresh_refuses_to_open_when_the_profile_write_fails(monkeypatch):
    await _driver_refuses_open_when_persist_fails(monkeypatch, "open_fresh")


async def _real_writer_failure_refuses_open(monkeypatch, tmp_path, method):
    """The REAL registry writer failing (ENOSPC) must propagate through the
    real update_plugin_profiles and refuse the open — pinned against the
    writer itself, not a substitute persister (diff round 3, Astra A)."""
    import pytest
    from claude_agent_sdk import ClaudeAgentOptions
    from drivers.in_casa_driver import InCasaDriver
    from test_in_casa_driver import _mk_noop_factory
    import tools as tools_mod
    import engagement_registry as er
    reg = await _registry(tmp_path)
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="t", origin={}, topic_id=1,
        plugin_artifacts=({"name": "gmail", "artifact_id": "a", "path": "/p",
                           "manifest_name": "gmail", "profile": None},))
    rec.sdk_session_id = "sess-1"
    order = []

    class _FakeClient:
        def __init__(self, options):
            pass
        async def __aenter__(self):
            order.append("open")
            return self
        async def __aexit__(self, *a):
            pass
        async def close(self):
            pass
    monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _FakeClient)

    def _fake_rebuild(engagement, session_id):
        engagement.plugin_artifacts = tuple(
            {**row, "profile": "read"} for row in engagement.plugin_artifacts)
        return ClaudeAgentOptions(resume=session_id, disallowed_tools=["Agent", "Task"])
    monkeypatch.setattr(tools_mod, "build_engagement_resume_options", _fake_rebuild)

    def _enospc(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(er.EngagementRegistry, "_write_tombstone", _enospc)
    drv = InCasaDriver(topic_stream_factory=_mk_noop_factory(),
                       persist_plugin_profiles=reg.update_plugin_profiles)
    with pytest.raises(OSError):
        await getattr(drv, method)(rec, *(["sess-1"] if method == "resume" else []))
    assert order == [] and not drv.is_alive(rec)


async def test_resume_refuses_to_open_when_the_real_writer_fails(monkeypatch, tmp_path):
    await _real_writer_failure_refuses_open(monkeypatch, tmp_path, "resume")


async def test_open_fresh_refuses_to_open_when_the_real_writer_fails(monkeypatch, tmp_path):
    await _real_writer_failure_refuses_open(monkeypatch, tmp_path, "open_fresh")
