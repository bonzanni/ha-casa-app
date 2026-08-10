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
from datetime import datetime
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
    return m.group(1), text[m.end():]


def strip_time_envelope(text: str) -> str:
    """:func:`split_time_envelope`, discarding the timestamp."""
    return split_time_envelope(text)[1]
