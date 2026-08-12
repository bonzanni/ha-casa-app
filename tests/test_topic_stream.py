"""Tests for ``drivers.topic_stream`` — the NDJSON engagement-log → Telegram
topic relay (design §W1; Sol adversarial-review invariants B6/B7/B4/B5).

Each test drives a REAL temp file as the log and injects fake async
send/edit/delete recorders + an ``on_turn_event`` recorder, so the relay's
crash-safe segment cursor and at-least-once posting are exercised end-to-end.
Time is injected (``edit_throttle=0.0`` + a no-op ``_sleep``) — we never patch
``asyncio.sleep`` (the global-patch OOM lesson).
"""
from __future__ import annotations

import json
import logging
import os

from channels.output_sequencer import REPLY_TOOL, projection_hash
from drivers.topic_stream import (
    SEGMENT_GAP,
    StreamCursor,
    TopicStreamRelay,
    extract_text_blocks,
    is_mutating_tooluse,
    iter_content_blocks,
    iter_log_segments,
    parse_frame,
)


# ---------------------------------------------------------------------------
# Fakes + frame builders.
# ---------------------------------------------------------------------------


class Recorder:
    """Fake async Telegram senders that record every call."""

    def __init__(self) -> None:
        self.sends: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.deletes: list[tuple[int, int]] = []
        self._next_id = 1
        self.send_fails = 0  # first N send() calls raise
        self.edit_fails = 0  # first N edit() calls return False
        self.fail_edit_texts: dict[str, int] = {}  # per-text edit-fail counts

    async def send(self, topic_id: int, text: str) -> int | None:
        if self.send_fails > 0:
            self.send_fails -= 1
            raise RuntimeError("telegram send boom")
        self.sends.append((topic_id, text))
        mid = self._next_id
        self._next_id += 1
        return mid

    async def edit(self, topic_id: int, message_id: int, text: str) -> bool:
        if self.fail_edit_texts.get(text, 0) > 0:
            self.fail_edit_texts[text] -= 1
            return False
        if self.edit_fails > 0:
            self.edit_fails -= 1
            return False
        self.edits.append((topic_id, message_id, text))
        return True

    async def delete(self, topic_id: int, message_id: int) -> bool:
        self.deletes.append((topic_id, message_id))
        return True


async def _no_sleep(_seconds: float) -> None:
    return None


def _spawn(epoch: int) -> dict:
    return {"casa_control": "spawn", "epoch": epoch}


def _init(session_id: str = "sid-1") -> dict:
    return {"type": "system", "subtype": "init", "session_id": session_id}


def _text(*texts: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": t} for t in texts]},
    }


def _tool(name: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": name, "input": {}}]},
    }


def _thinking(t: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": t}]},
    }


def _ratelimit() -> dict:
    return {"type": "rate_limit_event"}


def _result(subtype: str = "success") -> dict:
    return {"type": "result", "subtype": subtype}


def _write_current(log_dir, frames) -> list[int]:
    """Write *frames* as NDJSON to ``<log_dir>/current``; return end offsets.

    ``frames`` items may be dicts (JSON-encoded), ``str`` (raw line, newline
    appended), or ``bytes`` (written verbatim). Returns the byte offset just
    after each written frame.
    """
    path = os.path.join(str(log_dir), "current")
    offsets: list[int] = []
    with open(path, "wb") as fh:
        for fr in frames:
            if isinstance(fr, bytes):
                fh.write(fr)
            elif isinstance(fr, str):
                fh.write(fr.encode("utf-8") + b"\n")
            else:
                fh.write(json.dumps(fr).encode("utf-8") + b"\n")
            offsets.append(fh.tell())
    return offsets


def _ident(path) -> list[int]:
    st = os.stat(path)
    return [st.st_dev, st.st_ino]


def _make_relay(log_dir, cursor_path, rec, events, reply_texts=None, **kw):
    kw.setdefault("edit_throttle", 0.0)
    kw.setdefault("_sleep", _no_sleep)
    return TopicStreamRelay(
        engagement_id="eng-1",
        topic_id=42,
        log_dir=str(log_dir),
        cursor_path=str(cursor_path),
        send_message=rec.send,
        edit_message=rec.edit,
        delete_message=rec.delete,
        on_turn_event=lambda kind, payload: events.append((kind, payload)),
        reply_texts=reply_texts or (lambda: set()),
        **kw,
    )


# ---------------------------------------------------------------------------
# Pure-function classifiers.
# ---------------------------------------------------------------------------


def test_parse_frame_json_and_garbage():
    assert parse_frame(b'{"a": 1}') == {"a": 1}
    assert parse_frame(b"not json at all") is None
    assert parse_frame(b"[1, 2, 3]") is None  # non-object


def test_extract_text_blocks_text_only():
    frame = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "tool_use", "name": "Edit", "input": {}},
                {"type": "thinking", "thinking": "secret reasoning"},
                {"type": "text", "text": "world"},
            ]
        },
    }
    assert extract_text_blocks(frame) == ["hello", "world"]
    assert extract_text_blocks(_tool("Read")) == []
    assert extract_text_blocks({"type": "result"}) == []


def test_is_mutating_tooluse_allowlist():
    assert is_mutating_tooluse(_tool("Edit")) == (True, "Edit")
    assert is_mutating_tooluse(_tool("Bash"))[0] is True
    assert is_mutating_tooluse(_tool("Read")) == (False, "")
    assert is_mutating_tooluse(_tool("mcp__casa-engagement-channel__reply")) == (
        False,
        "",
    )
    # Unknown mcp__* tool → mutating.
    assert is_mutating_tooluse(_tool("mcp__something__do_it")) == (
        True,
        "mcp__something__do_it",
    )
    assert is_mutating_tooluse(_text("hi")) == (False, "")


# ---------------------------------------------------------------------------
# Live streaming.
# ---------------------------------------------------------------------------


async def test_streams_assistant_text_single_edited_message(tmp_path):
    rec, events = Recorder(), []
    offs = _write_current(
        tmp_path, [_spawn(1), _init(), _text("Hello "), _text("world"), _result()]
    )
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    assert len(rec.sends) == 1  # exactly one message opened
    assert rec.sends[0][1] == "Hello "
    assert len(rec.edits) >= 1
    assert rec.edits[-1][2] == "Hello world"  # final == joined
    cur = StreamCursor.load(cursor)
    assert cur.current["offset"] == offs[-1]  # advanced to file size
    assert ("spawn", {"epoch": 1}) in events


async def test_ignores_tools_thinking_unknown_and_ratelimit(tmp_path):
    rec, events = Recorder(), []
    _write_current(
        tmp_path,
        [
            _init(),
            _tool("Read"),
            _thinking("private chain of thought"),
            _ratelimit(),
            "this is not json",
            _result(),
        ],
    )
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    assert rec.sends == []
    assert rec.edits == []


async def test_mutating_tooluse_emits_event_not_stream(tmp_path):
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _tool("Edit"), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    assert ("mutating_tool", {"tool": "Edit"}) in events
    assert rec.sends == []  # never streamed


async def test_rollover_past_3900_two_messages(tmp_path):
    rec, events = Recorder(), []
    a = "a" * 2500
    b = "b" * 2500  # 5000 total > 3900 → rolls to a second message
    _write_current(tmp_path, [_init(), _text(a), _text(b), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    assert len(rec.sends) == 2  # SEND recorder shows two messages
    for _topic, text in rec.sends:
        assert len(text) <= 3900


async def test_single_huge_block_splits_into_three_messages(tmp_path):
    rec, events = Recorder(), []
    huge = "x" * 10000  # one block → ceil(10000/3900) = 3 messages
    _write_current(tmp_path, [_init(), _text(huge), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    assert len(rec.sends) >= 3
    for _topic, text in rec.sends:
        assert len(text) <= 3900
    assert sum(len(t) for _tp, t in rec.sends) == 10000


async def test_open_midturn_checkpoint_tracks_message_ids(tmp_path):
    rec, events = Recorder(), []
    # Rollover to two messages, but NO result frame → turn stays open.
    _write_current(tmp_path, [_init(), _text("y" * 5000)])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cursor, rec, events)
    await relay.run()

    assert relay.cursor.message_ids == [1, 2]
    assert StreamCursor.load(cursor).message_ids == [1, 2]


async def test_rollover_final_chunk_posted_never_whole_turn(tmp_path):
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _text("m" * 5000), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    # v0.79.0: rollover posts the final chunk as its OWN message (a send); the
    # closing finalize edit is a no-op (the sequencer's F1 no-op gate skips it,
    # since the send already cached that exact text). No message ever carries
    # the whole 5000-char turn.
    assert len(rec.sends) == 2
    assert rec.sends[-1][1] == "m" * 1100  # last message = final chunk
    assert all(len(t) <= 3900 for _tp, t in rec.sends)
    assert rec.edits == []  # nothing to edit — the chunk was already posted


async def test_finalize_persists_closed_turn_checkpoint(tmp_path):
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("done"), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    cur = StreamCursor.load(cursor)
    assert cur.message_ids == []
    assert cur.turn_start == cur.current
    assert cur.current["offset"] == offs[-1]


async def test_finalize_edit_retries_before_closing_checkpoint(tmp_path):
    """B2 (Sol r1): the finalize edit must honor the at-least-once contract —
    a transient Telegram failure retries via the bounded backoff and the
    fragment IS delivered before the closed-turn checkpoint advances past
    ``result``.

    v0.79.0: the closing edit is a real wire edit ONLY when the last streaming
    edit was THROTTLED (held in memory, not posted). We force that with a
    frozen clock + a large throttle window: "a"→send, "ab"→edit (posts, arms
    the window), "abc"→edit (throttled, held); finalize flushes "abc" — the
    edit that ``edit_fails`` bites."""
    rec, events = Recorder(), []
    rec.fail_edit_texts = {"abc": 2}  # ONLY the flushed finalize edit fails
    offs = _write_current(
        tmp_path, [_init(), _text("a"), _text("b"), _text("c"), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(
        tmp_path, cursor, rec, events,
        edit_throttle=100.0, _now=lambda: 1000.0,
    ).run()

    # The final fragment landed (retried, not silently dropped).
    assert rec.edits and rec.edits[-1][2] == "abc"
    cur = StreamCursor.load(cursor)
    # Checkpoint closed ONLY after the edit succeeded — normal (non-drop) close.
    assert cur.message_ids == []
    assert cur.dropped_through is None
    assert cur.current["offset"] == offs[-1]


async def test_finalize_persistent_edit_failure_drops_once(tmp_path, caplog):
    """B2 (Sol r1): if the finalize edit fails persistently, the turn enters
    the documented drop path — warn ONCE, checkpoint closes via ``dropped_through``
    (no silent loss), and there is no infinite retry loop."""
    rec, events = Recorder(), []
    rec.fail_edit_texts = {"abc": 10_000}  # the flushed finalize edit never lands
    offs = _write_current(
        tmp_path, [_init(), _text("a"), _text("b"), _text("c"), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    with caplog.at_level(logging.WARNING):
        await _make_relay(
            tmp_path, cursor, rec, events,
            edit_throttle=100.0, _now=lambda: 1000.0,
        ).run()

    cur = StreamCursor.load(cursor)
    # Dropped through the terminal coordinate (not silently advanced).
    assert cur.dropped_through is not None
    assert cur.dropped_through["offset"] == offs[-1]
    assert cur.current["offset"] == offs[-1]
    assert cur.message_ids == []
    drop_warnings = [r for r in caplog.records if "dropping remainder" in r.message]
    assert len(drop_warnings) == 1  # exactly one WARNING


async def test_restart_after_turn_no_delete_clean_second_turn(tmp_path):
    # v0.79.0 (§2(d)): the reply de-dup DELETE is REMOVED — a duplicate is
    # preferred over erasing history. Turn 1's message is KEPT.
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("ping"), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    reply = lambda: {"ping"}
    await _make_relay(tmp_path, cursor, rec, events, reply_texts=reply).run()
    assert rec.deletes == []  # NEVER deleted (was [(42, 1)] pre-v0.79.0)
    assert rec.sends == [(42, "ping")]  # message kept
    assert StreamCursor.load(cursor).message_ids == []

    # Turn 2 appended; a NEW relay resumes from the closed checkpoint.
    with open(os.path.join(str(tmp_path), "current"), "ab") as fh:
        for fr in (_init("sid-2"), _text("second turn"), _result()):
            fh.write(json.dumps(fr).encode("utf-8") + b"\n")
    rec2, events2 = Recorder(), []
    rec2._next_id = 100  # turn 2's new message gets a fresh, distinct id
    await _make_relay(tmp_path, cursor, rec2, events2).run()

    # The second turn streams cleanly into its OWN new message (id 100), never
    # touching turn 1's message id (1).
    assert all(mid != 1 for _t, mid, _x in rec2.edits)
    assert rec2.sends and rec2.sends[0][1] == "second turn"
    assert StreamCursor.load(cursor).message_ids == []
    _ = offs


# ---------------------------------------------------------------------------
# Failure / drop.
# ---------------------------------------------------------------------------


async def test_cursor_advances_only_after_success(tmp_path):
    rec, events = Recorder(), []
    rec.edit_fails = 2  # first two edits fail, third succeeds
    _write_current(tmp_path, [_init(), _text("first"), _text("second")])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cursor, rec, events)
    await relay.run()

    # The send for msg 1 + the retried edit for the second text both landed;
    # the cursor advanced past the second text only after the edit succeeded.
    assert rec.sends[0][1] == "first"
    assert rec.edits[-1][2] == "firstsecond"
    saved = StreamCursor.load(cursor)
    assert saved.message_ids == [1]
    assert saved.last_posted_len == len("firstsecond")


async def test_persistent_failure_drops_through_terminal(tmp_path, caplog):
    rec, events = Recorder(), []
    rec.send_fails = 10_000  # every send fails forever
    offs = _write_current(tmp_path, [_init(), _text("a"), _text("b"), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    with caplog.at_level(logging.WARNING):
        await _make_relay(tmp_path, cursor, rec, events).run()

    cur = StreamCursor.load(cursor)
    assert cur.dropped_through is not None
    assert cur.dropped_through["offset"] == offs[-1]  # terminal coordinate
    assert cur.current["offset"] == offs[-1]
    assert rec.sends == []  # nothing ever posted
    drop_warnings = [r for r in caplog.records if "dropping remainder" in r.message]
    assert len(drop_warnings) == 1  # exactly one WARNING


# ---------------------------------------------------------------------------
# Recovery.
# ---------------------------------------------------------------------------


async def test_recovery_replays_turn_edits_last_id(tmp_path):
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("hello world"), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    # Mid-turn: message 7 posted, cursor checkpointed through the text frame.
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[1]},
        message_ids=[7],
        # A1a: last_posted_len is now load-bearing (delta reconcile). 0 pins the
        # LEGACY full-repost / conservative-seal path this test covers; the delta
        # (partial-tail) path is exercised in test_topic_stream_round3.py.
        last_posted_len=0,
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    # v0.79.0 (§2 sealing across restart, option B): the checkpoint-named
    # message is CONSERVATIVELY SEALED on recovery, so the reconciled state
    # posts as a NEW closing message (a send) rather than editing id 7. The
    # old message is never touched (accepting the documented duplicate risk).
    assert rec.sends == [(42, "hello world")]
    assert rec.edits == []
    assert all(mid != 7 for _t, mid, _x in rec.edits)


async def test_recovery_dropped_through_is_honored(tmp_path):
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("a"), _text("b"), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    # A previous run dropped the whole turn (dropped_through == terminal).
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[-1]},
        message_ids=[],
        dropped_through={"segment": seg, "offset": offs[-1]},
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    assert rec.sends == []  # no re-post of dropped bytes
    assert rec.edits == []


async def test_crash_after_post_before_persist_dup_send(tmp_path):
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("fragment")])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    # Simulate crash-after-post-before-persist: init was checkpointed but the
    # text frame's post landed WITHOUT the cursor being saved (message_ids empty,
    # current still at the init boundary).
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[0]},
        message_ids=[],
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    # The fragment is RE-SENT — documented at-least-once duplicate.
    assert [t for _tp, t in rec.sends] == ["fragment"]


async def test_recovery_replays_turn_edits_last_id_after_more_live_text(tmp_path):
    # Sanity companion: replay prefix + a live text frame past current.
    rec, events = Recorder(), []
    offs = _write_current(
        tmp_path, [_init(), _text("hello"), _text(" more"), _result()]
    )
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[1]},  # through "hello"
        message_ids=[7],
        last_posted_len=0,  # A1a: legacy full-repost path (see round3 for delta)
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    # Recovery seals id 7 → the reconciled "hello" reposts as a NEW message,
    # then the LIVE " more" grows THAT new message (not the sealed id 7).
    assert rec.sends == [(42, "hello")]
    assert rec.edits[-1] == (42, 1, "hello more")


# ---------------------------------------------------------------------------
# Segments: rotation / archive-span / gap.
# ---------------------------------------------------------------------------


async def test_rotation_drains_old_segment(tmp_path):
    log_dir = str(tmp_path)
    current = os.path.join(log_dir, "current")
    # First half in `current` (inode X).
    with open(current, "wb") as fh:
        fh.write(b'{"n": 1}\n{"n": 2}\n')

    gen = iter_log_segments(log_dir, {"segment": [0, 0], "offset": 0})
    first = await gen.__anext__()  # opens current(X), holds the fd

    # Rotate: current(X) → @a.s, new current(Y) with the second half.
    os.rename(current, os.path.join(log_dir, "@a.s"))
    with open(current, "wb") as fh:
        fh.write(b'{"n": 3}\n{"n": 4}\n')

    rest = [item async for item in gen]
    lines = [first[2]] + [r[2] for r in rest]
    ns = [json.loads(x)["n"] for x in lines]
    assert ns == [1, 2, 3, 4]  # BOTH halves seen via held-fd drainage


async def test_restart_turn_spans_archive_then_current(tmp_path):
    log_dir = str(tmp_path)
    # Archive @a.s holds the turn start (init + first text); current holds the rest.
    archive = os.path.join(log_dir, "@a.s")
    with open(archive, "wb") as fh:
        for fr in (_init(), _text("part-one ")):
            fh.write(json.dumps(fr).encode("utf-8") + b"\n")
    seg_arch = _ident(archive)
    current = os.path.join(log_dir, "current")
    with open(current, "wb") as fh:
        for fr in (_text("part-two"), _result()):
            fh.write(json.dumps(fr).encode("utf-8") + b"\n")

    rec, events = Recorder(), []
    cur_path = tmp_path / ".stream_cursor.json"
    StreamCursor(
        turn_start={"segment": seg_arch, "offset": 0},
        current={"segment": seg_arch, "offset": 0},  # nothing applied yet
        message_ids=[],
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    # Full turn recovered across the boundary; a single message carries it.
    assert len(rec.sends) == 1
    assert rec.edits[-1][2] == "part-one part-two"


async def test_cold_recovery_at_rotated_segment_eof_delivers_next_turn(tmp_path):
    # #300: the previous turn closed on the LAST frame of a segment that then
    # rotated — the closed-turn cursor (turn_start == current) sits at the
    # exact EOF of the archive. The next turn already began in the new
    # ``current`` before the restart. Recovery opens the archive at EOF, reads
    # zero frames from the cursor's own segment, and must STILL latch live for
    # the successor segment — not replay-mute the entire next turn.
    log_dir = str(tmp_path)
    archive = os.path.join(log_dir, "@a.s")
    with open(archive, "wb") as fh:
        for fr in (_spawn(1), _init(), _text("old turn"), _result()):
            fh.write(json.dumps(fr).encode("utf-8") + b"\n")
        eof = fh.tell()
    seg_arch = _ident(archive)
    _write_current(
        tmp_path, [_spawn(2), _init("sid-2"), _text("new turn"), _result()]
    )

    rec, events = Recorder(), []
    cur_path = tmp_path / ".stream_cursor.json"
    StreamCursor(
        turn_start={"segment": seg_arch, "offset": eof},
        current={"segment": seg_arch, "offset": eof},
        message_ids=[],
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    # The successor segment's whole turn is LIVE: spawn side effect fires and
    # the narration posts.
    assert ("spawn", {"epoch": 2}) in events
    assert rec.sends and rec.sends[0][1] == "new turn"


async def test_rollover_plus_restart_recovers_editing_final_id(tmp_path):
    rec, events = Recorder(), []
    # A >3900 turn spanning two messages (ids 7 and 9), persisted mid-turn.
    big = "z" * 5000
    offs = _write_current(tmp_path, [_init(), _text(big), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[1]},  # through the text frame
        message_ids=[7, 9],
        last_posted_len=0,  # A1a: legacy full-repost path (see round3 for delta)
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    # Recovery seals the final id (9) → the final chunk reposts as a NEW
    # message (never editing a message with content below it). Only the final
    # ≤3900 chunk is reposted, never the whole 5000-char turn.
    assert len(rec.sends) == 1
    assert rec.sends[0][1] == big[3900:]
    assert rec.edits == []


async def test_invisible_frames_checkpoint_and_no_spawn_replay(tmp_path):
    rec, events = Recorder(), []
    # spawn + init + a tool-only frame: no visible text, all invisible.
    offs = _write_current(tmp_path, [_spawn(1), _init(), _tool("Read")])
    cur_path = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cur_path, rec, events)
    await relay.run()

    assert [k for k, _p in events].count("spawn") == 1  # emitted once, live
    assert relay.cursor.current["offset"] == offs[-1]  # all advanced the cursor

    # Restart from the saved cursor → no spawn re-emitted (it precedes turn_start).
    rec2, events2 = Recorder(), []
    await _make_relay(tmp_path, cur_path, rec2, events2).run()
    assert [k for k, _p in events2].count("spawn") == 0


async def test_recovery_suppresses_inturn_checkpointed_spawn_side_effect(tmp_path):
    rec, events = Recorder(), []
    # Turn: init, text (posted), an in-turn RESPAWN control frame, a mutating
    # tool, then more text — ALL checkpointed (<= current).
    frames = [
        _init(),
        _text("alpha"),
        _spawn(2),  # abnormal in-turn respawn
        _tool("Edit"),  # in-turn mutating tool
        _text(" beta"),
    ]
    offs = _write_current(tmp_path, frames)
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[-1]},  # everything <= current
        message_ids=[5],
        last_posted_len=0,  # A1a: legacy full-repost path (see round3 for delta)
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    # Text state rebuilt → reconcile CONSERVATIVELY SEALS id 5 and reposts the
    # full text as a NEW message (§2 sealing across restart).
    assert rec.sends == [(42, "alpha beta")]
    assert rec.edits == []
    # NO side effects re-fired for the in-turn checkpointed frames.
    assert [k for k, _p in events].count("spawn") == 0
    assert [k for k, _p in events].count("mutating_tool") == 0


async def test_segment_gap_warns_and_resumes(tmp_path, caplog):
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("recovered"), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    # turn_start points at a segment that is NOT on disk (rotated out).
    StreamCursor(
        turn_start={"segment": [999, 999], "offset": 20},
        current={"segment": [999, 999], "offset": 40},
        message_ids=[],
    ).save(cur_path)

    with caplog.at_level(logging.WARNING):
        await _make_relay(tmp_path, cur_path, rec, events).run()

    assert any("retention gap" in r.message for r in caplog.records)
    # Resumed at current offset 0 → the whole live turn was processed.
    assert rec.sends and rec.sends[0][1] == "recovered"
    assert StreamCursor.load(cur_path).current["offset"] == offs[-1]


async def test_reply_dedup_never_deletes_keeps_message(tmp_path):
    # v0.79.0 (§2(d)): de-dup DELETE removed — the streamed message is KEPT
    # even when byte-identical to a reply already recorded (no deletes ever).
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _text("the answer is 42"), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    reply = lambda: {"the answer is 42"}
    await _make_relay(tmp_path, cur_path, rec, events, reply_texts=reply).run()

    assert rec.deletes == []  # never deleted
    assert rec.sends == [(42, "the answer is 42")]  # message kept


async def test_spawn_frame_emits_event(tmp_path):
    rec, events = Recorder(), []
    _write_current(tmp_path, [_spawn(3)])
    cur_path = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cur_path, rec, events).run()

    assert ("spawn", {"epoch": 3}) in events


# ---------------------------------------------------------------------------
# v0.79.0 (§2): relay-mediated discrete posting + rollover-on-interleave +
# crash/seal invariant.
# ---------------------------------------------------------------------------


def _reply_input(text: str) -> dict:
    return {"chat_id": "x", "text": text}


def _reply_tool_frame(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": REPLY_TOOL, "input": _reply_input(text)},
        ]},
    }


def _mixed_frame(before: str, reply_text: str, after: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": before},
            {"type": "tool_use", "name": REPLY_TOOL, "input": _reply_input(reply_text)},
            {"type": "text", "text": after},
        ]},
    }


def _arm_reply(relay, rec, text: str, request_id: str = "r1") -> None:
    """Register + arm a reply SEND INTENT on the relay's sequencer whose poster
    posts a distinguishable ``[reply]<text>`` marker via ``rec.send``."""
    h = projection_hash(REPLY_TOOL, {"text": text})

    async def poster():
        return await rec.send(42, f"[reply]{text}")

    relay.sequencer.register_intent(
        request_id=request_id, tool_name=REPLY_TOOL, projection_hash=h,
        poster=poster,
    )
    relay.sequencer.arm_intent(request_id)


def test_iter_content_blocks_preserves_order():
    blocks = iter_content_blocks(_mixed_frame("pre", "R", "post"))
    assert blocks == [
        ("text", "pre"),
        ("tool_use", REPLY_TOOL, {"chat_id": "x", "text": "R"}),
        ("text", "post"),
    ]


async def test_multi_block_frame_posts_in_block_order(tmp_path):
    """§2(3): text + a discrete post + text in ONE frame post in block order —
    the armed reply intent posts at ITS block, between the two narration texts."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _mixed_frame("before ", "R", "after"),
                              _result()])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cursor, rec, events)
    _arm_reply(relay, rec, "R")
    await relay.run()

    # before → discrete reply → after, strictly in block order.
    assert [t for _tp, t in rec.sends] == ["before ", "[reply]R", "after"]


async def test_discrete_post_seals_narration_rollover_on_interleave(tmp_path):
    """§2: narration seals when anything else posts below it — a mid-turn
    discrete post forces the next narration text into a NEW message rather than
    editing the message now sitting above the discrete post."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [
        _init(), _text("part one"), _reply_tool_frame("R"), _text("part two"),
        _result(),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cursor, rec, events)
    _arm_reply(relay, rec, "R")
    await relay.run()

    # msg1 "part one"; discrete "[reply]R" below it seals it; "part two" opens
    # a NEW message (never edits the sealed "part one").
    assert [t for _tp, t in rec.sends] == ["part one", "[reply]R", "part two"]
    assert all(text == "part one" or "part" not in text
               for _tp, _mid, text in rec.edits)  # no edit merged the two


async def test_hold_eligible_block_holds_slot_when_registry_empty(tmp_path):
    """F4: an ask/reply tool_use frame with NO registered intent HOLDS the slot
    (the intent may arm milliseconds later — the empty-registry no-hold guard
    used to defeat this race). The block holds for the (tiny, injected) slot,
    then proceeds on slot timeout posting nothing. A non-fenced tool keeps the
    fast path (covered elsewhere)."""
    from channels.output_sequencer import OutputSequencer

    rec, events = Recorder(), []
    _write_current(tmp_path, [_init(), _reply_tool_frame("R"), _result()])
    cursor = tmp_path / ".stream_cursor.json"
    hold_calls = {"n": 0}
    clock = {"t": 0.0}

    async def _counting_sleep(dt):
        hold_calls["n"] += 1
        clock["t"] += dt        # advance the fake clock so the slot deadline is

    seq = OutputSequencer(
        engagement_id="eng-1", topic_id=42,
        send_message=rec.send, edit_message=rec.edit,
        _now=lambda: clock["t"],  # reached after one poll (no real sleeping)
        _sleep=_counting_sleep,
        slot_hold_s=0.05, hold_poll_s=0.05,
    )
    await _make_relay(tmp_path, cursor, rec, events, sequencer=seq).run()
    # It HELD (slept at least once) rather than proceeding instantly, then slot-
    # timed-out and posted nothing (no intent ever arrived).
    assert hold_calls["n"] >= 1
    assert rec.sends == []


# -- the four crash / seal tests (§2, Sol r1-2): no post-recovery edit ever
#    lands on a message with anything below it. --------------------------------


async def test_crash_recovery_reconcile_seals_never_edits_prior(tmp_path):
    """recovery-reconcile: an OPEN turn (message_ids=[7], no result) recovered
    after a crash — reconcile conservatively SEALS id 7 and reposts as a new
    message. NO edit lands on id 7."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("hello world")])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[1]},
        message_ids=[7], last_posted_len=0,  # A1a: legacy full-repost path
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    assert all(mid != 7 for _t, mid, _x in rec.edits)  # never edits below-content id
    assert rec.sends == [(42, "hello world")]  # reposted as a NEW message


async def test_crash_result_finalize_seals_never_edits_prior(tmp_path):
    """result-finalize: the result frame lands live on recovery; the finalize
    edit routes through edit_narration_if_latest → SEALED → new closing
    message. NO edit lands on id 7."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _text("done text"), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[1]},  # through the text frame
        message_ids=[7], last_posted_len=0,  # A1a: legacy full-repost path
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    assert all(mid != 7 for _t, mid, _x in rec.edits)
    assert rec.sends == [(42, "done text")]
    assert StreamCursor.load(cur_path).message_ids == []  # closed


async def test_crash_interleave_before_checkpoint_no_edit_below(tmp_path):
    """interleave-before-checkpoint: a discrete-posting tool_use frame sits
    BELOW the narration and BEFORE the checkpoint boundary (replayed on
    recovery). No intent survives the crash, so nothing re-posts, and the
    reconcile seals id 7 — no edit lands on a message with content below it."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [
        _init(), _text("narr"), _reply_tool_frame("R"),
    ])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    # current is PAST the interleaved reply frame — everything replayed.
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[-1]},
        message_ids=[7], last_posted_len=0,  # A1a: legacy full-repost path
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    assert all(mid != 7 for _t, mid, _x in rec.edits)
    assert rec.sends == [(42, "narr")]  # sealed → reposted new; no re-fired discrete


async def test_crash_interleave_after_checkpoint_no_edit_below(tmp_path):
    """interleave-after-checkpoint: the discrete-posting tool_use frame sits
    AFTER the checkpoint (live on recovery). Going live triggers reconcile
    (seals id 7) BEFORE the live reply frame is handled — no edit lands on a
    message with content below it."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [
        _init(), _text("narr"), _reply_tool_frame("R"), _result(),
    ])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    # current is through the TEXT frame only; the reply frame is live.
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[1]},
        message_ids=[7], last_posted_len=0,  # A1a: legacy full-repost path
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    assert all(mid != 7 for _t, mid, _x in rec.edits)
    # id 7 sealed → "narr" reposted as a new message; the live reply frame has
    # no surviving intent (in-memory), so it resolves to no_match (nothing new).
    assert rec.sends == [(42, "narr")]


# ---------------------------------------------------------------------------
# v0.81.0 (W-R4, Sol r1-3): finalize NEVER reposts the visible sealed narration
# tail. The production bug: msg 1137 = "Option A locked in…"; msgs 1139/1141/1143
# = that SAME text reposted at result finalization after a later discrete post.
# ---------------------------------------------------------------------------


async def test_finalize_no_repost_of_fully_visible_sealed_narration(tmp_path):
    """R4 (a) NO-REPOST: narration is fully posted → a discrete SEALS it → the
    result finalize must NOT repost the already-visible sealed tail. Exactly ONE
    copy of the narration reaches the topic (pre-fix the finalize SEALED branch
    reposted the whole ``_per_message_text`` as a new message)."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [
        _init(), _text("Option A locked in"), _reply_tool_frame("R"), _result(),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cursor, rec, events)
    _arm_reply(relay, rec, "R")
    await relay.run()

    # Narration once, the discrete once, and finalize reposts NOTHING.
    assert [t for _tp, t in rec.sends] == ["Option A locked in", "[reply]R"]
    assert sum(t == "Option A locked in" for _tp, t in rec.sends) == 1


async def test_finalize_posts_only_pending_suffix_of_sealed_narration(tmp_path):
    """R4 (b) NO-LOSS: a visible PREFIX plus a PENDING throttled SUFFIX (the
    relay held an unposted edit at :769) → a discrete SEALS the message →
    finalize posts ONLY the genuinely-unposted suffix, exactly once — never the
    whole tail (the R4 dup), never nothing (data loss).

    Frozen clock + a wide throttle window: "AAA"→send, "AAABBB"→edit (posts,
    arms the window at t=0), "AAABBBCCC"→edit THROTTLED (held, unposted). The
    discrete then seals the narration; finalize must flush ONLY "CCC"."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [
        _init(), _text("AAA"), _text("BBB"), _text("CCC"),
        _reply_tool_frame("R"), _result(),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, edit_throttle=10.0, _now=lambda: 0.0,
    )
    _arm_reply(relay, rec, "R")
    await relay.run()

    # Prefix "AAABBB" was posted (send + edit); the discrete sealed it; only the
    # held "CCC" is newly posted at finalize.
    assert [t for _tp, t in rec.sends] == ["AAA", "[reply]R", "CCC"]
    # The whole tail is NEVER reposted, and the suffix is NEVER lost.
    assert all(t != "AAABBBCCC" for _tp, t in rec.sends)
    assert sum(t == "CCC" for _tp, t in rec.sends) == 1


async def test_seal_edit_branch_no_loss_via_posted_len(tmp_path):
    """F2/R4 NO-LOSS (Sol diff gate): the SEALED-EDIT branch in ``_execute_ops``
    (distinct from the finalize branch T4 fixed) must slice the fresh message
    from ``_posted_len`` — the accurate wire-posted length — NOT ``len(prior)``,
    which includes throttled never-posted text.

    Repro: ``AAA`` sent → ``AAABBB`` edited (both reach the wire) → ``CCC``
    THROTTLED (``_per_message_text`` advances to ``AAABBBCCC`` but it never
    reaches the wire, so ``_posted_len`` stays at 6) → a discrete reply SEALS
    the message → ``DDD`` arrives after the throttle window → the edit to
    ``AAABBBCCCDDD`` hits the sealed message → SEALED → a fresh message opens.
    That fresh message must carry the held ``CCC`` then ``DDD`` (``CCCDDD``);
    pre-fix it sliced ``value[len(prior):]`` = ``DDD`` and lost ``CCC``."""
    from channels.output_sequencer import OutputSequencer

    rec, events = Recorder(), []
    cursor = tmp_path / ".stream_cursor.json"
    seq = OutputSequencer(
        engagement_id="eng-1", topic_id=42,
        send_message=rec.send, edit_message=rec.edit,
    )
    relay = _make_relay(tmp_path, cursor, rec, events, sequencer=seq)

    # AAA posted (send), then AAABBB posted (edit) — both reach the wire.
    await relay._execute_ops([("send", "AAA")])
    await relay._execute_ops([("edit", "AAABBB")])
    assert relay._posted_len == 6  # only "AAABBB" reached the wire

    # CCC is THROTTLED: _per_message_text advances but _posted_len does NOT
    # (mirrors _post_text's held-edit path at :794).
    relay._per_message_text = "AAABBBCCC"

    # A discrete reply SEALS the narration message.
    await seq.seal_narration()

    # DDD after the throttle window → edit hits the sealed message → SEALED →
    # a fresh message opens for the genuinely-unposted increment.
    await relay._execute_ops([("edit", "AAABBBCCCDDD")])

    posted = [t for _tp, t in rec.sends]
    # NO-LOSS: the fresh message carries the held CCC then DDD, never just DDD.
    assert posted == ["AAA", "CCCDDD"]
    # CCC is never permanently lost, and nothing is duplicated.
    assert "".join(posted) == "AAACCCDDD"


async def test_crash_recovery_reposts_full_sealed_tail_unchanged_by_r4(tmp_path):
    """R4 (c): the crash-recovery CONSERVATIVE SEAL stays a SEPARATE,
    distinguishable branch from finalize. A real post-restart recovery of an
    OPEN turn whose narration was sealed by a discrete still reposts the FULL
    narration as a NEW message (the accepted at-least-once duplicate). The R4
    finalize suffix-only rule does NOT touch this reconcile path — this test
    stays green before and after the fix, pinning the branch boundary.

    A1a note: ``last_posted_len`` became load-bearing, so this scenario is pinned
    to the LEGACY (``last_posted_len == 0``) full-repost path; the delta-aware
    partial-tail reconcile is covered in test_topic_stream_round3.py."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [
        _init(), _text("recovered tail"), _reply_tool_frame("R"),
    ])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    # OPEN turn (no result): current is PAST the discrete frame — all replayed.
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[-1]},
        message_ids=[7], last_posted_len=0,  # A1a: legacy full-repost path
    ).save(cur_path)

    # FRESH relay + FRESH sequencer (models a process restart).
    await _make_relay(tmp_path, cur_path, rec, events).run()

    # Conservative seal reposts the WHOLE narration as a new message (accepted
    # duplicate) and never edits the below-content id 7.
    assert rec.sends == [(42, "recovered tail")]
    assert all(mid != 7 for _t, mid, _x in rec.edits)


# ---------------------------------------------------------------------------
# v0.79.0 (T1 review): replay models discrete-rollover boundaries so a
# same-process poll re-run never stale-prepends a rolled narration message.
# ---------------------------------------------------------------------------


async def test_discrete_rollover_same_process_rerun_keeps_second_msg_text(tmp_path):
    """Regression (T1 review): text → armed discrete post → text ROLLS the
    narration to a SECOND message; a same-process 0.5s poll re-run (the driver's
    ``while True: relay.run()`` on the SAME relay/sequencer) must rebuild the
    per-message text at the RECORDED discrete-rollover boundaries so the second
    narration message keeps ONLY its own text — never a stale-prepended merge.

    Pre-fix, ``_replay_text`` split only at ``_MSG_MAX`` and ignored the discrete
    rollover, rebuilding ``per_message_text="abcdef"``; with ``narration_msg_id``
    still intact in-process, ``_reconcile`` then MERGE-edited msg 3 to ``abcdef``,
    prepending the stale ``abc``.
    """
    rec, events = Recorder(), []
    # Open turn (NO result): "abc" → armed reply seals → "def" rolls to msg 3.
    _write_current(tmp_path, [
        _init(), _text("abc"), _reply_tool_frame("R"), _text("def"),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cursor, rec, events)
    _arm_reply(relay, rec, "R")
    await relay.run()  # first LIVE pass

    # Live state: msg1="abc", discrete "[reply]R"=msg2, msg3="def".
    assert [t for _tp, t in rec.sends] == ["abc", "[reply]R", "def"]
    assert relay.cursor.message_ids == [1, 3]
    # The additive per-message boundary field mirrors the two narration msgs.
    assert relay.cursor.message_text_lens == [3, 3]

    # Same-process poll re-run on the SAME relay/sequencer: narration_msg_id is
    # intact, so reconcile would MERGE-edit (not repost) — it must NOT prepend.
    await relay.run()

    assert all(text != "abcdef" for _tp, _mid, text in rec.edits)
    # msg 3 (the rolled narration message) is never edited to carry "abc".
    assert all(mid != 3 or "abc" not in text for _tp, mid, text in rec.edits)
    # No duplicate discrete/narration re-post from the second pass either.
    assert [t for _tp, t in rec.sends] == ["abc", "[reply]R", "def"]


async def test_legacy_checkpoint_absent_lens_falls_back_and_seals(tmp_path):
    """Legacy checkpoint (``message_text_lens`` ABSENT) still converges: with no
    recorded boundaries ``_replay_text`` falls back to today's ``_MSG_MAX``-only
    reconstruction, and the fresh-process CONSERVATIVE SEAL reposts the
    reconstructed narration as a NEW message — no edit ever lands on a message
    with content below it."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [
        _init(), _text("abc"), _reply_tool_frame("R"), _text("def"),
    ])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    # LEGACY checkpoint: message_ids present, message_text_lens field absent
    # (default []); current is PAST the whole (result-less) open turn.
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[-1]},
        message_ids=[7, 9],
        last_posted_len=0,  # A1a: legacy full-repost path (see round3 for delta)
    ).save(cur_path)

    # FRESH relay + FRESH sequencer (models a process restart).
    await _make_relay(tmp_path, cur_path, rec, events).run()

    # Fallback folds all text into one message via _MSG_MAX; the conservative
    # seal reposts it as a NEW message (id not among the checkpoint ids).
    assert all(mid not in (7, 9) for _t, mid, _x in rec.edits)
    assert rec.sends == [(42, "abcdef")]


# ---------------------------------------------------------------------------
# Cursor persistence round-trip.
# ---------------------------------------------------------------------------


def test_stream_cursor_load_absent_is_zero(tmp_path):
    cur = StreamCursor.load(tmp_path / "nope.json")
    assert cur.turn_start == {"segment": [0, 0], "offset": 0}
    assert cur.current == {"segment": [0, 0], "offset": 0}
    assert cur.message_ids == []
    assert cur.dropped_through is None


def test_stream_cursor_save_load_roundtrip(tmp_path):
    path = tmp_path / "c.json"
    StreamCursor(
        turn_start={"segment": [1, 2], "offset": 3},
        current={"segment": [1, 2], "offset": 9},
        message_ids=[7, 9],
        message_text_lens=[3900, 8],
        last_posted_len=11,
        dropped_through={"segment": [1, 2], "offset": 9},
    ).save(path)
    back = StreamCursor.load(path)
    assert back.message_ids == [7, 9]
    assert back.message_text_lens == [3900, 8]  # additive replay-boundary field
    assert back.current == {"segment": [1, 2], "offset": 9}
    assert back.dropped_through == {"segment": [1, 2], "offset": 9}


# ---------------------------------------------------------------------------
# v0.79.0 (§5): per-block tool_use events drive the live-summary controller
# (LIVE only — replay suppresses them so post-recovery state derives from the
# lifecycle, never stale tool frames).
# ---------------------------------------------------------------------------


def _tool_in(name: str, inp: dict) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]},
    }


async def test_live_tool_use_emits_activity_event(tmp_path):
    rec, events = Recorder(), []
    _write_current(
        tmp_path,
        [_spawn(1), _init(), _tool_in("Bash", {"command": "ls"}), _result()],
    )
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()
    # §5 P1-B (r6 fix): the tool_use event carries a MONOTONIC per-turn
    # tool-event counter as its ordering ``seq`` — a plain int, NOT a
    # segment-derived coordinate. The turn's first tool_use is 1.
    tool_events = [p for k, p in events if k == "tool_use"]
    assert len(tool_events) == 1
    assert tool_events[0]["tool"] == "Bash"
    assert tool_events[0]["input"] == {"command": "ls"}
    assert tool_events[0]["seq"] == 1


async def test_tool_use_seq_is_monotonic_within_turn(tmp_path):
    # Two tool_use blocks in ONE assistant frame, plus a third in a later frame
    # of the SAME turn, get distinct STRICTLY-INCREASING counter seqs.
    rec, events = Recorder(), []
    two_block = {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "TodoWrite", "input": {}},
            {"type": "tool_use", "name": "Bash", "input": {}},
        ]},
    }
    _write_current(
        tmp_path,
        [_spawn(1), _init(), two_block, _tool_in("Read", {}), _result()],
    )
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()
    seqs = [p["seq"] for k, p in events if k == "tool_use"]
    assert seqs == [1, 2, 3]  # distinct + strictly increasing within the turn


async def test_tool_use_seq_crosses_rotation_monotonically(tmp_path):
    # §5 P1-B (r6 fix) rotation-crossing: a TodoWrite in segment A, a mid-turn
    # log rotation, then a TodoWrite in segment B ⇒ B's seq is still GREATER
    # than A's (a bare per-turn counter, not a segment-derived coordinate that a
    # rotated inode could make sort lower and the controller reject forever).
    rec, events = Recorder(), []
    log_dir = str(tmp_path)
    current = os.path.join(log_dir, "current")
    cursor = tmp_path / ".stream_cursor.json"
    # Segment A (inode X): open turn — init + first TodoWrite, no result yet.
    _write_current(tmp_path, [_spawn(1), _init(), _tool("TodoWrite")])
    relay = _make_relay(tmp_path, cursor, rec, events)
    await relay.run()  # consumes segment A live, latches WARM (turn still open)
    seqs_a = [p["seq"] for k, p in events if k == "tool_use"]
    assert seqs_a == [1]
    # Rotate: current(X) → @a.s archive; a NEW current(Y) holds the turn's tail.
    os.rename(current, os.path.join(log_dir, "@a.s"))
    _write_current(tmp_path, [_tool("TodoWrite"), _result()])
    # WARM re-entry on the SAME relay (same process): the per-turn counter is NOT
    # reset, so the post-rotation TodoWrite sorts strictly above the first.
    await relay.run()
    seqs = [p["seq"] for k, p in events if k == "tool_use"]
    assert seqs == [1, 2]
    assert seqs[1] > seqs[0]  # segment B accepted (monotonic across rotation)


async def test_replayed_tool_use_is_suppressed(tmp_path):
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_init(), _tool("Read"), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    # Mid-turn checkpoint PAST the tool frame: it replays (no side effects).
    StreamCursor(
        turn_start={"segment": seg, "offset": 0},
        current={"segment": seg, "offset": offs[1]},
        message_ids=[],
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    assert not any(kind == "tool_use" for kind, _p in events)


# ---------------------------------------------------------------------------
# #334: durable result-event delivery (result_event_pending marker).
# ---------------------------------------------------------------------------


class _RaiseOnResult:
    """Event recorder whose first N "result" deliveries raise (transient)."""

    def __init__(self, fail_results: int = 1) -> None:
        self.events: list[tuple[str, dict]] = []
        self.fail_results = fail_results

    def __call__(self, kind: str, payload: dict) -> None:
        if kind == "result" and self.fail_results > 0:
            self.fail_results -= 1
            raise RuntimeError("driver callback boom")
        self.events.append((kind, payload))


async def test_raising_result_callback_is_redelivered_on_next_run(tmp_path):
    """#334: a transiently-failing ``on_turn_event("result")`` must not lose
    the turn-end event — the closed-turn checkpoint stays advanced (no
    narration re-post), the pending marker is durable, and the next cold run
    delivers the event before touching any frame."""
    rec = Recorder()
    handler = _RaiseOnResult(fail_results=1)
    offs = _write_current(
        tmp_path, [_spawn(1), _init(), _text("Hello"), _result()]
    )
    cur_path = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cur_path, rec, [])
    relay.on_turn_event = handler

    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="driver callback boom"):
        await relay.run()

    # The closed turn is durable: checkpoint past the result, marker pending.
    cur = StreamCursor.load(cur_path)
    assert cur.current["offset"] == offs[-1]
    assert cur.result_event_pending == {"subtype": "success"}
    sends_before, edits_before = len(rec.sends), len(rec.edits)

    relay2 = _make_relay(tmp_path, cur_path, rec, [])
    relay2.on_turn_event = handler
    await relay2.run()

    # Event delivered on recovery; marker cleared; nothing re-posted.
    assert ("result", {"subtype": "success"}) in handler.events
    assert len(rec.sends) == sends_before
    assert len(rec.edits) == edits_before
    cur = StreamCursor.load(cur_path)
    assert cur.result_event_pending is None


async def test_result_event_marker_cleared_on_successful_delivery(tmp_path):
    rec, events = Recorder(), []
    _write_current(tmp_path, [_spawn(1), _init(), _text("Hi"), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cur_path, rec, events).run()

    assert ("result", {"subtype": "success"}) in events
    assert sum(1 for k, _p in events if k == "result") == 1
    assert StreamCursor.load(cur_path).result_event_pending is None


async def test_pending_result_event_from_crash_delivers_without_replay(tmp_path):
    """#334 crash window: process died between the closed-turn save (marker
    set) and delivery. The next cold run delivers exactly the pending event
    and does not replay the closed turn's narration."""
    rec, events = Recorder(), []
    offs = _write_current(tmp_path, [_spawn(1), _init(), _text("Hello"), _result()])
    cur_path = tmp_path / ".stream_cursor.json"
    seg = _ident(os.path.join(str(tmp_path), "current"))
    StreamCursor(
        turn_start={"segment": seg, "offset": offs[-1]},
        current={"segment": seg, "offset": offs[-1]},
        message_ids=[],
        result_event_pending={"subtype": "success"},
    ).save(cur_path)

    await _make_relay(tmp_path, cur_path, rec, events).run()

    assert ("result", {"subtype": "success"}) in events
    assert rec.sends == []  # closed turn: nothing replayed onto the wire
    assert StreamCursor.load(cur_path).result_event_pending is None


def test_stream_cursor_legacy_load_defaults_pending_none(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({
        "turn_start": {"segment": [1, 2], "offset": 3},
        "current": {"segment": [1, 2], "offset": 3},
        "message_ids": [],
    }), encoding="utf-8")
    assert StreamCursor.load(path).result_event_pending is None
