"""Browser executor §5.4 (v9.1): the two predicates that keep a specialist
whose role reads no memory (``memory.token_budget: 0``) out of memory. The
capture seams that ask them are pinned by
``tests/test_memory_excluded_specialist.py`` (INV-MEM-032)."""
from __future__ import annotations

import types

import pytest

import delegated_memory
import tools
from config import AgentConfig, CharacterConfig, MemoryConfig

from role_artifact_stub import STUB_ROLE_ARTIFACT

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


def test_a_resident_line_that_merely_quotes_the_header_is_not_a_notice(roles):
    assert delegated_memory.notice_from_excluded_role(
        "Look: [System notification: your delegation to browser (id x)") is False
