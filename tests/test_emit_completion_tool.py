"""Tests for the emit_completion tool (agent-side, Plan 2)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class TestEmitCompletionHandler:
    async def test_returns_acknowledged_inside_engagement(self, tmp_path, monkeypatch):
        from engagement_registry import EngagementRegistry
        from tools import emit_completion, init_tools, engagement_var

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )
        tch = MagicMock(); tch.send_to_topic = AsyncMock(); tch.send_response_to_topic = AsyncMock(); tch.close_topic = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler({
                "text": "done", "artifacts": ["sha1"], "next_steps": [], "status": "ok",
            })
        finally:
            engagement_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "acknowledged"
        assert rec.status == "completed"

    async def test_returns_not_in_engagement_when_outside(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import emit_completion, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        init_tools(
            channel_manager=MagicMock(), bus=MagicMock(),
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        res = await emit_completion.handler({"text": "x"})
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "not_in_engagement"

    async def test_error_status_finalizes_with_error_outcome(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import emit_completion, init_tools, engagement_var

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )
        tch = MagicMock(); tch.send_to_topic = AsyncMock(); tch.send_response_to_topic = AsyncMock(); tch.close_topic = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        token = engagement_var.set(rec)
        try:
            await emit_completion.handler({"text": "boom", "status": "error"})
        finally:
            engagement_var.reset(token)
        assert rec.status == "error"


class TestEmitCompletionIdempotency:
    """Bug 9 (v0.14.6): emit_completion is a no-op once the engagement is
    in a terminal state. Pre-fix, a duplicate call (SDK retry / hook
    misfire) ran _finalize_engagement twice, double-NOTIFYing Ellen and
    double-writing the meta-scope summary into Honcho.
    """

    async def _make_finalised_engagement(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import emit_completion, init_tools, engagement_var

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )
        tch = MagicMock()
        tch.send_to_topic = AsyncMock()
        tch.send_response_to_topic = AsyncMock()
        tch.close_topic = AsyncMock()
        cm = MagicMock()
        cm.get.return_value = tch
        bus = MagicMock()
        bus.notify = AsyncMock()
        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        token = engagement_var.set(rec)
        return reg, rec, cm, tch, bus, token, emit_completion

    async def test_double_emit_does_not_re_finalize(self, tmp_path):
        from tools import engagement_var
        reg, rec, cm, tch, bus, token, emit_completion = (
            await self._make_finalised_engagement(tmp_path)
        )
        try:
            res1 = await emit_completion.handler({"text": "done", "status": "ok"})
            payload1 = json.loads(res1["content"][0]["text"])
            assert payload1["status"] == "acknowledged"
            assert rec.status == "completed"

            # Snapshot side-effect counts after the legitimate first call.
            close_calls_before = tch.close_topic.await_count
            notify_calls_before = bus.notify.await_count

            # Re-emit (the bug scenario).
            res2 = await emit_completion.handler({"text": "done again", "status": "ok"})
        finally:
            engagement_var.reset(token)

        payload2 = json.loads(res2["content"][0]["text"])
        # Tool acknowledges but tags it as a no-op.
        assert payload2["status"] == "acknowledged"
        assert payload2["kind"] == "already_terminal"

        # Critical assertion: side effects did NOT fire a second time.
        assert tch.close_topic.await_count == close_calls_before
        assert bus.notify.await_count == notify_calls_before

    async def test_re_emit_after_cancel_is_noop(self, tmp_path):
        from tools import engagement_var
        reg, rec, cm, tch, bus, token, emit_completion = (
            await self._make_finalised_engagement(tmp_path)
        )
        try:
            await reg.mark_cancelled(rec.id)
            assert rec.status == "cancelled"
            res = await emit_completion.handler({"text": "late", "status": "ok"})
        finally:
            engagement_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "already_terminal"
        # Topic close / bus.notify NEVER fired.
        assert tch.close_topic.await_count == 0
        assert bus.notify.await_count == 0


class TestConcurrentCancelVsEmit:
    """L75/L24: /cancel landing while emit_completion is suspended in a
    real await (the G-2 forced-reload window) must not double-finalize
    or let emit_completion clobber the winning 'cancelled' outcome."""

    async def test_cancel_during_g2_reload_window_finalizes_once(
        self, tmp_path, monkeypatch,
    ):
        """Pins INV-ENG-001. Red case demonstrated: reducing
        try_transition_terminal's already-terminal refusal to `rec is None`
        lets the loser finalize too and fails this test."""
        import tools
        from engagement_registry import EngagementRegistry
        from tools import emit_completion, engagement_var, init_tools, _finalize_engagement

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"}, topic_id=42,
        )
        tch = MagicMock(); tch.send_to_topic = AsyncMock(); tch.send_response_to_topic = AsyncMock(); tch.close_topic = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(channel_manager=cm, bus=bus, specialist_registry=MagicMock(),
                   mcp_registry=MagicMock(), trigger_registry=MagicMock(),
                   engagement_registry=reg)

        gate = asyncio.Event()

        async def fake_reload(args):
            await gate.wait()
            return {"content": [{"type": "text", "text": json.dumps({"status": "ok"})}]}
        monkeypatch.setattr(tools.casa_reload, "handler", fake_reload)
        tools._ENGAGEMENTS_PENDING_RELOAD.add(rec.id)

        token = engagement_var.set(rec)
        try:
            emit_task = asyncio.create_task(
                emit_completion.handler({"text": "done", "status": "ok"}))
            await asyncio.sleep(0)   # emit passes its terminal check, parks in fake_reload
            # user /cancel wins the race (same call casa_core._finalize_cancel makes)
            await _finalize_engagement(rec, outcome="cancelled",
                                       text="Cancelled by user.",
                                       artifacts=[], next_steps=[], driver=None)
            gate.set()
            await emit_task
        finally:
            engagement_var.reset(token)

        assert rec.status == "cancelled"          # emit must NOT overwrite the cancel
        assert bus.notify.await_count == 1        # exactly one DelegationComplete
        assert tch.close_topic.await_count == 1   # topic closed exactly once


def _wire_engagement(tmp_path):
    """Registry + mocks + init_tools; returns (rec, registry, telegram, bus)."""
    import asyncio as _a  # noqa: F401
    from engagement_registry import EngagementRegistry
    from tools import init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = _a.get_event_loop() if False else None  # placeholder never used
    tch = MagicMock(); tch.send_to_topic = AsyncMock(); tch.send_response_to_topic = AsyncMock(); tch.close_topic = AsyncMock()
    cm = MagicMock(); cm.get.return_value = tch
    bus = MagicMock(); bus.notify = AsyncMock()
    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )
    return reg, tch, bus


class TestEmitCompletionValidation:
    """B-3 (v0.69.3): a fully-successful configurator engagement finalized
    outcome=error kind=emit_completion_error (2026-07-12 00:14Z) — the tool
    mapped EVERY status other than exactly "ok" (including the doctrine's
    own "partial"/"cancelled", or a model writing "success") to a terminal
    error, and malformed arg shapes rode straight into finalize. Malformed
    calls must get a TOOL error back (agent retries); doctrine statuses map
    to their true outcomes."""

    async def _emit(self, reg, rec, args):
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler(args)
        finally:
            engagement_var.reset(token)
        return json.loads(res["content"][0]["text"])

    async def _rec(self, reg, tmp_path):
        return await reg.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )

    async def test_unknown_status_is_tool_error_not_engagement_failure(self, tmp_path):
        reg, tch, bus = _wire_engagement(tmp_path)
        rec = await self._rec(reg, tmp_path)
        payload = await self._emit(reg, rec, {"text": "all good", "status": "success"})
        assert payload["status"] == "error"
        assert payload["kind"] == "invalid_status"
        assert "ok" in payload["message"] and "partial" in payload["message"]
        assert rec.status == "active"          # engagement NOT finalized
        tch.close_topic.assert_not_awaited()   # agent gets to retry

    async def test_cancelled_status_finalizes_cancelled_not_error(self, tmp_path):
        reg, tch, bus = _wire_engagement(tmp_path)
        rec = await self._rec(reg, tmp_path)
        payload = await self._emit(reg, rec, {"text": "user aborted", "status": "cancelled"})
        assert payload["status"] == "acknowledged"
        assert rec.status == "cancelled"       # doctrine status, true outcome
        assert rec.origin.get("error_kind") is None

    async def test_partial_status_completes_with_partial_marker(self, tmp_path):
        reg, tch, bus = _wire_engagement(tmp_path)
        rec = await self._rec(reg, tmp_path)
        payload = await self._emit(reg, rec, {"text": "did 2 of 3", "status": "partial"})
        assert payload["status"] == "acknowledged"
        assert rec.status == "completed"
        sent = " ".join(str(c.args) for c in (list(tch.send_to_topic.await_args_list) + list(tch.send_response_to_topic.await_args_list)))
        assert "partial" in sent.lower()

    async def test_failed_status_finalizes_error(self, tmp_path):
        reg, tch, bus = _wire_engagement(tmp_path)
        rec = await self._rec(reg, tmp_path)
        payload = await self._emit(reg, rec, {"text": "could not", "status": "failed"})
        assert payload["status"] == "acknowledged"
        assert rec.status == "error"

    async def test_string_artifacts_wrapped_not_exploded(self, tmp_path):
        """list("sha123") == ['s','h','a','1','2','3'] — the old coercion."""
        reg, tch, bus = _wire_engagement(tmp_path)
        rec = await self._rec(reg, tmp_path)
        payload = await self._emit(
            reg, rec, {"text": "done", "status": "ok", "artifacts": "sha123"})
        assert payload["status"] == "acknowledged"
        complete = bus.notify.await_args_list[0].args[0].content
        assert complete.origin is not None  # sanity: DelegationComplete shape

    async def test_non_list_next_steps_is_tool_error(self, tmp_path):
        reg, tch, bus = _wire_engagement(tmp_path)
        rec = await self._rec(reg, tmp_path)
        payload = await self._emit(
            reg, rec, {"text": "done", "status": "ok", "next_steps": {"step": 1}})
        assert payload["kind"] == "invalid_args"
        assert rec.status == "active"

    async def test_non_string_text_is_tool_error(self, tmp_path):
        reg, tch, bus = _wire_engagement(tmp_path)
        rec = await self._rec(reg, tmp_path)
        payload = await self._emit(
            reg, rec, {"text": {"summary": "done"}, "status": "ok"})
        assert payload["kind"] == "invalid_args"
        assert rec.status == "active"

    async def test_oversized_text_truncated_not_fatal(self, tmp_path):
        reg, tch, bus = _wire_engagement(tmp_path)
        rec = await self._rec(reg, tmp_path)
        payload = await self._emit(
            reg, rec, {"text": "x" * 50000, "status": "ok"})
        assert payload["status"] == "acknowledged"
        assert rec.status == "completed"
        complete = bus.notify.await_args_list[0].args[0].content
        assert len(complete.text) <= 8100  # 8000 cap + truncation marker


class TestPluginDeveloperCompletionGuard:
    """A.2 (v0.74.0): the release-identity gate at the emit_completion
    boundary — rejection keeps the engagement live with NO finalize side
    effects."""

    async def _mk(self, tmp_path, *, role="plugin-developer"):
        from engagement_registry import EngagementRegistry
        from tools import init_tools
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"),
                                 bus=None)
        rec = await reg.create(
            kind="executor", role_or_type=role, driver="claude_code",
            task="build plugin",
            origin={"role": "assistant", "channel": "telegram"}, topic_id=42)
        tch = MagicMock()
        tch.send_to_topic = AsyncMock()
        tch.send_response_to_topic = AsyncMock()
        tch.close_topic = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(channel_manager=cm, bus=bus,
                   specialist_registry=MagicMock(), mcp_registry=MagicMock(),
                   trigger_registry=MagicMock(), engagement_registry=reg)
        return reg, rec, tch, bus

    async def test_bad_artifact_rejected_engagement_live_no_side_effects(
            self, tmp_path, monkeypatch):
        import plugin_completion_guard as guard_mod
        reg, rec, tch, bus = await self._mk(tmp_path)
        monkeypatch.setattr(
            guard_mod, "validate_completion_artifacts",
            lambda arts: [{"index": 0, "reason_code": "tag_not_annotated",
                           "message": "m"}])
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler(
                {"text": "done", "status": "ok",
                 "artifacts": [{"kind": "casa_plugin_repo"}]})
        finally:
            engagement_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "completion_rejected"
        assert payload["failures"][0]["reason_code"] == "tag_not_annotated"
        assert res.get("is_error") is True
        assert reg.get(rec.id).status == "active"       # engagement stays live
        tch.close_topic.assert_not_called()             # S2: no finalize
        tch.send_to_topic.assert_not_called()           #     side effects
        tch.send_response_to_topic.assert_not_called()
        bus.notify.assert_not_called()

    async def test_valid_artifact_finalizes(self, tmp_path, monkeypatch):
        import plugin_completion_guard as guard_mod
        reg, rec, tch, bus = await self._mk(tmp_path)
        monkeypatch.setattr(guard_mod, "validate_completion_artifacts",
                            lambda arts: [])
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler(
                {"text": "done", "status": "ok",
                 "artifacts": [{"kind": "casa_plugin_repo"}]})
        finally:
            engagement_var.reset(token)
        assert json.loads(res["content"][0]["text"])["status"] == "acknowledged"
        assert reg.get(rec.id).status == "completed"

    async def test_non_ok_status_skips_guard(self, tmp_path, monkeypatch):
        import plugin_completion_guard as guard_mod
        reg, rec, tch, bus = await self._mk(tmp_path)
        monkeypatch.setattr(
            guard_mod, "validate_completion_artifacts",
            lambda arts: (_ for _ in ()).throw(AssertionError("not called")))
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            await emit_completion.handler(
                {"text": "gave up", "status": "failed", "artifacts": []})
        finally:
            engagement_var.reset(token)
        assert reg.get(rec.id).status == "error"

    async def test_other_executor_types_skip_guard(self, tmp_path, monkeypatch):
        import plugin_completion_guard as guard_mod
        reg, rec, tch, bus = await self._mk(tmp_path, role="configurator")
        monkeypatch.setattr(
            guard_mod, "validate_completion_artifacts",
            lambda arts: (_ for _ in ()).throw(AssertionError("not called")))
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            await emit_completion.handler(
                {"text": "done", "status": "ok", "artifacts": []})
        finally:
            engagement_var.reset(token)
        assert reg.get(rec.id).status == "completed"

    async def test_guard_crash_fails_closed(self, tmp_path, monkeypatch):
        import plugin_completion_guard as guard_mod
        reg, rec, tch, bus = await self._mk(tmp_path)
        monkeypatch.setattr(
            guard_mod, "validate_completion_artifacts",
            lambda arts: (_ for _ in ()).throw(RuntimeError("boom")))
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler(
                {"text": "done", "status": "ok",
                 "artifacts": [{"kind": "casa_plugin_repo"}]})
        finally:
            engagement_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["kind"] == "completion_rejected"
        assert payload["failures"][0]["reason_code"] == "guard_error"
        assert reg.get(rec.id).status == "active"


class TestFinalizeResultMapping:
    """G4 D5 (v0.96.0): emit_completion must not acknowledge a completion
    that did not actually finalize (Sol g4-r1-6 — the persistence-rollback
    False was previously acked as success)."""

    async def _setup(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import init_tools
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"),
                                 bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )
        tch = MagicMock(); tch.send_to_topic = AsyncMock()
        tch.send_response_to_topic = AsyncMock()
        tch.close_topic = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        return reg, rec

    async def test_persist_failure_returns_retryable_error(
            self, tmp_path, monkeypatch):
        from tools import emit_completion, engagement_var
        reg, rec = await self._setup(tmp_path)

        async def boom(*a, **k):
            raise OSError("tombstone write failed")
        monkeypatch.setattr(reg, "try_transition_terminal", boom)

        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler({
                "text": "done", "artifacts": [], "next_steps": [],
                "status": "ok"})
        finally:
            engagement_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] != "acknowledged"
        assert payload["kind"] == "finalize_persist_failed"
        assert payload.get("retryable") is True
        assert rec.status not in ("completed", "error", "cancelled")


class _FakeInboundDriver:
    """Minimal claude_code-driver stand-in for the G4 completion gate.

    #591: `in_flight_*` mirrors the real driver's split — `_texts`/`_depth` are
    the QUEUED population (nothing has been handed to the CLI), `_in_flight` is
    the population already written into its stdin FIFO with no turn_start
    evidence back, and `_in_flight_blocking` is the sub-population still young
    enough to veto. They are separate fields for the same reason the accessors
    are separate methods: a test that cannot express "delivered but not taken
    up" cannot exercise the defect.
    """
    def __init__(self, depth=0, reservations=0, texts=(),
                 in_flight=(), in_flight_blocking=None,
                 command_reservations=0, reservation_texts=(),
                 real=None, real_eid=None):
        # C4 round 2: `real` delegates EVERY inbound accessor to a real
        # `ClaudeCodeDriver` holding real ledger and real spool state, so a
        # renderer case exercises the actual read-time exclusion instead of a
        # canned list. The terminal machinery around it stays cheap.
        self._real = real
        self._real_eid = real_eid
        self._depth = depth
        self._resv = reservations
        self._cmd_resv = command_reservations
        self._texts = list(texts)
        self._in_flight = list(in_flight)
        self._in_flight_blocking = (
            len(self._in_flight) if in_flight_blocking is None
            else in_flight_blocking)
        # C4/#691: OPT-IN and default-empty, so every fixture written for the
        # count-only ceiling stays genuinely count-only.
        self._reservation_texts = list(reservation_texts)
        self.refusals: list[str] = []
        self.cancelled_intents: list[tuple] = []
        self.forced_boundaries: list[str] = []

    def _r(self, name, fallback):
        if self._real is None:
            return fallback()
        return getattr(self._real, name)(self._real_eid)

    def inbound_unread_depth(self, eng_id):
        return self._r("inbound_unread_depth", lambda: self._depth)
    def inbound_reservations(self, eng_id):
        return self._r("inbound_reservations", lambda: self._resv)
    def inbound_message_reservations(self, eng_id):
        # #664: the disclosure projection — reservations minus the ones a
        # recognized command holds for itself (mirrors the real driver).
        return self._r("inbound_message_reservations",
                       lambda: max(0, self._resv - self._cmd_resv))
    def inbound_unread_texts(self, eng_id):
        return self._r("inbound_unread_texts", lambda: list(self._texts))
    def inbound_in_flight_texts(self, eng_id):
        return self._r("inbound_in_flight_texts", lambda: list(self._in_flight))
    def inbound_in_flight_blocking(self, eng_id):
        return self._r("inbound_in_flight_blocking",
                       lambda: self._in_flight_blocking)
    def inbound_reservation_texts(self, eng_id):
        return self._r("inbound_reservation_texts",
                       lambda: list(self._reservation_texts))

    async def force_completion_turn_boundary(self, engagement):
        self.forced_boundaries.append(engagement.id)
    def record_completion_refusal(self, eng_id):
        self.refusals.append(eng_id); return len(self.refusals)
    def cancel_send_intent(self, eng_id, request_id):
        self.cancelled_intents.append((eng_id, request_id))
    def register_completion_consumption(self, eng_id, args): pass
    async def drain_inbound_spool(self, engagement): pass


class TestCompletionInboundGate:
    """G4 D1/D2 (v0.96.0): emit_completion must not complete past unread
    operator input."""

    async def _setup(self, tmp_path, driver):
        import agent as agent_mod
        from engagement_registry import EngagementRegistry
        from tools import init_tools
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"),
                                 bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="probe-exec",
            driver="claude_code", task="t",
            origin={"role": "assistant", "channel": "telegram"}, topic_id=42,
        )
        tch = MagicMock(); tch.send_to_topic = AsyncMock()
        tch.send_response_to_topic = AsyncMock()
        tch.close_topic = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        agent_mod.active_claude_code_driver = driver
        return reg, rec, tch

    async def _emit(self, rec, status="ok"):
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler({
                "text": "done", "artifacts": [], "next_steps": [],
                "status": status})
        finally:
            engagement_var.reset(token)
        return json.loads(res["content"][0]["text"])

    async def test_unread_queued_message_refuses_completion(
            self, tmp_path, monkeypatch):
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=1, texts=["change the design"])
        reg, rec, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["kind"] == "unread_inbound"
        assert payload.get("retryable") is True
        assert rec.status not in ("completed", "error", "cancelled")
        assert drv.refusals  # counted for escalation

    async def test_pending_ingress_reservation_refuses_completion(
            self, tmp_path):
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=0, reservations=1)
        reg, rec, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["kind"] == "unread_inbound"
        assert rec.status not in ("completed", "error", "cancelled")

    async def test_clean_spool_completes(self, tmp_path):
        import agent as agent_mod
        drv = _FakeInboundDriver()
        reg, rec, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["status"] == "acknowledged"
        assert rec.status == "completed"

    async def test_inbound_racing_past_precheck_is_vetoed_by_terminal_hook(
            self, tmp_path):
        """Pins INV-ENG-003's second enforcement layer: the gate re-evaluates
        INSIDE the terminal transition, so input that arrives after
        emit_completion's pre-check but before the flip still vetoes.

        Red case demonstrated: neutering the terminal hook's veto
        (`if inbound_gate and (texts or resv):` → `if False:`) completes this
        engagement and fails the test. Breaking emit_completion's pre-check
        alone fails nothing — the hook backstops it and the refusal is
        recorded on the veto path too — so the hook is where the invariant is
        actually pinned; the pre-check is an optimisation that spares
        sequencer debt.
        """
        import agent as agent_mod
        drv = _FakeInboundDriver()
        reg, rec, _ = await self._setup(tmp_path, drv)

        async def _inbound_arrives_during_drain(engagement):
            drv._depth = 1
            drv._texts = ["arrived during completion"]

        drv.drain_inbound_spool = _inbound_arrives_during_drain
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["kind"] == "unread_inbound"
        assert rec.status not in ("completed", "error", "cancelled")

    async def test_error_status_not_gated_but_annotated(self, tmp_path):
        """A broken engagement must be able to die even with unread input;
        the topic post carries the never-read texts (D4)."""
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=1, texts=["msg-that-was-never-read"])
        reg, rec, tch = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec, status="error")
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["status"] == "acknowledged"
        assert rec.status == "error"
        posted = "".join(str(c.args) + str(c.kwargs)
                         for c in (list(tch.send_to_topic.call_args_list)
                                   + list(tch.send_response_to_topic.call_args_list)))
        # #591: the copy claims what Casa can evidence (no turn start recorded)
        # rather than asserting the message was never read — which is not
        # provable for an envelope already handed to the CLI.
        assert "no turn start recorded" in posted
        assert "msg-that-was-never-read" in posted

    async def test_error_terminal_discloses_a_pending_reservation_count(
            self, tmp_path):
        """RED CASE (#664, reviewer-specified): a message still inside its
        ingress-reservation window — accepted by the Telegram handler,
        reserved, not yet spooled, so no text exists anywhere — dies with an
        error terminal and today is disclosed to nobody: the rendering fires
        only on ``unread_snapshot`` (in-flight + queued texts) and the
        reservation count feeds only the completed-path gate predicate.

        Pins: a pending ingress reservation at a non-completed terminal is
        disclosed to the engagement topic as a count. Because the reservation
        is anonymous at this base, the disclosed total is an upper bound and
        the copy says so ("up to N"); with no text there is no excerpt bullet
        and the sentence ends with "." rather than ":".
        """
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=0, reservations=1)
        reg, rec, tch = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec, status="error")
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["status"] == "acknowledged"
        assert rec.status == "error"
        assert tch.send_response_to_topic.call_count == 1
        posted = "".join(str(c.args) + str(c.kwargs)
                         for c in (list(tch.send_to_topic.call_args_list)
                                   + list(tch.send_response_to_topic.call_args_list)))
        assert "up to 1 inbound message(s)" in posted
        assert "no turn start recorded" in posted
        assert "•" not in posted  # no excerpt bullet — no text exists

    async def test_race_message_lands_between_gate_and_flip(
            self, tmp_path, monkeypatch):
        """G4 D2: depth flips to >0 after the handler gate but before the
        terminal mutation — the registry-internal hook must refuse."""
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=0)
        reg, rec, _ = await self._setup(tmp_path, drv)

        async def racing_drain(engagement):
            drv._depth = 1
            drv._texts = ["late message"]
        drv.drain_inbound_spool = racing_drain

        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["kind"] == "unread_inbound"
        assert rec.status not in ("completed", "error", "cancelled")
        # the pre-registered consumption debt was rolled back
        assert any(r[1].startswith("emit_completion:")
                   for r in drv.cancelled_intents) or drv.cancelled_intents == []


class TestReservationDisclosure:
    """#664 — a message still inside its ingress-reservation window dies
    TOLD, on every non-completed terminal. The reviewer-accepted red case
    lives in TestCompletionInboundGate; these pin the arithmetic's edges:
    the upper-bound copy, the command exclusion, strict typing, and the
    accessor-failure containment.
    """

    _emit = TestCompletionInboundGate._emit

    async def _setup(self, tmp_path, driver):
        import agent as agent_mod
        from engagement_registry import EngagementRegistry
        from tools import init_tools
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"),
                                 bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="probe-exec",
            driver="claude_code", task="t",
            origin={"role": "assistant", "channel": "telegram"}, topic_id=42,
        )
        tch = MagicMock(); tch.send_to_topic = AsyncMock()
        tch.send_response_to_topic = AsyncMock()
        tch.close_topic = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        agent_mod.active_claude_code_driver = driver
        return reg, rec, tch, bus

    @staticmethod
    def _posted(tch):
        return "".join(
            str(c.args) + str(c.kwargs)
            for c in (list(tch.send_to_topic.call_args_list)
                      + list(tch.send_response_to_topic.call_args_list)))

    async def test_mixed_populations_fold_reservations_into_an_upper_bound(
            self, tmp_path):
        """Texts keep their excerpts (each exactly once); reservations add
        a count; a total that includes anonymous reservations is an upper
        bound and the copy says so."""
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=1, texts=["q-text-queued"],
                                 in_flight=["f-text-in-flight"],
                                 reservations=2)
        reg, rec, tch, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec, status="error")
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["status"] == "acknowledged"
        posted = self._posted(tch)
        assert "up to 4 inbound message(s)" in posted
        assert posted.count("q-text-queued") == 1
        assert posted.count("f-text-in-flight") == 1

    async def test_text_only_total_keeps_the_exact_copy(self, tmp_path):
        """The qualifier appears ONLY when reservations contribute — a
        text-only disclosure keeps today's exact claim byte-for-byte."""
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=2, texts=["a-text", "b-text"])
        reg, rec, tch, _ = await self._setup(tmp_path, drv)
        try:
            await self._emit(rec, status="error")
        finally:
            agent_mod.active_claude_code_driver = None
        posted = self._posted(tch)
        assert "2 inbound message(s)" in posted
        assert "up to" not in posted

    async def test_a_command_reservation_is_not_disclosed_by_a_racing_terminal(
            self, tmp_path):
        """seam pin (#664): /cancel reserves, then awaits; an agent error
        terminal wins. The winner must not disclose the command being
        processed as a lost message — the exclusion is classified at the
        reservation's BIRTH, so it holds for every winner, not only the
        command's own finalize."""
        import agent as agent_mod
        drv = _FakeInboundDriver(reservations=1, command_reservations=1)
        reg, rec, tch, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec, status="error")
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["status"] == "acknowledged"
        assert rec.status == "error"
        assert "inbound message(s)" not in self._posted(tch)

    async def test_a_foreign_reservation_is_disclosed_past_a_command(
            self, tmp_path):
        import agent as agent_mod
        drv = _FakeInboundDriver(reservations=2, command_reservations=1)
        reg, rec, tch, _ = await self._setup(tmp_path, drv)
        try:
            await self._emit(rec, status="error")
        finally:
            agent_mod.active_claude_code_driver = None
        assert "up to 1 inbound message(s)" in self._posted(tch)

    async def test_command_reservations_still_veto_completion(self, tmp_path):
        """The exclusion is DISCLOSURE-only: the gate keeps the raw total,
        so a command reservation still vetoes a racing successful
        completion (the property that keeps /cancel-vs-completed races
        deciding as before)."""
        import agent as agent_mod
        drv = _FakeInboundDriver(reservations=1, command_reservations=1)
        reg, rec, tch, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["kind"] == "unread_inbound"
        assert rec.status not in ("completed", "error", "cancelled")

    async def test_non_int_message_reservations_reads_as_zero(self, tmp_path):
        """Strict typing: a duck/mock accessor must read as no lost
        reservations, never fabricate a count."""
        import agent as agent_mod
        drv = _FakeInboundDriver()
        drv.inbound_message_reservations = lambda eng_id: "1"
        reg, rec, tch, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec, status="error")
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["status"] == "acknowledged"
        assert "inbound message(s)" not in self._posted(tch)

    async def test_a_raising_accessor_costs_only_the_new_coverage(
            self, tmp_path):
        """seam pin: the NEW accessor lives in its own try — its failure
        must not erase the text disclosure that already worked."""
        import agent as agent_mod
        def _boom(eng_id):
            raise RuntimeError("accessor down")
        drv = _FakeInboundDriver(depth=1, texts=["kept-text"])
        drv.inbound_message_reservations = _boom
        reg, rec, tch, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec, status="error")
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["status"] == "acknowledged"
        posted = self._posted(tch)
        assert "1 inbound message(s)" in posted
        assert "kept-text" in posted
        assert "up to" not in posted

    async def test_a_raising_accessor_cannot_disable_the_gate(self, tmp_path):
        """seam pin: nor may the new accessor's failure switch off the veto
        that already existed — the gate reads the raw total from its own
        try."""
        import agent as agent_mod
        def _boom(eng_id):
            raise RuntimeError("accessor down")
        drv = _FakeInboundDriver(reservations=1)
        drv.inbound_message_reservations = _boom
        reg, rec, tch, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["kind"] == "unread_inbound"
        assert rec.status not in ("completed", "error", "cancelled")

    async def test_in_flight_failure_keeps_the_reservation_disclosure(
            self, tmp_path):
        """The in-flight try failing costs only the in-flight population;
        queued texts AND the reservation count still reach the topic."""
        import agent as agent_mod
        def _boom(eng_id):
            raise RuntimeError("in-flight accessor down")
        drv = _FakeInboundDriver(depth=1, texts=["q-survives"],
                                 reservations=1)
        drv.inbound_in_flight_texts = _boom
        reg, rec, tch, _ = await self._setup(tmp_path, drv)
        try:
            await self._emit(rec, status="error")
        finally:
            agent_mod.active_claude_code_driver = None
        posted = self._posted(tch)
        assert "up to 2 inbound message(s)" in posted
        assert "q-survives" in posted

    async def test_disclosure_stays_out_of_the_bus_payload(self, tmp_path):
        """Topic-only: the count rides summary_text; the bus notification
        (which flows to semantic memory) must never carry it."""
        import agent as agent_mod
        drv = _FakeInboundDriver(reservations=1)
        reg, rec, tch, bus = await self._setup(tmp_path, drv)
        try:
            await self._emit(rec, status="error")
        finally:
            agent_mod.active_claude_code_driver = None
        assert "up to 1 inbound message(s)" in self._posted(tch)
        assert "inbound message(s)" not in str(bus.notify.call_args_list)


class TestCompletionGateSeesTurnsInThePipe:
    """#591 — INV-ENG-003 extended to the population the gate could not see.

    An envelope written into the engagement's stdin FIFO whose turn has not
    started yet counted as nothing: `unread_*` excludes `delivered` (right for
    the ask gate — it may already have been read), so a completion could pass
    the gate, commit terminal, and only then have the CLI begin the turn that
    was already in the pipe. The record was `completed`, so its casa tools were
    refused — but nothing stopped its ordinary Bash/Write/Edit.
    """

    _setup = TestCompletionInboundGate._setup
    _emit = TestCompletionInboundGate._emit

    async def test_in_flight_message_refuses_completion(self, tmp_path):
        """Red case: revert the pre-check to `_depth > 0 or _resv > 0` and the
        refusal still comes — from the terminal hook — so ALSO neuter the
        hook's `blocking` term and this completes, which is the bug."""
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=0, in_flight=["already in the pipe"])
        reg, rec, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None

        assert payload["kind"] == "unread_inbound"
        assert rec.status not in ("completed", "error", "cancelled")

    async def test_in_flight_arriving_after_the_precheck_is_still_vetoed(
            self, tmp_path):
        """The hook is where the invariant actually lives: delivery can happen
        during the finalize funnel's own awaits (the spool drain, a forced
        reload), long after the pre-check said the spool was clean."""
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=0)
        reg, rec, _ = await self._setup(tmp_path, drv)

        async def _delivered_during_drain(engagement):
            drv._in_flight = ["pumped while finalize ran"]
            drv._in_flight_blocking = 1

        drv.drain_inbound_spool = _delivered_during_drain
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None

        assert payload["kind"] == "unread_inbound"
        assert rec.status not in ("completed", "error", "cancelled")

    async def test_in_flight_alone_never_forces_a_turn_boundary(self, tmp_path):
        """The D3 escalation kills the CLI so a QUEUED envelope pumps at the
        respawn. An in-flight envelope is already past that boundary, and the
        kill is guarded to the very epoch holding it — so a turn that consumed
        it a moment earlier would die mid-work and the message would never be
        redelivered (`consumed` ⇒ no redelivery, the #341 hazard).

        Red case: drop the `_depth > 0 or _resv > 0` condition from the
        escalation and this fails on the second refusal.
        """
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=0, in_flight=["in the pipe"])
        reg, rec, _ = await self._setup(tmp_path, drv)
        try:
            first = await self._emit(rec)
            second = await self._emit(rec)      # 2nd consecutive → escalates
        finally:
            agent_mod.active_claude_code_driver = None

        assert first["kind"] == second["kind"] == "unread_inbound"
        assert drv.forced_boundaries == [], (
            "an in-flight envelope must never trigger the epoch kill")

    async def test_queued_input_still_escalates(self, tmp_path):
        """Mutation guard for the test above: the escalation must still fire
        for the population it was built for, or the scoping change would have
        silently disabled D3 altogether."""
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=1, texts=["queued, never delivered"])
        reg, rec, _ = await self._setup(tmp_path, drv)
        try:
            await self._emit(rec)
            await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None

        assert drv.forced_boundaries == [rec.id]

    async def test_expired_in_flight_completes_but_is_disclosed(self, tmp_path):
        """The fail-open bound, at the gate. A delivery whose turn never starts
        stops vetoing (`blocking` drops to 0) but stays in the annotation — the
        engagement completes AND the operator is told, rather than the
        engagement becoming impossible to complete."""
        import agent as agent_mod
        drv = _FakeInboundDriver(
            depth=0, in_flight=["delivered an hour ago, no turn"],
            in_flight_blocking=0)
        reg, rec, tch = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None

        assert payload["status"] == "acknowledged"
        assert rec.status == "completed"
        posted = "".join(str(c.args) + str(c.kwargs)
                         for c in (list(tch.send_to_topic.call_args_list)
                                   + list(tch.send_response_to_topic.call_args_list)))
        assert "delivered an hour ago, no turn" in posted, (
            "an expired veto must not become a silent loss")

    async def test_cancel_discloses_an_in_flight_message(self, tmp_path):
        """The half of #591 that needs no concurrency at all: /cancel is
        ungated by design, so the message dies with the engagement — and until
        now the operator was never told, because the snapshot only ever looked
        at the queued population.

        Red case: drop `in_flight` from `unread_snapshot` and the text vanishes
        from the topic post, restoring the silence.
        """
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=0, in_flight=["msg-in-the-pipe"])
        reg, rec, tch = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec, status="error")   # ungated outcome
        finally:
            agent_mod.active_claude_code_driver = None

        assert payload["status"] == "acknowledged"
        assert rec.status == "error"
        posted = "".join(str(c.args) + str(c.kwargs)
                         for c in (list(tch.send_to_topic.call_args_list)
                                   + list(tch.send_response_to_topic.call_args_list)))
        assert "msg-in-the-pipe" in posted
        assert "no turn start recorded" in posted

    async def test_both_populations_are_counted_once_each(self, tmp_path):
        """A queued message and an in-flight one are two messages, not one and
        not three — the count in the annotation is what the operator reads."""
        import agent as agent_mod
        drv = _FakeInboundDriver(
            depth=1, texts=["still queued"], in_flight=["in the pipe"])
        reg, rec, tch = await self._setup(tmp_path, drv)
        try:
            await self._emit(rec, status="error")
        finally:
            agent_mod.active_claude_code_driver = None

        posted = "".join(str(c.args) + str(c.kwargs)
                         for c in (list(tch.send_to_topic.call_args_list)
                                   + list(tch.send_response_to_topic.call_args_list)))
        # #649 (deliberate premise rewrite): the disclosure noun became
        # provenance-neutral "inbound" — deliver_system_turn continuations
        # are Casa-authored, not operator messages.
        assert "2 inbound message(s)" in posted
        assert "still queued" in posted and "in the pipe" in posted

    async def test_a_driver_without_the_new_accessors_still_completes(
            self, tmp_path):
        """Fail-open, unchanged: the in-casa driver and every older duck driver
        expose no in-flight accessors, and must not be gated by their absence
        or by a MagicMock's truthiness."""
        import agent as agent_mod

        class _LegacyDriver:
            def inbound_unread_depth(self, eng_id): return 0
            def inbound_reservations(self, eng_id): return 0
            def inbound_unread_texts(self, eng_id): return []
            def register_completion_consumption(self, eng_id, args): pass
            async def drain_inbound_spool(self, engagement): pass

        drv = _LegacyDriver()
        reg, rec, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None

        assert payload["status"] == "acknowledged"
        assert rec.status == "completed"

    async def test_a_mock_driver_cannot_fabricate_an_in_flight_depth(
            self, tmp_path):
        """`int(MagicMock()) == 1` bit this gate once already. A non-int/list
        return reads as empty, never as a fabricated depth."""
        import agent as agent_mod
        drv = _FakeInboundDriver()
        drv.inbound_in_flight_blocking = lambda eng_id: MagicMock()
        drv.inbound_in_flight_texts = lambda eng_id: MagicMock()
        reg, rec, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None

        assert payload["status"] == "acknowledged"
        assert rec.status == "completed"

    async def test_a_failing_in_flight_accessor_keeps_the_old_gate(
            self, tmp_path):
        """Found in diff review (Terra, S1). The new in-flight reads first
        shared a try/except with the queued and reservation reads, so a raise
        from EITHER new accessor reset all of them to zero — adding a gate
        could switch off the gate that was already there, and an accepted
        operator message could be committed past with no veto at all.

        What this pins, precisely: the refusal survives, through whichever
        layer gets there first. With queued depth positive at entry that is the
        pre-check, so merging the guards in the pre-check ALONE still fails
        this — but merging them in the terminal hook alone does not, because
        the hook is never reached. The hook's own guard is pinned by
        `test_a_failing_in_flight_accessor_keeps_the_TERMINAL_HOOK_gate` below,
        and the hook is where the invariant actually lives (same division of
        labour the original gate's tests already record).
        """
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=1, texts=["queued and still unread"])

        def _raises(eng_id):
            raise RuntimeError("driver bug in the NEW accessor")

        drv.inbound_in_flight_blocking = _raises
        drv.inbound_in_flight_texts = _raises
        reg, rec, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None

        assert payload["kind"] == "unread_inbound"
        assert rec.status not in ("completed", "error", "cancelled")

    async def test_a_failing_old_accessor_still_fails_open(self, tmp_path):
        """The other side of the split guard, unchanged from before: a broken
        driver must not wedge termination forever."""
        import agent as agent_mod
        drv = _FakeInboundDriver()

        def _raises(eng_id):
            raise RuntimeError("driver bug in the OLD accessor")

        drv.inbound_unread_texts = _raises
        reg, rec, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None

        assert payload["status"] == "acknowledged"
        assert rec.status == "completed"

    async def test_a_failing_in_flight_accessor_keeps_the_TERMINAL_HOOK_gate(
            self, tmp_path):
        """The same S1, at the layer that actually enforces it.

        The sibling test above has positive queued depth at entry, so the
        pre-check refuses and the terminal hook is never reached — mutating the
        hook's guard alone left every test green, which is precisely the
        "mutation that fails to mutate" trap. Here the queued message arrives
        DURING the finalize funnel (as a real delivery does), so the pre-check
        passes and the hook is the only thing standing between an accepted
        operator message and a silent terminal commit.

        Red case: merge the hook's two accessor guards back into one and this
        completes the engagement with an empty annotation.
        """
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=0)

        def _raises(eng_id):
            raise RuntimeError("driver bug in the NEW accessor")

        drv.inbound_in_flight_blocking = _raises
        drv.inbound_in_flight_texts = _raises

        async def _queued_arrives_during_drain(engagement):
            drv._depth = 1
            drv._texts = ["arrived while finalize ran"]

        drv.drain_inbound_spool = _queued_arrives_during_drain
        reg, rec, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_claude_code_driver = None

        assert payload["kind"] == "unread_inbound"
        assert rec.status not in ("completed", "error", "cancelled")

    async def test_a_failing_in_flight_accessor_still_discloses_the_queued_text(
            self, tmp_path):
        """The disclosure half of the same layer: an ungated outcome must still
        list the queued messages when only the new accessors are broken."""
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=1, texts=["queued, and still owed"])

        def _raises(eng_id):
            raise RuntimeError("driver bug in the NEW accessor")

        drv.inbound_in_flight_blocking = _raises
        drv.inbound_in_flight_texts = _raises
        reg, rec, tch = await self._setup(tmp_path, drv)
        try:
            await self._emit(rec, status="error")     # ungated outcome
        finally:
            agent_mod.active_claude_code_driver = None

        posted = "".join(str(c.args) + str(c.kwargs)
                         for c in (list(tch.send_to_topic.call_args_list)
                                   + list(tch.send_response_to_topic.call_args_list)))
        assert "queued, and still owed" in posted


class TestEmitCompletionCancellation:
    """#632 red case: the finalize tail must survive its own driver teardown.

    An in_casa emit_completion runs inside an SDK control-request handler task
    that ``Query._close_impl`` cancels when the funnel's own driver teardown
    closes the client (query.py:998-999 on the pinned SDK). The cancellation
    is simulated DIRECTLY — the fake driver's ``cancel()`` cancels the
    captured host task and then suspends once, exactly as the funnel is
    suspended awaiting teardown in production — so the case fails identically
    on CPython 3.11 and 3.12 (``asyncio.wait_for`` topology is not involved).

    Invariant (INV-ENG-010, docs/architecture/engagement-finalization.md; H-1
    contract at the deferred-restart drain; issue #632): once the terminal
    transition is won, the DelegationComplete bus notification, both retains,
    the deferred Supervisor restart POST (strictly after retains are
    gathered), the finalized log and the deferred-marker drain must all still
    happen even though the host task ends cancelled. Specified externally
    (redcase-specify r2) and accepted externally; do not modify.
    """

    async def test_in_casa_teardown_cannot_cancel_finalize_tail(
            self, tmp_path, monkeypatch):
        import agent as agent_mod
        import tools as tools_mod
        from engagement_registry import EngagementRegistry
        from tools import emit_completion, init_tools, engagement_var

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"),
                                 bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=None,
        )
        events: list[str] = []

        async def _notify(msg):
            events.append("notify")
        bus = MagicMock()
        bus.notify = AsyncMock(side_effect=_notify)
        init_tools(
            channel_manager=MagicMock(), bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )

        host_box: dict = {}

        class _Driver:
            cancel_calls = 0

            async def cancel(self, engagement):
                type(self).cancel_calls += 1
                events.append("cancel")
                host_box["task"].cancel()
                # Production delivers the CancelledError while the funnel is
                # suspended awaiting the teardown; suspend so it can land.
                await asyncio.sleep(0)

        driver = _Driver()
        monkeypatch.setattr(agent_mod, "active_engagement_driver", driver,
                            raising=False)
        monkeypatch.setattr(agent_mod, "active_semantic_memory", object(),
                            raising=False)

        async def _retain(sem, **kwargs):
            events.append("retain")
        monkeypatch.setattr(tools_mod, "retain_delegated", _retain)

        tools_mod._ENGAGEMENTS_DEFERRED_HARD_RELOAD.add(rec.id)

        async def _post():
            events.append("post")
            return {"status": "ok"}
        monkeypatch.setattr(tools_mod, "_post_supervisor_restart", _post)

        tail_complete = asyncio.Event()
        real_info = tools_mod.logger.info

        def _info(msg, *args, **kwargs):
            if msg == "Engagement %s finalized outcome=%s":
                events.append("final-log")
                tail_complete.set()
            return real_info(msg, *args, **kwargs)
        monkeypatch.setattr(tools_mod.logger, "info", _info)

        token = engagement_var.set(rec)
        try:
            host_task = asyncio.get_running_loop().create_task(
                emit_completion.handler({
                    "text": "done", "artifacts": [], "next_steps": [],
                    "status": "ok",
                }))
        finally:
            engagement_var.reset(token)
        host_box["task"] = host_task

        # The host faithfully ends cancelled — handler survival is NOT part
        # of the invariant (the tool_result is documented-lost on this path).
        with pytest.raises(asyncio.CancelledError):
            await host_task
        assert host_task.cancelled() is True

        # The side effects are observed through an independent completion
        # signal, never through the cancelled host task.
        await asyncio.wait_for(tail_complete.wait(), timeout=1)

        assert _Driver.cancel_calls == 1
        assert bus.notify.await_count == 1
        assert events.count("notify") == 1
        assert events.count("retain") == 2
        assert events.count("post") == 1
        assert events.count("final-log") == 1
        assert rec.id not in tools_mod._ENGAGEMENTS_DEFERRED_HARD_RELOAD
        assert events.index("cancel") < events.index("notify")
        assert events.index("notify") < min(
            i for i, e in enumerate(events) if e == "retain")
        assert max(
            i for i, e in enumerate(events) if e == "retain"
        ) < events.index("post")
        assert events.index("post") < events.index("final-log")


class TestFinalizeTailObservability:
    """#632 seam findings (Sol S1, Terra F3): the detached tail must be
    visibly anchored while it runs, its failures must be observed, and its
    payloads must carry the values frozen at the terminal flip."""

    async def _launch(self, tmp_path, monkeypatch, *, driver, notify=None,
                      quiesce=None):
        import agent as agent_mod
        import tools as tools_mod
        from engagement_registry import EngagementRegistry
        from tools import emit_completion, init_tools, engagement_var

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"),
                                 bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="pay rent", origin={
                "role": "assistant", "channel": "telegram",
                "_origin_clearance": "private",
            },
            topic_id=None,
        )
        bus = MagicMock()
        bus.notify = AsyncMock(side_effect=notify)
        init_tools(
            channel_manager=MagicMock(), bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        if quiesce is not None:
            monkeypatch.setattr(reg, "await_quiesce", quiesce)
        monkeypatch.setattr(agent_mod, "active_engagement_driver", driver,
                            raising=False)
        monkeypatch.setattr(agent_mod, "active_semantic_memory", None,
                            raising=False)
        token = engagement_var.set(rec)
        try:
            host = asyncio.get_running_loop().create_task(
                emit_completion.handler({"text": "done", "status": "ok"}))
        finally:
            engagement_var.reset(token)
        return tools_mod, reg, rec, bus, host

    async def test_tail_is_anchored_while_suspended_then_drains(
            self, tmp_path, monkeypatch):
        import tools as tools_mod
        gate = asyncio.Event()
        released = asyncio.Event()

        class _Driver:
            async def cancel(self, engagement):
                gate.set()
                await released.wait()

        tools_mod_, reg, rec, bus, host = await self._launch(
            tmp_path, monkeypatch, driver=_Driver())
        await gate.wait()
        assert len(tools_mod_._finalize_tail_tasks) == 1
        released.set()
        payload = json.loads((await host)["content"][0]["text"])
        assert payload["status"] == "acknowledged"
        await asyncio.sleep(0)
        assert len(tools_mod_._finalize_tail_tasks) == 0
        assert bus.notify.await_count == 1

    async def test_orphaned_tail_failure_logs_exactly_one_error(
            self, tmp_path, monkeypatch, caplog):
        import logging as _logging
        import tools as tools_mod
        host_box: dict = {}

        class _Driver:
            async def cancel(self, engagement):
                host_box["task"].cancel()
                await asyncio.sleep(0)

        # An unexpected tail exception AFTER the host is gone: force the
        # DelegationComplete constructor (outside the per-step try/except)
        # to blow up, so only the done callback can observe the failure.
        monkeypatch.setattr(
            tools_mod, "DelegationComplete",
            MagicMock(side_effect=RuntimeError("boom")))
        tools_mod_, reg, rec, bus, host = await self._launch(
            tmp_path, monkeypatch, driver=_Driver())
        host_box["task"] = host
        with caplog.at_level(_logging.ERROR, logger="tools"):
            with pytest.raises(asyncio.CancelledError):
                await host
            for _ in range(10):
                if tools_mod_._finalize_tail_tasks:
                    await asyncio.sleep(0)
        errors = [r for r in caplog.records
                  if r.levelno == _logging.ERROR
                  and "finalize tail raised" in r.getMessage()
                  and rec.id[:8] in r.getMessage()]
        assert len(errors) == 1
        assert len(tools_mod_._finalize_tail_tasks) == 0

    async def test_payloads_carry_the_values_frozen_at_the_flip(
            self, tmp_path, monkeypatch):
        """Terra seam F3: a post-terminal ``lower_origin_clearance`` (real
        mutator — no terminal refusal) lands during the funnel's quiesce
        await; the DelegationComplete must still carry the flip-time origin
        and the retained summary the flip-time task."""
        import tools as tools_mod
        seen: dict = {}

        async def _notify(msg):
            seen["origin"] = dict(msg.content.origin)

        reg_box: dict = {}
        rec_box: dict = {}

        async def _quiesce(engagement_id, timeout):
            # The clamp arrives while the funnel is suspended post-flip.
            await reg_box["reg"].lower_origin_clearance(
                rec_box["rec"].id, "public")
            return True

        class _Driver:
            async def cancel(self, engagement):
                return None

        retained: list = []

        async def _retain(sem, **kwargs):
            retained.append(kwargs)
        monkeypatch.setattr(tools_mod, "retain_delegated", _retain)

        import agent as agent_mod
        tools_mod_, reg, rec, bus, host = await self._launch(
            tmp_path, monkeypatch, driver=_Driver(), notify=_notify,
            quiesce=_quiesce)
        # _launch built reg/rec before quiesce could reference them:
        reg_box["reg"] = reg
        rec_box["rec"] = rec
        monkeypatch.setattr(agent_mod, "active_semantic_memory", object(),
                            raising=False)
        payload = json.loads((await host)["content"][0]["text"])
        assert payload["status"] == "acknowledged"
        # The real mutator DID fire post-flip:
        assert rec.origin["_origin_clearance"] == "public"
        assert rec.task != "pay rent"
        # ...and the completion payloads carry the flip-time values:
        assert seen["origin"]["_origin_clearance"] == "private"
        for kwargs in retained:
            for turn in kwargs.get("turns", []):
                if "engagement_summary" in turn.text:
                    assert json.loads(turn.text)["task"] == "pay rent"

    async def test_claude_code_emit_stays_synchronous(
            self, tmp_path, monkeypatch):
        """Selector control: a non-in_casa completion runs the tail in the
        caller's own task — no detached tail is created."""
        import agent as agent_mod
        import tools as tools_mod
        from engagement_registry import EngagementRegistry
        from tools import emit_completion, init_tools, engagement_var

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"),
                                 bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="custom-exec",
            driver="claude_code", task="t",
            origin={"role": "assistant", "channel": "telegram"},
            topic_id=None,
        )
        seen: dict = {}

        async def _notify(msg):
            seen["task"] = asyncio.current_task()
            seen["anchored"] = len(tools_mod._finalize_tail_tasks)
        bus = MagicMock()
        bus.notify = AsyncMock(side_effect=_notify)
        init_tools(
            channel_manager=MagicMock(), bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        monkeypatch.setattr(agent_mod, "active_claude_code_driver",
                            MagicMock(spec=[]), raising=False)
        monkeypatch.setattr(agent_mod, "active_semantic_memory", None,
                            raising=False)
        token = engagement_var.set(rec)
        try:
            host = asyncio.get_running_loop().create_task(
                emit_completion.handler({"text": "done", "status": "ok"}))
        finally:
            engagement_var.reset(token)
        payload = json.loads((await host)["content"][0]["text"])
        assert payload["status"] == "acknowledged"
        assert seen["task"] is host
        assert seen["anchored"] == 0


class TestC4DisclosureWithoutVeto:
    """C4 RED CASES at the terminal renderer — #740 / #691.

    Specified by sol (round `redcase-specify`), accepted by terra.

    INV-ENG-016's second clause: a population sourced from the durable spool
    FILE — texts with no attached incarnation, so the veto accessors answer 0 —
    is DISCLOSED at the terminal and never refuses the completion, because
    nothing the gate can force could ever clear it.

    INV-ENG-017's clause: a reservation's text is QUOTED, while the count and
    the "up to" hedge stay exactly as a text-less reservation would have made
    them.
    """

    _emit = TestCompletionInboundGate._emit
    _setup = TestReservationDisclosure._setup

    async def test_file_only_population_is_disclosed_without_refusing(
            self, tmp_path):
        import agent as agent_mod
        # depth/blocking 0 is what the file tier answers; the texts are what it
        # reads through. Today the terminal hook vetoes off `texts`, so this
        # completion is REFUSED and the operator is told nothing.
        drv = _FakeInboundDriver(
            depth=0, reservations=0,
            texts=["queued file text"],
            in_flight=["in-flight file text"], in_flight_blocking=0,
        )
        reg, rec, tch, _bus = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec, status="ok")
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["status"] == "acknowledged"
        assert len(drv.refusals) == 0
        assert rec.status == "completed"
        posted = "".join(str(c.args) + str(c.kwargs)
                         for c in (list(tch.send_to_topic.call_args_list)
                                   + list(tch.send_response_to_topic.call_args_list)))
        assert posted.count("• queued file text") == 1
        assert posted.count("• in-flight file text") == 1
        assert posted.count("2 inbound message(s)") == 1
        assert posted.count("up to") == 0

    async def test_reservation_texts_are_quoted_without_moving_the_count(
            self, tmp_path):
        import agent as agent_mod
        # One spooled message plus two held reservations, one of which Casa
        # knows the text of. The count arithmetic is exactly what it is today
        # (1 + 2 = 3, hedged because reservations are counted); what changes is
        # that the known text is QUOTED instead of merely counted.
        drv = _FakeInboundDriver(
            depth=0, reservations=2,
            texts=["spooled text"],
            reservation_texts=["reserved but never spooled"],
        )
        reg, rec, tch, _bus = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec, status="error")
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["status"] == "acknowledged"
        assert rec.status == "error"
        posted = "".join(str(c.args) + str(c.kwargs)
                         for c in (list(tch.send_to_topic.call_args_list)
                                   + list(tch.send_response_to_topic.call_args_list)))
        assert posted.count("• spooled text") == 1
        assert posted.count("• reserved but never spooled") == 1
        assert posted.count("up to 3 inbound message(s)") == 1
        assert posted.count("up to 4 inbound message(s)") == 0


class TestReservationReadTimeExclusion:
    """C4 RED CASES, ROUND 2 — INV-ENG-017 (re-declared) at the emit_completion
    terminal, through a REAL `ClaudeCodeDriver` and a REAL `_InboundSpool`.

    Every assertion here is on the notice the terminal actually posted, never
    on a population reconstructed inside the test. `_FakeInboundDriver(real=…)`
    delegates every inbound accessor to the real driver, so what is exercised
    is the real read-time exclusion; only the terminal machinery around it is
    a stand-in.

    Specified by sol (round `redcase2-specify`); accepted by terra.
    """

    _emit = TestCompletionInboundGate._emit
    _setup = TestReservationDisclosure._setup

    @staticmethod
    async def _real(tmp_path, eid):
        """A real driver with a real spool attached at the production path."""
        import os
        from unittest.mock import AsyncMock as _AM
        import drivers.claude_code_driver as ccd
        from drivers.workspace import control_dir, inbound_spool_path
        d = ccd.ClaudeCodeDriver(
            engagements_root=str(tmp_path / "eng"),
            send_to_topic=_AM(), casa_framework_mcp_url="http://x")
        os.makedirs(control_dir(eid), exist_ok=True)
        spool = ccd._InboundSpool(
            engagement_id=eid, spool_path=inbound_spool_path(eid),
            write_fifo=_AM(return_value=False),
            send_notice=_AM(return_value=True))
        d._inbound[eid] = spool
        return d, spool

    async def _render(self, tmp_path, real, eid, status="error"):
        import agent as agent_mod
        drv = _FakeInboundDriver(real=real, real_eid=eid)
        reg, rec, tch, _bus = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec, status=status)
        finally:
            agent_mod.active_claude_code_driver = None
        assert payload["status"] == "acknowledged"
        return TestReservationDisclosure._posted(tch)

    async def test_redelivered_message_is_quoted_once_after_its_envelope_is_consumed(
            self, tmp_path):
        """THE red case at the renderer. Telegram redelivers message 41; both
        deliveries reserve; delivery A persists, releases, is delivered and is
        consumed by a real `on_turn_start`, which prunes its envelope. B is
        still held.

        Red at `3bb55f2e`: the terminal posts `up to 1 inbound message(s)` with
        no bullet at all — the operator's words exist in no population."""
        eid = "e-render-redeliver"
        d, spool = await self._real(tmp_path, eid)
        d.reserve_inbound(eid, text="hello", message_id=41)
        d.reserve_inbound(eid, text="hello", message_id=41)
        assert await spool.enqueue("hello", tg_message_id=41) == "queued"
        d.release_inbound_reservation(eid, message_id=41)
        for e in spool._envelopes:
            e.state = "delivered"
        await spool.on_turn_start()

        posted = await self._render(tmp_path, d, eid)
        assert posted.count("• hello") == 1, posted
        assert posted.count("up to 1 inbound message(s)") == 1, posted

    async def test_a_live_envelope_and_its_alias_reservation_produce_one_bullet(
            self, tmp_path):
        """While A's envelope is QUEUED the message is quoted once, from the
        spool, and the count still says two because two reservations are held.
        The hedge and the arithmetic are exactly what a text-less pair of
        reservations would have produced."""
        eid = "e-render-queued"
        d, spool = await self._real(tmp_path, eid)
        d.reserve_inbound(eid, text="hello", message_id=41)
        d.reserve_inbound(eid, text="hello", message_id=41)
        assert await spool.enqueue("hello", tg_message_id=41) == "queued"

        posted = await self._render(tmp_path, d, eid)
        assert posted.count("• hello") == 1, posted
        assert posted.count("up to 3 inbound message(s)") == 1, posted

    async def test_a_delivered_envelope_and_its_alias_reservation_produce_one_bullet(
            self, tmp_path):
        """Stopped in the DELIVERED state, before any turn_start. Kills the
        mutant that omits the in-flight arm of the exclusion union — Sol
        measured that one at two bullets for one message."""
        eid = "e-render-inflight"
        d, spool = await self._real(tmp_path, eid)
        d.reserve_inbound(eid, text="hello", message_id=41)
        d.reserve_inbound(eid, text="hello", message_id=41)
        assert await spool.enqueue("hello", tg_message_id=41) == "queued"
        for e in spool._envelopes:
            e.state = "delivered"

        posted = await self._render(tmp_path, d, eid)
        assert posted.count("• hello") == 1, posted
        assert posted.count("up to 3 inbound message(s)") == 1, posted

    async def test_two_held_reservations_for_one_message_render_one_bullet(
            self, tmp_path):
        """No spool population at all: two occurrences of one message id, one
        bullet, and a count that still says two."""
        eid = "e-render-onebullet"
        d, _spool = await self._real(tmp_path, eid)
        d.reserve_inbound(eid, text="hello", message_id=41)
        d.reserve_inbound(eid, text="hello", message_id=41)

        posted = await self._render(tmp_path, d, eid)
        assert posted.count("• hello") == 1, posted
        assert posted.count("up to 2 inbound message(s)") == 1, posted

    async def test_identical_text_under_distinct_ids_renders_two_bullets(
            self, tmp_path):
        """Kills dedupe-by-text at the renderer: two different messages with
        identical words were both lost and both must be shown."""
        eid = "e-render-twoids"
        d, _spool = await self._real(tmp_path, eid)
        d.reserve_inbound(eid, text="hello", message_id=41)
        d.reserve_inbound(eid, text="hello", message_id=42)

        posted = await self._render(tmp_path, d, eid)
        assert posted.count("• hello") == 2, posted
        assert posted.count("up to 2 inbound message(s)") == 1, posted

    async def test_a_boolean_envelope_id_does_not_suppress_message_one(
            self, tmp_path):
        """`True == 1`: a malformed envelope must not silence an unrelated
        reservation. Both texts are rendered."""
        eid = "e-render-boolid"
        d, spool = await self._real(tmp_path, eid)
        assert await spool.enqueue("spooled", tg_message_id=41) == "queued"
        spool._envelopes[0].tg_message_id = True
        d.reserve_inbound(eid, text="held", message_id=1)

        posted = await self._render(tmp_path, d, eid)
        assert posted.count("• spooled") == 1, posted
        assert posted.count("• held") == 1, posted
        assert posted.count("up to 2 inbound message(s)") == 1, posted


class _AccessorProbe:
    """A duck driver exposing exactly the eight inbound accessors, recording
    every call, with any subset made to RAISE or to be ABSENT.

    Local rather than a widened `_FakeInboundDriver`: that fake has no
    `inbound_evicted_pending_texts`, and every fixture written against it
    depends on that absence answering `hasattr` — giving it one would silently
    change their disclosure. Everything that is not an inbound accessor raises
    `AttributeError`, exactly as a duck driver's absent method would, so the
    funnel takes its fallback paths and the post the mocked channel received is
    what the assertions read.
    """

    _ACCESSORS = (
        "inbound_unread_texts", "inbound_unread_depth", "inbound_reservations",
        "inbound_message_reservations", "inbound_reservation_texts",
        "inbound_in_flight_texts", "inbound_in_flight_blocking",
        "inbound_evicted_pending_texts",
    )
    # Class-level defaults so `__getattr__` can consult them before `__init__`
    # has bound the instance ones (otherwise the lookup recurses).
    _broken: frozenset = frozenset()
    _absent: frozenset = frozenset()
    _values: dict = {}

    def __init__(self, *, broken=(), absent=(), texts=(), depth=0,
                 reservations=0, message_reservations=0, reservation_texts=(),
                 in_flight=(), in_flight_blocking=0, evicted=()):
        self._broken = frozenset(
            [broken] if isinstance(broken, str) else broken)
        self._absent = frozenset(
            [absent] if isinstance(absent, str) else absent)
        self._values = {
            "inbound_unread_texts": list(texts),
            "inbound_unread_depth": depth,
            "inbound_reservations": reservations,
            "inbound_message_reservations": message_reservations,
            "inbound_reservation_texts": list(reservation_texts),
            "inbound_in_flight_texts": list(in_flight),
            "inbound_in_flight_blocking": in_flight_blocking,
            "inbound_evicted_pending_texts": list(evicted),
        }
        unknown = (self._broken | self._absent) - set(self._ACCESSORS)
        assert not unknown, f"not an inbound accessor: {sorted(unknown)}"
        self.calls: dict[str, int] = {}

    def __getattr__(self, name):
        if name not in self._ACCESSORS or name in self._absent:
            raise AttributeError(name)

        def _read(_eng_id):
            self.calls[name] = self.calls.get(name, 0) + 1
            if name in self._broken:
                raise RuntimeError(f"{name} is broken")
            value = self._values[name]
            return list(value) if isinstance(value, list) else value

        return _read


class TestOneFailingAccessorCostsOnlyItsOwnInput:
    """#807 — a driver accessor that fails changes what the terminal hook
    KNOWS, never what it DOES about everything else it read.

    `_finalize_engagement` is called DIRECTLY: `emit_completion`'s pre-check
    (which reads depth / reservations / in-flight blocking in its own tries
    before the funnel) would refuse first and mask the hook's own arm, so the
    funnel is the only place the hook's decision is observable.

    Pins INV-ENG-003: the veto still fires on every arm the hook could still
    read, and every terminal still discloses every population that did read.
    """

    async def _setup(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"),
                                 bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="probe-exec", driver="claude_code",
            task="t", origin={"role": "assistant", "channel": "telegram"},
            topic_id=42,
        )
        tch = MagicMock()
        tch.send_to_topic = AsyncMock(return_value=11)
        tch.send_response_to_topic = AsyncMock(return_value=12)
        tch.close_topic = AsyncMock()
        cm = MagicMock(); cm.get.return_value = tch
        bus = MagicMock(); bus.notify = AsyncMock()
        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        return reg, rec, tch

    @staticmethod
    def _posted(tch):
        return "".join(
            str(c.args) + str(c.kwargs)
            for c in (list(tch.send_to_topic.call_args_list)
                      + list(tch.send_response_to_topic.call_args_list)))

    @staticmethod
    async def _finalize(rec, drv, *, outcome, gate):
        from tools import _finalize_engagement

        return await _finalize_engagement(
            rec, outcome=outcome, text="done", artifacts=[], next_steps=[],
            driver=drv, inbound_gate=gate)

    # ---- red cases ----------------------------------------------------

    @pytest.mark.parametrize("mode", ["absent", "raises"])
    @pytest.mark.parametrize("arm", ["depth", "blocking", "reservations"])
    async def test_a_missing_or_raising_unread_text_read_never_erases_a_readable_veto(
            self, tmp_path, mode, arm):
        """The #807 reproduction. The unread-text read is the hook's first,
        and it must not decide for the arms below it: with it absent or
        raising and exactly ONE other veto arm armed, a gated completion is
        still refused. Kills both whole-hook early returns."""
        from tools import FinalizeResult

        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(
            broken="inbound_unread_texts" if mode == "raises" else (),
            absent="inbound_unread_texts" if mode == "absent" else (),
            depth=1 if arm == "depth" else 0,
            in_flight_blocking=1 if arm == "blocking" else 0,
            reservations=1 if arm == "reservations" else 0)

        result = await self._finalize(rec, drv, outcome="completed", gate=True)

        assert result is FinalizeResult.PRECONDITION_FAILED
        assert rec.status not in ("completed", "error", "cancelled")
        assert tch.send_response_to_topic.await_count == 0
        assert drv.calls.get({"depth": "inbound_unread_depth",
                              "blocking": "inbound_in_flight_blocking",
                              "reservations": "inbound_reservations"}[arm]) == 1

    async def test_a_raising_in_flight_text_read_never_erases_the_blocking_veto(
            self, tmp_path):
        """The in-flight pair shared ONE try, so the texts read failing zeroed
        the blocking count with it — a new accessor's failure switching off a
        veto arm the gate can still read. Kills re-merging the two tries."""
        from tools import FinalizeResult

        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(broken="inbound_in_flight_texts",
                             in_flight_blocking=1)

        result = await self._finalize(rec, drv, outcome="completed", gate=True)

        assert result is FinalizeResult.PRECONDITION_FAILED
        assert rec.status not in ("completed", "error", "cancelled")
        assert drv.calls.get("inbound_in_flight_blocking") == 1
        assert tch.send_response_to_topic.await_count == 0

    @pytest.mark.parametrize("mode", ["absent", "raises"])
    @pytest.mark.parametrize("outcome", ["completed", "cancelled", "error"])
    async def test_an_unread_text_failure_preserves_every_other_population(
            self, tmp_path, mode, outcome):
        """Ungated, so every terminal reaches the renderer: the in-flight,
        reservation and evicted populations still reach the topic with their
        count when the unread-text read is absent or raising."""
        from tools import FinalizeResult

        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(
            broken="inbound_unread_texts" if mode == "raises" else (),
            absent="inbound_unread_texts" if mode == "absent" else (),
            in_flight=["flight"], message_reservations=1,
            reservation_texts=["held"], evicted=["evicted"])

        result = await self._finalize(rec, drv, outcome=outcome, gate=False)

        assert result is FinalizeResult.FINALIZED
        posted = self._posted(tch)
        assert posted.count("• flight") == 1, posted
        assert posted.count("• evicted") == 1, posted
        assert posted.count("• held") == 1, posted
        assert posted.count("up to 3 inbound message(s)") == 1, posted
        for name in ("inbound_in_flight_texts", "inbound_message_reservations",
                     "inbound_reservation_texts",
                     "inbound_evicted_pending_texts"):
            assert drv.calls.get(name) == 1, (name, drv.calls)

    @pytest.mark.parametrize("outcome", ["completed", "cancelled", "error"])
    async def test_a_blocking_failure_preserves_the_in_flight_text(
            self, tmp_path, outcome):
        """The other half of the split: the blocking read failing costs only
        the veto contribution, never the in-flight text already read."""
        from tools import FinalizeResult

        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(broken="inbound_in_flight_blocking",
                             in_flight=["flight"])

        result = await self._finalize(rec, drv, outcome=outcome, gate=False)

        assert result is FinalizeResult.FINALIZED
        posted = self._posted(tch)
        assert posted.count("• flight") == 1, posted
        assert posted.count("1 inbound message(s)") == 1, posted
        assert posted.count("up to") == 0, posted

    async def test_the_unread_text_warning_no_longer_claims_the_gate_was_skipped(
            self, tmp_path, caplog):
        """The diagnostic is the operator's only trace of the failure. It said
        "gate skipped"; the gate now stands and vetoes, so saying so would be
        a false statement about a refusal that did happen."""
        import logging

        from tools import FinalizeResult

        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(broken="inbound_unread_texts", reservations=1)
        with caplog.at_level(logging.WARNING, logger="tools"):
            result = await self._finalize(rec, drv, outcome="completed",
                                          gate=True)

        assert result is FinalizeResult.PRECONDITION_FAILED
        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno >= logging.WARNING]
        named = [m for m in warnings if "inbound_unread_texts" in m]
        assert len(named) == 1, warnings
        assert "gate skipped" not in "".join(warnings), warnings

    async def test_an_unread_text_failure_discloses_and_never_raises_without_a_depth_accessor(
            self, tmp_path):
        """Red at base for its disclosure (the early return drops the held
        reservation), and it is ALSO the definedness pin: the hook runs
        synchronously inside the registry's terminal critical section, where an
        exception propagates out of `try_transition_terminal` and is misread as
        a persist failure that leaves the record live. The absent-depth
        fallback reads the DEGRADED text list, so a failed text read must leave
        it defined and empty rather than unbound."""
        from tools import FinalizeResult

        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(broken="inbound_unread_texts",
                             absent="inbound_unread_depth",
                             reservation_texts=["held"],
                             message_reservations=1)

        result = await self._finalize(rec, drv, outcome="cancelled", gate=False)

        assert result is FinalizeResult.FINALIZED
        assert rec.status == "cancelled"
        assert self._posted(tch).count("• held") == 1

    # ---- the 8-accessor matrix: outcome, then post contents -------------
    #
    # `none` plus each accessor raising, over a driver with EVERY veto arm
    # armed and EVERY disclosure population non-empty. Only the
    # `inbound_unread_texts` rows are red at the pre-fix tree; the rest pin
    # the containment the other seven guards already had, so a fix that
    # widened one of them would be caught here.

    _FULL = dict(depth=1, reservations=1, in_flight_blocking=1,
                 message_reservations=1, texts=["q-text"],
                 in_flight=["f-text"], evicted=["e-text"],
                 reservation_texts=["r-text"])

    # broken -> (bullets that must survive, the count copy the renderer emits)
    _MATRIX = {
        None: (("q-text", "f-text", "e-text", "r-text"), "up to 4"),
        "inbound_unread_texts": (("f-text", "e-text", "r-text"), "up to 3"),
        "inbound_unread_depth": (("q-text", "f-text", "e-text", "r-text"),
                                 "up to 4"),
        "inbound_reservations": (("q-text", "f-text", "e-text", "r-text"),
                                 "up to 4"),
        # The COUNT is deliberately not asserted for this row. Its text read
        # succeeds while its count read fails, and `lost_reservations` is the
        # only count unit the reservation population contributes — so the
        # renderer quotes four bullets under an exact count of three. That
        # incoherence is PRE-EXISTING (it is reachable at the base with this
        # same driver, unchanged by this fix) and is filed as #848; pinning
        # it as observed would codify an undercount, so this row pins only the
        # containment #807 is about: every readable bullet survives.
        "inbound_message_reservations": (
            ("q-text", "f-text", "e-text", "r-text"), None),
        "inbound_reservation_texts": (("q-text", "f-text", "e-text"),
                                      "up to 4"),
        "inbound_in_flight_texts": (("q-text", "e-text", "r-text"), "up to 3"),
        "inbound_in_flight_blocking": (
            ("q-text", "f-text", "e-text", "r-text"), "up to 4"),
        "inbound_evicted_pending_texts": (("q-text", "f-text", "r-text"),
                                          "up to 3"),
    }

    @pytest.mark.parametrize("broken", list(_MATRIX))
    async def test_no_single_accessor_failure_lets_a_gated_completion_through(
            self, tmp_path, broken):
        from tools import FinalizeResult

        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(broken=broken or (), **self._FULL)

        result = await self._finalize(rec, drv, outcome="completed", gate=True)

        assert result is FinalizeResult.PRECONDITION_FAILED
        assert rec.status not in ("completed", "error", "cancelled")

    @pytest.mark.parametrize("broken", list(_MATRIX))
    async def test_a_single_accessor_failure_costs_only_its_own_bullet(
            self, tmp_path, broken):
        from tools import FinalizeResult

        expected, count = self._MATRIX[broken]
        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(broken=broken or (), **self._FULL)

        result = await self._finalize(rec, drv, outcome="cancelled", gate=False)

        assert result is FinalizeResult.FINALIZED
        posted = self._posted(tch)
        for sentinel in ("q-text", "f-text", "e-text", "r-text"):
            assert posted.count("• " + sentinel) == (
                1 if sentinel in expected else 0), (sentinel, posted)
        if count is not None:
            assert posted.count(f"{count} inbound message(s)") == 1, posted

    # ---- mutation checks (GREEN at the pre-fix tree, not red cases) ------

    @pytest.mark.parametrize("condition", [
        ("raises", "inbound_unread_texts"),
        ("absent", "inbound_unread_texts"),
        ("raises", "inbound_in_flight_texts"),
        ("raises", "inbound_in_flight_blocking"),
    ])
    async def test_an_unreadable_accessor_with_no_other_evidence_fails_open(
            self, tmp_path, condition):
        """Option B is NOT what was built: an input the gate cannot read is
        never itself a refusal. Kills a fix that makes any new guard
        fail-closed."""
        from tools import FinalizeResult

        mode, name = condition
        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(broken=name if mode == "raises" else (),
                             absent=name if mode == "absent" else ())

        result = await self._finalize(rec, drv, outcome="completed", gate=True)

        assert result is FinalizeResult.FINALIZED
        assert rec.status == "completed"
        assert "inbound message(s)" not in self._posted(tch)

    async def test_a_driver_exposing_no_inbound_accessor_is_not_gated(
            self, tmp_path):
        """The published "a driver that implements none of them is not gated"
        outcome, pinned so dropping the unread-text `hasattr` short-circuit
        cannot quietly change it."""
        from tools import FinalizeResult

        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(absent=_AccessorProbe._ACCESSORS)

        result = await self._finalize(rec, drv, outcome="completed", gate=True)

        assert result is FinalizeResult.FINALIZED
        assert rec.status == "completed"
        assert "inbound message(s)" not in self._posted(tch)
        assert drv.calls == {}

    async def test_an_absent_depth_accessor_still_vetoes_on_readable_texts(
            self, tmp_path):
        """The other side of that fallback: a duck driver exposing only the
        text read keeps deriving its depth from it. Kills replacing the
        `len(texts)` fallback with a bare literal 0."""
        from tools import FinalizeResult

        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(absent="inbound_unread_depth", texts=["q-text"])

        result = await self._finalize(rec, drv, outcome="completed", gate=True)

        assert result is FinalizeResult.PRECONDITION_FAILED
        assert rec.status not in ("completed", "error", "cancelled")
