"""A session's view is frozen at its start.

A new session renders the ledger and records its base in ``session_views``;
a later commit never changes what that session was served. A cold
reconnect renders the view again as of the recorded base and checks its
hash; a session whose view cannot be reproduced (no record, another budget
or renderer, a hash that differs) starts afresh once. Real store; the
resident turn runs through the real ``Agent._process`` and pool with the
CLI client stubbed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agent import Agent
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from mcp_registry import McpServerRegistry
from memory import sessions, view
from memory_serving_support import add, fact, ledger_store, wire
from session_reg_helpers import (
    RESIDENT_DIGEST, align_prompt_surface, resident_prov, resident_role_id,
)
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

pytestmark = [pytest.mark.unit]

KEY = "telegram:assistant:42"


def _row(store, key=KEY):
    with store.transaction() as conn:
        return sessions.get(conn, key)


# --- the store half ------------------------------------------------------------

def test_a_new_session_records_its_base_and_a_commit_leaves_it_frozen(
        tmp_path):
    store = ledger_store(tmp_path, [fact("city", "Rome")])
    served = view.serve(store, KEY, clearance="private", budget=1000)
    assert not served.reset and "Rome" in served.view.text
    row = _row(store)
    assert (row.ledger, row.base_commit_no, row.clearance, row.budget,
            row.renderer_version, row.block_hash) == (
        "private", 1, "private", 1000, view.RENDERER_VERSION,
        served.view.block_hash)
    add(store, [fact("pet", "a cat")])
    again = view.serve(store, KEY, clearance="private", budget=1000,
                       resume=True)
    assert not again.reset
    assert again.view.text == served.view.text and "a cat" not in again.view.text
    assert _row(store) == row


@pytest.mark.parametrize("change", ["no_record", "budget", "renderer", "hash"])
def test_a_view_that_cannot_be_reproduced_starts_afresh_once(
        tmp_path, change, caplog):
    """Red case: trusting the record without re-rendering resumes the
    session on a view nobody can show it was served."""
    store = ledger_store(tmp_path, [fact("city", "Rome")])
    if change != "no_record":
        view.serve(store, KEY, clearance="private", budget=1000)
        with store.transaction(write=True) as conn:
            if change == "budget":
                conn.execute("UPDATE session_views SET budget = 999")
            elif change == "renderer":
                conn.execute("UPDATE session_views SET renderer_version = '0'")
            else:
                conn.execute("UPDATE session_views SET block_hash = 'x'")
    add(store, [fact("pet", "a cat")])
    served = view.serve(store, KEY, clearance="private", budget=1000,
                        resume=True)
    assert served.reset is True
    assert "a cat" in served.view.text            # a fresh view, recorded
    assert _row(store).base_commit_no == 2
    assert any("starts afresh" in r.message for r in caplog.records)
    # Once: the next reconnect reproduces the new record.
    assert view.serve(store, KEY, clearance="private", budget=1000,
                      resume=True).reset is False


def test_a_session_reading_no_ledger_is_served_nothing(tmp_path):
    store = ledger_store(tmp_path, [fact("city", "Rome")])
    assert view.serve(store, KEY, clearance="friends", budget=1000).view is None
    assert view.serve(store, KEY, clearance="private", budget=0).view is None
    assert _row(store) is None


# --- the turn half ---------------------------------------------------------------

class _Client:
    options: list = []

    def __init__(self, options):
        type(self).options.append(options)

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
            m = _AM.__new__(_AM)
            m.content = [block]  # type: ignore[attr-defined]
            yield m
        r = _RM.__new__(_RM)
        r.session_id = "sid-frozen"  # type: ignore[attr-defined]
        r.usage = {"input_tokens": 1, "output_tokens": 1}  # type: ignore[attr-defined]
        yield r


def _agent(tmp_path) -> Agent:
    cfg = AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT, role="assistant",
        model="claude-sonnet-4-6", system_prompt="You are assistant.",
        character=CharacterConfig(name="Assistant"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
        role_id=resident_role_id("assistant"), kind="resident",
        binding_digest=RESIDENT_DIGEST,
        speaker_provenance=resident_prov("assistant"),
    )
    return Agent(config=cfg,
                 session_registry=SessionRegistry(str(tmp_path / "s.json")),
                 mcp_registry=McpServerRegistry(),
                 channel_manager=ChannelManager())


def _msg() -> BusMessage:
    return BusMessage(type=MessageType.CHANNEL_IN, source="telegram",
                      target="assistant", content="hi", channel="telegram",
                      context={"chat_id": "42"})


async def test_a_cold_reconnect_resumes_on_the_frozen_view(
        tmp_path, monkeypatch):
    store = ledger_store(tmp_path, [fact("city", "Rome")])
    wire(monkeypatch, store)
    _Client.options = []
    with patch("sdk_client_pool._default_make_client", _Client):
        first = _agent(tmp_path)
        await first._process(_msg())
        await first.aclose()
        add(store, [fact("pet", "a cat")])
        # A new process: the pool is cold, the registry names the session.
        second = _agent(tmp_path)
        await second._process(_msg())
        await second.aclose()
    fresh, resumed = _Client.options
    assert fresh.resume is None and "Rome" in fresh.system_prompt
    assert resumed.resume == "sid-frozen"
    assert "<memory>" in resumed.system_prompt
    assert "a cat" not in resumed.system_prompt


async def test_a_cold_reconnect_without_a_recorded_view_starts_afresh(
        tmp_path, monkeypatch):
    """Red case: ignoring ``Served.reset`` resumes the old session."""
    store = ledger_store(tmp_path, [fact("city", "Rome")])
    wire(monkeypatch, store)
    reg = SessionRegistry(str(tmp_path / "s.json"))
    reg._data[build_scoped_session_key("telegram", "assistant", "42")] = {
        "agent": resident_role_id("assistant"), "sdk_session_id": "old-sid",
        "binding_digest": RESIDENT_DIGEST,
        "last_active": (datetime.now(timezone.utc)
                        - timedelta(minutes=5)).isoformat(),
    }
    reg._write(reg._data)
    _Client.options = []
    decisions = []
    with patch("sdk_client_pool._default_make_client", _Client):
        agent = _agent(tmp_path)
        # The session itself would resume: only its view stands in the way.
        align_prompt_surface(agent._session_registry, agent, "telegram", "42")
        real_decide = agent._pool._decide

        def _decide(*a):
            decision = real_decide(*a)
            decisions.append(decision.action)
            return decision

        agent._pool._decide = _decide
        await agent._process(_msg())
        await agent.aclose()
    assert decisions == ["resume"]
    [options] = _Client.options
    assert options.resume is None
    assert "Rome" in options.system_prompt
    key = build_scoped_session_key("telegram", "assistant", "42")
    assert _row(store, key).base_commit_no == 1
