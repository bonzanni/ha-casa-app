"""Task 7 / v0.83.0 §A3 — the answered-RESERVATION token lifecycle +
enqueue-time answer promotion (F-ORDER linearization).

Free-text anchor questions are answered by the operator's next Telegram
message. The answer-lifecycle decision is LINEARIZED with the answer's visible
arrival (Sol r6-2 + r7-1): the Telegram handler sets a per-message reservation
TOKEN in the same synchronous section as the topic high-water advance, so a
concurrent ``result``-finalize that observed the answer's high-water also
observes the reservation (and does not re-post the question below its answer).
The reservation is PROMOTED (unconditionally) at durable enqueue and CAS-rolled-
back on every non-delivery path.

REAL registry over a tmp tombstone + REAL ``_InboundSpool`` + REAL driver
methods + the REAL Telegram handler (fake bot / fake update). Injected clocks;
never patches ``<module>.asyncio.sleep`` (the shared attribute — the memory-cage
rule)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


# ---------------------------------------------------------------------------
# fakes / helpers
# ---------------------------------------------------------------------------


class _FakeWriter:
    def __init__(self, result: bool = True):
        self.result = result
        self.calls: list[str] = []

    async def __call__(self, text: str) -> bool:
        self.calls.append(text)
        return self.result


class _RecordNotice:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[tuple] = []

    async def __call__(self, text, reply_to):
        self.calls.append((text, reply_to))
        return self.ok


class _FakeSequencer:
    def __init__(self):
        self.reply_targets: list = []

    def set_turn_reply_to(self, message_id):
        self.reply_targets.append(message_id)


async def _make_registry(tmp_path: Path):
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        "executor", "configurator", "claude_code", "t",
        {"user_id": 77}, topic_id=555)
    return reg, rec


async def _add_anchor(reg, rec, *, mid, text="Q1: DB name?"):
    n = await reg.allocate_question_number(rec.id)
    await reg.add_open_question(rec.id, n, mid, text=text, kind="anchor")
    return n


def _make_driver(tmp_path: Path, reg, *, edit_topic_message=None):
    from drivers.claude_code_driver import ClaudeCodeDriver

    return ClaudeCodeDriver(
        engagements_root=str(tmp_path / "engagements"),
        send_to_topic=AsyncMock(),
        casa_framework_mcp_url="http://x",
        edit_topic_message=edit_topic_message,
        registry=reg,
    )


def _wire_spool(drv, rec, tmp_path, *, writer_ok=True, sequencer=None,
                spool_path=None):
    """Attach a REAL ``_InboundSpool`` wired to the driver's promotion +
    delivery-settle hooks (exactly as ``start()`` wires it in production)."""
    from drivers.claude_code_driver import _InboundSpool

    seq = sequencer if sequencer is not None else _FakeSequencer()
    spool = _InboundSpool(
        engagement_id=rec.id,
        spool_path=spool_path or str(tmp_path / f"{rec.id}.spool.jsonl"),
        write_fifo=_FakeWriter(writer_ok),
        send_notice=_RecordNotice(),
        is_turn_running=lambda: False,
        current_epoch=lambda: 1,
        sequencer=seq,
        registry=drv._registry,
        settle_anchor_on_delivery=lambda op: drv._settle_open_anchor(rec, op),
        promote_answer_on_enqueue=lambda: drv._promote_answer_on_enqueue(rec),
    )
    drv._inbound[rec.id] = spool
    return spool, seq


# ===========================================================================
# 1. reservation invisibility + rollback restore (effective/union view)
# ===========================================================================


class TestReservationVisibility:
    async def test_reserve_hides_question_rollback_restores(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=8001)
        drv = _make_driver(tmp_path, reg)

        assert drv._effective_open_question_numbers(rec.id) == [n]

        token = drv.reserve_answer(rec.id)
        assert token is not None
        # Reserved-but-unpromoted counts as ANSWERED for gates/summary/re-anchor.
        assert drv._effective_open_question_numbers(rec.id) == []
        assert drv._reserved_question_number(rec.id) == n

        assert await drv.rollback_answer_reservation(rec.id, token) is True
        assert drv._effective_open_question_numbers(rec.id) == [n]
        assert drv._reserved_question_number(rec.id) is None

    async def test_reserve_hides_question_from_real_summary(self, tmp_path):
        from channels.output_sequencer import OutputSequencer
        from drivers.summary_controller import (
            SummaryController, STATUS_WORKING,
        )

        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=8001)
        drv = _make_driver(tmp_path, reg)
        eid = rec.id

        edits: list[tuple[int, str]] = []

        async def _send(topic_id, text):
            return 500

        async def _edit(topic_id, mid, text):
            edits.append((mid, text))
            return True

        async def _park(_dt):
            import asyncio
            await asyncio.Event().wait()

        seq = OutputSequencer(
            engagement_id=eid, topic_id=555,
            send_message=_send, edit_message=_edit)
        ctrl = SummaryController(
            engagement_id=eid, sequencer=seq, goal_line="goal",
            open_question_numbers=lambda:
                drv._effective_open_question_numbers(eid),
            message_id=500, _sleep=_park,
        )
        drv._summaries[eid] = ctrl
        drv._turn_running[eid] = True

        await drv.recompute_engagement_status(eid)
        assert f"Open questions: Q{n}" in edits[-1][1]

        # Reservation lands → the summary drops the open-question line + returns
        # to ⚙️ working (not stuck ⏳ on a question whose answer is arriving).
        drv.reserve_answer(eid)
        await drv.recompute_engagement_status(eid)
        assert "Open questions" not in edits[-1][1]
        assert edits[-1][1].splitlines()[0] == STATUS_WORKING
        ctrl.shutdown()


# ===========================================================================
# 2. reserve semantics — no anchor → None; single-anchor double-reserve
# ===========================================================================


class TestReserveSemantics:
    async def test_no_open_anchor_returns_none(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        drv = _make_driver(tmp_path, reg)
        assert drv.reserve_answer(rec.id) is None
        assert drv._answer_reservations.get(rec.id) is None

    async def test_second_reserve_finds_no_unreserved_anchor(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=8001)
        drv = _make_driver(tmp_path, reg)

        t1 = drv.reserve_answer(rec.id)
        assert t1 is not None
        # The only anchor is already reserved → nothing left to reserve.
        assert drv.reserve_answer(rec.id) is None
        # The first reservation is NOT clobbered.
        assert drv._answer_reservations[rec.id][0] == t1

    async def test_answered_anchor_is_not_reserved(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=8001)
        drv = _make_driver(tmp_path, reg)
        await reg.mark_question_answered(rec.id, n)
        assert drv.reserve_answer(rec.id) is None


# ===========================================================================
# 3. CAS rollback semantics + Task-9 seam
# ===========================================================================


class TestRollbackCAS:
    async def test_wrong_token_is_noop_current_not_clobbered(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=8001)
        drv = _make_driver(tmp_path, reg)

        t1 = drv.reserve_answer(rec.id)
        assert await drv.rollback_answer_reservation(rec.id, "wrong") is False
        assert drv._answer_reservations.get(rec.id) is not None
        assert await drv.rollback_answer_reservation(rec.id, t1) is True
        assert drv._answer_reservations.get(rec.id) is None

    async def test_none_token_is_noop(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        drv = _make_driver(tmp_path, reg)
        assert await drv.rollback_answer_reservation(rec.id, None) is False

    async def test_successful_rollback_calls_seam_failed_does_not(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=8001)
        drv = _make_driver(tmp_path, reg)

        called: list[str] = []

        async def _spy(eid):
            called.append(eid)

        drv._on_reservation_rolled_back = _spy

        t1 = drv.reserve_answer(rec.id)
        await drv.rollback_answer_reservation(rec.id, t1)
        assert called == [rec.id]

        # A failed CAS (stale token) must NOT fire the seam.
        called.clear()
        await drv.rollback_answer_reservation(rec.id, "wrong")
        assert called == []

    async def test_default_seam_is_noop(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        drv = _make_driver(tmp_path, reg)
        assert await drv._on_reservation_rolled_back(rec.id) is None


# ===========================================================================
# 4. promotion at durable enqueue — unconditional, consumes reservation
# ===========================================================================


class TestPromotion:
    async def test_enqueue_promotes_settles_and_records_mid(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=8001)

        edits: list[tuple[int, str]] = []

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            edits.append((mid, text))
            return True

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)
        spool, seq = _wire_spool(drv, rec, tmp_path)

        token = drv.reserve_answer(rec.id)
        assert token is not None

        # FIFO busy → the reader is unarmed (no on_spawn), so enqueue does NOT
        # deliver; promotion still runs at durable enqueue.
        disp = await drv.send_user_turn(rec, "it is prod-db", tg_message_id=42)
        assert disp == "queued"

        # Promotion consumed the reservation + marked the anchor answered.
        assert drv._answer_reservations.get(rec.id) is None
        # Settle ran EXACTLY once (confirmed → entry removed).
        assert len(edits) == 1
        assert edits[0][0] == 8001 and "✅ answered below" in edits[0][1]
        assert reg.open_question_entries(rec.id) == []
        assert drv._effective_open_question_numbers(rec.id) == []

        # The envelope carries the anchor mid.
        env = spool._envelopes[-1]
        assert env.answer_anchor_mid == 8001

        # Delivery LATER only THREADS — no second settle edit.
        await spool.on_spawn()
        assert len(edits) == 1                    # unchanged
        assert seq.reply_targets[-1] == 8001

    async def test_promotion_is_unconditional_without_reservation(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=8001)

        edits: list = []

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            edits.append((mid, text))
            return True

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)
        _wire_spool(drv, rec, tmp_path)

        # No reserve_answer() at all — promotion still fires (any delivered
        # operator message answers the one open question).
        disp = await drv.send_user_turn(rec, "answer", tg_message_id=42)
        assert disp == "queued"
        assert reg.open_question_entries(rec.id) == []

    async def test_promotion_persist_failure_sets_overlay_settle_still_runs(
        self, tmp_path,
    ):
        reg, rec = await _make_registry(tmp_path)
        n = await _add_anchor(reg, rec, mid=8001)

        edits: list = []

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            edits.append((mid, text))
            return True

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)
        _wire_spool(drv, rec, tmp_path)

        # The strict answered-persist raises → overlay covers the live process
        # (Task-6 §A3 answered-persist-failure policy).
        reg.mark_question_answered = AsyncMock(side_effect=OSError("disk full"))

        disp = await drv.send_user_turn(rec, "answer", tg_message_id=42)
        assert disp == "queued"
        assert n in drv._answered_overlay.get(rec.id, set())
        # The visual settle STILL ran despite the failed durable write.
        assert edits and "✅ answered below" in edits[-1][1]
        # Effective view treats it answered immediately (overlay ∪ persisted).
        assert drv._effective_open_question_numbers(rec.id) == []

    async def test_post_promotion_rollback_cas_fails(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=8001)

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            return True

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)
        _wire_spool(drv, rec, tmp_path)

        token = drv.reserve_answer(rec.id)
        await drv.send_user_turn(rec, "answer", tg_message_id=42)
        # Promotion consumed the reservation → a late rollback CAS fails.
        assert await drv.rollback_answer_reservation(rec.id, token) is False


# ===========================================================================
# 5. A-rejected / B-success interleaving → stays answered
# ===========================================================================


class TestInterleaving:
    async def test_a_rejected_b_success_stays_answered(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=8001)

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            return True

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)
        _wire_spool(drv, rec, tmp_path)

        # A reserves.
        token_a = drv.reserve_answer(rec.id)
        assert token_a is not None

        # B enqueues successfully → promotion (unconditional) consumes A's
        # reservation and answers the question.
        disp_b = await drv.send_user_turn(rec, "B answers", tg_message_id=50)
        assert disp_b == "queued"
        assert drv._answer_reservations.get(rec.id) is None

        # A's enqueue then fails → A rolls back with token_a. The CAS FAILS
        # (reservation already promoted) → the question STAYS answered.
        assert await drv.rollback_answer_reservation(rec.id, token_a) is False
        assert drv._effective_open_question_numbers(rec.id) == []
        assert reg.open_question_entries(rec.id) == []


# ===========================================================================
# 6. disposition propagation through send_user_turn (whole-chain contract)
# ===========================================================================


class TestDisposition:
    async def test_send_user_turn_returns_queued(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        drv = _make_driver(tmp_path, reg)
        _wire_spool(drv, rec, tmp_path)
        assert await drv.send_user_turn(rec, "hi", tg_message_id=1) == "queued"

    async def test_send_user_turn_returns_dropped_full(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        drv = _make_driver(tmp_path, reg)
        spool, _ = _wire_spool(drv, rec, tmp_path)
        # Force the ordinary lane to appear full.
        spool._ordinary_count = lambda: 999
        disp = await drv.send_user_turn(rec, "hi", tg_message_id=1)
        assert disp == "dropped_full"

    async def test_send_user_turn_returns_error_on_persist_failure(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        drv = _make_driver(tmp_path, reg)
        spool, _ = _wire_spool(drv, rec, tmp_path)

        def _boom():
            raise OSError("disk full")

        spool._persist = _boom
        disp = await drv.send_user_turn(rec, "hi", tg_message_id=1)
        assert disp == "error"

    async def test_rejection_does_not_promote(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=8001)
        drv = _make_driver(tmp_path, reg, edit_topic_message=AsyncMock(
            return_value=True))
        spool, _ = _wire_spool(drv, rec, tmp_path)
        spool._ordinary_count = lambda: 999   # capacity full → dropped_full

        drv.reserve_answer(rec.id)
        disp = await drv.send_user_turn(rec, "answer", tg_message_id=1)
        assert disp == "dropped_full"
        # A rejected enqueue never durably spooled → NO promotion; the question
        # is still open (the reservation is rolled back by the caller/handler).
        assert reg.open_question_entries(rec.id)[0].get("answered") is False


# ===========================================================================
# 7. legacy envelope (no answer_anchor_mid) → delivery-time settle fallback
# ===========================================================================


class TestLegacyEnvelope:
    async def test_legacy_envelope_uses_delivery_time_settle(self, tmp_path):
        reg, rec = await _make_registry(tmp_path)
        await _add_anchor(reg, rec, mid=8001)

        edits: list = []

        async def _edit(topic_id, mid, text, *, clear_keyboard=False):
            edits.append((mid, text))
            return True

        drv = _make_driver(tmp_path, reg, edit_topic_message=_edit)

        # A legacy spool line: no ``answer_anchor_mid`` key (pre-Task-7).
        spool_path = tmp_path / f"{rec.id}.spool.jsonl"
        legacy = json.dumps({
            "text": "the answer", "tg_message_id": 42, "priority": False,
            "receipt": "not_required", "notice": "none", "notice_text": None,
            "enqueued_at": 1.0, "delivery_epoch": None, "state": "queued",
            "seq": 0, "is_initial": False,
        })
        spool_path.write_text(legacy + "\n", encoding="utf-8")

        spool, seq = _wire_spool(
            drv, rec, tmp_path, spool_path=str(spool_path))
        env = spool._envelopes[-1]
        assert env.answer_anchor_mid is None    # legacy → field absent

        # Delivery falls back to the delivery-time settle path.
        await spool.on_spawn()
        assert len(edits) == 1 and "✅ answered below" in edits[0][1]
        assert seq.reply_targets[-1] == 8001


# ===========================================================================
# 8. envelope persistence round-trips the new field
# ===========================================================================


class TestEnvelopePersistence:
    async def test_answer_anchor_mid_round_trips(self):
        from drivers.claude_code_driver import _Envelope

        env = _Envelope(
            text="x", tg_message_id=1, priority=False, receipt="not_required",
            notice="none", enqueued_at=1.0, delivery_epoch=None,
            state="queued", seq=0, answer_anchor_mid=8001)
        back = _Envelope.from_line(env.to_line())
        assert back.answer_anchor_mid == 8001

    async def test_legacy_line_defaults_none(self):
        from drivers.claude_code_driver import _Envelope

        legacy = json.dumps({
            "text": "x", "tg_message_id": 1, "priority": False,
            "receipt": "not_required", "notice": "none", "enqueued_at": 1.0,
            "delivery_epoch": None, "state": "queued", "seq": 0,
        })
        back = _Envelope.from_line(legacy)
        assert back.answer_anchor_mid is None


# ===========================================================================
# 9. REAL Telegram handler — command paths roll back; delivery hands off
# ===========================================================================


def _mk_update(*, chat_id, text, thread_id=None, user_id=77):
    u = MagicMock()
    u.message = MagicMock()
    u.message.chat = MagicMock()
    u.message.chat.id = chat_id
    u.message.text = text
    u.message.message_thread_id = thread_id
    u.message.from_user = MagicMock(id=user_id)
    u.message.message_id = 999
    return u


async def _drain_turns(ch) -> None:
    import asyncio
    tasks = list(getattr(ch, "_turn_tasks", ()) or ())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _handler_ctx(tmp_path, fake_telegram_bot):
    """A channel wired to a REAL driver + registry with one open anchor."""
    from channels.telegram import TelegramChannel

    reg, rec = await _make_registry(tmp_path)
    n = await _add_anchor(reg, rec, mid=8001)
    drv = _make_driver(tmp_path, reg, edit_topic_message=AsyncMock(
        return_value=True))

    ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                         engagement_supergroup_id=-1001)
    ch._engagement_registry = reg
    ch._driver_advance_high_water = AsyncMock()
    ch._driver_reserve_answer = lambda r: drv.reserve_answer(r.id)

    async def _rb(r, tok, *, suppress_reanchor=False):
        # Mirrors casa_core's real _driver_rollback_answer_reservation seam,
        # which forwards F2's suppress_reanchor to the driver.
        return await drv.rollback_answer_reservation(
            r.id, tok, suppress_reanchor=suppress_reanchor)

    ch._driver_rollback_answer_reservation = _rb
    ch._post_engagement_notice = AsyncMock()
    return ch, reg, rec, drv, n


class TestHandlerRollback:
    async def test_silent_rolls_back_not_answered(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, n = await _handler_ctx(tmp_path, fake_telegram_bot)
        ch._observer = MagicMock()

        u = _mk_update(chat_id=-1001, text="/silent", thread_id=555, user_id=77)
        await ch.handle_update(u)

        assert drv._answer_reservations.get(rec.id) is None      # rolled back
        assert reg.open_question_entries(rec.id)[0].get("answered") is False
        assert drv._effective_open_question_numbers(rec.id) == [n]

    async def test_cancel_originator_rolls_back(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, n = await _handler_ctx(tmp_path, fake_telegram_bot)
        ch._finalize_cancel = AsyncMock()

        u = _mk_update(chat_id=-1001, text="/cancel", thread_id=555, user_id=77)
        await ch.handle_update(u)

        ch._finalize_cancel.assert_awaited_once()
        assert drv._answer_reservations.get(rec.id) is None
        assert reg.open_question_entries(rec.id)[0].get("answered") is False

    async def test_complete_originator_rolls_back(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, n = await _handler_ctx(tmp_path, fake_telegram_bot)
        ch._finalize_complete_user = AsyncMock()

        u = _mk_update(
            chat_id=-1001, text="/complete", thread_id=555, user_id=77)
        await ch.handle_update(u)

        ch._finalize_complete_user.assert_awaited_once()
        assert drv._answer_reservations.get(rec.id) is None

    async def test_rejected_originator_command_rolls_back(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, n = await _handler_ctx(tmp_path, fake_telegram_bot)
        ch._finalize_cancel = AsyncMock()

        # A NON-originator /cancel (rec origin user_id=77, update from 999).
        u = _mk_update(chat_id=-1001, text="/cancel", thread_id=555, user_id=999)
        await ch.handle_update(u)

        ch._finalize_cancel.assert_not_awaited()          # refused
        ch._post_engagement_notice.assert_awaited()       # refusal notice
        assert drv._answer_reservations.get(rec.id) is None
        assert reg.open_question_entries(rec.id)[0].get("answered") is False


# ===========================================================================
# 9b. M5 — the FOURTH-consumer acceptance matrix through a REAL OutputSequencer
#     (ordered fake wire). For each non-delivery cause the anchor must end LAST
#     (re-anchored copy is the final wire post); terminal-finalize SETTLES.
# ===========================================================================


class _OrderedWire:
    """One ordered fake wire backing BOTH the text send (notices) and the markup
    send (re-anchor ``post_discrete``), so wire order == a single monotonic mid
    sequence (mirrors ``test_anchor_reanchor._Wire``)."""

    def __init__(self, *, start: int = 1000):
        self._n = start
        self.posts: list[tuple[str, int, str]] = []
        self.edits: list[tuple[str, int, str]] = []

    def _mid(self) -> int:
        self._n += 1
        return self._n

    async def send_text(self, topic, text, **kw) -> int:
        mid = self._mid()
        self.posts.append(("text", mid, text))
        return mid

    async def send_markup(self, topic, text, markup, reply_to=None):
        mid = self._mid()
        self.posts.append(("markup", mid, text))
        return mid

    async def edit_text(self, topic, mid, text, clear_keyboard=False) -> bool:
        self.edits.append(("text", mid, text))
        return True

    async def edit_markup(self, topic, mid, text, markup) -> bool:
        self.edits.append(("markup", mid, text))
        return True


def _make_driver_seq(tmp_path, reg, wire):
    from drivers.claude_code_driver import ClaudeCodeDriver

    return ClaudeCodeDriver(
        engagements_root=str(tmp_path / "engagements"),
        send_to_topic=wire.send_text,
        casa_framework_mcp_url="http://x",
        edit_topic_message=wire.edit_text,
        send_topic_message_markup=wire.send_markup,
        edit_topic_message_markup=wire.edit_markup,
        registry=reg)


def _entry(reg, rec, n):
    for q in reg.open_question_entries(rec.id):
        if q.get("n") == n:
            return q
    return None


async def _handler_ctx_seq(tmp_path, fake_telegram_bot):
    """A channel wired to a REAL driver + REAL OutputSequencer over an ordered
    wire, with one open anchor at a LOW mid and content already posted below it
    (high-water > anchor) so a rollback consumer's re-anchor genuinely fires.
    Platform notices route THROUGH the sequencer onto the same ordered wire."""
    from channels.telegram import TelegramChannel

    reg, rec = await _make_registry(tmp_path)
    n = await _add_anchor(reg, rec, mid=500)     # LOW mid → notices land below
    wire = _OrderedWire()
    drv = _make_driver_seq(tmp_path, reg, wire)
    seq = drv._ensure_sequencer(rec)
    seq._high_water = 600                          # content posted below the anchor

    ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                         engagement_supergroup_id=-1001)
    ch._engagement_registry = reg
    ch._observer = MagicMock()
    ch._driver_advance_high_water = AsyncMock()
    ch._driver_reserve_answer = lambda r: drv.reserve_answer(r.id)

    async def _rb(r, tok, *, suppress_reanchor=False):
        # Mirrors casa_core's real _driver_rollback_answer_reservation seam,
        # which forwards F2's suppress_reanchor to the driver.
        return await drv.rollback_answer_reservation(
            r.id, tok, suppress_reanchor=suppress_reanchor)

    ch._driver_rollback_answer_reservation = _rb
    # Route platform notices through the REAL sequencer onto the shared wire.
    ch._driver_post_notice = lambda r, text: seq.post_platform_notice(text)
    return ch, reg, rec, drv, seq, wire, n


class TestFourthConsumerReanchorMatrix:
    async def test_silent_command_reanchors_anchor_last(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, seq, wire, n = await _handler_ctx_seq(
            tmp_path, fake_telegram_bot)
        u = _mk_update(chat_id=-1001, text="/silent", thread_id=555, user_id=77)
        await ch.handle_update(u)      # rollback + re-anchor are synchronous here

        # The command notice (text) posted BELOW the anchor, then the re-anchored
        # copy (markup) is the FINAL wire message.
        assert [k for k, _, _ in wire.posts] == ["text", "markup"]
        reanchor_mid = wire.posts[-1][1]
        assert seq.high_water == reanchor_mid
        assert _entry(reg, rec, n)["tg_message_id"] == reanchor_mid
        # A command is NOT an answer.
        assert _entry(reg, rec, n).get("answered") is False
        assert drv._answer_reservations.get(rec.id) is None

    async def test_rejected_originator_command_reanchors_anchor_last(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, seq, wire, n = await _handler_ctx_seq(
            tmp_path, fake_telegram_bot)
        ch._finalize_cancel = AsyncMock()
        # A NON-originator /cancel (rec origin user_id=77, update from 999) →
        # refused + refusal notice + reservation rolled back.
        u = _mk_update(chat_id=-1001, text="/cancel", thread_id=555, user_id=999)
        await ch.handle_update(u)

        ch._finalize_cancel.assert_not_awaited()
        assert wire.posts and wire.posts[-1][0] == "markup"
        reanchor_mid = wire.posts[-1][1]
        assert _entry(reg, rec, n)["tg_message_id"] == reanchor_mid
        assert _entry(reg, rec, n).get("answered") is False

    async def test_dropped_full_delivery_reanchors_anchor_last(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, seq, wire, n = await _handler_ctx_seq(
            tmp_path, fake_telegram_bot)
        ch._driver_send_user_turn = AsyncMock(return_value="dropped_full")
        u = _mk_update(chat_id=-1001, text="answer", thread_id=555, user_id=77)
        await ch.handle_update(u)
        await _drain_turns(ch)

        assert wire.posts and wire.posts[-1][0] == "markup"
        reanchor_mid = wire.posts[-1][1]
        assert seq.high_water == reanchor_mid
        assert _entry(reg, rec, n)["tg_message_id"] == reanchor_mid
        assert _entry(reg, rec, n).get("answered") is False
        assert drv._answer_reservations.get(rec.id) is None

    async def test_persistence_error_delivery_reanchors_anchor_last(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, seq, wire, n = await _handler_ctx_seq(
            tmp_path, fake_telegram_bot)
        ch._driver_send_user_turn = AsyncMock(side_effect=RuntimeError("boom"))
        u = _mk_update(chat_id=-1001, text="answer", thread_id=555, user_id=77)
        await ch.handle_update(u)
        await _drain_turns(ch)

        # Sol r12-1 (fixed in telegram.py): ``_deliver_turn_bg`` posts the
        # "Turn failed" notice FIRST and only then rolls back — so the rollback
        # consumer's re-posted question is the strictly LAST wire message,
        # below the notice.
        reanchor = [(k, mid) for k, mid, _ in wire.posts if k == "markup"]
        assert reanchor, "the rollback consumer did not re-anchor"
        reanchor_mid = reanchor[-1][1]
        assert any(k == "text" and "Turn failed" in t for k, _, t in wire.posts)
        assert wire.posts[-1][0] == "markup", (
            "the re-anchored question must be the LAST wire message "
            f"(got trailing {wire.posts[-1]!r})"
        )
        assert wire.posts[-1][1] == reanchor_mid
        assert _entry(reg, rec, n)["tg_message_id"] == reanchor_mid
        assert _entry(reg, rec, n).get("answered") is False

    async def test_handler_cancellation_reanchors_anchor_last(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, seq, wire, n = await _handler_ctx_seq(
            tmp_path, fake_telegram_bot)
        started = asyncio.Event()

        async def _blocking(rec_, text, *, tg_message_id=None):
            started.set()
            await asyncio.Event().wait()   # block until cancelled

        ch._driver_send_user_turn = _blocking
        u = _mk_update(chat_id=-1001, text="answer", thread_id=555, user_id=77)
        await ch.handle_update(u)
        await asyncio.wait_for(started.wait(), timeout=1.0)
        for t in list(ch._turn_tasks):
            t.cancel()
        await _drain_turns(ch)

        # Cancellation → rollback → re-anchor. The anchor ends LAST.
        assert wire.posts and wire.posts[-1][0] == "markup"
        reanchor_mid = wire.posts[-1][1]
        assert _entry(reg, rec, n)["tg_message_id"] == reanchor_mid
        assert drv._answer_reservations.get(rec.id) is None

    async def test_terminal_finalize_settles_not_reanchors(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, seq, wire, n = await _handler_ctx_seq(
            tmp_path, fake_telegram_bot)
        # The terminal-finalize consumer SETTLES open anchors rather than
        # re-anchoring (closing the /cancel-with-live-anchor gap).
        await drv.settle_all_open_questions(rec, "cancelled")

        assert not any(k == "markup" for k, _, _ in wire.posts)   # no re-anchor
        assert any(mid == 500 and "engagement ended" in text
                   for _, mid, text in wire.edits)
        assert reg.open_question_entries(rec.id) == []

    async def test_terminal_finalize_failure_reanchors_anchor_last(
        self, tmp_path, fake_telegram_bot,
    ):
        """F2 (whole-branch r2): a /cancel whose STRICT terminal transition
        FAILS (finalize returns False → engagement stays live, nothing settled)
        must NOT strand the live anchor above the command. The suppression is
        gated on finalize success, so the rollback runs WITHOUT suppression and
        the fourth-consumer re-anchor moves the anchor below the command."""
        ch, reg, rec, drv, seq, wire, n = await _handler_ctx_seq(
            tmp_path, fake_telegram_bot)

        async def _failing_finalize(_rec, reason="user"):
            # Strict terminal transition lost / rolled back → engagement live.
            return False

        ch._finalize_cancel = _failing_finalize
        u = _mk_update(chat_id=-1001, text="/cancel", thread_id=555, user_id=77)
        await ch.handle_update(u)

        # Re-anchored (NOT suppressed) → the markup copy is the LAST wire post.
        assert wire.posts and wire.posts[-1][0] == "markup"
        reanchor_mid = wire.posts[-1][1]
        assert seq.high_water == reanchor_mid
        assert _entry(reg, rec, n)["tg_message_id"] == reanchor_mid
        assert _entry(reg, rec, n).get("answered") is False
        assert drv._answer_reservations.get(rec.id) is None

    async def test_terminal_finalize_success_settles_no_reanchor(
        self, tmp_path, fake_telegram_bot,
    ):
        """F2: a /cancel whose finalize SUCCEEDS lets its own
        ``settle_all_open_questions`` own the open anchor (settled once); the
        terminal rollback is then suppressed so no redundant re-anchor copy is
        posted. Finalize runs FIRST — a still-held reservation is inert to
        settle, which never reads the reservation state."""
        ch, reg, rec, drv, seq, wire, n = await _handler_ctx_seq(
            tmp_path, fake_telegram_bot)

        async def _ok_finalize(_rec, reason="user"):
            # Mirror production: a winning finalize settles the open anchors
            # while the answered reservation is still held.
            assert drv._answer_reservations.get(rec.id) is not None
            await drv.settle_all_open_questions(_rec, "cancelled")
            return True

        ch._finalize_cancel = _ok_finalize
        u = _mk_update(chat_id=-1001, text="/cancel", thread_id=555, user_id=77)
        await ch.handle_update(u)

        assert not any(k == "markup" for k, _, _ in wire.posts)   # no re-anchor
        assert reg.open_question_entries(rec.id) == []            # settled once
        assert drv._answer_reservations.get(rec.id) is None


class TestM4NoticeAwaitCancellation:
    """§A3 wave 2 — M4: a CancelledError while AWAITING the ``Turn failed``
    notice must NOT leak the answered reservation. The rollback is guaranteed by
    a try/finally around the notice (the CancelledError still propagates)."""

    async def test_cancel_during_failure_notice_rolls_back_reservation(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, seq, wire, n = await _handler_ctx_seq(
            tmp_path, fake_telegram_bot)
        # Reserve the answer (as the handler does synchronously at entry).
        token = drv.reserve_answer(rec.id)
        assert token is not None
        assert drv._answer_reservations.get(rec.id) is not None

        # Delivery raises a NON-terminal Exception → reach the failure-notice
        # block; the notice await BLOCKS so we can cancel mid-await.
        ch._driver_send_user_turn = AsyncMock(side_effect=RuntimeError("boom"))
        entered = asyncio.Event()

        async def _blocking_notice(_rec, _text):
            entered.set()
            await asyncio.Event().wait()      # block until cancelled

        ch._driver_post_notice = _blocking_notice

        task = asyncio.ensure_future(ch._deliver_turn_bg(
            rec, "answer", tg_message_id=999, answer_token=token))
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # M4: the reservation was rolled back despite the cancellation.
        assert drv._answer_reservations.get(rec.id) is None


class TestHandlerDelivery:
    async def test_accepted_delivery_keeps_reservation_for_bg_owner(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, n = await _handler_ctx(tmp_path, fake_telegram_bot)
        # A fake send that ACCEPTS (does not actually promote here) — the
        # handler must NOT roll back on the accepted path (the bg task owns it).
        ch._driver_send_user_turn = AsyncMock(return_value="queued")

        u = _mk_update(
            chat_id=-1001, text="prod-db", thread_id=555, user_id=77)
        await ch.handle_update(u)
        await _drain_turns(ch)

        ch._driver_send_user_turn.assert_awaited_once()
        assert ch._driver_send_user_turn.await_args.kwargs.get(
            "tg_message_id") == 999
        # Not rolled back (accepted → the enqueue promoted it in production).
        assert drv._answer_reservations.get(rec.id) is not None

    async def test_rejected_delivery_rolls_back(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, n = await _handler_ctx(tmp_path, fake_telegram_bot)
        ch._driver_send_user_turn = AsyncMock(return_value="dropped_full")

        u = _mk_update(chat_id=-1001, text="answer", thread_id=555, user_id=77)
        await ch.handle_update(u)
        await _drain_turns(ch)

        assert drv._answer_reservations.get(rec.id) is None

    async def test_raised_delivery_rolls_back(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, n = await _handler_ctx(tmp_path, fake_telegram_bot)
        ch._driver_send_user_turn = AsyncMock(side_effect=RuntimeError("boom"))

        u = _mk_update(chat_id=-1001, text="answer", thread_id=555, user_id=77)
        await ch.handle_update(u)
        await _drain_turns(ch)

        assert drv._answer_reservations.get(rec.id) is None


class TestInboundIngressReservation:
    """G4 D2 (v0.96.0, Sol code-r1-2/r2-1 mandatory): the ingress
    reservation is visible BEFORE the handler's FIRST await (the high-water
    advance) and is released on every path — delivery, command
    classification, and cancellation while suspended."""

    async def _ctx(self, tmp_path, fake_telegram_bot):
        ch, reg, rec, drv, n = await _handler_ctx(tmp_path, fake_telegram_bot)
        ch._observer = MagicMock()
        ch._driver_reserve_inbound = (
            lambda r, command=False: (
                drv.reserve_inbound(r.id, command=command), True)[1])
        ch._driver_release_inbound = (
            lambda r, command=False: drv.release_inbound_reservation(
                r.id, command=command))
        return ch, reg, rec, drv

    async def test_reservation_visible_at_first_await(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv = await self._ctx(tmp_path, fake_telegram_bot)
        seen: list[int] = []

        async def spying_high_water(r, mid):
            # THE first await of the handler — a completion racing this
            # suspension must already see the reservation.
            seen.append(drv.inbound_reservations(rec.id))
        ch._driver_advance_high_water = spying_high_water

        u = _mk_update(chat_id=-1001, text="please reconsider the design",
                       thread_id=555, user_id=77)
        await ch.handle_update(u)
        await _drain_turns(ch)

        assert seen and seen[0] >= 1
        # enqueue resolved → released; unread accounting lives in the spool.
        assert drv.inbound_reservations(rec.id) == 0

    async def test_command_path_takes_then_releases(
        self, tmp_path, fake_telegram_bot,
    ):
        """Non-vacuous (Sol code-r2-1): the reservation IS taken before the
        command branch runs, and the finally releases it."""
        ch, reg, rec, drv = await self._ctx(tmp_path, fake_telegram_bot)
        seen: list[int] = []

        async def spying_high_water(r, mid):
            seen.append(drv.inbound_reservations(rec.id))
        ch._driver_advance_high_water = spying_high_water

        u = _mk_update(chat_id=-1001, text="/silent", thread_id=555,
                       user_id=77)
        await ch.handle_update(u)
        await _drain_turns(ch)
        assert seen and seen[0] >= 1        # reservation existed during the await
        assert drv.inbound_reservations(rec.id) == 0   # and was released

    async def test_cancellation_while_suspended_releases(
        self, tmp_path, fake_telegram_bot,
    ):
        """Handler cancelled while blocked on the high-water await — the
        finally must still release the reservation."""
        import asyncio as _asyncio
        ch, reg, rec, drv = await self._ctx(tmp_path, fake_telegram_bot)
        gate = _asyncio.Event()
        entered = _asyncio.Event()

        async def blocking_high_water(r, mid):
            entered.set()
            await gate.wait()
        ch._driver_advance_high_water = blocking_high_water

        u = _mk_update(chat_id=-1001, text="hello there", thread_id=555,
                       user_id=77)
        task = _asyncio.create_task(ch.handle_update(u))
        await entered.wait()
        assert drv.inbound_reservations(rec.id) >= 1   # held while suspended
        task.cancel()
        try:
            await task
        except _asyncio.CancelledError:
            pass
        assert drv.inbound_reservations(rec.id) == 0   # released on cancel


# ===========================================================================
# 10. RED CASE (#649) — in_casa completion inbound gate
#     Specified by Terra (drive run 2026-08-18-b, rounds-649/redcase-spec),
#     accepted by Sol. FROZEN once accepted — do not modify; re-specify +
#     re-accept instead. Invariant: INV-ENG-003 (engagement-completion-gate.md) extended
#     to the in_casa driver — a SUCCESSFUL emit_completion is refused
#     (kind=unread_inbound, retryable) while an inbound turn has been admitted
#     for delivery but not yet handed to the engagement's ClaudeSDKClient,
#     including a turn queued behind the driver's per-turn lock
#     (in_casa_driver.py:411) while an earlier turn streams. At 3dce522d the
#     gate hasattr-no-ops (tools.py:8563; InCasaDriver implements no inbound
#     accessors) and the queued turn dies on the closed client, dropped at
#     INFO (telegram.py:1877-1889; dossier 649 Part B, measured).
# ===========================================================================


class _InCasaRedCaseClient:
    """Fake ClaudeSDKClient. First turn streams (holds the per-turn lock)
    until ``release_first``; ``close()`` marks closed and releases the first
    stream; a later ``query()`` on a closed client raises — the measured
    pre-fix loss mode (dossier 649 claim 5, interleaving (c))."""

    def __init__(self):
        self.query_prompts: list[str] = []
        self.close_calls = 0
        self.closed = False
        self.first_streaming = asyncio.Event()
        self.release_first = asyncio.Event()

    async def query(self, prompt: str) -> None:
        if self.closed:
            raise RuntimeError("client closed")
        self.query_prompts.append(prompt)

    def receive_response(self):
        from claude_agent_sdk import AssistantMessage, TextBlock
        client = self

        def _assistant(text):
            try:
                return AssistantMessage(content=[TextBlock(text=text)],
                                        model="m")
            except TypeError:
                m = AssistantMessage.__new__(AssistantMessage)
                m.content = [TextBlock(text=text)]
                return m

        first = len(client.query_prompts) == 1

        async def _gen():
            if first:
                client.first_streaming.set()
                await client.release_first.wait()
                yield _assistant("first turn text")
            else:
                yield _assistant("second turn text")
        return _gen()

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        self.release_first.set()


class _ProbeLock(asyncio.Lock):
    """Per-engagement driver lock that flags when a SECOND turn queues."""

    def __init__(self):
        super().__init__()
        self.second_waiting = asyncio.Event()

    async def __aenter__(self):
        if self.locked():
            self.second_waiting.set()
        await self.acquire()
        return None


class _FakeStreamHandle:
    def __init__(self):
        self.emits: list[str] = []
        self.finals: list[str] = []

    async def emit(self, text):
        self.emits.append(text)

    async def finalize(self, text):
        self.finals.append(text)


class TestInCasaCompletionInboundGate:
    async def _ctx(self, tmp_path, fake_telegram_bot):
        import agent as agent_mod
        from channels.telegram import TelegramChannel
        from drivers.in_casa_driver import InCasaDriver
        from engagement_registry import EngagementRegistry
        from tools import init_tools

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            "executor", "configurator", "in_casa", "t",
            {"role": "assistant", "channel": "telegram", "user_id": 77},
            topic_id=555)

        client = _InCasaRedCaseClient()
        drv = InCasaDriver(topic_stream_factory=lambda tid: _FakeStreamHandle())
        lock = _ProbeLock()
        drv._clients[rec.id] = client
        drv._ctx_stack[rec.id] = client
        drv._locks[rec.id] = lock

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
        agent_mod.active_engagement_driver = drv

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = reg
        ch._engagement_driver = drv
        ch._observer = MagicMock()
        ch._driver_advance_high_water = AsyncMock()

        async def _send_user_turn(r, text, *, tg_message_id=None):
            # Mirrors casa_core's in_casa branch (casa_core.py:4386-4389).
            await drv.send_user_turn(r, text)
            return None
        ch._driver_send_user_turn = _send_user_turn
        return ch, reg, rec, drv, client, lock, tch

    async def _emit_ok(self, rec):
        from tools import emit_completion, engagement_var
        token = engagement_var.set(rec)
        try:
            res = await emit_completion.handler({
                "text": "done", "artifacts": [], "next_steps": [],
                "status": "ok"})
        finally:
            engagement_var.reset(token)
        return json.loads(res["content"][0]["text"])

    async def test_queued_in_casa_turn_vetoes_completion_before_client_handoff(
            self, tmp_path, fake_telegram_bot):
        import agent as agent_mod
        ch, reg, rec, drv, client, lock, tch = await self._ctx(
            tmp_path, fake_telegram_bot)
        try:
            first = asyncio.create_task(drv.send_user_turn(rec, "first"))
            await asyncio.wait_for(client.first_streaming.wait(), 5)

            u = _mk_update(chat_id=-1001, text="operator message",
                           thread_id=555, user_id=77)
            await ch.handle_update(u)
            await asyncio.wait_for(lock.second_waiting.wait(), 5)
            # Admitted but NOT handed to the client.
            assert client.query_prompts == ["first"]

            payload = await self._emit_ok(rec)
            assert payload["status"] == "error"
            assert payload["kind"] == "unread_inbound"
            assert payload.get("retryable") is True
            # Sol (redcase acceptance r1): pin the HANDLER PRE-CHECK layer
            # specifically — its refusal copy ("waiting unread",
            # tools.py:8615-8622) differs from the terminal hook's
            # ("arrived while completing", tools.py:8669-8675). A hook-only
            # implementation must fail here.
            assert "waiting unread" in payload["message"]
            assert reg.get(rec.id).status == "active"
            assert client.close_calls == 0

            client.release_first.set()
            await asyncio.wait_for(first, 5)
            await _drain_turns(ch)
            # The queued operator turn DELIVERED after the refusal pumped it.
            assert client.query_prompts == ["first", "operator message"]
            assert client.close_calls == 0

            # The veto is CLEARABLE: with nothing queued, completion succeeds.
            payload2 = await self._emit_ok(rec)
            assert payload2["status"] == "acknowledged"
            assert reg.get(rec.id).status == "completed"
        finally:
            agent_mod.active_engagement_driver = None

    async def test_in_casa_turn_admitted_during_finalize_is_vetoed_by_terminal_hook(
            self, tmp_path, fake_telegram_bot):
        import agent as agent_mod
        ch, reg, rec, drv, client, lock, tch = await self._ctx(
            tmp_path, fake_telegram_bot)
        try:
            first = asyncio.create_task(drv.send_user_turn(rec, "first"))
            await asyncio.wait_for(client.first_streaming.wait(), 5)

            # Event-controlled seam INSIDE _finalize_engagement (step 0),
            # which runs AFTER emit_completion's handler pre-check and BEFORE
            # try_transition_terminal invokes its terminal hook: the operator
            # update is admitted exactly in that window.
            async def _drain(engagement):
                u = _mk_update(chat_id=-1001, text="operator message",
                               thread_id=555, user_id=77)
                await ch.handle_update(u)
                await asyncio.wait_for(lock.second_waiting.wait(), 5)
            drv.drain_inbound_spool = _drain

            payload = await self._emit_ok(rec)
            assert payload["status"] == "error"
            assert payload["kind"] == "unread_inbound"
            # Sol (redcase acceptance r1): pin the TERMINAL-HOOK layer — this
            # refusal must be the hook's PRECONDITION_FAILED copy ("arrived
            # while completing", tools.py:8669-8675), proving the veto fired
            # inside the transition, not at the handler pre-check (which read
            # zero before the admission).
            assert "arrived while completing" in payload["message"]
            assert reg.get(rec.id).status == "active"
            assert client.close_calls == 0
            assert client.query_prompts == ["first"]
        finally:
            agent_mod.active_engagement_driver = None
            client.release_first.set()
            await asyncio.wait_for(first, 5)
            await _drain_turns(ch)


class TestReservationKindThreading:
    """#664: the command classification is decided ONCE, at the reservation's
    birth in the handler, and travels with it to release — across the
    channel hook, the casa_core wrapper, and the driver. A dropped kind at
    any hop either falsely discloses the command being processed (reserve
    hop) or leaves a phantom command counter that later hides one real
    message (release hop)."""

    async def _spied_ctx(self, tmp_path, fake_telegram_bot):
        ch, reg, rec, drv, n = await _handler_ctx(tmp_path, fake_telegram_bot)
        ch._observer = MagicMock()
        calls: list[tuple[str, bool]] = []

        def _res(r, command=False):
            calls.append(("reserve", command))
            drv.reserve_inbound(r.id, command=command)
            return True

        def _rel(r, command=False):
            calls.append(("release", command))
            drv.release_inbound_reservation(r.id, command=command)

        ch._driver_reserve_inbound = _res
        ch._driver_release_inbound = _rel
        return ch, reg, rec, drv, calls

    async def test_a_recognized_command_reserves_and_releases_as_command(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, calls = await self._spied_ctx(
            tmp_path, fake_telegram_bot)
        u = _mk_update(chat_id=-1001, text="/silent", thread_id=555,
                       user_id=77)
        await ch.handle_update(u)
        await _drain_turns(ch)
        assert calls == [("reserve", True), ("release", True)], calls
        # end-to-end: nothing lingers on any counter after the command
        assert drv.inbound_reservations(rec.id) == 0
        assert drv.inbound_message_reservations(rec.id) == 0
        # ...and a subsequent ordinary reservation projects as exactly one
        # lost-disclosable message (a phantom command counter would hide it)
        drv.reserve_inbound(rec.id)
        assert drv.inbound_message_reservations(rec.id) == 1
        drv.release_inbound_reservation(rec.id)

    async def test_an_ordinary_message_reserves_and_releases_as_message(
        self, tmp_path, fake_telegram_bot,
    ):
        ch, reg, rec, drv, calls = await self._spied_ctx(
            tmp_path, fake_telegram_bot)
        u = _mk_update(chat_id=-1001, text="please look again",
                       thread_id=555, user_id=77)
        await ch.handle_update(u)
        await _drain_turns(ch)
        assert ("reserve", False) in calls, calls
        assert ("reserve", True) not in calls, calls
        assert ("release", True) not in calls, calls
        assert drv.inbound_reservations(rec.id) == 0

    def test_casa_core_wrappers_forward_the_kind(self):
        """The production wrappers are closures inside casa_core.main(),
        unreachable without booting it — pin the forwarding at the source
        (the idiom test_callback_wiring.py already uses for main()'s
        wiring). Mutation: dropping `command=command` at either hop fails
        here."""
        import ast as _ast
        from pathlib import Path as _Path
        src = (_Path(__file__).resolve().parents[1]
               / "casa" / "rootfs" / "opt" / "casa"
               / "casa_core.py").read_text(encoding="utf-8")
        tree = _ast.parse(src)
        found = {}
        for node in _ast.walk(tree):
            if (isinstance(node, _ast.FunctionDef) and node.name in (
                    "_driver_reserve_inbound", "_driver_release_inbound")):
                kwonly = [a.arg for a in node.args.kwonlyargs]
                forwards = any(
                    isinstance(c, _ast.Call)
                    and any(k.arg == "command"
                            and isinstance(k.value, _ast.Name)
                            and k.value.id == "command"
                            for k in c.keywords)
                    for c in _ast.walk(node))
                found[node.name] = ("command" in kwonly, forwards)
        assert found.get("_driver_reserve_inbound") == (True, True), found
        assert found.get("_driver_release_inbound") == (True, True), found
