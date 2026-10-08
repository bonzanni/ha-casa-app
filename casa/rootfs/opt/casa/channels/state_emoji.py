"""U3 + U13 emoji lookup tables + topic-title composition.

Spec: docs/superpowers/specs/2026-05-12-e12-claude_code-channels.md §6.3, §6.9.
"""

from __future__ import annotations

import re

from text_util import is_unsafe_text, truncate_for_topic

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
    "plugin-developer": "🛠",
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


# W-R6 (v0.81.0): an engager may supply a short 2-3 word ``topic_title`` on
# engage_executor. It is normalized ONCE at ingest, persisted on the
# EngagementRecord, and shared by the topic-name state edit AND the live
# summary title — a single source. ~24 chars / 3 words, word-boundary capped.
TOPIC_TITLE_CHAR_CAP = 24
TOPIC_TITLE_WORD_CAP = 3


def normalize_topic_title(raw: object) -> str:
    """Normalize an OPTIONAL engager-supplied ``topic_title`` (W-R6).

    Rejects UNSAFE-TEXT (control/bidi codepoints incl. newlines — the v0.78
    predicate) by returning ``""`` so the caller falls back to a Casa-derived
    label. A safe title is capped to ~24 chars / 3 words at a WORD boundary,
    or inside a word (ellipsized) when the boundary would keep under half.
    Returns ``""`` for a non-str, blank, or unsafe value."""
    if not isinstance(raw, str):
        return ""
    s = raw.strip()
    if not s or is_unsafe_text(s):
        return ""
    words = s.split()
    if len(words) > TOPIC_TITLE_WORD_CAP:
        s = " ".join(words[:TOPIC_TITLE_WORD_CAP])
    if len(s) > TOPIC_TITLE_CHAR_CAP:
        head = s[:TOPIC_TITLE_CHAR_CAP]
        # #1368: cut at the last word boundary only when that keeps at least
        # half the cap; otherwise ("Remove water-plants-test trigger" kept
        # only "Remove") cut inside the word and ellipsize, as #357 does for
        # a first word longer than the cap.
        if " " in head and len(head.rsplit(" ", 1)[0].rstrip()) >= \
                TOPIC_TITLE_CHAR_CAP // 2:
            head = head.rsplit(" ", 1)[0]
        else:
            head = s[:TOPIC_TITLE_CHAR_CAP - 1].rstrip() + "…"
        s = head.rstrip()
    return s


def compose_topic_title(*, state: str, short_task: str) -> str:
    """Compose '<state> <short_task>' per spec §6.3 (revised v0.37.1).

    The role emoji is no longer in the title — the bubble carries it
    via ``channels.topic_icons.icon_id_for_role``. Unknown ``state``
    falls back to 🟢 (active) — never raises.
    """
    se = STATE_EMOJI.get(state, STATE_EMOJI["active"])
    return f"{se} {short_task}".rstrip()
