"""Tests for the shared _finalize_engagement helper in tools.py."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class TestFinalizeEngagement:
    async def test_happy_path_closes_topic_and_notifies_ellen(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t",
            origin={"role": "assistant", "channel": "telegram", "chat_id": "12345"},
            topic_id=42,
        )

        telegram = MagicMock()
        telegram.send_to_topic = AsyncMock()
        telegram.send_response_to_topic = AsyncMock()
        telegram.close_topic = AsyncMock()
        cm = MagicMock()
        cm.get.return_value = telegram
        bus = MagicMock()
        bus.notify = AsyncMock()

        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        driver = MagicMock()
        driver.cancel = AsyncMock()

        await _finalize_engagement(
            rec, outcome="completed", text="summary", artifacts=["sha1"],
            next_steps=[], driver=driver,
        )

        # Topic closed + icon flipped
        telegram.close_topic.assert_awaited_once_with(thread_id=42)
        # Completion message posted in topic
        telegram.send_response_to_topic.assert_awaited()  # v0.109.0 rich summary
        # NOTIFICATION sent to Ellen
        bus.notify.assert_awaited_once()
        # Driver cancelled
        driver.cancel.assert_awaited_once_with(rec)
        # Record status is completed
        assert rec.status == "completed"
        assert rec.completed_at is not None

    async def test_spool_drain_precedes_topic_close(self, tmp_path):
        """v0.79.0 (§3): the pre-close inbound spool drain runs BEFORE the
        topic is closed (so pending receipts/notices post while the topic is
        still open), and the terminal transition is STRICT."""
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="hello", driver="claude_code",
            task="t",
            origin={"role": "assistant", "channel": "telegram", "chat_id": "1"},
            topic_id=42,
        )

        order: list[str] = []
        telegram = MagicMock()
        telegram.send_to_topic = AsyncMock()
        telegram.send_response_to_topic = AsyncMock()

        async def _close(*, thread_id):
            order.append("close_topic")
        telegram.close_topic = AsyncMock(side_effect=_close)
        cm = MagicMock()
        cm.get.return_value = telegram
        bus = MagicMock()
        bus.notify = AsyncMock()

        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        driver = MagicMock()
        driver.cancel = AsyncMock()

        async def _drain(engagement):
            order.append("drain")
        driver.drain_inbound_spool = AsyncMock(side_effect=_drain)

        await _finalize_engagement(
            rec, outcome="completed", text="s", artifacts=[], next_steps=[],
            driver=driver,
        )

        assert order == ["drain", "close_topic"]
        driver.drain_inbound_spool.assert_awaited_once_with(rec)
        assert rec.status == "completed"

    async def test_cancel_outcome_uses_cancel_path(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )

        telegram = MagicMock()
        telegram.send_to_topic = AsyncMock()
        telegram.send_response_to_topic = AsyncMock()
        telegram.close_topic = AsyncMock()
        cm = MagicMock()
        cm.get.return_value = telegram
        bus = MagicMock()
        bus.notify = AsyncMock()

        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        driver = MagicMock()
        driver.cancel = AsyncMock()

        await _finalize_engagement(
            rec, outcome="cancelled", text="user cancelled",
            artifacts=[], next_steps=[], driver=driver,
        )
        assert rec.status == "cancelled"
        driver.cancel.assert_awaited_once_with(rec)


async def test_finalize_writes_retention_for_claude_code_driver(
    tmp_path, monkeypatch,
):
    """Plan 4a.1: _finalize_engagement must update .casa-meta.json with
    retention_until = now + 7 days when driver=='claude_code' and a
    workspace dir exists."""
    import json
    from pathlib import Path
    import tools as tools_mod
    from engagement_registry import EngagementRecord, EngagementRegistry
    from drivers.workspace import casa_meta_path, provision_control_dir, write_casa_meta
    from tools import _finalize_engagement

    ws = tmp_path / "eng1"
    ws.mkdir()
    # Task 4 (containment stage 2): .casa-meta.json lives in the control dir.
    provision_control_dir("eng1")
    write_casa_meta(
        workspace_path=str(ws), engagement_id="eng1",
        executor_type="hello-driver", status="UNDERGOING",
        created_at="2026-04-23T08:00:00Z",
        finished_at=None, retention_until=None,
    )

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "tomb.json"), bus=None)
    rec = EngagementRecord(
        id="eng1", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="active", topic_id=None,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={}, task="t",
    )
    reg._records["eng1"] = rec

    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_channel_manager", None)
    monkeypatch.setattr(tools_mod, "_bus", None)
    # Point the hardcoded /data/engagements path to tmp.
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    await _finalize_engagement(
        rec, outcome="completed", text="done",
        artifacts=[], next_steps=[], driver=None,
    )

    meta = json.loads(Path(casa_meta_path("eng1")).read_text())
    assert meta["status"] == "COMPLETED"
    assert meta["retention_until"] is not None
    # Parseable as ISO 8601 Z.
    import re
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                        meta["retention_until"])


async def test_finalize_preserves_real_allocated_uid_when_meta_unreadable(
    tmp_path, monkeypatch,
):
    """Fix-loop round 1 (Important): the terminal .casa-meta.json rewrite
    must persist the RECORD's allocated_uid, never a round-trip through
    ``load_casa_meta`` — that helper returns None/{} on I/O error,
    malformed/non-object JSON, or a refused legacy symlink, and falling
    back to ``meta.get("allocated_uid", ...)`` in any of those cases would
    silently regress a real uid to UNALLOCATED_UID in this rewrite,
    permanently orphaning that uid's casa-eng-<uid> passwd/group lines
    (the registry-less sweeper reads ONLY this field to decide what to
    prune)."""
    import json
    from pathlib import Path

    import tools as tools_mod
    from drivers.workspace import casa_meta_path, provision_control_dir, write_casa_meta
    from engagement_registry import EngagementRecord, EngagementRegistry
    from engagement_uids import UID_BASE
    from tools import _finalize_engagement

    ws = tmp_path / "eng-uid-fin"
    ws.mkdir()
    provision_control_dir("eng-uid-fin")
    write_casa_meta(
        workspace_path=str(ws), engagement_id="eng-uid-fin",
        executor_type="hello-driver", status="UNDERGOING",
        created_at="2026-04-23T08:00:00Z",
        finished_at=None, retention_until=None,
        allocated_uid=UID_BASE + 42,
    )

    # Simulate load_casa_meta returning None (unreadable/malformed/refused)
    # — exactly the fallback case the finding calls out. _finalize_engagement
    # does `from drivers.workspace import load_casa_meta` INSIDE the
    # function body, so the patch target is drivers.workspace's own
    # attribute, not a name on the tools module. If the fix regressed to
    # ``meta.get("allocated_uid", ...)``, this would make the finalize
    # rewrite fall back to UNALLOCATED_UID.
    import drivers.workspace as ws_mod
    monkeypatch.setattr(ws_mod, "load_casa_meta", lambda *a, **kw: None)

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "tomb.json"), bus=None)
    rec = EngagementRecord(
        id="eng-uid-fin", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="active", topic_id=None,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={}, task="t",
        allocated_uid=UID_BASE + 42,
    )
    reg._records["eng-uid-fin"] = rec

    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_channel_manager", None)
    monkeypatch.setattr(tools_mod, "_bus", None)
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    await _finalize_engagement(
        rec, outcome="completed", text="done",
        artifacts=[], next_steps=[], driver=None,
    )

    meta = json.loads(Path(casa_meta_path("eng-uid-fin")).read_text())
    assert meta["status"] == "COMPLETED"
    # The real uid must survive the terminal rewrite — this is what the
    # sweeper will read to decide which passwd/group entry to prune.
    assert meta["allocated_uid"] == UID_BASE + 42


class TestFinalizeU3Transition:
    """E-12 (v0.37.0) Task 23: terminal-state U3 flip from _finalize_engagement."""

    async def _make_setup(self, outcome, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="plugin-developer",
            driver="claude_code", task="t",
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "12345"},
            topic_id=42,
        )

        telegram = MagicMock()
        telegram.send_to_topic = AsyncMock()
        telegram.send_response_to_topic = AsyncMock()
        telegram.close_topic = AsyncMock()
        telegram.update_topic_state = AsyncMock()
        cm = MagicMock()
        cm.get.return_value = telegram
        bus = MagicMock()
        bus.notify = AsyncMock()

        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        driver = MagicMock()
        driver.cancel = AsyncMock()

        await _finalize_engagement(
            rec, outcome=outcome, text="x", artifacts=[],
            next_steps=[], driver=driver,
        )
        return telegram, rec

    async def test_completed_flips_topic_to_completed(self, tmp_path):
        telegram, rec = await self._make_setup("completed", tmp_path)
        telegram.update_topic_state.assert_awaited_once_with(
            engagement_id=rec.id, new_state="completed",
        )

    async def test_cancelled_flips_topic_to_cancelled(self, tmp_path):
        telegram, rec = await self._make_setup("cancelled", tmp_path)
        telegram.update_topic_state.assert_awaited_once_with(
            engagement_id=rec.id, new_state="cancelled",
        )

    async def test_error_flips_topic_to_failed(self, tmp_path):
        telegram, rec = await self._make_setup("error", tmp_path)
        telegram.update_topic_state.assert_awaited_once_with(
            engagement_id=rec.id, new_state="failed",
        )

    async def test_state_update_failure_does_not_block_close(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="plugin-developer",
            driver="claude_code", task="t",
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "12345"},
            topic_id=42,
        )

        telegram = MagicMock()
        telegram.send_to_topic = AsyncMock()
        telegram.send_response_to_topic = AsyncMock()
        telegram.close_topic = AsyncMock()
        telegram.update_topic_state = AsyncMock(
            side_effect=RuntimeError("telegram down"),
        )
        cm = MagicMock()
        cm.get.return_value = telegram

        init_tools(
            channel_manager=cm, bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        await _finalize_engagement(
            rec, outcome="completed", text="x", artifacts=[],
            next_steps=[], driver=None,
        )
        # Close still happened despite the state-update failure.
        telegram.close_topic.assert_awaited_once()
        assert rec.status == "completed"


class TestFinalizeEngagementBrokerCleanup:
    """v0.75.0 (W5/Sol B3,B4, r5-B6): _finalize_engagement must cancel_scope
    + drain_hooks IMMEDIATELY after winning the terminal transition, BEFORE
    the topic-close ops — so a pending ask's keyboard-edit finish-hook lands
    while the topic is still open, and a tap arriving after the terminal
    flip is rejected (stale)."""

    async def test_broker_cleanup_precedes_topic_close_and_taps_go_stale(
        self, tmp_path, monkeypatch,
    ):
        import verdict_broker
        from verdict_broker import VerdictBroker
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement, init_tools

        fresh_broker = VerdictBroker()
        monkeypatch.setattr(verdict_broker, "BROKER", fresh_broker)

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t",
            origin={"role": "assistant", "channel": "telegram", "chat_id": "1"},
            topic_id=42,
        )

        order: list[str] = []
        telegram = MagicMock()

        async def _send_to_topic(*a, **kw):
            order.append("send_to_topic")
            return 1

        async def _close_topic(*a, **kw):
            order.append("close_topic")

        telegram.send_to_topic = AsyncMock(side_effect=_send_to_topic)
        telegram.send_response_to_topic = AsyncMock(side_effect=_send_to_topic)
        telegram.close_topic = AsyncMock(side_effect=_close_topic)
        cm = MagicMock()
        cm.get.return_value = telegram
        bus = MagicMock()
        bus.notify = AsyncMock()

        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        # A pending engagement_ask on this engagement, with a finish hook
        # that records into the SAME order list.
        req, created = fresh_broker.register(
            namespace="engagement_ask", scope=rec.id, request_id="ask-1",
            timeout_s=5.0,
        )
        assert created is True

        async def _hook(outcome):
            order.append("keyboard_edit")

        fresh_broker.set_finish_hook(req, lambda outcome: _hook(outcome))

        driver = MagicMock()
        driver.cancel = AsyncMock()

        won = await _finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=driver,
        )
        assert bool(won) is True   # FinalizeResult.FINALIZED (D5)

        assert "keyboard_edit" in order
        assert order.index("keyboard_edit") < order.index("send_to_topic")
        assert order.index("keyboard_edit") < order.index("close_topic")

        # A tap arriving after the terminal flip is rejected (stale) — the
        # cancel_scope call already resolved (and retired) the request.
        claim = fresh_broker.claim(
            namespace="engagement_ask", scope=rec.id, request_id="ask-1",
            option_index=0, actor_id=1,
        )
        assert claim == "stale"

    async def test_broker_cleanup_swallows_drain_failure(
        self, tmp_path, monkeypatch,
    ):
        """A drain_hooks()/cancel_scope() failure must not abort the rest of
        the finalize funnel — the topic must still close."""
        import verdict_broker
        from engagement_registry import EngagementRegistry
        from tools import _finalize_engagement, init_tools

        class _ExplodingBroker:
            def cancel_scope(self, **kw):
                raise RuntimeError("broker down")

            async def drain_hooks(self):
                raise AssertionError("unreachable — cancel_scope raised first")

        monkeypatch.setattr(verdict_broker, "BROKER", _ExplodingBroker())

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )

        telegram = MagicMock()
        telegram.send_to_topic = AsyncMock()
        telegram.send_response_to_topic = AsyncMock()
        telegram.close_topic = AsyncMock()
        cm = MagicMock()
        cm.get.return_value = telegram
        bus = MagicMock()
        bus.notify = AsyncMock()

        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        won = await _finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=None,
        )
        assert bool(won) is True   # FinalizeResult.FINALIZED (D5)
        telegram.close_topic.assert_awaited_once()


async def test_finalize_preserves_plugin_artifacts_in_casa_meta(
        tmp_path, monkeypatch):
    """§3.8: the terminal .casa-meta.json rewrite must NOT drop the
    immutable plugin_artifacts recorded at engagement start."""
    import tools as tools_mod
    from drivers.workspace import load_casa_meta, provision_control_dir, write_casa_meta
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    eng_root = tmp_path / "engagements"
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(eng_root))

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="executor", role_or_type="plugin-developer",
        driver="claude_code", task="t",
        origin={"role": "assistant", "channel": "telegram", "chat_id": "1"},
        topic_id=42)

    ws = eng_root / rec.id
    ws.mkdir(parents=True)
    # Task 4 (containment stage 2): .casa-meta.json lives in the control dir.
    provision_control_dir(rec.id)
    artifacts = [{"name": "superpowers", "artifact_id": "a" * 64,
                  "path": "/config/plugins/store/superpowers/" + "a" * 64}]
    write_casa_meta(
        workspace_path=str(ws), engagement_id=rec.id,
        executor_type="plugin-developer", status="UNDERGOING",
        created_at="2026-07-13T00:00:00Z", finished_at=None,
        retention_until=None, plugin_artifacts=artifacts)

    telegram = MagicMock()
    telegram.send_to_topic = AsyncMock()
    telegram.close_topic = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = telegram
    init_tools(channel_manager=cm, bus=MagicMock(),
               specialist_registry=MagicMock(), mcp_registry=MagicMock(),
               trigger_registry=MagicMock(), engagement_registry=reg)
    driver = MagicMock()
    driver.cancel = AsyncMock()

    await _finalize_engagement(rec, outcome="completed", text="s",
                               artifacts=[], next_steps=[], driver=driver)

    meta = load_casa_meta(str(ws))
    assert meta["status"] == "COMPLETED"
    assert meta["plugin_artifacts"] == artifacts       # NOT dropped


# --- G4 D5 (v0.96.0): typed finalize result --------------------------------


def test_finalize_result_enum_exists():
    from tools import FinalizeResult
    assert {r.name for r in FinalizeResult} >= {
        "FINALIZED", "ALREADY_TERMINAL", "PRECONDITION_FAILED",
        "PERSIST_FAILED"}
    # Truthiness contract preserved for existing boolean callers: only a
    # won finalize is truthy.
    assert bool(FinalizeResult.FINALIZED) is True
    assert bool(FinalizeResult.ALREADY_TERMINAL) is False
    assert bool(FinalizeResult.PERSIST_FAILED) is False
    assert bool(FinalizeResult.PRECONDITION_FAILED) is False


def test_terminal_hook_abort_raises_and_leaves_live():
    """G4 D2: the registry evaluates the terminal hook INSIDE the mutation
    critical section; an abort leaves the record live."""
    import asyncio
    from engagement_registry import (
        EngagementRegistry, TerminalPreconditionFailed)

    async def run(tmp_path="/tmp"):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            reg = EngagementRegistry(tombstone_path=td + "/e.json", bus=None)
            rec = await reg.create(
                kind="executor", role_or_type="plugin-developer",
                driver="claude_code", task="t",
                origin={"role": "assistant"}, topic_id=1)
            calls = []
            def hook():
                calls.append(1)
                return "unread_inbound depth=1"
            try:
                await reg.try_transition_terminal(
                    rec.id, "completed", strict=True, terminal_hook=hook)
            except TerminalPreconditionFailed as exc:
                assert "unread_inbound" in str(exc)
            else:
                raise AssertionError("expected TerminalPreconditionFailed")
            assert calls == [1]
            assert rec.status not in ("completed", "cancelled", "error")
            # hook returning None proceeds
            won = await reg.try_transition_terminal(
                rec.id, "completed", strict=True, terminal_hook=lambda: None)
            assert won is True
            assert rec.status == "completed"
    asyncio.get_event_loop().run_until_complete(run()) if False else None
    asyncio.run(run())
