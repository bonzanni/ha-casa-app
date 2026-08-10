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

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import session_saver
from hindsight_ids import content_document_id
from session_saver import transcript_to_items
from session_reg_helpers import STUB_SPEAKER_PROV, STUB_USER_PROV
from timekeeping import compose_time_envelope, split_time_envelope, strip_time_envelope

pytestmark = pytest.mark.unit

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
        msgs, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
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
