"""An answer sent as a reply to the message Casa names.

Two reserved context keys carry Telegram message addresses
(``[bot_id, chat_id, message_id]``): ``_tg_request`` is the operator's own
inbound message, stamped at ingress and copied into the turn's origin;
``_reply_to`` is a reply target Casa chose. When a delivery context carries a
``_reply_to`` on this endpoint's bot and in the chat being sent to, the first
physical message of the answer is a Telegram reply to it — sent even when the
target is gone — and every later page is a plain message.
"""
from __future__ import annotations

import types
from unittest.mock import patch

import pytest
from telegram.error import BadRequest

import agent as agent_mod
from bus import MessageType
from channels.tg_address import TgAddress
from provenance import sanitize_external_context
from telegram_reply_harness import BOT, OPERATOR, _channel, _settle
from test_agent_process import FakeClient, _make_agent, patch_retry_sleep
from test_output_boundary_channel import _admitted
from test_telegram_new_reset import _drain_bus

OTHER_BOT = BOT + 1


def _two_pages(rich: bool) -> str:
    """A text ``render_paged`` delivers as two pages."""
    return (("**bold** " * 60 if rich else "") + "word " * 1000).strip()


def _sends(ch) -> list[dict]:
    return ch._app.bot.sent


@pytest.mark.parametrize("rich", [True, False], ids=["rich", "plain"])
async def test_a_reply_target_makes_the_first_page_a_reply(rich):
    ch, _bus = _channel()
    ctx = {"chat_id": OPERATOR, "_reply_to": [BOT, OPERATOR, 7]}
    await ch.send_response(_admitted(_two_pages(rich)), ctx)
    sends = _sends(ch)
    assert len(sends) == 2
    assert sends[0]["reply_parameters"].message_id == 7
    assert sends[0]["reply_parameters"].allow_sending_without_reply is True
    assert "reply_parameters" not in sends[1]


async def test_a_single_plain_page_carries_the_target():
    # no entities and no post record: send_response hands it to send()
    ch, _bus = _channel()
    await ch.send_response(_admitted("All filed."), {"chat_id": OPERATOR,
                                                    "_reply_to": [BOT, OPERATOR, 7]})
    (sent,) = _sends(ch)
    assert sent["reply_parameters"].message_id == 7
    assert sent["reply_parameters"].allow_sending_without_reply is True


async def test_a_single_rich_page_carries_the_target_on_its_plain_retry_too():
    ch, _bus = _channel()
    bot = ch._app.bot
    real = bot.send_message

    async def refuse_entities(**kw):
        if "entities" in kw:
            raise BadRequest("can't parse entities")
        return await real(**kw)
    bot.send_message = refuse_entities
    await ch.send_response(_admitted("**All** filed."), {"chat_id": OPERATOR,
                                                        "_reply_to": [BOT, OPERATOR, 7]})
    (sent,) = _sends(ch)
    assert sent["text"] == "**All** filed."
    assert sent["reply_parameters"].message_id == 7


async def test_a_task_posts_single_page_carries_the_target():
    ch, _bus = _channel()
    post = types.SimpleNamespace(owner="")
    await ch.send_response(_admitted("All filed."), {
        "chat_id": OPERATOR, "_reply_to": [BOT, OPERATOR, 7], "_post": post})
    (sent,) = _sends(ch)
    assert sent["reply_parameters"].message_id == 7


async def test_send_puts_the_target_on_its_first_chunk_only():
    ch, _bus = _channel()
    await ch.send(_admitted("line\n" * 1500), {"chat_id": str(OPERATOR),
                                               "_reply_to": [BOT, OPERATOR, 7]})
    sends = _sends(ch)
    assert len(sends) == 2
    assert sends[0]["reply_parameters"].message_id == 7
    assert "reply_parameters" not in sends[1]


@pytest.mark.parametrize("target", [
    [OTHER_BOT, OPERATOR, 7],          # another bot's message: this bot cannot reply to it
    [BOT, OPERATOR + 1, 7],            # another chat's message
    [BOT, OPERATOR, None],             # a chat, not a message
    ["casa", OPERATOR, 7],             # a route, never an address
    "7",                               # not an address at all
])
async def test_a_target_on_another_bot_or_chat_is_ignored(target):
    ch, _bus = _channel()
    await ch.send_response(_admitted(_two_pages(True)), {"chat_id": OPERATOR,
                                                        "_reply_to": target})
    await ch.send(_admitted("plain"), {"chat_id": OPERATOR, "_reply_to": target})
    sends = _sends(ch)
    assert len(sends) == 3
    assert all("reply_parameters" not in s for s in sends)


async def test_no_target_sends_plain_messages():
    ch, _bus = _channel()
    await ch.send_response(_admitted("All filed."), {"chat_id": OPERATOR})
    (sent,) = _sends(ch)
    assert "reply_parameters" not in sent


def test_reply_parameters_is_none_without_a_target():
    ch, _bus = _channel()
    assert ch._reply_parameters({"chat_id": OPERATOR}, OPERATOR) is None
    params = ch._reply_parameters({"_reply_to": [BOT, OPERATOR, 9]}, str(OPERATOR))
    assert params.message_id == 9 and params.allow_sending_without_reply is True


# ---------------------------------------------------------------------------
# The operator's request, stamped at ingress
# ---------------------------------------------------------------------------

def _user(uid):
    return types.SimpleNamespace(first_name="Nicola", id=uid)


def _update(*, sender=OPERATOR, message_id=70, text="file the Q3 invoices"):
    message = types.SimpleNamespace(text=text, message_id=message_id, reply_to_message=None,
                                    from_user=_user(sender), sender_chat=None)
    return types.SimpleNamespace(message=message,
                                 effective_chat=types.SimpleNamespace(id=str(OPERATOR)),
                                 effective_user=_user(sender))


async def _operator_turn(message_id=70):
    ch, bus = _channel()
    await ch._handle(_update(message_id=message_id), None)
    await _settle(ch)
    (turn,) = await _drain_bus(bus)
    return turn


async def test_an_operator_message_stamps_its_address_on_the_turn():
    turn = await _operator_turn(message_id=70)
    assert turn.context["_tg_request"] == [BOT, OPERATOR, 70]
    assert TgAddress.from_json(turn.context["_tg_request"]) == TgAddress(BOT, OPERATOR, 70)


async def test_another_senders_message_carries_no_request():
    ch, bus = _channel()
    update = _update(sender=7)
    assert await ch._dispatch_dm_turn(update, str(OPERATOR), "hello",
                                      user=update.effective_user) is True
    (turn,) = await _drain_bus(bus)
    assert "_tg_request" not in turn.context


def test_request_and_reply_keys_are_reserved():
    assert sanitize_external_context({"_tg_request": [1, 2, 3], "_reply_to": [1, 2, 3]}) == {}
    assert "_tg_request" in agent_mod.COPIED_CONTEXT_MARKERS


class _Capturing(FakeClient):
    """The SDK client of a real turn: keeps the origin bound while it ran."""
    origins: list[dict] = []

    async def query(self, text):
        type(self).origins.append(dict(agent_mod.origin_var.get(None) or {}))
        await super().query(text)

    async def disconnect(self):
        return None


async def test_the_request_reaches_the_turns_origin(tmp_path):
    turn = await _operator_turn(message_id=70)
    assert turn.type is MessageType.CHANNEL_IN
    FakeClient.reset()
    _Capturing.origins = []
    agent = _make_agent(tmp_path)
    try:
        with patch("sdk_client_pool._default_make_client", _Capturing), patch_retry_sleep():
            await agent.handle_message(turn)
    finally:
        await agent.aclose()
    (origin,) = _Capturing.origins
    assert origin["_tg_request"] == [BOT, OPERATOR, 70]
