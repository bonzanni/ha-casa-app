"""send_message egress is operator-bound for untrusted webhook turns (Layer 1)."""
from __future__ import annotations

import pytest

import agent as agent_mod
import tools
from output_boundary_testing import with_scope

pytestmark = [pytest.mark.unit]


class _RecordingChannel:
    def __init__(self, name):
        self.name = name
        self.sent = []

    async def send(self, message, ctx):
        self.sent.append((message, ctx))


class _CM:
    def __init__(self):
        self.channels = {
            "telegram": _RecordingChannel("telegram"),
            "voice": _RecordingChannel("voice"),
        }

    def get(self, name):
        return self.channels.get(name)


async def test_untrusted_webhook_send_bound_to_operator(monkeypatch):
    cm = _CM()
    monkeypatch.setattr(tools, "_channel_manager", cm)
    token = agent_mod.origin_var.set(with_scope({
        "channel": "webhook", "_origin_route": "webhook_trigger",
    }))
    try:
        # caller tries to select the voice channel — must be ignored
        await tools.send_message.handler({"message": "hi", "channel": "voice"})
    finally:
        agent_mod.origin_var.reset(token)
    assert cm.channels["telegram"].sent, "should route to operator telegram"
    assert not cm.channels["voice"].sent, "caller-selected channel ignored"


async def test_missing_route_webhook_send_bound_to_operator(monkeypatch):
    """Fail-closed: a webhook turn with no route is still operator-bound."""
    cm = _CM()
    monkeypatch.setattr(tools, "_channel_manager", cm)
    token = agent_mod.origin_var.set(with_scope({"channel": "webhook"}))
    try:
        await tools.send_message.handler({"message": "hi", "channel": "voice"})
    finally:
        agent_mod.origin_var.reset(token)
    assert cm.channels["telegram"].sent
    assert not cm.channels["voice"].sent


async def test_invoke_send_honors_caller_channel(monkeypatch):
    """Operator-signed /invoke is trusted — may select the channel."""
    cm = _CM()
    monkeypatch.setattr(tools, "_channel_manager", cm)
    token = agent_mod.origin_var.set(with_scope({
        "channel": "webhook", "_origin_route": "invoke",
    }))
    try:
        await tools.send_message.handler({"message": "hi", "channel": "voice"})
    finally:
        agent_mod.origin_var.reset(token)
    assert cm.channels["voice"].sent
    assert not cm.channels["telegram"].sent


async def test_normal_telegram_turn_unaffected(monkeypatch):
    cm = _CM()
    monkeypatch.setattr(tools, "_channel_manager", cm)
    token = agent_mod.origin_var.set(with_scope({"channel": "telegram"}))
    try:
        await tools.send_message.handler({"message": "hi", "channel": "voice"})
    finally:
        agent_mod.origin_var.reset(token)
    # non-webhook origin: no binding applied, caller channel honored
    assert cm.channels["voice"].sent
