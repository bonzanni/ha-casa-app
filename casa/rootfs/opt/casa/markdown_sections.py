from __future__ import annotations

import re

from markdown_it import MarkdownIt

from canonical_bytes import canonical_text

_ALLOWED = {
    "heading_open", "heading_close", "inline", "paragraph_open",
    "paragraph_close", "bullet_list_open", "bullet_list_close",
    "list_item_open", "list_item_close", "text", "em_open", "em_close",
    "strong_open", "strong_close", "code_inline", "softbreak", "hardbreak",
}
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_RAW_HTML = re.compile(
    r"(?is)<!--|<![A-Z]|<\?|</?[A-Za-z][^>]*>|<[A-Za-z][^\n>]*$"
)
_INLINE_CODE = re.compile(r"(`+)(.*?)(?:\1)", re.DOTALL)


class MarkdownSectionError(ValueError):
    pass


def _iter_tokens(tokens):
    # `MarkdownIt.parse` returns a flat top-level list, but inline tokens
    # (e.g. links, images, emphasis) carry their real structure in
    # `token.children` rather than as sibling top-level tokens — an
    # image or a raw link would otherwise slip past a top-level-only scan.
    # Walk the full tree so nothing unsupported hides inside an "inline".
    for token in tokens:
        yield token
        if token.children:
            yield from _iter_tokens(token.children)


def validate_markdown(source: str) -> str:
    canonical = canonical_text(source)
    source_without_inline_code = _INLINE_CODE.sub("", canonical)
    if _RAW_HTML.search(source_without_inline_code):
        raise MarkdownSectionError("raw HTML is forbidden")
    parser = MarkdownIt("commonmark", {"html": True})
    for token in _iter_tokens(parser.parse(canonical)):
        if token.type in {"html_inline", "html_block"}:
            raise MarkdownSectionError("raw HTML is forbidden")
        if token.type not in _ALLOWED:
            raise MarkdownSectionError(f"unsupported Markdown token: {token.type}")
    return canonical


def _section_spans(canonical: str) -> list[tuple[int, str, int, int, int]]:
    """(level, name, heading_start, body_start, end) per heading; a section
    runs to the next heading of the same or a shallower level."""
    matches = list(_HEADING.finditer(canonical))
    spans = []
    for index, match in enumerate(matches):
        level = len(match.group(1))
        end = len(canonical)
        for later in matches[index + 1:]:
            if len(later.group(1)) <= level:
                end = later.start()
                break
        spans.append(
            (level, match.group(2).strip(), match.start(), match.end(), end))
    return spans


def sections(source: str) -> list[tuple[int, str, str]]:
    canonical = validate_markdown(source)
    return [
        (level, name, canonical[body_start:end].strip("\n") + "\n")
        for level, name, _, body_start, end in _section_spans(canonical)
    ]


def root_sections(source: str) -> list[tuple[int, str, str]]:
    """(level, name, body) for each section NO OTHER section contains (#611).

    ``sections`` is FLAT across heading levels while a parent's body physically
    runs through its children — ``_section_spans`` ends a span at the next
    heading of the same-or-shallower level — so iterating it re-emits every
    nested subsection once per ancestor: twice at depth two, three times at
    depth three.

    Containment is decided by the level sequence ALONE, so a span is a root
    exactly when its level is <= every level before it: a running
    prefix-minimum. Checked against offset-based containment computed from the
    real ``_section_spans`` over every level sequence of length 1..6 on levels
    1..4 — 5460 sequences, 0 mismatches — and the roots so chosen TILE the
    document from the first heading to its end, which is what guarantees that
    nothing authored under a heading is dropped.

    CONTRACT: this depends on ``sections`` returning rows in DOCUMENT ORDER
    (``_HEADING.finditer`` yields matches left to right and neither function
    sorts or reverses). A change that reordered those rows would break this
    function silently; the parametrised unit case pins the order as a list.
    """
    roots: list[tuple[int, str, str]] = []
    shallowest: int | None = None
    for level, name, body in sections(source):
        if shallowest is None or level <= shallowest:
            shallowest = level
            roots.append((level, name, body))
    return roots


def select_markdown_sections(
    source: str, names: tuple[str, ...], *, exclude: tuple[str, ...] = (),
) -> str:
    """Concatenate the named sections' bodies. A section's body includes its
    nested subsections — EXCEPT any subtree whose heading is in ``exclude``
    (#355: the shipped doctrines nest every projection heading under
    ``# Core doctrine``, so without exclusion a single-surface selection
    drags every sibling surface's instructions along)."""
    canonical = validate_markdown(source)
    spans = _section_spans(canonical)
    cut_ranges = [
        (heading_start, end)
        for _, name, heading_start, _, end in spans if name in exclude
    ]
    selected = []
    for _, name, _, body_start, end in spans:
        if name not in names:
            continue
        pos, parts = body_start, []
        for cut_start, cut_end in sorted(cut_ranges):
            # Only subtrees strictly INSIDE this section's body are cut —
            # a selected section that is itself excludable keeps its body
            # (its own heading starts before its body).
            if cut_start < body_start or cut_end > end or cut_end <= pos:
                continue
            parts.append(canonical[pos:max(pos, cut_start)])
            pos = max(pos, cut_end)
        parts.append(canonical[pos:end])
        body = "".join(parts).strip("\n") + "\n"
        selected.append(body)
    return "\n".join(body.rstrip("\n") for body in selected) + "\n"
