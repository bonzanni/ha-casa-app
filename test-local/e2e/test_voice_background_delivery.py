"""Real Casa ↔ HA integration background-delivery protocol acceptance."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from ha_integration_stubs import HA_STUB_EXPORTS, install, validate_exports


install()


def _integration_root() -> Path:
    configured = os.environ.get("CASA_HA_INTEGRATION_PATH")
    if configured is not None:
        candidate = Path(configured).expanduser()
        if not (candidate / "custom_components/casa/delivery.py").is_file():
            pytest.fail(
                "CASA_HA_INTEGRATION_PATH does not contain the background "
                "delivery integration",
                pytrace=False,
            )
        return candidate.resolve()
    candidates = [
        parent / "ha-casa-integration"
        for parent in Path(__file__).resolve().parents
    ]
    for candidate in candidates:
        if (candidate / "custom_components/casa/delivery.py").is_file():
            return candidate.resolve()
    pytest.fail(
        "ha-casa-integration checkout with background delivery is unavailable",
        pytrace=False,
    )


INTEGRATION_ROOT = _integration_root()
sys.path.insert(0, str(INTEGRATION_ROOT))
CASA_CODE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "casa/rootfs/opt/casa"
)
sys.path.insert(0, str(CASA_CODE_ROOT))

_conftest_spec = importlib.util.spec_from_file_location(
    "_casa_integration_stub_manifest",
    INTEGRATION_ROOT / "tests/conftest.py",
)
assert _conftest_spec is not None and _conftest_spec.loader is not None
_conftest = importlib.util.module_from_spec(_conftest_spec)
_conftest_spec.loader.exec_module(_conftest)
validate_exports()

from custom_components.casa.api import CasaApiClient  # noqa: E402
from custom_components.casa.delivery import (  # noqa: E402
    BackgroundDeliveryManager,
    SatelliteDirectory,
)

import tools  # noqa: E402
from agent import Agent  # noqa: E402
from bus import BusMessage, MessageBus, MessageType  # noqa: E402
from channels import ChannelManager  # noqa: E402
from channels.voice.channel import VoiceChannel  # noqa: E402
from channels.voice.delivery import VoiceDeliveryCoordinator  # noqa: E402
from config import (  # noqa: E402
    AgentConfig,
    CharacterConfig,
    DelegateEntry,
    MemoryConfig,
    ToolsConfig,
)
from job_registry import DeliveryState, ExecutionState, JobRegistry  # noqa: E402
from mcp_registry import McpServerRegistry  # noqa: E402
from session_registry import SessionRegistry  # noqa: E402
from specialist_limits import SpecialistLimiter  # noqa: E402
from specialist_registry import SpecialistRegistry  # noqa: E402


pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


async def _eventually(predicate, *, timeout: float = 3.5) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def _tool_payload(envelope: dict) -> dict:
    return json.loads(envelope["content"][0]["text"])


class _SpecialistRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.outputs: asyncio.Queue[tools.DelegatedOutput] = asyncio.Queue()

    async def __call__(
        self, cfg, task_text, context_text, resolution=None, output_format=None,
        tool_counts=None,
    ) -> tools.DelegatedOutput:
        self.calls.append({
            "task": task_text,
            "context": context_text,
            "output_format": output_format,
        })
        return await self.outputs.get()

    async def finish(
        self,
        *,
        spoken: str = "The ruling is no.",
        answer: str = "PRIVATE_ANSWER_CANARY",
        citation: str = "PRIVATE_CITATION_CANARY",
        sensitivity: str = "household",
        status: str = "answered",
        clarification: str = "",
    ) -> None:
        await self.outputs.put(tools.DelegatedOutput(
            text="PRIVATE_SPECIALIST_TEXT_CANARY",
            structured_output={
                "status": status,
                "spoken_summary": spoken,
                "answer": answer,
                "clarification": clarification,
                "citations": [citation],
                "assumptions": [],
                "provenance": {},
                "sensitivity": sensitivity,
                "delivery_ttl_s": 900,
            },
        ))


def _assistant_message(text: str) -> AssistantMessage:
    try:
        block = TextBlock(text=text)
    except TypeError:
        block = TextBlock(text)
    try:
        return AssistantMessage(content=[block], model="scripted-gary")
    except TypeError:
        message = AssistantMessage.__new__(AssistantMessage)
        message.content = [block]
        return message


def _result_message() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="gary-e2e-session",
        usage={"input_tokens": 1, "output_tokens": 1},
        result="",
    )


class _GarySdkClient:
    """Scripted SDK transport around the real pooled Gary Agent."""

    def __init__(self, options, factory) -> None:
        self.options = options
        self.factory = factory
        self.response = "Still listening."

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        origin = dict(tools._snapshot_origin())
        self.factory.queries.append({"prompt": prompt, "origin": origin})
        user_text = prompt.rsplit("\n\n", 1)[-1]
        if user_text.startswith("delegate:"):
            _, agent, task = user_text.split(":", 2)
            envelope = await tools.delegate_to_agent.handler({
                "agent": agent,
                "task": task,
                "context": "",
                "mode": "async",
            })
            payload = _tool_payload(envelope)
            self.factory.tool_results.append(payload)
            await self.factory.accepted.put(payload)
            self.response = "Got it; I will bring back the specialist's answer."
        elif user_text.startswith("continue:"):
            _, job_id, continuation = user_text.split(":", 2)
            envelope = await tools.continue_voice_job.handler({
                "job_id": job_id,
                "input": continuation,
            })
            payload = _tool_payload(envelope)
            self.factory.tool_results.append(payload)
            await self.factory.accepted.put(payload)
            self.response = "Got it; I will continue with the specialist."
        else:
            self.response = "Still listening."

    async def receive_response(self):
        assistant = _assistant_message(self.response)
        result = _result_message()
        self.factory.messages.extend((assistant, result))
        yield assistant
        yield result


class _GarySdkFactory:
    def __init__(self) -> None:
        self.clients: list[_GarySdkClient] = []
        self.queries: list[dict] = []
        self.messages: list[object] = []
        self.tool_results: list[dict] = []
        self.accepted: asyncio.Queue[dict] = asyncio.Queue()

    def __call__(self, options) -> _GarySdkClient:
        client = _GarySdkClient(options, self)
        self.clients.append(client)
        return client


class _GaryMemory:
    async def retain(self, *args, **kwargs):
        return None

    async def recall(self, *args, **kwargs):
        return ""

    async def recall_items(self, *args, **kwargs):
        # Task 11: typed recall — a genuine zero-hit is the empty tuple.
        return ()

    async def profile(self, *args, **kwargs):
        return ""


class _Stack:
    def __init__(self) -> None:
        self.announces: list[dict] = []

    async def submit(
        self,
        *,
        task: str = "Does this target?",
        device_id: str = "dev-k",
        agent: str = "judge",
    ) -> tuple[dict, float]:
        started = time.monotonic()
        frames = []
        async with asyncio.timeout(3.5):
            async for frame in self.api.stream_utterance(
                text=f"delegate:{agent}:{task}",
                agent_role="concierge",
                scope_id=device_id,
                utterance_id=f"submit-{time.monotonic_ns()}",
                context={"device_id": device_id},
            ):
                frames.append(frame)
        assert frames[-1].kind == "done", [
            (frame.kind, vars(frame)) for frame in frames
        ] + [(type(message).__name__,) for message in self.gary_sdk.messages]
        accepted = await asyncio.wait_for(self.gary_sdk.accepted.get(), timeout=1)
        return accepted, time.monotonic() - started

    async def continue_job(
        self,
        job_id: str,
        *,
        device_id: str = "dev-o",
        continuation: str = "Black Lotus",
    ) -> dict:
        frames = []
        async with asyncio.timeout(3.5):
            async for frame in self.api.stream_utterance(
                text=f"continue:{job_id}:{continuation}",
                agent_role="concierge",
                scope_id=device_id,
                utterance_id=f"continue-{time.monotonic_ns()}",
                context={"device_id": device_id},
            ):
                frames.append(frame.kind)
        assert frames[-1] == "done"
        return await asyncio.wait_for(self.gary_sdk.accepted.get(), timeout=1)

    async def second_utterance(self, utterance_id: str = "utterance-2") -> list[str]:
        frames = []
        async with asyncio.timeout(3.5):
            async for frame in self.api.stream_utterance(
                text="What else can I do?",
                agent_role="concierge",
                scope_id="dev-k",
                utterance_id=utterance_id,
                context={"device_id": "dev-k"},
            ):
                frames.append(frame.kind)
        return frames

    def gary_snapshot(self) -> dict:
        """Observable real-Agent/pool transcript state, excluding payloads."""
        return {
            "pool": self.gary._pool.stats().copy(),
            "queries": len(self.gary_sdk.queries),
            "messages": len(self.gary_sdk.messages),
            "tool_results": len(self.gary_sdk.tool_results),
        }

    async def reconnect(self) -> None:
        old_generation = self.api._ws_generation
        await self.api._ws.close()
        await _eventually(
            lambda: (
                self.api._ws_generation > old_generation
                and self.api.background_capable
            ),
        )

    async def close(self) -> None:
        async with asyncio.timeout(5):
            await self.manager.close()
            await self.api.close()
            await self.session.close()
            await self.coordinator.stop()
            await self.registry.close()
            self.bus_loop.cancel()
            await asyncio.gather(self.bus_loop, return_exceptions=True)
            await self.gary.aclose()
            await self.runner.cleanup()


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    assert HA_STUB_EXPORTS == _conftest.HA_STUB_EXPORTS
    result = _Stack()
    result.registry = JobRegistry(tmp_path / "jobs.json")
    await result.registry.load()
    result.specialists = SpecialistRegistry(
        str(tmp_path / "specialists"), job_registry=result.registry,
    )
    caller = AgentConfig(
        role="concierge",
        channels=["ha_voice"],
        model="claude-sonnet-4-6",
        system_prompt="You are Gary.",
        character=CharacterConfig(name="Gary"),
        tools=ToolsConfig(
            allowed=[
                "mcp__casa-framework__delegate_to_agent",
                "mcp__casa-framework__continue_voice_job",
            ],
            permission_mode="acceptEdits",
        ),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
    )
    caller.delegates = [
        DelegateEntry(agent="judge", purpose="rules", when="rules question"),
        DelegateEntry(agent="health", purpose="health", when="health question"),
    ]
    judge = AgentConfig(
        role="judge",
        character=CharacterConfig(name="Judge"),
        model="claude-sonnet-4-6",
    )
    health = AgentConfig(
        role="health",
        character=CharacterConfig(name="Health"),
        model="claude-sonnet-4-6",
    )
    result.specialist_runner = _SpecialistRunner()
    monkeypatch.setattr(tools, "_run_delegated_agent", result.specialist_runner)
    result.bus = MessageBus()
    result.channel_manager = ChannelManager()
    tools.init_tools(
        result.channel_manager, result.bus, result.specialists,
        agent_role_map={
            "concierge": caller,
            "judge": judge,
            "health": health,
        },
        specialist_limiter=SpecialistLimiter(max_global=4),
    )

    result.gary_sdk = _GarySdkFactory()
    monkeypatch.setattr(
        "sdk_client_pool._default_make_client", result.gary_sdk,
    )
    result.gary = Agent(
        config=caller,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=result.channel_manager,
        semantic_memory=_GaryMemory(),
    )
    result.bus.register("concierge", result.gary.handle_message)
    result.bus_loop = result.bus.start_agent_loop("concierge")
    result.coordinator = VoiceDeliveryCoordinator(
        result.registry,
        None,
        lease_s=15,
        renew_s=5,
    )
    result.integration_to_casa_frames = []
    real_coordinator_handle = result.coordinator.handle

    async def capture_integration_frame(endpoint, frame):
        result.integration_to_casa_frames.append(dict(frame))
        await real_coordinator_handle(endpoint, frame)

    result.coordinator.handle = capture_integration_frame
    channel = VoiceChannel(
        bus=result.bus,
        default_agent="concierge",
        webhook_secret="e2e-secret",
        sse_path="/api/converse",
        ws_path="/api/converse/ws",
        agent_configs={"concierge": caller},
        memory=AsyncMock(),
        idle_timeout=300,
        delivery_coordinator=result.coordinator,
    )
    result.channel = channel
    result.channel_manager.register(channel)
    result.coordinator._routes = channel.routes
    # Production wires the same route registry into tools via CasaRuntime.
    # This local equivalent makes launch freshness checks exercise the real
    # registry rather than trusting captured capabilities indefinitely.
    monkeypatch.setattr(
        tools,
        "_runtime",
        SimpleNamespace(voice_route_registry=channel.routes),
    )
    app = web.Application()
    channel.register_routes(app)
    result.runner = web.AppRunner(app)
    await result.runner.setup()
    site = web.TCPSite(result.runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    result.session = aiohttp.ClientSession()
    result.api = CasaApiClient(
        result.session, "127.0.0.1", port, "e2e-secret",
    )
    directory = SatelliteDirectory()
    directory.add("dev-k", "assist_satellite.kitchen")
    directory.add("dev-o", "assist_satellite.office")
    directory.set_state("dev-k", "processing", changed_at=time.time())
    directory.set_state("dev-o", "processing", changed_at=time.time())
    services = SimpleNamespace()
    result.announce_started = asyncio.Event()
    result.announce_release = asyncio.Event()
    result.announce_release.set()

    async def announce(domain, service, data, *, blocking):
        assert (domain, service, blocking) == (
            "assist_satellite", "announce", True,
        )
        result.announce_started.set()
        await result.announce_release.wait()
        result.announces.append(dict(data))

    services.async_call = announce
    hass = SimpleNamespace(services=services)
    result.directory = directory
    result.manager = BackgroundDeliveryManager(
        hass,
        result.api,
        route_id="entry-1",
        directory=directory,
        idle_stability_ms=750,
    )
    result.inbound_job_frames = []

    async def capture_job_frame(frame):
        result.inbound_job_frames.append(dict(frame))
        await result.manager.handle_frame(frame)

    await result.api.start_background(
        route_id="entry-1",
        agent_role="concierge",
        job_handler=capture_job_frame,
    )
    await _eventually(lambda: result.api.background_capable)
    await result.coordinator.start()
    try:
        yield result
    finally:
        await result.close()


async def test_busy_completion_waits_then_announces_without_reentering_gary(stack):
    pending, elapsed = await stack.submit()
    assert pending["status"] == "pending"
    assert elapsed <= 3.5

    assert await stack.second_utterance() == ["block", "done"]
    before_completion = stack.gary_snapshot()
    await stack.specialist_runner.finish()
    job = await asyncio.wait_for(
        stack.registry.wait_for_terminal(pending["job_id"]), timeout=1,
    )
    await stack.coordinator.sweep_once()
    await _eventually(lambda: stack.manager.attempt_count_for_test == 1)
    assert stack.registry.get(pending["job_id"]).delivery_state is (
        DeliveryState.CLAIMED
    )
    assert stack.announces == []

    stack.directory.set_state(
        "dev-k", "idle", changed_at=time.time() - 1,
    )
    await _eventually(
        lambda: (
            stack.registry.get(pending["job_id"]).delivery_state
            is DeliveryState.DELIVERED
        ),
    )

    assert [call["message"] for call in stack.announces] == ["The ruling is no."]
    assert stack.gary_snapshot() == before_completion
    assert not any(
        message.type is MessageType.NOTIFICATION
        for message in stack.bus.get_log()
    )


async def test_immediate_idle_completion_announces_without_extra_transition(stack):
    stack.directory.set_state("dev-k", "idle", changed_at=time.time() - 1)
    pending, _ = await stack.submit()
    await stack.specialist_runner.finish(spoken="Immediate answer.")
    await asyncio.wait_for(
        stack.registry.wait_for_terminal(pending["job_id"]), timeout=1,
    )
    await stack.coordinator.sweep_once()

    await _eventually(
        lambda: stack.registry.get(pending["job_id"]).delivery_state
        is DeliveryState.DELIVERED,
    )
    assert [call["message"] for call in stack.announces] == ["Immediate answer."]


async def test_delivered_waits_for_blocking_announce_to_return(stack):
    stack.directory.set_state("dev-k", "idle", changed_at=time.time() - 1)
    stack.announce_release.clear()
    pending, _ = await stack.submit()
    await stack.specialist_runner.finish(spoken="Blocking answer.")
    await asyncio.wait_for(
        stack.registry.wait_for_terminal(pending["job_id"]), timeout=1,
    )
    await stack.coordinator.sweep_once()
    await asyncio.wait_for(stack.announce_started.wait(), timeout=1)
    await _eventually(
        lambda: stack.registry.get(pending["job_id"]).delivery_state
        is DeliveryState.PLAYING,
    )

    assert stack.registry.get(pending["job_id"]).delivery_state is (
        DeliveryState.PLAYING
    )
    assert stack.announces == []

    stack.announce_release.set()
    await _eventually(
        lambda: stack.registry.get(pending["job_id"]).delivery_state
        is DeliveryState.DELIVERED,
    )
    assert [call["message"] for call in stack.announces] == ["Blocking answer."]


async def test_cancel_running_never_announces(stack):
    pending, _ = await stack.submit()
    job = stack.registry.get(pending["job_id"])
    await stack.registry.request_cancel(pending["job_id"], actor={
        "creator_peer": job.creator_peer,
        "creator_user_id": job.creator_user_id,
        "scope_id": job.scope_id,
        "job_control_id": job.job_control_id,
    })
    await _eventually(
        lambda: stack.registry.get(pending["job_id"]).execution_state
        is ExecutionState.CANCELLED,
    )
    await stack.coordinator.sweep_once()
    assert stack.announces == []


@pytest.mark.parametrize("phase", ["ready", "claimed"])
async def test_cancel_before_playback_never_announces(stack, phase):
    pending, _ = await stack.submit()
    if phase == "ready":
        await stack.coordinator.stop()
    await stack.specialist_runner.finish()
    await asyncio.wait_for(
        stack.registry.wait_for_terminal(pending["job_id"]), timeout=1,
    )
    if phase == "claimed":
        await stack.coordinator.sweep_once()
        await _eventually(
            lambda: (
                stack.registry.get(pending["job_id"]).delivery_state
                is DeliveryState.CLAIMED
            ),
        )
    else:
        assert stack.registry.get(pending["job_id"]).delivery_state is (
            DeliveryState.READY
        )
    job = stack.registry.get(pending["job_id"])
    await stack.registry.request_cancel(pending["job_id"], actor={
        "creator_peer": job.creator_peer,
        "creator_user_id": job.creator_user_id,
        "scope_id": job.scope_id,
        "job_control_id": job.job_control_id,
    })
    await stack.coordinator.sweep_once()
    await _eventually(
        lambda: stack.registry.get(pending["job_id"]).delivery_state
        is DeliveryState.CANCELLED,
    )
    assert stack.announces == []


async def test_ws_reconnect_preserves_background_delivery(stack):
    await stack.reconnect()
    stack.directory.set_state("dev-k", "idle", changed_at=time.time() - 1)
    pending, _ = await stack.submit()
    await stack.specialist_runner.finish(spoken="After reconnect.")
    await asyncio.wait_for(
        stack.registry.wait_for_terminal(pending["job_id"]), timeout=1,
    )
    await stack.coordinator.sweep_once()

    await _eventually(
        lambda: stack.registry.get(pending["job_id"]).delivery_state
        is DeliveryState.DELIVERED,
    )
    assert [call["message"] for call in stack.announces] == ["After reconnect."]


async def test_lost_delivered_ack_reoffers_without_replay_or_mapping(stack):
    stack.directory.set_state("dev-k", "idle", changed_at=time.time() - 1)
    outbound: list[dict] = []
    dropped = False
    real_send = stack.api.send_job_frame

    async def drop_first_delivered(frame):
        nonlocal dropped
        outbound.append(dict(frame))
        if frame.get("type") == "job_delivered" and not dropped:
            dropped = True
            return
        await real_send(frame)

    stack.api.send_job_frame = drop_first_delivered
    pending, _ = await stack.submit()
    await stack.specialist_runner.finish(spoken="Exactly once audio.")
    await asyncio.wait_for(
        stack.registry.wait_for_terminal(pending["job_id"]), timeout=1,
    )
    await stack.coordinator.sweep_once()
    await _eventually(
        lambda: pending["job_id"] in stack.manager.delivered_ids_for_test,
    )
    assert stack.registry.get(pending["job_id"]).delivery_state is (
        DeliveryState.PLAYING
    )
    assert [call["message"] for call in stack.announces] == [
        "Exactly once audio.",
    ]

    stack.directory.remove("assist_satellite.kitchen")
    stack.registry._clock = lambda: time.time() + 16
    await stack.coordinator.sweep_once()
    await _eventually(
        lambda: stack.registry.get(pending["job_id"]).delivery_state
        is DeliveryState.DELIVERED,
    )

    ready = [
        frame for frame in stack.inbound_job_frames
        if frame.get("type") == "job_ready"
        and frame.get("job_id") == pending["job_id"]
    ]
    assert len(ready) == 2
    assert ready[0]["delivery_attempt_id"] != ready[1]["delivery_attempt_id"]
    assert [frame["type"] for frame in outbound] == [
        "job_claimed",
        "job_delivery_start",
        "job_playback_started",
        "job_delivered",
        "job_claimed",
        "job_delivered",
    ]
    assert [call["message"] for call in stack.announces] == [
        "Exactly once audio.",
    ]


async def test_private_result_canaries_never_reach_wire_or_logs(stack, caplog):
    summary = "PRIVATE_E2E_SUMMARY_CANARY"
    answer = "PRIVATE_E2E_ANSWER_CANARY"
    citation = "PRIVATE_E2E_CITATION_CANARY"
    stack.directory.set_state("dev-k", "idle", changed_at=time.time() - 1)
    pending, _ = await stack.submit()

    with caplog.at_level("DEBUG"):
        await stack.specialist_runner.finish(
            spoken=summary,
            answer=answer,
            citation=citation,
            sensitivity="private",
        )
        await asyncio.wait_for(
            stack.registry.wait_for_terminal(pending["job_id"]), timeout=1,
        )
        await stack.coordinator.sweep_once()
        await _eventually(
            lambda: stack.registry.get(pending["job_id"]).delivery_state
            is DeliveryState.DELIVERED,
        )

    assert [call["message"] for call in stack.announces] == [
        "Your result is ready; ask me for the details.",
    ]
    serialized_wire = json.dumps(stack.inbound_job_frames)
    for canary in (summary, answer, citation):
        assert canary not in json.dumps(pending)
        assert canary not in serialized_wire
        assert canary not in caplog.text


async def test_real_other_satellite_continuation_reanchors_child_only(stack):
    pending, _ = await stack.submit(device_id="dev-k")
    await stack.specialist_runner.finish(
        spoken="Which card do you mean?",
        status="needs_clarification",
        clarification="Which card do you mean?",
    )
    parent = await asyncio.wait_for(
        stack.registry.wait_for_terminal(pending["job_id"]), timeout=1,
    )
    await asyncio.wait_for(
        stack.registry.wait_for_runtime_release(parent.id), timeout=1,
    )

    continued = await stack.continue_job(parent.id, device_id="dev-o")

    assert continued["status"] == "pending"
    child = stack.registry.get(continued["job_id"])
    historical_parent = stack.registry.get(parent.id)
    assert child.parent_job_id == parent.id
    assert child.scope_id == "dev-o"
    assert child.origin_device_id == "dev-o"
    assert child.job_control_id == "entry-1"
    assert historical_parent.scope_id == "dev-k"
    assert historical_parent.origin_device_id == "dev-k"
    assert historical_parent.job_control_id == "entry-1"


async def test_real_other_satellite_detail_reanchors_prompted_child_only(stack):
    pending, _ = await stack.submit(device_id="dev-k")
    await stack.specialist_runner.finish(spoken="Stored answer.")
    parent = await asyncio.wait_for(
        stack.registry.wait_for_terminal(pending["job_id"]), timeout=1,
    )
    await asyncio.wait_for(
        stack.registry.wait_for_runtime_release(parent.id), timeout=1,
    )
    specialist_calls = len(stack.specialist_runner.calls)

    detail = await stack.continue_job(
        parent.id,
        device_id="dev-o",
        continuation="Tell me the stored details",
    )

    child = stack.registry.get(detail["job_id"])
    historical_parent = stack.registry.get(parent.id)
    assert child.prompted_delivery is True
    assert child.parent_job_id == parent.id
    assert child.scope_id == "dev-o"
    assert child.origin_device_id == "dev-o"
    assert child.job_control_id == "entry-1"
    assert historical_parent.scope_id == "dev-k"
    assert historical_parent.origin_device_id == "dev-k"
    assert len(stack.specialist_runner.calls) == specialist_calls


async def test_real_reader_writer_interleave_keeps_devices_and_frames_isolated(
    stack, monkeypatch,
):
    from custom_components.casa import delivery as integration_delivery

    monkeypatch.setattr(integration_delivery, "_LEASE_RENEW_SECONDS", 0.05)
    client_ws = stack.api._ws
    server_route = stack.channel.routes.get_connected("entry-1")
    assert client_ws is not None and server_route is not None
    server_ws = server_route.connection._ws
    send_activity = {
        "client": 0, "client_max": 0,
        "server": 0, "server_max": 0,
    }

    async def instrument(name, send, *args, **kwargs):
        send_activity[name] += 1
        send_activity[f"{name}_max"] = max(
            send_activity[f"{name}_max"], send_activity[name],
        )
        await asyncio.sleep(0)
        try:
            return await send(*args, **kwargs)
        finally:
            send_activity[name] -= 1

    client_send = client_ws.send_json
    server_send = server_ws.send_json

    async def instrument_client(*args, **kwargs):
        return await instrument("client", client_send, *args, **kwargs)

    async def instrument_server(*args, **kwargs):
        return await instrument("server", server_send, *args, **kwargs)

    monkeypatch.setattr(client_ws, "send_json", instrument_client)
    monkeypatch.setattr(server_ws, "send_json", instrument_server)

    kitchen, _ = await stack.submit(task="Kitchen ruling", device_id="dev-k")
    office, _ = await stack.submit(
        task="Office health question",
        device_id="dev-o",
        agent="health",
    )
    await stack.specialist_runner.finish(spoken="Kitchen answer.")
    await stack.specialist_runner.finish(spoken="Office answer.")
    await asyncio.wait_for(asyncio.gather(
        stack.registry.wait_for_terminal(kitchen["job_id"]),
        stack.registry.wait_for_terminal(office["job_id"]),
    ), timeout=1)
    await stack.coordinator.sweep_once()
    await _eventually(lambda: all(
        stack.registry.get(job_id).delivery_state is DeliveryState.CLAIMED
        for job_id in (kitchen["job_id"], office["job_id"])
    ))
    initial_office_lease = stack.registry.get(office["job_id"]).lease_until

    utterance_one = asyncio.create_task(stack.second_utterance("utterance-a"))
    utterance_two = asyncio.create_task(stack.second_utterance("utterance-b"))
    await asyncio.sleep(0.12)
    assert stack.registry.get(office["job_id"]).lease_until > initial_office_lease

    kitchen_job = stack.registry.get(kitchen["job_id"])
    await stack.registry.request_cancel(kitchen_job.id, actor={
        "creator_peer": kitchen_job.creator_peer,
        "creator_user_id": kitchen_job.creator_user_id,
        "scope_id": kitchen_job.scope_id,
        "job_control_id": kitchen_job.job_control_id,
    })
    await stack.coordinator.sweep_once()
    stack.directory.set_state("dev-o", "idle", changed_at=time.time() - 1)

    frames_one, frames_two = await asyncio.wait_for(
        asyncio.gather(utterance_one, utterance_two), timeout=3.5,
    )
    await _eventually(
        lambda: (
            stack.registry.get(office["job_id"]).delivery_state
            is DeliveryState.DELIVERED
        ),
    )
    await _eventually(
        lambda: (
            stack.registry.get(kitchen["job_id"]).delivery_state
            is DeliveryState.CANCELLED
        ),
    )

    assert frames_one == ["block", "done"]
    assert frames_two == ["block", "done"]
    assert [call["message"] for call in stack.announces] == ["Office answer."]
    assert send_activity["client_max"] == 1
    assert send_activity["server_max"] == 1


async def test_disconnect_completion_reconnect_preserves_fifo_and_accounting(
    stack, monkeypatch,
):
    from custom_components.casa import delivery as integration_delivery

    monkeypatch.setattr(integration_delivery, "_LEASE_RENEW_SECONDS", 0.05)
    client_type = type(stack.api._ws)
    server_route = stack.channel.routes.get_connected("entry-1")
    assert server_route is not None
    server_type = type(server_route.connection._ws)
    original_client_send = client_type.send_json
    original_server_send = server_type.send_json
    active = {"client": 0, "server": 0}
    maximum = {"client": 0, "server": 0}
    client_job_sent: list[dict] = []
    server_job_sent: list[dict] = []
    wire_events: list[tuple[str, dict]] = []

    async def instrument(name, original, socket, frame, *args, **kwargs):
        active[name] += 1
        maximum[name] = max(maximum[name], active[name])
        await asyncio.sleep(0)
        try:
            if isinstance(frame, dict) and str(frame.get("type", "")).startswith(
                "job_"
            ):
                copied = dict(frame)
                (client_job_sent if name == "client" else server_job_sent).append(
                    copied
                )
                wire_events.append((name, copied))
            return await original(socket, frame, *args, **kwargs)
        finally:
            active[name] -= 1

    async def client_send(socket, frame, *args, **kwargs):
        return await instrument(
            "client", original_client_send, socket, frame, *args, **kwargs,
        )

    async def server_send(socket, frame, *args, **kwargs):
        return await instrument(
            "server", original_server_send, socket, frame, *args, **kwargs,
        )

    monkeypatch.setattr(client_type, "send_json", client_send)
    monkeypatch.setattr(server_type, "send_json", server_send)

    first, _ = await stack.submit(
        task="First kitchen ruling", device_id="dev-k", agent="judge",
    )
    second, _ = await stack.submit(
        task="Second kitchen health", device_id="dev-k", agent="health",
    )
    other, _ = await stack.submit(
        task="Office ruling", device_id="dev-o", agent="judge",
    )
    await _eventually(lambda: len(stack.specialist_runner.calls) == 3)

    reconnect_gate = asyncio.Event()
    real_ensure_ws = stack.api._ensure_ws

    async def gated_ensure_ws():
        current = stack.api._ws
        if current is None or current.closed:
            await reconnect_gate.wait()
        return await real_ensure_ws()

    monkeypatch.setattr(stack.api, "_ensure_ws", gated_ensure_ws)
    old_generation = stack.api._ws_generation
    await stack.api._ws.close()
    await _eventually(lambda: not stack.api.background_capable)

    await stack.specialist_runner.finish(spoken="First kitchen answer.")
    await stack.specialist_runner.finish(spoken="Second kitchen answer.")
    await stack.specialist_runner.finish(spoken="Office answer.")
    await asyncio.wait_for(asyncio.gather(*(
        stack.registry.wait_for_terminal(payload["job_id"])
        for payload in (first, second, other)
    )), timeout=1)
    await stack.coordinator.sweep_once()
    assert server_job_sent == []
    assert stack.announces == []

    reconnect_gate.set()
    await _eventually(lambda: (
        stack.api._ws_generation > old_generation
        and stack.api.background_capable
    ))
    await _eventually(lambda: all(
        stack.registry.get(payload["job_id"]).delivery_state
        is DeliveryState.CLAIMED
        for payload in (first, other)
    ))
    assert stack.registry.get(second["job_id"]).delivery_state is (
        DeliveryState.READY
    )
    assert not any(
        frame.get("type") == "job_ready"
        and frame.get("job_id") == second["job_id"]
        for frame in server_job_sent
    )
    await _eventually(lambda: sum(
        frame.get("type") == "job_claim_renew"
        for frame in client_job_sent
    ) >= 2)

    stack.directory.set_state("dev-o", "idle", changed_at=time.time() - 1)
    await _eventually(
        lambda: stack.registry.get(other["job_id"]).delivery_state
        is DeliveryState.DELIVERED,
    )
    assert stack.registry.get(first["job_id"]).delivery_state is (
        DeliveryState.CLAIMED
    )

    stack.directory.set_state("dev-k", "idle", changed_at=time.time() - 1)
    await _eventually(lambda: all(
        stack.registry.get(payload["job_id"]).delivery_state
        is DeliveryState.DELIVERED
        for payload in (first, second, other)
    ))

    assert [call["message"] for call in stack.announces] == [
        "Office answer.",
        "First kitchen answer.",
        "Second kitchen answer.",
    ]
    assert maximum == {"client": 1, "server": 1}
    assert server_job_sent == stack.inbound_job_frames
    assert client_job_sent == stack.integration_to_casa_frames

    ready_pairs = {
        (frame["job_id"], frame["delivery_attempt_id"])
        for frame in server_job_sent
        if frame["type"] == "job_ready"
    }
    assert len(ready_pairs) == 3
    for frame in (*server_job_sent, *client_job_sent):
        assert (frame["job_id"], frame["delivery_attempt_id"]) in ready_pairs

    first_delivered_index = next(
        index for index, (direction, frame) in enumerate(wire_events)
        if direction == "client"
        and frame["type"] == "job_delivered"
        and frame["job_id"] == first["job_id"]
    )
    second_ready_index = next(
        index for index, (direction, frame) in enumerate(wire_events)
        if direction == "server"
        and frame["type"] == "job_ready"
        and frame["job_id"] == second["job_id"]
    )
    assert second_ready_index > first_delivered_index
