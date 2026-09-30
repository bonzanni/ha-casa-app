"""#1116 red case (a): the shipped weekday morning-briefing prompt's TEXT.

The briefing is a buffered, final-text scheduled turn (its closing text is the
operator's copy; `<silent/>` is its only silence path — see
docs/architecture/scheduled-prompt-endings.md). At the base its checklist sent
the model to memory and to `get_schedule` and defined no send criteria, so an
ordinary day produced filler instead of silence.

This pins the text the model receives: the send tests, the statement that
recalled memory is not verification of today's state, that `get_schedule` is
Casa's own trigger list, the NEVER list, and the silence path. It does NOT and
cannot prove that a model obeys the text — whether the briefing actually stays
silent is observed after deploy, not here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

BRIEFING = (
    Path(__file__).resolve().parents[1]
    / "casa/rootfs/opt/casa/defaults/agents/assistant/prompts/morning-briefing.md"
)

PINS = (
    "Send one message only for what the operator must act on today and does "
    "not already know; silence is the default and the normal outcome.",

    "Look in live sources available this run, such as calendar, mail or tasks "
    "tools; do not assume a calendar integration exists.",

    "Recalled memory already in the instructions, any `recall_memory` result, "
    "and earlier assistant messages are leads or history, never verification "
    "of today's state.",

    "Use today's date and weekday from the turn's time and the source's own "
    "dates; a past mention does not establish that something is still open "
    "or overdue.",

    "`get_schedule` lists Casa's own triggers and reminders, not the operator's "
    "calendar or agenda; never report its entries.",

    "Send only if every line passes ALL of these tests:",

    "The line is concrete and concerns the operator's own action today.",

    "The line is verified from its live source in this run or explicitly "
    "marked unverified; marking alone does not qualify a line for sending.",

    "The operator has not already been told in the last 24 hours, unless "
    "something changed.",

    "Sending the line now is more useful than waiting for the operator to ask.",

    'NEVER send negative or empty results ("no meetings", "nothing else", '
    '"all quiet"), an unavailable check or memory outage, narration or '
    'self-status about what you checked, anything beginning with "I" or '
    '"Let me", restated memory or schedule entries, greetings, or closing '
    'questions or offers.',

    "Your tokens are buffered until the turn ends — nothing reaches the "
    "operator until you stop.",

    "SEND — output ONLY the final Telegram message text, at most six bullets; "
    "one is enough.",

    "STAY SILENT — output literally `<silent/>` and nothing else, or produce "
    "no output at all; never write prose about silence.",

    "If in doubt, emit `<silent/>`.",
)

FORBIDDEN = (
    "Check memory for anything scheduled or due today",
    "Check if any scheduled tasks or delegations are queued in your schedule",
    "After the send, output the sentinel",
)


def _n(s: str) -> str:
    return " ".join(s.split())


def test_briefing_carries_explicit_send_tests_and_silence() -> None:
    text = _n(BRIEFING.read_text(encoding="utf-8"))
    assert [text.count(_n(pin)) for pin in PINS] == [1] * len(PINS)
    assert [text.count(_n(pin)) for pin in FORBIDDEN] == [0, 0, 0]
    assert text.count("<silent/>") == 2
