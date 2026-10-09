"""Tests for the eager in-process Home Assistant MCP facade."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Callable

import pytest
from mcp.types import (
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    Implementation,
    InitializeResult,
    ListToolsResult,
    ServerCapabilities,
    TextContent,
    Tool,
    ToolsCapability,
)

from ha_mcp_facade import UNAVAILABLE_TEXT, HomeAssistantFacade


pytestmark = pytest.mark.unit


class FakeHaSession:
    """Complete MCP-session boundary fake with typed protocol results."""

    def __init__(
        self,
        *,
        tools: list[Tool],
        results: dict[str, CallToolResult | BaseException] | None = None,
    ) -> None:
        self._tools = tools
        self._results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def initialize(self) -> InitializeResult:
        return InitializeResult(
            protocolVersion="2025-06-18",
            capabilities=ServerCapabilities(tools=ToolsCapability()),
            serverInfo=Implementation(name="fake-ha", version="1.0"),
        )

    async def list_tools(self) -> ListToolsResult:
        return ListToolsResult(tools=self._tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        self.calls.append((name, arguments))
        outcome = self._results.get(name)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is not None:
            return outcome
        return text_result('{"success":true}')


class RawDiscoverySession(FakeHaSession):
    """Mirror the pinned client's whole-list validation boundary."""

    def __init__(self, raw_tools: list[dict[str, Any]]) -> None:
        super().__init__(tools=[])
        self._raw_tools = raw_tools

    async def list_tools(self) -> ListToolsResult:
        return ListToolsResult.model_validate({"tools": self._raw_tools})

    async def send_request(self, _request: Any, result_type: Any) -> Any:
        return result_type.model_validate({"tools": self._raw_tools})


class RawCallSession(RawDiscoverySession):
    """Expose raw request support while rejecting strict call_tool()."""

    def __init__(self, raw_tools: list[dict[str, Any]]) -> None:
        super().__init__(raw_tools)
        self.raw_calls: list[tuple[str, dict[str, Any]]] = []

    async def send_request(self, request: Any, result_type: Any) -> Any:
        if isinstance(request.root, CallToolRequest):
            params = request.root.params
            self.raw_calls.append((params.name, params.arguments or {}))
            return text_result('{"success":true}')
        return await super().send_request(request, result_type)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        raise AssertionError("strict call_tool path must not rediscover tools")


@asynccontextmanager
async def fake_connection(session: FakeHaSession):
    yield session


class SessionSequence:
    """Open one complete fake MCP session per facade connection."""

    def __init__(self, *sessions: FakeHaSession) -> None:
        self.sessions = sessions
        self.open_count = 0

    def __call__(self):
        @asynccontextmanager
        async def connection():
            session = self.sessions[self.open_count]
            self.open_count += 1
            yield session

        return connection()


class CoordinatedFailingSession(FakeHaSession):
    """Fail two in-flight calls together to exercise reconnect deduplication."""

    def __init__(self, *, tools: list[Tool]) -> None:
        super().__init__(tools=tools)
        self._both_started = asyncio.Event()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        self.calls.append((name, arguments))
        if len(self.calls) == 2:
            self._both_started.set()
        await self._both_started.wait()
        raise ConnectionError("secret-bearing transport detail")


class StaggeredFailingSession(FakeHaSession):
    """Let one old-generation failure arrive after recovery completes."""

    def __init__(self, *, tools: list[Tool]) -> None:
        super().__init__(tools=tools)
        self.slow_started = asyncio.Event()
        self.release_slow = asyncio.Event()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        self.calls.append((name, arguments))
        if arguments["name"] == "slow":
            self.slow_started.set()
            await self.release_slow.wait()
        else:
            await self.slow_started.wait()
        raise ConnectionError("secret-bearing staggered detail")


class BlockingInitializeSession(FakeHaSession):
    """Keep a reconnect in flight while the facade has no session."""

    def __init__(self, *, tools: list[Tool]) -> None:
        super().__init__(tools=tools)
        self.initialize_started = asyncio.Event()
        self.release_initialize = asyncio.Event()

    async def initialize(self) -> InitializeResult:
        self.initialize_started.set()
        await self.release_initialize.wait()
        return await super().initialize()


class SchemaChangeRecorder:
    def __init__(self) -> None:
        self.count = 0

    async def __call__(self) -> None:
        self.count += 1


class BlockingSchemaChangeRecorder(SchemaChangeRecorder):
    """Expose a published changed surface while its callback remains blocked."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self) -> None:
        self.count += 1
        self.entered.set()
        await self.release.wait()


async def wait_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


def make_facade(
    session: FakeHaSession | SessionSequence,
    *,
    on_schema_change: SchemaChangeRecorder | None = None,
    monotonic=None,
) -> HomeAssistantFacade:
    session_factory = (
        session if isinstance(session, SessionSequence)
        else lambda: fake_connection(session)
    )
    kwargs = {"monotonic": monotonic} if monotonic is not None else {}
    return HomeAssistantFacade(
        "http://ha/mcp",
        {"Authorization": "Bearer secret"},
        on_schema_change=on_schema_change,
        session_factory=session_factory,
        **kwargs,
    )


def text_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)])


def action_tool(name: str) -> Tool:
    return Tool(
        name=name,
        description=f"Action {name}",
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
        },
    )


def live_context_tool() -> Tool:
    return Tool(
        name="GetLiveContext",
        description="Current Home Assistant state",
        inputSchema={},
    )


async def invoke_sdk_tool(
    server_config: dict[str, Any],
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Invoke through the real low-level SDK server request handler."""
    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments),
    )
    wrapped = await server_config["instance"].request_handlers[CallToolRequest](
        request,
    )
    result = wrapped.root
    payload = {
        "content": [
            item.model_dump(mode="json", by_alias=True, exclude_none=True)
            for item in result.content
        ],
    }
    if result.isError:
        payload["is_error"] = True
    return payload


@pytest.mark.asyncio
async def test_discovers_all_healthy_tools_and_normalizes_live_context():
    upstream = FakeHaSession(
        tools=[action_tool("HassTurnOff"), live_context_tool()],
    )
    facade = make_facade(upstream)

    await facade.start()
    try:
        by_name = {candidate.name: candidate for candidate in facade.tools}
        assert set(by_name) == {"HassTurnOff", "GetLiveContext"}
        assert by_name["GetLiveContext"].input_schema == {
            "type": "object",
            "properties": {"domain": {"type": "string"}},
            "additionalProperties": False,
        }
        assert facade.tool_names == ("HassTurnOff", "GetLiveContext")
        assert facade.server_config["alwaysLoad"] is True
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_one_bad_schema_omits_only_that_tool(caplog):
    broken = Tool.model_construct(
        name="Broken",
        description="Bad schema",
        inputSchema="not-an-object",
    )
    upstream = FakeHaSession(
        tools=[broken, action_tool("HassTurnOn")],
    )
    facade = make_facade(upstream)

    await facade.start()
    try:
        assert facade.tool_names == ("HassTurnOn",)
        assert "Broken" in caplog.text
        assert "not-an-object" not in caplog.text
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_raw_discovery_omits_bad_schema_without_losing_healthy_tool():
    upstream = RawDiscoverySession([
        {
            "name": "Broken",
            "description": "Bad schema",
            "inputSchema": "not-an-object",
        },
        {
            "name": "HassTurnOn",
            "description": "Turn on",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ])
    facade = make_facade(upstream)

    await facade.start()
    try:
        assert facade.tool_names == ("HassTurnOn",)
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_raw_discovery_calls_healthy_tool_without_strict_rediscovery():
    upstream = RawCallSession([
        {
            "name": "Broken",
            "description": "Bad schema",
            "inputSchema": "not-an-object",
        },
        {
            "name": "HassTurnOn",
            "description": "Turn on",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ])
    facade = make_facade(upstream)

    await facade.start()
    try:
        result = await invoke_sdk_tool(
            facade.server_config,
            "HassTurnOn",
            {"name": "office light"},
        )
        assert "is_error" not in result
        assert upstream.raw_calls == [
            ("HassTurnOn", {"name": "office light"}),
        ]
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_live_context_domain_is_never_forwarded_upstream():
    upstream = FakeHaSession(
        tools=[live_context_tool()],
        results={
            "GetLiveContext": text_result(json.dumps({
                "light.kitchen": "on",
                "climate.office": "idle",
            })),
        },
    )
    facade = make_facade(upstream)

    await facade.start()
    try:
        result = await invoke_sdk_tool(
            facade.server_config,
            "GetLiveContext",
            {"domain": "light"},
        )
        assert upstream.calls == [("GetLiveContext", {})]
        # The payload is passed through verbatim — casa does not filter
        # GetLiveContext output (the pre-#223 flat-dict domain filter is
        # gone); the domain argument only scopes the (never-forwarded)
        # request intent.
        assert json.loads(result["content"][0]["text"]) == {
            "light.kitchen": "on",
            "climate.office": "idle",
        }
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_live_context_success_result_envelope_passes_through(caplog):
    # Regression for issue #223: current HA GetLiveContext returns a
    # {"success": bool, "result": "<text overview>"} envelope, not a dict
    # keyed by domain.entity ids. The old filter kept only top-level keys
    # whose prefix matched the domain, dropping every device (the live bug
    # logged object_count=1 input_count=2 output_count=0). The result text
    # must survive untouched.
    overview = (
        "Live Context: An overview of the areas and the devices in this "
        "smart home:\n"
        "- names: Bathroom\n  domain: light\n  state: on\n"
        "- names: Kitchen\n  domain: light\n  state: off\n"
    )
    raw = json.dumps({"success": True, "result": overview})
    upstream = FakeHaSession(
        tools=[live_context_tool()],
        results={"GetLiveContext": text_result(raw)},
    )
    facade = make_facade(upstream)

    await facade.start()
    try:
        with caplog.at_level(logging.INFO):
            result = await invoke_sdk_tool(
                facade.server_config,
                "GetLiveContext",
                {"domain": "light"},
            )
        assert upstream.calls == [("GetLiveContext", {})]
        # The envelope is passed through verbatim — the device data survives.
        payload = json.loads(result["content"][0]["text"])
        assert payload == {"success": True, "result": overview}
        assert "Bathroom" in payload["result"]
        assert "Kitchen" in payload["result"]
        assert "domain: light" in payload["result"]
        # Truthful, distinct log so a future contract change is observable;
        # the old buggy "output_count=0" line must not appear.
        assert "live-context passthrough" in caplog.text
        assert "shape=success_result" in caplog.text
        assert "output_count=0" not in caplog.text
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_live_context_without_domain_is_returned_unchanged():
    # No `domain` argument means the facade must not touch the response at
    # all (the filter is never entered) — the full envelope reaches the caller.
    overview = json.dumps(
        {"success": True, "result": "Live Context: everything here"},
    )
    upstream = FakeHaSession(
        tools=[live_context_tool()],
        results={"GetLiveContext": text_result(overview)},
    )
    facade = make_facade(upstream)

    await facade.start()
    try:
        result = await invoke_sdk_tool(
            facade.server_config,
            "GetLiveContext",
            {},
        )
        assert upstream.calls == [("GetLiveContext", {})]
        assert result["content"][0]["text"] == overview
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_unparseable_live_context_is_returned_unchanged():
    raw = "Kitchen light: on\nOffice climate: idle"
    upstream = FakeHaSession(
        tools=[live_context_tool()],
        results={"GetLiveContext": text_result(raw)},
    )
    facade = make_facade(upstream)

    await facade.start()
    try:
        result = await invoke_sdk_tool(
            facade.server_config,
            "GetLiveContext",
            {"domain": "light"},
        )
        assert upstream.calls == [("GetLiveContext", {})]
        assert result["content"][0]["text"] == raw
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_parseable_non_object_live_context_is_returned_unchanged():
    raw = "[]"
    upstream = FakeHaSession(
        tools=[live_context_tool()],
        results={"GetLiveContext": text_result(raw)},
    )
    facade = make_facade(upstream)

    await facade.start()
    try:
        result = await invoke_sdk_tool(
            facade.server_config,
            "GetLiveContext",
            {"domain": "light"},
        )
        assert upstream.calls == [("GetLiveContext", {})]
        assert result["content"][0]["text"] == raw
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_action_proxy_preserves_arguments_exactly():
    upstream = FakeHaSession(
        tools=[action_tool("HassTurnOff")],
        results={"HassTurnOff": text_result('{"success":true}')},
    )
    facade = make_facade(upstream)

    await facade.start()
    try:
        await invoke_sdk_tool(
            facade.server_config,
            "HassTurnOff",
            {"name": "office light"},
        )
        assert upstream.calls == [
            ("HassTurnOff", {"name": "office light"}),
        ]
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_facade_call_logs_only_tool_status_and_monotonic_ms(caplog):
    secret_argument = "SECRET_HA_ARGUMENT"
    secret_response = "SECRET_HA_RESPONSE"
    clock = iter((10.0, 10.125))
    upstream = FakeHaSession(
        tools=[action_tool("HassTurnOff")],
        results={"HassTurnOff": text_result(secret_response)},
    )
    facade = make_facade(upstream, monotonic=lambda: next(clock))

    await facade.start()
    try:
        with caplog.at_level(logging.INFO, logger="ha_mcp_facade"):
            await invoke_sdk_tool(
                facade.server_config,
                "HassTurnOff",
                {"name": secret_argument},
            )
        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "ha_mcp_facade"
            and "ha_facade_call" in record.getMessage()
        ]
        assert messages == [
            "ha_facade_call tool=HassTurnOff ok=True ms=125"
        ]
        assert secret_argument not in caplog.text
        assert secret_response not in caplog.text
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_transport_failure_returns_fixed_error_and_refreshes_once(caplog):
    failed = FakeHaSession(
        tools=[action_tool("HassTurnOn")],
        results={
            "HassTurnOn": ConnectionError(
                "secret-bearing transport detail",
            ),
        },
    )
    healthy = FakeHaSession(tools=[action_tool("HassTurnOn")])
    sessions = SessionSequence(failed, healthy)
    changed = SchemaChangeRecorder()
    clock = iter((20.0, 20.250))
    facade = make_facade(
        sessions,
        on_schema_change=changed,
        monotonic=lambda: next(clock),
    )

    await facade.start()
    try:
        original_config = facade.server_config
        with caplog.at_level(logging.INFO, logger="ha_mcp_facade"):
            result = await invoke_sdk_tool(
                original_config,
                "HassTurnOn",
                {"name": "private office"},
            )
        assert result == {
            "content": [{
                "type": "text",
                "text": "Home Assistant is temporarily unavailable.",
            }],
            "is_error": True,
        }
        await wait_until(
            lambda: sessions.open_count == 2
            and facade.server_config is not original_config,
        )
        assert changed.count == 0
        assert sessions.open_count == 2
        assert failed.calls == [
            ("HassTurnOn", {"name": "private office"}),
        ]
        assert healthy.calls == []
        assert "Bearer secret" not in caplog.text
        assert "secret-bearing transport detail" not in caplog.text
        assert "private office" not in caplog.text
        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == "ha_mcp_facade"
            and "ha_facade_call" in record.getMessage()
        ] == ["ha_facade_call tool=HassTurnOn ok=False ms=250"]
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_concurrent_transport_failures_share_one_reconnect():
    failed = CoordinatedFailingSession(
        tools=[action_tool("HassTurnOn")],
    )
    healthy = FakeHaSession(tools=[action_tool("HassTurnOn")])
    sessions = SessionSequence(failed, healthy)
    changed = SchemaChangeRecorder()
    facade = make_facade(sessions, on_schema_change=changed)

    await facade.start()
    try:
        original_config = facade.server_config
        results = await asyncio.gather(
            invoke_sdk_tool(original_config, "HassTurnOn", {"name": "one"}),
            invoke_sdk_tool(original_config, "HassTurnOn", {"name": "two"}),
        )
        assert all(result["is_error"] is True for result in results)
        await wait_until(
            lambda: sessions.open_count == 2
            and facade.server_config is not original_config,
        )
        assert changed.count == 0
        assert sessions.open_count == 2
        assert len(failed.calls) == 2
        assert healthy.calls == []
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_unavailable_calls_do_not_queue_redundant_reconnect():
    failed = FakeHaSession(
        tools=[action_tool("HassTurnOn")],
        results={"HassTurnOn": ConnectionError("initial failure")},
    )
    replacement = BlockingInitializeSession(
        tools=[action_tool("HassTurnOn")],
    )
    redundant_reconnect_sentinel = FakeHaSession(
        tools=[action_tool("HassTurnOn")],
    )
    sessions = SessionSequence(
        failed,
        replacement,
        redundant_reconnect_sentinel,
    )
    facade = make_facade(sessions)
    unavailable = {
        "content": [{
            "type": "text",
            "text": "Home Assistant is temporarily unavailable.",
        }],
        "is_error": True,
    }

    await facade.start()
    try:
        original_config = facade.server_config
        failed_result = await invoke_sdk_tool(
            original_config,
            "HassTurnOn",
            {"name": "failed"},
        )
        assert failed_result == unavailable

        await asyncio.wait_for(replacement.initialize_started.wait(), timeout=1)
        assert sessions.open_count == 2

        unavailable_results = await asyncio.wait_for(
            asyncio.gather(*(
                invoke_sdk_tool(
                    original_config,
                    "HassTurnOn",
                    {"name": f"waiting-{index}"},
                )
                for index in range(16)
            )),
            timeout=1,
        )
        assert unavailable_results == [unavailable] * 16
        assert sessions.open_count == 2
        assert replacement.calls == []

        replacement.release_initialize.set()
        await wait_until(
            lambda: facade.server_config is not original_config,
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert sessions.open_count == 2
        healthy_result = await invoke_sdk_tool(
            facade.server_config,
            "HassTurnOn",
            {"name": "healthy"},
        )
        assert "is_error" not in healthy_result
        assert failed.calls == [("HassTurnOn", {"name": "failed"})]
        assert replacement.calls == [("HassTurnOn", {"name": "healthy"})]
        assert redundant_reconnect_sentinel.calls == []
    finally:
        replacement.release_initialize.set()
        await facade.aclose()


@pytest.mark.asyncio
async def test_failure_during_blocked_schema_callback_preserves_refresh():
    initial = FakeHaSession(
        tools=[action_tool("HassTurnOn")],
        results={"HassTurnOn": ConnectionError("initial failure")},
    )
    recovered = FakeHaSession(
        tools=[
            action_tool("HassTurnOn"),
            action_tool("HassLightSet"),
        ],
        results={"HassTurnOn": ConnectionError("recovered failure")},
    )
    healthy = FakeHaSession(tools=[
        action_tool("HassTurnOn"),
        action_tool("HassLightSet"),
    ])
    reconnect_storm_sentinel = FakeHaSession(tools=[
        action_tool("HassTurnOn"),
        action_tool("HassLightSet"),
    ])
    sessions = SessionSequence(
        initial,
        recovered,
        healthy,
        reconnect_storm_sentinel,
    )
    changed = BlockingSchemaChangeRecorder()
    facade = make_facade(sessions, on_schema_change=changed)

    await facade.start()
    try:
        original_config = facade.server_config
        initial_result = await invoke_sdk_tool(
            original_config,
            "HassTurnOn",
            {"name": "initial"},
        )
        assert initial_result["is_error"] is True

        await asyncio.wait_for(changed.entered.wait(), timeout=1)
        recovered_config = facade.server_config
        assert recovered_config is not original_config
        assert facade.tool_names == ("HassTurnOn", "HassLightSet")
        assert sessions.open_count == 2
        assert changed.count == 1

        recovered_result = await invoke_sdk_tool(
            recovered_config,
            "HassTurnOn",
            {"name": "recovered"},
        )
        assert recovered_result["is_error"] is True
        assert sessions.open_count == 2

        changed.release.set()
        await wait_until(
            lambda: sessions.open_count == 3
            and facade.server_config is not recovered_config,
        )
        healthy_config = facade.server_config
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert sessions.open_count == 3
        assert changed.count == 1
        healthy_result = await invoke_sdk_tool(
            healthy_config,
            "HassTurnOn",
            {"name": "healthy"},
        )
        assert "is_error" not in healthy_result
        assert initial.calls == [("HassTurnOn", {"name": "initial"})]
        assert recovered.calls == [("HassTurnOn", {"name": "recovered"})]
        assert healthy.calls == [("HassTurnOn", {"name": "healthy"})]
        assert reconnect_storm_sentinel.calls == []
    finally:
        changed.release.set()
        await facade.aclose()


@pytest.mark.asyncio
async def test_new_tool_appears_after_refresh_without_losing_healthy_tools():
    sessions = SessionSequence(
        FakeHaSession(tools=[action_tool("HassTurnOn")]),
        FakeHaSession(tools=[
            action_tool("HassTurnOn"),
            action_tool("HassLightSet"),
        ]),
    )
    changed = SchemaChangeRecorder()
    facade = make_facade(sessions, on_schema_change=changed)

    await facade.start()
    try:
        await facade.refresh()
        assert facade.tool_names == ("HassTurnOn", "HassLightSet")
        assert sessions.open_count == 2
        assert changed.count == 1
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_failed_schema_publication_retries_on_next_refresh():
    """#343: _refresh_locked used to commit the new surface descriptor
    BEFORE on_schema_change published it — a publish failure made every
    later refresh see the surface as unchanged, freezing the agent's tool
    surface until HA changed schema again or a restart."""
    sessions = SessionSequence(
        FakeHaSession(tools=[action_tool("HassTurnOn")]),
        FakeHaSession(tools=[
            action_tool("HassTurnOn"),
            action_tool("HassLightSet"),
        ]),
        FakeHaSession(tools=[
            action_tool("HassTurnOn"),
            action_tool("HassLightSet"),
        ]),
    )

    class FlakyRecorder(SchemaChangeRecorder):
        async def __call__(self) -> None:
            await super().__call__()
            if self.count == 1:
                raise RuntimeError("SDK reload failed")

    changed = FlakyRecorder()
    facade = make_facade(sessions, on_schema_change=changed)

    await facade.start()
    try:
        with pytest.raises(RuntimeError):
            await facade.refresh()          # publish fails
        # Same (already-committed) surface — the failed publication must
        # still be retried, not silently dropped.
        await facade.refresh()
        assert changed.count == 2
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_slow_publisher_success_does_not_clear_newer_failed_publication():
    """Terra r2-1: refresh A's callback is still in flight when refresh B
    commits a NEWER surface and B's publication fails. A's late success
    must not clear the pending-publication flag B set — the next refresh
    still owes a republication of B's surface."""
    sessions = SessionSequence(
        FakeHaSession(tools=[action_tool("HassTurnOn")]),
        FakeHaSession(tools=[
            action_tool("HassTurnOn"),
            action_tool("HassLightSet"),
        ]),
        FakeHaSession(tools=[
            action_tool("HassTurnOn"),
            action_tool("HassLightSet"),
            action_tool("HassClimateSet"),
        ]),
        FakeHaSession(tools=[
            action_tool("HassTurnOn"),
            action_tool("HassLightSet"),
            action_tool("HassClimateSet"),
        ]),
    )

    release_a = asyncio.Event()
    entered_a = asyncio.Event()

    class Recorder(SchemaChangeRecorder):
        async def __call__(self) -> None:
            await super().__call__()
            if self.count == 1:          # refresh A: block until released
                entered_a.set()
                await release_a.wait()
            elif self.count == 2:        # refresh B: newer surface, fails
                raise RuntimeError("SDK reload failed")

    changed = Recorder()
    facade = make_facade(sessions, on_schema_change=changed)

    await facade.start()
    try:
        task_a = asyncio.create_task(facade.refresh())
        await asyncio.wait_for(entered_a.wait(), timeout=5)
        with pytest.raises(RuntimeError):
            await facade.refresh()       # B commits newer, publish fails
        release_a.set()
        await asyncio.wait_for(task_a, timeout=5)  # A succeeds late

        # The flag must still be pending: the next refresh republishes.
        await facade.refresh()
        assert changed.count == 3
    finally:
        release_a.set()
        await facade.aclose()


@pytest.mark.asyncio
async def test_identical_normalized_surface_does_not_notify_schema_change():
    initial = Tool(
        name="HassTurnOn",
        description="Turn on",
        inputSchema={},
    )
    equivalent = Tool(
        name="HassTurnOn",
        description="Turn on",
        inputSchema={"type": "object", "properties": {}},
    )
    sessions = SessionSequence(
        FakeHaSession(tools=[initial]),
        FakeHaSession(tools=[equivalent]),
    )
    changed = SchemaChangeRecorder()
    facade = make_facade(sessions, on_schema_change=changed)

    await facade.start()
    try:
        original_config = facade.server_config
        await facade.refresh()
        assert facade.server_config is not original_config
        assert changed.count == 0
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_stale_generation_failure_does_not_replace_recovered_session():
    failed = StaggeredFailingSession(
        tools=[action_tool("HassTurnOn")],
    )
    healthy = FakeHaSession(tools=[action_tool("HassTurnOn")])
    replacement = FakeHaSession(tools=[action_tool("HassTurnOn")])
    sessions = SessionSequence(failed, healthy, replacement)
    facade = make_facade(sessions)

    await facade.start()
    try:
        original_config = facade.server_config
        slow_call = asyncio.create_task(
            invoke_sdk_tool(
                original_config,
                "HassTurnOn",
                {"name": "slow"},
            ),
        )
        await asyncio.wait_for(failed.slow_started.wait(), timeout=1)

        fast_result = await invoke_sdk_tool(
            original_config,
            "HassTurnOn",
            {"name": "fast"},
        )
        assert fast_result["is_error"] is True
        await wait_until(
            lambda: sessions.open_count == 2
            and facade.server_config is not original_config,
        )
        recovered_config = facade.server_config

        failed.release_slow.set()
        slow_result = await slow_call
        assert slow_result["is_error"] is True
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert sessions.open_count == 2
        await invoke_sdk_tool(
            recovered_config,
            "HassTurnOn",
            {"name": "healthy"},
        )
        assert healthy.calls == [("HassTurnOn", {"name": "healthy"})]
        assert replacement.calls == []
    finally:
        failed.release_slow.set()
        await facade.aclose()


async def test_pin_inv_ha_001_action_arguments_pass_through_unchanged():
    """Pins INV-HA-001: no code here restricts a control action by entity or
    domain — ordinary action arguments reach the upstream unchanged.

    Red case demonstrated: filtering entity_id/domain out of non-live-context
    arguments in HomeAssistantFacade._call fails this test.
    """
    upstream = FakeHaSession(tools=[Tool(
        name="HassTurnOff",
        description="action",
        inputSchema={"type": "object", "properties": {
            "entity_id": {"type": "string"}, "domain": {"type": "string"},
        }},
    )])
    facade = make_facade(upstream)
    await facade.start()
    try:
        arguments = {"entity_id": "switch.security_system", "domain": "switch"}
        await invoke_sdk_tool(facade.server_config, "HassTurnOff", arguments)
        assert upstream.calls == [("HassTurnOff", arguments)]
    finally:
        await facade.aclose()


@pytest.mark.asyncio
async def test_pin_inv_ha_001_explanation_matches_live_context():
    """#1103: the INV-HA-001 explanation agrees with the facade — the
    live-context tool's upstream arguments are replaced with {}, and nothing
    filters returned content (nor is the result forwarded verbatim:
    _sdk_result keeps only content items and isError).

    Red case demonstrated: the base paragraph's "the only filtering applied
    is to returned content on the read path" fails the no-filtering check.
    """
    from pathlib import Path
    import re

    upstream = FakeHaSession(tools=[live_context_tool()])
    facade = make_facade(upstream)
    await facade.start()
    try:
        await invoke_sdk_tool(
            facade.server_config, "GetLiveContext", {"domain": "light"},
        )
        assert upstream.calls == [("GetLiveContext", {})], "upstream substitution"
    finally:
        await facade.aclose()

    doc = (
        Path(__file__).resolve().parents[1]
        / "docs/architecture/home-assistant-control.md"
    ).read_text(encoding="utf-8")
    statements = list(re.finditer(r"(?m)^\*\*INV-HA-001\*\*:.*$", doc))
    assert len(statements) == 1, "one INV-HA-001 statement"
    tail = doc[statements[0].end():]
    boundary = re.search(r"(?m)^What it does not cover:", tail)
    assert boundary is not None, "explanation boundary"
    paragraphs = re.split(r"\n\s*\n", tail[:boundary.start()].strip())
    assert len(paragraphs) == 1 and paragraphs[0], "one explanation paragraph"
    p = " ".join(paragraphs[0].replace(chr(96), "").lower().split())

    assert re.search(
        r"\b(?:no (?:returned[- ]content|response[- ]content) filtering"
        r"|(?:returned content|response content|responses?) "
        r"(?:is |are |passes? through )?unfiltered"
        r"|(?:does not|never) filters? (?:returned|response) content)\b", p
    ), "no returned-content filtering"
    assert not re.search(
        r"\b(?:only filtering applied is to returned content"
        r"|filters? (?:the )?returned content on the read path)\b", p
    ), "no retained filtering claim"
    assert "verbatim" not in p, "no verbatim-result claim"
    assert not re.search(
        r"\b(?:results?|responses?)\b[^.;]*\b"
        r"(?:forwarded|returned|passed(?: through)?) unchanged\b", p
    ), "no unchanged-result claim"

    clauses = re.split(r"[.;]\s*", p)
    assert any(
        re.search(r"\b(?:live[- ]context|getlivecontext)\b", c)
        and re.search(r"\barguments?\b", c)
        and "{}" in c
        and re.search(r"\b(?:replac\w*|substitut\w*|reset\w*)\b", c)
        for c in clauses
    ), "live-context argument substitution"
    for c in clauses:
        if re.search(r"\barguments (?:pass through|are forwarded) unchanged\b", c):
            assert re.search(
                r"\b(?:ordinary|non[- ]live[- ]context|except|exception)\b", c
            ), "no unqualified argument passthrough"


async def test_pin_inv_ha_003_tool_cache_has_no_time_expiry():
    """Pins INV-HA-003: the facade's cached tool surface never expires by
    time — only explicit refresh or transport recovery rediscovers.

    Red case demonstrated: adding a TTL-based refresh to _call fails this
    test (the second session would be opened and the surface would change).
    """
    clock = [0.0]
    sessions = SessionSequence(
        FakeHaSession(tools=[action_tool("HassTurnOn")]),
        FakeHaSession(tools=[action_tool("HassLightSet")]),
    )
    facade = make_facade(sessions, monotonic=lambda: clock[0])
    await facade.start()
    try:
        clock[0] = 10**12
        await invoke_sdk_tool(
            facade.server_config, "HassTurnOn", {"name": "kitchen"},
        )
        assert facade.tool_names == ("HassTurnOn",)
        assert sessions.open_count == 1
    finally:
        await facade.aclose()


class AnyioSessionFactory:
    """#1400: a connection that holds an anyio task group, as the streamable
    HTTP client and ``ClientSession`` do — so a cancel scope entered in one
    task and exited in another misbehaves here exactly as it does live."""

    def __init__(self, session: FakeHaSession | None = None) -> None:
        self.entered = 0
        self.exited = 0
        self.session = session
        self.drop = asyncio.Event()

    def __call__(self):
        import anyio

        async def transport() -> None:
            await self.drop.wait()
            raise OSError("Home Assistant went away")

        @asynccontextmanager
        async def connection():
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(transport)
                self.entered += 1
                try:
                    yield (self.session
                           or FakeHaSession(tools=[action_tool("HassTurnOn")]))
                finally:
                    task_group.cancel_scope.cancel()
                    self.exited += 1

        return connection()

    def facade(self) -> HomeAssistantFacade:
        return HomeAssistantFacade(
            "http://ha/mcp",
            {"Authorization": "Bearer secret"},
            session_factory=self,
        )


async def _awaits_survive(count: int = 3) -> None:
    for _ in range(count):
        await asyncio.sleep(0)


async def test_close_from_another_task_leaves_the_opener_uncancelled(caplog):
    """#1400: opened by this task (as boot opens it in Casa's main task) and
    closed by another while this one waits — what Python 3.11's ``wait_for``
    does to the stop's close. Both return and this task keeps running.

    Base: CancelledError out of the wait, every later await cancelled."""
    import casa_core

    factory = AnyioSessionFactory()
    facade = factory.facade()
    await facade.start()
    await asyncio.create_task(casa_core._close_tina_ha_facade(facade))
    await _awaits_survive()
    assert (factory.entered, factory.exited) == (1, 1)
    assert "upstream close failed" not in caplog.text


async def test_close_after_the_opener_ended_exits_the_connection(caplog):
    """#1400: opened by a task that has since ended (a scheduled refresh) and
    closed from Casa's stop — the connection's own scope is exited cleanly.

    Base: "upstream close failed" — the scope cannot be exited here."""
    import casa_core

    factory = AnyioSessionFactory()
    facade = factory.facade()
    await asyncio.create_task(facade.start())
    await casa_core._close_tina_ha_facade(facade)
    await _awaits_survive()
    assert (factory.entered, factory.exited) == (1, 1)
    assert "upstream close failed" not in caplog.text


async def test_refresh_from_another_task_leaves_both_tasks_uncancelled():
    """#1400: a refresh task closes the boot connection and opens its own; the
    boot task and the later stop both keep running.

    Base: the boot task is cancelled while it awaits the refresh."""
    factory = AnyioSessionFactory()
    facade = factory.facade()
    await facade.start()
    await asyncio.create_task(facade.refresh())
    await _awaits_survive()
    await facade.aclose()
    await _awaits_survive()
    assert (factory.entered, factory.exited) == (2, 2)


class _UnansweredSession(FakeHaSession):
    """A call the server never answers: the transport died under it."""

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        await asyncio.Event().wait()


async def test_a_connection_that_drops_mid_call_fails_the_call(caplog):
    """#1400 r1 (Astra): the connection's transport fails while a tool call
    waits for its answer. The call reports Home Assistant unavailable and a
    reconnect is scheduled, instead of waiting forever for an answer that
    cannot come — and no other task is cancelled.

    Base: the waiting task is cancelled by the connection's task group."""
    session = _UnansweredSession(tools=[action_tool("HassTurnOn")])
    factory = AnyioSessionFactory(session)
    facade = factory.facade()
    await facade.start()
    call = asyncio.create_task(
        invoke_sdk_tool(facade.server_config, "HassTurnOn", {}))
    await wait_until(lambda: session.calls)
    factory.drop.set()
    payload = await asyncio.wait_for(call, 5)
    assert payload == {"content": [{"type": "text", "text": UNAVAILABLE_TEXT}],
                       "is_error": True}
    await _awaits_survive()
    await facade.aclose()
    assert factory.exited >= 1
