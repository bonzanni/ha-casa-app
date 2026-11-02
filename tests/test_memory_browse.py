"""``browse_memory_day``: a day, or up to seven, of saved conversation in
time order, paged at about 10 000 characters. The reader's clearance filters
every event before anything is counted or paged, so a count, a page number
or a page break never reflects an event the reader cannot read."""
from __future__ import annotations

import pytest

import tools
from memory import browse as memory_browse
from memory_tools_support import (  # noqa: F401 — fixtures
    canary, mem, payload, store_event, turn)

pytestmark = [pytest.mark.asyncio]


async def _browse(**args) -> dict:
    return payload(await tools.browse_memory_day.handler(args))


async def test_a_day_reads_in_time_order(mem, turn):
    store_event(mem, "second", day="2026-10-05", at="09:30")
    store_event(mem, "first", day="2026-10-05", at="08:00",
                speaker_name="Sam")
    store_event(mem, "next day", day="2026-10-06", at="07:00")
    store_event(mem, "noted", day="2026-10-05", at="09:31",
                speaker="resident", speaker_id="assistant")
    turn("private")
    out = await _browse(date="2026-10-05")
    assert out["text"] == ("## Monday 2026-10-05\n08:00 Sam: first\n"
                           "09:30 operator: second\n09:31 you: noted")
    assert out["events"] == "1-3 of 3" and out["pages"] == 1
    assert out["note"] == tools.MEMORY_READ_NOTE


async def test_the_tier_filter_runs_before_counts_and_pages(mem, turn):
    """A friends reader's counts and pages are those of its readable events
    alone; a page-filling private event changes neither."""
    secret = canary()
    store_event(mem, "shared one", day="2026-10-05", at="08:00",
                tier="friends")
    store_event(mem, secret * 1300, day="2026-10-05", at="08:30")
    store_event(mem, "shared two", day="2026-10-05", at="09:00",
                tier="public")
    turn("friends")
    out = await _browse(date="2026-10-05")
    assert out["events"] == "1-2 of 2" and out["pages"] == 1
    assert secret not in str(out)
    assert out["text"].splitlines()[1:] == ["08:00 operator: shared one",
                                            "09:00 operator: shared two"]


async def test_pages_hold_about_ten_thousand_characters(mem, turn):
    for n in range(12):
        store_event(mem, f"{n:02d} " + "x" * 2500, day="2026-10-05",
                    at=f"08:{n:02d}")
    turn("private")
    first = await _browse(date="2026-10-05")
    seen = []
    for page in range(1, first["pages"] + 1):
        out = await _browse(date="2026-10-05", page=page)
        assert len(out["text"]) <= memory_browse.PAGE_CHARS
        assert out["text"].startswith("## Monday 2026-10-05\n")
        seen += [line.split(" ")[2] for line in out["text"].splitlines()[1:]]
    assert first["pages"] > 1
    assert seen == [f"{n:02d}" for n in range(12)]


async def test_a_time_window_runs_from_the_first_day_to_the_last(mem, turn):
    store_event(mem, "too early", day="2026-10-05", at="21:00")
    store_event(mem, "late night", day="2026-10-05", at="23:10")
    store_event(mem, "small hours", day="2026-10-06", at="01:30")
    store_event(mem, "too late", day="2026-10-06", at="02:30")
    turn("private")
    out = await _browse(date="2026-10-05", to_date="2026-10-06",
                        from_time="22:00", to_time="02:00")
    assert [line.split(": ")[1] for line in out["text"].splitlines()
            if not line.startswith("## ")] == ["late night", "small hours"]
    assert out["days"] == {"from": "2026-10-05", "to": "2026-10-06"}


async def test_more_than_seven_days_is_refused_naming_search(mem, turn):
    turn("private")
    out = await _browse(date="2026-10-01", to_date="2026-10-08")
    assert out["status"] == "error" and "search_memory" in out["message"]
    within = await _browse(date="2026-10-01", to_date="2026-10-07")
    assert within["status"] == "ok"


@pytest.mark.parametrize("args", [
    {"date": "soon"}, {"date": "2026-10-05", "to_date": "2026-10-04"},
    {"date": "2026-10-05", "from_time": "8am"},
])
async def test_an_unreadable_request_reads_nothing(mem, turn, args):
    turn("private")
    out = await _browse(**args)
    assert out["status"] == "error" and "Nothing was read" in out["message"]


async def test_an_empty_day_is_bounded_not_absence(mem, turn):
    turn("private")
    out = await _browse(date="2026-10-05")
    assert out["status"] == "ok" and out["text"] == "" and out["pages"] == 0
    assert out["note"] == tools.MEMORY_READ_NOTE


async def test_an_unavailable_store_is_never_an_empty_day(mem, turn):
    turn("private")
    mem._unavailable_reason = "a simulated outage"
    try:
        out = await _browse(date="2026-10-05")
    finally:
        mem._unavailable_reason = None
    assert out["status"] == "unavailable" and "text" not in out
