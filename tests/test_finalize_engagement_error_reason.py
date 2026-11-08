"""G-4 (v0.33.0) regression-locker: _finalize_engagement must surface a
reason field when outcome=error.

Background — exploration2 (2026-05-01) finding G-4: configurator
engagement `fa3c1486` finalized outcome=error 24s after subprocess
system_init with zero tool_uses inside. Pre-fix the only log line was
an unconditional `logger.info("... outcome=error")` with no reason
field — operators saw `outcome=error` and had no starting point for
triage.

Fix shape: in tools.py::_finalize_engagement, when outcome=='error'
emit a WARNING-level log with structured reason fields pulled off the
registry origin (`error_kind` + `error_message` set by mark_error) plus
the text the caller passed in.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


async def test_outcome_error_emits_warning_with_reason(tmp_path, caplog):
    """When _finalize_engagement is called with outcome='error' it must
    log at WARNING level with structured `kind=` and `reason=` fields.
    Verifies G-4 fix.

    Implementation note: _finalize_engagement itself runs
    `mark_error(kind='emit_completion_error', message=text)` BEFORE
    reaching the log line, so kind is always stamped — pre-fix the
    operator's only signal was an unconditional INFO line with
    nothing but the engagement id.
    """
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="t",
        origin={"role": "assistant", "channel": "telegram", "chat_id": "12345"},
        topic_id=42,
    )
    init_tools(
        channel_manager=None, bus=None,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    with caplog.at_level(logging.WARNING, logger="tools"):
        await _finalize_engagement(
            rec, outcome="error",
            text="schema validation failed: extra key TRAIT",
            artifacts=[], next_steps=[],
            driver=None,
        )

    finalize_lines = [
        r for r in caplog.records
        if "finalized outcome=error" in r.getMessage()
    ]
    assert finalize_lines, (
        "expected a 'finalized outcome=error' log record; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    rec_msg = finalize_lines[0].getMessage()
    assert finalize_lines[0].levelno == logging.WARNING, (
        f"outcome=error must log at WARNING level (got "
        f"{finalize_lines[0].levelname}: {rec_msg!r})"
    )
    # Both structured fields must be present.
    assert "kind=" in rec_msg, f"expected kind= field: {rec_msg!r}"
    assert "reason=" in rec_msg, f"expected reason= field: {rec_msg!r}"
    # The text the caller passed in must surface as the reason — that's
    # the operator's debug starting point.
    assert "schema validation failed" in rec_msg, (
        f"expected text to surface in reason: {rec_msg!r}"
    )


async def test_outcome_error_handles_empty_text(tmp_path, caplog):
    """Defensive: even when the caller passes an empty text (the G-4
    repro shape — model emitted status without explanation), the log
    still includes a `reason=` field with a sentinel value rather than
    the raw bug-pre-fix shape of no reason at all."""
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="t", origin={"role": "assistant", "channel": "telegram"},
        topic_id=42,
    )
    init_tools(
        channel_manager=None, bus=None,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    with caplog.at_level(logging.WARNING, logger="tools"):
        await _finalize_engagement(
            rec, outcome="error", text="",
            artifacts=[], next_steps=[],
            driver=None,
        )

    finalize_lines = [
        r for r in caplog.records
        if "finalized outcome=error" in r.getMessage()
    ]
    assert finalize_lines, "expected outcome=error log line"
    msg = finalize_lines[0].getMessage()
    assert "reason=" in msg, f"expected reason= field always present: {msg!r}"
    # mark_error inside finalize stashes kind="emit_completion_error" +
    # message=text; with empty text the registry-stored message is "",
    # so reason falls back to the sentinel.
    assert "no_reason_provided" in msg, (
        f"expected sentinel when no reason text available: {msg!r}"
    )


async def test_completed_engagement_posts_the_plain_summary(
    tmp_path,
) -> None:
    """Sanity: a completed engagement gets the plain completion summary,
    with nothing appended."""
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="t", origin={"role": "assistant", "channel": "telegram"},
        topic_id=42,
    )

    tch = MagicMock()
    tch.send_to_topic = AsyncMock()
    tch.send_response_to_topic = AsyncMock()
    tch.close_topic = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = tch
    init_tools(
        channel_manager=cm, bus=MagicMock(notify=AsyncMock()),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    await _finalize_engagement(
        rec, outcome="completed", text="all done",
        artifacts=[], next_steps=[], driver=None,
    )

    tch.send_response_to_topic.assert_awaited_once()  # v0.109.0 rich summary
    posted_text = tch.send_response_to_topic.await_args.args[1]
    assert posted_text == "Engagement completed. Summary:\nall done"


async def test_outcome_completed_stays_at_info_level(tmp_path, caplog):
    """Sanity: outcome=completed must NOT regress to WARNING level."""
    from engagement_registry import EngagementRegistry
    from tools import _finalize_engagement, init_tools

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="t", origin={"role": "assistant", "channel": "telegram"},
        topic_id=42,
    )
    init_tools(
        channel_manager=None, bus=None,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    with caplog.at_level(logging.INFO, logger="tools"):
        await _finalize_engagement(
            rec, outcome="completed", text="all good",
            artifacts=[], next_steps=[],
            driver=None,
        )

    completed_lines = [
        r for r in caplog.records
        if "finalized outcome=completed" in r.getMessage()
    ]
    assert completed_lines, "missing completed-path log line"
    assert all(r.levelno == logging.INFO for r in completed_lines), (
        "outcome=completed must stay at INFO level"
    )
