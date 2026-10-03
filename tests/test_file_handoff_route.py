"""S6 §2.1 / §5 — a file follows the reply rule: addressed by a swipe-reply on a retained
specialist post or by a live arming, judged under the intake lock, stored in THAT
specialist's inbox and started as one desk turn; every other file is Ellen's, byte for
byte (INV-FILE-001)."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import agent_inbox as ai
import result_broker as rb
import specialist_desk as sd
from test_inbound_files import (OPERATOR, PDF, _channel, _doc, _msg, _replies, _update,  # noqa: F401
                                wired)

pytestmark = pytest.mark.asyncio
FIN = "finance"


def _record(**over):
    base = dict(role=FIN, operator_id=OPERATOR, plugin="probe", slot="report",
                tool_use_id="call-1", owner="d-1", posted_at=1.0)
    base.update(over)
    return rb.PostRecord(**base)


@pytest.fixture
async def routed(wired, tmp_path, monkeypatch, fake_telegram_bot):
    root = str(tmp_path / "agent-inbox")
    ai._inboxes[FIN] = ai.open_inbox(FIN, root)                       # what wire() registers
    pm = rb.PostMap()
    monkeypatch.setattr(rb, "POST_MAP", pm)
    pm.record(OPERATOR, 11, _record())
    monkeypatch.setattr(sd, "DESKS", sd.DeskRegistry())
    ok = {"finance": True}
    monkeypatch.setattr(sd, "desk_target_ok", lambda resident, role: ok.get(role, False))
    monkeypatch.setattr(sd, "label_for", lambda role: "📊 Finance" if role == FIN else role)
    spawned = AsyncMock(return_value=None)
    monkeypatch.setattr(sd, "handle_reply", spawned)
    echoes = []
    monkeypatch.setattr(sd, "record_echo", lambda chat, line: echoes.append(line))
    fetched = []

    async def fetch(bot, file_id, cap):
        fetched.append(file_id)
        if env.slow is not None:
            await env.slow.wait()
        return PDF
    monkeypatch.setattr(ai, "fetch_telegram_file", fetch)
    ch = _channel(fake_telegram_bot)
    notices = []

    async def notice(chat_id, text):
        notices.append(text)
        return True
    ch.deliver_desk_notice = notice
    ch._start_typing = lambda *a, **k: None
    env = NS(ch=ch, pm=pm, ok=ok, spawned=spawned, echoes=echoes, notices=notices,
             fetched=fetched, bot=fake_telegram_bot, slow=None, root=root)
    return env


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


async def test_a_reply_on_a_specialist_post_stores_in_that_inbox_and_starts_one_desk_turn(routed, wired):
    await routed.ch._on_non_text_message(_update(_reply_msg(caption="the Q3 invoice")))
    await _settle(routed.ch)
    assert _replies(routed.bot) == []                                   # no channel reply of its own
    [stored] = ai.get_inbox(FIN).list_files()
    assert stored.display_name == "statement-q3.pdf" and wired.list_files() == []
    assert routed.spawned.await_count == 1
    kw = routed.spawned.await_args.kwargs
    assert kw["desk_role"] == FIN and kw["file_name"] == "statement-q3.pdf"
    assert kw["text"].startswith("[casa file] The operator sent you a file: statement-q3.pdf")
    assert "The operator wrote: the Q3 invoice" in kw["text"]
    assert kw["record"] is not None and stored.path in kw["text"]


async def test_an_unaddressed_file_takes_todays_path_byte_for_byte(routed, wired):
    await routed.ch._on_non_text_message(_update(_plain_msg()))
    [reply] = _replies(routed.bot)
    assert reply.startswith("Got statement-q3.pdf") and "7 days" in reply
    assert len(wired.list_files()) == 1 and ai.get_inbox(FIN).list_files() == []
    assert routed.spawned.await_count == 0 and routed.notices == []


async def test_an_addressed_file_for_a_removed_delegate_is_refused_never_ellens(routed, wired):
    routed.ok[FIN] = False
    await routed.ch._on_non_text_message(_update(_reply_msg()))
    assert routed.fetched == [] and wired.list_files() == [] and ai.get_inbox(FIN).list_files() == []
    assert routed.notices == ["📊 Finance could not take your file (not delegable)."]
    assert routed.echoes == routed.notices and _replies(routed.bot) == []


async def test_an_addressed_unsupported_kind_draws_the_labelled_refusal_and_no_download(routed, wired):
    await routed.ch._on_non_text_message(_update(_reply_msg(name="notes.docx")))
    assert routed.fetched == [] and wired.list_files() == []
    assert routed.notices == ["📊 Finance could not take notes.docx: it was not a kind it can read."]
    assert _replies(routed.bot) == []


async def test_delegability_lost_during_the_download_keeps_the_file_and_starts_no_turn(routed):
    routed.slow = asyncio.Event()
    task = asyncio.create_task(routed.ch._on_non_text_message(_update(_reply_msg())))
    await asyncio.sleep(0.05)
    routed.ok[FIN] = False                                               # a reload during the fetch
    routed.slow.set()
    await task
    [stored] = ai.get_inbox(FIN).list_files()
    assert routed.spawned.await_count == 0
    assert routed.notices == ["📊 Finance stored statement-q3.pdf in its inbox but was not delegable at the post-download check."]


async def test_a_full_desk_keeps_the_file_and_tells_as_a_past_event(routed, monkeypatch):
    desk = sd.DESKS.get_or_create(OPERATOR, FIN)
    monkeypatch.setattr(desk, "reserve", lambda: None)
    await routed.ch._on_non_text_message(_update(_reply_msg()))
    assert len(ai.get_inbox(FIN).list_files()) == 1 and routed.spawned.await_count == 0
    assert routed.notices == ["📊 Finance stored statement-q3.pdf in its inbox; its desk was full when the place was requested."]


async def test_an_armed_file_is_routed_once_and_a_second_file_is_ellens(routed, wired):
    routed.ch._armings[OPERATOR] = {"chat_id": OPERATOR, "operator_id": OPERATOR, "role": FIN,
                                    "artifact_id": "art", "expires_at": time.monotonic() + 600}
    await routed.ch._on_non_text_message(_update(_plain_msg(name="a.pdf")))
    await _settle(routed.ch)
    assert routed.spawned.await_count == 1 and routed.spawned.await_args.kwargs["record"] is None
    assert routed.spawned.await_args.kwargs["file_name"] == "a.pdf"
    assert OPERATOR not in routed.ch._armings                            # consumed
    await routed.ch._on_non_text_message(_update(_plain_msg(name="b.pdf")))
    assert routed.spawned.await_count == 1 and len(wired.list_files()) == 1


async def test_an_expired_arming_is_ellens_and_an_armed_docx_consumes_the_arming(routed, wired):
    routed.ch._armings[OPERATOR] = {"chat_id": OPERATOR, "operator_id": OPERATOR, "role": FIN,
                                    "artifact_id": "art", "expires_at": time.monotonic() - 1}
    await routed.ch._on_non_text_message(_update(_plain_msg(name="a.pdf")))
    assert len(wired.list_files()) == 1 and routed.spawned.await_count == 0
    routed.ch._armings[OPERATOR] = {"chat_id": OPERATOR, "operator_id": OPERATOR, "role": FIN,
                                    "artifact_id": "art", "expires_at": time.monotonic() + 600}
    await routed.ch._on_non_text_message(_update(_plain_msg(name="x.docx")))
    assert OPERATOR not in routed.ch._armings
    assert routed.notices[-1] == "📊 Finance could not take x.docx: it was not a kind it can read."


async def test_two_files_to_one_specialist_are_queued_in_sending_order(routed):
    gate = asyncio.Event()
    routed.slow = gate                                                   # the first download blocks…
    first = asyncio.create_task(routed.ch._on_non_text_message(_update(_reply_msg(name="first.pdf"))))
    await asyncio.sleep(0.05)
    routed.slow = None                                                   # …the second would be fast
    second = asyncio.create_task(routed.ch._on_non_text_message(_update(_reply_msg(name="second.pdf"))))
    await asyncio.sleep(0.05)
    assert routed.spawned.await_count == 0                               # but waits under the intake lock
    gate.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    names = [c.kwargs["file_name"] for c in routed.spawned.await_args_list]
    assert names == ["first.pdf", "second.pdf"]


async def test_a_reply_on_a_post_retained_for_another_operator_is_not_addressed(routed, wired):
    routed.pm.record(OPERATOR, 12, _record(operator_id=99))
    await routed.ch._on_non_text_message(_update(_reply_msg(quoted_id=12)))
    assert routed.spawned.await_count == 0 and ai.get_inbox(FIN).list_files() == []
    [reply] = _replies(routed.bot)
    assert reply.startswith("Got statement-q3.pdf") and len(wired.list_files()) == 1   # today's path


async def test_a_swipe_reply_file_spends_a_live_arming_and_still_goes_to_the_replied_specialist(routed, wired):
    """INV-FILE-003 (diff round 1, Terra): the arming arms the NEXT file; a swipe-reply file
    is that next file — the reply decides where it goes, and the arming is spent, so a later
    unaddressed file is the default agent's again."""
    routed.ch._armings[OPERATOR] = {"chat_id": OPERATOR, "operator_id": OPERATOR, "role": "records",
                                    "artifact_id": "art", "expires_at": time.monotonic() + 600}
    await routed.ch._on_non_text_message(_update(_reply_msg(name="a.pdf")))
    await _settle(routed.ch)
    assert routed.spawned.await_count == 1
    assert routed.spawned.await_args.kwargs["file_name"] == "a.pdf"
    assert len(ai.get_inbox(FIN).list_files()) == 1                     # the reply decided
    assert OPERATOR not in routed.ch._armings                            # and the arming is spent
    await routed.ch._on_non_text_message(_update(_plain_msg(name="b.pdf")))
    assert routed.spawned.await_count == 1 and len(wired.list_files()) == 1   # b.pdf is Ellen's
