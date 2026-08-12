# tests/test_reaper_voice_recall_only.py
"""The reaper saves telegram but never voice; it drops cold voice entries."""
from datetime import datetime, timedelta, timezone

import pytest

from freshness_reaper import FreshnessReaper
from session_reg_helpers import STUB_SPEAKER_PROV, STUB_USER_PROV
from speaker_provenance import provenance_mapping

pytestmark = [pytest.mark.unit]

_PROV_FIELDS = {
    "sdk_session_id": "sid-1",
    "speaker_provenance": provenance_mapping(STUB_SPEAKER_PROV),
    "user_provenance": provenance_mapping(STUB_USER_PROV),
}


class _Reg:
    def __init__(self, entries):
        self._e = entries
        self.removed = []
        self.saved = []
        self.cleared_claims = []

    def all_entries(self):
        return dict(self._e)

    def generation(self, key):
        return None

    async def remove(self, key, expected_sid=None, *, expected_generation=None):
        self.removed.append(key)
        self._e.pop(key, None)

    async def clear_save_claim(self, key, sid=None, *, expected_generation=None):
        self.cleared_claims.append(key)
        self._e.get(key, {}).pop("consolidated_at", None)


async def _save_fn(key, reg, sem, *, directory, channel, expected_sid=None, expected_generation=None):
    reg.saved.append((key, channel))
    return True


def _old_iso():
    return (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()


async def test_reaper_saves_telegram_not_voice():
    reg = _Reg({
        "telegram-1": {"agent": "assistant", "last_active": _old_iso(), **_PROV_FIELDS},
        "voice-1": {"agent": "butler", "last_active": _old_iso(), **_PROV_FIELDS},
    })
    reaper = FreshnessReaper(
        registry=reg, semantic_memory=object(),
        directory_for=lambda role: "/tmp", save_fn=_save_fn,
    )
    await reaper.sweep_once()
    assert ("telegram-1", "telegram") in reg.saved
    assert all(ch != "voice" for _, ch in reg.saved)   # voice never saved
    assert "voice-1" in reg.removed                      # cold voice entry dropped


async def test_stale_claim_voice_entry_is_cleared_and_removed():
    """A cold VOICE entry with a stale save-claim: claim cleared, entry removed, never saved."""
    # stale consolidated_at: 1 day old > _STALE_CLAIM_MULTIPLIER × interval_s (2 × 3600s = 7200s)
    stale_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    reg = _Reg({
        "voice-stale": {
            "agent": "assistant",
            "last_active": _old_iso(),
            "consolidated_at": stale_iso,
        },
    })
    reaper = FreshnessReaper(
        registry=reg, semantic_memory=object(),
        directory_for=lambda role: "/tmp", save_fn=_save_fn,
        interval_s=3600.0,
    )
    await reaper.sweep_once()
    assert "voice-stale" in reg.removed             # cold voice entry dropped
    assert "voice-stale" not in [k for k, _ in reg.saved]  # never saved
    assert "voice-stale" in reg.cleared_claims      # stale claim was cleared before removal
