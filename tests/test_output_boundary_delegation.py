"""The note across delegations and engagements (#1038 §2.3, §3.3, §3.4, §7):
a brief authored under an undischarged obligation carries its resolved note on
the delegation record, on the job row across a restart, into the synthesized
narration turn, and — for the child's own DM output — on a scope VIEW built at
launch, never the parent's live scope. An engagement's DM sends resolve the
engagement's own scope from its persisted record.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import agent as agent_mod
import output_boundary as ob
import tools
from bus import BusMessage, MessageType
from job_registry import DeliveryState, ExecutionState, JobRegistry, VoiceJob
from output_boundary import Admitted, IntentKind as K
from output_boundary_testing import scope as _scope
from personality_types import SpeakerProvenance
from specialist_registry import DelegationComplete, DelegationRecord, SpecialistRegistry
from test_agent_process import _StubChannel, _make_agent
from test_delegate_to_agent import _caller_cfg, _specialist_cfg

pytestmark = [pytest.mark.unit]

INVOICE = ("/data/agent-inbox/assistant/ready/1758500000000-a1b2.pdf", "invoice.pdf")
WROTE = "Casa: Test wrote this without opening “invoice.pdf”."
SYSTEM = SpeakerProvenance(speaker_kind="system")


def _armed_scope():
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    return s


# ---------------------------------------------------------------------------
# The synthesized narration turn
# ---------------------------------------------------------------------------

async def test_the_synthesized_narration_turn_inherits_the_note(tmp_path):
    agent = _make_agent(tmp_path, role="assistant")
    stub = _StubChannel()
    agent._channel_manager.register(stub)
    complete = DelegationComplete(
        delegation_id="d1", agent="finance", status="ok", text="€412 due Friday",
        result_available=True,
        origin={"role": "assistant", "channel": "telegram", "chat_id": "500",
                "cid": "c", "user_text": "check the invoice", "_inherited_note": WROTE})
    msg = BusMessage(type=MessageType.NOTIFICATION, source="finance", target="assistant",
                     content=complete, channel="telegram",
                     context={"cid": "c", "chat_id": "500", "delegation_id": "d1"})
    with patch.object(agent, "_process", AsyncMock(return_value="Your invoice is €412, due Friday.")):
        await agent.handle_message(msg)
    delivered = stub.finalize_response_stream.await_args.args[0]
    assert isinstance(delivered, Admitted)
    assert delivered == WROTE + "\n\nYour invoice is €412, due Friday."


async def test_a_completion_without_a_note_narrates_clean(tmp_path):
    agent = _make_agent(tmp_path, role="assistant")
    stub = _StubChannel()
    agent._channel_manager.register(stub)
    complete = DelegationComplete(delegation_id="d2", agent="finance", status="ok",
                                  text="ok", result_available=True,
                                  origin={"role": "assistant", "channel": "telegram",
                                          "chat_id": "500", "cid": "c", "user_text": "x"})
    msg = BusMessage(type=MessageType.NOTIFICATION, source="finance", target="assistant",
                     content=complete, channel="telegram", context={"cid": "c", "chat_id": "500"})
    with patch.object(agent, "_process", AsyncMock(return_value="Done.")):
        await agent.handle_message(msg)
    assert stub.finalize_response_stream.await_args.args[0] == "Done."


# ---------------------------------------------------------------------------
# The job row, across a restart
# ---------------------------------------------------------------------------

def _job(**changes):
    base = VoiceJob(
        id="job-1", parent_job_id=None, creating_speaker=SYSTEM, executing_speaker=SYSTEM,
        creating_role="assistant", specialist_role="finance", specialist_display_name="Finance",
        creator_peer="telegram", creator_user_id=None, scope_id="500", origin_route_id="c",
        origin_device_id=None, task="check the invoice", context="", created_at=100.0,
        started_at=101.0, terminal_at=None, expires_at=None,
        execution_state=ExecutionState.RUNNING, delivery_state=DeliveryState.NONE,
        result=None, failure=None, awaiting_input=False, continuable_until=None,
        delivery_sequence=0, delivery_attempt_id=None, lease_until=None, cancel_pending=False,
    )
    return replace(base, **changes)


async def test_a_job_row_keeps_the_note_across_a_reload(tmp_path):
    reg = JobRegistry(tmp_path / "jobs.json", clock=lambda: 200.0)
    await reg.load()
    await reg.create(_job(output_note=WROTE))
    again = JobRegistry(tmp_path / "jobs.json", clock=lambda: 300.0)
    await again.load()
    assert again.get("job-1").output_note == WROTE


async def test_a_row_written_before_the_field_decodes_as_owing_no_note(tmp_path):
    reg = JobRegistry(tmp_path / "jobs.json", clock=lambda: 200.0)
    await reg.load()
    await reg.create(_job())
    raw = json.loads((tmp_path / "jobs.json").read_text())
    rows = raw if isinstance(raw, list) else (raw.get("jobs") or raw.get("items") or [])
    for row in rows:
        row.pop("output_note", None)
    (tmp_path / "jobs.json").write_text(json.dumps(raw))
    again = JobRegistry(tmp_path / "jobs.json", clock=lambda: 300.0)
    await again.load()
    assert again.get("job-1").output_note == ""


async def test_boot_replay_restores_the_note_on_the_completion_origin(tmp_path):
    from casa_core import _notify_recovered_delegations
    reg = JobRegistry(tmp_path / "jobs.json", clock=lambda: 200.0)
    await reg.load()
    await reg.create(_job(output_note=WROTE))
    owed = await reg.recover_after_restart()
    assert [j.id for j in owed] == ["job-1"]

    class _Bus:
        queues = {"assistant": object()}

        def __init__(self):
            self.sent = []

        async def notify(self, message):
            self.sent.append(message)

    bus = _Bus()
    await _notify_recovered_delegations(owed, reg, bus, assistant_role="assistant")
    assert bus.sent[0].content.origin["_inherited_note"] == WROTE


async def test_the_registration_adapter_writes_the_note_from_the_record_origin(tmp_path):
    reg = SpecialistRegistry(str(tmp_path / "specs"), tombstone_path=str(tmp_path / "tombs.json"))
    record = DelegationRecord(id="d9", agent="finance", started_at=100.0,
                              origin={"role": "assistant", "channel": "telegram", "chat_id": "500",
                                      "cid": "c", "user_text": "check", "_inherited_note": WROTE})
    await reg.register_delegation(record)
    assert reg.job_registry.get("d9").output_note == WROTE


# ---------------------------------------------------------------------------
# The launch: the note on the record, the view on the child
# ---------------------------------------------------------------------------

async def test_the_child_runs_under_a_view_with_the_launch_note_even_after_the_holder_is_rewritten(
        tmp_path, monkeypatch):
    resident_cfg = _specialist_cfg(role="butler")
    reg = SpecialistRegistry(str(tmp_path / "specs"), tombstone_path=str(tmp_path / "tombs.json"))
    tools.init_tools(channel_manager=None, bus=None, specialist_registry=reg, mcp_registry=None,
                     agent_role_map={"butler": resident_cfg,
                                     "assistant": _caller_cfg(delegates=("butler",))})
    monkeypatch.setattr(tools, "_attach_completion_callback", lambda task, record: None)

    a_scope = _armed_scope()
    holder = {"role": "assistant", "channel": "telegram", "chat_id": "1", "user_id": 1,
              "cid": "A", "user_text": "check the invoice", "turn_scope": a_scope}
    gate = asyncio.Event()
    seen: dict = {}

    async def _fake_bounded(cfg, task_text, context_text, resolution=None, output_format=None):
        await gate.wait()                       # the child takes its first step LATE
        seen["origin"] = dict(agent_mod.origin_var.get(None) or {})
        return tools.DelegatedOutput(text="ok")

    monkeypatch.setattr(tools, "_run_delegated_agent_bounded", _fake_bounded)
    token = agent_mod.origin_var.set(holder)
    try:
        res = await tools.delegate_to_agent.handler(
            {"agent": "butler", "task": "check the €412 invoice", "context": "", "mode": "async"})
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "pending"
        assert payload["casa_note"] == WROTE
        # turn B rewrites the pooled holder IN PLACE before the child's first step
        holder.clear()
        holder.update({"role": "assistant", "channel": "telegram", "chat_id": "2", "user_id": 2,
                       "cid": "B", "user_text": "unrelated", "turn_scope": _scope(id="turn-B")})
        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        agent_mod.origin_var.reset(token)
    origin = seen["origin"]
    assert origin["cid"] == "A" and origin["chat_id"] == "1"
    view = origin["turn_scope"]
    assert view.id == a_scope.id and view is not a_scope
    assert view.admit(K.CAPTION, "invoice.pdf") == WROTE + "\n\ninvoice.pdf"
    # the parent reading AFTER launch does not un-annotate the child's brief
    a_scope.note_read_ok(INVOICE[0])
    assert view.admit(K.CAPTION, "invoice.pdf").annotations == (WROTE,)
    rec = reg.job_registry.get(payload["delegation_id"])
    assert rec.output_note == WROTE


async def test_a_launch_owing_nothing_stores_no_note(tmp_path, monkeypatch):
    resident_cfg = _specialist_cfg(role="butler")
    reg = SpecialistRegistry(str(tmp_path / "specs"), tombstone_path=str(tmp_path / "tombs.json"))
    tools.init_tools(channel_manager=None, bus=None, specialist_registry=reg, mcp_registry=None,
                     agent_role_map={"butler": resident_cfg,
                                     "assistant": _caller_cfg(delegates=("butler",))})
    monkeypatch.setattr(tools, "_attach_completion_callback", lambda task, record: None)

    async def _fake_bounded(cfg, task_text, context_text, resolution=None, output_format=None):
        return tools.DelegatedOutput(text="ok")

    monkeypatch.setattr(tools, "_run_delegated_agent_bounded", _fake_bounded)
    token = agent_mod.origin_var.set({"role": "assistant", "channel": "telegram", "chat_id": "1",
                                      "user_id": 1, "cid": "A", "user_text": "x",
                                      "turn_scope": _scope()})
    try:
        res = await tools.delegate_to_agent.handler(
            {"agent": "butler", "task": "turn off the lights", "context": "", "mode": "async"})
    finally:
        agent_mod.origin_var.reset(token)
    payload = json.loads(res["content"][0]["text"])
    assert payload["status"] == "pending" and "casa_note" not in payload
    assert reg.job_registry.get(payload["delegation_id"]).output_note == ""


# ---------------------------------------------------------------------------
# Engagement-origin sends resolve the engagement's own scope
# ---------------------------------------------------------------------------

def test_current_scope_prefers_a_bound_engagement_and_carries_its_note():
    eng = SimpleNamespace(id="eng-1", status="active",
                          origin={"role": "assistant", "channel": "telegram", "chat_id": 500,
                                  "_inherited_note": WROTE})
    token = tools.engagement_var.set(eng)
    try:
        s = tools._current_scope()
    finally:
        tools.engagement_var.reset(token)
    assert s.id == "eng-1"
    assert s.admit(K.CAPTION, "the report") == WROTE + "\n\nthe report"


def test_launch_note_helper_resolves_the_note_and_leaves_the_origin_clean_when_nothing_is_owed():
    armed = _armed_scope()
    origin_with, note = tools._launch_note({"role": "assistant", "turn_scope": armed},
                                           "check the €412 invoice")
    assert note == WROTE and origin_with["_inherited_note"] == WROTE
    origin_without, note2 = tools._launch_note({"role": "assistant", "turn_scope": _scope()}, "x")
    assert note2 == "" and "_inherited_note" not in origin_without
    origin_none, note3 = tools._launch_note({"role": "assistant"}, "x")
    assert note3 == "" and "_inherited_note" not in origin_none
