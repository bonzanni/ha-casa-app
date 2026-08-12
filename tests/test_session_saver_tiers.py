# tests/test_session_saver_tiers.py
"""Save path: per-item true-tier tags, shared bank, voice is recall-only."""
import logging
import sys
import types

import pytest

import session_saver
from hindsight_ids import agent_document_id, content_document_id
from session_reg_helpers import STUB_SPEAKER_PROV, STUB_USER_PROV

pytestmark = [pytest.mark.unit]

# STUB_USER_PROV.user_peer is "tester"; STUB_SPEAKER_PROV is a resident.
_USER_PEER = STUB_USER_PROV.user_peer


class _Msg:
    def __init__(self, mtype, text):
        self.type = mtype
        self.message = {"role": mtype, "content": text}


async def test_transcript_items_tagged_per_item(monkeypatch):
    async def fake_classify(content: str) -> str:
        return "private" if "salary" in content else "public"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)

    msgs = [_Msg("user", "my salary is 5000"), _Msg("assistant", "bin day is Tuesday")]
    items = await session_saver.transcript_to_items(
        msgs, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
    )
    assert [i["tags"][0] for i in items] == ["private", "public"]
    # Task 10: content-derived document_id, keyed by KIND (user_peer vs persona).
    assert [i["document_id"] for i in items] == [
        content_document_id(_USER_PEER, "my salary is 5000"),
        agent_document_id(STUB_SPEAKER_PROV, "bin day is Tuesday"),
    ]


def _install_fake_sdk_reply(monkeypatch, reply: str):
    """Fake claude_agent_sdk for the REAL classify_tier (no monkeypatched
    classifier here — the #508 aggregate count lives inside tier_classifier,
    so the save-level test must run the genuine classify path)."""
    fake = types.ModuleType("claude_agent_sdk")

    class _Text:
        def __init__(self, text):
            self.text = text

    class _Assistant:
        def __init__(self, text):
            self.content = [_Text(text)]

    class ClaudeAgentOptions:  # noqa: N801
        def __init__(self, **kw):
            self.kw = kw

    fake.ClaudeAgentOptions = ClaudeAgentOptions
    fake.AssistantMessage = _Assistant

    async def query(*, prompt, options):  # noqa: ANN001
        yield _Assistant(reply)

    fake.query = query
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)


async def test_save_logs_one_aggregate_defaulted_count(monkeypatch, caplog):
    """#508: at a ~12% per-call default rate, small saves look clean by chance
    and per-item WARNs are scattered — the save must log ONE
    N-defaulted-of-M line when any item fell to the failure default."""
    _install_fake_sdk_reply(monkeypatch, "no mandated answer line here")
    msgs = [_Msg("user", "my salary is 5000"), _Msg("assistant", "bin day is Tuesday")]
    with caplog.at_level(logging.WARNING):
        items = await session_saver.transcript_to_items(
            msgs, speaker_provenance=STUB_SPEAKER_PROV,
            user_provenance=STUB_USER_PROV,
        )
    # Leak-safe default preserved on the items themselves.
    assert [i["tags"][0] for i in items] == ["private", "private"]
    aggregate = [r.getMessage() for r in caplog.records
                 if "of 2 items" in r.getMessage()]
    assert len(aggregate) == 1
    assert "2 of 2 items" in aggregate[0]
    # Aggregate line stays content-free (structural counts only).
    assert "salary" not in aggregate[0]


async def test_clean_save_logs_no_aggregate_line(monkeypatch, caplog):
    _install_fake_sdk_reply(monkeypatch, "friends")
    msgs = [_Msg("user", "dinner is at seven")]
    with caplog.at_level(logging.WARNING):
        items = await session_saver.transcript_to_items(
            msgs, speaker_provenance=STUB_SPEAKER_PROV,
            user_provenance=STUB_USER_PROV,
        )
    assert [i["tags"][0] for i in items] == ["friends"]
    assert not [r for r in caplog.records if "defaulted" in r.getMessage()]


async def test_transcript_dedupes_repeated_line_within_batch(monkeypatch):
    """F1: a line repeated N times in one transcript yields ONE item (first
    occurrence wins, order preserved)."""
    calls = []

    async def fake_classify(content: str) -> str:
        calls.append(content)
        return "public"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)

    msgs = [
        _Msg("user", "harden probe TG-DISPATCH-001 reply ok"),
        _Msg("assistant", "ok"),
        _Msg("user", "harden probe TG-DISPATCH-001 reply ok"),  # dup
        _Msg("user", "harden probe TG-DISPATCH-001 reply ok"),  # dup
        _Msg("assistant", "ok"),                                # dup
    ]
    items = await session_saver.transcript_to_items(
        msgs, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
    )
    assert [i["content"] for i in items] == [
        "harden probe TG-DISPATCH-001 reply ok", "ok",
    ]
    # classify runs once per distinct item, not once per occurrence.
    assert len(calls) == 2


async def test_transcript_same_content_stable_id_across_sessions(monkeypatch):
    """F1: identical (speaker, text) retained from a rotated sid maps to the
    SAME document_id, so Hindsight upserts instead of duplicating."""
    async def fake_classify(content: str) -> str:
        return "public"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)

    msg = [_Msg("user", "the bins go out on Tuesday")]
    a = await session_saver.transcript_to_items(
        msg, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    b = await session_saver.transcript_to_items(
        msg, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    assert a[0]["document_id"] == b[0]["document_id"]

    # A different speaker KIND (agent) with the same words is a distinct document.
    other = await session_saver.transcript_to_items(
        [_Msg("assistant", "the bins go out on Tuesday")],
        speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
    )
    assert other[0]["document_id"] != a[0]["document_id"]


async def test_save_session_skips_voice():
    retained = []

    class _Sem:
        async def retain(self, bank, items, *, async_=True):
            retained.append(bank)

    class _Reg:
        async def try_begin_save(self, k): return True
        def get(self, k): return {"sdk_session_id": "s1"}
        async def clear_save_claim(self, k, sid=None): pass
        async def finish_save(self, k, sid=None): pass

    ok = await session_saver.save_session(
        "voice-abc", _Reg(), _Sem(), directory="/tmp", channel="voice",
    )
    assert ok is False
    assert retained == []  # voice persists nothing (recall-only)


async def test_save_session_retains_telegram_to_shared_bank(monkeypatch):
    async def fake_classify(content: str) -> str:
        return "friends"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    monkeypatch.setattr(
        session_saver, "get_session_messages",
        lambda sid, directory: [_Msg("user", "kids like pizza")],
    )
    retained = {}

    class _Sem:
        async def retain(self, bank, items, *, async_=True):
            retained["bank"] = bank
            retained["tags"] = [i["tags"] for i in items]

    from speaker_provenance import provenance_mapping

    class _Reg:
        async def try_begin_save(self, k): return True
        def get(self, k):
            # Task 10: save_session reads speaker/user provenance from the entry.
            return {
                "agent": "resident:assistant", "sdk_session_id": "s1",
                "speaker_provenance": provenance_mapping(STUB_SPEAKER_PROV),
                "user_provenance": provenance_mapping(STUB_USER_PROV),
            }
        async def finish_save(self, k, sid=None): pass
        async def clear_save_claim(self, k, sid=None): pass

    ok = await session_saver.save_session(
        "telegram-1", _Reg(), _Sem(), directory="/tmp", channel="telegram",
    )
    assert ok is True
    assert retained["bank"] == "casa"
    assert [t[0] for t in retained["tags"]] == ["friends"]   # tier tag first


async def test_transcript_classification_is_concurrent(monkeypatch):
    """M29: classification must overlap (bounded gather), not run one-at-a-time,
    while preserving order and idempotent document_ids."""
    import asyncio
    in_flight = 0
    peak = 0

    async def fake_classify(content: str) -> str:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return "public"

    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    msgs = [_Msg("user", f"fact {i}") for i in range(8)]
    items = await session_saver.transcript_to_items(
        msgs, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
    )
    assert len(items) == 8
    # Serial gives peak == 1; bounded-parallel gives peak in [2, 4].
    assert peak >= 2, f"classification ran serially (peak in-flight = {peak})"
    assert peak <= session_saver._CLASSIFY_CONCURRENCY, "semaphore bound exceeded"
    # Content-derived ids, one per distinct fact, order preserved.
    assert [i["document_id"] for i in items] == [
        content_document_id(_USER_PEER, f"fact {i}") for i in range(8)
    ]
