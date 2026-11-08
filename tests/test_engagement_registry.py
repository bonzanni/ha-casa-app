"""Tests for engagement_registry.py — engagement lifecycle + tombstone."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


class TestEngagementRecord:
    def test_dataclass_shape(self):
        from engagement_registry import EngagementRecord

        rec = EngagementRecord(
            id="e1",
            kind="specialist",
            role_or_type="finance",
            driver="in_casa",
            status="active",
            topic_id=12345,
            started_at=1000.0,
            last_user_turn_ts=1000.0,
            completed_at=None,
            sdk_session_id=None,
            origin={"role": "assistant", "channel": "telegram"},
            task="Plan Q2 invoicing",
        )
        assert rec.id == "e1"
        assert rec.kind == "specialist"
        assert rec.topic_id == 12345
        assert rec.sdk_session_id is None
        assert rec.origin["role"] == "assistant"


class TestRegistryInitAndLoad:
    async def test_load_missing_tombstone_is_empty(self, tmp_path):
        from engagement_registry import EngagementRegistry

        tombstone = tmp_path / "engagements.json"
        reg = EngagementRegistry(tombstone_path=str(tombstone))
        await reg.load()
        assert reg.active_and_idle() == []

    async def test_load_reads_active_records(self, tmp_path):
        from engagement_registry import EngagementRegistry

        tombstone = tmp_path / "engagements.json"
        tombstone.write_text(json.dumps([
            {
                "id": "e1",
                "kind": "specialist",
                "role_or_type": "finance",
                "driver": "in_casa",
                "status": "active",
                "topic_id": 42,
                "started_at": 1000.0,
                "last_user_turn_ts": 1000.0,
                "last_idle_reminder_ts": 0.0,
                "completed_at": None,
                "sdk_session_id": None,
                "origin": {"role": "assistant"},
                "task": "x",
            }
        ]))
        reg = EngagementRegistry(tombstone_path=str(tombstone))
        await reg.load()
        records = reg.active_and_idle()
        assert len(records) == 1
        assert records[0].id == "e1"
        assert records[0].topic_id == 42

    async def test_load_corrupt_tombstone_truncates_and_returns_empty(self, tmp_path, caplog):
        from engagement_registry import EngagementRegistry

        tombstone = tmp_path / "engagements.json"
        tombstone.write_text("{not json")
        reg = EngagementRegistry(tombstone_path=str(tombstone))
        await reg.load()
        assert reg.active_and_idle() == []
        # File truncated to []
        assert json.loads(tombstone.read_text()) == []
        assert any("corrupt" in r.message.lower() for r in caplog.records)


class TestRegistryCreate:
    async def test_create_assigns_uuid_and_indexes_topic(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            kind="specialist",
            role_or_type="finance",
            driver="in_casa",
            task="task text",
            origin={"role": "assistant"},
            topic_id=777,
        )
        assert rec.id and len(rec.id) >= 32
        assert rec.status == "active"
        assert reg.get(rec.id) is rec
        assert reg.by_topic_id(777) is rec
        # Tombstone written
        assert (tmp_path / "e.json").exists()

    async def test_create_without_topic_id_still_writes_tombstone(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={}, topic_id=None,
        )
        assert rec.topic_id is None
        assert reg.by_topic_id(0) is None

    async def test_create_strips_non_persistable_origin_from_tombstone(self, tmp_path):
        """Task 10: origin snapshots may carry LIVE, non-JSON objects (a
        SpeakerProvenance dataclass, the voice handoff reservation) for
        in-process reads only. Persisting them would crash json.dumps — the
        tombstone must strip them, while the in-memory record keeps them live."""
        from engagement_registry import EngagementRegistry
        from personality_types import SpeakerProvenance

        prov = SpeakerProvenance(
            speaker_kind="resident", role_id="resident:butler", persona_id="casa/tina",
            persona_version="0.1.0", display_name="Tina", binding_digest="sha256:" + "a" * 64,
        )

        class _LiveReservation:
            def reserve(self): ...
            def release(self): ...
            def commit(self): ...

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        # create() writes the tombstone synchronously — a live-object origin must
        # not raise here.
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa", task="t",
            origin={
                "role": "resident:butler", "channel": "telegram",
                "speaker_provenance": prov,
                "_voice_handoff_reservation": _LiveReservation(),
                "voice_route_capabilities": frozenset({"cap"}),
            },
            topic_id=None,
        )
        # In-memory record keeps the live objects for in-process use.
        assert rec.origin["speaker_provenance"] is prov
        # Persisted tombstone dropped them and is valid JSON.
        persisted = json.loads((tmp_path / "e.json").read_text())
        row = next(r for r in persisted if r["id"] == rec.id)
        assert "speaker_provenance" not in row["origin"]
        assert "_voice_handoff_reservation" not in row["origin"]
        assert "voice_route_capabilities" not in row["origin"]
        assert row["origin"]["channel"] == "telegram"


class TestRegistryStateTransitions:
    async def test_mark_completed_persists_terminal_tombstone(self, tmp_path):
        """D-4: terminal records STAY on disk as tombstones (they used to be
        dropped, so the duplicate-task guard forgot them across restarts and
        the file never matched the 'tombstone' name)."""
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        await reg.mark_completed(rec.id, completed_at=time.time())
        assert rec.status == "completed"
        rows = json.loads((tmp_path / "e.json").read_text())
        assert [r["id"] for r in rows] == [rec.id]
        assert rows[0]["status"] == "completed"
        # Still in memory for short-term lookups (by_topic_id after /cancel)
        assert reg.get(rec.id) is rec

    async def test_terminal_tombstones_pruned_after_retention(self, tmp_path):
        """D-4: terminal tombstones age out of the file (30d) so it can't
        grow unboundedly; in-flight records are never pruned."""
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        old = await reg.create("executor", "configurator", "in_casa", "t1", {}, 1)
        await reg.mark_completed(old.id, completed_at=time.time() - 31 * 86400)
        # An in_casa record ages out only once the transcript reaper is done.
        await reg.mark_transcripts_reaped([old.id])
        fresh = await reg.create("specialist", "finance", "in_casa", "t2", {}, 2)
        rows = json.loads((tmp_path / "e.json").read_text())
        ids = [r["id"] for r in rows]
        assert fresh.id in ids
        assert old.id not in ids, "31d-old terminal tombstone must be pruned"

    async def test_load_reconciles_active_to_idle(self, tmp_path):
        """D-4 boot reconcile: a record loaded as 'active' cannot have a live
        driver (the process that ran it died with the old container) — load()
        must flip it to idle so it stops claiming to run forever."""
        from engagement_registry import EngagementRegistry

        base = {
            "kind": "executor", "role_or_type": "configurator",
            "driver": "in_casa", "started_at": 1000.0,
            "last_user_turn_ts": 1000.0, "origin": {}, "task": "t",
        }
        tombstone = tmp_path / "engagements.json"
        tombstone.write_text(json.dumps([
            {**base, "id": "e-active", "status": "active", "topic_id": 1},
            {**base, "id": "e-idle", "status": "idle", "topic_id": 2},
            # recent completed_at so the 30d tombstone-prune keeps it.
            {**base, "id": "e-done", "status": "completed", "topic_id": 3,
             "completed_at": time.time()},
        ]))
        reg = EngagementRegistry(tombstone_path=str(tombstone))
        await reg.load()
        assert reg.get("e-active").status == "idle"
        assert reg.get("e-idle").status == "idle"
        assert reg.get("e-done").status == "completed"
        assert {r.id for r in reg.active_and_idle()} == {"e-active", "e-idle"}
        # v0.69.6: the reconcile must be PERSISTED — the disk-reading auditor
        # (invariant E) must not keep seeing the stale "active" until some
        # later mutation happens to rewrite the file.
        on_disk = {r["id"]: r["status"] for r in json.loads(tombstone.read_text())}
        assert on_disk["e-active"] == "idle", "boot reconcile must persist to disk"
        assert on_disk["e-idle"] == "idle"
        assert on_disk["e-done"] == "completed"

    async def test_load_without_reconcile_does_not_rewrite(self, tmp_path):
        """No active records → nothing to reconcile → load() must not rewrite
        the file (avoid needless boot churn / mtime bump)."""
        from engagement_registry import EngagementRegistry

        tombstone = tmp_path / "engagements.json"
        tombstone.write_text(json.dumps([{
            "id": "e-idle", "kind": "specialist", "role_or_type": "finance",
            "driver": "in_casa", "status": "idle", "topic_id": 2,
            "started_at": 1000.0, "last_user_turn_ts": 1000.0,
            "origin": {}, "task": "t",
        }]))
        before = tombstone.read_text()
        reg = EngagementRegistry(tombstone_path=str(tombstone))
        await reg.load()
        assert tombstone.read_text() == before, "load() rewrote with nothing to reconcile"

    async def test_recent_for_origin_survives_restart_via_tombstone(self, tmp_path):
        """D-4: the P32 duplicate-task guard reads recent engagements from
        memory; persisting terminal tombstones makes it hold across restarts."""
        from engagement_registry import EngagementRegistry

        path = str(tmp_path / "e.json")
        reg1 = EngagementRegistry(tombstone_path=path)
        rec = await reg1.create(
            "executor", "configurator", "in_casa", "install plugin X",
            {"channel": "telegram", "chat_id": "123"}, 1,
        )
        await reg1.mark_completed(rec.id, completed_at=time.time())

        reg2 = EngagementRegistry(tombstone_path=path)
        await reg2.load()
        found = reg2.recent_for_origin(
            channel="telegram", chat_id="123", max_age_s=3600,
        )
        assert found is not None and found.id == rec.id

    async def test_mark_idle_and_back_to_active(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        await reg.mark_idle(rec.id)
        assert rec.status == "idle"
        await reg.update_user_turn(rec.id, ts=3000.0)
        assert rec.status == "active"
        assert rec.last_user_turn_ts == 3000.0

    async def test_persist_session_id(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        await reg.persist_session_id(rec.id, "sess-abc")
        assert rec.sdk_session_id == "sess-abc"

    async def test_persist_session_id_failure_raises_and_rolls_back(
        self, tmp_path, monkeypatch,
    ):
        """#302: a swallowed tombstone-write failure made the in-memory record
        look current while the durable registry never received the session ID
        — after a restart the engagement could not resume. The write is strict
        and the record mutation rolls back so the driver's same-sid retry
        guard fires again."""
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)

        def boom(snapshot):
            raise OSError("disk full")

        monkeypatch.setattr(reg, "_write_tombstone", boom)
        with pytest.raises(OSError):
            await reg.persist_session_id(rec.id, "sess-abc")
        assert rec.sdk_session_id is None

    async def test_mark_error_captures_kind(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        await reg.mark_error(rec.id, kind="resume_failed", message="SDK rotated")
        assert rec.status == "error"
        assert rec.origin.get("error_kind") == "resume_failed"
        assert rec.origin.get("error_message") == "SDK rotated"

    async def test_try_transition_terminal_wins_once(self, tmp_path):
        """L75/L24: try_transition_terminal is the atomic gate — the first
        caller wins and flips the record; every subsequent caller (even
        with a different outcome) is refused and the status is untouched."""
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)

        won = await reg.try_transition_terminal(rec.id, "cancelled")
        assert won is True
        assert rec.status == "cancelled"

        # A second caller (e.g. emit_completion resuming after the race)
        # must NOT win and must NOT overwrite the winning outcome.
        won2 = await reg.try_transition_terminal(rec.id, "completed", completed_at=999.0)
        assert won2 is False
        assert rec.status == "cancelled"

    async def test_try_transition_terminal_missing_record_returns_false(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        assert await reg.try_transition_terminal("ghost", "completed") is False

    async def test_try_transition_terminal_error_sets_origin(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        won = await reg.try_transition_terminal(
            rec.id, "error", error_kind="emit_completion_error", error_message="boom",
        )
        assert won is True
        assert rec.status == "error"
        assert rec.origin.get("error_kind") == "emit_completion_error"
        assert rec.origin.get("error_message") == "boom"

    async def test_strict_transition_rolls_back_full_field_on_persist_failure(
        self, tmp_path,
    ):
        """Pins INV-ENG-002. Red case demonstrated: removing _restore() from
        the tombstone-write exception handler leaves the in-memory record
        terminal after the raise and fails this test."""
        """v0.79.0 (§3, Sol r7-2): the STRICT terminal transition snapshots
        EVERY mutated field (status, completed_at, error metadata) and, on a
        tombstone-write failure, restores the FULL snapshot and re-raises — no
        closed topic with a torn (memory ≠ disk) terminal record."""
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        # Baseline: an active record with no error metadata.
        assert rec.status == "active"
        assert "error_kind" not in rec.origin

        # Fail the tombstone write only for the strict transition.
        import unittest.mock as _mock
        with _mock.patch.object(
            reg, "_write_tombstone", side_effect=OSError("disk full"),
        ):
            with pytest.raises(OSError):
                await reg.try_transition_terminal(
                    rec.id, "error", error_kind="k", error_message="m",
                    strict=True,
                )
        # FULL-FIELD rollback: status, completed_at AND the error metadata that
        # the transition added are all reverted (the added keys are removed,
        # not left as None).
        assert rec.status == "active"
        assert rec.completed_at is None
        assert "error_kind" not in rec.origin
        assert "error_message" not in rec.origin

    async def test_strict_transition_cancel_during_persist_completes_write(
        self, tmp_path,
    ):
        """v0.79.0 (§3, Sol r7-2): cancelling the CALLER during the strict
        transition's ``to_thread`` persist cannot tear memory from disk — the
        shielded mutate+persist runs to completion before the cancel is honored.
        """
        import asyncio
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)

        async def _driver():
            await reg.try_transition_terminal(rec.id, "completed", strict=True)

        task = asyncio.ensure_future(_driver())
        await asyncio.sleep(0)          # let it enter the shielded persist
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The durable write completed under the shield: memory is terminal AND
        # a fresh registry load sees the same terminal status.
        assert rec.status == "completed"
        reg2 = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        await reg2.load()
        assert reg2.get(rec.id).status == "completed"

    async def test_non_strict_transition_swallows_persist_failure(self, tmp_path):
        """The historical non-strict path keeps best-effort semantics: a
        tombstone-write failure is swallowed and the in-memory flip stands."""
        import unittest.mock as _mock
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        with _mock.patch.object(
            reg, "_write_tombstone", side_effect=OSError("disk full"),
        ):
            won = await reg.try_transition_terminal(rec.id, "cancelled")
        assert won is True
        assert rec.status == "cancelled"

    async def test_terminal_records_accessor(self, tmp_path):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        live = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)
        gone = await reg.create("specialist", "ops", "in_casa", "t", {}, 2)
        await reg.try_transition_terminal(gone.id, "completed")
        ids = {r.id for r in reg.terminal_records()}
        assert gone.id in ids and live.id not in ids

    async def test_a_row_with_retired_fields_loads_and_sheds_them(
            self, tmp_path):
        """A tombstone written before the idle reminders and the executor
        epoch were removed still carries ``last_idle_reminder_ts`` and
        ``procedural_epoch``. It loads, and the next write drops both."""
        from engagement_registry import EngagementRegistry

        path = tmp_path / "engagements.json"
        path.write_text(json.dumps([{
            "id": "b" * 32, "kind": "executor",
            "role_or_type": "configurator", "driver": "in_casa",
            "status": "idle", "topic_id": 7, "started_at": 1000.0,
            "last_user_turn_ts": 1000.0, "last_idle_reminder_ts": 2000.0,
            "procedural_epoch": "abc123", "completed_at": None,
            "sdk_session_id": "s-1", "origin": {}, "task": "legacy",
        }]))
        reg = EngagementRegistry(tombstone_path=str(path))
        await reg.load()
        assert reg.get("b" * 32) is not None
        await reg.update_user_turn("b" * 32, 3000.0)
        row = json.loads(path.read_text())[0]
        assert "last_idle_reminder_ts" not in row
        assert "procedural_epoch" not in row


class TestChannelStateFields:
    """E-12 (v0.37.0): EngagementRecord carries channel-side state."""

    async def test_record_has_channel_fields_with_defaults(self, tmp_path):
        from engagement_registry import EngagementRegistry
        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "engagements.json"),
        )
        rec = await reg.create(
            kind="executor", role_or_type="configurator",
            driver="in_casa", task="t", origin={}, topic_id=42,
        )
        assert rec.current_state_emoji is None

    async def test_set_channel_state_persists_each_field(self, tmp_path):
        from engagement_registry import EngagementRegistry
        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "engagements.json"),
        )
        rec = await reg.create(
            kind="executor", role_or_type="configurator",
            driver="in_casa", task="t", origin={}, topic_id=42,
        )
        await reg.set_channel_state(rec.id, current_state_emoji="🟢")
        assert rec.current_state_emoji == "🟢"

    async def test_set_channel_state_leaves_omitted_fields_untouched(self, tmp_path):
        from engagement_registry import EngagementRegistry
        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "engagements.json"),
        )
        rec = await reg.create(
            kind="executor", role_or_type="configurator",
            driver="in_casa", task="t", origin={}, topic_id=42,
        )
        await reg.set_channel_state(rec.id, current_state_emoji="🟡")
        await reg.set_channel_state(rec.id)
        assert rec.current_state_emoji == "🟡"

    async def test_channel_fields_round_trip_through_tombstone(self, tmp_path):
        from engagement_registry import EngagementRegistry
        tombstone = tmp_path / "engagements.json"
        reg = EngagementRegistry(tombstone_path=str(tombstone))
        rec = await reg.create(
            kind="executor", role_or_type="configurator",
            driver="in_casa", task="t", origin={}, topic_id=42,
        )
        await reg.set_channel_state(rec.id, current_state_emoji="🟢")

        reg2 = EngagementRegistry(tombstone_path=str(tombstone))
        await reg2.load()
        rec2 = reg2.get(rec.id)
        assert rec2 is not None
        assert rec2.current_state_emoji == "🟢"

    async def test_set_channel_state_unknown_engagement_noop(self, tmp_path):
        from engagement_registry import EngagementRegistry
        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "engagements.json"),
        )
        # Should not raise.
        await reg.set_channel_state("does-not-exist", current_state_emoji="🟢")


@pytest.fixture
def registry(tmp_path):
    from engagement_registry import EngagementRegistry
    return EngagementRegistry(
        tombstone_path=str(tmp_path / "engagements.json"),
    )


class TestToolsAllowedField:
    async def test_default_empty_tuple(self, registry):
        rec = await registry.create(
            kind="executor",
            role_or_type="configurator",
            driver="in_casa",
            task="probe",
            origin={},
            topic_id=None,
        )
        assert rec.tools_allowed == ()

    async def test_create_accepts_tools_allowed(self, registry):
        allow = ("Bash(npm*)", "Read", "Edit(/data/engagements/*)")
        rec = await registry.create(
            kind="executor",
            role_or_type="configurator",
            driver="in_casa",
            task="probe",
            origin={},
            topic_id=None,
            tools_allowed=allow,
        )
        assert rec.tools_allowed == allow

    async def test_tombstone_round_trip(self, tmp_path):
        from engagement_registry import EngagementRegistry
        path = str(tmp_path / "engagements.json")
        reg1 = EngagementRegistry(tombstone_path=path)
        await reg1.load()
        await reg1.create(
            kind="executor",
            role_or_type="configurator",
            driver="in_casa",
            task="t",
            origin={},
            topic_id=None,
            tools_allowed=("Bash(npm*)", "Read"),
        )
        # Fresh registry reading the same tombstone:
        reg2 = EngagementRegistry(tombstone_path=path)
        await reg2.load()
        ids = list(reg2._records)
        assert len(ids) == 1
        rec = reg2._records[ids[0]]
        assert rec.tools_allowed == ("Bash(npm*)", "Read")

    async def test_pre_v0_37_2_tombstone_loads_with_empty(self, tmp_path):
        """Back-compat: tombstones written before v0.37.2 lack the field."""
        import json
        from engagement_registry import EngagementRegistry
        path = tmp_path / "engagements.json"
        path.write_text(json.dumps([{
            "id": "a" * 32,
            "kind": "executor",
            "role_or_type": "configurator",
            "driver": "in_casa",
            "status": "active",
            "topic_id": 42,
            "started_at": 1700000000.0,
            "last_user_turn_ts": 1700000000.0,
            "last_idle_reminder_ts": 0.0,
            "completed_at": None,
            "sdk_session_id": None,
            "origin": {},
            "task": "legacy",
        }]))
        reg = EngagementRegistry(tombstone_path=str(path))
        await reg.load()
        rec = reg._records["a" * 32]
        assert rec.tools_allowed == ()


class TestRecentForOrigin:
    """P32 (v0.37.10): query the most-recent engagement for a given
    (channel, chat_id) within a time window. Powers the duplicate-task
    guard at the engage_executor MCP call site — see
    docs/bug-review-2026-05-14-exploration6.md::O-6.
    """

    async def test_returns_most_recent_within_window(self, registry):
        older = await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="old", origin={"channel": "telegram", "chat_id": "c1"},
            topic_id=10,
        )
        newer = await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="new", origin={"channel": "telegram", "chat_id": "c1"},
            topic_id=11,
        )
        result = registry.recent_for_origin(
            channel="telegram", chat_id="c1", max_age_s=60.0,
        )
        assert result is newer
        assert result.id != older.id

    async def test_returns_none_for_no_match(self, registry):
        await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={"channel": "telegram", "chat_id": "c1"},
            topic_id=10,
        )
        assert registry.recent_for_origin(
            channel="telegram", chat_id="other", max_age_s=60.0,
        ) is None
        assert registry.recent_for_origin(
            channel="discord", chat_id="c1", max_age_s=60.0,
        ) is None

    async def test_excludes_older_than_max_age(self, registry):
        import time as time_mod
        rec = await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={"channel": "telegram", "chat_id": "c1"},
            topic_id=10,
        )
        # Backdate the engagement past the window.
        rec.started_at = time_mod.time() - 300.0
        assert registry.recent_for_origin(
            channel="telegram", chat_id="c1", max_age_s=60.0,
        ) is None
        # But a wider window still returns it.
        assert registry.recent_for_origin(
            channel="telegram", chat_id="c1", max_age_s=600.0,
        ) is rec

    async def test_includes_terminal_status_engagements(self, registry):
        """Completed / cancelled / error engagements stay in memory
        post-finalize (the tombstone drops them but ``_records`` retains).
        The duplicate-task guard must still see them so back-to-back
        spawns where the prior just terminated are caught."""
        rec = await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={"channel": "telegram", "chat_id": "c1"},
            topic_id=10,
        )
        await registry.mark_completed(rec.id, completed_at=rec.started_at + 1)
        # Even completed, recent_for_origin returns it for the duplicate
        # guard (we want to compare against the LAST task spawned, not the
        # last ACTIVE task).
        assert registry.recent_for_origin(
            channel="telegram", chat_id="c1", max_age_s=60.0,
        ) is rec

    async def test_coerces_chat_id_to_string(self, registry):
        """``EngagementRecord.origin['chat_id']`` may be int or str
        depending on the channel adapter. The query must coerce for a
        consistent compare so a telegram int chat_id matches a str."""
        await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={"channel": "telegram", "chat_id": 42},
            topic_id=10,
        )
        assert registry.recent_for_origin(
            channel="telegram", chat_id="42", max_age_s=60.0,
        ) is not None


# ---------------------------------------------------------------------------
# TestTombstoneAtomicity — M15: crash mid-write must not corrupt the tombstone
# ---------------------------------------------------------------------------


class TestTombstoneAtomicity:
    async def test_crash_between_tempwrite_and_replace_keeps_tombstone(
        self, tmp_path, monkeypatch,
    ):
        """A crash BETWEEN the temp write and os.replace must leave the prior
        engagements.json intact (not truncated), preserving in-flight state."""
        import atomic_io
        from engagement_registry import EngagementRegistry

        tombstone = tmp_path / "engagements.json"
        reg = EngagementRegistry(tombstone_path=str(tombstone))
        rec = await reg.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="keep me", origin={"channel": "telegram", "chat_id": 1},
            topic_id=7,
        )
        first = json.loads(tombstone.read_text(encoding="utf-8"))
        assert first[0]["id"] == rec.id

        def boom(*args, **kwargs):
            raise RuntimeError("simulated crash before replace")

        monkeypatch.setattr(atomic_io.os, "replace", boom)
        # _write_tombstone_locked swallows the exception (logs a warning), so
        # the mutation call itself must not raise — but disk must be untouched.
        await reg.update_user_turn(rec.id, ts=time.time())

        on_disk = json.loads(tombstone.read_text(encoding="utf-8"))
        assert on_disk == first  # prior tombstone intact, not truncated
        import os as _os
        assert [f for f in _os.listdir(tmp_path) if f != "engagements.json"] == []


# ---------------------------------------------------------------------------
# TestInteractionState — W2/Sol B9 (Task 7): observational turn-taking.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# v0.79.0 (§4) — persisted question numbering + open-question ledger
# ---------------------------------------------------------------------------


class TestTerminalGuards:
    """#326: non-atomic status writes — mark_idle/mark_error (and the other
    direct mark_* mutators) must never overwrite a concurrently committed
    terminal status, and create() must not report success after a failed
    durable write."""

    async def test_mark_idle_never_overwrites_terminal(self, registry):
        rec = await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={}, topic_id=1,
        )
        assert await registry.try_transition_terminal(rec.id, "cancelled")
        await registry.mark_idle(rec.id)
        assert registry.get(rec.id).status == "cancelled"

    async def test_mark_error_loses_to_terminal_winner(self, registry):
        rec = await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={}, topic_id=1,
        )
        assert await registry.try_transition_terminal(rec.id, "cancelled")
        won = await registry.mark_error(rec.id, kind="resume_failed", message="x")
        assert won is False
        assert registry.get(rec.id).status == "cancelled"
        assert "error_kind" not in registry.get(rec.id).origin

    async def test_mark_error_wins_on_live_record(self, registry):
        rec = await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={}, topic_id=1,
        )
        won = await registry.mark_error(rec.id, kind="boom", message="m")
        assert won is True
        assert registry.get(rec.id).status == "error"
        assert registry.get(rec.id).origin["error_kind"] == "boom"

    async def test_mark_completed_and_cancelled_respect_terminal(self, registry):
        rec = await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={}, topic_id=1,
        )
        assert await registry.try_transition_terminal(rec.id, "cancelled")
        await registry.mark_completed(rec.id, completed_at=123.0)
        assert registry.get(rec.id).status == "cancelled"
        rec2 = await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={}, topic_id=2,
        )
        assert await registry.try_transition_terminal(rec2.id, "completed")
        await registry.mark_cancelled(rec2.id)
        assert registry.get(rec2.id).status == "completed"

    async def test_create_persist_failure_raises_and_rolls_back(
        self, registry, monkeypatch,
    ):
        """create() used the best-effort tombstone writer, so a disk failure
        produced a running engagement with no crash-recovery record. It must
        now propagate the failure and leave no ghost record behind."""
        import engagement_registry as er

        def _boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(er, "atomic_write_json", _boom)
        with pytest.raises(OSError):
            await registry.create(
                kind="executor", role_or_type="configurator", driver="in_casa",
                task="t", origin={}, topic_id=99,
            )
        assert registry.active_and_idle() == []
        assert registry.by_topic_id(99) is None

    async def test_sweep_does_not_resurrect_concurrently_finalized(self, registry):
        """#326: the idle sweep awaits driver.cancel(); a finalizer that commits
        a terminal status during that await must not be flipped back to idle."""
        rec = await registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={}, topic_id=5,
        )
        now = time.time()
        rec.last_user_turn_ts = now - 90000  # > _SESSION_SUSPEND_IDLE_S

        class Driver:
            def is_alive(self, r):
                return True

            def get_session_id(self, r):
                return "sid-1"

            async def cancel(self, r):
                # Concurrent finalizer wins while the sweep awaits us.
                assert await registry.try_transition_terminal(rec.id, "cancelled")

        await registry.sweep_idle_and_suspend(driver=Driver(), now_override=now)
        assert registry.get(rec.id).status == "cancelled"


class TestResumeFailCountPersistence:
    async def test_set_resume_fail_count_persists_across_reload(self, tmp_path):
        """#326 (low): the two-strike resume counter must survive a restart —
        an unrecoverable engagement must not dodge the error transition by
        resetting its counter on every boot."""
        from engagement_registry import EngagementRegistry

        path = str(tmp_path / "e.json")
        reg = EngagementRegistry(tombstone_path=path)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={}, topic_id=3,
        )
        await reg.set_resume_fail_count(rec.id, 1)
        assert reg.get(rec.id).origin["_resume_fail_count"] == 1

        reg2 = EngagementRegistry(tombstone_path=path)
        await reg2.load()
        assert reg2.get(rec.id).origin.get("_resume_fail_count") == 1

    async def test_create_cancelled_mid_persist_leaves_no_ghost(
        self, registry, monkeypatch,
    ):
        """Terra r2-1: a caller cancelled while create()'s persist commits must
        not strand a durable active record with no driver — the insert is
        compensated (memory rolled back, removal persisted) under the lock."""
        import threading

        import engagement_registry as er

        write_entered = threading.Event()
        release_write = threading.Event()
        real_write = er.atomic_write_json
        calls = []

        def gated_write(path, data, **kw):
            calls.append(1)
            if len(calls) == 1:
                write_entered.set()
                release_write.wait(timeout=5)
            return real_write(path, data, **kw)

        monkeypatch.setattr(er, "atomic_write_json", gated_write)

        create_task = asyncio.ensure_future(registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={}, topic_id=44,
        ))
        await asyncio.to_thread(write_entered.wait, 5)
        create_task.cancel()
        release_write.set()
        with pytest.raises(asyncio.CancelledError):
            await create_task

        assert registry.active_and_idle() == []
        assert registry.by_topic_id(44) is None
        # The compensating write persisted the removal: no row on disk.
        rows = json.loads(Path(registry._tombstone_path).read_text())
        assert rows == []

    async def test_create_double_cancellation_still_compensates(
        self, registry, monkeypatch,
    ):
        """Terra/Sol r3: a SECOND cancellation arriving while the first one is
        waiting for the in-flight persist to settle must not abandon the
        commit — the persist and the compensation both run to completion
        before the cancellation propagates."""
        import threading

        import engagement_registry as er

        write_entered = threading.Event()
        release_write = threading.Event()
        real_write = er.atomic_write_json
        calls = []

        def gated_write(path, data, **kw):
            calls.append(1)
            if len(calls) == 1:
                write_entered.set()
                release_write.wait(timeout=5)
            return real_write(path, data, **kw)

        monkeypatch.setattr(er, "atomic_write_json", gated_write)

        create_task = asyncio.ensure_future(registry.create(
            kind="executor", role_or_type="configurator", driver="in_casa",
            task="t", origin={}, topic_id=45,
        ))
        await asyncio.to_thread(write_entered.wait, 5)
        create_task.cancel()
        await asyncio.sleep(0)   # now blocked settling the in-flight persist
        create_task.cancel()     # second cancellation mid-settle
        await asyncio.sleep(0)
        release_write.set()
        with pytest.raises(asyncio.CancelledError):
            await create_task

        assert registry.active_and_idle() == []
        assert registry.by_topic_id(45) is None
        rows = json.loads(Path(registry._tombstone_path).read_text())
        assert rows == []


class TestPersistSessionIdCancellationSafety:
    """Sol r4 (#302): a caller cancelled mid-write must not tear the
    in-memory retry guard from disk — the write settles first, then memory
    reflects the settled outcome, then the cancellation propagates."""

    async def test_cancelled_failed_write_rolls_back(
        self, tmp_path, monkeypatch,
    ):
        import threading
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)

        release = threading.Event()

        def slow_fail(snapshot):
            release.wait(2)
            raise OSError("disk full")

        monkeypatch.setattr(reg, "_write_tombstone", slow_fail)
        t = asyncio.create_task(reg.persist_session_id(rec.id, "sess-abc"))
        await asyncio.sleep(0.05)          # inside the to_thread write
        t.cancel()
        await asyncio.sleep(0.05)          # cancellation delivered pre-settle
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await t
        # The write CONFIRMED-failed → the retry guard must be re-armed.
        assert rec.sdk_session_id is None

    async def test_cancelled_committed_write_keeps_sid(
        self, tmp_path, monkeypatch,
    ):
        import threading
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"))
        rec = await reg.create("specialist", "finance", "in_casa", "t", {}, 1)

        release = threading.Event()
        real_write = reg._write_tombstone

        def slow_commit(snapshot):
            release.wait(2)
            real_write(snapshot)

        monkeypatch.setattr(reg, "_write_tombstone", slow_commit)
        t = asyncio.create_task(reg.persist_session_id(rec.id, "sess-abc"))
        await asyncio.sleep(0.05)
        t.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await t
        # The write settled COMMITTED → memory matches disk (no rollback).
        assert rec.sdk_session_id == "sess-abc"


class TestChannelStateStrictAndInitial:
    """#529: the topic-state sentinel scheme needs (a) a STRICT
    ``set_channel_state`` whose persistence failure propagates and rolls the
    in-memory field back (memory must never claim what disk refused — the
    telegram no-op guard reads the live record), (b) a tombstone write that
    SETTLES before a cancellation propagates (a detached ``to_thread`` write
    could land a stale snapshot over a newer one after the lock released),
    and (c) launch-time initial-emoji writers that cannot clobber a settled
    terminal emoji or an in-flight paint's sentinel."""

    async def _mk(self, tmp_path):
        from engagement_registry import EngagementRegistry
        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "engagements.json"),
        )
        rec = await reg.create(
            kind="executor", role_or_type="configurator",
            driver="in_casa", task="t", origin={}, topic_id=42,
        )
        return reg, rec

    async def test_strict_write_failure_propagates_and_rolls_back(
            self, tmp_path, monkeypatch):
        reg, rec = await self._mk(tmp_path)
        await reg.set_channel_state(rec.id, current_state_emoji="🟢")

        def boom(snapshot):
            raise OSError("disk full")

        monkeypatch.setattr(reg, "_write_tombstone", boom)
        with pytest.raises(OSError):
            await reg.set_channel_state(
                rec.id, current_state_emoji="", strict=True,
            )
        assert rec.current_state_emoji == "🟢"  # rolled back

    async def test_non_strict_write_failure_keeps_todays_semantics(
            self, tmp_path, monkeypatch):
        reg, rec = await self._mk(tmp_path)
        await reg.set_channel_state(rec.id, current_state_emoji="🟢")

        def boom(snapshot):
            raise OSError("disk full")

        monkeypatch.setattr(reg, "_write_tombstone", boom)
        # Best-effort caller: swallowed, and the in-memory value KEEPS the
        # mutation (unchanged pre-#529 behavior).
        await reg.set_channel_state(rec.id, current_state_emoji="🟡")
        assert rec.current_state_emoji == "🟡"

    async def test_cancelled_tombstone_write_settles_before_raising(
            self, tmp_path, monkeypatch):
        """Cancellation landing while the to_thread write is in flight must
        not abandon the thread: the write settles first, THEN CancelledError
        propagates — so a later writer can never be overtaken by a detached
        stale ``os.replace``."""
        import threading
        reg, rec = await self._mk(tmp_path)
        release = threading.Event()
        done = threading.Event()
        real_write = reg._write_tombstone

        def slow_write(snapshot):
            release.wait(timeout=10)
            real_write(snapshot)
            done.set()

        monkeypatch.setattr(reg, "_write_tombstone", slow_write)
        task = asyncio.ensure_future(
            reg.set_channel_state(rec.id, current_state_emoji="🟡"))
        await asyncio.sleep(0.05)      # write is blocked in the thread
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()         # cancel absorbed until the write settles
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert done.is_set()           # the write ran to completion first

    async def test_initial_emoji_declines_when_any_state_established(
            self, tmp_path):
        reg, rec = await self._mk(tmp_path)
        # Fresh record: sets.
        await reg.set_initial_state_emoji(rec.id, "🟢")
        assert rec.current_state_emoji == "🟢"
        # Established state (a terminal paint's emoji): declines.
        await reg.set_channel_state(rec.id, current_state_emoji="✅")
        await reg.set_initial_state_emoji(rec.id, "🟢")
        assert rec.current_state_emoji == "✅"

    async def test_initial_emoji_declines_over_sentinel_and_terminal(
            self, tmp_path):
        reg, rec = await self._mk(tmp_path)
        # An in-flight paint's uncertain sentinel must survive (a launch-path
        # overwrite would re-arm the wrongful no-op the sentinel exists to
        # break).
        await reg.set_channel_state(rec.id, current_state_emoji="")
        await reg.set_initial_state_emoji(rec.id, "🟢")
        assert rec.current_state_emoji == ""
        # Terminal record: declines even with no emoji at all.
        reg2, rec2 = await self._mk(tmp_path)
        await reg2.mark_completed(rec2.id, time.time())
        await reg2.set_initial_state_emoji(rec2.id, "🟢")
        assert rec2.current_state_emoji is None

    async def test_initial_emoji_unknown_engagement_is_noop(self, tmp_path):
        reg, _rec = await self._mk(tmp_path)
        await reg.set_initial_state_emoji("nope", "🟢")  # no boom


class TestBeginTurnDelivery:
    """#588 — the delivery admission seam.

    A turn REDELIVERED to a respawned CLI after a casa-main restart used to
    run against a record boot reconcile had rewritten `active→idle`: the
    bridge grant-gate binds only `active`, so every non-terminal casa tool
    that turn called was refused `tool_not_granted`, including tools the
    engagement genuinely holds. Nothing on the redelivery path called
    `update_user_turn`, which is what the ordinary Telegram arrival path uses
    to flip the record on the way in.
    """

    async def _mk(self, tmp_path, name="e.json"):
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / name))
        rec = await reg.create(
            "executor", "configurator", "in_casa", "t", {}, 1)
        return reg, rec

    async def test_seam_is_not_a_coroutine_function(self):
        """The whole guarantee is that no suspension point exists between the
        caller's FIFO open and its first write. An `async def` here — however
        harmless it looked — reopens exactly the window this closes."""
        import inspect

        from engagement_registry import EngagementRegistry

        assert not inspect.iscoroutinefunction(
            EngagementRegistry.begin_turn_delivery)

    async def test_idle_record_is_reactivated(self, tmp_path):
        reg, rec = await self._mk(tmp_path)
        await reg.mark_idle(rec.id)
        assert rec.status == "idle"
        assert reg.begin_turn_delivery(rec.id) is True
        assert rec.status == "active"

    async def test_reactivation_does_not_restamp_the_user_turn_clock(
            self, tmp_path):
        """A redelivery is not a new user turn. Re-stamping the clock (which
        is why `update_user_turn` is NOT reused) would push the idle sweep's
        suspend threshold out by the age of the engagement on every
        restart."""
        reg, rec = await self._mk(tmp_path)
        await reg.mark_idle(rec.id)
        before_turn = rec.last_user_turn_ts
        reg.begin_turn_delivery(rec.id)
        assert rec.last_user_turn_ts == before_turn

    async def test_active_record_is_admitted_untouched(self, tmp_path):
        reg, rec = await self._mk(tmp_path)
        assert rec.status == "active"
        before = rec.last_user_turn_ts
        assert reg.begin_turn_delivery(rec.id) is True
        assert rec.status == "active"
        assert rec.last_user_turn_ts == before

    @pytest.mark.parametrize("terminal", ["completed", "cancelled", "error"])
    async def test_terminal_record_is_refused_and_never_resurrected(
            self, tmp_path, terminal):
        """Each terminal status checked ALONE: a single `status != "idle"`
        guard is what makes resurrection unrepresentable, and a mutation that
        relaxed it to `status not in TERMINAL` would still pass a test that
        only ever looked at `completed`."""
        reg, rec = await self._mk(tmp_path, name=f"{terminal}.json")
        rec.status = terminal
        assert reg.begin_turn_delivery(rec.id) is False
        assert rec.status == terminal

    async def test_unknown_record_is_admitted(self, tmp_path):
        """Records leave `_records` only on a create() rollback, so this is
        unreachable for a live engagement — and refusing here would make
        delivery hostage to registry knowledge for no authority gain, since
        the bridge gate already fails closed for an id it cannot resolve."""
        reg, _rec = await self._mk(tmp_path)
        assert reg.begin_turn_delivery("no-such-engagement") is True

    async def test_reactivation_reaches_disk(self, tmp_path):
        """Durability is deferred, not dropped: the seam may not await, so it
        schedules the tombstone write instead of performing it."""
        path = tmp_path / "persist.json"
        reg, rec = await self._mk(tmp_path, name="persist.json")
        await reg.mark_idle(rec.id)
        assert json.loads(path.read_text())[0]["status"] == "idle"
        reg.begin_turn_delivery(rec.id)
        # The in-memory flip is immediate; the write lands on the next loop
        # pass (and authority never waited for it).
        for _ in range(50):
            await asyncio.sleep(0.01)
            if json.loads(path.read_text())[0]["status"] == "active":
                break
        assert json.loads(path.read_text())[0]["status"] == "active"

    async def test_persist_failure_does_not_break_the_flip(
            self, tmp_path, monkeypatch):
        """The grant-gate reads the in-memory record, so a failed write costs
        only that another restart re-idles the record — after which the same
        redelivery re-flips it. It must never raise into the delivery path."""
        reg, rec = await self._mk(tmp_path, name="fail.json")
        await reg.mark_idle(rec.id)

        async def _boom(*_a, **_kw):
            raise OSError("disk gone")

        monkeypatch.setattr(reg, "_write_tombstone_locked", _boom)
        assert reg.begin_turn_delivery(rec.id) is True
        assert rec.status == "active"
        await asyncio.sleep(0.05)          # let the deferred task run + fail

    async def test_admission_during_a_failing_strict_transition_is_refused(
            self, tmp_path):
        """The lock-free seam must never observe a status a rollback is about
        to overwrite.

        `try_transition_terminal(strict=True)` snapshots the record's fields
        and restores them if the tombstone write fails. Its mutation used to
        live inside a task created with `asyncio.ensure_future`, which only
        SCHEDULES a coroutine — so the first line of that task ran at the
        `await asyncio.shield(task)`, and in between the record still carried
        its pre-transition status. `begin_turn_delivery` does not take the
        registry lock, so it ran in that window, admitted a turn and flipped
        the record `active`; the restore then put `idle` back, leaving a
        delivered turn against a record whose status strips its authority —
        #588 re-entering through the rollback path (found by Sol on the diff).
        """
        reg, rec = await self._mk(tmp_path, name="strict-race.json")
        await reg.mark_idle(rec.id)
        admitted: list[bool] = []

        # The write FAILS, so the rollback this test is about actually runs
        # (Terra, diff review r2: without this the test exercised the window
        # but never the restore it names).
        async def _failing_write(*_a, **_kw):
            reg._last_tombstone_ok = False
            raise OSError("disk gone")

        reg._write_tombstone_locked = _failing_write   # type: ignore[method-assign]

        task = asyncio.ensure_future(
            reg.try_transition_terminal(rec.id, "cancelled", strict=True))
        # Let the transition run up to its first suspension point — which is
        # exactly the window this guards.
        await asyncio.sleep(0)
        admitted.append(reg.begin_turn_delivery(rec.id))

        outcome = await asyncio.gather(task, return_exceptions=True)

        assert admitted == [False], (
            "a turn was admitted against a record mid-terminal-transition; "
            "the rollback would then strip the authority it was given")
        assert isinstance(outcome[0], OSError), outcome
        # And the rollback did its own job: a refused persist leaves the record
        # exactly as it was, never half-terminal.
        assert rec.status == "idle", rec.status
        assert rec.completed_at is None, rec.completed_at

    async def test_cancelled_strict_write_that_failed_rolls_back(
            self, tmp_path):
        """INV-ENG-002 under cancellation, not just under error.

        Committing the terminal fields before the persist task (above) means
        every later failure path owes a restore — including a `CancelledError`,
        which a repeated cancellation delivers into the write through the
        gather in the caller's cancellation branch. `except Exception` does not
        catch it, so the record was left terminal in memory while disk still
        said live (found by Sol, diff review r2).
        """
        reg, rec = await self._mk(tmp_path, name="cancel-rollback.json")
        await reg.mark_idle(rec.id)

        async def _cancelled_failed_write(*_a, **_kw):
            reg._last_tombstone_ok = False       # settled, and it FAILED
            raise asyncio.CancelledError()

        reg._write_tombstone_locked = _cancelled_failed_write  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await reg.try_transition_terminal(rec.id, "cancelled", strict=True)

        assert rec.status == "idle", (
            "a cancelled strict transition whose write failed left the record "
            f"terminal in memory while disk says otherwise: {rec.status}")
        assert rec.completed_at is None

    async def test_cancelled_strict_write_that_committed_keeps_the_transition(
            self, tmp_path):
        """The other half of the guard: when the settled write DID commit,
        memory and disk already agree, so a cancellation arriving afterwards
        must not undo a terminal state that is durably on disk."""
        reg, rec = await self._mk(tmp_path, name="cancel-committed.json")

        async def _cancelled_committed_write(*_a, **_kw):
            reg._last_tombstone_ok = True        # settled, and it COMMITTED
            raise asyncio.CancelledError()

        reg._write_tombstone_locked = _cancelled_committed_write  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await reg.try_transition_terminal(rec.id, "completed", strict=True)

        assert rec.status == "completed", rec.status

    async def test_repeated_cancellation_holds_the_lock_until_the_write_settles(
            self, tmp_path):
        """A competing writer must never see the transient terminal status.

        The strict path commits the terminal fields in memory, then persists;
        on failure it rolls them back. Between those two the record carries a
        status no one else may act on or persist.

        Raised in review (Terra, diff r3) as a hazard: a REPEATED cancellation
        cancels the caller's `gather`, `async with self._lock` exits while the
        persist is still in flight, and another writer reads the transient
        value. Measured against this code, it does not happen — cancelling the
        gather cancels its child, but the child cannot complete until
        `_write_tombstone_locked`'s absorb-until-settle loop sees its
        uncancellable `to_thread` write settle, so the gather keeps waiting.

        Be honest about what this test is: the property is real and worth
        pinning, but it is provided by the WRITE helper, not by this caller —
        so it does NOT discriminate the caller's cancellation shape. It is a
        regression guard on the absorb-until-settle behaviour those two rely
        on. It drives the real writer (gated, then failing) rather than a
        double that supplies the settlement state.
        """
        import threading

        reg, rec = await self._mk(tmp_path, name="cancel-lock.json")
        await reg.mark_idle(rec.id)

        release = threading.Event()

        def _gated_failing_write(_snapshot):
            release.wait(timeout=5)
            raise OSError("disk gone")

        reg._write_tombstone = _gated_failing_write  # type: ignore[method-assign]

        observed: list[str] = []

        async def competitor():
            async with reg._lock:
                observed.append(rec.status)

        task = asyncio.ensure_future(
            reg.try_transition_terminal(rec.id, "cancelled", strict=True))
        for _ in range(5):
            await asyncio.sleep(0)         # reach the in-flight write
        comp = asyncio.ensure_future(competitor())   # queued on the lock
        # Cancel repeatedly: one of these lands while the caller is waiting for
        # the persist task to settle, which is the case a single gather bailed
        # out of.
        for _ in range(20):
            task.cancel()
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)

        assert observed == [], (
            "the lock was released while the persist was still in flight; a "
            f"competing writer read the transient terminal status: {observed}")

        release.set()
        await asyncio.gather(task, comp, return_exceptions=True)

        assert observed == ["idle"], observed
        assert rec.status == "idle", rec.status

    async def test_rollback_runs_when_the_write_raises_before_it_settles(
            self, tmp_path):
        """`_last_tombstone_ok` must describe THIS write, not the last one.

        The rollback is guarded by that flag so a cancellation whose write
        COMMITTED keeps its mutation. The flag used to be reset only after the
        tombstone snapshot had been built — so a raise during construction left
        the previous (successful) write's `True` in place, the guard read it,
        and a strict transition kept a terminal status in memory although no
        write had happened at all (found by Sol, diff review r3).
        """
        reg, rec = await self._mk(tmp_path, name="stale-flag.json")
        await reg.mark_idle(rec.id)
        assert reg._last_tombstone_ok is True, "precondition: a write succeeded"

        # Make snapshot CONSTRUCTION raise, before any write is scheduled.
        rec.plugin_artifacts = (object(),)   # type: ignore[assignment]
        with pytest.raises(TypeError):
            await reg.try_transition_terminal(rec.id, "cancelled", strict=True)

        assert rec.status == "idle", (
            "no write happened, so the transition owed a rollback: "
            f"{rec.status}")
        assert rec.completed_at is None
