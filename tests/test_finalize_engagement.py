"""Tests for the shared _finalize_engagement helper in tools.py."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _driver_double():
    """A driver double shaped like the REAL engagement drivers.

    A bare ``MagicMock()`` fabricates EVERY attribute, so the funnel's
    ``hasattr`` probes find ``finalize_completion_post``, ``finalize_summary``,
    ``settle_all_open_questions`` and ``drain_inbound_spool`` — none of which
    ``InCasaDriver`` has — and awaiting their non-awaitable returns raises
    ``TypeError`` into the guards around each one.

    Before #678 that was invisible: the completion post's failure was swallowed
    and the funnel painted and closed regardless, so these tests asserted a
    close that only happened because the double was broken. Now a fabricated
    ``finalize_completion_post`` tells the funnel a sequencer post was
    ATTEMPTED and failed part-way, and it correctly declines to replay the
    summary. The double was always wrong; what changed is that it is no longer
    silent.
    """
    d = MagicMock()
    d.cancel = AsyncMock()
    for hook in ("finalize_completion_post", "finalize_summary",
                 "settle_all_open_questions", "drain_inbound_spool"):
        delattr(d, hook)
    return d


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

        driver = _driver_double()

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

        driver = _driver_double()

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

        driver = _driver_double()

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

        driver = _driver_double()

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

        driver = _driver_double()

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
    driver = _driver_double()

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


# ---------------------------------------------------------------------------
# #678 R2(i) red case — INV-ENG-013: the paint and the close wait for a
# CONFIRMED completion post
# ---------------------------------------------------------------------------


_EXPECTED_DISCLOSURE = (
    "\u26a0\ufe0f This engagement is recorded as {outcome}, but its completion "
    "summary could not be confirmed as posted here. The topic is left "
    "UNMARKED on purpose so the failure stays visible; it is closed like "
    "every finished topic, and it is deleted with everything in it at its "
    "retention deadline, so do not keep anything here. That the engagement "
    "ENDED is recorded durably; whether its summary reached the engager is a "
    "separate best-effort step this cannot speak for."
)
"""The disclosure's exact sentence, duplicated here rather than imported so a
reworded production string fails a test. "could not be CONFIRMED as posted",
never "could not be posted": a Telegram ``TimedOut`` can lose the
acknowledgement of a message the wire accepted (SEAM-2).

R2: it no longer says the topic is left OPEN, because it no longer is. The
seam round established that a deliberately-open terminal topic still ACCEPTS
messages while its retention ledger entry — appended unconditionally, before
this block ever runs — later deletes the topic and everything in it, so a note
typed there is lost. The close is therefore unconditional again and only the
outcome MARK is withheld, which carries the same failure signal; what the
notice now warns about is the retention deletion, which is true of every
terminal topic and is the thing the operator can still act on."""


class _FakeSequencer:
    """Only the two members ``ClaudeCodeDriver.finalize_completion_post``
    touches, plus a counted ``post_completion_notice`` whose return value is
    the confirmation the funnel must respect. ``None`` is the pinned SDK-side
    contract for a DEFINITE send failure (output_sequencer.py's #392/#332
    rule), and it is returned WITHOUT raising — which is exactly why a
    "did it raise?" predicate cannot see this route."""

    def __init__(self, confirm):
        self._confirm = confirm
        self.posts: list[str] = []
        self.drains = 0
        self.flushes = 0
        self.seals = 0

    async def await_completion_drain(self, rid):
        self.drains += 1
        return True

    async def flush_armed_intents(self):
        self.flushes += 1

    async def seal_narration(self):
        self.seals += 1

    async def post_completion_notice(self, text):
        self.posts.append(text)
        return self._confirm


async def _build_finalize(tmp_path, *, rich_returns=None, rich_raises=None,
                          sequencer=None):
    """Real registry + on-disk tombstone; a channel double whose senders are
    genuinely awaitable (a bare MagicMock fabricates every attribute and then
    raises TypeError when awaited, which is how eight tests in this repo came
    to assert the pre-fix paint-and-close over a topic that got nothing)."""
    import agent as agent_mod
    from engagement_registry import EngagementRegistry
    from tools import init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="t",
        origin={"role": "assistant", "channel": "telegram", "chat_id": "1"},
        topic_id=42,
    )

    events: list[str] = []
    telegram = MagicMock()

    async def _rich(thread_id, text, **kw):
        events.append("rich")
        if rich_raises is not None:
            raise rich_raises
        return rich_returns

    async def _plain(thread_id, text, **kw):
        events.append("plain")
        return 9001

    async def _paint(*, engagement_id, new_state):
        events.append(f"paint:{new_state}")

    async def _close(*, thread_id):
        events.append("close")

    telegram.send_response_to_topic = AsyncMock(side_effect=_rich)
    telegram.send_to_topic = AsyncMock(side_effect=_plain)
    telegram.update_topic_state = AsyncMock(side_effect=_paint)
    telegram.close_topic = AsyncMock(side_effect=_close)
    cm = MagicMock()
    cm.get.return_value = telegram
    bus = MagicMock()
    bus.notify = AsyncMock()
    agent_mod.active_semantic_memory = None

    init_tools(
        channel_manager=cm, bus=bus, specialist_registry=MagicMock(),
        mcp_registry=MagicMock(), trigger_registry=MagicMock(),
        engagement_registry=reg,
    )

    driver = MagicMock()
    driver.cancel = AsyncMock()
    if sequencer is None:
        # No sequencer hook at all — the pre-v0.79 direct-send route.
        del driver.finalize_completion_post
    else:
        from drivers.claude_code_driver import ClaudeCodeDriver

        async def _hook(engagement, summary_text):
            return await ClaudeCodeDriver.finalize_completion_post(
                _SeqHolder(sequencer), engagement, summary_text)
        driver.finalize_completion_post = _hook
    del driver.finalize_summary
    del driver.settle_all_open_questions
    del driver.drain_inbound_spool
    return reg, rec, telegram, bus, driver, events


class _AlwaysSeq(dict):
    """``self._sequencers`` for the holder below: ``.get(engagement_id)``
    resolves to the one scripted sequencer whatever the id."""

    def __init__(self, seq):
        super().__init__()
        self._seq = seq

    def get(self, key, default=None):
        return self._seq


class _SeqHolder:
    """Minimal ``self`` for the real, unbound
    ``ClaudeCodeDriver.finalize_completion_post`` — it reads only
    ``self._sequencers``."""

    def __init__(self, seq):
        self._sequencers = _AlwaysSeq(seq)


class TestFinalizeCompletionConfirmation:
    """INV-ENG-013: the funnel paints the topic's terminal state and closes it
    only after the completion post is CONFIRMED by the wire. An unconfirmed
    post leaves the topic UNMARKED after one bounded plain disclosure, and
    the post-topic tail runs either way. R2: the CLOSE is unconditional and
    bounded — only the outcome mark is withheld — because a deliberately
    open terminal topic still accepts messages that its already-appended
    retention entry later deletes.

    Pre-fix this is false two ways: the direct send's exception is swallowed
    and the paint and close proceed regardless; and on the sequencer route
    ``post_completion_notice`` returns ``None`` for a definite failure while
    ``finalize_completion_post`` discards that value and returns ``True``.
    """

    async def test_a_raising_direct_post_leaves_the_topic_unmarked(self, tmp_path):
        """MUTATION: make the paint-and-close unconditional → this fails
        (paint and close each become 1). MUTATION: set the confirmation flag
        before the await → this fails the same way."""
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_raises=RuntimeError("telegram 500"))

        won = await _finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert reg.get(rec.id).status == "completed"     # the work IS done
        assert telegram.send_response_to_topic.await_count == 1
        # ONE bounded plain disclosure, and it is the whole sentence.
        assert telegram.send_to_topic.await_count == 1
        assert (telegram.send_to_topic.await_args.args[1]
                == _EXPECTED_DISCLOSURE.format(outcome="completed"))
        # The topic is left UNPAINTED — the defect was painting ✅ over a
        # topic that heard nothing. It is still CLOSED, like every
        # terminal topic (R2).
        assert telegram.update_topic_state.await_count == 0
        assert telegram.close_topic.await_count == 1
        assert "paint:completed" not in events
        assert "close" in events
        # The post-topic tail is untouched.
        assert driver.cancel.await_count == 1
        assert bus.notify.await_count == 1

    async def test_an_unconfirmed_sequencer_post_falls_back_to_the_direct_send(
        self, tmp_path,
    ):
        """The route a "did it raise?" predicate is blind to: the real
        ``ClaudeCodeDriver.finalize_completion_post`` over a sequencer whose
        completion send reports a definite failure by returning ``None``.

        MUTATION: restore ``finalize_completion_post``'s unconditional
        ``return True`` → this fails (paint and close each become 1 and the
        disclosure never happens).
        """
        from tools import FinalizeResult, _finalize_engagement

        seq = _FakeSequencer(confirm=None)
        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_returns=777, sequencer=seq)

        won = await _finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert len(seq.posts) == 1                       # attempted once
        # A part-way sequencer failure must NOT be replayed through the rich
        # sender: post_completion_notice returning None means nothing landed,
        # so the direct send is the legitimate second attempt.
        assert telegram.send_response_to_topic.await_count == 1
        assert telegram.update_topic_state.await_count == 1
        assert telegram.close_topic.await_count == 1

    async def test_an_unconfirmed_sequencer_and_a_failing_direct_send(
        self, tmp_path,
    ):
        """Both routes fail: no paint, one close, one disclosure, tail
        intact."""
        from tools import FinalizeResult, _finalize_engagement

        seq = _FakeSequencer(confirm=None)
        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_raises=RuntimeError("telegram 500"), sequencer=seq)

        won = await _finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert len(seq.posts) == 1
        assert telegram.send_response_to_topic.await_count == 1
        assert telegram.send_to_topic.await_count == 1
        assert telegram.update_topic_state.await_count == 0
        assert telegram.close_topic.await_count == 1
        assert driver.cancel.await_count == 1
        assert bus.notify.await_count == 1

    async def test_a_confirmed_sequencer_post_paints_and_closes_once(
        self, tmp_path,
    ):
        """The CONTROL. A confirmed sequencer post skips the direct send and
        paints and closes exactly once each, with no disclosure."""
        from tools import FinalizeResult, _finalize_engagement

        seq = _FakeSequencer(confirm=555)
        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, sequencer=seq)

        won = await _finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert len(seq.posts) == 1
        assert telegram.send_response_to_topic.await_count == 0
        assert telegram.send_to_topic.await_count == 0
        assert telegram.update_topic_state.await_count == 1
        assert telegram.close_topic.await_count == 1
        assert events == ["paint:completed", "close"]
        assert driver.cancel.await_count == 1
        assert bus.notify.await_count == 1

    async def test_a_confirmed_direct_post_paints_and_closes_once(
        self, tmp_path,
    ):
        """The CONTROL for the no-sequencer route: a rich send that returns a
        message id is confirmation."""
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_returns=4242)

        won = await _finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert telegram.send_response_to_topic.await_count == 1
        assert telegram.send_to_topic.await_count == 0
        assert telegram.update_topic_state.await_count == 1
        assert telegram.close_topic.await_count == 1
        assert events == ["rich", "paint:completed", "close"]

    async def test_a_partway_sequencer_failure_is_not_replayed(self, tmp_path):
        """SEAM-1 (Sol): a sequencer post that RAISES may have posted some of
        its pages, so replaying the whole summary through the rich sender
        duplicates content and then paints and closes over it. A raise is
        therefore distinguished from an unconfirmed ``None``: no direct
        resend, straight to the bounded disclosure.

        MUTATION: treat a raise like ``False`` → the rich send count becomes 1
        and this fails.
        """
        from tools import FinalizeResult, _finalize_engagement

        class _RaisingSequencer(_FakeSequencer):
            async def post_completion_notice(self, text):
                self.posts.append(text)
                raise RuntimeError("page 2 of 4 failed")

        seq = _RaisingSequencer(confirm=None)
        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_returns=777, sequencer=seq)

        won = await _finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert len(seq.posts) == 1
        assert telegram.send_response_to_topic.await_count == 0   # NOT replayed
        assert telegram.send_to_topic.await_count == 1            # disclosure
        assert telegram.update_topic_state.await_count == 0
        assert telegram.close_topic.await_count == 1
        assert driver.cancel.await_count == 1
        assert bus.notify.await_count == 1

    async def test_a_wedged_disclosure_is_bounded(self, tmp_path, monkeypatch):
        """MUTATION: remove the disclosure's bound → this hangs. The wedged
        counterparty is an Event never set; the production constant is
        shortened. No asyncio.sleep is patched."""
        import asyncio as _aio
        import tools as tools_mod
        from tools import FinalizeResult, _finalize_engagement

        monkeypatch.setattr(tools_mod, "_TOPIC_OP_TIMEOUT_S", 0.05)
        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_raises=RuntimeError("telegram 500"))
        wedged = _aio.Event()
        attempts = []

        async def _hang(thread_id, text, **kw):
            attempts.append(text)
            await wedged.wait()

        telegram.send_to_topic = AsyncMock(side_effect=_hang)

        won = await _finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert len(attempts) == 1
        assert telegram.update_topic_state.await_count == 0
        assert telegram.close_topic.await_count == 1
        assert bus.notify.await_count == 1

    async def test_an_unconfirmed_cancel_also_stays_unmarked(self, tmp_path):
        """The rule is uniform across outcomes: a 🛑 over a topic that never
        heard why is the same lie as a ✅.

        MUTATION: scope the confirmation gate to ``outcome == "completed"``
        → this fails.
        """
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_raises=RuntimeError("telegram 500"))

        won = await _finalize_engagement(
            rec, outcome="cancelled", text="stopped", artifacts=[],
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert reg.get(rec.id).status == "cancelled"
        assert telegram.update_topic_state.await_count == 0
        assert telegram.close_topic.await_count == 1
        assert telegram.send_to_topic.await_count == 1

    async def test_a_wedged_close_cannot_strand_the_tail(
        self, tmp_path, monkeypatch,
    ):
        """R2 seam finding (Sol), H3. Making the close unconditional put a
        BARE, unbounded await on the untold path: this funnel's close has no
        caller-side bound, while the same module's abort path already wraps the
        identical call in ``asyncio.wait_for(..., _TOPIC_OP_TIMEOUT_S)``.

        A close that never returns would strand everything behind it — the
        driver teardown, the bus notification, the retains — over a record that
        is ALREADY durably terminal, and, because ``driver.cancel`` is what
        ends the response iterator, it would also strand the follow-up turn
        that is waiting to adjudicate its own cutoff. F1's fix disabled by
        F3's fix.

        MUTATION: remove the bound around the close → this test HANGS instead
        of passing. A "the close raises" pin does NOT kill that mutation and is
        not a substitute for this one.

        The wedged counterparty is an Event that is never set and the
        production constant is shortened; no ``asyncio.sleep`` is patched.
        """
        import asyncio as _aio
        import tools as tools_mod
        from tools import FinalizeResult, _finalize_engagement

        monkeypatch.setattr(tools_mod, "_TOPIC_OP_TIMEOUT_S", 0.05)
        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_raises=RuntimeError("telegram 500"))
        wedged = _aio.Event()
        closes = []

        async def _hang(*, thread_id):
            closes.append(thread_id)
            await wedged.wait()
        telegram.close_topic = AsyncMock(side_effect=_hang)

        won = await _finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert closes == [42]                    # attempted exactly once
        assert telegram.send_to_topic.await_count == 1   # the disclosure
        # Everything the wedge would otherwise have stranded.
        assert driver.cancel.await_count == 1
        assert bus.notify.await_count == 1


class TestTheFunnelRecordsWhetherItToldTheTopic:
    """R2 mutation pins — NOT part of the accepted red case. The funnel must
    record, for the follow-up adjudicator to read, whether ITS telling reached
    the topic.

    These assert the recorded fact directly, which means they name the
    mechanism's storage. That is deliberate HERE and would be wrong in the red
    case: Terra returned an earlier version of this cluster's red case for
    pinning this representation, and the behavioural version of the same
    property now lives end to end in
    ``tests/test_launch_death_reporter.py::TestTerminalStatusIsNotProofOfATelling``,
    which names nothing. What these add on top is a killing mutation for each
    ARM of the recording rule, which an end-to-end count cannot separate.

    Terminal STATUS is not that fact. A ticketed follow-up turn that calls
    ``emit_completion`` ends with no ``ResultMessage`` on the happy path AND on
    the failure path, and the delivery task can only tell those apart by
    knowing whether the terminal path actually said anything into the topic.
    The receipt is that knowledge, and it is written here.

    MUTATIONS: never write the receipt → the terminal-untold red case in
    ``tests/test_launch_death_reporter.py`` cannot distinguish the states.
    Write it as confirmed whatever happened → the untold parameter there
    fails. Write it AFTER the close → the wedged-close case below fails.
    """

    @staticmethod
    def _receipts(reg):
        """The recorded telling, read without asserting an implementation:
        ``{engagement_id: confirmed}``."""
        return dict(getattr(reg, "_terminal_telling", {}) or {})

    async def test_a_confirmed_post_records_the_telling_as_confirmed(
        self, tmp_path,
    ):
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_returns=4242)
        won = await _finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert self._receipts(reg) == {rec.id: True}

    async def test_a_failed_post_and_a_confirmed_disclosure_still_told(
        self, tmp_path,
    ):
        """The disclosure IS a telling. If it landed, the topic heard why, and
        the follow-up owner must stay quiet.

        MUTATION: record the receipt from the summary alone, ignoring the
        disclosure → this fails.
        """
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_raises=RuntimeError("telegram 500"))
        won = await _finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert telegram.send_to_topic.await_count == 1   # the disclosure
        assert self._receipts(reg) == {rec.id: True}

    async def test_nothing_confirmed_records_the_telling_as_UNTOLD(
        self, tmp_path,
    ):
        """The state the seam round found: the summary failed AND the
        disclosure failed, so the topic heard nothing at all."""
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_raises=RuntimeError("telegram 500"))

        async def _plain_fails(thread_id, text, **kw):
            events.append("plain")
            raise RuntimeError("telegram 500 again")
        telegram.send_to_topic = AsyncMock(side_effect=_plain_fails)

        won = await _finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert telegram.send_to_topic.await_count == 1
        assert self._receipts(reg) == {rec.id: False}
        assert telegram.update_topic_state.await_count == 0
        assert telegram.close_topic.await_count == 1

    async def test_a_disclosure_returning_none_is_not_a_telling(
        self, tmp_path,
    ):
        """MUTATION: treat the disclosure send RETURNING as confirmation → this
        fails. The same third possibility the summary path already pins: a
        sender that returns normally having sent nothing."""
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_raises=RuntimeError("telegram 500"))

        async def _plain_none(thread_id, text, **kw):
            events.append("plain")
            return None
        telegram.send_to_topic = AsyncMock(side_effect=_plain_none)

        won = await _finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert self._receipts(reg) == {rec.id: False}

class TestFinalizeConfirmationMutationPins:
    """#678 first-diff-round reviewer findings. These are NOT part of the
    accepted red case; they exist so specific mutations of the confirmation
    gate cannot pass. Each names the mutation it kills."""

    async def test_a_direct_sender_returning_none_is_not_confirmation(
        self, tmp_path,
    ):
        """MUTATION: accept a ``None`` return from the direct sender as
        confirmation (e.g. ``completion_post_confirmed = True`` after the
        await, or ``_mid is not None`` weakened to a bare truthiness test on a
        call that returned) → this fails.

        Both reviewers found the same gap: every direct-send case in the
        accepted red case either RAISES or returns ``4242``, so nothing pinned
        the third possibility — a sender that returns normally having sent
        nothing. That is not hypothetical on this path: it is exactly what the
        sequencer seam does, and a channel implementation is free to do it too.
        """
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_returns=None)

        won = await _finalize_engagement(
            rec, outcome="completed", text="done", artifacts=[],
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert telegram.send_response_to_topic.await_count == 1
        assert telegram.send_to_topic.await_count == 1          # disclosure
        assert telegram.update_topic_state.await_count == 0
        assert telegram.close_topic.await_count == 1
        assert driver.cancel.await_count == 1
        assert bus.notify.await_count == 1

    async def test_an_error_outcome_also_withholds_the_mark_and_the_close(
        self, tmp_path,
    ):
        """MUTATION: scope the confirmation gate to
        ``outcome in ("completed", "cancelled")`` → this fails.

        The accepted red case covers ``completed`` and ``cancelled``. The
        third terminal outcome takes the same branch and is asserted here
        separately, because a gate that silently excluded one outcome would
        leave that outcome painting a ⚠️ over a topic that never heard why.
        """
        from tools import FinalizeResult, _finalize_engagement

        reg, rec, telegram, bus, driver, events = await _build_finalize(
            tmp_path, rich_raises=RuntimeError("telegram 500"))

        won = await _finalize_engagement(
            rec, outcome="error", text="broke", artifacts=[],
            next_steps=[], driver=driver)

        assert won is FinalizeResult.FINALIZED
        assert reg.get(rec.id).status == "error"
        assert telegram.update_topic_state.await_count == 0
        assert telegram.close_topic.await_count == 1
        assert telegram.send_to_topic.await_count == 1
        assert bus.notify.await_count == 1
