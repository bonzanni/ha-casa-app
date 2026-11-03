"""A specialist that keeps no memory posted to the operator's chat: memory
keeps one body-free Casa line that it did (INV-MEM-040).

Fido writes no memory (``memory.token_budget`` 0, INV-MEM-032): his answer,
Casa's notice carrying it and the echo lines in the resident's Casa notes are
all kept out of the store. Without a line of its own, the writer saw a request
handed to him and then nothing, and kept the errand open. So when a message
Casa sends on such a specialist's behalf lands in the operator's chat, one
event is captured: "<name> posted to your chat.", Casa's words, with the time.
Never the post's body, caption, file name or buttons. Every page of one post,
and a re-landing of it, is one event.

The sends are the channel's real ones over a fake bot, against a real store.
"""
from __future__ import annotations

import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import result_broker as rb
import tools
from channels import DeliveryOutcome
from test_memory_capture import captured_events
from test_memory_capture import mem  # noqa: F401 — fixture
from test_no_retain_specialist import _cfg

pytestmark = [pytest.mark.asyncio]

OPERATOR_CHAT = 42
CANARY = secrets.token_hex(8)


@pytest.fixture
def roles(monkeypatch):
    m = {"browser": _cfg("browser", "specialist", 0),
         "finance": _cfg("finance", "specialist", 4000),
         "assistant": _cfg("assistant", "resident", 5000)}
    m["browser"].character.name = "Fido"
    monkeypatch.setattr(tools, "_agent_role_map", m, raising=False)
    return m


def _channel(chat_id: str = str(OPERATOR_CHAT)):
    from channels.telegram import TelegramChannel
    ch = TelegramChannel(bot=MagicMock(), chat_id=chat_id,
                         default_agent="assistant")
    ch._app = MagicMock()
    ids = iter(range(100, 200))

    async def _send(**kw):
        return SimpleNamespace(message_id=next(ids))
    ch._app.bot.send_message = AsyncMock(side_effect=_send)
    return ch


def _post(role: str = "browser", **over) -> rb.PostRecord:
    base = dict(role=role, operator_id=OPERATOR_CHAT, plugin="browser",
                slot="answer", tool_use_id="call-1", owner="d-1",
                posted_at=1000.0, kind=rb.OPERATOR_MESSAGE)
    base.update(over)
    return rb.PostRecord(**base)


def _message(body: str) -> str:
    return f"📊 Fido\n{body}"


async def test_a_no_memory_specialist_s_post_leaves_one_casa_line(mem, roles):
    ch = _channel()
    out = await ch.deliver_operator_message(
        OPERATOR_CHAT, _message(f"The pool opens at 7 {CANARY}"), post=_post())
    assert out is DeliveryOutcome.DELIVERED
    rows = await captured_events(mem)
    assert [(e["speaker_kind"], e["origin_kind"], e["role"], e["text"])
            for e in rows] == [
        ("casa", "system", "assistant", "Fido posted to your chat.")]
    [row] = rows
    assert row["channel"] == "telegram" and row["origin_tier"] == "private"
    assert row["speaker_id"] == "browser"
    assert row["source_key"].startswith("post:")
    assert not any(CANARY in str(v) for v in row.values())


async def test_every_page_of_one_post_and_a_re_landing_are_one_event(
        mem, roles):
    ch = _channel()
    post = _post()
    for page in ("one", "two", "three"):
        await ch.deliver_operator_page(OPERATOR_CHAT, _message(page),
                                       post=post)
    # The same post landing again (a re-delivery of it) adds nothing.
    await ch.deliver_operator_message(OPERATOR_CHAT, _message("again"),
                                      post=_post())
    # A second post is a second line.
    await ch.deliver_operator_message(OPERATOR_CHAT, _message("next"),
                                      post=_post(tool_use_id="call-2"))
    rows = await captured_events(mem)
    assert [e["text"] for e in rows] == ["Fido posted to your chat."] * 2


async def test_a_desk_reply_and_a_relayed_answer_leave_a_line_too(
        mem, roles):
    ch = _channel()
    for post in (_post(plugin="", slot="desk", tool_use_id="t-1",
                       owner="t-1", kind="desk_reply"),
                 _post(plugin="", slot="relay", tool_use_id="d-9",
                       owner="relay:d-9", kind="relayed_answer")):
        from output_boundary import casa_text
        out = await ch.send_response(casa_text(_message(f"hi {CANARY}")),
                                     {"chat_id": OPERATOR_CHAT, "_post": post})
        assert out is DeliveryOutcome.DELIVERED
    rows = await captured_events(mem)
    assert [e["text"] for e in rows] == ["Fido posted to your chat."] * 2
    assert not any(CANARY in e["text"] for e in rows)


async def test_nothing_for_a_specialist_that_keeps_memory_or_another_chat(
        mem, roles):
    ch = _channel()
    await ch.deliver_operator_message(
        OPERATOR_CHAT, "📊 Finance\nsaved", post=_post(role="finance"))
    await ch.deliver_operator_message(7, _message("elsewhere"),
                                      post=_post(tool_use_id="call-3"))
    await ch.deliver_operator_message(OPERATOR_CHAT, "a plain reply")
    unconfigured = _channel(chat_id="")
    await unconfigured.deliver_operator_message(
        OPERATOR_CHAT, _message("open mode"), post=_post(tool_use_id="c-4"))
    assert await captured_events(mem) == []


async def test_a_role_no_longer_loaded_counts_as_keeping_no_memory(
        mem, roles):
    ch = _channel()
    await ch.deliver_operator_message(
        OPERATOR_CHAT, "📊 gone\nhi", post=_post(role="gone"))
    rows = await captured_events(mem)
    assert [e["text"] for e in rows] == ["gone posted to your chat."]


async def test_a_capture_that_fails_never_fails_the_send(
        mem, roles, monkeypatch):
    from memory import capture

    def boom(**kw):
        raise RuntimeError("no")

    monkeypatch.setattr(capture, "event", boom)
    ch = _channel()
    out = await ch.deliver_operator_message(
        OPERATOR_CHAT, _message("still sent"), post=_post())
    assert out is DeliveryOutcome.DELIVERED
    assert await captured_events(mem) == []


async def test_a_link_posted_as_a_button_leaves_a_line(mem, roles):
    ch = _channel()
    url = f"https://live.example/{CANARY}"
    post = _post(slot="live_view", kind=rb.OPERATOR_LINK)
    out = await ch.deliver_operator_link(
        OPERATOR_CHAT, "📊 Fido\nlive view", [], f"📊 Fido\n{url}", post=post,
        button=("📊 Fido\nlive view", "Open (live.example)", url))
    assert out is DeliveryOutcome.DELIVERED
    rows = await captured_events(mem)
    assert [e["text"] for e in rows] == ["Fido posted to your chat."]
    assert not any(CANARY in str(v) for e in rows for v in e.values())
