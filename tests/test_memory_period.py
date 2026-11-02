"""INV-MEM-019: ``search_memory``'s optional ``from`` and ``to``.

They name local days in the operator's timezone — a day, or a named period
(its first day as ``from``, its last as ``to``). A search given them keeps
only events whose frozen local day lies inside, inclusive; the range and the
reader's clearance filter every event before anything is paged; an empty
in-range result carries the same bounded note as any other; a bound that
cannot be read, or a ``to`` before ``from``, is refused before the index or
the store is touched; and a search without them reads every day.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import tools
from memory import search as memory_search
from memory_tools_support import (  # noqa: F401 — fixtures
    mem, payload, store_event, turn)
from timekeeping import resolve_tz

pytestmark = [pytest.mark.asyncio]


async def _search(**args) -> dict:
    return payload(await tools.search_memory.handler(args))


async def test_a_range_keeps_only_events_said_inside_it(mem, turn):
    for day in ("2026-09-30", "2026-10-01", "2026-10-03", "2026-10-04"):
        store_event(mem, f"pool cleaning on {day}", day=day)
    turn("private")
    out = await _search(query="pool cleaning", **{"from": "2026-10-01",
                                                  "to": "2026-10-03"})
    assert sorted(h["date"] for h in out["hits"]) == ["2026-10-01",
                                                      "2026-10-03"]
    assert out["days"] == {"from": "2026-10-01", "to": "2026-10-03"}
    only_from = await _search(query="pool cleaning", **{"from": "2026-10-03"})
    assert sorted(h["date"] for h in only_from["hits"]) == [
        "2026-10-03", "2026-10-04"]
    only_to = await _search(query="pool cleaning", to="2026-09-30")
    assert [h["date"] for h in only_to["hits"]] == ["2026-09-30"]


async def test_the_range_runs_before_paging(mem, turn):
    """Out-of-range matches never take a place on the page, whatever their
    rank, and never make it report more."""
    for n in range(memory_search.PAGE_SIZE + 3):
        store_event(mem, f"roof roof roof repair {n}", day="2026-08-01")
    for n in range(2):
        store_event(mem, f"roof repair {n}", day="2026-10-10")
    turn("private")
    out = await _search(query="roof repair", **{"from": "2026-10-10",
                                                "to": "2026-10-10"})
    assert sorted(h["text"] for h in out["hits"]) == [
        "roof repair 0", "roof repair 1"]
    assert "more" not in out


async def test_the_range_and_the_clearance_both_filter(mem, turn):
    store_event(mem, "porch light private", day="2026-10-10", tier="private")
    store_event(mem, "porch light shared", day="2026-10-10", tier="friends")
    store_event(mem, "porch light earlier", day="2026-10-01", tier="friends")
    turn("friends")
    out = await _search(query="porch light", **{"from": "2026-10-10"})
    assert [h["text"] for h in out["hits"]] == ["porch light shared"]


async def test_an_empty_range_result_is_bounded_not_absence(mem, turn):
    store_event(mem, "porch light", day="2026-10-10")
    turn("private")
    out = await _search(query="porch light", **{"from": "2026-10-11"})
    assert out["status"] == "ok" and out["hits"] == []
    assert out["note"] == tools.MEMORY_READ_NOTE


@pytest.mark.parametrize("args", [
    {"from": "the other day"},
    {"to": "2026-13-01"},
    {"from": "2026-10-05", "to": "2026-10-04"},
    {"from": "this_month", "to": "last_month"},
])
async def test_an_unreadable_or_inverted_range_is_refused_unsearched(
        mem, turn, monkeypatch, args):
    touched = []
    monkeypatch.setattr(memory_search, "catch_up",
                        lambda *a, **k: touched.append("index"))
    monkeypatch.setattr(memory_search, "search",
                        lambda *a, **k: touched.append("search"))
    turn("private")
    out = await _search(query="porch", **args)
    assert out["status"] == "error" and out["kind"] == "invalid_days"
    assert "Nothing was searched" in out["message"]
    assert touched == []


async def test_named_periods_resolve_in_the_operator_zone(mem, turn,
                                                         monkeypatch):
    """``yesterday`` is the operator's yesterday; ``last_week`` as ``from``
    is its Monday and as ``to`` its Sunday."""
    seen = []
    real = memory_search.search
    monkeypatch.setattr(memory_search, "search", lambda *a, **k: (
        seen.append((k["first_day"], k["last_day"])), real(*a, **k))[1])
    turn("private")
    today = datetime.now(resolve_tz()).date()
    await _search(query="porch", **{"from": "yesterday", "to": "yesterday"})
    await _search(query="porch", **{"from": "last_week", "to": "last_week"})
    monday = today - timedelta(days=today.weekday() + 7)
    assert seen == [(today - timedelta(days=1), today - timedelta(days=1)),
                    (monday, monday + timedelta(days=6))]


async def test_a_search_without_a_range_reads_every_day(mem, turn,
                                                       monkeypatch):
    seen = []
    real = memory_search.search
    monkeypatch.setattr(memory_search, "search", lambda *a, **k: (
        seen.append((k["first_day"], k["last_day"])), real(*a, **k))[1])
    for day in ("2025-01-01", "2026-10-10"):
        store_event(mem, "porch light", day=day)
    turn("private")
    out = await _search(query="porch light", to=None)
    assert seen == [(None, None)] and "days" not in out
    assert sorted(h["date"] for h in out["hits"]) == ["2025-01-01",
                                                      "2026-10-10"]
