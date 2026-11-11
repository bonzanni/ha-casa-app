"""Shared fixtures for tests that drive a reply or a message through Casa's
Telegram channel: the operator, the bot, a fake bot that records what is sent,
a channel wired to a bus, a plain post-record builder, and a helper that waits
for the channel's turn tasks to finish."""
from __future__ import annotations

import asyncio
import types

import result_broker as rb
from bus import MessageBus
from channels.telegram import TelegramChannel
from two_endpoints import CASA_BOT
from test_telegram_new_reset import _noop

OPERATOR = 42
BOT = CASA_BOT


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=1)


def _channel():
    bus = MessageBus()
    bus.register("assistant", _noop)
    ch = TelegramChannel(bot_token="T", chat_id=str(OPERATOR), default_agent="assistant", bus=bus,
                         is_casa=True, known_bot_id=BOT)
    ch._start_typing = lambda *a, **k: None
    ch._app = types.SimpleNamespace(bot=_FakeBot())
    ch._session_registry = None
    return ch, bus


def _record(**over):
    base = dict(role="finance", operator_id=OPERATOR, plugin="probe", slot="report",
                tool_use_id="call-1", owner="d-1", posted_at=1.0)
    base.update(over)
    return rb.PostRecord(**base)


async def _settle(ch):
    for t in list(ch._turn_tasks):
        await t
    await asyncio.sleep(0)                                    # let done-callbacks run
    await asyncio.sleep(0)
