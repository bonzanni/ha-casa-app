"""The channel's seams into a background job: the operator's reply in their
direct chat, a tapped answer. Each seam journals, admits and queues the
message through the driver's synchronous ``submit_operator`` and returns — no
Telegram handler waits on the job's session. Nothing written in the log group
reaches a job. A message the job did not accept draws one "not delivered"
line.

REAL ``TelegramChannel``, ``EngagementRegistry`` and ``InCasaDriver``; the
CLI is the ``FakeCLI`` of ``test_job_session_driver`` (a write can be held in
the transport to stand for a slow or stuck session)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ClaudeAgentOptions

import job_inbox
from drivers import in_casa_driver as drv

try:
    from tests.test_job_session_driver import FakeCLI, until
except ImportError:  # pragma: no cover — direct-path collection
    from test_job_session_driver import FakeCLI, until

pytestmark = pytest.mark.asyncio

OPERATOR = 100
MEMBER = 555
SUPERGROUP = -1001
TOPIC = 77


class _Bot:
    def __init__(self):
        self.sent: list[dict] = []
        self._next = 2000

    async def send_message(self, chat_id, text, **kw):
        self._next += 1
        rp = kw.get("reply_parameters")
        self.sent.append({"chat_id": chat_id, "text": str(text),
                          "reply_to": getattr(rp, "message_id", None),
                          "thread": kw.get("message_thread_id")})
        return SimpleNamespace(message_id=self._next)


async def _reply(rig, text, *, message_id=900):
    return await rig.ch.deliver_operator_reply(
        rig.rec, text, chat_id=OPERATOR, message_id=message_id)


def _topic_update(text, *, user=OPERATOR, message_id=900):
    msg = SimpleNamespace(
        text=text, message_id=message_id, message_thread_id=TOPIC,
        chat=SimpleNamespace(id=SUPERGROUP),
        from_user=SimpleNamespace(id=user, first_name="Someone"))
    return SimpleNamespace(message=msg)


@pytest.fixture
async def rig(tmp_path, monkeypatch):
    from channels.telegram import TelegramChannel
    from engagement_registry import EngagementRegistry
    import background_jobs as jobs

    FakeCLI.instances, FakeCLI.events = [], []
    FakeCLI.on_enter = FakeCLI.hold_disconnect = None
    monkeypatch.setattr(drv, "ClaudeSDKClient", FakeCLI)
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    decl = jobs.JobDecl("q:run", "q", "run", "q:job", "Accounting check", None)
    origin = {"role": "assistant", "channel": "telegram",
              "chat_id": str(OPERATOR), "user_id": OPERATOR,
              "_origin_clearance": "private",
              "job": jobs.initial_job_state(decl, started_by="operator")}
    rec = await reg.create("specialist", "finance", "in_casa", "Check", origin)
    bot = _Bot()
    ch = TelegramChannel(bot=bot, chat_id=str(OPERATOR),
                         engagement_supergroup_id=SUPERGROUP)
    driver = drv.InCasaDriver(
        record_lookup=reg.get, begin_turn_delivery=reg.begin_turn_delivery,
        lifecycle_lock=ch.engagement_lifecycle_lock)
    ch._engagement_registry = reg
    ch._engagement_driver = driver
    await driver.open(rec, ClaudeAgentOptions())
    cli = FakeCLI.instances[-1]
    yield SimpleNamespace(reg=reg, rec=rec, ch=ch, drv=driver, cli=cli, bot=bot)
    await driver.cancel(rec)


async def test_a_reply_is_accepted_without_waiting_on_the_session(rig):
    """The CLI's transport holds every write: the seam still returns, with
    the message journaled and admitted, and the job's delivery worker writes
    it once the transport moves."""
    rig.cli.gate = asyncio.Event()
    assert await asyncio.wait_for(_reply(rig, "use Q3 only"), 2)
    assert [e["text"] for e in job_inbox.load(rig.rec.id)] == ["use Q3 only"]
    assert rig.drv.inbound_unread_depth(rig.rec.id) == 1
    assert rig.cli.written == []
    rig.cli.gate.set()
    await until(lambda: rig.cli.texts() == ["use Q3 only"])


async def test_a_second_message_is_accepted_while_the_first_write_hangs(rig):
    rig.cli.gate = asyncio.Event()
    assert await _reply(rig, "first", message_id=1)
    await until(lambda: rig.cli.writing == 1)
    assert await asyncio.wait_for(_reply(rig, "second", message_id=2), 2)
    assert [e["text"] for e in job_inbox.load(rig.rec.id)] == ["first", "second"]
    rig.cli.gate.set()
    await until(lambda: rig.cli.texts() == ["first", "second"])


async def test_a_resume_in_progress_does_not_block_a_message(rig):
    """A resume owner holds the job's lifecycle lock (a slow resume): the
    reply seam does not take it, so the message is still accepted."""
    lock = rig.ch.engagement_lifecycle_lock(rig.rec)
    async with lock:
        assert await asyncio.wait_for(_reply(rig, "hello"), 2)
        assert [e["text"] for e in job_inbox.load(rig.rec.id)] == ["hello"]


@pytest.mark.parametrize("user", [OPERATOR, MEMBER])
async def test_a_message_in_the_log_group_never_reaches_the_job(rig, user):
    """Even in the thread an older version used as this job's topic: one
    pointer to the direct chat, and nothing journaled, admitted or written."""
    await rig.ch.handle_update(_topic_update("restate the task", user=user))
    assert [(m["chat_id"], m["thread"], m["text"]) for m in rig.bot.sent] == [
        (SUPERGROUP, TOPIC,
         "This group is a read-only log. Talk to me in our direct chat.")]
    assert job_inbox.load(rig.rec.id) == []
    assert rig.drv.inbound_unread_depth(rig.rec.id) == 0
    await asyncio.sleep(0.01)
    assert rig.cli.written == []
    assert rig.reg.get(rig.rec.id).origin["_origin_clearance"] == "private"


async def test_a_message_the_journal_cannot_take_is_told_once(rig, tmp_path,
                                                               monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(job_inbox, "INBOX_ROOT", blocker)
    assert await _reply(rig, "lost?", message_id=41) is False
    assert [(m["reply_to"], m["text"]) for m in rig.bot.sent] == [
        (41, "Your message could not be delivered — please resend it.")]
    assert rig.drv.inbound_unread_depth(rig.rec.id) == 0


async def test_an_operator_reply_is_journaled_with_its_notice_target(rig):
    rig.cli.gate = asyncio.Event()
    ok = await asyncio.wait_for(rig.ch.deliver_operator_reply(
        rig.rec, "go on", chat_id=OPERATOR, message_id=31,
        quoted_text="📊 Finance started «Accounting check»."), 2)
    assert ok is True
    [entry] = job_inbox.load(rig.rec.id)
    assert entry["notice"] == {"chat_id": OPERATOR, "message_id": 31,
                               "answer": False}
    assert entry["text"] == (
        "[replying to «📊 Finance started «Accounting check».»]\ngo on")
    rig.cli.gate.set()
    await until(lambda: rig.cli.written)


async def test_a_tapped_answer_hands_its_reservation_to_the_ticket(rig):
    """The tap's reservation is released in the step that admits the ticket:
    the completion gate never sees both empty."""
    rig.cli.gate = asyncio.Event()
    lease = rig.ch.engagement_inbound_reservation(rig.rec.id)
    assert lease.take()
    seen = []
    original = rig.drv.submit_operator

    def submit(*a, **kw):
        ticket = original(*a, **kw)
        seen.append((rig.drv.inbound_unread_depth(rig.rec.id),
                     rig.drv.inbound_reservations(rig.rec.id)))
        return ticket
    rig.drv.submit_operator = submit
    from channels.telegram import _NoticeTarget
    ok = await rig.ch.deliver_system_turn(
        rig.rec, "[operator answered «Which?»: B]", inbound_reservation=lease,
        notice_target=_NoticeTarget(chat_id=OPERATOR, message_id=40, answer=True))
    assert ok is True
    assert seen == [(1, 1)]                       # both held at admission
    assert rig.drv.inbound_reservations(rig.rec.id) == 0
    assert job_inbox.load(rig.rec.id)[0]["notice"]["answer"] is True
    rig.cli.gate.set()


async def test_a_lost_journal_entry_is_told_in_reply_to_its_message(rig, caplog):
    """Every journaled operator message names the direct chat message or
    card to reply to; an entry without one is logged and discharged."""
    await rig.reg.try_transition_terminal(rig.rec.id, "completed", strict=True)
    await rig.ch.tell_journal_lost(
        rig.rec, {"uuid": "u", "text": "x",
                  "notice": {"chat_id": OPERATOR, "message_id": 31, "answer": False}})
    await rig.ch.tell_journal_lost(
        rig.rec, {"uuid": "v", "text": "y",
                  "notice": {"chat_id": OPERATOR, "message_id": 40, "answer": True}})
    with caplog.at_level("WARNING", logger="channels.telegram"):
        await rig.ch.tell_journal_lost(
            rig.rec, {"uuid": "w", "text": "z", "notice": None})
    assert [(m["reply_to"], m["text"]) for m in rig.bot.sent] == [
        (31, "The job had just ended — not delivered, please resend."),
        (40, "The job had already ended; your answer wasn't used.")]
    assert any("no direct chat message to reply to" in r.message
               for r in caplog.records if r.levelname == "WARNING")


async def test_the_finalize_funnel_tells_each_unread_message_once(rig, monkeypatch):
    """INV-ENG-029 / INV-BGJOB-021: a job ended with a message it never read
    — journaled, written, never echoed — tells that message as lost once, in
    reply to it; the journal goes with the record."""
    import tools
    from unittest.mock import MagicMock
    cm = MagicMock()
    cm.get.return_value = rig.ch
    monkeypatch.setattr(tools, "_channel_manager", cm)
    monkeypatch.setattr(tools, "_engagement_registry", rig.reg)
    monkeypatch.setattr(tools, "_bus", None)
    monkeypatch.setattr(job_inbox, "transcript_path",
                        lambda cwd, sid: None)
    ok = await rig.ch.deliver_operator_reply(
        rig.rec, "one more thing", chat_id=OPERATOR, message_id=55)
    assert ok
    await until(lambda: rig.cli.written)
    await tools._finalize_engagement(
        rig.rec, outcome="cancelled", text="Cancelled by the operator.",
        next_steps=[], driver=rig.drv)
    replies = [m for m in rig.bot.sent if m["reply_to"] == 55]
    assert [m["text"] for m in replies] == [
        "The job had just ended — not delivered, please resend."]
    assert not job_inbox.path_for(rig.rec.id).exists()
    assert rig.drv.inbound_unread_depth(rig.rec.id) == 0
