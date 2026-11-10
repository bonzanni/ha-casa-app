"""INV-ENG-021 (design 2026-09-15 §2.A): the engager is not held.

On the production box (2026-09-15) Ellen's tool call was held for three minutes because
the engagement launcher (then the configurator's ``engage_executor``) awaited the
whole in-casa launch turn inline. Now the launch opens the job's session, queues its
brief and returns ``pending``; the session's reader owns the job from there. A launch
that dies after its record exists — cancelled while its session opens, or caught by
the graceful stop — is reported by the cancellation owner with the engager's
obligation armed and told live; a stop latched during ``open()``, a persist failure
and a lost terminal race each have an owner of their own. On the real registry,
driver and tool.
"""
from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from test_in_casa_launch_terminal_artifact import (
    ScriptedCompleteClient, ScriptedCutoffClient, _Probe, _build, _launch,
)
from test_launch_death_reporter import _cancel_while_opening


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


class GatedClient(ScriptedCompleteClient):
    """Blocks before its launch turn's first frame until the test releases it;
    the turn then runs to its end."""
    gate: asyncio.Event | None = None

    async def receive_response(self):
        await type(self).gate.wait()
        async for frame in super().receive_response():
            yield frame


async def _until(predicate, timeout: float = 5.0) -> None:
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), timeout)


def _hang_open(driver, monkeypatch):
    """Make the job session's open never return; the returned event is set
    once the launcher is inside it (its record exists)."""
    entered = asyncio.Event()

    async def _hang(engagement, options=None):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(driver, "open", _hang)
    return entered


def _with_bus(monkeypatch, bus):
    import tools as tools_mod
    monkeypatch.setattr(tools_mod, "_bus", bus)
    return tools_mod


async def _drain_all(tools_mod):
    await tools_mod.drain_launch_death_reports()
    await asyncio.sleep(0)


# --- red case 1: pending before the first frame ---------------------------------

async def test_the_engager_gets_pending_before_the_launch_turn_starts(tmp_path, monkeypatch):
    probe = _Probe()
    GatedClient.gate = asyncio.Event()
    launcher, registry, channel, driver = _build(tmp_path, monkeypatch, probe, GatedClient)
    tools_mod = _with_bus(monkeypatch, _RecordingBus())

    # bounded: an inline launch would wait on the gated first frame for ever,
    # and a hang is not a pin
    envelope = await asyncio.wait_for(_launch(launcher), 5)
    payload = json.loads(envelope["content"][0]["text"])
    assert payload["status"] == "pending", payload
    assert GatedClient.frames_yielded == 0          # the turn has not begun
    created_id = next(iter(registry._records))
    assert registry.get(created_id).status == "active"
    assert driver.is_alive(registry.get(created_id)) is True

    GatedClient.gate.set()
    await _until(lambda: probe.finalize_count == 1)
    assert GatedClient.frames_yielded == 4
    # the session's reader ran the turn; the launch reported nothing
    assert registry.get(created_id).status == "active"
    assert probe.notice_texts == []
    await driver.cancel(registry.get(created_id))


# --- a dropped telling stays owed ------------------------------------------------

async def test_a_dropped_bus_target_leaves_the_obligation_owed(tmp_path, monkeypatch):
    probe = _Probe()
    launcher, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus(roles=())          # the engager's role is not registered
    tools_mod = _with_bus(monkeypatch, bus)

    await _cancel_while_opening(launcher, driver, monkeypatch)
    await _drain_all(tools_mod)

    created_id = next(iter(registry._records))
    assert registry.get(created_id).status == "error"
    assert bus.messages == []
    assert [r.id for r in registry.records_owing_terminal_notification()] == [created_id]


# --- red case 3: cancelling the tool call after return does not touch the turn -

async def test_cancelling_the_tool_task_after_return_does_not_cancel_the_turn(tmp_path, monkeypatch):
    probe = _Probe()
    GatedClient.gate = asyncio.Event()
    launcher, registry, channel, driver = _build(tmp_path, monkeypatch, probe, GatedClient)
    _with_bus(monkeypatch, _RecordingBus())

    task = asyncio.ensure_future(_launch(launcher))
    envelope = await task
    assert json.loads(envelope["content"][0]["text"])["status"] == "pending"
    task.cancel()                           # a finished task: nothing to cancel
    GatedClient.gate.set()
    await _until(lambda: probe.finalize_count == 1)
    assert GatedClient.frames_yielded == 4   # the turn ran to its end regardless
    assert GatedClient.result_messages_yielded == 1
    created_id = next(iter(registry._records))
    assert registry.get(created_id).status == "active"
    await driver.cancel(registry.get(created_id))


# --- the graceful stop drains a running launch -----------------------------------

async def test_the_stop_drains_a_running_owner_and_reports_the_cancellation(tmp_path, monkeypatch):
    probe = _Probe()
    launcher, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)
    entered = _hang_open(driver, monkeypatch)

    task = asyncio.ensure_future(_launch(launcher))
    await asyncio.wait_for(entered.wait(), 5)     # the record exists; the session opens

    # bounded: a stop that cannot find the launch (not enrolled) would wait on
    # the hung open for ever, and a hang is not a pin
    await asyncio.wait_for(tools_mod.stop_engagement_launches(registry), 5)
    with pytest.raises(asyncio.CancelledError):
        await task

    created_id = next(iter(registry._records))
    rec = registry.get(created_id)
    assert rec.status == "error"
    assert rec.origin["error_kind"] == "launch_cancelled"
    assert rec.origin.get("shutdown_reason")
    assert probe.notice_texts == ["⚠️ stopped before reporting a result"]       # one line in the job's log
    assert channel.close_topic.await_count == 0   # no topic: none was opened
    assert [m.content.kind for m in bus.messages] == ["launch_cancelled"]
    assert registry.launch_drains_complete() is True


# --- red case 5b / 5h: a stop latched during open() mints no owner ----------------

async def test_a_stop_latched_during_open_mints_no_owner(tmp_path, monkeypatch):
    probe = _Probe()
    launcher, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
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

    envelope = await _launch(launcher)
    payload = json.loads(envelope["content"][0]["text"])
    assert payload["status"] == "error", payload
    assert payload["kind"] == "launch_cancelled"
    assert ScriptedCutoffClient.frames_yielded == 0     # the brief was never queued
    await tools_mod.drain_launch_death_reports()
    created_id = next(iter(registry._records))
    assert registry.get(created_id).status == "error"
    assert probe.notice_texts == ["⚠️ stopped before reporting a result"]
    assert channel.close_topic.await_count == 0
    assert registry.get(created_id).terminal_notification_pending is False  # acked on return
    assert bus.messages == []                          # the envelope was the telling
    assert all(t.done() or t is asyncio.current_task() for t in asyncio.all_tasks())


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
    launcher, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_engagement_driver", None, raising=False)

    envelope = await _launch(launcher)
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


# --- red case 6: a specialist-hosted job's engagement launch has the same shape ---

async def test_a_job_engagement_launch_returns_pending_before_its_turn(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from tools import init_tools
    from test_delegate_to_agent_interactive import (
        _make_alex_cfg, _make_assistant_cfg, launch_job)
    probe = _Probe()
    GatedClient.gate = asyncio.Event()
    _exec, registry, channel, driver = _build(tmp_path, monkeypatch, probe, GatedClient)
    cm = MagicMock(); cm.get = MagicMock(return_value=channel)
    specialist_reg = MagicMock(); specialist_reg.get.return_value = _make_alex_cfg()
    init_tools(
        channel_manager=cm, bus=MagicMock(), specialist_registry=specialist_reg,
        mcp_registry=MagicMock(), trigger_registry=MagicMock(),
        engagement_registry=registry,
        agent_role_map={"assistant": _make_assistant_cfg()},
    )
    _with_bus(monkeypatch, _RecordingBus())
    res = await asyncio.wait_for(launch_job("Plan Q2"), 5)
    payload = json.loads(res["content"][0]["text"])
    assert payload["status"] == "pending", payload
    assert GatedClient.frames_yielded == 0
    GatedClient.gate.set()
    await _until(lambda: probe.finalize_count == 1)
    assert GatedClient.frames_yielded == 4
    created_id = next(iter(registry._records))
    assert registry.get(created_id).status == "active"     # the job runs on
    assert probe.notice_texts == []
    await driver.cancel(registry.get(created_id))


# --- red case 5e: an inline abort whose launcher is cancelled before its return ----

async def test_an_inline_abort_cancelled_before_its_return_is_told_live(tmp_path, monkeypatch):
    probe = _Probe()
    launcher, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_engagement_driver", None, raising=False)
    entered = asyncio.Event(); release = asyncio.Event()
    settled: list = []
    real_settle = tools_mod._settle_job_journal

    async def _blocked_settle(rec, driver):
        entered.set()
        await release.wait()
        await real_settle(rec, driver)
        settled.append(rec.id)

    monkeypatch.setattr(tools_mod, "_settle_job_journal", _blocked_settle)

    task = asyncio.ensure_future(_launch(launcher))
    await asyncio.wait_for(entered.wait(), 5)       # the strict flip committed; the abort is wedged
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await _drain_all(tools_mod)

    created_id = next(iter(registry._records))
    rec = registry.get(created_id)
    assert rec.status == "error" and rec.origin["error_kind"] == "no_driver"
    assert settled == [created_id]                          # the anchored abort finished
    # no envelope was returned, so the cancellation owner told the engager live
    assert [m.content.kind for m in bus.messages] == ["no_driver"]
    assert rec.terminal_notification_pending is True
    await bus.deliver_all()
    assert registry.get(created_id).terminal_notification_pending is False


# --- diff round 1 (Astra J1.1): the cancellation owner tells a persist failure --------

async def test_a_stop_whose_cancellation_report_cannot_persist_still_tells(tmp_path, monkeypatch):
    probe = _Probe()
    launcher, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)
    real_write = registry._write_tombstone

    def _boom_on_terminal(snapshot):
        if any(row.get("status") == "error" for row in snapshot):
            raise OSError("tombstone write refused")
        return real_write(snapshot)

    monkeypatch.setattr(registry, "_write_tombstone", _boom_on_terminal)
    entered = _hang_open(driver, monkeypatch)

    task = asyncio.ensure_future(_launch(launcher))
    await asyncio.wait_for(entered.wait(), 5)
    await asyncio.wait_for(tools_mod.stop_engagement_launches(registry), 5)
    with pytest.raises(asyncio.CancelledError):
        await task

    created_id = next(iter(registry._records))
    rec = registry.get(created_id)
    assert rec.status == "active"                                  # rolled back, live
    assert probe.notice_texts == ["⚠️ could not be recorded"]
    assert channel.close_topic.await_count == 0
    assert [m.content.kind for m in bus.messages] == ["launch_outcome_uncommitted"]
    assert rec.terminal_notification_pending is False


# --- diff round 1 (Astra J1.2): a lost race tells nobody, however armed the record ----

async def test_a_cancellation_owner_that_loses_to_a_telling_writer_sends_nothing(tmp_path, monkeypatch):
    probe = _Probe()
    launcher, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)
    entered = _hang_open(driver, monkeypatch)

    task = asyncio.ensure_future(_launch(launcher))
    await asyncio.wait_for(entered.wait(), 5)
    created_id = next(iter(registry._records))
    # The funnel (the operator cancelled the engagement) wins the terminal and
    # ARMS its own telling, whose delivery is still pending when the stop lands.
    won = await registry.try_transition_terminal(
        created_id, "cancelled", strict=True, owes_terminal_notification=True)
    assert won is True
    await asyncio.wait_for(tools_mod.stop_engagement_launches(registry), 5)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert bus.messages == []                                       # the winner's to tell
    rec = registry.get(created_id)
    assert rec.status == "cancelled"
    assert rec.terminal_notification_pending is True                # still the funnel's
    assert probe.notice_texts == []
    assert channel.close_topic.await_count == 0


# --- diff round 1 (Astra J3): a losing inline abort acknowledges nothing ------------

async def test_a_losing_inline_abort_does_not_clear_another_writers_obligation(tmp_path, monkeypatch):
    probe = _Probe()
    launcher, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
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

    envelope = await _launch(launcher)
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
    launcher, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    tools_mod = _with_bus(monkeypatch, _RecordingBus())
    entered = asyncio.Event(); release = asyncio.Event()
    real_create = registry.create

    async def _slow_create(*a, **kw):
        entered.set()
        await release.wait()
        return await real_create(*a, **kw)

    monkeypatch.setattr(registry, "create", _slow_create)
    task = asyncio.ensure_future(_launch(launcher))
    await asyncio.wait_for(entered.wait(), 5)           # no record yet
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
    created_id = next(iter(registry._records))
    assert registry.get(created_id).status == "active"   # left live for boot
    assert any("after the stop's drains completed" in r.getMessage() for r in caplog.records)


# --- diff round 2 (Astra J2): a named-fault arm reached after the drains mints nothing --

async def test_an_inline_abort_reached_after_the_drains_mints_nothing(tmp_path, monkeypatch, caplog):
    """A launcher enrolled after the stop's ledger walk is cancelled at
    enrolment, but the cancellation is only DELIVERED at its next suspension;
    a named-fault arm reached before then must mint nothing. (The configurator's
    launcher reached ``plugin_superseded`` with no suspension in between; the
    job launcher suspends at every step before its arms, so the window is built
    directly: enrol after the mark, then take the named arm synchronously.)"""
    probe = _Probe()
    launcher, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)
    rec = await registry.create(
        kind="specialist", role_or_type="finance", driver="in_casa", topic_id=42,
        origin={"role": "assistant", "channel": "telegram", "chat_id": "c1", "cid": "x"},
        task="t")
    registry.begin_launch_shutdown()          # the whole stop ran before this launch enrolled
    registry.mark_launch_drains_complete()

    async def _launch_reaching_its_arm():
        handle = tools_mod._register_launch_safe(rec.id)   # enrolment cancels; not yet delivered
        try:
            return await tools_mod._abort_launch_inline(
                channel, rec, 42, kind="plugin_superseded", message="moved")
        finally:
            tools_mod._unregister_launch_safe(handle)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.ensure_future(_launch_reaching_its_arm()), 5)
        await asyncio.sleep(0)
    assert not tools_mod._LAUNCH_DEATH_TASKS     # no abort, no compensator
    assert registry.get(rec.id).status == "active"
    assert bus.messages == [] and channel.close_topic.await_count == 0
    assert any("plugin_superseded abort after the stop's drains completed" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


# --- diff round 3 (Terra S1): the replay keeps its configured fallback role ------------

async def test_the_boot_replay_addresses_a_roleless_record_to_the_configured_assistant(tmp_path):
    import casa_core
    from engagement_registry import EngagementRegistry
    registry = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    rec = await registry.create(kind="specialist", role_or_type="finance", driver="in_casa",
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
    """The window: an anchored abort has COMPLETED, and the done callback that
    transfers its telling (the real one, `_spawn_abandoned_inline_telling`) is
    queued but has not run. A drain that returned on that snapshot let the stop
    mark with the telling still to be minted.

    The window is built deterministically rather than borrowed from the real
    abort's remaining steps: a task created here runs on the next loop
    iteration and completes, scheduling its done callbacks at the END of the
    ready queue — behind this coroutine's own `sleep(0)` wake-up, which was
    queued first. (An earlier version relied on the real abort finishing in
    the same step as its topic close; in CI a failing topic-ledger append
    added a suspension and the precondition did not hold.)
    """
    import tools as tools_mod
    from engagement_registry import EngagementRegistry
    from tools import LaunchAbortResult
    registry = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    monkeypatch.setattr(tools_mod, "_engagement_registry", registry)
    bus = _RecordingBus()
    tools_mod = _with_bus(monkeypatch, bus)
    rec = await registry.create(kind="specialist", role_or_type="finance", driver="in_casa",
                                topic_id=42, origin={"role": "assistant", "channel": "telegram",
                                                     "cid": "c", "chat_id": "1"}, task="t")
    # the abort WON its transition and armed the obligation, as the inline arm does
    assert await registry.try_transition_terminal(
        rec.id, "error", strict=True, error_kind="no_driver", error_message="m",
        owes_terminal_notification=True)

    async def _abort_already_done():
        return LaunchAbortResult.ABORTED

    abort = asyncio.ensure_future(_abort_already_done())
    tools_mod._LAUNCH_DEATH_TASKS.add(abort)
    abort.add_done_callback(tools_mod._LAUNCH_DEATH_TASKS.discard)
    # the launcher was cancelled: it handed the telling to the abort's completion
    abort.add_done_callback(
        lambda t: tools_mod._spawn_abandoned_inline_telling(
            t, rec, MagicMock(), kind="no_driver", detail="m",
            won=lambda r: r is LaunchAbortResult.ABORTED))
    await asyncio.sleep(0)
    assert abort.done(), "the window this case models: a completed abort with queued callbacks"
    assert bus.messages == []                       # the callback has not run yet
    await tools_mod.drain_launch_death_reports()
    assert [m.content.kind for m in bus.messages] == ["no_driver"]
    assert not [x for x in tools_mod._LAUNCH_DEATH_TASKS if not x.done()]


# --- a launch that ends before its record exists leaves nothing behind -----------------

async def test_a_cancellation_before_the_record_leaves_nothing_to_drain(tmp_path, monkeypatch):
    """No topic is opened at launch, so a launch cancelled before its record
    exists has nothing to undo: no abort task, no record, and the stop does
    not wait on anything of it."""
    probe = _Probe()
    launcher, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    import tools as tools_mod
    entered = asyncio.Event(); release = asyncio.Event()
    real_create = registry.create

    async def _slow_create(*a, **kw):
        entered.set()
        await release.wait()
        return await real_create(*a, **kw)

    monkeypatch.setattr(registry, "create", _slow_create)
    task = asyncio.ensure_future(_launch(launcher))
    await asyncio.wait_for(entered.wait(), 5)         # the record does not exist yet
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not [x for x in tools_mod._ABORT_BG_TASKS if not x.done()]
    await asyncio.wait_for(tools_mod.stop_engagement_launches(registry), 5)
    assert registry._records == {}
    assert channel.open_engagement_topic.await_count == 0
    assert probe.log_lines == []


async def test_a_failure_before_the_record_leaves_nothing_to_drain(tmp_path, monkeypatch):
    """The record's strict create fails: the launch returns its named fault
    and leaves no abort task and no record."""
    probe = _Probe()
    launcher, registry, channel, driver = _build(tmp_path, monkeypatch, probe, ScriptedCutoffClient)
    import tools as tools_mod

    async def _failing_create(*a, **kw):
        raise OSError("persist sentinel")

    monkeypatch.setattr(registry, "create", _failing_create)
    envelope = await asyncio.wait_for(_launch(launcher), 5)
    payload = json.loads(envelope["content"][0]["text"])
    assert payload["kind"] == "record_persist_failed", payload
    assert not [x for x in tools_mod._ABORT_BG_TASKS if not x.done()]
    assert registry._records == {}
    assert channel.open_engagement_topic.await_count == 0


# --- diff round 5 (Astra J2): the anchors drain to JOINT quiescence --------------------

async def test_the_stop_returns_only_when_no_anchor_has_pending_work(tmp_path, monkeypatch):
    """A task in the pre-record abort set completes and, in completing, mints
    a task into the reporter set — the shape of a launcher released by a
    finished topic abort whose named abort then tells. Three sequential drains
    would have drained the reporter set first and marked with this reporter
    pending."""
    import tools as tools_mod
    from engagement_registry import EngagementRegistry
    registry = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
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
