"""The job tools in a job's one long session: ``report_job_progress`` is a
plain, optional progress note Casa keeps; an approval left pending marks the
job as waiting for the operator, so the turn-end guard does not nudge it."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

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
         "job": bj.initial_job_state(decl, started_by="operator")})
    channel = MagicMock()
    cm = MagicMock()
    cm.get.return_value = channel
    tools.init_tools(channel_manager=cm, bus=MagicMock(),
                     specialist_registry=MagicMock(), mcp_registry=MagicMock(),
                     trigger_registry=MagicMock(), engagement_registry=reg)
    bj.configure(bj.JobRunner(registry=reg))
    token = tools.engagement_var.set(rec)
    yield rec, channel
    tools.engagement_var.reset(token)


def _payload(result):
    return json.loads(result["content"][0]["text"])


async def test_a_progress_note_is_recorded(job):
    import tools
    rec, channel = job
    out = _payload(await tools.report_job_progress.handler(
        {"summary": "12 rows classified\nignored", "done": 12, "remaining": 28}))
    assert out == {"ok": True}
    assert rec.origin["job"]["last_summary"] == "12 rows classified (12 of 40 done)"


@pytest.mark.parametrize("counts,note", [
    ({}, "rows classified"),
    ({"done": 12, "remaining": 28}, "rows classified (12 of 40 done)"),
    ({"done": 12}, "rows classified (12 done)"),
    ({"remaining": 28}, "rows classified (28 remaining)"),
    ({"done": 0, "remaining": 0}, "rows classified (0 of 0 done)"),
])
async def test_the_saved_note_says_the_counts_the_job_passed(job, counts, note):
    import tools
    rec, channel = job
    await tools.report_job_progress.handler({"summary": "rows classified", **counts})
    assert rec.origin["job"]["last_summary"] == note


@pytest.mark.parametrize("counts,suffix", [
    ({}, ""),
    ({"done": 12, "remaining": 28}, " (12 of 40 done)"),
    ({"done": 7}, " (7 done)"),
    ({"remaining": 123456}, " (123456 remaining)"),
])
async def test_the_saved_note_keeps_its_bound_with_the_counts(job, counts, suffix):
    """The summary is cut so that it and the counts fit in 300 characters:
    the counts are never the part that is lost."""
    import tools
    rec, channel = job
    await tools.report_job_progress.handler({"summary": "x" * 400, **counts})
    note = rec.origin["job"]["last_summary"]
    assert len(note) == 300 == bj.PROGRESS_NOTE_MAX
    assert note == "x" * (300 - len(suffix)) + suffix


async def test_a_note_of_counts_alone_and_counts_too_long_to_fit():
    assert bj.progress_note("", 3, 1) == "(3 of 4 done)"
    assert bj.progress_note("\nsecond line", None, 5) == "(5 remaining)"
    huge = 10 ** 400
    assert len(bj.progress_note("rows", huge)) == 300


async def test_a_progress_note_does_not_touch_the_turn_end_guard(job):
    """The note is a note: no rule of the guard reads whether the job
    reported."""
    import tools
    rec, channel = job
    await tools.report_job_progress.handler({"summary": "x", "done": 1})
    assert rec.id not in bj._states
    assert not hasattr(bj, "note_progress")


async def test_progress_needs_no_counts_and_no_batch(job):
    import tools
    rec, channel = job
    assert _payload(await tools.report_job_progress.handler(
        {"summary": "reading the ledger"}))["ok"] is True
    assert rec.origin["job"]["last_summary"] == "reading the ledger"
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
