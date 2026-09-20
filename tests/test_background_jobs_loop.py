"""Background batches through the real driver, registry and Telegram owners."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock

import agent
import background_jobs as jobs
import tools
from bus import MessageBus
from channels import ChannelManager
from channels.telegram import TelegramChannel
from drivers.in_casa_driver import InCasaDriver
from engagement_registry import EngagementRegistry
from error_kinds import ApiErrorTurn, ErrorKind
from specialist_limits import SpecialistLimiter

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def result(subtype="success"):
    return ResultMessage(subtype, 1, 1, subtype != "success", 1, "session")


def text_frame(text):
    return AssistantMessage(content=[TextBlock(text=text)], model="test")


def payload(reply):
    return json.loads(reply["content"][0]["text"])


class Bot:
    def __init__(self):
        self.posts = []
        self.edits = []
        self.closed = []
        self.slow_text = None
        self.edit_entered = asyncio.Event()
        self.release_edit = asyncio.Event()

    async def send_message(self, **kw):
        self.posts.append(kw)
        return SimpleNamespace(message_id=len(self.posts))

    async def edit_message_text(self, **kw):
        if kw["text"] == self.slow_text:
            self.edit_entered.set()
            await self.release_edit.wait()
        self.edits.append(kw)
        return True

    async def close_forum_topic(self, **kw):
        self.closed.append(kw)
        return True

    async def edit_forum_topic(self, **kw):
        return True

    async def get_me(self):
        return SimpleNamespace(id=42, username="casabot")


class Client:
    def __init__(self):
        self.scripts = []
        self.prompts = []
        self.current = None
        self.closed = False

    async def __aenter__(self):
        self.closed = False
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        self.closed = True

    async def query(self, prompt):
        assert not self.closed
        self.prompts.append(prompt)
        assert self.scripts, f"unexpected turn: {prompt}"
        self.current = self.scripts.pop(0)

    async def receive_response(self):
        script = self.current
        for item in script:
            if isinstance(item, Exception):
                raise item
            if callable(item):
                await item()
            else:
                yield item


class Harness:
    async def start(self):
        self.client.scripts.insert(0, [text_frame("Starting the job"), result()])
        await self.driver.start(self.rec, prompt="Acknowledge the job", options=ClaudeAgentOptions())
        await jobs.job_after_turn(self.rec, self.channel)

    async def drain(self):
        async def finish():
            for _ in range(100):
                tasks = list(self.channel._turn_tasks | self.channel._inbound_cleanup_tasks
                             | tools._finalize_tail_tasks | self.reg._deferred_persists)
                if not tasks:
                    return
                await asyncio.gather(*tasks)
                await asyncio.sleep(0)
            pytest.fail("tasks did not settle")
        await asyncio.wait_for(finish(), 5)

    async def operator(self, text):
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-1001), text=text,
            message_thread_id=555, from_user=SimpleNamespace(id=77), message_id=900,
            photo=None, document=None, voice=None, audio=None, caption=None,
        )
        await self.channel.handle_update(SimpleNamespace(message=message))

    def topic(self):
        return "\n".join(p["text"] for p in self.bot.posts if p.get("message_thread_id") == 555)

    def resident(self):
        q = self.bus.queues["assistant"]
        messages = []
        while not q.empty():
            messages.append(q.get_nowait()[-1])
        return "\n".join(str(m.content) for m in messages)

    def batches(self):
        return [p for p in self.client.prompts if p.startswith("Batch ")]

    async def report(self, summary="Handled rows", progressed=True, **counts):
        reply = payload(await tools.report_job_progress.handler(
            {"summary": summary, "progressed": progressed, **counts}))
        assert reply == {"ok": True}

    async def complete(self):
        reply = payload(await tools.emit_completion.handler({"text": "All rows handled"}))
        assert reply.get("kind") != "unread_inbound", reply

    def assert_terminal(self, status, detail):
        assert self.rec.status == status
        assert detail in self.topic()
        assert detail in self.resident()
        assert len(self.bot.closed) == 1
        assert self.limiter.in_flight == 0
        assert jobs.turn_owners(self.rec.id) == 0


@pytest_asyncio.fixture
async def harness(tmp_path, monkeypatch, request):
    h = Harness()
    h.bot, h.client, h.bus = Bot(), Client(), MessageBus()
    h.bus.register("assistant")
    h.reg = EngagementRegistry(tombstone_path=str(tmp_path / "jobs.json"), bus=None)
    decl = jobs.JobDecl("sample:work", "sample", "work", "sample:work", "Process rows", None, None, 10)
    h.rec = await h.reg.create(
        getattr(request, "param", "specialist"), "worker", "in_casa", "Process rows",
        {"role": "assistant", "channel": "telegram", "chat_id": "100", "user_id": 77,
         "job": jobs.initial_job_state(decl)}, topic_id=555,
        tools_allowed=tools.SPECIALIST_CASA_GRANTS + jobs.JOB_CASA_GRANTS)
    h.limiter = SpecialistLimiter(2)
    h.rec.permit = h.limiter.try_acquire("worker:engagement")
    h.channel = TelegramChannel(bot=h.bot, chat_id=100, engagement_supergroup_id=-1001)
    h.channel._app = SimpleNamespace(bot=h.bot)
    h.driver = InCasaDriver(
        topic_stream_factory=h.channel.create_topic_stream,
        persist_session_id=h.reg.persist_session_id, record_lookup=h.reg.get,
        begin_turn_delivery=h.reg.begin_turn_delivery)
    h.channel._engagement_registry = h.reg
    h.channel._engagement_driver = h.driver
    h.channel._driver_admit_inbound = lambda r, t: h.driver.admit_inbound(r.id, t)
    h.channel._driver_discharge_inbound = lambda r, t: h.driver.discharge_inbound(r.id, t)
    h.channel._driver_inbound_held = lambda r, t: h.driver.inbound_token_held(r.id, t)
    h.channel._driver_turn_incomplete = lambda r, t: h.driver.followup_turn_incomplete(r.id, t)

    async def send(r, text, *, tg_message_id=None, inbound_token=None):
        await h.driver.send_user_turn(r, text, inbound_token=inbound_token)
    h.channel._driver_send_user_turn = send

    async def cancel(r, *, reason):
        return await tools._finalize_engagement(
            r, outcome="cancelled", text="Cancelled by operator", artifacts=[],
            next_steps=[], driver=h.driver)
    h.channel._finalize_cancel = cancel
    cm = ChannelManager()
    cm.register(h.channel)
    monkeypatch.setattr(tools, "_channel_manager", cm)
    monkeypatch.setattr(tools, "_engagement_registry", h.reg)
    monkeypatch.setattr(tools, "_bus", h.bus)
    monkeypatch.setattr(agent, "active_engagement_driver", h.driver)
    monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", lambda options: h.client)
    yield h
    h.bot.release_edit.set()
    for task in list(h.channel._turn_tasks):
        task.cancel()
    await asyncio.gather(*h.channel._turn_tasks, return_exceptions=True)
    await h.driver.cancel(h.rec)
    await h.drain()


@pytest.mark.parametrize("subtype", ["success", "error_max_turns"])
async def test_launch_batches_and_completion(harness, subtype):
    h = harness
    h.client.scripts = [[text_frame("Batch work"), h.report, result(subtype)],
                        [h.complete, result()]]
    await h.start()
    await h.drain()
    assert h.batches() == [jobs.batch_prompt(1, "Process rows"), jobs.batch_prompt(2, "Process rows")]
    assert "📊 Batch 1: Handled rows" in h.topic()
    h.assert_terminal("completed", "All rows handled")


@pytest.mark.parametrize("messages", [1, 2, 5])
async def test_operator_turns_during_slow_final_edit_respect_cap(harness, messages):
    h = harness
    h.rec.origin["job"]["batches"] = 1
    h.bot.slow_text = "Batch work"
    h.client.scripts = [[text_frame("Batch work"), h.report, result()]] + [
        [text_frame(f"Answer {i}"), result()] for i in range(messages)]
    await h.start()
    await asyncio.wait_for(h.bot.edit_entered.wait(), 5)
    batch_owners = set(h.channel._turn_tasks)
    for i in range(messages):
        await h.operator(f"Message {i}")
    # All operator turns end while the batch still owns its final edit.
    await asyncio.wait_for(asyncio.gather(*(
        h.channel._turn_tasks - batch_owners)), 5)
    assert h.rec.origin["job"]["started"] == 1
    assert h.rec.status == "active"
    h.bot.release_edit.set()
    await h.drain()
    assert h.batches() == [jobs.batch_prompt(1, "Process rows")]
    assert h.client.prompts[2:] == [f"Message {i}" for i in range(messages)]
    h.assert_terminal("error", "reached its limit of 1 batches")
    assert "Last progress: Handled rows" in h.topic()


async def test_operator_precedes_single_next_batch(harness):
    h = harness
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold():
        entered.set()
        await release.wait()
    h.client.scripts = [[text_frame("Working"), hold, h.report, result()],
                        [text_frame("Answer"), result()], [h.complete, result()]]
    await h.start()
    await asyncio.wait_for(entered.wait(), 5)
    await h.operator("Correction")
    assert h.driver.inbound_unread_texts(h.rec.id) == ["Correction"]
    release.set()
    await h.drain()
    assert h.client.prompts[1:] == [jobs.batch_prompt(1, "Process rows"), "Correction",
                                     jobs.batch_prompt(2, "Process rows")]
    assert "Answer" in h.topic()
    h.assert_terminal("completed", "All rows handled")


@pytest.mark.parametrize("failure", ["exception", "api_error", "cutoff", "slow_cutoff"])
async def test_batch_failure_reports_progress(harness, failure):
    h = harness
    ending = [RuntimeError("transport broke")] if failure == "exception" else []
    if failure == "api_error":
        ending = [ApiErrorTurn(ErrorKind.TIMEOUT)]
    h.client.scripts = [[text_frame("Batch work"), h.report, *ending]]
    if failure == "slow_cutoff":
        h.bot.slow_text = "Batch work"
        h.client.scripts.append([text_frame("Answer"), result()])
    await h.start()
    if failure == "slow_cutoff":
        await asyncio.wait_for(h.bot.edit_entered.wait(), 5)
        await h.operator("Correction")
        # Wait for the operator's output while the failed batch's edit is held.
        for _ in range(1000):
            if "Answer" in h.topic() and jobs.turn_owners(h.rec.id) == 1:
                break
            await asyncio.sleep(.001)
        assert "Answer" in h.topic()
        assert len(h.batches()) == 1
        h.bot.release_edit.set()
    await h.drain()
    detail = "a batch failed: RuntimeError" if failure == "exception" else "a batch stopped before finishing"
    if failure == "api_error":
        detail = "a batch failed: timeout"
    assert len(h.batches()) == 1
    resident = h.resident()
    assert detail in resident and "Last progress: Handled rows" in resident
    # assert_terminal consumes the queue, so inspect the remaining surfaces here.
    assert h.rec.status == "error"
    assert detail in h.topic() and "Last progress: Handled rows" in h.topic()
    assert len(h.bot.closed) == 1
    assert h.limiter.in_flight == 0
    assert jobs.turn_owners(h.rec.id) == 0


async def test_cancel_stops_batches_and_reports_progress(harness):
    h = harness
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold():
        entered.set()
        await release.wait()
    h.client.scripts = [[text_frame("Working"), h.report, hold, result()]]
    await h.start()
    await asyncio.wait_for(entered.wait(), 5)
    await h.operator("/cancel")
    release.set()
    await h.drain()
    assert len(h.batches()) == 1
    h.assert_terminal("cancelled", "Last progress: Handled rows")


# A batch is stuck when it reported no progress, or ended without reporting at
# all — never because a count failed to move (#1031).
@pytest.mark.parametrize("silent", [False, True])
async def test_stuck_guard(harness, silent):
    h = harness
    async def report():
        await h.report(progressed=False, remaining=8)
    h.client.scripts = [[text_frame("Working"), *([] if silent else [report]), result()]
                        for _ in range(3)]
    await h.start()
    await h.drain()
    # Three either way: a reported no-progress batch counts from the first one,
    # where the old count rule could not judge batch 1 at all.
    assert len(h.batches()) == 3
    h.assert_terminal("error", "no progress in 3 consecutive batches")
    if not silent:
        assert "Last progress: Handled rows" in h.topic()


async def test_reported_progress_resets_stuck(harness):
    h = harness
    async def stalled(): await h.report(progressed=False)
    async def moved(): await h.report(progressed=True)
    h.client.scripts = [[text_frame("Working"), stalled, result()] for _ in range(2)] + [
        [text_frame("Working"), moved, result()], [h.complete, result()]]
    await h.start()
    await h.drain()
    assert len(h.batches()) == 4
    assert h.rec.origin["job"]["stuck"] == 0
    h.assert_terminal("completed", "All rows handled")


# An upgrade mid-job: the record carries the pre-#1031 keys and no `advanced`.
@pytest.mark.parametrize("old,batch,terminal", [
    # The old rule had credited the in-flight batch: it keeps that credit and
    # batch 2 runs.
    ({"reported": True, "remaining": 7, "prev_remaining": 8, "stuck": 2}, 2, None),
    # It had not: the batch is stuck, and this is the third one.
    ({"reported": False, "remaining": 8, "prev_remaining": 8, "stuck": 2}, None,
     "no progress in 3 consecutive batches"),
])
async def test_legacy_job_state_is_judged_under_the_rule_it_ran_under(
        harness, old, batch, terminal):
    h = harness
    job = h.rec.origin["job"]
    job.pop("advanced")
    job.update(started=1, judged=0, **old)
    h.client.scripts = [[text_frame("Working"), h.report, result()], [h.complete, result()]]
    await h.start()
    await h.drain()
    if terminal:
        assert h.batches() == []
        h.assert_terminal("error", terminal)
    else:
        assert h.batches()[:1] == [jobs.batch_prompt(batch, "Process rows")]
        assert h.rec.origin["job"]["stuck"] == 0


# A launch-time record from the old version has no in-flight batch to judge:
# the missing key must not crash the batch it is about to start.
async def test_legacy_job_state_before_any_batch_starts_batch_one(harness):
    h = harness
    job = h.rec.origin["job"]
    job.pop("advanced")
    job.update(reported=False, remaining=None, prev_remaining=None)
    h.client.scripts = [[text_frame("Working"), h.report, result()], [h.complete, result()]]
    await h.start()
    await h.drain()
    assert h.batches()[:1] == [jobs.batch_prompt(1, "Process rows")]
    assert h.rec.origin["job"]["started"] >= 1


# Two reports in one batch: the last one decides, and it is the line the
# operator is left looking at.
@pytest.mark.parametrize("last,stuck", [(True, 0), (False, 1)])
async def test_the_last_report_of_a_batch_decides_it(harness, last, stuck):
    h = harness
    async def two():
        await h.report("first", progressed=not last)
        await h.report("second", progressed=last)
    h.client.scripts = [[text_frame("Working"), two, result()], [h.complete, result()]]
    await h.start()
    await h.drain()
    assert h.rec.origin["job"]["stuck"] == stuck
    # The lines carry the summaries only: no posted line claims a verdict that
    # the batch's later report would contradict.
    topic = h.topic()
    assert "no progress" not in topic
    assert "first" in topic and "second" in topic


# The #1031 red case: counts a job cannot measure (here a constant 0 left) must
# never end a job whose batches report progress.
async def test_unchanging_counts_do_not_end_a_progressing_job(harness):
    h = harness
    async def report(): await h.report(done=0, remaining=0)
    h.client.scripts = [[text_frame("Working"), report, result()] for _ in range(4)] + [
        [h.complete, result()]]
    await h.start()
    await h.drain()
    assert len(h.batches()) == 5
    assert h.rec.origin["job"]["stuck"] == 0
    h.assert_terminal("completed", "All rows handled")


@pytest.mark.parametrize("args", [
    {"progressed": True, "done": True}, {"progressed": True, "remaining": -1},
    {"progressed": True, "done": 1.5}, {"progressed": True, "remaining": False},
    # `progressed` carries the whole judgment: absent or fuzzy is never progress.
    {}, {"progressed": "yes"}, {"progressed": 1}, {"progressed": None}])
async def test_progress_rejects_invalid_arguments(harness, args):
    h = harness
    token = tools.engagement_var.set(h.rec)
    try:
        reply = payload(await tools.report_job_progress.handler({"summary": "work", **args}))
    finally:
        tools.engagement_var.reset(token)
    assert reply == {"ok": False, "kind": "invalid_arguments"}
    assert h.bot.posts == []
    assert not h.rec.origin["job"]["advanced"]


async def test_progress_format_persistence_and_grant(harness, tmp_path):
    h = harness
    h.rec.origin["job"]["started"] = 2
    token = tools.engagement_var.set(h.rec)
    try:
        await h.report("x" * 310 + "\nnot included", done=0, remaining=9)
    finally:
        tools.engagement_var.reset(token)
    assert h.topic() == "📊 Batch 2: " + "x" * 300 + " · 0 done · 9 left"
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "jobs.json"), bus=None)
    await reg.load()
    persisted = reg.get(h.rec.id).origin["job"]
    assert persisted["advanced"]
    assert persisted["last_summary"] == "x" * 300
    assert "report_job_progress" not in {t.name for t in tools.select_casa_tools(frozenset(tools.SPECIALIST_CASA_GRANTS))}
    assert "report_job_progress" in {t.name for t in tools.select_casa_tools(frozenset(jobs.JOB_CASA_GRANTS))}


@pytest.mark.parametrize("context", ["none", "interactive", "terminal"])
async def test_progress_outside_live_job(harness, context):
    h = harness
    rec = h.rec
    if context == "interactive": rec.origin.pop("job")
    if context == "terminal": await h.reg.mark_cancelled(rec.id)
    token = tools.engagement_var.set(None if context == "none" else rec)
    try:
        reply = payload(await tools.report_job_progress.handler({"summary": "work"}))
    finally:
        tools.engagement_var.reset(token)
    assert reply == {"ok": False, "kind": "not_a_job"}
    assert h.bot.posts == []


async def test_restart_resumes_next_batch(harness, monkeypatch, tmp_path):
    from casa_core import _resume_background_jobs
    h = harness
    h.rec.origin["job"].update(started=1, reported=True, last_summary="First batch")
    await h.reg.persist_origin(h.rec.id)
    await h.reg.persist_session_id(h.rec.id, "session")
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "jobs.json"), bus=None)
    await reg.load()
    h.rec.permit.release()
    h.reg, h.rec = reg, reg.get(h.rec.id)
    assert h.rec.status == "idle"
    h.channel._engagement_registry = reg
    monkeypatch.setattr(tools, "_engagement_registry", reg)
    h.driver._record_lookup = reg.get
    h.driver._begin_turn_delivery = reg.begin_turn_delivery
    # U2 supplies the job-specific options; this unit exercises real resume/delivery.
    monkeypatch.setattr(tools, "build_engagement_resume_options", lambda rec, sid: ClaudeAgentOptions(resume=sid))
    h.client.scripts = [[h.complete, result()]]
    await _resume_background_jobs(reg, h.channel)
    await h.drain()
    assert h.batches() == [jobs.batch_prompt(2, "Process rows")]
    assert '↻ Resuming "Process rows" after a restart.' in h.topic()
    h.assert_terminal("completed", "All rows handled")
