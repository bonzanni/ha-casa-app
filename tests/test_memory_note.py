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
from memory import capture, corrections, note, view, writer
from memory_serving_support import fact, ledger_store, wire
from memory_writer_support import (
    ScriptedPort, new_writer, no_change_all, reply, seed,
)
from test_memory_frozen_view import _Client, _agent

pytestmark = [pytest.mark.unit]

KEY = "telegram:assistant:42"


def _note(store, key=KEY, clearance="private", sid=None) -> str:
    return note.for_turn(store, clearance=clearance, session_key=key,
                         sdk_session_id=sid or f"sid-{key}").text


def _start(store, key=KEY) -> None:
    """A session started on its view, bound to its SDK session."""
    served = view.serve(store, key, clearance="private", budget=1000)
    assert view.bind(store, key, served.view, f"sid-{key}")


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
    _start(store)
    assert _note(store) == ""                      # c1 is in the view
    _correction(store, "the dentist is Dr Rossi")
    assert "Dr Rossi" in _note(store)              # (a) pending
    await _run_jobs(store, no_change_all)
    assert "Dr Rossi" in _note(store)              # (b) committed after base
    assert "4417" not in _note(store)
    # The base belongs to the SDK session the view was bound to: another
    # session on the same chat is owed the pending corrections only.
    assert _note(store, sid="another-session") == ""
    # A session that started after that commit has it in its view.
    _start(store, "telegram:assistant:43")
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


async def test_a_correction_filed_while_a_session_starts_is_never_lost(
        tmp_path, monkeypatch):
    """The view a session starts on is read as of the same snapshot as its
    first turn's note, so a correction marked and filed between the two
    (here with ``no_change``, which changes no entry) is owed to the next
    turn. Red case: a view rendered from a later snapshot puts that commit
    under the session's base, and no note ever shows it."""
    store = ledger_store(tmp_path, [fact("code", "4471")])
    wire(monkeypatch, store)
    _QueryClient.options, _QueryClient.queries = [], []
    with patch("sdk_client_pool._default_make_client", _QueryClient):
        agent = _agent(tmp_path)
        real_serve = agent._serve_memory_view
        filed = []

        async def serve_after_a_correction(**kw):
            if not filed:
                filed.append(_correction(store, "the code is 4417"))
                await _run_jobs(store, no_change_all)
            return await real_serve(**kw)

        monkeypatch.setattr(agent, "_serve_memory_view",
                            serve_after_a_correction)
        await agent._process(_msg())                   # the view is served
        await agent._process(_msg())                   # a warm turn
        await agent.aclose()
    first, second = _QueryClient.queries
    assert "4417" not in first
    assert 'his words: "the code is 4417"' in second


@pytest.mark.parametrize("bind_fails", ["raises", "not_bound"])
async def test_a_session_whose_view_could_not_be_bound_is_not_kept_warm(
        tmp_path, monkeypatch, bind_fails):
    """A session whose view record could not be bound to it has no base for
    its note, so its warm client is let go after the turn: the next turn
    reconnects, finds no record bound to the session and starts afresh on a
    current view. Red case: keeping the warm client serves the old view, and
    a correction committed meanwhile never reaches the session."""
    import sqlite3
    from memory_serving_support import add
    store = ledger_store(tmp_path)
    _, [city] = add(store, [fact("city", "Rome")])
    wire(monkeypatch, store)
    real_bind = view.bind

    def failing_bind(*a, **kw):
        if bind_fails == "raises":
            raise sqlite3.OperationalError("database is locked")
        return False

    _QueryClient.options, _QueryClient.queries = [], []
    with patch("sdk_client_pool._default_make_client", _QueryClient):
        agent = _agent(tmp_path)
        monkeypatch.setattr(view, "bind", failing_bind)
        await agent._process(_msg())
        monkeypatch.setattr(view, "bind", real_bind)
        seq = _correction(store, "no, the city is Milan")
        await _run_jobs(store, lambda p: reply(
            {e["seq"]: "patched" for e in p["events"]},
            [{"op": "replace", "object_id": city, "body": "Milan",
              "source_seqs": [seq]}]))
        await agent._process(_msg())
        await agent.aclose()
    first, second = _QueryClient.options
    assert "Rome" in first.system_prompt
    assert second.resume is None and "- city: Milan" in second.system_prompt


async def test_a_session_whose_view_could_not_be_recorded_is_not_kept_warm(
        tmp_path, monkeypatch):
    """A session used again (warm or resumed) must have a record bound to
    it. When a new session's view cannot be recorded (the store's write lock
    is held through its first turn), it has none, so its warm client is let
    go after the turn and the next turn starts afresh on a current view.
    Red case: keeping the warm client leaves the session without a base, and
    a correction committed meanwhile never reaches it."""
    import sqlite3
    from memory_serving_support import add
    store = ledger_store(tmp_path)
    _, [city] = add(store, [fact("city", "Rome")])
    wire(monkeypatch, store)
    _QueryClient.options, _QueryClient.queries = [], []
    with patch("sdk_client_pool._default_make_client", _QueryClient):
        agent = _agent(tmp_path)
        lock = sqlite3.connect(store.path, isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        try:
            await agent._process(_msg())
        finally:
            lock.execute("ROLLBACK")
            lock.close()
        capture.drain_spool(store)  # what the lock spooled, stored now
        seq = _correction(store, "no, the city is Milan")
        await _run_jobs(store, lambda p: reply(
            {e["seq"]: "patched" for e in p["events"]},
            [{"op": "replace", "object_id": city, "body": "Milan",
              "source_seqs": [seq]}]))
        await agent._process(_msg())
        await agent.aclose()
    first, second = _QueryClient.options
    assert "<memory>" not in first.system_prompt
    assert second.resume is None and "- city: Milan" in second.system_prompt


async def test_a_session_kept_while_the_store_cannot_be_read(
        tmp_path, monkeypatch):
    """The settled exception: when the store cannot be read at all, the
    session is kept on its own pinned view rather than reset."""
    store = ledger_store(tmp_path, [fact("city", "Rome")])
    wire(monkeypatch, store)
    _QueryClient.options, _QueryClient.queries = [], []
    with patch("sdk_client_pool._default_make_client", _QueryClient):
        agent = _agent(tmp_path)
        await agent._process(_msg())
        store._unavailable_reason = "a simulated outage"
        await agent._process(_msg())
        await agent._process(_msg())
        await agent.aclose()
    assert len(_QueryClient.options) == 1


def _backdate_commits(store, seconds: float) -> None:
    """Move every commit so far *seconds* into the past."""
    from datetime import datetime, timedelta, timezone
    at = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(
        timespec="microseconds")
    with store.transaction(write=True) as conn:
        for table in ("dispositions", "revisions"):
            conn.execute(f"UPDATE {table} SET committed_at = ? "
                         "WHERE committed_at IS NOT NULL", (at,))


async def test_an_engagement_owes_a_correction_committed_as_it_started(
        tmp_path, monkeypatch):
    """A commit can be stamped before an engagement starts and land after it
    (the stamp is taken inside the commit's transaction). The engagement's
    base is therefore conservative: commits within ENGAGEMENT_BASE_MARGIN_S
    of its start count as after it, and their corrections are noted, at
    worst once more than needed. Red case: a base taken at the start itself
    includes that commit, and no turn ever notes its correction."""
    import time as _time
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRecord
    from test_memory_capture import _TopicStream
    store = ledger_store(tmp_path)
    wire(monkeypatch, store)
    monkeypatch.setattr(tools, "_agent_role_map", {}, raising=False)
    _correction(store, "the city is Milan")
    await _run_jobs(store, no_change_all)          # stamped just before the start
    rec = EngagementRecord(
        id="b" * 32, kind="specialist", role_or_type="finance",
        driver="in_casa", status="active", topic_id=99,
        started_at=_time.time(), last_user_turn_ts=0.0,
        last_idle_reminder_ts=0.0, completed_at=None, sdk_session_id=None,
        task="t", origin={"channel": "telegram", "_origin_route": "telegram",
                          "_origin_clearance": "private"})
    drv = InCasaDriver(topic_stream_factory=lambda tid: _TopicStream())
    _EngagementClient.prompts = []
    drv._clients[rec.id] = _EngagementClient()
    drv._locks[rec.id] = asyncio.Lock()
    for i, text in enumerate(("launch", "second")):
        ticket = drv.admit_inbound(rec.id, text)
        await drv.send_user_turn(rec, text, tg_message_id=i,
                                 inbound_token=ticket)
    for prompt in _EngagementClient.prompts:
        assert 'his words: "the city is Milan"' in prompt


async def test_an_engagement_owes_every_correction_filed_since_it_started(
        tmp_path, monkeypatch):
    """An engagement's base is the newest commit at or before its own start,
    derived, never recorded, so no store failure at its launch can move it:
    a correction filed after the engagement started is owed to every later
    turn, and one filed well before it is not. Red case: a base recorded at the
    first turn whose write succeeds (here the launch's write is locked out)
    lands after the correction, and no turn ever notes it."""
    import sqlite3
    import time as _time
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRecord
    from test_memory_capture import _TopicStream
    store = ledger_store(tmp_path)
    wire(monkeypatch, store)
    monkeypatch.setattr(tools, "_agent_role_map", {}, raising=False)
    _correction(store, "the code is 4417")
    await _run_jobs(store, no_change_all)
    _backdate_commits(store, 3600)                 # filed long before the start
    drv = InCasaDriver(topic_stream_factory=lambda tid: _TopicStream())
    rec = EngagementRecord(
        id="a" * 32, kind="specialist", role_or_type="finance",
        driver="in_casa", status="active", topic_id=99,
        started_at=_time.time(), last_user_turn_ts=0.0,
        last_idle_reminder_ts=0.0, completed_at=None, sdk_session_id=None,
        task="t", origin={"channel": "telegram", "_origin_route": "telegram",
                          "_origin_clearance": "private"})
    _EngagementClient.prompts = []
    drv._clients[rec.id] = _EngagementClient()
    drv._locks[rec.id] = asyncio.Lock()

    async def turn(text, message_id):
        ticket = drv.admit_inbound(rec.id, text)
        await drv.send_user_turn(rec, text, tg_message_id=message_id,
                                 inbound_token=ticket)

    lock = sqlite3.connect(store.path, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    try:
        await turn("launch", 1)
    finally:
        lock.execute("ROLLBACK")
        lock.close()
    capture.drain_spool(store)  # what the lock spooled, stored now
    _correction(store, "the city is Milan")
    await _run_jobs(store, no_change_all)          # filed after the start
    await turn("second", 2)
    await turn("third", 3)
    for prompt in _EngagementClient.prompts[1:]:
        assert 'his words: "the city is Milan"' in prompt
        assert "4417" not in prompt
