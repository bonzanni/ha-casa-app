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
from memory import capture, sessions, view
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
    assert row.sdk_session_id is None              # unbound until named
    assert view.bind(store, KEY, served.view, "sid-1")
    add(store, [fact("pet", "a cat")])
    again = view.serve(store, KEY, clearance="private", budget=1000,
                       resume_sid="sid-1")
    assert not again.reset
    assert again.view.text == served.view.text and "a cat" not in again.view.text
    assert _row(store).sdk_session_id == "sid-1"


def test_a_record_resumes_only_the_session_it_is_bound_to(tmp_path):
    """A view belongs to one SDK session. Red case: a record accepted for any
    session lets another session resume on a view it was never served."""
    store = ledger_store(tmp_path, [fact("city", "Rome")])
    served = view.serve(store, KEY, clearance="private", budget=1000)
    assert view.serve(store, KEY, clearance="private", budget=1000,
                      resume_sid="sid-1").reset is True    # unbound
    assert view.bind(store, KEY, served.view, "sid-1")
    assert view.serve(store, KEY, clearance="private", budget=1000,
                      resume_sid="sid-2").reset is True
    # A record replaced since the view was served is not bound to it.
    view.serve(store, KEY, clearance="private", budget=999)
    assert not view.bind(store, KEY, served.view, "sid-1")


@pytest.mark.parametrize("change", ["no_record", "budget", "renderer",
                                    "clearance", "hash"])
def test_a_view_that_cannot_be_reproduced_starts_afresh_once(
        tmp_path, change, caplog):
    """Red case: trusting the record without re-rendering resumes the
    session on a view nobody can show it was served. A reset renders and
    records nothing: the caller drops the resume pointer first, then serves
    a new session."""
    store = ledger_store(tmp_path, [fact("city", "Rome")])
    if change != "no_record":
        first = view.serve(store, KEY, clearance="private", budget=1000)
        assert view.bind(store, KEY, first.view, "sid-1")
        with store.transaction(write=True) as conn:
            conn.execute({
                "budget": "UPDATE session_views SET budget = 999",
                "renderer": "UPDATE session_views SET renderer_version = '0'",
                "clearance": "UPDATE session_views SET clearance = 'family'",
                "hash": "UPDATE session_views SET block_hash = 'x'",
            }[change])
    before = _row(store)
    add(store, [fact("pet", "a cat")])
    served = view.serve(store, KEY, clearance="private", budget=1000,
                        resume_sid="sid-1")
    assert (served.view, served.reset) == (None, True)
    assert _row(store) == before                  # nothing recorded yet
    assert any("starts afresh" in r.message for r in caplog.records)
    fresh = view.serve(store, KEY, clearance="private", budget=1000)
    assert "a cat" in fresh.view.text and _row(store).base_commit_no == 2
    assert view.bind(store, KEY, fresh.view, "sid-2")
    # Once: the next reconnect of the new session reproduces its record.
    assert view.serve(store, KEY, clearance="private", budget=1000,
                      resume_sid="sid-2").reset is False


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


class _FlakyClient(_Client):
    """Times out on the first query of the test, then answers."""

    failures: list = []

    async def query(self, text):
        if type(self).failures:
            raise type(self).failures.pop(0)


async def test_a_retry_never_resumes_the_session_a_reset_replaced(
        tmp_path, monkeypatch):
    """A cold reconnect whose view cannot be reproduced starts afresh, and a
    retry of that same turn (a connect timeout) must not then resume the old
    session on the new view. Red case: replacing the view record while the
    session pointer still names the old session lets the retry resume it."""
    import asyncio
    from test_agent_process import patch_retry_sleep
    store = ledger_store(tmp_path, [fact("city", "Rome")])
    wire(monkeypatch, store)
    key = build_scoped_session_key("telegram", "assistant", "42")
    reg = SessionRegistry(str(tmp_path / "s.json"))
    reg._data[key] = {
        "agent": resident_role_id("assistant"), "sdk_session_id": "old-sid",
        "binding_digest": RESIDENT_DIGEST,
        "last_active": (datetime.now(timezone.utc)
                        - timedelta(minutes=5)).isoformat(),
    }
    reg._write(reg._data)
    _FlakyClient.options = []
    _FlakyClient.failures = [asyncio.TimeoutError()]
    with patch("sdk_client_pool._default_make_client", _FlakyClient), \
            patch_retry_sleep():
        agent = _agent(tmp_path)
        align_prompt_surface(agent._session_registry, agent, "telegram", "42")
        await agent._process(_msg())
        await agent.aclose()
    assert [o.resume for o in _FlakyClient.options] == [None, None]
    assert all("Rome" in o.system_prompt for o in _FlakyClient.options)
    assert _row(store, key).base_commit_no == 1


def _sender(clearance: str) -> BusMessage:
    msg = _msg()
    msg.context.update(_origin_route="telegram", _origin_clearance=clearance)
    return msg


@pytest.mark.parametrize("order", [("private", "public"),
                                   ("public", "private")])
async def test_a_turn_never_runs_on_a_session_of_another_clearance(
        tmp_path, monkeypatch, order):
    """The reader's clearance is part of the session's identity: a turn
    whose clearance differs from its chat's session starts a session at its
    own clearance, never reusing or resuming a client built for another.
    Red case: reuse keyed without the clearance hands the public sender a
    client whose prompt holds the private ledger."""
    store = ledger_store(tmp_path, [fact("city", "PRIVATE-ROME")])
    wire(monkeypatch, store)
    _Client.options = []
    with patch("sdk_client_pool._default_make_client", _Client):
        agent = _agent(tmp_path)
        for clearance in order:
            await agent._process(_sender(clearance))
        await agent.aclose()
    assert len(_Client.options) == 2
    for clearance, options in zip(order, _Client.options):
        assert options.resume is None
        assert ("PRIVATE-ROME" in options.system_prompt) == (
            clearance == "private")


class _SessionClient(_Client):
    """Each client is a new SDK session with its own id."""

    count = 0

    async def receive_response(self):
        type(self).count += 1
        sid = f"sid-{type(self).count}"
        async for message in super().receive_response():
            if getattr(message, "session_id", None) == "sid-frozen":
                message.session_id = sid  # type: ignore[attr-defined]
            yield message


async def test_a_session_whose_view_was_never_recorded_never_resumes_another(
        tmp_path, monkeypatch):
    """A view record belongs to the SDK session it was served to. When a new
    session's record cannot be written (the store's write lock is held
    through its first turn), its reconnect must start afresh rather than
    resume on the chat's previous session's record. Red case: a record
    keyed by the chat alone is accepted for the new session."""
    import sqlite3
    store = ledger_store(tmp_path, [fact("city", "Rome")])
    wire(monkeypatch, store)
    key = build_scoped_session_key("telegram", "assistant", "42")
    _SessionClient.options, _SessionClient.count = [], 0
    with patch("sdk_client_pool._default_make_client", _SessionClient):
        first = _agent(tmp_path)
        await first._process(_msg())                     # session sid-1
        entry = first._session_registry._data[key]
        entry["last_active"] = (datetime.now(timezone.utc)
                                - timedelta(days=30)).isoformat()
        add(store, [fact("pet", "a cat")])
        lock = sqlite3.connect(store.path, isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        try:
            await first._process(_msg())                 # session sid-2
        finally:
            lock.execute("ROLLBACK")
            lock.close()
        capture.drain_spool(store)  # what the lock spooled, stored now
        await first.aclose()
        assert first._session_registry.get(key)["sdk_session_id"] == "sid-2"
        second = _agent(tmp_path)
        await second._process(_msg())                    # cold reconnect
        await second.aclose()
    one, two, three = _SessionClient.options
    assert (one.resume, two.resume) == (None, None)
    assert "<memory>" not in two.system_prompt           # never recorded
    assert three.resume is None                          # not sid-2
    assert "a cat" in three.system_prompt


#: What serving may add to a turn that starts a session while another
#: connection holds the store's write lock: its record write gives up at
#: serving's short busy bound and the turn runs without a view.
SERVING_OVERHEAD_BOUND_S = 0.5


async def test_a_held_write_lock_never_holds_a_starting_turn(
        tmp_path, monkeypatch):
    """Serving's store transactions on the turn path wait no longer than
    capture's: a contended store degrades a starting turn to no view at
    once. Red case: a one-second busy timeout on the record write holds the
    turn past the bound."""
    import asyncio
    import sqlite3
    store = ledger_store(tmp_path, [fact("city", "Rome")])
    wire(monkeypatch, store)
    loop = asyncio.get_running_loop()

    async def timed(agent, chat):
        start = loop.time()
        msg = _msg()
        msg.context["chat_id"] = chat
        await agent._process(msg)
        return loop.time() - start

    _Client.options = []
    with patch("sdk_client_pool._default_make_client", _Client):
        agent = _agent(tmp_path)
        await timed(agent, "warm-up")
        baseline = await timed(agent, "baseline")
        lock = sqlite3.connect(store.path, isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        try:
            held = await timed(agent, "held")
        finally:
            lock.execute("ROLLBACK")
            lock.close()
        await agent.aclose()
    assert "<memory>" not in _Client.options[-1].system_prompt
    assert held - baseline < SERVING_OVERHEAD_BOUND_S, (held, baseline)
