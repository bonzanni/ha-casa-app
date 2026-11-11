"""A background job in the operator's direct chat: replies into a live job,
its questions and their taps, the start line's Cancel, the one notice per
lost message, keyboard retirement, and quiet runs that end on interaction.

REAL ``TelegramChannel`` handlers and seams, REAL ``EngagementRegistry`` and
``InCasaDriver`` (with a scripted SDK client), the REAL finalize funnel; a
fake Bot API that records what was sent, edited and answered. The race tests
count notices and deliveries, not statuses. No ``asyncio.sleep`` is patched.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import job_inbox

try:
    from tests.test_job_session_driver import until
    from tests.turn_scripted_session import TurnScriptedSession, attach_session
except ImportError:  # pragma: no cover — direct-path collection
    from test_job_session_driver import until
    from turn_scripted_session import TurnScriptedSession, attach_session

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

ENDED_REPLY_NOTICE = ("The job had just finished, so your message didn't get through. "
                      "Could you send it again?")

OPERATOR = 100
CHAT = 100
#: The bot the rig's channel is: every job post is addressed through it.
BOT = 111


def A(mid: int) -> "TgAddress":
    """The address of message *mid* in the operator's chat on the rig's bot."""
    from channels.tg_address import TgAddress
    return TgAddress(BOT, CHAT, mid)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _Bot:
    """The Bot API calls the job path makes, recorded."""

    def __init__(self):
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.markups: list[dict] = []
        self._next = 1000
        self.fail_sends = 0

    async def send_message(self, chat_id, text, **kw):
        if self.fail_sends:
            self.fail_sends -= 1
            raise RuntimeError("send failed")
        self._next += 1
        rp = kw.get("reply_parameters")
        self.sent.append({"chat_id": chat_id, "text": str(text),
                          "reply_to": getattr(rp, "message_id", None),
                          "markup": kw.get("reply_markup"),
                          "message_id": self._next})
        return SimpleNamespace(message_id=self._next)

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.edits.append({"chat_id": chat_id, "message_id": message_id,
                           "text": str(text), "markup": kw.get("reply_markup")})
        return True

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None, **kw):
        self.markups.append({"chat_id": chat_id, "message_id": message_id,
                             "markup": reply_markup})
        return True

    def replies(self):
        return [m for m in self.sent if m["reply_to"] is not None]


class _CQ:
    """A callback query whose answer can be held, to observe what happened
    before the handler's first await."""

    def __init__(self, data, message_id, *, user=OPERATOR, hold=False):
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(id=CHAT),
                                       message_id=message_id)
        self.from_user = SimpleNamespace(id=user, first_name="Op")
        self.answers: list[str] = []
        self.gate = asyncio.Event()
        if not hold:
            self.gate.set()
        self.answering = asyncio.Event()

    async def answer(self, text=None):
        self.answering.set()
        await self.gate.wait()
        self.answers.append(text)


def _job_state(*, started_by="operator", quiet=False):
    import background_jobs as jobs
    decl = jobs.JobDecl("sample:work", "sample", "work", "sample:work",
                        "Process rows", None, quiet_when_scheduled=quiet)
    return jobs.initial_job_state(decl, started_by=started_by)


def _mk_assistant(text):
    from claude_agent_sdk import AssistantMessage, TextBlock
    return AssistantMessage(content=[TextBlock(text=text)], model="m")


def _result():
    from claude_agent_sdk import ResultMessage
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=False, num_turns=1, session_id="sid")


class _Session(TurnScriptedSession):
    """The job's live session: each written message runs as its own turn,
    taken up (echoed) first and answered with one line. ``gate``, when set,
    holds every write inside the client call — a session busy or slow to
    accept input."""

    def __init__(self):
        self.query_prompts: list[str] = []
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def record_query(self, text):
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        self.query_prompts.append(text)

    async def receive_response(self):
        yield _mk_assistant("ok")
        yield _result()

    async def close(self):
        pass


class _Rig:
    pass


@pytest.fixture(autouse=True)
def _task_index(monkeypatch):
    """Every job's posts are found through the task index, which reads the
    engagement registry that holds the job."""
    import task_posts_double
    return task_posts_double.install(monkeypatch)


async def _route_reply(ch, update, chat_id) -> bool:
    """The reply route the text handler runs first: a live task's post, into
    the task. ``True`` when the message was taken there."""
    live, _ended = ch._quoted_task(chat_id, update.effective_user,
                                   update.message.reply_to_message)
    return live is not None and await ch._route_task_reply(update, chat_id, live) == "taken"


async def _rig(tmp_path, monkeypatch, *, quiet=False, started_by="operator",
               kind="specialist"):
    import tools
    from channels.telegram import TelegramChannel
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRegistry

    r = _Rig()
    r.bot = _Bot()
    r.reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    origin = {"role": "assistant", "channel": "telegram", "chat_id": str(CHAT),
              "user_id": OPERATOR, "_tg_bot": BOT,
              "job": _job_state(started_by="scheduled" if quiet else started_by,
                                quiet=quiet)}
    from channels.tg_address import TgAddress
    from task_model import Requester
    # the job posts in this chat, decided at its birth
    r.rec = await r.reg.create(kind, "finance", "in_casa", "t", origin,
                               executor="finance",
                               requester=Requester("assistant", {"_tg_bot": BOT}),
                               delivery=TgAddress(BOT, CHAT))
    ch = TelegramChannel(bot=r.bot, chat_id=str(CHAT), known_bot_id=BOT)
    r.client = _Session()
    r.drv = InCasaDriver(
        record_lookup=r.reg.get,
        begin_turn_delivery=r.reg.begin_turn_delivery,
        lifecycle_lock=ch.engagement_lifecycle_lock)
    attach_session(r.drv, r.rec, r.client)
    # No session transcript in the rig: the journal's terminal branch tells
    # every entry still in the journal.
    monkeypatch.setattr(job_inbox, "transcript_path", lambda cwd, sid: None)
    ch._engagement_registry = r.reg
    ch._engagement_driver = r.drv
    # ``delivered``: the messages the job accepted (journaled and queued);
    # ``client.query_prompts``: what was written into its session.
    r.delivered: list[str] = []
    real_submit = r.drv.submit_operator

    def _submit(rec, text, **kw):
        ticket = real_submit(rec, text, **kw)
        r.delivered.append(text)
        return ticket
    r.drv.submit_operator = _submit

    async def _finalize_cancel(rec, reason="user"):
        return await tools._finalize_engagement(
            rec, outcome="cancelled", text=f"Cancelled by {reason}.",
            next_steps=[], driver=r.drv)
    ch._finalize_cancel = _finalize_cancel
    r.ch = ch
    # The channel is Casa's endpoint behind the transport, as casa_core
    # registers it.
    from telegram_channel_double import make_transport
    cm = make_transport(ch)
    r.bus = MagicMock()
    r.bus.notify = AsyncMock()
    tools.init_tools(channel_manager=cm, bus=r.bus,
                     specialist_registry=MagicMock(), mcp_registry=MagicMock(),
                     trigger_registry=MagicMock(), engagement_registry=r.reg)
    return r


async def _drain(r):
    """Every channel task done (a Cancel's funnel, the keyboard edits), every
    message the job accepted written, taken up and its turn ended (or
    dropped), and every deferred persist written."""
    def _pending():
        return [t for t in (r.ch._turn_tasks | r.ch._job_ui_tasks)
                if not t.done()]

    for _ in range(200):
        pending = _pending()
        if pending:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), 10)
        lane = r.drv._lanes.get(r.rec.id)
        if r.client.gate is None or r.client.gate.is_set():
            await until(lambda: (not r.drv.turn_open(r.rec.id))
                        and not (lane is not None and lane.queue), 5)
        if not _pending():
            break
    while r.reg._deferred_persists:
        await asyncio.gather(*list(r.reg._deferred_persists),
                             return_exceptions=True)


def _reply_update(text, quoted_id, *, user=OPERATOR, quoted_text="📊 Finance started «Process rows».",
                  message_id=7):
    quoted = SimpleNamespace(message_id=quoted_id, text=quoted_text, caption=None,
                             from_user=None, sender_chat=None, date=None)
    msg = SimpleNamespace(text=text, message_id=message_id, reply_to_message=quoted,
                          from_user=SimpleNamespace(id=user, first_name="Op"),
                          sender_chat=None)
    return SimpleNamespace(message=msg,
                           effective_user=SimpleNamespace(id=user, first_name="Op"),
                           effective_chat=SimpleNamespace(id=CHAT))


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


class _HeldWrite:
    def __init__(self, reg):
        self.reg = reg
        self.real = reg._write_tombstone
        self.started = threading.Event()
        self.release = threading.Event()
        self.fail = False
        reg._write_tombstone = self._write

    def _write(self, snapshot):
        self.started.set()
        self.release.wait(5)
        if self.fail:
            raise OSError("disk full")
        self.real(snapshot)

    def restore(self):
        self.reg._write_tombstone = self.real


async def _wait(event_like, what):
    for _ in range(1000):
        if event_like():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


# ---------------------------------------------------------------------------
# the reply route
# ---------------------------------------------------------------------------


class TestReplyRoute:
    async def test_a_reply_to_a_live_jobs_post_goes_into_the_job(
            self, tmp_path, monkeypatch):
        r = await _rig(tmp_path, monkeypatch)
        r.reg.record_job_post(r.rec.id, A(501))
        resident = AsyncMock(return_value=True)
        monkeypatch.setattr(r.ch, "_dispatch_dm_turn", resident)
        await r.ch._handle_serialized(_reply_update("use the second account", 501),
                                      str(CHAT))
        await _drain(r)
        assert r.delivered == [
            "[replying to «📊 Finance started «Process rows».»]\nuse the second account"]
        assert r.client.query_prompts == r.delivered
        assert resident.await_count == 0
        assert r.bot.replies() == []
        assert r.drv.inbound_unread_depth(r.rec.id) == 0

    async def test_a_reply_to_an_ended_jobs_post_falls_through(
            self, tmp_path, monkeypatch):
        r = await _rig(tmp_path, monkeypatch)
        r.reg.record_job_post(r.rec.id, A(501))
        await r.reg.try_transition_terminal(r.rec.id, "completed", strict=True)
        resident = AsyncMock(return_value=True)
        monkeypatch.setattr(r.ch, "_dispatch_dm_turn", resident)
        await r.ch._handle_serialized(_reply_update("hello", 501), str(CHAT))
        # #1522: past the job, to the resident's turn
        assert resident.await_count == 1
        assert r.delivered == []

    async def test_another_sender_falls_through(self, tmp_path, monkeypatch):
        r = await _rig(tmp_path, monkeypatch)
        r.reg.record_job_post(r.rec.id, A(501))
        assert await _route_reply(r.ch, 
            _reply_update("hello", 501, user=555), str(CHAT)) is False

    async def test_a_plugin_post_of_the_job_is_filed_and_routes(
            self, tmp_path, monkeypatch):
        import result_broker
        r = await _rig(tmp_path, monkeypatch)
        post = result_broker.PostRecord(
            role="finance", operator_id=OPERATOR, plugin="ledger",
            slot="operator_message", tool_use_id="tu", owner=r.rec.id,
            posted_at=0.0)
        r.ch._record_post(CHAT, SimpleNamespace(message_id=640), post)
        assert r.reg.job_post_owner(A(640)) is r.rec
        assert await _route_reply(r.ch, 
            _reply_update("skip that one", 640, quoted_text="📊 Finance\nrow 7"),
            str(CHAT)) is True
        await _drain(r)
        assert r.delivered == ["[replying to «📊 Finance»]\nskip that one"]


# ---------------------------------------------------------------------------
# one notice per lost message
# ---------------------------------------------------------------------------


class TestOneNoticePerLostMessage:
    async def test_a_reply_refused_because_the_job_ended_is_told_once_in_reply(
            self, tmp_path, monkeypatch):
        r = await _rig(tmp_path, monkeypatch)
        await r.reg.try_transition_terminal(r.rec.id, "completed", strict=True)
        handed = await r.ch.deliver_operator_reply(
            r.rec, "hello", chat_id=CHAT, message_id=7)
        await _drain(r)
        assert handed is False
        assert [(m["text"], m["reply_to"]) for m in r.bot.replies()] == [
            (ENDED_REPLY_NOTICE, 7)]
        assert r.drv.inbound_unread_depth(r.rec.id) == 0

    async def test_cancel_racing_a_queued_reply_tells_it_exactly_once(
            self, tmp_path, monkeypatch):
        """RACE: a reply accepted and waiting in the job's queue behind a
        write the session has not finished taking, while a Cancel tap ends the
        job. The write-time fence keeps it out of the session, and the
        journal's terminal branch — run by the finalize funnel after the
        client closed — tells the operator once, in reply to their message."""
        r = await _rig(tmp_path, monkeypatch)
        r.client.gate = asyncio.Event()          # the session is busy
        r.drv.submit_casa(r.rec, "Continue.")
        await asyncio.wait_for(r.client.entered.wait(), 5)
        assert await r.ch.deliver_operator_reply(
            r.rec, "stop at row 9", chat_id=CHAT, message_id=7) is True
        cq = _CQ(f"v1|job_cancel|{r.rec.id}", 900)
        await r.ch._on_inline_callback(SimpleNamespace(callback_query=cq))
        await _wait(lambda: r.rec.status == "cancelled", "the cancel")
        r.client.gate.set()
        await _drain(r)
        assert cq.answers == ["Cancelling…"]
        assert r.delivered == ["stop at row 9"]             # accepted ...
        assert "stop at row 9" not in "".join(r.client.query_prompts)  # ... never written
        notices = [(m["text"], m["reply_to"]) for m in r.bot.replies()]
        assert notices == [(ENDED_REPLY_NOTICE, 7)]
        assert not job_inbox.path_for(r.rec.id).exists()

    async def test_a_tapped_answer_refused_is_told_once_on_the_question(
            self, tmp_path, monkeypatch):
        from channels.telegram import _NoticeTarget
        r = await _rig(tmp_path, monkeypatch)
        await r.reg.try_transition_terminal(r.rec.id, "error", strict=True)
        handed = await r.ch.deliver_system_turn(
            r.rec, "[operator answered «q»: A]",
            notice_target=_NoticeTarget(A(812), answer=True))
        await _drain(r)
        assert handed is False
        assert [(m["text"], m["reply_to"]) for m in r.bot.replies()] == [
            ("The job had already finished, so your answer wasn't used.", 812)]


# ---------------------------------------------------------------------------
# questions and taps
# ---------------------------------------------------------------------------


async def _ask(r, question="Which account?", options=("Savings", "Current")):
    import tools
    token = tools.engagement_var.set(r.rec)
    try:
        payload = await tools.ask_operator.handler(
            {"question": question, "options": list(options)})
    finally:
        tools.engagement_var.reset(token)
    return json.loads(payload["content"][0]["text"])


class TestAskAndTap:
    async def test_a_question_is_a_filed_post_whose_buttons_name_the_engagement(
            self, tmp_path, monkeypatch):
        from engagement_registry import JOB_POST_QUESTION
        r = await _rig(tmp_path, monkeypatch)
        out = await _ask(r)
        assert out["status"] == "asked"
        mid = out["message_id"]
        post = r.bot.sent[-1]
        assert "Which account?" in post["text"]
        (qid,) = r.rec.questions
        datas = [b.callback_data for b in _buttons(post["markup"])]
        assert datas == [f"v1|job_q|{r.rec.id}|{qid}|0",
                         f"v1|job_q|{r.rec.id}|{qid}|1",
                         f"v1|job_cancel|{r.rec.id}"]
        assert all(len(d.encode()) <= 64 for d in datas)
        assert r.reg.job_post_owner(A(mid)) is r.rec
        assert (A(mid), JOB_POST_QUESTION) in r.rec.ui_posts

    async def test_a_plugin_worker_cannot_ask(self, tmp_path, monkeypatch):
        r = await _rig(tmp_path, monkeypatch, kind="plugin")
        assert (await _ask(r))["kind"] == "not_a_job"
        assert r.bot.sent == []

    async def test_a_tap_answers_the_job(self, tmp_path, monkeypatch):
        r = await _rig(tmp_path, monkeypatch)
        mid = (await _ask(r))["message_id"]
        (qid,) = r.rec.questions
        cq = _CQ(f"v1|job_q|{r.rec.id}|{qid}|1", mid)
        await r.ch._on_inline_callback(SimpleNamespace(callback_query=cq))
        await _drain(r)
        assert cq.answers == ["✔"]
        assert r.delivered == ["[operator answered «Which account?»: Current]"]
        assert r.client.query_prompts == r.delivered
        assert r.bot.markups and "✔ Current" in [
            b.text for b in _buttons(r.bot.markups[-1]["markup"])]
        assert r.drv.inbound_reservations(r.rec.id) == 0

    async def test_tap_reserves_before_its_first_await_so_a_completion_is_vetoed(
            self, tmp_path, monkeypatch):
        """RACE: the tap's toast is its first await; held there, a successful
        completion is refused by the reservation the tap already took — and
        the answer is then delivered once, with no notice."""
        import tools
        r = await _rig(tmp_path, monkeypatch)
        mid = (await _ask(r))["message_id"]
        (qid,) = r.rec.questions
        cq = _CQ(f"v1|job_q|{r.rec.id}|{qid}|0", mid, hold=True)
        tap = asyncio.ensure_future(
            r.ch._on_inline_callback(SimpleNamespace(callback_query=cq)))
        await asyncio.wait_for(cq.answering.wait(), 5)
        result = await tools._finalize_engagement(
            r.rec, outcome="completed", text="done", next_steps=[],
            driver=r.drv, inbound_gate=True)
        assert result is tools.FinalizeResult.PRECONDITION_FAILED
        assert r.rec.status == "active"
        cq.gate.set()
        await tap
        await _drain(r)
        assert r.delivered == ["[operator answered «Which account?»: Savings]"]
        assert r.bot.replies() == []

    async def test_a_tap_on_a_question_the_record_no_longer_holds_delivers_nothing(
            self, tmp_path, monkeypatch):
        r = await _rig(tmp_path, monkeypatch)
        cq = _CQ(f"v1|job_q|{r.rec.id}|ABCDEFGH|0", 811)
        await r.ch._on_inline_callback(SimpleNamespace(callback_query=cq))
        await _drain(r)
        assert cq.answers == ["This question is no longer active — reply in words"]
        assert r.delivered == [] and r.bot.sent == []
        assert r.drv.inbound_reservations(r.rec.id) == 0

    async def test_a_tap_on_an_ended_job_is_already_finished(
            self, tmp_path, monkeypatch):
        r = await _rig(tmp_path, monkeypatch)
        mid = (await _ask(r))["message_id"]
        (qid,) = r.rec.questions
        await r.reg.try_transition_terminal(r.rec.id, "completed", strict=True)
        await _drain(r)
        r.bot.sent.clear()
        cq = _CQ(f"v1|job_q|{r.rec.id}|{qid}|0", mid)
        await r.ch._on_inline_callback(SimpleNamespace(callback_query=cq))
        assert cq.answers == ["already finished"]
        assert r.delivered == [] and r.bot.sent == []

    async def test_a_tap_by_someone_else_is_refused(self, tmp_path, monkeypatch):
        r = await _rig(tmp_path, monkeypatch)
        mid = (await _ask(r))["message_id"]
        (qid,) = r.rec.questions
        cq = _CQ(f"v1|job_q|{r.rec.id}|{qid}|0", mid, user=555)
        await r.ch._on_inline_callback(SimpleNamespace(callback_query=cq))
        assert cq.answers == ["not for you"] and r.delivered == []

    @pytest.mark.parametrize("data", [
        "v1|job_q|nothex|ABCDEFGH|0",
        "v1|job_q|" + "a" * 32 + "|abcdefgh|0",
        "v1|job_q|" + "a" * 32 + "|ABCDEFGH|x",
        "v1|job_q|" + "a" * 32 + "|ABCDEFGH|0|9",
        "v1|job_cancel|" + "a" * 31,
    ])
    async def test_a_malformed_job_button_is_expired(self, tmp_path, monkeypatch, data):
        r = await _rig(tmp_path, monkeypatch)
        cq = _CQ(data, 1)
        await r.ch._on_inline_callback(SimpleNamespace(callback_query=cq))
        assert cq.answers == ["expired"]

    async def test_job_buttons_are_matched_before_the_generic_parser(self):
        from channels.telegram import _parse_callback_data, _parse_job_callback
        data = "v1|job_q|" + "a" * 32 + "|ABCDEFGH|3"
        assert _parse_callback_data(data) == (None, None, None, None)
        assert _parse_job_callback(data) == ("q", "a" * 32, "ABCDEFGH", 3)


# ---------------------------------------------------------------------------
# start line, Cancel, keyboard retirement
# ---------------------------------------------------------------------------


class TestStartLineCancelAndRetirement:
    async def test_an_operator_started_job_gets_a_silent_start_line_with_cancel(
            self, tmp_path, monkeypatch):
        import tools
        from engagement_registry import JOB_POST_START
        r = await _rig(tmp_path, monkeypatch)
        await tools._post_job_start_line(r.rec.id)
        (post,) = r.bot.sent
        assert post["text"] == "📊 finance started «Process rows»."
        assert [b.callback_data for b in _buttons(post["markup"])] == [
            f"v1|job_cancel|{r.rec.id}"]
        assert r.rec.ui_posts == [(A(post["message_id"]), JOB_POST_START)]
        assert r.reg.job_post_owner(A(post["message_id"])) is r.rec

    async def test_a_scheduled_job_gets_none(self, tmp_path, monkeypatch):
        import tools
        r = await _rig(tmp_path, monkeypatch, started_by="scheduled")
        await tools._post_job_start_line(r.rec.id)
        assert r.bot.sent == []

    async def test_cancel_ends_the_job_and_every_keyboard_is_retired(
            self, tmp_path, monkeypatch):
        import tools
        r = await _rig(tmp_path, monkeypatch)
        await tools._post_job_start_line(r.rec.id)
        start_mid = r.bot.sent[-1]["message_id"]
        q_mid = (await _ask(r))["message_id"]
        cq = _CQ(f"v1|job_cancel|{r.rec.id}", start_mid)
        await r.ch._on_inline_callback(SimpleNamespace(callback_query=cq))
        await _drain(r)
        assert cq.answers == ["Cancelling…"]
        assert r.rec.status == "cancelled"
        assert [e["text"] for e in r.bot.edits if e["message_id"] == start_mid] == [
            "🛑 finance «Process rows» · 1 min"]
        assert [m["message_id"] for m in r.bot.markups] == [q_mid]
        assert r.rec.ui_posts == []
        assert r.bot.replies() == []

    async def test_a_second_cancel_tap_while_the_first_runs_is_already_finishing(
            self, tmp_path, monkeypatch):
        r = await _rig(tmp_path, monkeypatch)
        r.ch._job_cancels.add(r.rec.id)
        cq = _CQ(f"v1|job_cancel|{r.rec.id}", 1)
        await r.ch._on_inline_callback(SimpleNamespace(callback_query=cq))
        assert cq.answers == ["already finishing"]

    async def test_a_cancel_that_could_not_be_saved_keeps_the_button_and_says_so(
            self, tmp_path, monkeypatch):
        import tools
        r = await _rig(tmp_path, monkeypatch)
        await tools._post_job_start_line(r.rec.id)
        await _drain(r)
        start_mid = r.bot.sent[-1]["message_id"]

        def _fail(snapshot):
            raise OSError("disk full")
        real = r.reg._write_tombstone
        r.reg._write_tombstone = _fail
        cq = _CQ(f"v1|job_cancel|{r.rec.id}", start_mid)
        await r.ch._on_inline_callback(SimpleNamespace(callback_query=cq))
        await _drain(r)
        r.reg._write_tombstone = real
        assert r.rec.status == "active"
        assert r.bot.edits == [] and r.bot.markups == []
        assert [(m["text"], m["reply_to"]) for m in r.bot.replies()] == [
            ("The job could not be cancelled just now — tap Cancel again.", start_mid)]
        assert r.rec.ui_posts and r.rec.id not in r.ch._job_cancels

    async def test_a_keyboard_post_landing_after_the_end_is_retired_by_its_filer(
            self, tmp_path, monkeypatch):
        from engagement_registry import JOB_POST_QUESTION
        r = await _rig(tmp_path, monkeypatch)
        await r.reg.try_transition_terminal(r.rec.id, "completed", strict=True)
        await r.ch._file_job_keyboard(r.rec, 950, JOB_POST_QUESTION)
        assert [m["message_id"] for m in r.bot.markups] == [950]
        assert r.rec.ui_posts == []
        assert r.reg.ended_job_post_owner(A(950)) is r.rec

    async def test_boot_retires_what_an_ended_job_still_carries(
            self, tmp_path, monkeypatch):
        from engagement_registry import JOB_POST_START
        r = await _rig(tmp_path, monkeypatch)
        r.reg.record_job_post(r.rec.id, A(960), keyboard=JOB_POST_START)
        r.reg._terminal_observer = None          # as if the edits never ran
        await r.reg.try_transition_terminal(r.rec.id, "completed", strict=True)
        assert r.ch.retire_ended_job_keyboards() == 1
        await _drain(r)
        assert [(e["message_id"], e["text"]) for e in r.bot.edits] == [
            (960, "✅ finance «Process rows» · 1 min")]
        assert r.ch.retire_ended_job_keyboards() == 0

    async def test_launch_death_retires_the_keyboards_too(self, tmp_path, monkeypatch):
        import tools
        from engagement_registry import JOB_POST_START
        r = await _rig(tmp_path, monkeypatch)
        r.reg.record_job_post(r.rec.id, A(970), keyboard=JOB_POST_START)
        result = await tools._report_launch_death(
            None, r.rec, kind="launch_incomplete", detail="x", driver=None)
        assert result is tools.LaunchDeathResult.REPORTED
        await _drain(r)
        assert [(e["message_id"], e["text"]) for e in r.bot.edits] == [
            (970, "❌ finance «Process rows» · 1 min")]


# ---------------------------------------------------------------------------
# quiet runs end on interaction
# ---------------------------------------------------------------------------


class TestQuietRunsEndOnInteraction:
    async def test_a_quiet_run_that_asks_is_told_at_its_end(
            self, tmp_path, monkeypatch):
        import tools
        r = await _rig(tmp_path, monkeypatch, quiet=True)
        assert (await _ask(r))["status"] == "asked"
        assert r.rec.origin["job"]["quiet"] is False
        await tools._finalize_engagement(
            r.rec, outcome="completed", text="done", next_steps=[], driver=r.drv)
        assert r.rec.terminal_notification_pending is True
        assert r.bus.notify.await_count == 1

    async def test_a_quiet_run_nobody_interacted_with_is_told_nowhere(
            self, tmp_path, monkeypatch):
        import tools
        r = await _rig(tmp_path, monkeypatch, quiet=True)
        await tools._finalize_engagement(
            r.rec, outcome="completed", text="done", next_steps=[], driver=r.drv)
        assert r.rec.terminal_notification_pending is False
        assert r.bus.notify.await_count == 0

    async def test_a_reply_racing_a_pending_quiet_completion_is_told_once(
            self, tmp_path, monkeypatch):
        """RACE: the reply is routed while the quiet run's completion write is
        pending (the record counts as live). The promotion waits for the
        write; the completion commits, so the reply is refused with its one
        notice and the end stays told to no one."""
        import tools
        r = await _rig(tmp_path, monkeypatch, quiet=True)
        r.reg.record_job_post(r.rec.id, A(501))
        await _drain(r)
        held = _HeldWrite(r.reg)
        fin = asyncio.ensure_future(tools._finalize_engagement(
            r.rec, outcome="completed", text="done", next_steps=[], driver=r.drv))
        await _wait(held.started.is_set, "the completion write")
        reply = asyncio.ensure_future(_route_reply(r.ch, 
            _reply_update("one more thing", 501), str(CHAT)))
        await asyncio.sleep(0.01)
        assert not reply.done()                  # its promotion is waiting
        assert r.drv.inbound_unread_depth(r.rec.id) == 0
        held.release.set()
        await fin
        assert await reply is True
        held.restore()
        await _drain(r)
        assert r.rec.status == "completed"
        assert r.rec.terminal_notification_pending is False
        assert r.bus.notify.await_count == 0
        assert r.delivered == []
        assert [(m["text"], m["reply_to"]) for m in r.bot.replies()] == [
            (ENDED_REPLY_NOTICE, 7)]

    async def test_a_reply_racing_a_quiet_completion_that_rolls_back_is_delivered(
            self, tmp_path, monkeypatch):
        r = await _rig(tmp_path, monkeypatch, quiet=True)
        r.reg.record_job_post(r.rec.id, A(501))
        await _drain(r)
        held = _HeldWrite(r.reg)
        held.fail = True
        transition = asyncio.ensure_future(r.reg.try_transition_terminal(
            r.rec.id, "completed", strict=True))
        await _wait(held.started.is_set, "the completion write")
        reply = asyncio.ensure_future(_route_reply(r.ch, 
            _reply_update("one more thing", 501), str(CHAT)))
        await asyncio.sleep(0.01)
        assert not reply.done()                  # its promotion is waiting
        held.release.set()
        with pytest.raises(OSError):
            await transition
        held.fail = False
        assert await reply is True
        held.restore()
        await _drain(r)
        assert r.rec.status == "active"
        assert r.rec.origin["job"]["quiet"] is False
        assert r.delivered and r.delivered[0].endswith("one more thing")
        assert r.bot.replies() == []

    async def test_a_reply_ends_a_quiet_runs_quietness_so_its_end_is_told(
            self, tmp_path, monkeypatch):
        import tools
        r = await _rig(tmp_path, monkeypatch, quiet=True)
        r.reg.record_job_post(r.rec.id, A(501))
        assert await _route_reply(r.ch, 
            _reply_update("also check May", 501), str(CHAT)) is True
        await _drain(r)
        assert r.rec.origin["job"]["quiet"] is False
        await tools._finalize_engagement(
            r.rec, outcome="completed", text="done", next_steps=[], driver=r.drv)
        assert r.rec.terminal_notification_pending is True
        assert r.bus.notify.await_count == 1

    async def test_a_completion_entering_while_a_promotion_commits_owes_the_telling(
            self, tmp_path, monkeypatch):
        """RACE: a promotion is queued on the registry lock ahead of a
        completing funnel (another write holds the lock). The owed-telling
        decision is made inside the transition, from the stamp the promotion
        committed — a decision taken before the funnel waited for the lock
        would still read the run as quiet."""
        import tools
        r = await _rig(tmp_path, monkeypatch, quiet=True)
        held = _HeldWrite(r.reg)
        other = asyncio.ensure_future(r.reg.persist_origin(r.rec.id))
        await _wait(held.started.is_set, "another write holding the lock")
        promotion = asyncio.ensure_future(r.reg.promote_from_quiet(r.rec.id))
        await asyncio.sleep(0)
        fin = asyncio.ensure_future(tools._finalize_engagement(
            r.rec, outcome="completed", text="done", next_steps=[], driver=r.drv))
        await asyncio.sleep(0)
        assert r.rec.origin["job"]["quiet"] is True     # nothing promoted yet
        held.release.set()
        await other
        assert await promotion is True
        await fin
        held.restore()
        assert r.rec.status == "completed"
        assert r.rec.terminal_notification_pending is True
        assert r.bus.notify.await_count == 1


class TestPendingTerminalRoutesIntoTheJob:
    async def test_a_reply_routed_while_a_terminal_write_is_pending_is_delivered_after_a_rollback(
            self, tmp_path, monkeypatch):
        r = await _rig(tmp_path, monkeypatch)
        r.reg.record_job_post(r.rec.id, A(501))
        await _drain(r)
        held = _HeldWrite(r.reg)
        held.fail = True
        transition = asyncio.ensure_future(r.reg.try_transition_terminal(
            r.rec.id, "completed", strict=True))
        await _wait(held.started.is_set, "the terminal write")
        reply = asyncio.ensure_future(_route_reply(r.ch, 
            _reply_update("wait", 501), str(CHAT)))
        # Routed: the reply waits for the pending write to settle before the
        # job accepts it.
        await asyncio.sleep(0.01)
        assert not reply.done()
        held.release.set()
        with pytest.raises(OSError):
            await transition
        held.fail = False
        assert await reply is True
        held.restore()
        await _drain(r)
        assert r.rec.status == "active"
        assert r.delivered and r.delivered[0].endswith("wait")
        assert r.bot.replies() == []


# ---------------------------------------------------------------------------
# interleavings at the edges of the seams
# ---------------------------------------------------------------------------


class _FailFirstWrite(_HeldWrite):
    """Hold the first tombstone write until released, fail it, and let every
    later write through."""

    def __init__(self, reg):
        super().__init__(reg)
        self.fail = True
        self.calls = 0

    def _write(self, snapshot):
        self.calls += 1
        if self.calls > 1:
            self.real(snapshot)
            return
        super()._write(snapshot)


class TestSeamEdges:
    async def test_a_cancel_tap_during_another_pending_terminal_write_still_cancels(
            self, tmp_path, monkeypatch):
        """RACE: a completion's terminal write is pending when the operator
        taps Cancel; the write fails and rolls back. The Cancel waits for it
        and then cancels the job — the tap is not dropped."""
        import tools
        r = await _rig(tmp_path, monkeypatch)
        held = _FailFirstWrite(r.reg)
        fin = asyncio.ensure_future(tools._finalize_engagement(
            r.rec, outcome="completed", text="done", next_steps=[], driver=r.drv))
        await _wait(held.started.is_set, "the completion write")
        cq = _CQ(f"v1|job_cancel|{r.rec.id}", 900)
        await r.ch._on_inline_callback(SimpleNamespace(callback_query=cq))
        held.release.set()
        assert await fin is tools.FinalizeResult.PERSIST_FAILED
        await _drain(r)
        held.restore()
        assert cq.answers == ["Cancelling…"]
        assert r.rec.status == "cancelled"
        assert r.bus.notify.await_count == 1
        assert r.bot.replies() == []

    async def test_a_cancel_tap_losing_to_a_committed_completion_ends_quietly(
            self, tmp_path, monkeypatch):
        import tools
        r = await _rig(tmp_path, monkeypatch)
        held = _HeldWrite(r.reg)
        fin = asyncio.ensure_future(tools._finalize_engagement(
            r.rec, outcome="completed", text="done", next_steps=[], driver=r.drv))
        await _wait(held.started.is_set, "the completion write")
        cq = _CQ(f"v1|job_cancel|{r.rec.id}", 900)
        await r.ch._on_inline_callback(SimpleNamespace(callback_query=cq))
        held.release.set()
        assert await fin is tools.FinalizeResult.FINALIZED
        await _drain(r)
        held.restore()
        assert r.rec.status == "completed"
        assert r.bus.notify.await_count == 1         # the completion's telling
        assert r.bot.replies() == []
        assert r.rec.id not in r.ch._job_cancels

    async def test_a_question_asked_while_a_promotion_write_fails_keeps_the_run_told(
            self, tmp_path, monkeypatch):
        """RACE: a reply's promotion has cleared a quiet run's stamp in memory
        and its write is in flight when the job asks; the write fails and
        rolls the stamp back. The question's own promotion waits for that
        outcome and promotes again, so the run that asked is told at its
        end."""
        import tools
        r = await _rig(tmp_path, monkeypatch, quiet=True)
        r.reg.record_job_post(r.rec.id, A(501))
        await _drain(r)
        held = _FailFirstWrite(r.reg)
        reply = asyncio.ensure_future(_route_reply(r.ch, 
            _reply_update("one more thing", 501), str(CHAT)))
        await _wait(held.started.is_set, "the reply's promotion write")
        assert r.rec.origin["job"]["quiet"] is False      # provisional
        ask = asyncio.ensure_future(_ask(r))
        await asyncio.sleep(0.01)
        held.release.set()
        assert await reply is True
        out = await ask
        held.restore()
        await _drain(r)
        assert out["status"] == "asked"
        assert r.rec.origin["job"]["quiet"] is False
        # the reply whose promotion failed was told once; the question posted
        assert [(m["text"], m["reply_to"]) for m in r.bot.replies()] == [
            ("Your message didn't get through. Could you send it again?", 7)]
        questions = [m for m in r.bot.sent if "Which account?" in m["text"]]
        assert len(questions) == 1
        await tools._finalize_engagement(
            r.rec, outcome="completed", text="done", next_steps=[], driver=r.drv)
        assert r.rec.terminal_notification_pending is True
        assert r.bus.notify.await_count == 1


    async def test_a_reply_arriving_while_a_question_promotion_write_fails_keeps_the_run_told(
            self, tmp_path, monkeypatch):
        """RACE, the other way round: the job's question cleared the stamp in
        memory and its write fails; a reply that arrived meanwhile waits for
        that outcome and promotes again, so the run it reached is told at its
        end."""
        import tools
        r = await _rig(tmp_path, monkeypatch, quiet=True)
        r.reg.record_job_post(r.rec.id, A(501))
        await _drain(r)
        held = _FailFirstWrite(r.reg)
        ask = asyncio.ensure_future(_ask(r))
        await _wait(held.started.is_set, "the question's promotion write")
        assert r.rec.origin["job"]["quiet"] is False      # provisional
        reply = asyncio.ensure_future(_route_reply(r.ch, 
            _reply_update("one more thing", 501), str(CHAT)))
        await asyncio.sleep(0.01)
        held.release.set()
        assert (await ask)["kind"] == "not_asked"
        assert await reply is True
        held.restore()
        await _drain(r)
        assert r.delivered and r.delivered[0].endswith("one more thing")
        assert r.rec.origin["job"]["quiet"] is False
        await tools._finalize_engagement(
            r.rec, outcome="completed", text="done", next_steps=[], driver=r.drv)
        assert r.rec.terminal_notification_pending is True
        assert r.bus.notify.await_count == 1


class TestApprovalLostToTermination:
    async def test_an_approval_continuation_refused_by_an_ended_job_is_told_once(
            self, tmp_path, monkeypatch):
        """RACE: the approval tap commits, then the job ends before the
        card's finish hook dispatches the continuation. The seam tells the
        operator once, in reply to the card; the card shows no second
        failure line."""
        import authz_grants
        import tools
        import verdict_broker
        from authz_grants import ChallengeCoordinator, GrantKey, GrantStore, canonical_args_hash
        broker = verdict_broker.VerdictBroker()
        monkeypatch.setattr(verdict_broker, "BROKER", broker)
        monkeypatch.setattr(authz_grants, "_AWAITING_APPROVAL", {})
        r = await _rig(tmp_path, monkeypatch)
        coord = ChallengeCoordinator()
        monkeypatch.setattr(tools, "CHALLENGES", coord)
        key = GrantKey(operator_id=OPERATOR, bot_id=BOT, chat_id=CHAT, enforcement_role="finance",
                       artifact_id="a1", tool_name="purge",
                       args_hash=canonical_args_hash({"x": 1}), engagement_id=r.rec.id)
        handle = coord.get_or_create(
            key, chat_id=CHAT, operator_id=OPERATOR, target_role="finance",
            tool_name="purge", canonical_json='{"x":1}', enforcement_role="finance",
            channel=r.ch, grants=GrantStore(), engagement_id=r.rec.id)
        assert await handle.settled_post() == "posted"
        card = r.bot.sent[-1]["message_id"]
        gate = asyncio.Event()
        real_edit = r.bot.edit_message_text

        async def held_edit(*a, **kw):
            await gate.wait()
            return await real_edit(*a, **kw)
        r.bot.edit_message_text = held_edit
        ch = coord._entries[key]
        claim = broker.claim(namespace="resident_ask", scope=ch.scope,
                             request_id=ch.rid, option_index=0, actor_id=OPERATOR)
        assert broker.commit(claim) is True
        ch.req.meta["on_commit_sync"](0)
        # The finish hook is waiting on the approval edit; the job ends.
        await r.reg.try_transition_terminal(r.rec.id, "cancelled", strict=True)
        gate.set()
        await broker.drain_hooks()
        await _drain(r)
        assert r.delivered == []
        assert [(m["text"], m["reply_to"]) for m in r.bot.replies()] == [
            ("The job had already finished, so your answer wasn't used.", card)]
        edits = [e["text"] for e in r.bot.edits if e["message_id"] == card]
        assert edits and edits[0].startswith("✅ Approved")
        assert not any("could not be resumed" in e for e in edits), edits
        assert r.drv.inbound_reservations(r.rec.id) == 0
