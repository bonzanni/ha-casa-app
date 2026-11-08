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


def _wire_engagement(tmp_path):
    """Registry + mocks + init_tools; returns (rec, registry, telegram, bus)."""
    from engagement_registry import EngagementRegistry
    from tools import init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
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
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"),
                                 bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="probe-exec",
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

    async def test_error_status_not_gated_but_annotated(self, tmp_path):
        """A broken engagement must be able to die even with unread input;
        the topic post carries the never-read texts (D4)."""
        import agent as agent_mod
        drv = _FakeInboundDriver(depth=1, texts=["msg-that-was-never-read"])
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
        # #591: the copy claims what Casa can evidence (no turn start recorded)
        # rather than asserting the message was never read — which is not
        # provable for a message already handed to the client.
        assert "no turn start recorded" in posted
        assert "msg-that-was-never-read" in posted

    async def test_error_terminal_discloses_a_pending_reservation_count(
            self, tmp_path):
        """RED CASE (#664, reviewer-specified): a message still inside its
        ingress-reservation window — accepted by the Telegram handler,
        reserved, not yet admitted, so no text exists anywhere — dies with an
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
            agent_mod.active_engagement_driver = None
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
        _arrive_before_the_flip(monkeypatch, reg, drv, depth=1,
                                texts=["late message"])

        try:
            payload = await self._emit(rec)
        finally:
            agent_mod.active_engagement_driver = None
        assert payload["kind"] == "unread_inbound"
        assert rec.status not in ("completed", "error", "cancelled")


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
            agent_mod.active_engagement_driver = None
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
            agent_mod.active_engagement_driver = None
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
            agent_mod.active_engagement_driver = None
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
            agent_mod.active_engagement_driver = None
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
            agent_mod.active_engagement_driver = None
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
            agent_mod.active_engagement_driver = None
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
            agent_mod.active_engagement_driver = None
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
            agent_mod.active_engagement_driver = None
        assert "up to 1 inbound message(s)" in self._posted(tch)
        assert "inbound message(s)" not in str(bus.notify.call_args_list)


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

        async def _capture(frozen, text, *, allowed):
            events.append("capture")
            return True
        monkeypatch.setattr(tools_mod, "_capture_summary", _capture)

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
        assert events.count("capture") == 2
        assert events.count("post") == 1
        assert events.count("final-log") == 1
        assert rec.id not in tools_mod._ENGAGEMENTS_DEFERRED_HARD_RELOAD
        assert events.index("cancel") < events.index("notify")
        assert events.index("notify") < min(
            i for i, e in enumerate(events) if e == "capture")
        assert max(
            i for i, e in enumerate(events) if e == "capture"
        ) < events.index("post")
        assert events.index("post") < events.index("final-log")


class TestFinalizeTailObservability:
    """#632 seam findings (Sol S1, Terra F3): the detached tail must be
    visibly anchored while it runs, its failures must be observed, and its
    payloads must carry the values frozen at the terminal flip."""

    async def _launch(self, tmp_path, monkeypatch, *, driver, notify=None):
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
        """Terra seam F3: a post-terminal ``lower_origin_clearance`` (real
        mutator — no terminal refusal) lands during the funnel's post-flip
        driver-teardown await; the DelegationComplete must still carry the
        flip-time origin and the captured summary the flip-time task."""
        import tools as tools_mod
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

        captured: list[str] = []

        async def _capture(frozen, text, *, allowed):
            captured.append(text)
            return True
        monkeypatch.setattr(tools_mod, "_capture_summary", _capture)

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
        # ...and the completion payloads carry the flip-time values:
        assert seen["origin"]["_origin_clearance"] == "private"
        tasks = [json.loads(t)["task"] for t in captured
                 if json.loads(t)["kind"] == "engagement_summary"]
        assert tasks == ["pay rent"]

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
    read, and every terminal still discloses every population that did read.
    """

    async def _setup(self, tmp_path):
        from engagement_registry import EngagementRegistry
        from tools import init_tools

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"),
                                 bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="probe-exec", driver="in_casa",
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

    @pytest.mark.parametrize("mode", ["absent", "raises"])
    @pytest.mark.parametrize("outcome", ["completed", "cancelled", "error"])
    async def test_an_unread_text_failure_preserves_every_other_population(
            self, tmp_path, mode, outcome):
        """Ungated, so every terminal reaches the renderer: the in-flight and
        reservation populations still reach the topic with their count when
        the unread-text read is absent or raising."""
        from tools import FinalizeResult

        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(
            broken="inbound_unread_texts" if mode == "raises" else (),
            absent="inbound_unread_texts" if mode == "absent" else (),
            in_flight=["flight"], message_reservations=1,
            reservation_texts=["held"])

        result = await self._finalize(rec, drv, outcome=outcome, gate=False)

        assert result is FinalizeResult.FINALIZED
        posted = self._posted(tch)
        assert posted.count("• flight") == 1, posted
        assert posted.count("• held") == 1, posted
        assert posted.count("up to 2 inbound message(s)") == 1, posted
        for name in ("inbound_in_flight_texts", "inbound_message_reservations",
                     "inbound_reservation_texts"):
            assert drv.calls.get(name) == 1, (name, drv.calls)

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

    # ---- the 6-accessor matrix: outcome, then post contents -------------
    #
    # `none` plus each accessor raising, over a driver with EVERY veto arm
    # armed and EVERY disclosure population non-empty. Only the
    # `inbound_unread_texts` rows are red at the pre-fix tree; the rest pin
    # the containment the other five guards already had, so a fix that
    # widened one of them would be caught here.

    _FULL = dict(depth=1, reservations=1,
                 message_reservations=1, texts=["q-text"],
                 in_flight=["f-text"], reservation_texts=["r-text"])

    # broken -> (bullets that must survive, the count copy the renderer emits)
    _MATRIX = {
        None: (("q-text", "f-text", "r-text"), "up to 3"),
        "inbound_unread_texts": (("f-text", "r-text"), "up to 2"),
        "inbound_unread_depth": (("q-text", "f-text", "r-text"), "up to 3"),
        "inbound_reservations": (("q-text", "f-text", "r-text"), "up to 3"),
        # #848: the text read succeeds while the count read fails, so the
        # reservation population is quoted and cannot be counted — the
        # renderer says so instead of quoting three bullets under an exact
        # count of two (the base's undercount).
        "inbound_message_reservations": (
            ("q-text", "f-text", "r-text"), "An unknown number of"),
        "inbound_reservation_texts": (("q-text", "f-text"), "up to 3"),
        "inbound_in_flight_texts": (("q-text", "r-text"), "up to 2"),
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
        for sentinel in ("q-text", "f-text", "r-text"):
            assert posted.count("• " + sentinel) == (
                1 if sentinel in expected else 0), (sentinel, posted)
        if count is not None:
            assert posted.count(f"{count} inbound message(s)") == 1, posted

    # ---- mutation checks (GREEN at the pre-fix tree, not red cases) ------

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


UNKNOWN_COUNT_SENTENCE = (
    "⚠️ An unknown number of inbound message(s) had no turn start "
    "recorded before this engagement ended — they may never have been "
    "read"
)

LAUNCH_DEATH_BASE_SENTENCE = (
    "⚠️ This engagement stopped before reporting a result "
    "(test detail). Its task may be incomplete — inspect any partial "
    "changes before retrying."
)


class TestAFailedCountReadStillDisclosesTheTextsItRead:
    """#848 RED CASES — the failed-count disclosure, on BOTH terminal
    renderers, each driven DIRECTLY with the six-accessor duck driver: `_finalize_engagement` on an ungated cancel, and
    `_report_launch_death` with a channel exposing the async
    `_post_engagement_notice` seam (never through `_abort_launch_on_cancel`,
    which another change owns).

    The state: the reservation-COUNT read raises while the reservation-TEXT
    read answers. At the base both renderers derive the paragraph's existence
    and its count from `lost_reservations` alone, which the count read's
    except arm sets to 0 — so with another population present the operator
    sees three bullets under an exact `2 inbound message(s)`, and with only a
    reservation text known no paragraph is posted at all and the text that
    WAS read is dropped silently. The fix posts the paragraph whenever the
    failed count sits beside a read text, and states no number in place of
    one it cannot evidence.

    Specified by terra (round `redcase-specify`); accepted by astra.
    Registry and channel wiring borrowed from the #807 class above, unchanged.
    """

    _setup = TestOneFailingAccessorCostsOnlyItsOwnInput._setup
    _finalize = staticmethod(TestOneFailingAccessorCostsOnlyItsOwnInput._finalize)
    _posted = staticmethod(TestOneFailingAccessorCostsOnlyItsOwnInput._posted)
    _FULL = TestOneFailingAccessorCostsOnlyItsOwnInput._FULL

    @staticmethod
    def _paragraph(posted: str) -> str:
        """The inbound paragraph the finalize tail posted, from its warning
        sign to the end of the posted text, so equality is on the WHOLE
        paragraph rather than on a substring a contradictory copy could
        also contain."""
        i = posted.find("⚠️")
        assert i >= 0, posted
        return posted[i:]

    @staticmethod
    def _finalize_text(tch) -> str:
        """The one text the ungated cancel posted into the topic."""
        calls = (list(tch.send_to_topic.call_args_list)
                 + list(tch.send_response_to_topic.call_args_list))
        assert len(calls) == 1, calls
        return next(a for a in calls[0].args if isinstance(a, str))

    @staticmethod
    def _launch_death_channel():
        from types import SimpleNamespace
        return SimpleNamespace(
            _post_engagement_notice=AsyncMock(), close_topic=AsyncMock())

    @staticmethod
    async def _launch_death(rec, drv, channel):
        from tools import _report_launch_death

        return await _report_launch_death(
            channel, rec, rec.topic_id, kind="test", detail="test detail",
            driver=drv)

    async def _render(self, renderer, rec, drv, tch) -> str:
        if renderer == "finalize":
            from tools import FinalizeResult
            result = await self._finalize(rec, drv, outcome="cancelled",
                                          gate=False)
            assert result is FinalizeResult.FINALIZED
            return self._posted(tch)
        from tools import LaunchDeathResult
        channel = self._launch_death_channel()
        result = await self._launch_death(rec, drv, channel)
        assert result is LaunchDeathResult.REPORTED
        assert channel._post_engagement_notice.await_count == 1
        (_rec, notice), _kw = channel._post_engagement_notice.await_args
        return notice

    # ---- the four red arms ------------------------------------------

    async def test_finalize_names_no_count_over_the_three_bullets_it_quotes(
            self, tmp_path):
        """ARM 1. Red at the base: three bullets under an exact
        `2 inbound message(s)` — more bullets than the count above them."""
        from tools import FinalizeResult

        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(broken="inbound_message_reservations",
                             **self._FULL)

        result = await self._finalize(rec, drv, outcome="cancelled", gate=False)

        assert result is FinalizeResult.FINALIZED
        assert drv.calls["inbound_reservation_texts"] == 1
        posted = self._finalize_text(tch)
        assert self._paragraph(posted) == (
            UNKNOWN_COUNT_SENTENCE
            + ":\n• f-text\n• q-text\n• r-text")
        assert posted.count(UNKNOWN_COUNT_SENTENCE) == 1, posted
        for sentinel in ("f-text", "q-text", "r-text"):
            assert posted.count("• " + sentinel) == 1, (sentinel, posted)
        assert "2 inbound message(s)" not in posted, posted
        assert "up to" not in posted, posted

    async def test_finalize_quotes_a_reservation_text_that_was_all_it_could_read(
            self, tmp_path):
        """ARM 2. Red at the base: the renderer's predicate is false with no
        other population and a zeroed count, so NO `inbound message(s)`
        paragraph is posted and the read text is dropped silently."""
        from tools import FinalizeResult

        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(broken="inbound_message_reservations",
                             message_reservations=1,
                             reservation_texts=["r-text"])

        result = await self._finalize(rec, drv, outcome="cancelled", gate=False)

        assert result is FinalizeResult.FINALIZED
        assert drv.calls["inbound_reservation_texts"] == 1
        posted = self._finalize_text(tch)
        assert posted.count("inbound message(s)") == 1, posted
        assert self._paragraph(posted) == (
            UNKNOWN_COUNT_SENTENCE + ":\n• r-text")
        assert posted.count(UNKNOWN_COUNT_SENTENCE) == 1, posted
        assert posted.count("• r-text") == 1, posted

    async def test_launch_death_names_no_count_over_the_three_bullets_it_quotes(
            self, tmp_path):
        """ARM 3. Red at the base: the sole notice says
        `2 inbound message(s)` over three bullets."""
        from tools import LaunchDeathResult

        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(broken="inbound_message_reservations",
                             **self._FULL)
        channel = self._launch_death_channel()

        result = await self._launch_death(rec, drv, channel)

        assert result is LaunchDeathResult.REPORTED
        assert drv.calls["inbound_reservation_texts"] == 1
        assert channel._post_engagement_notice.await_count == 1
        (_rec, notice), _kw = channel._post_engagement_notice.await_args
        assert notice == (
            LAUNCH_DEATH_BASE_SENTENCE + "\n\n" + UNKNOWN_COUNT_SENTENCE
            + ":\n• f-text\n• q-text\n• r-text"), notice

    async def test_launch_death_quotes_a_reservation_text_that_was_all_it_could_read(
            self, tmp_path):
        """ARM 4. Red at the base: one notice, no inbound paragraph — the
        text that was read is dropped silently."""
        from tools import LaunchDeathResult

        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(broken="inbound_message_reservations",
                             message_reservations=1,
                             reservation_texts=["r-text"])
        channel = self._launch_death_channel()

        result = await self._launch_death(rec, drv, channel)

        assert result is LaunchDeathResult.REPORTED
        assert drv.calls["inbound_reservation_texts"] == 1
        assert channel._post_engagement_notice.await_count == 1
        (_rec, notice), _kw = channel._post_engagement_notice.await_args
        assert notice == (
            LAUNCH_DEATH_BASE_SENTENCE + "\n\n" + UNKNOWN_COUNT_SENTENCE
            + ":\n• r-text"), notice

    # ---- the alias witness: why the copy names no number -----------

    @pytest.mark.parametrize("renderer", ["finalize", "launch_death"])
    async def test_an_alias_beside_its_envelope_is_quoted_twice_under_no_count(
            self, tmp_path, renderer):
        """A reservation text can repeat an unread text of the same message
        (the renderer does not de-duplicate across populations). With the
        count read also failed, a lower-bound copy would claim two messages
        for one; the copy names no number. Red at the base on the copy
        (`1 inbound message(s)`)."""
        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(broken="inbound_message_reservations",
                             message_reservations=1, texts=["hello"],
                             reservation_texts=["hello"])

        posted = await self._render(renderer, rec, drv, tch)

        assert posted.count("• hello") == 2, posted
        assert posted.count(UNKNOWN_COUNT_SENTENCE) == 1, posted
        assert "at least" not in posted, posted
        assert "1 inbound message(s)" not in posted, posted

    # ---- mutation controls (GREEN at the base, not red cases) ---------

    @pytest.mark.parametrize("renderer", ["finalize", "launch_death"])
    @pytest.mark.parametrize("count", [
        ("absent", None), ("non-int", "1"), ("zero", 0)])
    async def test_a_count_that_did_not_raise_beside_a_text_alone_keeps_the_base_output(
            self, tmp_path, renderer, count):
        """A successful zero, an absent accessor and a non-int answer are not
        failures: the flag stays down and a reservation text beside them is
        the off-contract state (texts are clamped to the count) the renderer
        does not re-clamp — no paragraph, exactly as at the base. Kills a
        flag raised on the wrong fallback, and a predicate that reads the
        texts without the flag."""
        mode, value = count
        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(
            absent="inbound_message_reservations" if mode == "absent" else (),
            message_reservations=0 if value is None else value,
            reservation_texts=["r"])

        posted = await self._render(renderer, rec, drv, tch)

        assert "inbound message(s)" not in posted, posted
        assert "An unknown number" not in posted, posted

    @pytest.mark.parametrize("renderer", ["finalize", "launch_death"])
    async def test_a_successful_zero_beside_a_spool_text_and_a_reservation_text_keeps_the_base_copy(
            self, tmp_path, renderer):
        """The off-contract state with an unread population present: pinned as
        UNCHANGED, not endorsed — the base's exact `1` over two bullets, no
        unknown-number copy. Kills deriving "the read failed" from
        `lost_reservations == 0 and reservation_texts`."""
        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(message_reservations=0, texts=["q"],
                             reservation_texts=["r"])

        posted = await self._render(renderer, rec, drv, tch)

        assert posted.count("1 inbound message(s)") == 1, posted
        assert posted.count("• q") == 1, posted
        assert posted.count("• r") == 1, posted
        assert "An unknown number" not in posted, posted

    async def test_launch_death_with_a_failed_count_and_nothing_else_read_posts_the_base_notice(
            self, tmp_path):
        """The launch-death twin of the fail-open case: a raising count read
        with NOTHING else read is not evidence, so the notice is the base
        sentence alone, byte for byte — no `0 inbound message(s)` paragraph.
        Kills a launch-death predicate that fires on the flag alone (the
        finalize side is killed by the fail-open case's new arm)."""
        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(broken="inbound_message_reservations")

        notice = await self._render("launch_death", rec, drv, tch)

        assert notice == LAUNCH_DEATH_BASE_SENTENCE, notice

    @pytest.mark.parametrize("renderer", ["finalize", "launch_death"])
    async def test_a_failed_count_beside_spool_text_only_keeps_the_exact_count(
            self, tmp_path, renderer):
        """The failed count with NO reservation text: the base's exact
        numeric paragraph, byte for byte — the new arm needs the text as
        well as the flag, and the count copy is untouched without one."""
        reg, rec, tch = await self._setup(tmp_path)
        drv = _AccessorProbe(broken="inbound_message_reservations",
                             texts=["q"])

        posted = await self._render(renderer, rec, drv, tch)

        assert posted.count("1 inbound message(s)") == 1, posted
        assert posted.count("• q") == 1, posted
        assert "An unknown number" not in posted, posted
        assert "up to" not in posted, posted
