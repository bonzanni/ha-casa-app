"""Reading the verbatim store by day: ``browse_memory_day``, and the shared
rendering of one stored event for the memory tools.

:func:`browse` returns one day, or up to :data:`MAX_DAYS` consecutive days,
of saved conversation in time order, optionally cut to a time window that
starts on the first day and ends on the last. It reads ``events`` in one read
transaction. The reader's clearance filters every event by its own tier
first, and the time window next; only then are the events counted and
packed into pages of about :data:`PAGE_CHARS` characters. So a count, a page
number or a page break never depends on an event the reader cannot read.

An event's day is the local day frozen when it was captured; its time is
shown in the zone it was captured in. Nothing here calls a model, and
nothing is classified by its words.

Blocking; async callers use ``asyncio.to_thread``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from memory import pending
from memory.store import MemoryStore
from memory.view import BUSY_TIMEOUT_S

#: The longest stretch one browse covers.
MAX_DAYS = 7
#: The size a page is packed to; an event longer than this is a page alone.
PAGE_CHARS = 10_000

#: An event's tier rank, as SQL.
TIER_RANK = ("(CASE e.tier WHEN 'public' THEN 0 WHEN 'friends' THEN 1 "
             "WHEN 'family' THEN 2 ELSE 3 END)")

EVENT_COLUMNS = ("seq", "occurred_at", "tz", "local_day", "role", "channel",
                 "speaker_kind", "speaker_id", "speaker_name", "scheduled",
                 "text")


def rows(conn: sqlite3.Connection, sql: str, args=()) -> list[dict]:
    cur = conn.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur]


def local_time(event: dict) -> datetime | None:
    """When *event* happened, in the zone it was captured in."""
    try:
        return datetime.fromisoformat(event["occurred_at"]).astimezone(
            ZoneInfo(event["tz"]))
    except (ValueError, KeyError, TypeError):
        return None


def speaker(event: dict, reader_role: str | None = None) -> str:
    """Who said it, from the event's recorded speaker: the reader's own
    earlier words are ``you``."""
    kind = event.get("speaker_kind")
    if kind == "operator":
        return event.get("speaker_name") or "operator"
    if kind == "resident":
        who = event.get("speaker_id") or event.get("role")
        return "you" if reader_role and who == reader_role else (who or "")
    if kind == "specialist":
        return event.get("speaker_id") or event.get("role") or "specialist"
    if kind == "casa":
        return "Casa"
    return event.get("speaker_name") or "someone else"


def clip(text: str, limit: int, cut: str) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + cut


def line(event: dict, reader_role: str | None = None, *,
         limit: int | None = None, cut: str = " …") -> str:
    """``HH:MM speaker: text``, with ``(scheduled)`` for a turn Casa's
    schedule fired."""
    at = local_time(event)
    when = at.strftime("%H:%M") if at else ""
    mark = " (scheduled)" if event.get("scheduled") else ""
    text = event.get("text") or ""
    if limit is not None:
        text = clip(text, limit, cut)
    return f"{when} {speaker(event, reader_role)}{mark}: {text}"


def day_heading(day: str) -> str:
    try:
        return f"## {date.fromisoformat(day).strftime('%A')} {day}"
    except ValueError:
        return f"## {day}"


@dataclass(frozen=True)
class Page:
    """One page of a browse. ``total`` counts the readable events in the
    stretch; ``first`` and ``last`` number this page's events in it (1-based,
    both 0 when the stretch holds none)."""

    text: str
    page: int
    pages: int
    total: int
    first: int = 0
    last: int = 0


def _in_window(event: dict, start: tuple, end: tuple) -> bool:
    at = local_time(event)
    moment = (event["local_day"], at.time() if at else time(0))
    return start <= moment <= end


def browse(store: MemoryStore, *, clearance: str, first_day: date,
           last_day: date, from_time: time | None = None,
           to_time: time | None = None, page: int = 1,
           reader_role: str | None = None) -> Page:
    """Page *page* of the saved conversation readable at *clearance* from
    *first_day* (at *from_time*) to *last_day* (at *to_time*). Raises
    ``ValueError`` for a stretch that ends before it starts or covers more
    than :data:`MAX_DAYS` days, and when the store is unavailable or
    fails."""
    if last_day < first_day:
        raise ValueError("the stretch ends before it starts")
    if (last_day - first_day).days + 1 > MAX_DAYS:
        raise ValueError(f"a browse covers at most {MAX_DAYS} days")
    start = (first_day.isoformat(), from_time or time(0))
    end = (last_day.isoformat(), to_time or time(23, 59, 59, 999999))
    with store.transaction(busy_timeout=BUSY_TIMEOUT_S) as conn:
        found = rows(
            conn, f"SELECT {', '.join('e.' + c for c in EVENT_COLUMNS)} "
            "FROM events AS e WHERE e.local_day BETWEEN ? AND ? "
            f"AND {TIER_RANK} <= ? ORDER BY e.occurred_at, e.seq",
            (start[0], end[0], pending.rank(clearance)))
    found = [e for e in found if _in_window(e, start, end)]
    pages: list[list[tuple[int, str]]] = []
    size = 0
    day = None
    for n, event in enumerate(found, 1):
        text = line(event, reader_role)
        if event["local_day"] != day:
            day = event["local_day"]
            text = day_heading(day) + "\n" + text
        if not pages or (size + len(text) + 1 > PAGE_CHARS and pages[-1]):
            pages.append([])
            size = 0
            if not text.startswith("## "):
                text = day_heading(day) + "\n" + text
        pages[-1].append((n, text))
        size += len(text) + 1
    if not pages:
        return Page(text="", page=page, pages=0, total=0)
    if page < 1 or page > len(pages):
        return Page(text="", page=page, pages=len(pages), total=len(found))
    shown = pages[page - 1]
    return Page(text="\n".join(t for _, t in shown), page=page,
                pages=len(pages), total=len(found), first=shown[0][0],
                last=shown[-1][0])
