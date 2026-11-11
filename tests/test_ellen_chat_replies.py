"""In the resident's chat every reply is the resident's turn, unless it answers
a live task's post, which goes into that task. Nothing in the chat reaches a
specialist or the configurator because of who posted the message it quotes:
not a Casa card, not a specialist's post that belongs to no task (one posted
before an upgrade, or any message no task index knows), not a file sent as a
reply to one. A reply to an ended task's post is the resident's turn with the
task's end in its note, and a "/new" sent as a reply resets the resident's
conversation like any other "/new".

The channel, its text and file handlers and its post filing (``_record_post``)
are real; the task index is the shared double, and the roles behind the quoted
posts are configured delegates of the resident, so a route that looked the
poster up would take them.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest

import result_broker as rb
from channels.tg_address import TgAddress
from task_model import Requester
from task_posts import TaskRef
from test_own_chat_routing import (  # noqa: F401 — ``world`` is a fixture
    CASA_BOT, OPERATOR, _arm, _endpoint, _file, _handle, _job, _land, _text, _turns, world)

pytestmark = pytest.mark.asyncio

RESIDENT = "assistant"


@pytest.fixture
def delegates(monkeypatch):
    """Finance (a specialist) and the configurator are loaded and declared
    delegates of the resident."""
    import tools
    roles = {"finance": types.SimpleNamespace(kind="specialist", name="finance"),
             "configurator": types.SimpleNamespace(kind="executor", name="configurator")}
    monkeypatch.setattr(tools, "_agent_role_map", roles)
    monkeypatch.setattr(tools, "declares_delegate",
                        lambda resident, role: resident == RESIDENT and role in roles)
    return roles


def _post_of_no_task(ch, mid, role, slot):
    """A post of *role* landing in the chat outside any task, filed as it lands."""
    ch._record_post(OPERATOR, types.SimpleNamespace(message_id=mid),
                    rb.PostRecord(role=role, operator_id=OPERATOR, plugin="probe", slot=slot,
                                  tool_use_id="tu", owner="", posted_at=1.0, kind=slot))


def _from_bot(update):
    """The quoted message was sent by the resident's bot."""
    update.message.reply_to_message.from_user = types.SimpleNamespace(id=CASA_BOT)
    return update


def _from_operator(update):
    update.message.reply_to_message.from_user = types.SimpleNamespace(id=OPERATOR)
    return update


def _ended_job(ch, world, mid):
    rec = types.SimpleNamespace(
        id="e-2", role_or_type="finance", status="completed", task="t",
        started_at=1000.0, completed_at=1060.0,
        origin={"user_id": OPERATOR, "chat_id": str(OPERATOR), "job": {"title": "Q3 check"}})
    ch._engagement_registry = types.SimpleNamespace(get=lambda i: rec if i == "e-2" else None)
    world.index.ended[TgAddress(ch.bot_id, OPERATOR, mid)] = TaskRef(
        "job", "e-2", "finance", Requester(RESIDENT, {}))


def _her_own_message(ch, world):
    return _from_bot(_text("and the other one?", reply_to=31, quoted_text="Done."))


def _a_casa_card(ch, world):
    _post_of_no_task(ch, 32, "configurator", "approval")
    return _from_bot(_text("why that version?", reply_to=32, quoted_text="Install Finance?"))


def _a_pre_upgrade_specialist_post(ch, world):
    _post_of_no_task(ch, 33, "finance", "operator_proposal")
    return _from_bot(_text("and March?", reply_to=33, quoted_text="📊 Finance\nQ3 report"))


def _a_message_no_index_knows(ch, world):
    return _from_bot(_text("and March?", reply_to=34, quoted_text="📊 Finance\nQ2 report"))


def _the_operators_own_message(ch, world):
    return _from_operator(_text("I meant this", reply_to=35, quoted_text="the bank file"))


def _an_ended_delegations_post(ch, world):
    world.index.delegation("d-1", "finance")
    _land(ch, 640, "d-1", "finance")
    world.index.end("d-1")
    return _text("and now?", reply_to=640)


def _an_ended_jobs_post(ch, world):
    _ended_job(ch, world, 501)
    return _text("and now?", reply_to=501)


ELLENS = {
    "her own message": (_her_own_message, "one of your earlier messages"),
    "a Casa card": (_a_casa_card, "one of your earlier messages"),
    "a pre-upgrade specialist post": (_a_pre_upgrade_specialist_post,
                                      "one of your earlier messages"),
    "a message no index knows": (_a_message_no_index_knows, "one of your earlier messages"),
    "the operator's own message": (_the_operators_own_message, "their own earlier message"),
    "an ended delegation's post": (_an_ended_delegations_post,
                                   "a message from Finance's earlier task, which has ended"),
    "an ended job's post": (_an_ended_jobs_post, "a message from Finance's background job"),
}


@pytest.mark.parametrize("case", list(ELLENS))
async def test_a_reply_to_anything_but_a_live_tasks_post_is_the_residents_turn(
        world, delegates, case):
    build, who = ELLENS[case]
    ch, bus = _endpoint("casa")
    ch.deliver_operator_reply = AsyncMock(return_value=True)
    update = build(ch, world)
    words = update.message.text
    await _handle(ch, update)
    [turn] = _turns(bus, RESIDENT)
    assert turn.target == RESIDENT and turn.content == words
    assert f"a reply to {who}" in turn.context["_reply_note"]
    for role in delegates:
        assert _turns(bus, role) == []
    assert world.inputs.calls == [] and ch.deliver_operator_reply.await_count == 0
    assert ch._turn_tasks == set()                 # no other turn was started for it


async def test_a_reply_to_a_live_delegations_post_goes_into_the_delegation(world, delegates):
    ch, bus = _endpoint("casa")
    world.index.delegation("d-1", "finance")
    _land(ch, 640, "d-1", "finance")
    await _handle(ch, _text("the second one", reply_to=640))
    assert world.inputs.calls == [("d-1", "[replying to «📊 Finance»]\nthe second one")]
    assert _turns(bus, RESIDENT) == []


async def test_a_reply_to_a_live_jobs_post_goes_into_the_job(world, delegates):
    ch, bus = _endpoint("casa")
    rec = _job(ch, world, "finance", 501)
    ch.deliver_operator_reply = AsyncMock(return_value=True)
    await _handle(ch, _text("use the second account", reply_to=501))
    args, _kw = ch.deliver_operator_reply.await_args
    assert args == (rec, "use the second account")
    assert _turns(bus, RESIDENT) == []


@pytest.mark.parametrize("quoted", [32, 33])
async def test_a_new_sent_as_a_reply_resets_the_residents_conversation(world, delegates, quoted):
    from unittest.mock import patch
    ch, bus = _endpoint("casa")
    _post_of_no_task(ch, 32, "configurator", "approval")
    _post_of_no_task(ch, 33, "finance", "operator_proposal")
    ch._session_registry = object()
    with patch("session_registry.reset_channel", new=AsyncMock()) as reset:
        await _handle(ch, _from_bot(_text("/new", reply_to=quoted)))
    assert reset.await_count == 1
    assert [m["text"] for m in ch._app.bot.sent] == [
        "Starting fresh — I still remember what matters."]
    assert _turns(bus, RESIDENT) == [] and ch._turn_tasks == set()


async def test_a_file_replying_to_a_post_of_no_task_is_the_residents(world, delegates):
    ch, _bus = _endpoint("casa")
    _post_of_no_task(ch, 33, "finance", "operator_proposal")
    await ch._on_non_text_message(types.SimpleNamespace(message=_file(reply_to=33)))
    assert list(world.inboxes) == [RESIDENT]               # never the specialist's inbox
    assert world.grants == [] and world.inputs.calls == [] and ch._turn_tasks == set()


async def test_a_file_after_arming_a_card_of_no_task_is_the_residents(world, delegates):
    ch, _bus = _endpoint("casa")
    _arm(ch, ("conversation", "finance"))
    assert ch._file_address(OPERATOR, types.SimpleNamespace(id=OPERATOR), _file()) is None
    assert OPERATOR not in ch._armings
