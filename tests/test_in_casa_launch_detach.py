"""INV-ENG-021 (design 2026-09-15 §2.A): the engager is not held.

On the N150 (2026-09-15) Ellen's tool call was held for three minutes because
``engage_executor`` awaited the whole in-casa launch turn inline. Now the tool
call opens the client, hands the launch TURN to an anchored owner in one
synchronous block, and returns ``pending``. The owner does what the tool call
did after ``start()`` returned — reports a death through the reporter with the
engager's obligation armed and tells the engager live — and a never-started
owner, a stop latched during ``open()``, and a persist failure each have an
owner of their own. These are the design's red cases 1, 2, 2b, 2c, 3, 5, 5b,
5c, 5d, 5f, 5g, 5h, 5i and 7, on the real registry, driver and tool.
"""
from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest

from test_in_casa_launch_terminal_artifact import (
    ScriptedCompleteClient, ScriptedCutoffClient, _Probe, _build, _launch,
)


class _RecordingBus:
    """A bus that records every NOTIFICATION and can run its acknowledgement."""

    def __init__(self, roles=("assistant",)):
        self.queues = {r: object() for r in roles}
        self.messages: list = []

    async def notify(self, msg):
        if msg.target not in self.queues:
            return None          # the real bus drops an unknown target silently
        self.messages.append(msg)
        return True

    async def deliver_all(self):
        for m in self.messages:
            if m.on_delivery is not None:
                await m.on_delivery()


class GatedClient(ScriptedCutoffClient):
    """Blocks before its first frame until the test releases it."""
    gate: asyncio.Event | None = None

    async def receive_response(self):
        await type(self).gate.wait()
        async for frame in super().receive_response():
            yield frame


class PreFrameApiErrorClient(ScriptedCutoffClient):
    """Raises the driver's API-fault shape before any frame."""

    async def receive_response(self):
        from error_kinds import ErrorKind
        from drivers.in_casa_driver import ApiErrorTurn
        raise ApiErrorTurn(ErrorKind.API_ERROR, "boom")
        yield  # pragma: no cover


def _with_bus(monkeypatch, bus):
    import tools as tools_mod
    monkeypatch.setattr(tools_mod, "_bus", bus)
    return tools_mod


async def _drain_all(tools_mod):
    await tools_mod.drain_launch_turns()
    await tools_mod.drain_launch_death_reports()
    await asyncio.sleep(0)


# --- red case 1: pending before the first frame ---------------------------------

async def test_the_engager_gets_pending_before_the_launch_turn_starts(tmp_path, monkeypatch):
    probe = _Probe()
    GatedClient.gate = asyncio.Event()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, GatedClient)
    tools_mod = _with_bus(monkeypatch, _RecordingBus())

    # bounded: an inline launch would wait on the gated first frame for ever,
    # and a hang is not a pin
    envelope = await asyncio.wait_for(_launch(engage_executor), 5)
    payload = json.loads(envelope["content"][0]["text"])
    assert payload["status"] == "pending", payload
    assert GatedClient.frames_yielded == 0          # the turn has not begun
    created_id = next(iter(registry._records))
    assert registry.get(created_id).status == "active"
    assert len(tools_mod._LAUNCH_TURN_TASKS) == 1

    GatedClient.gate.set()
    await _drain_all(tools_mod)
    assert GatedClient.frames_yielded == 2
    # the cutoff client dies mid-loop: the owner reported it
    assert registry.get(created_id).status == "error"
    assert channel._post_engagement_notice.await_count == 1
    assert channel.close_topic.await_count == 1
    assert not tools_mod._LAUNCH_TURN_TASKS


# --- red cases 2 / 2b / 2c: the owner tells the engager live -------------------

async def test_a_cutoff_after_pending_tells_the_engager_live(tmp_path, monkeypatch):
    probe = _Probe()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)

    envelope = await _launch(engage_executor)
    assert json.loads(envelope["content"][0]["text"])["status"] == "pending"
    await _drain_all(tools_mod)

    created_id = next(iter(registry._records))
    rec = registry.get(created_id)
    assert rec.status == "error"
    assert rec.origin["error_kind"] == "launch_turn_incomplete"
    assert channel._post_engagement_notice.await_count == 1
    assert channel.close_topic.await_count == 1
    assert probe.events.index("notice") < probe.events.index("topic_close")
    # exactly one live notification, of the launch fault's kind, to the engager
    assert len(bus.messages) == 1
    complete = bus.messages[0].content
    assert complete.delegation_id == created_id
    assert complete.status == "error"
    assert complete.kind == "launch_turn_incomplete"
    assert complete.result_available is False
    assert bus.messages[0].target == "assistant"
    # the obligation is ARMED until the delivery acknowledgement clears it
    assert rec.terminal_notification_pending is True
    await bus.deliver_all()
    assert registry.get(created_id).terminal_notification_pending is False
    assert registry.records_owing_terminal_notification() == []


async def test_a_pre_frame_api_fault_after_pending_is_told_once(tmp_path, monkeypatch):
    probe = _Probe()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, PreFrameApiErrorClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)

    envelope = await _launch(engage_executor)
    assert json.loads(envelope["content"][0]["text"])["status"] == "pending"
    await _drain_all(tools_mod)

    created_id = next(iter(registry._records))
    rec = registry.get(created_id)
    assert rec.status == "error"
    assert rec.origin["error_kind"] == "api_error"
    assert channel._post_engagement_notice.await_count == 1      # tonight's silent arm posts
    assert channel.close_topic.await_count == 1
    assert [m.content.kind for m in bus.messages] == ["api_error"]


async def test_a_dropped_bus_target_leaves_the_obligation_owed(tmp_path, monkeypatch):
    probe = _Probe()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus(roles=())          # the engager's role is not registered
    tools_mod = _with_bus(monkeypatch, bus)

    await _launch(engage_executor)
    await _drain_all(tools_mod)

    created_id = next(iter(registry._records))
    assert bus.messages == []
    assert [r.id for r in registry.records_owing_terminal_notification()] == [created_id]


# --- red case 3: cancelling the tool call after return does not touch the turn -

async def test_cancelling_the_tool_task_after_return_does_not_cancel_the_turn(tmp_path, monkeypatch):
    probe = _Probe()
    GatedClient.gate = asyncio.Event()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, GatedClient)
    tools_mod = _with_bus(monkeypatch, _RecordingBus())

    task = asyncio.ensure_future(_launch(engage_executor))
    envelope = await task
    assert json.loads(envelope["content"][0]["text"])["status"] == "pending"
    task.cancel()                           # a finished task: nothing to cancel
    GatedClient.gate.set()
    await _drain_all(tools_mod)
    assert GatedClient.frames_yielded == 2   # the turn ran to its end regardless


# --- red case 5: the graceful stop drains a running owner ------------------------

async def test_the_stop_drains_a_running_owner_and_reports_the_cancellation(tmp_path, monkeypatch):
    probe = _Probe()
    GatedClient.gate = asyncio.Event()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, GatedClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)

    await _launch(engage_executor)
    await asyncio.sleep(0)                  # the owner has started and is gated
    assert len(tools_mod._LAUNCH_TURN_TASKS) == 1
    owner = next(iter(tools_mod._LAUNCH_TURN_TASKS))

    # bounded: a stop that cannot find the owner (not enrolled) would wait
    # on the gated turn for ever, and a hang is not a pin
    await asyncio.wait_for(tools_mod.stop_engagement_launches(registry), 5)

    assert owner.done()
    assert not tools_mod._LAUNCH_TURN_TASKS
    created_id = next(iter(registry._records))
    rec = registry.get(created_id)
    assert rec.status == "error"
    assert rec.origin["error_kind"] == "launch_cancelled"
    assert rec.origin.get("shutdown_reason")
    assert channel._post_engagement_notice.await_count == 1
    assert channel.close_topic.await_count == 1
    assert [m.content.kind for m in bus.messages] == ["launch_cancelled"]
    assert registry.launch_drains_complete() is True


# --- red case 5b / 5h: a stop latched during open() mints no owner ----------------

async def test_a_stop_latched_during_open_mints_no_owner(tmp_path, monkeypatch):
    probe = _Probe()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)

    real_aenter = ScriptedCutoffClient.__aenter__

    async def _latch_then_enter(self):
        # The stop begins while open() is suspended in the client's connect,
        # and the connect CONSUMES the cancellation the ledger delivered (an
        # SDK connect that catches BaseException to clean up and then
        # succeeds) — so the tool call resumes un-cancelled after open().
        registry.begin_launch_shutdown()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        return await real_aenter(self)

    monkeypatch.setattr(ScriptedCutoffClient, "__aenter__", _latch_then_enter)

    envelope = await _launch(engage_executor)
    payload = json.loads(envelope["content"][0]["text"])
    assert payload["status"] == "error", payload
    assert payload["kind"] == "launch_cancelled"
    assert tools_mod._LAUNCH_TURN_TASKS == set()
    assert ScriptedCutoffClient.frames_yielded == 0
    await tools_mod.drain_launch_death_reports()
    created_id = next(iter(registry._records))
    assert registry.get(created_id).status == "error"
    assert channel._post_engagement_notice.await_count == 1
    assert channel.close_topic.await_count == 1
    assert registry.get(created_id).terminal_notification_pending is False  # acked on return
    assert bus.messages == []                          # the envelope was the telling
    assert all(t.done() or t is asyncio.current_task() for t in asyncio.all_tasks())


# --- red case 5d: an owner cancelled before its first step ---------------------

async def test_an_owner_cancelled_before_its_first_step_is_reported_by_its_callback(
        tmp_path, monkeypatch):
    probe = _Probe()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)

    # Build a live record with an open client the way the launcher would,
    # then hand off and latch in the SAME synchronous block.
    import agent as agent_mod
    from engagement_registry import EngagementRecord
    rec = await registry.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        topic_id=42, origin={"role": "assistant", "channel": "telegram", "chat_id": "c1",
                             "cid": "x", "user_text": "hi"}, task="t")
    options = AsyncMock()
    await driver.open(rec, options=options, expected_generation=rec.context_generation)
    handle = registry.register_launch(rec.id, asyncio.current_task())
    tools_mod._hand_off_launch_turn(driver, rec, "prompt", channel, 42, handle)
    issued = registry.begin_launch_shutdown()      # cancels the owner before it runs
    assert issued == 1
    await _drain_all(tools_mod)

    assert ScriptedCutoffClient.frames_yielded == 0   # zero owner statements ran
    latest = registry.get(rec.id)
    assert latest.status == "error"
    assert latest.origin["error_kind"] == "launch_cancelled"
    assert channel._post_engagement_notice.await_count == 1
    assert channel.close_topic.await_count == 1
    assert [m.content.kind for m in bus.messages] == ["launch_cancelled"]
    assert registry._launch_handles == {}            # the callback dropped the handle
    assert driver.is_alive(latest) is False


# --- red case 5i: after the drains, a callback mints nothing --------------------

async def test_after_the_drains_a_never_started_owner_is_only_logged(tmp_path, monkeypatch, caplog):
    probe = _Probe()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)
    rec = await registry.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        topic_id=42, origin={"role": "assistant"}, task="t")
    await driver.open(rec, options=AsyncMock(), expected_generation=rec.context_generation)
    registry.mark_launch_drains_complete()
    handle = registry.register_launch(rec.id, asyncio.current_task())
    with caplog.at_level(logging.ERROR):
        tools_mod._hand_off_launch_turn(driver, rec, "prompt", channel, 42, handle)
        owner = next(iter(tools_mod._LAUNCH_TURN_TASKS))
        owner.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert not tools_mod._LAUNCH_TURN_TASKS
    assert not tools_mod._LAUNCH_DEATH_TASKS          # nothing minted
    assert registry.get(rec.id).status == "active"    # left live for boot
    assert any("after the stop's drains completed" in r.getMessage() for r in caplog.records)


# --- red case 5c: a persist failure has an owner, and it is not a failure ------

async def test_a_persist_failure_after_pending_tells_the_engager_the_outcome_is_uncommitted(
        tmp_path, monkeypatch):
    probe = _Probe()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)
    real_write = registry._write_tombstone

    def _boom_on_terminal(snapshot):
        if any(row.get("status") == "error" for row in snapshot):
            raise OSError("tombstone write refused")
        return real_write(snapshot)

    monkeypatch.setattr(registry, "_write_tombstone", _boom_on_terminal)

    envelope = await _launch(engage_executor)
    assert json.loads(envelope["content"][0]["text"])["status"] == "pending"
    await _drain_all(tools_mod)

    created_id = next(iter(registry._records))
    rec = registry.get(created_id)
    assert rec.status == "active"                       # rolled back, live
    assert rec.completed_at is None
    assert channel._post_engagement_notice.await_count == 1   # the bounded notice
    assert "could not be recorded" in probe.notice_texts[0]
    assert channel.close_topic.await_count == 0
    assert rec.terminal_notification_pending is False   # nothing to arm
    assert [m.content.kind for m in bus.messages] == ["launch_outcome_uncommitted"]
    assert bus.messages[0].on_delivery is None
    assert driver.is_alive(rec) is False                # the dead client retired


def test_the_consumer_narrates_an_uncommitted_outcome_as_still_open():
    """agent.py's completion consumer must not send this kind through the
    terminal-failure branch (Terra/Astra design round 2)."""
    from agent import Agent
    from bus import BusMessage, MessageType
    from specialist_registry import DelegationComplete
    complete = DelegationComplete(
        delegation_id="abcdef0123456789", agent="configurator", status="error",
        kind="launch_outcome_uncommitted", message="not recorded",
        result_available=False, origin={"user_text": "install gmail"})
    msg = BusMessage(type=MessageType.NOTIFICATION, source="configurator",
                     target="assistant", content=complete, channel="telegram",
                     context={"cid": "x", "chat_id": "c1", "engagement_id": complete.delegation_id})
    turn = Agent._synthesize_delegation_turn(object.__new__(Agent), msg)
    body = turn.content if isinstance(turn.content, str) else str(turn.content)
    assert "still open" in body
    assert "Delegation failed" not in body
    assert "did not fail" in body


# --- red case 5g: an inline named abort acknowledges on return ------------------

async def test_an_inline_named_abort_is_acknowledged_on_return(tmp_path, monkeypatch):
    probe = _Probe()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_engagement_driver", None, raising=False)

    envelope = await _launch(engage_executor)
    payload = json.loads(envelope["content"][0]["text"])
    assert payload["status"] == "error" and payload["kind"] == "no_driver"
    created_id = next(iter(registry._records))
    await tools_mod.drain_launch_death_reports()
    rec = registry.get(created_id)
    assert rec.status == "error"
    assert rec.terminal_notification_pending is False     # acknowledged by the return
    assert bus.messages == []                              # the envelope was the telling


# --- red case 5f: the shared envelope carries the funnel's next_steps ------------

async def test_the_shared_envelope_carries_context_extra_and_routes_from_the_origin():
    import tools as tools_mod
    from specialist_registry import DelegationComplete
    bus = _RecordingBus(roles=("assistant", "butler"))
    complete = DelegationComplete(delegation_id="e1", agent="configurator", status="ok", text="done")
    acked = []

    async def _ack():
        acked.append(True)

    await tools_mod.send_engagement_outcome(
        bus, complete=complete, origin={"role": "butler", "channel": "voice", "cid": "c", "chat_id": "42"},
        ack=_ack, context_extra={"next_steps": [{"action": "x"}]})
    m = bus.messages[0]
    assert m.target == "butler" and m.channel == "voice" and m.source == "configurator"
    assert m.context == {"cid": "c", "chat_id": "42", "engagement_id": "e1", "next_steps": [{"action": "x"}]}
    await m.on_delivery()
    assert acked == [True]
    # no role in the origin: the assistant fallback
    await tools_mod.send_engagement_outcome(bus, complete=complete, origin={}, ack=None)
    assert bus.messages[1].target == "assistant" and bus.messages[1].channel == ""
    assert "next_steps" not in bus.messages[1].context


# --- red case 6: the interactive specialist launch has the same shape -----------

async def test_the_interactive_specialist_launch_returns_pending_before_its_turn(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    import agent as agent_mod
    from tools import delegate_to_agent, init_tools
    from test_delegate_to_agent_interactive import _make_alex_cfg, _make_assistant_cfg
    probe = _Probe()
    GatedClient.gate = asyncio.Event()
    _exec, registry, channel, driver = _build(tmp_path, monkeypatch, probe, GatedClient)
    cm = MagicMock(); cm.get = MagicMock(return_value=channel)
    specialist_reg = MagicMock(); specialist_reg.get.return_value = _make_alex_cfg()
    init_tools(
        channel_manager=cm, bus=MagicMock(), specialist_registry=specialist_reg,
        mcp_registry=MagicMock(), trigger_registry=MagicMock(),
        engagement_registry=registry, executor_registry=MagicMock(),
        agent_role_map={"assistant": _make_assistant_cfg()},
    )
    tools_mod = _with_bus(monkeypatch, _RecordingBus())
    token = agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram", "chat_id": "c1", "cid": "x",
        "user_text": "hi", "scope": "business",
    })
    try:
        res = await asyncio.wait_for(delegate_to_agent.handler({
            "agent": "finance", "task": "Plan Q2", "context": "", "mode": "interactive",
        }), 5)
    finally:
        agent_mod.origin_var.reset(token)
    payload = json.loads(res["content"][0]["text"])
    assert payload["status"] == "pending", payload
    assert payload["mode"] == "interactive"
    assert GatedClient.frames_yielded == 0
    assert len(tools_mod._LAUNCH_TURN_TASKS) == 1
    GatedClient.gate.set()
    await _drain_all(tools_mod)
    assert GatedClient.frames_yielded == 2
    created_id = next(iter(registry._records))
    assert registry.get(created_id).status == "error"      # the cutoff client, reported
    assert channel._post_engagement_notice.await_count == 1
    assert channel.close_topic.await_count == 1


# --- red case 5e: an inline abort whose launcher is cancelled before its return ----

async def test_an_inline_abort_cancelled_before_its_return_is_told_live(tmp_path, monkeypatch):
    probe = _Probe()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_engagement_driver", None, raising=False)
    entered = asyncio.Event(); release = asyncio.Event()

    async def _blocked_close(*, thread_id):
        entered.set()
        await release.wait()
        probe.events.append("topic_close")

    channel.close_topic = AsyncMock(side_effect=_blocked_close)

    task = asyncio.ensure_future(_launch(engage_executor))
    await asyncio.wait_for(entered.wait(), 5)       # the strict flip committed; the close is wedged
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await _drain_all(tools_mod)

    created_id = next(iter(registry._records))
    rec = registry.get(created_id)
    assert rec.status == "error" and rec.origin["error_kind"] == "no_driver"
    assert channel.close_topic.await_count == 1             # the anchored abort finished
    # no envelope was returned, so the cancellation owner told the engager live
    assert [m.content.kind for m in bus.messages] == ["no_driver"]
    assert rec.terminal_notification_pending is True
    await bus.deliver_all()
    assert registry.get(created_id).terminal_notification_pending is False


# --- diff round 1 (Terra S1): a cancellation inside the live telling tells once ----

async def test_an_owner_cancelled_inside_its_live_telling_tells_the_engager_once(tmp_path, monkeypatch):
    probe = _Probe()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    entered = asyncio.Event(); release = asyncio.Event()

    class _BlockingBus(_RecordingBus):
        async def notify(self, msg):
            entered.set()
            await release.wait()      # the telling is in flight; the bus has not yet taken it
            self.messages.append(msg)
            return True

    bus = _BlockingBus()
    tools_mod = _with_bus(monkeypatch, bus)

    await _launch(engage_executor)
    await asyncio.wait_for(entered.wait(), 5)
    owner = next(iter(tools_mod._LAUNCH_TURN_TASKS))
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    release.set()
    await _drain_all(tools_mod)

    created_id = next(iter(registry._records))
    assert [m.content.kind for m in bus.messages] == ["launch_turn_incomplete"]   # ONE telling
    assert registry.get(created_id).status == "error"
    assert channel._post_engagement_notice.await_count == 1
    assert channel.close_topic.await_count == 1
    assert registry.get(created_id).terminal_notification_pending is True         # until delivery
    await bus.deliver_all()
    assert registry.get(created_id).terminal_notification_pending is False


# --- diff round 1 (Astra J1.1): the cancellation owner tells a persist failure --------

async def test_a_stop_whose_cancellation_report_cannot_persist_still_tells(tmp_path, monkeypatch):
    probe = _Probe()
    GatedClient.gate = asyncio.Event()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, GatedClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)
    real_write = registry._write_tombstone

    def _boom_on_terminal(snapshot):
        if any(row.get("status") == "error" for row in snapshot):
            raise OSError("tombstone write refused")
        return real_write(snapshot)

    monkeypatch.setattr(registry, "_write_tombstone", _boom_on_terminal)

    await _launch(engage_executor)
    await asyncio.sleep(0)
    await asyncio.wait_for(tools_mod.stop_engagement_launches(registry), 5)

    created_id = next(iter(registry._records))
    rec = registry.get(created_id)
    assert rec.status == "active"                                  # rolled back, live
    assert channel._post_engagement_notice.await_count == 1
    assert "could not be recorded" in probe.notice_texts[0]
    assert channel.close_topic.await_count == 0
    assert [m.content.kind for m in bus.messages] == ["launch_outcome_uncommitted"]
    assert rec.terminal_notification_pending is False


# --- diff round 1 (Astra J1.2): a lost race tells nobody, however armed the record ----

async def test_a_cancellation_owner_that_loses_to_a_telling_writer_sends_nothing(tmp_path, monkeypatch):
    probe = _Probe()
    GatedClient.gate = asyncio.Event()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, GatedClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)

    await _launch(engage_executor)
    await asyncio.sleep(0)
    created_id = next(iter(registry._records))
    # The funnel (the operator cancelled the engagement) wins the terminal and
    # ARMS its own telling, whose delivery is still pending when the stop lands.
    won = await registry.try_transition_terminal(
        created_id, "cancelled", strict=True, owes_terminal_notification=True)
    assert won is True
    await asyncio.wait_for(tools_mod.stop_engagement_launches(registry), 5)

    assert bus.messages == []                                       # the winner's to tell
    rec = registry.get(created_id)
    assert rec.status == "cancelled"
    assert rec.terminal_notification_pending is True                # still the funnel's
    assert channel._post_engagement_notice.await_count == 0
    assert channel.close_topic.await_count == 0


# --- diff round 1 (Astra J3): a losing inline abort acknowledges nothing ------------

async def test_a_losing_inline_abort_does_not_clear_another_writers_obligation(tmp_path, monkeypatch):
    probe = _Probe()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_engagement_driver", None, raising=False)
    real_create = registry.create

    async def _create_then_lose(*a, **kw):
        rec = await real_create(*a, **kw)
        # another writer wins the terminal, owing its telling, before the arm runs
        assert await registry.try_transition_terminal(
            rec.id, "cancelled", strict=True, owes_terminal_notification=True)
        return rec

    monkeypatch.setattr(registry, "create", _create_then_lose)

    envelope = await _launch(engage_executor)
    payload = json.loads(envelope["content"][0]["text"])
    assert payload["status"] == "error" and payload["kind"] == "no_driver"   # its own fault, as ever
    await _drain_all(tools_mod)
    created_id = next(iter(registry._records))
    rec = registry.get(created_id)
    assert rec.status == "cancelled"                                 # the winner's
    assert rec.terminal_notification_pending is True                 # NOT acknowledged by the loser
    assert [r.id for r in registry.records_owing_terminal_notification()] == [created_id]
    assert bus.messages == []
    assert channel.close_topic.await_count == 0


# --- diff round 1 (Astra J2): the compensator mints nothing after the drains ------------

async def test_a_launcher_cancelled_after_the_drains_mints_no_compensation(tmp_path, monkeypatch, caplog):
    probe = _Probe()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    tools_mod = _with_bus(monkeypatch, _RecordingBus())
    entered = asyncio.Event(); release = asyncio.Event()

    async def _slow_topic(*a, **kw):
        entered.set()
        await release.wait()
        return 42

    channel.open_engagement_topic = AsyncMock(side_effect=_slow_topic)
    task = asyncio.ensure_future(_launch(engage_executor))
    await asyncio.wait_for(entered.wait(), 5)           # the topic is being created; no record yet
    # The whole stop ran meanwhile: its ledger walk could not see this launch,
    # so it latched, drained and marked. The handler then creates its record
    # and enrols it — enrolment after the latch cancels it at once.
    registry.begin_launch_shutdown()
    registry.mark_launch_drains_complete()
    with caplog.at_level(logging.ERROR):
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        await asyncio.sleep(0)
    assert not tools_mod._LAUNCH_DEATH_TASKS
    assert not tools_mod._LAUNCH_TURN_TASKS
    created_id = next(iter(registry._records))
    assert registry.get(created_id).status == "active"   # left live for boot
    assert any("after the stop's drains completed" in r.getMessage() for r in caplog.records)


# --- diff round 2 (Terra S1): a cancellation inside the uncommitted telling tells once --

async def test_an_owner_cancelled_inside_its_uncommitted_telling_tells_once(tmp_path, monkeypatch):
    probe = _Probe()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    real_write = registry._write_tombstone

    def _boom_on_terminal(snapshot):
        if any(row.get("status") == "error" for row in snapshot):
            raise OSError("tombstone write refused")
        return real_write(snapshot)

    monkeypatch.setattr(registry, "_write_tombstone", _boom_on_terminal)
    entered = asyncio.Event(); release = asyncio.Event()

    class _BlockingBus(_RecordingBus):
        async def notify(self, msg):
            entered.set()
            await release.wait()
            self.messages.append(msg)
            return True

    bus = _BlockingBus()
    tools_mod = _with_bus(monkeypatch, bus)

    await _launch(engage_executor)
    await asyncio.wait_for(entered.wait(), 5)          # the uncommitted telling is in flight
    owner = next(iter(tools_mod._LAUNCH_TURN_TASKS))
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    release.set()
    await _drain_all(tools_mod)

    created_id = next(iter(registry._records))
    assert [m.content.kind for m in bus.messages] == ["launch_outcome_uncommitted"]   # ONE
    assert channel._post_engagement_notice.await_count == 1                          # one bounded notice
    assert registry.get(created_id).status == "active"
    assert channel.close_topic.await_count == 0


# --- diff round 2 (Astra J2): a named-fault arm reached after the drains mints nothing --

async def test_an_inline_abort_reached_after_the_drains_mints_nothing(tmp_path, monkeypatch, caplog):
    probe = _Probe()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)
    # The plugin snapshot moves while the record is being created, so the FIRST
    # arm after enrolment — `plugin_superseded`, with no suspension between the
    # two — fires before the enrolment's cancellation has been delivered.
    gens = iter([1, 1, 2, 2, 2, 2])       # stable at create (1 == 1), moved by the recheck (2 != 1)
    monkeypatch.setattr(tools_mod.plugin_registry, "snapshot_generation", lambda: next(gens))
    entered = asyncio.Event(); release = asyncio.Event()

    async def _slow_topic(*a, **kw):
        entered.set()
        await release.wait()
        return 42

    channel.open_engagement_topic = AsyncMock(side_effect=_slow_topic)
    task = asyncio.ensure_future(_launch(engage_executor))
    await asyncio.wait_for(entered.wait(), 5)
    registry.begin_launch_shutdown()          # the whole stop ran while the topic was being created
    registry.mark_launch_drains_complete()
    with caplog.at_level(logging.ERROR):
        release.set()                          # enrolment cancels; the arm runs before the cancel lands
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        await asyncio.sleep(0)
    assert not tools_mod._LAUNCH_DEATH_TASKS     # no abort, no compensator
    assert not tools_mod._LAUNCH_TURN_TASKS
    created_id = next(iter(registry._records))
    assert registry.get(created_id).status == "active"
    assert bus.messages == [] and channel.close_topic.await_count == 0
    assert any("plugin_superseded abort after the stop's drains completed" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


# --- diff round 3 (Terra S1): the replay keeps its configured fallback role ------------

async def test_the_boot_replay_addresses_a_roleless_record_to_the_configured_assistant(tmp_path):
    import casa_core
    from engagement_registry import EngagementRegistry
    registry = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await registry.create(kind="executor", role_or_type="configurator", driver="in_casa",
                                topic_id=7, origin={"channel": "telegram", "cid": "c", "chat_id": "1"},
                                task="t")
    assert await registry.try_transition_terminal(
        rec.id, "error", strict=True, error_kind="x", error_message="m",
        owes_terminal_notification=True)
    bus = _RecordingBus(roles=("ellen",))                 # the deployment's assistant is `ellen`
    await casa_core._replay_one_engagement_outcome(registry, bus, registry.get(rec.id), assistant_role="ellen")
    assert [m.target for m in bus.messages] == ["ellen"]
    assert bus.messages[0].content.kind == "error"
    await bus.deliver_all()
    assert registry.records_owing_terminal_notification() == []


# --- diff round 3 (Astra J2): a drain sees what a queued done callback is about to mint ---

async def test_the_drain_awaits_the_telling_a_just_completed_abort_is_about_to_mint(tmp_path, monkeypatch):
    probe = _Probe()
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_engagement_driver", None, raising=False)
    entered = asyncio.Event(); release = asyncio.Event(); closing = asyncio.Event()

    async def _close(*, thread_id):
        entered.set()
        await release.wait()
        closing.set()           # wakes the test BEFORE this abort task completes its step
        probe.events.append("topic_close")

    channel.close_topic = AsyncMock(side_effect=_close)
    task = asyncio.ensure_future(_launch(engage_executor))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()                                     # the launcher abandons its inline abort
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await closing.wait()
    # Here the abort task is done and its done callbacks (the telling's
    # transfer among them) are queued but have not run: the drain's snapshot
    # sees nothing pending. It must still come back with the telling done.
    abort = [x for x in tools_mod._LAUNCH_DEATH_TASKS if x.done()]
    assert abort, "the window this case models: a completed abort with queued callbacks"
    await tools_mod.drain_launch_death_reports()
    assert [m.content.kind for m in bus.messages] == ["no_driver"]
    assert not [x for x in tools_mod._LAUNCH_DEATH_TASKS if not x.done()]


# --- diff round 4 (Astra): the pre-record topic abort is drained, and fenced after the mark --

async def _cancel_between_topic_and_record(tmp_path, monkeypatch, probe):
    engage_executor, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    entered = asyncio.Event(); release = asyncio.Event()
    real_create = registry.create

    async def _slow_create(*a, **kw):
        entered.set()
        await release.wait()
        return await real_create(*a, **kw)

    monkeypatch.setattr(registry, "create", _slow_create)
    task = asyncio.ensure_future(_launch(engage_executor))
    await asyncio.wait_for(entered.wait(), 5)         # the topic exists (42); the record does not
    return task, registry, channel


async def test_the_stop_drains_a_pre_record_topic_abort(tmp_path, monkeypatch):
    probe = _Probe()
    task, registry, channel = await _cancel_between_topic_and_record(tmp_path, monkeypatch, probe)
    import tools as tools_mod
    closing = asyncio.Event(); release_close = asyncio.Event()

    async def _slow_close(*, thread_id):
        closing.set()
        await release_close.wait()
        probe.events.append("topic_close")

    channel.close_topic = AsyncMock(side_effect=_slow_close)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(closing.wait(), 5)         # the topic abort is in flight, in no ledger
    stop = asyncio.ensure_future(tools_mod.stop_engagement_launches(registry))
    await asyncio.sleep(0.05)
    assert not stop.done()                            # the stop waits for it
    release_close.set()
    await asyncio.wait_for(stop, 5)
    assert channel.close_topic.await_count == 1
    assert not [x for x in tools_mod._ABORT_BG_TASKS if not x.done()]
    assert registry._records == {}                    # never had a record


async def test_a_pre_record_cancellation_after_the_drains_mints_nothing(tmp_path, monkeypatch, caplog):
    probe = _Probe()
    task, registry, channel = await _cancel_between_topic_and_record(tmp_path, monkeypatch, probe)
    import tools as tools_mod
    registry.begin_launch_shutdown()
    registry.mark_launch_drains_complete()
    with caplog.at_level(logging.ERROR):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
    assert not tools_mod._ABORT_BG_TASKS
    assert channel.close_topic.await_count == 0
    assert any("before its record existed, after the stop's drains completed" in r.getMessage()
               for r in caplog.records)


# --- diff round 5 (Astra J2): the anchors drain to JOINT quiescence --------------------

async def test_the_stop_returns_only_when_no_anchor_has_pending_work(tmp_path, monkeypatch):
    """A task in the pre-record abort set completes and, in completing, mints
    a task into the reporter set — the shape of a launcher released by a
    finished topic abort whose named abort then tells. Three sequential drains
    would have drained the reporter set first and marked with this reporter
    pending."""
    import tools as tools_mod
    from engagement_registry import EngagementRegistry
    registry = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    release = asyncio.Event(); told: list = []

    async def _late_telling():
        await asyncio.sleep(0.05)
        told.append(True)

    async def _pre_record_abort():
        await release.wait()
        tell = asyncio.ensure_future(_late_telling())      # minted into the OTHER anchor
        tools_mod._LAUNCH_DEATH_TASKS.add(tell)
        tell.add_done_callback(tools_mod._LAUNCH_DEATH_TASKS.discard)

    abort = asyncio.ensure_future(_pre_record_abort())
    tools_mod._ABORT_BG_TASKS.add(abort)
    abort.add_done_callback(tools_mod._ABORT_BG_TASKS.discard)

    stop = asyncio.ensure_future(tools_mod.stop_engagement_launches(registry))
    await asyncio.sleep(0.02)
    assert not stop.done()
    release.set()
    await asyncio.wait_for(stop, 5)
    assert told == [True]                                       # the stop waited for it
    assert not [x for x in tools_mod._LAUNCH_DEATH_TASKS if not x.done()]
    assert registry.launch_drains_complete() is True
