# tests/test_freshness_reaper.py
"""FreshnessReaper (spec §4.2 entry point 1): saves sessions idle past their
channel freshness window. Runs once at boot then hourly; never resumes a saved
session. Includes C3 stale-claim recovery."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from freshness_reaper import FreshnessReaper
from session_registry import SessionRegistry
from session_reg_helpers import STUB_BINDING_DIGEST, STUB_SPEAKER_PROV, STUB_USER_PROV

pytestmark = [pytest.mark.unit]


async def test_sweep_saves_only_cold_conversational_entries(tmp_path):
    reg = SessionRegistry(str(tmp_path / "s.json"))
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    # cold voice (idle 1h > 30m window) → REMOVED (recall-only, not saved)
    await reg.register("voice-r1", "assistant", "sid-1", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    reg._data["voice-r1"]["last_active"] = (now - timedelta(hours=1)).isoformat()
    # cold telegram (idle 13h > 12h window) → saved
    await reg.register("telegram-r2", "assistant", "sid-3", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    reg._data["telegram-r2"]["last_active"] = (now - timedelta(hours=13)).isoformat()
    # warm telegram (idle 1h < 12h window) → skip
    await reg.register("telegram-42", "assistant", "sid-2", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    reg._data["telegram-42"]["last_active"] = (now - timedelta(hours=1)).isoformat()

    saved = []
    async def fake_save(key, *a, **k):
        saved.append(key); return True

    reaper = FreshnessReaper(
        registry=reg, semantic_memory=AsyncMock(),
        directory_for=lambda role: f"/home/{role}", now=lambda: now, save_fn=fake_save,
    )
    await reaper.sweep_once()
    assert saved == ["telegram-r2"]          # cold telegram saved
    assert reg.get("voice-r1") is None       # cold voice entry removed


async def test_sweep_skips_webhook_and_scheduler(tmp_path):
    reg = SessionRegistry(str(tmp_path / "s.json"))
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    await reg.register("webhook-abc", "assistant", "sid-3", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    reg._data["webhook-abc"]["last_active"] = (now - timedelta(days=5)).isoformat()
    saved = []
    async def fake_save(key, *a, **k):
        saved.append(key); return True
    reaper = FreshnessReaper(registry=reg, semantic_memory=AsyncMock(),
        directory_for=lambda r: "/h", now=lambda: now, save_fn=fake_save)
    await reaper.sweep_once()
    assert saved == []   # webhook one-shots are not retained


async def test_fresh_claim_is_skipped_but_stale_claim_is_recovered(tmp_path):
    reg = SessionRegistry(str(tmp_path / "s.json"))
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    # cold voice with a FRESH in-flight claim → skip (a save is running); claim left intact
    await reg.register("voice-fresh", "assistant", "sid-a", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    reg._data["voice-fresh"]["last_active"] = (now - timedelta(hours=1)).isoformat()
    reg._data["voice-fresh"]["consolidated_at"] = (now - timedelta(minutes=1)).isoformat()
    # cold telegram with a STALE claim (crashed mid-save) → recover + save
    await reg.register("telegram-stale", "assistant", "sid-b", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    reg._data["telegram-stale"]["last_active"] = (now - timedelta(hours=13)).isoformat()
    reg._data["telegram-stale"]["consolidated_at"] = (now - timedelta(hours=5)).isoformat()

    saved = []
    async def fake_save(key, *a, **k):
        saved.append(key); return True
    reaper = FreshnessReaper(registry=reg, semantic_memory=AsyncMock(),
        directory_for=lambda r: "/h", now=lambda: now, save_fn=fake_save,
        interval_s=3600.0)
    await reaper.sweep_once()
    assert saved == ["telegram-stale"]                          # stale claim recovered + saved
    assert "consolidated_at" not in reg.get("telegram-stale")  # clear_save_claim ran before save_fn; entry remains because the fake save_fn doesn't finish_save
    assert reg.get("voice-fresh").get("consolidated_at")        # fresh claim untouched


async def test_sweep_drops_stale_legacy_entry_with_no_provenance(tmp_path):
    """M1: a legacy pre-Task-9 entry (valid agent/sdk_session_id, but no
    speaker_provenance/user_provenance) must be DROPPED like a None snapshot,
    not handed to save_session forever (save_session refuses to retain it and
    returns False without removing the entry, which would churn every sweep)."""
    reg = SessionRegistry(str(tmp_path / "s.json"))
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    await reg.register("telegram-legacy", "assistant", "sid-legacy", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    # Simulate a pre-Task-9 entry: strip the provenance fields register() added.
    del reg._data["telegram-legacy"]["speaker_provenance"]
    del reg._data["telegram-legacy"]["user_provenance"]
    reg._data["telegram-legacy"]["last_active"] = (now - timedelta(hours=13)).isoformat()

    save_fn = AsyncMock(return_value=True)
    reaper = FreshnessReaper(
        registry=reg, semantic_memory=AsyncMock(),
        directory_for=lambda role: f"/home/{role}", now=lambda: now, save_fn=save_fn,
    )
    await reaper.sweep_once()
    assert save_fn.await_count == 0            # never handed to a save that can't retain it
    assert reg.get("telegram-legacy") is None  # stale pointer dropped, not retried forever


def test_is_stale_claim_handles_bad_input():
    from datetime import datetime, timezone
    reaper = FreshnessReaper(registry=None, semantic_memory=None,
        directory_for=lambda r: "/h", interval_s=3600.0)
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    assert reaper._is_stale_claim(None, now) is True          # non-str → reclaim
    assert reaper._is_stale_claim("not-a-date", now) is True  # bad ISO → reclaim


async def test_sweep_passes_cold_snapshot_sid_to_save(tmp_path):
    """#353: the reaper must pin the save to the session it judged cold, so a
    new turn racing the claim can never have its live session retained+removed."""
    reg = SessionRegistry(str(tmp_path / "s.json"))
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    await reg.register("telegram-r2", "assistant", "sid-cold", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    reg._data["telegram-r2"]["last_active"] = (now - timedelta(hours=13)).isoformat()

    seen = {}
    async def fake_save(key, *a, **k):
        seen[key] = k
        return True

    reaper = FreshnessReaper(
        registry=reg, semantic_memory=AsyncMock(),
        directory_for=lambda role: f"/home/{role}", now=lambda: now, save_fn=fake_save,
    )
    await reaper.sweep_once()
    assert seen["telegram-r2"].get("expected_sid") == "sid-cold"


async def test_sweep_direct_removals_spare_a_racing_fresh_registration(tmp_path):
    """#353 (Sol r2): the sweep iterates a STALE all_entries() snapshot across
    awaits. A cold voice entry replaced by a new live registration mid-sweep
    must not have its fresh session deleted by the recall-only removal branch."""
    reg = SessionRegistry(str(tmp_path / "s.json"))
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    await reg.register("telegram-a", "assistant", "sid-a", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    reg._data["telegram-a"]["last_active"] = (now - timedelta(hours=13)).isoformat()
    await reg.register("voice-b", "butler", "sid-cold", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    reg._data["voice-b"]["last_active"] = (now - timedelta(hours=1)).isoformat()

    async def racing_save(key, *a, **k):
        # While the sweep saves the telegram entry, a live voice turn
        # re-registers the voice key the sweep judged cold.
        await reg.register(
            "voice-b", "butler", "sid-live",
            binding_digest=STUB_BINDING_DIGEST,
            speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV,
        )
        return True

    reaper = FreshnessReaper(
        registry=reg, semantic_memory=AsyncMock(),
        directory_for=lambda role: "/h", now=lambda: now, save_fn=racing_save,
    )
    await reaper.sweep_once()
    entry = reg.get("voice-b")
    assert entry is not None, "the racing fresh voice session must survive the sweep"
    assert entry["sdk_session_id"] == "sid-live"


async def test_sweep_processes_the_cold_retain_retry_spool(tmp_path, monkeypatch):
    """#345: the reaper is the durable retry driver for failed gap retains —
    every sweep must process the spool (session_saver.retry_spooled_cold_retains)
    so a transient Hindsight outage no longer loses the old transcript."""
    import freshness_reaper as reaper_mod

    reg = SessionRegistry(str(tmp_path / "s.json"))
    sem = AsyncMock()
    calls = []

    async def fake_retry(semantic_memory, *, retry_dir=None):
        calls.append((semantic_memory, retry_dir))

    monkeypatch.setattr(reaper_mod, "retry_spooled_cold_retains", fake_retry)
    reaper = FreshnessReaper(
        registry=reg, semantic_memory=sem,
        directory_for=lambda role: "/h",
        now=lambda: datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc),
    )
    await reaper.sweep_once()
    assert calls and calls[0][0] is sem


async def test_stale_claim_release_declines_after_reregistration(tmp_path):
    """#526 (Terra diff-r1): staleness is judged from the sweep's STALE entry
    copy. A re-registration (same sid included) that lands a FRESH claim
    between the copy and the release must keep that claim — the generation
    guard declines the unconditional clear the pre-fix code issued."""
    reg = SessionRegistry(str(tmp_path / "s.json"))
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    await reg.register("telegram-stale", "assistant", "sid-b", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
    reg._data["telegram-stale"]["last_active"] = (now - timedelta(hours=13)).isoformat()
    reg._data["telegram-stale"]["consolidated_at"] = (now - timedelta(hours=5)).isoformat()

    saved = []

    async def racing_save(key, *a, **k):
        saved.append(key)
        return True

    reaper = FreshnessReaper(registry=reg, semantic_memory=AsyncMock(),
        directory_for=lambda r: "/h", now=lambda: now, save_fn=racing_save,
        interval_s=3600.0)

    # Simulate the race: after sweep_once() captured its entry+generation
    # copy but before the per-key body runs, a same-sid turn re-registers and
    # a new saver claims. Patch clear_save_claim's entry hook via a wrapper
    # that performs the re-registration+claim FIRST, exactly once.
    from session_registry import _UNCONDITIONAL

    real_clear = reg.clear_save_claim
    raced = {"done": False}

    # Transparent wrapper: forward EXACTLY what the reaper passed (default
    # _UNCONDITIONAL, matching the real signature — a None default would
    # itself act as a guard and mask an unguarded caller).
    async def racing_clear(key, sid=None, *, expected_generation=_UNCONDITIONAL):
        if not raced["done"]:
            raced["done"] = True
            await reg.register("telegram-stale", "assistant", "sid-b", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
            reg._data["telegram-stale"]["consolidated_at"] = now.isoformat()
        await real_clear(key, sid, expected_generation=expected_generation)

    reg.clear_save_claim = racing_clear
    await reaper.sweep_once()

    # The fresh claim survived the stale-claim release (generation moved).
    assert reg.get("telegram-stale").get("consolidated_at") == now.isoformat()
