"""#1117 regression guards: behaviour that was already true before the change
and must stay true after it (green at the base, mutation-checked; not red
cases). The dated, marked behaviour itself is pinned by
tests/test_pin_1117_dated_marked_recall.py."""
from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, patch

import pytest

import delegated_memory
import session_saver
from hindsight_memory import HindsightSemanticMemory
from memory_provenance import build_retain_items
from personality_types import RetainedTurn, SpeakerProvenance
from semantic_memory import RecallUnavailable
from session_reg_helpers import STUB_BINDING_DIGEST, STUB_SPEAKER_PROV, STUB_USER_PROV
from session_registry import SessionRegistry
from speaker_provenance import encode_provenance_tag
from test_pin_1117_dated_marked_recall import (
    MARK, SYSTEM, T0, T1, T2, T3, _Msg, _Resp, _Session, _env, _items,
    _scheduled_transcript, _ts, amsterdam,  # noqa: F401 — fixture re-export
)


async def _never_saved(_document_id):
    """#1123: the stored-tier reader for a bank that holds nothing yet."""
    return None

_TIERS = ("public", "friends", "family", "private")


def _tier_for(text: str) -> str:
    """A deterministic per-text classifier verdict, different across texts."""
    return _TIERS[hashlib.sha256(text.encode()).digest()[0] % 4]


# ---------------------------------------------------------------------------
# G: only a 503 is retried, and only on recall
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [504, 429, 500])
@pytest.mark.parametrize("method", ["recall", "recall_items"])
async def test_other_statuses_stay_single_shot(method, status):
    sleeps: list[float] = []

    async def _sleep(s):
        sleeps.append(s)

    mem = HindsightSemanticMemory(base_url="http://hs:8888", sleep=_sleep)
    session = _Session([_Resp(status=status), _Resp(body={"results": []})])
    mem._session = session
    with pytest.raises(RecallUnavailable) as ei:
        if method == "recall":
            await mem.recall("casa", "q", tags=["public"], max_tokens=100)
        else:
            await mem.recall_items("casa", "q", tags=["public"], max_tokens=100,
                                   clearance="public")
    assert (len(session.payloads), sleeps, ei.value.reason) == (1, [], f"http_{status}")


async def test_retain_is_never_retried_on_503():
    sleeps: list[float] = []

    async def _sleep(s):
        sleeps.append(s)

    mem = HindsightSemanticMemory(base_url="http://hs:8888", sleep=_sleep)
    session = _Session([_Resp(status=503), _Resp(body={})])
    mem._session = session
    import aiohttp
    with pytest.raises(aiohttp.ClientResponseError):
        await mem.retain("casa", [{"content": "x", "tags": ["public"]}])
    assert (len(session.payloads), sleeps) == (1, [])


# ---------------------------------------------------------------------------
# Delegated retains are unchanged (the per-turn tag never leaks into them)
# ---------------------------------------------------------------------------


async def test_retain_delegated_items_carry_exactly_tier_source_and_batch_tags(monkeypatch):
    async def _classify(text):
        return _tier_for(text)

    monkeypatch.setattr(delegated_memory, "classify_tier", _classify)
    sem = AsyncMock()
    sem.document_tags.return_value = None  # #1123: never saved
    turns = [RetainedTurn("question for finance", STUB_USER_PROV),
             RetainedTurn("finance answer", STUB_SPEAKER_PROV)]
    await delegated_memory.retain_delegated(
        sem, origin_channel="telegram", turns=turns, application_tags=("epoch-7",))
    items = sem.retain.await_args.args[1]
    assert [i["tags"] for i in items] == [
        [_tier_for(t.text), encode_provenance_tag(t.provenance), "epoch-7"] for t in turns
    ]
    assert all("timestamp" not in i for i in items)


# ---------------------------------------------------------------------------
# #1123 non-worsening: the tier sent is that save's own verdict, drawn once
# ---------------------------------------------------------------------------


async def test_scheduled_save_sends_each_items_own_tier_with_base_classifier_draws(monkeypatch, amsterdam):
    calls: list[str] = []

    async def _classify(text):
        calls.append(text)
        return _tier_for(text)

    monkeypatch.setattr(session_saver, "classify_tier", _classify)
    counts = {}
    for scheduled in (None, False, True):
        calls.clear()
        items = await _items(_scheduled_transcript(), scheduled=scheduled)
        counts[scheduled] = len(calls)
        assert sorted(calls) == sorted(i["content"] for i in items)
        for item in items:
            assert item["tags"][0] == _tier_for(item["content"])
            assert sum(1 for t in item["tags"] if t in _TIERS) == 1
    assert counts == {None: 8, False: 8, True: 8}


async def test_per_turn_tags_are_validated_before_any_classification():
    classified: list[str] = []

    async def _classify(text):
        classified.append(text)
        return "public"

    for bad in ("private", "casa-source-forged"):
        turns = [RetainedTurn("fine", STUB_USER_PROV),
                 RetainedTurn("fine", STUB_USER_PROV, application_tags=(bad,))]
        with pytest.raises(ValueError):
            await build_retain_items(turns, classify=_classify, stored_tags=_never_saved)
    assert classified == []


# ---------------------------------------------------------------------------
# Nothing saved today stops being saved; an unknown marker is today's form
# ---------------------------------------------------------------------------


async def test_every_marker_state_saves_the_same_items(monkeypatch, amsterdam):
    async def _classify(text):
        return "private"

    monkeypatch.setattr(session_saver, "classify_tier", _classify)
    contents = {}
    for scheduled in (None, False, True):
        items = await _items(_scheduled_transcript(), scheduled=scheduled)
        contents[scheduled] = [(i["content"], i["document_id"]) for i in items]
    assert contents[None] == contents[False] == contents[True]
    assert len(contents[None]) == 8


async def test_unknown_marker_saves_in_todays_form(tmp_path, monkeypatch, amsterdam):
    """P2: a reaper save of an entry written before the marker existed."""
    async def _classify(text):
        return "private"

    monkeypatch.setattr(session_saver, "classify_tier", _classify)
    reg = SessionRegistry(str(tmp_path / "s.json"))
    key = "telegram-v2-legacy"
    await reg.register(key, "resident:assistant", "sid-legacy",
                       binding_digest=STUB_BINDING_DIGEST,
                       speaker_provenance=STUB_SPEAKER_PROV, user_provenance=SYSTEM)
    reg._data[key].pop("scheduled")
    sem = AsyncMock()
    sem.document_tags.return_value = None  # #1123: never saved
    with patch("session_saver.get_session_messages", return_value=_scheduled_transcript()):
        await session_saver.save_session(key, reg, sem, directory="/x", channel="telegram")
    items = sem.retain.await_args.args[1]
    assert [i.get("timestamp") for i in items] == [
        _ts(T0), None, _ts(T1), None, _ts(T2), None, _ts(T3), None]
    assert sum(MARK in i["tags"] for i in items) == 0


async def test_a_dm_session_is_written_not_scheduled_and_marks_nothing(tmp_path, monkeypatch, amsterdam):
    async def _classify(text):
        return "private"

    monkeypatch.setattr(session_saver, "classify_tier", _classify)
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("telegram-v2-dm", "resident:assistant", "sid-dm",
                       binding_digest=STUB_BINDING_DIGEST,
                       speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    assert reg.get("telegram-v2-dm")["scheduled"] is False
    msgs = [_Msg("user", _env(T0) + "is the lamp on?"), _Msg("assistant", "It is off.")]
    sem = AsyncMock()
    sem.document_tags.return_value = None  # #1123: never saved
    with patch("session_saver.get_session_messages", return_value=msgs):
        await session_saver.save_session("telegram-v2-dm", reg, sem, directory="/x",
                                         channel="telegram")
    items = sem.retain.await_args.args[1]
    assert [(i["content"], i.get("timestamp"), MARK in i["tags"]) for i in items] == [
        ("is the lamp on?", _ts(T0), False), ("It is off.", _ts(T0), False)]


def test_snapshot_reads_a_corrupt_marker_as_unknown():
    from agent import snapshot_session_entry
    base = {"agent": "resident:assistant", "sdk_session_id": "s"}
    assert [snapshot_session_entry({**base, **extra}).scheduled for extra in (
        {}, {"scheduled": True}, {"scheduled": False}, {"scheduled": "yes"},
        {"scheduled": 1}, {"scheduled": None})] == [None, True, False, None, None, None]
