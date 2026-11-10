"""The job tools in a job's one long session: ``report_job_progress`` is a
plain, optional progress line; an approval left pending marks the job as
waiting for the operator, so the turn-end guard does not nudge it."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import background_jobs as bj

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def job(tmp_path):
    import tools
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    decl = bj.JobDecl("q:run", "q", "run", "q:job", "Accounting check", None)
    rec = await reg.create(
        "specialist", "finance", "in_casa", "t",
        {"role": "assistant", "channel": "telegram",
         "job": bj.initial_job_state(decl, started_by="operator")}, topic_id=9)
    from agent_log_double import RecordingLogs
    channel = MagicMock()
    channel._post_engagement_notice = AsyncMock()
    channel.logs = RecordingLogs()
    cm = MagicMock()
    cm.get.return_value = channel
    tools.init_tools(channel_manager=cm, bus=MagicMock(),
                     specialist_registry=MagicMock(), mcp_registry=MagicMock(),
                     trigger_registry=MagicMock(), engagement_registry=reg,
                     agent_logs=channel.logs)
    bj.configure(bj.JobRunner(registry=reg))
    token = tools.engagement_var.set(rec)
    yield rec, channel
    tools.engagement_var.reset(token)


def _payload(result):
    return json.loads(result["content"][0]["text"])


async def test_a_progress_line_is_logged_and_recorded(job):
    import tools
    from agent_logs import LogKey
    rec, channel = job
    out = _payload(await tools.report_job_progress.handler(
        {"summary": "12 rows classified\nignored", "done": 12, "remaining": 28}))
    assert out == {"ok": True}
    assert channel.logs.lines == [(LogKey("agent", "finance"), "Accounting check",
                                   "📊 12 rows classified · 12 done · 28 left")]
    channel._post_engagement_notice.assert_not_awaited()
    assert rec.origin["job"]["last_summary"] == "12 rows classified"
    assert bj._state(rec.id).progress is True


async def test_progress_needs_no_counts_and_no_batch(job):
    import tools
    rec, channel = job
    assert _payload(await tools.report_job_progress.handler(
        {"summary": "reading the ledger"}))["ok"] is True
    assert channel.logs.texts() == ["📊 reading the ledger"]
    assert "batch" not in tools.report_job_progress.description.lower()
    assert "progressed" not in tools.report_job_progress.input_schema["properties"]


async def test_bad_counts_are_refused(job):
    import tools
    out = _payload(await tools.report_job_progress.handler(
        {"summary": "x", "done": True}))
    assert out == {"ok": False, "kind": "invalid_arguments"}


async def test_a_pending_approval_marks_the_job_waiting(job):
    import authz_grants
    rec, _ = job
    authz_grants._note_approval("toolu_1", "pending", MagicMock())
    assert bj.awaiting(rec) == bj.AWAITING_APPROVAL
    rec.origin["job"].pop(bj.AWAITING_KEY)
    authz_grants._note_approval("toolu_2", "consumed", MagicMock())
    assert bj.awaiting(rec) is None


async def test_the_completion_refusal_says_the_message_arrives_next_step(job):
    import tools
    assert tools._UNREAD_REFUSAL == (
        "An operator message reaches you at your next step. Read it, then "
        "call emit_completion again.")
    assert "end your turn" not in tools._UNREAD_REFUSAL.lower()
