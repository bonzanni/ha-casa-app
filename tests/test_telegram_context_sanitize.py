"""A:§3.5: TelegramChannel._handle routes its (Casa-owned) context dict
through sanitize_external_context() for uniformity with the other
ingresses — a no-op here since none of the keys it builds are reserved.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

import provenance
from bus import BusMessage, MessageBus
from channels.telegram import TelegramChannel

pytestmark = pytest.mark.asyncio


class _FakeBot:
    async def send_message(self, **kwargs: Any) -> Any:
        return types.SimpleNamespace(message_id=1)


class _FakeApp:
    def __init__(self) -> None:
        self.bot = _FakeBot()


def _fake_update(chat_id: str = "42", text: str = "hi") -> Any:
    user = types.SimpleNamespace(first_name="User", id=7)
    message = types.SimpleNamespace(text=text, message_id=42)
    chat = types.SimpleNamespace(id=chat_id)
    return types.SimpleNamespace(message=message, effective_chat=chat, effective_user=user)


async def _drain(bus: MessageBus, target: str = "assistant") -> list[BusMessage]:
    q = bus.queues.get(target)
    if q is None:
        return []
    out = []
    while not q.empty():
        _p, _s, msg = q.get_nowait()
        out.append(msg)
        q.task_done()
    return out


async def _noop_handler(_msg: BusMessage) -> None:
    return None


def _channel():
    bus = MessageBus()
    bus.register("assistant", _noop_handler)
    channel = TelegramChannel(
        known_bot_id=111,
        bot_token="T", chat_id="0", default_agent="assistant", bus=bus,
    )
    channel._start_typing = lambda *a, **k: None  # type: ignore[assignment]
    channel._app = _FakeApp()  # type: ignore[assignment]
    return channel, bus


class TestTelegramHandleRoutesThroughSanitize:
    async def test_sanitize_external_context_is_called(self, monkeypatch):
        channel, bus = _channel()
        calls: list[dict] = []
        real = provenance.sanitize_external_context

        def _spy(ctx):
            calls.append(dict(ctx) if ctx else {})
            return real(ctx)

        monkeypatch.setattr("channels.telegram.sanitize_external_context", _spy)

        await channel._handle(_fake_update("42", "hello"), None)

        assert calls, "sanitize_external_context must be called by _handle"

    async def test_no_op_for_the_casa_owned_context(self):
        """None of the keys _handle builds are reserved, so the resulting
        BusMessage.context must be unaffected by the sanitize pass."""
        channel, bus = _channel()
        await channel._handle(_fake_update("42", "hello"), None)
        msgs = await _drain(bus)
        assert len(msgs) == 1
        ctx = msgs[0].context
        assert ctx["chat_id"] == "42"
        assert ctx["user_id"] == 7
        assert ctx["user_name"] == "User"
        assert ctx["message_id"] == "42"
        assert "cid" in ctx
        # #336: the ONLY reserved keys present are the two origin markers
        # and the turn's route (the bot it arrived on), which Casa itself
        # stamps AFTER the sanitize pass (server-side, so an external payload
        # still cannot supply them) — nothing else reserved ever entered.
        assert provenance.RESERVED_CONTEXT_KEYS & ctx.keys() == {
            "_origin_route", "_origin_clearance", "_tg_bot",
        }
        assert ctx["_origin_route"] == "telegram"
        assert ctx["_tg_bot"] == channel.bot_id
        assert ctx["_origin_clearance"] in ("private", "public")
