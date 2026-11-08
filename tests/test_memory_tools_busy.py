"""No memory tool holds a turn on a locked store.

Every connection a memory tool or the engager query opens — store reads,
the index's catch-up, index reads, the correction mark — waits for a lock no
longer than the turn-path bound (``memory.view.BUSY_TIMEOUT_S``). This pin
holds the locks other connections can hold, in each journal mode the store
can be in, and calls every memory tool and the engager query: each answers
within a small bound, and a read it could not make is ``unavailable``, never
an empty result. A connection added later without the bound fails here.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import ExitStack

import pytest

import agent as agent_mod
import tools
from memory import capture, occurrence
from memory import search as memory_search
from memory import view as memory_view
from memory_serving_support import add, fact
from memory_tools_support import new_store, store_event

pytestmark = [pytest.mark.asyncio]

#: Each call must answer well inside this (the bound is 50 ms; this leaves
#: room for a loaded test host, and is far below any default wait).
ANSWER_WITHIN_S = 1.5

KEY = occurrence.provider_key("telegram", "4242", "900")

#: (name, which store file and lock, the store's journal mode, whether the
#: lock is taken only once the search's catch-up is done — so the search's
#: own reads, after the catch-up, are the ones that meet it)
LOCKS = [
    ("index write lock", "search.db", "IMMEDIATE", "wal", False),
    ("index exclusive lock", "search.db", "EXCLUSIVE", "wal", False),
    ("store write lock, WAL", "memory.db", "IMMEDIATE", "wal", False),
    ("store exclusive lock, rollback journal", "memory.db", "EXCLUSIVE",
     "delete", False),
    ("index exclusive lock after the catch-up", "search.db", "EXCLUSIVE",
     "wal", True),
    ("store exclusive lock after the catch-up, rollback journal",
     "memory.db", "EXCLUSIVE", "delete", True),
]


async def _engager(question: str) -> dict:
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=_engager.tmp + "/e.json")
    rec = await reg.create(
        kind="specialist", role_or_type="finance", driver="in_casa",
        task="t", topic_id=1,
        origin={"role": "assistant", "channel": "telegram", "chat_id": "1",
                "_origin_route": "telegram", "_origin_clearance": "private"})
    token = tools.engagement_var.set(rec)
    try:
        return json.loads((await tools.query_engager.handler(
            {"question": question, "max_tokens": 100}))["content"][0]["text"])
    finally:
        tools.engagement_var.reset(token)


def _calls(entry: str):
    async def call(name, args):
        res = await getattr(tools, name).handler(args)
        return json.loads(res["content"][0]["text"])
    return [
        ("search_memory", lambda: call("search_memory", {"query": "boiler"})),
        ("browse_memory_day",
         lambda: call("browse_memory_day", {"date": "2026-10-05"})),
        ("open_memory", lambda: call("open_memory", {"ids": [entry]})),
        ("correct_memory",
         lambda: call("correct_memory", {"what": "the boiler"})),
        ("query_engager", lambda: _engager("when is the boiler serviced?")),
    ]


@pytest.mark.parametrize("lock", LOCKS, ids=[lock[0] for lock in LOCKS])
async def test_every_memory_tool_answers_at_once_on_a_locked_store(
        tmp_path, monkeypatch, lock):
    _, filename, mode, journal, after_catch_up = lock
    store = new_store(tmp_path)
    monkeypatch.setitem(capture._wiring, "store", store)
    monkeypatch.setitem(capture._wiring, "notify", None)
    store_event(store, "the boiler service is booked", day="2026-10-05",
                source_key=KEY)
    _, (entry,) = add(store, [fact("boiler", "serviced in May")])
    memory_search.ensure(store)
    if journal == "delete":
        conn = sqlite3.connect(store.path, isolation_level=None)
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] \
            == "delete"
        conn.close()

    async def synth(question, context, max_tokens):
        return "UNKNOWN"
    monkeypatch.setattr(tools, "_synthesize_answer", synth)
    monkeypatch.setattr(tools, "_bus", None)
    _engager.tmp = str(tmp_path)
    agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram", "chat_id": "4242",
        "_origin_route": "telegram", "_origin_clearance": "private",
        agent_mod.MEMORY_OPENING_KEY: KEY})

    path = (store.path if filename == "memory.db"
            else memory_search.path_for(store))
    timings = {}
    with ExitStack() as stack:
        holder = sqlite3.connect(path, isolation_level=None,
                                 check_same_thread=False)
        stack.callback(holder.close)
        stack.callback(lambda: holder.in_transaction
                       and holder.execute("ROLLBACK"))
        if after_catch_up:
            real = memory_search.catch_up

            def catch_up_then_lock(*a, **k):
                added = real(*a, **k)
                if not holder.in_transaction:
                    holder.execute(f"BEGIN {mode}")
                return added
            monkeypatch.setattr(memory_search, "catch_up", catch_up_then_lock)
        else:
            holder.execute(f"BEGIN {mode}")
        for name, call in _calls(entry):
            started = time.monotonic()
            out = await call()
            timings[name] = round(time.monotonic() - started, 3)
            assert out["status"] in ("ok", "unknown", "unavailable",
                                     "queued"), (name, out)
            if out["status"] == "ok" and name in ("search_memory",
                                                  "browse_memory_day"):
                assert out["note"] == tools.MEMORY_READ_NOTE
        assert holder.in_transaction, "the lock was never held"
    assert all(t < ANSWER_WITHIN_S for t in timings.values()), timings
    assert memory_view.BUSY_TIMEOUT_S <= 0.05


def _spooled_opening(store) -> None:
    """His message, captured while the store was down: it waits in the
    spool under the turn's opening key."""
    item = capture.event(
        role="assistant", channel="telegram", speaker_kind="operator",
        origin_kind="operator", text="no, the boiler is serviced in June",
        eligibility="write_trusted_turn", origin_tier="private",
        source_key=KEY)
    store._unavailable_reason = "busy"
    try:
        assert capture.append(store, item).outcome == capture.SPOOLED
    finally:
        store._unavailable_reason = None


def _marks(store) -> int:
    with store.transaction() as conn:
        return conn.execute(
            "SELECT count(*) FROM correction_marks").fetchone()[0]


async def _setup_spooled(tmp_path, monkeypatch):
    store = new_store(tmp_path)
    monkeypatch.setitem(capture._wiring, "store", store)
    monkeypatch.setitem(capture._wiring, "notify", None)
    monkeypatch.setattr(capture, "_background", set())
    monkeypatch.setattr(capture, "_drain", [None])
    store_event(store, "the boiler service is booked", day="2026-10-05")
    _, (entry,) = add(store, [fact("boiler", "serviced in May")])
    memory_search.ensure(store)
    _spooled_opening(store)

    async def synth(question, context, max_tokens):
        return "UNKNOWN"
    monkeypatch.setattr(tools, "_synthesize_answer", synth)
    monkeypatch.setattr(tools, "_bus", None)
    _engager.tmp = str(tmp_path)
    agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram", "chat_id": "4242",
        "_origin_route": "telegram", "_origin_clearance": "private",
        agent_mod.MEMORY_OPENING_KEY: KEY})
    return store, entry


async def test_a_spooled_opening_is_never_drained_inline(tmp_path,
                                                         monkeypatch):
    """His message waits in the spool: correct_memory does not drain it on
    the turn's path. It answers ``unavailable`` at once, marks nothing, and
    asks for the background drain; once that drain has stored his message,
    the next call marks it."""
    store, _ = await _setup_spooled(tmp_path, monkeypatch)
    inline = []
    for name in ("drain_spool", "drain_waiting"):
        real = getattr(capture, name)
        monkeypatch.setattr(capture, name, (
            lambda real: lambda *a, **k: (inline.append(1), real(*a, **k))[1]
        )(real))
    started = time.monotonic()
    out = json.loads((await tools.correct_memory.handler(
        {"what": "the boiler"}))["content"][0]["text"])
    assert time.monotonic() - started < ANSWER_WITHIN_S
    assert out["status"] == "unavailable" and _marks(store) == 0
    drain = capture._drain[0]
    assert drain is not None, "no background drain was requested"
    await capture.settle()
    assert inline == [1]          # the background drain, never the tool
    again = json.loads((await tools.correct_memory.handler(
        {"what": "the boiler"}))["content"][0]["text"])
    assert again["status"] == "queued" and _marks(store) == 1


async def test_every_memory_tool_answers_while_a_spool_append_is_paused(
        tmp_path, monkeypatch):
    """Another capture is part-way through writing to the spool (it holds
    the spool lock) while his message waits there: no memory tool waits for
    it, and correct_memory answers ``unavailable`` with nothing marked."""
    import threading
    store, entry = await _setup_spooled(tmp_path, monkeypatch)
    inside, release = threading.Event(), threading.Event()
    real_write = capture._write_all

    def paused_write(fd, data):
        inside.set()
        assert release.wait(30)
        real_write(fd, data)
    monkeypatch.setattr(capture, "_write_all", paused_write)
    writer = threading.Thread(target=capture._spool, args=(
        store.directory, capture.event(
            role="assistant", channel="telegram", speaker_kind="resident",
            origin_kind="operator", text="a reply being spooled",
            eligibility="resident_message", origin_tier="private")))
    writer.start()
    assert inside.wait(5)
    timings, outs = {}, {}
    try:
        for name, call in _calls(entry):
            started = time.monotonic()
            outs[name] = await call()
            timings[name] = round(time.monotonic() - started, 3)
    finally:
        release.set()
        writer.join(10)
        await capture.settle()
    assert all(t < ANSWER_WITHIN_S for t in timings.values()), timings
    assert outs["correct_memory"]["status"] == "unavailable"
    assert _marks(store) == 0
