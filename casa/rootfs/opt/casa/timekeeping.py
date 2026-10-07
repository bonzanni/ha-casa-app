"""Single source of truth for the app's timezone.

Read from ``CASA_TZ`` env var, else ``TZ`` env var (which HA OS sets to
the operator's own zone), else UTC as the final fallback. Used by
APScheduler (so cron wall-clock means local time) and by
``Agent._process`` (for the ``<current_time>`` block in the composed
system prompt).

The order is what makes the app locale-neutral, and it only works while
the ``casa_tz`` option ships EMPTY: a pre-populated default would win
over ``TZ`` on every fresh install and silently impose the packager's
zone on operators elsewhere (Sol review). An empty option resolves to
nothing here, so Home Assistant's own zone is used.

If the resolved name is not a known IANA zone, log a warning and fall
back to UTC rather than raising. ``ZoneInfoNotFoundError``
is not cached by ``@lru_cache``, so without this guard a typo'd ``casa_tz``
add-on option would crash every turn.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_FALLBACK_TZ = "UTC"

# #471: the composed envelope is one inner line between the tags, then a blank
# line before the user text. The stripper matches STRUCTURE, not the exact
# datetime rendering, so it tolerates any single-line payload — and a pinned
# compose→strip round-trip test keeps the pair from drifting apart. The inner
# line's leading ISO token is captured so the turn's wall-clock time survives
# out-of-band (RetainedTurn.timestamp) once the envelope leaves the text.
# The terminator is the composed "\n\n" OR end-of-input: the readback boundary
# whitespace-strips messages first, so an envelope-only turn arrives without
# its trailing blank line and must still be recognised (and then dropped).
_TIME_ENVELOPE_RE = re.compile(
    r"\A<current_time>\n(\S+)[^\n]*\n</current_time>(?:\n\n|\s*\Z)")

# #1314/#1317: Casa's own per-turn notes (the front-desk lines, a reply's note)
# ride in ONE block directly after the envelope, so the readback strips them
# with it and nothing Casa wrote is retained as the speaker's words. The block
# is recognised only right after an envelope, and its close only as the
# composer writes it: a note's own text can never contain the close tag (the
# composer breaks it with a zero-width space). Same terminator rule as the
# envelope: the readback's whitespace strip eats the blank line when the body
# after the block is whitespace only.
NOTES_OPEN = "<casa_notes>\n"
NOTES_CLOSE = "</casa_notes>"
_NOTES_RE = re.compile(r"<casa_notes>\n.*?\n</casa_notes>(?:\n\n|\s*\Z)", re.S)

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def resolve_tz() -> ZoneInfo:
    tz_name = (
        os.environ.get("CASA_TZ")
        or os.environ.get("TZ")
        or _FALLBACK_TZ
    )
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning(
            "resolve_tz: %r is not a known IANA timezone; "
            "falling back to %r. Fix the casa_tz add-on option to silence "
            "this warning.", tz_name, _FALLBACK_TZ,
        )
        return ZoneInfo(_FALLBACK_TZ)


@dataclass(frozen=True)
class RecallWindow:
    """#1120: a resolved time period for a memory search. Both bounds are
    timezone-aware and INCLUSIVE, the same representation the memory server's
    ``temporal_window`` takes, so the bounds Casa filters on, sends and echoes
    are one value. Comparisons are made on the UTC timeline: two datetimes
    sharing one zone object compare by wall clock, which is wrong in the hour
    a clock change repeats."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        for bound in (self.start, self.end):
            if bound.tzinfo is None or bound.utcoffset() is None:
                raise ValueError("a recall window needs timezone-aware bounds")
        if self.end.astimezone(timezone.utc) < self.start.astimezone(timezone.utc):
            raise ValueError("a recall window cannot end before it starts")

    def contains(self, moment: datetime) -> bool:
        utc = moment.astimezone(timezone.utc)
        return (self.start.astimezone(timezone.utc) <= utc
                <= self.end.astimezone(timezone.utc))


# The named periods a search can be limited to; weeks start on Monday, as the
# ``week N`` of the time envelope does.
NAMED_PERIODS = ("today", "yesterday", "this_week", "last_week",
                 "this_month", "last_month")
_ISO_DAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


def _parse_day(text: str) -> date:
    # date.fromisoformat also takes "20260404" and "2026-W14-6"; only the one
    # published form is a period.
    if not _ISO_DAY_RE.fullmatch(text):
        raise ValueError(f"not a YYYY-MM-DD day: {text!r}")
    return date.fromisoformat(text)


def _named_days(name: str, today: date) -> tuple[date, date]:
    if name == "today":
        return today, today
    if name == "yesterday":
        day = today - timedelta(days=1)
        return day, day
    if name in ("this_week", "last_week"):
        monday = today - timedelta(days=today.weekday())
        if name == "last_week":
            monday -= timedelta(days=7)
        return monday, monday + timedelta(days=6)
    first_this = today.replace(day=1)
    if name == "this_month":
        next_first = (first_this + timedelta(days=32)).replace(day=1)
        return first_this, next_first - timedelta(days=1)
    last_prev = first_this - timedelta(days=1)  # last_month
    return last_prev.replace(day=1), last_prev


def resolve_period(spec: str, now: datetime) -> RecallWindow:
    """#1120: resolve a period — a name in :data:`NAMED_PERIODS`, one day
    ``YYYY-MM-DD``, or an inclusive day range ``YYYY-MM-DD..YYYY-MM-DD`` — to
    whole local days in ``now``'s timezone (the operator's, from
    :func:`resolve_tz`). Raises ``ValueError`` on anything else, including a
    range that ends before it starts.

    The bounds are computed on the UTC timeline: the first instant of the
    first day, and one microsecond before the first instant of the day after
    the last. Subtracting on the wall clock instead would cut off the hour a
    clock change repeats at the end of a day."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("resolve_period needs a timezone-aware now")
    if not isinstance(spec, str):
        raise ValueError("a period is a string")
    tz = now.tzinfo
    key = spec.strip().lower().replace(" ", "_").replace("-", "_")
    if key in NAMED_PERIODS:
        first, last = _named_days(key, now.date())
    else:
        text = spec.strip()
        lo, sep, hi = text.partition("..")
        first = _parse_day(lo)
        last = _parse_day(hi) if sep else first
        if last < first:
            raise ValueError("a period cannot end before it starts")

    def _first_instant(day: date) -> datetime:
        return datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc)

    start = _first_instant(first)
    end = _first_instant(last + timedelta(days=1)) - timedelta(microseconds=1)
    return RecallWindow(start=start.astimezone(tz), end=end.astimezone(tz))


def compose_time_envelope(now: datetime) -> str:
    """The per-turn ``<current_time>`` block (M27) that Agent._process prepends
    to the sent query text, INCLUDING the blank-line separator. Kept beside
    :func:`strip_time_envelope` as a pinned pair: retention reads the SDK
    transcript back, and the envelope must never reach the content-addressed
    ``document_id`` or the stored memory text (#471)."""
    return (
        f"<current_time>\n"
        f"{now.isoformat(timespec='seconds')} "
        f"({now.strftime('%A').lower()} "
        f"{now.strftime('%p').lower()}, "
        f"week {now.isocalendar().week})\n"
        f"</current_time>\n\n"
    )


def compose_turn_preamble(now: datetime, notes: "list[str] | tuple[str, ...]" = ()) -> str:
    """What Agent._process prepends to a turn's query text: the envelope, then
    — only when there are notes — ONE ``<casa_notes>`` block, each note a
    paragraph. With no notes it is exactly :func:`compose_time_envelope`.
    Pinned with :func:`split_time_envelope`, which strips both."""
    envelope = compose_time_envelope(now)
    kept = [n.replace(NOTES_CLOSE, "</casa_notes\u200b>").strip("\n") for n in notes if n and n.strip()]
    if not kept:
        return envelope
    return envelope + NOTES_OPEN + "\n\n".join(kept) + "\n" + NOTES_CLOSE + "\n\n"


def split_time_envelope(text: str) -> tuple[str | None, str]:
    """Split ONE leading turn envelope off ``text``: ``(iso_timestamp, rest)``
    when the envelope is present, ``(None, text)`` otherwise — no envelope, an
    envelope mentioned mid-text, a quoted block after the real one all pass
    through untouched. Applied at the transcript-readback boundary
    (session_saver), to USER turns only, so an identical utterance hashes and
    stores identically whatever second it was said in (#471), while the
    turn's wall-clock time survives out-of-band on the retain item."""
    m = _TIME_ENVELOPE_RE.match(text)
    if m is None:
        return None, text
    rest = text[m.end():]
    # the composer's notes block, only where it puts it (#1314/#1317)
    notes = _NOTES_RE.match(rest)
    if notes is not None:
        rest = rest[notes.end():]
    return m.group(1), rest


def strip_time_envelope(text: str) -> str:
    """:func:`split_time_envelope`, discarding the timestamp."""
    return split_time_envelope(text)[1]
