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


# ---------------------------------------------------------------------------
# The agent's own <reply/> marker
# ---------------------------------------------------------------------------

def _marked(text: str):
    """*text* admitted as a final reply, the way a turn's answer reaches the
    channel: a leading marker is gone from the text and kept as ``reply``."""
    from output_boundary import IntentKind
    from output_boundary_testing import admitted
    return admitted(text, IntentKind.FINAL_REPLY)


_ASKED = {"chat_id": OPERATOR, "_tg_request": [BOT, OPERATOR, 9],
          "_operator_words": "Mon and Tue?"}


async def test_a_marked_answer_replies_to_his_message_with_the_quote():
    ch, _bus = _channel()
    await ch.send_response(_marked('<reply quote="Tue"/>Tue works.'), dict(_ASKED))
    sends = _sends(ch)
    assert len(sends) == 1
    assert sends[0]["text"] == "Tue works."
    rp = sends[0]["reply_parameters"]
    assert (rp.message_id, rp.quote) == (9, "Tue")
    assert rp.allow_sending_without_reply is True


async def test_a_bare_marker_replies_without_a_quote():
    ch, _bus = _channel()
    await ch.send_response(_marked("<reply/>**Both** work."), dict(_ASKED))
    (sent,) = _sends(ch)
    assert sent["text"] == "Both work."
    assert (sent["reply_parameters"].message_id, sent["reply_parameters"].quote) == (9, None)


@pytest.mark.parametrize("quote", ["Tuesday", "T" * 1025], ids=["not-his-words", "too-long"])
async def test_a_quote_not_in_his_message_gives_a_plain_reply(quote):
    ch, _bus = _channel()
    ctx = dict(_ASKED, _operator_words="Mon and Tue? " + "T" * 1025)
    await ch.send_response(_marked(f'<reply quote="{quote}"/>Tue works.'), ctx)
    (sent,) = _sends(ch)
    assert sent["reply_parameters"].message_id == 9
    assert sent["reply_parameters"].quote is None


@pytest.mark.parametrize("text", ['<reply quote="Tue"/>Tue works.',
                                  '<reply quote="Tue"/>**Tue** works.'], ids=["plain", "rich"])
async def test_a_refused_quote_is_resent_without_it(text):
    ch, _bus = _channel()
    bot = ch._app.bot
    real = bot.send_message
    tries = []

    async def refuse_quote(**kw):
        tries.append(kw)
        rp = kw.get("reply_parameters")
        if rp is not None and rp.quote:
            raise BadRequest("Bad Request: QUOTE_TEXT_INVALID")
        return await real(**kw)
    bot.send_message = refuse_quote
    await ch.send_response(_marked(text), dict(_ASKED))
    assert len(tries) == 2
    assert tries[0]["reply_parameters"].quote == "Tue"
    assert tries[1]["reply_parameters"].quote is None
    assert tries[1]["reply_parameters"].message_id == 9
    assert tries[1]["text"] == "Tue works."
    assert ("entities" in tries[1]) == ("entities" in tries[0])   # same rendering


async def test_another_bad_request_is_not_taken_for_a_quote():
    ch, _bus = _channel()

    async def refuse(**kw):
        raise BadRequest("Bad Request: chat not found")
    ch._app.bot.send_message = refuse
    with pytest.raises(BadRequest):
        await ch.send_response(_marked('<reply quote="Tue"/>Tue works.'), dict(_ASKED))


async def test_casa_s_target_wins_over_the_marker():
    ch, _bus = _channel()
    await ch.send_response(_marked('<reply quote="Tue"/>Tue works.'),
                           dict(_ASKED, _reply_to=[BOT, OPERATOR, 7]))
    (sent,) = _sends(ch)
    assert sent["reply_parameters"].message_id == 7
    assert sent["reply_parameters"].quote is None


@pytest.mark.parametrize("request_", [[OTHER_BOT, OPERATOR, 9], [BOT, OPERATOR + 1, 9], None],
                         ids=["other-bot", "other-chat", "no-request"])
async def test_a_marker_without_his_message_here_sends_plain(request_):
    ch, _bus = _channel()
    ctx = dict(_ASKED, _tg_request=request_)
    await ch.send_response(_marked("<reply/>Tue works."), ctx)
    (sent,) = _sends(ch)
    assert sent["text"] == "Tue works."
    assert "reply_parameters" not in sent


async def test_an_unmarked_answer_is_a_plain_message_even_with_his_request():
    ch, _bus = _channel()
    await ch.send_response(_marked("Tue works."), dict(_ASKED))
    (sent,) = _sends(ch)
    assert "reply_parameters" not in sent


async def test_the_marker_rides_only_the_answer_not_other_sends_of_its_context():
    ch, _bus = _channel()
    ctx = dict(_ASKED)
    await ch.send_response(_marked('<reply quote="Tue"/>' + _two_pages(True)), ctx)
    await ch.send(_admitted("a line of Casa's own"), ctx)
    sends = _sends(ch)
    assert sends[0]["reply_parameters"].quote == "Tue"
    assert all("reply_parameters" not in s for s in sends[1:])
