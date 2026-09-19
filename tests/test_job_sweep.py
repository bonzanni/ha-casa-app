"""Job continuation recovery with real persisted records and turn owners."""
import asyncio
import ast
import time
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions

import background_jobs as jobs
import tools
from casa_core import _resume_background_jobs
from engagement_registry import EngagementRegistry
from test_background_jobs_loop import harness, result, text_frame

pytestmark = [pytest.mark.asyncio, pytest.mark.unit,
              pytest.mark.parametrize("harness", ["specialist", "plugin"], indirect=True)]


@pytest.fixture(autouse=True)
def _stalled(monkeypatch):
    """These tests are about what the sweep DOES once a job is stalled, so the
    window is zeroed; `test_progress_timestamp_and_fresh_job_not_swept` restores
    the real value to pin the window itself."""
    monkeypatch.setattr(jobs, "_JOB_STALL_S", 0.0)


async def suspended(h, monkeypatch, failures=0):
    await h.reg.persist_session_id(h.rec.id, "session")
    monkeypatch.setattr(tools, "build_engagement_resume_options",
                        lambda rec, sid: ClaudeAgentOptions(resume=sid))
    original = h.client.__class__.__aenter__
    async def enter(client):
        nonlocal failures
        if failures:
            failures -= 1
            raise RuntimeError("resume unavailable")
        return await original(client)
    monkeypatch.setattr(h.client.__class__, "__aenter__", enter)


async def test_resume_failures_recover_or_tell_once(harness, monkeypatch):
    h = harness
    await suspended(h, monkeypatch, failures=2)
    h.rec.origin["job"]["last_summary"] = "Handled 7 rows"
    for attempt in (1, 2):
        await jobs.sweep_jobs(h.reg, h.channel)
        assert h.rec.status == "active"
        assert h.rec.origin["job"]["stalls"] == attempt
        assert h.rec.origin["job"]["started"] == 0
        assert h.limiter.in_flight == 1
    h.client.scripts = [[h.complete, result()]]
    await jobs.sweep_jobs(h.reg, h.channel)
    await h.drain()
    assert h.rec.origin["job"]["stalls"] == 0
    assert len(h.batches()) == 1
    h.assert_terminal("completed", "All rows handled")


@pytest.mark.parametrize("failure", ["resume", "rebuild"])
async def test_three_failed_continuations_finalize(harness, monkeypatch, failure):
    h = harness
    await suspended(h, monkeypatch, failures=3)
    if failure == "rebuild":
        h.rec.context_rebuild_pending = True
        h.channel._engagement_context_rebuilder = h.driver.open_fresh
    h.rec.origin["job"]["last_summary"] = "Handled 7 rows"
    for attempt in (1, 2, 3):
        await jobs.sweep_jobs(h.reg, h.channel)
        assert h.rec.origin["job"]["stalls"] == attempt
        assert h.rec.status == ("error" if attempt == 3 else "active")
    await h.drain()
    await jobs.sweep_jobs(h.reg, h.channel)
    detail = "could not be continued after 3 attempts"
    assert h.topic().count(detail) == 1
    assert h.bus.queues["assistant"].qsize() == 1
    resident = h.resident()
    assert detail in resident and "Last progress: Handled 7 rows" in resident
    assert "Last progress: Handled 7 rows" in h.topic()
    assert h.rec.terminal_notification_pending
    assert len(h.bot.closed) == 1 and h.limiter.in_flight == 0


async def test_rebuild_failure_recovers(harness, monkeypatch):
    h = harness
    await suspended(h, monkeypatch, failures=1)
    h.rec.context_rebuild_pending = True
    h.channel._engagement_context_rebuilder = h.driver.open_fresh
    await jobs.sweep_jobs(h.reg, h.channel)
    assert h.rec.context_rebuild_pending and h.rec.status == "active"
    assert h.rec.origin["job"]["stalls"] == 1
    h.client.scripts = [[h.complete, result()]]
    await jobs.sweep_jobs(h.reg, h.channel)
    await h.drain()
    assert not h.rec.context_rebuild_pending
    h.assert_terminal("completed", "All rows handled")


@pytest.mark.parametrize("resume_failure", [False, True])
async def test_boot_notice_failure_still_runs_next_batch(harness, monkeypatch, tmp_path, resume_failure):
    h = harness
    await suspended(h, monkeypatch, failures=int(resume_failure))
    h.rec.origin["job"].update(started=1, reported=True)
    h.rec.topic_title = "Worker · Process rows"
    paints = []
    edit_topic = h.bot.edit_forum_topic
    async def record_paint(**kw):
        paints.append(kw)
        return await edit_topic(**kw)
    monkeypatch.setattr(h.bot, "edit_forum_topic", record_paint)
    await h.reg.persist_origin(h.rec.id)
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "jobs.json"), bus=None)
    await reg.load()
    if h.rec.permit is not None:
        h.rec.permit.release()
    h.reg, h.rec = reg, reg.get(h.rec.id)
    h.channel._engagement_registry = reg
    monkeypatch.setattr(tools, "_engagement_registry", reg)
    h.driver._record_lookup = reg.get
    h.driver._begin_turn_delivery = reg.begin_turn_delivery
    send = h.bot.send_message
    async def fail_notice(**kw):
        if "Resuming" in kw.get("text", ""):
            raise RuntimeError("Telegram unavailable")
        return await send(**kw)
    monkeypatch.setattr(h.bot, "send_message", fail_notice)
    h.client.scripts = [[h.complete, result()]]
    await _resume_background_jobs(reg, h.channel)
    if resume_failure:
        assert h.rec.status == "idle" and not h.batches()
        await jobs.sweep_jobs(reg, h.channel)
    await h.drain()
    assert h.batches() == [jobs.batch_prompt(2, "Process rows")]
    assert h.rec.topic_title == "Worker · Process rows"
    assert paints and all("Worker · Process rows" in p["name"] for p in paints if "name" in p)
    h.assert_terminal("completed", "All rows handled")


@pytest.mark.parametrize("previous", [False, True])
async def test_refusal_preserves_all_accounting(harness, monkeypatch, previous):
    h = harness
    await suspended(h, monkeypatch, failures=1)
    job = h.rec.origin["job"]
    if previous:
        job.update(started=1, reported=True, remaining=5, prev_remaining=9, stuck=2)
    else:
        job["batches"] = 1
    before = dict(job)
    assert await jobs.start_next_batch(h.rec, h.channel) is False
    assert job == before
    h.client.scripts = [[h.complete, result()]]
    await jobs.sweep_jobs(h.reg, h.channel)
    await h.drain()
    assert h.batches() == [jobs.batch_prompt(2 if previous else 1, "Process rows")]
    h.assert_terminal("completed", "All rows handled")


async def test_long_batch_and_racing_hook_sweep(harness, monkeypatch, tmp_path):
    h = harness
    await suspended(h, monkeypatch)
    h.rec.origin["job"]["stalls"] = 2
    admitted_after = time.time()
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold():
        entered.set()
        await release.wait()
    h.client.scripts = [[hold, h.report, result()], [h.complete, result()]]
    await asyncio.gather(jobs.sweep_jobs(h.reg, h.channel),
                         jobs.job_after_turn(h.rec, h.channel))
    await asyncio.wait_for(entered.wait(), 5)
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "jobs.json"), bus=None)
    await reg.load()
    persisted = reg.get(h.rec.id).origin["job"]
    assert persisted["started"] == 1 and persisted["stalls"] == 0
    assert persisted["last_advance"] >= admitted_after
    h.rec.origin["job"]["last_advance"] = 1
    before = dict(h.rec.origin["job"])
    await jobs.sweep_jobs(h.reg, h.channel)
    assert h.rec.origin["job"] == before
    assert len(h.batches()) == 1
    release.set()
    await h.drain()
    assert h.batches() == [jobs.batch_prompt(1, "Process rows"),
                           jobs.batch_prompt(2, "Process rows")]
    h.assert_terminal("completed", "All rows handled")


async def test_stopping_refusal_leaves_live_for_boot(harness, monkeypatch):
    h = harness
    await suspended(h, monkeypatch)
    h.channel._stopping = True
    before = dict(h.rec.origin["job"])
    await jobs.sweep_jobs(h.reg, h.channel)
    assert not h.driver.is_alive(h.rec)
    assert await jobs.start_next_batch(h.rec, h.channel) is False
    assert h.rec.origin["job"] == before and h.rec.status == "active"
    h.channel._stopping = False
    h.client.scripts = [[h.complete, result()]]
    await _resume_background_jobs(h.reg, h.channel)
    await h.drain()
    assert '↻ Resuming "Process rows" after a restart.' in h.topic()
    assert h.batches() == [jobs.batch_prompt(1, "Process rows")]
    h.assert_terminal("completed", "All rows handled")


async def test_orphan_uses_funnel(harness):
    h = harness
    h.rec.origin["job"]["last_summary"] = "Handled 7 rows"
    assert await jobs.start_next_batch(h.rec, h.channel) is False
    await h.drain()
    h.assert_terminal("error", "Last progress: Handled 7 rows")
    assert h.rec.terminal_notification_pending


async def test_interactive_retains_two_strikes(harness, monkeypatch):
    h = harness
    h.rec.origin.pop("job")
    await suspended(h, monkeypatch, failures=2)
    assert await h.channel.deliver_system_turn(h.rec, "continue") is False
    assert h.rec.status == "active" and h.rec.origin["_resume_fail_count"] == 1
    assert await h.channel.deliver_system_turn(h.rec, "continue") is False
    assert h.rec.status == "error" and h.rec.origin["_resume_fail_count"] == 2
    assert len(h.bot.closed) == 1


async def test_progress_timestamp_and_fresh_job_not_swept(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_JOB_STALL_S", 180.0)  # the real window
    h = harness
    before = time.time()
    token = tools.engagement_var.set(h.rec)
    try:
        await h.report()
    finally:
        tools.engagement_var.reset(token)
    assert h.rec.origin["job"]["last_advance"] >= before
    await jobs.sweep_jobs(h.reg, h.channel)
    assert h.rec.status == "active" and h.rec.origin["job"]["stalls"] == 0
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "jobs.json"), bus=None)
    await reg.load()
    assert reg.get(h.rec.id).origin["job"]["last_advance"] >= before


async def test_scheduler_registration(harness):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    import casa_core
    tree = ast.parse(Path(casa_core.__file__).read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and any(k.arg == "id" and isinstance(k.value, ast.Constant)
                     and k.value.value == "background_job_sweep" for k in node.keywords)]
    assert len(calls) == 1
    scheduler = AsyncIOScheduler()
    scope = dict(scheduler=scheduler, sweep_jobs=jobs.sweep_jobs,
                 engagement_registry=harness.reg, telegram_channel=harness.channel)
    exec(compile(ast.Expression(calls[0]), casa_core.__file__, "eval"), scope)
    scheduler.start(paused=True)
    try:
        job = scheduler.get_job("background_job_sweep")
        assert job.func is jobs.sweep_jobs and job.args == (harness.reg, harness.channel)
        assert job.trigger.interval.total_seconds() == 60
        assert job.coalesce and job.max_instances == 1 and job.misfire_grace_time == 60
    finally:
        scheduler.shutdown(wait=False)


async def test_stop_during_resume_does_not_count_stall(harness, monkeypatch):
    h = harness
    await suspended(h, monkeypatch)
    enter = h.client.__class__.__aenter__
    async def stop_on_enter(client):
        h.channel._stopping = True
        return await enter(client)
    monkeypatch.setattr(h.client.__class__, "__aenter__", stop_on_enter)
    before = dict(h.rec.origin["job"])
    await jobs.sweep_jobs(h.reg, h.channel)
    assert h.rec.origin["job"] == before
    assert h.rec.status == "active"


@pytest.mark.parametrize("phase", ["launch", "batch"])
async def test_shutdown_boundary_and_disk_restart(harness, monkeypatch, tmp_path, phase):
    h = harness
    entered = asyncio.Event()
    async def hold():
        entered.set()
        await asyncio.Event().wait()
    h.client.scripts = ([[text_frame("Starting"), hold, result()]] if phase == "launch"
                        else [[text_frame("Starting"), result()], [h.report, hold, result()]])
    await h.driver.open(h.rec, options=ClaudeAgentOptions())
    tools._hand_off_launch_turn(h.driver, h.rec, "Acknowledge", h.channel, 555, None)
    await asyncio.wait_for(entered.wait(), 5)
    h.reg.begin_launch_shutdown()
    await h.reg.drain_launches()
    await tools.drain_launch_turns()
    await tools.drain_launch_death_reports()
    app = h.channel._app
    app.updater = None
    await h.channel.stop()
    h.channel._app = app
    await h.drain()
    if phase == "launch":
        assert h.rec.status == "error" and h.rec.origin["error_kind"] == "launch_cancelled"
        assert not h.batches()
        assert "Casa was stopping" in h.topic()
        assert "Casa was stopping" in h.resident()
        assert h.rec.terminal_notification_pending
    else:
        assert h.rec.status == "active"
        assert h.rec.origin["job"]["started"] == 1
    await h.driver.cancel(h.rec)
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "jobs.json"), bus=None)
    await reg.load()
    if h.rec.permit is not None:
        h.rec.permit.release()
    h.reg, h.rec = reg, reg.get(h.rec.id)
    h.channel._engagement_registry = reg
    h.channel._stopping = False
    monkeypatch.setattr(tools, "_engagement_registry", reg)
    h.driver._record_lookup = reg.get
    h.driver._begin_turn_delivery = reg.begin_turn_delivery
    monkeypatch.setattr(tools, "build_engagement_resume_options",
                        lambda rec, sid: ClaudeAgentOptions(resume=sid))
    h.client.scripts = [[h.complete, result()]]
    await _resume_background_jobs(reg, h.channel)
    await h.drain()
    if phase == "launch":
        assert h.rec.status == "error" and not h.batches()
    else:
        assert h.batches() == [jobs.batch_prompt(1, "Process rows"),
                               jobs.batch_prompt(2, "Process rows")]
        h.assert_terminal("completed", "All rows handled")


@pytest.mark.parametrize("session", [False, True])
async def test_launch_resume_gate_keeps_launch_path(harness, monkeypatch, session):
    h = harness
    if session:
        await suspended(h, monkeypatch, failures=2)
    handle = h.reg.register_launch(h.rec.id, asyncio.current_task())
    try:
        if session:
            assert await h.channel.deliver_system_turn(h.rec, "continue") is False
            assert h.rec.status == "active"
        assert await h.channel.deliver_system_turn(h.rec, "continue") is False
        assert h.rec.status == "error"
        assert h.rec.origin["error_kind"] == ("resume_failed" if session else "orphan_no_session")
        assert not h.rec.terminal_notification_pending
        assert h.bus.queues["assistant"].empty()
    finally:
        h.reg.unregister_launch(handle)


async def test_a_job_launching_is_not_swept(harness, monkeypatch):
    """Diff review r1: the sweep saw a job whose acknowledgement turn had not
    started — no turn owner yet — as stalled, and ended it within a minute of
    the operator asking for it. Launch ownership excludes it; a job with no
    advance yet is measured from when its record was created."""
    monkeypatch.setattr(jobs, "_JOB_STALL_S", 180.0)  # the real window
    h = harness
    h.rec.origin["job"]["launched"] = False   # still inside its launch
    assert h.rec.origin["job"]["last_advance"] is None
    handle = h.reg.register_launch(h.rec.id, asyncio.current_task())
    try:
        assert h.reg.launch_in_flight(h.rec.id)
        # Even with the record backdated past the window, the launch owns it.
        h.rec.started_at = time.time() - 10_000
        await jobs.sweep_jobs(h.reg, h.channel)
        assert h.rec.status == "active"
        assert h.rec.origin["job"]["stalls"] == 0
        assert h.batches() == []
    finally:
        h.reg.unregister_launch(handle)
    # A fresh record without a launch handle is still not stale yet.
    h.rec.started_at = time.time()
    await jobs.sweep_jobs(h.reg, h.channel)
    assert h.rec.origin["job"]["stalls"] == 0 and h.batches() == []


async def test_a_job_awaiting_a_failed_rebuild_is_still_swept(harness):
    """Diff review r3: a clearance clamp clears the session pointer of a job
    that HAS started and marks its context for rebuild. A rebuild that fails
    leaves no session, no advance and no owner — the creation guard must not
    exempt that state, or the job waits for a message that never comes."""
    h = harness
    await h.reg.persist_session_id(h.rec.id, "session")   # it had started
    h.rec.sdk_session_id = None                            # the clamp cleared it
    h.rec.context_rebuild_pending = True
    h.rec.origin["job"]["last_advance"] = None
    h.rec.started_at = time.time() - 10_000
    rebuilds = []
    async def rebuild(rec):
        rebuilds.append(rec.id)
        raise RuntimeError("rebuild unavailable")
    h.channel._engagement_context_rebuilder = rebuild
    await jobs.sweep_jobs(h.reg, h.channel)
    assert rebuilds, "the sweep never reached the pending rebuild"
    assert h.rec.status == "active"
    assert h.rec.origin["job"]["stalls"] == 1


async def test_a_cancelled_but_settled_clear_keeps_the_rebuild_flag_cleared(harness):
    """Diff review r6 (both reviewers): the tombstone write settles even under
    cancellation and re-raises the cancellation in preference to its own
    result. Restoring the flag unconditionally (my r5 fold) contradicted a
    write that had SUCCEEDED; the handler follows the settled write."""
    h = harness
    h.rec.context_rebuild_pending = True
    real = h.reg._write_tombstone_locked

    async def settle_then_cancel(*a, **k):
        await real(*a, **k)                       # the write really lands
        raise asyncio.CancelledError()
    h.reg._write_tombstone_locked = settle_then_cancel
    with pytest.raises(asyncio.CancelledError):
        await h.reg.clear_context_rebuild_pending(h.rec.id)
    assert h.reg._last_tombstone_ok is True
    assert h.rec.context_rebuild_pending is False   # memory follows the disk

    # …and a write that did NOT settle keeps the rebuild owed.
    h.rec.context_rebuild_pending = True
    async def fail_then_cancel(*a, **k):
        h.reg._last_tombstone_ok = False
        raise asyncio.CancelledError()
    h.reg._write_tombstone_locked = fail_then_cancel
    with pytest.raises(asyncio.CancelledError):
        await h.reg.clear_context_rebuild_pending(h.rec.id)
    assert h.rec.context_rebuild_pending is True


@pytest.mark.parametrize("state", [
    {"sdk_session_id": ""},                                    # creation not settled
    {"sdk_session_id": None, "context_rebuild_pending": True},  # rebuild failed
    {"sdk_session_id": "session"},                              # ordinary, not advancing
], ids=["no-session", "rebuild-pending", "resumable"])
async def test_no_live_state_leaves_the_sweep_inert(harness, state):
    """Seven review rounds each found a live state the sweep read wrongly and
    then ignored for ever. The property that ends that class is not a better
    reading: it is that NO state is inert. For each, the sweep either continues
    the job, counts a refused attempt toward its own bound, or ends it with a
    telling — never nothing. One record per state (diff review r8: successive
    mutations of a single record let an early terminal state hide the rest)."""
    h = harness
    for attr, value in state.items():
        setattr(h.rec, attr, value)
    h.rec.started_at = time.time() - 10_000
    h.rec.origin["job"].update(last_advance=None, stalls=0)
    before_batches, before_posts = len(h.batches()), len(h.bot.posts)
    await jobs.sweep_jobs(h.reg, h.channel)
    acted = (len(h.batches()) > before_batches                 # continued
             or h.rec.origin["job"]["stalls"] == 1             # attempt counted
             or (h.rec.status == "error"                       # ended, and told
                 and len(h.bot.posts) > before_posts))
    assert acted, f"the sweep did nothing for {state}"


async def test_a_launch_in_flight_is_left_alone_then_recovered(harness):
    """While a launch is enrolled the sweep stays out of its way; once the
    handle is gone — however it went — the job is ordinary work again and the
    sweep reaches it, without anything having had to record that fact."""
    h = harness
    h.rec.started_at = time.time() - 10_000
    h.rec.origin["job"]["last_advance"] = None
    await h.reg.persist_session_id(h.rec.id, "session")
    handle = h.reg.register_launch(h.rec.id, asyncio.current_task())
    await jobs.sweep_jobs(h.reg, h.channel)
    assert h.rec.origin["job"]["stalls"] == 0 and h.batches() == []
    h.reg.unregister_launch(handle)
    await jobs.sweep_jobs(h.reg, h.channel)
    assert h.rec.origin["job"]["stalls"] == 1 or h.batches()


async def test_three_refused_continuations_end_a_job_with_its_progress(harness, monkeypatch):
    """A job that truly cannot be continued is ended by the sweep's own bound,
    with what it had already reported — never left live indefinitely."""
    h = harness
    await h.reg.persist_session_id(h.rec.id, "session")
    h.rec.origin["job"].update(last_advance=None, stalls=0, last_summary="Handled rows")
    monkeypatch.setattr(tools, "build_engagement_resume_options",
                        lambda rec, sid: (_ for _ in ()).throw(RuntimeError("no config")))
    for _ in range(jobs._JOB_MAX_STALLS):
        h.rec.started_at = time.time() - 10_000
        await jobs.sweep_jobs(h.reg, h.channel)
    assert h.rec.status == "error"
    assert "could not be continued" in h.topic()
    assert "Last progress: Handled rows" in h.topic()
