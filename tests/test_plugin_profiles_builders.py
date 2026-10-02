"""S8 plugin access profiles — every options builder enforces the plan.

A session BUILT after a profiled assignment permits exactly the profile's
tools: the resident builder, the specialist builder (sync delegation,
specialist-hosted job, resume) and the resident-hosted plugin-job builder all
inject the profile guard matcher beside the settings guard and apply the
list hygiene; an unprofiled build is byte-identical to today's. The
v0.335.2 cross-session denials and settings stay in place (INV-MCP-013).
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugin_registry import reload_snapshot, resolve_for
from plugin_fixtures import entry, mk_artifact, mk_registry

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

FQ_READ = "mcp__plugin_gmail_gmail__search_emails"
FQ_SEND = "mcp__plugin_gmail_gmail__send_email"
SERVER = "mcp__plugin_gmail_gmail"
CROSS = {"SendMessage", "ListAgents", "PushNotification"}


def _live(tmp_path, target, *, profiled=True):
    store = tmp_path / "store"
    e = entry("gmail", [target])
    if profiled:
        e["profiles"] = {target: "read"}
    art = mk_artifact(store, "gmail", e["artifact_id"], mcp_servers={"gmail": {}},
                      extra_manifest={"casa": {"provides_tools": [FQ_READ, FQ_SEND],
                                               "profiles": {"read": ["search_emails"]}}})
    reload_snapshot(registry_path=mk_registry(tmp_path, [e]), store_root=store)
    return art


PROFILE_PATTERN = re.escape("mcp__plugin_gmail_gmail__") + ".*"


def _profile_matchers(opts):
    """The profile guard matcher(s) — identified by their exact pattern, since
    the result-broker matchers also name plugin tools."""
    return [m for m in opts.hooks.get("PreToolUse", [])
            if getattr(m, "matcher", "") == PROFILE_PATTERN]


def _assert_profiled(opts):
    assert FQ_READ in opts.allowed_tools
    assert SERVER not in opts.allowed_tools and FQ_SEND not in opts.allowed_tools
    assert FQ_SEND in opts.disallowed_tools
    assert CROSS <= set(opts.disallowed_tools)
    assert json.loads(opts.settings).get("crossSessionInbound") == "refuse"
    (m,) = _profile_matchers(opts)
    assert re.fullmatch(m.matcher, FQ_SEND) and re.fullmatch(m.matcher, FQ_READ)


def _assert_unprofiled(opts):
    assert SERVER in opts.allowed_tools
    assert FQ_READ not in opts.allowed_tools and FQ_SEND not in opts.disallowed_tools
    assert _profile_matchers(opts) == []
    assert CROSS <= set(opts.disallowed_tools)


def _spec_cfg(role="finance"):
    from config import HooksConfig
    return SimpleNamespace(
        role=role, model="claude-sonnet-4-6", system_prompt="You are Alex.",
        tools=SimpleNamespace(allowed=["Read", SERVER], disallowed=["Bash"],
                              permission_mode="acceptEdits", max_turns=10),
        mcp_server_names=[], hooks=HooksConfig(), cwd="",
    )


@pytest.mark.parametrize("profiled", [True, False])
def test_specialist_builder_enforces_the_live_profile(tmp_path, monkeypatch, profiled):
    import tools as tools_mod
    _live(tmp_path, "specialist:finance", profiled=profiled)
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    monkeypatch.setattr(tools_mod, "_agent_registry", None, raising=False)
    opts = tools_mod._build_specialist_options(_spec_cfg())
    (_assert_profiled if profiled else _assert_unprofiled)(opts)
    assert "Bash" in opts.disallowed_tools and "Read" in opts.allowed_tools


def test_specialist_builder_strips_a_config_level_server_allow(tmp_path, monkeypatch):
    """runtime.yaml allowing the whole server is a config-level overlap the
    hygiene removes for a profiled plugin (round-1 Astra A1)."""
    import tools as tools_mod
    _live(tmp_path, "specialist:finance")
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    monkeypatch.setattr(tools_mod, "_agent_registry", None, raising=False)
    opts = tools_mod._build_specialist_options(_spec_cfg())
    assert SERVER not in opts.allowed_tools


def test_specialist_builder_keeps_an_operator_deny_on_a_profile_tool(tmp_path, monkeypatch):
    """An operator's config deny on a tool inside the profile is never removed
    (round-7 Astra A: config can only remove capability)."""
    import tools as tools_mod
    _live(tmp_path, "specialist:finance")
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    monkeypatch.setattr(tools_mod, "_agent_registry", None, raising=False)
    cfg = _spec_cfg()
    cfg.tools.disallowed = ["Bash", FQ_READ]
    opts = tools_mod._build_specialist_options(cfg)
    assert FQ_READ in opts.disallowed_tools


@pytest.mark.parametrize("profiled", [True, False])
async def test_resident_builder_enforces_the_live_profile(tmp_path, profiled):
    from agent import Agent
    from channels import ChannelManager
    from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
    from mcp_registry import McpServerRegistry
    from session_registry import SessionRegistry
    _live(tmp_path, "resident:butler", profiled=profiled)
    cfg = AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT, role="butler",
        model="claude-haiku-4-5", system_prompt="You are Tina.",
        character=CharacterConfig(name="Tina"),
        tools=ToolsConfig(allowed=["Read"], disallowed=[]),
        memory=MemoryConfig(token_budget=800, read_strategy="cached"),
    )
    memory = AsyncMock()
    memory.profile.return_value = ""
    memory.recall.return_value = ""
    resident = Agent(
        config=cfg, session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(), channel_manager=ChannelManager(),
        semantic_memory=memory,
    )
    opts = await resident._build_options(
        channel="telegram", channel_key="telegram-k", is_fresh=True,
        resume_sid=None, user_text="hello")
    (_assert_profiled if profiled else _assert_unprofiled)(opts)


def _job_rec():
    return SimpleNamespace(
        id="eng-1", role_or_type="assistant", kind="plugin",
        origin={"plugin_job": {"plugin": "gmail", "model": "sonnet"},
                "job": {"turns_per_batch": 5}},
    )


@pytest.mark.parametrize("profiled", [True, False])
def test_plugin_job_builder_enforces_the_live_profile(tmp_path, monkeypatch, profiled):
    import tools as tools_mod
    _live(tmp_path, "resident:assistant", profiled=profiled)
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    monkeypatch.setattr(tools_mod, "_channel_manager", None, raising=False)
    monkeypatch.setattr(tools_mod._PLUGIN_JOB_ROOT.__class__, "mkdir",
                        lambda self, *a, **k: None, raising=False)
    monkeypatch.setattr(tools_mod, "_PLUGIN_JOB_ROOT", tmp_path / "eng")
    res = resolve_for("resident:assistant")
    opts = tools_mod._build_plugin_job_options(_job_rec(), res)
    (_assert_profiled if profiled else _assert_unprofiled)(opts)
    assert {"Agent", "Task", "AskUserQuestion"} <= set(opts.disallowed_tools)
    if not profiled:
        assert opts.allowed_tools[:3] == ["Skill", "ToolSearch", SERVER]


def test_recorded_artifact_row_restores_the_effective_profile(tmp_path):
    import tools as tools_mod
    art = mk_artifact(tmp_path / "store", "gmail", "abcd", mcp_servers={"gmail": {}})
    row = {"name": "gmail", "artifact_id": "abcd", "path": str(art),
           "manifest_name": "gmail", "profile": "read"}
    res = tools_mod._resolution_from_recorded([row])
    assert res.plugins[0].profile == "read"
    old = dict(row)
    del old["profile"]
    assert tools_mod._resolution_from_recorded([old]).plugins[0].profile is None


def test_plan_effective_profiles_is_what_a_record_persists(tmp_path):
    from plugin_grants import profile_plan
    _live(tmp_path, "specialist:finance")
    plan = profile_plan(resolve_for("specialist:finance"), target="specialist:finance")
    assert plan.effective_profiles() == {"gmail": "read"}


def test_builders_report_the_plan_they_applied(tmp_path, monkeypatch):
    """The plan a builder enforced is what its caller persists — handed back
    through `plan_out`, never re-read after the build (diff round 1, Astra A)."""
    import tools as tools_mod
    _live(tmp_path, "specialist:finance")
    monkeypatch.setattr(tools_mod, "_mcp_registry", None, raising=False)
    monkeypatch.setattr(tools_mod, "_agent_registry", None, raising=False)
    sink = []
    tools_mod._build_specialist_options(_spec_cfg(), plan_out=sink)
    assert [p.effective_profiles() for p in sink] == [{"gmail": "read"}]
    _live(tmp_path / "job", "resident:assistant")
    monkeypatch.setattr(tools_mod, "_channel_manager", None, raising=False)
    monkeypatch.setattr(tools_mod, "_PLUGIN_JOB_ROOT", tmp_path / "eng")
    sink = []
    tools_mod._build_plugin_job_options(_job_rec(), resolve_for("resident:assistant"), plan_out=sink)
    assert [p.effective_profiles() for p in sink] == [{"gmail": "read"}]
