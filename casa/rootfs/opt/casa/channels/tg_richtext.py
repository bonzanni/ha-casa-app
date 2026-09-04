"""Fail-literal Markdown → Telegram-entity renderer (v3, 2026-08-12 rework).

Hybrid architecture, converged with Sol + Terra over three design rounds:

* BLOCK segmentation is casa's own line-based scanner — fenced code blocks
  (an UNCLOSED fence keeps its whole remainder byte-literal, bypassing every
  other pass) and confident table runs. Byte-fidelity doctrine lives here.
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
  - markdown tables     | a | b |                → padded PRE box, per-record
                                                   stanza, or inline rows (#506)

Doctrine (pinned):
  - Underscores are ALWAYS literal (protects ``mcp__tool__names`` and
    snake_case): ``_x_`` / ``__x__`` re-emit their markers byte-for-byte.
  - Link URLs are http(s) only. A line containing a link with any other
    scheme, an empty label, or image syntax (unescaped ``![``) renders
    FULLY LITERAL — titles and all (round-3 rule; nothing is dropped).
  - Bot API nesting: BOLD/ITALIC never contain or intersect CODE or
    TEXT_LINK — emphasis is SPLIT AROUND those atoms at emission. Inside a
    link label, inner constructs render as plain text (a link entity never
    nests another entity). No entity is ever emitted inside PRE.
  - The parser NEVER raises; anything ambiguous or unsupported degrades to
    literal text, and any entity-conversion failure degrades to plain.

Tables (#506): a confident table (separatored, or separator-less with >= 3
consistent rows — v0.109 shape) is re-emitted from parsed cells (``\\|`` is
cell content; unescaped pipes split, even inside backticks — GFM rule).
Three forms, chosen so content and URLs are NEVER dropped: a padded PRE box
(narrow, link-free), a per-record ``Header: value`` stanza (wide or
link-bearing, real non-empty link-free header, >= 1 data row), or inline
rows with entities intact (every remaining case). Ragged runs stay text.

``render()`` returns one message; ``render_paged()`` is the delivery planner —
it parses ONCE, cuts the display at preferred boundaries (paragraph → line →
space → hard), clips/rebases spans per page (a span crossing a cut becomes one
entity per page; PRE included, and a split TEXT_LINK carries its URL on every
page), and enforces BOTH the 4096 UTF-16-unit length budget AND the
100-entity budget per page.
"""
from __future__ import annotations

import bisect
import logging
import re

from markdown_it import MarkdownIt
from telegram import MessageEntity

from text_util import utf16_len, utf16_prefix_end

# kind: pre/code/bold/italic, or "link:<url>" (URL rides in the kind so the
# pagination clip/rebase logic carries it across page cuts unchanged).
Span = tuple[int, int, str]

logger = logging.getLogger(__name__)

MAX_LEN = 4096
MAX_ENTITIES = 100

_LINK_KIND = "link:"
_TABLE_PRE_MAX_WIDTH = 40  # padded monospace wider than a phone → stanza

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


def parse_markdown(src: str) -> tuple[str, list[Span]]:
    """Return (display_text, spans). Never raises."""
    display_parts: list[str] = []
    spans: list[Span] = []
    pos = 0
    for kind, content in _split_blocks(src):
        if kind == "pre":
            display_parts.append(content)
            spans.append((pos, pos + len(content), "pre"))
            pos += len(content)
        elif kind == "literal":
            display_parts.append(content)
            pos += len(content)
        elif kind == "table":
            text, tspans = content
            display_parts.append(text)
            spans.extend((pos + s, pos + e, k) for s, e, k in tspans)
            pos += len(text)
        else:
            text, inline = _parse_text(content)
            display_parts.append(text)
            spans.extend((pos + s, pos + e, k) for s, e, k in inline)
            pos += len(text)
    spans.sort(key=lambda x: (x[0], -x[1], x[2]))
    return "".join(display_parts), spans


def _split_blocks(src: str):
    """Fence split, then confident-table split inside the text blocks."""
    blocks = []
    for kind, content in _split_fenced(src):
        if kind == "text":
            blocks.extend(_split_tables(content))
        else:
            blocks.append((kind, content))
    return blocks


def _split_fenced(src: str) -> list[tuple[str, str]]:
    """Split into ('pre', inner), ('literal', chunk) and ('text', chunk)
    blocks, line-based.

    A fence opens on a line whose stripped text is ``` optionally followed by a
    language token (no spaces, no backticks) and closes on a line whose stripped
    text is exactly ```. An UNCLOSED fence keeps the opener and remainder as a
    'literal' block — byte-for-byte, and exempt from table detection and the
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
    disallowed scheme or an empty label, or any walk surprise). Spans are in
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
    link_stack: list[tuple[int, str]] = []
    code_spans: list[tuple[int, int]] = []
    link_spans: list[Span] = []
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
            if not href.lower().startswith(("http://", "https://")):
                return None
            link_stack.append((n, href))
        elif t == "link_close":
            if not link_stack:
                return None
            start, href = link_stack.pop()
            link_spans.append((start, n, _LINK_KIND + href))
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

    holes = sorted(code_spans + [(s, e) for s, e, _ in link_spans])
    spans: list[Span] = [(s, e, "code") for s, e in code_spans]
    spans.extend(link_spans)
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
                holes = sorted(
                    (s, e) for s, e, k in ispans
                    if k == "code" or k.startswith(_LINK_KIND)
                )
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


# ---------------------------------------------------------------------------
# Tables (#506).
# ---------------------------------------------------------------------------

_TABLE_SEP_CELL_RE = re.compile(r"\s*:?-{3,}:?\s*")


def _split_cells(stripped_row: str) -> list[str]:
    """Cells of a bordered row: split on UNESCAPED pipes. A pipe is escaped
    only when preceded by an ODD run of backslashes — ``\\|`` is cell
    content, ``\\\\|`` is a literal backslash then a structural pipe
    (diff-review round 1). Escapes stay in the cell source; the inline
    escape pass renders them."""
    inner = stripped_row[1:-1]
    cells: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == "\\":
            j = i
            while j < len(inner) and inner[j] == "\\":
                j += 1
            run = j - i
            if j < len(inner) and inner[j] == "|" and run % 2 == 1:
                cur.append(inner[i:j + 1])  # escaped pipe is cell content
                i = j + 1
            else:
                cur.append(inner[i:j])
                i = j
        elif ch == "|":
            cells.append("".join(cur))
            cur = []
            i += 1
        else:
            cur.append(ch)
            i += 1
    cells.append("".join(cur))
    return cells


def _table_shape(stripped_rows: list[str]) -> "str | None":
    """CONFIDENT markdown table shape: 'sep' (header + ``|---|`` separator
    row, consistent columns), 'nosep' (v0.109, deliberately NARROW: >= 3
    rows, consistent counts, >= 2 columns, every row non-empty), or None.
    Markers inside cells no longer disqualify — cells are re-emitted from a
    parse, not passed through verbatim (#506 mode-A fix)."""
    if len(stripped_rows) < 2:
        return None
    ncols = len(_split_cells(stripped_rows[0]))
    sep = _split_cells(stripped_rows[1])
    if all(_TABLE_SEP_CELL_RE.fullmatch(c) for c in sep):
        if len(sep) == ncols and all(
            len(_split_cells(row)) == ncols for row in stripped_rows
        ):
            return "sep"
        return None
    if len(stripped_rows) < 3 or ncols < 2:
        return None
    ok = all(
        len(cells) == ncols and any(c.strip() for c in cells)
        for cells in map(_split_cells, stripped_rows)
    )
    return "nosep" if ok else None


def _split_tables(text: str) -> list[tuple[str, object]]:
    """Split ('text', chunk) blocks around confident table blocks, each
    pre-rendered to ('table', (display, spans)). A candidate block is >= 2
    CONTIGUOUS lines that (stripped) start AND end with an unescaped ``|``;
    it must match a ``_table_shape`` or the lines stay ordinary text
    (fail-literal). The last row's line ending stays ordinary text."""
    lines = text.splitlines(keepends=True)
    blocks: list[tuple[str, object]] = []
    text_buf: list[str] = []

    def flush() -> None:
        if text_buf:
            blocks.append(("text", "".join(text_buf)))
            text_buf.clear()

    def bordered(stripped: str) -> bool:
        return (
            len(stripped) >= 2
            and stripped.endswith("|")
            # a closing border is escaped only under ODD backslash parity
            and _backslash_run_before(stripped, len(stripped) - 1) % 2 == 0
        )

    i = 0
    while i < len(lines):
        # The candidate group is the run of CONSECUTIVE pipe-starting lines;
        # one row without an unescaped closing border poisons the WHOLE run
        # (diff-review round 2: no valid-prefix table may form from it).
        j = i
        while j < len(lines):
            stripped = lines[j].rstrip("\r\n").strip()
            if stripped.startswith("|"):  # even a bare "|" joins the run
                j += 1
            else:
                break
        rows = [lines[k].rstrip("\r\n").strip() for k in range(i, j)]
        shape = (
            _table_shape(rows)
            if j - i >= 2 and all(bordered(r) for r in rows)
            else None
        )
        if shape:
            flush()
            blocks.append(("table", _render_table(rows, shape == "sep")))
            ending = lines[j - 1][len(lines[j - 1].rstrip("\r\n")):]
            if ending:
                text_buf.append(ending)
            i = j
        elif j > i:
            text_buf.extend(lines[i:j])  # rejected run stays text, whole
            i = j
        else:
            text_buf.append(lines[i])
            i += 1

    flush()
    return blocks


def _render_table(
    rows: list[str], separatored: bool,
) -> tuple[str, list[Span]]:
    """Re-emit a confident table in ONE of three forms — chosen so cell
    content and link URLs are NEVER dropped (design rounds 1-3):

    1. padded PRE box: link-free AND padded width <= 40 (cell entity spans
       are discarded — the Bot API forbids entities inside PRE; the
       marker-stripped monospace box is the honest render);
    2. per-record ``Header: value`` stanza: wide or link-bearing, and the
       header is REAL (separatored), non-empty, link-free, with >= 1 data
       row — values keep their entity spans, the bold header label splits
       around header CODE spans;
    3. inline rows: every remaining case — cells re-emitted between literal
       pipes with their entity spans intact.
    """
    src_cells = [_split_cells(r) for r in rows]
    body_cells = (
        [src_cells[0]] + src_cells[2:] if separatored else src_cells
    )
    rendered: list[list[tuple[str, list[Span]]]] = []
    for row in body_cells:
        rrow = []
        for cell in row:
            r = _inline_line(cell.strip())
            rrow.append(r if r is not None else (cell.strip(), []))
        rendered.append(rrow)

    ncols = len(rendered[0])
    widths = [max(len(row[c][0]) for row in rendered) for c in range(ncols)]
    has_link = any(
        k.startswith(_LINK_KIND)
        for row in rendered for _, sp in row for _, _, k in sp
    )
    padded_width = sum(widths) + 3 * (ncols - 1) + 4

    if not has_link and padded_width <= _TABLE_PRE_MAX_WIDTH:
        text = "\n".join(
            "| " + " | ".join(
                row[c][0].ljust(widths[c]) for c in range(ncols)
            ) + " |"
            for row in rendered
        )
        return text, [(0, len(text), "pre")]

    header, data = rendered[0], rendered[1:]
    header_ok = (
        separatored
        and data
        # >= 1 non-empty rendered value, or the stanza would emit an empty
        # message (diff-review round 2)
        and any(v[0].strip() for row in data for v in row)
        and all(h[0].strip() for h in header)
        and not any(
            k.startswith(_LINK_KIND) for _, sp in header for _, _, k in sp
        )
    )
    if header_ok:
        return _render_stanza(header, data)

    parts: list[str] = []
    spans: list[Span] = []
    pos = 0
    for row in rendered:
        line_cells: list[str] = []
        cursor = pos + 2  # "| "
        for c in range(ncols):
            disp, csp = row[c]
            spans.extend((cursor + s, cursor + e, k) for s, e, k in csp)
            line_cells.append(disp)
            cursor += len(disp) + 3  # " | "
        parts.append("| " + " | ".join(line_cells) + " |")
        pos += len(parts[-1]) + 1  # "\n"
    return "\n".join(parts), spans


def _render_stanza(
    header: list[tuple[str, list[Span]]],
    data: list[list[tuple[str, list[Span]]]],
) -> tuple[str, list[Span]]:
    parts: list[str] = []
    spans: list[Span] = []
    pos = 0
    first_record = True
    for row in data:
        lines_of_record: list[str] = []
        for c, (vdisp, vspans) in enumerate(row):
            hdisp, hspans = header[c]
            if not vdisp.strip():
                continue
            if not lines_of_record and not first_record:
                parts.append("\n")
                pos += 1
            line = hdisp + ": " + vdisp
            label_end = len(hdisp) + 1  # includes the colon
            holes = sorted((s, e) for s, e, k in hspans if k == "code")
            spans.extend((pos + s, pos + e, "code") for s, e in holes)
            spans.extend(
                (pos + s, pos + e, "bold")
                for s, e in _subtract_intervals(0, label_end, holes)
            )
            vbase = pos + label_end + 1
            spans.extend((vbase + s, vbase + e, k) for s, e, k in vspans)
            parts.append(line + "\n")
            pos += len(line) + 1
            lines_of_record.append(line)
        if lines_of_record:
            first_record = False
    text = "".join(parts)
    if text.endswith("\n"):  # the block's own line ending is emitted outside
        text = text[:-1]
    return text, [sp for sp in spans if sp[1] <= len(text)]


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
# Delivery: single-message render + the paged delivery planner.
# ---------------------------------------------------------------------------


def _utf16_units(s: str) -> int:
    # #305: delegates to the shared text_util measurement so the rich
    # paginator, the plain splitter, and the authz size gate use ONE unit.
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
    but whose display fits still renders. Single-message contract; callers
    that may exceed one message use ``render_paged()``."""
    display, spans = parse_markdown(text)
    if (
        not spans
        or len(display) > MAX_LEN
        or _utf16_units(display) > MAX_LEN
        or len(spans) > MAX_ENTITIES
    ):
        return display, None
    return display, _spans_to_entities(display, spans)


def _link_spans(display: str, entities) -> "list[tuple[int, int, str]]":
    """``(python_start, python_end, url)`` for every TEXT_LINK in *entities*
    whose url is not already visible inside its own label, in display order.

    ``[]`` on anything unusable — the callers below are plain-fallback paths
    and must degrade rather than raise (#831). Entity offsets are UTF-16 units
    (``MessageEntity.adjust_message_entities_to_utf_16`` was applied when the
    entities were built), so each boundary converts back through
    ``text_util.utf16_prefix_end``, the shared measurement helper, and is
    round-trip checked: an offset that does not map back to itself lands
    inside a surrogate pair and the entity is dropped rather than guessed at.
    """
    try:
        out: list[tuple[int, int, str]] = []
        for ent in entities or []:
            if getattr(ent, "type", None) != MessageEntity.TEXT_LINK:
                continue
            url = getattr(ent, "url", None)
            offset, length = getattr(ent, "offset", None), getattr(ent, "length", None)
            if not url or type(offset) is not int or type(length) is not int:
                continue
            if offset < 0 or length <= 0:
                continue
            start = utf16_prefix_end(display, 0, offset)
            end = utf16_prefix_end(display, start, length)
            if _utf16_units(display[:start]) != offset:
                continue
            if _utf16_units(display[start:end]) != length:
                continue
            if url in display[start:end]:
                # An autolink whose label IS the address already carries it.
                continue
            out.append((start, end, url))
        out.sort(key=lambda span: span[0])
        return out
    except Exception:  # noqa: BLE001 — never raise on a fallback path
        return []


def plain_with_link_targets(display: str, entities) -> str:
    """*display* with every TEXT_LINK destination re-attached, as ``label (url)``.

    #831: ``render_paged`` pages are marker-free DISPLAY slices, so a multi-page
    sender whose entities Telegram rejects has only the display to retry with —
    and a ``TEXT_LINK``'s url is the ONE datum a display cannot carry (``_KIND``
    is closed to pre/code/bold/italic, and ``_spans_to_entities`` emits only
    those plus TEXT_LINK). Reconstructing from ``(display, entities)`` is
    therefore lossless, needs no authored source, and leaves ``render_paged``
    and its two-tuple shape untouched.

    Insertions run right-to-left so an earlier index is never invalidated.
    Non-link entities are ignored: their information is already in the display.
    NEVER raises — an exception here would turn a formatting nuisance into a
    lost turn — and returns *display* unchanged when there is nothing to add.
    """
    spans = _link_spans(display, entities)
    if not spans:
        return display
    out = display
    for _, end, url in reversed(spans):
        out = f"{out[:end]} ({url}){out[end:]}"
    return out


def missing_link_targets(display: str, entities) -> list[str]:
    """The destinations ``plain_with_link_targets`` would add, in order, without
    duplicates — the overflow form, one url per line, used when the inline
    reconstruction does not fit one message (#831)."""
    seen: list[str] = []
    for _, _, url in _link_spans(display, entities):
        if url not in seen:
            seen.append(url)
    return seen


def _advance_by_utf16(display: str, start: int, budget: int) -> int:
    """Largest index ``end`` such that ``display[start:end]`` fits *budget*
    UTF-16 units (shared implementation: ``text_util.utf16_prefix_end``)."""
    return utf16_prefix_end(display, start, budget)


def _preferred_cut(display: str, start: int, end: int) -> int:
    """Pick a cut in ``(start, end]`` preferring paragraph, then line, then
    space boundaries; hard cut at *end* otherwise."""
    para = display.rfind("\n\n", start + 1, end)
    if para > start:
        return para
    line = display.rfind("\n", start + 1, end)
    if line > start:
        return line
    space = display.rfind(" ", start + 1, end)
    if space > start:
        return space
    return end


def _paginate(
    display: str, spans: list[Span], limit: int, max_entities: int,
) -> list[tuple[str, list[Span]]]:
    """Cut *display* into pages within the UTF-16 *limit* AND the entity
    budget; spans are clipped/rebased per page (a span crossing a cut becomes
    one span per page — PRE included). Page-leading newlines are stripped."""
    if _utf16_units(display) <= limit and len(spans) <= max_entities:
        return [(display, spans)]
    spans_sorted = sorted(spans, key=lambda x: (x[0], -x[1], x[2]))
    pre_intervals = [(s, e) for s, e, k in spans_sorted if k == "pre"]
    pages: list[tuple[str, list[Span]]] = []
    start = 0
    n = len(display)
    while start < n:
        end = _advance_by_utf16(display, start, limit)
        cut = n if end >= n else _preferred_cut(display, start, end)
        inter = [sp for sp in spans_sorted if sp[0] < cut and sp[1] > start]
        if len(inter) > max_entities:
            bound = inter[max_entities][0]
            if bound > start:
                cut = min(cut, bound)
                inter = [
                    sp for sp in spans_sorted if sp[0] < cut and sp[1] > start
                ]
            if len(inter) > max_entities:  # degenerate pileup: drop the excess
                inter = inter[:max_entities]
        page_spans = [
            (max(s, start) - start, min(e, cut) - start, k)
            for s, e, k in inter
        ]
        pages.append((
            display[start:cut],
            [p for p in page_spans if p[1] > p[0]],
        ))
        # Swallow AT MOST the one paragraph separator at the cut (bounded — an
        # unbounded skip silently drops content), and NEVER inside a PRE span
        # (blank code lines are meaningful).
        skipped = 0
        while (
            cut < n and display[cut] == "\n" and skipped < 2
            and not any(s <= cut < e for s, e in pre_intervals)
        ):
            cut += 1
            skipped += 1
        if cut <= start:  # progress guard (unreachable in practice)
            cut = start + 1
        start = cut
    return pages


def _without_lone_surrogates(text: str) -> str:
    """*text* with every surrogate code point replaced by ``U+FFFD``.

    Not cosmetic, and not the same thing as the measurement's tolerance. PTB
    22.7 sends a request as ``data=request_data.json_parameters`` — FORM data,
    which httpx encodes as UTF-8 — not as ``json_payload``, whose
    ``ensure_ascii`` would have escaped a lone surrogate harmlessly. So a page
    that still carries one raises ``NetworkError(UnicodeEncodeError)`` before
    any request leaves the process: the repaired page would deliver nothing at
    all. Replacement is ONE code point for one, which preserves every Python
    offset the reconstruction inserts at and every UTF-16 unit the budget was
    measured in.

    Applied only to the pages the conversion-failure arm emits. A page whose
    spans convert, and a page with no spans, never reach here and keep their
    bytes exactly — narrowing this asymmetry further is a separate defect with
    its own reachability, not this one.
    """
    if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return text
    return "".join(
        "\ufffd" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in text)


def _link_targets_for_page(
    page_text: str, page_spans: list[Span],
) -> tuple[list[tuple[int, int, str]], list[str], bool]:
    """``(insertable spans, unique urls, inline_ok)`` for one page's spans.

    The span-side sibling of ``_link_spans``: ``_paginate`` rebases every span
    to PYTHON offsets on its own page, so a reconstruction from spans needs no
    UTF-16 round-trip — and that round-trip is precisely what a lone surrogate
    breaks. Selection mirrors ``_link_spans`` so the two plain forms agree: a
    non-link kind carries no datum the display lacks, an empty url is what
    ``_spans_to_entities`` itself rejects, and a url already visible inside its
    own label would be printed twice.

    ``inline_ok`` is False when a link span's offsets do not address this page.
    Its url still reaches the caller, which then uses the whole-page overflow
    form rather than guessing an insertion point.
    """
    spans: list[tuple[int, int, str]] = []
    urls: list[str] = []
    inline_ok = True
    for start, end, kind in page_spans:
        if not kind.startswith(_LINK_KIND):
            continue
        url = kind[len(_LINK_KIND):]
        if not url:
            continue
        if not (0 <= start < end <= len(page_text)):
            inline_ok = False
        elif url in page_text[start:end]:
            continue                     # an autolink already carries it
        else:
            spans.append((start, end, url))
        if url not in urls:
            urls.append(url)
    spans.sort(key=lambda sp: sp[0])
    return spans, urls, inline_ok


def _target_pages(urls: list[str]) -> list[str]:
    """*urls* as pages of one destination per line, each within ``MAX_LEN``.

    A destination longer than one whole message cannot be delivered by this
    platform in ANY shape, and fragments of an address are worse than none, so
    it is dropped with a log line — the rule
    ``TelegramChannel._plain_fallback_chunks`` already applies. Packing the
    survivors greedily at newlines is safe exactly because each one fits alone.
    """
    pages: list[str] = []
    current = ""
    for url in urls:
        if _utf16_units(url) > MAX_LEN:
            logger.warning(
                "link destination exceeds one message and cannot be "
                "delivered plain: %d units", _utf16_units(url))
            continue
        candidate = f"{current}\n{url}" if current else url
        if current and _utf16_units(candidate) > MAX_LEN:
            pages.append(current)
            current = url
        else:
            current = candidate
    if current:
        pages.append(current)
    return pages


def _plain_pages_with_targets(
    page_text: str, page_spans: list[Span],
) -> list[tuple[str, None]]:
    """The pages that replace one page whose entities cannot be EXPRESSED.

    #834: a page whose conversion failed is emitted as ``(page_text, None)``,
    which every sender reads as "this page has no formatting" — so a
    ``link:<url>`` span's destination, the one datum a display cannot carry,
    reaches nobody and no ``BadRequest`` is raised for the sender-side plain
    fallback to key on. Here the renderer re-attaches the destinations itself,
    in the same ``label (url)`` form that fallback produces from entities.

    Inline while it fits the page budget; otherwise the page's own text
    unchanged, followed by destination-only pages. Insertions run right-to-left
    so an earlier index is never invalidated.
    """
    spans, urls, inline_ok = _link_targets_for_page(page_text, page_spans)
    page_text = _without_lone_surrogates(page_text)
    if not urls:
        return [(page_text, None)]
    if inline_ok:
        inline = page_text
        for _, end, url in reversed(spans):
            inline = f"{inline[:end]} ({url}){inline[end:]}"
        if _utf16_units(inline) <= MAX_LEN:
            return [(inline, None)]
    return [(page_text, None), *((page, None) for page in _target_pages(urls))]


def render_paged(
    text: str,
) -> list[tuple[str, "list[MessageEntity] | None"]]:
    """Delivery planner (v2): parse ONCE, then return ``[(display, entities)]``
    pages that each fit Telegram's 4096 UTF-16-unit and 100-entity budgets.

    Marker-free by construction — a page whose entities degrade (``None``:
    no spans on that page, or UTF-16 conversion failure) still carries its
    DISPLAY slice, never raw source. Callers send each page as one physical
    message; kwargs like ``reply_parameters`` belong on the FIRST page only.
    Never raises.

    #834: when a page's spans exist but cannot be CONVERTED — a lone surrogate
    makes PTB's UTF-16 adjustment raise — the plain page is emitted with each
    link destination re-attached, because ``(page_text, None)`` is what a page
    with no formatting looks like and no sender can tell the two apart. So a
    multi-page result no longer concatenates back to the display: a page that
    lost its entities and held a link is deliberately longer than its slice.
    A SINGLE-page result is untouched, entities and bytes both — its senders
    retry with the AUTHORED text, which still carries every address.
    """
    display, spans = parse_markdown(text)
    out: list[tuple[str, "list[MessageEntity] | None"]] = []
    paged = _paginate(display, spans, MAX_LEN, MAX_ENTITIES)
    for page_text, page_spans in paged:
        if not page_spans:
            out.append((page_text, None))
            continue
        entities = _spans_to_entities(page_text, page_spans)
        if entities is not None or len(paged) == 1:
            out.append((page_text, entities))
            continue
        try:
            out.extend(_plain_pages_with_targets(page_text, page_spans))
        except Exception:  # noqa: BLE001 — this contract never raises
            logger.warning(
                "could not reconstruct link destinations for a page whose "
                "entities failed to convert", exc_info=True)
            out.append((page_text, None))
    return out
