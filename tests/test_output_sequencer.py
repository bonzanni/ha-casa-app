"""Tests for ``channels.output_sequencer`` — the per-topic OUTPUT SEQUENCER +
relay-mediated discrete-posting intent registry (v0.79.0 Primitive A, design §2).

Every §2 sentence is binding; these exercise the machinery in isolation with
injected async send/edit recorders and an injected clock. Time is injected — the
slot-hold loop terminates because the fake ``_sleep`` advances the fake clock, so
we never patch ``asyncio.sleep`` (the global-patch OOM lesson).
"""
from __future__ import annotations

import asyncio

import pytest

from channels.output_sequencer import (
    APPLIED,
    ASK_TOOL,
    EMIT_COMPLETION_TOOL,
    FAILED,
    MARKUP_EMPTY,
    REPLY_TOOL,
    SEALED,
    IntentRegistry,
    OutputSequencer,
    project_args,
    projection_hash,
)
from drivers.summary_controller import (
    STATUS_WAITING_REPLY,
    STATUS_WORKING,
    SummaryController,
)


# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.sends: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self._next_id = 100
        self.edit_fails = 0
        self.send_fails = 0

    async def send(self, topic_id: int, text: str) -> int | None:
        if self.send_fails > 0:
            self.send_fails -= 1
            return None
        self.sends.append((topic_id, text))
        mid = self._next_id
        self._next_id += 1
        return mid

    async def edit(self, topic_id: int, message_id: int, text: str) -> bool:
        if self.edit_fails > 0:
            self.edit_fails -= 1
            return False
        self.edits.append((topic_id, message_id, text))
        return True


class Clock:
    """Monotonic fake clock; ``sleep`` advances it so hold loops terminate."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, dt: float) -> None:
        self.t += dt


def _make_seq(rec, clock, **kw):
    return OutputSequencer(
        engagement_id="eng-1",
        topic_id=42,
        send_message=rec.send,
        edit_message=rec.edit,
        _now=clock.now,
        _sleep=clock.sleep,
        slot_hold_s=2.0,
        intent_timeout_s=10.0,
        hold_poll_s=0.05,
        **kw,
    )


def _poster(rec, text):
    async def _post():
        return await rec.send(42, text)
    return _post


# ---------------------------------------------------------------------------
# Projection / hash.
# ---------------------------------------------------------------------------


def test_project_args_pins_reply_to_text_only():
    assert project_args(REPLY_TOOL, {"chat_id": "x", "text": "hi"}) == {"text": "hi"}
    # A5 · F-MULTI: ``multi`` joins the ask projection (defaults False when the
    # frame omits it) — mirrors ``casa_engagement_channel._ask_projection_hash``.
    assert project_args(ASK_TOOL, {"question": "q", "options": ["a", "b"],
                                   "timeout_s": 300, "extra": 1}) == {
        "question": "q", "options": ["a", "b"], "timeout_s": 300, "multi": False,
    }
    assert project_args(ASK_TOOL, {"question": "q", "options": ["a", "b"],
                                   "multi": True})["multi"] is True
    # identity for a gated tool / emit_completion.
    assert project_args("Bash", {"command": "ls"}) == {"command": "ls"}


def test_projection_hash_ignores_reply_chat_id():
    a = projection_hash(REPLY_TOOL, {"chat_id": "1", "text": "same"})
    b = projection_hash(REPLY_TOOL, {"chat_id": "2", "text": "same"})
    assert a == b
    c = projection_hash(REPLY_TOOL, {"text": "different"})
    assert c != a


# ---------------------------------------------------------------------------
# Narration: open / edit-if-latest / seal / no-op gate.
# ---------------------------------------------------------------------------


async def test_edit_narration_if_latest_applies_then_seals_on_interleave():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    mid = await seq.open_narration("hello")
    assert mid == 100 and seq.narration_msg_id == 100 and seq.high_water == 100
    # Still latest → edit applies.
    assert await seq.edit_narration_if_latest(mid, "hello world") == APPLIED
    assert rec.edits[-1] == (42, 100, "hello world")
    # A discrete post below seals narration → subsequent edit returns SEALED.
    h = projection_hash(REPLY_TOOL, {"text": "R"})
    seq.register_intent(request_id="r1", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "R"))
    seq.arm_intent("r1")
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"
    assert seq.narration_msg_id is None  # rollover-on-interleave sealed it
    assert await seq.edit_narration_if_latest(mid, "hello world!") == SEALED


async def test_noop_edit_gate_skips_identical_and_retries_after_failure():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    mid = await seq.open_narration("x")
    # open cached (text="x", absent); identical edit is a no-op skip.
    assert await seq.edit_narration_if_latest(mid, "x") == APPLIED
    assert rec.edits == []  # skipped, never hit the wire
    # A distinct edit that FAILS invalidates the cache so a retry is not
    # suppressed even though its text/markup matches the failed attempt.
    rec.edit_fails = 1
    assert await seq.edit_narration_if_latest(mid, "y") == FAILED
    assert await seq.edit_narration_if_latest(mid, "y") == APPLIED
    assert rec.edits[-1] == (42, mid, "y")


async def test_markup_tristate_distinguishes_empty_from_absent():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    mid = await seq.open_narration("q")  # cached as (q, absent)
    # Same text but an explicit-empty markup is NOT a no-op (Sol r2-2): a
    # markup-only settlement must still fire.
    assert await seq.edit_narration_if_latest(mid, "q", markup=MARKUP_EMPTY) == APPLIED
    assert rec.edits[-1] == (42, mid, "q")
    # Now identical (q, empty) IS a no-op.
    rec.edits.clear()
    assert await seq.edit_narration_if_latest(mid, "q", markup=MARKUP_EMPTY) == APPLIED
    assert rec.edits == []


async def test_inbound_advance_seals_narration():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    mid = await seq.open_narration("mid-turn narration")
    await seq.advance_high_water_for_inbound(operator_msg_id=555)
    assert seq.narration_msg_id is None
    assert seq.high_water == 555
    assert await seq.edit_narration_if_latest(mid, "late narration") == SEALED


# ---------------------------------------------------------------------------
# Intent matching at content-block positions.
# ---------------------------------------------------------------------------


async def test_non_hold_eligible_block_without_intent_is_no_match_instantly():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash("Bash", {"command": "ls"})
    assert await seq.post_for_block("Bash", h) == "no_match"
    assert clock.t == 0.0  # never held


async def test_armed_intent_posts_at_block():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(REPLY_TOOL, {"text": "hi"})
    intent, created = seq.register_intent(
        request_id="r1", tool_name=REPLY_TOOL, projection_hash=h,
        poster=_poster(rec, "hi"))
    assert created
    seq.arm_intent("r1")
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"
    assert rec.sends == [(42, "hi")]
    assert intent.message_id == 100
    assert intent.outcome == {"ok": True, "message_id": 100, "out_of_band": False}


async def test_identical_consecutive_replies_both_post_no_dedup():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(REPLY_TOOL, {"text": "same"})
    seq.register_intent(request_id="r1", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "same"))
    seq.register_intent(request_id="r2", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "same"))
    seq.arm_intent("r1")
    seq.arm_intent("r2")
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"
    assert rec.sends == [(42, "same"), (42, "same")]  # duplicates preferred


async def test_cancelled_first_valid_second_same_projection():
    """§2(3): a tombstone consumes block 1; the valid intent binds block 2 —
    cancelled-first/valid-second poisoning is structurally closed."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(REPLY_TOOL, {"text": "dup"})
    seq.register_intent(request_id="bad", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "dup"))
    seq.register_intent(request_id="good", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "dup"))
    seq.cancel_intent("bad")   # tombstone
    seq.arm_intent("good")
    # Block 1 binds the OLDEST matchable (the tombstone) → consumed-cancelled.
    assert await seq.post_for_block(REPLY_TOOL, h) == "consumed_cancelled"
    assert rec.sends == []
    # Block 2 binds the valid intent.
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"
    assert rec.sends == [(42, "dup")]


async def test_reversed_arrival_distinct_payloads_via_slot_hold():
    """§2(4): handler B reaches casa-main first (B armed before A registers);
    stream order is block A then block B. The slot hold on block A waits for A
    to arm, so A posts at block A and B at block B — stream order preserved."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    hA = projection_hash(REPLY_TOOL, {"text": "A"})
    hB = projection_hash(REPLY_TOOL, {"text": "B"})
    # B registers + arms first.
    seq.register_intent(request_id="B", tool_name=REPLY_TOOL,
                        projection_hash=hB, poster=_poster(rec, "B"))
    seq.arm_intent("B")

    # Relay reads block A first. A is absent → HOLD. Register+arm A "during"
    # the hold by scheduling it on the first poll via a custom sleep.
    async def sleep_then_arm_A(dt):
        clock.t += dt
        if not seq.registry.by_request_id("A"):
            seq.register_intent(request_id="A", tool_name=REPLY_TOOL,
                                projection_hash=hA, poster=_poster(rec, "A"))
            seq.arm_intent("A")
    seq._sleep = sleep_then_arm_A

    assert await seq.post_for_block(REPLY_TOOL, hA) == "posted"
    assert await seq.post_for_block(REPLY_TOOL, hB) == "posted"
    assert rec.sends == [(42, "A"), (42, "B")]  # A before B, stream order


async def test_slot_timeout_late_intent_posts_out_of_band_threaded():
    """§2(4): a pending intent held past the 2s slot is marked slot_missed; the
    relay proceeds; the late intent then posts out-of-band on arrival (the one
    documented, bounded R2 weakening) — no warn, no debt."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(ASK_TOOL, {"question": "q", "options": ["a", "b"],
                                   "timeout_s": 300})
    seq.register_intent(request_id="q1", tool_name=ASK_TOOL,
                        projection_hash=h, poster=_poster(rec, "Q"))
    # pending (not armed) → held then slot times out.
    assert await seq.post_for_block(ASK_TOOL, h) == "slot_timeout"
    assert seq.registry.by_request_id("q1").slot_missed is True
    assert rec.sends == []
    # It arms later; the watcher pass posts it out-of-band threaded.
    seq.arm_intent("q1")
    await seq.process_intents_once()
    assert rec.sends == [(42, "Q")]
    assert seq.registry.by_request_id("q1").outcome["out_of_band"] is True


async def test_intent_timeout_warns_and_leaves_consumption_debt(caplog):
    """§2(5): timeout-post A → late block A consumes the debt → same-hash
    intent B binds block B exactly once."""
    import logging
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(REPLY_TOOL, {"text": "dup"})
    seq.register_intent(request_id="A", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "A"))
    seq.arm_intent("A")
    # 10s pass with no block for A → out-of-band WARN post + a debt tombstone.
    clock.t = 10.0
    with caplog.at_level(logging.WARNING):
        await seq.process_intents_once()
    assert rec.sends == [(42, "A")]
    assert any("out-of-band" in r.message for r in caplog.records)
    a = seq.registry.by_request_id("A")
    assert a.timeout_posted is True and a.matchable() is True  # debt live

    # A second same-hash intent B registers+arms AFTER the timeout post.
    seq.register_intent(request_id="B", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "B"))
    seq.arm_intent("B")
    # Block A arrives late → consumes the DEBT silently (not B).
    assert await seq.post_for_block(REPLY_TOOL, h) == "debt_consumed"
    assert rec.sends == [(42, "A")]  # nothing new
    # Block B binds B exactly once.
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"
    assert rec.sends == [(42, "A"), (42, "B")]


async def test_intent_timeout_measured_from_arm_not_registration():
    """#332: an ingress can sit PENDING through validation/allocation long
    past the timeout — the documented 10s applies to an ARMED intent waiting
    for its block, so the clock starts at arm time, not registration."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(REPLY_TOOL, {"text": "slow"})
    seq.register_intent(request_id="A", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "A"))
    # Validation backlog: the intent arms 11s AFTER registration.
    clock.t = 11.0
    seq.arm_intent("A")
    await seq.process_intents_once()
    assert rec.sends == []  # armed 0s ago — must NOT out-of-band post yet
    clock.t = 20.9
    await seq.process_intents_once()
    assert rec.sends == []  # 9.9s armed — still within the window
    clock.t = 21.0
    await seq.process_intents_once()
    assert rec.sends == [(42, "A")]  # 10s armed — now the timeout post fires


async def test_failed_first_narration_open_keeps_reply_target():
    """#332: _open_narration_locked cleared _turn_reply_to before awaiting
    the send — a transient failure meant the topic-stream retry re-opened
    without the inbound message to reply to, posting the turn's first output
    unthreaded."""
    rec = _ThreadRecorder()
    clock = Clock()
    seq = _make_seq(rec, clock)

    fail_first = {"n": 1}
    real_send = rec.send

    async def _flaky_send(topic_id, text, reply_to=None):
        if fail_first["n"]:
            fail_first["n"] -= 1
            return None
        return await real_send(topic_id, text, reply_to=reply_to)

    seq.send_message = _flaky_send
    seq.set_turn_reply_to(555)
    assert await seq.open_narration("first") is None      # transient failure
    # The retry still threads to the operator's message.
    mid = await seq.open_narration("first")
    assert mid is not None
    assert rec.sends[-1] == (42, "first", 555)
    # Consumed on success — the next open is not a reply.
    seq._narration_msg_id = None
    await seq.open_narration("second")
    assert rec.sends[-1] == (42, "second", None)


async def test_restore_turn_reply_to_failure_undo_never_clobbers_newer():
    """#332: the ask/reply posters consume the one-shot target before their
    send; a failed send restores it — unless a newer envelope already set
    a fresh target."""
    rec = _ThreadRecorder()
    clock = Clock()
    seq = _make_seq(rec, clock)
    seq.set_turn_reply_to(555)
    assert seq.consume_turn_reply_to() == 555
    seq.restore_turn_reply_to(555)               # failed send undo
    assert seq.consume_turn_reply_to() == 555    # target re-armed
    seq.restore_turn_reply_to(555)
    seq.set_turn_reply_to(777)                   # newer envelope wins
    seq.restore_turn_reply_to(555)
    assert seq.consume_turn_reply_to() == 777


async def test_failed_intent_poster_leaves_narration_open():
    """Sol r1 (#332): the relay-mediated path is the PRINCIPAL ask/reply
    route — a poster that fails (None, no compensation) must leave the open
    narration editable, exactly like a failed post_discrete."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    nid = await seq.open_narration("working...")
    h = projection_hash(REPLY_TOOL, {"text": "R"})

    async def _fail():
        return None

    seq.register_intent(request_id="r1", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_fail)
    seq.arm_intent("r1")
    await seq.post_for_block(REPLY_TOOL, h)
    intent = seq.registry.by_request_id("r1")
    assert intent.post_failed is True
    assert seq.narration_msg_id == nid   # narration still open + editable
    assert await seq.edit_narration_if_latest(nid, "still editing") == APPLIED


async def test_cancelled_poster_seals_and_retires_intent():
    """Sol r2 (#332): a poster can SEND and then be cancelled during its
    post-send bookkeeping — ambiguous, like a wire timeout. The sequencer
    must seal conservatively and retire the intent fail-closed so the
    watcher can never repost a possibly-landed message."""
    import asyncio

    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    nid = await seq.open_narration("working...")
    assert nid is not None
    h = projection_hash(REPLY_TOOL, {"text": "R"})

    async def _send_then_cancelled():
        await rec.send(42, "R")             # the message LANDS...
        raise asyncio.CancelledError()      # ...then bookkeeping is cancelled

    seq.register_intent(request_id="r1", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_send_then_cancelled)
    seq.arm_intent("r1")
    with pytest.raises(asyncio.CancelledError):
        await seq.post_for_block(REPLY_TOOL, h)
    assert seq.narration_msg_id is None          # sealed: send may have landed
    intent = seq.registry.by_request_id("r1")
    assert intent.post_failed is True            # resolved fail-closed
    assert intent.outcome == {
        "ok": False, "message_id": None, "out_of_band": False,
        "cancelled": True,
    }
    # The watcher can never repost it.
    sends_before = list(rec.sends)
    clock.t = 100.0
    await seq.process_intents_once()
    assert rec.sends == sends_before


async def test_compensated_intent_seals_narration():
    """Sol r1 (#332): a compensated post PHYSICALLY landed — narration must
    seal exactly as on the confirmed-post path (pre-#332 the unconditional
    pre-poster seal covered this)."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    nid = await seq.open_narration("working...")
    h = projection_hash(REPLY_TOOL, {"text": "R"})
    seq.register_intent(request_id="r1", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "R"))
    seq.arm_intent("r1")
    await seq.mark_intent_compensated("r1", 500)
    assert seq.narration_msg_id is None   # sealed: the message exists
    assert seq.high_water == 500
    assert nid is not None


async def test_response_loss_after_post_reattaches_without_double_post():
    """§2(1): a transport retry whose request_id matches an already-posted
    intent reattaches idempotently and reads the recorded outcome (incl. the
    posted message id) — no second post, no second frame consumed."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(REPLY_TOOL, {"text": "hi"})
    intent, created = seq.register_intent(
        request_id="r1", tool_name=REPLY_TOOL, projection_hash=h,
        poster=_poster(rec, "hi"))
    assert created
    seq.arm_intent("r1")
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"
    assert rec.sends == [(42, "hi")]
    # Response lost after post → transport retry re-registers the SAME id.
    reattached, created2 = seq.register_intent(
        request_id="r1", tool_name=REPLY_TOOL, projection_hash=h,
        poster=_poster(rec, "hi"))
    assert created2 is False
    assert reattached is intent
    assert seq.intent_outcome("r1") == {"ok": True, "message_id": 100,
                                        "out_of_band": False}
    assert rec.sends == [(42, "hi")]  # NO second post


async def test_pre_consumed_block_not_rebound_by_late_same_hash_intent():
    """§2(3): a ``posted`` intent is retired from matching — a late same-hash
    intent binds only its OWN block, never the pre-consumed (retained) one."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(REPLY_TOOL, {"text": "x"})
    a, _ = seq.register_intent(request_id="A", tool_name=REPLY_TOOL,
                               projection_hash=h, poster=_poster(rec, "x"))
    seq.arm_intent("A")
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"  # A consumed
    assert a.matchable() is False  # retired
    # A late same-hash intent B arms; its block binds B, not the retired A.
    b, _ = seq.register_intent(request_id="B", tool_name=REPLY_TOOL,
                               projection_hash=h, poster=_poster(rec, "x2"))
    seq.arm_intent("B")
    assert await seq.post_for_block(REPLY_TOOL, h) == "posted"
    assert b.message_id is not None and b.message_id != a.message_id
    assert rec.sends == [(42, "x"), (42, "x2")]


async def test_prune_turn_clears_registry():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(REPLY_TOOL, {"text": "x"})
    seq.register_intent(request_id="r1", tool_name=REPLY_TOOL,
                        projection_hash=h, poster=_poster(rec, "x"))
    seq.prune_turn()
    assert seq.registry.by_request_id("r1") is None


# ---------------------------------------------------------------------------
# IntentRegistry ordering unit.
# ---------------------------------------------------------------------------


def test_registry_oldest_matchable_is_fifo_on_equal_hash():
    reg = IntentRegistry(_now=lambda: 0.0)
    reg.register(request_id="a", tool_name=REPLY_TOOL, projection_hash="H", poster="a")
    reg.register(request_id="b", tool_name=REPLY_TOOL, projection_hash="H", poster="b")
    first = reg.oldest_matchable(REPLY_TOOL, "H")
    assert first.request_id == "a"
    first.consumed = True
    assert reg.oldest_matchable(REPLY_TOOL, "H").request_id == "b"


# ---------------------------------------------------------------------------
# v0.79.0 (§3, Primitive B) — reply-threading of the turn's first post.
# ---------------------------------------------------------------------------


class _ThreadRecorder:
    """Send recorder that records the reply_to target (3-arg send)."""

    def __init__(self) -> None:
        self.sends: list[tuple[int, str, "int | None"]] = []
        self._next_id = 200

    async def send(self, topic_id, text, reply_to=None):
        self.sends.append((topic_id, text, reply_to))
        mid = self._next_id
        self._next_id += 1
        return mid

    async def edit(self, topic_id, message_id, text):
        return True


async def test_turn_first_narration_threads_to_inbound_then_clears():
    rec = _ThreadRecorder()
    clock = Clock()
    seq = _make_seq(rec, clock)
    # Delivery of an inbound envelope sets the turn's reply-thread target.
    seq.set_turn_reply_to(555)
    await seq.open_narration("first line of the turn")
    # The FIRST post threads to the operator's message.
    assert rec.sends[0] == (42, "first line of the turn", 555)
    # A SECOND post this turn is NOT a reply (target consumed once).
    seq._narration_msg_id = None      # force a fresh open
    await seq.open_narration("second line")
    assert rec.sends[1] == (42, "second line", None)


async def test_consume_turn_reply_to_is_one_shot():
    rec = _ThreadRecorder()
    clock = Clock()
    seq = _make_seq(rec, clock)
    seq.set_turn_reply_to(777)
    assert seq.consume_turn_reply_to() == 777
    assert seq.consume_turn_reply_to() is None      # cleared


async def test_prune_turn_clears_unconsumed_reply_anchor(caplog):
    """Review M1: the causal-handoff one-shot anchor "expires at turn end". A
    button answer that continued the turn but produced NO output leaves the
    anchor set; ``prune_turn`` (the turn-finalize path) MUST clear it so it does
    not leak into the next turn and mis-thread that turn's first message."""
    rec = _ThreadRecorder()
    clock = Clock()
    seq = _make_seq(rec, clock)
    # Anchor set (button answer), but the turn produced no output.
    seq.set_turn_reply_to(4242)
    # Turn ends → prune. The anchor must NOT survive.
    seq.prune_turn()
    assert seq.consume_turn_reply_to() is None
    # Next turn's FIRST message is UNTHREADED (2-arg send, no reply_to target).
    await seq.open_narration("next turn line one")
    assert rec.sends[0] == (42, "next turn line one", None)


async def test_no_reply_target_keeps_two_arg_send():
    # With no inbound target, open_narration uses the 2-arg send (back-compat
    # with the T1 Recorder that has no reply_to parameter).
    rec = Recorder()
    clock = Clock()
    seq = _make_seq(rec, clock)
    await seq.open_narration("hi")
    assert rec.sends == [(42, "hi")]


# ---------------------------------------------------------------------------
# v0.79.0 (§4) — eager out-of-band post leaves a consumption debt so the relay
# debt-consumes the ask/reply block (sealing narration, no double post) and a
# retry reattaches to the recorded outcome.
# ---------------------------------------------------------------------------


async def test_mark_intent_posted_leaves_debt_relay_consumes_block():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    await seq.open_narration("narration before the ask")
    h = projection_hash(ASK_TOOL, {"question": "q", "options": ["a", "b"],
                                   "timeout_s": None})
    # Ingress registers the intent, the handler posts eagerly, then records it.
    seq.register_intent(request_id="a1", tool_name=ASK_TOOL,
                        projection_hash=h, poster=_poster(rec, "unused"))
    intent = await seq.mark_intent_posted("a1", 777)
    assert intent.state == "posted" and intent.message_id == 777
    assert seq.high_water == 777
    # The relay reaching the ask block DEBT-CONSUMES it (no second post) and
    # seals the open narration at that position.
    assert await seq.post_for_block(ASK_TOOL, h) == "debt_consumed"
    assert seq.narration_msg_id is None  # narration sealed
    # No extra sends beyond the one narration open (the eager post is external).
    assert len(rec.sends) == 1
    # Retry reattachment: the recorded outcome carries the posted message id.
    assert seq.intent_outcome("a1") == {
        "ok": True, "message_id": 777, "out_of_band": True}


# ---------------------------------------------------------------------------
# §5 R1 exception: edit_summary is a NON-narration edit.
# ---------------------------------------------------------------------------


async def test_edit_summary_does_not_touch_narration_or_high_water():
    """The pinned SUMMARY (§5) lives ABOVE the causal log: editing it must not
    seal the open narration, advance the high-water, or seal the summary
    itself — so T1 narration invariants are preserved."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    # The summary is posted BEFORE any narration (lowest id); simulate id 500.
    summary_id = 500
    nid = await seq.open_narration("live narration")
    assert seq.high_water == nid and seq.narration_msg_id == nid

    assert await seq.edit_summary(summary_id, "goal\n⚙️ working") == APPLIED
    # High-water + open narration are UNTOUCHED by the summary edit.
    assert seq.high_water == nid
    assert seq.narration_msg_id == nid
    # The narration is still editable-if-latest (not sealed by the summary edit).
    assert await seq.edit_narration_if_latest(nid, "live narration more") == APPLIED


async def test_edit_summary_noop_gate_and_failed_invalidation():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    assert await seq.edit_summary(500, "text-A") == APPLIED
    assert len(rec.edits) == 1
    # Identical edit is a no-op skip (still APPLIED, no new wire edit).
    assert await seq.edit_summary(500, "text-A") == APPLIED
    assert len(rec.edits) == 1
    # A failed edit invalidates the cache so a retry is never suppressed.
    rec.edit_fails = 1
    assert await seq.edit_summary(500, "text-B") == FAILED
    assert await seq.edit_summary(500, "text-B") == APPLIED
    assert rec.edits[-1] == (42, 500, "text-B")


# ---------------------------------------------------------------------------
# F1(b) — platform-origin discrete send (receipt/notice) through the writer.
# ---------------------------------------------------------------------------


async def test_platform_notice_seals_narration_below_it():
    """F1(b): a receipt/notice posted through the single writer SEALS open
    narration — a direct receipt posting below narration while
    ``edit_narration_if_latest`` still returns APPLIED is now impossible."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    nar = await seq.open_narration("working on it ")
    assert await seq.edit_narration_if_latest(nar, "working on it more") == APPLIED
    # A platform notice (receipt) posts through the writer → seals narration.
    notice_mid = await seq.post_platform_notice("📥 Received")
    assert notice_mid is not None and notice_mid > nar          # posted BELOW
    # Narration is now SEALED: a further edit can NOT land as APPLIED (it would
    # edit a message with the receipt below it).
    assert await seq.edit_narration_if_latest(nar, "sneaky append") == SEALED


async def test_platform_notice_failed_send_leaves_narration_open():
    """#392: a notice whose send DEFINITELY failed (wire wrapper returned
    ``None``) must leave narration open and editable — the #332 rule seals
    only on a confirmed send (nothing landed below the narration)."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    nar = await seq.open_narration("working on it ")
    rec.send_fails = 1
    assert await seq.post_platform_notice("📥 Received") is None
    # Nothing was posted below the narration, so it must still be editable.
    assert await seq.edit_narration_if_latest(nar, "still going") == APPLIED
    assert seq.narration_msg_id == nar


async def test_completion_notice_failed_send_leaves_narration_open():
    """#392: same confirmed-send seal rule for the terminal completion post
    (the other caller of the shared notice body)."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    nar = await seq.open_narration("wrapping up ")
    rec.send_fails = 1
    assert await seq.post_completion_notice("✅ done") is None
    assert await seq.edit_narration_if_latest(nar, "wrapping up .") == APPLIED
    assert seq.narration_msg_id == nar


async def test_paged_completion_failed_send_leaves_narration_open():
    """#392: the paged-sender completion branch follows the same rule."""
    from unittest.mock import AsyncMock

    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock, send_paged=AsyncMock(return_value=None))
    nar = await seq.open_narration("wrapping up ")
    assert await seq.post_completion_notice("**summary**") is None
    assert await seq.edit_narration_if_latest(nar, "wrapping up .") == APPLIED
    assert seq.narration_msg_id == nar


# ---------------------------------------------------------------------------
# F4 — turn-end flush of a late armed intent before prune.
# ---------------------------------------------------------------------------


async def test_flush_armed_intents_posts_before_prune():
    """F4: an armed-but-unposted late intent (its block never arrived) must POST
    out-of-band at turn end, before prune drops it — never silently vanish."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    seq.register_intent(
        request_id="x1", tool_name=REPLY_TOOL, projection_hash="h",
        poster=_poster(rec, "late reply"))
    seq.arm_intent("x1")
    # No post_for_block ever runs (the block never arrived). Turn end flushes it.
    await seq.flush_armed_intents()
    assert (42, "late reply") in rec.sends
    outcome = seq.intent_outcome("x1")
    assert outcome is not None and outcome["ok"] is True
    # Prune signals resolution + clears the registry.
    seq.prune_turn()
    assert seq.registry.by_request_id("x1") is None


# ---------------------------------------------------------------------------
# F6 — drain_and_prune_turn: locked drain + prune (no stale-snapshot drop).
# ---------------------------------------------------------------------------


async def test_drain_and_prune_posts_intent_armed_during_drain():
    """F6: an intent B registered+armed by a late ingress DURING the drain's
    poster await must still POST — never be dropped from a stale armed snapshot
    (the old flush→prune sequence pruned it before it posted)."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)

    async def _a_poster():
        # A late ingress lands mid-post (after any armed snapshot): register +
        # arm intent B while the drain holds the lock.
        seq.register_intent(
            request_id="B", tool_name=REPLY_TOOL, projection_hash="hb",
            poster=_poster(rec, "reply B"))
        seq.arm_intent("B")
        return await rec.send(42, "reply A")

    seq.register_intent(
        request_id="A", tool_name=REPLY_TOOL, projection_hash="ha",
        poster=_a_poster)
    seq.arm_intent("A")

    await seq.drain_and_prune_turn()

    # BOTH posted — B caught by the re-snapshot loop, not silently dropped.
    assert (42, "reply A") in rec.sends
    assert (42, "reply B") in rec.sends
    # Registry fully pruned afterwards.
    assert seq.registry.by_request_id("A") is None
    assert seq.registry.by_request_id("B") is None


async def test_drain_and_prune_seals_narration_and_signals_awaiters():
    """drain_and_prune seals open narration and unblocks any awaiter."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    nar = await seq.open_narration("live narration")
    assert seq.narration_msg_id == nar
    # A pending (never-armed) intent with an awaiter waiting.
    seq.register_intent(
        request_id="p1", tool_name=REPLY_TOOL, projection_hash="h",
        poster=_poster(rec, "x"))
    waiter = asyncio.ensure_future(seq.await_intent_resolution("p1", timeout=5.0))
    await asyncio.sleep(0)
    await seq.drain_and_prune_turn()
    # Narration sealed, registry pruned, awaiter released (None outcome).
    assert seq.narration_msg_id is None
    assert seq.registry.by_request_id("p1") is None
    assert await asyncio.wait_for(waiter, timeout=1.0) is None


# ---------------------------------------------------------------------------
# F3 — await_completion_drain: block until the emit_completion debt is consumed.
# ---------------------------------------------------------------------------


def _register_completion_debt(seq, phash="hc"):
    # A consumption debt is never POSTED (its block is consumed silently), so a
    # placeholder poster is fine — it is never invoked.
    intent, _ = seq.register_intent(
        request_id="emit_completion:eng-1", tool_name=EMIT_COMPLETION_TOOL,
        projection_hash=phash, poster="debt")
    intent.state = "posted"
    intent.timeout_posted = True
    intent.consumed = False
    return intent


async def test_await_completion_drain_waits_for_debt_consumption():
    """F3: the completion drain blocks until the relay CONSUMES the
    emit_completion debt (reaches its block ⇒ all prior frames processed)."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    _register_completion_debt(seq)

    drain = asyncio.ensure_future(
        seq.await_completion_drain("emit_completion:eng-1", timeout=5.0))
    await asyncio.sleep(0)
    assert not drain.done()  # blocked: debt not yet consumed

    res = await seq.post_for_block(EMIT_COMPLETION_TOOL, "hc")
    assert res == "debt_consumed"
    assert await asyncio.wait_for(drain, timeout=1.0) is True


async def test_await_completion_drain_times_out_returns_false():
    """F3: an un-consumed debt (no emit_completion block ever arrives) times out
    → False (the caller WARNs and proceeds)."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    _register_completion_debt(seq)
    assert await seq.await_completion_drain(
        "emit_completion:eng-1", timeout=0.05) is False


async def test_await_completion_drain_unknown_intent_returns_true():
    """F3: no debt registered (a cancel/error finalize) ⇒ drain returns True
    immediately."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    assert await seq.await_completion_drain("nope", timeout=0.05) is True


# ---------------------------------------------------------------------------
# W-R5 repro-gate (Sol r2-1/r1-7): no edit of a message AFTER a newer causal
# event exists. The recommendation→ask→result regression, on the REAL sequencer.
# ---------------------------------------------------------------------------


async def test_r5_recommendation_not_edited_after_ask_posts_below_it():
    """W-R5 PROPERTY TEST (repro-first, Sol r2-1): a turn streams a
    recommendation (narration), posts an ask BELOW it, then finalizes the
    result. The recommendation, the ask, and the result each land as a NEW
    message in strict causal order, and once the ask sits below the
    recommendation NO further edit targets the recommendation — the seal-and-
    open-new mechanism (``edit_narration_if_latest`` returns SEALED past a newer
    high-water) enforces the invariant. If this passes, R5 needs no new code."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)

    # 1. Stream the recommendation as latest-message narration (open + grow).
    rec_mid = await seq.open_narration("Recommendation: Option A —")
    assert rec_mid is not None
    assert (
        await seq.edit_narration_if_latest(
            rec_mid, "Recommendation: Option A — locked in")
        == APPLIED
    )  # streaming the latest narration STAYS allowed

    # 2. Post the ask BELOW the recommendation (a discrete relay-mediated intent).
    seq.register_intent(
        request_id="ask-1", tool_name=ASK_TOOL, projection_hash="h",
        poster=_poster(rec, "Q1: Proceed?"))
    seq.arm_intent("ask-1")
    assert await seq.post_for_block(ASK_TOOL, "h") == "posted"
    ask_mid = seq.intent_outcome("ask-1")["message_id"]
    assert ask_mid > rec_mid  # the ask is a NEW message, causally AFTER the rec

    # 3. A late narration token / finalize tries to edit the recommendation now
    #    that the ask sits below it → SEALED (the caller must open a NEW message,
    #    NOT edit the recommendation).
    assert (
        await seq.edit_narration_if_latest(
            rec_mid, "Recommendation: Option A — locked in (late token)")
        == SEALED
    )

    # 4. Finalize the result as a NEW message below the ask.
    res_mid = await seq.open_narration("Result: done")
    assert res_mid is not None

    # PROPERTY 1: three DISTINCT messages in strict causal order.
    assert rec_mid < ask_mid < res_mid
    # PROPERTY 2: the recommendation message is NEVER an edit target after the
    # ask posts — the only edit that ever touched it was the pre-ask streaming
    # grow; no edit-after-newer-event bypassed edit_narration_if_latest.
    edits_of_rec = [e for e in rec.edits if e[1] == rec_mid]
    assert edits_of_rec == [
        (42, rec_mid, "Recommendation: Option A — locked in")]


# ---------------------------------------------------------------------------
# Sol diff gate r2 — REENTRANT-LOCK DEADLOCK on the ask-poster → summary-edit
# path. The ask poster runs WHILE the sequencer's single writer lock is held
# (seal-narration + post is atomic). It calls back into the NON-narration
# summary path (note_ask_waiting → SummaryController.submit_status →
# edit_summary), which re-enters the SAME lock. A plain non-reentrant lock
# deadlocks the holding task forever. These use the REAL SummaryController wired
# to the REAL sequencer — a fake sequencer HIDES this bug (that is why it
# shipped).
# ---------------------------------------------------------------------------


async def _park_forever(_dt: float) -> None:
    """A ``_sleep`` that parks so a controller tick loop never spins (we never
    reach the working+turn-running predicate here anyway)."""
    await asyncio.Event().wait()


def _wire_controller(seq, *, open_qs, goal="Gmail plugin", message_id=500):
    """Real SummaryController wired to the real *seq* — a fake sequencer would
    hide the reentrant-lock deadlock this suite reproduces."""
    return SummaryController(
        engagement_id="eng-1",
        sequencer=seq,
        goal_line=goal,
        open_question_numbers=lambda: list(open_qs),
        message_id=message_id,
        _now=lambda: 0.0,
        _sleep=_park_forever,
    )


async def test_ask_poster_summary_edit_does_not_deadlock_held_writer_lock():
    """Sol's exact repro: status=WAITING, open=[Q11]; an armed DEFERRED ask
    poster adds Q12 and submits WAITING at a NEWER revision (the F1 same-status
    revision-bump path). ``post_for_block`` posts the ask while HOLDING the
    writer lock and awaits the poster; the poster's submit_status → edit_summary
    re-enters that same lock. Before the reentrant-per-task fix this DEADLOCKS —
    the bounded ``asyncio.wait_for(..., 0.2)`` TIMES OUT (RED). With the fix the
    post completes and the summary reflects the added question."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    open_qs = [11]
    controller = _wire_controller(seq, open_qs=open_qs)
    # Seed the current status WAITING at revision 0 (the ask already waiting).
    await controller.submit_status(STATUS_WAITING_REPLY, 0)
    rec.edits.clear()

    async def _ask_poster():
        # Runs UNDER the held writer lock. Adding Q12 and re-submitting WAITING
        # at a newer revision drives edit_summary — which re-enters the lock.
        open_qs.append(12)
        await controller.submit_status(STATUS_WAITING_REPLY, 1)
        return await rec.send(42, "Q12: Proceed?")

    seq.register_intent(
        request_id="ask-2", tool_name=ASK_TOOL, projection_hash="h",
        poster=_ask_poster)
    seq.arm_intent("ask-2")

    # RED-VERIFIED ASSERTION: this call DEADLOCKS before the fix (wait_for times
    # out); after the fix it returns "posted".
    res = await asyncio.wait_for(seq.post_for_block(ASK_TOOL, "h"), 0.2)
    assert res == "posted"
    # The nested summary edit landed and reflects the grown open-questions set.
    assert any("Q11, Q12" in text for _, _, text in rec.edits)


async def test_first_ask_working_to_waiting_class_change_does_not_deadlock():
    """The FIRST ask is a working→waiting status-CLASS change — latent since T1
    (commit 2670794 put note_ask_waiting in the poster), BEFORE the F1 fix. The
    armed ask poster submits WAITING (rev 0) from the default WORKING status and
    adds Q11; the class-change flush drives edit_summary → re-enters the held
    lock. Before the fix ``post_for_block`` DEADLOCKS (wait_for times out)."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    open_qs: list[int] = []
    controller = _wire_controller(seq, open_qs=open_qs)
    assert controller._status == STATUS_WORKING  # default, first-ask precondition

    async def _ask_poster():
        open_qs.append(11)
        await controller.submit_status(STATUS_WAITING_REPLY, 0)  # working→waiting
        return await rec.send(42, "Q11: Proceed?")

    seq.register_intent(
        request_id="ask-1", tool_name=ASK_TOOL, projection_hash="h",
        poster=_ask_poster)
    seq.arm_intent("ask-1")

    res = await asyncio.wait_for(seq.post_for_block(ASK_TOOL, "h"), 0.2)
    assert res == "posted"
    assert controller._status == STATUS_WAITING_REPLY
    assert any(
        STATUS_WAITING_REPLY in text and "Q11" in text
        for _, _, text in rec.edits)


async def test_serialized_still_mutually_exclusive_across_tasks():
    """Owner-tracking must NOT relax exclusion for a DIFFERENT task: while one
    task holds the writer lock (inside a poster await), another task's locked
    op BLOCKS until release. Guards against the reentrant conversion turning the
    single-writer lock into a no-op for concurrent tasks.

    Pins INV-TG-004. Red case demonstrated: making _serialized treat every task as the lock owner fails this test.
    """
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    entered = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def _holder_poster():
        order.append("A-in")
        entered.set()
        await release.wait()          # hold the writer lock open
        order.append("A-out")
        return await rec.send(42, "A")

    seq.register_intent(
        request_id="A", tool_name=REPLY_TOOL, projection_hash="h",
        poster=_holder_poster)
    seq.arm_intent("A")
    task_a = asyncio.ensure_future(seq.post_for_block(REPLY_TOOL, "h"))
    await asyncio.wait_for(entered.wait(), 1.0)

    # A DIFFERENT task attempts a locked summary edit — must BLOCK while A holds.
    task_b = asyncio.ensure_future(seq.edit_summary(500, "B"))
    await asyncio.sleep(0)
    assert not task_b.done()          # exclusion preserved for the other task
    order.append("B-blocked")

    release.set()
    assert await asyncio.wait_for(task_a, 1.0) == "posted"
    assert await asyncio.wait_for(task_b, 1.0) == APPLIED
    # B's edit only ran AFTER A released the lock.
    assert order == ["A-in", "B-blocked", "A-out"]


# ---------------------------------------------------------------------------
# Sol diff gate r3 — CROSS-TASK AB-BA lock inversion between the sequencer
# writer lock and the summary lock. Per-task reentrancy (r2) fixes a poster
# re-entering on its OWN task; it CANNOT fix a DIFFERENT-task cycle:
#   * Task A (ask poster): holds sequencer lock, then wants summary lock;
#   * Task B (submit_status / tick): holds summary lock, then wants sequencer.
# The fix imposes ONE order (sequencer OUTER, summary INNER via _writing), so
# holding the summary lock REQUIRES first holding the sequencer lock — no cycle.
# REAL sequencer + REAL SummaryController (a fake sequencer hides this).
# ---------------------------------------------------------------------------


async def test_cross_task_ask_poster_vs_summary_flush_no_deadlock():
    """Sol's cross-task repro. Task A runs an armed ask poster that, WHILE holding
    the sequencer writer lock, calls ``submit_status`` (wants the summary lock).
    Task B concurrently runs ``submit_status`` on a DIFFERENT task — a text-changing
    transition that acquires the summary lock and then reaches ``edit_summary``
    (wants the sequencer lock). Interleaved so B grabs the summary lock before A's
    poster asks for it.

    On 88beebe (summary→sequencer order still present) this is a classic AB-BA
    deadlock: A holds sequencer + waits summary; B holds summary + waits sequencer
    — the bounded ``asyncio.wait_for`` TIMES OUT and this asserts the deadlock
    (RED). With the global sequencer-OUTER/summary-INNER order, B can never hold
    the summary lock while A holds the sequencer (it blocks on the sequencer FIRST),
    so both tasks complete and the intent is marked posted (GREEN)."""
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    open_qs = [11]

    controller = SummaryController(
        engagement_id="eng-1", sequencer=seq, goal_line="Gmail plugin",
        open_question_numbers=lambda: list(open_qs), message_id=500,
        _now=lambda: 0.0, _sleep=_park_forever,
    )

    poster_holds_seq = asyncio.Event()
    b_released = asyncio.Event()

    async def _ask_poster():
        # Runs UNDER the held sequencer writer lock (post_for_block posts armed
        # intents from inside _serialized). Signal ownership, wait until B has had
        # its chance to grab the summary lock, THEN cross into the summary lock.
        poster_holds_seq.set()
        await b_released.wait()
        await controller.submit_status(STATUS_WAITING_REPLY, 10)  # wants summary
        return await rec.send(42, "Q11: Proceed?")

    seq.register_intent(
        request_id="ask-A", tool_name=ASK_TOOL, projection_hash="h",
        poster=_ask_poster)
    seq.arm_intent("ask-A")

    task_a = asyncio.ensure_future(seq.post_for_block(ASK_TOOL, "h"))
    await asyncio.wait_for(poster_holds_seq.wait(), 1.0)  # A holds the sequencer

    # Task B: a WORKING(-1 default) → WAITING transition — text changes, so
    # _flush_locked actually reaches edit_summary (and thus the sequencer lock).
    task_b = asyncio.ensure_future(
        controller.submit_status(STATUS_WAITING_REPLY, 5))
    # Advance B to its blocking point. This cannot be a single event-gate because
    # the two code paths block in DIFFERENT places: on 88beebe B acquires the
    # summary lock and then blocks on the sequencer lock (it renders); on the fix
    # B blocks on the sequencer lock FIRST and never holds the summary lock (so it
    # never renders). A bounded run of scheduler turns reaches whichever blocking
    # point applies before we release A.
    for _ in range(6):
        await asyncio.sleep(0)
    b_released.set()  # release A's poster into the summary lock

    try:
        res_a, _ = await asyncio.wait_for(
            asyncio.gather(task_a, task_b), timeout=0.5)
    except asyncio.TimeoutError:
        task_a.cancel()
        task_b.cancel()
        await asyncio.gather(task_a, task_b, return_exceptions=True)
        raise AssertionError(
            "CROSS-TASK DEADLOCK: ask poster holds the sequencer lock and waits "
            "for the summary lock while submit_status holds the summary lock and "
            "waits for the sequencer lock (AB-BA inversion)")
    assert res_a == "posted"
    intent = seq.registry.by_request_id("ask-A")
    assert intent.state == "posted" and intent.outcome["ok"] is True
    # Both status submissions applied; the summary reflects the waiting status.
    assert controller._status == STATUS_WAITING_REPLY
    assert any(STATUS_WAITING_REPLY in text for _, _, text in rec.edits)


async def test_global_lock_order_completes_regardless_of_start_order():
    """The single global order (sequencer OUTER, summary INNER) is symmetric: a
    summary flush and an ask poster running concurrently both complete no matter
    which task starts first — there is no ordering-dependent hang."""
    for summary_first in (True, False):
        rec, clock = Recorder(), Clock()
        seq = _make_seq(rec, clock)
        open_qs = [11]
        controller = SummaryController(
            engagement_id="eng-1", sequencer=seq, goal_line="Gmail plugin",
            open_question_numbers=lambda: list(open_qs), message_id=500,
            _now=lambda: 0.0, _sleep=_park_forever,
        )

        async def _ask_poster():
            await controller.submit_status(STATUS_WAITING_REPLY, 7)
            return await rec.send(42, "Q11?")

        seq.register_intent(
            request_id="ask-X", tool_name=ASK_TOOL, projection_hash="h",
            poster=_ask_poster)
        seq.arm_intent("ask-X")

        # Vary the ACTUAL scheduling order: whichever is created first is the
        # first coroutine the loop steps. (Swapping references after both are
        # scheduled would not change scheduling at all.)
        if summary_first:
            flush = asyncio.ensure_future(controller.submit_activity("reading files"))
            poster = asyncio.ensure_future(seq.post_for_block(ASK_TOOL, "h"))
        else:
            poster = asyncio.ensure_future(seq.post_for_block(ASK_TOOL, "h"))
            flush = asyncio.ensure_future(controller.submit_activity("reading files"))
        res = await asyncio.wait_for(
            asyncio.gather(poster, flush), timeout=0.5)
        assert "posted" in res  # the poster resolved


# ===========================================================================
# wb4-1 (whole-branch gate wave 4, BLOCKER): once the engagement TERMINALIZES,
# no discrete send may register/arm/post BELOW the terminal completion.
# (a) ``register_intent`` returns the ``TERMINAL_REGISTRATION`` sentinel (NOT
#     None — None means "no live sequencer" → the ingress eager-fallback), so
#     the ask/reply ingress surfaces ``engagement_terminal``.
# (b) ``_post_intent_locked`` refuses to post under the latch (belt-and-
#     suspenders for an intent that raced in a beat before terminalize) —
#     resolving it fail-closed instead.
# ===========================================================================


async def test_register_intent_rejected_after_terminalize():
    from channels.output_sequencer import TERMINAL_REGISTRATION
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    await seq.terminalize()
    h = projection_hash(REPLY_TOOL, {"text": "late"})
    res = seq.register_intent(
        request_id="r1", tool_name=REPLY_TOOL, projection_hash=h,
        poster=_poster(rec, "late"))
    # A sentinel, NOT None and NOT a (intent, created) tuple.
    assert res is TERMINAL_REGISTRATION
    # Nothing was actually registered.
    assert seq.registry.by_request_id("r1") is None


async def test_post_intent_locked_discards_under_terminal_latch():
    """Pins INV-ENG-005. Red case demonstrated: disabling _post_intent_locked's
    terminal-latch fail-closed branch (`if False:`) fails this test."""
    from channels.output_sequencer import SendIntent
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    h = projection_hash(REPLY_TOOL, {"text": "x"})
    intent, _ = seq.register_intent(
        request_id="r1", tool_name=REPLY_TOOL, projection_hash=h,
        poster=_poster(rec, "x"))
    seq.arm_intent("r1")
    # Latch terminal DIRECTLY (simulates the race: this armed intent slipped in
    # a beat before terminalize's abort loop, so it carries no outcome yet).
    seq._terminal = True
    async with seq.serialized():
        await seq._post_intent_locked(intent, out_of_band=False)
    # Nothing posted below the terminal completion; the intent resolved
    # fail-closed with a terminal outcome.
    assert rec.sends == []
    assert intent.outcome == {"ok": False, "message_id": None, "terminal": True}


# ===========================================================================
# wb4-2 (whole-branch gate wave 4, BLOCKER): terminalize must PRESERVE an
# unresolved emit_completion consumption debt (so finalize's completion drain
# still waits for the relay to reach its causal block), and every post-terminal
# PLATFORM notice must be DISCARDED — only the dedicated completion seam posts.
# ===========================================================================


def _register_wb4_completion_debt(seq):
    """Register the emit_completion one-block consumption debt exactly as
    ``driver.register_completion_consumption`` does (wb4-2)."""
    rid = "emit_completion:eng-1"
    phash = projection_hash(EMIT_COMPLETION_TOOL, {})

    async def _noop():
        return None

    intent, _ = seq.register_intent(
        request_id=rid, tool_name=EMIT_COMPLETION_TOOL,
        projection_hash=phash, poster=_noop)
    intent.state = "posted"
    intent.timeout_posted = True
    intent.consumed = False
    return rid


async def test_terminalize_preserves_unconsumed_completion_debt():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    rid = _register_wb4_completion_debt(seq)
    await seq.terminalize()
    # The debt SURVIVES the prune — finalize's drain can still observe it.
    intent = seq.registry.by_request_id(rid)
    assert intent is not None
    assert intent.timeout_posted and not intent.consumed


async def test_await_completion_drain_blocks_on_preserved_debt():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    rid = _register_wb4_completion_debt(seq)
    await seq.terminalize()
    # An UNCONSUMED preserved debt is NOT trivially drained: the drain times out
    # (returns False), never reading the pruned-away intent as drained.
    drained = await seq.await_completion_drain(rid, timeout=0.02)
    assert drained is False
    # Consuming it (the relay reaching the emit_completion block) then drains it.
    intent = seq.registry.by_request_id(rid)
    intent.consumed = True
    seq._signal_resolution(rid)
    assert await seq.await_completion_drain(rid, timeout=0.02) is True


async def test_platform_notice_discarded_after_terminal_completion_posts():
    rec, clock = Recorder(), Clock()
    seq = _make_seq(rec, clock)
    await seq.terminalize()
    # A lagging platform notice (an inbound receipt / violation notice) is
    # DISCARDED — nothing may land below the terminal completion.
    assert await seq.post_platform_notice("late violation notice") is None
    assert rec.sends == []
    # The completion-only write seam STILL posts the terminal completion.
    mid = await seq.post_completion_notice("terminal completion")
    assert mid is not None
    assert rec.sends == [(42, "terminal completion")]
    assert seq.high_water == mid
