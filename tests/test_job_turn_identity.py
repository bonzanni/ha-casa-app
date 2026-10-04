"""#1033 + #1165: a job turn's identity — the batch its progress report counts
for, and the cid its log lines carry.

Both are read where production reads them: the in-process tool handler and the
SDK's tool-callback lines run in a task the SDK reader spawns from the context
it snapshotted at the client's ``__aenter__``, not in the turn task. The base
harness ``Client`` runs script callables inline in the turn task, so a fix
carried on a per-turn ContextVar would pass there and fail in production;
``ReaderClient`` below dispatches them the production way.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging

import pytest
from claude_agent_sdk import ClaudeAgentOptions

import background_jobs as jobs
import log_cid
import tools
from log_cid import cid_var
# #898: install_logging is process-global; the guard fails a test here that
# leaves logging state altered. Autouse applies only where the name is imported.
from logging_state import (  # noqa: F401 — autouse where imported
    casa_logging_guard,
    casa_logging_restored,
)
from sdk_client_pool import _CidBox
from test_background_jobs_loop import (  # noqa: F401 — harness is a fixture
    Client, ClosableClient, harness, payload, result, text_frame)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


async def report(summary, progressed):
    """A progress report that does not assert success: a refused report must
    show up in the verdict, not as a failed turn."""
    return payload(await tools.report_job_progress.handler(
        {"summary": summary, "progressed": progressed}))


def rep(summary, progressed, sink=None):
    async def go():
        reply = await report(summary, progressed)
        if sink is not None:
            sink.append(reply)
    return go


class Detached:
    """A script step the reader dispatches and does NOT wait for: its turn's
    response stream ends while it is still running."""

    def __init__(self, fn):
        self.fn = fn


class ReaderClient(Client):
    """Runs every script callable in a task created from the context captured
    at ``__aenter__`` — the SDK reader's dispatch — never in the turn task."""

    def __init__(self):
        super().__init__()
        self.saved = None
        self.detached = []

    async def __aenter__(self):
        self.saved = contextvars.copy_context()
        return await super().__aenter__()

    async def receive_response(self):
        loop = asyncio.get_running_loop()
        for item in self.current:
            if isinstance(item, Exception):
                raise item
            if isinstance(item, Detached):
                self.detached.append(
                    loop.create_task(item.fn(), context=self.saved.copy()))
            elif callable(item):
                await loop.create_task(item(), context=self.saved.copy())
            else:
                yield item


def batch_lines(h, text):
    return h.topic().count(text)


async def _reply_after_stalled_batch(h):
    """Batch 1 reports no progress and holds; an operator message queues; the
    reply reports progress; batch 2 captures the judgment of batch 1."""
    h.channel.chat_id = 77  # the operator's own message: no clearance clamp
    entered, release = asyncio.Event(), asyncio.Event()
    seen = {}

    async def hold():
        entered.set()
        await release.wait()

    async def capture():
        seen.update(h.rec.origin["job"])
    h.client.scripts = [
        [text_frame("Working"), rep("batch stalled", False), hold, result()],
        [text_frame("Answer"), rep("reply moved", True), result()],
        [capture, h.complete, result()]]
    await h.start()
    await asyncio.wait_for(entered.wait(), 5)
    await h.operator("How is it going?")
    release.set()
    await h.drain()
    return seen


class TestReportCreditsOnlyItsOwnBatch:
    """INV-BGJOB-002: the LAST report OF THAT BATCH decides it — a report made
    by any other turn of the job decides nothing and is not posted as a batch's."""

    async def test_reply_report_credits_stalled_batch(self, harness):
        h = harness
        seen = await _reply_after_stalled_batch(h)
        assert seen["stuck"] == 1, seen
        assert len(h.batches()) == 2
        assert batch_lines(h, "📊 Batch 1: reply moved") == 0
        assert "Turn failed" not in h.topic()

    async def test_reader_reply_does_not_credit_batch(self, harness):
        h = harness
        h.client = ReaderClient()
        seen = await _reply_after_stalled_batch(h)
        assert seen["stuck"] == 1, seen
        assert batch_lines(h, "📊 Batch 1: reply moved") == 0
        assert "Turn failed" not in h.topic()

    async def test_replies_keep_a_stalled_job_alive(self, harness):
        h = harness
        h.channel.chat_id = 77
        h.rec.origin["job"]["batches"] = 4
        entered = [asyncio.Event() for _ in range(3)]
        release = [asyncio.Event() for _ in range(3)]

        def batch(i):
            async def hold():
                entered[i].set()
                await release[i].wait()
            return [rep(f"batch {i + 1} stalled", False), hold, result()]
        scripts = []
        for i in range(3):
            scripts.append(batch(i))
            scripts.append([text_frame(f"Answer {i}"), rep("reply moved", True), result()])
        scripts.append([text_frame("Batch four"), result()])
        h.client.scripts = scripts
        await h.start()
        for i in range(3):
            await asyncio.wait_for(entered[i].wait(), 5)
            await h.operator("How is it going?")
            release[i].set()
        await h.drain()
        assert len(h.batches()) == 3, (h.batches(), h.rec.origin["job"])
        assert batch_lines(h, "no progress in 3 consecutive batches") == 1
        assert len(h.bot.closed) == 1
        assert "Turn failed" not in h.topic()

    async def test_launch_turn_credits_batch_one(self, harness):
        h = harness
        seen = {}

        async def capture():
            seen.update(h.rec.origin["job"])
        h.client.scripts = [[text_frame("Starting"), rep("ack", True), result()],
                            [text_frame("silent batch"), result()],
                            [capture, h.complete, result()]]
        await h.driver.start(h.rec, prompt="Acknowledge the job",
                             options=ClaudeAgentOptions())
        await jobs.job_after_turn(h.rec, h.channel)
        await h.drain()
        assert seen["stuck"] == 1, seen
        assert batch_lines(h, "📊 Batch 0: ack") == 0
        assert "Turn failed" not in h.topic()

    async def test_downgraded_batch_then_member_reply(self, harness, monkeypatch):
        """#1166's shape: batch 1's own report is refused by the rebuild fence;
        the member's message, re-sent into the fresh session, must not decide
        batch 1 either."""
        h = harness
        note = "[Context reset]"
        clients = [ClosableClient(), ClosableClient()]
        made = []

        def factory(options):
            made.append(clients[len(made)])
            return made[-1]
        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", factory)
        monkeypatch.setattr(tools, "build_engagement_resume_options",
                            lambda rec, sid: ClaudeAgentOptions(resume=sid))

        async def rebuild(rec):
            await h.driver.invalidate_session(rec)
            await h.driver.open_fresh(rec)
            return note
        h.channel._engagement_context_rebuilder = rebuild
        h.rec.origin["_origin_clearance"] = "private"
        old, fresh = clients
        h.client = old
        entered, release = asyncio.Event(), asyncio.Event()
        batch_two, hold_two = asyncio.Event(), asyncio.Event()
        late, seen = [], {}

        async def hold():
            entered.set()
            await release.wait()

        async def second():
            seen.update(h.rec.origin["job"])
            batch_two.set()
            await hold_two.wait()
        old.scripts = [[text_frame("Working"), hold,
                        rep("batch stalled", False, late), result()]]
        fresh.scripts = [[text_frame("Answer"), rep("reply says moved", True, late), result()],
                         [second, result()], [h.complete, result()]]
        await h.start()
        await asyncio.wait_for(entered.wait(), 5)
        await h.operator("Correction")  # user 77 is not the operator (chat 100)
        release.set()
        for _ in range(2000):
            if batch_two.is_set() or h.rec.status not in ("active", "idle"):
                break
            await asyncio.sleep(.001)
        old_prompts, fresh_prompts = list(old.prompts), list(fresh.prompts)
        hold_two.set()
        await h.drain()
        assert batch_two.is_set(), (h.rec.status, h.topic())
        assert len(made) == 2
        assert old_prompts == ["Acknowledge the job", jobs.batch_prompt(1, "Process rows")]
        assert fresh_prompts == [f"{note}\n\nCorrection", jobs.batch_prompt(2, "Process rows")]
        assert late[0].get("kind") == "engagement_context_rebuilding"
        assert seen["stuck"] == 1, seen
        assert batch_lines(h, "📊 Batch 1: reply says moved") == 0

    async def test_late_report_does_not_write_next_batch(self, harness):
        """A batch's report that is still posting when its turn has ended (the
        CLI abandoned the call) must not land in the NEXT batch's verdict."""
        h = harness
        h.client = ReaderClient()
        posting, release_post = asyncio.Event(), asyncio.Event()
        real_send = h.bot.send_message

        async def send_message(**kw):
            if str(kw.get("text", "")).startswith("📊 Batch 1"):
                posting.set()
                await release_post.wait()
            return await real_send(**kw)
        h.bot.send_message = send_message
        advanced, stuck = [], []

        async def started_report():
            await asyncio.wait_for(posting.wait(), 5)

        async def batch_two():
            advanced.append(h.rec.origin["job"]["advanced"])
            release_post.set()
            await asyncio.gather(*h.client.detached)
            advanced.append(h.rec.origin["job"]["advanced"])

        async def batch_three():
            stuck.append(h.rec.origin["job"]["stuck"])
        h.client.scripts = [
            [Detached(rep("batch one moved", True)), started_report, result()],
            [batch_two, result()],
            [batch_three, h.complete, result()]]
        await h.start()
        await h.drain()
        assert advanced + stuck == [False, False, 1], (advanced, stuck)
        assert batch_lines(h, "📊 Batch 1: batch one moved") == 1
        assert "Turn failed" not in h.topic()


async def test_reader_batch_report_is_credited(harness):
    """The positive control for the reader-dispatched cases: a batch's own
    report, made the production way, IS its verdict (green at the base; it
    stops a fix that refuses every report, or reads a per-turn ContextVar the
    reader cannot see, from passing the cases above)."""
    h = harness
    h.client = ReaderClient()
    seen = {}

    async def capture():
        seen.update(h.rec.origin["job"])
    h.client.scripts = [[text_frame("Working"), rep("batch one moved", True), result()],
                        [capture, h.complete, result()]]
    await h.start()
    await h.drain()
    assert seen["stuck"] == 0, seen
    assert batch_lines(h, "📊 Batch 1: batch one moved") == 1


# ---------------------------------------------------------------------------
# #1165: every engagement turn logs under a cid of its own
# ---------------------------------------------------------------------------

ENGAGING = "aaaaaaaa"   # the resident turn that started the job
LATER = "bbbbbbbb"      # the resident's next, unrelated turn


class _Ties(logging.Handler):
    """Collects the driver's per-turn tie lines (INFO, ``turn cid=``)."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.records = []

    def emit(self, record):
        if "turn cid=" in record.getMessage():
            self.records.append(record)


@pytest.fixture
def ties():
    with casa_logging_restored():
        log_cid.install_logging()
        handler = _Ties()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            yield handler
        finally:
            root.removeHandler(handler)


def observe(h, seen, tag):
    """Record, from wherever the client runs this step, the cid a log line
    would carry — the contextvar AND a real LogRecord made through the
    installed factory — and that the engagement's turn lock is held."""
    async def go():
        record = logging.getLogger("tool").makeRecord(
            "tool", logging.INFO, __file__, 1, "probe", (), None)
        seen.append((tag, str(cid_var.get()), record.cid,
                     h.driver.turn_in_progress(h.rec.id)))
    return go


def assert_ties(h, ties, cids, batches):
    """Exactly one tie line per executed turn, under that turn's cid, naming
    the engagement, the batch and the engaging cid."""
    assert [r.cid for r in ties.records] == cids, [r.getMessage() for r in ties.records]
    for record, batch in zip(ties.records, batches):
        message = record.getMessage()
        assert h.rec.id[:8] in message, message
        assert f"batch={batch}" in message, message
        assert f"engaged_by={ENGAGING}" in message, message


class TestEngagementTurnCid:
    """Declared (D34): every in_casa engagement turn logs under a cid minted for
    that turn — turn-task lines throughout, reader-task lines while it holds the
    engagement's turn lock — tied by one line to the engagement."""

    async def test_turn_cids_ignore_engaging_box(self, harness, ties):
        h = harness
        h.rec.origin["cid"] = ENGAGING
        h.rec.origin["job"]["batches"] = 2
        box = _CidBox()
        box.value = ENGAGING
        cid_var.set(box)  # the resident read task's binding when start_job runs
        seen = []
        h.client.scripts = [[text_frame("Start"), observe(h, seen, "launch"), result()],
                            [observe(h, seen, "b1"), result()],
                            [observe(h, seen, "b2"), result()]]
        await h.driver.start(h.rec, prompt="Acknowledge", options=ClaudeAgentOptions())
        assert cid_var.get() is box  # the opener's own binding is restored
        box.value = LATER
        await jobs.job_after_turn(h.rec, h.channel)
        await h.drain()
        assert [s[0] for s in seen] == ["launch", "b1", "b2"]
        assert all(s[3] for s in seen), seen
        assert all(s[1] == s[2] for s in seen), seen
        cids = [s[1] for s in seen]
        assert not set(cids) & {ENGAGING, LATER, "-"}, cids
        assert len(set(cids)) == 3, cids
        assert_ties(h, ties, cids, ["-", "1", "2"])

    async def test_batch_cid_from_empty_context(self, harness, ties):
        h = harness
        h.rec.origin["cid"] = ENGAGING
        h.rec.origin["job"]["batches"] = 2
        seen = []
        h.client.scripts = [[text_frame("Start"), result()],
                            [observe(h, seen, "b1"), result()],
                            [observe(h, seen, "b2"), result()]]
        await h.driver.start(h.rec, prompt="Acknowledge", options=ClaudeAgentOptions())

        async def from_sweep():
            await jobs.start_next_batch(h.rec, h.channel)
        # A batch started by the sweep, at boot or by a scheduled trigger.
        await asyncio.get_running_loop().create_task(
            from_sweep(), context=contextvars.Context())
        await h.drain()
        assert [s[0] for s in seen] == ["b1", "b2"]
        assert all(s[1] == s[2] and s[1] != "-" for s in seen), seen
        assert seen[0][1] != seen[1][1], seen
        assert [r.cid for r in ties.records][1:] == [s[1] for s in seen]

    async def test_reader_records_follow_current_turn(self, harness, ties):
        h = harness
        h.rec.origin["cid"] = ENGAGING
        h.rec.origin["job"]["batches"] = 2
        h.client = ReaderClient()
        box = _CidBox()
        box.value = ENGAGING
        cid_var.set(box)
        seen = []
        h.client.scripts = [[text_frame("Start"), observe(h, seen, "launch"), result()],
                            [observe(h, seen, "b1"), result()],
                            [observe(h, seen, "b2"), result()]]
        await h.driver.start(h.rec, prompt="Acknowledge", options=ClaudeAgentOptions())
        box.value = LATER
        await jobs.job_after_turn(h.rec, h.channel)
        await h.drain()
        assert [s[0] for s in seen] == ["launch", "b1", "b2"]
        assert all(s[3] for s in seen), seen  # read while the turn holds the lock
        assert all(s[1] == s[2] for s in seen), seen
        cids = [s[1] for s in seen]
        assert len(set(cids)) == 3 and "-" not in cids, cids
        assert_ties(h, ties, cids, ["-", "1", "2"])
