"""Tests for session_registry.py."""

import asyncio
import json
import os
import time

import pytest

from session_registry import SessionRegistry
from session_reg_helpers import STUB_BINDING_DIGEST, STUB_SPEAKER_PROV, STUB_USER_PROV

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class TestSessionRegistry:
    async def test_register_and_get(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("tg:123", "assistant", "sdk-1", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)

        entry = reg.get("tg:123")
        assert entry is not None
        assert entry["agent"] == "assistant"
        assert entry["sdk_session_id"] == "sdk-1"
        assert "last_active" in entry

    async def test_get_missing_returns_none(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        assert reg.get("nonexistent") is None

    async def test_load_quarantines_structurally_corrupt_entries(self, tmp_path, caplog):
        """#345: load validated only the JSON root, so a non-dict ENTRY (e.g.
        `[]`) was accepted — then register() (deepcopy+.update), the sweepers,
        and every _save_locked (`dict(entry)`) blew up on it, breaking healthy
        entries too. Bad entries must be dropped at load; good ones kept."""
        path = str(tmp_path / "sessions.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "telegram-v2-bad": [],
                "telegram-v2-worse": "nope",
                "telegram-v2-good": {"agent": "assistant", "sdk_session_id": "sid-1"},
            }, fh)
        import logging
        with caplog.at_level(logging.ERROR):
            reg = SessionRegistry(path)
        assert reg.get("telegram-v2-bad") is None
        assert reg.get("telegram-v2-worse") is None
        assert reg.get("telegram-v2-good") == {"agent": "assistant", "sdk_session_id": "sid-1"}
        assert any("quarantin" in r.getMessage() for r in caplog.records)
        # The registry stays fully functional: register over a quarantined key
        # and persist (pre-fix _save_locked crashed on `dict([])`).
        await reg.register(
            "telegram-v2-bad", "assistant", "sid-2", binding_digest=STUB_BINDING_DIGEST,
            speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
        assert reg.get("telegram-v2-bad")["sdk_session_id"] == "sid-2"

    async def test_touch_updates_last_active(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("tg:123", "assistant", "sdk-1", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)

        ts1 = reg.get("tg:123")["last_active"]
        await asyncio.sleep(0.05)
        await reg.touch("tg:123")
        ts2 = reg.get("tg:123")["last_active"]

        assert ts2 >= ts1

    async def test_persistence(self, tmp_path):
        """Data survives across two instances."""
        path = str(tmp_path / "sessions.json")

        r1 = SessionRegistry(path)
        await r1.register("tg:42", "butler", "sdk-2", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)

        r2 = SessionRegistry(path)
        entry = r2.get("tg:42")
        assert entry is not None
        assert entry["agent"] == "butler"

    async def test_remove(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("tg:99", "assistant", "sdk-3", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
        assert reg.get("tg:99") is not None

        await reg.remove("tg:99")
        assert reg.get("tg:99") is None

    async def test_all_entries(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("a", "assistant", "s1", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
        await reg.register("b", "butler", "s2", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)

        entries = reg.all_entries()
        assert set(entries.keys()) == {"a", "b"}


class TestConcurrency:
    async def test_concurrent_register_on_distinct_keys_preserves_all(
        self, tmp_path,
    ):
        """50 concurrent register() calls, distinct keys, all persist."""
        import json

        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)

        async def reg_one(i: int) -> None:
            await reg.register(f"tg:{i}", "assistant", f"sdk-{i}", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)

        await asyncio.gather(*(reg_one(i) for i in range(50)))

        on_disk = json.loads((tmp_path / "sessions.json").read_text())
        assert len(on_disk) == 50
        for i in range(50):
            assert on_disk[f"tg:{i}"]["sdk_session_id"] == f"sdk-{i}"

    async def test_concurrent_register_and_touch_preserve_sdk_session_id(
        self, tmp_path,
    ):
        """register(new sdk_session_id) + touch() on same key: final state keeps sdk_session_id."""
        import json

        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        # Seed an entry so touch() has something to update.
        await reg.register("tg:1", "assistant", "sdk-OLD", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)

        # Now race: register overwrites with sdk-NEW, touch updates last_active.
        await asyncio.gather(
            reg.register("tg:1", "assistant", "sdk-NEW", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV),
            reg.touch("tg:1"),
        )

        on_disk = json.loads((tmp_path / "sessions.json").read_text())
        assert on_disk["tg:1"]["sdk_session_id"] == "sdk-NEW"

    async def test_public_save_acquires_lock_while_internal_save_assumes_held(
        self, tmp_path,
    ):
        """public save() must acquire; _save_locked() must not (caller holds)."""
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("tg:1", "assistant", "sdk-1", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)

        # Holding the lock, _save_locked must succeed without deadlock.
        async with reg._lock:
            await reg._save_locked()

        # Public save() acquires fresh (no caller holds) and completes.
        await reg.save()


# ---------------------------------------------------------------------------
# TestClearSdkSession — 5.8 §3.1
# ---------------------------------------------------------------------------


class TestClearSdkSession:
    """clear_sdk_session drops only the sdk_session_id field; keeps entry."""

    async def test_removes_sdk_session_id_field(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("voice:scope-a", "butler", "sid-123", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
        assert reg.get("voice:scope-a")["sdk_session_id"] == "sid-123"

        await reg.clear_sdk_session("voice:scope-a")

        entry = reg.get("voice:scope-a")
        assert entry is not None
        assert "sdk_session_id" not in entry

    async def test_keeps_last_active_and_agent(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("voice:scope-a", "butler", "sid-123", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
        before = reg.get("voice:scope-a")["last_active"]

        await reg.clear_sdk_session("voice:scope-a")

        entry = reg.get("voice:scope-a")
        assert entry["agent"] == "butler"
        assert entry["last_active"] == before

    async def test_missing_key_is_noop(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)

        # Must not raise.
        await reg.clear_sdk_session("voice:never-registered")

        assert reg.get("voice:never-registered") is None

    async def test_persists_to_disk(self, tmp_path):
        import json
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("voice:scope-a", "butler", "sid-123", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)

        await reg.clear_sdk_session("voice:scope-a")

        # Re-read fresh from disk.
        with open(path) as fh:
            data = json.load(fh)
        assert "voice:scope-a" in data
        assert "sdk_session_id" not in data["voice:scope-a"]
        assert data["voice:scope-a"]["agent"] == "butler"

    async def test_expected_sid_mismatch_declines(self, tmp_path):
        """#349: a clear conditioned on the FAILED sid must leave a newer
        registration (a concurrent turn's session) intact."""
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("voice:scope-a", "butler", "sid-NEW", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)

        await reg.clear_sdk_session("voice:scope-a", expected_sid="sid-OLD")

        assert reg.get("voice:scope-a")["sdk_session_id"] == "sid-NEW"

    async def test_expected_sid_match_clears(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)
        await reg.register("voice:scope-a", "butler", "sid-123", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)

        await reg.clear_sdk_session("voice:scope-a", expected_sid="sid-123")

        entry = reg.get("voice:scope-a")
        assert entry is not None
        assert "sdk_session_id" not in entry

    async def test_expected_sid_on_missing_key_is_noop(self, tmp_path):
        path = str(tmp_path / "sessions.json")
        reg = SessionRegistry(path)

        await reg.clear_sdk_session("voice:none", expected_sid="sid-1")

        assert reg.get("voice:none") is None


# ---------------------------------------------------------------------------
# TestCrashSafety — H12: atomic write + tolerant load
# ---------------------------------------------------------------------------


class TestCrashSafety:
    """sessions.json must survive a crash mid-write and a corrupt file on boot."""

    async def test_corrupt_file_starts_empty_and_quarantines(self, tmp_path):
        """A truncated sessions.json (power loss / OOM-kill mid-write) must not
        raise on load — it is quarantined to .corrupt and the registry starts
        empty instead of crash-looping the add-on."""
        p = tmp_path / "sessions.json"
        p.write_text('{"telegram-123": {"agent": "casa", "sdk_ses', encoding="utf-8")

        reg = SessionRegistry(str(p))  # must NOT raise

        assert reg.all_entries() == {}
        assert (tmp_path / "sessions.json.corrupt").exists()

    async def test_non_dict_json_is_quarantined(self, tmp_path):
        """A syntactically valid but wrong-shape file (e.g. a JSON list) is
        also treated as corrupt rather than loaded blindly."""
        p = tmp_path / "sessions.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")

        reg = SessionRegistry(str(p))

        assert reg.all_entries() == {}
        assert (tmp_path / "sessions.json.corrupt").exists()

    async def test_crash_between_tempwrite_and_replace_keeps_original(
        self, tmp_path, monkeypatch,
    ):
        """Simulate a crash BETWEEN the temp-file write and os.replace: the
        live sessions.json must keep its previous valid contents (the old
        truncate-in-place open('w') left it empty)."""
        import atomic_io

        p = tmp_path / "sessions.json"
        reg = SessionRegistry(str(p))
        await reg.register("tg:1", "assistant", "sdk-OLD", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
        assert json.loads(p.read_text(encoding="utf-8"))["tg:1"]["sdk_session_id"] == "sdk-OLD"

        def boom(*args, **kwargs):
            raise RuntimeError("simulated crash before replace")

        monkeypatch.setattr(atomic_io.os, "replace", boom)
        with pytest.raises(RuntimeError):
            reg._write({"tg:1": {"agent": "assistant", "sdk_session_id": "sdk-NEW"}})

        # Original intact, not truncated.
        on_disk = json.loads(p.read_text(encoding="utf-8"))
        assert on_disk["tg:1"]["sdk_session_id"] == "sdk-OLD"
        # No orphaned temp sidecar.
        assert [f for f in os.listdir(tmp_path) if f != "sessions.json"] == []


class TestSidGuardedRemove:
    """#317: reset_channel's trailing remove() must not erase a session a
    concurrent follow-up turn registered after the reset snapshot was taken."""

    async def test_remove_with_matching_expected_sid_removes(self, tmp_path):
        reg = SessionRegistry(str(tmp_path / "s.json"))
        await reg.register("telegram-r1", "assistant", "sid-old", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
        await reg.remove("telegram-r1", expected_sid="sid-old")
        assert reg.get("telegram-r1") is None

    async def test_remove_with_stale_expected_sid_spares_newer_session(self, tmp_path):
        reg = SessionRegistry(str(tmp_path / "s.json"))
        await reg.register("telegram-r1", "assistant", "sid-new", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
        # A reset that snapshotted sid-old must leave the newer registration.
        await reg.remove("telegram-r1", expected_sid="sid-old")
        assert reg.get("telegram-r1") is not None
        assert reg.get("telegram-r1")["sdk_session_id"] == "sid-new"

    async def test_remove_without_expected_sid_stays_unconditional(self, tmp_path):
        reg = SessionRegistry(str(tmp_path / "s.json"))
        await reg.register("telegram-r1", "assistant", "sid-x", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
        await reg.remove("telegram-r1")
        assert reg.get("telegram-r1") is None

    async def test_remove_with_expected_none_guards_on_sidless_entry(self, tmp_path):
        """#353: expected_sid=None means the caller snapshotted a SID-LESS
        entry — the removal declines once any real session registers."""
        reg = SessionRegistry(str(tmp_path / "s.json"))
        reg._data["telegram-r1"] = {"agent": "assistant"}  # malformed: no sid
        await reg.remove("telegram-r1", expected_sid=None)
        assert reg.get("telegram-r1") is None  # sid-less snapshot matches

        await reg.register("telegram-r2", "assistant", "sid-live", binding_digest=STUB_BINDING_DIGEST, speaker_provenance=STUB_SPEAKER_PROV, user_provenance=STUB_USER_PROV)
        await reg.remove("telegram-r2", expected_sid=None)
        assert reg.get("telegram-r2") is not None  # real session spared
