"""#1165: every turn of a job's session logs under a cid of its own.

The cid is read where production reads it: in the session's reader (which
runs the frames) and in a task created from the context the SDK reader
snapshotted at the client's ``__aenter__`` — where the SDK runs tool
callbacks. ``ReaderClient`` dispatches its script steps the second way, so a
cid carried on a per-turn ContextVar of the reader alone would pass with
``ScriptedClient`` and fail here.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock

import background_jobs as jobs
import log_cid
from drivers.in_casa_driver import InCasaDriver
from engagement_registry import EngagementRegistry
from log_cid import cid_var
# #898: install_logging is process-global; the guard fails a test here that
# leaves logging state altered. Autouse applies only where the name is imported.
from logging_state import (  # noqa: F401 — autouse where imported
    casa_logging_guard,
    casa_logging_restored,
)
from sdk_client_pool import _CidBox
from turn_scripted_session import TurnScriptedSession

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def result():
    return ResultMessage("success", 1, 1, False, 1, "session")


def text_frame(text):
    return AssistantMessage(content=[TextBlock(text=text)], model="test")


class ScriptedClient(TurnScriptedSession):
    """Each written message runs one script, in order. A callable step runs
    inline, in the session's reader."""

    def __init__(self):
        self.scripts: list[list] = []
        self.prompts: list[str] = []
        self.current = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def record_query(self, prompt):
        self.prompts.append(prompt)

    async def receive_response(self):
        script = self.scripts.pop(0)
        for item in script:
            if callable(item):
                await self.run(item)
            else:
                yield item

    async def run(self, step):
        await step()


class ReaderClient(ScriptedClient):
    """Runs every script callable in a task created from the context captured
    at ``__aenter__`` — the SDK reader's dispatch of a tool callback."""

    def __init__(self):
        super().__init__()
        self.saved = None

    async def __aenter__(self):
        self.saved = contextvars.copy_context()
        return self

    async def run(self, step):
        await asyncio.get_running_loop().create_task(step(), context=self.saved.copy())


class _Stream:
    async def emit(self, text):
        return None

    async def finalize(self, text):
        from channels import DeliveryOutcome
        return DeliveryOutcome.DELIVERED


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


@pytest.fixture
async def rig(tmp_path, monkeypatch):
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "jobs.json"))
    decl = jobs.JobDecl("sample:work", "sample", "work", "sample:work", "Process rows", None)
    rec = await reg.create(
        "specialist", "worker", "in_casa", "Process rows",
        {"role": "assistant", "channel": "telegram", "chat_id": "100",
         "cid": ENGAGING, "job": jobs.initial_job_state(decl)})
    driver = InCasaDriver(record_lookup=reg.get,
                          begin_turn_delivery=reg.begin_turn_delivery)
    holder = {}

    def factory(options):
        return holder["client"]
    monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", factory)
    yield driver, rec, holder
    await driver.cancel(rec)


def observe(driver, rec, seen, tag):
    """Record, from wherever the client runs this step, the cid a log line
    would carry — the contextvar AND a real LogRecord made through the
    installed factory — and whether the job's turn is open."""
    async def go():
        record = logging.getLogger("tool").makeRecord(
            "tool", logging.INFO, __file__, 1, "probe", (), None)
        seen.append((tag, str(cid_var.get()), record.cid, driver.turn_open(rec.id)))
    return go


async def until(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "condition never held"
        await asyncio.sleep(.002)


def assert_ties(rec, ties, cids):
    """Exactly one tie line per executed turn, naming that turn's cid, the
    engagement and the engaging cid. The cid the tie line's own record
    carries is pinned in ``test_the_tie_line_is_logged_under_the_turns_cid``."""
    messages = [r.getMessage() for r in ties.records]
    assert len(messages) == len(cids), messages
    for message, cid in zip(messages, cids):
        assert f"turn cid={cid} " in message, message
        assert rec.id[:8] in message, message
        assert f"engaged_by={ENGAGING}" in message, message


class TestEngagementTurnCid:
    """Declared (D34): every in_casa engagement turn logs under a cid minted for
    that turn — in the session's reader and in the tasks the SDK reader runs
    tool callbacks in — tied by one line to the engagement."""

    async def _three_turns(self, rig, client, ties):
        driver, rec, holder = rig
        holder["client"] = client
        box = _CidBox()
        box.value = ENGAGING
        cid_var.set(box)  # the resident read task's binding when start_job runs
        seen = []
        client.scripts = [[text_frame("Start"), observe(driver, rec, seen, "brief"), result()],
                          [observe(driver, rec, seen, "t1"), result()],
                          [observe(driver, rec, seen, "t2"), result()]]
        await driver.open(rec, ClaudeAgentOptions())
        assert cid_var.get() is box  # the opener's own binding is restored
        driver.enqueue_brief(rec, "The brief")
        await until(lambda: len(seen) == 1 and not driver.turn_open(rec.id))
        box.value = LATER
        driver.submit_casa(rec, "Continue the job.")
        await until(lambda: len(seen) == 2 and not driver.turn_open(rec.id))
        driver.submit_casa(rec, "Continue the job.")
        await until(lambda: len(seen) == 3 and not driver.turn_open(rec.id))
        assert [s[0] for s in seen] == ["brief", "t1", "t2"]
        assert all(s[3] for s in seen), seen          # read while the turn is open
        assert all(s[1] == s[2] for s in seen), seen
        cids = [s[1] for s in seen]
        assert not set(cids) & {ENGAGING, LATER, "-"}, cids
        assert len(set(cids)) == 3, cids
        assert_ties(rec, ties, cids)

    async def test_turn_cids_ignore_engaging_box(self, rig, ties):
        await self._three_turns(rig, ScriptedClient(), ties)

    async def test_reader_records_follow_current_turn(self, rig, ties):
        await self._three_turns(rig, ReaderClient(), ties)

    async def test_batch_cid_from_empty_context(self, rig, ties):
        """A turn Casa itself starts from a context with no cid (the turn-end
        guard's nudge, a boot resume) gets a cid of its own too."""
        driver, rec, holder = rig
        client = holder["client"] = ScriptedClient()
        seen = []
        client.scripts = [[text_frame("Start"), result()],
                          [observe(driver, rec, seen, "t1"), result()],
                          [observe(driver, rec, seen, "t2"), result()]]
        await driver.open(rec, ClaudeAgentOptions())
        driver.enqueue_brief(rec, "The brief")
        await until(lambda: len(client.prompts) == 1 and not driver.turn_open(rec.id))

        async def from_nowhere():
            driver.submit_casa(rec, "Continue the job.")
        for n in (1, 2):
            await asyncio.get_running_loop().create_task(
                from_nowhere(), context=contextvars.Context())
            await until(lambda: len(seen) == n and not driver.turn_open(rec.id))
        assert [s[0] for s in seen] == ["t1", "t2"]
        assert all(s[1] == s[2] and s[1] != "-" for s in seen), seen
        assert seen[0][1] != seen[1][1], seen
        messages = [r.getMessage() for r in ties.records][1:]
        assert len(messages) == 2, messages
        for message, s in zip(messages, seen):
            assert f"turn cid={s[1]} " in message, message


async def test_the_tie_line_is_logged_under_the_turns_cid(rig, ties):
    driver, rec, holder = rig
    client = holder["client"] = ScriptedClient()
    box = _CidBox()
    box.value = ENGAGING
    cid_var.set(box)
    seen = []
    client.scripts = [[observe(driver, rec, seen, "brief"), result()]]
    await driver.open(rec, ClaudeAgentOptions())
    driver.enqueue_brief(rec, "The brief")
    await until(lambda: len(seen) == 1 and not driver.turn_open(rec.id))
    assert [r.cid for r in ties.records] == [seen[0][1]], [
        (r.cid, r.getMessage()) for r in ties.records]
