"""Per-turn durable capture into ``memory.db`` (``memory/capture.py``).

Occurrence identity (INV-MEM-026): every key comes from one constructor; an
operator message with a provider id collapses on redelivery, every other live
emission has a key minted once when capture builds it — so repeats, retries'
outputs and regenerated answers stay distinct, and a spool drain re-run adds
nothing. Durability: an append returns a receipt only once the item is
committed or spooled and fsynced; a store failure spools, and appends and
drains are serialized so a drain removes only lines it stored.

The resident, delegated and engagement cases drive the real seams —
``Agent._process`` with an SDK double, ``tools._run_delegated_agent`` and
``InCasaDriver.send_user_turn`` — against a real SQLite store.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import agent as agent_mod
import tools
from bus import BusMessage, MessageType
from log_cid import cid_var
from memory import capture, occurrence
from memory.store import MemoryStore
from test_agent_process import (
    FakeClient, ScriptedToolClient, _make_agent, _mk_assistant, _mk_result,
    _person_typed, patch_retry_sleep,
)
from test_specialist_memory_tiers import _FakeSDKClient, _specialist_cfg

pytestmark = [pytest.mark.asyncio]

CHAT = "4242"


@pytest.fixture
def mem(tmp_path, monkeypatch):
    """A real store wired into the live seams; no notifier, nothing noticed."""
    store = MemoryStore(str(tmp_path / "memory"))
    assert store.open() is True, store.unavailable_reason
    monkeypatch.setitem(capture._wiring, "store", store)
    monkeypatch.setitem(capture._wiring, "notify", None)
    monkeypatch.setattr(capture, "_noticed", set())
    return store


def events(store: MemoryStore) -> list[dict]:
    conn = sqlite3.connect(store.path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY seq")]
    finally:
        conn.close()


def summaries(store: MemoryStore) -> list[dict]:
    conn = sqlite3.connect(store.path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM engagement_summaries ORDER BY id")]
    finally:
        conn.close()


def spool_lines(store: MemoryStore) -> list[dict]:
    try:
        raw = Path(capture.spool_path(store.directory)).read_bytes()
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _item(text: str = "a sample line", **kw) -> capture.Event:
    return capture.event(role="assistant", channel="telegram",
                         speaker_kind="resident", origin_kind="operator",
                         text=text, eligibility="resident_message",
                         origin_tier="private", **kw)


def _operator_msg(text: str = "ping", message_id: str = "501",
                  **context) -> BusMessage:
    msg = BusMessage(
        type=MessageType.CHANNEL_IN, source="telegram", target="assistant",
        content=text, channel="telegram",
        context={"chat_id": CHAT, "user_id": 9001, "message_id": message_id,
                 "_origin_route": "telegram", "_origin_clearance": "private",
                 "_operator_turn": True, **context})
    msg.trusted_user_origin = _person_typed()
    return msg


def _system_msg(text: str = "ping") -> BusMessage:
    """A telegram turn no person typed (no provider id): a system turn."""
    return BusMessage(
        type=MessageType.CHANNEL_IN, source="telegram", target="assistant",
        content=text, channel="telegram",
        context={"chat_id": CHAT, "_origin_route": "telegram",
                 "_origin_clearance": "private"})


def _down(store: MemoryStore) -> None:
    store._unavailable_reason = "simulated outage"


def _up(store: MemoryStore) -> None:
    store._unavailable_reason = None


# --- occurrence identity (INV-MEM-026) --------------------------------------

async def test_repeats_stay_distinct_retry_collapses(mem):
    """The same words said twice are two events; the same item stored twice
    (a retried append, a drain re-run) is one; the same provider message
    delivered twice is one."""
    one, two = _item("same words"), _item("same words")
    assert one.source_key != two.source_key
    for item in (one, two, one):
        receipt = capture.append(mem, item)
        assert receipt is not None and receipt.outcome == capture.STORED
    assert [e["text"] for e in events(mem)] == ["same words", "same words"]

    key = occurrence.provider_key("telegram", CHAT, "77")
    assert key == occurrence.provider_key("telegram", CHAT, "77")
    assert key != occurrence.provider_key("telegram", CHAT, "78")
    assert key != occurrence.provider_key("telegram", "4243", "77")
    for _ in range(2):
        capture.append(mem, _item("hello", source_key=key))
    assert [e["source_key"] for e in events(mem)].count(key) == 1
    assert len(events(mem)) == 3


async def test_two_turns_with_one_log_cid_stay_distinct(mem, tmp_path):
    """Two turns forced onto the same 8-hex log cid keep two full sets of
    events: no key is built from the cid or a position under it."""
    FakeClient.reset()
    agent = _make_agent(tmp_path)
    with patch("sdk_client_pool._default_make_client", FakeClient):
        for _ in range(2):
            token = cid_var.set("deadbeef")
            try:
                await agent._process(_system_msg("ping"))
            finally:
                cid_var.reset(token)
    rows = events(mem)
    assert [(e["speaker_kind"], e["text"]) for e in rows] == [
        ("casa", "ping"), ("resident", "pong"),
        ("casa", "ping"), ("resident", "pong")]
    assert len({e["source_key"] for e in rows}) == 4
    assert all("deadbeef" not in e["source_key"] for e in rows)


async def test_telegram_redelivery_collapses(mem, tmp_path):
    """The same (chat, message_id) admitted twice is one operator event."""
    FakeClient.reset()
    agent = _make_agent(tmp_path)
    with patch("sdk_client_pool._default_make_client", FakeClient):
        await agent._process(_operator_msg("ping", "501"))
        await agent._process(_operator_msg("ping", "501"))
    operator = [e for e in events(mem) if e["speaker_kind"] == "operator"]
    assert len(operator) == 1
    assert operator[0]["source_key"] == occurrence.provider_key(
        "telegram", CHAT, "501")
    assert operator[0]["provider_message_id"] == "501"
    assert operator[0]["text"] == "ping"


async def test_redelivery_with_a_regenerated_answer_keeps_both_answers(
        mem, tmp_path):
    """The same message admitted twice, with fresh channel state between,
    answered A and then B: one operator event and both answers."""
    for answer in ("answer A", "answer B"):
        home = tmp_path / answer.replace(" ", "-")
        home.mkdir()
        agent = _make_agent(home)
        FakeClient.reset()
        FakeClient.response_text = answer
        with patch("sdk_client_pool._default_make_client", FakeClient):
            await agent._process(_operator_msg("what is new?", "601"))
    FakeClient.reset()
    rows = events(mem)
    assert [e["text"] for e in rows if e["speaker_kind"] == "operator"] == [
        "what is new?"]
    assert [e["text"] for e in rows if e["speaker_kind"] == "resident"] == [
        "answer A", "answer B"]


async def test_internal_retry_output_is_captured(mem, tmp_path):
    """A turn whose first attempt wrote a message and then failed retryably,
    and whose retry wrote a different one, captures both, each under its own
    key."""
    agent = _make_agent(tmp_path)
    ScriptedToolClient.reset(
        [_mk_assistant("first try"), ConnectionError("broken")],
        [_mk_assistant("second try"), _mk_result("sid-retry")],
    )
    with patch("sdk_client_pool._default_make_client", ScriptedToolClient), \
            patch_retry_sleep():
        await agent._process(_system_msg("ping"))
    resident = [e for e in events(mem) if e["speaker_kind"] == "resident"]
    assert [e["text"] for e in resident] == ["first try", "second try"]
    assert len({e["source_key"] for e in resident}) == 2


# --- durability: the store, the spool and the drain -------------------------

async def test_store_failure_spools_and_never_drops_a_turn(mem, tmp_path,
                                                         monkeypatch):
    """An unavailable store and a busy one both spool, fsynced, with a
    receipt; the turn itself is unaffected."""
    FakeClient.reset()
    _down(mem)
    agent = _make_agent(tmp_path)
    with patch("sdk_client_pool._default_make_client", FakeClient):
        reply = await agent._process(_operator_msg("ping", "701"))
    assert reply == "pong"
    lines = spool_lines(mem)
    assert [(ln["table"], ln["item"]["text"]) for ln in lines] == [
        ("events", "ping"), ("events", "pong")]
    assert lines[0]["item"]["source_key"] == occurrence.provider_key(
        "telegram", CHAT, "701")
    _up(mem)
    assert events(mem) == []

    # A store held by another writer past the capture's bound spools too.
    monkeypatch.setattr(capture, "CAPTURE_BUSY_TIMEOUT_S", 0.05)
    holder = sqlite3.connect(mem.path, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        receipt = capture.append(mem, _item("while busy"))
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert receipt is not None and receipt.outcome == capture.SPOOLED
    assert [ln["item"]["text"] for ln in spool_lines(mem)] == [
        "ping", "pong", "while busy"]


async def test_spool_drains_at_next_healthy_append(mem):
    _down(mem)
    spooled = [capture.append(mem, _item(f"spooled {i}")) for i in range(3)]
    assert {r.outcome for r in spooled} == {capture.SPOOLED}
    assert len(spool_lines(mem)) == 3
    _up(mem)
    receipt = capture.append(mem, _item("healthy"))
    assert receipt.outcome == capture.STORED
    assert sorted(e["text"] for e in events(mem)) == [
        "healthy", "spooled 0", "spooled 1", "spooled 2"]
    assert not os.path.exists(capture.spool_path(mem.directory))


async def test_old_spool_sends_one_private_notice(mem):
    notify = AsyncMock()
    capture.configure(notify=notify)
    _down(mem)
    await capture.record(_item("first"))
    notify.assert_not_awaited()                     # a young spool says nothing
    path = Path(capture.spool_path(mem.directory))
    first = json.loads(path.read_text())
    first["spooled_at"] -= capture.SPOOL_NOTICE_AFTER_S + 60
    path.write_text(json.dumps(first) + "\n")
    await capture.record(_item("second"))
    await capture.record(_item("third"))
    notify.assert_awaited_once_with(capture.SPOOL_NOTICE)


async def test_no_model_call_on_capture(mem, tmp_path, monkeypatch):
    """Capture — stored, spooled and drained — calls no model: the turn's
    own SDK client is the only one built, and no classifier runs."""
    import delegated_memory
    import tier_classifier

    def _no_model(*_a, **_k):
        raise AssertionError("a model was called on the capture path")

    monkeypatch.setattr(tier_classifier, "classify_tier", _no_model)
    monkeypatch.setattr(delegated_memory, "classify_tier", _no_model)
    monkeypatch.setattr("claude_agent_sdk.query", _no_model)
    FakeClient.reset()
    agent = _make_agent(tmp_path)
    with patch("sdk_client_pool._default_make_client", FakeClient):
        await agent._process(_operator_msg("ping", "801"))
        _down(mem)
        await agent._process(_operator_msg("again", "802"))
        _up(mem)
    assert FakeClient.attempts == 1                  # the turns' own warm client
    capture.append(mem, _item("drains the spool"))
    assert [e["text"] for e in events(mem)] == [
        "ping", "pong", "drains the spool", "again", "pong"]


async def test_spool_drain_rerun_adds_each_item_once(mem, monkeypatch):
    """A drain killed after its commit, before it removed the spool, and run
    again leaves exactly one event per spooled item."""
    _down(mem)
    for i in range(3):
        capture.append(mem, _item(f"spooled {i}"))
    _up(mem)

    class Killed(BaseException):
        pass

    spool = capture.spool_path(mem.directory)
    real_unlink = os.unlink

    def _unlink(path, *a, **k):
        if os.fspath(path) == spool:
            raise Killed
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(os, "unlink", _unlink)
    with pytest.raises(Killed):
        capture.drain_spool(mem)
    monkeypatch.setattr(os, "unlink", real_unlink)
    assert len(events(mem)) == 3 and len(spool_lines(mem)) == 3
    assert capture.drain_spool(mem) == 0
    assert sorted(e["text"] for e in events(mem)) == [
        "spooled 0", "spooled 1", "spooled 2"]
    assert not os.path.exists(spool)


def _pause_drain(monkeypatch, drain_thread_name: str):
    """Pause the drain inside its write transaction, after its snapshot read,
    on its first insert; returns (paused, release)."""
    paused, release = threading.Event(), threading.Event()
    real_insert = capture._insert

    def _insert(conn, item):
        if threading.current_thread().name == drain_thread_name \
                and not paused.is_set():
            paused.set()
            assert release.wait(10)
        return real_insert(conn, item)

    monkeypatch.setattr(capture, "_insert", _insert)
    return paused, release


async def test_append_during_a_drain_is_kept(mem, monkeypatch):
    """A capture that arrives while a drain holds the store keeps its item:
    it times out on the store, spools, fsyncs and returns a receipt, and the
    line survives the drain's removal of the spool (it is either an event or
    a line at the spool path the next drain stores)."""
    monkeypatch.setattr(capture, "CAPTURE_BUSY_TIMEOUT_S", 0.1)
    links_at_fsync: list[int] = []
    real_fsync = capture._fsync

    def _fsync(fd):
        if not os.path.isdir(f"/proc/self/fd/{fd}"):
            links_at_fsync.append(os.fstat(fd).st_nlink)
        return real_fsync(fd)

    monkeypatch.setattr(capture, "_fsync", _fsync)
    reached_spool = threading.Event()
    real_spool_write = capture._spool_write

    def _spool_write(*a, **k):
        reached_spool.set()
        return real_spool_write(*a, **k)

    monkeypatch.setattr(capture, "_spool_write", _spool_write)

    _down(mem)
    capture.append(mem, _item("already spooled"))
    _up(mem)
    links_at_fsync.clear()
    paused, release = _pause_drain(monkeypatch, "drain")
    drain = threading.Thread(target=capture.drain_spool, args=(mem,),
                             name="drain")
    drain.start()
    assert paused.wait(10)

    late = _item("arrived during the drain")
    result: list = []
    appender = threading.Thread(
        target=lambda: result.append(capture.append(mem, late)))
    appender.start()
    # The capture reaches its spool write while the drain still holds the
    # lock and its transaction; the drain then unlinks the spool.
    assert reached_spool.wait(10)
    await asyncio.sleep(0.2)
    release.set()
    drain.join(10)
    appender.join(10)
    assert not drain.is_alive() and not appender.is_alive()

    receipt = result[0]
    assert receipt is not None and receipt.outcome == capture.SPOOLED
    assert links_at_fsync == [1], links_at_fsync
    stored = [e["source_key"] for e in events(mem)]
    lines = [ln["item"]["source_key"] for ln in spool_lines(mem)]
    assert (late.source_key in stored) != (late.source_key in lines)
    assert lines == [late.source_key]               # never lost, never doubled
    capture.drain_spool(mem)
    assert [e["text"] for e in events(mem)] == [
        "already spooled", "arrived during the drain"]


# --- tier floors and the delegated request ----------------------------------

async def test_model_output_floor_is_session_clearance(mem, tmp_path,
                                                       monkeypatch):
    """A captured model output's floor is the read clearance of the session
    that wrote it; every event is private and provisional at capture."""
    FakeClient.reset()
    agent = _make_agent(tmp_path)
    msg = _operator_msg("hello from a friend", "901", _origin_clearance="friends")
    msg.context.pop("_operator_turn")
    with patch("sdk_client_pool._default_make_client", FakeClient):
        await agent._process(msg)

    monkeypatch.setattr(agent_mod, "active_semantic_memory", None,
                        raising=False)
    token = agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram", "chat_id": CHAT,
        "cid": "c", "delegation_depth": 0, "_origin_route": "telegram",
        "_origin_clearance": "family"})
    _FakeSDKClient.reset("a family answer")
    try:
        with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
            await tools._run_delegated_agent(
                _specialist_cfg("finance"), "a family question", "")
    finally:
        agent_mod.origin_var.reset(token)
    rows = {e["text"]: e for e in events(mem)}
    assert rows["hello from a friend"]["speaker_kind"] == "third_party"
    assert rows["pong"]["origin_tier"] == "friends"
    assert rows["hello from a friend"]["origin_tier"] == "friends"
    assert rows["a family answer"]["origin_tier"] == "family"
    assert rows["a family question"]["origin_tier"] == "family"
    assert {(e["tier"], e["tier_status"]) for e in rows.values()} == {
        ("private", "provisional")}


async def test_delegated_request_captured_before_the_run(mem, monkeypatch):
    """The raw task is an event before the specialist's client exists; the
    answer follows when the run completes."""
    monkeypatch.setattr(agent_mod, "active_semantic_memory", None,
                        raising=False)
    seen_at_launch: list[list[str]] = []

    class _Client(_FakeSDKClient):
        def __init__(self, options):
            seen_at_launch.append([e["text"] for e in events(mem)])
            super().__init__(options)

    token = agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram", "chat_id": CHAT,
        "cid": "c", "delegation_depth": 0, "_origin_route": "telegram",
        "_origin_clearance": "private", "_unauthored_opening": False})
    _Client.reset("Q1 is on track")
    try:
        with patch.object(tools, "ClaudeSDKClient", _Client):
            await tools._run_delegated_agent(
                _specialist_cfg("finance"), "Q1 cashflow?", "some context")
    finally:
        agent_mod.origin_var.reset(token)
    assert seen_at_launch == [["Q1 cashflow?"]]
    rows = events(mem)
    assert [(e["speaker_kind"], e["speaker_id"], e["origin_kind"], e["text"])
            for e in rows] == [
        ("resident", "assistant", "operator", "Q1 cashflow?"),
        ("specialist", "finance", "delegated_result", "Q1 is on track")]
    assert rows[1]["session_id"] is not None


# --- the exit condition: each path, exact counts ----------------------------

class _TopicStream:
    async def emit(self, text):
        return None

    async def finalize(self, text):
        return None


class _EngagementClient:
    async def query(self, prompt):
        self.prompt = prompt

    async def receive_response(self):
        from test_specialist_limits import _mk_result_message
        yield _mk_assistant("first part")
        yield _mk_assistant("second part")
        yield _mk_result_message()


async def test_a_turn_a_delegation_and_an_engagement_turn_each_capture(
        mem, tmp_path, monkeypatch):
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRecord

    # A Telegram turn through the real _process: the operator's message and
    # the resident's answer.
    agent = _make_agent(tmp_path)
    FakeClient.reset()
    with patch("sdk_client_pool._default_make_client", FakeClient):
        await agent._process(_operator_msg("ping", "1001"))
    assert [(e["speaker_kind"], e["eligibility"]) for e in events(mem)] == [
        ("operator", "write_trusted_turn"), ("resident", "resident_message")]

    # A delegation: its request and its answer.
    monkeypatch.setattr(agent_mod, "active_semantic_memory", None,
                        raising=False)
    token = agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram", "chat_id": CHAT,
        "cid": "c", "delegation_depth": 0})
    _FakeSDKClient.reset("delegated answer")
    try:
        with patch.object(tools, "ClaudeSDKClient", _FakeSDKClient):
            await tools._run_delegated_agent(
                _specialist_cfg("finance"), "delegated task", "")
    finally:
        agent_mod.origin_var.reset(token)
    assert [e["eligibility"] for e in events(mem)][2:] == [
        "delegated_request", "delegated_answer"]

    # An in-casa engagement turn of a memory-writing specialist: the
    # operator's topic message and each message the specialist wrote.
    drv = InCasaDriver(topic_stream_factory=lambda tid: _TopicStream())
    rec = EngagementRecord(
        id="e" * 32, kind="specialist", role_or_type="finance",
        driver="in_casa", status="active", topic_id=99, started_at=0.0,
        last_user_turn_ts=0.0, last_idle_reminder_ts=0.0, completed_at=None,
        sdk_session_id=None, task="t",
        origin={"channel": "telegram", "_origin_route": "telegram",
                "_origin_clearance": "private"})
    drv._clients[rec.id] = _EngagementClient()
    drv._locks[rec.id] = asyncio.Lock()
    ticket = drv.admit_inbound(rec.id, "topic message")
    await drv.send_user_turn(rec, "topic message", tg_message_id=55,
                             inbound_token=ticket)
    rows = events(mem)
    assert len(rows) == 7
    assert [(e["speaker_kind"], e["text"]) for e in rows[4:]] == [
        ("operator", "topic message"), ("specialist", "first part"),
        ("specialist", "second part")]
    assert rows[4]["source_key"] == occurrence.provider_key(
        "telegram", f"engagement:{rec.id}", "55")
    assert summaries(mem) == []
