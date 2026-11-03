"""Layer 2 — SDK-boundary memory-reachability contract over the mode matrix.

Drives the REAL `Agent._process` with each resident's REAL `tools.allowed`
(read from the shipped runtime.yaml) across the channel × session cells, and
captures the actual `ClaudeAgentOptions` handed to the SDK. Asserts the
invariant a missing memory-tool grant once violated:

    No turn recalls on its own, and a session's served view is a ledger, not
    the saved conversation: the path to what was said is the `search_memory`
    tool — so it MUST be in the captured `options.allowed_tools` in every
    cell a resident serves (resumed session, voice, or a scheduled turn).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from agent import Agent
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from mcp_registry import McpServerRegistry
from session_registry import SessionRegistry, build_scoped_session_key

from claude_agent_sdk import (
    AssistantMessage as _AM,
    ResultMessage as _RM,
    TextBlock as _TB,
)

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

AGENTS = Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa" / "defaults" / "agents"
RECALL_TOOL = "mcp__casa-framework__search_memory"


def _real_allowed(role: str) -> list[str]:
    data = yaml.safe_load((AGENTS / role / "runtime.yaml").read_text(encoding="utf-8"))
    return (data.get("tools") or {}).get("allowed") or []


class _CaptureClient:
    captured_options = None

    def __init__(self, options):
        _CaptureClient.captured_options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def query(self, text):
        return None

    async def receive_response(self):
        try:
            block = _TB(text="ok")
        except TypeError:
            block = _TB("ok")  # type: ignore[call-arg]
        try:
            yield _AM(content=[block])
        except TypeError:
            m = _AM.__new__(_AM); m.content = [block]; yield m  # type: ignore[attr-defined]
        r = _RM.__new__(_RM)
        r.session_id = "sid-reach"  # type: ignore[attr-defined]
        r.usage = {"input_tokens": 1, "output_tokens": 1}  # type: ignore[attr-defined]
        yield r


def _agent(tmp_path, role: str, *, seed_resumed: str | None = None) -> Agent:
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role=role,
        model="claude-sonnet-4-6",
        system_prompt=f"You are {role}.",
        character=CharacterConfig(name=role.capitalize()),
        tools=ToolsConfig(allowed=_real_allowed(role), permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
    )
    reg = SessionRegistry(str(tmp_path / f"{role}.json"))
    if seed_resumed is not None:
        # Seed a warm entry so the REAL _resume_decision resumes it
        # → is_fresh=False — the cell the regression lived in.
        reg._data[seed_resumed] = {
            "agent": role,
            "sdk_session_id": "warm-sid",
            "last_active": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        }
    return Agent(
        config=cfg,
        session_registry=reg,
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
    )


def _msg(channel: str, chat_id: str) -> BusMessage:
    return BusMessage(
        type=MessageType.CHANNEL_IN, source=channel, target="x",
        content="hi", channel=channel, context={"chat_id": chat_id},
    )


# (role, channel, is_fresh) — the cells each resident actually serves.
# assistant: telegram (interactive + scheduled turns share the channel);
# butler: voice (default_agent) + telegram.
CELLS = [
    ("assistant", "telegram", True),
    ("assistant", "telegram", False),   # resumed / scheduled — the regression cell
    ("butler", "voice", True),          # regression cell
    ("butler", "voice", False),
    ("butler", "telegram", False),
]


@pytest.mark.parametrize("role,channel,is_fresh", CELLS)
async def test_memory_reachable_in_every_cell(tmp_path, role, channel, is_fresh):
    chat_id = f"{channel}-{is_fresh}"
    key = None if is_fresh else build_scoped_session_key(channel, role, chat_id)
    agent = _agent(tmp_path, role, seed_resumed=key)
    with patch("sdk_client_pool._default_make_client", _CaptureClient):
        await agent._process(_msg(channel, chat_id))

    opts = _CaptureClient.captured_options
    assert opts is not None, "options never captured"
    assert RECALL_TOOL in opts.allowed_tools, (
        f"[{role}/{channel}/fresh={is_fresh}] memory UNREACHABLE: "
        f"{RECALL_TOOL} is not in allowed_tools {opts.allowed_tools}"
    )


@pytest.mark.parametrize("role,channel,is_fresh", CELLS)
async def test_options_allowed_tools_superset_of_config(tmp_path, role, channel, is_fresh):
    """The assembled options must carry every granted tool (handler must not
    silently drop grants) — the captured contract ⊇ the config."""
    chat_id = f"sup-{channel}-{is_fresh}"
    key = None if is_fresh else build_scoped_session_key(channel, role, chat_id)
    agent = _agent(tmp_path, role, seed_resumed=key)
    with patch("sdk_client_pool._default_make_client", _CaptureClient):
        await agent._process(_msg(channel, chat_id))
    opts = _CaptureClient.captured_options
    # (f) v0.69.9: skills are enabled via skills="all", not a bare "Skill" in
    # allowed_tools (deprecated) — exclude it from the superset contract and
    # assert the new positive contract instead.
    assert (set(_real_allowed(role)) - {"Skill"}) <= set(opts.allowed_tools)
    assert "Skill" not in opts.allowed_tools
    assert opts.skills == "all"
