"""Delta-fed, tag-opaque prosodic splitter (spec §5.1).

Feed token suffixes via ``feed(delta)`` — it returns any blocks that
closed on this delta. Call ``flush_tail()`` on stream end to drain the
buffer.

Each emission is a :class:`Block`: the clean prosodic unit plus the exact
whitespace that separated it from the *previous* block. Blocks are handed
to TTS as independent utterances (where leading/trailing whitespace is
noise), but the same blocks are also concatenated back into one message by
the Home Assistant companion integration — so the separator that sits
between two blocks is real information and must survive the split
(issue #257). Carrying it on the *following* block, rather than holding a
finished block until its trailing whitespace arrives, keeps speech
latency unchanged: a block is still emitted the instant it closes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

_SENTENCE_MARKS = ".!?…"
_CLAUSE_MARKS = ",;—"
# No "<": canonical voice tags use square brackets, and a bare "<" in
# speech ("below < 19") has no closer, so treating it as an opener held
# every later sentence back to the safety cap (#1646).
_OPEN = {"[": "]", "(": ")", "{": "}"}
_CHAR_CAP = 200
_TIME_CAP = 1.5  # seconds since last flush


@dataclass(frozen=True)
class Block:
    """One prosodic unit and the separator that preceded it.

    ``sep`` is the exact whitespace run that stood between the previous
    block and this one in the fed stream — empty for the first block of a
    stream, and empty whenever the source really had no separator (a
    safety-cap hard cut lands mid-word, so a synthetic space there would
    split a word). ``text`` is the speech unit itself: never leading- or
    trailing-whitespace padded, never empty.

    Losslessness invariant, per uninterrupted splitter lifetime:
    ``"".join(b.sep + b.text for b in every emission, flush_tail() last)``
    equals the concatenation of every fed delta, minus whitespace trailing
    the stream as a whole.
    """

    sep: str
    text: str


class ProsodicSplitter:
    def __init__(self) -> None:
        self._buf: str = ""
        # Whitespace already consumed from the stream that belongs *in
        # front of* the next block to be emitted.
        self._pending_sep: str = ""
        self._last_flush: float = time.monotonic()

    # --- public -------------------------------------------------------

    def feed(self, delta: str) -> list[Block]:
        if delta and not self._buf:
            self._last_flush = time.monotonic()
        self._buf += delta
        return self._drain()

    @property
    def pending_sep(self) -> str:
        """The whitespace this splitter is holding in front of its next
        block — consumed separator plus any still at the head of the buffer.

        A caller that abandons a splitter mid-stream (the AR-B non-prefix
        reset) discards its buffered text on purpose, but this gap is not
        that text: it is what would have separated the last block already
        emitted from whatever comes next, and dropping it welds them
        together (#257).
        """
        head = self._buf[: len(self._buf) - len(self._buf.lstrip())]
        return self._pending_sep + head

    def flush_tail(self) -> Block | None:
        """Drain the buffer at stream end, or None if nothing is left to
        speak (an empty buffer, or one holding only the stream's trailing
        whitespace — which no longer separates anything)."""
        body, self._buf, self._pending_sep = (
            self._pending_sep + self._buf, "", "",
        )
        text = body.strip()
        if not text:
            return None
        self._last_flush = time.monotonic()
        return Block(sep=body[: len(body) - len(body.lstrip())], text=text)

    # --- internal -----------------------------------------------------

    def _drain(self) -> list[Block]:
        out: list[Block] = []
        while True:
            cut = self._find_cut(self._buf)
            if cut is None:
                break
            raw, self._buf = self._buf[:cut], self._buf[cut:]
            if not raw.strip():
                # A paragraph break sitting at the head of the buffer: the
                # break itself IS the separator and the cut is at index 0,
                # so consume the whole whitespace run — otherwise
                # _find_cut keeps returning 0 and the loop never advances.
                ws = len(self._buf) - len(self._buf.lstrip())
                raw, self._buf = raw + self._buf[:ws], self._buf[ws:]
            block = self._attribute(raw)
            if block is not None:
                out.append(block)
            self._last_flush = time.monotonic()
        # Safety caps
        cap_block = self._safety_cap()
        if cap_block is not None:
            out.append(cap_block)
        return out

    def _attribute(self, raw: str) -> Block | None:
        """Turn a consumed slice into a Block, routing the whitespace on
        either side of it into ``sep`` / the pending separator.

        Returns None when the slice carried no speech at all (a bare
        paragraph break): its whitespace is held for the next block rather
        than emitted as an empty one.
        """
        body = self._pending_sep + raw
        core = body.rstrip()
        # Whitespace after the block belongs to whatever comes next.
        self._pending_sep = body[len(core):]
        text = core.lstrip()
        if not text:
            self._pending_sep = body
            return None
        return Block(sep=core[: len(core) - len(text)], text=text)

    def _find_cut(self, s: str) -> int | None:
        """Index *after* the sentence mark (or paragraph break) that
        ends the first complete block in *s*, or None if the buffer is
        not yet flushable.
        """
        i = 0
        n = len(s)
        while i < n:
            ch = s[i]
            if ch in _OPEN:
                close = self._match_close(s, i)
                if close is None:
                    return None  # unclosed bracket — wait
                i = close + 1
                continue
            if ch == "\n" and i + 1 < n and s[i + 1] == "\n":
                return i  # paragraph break
            if ch in _SENTENCE_MARKS:
                return i + 1
            i += 1
        return None

    @staticmethod
    def _match_close(s: str, open_idx: int) -> int | None:
        close_char = _OPEN[s[open_idx]]
        depth = 1
        for j in range(open_idx + 1, len(s)):
            c = s[j]
            if c == s[open_idx]:
                depth += 1
            elif c == close_char:
                depth -= 1
                if depth == 0:
                    return j
        return None

    def _safety_cap(self) -> Block | None:
        """If the buffer has blown the char/time cap, force a break."""
        now = time.monotonic()
        too_long = len(self._buf) >= _CHAR_CAP
        too_slow = (now - self._last_flush) >= _TIME_CAP and self._buf
        if not (too_long or too_slow):
            return None

        window = self._buf[:_CHAR_CAP] if too_long else self._buf
        cut = self._rightmost_clause_mark(window)
        if cut is None:
            cut = len(window)  # hard cut
        raw, self._buf = self._buf[:cut], self._buf[cut:]
        self._last_flush = now
        return self._attribute(raw)

    @staticmethod
    def _rightmost_clause_mark(s: str) -> int | None:
        """Return index AFTER rightmost clause mark outside any bracket, or None."""
        depth = 0
        open_stack: list[str] = []
        last: int | None = None
        i = 0
        while i < len(s):
            c = s[i]
            if c in _OPEN:
                open_stack.append(_OPEN[c])
                depth += 1
            elif open_stack and c == open_stack[-1]:
                open_stack.pop()
                depth -= 1
            elif depth == 0 and c in _CLAUSE_MARKS:
                last = i + 1
            i += 1
        return last
