"""NDJSON engagement-log → Telegram topic relay (design §W1 — live topic streaming).

A ``claude_code`` engagement's CLI is invoked ``--print --verbose
--output-format stream-json`` (Task 4 run template), so its stdout is strictly
NDJSON captured by the s6-log producer/consumer pair. Each line is either a
Casa control frame (``{"casa_control": "spawn", "epoch": N}`` printed pre-exec)
or a CLI stream-json frame (``system``/``init``, ``assistant`` with content
blocks, ``result``, plus a tolerated ``rate_limit_event``). This module turns
that stream into a *live window* on the engagement: assistant TEXT accumulates
into ONE edited topic message per turn (rolling to a new message past Telegram's
limit), tool names/args/results and thinking are NEVER surfaced, and a
crash-safe SEGMENT-QUALIFIED cursor gives honest **at-least-once** visible
delivery (a documented rare duplicate — never exactly-once).

Design invariants pinned here (Sol adversarial review, §W1):

* **Checkpoint every fully-handled frame (Sol r3-B7)** — including invisible
  ones (spawn, ``system``/init, tool-only assistant, ``rate_limit_event``,
  unknown, textless result). Only a visible-text frame whose post/edit has not
  yet succeeded blocks cursor advancement. ``current`` therefore marks the
  boundary between already-applied side effects (``<= current``) and un-applied
  ones (``> current``).
* **Recovery with side-effect suppression (Sol r4-B5)** — recovery re-reads
  from ``turn_start``; frames ``<= current`` replay in REPLAY mode (rebuild the
  visible-text state ONLY; NO ``on_turn_event``, NO ``send_message``, NO failure
  counting), so a checkpointed in-turn ``spawn`` does not re-arm the inbound
  queue and a checkpointed ``mutating_tool`` does not re-flag. Past ``current``
  the relay goes LIVE.
* **Closed-turn checkpoint at ``result`` (Sol r5-B5)** — after the final edit
  and reply de-dup delete, persist ``message_ids=[]``,
  ``turn_start == current`` (past ``result``) so a restart-after-dedup never
  ghost-edits a deleted message.
* **Split a huge block (Sol r5-B4)** — a single 8-10K assistant text block
  becomes repeated ``<= _MSG_MAX`` sends in a loop, never one oversized message.
* **Drop mode (Sol r2-B5)** — after ``_DROP_THRESHOLD`` consecutive failures,
  one WARNING, keep consuming and advance ``dropped_through`` to the turn's
  terminal coordinate so a restart never replays the discarded tail.
* **Held-fd rotation drainage (Sol B6)** — when ``current``'s inode changes the
  held fd is drained to EOF before opening the successor; a missing start
  segment yields a retention-gap WARNING and resumes at ``current`` offset 0.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, NamedTuple

from atomic_io import atomic_write_json
from channels.output_sequencer import (
    APPLIED,
    ASK_TOOL,
    DISCARDED,
    FAILED,
    HOLD_ELIGIBLE_TOOLS,
    SEALED,
    OutputSequencer,
    projection_hash,
)

logger = logging.getLogger(__name__)

_MSG_MAX = 3900          # roll to a new topic message past this many chars
_SEP = "\n\n"            # R3 narration-boundary separator (P2 provenance target)
_DROP_THRESHOLD = 20     # consecutive Telegram failures before dropping the turn
_BACKOFF_BASE = 1.0      # seconds
_BACKOFF_MAX = 30.0      # seconds
_ZERO_SEG = (0, 0)

# Segment sentinel yielded by iter_log_segments when turn_start's segment is
# absent from disk (retention gap) — the relay logs a WARNING and resumes.
SEGMENT_GAP = "__gap__"


class _FlushDeferred(Exception):
    """wb6-1 internal signal: a held-prose FLUSH failed (drop mode — every send
    hit the Telegram wire failure floor). The caller left the ``hold_pending``
    marker set, the buffer retained, and the replay boundary UNADVANCED (behind
    the held frames); raising this ABANDONS the current run WITHOUT latching warm
    so the next driver poll cold-recovers and resurfaces the held prose as
    ordinary (disarmed) narration — §D5 resurface-never-lose. Never escapes the
    relay: caught in ``_run_cold`` / ``_run_warm``."""

# Explicit non-mutating allowlist (Sol r3-B4). Everything else — including an
# unknown ``mcp__*`` tool — is treated as mutating. The engagement CONTROL
# tools are how the agent interacts while awaiting_operator, so they must NEVER
# flag a violation.
_NON_MUTATING_TOOLS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "WebFetch",
        "WebSearch",
        "ToolSearch",
        "Skill",
        "mcp__casa-engagement-channel__reply",
        "mcp__casa-engagement-channel__ask",
        "mcp__casa-engagement-channel__set_progress",
    }
)


# ---------------------------------------------------------------------------
# Frame parsing / classification (pure functions).
# ---------------------------------------------------------------------------


def parse_frame(line: bytes) -> dict | None:
    """``json.loads`` one NDJSON line; return the object dict or ``None``.

    Non-JSON, non-object, or empty input → ``None`` (the caller skips and
    debug-logs). Never raises.
    """
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def extract_text_blocks(frame: dict) -> list[str]:
    """Assistant ``TextBlock`` text ONLY — never ``tool_use`` / ``thinking``.

    Mirrors ``agent.py::_make_on_message``'s TextBlock filter, but on raw
    stream-json dicts: ``{"type": "assistant", "message": {"content": [...]}}``.
    """
    if not isinstance(frame, dict) or frame.get("type") != "assistant":
        return []
    message = frame.get("message") or {}
    content = message.get("content") or []
    out: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                out.append(text)
    return out


def iter_content_blocks(frame: dict) -> list[tuple]:
    """Assistant content blocks IN ORDER (§2(3): per-block positions).

    Returns a list of ``("text", text)`` and ``("tool_use", name, input)``
    tuples in the block order the CLI emitted them; ``thinking`` blocks are
    dropped (never surfaced). One NDJSON assistant frame can interleave text
    and multiple tool_use blocks, so the relay must walk them in order to place
    a relay-mediated discrete post at exactly its block's position.
    """
    if not isinstance(frame, dict) or frame.get("type") != "assistant":
        return []
    message = frame.get("message") or {}
    out: list[tuple] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                out.append(("text", text))
        elif btype == "tool_use":
            out.append((
                "tool_use", block.get("name") or "", block.get("input") or {},
            ))
    return out


class AssistantNarration(NamedTuple):
    """The §R3 boundary signal read from ONE assistant frame."""

    message_id: str
    text: str


def extract_narration(frame: dict) -> "AssistantNarration | None":
    """The ONE typed reader of an assistant frame's ``(message.id, text)`` — the
    §R3 narration-boundary signal.

    Returns ``None`` for a non-assistant frame. ``text`` joins the frame's
    non-empty text blocks — per the captured real-CLI contract there is AT MOST
    ONE (the fixture-tripwire test enforces it), so the join is a no-op for the
    single-block case and stays defensively correct otherwise. ``message_id`` is
    ``""`` only for a malformed frame missing ``message.id`` — treated as an id
    that never changes, so an id-less stream degrades to the pre-R3 separator-
    free join. A DISTINCT ``message_id`` marks a NEW narration segment: the live
    path, ``_replay_text`` and the held-anchor-buffer flush all read the boundary
    through THIS extractor so the three advance narration state identically. The
    boundary is the STRUCTURAL id change, never content inspection.
    """
    if not isinstance(frame, dict) or frame.get("type") != "assistant":
        return None
    message = frame.get("message") or {}
    mid = message.get("id")
    texts = [
        block["text"]
        for block in message.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text"
        and isinstance(block.get("text"), str) and block["text"]
    ]
    return AssistantNarration(
        message_id=mid if isinstance(mid, str) else "",
        text="".join(texts),
    )


def is_mutating_tooluse(frame: dict) -> tuple[bool, str]:
    """``(True, tool_name)`` iff *frame* is an assistant ``tool_use`` for a
    mutating tool.

    Read-only tools (Read/Glob/Grep/WebFetch/WebSearch/ToolSearch/Skill) and the
    engagement control tools (``mcp__casa-engagement-channel__{reply,ask,
    set_progress}``) are non-mutating → ``(False, "")``. Anything else — every
    other tool, including unknown ``mcp__*`` — is mutating. The first mutating
    ``tool_use`` block found wins.
    """
    if not isinstance(frame, dict) or frame.get("type") != "assistant":
        return (False, "")
    message = frame.get("message") or {}
    for block in message.get("content") or []:
        if not (isinstance(block, dict) and block.get("type") == "tool_use"):
            continue
        name = block.get("name") or ""
        if name not in _NON_MUTATING_TOOLS:
            return (True, name)
    return (False, "")


# ---------------------------------------------------------------------------
# Segment-qualified crash-safe cursor.
# ---------------------------------------------------------------------------


def _zero_coord() -> dict:
    return {"segment": [0, 0], "offset": 0}


@dataclass
class StreamCursor:
    """Persisted at the control dir's ``.stream_cursor.json`` (Task 4,
    containment stage 2 — ``drivers.workspace.stream_cursor_path``) via
    temp+rename (atomic_io).

    Coordinates are SEGMENT-QUALIFIED: ``segment`` is a log file's stable
    ``[st_dev, st_ino]`` identity recorded at open, so an offset is never
    ambiguous across an s6-log rotation while Casa was down.
    """

    turn_start: dict = field(default_factory=_zero_coord)
    current: dict = field(default_factory=_zero_coord)
    message_ids: list[int] = field(default_factory=list)
    # Additive, backwards-tolerated (T1 review): per-message narration TEXT
    # length, parallel to ``message_ids`` — the boundaries at which the live
    # path split narration across messages (both _MSG_MAX and discrete-
    # interleave/seal rollovers). ``_replay_text`` splits reconstructed text at
    # these when present, falling back to _MSG_MAX-only when absent (legacy
    # checkpoints — the conservative recovery seal already covers them). The
    # design constraint pinned the cursor/checkpoint format UNCHANGED for the
    # RECOVERY-SEALING option; an absent-tolerated additive field honors that
    # intent (no incompatibility, replay converges).
    message_text_lens: list[int] = field(default_factory=list)
    # P2 (F-LEADSEP, r6): per-message narration separator-provenance flag,
    # parallel to ``message_ids``. ``True`` iff the live path STRIPPED the
    # injected ``\n\n`` boundary separator at this message's head (a sealed-open
    # discarded it — the separator would otherwise render a leading blank line
    # after a tool burst). Replay skips the sep for exactly these ordinals so
    # the reconstructed narration is byte-identical to the live wire. NORMALIZED
    # ON LOAD to ``[False] * len(message_ids)`` (absent/short legacy state pads
    # with ``False`` — never wholesale-replaces valid flags); a save-time
    # assertion holds it parallel to ``message_ids``. Additive, legacy=all-False.
    sep_stripped: list[bool] = field(default_factory=list)
    last_posted_len: int = 0
    dropped_through: dict | None = None
    # D5 Task C3 (Sol r4-3): the ONE minimal persisted exception to the
    # in-memory suppression state — a boolean HELD-FRAMES marker. It carries NO
    # content, only "frames beyond ``current`` may contain previously-buffered
    # prose". Write-ahead ordering (Sol r5-2): set True (``_save``) BEFORE the
    # first frame's text is treated as held; cleared ONLY atomically (same
    # ``_save``) with the checkpoint that advances beyond the held frames (after
    # a flush, on the result-time discard, or at an abnormal spawn boundary).
    # Absent-tolerated → ``False`` (older checkpoints predate the field).
    hold_pending: bool = False
    # #538: the DISCARD tombstone riding ``hold_pending``. ``_finalize``'s
    # result-time discard decision write-aheads it (own ``_save``) BEFORE the
    # closing-edit/drain awaits, so a crash in that window recovers as "the
    # held frames are DEAD" — recovery DROPS their text instead of
    # resurfacing it (the one case where resurfacing is wrong). Shape:
    # ``{"from": {"segment": [...], "offset": int}}`` — the FIRST held
    # frame's coordinate; only frames AT/PAST it are dead (Sol diff-r1: a
    # frame-scoped bool would also drop throttled-but-landed pre-anchor text
    # from the wire-high-water reconstruction and shrink the closing edit).
    # Meaningless without ``hold_pending``; cleared at every site that
    # clears the marker AND reset by ``_arm_hold_marker`` when a new hold
    # generation starts. Absent-tolerated → ``None``.
    hold_discarded: dict | None = None
    # #334: durable turn-end-event outbox (size one). Set ATOMICALLY in the
    # same save as the closed-turn checkpoint (write-ahead, the ``hold_pending``
    # precedent), cleared by a second save only after ``on_turn_event("result")``
    # returned. A callback that raises (or a crash before the clear) leaves the
    # marker durable; ``_run_cold`` re-delivers the pending event BEFORE the
    # frame loop — the closed turn itself never replays. At-least-once: a crash
    # between a successful delivery and the clear duplicates the event, which
    # drivers already tolerate (spawn replay has the same contract).
    # Absent-tolerated → ``None`` (older checkpoints predate the field).
    result_event_pending: dict | None = None
    # #523: qualified wire high-water marker, written (with its own ``_save``)
    # the moment ``_finalize``'s closing narration edit lands APPLIED and
    # cleared atomically with the closed-turn checkpoint. Shape:
    # ``{"coord": {"segment": [...], "offset": int}, "len": int}`` — "the last
    # narration message's on-wire text has length ``len``, valid for frames at
    # or below ``coord`` (the result frame)". A crash between the closing edit
    # and the closed-turn save used to repost the throttled closing suffix on
    # cold replay (the suffix frames lie PAST the persisted ``current``, so
    # they replayed LIVE against a conservatively sealed message); with the
    # marker, replay reconstructs them text-only and adopts ``len`` at
    # ``coord`` — no repost, and a crash BEFORE the edit (no marker) keeps
    # today's at-least-once delivery. Absent-tolerated → ``None``.
    wire_hw: dict | None = None

    def __post_init__(self) -> None:
        # P2: NORMALIZE sep_stripped parallel to message_ids at EVERY
        # construction (load AND direct) — pad a short/absent legacy list to
        # ``[False] * len(message_ids)`` (never wholesale-replace valid flags),
        # trim an over-long one, before ANY use. A legacy checkpoint (field
        # absent) becomes all-False; a v0.91+ checkpoint round-trips unchanged.
        self.sep_stripped = [bool(x) for x in self.sep_stripped]
        if len(self.sep_stripped) < len(self.message_ids):
            self.sep_stripped.extend(
                [False] * (len(self.message_ids) - len(self.sep_stripped))
            )
        elif len(self.sep_stripped) > len(self.message_ids):
            del self.sep_stripped[len(self.message_ids):]

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "StreamCursor":
        """Load the cursor; an absent/corrupt file yields an all-zero cursor.

        ``sep_stripped`` is normalized parallel to ``message_ids`` in
        ``__post_init__`` (legacy checkpoints predate the field → all-False)."""
        try:
            with open(path, "rb") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(
            turn_start=data.get("turn_start") or _zero_coord(),
            current=data.get("current") or _zero_coord(),
            message_ids=list(data.get("message_ids") or []),
            message_text_lens=list(data.get("message_text_lens") or []),
            sep_stripped=list(data.get("sep_stripped") or []),
            last_posted_len=int(data.get("last_posted_len") or 0),
            dropped_through=data.get("dropped_through"),
            hold_pending=bool(data.get("hold_pending") or False),
            hold_discarded=(
                data.get("hold_discarded")
                if isinstance(data.get("hold_discarded"), dict)
                else None
            ),
            result_event_pending=(
                data.get("result_event_pending")
                if isinstance(data.get("result_event_pending"), dict)
                else None
            ),
            wire_hw=(
                data.get("wire_hw")
                if isinstance(data.get("wire_hw"), dict)
                else None
            ),
        )

    def save(self, path: str | os.PathLike[str]) -> None:
        # P2 save-time invariant: sep_stripped stays parallel to message_ids
        # (the relay's cursor helper enforces this on every mutation — this is a
        # belt-and-suspenders assertion that catches any missed site in tests).
        assert len(self.sep_stripped) == len(self.message_ids), (
            "sep_stripped/message_ids length skew: "
            f"{len(self.sep_stripped)} != {len(self.message_ids)}"
        )
        atomic_write_json(
            path,
            {
                "turn_start": self.turn_start,
                "current": self.current,
                "message_ids": self.message_ids,
                "message_text_lens": self.message_text_lens,
                "sep_stripped": self.sep_stripped,
                "last_posted_len": self.last_posted_len,
                "dropped_through": self.dropped_through,
                "hold_pending": self.hold_pending,
                "hold_discarded": self.hold_discarded,
                "result_event_pending": self.result_event_pending,
                "wire_hw": self.wire_hw,
            },
        )


# ---------------------------------------------------------------------------
# Held-fd segment generator (Sol B6).
# ---------------------------------------------------------------------------


def _seg_ident(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _ordered_segments(log_dir: str) -> list[tuple[str, tuple[int, int]]]:
    """``@*.s`` archives (chronological — TAI64N sorts lexically) then ``current``."""
    entries: list[tuple[str, tuple[int, int]]] = []
    try:
        names = sorted(os.listdir(log_dir))
    except OSError:
        names = []
    for name in names:
        if name.startswith("@") and name.endswith(".s"):
            path = os.path.join(log_dir, name)
            ident = _seg_ident(path)
            if ident is not None:
                entries.append((path, ident))
    cur = os.path.join(log_dir, "current")
    cur_ident = _seg_ident(cur)
    if cur_ident is not None:
        entries.append((cur, cur_ident))
    return entries


def _find_segment(log_dir: str, seg: tuple[int, int]) -> str | None:
    for path, ident in _ordered_segments(log_dir):
        if ident == seg:
            return path
    return None


def _successor_path(log_dir: str, seg: tuple[int, int]) -> str | None:
    """The path of the segment that follows *seg*, or ``None`` at the live end.

    Rebuilt fresh each call so a rotation that renamed the held ``current`` to
    an ``@*.s`` archive (and created a new ``current``) resolves correctly.
    """
    entries = _ordered_segments(log_dir)
    for i, (_path, ident) in enumerate(entries):
        if ident == seg:
            return entries[i + 1][0] if i + 1 < len(entries) else None
    # *seg* is no longer named on disk (fully drained held fd). If a different
    # ``current`` now exists, it is the successor; otherwise we are at the end.
    cur = os.path.join(log_dir, "current")
    cur_ident = _seg_ident(cur)
    if cur_ident is not None and cur_ident != seg:
        return cur
    return None


def _open_seg(path: str, offset: int):
    """``os.open`` + ``os.fdopen('rb')``; return ``(reader, (dev, ino))``."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    reader = os.fdopen(fd, "rb")
    if offset:
        reader.seek(offset)
    return reader, (st.st_dev, st.st_ino)


async def iter_log_segments(log_dir: str, start: dict):
    """Yield ``(segment, byte_offset_after, raw_line)`` from *start* onward.

    ``segment`` is the yielding file's ``(st_dev, st_ino)``; ``byte_offset_after``
    is the offset in that file just past the yielded line's newline. Opens the
    segment matching *start*'s ``(dev, ino)`` among ``current`` + ``@*.s``
    archives (or ``current`` at offset 0 when *start* is zero). On EOF it drains
    to the successor: an archive → the next segment at offset 0; the held
    ``current`` whose inode has since changed → the new ``current`` at offset 0
    (held-fd rotation drainage). A *start* segment absent from disk first yields
    a ``(SEGMENT_GAP, 0, b"")`` sentinel, then resumes at ``current`` offset 0.
    """
    start_seg = tuple(start.get("segment", (0, 0)))
    start_off = int(start.get("offset", 0))

    opened = None
    if start_seg == _ZERO_SEG:
        opened = _open_seg(os.path.join(log_dir, "current"), 0)
    else:
        path = _find_segment(log_dir, start_seg)
        if path is None:
            # Retention gap: the turn's start segment rotated out while down.
            yield (SEGMENT_GAP, 0, b"")
            opened = _open_seg(os.path.join(log_dir, "current"), 0)
        else:
            opened = _open_seg(path, start_off)
    if opened is None:
        return

    reader, seg = opened
    try:
        while True:
            line = reader.readline()
            if line.endswith(b"\n"):
                yield (seg, reader.tell(), line)
                continue
            # No complete line: EOF (or a partial tail we do not surface).
            successor = _successor_path(log_dir, seg)
            if successor is None:
                return
            reader.close()
            nxt = _open_seg(successor, 0)
            if nxt is None:
                return
            reader, seg = nxt
    finally:
        try:
            reader.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# The relay.
# ---------------------------------------------------------------------------

SendMessage = Callable[[int, str], Awaitable[int | None]]
EditMessage = Callable[[int, int, str], Awaitable[bool]]
DeleteMessage = Callable[[int, int], Awaitable[bool]]
OnTurnEvent = Callable[[str, dict], Any]
ReplyTexts = Callable[[], Any]
# D5 (spec "Successful-anchor identity comes from the DRIVER"): the driver-
# injected seam reading the LEDGER + answered-overlay + reservation truth.
# Returns ``(question_number, tg_message_id, source_hash)`` for the oldest
# genuinely open, unanswered free-text anchor, or ``None``. Same injection style
# as ``on_turn_event``; default ``None`` (below) leaves the feature inert.
#
# wb2-1 (whole-branch gate wave 2): ``source_hash`` is the projection hash of the
# ask that PRODUCED the anchor (the SAME hash the relay computes for its ask
# block). It lets a candidate bind POSITIVELY to the anchor its OWN ask produced,
# so a prior / co-existing open anchor can never arm a later, unrelated candidate
# (the F-LEAK2 cross-turn residual). A legacy 2-tuple seam (``source_hash``
# absent) reads as unbindable — the feature is then inert for that caller.
OpenAnchorState = Callable[
    [], "tuple[int, int, str | None] | tuple[int, int] | None"]
# wb2-3 (whole-branch gate wave 2): the driver-injected TERMINAL lifecycle seam.
# ``True`` once the engagement record has flipped terminal and
# ``settle_all_open_questions`` is closing the anchor ledger while this relay is
# still alive (until completion posting / topic closure). At ``result``/finalize a
# terminal closure DISCARDS held narration (the engagement is over — D5 discard
# doctrine) instead of mistaking the now-closed ledger for an answer and flushing
# a held sign-off below the terminal completion. Default ``None`` = inert.
EngagementTerminal = Callable[[], bool]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _default_sleep(seconds: float) -> None:  # pragma: no cover - trivial
    import asyncio

    await asyncio.sleep(seconds)


class TopicStreamRelay:
    """Relay one engagement's NDJSON log to its Telegram topic (plain sender).

    Mirrors ``channels.telegram.TopicStreamHandle``'s STRUCTURE (one edited
    message per turn, rolling past ``_MSG_MAX``) but drives injected PLAIN
    senders rather than the rich channel primitive, so it is testable in
    isolation and reusable by the driver (Task 6 wires it in).
    """

    def __init__(
        self,
        *,
        engagement_id: str,
        topic_id: int,
        log_dir: str,
        cursor_path: str,
        send_message: SendMessage,
        edit_message: EditMessage,
        delete_message: DeleteMessage,
        on_turn_event: OnTurnEvent,
        reply_texts: ReplyTexts,
        edit_throttle: float = 1.0,
        sequencer: "OutputSequencer | None" = None,
        open_anchor_state: "OpenAnchorState | None" = None,
        engagement_terminal: "EngagementTerminal | None" = None,
        _now: Callable[[], float] = time.monotonic,
        _sleep: Callable[[float], Awaitable[None]] = _default_sleep,
    ) -> None:
        self.engagement_id = engagement_id
        self.topic_id = topic_id
        self.log_dir = log_dir
        self.cursor_path = cursor_path
        self.send_message = send_message
        self.edit_message = edit_message
        # ``delete_message`` and ``reply_texts`` are injected by the driver but
        # currently UNUSED here: §2(d) removed the finalize de-dup DELETE (a
        # duplicate is preferred over erasing history), and there is NO
        # content-based de-dup-before-post inside the sequencer — that seam was
        # never wired (a prior comment here claimed it was; it is not). Duplicate
        # NARRATION is prevented STRUCTURALLY instead: ``_finalize`` reposts only
        # genuinely-unposted trailing text (W-R4, tracked via ``_posted_len``),
        # and the sequencer's SEAL / high-water invariant keeps ordering. These
        # two seams are retained only so the driver wiring stays stable.
        self.delete_message = delete_message
        self.on_turn_event = on_turn_event
        self.reply_texts = reply_texts
        self.open_anchor_state = open_anchor_state
        # wb2-3: the driver-injected terminal-lifecycle seam (see EngagementTerminal).
        self.engagement_terminal = engagement_terminal
        self._edit_throttle = edit_throttle
        self._now = _now
        self._sleep = _sleep
        # v0.79.0 (§2): ALL topic output flows through ONE per-topic sequencer
        # (the single writer that owns the high-water mark, the no-op edit gate
        # and the relay-mediated discrete-posting intent registry). The driver
        # passes the SHARED per-engagement sequencer so discrete ingresses and
        # this relay agree on ordering; an internal one is built when none is
        # injected (isolated relay tests + belt-and-suspenders).
        self.sequencer = sequencer or OutputSequencer(
            engagement_id=engagement_id,
            topic_id=topic_id,
            send_message=send_message,
            edit_message=edit_message,
            _now=_now,
            _sleep=_sleep,
        )

        self.cursor = StreamCursor()
        # Per-turn in-memory state (never persisted — rebuilt on recovery).
        self._turn_text = ""
        # §5 P1-B (r6 fix): a relay-owned MONOTONIC per-turn tool-event counter.
        # Incremented ONCE per tool_use block processed on the LIVE path and
        # stamped as the plan-frame ordering ``seq`` (below). It is strictly
        # increasing WITHIN a turn and — unlike the former ``(segment, offset,
        # block_ordinal)`` coordinate — carries NO file identity, so a mid-turn
        # log rotation can never make a newer TodoWrite sort lower than an
        # earlier one (which the controller's ≤-watermark would then reject
        # forever, freezing the checklist). Reset with the rest of the per-turn
        # state in ``_reset_turn_state``; the controller's watermark resets on
        # the same turn boundary.
        self._tool_event_seq = 0
        # §R3: message.id of the LAST text frame appended to narration (``None``
        # at a turn boundary). A frame whose id differs begins a NEW assistant
        # message → its text is prefixed with a ``\n\n`` separator. Advanced by
        # ``_apply_msg_sep`` — the ONE boundary decision the live path, replay
        # and the held-buffer flush all share, so they stay byte-parallel.
        self._last_text_msg_id: str | None = None
        # P2 (F-LEADSEP, r6): ORDERED separator-provenance descriptors — absolute
        # offsets (in ``_per_message_text`` coordinates) of every injected-and-
        # not-yet-posted ``\n\n`` boundary separator. Appended ON THE LIVE COMMIT
        # PATH ONLY (``_apply_msg_sep(record=True)``); replay never appends and
        # this is asserted EMPTY at the replay→live transition. ONE helper owns
        # every mutation (append/consume/rebase/clear) — no site touches it
        # directly. Multiple pending injections coexist (throttled tails); the
        # sealed-open re-plan strips iff the earliest descriptor sits at the
        # tail's head. NO content matching anywhere — provenance only.
        self._pending_seps: list[int] = []
        # P2 replay ordinal invariant: ``j`` = number of COMPLETED recorded
        # messages whose start-boundary the reconstruction has consumed. Used to
        # decide, at each replayed ``message.id`` boundary, whether the separator
        # was stripped live (``sep_stripped[j]``) and must be skipped so the
        # reconstructed byte-stream matches the wire. Reset every turn boundary.
        self._replay_j = 0
        self._per_message_text = ""
        # W-R4 (Sol r1-3): length of the CURRENT narration message's text that
        # has actually reached the wire (a send/edit that landed). It tracks
        # ``_per_message_text`` EXCEPT while a throttled edit is held unposted
        # (:769) — then ``_per_message_text`` grows but this does not, so the
        # held suffix is ``_per_message_text[_posted_len:]``. ``_finalize`` uses
        # it to repost ONLY genuinely-unposted trailing text when the narration
        # was sealed (never the visible prefix). Unlike the write-only cursor
        # ``last_posted_len``, an invisible tool-only frame never bumps this.
        self._posted_len = 0
        self._replay_msg_count = 0
        self._fail_count = 0
        self._dropped = False
        self._drop_warned = False
        self._last_edit_ts = float("-inf")
        # Recovery bookkeeping.
        self._live = True
        self._reconciled = False
        self._passed_cur_seg = False
        # §A1(1) WARM re-entry (F-DUP): the relay object survives the driver's
        # 0.5s poll; a CLEAN re-entry resumes IN PLACE instead of reloading the
        # cursor, resetting turn state, replaying, and reconciling (which sealed
        # + reposted the open narration on EVERY poll — the every-turn dup).
        # ``_warm`` latches at clean exhaustion; ``_read_coord`` is a MEMORY-ONLY
        # resume coordinate ({"segment": [dev, ino], "offset": N}) set for EVERY
        # frame consumed (replay AND live) — NOT ``cursor.current`` (which sits
        # behind a throttle-held text frame that returned without checkpointing;
        # resuming there would re-read and duplicate the held suffix, Sol r1-1).
        self._warm = False
        self._read_coord: dict | None = None
        # D5 anchor-candidate + arming (Task C1 — buffering/flush is C2, the
        # ``hold_pending`` marker is C3). ``_anchor_candidate`` is the
        # ``(tool_name, block_hash)`` of this turn's matching free-text-anchor
        # ask block (recorded regardless of ``post_for_block``'s return);
        # ``_suppressing_for`` is the ARMED ``(question_number, tg_message_id)``
        # once ``open_anchor_state`` confirms the candidate's anchor is
        # genuinely open-and-unanswered. Never persisted; reset every turn
        # boundary (``_reset_turn_state``).
        self._anchor_candidate: tuple[str, str] | None = None
        self._suppressing_for: tuple[int, int] | None = None
        # wb2-1 (whole-branch gate wave 2): arming is now POSITIVELY bound — a
        # candidate arms ONLY on the anchor its OWN ask produced (the seam's
        # ``source_hash`` matches the candidate's block hash — see
        # ``_effective_open_state``). This supersedes wave-1's negative
        # ``_disarmed_mids`` set (which excluded already-flushed mids but could not
        # tell a candidate's OWN cross-turn anchor from an unrelated one) — ONE
        # mechanism now (the r17 single-mechanism lesson), so that set is gone.
        # D5 Task C2: the anchor-scoped NARRATION BUFFER. While suppression is
        # armed, trailing prose is HELD here (never posted) as
        # ``(text, segment, offset_after)`` tuples — the frame coordinates travel
        # with the held text so Task C3 can add the persisted ``hold_pending``
        # marker + checkpoint-hold exemption on top WITHOUT restructuring. The
        # buffer FLUSHES (posts normally) on a later tool_use (which also DISARMS)
        # or an answer before ``result``; it is DISCARDED at ``result`` if the
        # anchor is still open-and-unanswered. In-memory only, reset every turn
        # boundary (``_reset_turn_state``).
        # §R3: each held frame carries its ``message.id`` (4th element) so the
        # flush re-derives the SAME per-message separators the live/replay paths
        # would, keeping the three consumers byte-parallel.
        self._anchor_buffer: list[tuple[str, list[int], int, str]] = []
        # D5 Task C3 (Sol r4-3): cold-replay DISARM latch. Set at cold start
        # when the persisted ``hold_pending`` marker is set — the recovered
        # turn's held frames lie BEYOND ``current`` and would otherwise re-
        # execute the suppression logic on catch-up (durable anchor ⇒
        # re-buffered-and-discarded ⇒ lost). While latched, no anchor candidate
        # is recorded and suppression never arms, so the previously-buffered
        # prose re-renders as ORDINARY narration (at-least-once resurface). It
        # clears at the recovered turn's boundary (``_reset_turn_state`` at
        # ``result`` / abnormal ``spawn`` / retention gap), after which normal
        # arming resumes. In-memory only; ``False`` on warm re-entry.
        self._replay_disarmed = False
        # #538: cold-replay DISCARD latch — the tombstone's in-memory face
        # (the dead range's ``from`` coordinate, or ``None``). Set at cold
        # start iff BOTH ``hold_pending`` and ``hold_discarded`` are durable:
        # the recovered turn's held frames were already ruled dead at
        # ``result``, so recovery DROPS text for frames at/past ``from``
        # (checkpoint still advances) instead of the disarm latch's resurface
        # rendering — in the live catch-up commit sites AND the replay/
        # wire-high-water reconstruction sites, so a second crash mid-recovery
        # cannot resurrect the range through ``_replay_text``. Dies with
        # ``_replay_disarmed`` at the recovered turn's boundary
        # (``_reset_turn_state`` / abnormal ``spawn``) so a later turn's
        # narration is never muted. In-memory only; ``None`` on warm re-entry.
        self._replay_discard: dict | None = None
        # #538 (Sol r2): positional latch — True once the chronological scan
        # has reached the tombstone's ``from`` coordinate (see
        # ``_latch_discard_range``). Lives and dies with ``_replay_discard``.
        self._discard_latched = False

    # -- persistence helpers ------------------------------------------------

    def _save(self) -> None:
        self.cursor.save(self.cursor_path)

    def _arm_hold_marker(self) -> None:
        """§D5 write-ahead (Sol r5-2): durably persist ``hold_pending=True``
        BEFORE the first frame's text is treated as held. Buffer-then-persist
        would leave a crash window where the marker is false, cold replay
        re-suppresses, and the prose is permanently lost — the exact path this
        marker exists to close. The ``_save`` here persists the marker with
        ``current`` still BEHIND the held frames (the held frame does not
        checkpoint), so a crash after it leaves marker-True/checkpoint-held —
        cold recovery then disarms and resurfaces. Idempotent: a no-op once the
        marker is already set (the buffer is already held)."""
        if not self.cursor.hold_pending:
            self.cursor.hold_pending = True
            # #538: a NEW hold generation starts with the tombstone reset —
            # a stale ``hold_discarded`` surviving from a prior turn (e.g.
            # left behind by the retention-gap floor before it cleared both)
            # would otherwise DROP this hold's prose on a crash-before-result
            # recovery that must RESURFACE it.
            self.cursor.hold_discarded = None
            self._save()

    def _checkpoint(self, seg, off_after: int) -> None:
        """Advance ``current`` past a fully-handled (usually invisible) frame."""
        self.cursor.current = {"segment": list(seg), "offset": off_after}
        self._save()

    def _advance_dropped(self, seg, off_after: int) -> None:
        coord = {"segment": list(seg), "offset": off_after}
        self.cursor.current = coord
        self.cursor.dropped_through = dict(coord)
        self._save()

    def _sync_last_len(self) -> None:
        """Keep ``message_text_lens`` AND ``sep_stripped`` parallel to
        ``message_ids`` with the lens' last entry == the current message's
        narration length (the additive replay-boundary field). Prior entries are
        frozen at each message's final text length, including a SEALED message
        the live path rolled off of. P2: sep_stripped is PADDED with ``False``
        for any id lacking a flag (never overwritten — the flag is authored once
        at ``_open_message`` time), so this and the openers keep all three lists
        parallel via ONE routine (Sol r3-MAJOR: clearing message_ids while
        leaving flags would misalign the next turn)."""
        lens = self.cursor.message_text_lens
        flags = self.cursor.sep_stripped
        ids = self.cursor.message_ids
        if len(lens) > len(ids):
            del lens[len(ids):]
        if len(flags) > len(ids):
            del flags[len(ids):]
        while len(lens) < len(ids):
            lens.append(0)
        while len(flags) < len(ids):
            flags.append(False)
        if ids:
            lens[-1] = len(self._per_message_text)

    # -- P2 message-list + separator-provenance helpers ---------------------

    def _open_message(self, mid, text: str, *, sep_stripped: bool) -> None:
        """Open a NEW narration message ATOMICALLY (Sol r2-3): append its id +
        its authored ``sep_stripped`` flag, adopt it as the current message
        (``_per_message_text``/``_posted_len``), and sync the parallel lens.
        The ONE opener the live commit paths + reconcile route through."""
        self.cursor.message_ids.append(mid)
        self.cursor.sep_stripped.append(bool(sep_stripped))
        self._per_message_text = text
        self._posted_len = len(text)
        self._sync_last_len()

    def _reset_message_lists(self) -> None:
        """Clear message_ids/message_text_lens/sep_stripped TOGETHER (turn
        close/reset/gap) so no turn boundary can leave the flags misaligned."""
        self.cursor.message_ids = []
        self.cursor.message_text_lens = []
        self.cursor.sep_stripped = []

    def _seps_append(self, offset: int) -> None:
        self._pending_seps.append(offset)

    def _seps_consume_below(self, posted_len: int) -> None:
        """CONSUME descriptors whose bytes are now on the wire (offset <
        posted_len) — coordinates otherwise unchanged (applied-edit path)."""
        self._pending_seps = [s for s in self._pending_seps if s >= posted_len]

    def _seps_rebase_to(self, new_start: int) -> None:
        """Op-scoped rebasing (Terra r4-MINOR): a rollover send opens a new
        message starting at source offset ``new_start`` — CONSUME descriptors in
        the already-sealed prefix and subtract the new message's start from the
        rest (no content inspection, exact parity in long throttled tails)."""
        self._pending_seps = [
            s - new_start for s in self._pending_seps if s >= new_start
        ]

    def _seps_clear(self) -> None:
        self._pending_seps = []

    # -- coordinate ordering ------------------------------------------------

    def _seg_rank(self, seg: tuple[int, int]) -> int:
        for i, (_p, ident) in enumerate(_ordered_segments(self.log_dir)):
            if ident == seg:
                return i
        return 1 << 30

    def _coord_le(self, seg, off_after: int, target: dict) -> bool:
        tseg = tuple(target.get("segment", (0, 0)))
        toff = int(target.get("offset", 0))
        st = tuple(seg)
        if st == tseg:
            return off_after <= toff
        # §A1(3) comparison-level absence rule (Sol A1 review): when the target's
        # segment is NOT the frame's segment AND the target segment is absent from
        # disk (``_seg_rank`` == the sentinel), its data is gone — no readable
        # frame can be ``<=`` it, so never mute. This replaces the racy cold-start
        # ``dropped_through`` clear: it fixes the WARM path too (a live marker
        # whose segment rotates out mid-run no longer rank-infinity-mutes every
        # frame) and never mutates persisted state from a scan. Accepted rare
        # residual: during a rotation race a transient ``_ordered_segments`` miss
        # lets at most the frames read in that window through (one stray message
        # class) — preferred over permanently muting the relay or clearing a live
        # marker.
        if self._seg_rank(tseg) == (1 << 30):
            return False
        return self._seg_rank(st) < self._seg_rank(tseg)

    def _latch_discard_range(self, seg, off_after: int) -> None:
        """#538 (Sol r2): POSITIONAL entry into the tombstoned held range.
        The frame scan is chronological, so the latch flips exactly when the
        scan reaches the tombstone's ``from`` coordinate — never by coordinate
        COMPARISON (``_coord_le``'s absent-segment rule ranks a frame whose
        source archive was unlinked mid-recovery at infinity, which would
        misclassify a chronologically EARLIER frame as dead and lose unposted
        pre-anchor prose). A ``from`` the scan never reaches (e.g. its segment
        rotated out) leaves the latch unfired and the prose resurfaces —
        failing toward a bounded duplicate, never toward loss. Called for
        EVERY frame at ``_handle_frame`` entry; dies with ``_replay_discard``
        at the recovered turn's boundary."""
        rd = self._replay_discard
        if rd is None or self._discard_latched:
            return
        fr = rd.get("from") or {}
        if (
            tuple(seg) == tuple(fr.get("segment", (0, 0)))
            and off_after == int(fr.get("offset", 0))
        ):
            self._discard_latched = True

    def _in_discarded_range(self, seg, off_after: int) -> bool:
        """#538: is this frame inside the tombstoned (result-time DISCARDED)
        held range? Purely the positional latch — frames scanned BEFORE the
        ``from`` coordinate (posted or throttled-but-landed pre-anchor
        narration) are genuine wire content the reconstruction and the re-run
        closing edit still need."""
        return self._replay_discard is not None and self._discard_latched

    def _update_live(self, seg, off_after: int) -> None:
        """Latch REPLAY→LIVE using monotonic segment order (turn_start<=current)."""
        if self._live:
            return
        cur_seg = tuple(self.cursor.current.get("segment", (0, 0)))
        cur_off = int(self.cursor.current.get("offset", 0))
        st = tuple(seg)
        if st == cur_seg:
            if off_after > cur_off:
                self._live = True
            self._passed_cur_seg = True
        else:
            if self._passed_cur_seg:
                self._live = True

    # -- replay-mode text reconstruction -----------------------------------

    def _apply_msg_sep(
        self, message_id: str, text: str, *, record: bool = False,
    ) -> str:
        """§R3: prefix *text* with a ``\\n\\n`` separator iff *message_id* begins
        a NEW assistant message (differs from the last-appended text frame's id
        AND a segment was already appended this turn), then advance the recorded
        id. THE single narration-boundary decision — a structural ``message.id``
        change, never content inspection — shared by every LIVE narration-commit
        path so they advance ``_last_text_msg_id`` and the narration byte-stream
        IDENTICALLY. Under the CLI contract each text frame has a distinct id, so
        this fires before every segment after the first.

        P2 (F-LEADSEP, r6) — LIVE-COMMIT-ONLY provenance: when *record* is set
        (every LIVE commit path) an injected separator appends a descriptor at
        ``len(_per_message_text)`` — the exact offset the sep will occupy once
        the ops apply. Replay NEVER records (``record`` defaults False): its
        ``_apply_msg_sep`` reconstructs already-posted bytes and reads the
        persisted ``sep_stripped`` flags instead, so a historical separator can
        never masquerade as a live pending one (Sol/Terra r5-BLOCKER)."""
        if self._last_text_msg_id is not None and message_id != self._last_text_msg_id:
            if record:
                self._seps_append(len(self._per_message_text))
            text = _SEP + text
        self._last_text_msg_id = message_id
        return text

    def _replay_sep_at_boundary(self) -> str:
        """P2 replay skip-decision at a reconstructed ``message.id`` boundary.

        Return ``""`` (skip) iff this boundary begins recorded message ordinal
        ``j`` EXACTLY (the reconstructed position equals ``j``'s cumulative start
        in ``message_text_lens``) AND ``sep_stripped[j]`` — else ``\\n\\n``. The
        ordinal invariant (Sol/Terra r2-MAJOR): ``_replay_j`` counts completed
        recorded messages consumed; each boundary is consumed ONCE; the flag
        lookup is ``sep_stripped[j] if 0 <= j < len(sep_stripped) else False`` —
        NEVER clamped or rebound (Sol r4/r5). Consecutive legacy zero-length lens
        entries share a position and are consumed flag-False (never a skip)."""
        lens = self.cursor.message_text_lens
        flags = self.cursor.sep_stripped
        pos = len(self._turn_text)
        sep = _SEP  # default: a mid-message id change, not a recorded boundary
        while self._replay_j + 1 <= len(lens):
            cum = sum(lens[: self._replay_j + 1])
            if cum > pos:
                break
            self._replay_j += 1
            if cum == pos:
                j = self._replay_j
                stripped = flags[j] if 0 <= j < len(flags) else False
                sep = "" if stripped else _SEP
        return sep

    def _replay_text(self, text: str, message_id: str = "") -> None:
        """Rebuild ``per_message_text`` from a replayed text block — no sends.

        When the checkpoint recorded per-message narration boundaries
        (``message_text_lens``, the additive field), the reconstructed turn text
        is split at THOSE boundaries so a discrete-rollover message keeps only
        its own text (the last message's slice becomes ``per_message_text``).
        Legacy checkpoints (field absent) fall back to _MSG_MAX-only splitting —
        their recovery is already covered by the conservative seal.

        §R3/P2: the replayed frame is prefixed with the SAME ``\\n\\n`` separator
        the live path inserted at a ``message.id`` boundary — EXCEPT where the
        live path STRIPPED it at a sealed-open (``sep_stripped[j]``), in which
        case the boundary skip-decision omits it so the reconstructed
        ``_turn_text`` (the only READ of ``_turn_text``) matches the live
        narration byte-for-byte and the ``message_text_lens`` split lands
        identically. Replay records NO separator descriptor (LIVE-COMMIT-ONLY).
        """
        is_boundary = (
            self._last_text_msg_id is not None
            and message_id != self._last_text_msg_id
        )
        self._last_text_msg_id = message_id
        if is_boundary:
            text = self._replay_sep_at_boundary() + text
        self._turn_text += text
        lens = self.cursor.message_text_lens
        if lens:
            # Boundary-aware: redistribute the whole replayed turn text across
            # the recorded per-message lengths; the trailing slice is the
            # current (last) message's narration.
            full = self._turn_text
            pos = 0
            count = 0
            pmt = ""
            for n in lens:
                if pos >= len(full) and count:
                    break
                pmt = full[pos:pos + n]
                pos += len(pmt)
                count += 1
            if pos < len(full):
                # Residual beyond the recorded boundaries → trailing message
                # (should not occur at the checkpoint; tolerated defensively).
                pmt = pmt + full[pos:]
            self._per_message_text = pmt
            self._replay_msg_count = count
            return
        # Legacy fallback: _MSG_MAX-only reconstruction (unchanged behavior).
        pmt = self._per_message_text
        if self._replay_msg_count == 0:
            head, text = text[:_MSG_MAX], text[_MSG_MAX:]
            pmt = head
            self._replay_msg_count += 1
        else:
            space = _MSG_MAX - len(pmt)
            if space > 0 and text:
                head, text = text[:space], text[space:]
                pmt = pmt + head
        while text:
            piece, text = text[:_MSG_MAX], text[_MSG_MAX:]
            pmt = piece
            self._replay_msg_count += 1
        self._per_message_text = pmt

    async def _reconcile(self) -> None:
        """Recover a lost-before-persist edit for the LAST message (§2:471).

        Only when ``message_ids`` is non-empty (an OPEN turn): a closed-turn
        checkpoint has ``message_ids == []`` so there is nothing to reconcile.
        Routes through ``edit_narration_if_latest`` (§2 sealing across restart,
        option B): on a fresh post-recovery sequencer the checkpoint-named
        message is CONSERVATIVELY SEALED, so the reconciled state posts as a NEW
        closing message rather than editing a message that may have discrete
        posts under it. Any single duplicate is accepted (the documented
        at-least-once risk).
        """
        self._reconciled = True
        # P2 replay→live boundary invariant (Sol/Terra r5-BLOCKER): replay never
        # records a separator descriptor, so the pending list is EMPTY here — a
        # historical already-posted separator can never survive to strip authored
        # bytes at a sealed continuation.
        assert not self._pending_seps, (
            "pending separator descriptors leaked into replay→live transition"
        )
        # #523: a persisted wire-high-water marker means the turn's CLOSING
        # edit landed — nothing was lost-before-persist on this message, and
        # the reconstruction here is necessarily BEHIND the wire (the suffix
        # frames past ``current`` have not replayed yet). Editing the wire
        # down to the reconstruction would TRUNCATE the landed suffix; skip
        # entirely and let the marker's adoption (``_handle_frame``) converge
        # the high-waters at the recorded coordinate.
        if self.cursor.wire_hw is not None:
            return
        if not (self.cursor.message_ids and self._per_message_text):
            return
        try:
            res = await self._apply_seq_edit(
                self.cursor.message_ids[-1], self._per_message_text,
            )
            if res == "sealed":
                # §A1(2) delta-aware cold reconcile: on a fresh post-recovery
                # sequencer the checkpoint-named message is CONSERVATIVELY
                # SEALED. Repost ONLY the genuinely-unposted tail past the
                # PERSISTED wire high-water — never the already-visible prefix
                # (the production F-DUP reposted the WHOLE tail here). Empty
                # pending (a fully-posted sealed tail) reposts NOTHING. A legacy
                # checkpoint (``last_posted_len`` 0) degrades to today's full
                # repost; a lost-cursor-persist window degrades to a small
                # suffix duplicate — both at-least-once, both cold-path-only.
                pending = self._per_message_text[self.cursor.last_posted_len:]
                if pending:
                    applied, mid = await self._apply_op(
                        lambda p=pending: self.sequencer.open_narration(p)
                    )
                    if applied:
                        # Adopt the delta message exactly like ``_execute_ops``'
                        # sealed branch, which APPENDS — the state must stay
                        # REPLAY-CONVERGENT because ``turn_start`` still covers the
                        # FULL turn (a cold restart re-reads and reconstructs the
                        # WHOLE narration, then re-splits it at ``message_text_lens``
                        # boundaries). FREEZE the old current message's boundary at
                        # the WIRE truth (its visible prefix == ``last_posted_len``)
                        # and APPEND the delta message carrying ONLY ``pending``.
                        # On the next restart the trailing slice re-derives EXACTLY
                        # ``pending`` and the persisted ``last_posted_len ==
                        # len(pending)`` makes that restart's pending empty (posts
                        # NOTHING) — so successive restarts converge instead of
                        # re-posting overlapping fragments forever. (Replacing the
                        # boundaries here, as the pre-fix code did, left
                        # ``turn_start`` covering the full turn but the boundaries
                        # sized to the delta, so replay mis-split the reconstructed
                        # text and the next reconcile re-posted a shifted overlap.)
                        lens = self.cursor.message_text_lens
                        flags = self.cursor.sep_stripped
                        ids = self.cursor.message_ids
                        if len(lens) > len(ids):
                            del lens[len(ids):]
                        if len(flags) > len(ids):
                            del flags[len(ids):]
                        while len(lens) < len(ids):
                            lens.append(0)
                        while len(flags) < len(ids):
                            flags.append(False)
                        if ids:
                            lens[-1] = self.cursor.last_posted_len
                        # P2: the delta message carries no INJECTED separator (a
                        # raw wire-truth slice), so its flag is False; open it via
                        # the shared atomic opener so all three lists stay parallel.
                        self._open_message(mid, pending, sep_stripped=False)
                        self.cursor.last_posted_len = len(pending)
                    else:
                        # Dropped mid-reconcile: the tail never reached the wire,
                        # so the persisted wire high-water is still the truth.
                        self._posted_len = self.cursor.last_posted_len
                else:
                    # Fully-posted sealed tail — the wire already carries the
                    # full narration on ``message_ids[-1]``; record it as the
                    # wire high-water so a subsequent live edit computes the
                    # correct increment (W-R4).
                    self._posted_len = len(self._per_message_text)
                    self.cursor.last_posted_len = len(self._per_message_text)
            else:
                # APPLIED in-place edit: the editable message now carries the
                # FULL narration on the wire.
                self._sync_last_len()
                self._posted_len = len(self._per_message_text)
                self.cursor.last_posted_len = len(self._per_message_text)
            self._save()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "topic stream reconcile edit failed for engagement %s: %s",
                self.engagement_id, exc,
            )

    # -- live posting -------------------------------------------------------

    def _backoff(self) -> float:
        return min(_BACKOFF_MAX, _BACKOFF_BASE * (2 ** min(self._fail_count, 5)))

    def _enter_drop(self) -> None:
        if not self._drop_warned:
            self._drop_warned = True
            logger.warning(
                "topic stream for engagement %s: dropping remainder of turn "
                "after %d consecutive Telegram failures",
                self.engagement_id, self._fail_count,
            )
        self._dropped = True
        # P2: drop mode discards the unposted tail — clear pending separators so
        # no stale descriptor survives into a later commit (Terra r2-MAJOR).
        self._seps_clear()

    async def _apply_op(self, factory: Callable[[], Any]) -> tuple[bool, Any]:
        """Run one Telegram op, retrying with bounded backoff until it succeeds
        or the turn crosses ``_DROP_THRESHOLD`` consecutive failures (→ drop).

        Returns ``(applied, result)``; ``applied is False`` means we entered
        drop mode and gave up on this op.
        """
        while True:
            try:
                res = await _maybe_await(factory())
                # wb3-2: the sequencer DISCARDED this write (engagement terminal)
                # — a clean stop, NOT a wire failure: no retry, no drop-mode,
                # no fail-count bump. The caller treats ``applied is False`` as
                # "nothing landed" and advances without reposting.
                if res is DISCARDED:
                    return False, None
                ok = res is not None and res is not False
            except Exception as exc:  # noqa: BLE001
                ok = False
                res = None
                logger.debug("topic stream op failed (will retry): %s", exc)
            if ok:
                self._fail_count = 0
                return True, res
            self._fail_count += 1
            if self._fail_count >= _DROP_THRESHOLD:
                self._enter_drop()
                return False, None
            await self._sleep(self._backoff())

    async def _apply_seq_edit(self, msg_id: int, value: str) -> str:
        """Route a narration edit through the sequencer's
        ``edit_narration_if_latest`` while honoring the at-least-once
        retry/drop contract. Returns ``"applied"`` | ``"sealed"`` |
        ``"dropped"``.

        ``SEALED`` (something posted below this message — §2 rollover) is NOT a
        failure: the caller opens a fresh narration message for the pending
        text. Only a ``FAILED`` wire edit counts toward the drop threshold.

        wb3-2: ``DISCARDED`` (the engagement terminalized) returns ``"discarded"``
        — the caller must NOT treat it as SEALED (which would repost the tail
        below the terminal completion); it discards cleanly instead.
        """
        while True:
            try:
                res = await _maybe_await(
                    self.sequencer.edit_narration_if_latest(msg_id, value)
                )
            except Exception as exc:  # noqa: BLE001
                res = FAILED
                logger.debug("topic stream edit op failed (will retry): %s", exc)
            if res == DISCARDED:
                self._fail_count = 0
                return "discarded"
            if res == APPLIED:
                self._fail_count = 0
                return "applied"
            if res == SEALED:
                self._fail_count = 0
                return "sealed"
            self._fail_count += 1
            if self._fail_count >= _DROP_THRESHOLD:
                self._enter_drop()
                return "dropped"
            await self._sleep(self._backoff())

    def _plan_ops(self, text: str) -> list[tuple[str, str]]:
        """Ops to render *text* appended to the current message with rollover.

        First text of a turn → one ``send``; otherwise fill the current message
        via one ``edit``; any surplus past ``_MSG_MAX`` splits into ``send``s.
        """
        ops: list[tuple[str, str]] = []
        pmt = self._per_message_text
        if not self.cursor.message_ids:
            head, text = text[:_MSG_MAX], text[_MSG_MAX:]
            ops.append(("send", head))
            pmt = head
        else:
            space = _MSG_MAX - len(pmt)
            if space > 0 and text:
                head, text = text[:space], text[space:]
                pmt = pmt + head
                ops.append(("edit", pmt))
        while text:
            piece, text = text[:_MSG_MAX], text[_MSG_MAX:]
            ops.append(("send", piece))
            pmt = piece
        return ops

    async def _execute_ops(
        self, ops: list[tuple[str, str]], complete_target: str | None = None,
    ) -> None:
        """Render *ops* (send/edit) through the sequencer. Sets ``self._dropped``
        on a drop; NEVER checkpoints (the caller owns cursor advancement).

        P2: *complete_target* is the COMPLETE LOGICAL TARGET of this commit —
        ``_per_message_text`` (before the append) + the sep-applied text — the
        SAME coordinate system the pending separator descriptors live in. The
        sealed re-plan reads the WHOLE tail from it and never from a single
        sealed op's fragment (Sol r4-BLOCKER; makes the ``_MSG_MAX−1/−2``
        rollover-boundary degeneracies unrepresentable). ``cum`` tracks each op's
        source start in that target (derived so a full-message rollover, whose
        already-posted prefix is NOT among the ops, still aligns) for op-scoped
        descriptor rebasing. When *complete_target* is omitted (direct-call tests
        that never touch descriptors) it defaults to the ops' concatenation."""
        if complete_target is None:
            complete_target = "".join(value for _kind, value in ops)
        # Source start of the FIRST op: the part of the target the ops do NOT
        # re-render (a full-message rollover's already-posted prefix) is exactly
        # ``len(target) − Σ len(op values)``.
        cum = len(complete_target) - sum(len(value) for _kind, value in ops)
        for kind, value in ops:
            if self._dropped:
                return
            if kind == "send":
                applied, mid = await self._apply_op(
                    lambda v=value: self.sequencer.open_narration(v)
                )
                if not applied:
                    return
                # A send opens a NEW message at source offset ``cum``: consume
                # descriptors in the already-sealed prefix, rebase the rest
                # relative to the new message's start (never strip — Terra
                # r4-MINOR), then consume any that this fully-posted send just put
                # on the wire.
                self._seps_rebase_to(cum)
                self._open_message(mid, value, sep_stripped=False)
                self._seps_consume_below(self._posted_len)
            else:  # edit
                res = await self._apply_seq_edit(
                    self.cursor.message_ids[-1], value
                )
                if res == "dropped":
                    return
                if res == "discarded":
                    # wb3-2: the engagement terminalized mid-edit — stop cleanly,
                    # post nothing more below the terminal completion.
                    self._seps_clear()
                    return
                if res == "sealed":
                    # P2 FULL-TAIL RE-PLAN (Sol r4-BLOCKER): the message was
                    # sealed (a discrete posted below it). DISCARD the remaining
                    # old ops and re-plan the COMPLETE tail from ``_posted_len``,
                    # stripping the leading injected separator iff its descriptor
                    # sits exactly at the tail's head. Never resume the old loop.
                    await self._replan_sealed(complete_target)
                    return
                # applied: the whole edit value reached the wire — its
                # descriptors are now immutable; coordinates otherwise unchanged.
                self._per_message_text = value
                self._posted_len = len(value)
                self._seps_consume_below(self._posted_len)
                self._sync_last_len()
                self._last_edit_ts = self._now()
            cum += len(value)

    async def _replan_sealed(self, complete_target: str) -> None:
        """P2 sealed-open re-plan (r5). Post the COMPLETE logical tail past the
        wire high-water as a fresh message chain — FIRST op always a ``send`` —
        stripping the one leading injected separator iff the earliest pending
        descriptor sits at the tail's head. The first opened message records
        ``sep_stripped=True`` iff the strip fired; every rollover continuation
        records ``False``. After the tail is posted ALL pending descriptors are
        consumed (Terra r3-BLOCKER: no descriptor survives to remap against a
        later ``_posted_len``)."""
        tail = complete_target[self._posted_len:]
        # Strip iff the earliest injected separator begins the tail (provenance,
        # never content matching).
        strip = bool(self._pending_seps) and self._pending_seps[0] == self._posted_len
        if strip:
            candidate = tail[len(_SEP):]
            if candidate:
                tail = candidate
            else:
                # Empty-after-strip defensive floor (Sol r2-2): NEVER strip to
                # empty — its failure direction is the old cosmetic bug, never a
                # parity break. (Unreachable given the non-empty-text guard.)
                strip = False
        if not tail:
            self._seps_clear()
            return
        first = True
        while tail:
            piece, tail = tail[:_MSG_MAX], tail[_MSG_MAX:]
            applied, mid = await self._apply_op(
                lambda v=piece: self.sequencer.open_narration(v)
            )
            if not applied:
                return
            self._open_message(mid, piece, sep_stripped=(strip and first))
            first = False
        self._seps_clear()

    async def _commit_narration(self, message_id: str, text: str) -> None:
        """P2: the ONE shared LIVE narration append routine — apply the R3
        separator (recording its provenance descriptor), then execute the planned
        ops. Used by the mixed-frame path, the atomic hold-or-post poster, and
        the per-tuple held-buffer flush so all commit identically."""
        if not text:
            return
        sep_applied = self._apply_msg_sep(message_id, text, record=True)
        complete_target = self._per_message_text + sep_applied
        await self._execute_ops(self._plan_ops(sep_applied), complete_target)

    async def _post_text(
        self, text: str, seg, off_after: int, message_id: str = "",
    ) -> None:
        """Text-only streaming path: throttle + single frame-end checkpoint."""
        # wb2-3: once the engagement is TERMINAL (settle_all_open_questions closed
        # the anchor ledger; this relay stays alive until completion posting),
        # FORBID further narration writes — nothing may post below the terminal
        # completion (D5 discard doctrine). Drop the text and advance the cursor so
        # the frame is not re-read on the next poll.
        if self._is_terminal():
            # P2: terminalize is a clearing site — no narration will post below
            # the terminal completion, so drop any pending separator descriptors.
            self._seps_clear()
            self._checkpoint(seg, off_after)
            return
        # P2 empty-text guard (Sol r2-2): a text-less frame never reaches a
        # commit — checkpoint and return, so a sealed increment is always
        # ``sep + non-empty`` and post-strip text is never empty. No-behavior
        # change (the caller already gates on ``narr.text``); belt-and-suspenders.
        if not text:
            self._checkpoint(seg, off_after)
            return
        # #538: tombstone catch-up — this text frame is inside a held range
        # already ruled DISCARDED at the crashed run's ``result``. Drop the
        # text (never buffer, never post) but still advance the checkpoint so
        # the frame is not re-read; the latch dies at the recovered turn's
        # boundary.
        if self._in_discarded_range(seg, off_after):
            self._checkpoint(seg, off_after)
            return
        await self._maybe_arm_suppression()
        # §D5: armed (anchor open) — trailing prose is BUFFERED, never posted
        # here. Flushed on a later tool_use / an answer before ``result``, or
        # DISCARDED at ``result`` if the anchor is still open. A held frame does
        # NOT checkpoint (mirrors the throttle-hold below) so a crash can never
        # strand the held prose past an advanced cursor. §D5 write-ahead (C3):
        # persist ``hold_pending`` BEFORE holding, so a crash here re-renders the
        # prose (disarmed) on cold recovery instead of re-suppressing it.
        if self._suppressing_for is not None:
            self._arm_hold_marker()
            self._anchor_buffer.append((text, list(seg), off_after, message_id))
            return
        # ``_turn_text`` is a WRITE-ONLY replay scratch on the LIVE path (its only
        # READ is in ``_replay_text``, which rebuilds its own copy on cold
        # recovery), so the §R3 separator is applied where it is load-bearing —
        # the posting ops below (``_per_message_text`` / ``message_text_lens``)
        # and the replay reconstruction. Keeping this append RAW also avoids
        # committing the boundary decision BEFORE the atomic hold-or-post branch
        # (a HOLD must NOT advance ``_last_text_msg_id`` — its separator is
        # deferred to the flush).
        self._turn_text += text
        if self._dropped:
            self._advance_dropped(seg, off_after)
            return

        # §D5 r4-2: a pending, not-yet-armed anchor-candidate — the anchor may be
        # posting LATE (out-of-band). Route the post through the sequencer's
        # ATOMIC read-decide-write so the seam re-read + narration post share the
        # ONE lock the late poster uses; a hold buffers + arms (no checkpoint).
        # The §R3 separator is applied INSIDE the poster (POST) / deferred to the
        # flush (HOLD), so the boundary decision follows the atomic outcome.
        if self._anchor_candidate is not None and self.open_anchor_state is not None:
            if await self._atomic_post_or_hold(text, seg, off_after, message_id):
                return
        else:
            # §R3/P2: prefix the narration separator when this frame begins a NEW
            # assistant message (the shared boundary decision), recording its
            # provenance descriptor on this LIVE commit path.
            sep_applied = self._apply_msg_sep(message_id, text, record=True)
            complete_target = self._per_message_text + sep_applied
            ops = self._plan_ops(sep_applied)
            if not ops:
                self._checkpoint(seg, off_after)
                return

            # Throttle only the rapid single-edit streaming case: skip the live
            # edit within the window (text is retained in memory and flushed by
            # the next edit or at finalize), and do NOT advance the cursor. P2:
            # coordinates are unchanged (same string grows at the tail), so the
            # pending separator descriptor is RETAINED across the hold.
            if len(ops) == 1 and ops[0][0] == "edit":
                if self._now() - self._last_edit_ts < self._edit_throttle:
                    self._per_message_text = ops[0][1]
                    return

            await self._execute_ops(ops, complete_target)
        if self._dropped:
            self._advance_dropped(seg, off_after)
            return

        self.cursor.current = {"segment": list(seg), "offset": off_after}
        # §A1 (Sol r2-1a): persist the WIRE high-water (``_posted_len``), NOT
        # ``len(self._per_message_text)`` — the two are equal on this happy path,
        # but diverge under a throttled hold; the delta reconcile slices at this
        # value, so an inflated length would LOSE the held suffix on recovery.
        self.cursor.last_posted_len = self._posted_len
        self._save()

    async def _append_narration(
        self, text: str, seg, off_after: int, message_id: str = "",
    ) -> None:
        """Append narration text WITHOUT throttle or checkpoint (used by the
        block-ordered mixed-frame path, which checkpoints once at frame end)."""
        # wb2-3: terminal ⇒ forbid narration writes (see ``_post_text``). The
        # frame-end checkpoint in ``_handle_assistant_blocks`` advances the cursor.
        if self._is_terminal():
            return
        # P2 empty-text guard (Sol r2-2): a text-less block never reaches a
        # commit (mixed-frame commit site). No-behavior change.
        if not text:
            return
        # #538: tombstone catch-up (mixed-frame commit site) — drop the dead
        # held text; the frame's tool blocks still process and the frame-end
        # checkpoint in ``_handle_assistant_blocks`` advances (the buffer
        # stays empty, so the hold exemption never fires).
        if self._in_discarded_range(seg, off_after):
            return
        await self._maybe_arm_suppression()
        # §D5: armed — BUFFER (see ``_post_text``). The frame-end checkpoint in
        # ``_handle_assistant_blocks`` is EXEMPTED while the buffer is non-empty.
        # Write-ahead the ``hold_pending`` marker before holding (C3).
        if self._suppressing_for is not None:
            self._arm_hold_marker()
            self._anchor_buffer.append((text, list(seg), off_after, message_id))
            return
        # ``_turn_text`` is write-only on the live path (see ``_post_text``); the
        # §R3 separator is applied to the posting ops / poster below.
        self._turn_text += text
        if self._dropped:
            return
        # §D5 r4-2 atomic hold-or-post for a pending, not-yet-armed candidate.
        if self._anchor_candidate is not None and self.open_anchor_state is not None:
            await self._atomic_post_or_hold(text, seg, off_after, message_id)
            return
        # §R3/P2: commit through the ONE shared LIVE append routine (separator +
        # provenance descriptor + planned ops).
        await self._commit_narration(message_id, text)

    async def _match_discrete_block(self, name: str, tool_input: dict) -> None:
        """Drive relay-mediated discrete matching for ONE tool_use block (§2(3)).

        Computes the block's ``(tool, projection_hash)`` under the pinned
        projection and asks the sequencer to resolve it at this position. An
        armed intent posts here (sealing narration); a tombstone / debt consumes
        the block silently; a pending/absent hold-eligible intent may hold the
        slot. The T1-stubbed state (no intents registered) resolves to
        ``no_match`` instantly, so this is inert until T2/T3 wire the
        ingresses."""
        try:
            block_hash = projection_hash(name, tool_input)
        except ValueError:
            # Non-serializable tool_input can never match a hashed intent.
            return
        try:
            status = await self.sequencer.post_for_block(name, block_hash)
        except Exception as exc:  # noqa: BLE001 — discrete posting is best-effort
            logger.warning(
                "topic stream discrete-post match failed for engagement %s "
                "(tool=%s): %s", self.engagement_id, name, exc,
            )
            return
        self._log_oob_match(name, block_hash, status)
        # D5 (Sol r3-6, "Arming survives out-of-band posting"): every matching
        # free-text-anchor ask block (the tool's own bare-question, no
        # ``options`` — a button ask blocks the turn anyway, spec §D5 scope)
        # records a per-turn anchor-candidate REGARDLESS of
        # ``post_for_block``'s return. ``"posted"`` alone is NOT proof the
        # anchor is genuinely open (an armed intent reports ``"posted"`` even
        # when its poster recorded a FAILURE outcome — §D5 "Successful-anchor
        # identity comes from the DRIVER"); ``"slot_timeout"``/
        # ``"debt_consumed"`` cover the late/out-of-band posting orderings
        # (§D5 "Arming survives out-of-band posting"). The candidate is only
        # the TRIGGER to re-consult the driver-injected ``open_anchor_state``
        # seam — the actual ARM decision trusts ONLY the seam (see
        # ``_maybe_arm_suppression``, called before the next text frame and
        # again at ``result``).
        # §D5 C3 (Sol r4-3): while cold-replay DISARMED (recovering a held-prose
        # turn), record NO candidate — the previously-buffered prose must
        # re-render as ordinary narration on catch-up, so neither arming nor the
        # r4-2 atomic hold-path (both gated on a candidate) may fire until the
        # recovered turn's boundary clears the latch.
        if (
            name == ASK_TOOL
            and not tool_input.get("options")
            and status in ("posted", "slot_timeout", "debt_consumed")
            and not self._replay_disarmed
        ):
            self._anchor_candidate = (name, block_hash)

    def _log_oob_match(self, tool_name: str, block_hash: str, status: str) -> None:
        """F-OOB instrumentation (spec D7): content-free log at the
        discrete-post MATCH POINT (``_match_discrete_block``).

        Carries ONLY the pinned projection-hash PREFIX (8 hex — never the
        projected args/question/options text), the block-resolution result
        (``posted``/``slot_timeout``/``debt_consumed``/``no_match``/
        ``consumed_cancelled``, verbatim from ``post_for_block``), the
        matched intent's state (or ``none`` when no intent was ever
        registered for this hash — a genuinely absent hold-eligible block),
        the tool name and engagement id (system identifiers, not operator
        content), and the registration-to-block latency in ms. Paired with
        ``OutputSequencer._log_late_post`` (the watcher-path counterpart for
        a ``slot_timeout`` block's eventual out-of-band post), the two log
        lines carry enough timing to reconstruct the observed F-OOB ~10s gap
        (Sol r1-8: likely the 2s slot hold + 10s intent timeout, not a hash
        defect) — instrumentation only, NO behavioral change.

        R6a (round-5 minors): every tool_use block drives this — including
        non-post tools (Read/Glob/ToolSearch/TaskCreate/...) that can NEVER
        match a hold-eligible intent and always resolve to a guaranteed-
        uninteresting ``no_match latency=-1.0``. Logging those at INFO buried
        the real F-OOB ``oob_late_post``/``slot_timeout`` signal, so only the
        HOLD-ELIGIBLE post tools (``ask``/``reply``/``emit_completion`` —
        ``HOLD_ELIGIBLE_TOOLS``) log at INFO; every other block logs the same
        line at DEBUG."""
        intent = self.sequencer.registry.peek(tool_name, block_hash)
        if intent is not None:
            intent_state = intent.state
            latency_ms = (self._now() - intent.registered_at) * 1000.0
        else:
            intent_state = "none"
            latency_ms = -1.0
        level = logging.INFO if tool_name in HOLD_ELIGIBLE_TOOLS else logging.DEBUG
        logger.log(
            level,
            "oob_match hash=%s result=%s intent_state=%s latency_ms=%.1f "
            "tool=%s engagement=%s",
            block_hash[:8], status, intent_state, latency_ms, tool_name,
            self.engagement_id,
        )

    async def _effective_open_state(self) -> "tuple[int, int] | None":
        """The driver-injected ``open_anchor_state`` (``(n, mid, source_hash) |
        None``) with wb2-1 POSITIVE candidate-binding applied: the reported anchor
        is eligible to arm / hold / discard THIS turn's prose ONLY when its
        ``source_hash`` matches the CURRENT anchor-candidate's block hash — i.e.
        the anchor was produced by the candidate's OWN ask, never a prior or
        co-existing anchor (the F-LEAK2 cross-turn residual: a refused/late ask
        recorded a candidate but never surfaced its own anchor, and a bare oldest-
        open read armed it off the still-open PRIOR anchor). Returns ``(n, mid)``
        on a match, else ``None`` — the single seam read shared by arming, the
        atomic hold-or-post, and the ``result``-time flush-vs-discard so all three
        agree. ``None`` when the seam is not injected, when there is no candidate
        to bind to, or when a legacy 2-tuple seam carries no ``source_hash``."""
        if self.open_anchor_state is None:
            return None
        state = await _maybe_await(self.open_anchor_state())
        if state is None:
            return None
        cand = self._anchor_candidate
        if cand is None:
            return None
        source_hash = state[2] if len(state) > 2 else None
        # POSITIVE binding: only the anchor THIS candidate's own ask produced.
        if source_hash is None or source_hash != cand[1]:
            return None
        return (state[0], state[1])

    def _is_terminal(self) -> bool:
        """wb2-3: ``True`` once the driver-injected terminal seam reports the
        engagement has flipped terminal (``settle_all_open_questions`` is closing
        the anchor ledger while this relay is still alive). Inert (``False``) when
        the seam is not injected."""
        if self.engagement_terminal is None:
            return False
        try:
            return bool(self.engagement_terminal())
        except Exception:  # noqa: BLE001 — a seam read must never wedge the relay
            logger.debug("engagement_terminal seam read failed", exc_info=True)
            return False

    async def _maybe_arm_suppression(self) -> None:
        """Re-read the (candidate-bound) open-anchor seam and ARM suppression the
        moment this turn's anchor-candidate resolves to a genuinely open,
        unanswered anchor produced by the candidate's OWN ask — the seam's
        ``source_hash`` matches the candidate's block hash (§D5; wb2-1 positive
        binding, in ``_effective_open_state``). A prior / co-existing anchor never
        matches, so it cannot arm this candidate. Called before processing each
        subsequent text frame and again at ``result``. No-op once armed, when
        there is no candidate, or when the seam was not injected (inert)."""
        # §D5 C3 (Sol r4-3): a cold recovery of a held-prose turn DISARMS
        # suppression for the recovered turn's catch-up so the prose re-renders
        # as ordinary narration; never arm while the latch is set.
        if self._replay_disarmed:
            return
        if self._suppressing_for is not None or self._anchor_candidate is None:
            return
        if self.open_anchor_state is None:
            return
        state = await self._effective_open_state()
        if state is not None:
            self._suppressing_for = (state[0], state[1])

    async def _atomic_post_or_hold(
        self, text: str, seg, off_after: int, message_id: str = "",
    ) -> bool:
        """§D5 r4-2: route a not-yet-armed prose post through the sequencer's
        ATOMIC read-decide-write, so the seam re-read and the narration post
        share the ONE lock the late anchor poster uses. Returns ``True`` if the
        text was HELD (buffered + armed — the caller must NOT post/checkpoint
        it); ``False`` if the anchor was closed/absent and the poster already
        posted the text normally.

        Reached only when a candidate is pending and NOT yet armed (the seam was
        closed at the ``_maybe_arm_suppression`` check just above the caller) —
        the r4-2 race window where the anchor may be surfacing LATE. Once armed,
        callers buffer directly without a seam read (no post ⇒ no race)."""
        # wb1-3: pass the CANDIDATE-BOUND seam (``_effective_open_state``) so an
        # already-flushed/disarmed prior anchor cannot make this op hold — it
        # would otherwise buffer legitimate post-B prose off the still-open A.
        # §R3: the separator is applied INSIDE the poster, which runs ONLY on a
        # POST — a HOLD never invokes it, so ``_last_text_msg_id`` is not advanced
        # for a held frame (its separator is deferred to the flush, which buffers
        # the RAW text below).
        status = await self.sequencer.post_unless_anchor_open(
            text,
            self._effective_open_state,
            poster=lambda t=text, m=message_id: self._commit_narration(m, t),
        )
        if status != "held":
            return False
        # The op surfaced the anchor under the lock ⇒ ARM + buffer. Re-read the
        # (bound) seam for the arm identity; this is post-DECISION (we will NOT
        # post regardless), so it is not the r4-2 race — a value that raced to
        # answered only means the buffer flushes at ``result`` instead of
        # discarding. Keep the prior identity (or a sentinel) if it raced away,
        # so the armed buffering discipline still holds.
        state = await self._effective_open_state()
        if state is not None:
            self._suppressing_for = (state[0], state[1])
        elif self._suppressing_for is None:
            self._suppressing_for = (0, 0)
        # Write-ahead the ``hold_pending`` marker before holding (C3).
        self._arm_hold_marker()
        self._anchor_buffer.append((text, list(seg), off_after, message_id))
        return True

    async def _flush_anchor_buffer(self) -> bool:
        """Post all buffered anchor-scoped prose as ORDINARY narration (§D5
        flush), through the normal rollover/seal-aware path so the cursor
        bookkeeping stays consistent.

        wb6-1: reports whether the held prose was DELIVERED. Returns ``True`` on
        an empty buffer, on a fully-delivered flush (buffer then cleared), or on
        a terminal ``DISCARDED`` write (the prose is intentionally not posted
        below a terminal completion — a decision, not a loss). Returns ``False``
        when delivery FAILED (drop mode — every send crossed ``_DROP_THRESHOLD``
        consecutive Telegram failures): the buffer is RETAINED so the caller can
        keep ``hold_pending`` + an UNADVANCED replay boundary and defer to a cold
        restart that resurfaces it (§D5 resurface-never-lose). A partly-delivered
        prefix simply re-renders whole on recovery — at-least-once, the relay's
        existing visible-delivery contract."""
        if not self._anchor_buffer:
            return True
        # P2 (Sol r2-1 / Terra r2-2): commit the buffered tuples SEQUENTIALLY
        # through the ONE shared LIVE append routine — one ``_apply_msg_sep`` +
        # commit PER TUPLE, so separator descriptors are generated and consumed
        # per commit EXACTLY as live (never a batched comprehension: an offset
        # keyed to the batch could delete authored text — Terra ``XY\n\nZ``).
        delivered = 0
        for (text, _seg, _off, mid) in list(self._anchor_buffer):
            await self._commit_narration(mid, text)
            if self._dropped:
                # Partial-failure (Terra r3-MINOR): the delivered PREFIX is on the
                # wire — remove ONLY those tuples, RETAIN the rest + the hold
                # marker with the boundary behind them, and clear pending
                # descriptors with the drop (per the clearing rules). Recovery is
                # cold replay's job (§D5 resurface-never-lose).
                del self._anchor_buffer[:delivered]
                self._seps_clear()
                return False
            delivered += 1
        self._anchor_buffer = []
        return True

    async def _flush_and_disarm(self) -> None:
        """§D5 r23-2: a tool_use proves the agent is still working, so buffered
        post-anchor prose is legitimate — FLUSH it and DISARM for the rest of the
        turn. The spent candidate is dropped too, so the CURRENT anchor never
        re-arms on the next prose frame; re-arming happens ONLY when a NEW anchor
        surfaces (a fresh ``_match_discrete_block`` candidate)."""
        # wb7-1: the tool_use flush obeys the SAME wave-6 resurface-never-lose
        # contract as the spawn/finalize flushes. If delivery FAILED (drop mode)
        # the buffer is RETAINED — do NOT disarm suppression, and (critically) do
        # NOT let ``_handle_assistant_blocks`` fall through to ``_advance_dropped``,
        # which would set ``dropped_through`` past the still-held frames and make a
        # cold restart SKIP them despite ``hold_pending=True`` (held prose lost).
        # Defer INSTEAD: raise ``_FlushDeferred`` BEFORE disarming/advancing, so
        # ``current``/``dropped_through`` stay behind the held frames, the marker
        # stays set, and the run-loop catch cold-recovers to resurface the prose
        # (the C3 cold-disarm machinery renders it as ordinary narration).
        if not await self._flush_anchor_buffer():
            logger.warning(
                "topic stream for engagement %s: held-prose flush failed at a "
                "tool_use boundary; deferring to a cold restart so the held "
                "narration resurfaces (marker kept, checkpoint held behind the "
                "held frames)",
                self.engagement_id,
            )
            raise _FlushDeferred
        # wb2-1: dropping the spent candidate is what enforces "re-arm only when a
        # NEW anchor surfaces" — the next prose has no candidate to bind, and a
        # later candidate only arms on ITS OWN anchor (positive source-hash bind),
        # never off this just-disarmed one. (Wave-1's ``_disarmed_mids`` negative
        # set is superseded by that positive binding.)
        self._suppressing_for = None
        self._anchor_candidate = None

    async def _handle_assistant_blocks(
        self, blocks: list[tuple], seg, off_after: int, message_id: str = "",
    ) -> None:
        """Process a tool-bearing assistant frame block-by-block (§2(3)).

        Text blocks append to narration; each tool_use block fires the
        mutating-tool event (once, for the first mutating tool) and drives
        relay-mediated discrete matching AT ITS POSITION. Checkpoints ONCE at
        frame end. §R3: every text block in this frame carries the frame's
        ``message_id`` (per the CLI contract there is at most one), so the shared
        separator decision fires at the frame's message boundary."""
        if self._dropped:
            self._advance_dropped(seg, off_after)
            return
        fired_mutating = False
        flushed_buffer = False
        for block in blocks:
            if block[0] == "text":
                await self._append_narration(block[1], seg, off_after, message_id)
                if self._dropped:
                    self._advance_dropped(seg, off_after)
                    return
            else:  # tool_use
                _kind, name, tool_input = block
                # §D5 r23-2: a tool_use proves the agent is still working — FLUSH
                # any buffered post-anchor prose (above this block's discrete
                # post) and DISARM. The flush drops the spent candidate, so if
                # this block is itself a NEW anchor ask, ``_match_discrete_block``
                # below re-records the candidate and the next prose re-arms.
                if self._suppressing_for is not None:
                    flushed_buffer = True
                    # wb7-1: a flush that hits drop mode raises ``_FlushDeferred``
                    # INSIDE ``_flush_and_disarm`` (buffer + marker retained,
                    # boundary left behind the held frames) so it never reaches an
                    # ``_advance_dropped`` divert here — that would set
                    # ``dropped_through`` past the still-held frames and lose them
                    # on a cold restart. Only a SUCCESSFUL flush returns; the loop
                    # then continues with an empty buffer, disarmed.
                    await self._flush_and_disarm()
                if not fired_mutating and name not in _NON_MUTATING_TOOLS:
                    fired_mutating = True
                    await _maybe_await(
                        self.on_turn_event("mutating_tool", {"tool": name})
                    )
                # v0.79.0 (§5): EVERY tool_use block drives the live-summary
                # controller's activity + plan progress. Emitted here (LIVE
                # only — replay never reaches _handle_assistant_blocks), so the
                # controller derives post-recovery state from the lifecycle
                # alone and never from stale, replayed tool frames.
                # §5 P1-B (r6 fix): stamp a MONOTONIC per-turn tool-event counter
                # as the ordering ``seq`` — incremented once per tool_use block
                # processed, so two TodoWrite blocks in ONE assistant frame get
                # distinct, strictly-increasing sequences AND a TodoWrite after a
                # mid-turn log rotation still sorts ABOVE an earlier one (the old
                # ``(segment, offset, block_ordinal)`` coordinate carried file
                # identity, so a rotated segment could sort lower and the
                # controller's ≤-watermark would reject the newer plan forever).
                # The controller's plan watermark rejects a frame ≤ this; passed
                # verbatim through to ``submit_plan``.
                self._tool_event_seq += 1
                await _maybe_await(
                    self.on_turn_event(
                        "tool_use",
                        {
                            "tool": name,
                            "input": tool_input,
                            "seq": self._tool_event_seq,
                        },
                    )
                )
                await self._match_discrete_block(name, tool_input)

        # §D5: a frame that still holds UNFLUSHED buffered prose is EXEMPT from
        # checkpoint advancement (mirrors the throttled-text hold at :894) so a
        # crash can never strand the held prose behind an advanced cursor. The
        # ``hold_pending`` marker (write-ahead) keeps the held frame recoverable
        # here; a later flush / the ``result`` discard advances the cursor and
        # clears the marker atomically.
        if self._anchor_buffer:
            return
        self.cursor.current = {"segment": list(seg), "offset": off_after}
        # §A1 (Sol r2-1a): a tool-only frame can checkpoint WHILE a throttled
        # narration edit is held unposted (``_per_message_text`` grew, the wire
        # did not). Persist the WIRE high-water (``_posted_len``), never the
        # inflated in-memory length, so the delta reconcile keeps the held
        # suffix instead of slicing it behind the seal forever.
        self.cursor.last_posted_len = self._posted_len
        # §D5 r3-2 (C3): if a tool_use FLUSHED held prose this frame, the buffer
        # is now on the wire and this checkpoint advances PAST those frames —
        # clear ``hold_pending`` ATOMICALLY (same ``_save``) with the advance.
        # Gate on the flush: a disarmed cold-catch-up tool frame (no flush) must
        # keep the marker set until the recovered turn's ``result`` boundary.
        if flushed_buffer:
            self.cursor.hold_pending = False
            # #538: defensive — a flush is unreachable while the tombstone is
            # set (it only exists between the result-time discard decision and
            # the closed-turn save), but every ``hold_pending`` clear site
            # drops the tombstone with it so the pair can never skew.
            self.cursor.hold_discarded = None
        self._save()

    def _reset_turn_state(self) -> None:
        self._turn_text = ""
        # §5 P1-B (r6 fix): a fresh turn restarts the monotonic tool-event
        # counter at 0. The controller's plan-ordering watermark resets on the
        # SAME boundary (``note_turn_start``), so the new turn's first tool_use
        # (counter 1) is always accepted.
        self._tool_event_seq = 0
        self._last_text_msg_id = None  # §R3: no prior segment in a fresh turn
        # P2: a fresh turn has no pending separators and starts replay ordinal 0.
        self._seps_clear()
        self._replay_j = 0
        self._per_message_text = ""
        self._posted_len = 0
        self._replay_msg_count = 0
        self._fail_count = 0
        self._dropped = False
        self._drop_warned = False
        self._last_edit_ts = float("-inf")
        # §D5: turn boundaries reset candidate/arming state AND the buffer. The
        # abnormal ``spawn`` boundary flushes the buffer + clears ``hold_pending``
        # BEFORE this reset runs (C3); the normal ``result`` path flushes/
        # discards + clears the marker before reaching here.
        self._anchor_candidate = None
        self._suppressing_for = None
        self._anchor_buffer = []
        # §D5 C3: a turn boundary ends the cold-recovery catch-up window — clear
        # the disarm latch so subsequent turns arm suppression normally. (Cold
        # replay never calls ``_reset_turn_state`` — replayed frames <= current
        # take the side-effect-suppressed path — so ``_run_cold`` re-sets this
        # AFTER its initial reset; the latch clears at the FIRST live boundary.)
        self._replay_disarmed = False
        # #538: the discard latch dies on the SAME boundary — a later turn's
        # narration must never be muted by a recovered turn's tombstone.
        self._replay_discard = None
        self._discard_latched = False

    async def _finalize(
        self, seg, off_after: int, *, subtype: str | None = None,
    ) -> None:
        """Route the closing edit through ``edit_narration_if_latest`` (§2:612),
        then persist a CLOSED-TURN checkpoint (``message_ids=[]``,
        ``turn_start == current`` past ``result``) that carries a durable
        ``result_event_pending`` marker, then deliver ``on_turn_event("result")``
        and clear the marker (#334 — see the ``StreamCursor`` field comment).

        §2(d): the reply de-dup DELETE (formerly here at :633) is REMOVED — no
        message is ever deleted; a duplicate is preferred over erasing history.
        De-dup, when wired (T3), happens BEFORE posting inside the sequencer.
        §2(c): if the narration message was SEALED (a discrete post below it, or
        a conservative seal after process recovery), the closing state posts as
        a NEW message instead of editing one with content under it.
        """
        # §D5: re-consult the seam AGAIN at result (an answer that arrived
        # during the turn, or a late anchor post, may only now resolve).
        await self._maybe_arm_suppression()
        # §D5 FLUSH-vs-DISCARD: an answer that arrived during the turn (the seam
        # now reports the anchor closed/answered) FLUSHES the held prose as
        # ordinary narration; an anchor still open-and-unanswered at ``result``
        # DISCARDS it — the F-LEAK2 kill (the trailing sign-off never posts).
        # The seam re-read here is not the r4-2 race: a reported answer means the
        # anchor already resolved, so no late anchor post can still race us.
        if self._anchor_buffer:
            # wb2-1: read the candidate-BOUND seam — only the anchor this buffer
            # was actually armed under (its own ask's ``source_hash``) keeps it
            # held; a closed/answered/unrelated anchor reads as ``None``.
            state = await self._effective_open_state()
            # wb2-3: a TERMINAL engagement closure (the record flipped terminal and
            # ``settle_all_open_questions`` closed the anchor ledger, so the seam
            # now reports ``None``) is INDISTINGUISHABLE from an answer at the bare
            # seam — but the engagement is OVER, so held prose must be DISCARDED,
            # never flushed below the terminal completion (D5 discard doctrine). A
            # genuine answer (``None`` and NOT terminal) still FLUSHES.
            if state is None and not self._is_terminal():
                # wb6-1: an answer arrived ⇒ FLUSH. If delivery FAILED (drop
                # mode), do NOT discard the buffer, advance the closed-turn
                # checkpoint, or clear ``hold_pending`` (all downstream in this
                # method) — that would lose the undelivered held prose. Retain
                # the buffer + the marker + the UNADVANCED boundary (``current``
                # is still behind the held frames — they never checkpointed) and
                # defer to a cold restart that resurfaces it (§D5 resurface-
                # never-lose). The cold catch-up re-renders the prose as ordinary
                # disarmed narration; this finalize re-runs with an EMPTY buffer.
                if not await self._flush_anchor_buffer():
                    logger.warning(
                        "topic stream for engagement %s: held-prose flush failed "
                        "at result (answer arrived); deferring finalize to a cold "
                        "restart so the held narration resurfaces (marker kept, "
                        "checkpoint held behind the held frames)",
                        self.engagement_id,
                    )
                    raise _FlushDeferred
            else:
                # Held-buffer DISCARD (anchor still open, or terminal): the held
                # prose never posts — clear the buffer AND any pending separators.
                # (#538: capture the dead range's start — the FIRST held frame's
                # coordinate — before the buffer goes away.)
                held_from = (self._anchor_buffer[0][1], self._anchor_buffer[0][2])
                self._anchor_buffer = []
                self._seps_clear()
                # #538: write-ahead the DISCARD tombstone BEFORE the awaits
                # below (closing edit / drain). A crash in that window used to
                # leave ``hold_pending`` alone durable, and cold recovery's
                # disarmed catch-up RESURFACED the held frames — the one case
                # where resurfacing is wrong (the sign-off the discard killed
                # posted after all). The tombstone carries the FIRST held
                # frame's coordinate so recovery drops exactly the dead range;
                # both fields clear atomically in the closed-turn save below.
                _first_held = held_from
                self.cursor.hold_discarded = {
                    "from": {
                        "segment": list(_first_held[0]),
                        "offset": int(_first_held[1]),
                    },
                }
                self._save()
        coord = {"segment": list(seg), "offset": off_after}
        # wb3-2: a TERMINAL engagement DISCARDS its closing edit entirely — the
        # completion has (or will) post, and a sealed-narration repost of the
        # unposted suffix (:1400) would land a NEW message BELOW it. The
        # sequencer's locked writers also discard (belt-and-suspenders for a
        # terminalization that races this block), but skipping here keeps the
        # intent explicit and avoids a needless sealed-repost attempt.
        if (
            not self._dropped and not self._is_terminal()
            and self.cursor.message_ids and self._per_message_text
        ):
            # B2 (Sol r1): the closing edit carries this turn's FINAL fragment,
            # so it honors the at-least-once retry/drop contract via
            # ``_apply_seq_edit``. Only after it lands (APPLIED/SEALED-then-new),
            # or the turn crosses the drop threshold, may the closed-turn
            # checkpoint below advance past ``result``.
            res = await self._apply_seq_edit(
                self.cursor.message_ids[-1], self._per_message_text,
            )
            if res == "applied":
                # #523: the closing edit LANDED — durably record the wire
                # high-water it established, qualified by the result frame's
                # coordinate, BEFORE the closed-turn mutations below. A crash
                # in the window then replays convergently: frames at/below
                # ``coord`` reconstruct text-only (the wire already carries
                # them) and the re-run finalize's edit computes an empty
                # increment — no reposted suffix. The SEALED branch
                # deliberately writes NO marker (its repost path already owns
                # the at-least-once duplicate; a seal+crash double fault
                # keeps today's contract), as do drop/discard.
                self.cursor.wire_hw = {
                    "coord": dict(coord),
                    "len": len(self._per_message_text),
                }
                self._save()
            if res == "sealed":
                # §2(c) / W-R4 (Sol r1-3): the narration message was SEALED (a
                # discrete posted below it), so it can no longer be edited. Post
                # ONLY the GENUINELY-UNPOSTED trailing text — the already-visible
                # prefix (``_posted_len`` chars, what actually reached the wire)
                # must NOT be reposted. A fully-posted sealed tail (pending == "")
                # reposts NOTHING (the production dup: msgs 1139/1141/1143 were
                # this branch reposting the whole visible tail); a throttled,
                # never-posted suffix (:769 held it without advancing
                # ``_posted_len``) posts EXACTLY that suffix, once. This is the
                # NORMAL in-process finalize; the conservative crash-recovery
                # seal in ``_reconcile`` is a SEPARATE branch that may duplicate.
                # P2: route through the SAME sealed-open re-plan as ``_execute_ops``
                # so a throttled leading separator at the tail's head is STRIPPED
                # (no leading blank line when the closing fragment opens a new
                # message); the complete logical target is the full current
                # message, so ``tail == pending`` and a no-descriptor tail behaves
                # exactly as before (posts ``pending``, or nothing when empty).
                await self._replan_sealed(self._per_message_text)

        if self._dropped:
            # The turn ended in drop mode: ``dropped_through`` reaches the
            # turn's TERMINAL coordinate so a restart never replays the tail.
            self.cursor.dropped_through = dict(coord)
        self.cursor.current = coord
        self.cursor.turn_start = dict(coord)
        # P2: clear all three message lists together (parallel-list lifecycle).
        self._reset_message_lists()
        self.cursor.last_posted_len = 0
        # #523: the closed-turn checkpoint advances past the closing edit's
        # coordinate — the wire high-water marker has served its purpose and
        # clears atomically in this same save.
        self.cursor.wire_hw = None
        # §D5 r3-2 (C3): ``result`` is a held-frames boundary — the buffer was
        # just FLUSHED (answer arrived) or DISCARDED (anchor still open), and a
        # cold-recovery catch-up reaches its ``result`` with the prose already
        # re-rendered. Either way the closed-turn checkpoint advances past the
        # held frames, so clear ``hold_pending`` ATOMICALLY in this same save.
        self.cursor.hold_pending = False
        self.cursor.hold_discarded = None  # #538: tombstone dies with it
        self._reset_turn_state()
        # F4+F6: drain every still-armed late intent, then prune + seal, as ONE
        # atomic lock hold. The former flush→prune→seal sequence released the
        # lock between steps, so an intent registered+armed by a late ingress
        # during a flush poster-await could be pruned before it posted (F6:
        # intent B silently dropped). ``drain_and_prune_turn`` re-snapshots the
        # armed set under a single held lock until it is empty, so nothing live
        # is pruned.
        await self.sequencer.drain_and_prune_turn()
        # #334: the pending-event marker rides the SAME save as the closed-turn
        # checkpoint (write-ahead). If the delivery below raises, the run
        # aborts with the marker durable and ``_run_cold`` re-delivers it; the
        # closed turn itself never replays.
        self.cursor.result_event_pending = {"subtype": subtype}
        self._save()
        await _maybe_await(self.on_turn_event("result", {"subtype": subtype}))
        self.cursor.result_event_pending = None
        self._save()

    # -- main loop ----------------------------------------------------------

    async def run(self) -> None:
        """Relay the log to the topic. COLD on the first call / after any error
        (reload the cursor, compute ``recovering``, replay from ``turn_start``,
        reconcile on going live); WARM on a clean re-entry (resume from
        ``_read_coord`` live — no reload, no state reset, no replay, no
        reconcile). ``_reconcile`` is thereby confined to genuine restarts."""
        if self._warm and self._read_coord is not None:
            await self._run_warm()
        else:
            await self._run_cold()

    async def _run_cold(self) -> None:
        self.cursor = StreamCursor.load(self.cursor_path)
        # #334: a durable pending turn-end event (the callback raised, or the
        # process died before the post-delivery clear) is re-delivered FIRST —
        # before any frame is processed — so the driver's turn lifecycle closes
        # ahead of any later turn's events. A raise here aborts the run with
        # the marker intact; the next cold entry retries. At-least-once.
        if self.cursor.result_event_pending is not None:
            await _maybe_await(self.on_turn_event(
                "result", dict(self.cursor.result_event_pending),
            ))
            self.cursor.result_event_pending = None
            self._save()
        # §A1(3) (Sol A1 review): a STALE ``dropped_through`` whose segment has
        # rotated off disk is NO LONGER cleared here. The prior cold-start clear
        # (a) never helped the WARM path (a live marker whose segment rotated out
        # while warm still rank-infinity-muted everything) and (b) had a TOCTOU —
        # ``_ordered_segments`` can transiently miss a segment mid-rotation, so the
        # clear could erase a LIVE marker and let deliberately-dropped bytes
        # reconcile back onto the wire. The permanent-mute bug is instead handled
        # at comparison level in ``_coord_le`` (an absent target segment is never
        # ``>=`` a readable frame), which fixes both paths without mutating
        # persisted state from a racy scan.
        cur_seg = tuple(self.cursor.current.get("segment", (0, 0)))
        recovering = (
            cur_seg != _ZERO_SEG
            or bool(self.cursor.message_ids)
            or self.cursor.dropped_through is not None
            # §D5 C3: a set ``hold_pending`` marks an in-progress held-prose turn
            # to recover — its held frames lie beyond ``current`` and must be
            # replayed-then-caught-up (DISARMED), never processed fresh-and-live.
            or bool(self.cursor.hold_pending)
        )
        self._live = not recovering
        self._reconciled = False
        self._passed_cur_seg = False
        # #300: when the scan's start coordinate is already at-or-past
        # ``current`` (a closed-turn cursor has ``turn_start == current``; the
        # checkpoint may sit at the exact EOF of a since-rotated archive), the
        # cursor's segment can yield ZERO frames — recovery drains straight
        # into the successor and ``_update_live``'s per-frame latch would
        # never fire, replay-muting the entire next segment. Pre-latch
        # ``_passed_cur_seg``: the scan starts positioned past the checkpoint,
        # so any frame from a DIFFERENT segment is genuinely post-checkpoint
        # (frames from the cursor segment itself still latch via the
        # ``off_after > cur_off`` branch). A zero ``current`` (never
        # checkpointed but recovering via message_ids/hold_pending/
        # dropped_through) means NOTHING was consumed — same pre-latch, every
        # frame is post-checkpoint.
        ts_seg = tuple(self.cursor.turn_start.get("segment", (0, 0)))
        if cur_seg == _ZERO_SEG or (
            ts_seg == cur_seg
            and int(self.cursor.turn_start.get("offset", 0))
            >= int(self.cursor.current.get("offset", 0))
        ):
            self._passed_cur_seg = True
        self._reset_turn_state()
        self._read_coord = None
        # §D5 C3 (Sol r4-3): cold start with the held-frames marker set ⇒ DISARM
        # suppression for the recovered turn's catch-up so the previously-
        # buffered prose re-renders as ordinary narration (at-least-once
        # resurface). Set AFTER ``_reset_turn_state`` (which clears the latch);
        # it clears again at the recovered turn's boundary (result / abnormal
        # spawn / gap), after which later turns arm normally.
        self._replay_disarmed = bool(self.cursor.hold_pending)
        # #538: the DISCARD tombstone overrides the resurface rendering — the
        # held frames were already ruled dead at the crashed run's ``result``,
        # so this recovery DROPS text for frames at/past its ``from``
        # coordinate. Rides (never replaces) the disarm latch and dies on the
        # same boundaries.
        self._replay_discard = (
            self.cursor.hold_discarded if self.cursor.hold_pending else None
        )
        self._discard_latched = False
        gap_seen = False

        try:
            async for seg, off_after, raw in iter_log_segments(
                self.log_dir, self.cursor.turn_start
            ):
                if seg == SEGMENT_GAP:
                    logger.warning(
                        "topic stream retention gap for engagement %s: "
                        "turn_start segment %s absent on disk; resuming at "
                        "current offset 0",
                        self.engagement_id,
                        self.cursor.turn_start.get("segment"),
                    )
                    # The turn's history is unrecoverable — resume fresh and
                    # live. The sentinel NEVER becomes a coordinate: after a gap,
                    # ``_read_coord`` is seeded only by the first REAL frame
                    # consumed post-gap (and a gap-only run stays cold).
                    # §D5 r29-3 (C3): SEGMENT_GAP is a TERMINAL recovery floor
                    # for the marker. If a held-prose turn's frames rotated out,
                    # ``hold_pending`` would otherwise stay set with suppression
                    # disarmed into a LATER turn (F-LEAK2 recurs). Log the
                    # unavoidable lost-source residual (the held prose's frames
                    # are gone — nothing can resurface them), durably CLEAR the
                    # marker, and re-arm normal suppression (``_reset_turn_state``
                    # below clears the disarm latch).
                    if self.cursor.hold_pending:
                        logger.warning(
                            "topic stream for engagement %s: retention gap "
                            "dropped a turn with buffered prose (hold_pending "
                            "was set); its source frames rotated out and cannot "
                            "resurface — clearing the marker and re-arming",
                            self.engagement_id,
                        )
                        self.cursor.hold_pending = False
                        # #538: the tombstone belongs to the same rotated-out
                        # hold — leaving it behind would DROP a LATER turn's
                        # crash-recovery resurface once a fresh hold re-set
                        # ``hold_pending`` (belt-and-suspenders with the
                        # ``_arm_hold_marker`` reset).
                        self.cursor.hold_discarded = None
                        self._save()
                    # #523: the marker's source frames rotated out with the
                    # gap — its coordinate can never be reached again, so it
                    # would linger forever (harmless to correctness via the
                    # ``_coord_le`` absence rule, but stale state). Clear it;
                    # durable on the next checkpoint save.
                    self.cursor.wire_hw = None
                    self._live = True
                    self._reconciled = True
                    self._reset_message_lists()
                    self._reset_turn_state()
                    gap_seen = True
                    continue

                # The warm resume coordinate: set for EVERY real frame consumed
                # (replay AND live) BEFORE dispatch (Sol r1-1 / r2-1b).
                self._read_coord = {"segment": list(seg), "offset": off_after}

                # Drop-mode tail from a prior run: skip entirely (no side
                # effects) but still advance ``_read_coord`` past it.
                if self.cursor.dropped_through is not None and self._coord_le(
                    seg, off_after, self.cursor.dropped_through
                ):
                    continue

                was_live = self._live
                self._update_live(seg, off_after)
                if self._live and not was_live and not self._reconciled:
                    await self._reconcile()

                await self._handle_frame(seg, off_after, raw)

            # Stream ended while still replaying an open turn — reconcile now.
            if recovering and not self._reconciled:
                await self._reconcile()
        except _FlushDeferred:
            # wb6-1: a held-prose flush failed. Abandon this run WITHOUT latching
            # warm — the replay boundary was intentionally left behind the held
            # frames with ``hold_pending`` set, so the next poll cold-recovers and
            # resurfaces the prose. (Do NOT re-raise: this is a controlled defer,
            # not a relay error.)
            self._warm = False
            return
        except BaseException:
            # Any error escaping the loop invalidates the warm latch so the
            # driver's retry does a full cold recovery (reconcile stays
            # restart-only).
            self._warm = False
            raise

        self._latch_warm(gap_seen)

    def _latch_warm(self, gap_seen: bool) -> None:
        """Latch WARM re-entry at clean exhaustion when the resume coordinate is
        valid (§A1(1), Sol r3-3). ``_read_coord`` is non-``None`` iff at least
        one real frame was consumed; when ZERO frames were consumed it may be
        seeded from ``cursor.current`` ONLY at a closed-turn boundary
        (``message_ids == []`` — memory-empty matches disk) and only when no
        retention gap intervened. A zero-frame run over an OPEN turn — or any
        gap-only run — does NOT latch; the next poll runs cold again."""
        if self._read_coord is not None:
            self._warm = True
            return
        if not gap_seen and not self.cursor.message_ids:
            self._read_coord = dict(self.cursor.current)
            self._warm = True

    async def _run_warm(self) -> None:
        """Resume IN PLACE from ``_read_coord``: no cursor reload, no turn-state
        reset, live from the first frame, no replay, no reconcile (§A1(1))."""
        self._reconciled = True  # warm never reconciles
        self._live = True
        try:
            async for seg, off_after, raw in iter_log_segments(
                self.log_dir, self._read_coord
            ):
                if seg == SEGMENT_GAP:
                    # The resume segment rotated off disk between polls (rare):
                    # drop the warm latch and let the next poll cold-recover
                    # from ``turn_start`` (the sentinel never becomes a coord).
                    logger.warning(
                        "topic stream for engagement %s: retention gap on warm "
                        "resume; falling back to cold recovery",
                        self.engagement_id,
                    )
                    self._warm = False
                    return

                self._read_coord = {"segment": list(seg), "offset": off_after}

                if self.cursor.dropped_through is not None and self._coord_le(
                    seg, off_after, self.cursor.dropped_through
                ):
                    continue

                await self._handle_frame(seg, off_after, raw)
        except _FlushDeferred:
            # wb6-1: a held-prose flush failed on a warm resume — drop the warm
            # latch so the next poll cold-recovers from ``hold_pending`` and
            # resurfaces the held prose (the boundary stays behind the held
            # frames). A controlled defer, not a relay error: do NOT re-raise.
            self._warm = False
            return
        except BaseException:
            self._warm = False
            raise

    async def _handle_frame(self, seg, off_after: int, raw: bytes) -> None:
        frame = parse_frame(raw)
        # #538 (Sol r2): positional dead-range entry — checked for EVERY frame
        # in scan order, before any dispatch branch consults the latch.
        self._latch_discard_range(seg, off_after)

        if not self._live:
            # REPLAY: rebuild visible-text state ONLY; suppress all side effects.
            # §R3: re-derive from the SAME (message_id, text) extractor the live
            # path uses, so the separator lands identically on reconstruction.
            # #538: a tombstoned frame's text is NOT wire content — a prior
            # tombstone recovery may have checkpointed past dropped frames
            # before a second crash, so they can arrive here ≤ ``current``;
            # reconstructing them would inflate ``_per_message_text`` past
            # the true wire and the re-run closing edit would post the dead
            # suffix (Sol diff-r1 S2, double-crash arm).
            if frame is not None and not self._in_discarded_range(
                seg, off_after,
            ):
                narr = extract_narration(frame)
                if narr is not None and narr.text:
                    self._replay_text(narr.text, narr.message_id)
            return

        # #523: LIVE frames at/below a persisted wire-high-water coordinate
        # were fully handled before the crash (the marker exists only because
        # the turn REACHED its closing edit): reconstruct their narration
        # text-only — the wire already carries it — and suppress re-execution.
        # At exactly the recorded coordinate (the result frame), adopt the
        # landed high-water FIRST (Sol design-r2: adopting only after passing
        # the coordinate would let the re-run finalize repost the suffix via
        # the sealed path), then dispatch the frame normally so the result
        # event and closed-turn checkpoint re-run as today.
        hw = self.cursor.wire_hw
        if hw is not None:
            hw_coord = hw.get("coord") or {}
            if (
                tuple(seg) == tuple(hw_coord.get("segment", (0, 0)))
                and off_after == int(hw_coord.get("offset", 0))
            ):
                self._posted_len = int(hw.get("len") or 0)
                self.cursor.last_posted_len = self._posted_len
                self.cursor.wire_hw = None  # durably cleared by finalize
            elif self._coord_le(seg, off_after, hw_coord):
                # #538 (Sol diff-r1 S2): held frames sit between ``current``
                # and the wire-high-water coordinate, so this branch would
                # otherwise reconstruct the DISCARDED prose into
                # ``_per_message_text`` — past ``wire_hw.len`` — and the
                # re-run finalize's closing edit would post the dead suffix.
                # Range-scoped (not blanket): throttled-but-landed pre-anchor
                # text BELOW the tombstone's ``from`` is genuine wire content
                # the reconstruction must keep, or the closing edit would
                # SHRINK the posted message.
                if frame is not None and not self._in_discarded_range(
                    seg, off_after,
                ):
                    narr = extract_narration(frame)
                    if narr is not None and narr.text:
                        self._replay_text(narr.text, narr.message_id)
                return

        if frame is None:
            logger.debug(
                "topic stream: skipping non-JSON line for engagement %s",
                self.engagement_id,
            )
            self._checkpoint(seg, off_after)
            return

        if frame.get("casa_control") == "spawn":
            # §D5 abnormal turn boundary — ``spawn`` without a preceding
            # ``result`` (Sol r5-3/r8-2/r9-3). On warm re-entry cold disarming
            # never runs, so a held buffer could be reset (lost) or
            # ``hold_pending`` stranded into a later turn. EVENT-FIRST ordering
            # (Sol r9-3) preserves the relay's crash-safety invariant: frames
            # at/below ``current`` replay with side effects suppressed, so
            # checkpointing the spawn BEFORE emitting its event would lose the
            # event permanently on a crash in between. Order (matching today's
            # side-effects-before-checkpoint): (1) flush any held prose as
            # ordinary narration + reset suppression/recovery state IN MEMORY;
            # (2) deliver the spawn event; (3) THEN atomically checkpoint the
            # handled spawn AND clear ``hold_pending`` in the SAME ``_save``. The
            # marker clears UNCONDITIONALLY (Sol r8-2, not buffer-conditional): a
            # cold recovery renders held prose immediately with an EMPTY buffer
            # yet must still clear here, because an abnormal turn has no
            # ``result`` to clear at (a buffer-conditional clear would strand the
            # marker into the next turn with suppression disarmed — F-LEAK2). A
            # crash between (2) and (3) re-delivers the event on recovery
            # (at-least-once — the relay's existing contract).
            # wb6-1: (1) flush (no-op if empty). If delivery FAILED (drop mode),
            # do NOT proceed to reset suppression, deliver the spawn event, or
            # checkpoint-and-clear: advancing the boundary past the still-
            # undelivered held frames + clearing ``hold_pending`` would PERMANENTLY
            # lose the held prose (§D5 resurface-never-lose). Leave ``current``
            # behind the held frames + the marker set + the buffer retained, and
            # abandon this run (``_FlushDeferred``) so a cold restart resurfaces
            # the prose as disarmed catch-up narration, then handles the spawn.
            if not await self._flush_anchor_buffer():  # (1)
                logger.warning(
                    "topic stream for engagement %s: held-prose flush failed at "
                    "an abnormal spawn boundary; deferring the spawn to a cold "
                    "restart so the held narration resurfaces (marker kept, "
                    "checkpoint held behind the held frames)",
                    self.engagement_id,
                )
                raise _FlushDeferred
            self._suppressing_for = None
            self._anchor_candidate = None
            self._replay_disarmed = False
            self._replay_discard = None  # #538: same boundary, same death
            self._discard_latched = False
            await _maybe_await(
                self.on_turn_event("spawn", {"epoch": frame.get("epoch")})  # (2)
            )
            self.cursor.current = {"segment": list(seg), "offset": off_after}  # (3)
            self.cursor.last_posted_len = self._posted_len
            self.cursor.hold_pending = False
            self.cursor.hold_discarded = None  # #538: tombstone dies with it
            self._save()
            return

        ftype = frame.get("type")

        if ftype == "system" and frame.get("subtype") == "init":
            off_before = off_after - len(raw)
            self.cursor.turn_start = {"segment": list(seg), "offset": off_before}
            self._reset_message_lists()
            self.cursor.last_posted_len = 0
            # #523: a new turn begins — any leftover wire-high-water marker
            # (its result frame never replayed, e.g. adoption raced a crash)
            # belongs to the previous turn and must not qualify frames here.
            self.cursor.wire_hw = None
            self._reset_turn_state()
            await _maybe_await(
                self.on_turn_event(
                    "turn_start", {"session_id": frame.get("session_id")}
                )
            )
            self._checkpoint(seg, off_after)
            return

        if ftype == "assistant":
            # §R3: read the frame's (message_id, text) ONCE through the shared
            # extractor and thread the id into every narration-commit path.
            narr = extract_narration(frame)
            blocks = iter_content_blocks(frame)
            if any(b[0] == "tool_use" for b in blocks):
                # Mixed / tool-bearing frame: process blocks IN ORDER so a
                # relay-mediated discrete post lands at exactly its content-
                # block position (§2(3)), interleaved with narration text.
                await self._handle_assistant_blocks(
                    blocks, seg, off_after, narr.message_id
                )
                return
            # Text-only frame (the common streaming case): unchanged fast path —
            # join, throttle, single checkpoint.
            if narr.text:
                await self._post_text(narr.text, seg, off_after, narr.message_id)
                return
            # No visible text (e.g. thinking-only) → invisible checkpoint.
            self._checkpoint(seg, off_after)
            return

        if ftype == "result":
            # #334: ``_finalize`` owns the event delivery — the closed-turn
            # checkpoint and the durable ``result_event_pending`` marker are
            # saved atomically before the callback runs.
            await self._finalize(seg, off_after, subtype=frame.get("subtype"))
            return

        # rate_limit_event / unknown type → invisible checkpoint.
        self._checkpoint(seg, off_after)
