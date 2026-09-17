"""A completed voice job must never create a second Gary model turn."""

from __future__ import annotations

import asyncio
import json

import pytest

import agent as agent_mod
import tools
from bus import MessageBus, MessageType
from channels import ChannelManager
from channels.voice.channel import VoiceHandoffReservation
from config import AgentConfig, CharacterConfig, DelegateEntry
from job_registry import ExecutionState, JobRegistry
from specialist_limits import SpecialistLimiter
from specialist_registry import SpecialistRegistry
from test_agent_process import _make_agent

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _origin() -> dict:
    reservation = VoiceHandoffReservation()
    reservation.bind_commit(lambda _job: None)
    return {
        "role": "concierge",
        "execution_role": "concierge",
        "channel": "voice",
        "chat_id": "scope-1",
        "user_id": "user-1",
        "cid": "turn-1",
        "user_text": "private question",
        "voice_transport": "ws",
        "voice_route_id": "entry-1",
        "voice_route_capabilities": frozenset({
            "background_jobs", "endpoint_delivery", "voice_handoff",
        }),
        "origin_device_id": "device-kitchen",
        "voice_delivery_offer": {
            "modality": "audio", "receipt": "playback_complete"},
        "voice_job_control_id": "entry-1",
        "_voice_handoff_reservation": reservation,
    }


def _payload(envelope: dict) -> dict:
    return json.loads(envelope["content"][0]["text"])


async def test_voice_job_completion_never_reenters_gary(
    tmp_path, monkeypatch, caplog,
):
    private_summary = "PRIVATE_SUMMARY_CANARY-7f31"
    private_answer = "PRIVATE_ANSWER_CANARY-a19c"
    private_citation = "PRIVATE_CITATION_CANARY-c8e2"
    private_canaries = (private_summary, private_answer, private_citation)
    release = asyncio.Event()

    async def _run(
        cfg, task_text, context_text, resolution=None, output_format=None,
        tool_counts=None,
    ) -> tools.DelegatedOutput:
        await release.wait()
        return tools.DelegatedOutput(
            text=private_answer,
            structured_output={
                "status": "answered",
                "spoken_summary": private_summary,
                "answer": private_answer,
                "clarification": "",
                "citations": [private_citation],
                "assumptions": [],
                "provenance": {},
                "sensitivity": "private",
                "delivery_ttl_s": 900,
            },
        )

    monkeypatch.setattr(tools, "_run_delegated_agent", _run)

    registry = JobRegistry(tmp_path / "jobs.json")
    await registry.load()
    specialists = SpecialistRegistry(
        str(tmp_path / "specialists"), job_registry=registry,
    )
    caller = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role="concierge")
    caller.delegates = [
        DelegateEntry(agent="judge", purpose="rules", when="rules question"),
    ]
    judge = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role="judge",
        character=CharacterConfig(name="Judge"),
        model="claude-sonnet-4-6",
    )
    bus = MessageBus()
    tools.init_tools(
        ChannelManager(), bus, specialists,
        agent_role_map={"concierge": caller, "judge": judge},
        specialist_limiter=SpecialistLimiter(max_global=2),
    )

    gary = _make_agent(tmp_path / "gary", role="concierge")
    gary_messages = []
    real_handle_message = gary.handle_message

    async def _capture_gary_message(msg):
        gary_messages.append(msg)
        return await real_handle_message(msg)

    bus.register("concierge", _capture_gary_message)
    loop_task = bus.start_agent_loop("concierge")
    before = gary._pool.stats().copy()

    token = agent_mod.origin_var.set(_origin())
    try:
        accepted = await tools.delegate_to_agent.handler({
            "agent": "judge",
            "task": "private question",
            "context": "PRIVATE_CASE_CANARY",
            "mode": "async",
        })
    finally:
        agent_mod.origin_var.reset(token)

    accepted_payload = _payload(accepted)
    assert accepted_payload == {
        "status": "pending",
        "job_id": accepted_payload["job_id"],
        "specialist_display_name": "Judge",
    }

    release.set()
    job_id = accepted_payload["job_id"]
    job = await asyncio.wait_for(
        registry.wait_for_terminal(job_id), timeout=1,
    )
    await asyncio.wait_for(
        registry.wait_for_runtime_release(job_id), timeout=1,
    )

    after = gary._pool.stats().copy()
    assert [m for m in bus.get_log() if m.type is MessageType.NOTIFICATION] == []
    assert gary_messages == []
    assert after == before
    for canary in private_canaries:
        assert canary not in caplog.text
        assert canary not in json.dumps(accepted_payload)
        assert canary not in json.dumps(vars(gary.config), default=str)
        assert canary in (job.result or "")

    token = agent_mod.origin_var.set(_origin())
    try:
        detail = await tools.continue_voice_job.handler({
            "job_id": job_id,
            "input": "Please tell me the details",
        })
    finally:
        agent_mod.origin_var.reset(token)
    detail_payload = _payload(detail)
    child = registry.get(detail_payload["job_id"])
    assert child.parent_job_id == job_id
    assert child.prompted_delivery is True
    assert gary._pool.stats().copy() == before
    assert gary_messages == []
    for canary in private_canaries:
        assert canary not in json.dumps(detail_payload)
        assert canary not in caplog.text

    loop_task.cancel()
    await asyncio.gather(loop_task, return_exceptions=True)
    await gary.aclose()
    await registry.close()
