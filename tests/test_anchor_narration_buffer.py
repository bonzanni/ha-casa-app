"""Tests for the D5 anchor-scoped NARRATION BUFFER (Task C2) + the atomic
``OutputSequencer.post_unless_anchor_open`` read-decide-write (Sol r4-2), built
on top of Task C1's driver-injected ``open_anchor_state`` seam +
anchor-candidate + arming bookkeeping (design round-4 §D5).

Task C2 turns arming from a behavior-neutral latch (C1) into the real
buffer state machine:

* once a turn has SUCCESSFULLY surfaced a free-text ANCHOR, subsequent prose is
  BUFFERED (routed through the atomic op), never posted;
* a later tool_use FLUSHES the buffer (post-tool prose is legitimate) AND
  DISARMS suppression for the rest of the turn (r23-2);
* an answer arriving before ``result`` (the seam re-read reports no open anchor)
  FLUSHES the buffer;
* ``result`` with the anchor STILL open-and-unanswered DISCARDS the buffer (the
  F-LEAK2 kill).

The ``hold_pending`` persistence / crash-replay machinery is Task C3 — this task
keeps the buffer purely IN-MEMORY (held frames carry their frame coordinates so
C3 can add the checkpoint exemption on top without restructuring).

These tests drive a REAL relay over a REAL ``OutputSequencer`` (constructed with
tiny, injected-clock hold/timeout windows so nothing waits on real wall time —
never a mock of the sequencer, matching ``test_topic_stream.py``'s style)
against a REAL temp NDJSON log dir. Clocks are injected (``_now``/``_sleep``);
we never patch ``asyncio.sleep`` (the module-local / injected-clock rule,
CLAUDE.md memory cage).
"""
from __future__ import annotations

import asyncio
import logging
import os

import pytest

from channels.output_sequencer import ASK_TOOL, OutputSequencer, projection_hash
from drivers.topic_stream import StreamCursor
from test_topic_stream import (
    Recorder,
    _ident,
    _init,
    _make_relay,
    _result,
    _spawn,
    _text,
    _tool_in,
    _write_current,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _fast_sequencer(
    rec, *, slot_hold_s: float = 0.05, intent_timeout_s: float = 5.0,
    hold_poll_s: float = 0.05,
):
    """A REAL ``OutputSequencer`` with a fake clock/sleep so the 2s ordered-
    slot hold (and any intent-timeout math) resolves instantly in test time —
    the sleep fake ADVANCES the fake clock rather than actually waiting, so
    ``post_for_block``'s hold loop still exercises its real deadline logic."""
    clock = {"t": 0.0}

    async def _tick_sleep(dt: float) -> None:
        clock["t"] += dt

    seq = OutputSequencer(
        engagement_id="eng-1", topic_id=42,
        send_message=rec.send, edit_message=rec.edit,
        _now=lambda: clock["t"], _sleep=_tick_sleep,
        slot_hold_s=slot_hold_s, intent_timeout_s=intent_timeout_s,
        hold_poll_s=hold_poll_s,
    )
    return seq, clock


def _append_current(log_dir, frames) -> None:
    """Append NDJSON *frames* to ``<log_dir>/current`` (models a live poll)."""
    import json
    import os

    path = os.path.join(str(log_dir), "current")
    with open(path, "ab") as fh:
        for fr in frames:
            fh.write(json.dumps(fr).encode("utf-8") + b"\n")


def _anchor_ask(question: str = "Q?") -> dict:
    """A free-text-anchor ask tool_use block — NO ``options`` key (§D5 scope:
    free-text anchors only, never a button ask)."""
    return _tool_in(ASK_TOOL, {"question": question})


def _button_ask(question: str = "Pick one") -> dict:
    return _tool_in(ASK_TOOL, {"question": question, "options": ["a", "b"]})


def _narration_sends(rec, *, exclude=()):
    return [t for _tp, t in rec.sends if t not in exclude]


def _ah(question: str = "Q?") -> str:
    """wb2-1 (whole-branch gate wave 2): the projection hash the relay computes
    for an anchor ask with *question* — the SAME value the driver seam reports as
    an anchor's ``source_hash``. A candidate arms POSITIVELY only when the seam's
    ``source_hash`` matches its own block hash, so every arming test's seam stub
    now carries the identity of the anchor whose prose it is meant to suppress."""
    return projection_hash(ASK_TOOL, {"question": question})


# ===========================================================================
# RED anchor (d): the atomic sequencer op (Sol r4-2) — deterministic interleave.
# ===========================================================================


async def test_atomic_op_holds_when_anchor_open():
    """A bare seam re-read races the late poster; the atomic op makes the
    read + narration write share the ONE writer lock the late poster uses.
    Deterministic interleave: the late poster takes the lock and posts the
    anchor BEFORE the atomic op acquires it, so the op observes the anchor open
    and HOLDS — narration never lands below the anchor."""
    rec = Recorder()
    seq, _clock = _fast_sequencer(rec)
    anchor = {"posted": False}
    seam = lambda: (1, 111) if anchor["posted"] else None  # noqa: E731
    holding = asyncio.Event()
    proceed = asyncio.Event()

    async def late_anchor_poster():
        async with seq.serialized():
            holding.set()
            await proceed.wait()          # hold the lock across the barrier
            mid = await rec.send(42, "[ANCHOR]")
            anchor["posted"] = True
            seq._high_water = mid

    async def narration():
        await holding.wait()              # the anchor task owns the lock
        proceed.set()                     # let it post the anchor + release
        return await seq.post_unless_anchor_open(
            "trailing", seam, poster=lambda: rec.send(42, "trailing"),
        )

    tp = asyncio.create_task(late_anchor_poster())
    status = await narration()
    await tp

    assert status == "held"
    assert _narration_sends(rec) == ["[ANCHOR]"]  # narration never below anchor


async def test_atomic_op_posts_when_anchor_absent():
    """No open anchor at the seam read ⇒ the atomic op runs the poster under the
    lock and returns ``"posted"``."""
    rec = Recorder()
    seq, _clock = _fast_sequencer(rec)
    status = await seq.post_unless_anchor_open(
        "narration", lambda: None, poster=lambda: rec.send(42, "narration"),
    )
    assert status == "posted"
    assert _narration_sends(rec) == ["narration"]


async def test_atomic_op_default_poster_opens_narration():
    """Called without an explicit poster, the atomic op posts *text* as a fresh
    narration message (belt-and-suspenders default)."""
    rec = Recorder()
    seq, _clock = _fast_sequencer(rec)
    status = await seq.post_unless_anchor_open("hello", lambda: None)
    assert status == "posted"
    assert _narration_sends(rec) == ["hello"]
    assert seq.narration_msg_id is not None


# ===========================================================================
# RED anchor (a): the live-observed F-LEAK2 sign-off — DISCARD at result.
# ===========================================================================


async def test_anchor_open_at_result_discards_trailing_narration(tmp_path):
    """The exact live sequence: an anchor surfaced, the agent narrated the
    observed sign-off, and ``result`` arrived with the anchor STILL open-and-
    unanswered ⇒ the trailing narration is DISCARDED — ZERO narration posts."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    signoff = (
        "The ask posted as an open-ended anchor. I'll end my turn and wait for "
        "the operator's answer before proceeding."
    )
    _write_current(tmp_path, [_init(), _anchor_ask(), _text(signoff), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (5, 500, _ah()),  # open-and-unanswered throughout
    )
    await relay.run()

    assert rec.sends == []   # the sign-off never reached the wire (F-LEAK2 kill)
    assert rec.edits == []


# ===========================================================================
# RED anchor (b): tool_use FLUSHES + DISARMS (Sol r23-2) — both segments post.
# ===========================================================================


async def test_tool_use_flushes_buffer_and_disarms(tmp_path):
    """anchor → prose → tool_use → post-tool prose → ``result`` (anchor still
    open): the tool_use FLUSHES the buffered prose (post-tool work is
    legitimate) and DISARMS, so the post-tool prose also posts — BOTH segments
    are visible even though the anchor is open at result."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    _write_current(tmp_path, [
        _init(), _anchor_ask(),
        _text("alpha "),
        _tool_in("Bash", {"command": "ls"}),
        _text("beta"),
        _result(),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (5, 500, _ah()),  # anchor stays open all turn
    )
    await relay.run()

    assert _narration_sends(rec) == ["alpha "]   # flushed on the tool_use
    assert rec.edits[-1][2] == "alpha beta"       # post-tool prose appended
    # DISARMED: the post-tool prose is not re-buffered/discarded at result.


async def test_second_anchor_reamms_after_flush(tmp_path):
    """Re-arming happens ONLY when a NEW anchor surfaces. A first anchor's
    buffer flushes+disarms on a tool_use; a SECOND, genuinely NEW anchor (its
    OWN, DIFFERENT ``tg_message_id``) re-arms, so its trailing prose is buffered
    and DISCARDED at result again.

    wb1-3: the NEW anchor must carry its OWN mid — re-arming off the SAME
    already-flushed/disarmed mid is precisely the bug this finding fixes, so the
    seam here reports 500 for Q1 then 601 for the genuinely-new Q2."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    seam_state = {"open": (5, 500, _ah("Q1?"))}
    seam = lambda: seam_state["open"]  # noqa: E731

    _write_current(tmp_path, [
        _init(), _anchor_ask("Q1?"),
        _text("first "),
        _tool_in("Bash", {"command": "ls"}),   # flush "first " + disarm A(500)
    ])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq, open_anchor_state=seam,
    )
    await relay.run()
    assert _narration_sends(rec) == ["first "]      # flushed on the tool_use
    assert relay._suppressing_for is None

    # A SECOND, genuinely NEW anchor surfaces with its OWN mid (601, not 500).
    seam_state["open"] = (6, 601, _ah("Q2?"))
    _append_current(tmp_path, [
        _anchor_ask("Q2?"),                     # NEW anchor → re-arm off 601
        _text("second signoff"),                # buffered under the new anchor
        _result(),
    ])
    await relay.run()

    assert _narration_sends(rec) == ["first "]   # "second signoff" DISCARDED


async def test_rejected_ask_after_flush_does_not_rearm_off_prior_anchor(tmp_path):
    """wb1-3 (whole-branch gate wave 1): anchor A is open and armed; a tool_use
    FLUSHES + DISARMS A; then a REJECTED / invalid free-anchor ask B records a
    candidate but never surfaces its OWN anchor — the seam still reports A open.
    Suppression must NOT re-arm off A (D5's 're-arm only when a NEW anchor
    successfully surfaces'), so the legitimate post-B prose POSTS. Pre-fix the
    candidate armed off the still-open A and DISCARDED the post-B prose at
    result."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    _write_current(tmp_path, [
        _init(), _anchor_ask("Q_A?"),        # anchor A surfaces → arm off 500
        _text("a-signoff "),                 # buffered under A
        _tool_in("Bash", {"command": "ls"}),  # flush "a-signoff " + DISARM A(500)
        _anchor_ask("Q_B?"),                 # REJECTED ask B → candidate, no anchor
        _text("b-prose"),                    # post-B prose — must POST
        _result(),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (5, 500, _ah("Q_A?")),  # A stays open; B never surfaces
    )
    await relay.run()

    # "a-signoff " flushed on the tool_use (a send); "b-prose" reaches the wire
    # (appended to the same growing narration message) because B never surfaced a
    # NEW anchor — pre-fix it re-armed off A and DISCARDED "b-prose".
    assert _narration_sends(rec) == ["a-signoff "]
    assert rec.edits[-1][2] == "a-signoff b-prose"
    assert relay._suppressing_for is None


async def test_rejected_ask_next_turn_does_not_bind_prior_open_anchor(tmp_path):
    """wb2-1 (whole-branch gate wave 2): the CROSS-TURN residual. Anchor A is
    open from turn 1; turn 2's ask B records a candidate (its block resolves
    ``slot_timeout`` — the out-of-band / refusal ordering) but never surfaces its
    OWN anchor (the driver later refuses B ``question_pending`` and cancels its
    intent), while the driver seam still reports A open. The per-turn disarmed set
    was empty, so wave-1's negative binding armed B's prose off the still-open A
    and discarded it at result. Positive source-hash binding fixes it: B's
    candidate (its own block hash) never matches A's ``source_hash``, so the
    post-B prose POSTS."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    # The seam reports A (turn 1's anchor) throughout — with A's OWN source hash.
    seam = lambda: (5, 500, _ah("A?"))  # noqa: E731

    _write_current(tmp_path, [_init(), _anchor_ask("A?"), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq, open_anchor_state=seam,
    )
    await relay.run()

    # Turn 2's B records a candidate but its own anchor never surfaces; the seam
    # still reports A. B's post-prose must reach the wire (not armed off A).
    _append_current(
        tmp_path,
        [_init(), _anchor_ask("B?"), _text("keep me"), _result()],
    )
    await relay.run()

    assert "keep me" in _narration_sends(rec)


# ===========================================================================
# wb2-3 (whole-branch gate wave 2): TERMINAL settlement DISCARDS held narration.
# ===========================================================================


async def test_terminal_settle_does_not_flush_held_signoff(tmp_path):
    """wb2-3: terminal settlement closes the anchor ledger (the seam now reports
    ``None``) while the relay is still alive and a queued ``result`` is consumed
    in that window. Without a terminal signal the relay mistakes the closed
    ledger for an answer and FLUSHES the held sign-off below the terminal
    completion. The driver-injected ``engagement_terminal`` seam makes the result
    boundary DISCARD the held prose instead (D5 discard doctrine)."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    terminal = {"value": False}

    def seam():
        # Terminal settle closes the anchor, so the effective ledger seam reports
        # None — indistinguishable from an answer at the bare seam.
        return None if terminal["value"] else (5, 500, _ah())

    _write_current(
        tmp_path,
        [_init(), _anchor_ask(), _text("I will wait")],
    )
    relay = _make_relay(
        tmp_path, tmp_path / ".stream_cursor.json", rec, events,
        sequencer=seq, open_anchor_state=seam,
        engagement_terminal=lambda: terminal["value"],
    )
    await relay.run()
    assert rec.sends == []

    # Model settle_all_open_questions closing the ledger before driver.cancel
    # stops the still-live relay, then a queued result frame is consumed.
    terminal["value"] = True
    _append_current(tmp_path, [_result()])
    await relay.run()

    assert "I will wait" not in _narration_sends(rec)


# ===========================================================================
# RED anchor (c): an answer before result FLUSHES the buffer.
# ===========================================================================


async def test_answer_before_result_flushes_buffer(tmp_path):
    """anchor → prose → answer arrives → ``result``: the seam re-read at result
    reports no open anchor (answered), so the buffered prose is FLUSHED (an
    answer legitimizes the trailing prose)."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    seam_state = {"open": (9, 900, _ah())}
    seam = lambda: seam_state["open"]  # noqa: E731

    _write_current(tmp_path, [_init(), _anchor_ask(), _text("bye")])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq, open_anchor_state=seam,
    )
    await relay.run()

    assert relay._suppressing_for == (9, 900)   # armed mid-turn
    assert rec.sends == []                        # buffered, not yet posted

    # The operator ANSWERS before the turn's result — the seam now reports the
    # anchor closed.
    seam_state["open"] = None
    _append_current(tmp_path, [_result()])
    await relay.run()

    assert _narration_sends(rec) == ["bye"]      # FLUSHED at result


# ===========================================================================
# C1 bookkeeping, updated for C2 behaviour.
# ===========================================================================


async def test_late_post_arms_then_discards(tmp_path):
    """(C1 (a), now with the C2 consumer) The ask block resolves
    ``slot_timeout`` (nothing registered — a late out-of-band post); suppression
    does NOT arm at block-resolution time (seam still None), then arms once the
    seam reports the anchor open before the next text frame. The trailing prose
    is buffered and DISCARDED at result (anchor still open)."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)

    seam_state = {"open": None}
    seam = lambda: seam_state["open"]  # noqa: E731

    _write_current(tmp_path, [_init(), _anchor_ask()])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq, open_anchor_state=seam,
    )
    await relay.run()

    assert relay._anchor_candidate is not None
    assert relay._suppressing_for is None  # seam None at block time ⇒ unarmed

    # The anchor surfaces OUT OF BAND (a late watcher post) between polls.
    seam_state["open"] = (7, 555, _ah())
    _append_current(tmp_path, [_text("hello")])
    await relay.run()

    assert relay._suppressing_for == (7, 555)  # armed before the text frame
    assert rec.sends == []                      # "hello" buffered, not posted

    _append_current(tmp_path, [_result()])
    await relay.run()
    assert rec.sends == []                       # DISCARDED (anchor still open)


async def test_post_first_debt_consumed_arms_then_discards(tmp_path):
    """(C1 (b)) The intent times out and posts OUT OF BAND before the relay
    reaches its block; the block resolves ``debt_consumed``. Still records a
    candidate and arms once the seam confirms open — trailing prose is buffered
    and DISCARDED at result (anchor still open)."""
    rec, events = Recorder(), []
    seq, clock = _fast_sequencer(rec)

    h = projection_hash(ASK_TOOL, {"question": "Q?"})

    async def poster():
        return await rec.send(42, "[anchor]Q?")

    seq.register_intent(
        request_id="a1", tool_name=ASK_TOOL, projection_hash=h, poster=poster,
    )
    seq.arm_intent("a1")
    clock["t"] = 10.0  # past intent_timeout_s (5.0)
    await seq.process_intents_once()  # posts out-of-band NOW; leaves the debt

    seam = lambda: (3, 777, _ah())  # noqa: E731 — the anchor is genuinely open

    _write_current(tmp_path, [_init(), _anchor_ask(), _text("after")])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq, open_anchor_state=seam,
    )
    await relay.run()

    assert relay._anchor_candidate is not None
    assert relay._suppressing_for == (3, 777)
    assert _narration_sends(rec, exclude=("[anchor]Q?",)) == []  # buffered

    _append_current(tmp_path, [_result()])
    await relay.run()
    assert _narration_sends(rec, exclude=("[anchor]Q?",)) == []  # DISCARDED


async def test_failed_poster_never_arms_narration_posts(tmp_path):
    """(C1 (c)) ``post_for_block`` can return ``"posted"`` even though the
    ARMED intent's poster recorded a FAILURE outcome — the driver's ledger
    never gained the entry, so the seam never reports it open. The candidate is
    recorded, suppression never arms, and narration is routed through the atomic
    op which POSTS it (the anchor is genuinely absent)."""
    async def failing_poster():
        return None  # simulates a wire post failure

    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    h = projection_hash(ASK_TOOL, {"question": "Q?"})
    seq.register_intent(
        request_id="f1", tool_name=ASK_TOOL, projection_hash=h,
        poster=failing_poster,
    )
    seq.arm_intent("f1")

    _write_current(tmp_path, [_init(), _anchor_ask(), _text("after")])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: None,  # the ledger never gained the entry
    )
    await relay.run()

    assert relay._anchor_candidate is not None  # status was "posted" (misleading)
    assert relay._suppressing_for is None        # seam never confirmed it open
    assert _narration_sends(rec) == ["after"]    # posted via the atomic op

    _append_current(tmp_path, [_result()])
    await relay.run()
    assert _narration_sends(rec) == ["after"]    # unchanged after the turn closes


async def test_default_open_anchor_state_is_inert(tmp_path):
    """An un-injected ``open_anchor_state`` (default ``None``) leaves the
    feature completely inert — a matching ask block still records a candidate
    (harmless bookkeeping) but suppression can never arm and narration posts
    unbuffered. Backward-compatible for every other ``TopicStreamRelay``
    caller."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    _write_current(tmp_path, [_init(), _anchor_ask(), _text("hi")])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cursor, rec, events, sequencer=seq)  # no seam
    await relay.run()

    assert relay._anchor_candidate is not None
    assert relay._suppressing_for is None
    assert _narration_sends(rec) == ["hi"]

    _append_current(tmp_path, [_result()])
    await relay.run()
    assert _narration_sends(rec) == ["hi"]


async def test_turn_boundary_resets_candidate_arming_and_buffer(tmp_path):
    """An anchor armed mid-turn (with a non-empty buffer) must not leak into the
    NEXT turn — the turn-ending ``result`` frame resets candidate, arming AND
    the buffer exactly like every other per-turn field."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    _write_current(tmp_path, [_init(), _anchor_ask(), _text("hi")])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (1, 100, _ah()),
    )
    await relay.run()
    assert relay._suppressing_for == (1, 100)  # armed mid-turn (before result)
    assert relay._anchor_buffer                # "hi" buffered

    _append_current(tmp_path, [_result()])
    await relay.run()
    assert relay._anchor_candidate is None
    assert relay._suppressing_for is None
    assert relay._anchor_buffer == []          # the turn boundary cleared it


async def test_button_ask_with_options_is_not_a_candidate(tmp_path):
    """Scope (§D5): "not button asks (they block the turn anyway)" — a
    button-style ask (``options`` given) never sets ``_anchor_candidate``, so
    suppression never arms and its trailing prose posts unbuffered."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    _write_current(tmp_path, [_init(), _button_ask(), _text("hi"), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (1, 100),
    )
    await relay.run()

    assert relay._anchor_candidate is None
    assert relay._suppressing_for is None
    assert _narration_sends(rec) == ["hi"]     # posted (never buffered)


# ===========================================================================
# Task C3: the ``hold_pending`` write-ahead marker + crash/replay machinery
# (spec §D5 r5-2 write-ahead / r3-2 checkpoint-hold / r4-3 cold disarm /
# r29-3 SEGMENT_GAP floor / r5-3+r8-2+r9-3 abnormal spawn).
#
# Crash injection = DROP the relay instance and build a COMPLETELY NEW one on
# the persisted cursor / NDJSON (never warm re-entry). Real primitives + real
# temp NDJSON + injected clocks throughout; ``asyncio.sleep`` is never patched.
# ===========================================================================


def _mixed_anchor_text(question: str, text: str) -> dict:
    """A single assistant frame carrying an anchor ask tool_use block PLUS a
    trailing text block (the §D5 r3-2 mixed frame)."""
    return {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": ASK_TOOL, "input": {"question": question}},
            {"type": "text", "text": text},
        ]},
    }


# --- RED (a): mixed anchor+text, no prior narration; crash after processing --


async def test_crash_after_held_mixed_frame_resurfaces_before_advance(tmp_path):
    """(a) A mixed anchor+text frame is the turn's FIRST visible output; the
    trailing prose is buffered (write-ahead marker set) and its frame does NOT
    checkpoint. A crash after processing (drop the relay) leaves marker-True /
    checkpoint-held. A COMPLETELY NEW relay on the persisted cursor DISARMS,
    re-renders the prose as ordinary narration, THEN advances the cursor past
    the frame — resurface-never-lose."""
    signoff = "I'll wait for your answer."
    offs = _write_current(tmp_path, [_init(), _mixed_anchor_text("Q?", signoff)])
    cursor = tmp_path / ".stream_cursor.json"

    # First relay: holds the prose (anchor open), never posts, crashes (dropped).
    rec1 = Recorder()
    seq1, _c1 = _fast_sequencer(rec1)
    relay1 = _make_relay(
        tmp_path, cursor, rec1, [], sequencer=seq1,
        open_anchor_state=lambda: (5, 500, _ah()),
    )
    await relay1.run()
    assert rec1.sends == []                                   # held, never posted
    assert StreamCursor.load(cursor).hold_pending is True     # write-ahead marker
    assert StreamCursor.load(cursor).current["offset"] == offs[0]  # held frame

    # Brand-new relay on the persisted cursor/NDJSON — a genuine cold recovery.
    rec2, events2 = Recorder(), []
    seq2, _c2 = _fast_sequencer(rec2)
    relay2 = _make_relay(
        tmp_path, cursor, rec2, events2, sequencer=seq2,
        open_anchor_state=lambda: (5, 500, _ah()),   # anchor STILL open on recovery
    )
    await relay2.run()

    assert _narration_sends(rec2) == [signoff]                # RESURFACED
    assert relay2.cursor.current["offset"] == offs[1]         # advanced past it


# --- RED (b): crash BEFORE the write-ahead save (marker false, checkpoint held)


async def test_crash_before_writeahead_save_resurfaces(tmp_path):
    """(b) The crash lands the instant BEFORE the write-ahead ``_save`` — on
    disk the marker is FALSE, but the anchor frame was already checkpointed and
    the held text frame was NOT (checkpoint held one frame back). A new relay
    re-processes the text frame; the anchor's candidate is lost to replay
    suppression (a tool_use frame replays with no side effects), so the prose
    posts as ordinary narration. The dangerous window (marker false AND cursor
    advanced past the held frame) never exists."""
    offs = _write_current(
        tmp_path, [_init(), _anchor_ask("Q?"), _text("signoff"), _result()],
    )
    cursor = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    # Crash-before-write-ahead state: anchor checkpointed, marker never saved,
    # the text frame's checkpoint held one frame back.
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[1]},   # through the anchor frame
        message_ids=[],
        hold_pending=False,                            # write-ahead never landed
    ).save(cursor)

    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (5, 500, _ah()),   # anchor open — yet the prose posts
    )
    await relay.run()

    assert _narration_sends(rec) == ["signoff"]        # RESURFACED
    assert StreamCursor.load(cursor).hold_pending is False


# --- RED (c): cold + marker ⇒ catch-up renders, clears, later turn re-arms ----


async def test_cold_marker_disarms_catchup_then_next_turn_rearms(tmp_path):
    """(c) Cold start with the marker set DISARMS the recovered turn's catch-up
    (held prose re-renders as ordinary narration); the marker clears at that
    turn's ``result`` boundary and a SUBSEQUENT anchor turn arms suppression
    again (its trailing prose is buffered and discarded).

    Uses a MIXED anchor+text frame BEYOND ``current`` so the anchor re-executes
    LIVE on catch-up (the durable anchor makes the seam report open) — DISARM,
    not replay-suppression of the candidate, is what makes the prose render."""
    offs = _write_current(tmp_path, [
        _init("s1"), _mixed_anchor_text("Q1?", "held prose"), _result(),
        _init("s2"), _mixed_anchor_text("Q2?", "turn2 prose"), _result(),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    # Recover turn 1 mid-flight: the marker is set and the checkpoint is held
    # BEFORE the mixed frame (through init1), so the mixed anchor+text frame
    # re-executes LIVE on recovery.
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[0]},
        message_ids=[],
        hold_pending=True,
    ).save(cursor)

    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (5, 500, _ah("Q2?")),   # both anchors open all along
    )
    await relay.run()

    # Turn 1's held prose RESURFACED (disarmed catch-up); turn 2's prose was
    # buffered under its own anchor and DISCARDED (re-armed) — never on the wire.
    assert _narration_sends(rec) == ["held prose"]
    assert StreamCursor.load(cursor).hold_pending is False


# --- RED (d): SEGMENT_GAP with the marker set — terminal recovery floor -------


async def test_segment_gap_with_marker_clears_and_next_turn_arms(tmp_path, caplog):
    """(d) A retention gap rotated out the held turn's frames. On ``SEGMENT_GAP``
    with the marker set: log the lost-source residual, durably CLEAR the marker,
    and re-arm — a fresh anchor turn in ``current`` arms suppression (its prose
    is buffered and discarded)."""
    _write_current(tmp_path, [
        _init(), _anchor_ask("Q?"), _text("new prose"), _result(),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    # turn_start points at a segment NO LONGER on disk (rotated out) with the
    # held-frames marker set.
    StreamCursor(
        turn_start={"segment": [999, 999], "offset": 0},
        current={"segment": [999, 999], "offset": 40},
        message_ids=[],
        hold_pending=True,
    ).save(cursor)

    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (5, 500, _ah()),
    )
    with caplog.at_level(logging.WARNING):
        await relay.run()

    assert any("retention gap" in r.message for r in caplog.records)
    assert any("buffered prose" in r.message for r in caplog.records)  # residual
    assert StreamCursor.load(cursor).hold_pending is False   # durably cleared
    assert rec.sends == []   # the fresh anchor turn RE-ARMED → prose discarded


# --- RED (e): abnormal spawn (spawn without result) — warm + cold + crash -----


async def test_warm_abnormal_spawn_flushes_before_checkpoint(tmp_path):
    """(e-warm) A non-empty buffer at an abnormal ``spawn`` boundary (no
    preceding ``result``) FLUSHES the held prose as ordinary narration BEFORE
    checkpointing the spawn, delivers the spawn event, resets suppression, and
    clears the marker — all in one live run (in-memory buffer survives)."""
    _write_current(tmp_path, [
        _init(), _anchor_ask("Q?"), _text("held"), _spawn(9),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (5, 500, _ah()),   # anchor open ⇒ "held" is buffered
    )
    await relay.run()

    assert _narration_sends(rec) == ["held"]                # flushed at the spawn
    assert ("spawn", {"epoch": 9}) in events                # event delivered
    assert relay._suppressing_for is None                   # suppression reset
    assert relay._anchor_buffer == []
    assert StreamCursor.load(cursor).hold_pending is False   # cleared


async def test_cold_abnormal_spawn_clears_marker_unconditionally(tmp_path):
    """(e-cold) A cold recovery renders the held prose immediately (its in-memory
    buffer stays EMPTY), so the abnormal ``spawn`` boundary hits with an empty
    buffer — yet the marker clears UNCONDITIONALLY (Sol r8-2). A subsequent
    anchor turn (appended + resumed) then arms suppression, proving the disarm
    latch did not leak past the spawn.

    Uses a MIXED anchor+text frame BEYOND ``current`` so DISARM (not replay-
    suppression of the candidate) is what empties the buffer before the spawn."""
    offs = _write_current(tmp_path, [
        _init("s1"), _mixed_anchor_text("Q1?", "held"), _spawn(9),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[0]},   # held BEFORE the mixed frame
        message_ids=[],
        hold_pending=True,
    ).save(cursor)

    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (5, 500, _ah("Q2?")),
    )
    await relay.run()

    assert _narration_sends(rec) == ["held"]                 # resurfaced (disarmed)
    assert ("spawn", {"epoch": 9}) in events
    # Empty buffer at the abnormal boundary, yet the marker cleared regardless.
    assert StreamCursor.load(cursor).hold_pending is False

    # Continue into a subsequent anchor turn (warm resume) — suppression ARMS.
    _append_current(tmp_path, [
        _init("s2"), _anchor_ask("Q2?"), _text("turn2 prose"), _result(),
    ])
    await relay.run()
    assert _narration_sends(rec) == ["held"]   # "turn2 prose" armed → discarded


async def test_abnormal_spawn_crash_between_event_and_checkpoint_redelivers(
    tmp_path,
):
    """(e-crash) EVENT-FIRST ordering (Sol r9-3): the spawn event is delivered
    BEFORE the checkpoint. A crash injected between them (the event handler
    raises after recording) leaves the spawn un-checkpointed; a brand-new relay
    RE-DELIVERS the spawn event on recovery — at-least-once, the relay's
    existing contract."""
    offs = _write_current(tmp_path, [
        _init("s1"), _anchor_ask("Q1?"), _text("held"), _spawn(9),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[1]},
        message_ids=[],
        hold_pending=True,
    ).save(cursor)

    # First relay: crash the instant the spawn event is delivered (after it is
    # recorded), before the checkpoint+clear ``_save`` can run.
    events1: list = []

    def crashing_on_event(kind, payload):
        events1.append((kind, payload))
        if kind == "spawn":
            raise RuntimeError("crash between event and checkpoint")

    rec1 = Recorder()
    seq1, _c1 = _fast_sequencer(rec1)
    relay1 = _make_relay(
        tmp_path, cursor, rec1, [], sequencer=seq1,
        open_anchor_state=lambda: (5, 500, _ah("Q1?")),
    )
    relay1.on_turn_event = crashing_on_event
    with pytest.raises(RuntimeError):
        await relay1.run()

    assert ("spawn", {"epoch": 9}) in events1                # delivered pre-crash
    # The spawn was NOT checkpointed (crash before the save): its frame is still
    # beyond ``current``, so a recovery re-reads and re-delivers it.
    assert StreamCursor.load(cursor).current["offset"] < offs[3]

    # Brand-new relay recovers and RE-DELIVERS the spawn event (at-least-once).
    rec2, events2 = Recorder(), []
    seq2, _c2 = _fast_sequencer(rec2)
    relay2 = _make_relay(
        tmp_path, cursor, rec2, events2, sequencer=seq2,
        open_anchor_state=lambda: (5, 500, _ah("Q1?")),
    )
    await relay2.run()

    assert ("spawn", {"epoch": 9}) in events2                # RE-DELIVERED
    assert StreamCursor.load(cursor).hold_pending is False   # cleared on recovery


# ===========================================================================
# wb6-1 (whole-branch gate wave 6): a FAILED flush (every send drops after
# ``_DROP_THRESHOLD`` Telegram failures) must NOT lose the held prose. The flush
# reports failure; the caller RETAINS the buffer + the ``hold_pending`` marker +
# an UNADVANCED replay boundary (no checkpoint past the held frames) and defers
# to a cold restart that resurfaces the prose (§D5 resurface-never-lose). Real
# primitives + real temp NDJSON + injected clocks; ``asyncio.sleep`` never
# patched (the drop backoff runs under the relay's injected ``_no_sleep``).
# ===========================================================================


async def test_abnormal_spawn_failed_flush_retains_for_resurface(tmp_path):
    """(wb6-1, abnormal-spawn path) The buffer is non-empty at an abnormal
    ``spawn`` boundary and every flush send FAILS (drop mode). The spawn must NOT
    be checkpointed-with-clear: the marker stays set, the checkpoint stays behind
    the held frame, and a brand-new relay (Telegram healthy) resurfaces the prose
    and only THEN delivers the spawn + clears the marker."""
    offs = _write_current(tmp_path, [
        _init(), _anchor_ask("Q?"), _text("held"), _spawn(9),
    ])
    cursor = tmp_path / ".stream_cursor.json"

    # First relay: "held" is buffered (anchor open); the flush at the spawn fails
    # persistently → drop. Nothing reaches the wire; nothing is checkpointed away.
    rec1, events1 = Recorder(), []
    rec1.send_fails = 10_000                       # every send fails forever → drop
    seq1, _c1 = _fast_sequencer(rec1)
    relay1 = _make_relay(
        tmp_path, cursor, rec1, events1, sequencer=seq1,
        open_anchor_state=lambda: (5, 500, _ah()),
    )
    await relay1.run()

    assert rec1.sends == []                         # never delivered (all failed)
    assert ("spawn", {"epoch": 9}) not in events1   # spawn NOT delivered on failure
    assert relay1._warm is False                    # deferred → next poll cold
    saved = StreamCursor.load(cursor)
    assert saved.hold_pending is True               # marker RETAINED
    assert saved.current["offset"] == offs[1]       # checkpoint HELD behind "held"
    assert saved.current["offset"] < offs[2]

    # Brand-new relay, Telegram healthy — cold recovery resurfaces the prose,
    # then delivers the spawn + clears the marker (event-first, at-least-once).
    rec2, events2 = Recorder(), []
    seq2, _c2 = _fast_sequencer(rec2)
    relay2 = _make_relay(
        tmp_path, cursor, rec2, events2, sequencer=seq2,
        open_anchor_state=lambda: (5, 500, _ah()),
    )
    await relay2.run()

    assert _narration_sends(rec2) == ["held"]        # RESURFACED
    assert ("spawn", {"epoch": 9}) in events2         # spawn delivered on recovery
    assert StreamCursor.load(cursor).hold_pending is False
    assert relay2.cursor.current["offset"] == offs[3]  # advanced past the spawn


async def test_answered_flush_failure_retains_for_resurface(tmp_path):
    """(wb6-1, answered-before-result path) The operator answers during the turn,
    so ``result`` FLUSHES the held prose — but every flush send FAILS (drop). The
    buffer must be RETAINED (not discarded), the closed-turn checkpoint must NOT
    advance, and the marker must stay set so a cold restart resurfaces it."""
    seam_state = {"open": (9, 900, _ah())}
    seam = lambda: seam_state["open"]  # noqa: E731
    _write_current(tmp_path, [_init(), _anchor_ask(), _text("bye")])
    cursor = tmp_path / ".stream_cursor.json"

    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq, open_anchor_state=seam,
    )
    await relay.run()                       # buffers "bye" (held), marker set
    assert rec.sends == []
    assert relay._suppressing_for == (9, 900)
    assert StreamCursor.load(cursor).hold_pending is True
    held_current = StreamCursor.load(cursor).current["offset"]

    # The operator ANSWERS; ``result`` arrives. The result-time flush FAILS.
    seam_state["open"] = None
    rec.send_fails = 10_000
    _append_current(tmp_path, [_result()])
    await relay.run()                       # WARM re-entry; flush fails → defer

    assert rec.sends == []                  # nothing delivered
    assert relay._warm is False             # deferred → next poll cold
    saved = StreamCursor.load(cursor)
    assert saved.hold_pending is True       # marker RETAINED (not cleared at result)
    assert saved.current["offset"] == held_current   # checkpoint NOT advanced

    # Brand-new relay, Telegram healthy — cold recovery resurfaces "bye".
    rec2, events2 = Recorder(), []
    seq2, _c2 = _fast_sequencer(rec2)
    relay2 = _make_relay(
        tmp_path, cursor, rec2, events2, sequencer=seq2,
        open_anchor_state=lambda: None,     # answered — disarmed catch-up renders
    )
    await relay2.run()

    assert _narration_sends(rec2) == ["bye"]         # RESURFACED
    assert StreamCursor.load(cursor).hold_pending is False


# ===========================================================================
# wb7-1 (whole-branch gate wave 7): the SHARED tool-use flush path
# (``_flush_and_disarm`` at a subsequent ``tool_use``) must obey the same
# wave-6 resurface-never-lose contract. When the held-prose flush hits drop
# mode the flush reports failure; the tool-use path must NOT swallow it and
# ``_advance_dropped`` past the held frame (which set ``dropped_through`` and
# made cold recovery SKIP the held frames despite ``hold_pending=True``).
# Instead it defers (``_FlushDeferred``) BEFORE disarming or advancing the
# cursor/``dropped_through``, so a cold restart resurfaces the prose. Both the
# cold-first-run and warm-re-entry tool-use flush-failure shapes, mirroring the
# wb6-1 spawn/answered tests.
# ===========================================================================


async def test_tool_use_failed_flush_retains_for_resurface(tmp_path):
    """(wb7-1, tool_use-flush path, COLD) A ``tool_use`` FLUSHES the held buffer
    (post-tool prose is legitimate) and every flush send FAILS (drop mode). The
    flush must NOT disarm suppression or advance ``current``/``dropped_through``
    past the held frame: the buffer + ``hold_pending`` marker + UNADVANCED
    boundary are RETAINED and a brand-new relay (Telegram healthy) resurfaces the
    prose. Before the fix ``_flush_and_disarm`` swallowed the failure and
    ``_handle_assistant_blocks`` ``_advance_dropped`` set ``dropped_through`` past
    the held frame, so cold recovery skipped it (held prose lost)."""
    offs = _write_current(tmp_path, [
        _init(), _anchor_ask("Q?"), _text("held"),
        _tool_in("Bash", {"command": "ls"}), _result(),
    ])
    cursor = tmp_path / ".stream_cursor.json"

    # First relay: "held" is buffered (anchor open); the flush at the tool_use
    # fails persistently → drop. Nothing reaches the wire; nothing checkpoints away.
    rec1, events1 = Recorder(), []
    rec1.send_fails = 10_000                       # every send fails forever → drop
    seq1, _c1 = _fast_sequencer(rec1)
    relay1 = _make_relay(
        tmp_path, cursor, rec1, events1, sequencer=seq1,
        open_anchor_state=lambda: (5, 500, _ah()),
    )
    await relay1.run()

    assert rec1.sends == []                         # never delivered (all failed)
    assert relay1._warm is False                    # deferred → next poll cold
    saved = StreamCursor.load(cursor)
    assert saved.hold_pending is True               # marker RETAINED
    assert saved.dropped_through is None            # NOT diverted past the held frame
    assert saved.current["offset"] == offs[1]       # checkpoint HELD behind "held"
    assert saved.current["offset"] < offs[2]

    # Brand-new relay, Telegram healthy — cold recovery resurfaces the prose.
    rec2, events2 = Recorder(), []
    seq2, _c2 = _fast_sequencer(rec2)
    relay2 = _make_relay(
        tmp_path, cursor, rec2, events2, sequencer=seq2,
        open_anchor_state=lambda: (5, 500, _ah()),
    )
    await relay2.run()

    assert _narration_sends(rec2) == ["held"]        # RESURFACED
    assert StreamCursor.load(cursor).hold_pending is False


async def test_tool_use_failed_flush_warm_retains_for_resurface(tmp_path):
    """(wb7-1, tool_use-flush path, WARM re-entry) The buffer is held on the first
    poll (warm-latched); a later ``tool_use`` arrives and the SAME relay resumes
    WARM. Its flush FAILS (drop). The warm path must RETAIN the buffer + marker,
    leave the checkpoint behind the held frame, keep ``dropped_through`` unset, and
    drop the warm latch so a cold restart resurfaces the prose."""
    _write_current(tmp_path, [_init(), _anchor_ask("Q?"), _text("held")])
    cursor = tmp_path / ".stream_cursor.json"

    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (5, 500, _ah()),
    )
    await relay.run()                       # buffers "held", marker set, warm-latched
    assert rec.sends == []
    assert relay._suppressing_for == (5, 500)
    assert StreamCursor.load(cursor).hold_pending is True
    held_current = StreamCursor.load(cursor).current["offset"]

    # A tool_use arrives; the SAME relay resumes WARM. The flush FAILS.
    rec.send_fails = 10_000
    _append_current(tmp_path, [_tool_in("Bash", {"command": "ls"}), _result()])
    await relay.run()                       # WARM re-entry; flush fails → defer

    assert rec.sends == []                  # nothing delivered
    assert relay._warm is False             # deferred → next poll cold
    saved = StreamCursor.load(cursor)
    assert saved.hold_pending is True       # marker RETAINED
    assert saved.dropped_through is None    # NOT diverted past the held frame
    assert saved.current["offset"] == held_current   # checkpoint NOT advanced

    # Brand-new relay, Telegram healthy — cold recovery resurfaces "held".
    rec2, events2 = Recorder(), []
    seq2, _c2 = _fast_sequencer(rec2)
    relay2 = _make_relay(
        tmp_path, cursor, rec2, events2, sequencer=seq2,
        open_anchor_state=lambda: (5, 500, _ah()),
    )
    await relay2.run()

    assert _narration_sends(rec2) == ["held"]        # RESURFACED
    assert StreamCursor.load(cursor).hold_pending is False


# ===========================================================================
# wb3-2 (whole-branch gate wave 3): terminal narration DISCARD is revalidated
# INSIDE the sequencer writer lock (one truth source = the terminal latch), so
# neither a write blocked on the lock at the instant of terminalization nor a
# throttled/unposted suffix at ``_finalize`` can land BELOW the terminal
# completion. Both of Sol's forced probes, adapted to the real ``terminalize``
# latch + a seam wired to ``seq.is_terminal()``.
# ===========================================================================


async def test_terminal_discards_narration_blocked_on_writer_lock(tmp_path):
    """Probe 1 (writer-lock inversion): a relay narration write BLOCKED on the
    sequencer writer lock when the engagement terminalizes must be DISCARDED
    inside the lock — never posted below the terminal completion. Before the
    fix the write's outside-lock terminal check passed, then it acquired the
    lock and posted ``['terminal completion', 'late narration']``."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    relay = _make_relay(
        tmp_path, tmp_path / ".stream_cursor.json", rec, events,
        sequencer=seq, engagement_terminal=lambda: seq.is_terminal(),
    )
    async with seq.serialized():
        # The relay's narration write blocks INSIDE open_narration on the writer
        # lock this task holds.
        late = asyncio.create_task(relay._append_narration("late narration", [], 0))
        await asyncio.sleep(0)
        assert not late.done()                      # parked on the writer lock
        # Terminalize (reentrant under the held lock), then post the completion.
        await seq.terminalize()
        await seq.post_completion_notice("terminal completion")
    await late

    assert _narration_sends(rec) == ["terminal completion"]
    assert "late narration" not in _narration_sends(rec)


async def test_terminal_finalize_discards_unposted_suffix(tmp_path):
    """Probe 2 (throttled suffix at finalize): a never-posted narration suffix
    must NOT be reposted as a NEW message below the terminal completion at
    ``_finalize`` once terminal. Before the fix ``_finalize`` saw the narration
    SEALED (by the completion) and reposted the unposted tail →
    ``['prefix', 'terminal completion', ' LATE']``."""
    rec, events = Recorder(), []
    seq, _clock = _fast_sequencer(rec)
    relay = _make_relay(
        tmp_path, tmp_path / ".stream_cursor.json", rec, events,
        sequencer=seq, engagement_terminal=lambda: seq.is_terminal(),
    )
    mid = await seq.open_narration("prefix")
    # Model a throttled hold: the wire shows "prefix" (6 chars) but the relay
    # holds an unposted " LATE" suffix in ``_per_message_text``.
    relay.cursor.message_ids = [mid]
    relay.cursor.message_text_lens = [6]
    relay._per_message_text = "prefix LATE"
    relay._posted_len = 6

    await seq.terminalize()
    await seq.post_completion_notice("terminal completion")
    await relay._finalize([], 1)

    assert _narration_sends(rec) == ["prefix", "terminal completion"]
    assert " LATE" not in _narration_sends(rec)


async def test_terminal_latch_discards_via_sequencer_writers(tmp_path):
    """The latch is the ONE truth source consulted inside the locked writers:
    once terminalized, ``open_narration`` / ``edit_narration_if_latest`` return
    DISCARDED and ``post_unless_anchor_open`` holds — directly, with no relay.

    Pins INV-ENG-005. Red case demonstrated: disabling _post_intent_locked's
    terminal-latch fail-closed branch (`if False:`) fails this test."""
    from channels.output_sequencer import DISCARDED
    rec, _clock = Recorder(), None
    seq, _clock = _fast_sequencer(rec)
    mid = await seq.open_narration("live")
    assert isinstance(mid, int)
    await seq.terminalize()
    assert seq.is_terminal() is True
    assert await seq.open_narration("after") == DISCARDED
    assert await seq.edit_narration_if_latest(mid, "edited") == DISCARDED
    # The completion seam STILL posts the terminal message; a plain platform
    # notice would now be DISCARDED under the latch (wb4-2).
    assert await seq.post_platform_notice("discarded notice") is None
    assert await seq.post_completion_notice("completion") is not None
    assert _narration_sends(rec) == ["live", "completion"]
