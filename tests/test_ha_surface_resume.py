"""#1091: a Home Assistant tool rename reaches a live butler conversation.

Home Assistant 2026.9 renamed every MCP tool to ``<domain>__<name>``. Two things
kept a live butler on the vanished names:

- the facade rediscovered its tool surface only after a TRANSPORT exception, and
  HA answers a vanished name with an ``isError`` "Tool not found" RESULT;
- a republished surface never reached a session that was being resumed, because
  the resume identity (the #1029 prompt-surface digest) did not describe the HA
  tool surface. The resumed conversation kept calling the old names from its own
  history, and the CLI refused them.

These tests run the real facade, the real registry publication
(``casa_core.wire_tina_ha_facade``), a real butler ``Agent``'s armed surface and
the real resume gate. Only the HA transport is faked.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from mcp.types import CallToolResult, TextContent

import agent as agent_mod
from agent import _resume_decision

try:
    from tests.test_ha_mcp_facade import (
        FakeHaSession, SessionSequence, action_tool, invoke_sdk_tool,
        live_context_tool, make_facade, text_result,
    )
except ImportError:
    from test_ha_mcp_facade import (
        FakeHaSession, SessionSequence, action_tool, invoke_sdk_tool,
        live_context_tool, make_facade, text_result,
    )

pytestmark = [pytest.mark.unit]

MISSING_TOOL_TEXT = 'Error calling tool: Tool "HassTurnOn" not found'
BARE = ("HassTurnOn", "GetLiveContext")
PREFIXED = ("homeassistant__HassTurnOn", "homeassistant__GetLiveContext")


def _error_result(text: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)], isError=True,
    )


def _prefixed_live_context():
    tool = live_context_tool()
    return tool.model_copy(update={"name": "homeassistant__GetLiveContext"})


def _bare_session(results=None) -> FakeHaSession:
    return FakeHaSession(
        tools=[action_tool("HassTurnOn"), live_context_tool()],
        results=results,
    )


def _prefixed_session() -> FakeHaSession:
    return FakeHaSession(
        tools=[action_tool("homeassistant__HassTurnOn"), _prefixed_live_context()],
    )


def _butler(tmp_path, mcp_registry):
    from agent import Agent
    from channels import ChannelManager
    from config import (
        AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig,
    )
    from session_registry import SessionRegistry

    try:
        from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
        from tests.session_reg_helpers import (
            RESIDENT_DIGEST, resident_prov, resident_role_id,
        )
    except ImportError:
        from role_artifact_stub import STUB_ROLE_ARTIFACT
        from session_reg_helpers import (
            RESIDENT_DIGEST, resident_prov, resident_role_id,
        )

    class _FakeMemory:
        def __getattr__(self, _name):
            async def _noop(*a, **kw):
                return None
            return _noop

    cfg = AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT, role="butler",
        model="claude-sonnet-4-6", system_prompt="You are Tina.",
        character=CharacterConfig(name="Tina"),
        tools=ToolsConfig(
            allowed=["mcp__homeassistant"], permission_mode="acceptEdits",
        ),
        memory=MemoryConfig(token_budget=800, read_strategy="cached"),
        mcp_server_names=["homeassistant"],
        role_id=resident_role_id("butler"), kind="resident",
        binding_digest=RESIDENT_DIGEST,
        speaker_provenance=resident_prov("butler"),
    )
    return Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=mcp_registry,
        channel_manager=ChannelManager(),
        semantic_memory=_FakeMemory(),
    )


async def _armed_digest(butler) -> str:
    async with butler._armed_prompt_surface():
        digest = agent_mod._armed_surface_digest()
    assert digest is not None
    return digest


def _stored_entry(butler, digest: str) -> dict:
    return {
        "agent": butler.config.role_id, "sdk_session_id": "sid-before-upgrade",
        "last_active": datetime.now(timezone.utc).isoformat(),
        "binding_digest": butler.config.binding_digest,
        "prompt_surface_digest": digest,
    }


def _decide(butler, entry, digest):
    return _resume_decision(
        "ha_voice", entry, datetime.now(timezone.utc),
        role_id=butler.config.role_id,
        binding_digest=butler.config.binding_digest,
        prompt_surface_digest=digest,
    )


class _Publications:
    """The facade's on_schema_change: count, then run the real wiring."""

    def __init__(self) -> None:
        self.count = 0
        self.wire = None

    async def __call__(self) -> None:
        self.count += 1
        await self.wire()


async def _published_butler(tmp_path, sessions):
    """Boot: start the facade and publish it to a real butler, as casa_core does."""
    from casa_core import wire_tina_ha_facade
    from mcp_registry import McpServerRegistry

    registry = McpServerRegistry()
    butler = _butler(tmp_path, registry)
    publications = _Publications()
    facade = make_facade(sessions, on_schema_change=publications)
    publications.wire = lambda: wire_tina_ha_facade(
        registry, facade, {"butler": butler}, tina_role="butler",
    )
    await facade.start()
    # start() does not run the callback; casa_core publishes once after it.
    await wire_tina_ha_facade(
        registry, facade, {"butler": butler}, tina_role="butler",
    )
    return registry, butler, facade, publications


async def _drain_refresh(facade) -> None:
    task = facade._refresh_task
    if task is not None:
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_missing_tool_refreshes_and_retires_previous_surface(tmp_path):
    """RC-1 (C2-4): the reported sequence, end to end.

    A butler session registered against the bare names must not be resumed
    after HA answers a bare name with "Tool not found" — the facade must
    rediscover on that result, and the republished names must reach the
    resume decision."""
    sessions = SessionSequence(
        _bare_session({"HassTurnOn": _error_result(MISSING_TOOL_TEXT)}),
        _prefixed_session(),
    )
    registry, butler, facade, publications = await _published_butler(
        tmp_path, sessions,
    )
    try:
        d1 = await _armed_digest(butler)
        entry = _stored_entry(butler, d1)
        assert _decide(butler, entry, d1).action == "resume"

        published = registry.resolve(["homeassistant"], role="butler")
        result = await invoke_sdk_tool(
            published["homeassistant"], "HassTurnOn", {"name": "kitchen"},
        )
        await _drain_refresh(facade)

        assert result["content"][0]["text"] == MISSING_TOOL_TEXT
        assert sessions.open_count == 2
        assert publications.count == 1
        assert facade.tool_names == PREFIXED
        d2 = await _armed_digest(butler)
        assert d2 != d1
        decision = _decide(butler, entry, d2)
        assert decision.action == "new"
        assert decision.reason == "prompt_surface_changed"
        assert decision.retain_old is True
        assert decision.old.sdk_session_id == "sid-before-upgrade"
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_changed_ha_publication_changes_armed_surface(tmp_path):
    """RC-2 (arm 5 alone): a published rename must move the butler's resume
    identity, independently of what triggered the rediscovery."""
    sessions = SessionSequence(_bare_session(), _prefixed_session())
    registry, butler, facade, publications = await _published_butler(
        tmp_path, sessions,
    )
    try:
        d1 = await _armed_digest(butler)
        entry = _stored_entry(butler, d1)

        await facade.refresh()

        assert sessions.open_count == 2
        assert publications.count == 1
        assert facade.tool_names == PREFIXED
        d2 = await _armed_digest(butler)
        assert d2 != d1
        decision = _decide(butler, entry, d2)
        assert decision.action == "new"
        assert decision.reason == "prompt_surface_changed"
        assert decision.retain_old is True
    finally:
        await facade.aclose()
