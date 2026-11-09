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

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
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
                "text": "done", "next_steps": [], "status": "ok",
            })
        finally:
            engagement_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] == "acknowledged"
        assert rec.status == "completed"

    async def test_returns_not_in_engagement_when_outside(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import emit_completion, init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
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

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
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

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
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


def _wire_engagement(tmp_path):
    """Registry + mocks + init_tools; returns (rec, registry, telegram, bus)."""
    from engagement_registry import EngagementRegistry
    from tools import init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
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
            kind="specialist", role_or_type="finance", driver="in_casa",
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


class TestFinalizeResultMapping:
    """G4 D5 (v0.96.0): emit_completion must not acknowledge a completion
    that did not actually finalize (Sol g4-r1-6 — the persistence-rollback
    False was previously acked as success)."""

    async def _setup(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import init_tools
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
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
                "text": "done", "next_steps": [],
                "status": "ok"})
        finally:
            engagement_var.reset(token)
        payload = json.loads(res["content"][0]["text"])
        assert payload["status"] != "acknowledged"
        assert payload["kind"] == "finalize_persist_failed"
        assert payload.get("retryable") is True
        assert rec.status not in ("completed", "error", "cancelled")


class _FakeInboundDriver:
    """Minimal stand-in for the in_casa driver's inbound surface, read by the
    G4 completion gate and the terminal disclosure.

    `_texts`/`_depth` are the UNREAD population (admitted, not yet handed to
    the client); `_in_flight` is the disclosure-only population the client
    accepted with no model-evidence frame processed yet. Reservation texts are
    always empty on the in_casa driver, but the renderers still read the
    accessor, so it stays settable here to pin that rendering.
    """
    def __init__(self, depth=0, reservations=0, texts=(),
                 in_flight=(), command_reservations=0,
                 reservation_texts=()):
        self._depth = depth
        self._resv = reservations
        self._cmd_resv = command_reservations
        self._texts = list(texts)
        self._in_flight = list(in_flight)
        self._reservation_texts = list(reservation_texts)

    def inbound_unread_depth(self, eng_id):
        return self._depth
    def inbound_reservations(self, eng_id):
        return self._resv
    def inbound_message_reservations(self, eng_id):
        # #664: the disclosure projection — reservations minus the ones a
        # recognized command holds for itself (mirrors the real driver).
        return max(0, self._resv - self._cmd_resv)
    def inbound_unread_texts(self, eng_id):
        return list(self._texts)
    def inbound_in_flight_texts(self, eng_id):
        return list(self._in_flight)
    def inbound_reservation_texts(self, eng_id):
        return list(self._reservation_texts)


def _arrive_before_the_flip(monkeypatch, reg, drv, *, depth, texts):
    """Make inbound input land after emit_completion's pre-check but before
    the registry's terminal transition: the wrapped transition mutates the
    driver's unread population, then runs the real one (whose terminal hook
    must now see it)."""
    real = reg.try_transition_terminal

    async def _racing(*a, **kw):
        drv._depth = depth
        drv._texts = list(texts)
        return await real(*a, **kw)

    monkeypatch.setattr(reg, "try_transition_terminal", _racing)


class TestCompletionInboundGate:
    """G4 D1/D2 (v0.96.0): emit_completion must not complete past unread
    operator input."""

    async def _setup(self, tmp_path, driver):
        import agent as agent_mod
        from engagement_registry import EngagementRegistry
        from tools import init_tools
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            kind="specialist", role_or_type="probe-exec",
            driver="in_casa", task="t",
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
        agent_mod.active_engagement_driver = driver
        return reg, rec, tch

    async def _emit(self, rec, status="ok"):
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler({
                "text": "done", "next_steps": [],
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
            agent_mod.active_engagement_driver = None
        assert payload["kind"] == "unread_inbound"
        assert payload.get("retryable") is True
        assert rec.status not in ("completed", "error", "cancelled")

    async def test_pending_ingress_reservation_refuses_completion(
            self, tmp_path):
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=0, reservations=1)
        reg, rec, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_engagement_driver = None
        assert payload["kind"] == "unread_inbound"
        assert rec.status not in ("completed", "error", "cancelled")

    async def test_clean_spool_completes(self, tmp_path):
        import agent as agent_mod
        drv = _FakeInboundDriver()
        reg, rec, _ = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_engagement_driver = None
        assert payload["status"] == "acknowledged"
        assert rec.status == "completed"

    async def test_inbound_racing_past_precheck_is_vetoed_by_terminal_hook(
            self, tmp_path, monkeypatch):
        """Pins INV-ENG-003's second enforcement layer: the gate re-evaluates
        INSIDE the terminal transition, so input that arrives after
        emit_completion's pre-check but before the flip still vetoes.

        Red case demonstrated: neutering the terminal hook's veto
        (`if inbound_gate and (texts or resv):` → `if False:`) completes this
        engagement and fails the test. Breaking emit_completion's pre-check
        alone fails nothing — the hook backstops it — so the hook is where
        the invariant is actually pinned; the pre-check is an optimisation.
        """
        import agent as agent_mod
        drv = _FakeInboundDriver()
        reg, rec, _ = await self._setup(tmp_path, drv)
        _arrive_before_the_flip(monkeypatch, reg, drv, depth=1,
                                texts=["arrived during completion"])
        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_engagement_driver = None
        assert payload["kind"] == "unread_inbound"
        assert rec.status not in ("completed", "error", "cancelled")

    async def test_error_status_not_gated_and_lists_no_inbound(self, tmp_path):
        """A broken engagement must be able to die even with unread input
        (D4). Its end lists no inbound message: each one that dies with it is
        told once, by its own ticket's holder, in reply to the message."""
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=1, texts=["msg-that-was-never-read"],
                                 reservations=1)
        reg, rec, tch = await self._setup(tmp_path, drv)
        try:
            payload = await self._emit(rec, status="error")
        finally:
            agent_mod.active_engagement_driver = None
        assert payload["status"] == "acknowledged"
        assert rec.status == "error"
        posted = "".join(str(c.args) + str(c.kwargs)
                         for c in (list(tch.send_to_topic.call_args_list)
                                   + list(tch.send_response_to_topic.call_args_list)))
        assert "inbound message(s)" not in posted
        assert "msg-that-was-never-read" not in posted

    async def test_race_message_lands_between_gate_and_flip(
            self, tmp_path, monkeypatch):
        """G4 D2: depth flips to >0 after the handler gate but before the
        terminal mutation — the registry-internal hook must refuse."""
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=0)
        reg, rec, _ = await self._setup(tmp_path, drv)
        _arrive_before_the_flip(monkeypatch, reg, drv, depth=1,
                                texts=["late message"])

        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_engagement_driver = None
        assert payload["kind"] == "unread_inbound"
        assert rec.status not in ("completed", "error", "cancelled")


class TestReservationGate:
    """A reservation vetoes a successful completion exactly as an unread
    ticket does, a command's reservation included, and an accessor failure
    never switches the veto off. No terminal lists the messages behind
    them: each is told by its own ticket's holder."""

    _emit = TestCompletionInboundGate._emit

    async def _setup(self, tmp_path, driver):
        import agent as agent_mod
        from engagement_registry import EngagementRegistry
        from tools import init_tools
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            kind="specialist", role_or_type="probe-exec",
            driver="in_casa", task="t",
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
        agent_mod.active_engagement_driver = driver
        return reg, rec, tch, bus

    @staticmethod
    def _posted(tch):
        return "".join(
            str(c.args) + str(c.kwargs)
            for c in (list(tch.send_to_topic.call_args_list)
                      + list(tch.send_response_to_topic.call_args_list)))

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
            agent_mod.active_engagement_driver = None
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
            agent_mod.active_engagement_driver = None
        assert payload["status"] == "acknowledged"
        assert "inbound message(s)" not in self._posted(tch)

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
            agent_mod.active_engagement_driver = None
        assert payload["kind"] == "unread_inbound"
        assert rec.status not in ("completed", "error", "cancelled")

class TestEmitCompletionCancellation:
    """#632 red case: the finalize tail must survive its own driver teardown.

    An in_casa emit_completion runs inside an SDK control-request handler task
    that ``Query._close_impl`` cancels when the funnel's own driver teardown
    closes the client (query.py:998-999 on the pinned SDK). The cancellation
    is simulated DIRECTLY — the fake driver's ``cancel()`` cancels the
    captured host task and then suspends once, exactly as the funnel is
    suspended awaiting teardown in production — so the case fails identically
    on CPython 3.11 and 3.12 (``asyncio.wait_for`` topology is not involved).

    Invariant (INV-ENG-010, docs/architecture/engagement-finalization.md;
    issue #632): once the terminal transition is won, the DelegationComplete
    bus notification and the finalized log must both still happen even
    though the host task ends cancelled.
    """

    async def test_in_casa_teardown_cannot_cancel_finalize_tail(
            self, tmp_path, monkeypatch):
        import agent as agent_mod
        import tools as tools_mod
        from engagement_registry import EngagementRegistry
        from tools import emit_completion, init_tools, engagement_var

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
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
                    "text": "done", "next_steps": [],
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
        assert events.count("post") == 0          # no restart on this path
        assert events.count("final-log") == 1
        assert events == ["cancel", "notify", "final-log"], events


class TestFinalizeTailObservability:
    """#632 seam findings (Sol S1, Terra F3): the detached tail must be
    visibly anchored while it runs, its failures must be observed, and its
    payloads must carry the values frozen at the terminal flip."""

    async def _launch(self, tmp_path, monkeypatch, *, driver, notify=None):
        import agent as agent_mod
        import tools as tools_mod
        from engagement_registry import EngagementRegistry
        from tools import emit_completion, init_tools, engagement_var

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
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
        monkeypatch.setattr(agent_mod, "active_engagement_driver", driver,
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
        """A post-terminal ``lower_origin_clearance`` (real mutator — no
        terminal refusal) lands during the funnel's post-flip driver-teardown
        await; the DelegationComplete must still carry the flip-time
        origin."""
        seen: dict = {}

        async def _notify(msg):
            seen["origin"] = dict(msg.content.origin)

        reg_box: dict = {}
        rec_box: dict = {}

        class _Driver:
            async def cancel(self, engagement):
                # The clamp arrives while the funnel is suspended post-flip.
                await reg_box["reg"].lower_origin_clearance(
                    rec_box["rec"].id, "public")

        tools_mod_, reg, rec, bus, host = await self._launch(
            tmp_path, monkeypatch, driver=_Driver(), notify=_notify)
        # _launch built reg/rec before quiesce could reference them:
        reg_box["reg"] = reg
        rec_box["rec"] = rec
        payload = json.loads((await host)["content"][0]["text"])
        assert payload["status"] == "acknowledged"
        # The real mutator DID fire post-flip:
        assert rec.origin["_origin_clearance"] == "public"
        assert rec.task != "pay rent"
        # ...and the completion payload carries the flip-time origin:
        assert seen["origin"]["_origin_clearance"] == "private"

class _AccessorProbe:
    """A duck driver exposing exactly the six inbound accessors the in_casa
    driver offers, recording every call, with any subset made to RAISE or to
    be ABSENT.

    Everything that is not an inbound accessor raises `AttributeError`,
    exactly as a duck driver's absent method would, so the funnel takes its
    fallback paths and the post the mocked channel received is what the
    assertions read.
    """

    _ACCESSORS = (
        "inbound_unread_texts", "inbound_unread_depth", "inbound_reservations",
        "inbound_message_reservations", "inbound_reservation_texts",
        "inbound_in_flight_texts",
    )
    # Class-level defaults so `__getattr__` can consult them before `__init__`
    # has bound the instance ones (otherwise the lookup recurses).
    _broken: frozenset = frozenset()
    _absent: frozenset = frozenset()
    _values: dict = {}

    def __init__(self, *, broken=(), absent=(), texts=(), depth=0,
                 reservations=0, message_reservations=0, reservation_texts=(),
                 in_flight=()):
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
    (which reads depth and reservations before the funnel) would refuse first and mask the hook's own arm, so the
    funnel is the only place the hook's decision is observable.

    Pins INV-ENG-003: the veto still fires on every arm the hook could still
    read.
    """

    async def _setup(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            kind="specialist", role_or_type="probe-exec", driver="in_casa",
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
            rec, outcome=outcome, text="done", next_steps=[],
            driver=drv, inbound_gate=gate)

    # ---- red cases ----------------------------------------------------

    @pytest.mark.parametrize("mode", ["absent", "raises"])
    @pytest.mark.parametrize("arm", ["depth", "reservations"])
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
            reservations=1 if arm == "reservations" else 0)

        result = await self._finalize(rec, drv, outcome="completed", gate=True)

        assert result is FinalizeResult.PRECONDITION_FAILED
        assert rec.status not in ("completed", "error", "cancelled")
        assert tch.send_response_to_topic.await_count == 0
        assert drv.calls.get({"depth": "inbound_unread_depth",
                              "reservations": "inbound_reservations"}[arm]) == 1

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

    async def test_an_unread_text_failure_never_raises_without_a_depth_accessor(
            self, tmp_path):
        """The definedness pin: the hook runs synchronously inside the
        registry's terminal critical section, where an exception propagates
        out of `try_transition_terminal` and is misread as a persist failure
        that leaves the record live. The absent-depth fallback reads the
        DEGRADED text list, so a failed text read must leave it defined and
        empty rather than unbound."""
        from tools import FinalizeResult

        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(broken="inbound_unread_texts",
                             absent="inbound_unread_depth")

        result = await self._finalize(rec, drv, outcome="completed", gate=True)

        assert result is FinalizeResult.FINALIZED
        assert rec.status == "completed"

    # ---- the accessor matrix ---------------------------------------------
    #
    # `none` plus each accessor raising, over a driver with EVERY veto arm
    # armed: no single failure lets a gated completion through.

    _FULL = dict(depth=1, reservations=1,
                 message_reservations=1, texts=["q-text"],
                 in_flight=["f-text"], reservation_texts=["r-text"])

    _MATRIX = (None,) + _AccessorProbe._ACCESSORS

    @pytest.mark.parametrize("broken", list(_MATRIX))
    async def test_no_single_accessor_failure_lets_a_gated_completion_through(
            self, tmp_path, broken):
        from tools import FinalizeResult

        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(broken=broken or (), **self._FULL)

        result = await self._finalize(rec, drv, outcome="completed", gate=True)

        assert result is FinalizeResult.PRECONDITION_FAILED
        assert rec.status not in ("completed", "error", "cancelled")

    @pytest.mark.parametrize("condition", [
        ("raises", "inbound_unread_texts"),
        ("absent", "inbound_unread_texts"),
        ("raises", "inbound_in_flight_texts"),
        ("raises", "inbound_message_reservations"),
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
