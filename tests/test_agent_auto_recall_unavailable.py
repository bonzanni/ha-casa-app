"""Automatic pre-turn recall must degrade cleanly when the backend fails.

Three-outcome contract (v0.99.0): the auto-recall in `_build_options` runs
under a short bounded deadline (never the full HTTP client timeout), does not
crash prompt construction, injects NO <memory_context> block on failure, and
a circuit breaker stops hammering an unavailable backend after consecutive
failures until a recovery probe succeeds.

Reuses the real-`_process` harness shape from
tests/test_memory_reachability_contract.py (capture the assembled
ClaudeAgentOptions via a fake SDK client).
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import agent as agent_mod
from agent import Agent, _RecallBreaker
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from mcp_registry import McpServerRegistry
from semantic_memory import RecallUnavailable, SemanticMemory
from session_registry import SessionRegistry

from claude_agent_sdk import (
    AssistantMessage as _AM,
    ResultMessage as _RM,
    TextBlock as _TB,
)

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.unit]

AGENTS = Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa" / "defaults" / "agents"


def _real_allowed(role: str) -> list[str]:
    data = yaml.safe_load((AGENTS / role / "runtime.yaml").read_text(encoding="utf-8"))
    return (data.get("tools") or {}).get("allowed") or []


class _FailingSem(SemanticMemory):
    """Recall raises RecallUnavailable; counts attempts."""

    def __init__(self) -> None:
        self.recall_attempts = 0

    async def retain(self, bank, items, *, async_=True):
        return None

    async def recall(self, bank, query, *, tags, max_tokens,
                     types=("world", "experience", "observation"),
                     tags_match="any", budget="mid"):
        raise RecallUnavailable("http_504")

    async def recall_items(self, bank, query, *, tags, max_tokens, clearance,
                           types=("world", "experience", "observation"),
                           tags_match="any", budget="mid"):
        self.recall_attempts += 1
        raise RecallUnavailable("http_504")

    async def document_tags(self, bank, document_id):
        return None  # #1123: nothing stored here — every document reads never-saved

    async def profile(self, bank):
        return ""


class _SlowSem(_FailingSem):
    """Recall hangs far past the bounded deadline (cancelled by wait_for)."""

    async def recall_items(self, bank, query, *, tags, max_tokens, clearance,
                           types=("world", "experience", "observation"),
                           tags_match="any", budget="mid"):
        self.recall_attempts += 1
        await asyncio.sleep(30)
        return ()


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
        r.session_id = "sid-unavail"  # type: ignore[attr-defined]
        r.usage = {"input_tokens": 1, "output_tokens": 1}  # type: ignore[attr-defined]
        yield r


def _agent(tmp_path, sem) -> Agent:
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role="assistant",
        model="claude-sonnet-4-6",
        system_prompt="You are assistant.",
        character=CharacterConfig(name="Assistant"),
        tools=ToolsConfig(allowed=_real_allowed("assistant"), permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
    )
    return Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "assistant.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
        semantic_memory=sem,
    )


def _msg(chat_id: str) -> BusMessage:
    return BusMessage(
        type=MessageType.CHANNEL_IN, source="telegram", target="x",
        content="what do you remember?", channel="telegram",
        context={"chat_id": chat_id},
    )


async def test_unavailable_recall_still_builds_options_without_memory_block(tmp_path):
    """Backend down → turn proceeds, NO <memory_context> injected (the model
    must never be told an empty recall happened)."""
    sem = _FailingSem()
    agent = _agent(tmp_path, sem)
    with patch("sdk_client_pool._default_make_client", _CaptureClient):
        await agent._process(_msg("c-unavail"))
    opts = _CaptureClient.captured_options
    assert opts is not None
    assert sem.recall_attempts == 1
    assert "<memory_context>" not in (opts.system_prompt or "")


async def test_auto_recall_bounded_deadline_not_full_http_timeout(tmp_path, monkeypatch):
    """A hung backend must not stall prompt construction: the recall is
    cancelled at the short deadline and the turn proceeds cold."""
    monkeypatch.setattr(agent_mod, "_AUTO_RECALL_TIMEOUT_S", 0.05)
    sem = _SlowSem()
    agent = _agent(tmp_path, sem)
    t0 = time.monotonic()
    with patch("sdk_client_pool._default_make_client", _CaptureClient):
        await agent._process(_msg("c-slow"))
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"prompt construction blocked {elapsed:.1f}s on a hung recall"
    opts = _CaptureClient.captured_options
    assert "<memory_context>" not in (opts.system_prompt or "")


async def test_breaker_opens_after_consecutive_failures_and_skips_recall(tmp_path):
    """After the failure threshold, auto-recall runs cold (no backend calls)
    instead of adding load to an overloaded reranker on every turn."""
    sem = _FailingSem()
    agent = _agent(tmp_path, sem)
    threshold = agent._recall_breaker._threshold
    with patch("sdk_client_pool._default_make_client", _CaptureClient):
        for i in range(threshold + 2):   # fresh chat ids → every turn auto-recalls
            await agent._process(_msg(f"c-brk-{i}"))
    assert sem.recall_attempts == threshold, (
        f"breaker must open after {threshold} consecutive failures "
        f"(saw {sem.recall_attempts} backend attempts)"
    )


async def test_breaker_recovers_after_successful_probe(tmp_path):
    """Once the cooldown elapses, the next turn is a recovery probe; on
    success the breaker closes and recall resumes on subsequent turns."""
    sem = _FailingSem()
    agent = _agent(tmp_path, sem)
    threshold = agent._recall_breaker._threshold
    with patch("sdk_client_pool._default_make_client", _CaptureClient):
        for i in range(threshold):
            await agent._process(_msg(f"c-rec-{i}"))
        assert agent._recall_breaker.open

        # Heal the backend and force the cooldown to have elapsed.
        async def _healthy(bank, query, *, tags, max_tokens, clearance, **kw):
            from personality_types import RecallHit
            sem.recall_attempts += 1
            return (RecallHit(
                text="a fact", memory_type="world", sensitivity="friends",
                application_tags=(), provenance=None, backend_id="b1",
                document_id=None, chunk_id=None, source_fact_ids=None,
                metadata=None, context=None, score=None,
            ),)
        sem.recall_items = _healthy  # type: ignore[method-assign]
        agent._recall_breaker._opened_at -= (agent._recall_breaker._cooldown_s + 1)

        before = sem.recall_attempts
        await agent._process(_msg("c-rec-probe"))
        assert sem.recall_attempts == before + 1   # probe attempted
        assert not agent._recall_breaker.open      # success → closed
        opts = _CaptureClient.captured_options
        assert "<memory_context>" in (opts.system_prompt or "")


# --- _RecallBreaker unit behaviour (injectable clock, no real sleeps) -------


def test_breaker_closed_allows_and_counts_reset_on_success():
    now = [0.0]
    b = _RecallBreaker(threshold=3, cooldown_s=60.0, clock=lambda: now[0])
    assert b.allow()
    b.record_failure(); b.record_failure()
    b.record_success()
    b.record_failure(); b.record_failure()
    assert not b.open           # success reset the consecutive count
    assert b.allow()


def test_breaker_opens_at_threshold_and_blocks_until_cooldown():
    now = [100.0]
    b = _RecallBreaker(threshold=3, cooldown_s=60.0, clock=lambda: now[0])
    for _ in range(3):
        b.record_failure()
    assert b.open
    assert not b.allow()
    now[0] += 59.9
    assert not b.allow()
    now[0] += 0.2               # cooldown elapsed → half-open probe allowed
    assert b.allow()


def test_breaker_failed_probe_reopens_for_full_cooldown():
    now = [0.0]
    b = _RecallBreaker(threshold=3, cooldown_s=60.0, clock=lambda: now[0])
    for _ in range(3):
        b.record_failure()
    now[0] += 61
    assert b.allow()            # half-open probe
    b.record_failure()          # probe failed
    assert not b.allow()        # re-opened
    now[0] += 59
    assert not b.allow()
    now[0] += 2
    assert b.allow()


def test_breaker_half_open_allows_exactly_one_probe():
    """Concurrent turns after cooldown must not all probe at once — the
    half-open state admits ONE probe until it records success or failure."""
    now = [0.0]
    b = _RecallBreaker(threshold=3, cooldown_s=60.0, clock=lambda: now[0])
    for _ in range(3):
        b.record_failure()
    now[0] += 61
    assert b.allow()            # first turn reserves the probe
    assert not b.allow()        # concurrent turn: probe already in flight
    assert not b.allow()
    b.record_success()
    assert b.allow()            # closed again — everyone may recall


def test_breaker_stale_probe_reservation_expires():
    """A probe whose turn died without recording (e.g. cancelled) must not
    wedge the breaker: the reservation expires after a cooldown."""
    now = [0.0]
    b = _RecallBreaker(threshold=3, cooldown_s=60.0, clock=lambda: now[0])
    for _ in range(3):
        b.record_failure()
    now[0] += 61
    assert b.allow()            # probe reserved, never recorded
    assert not b.allow()
    now[0] += 61                # reservation stale
    assert b.allow()            # a fresh probe may proceed


def test_breaker_successful_probe_closes():
    now = [0.0]
    b = _RecallBreaker(threshold=3, cooldown_s=60.0, clock=lambda: now[0])
    for _ in range(3):
        b.record_failure()
    now[0] += 61
    assert b.allow()
    b.record_success()
    assert not b.open
    assert b.allow()
