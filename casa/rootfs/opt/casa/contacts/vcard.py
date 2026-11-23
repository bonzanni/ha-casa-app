"""A small vCard reader for what an address book serves (3.0, Apple habits).

Every ``BEGIN:VCARD`` block yields exactly one :class:`Card`; nothing is
dropped, because a missing card would later read as a deleted contact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ESCAPE = re.compile(r"\\([nN,;\\])")
_ITEM_PREFIX = re.compile(r"^item\d+\.", re.IGNORECASE)
_BEGIN = re.compile(r"^BEGIN:VCARD\s*$", re.IGNORECASE | re.MULTILINE)
_NOT_LABELS = {"VOICE", "PREF"}


@dataclass(frozen=True)
class Card:
    uid: str
    name: str
    first_name: str
    org: str
    note: str
    tels: tuple[tuple[str, str], ...]
    related: tuple[str, ...]
    is_group: bool
    members: tuple[str, ...]


def count_blocks(text: str) -> int:
    """Number of ``BEGIN:VCARD`` lines in ``text``."""
    return len(_BEGIN.findall(text))


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _unescape(value: str) -> str:
    return _ESCAPE.sub(lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def _split_line(line: str) -> tuple[str, list[str], str] | None:
    """Split ``name;params:value``; a colon inside quoted params is skipped."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return None
    name, *params = head.split(";")
    name = _ITEM_PREFIX.sub("", name.strip()).upper()
    return name, params, value


def _label(params: list[str]) -> str:
    for param in params:
        key, sep, val = param.partition("=")
        types = val if sep and key.strip().upper() == "TYPE" else ("" if sep else key)
        for t in types.split(","):
            t = t.strip().strip('"').upper()
            if t and t not in _NOT_LABELS:
                return t
    return ""


def _build(lines: list[str]) -> Card:
    uid = fn = org = note = ""
    family = given = ""
    tels: list[tuple[str, str]] = []
    related: list[str] = []
    members: list[str] = []
    is_group = False
    for line in lines:
        parts = _split_line(line)
        if parts is None:
            continue
        name, params, value = parts
        if name == "UID" and not uid:
            uid = value.strip()
        elif name == "FN":
            fn = _unescape(value).strip()
        elif name == "N":
            comps = re.split(r"(?<!\\);", value)
            family = _unescape(comps[0]).strip() if comps else ""
            given = _unescape(comps[1]).strip() if len(comps) > 1 else ""
        elif name == "ORG":
            org = _unescape(re.split(r"(?<!\\);", value)[0]).strip()
        elif name == "NOTE":
            note = _unescape(value)
        elif name == "TEL":
            tels.append((_unescape(value).strip(), _label(params)))
        elif name in ("X-ABRELATEDNAMES", "RELATED"):
            if value.strip():
                related.append(_unescape(value).strip())
        elif name == "X-ADDRESSBOOKSERVER-KIND":
            is_group = value.strip().lower() == "group"
        elif name == "X-ADDRESSBOOKSERVER-MEMBER":
            v = value.strip()
            members.append(v[len("urn:uuid:"):] if v.lower().startswith("urn:uuid:") else v)
    display = fn or " ".join(p for p in (given, family) if p) or org
    return Card(
        uid=uid,
        name=display,
        first_name=given,
        org=org,
        note=note,
        tels=tuple(tels),
        related=tuple(related),
        is_group=is_group,
        members=tuple(members),
    )


def parse_cards(text: str) -> list[Card]:
    """Parse one or many vCard blocks; one :class:`Card` per ``BEGIN:VCARD``."""
    cards: list[Card] = []
    block: list[str] | None = None
    for line in _unfold(text):
        tag = line.strip().upper()
        if tag == "BEGIN:VCARD":
            if block is not None:
                cards.append(_build(block))
            block = []
        elif tag == "END:VCARD":
            if block is not None:
                cards.append(_build(block))
            block = None
        elif block is not None:
            block.append(line)
    if block is not None:
        cards.append(_build(block))
    return cards
