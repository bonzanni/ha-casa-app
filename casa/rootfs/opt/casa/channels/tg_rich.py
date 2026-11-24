"""Prepare agent-written Markdown for Telegram rich messages.

A Telegram rich message takes GitHub-flavoured Markdown mixed with a fixed set
of HTML tags and renders it natively: headings, tables, folds, quotes,
buttons. Almost everything the agent writes goes through as written. This
module changes only what Telegram would otherwise misread or drop:

* ``_`` outside code, link destinations and bare URLs is sent as the entity
  ``&#95;``. Bare underscores turn into italics or vanish, and a backslash
  escape stays visible inside HTML blocks, while the entity shows ``_`` in
  every context.
* ``<`` outside code that does not open a supported tag is sent as ``&lt;``;
  Telegram silently drops an unknown tag and, for some, its content.
* For both characters, a backslash that was already escaping the character
  (an odd run of backslashes before it) is dropped with it, so ``living\\_room``
  becomes ``living&#95;room``.
* ``[label](time:<ISO 8601>)`` becomes ``<tg-time unix="N">label</tg-time>``,
  with no display format; an unparseable target leaves the label as text.
* A button written in the text stays a button only when it is a URL or a
  copy-text button; any other is sent as its label. Callback data is Casa's
  own and only travels in the keyboard of its cards.
* A table whose rows have more cells than its header gets a wider header, so
  Telegram does not drop the extra cells; a table inside a quote or a list
  item too.
* A single line break inside a paragraph (a soft line break) is kept as a
  line break: a backslash goes before it, the hard line break of CommonMark,
  since Telegram otherwise joins the two lines into one. A line already
  ending in a hard break or a ``<br>``, a line holding only a list marker,
  and a summary's title written over several lines (Telegram shows the
  backslash there) are left alone. Line breaks are sent as ``"\\n"``.

Code is never touched. Where code is, and where tables are, is decided by
markdown-it-py reading the text as CommonMark with GitHub's tables: fenced
and indented code blocks and inline code spans, wherever they sit (quotes,
list items, table cells, headings, link labels). The text of an HTML block is
not Markdown, with two exceptions Telegram makes: it reads a code span inside
a ``<summary>``, and it reads a ``<details>`` as a container whose body is
Markdown, blank lines or not. Code there is code too; backticks anywhere else in
an HTML block (an ``<aside>``, an expandable quote) are text. A fold written
inside an indented code block is code. An HTML comment, like anything else
opened by ``<!`` or ``<?``, is no tag Telegram supports: its ``<`` goes out as
``&lt;``, so it is read as the Markdown text it then is, and its code is code.

Telegram silently truncates multi-byte text past about 35,000 UTF-8 bytes
instead of refusing it, so the size budget is measured on the prepared UTF-8
bytes before sending: :func:`split` cuts raw Markdown into parts that each fit
once prepared, never inside a link, tag or code span that fits on its own.
Before such a text is cut, each reference link (``[text][label]``) is written
inline, to the destination and title its definition gives, so a link does not
depend on a definition left in another part; the definitions stay where they
were, and a text that fits keeps its links as written.
:func:`plain_blocks` builds a fallback payload that parses nothing, and
:func:`flatten` turns a received rich message back into text.
:func:`credited_quote` wraps a text in one quote credited to an agent, the
form a post takes in another agent's chat; it reads back as the body's lines
and the name on the last.

:func:`send_rich`, :func:`edit_rich` and :func:`send_rich_draft` make the raw
Bot API calls (``sendRichMessage``, ``editMessageText`` with
``rich_message``, ``sendRichMessageDraft``) through any bot.
"""

from __future__ import annotations

import bisect
import html
import logging
import re
import warnings
from datetime import datetime
from typing import Any, Callable, NamedTuple

from markdown_it import MarkdownIt
from markdown_it.common.utils import normalizeReference
from telegram import Message, ReplyParameters
from telegram.error import BadRequest, RetryAfter

from text_util import replace_lone_surrogates
from timekeeping import resolve_tz

logger = logging.getLogger(__name__)

# The library's Bot has an editMessageText method of its own, so it warns on
# every raw call to that endpoint; the rich form of the edit is only reachable
# as a raw call.
warnings.filterwarnings(
    "ignore", message=r"Please use 'Bot\.editMessageText'", category=UserWarning)

RICH_BUDGET_BYTES: int = 32_000
RICH_MAX_BLOCKS: int = 500
SIZE_REFUSALS: frozenset[str] = frozenset({
    "RICH_MESSAGE_TEXT_TOO_LONG",
    "RICH_MESSAGE_TOO_LARGE",
    "RICH_MESSAGE_BLOCKS_TOO_MANY",
})

FORMAT_GUIDE: str = (
    "telegram_formatting: your messages in this chat are Telegram rich messages; write "
    "Markdown. Available: headings, lists and checklists, tables (right-align amounts), folds "
    "(<details><summary>), quotes (plain, expandable, pull), ==highlight==, ||spoiler||, "
    "footnotes, divider, footer, tel: and mailto: links, and a <tg-button-row> of URL or "
    "copy_text buttons. Give a long answer the shape that fits it, so it is quick to scan on "
    "a phone: records in a table, kept narrow (fewer, shorter columns; a wide one scrolls "
    "sideways), detail the reader may skip in a fold, done and left in a checklist. A short "
    "reply stays a short line. Forms: a fold is <details><summary>title</summary>, a blank "
    "line, the Markdown body, a blank line, </details>; a quote is > lines (Markdown works "
    "inside); an expandable quote <blockquote expandable>...</blockquote> and a pull quote "
    "<aside>...<cite>who</cite></aside> take plain text or <b>/<i>, not Markdown; a footer is "
    "<footer>...</footer>; a checklist is - [ ] / - [x]; a footnote is text[^1] with "
    "[^1]: note on its own line. Write identifiers in backticks. No media, maps or callback "
    "buttons.\n"
    "time_links: in the messages the person reads in this chat (answers, questions, progress "
    "notes, summaries) write every date or time as a Markdown link whose target is time: and "
    "the moment in ISO 8601 with its UTC offset, a T between date and time and no spaces, "
    "keeping your own words as the label, for example "
    "[Monday 16 November at 09:00](time:2026-11-16T09:00+01:00) or [16 November](time:2026-11-16). "
    "Telegram makes it tappable. Everywhere else (files, "
    "emails, tool fields, records) write dates plainly.")

# The tag names Telegram's rich HTML accepts. Any other "<name" is text.
_TAGS = frozenset("""a b strong i em u ins s strike del code mark sub sup tg-spoiler tg-emoji img
tg-time tg-math tg-math-block tg-reference h1 h2 h3 h4 h5 h6 p pre footer hr ul ol li input br
blockquote cite aside video audio tg-document figure figcaption tg-map tg-collage tg-slideshow
table caption tr th td details summary tg-button tg-button-row tg-thinking""".split())

_ENTITY = {"_": "&#95;", "<": "&lt;"}
_LABEL_ENTITY = {"&": "&amp;", "<": "&lt;", ">": "&gt;", "_": "&#95;"}

# A "<" opens a tag only when the whole construct is tag syntax: every
# attribute is name="value" / name='value', or one of the boolean attributes
# Telegram's rich HTML uses. Prose such as "<a lot of time>" or "<b or c>"
# would otherwise be taken for a tag and its words silently lost.
_BOOLEAN_ATTRS = (
    "open", "expandable", "bordered", "striped", "compact", "checked", "reversed",
    "tg-spoiler", "request-write-access", "allow-user-chats", "allow-bot-chats",
    "allow-group-chats", "allow-channel-chats")
_TAG_NAME = r"([A-Za-z][A-Za-z0-9-]*)"
_ATTR = (r"""\s+(?:[a-z][a-z-]*\s*=\s*(?:"[^"]*"|'[^']*')"""
         r"|(?:" + "|".join(_BOOLEAN_ATTRS) + r")(?![A-Za-z0-9-]))")
_TAG = re.compile(rf"</{_TAG_NAME}\s*>|<{_TAG_NAME}(?:{_ATTR})*\s*/?>")
# A CommonMark autolink: Telegram renders it as a link when sent as written.
_AUTOLINK = re.compile(r"<https?://[^\s<>]*>", re.I)
_TIME_LINK = re.compile(r"\[([^\[\]\n]*)\]\(\s*(time:[^()\s]*)\s*\)", re.I)
_URL = re.compile(r"https?://", re.I)
_PAREN_OR_ESCAPE = re.compile(r"\\.|[()\n]", re.S)
_BUTTON = re.compile(r"<tg-button(?![\w-])([^>]*)>(.*?)</tg-button\s*>", re.I | re.S)
_TYPE_ATTR = re.compile(r"""(?<![\w-])type\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.I)
# The button types model text may carry; every other type acts on Casa.
_TEXT_BUTTON_TYPES = frozenset({"url", "copy_text"})
_NEWLINE = re.compile(r"\n")
_SPACE_RUN = re.compile(r"[ \t]+")
# A line that already ends in a <br>: Telegram breaks the line there.
_BR_END = re.compile(r"<br\s*/?>[ \t]*\Z", re.I)
_BARE_MARKER = re.compile(r"[ \t>]*(?:[-+*]|\d{1,9}[.)])[ \t]*")
_SUMMARY = re.compile(r"<summary(?:\s[^>]*)?>(.*?)</summary\s*>", re.I | re.S)
_SUMMARY_OPEN = re.compile(r"<summary(?:\s[^>]*)?>", re.I)
_SUMMARY_CLOSE = re.compile(r"</summary\s*>", re.I)
# A fold's tag line: behind any quote markers, list markers and indentation,
# nothing but fold tags: <details> with or without attributes, a whole
# <summary>...</summary>, a <summary> or </summary> on its own, </details>.
_TAG_LINE = re.compile(r"^(?:[ \t]|>|[-+*](?=[ \t])|\d{1,9}[.)](?=[ \t]))*(<[^\n]*)$", re.M)
# The "<" of "<!" and "<?", which open no supported tag.
_NOT_A_TAG = re.compile(r"<(?=[!?])")
_FOLD_TAGS = re.compile(
    r"(?:(?:<details(?:\s[^>]*)?>|</details\s*>|<summary(?:\s[^>]*)?>[^\n]*?</summary\s*>"
    r"|<summary(?:\s[^>]*)?>|</summary\s*>)[ \t]*)+", re.I)


# ---------------------------------------------------------------------------
# Reading the Markdown
# ---------------------------------------------------------------------------

# The parser fills these in the env of each parse: the spans its inline rules
# consumed, keyed by the inline text they were found in (two inline tokens
# with the same text hold the same spans), the offset where each table row's
# text starts, and each reference definition.
_SPANS = "tg_rich_spans"
_ROWS = "tg_rich_rows"
_DEFS = "tg_rich_definitions"
# A reference definition up to its destination: "[label]:".
_DEFINITION = re.compile(r"\[(?:\\.|[^\\\]])*\]:")


def _noting_span(kind: str, rule: Callable) -> Callable:
    """*rule*, also noting the span of inline text it consumed as *kind*:
    ``"code"`` for a code span, ``"unit"`` for a link, image, autolink or tag,
    ``"break"`` for a soft line break.

    A backtick run with no closer is ordinary text, so it is noted only when
    it became a code span; a line break only when it is a soft one. A
    reference link or image (one that ends in ``]``) also gets its tail, from
    the ``]`` closing its label, noted as ``("ref", label)``."""
    token_type = {"code": "code_inline", "break": "softbreak"}.get(kind)

    def noted(state: Any, silent: bool) -> bool:
        start, count = state.pos, len(state.tokens)
        found = rule(state, silent)
        if found and not silent and (token_type is None or (
                len(state.tokens) > count and state.tokens[-1].type == token_type)):
            spans = state.env[_SPANS].setdefault(state.src, set())
            spans.add((kind, start, state.pos))
            if kind == "unit" and state.src[start] in "[!" and state.src[state.pos - 1] == "]":
                image = state.src[start] == "!"
                label_end = state.md.helpers.parseLinkLabel(state, start + image, not image)
                if label_end <= start:
                    return found
                tail = state.src[label_end + 1:state.pos]
                label = tail[1:-1] if len(tail) > 2 else state.src[start + 1 + image:label_end]
                spans.add((("ref", normalizeReference(label)), label_end, state.pos))
        return found
    return noted


def _noting_definitions(rule: Callable) -> Callable:
    """The reference definition rule, also noting where each definition's
    text starts and ends, the label it defines (``None`` for a repeat, which
    defines nothing) and, for one on a single line, where its destination
    starts."""
    def noted(state: Any, start_line: int, end_line: int, silent: bool) -> bool:
        count = len(state.env.get("references", ()))
        found = rule(state, start_line, end_line, silent)
        if found and not silent:
            references = state.env["references"]
            begin = state.bMarks[start_line] + state.tShift[start_line]
            end = state.eMarks[state.line - 1]
            definition = _DEFINITION.match(state.src, begin, end)
            state.env.setdefault(_DEFS, []).append((
                begin, end, next(reversed(references)) if len(references) > count else None,
                definition.end() if definition and state.line == start_line + 1 else None))
        return found
    return noted


def _noting_rows(rule: Callable) -> Callable:
    """The table rule, also noting where each row's text starts in the
    source, behind any quote or list prefix."""
    def noted(state: Any, start_line: int, end_line: int, silent: bool) -> bool:
        count = len(state.tokens)
        found = rule(state, start_line, end_line, silent)
        if found and not silent:
            first, last = state.tokens[count].map
            state.env[_ROWS].append(
                {line: state.bMarks[line] + state.tShift[line] for line in range(first, last)})
        return found
    return noted


def _parser() -> MarkdownIt:
    """CommonMark with GitHub's tables and strikethrough, as Telegram reads
    rich Markdown. Emphasis is left out: it never moves code, links or tags,
    and on text with many underscores its delimiter bookkeeping costs more
    than all the rest of the parse."""
    md = MarkdownIt("commonmark").enable(["table", "strikethrough"]).disable("emphasis")
    for name, kind in (("backticks", "code"), ("link", "unit"), ("image", "unit"),
                       ("autolink", "unit"), ("html_inline", "unit"), ("newline", "break")):
        rule = md.inline.ruler.__rules__[md.inline.ruler.__find__(name)]
        md.inline.ruler.at(name, _noting_span(kind, rule.fn), {"alt": list(rule.alt)})
    block_rules = (("table", _noting_rows), ("reference", _noting_definitions))
    for name, wrap in block_rules:
        rule = md.block.ruler.__rules__[md.block.ruler.__find__(name)]
        md.block.ruler.at(name, wrap(rule.fn), {"alt": list(rule.alt)})
    return md


_MD = _parser()


class _Fence(NamedTuple):
    opener: str      # the opener line, stripped
    closer: str      # a line that closes it: its run, behind the quote
                     # markers and indentation of its opener
    body: int        # the offset where its opener line ends
    end: int         # the last offset still inside it: the line break before
                     # its closing line, or the end of the block it sits in


class _Reading(NamedTuple):
    code: list[tuple[int, int]]       # sorted, disjoint [start, end) of code
    units: list[tuple[int, int]]      # spans a cut must not fall inside
    fences: list[_Fence]
    wide: list[tuple[int, int, int, int]]   # tables to widen: (header start,
                                            # header end, delimiter end, cells
                                            # the header lacks)
    breaks: list[int]                 # the "\n" of each soft line break,
                                      # which a backslash makes a hard one
    references: list[tuple[int, int, str]] = []   # each reference link or
                                      # image: [start, end) of its tail from
                                      # the "]" closing its label, the label
    definitions: list[tuple[int, int, str | None, int | None]] = []
                                      # each reference definition: [start,
                                      # end), the label it defines, where
                                      # its destination starts on one line


def _summary_titles(text: str, code: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Each summary written over several lines, from its ``<summary>`` to its
    ``</summary>``: Telegram shows a backslash there as written, so its line
    breaks get none. A tag inside code or after an escaping backslash is
    text, and is never taken for one end."""
    def tags(pattern: re.Pattern[str]) -> list[re.Match[str]]:
        return [m for m in pattern.finditer(text) if not _escaped(text, m.start())
                and not any(start <= m.start() < end for start, end in code)]
    closers = tags(_SUMMARY_CLOSE)
    titles: list[tuple[int, int]] = []
    for opener in tags(_SUMMARY_OPEN):
        if titles and opener.start() < titles[-1][1]:
            continue
        closer = next((c for c in closers if c.start() >= opener.end()), None)
        if closer is not None and "\n" in text[opener.end():closer.start()]:
            titles.append((opener.start(), closer.end()))
    return titles


def _escaped(s: str, i: int) -> bool:
    """The character at *i* follows an odd run of backslashes."""
    j = i
    while j > 0 and s[j - 1] == "\\":
        j -= 1
    return (i - j) % 2 == 1


def _row_cells(src: str, begin: int, end: int) -> list[list[int]]:
    """The cells of the table row ``src[begin:end]`` as GitHub-flavoured
    Markdown reads it: split on every pipe not right after a backslash, code
    span or not, with an escaped pipe losing its backslash, and an outer
    leading or trailing pipe making no cell. Each cell is the source offset
    of each character of its trimmed content."""
    row = src[begin:end]
    base = begin + len(row) - len(row.lstrip())
    cells: list[list[int]] = []
    current: list[int] = []
    escaped = False
    for i, ch in enumerate(row.strip()):
        if ch == "|" and not escaped:
            cells.append(current)
            current = []
        else:
            if ch == "|":
                current.pop()
            current.append(base + i)
        escaped = ch == "\\"
    cells.append(current)
    if cells and not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    out = []
    for cell in cells:
        while cell and src[cell[0]].isspace():
            cell = cell[1:]
        while cell and src[cell[-1]].isspace():
            cell = cell[:-1]
        out.append(cell)
    return out


def _read(text: str, *, units: bool = False) -> _Reading:
    """Where *text* holds code, as a CommonMark reader with GitHub's tables
    finds it: fenced and indented code blocks, and inline code spans wherever
    they sit (quotes, list items, table cells, headings, link labels). With
    *units*, also the spans a cut must not fall inside: links, autolinks,
    tags, time links and code spans."""
    # *text* has "\n" line breaks only (:func:`_normalized`), and this
    # spelling keeps every offset the parser reports an offset in *text*. A
    # final line break changes nothing in what the text parses to, and keeps
    # markdown-it-py 4.0.0 from reading past the end of a text whose last
    # line is an empty quote line after a table. A "<" before "!" or "?" (an
    # HTML comment, declaration or processing instruction) never opens a tag
    # Telegram supports, so it is sent as "&lt;" and what follows is Markdown
    # text; the parser reads it with a letter there, which opens nothing.
    plain = text.replace("\0", "\ufffd") + "\n"
    src, tag_lines = _mask_folds(_NOT_A_TAG.sub("x", plain))
    env: dict = {_SPANS: {}, _ROWS: []}
    try:
        tokens = _MD.parse(src, env)
    except Exception as exc:  # noqa: BLE001 — a parser fault must not cost the message
        logger.warning("Markdown not parsed, sent with no code found: %s", type(exc).__name__)
        return _Reading([], [], [], [], [])
    starts = [0, *(m.end() for m in _NEWLINE.finditer(src))]

    def line_start(line: int) -> int:
        return starts[line] if line < len(starts) else len(src)

    def line_end(line: int) -> int:
        return line_start(line + 1) - 1 if line + 1 < len(starts) else len(src)

    cells: dict[int, list[list[int]]] = {}
    wide: list[tuple[int, int, int, int]] = []
    for rows in env[_ROWS]:
        lines = sorted(rows)
        for line in lines:
            cells[line] = _row_cells(src, rows[line], line_end(line))
        head = lines[0]
        widest = max(len(cells[line]) for line in lines)
        if widest > len(cells[head]):
            wide.append((line_start(head), line_end(head), line_end(head + 1),
                         widest - len(cells[head])))

    code: list[tuple[int, int]] = []
    found_units: list[tuple[int, int]] = []
    fences: list[_Fence] = []
    breaks: list[int] = []
    references: list[tuple[int, int, str]] = []
    row_line: int | None = None    # the source line of the table row being read
    column = -1
    for token in tokens:
        if token.type in ("fence", "code_block") and token.map:
            first, last = token.map
            code.append((line_start(first), line_start(last)))
            if token.type == "fence":
                closer = text[line_start(last - 1):line_end(last - 1)].lstrip(" \t>").rstrip()
                closed = (last - 1 > first and len(closer) >= len(token.markup)
                          and set(closer) == {token.markup[0]})
                opener = text[line_start(first):line_end(first)]
                prefix = opener[:max(opener.find(token.markup), 0)]
                fences.append(_Fence(
                    opener.strip(), re.sub(r"[^>\s]", " ", prefix) + token.markup,
                    line_end(first),
                    line_start(last - 1) - 1 if closed else line_start(last) - 1))
            continue
        if token.type == "html_block" and token.map:
            tag_lines.append((line_start(token.map[0]), line_end(token.map[1] - 1)))
            continue
        if token.type == "tr_open":
            row_line, column = (token.map[0] if token.map else None), -1
            continue
        if token.type == "tr_close":
            row_line = None
            continue
        if token.type in ("th_open", "td_open"):
            column += 1
            continue
        if token.type != "inline" or not token.map:
            continue
        spans = env[_SPANS].get(token.content)
        if not spans:
            continue
        if row_line is not None:
            row = cells.get(row_line, [])
            if not (column < len(row)
                    and "".join(src[o] for o in row[column]) == token.content):
                continue    # the split above did not find the parser's cell
            at = row[column].__getitem__
        else:
            at = _inline_offsets(src, token.content, token.map[0], line_start, line_end)
        for kind, start, end in spans:
            if isinstance(kind, tuple):
                references.append((at(start), at(end - 1) + 1, kind[1]))
                continue
            if kind == "break":
                newline = at(start)
                line = text[text.rfind("\n", 0, newline) + 1:newline]
                # Not after a <br>, which already breaks the line, nor after
                # a bare list marker, which a backslash would turn into a
                # list item.
                br = _BR_END.search(line)
                if not ((br and not _escaped(line, br.start()))
                        or _BARE_MARKER.fullmatch(line)):
                    breaks.append(newline)
                continue
            if end <= start:
                continue
            span = (at(start), at(end - 1) + 1)
            if kind == "code":
                code.append(span)
            if units:
                found_units.append(span)

    for begin, end in tag_lines:
        spans = _summary_code(plain[begin:end], env)
        code += [(begin + a, begin + b) for a, b in spans]
        if units:
            found_units += [(begin + a, begin + b) for a, b in spans]
    if units:
        for pattern in (_TIME_LINK, _AUTOLINK, _TAG):
            found_units += [m.span() for m in pattern.finditer(text)]
    code.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ((s, min(e, len(text))) for s, e in code):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    titles = _summary_titles(text, merged)
    breaks = [b for b in breaks if not any(start <= b < end for start, end in titles)]
    return _Reading(merged, found_units, fences, wide, sorted(breaks), references,
                    env.get(_DEFS, []))


def _mask_folds(src: str) -> tuple[str, list[tuple[int, int]]]:
    """*src* with each fold's tag line blanked, and where those tag lines are.

    Telegram reads ``<details>`` as a container whose body is Markdown, blank
    lines or not, while CommonMark would take the tag lines and the lines
    around them for one HTML block. With the tag lines turned into spaces,
    behind their quote markers, list markers and indentation, the body
    parses as the Markdown it is. A tag line in an indented code block, a
    fold shown as code, is left alone: one the text as written puts in an
    indented code block, or the text with every tag line's tags turned into
    letters of the same length. The second reading is needed after a fold
    whose body holds a blank line: as written, the fold's tags open an HTML
    block that ends at that blank line and can put a fence in the wrong
    place, which then swallows the example. One in a fence is blanked all
    the same: the fence keeps it as code either way. Neither reading is
    needed when no tag line is indented by four spaces or a tab, which an
    indented code block takes. The offsets stay those of *src*."""
    if "details" not in src.lower():
        return src, []
    tags: list[tuple[int, int, int]] = []     # line number, [start, end) of the tags
    line_number, counted, deep = 0, 0, False
    for line in _TAG_LINE.finditer(src):
        line_number += src.count("\n", counted, line.start())
        counted = line.start()
        if _FOLD_TAGS.fullmatch(line.group(1)):
            tags.append((line_number, *line.span(1)))
            indent = src[line.start():line.start(1)]
            deep = deep or indent.endswith("    ") or "\t" in indent
    if not tags:
        return src, []
    code_lines = []
    if deep:
        try:
            tokens = [token for text in (src, _spliced(src, [(s, e, "x" * (e - s))
                                                             for _, s, e in tags]))
                      for token in _MD.parse(text, {_SPANS: {}, _ROWS: []})]
        except Exception:  # noqa: BLE001 — the parse proper reports a parser fault
            return src, []
        code_lines = [t.map for t in tokens if t.type == "code_block" and t.map]
    found = [(start, end) for number, start, end in tags
             if not any(first <= number < last for first, last in code_lines)]
    return _spliced(src, [(s, e, " " * (e - s)) for s, e in found]), found


def _spliced(text: str, edits: list[tuple[int, int, str]]) -> str:
    """*text* with each ``(start, end, new)`` of *edits*, which do not
    overlap, putting *new* in place of ``text[start:end]``."""
    out, pos = [], 0
    for start, end, new in sorted(edits):
        out += [text[pos:start], new]
        pos = end
    return "".join(out) + text[pos:]


def _summary_code(html: str, env: dict) -> list[tuple[int, int]]:
    """The code spans inside each ``<summary>`` of *html*, which Telegram
    reads as code; the rest of an HTML block's text is text."""
    spans: list[tuple[int, int]] = []
    for summary in _SUMMARY.finditer(html):
        inner = summary.group(1)
        if "`" not in inner:
            continue
        _MD.parseInline(inner, env)
        spans += [(summary.start(1) + a, summary.start(1) + b)
                  for kind, a, b in env[_SPANS].get(inner, ()) if kind == "code" and b > a]
    return spans


def _inline_offsets(src: str, content: str, first_line: int, line_start: Callable,
                    line_end: Callable) -> Callable[[int], int]:
    """A map from an offset in a paragraph's or heading's inline text to its
    offset in *src*.

    Line *j* of the inline text is the tail of source line *first_line* +
    *j*, after its quote markers and indentation; the text as a whole is
    trimmed, and a heading also loses its markers and closing sequence."""
    lines = content.split("\n")
    begins = [0]
    offsets = []
    for j, line in enumerate(lines):
        begin, end = line_start(first_line + j), line_end(first_line + j)
        if j == len(lines) - 1:
            end = begin + len(src[begin:end].rstrip())
        offset = end - len(line)
        if src[offset:end] != line:
            found = src.rfind(line, begin, line_end(first_line + j))
            # A tab partly taken as indentation reads as spaces: the line's
            # tail still lines up from the right.
            offset = found if found >= 0 else offset
        offsets.append(offset)
        begins.append(begins[-1] + len(line) + 1)

    def at(p: int) -> int:
        j = bisect.bisect_right(begins, p) - 1
        return offsets[j] + p - begins[j]
    return at


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _pad_row(line: str, padding: str) -> str:
    body = line.rstrip("\r\n")
    eol = line[len(body):]
    core = body.rstrip()
    if not (core.endswith("|") and not core.endswith("\\|")):
        core += " |"
    return core + padding + eol


def _widen_tables(text: str, wide: list[tuple[int, int, int, int]]) -> str:
    """Widen each table's header and delimiter rows to its widest row, so
    Telegram does not drop the extra cells of a body row. *wide* holds, for
    each table that needs it, where its header row starts, where its
    delimiter row ends and how many cells the header lacks; any quote or list
    prefix stays as written."""
    for start, mid, end, extra in sorted(wide, reverse=True):
        text = (text[:start] + _pad_row(text[start:mid], " |" * extra) + text[mid]
                + _pad_row(text[mid + 1:end], " --- |" * extra) + text[end:])
    return text


# ---------------------------------------------------------------------------
# Character encoding outside code
# ---------------------------------------------------------------------------


def _destination_ends(s: str) -> dict[int, int]:
    """For each ``(`` of *s* closed on its line, the end (exclusive) of the
    ``)`` that closes it; a link destination opened by ``](`` runs to there.
    A character after a backslash is skipped. One pass for the whole text,
    so a line of many unclosed ``](`` costs no more than any other."""
    ends: dict[int, int] = {}
    opened: list[int] = []
    for m in _PAREN_OR_ESCAPE.finditer(s):
        c = m.group()
        if c == "\n":
            opened.clear()
        elif c == "(":
            opened.append(m.start())
        elif c == ")" and opened:
            ends[opened.pop()] = m.end()
    return ends


def _url_end(s: str, i: int) -> int:
    """End (exclusive) of a bare URL starting at *i*: the next whitespace,
    ``<``, ``>``, ``|`` (a table cell border), or a ``)`` with no ``(`` of
    its own inside the URL."""
    depth = 0
    k = i
    while k < len(s):
        c = s[k]
        if c.isspace() or c in "<>|":
            break
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                break
            depth -= 1
        k += 1
    return k


def _escape_with(s: str, table: dict[str, str]) -> str:
    """Replace each character in *table* with its entity, dropping a
    backslash that was escaping it; nothing else changes."""
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\":
            j = i
            while j < n and s[j] == "\\":
                j += 1
            if j < n and (j - i) % 2 == 1 and s[j] in table:
                out.append("\\" * (j - i - 1) + table[s[j]])
                i = j + 1
            else:
                out.append(s[i:j])
                i = j
            continue
        out.append(table.get(ch, ch))
        i += 1
    return "".join(out)


def time_instant(target: str) -> "int | None":
    """The unix second a ``time:`` link target names, or ``None``.

    The target is ``time:`` and an ISO 8601 date or date-time. One with no
    UTC offset, or a date alone (its local midnight), is read in Casa's
    timezone. Anything unparseable, or before 1970, gives ``None``."""
    try:
        moment = datetime.fromisoformat(target[len("time:"):].strip())
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=resolve_tz())
        seconds = int(moment.timestamp())
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    return seconds if seconds >= 0 else None


def _time_tag(label: str, target: str) -> str:
    unix = time_instant(target)
    if unix is None:
        return _encode(label)
    return f'<tg-time unix="{unix}">{_escape_with(label, _LABEL_ENTITY)}</tg-time>'


def _tag_name(tag: re.Match[str]) -> str:
    return (tag.group(1) or tag.group(2)).lower()


def _is_text_button(attributes: str) -> bool:
    """A button that model text may carry: every ``type`` it names, read with
    its entities decoded as Telegram reads them, is a URL or copy-text type,
    and it names one."""
    kinds = [next(v for v in m.groups() if v is not None).strip().lower()
             for m in _TYPE_ATTR.finditer(html.unescape(attributes))]
    return bool(kinds) and all(kind in _TEXT_BUTTON_TYPES for kind in kinds)


def _is_refused_button_tag(tag: re.Match[str]) -> bool:
    return (_tag_name(tag) == "tg-button" and tag.group(2) is not None
            and not _is_text_button(tag.group(0)))


def _encode(segment: str) -> str:
    """Encode ``_`` and stray ``<`` in a stretch of text that holds no code.

    Link destinations, bare URLs and the text of supported tags (their
    attributes included) are copied as written."""
    out: list[str] = []
    ends = _destination_ends(segment) if "](" in segment else {}
    i = 0
    n = len(segment)
    while i < n:
        ch = segment[i]
        if ch == "\\":
            j = i
            while j < n and segment[j] == "\\":
                j += 1
            if j < n and (j - i) % 2 == 1:
                # The character after an odd run is escaped: never markup.
                escaped = segment[j]
                if escaped in _ENTITY:
                    out.append("\\" * (j - i - 1) + _ENTITY[escaped])
                else:
                    out.append(segment[i:j + 1])
                i = j + 1
            else:
                out.append(segment[i:j])
                i = j
            continue
        if ch == "[":
            m = _TIME_LINK.match(segment, i)
            if m:
                out.append(_time_tag(m.group(1), m.group(2)))
                i = m.end()
                continue
        elif ch == "]" and segment.startswith("](", i):
            end = ends.get(i + 1)
            if end is not None:
                out.append(segment[i:end])
                i = end
                continue
        elif ch == "<":
            m = _AUTOLINK.match(segment, i) or _TAG.match(segment, i)
            if m and (m.re is _AUTOLINK
                      or (_tag_name(m) in _TAGS and not _is_refused_button_tag(m))):
                out.append(m.group(0))
                i = m.end()
            else:
                out.append(_ENTITY["<"])
                i += 1
            continue
        elif ch in "hH" and _URL.match(segment, i):
            end = _url_end(segment, i)
            out.append(segment[i:end])
            i = end
            continue
        out.append(_ENTITY.get(ch, ch))
        i += 1
    return "".join(out)


def _normalized(markdown: str) -> str:
    """*markdown* with its lone surrogates replaced and every line break
    spelled ``"\\n"``, as Telegram reads ``"\\r\\n"`` and ``"\\r"`` too."""
    return replace_lone_surrogates(markdown).replace("\r\n", "\n").replace("\r", "\n")


def _encode_text(piece: str) -> str:
    """A stretch of text outside code, its buttons and characters encoded."""
    return _encode(_BUTTON.sub(
        lambda m: m.group(0) if _is_text_button(m.group(1)) else m.group(2), piece))


def prepare(markdown: str) -> str:
    """The agent's Markdown, ready to send as a rich message."""
    text = _normalized(markdown)
    reading = _read(text)
    if reading.wide:
        text = _widen_tables(text, reading.wide)
        reading = _read(text)
    out: list[str] = []
    pos = 0
    breaks = iter(reading.breaks)
    after = next(breaks, None)

    def text_until(end: int) -> str:
        # The stretch of text up to *end*, a backslash before each soft line
        # break in it, so Telegram keeps the line break.
        nonlocal after
        while after is not None and after < pos:     # never inside code
            after = next(breaks, None)
        pieces, at = [], pos
        while after is not None and after < end:
            pieces += [text[at:after], "\\"]
            at, after = after, next(breaks, None)
        return _encode_text("".join(pieces) + text[at:end])

    for start, end in reading.code:
        out += [text_until(start), text[start:end]]
        pos = end
    out.append(text_until(len(text)))
    return "".join(out)


def credited_quote(markdown: str, name: str) -> str:
    """*markdown* as one quote credited to *name*, the way Casa shows a post
    one agent makes in another agent's chat.

    The quote opens with ``> <cite>name</cite>`` and an empty quote line, and
    every line of the text follows quoted (``> ``; an empty line ``>``).
    Telegram reads that as one quote with *name* as its credit, shown under
    it, and the body inside still read as Markdown: tables, lists, folds,
    code and nested quotes are kept. The FIRST ``<cite>`` in a quote takes
    the credit, which is why Casa's comes before anything the text wrote: a
    ``<cite>`` in the body, wherever it sits and however Telegram reads the
    lines around it, is a paragraph of the post and never the credit. The
    name is escaped for the tag's content. The body is quoted character for
    character; a code block indented with a TAB can read as text behind the
    quote marker, where its tab stop falls elsewhere, and is then shown with
    the same characters but not as code. Fenced code is unaffected. With no
    *name*, *markdown* is returned as written."""
    if not name:
        return markdown
    credit = name.replace("&", "&amp;").replace("<", "&lt;")
    quoted = "\n".join(f"> {line}" if line else ">"
                       for line in _normalized(markdown).split("\n"))
    return f"> <cite>{credit}</cite>\n>\n{quoted}"


# ---------------------------------------------------------------------------
# Size
# ---------------------------------------------------------------------------


def _fits(markdown: str, budget: int) -> bool:
    return len(prepare(markdown).encode("utf-8")) <= budget


def _inline_references(text: str) -> str:
    """*text* with each reference link and image written as an inline one,
    to the destination and title its definition gives, as written there: cut
    into parts, a link would otherwise lose a definition left in another
    part. The definitions themselves stay, since a reference this reading
    does not see (one inside an image's label) may still need one. The links
    to a definition over several lines, or with a ``|`` (a table cell
    border), stay as written."""
    if "]:" not in text:
        return text
    reading = _read(text)
    targets = {label: text[target:end].strip() for _, end, label, target in reading.definitions
               if label is not None and target is not None and "|" not in text[target:end]}
    edits = [(start, end, f"]({targets[label]})")
             for start, end, label in reading.references if label in targets]
    return _spliced(text, edits)


def _cut_at(rest: str, k: int, sep: int, fences: list[_Fence]) -> tuple[str, str]:
    """Cut *rest* at offset *k*, dropping *sep* separator characters.

    A cut inside one of *rest*'s fenced blocks closes it in the first part
    and reopens it, with its language, in the second. There only the line
    break at the cut is replaced, so a blank line in the code stays in the
    code."""
    fence = next((f for f in fences if f.body <= k <= f.end), None)
    if fence is None:
        return rest[:k], rest[k + sep:]
    return f"{rest[:k]}\n{fence.closer}", f"{fence.opener}\n{rest[k + min(sep, 1):]}"


def _cut(rest: str, budget: int) -> tuple[str, str]:
    """The longest leading part of *rest* that fits *budget* once prepared,
    cut at a paragraph boundary, else a line boundary, else after a space,
    else anywhere. None of these cuts falls inside a link, an autolink, a
    tag, a time link or a code span; only a single one of those bigger than
    the budget is cut through."""
    reading = _read(rest, units=True)
    inside = bytearray(len(rest) + 1)
    for start, end in reading.units:
        if end - start > 1:
            inside[start + 1:end] = b"\x01" * (end - start - 1)
    first = len(rest) - len(rest.lstrip())

    def progress(k: int, pair: tuple[str, str]) -> bool:
        # A part must hold something, and the remainder must shrink: a cut
        # right after a fence opener would only reopen the same fence.
        return k > first and len(pair[1]) < len(rest)

    def largest_fitting(cuts: list[int], sep: int) -> tuple[str, str] | None:
        # Binary search for the last cut whose part fits. Prepared size
        # grows with the part almost everywhere; every part returned was
        # itself measured, so the result always fits.
        best = None
        lo, hi = 0, len(cuts) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            pair = _cut_at(rest, cuts[mid], sep, reading.fences)
            if not progress(cuts[mid], pair):
                lo = mid + 1  # only the earliest cuts make no progress
            elif _fits(pair[0], budget):
                best, lo = pair, mid + 1
            else:
                hi = mid - 1
        return best

    for sep in ("\n\n", "\n"):
        found = largest_fitting(
            [m.start() for m in re.finditer(re.escape(sep), rest) if not inside[m.start()]],
            len(sep))
        if found:
            return found
    # After a run of spaces, which stays with the first part: the next part
    # starts at a word, never at indentation that would read as code.
    found = largest_fitting(
        [m.end() for m in _SPACE_RUN.finditer(rest) if not inside[m.end()]], 0)
    if found:
        return found
    anywhere = list(range(1, len(rest)))
    outside = [k for k in anywhere if not inside[k]]
    found = largest_fitting(outside, 0)
    if found:
        return found
    if len(outside) < len(anywhere):
        found = largest_fitting(anywhere, 0)
        if found:
            return found
    # A budget too small for even one character: cut as little as possible
    # so the loop still ends.
    for k in anywhere:
        pair = _cut_at(rest, k, 0, reading.fences)
        if progress(k, pair):
            return pair
    return rest, ""


def split(markdown: str, budget: int = RICH_BUDGET_BYTES) -> list[str]:
    """Raw Markdown parts, each at most *budget* bytes once prepared.

    Cuts fall at paragraph boundaries where possible, then at line
    boundaries, then after a space, then anywhere, but never inside a link,
    tag or code span that fits the budget. A paragraph cut outside code
    drops its blank line, so parts cut that way join back with
    ``"\\n\\n"``; a cut after a space or anywhere drops nothing; a cut inside a
    fenced block closes and reopens the fence and keeps every code line."""
    rest = _normalized(markdown)
    parts: list[str] = []
    if not _fits(rest, budget):
        rest = _inline_references(rest)
    while not _fits(rest, budget):
        part, rest = _cut(rest, budget)
        if part.strip():
            parts.append(part)
    if rest.strip() or not parts:
        parts.append(rest)
    return parts


def plain_blocks(text: str, budget: int = RICH_BUDGET_BYTES) -> list[dict]:
    """*text* as explicit paragraph blocks, with nothing parsed.

    The fallback when Telegram refuses the Markdown: paragraphs are split on
    blank lines, neighbours are joined until there are at most
    ``RICH_MAX_BLOCKS``, and the total is clipped to *budget* bytes (with a
    closing ``…`` only when something was cut)."""
    text = replace_lone_surrogates(text)
    if not text.strip():
        return []
    pieces: list[str] = []
    for piece in text.split("\n\n"):
        if pieces and (not piece.strip() or not pieces[-1].strip()):
            pieces[-1] += "\n\n" + piece
        else:
            pieces.append(piece)
    if len(pieces) > RICH_MAX_BLOCKS:
        size = -(-len(pieces) // RICH_MAX_BLOCKS)
        pieces = ["\n\n".join(pieces[i:i + size]) for i in range(0, len(pieces), size)]
    if len("\n\n".join(pieces).encode("utf-8")) > budget:
        pieces = _clip(pieces, budget)
    return [{"type": "paragraph", "text": piece} for piece in pieces]


def _clip(pieces: list[str], budget: int) -> list[str]:
    ellipsis = "…"
    limit = budget - len(ellipsis.encode("utf-8"))
    kept: list[str] = []
    used = 0
    for piece in pieces:
        sep = 2 if kept else 0
        size = len(piece.encode("utf-8"))
        if used + sep + size <= limit:
            kept.append(piece)
            used += sep + size
            continue
        room = max(limit - used - sep, 0)
        head = piece.encode("utf-8")[:room].decode("utf-8", "ignore")
        if head:
            kept.append(head)
        break
    if not kept:
        return [ellipsis]
    kept[-1] += ellipsis
    return kept


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------


def _flat(node: object) -> str:
    """The text of a rich-message node: blocks one per line, rich text runs
    concatenated, table cells joined with " | ", a mathematical expression as
    its LaTeX. It reads a fixed list of the schema's text-bearing fields, so
    URLs, times and other scalar fields are left out, and a field added to
    the schema later is read only once it joins the list."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_flat(item) for item in node)
    if not isinstance(node, dict):
        return ""
    pieces: list[str] = []
    for key in ("summary", "text", "expression", "cells", "items", "blocks", "buttons",
                "button", "caption", "credit"):
        value = node.get(key)
        if value is None:
            continue
        if key == "cells" and isinstance(value, list):
            piece = "\n".join(
                " | ".join(_flat(cell) for cell in row) if isinstance(row, list) else _flat(row)
                for row in value)
        elif key in ("items", "blocks") and isinstance(value, list):
            piece = "\n".join(_flat(item) for item in value)
        elif key == "buttons" and isinstance(value, list):
            piece = " | ".join(_flat(b.get("text")) for b in value if isinstance(b, dict))
        elif key == "button":
            piece = _flat(value.get("text")) if isinstance(value, dict) else ""
        else:
            piece = _flat(value)
        if piece:
            pieces.append(piece)
    return "\n".join(pieces)


def flatten(rich_message: dict) -> str:
    """The readable text of a received rich message."""
    return replace_lone_surrogates(_flat(rich_message))


def is_size_refusal(exc: BaseException) -> bool:
    """True when Telegram refused a rich message for its size or block count.

    python-telegram-bot rewrites the error name (``Bad Request:
    RICH_MESSAGE_BLOCKS_TOO_MANY`` arrives as ``Rich_message_blocks_too_many``),
    so the names are compared case-insensitively."""
    message = str(exc).lower()
    return any(name.lower() in message for name in SIZE_REFUSALS)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

_MAX_HALVINGS = 4
_PARAGRAPH_BREAK = re.compile(r"\n\n")


def _api_value(value: Any) -> Any:
    """A library object (keyboard, reply parameters, ...) as its JSON dict."""
    to_dict = getattr(value, "to_dict", None)
    return to_dict() if callable(to_dict) else value


def _api_kwargs(rich_message: dict, **params: Any) -> dict:
    out = {k: _api_value(v) for k, v in params.items() if v is not None}
    out["rich_message"] = rich_message
    return out


def _is_not_modified(exc: BaseException) -> bool:
    return "not modified" in str(exc).lower()


def is_message_refusal(exc: BaseException) -> bool:
    """Telegram refused the edit for the message itself, not its text: the
    message is gone, or was sent with a reply keyboard and is never editable.
    Resending the text, unformatted or otherwise, would be refused the same
    way."""
    message = str(exc).lower()
    return "can't be edited" in message or "message to edit not found" in message


async def _post_message(bot: Any, chat_id: Any, rich_message: dict, *,
                        reply_parameters: Any, reply_markup: Any, kw: dict) -> Any:
    """One ``sendRichMessage``. When Telegram refuses a message whose reply
    carries a quote, for any reason but its size, the same message goes once
    more without the quote, as a plain reply (a quote that is not exact text
    of the message replied to is one such refusal). A refusal of that resend
    propagates."""
    async def post(reply: Any) -> Any:
        return await bot.do_api_request(
            "sendRichMessage",
            api_kwargs=_api_kwargs(rich_message, chat_id=chat_id,
                                   reply_parameters=reply,
                                   reply_markup=reply_markup, **kw),
            return_type=Message)

    try:
        return await post(reply_parameters)
    except BadRequest as exc:
        if (reply_parameters is None or not getattr(reply_parameters, "quote", None)
                or is_size_refusal(exc)):
            raise
        logger.warning("reply quote refused; sent without it: %s", type(exc).__name__)
        return await post(ReplyParameters(message_id=reply_parameters.message_id,
                                          allow_sending_without_reply=True))


def _halves(part: str) -> tuple[str, str] | None:
    """*part* cut into two halves by paragraph count, or ``None`` for a
    single paragraph. A cut inside a fenced block closes and reopens it, and
    reference links are written inline first, as :func:`split` does."""
    if not _PARAGRAPH_BREAK.search(part.strip()):
        return None
    part = _inline_references(part)
    breaks = [m.start() for m in _PARAGRAPH_BREAK.finditer(part.strip())]
    if not breaks:
        return None
    lead = len(part) - len(part.lstrip())
    return _cut_at(part, lead + breaks[len(breaks) // 2], 2, _read(part).fences)


async def _send_part(bot: Any, chat_id: Any, part: str, *, reply_parameters: Any,
                     reply_markup: Any, kw: dict, record: Callable[[Any], None] | None,
                     depth: int = 0) -> list:
    """Send one part; returns every message it became, each handed to
    *record* the moment it lands."""
    def landed(message: Any) -> list:
        if record is not None:
            record(message)
        return [message]

    try:
        return landed(await _post_message(bot, chat_id, {"markdown": prepare(part)},
                                          reply_parameters=reply_parameters,
                                          reply_markup=reply_markup, kw=kw))
    except BadRequest as exc:
        # The class only: the platform's text can quote the message it refused.
        halves = (_halves(part) if is_size_refusal(exc) and depth < _MAX_HALVINGS
                  else None)
        if halves is None:
            logger.warning("rich message sent as plain text: %s", type(exc).__name__)
            return landed(await _post_message(bot, chat_id, {"blocks": plain_blocks(part)},
                                              reply_parameters=reply_parameters,
                                              reply_markup=reply_markup, kw=kw))
        logger.warning("rich message split in two: %s", type(exc).__name__)
        first, second = halves
        return [
            *await _send_part(bot, chat_id, first, reply_parameters=reply_parameters,
                              reply_markup=None, kw=kw, record=record, depth=depth + 1),
            *await _send_part(bot, chat_id, second, reply_parameters=None,
                              reply_markup=reply_markup, kw=kw, record=record,
                              depth=depth + 1),
        ]


async def send_rich(bot: Any, chat_id: Any, markdown: str, *, reply_markup: Any = None,
                    reply_parameters: Any = None,
                    record: Callable[[Any], None] | None = None, **kw: Any) -> list:
    """Send *markdown* as rich messages, one per :func:`split` part, and
    return the messages sent.

    *reply_parameters* goes on the first message and *reply_markup* on the
    last; every other keyword (``disable_notification``,
    ``message_thread_id``, ...) on each. *record*, when given, receives each
    message the moment it lands, before the next is sent, so a later failure
    leaves every landed message recorded.

    A part refused for its size or block count is cut in two by paragraph
    count and each half sent, at most four levels deep; a part that cannot be
    cut further, or is refused for anything else, is sent as
    :func:`plain_blocks`. A refusal of the plain blocks, and any other
    failure, propagates."""
    parts = [part for part in split(markdown) if part.strip()]
    sent: list = []
    for i, part in enumerate(parts):
        sent += await _send_part(
            bot, chat_id, part,
            reply_parameters=reply_parameters if i == 0 else None,
            reply_markup=reply_markup if i == len(parts) - 1 else None, kw=kw, record=record)
    return sent


async def edit_rich(bot: Any, markdown: str, *, chat_id: Any = None, message_id: Any = None,
                    inline_message_id: str | None = None, reply_markup: Any = None) -> Any:
    """Edit one message into *markdown* as a rich message.

    An edit stays one message: text past the budget is cut, ending in
    ``…``, and any refusal of the text resends it as :func:`plain_blocks`.
    "Message is not modified" is success and returns ``True``. A refusal of
    the message itself (gone, or never editable) propagates at once, as does
    a refusal of the plain blocks and any other failure. Returns what
    Telegram returns: the edited message, or ``True`` for an inline
    message."""
    parts = split(markdown, RICH_BUDGET_BYTES - 3)
    text = parts[0] + ("…" if len(parts) > 1 else "")

    async def edit(rich_message: dict) -> Any:
        try:
            return await bot.do_api_request(
                "editMessageText",
                api_kwargs=_api_kwargs(rich_message, chat_id=chat_id, message_id=message_id,
                                       inline_message_id=inline_message_id,
                                       reply_markup=reply_markup),
                return_type=Message)
        except BadRequest as exc:
            if _is_not_modified(exc):
                return True
            raise

    try:
        return await edit({"markdown": prepare(text)})
    except BadRequest as exc:
        if is_message_refusal(exc):
            raise
        logger.warning("rich edit sent as plain text: %s", type(exc).__name__)
        return await edit({"blocks": plain_blocks(markdown)})


def retry_after_seconds(exc: RetryAfter) -> float:
    """The seconds a flood wait asks for, whether the library states them as a
    number or as a ``timedelta``."""
    wait = exc.retry_after
    return wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)


async def send_rich_draft(bot: Any, chat_id: Any, draft_id: int, markdown: str, *,
                          can_stop: bool, keep_on_stop: bool) -> float | None:
    """Show *markdown* as the rich draft *draft_id* in *chat_id*.

    Returns ``None``, or the seconds a flood wait asks for; any other failure
    propagates."""
    try:
        await bot.do_api_request(
            "sendRichMessageDraft",
            api_kwargs=_api_kwargs({"markdown": prepare(markdown)}, chat_id=chat_id,
                                   draft_id=draft_id, can_stop=can_stop,
                                   keep_on_stop=keep_on_stop))
    except RetryAfter as exc:
        return retry_after_seconds(exc)
    return None
