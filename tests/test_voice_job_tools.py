"""Metadata-only voice job tools and asynchronous voice acceptance.

These tests deliberately use the real durable JobRegistry and specialist
limiter.  Only the external specialist SDK turn is controlled, so lifecycle,
authorization, ambiguity, cancellation, and permit behavior remain real.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

import agent as agent_mod
import tools
from bus import MessageBus
from channels import ChannelManager
from channels.voice.channel import VoiceChannel
from channels.voice.routes import VoiceRouteRegistry, VoiceWsConnection
from config import AgentConfig, CharacterConfig, DelegateEntry
from job_registry import (
    DeliveryState,
    ExecutionState,
    HandoffState,
    JobFailure,
    JobRegistry,
    VoiceJob,
)
from personality_types import SpeakerProvenance
from specialist_limits import SpecialistLimiter
from specialist_registry import SpecialistRegistry

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT


pytestmark = pytest.mark.unit


def _caller_cfg() -> AgentConfig:
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role="concierge")
    cfg.delegates = [
        DelegateEntry(agent="judge", purpose="rules", when="rules question"),
        DelegateEntry(agent="health", purpose="health", when="health question"),
    ]
    return cfg


def _specialist_cfg(role: str, display_name: str) -> AgentConfig:
    return AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role=role,
        character=CharacterConfig(name=display_name),
        model="claude-sonnet-4-6",
    )


def voice_origin(**overrides) -> dict:
    origin = {
        "role": "concierge",
        "execution_role": "concierge",
        "channel": "voice",
        "chat_id": "scope-1",
        "user_id": "user-1",
        "cid": "turn-1",
        "user_text": "Does this target?",
        "voice_transport": "ws",
        "voice_route_id": "entry-1",
        "voice_route_capabilities": frozenset({
            "background_jobs", "endpoint_delivery", "voice_handoff",
        }),
        "origin_device_id": "device-kitchen",
        "voice_job_control_id": "entry-1",
        # Route protocol 3 (#233/#224): the per-utterance endpoint offer is
        # what authorizes a deferred answer now — route capabilities describe
        # the shared socket, not the device that asked.
        "voice_delivery_offer": {
            "modality": "audio", "receipt": "playback_complete",
        },
    }
    origin.update(overrides)
    return origin


def _structured_result(**overrides) -> dict:
    return {
        "status": "answered",
        "spoken_summary": "The ruling is no.",
        "answer": "No, because it does not target.",
        "clarification": "",
        "citations": ["CR 115.1"],
        "assumptions": [],
        "provenance": {},
        "sensitivity": "household",
        "delivery_ttl_s": 900,
        **overrides,
    }


def tool_payload(envelope: dict) -> dict:
    return json.loads(envelope["content"][0]["text"])


async def _call(tool, origin: dict, args: dict) -> dict:
    token = agent_mod.origin_var.set(origin)
    try:
        return await tool.handler(args)
    finally:
        agent_mod.origin_var.reset(token)


class _ControlledRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.started = asyncio.Event()
        self.outputs: asyncio.Queue[tools.DelegatedOutput | BaseException] = (
            asyncio.Queue()
        )

    async def __call__(
        self, cfg, task_text, context_text, resolution=None, output_format=None,
    ) -> tools.DelegatedOutput:
        self.calls.append({
            "cfg": cfg,
            "task": task_text,
            "context": context_text,
            "resolution": resolution,
            "output_format": output_format,
        })
        self.started.set()
        output = await self.outputs.get()
        if isinstance(output, BaseException):
            raise output
        return output

    async def finish(self, **overrides) -> None:
        await self.outputs.put(tools.DelegatedOutput(
            text=overrides.pop("text", "PRIVATE_RESULT_CANARY"),
            structured_output=_structured_result(**overrides),
        ))

    async def fail(self, exc: BaseException) -> None:
        await self.outputs.put(exc)


class _Reservation:
    def __init__(self) -> None:
        self.reserved = 0
        self.released = 0
        self.committed: list[VoiceJob] = []

    def reserve(self) -> None:
        self.reserved += 1

    def release(self) -> None:
        self.released += 1

    def commit(self, job: VoiceJob) -> None:
        self.committed.append(job)


class ToolEnv:
    def __init__(
        self,
        registry: JobRegistry,
        specialist_registry: SpecialistRegistry,
        limiter: SpecialistLimiter,
        runner: _ControlledRunner,
    ) -> None:
        self.job_registry = registry
        self.specialist_registry = specialist_registry
        self.limiter = limiter
        self.runner = runner

    async def invoke_delegate(
        self, origin: dict | None = None, *, mode: str = "async",
        agent: str = "judge", task: str = "Does this target?", context: str = "",
    ) -> dict:
        return await _call(
            tools.delegate_to_agent,
            origin or voice_origin(),
            {"agent": agent, "task": task, "context": context, "mode": mode},
        )

    async def add_job(self, job_id: str, **changes) -> VoiceJob:
        sequence = len(self.job_registry.all()) + 1
        base = VoiceJob(
            id=job_id,
            parent_job_id=None,
            creating_speaker=SpeakerProvenance(speaker_kind="system"),
            executing_speaker=SpeakerProvenance(speaker_kind="system"),
            creating_role="concierge",
            specialist_role="judge",
            specialist_display_name="Judge",
            creator_peer="voice",
            creator_user_id="user-1",
            scope_id="scope-1",
            origin_route_id="entry-1",
            origin_device_id="device-kitchen",
            task="PRIVATE_TASK_CANARY",
            context="PRIVATE_CONTEXT_CANARY",
            created_at=time.time(),
            started_at=time.time(),
            terminal_at=None,
            expires_at=None,
            execution_state=ExecutionState.RUNNING,
            delivery_state=DeliveryState.NONE,
            result=None,
            failure=None,
            awaiting_input=False,
            continuable_until=None,
            delivery_sequence=sequence,
            delivery_attempt_id=None,
            lease_until=None,
            cancel_pending=False,
        )
        job = replace(base, **changes)
        await self.job_registry.create(job)
        return job


@pytest.fixture
async def tool_env(tmp_path, monkeypatch):
    registry = JobRegistry(tmp_path / "jobs.json")
    await registry.load()
    specialist_registry = SpecialistRegistry(
        str(tmp_path / "specialists"), job_registry=registry,
    )
    limiter = SpecialistLimiter(max_global=4)
    runner = _ControlledRunner()
    monkeypatch.setattr(tools, "_run_delegated_agent", runner)
    tools.init_tools(
        ChannelManager(), MessageBus(), specialist_registry,
        agent_role_map={
            "concierge": _caller_cfg(),
            "judge": _specialist_cfg("judge", "Judge"),
            "health": _specialist_cfg("health", "Health"),
        },
        specialist_limiter=limiter,
    )
    env = ToolEnv(registry, specialist_registry, limiter, runner)
    try:
        yield env
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_voice_async_accepts_and_returns_only_opaque_metadata(tool_env):
    result = await tool_env.invoke_delegate()
    payload = tool_payload(result)

    assert payload == {
        "status": "pending",
        "job_id": payload["job_id"],
        "specialist_display_name": "Judge",
    }
    assert "task" not in payload and "text" not in payload
    jobs = tool_env.job_registry.all()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.id == payload["job_id"]
    assert job.execution_state is ExecutionState.RUNNING
    assert job.origin_route_id == "entry-1"
    assert job.origin_device_id == "device-kitchen"
    assert job.task == "Does this target?"
    assert tool_env.limiter.in_flight == 1


@pytest.mark.asyncio
async def test_async_job_falls_back_to_system_speakers_when_unbound(tool_env):
    """Neither the caller's origin nor the target specialist's config carry
    an activated speaker_provenance (Plan 1's baseline) — both snapshots
    must be the explicit unattributed system identity, never fabricated
    from the bare creating_role/specialist_role strings."""
    payload = tool_payload(await tool_env.invoke_delegate())
    job = tool_env.job_registry.get(payload["job_id"])
    assert job.creating_speaker == SpeakerProvenance(speaker_kind="system")
    assert job.executing_speaker == SpeakerProvenance(speaker_kind="system")


@pytest.mark.asyncio
async def test_async_job_persists_creating_and_executing_speaker_snapshots(
    tool_env,
):
    """A real caller identity on origin["speaker_provenance"] (Task 10 Step
    7's origin_var wiring) becomes creating_speaker; the target specialist's
    own AgentConfig.speaker_provenance becomes executing_speaker."""
    caller_provenance = SpeakerProvenance(
        speaker_kind="resident", role_id="resident:concierge",
        persona_id="casa/gary", persona_version="0.1.0",
        display_name="Gary", binding_digest="sha256:" + "a" * 64,
    )
    judge_provenance = SpeakerProvenance(
        speaker_kind="specialist", role_id="specialist:judge",
        persona_id="casa/judge", persona_version="0.1.0",
        display_name="Judge", binding_digest="sha256:" + "b" * 64,
    )
    tools._agent_role_map["judge"].speaker_provenance = judge_provenance

    origin = voice_origin(speaker_provenance=caller_provenance)
    payload = tool_payload(await tool_env.invoke_delegate(origin))
    job = tool_env.job_registry.get(payload["job_id"])
    assert job.creating_speaker == caller_provenance
    assert job.executing_speaker == judge_provenance


@pytest.mark.asyncio
async def test_handoff_commit_follows_durable_pending_latch(tool_env):
    reservation = _Reservation()
    origin = voice_origin(_voice_handoff_reservation=reservation)

    payload = tool_payload(await tool_env.invoke_delegate(origin, mode="sync"))
    job = tool_env.job_registry.get(payload["job_id"])

    assert job.handoff_state is HandoffState.PENDING
    assert job.handoff_id is not None
    assert reservation.reserved == 1
    assert reservation.released == 0
    assert reservation.committed == [job]


@pytest.mark.asyncio
async def test_channel_handoff_commits_real_job_before_cancelling_outer_request(
    tool_env, monkeypatch,
):
    """Channel, real tool persistence, and registry share one handoff seam."""
    gate_entered = asyncio.Event()
    release_gate = asyncio.Event()
    outer_cancelled = asyncio.Event()
    bus = MessageBus()

    class _Ws:
        voice_route_id = "entry-1"
        voice_route_capabilities = frozenset({
            "background_jobs", "endpoint_delivery", "voice_handoff",
        })
        voice_job_control_id = "entry-1"

        def __init__(self) -> None:
            self.frames: list[dict] = []

        async def send_json(self, frame: dict) -> None:
            self.frames.append(frame)

    async def _prelaunch(_agent, origin, mode, *_args):
        assert mode == "async"
        assert origin["_voice_handoff_reservation"].held is True
        gate_entered.set()
        await release_gate.wait()
        return "judge", _specialist_cfg("judge", "Judge"), None, None, None

    async def _concierge(msg):
        reservation = msg.context["_voice_handoff_reservation"]
        origin = voice_origin(
            _voice_handoff_reservation=reservation,
            voice_deadline=msg.context["_voice_deadline"],
        )
        token = agent_mod.origin_var.set(origin)
        try:
            await tools.delegate_to_agent.handler({
                "agent": "judge", "task": "durable task", "context": "",
                "mode": "sync",
            })
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            outer_cancelled.set()
            raise
        finally:
            agent_mod.origin_var.reset(token)

    monkeypatch.setattr(tools, "_prelaunch", _prelaunch)
    monkeypatch.setattr(tools, "deferred_delivery_available", lambda _origin: True)
    bus.register("concierge", _concierge)
    loop = asyncio.create_task(bus.run_agent_loop("concierge"))
    concierge_cfg = _caller_cfg()
    concierge_cfg.channels = ["ha_voice"]
    channel = VoiceChannel(
        bus=bus, default_agent="concierge", webhook_secret="",
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={"concierge": concierge_cfg}, memory=None,
        idle_timeout=300,
    )
    ws = _Ws()
    turn = asyncio.create_task(channel._run_ws_utterance(
        ws,
        {"agent_role": "concierge", "text": "ask judge", "device_id": "kitchen"},
        "utterance-1", asyncio.get_running_loop().time() + 20,
    ))
    await gate_entered.wait()
    assert ws.frames == []
    release_gate.set()
    await turn

    assert [frame["type"] for frame in ws.frames] == ["handoff"]
    job = tool_env.job_registry.all()[0]
    assert job.handoff_state is HandoffState.PENDING
    assert job.handoff_id == ws.frames[0]["handoff_id"]
    assert outer_cancelled.is_set()
    specialist_task = tool_env.job_registry._tasks[job.id]
    assert tool_env.job_registry.owns_task(job.id, specialist_task)
    assert not specialist_task.done()
    specialist_task.cancel()
    await asyncio.gather(specialist_task, return_exceptions=True)
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)


@pytest.mark.asyncio
async def test_voice_async_failure_persists_only_a_safe_ready_envelope(tool_env):
    accepted = tool_payload(await tool_env.invoke_delegate())
    job_id = accepted["job_id"]
    await tool_env.runner.fail(RuntimeError("PRIVATE_FAILURE_CANARY"))
    job = await asyncio.wait_for(
        tool_env.job_registry.wait_for_terminal(job_id), timeout=1,
    )
    await asyncio.wait_for(
        tool_env.job_registry.wait_for_runtime_release(job_id), timeout=1,
    )
    assert job.execution_state is ExecutionState.FAILED
    assert job.delivery_state is DeliveryState.READY
    assert job.result is None
    assert job.failure.kind == "unknown"
    assert job.failure.message == "Specialist could not complete the voice job."
    assert "PRIVATE_FAILURE_CANARY" not in json.dumps({
        "kind": job.failure.kind,
        "message": job.failure.message,
    })


@pytest.mark.asyncio
async def test_registry_owns_permit_until_terminal_persistence_and_close_waits(
    tool_env, monkeypatch,
):
    entered = asyncio.Event()
    release = asyncio.Event()
    real_finish = tool_env.job_registry.finish_voice_result

    async def blocked_finish(*args, **kwargs):
        entered.set()
        await release.wait()
        return await real_finish(*args, **kwargs)

    monkeypatch.setattr(
        tool_env.job_registry, "finish_voice_result", blocked_finish,
    )
    accepted = tool_payload(await tool_env.invoke_delegate())
    await tool_env.runner.finish()
    await entered.wait()

    close_task = asyncio.create_task(tool_env.job_registry.close())
    await asyncio.sleep(0)
    assert close_task.done() is False
    assert tool_env.limiter.in_flight == 1

    release.set()
    await close_task
    job = tool_env.job_registry.get(accepted["job_id"])
    assert job.execution_state is ExecutionState.SUCCEEDED
    assert job.delivery_state is DeliveryState.READY
    assert tool_env.limiter.in_flight == 0


@pytest.mark.asyncio
async def test_terminal_write_failure_uses_safe_fallback_without_private_log(
    tool_env, monkeypatch, caplog,
):
    async def fail_finish(*_args, **_kwargs):
        raise OSError("PRIVATE_PERSISTENCE_CANARY")

    monkeypatch.setattr(
        tool_env.job_registry, "finish_voice_result", fail_finish,
    )
    accepted = tool_payload(await tool_env.invoke_delegate())
    job_id = accepted["job_id"]
    await tool_env.runner.finish()
    job = await asyncio.wait_for(
        tool_env.job_registry.wait_for_terminal(job_id), timeout=1,
    )
    await asyncio.wait_for(
        tool_env.job_registry.wait_for_runtime_release(job_id), timeout=1,
    )
    assert job.execution_state is ExecutionState.FAILED
    assert job.delivery_state is DeliveryState.READY
    assert job.failure == JobFailure(
        "persistence_failed", "Specialist result could not be saved.",
    )
    assert "PRIVATE_PERSISTENCE_CANARY" not in caplog.text


@pytest.mark.asyncio
async def test_double_terminal_write_failure_reconciles_without_holding_permit(
    tool_env, monkeypatch, caplog,
):
    allow_reconciliation = False
    fallback_attempted = asyncio.Event()
    real_fail = tool_env.job_registry.fail_compat
    tool_env.job_registry._reconciliation_retry_interval = 0.01

    async def fail_primary(*_args, **_kwargs):
        raise OSError("PRIVATE_PRIMARY_WRITE_CANARY")

    async def controlled_fallback(*args, **kwargs):
        fallback_attempted.set()
        if not allow_reconciliation:
            raise OSError("PRIVATE_FALLBACK_WRITE_CANARY")
        return await real_fail(*args, **kwargs)

    monkeypatch.setattr(
        tool_env.job_registry, "finish_voice_result", fail_primary,
    )
    monkeypatch.setattr(
        tool_env.job_registry, "fail_compat", controlled_fallback,
    )
    accepted = tool_payload(await tool_env.invoke_delegate())
    job_id = accepted["job_id"]
    await tool_env.runner.finish()
    await asyncio.wait_for(fallback_attempted.wait(), timeout=1)
    await asyncio.wait_for(
        tool_env.job_registry.wait_for_runtime_release(job_id), timeout=1,
    )

    assert tool_env.limiter.in_flight == 0
    assert tool_env.job_registry.get(job_id).execution_state is ExecutionState.RUNNING
    assert tool_env.job_registry.reconciliation_count == 1

    allow_reconciliation = True
    terminal = await asyncio.wait_for(
        tool_env.job_registry.wait_for_terminal(job_id), timeout=1,
    )
    await asyncio.wait_for(
        tool_env.job_registry.wait_for_reconciliation(job_id), timeout=1,
    )
    assert terminal.execution_state is ExecutionState.FAILED
    assert terminal.failure == JobFailure(
        "persistence_failed", "Specialist result could not be saved.",
    )
    assert tool_env.job_registry.reconciliation_count == 0
    assert "PRIVATE_PRIMARY_WRITE_CANARY" not in caplog.text
    assert "PRIVATE_FALLBACK_WRITE_CANARY" not in caplog.text


@pytest.mark.asyncio
# Route protocol 3 (#233/#224): a route with a partial capability set can no
# longer reach here — registration compares for EXACT equality and rejects it.
# What DOES reach here is a device that offered no way to receive a deferred
# answer, which is the live failure (an iPhone with no satellite) this replaces.
@pytest.mark.parametrize("origin", [
    voice_origin(voice_route_id=None),
    voice_origin(voice_delivery_offer=None),
    voice_origin(voice_delivery_offer={"modality": "smoke-signal"}),
    voice_origin(voice_transport="sse"),
])
async def test_voice_async_without_capable_route_fails_before_side_effect(
    origin, tool_env,
):
    payload = tool_payload(await tool_env.invoke_delegate(origin))
    assert payload["kind"] == "background_delivery_unavailable"
    assert tool_env.job_registry.all() == []
    assert tool_env.runner.calls == []
    assert tool_env.limiter.in_flight == 0


@pytest.mark.asyncio
async def test_sixth_active_or_ready_job_on_route_is_rejected_without_mutation(
    tool_env,
):
    for index in range(5):
        await tool_env.add_job(
            f"job-{index}",
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.READY,
            terminal_at=time.time(),
            expires_at=time.time() + 900,
            result=json.dumps(_structured_result()),
        )

    payload = tool_payload(await tool_env.invoke_delegate())

    assert payload == {
        "status": "error",
        "kind": "route_capacity_reached",
        "message": (
            "This voice route already has 5 specialist jobs awaiting "
            "completion or delivery."
        ),
    }
    assert [job.id for job in tool_env.job_registry.all()] == [
        "job-0", "job-1", "job-2", "job-3", "job-4",
    ]
    assert tool_env.runner.calls == []
    assert tool_env.limiter.in_flight == 0


@pytest.mark.asyncio
async def test_expired_job_is_removed_before_route_capacity_is_counted(tool_env):
    now = time.time()
    for index in range(5):
        await tool_env.add_job(
            f"job-{index}",
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.READY,
            terminal_at=now - 10,
            expires_at=(now - 1 if index == 0 else now + 900),
            result=json.dumps(_structured_result()),
        )

    payload = tool_payload(await tool_env.invoke_delegate())

    assert payload["status"] == "pending"
    assert tool_env.job_registry.get("job-0").delivery_state is (
        DeliveryState.EXPIRED
    )


@pytest.mark.asyncio
async def test_tool_uses_live_route_freshness_at_launch_and_completion(
    tool_env, monkeypatch,
):
    now = [100.0]

    class _Socket:
        async def send_json(self, _frame):
            return None

    caller = _caller_cfg()
    caller.channels = ["ha_voice"]
    routes = VoiceRouteRegistry(
        secret_present=True,
        freshness_s=60,
        clock=lambda: now[0],
        agent_configs={"concierge": caller},
    )
    connection = VoiceWsConnection(_Socket())
    await routes.register(connection, {
        "type": "voice_route_register",
        "protocol": 3,
        "route_id": "entry-1",
        "agent_role": "concierge",
        "capabilities": [
            "background_jobs", "endpoint_delivery", "voice_handoff",
        ],
    })
    await routes.disconnect(connection)
    monkeypatch.setattr(
        tools,
        "_runtime",
        type("Runtime", (), {"voice_route_registry": routes})(),
    )

    now[0] = 159.0
    accepted = tool_payload(await tool_env.invoke_delegate())
    assert accepted["status"] == "pending"

    now[0] = 161.0
    await tool_env.runner.finish()
    terminal = await asyncio.wait_for(
        tool_env.job_registry.wait_for_terminal(accepted["job_id"]),
        timeout=1,
    )
    assert terminal.delivery_state is DeliveryState.READY
    assert routes.get_connected("entry-1") is None

    rejected = tool_payload(await tool_env.invoke_delegate(agent="health"))
    assert rejected["kind"] == "background_delivery_unavailable"
    assert len(tool_env.job_registry.all()) == 1


@pytest.mark.asyncio
async def test_status_never_returns_result_task_or_context_text(tool_env):
    canary = "PRIVATE_RESULT_CANARY"
    await tool_env.add_job(
        "job-1",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result(answer=canary)),
    )

    payload = tool_payload(await _call(
        tools.voice_job_status, voice_origin(), {"job_id": "job-1"},
    ))
    assert payload == {
        "status": "succeeded",
        "job_id": "job-1",
        "specialist_display_name": "Judge",
        "awaiting_input": False,
        "delivery_status": "ready",
    }
    serialized = json.dumps(payload)
    assert canary not in serialized
    assert "PRIVATE_TASK_CANARY" not in serialized
    assert "PRIVATE_CONTEXT_CANARY" not in serialized


@pytest.mark.asyncio
async def test_ready_status_reports_waiting_when_stable_route_is_absent(
    tool_env, monkeypatch,
):
    await tool_env.add_job(
        "job-1",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result()),
    )

    class _Routes:
        @staticmethod
        def get_connected(_route_id):
            return None

    runtime = type("Runtime", (), {"voice_route_registry": _Routes()})()
    monkeypatch.setattr(tools, "_runtime", runtime)
    payload = tool_payload(await _call(
        tools.voice_job_status, voice_origin(), {"job_id": "job-1"},
    ))

    assert payload["delivery_status"] == "waiting_for_route"


@pytest.mark.asyncio
async def test_ready_status_waits_when_reconnected_route_lacks_capabilities(
    tool_env, monkeypatch,
):
    await tool_env.add_job(
        "job-1",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result()),
    )

    incapable = type("Route", (), {"capabilities": frozenset()})()

    class _Routes:
        @staticmethod
        def get_connected(_route_id):
            return incapable

    runtime = type("Runtime", (), {"voice_route_registry": _Routes()})()
    monkeypatch.setattr(tools, "_runtime", runtime)
    payload = tool_payload(await _call(
        tools.voice_job_status, voice_origin(), {"job_id": "job-1"},
    ))

    assert payload["delivery_status"] == "waiting_for_route"


@pytest.mark.asyncio
async def test_explicit_status_can_inspect_an_owned_terminal_job(tool_env):
    await tool_env.add_job(
        "job-delivered",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.DELIVERED,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result()),
    )

    payload = tool_payload(await _call(
        tools.voice_job_status, voice_origin(), {"job_id": "job-delivered"},
    ))
    assert payload == {
        "status": "succeeded",
        "job_id": "job-delivered",
        "specialist_display_name": "Judge",
        "awaiting_input": False,
        "delivery_status": "delivered",
    }


@pytest.mark.asyncio
async def test_unauthorized_explicit_status_is_indistinguishable_from_missing(tool_env):
    await tool_env.add_job(
        "job-private", creator_user_id="other-user",
        result="PRIVATE_RESULT_CANARY",
    )
    denied = tool_payload(await _call(
        tools.voice_job_status, voice_origin(), {"job_id": "job-private"},
    ))
    missing = tool_payload(await _call(
        tools.voice_job_status, voice_origin(), {"job_id": "does-not-exist"},
    ))
    assert denied == missing
    assert denied["kind"] == "job_not_found"
    assert "PRIVATE" not in json.dumps(denied)


@pytest.mark.asyncio
async def test_anonymous_job_requires_an_exact_anonymous_actor(tool_env):
    await tool_env.add_job("job-anonymous", creator_user_id=None)
    denied = tool_payload(await _call(
        tools.voice_job_status, voice_origin(), {"job_id": "job-anonymous"},
    ))
    missing = tool_payload(await _call(
        tools.voice_job_status, voice_origin(), {"job_id": "does-not-exist"},
    ))
    assert denied == missing

    allowed = tool_payload(await _call(
        tools.voice_job_status,
        voice_origin(user_id=None),
        {"job_id": "job-anonymous"},
    ))
    assert allowed["job_id"] == "job-anonymous"


@pytest.mark.asyncio
async def test_omitted_status_id_selects_the_only_authorized_job(tool_env):
    await tool_env.add_job("job-mine")
    await tool_env.add_job("job-not-mine", creator_user_id="other-user")

    payload = tool_payload(await _call(
        tools.voice_job_status, voice_origin(), {},
    ))
    assert payload == {
        "status": "running",
        "job_id": "job-mine",
        "specialist_display_name": "Judge",
        "awaiting_input": False,
        "delivery_status": "none",
    }


@pytest.mark.asyncio
async def test_omitted_cancel_id_requires_exactly_one_match(tool_env):
    await tool_env.add_job("job-1")
    await tool_env.add_job(
        "job-2", specialist_role="health", specialist_display_name="Health",
    )

    payload = tool_payload(await _call(
        tools.cancel_voice_job, voice_origin(), {},
    ))
    assert payload["kind"] == "ambiguous_job"
    assert payload["choices"] == [
        {"job_id": "job-1", "specialist_display_name": "Judge"},
        {"job_id": "job-2", "specialist_display_name": "Health"},
    ]
    assert tool_env.job_registry.get("job-1").cancel_pending is False
    assert tool_env.job_registry.get("job-2").cancel_pending is False


@pytest.mark.asyncio
async def test_unauthorized_cancel_is_indistinguishable_from_missing(tool_env):
    await tool_env.add_job("job-private", creator_user_id="other-user")
    denied = tool_payload(await _call(
        tools.cancel_voice_job, voice_origin(), {"job_id": "job-private"},
    ))
    missing = tool_payload(await _call(
        tools.cancel_voice_job, voice_origin(), {"job_id": "does-not-exist"},
    ))
    assert denied == missing
    assert denied["kind"] == "job_not_found"
    assert tool_env.job_registry.get("job-private").cancel_pending is False


@pytest.mark.asyncio
async def test_cancel_authorizes_with_trusted_actor_and_hides_private_fields(tool_env):
    await tool_env.add_job("job-1")
    payload = tool_payload(await _call(
        tools.cancel_voice_job, voice_origin(), {"job_id": "job-1"},
    ))
    assert payload == {
        "status": "stopping",
        "job_id": "job-1",
        "specialist_display_name": "Judge",
    }
    assert "PRIVATE" not in json.dumps(payload)
    assert tool_env.job_registry.get("job-1").cancel_pending is True


@pytest.mark.asyncio
async def test_cancel_completion_race_has_one_honest_terminal_winner(
    tool_env, monkeypatch,
):
    monkeypatch.setattr(JobRegistry, "CANCEL_GRACE_SECONDS", 0.0)
    accepted = tool_payload(await tool_env.invoke_delegate())
    job_id = accepted["job_id"]
    await tool_env.runner.finish()
    cancel_task = asyncio.create_task(_call(
        tools.cancel_voice_job, voice_origin(), {"job_id": job_id},
    ))
    payload = tool_payload(await cancel_task)
    job = await asyncio.wait_for(
        tool_env.job_registry.wait_for_terminal(job_id), timeout=1,
    )
    assert payload["status"] in {"stopping", "cancelled", "too_late"}
    assert job.execution_state in {ExecutionState.SUCCEEDED, ExecutionState.CANCELLED}
    assert not (job.execution_state is ExecutionState.SUCCEEDED and job.cancel_pending)
    await asyncio.wait_for(
        tool_env.job_registry.wait_for_runtime_release(job_id), timeout=1,
    )
    assert tool_env.limiter.in_flight == 0


@pytest.mark.asyncio
async def test_continue_job_copies_private_backend_context_without_returning_it(tool_env):
    canary = "PRIVATE_PRIOR_RESULT_CANARY"
    parent = await tool_env.add_job(
        "job-parent",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result(
            status="needs_clarification",
            spoken_summary="Which card do you mean?",
            clarification="Which card do you mean?",
            answer=canary,
        )),
        awaiting_input=True,
        continuable_until=time.time() + 900,
    )

    payload = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(origin_device_id="device-office", voice_route_id="entry-2"),
        {"input": "I mean Black Lotus", "job_id": ""},
    ))
    assert payload == {
        "status": "pending",
        "job_id": payload["job_id"],
        "specialist_display_name": "Judge",
    }
    assert canary not in json.dumps(payload)
    child = tool_env.job_registry.get(payload["job_id"])
    consumed_parent = tool_env.job_registry.get(parent.id)
    assert consumed_parent.awaiting_input is False
    assert consumed_parent.continuable_until == parent.continuable_until
    assert child.parent_job_id == parent.id
    assert child.origin_route_id == "entry-2"
    assert child.origin_device_id == "device-office"
    assert child.task == "I mean Black Lotus"
    assert canary in child.context
    await tool_env.runner.started.wait()
    assert canary in tool_env.runner.calls[-1]["context"]


@pytest.mark.asyncio
async def test_max_size_parent_can_still_be_continued(tool_env):
    """#324: the awaiting-input continuation wraps parent task+context+result
    into an internal JSON envelope; re-checking that envelope against the
    caller-facing 8000-char context bound made a valid near-cap parent
    uncontinuable (input_too_large on a field the caller never wrote)."""
    from specialist_limits import _MAX_CONTEXT_CHARS, _MAX_TASK_CHARS

    await tool_env.add_job(
        "job-big",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        task="T" * _MAX_TASK_CHARS,
        context="C" * _MAX_CONTEXT_CHARS,
        result=json.dumps(_structured_result(
            status="needs_clarification",
            spoken_summary="Which card do you mean?",
            clarification="Which card do you mean?",
            answer="A" * _MAX_CONTEXT_CHARS,
        )),
        awaiting_input=True,
        continuable_until=time.time() + 900,
    )

    payload = tool_payload(await _call(
        tools.continue_voice_job, voice_origin(), {"input": "the black one"},
    ))
    assert payload["status"] == "pending", payload


@pytest.mark.asyncio
async def test_continuation_envelope_truncates_an_oversized_prior_result(tool_env):
    """The internal envelope skips the caller-facing bound, so its own
    components must stay bounded: an outsized stored result is truncated
    rather than shipped whole."""
    await tool_env.add_job(
        "job-huge",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result="R" * 200_000,
        awaiting_input=True,
        continuable_until=time.time() + 900,
    )

    payload = tool_payload(await _call(
        tools.continue_voice_job, voice_origin(), {"input": "go on"},
    ))
    assert payload["status"] == "pending", payload
    child = tool_env.job_registry.get(payload["job_id"])
    assert len(child.context) < 50_000
    assert "truncated" in child.context


@pytest.mark.asyncio
async def test_continuation_via_resume_copies_creator_and_recaptures_executor(
    tool_env,
):
    """The awaiting_input=True resume path (_start_voice_async_job with
    parent_job_id set) must copy the ORIGINAL job's creator verbatim — never
    whoever is invoking continue_voice_job now — while executing_speaker
    picks up the specialist's CURRENT binding."""
    original_caller = SpeakerProvenance(
        speaker_kind="resident", role_id="resident:concierge",
        persona_id="casa/gary", persona_version="0.1.0",
        display_name="Gary", binding_digest="sha256:" + "c" * 64,
    )
    updated_judge = SpeakerProvenance(
        speaker_kind="specialist", role_id="specialist:judge",
        persona_id="casa/judge", persona_version="0.2.0",
        display_name="Judge", binding_digest="sha256:" + "d" * 64,
    )
    parent = await tool_env.add_job(
        "job-parent",
        creating_speaker=original_caller,
        executing_speaker=SpeakerProvenance(speaker_kind="system"),
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result(
            status="needs_clarification",
            spoken_summary="Which card do you mean?",
            clarification="Which card do you mean?",
        )),
        awaiting_input=True,
        continuable_until=time.time() + 900,
    )
    # The specialist's binding has since activated — and the continuing
    # turn's own identity must be ignored for creating_speaker regardless.
    tools._agent_role_map["judge"].speaker_provenance = updated_judge

    payload = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(speaker_provenance=SpeakerProvenance(speaker_kind="system")),
        {"input": "I mean Black Lotus", "job_id": "job-parent"},
    ))
    child = tool_env.job_registry.get(payload["job_id"])
    assert child.parent_job_id == parent.id
    assert child.creating_speaker == original_caller
    assert child.executing_speaker == updated_judge


@pytest.mark.asyncio
@pytest.mark.parametrize("sensitivity", ["household", "private"])
async def test_explicit_detail_request_creates_prompted_delivery_child(
    tool_env, sensitivity,
):
    result_canary = f"{sensitivity.upper()}_DETAIL_RESULT_CANARY"
    parent = await tool_env.add_job(
        "job-parent",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.DELIVERED,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result(
            spoken_summary=result_canary,
            answer=f"{result_canary}_ANSWER",
            sensitivity=sensitivity,
        )),
        awaiting_input=False,
        continuable_until=None,
    )

    payload = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(origin_device_id="device-office", voice_route_id="entry-2"),
        {"input": "Please tell me the details", "job_id": "job-parent"},
    ))

    assert payload == {
        "status": "pending",
        "job_id": payload["job_id"],
        "specialist_display_name": "Judge",
    }
    assert result_canary not in json.dumps(payload)
    child = tool_env.job_registry.get(payload["job_id"])
    assert child.parent_job_id == parent.id
    assert child.origin_route_id == "entry-2"
    assert child.origin_device_id == "device-office"
    assert child.execution_state is ExecutionState.SUCCEEDED
    assert child.delivery_state is DeliveryState.READY
    assert child.result == parent.result
    assert child.prompted_delivery is True
    assert tool_env.job_registry.get(parent.id).origin_route_id == "entry-1"
    assert tool_env.job_registry.get(parent.id).origin_device_id == "device-kitchen"
    assert tool_env.runner.calls == []
    assert tool_env.limiter.in_flight == 0


@pytest.mark.asyncio
async def test_prompted_delivery_continuation_copies_creator_and_recaptures_executor(
    tool_env,
):
    """The prompted-delivery branch (_new_voice_job + create_prompted_delivery,
    parent.awaiting_input is False) must ALSO copy the ORIGINAL job's creator
    verbatim and pick up the specialist's CURRENT binding for the executor —
    same Task 12 continuation contract as the resume branch."""
    original_caller = SpeakerProvenance(
        speaker_kind="resident", role_id="resident:concierge",
        persona_id="casa/gary", persona_version="0.1.0",
        display_name="Gary", binding_digest="sha256:" + "e" * 64,
    )
    updated_judge = SpeakerProvenance(
        speaker_kind="specialist", role_id="specialist:judge",
        persona_id="casa/judge", persona_version="0.2.0",
        display_name="Judge", binding_digest="sha256:" + "f" * 64,
    )
    parent = await tool_env.add_job(
        "job-parent",
        creating_speaker=original_caller,
        executing_speaker=SpeakerProvenance(speaker_kind="system"),
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.DELIVERED,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result()),
        awaiting_input=False,
        continuable_until=None,
    )
    tools._agent_role_map["judge"].speaker_provenance = updated_judge

    payload = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(speaker_provenance=SpeakerProvenance(speaker_kind="system")),
        {"input": "Please tell me the details", "job_id": "job-parent"},
    ))
    child = tool_env.job_registry.get(payload["job_id"])
    assert child.parent_job_id == parent.id
    assert child.creating_speaker == original_caller
    assert child.executing_speaker == updated_judge


@pytest.mark.asyncio
async def test_continuation_task_create_failure_restores_parent_without_child(
    tool_env, monkeypatch, caplog,
):
    child_canary = "PRIVATE_UNBOUND_CHILD_TASK_CANARY"
    continuable_until = time.time() + 900
    await tool_env.add_job(
        "job-parent",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.DELIVERED,
        terminal_at=time.time(),
        expires_at=continuable_until,
        result=json.dumps(_structured_result(
            status="needs_clarification",
            spoken_summary="Which one?",
            clarification="Which one?",
        )),
        awaiting_input=True,
        continuable_until=continuable_until,
    )
    real_factory = getattr(tools, "_create_voice_lifecycle_task", None)

    def fail_task_create(**_kwargs):
        raise RuntimeError("synthetic task creation failure")

    monkeypatch.setattr(
        tools, "_create_voice_lifecycle_task", fail_task_create, raising=False,
    )
    with pytest.raises(RuntimeError, match="task creation failure"):
        await _call(
            tools.continue_voice_job,
            voice_origin(),
            {"input": child_canary, "job_id": "job-parent"},
        )

    parent = tool_env.job_registry.get("job-parent")
    assert parent.awaiting_input is True
    assert parent.continuable_until == continuable_until
    assert [job.id for job in tool_env.job_registry.all()] == ["job-parent"]
    assert child_canary not in Path(tool_env.job_registry.path).read_text()
    assert child_canary not in caplog.text
    assert tool_env.limiter.in_flight == 0
    assert tool_env.runner.calls == []

    assert real_factory is not None
    monkeypatch.setattr(tools, "_create_voice_lifecycle_task", real_factory)
    retry = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(),
        {"input": "safe retry", "job_id": "job-parent"},
    ))
    assert retry["status"] == "pending"
    children = [
        job for job in tool_env.job_registry.all()
        if job.parent_job_id == "job-parent"
    ]
    assert [job.id for job in children] == [retry["job_id"]]
    assert children[0].execution_state is ExecutionState.RUNNING


@pytest.mark.asyncio
async def test_continuation_bind_write_failure_restores_parent_without_child(
    tool_env, monkeypatch, caplog,
):
    child_canary = "PRIVATE_UNBOUND_CHILD_BIND_CANARY"
    continuable_until = time.time() + 900
    await tool_env.add_job(
        "job-parent",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.DELIVERED,
        terminal_at=time.time(),
        expires_at=continuable_until,
        result=json.dumps(_structured_result(
            status="needs_clarification",
            spoken_summary="Which one?",
            clarification="Which one?",
        )),
        awaiting_input=True,
        continuable_until=continuable_until,
    )
    real_write = tool_env.job_registry._write_snapshot_locked

    async def fail_running_child(jobs):
        if any(
            job.parent_job_id == "job-parent"
            and job.execution_state is ExecutionState.RUNNING
            for job in jobs.values()
        ):
            raise OSError("synthetic bind write failure")
        await real_write(jobs)

    monkeypatch.setattr(
        tool_env.job_registry, "_write_snapshot_locked", fail_running_child,
    )
    with pytest.raises(OSError, match="bind write failure"):
        await _call(
            tools.continue_voice_job,
            voice_origin(),
            {"input": child_canary, "job_id": "job-parent"},
        )

    parent = tool_env.job_registry.get("job-parent")
    assert parent.awaiting_input is True
    assert parent.continuable_until == continuable_until
    assert [job.id for job in tool_env.job_registry.all()] == ["job-parent"]
    assert child_canary not in Path(tool_env.job_registry.path).read_text()
    assert child_canary not in caplog.text
    assert tool_env.limiter.in_flight == 0
    assert tool_env.runner.calls == []


@pytest.mark.asyncio
async def test_bind_published_before_caller_cancel_keeps_running_child_owned(
    tool_env, monkeypatch,
):
    continuable_until = time.time() + 900
    await tool_env.add_job(
        "job-parent",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.DELIVERED,
        terminal_at=time.time(),
        expires_at=continuable_until,
        result=json.dumps(_structured_result(
            status="needs_clarification",
            spoken_summary="Which one?",
            clarification="Which one?",
        )),
        awaiting_input=True,
        continuable_until=continuable_until,
    )
    running_written = asyncio.Event()
    release_write = asyncio.Event()
    real_write = tool_env.job_registry._write_snapshot_locked

    async def block_after_running_write(jobs):
        await real_write(jobs)
        if any(
            job.parent_job_id == "job-parent"
            and job.execution_state is ExecutionState.RUNNING
            for job in jobs.values()
        ):
            running_written.set()
            await release_write.wait()

    monkeypatch.setattr(
        tool_env.job_registry, "_write_snapshot_locked", block_after_running_write,
    )
    continuation = asyncio.create_task(_call(
        tools.continue_voice_job,
        voice_origin(),
        {"input": "continue after caller cancel", "job_id": "job-parent"},
    ))
    await asyncio.wait_for(running_written.wait(), timeout=1)
    continuation.cancel()
    release_write.set()
    with pytest.raises(asyncio.CancelledError):
        await continuation

    children = [
        job for job in tool_env.job_registry.all()
        if job.parent_job_id == "job-parent"
    ]
    assert len(children) == 1
    child = children[0]
    assert child.execution_state is ExecutionState.RUNNING
    assert tool_env.job_registry.get("job-parent").awaiting_input is False
    assert tool_env.job_registry.get("job-parent").continuable_until == continuable_until
    await asyncio.wait_for(tool_env.runner.started.wait(), timeout=1)
    await tool_env.runner.finish()
    await asyncio.wait_for(
        tool_env.job_registry.wait_for_terminal(child.id), timeout=1,
    )
    await asyncio.wait_for(
        tool_env.job_registry.wait_for_runtime_release(child.id), timeout=1,
    )
    assert tool_env.limiter.in_flight == 0


@pytest.mark.asyncio
async def test_continuation_parent_can_be_consumed_only_once(tool_env):
    await tool_env.add_job(
        "job-parent",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.DELIVERED,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result(
            status="needs_clarification",
            spoken_summary="Which one?",
            clarification="Which one?",
        )),
        awaiting_input=True,
        continuable_until=time.time() + 900,
    )
    first = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(),
        {"input": "first", "job_id": "job-parent"},
    ))
    assert first["status"] == "pending"
    await tool_env.runner.finish()
    child_id = first["job_id"]
    await asyncio.wait_for(
        tool_env.job_registry.wait_for_terminal(child_id), timeout=1,
    )
    await asyncio.wait_for(
        tool_env.job_registry.wait_for_runtime_release(child_id), timeout=1,
    )

    second = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(),
        {"input": "second", "job_id": "job-parent"},
    ))
    assert second["kind"] == "job_not_continuable"
    assert len(tool_env.job_registry.all()) == 2


@pytest.mark.asyncio
async def test_omitted_continue_id_is_ambiguous_and_creates_no_child(tool_env):
    for job_id, role, display in (
        ("job-1", "judge", "Judge"),
        ("job-2", "health", "Health"),
    ):
        await tool_env.add_job(
            job_id,
            specialist_role=role,
            specialist_display_name=display,
            execution_state=ExecutionState.SUCCEEDED,
            delivery_state=DeliveryState.READY,
            terminal_at=time.time(),
            expires_at=time.time() + 900,
            result=json.dumps(_structured_result()),
            awaiting_input=True,
            continuable_until=time.time() + 900,
        )

    payload = tool_payload(await _call(
        tools.continue_voice_job, voice_origin(), {"input": "more", "job_id": ""},
    ))
    assert payload["kind"] == "ambiguous_job"
    assert payload["choices"] == [
        {"job_id": "job-1", "specialist_display_name": "Judge"},
        {"job_id": "job-2", "specialist_display_name": "Health"},
    ]
    assert [job.id for job in tool_env.job_registry.all()] == ["job-1", "job-2"]


async def test_omitted_continue_id_selects_only_clarification_parent(tool_env):
    awaiting = await tool_env.add_job(
        "job-awaiting",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result(
            status="needs_clarification",
            clarification="Which card?",
        )),
        awaiting_input=True,
        continuable_until=time.time() + 900,
    )
    answered = await tool_env.add_job(
        "job-answered",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.DELIVERED,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result()),
    )

    payload = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(),
        {"input": "Black Lotus", "job_id": ""},
    ))

    assert payload["status"] == "pending"
    child = tool_env.job_registry.get(payload["job_id"])
    assert child.parent_job_id == awaiting.id
    assert tool_env.job_registry.get(awaiting.id).awaiting_input is False
    assert tool_env.job_registry.get(answered.id) == answered


async def test_server_bound_control_identity_reanchors_from_other_device(tool_env):
    parent = await tool_env.add_job(
        "job-parent",
        job_control_id="entry-1",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result(
            status="needs_clarification",
            clarification="Which card?",
        )),
        awaiting_input=True,
        continuable_until=time.time() + 900,
    )

    payload = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(
            chat_id="device-office",
            origin_device_id="device-office",
            voice_job_control_id="entry-1",
        ),
        {"input": "Black Lotus", "job_id": parent.id},
    ))

    assert payload["status"] == "pending"
    child = tool_env.job_registry.get(payload["job_id"])
    assert child.job_control_id == "entry-1"
    assert child.scope_id == "device-office"
    assert child.origin_device_id == "device-office"
    historical_parent = tool_env.job_registry.get(parent.id)
    assert historical_parent.scope_id == "scope-1"
    assert historical_parent.origin_device_id == "device-kitchen"


async def test_different_server_control_identity_cannot_access_job(tool_env):
    await tool_env.add_job(
        "job-parent",
        job_control_id="entry-1",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result(
            status="needs_clarification",
            clarification="Which card?",
        )),
        awaiting_input=True,
        continuable_until=time.time() + 900,
    )

    denied = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(
            chat_id="device-office",
            origin_device_id="device-office",
            voice_job_control_id="entry-other",
        ),
        {"input": "Black Lotus", "job_id": "job-parent"},
    ))

    assert denied["kind"] == "job_not_found"
    assert len(tool_env.job_registry.all()) == 1


@pytest.mark.asyncio
async def test_unauthorized_continue_is_indistinguishable_from_missing(tool_env):
    await tool_env.add_job(
        "job-private",
        creator_user_id="other-user",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result()),
        awaiting_input=True,
        continuable_until=time.time() + 900,
    )
    denied = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(),
        {"input": "more", "job_id": "job-private"},
    ))
    missing = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(),
        {"input": "more", "job_id": "does-not-exist"},
    ))
    assert denied == missing
    assert denied["kind"] == "job_not_found"
    assert len(tool_env.job_registry.all()) == 1


@pytest.mark.asyncio
async def test_expired_continuation_is_rejected_without_child(tool_env):
    await tool_env.add_job(
        "job-expired",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time() - 120,
        expires_at=time.time() - 60,
        result=json.dumps(_structured_result()),
        awaiting_input=True,
        continuable_until=time.time() - 60,
    )
    payload = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(),
        {"input": "too late", "job_id": "job-expired"},
    ))
    assert payload["kind"] == "job_not_continuable"
    assert len(tool_env.job_registry.all()) == 1


@pytest.mark.asyncio
async def test_continue_without_current_capable_route_creates_no_child(tool_env):
    await tool_env.add_job(
        "job-parent",
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        terminal_at=time.time(),
        expires_at=time.time() + 900,
        result=json.dumps(_structured_result()),
        awaiting_input=True,
        continuable_until=time.time() + 900,
    )
    payload = tool_payload(await _call(
        tools.continue_voice_job,
        voice_origin(voice_transport="sse", voice_route_id=None),
        {"input": "more", "job_id": "job-parent"},
    ))
    assert payload["kind"] == "background_delivery_unavailable"
    assert len(tool_env.job_registry.all()) == 1


def test_voice_job_tools_are_registered_on_both_framework_surfaces():
    names = {candidate.name for candidate in tools.CASA_TOOLS}
    assert {
        "voice_job_status", "cancel_voice_job", "continue_voice_job",
    } <= names
    selected = {
        candidate.name for candidate in tools.select_casa_tools(frozenset({
            "mcp__casa-framework__voice_job_status",
            "mcp__casa-framework__cancel_voice_job",
            "mcp__casa-framework__continue_voice_job",
        }))
    }
    assert selected == {
        "voice_job_status", "cancel_voice_job", "continue_voice_job",
    }


@pytest.mark.asyncio
async def test_a_device_that_cannot_receive_is_told_apart_from_a_missing_route(
    tool_env,
):
    """The refusal the model reads must name the REAL obstacle.

    Both refusals used to say "requires an acknowledged WebSocket route",
    which is false for a phone: the route is fine, the DEVICE simply has
    nowhere to put an answer that arrives a minute later. A model handed the
    wrong reason cannot tell the user anything useful, and the turn ends in
    the dead air this whole release exists to remove (#233/#224).
    """
    payload = tool_payload(await tool_env.invoke_delegate(
        voice_origin(voice_delivery_offer=None)))

    assert payload["kind"] == "background_delivery_unavailable"
    assert payload["reason"] == "no_delivery_endpoint"
    # It must instruct the model to SAY something rather than promise.
    assert "device" in payload["message"].lower()
    assert "do not promise" in payload["message"].lower()


@pytest.mark.asyncio
async def test_an_unacknowledged_route_still_reports_the_route(tool_env):
    payload = tool_payload(await tool_env.invoke_delegate(
        voice_origin(voice_transport="sse")))

    assert payload["reason"] == "no_acknowledged_route"


@pytest.mark.asyncio
async def test_an_invalid_specialist_result_logs_why(tool_env):
    """A failure the operator cannot diagnose is barely better than silence.

    A live mtg job failed with `kind=invalid_specialist_result` and nothing
    else, so there was no way to tell from prod logs which field the specialist
    got wrong. The parser's own message names the field and never quotes the
    payload, which makes it the one diagnostic that is both useful and safe.
    """
    import logging

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Capture(level=logging.WARNING)
    tools.logger.addHandler(handler)
    try:
        accepted = tool_payload(await tool_env.invoke_delegate())
        # A specialist that answers with an incomplete envelope.
        await tool_env.runner.outputs.put(tools.DelegatedOutput(
            text="PRIVATE_RESULT_CANARY",
            structured_output={"status": "answered"},
        ))
        await tool_env.job_registry.wait_for_terminal(accepted["job_id"])
        # The durable write lands before the log line; let the job task finish.
        for _ in range(8):
            await asyncio.sleep(0)
    finally:
        tools.logger.removeHandler(handler)

    failures = [m for m in records if "invalid_specialist_result" in m]
    assert failures, records
    assert "reason=" in failures[0]
    assert "missing required fields" in failures[0]
    # The rejected payload must never reach the log.
    assert "PRIVATE_RESULT_CANARY" not in failures[0]


@pytest.mark.asyncio
async def test_a_cli_aborted_job_is_not_blamed_on_the_envelope(tool_env):
    """The async path must name the CLI's verdict too (#254).

    `structured_output=None` has two very different causes: the specialist
    emitted a malformed envelope, or the CLI aborted the run before any
    envelope existed (max turns, budget, structured-output retries). Both
    reached the operator as `invalid_specialist_result` with the same reason,
    which sent the live mtg diagnosis after the wrong specialist behaviour.
    """
    import logging

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Capture(level=logging.WARNING)
    tools.logger.addHandler(handler)
    try:
        accepted = tool_payload(await tool_env.invoke_delegate())
        await tool_env.runner.outputs.put(tools.DelegatedOutput(
            text="PRIVATE_RESULT_CANARY",
            structured_output=None,
            run_subtype="error_max_structured_output_retries",
        ))
        job = await tool_env.job_registry.wait_for_terminal(accepted["job_id"])
        for _ in range(8):
            await asyncio.sleep(0)
    finally:
        tools.logger.removeHandler(handler)

    assert job.failure is not None
    assert job.failure.kind == "specialist_result_contract_failed"
    aborted = [m for m in records if "specialist_result_contract_failed" in m]
    assert aborted, records
    assert "error_max_structured_output_retries" in aborted[0]
    assert "PRIVATE_RESULT_CANARY" not in aborted[0]


@pytest.mark.asyncio
async def test_a_turn_limited_job_names_the_turn_limit(tool_env):
    """`error_max_turns` is a different repair from a contract failure (#254).

    One says "raise max_turns"; the other says "the specialist's own result
    contract collides with the envelope". Collapsing them into one kind is
    what made the live failure unactionable.
    """
    accepted = tool_payload(await tool_env.invoke_delegate())
    await tool_env.runner.outputs.put(tools.DelegatedOutput(
        text="", structured_output=None, run_subtype="error_max_turns",
    ))
    job = await tool_env.job_registry.wait_for_terminal(accepted["job_id"])

    assert job.failure is not None
    assert job.failure.kind == "specialist_turn_limit"


@pytest.mark.asyncio
async def test_handoff_telemetry_names_the_capabilities_that_exist(tool_env):
    """A diagnostic that reports a dead capability is worse than none.

    The line said `cap_satellite_announce=False` long after that capability was
    replaced by `endpoint_delivery`. It is the line an operator reads to work
    out why a hand-off was refused, and a permanently-false field in it points
    straight at a cause that cannot be true — which is exactly the wrong turn
    it invited during the v0.121.0 live verification.
    """
    import logging

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Capture(level=logging.INFO)
    previous = tools.logger.level
    tools.logger.setLevel(logging.INFO)
    tools.logger.addHandler(handler)
    try:
        await tool_env.invoke_delegate(voice_origin())
    finally:
        tools.logger.removeHandler(handler)
        tools.logger.setLevel(previous)

    decisions = [m for m in records if "voice_handoff_decision" in m]
    assert decisions, records
    line = decisions[0]
    assert "cap_endpoint_delivery=True" in line, line
    # The route contract no longer has this capability at all.
    assert "satellite_announce" not in line, line
