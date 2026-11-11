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
``InCasaDriver.submit_operator`` into a live job session — against a real
SQLite store.
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
from channels.tg_address import TgAddress
from log_cid import cid_var
from memory import capture, occurrence, transcript_runs
from memory.store import MemoryStore
from test_agent_process import (
    FakeClient, ScriptedToolClient, _make_agent, _mk_assistant, _mk_result,
    _person_typed, patch_retry_sleep,
)
from test_specialist_memory_tiers import _FakeSDKClient, _specialist_cfg

pytestmark = [pytest.mark.asyncio]

CHAT = "4242"
BOT = 111          # the bot that received the operator's messages


@pytest.fixture
def mem(tmp_path, monkeypatch):
    """A real store wired into the live seams; no notifier, nothing noticed."""
    store = MemoryStore(str(tmp_path / "memory"))
    assert store.open() is True, store.unavailable_reason
    monkeypatch.setitem(capture._wiring, "store", store)
    monkeypatch.setitem(capture._wiring, "notify", None)
    monkeypatch.setattr(capture, "_noticed", set())
    monkeypatch.setattr(capture, "_background", set())
    monkeypatch.setattr(capture, "_drain", [None])
    return store


def events(store: MemoryStore) -> list[dict]:
    conn = sqlite3.connect(store.path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY seq")]
    finally:
        conn.close()


async def captured_events(store: MemoryStore) -> list[dict]:
    """Every event capture made durable, stored now, in the order they
    occurred. For a question about what a seam captured, not where it waited:
    on a loaded machine a run's own detached writes can hold the write lock
    past capture's 50 ms bound, and the capture then waits in the spool.

    Every write the seams detached (run-row writes, drains) is settled first,
    so the drain here never meets a store one of them still holds; then a
    waiting spool is drained. A drained item is stored after the items that
    followed it, so the rows are ordered by when they occurred."""
    await transcript_runs.settle()
    await capture.settle()
    capture.drain_waiting(store)
    return sorted(events(store), key=lambda e: e["occurred_at"])


def durable_texts(store: MemoryStore) -> list[str]:
    """The event texts capture has made durable, read without moving any:
    those waiting in the spool or a drain's snapshot, and the stored ones.
    A capture that met a busy store (on a loaded machine a run's own detached
    writes can hold the write lock past capture's 50 ms bound) is durable in
    the spool. Read in the order a drain moves items and keyed by source key,
    so a drain running meanwhile can neither hide an item nor count it twice;
    a torn line was never a receipt."""
    found: dict[str, str] = {}
    for path in (capture.spool_path(store.directory),
                 capture.snapshot_path(store.directory)):
        try:
            raw = Path(path).read_bytes()
        except FileNotFoundError:
            continue
        for line in raw.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record["table"] == "events":
                found.setdefault(record["item"]["source_key"],
                                 record["item"]["text"])
    for e in events(store):
        found.setdefault(e["source_key"], e["text"])
    return list(found.values())


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
                 "_operator_turn": True, "_tg_bot": BOT, **context})
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

    key = occurrence.message_key("telegram", BOT, CHAT, "77")
    assert key == occurrence.message_key("telegram", BOT, CHAT, "77")
    assert key != occurrence.message_key("telegram", BOT, CHAT, "78")
    assert key != occurrence.message_key("telegram", BOT, "4243", "77")
    assert key != occurrence.message_key("telegram", 222, CHAT, "77")
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
    assert operator[0]["source_key"] == occurrence.message_key(
        "telegram", BOT, CHAT, "501")
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


async def test_casas_notes_never_reach_the_captured_text(mem, tmp_path,
                                                       monkeypatch):
    """INV-MEM-022 at the capture boundary: a turn whose sent query carries
    Casa's notes block (a chat note and a reply note) captures only the
    operator's own words, never a note."""
    import chat_notes
    chat_notes.reset()
    chat_notes.record(BOT, int(CHAT), "📊 Finance answered your reply (1 page).")
    note = ("The person sent this as a reply to a message Casa posted for 📊 Finance, "
            "posted 2026-10-06 21:40, which read:\n«📊 Finance\nWhich quarter?»\n"
            "Their words answer that message, which may not be your latest one.")
    FakeClient.reset()
    agent = _make_agent(tmp_path)
    sent: list[str] = []
    real_query = FakeClient.query

    async def _recording_query(self, text):
        sent.append(text)
        return await real_query(self, text)

    monkeypatch.setattr(FakeClient, "query", _recording_query)
    with patch("sdk_client_pool._default_make_client", FakeClient):
        await agent._process(_operator_msg("start from Q2 2026", "811",
                                           _reply_note=note))
    # The precondition: the notes really were in what the model was sent.
    assert len(sent) == 1
    assert "<casa_notes>" in sent[0] and "(Casa) 📊 Finance answered" in sent[0]
    assert "Which quarter" in sent[0]
    operator = [e["text"] for e in events(mem)
                if e["speaker_kind"] == "operator"]
    assert operator == ["start from Q2 2026"]
    assert not any("(Casa)" in e["text"] or "Which quarter" in e["text"]
                   for e in events(mem))


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
    assert lines[0]["item"]["source_key"] == occurrence.message_key(
        "telegram", BOT, CHAT, "701")
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
    receipt = await capture.record(_item("healthy"))
    assert receipt.outcome == capture.STORED
    assert [e["text"] for e in events(mem)] == ["healthy"]   # receipt first
    await capture.settle()                                  # then the drain
    assert sorted(e["text"] for e in events(mem)) == [
        "healthy", "spooled 0", "spooled 1", "spooled 2"]
    assert not os.path.exists(capture.spool_path(mem.directory))


async def test_old_spool_sends_one_private_notice(mem):
    """One notice per old spool, sent detached; a send that fails is tried
    again at the next spooled append, and then no more."""
    notify = AsyncMock(side_effect=[RuntimeError("telegram not ready"), None])
    capture.configure(notify=notify)
    _down(mem)
    await capture.record(_item("first"))
    notify.assert_not_awaited()                     # a young spool says nothing
    path = Path(capture.spool_path(mem.directory))
    first = json.loads(path.read_text())
    first["spooled_at"] -= capture.SPOOL_NOTICE_AFTER_S + 60
    path.write_text(json.dumps(first) + "\n")
    for later in ("second", "third", "fourth"):
        await capture.record(_item(later))
        await capture.settle()
    assert notify.await_count == 2
    notify.assert_awaited_with(capture.SPOOL_NOTICE)


async def test_no_model_call_on_capture(mem, tmp_path, monkeypatch):
    """Capture — stored, spooled and drained — calls no model: the turn's
    own SDK client is the only one built, and no classifier runs."""
    import tier_classifier

    def _no_model(*_a, **_k):
        raise AssertionError("a model was called on the capture path")

    monkeypatch.setattr(tier_classifier, "classify_tier", _no_model)
    monkeypatch.setattr("claude_agent_sdk.query", _no_model)
    FakeClient.reset()
    agent = _make_agent(tmp_path)
    with patch("sdk_client_pool._default_make_client", FakeClient):
        await agent._process(_operator_msg("ping", "801"))
        _down(mem)
        await agent._process(_operator_msg("again", "802"))
        _up(mem)
    assert FakeClient.attempts == 1                  # the turns' own warm client
    await capture.record(_item("drains the spool"))
    await capture.settle()
    assert [e["text"] for e in events(mem)] == [
        "ping", "pong", "drains the spool", "again", "pong"]


def _snapshot_lines(store: MemoryStore) -> list[dict]:
    try:
        raw = Path(capture.snapshot_path(store.directory)).read_bytes()
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


class Killed(BaseException):
    """A process killed at a chosen point (never caught as an Exception)."""


async def test_spool_drain_rerun_adds_each_item_once(mem, monkeypatch):
    """A drain killed after its commits, before it removed its snapshot, and
    run again leaves exactly one event per spooled item."""
    _down(mem)
    for i in range(3):
        capture.append(mem, _item(f"spooled {i}"))
    _up(mem)
    snapshot = capture.snapshot_path(mem.directory)
    real_unlink = os.unlink

    def _unlink(path, *a, **k):
        if os.fspath(path) == snapshot:
            raise Killed
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(os, "unlink", _unlink)
    with pytest.raises(Killed):
        capture.drain_spool(mem)
    monkeypatch.setattr(os, "unlink", real_unlink)
    assert len(events(mem)) == 3 and len(_snapshot_lines(mem)) == 3
    assert capture.drain_spool(mem) == 0
    assert sorted(e["text"] for e in events(mem)) == [
        "spooled 0", "spooled 1", "spooled 2"]
    assert not os.path.exists(snapshot)
    assert not os.path.exists(capture.spool_path(mem.directory))


async def test_a_leftover_snapshot_is_drained_exactly_once(mem, monkeypatch):
    """A drain killed right after it renamed the spool leaves the snapshot;
    items spooled afterwards go to a fresh spool; the next drain stores the
    leftover snapshot first and then the new spool, each item exactly once,
    and leaves neither file."""
    _down(mem)
    for i in range(3):
        capture.append(mem, _item(f"before {i}"))
    real_rename = capture._rename

    def _rename_then_die(src, dst):
        real_rename(src, dst)
        raise Killed

    monkeypatch.setattr(capture, "_rename", _rename_then_die)
    with pytest.raises(Killed):
        capture.drain_spool(mem)
    monkeypatch.setattr(capture, "_rename", real_rename)
    assert [ln["item"]["text"] for ln in _snapshot_lines(mem)] == [
        "before 0", "before 1", "before 2"]
    assert spool_lines(mem) == []
    capture.append(mem, _item("after"))            # still down: a fresh spool
    assert [ln["item"]["text"] for ln in spool_lines(mem)] == ["after"]
    _up(mem)
    assert capture.drain_spool(mem) == 4
    assert sorted(e["text"] for e in events(mem)) == [
        "after", "before 0", "before 1", "before 2"]
    assert capture.drain_spool(mem) == 0
    assert not os.path.exists(capture.snapshot_path(mem.directory))
    assert not os.path.exists(capture.spool_path(mem.directory))


async def test_a_healthy_append_finishes_a_leftover_snapshot(mem, monkeypatch):
    """A snapshot left with no spool beside it is still drained: the next
    healthy capture starts the drain, and every line is stored once."""
    _down(mem)
    for i in range(2):
        capture.append(mem, _item(f"left {i}"))
    with pytest.raises(Exception):
        capture.drain_spool(mem)                  # cut off by the store
    _up(mem)
    assert len(_snapshot_lines(mem)) == 2 and spool_lines(mem) == []
    await capture.record(_item("healthy"))
    await capture.settle()
    assert sorted(e["text"] for e in events(mem)) == [
        "healthy", "left 0", "left 1"]
    assert not os.path.exists(capture.snapshot_path(mem.directory))


def _run(target, *args, name: str, errors: list):
    """A thread whose exception is recorded instead of escaping."""
    def _body():
        try:
            target(*args)
        except BaseException as exc:  # noqa: BLE001 — reported by the test
            errors.append((name, exc))
    thread = threading.Thread(target=_body, name=name)
    thread.start()
    return thread


async def test_the_drain_renames_only_under_the_spool_lock(mem, monkeypatch):
    """A spool write that has opened the spool and not yet written its line
    holds the spool lock, so a drain cannot take the spool from under it:
    the line lands in the file the drain reads, never in a renamed file
    after the drain has read it."""
    _down(mem)
    capture.append(mem, _item("already spooled"))
    _up(mem)
    opened, drain_read = threading.Event(), threading.Event()
    real_write_all, real_read = capture._write_all, capture._read

    def _write_all(fd, data):
        if threading.current_thread().name == "appender":
            opened.set()
            # The drain has read its snapshot (a drain that renamed without
            # the lock), or a bounded wait passes (one that waits for it).
            drain_read.wait(1.0)
        return real_write_all(fd, data)

    def _read(path):
        data = real_read(path)
        drain_read.set()
        return data

    monkeypatch.setattr(capture, "_write_all", _write_all)
    monkeypatch.setattr(capture, "_read", _read)
    late = _item("written between open and write")
    errors: list = []
    appender = _run(capture._spool, mem.directory, late, name="appender",
                    errors=errors)
    assert opened.wait(10)
    drain = _run(capture.drain_spool, mem, name="drain", errors=errors)
    appender.join(10)
    drain.join(10)
    assert errors == []
    capture.drain_spool(mem)
    assert sorted(e["text"] for e in events(mem)) == [
        "already spooled", "written between open and write"]
    assert not os.path.exists(capture.snapshot_path(mem.directory))
    assert not os.path.exists(capture.spool_path(mem.directory))


async def test_two_drains_never_overlap(mem, monkeypatch):
    """The background drain and a direct (export-style) drain run at once:
    the second waits for the first, so neither removes a snapshot the other
    has not stored, and every line is stored exactly once."""
    _down(mem)
    capture.append(mem, _item("first spool"))
    _up(mem)
    a_read, a_go = threading.Event(), threading.Event()
    b_second_read, b_go = threading.Event(), threading.Event()
    b_reads: list[str] = []
    real_read = capture._read

    def _read(path):
        name = threading.current_thread().name
        if name == "B":
            b_reads.append(path)
            if len(b_reads) == 2:              # B's own snapshot, not A's
                b_second_read.set()
                assert b_go.wait(10)
        data = real_read(path)
        if name == "A":
            a_read.set()
            assert a_go.wait(10)
        return data

    monkeypatch.setattr(capture, "_read", _read)
    errors: list = []
    drain_a = _run(capture.drain_spool, mem, name="A", errors=errors)
    assert a_read.wait(10)                     # A has taken and read the spool
    _down(mem)
    capture.append(mem, _item("second spool"))  # a fresh spool beside A's
    _up(mem)
    drain_b = _run(capture.drain_spool, mem, name="B", errors=errors)
    b_second_read.wait(1.0)                    # only if B overlaps A
    a_go.set()
    drain_a.join(10)
    b_go.set()
    drain_b.join(10)
    assert errors == []
    assert sorted(e["text"] for e in events(mem)) == ["first spool",
                                                      "second spool"]
    assert not os.path.exists(capture.snapshot_path(mem.directory))
    assert not os.path.exists(capture.spool_path(mem.directory))


def _observe_spool_fsyncs(monkeypatch) -> list[int]:
    """Link counts of every spool file at the moment it is fsynced."""
    links: list[int] = []
    real_fsync = capture._fsync

    def _fsync(fd):
        if not os.path.isdir(f"/proc/self/fd/{fd}"):
            links.append(os.fstat(fd).st_nlink)
        return real_fsync(fd)

    monkeypatch.setattr(capture, "_fsync", _fsync)
    return links


def _kept_once(store: MemoryStore, key: str) -> None:
    """*key* is stored or in a spool file, exactly once overall."""
    places = ([e["source_key"] for e in events(store)]
              + [ln["item"]["source_key"] for ln in spool_lines(store)]
              + [ln["item"]["source_key"] for ln in _snapshot_lines(store)])
    assert places.count(key) == 1, places


async def test_append_during_a_drain_is_kept(mem, monkeypatch):
    """A capture that must spool while a drain runs keeps its item and never
    waits for the drain.

    (a) The drain is storing its snapshot, holding the store's write lock: the
    capture times out on the store, spools to a fresh spool, fsyncs and has
    its receipt while the drain is still paused; the next drain stores it.
    (b) The capture reaches its spool write while the drain holds the spool
    lock to rename the spool: its line lands in the snapshot (written before
    the rename) or in the fresh spool (after it), never in a renamed file the
    drain has already read."""
    monkeypatch.setattr(capture, "CAPTURE_BUSY_TIMEOUT_S", 0.1)
    links = _observe_spool_fsyncs(monkeypatch)

    # (a) the drain is storing.
    _down(mem)
    capture.append(mem, _item("already spooled"))
    _up(mem)
    paused, release = threading.Event(), threading.Event()
    real_insert = capture._insert

    def _insert(conn, item):
        if threading.current_thread().name == "drain" and not paused.is_set():
            paused.set()
            assert release.wait(10)
        return real_insert(conn, item)

    monkeypatch.setattr(capture, "_insert", _insert)
    drain = threading.Thread(target=capture.drain_spool, args=(mem,),
                             name="drain")
    drain.start()
    assert paused.wait(10)
    links.clear()
    late = _item("arrived while the drain stored")
    result: list = []
    appender = threading.Thread(
        target=lambda: result.append(capture.append(mem, late)))
    appender.start()
    appender.join(5)
    still_paused = not release.is_set()
    release.set()
    drain.join(10)
    assert not appender.is_alive() and still_paused   # receipt before the drain
    assert result[0] is not None and result[0].outcome == capture.SPOOLED
    assert links == [1], links
    _kept_once(mem, late.source_key)
    assert [ln["item"]["source_key"] for ln in spool_lines(mem)] == [
        late.source_key]
    capture.drain_spool(mem)
    assert [e["text"] for e in events(mem)] == [
        "already spooled", "arrived while the drain stored"]
    monkeypatch.setattr(capture, "_insert", real_insert)

    # (b) rename versus append.
    _down(mem)
    capture.append(mem, _item("in the spool before the rename"))
    _up(mem)
    renaming, rename_go = threading.Event(), threading.Event()
    snapshot_read = threading.Event()
    real_rename, real_read = capture._rename, capture._read

    def _rename(src, dst):
        renaming.set()
        assert rename_go.wait(10)
        return real_rename(src, dst)

    def _read(path):
        data = real_read(path)
        snapshot_read.set()
        return data

    class _GatedLock:
        """The spool lock, except that the appender, once it holds it, waits
        until the drain has read its snapshot before writing."""

        def __init__(self, lock):
            self._lock = lock

        def __enter__(self):
            self._lock.acquire()
            if threading.current_thread().name == "appender":
                assert snapshot_read.wait(10)
            return self

        def __exit__(self, *exc):
            self._lock.release()

    monkeypatch.setattr(capture, "_rename", _rename)
    monkeypatch.setattr(capture, "_read", _read)
    monkeypatch.setattr(capture, "_spool_lock", _GatedLock(threading.Lock()))
    reached_spool = threading.Event()
    real_spool_write = capture._spool_write

    def _spool_write(*a, **k):
        reached_spool.set()
        return real_spool_write(*a, **k)

    monkeypatch.setattr(capture, "_spool_write", _spool_write)
    drain = threading.Thread(target=capture.drain_spool, args=(mem,),
                             name="drain")
    drain.start()
    assert renaming.wait(10)                   # the drain holds the spool lock
    links.clear()
    late = _item("arrived during the rename")
    result = []
    # The capture's spool fallback itself (its store attempt is (a)'s case).
    appender = threading.Thread(
        target=lambda: result.append(capture._spool(mem.directory, late)),
        name="appender")
    appender.start()
    assert reached_spool.wait(10)
    await asyncio.sleep(0.2)
    rename_go.set()
    drain.join(10)
    appender.join(10)
    assert not drain.is_alive() and not appender.is_alive()
    assert result[0] is not None and result[0].outcome == capture.SPOOLED
    assert links == [1], links
    _kept_once(mem, late.source_key)
    capture.drain_spool(mem)
    assert sorted(e["text"] for e in events(mem)) == [
        "already spooled", "arrived during the rename",
        "arrived while the drain stored", "in the spool before the rename"]


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
    rows = {e["text"]: e for e in await captured_events(mem)}
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
    seen_at_launch: list[list[str]] = []

    class _Client(_FakeSDKClient):
        def __init__(self, options):
            seen_at_launch.append(durable_texts(mem))
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
    rows = await captured_events(mem)
    assert [(e["speaker_kind"], e["speaker_id"], e["origin_kind"], e["text"])
            for e in rows] == [
        ("resident", "assistant", "operator", "Q1 cashflow?"),
        ("specialist", "finance", "delegated_result", "Q1 is on track")]
    assert rows[1]["session_id"] is not None


# --- the exit condition: each path, exact counts ----------------------------

class _ScriptedEngagementTurn:
    async def query(self, prompt):
        self.prompt = prompt

    async def receive_response(self):
        from test_specialist_limits import _mk_result_message
        yield _mk_assistant("first part")
        yield _mk_assistant("second part")
        yield _mk_result_message()


def _engagement_client():
    """A job session whose every written message runs one scripted turn: two
    specialist messages, then the turn's result."""
    from turn_scripted_session import TurnScriptedSession

    class _Session(TurnScriptedSession, _ScriptedEngagementTurn):
        pass

    return _Session()


async def _rows_when(store, count: int, timeout: float = 2.0) -> list[dict]:
    """The captured rows once *count* of them are durable (the reader
    captures on its own task), or what there is at the timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        rows = await captured_events(store)
        if len(rows) >= count or loop.time() > deadline:
            return rows
        await asyncio.sleep(0.01)


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
    assert [(e["speaker_kind"], e["eligibility"])
            for e in await captured_events(mem)] == [
        ("operator", "write_trusted_turn"), ("resident", "resident_message")]

    # A delegation: its request and its answer.
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
    assert [e["eligibility"] for e in await captured_events(mem)][2:] == [
        "delegated_request", "delegated_answer"]

    # An in-casa engagement turn of a memory-writing specialist: the
    # operator's topic message and each message the specialist wrote.
    drv = InCasaDriver()
    rec = EngagementRecord(
        id="e" * 32, kind="specialist", role_or_type="finance",
        driver="in_casa", status="active", started_at=0.0,
        last_user_turn_ts=0.0, completed_at=None,
        sdk_session_id=None, task="t",
        origin={"channel": "telegram", "_origin_route": "telegram",
                "_origin_clearance": "private"})
    from turn_scripted_session import attach_session
    attach_session(drv, rec, _engagement_client())
    try:
        drv.submit_operator(rec, "topic message",
                            tg_message=TgAddress(BOT, int(CHAT), 55))
        await _rows_when(mem, 7)
    finally:
        await drv.cancel(rec)
    rows = await captured_events(mem)
    assert len(rows) == 7
    assert [(e["speaker_kind"], e["text"]) for e in rows[4:]] == [
        ("operator", "topic message"), ("specialist", "first part"),
        ("specialist", "second part")]
    assert rows[4]["source_key"] == occurrence.engagement_key(
        "telegram", BOT, rec.id, "55")


# --- the turn is never held by capture --------------------------------------

#: What capture may add to one turn (its opening and its one answer) while
#: the store is locked, the spool is old and its notice never resolves, or a
#: large spool waits to be drained. The awaited part of a capture is only the
#: item's own durable write: one store attempt bounded by
#: ``CAPTURE_BUSY_TIMEOUT_S``, else one fsynced spool line.
CAPTURE_OVERHEAD_BOUND_S = 0.5

#: The warm baseline turn's duration on an unloaded machine (about 0.07 s
#: measured). The overhead bound holds at this speed; when the same run's
#: baseline shows turns running slower, a loaded machine stretches capture's
#: overhead with them (a drain thread's parsing competes for the GIL, which
#: an unloaded run already measures at about 0.18 s), and the bound is scaled
#: by that same-run slowdown. A turn that waited for the drain (several
#: seconds for this spool) is still far past it.
NOMINAL_TURN_S = 0.1

#: Spool lines for the large-spool case; drained synchronously, this many
#: took well over a second on real SQLite.
LARGE_SPOOL = 100_000




def _time_write_holds(store: MemoryStore, monkeypatch
                      ) -> tuple[list[tuple[float, float]],
                                 list[tuple[float, float]]]:
    """Times every write transaction on *store*, measured in this run: each
    one that held the write lock, from asking for it through ``COMMIT``, and
    each one that gave up waiting for it, from asking to giving up."""
    import contextlib
    import time as _time
    held: list[tuple[float, float]] = []
    gave_up: list[tuple[float, float]] = []
    real_transaction = store.transaction

    @contextlib.contextmanager
    def _timed(**kw):
        if not kw.get("write"):
            with real_transaction(**kw) as conn:
                yield conn
            return
        asked = _time.monotonic()
        locked = False
        try:
            with real_transaction(**kw) as conn:
                locked = True
                yield conn
        except sqlite3.OperationalError:
            if not locked:
                gave_up.append((asked, _time.monotonic()))
            raise
        finally:
            if locked:
                held.append((asked, _time.monotonic()))

    monkeypatch.setattr(store, "transaction", _timed)
    return held, gave_up


async def _timed_turn(agent, message_id: str, limit: float = 5.0) -> float:
    start = asyncio.get_running_loop().time()
    with patch("sdk_client_pool._default_make_client", FakeClient):
        await asyncio.wait_for(
            agent._process(_operator_msg(f"turn {message_id}", message_id)),
            limit)
    return asyncio.get_running_loop().time() - start


def _write_spool(store: MemoryStore, lines: int, *, age_s: float = 0.0) -> None:
    import dataclasses
    import time as _time
    template = _item("spooled long ago")
    os.makedirs(store.directory, exist_ok=True)
    with open(capture.spool_path(store.directory), "wb") as f:
        for i in range(lines):
            record = {"table": "events", "spooled_at": _time.time() - age_s,
                      "item": dataclasses.asdict(dataclasses.replace(
                          template, source_key=f"event:{i:032x}"))}
            f.write((json.dumps(record) + "\n").encode())


@pytest.mark.parametrize("case", ["store_locked", "old_spool_notice_hangs",
                                  "large_spool", "drain_running",
                                  "spool_behind_a_drain"])
async def test_capture_never_holds_a_turn(mem, tmp_path, monkeypatch, case):
    """A real ``Agent._process`` turn's capture overhead stays under
    ``CAPTURE_OVERHEAD_BOUND_S`` against a warm baseline turn of the same
    agent, whatever state the store and spool are in: a write lock another
    connection holds, an old spool whose notice never resolves, or a large
    spool waiting to be drained — the turn's own capture starting the drain,
    or a drain already storing batches when the turn arrives — and a turn that
    must spool (the store's write lock held) while a drain of a large spool
    is in flight. Each item still gets its receipt."""
    FakeClient.reset()
    agent = _make_agent(tmp_path)
    baseline = await _timed_turn(agent, "1")            # warm, healthy
    # The baseline turn's detached writes (its run row's confirmation) land
    # before the case is set up; on a loaded machine one still holding the
    # write lock made the case's own first capture spool.
    await transcript_runs.settle()
    await capture.settle()
    receipts: list = []
    real_record = capture.record

    async def _recording(item):
        receipt = await real_record(item)
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(capture, "record", _recording)
    held, gave_up = _time_write_holds(mem, monkeypatch)
    holder = None
    never = asyncio.Event()
    drain_go = threading.Event()
    if case == "spool_behind_a_drain":
        _write_spool(mem, LARGE_SPOOL)
        drain_paused = threading.Event()
        real_parse = capture._parse

        def _paused_parse(raw):
            drain_paused.set()
            assert drain_go.wait(30)
            return real_parse(raw)

        monkeypatch.setattr(capture, "_parse", _paused_parse)
        await real_record(_item("starts the drain"))
        assert await asyncio.to_thread(drain_paused.wait, 10)
        holder = sqlite3.connect(mem.path, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
    elif case == "store_locked":
        holder = sqlite3.connect(mem.path, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
    elif case == "old_spool_notice_hangs":
        async def _never_delivered(_text):
            await never.wait()
        capture.configure(notify=_never_delivered)
        _write_spool(mem, 1, age_s=capture.SPOOL_NOTICE_AFTER_S + 60)
        _down(mem)
    else:
        _write_spool(mem, LARGE_SPOOL)
        if case == "drain_running":
            inserted: list[int] = []
            real_insert = capture._insert

            def _counting(conn, item):
                inserted.append(1)
                return real_insert(conn, item)

            monkeypatch.setattr(capture, "_insert", _counting)
            await real_record(_item("starts the drain"))
            for _ in range(500):                   # the drain is storing
                if len(inserted) > 500:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("the drain never started storing")
    try:
        elapsed = await _timed_turn(agent, "2")
    finally:
        if holder is not None:
            holder.execute("ROLLBACK")
            holder.close()
        _up(mem)
        never.set()
        drain_go.set()
        await capture.settle()
    # Only a running drain competes with the turn for the GIL, a cost that
    # grows with the machine's load as the baseline does; every other case
    # keeps the fixed bound, so a capture that stalls the turn fails there.
    scale = (max(1.0, baseline / NOMINAL_TURN_S)
             if case in ("large_spool", "drain_running") else 1.0)
    bound = CAPTURE_OVERHEAD_BOUND_S * scale
    assert elapsed - baseline < bound, (case, elapsed, baseline, bound)
    assert len(receipts) == 2 and all(r is not None for r in receipts)
    outcomes = {r.outcome for r in receipts}
    if case in ("large_spool", "drain_running"):
        # A healthy store with a drain in flight stores each item: the drain
        # leaves the store free between its batches. Only a wait that ONE
        # write transaction covered from start to end — a batch that alone
        # held the lock past the capture bound, as a loaded machine's disk
        # makes it — spools an item by design; back-to-back batches never may.
        covered = bool(gave_up) and all(
            any(start <= asked and end >= given for start, end in held)
            for asked, given in gave_up)
        assert outcomes == {capture.STORED} or (
            covered and outcomes <= {capture.STORED, capture.SPOOLED}), (
            case, outcomes, gave_up,
            max((end - start for start, end in held), default=0.0))
    else:
        assert outcomes == {capture.SPOOLED}


async def test_one_background_drain_at_a_time(mem, monkeypatch):
    """Healthy appends while a spool waits start one detached drain, not one
    each, and none of them waits for it."""
    import threading as _threading
    _write_spool(mem, 3)
    release = _threading.Event()
    calls: list[int] = []
    real_drain = capture.drain_spool

    def _slow_drain(store):
        calls.append(1)
        assert release.wait(10)
        return real_drain(store)

    monkeypatch.setattr(capture, "drain_spool", _slow_drain)
    for i in range(3):
        receipt = await asyncio.wait_for(
            capture.record(_item(f"healthy {i}")), 2)
        assert receipt.outcome == capture.STORED
    await asyncio.sleep(0.05)
    assert calls == [1]
    release.set()
    await capture.settle()
    assert len(events(mem)) == 6


async def test_the_operators_own_words_can_reach_the_friends_ledger(
        mem, tmp_path):
    """The operator's message in his private DM has no floor: classified
    ``friends``, it is pending in the friends ledger the butler is served,
    while Ellen's reply in the same turn keeps the session's floor."""
    from memory import writer
    from memory_writer_support import (
        ScriptedPort, add_each, classifier_of, new_writer, pending_seqs,
        run_new)
    FakeClient.reset()
    agent = _make_agent(tmp_path)
    told_text = "the guest wifi is casa-guest"
    with patch("sdk_client_pool._default_make_client", FakeClient):
        await agent._process(_operator_msg(
            told_text, _operator_words=told_text))
    rows = {e["text"]: e for e in await captured_events(mem)}
    told, answer = rows["the guest wifi is casa-guest"], rows["pong"]
    assert (told["speaker_kind"], told["origin_tier"]) == ("operator", None)
    assert answer["origin_tier"] == "private"
    assert (await run_new(new_writer(mem, ScriptedPort(add_each),
                                     classifier=classifier_of("friends"))))[
        1] == writer.COMMITTED
    assert pending_seqs(mem, "friends") == [told["seq"]]


@pytest.mark.parametrize("words, extra", [
    (None, {"synthetic": "button"}),          # a tap: Casa's option text
    ("something else", {}),                    # not the captured text
])
async def test_an_opening_he_did_not_send_keeps_the_floor(
        mem, tmp_path, words, extra):
    FakeClient.reset()
    agent = _make_agent(tmp_path)
    ctx = dict(extra)
    if words is not None:
        ctx["_operator_words"] = words
    with patch("sdk_client_pool._default_make_client", FakeClient):
        await agent._process(_operator_msg("the code is 1234", **ctx))
    rows = {e["text"]: e for e in await captured_events(mem)}
    assert rows["the code is 1234"]["speaker_kind"] == "operator"
    assert rows["the code is 1234"]["origin_tier"] == "private"
