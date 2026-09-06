"""Small text utilities shared across Casa.

Houses ``truncate_for_topic``, used by tools.py to fit Telegram
forum-topic names within the Bot API's ~128-byte limit without
slicing mid-word (E-9 from bug-review-2026-04-29); ``sanitize_segment``,
the documented Claude Code plugin MCP-tool-name namespace sanitization
(re-exported by ``plugin_grants`` for its existing callers/tests); and
``is_unsafe_text``, the pinned UNSAFE-TEXT codepoint predicate (v0.78.0
design doc, W1/W2) reused verbatim by ``plugin_store.manifest_protected_tools``
for protectedTools summary templates (W1) and by the W2 challenge
renderer for interpolated values and display names; and ``utf16_len`` /
``utf16_prefix_end``, the shared UTF-16 code-unit measurement Telegram's
Bot API limits are counted in (#305/#328).

STDLIB-ONLY (no third-party imports): ``plugin_store.py`` is copied into
the image and imported by the Dockerfile build helper BEFORE any venv
exists, so anything it imports — including this module — must stay
stdlib-only.
"""

from __future__ import annotations

import re

# Telegram Bot API limit for createForumTopic 'name' parameter, in
# bytes. (Documented as ~128; tested empirically at 128.)
TELEGRAM_TOPIC_NAME_BYTES = 128

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_segment(s: str) -> str:
    """Documented CC sanitization for MCP-tool namespace segments: any char
    outside ``A-Za-z0-9_-`` becomes ``_``."""
    return _SANITIZE_RE.sub("_", s)


# UNSAFE-TEXT predicate (pinned, 2026-07-14 approval-summaries design
# section W1/W2): text is UNSAFE iff it contains any codepoint in
# U+0000-001F (C0, incl. newline/CR), U+007F-009F (DEL + C1),
# U+2028/U+2029 (line/paragraph separator), U+061C (Arabic Letter Mark),
# U+200E/U+200F (LRM/RLM), U+202A-202E (bidi embedding/override), or
# U+2066-2069 (bidi isolates). Single-line-ness is implied by the C0
# exclusion. Built from an explicit integer codepoint table (never a
# raw literal control/bidi glyph in source) so the pinned ranges stay
# auditable.
_UNSAFE_TEXT_RANGES = (
    (0x0000, 0x001F),
    (0x007F, 0x009F),
    (0x2028, 0x2028),
    (0x2029, 0x2029),
    (0x061C, 0x061C),
    (0x200E, 0x200F),
    (0x202A, 0x202E),
    (0x2066, 0x2069),
)
_UNSAFE_TEXT_RE = re.compile(
    "[" + "".join(chr(lo) + "-" + chr(hi) for lo, hi in _UNSAFE_TEXT_RANGES) + "]"
)


def utf16_len(s: str) -> int:
    """Length of *s* in UTF-16 code units — the unit Telegram's Bot API counts
    for message-length limits and entity offsets (#305/#328). Astral
    codepoints (most emoji) are surrogate pairs and count as 2; everything in
    the BMP counts as 1. Summed per-character rather than via
    ``encode("utf-16-le")`` so a lone surrogate (surrogateescape'd input) is
    measured instead of raising mid-send. This is the SINGLE home for the
    measurement: the plain splitter, the streaming edit checks, the rich
    paginator, and the authz challenge size gate all resolve here so they can
    never drift apart again."""
    return sum(2 if ord(ch) >= 0x10000 else 1 for ch in s)


def replace_lone_surrogates(text: str) -> str:
    """*text* with every surrogate code point (U+D800-U+DFFF) replaced by U+FFFD.

    ONE code point for one, so every Python offset and every UTF-16 unit is
    preserved: entities computed before the replacement stay valid after it,
    and a budget measured by :func:`utf16_len` (which counts a lone surrogate
    and U+FFFD as one unit each) is invariant under it. Deleting instead of
    replacing would break both.

    Why it exists: python-telegram-bot sends every request as UTF-8 FORM data,
    so a string parameter still carrying a lone surrogate — a JSON ``\\ud800``
    escape in a model or tool result decodes to one — raises
    ``NetworkError(UnicodeEncodeError)`` before any request leaves the process.
    Two callers: the paginating renderer's conversion-failure arm (#834) and
    the channel's request boundary below every sender (#846, INV-TG-009)."""
    if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return text
    return "".join(
        "\ufffd" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in text)


def utf16_prefix_end(s: str, start: int, budget: int) -> int:
    """Largest index ``end`` such that ``s[start:end]`` fits *budget* UTF-16
    units. Because the budget is spent per-codepoint, the boundary can never
    land between the two units of a surrogate pair."""
    units = 0
    i = start
    n = len(s)
    while i < n:
        units += 2 if ord(s[i]) >= 0x10000 else 1
        if units > budget:
            return i
        i += 1
    return n


def is_unsafe_text(s: str) -> bool:
    """True iff ``s`` contains any UNSAFE-TEXT codepoint (see module docstring
    and the regex comment above). Reused verbatim for protectedTools summary
    templates (W1), and — unmodified by this change — intended for W2's
    interpolated values and display names, so all three call sites can never
    drift apart."""
    return bool(_UNSAFE_TEXT_RE.search(s))


def escape_unsafe_text(s: str) -> str:
    """Replace every UNSAFE-TEXT codepoint in *s* with its visible ``\\uXXXX``
    escape (#328). Built for display surfaces that must show operator-facing
    text FAITHFULLY: a bidi override left raw can reorder what the operator
    reads (``safe\\u202egpj.exe`` displays as a benign ``...jpg`` name), so the
    unsafe codepoint is spelled out instead of rendered. Inside a JSON string
    the replacement is itself a valid JSON escape for the same value, so an
    escaped canonical-args block still parses to the bound arguments."""
    return _UNSAFE_TEXT_RE.sub(lambda m: "\\u%04x" % ord(m.group()), s)


_ELLIPSIS = "…"
_ELLIPSIS_BYTES = len(_ELLIPSIS.encode("utf-8"))  # 3
_TRAILING_PUNCT = ",;:."


def truncate_for_topic(text: str, *, byte_budget: int) -> str:
    """Truncate ``text`` so its UTF-8 byte length is ≤ ``byte_budget``.

    Breaks on the last whitespace boundary when possible and signals
    truncation with a trailing Unicode ellipsis '…' (3 UTF-8 bytes).
    Strips trailing punctuation in {',;:.'} before the ellipsis to
    avoid orphan punctuation. The returned string's UTF-8 byte length
    is *strictly* ≤ byte_budget.

    Edge cases:
    - empty ``text`` → empty string.
    - ``text`` already fits → returned unchanged.
    - ``byte_budget`` < 3 (cannot fit '…') → empty string.
    - ``byte_budget`` ≥ 3 but no whitespace boundary fits → hard
      byte-cut on the last UTF-8 boundary that fits within
      ``byte_budget - 3`` bytes, then append '…'.
    """
    if not text:
        return ""

    raw = text.encode("utf-8")
    if len(raw) <= byte_budget:
        return text

    if byte_budget < _ELLIPSIS_BYTES:
        return ""

    # Budget for the body (everything before '…').
    body_byte_budget = byte_budget - _ELLIPSIS_BYTES

    # Walk text char-by-char, accumulating bytes, tracking the last
    # whitespace boundary as a candidate break point.
    body: list[str] = []
    body_bytes = 0
    last_space_idx_in_body = -1  # index into body[] just AFTER a space
    for ch in text:
        ch_bytes = len(ch.encode("utf-8"))
        if body_bytes + ch_bytes > body_byte_budget:
            break
        body.append(ch)
        body_bytes += ch_bytes
        if ch == " ":
            last_space_idx_in_body = len(body)  # break AT the space

    if not body:
        # Even the first char doesn't fit; return just the ellipsis.
        return _ELLIPSIS

    # Prefer to break on the last whitespace if we found one; else
    # hard-cut.
    if last_space_idx_in_body > 0:
        truncated = "".join(body[:last_space_idx_in_body])
    else:
        truncated = "".join(body)

    # Strip trailing whitespace and orphan punctuation.
    truncated = truncated.rstrip()
    while truncated and truncated[-1] in _TRAILING_PUNCT:
        truncated = truncated[:-1].rstrip()

    return truncated + _ELLIPSIS
