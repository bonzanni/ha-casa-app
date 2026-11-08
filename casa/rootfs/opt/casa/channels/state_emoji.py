"""U3 + U13 emoji lookup tables + topic-title composition."""

from __future__ import annotations

import re

from text_util import (
    TELEGRAM_TOPIC_NAME_BYTES, is_unsafe_text, replace_lone_surrogates,
    truncate_for_topic,
)

# §6.3 state emoji.
STATE_EMOJI: dict[str, str] = {
    "active": "🟢",
    "awaiting": "🟡",
    "completed": "✅",
    "failed": "❌",
    "cancelled": "⏹",
}

# §6.3 role emoji.
ROLE_EMOJI: dict[str, str] = {
    "configurator": "⚙️",
}

_DEFAULT_ROLE_EMOJI = "🤖"

# §6.9 progress-panel glyphs.
PROGRESS_GLYPH: dict[str, str] = {
    "pending": "☐",
    "in_progress": "⏳",
    "completed": "☑",
    "blocked": "🚫",
    "skipped": "⏭",
}

# §6.3 byte budget for U3 task body (tightened from TELEGRAM_TOPIC_NAME_BYTES=128).
U3_TASK_BYTE_BUDGET = 26

_LEADING_FILLER_RE = re.compile(
    r"^(?:please|can you|i need you to|help me|could you|"
    r"would you|let's|let us)\s+",
    re.IGNORECASE,
)
# §6.3 rule 2: drop articles as whole word tokens (anywhere, not just leading).
_ARTICLE_RE = re.compile(r"\b(?:the|a|an)\s+", re.IGNORECASE)
_TRAILING_PUNCT_RE = re.compile(r"[\.!\?:,;]+$")


def role_emoji(executor_type: str) -> str:
    """Fall back to 🤖 for unknown executor types (never raises).

    Kept for non-topic contexts (logs, doctrine snippets) even
    though as of v0.37.1 the topic-title composer no longer uses
    it — the topic bubble carries the role icon (see
    ``channels.topic_icons``) and the title carries state + task.
    """
    return ROLE_EMOJI.get(executor_type, _DEFAULT_ROLE_EMOJI)


def concise_task(task: str) -> str:
    """Apply §6.3 concision rules 1-4 to ``task``.

    Returns a string whose UTF-8 byte length is ≤ U3_TASK_BYTE_BUDGET.
    """
    if not task:
        return ""
    s = task.strip()
    while True:
        m = _LEADING_FILLER_RE.match(s)
        if not m:
            break
        s = s[m.end():].lstrip()
    s = _ARTICLE_RE.sub("", s)
    s = _TRAILING_PUNCT_RE.sub("", s).rstrip()
    return truncate_for_topic(s, byte_budget=U3_TASK_BYTE_BUDGET)


# W-R6 (v0.81.0): an engager may supply a short ``topic_title`` on
# engage_executor. It is normalized ONCE at ingest, persisted on the
# EngagementRecord, and shared by the topic-name state edit, the live summary
# title and the hand-off line in the person's chat — a single source.
# v0.347.12: about 40 chars, cut between words, and a cut always shows "…".
# The title also stays within Telegram's topic-name byte limit once
# compose_topic_title puts a state emoji and a space in front of it.
TOPIC_TITLE_CHAR_CAP = 40
TOPIC_TITLE_BYTE_CAP = TELEGRAM_TOPIC_NAME_BYTES - max(
    len(f"{e} ".encode("utf-8")) for e in STATE_EMOJI.values())
_WORD_RE = re.compile(r"\S+")


def _title_fits(t: str) -> bool:
    return (len(t) <= TOPIC_TITLE_CHAR_CAP
            and len(t.encode("utf-8")) <= TOPIC_TITLE_BYTE_CAP)


def normalize_topic_title(raw: object) -> str:
    """Normalize an OPTIONAL engager-supplied ``topic_title`` (W-R6).

    Rejects UNSAFE-TEXT (control/bidi codepoints incl. newlines — the v0.78
    predicate) by returning ``""`` so the caller falls back to a Casa-derived
    label. A safe title longer than ``TOPIC_TITLE_CHAR_CAP`` (or the byte cap)
    keeps the words that fit, separators untouched, and ends in "…"; the
    result never exceeds either cap. A first word too long to fit is cut
    inside the word, also ending in "…" (#357).
    A lone surrogate becomes U+FFFD.
    Returns ``""`` for a non-str, blank, or unsafe value."""
    if not isinstance(raw, str):
        return ""
    # A JSON "\ud800" decodes to a lone surrogate, which cannot be encoded
    # (to measure bytes here, or by Telegram later): U+FFFD stands in.
    s = replace_lone_surrogates(raw).strip()
    if not s or is_unsafe_text(s):
        return ""
    if _title_fits(s):
        return s
    end = 0
    for m in _WORD_RE.finditer(s):
        if not _title_fits(s[:m.end()] + "…"):
            break
        end = m.end()
    kept = s[:end]
    if not kept:
        kept = s[:TOPIC_TITLE_CHAR_CAP - 1]
        while not _title_fits(kept + "…"):
            kept = kept[:-1]
        kept = kept.rstrip()
    return kept + "…"


def compose_topic_title(*, state: str, short_task: str) -> str:
    """Compose '<state> <short_task>' per spec §6.3 (revised v0.37.1).

    The role emoji is no longer in the title — the bubble carries it
    via ``channels.topic_icons.icon_id_for_role``. Unknown ``state``
    falls back to 🟢 (active) — never raises.
    """
    se = STATE_EMOJI.get(state, STATE_EMOJI["active"])
    return f"{se} {short_task}".rstrip()
