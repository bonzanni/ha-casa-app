"""S4 §6: the chat's resident learns of a desk turn only through the body-free
lines Casa prepends to its NEXT turn's prompt — drained once, no model turn
spent on the echo, the origin's user_text left raw (INV-DESK-002).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import result_broker as rb
import specialist_desk as sd

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = pytest.mark.asyncio


class _FakeClient:
    captured: dict = {}

    def __init__(self, options):
        _FakeClient.captured["options"] = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, text):
        _FakeClient.captured.setdefault("queries", []).append(text)
        return None

    async def receive_response(self):
        return
        yield  # pragma: no cover

    @property
    def session_id(self):
        return "sid"


def _agent(tmp_path):
    import agent as agent_mod
    from channels import ChannelManager
    from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
    from mcp_registry import McpServerRegistry
    from session_registry import SessionRegistry
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role="assistant",
                      model="claude-sonnet-4-6", system_prompt="You are Ellen.",
                      character=CharacterConfig(name="Ellen"), tools=ToolsConfig(allowed=[]),
                      memory=MemoryConfig(token_budget=0))
    return agent_mod.Agent(config=cfg,
                           session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
                           mcp_registry=McpServerRegistry(), channel_manager=ChannelManager())


async def _turn(agent, text="hello", channel="telegram", chat_id="42"):
    from bus import BusMessage, MessageType
    _FakeClient.captured = {}
    with patch("sdk_client_pool._default_make_client", _FakeClient):
        msg = BusMessage(type=MessageType.REQUEST, source=channel, target="assistant",
                         content=text, channel=channel, context={"chat_id": chat_id, "cid": "c-1"})
        await agent._process(msg, on_token=None)
    return _FakeClient.captured["queries"][-1]


async def test_the_residents_next_turn_carries_the_echo_once_then_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "DESK_ECHO", rb.PostLedger(max_events=64))
    agent = _agent(tmp_path)
    sd.record_echo(42, "📊 Finance answered your reply (2 pages).")
    sd.record_echo(42, "📊 Finance posted to your chat (1 page).")
    query = await _turn(agent)
    assert query.startswith("(front desk) 📊 Finance answered your reply (2 pages).\n"
                            "(front desk) 📊 Finance posted to your chat (1 page).\n\n<current_time>")
    assert query.rstrip().endswith("hello")
    query2 = await _turn(agent, text="again")
    assert query2.startswith("<current_time>") and "(front desk)" not in query2


async def test_another_chats_echo_and_a_non_telegram_turn_get_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, "DESK_ECHO", rb.PostLedger(max_events=64))
    agent = _agent(tmp_path)
    sd.record_echo(42, "📊 Finance answered your reply (1 page).")
    assert "(front desk)" not in await _turn(agent, chat_id="43")
    assert "(front desk)" not in await _turn(agent, channel="webhook", chat_id="42")
    assert "(front desk)" in await _turn(agent)                   # still owed to chat 42
