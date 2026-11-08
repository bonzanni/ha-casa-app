"""``search_memory`` and ``search.db`` (INV-MEM-010, the search half of
INV-MEM-032's read side).

The index is a real ``search.db`` built from a real store; every event is
stored through capture's own append. A hit is shown only when the reader's
clearance reads its tier, read from the store at search time, before
paging; so are its window neighbours. Every result, empty or not, carries
one note that is the same at every clearance, and a store that cannot be
read is reported as such, never as an empty result.
"""
from __future__ import annotations

import os

import pytest

import tools
from memory import search as memory_search
from memory_tools_support import (  # noqa: F401 — fixtures
    canary, mem, new_store, payload, store_event, turn)

pytestmark = [pytest.mark.asyncio]

TIERS = ("public", "friends", "family", "private")


async def _search(**args) -> dict:
    return payload(await tools.search_memory.handler(args))


async def test_every_result_carries_one_note_at_every_clearance(mem, turn):
    """INV-MEM-010: a result with hits and an empty one both carry the note,
    word for word the same at every clearance."""
    store_event(mem, "the boiler service is booked", tier="public")
    notes = set()
    for clearance in TIERS:
        turn(clearance)
        found = await _search(query="boiler")
        empty = await _search(query="zeppelin")
        assert found["status"] == empty["status"] == "ok"
        assert [h["text"] for h in found["hits"]] == [
            "the boiler service is booked"]
        assert empty["hits"] == []
        notes |= {found["note"], empty["note"]}
    assert notes == {tools.MEMORY_READ_NOTE}
    assert "not proof" in tools.MEMORY_READ_NOTE


async def test_an_unreadable_store_is_never_an_empty_result(mem, turn,
                                                            monkeypatch):
    """INV-MEM-010: an unavailable store, and a search that fails, report
    that memory could not be checked; neither comes back as no hits."""
    store_event(mem, "the boiler service is booked")
    turn("private")
    mem._unavailable_reason = "a simulated outage"
    try:
        out = await _search(query="boiler")
    finally:
        mem._unavailable_reason = None
    assert out["status"] == "unavailable" and "hits" not in out
    assert "doesn't exist" in out["message"]

    def broken(*a, **k):
        raise OSError("disk gone")
    monkeypatch.setattr(memory_search, "search", broken)
    out = await _search(query="boiler")
    assert out["status"] == "unavailable" and "hits" not in out


async def test_a_page_with_more_behind_it_says_so(mem, turn):
    """INV-MEM-010: hits that did not fit a page are reported as existing."""
    for n in range(memory_search.PAGE_SIZE + 2):
        store_event(mem, f"boiler note {n}")
    turn("private")
    first = await _search(query="boiler")
    assert len(first["hits"]) == memory_search.PAGE_SIZE
    assert "page 2" in first["more"]
    second = await _search(query="boiler", page=2)
    assert len(second["hits"]) == 2 and "more" not in second


async def test_a_blank_query_searches_nothing(mem, turn, monkeypatch):
    calls = []
    monkeypatch.setattr(memory_search, "catch_up",
                        lambda *a: calls.append(a))
    turn("private")
    out = await _search(query="  ?! ")
    assert out["status"] == "error" and calls == []


async def test_hits_and_neighbours_are_filtered_per_event(mem, turn):
    """A friends reader sees the friends hit; its private neighbour, and a
    private hit with the same words, are not shown in any form."""
    secret = canary()
    day = "2026-10-04"
    store_event(mem, f"the lake code is {secret}", tier="private", day=day)
    store_event(mem, "dinner at the lake on sunday", tier="friends", day=day)
    store_event(mem, f"lake house alarm {secret}", tier="private", day=day)
    store_event(mem, "bring the lake towels", tier="public", day=day)
    turn("friends")
    out = await _search(query="lake")
    assert sorted(h["text"] for h in out["hits"]) == [
        "bring the lake towels", "dinner at the lake on sunday"]
    assert secret not in str(out)
    dinner = next(h for h in out["hits"] if "dinner" in h["text"])
    assert dinner.get("after") == ["12:00 operator: bring the lake towels"]
    assert "before" not in dinner


async def test_paging_counts_only_readable_hits(mem, turn):
    """The tier filter runs before paging: private matches never take a
    place on a lower reader's page or make it report more."""
    for n in range(memory_search.PAGE_SIZE + 3):
        store_event(mem, f"garden plan private {n}", tier="private")
    for n in range(3):
        store_event(mem, f"garden plan shared {n}", tier="friends")
    turn("friends")
    out = await _search(query="garden plan")
    assert sorted(h["text"] for h in out["hits"]) == [
        f"garden plan shared {n}" for n in range(3)]
    assert "more" not in out


async def test_a_tier_raised_after_indexing_hides_the_hit(mem, turn):
    """The index holds no tier: an event classified up after it was indexed
    is filtered by its tier at search time."""
    seq = store_event(mem, "the spare key is under the pot", tier="friends")
    assert memory_search.ensure(mem) is True
    with mem.transaction(write=True) as conn:
        conn.execute("UPDATE events SET tier = 'private' WHERE seq = ?",
                     (seq,))
    turn("friends")
    assert (await _search(query="spare key"))["hits"] == []
    turn("private")
    assert len((await _search(query="spare key"))["hits"]) == 1


async def test_a_hit_shows_the_stores_text_never_the_index(tmp_path, turn,
                                                           monkeypatch):
    """An index left behind by a store it no longer matches can rank, but a
    hit's text is read from the store: words the store no longer holds are
    never shown."""
    from memory import capture
    first = new_store(tmp_path / "a")
    secret = canary()
    store_event(first, f"old fact {secret}")
    memory_search.ensure(first)
    second = new_store(tmp_path / "b")
    os.replace(memory_search.path_for(first), memory_search.path_for(second))
    store_event(second, "new fact about the porch")
    monkeypatch.setitem(capture._wiring, "store", second)
    turn("private")
    out = await _search(query=f"fact {secret}")
    assert secret not in str(out)


async def test_a_missing_index_is_built_from_the_store(mem, turn):
    """The boot check builds a missing index; search builds one too."""
    store_event(mem, "the boiler service is booked")
    path = memory_search.path_for(mem)
    assert not os.path.exists(path)
    assert memory_search.ensure(mem) is True
    assert memory_search.ensure(mem) is False
    os.unlink(path)
    turn("private")
    assert len((await _search(query="boiler"))["hits"]) == 1
    assert os.path.exists(path)


async def test_an_unreadable_index_is_rebuilt_at_boot(mem, turn):
    store_event(mem, "the boiler service is booked")
    with open(memory_search.path_for(mem), "wb") as f:
        f.write(os.urandom(4096))
    assert memory_search.ensure(mem) is True
    turn("private")
    assert len((await _search(query="boiler"))["hits"]) == 1


async def test_new_events_are_searchable_at_once(mem, turn):
    """The index catches up before every search."""
    memory_search.ensure(mem)
    turn("private")
    assert (await _search(query="porch"))["hits"] == []
    store_event(mem, "paint the porch blue")
    assert len((await _search(query="porch"))["hits"]) == 1


async def test_a_hit_carries_its_date_time_speaker_and_mark(mem, turn):
    store_event(mem, "water the plants", day="2026-10-05", at="08:15",
                speaker="operator", speaker_name="Sam", scheduled=True)
    store_event(mem, "done, plants watered", day="2026-10-05", at="08:16",
                speaker="resident", speaker_id="assistant")
    turn("private")
    hits = (await _search(query="plants"))["hits"]
    by_text = {h["text"]: h for h in hits}
    first = by_text["water the plants"]
    assert (first["date"], first["weekday"], first["time"], first["speaker"],
            first["scheduled"]) == ("2026-10-05", "Monday", "08:15", "Sam",
                                    True)
    assert by_text["done, plants watered"]["speaker"] == "you"
    assert "scheduled" not in by_text["done, plants watered"]


async def test_a_restricted_webhook_turn_reads_public_only(mem, turn):
    store_event(mem, "the garage opens at seven", tier="public")
    store_event(mem, "the garage code is private", tier="private")
    turn("public", _origin_route="webhook_trigger", channel="webhook")
    out = await _search(query="garage")
    assert [h["text"] for h in out["hits"]] == ["the garage opens at seven"]


async def test_query_engager_unknown_carries_guidance(mem, monkeypatch):
    """INV-MEM-010: the engager query's synthesizer gets the engager's view
    and a search; with nothing readable it is UNKNOWN, worded as not proof
    of absence."""
    from engagement_registry import EngagementRecord
    rec = EngagementRecord(
        id="e" * 32, kind="specialist", role_or_type="finance",
        driver="in_casa", status="active", topic_id=1, started_at=0.0,
        last_user_turn_ts=0.0, completed_at=None,
        sdk_session_id=None, task="a task", origin={"channel": "telegram",
                                     "_origin_route": "telegram",
                                     "_origin_clearance": "friends"})
    store_event(mem, "the invoice total is private", tier="private")
    token = tools.engagement_var.set(rec)
    monkeypatch.setattr(tools, "_bus", None)
    try:
        out = payload(await tools.query_engager.handler(
            {"question": "what is the invoice total?", "max_tokens": 100}))
    finally:
        tools.engagement_var.reset(token)
    assert out["status"] == "unknown"
    assert "NOT proof of absence" in out["message"]


@pytest.mark.parametrize("stamped,seen", [
    ("public", ["the salary review is on friday"]),
    ("private", ["the salary is private", "the salary review is on friday"]),
])
async def test_an_engagement_reads_at_its_own_records_clearance(
        mem, stamped, seen):
    """INV-MEM-008: an engagement's tool calls arrive with no ambient origin,
    so the clearance comes from the engagement's own record. Red case:
    dropping the record fallback in ``_origin_clearance_markers`` yields the
    telegram channel default (private) for the non-operator's engagement."""
    from types import SimpleNamespace
    store_event(mem, "the salary is private", tier="private")
    store_event(mem, "the salary review is on friday", tier="public")
    token = tools.engagement_var.set(SimpleNamespace(
        id="e1", context_rebuild_pending=False, kind="executor",
        origin={"channel": "telegram", "_origin_route": "telegram",
                "_origin_clearance": stamped}))
    try:
        out = await _search(query="salary")
    finally:
        tools.engagement_var.reset(token)
    assert sorted(h["text"] for h in out["hits"]) == sorted(seen)


async def test_two_first_catch_ups_build_one_index(tmp_path):
    """Catch-ups that each find no index build it once between them: the
    one that waits sees the other's tables and does not create them again,
    and none mistakes a creation it sees half committed for a foreign file.
    (Run with the boot bound so they wait for each other; on a turn's path
    a catch-up that would wait answers unavailable instead.)"""
    import asyncio
    from memory.store import BUSY_TIMEOUT_S
    for round_no in range(3):
        store = new_store(tmp_path / f"r{round_no}")
        store_event(store, "the boiler service is booked")
        added = await asyncio.gather(*(asyncio.to_thread(
            memory_search.catch_up, store, busy_timeout=BUSY_TIMEOUT_S)
            for _ in range(8)))
        assert sorted(added) == [0] * 7 + [1]
    found = memory_search.search(store, "boiler", clearance="private")
    assert [h["text"] for h in found.hits] == ["the boiler service is booked"]


async def test_an_index_seen_half_created_is_judged_again_under_the_lock(
        mem, monkeypatch):
    """Two reads outside a transaction can straddle another caller's
    creating commit (no layout version yet, then its tables): the open
    must not take that for a foreign file, but judge the file again under
    the write lock, where it is one consistent state."""
    store_event(mem, "the boiler service is booked")
    assert memory_search.ensure(mem) is True
    real = memory_search._current
    seen = []

    def half_created_once(conn):
        if not seen:
            seen.append(1)
            return False
        return real(conn)
    monkeypatch.setattr(memory_search, "_current", half_created_once)
    assert memory_search.catch_up(mem) == 0
    found = memory_search.search(mem, "boiler", clearance="private")
    assert len(found.hits) == 1
