"""Shared fixtures for tests that read the prompt a resident's turn sends to
the SDK: a fake client that captures the options and every query, a real
assistant agent over it, and a helper that runs one turn and returns the
query it sent."""
from __future__ import annotations

from unittest.mock import patch

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

#: the bot the operator's Telegram turns arrive on
BOT = 111


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


async def _turn(agent, text="hello", channel="telegram", chat_id="42", **context):
    from bus import BusMessage, MessageType
    _FakeClient.captured = {}
    if channel == "telegram":
        context.setdefault("_tg_bot", BOT)
    with patch("sdk_client_pool._default_make_client", _FakeClient):
        msg = BusMessage(type=MessageType.REQUEST, source=channel, target="assistant",
                         content=text, channel=channel,
                         context={"chat_id": chat_id, "cid": "c-1", **context})
        await agent._process(msg, on_token=None)
    return _FakeClient.captured["queries"][-1]
