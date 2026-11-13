"""S6 §2.1 / §5 — a file follows the reply rule: a file for a live task's post goes to
the task; every other file in Ellen's chat is Ellen's, byte for byte (INV-FILE-006) — a
reply on a specialist's post and a 📎 arming of a card of no live task included. An
arming is spent by the next file whatever that file's route."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace as NS

import pytest

import agent_inbox as ai
import result_broker as rb
from test_inbound_files import (OPERATOR, PDF, _channel, _doc, _msg, _replies, _update,  # noqa: F401
                                wired)

pytestmark = pytest.mark.asyncio
FIN = "finance"


@pytest.fixture
async def routed(wired, tmp_path, monkeypatch, fake_telegram_bot):
    root = str(tmp_path / "agent-inbox")
    ai._inboxes[FIN] = ai.open_inbox(FIN, root)                       # what wire() registers
    fetched = []

    async def fetch(bot, file_id, cap):
        fetched.append(file_id)
        return PDF
    monkeypatch.setattr(ai, "fetch_telegram_file", fetch)
    ch = _channel(fake_telegram_bot)
    # a specialist's post of no task landed in the chat at message 11
    ch._record_post(OPERATOR, NS(message_id=11),
                    rb.PostRecord(role=FIN, operator_id=OPERATOR, plugin="probe", slot="report",
                                  tool_use_id="call-1", owner="", posted_at=1.0))
    notices = []

    async def notice(chat_id, text):
        notices.append(text)
        return True
    ch.deliver_casa_notice = notice
    ch._start_typing = lambda *a, **k: None
    return NS(ch=ch, notices=notices, fetched=fetched, bot=fake_telegram_bot, root=root)


def _reply_msg(name="statement-q3.pdf", quoted_id=11, caption=None, **attach):
    m = _msg(document=_doc(name), **attach)
    m.reply_to_message = NS(message_id=quoted_id, text="📊 Finance\nQ3 report", caption=None)
    m.caption = caption
    return m


def _plain_msg(name="statement-q3.pdf", **attach):
    m = _msg(document=_doc(name), **attach)
    m.reply_to_message = None
    m.caption = None
    return m


async def _settle(ch):
    for t in list(getattr(ch, "_turn_tasks", ())):
        await t
    await asyncio.sleep(0)


async def test_an_unaddressed_file_takes_todays_path_byte_for_byte(routed, wired):
    await routed.ch._on_non_text_message(_update(_plain_msg()))
    assert _replies(routed.bot) == [] and len(routed.bot.reactions) == 1   # stored: 👍
    assert len(wired.list_files()) == 1 and ai.get_inbox(FIN).list_files() == []
    assert routed.notices == []


@pytest.mark.parametrize("name", ["statement-q3.pdf", "notes.docx"])
async def test_a_file_replying_to_a_specialists_post_is_ellens_byte_for_byte(routed, wired, name):
    """The same outcome as an unaddressed file: Ellen's inbox and her
    acknowledgement (or her refusal of a kind she cannot take), never the
    specialist's inbox, no Casa notice, no other turn."""
    await routed.ch._on_non_text_message(_update(_reply_msg(name=name)))
    await _settle(routed.ch)
    plain_bot = type(routed.bot)()
    plain = _channel(plain_bot)
    await plain._on_non_text_message(_update(_plain_msg(name=name)))
    assert _replies(routed.bot) == _replies(plain_bot)
    assert routed.bot.reactions == plain_bot.reactions
    assert ai.get_inbox(FIN).list_files() == [] and routed.notices == []
    assert routed.ch._turn_tasks == set()


async def test_an_armed_file_for_a_card_of_no_task_is_ellens_and_spends_the_arming(routed, wired):
    routed.ch._armings[OPERATOR] = {"chat_id": OPERATOR, "operator_id": OPERATOR, "role": FIN,
                                    "artifact_id": "art", "expires_at": time.monotonic() + 600,
                                    "dest": ("conversation", FIN)}
    await routed.ch._on_non_text_message(_update(_plain_msg(name="a.pdf")))
    await _settle(routed.ch)
    assert OPERATOR not in routed.ch._armings                            # consumed
    assert len(wired.list_files()) == 1 and ai.get_inbox(FIN).list_files() == []
    assert routed.notices == [] and routed.ch._turn_tasks == set()


async def test_an_expired_arming_is_ellens(routed, wired):
    routed.ch._armings[OPERATOR] = {"chat_id": OPERATOR, "operator_id": OPERATOR, "role": FIN,
                                    "artifact_id": "art", "expires_at": time.monotonic() - 1,
                                    "dest": ("conversation", FIN)}
    await routed.ch._on_non_text_message(_update(_plain_msg(name="a.pdf")))
    assert len(wired.list_files()) == 1 and routed.notices == []


async def test_a_reply_file_spends_a_live_arming(routed, wired):
    """INV-FILE-007: the arming arms the NEXT file; a reply file is that next file
    and spends it, so no later file follows the arming."""
    routed.ch._armings[OPERATOR] = {"chat_id": OPERATOR, "operator_id": OPERATOR, "role": "records",
                                    "artifact_id": "art", "expires_at": time.monotonic() + 600,
                                    "dest": ("conversation", "records")}
    await routed.ch._on_non_text_message(_update(_reply_msg(name="a.pdf")))
    assert OPERATOR not in routed.ch._armings
    assert len(wired.list_files()) == 1 and ai.get_inbox(FIN).list_files() == []
