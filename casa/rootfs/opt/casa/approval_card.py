"""Operator approval cards: model, rendering and fallbacks.

A card is plain text plus BOLD entities that Casa chooses itself, from the
card's structure alone: the whole title and each whole ``Label:``. Nothing a
model, a plugin or a tool argument says ever decides an entity, and no text
goes through a markup converter -- the display text is exactly what the
operator reads.

All lengths are UTF-16 code units, the unit Telegram counts in. Every field is
stripped of lone surrogates first (Telegram's UTF-8 request encoding rejects
them); the replacement keeps one unit per code point, so measurements and
offsets taken afterwards stay valid.

Pure module: no I/O, no channel or model access.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from telegram import MessageEntity

from text_util import (
    escape_unsafe_text, is_unsafe_text, replace_lone_surrogates, utf16_len,
    utf16_prefix_end,
)

# Bounds on a model-written card (checked, never clipped).
_ACTION_MAX = 60
_SUMMARY_MAX = 240
_LINES_MAX = 5
_LABEL_MAX = 24
_VALUE_MAX = 160

# Budgets of the card Casa writes itself from a tool call's arguments.
_FALLBACK_LABEL_MAX = 40
_FALLBACK_VALUE_MAX = 160
_FALLBACK_VALUE_HEAD = 120
_FALLBACK_LINES_MAX = 12
_FALLBACK_CARD_MAX = 2600
_FALLBACK_DEPTH_MAX = 20
_FALLBACK_COUNT_CAP = 10_000
# Room kept free for the closing "…and N more" line.
_CLOSING_LINE_RESERVE = 24

_SENSITIVE_MARK = "⚠️ "


@dataclass(frozen=True)
class CardLine:
    label: str
    value: str
    sensitive: bool = False


@dataclass(frozen=True)
class Card:
    title: str
    summary: str = ""
    lines: tuple[CardLine, ...] = ()
    # Appended to the title verbatim (it carries its own leading newline);
    # used only by the text fallback.
    body: str = ""


def _clean(text: str) -> str:
    return replace_lone_surrogates(text)


def _bold(offset: int, length: int) -> MessageEntity:
    return MessageEntity(type=MessageEntity.BOLD, offset=offset, length=length)


def render_card(card: Card) -> tuple[str, list[MessageEntity]]:
    """Display text and BOLD entities for *card*, built in one pass."""
    parts: list[str] = []
    entities: list[MessageEntity] = []
    pos = 0  # UTF-16 units appended so far

    def add(text: str, *, bold_units: int = 0) -> None:
        nonlocal pos
        if bold_units:
            entities.append(_bold(pos, bold_units))
        parts.append(text)
        pos += utf16_len(text)

    title = _clean(card.title)
    add(title, bold_units=utf16_len(title))
    summary = _clean(card.summary)
    if summary:
        add("\n\n" + summary)
    for i, line in enumerate(card.lines):
        add("\n\n" if i == 0 else "\n")
        label = _clean(line.label)
        value = _clean(line.value)
        if line.sensitive:
            add(_SENSITIVE_MARK)
        if label:
            add(label + ":", bold_units=utf16_len(label) + 1)
            add(" " + value)
        else:
            add(value)
    add(_clean(card.body))
    return "".join(parts), entities


def shift_entities(entities: list, prefix: str) -> list:
    """New entities moved right by the UTF-16 length of *prefix*."""
    shift = utf16_len(prefix)
    return [
        MessageEntity(type=e.type, offset=e.offset + shift, length=e.length)
        for e in entities
    ]


def _bounded_str(value: object, limit: int) -> str | None:
    """*value* when it is a one-line safe string within *limit*, else None."""
    if not isinstance(value, str):
        return None
    value = _clean(value)
    if is_unsafe_text(value) or utf16_len(value) > limit:
        return None
    return value


def card_from_reply(text: str, *, who: str, title: str | None = None) -> Card | None:
    """Parse the model's JSON reply into a card, or None when it is not
    exactly the agreed shape or any field is out of bounds."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    action = _bounded_str(data.get("action"), _ACTION_MAX)
    summary = _bounded_str(data.get("summary"), _SUMMARY_MAX)
    raw_lines = data.get("lines")
    if not action or summary is None:
        return None
    if not isinstance(raw_lines, list) or len(raw_lines) > _LINES_MAX:
        return None
    lines: list[CardLine] = []
    flagged = False
    for raw in raw_lines:
        if not isinstance(raw, dict):
            return None
        label = _bounded_str(raw.get("label"), _LABEL_MAX)
        value = _bounded_str(raw.get("value"), _VALUE_MAX)
        sensitive = raw.get("sensitive", False)
        if label is None or value is None or not isinstance(sensitive, bool):
            return None
        # Only the first sensitive line is flagged: the mark loses meaning
        # when everything carries it.
        lines.append(CardLine(label, value, sensitive and not flagged))
        flagged = flagged or sensitive
    if title is None:
        title = f"🔐 Allow {_clean(who)} to {action}?"
    return Card(title=title, summary=summary, lines=tuple(lines))


def text_fallback(body: str) -> Card:
    """Card for a consent text Casa already wrote: the first line becomes the
    bold title, everything after it is kept literally."""
    body = _clean(body)
    first, newline, rest = body.partition("\n")
    return Card(title=first, body=newline + rest)


# --- tool fallback -----------------------------------------------------------

def _is_scalar(value: object) -> bool:
    return not isinstance(value, (dict, list))


def _scalar_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _label_part(key: object) -> str:
    return escape_unsafe_text(_clean(str(key))).replace("_", " ").capitalize()


def _fit_label(label: str) -> str:
    if utf16_len(label) <= _FALLBACK_LABEL_MAX:
        return label
    return label[:utf16_prefix_end(label, 0, _FALLBACK_LABEL_MAX - 1)] + "…"


def _fit_value(value: str) -> str:
    value = escape_unsafe_text(_clean(value))
    if utf16_len(value) <= _FALLBACK_VALUE_MAX:
        return value
    head = value[:utf16_prefix_end(value, 0, _FALLBACK_VALUE_HEAD)]
    return f"{head}… ({len(value)} characters)"


def _children(value: object):
    """(path part, child) pairs of a container, in canonical order: dict keys
    sorted at every level, list items in sequence."""
    if isinstance(value, dict):
        return sorted(((str(k), v) for k, v in value.items()), key=lambda kv: kv[0])
    return [(str(i + 1), v) for i, v in enumerate(value)]


def _leaves(args: dict):
    """Yield ``(path, text)`` for every leaf of *args*, depth first in
    canonical order. Iterative (an explicit stack), so argument depth never
    reaches the interpreter's recursion limit; a container deeper than the
    cap is one "…" leaf."""
    stack = [((), args, 0)]
    while stack:
        path, value, depth = stack.pop()
        if _is_scalar(value):
            yield path, _scalar_text(value)
        elif depth > _FALLBACK_DEPTH_MAX:
            yield path, "…"
        elif not value:
            yield path, "{}" if isinstance(value, dict) else "[]"
        elif isinstance(value, list) and all(_is_scalar(v) for v in value):
            yield path, ", ".join(_scalar_text(v) for v in value)
        else:
            for part, child in reversed(_children(value)):
                stack.append((path + (part,), child, depth + 1))


def tool_fallback(*, who: str, plugin: str, args: dict) -> Card:
    """Card Casa writes itself from a tool call's arguments, within fixed
    budgets, naming how many lines it left out."""
    title = f"🔐 Allow {_clean(who)} to use {_clean(plugin)}?"
    lines: list[CardLine] = []
    used = utf16_len(title)
    full = False
    left = 0
    for path, text in _leaves(args if isinstance(args, dict) else {}):
        if full:
            left += 1
            if left >= _FALLBACK_COUNT_CAP:
                break
            continue
        label = _fit_label(" › ".join(_label_part(p) for p in path))
        line = CardLine(label, _fit_value(text))
        # The line's own rendered cost: separator, label + ": " and value.
        cost = 2 if not lines else 1
        cost += utf16_len(line.value) + (utf16_len(label) + 2 if label else 0)
        if (len(lines) >= _FALLBACK_LINES_MAX
                or used + cost > _FALLBACK_CARD_MAX - _CLOSING_LINE_RESERVE):
            full = True
            left += 1
            continue
        lines.append(line)
        used += cost
    if left:
        more = f"{left}+" if left >= _FALLBACK_COUNT_CAP else str(left)
        lines.append(CardLine("", f"…and {more} more"))
    return Card(title=title, lines=tuple(lines))
