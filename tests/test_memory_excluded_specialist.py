"""INV-MEM-032, the capture half: an excluded specialist's output — a loaded
specialist whose ``memory.token_budget`` is 0, or a role no longer loaded — is
never stored in ``events``, through every path its output reaches a capture
seam; the resident's own words around it are.

The notices and relays are the real producers
(``Agent._synthesize_delegation_turn``, ``plugin_continuations``), and every
seam is the real one, against a real store.
"""
from __future__ import annotations

import asyncio
import json
import secrets
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agent as agent_mod
import plugin_continuations as pc
import tools
from bus import BusMessage, MessageType
from test_agent_process import FakeClient, _make_agent
from test_memory_capture import captured_events
from test_memory_capture import mem  # noqa: F401 — fixture
from test_no_retain_specialist import _cfg, _notice
from test_plugin_continuations import _binding

pytestmark = [pytest.mark.asyncio]

#: Written only by the excluded specialist; must reach no table.
CANARY = secrets.token_hex(8)
EXCLUDED_ANSWER = f"saved the invoice {CANARY}"


@pytest.fixture
def roles(monkeypatch):
    m = {"browser": _cfg("browser", "specialist", 0),
         "finance": _cfg("finance", "specialist", 4000),
         "assistant": _cfg("assistant", "resident", 5000)}
    monkeypatch.setattr(tools, "_agent_role_map", m, raising=False)
    return m


async def events(store) -> list[dict]:
    """The captured events. A capture that met a busy store (a loaded machine
    holds the write lock past capture's 50 ms bound) waits durably in the
    spool; every question here is what was captured, not where it waited —
    and a canary waiting in the spool counts. ``captured_events`` settles the
    seams' detached writes, drains the spool and orders by occurrence."""
    return await captured_events(store)


async def _stored_text(store) -> list[str]:
    return [e["text"] for e in await events(store)]


async def _assert_no_canary(store) -> None:
    leaked = [t for t in await _stored_text(store) if CANARY in t]
    assert leaked == []


class _Client:
    """A specialist run that answers ``answer`` and completes."""

    answer = "finance answer"

    def __init__(self, options):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def query(self, prompt):
        return None

    async def receive_response(self):
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
        yield AssistantMessage(content=[TextBlock(text=type(self).answer)],
                               model="x")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                            is_error=False, num_turns=1, session_id="s")


async def _delegate(cfg, task, origin, answer):
    _Client.answer = answer
    token = agent_mod.origin_var.set(origin)
    try:
        with patch.object(tools, "ClaudeSDKClient", _Client):
            await tools._run_delegated_agent(cfg, task, "")
    finally:
        agent_mod.origin_var.reset(token)


ORIGIN = {"role": "assistant", "channel": "telegram", "chat_id": "1",
          "user_id": 1, "cid": "c", "delegation_depth": 0}


async def test_the_notice_is_not_captured_and_the_relay_is(roles, mem,
                                                           tmp_path):
    """The resident turn Casa opens with an excluded specialist's notice
    captures the resident's relay, never the notice that carries the answer;
    a memory-writing specialist's notice is captured as usual."""
    FakeClient.reset()
    FakeClient.response_text = "Your invoice is saved."
    agent = _make_agent(tmp_path)
    for role in ("browser", "finance"):
        notice = _notice(role, status="ok", text=EXCLUDED_ANSWER
                         if role == "browser" else "Q3 is complete")
        msg = BusMessage(type=MessageType.REQUEST, source=role,
                         target="assistant", content=notice, channel="telegram",
                         context={"chat_id": "1"})
        with patch("sdk_client_pool._default_make_client", FakeClient):
            await agent._process(msg, origin_question="get my invoice")
    FakeClient.reset()
    rows = await events(mem)
    assert [(e["speaker_kind"], e["origin_kind"]) for e in rows] == [
        ("resident", "delegated_result"),
        ("casa", "delegated_result"), ("resident", "delegated_result")]
    assert "Q3 is complete" in rows[1]["text"]
    assert rows[0]["text"] == rows[2]["text"] == "Your invoice is saved."
    await _assert_no_canary(mem)


async def test_an_excluded_delegation_captures_nothing(roles, mem):
    await _delegate(roles["browser"], "fetch my invoice", ORIGIN,
                    EXCLUDED_ANSWER)
    assert await events(mem) == []
    del roles["browser"]                        # a role no longer loaded
    await _delegate(_cfg("browser", "specialist", 0), "fetch it again",
                    ORIGIN, EXCLUDED_ANSWER)
    assert await events(mem) == []


async def test_a_handoff_task_from_an_excluded_specialist_is_not_captured(
        roles, mem):
    """The browsing specialist hands finance its own report: finance's
    answer is captured, the report never."""
    origin = {**ORIGIN, "execution_role": "browser", "delegation_depth": 1,
              "_handoff_chain": ("assistant", "browser")}
    await _delegate(roles["finance"], EXCLUDED_ANSWER, origin,
                    "finance answer")
    assert [e["text"] for e in await events(mem)] == ["finance answer"]
    await _assert_no_canary(mem)


@pytest.mark.parametrize("composed", [True, False],
                         ids=["inside-relay", "plain-hand-on"])
async def test_no_task_inside_a_relay_is_captured(roles, mem, composed):
    """A relay climbing back through finance, and a hand-off made from
    inside one, capture the executing role's answer, never the task."""
    relay = pc.continuation_origin(_binding(("assistant", "finance", "browser")),
                                   executing="finance")
    await _delegate(roles["finance"], EXCLUDED_ANSWER, relay, "relay answer")
    nested = {**pc.continuation_origin(
        _binding(("assistant", "finance", "browser")), executing="browser"),
        "execution_role": "browser", "_handoff_chain": ("assistant", "browser"),
        "delegation_depth": 1}
    if not composed:
        nested.pop("synthetic")
    await _delegate(roles["finance"], EXCLUDED_ANSWER, nested, "target answer")
    assert [e["text"] for e in await events(mem)] == ["relay answer", "target answer"]
    await _assert_no_canary(mem)


async def _finalize(tmp_path, monkeypatch, roles, role, text):
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(tmp_path / f"{role}.json"))
    rec = await reg.create(kind="specialist", role_or_type=role,
                           driver="in_casa", task="t",
                           origin={"role": "assistant", "channel": "telegram",
                                   "chat_id": "12345"}, topic_id=42)
    telegram = MagicMock()
    telegram.send_to_topic = AsyncMock()
    telegram.close_topic = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = telegram
    bus = MagicMock()
    bus.notify = AsyncMock()
    tools.init_tools(channel_manager=cm, bus=bus,
                     specialist_registry=MagicMock(), mcp_registry=MagicMock(),
                     trigger_registry=MagicMock(), engagement_registry=reg)
    monkeypatch.setattr(tools, "_agent_role_map", roles, raising=False)
    driver = MagicMock()
    driver.cancel = AsyncMock()
    await tools._finalize_engagement(rec, outcome="completed", text=text,
                                     next_steps=[], driver=driver)


async def test_a_finalized_engagement_captures_nothing(roles, mem, tmp_path,
                                                       monkeypatch):
    """The end of an engagement writes nothing into memory, of an excluded
    specialist or of one that keeps memory: the summary is told to the
    requester and stored nowhere. Red case: a finalize that captures its
    summary text."""
    await _finalize(tmp_path, monkeypatch, roles, "browser", EXCLUDED_ANSWER)
    await _finalize(tmp_path, monkeypatch, roles, "finance", "Q3 is complete")
    assert await events(mem) == []
    await _assert_no_canary(mem)


class _TopicStream:
    async def emit(self, text):
        return None

    async def finalize(self, text):
        return None


class _TopicClient:
    async def query(self, prompt):
        return None

    async def receive_response(self):
        from test_agent_process import _mk_assistant
        from test_specialist_limits import _mk_result_message
        yield _mk_assistant(EXCLUDED_ANSWER)
        yield _mk_result_message()


async def test_an_excluded_specialists_engagement_turn_captures_nothing(
        roles, mem):
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRecord
    from turn_scripted_session import TurnScriptedSession, attach_session

    class _Session(TurnScriptedSession, _TopicClient):
        pass

    ended: list = []
    drv = InCasaDriver(topic_stream_factory=lambda tid: _TopicStream(),
                       on_turn_end=lambda rec, result, api_error: ended.append(rec))
    rec = EngagementRecord(
        id="b" * 32, kind="specialist", role_or_type="browser",
        driver="in_casa", status="active", topic_id=7, started_at=0.0,
        last_user_turn_ts=0.0, completed_at=None,
        sdk_session_id=None, task="t", origin={"channel": "telegram"})
    attach_session(drv, rec, _Session())
    try:
        drv.submit_operator(rec, "find the invoice", tg_message_id=9)
        async with asyncio.timeout(2):
            while not ended:
                await asyncio.sleep(0.001)
    finally:
        await drv.cancel(rec)
    # The turn ran to its end: the operator's message and the answer were
    # both there to capture.
    assert len(ended) == 1
    assert await events(mem) == []


async def test_the_ledger_never_holds_excluded_output(roles, mem, tmp_path):
    """INV-MEM-032, the ledger half. Through the real notice producer, an
    excluded delegation and a hand-off from the excluded specialist, the
    store gets the resident's and finance's words only; the writer then
    files every pending event and neither its model prompt nor any object
    or revision of the ledger holds the excluded output."""
    from memory import writer
    from memory_writer_support import ScriptedPort, add_each, new_writer, rows

    FakeClient.reset()
    FakeClient.response_text = "Your invoice is saved."
    agent = _make_agent(tmp_path)
    notice = _notice("browser", status="ok", text=EXCLUDED_ANSWER)
    msg = BusMessage(type=MessageType.REQUEST, source="browser",
                     target="assistant", content=notice, channel="telegram",
                     context={"chat_id": "1"})
    with patch("sdk_client_pool._default_make_client", FakeClient):
        await agent._process(msg, origin_question="get my invoice")
    FakeClient.reset()
    await _delegate(roles["browser"], "fetch my invoice", ORIGIN,
                    EXCLUDED_ANSWER)
    await _delegate(roles["finance"], EXCLUDED_ANSWER,
                    {**ORIGIN, "execution_role": "browser",
                     "delegation_depth": 1,
                     "_handoff_chain": ("assistant", "browser")},
                    "finance answer")
    stored = await events(mem)
    assert len(stored) == 2

    port = ScriptedPort(add_each)
    job = writer.enqueue(mem, "private", writer.ORDINARY)
    assert await new_writer(mem, port).run_job(job) == writer.COMMITTED
    assert [e["seq"] for e in port.prompts[0]["events"]] == [
        e["seq"] for e in stored]
    assert CANARY not in json.dumps(port.prompts)
    ledger = rows(mem, "SELECT * FROM objects") + rows(
        mem, "SELECT * FROM revisions")
    assert len(ledger) == 4
    assert [r for r in ledger if CANARY in json.dumps(r)] == []


async def test_search_never_returns_excluded_output(roles, mem, tmp_path):
    """INV-MEM-032, the search half. Through the same real producers, a
    search of the saved conversation — by the excluded answer's own words,
    by its canary, and day by day — returns the resident's and finance's
    words and never the excluded output."""
    from datetime import datetime

    from memory_tools_support import payload
    from timekeeping import resolve_tz

    FakeClient.reset()
    FakeClient.response_text = "Your invoice is saved."
    agent = _make_agent(tmp_path)
    notice = _notice("browser", status="ok", text=EXCLUDED_ANSWER)
    msg = BusMessage(type=MessageType.REQUEST, source="browser",
                     target="assistant", content=notice, channel="telegram",
                     context={"chat_id": "1"})
    with patch("sdk_client_pool._default_make_client", FakeClient):
        await agent._process(msg, origin_question="get my invoice")
    FakeClient.reset()
    await _delegate(roles["browser"], "fetch my invoice", ORIGIN,
                    EXCLUDED_ANSWER)
    await _delegate(roles["finance"], EXCLUDED_ANSWER,
                    {**ORIGIN, "execution_role": "browser",
                     "delegation_depth": 1,
                     "_handoff_chain": ("assistant", "browser")},
                    "finance answer")
    # The producers' detached writes settle, and any capture that met the
    # lock they held (on a loaded machine) is drained, before memory is read.
    await captured_events(mem)
    agent_mod.origin_var.set({**ORIGIN, "_origin_route": "telegram",
                              "_origin_clearance": "private"})
    found = []
    for query in ("saved the invoice", CANARY, "finance answer"):
        found.append(payload(await tools.search_memory.handler(
            {"query": query})))
    texts = [h["text"] for out in found for h in out["hits"]]
    assert "Your invoice is saved." in texts and "finance answer" in texts
    day = payload(await tools.browse_memory_day.handler(
        {"date": datetime.now(resolve_tz()).date().isoformat()}))
    assert "finance answer" in day["text"]
    assert CANARY not in json.dumps(found) + json.dumps(day)
