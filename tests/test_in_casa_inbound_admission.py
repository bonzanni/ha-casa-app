"""#649 — the job's admission-ticket ledger and the Telegram seams' admission.

INV-ENG-003 for the job session: an operator message is admitted at the seam,
synchronously — journaled, ticketed and queued before anything is awaited —
and its ticket vetoes a successful completion until the CLI takes it up. A
recognized command is consumed by the handler and never ticketed. A message
the job does not accept draws one bounded visible outcome (the visibility
law) and leaves nothing behind. When the job ends with a message it never
read, the journal tells that message as lost, once.

REAL ``InCasaDriver`` + REAL registry + the REAL Telegram handler seams;
event-driven coordination only — no sleeps, never ``<module>.asyncio.sleep``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from turn_scripted_session import TurnScriptedSession, attach_session

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def _mk_assistant(text: str):
    from claude_agent_sdk import AssistantMessage, TextBlock
    try:
        return AssistantMessage(content=[TextBlock(text=text)], model="m")
    except TypeError:
        m = AssistantMessage.__new__(AssistantMessage)
        m.content = [TextBlock(text=text)]
        return m


class _ScriptedClient(TurnScriptedSession):
    """query() records prompts; receive_response() yields the next scripted
    frame list. ``on_query`` lets a test observe ledger state AT the
    hand-off instant (the pre-query-move mutation pin)."""

    def __init__(self, scripts=None, on_query=None):
        self.query_prompts: list[str] = []
        self.close_calls = 0
        self.closed = False
        self.scripts = list(scripts or [])
        self.on_query = on_query

    async def record_query(self, prompt):
        if self.closed:
            raise RuntimeError("client closed")
        if self.on_query is not None:
            self.on_query(prompt)
        self.query_prompts.append(prompt)

    def receive_response(self):
        frames = self.scripts.pop(0) if self.scripts else []

        async def _gen():
            for f in frames:
                yield f
        return _gen()

    async def close(self):
        self.close_calls += 1
        self.closed = True


class _Stream:
    async def emit(self, text):
        pass

    async def finalize(self, text):
        pass


async def _mk_driver_rec(tmp_path, client):
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    rec = await reg.create(
        "executor", "configurator", "in_casa", "t",
        {"role": "assistant", "channel": "telegram", "user_id": 77},
        topic_id=555)
    drv = InCasaDriver(topic_stream_factory=lambda tid: _Stream(),
                       record_lookup=reg.get,
                       begin_turn_delivery=reg.begin_turn_delivery)
    attach_session(drv, rec, client)
    return reg, rec, drv


# ===========================================================================
# 1. ledger unit behavior
# ===========================================================================


class TestLedger:
    async def test_exact_token_discharge_with_duplicate_texts(self, tmp_path):
        reg, rec, drv = await _mk_driver_rec(tmp_path, _ScriptedClient())
        # Synchronous from here to the last assertion: the delivery worker
        # does not run, so only the ledger moves.
        t1 = drv.submit_operator(rec, "same text")
        t2 = drv.submit_operator(rec, "same text")
        assert drv.inbound_unread_depth(rec.id) == 2
        drv.discharge_inbound(rec.id, t1)
        assert drv.inbound_unread_depth(rec.id) == 1
        assert drv.inbound_unread_texts(rec.id) == ["same text"]
        drv.discharge_inbound(rec.id, t1)          # idempotent no-op
        assert drv.inbound_unread_depth(rec.id) == 1
        assert drv.inbound_token_held(rec.id, t2) is True
        drv.discharge_inbound(rec.id, t2)
        assert drv.inbound_unread_depth(rec.id) == 0

    async def test_strict_return_types(self, tmp_path):
        reg, rec, drv = await _mk_driver_rec(tmp_path, _ScriptedClient())
        assert type(drv.inbound_unread_depth(rec.id)) is int
        assert type(drv.inbound_unread_texts(rec.id)) is list
        assert not hasattr(drv, "inbound_in_flight_blocking")
        assert not hasattr(drv, "force_completion_turn_boundary")
        # C1/#663: the continuation window needs something for the gate to be
        # vetoed on. What the gate demands of the reservation accessors is
        # pinned here: STRICT int, because the reads are `type(x) is int` and
        # a duck driver must read as "nothing held" rather than fabricate a
        # depth.
        assert type(drv.inbound_reservations(rec.id)) is int
        assert type(drv.inbound_message_reservations(rec.id)) is int


# ===========================================================================
# 2. Telegram seams — admission placement + visibility law
# ===========================================================================


def _mk_update(*, chat_id, text, thread_id=None, user_id=77):
    u = MagicMock()
    u.message = MagicMock()
    u.message.chat = MagicMock()
    u.message.chat.id = chat_id
    u.message.text = text
    u.message.message_thread_id = thread_id
    u.message.from_user = MagicMock(id=user_id)
    u.message.message_id = 999
    return u


async def _mk_channel(tmp_path, fake_telegram_bot, client):
    """A real channel over the real driver and registry, the job's session
    being *client*."""
    from channels.telegram import TelegramChannel

    reg, rec, drv = await _mk_driver_rec(tmp_path, client)
    ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                         engagement_supergroup_id=-1001)
    ch._engagement_registry = reg
    ch._engagement_driver = drv
    ch._post_engagement_notice = AsyncMock()
    return ch, reg, rec, drv


async def _drain(ch):
    """Wait until the job's delivery worker has written everything queued and
    the session has taken every write up.

    Bounded: work that never drains is a defect to surface, not to hang the
    suite on — so it RAISES with what is left named."""
    drv = ch._engagement_driver
    for _ in range(400):
        queued = [len(lane.queue) for lane in drv._lanes.values()]
        written = [len(s.written) for s in drv._sessions.values()]
        if not any(queued) and not any(written):
            await asyncio.sleep(0)
            return
        await asyncio.sleep(0.005)
    raise AssertionError(
        f"job delivery did not drain — queued={queued} written={written}")


class _HeldClient(_ScriptedClient):
    """A session whose transport holds every write until ``release`` is set."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.release = asyncio.Event()

    async def record_query(self, prompt):
        await self.release.wait()
        await super().record_query(prompt)


class TestManualSeamAdmission:
    async def test_recognized_commands_are_never_ticketed(
            self, tmp_path, fake_telegram_bot):
        """The command cut: /cancel is consumed by the handler, not
        delivered — no ticket at any point (its own ungated finalize must
        not disclose it as unread)."""
        client = _ScriptedClient()
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._finalize_cancel = AsyncMock(return_value=True)
        seen = []
        real_submit = drv.submit_operator
        drv.submit_operator = lambda r, text, **kw: (
            seen.append(text), real_submit(r, text, **kw))[1]

        u = _mk_update(chat_id=-1001, text="/cancel", thread_id=555,
                       user_id=77)
        await ch.handle_update(u)
        await _drain(ch)
        assert seen == []
        assert drv.inbound_unread_depth(rec.id) == 0
        ch._finalize_cancel.assert_awaited_once()

    async def test_unknown_and_other_bot_slash_texts_are_admitted(
            self, tmp_path, fake_telegram_bot):
        """/foo and /cancel@otherbot are DELIVERABLE and
        are admitted like plain text, before the handler returns."""
        for text in ("/foo now", "/cancel@otherbot"):
            client = _HeldClient(scripts=[[_mk_assistant("ok")]])
            ch, reg, rec, drv = await _mk_channel(
                tmp_path, fake_telegram_bot, client)
            ch._bot_username = "casabot"

            u = _mk_update(chat_id=-1001, text=text, thread_id=555,
                           user_id=77)
            await asyncio.wait_for(ch.handle_update(u), 5)
            assert drv.inbound_unread_depth(rec.id) == 1, text
            client.release.set()
            await _drain(ch)
            assert client.query_prompts == [text]
            assert drv.inbound_unread_depth(rec.id) == 0

    async def test_command_persist_failure_posts_retry_notice(
            self, tmp_path, fake_telegram_bot):
        """Sol seam r2 pin: a falsy finalize with the record STILL LIVE
        posts the retry notice; an already-terminal record stays silent."""
        client = _ScriptedClient()
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._finalize_cancel = AsyncMock(return_value=False)   # falsy, live
        u = _mk_update(chat_id=-1001, text="/cancel", thread_id=555,
                       user_id=77)
        await ch.handle_update(u)
        notices = [c.args[1] for c in
                   ch._post_engagement_notice.await_args_list]
        assert sum("could not be finalized" in n for n in notices) == 1


class TestSystemSeamAdmission:
    async def test_a_continuation_is_admitted_before_the_session_reads_it(
            self, tmp_path, fake_telegram_bot):
        """The consent-continuation window: the seam returns with the ticket
        already admitted while the session has not read the message — a
        completion racing the write sees depth 1."""
        client = _HeldClient(scripts=[[_mk_assistant("ok")]])
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)

        accepted = await asyncio.wait_for(
            ch.deliver_system_turn(rec, "[consent granted] continue"), 5)
        assert accepted is True
        assert drv.inbound_unread_depth(rec.id) == 1
        assert client.query_prompts == []
        client.release.set()
        await _drain(ch)
        assert client.query_prompts == ["[consent granted] continue"]
        assert drv.inbound_unread_depth(rec.id) == 0

    async def test_a_terminal_record_refuses_without_leak(
            self, tmp_path, fake_telegram_bot):
        client = _ScriptedClient()
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        await reg.mark_error(rec.id, kind="x", message="dead")
        accepted = await ch.deliver_system_turn(rec, "resume")
        await _drain(ch)
        import job_inbox
        assert accepted is False
        assert drv.inbound_unread_depth(rec.id) == 0
        assert job_inbox.load(rec.id) == []
        assert client.query_prompts == []
        notices = [c.args[1] for c in
                   ch._post_engagement_notice.await_args_list]
        assert sum("could not be delivered" in n for n in notices) == 1

    async def test_a_journal_failure_applies_visibility_law(
            self, tmp_path, fake_telegram_bot, monkeypatch):
        """A message whose journal entry cannot be written was not accepted:
        one notice attempt, and no ticket left behind (a held one would veto
        every completion for ever)."""
        import job_inbox
        client = _ScriptedClient()
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)

        def _refuse(*a, **kw):
            raise job_inbox.JournalError("disk full")
        monkeypatch.setattr(job_inbox, "append", _refuse)
        accepted = await ch.deliver_system_turn(rec, "resume")
        assert accepted is False
        assert drv.inbound_unread_depth(rec.id) == 0
        notices = [c.args[1] for c in
                   ch._post_engagement_notice.await_args_list]
        assert sum("could not be delivered" in n for n in notices) == 1

    async def test_stop_gate_refuses_new_delivery(
            self, tmp_path, fake_telegram_bot):
        """Once _stopping is set, the seam accepts
        nothing and the message is told, not leaked."""
        client = _ScriptedClient()
        ch, reg, rec, drv = await _mk_channel(
            tmp_path, fake_telegram_bot, client)
        ch._stopping = True
        accepted = await ch.deliver_system_turn(rec, "resume")
        await _drain(ch)
        assert accepted is False
        assert client.query_prompts == []
        assert drv.inbound_unread_depth(rec.id) == 0
        notices = [c.args[1] for c in
                   ch._post_engagement_notice.await_args_list]
        assert sum("could not be delivered" in n for n in notices) == 1


# ===========================================================================
# 3. an ungated terminal and a message the job never read
# ===========================================================================


class TestUngatedTerminalKeepsTheTicket:
    async def test_cancel_finalize_lists_nothing_and_tells_the_message_lost_once(
            self, tmp_path):
        """A non-gated finalize (cancel) over a journaled message the job
        never read lists no inbound message in its own output; the journal's
        terminal branch tells that one message as lost, once, and leaves no
        ticket and no journal entry behind."""
        import agent as agent_mod
        import job_inbox
        from tools import _finalize_engagement, init_tools

        client = _HeldClient()                 # the session never reads it
        reg, rec, drv = await _mk_driver_rec(tmp_path, client)
        tch = MagicMock()
        tch.send_to_topic = AsyncMock()
        tch.send_response_to_topic = AsyncMock()
        tch.close_topic = AsyncMock()
        tch.tell_journal_lost = AsyncMock()
        cm = MagicMock()
        cm.get.return_value = tch
        bus = MagicMock()
        bus.notify = AsyncMock()
        init_tools(
            channel_manager=cm, bus=bus,
            specialist_registry=MagicMock(), mcp_registry=MagicMock(),
            trigger_registry=MagicMock(), engagement_registry=reg,
        )
        try:
            token = drv.submit_operator(rec, "approval resume that died")
            fin = await _finalize_engagement(
                rec, outcome="cancelled", text="",
                next_steps=[], driver=drv)
            assert bool(fin) is True
            posted = "".join(
                str(c.args) + str(c.kwargs)
                for c in (list(tch.send_to_topic.call_args_list)
                          + list(tch.send_response_to_topic.call_args_list)))
            assert "approval resume that died" not in posted
            assert "inbound message(s)" not in posted
            assert tch.tell_journal_lost.await_count == 1
            told = tch.tell_journal_lost.await_args.args[1]
            assert told["text"] == "approval resume that died"
            assert drv.inbound_token_held(rec.id, token) is False
            assert job_inbox.load(rec.id) == []
        finally:
            client.release.set()
            agent_mod.active_engagement_driver = None
