"""#1079 — a clean chosen silence discharges the announcement it answers.

A durable announcement (a delegation terminal notice, live or replayed at boot,
a replayed ORPHAN notice, or a replayed engagement outcome) reaches the resident
through ``Agent._synthesize_delegation_turn``. When the resident's answer is a
deliberate ``<silent/>`` — no error, no consumed SDK retry, a channel present,
and every piece of model-authored content Casa committed to the operator on the
turn confirmed delivered — that answer IS the telling, and the obligation is
discharged through ``Agent._ack_delivery`` exactly once. Before #1079 it
acknowledged nothing, so the notice replayed at every boot with its answer
retained on the row.

Red cases specified by **astra** (drive redcase round, MODE: SPECIFY, against
``8d7a157d9391bcc5cf8bf6a212e61c190286735e``), accepted by **terra**. Every
case drives the REAL ``Agent.handle_message`` → ``_synthesize_delegation_turn``
→ ``_process`` with a scripted SDK client behind
``sdk_client_pool._default_make_client`` — never a patched ``_process`` — and
the durable twins go through the REAL ack factories, asserting the registry's
own state rather than a stub's call count.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)

import retry as retry_mod
from agent import Agent
from bus import BusMessage, MessageType
from channels import ChannelManager, DeliveryOutcome
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from mcp_registry import McpServerRegistry
from session_registry import SessionRegistry
from specialist_registry import DelegationComplete

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:  # pragma: no cover — run from inside tests/
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio]

ROLE = "assistant"
ANSWER = "The Q3 ledger reconciles to the cent (distinctive-answer-7731)."
QUESTION = "did the Q3 ledger reconcile? (distinctive-question-4410)"


# ---------------------------------------------------------------------------
# The scripted SDK boundary
# ---------------------------------------------------------------------------


def _mk_assistant(text: str) -> _SDKAssistantMessage:
    try:
        return _SDKAssistantMessage(content=[_SDKTextBlock(text=text)])
    except TypeError:  # pragma: no cover — older SDK constructor
        m = _SDKAssistantMessage.__new__(_SDKAssistantMessage)
        m.content = [_SDKTextBlock(text)]  # type: ignore[call-arg]
        return m


def _mk_result(sid: str) -> _SDKResultMessage:
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid  # type: ignore[attr-defined]
    m.is_error = False  # type: ignore[attr-defined]
    m.result = ""  # type: ignore[attr-defined]
    return m


class _Hook:
    """A script step that runs a coroutine INSIDE the response stream — where
    the SDK runs a tool call — instead of yielding a message."""

    def __init__(self, fn) -> None:
        self.fn = fn


class _ScriptedClient:
    def __init__(self, options, script: list, sid: str) -> None:
        self.options = options
        self.queries: list[str] = []
        self._script = script
        self._sid = sid

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)

    async def receive_response(self):
        for item in self._script:
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, _Hook):
                await item.fn()
                continue
            yield item
        yield _mk_result(self._sid)


class _Factory:
    """One script per client construction — a retried attempt cold-connects a
    fresh client, so attempt N runs script N."""

    def __init__(self, scripts: list[list]) -> None:
        self._scripts = list(scripts)
        self.clients: list[_ScriptedClient] = []

    def __call__(self, options) -> _ScriptedClient:
        script = self._scripts.pop(0) if self._scripts else []
        c = _ScriptedClient(options, script, sid=f"sid-{len(self.clients) + 1}")
        self.clients.append(c)
        return c


@contextmanager
def _patch_retry_sleep():
    # retry.py's MODULE-LOCAL asyncio only — never the shared asyncio.sleep.
    ns = SimpleNamespace(sleep=AsyncMock(), CancelledError=asyncio.CancelledError)
    with patch.object(retry_mod, "asyncio", ns):
        yield


# ---------------------------------------------------------------------------
# The resident, its channel, and one notice
# ---------------------------------------------------------------------------


class _TelegramStub:
    name = "telegram"

    def __init__(self, send_outcome=DeliveryOutcome.DELIVERED) -> None:
        self.send = AsyncMock(return_value=send_outcome)
        self.send_response = AsyncMock(return_value=DeliveryOutcome.DELIVERED)
        self.finalize_stream = AsyncMock(return_value=DeliveryOutcome.DELIVERED)
        self.finalize_response_stream = AsyncMock(
            return_value=DeliveryOutcome.DELIVERED)
        self.turn_finished = AsyncMock()

    def create_on_token(self, _context):
        async def _on_token(_text: str) -> None:
            return None
        return _on_token

    def final_sends(self) -> int:
        return (self.send_response.await_count
                + self.finalize_stream.await_count
                + self.finalize_response_stream.await_count)


def _make_agent(tmp_path) -> Agent:
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role=ROLE,
        model="claude-sonnet-4-6",
        system_prompt="You are helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
    )
    return Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
    )


@pytest.fixture
async def resident(tmp_path):
    agent = _make_agent(tmp_path)
    stub = _TelegramStub()
    agent._channel_manager.register(stub)
    import tools
    tools.init_tools(
        channel_manager=agent._channel_manager, bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
    )
    yield agent, stub
    await agent.aclose()


class _Counted:
    """Wraps a notice's REAL ``on_delivery`` and counts how often it ran."""

    def __init__(self, inner=None) -> None:
        self.inner = inner
        self.count = 0

    async def __call__(self) -> None:
        self.count += 1
        if self.inner is not None:
            await self.inner()


def _live_notice(on_delivery) -> BusMessage:
    return BusMessage(
        type=MessageType.NOTIFICATION,
        source="finance",
        target=ROLE,
        content=DelegationComplete(
            delegation_id="deleg-live-1", agent="finance", status="ok",
            text=ANSWER,
            origin={"role": ROLE, "channel": "telegram", "chat_id": "123",
                    "cid": "route-1", "user_text": QUESTION},
        ),
        channel="telegram",
        context={"chat_id": "123", "cid": "route-1",
                 "delegation_id": "deleg-live-1"},
        on_delivery=on_delivery,
    )


async def _handle(agent, notice, factory, monkeypatch):
    """Drive ``notice`` through the real ``handle_message``, observing (never
    replacing) ``_process``, ``_synthesize_delegation_turn`` and
    ``_ack_delivery``."""
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    seen = SimpleNamespace(synth=[], process=[], acks=[], ack_count_at_return=[])
    real_synth = agent._synthesize_delegation_turn
    real_process = agent._process
    real_ack = agent._ack_delivery
    counted = notice.on_delivery

    def _synth(msg):
        out = real_synth(msg)
        seen.synth.append(out)
        return out

    async def _process(msg, on_token=None, turn_report=None, **kwargs):
        text = await real_process(msg, on_token=on_token,
                                  turn_report=turn_report, **kwargs)
        seen.process.append((text, list((turn_report or {}).get("retries", []))))
        seen.ack_count_at_return.append(counted.count)
        return text

    async def _ack(msg, error_kind=None):
        seen.acks.append((msg, error_kind))
        return await real_ack(msg, error_kind)

    with patch.object(agent, "_synthesize_delegation_turn", _synth), \
            patch.object(agent, "_process", _process), \
            patch.object(agent, "_ack_delivery", _ack), \
            _patch_retry_sleep():
        response = await agent.handle_message(notice)
    return response, seen


def _assert_one_silent_discharge(stub, response, seen, counted, *, final_sends=0):
    # Synthesis ran once and the REQUEST it built carried the obligation.
    assert len(seen.synth) == 1
    synthesized = seen.synth[0]
    assert synthesized.type is MessageType.REQUEST
    # One attempt, the sentinel, and no retry consumed.
    assert seen.process == [("<silent/>", [])]
    # Nothing was acknowledged while the turn was still running.
    assert seen.ack_count_at_return == [0]
    # The silent branch, not the transport path: no final reply went out, the
    # typing lease was torn down, and the RESPONSE is empty (M4).
    assert stub.final_sends() == final_sends
    assert stub.turn_finished.await_count == 1
    assert response is not None and response.content == ""
    # Discharged through the one seam, once, with no error.
    assert [(m is synthesized, e) for m, e in seen.acks] == [(True, None)]
    assert counted.count == 1
    assert synthesized.on_delivery is None


async def test_live_terminal_chosen_silence_acks_once(resident, monkeypatch):
    agent, stub = resident
    counted = _Counted()
    factory = _Factory([[_mk_assistant("<silent/>")]])

    response, seen = await _handle(agent, _live_notice(counted), factory,
                                   monkeypatch)

    assert len(factory.clients) == 1
    assert len(factory.clients[0].queries) == 1
    assert ANSWER in factory.clients[0].queries[0]
    assert QUESTION in factory.clients[0].queries[0]
    assert stub.send.await_count == 0
    _assert_one_silent_discharge(stub, response, seen, counted)


async def test_delivered_discrete_then_chosen_silence_acks_once(
    resident, monkeypatch,
):
    agent, stub = resident
    counted = _Counted()
    tool_results: list = []
    count_after_send: list = []

    async def _send():
        import tools
        tool_results.append(await tools.send_message.handler(
            {"message": "Reconciled to the cent (sent-by-tool-5520).",
             "channel": "telegram"}))
        count_after_send.append(counted.count)

    factory = _Factory([[_Hook(_send), _mk_assistant("<silent/>")]])

    response, seen = await _handle(agent, _live_notice(counted), factory,
                                   monkeypatch)

    assert stub.send.await_count == 1
    sent = stub.send.await_args.args[0]
    assert "sent-by-tool-5520" in sent
    assert len(tool_results) == 1
    assert not tool_results[0].get("is_error")
    assert count_after_send == [0]
    _assert_one_silent_discharge(stub, response, seen, counted)


# ---------------------------------------------------------------------------
# The durable twins — through the REAL ack factories and the registries' state
# ---------------------------------------------------------------------------


class _BusProbe:
    queues = {ROLE: object()}

    def __init__(self) -> None:
        self.sent: list = []

    async def notify(self, message) -> None:
        self.sent.append(message)


def _job(**changes):
    from job_registry import DeliveryState, ExecutionState, VoiceJob
    from personality_types import SpeakerProvenance
    system = SpeakerProvenance(speaker_kind="system")
    base = VoiceJob(
        id="job-1079", parent_job_id=None,
        creating_speaker=system, executing_speaker=system,
        creating_role=ROLE, specialist_role="finance",
        specialist_display_name="Finance",
        creator_peer="telegram", creator_user_id=None,
        scope_id="123", origin_route_id="route-1",
        origin_device_id=None,
        task=QUESTION, context="",
        created_at=100.0, started_at=101.0, terminal_at=None,
        expires_at=None, execution_state=ExecutionState.RUNNING,
        delivery_state=DeliveryState.NONE,
        result=None, failure=None, awaiting_input=False,
        continuable_until=None, delivery_sequence=0,
        delivery_attempt_id=None, lease_until=None,
        cancel_pending=False,
    )
    return replace(base, **changes)


async def _jobs(tmp_path, now):
    from job_registry import JobRegistry
    reg = JobRegistry(tmp_path / "jobs.json", clock=lambda: now)
    await reg.load()
    return reg


async def _replay_jobs(registry):
    from casa_core import _notify_recovered_delegations
    owed = await registry.recover_after_restart()
    bus = _BusProbe()
    await _notify_recovered_delegations(owed, registry, bus,
                                        assistant_role=ROLE)
    return owed, bus


async def test_recovered_orphan_chosen_silence_clears_durable_obligation(
    resident, tmp_path, monkeypatch,
):
    agent, stub = resident
    first = await _jobs(tmp_path, 200.0)
    await first.create(_job())

    registry = await _jobs(tmp_path, 300.0)
    owed, bus = await _replay_jobs(registry)
    assert [j.id for j in owed] == ["job-1079"]
    row = registry.get("job-1079")
    assert row.failure is not None and row.failure.kind == "restart_orphan"
    assert row.orphan_notification_pending is True
    assert row.terminal_notification_pending is False
    assert len(bus.sent) == 1
    notice = bus.sent[0]
    assert (notice.target, notice.channel, notice.context["chat_id"]) == (
        ROLE, "telegram", "123")
    # Enqueue on its own settles nothing.
    assert registry.get("job-1079").orphan_notification_pending is True

    counted = _Counted(notice.on_delivery)          # the REAL _recovery_delivery_ack
    notice.on_delivery = counted
    factory = _Factory([[_mk_assistant("<silent/>")]])
    response, seen = await _handle(agent, notice, factory, monkeypatch)

    _assert_one_silent_discharge(stub, response, seen, counted)
    assert registry.get("job-1079").orphan_notification_pending is False
    fresh = await _jobs(tmp_path, 400.0)
    assert await fresh.recover_after_restart() == []


async def test_replayed_terminal_chosen_silence_clears_marker_and_answer(
    resident, tmp_path, monkeypatch,
):
    agent, stub = resident
    first = await _jobs(tmp_path, 200.0)
    await first.create(_job())
    await first.finish_compat("job-1079", ANSWER, announce_creator=True)

    registry = await _jobs(tmp_path, 300.0)
    owed, bus = await _replay_jobs(registry)
    assert [j.id for j in owed] == ["job-1079"]
    row = registry.get("job-1079")
    assert row.terminal_notification_pending is True
    assert row.orphan_notification_pending is False
    assert row.result == ANSWER and row.result_available is True
    assert len(bus.sent) == 1
    notice = bus.sent[0]
    assert notice.content.text == ANSWER

    counted = _Counted(notice.on_delivery)
    notice.on_delivery = counted
    factory = _Factory([[_mk_assistant("<silent/>")]])
    response, seen = await _handle(agent, notice, factory, monkeypatch)

    _assert_one_silent_discharge(stub, response, seen, counted)
    row = registry.get("job-1079")
    assert row.terminal_notification_pending is False
    # INV-JOB-015: the retained answer goes in the same snapshot as the marker.
    assert row.result_available is False and row.result == ""
    fresh = await _jobs(tmp_path, 400.0)
    assert await fresh.recover_after_restart() == []


async def test_replayed_failure_chosen_silence_clears_marker_once(
    resident, tmp_path, monkeypatch,
):
    """#1084 regression guard (green at the base; no receipt): the replay
    statement the error arm now carries changes only what the resident is
    told — a clean chosen silence on a replayed FAILURE still discharges the
    obligation exactly once (INV-JOB-010, #1079 untouched)."""
    from job_registry import JobFailure

    agent, stub = resident
    first = await _jobs(tmp_path, 200.0)
    await first.create(_job())
    await first.fail_compat(
        "job-1079", JobFailure("timeout", "took too long"),
        announce_creator=True)

    registry = await _jobs(tmp_path, 300.0)
    owed, bus = await _replay_jobs(registry)
    assert [j.id for j in owed] == ["job-1079"]
    assert len(bus.sent) == 1
    notice = bus.sent[0]
    assert notice.content.status == "error"
    assert notice.content.replayed_after_restart is True

    counted = _Counted(notice.on_delivery)
    notice.on_delivery = counted
    factory = _Factory([[_mk_assistant("<silent/>")]])
    response, seen = await _handle(agent, notice, factory, monkeypatch)

    _assert_one_silent_discharge(stub, response, seen, counted)
    assert seen.synth[0].content.count("post-restart re-announcement") == 1
    assert registry.get("job-1079").terminal_notification_pending is False
    fresh = await _jobs(tmp_path, 400.0)
    assert await fresh.recover_after_restart() == []


def _driver_double():
    d = MagicMock()
    d.cancel = AsyncMock()
    for hook in ("finalize_completion_post", "finalize_summary",
                 "settle_all_open_questions", "drain_inbound_spool"):
        delattr(d, hook)
    return d


async def test_replayed_engagement_chosen_silence_clears_durable_obligation(
    resident, tmp_path, monkeypatch,
):
    import casa_core
    import tools
    from engagement_registry import EngagementRegistry

    agent, stub = resident
    tombstone = tmp_path / "engagements.json"
    reg = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    topic = MagicMock()
    topic.send_to_topic = AsyncMock()
    topic.send_response_to_topic = AsyncMock()
    topic.close_topic = AsyncMock()
    topic.update_topic_state = AsyncMock()
    topic_cm = MagicMock()
    topic_cm.get.return_value = topic
    tools.init_tools(
        channel_manager=topic_cm, bus=_BusProbe(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        engagement_registry=reg,
    )
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="reconcile the ledger",
        origin={"role": ROLE, "channel": "telegram", "chat_id": "123",
                "cid": "route-1", "user_text": QUESTION},
        topic_id=None)
    await tools._finalize_engagement(
        rec, outcome="completed", text="reconciled", artifacts=[],
        next_steps=[], driver=_driver_double())
    # The resident's own channel manager for the narration turn.
    tools.init_tools(
        channel_manager=agent._channel_manager, bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
    )

    registry = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await registry.load()
    assert [r.id for r in registry.records_owing_terminal_notification()] == [
        rec.id]
    bus = _BusProbe()
    await casa_core._replay_one_engagement_outcome(
        registry, bus, registry.get(rec.id), assistant_role=ROLE)
    assert len(bus.sent) == 1
    notice = bus.sent[0]
    assert (notice.target, notice.channel, notice.context["chat_id"]) == (
        ROLE, "telegram", "123")
    assert notice.content.status == "ok"
    assert notice.content.result_available is False
    assert [r.id for r in registry.records_owing_terminal_notification()] == [
        rec.id]

    counted = _Counted(notice.on_delivery)      # the REAL _engagement_delivery_ack
    notice.on_delivery = counted
    factory = _Factory([[_mk_assistant("<silent/>")]])
    response, seen = await _handle(agent, notice, factory, monkeypatch)

    _assert_one_silent_discharge(stub, response, seen, counted)
    assert registry.records_owing_terminal_notification() == []
    fresh = EngagementRegistry(tombstone_path=str(tombstone), bus=None)
    await fresh.load()
    assert fresh.records_owing_terminal_notification() == []
    assert fresh.get(rec.id).terminal_notification_pending is False
