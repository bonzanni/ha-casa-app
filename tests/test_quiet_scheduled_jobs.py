"""#1301 — a job declaring ``quietWhenScheduled`` that the scheduler started runs quietly:
nothing in the direct chat, and a completed end told nowhere. Its plugin's delivered
slots still reach the operator, a cancelled or failed end is still told, its agent log
is written like any job's, and every other run is unchanged (INV-BGJOB-025)."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import agent
import background_jobs as jobs
from agent_log_double import RecordingLogs
from agent_logs import LogKey
import tools
from authz_grants import GrantIdentity, resolve_grant_identity
from plugin_store import StoreError
from test_background_jobs_declaration import _job
from test_start_job import Client, DECL, drain_launch_turns, runtime  # noqa: F401 — the real launch runtime

pytestmark = pytest.mark.asyncio

QUIET = replace(DECL, quiet_when_scheduled=True)


def _origin(*, scheduled: bool) -> dict:
    origin = {"role": "assistant", "execution_role": "assistant",
              "channel": "telegram", "_tg_bot": 111, "chat_id": "1",
              "user_id": "7", "cid": "test", "user_name": "operator"}
    if scheduled:
        origin.update(source="scheduler", message_type="scheduled",
                      _scheduled_job=True)
    else:
        origin["_operator_turn"] = True
    return origin


async def _launch(runtime, decl, *, scheduled: bool):
    host = jobs.JobHost("specialist", "finance", decl, SimpleNamespace(name="ledger"))
    envelope = await tools._start_job_on_host(
        host, "Check the quarter", "", _origin(scheduled=scheduled))
    import json
    result = json.loads(envelope["content"][0]["text"])
    assert result["status"] == "pending", result
    await drain_launch_turns()
    return runtime.registry.get(result["engagement_id"])


# --- the declaration --------------------------------------------------------------------

@pytest.mark.parametrize("value", [1, "true", None, 0])
async def test_the_option_is_strictly_a_boolean(value):
    from plugin_store import manifest_jobs
    with pytest.raises(StoreError) as exc:
        manifest_jobs({"casa": {"jobs": [_job(quietWhenScheduled=value)]}})
    assert exc.value.reason_code == "jobs_invalid"
    assert "quietWhenScheduled" in str(exc.value)


async def test_the_option_reaches_the_declaration():
    from plugin_store import manifest_jobs
    [entry] = manifest_jobs({"casa": {"jobs": [_job(quietWhenScheduled=True)]}})
    assert entry["quietWhenScheduled"] is True
    assert DECL.quiet_when_scheduled is False


@pytest.mark.parametrize("started_by", ["operator", "agent", "scheduled", None])
async def test_only_a_scheduled_start_of_a_quiet_job_is_quiet(started_by):
    state = jobs.initial_job_state(QUIET, started_by=started_by)
    assert (state.get("quiet") is True) is (started_by == "scheduled")
    assert "quiet" not in jobs.initial_job_state(DECL, started_by="scheduled")


# --- the run ----------------------------------------------------------------------------

def _logs(runtime, monkeypatch) -> RecordingLogs:
    logs = RecordingLogs()
    monkeypatch.setattr(tools, "_agent_logs", logs)
    monkeypatch.setattr(runtime.driver, "_agent_logs", logs)
    return logs


async def test_a_quiet_scheduled_run_posts_nothing_and_logs_like_any_job(
        runtime, monkeypatch):
    logs = _logs(runtime, monkeypatch)
    rec = await _launch(runtime, QUIET, scheduled=True)
    assert jobs.is_quiet_run(rec)
    assert rec.status == "active"
    runtime.bot.create_forum_topic.assert_not_awaited()
    # the brief's turn ran and nothing was posted in the direct chat
    assert Client.instances and Client.instances[0].prompts
    runtime.bot.send_message.assert_not_awaited()
    runtime.bot.edit_message_text.assert_not_awaited()
    # its log: the start, then the turn's words
    assert logs.lines[0] == (LogKey("agent", "finance"), DECL.title, "▶ started")
    assert len(logs.words()) >= 1


async def test_a_quiet_runs_later_turn_and_progress_are_logged_not_posted(
        runtime, monkeypatch):
    """A later turn of a quiet run (here a continuation written into its
    session) and its progress line go to its log, never the direct chat; the
    job goes on."""
    logs = _logs(runtime, monkeypatch)
    rec = await _launch(runtime, QUIET, scheduled=True)
    runtime.channel._engagement_driver = runtime.driver
    assert await runtime.channel.deliver_system_turn(rec, "Continue the job.")
    await drain_launch_turns()
    assert len(Client.instances) == 1 and len(Client.instances[0].prompts) == 2
    assert Client.instances[0].prompts[1].endswith("Continue the job.")
    assert runtime.registry.get(rec.id).status == "active"
    token = tools.engagement_var.set(rec)
    try:
        result = await tools.report_job_progress.handler(
            {"summary": "matched 3", "done": 3, "remaining": 1})
    finally:
        tools.engagement_var.reset(token)
    assert '"ok": true' in result["content"][0]["text"]
    assert logs.texts()[-1] == "📊 matched 3 · 3 done · 1 left"
    runtime.bot.send_message.assert_not_awaited()
    runtime.bot.edit_message_text.assert_not_awaited()


@pytest.mark.parametrize("decl,scheduled", [(QUIET, False), (DECL, True), (DECL, False)])
async def test_every_other_run_is_not_quiet_and_opens_no_topic(runtime, decl, scheduled):
    rec = await _launch(runtime, decl, scheduled=scheduled)
    assert not jobs.is_quiet_run(rec)
    runtime.bot.create_forum_topic.assert_not_awaited()


# --- the plugin's delivered slots -------------------------------------------------------

def _identity(rec):
    token_o = agent.origin_var.set({"role": "finance", "execution_role": "finance",
                                    "channel": "telegram", "source": "telegram",
                                    "message_type": "channel_in", "chat_id": 1,
                                    "user_id": 7, "_tg_bot": 111})
    token_e = tools.engagement_var.set(rec)
    try:
        return resolve_grant_identity("finance")
    finally:
        tools.engagement_var.reset(token_e)
        agent.origin_var.reset(token_o)


@pytest.mark.parametrize("job", [{"quiet": True}, {}])
async def test_a_job_keeps_the_operators_grant_identity_without_a_topic(job, monkeypatch):
    """No job has a topic any more: an active job whose origin names the
    operator and their chat binds its protected calls to them, quiet or not."""
    from two_endpoints import build_two_endpoints
    monkeypatch.setattr(tools, "_channel_manager", build_two_endpoints().manager)
    rec = SimpleNamespace(id="job-q", kind="specialist", status="active",
                          role_or_type="finance",
                          origin={"chat_id": 1, "user_id": 7, "_tg_bot": 111,
                                  "job": job})
    identity, why = _identity(rec)
    assert why is None
    assert identity == GrantIdentity(operator_id=7, bot_id=111, chat_id=1,
                                     enforcement_role="finance", artifact_id="",
                                     engagement_id="job-q", target_role="finance")
    # without an operator in its origin it gets none
    rec.origin.pop("user_id")
    assert _identity(rec) == (None, "engagement_unavailable")


# --- the end ----------------------------------------------------------------------------

@pytest.mark.parametrize("outcome", ["completed", "error", "cancelled"])
async def test_a_completed_quiet_run_is_told_nowhere_and_a_failed_one_is_told(
        runtime, monkeypatch, outcome):
    told = []

    async def send(bus, *, complete, **kw):
        told.append(complete)
    monkeypatch.setattr(tools, "send_engagement_outcome", send)
    rec = await _launch(runtime, QUIET, scheduled=True)
    await tools._finalize_engagement(rec, outcome=outcome, text="nothing new",
                                     next_steps=[], driver=runtime.driver)
    rec = runtime.registry.get(rec.id)
    assert rec.status == outcome
    if outcome == "completed":
        assert told == [] and rec.terminal_notification_pending is False
    else:
        assert [c.status for c in told] == ["error"]
        assert rec.terminal_notification_pending is True
    runtime.bot.send_message.assert_not_awaited()
    runtime.bot.create_forum_topic.assert_not_awaited()


async def test_a_completed_run_that_is_not_quiet_is_still_told(runtime, monkeypatch):
    told = []

    async def send(bus, *, complete, **kw):
        told.append(complete)
    monkeypatch.setattr(tools, "send_engagement_outcome", send)
    rec = await _launch(runtime, DECL, scheduled=True)
    await tools._finalize_engagement(rec, outcome="completed", text="done",
                                     next_steps=[], driver=runtime.driver)
    assert [c.status for c in told] == ["ok"]


async def test_a_busy_refusal_for_a_quiet_run_does_not_point_at_a_topic():
    quiet = SimpleNamespace(id="job-q", origin={"job": {"quiet": True}})
    refusal = jobs._job_busy_refusal("ledger", "ledger:classify", "Classify entries", quiet)
    assert refusal["message"] == ("ledger is already running Classify entries on its "
                                  "schedule. Wait for it to finish.")
    assert "topic_id" not in refusal
    # quietness is the record's stamp, not a missing topic: every job lacks one now
    loud = SimpleNamespace(id="job-l", origin={"job": {}})
    refusal = jobs._job_busy_refusal("ledger", "ledger:classify", "Classify entries", loud)
    assert refusal["message"] == (
        "ledger already has a running job: Classify entries. Wait for it to finish, "
        "or cancel it with its Stop key at the keyboard or by asking me.")
    assert "topic" not in refusal["message"] and "topic_id" not in refusal
