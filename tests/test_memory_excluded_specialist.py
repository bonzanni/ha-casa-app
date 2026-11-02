"""INV-MEM-032, the capture half: an excluded specialist's output — a loaded
specialist whose ``memory.token_budget`` is 0, or a role no longer loaded — is
never stored in ``events`` or ``engagement_summaries``, through every path its
output reaches a capture seam; the resident's own words around it are.

The notices and relays are the real producers the INV-MEM-024 tests use
(``Agent._synthesize_delegation_turn``, ``plugin_continuations``), and every
seam is the real one, against a real store.
"""
from __future__ import annotations

import asyncio
import secrets
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agent as agent_mod
import plugin_continuations as pc
import tools
from bus import BusMessage, MessageType
from test_agent_process import FakeClient, _make_agent
from test_memory_capture import events, mem, summaries  # noqa: F401 — fixture
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
    monkeypatch.setattr(agent_mod, "active_semantic_memory", None,
                        raising=False)
    return m


def _stored_text(store) -> list[str]:
    return ([e["text"] for e in events(store)]
            + [s["text"] for s in summaries(store)])


def _assert_no_canary(store) -> None:
    leaked = [t for t in _stored_text(store) if CANARY in t]
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
    rows = events(mem)
    assert [(e["speaker_kind"], e["origin_kind"]) for e in rows] == [
        ("resident", "delegated_result"),
        ("casa", "delegated_result"), ("resident", "delegated_result")]
    assert "Q3 is complete" in rows[1]["text"]
    assert rows[0]["text"] == rows[2]["text"] == "Your invoice is saved."
    _assert_no_canary(mem)


async def test_an_excluded_delegation_captures_nothing(roles, mem):
    await _delegate(roles["browser"], "fetch my invoice", ORIGIN,
                    EXCLUDED_ANSWER)
    assert events(mem) == []
    del roles["browser"]                        # a role no longer loaded
    await _delegate(_cfg("browser", "specialist", 0), "fetch it again",
                    ORIGIN, EXCLUDED_ANSWER)
    assert events(mem) == []


async def test_a_handoff_task_from_an_excluded_specialist_is_not_captured(
        roles, mem):
    """The browsing specialist hands finance its own report: finance's
    answer is captured, the report never."""
    origin = {**ORIGIN, "execution_role": "browser", "delegation_depth": 1,
              "_handoff_chain": ("assistant", "browser")}
    await _delegate(roles["finance"], EXCLUDED_ANSWER, origin,
                    "finance answer")
    assert [e["text"] for e in events(mem)] == ["finance answer"]
    _assert_no_canary(mem)


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
    assert [e["text"] for e in events(mem)] == ["relay answer", "target answer"]
    _assert_no_canary(mem)


async def _finalize(tmp_path, monkeypatch, roles, role, text):
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(tmp_path / f"{role}.json"),
                             bus=None)
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
                                     artifacts=[], next_steps=[], driver=driver)
    await asyncio.gather(*list(tools._specialist_bg_tasks),
                         return_exceptions=True)


async def test_an_excluded_engagement_leaves_no_summary(roles, mem, tmp_path,
                                                        monkeypatch):
    await _finalize(tmp_path, monkeypatch, roles, "browser", EXCLUDED_ANSWER)
    assert summaries(mem) == []
    await _finalize(tmp_path, monkeypatch, roles, "finance", "Q3 is complete")
    rows = summaries(mem)
    assert [(r["role"], r["tier"]) for r in rows] == [("finance", "private")]
    assert "Q3 is complete" in rows[0]["text"]
    _assert_no_canary(mem)


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
    drv = InCasaDriver(topic_stream_factory=lambda tid: _TopicStream())
    rec = EngagementRecord(
        id="b" * 32, kind="specialist", role_or_type="browser",
        driver="in_casa", status="active", topic_id=7, started_at=0.0,
        last_user_turn_ts=0.0, last_idle_reminder_ts=0.0, completed_at=None,
        sdk_session_id=None, task="t", origin={"channel": "telegram"})
    drv._clients[rec.id] = _TopicClient()
    drv._locks[rec.id] = asyncio.Lock()
    ticket = drv.admit_inbound(rec.id, "find the invoice")
    await drv.send_user_turn(rec, "find the invoice", tg_message_id=9,
                             inbound_token=ticket)
    assert events(mem) == []
