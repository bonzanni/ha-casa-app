"""A job the operator asks for reads like a chat: 👍 on his request when it starts, and one
answer in reply to that request when it ends.

The REAL ``TelegramChannel`` endpoints behind the REAL transport, the REAL
``EngagementRegistry`` and Casa's start and outcome code; a fake Bot API that records what
was sent, reacted to and edited. Every assertion counts what each bot did."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import tools as tools_mod
from bus import BusMessage, MessageType
from channels.tg_address import TgAddress
from channels.telegram import TelegramChannel
from engagement_registry import JOB_POST_PARKED, JOB_POST_QUESTION, EngagementRegistry
from specialist_registry import DelegationComplete
from task_model import Requester
from telegram_channel_double import make_transport

OPERATOR = 100
BOT = 111              # Casa's bot: the resident's chat
SPECIALIST_BOT = 222   # the specialist's own bot
REQUEST_MID = 77       # the operator's message that asked for the job
REQUEST = [BOT, OPERATOR, REQUEST_MID]


class _Refused(Exception):
    """Telegram refusing a reaction."""


class _Bot:
    """The Bot API calls a job's start and end make, recorded."""

    def __init__(self, *, refuse_reactions: bool = False):
        self.sent: list[dict] = []
        self.reactions: list[tuple] = []
        self.markups: list[dict] = []
        self.refuse_reactions = refuse_reactions
        self._next = 900

    async def send_message(self, **kw):
        self.sent.append(kw)
        self._next += 1
        return SimpleNamespace(message_id=self._next)

    async def set_message_reaction(self, *, chat_id, message_id, reaction):
        if self.refuse_reactions:
            raise _Refused("REACTION_INVALID: the request's text")
        self.reactions.append((chat_id, message_id))
        return True

    async def edit_message_reply_markup(self, **kw):
        self.markups.append(kw)
        return True


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """Casa's endpoint (bot 111, the resident's) and the specialist's (bot 222), both
    serving the operator, over a fresh engagement registry."""
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    resident_bot, specialist_bot = _Bot(), _Bot()
    casa = TelegramChannel(bot=resident_bot, chat_id=str(OPERATOR), default_agent="assistant",
                           is_casa=True, known_bot_id=BOT)
    finance = TelegramChannel(bot=specialist_bot, chat_id=str(OPERATOR),
                              default_agent="finance", is_casa=False,
                              known_bot_id=SPECIALIST_BOT)
    for ch in (casa, finance):
        ch._engagement_registry = reg
    manager = make_transport(casa, finance, casa=casa)
    monkeypatch.setattr(tools_mod, "_engagement_registry", reg, raising=False)
    monkeypatch.setattr(tools_mod, "_channel_manager", manager, raising=False)
    monkeypatch.setattr(tools_mod, "_display_name_for_role", lambda role: "Finance")

    async def job(*, started_by="operator", request=REQUEST, delivery_bot=BOT):
        origin = {"role": "assistant", "channel": "telegram", "chat_id": str(OPERATOR),
                  "user_id": OPERATOR, "_tg_bot": BOT,
                  "job": {"title": "Q3 accounting", "started_by": started_by}}
        if request is not None:
            origin["_tg_request"] = list(request)
        return await reg.create("specialist", "finance", "in_casa", "t", origin,
                                executor="finance",
                                requester=Requester("assistant", {"_tg_bot": BOT}),
                                delivery=TgAddress(delivery_bot, OPERATOR))

    return SimpleNamespace(reg=reg, casa=casa, finance=finance, bot=resident_bot,
                           resident_bot=resident_bot, specialist_bot=specialist_bot,
                           transport=manager.get("telegram"), job=job)


# -- the start ---------------------------------------------------------------------------

async def test_an_operator_started_job_puts_a_thumbs_up_on_his_request_and_posts_no_line(rig):
    rec = await rig.job()
    await tools_mod._acknowledge_job_start(rec.id)
    assert rig.bot.reactions == [(OPERATOR, REQUEST_MID)] and rig.bot.sent == []
    assert rig.reg.get(rec.id).dm_posts == [] and rig.reg.get(rec.id).ui_posts == []


async def test_a_job_whose_reaction_fails_posts_the_silent_start_line_without_a_keyboard(
        rig, caplog):
    rig.bot.refuse_reactions = True
    rec = await rig.job()
    with caplog.at_level(logging.WARNING):
        await tools_mod._acknowledge_job_start(rec.id)
    assert rig.bot.reactions == []
    (s,) = rig.bot.sent
    assert s["text"] == "📊 Finance started «Q3 accounting»." and s["disable_notification"]
    assert "reply_markup" not in s and s["reply_parameters"].message_id == REQUEST_MID
    assert s["reply_parameters"].allow_sending_without_reply is True
    assert rig.reg.get(rec.id).ui_posts == [] and len(rig.reg.get(rec.id).dm_posts) == 1
    # the line is the job's post: a reply quoting it reaches the job
    assert rig.reg.job_post_owner(TgAddress(BOT, OPERATOR, 901)) is rec
    # the failure is logged with its class only
    assert [r.getMessage() for r in caplog.records
            if "reaction" in r.getMessage()] == ["reaction failed: _Refused"]


async def test_a_job_without_a_stamped_request_posts_the_start_line(rig):
    rec = await rig.job(request=None)
    await tools_mod._acknowledge_job_start(rec.id)
    assert rig.bot.reactions == []
    (s,) = rig.bot.sent
    assert s["text"] == "📊 Finance started «Q3 accounting»." and "reply_parameters" not in s
    assert len(rig.reg.get(rec.id).dm_posts) == 1


async def test_a_request_on_a_bot_with_no_endpoint_posts_the_start_line_once(rig):
    rec = await rig.job(request=[333, OPERATOR, REQUEST_MID])
    await tools_mod._acknowledge_job_start(rec.id)
    assert rig.bot.reactions == [] and rig.specialist_bot.reactions == []
    (s,) = rig.bot.sent
    assert "reply_parameters" not in s           # not this bot's message: no reply


@pytest.mark.parametrize("started_by", ["scheduled", "agent"])
async def test_a_scheduled_job_gets_neither_reaction_nor_line(rig, started_by):
    """Nor does one an agent started on its own: neither was asked for in the chat."""
    rec = await rig.job(started_by=started_by)
    await tools_mod._acknowledge_job_start(rec.id)
    assert rig.bot.reactions == [] and rig.bot.sent == []
    assert rig.specialist_bot.reactions == [] and rig.specialist_bot.sent == []


async def test_before_the_bot_has_an_id_no_start_line_is_posted(rig):
    rec = await rig.job(request=None)
    rig.casa._known_bot_id = None
    assert not rig.casa.has_bot_id
    assert await rig.casa.post_job_start_line(rec) is None
    assert rig.bot.sent == [] and rig.reg.get(rec.id).dm_posts == []


# -- the end -----------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["completed", "cancelled", "error"])
async def test_a_job_end_posts_no_end_line_and_clears_its_question_buttons(rig, status):
    rig.bot.refuse_reactions = True
    rec = await rig.job()
    await tools_mod._acknowledge_job_start(rec.id)          # the fallback start line
    assert rig.reg.record_job_post(rec.id, TgAddress(BOT, OPERATOR, 950),
                                   keyboard=JOB_POST_QUESTION)
    sent_before = len(rig.bot.sent)
    await rig.reg.try_transition_terminal(rec.id, status, strict=True)
    rig.transport.retire_job_keyboards(rec)
    await asyncio.gather(*rig.transport._job_ui_tasks)
    assert [m["message_id"] for m in rig.bot.markups] == [950]
    assert len(rig.bot.sent) == sent_before
    assert not any("Finished" in s.get("text", "") for s in rig.bot.sent)
    assert rig.reg.get(rec.id).ui_posts == []


class _Bus:
    def __init__(self):
        self.notified: list[BusMessage] = []

    async def notify(self, msg):
        self.notified.append(msg)


def _outcome(origin: dict) -> DelegationComplete:
    return DelegationComplete(delegation_id="e" * 32, agent="finance", status="ok",
                              text="Q3 is reconciled.", origin=origin, elapsed_s=60.0)


async def test_the_end_notice_names_his_request_as_its_reply_target(rig):
    rec = await rig.job()
    bus = _Bus()
    await tools_mod.send_engagement_outcome(
        bus, complete=_outcome(rec.origin), origin=rec.origin, ack=None)
    (notified,) = bus.notified
    assert notified.context["_reply_to"] == [BOT, OPERATOR, REQUEST_MID]
    # an outcome whose origin names no request carries no reply target
    bus = _Bus()
    await tools_mod.send_engagement_outcome(
        bus, complete=_outcome({"role": "assistant", "_tg_bot": BOT}),
        origin={"role": "assistant", "_tg_bot": BOT}, ack=None)
    assert "_reply_to" not in bus.notified[0].context


def test_the_reply_target_is_the_first_address_the_origin_holds():
    other = [BOT, OPERATOR, 5]
    assert tools_mod.reply_target({"_tg_request": REQUEST}) == {"_reply_to": REQUEST}
    assert tools_mod.reply_target({"_reply_to": other, "_tg_request": REQUEST}) == {
        "_reply_to": other}
    assert tools_mod.reply_target({"_reply_to": "nope", "_tg_request": REQUEST}) == {
        "_reply_to": REQUEST}
    assert tools_mod.reply_target({"_tg_request": [BOT, OPERATOR]}) == {}
    assert tools_mod.reply_target({}) == {}


def _notice(context: dict) -> BusMessage:
    return BusMessage(type=MessageType.NOTIFICATION, source="finance", target="assistant",
                      content=_outcome({"role": "assistant", "_tg_bot": BOT,
                                        "user_text": "do Q3"}),
                      channel="telegram", context=context)


def test_the_narration_of_a_job_end_is_told_to_open_with_done(rig):
    import agent
    base = {"cid": "-", "chat_id": str(OPERATOR), "engagement_id": "e" * 32, "_tg_bot": BOT}
    turn = agent.Agent._synthesize_delegation_turn(Mock(), _notice({**base, "_reply_to": REQUEST}))
    assert agent.REPLY_OPENING_NOTE in turn.content
    assert turn.context["_reply_to"] == [BOT, OPERATOR, REQUEST_MID]
    # the resident's answer goes out as a reply to his request, on the bot it came to
    reply = rig.casa._reply_parameters(turn.context, OPERATOR)
    assert reply.message_id == REQUEST_MID and reply.allow_sending_without_reply is True
    # no "_reply_to": the note is absent
    plain = agent.Agent._synthesize_delegation_turn(Mock(), _notice(base))
    assert agent.REPLY_OPENING_NOTE not in plain.content and "_reply_to" not in plain.context


def test_a_launch_whose_outcome_was_not_recorded_is_not_told_how_to_open_an_end():
    """The job is still open: the narration is not asked to open "Done.", "Stopped." or
    "Couldn't finish:"."""
    import agent
    context = {"cid": "-", "chat_id": str(OPERATOR), "engagement_id": "e" * 32,
               "_tg_bot": BOT, "_reply_to": REQUEST}
    complete = DelegationComplete(
        delegation_id="e" * 32, agent="finance", status="error",
        kind="launch_outcome_uncommitted", message="not recorded", text="",
        origin={"role": "assistant", "_tg_bot": BOT, "user_text": "do Q3"}, elapsed_s=1.0)
    msg = BusMessage(type=MessageType.NOTIFICATION, source="finance", target="assistant",
                     content=complete, channel="telegram", context=context)
    turn = agent.Agent._synthesize_delegation_turn(Mock(), msg)
    assert "the job is still open" in turn.content
    assert agent.REPLY_OPENING_NOTE not in turn.content
    error = DelegationComplete(
        delegation_id="e" * 32, agent="finance", status="error", kind="error",
        message="boom", text="", origin=complete.origin, elapsed_s=1.0)
    turn = agent.Agent._synthesize_delegation_turn(
        Mock(), BusMessage(type=MessageType.NOTIFICATION, source="finance",
                           target="assistant", content=error, channel="telegram",
                           context=context))
    assert agent.REPLY_OPENING_NOTE in turn.content


def test_the_reply_opening_note_is_casas_fixed_copy():
    import agent
    assert agent.REPLY_OPENING_NOTE == (
        "(Casa: your answer goes to the person as a reply to their request. Open it with "
        "\"Done.\" when the work finished, \"Stopped.\" when it was stopped, or "
        "\"Couldn't finish:\" and why when it did not finish.)")


# -- parking -----------------------------------------------------------------------------

async def test_a_parked_job_replies_to_his_request(rig):
    rec = await rig.job()
    mid = await rig.transport.post_job_parked(rec, "«Q3 accounting» stopped without finishing.")
    (s,) = rig.bot.sent
    assert s["reply_parameters"].message_id == REQUEST_MID
    assert s["reply_parameters"].allow_sending_without_reply is True
    assert rig.reg.get(rec.id).ui_posts == [(TgAddress(BOT, OPERATOR, mid), JOB_POST_PARKED)]


# -- across bots -------------------------------------------------------------------------

async def test_a_job_asked_on_another_bot_reacts_there_and_parks_as_a_new_post(rig):
    rec = await rig.job(delivery_bot=SPECIALIST_BOT)
    await tools_mod._acknowledge_job_start(rec.id)
    assert rig.resident_bot.reactions == [(OPERATOR, REQUEST_MID)]
    assert rig.specialist_bot.reactions == []
    assert rig.resident_bot.sent == [] and rig.specialist_bot.sent == []
    await rig.transport.post_job_parked(rec, "«Q3 accounting» stopped without finishing.")
    assert "reply_parameters" not in rig.specialist_bot.sent[0]          # the parked line
    assert rig.resident_bot.sent == []


async def test_a_failed_reaction_across_bots_posts_the_line_in_the_delivery_chat_unquoted(rig):
    rig.resident_bot.refuse_reactions = True
    rec = await rig.job(delivery_bot=SPECIALIST_BOT)
    await tools_mod._acknowledge_job_start(rec.id)
    assert rig.resident_bot.sent == []
    (s,) = rig.specialist_bot.sent
    # the specialist's own chat: no label, and no reply to another bot's message
    assert s["text"] == "Started «Q3 accounting»." and "reply_parameters" not in s
    assert "reply_markup" not in s
