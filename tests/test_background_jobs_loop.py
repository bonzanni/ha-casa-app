"""``report_job_progress`` on a live job record: argument checks, the line's
format, what is persisted, who holds the tool, and the refusal outside a live
job. The turn-end guard that reads the reported progress is pinned in
``test_job_turn_guard.py``."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

import background_jobs as jobs
import tools
from engagement_registry import EngagementRegistry

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def result(subtype="success"):
    return ResultMessage(subtype, 1, 1, subtype != "success", 1, "session")


def text_frame(text):
    return AssistantMessage(content=[TextBlock(text=text)], model="test")


def payload(reply):
    return json.loads(reply["content"][0]["text"])


@pytest.fixture
async def job(tmp_path):
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "jobs.json"))
    decl = jobs.JobDecl("sample:work", "sample", "work", "sample:work", "Process rows", None)
    rec = await reg.create(
        "specialist", "worker", "in_casa", "Process rows",
        {"role": "assistant", "channel": "telegram", "_tg_bot": 111, "chat_id": "100", "user_id": 77,
         "job": jobs.initial_job_state(decl)},
        tools_allowed=tools.SPECIALIST_CASA_GRANTS + jobs.JOB_CASA_GRANTS)
    channel = MagicMock()
    cm = MagicMock()
    cm.get.return_value = channel
    from agent_log_double import RecordingLogs
    channel.logs = RecordingLogs()
    tools.init_tools(channel_manager=cm, bus=MagicMock(),
                     specialist_registry=MagicMock(), mcp_registry=MagicMock(),
                     trigger_registry=MagicMock(), engagement_registry=reg,
                     agent_logs=channel.logs)
    jobs.configure(jobs.JobRunner(registry=reg))
    yield reg, rec, channel
    jobs.configure(None)


async def _report(rec, args):
    token = tools.engagement_var.set(rec)
    try:
        return payload(await tools.report_job_progress.handler(args))
    finally:
        tools.engagement_var.reset(token)


@pytest.mark.parametrize("args", [
    {"done": True}, {"remaining": -1}, {"done": 1.5}, {"remaining": False},
    {"summary": None}, {"summary": 3}])
async def test_progress_rejects_invalid_arguments(job, args):
    reg, rec, channel = job
    reply = await _report(rec, {"summary": "work", **args})
    assert reply == {"ok": False, "kind": "invalid_arguments"}
    assert channel.logs.lines == []
    assert rec.origin["job"]["last_summary"] is None
    assert jobs._state(rec.id).progress is False


async def test_progress_format_persistence_and_grant(job, tmp_path):
    reg, rec, channel = job
    reply = await _report(rec, {"summary": "x" * 310 + "\nnot included",
                                "done": 0, "remaining": 9})
    assert reply == {"ok": True}
    assert channel.logs.texts() == ["📊 " + "x" * 300 + " · 0 done · 9 left"]
    reloaded = EngagementRegistry(tombstone_path=str(tmp_path / "jobs.json"))
    await reloaded.load()
    assert reloaded.get(rec.id).origin["job"]["last_summary"] == "x" * 300
    assert "report_job_progress" not in {
        t.name for t in tools.select_casa_tools(frozenset(tools.SPECIALIST_CASA_GRANTS))}
    assert "report_job_progress" in {
        t.name for t in tools.select_casa_tools(frozenset(jobs.JOB_CASA_GRANTS))}


@pytest.mark.parametrize("context", ["none", "interactive", "terminal"])
async def test_progress_outside_live_job(job, context):
    reg, rec, channel = job
    if context == "interactive":
        rec.origin.pop("job")
    if context == "terminal":
        await reg.mark_cancelled(rec.id)
    reply = await _report(None if context == "none" else rec, {"summary": "work"})
    assert reply == {"ok": False, "kind": "not_a_job"}
