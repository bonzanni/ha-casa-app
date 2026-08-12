"""Round-3 F-DUP tests for ``drivers.topic_stream`` — the COLD-path half of the
A1 fix (design §A1, spec ``2026-07-15-engagement-ux-round3-design.md``).

Task 1 (A1a) makes ``cursor.last_posted_len`` load-bearing for the FIRST time:
the delta-aware cold ``_reconcile`` reposts only the genuinely-unposted tail
past the PERSISTED wire high-water, and every checkpoint site persists the WIRE
truth (``_posted_len``) rather than the in-memory ``len(_per_message_text)``
(which can include a throttled, never-posted suffix).

Each test drives a REAL ``OutputSequencer`` (constructed inside ``TopicStreamRelay``
with fake async send/edit recorders that return incrementing message ids) over a
REAL temp NDJSON log dir — never a mock of the sequencer. Time is injected
(``edit_throttle`` + a no-op ``_sleep``); we never patch ``asyncio.sleep``.
"""
from __future__ import annotations

import json
import os

import pytest

from channels.output_sequencer import REPLY_TOOL, projection_hash
from drivers.topic_stream import StreamCursor
from test_topic_stream import (
    Recorder,
    _ident,
    _init,
    _make_relay,
    _reply_tool_frame,
    _result,
    _text,
    _tool,
    _write_current,
)

pytestmark = pytest.mark.asyncio


def _append_current(log_dir, frames) -> None:
    """Append NDJSON *frames* to ``<log_dir>/current`` (models a live poll)."""
    path = os.path.join(str(log_dir), "current")
    with open(path, "ab") as fh:
        for fr in frames:
            fh.write(json.dumps(fr).encode("utf-8") + b"\n")


# ---------------------------------------------------------------------------
# spec §A1 RED test 4 — delta-aware cold reconcile posts only the unposted tail.
# ---------------------------------------------------------------------------


async def test_cold_reconcile_sealed_posts_only_unposted_tail(tmp_path):
    """§A1(2): a FRESH relay object recovers an OPEN turn whose narration was
    sealed by a discrete post before a crash. The persisted ``last_posted_len``
    records that only the PREFIX reached the wire (a lost-before-persist edit
    carried the rest). The delta-aware reconcile must repost ONLY the genuinely-
    unposted tail as a NEW message — never the already-visible prefix (the
    production F-DUP reposted the whole tail) — and adopt the delta message
    exactly like ``_execute_ops``' sealed branch.

    Log: init → "hello " → "world" (together "hello world") → a discrete reply
    frame (sealed narration live). Crash-before-persist: only "hello " (6 chars)
    was confirmed on the wire; the edit to "hello world" landed on Telegram but
    the cursor save was lost.
    """
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [
        _init(), _text("hello "), _text("world"), _reply_tool_frame("R"),
    ])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[-1]},  # everything replayed
        message_ids=[7],
        last_posted_len=len("hello "),  # only the prefix reached the wire
    ).save(cur_path)

    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()

    # ONLY the unposted tail "world" is reposted — never the visible "hello ".
    assert rec.sends == [(42, "world")]
    assert all(mid != 7 for _t, mid, _x in rec.edits)  # never edits below-content id
    # §A1(2) REPLAY-CONVERGENT adoption (Sol A1 review): APPEND the delta message
    # (mirroring ``_execute_ops``' sealed branch) and FREEZE the old current
    # message's boundary at the wire truth (``last_posted_len`` == len("hello ")).
    # ``turn_start`` still covers the full turn, so the persisted boundaries must
    # re-split the reconstructed "hello world" as ["hello ", "world"].
    assert relay.cursor.message_ids == [7, 1]  # old sealed msg + delta msg
    assert relay.cursor.message_text_lens == [len("hello "), len("world")]
    assert relay.cursor.last_posted_len == len("world")
    assert relay._posted_len == len("world")
    assert relay._per_message_text == "world"


async def test_cold_reconcile_delta_converges_across_restarts(tmp_path):
    """§A1(2) BLOCKER (Sol A1 review): the delta repost must persist REPLAY-
    CONVERGENT state. ``turn_start`` still covers the FULL turn, so every cold
    restart re-reads and reconstructs the WHOLE narration and re-splits it at the
    persisted ``message_text_lens``. After the FIRST delta repost, a SECOND fresh
    relay over the SAME on-disk log+cursor must reconstruct ``_per_message_text ==
    pending`` and reconcile NOTHING (empty slice / no-op edit) — and a THIRD
    restart likewise. Pre-fix (boundaries REPLACED, turn_start unchanged) the
    reconstructed text mis-split and successive restarts re-posted overlapping
    fragments ("world", " world", …)."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [
        _init(), _text("hello "), _text("world"), _reply_tool_frame("R"),
    ])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[-1]},
        message_ids=[7],
        last_posted_len=len("hello "),
    ).save(cur_path)

    # FIRST restart: posts only the unposted "world" delta and saves convergent
    # boundaries.
    r1 = _make_relay(tmp_path, cur_path, rec, events)
    await r1.run()
    assert [t for _tp, t in rec.sends] == ["world"]
    assert r1.cursor.message_ids == [7, 1]
    assert r1.cursor.message_text_lens == [len("hello "), len("world")]
    # _sync_last_len interaction: it retargets ONLY the LAST entry, so the frozen
    # prefix boundary survives a subsequent live-path normalization.
    r1._sync_last_len()
    assert r1.cursor.message_text_lens[0] == len("hello ")

    # SECOND restart: fresh relay + sequencer over the same on-disk state. Replay
    # reconstructs "hello world", re-splits it at [6, 5] → _per_message_text ==
    # "world", and reconcile posts NOTHING (pending empty).
    rec2, events2 = Recorder(), []
    r2 = _make_relay(tmp_path, cur_path, rec2, events2)
    await r2.run()
    assert rec2.sends == []
    assert r2._per_message_text == "world"
    assert all(mid != 7 for _t, mid, _x in rec2.edits)

    # THIRD restart for good measure: still converged, still posts nothing.
    rec3, events3 = Recorder(), []
    await _make_relay(tmp_path, cur_path, rec3, events3).run()
    assert rec3.sends == []


async def test_cold_reconcile_no_repost_when_fully_posted(tmp_path):
    """§A1(2) core F-DUP win: when ``last_posted_len`` shows the FULL narration
    already reached the wire, the cold reconcile reposts NOTHING (empty pending)
    — the every-turn duplicate the old full-repost produced is gone. The below-
    content message id is never edited."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [
        _init(), _text("all posted narration"), _reply_tool_frame("R"),
    ])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[-1]},
        message_ids=[7],
        last_posted_len=len("all posted narration"),  # whole tail on the wire
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    assert rec.sends == []                              # nothing reposted
    assert all(mid != 7 for _t, mid, _x in rec.edits)  # never edits id 7


# ---------------------------------------------------------------------------
# spec §A1 RED test 6 — a checkpoint persists the WIRE high-water, not the
# in-memory length (which includes a throttled, never-posted suffix).
# ---------------------------------------------------------------------------


async def test_tool_frame_checkpoint_persists_wire_truth(tmp_path):
    """§A1 "Persist the TRUE wire high-water" (Sol r2-1a): a text frame posts a
    prefix, the next text frame is THROTTLE-HELD (advances ``_per_message_text``
    WITHOUT reaching the wire), then a tool-only assistant frame checkpoints. The
    persisted ``last_posted_len`` must equal the WIRE length (excludes the held
    suffix), and cold recovery must repost the held suffix exactly once.

    Frozen clock + a wide throttle window: "AAA"→send, "AAABBB"→edit (posts,
    arms the window at t=0), "AAABBBCCC"→edit THROTTLED (held, unposted); then a
    tool-only ``Read`` frame checkpoints the wire truth (6), NOT the in-memory 9.
    """
    rec, events = Recorder(), []
    _write_current(tmp_path, [
        _init(), _text("AAA"), _text("BBB"), _text("CCC"), _tool("Read"),
    ])
    cur_path = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cur_path, rec, events, edit_throttle=10.0, _now=lambda: 0.0,
    )
    await relay.run()

    # "AAABBB" reached the wire (send + edit); "CCC" is throttle-held, unposted.
    assert relay._posted_len == 6
    assert relay._per_message_text == "AAABBBCCC"
    # The tool-only frame checkpointed the WIRE truth, not the in-memory length.
    saved = StreamCursor.load(cur_path)
    assert saved.last_posted_len == 6  # NOT 9 — excludes the held "CCC"

    # Cold recovery (FRESH relay + sequencer) reposts ONLY the held suffix once.
    rec2, events2 = Recorder(), []
    await _make_relay(tmp_path, cur_path, rec2, events2).run()
    assert [t for _tp, t in rec2.sends] == ["CCC"]
    assert sum(t == "CCC" for _tp, t in rec2.sends) == 1


# ---------------------------------------------------------------------------
# spec §A1(1) — WARM re-entry (the every-turn duplicate fix). The relay object
# survives the driver's 0.5s poll; a clean re-entry resumes IN PLACE from
# ``_read_coord`` (no cursor reload, no state reset, live, no replay/reconcile),
# so ``_reconcile`` is confined to genuine process/task restarts.
# ---------------------------------------------------------------------------


def _seal_via_discrete(relay, rec, text: str = "R", request_id: str = "d1") -> None:
    """Register + arm a reply intent on the SHARED sequencer and post it — the
    discrete post seals the open narration (as an ask/reply ingress would)."""
    h = projection_hash(REPLY_TOOL, {"text": text})

    async def poster():
        return await rec.send(42, f"[ask]{text}")

    relay.sequencer.register_intent(
        request_id=request_id, tool_name=REPLY_TOOL, projection_hash=h, poster=poster,
    )
    relay.sequencer.arm_intent(request_id)
    return h


async def test_warm_reentry_seal_then_new_frame_posts_only_delta(tmp_path):
    """§A1 RED 1: narration streams → ``run()`` returns at EOF (warm latched) →
    an ask intent posts through the SHARED sequencer (seals narration) → warm
    ``run()`` with ONE new text frame → the new message carries ONLY the delta;
    no wire message repeats the pre-seal narration."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _text("hello narration")])  # open turn
    cur_path = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()
    assert relay._warm is True
    assert [t for _tp, t in rec.sends] == ["hello narration"]

    h = _seal_via_discrete(relay, rec)
    assert await relay.sequencer.post_for_block(REPLY_TOOL, h) == "posted"

    _append_current(tmp_path, [_text(" more")])
    await relay.run()  # WARM re-entry

    assert [t for _tp, t in rec.sends] == ["hello narration", "[ask]R", " more"]
    assert sum("hello narration" in t for _tp, t in rec.sends) == 1
    assert all("hello narration" not in text for _tp, _mid, text in rec.edits)


async def test_warm_reentry_pure_poll_posts_nothing(tmp_path):
    """§A1 RED 2 (Sol A1 review — discriminating form): narration is SEALED via a
    discrete post through the SHARED sequencer BEFORE the pure poll, so the test
    actually exercises the warm-vs-cold difference. A per-poll cold reconcile
    would repost the whole sealed narration on this tick; warm re-entry (no
    replay, no reconcile) must post NOTHING. The prior form never sealed, so even
    a cold per-poll reconcile passed it via the identical-edit no-op gate."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _text("streaming body")])
    cur_path = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()
    assert relay._warm is True
    assert [t for _tp, t in rec.sends] == ["streaming body"]

    # SEAL the open narration with a discrete post through the shared sequencer.
    notice_id = await relay.sequencer.post_platform_notice("[notice]")
    assert notice_id is not None
    before = (list(rec.sends), list(rec.edits))

    # Sol A1 re-review MINOR: the wire assertions alone cannot discriminate the
    # warm skip from a cold pass whose delta reconcile computes an EMPTY pending
    # (last_posted_len == full length ⇒ posts nothing). Spy _reconcile so the
    # test pins the PATH: a pure warm poll must never reconcile at all.
    reconcile_calls = []
    real_reconcile = relay._reconcile

    async def _spy_reconcile():
        reconcile_calls.append(True)
        await real_reconcile()

    relay._reconcile = _spy_reconcile

    await relay.run()  # pure poll — nothing new on disk, narration now sealed

    assert (rec.sends, rec.edits) == before  # zero new sends/edits
    assert reconcile_calls == []  # warm path: no reconcile, not even a no-op one
    assert relay._warm is True


async def test_warm_reentry_inbound_seal_no_repost(tmp_path):
    """§A1 RED 3: ``advance_high_water_for_inbound`` seals between polls → the
    next warm ``run()`` never reposts the sealed narration; a new text frame
    opens a fresh message with only its delta."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _text("narr body")])
    cur_path = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()
    assert [t for _tp, t in rec.sends] == ["narr body"]

    await relay.sequencer.advance_high_water_for_inbound(999)  # inbound seal

    _append_current(tmp_path, [_text(" tail")])
    await relay.run()

    # Final-review pin: discriminate the WARM path — a delta-aware cold poll
    # would pass the wire assertions below identically.
    assert relay._warm is True
    assert [t for _tp, t in rec.sends] == ["narr body", " tail"]
    assert sum("narr body" in t for _tp, t in rec.sends) == 1
    assert all("narr body" not in text for _tp, _mid, text in rec.edits)


async def test_warm_invalidated_by_exception_next_run_cold(tmp_path):
    """§A1 RED 5: an exception escaping the warm frame loop clears ``_warm``, so
    the next ``run()`` takes the COLD path — observable via a reconcile that only
    the recovering cold path performs."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _text("body")])  # open turn persisted
    cur_path = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()
    assert relay._warm is True

    _append_current(tmp_path, [_text(" more")])

    async def _raising(*_a, **_k):
        raise RuntimeError("frame boom")

    relay._handle_frame = _raising
    with pytest.raises(RuntimeError):
        await relay.run()
    assert relay._warm is False  # warm latch invalidated

    del relay._handle_frame  # restore the bound method
    called = {"n": 0}
    orig = relay._reconcile

    async def _spy():
        called["n"] += 1
        await orig()

    relay._reconcile = _spy
    await relay.run()  # COLD (recovering) — reconcile fires
    assert called["n"] >= 1


async def test_replay_only_cold_run_seeds_read_coord_warm_resumes(tmp_path):
    """§A1 RED 7 (Sol r2-1b): a cold run that reaches EOF entirely in REPLAY must
    still seed ``_read_coord`` (from the last replayed frame) before latching
    warm; the next warm run resumes there without re-applying replayed frames."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [
        _init(), _text("recovered"), _reply_tool_frame("R"),
    ])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[-1]},  # all replayed, open turn
        message_ids=[7], last_posted_len=0,
    ).save(cur_path)
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()  # cold: entirely replay → reconcile at EOF

    assert rec.sends == [(42, "recovered")]  # conservative seal reposts once
    assert relay._warm is True
    assert relay._read_coord == {"segment": seg, "offset": offs[-1]}

    before = (list(rec.sends), list(rec.edits))
    await relay.run()  # warm poll — resumes at _read_coord, no re-apply
    assert (rec.sends, rec.edits) == before


async def test_zero_frame_closed_turn_latches_warm_from_current(tmp_path):
    """§A1 RED 6a (Sol r3-3): a zero-frame poll at EXACT EOF over a CLOSED-turn
    boundary (``message_ids == []``) latches warm, seeding ``_read_coord`` from
    ``cursor.current``."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("done"), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": offs[-1]},  # at EOF → zero frames
        current={"segment": seg, "offset": offs[-1]},
        message_ids=[],
    ).save(cur_path)
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()

    assert rec.sends == [] and rec.edits == []
    assert relay._warm is True
    assert relay._read_coord == {"segment": seg, "offset": offs[-1]}


async def test_zero_frame_open_turn_does_not_latch_warm(tmp_path):
    """§A1 RED 6b (Sol r3-3): a zero-frame run over an OPEN turn does NOT latch
    warm — the next poll runs cold again."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("open")])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": offs[-1]},  # at EOF → zero frames
        current={"segment": seg, "offset": offs[-1]},
        message_ids=[7],  # OPEN turn
    ).save(cur_path)
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()

    assert relay._warm is False
    assert relay._read_coord is None


async def test_gap_only_run_stays_cold(tmp_path):
    """§A1 RED 7 (Sol r3-3): a gap-only run (retention-gap sentinel, then zero
    real frames) does NOT latch warm — the sentinel never becomes a coordinate."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [])  # empty current
    cur_path = tmp_path / ".stream_cursor.json"
    StreamCursor(
        turn_start={"segment": [1234, 999999999], "offset": 0},  # phantom → gap
        current={"segment": [1234, 999999999], "offset": 0},
        message_ids=[],
    ).save(cur_path)
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()

    assert relay._warm is False
    assert relay._read_coord is None


async def test_absent_dropped_through_frames_flow_marker_preserved(tmp_path):
    """§A1(3) test (i) (Sol A1 review): a persisted ``dropped_through`` whose
    segment has rotated off disk ranks at infinity in ``_seg_rank``. The
    comparison-level absence rule in ``_coord_le`` returns False for such an
    absent target, so frames flow again — WITHOUT clearing the marker (the prior
    cold-start clear was racy and could erase a LIVE marker). The marker is
    PRESERVED, not mutated by a scan."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _text("flowing"), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": 0},
        message_ids=[],
        dropped_through={"segment": [1234, 999999999], "offset": 0},  # phantom
    ).save(cur_path)
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()

    assert relay.cursor.dropped_through is not None  # PRESERVED, never cleared
    assert [t for _tp, t in rec.sends] == ["flowing"]


async def test_present_dropped_through_still_honors_skip(tmp_path):
    """§A1(3) test (ii) (Sol A1 review): when the ``dropped_through`` marker's
    segment IS present on disk, the drop-tail skip is honored exactly as before
    (existing behavior). A whole-turn drop marker at the terminal coordinate
    suppresses re-post of every covered frame."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("dropped"), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[-1]},
        message_ids=[],
        dropped_through={"segment": seg, "offset": offs[-1]},  # present, terminal
    ).save(cur_path)
    await _make_relay(tmp_path, cur_path, rec, events).run()

    assert rec.sends == []  # covered frames stay dropped
    assert rec.edits == []


async def test_warm_aging_marker_segment_removed_frames_flow(tmp_path):
    """§A1(3) test (iii) (Sol A1 review) — the WARM-path fix. The relay goes warm
    with a LIVE ``dropped_through`` marker; the marker's segment is then removed
    from disk (rotated out) while warm. The next warm poll's new frames must still
    flow. Pre-fix, the warm path's ``_coord_le`` ranked the now-absent marker
    segment at infinity and muted EVERY subsequent frame forever."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _text("A")])
    cur_path = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()  # cold → posts "A", latches warm
    assert relay._warm is True
    assert [t for _tp, t in rec.sends] == ["A"]

    # A live marker whose segment then rotates off disk while warm (modeled by a
    # phantom absent segment): the resume coordinate stays on the present current
    # segment, but the marker segment is gone.
    relay.cursor.dropped_through = {"segment": [1234, 999999999], "offset": 0}

    _append_current(tmp_path, [_text("B")])
    await relay.run()  # WARM poll — the absent marker must NOT mute "B"

    assert relay._per_message_text == "AB"  # "B" flowed (edit), not muted
    assert rec.edits[-1] == (42, 1, "AB")


async def test_warm_throttled_hold_flushes_no_reread_duplication(tmp_path):
    """§A1 Sol r1-1: warm re-entry resumes from ``_read_coord`` (past the
    throttle-held frame), NOT from the behind-sitting ``cursor.current``. The
    held suffix stays in memory and flushes on the next edit — never re-read and
    duplicated (the very bug class A1 fixes)."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _text("AAA"), _text("BBB"), _text("CCC")])
    cur_path = tmp_path / ".stream_cursor.json"
    clock = {"t": 0.0}
    relay = _make_relay(
        tmp_path, cur_path, rec, events, edit_throttle=1.0, _now=lambda: clock["t"],
    )
    await relay.run()

    # AAA sent, BBB edited (arms the window at t=0), CCC edit THROTTLE-HELD.
    assert relay._posted_len == 6
    assert relay._per_message_text == "AAABBBCCC"
    assert [t for _tp, t in rec.sends] == ["AAA"]
    assert relay._warm is True

    clock["t"] = 10.0  # past the throttle window
    _append_current(tmp_path, [_text("DDD")])
    await relay.run()  # WARM — the held "CCC" flushes with "DDD"

    assert relay._per_message_text == "AAABBBCCCDDD"  # not "AAABBBCCCCCCDDD"
    assert rec.edits[-1] == (42, 1, "AAABBBCCCDDD")
    assert [t for _tp, t in rec.sends] == ["AAA"]  # no re-send / duplication


# ---------------------------------------------------------------------------
# #523: crash between the closing narration edit and the closed-turn save.
# ---------------------------------------------------------------------------


async def test_wire_hw_marker_prevents_suffix_repost_after_crash(tmp_path):
    """#523 (the fix): the closing edit LANDED (wire carries "hello suffix")
    and the wire-high-water marker was saved, but the process died before the
    closed-turn checkpoint. Cold recovery must repost NOTHING: the suffix
    frame (past the persisted ``current``) reconstructs text-only, the marker
    adopts the landed high-water at the result frame, and the re-run
    finalize's sealed re-plan computes an empty tail."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [
        _init(), _text("hello"), _text(" suffix"), _result(),
    ])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[1]},   # through "hello"
        message_ids=[7],
        message_text_lens=[len("hello")],
        last_posted_len=len("hello"),                  # suffix was throttled
        wire_hw={
            "coord": {"segment": seg, "offset": offs[3]},
            "len": len("hello suffix"),
        },
    ).save(cur_path)

    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()

    assert rec.sends == []                      # the #523 dup was a send here
    assert all(mid != 7 for _t, mid, _x in rec.edits)
    assert ("result", {"subtype": "success"}) in events  # finalize re-ran
    cur = StreamCursor.load(cur_path)
    assert cur.wire_hw is None                  # cleared with the closed turn
    assert cur.message_ids == []                # closed-turn checkpoint saved


async def test_no_marker_keeps_at_least_once_suffix_delivery(tmp_path):
    """#523 contrast (crash BEFORE the closing edit landed): no marker, so
    the suffix must still be DELIVERED — the at-least-once contract; loss
    would be worse than the duplicate (§2(d))."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [
        _init(), _text("hello"), _text(" suffix"), _result(),
    ])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[1]},
        message_ids=[7],
        message_text_lens=[len("hello")],
        last_posted_len=len("hello"),
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    # The suffix reaches the wire (as a new message past the sealed 7).
    assert " suffix" in "".join(t for _tp, t in rec.sends)


async def test_finalize_persists_wire_hw_before_closed_turn_save(tmp_path):
    """#523 writer side, end-to-end: kill the run between the closing edit
    and the closed-turn save (drain_and_prune_turn raises) and verify the
    marker is durable with the landed length + the result coordinate — then
    verify a fresh relay recovers with NO duplicate posts."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [
        _init(), _text("hello"), _text(" suffix"), _result(),
    ])
    cur_path = tmp_path / ".stream_cursor.json"

    relay = _make_relay(tmp_path, cur_path, rec, events)
    real_drain = relay.sequencer.drain_and_prune_turn

    async def crashing_drain():
        raise RuntimeError("crash in the #523 window")

    relay.sequencer.drain_and_prune_turn = crashing_drain
    try:
        await relay.run()
    except RuntimeError:
        pass

    seg = _ident(os.path.join(str(tmp_path), "current"))
    cur = StreamCursor.load(cur_path)
    assert cur.wire_hw is not None
    assert cur.wire_hw["len"] == len("hello suffix")
    assert cur.wire_hw["coord"] == {"segment": seg, "offset": offs[3]}
    assert cur.message_ids == [1]               # closed-turn save never ran

    # Recovery: a FRESH relay object over the same state.
    sends_before = list(rec.sends)
    relay2 = _make_relay(tmp_path, cur_path, rec, events)
    await relay2.run()
    assert rec.sends == sends_before            # nothing reposted
    cur2 = StreamCursor.load(cur_path)
    assert cur2.wire_hw is None
    assert cur2.message_ids == []


async def test_stream_cursor_wire_hw_round_trip(tmp_path):
    p = tmp_path / "c.json"
    hw = {"coord": {"segment": [1, 2], "offset": 33}, "len": 12}
    StreamCursor(wire_hw=hw).save(p)
    assert StreamCursor.load(p).wire_hw == hw
    StreamCursor().save(p)
    assert StreamCursor.load(p).wire_hw is None
