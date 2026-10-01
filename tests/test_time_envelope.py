# tests/test_time_envelope.py
"""#471: the per-turn <current_time> envelope must never reach the
content-addressed document id OR the stored memory text.

The envelope rides on the sent query text (M27, agent.py::_process), the SDK
transcript echoes it back, and the retain path hashes what it reads
(session_saver → build_retain_items → content_document_id). With the envelope
inside the hash input, an identical utterance minted a NEW document whenever
the second-precision timestamp differed — i.e. across any two sessions — and
the cross-session dedup that content addressing exists for (F1, 2026-07-09)
never engaged. These tests pin the fix: strip at the transcript-readback
boundary, so hash and stored content are BOTH envelope-free (fixing only the
hash would trip build_retain_items' same-id-different-text hard error and fail
whole saves)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import session_saver
from hindsight_ids import content_document_id
from session_saver import transcript_to_items
from session_reg_helpers import STUB_SPEAKER_PROV, STUB_USER_PROV
from timekeeping import (
    RecallWindow, compose_time_envelope, resolve_period, split_time_envelope,
    strip_time_envelope,
)

pytestmark = pytest.mark.unit


class _NeverSavedMemory:
    async def document_tags(self, bank, document_id):
        return None  # #1123: an empty bank — every document reads never saved


_NEVER_SAVED = _NeverSavedMemory()

_TZ = ZoneInfo("Europe/Amsterdam")
_T1 = datetime(2026, 8, 9, 9, 15, 3, tzinfo=_TZ)
_T2 = datetime(2026, 8, 10, 21, 40, 59, tzinfo=_TZ)


class _Msg:
    def __init__(self, type_, message):
        self.type = type_
        self.message = message


async def _items(msgs, monkeypatch):
    async def fake_classify(content: str) -> str:
        return "public"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    return await transcript_to_items(
        msgs, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV, semantic_memory=_NEVER_SAVED,
    )


class TestDedupAcrossTimestamps:
    """The #471 red case: two identical utterances, different timestamps →
    ONE document."""

    async def test_same_utterance_two_timestamps_is_one_document(self, monkeypatch):
        raw = "I like the bedroom at 19 degrees."
        msgs = [
            _Msg("user", {"role": "user", "content": compose_time_envelope(_T1) + raw}),
            _Msg("user", {"role": "user", "content": compose_time_envelope(_T2) + raw}),
        ]
        items = await _items(msgs, monkeypatch)
        assert len(items) == 1
        # Stored content is the raw utterance — no envelope, no stale timestamp.
        assert items[0]["content"] == raw
        # The id equals the RAW utterance's content address, so a retain from
        # any later session (any timestamp) upserts to this same document.
        assert items[0]["document_id"] == content_document_id("tester", raw)

    async def test_cross_session_id_is_timestamp_independent(self, monkeypatch):
        """Two separate retain batches (two sessions) yield the SAME id —
        asserted via the id, which is what Hindsight upserts on."""
        raw = "The bins go out on tuesday evening."
        first = await _items(
            [_Msg("user", {"role": "user", "content": compose_time_envelope(_T1) + raw})],
            monkeypatch,
        )
        second = await _items(
            [_Msg("user", {"role": "user", "content": compose_time_envelope(_T2) + raw})],
            monkeypatch,
        )
        assert first[0]["document_id"] == second[0]["document_id"]

    async def test_envelope_only_turn_is_dropped_not_retained(self, monkeypatch):
        # A message that is nothing but the envelope carries no utterance.
        msgs = [_Msg("user", {"role": "user", "content": compose_time_envelope(_T1)})]
        assert await _items(msgs, monkeypatch) == []

    async def test_timestamp_survives_out_of_band(self, monkeypatch):
        """Stripping the envelope must not lose the turn's only time signal:
        it moves to the retain item's documented ``timestamp`` field."""
        raw = "The dentist appointment is tomorrow."
        msgs = [_Msg("user", {"role": "user", "content": compose_time_envelope(_T1) + raw})]
        items = await _items(msgs, monkeypatch)
        assert items[0]["timestamp"] == "2026-08-09T09:15:03+02:00"
        assert "timestamp" not in items[0]["metadata"]

    async def test_assistant_turn_is_never_stripped(self, monkeypatch):
        """Only the USER turn carries the transport envelope; an assistant
        reply that happens to start with an envelope-shaped block is content
        and must be retained verbatim, with no timestamp claimed from it."""
        echoed = compose_time_envelope(_T1) + "here is the block you asked about"
        msgs = [_Msg("assistant", {"role": "assistant", "content": echoed})]
        items = await _items(msgs, monkeypatch)
        assert items[0]["content"] == echoed.strip()
        assert "timestamp" not in items[0]


class TestComposeStripPair:
    """compose_time_envelope / strip_time_envelope are a pinned pair: if the
    composed shape ever drifts without the stripper following, these fail."""

    def test_round_trip_is_identity_on_the_raw_text(self):
        for raw in ("hello", "multi\nline\ntext", "  leading space", "<tag>x</tag>"):
            assert strip_time_envelope(compose_time_envelope(_T1) + raw) == raw

    def test_strip_without_envelope_is_identity(self):
        for raw in ("plain text", "", "<current_time> mentioned inline",
                    "text that mentions </current_time> later"):
            assert strip_time_envelope(raw) == raw

    def test_strip_is_idempotent(self):
        text = compose_time_envelope(_T2) + "fact"
        assert strip_time_envelope(strip_time_envelope(text)) == "fact"

    def test_strips_only_a_single_leading_envelope(self):
        # A second block INSIDE the user text is user content, not the turn
        # envelope — it must survive.
        inner = compose_time_envelope(_T1) + "quoted"
        assert strip_time_envelope(compose_time_envelope(_T2) + inner) == inner

    def test_composed_shape_matches_the_documented_format(self):
        out = compose_time_envelope(_T1)
        assert out == (
            "<current_time>\n"
            "2026-08-09T09:15:03+02:00 (sunday am, week 32)\n"
            "</current_time>\n\n"
        )

    def test_split_returns_the_iso_timestamp_and_the_rest(self):
        ts, rest = split_time_envelope(compose_time_envelope(_T1) + "fact")
        assert ts == "2026-08-09T09:15:03+02:00"
        assert rest == "fact"

    def test_split_without_envelope_returns_none(self):
        assert split_time_envelope("plain") == (None, "plain")


# ---------------------------------------------------------------------------
# #1120: resolve_period — a memory search's period, in the operator's zone
# ---------------------------------------------------------------------------

ROME_TZ = ZoneInfo("Europe/Rome")


def _days(window):
    """The window as (first local day, last local day, exact-day check)."""
    tz = window.start.tzinfo
    first, last = window.start.date(), window.end.date()
    exact = (
        window.start.timetz().replace(tzinfo=None).isoformat() == "00:00:00"
        and (window.end + timedelta(microseconds=1)).astimezone(tz).time().isoformat() == "00:00:00"
    )
    return first.isoformat(), last.isoformat(), exact


@pytest.mark.parametrize("name, now, first, last", [
    ("today", "2026-10-01T09:00", "2026-10-01", "2026-10-01"),
    ("yesterday", "2026-03-01T09:00", "2026-02-28", "2026-02-28"),
    ("this_week", "2026-10-01T09:00", "2026-09-28", "2026-10-04"),   # Thursday
    ("this_week", "2026-09-28T00:00", "2026-09-28", "2026-10-04"),   # Monday 00:00
    ("this_week", "2026-10-04T23:59", "2026-09-28", "2026-10-04"),   # Sunday 23:59
    ("last_week", "2026-01-01T09:00", "2025-12-22", "2025-12-28"),
    ("this_month", "2026-12-15T09:00", "2026-12-01", "2026-12-31"),
    ("this_month", "2028-02-10T09:00", "2028-02-01", "2028-02-29"),  # leap year
    ("last_month", "2026-01-15T09:00", "2025-12-01", "2025-12-31"),
    ("last_month", "2026-03-31T23:30", "2026-02-01", "2026-02-28"),
    ("Last Month", "2026-03-31T23:30", "2026-02-01", "2026-02-28"),
    ("last-week", "2026-01-01T09:00", "2025-12-22", "2025-12-28"),
])
def test_named_periods_are_whole_local_days(name, now, first, last):
    window = resolve_period(name, datetime.fromisoformat(now).replace(tzinfo=ROME_TZ))
    assert _days(window) == (first, last, True)


def test_explicit_days_and_ranges_ignore_now():
    now = datetime(2026, 10, 1, 9, tzinfo=ROME_TZ)
    assert _days(resolve_period("2026-04-04", now)) == ("2026-04-04", "2026-04-04", True)
    assert _days(resolve_period(" 2026-03-29..2026-03-30 ", now)) == (
        "2026-03-29", "2026-03-30", True)
    # one day across the spring-forward change is 23 hours long
    w = resolve_period("2026-03-29", now)
    span = w.end.astimezone(timezone.utc) - w.start.astimezone(timezone.utc)
    assert span + timedelta(microseconds=1) == timedelta(hours=23)


def test_the_hour_a_clock_change_repeats_belongs_to_its_day():
    # Europe/Rome repeats 02:00-03:00 on 2026-10-25; both 02:30s are that day,
    # and the day is 25 hours long. Same-zone datetimes compare by wall clock,
    # so this pins that the window compares instants.
    w = resolve_period("2026-10-24", datetime(2026, 10, 25, 9, tzinfo=ROME_TZ))
    late = datetime(2026, 10, 24, 23, 30, tzinfo=ROME_TZ)
    assert w.contains(late)
    day = resolve_period("2026-10-25", datetime(2026, 10, 25, 9, tzinfo=ROME_TZ))
    second_half_hour = datetime(2026, 10, 25, 2, 30, fold=1, tzinfo=ROME_TZ)
    assert day.contains(second_half_hour)
    assert day.contains(datetime(2026, 10, 25, 23, 59, 59, tzinfo=ROME_TZ))
    assert not day.contains(datetime(2026, 10, 26, 0, 0, tzinfo=ROME_TZ))
    span = day.end.astimezone(timezone.utc) - day.start.astimezone(timezone.utc)
    assert span + timedelta(microseconds=1) == timedelta(hours=25)
    # the end bound is an instant: one microsecond later is the next day
    after = (day.end.astimezone(timezone.utc) + timedelta(microseconds=1)).astimezone(ROME_TZ)
    assert not day.contains(after) and after.date().isoformat() == "2026-10-26"


def test_the_operators_local_day_is_not_the_utc_day():
    now = datetime(2026, 10, 1, 0, 30, tzinfo=ROME_TZ)  # still 30 Sept in UTC
    w = resolve_period("today", now)
    assert w.start.isoformat() == "2026-10-01T00:00:00+02:00"
    assert w.end.isoformat() == "2026-10-01T23:59:59.999999+02:00"


def test_a_day_whose_midnight_does_not_exist_starts_at_its_first_instant():
    # America/Santiago skips 00:00-01:00 on 2026-09-06
    tz = ZoneInfo("America/Santiago")
    w = resolve_period("2026-09-06", datetime(2026, 9, 7, 12, tzinfo=tz))
    assert w.start.astimezone(timezone.utc).isoformat() == "2026-09-06T04:00:00+00:00"
    assert w.contains(datetime(2026, 9, 6, 4, 0, tzinfo=timezone.utc))
    assert not w.contains(datetime(2026, 9, 6, 3, 59, 59, tzinfo=timezone.utc))


@pytest.mark.parametrize("spec", [
    "", "  ", "2026-04-05..2026-04-04", "20260404", "2026-W14-6", "2026-02-30",
    "2026-4-4", "2026-04-04..", "..2026-04-04", "2026-04-04..2026-04-05..2026-04-06",
    "next_fortnight", "this week please", "２０２６-04-04", 7, None, True,
])
def test_anything_else_is_refused(spec):
    with pytest.raises(ValueError):
        resolve_period(spec, datetime(2026, 10, 1, 9, tzinfo=ROME_TZ))


def test_resolution_needs_an_aware_now_and_windows_need_aware_ordered_bounds():
    with pytest.raises(ValueError):
        resolve_period("today", datetime(2026, 10, 1, 9))
    aware = datetime(2026, 10, 1, tzinfo=ROME_TZ)
    with pytest.raises(ValueError):
        RecallWindow(start=datetime(2026, 10, 1), end=aware)
    with pytest.raises(ValueError):
        RecallWindow(start=aware, end=aware - timedelta(microseconds=1))
    assert RecallWindow(start=aware, end=aware).contains(aware)


def test_a_window_compares_instants_not_wall_clocks():
    # 02:35 on the second pass through Rome's repeated hour is 01:35Z, an hour
    # after a window over the first pass's 02:30-02:40 (00:30Z-00:40Z) closed.
    first_pass = RecallWindow(
        start=datetime(2026, 10, 25, 2, 30, tzinfo=ROME_TZ),
        end=datetime(2026, 10, 25, 2, 40, tzinfo=ROME_TZ),
    )
    assert not first_pass.contains(datetime(2026, 10, 25, 2, 35, fold=1, tzinfo=ROME_TZ))
    assert first_pass.contains(datetime(2026, 10, 25, 0, 35, tzinfo=timezone.utc))
    with pytest.raises(ValueError):  # ends (00:45Z) before it starts (01:30Z)
        RecallWindow(start=datetime(2026, 10, 25, 2, 30, fold=1, tzinfo=ROME_TZ),
                     end=datetime(2026, 10, 25, 2, 45, tzinfo=ROME_TZ))
