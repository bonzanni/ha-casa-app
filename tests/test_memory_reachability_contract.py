"""Layer 2 — SDK-boundary memory-reachability contract over the mode matrix.

Drives the REAL `Agent._process` with each resident's REAL `tools.allowed`
(read from the shipped runtime.yaml) across the channel × session cells, and
captures the actual `ClaudeAgentOptions` handed to the SDK. Asserts the
invariant the `recall_memory` regression violated:

    On any turn where auto-recall does NOT fire (resumed session, voice, or a
    scheduled turn), the ONLY long-term-memory path is the `recall_memory` pull
    tool — so it MUST be in the captured `options.allowed_tools`.

`test_agent_process.py` already covers the per-cell memory-LOAD behaviour
(fresh telegram recalls; voice/resumed skip). This ties that load plan to the
tool grant via the actual assembled options — the seam no single test owned.
Uses the real `_plan_load` (via `_process`), not a reimplementation.
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
from semantic_memory import SemanticMemory
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
RECALL_TOOL = "mcp__casa-framework__recall_memory"


def _real_allowed(role: str) -> list[str]:
    data = yaml.safe_load((AGENTS / role / "runtime.yaml").read_text(encoding="utf-8"))
    return (data.get("tools") or {}).get("allowed") or []


class _CaptureSem(SemanticMemory):
    def __init__(self) -> None:
        self.recall_calls: list[dict] = []

    async def retain(self, bank, items, *, async_=True):
        return None

    async def recall(self, bank, query, *, tags, max_tokens,
                     types=("world", "experience", "observation"),
                     tags_match="any", budget="mid"):
        self.recall_calls.append({"tags": list(tags)})
        return "some fact"

    async def recall_items(self, bank, query, *, tags, max_tokens, clearance,
                           types=("world", "experience", "observation"),
                           tags_match="any", budget="mid"):
        self.recall_calls.append({"tags": list(tags), "clearance": clearance})
        from personality_types import RecallHit
        return (RecallHit(
            text="some fact", memory_type="world", sensitivity="friends",
            application_tags=(), provenance=None, backend_id="b1", document_id=None,
            chunk_id=None, source_fact_ids=None, metadata=None, context=None,
            score=None,
        ),)

    async def document_tags(self, bank, document_id):
        return None  # #1123: nothing stored here — every document reads never-saved

    async def profile(self, bank):
        return ""


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


def _agent(tmp_path, role: str, *, seed_resumed: str | None = None) -> tuple[Agent, _CaptureSem]:
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
        # Seed a warm entry so the REAL _resume_decision returns ("resume", False)
        # → is_fresh=False (no auto-recall) — the cell the regression lived in.
        reg._data[seed_resumed] = {
            "agent": role,
            "sdk_session_id": "warm-sid",
            "last_active": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        }
    sem = _CaptureSem()
    agent = Agent(
        config=cfg,
        session_registry=reg,
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
        semantic_memory=sem,
    )
    return agent, sem


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
    ("butler", "voice", True),          # voice never auto-recalls — regression cell
    ("butler", "voice", False),
    ("butler", "telegram", False),
]


@pytest.mark.parametrize("role,channel,is_fresh", CELLS)
async def test_memory_reachable_in_every_cell(tmp_path, role, channel, is_fresh):
    chat_id = f"{channel}-{is_fresh}"
    key = None if is_fresh else build_scoped_session_key(channel, role, chat_id)
    agent, sem = _agent(tmp_path, role, seed_resumed=key)
    with patch("sdk_client_pool._default_make_client", _CaptureClient):
        await agent._process(_msg(channel, chat_id))

    opts = _CaptureClient.captured_options
    assert opts is not None, "options never captured"
    auto_recalled = len(sem.recall_calls) > 0
    tool_granted = RECALL_TOOL in opts.allowed_tools
    assert auto_recalled or tool_granted, (
        f"[{role}/{channel}/fresh={is_fresh}] memory UNREACHABLE: auto-recall "
        f"did not fire and {RECALL_TOOL} is not in allowed_tools "
        f"{opts.allowed_tools}"
    )


@pytest.mark.parametrize("role,channel,is_fresh", CELLS)
async def test_options_allowed_tools_superset_of_config(tmp_path, role, channel, is_fresh):
    """The assembled options must carry every granted tool (handler must not
    silently drop grants) — the captured contract ⊇ the config."""
    chat_id = f"sup-{channel}-{is_fresh}"
    key = None if is_fresh else build_scoped_session_key(channel, role, chat_id)
    agent, _ = _agent(tmp_path, role, seed_resumed=key)
    with patch("sdk_client_pool._default_make_client", _CaptureClient):
        await agent._process(_msg(channel, chat_id))
    opts = _CaptureClient.captured_options
    # (f) v0.69.9: skills are enabled via skills="all", not a bare "Skill" in
    # allowed_tools (deprecated) — exclude it from the superset contract and
    # assert the new positive contract instead.
    assert (set(_real_allowed(role)) - {"Skill"}) <= set(opts.allowed_tools)
    assert "Skill" not in opts.allowed_tools
    assert opts.skills == "all"


async def test_fresh_telegram_recall_uses_full_private_clearance(tmp_path):
    """Clearance-tag correctness on the captured recall: a fresh telegram turn
    (private clearance) recalls across all four tiers."""
    agent, sem = _agent(tmp_path, "assistant")
    with patch("sdk_client_pool._default_make_client", _CaptureClient):
        await agent._process(_msg("telegram", "clr"))
    assert len(sem.recall_calls) == 1
    assert set(sem.recall_calls[0]["tags"]) == {"private", "family", "friends", "public"}
