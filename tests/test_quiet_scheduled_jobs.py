"""#1301 — a job declaring ``quietWhenScheduled`` that the scheduler started runs quietly:
no topic, no acknowledgement, no progress line, and a completed end told nowhere. Its
plugin's delivered slots still reach the operator, a cancelled or failed end is still
told, and every other run is unchanged (INV-BGJOB-010)."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import agent
import background_jobs as jobs
import tools
from authz_grants import GrantIdentity, resolve_grant_identity
from channels.telegram import _handler_lock_key
from plugin_store import StoreError
from test_background_jobs_declaration import _job
from test_start_job import Client, DECL, drain_launch_turns, runtime  # noqa: F401 — the real launch runtime

pytestmark = pytest.mark.asyncio

QUIET = replace(DECL, quiet_when_scheduled=True)


def _origin(*, scheduled: bool) -> dict:
    origin = {"role": "assistant", "execution_role": "assistant",
              "channel": "telegram", "chat_id": "1", "user_id": "7",
              "cid": "test", "user_name": "operator"}
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

async def test_a_quiet_scheduled_run_opens_no_topic_and_posts_nothing(runtime):
    rec = await _launch(runtime, QUIET, scheduled=True)
    assert rec.topic_id is None and jobs.is_quiet_run(rec)
    assert rec.status == "active"
    runtime.bot.create_forum_topic.assert_not_awaited()
    # the launch turn ran (its acknowledgement was produced) and nothing was posted
    assert Client.instances and Client.instances[0].prompts
    runtime.bot.send_message.assert_not_awaited()
    runtime.bot.edit_message_text.assert_not_awaited()


async def test_a_quiet_runs_batch_turn_and_progress_post_nothing(runtime):
    """A later turn of a quiet run (here a continuation written into its
    session) and its progress line post nothing; the job goes on."""
    rec = await _launch(runtime, QUIET, scheduled=True)
    runtime.channel._engagement_driver = runtime.driver
    assert await runtime.channel.deliver_system_turn(rec, "Continue the job.")
    await drain_launch_turns()
    assert len(Client.instances) == 1 and len(Client.instances[0].prompts) == 2
    assert Client.instances[0].prompts[1].endswith("Continue the job.")
    # the withheld words are not a lost or undelivered turn: the job goes on
    assert runtime.registry.get(rec.id).status == "active"
    await tools._post_engagement_notice(runtime.channel, rec, "📊 matched 3")
    await runtime.channel._post_engagement_notice(rec, '↻ Resuming "x" after a restart.')
    runtime.bot.send_message.assert_not_awaited()
    runtime.bot.edit_message_text.assert_not_awaited()


@pytest.mark.parametrize("decl,scheduled", [(QUIET, False), (DECL, True), (DECL, False)])
async def test_every_other_run_opens_its_topic_as_today(runtime, decl, scheduled):
    rec = await _launch(runtime, decl, scheduled=scheduled)
    assert rec.topic_id == 42 and not jobs.is_quiet_run(rec)
    runtime.bot.create_forum_topic.assert_awaited_once()
    # the launch acknowledgement streams into the topic
    assert any(c.kwargs.get("message_thread_id") == 42
               for c in runtime.bot.send_message.await_args_list)


async def test_a_topic_send_without_a_topic_refuses_rather_than_post_to_general(runtime):
    for send in (runtime.channel.send_to_topic, runtime.channel.send_to_topic_rich,
                 runtime.channel.send_response_to_topic):
        with pytest.raises(ValueError):
            await send(None, "text")
    with pytest.raises(ValueError):
        await runtime.channel.close_topic(None)
    runtime.bot.send_message.assert_not_awaited()
    runtime.bot.close_forum_topic.assert_not_awaited()


async def test_topic_less_records_never_share_a_handler_lock():
    a = SimpleNamespace(id="a", topic_id=None)
    b = SimpleNamespace(id="b", topic_id=None)
    t = SimpleNamespace(id="c", topic_id=42)
    assert _handler_lock_key(a) != _handler_lock_key(b)
    assert _handler_lock_key(t) == 42  # the key a topic message resolves its record by


# --- the plugin's delivered slots -------------------------------------------------------

def _identity(rec):
    token_o = agent.origin_var.set({"role": "finance", "execution_role": "finance",
                                    "channel": "telegram", "source": "telegram",
                                    "message_type": "channel_in", "chat_id": 1,
                                    "user_id": 7})
    token_e = tools.engagement_var.set(rec)
    try:
        return resolve_grant_identity("finance")
    finally:
        tools.engagement_var.reset(token_e)
        agent.origin_var.reset(token_o)


async def test_a_quiet_run_keeps_the_operators_grant_identity():
    rec = SimpleNamespace(id="job-q", kind="specialist", status="active", topic_id=None,
                          role_or_type="finance",
                          origin={"chat_id": 1, "user_id": 7,
                                  "job": {"quiet": True}})
    identity, why = _identity(rec)
    assert why is None
    assert identity == GrantIdentity(7, 1, "finance", "", "job-q", target_role="finance")
    # a topic-less record that is not a quiet run gets none, as before
    rec.origin["job"] = {}
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
    runtime.bot.close_forum_topic.assert_not_awaited()


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
    rec = SimpleNamespace(id="job-q", topic_id=None)
    refusal = jobs._job_busy_refusal("ledger", "ledger:classify", "Classify entries", rec)
    assert "topic" not in refusal["message"] and "topic_id" not in refusal
