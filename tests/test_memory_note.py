"""INV-MEM-030: the correction note on every turn of every memory role.

Every turn's prompt carries every correction the
session's frozen view does not reflect, read in that turn's own snapshot:
(a) every correction pending in the session's ledger (``deferred`` counts as
pending), and (b) every correction committed after the session's
``base_commit_no``. It rides on every turn — a fresh session, a warm turn, a
cold reconnect to a resumed session, any channel — so no compaction, reconnect
or resume can drop it; a delegated specialist and an in-casa engagement turn
carry it too. Real store and real writer commits; the resident turn runs the
real ``Agent._process`` with the CLI client stubbed.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import agent as agent_mod
import tools
from bus import BusMessage, MessageType
from memory import corrections, note, view, writer
from memory_serving_support import fact, ledger_store, wire
from memory_writer_support import (
    ScriptedPort, new_writer, no_change_all, reply, seed,
)
from test_memory_frozen_view import _Client, _agent

pytestmark = [pytest.mark.unit]

KEY = "telegram:assistant:42"


def _note(store, key=KEY, clearance="private") -> str:
    return note.for_turn(store, clearance=clearance, session_key=key).text


def _correction(store, words: str) -> int:
    seq = seed(store, words)
    assert corrections.mark(store, seq).marked
    return seq


async def _run_jobs(store, script) -> None:
    """Run the queued jobs, and an ordinary one for what is still pending."""
    writer.enqueue(store, "private", writer.ORDINARY)
    w = new_writer(store, ScriptedPort(script))
    while await w.step():
        pass


def test_the_note_is_the_operators_words_dated(tmp_path):
    store = ledger_store(tmp_path)
    _correction(store, "No, the boiler service is on the 14th")
    text = _note(store, key=None)
    assert text.startswith("<pending_corrections>\n" + note.HEAD + "\n- ")
    assert text.endswith(
        ', his words: "No, the boiler service is on the 14th"\n'
        "</pending_corrections>")


async def test_deferred_counts_as_pending(tmp_path):
    """Red case: a ``deferred`` disposition read
    as terminal drops the correction from the note before any commit
    reflects it."""
    store = ledger_store(tmp_path)
    _correction(store, "the code is 4417, not 4471")
    await _run_jobs(store, lambda p: reply({e["seq"]: "deferred"
                                            for e in p["events"]}))
    assert "4417" in _note(store, key=None)
    await _run_jobs(store, no_change_all)
    assert "4417" not in _note(store, key=None)


async def test_a_session_sees_every_correction_its_view_does_not_reflect(
        tmp_path):
    """The snapshot is consistent at session
    start and at later turns. Red case: dropping (b) loses a correction
    committed after the session started, though its frozen view never
    showed it."""
    store = ledger_store(tmp_path, [fact("code", "4471")])
    _correction(store, "the code is 4417")
    await _run_jobs(store, no_change_all)          # c1 filed before start
    view.serve(store, KEY, clearance="private", budget=1000)
    assert _note(store) == ""                      # c1 is in the view
    _correction(store, "the dentist is Dr Rossi")
    assert "Dr Rossi" in _note(store)              # (a) pending
    await _run_jobs(store, no_change_all)
    assert "Dr Rossi" in _note(store)              # (b) committed after base
    assert "4417" not in _note(store)
    # A session that started after that commit has it in its view.
    view.serve(store, "telegram:assistant:43", clearance="private",
               budget=1000)
    assert _note(store, "telegram:assistant:43") == ""


def test_a_clearance_that_reads_no_ledger_gets_no_note(tmp_path):
    store = ledger_store(tmp_path)
    _correction(store, "the code is 4417")
    assert _note(store, key=None, clearance="friends") == ""
    assert "4417" in _note(store, key=None, clearance="private")


# --- every turn of every memory role ---------------------------------------------

class _QueryClient(_Client):
    queries: list = []

    async def query(self, text):
        type(self).queries.append(text)


def _msg(channel="telegram", chat="42", **context) -> BusMessage:
    return BusMessage(type=MessageType.CHANNEL_IN, source=channel,
                      target="assistant", content="hi", channel=channel,
                      context={"chat_id": chat, **context})


async def test_every_resident_turn_carries_the_note(tmp_path, monkeypatch):
    """A fresh turn, a warm turn on the same
    client, a cold reconnect that resumes the session, and a turn on another
    channel each carry every unreflected correction. Red case: keeping the
    note to Telegram (where the notes block lived) or to the turn that built
    the session drops it from the others."""
    store = ledger_store(tmp_path, [fact("code", "4471")])
    wire(monkeypatch, store)
    _QueryClient.options, _QueryClient.queries = [], []
    with patch("sdk_client_pool._default_make_client", _QueryClient):
        first = _agent(tmp_path)
        await first._process(_msg())                       # fresh
        _correction(store, "the code is 4417")
        await first._process(_msg())                       # warm
        await first.aclose()
        second = _agent(tmp_path)
        await second._process(_msg())                      # cold, resumed
        await second._process(_msg(
            "webhook", "hook", _origin_route="invoke"))    # another channel
        await second.aclose()
    fresh, warm, resumed, webhook = _QueryClient.queries
    assert "<pending_corrections>" not in fresh
    for query in (warm, resumed, webhook):
        assert '<pending_corrections>' in query
        assert 'his words: "the code is 4417"' in query
    assert [o.resume for o in _QueryClient.options] == [
        None, "sid-frozen", None]


async def test_a_delegated_specialist_gets_its_view_then_the_note(
        tmp_path, monkeypatch):
    from test_specialist_memory_tiers import _FakeSDKClient, _specialist_cfg
    store = ledger_store(tmp_path, [fact("code", "4471")])
    wire(monkeypatch, store)
    _correction(store, "the code is 4417")
    monkeypatch.setattr(agent_mod, "active_semantic_memory", None,
                        raising=False)
    token = agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram", "chat_id": "42",
        "cid": "c", "delegation_depth": 0})
    _FakeSDKClient.reset("ok")
    try:
        with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
            await tools._run_delegated_agent(
                _specialist_cfg("finance"), "check the alarm", "")
    finally:
        agent_mod.origin_var.reset(token)
    prompt = _FakeSDKClient.captured_prompt
    assert prompt.index("- code: 4471") < prompt.index(
        'his words: "the code is 4417"') < prompt.index("Task:")


class _EngagementClient:
    prompts: list = []

    async def query(self, prompt):
        type(self).prompts.append(prompt)

    async def receive_response(self):
        from test_specialist_limits import _mk_result_message
        yield _mk_result_message()


async def test_an_in_casa_engagement_turn_carries_the_note(
        tmp_path, monkeypatch):
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRecord
    from test_memory_capture import _TopicStream
    store = ledger_store(tmp_path)
    wire(monkeypatch, store)
    _correction(store, "the code is 4417")
    monkeypatch.setattr(tools, "_agent_role_map", {}, raising=False)
    drv = InCasaDriver(topic_stream_factory=lambda tid: _TopicStream())
    rec = EngagementRecord(
        id="e" * 32, kind="specialist", role_or_type="finance",
        driver="in_casa", status="active", topic_id=99, started_at=0.0,
        last_user_turn_ts=0.0, last_idle_reminder_ts=0.0, completed_at=None,
        sdk_session_id=None, task="t",
        origin={"channel": "telegram", "_origin_route": "telegram",
                "_origin_clearance": "private"})
    _EngagementClient.prompts = []
    drv._clients[rec.id] = _EngagementClient()
    drv._locks[rec.id] = asyncio.Lock()
    ticket = drv.admit_inbound(rec.id, "topic message")
    await drv.send_user_turn(rec, "topic message", tg_message_id=55,
                             inbound_token=ticket)
    [prompt] = _EngagementClient.prompts
    assert prompt.startswith("<pending_corrections>")
    assert 'his words: "the code is 4417"' in prompt
    assert prompt.endswith("topic message")


async def test_an_engagement_sees_corrections_filed_between_its_turns(
        tmp_path, monkeypatch):
    """An engagement session has a recorded base from its first turn, so a
    correction the writer files between two of its turns is still owed to
    every later turn. Red case: an engagement note of pending corrections
    only loses it once filed."""
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRecord
    from test_memory_capture import _TopicStream
    store = ledger_store(tmp_path)
    wire(monkeypatch, store)
    monkeypatch.setattr(tools, "_agent_role_map", {}, raising=False)
    drv = InCasaDriver(topic_stream_factory=lambda tid: _TopicStream())
    rec = EngagementRecord(
        id="f" * 32, kind="specialist", role_or_type="finance",
        driver="in_casa", status="active", topic_id=99, started_at=0.0,
        last_user_turn_ts=0.0, last_idle_reminder_ts=0.0, completed_at=None,
        sdk_session_id=None, task="t",
        origin={"channel": "telegram", "_origin_route": "telegram",
                "_origin_clearance": "private"})
    _EngagementClient.prompts = []
    drv._clients[rec.id] = _EngagementClient()
    drv._locks[rec.id] = asyncio.Lock()

    async def turn(text, message_id):
        ticket = drv.admit_inbound(rec.id, text)
        await drv.send_user_turn(rec, text, tg_message_id=message_id,
                                 inbound_token=ticket)

    await turn("first", 1)
    _correction(store, "the code is 4417")
    await _run_jobs(store, no_change_all)          # filed, no longer pending
    await turn("second", 2)
    await turn("third", 3)
    first, second, third = _EngagementClient.prompts
    assert "4417" not in first
    for prompt in (second, third):
        assert 'his words: "the code is 4417"' in prompt
