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
    reg = EngagementRegistry(tombstone_path=_engager.tmp + "/e.json", bus=None)
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
