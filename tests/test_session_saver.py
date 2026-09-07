# tests/test_session_saver.py
"""Per-channel freshness windows (spec §3.3): voice short, telegram long."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

import session_saver
from hindsight_ids import agent_document_id, content_document_id
from session_saver import freshness_window, reset_channel, save_session, transcript_to_items
from session_reg_helpers import STUB_BINDING_DIGEST, STUB_SPEAKER_PROV, STUB_USER_PROV

pytestmark = [pytest.mark.unit]


def test_voice_is_short():
    assert freshness_window("voice") == timedelta(minutes=30)


def test_telegram_is_long():
    assert freshness_window("telegram") == timedelta(hours=12)


def test_unknown_channel_falls_back_to_telegram_default():
    assert freshness_window("something-else") == timedelta(hours=12)


def test_env_override(monkeypatch):
    monkeypatch.setenv("FRESHNESS_VOICE_MINUTES", "10")
    assert freshness_window("voice") == timedelta(minutes=10)


class _Msg:
    def __init__(self, type_, message):
        self.type = type_
        self.message = message


async def test_transcript_to_items_builds_verified_shape(monkeypatch):
    # SessionMessage.message is Any — handle both content-block and string forms.
    # classify_tier is monkeypatched to a deterministic fake (avoids SDK I/O).
    async def fake_classify(content: str) -> str:
        return "public"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)

    msgs = [
        _Msg("user", {"role": "user", "content": "What temp do I like?"}),
        _Msg("assistant", {"role": "assistant", "content": [{"type": "text", "text": "20C."}]}),
    ]
    items = await transcript_to_items(
        msgs, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
    )
    assert [i["content"] for i in items] == ["What temp do I like?", "20C."]
    # Task 10: content-derived document_id, keyed by KIND — user turn on its
    # user_peer, assistant turn on its persona identity.
    assert items[0]["document_id"] == content_document_id("tester", "What temp do I like?")
    assert items[1]["document_id"] == agent_document_id(STUB_SPEAKER_PROV, "20C.")
    # Exactly one tier tag (first) + one reserved provenance tag per item.
    assert all(i["tags"][0] == "public" for i in items)
    assert all(sum(1 for t in i["tags"] if t.startswith("casa-source-")) == 1 for i in items)
    # Provenance survives into metadata for reconstruction.
    assert "casa_source_v1" in items[0]["metadata"]
    assert "casa_source_v1" in items[1]["metadata"]


async def test_transcript_to_items_skips_empty_and_toolonly(monkeypatch):
    async def fake_classify(content: str) -> str:
        return "public"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)

    msgs = [_Msg("assistant", {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]})]
    result = await transcript_to_items(
        msgs, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
    )
    assert result == []


async def test_save_session_retains_and_finishes(tmp_path, monkeypatch):
    async def fake_classify(content: str) -> str:
        return "friends"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)

    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("telegram-r1", "assistant", "sid-9", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    sem = AsyncMock()  # SemanticMemory
    msgs = [type("M", (), {"type": "user", "message": {"role": "user", "content": "hi"}})()]
    with patch("session_saver.get_session_messages", return_value=msgs):
        ok = await save_session(
            "telegram-r1", reg, sem,
            directory="/addon_configs/casa/agent-home/assistant",
            channel="telegram",
        )
    assert ok is True
    sem.retain.assert_awaited_once()
    bank, items = sem.retain.await_args.args[0], sem.retain.await_args.kwargs.get("items") or sem.retain.await_args.args[1]
    assert bank == "casa"
    assert items[0]["content"] == "hi"
    assert items[0]["tags"][0] == "friends"       # tier tag first (+ provenance tag)
    assert reg.get("telegram-r1") is None        # finished → entry removed


async def test_save_session_releases_claim_on_failure(tmp_path, monkeypatch):
    async def fake_classify(content: str) -> str:
        return "private"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)

    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("telegram-r1", "assistant", "sid-9", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    sem = AsyncMock()
    sem.retain.side_effect = RuntimeError("hindsight down")
    msgs = [type("M", (), {"type": "user", "message": {"content": "hi"}})()]
    with patch("session_saver.get_session_messages", return_value=msgs):
        ok = await save_session(
            "telegram-r1", reg, sem, directory="/d", channel="telegram",
        )
    assert ok is False
    assert reg.get("telegram-r1") is not None     # kept for retry
    assert not reg.get("telegram-r1").get("consolidated_at")  # claim released


async def test_save_session_cancellation_releases_the_claim(tmp_path, monkeypatch):
    """#345: CancelledError bypassed the `except Exception` that releases the
    save claim, so a shutdown mid-retain stranded `consolidated_at` — the
    reaper then skipped the entry for ~2 sweep intervals (C3 window) before
    stale-claim recovery kicked in."""
    import asyncio

    async def fake_classify(content: str) -> str:
        return "public"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)

    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("telegram-r1", "assistant", "sid-9", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)

    retain_started = asyncio.Event()

    class _HangingMemory:
        async def retain(self, *args, **kwargs):
            retain_started.set()
            await asyncio.Event().wait()  # hang until cancelled

    msgs = [type("M", (), {"type": "user", "message": {"content": "hi"}})()]
    with patch("session_saver.get_session_messages", return_value=msgs):
        task = asyncio.create_task(save_session(
            "telegram-r1", reg, _HangingMemory(), directory="/d", channel="telegram",
        ))
        await retain_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    entry = reg.get("telegram-r1")
    assert entry is not None                       # kept for retry
    assert not entry.get("consolidated_at")        # claim released despite the cancel


async def test_save_session_skips_when_already_claimed(tmp_path):
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("telegram-r1", "assistant", "sid-9", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    await reg.try_begin_save("telegram-r1")       # someone else claimed it
    sem = AsyncMock()
    ok = await save_session(
        "telegram-r1", reg, sem, directory="/d", channel="telegram",
    )
    assert ok is False
    sem.retain.assert_not_awaited()


async def test_save_session_empty_transcript_still_finishes(tmp_path):
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("telegram-r1", "assistant", "sid-9", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    sem = AsyncMock()
    # tool-only message → transcript_to_items returns [] → no retain, but still finishes
    msgs = [type("M", (), {"type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]}})()]
    with patch("session_saver.get_session_messages", return_value=msgs):
        ok = await save_session(
            "telegram-r1", reg, sem, directory="/d", channel="telegram",
        )
    assert ok is True
    sem.retain.assert_not_awaited()
    assert reg.get("telegram-r1") is None


async def test_save_session_no_sid_releases_claim(tmp_path):
    """Entry with no sdk_session_id → claim is released and False returned."""
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    # Plant an entry directly (no sdk_session_id) to hit the sid-guard.
    reg._data["telegram-r1"] = {"agent": "assistant"}
    sem = AsyncMock()
    ok = await save_session(
        "telegram-r1", reg, sem, directory="/d", channel="telegram",
    )
    assert ok is False
    sem.retain.assert_not_awaited()
    assert not reg.get("telegram-r1").get("consolidated_at")  # claim released


async def test_save_session_voice_skips_entirely(tmp_path):
    """Voice channel → writes_to_bank returns False → skip before any claim."""
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("voice-r1", "assistant", "sid-9", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    sem = AsyncMock()
    ok = await save_session(
        "voice-r1", reg, sem, directory="/d", channel="voice",
    )
    assert ok is False
    sem.retain.assert_not_awaited()
    # Entry is still present (not claimed) — voice sessions can be reaped after they go cold
    assert reg.get("voice-r1") is not None


async def test_reset_channel_saves_then_clears(tmp_path, monkeypatch):
    async def fake_classify(content: str) -> str:
        return "public"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)

    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("telegram-42", "assistant", "sid-9", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    sem = AsyncMock()
    msgs = [type("M", (), {"type": "user", "message": {"content": "remember X"}})()]
    with patch("session_saver.get_session_messages", return_value=msgs):
        await reset_channel("telegram-42", reg, sem, channel="telegram")
    sem.retain.assert_awaited_once()        # saved before clearing
    assert reg.get("telegram-42") is None   # pointer cleared → next turn starts fresh


async def test_reset_channel_no_entry_is_noop(tmp_path):
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    sem = AsyncMock()
    await reset_channel("telegram-99", reg, sem, channel="telegram")
    sem.retain.assert_not_awaited()         # nothing to save
    assert reg.get("telegram-99") is None


async def test_save_session_expected_sid_mismatch_releases_claim(tmp_path):
    """#353: the reaper decides an entry is cold, then a new turn replaces it
    before the save claim lands. save_session must notice the sid changed,
    release the claim it just placed on the NEW session, and retain nothing."""
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    # The registry now holds the NEW session (registered after the reaper's
    # cold snapshot of sid-old).
    await reg.register("telegram-r1", "assistant", "sid-new", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    sem = AsyncMock()
    ok = await save_session(
        "telegram-r1", reg, sem, directory="/d", channel="telegram",
        expected_sid="sid-old",
    )
    assert ok is False
    sem.retain.assert_not_awaited()
    entry = reg.get("telegram-r1")
    assert entry is not None                        # new session untouched
    assert entry["sdk_session_id"] == "sid-new"
    assert not entry.get("consolidated_at")         # claim released


async def test_save_session_expected_sid_match_proceeds(tmp_path, monkeypatch):
    async def fake_classify(content: str) -> str:
        return "public"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("telegram-r1", "assistant", "sid-9", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    sem = AsyncMock()
    msgs = [type("M", (), {"type": "user", "message": {"content": "hi"}})()]
    with patch("session_saver.get_session_messages", return_value=msgs):
        ok = await save_session(
            "telegram-r1", reg, sem, directory="/d", channel="telegram",
            expected_sid="sid-9",
        )
    assert ok is True
    assert reg.get("telegram-r1") is None


async def test_reset_channel_trailing_remove_spares_follow_up_session(tmp_path, monkeypatch):
    """#317: a follow-up message that registers a NEW session while /new's
    retain is in flight must not have its fresh session erased by the reset's
    trailing remove().

    #878 retargeted the seam from ``save_session`` to ``retain_cold_session``
    (the reset now retains its snapshot registry-decoupled); the assertions are
    unchanged — the reset must carry its OWN snapshot into the retain, and the
    follow-up's fresh session must survive."""
    from session_registry import SessionRegistry
    reg = SessionRegistry(str(tmp_path / "s.json"))
    await reg.register("telegram-42", "assistant", "sid-old", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    sem = AsyncMock()

    retained = []

    async def racing_retain(old, **kwargs):
        retained.append(old)
        # Simulate a follow-up turn landing mid-retain: it re-registers the
        # channel with a fresh session, then the retain fails silently.
        await reg.register(
            "telegram-42", "assistant", "sid-follow-up",
            binding_digest=STUB_BINDING_DIGEST,
            speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
        )

    monkeypatch.setattr(session_saver, "retain_cold_session", racing_retain)
    await reset_channel("telegram-42", reg, sem, channel="telegram")

    # The reset retains the conversation IT snapshotted, never the newer one.
    assert [s.sdk_session_id for s in retained] == ["sid-old"]
    entry = reg.get("telegram-42")
    assert entry is not None, "follow-up's fresh session must survive the reset"
    assert entry["sdk_session_id"] == "sid-follow-up"


# ---------------------------------------------------------------------------
# #878 — a /new whose retain fails keeps a way back to the conversation.
#
# The invariant these arms pin is DECLARED by the change (D34), not carried by
# the base: at the base the reset routes through ``save_session``, whose failure
# arm keeps the registry entry for the reaper — and the reset's trailing
# ``remove`` then deletes it, spooling nothing. The declaration:
#
#   An explicit Telegram reset whose retain fails ROUTES the ended conversation
#   into the module's registry-decoupled retry spool — the writer
#   ``_spool_cold_retain``, into the directory ``retry_spooled_cold_retains``
#   drains — rather than leaving the reset path with no retry route at all. Two
#   conditions on that routing, both pre-existing and unchanged: a retain-fence
#   generation change discards instead, by operator consent (INV-MEM-014); and
#   a spool write that itself fails is reported at ERROR naming the session id
#   and the transcript directory.
#
# It says nothing about the registry pointer, nothing about recoverability, and
# nothing about OUTCOMES beyond the route and the log — in particular nothing
# about how the existing retry path terminates, and nothing about whether some
# other record already covers the same session.
#
# Both exclusions were reached by the escalation rule rather than by preference.
# Three acceptance rounds returned the SAME SHAPE — an absolute clause in the
# declaration claiming more than the machinery guarantees. First the
# termination bound (the fence discard and the cancelled replay do not count an
# attempt; a failed attempt-counter write leaves a record retried forever
# without ever reaching the loud drop). Then "nothing retries the conversation"
# on the double failure (a record for the same sid written by an EARLIER failed
# reset survives an atomic write that fails before its rename, and will still
# retry). Each clause was CUT rather than narrowed again. No implementation
# change was made for either: the fence discard is INV-MEM-014 working as
# designed, and the bookkeeping, cancellation and record-preservation
# behaviours are pre-existing and untouched here. A fourth round cut one more
# phrase on the same shape — "a failed spool write writes no record", refuted by
# a `fsync_directory` that raises AFTER `os.replace` has already published the
# record (``atomic_io.py:95,104``), which the spool's own catch reports. The
# declaration keeps the ERROR requirement and drops the claim about the record.
# The arms below that exercise
# them are evidence about the record class this change now produces, not part
# of the declaration.
#
# Everything below runs against the REAL reset (``reset_channel`` →
# ``_reset_locked``), a REAL ``SessionRegistry`` persisted to disk, and the REAL
# SDK transcript reader over a real ``.jsonl`` in the SDK's own project layout.
# Only tier classification (the module's documented monkeypatch-by-name seam,
# to avoid spawning claude-CLI subprocesses) and the memory backend (whose
# raising IS the modelled outage) are substituted.
# ---------------------------------------------------------------------------

_R878_SID = "550e8400-e29b-41d4-a716-446655440000"
_R878_FOLLOW_UP_SID = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"


def _r878_write_transcript(project_dir, sid):
    """Write a real SDK-layout transcript for ``sid`` under the project dir.

    Placed through the SDK's OWN path helper, so the file lands exactly where
    the real ``get_session_messages`` looks; if that layout ever changes this
    fails loudly rather than silently reading nothing.
    """
    import json as _json

    from claude_agent_sdk._internal.sessions import _get_project_dir

    d = _get_project_dir(str(project_dir))
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "uuid": "u1", "parentUuid": None,
         "message": {"role": "user", "content": "Remember the blue box."}},
        {"type": "assistant", "uuid": "u2", "parentUuid": "u1",
         "message": {"role": "assistant",
                     "content": [{"type": "text",
                                  "text": "The blue box is upstairs."}]}},
    ]
    path = d / f"{sid}.jsonl"
    path.write_text("\n".join(_json.dumps(x) for x in lines) + "\n",
                    encoding="utf-8")
    return path


def _r878_records(retry_dir):
    """Every spool record in the retry directory, decoded, sorted by sid."""
    import json as _json
    from pathlib import Path

    root = Path(retry_dir)
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.glob("*.json")):
        out.append(_json.loads(p.read_text(encoding="utf-8")))
    return out


class _R878Memory:
    """Records every retain submission; raises when the outage is modelled."""

    def __init__(self, *, failing: bool) -> None:
        self.failing = failing
        self.retains: list[tuple] = []

    async def retain(self, bank, items, *, async_=False):
        self.retains.append((bank, items))
        if self.failing:
            raise RuntimeError("hindsight down")


@pytest.fixture
def r878(tmp_path, monkeypatch):
    """A registered telegram conversation with a real transcript on disk."""
    from types import SimpleNamespace

    import agent as _agent
    from claude_agent_sdk import get_session_messages
    from session_registry import SessionRegistry

    config_dir = tmp_path / "claude-config"
    project_dir = tmp_path / "agent-home" / "assistant"
    project_dir.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(
        _agent, "agent_home_for_role_id", lambda role: str(project_dir))

    async def fake_classify(content: str) -> str:
        return "private"
    monkeypatch.setattr(session_saver, "classify_tier", fake_classify)

    retry_dir = tmp_path / "cold-retain-retry"
    monkeypatch.setattr(session_saver, "_COLD_RETAIN_RETRY_DIR", str(retry_dir))

    _r878_write_transcript(project_dir, _R878_SID)
    # The reader is REAL: prove it can see this transcript before any reset
    # runs, so an arm that observes "0 retains" can never be an unreadable
    # fixture wearing the costume of a passing anti-overfire assertion.
    assert len(get_session_messages(_R878_SID, str(project_dir))) == 2

    registry_path = tmp_path / "sessions.json"
    reg = SessionRegistry(str(registry_path))
    return SimpleNamespace(
        reg=reg, registry_path=registry_path, project_dir=project_dir,
        retry_dir=retry_dir, sid=_R878_SID,
    )


async def _r878_register(reg, key="telegram-42", sid=_R878_SID):
    await reg.register(
        key, "assistant", sid, binding_digest=STUB_BINDING_DIGEST,
        speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
    )


async def test_r878_failed_retain_leaves_one_durable_retry_record(r878):
    """The whole defect: `/new` during a bank outage must not lose the
    conversation. Exactly one record, carrying the snapshotted session."""
    import stat

    await _r878_register(r878.reg)
    sem = _R878Memory(failing=True)

    await reset_channel("telegram-42", r878.reg, sem, channel="telegram")

    assert len(sem.retains) == 1                       # the retain was attempted
    records = _r878_records(r878.retry_dir)
    assert len(records) == 1                           # …and left a way back
    (rec,) = records
    assert rec["sdk_session_id"] == r878.sid
    assert rec["directory"] == str(r878.project_dir)
    assert rec["channel"] == "telegram"
    assert rec["attempts"] == 0
    assert rec["speaker_provenance"]["role_id"] == "resident:assistant"
    assert rec["user_provenance"]["user_peer"] == "tester"
    assert r878.reg.get("telegram-42") is None         # pointer still dropped
    assert not r878.reg.retirement_pending("telegram-42")
    # GHSA-569r-7crq-xr43: the record names a session id, a transcript
    # directory and speaker provenance — 0o600 under a 0o700 parent.
    record_path = next(r878.retry_dir.glob("*.json"))
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(r878.retry_dir.stat().st_mode) == 0o700


async def test_r878_record_is_drained_by_a_direct_replay(r878):
    """Something acts on the record: the spool drain retains and removes it."""
    from session_saver import retry_spooled_cold_retains

    await _r878_register(r878.reg)
    sem = _R878Memory(failing=True)
    await reset_channel("telegram-42", r878.reg, sem, channel="telegram")
    assert len(_r878_records(r878.retry_dir)) == 1

    sem.failing = False
    await retry_spooled_cold_retains(sem, retry_dir=r878.retry_dir)

    assert len(sem.retains) == 2                       # replayed exactly once
    assert [i["content"] for i in sem.retains[1][1]] == [
        "Remember the blue box.", "The blue box is upstairs."]
    assert _r878_records(r878.retry_dir) == []         # record consumed


async def test_r878_record_is_drained_by_the_real_freshness_sweep(r878):
    """The drain is not hypothetical: the shipped reaper drives it every sweep,
    against a registry reconstructed from disk (nothing in memory carries it)."""
    from freshness_reaper import FreshnessReaper
    from session_registry import SessionRegistry

    await _r878_register(r878.reg)
    sem = _R878Memory(failing=True)
    await reset_channel("telegram-42", r878.reg, sem, channel="telegram")
    assert len(_r878_records(r878.retry_dir)) == 1

    sem.failing = False
    reloaded = SessionRegistry(str(r878.registry_path))
    reaper = FreshnessReaper(
        registry=reloaded, semantic_memory=sem,
        directory_for=lambda role: str(r878.project_dir),
    )
    await reaper.sweep_once()

    assert len(sem.retains) == 2
    assert _r878_records(r878.retry_dir) == []


async def test_r878_recall_only_channel_spools_nothing(r878, caplog):
    """Anti-overfire: voice is recall-only. Nothing may be banked, so nothing
    may be queued for a retry that would bank it (INV-MEM-005)."""
    import logging

    _r878_write_transcript(r878.project_dir, r878.sid)
    await _r878_register(r878.reg, key="voice-7")
    sem = _R878Memory(failing=True)

    with caplog.at_level(logging.ERROR, logger="session_saver"):
        await reset_channel("voice-7", r878.reg, sem, channel="voice")

    assert sem.retains == []
    assert _r878_records(r878.retry_dir) == []
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.parametrize("missing", ["speaker_provenance", "user_provenance"])
async def test_r878_unusable_provenance_spools_nothing(r878, caplog, missing):
    """Anti-overfire: a legacy/corrupt entry is dropped, never retried forever —
    memory is never written with invented authorship. Zero ERRORs matters as
    much as zero records: a mutant that bypasses the provenance guard also
    produces zero retains and zero records, by failing downstream."""
    import logging

    await _r878_register(r878.reg)
    r878.reg._data["telegram-42"].pop(missing)
    await r878.reg.save()
    sem = _R878Memory(failing=True)

    with caplog.at_level(logging.ERROR, logger="session_saver"):
        await reset_channel("telegram-42", r878.reg, sem, channel="telegram")

    assert sem.retains == []
    assert _r878_records(r878.retry_dir) == []
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert r878.reg.get("telegram-42") is None


async def test_r878_advanced_fence_generation_spools_nothing(r878):
    """Anti-overfire, INV-MEM-014: the fence generation the reset captured in
    its no-await block must gate the spool as well as the retain — a durable
    record written for content an operator consented to deleting is exactly the
    residue a wipe must not leave.

    The generation is advanced from a reset listener, i.e. during the
    flush-close, which is where a completing wipe's exclusive section bumps it.
    This pins the fence guard as defence in depth; it does NOT claim a real wipe
    can complete inside a live reset — `reset_channel` holds turn admission
    SHARED for its whole body and `wipe_long_term_memory` takes it EXCLUSIVELY,
    so that interleaving is unreachable through the real reset."""
    from memory_wipe import FENCE

    await _r878_register(r878.reg)
    sem = _R878Memory(failing=True)
    listener_calls = []

    async def wipe_completes_mid_close(key):
        listener_calls.append(key)
        FENCE._generation += 1        # what _ExclusiveSection.__aenter__ does

    r878.reg.add_reset_listener(wipe_completes_mid_close)
    before = FENCE._generation
    try:
        await reset_channel("telegram-42", r878.reg, sem, channel="telegram")
    finally:
        FENCE._generation = before

    assert listener_calls == ["telegram-42"]
    assert sem.retains == []
    assert _r878_records(r878.retry_dir) == []


async def test_r878_follow_up_session_survives_and_the_old_one_is_spooled(r878):
    """The trailing sid guard is unchanged: a follow-up turn that re-registers
    during the flush-close keeps its fresh session, while the conversation the
    reset was ending still gets its retry record."""
    await _r878_register(r878.reg)
    sem = _R878Memory(failing=True)
    listener_calls = []

    async def racing_turn(key):
        listener_calls.append(key)
        await _r878_register(r878.reg, key=key, sid=_R878_FOLLOW_UP_SID)

    r878.reg.add_reset_listener(racing_turn)
    await reset_channel("telegram-42", r878.reg, sem, channel="telegram")

    assert listener_calls == ["telegram-42"]
    assert len(sem.retains) == 1                       # the OLD conversation
    records = _r878_records(r878.retry_dir)
    assert [r["sdk_session_id"] for r in records] == [r878.sid]
    entry = r878.reg.get("telegram-42")
    assert entry is not None and entry["sdk_session_id"] == _R878_FOLLOW_UP_SID


async def test_r878_double_failure_says_so_and_promises_nothing(r878, caplog):
    """Retain fails AND the spool write fails before it can publish a record:
    the two facts left to work from — the session id and the transcript
    directory — are said out loud at ERROR.

    It asserts no absolute about what retries afterwards, and the declaration
    makes no such claim either: a record for the same session written by an
    EARLIER failed reset would survive this arm's pre-``os.replace`` failure and
    would still be retried."""
    import logging

    from unittest.mock import patch as _patch

    await _r878_register(r878.reg)
    sem = _R878Memory(failing=True)
    spool_writes = []

    def exploding_write(path, obj, **kw):
        spool_writes.append(path)
        raise OSError("no space left on device")

    with caplog.at_level(logging.ERROR, logger="session_saver"), \
            _patch.object(session_saver, "atomic_write_json", exploding_write):
        await reset_channel("telegram-42", r878.reg, sem, channel="telegram")

    assert len(sem.retains) == 1
    assert len(spool_writes) == 1
    assert _r878_records(r878.retry_dir) == []
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert r878.sid in message
    assert str(r878.project_dir) in message


async def test_r878_retries_are_bounded_and_the_give_up_is_loud(r878, caplog):
    """The declaration's bound, measured: 48 FURTHER attempts after the reset's
    own, then the record is dropped with exactly one ERROR — 49 retain calls in
    total. Nothing silently retries forever, and nothing silently stops."""
    import logging

    from session_saver import _COLD_RETAIN_MAX_ATTEMPTS, retry_spooled_cold_retains

    assert _COLD_RETAIN_MAX_ATTEMPTS == 48
    await _r878_register(r878.reg)
    sem = _R878Memory(failing=True)
    await reset_channel("telegram-42", r878.reg, sem, channel="telegram")
    assert len(sem.retains) == 1

    with caplog.at_level(logging.ERROR, logger="session_saver"):
        for n in range(1, _COLD_RETAIN_MAX_ATTEMPTS):          # 1 … 47
            await retry_spooled_cold_retains(sem, retry_dir=r878.retry_dir)
            records = _r878_records(r878.retry_dir)
            assert len(records) == 1, f"record dropped early at replay {n}"
            assert records[0]["attempts"] == n
            assert len(sem.retains) == 1 + n
            assert [r for r in caplog.records
                    if r.levelno >= logging.ERROR] == []

        await retry_spooled_cold_retains(sem, retry_dir=r878.retry_dir)

    assert len(sem.retains) == 1 + _COLD_RETAIN_MAX_ATTEMPTS    # 49
    assert _r878_records(r878.retry_dir) == []
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert r878.sid in errors[0].getMessage()
    assert "48" in errors[0].getMessage()

    await retry_spooled_cold_retains(sem, retry_dir=r878.retry_dir)
    assert len(sem.retains) == 1 + _COLD_RETAIN_MAX_ATTEMPTS    # nothing left
