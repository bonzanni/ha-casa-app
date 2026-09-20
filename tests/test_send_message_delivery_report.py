"""`send_message` reports what its channel actually did (#990).

The tool used to discard `Channel.send`'s return and answer "Message sent via
X." unconditionally. Two shipped channels return normally having delivered
nothing — Telegram while its application is not started, and voice, whose
`send` is a documented no-op — so a scheduled turn following the closing-silence
convention suppressed its own final text on the strength of a false report and
the operator received nothing.

These pin the REPORT, against the real channels where the fact lives:

* a proven negative (`DeliveryOutcome.NOT_DELIVERED`) is an error result;
* `UNKNOWN`/`None` — a channel that has not opted into the contract — keeps
  today's success text, explicitly (#556: "channel cannot report" is not
  "delivery failed");
* a raised send becomes a tool result instead of propagating, and that result
  makes NO claim about what reached the operator — an exception does not
  establish one, and Casa's own taxonomy for the question lives in the channel
  (`channels/telegram.py`), not in a channel-agnostic tool;
* `asyncio.CancelledError` is never swallowed.
"""
from __future__ import annotations

import asyncio
import types

import pytest

import agent as agent_mod
import tools
from channels import DeliveryOutcome

pytestmark = [pytest.mark.unit]


def _text(result: dict) -> str:
    return result["content"][0]["text"]


class _Channel:
    """Channel double whose `send` returns/raises exactly what a test wants."""

    def __init__(self, name="telegram", *, outcome=None, raises=None,
                 head_sent=False):
        self.name = name
        self._outcome = outcome
        self._raises = raises
        self._head_sent = head_sent
        self.calls: list[tuple[str, dict]] = []

    async def send(self, message, context):
        self.calls.append((message, context))
        if self._head_sent:
            context["_delivery_head_sent"] = True
        if self._raises is not None:
            raise self._raises
        return self._outcome


class _CM:
    def __init__(self, *channels):
        self.channels = {c.name: c for c in channels}

    def get(self, name):
        return self.channels.get(name)


async def _send(monkeypatch, cm, *, channel="telegram", origin_channel="telegram"):
    monkeypatch.setattr(tools, "_channel_manager", cm)
    token = agent_mod.origin_var.set({"channel": origin_channel})
    try:
        return await tools.send_message.handler(
            {"message": "Bins out tonight.", "channel": channel})
    finally:
        agent_mod.origin_var.reset(token)


# --- the proven negative ---------------------------------------------------

async def test_not_delivered_is_reported_as_an_error(monkeypatch):
    ch = _Channel(outcome=DeliveryOutcome.NOT_DELIVERED)
    out = _text(await _send(monkeypatch, _CM(ch)))
    assert out.startswith("Error:"), out
    assert "Message sent" not in out


async def test_an_unstarted_telegram_channel_reports_the_error(monkeypatch):
    """The issue's first case, against the real channel: `send` returns
    NOT_DELIVERED having made zero Bot API calls while the app is absent."""
    from channels.telegram import TelegramChannel

    class _Bot:
        pass

    ch = TelegramChannel(bot=_Bot(), chat_id=100)
    assert ch.is_ready is False
    assert await ch.send("x", {}) is DeliveryOutcome.NOT_DELIVERED

    out = _text(await _send(monkeypatch, _CM(ch)))
    assert out.startswith("Error:"), out
    assert "Message sent" not in out


# --- voice: a documented no-op is a PROVEN negative, not UNKNOWN -----------

def _voice_channel():
    from channels.voice.channel import VoiceChannel

    return VoiceChannel(
        bus=None, default_agent="assistant", webhook_secret="s",
        sse_path="/voice/sse", ws_path="/voice/ws", agent_configs={},
        memory=None, idle_timeout=60,
    )


async def test_the_voice_channel_reports_that_it_delivered_nothing():
    assert await _voice_channel().send("x", {}) is DeliveryOutcome.NOT_DELIVERED


async def test_a_caller_selected_voice_channel_is_reported_as_not_delivered(
        monkeypatch):
    """The issue's measured middle row: a telegram-channel trigger whose turn
    sends via voice. `send_message` honours the caller-selected channel for a
    non-webhook turn and voice delivers nothing — so the tool must say so, or
    the turn ends `<silent/>` and Telegram receives nothing at all."""
    voice = _voice_channel()
    out = _text(await _send(monkeypatch, _CM(voice), channel="voice",
                            origin_channel="telegram"))
    assert out.startswith("Error:"), out
    assert "Message sent" not in out


# --- the positives are unchanged -------------------------------------------

async def test_delivered_keeps_todays_success_text(monkeypatch):
    ch = _Channel(outcome=DeliveryOutcome.DELIVERED)
    assert _text(await _send(monkeypatch, _CM(ch))) == "Message sent via telegram."


@pytest.mark.parametrize("outcome", [None, DeliveryOutcome.UNKNOWN])
async def test_a_channel_off_the_contract_keeps_todays_success_text(
        monkeypatch, outcome):
    """#556's rule, applied here: a channel that cannot report is not a channel
    that failed. Reporting UNKNOWN as an error would make every off-contract
    channel's every send look lost."""
    ch = _Channel(outcome=outcome)
    assert _text(await _send(monkeypatch, _CM(ch))) == "Message sent via telegram."


# --- a raised send makes NO delivery claim --------------------------------
#
# Three review findings across two rounds, each reproduced against the real
# channel, said the same thing about an earlier wording: it asserted more than
# an exception establishes. The judgment was cut rather than sharpened a third
# time, so what is pinned here is an ABSENCE — the report names the failure and
# claims nothing about what reached the operator. These are the three cases
# that were reproduced, and none of them may carry a claim.

_CLAIMS = (
    "did NOT reach", "may have been delivered", "stopped the rest",
    "reached the operator", "delivered nothing",
)


def _assert_makes_no_delivery_claim(out: str, exc_name: str) -> None:
    assert out.startswith("Error:"), out
    assert exc_name in out, out
    assert "Message sent" not in out, out
    for claim in _CLAIMS:
        assert claim not in out, (claim, out)


async def test_a_raised_send_becomes_an_error_result(monkeypatch):
    """The base case: the exception never propagates out of the tool."""
    ch = _Channel(raises=RuntimeError("boom"))
    out = _text(await _send(monkeypatch, _CM(ch)))
    _assert_makes_no_delivery_claim(out, "RuntimeError")


def _started_channel(bot):
    """A real `TelegramChannel` whose PTB application is started, so `send`
    runs its chunk loop and its `_delivery_head_sent` stamping for real."""
    from channels.telegram import TelegramChannel

    ch = TelegramChannel(bot=bot, chat_id=100)
    ch._app = types.SimpleNamespace(bot=bot)
    assert ch.is_ready is True
    return ch


async def test_a_lost_acknowledgement_is_not_reported_as_reaching_nobody(
        monkeypatch):
    """Round 1, both reviewers. `TelegramChannel.send` stamps
    `_delivery_head_sent` only after the Bot API call RETURNS, so a response
    lost in transit — a read timeout on a request the server already processed
    — raises with no stamp for a message that DID arrive."""
    from telegram.error import TimedOut

    accepted: list[str] = []

    class _Bot:
        async def send_message(self, chat_id, text):
            accepted.append(text)     # the server processed it ...
            raise TimedOut()          # ... and the response was lost

    out = _text(await _send(monkeypatch, _CM(_started_channel(_Bot()))))
    assert len(accepted) == 1, accepted
    _assert_makes_no_delivery_claim(out, "TimedOut")


async def test_a_refusal_is_not_reported_as_possibly_delivered(monkeypatch):
    """Round 2, Astra. A `Forbidden` is a response Telegram actually sent: the
    call was evaluated and declined, and nothing was displayed. A report saying
    it may have been delivered is false — and this tree already classifies the
    refusals, in `channels/telegram.py`."""
    from telegram.error import Forbidden

    attempted: list[str] = []

    class _Bot:
        async def send_message(self, chat_id, text):
            attempted.append(text)
            raise Forbidden("bot was blocked by the user")

    out = _text(await _send(monkeypatch, _CM(_started_channel(_Bot()))))
    assert len(attempted) == 1, attempted
    _assert_makes_no_delivery_claim(out, "Forbidden")


async def test_a_last_chunk_that_raises_is_not_reported_as_stopping_the_rest(
        monkeypatch):
    """Round 2, Astra. A message long enough to split: both chunks carry the
    whole message and are accepted, and the LAST call's acknowledgement is the
    one lost. There is no remainder, so "stopped the rest" names a loss that
    did not happen."""
    from telegram.error import TimedOut

    accepted: list[str] = []

    class _Bot:
        async def send_message(self, chat_id, text):
            accepted.append(text)
            if len(accepted) == 2:      # the final chunk's ack is lost
                raise TimedOut()

    ch = _started_channel(_Bot())
    monkeypatch.setattr(tools, "_channel_manager", _CM(ch))
    token = agent_mod.origin_var.set({"channel": "telegram"})
    try:
        out = _text(await tools.send_message.handler(
            {"message": "x " * 3000, "channel": "telegram"}))
    finally:
        agent_mod.origin_var.reset(token)

    assert len(accepted) == 2, accepted
    assert "".join(accepted).count("x") == 3000, len(accepted)
    _assert_makes_no_delivery_claim(out, "TimedOut")


async def test_cancellation_is_not_swallowed(monkeypatch):
    ch = _Channel(raises=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _send(monkeypatch, _CM(ch))


# --- the untrusted-webhook binding still wins ------------------------------

async def test_an_untrusted_webhook_turn_still_reports_the_bound_channel(
        monkeypatch):
    """Layer 1 rewrites the channel to telegram before the send; the report
    must name the channel that was actually used, not the one asked for."""
    tg = _Channel("telegram", outcome=DeliveryOutcome.DELIVERED)
    voice = _Channel("voice", outcome=DeliveryOutcome.NOT_DELIVERED)
    monkeypatch.setattr(tools, "_channel_manager", _CM(tg, voice))
    token = agent_mod.origin_var.set({
        "channel": "webhook", "_origin_route": "webhook_trigger",
    })
    try:
        out = _text(await tools.send_message.handler(
            {"message": "hi", "channel": "voice"}))
    finally:
        agent_mod.origin_var.reset(token)
    assert out == "Message sent via telegram."
    assert not voice.calls
