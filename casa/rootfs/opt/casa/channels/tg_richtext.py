"""Fail-literal Markdown → Telegram-entity renderer for single messages.

Agent text goes out as Telegram rich messages (``channels.tg_rich``). This
renderer serves what still carries classic entities: photo and file captions,
the approval card and Casa-composed cards.

* BLOCK segmentation is casa's own line-based scanner for fenced code blocks
  (an UNCLOSED fence keeps its whole remainder byte-literal, bypassing every
  other pass). Byte-fidelity doctrine lives here.
* INLINE semantics run through markdown-it-py (already a casa dependency),
  invoked PER LINE with a restricted rule set (backticks, escape, emphasis,
  link). Construct scope stays one line; a fail-literal cutoff never leaks
  past its line.

Rendered constructs:
  - fenced code blocks  ```` ``` ````            → PRE  (verbatim monospace box)
  - inline code         `code` / ``code``        → CODE
  - bold / italic       **b** / *i*  (asterisks) → BOLD / ITALIC
  - ATX headings        ## Heading               → BOLD line (hashes stripped)
  - labelled links      [label](https://…)       → TEXT_LINK (#404)
  - time links          [label](time:<ISO 8601>) → DATE_TIME over the label

A Markdown table has no entity form and stays literal text.

Doctrine (pinned):
  - Underscores are ALWAYS literal (protects ``mcp__tool__names`` and
    snake_case): ``_x_`` / ``__x__`` re-emit their markers byte-for-byte.
  - Link URLs are http(s) only. A line containing a link with any other
    scheme, an empty label, or image syntax (unescaped ``![``) renders
    FULLY LITERAL — titles and all (nothing is dropped).
  - The one other link target is ``time:`` followed by an ISO 8601 moment
    (``tg_rich.time_instant``). It never makes a line literal: a moment Casa
    can read becomes a DATE_TIME entity over the label, and one it cannot read
    leaves the label as plain text. Casa never looks for times in free text.
  - Bot API nesting: BOLD/ITALIC never contain or intersect CODE,
    TEXT_LINK or DATE_TIME (the ATOMS) — emphasis is SPLIT AROUND those
    atoms at emission. Inside a link label, inner constructs render as
    plain text (a link entity never nests another entity). No entity is ever emitted inside PRE.
  - The parser NEVER raises; anything ambiguous or unsupported degrades to
    literal text, and any entity-conversion failure degrades to plain.

``render()`` returns one message, within the 4096 UTF-16-unit length budget
and the 100-entity budget, or no entities at all.
"""
from __future__ import annotations

import bisect
import re
from datetime import datetime, timezone

from markdown_it import MarkdownIt
from telegram import MessageEntity

from channels.tg_rich import time_instant
from text_util import utf16_len

# kind: pre/code/bold/italic, "link:<url>" or "time:<unix seconds>" (the
# datum rides in the kind).
Span = tuple[int, int, str]

MAX_LEN = 4096
MAX_ENTITIES = 100

_LINK_KIND = "link:"
_TIME_KIND = "time:"

_KIND = {
    "pre": MessageEntity.PRE,
    "code": MessageEntity.CODE,
    "bold": MessageEntity.BOLD,
    "italic": MessageEntity.ITALIC,
}

_HEADING_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<hashes>#{1,6})(?P<gap>[ \t]+)(?P<rest>.*)$")
_TRAILING_HASH_RE = re.compile(r"[ \t]+#+[ \t]*$")

# Inline-only markdown-it: block rules never run (block structure is casa's
# scanner); image/autolink/entity/linkify/strikethrough stay disabled, so
# their syntax stays literal. "zero" + this list is the WHOLE grammar.
_MD = MarkdownIt("zero").enable(
    ["backticks", "escape", "emphasis", "link",
     "balance_pairs", "fragments_join"]
)
_normalize_link = _MD.normalizeLink


def _normalize_link_keeping_time(url: str) -> str:
    # markdown-it reads "time:2026-11-16T09:00+01:00" as a host and port and
    # rewrites it; a time target is Casa's own syntax and is kept as written.
    if url[:len(_TIME_KIND)].lower() == _TIME_KIND:
        return url
    return _normalize_link(url)


_MD.normalizeLink = _normalize_link_keeping_time


def _is_atom(kind: str) -> bool:
    """A span emphasis is split around: CODE, TEXT_LINK and DATE_TIME."""
    return kind == "code" or kind.startswith((_LINK_KIND, _TIME_KIND))


def parse_markdown(src: str) -> tuple[str, list[Span]]:
    """Return (display_text, spans). Never raises."""
    display_parts: list[str] = []
    spans: list[Span] = []
    pos = 0
    for kind, content in _split_fenced(src):
        if kind == "pre":
            display_parts.append(content)
            spans.append((pos, pos + len(content), "pre"))
            pos += len(content)
        elif kind == "literal":
            display_parts.append(content)
            pos += len(content)
        else:
            text, inline = _parse_text(content)
            display_parts.append(text)
            spans.extend((pos + s, pos + e, k) for s, e, k in inline)
            pos += len(text)
    spans.sort(key=lambda x: (x[0], -x[1], x[2]))
    return "".join(display_parts), spans


def _split_fenced(src: str) -> list[tuple[str, str]]:
    """Split into ('pre', inner), ('literal', chunk) and ('text', chunk)
    blocks, line-based.

    A fence opens on a line whose stripped text is ``` optionally followed by a
    language token (no spaces, no backticks) and closes on a line whose stripped
    text is exactly ```. An UNCLOSED fence keeps the opener and remainder as a
    'literal' block — byte-for-byte, and exempt from the
    inline pass (round-3 rule). Separator newlines are preserved as ordinary
    text, never part of the PRE span.
    """
    lines = src.splitlines(keepends=True)
    blocks: list[tuple[str, str]] = []
    text_buf: list[str] = []
    i = 0

    def body(line: str) -> str:
        return line.rstrip("\r\n")

    def eol(line: str) -> str:
        return line[len(body(line)):]

    def strip_one_eol(text: str) -> str:
        if text.endswith("\r\n"):
            return text[:-2]
        if text.endswith(("\n", "\r")):
            return text[:-1]
        return text

    def is_opener(line: str) -> bool:
        stripped = body(line).strip()
        if not stripped.startswith("```"):
            return False
        tail = stripped[3:]
        return "`" not in tail and not any(ch.isspace() for ch in tail)

    def flush_text() -> None:
        if text_buf:
            blocks.append(("text", "".join(text_buf)))
            text_buf.clear()

    while i < len(lines):
        if not is_opener(lines[i]):
            text_buf.append(lines[i])
            i += 1
            continue
        close = i + 1
        while close < len(lines) and body(lines[close]).strip() != "```":
            close += 1
        if close == len(lines):
            flush_text()  # unclosed → the whole remainder is literal
            blocks.append(("literal", "".join(lines[i:])))
            return blocks
        flush_text()
        blocks.append(("pre", strip_one_eol("".join(lines[i + 1:close]))))
        if close + 1 < len(lines):
            text_buf.append(eol(lines[close]))  # closing newline is ordinary text
        i = close + 1

    flush_text()
    return blocks


# ---------------------------------------------------------------------------
# Inline pass: one markdown-it parseInline per line.
# ---------------------------------------------------------------------------


def _backslash_run_before(s: str, i: int) -> int:
    j = i
    while j > 0 and s[j - 1] == "\\":
        j -= 1
    return i - j


def _has_unescaped_image_marker(body: str) -> bool:
    # Escaped only under ODD backslash parity: in "\\![…" the backslashes
    # pair to a literal backslash and the image marker is live (diff-review
    # round 1, Sol + Terra).
    i = body.find("![")
    while i != -1:
        if _backslash_run_before(body, i) % 2 == 0:
            return True
        i = body.find("![", i + 1)
    return False


def _inline_line(body: str) -> "tuple[str, list[Span]] | None":
    """Render ONE line's inline constructs → (display, spans), or ``None``
    when the line must stay fully literal (image syntax, a link with a
    disallowed scheme or an empty label, or any walk surprise). A ``time:``
    link is never a reason for that: its label is kept, as a DATE_TIME span
    when the moment parses and as plain text when it does not. Spans are in
    line-local display coordinates. Never raises."""
    if not body:
        return body, []
    if _has_unescaped_image_marker(body):
        return None
    try:
        tokens = _MD.parseInline(body, {})
        children = tokens[0].children if tokens else None
    except Exception:  # noqa: BLE001 — fail-literal, never raise
        return None
    if children is None:
        return None

    out: list[str] = []
    n = 0  # display cursor
    emph_stack: list[tuple[str | None, int]] = []
    link_stack: list[tuple[int, str | None]] = []
    code_spans: list[tuple[int, int]] = []
    # kind None: a time link whose moment does not parse — label kept, no span
    link_spans: list[tuple[int, int, str | None]] = []
    emph_spans: list[Span] = []

    for c in children:
        t = c.type
        if t == "text":
            out.append(c.content)
            n += len(c.content)
        elif t == "code_inline":
            start = n
            out.append(c.content)
            n += len(c.content)
            if not link_stack:  # inside a label: plain text, span suppressed
                code_spans.append((start, n))
        elif t in ("em_open", "strong_open"):
            if c.markup in ("*", "**"):
                kind = "bold" if c.markup == "**" else "italic"
                emph_stack.append((kind, n))
            else:  # underscore doctrine: markers are literal, re-emitted
                out.append(c.markup)
                n += len(c.markup)
                emph_stack.append((None, n))
        elif t in ("em_close", "strong_close"):
            if not emph_stack:
                return None
            kind, start = emph_stack.pop()
            if kind is None:
                out.append(c.markup)
                n += len(c.markup)
            elif not link_stack:  # inside a label: span suppressed
                emph_spans.append((start, n, kind))
        elif t == "link_open":
            if link_stack:  # nested TEXT_LINKs are Bot-API-invalid
                return None
            href = str(c.attrs.get("href", ""))
            if href[:len(_TIME_KIND)].lower() == _TIME_KIND:
                instant = time_instant(href)
                kind = None if instant is None else f"{_TIME_KIND}{instant}"
            elif href.lower().startswith(("http://", "https://")):
                kind = _LINK_KIND + href
            else:
                return None
            link_stack.append((n, kind))
        elif t == "link_close":
            if not link_stack:
                return None
            start, kind = link_stack.pop()
            link_spans.append((start, n, kind))
        elif t in ("softbreak", "hardbreak"):
            out.append("\n")
            n += 1
        else:  # unexpected token type ⇒ the whole line stays literal
            return None

    if emph_stack or link_stack:
        return None
    display = "".join(out)
    for s, e, _ in link_spans:
        if not display[s:e].strip():  # empty/blank label: URL must stay visible
            return None
    atom_spans: list[Span] = [sp for sp in link_spans if sp[2] is not None]

    holes = sorted(code_spans + [(s, e) for s, e, _ in atom_spans])
    spans: list[Span] = [(s, e, "code") for s, e in code_spans]
    spans.extend(atom_spans)
    for s, e, kind in emph_spans:
        spans.extend(
            (x, y, kind) for x, y in _subtract_intervals(s, e, holes)
        )
    return display, [sp for sp in spans if sp[1] > sp[0]]


def _parse_text(text: str) -> tuple[str, list[Span]]:
    """Line-oriented pass: headings + the markdown-it inline walk per line.

    A line the walker refuses (``_inline_line`` → ``None``) is emitted
    byte-for-byte with no spans — including its heading hashes."""
    parts: list[str] = []
    raw_spans: list[Span] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]

        heading_prefix = None
        content = body
        m = _HEADING_RE.match(body)
        if m and m.group("rest").strip():
            rest = m.group("rest")
            content_end = len(rest)
            tm = _TRAILING_HASH_RE.search(rest)
            if tm and rest[: tm.start()].strip():
                content_end = tm.start()
            heading_prefix = m.end("gap")
            content = rest[:content_end]

        rendered = _inline_line(content)
        if rendered is None:
            parts.append(body)
            pos += len(body)
        else:
            disp, ispans = rendered
            parts.append(disp)
            raw_spans.extend((pos + s, pos + e, k) for s, e, k in ispans)
            if heading_prefix is not None:
                holes = sorted((s, e) for s, e, k in ispans if _is_atom(k))
                raw_spans.extend(
                    (pos + s, pos + e, "bold")
                    for s, e in _subtract_intervals(0, len(disp), holes)
                )
            pos += len(disp)
        parts.append(ending)
        pos += len(ending)

    emph = [sp for sp in raw_spans if sp[2] in ("bold", "italic")]
    rest = [sp for sp in raw_spans if sp[2] not in ("bold", "italic")]
    return "".join(parts), rest + _merge_same_kind(emph)


def _subtract_intervals(
    start: int, end: int, holes: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Subtract sorted, disjoint *holes* from ``[start, end)``; drop empties.

    Bisects to the first hole that can overlap so a message with thousands of
    code spans stays O(k log n) per emphasis span, not O(n) (Sol impl-review:
    the linear scan made pathological inputs quadratic overall)."""
    pieces: list[tuple[int, int]] = []
    cur = start
    idx = bisect.bisect_left(holes, (start,))
    if idx:
        idx -= 1  # the previous hole may still overlap ``start``
    for hs, he in holes[idx:]:
        if hs >= end:
            break
        if he <= cur:
            continue
        if hs > cur:
            pieces.append((cur, hs))
        cur = max(cur, he)
    if cur < end:
        pieces.append((cur, end))
    return pieces


def _merge_same_kind(spans: list[Span]) -> list[Span]:
    """Union overlapping (incl. nested/duplicate) same-kind emphasis spans —
    e.g. a heading's line bold over an inner ``**bold**`` emits ONE entity."""
    out: list[Span] = []
    for kind in ("bold", "italic"):
        ranges = sorted((s, e) for s, e, k in spans if k == kind)
        merged: list[list[int]] = []
        for s, e in ranges:
            if merged and s < merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        out.extend((s, e, kind) for s, e in merged)
    out.extend(sp for sp in spans if sp[2] not in ("bold", "italic"))
    return out


# ---------------------------------------------------------------------------
# Delivery: single-message render.
# ---------------------------------------------------------------------------


def _utf16_units(s: str) -> int:
    # #305: delegates to the shared text_util measurement so the renderer
    # and the authz size gate use ONE unit.
    return utf16_len(s)


def _spans_to_entities(
    display: str, spans: list[Span],
) -> "list[MessageEntity] | None":
    """Validate spans against *display* and convert to UTF-16 entities;
    ``None`` on any invalid offset, unknown kind, empty link URL, or
    conversion failure (never raises)."""
    try:
        ents: list[MessageEntity] = []
        for start, end, kind in spans:
            if end <= start or start < 0 or end > len(display):
                return None
            if kind.startswith(_LINK_KIND):
                url = kind[len(_LINK_KIND):]
                if not url:
                    return None
                ents.append(MessageEntity(
                    type=MessageEntity.TEXT_LINK,
                    offset=start, length=end - start, url=url,
                ))
            elif kind.startswith(_TIME_KIND):
                # No date_time_format: the reader sees the label as written.
                ents.append(MessageEntity(
                    type=MessageEntity.DATE_TIME,
                    offset=start, length=end - start,
                    unix_time=datetime.fromtimestamp(
                        int(kind[len(_TIME_KIND):]), timezone.utc),
                ))
            else:
                ents.append(MessageEntity(
                    type=_KIND[kind], offset=start, length=end - start,
                ))
        return MessageEntity.adjust_message_entities_to_utf_16(display, ents)
    except Exception:  # noqa: BLE001 — e.g. an unpaired surrogate breaks UTF-16
        # "never raises": any offset-conversion failure degrades to plain text.
        return None


def render(text: str) -> tuple[str, "list[MessageEntity] | None"]:
    """Return (display_text, entities) — entities validated + UTF-16-adjusted, or
    None when the message must be sent plain (no spans, over limits, or invalid).

    v2: judged on DISPLAY length — a raw text whose markers push it past 4096
    but whose display fits still renders. Single-message contract.

    When entities is None, send ``plain_text(text)`` — not *display*, which
    has lost the formatting of a text over the limits, and not *text*, which
    shows the escapes of a text with no span (#1330)."""
    display, spans = parse_markdown(text)
    if (
        not spans
        or len(display) > MAX_LEN
        or _utf16_units(display) > MAX_LEN
        or len(spans) > MAX_ENTITIES
    ):
        return display, None
    return display, _spans_to_entities(display, spans)


def plain_text(text: str) -> str:
    """What a single message sent WITHOUT entities shows for *text* (#1330).

    A text with no span: its display, exactly as the rich path would show it —
    the backslash escapes consumed, a NUL replaced. Without a span there is
    no link address to lose; a plugin card escaped for markdown no longer
    shows its backslashes just because it carries no bold word. A display that
    would not fit one message is not used: the authored text goes out as before.

    A text WITH spans that are not being sent (over the limits, or not
    convertible): the authored text, unchanged — its markers are the only
    place its formatting and its link addresses still exist (fail-literal).

    Returns *text* itself whenever the answer equals it, so a caller holding
    a subclass of ``str`` keeps it in the common case. Never raises."""
    display, spans = parse_markdown(text)
    if spans or display == text or _utf16_units(display) > MAX_LEN:
        return text
    return display
