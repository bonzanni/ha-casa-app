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
(``casa_core.wire_butler_ha_facade``), a real butler ``Agent``'s armed surface and
the real resume gate. Only the HA transport is faked.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from mcp.types import CallToolResult, TextContent

import agent as agent_mod
from agent import _resume_decision
from ha_mcp_facade import UNAVAILABLE_TEXT

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
    from casa_core import wire_butler_ha_facade
    from mcp_registry import McpServerRegistry

    registry = McpServerRegistry()
    butler = _butler(tmp_path, registry)
    publications = _Publications()
    facade = make_facade(sessions, on_schema_change=publications)
    publications.wire = lambda: wire_butler_ha_facade(
        registry, facade, {"butler": butler}, butler_role="butler",
    )
    await facade.start()
    # start() does not run the callback; casa_core publishes once after it.
    await wire_butler_ha_facade(
        registry, facade, {"butler": butler}, butler_role="butler",
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


# --- regression pins around the red cases ---------------------------------


class _GatedSession(FakeHaSession):
    """Hold each call until released, then answer with the configured result."""

    def __init__(self, *, tools, results) -> None:
        super().__init__(tools=tools, results=results)
        self.release = asyncio.Event()
        self.started = 0

    async def call_tool(self, name, arguments):
        self.started += 1
        await self.release.wait()
        return await super().call_tool(name, arguments)


@pytest.mark.asyncio
async def test_transport_blip_and_reordered_reconnect_retire_nothing(tmp_path):
    """C2-2/C2-3: a reconnect to the same names — after a transport failure,
    or listed in another order — must never retire the butler's session."""
    sessions = SessionSequence(
        _bare_session({"HassTurnOn": ConnectionError("transport detail")}),
        _bare_session(),
        FakeHaSession(tools=[live_context_tool(), action_tool("HassTurnOn")]),
    )
    registry, butler, facade, publications = await _published_butler(
        tmp_path, sessions,
    )
    try:
        d1 = await _armed_digest(butler)
        entry = _stored_entry(butler, d1)
        decisions = []

        published = registry.resolve(["homeassistant"], role="butler")
        await invoke_sdk_tool(
            published["homeassistant"], "HassTurnOn", {"name": "kitchen"},
        )
        await _drain_refresh(facade)
        assert sessions.open_count == 2
        assert publications.count == 0
        decisions.append(_decide(butler, entry, await _armed_digest(butler)))

        await facade.refresh()
        assert sessions.open_count == 3
        assert facade.tool_names == ("GetLiveContext", "HassTurnOn")
        decisions.append(_decide(butler, entry, await _armed_digest(butler)))

        assert [d.action for d in decisions] == ["resume", "resume"]
        assert sum(d.action == "new" for d in decisions) == 0
    finally:
        await facade.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    _error_result("Error calling tool: MatchFailedError: entity not found"),
    _error_result("Error calling tool: service light.turn_on failed"),
    _error_result('Tool "HassTurnOff" not found'),
    text_result('{"note": "Tool \\"HassTurnOn\\" not found"}'),
], ids=["entity-not-found", "unrelated-error", "other-tool", "not-an-error"])
async def test_only_a_missing_tool_result_for_the_called_name_rediscovers(result):
    sessions = SessionSequence(
        _bare_session({"HassTurnOn": result}), _prefixed_session(),
    )
    facade = make_facade(sessions)
    await facade.start()
    try:
        await invoke_sdk_tool(
            facade.server_config, "HassTurnOn", {"name": "kitchen"},
        )
        await _drain_refresh(facade)
        assert sessions.open_count == 1
        assert facade._refresh_task is None
        assert facade.tool_names == BARE
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_missing_tool_results_in_one_generation_request_one_rediscovery():
    """While a requested refresh waits for the facade lock, more not-found
    results from the same connection must not queue a second one."""
    missing = {"HassTurnOn": _error_result(MISSING_TOOL_TEXT)}
    sessions = SessionSequence(
        _bare_session(missing), _prefixed_session(), _prefixed_session(),
    )
    facade = make_facade(sessions)
    await facade.start()
    try:
        await facade._lock.acquire()
        try:
            for _ in range(3):
                await asyncio.wait_for(invoke_sdk_tool(
                    facade.server_config, "HassTurnOn", {"name": "kitchen"},
                ), timeout=5)
                for _ in range(5):
                    await asyncio.sleep(0)
        finally:
            facade._lock.release()
        await _drain_refresh(facade)
        assert sessions.open_count == 2
        assert facade.tool_names == PREFIXED
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_a_late_missing_tool_result_from_a_replaced_connection_asks_nothing():
    """A call still waiting when a refresh replaces its connection asks for no
    rediscovery. #1400: closing the connection abandons the call — a closed
    MCP connection never answers it — so it ends as unavailable rather than
    carrying the late "not found" back."""
    stale = _GatedSession(
        tools=[action_tool("HassTurnOn"), live_context_tool()],
        results={"HassTurnOn": _error_result(MISSING_TOOL_TEXT)},
    )
    sessions = SessionSequence(stale, _prefixed_session(), _prefixed_session())
    facade = make_facade(sessions)
    await facade.start()
    try:
        call = asyncio.create_task(invoke_sdk_tool(
            facade.server_config, "HassTurnOn", {"name": "kitchen"},
        ))
        while stale.started == 0:
            await asyncio.sleep(0)
        await facade.refresh()
        assert sessions.open_count == 2
        stale.release.set()
        result = await asyncio.wait_for(call, timeout=5)
        await _drain_refresh(facade)
        assert result["content"][0]["text"] == UNAVAILABLE_TEXT
        assert sessions.open_count == 2
        assert facade._refresh_task is None
    finally:
        stale.release.set()
        await facade.aclose()


@pytest.mark.asyncio
async def test_a_missing_tool_rediscovery_is_not_awaited_by_the_call():
    """The call returns while the rediscovery it requested cannot yet run."""
    sessions = SessionSequence(
        _bare_session({"HassTurnOn": _error_result(MISSING_TOOL_TEXT)}),
        _prefixed_session(),
    )
    facade = make_facade(sessions)
    await facade.start()
    try:
        await facade._lock.acquire()
        try:
            result = await asyncio.wait_for(invoke_sdk_tool(
                facade.server_config, "HassTurnOn", {"name": "kitchen"},
            ), timeout=5)
            assert result["content"][0]["text"] == MISSING_TOOL_TEXT
            assert sessions.open_count == 1
            assert facade._refresh_task is not None
            assert not facade._refresh_task.done()
        finally:
            facade._lock.release()
        await _drain_refresh(facade)
        assert sessions.open_count == 2
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_error_call_line_carries_a_bounded_single_line_excerpt(caplog):
    import logging

    text = "Error calling tool:\n" + "x" * 500
    upstream = _bare_session({"HassTurnOn": _error_result(text)})
    facade = make_facade(upstream)
    await facade.start()
    try:
        with caplog.at_level(logging.INFO, logger="ha_mcp_facade"):
            await invoke_sdk_tool(
                facade.server_config, "HassTurnOn", {"name": "SECRET_ARG"},
            )
        lines = [
            r.getMessage() for r in caplog.records
            if "ha_facade_call" in r.getMessage()
        ]
        expected = repr(" ".join(text.split())[:200])
        assert lines == [
            f"ha_facade_call tool=HassTurnOn ok=False ms=0 error={expected}"
        ]
        assert "SECRET_ARG" not in caplog.text
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_prefixed_live_context_keeps_the_facade_contract(caplog):
    import logging
    from ha_mcp_facade import LIVE_CONTEXT_SCHEMA

    upstream = FakeHaSession(tools=[
        _prefixed_live_context(),
        action_tool("OtherGetLiveContext"),
    ])
    facade = make_facade(upstream)
    await facade.start()
    try:
        by_name = {t.name: t for t in facade.tools}
        assert by_name["homeassistant__GetLiveContext"].input_schema == (
            LIVE_CONTEXT_SCHEMA
        )
        assert by_name["OtherGetLiveContext"].input_schema != (
            LIVE_CONTEXT_SCHEMA
        )
        with caplog.at_level(logging.INFO, logger="ha_mcp_facade"):
            await invoke_sdk_tool(
                facade.server_config, "homeassistant__GetLiveContext",
                {"domain": "light"},
            )
            await invoke_sdk_tool(
                facade.server_config, "OtherGetLiveContext",
                {"name": "kept"},
            )
        assert upstream.calls == [
            ("homeassistant__GetLiveContext", {}),
            ("OtherGetLiveContext", {"name": "kept"}),
        ]
        assert caplog.text.count("live-context passthrough") == 1
    finally:
        await facade.aclose()


def test_a_role_with_no_published_surface_keeps_its_digest_byte_identical():
    """Only the butler's identity may move: with no tool surface the digest is
    exactly the #1029 formula over the three blocks."""
    import hashlib

    surface = agent_mod._render_prompt_surface(
        "assistant", [], None, live_names={}, allowed_tools=[],
    )
    expected = "sha256:" + hashlib.sha256("\x1f".join(
        (surface.delegates, surface.jobs, surface.executors),
    ).encode("utf-8")).hexdigest()
    assert surface.tool_servers == ()
    assert surface.digest == expected


def test_the_registry_pairs_a_config_with_the_digest_it_was_published_with():
    from mcp_registry import McpServerRegistry

    registry = McpServerRegistry()
    first, second = {"n": 1}, {"n": 2}
    registry.register_role_sdk("homeassistant", "butler", first,
                               surface_digest="sha256:one")
    assert registry.role_surfaces(
        ["homeassistant", "casa-framework"], role="butler",
    ) == (("homeassistant", first, "sha256:one"),)
    assert registry.role_surfaces(["homeassistant"], role="assistant") == ()
    registry.register_role_sdk("homeassistant", "butler", second)
    assert registry.role_surfaces(["homeassistant"], role="butler") == ()
    registry.register_role_sdk("homeassistant", "butler", second,
                               surface_digest="sha256:two")
    registry.unregister_role_sdk("homeassistant", "butler")
    assert registry.role_surfaces(["homeassistant"], role="butler") == ()


@pytest.mark.asyncio
async def test_a_publication_mid_turn_does_not_reach_that_turns_options(
    tmp_path,
):
    """The turn connects with the HA tools its resume decision gated on; the
    next armed turn picks up the new publication."""
    sessions = SessionSequence(_bare_session(), _prefixed_session())
    registry, butler, facade, publications = await _published_butler(
        tmp_path, sessions,
    )
    try:
        armed_config = registry.resolve(
            ["homeassistant"], role="butler",
        )["homeassistant"]
        async with butler._armed_prompt_surface():
            armed_digest = agent_mod._armed_surface_digest()
            await facade.refresh()
            assert publications.count == 1
            options = await butler._build_options(
                channel="ha_voice", channel_key="k", is_fresh=True,
                resume_sid=None, user_text="turn on the kitchen",
            )
        assert options.mcp_servers["homeassistant"] is armed_config
        assert await _armed_digest(butler) != armed_digest
        async with butler._armed_prompt_surface():
            options = await butler._build_options(
                channel="ha_voice", channel_key="k", is_fresh=True,
                resume_sid=None, user_text="turn on the kitchen",
            )
        assert options.mcp_servers["homeassistant"] is facade.server_config
    finally:
        await facade.aclose()
