"""Spec §4.2 #3 (next-turn-after-gap). Tests the extracted pure helper
agent._resume_decision rather than a full SDK turn.

Task 9: the helper now returns a structured ``ResumeDecision`` and gates on the
``{role_id, binding_digest}`` identity in addition to the freshness window."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent import _resume_decision

pytestmark = [pytest.mark.unit]

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
ROLE = "resident:assistant"
DIGEST = "sha256:" + "a" * 64


def _entry(sid, idle):
    return {
        "sdk_session_id": sid,
        "last_active": (NOW - idle).isoformat(),
        "agent": ROLE,
        "binding_digest": DIGEST,
    }


def _decide(entry, channel="voice"):
    return _resume_decision(
        channel, entry, NOW, role_id=ROLE, binding_digest=DIGEST,
    )


def test_resume_when_fresh():
    decision = _decide(_entry("s", timedelta(minutes=5)))
    assert decision.action == "resume"
    assert decision.resume_sid == "s"


def test_new_when_cold():
    # voice window 30m; idle 1h → start new
    decision = _decide(_entry("s", timedelta(hours=1)))
    assert decision.action == "new"
    assert decision.reason == "expired"


def test_new_when_no_entry():
    decision = _decide(None)
    assert decision.action == "new"
    assert decision.reason == "missing"


def test_new_when_entry_has_no_sid():
    decision = _resume_decision(
        "telegram", {"last_active": NOW.isoformat()}, NOW,
        role_id=ROLE, binding_digest=DIGEST,
    )
    assert decision.action == "new"
    assert decision.reason == "missing"
