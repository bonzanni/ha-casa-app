"""Browser executor §5.4 (v9.1): a specialist whose role reads no memory
(``memory.token_budget: 0``) keeps its own output out of the shared bank on
every path — the delegated result (pinned by
``test_specialist_memory_tiers.py::test_token_budget_zero_no_sem_calls``), the
delegation notice inside a resident's transcript, and an engagement summary.
The resident's own relaying words are saved as usual."""
from __future__ import annotations

import asyncio
import json
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import delegated_memory
import session_saver
import tools
from config import AgentConfig, CharacterConfig, MemoryConfig

from role_artifact_stub import STUB_ROLE_ARTIFACT
from session_reg_helpers import STUB_SPEAKER_PROV

from personality_types import SpeakerProvenance

SYSTEM = SpeakerProvenance(speaker_kind="system")
FIDO_ANSWER = "RunPod invoice INV-0042 for 31.20 USD saved"


def _cfg(role: str, kind: str, budget: int) -> AgentConfig:
    return AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role=role, model="x",
                       character=CharacterConfig(name=role.title()), system_prompt="x",
                       memory=MemoryConfig(token_budget=budget), kind=kind)


@pytest.fixture
def roles(monkeypatch):
    m = {"browser": _cfg("browser", "specialist", 0),
         "finance": _cfg("finance", "specialist", 4000),
         "concierge": _cfg("concierge", "resident", 0),
         "assistant": _cfg("assistant", "resident", 5000)}
    monkeypatch.setattr(tools, "_agent_role_map", m, raising=False)
    return m


def test_only_a_zero_budget_specialist_is_kept_out(roles):
    assert delegated_memory.role_writes_memory("browser") is False
    assert delegated_memory.role_writes_memory("finance") is True
    assert delegated_memory.role_writes_memory("concierge") is True    # a resident
    assert delegated_memory.role_writes_memory("gone") is False        # not loaded: kept out


def _notice(agent: str, **kw) -> str:
    """The REAL notice text ``Agent._synthesize_delegation_turn`` writes."""
    import agent as agent_mod
    from bus import BusMessage, MessageType
    from specialist_registry import DelegationComplete

    complete = DelegationComplete(delegation_id="abcdef1234567890", agent=agent,
                                  origin={"user_text": "get my invoice", "chat_id": "1"},
                                  **kw)
    msg = BusMessage(type=MessageType.NOTIFICATION, source=agent, target="assistant",
                     content=complete, channel="telegram", context={"chat_id": "1"})
    return str(agent_mod.Agent._synthesize_delegation_turn(
        types.SimpleNamespace(), msg).content)


BRANCHES = [
    {"status": "ok", "text": FIDO_ANSWER},
    {"status": "ok", "text": "", "result_available": False},
    {"status": "error", "kind": "timeout", "message": "it timed out"},
    {"status": "error", "kind": "restart_orphan", "message": ""},
    {"status": "error", "kind": "launch_outcome_uncommitted", "message": ""},
]


@pytest.mark.parametrize("kw", BRANCHES)
def test_every_notice_branch_names_its_role(roles, kw):
    assert delegated_memory.notice_from_excluded_role(_notice("browser", **kw)) is True
    assert delegated_memory.notice_from_excluded_role(_notice("finance", **kw)) is False


def test_a_notice_from_a_role_no_longer_loaded_is_kept_out(roles):
    """p3r2a: removing the specialist before the save must not let its notice in."""
    notice = _notice("browser", status="ok", text=FIDO_ANSWER)
    del roles["browser"]
    assert delegated_memory.notice_from_excluded_role(notice) is True


@pytest.mark.asyncio
async def test_a_handoff_task_written_by_a_zero_budget_specialist_is_not_banked(
        roles, monkeypatch):
    """p3r2a: the browsing specialist hands finance its own report; finance
    banks its answer, never the report."""
    import agent as agent_mod
    banked: list = []

    async def _retain(sem, *, origin_channel, turns, **kw):
        banked.extend(t.text for t in turns)

    monkeypatch.setattr(tools, "retain_delegated", _retain)
    monkeypatch.setattr(agent_mod, "active_semantic_memory", object(), raising=False)

    class _Client:
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return None
        async def query(self, prompt): return None
        async def receive_response(self):
            from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
            yield AssistantMessage(content=[TextBlock(text="finance answer")], model="x")
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=1, session_id="s")

    monkeypatch.setattr(tools, "ClaudeSDKClient", _Client)
    tok = agent_mod.origin_var.set({
        "role": "assistant", "execution_role": "browser", "channel": "telegram",
        "chat_id": "1", "user_id": 1, "cid": "c", "delegation_depth": 1,
        "_handoff_chain": ("assistant", "browser")})
    try:
        await tools._run_delegated_agent(roles["finance"], FIDO_ANSWER, "")
    finally:
        agent_mod.origin_var.reset(tok)
    await asyncio.gather(*list(tools._specialist_bg_tasks), return_exceptions=True)
    assert FIDO_ANSWER not in banked and "finance answer" in banked


def test_a_resident_line_that_merely_quotes_the_header_is_not_a_notice(roles):
    assert delegated_memory.notice_from_excluded_role(
        "Look: [System notification: your delegation to browser (id x)") is False


class _Msg:
    def __init__(self, type_, text):
        self.type = type_
        self.message = {"role": type_, "content": text}


class _EmptyBank:
    async def document_tags(self, bank, document_id):
        return None


async def _private(_text):
    return "private"


@pytest.mark.asyncio
async def test_cold_save_drops_the_notice_and_keeps_the_relay(roles, monkeypatch):
    monkeypatch.setattr(session_saver, "classify_tier", _private)
    messages = [
        _Msg("user", "Download my RunPod invoice"),
        _Msg("assistant", "Looking it up."),
        _Msg("user", _notice("browser", status="ok", text=FIDO_ANSWER)),
        _Msg("assistant", "Your RunPod invoice is in the chat."),
        _Msg("user", _notice("finance", status="ok", text="Q3 is complete")),
    ]
    items = await session_saver.transcript_to_items(
        messages, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=SYSTEM,
        semantic_memory=_EmptyBank())
    texts = [i["content"] for i in items]
    assert not any(FIDO_ANSWER in t for t in texts)
    assert "Download my RunPod invoice" in texts
    assert "Your RunPod invoice is in the chat." in texts
    assert any("Q3 is complete" in t for t in texts)          # finance writes memory


class _Sem:
    def __init__(self):
        self.retain_calls = []

    async def retain(self, bank, items, *, async_=True):
        self.retain_calls.append(items)


@pytest.mark.asyncio
@pytest.mark.parametrize("role,expected", [("browser", 0), ("finance", 1)])
async def test_specialist_engagement_summary_follows_the_switch(
        roles, tmp_path, monkeypatch, role, expected):
    import agent as agent_mod
    from engagement_registry import EngagementRegistry

    sem = _Sem()
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)
    monkeypatch.setattr(delegated_memory, "classify_tier", _private)
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(kind="specialist", role_or_type=role, driver="in_casa",
                           task="t", origin={"role": "assistant", "channel": "telegram",
                                             "chat_id": "12345"}, topic_id=42)
    telegram = MagicMock()
    telegram.send_to_topic = AsyncMock()
    telegram.close_topic = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = telegram
    bus = MagicMock()
    bus.notify = AsyncMock()
    tools.init_tools(channel_manager=cm, bus=bus, specialist_registry=MagicMock(),
                     mcp_registry=MagicMock(), trigger_registry=MagicMock(),
                     engagement_registry=reg)
    monkeypatch.setattr(tools, "_agent_role_map", roles, raising=False)
    driver = MagicMock()
    driver.cancel = AsyncMock()
    await tools._finalize_engagement(rec, outcome="completed", text=FIDO_ANSWER,
                                     artifacts=[], next_steps=[], driver=driver)
    await asyncio.gather(*list(tools._specialist_bg_tasks), return_exceptions=True)
    assert len(sem.retain_calls) == expected
    bus.notify.assert_awaited_once()                      # the requester is still told
    if expected:
        assert json.loads(sem.retain_calls[0][0]["content"])["kind"] == "engagement_summary"
