"""Tests for the <current_time> block emitted by Agent._process.

M27: the timestamp must NOT live in the (cached) system prompt — a per-second
timestamp there invalidates Anthropic prompt caching for the whole conversation
every turn. It rides on the per-turn query text instead, keeping the system
prompt byte-stable across resumed turns while still giving the agent the
current wall-clock time to second precision.
"""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo

import pytest

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = pytest.mark.unit


def _compose_query_prefix(user_text: str, tz):
    """The per-turn prompt_text prefix agent.py::_process builds — now the
    REAL composer (#471 moved it to timekeeping), not a hand-kept mirror."""
    from datetime import datetime

    from timekeeping import compose_time_envelope
    return compose_time_envelope(datetime.now(tz)) + user_text


class TestCurrentTimeBlock:
    def test_query_prefix_shape(self):
        tz = ZoneInfo("Europe/Amsterdam")
        out = _compose_query_prefix("hello", tz)
        m = re.search(
            r"^<current_time>\n"
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} "
            r"\((monday|tuesday|wednesday|thursday|friday|saturday|sunday) "
            r"(am|pm), week \d{1,2}\)\n"
            r"</current_time>\n\n",
            out,
        )
        assert m is not None, f"shape mismatch: {out!r}"
        assert out.endswith("hello")


class TestAgentProcessInjects:
    """Integration-style test: hit Agent._process via the production path."""

    @pytest.mark.asyncio
    async def test_system_prompt_time_free_and_timestamp_rides_on_query(self, tmp_path):
        """Regression guard for prompt-cache stability (M27): the system prompt
        must carry no wall-clock timestamp; the per-turn <current_time> block
        lives on the (never-cached) query text so resumed turns present a
        byte-stable prefix to the API."""
        from unittest.mock import patch

        import agent as agent_mod

        captured = {}

        class _FakeClient:
            def __init__(self, options):
                captured["options"] = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def query(self, text):
                captured["query_text"] = text
                return None

            async def receive_response(self):
                return
                yield  # pragma: no cover

            @property
            def session_id(self):
                return "sid"

        from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
        from bus import BusMessage, MessageType
        from channels import ChannelManager
        from mcp_registry import McpServerRegistry
        from session_registry import SessionRegistry
        cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
            role="assistant",
            model="claude-sonnet-4-6",
            system_prompt="You are Ellen.",
            character=CharacterConfig(name="Ellen"),
            tools=ToolsConfig(allowed=[]),
            memory=MemoryConfig(
                token_budget=0,
            ),
        )

        agent = agent_mod.Agent(
            config=cfg,
            session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
            mcp_registry=McpServerRegistry(),
            channel_manager=ChannelManager(),
        )

        with patch("sdk_client_pool._default_make_client", _FakeClient):
            msg = BusMessage(
                type=MessageType.REQUEST, source="telegram",
                target="assistant", content="hello",
                channel="telegram", context={"chat_id": "42", "cid": "c-1"},
            )
            await agent._process(msg, on_token=None)

        system_prompt = captured["options"].system_prompt
        # 1. No time block and no ISO wall-clock timestamp in the system prompt.
        assert "<current_time>" not in system_prompt
        assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", system_prompt) is None
        # 2. The block rides on the (never-cached) user turn, pinned shape.
        query_text = captured["query_text"]
        assert query_text.startswith("<current_time>")
        assert "</current_time>" in query_text
        assert query_text.rstrip().endswith("hello")
