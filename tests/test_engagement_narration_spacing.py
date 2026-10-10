"""R3 — narration spacing at the ``message.id`` boundary (v0.89.0).

Successive narration segments from DIFFERENT assistant messages were
concatenated with NO separator, producing run-ons like ``questions.Good`` and
``ext.Appl``. R3 inserts a ``\\n\\n`` separator at the boundary between distinct
assistant ``message.id``\\ s — IDENTICALLY across the three narration consumers:
the live path, the ``_replay_text`` cold-recovery path, and the held-anchor-
buffer flush.

The boundary signal is the structural ``message.id`` change (NO content
inspection / prose sniffing). The contract — VERIFIED against a captured
real-CLI fixture (``fixtures/engagement_cli_frames.ndjson``) — is that every
assistant frame carries ``message.id``; there is never more than one non-empty
text block per assistant message; and a ``message.id`` never carries non-empty
text in more than one frame. A DISTINCT ``message.id`` == a new segment.

These tests drive a REAL ``TopicStreamRelay`` over a REAL temp NDJSON log (the
``test_topic_stream`` harness) and REAL captured CLI frames; clocks are injected
and ``asyncio.sleep`` is never patched (the CLAUDE.md memory-cage rule).
"""
from __future__ import annotations

import json
import os

from channels.output_sequencer import ASK_TOOL, projection_hash
from drivers.topic_stream import StreamCursor, extract_narration
from test_topic_stream import (
    Recorder,
    _arm_reply,
    _init,
    _make_relay,
    _reply_tool_frame,
    _result,
    _spawn,
    _thinking,
    _tool_in,
    _write_current,
)

# ``asyncio_mode = auto`` (pytest.ini) marks the async tests; sync tests
# (the extractor + tripwire) stay unmarked.

FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "engagement_cli_frames.ndjson"
)


# ---------------------------------------------------------------------------
# Frame builders with an explicit message.id (the R3 boundary signal).
# ---------------------------------------------------------------------------


def _text_id(message_id: str, *texts: str) -> dict:
    """An assistant text frame carrying an explicit ``message.id``."""
    return {
        "type": "assistant",
        "message": {
            "id": message_id,
            "content": [{"type": "text", "text": t} for t in texts],
        },
    }


def _fast_sequencer(rec):
    """A REAL ``OutputSequencer`` with an injected clock (mirrors
    ``test_anchor_narration_buffer._fast_sequencer``)."""
    from channels.output_sequencer import OutputSequencer

    clock = {"t": 0.0}

    async def _tick_sleep(dt: float) -> None:
        clock["t"] += dt

    return OutputSequencer(
        engagement_id="eng-1", topic_id=42,
        send_message=rec.send, edit_message=rec.edit,
        _now=lambda: clock["t"], _sleep=_tick_sleep,
        slot_hold_s=0.05, intent_timeout_s=5.0, hold_poll_s=0.05,
    )


def _anchor_ask(question: str = "Q?") -> dict:
    return _tool_in(ASK_TOOL, {"question": question})


def _ah(question: str = "Q?") -> str:
    return projection_hash(ASK_TOOL, {"question": question})


def _load_text_frames() -> list[tuple[str, str]]:
    """Every text-bearing assistant frame in the fixture as ``(message_id,
    text)`` in log order — read through the SAME extractor the relay uses."""
    out: list[tuple[str, str]] = []
    with open(FIXTURE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            narr = extract_narration(json.loads(line))
            if narr is not None and narr.text:
                out.append((narr.message_id, narr.text))
    return out


# ---------------------------------------------------------------------------
# The typed extractor.
# ---------------------------------------------------------------------------


def test_extract_narration_reads_message_id_and_text():
    narr = extract_narration(_text_id("msg_A", "hello world"))
    assert narr is not None
    assert narr.message_id == "msg_A"
    assert narr.text == "hello world"
    # Non-assistant frames → None.
    assert extract_narration({"type": "result"}) is None
    assert extract_narration(_spawn(1)) is None
    # Thinking-only assistant frame → present, but empty text.
    tnarr = extract_narration(_thinking("private cot"))
    assert tnarr is not None and tnarr.text == ""
    # Missing message.id degrades to "" (an id that never changes → no sep).
    no_id = extract_narration(
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "x"}]}}
    )
    assert no_id.message_id == "" and no_id.text == "x"


# ---------------------------------------------------------------------------
# Live path — the run-on fix.
# ---------------------------------------------------------------------------


async def test_distinct_message_ids_separated_by_blank_line(tmp_path):
    """Two text frames with DIFFERENT message.ids get a ``\\n\\n`` separator —
    the ``questions.Good`` run-on becomes ``questions.\\n\\nGood``."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [
        _init(),
        _text_id("m1", "...into the design questions."),
        _text_id("m2", "Good — the project is a clean slate."),
        _result(),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    final = rec.edits[-1][2] if rec.edits else rec.sends[-1][1]
    assert "questions.\n\nGood" in final
    assert "questions.Good" not in final
    # The FIRST segment carries no LEADING separator.
    assert not final.startswith("\n\n")


async def test_first_segment_has_no_leading_separator(tmp_path):
    """A single-message turn is byte-identical to today (no id change → no
    separator is ever inserted)."""
    rec, events = Recorder(), []
    _write_current(tmp_path, [
        _init(), _text_id("only", "Just one segment."), _result(),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()
    assert rec.sends[0][1] == "Just one segment."
    assert "\n\n" not in rec.sends[0][1]


# ---------------------------------------------------------------------------
# Cold-replay byte-identity (the load-bearing parity point).
# ---------------------------------------------------------------------------


async def test_cold_replay_of_multi_id_turn_is_byte_identical(tmp_path):
    """A crash mid-turn, then a brand-new relay cold-recovers over the SAME
    cursor+log: the reconstructed per-message text and the byte-cursor
    (``message_text_lens`` / ``_posted_len``) match the live relay EXACTLY,
    separators included. A desync here breaks the round-4 checkpoint cursor."""
    frames = [
        _spawn(1), _init(),
        _text_id("m1", "First segment."),
        _text_id("m2", "Second segment."),
        _text_id("m3", "Third segment."),
    ]
    cursor = tmp_path / ".stream_cursor.json"

    # LIVE: an OPEN turn (no result) posts the narration and persists the cursor.
    rec_live, ev_live = Recorder(), []
    _write_current(tmp_path, frames)
    live = _make_relay(tmp_path, cursor, rec_live, ev_live)
    await live.run()
    assert live._per_message_text == (
        "First segment.\n\nSecond segment.\n\nThird segment."
    )

    # REPLAY: a completely NEW relay on the SAME on-disk cursor+log → cold
    # recovery replays m1/m2/m3 through ``_replay_text`` and reconstructs.
    rec_re, ev_re = Recorder(), []
    replay = _make_relay(tmp_path, cursor, rec_re, ev_re)
    await replay.run()

    assert replay._per_message_text == live._per_message_text  # byte-identical
    assert replay.cursor.message_text_lens == live.cursor.message_text_lens
    assert replay.cursor.message_ids == live.cursor.message_ids
    assert replay._posted_len == live._posted_len


async def test_cold_replay_multi_message_rollover_reconstructs_with_seps(tmp_path):
    """Separators are ordinary narration bytes that flow through the _MSG_MAX
    rollover: a turn that rolls into two Telegram messages recovers byte-
    identically (the separator lands wherever the rollover places it)."""
    big = "x" * 2000
    frames = [
        _init(),
        _text_id("a", big),   # message 1 (2000 + sep bytes)
        _text_id("b", big),   # rolls past _MSG_MAX into message 2
    ]
    cursor = tmp_path / ".stream_cursor.json"
    rec_live, ev = Recorder(), []
    _write_current(tmp_path, frames)
    live = _make_relay(tmp_path, cursor, rec_live, ev)
    await live.run()
    live_lens = list(live.cursor.message_text_lens)
    live_ids = list(live.cursor.message_ids)

    rec_re, ev2 = Recorder(), []
    replay = _make_relay(tmp_path, cursor, rec_re, ev2)
    await replay.run()
    assert replay._per_message_text == live._per_message_text
    assert replay.cursor.message_text_lens == live_lens
    assert replay.cursor.message_ids == live_ids


# ---------------------------------------------------------------------------
# Held-anchor-buffer flush — parity with the live path across the boundary.
# ---------------------------------------------------------------------------


async def test_held_buffer_flush_separates_message_ids_like_live(tmp_path):
    """anchor → two DISTINCT-id prose frames buffered → tool_use FLUSHES: the
    flushed narration separates the two message.ids with ``\\n\\n``, exactly as
    an un-suppressed live run of the same two frames would."""
    rec, events = Recorder(), []
    seq = _fast_sequencer(rec)
    _write_current(tmp_path, [
        _init(), _anchor_ask("Q?"),
        _text_id("m1", "Alpha part."),
        _text_id("m2", "Beta part."),
        _tool_in("Bash", {"command": "ls"}),
        _result(),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (5, 500, _ah("Q?")),
    )
    await relay.run()

    # P2 (r6): the buffer now flushes PER-TUPLE through the ONE shared LIVE
    # append routine (Sol r2-1/Terra r2-2 — a batched offset could delete
    # authored text), so the two segments render EXACTLY as a live run would:
    # a send opens the message, the second segment edits it to the separated
    # body. The final rendered message carries both segments + the ``\n\n`` sep.
    flush_final = rec.edits[-1][2] if rec.edits else rec.sends[-1][1]
    assert flush_final == "Alpha part.\n\nBeta part."
    assert [t for _tp, t in rec.sends] == ["Alpha part."]  # per-tuple: send then edit

    # PARITY: an un-suppressed live run of the SAME two frames yields the SAME
    # separated body (final growing message).
    rec2, ev2 = Recorder(), []
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    _write_current(live_dir, [
        _init(), _text_id("m1", "Alpha part."),
        _text_id("m2", "Beta part."), _result(),
    ])
    await _make_relay(
        live_dir, live_dir / ".stream_cursor.json", rec2, ev2,
    ).run()
    live_final = rec2.edits[-1][2] if rec2.edits else rec2.sends[-1][1]
    assert live_final == "Alpha part.\n\nBeta part."


# ---------------------------------------------------------------------------
# Real captured-CLI fixture: behaviour + the tripwire contract.
# ---------------------------------------------------------------------------


async def test_real_fixture_frames_are_separated_no_runons(tmp_path):
    """Drive the relay with the FIRST real consecutive text-bearing frames from
    the captured CLI fixture (each a distinct message.id) and assert the exact
    ``questions.Good`` run-on the fixture would otherwise produce is gone."""
    frames = _load_text_frames()[:4]
    assert len(frames) == 4, "fixture unexpectedly short"
    ids = [mid for mid, _ in frames]
    assert len(set(ids)) == 4, "expected 4 distinct message.ids"

    rec, events = Recorder(), []
    _write_current(
        tmp_path,
        [_init()] + [_text_id(mid, text) for mid, text in frames] + [_result()],
    )
    cursor = tmp_path / ".stream_cursor.json"
    await _make_relay(tmp_path, cursor, rec, events).run()

    final = rec.edits[-1][2] if rec.edits else rec.sends[-1][1]
    expected = "\n\n".join(text for _mid, text in frames)
    assert final == expected
    # The exact brief example, from real frames.
    assert "questions.\n\nGood" in final
    assert "questions.Good" not in final


def test_fixture_tripwire_contract():
    """FAIL LOUDLY if a future CLI breaks the message.id boundary contract the
    R3 separator rule depends on: every assistant frame carries a ``message.id``;
    at most ONE non-empty text block per frame; and NO ``message.id`` carries
    non-empty text in more than one frame."""
    assert os.path.exists(FIXTURE), f"missing captured-CLI fixture: {FIXTURE}"
    seen_text_ids: set[str] = set()
    n_assistant = 0
    with open(FIXTURE, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            frame = json.loads(line)
            if frame.get("type") != "assistant":
                continue
            n_assistant += 1
            message = frame.get("message") or {}
            mid = message.get("id")
            assert isinstance(mid, str) and mid, (
                f"frame {lineno}: assistant frame missing message.id — the R3 "
                f"boundary signal is gone; revisit the separator rule"
            )
            nonempty = [
                b for b in message.get("content") or []
                if isinstance(b, dict) and b.get("type") == "text"
                and isinstance(b.get("text"), str) and b["text"].strip()
            ]
            assert len(nonempty) <= 1, (
                f"frame {lineno} (id {mid}): >1 non-empty text block — CLI "
                f"contract broke; the message.id boundary rule needs revisiting"
            )
            if nonempty:
                assert mid not in seen_text_ids, (
                    f"frame {lineno}: message.id {mid} carries non-empty text in "
                    f"a SECOND frame — same-id text repeat; contract broke"
                )
                seen_text_ids.add(mid)
    assert n_assistant > 0, "fixture has no assistant frames"
    assert len(seen_text_ids) > 1, "fixture needs >1 distinct text-bearing id"


# ---------------------------------------------------------------------------
# P2 (F-LEADSEP, r6): no leading separator when a narration segment OPENS a new
# message via a SEAL. Ordered separator-provenance descriptors; live/replay
# byte-cursor parity preserved. REAL relay + synthetic NDJSON; NO content
# matching (provenance only). Numbered per the design spec's test list.
# ---------------------------------------------------------------------------


def _open_frames_1():
    """The seal-coincident scenario: ``Hello`` (m0) → a discrete SEALS it →
    ``World`` (m1, distinct id) OPENS a new message. OPEN turn (no result) so
    the mid-turn cursor (message_ids / lens / sep_stripped) is inspectable."""
    return [
        _init(),
        _text_id("m0", "Hello"),
        _reply_tool_frame("R"),
        # #1426: text right after a landed reply is not relayed; a tool call
        # in between keeps the seal-then-open shape this scenario pins.
        _tool_in("Read", {}),
        _text_id("m1", "World"),
    ]


# (1) seal-coincident boundary ⇒ no leading \n\n, sep_stripped[j]==True, lens =
#     stripped length.
async def test_p2_seal_coincident_boundary_strips_leading_sep(tmp_path):
    rec, events = Recorder(), []
    _write_current(tmp_path, _open_frames_1())
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cursor, rec, events)
    _arm_reply(relay, rec, "R")
    await relay.run()

    sends = [t for _tp, t in rec.sends]
    # "Hello", the discrete, then "World" — the sealed-open carries NO leading sep.
    assert sends == ["Hello", "[reply]R", "World"]
    assert not any(s.startswith("\n\n") for s in sends)
    # Two narration messages; the SECOND recorded the strip; its length is the
    # STRIPPED length ("World" == 5, not "\n\nWorld" == 7).
    assert len(relay.cursor.message_ids) == 2
    assert relay.cursor.sep_stripped == [False, True]
    assert relay.cursor.message_text_lens == [5, 5]
    assert relay._per_message_text == "World"
    assert relay._posted_len == 5
    assert relay._pending_seps == []  # all descriptors consumed by the re-plan


# (2) cold-replay of (1) byte-identical incl. cursor lengths.
async def test_p2_cold_replay_of_sealed_strip_is_byte_identical(tmp_path):
    cursor = tmp_path / ".stream_cursor.json"
    rec_live, ev_live = Recorder(), []
    _write_current(tmp_path, _open_frames_1())
    live = _make_relay(tmp_path, cursor, rec_live, ev_live)
    _arm_reply(live, rec_live, "R")
    await live.run()

    # A brand-new relay cold-recovers over the SAME on-disk cursor+log.
    rec_re, ev_re = Recorder(), []
    replay = _make_relay(tmp_path, cursor, rec_re, ev_re)
    await replay.run()

    assert replay._per_message_text == live._per_message_text == "World"
    assert replay.cursor.message_text_lens == live.cursor.message_text_lens == [5, 5]
    assert replay.cursor.sep_stripped == live.cursor.sep_stripped == [False, True]
    assert replay._posted_len == live._posted_len
    # Replay never leaves a pending descriptor (LIVE-COMMIT-ONLY).
    assert replay._pending_seps == []


# (3) exact-_MSG_MAX rollover then distinct-id frame ⇒ sep RETAINED both sides
#     (a natural rollover is NOT a seal — the separator is legitimate content).
async def test_p2_exact_msg_max_rollover_retains_sep(tmp_path):
    from drivers.topic_stream import _MSG_MAX

    big = "x" * _MSG_MAX  # fills message 1 exactly to the rollover boundary
    rec, events = Recorder(), []
    _write_current(tmp_path, [
        _init(), _text_id("m0", big), _text_id("m1", "tail"),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    live = _make_relay(tmp_path, cursor, rec, events)
    await live.run()

    sends = [t for _tp, t in rec.sends]
    # message 1 = the full block; message 2 = the ROLLOVER carrying the RETAINED
    # separator at its head (never stripped — no seal happened).
    assert sends[0] == big
    assert sends[-1] == "\n\ntail"
    assert live.cursor.sep_stripped == [False, False]  # nothing stripped

    # Cold-replay parity: reconstruction is byte-identical (sep retained).
    rec_re, ev_re = Recorder(), []
    replay = _make_relay(tmp_path, cursor, rec_re, ev_re)
    await replay.run()
    assert replay._per_message_text == live._per_message_text == "\n\ntail"
    assert replay.cursor.message_text_lens == live.cursor.message_text_lens


# (4) authored "\n\n" after a seal ⇒ NEVER stripped (no descriptor at the head).
async def test_p2_authored_blank_line_after_seal_never_stripped(tmp_path):
    rec, events = Recorder(), []
    # m1's own text literally STARTS with a blank line — SAME id as m0 (no
    # injected separator), so there is no descriptor at the tail head and the
    # authored bytes must survive the sealed-open unharmed.
    _write_current(tmp_path, [
        _init(), _text_id("m0", "Intro"),
        _reply_tool_frame("R"),
        _tool_in("Read", {}),  # #1426: a tool call re-opens narration
        _text_id("m0", "\n\nauthored"),  # same id ⇒ no injected sep
    ])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cursor, rec, events)
    _arm_reply(relay, rec, "R")
    await relay.run()

    sends = [t for _tp, t in rec.sends]
    # The sealed-open message carries the AUTHORED "\n\nauthored" verbatim (a
    # deletion attack is impossible — no separator descriptor sat at the head).
    assert "\n\nauthored" in sends
    assert relay.cursor.sep_stripped == [False, False]  # nothing stripped


# (5) THE THROTTLE CHAIN: coexisting descriptors [1, 4]; the sealed re-plan
#     strips ONLY the leading injected separator; internal seps retained;
#     replay parity holds. Direct-call live state (precise descriptor control,
#     mirroring test_topic_stream's _execute_ops-driven seal tests) + a
#     _replay_text-driven parity reconstruction.
async def test_p2_throttle_chain_strips_only_leading_descriptor(tmp_path):
    rec, events = Recorder(), []
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cursor, rec, events)

    # A posted as its own message.
    await relay._execute_ops([("send", "A")])
    assert relay._per_message_text == "A" and relay._posted_len == 1
    relay._last_text_msg_id = "m0"

    # B then C arrive throttled (never posted): _per_message_text grows and TWO
    # separator descriptors coexist at offsets 1 ("A"→B) and 4 ("A\n\nB"→C).
    sep_b = relay._apply_msg_sep("m1", "B", record=True)
    relay._per_message_text = "A" + sep_b               # "A\n\nB", _posted_len stays 1
    sep_c = relay._apply_msg_sep("m2", "C", record=True)
    relay._per_message_text = relay._per_message_text + sep_c  # "A\n\nB\n\nC"
    assert relay._pending_seps == [1, 4]

    # A discrete SEALS the narration; the throttled tail's edit hits it.
    await relay.sequencer.seal_narration()
    await relay._execute_ops(
        [("edit", "A\n\nB\n\nC")], "A\n\nB\n\nC",
    )

    # The fresh message strips ONLY the leading injected sep (descriptor 1 ==
    # _posted_len) and RETAINS the internal B→C separator.
    assert [t for _tp, t in rec.sends] == ["A", "B\n\nC"]
    assert relay.cursor.sep_stripped == [False, True]
    assert relay.cursor.message_text_lens == [1, 4]  # len("B\n\nC") == 4
    assert relay._pending_seps == []  # ALL descriptors consumed post-re-plan

    live_lens = list(relay.cursor.message_text_lens)
    live_flags = list(relay.cursor.sep_stripped)
    live_pmt = relay._per_message_text

    # Replay parity: a fresh relay reconstructs from the SAME lens + flags
    # byte-identically (the leading sep is skipped, the internal one retained).
    rec2, ev2 = Recorder(), []
    rep = _make_relay(tmp_path, tmp_path / "c2.json", rec2, ev2)
    rep.cursor.message_text_lens = live_lens
    rep.cursor.sep_stripped = live_flags
    for mid, txt in [("m0", "A"), ("m1", "B"), ("m2", "C")]:
        rep._replay_text(txt, mid)
    assert rep._per_message_text == live_pmt == "B\n\nC"
    assert rep._turn_text == "AB\n\nC"  # leading sep skipped, internal retained


# (6) held-flush XY same-id + distinct-id Z ⇒ authored text intact, per-tuple
#     provenance (Z's descriptor at its OWN commit's len(pmt), never the batch's
#     _posted_len), replay parity.
async def test_p2_held_flush_per_tuple_keeps_authored_text(tmp_path):
    rec, events = Recorder(), []
    seq = _fast_sequencer(rec)
    _write_current(tmp_path, [
        _init(), _anchor_ask("Q?"),
        _text_id("m1", "XY"),
        _text_id("m2", "Z"),
        _tool_in("Bash", {"command": "ls"}),
        _result(),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(
        tmp_path, cursor, rec, events, sequencer=seq,
        open_anchor_state=lambda: (5, 500, _ah("Q?")),
    )
    await relay.run()

    # Per-tuple flush: "XY" opens the message (send), "Z" edits it to the
    # separated body — the authored "XY" is intact, never truncated by a
    # batch-keyed offset (Terra XY\n\nZ deletion).
    assert [t for _tp, t in rec.sends] == ["XY"]
    flush_final = rec.edits[-1][2]
    assert flush_final == "XY\n\nZ"

    # PARITY: an un-suppressed live run of the SAME two frames yields the SAME
    # separated body.
    rec2, ev2 = Recorder(), []
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    _write_current(live_dir, [
        _init(), _text_id("m1", "XY"), _text_id("m2", "Z"), _result(),
    ])
    await _make_relay(live_dir, live_dir / ".stream_cursor.json", rec2, ev2).run()
    live_final = rec2.edits[-1][2] if rec2.edits else rec2.sends[-1][1]
    assert live_final == "XY\n\nZ"


# (7) legacy checkpoint (absent sep_stripped, incl. a zero-length lens entry) ⇒
#     normalized to all-False, NO skip, byte-exact v0.89.0 replay.
async def test_p2_legacy_checkpoint_normalizes_no_skip(tmp_path):
    # A v0.89.0-shaped OPEN turn: two distinct-id segments "Hello" / "World!".
    # v0.89.0 INJECTED the boundary separator, so message 2 on the wire was
    # "\n\nWorld!" (8 chars) and lens = [5, 8]. Written to disk with NO
    # sep_stripped field at all (a genuine legacy checkpoint).
    log_dir = tmp_path
    offs = _write_current(log_dir, [
        _init(), _text_id("m0", "Hello"), _text_id("m1", "World!"),
    ])
    import os as _os
    seg = _os.stat(_os.path.join(str(log_dir), "current"))
    cur_path = log_dir / ".stream_cursor.json"
    legacy = {
        "turn_start": {"segment": [seg.st_dev, seg.st_ino], "offset": 0},
        # ``current`` PAST every frame ⇒ all replay (side-effect-suppressed),
        # then reconcile — exercising the legacy reconstruction path.
        "current": {"segment": [seg.st_dev, seg.st_ino], "offset": offs[-1]},
        "message_ids": [7, 8],
        # v0.89.0 lens carry the retained seps.
        "message_text_lens": [5, 8],
        "last_posted_len": 8,
        # NO "sep_stripped" key — the legacy field is absent.
    }
    with open(cur_path, "w", encoding="utf-8") as fh:
        json.dump(legacy, fh)

    # Normalization: load pads sep_stripped to all-False parallel to message_ids.
    loaded = StreamCursor.load(cur_path)
    assert loaded.sep_stripped == [False, False]

    rec, events = Recorder(), []
    replay = _make_relay(log_dir, cur_path, rec, events)
    await replay.run()
    # NO skip anywhere: the separator is reconstructed exactly as v0.89.0 did.
    assert replay._per_message_text == "\n\nWorld!"
    assert replay._turn_text == "Hello\n\nWorld!"


# (8) parallel-list length asserted after every open across a multi-rollover
#     commit (the save-time invariant holds without a skew).
async def test_p2_parallel_list_length_after_multi_rollover(tmp_path):
    from drivers.topic_stream import _MSG_MAX

    # A single frame far larger than _MSG_MAX rolls into several messages; a
    # second distinct-id frame adds one more. Every open keeps the three lists
    # parallel — the save assertion (in StreamCursor.save) would fire otherwise.
    rec, events = Recorder(), []
    _write_current(tmp_path, [
        _init(),
        _text_id("m0", "a" * (_MSG_MAX * 3 + 100)),
        _text_id("m1", "b" * 200),
    ])
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cursor, rec, events)
    await relay.run()

    assert len(relay.cursor.message_ids) >= 4  # multiple rollover messages
    assert len(relay.cursor.sep_stripped) == len(relay.cursor.message_ids)
    assert len(relay.cursor.message_text_lens) == len(relay.cursor.message_ids)
    # Round-trips through save/load with the assertion intact.
    relay.cursor.save(cursor)
    reloaded = StreamCursor.load(cursor)
    assert len(reloaded.sep_stripped) == len(reloaded.message_ids)


# (9) multi-seal turn end-to-end parity: two seals in one turn, live then cold
#     replay reconstruct the same per-message text + cursor lengths.
async def test_p2_multi_seal_turn_end_to_end_parity(tmp_path):
    frames = [
        _init(),
        _text_id("m0", "First"),
        _reply_tool_frame("R1"),
        _tool_in("Read", {}),  # #1426: a tool call re-opens narration
        _text_id("m1", "Second"),
        _reply_tool_frame("R2"),
        _tool_in("Read", {}),
        _text_id("m2", "Third"),
    ]
    cursor = tmp_path / ".stream_cursor.json"
    rec_live, ev_live = Recorder(), []
    _write_current(tmp_path, frames)
    live = _make_relay(tmp_path, cursor, rec_live, ev_live)
    _arm_reply(live, rec_live, "R1", request_id="r1")
    _arm_reply(live, rec_live, "R2", request_id="r2")
    await live.run()

    # Each distinct-id segment, sealed by its preceding discrete, opens WITHOUT a
    # leading separator.
    narration = [t for _tp, t in rec_live.sends if not t.startswith("[reply]")]
    assert narration == ["First", "Second", "Third"]
    assert live.cursor.sep_stripped == [False, True, True]

    rec_re, ev_re = Recorder(), []
    replay = _make_relay(tmp_path, cursor, rec_re, ev_re)
    await replay.run()
    assert replay._per_message_text == live._per_message_text == "Third"
    assert replay.cursor.message_text_lens == live.cursor.message_text_lens
    assert replay.cursor.sep_stripped == live.cursor.sep_stripped


# (10) empty-text frames never reach a commit (the guard at the commit sites).
async def test_p2_empty_text_never_commits(tmp_path):
    rec, events = Recorder(), []
    cursor = tmp_path / ".stream_cursor.json"
    relay = _make_relay(tmp_path, cursor, rec, events)
    seg = (1, 1)

    # _post_text with empty text: checkpoints, posts nothing, records no sep.
    await relay._post_text("", seg, 10, "m0")
    assert rec.sends == [] and rec.edits == []
    assert relay._pending_seps == []
    assert relay.cursor.message_ids == []
    assert relay.cursor.current["offset"] == 10  # checkpoint advanced

    # _commit_narration with empty text: a no-op (no separator recorded).
    relay._last_text_msg_id = "prev"
    await relay._commit_narration("new", "")
    assert rec.sends == [] and relay._pending_seps == []
