"""#485 — scheduled-delivery eligibility survives a restart.

A scheduled turn that delegates and is interrupted by a restart resumes through
the DURABLE job, not through the live in-memory record. That path rebuilds the
completion origin field by field (``specialist_registry._origin_from_job`` and
the boot orphan-recovery builder in ``casa_core``), so a marker that lives only
in the live record's origin dict is silently gone by the time the resident is
resumed — and its ``send_media`` then refuses a turn that would have worked had
Casa stayed up.

So eligibility is an explicit durable FIELD, defaulting to ``False``: a row
written before this field existed, or any row whose value is missing or
malformed, restores no marker. Absence is not consent — the marker is restored
only from an exact stored ``True``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

LABEL = "cron-weekly-invoice"


def _job(**over):
    from job_registry import DeliveryState, ExecutionState, VoiceJob
    from personality_types import SpeakerProvenance

    fields = dict(
        id="job-1", parent_job_id=None,
        creating_speaker=SpeakerProvenance(speaker_kind="system"),
        executing_speaker=SpeakerProvenance(speaker_kind="system"),
        creating_role="assistant", specialist_role="finance",
        specialist_display_name="finance", creator_peer="telegram",
        creator_user_id=None, scope_id=LABEL, origin_route_id="cid-1",
        origin_device_id=None, task="build the invoice", context="",
        created_at=1.0, started_at=1.0, terminal_at=None, expires_at=None,
        execution_state=ExecutionState.RUNNING,
        delivery_state=DeliveryState.NONE, result=None, failure=None,
        awaiting_input=False, continuable_until=None, delivery_sequence=0,
        delivery_attempt_id=None, lease_until=None, cancel_pending=False,
    )
    fields.update(over)
    return VoiceJob(**fields)


class TestDurableField:
    def test_defaults_false_so_an_unmarked_job_restores_nothing(self):
        assert _job().scheduled_delivery is False

    def test_round_trips_through_the_snapshot(self):
        from job_registry import JobRegistry

        row = JobRegistry._encode_job(_job(scheduled_delivery=True))
        assert row["scheduled_delivery"] is True
        assert JobRegistry._decode_job(row).scheduled_delivery is True

    def test_legacy_row_without_the_field_decodes_false(self):
        """Rows written before this field exists must not fail to load, and
        must not invent eligibility."""
        from job_registry import JobRegistry

        row = JobRegistry._encode_job(_job(scheduled_delivery=True))
        del row["scheduled_delivery"]

        assert JobRegistry._decode_job(row).scheduled_delivery is False

    @pytest.mark.parametrize("stored", ["true", 1, None, "", {}])
    def test_only_an_exact_true_restores_eligibility(self, stored):
        from job_registry import JobRegistry

        row = JobRegistry._encode_job(_job())
        row["scheduled_delivery"] = stored

        assert JobRegistry._decode_job(row).scheduled_delivery is False


class TestOriginReconstruction:
    def test_marked_job_restores_the_marker(self):
        from specialist_registry import SpecialistRegistry

        origin = SpecialistRegistry._origin_from_job(
            _job(scheduled_delivery=True),
        )
        assert origin["_scheduled_delivery"] is True
        # The session label is still the label — restoring eligibility must not
        # smuggle in a delivery address.
        assert origin["chat_id"] == LABEL

    def test_unmarked_job_adds_no_key(self):
        from specialist_registry import SpecialistRegistry

        origin = SpecialistRegistry._origin_from_job(_job())
        assert "_scheduled_delivery" not in origin


class TestRegistration:
    @pytest.mark.parametrize(
        "marker,expected",
        [(True, True), (False, False), ("true", False), (1, False)],
    )
    async def test_register_delegation_stores_the_field_from_the_origin(
        self, tmp_path, marker, expected,
    ):
        """The assignment itself, not just the helper: a marked turn's
        delegation must reach `jobs.json` carrying the eligibility, and an
        unmarked or malformed one must not."""
        import asyncio

        from specialist_registry import DelegationRecord, SpecialistRegistry

        reg = SpecialistRegistry(
            str(tmp_path / "ex"), tombstone_path=str(tmp_path / "del.json"),
        )
        await reg.register_delegation(DelegationRecord(
            id="delegation-1", agent="finance",
            started_at=asyncio.get_running_loop().time(),
            origin={
                "role": "assistant", "execution_role": "assistant",
                "channel": "telegram", "chat_id": LABEL,
                "_scheduled_delivery": marker,
            },
        ))

        job = reg._job_registry.get("delegation-1")
        assert job.scheduled_delivery is expected

    def test_marker_on_the_turn_origin_becomes_the_durable_field(self):
        from specialist_registry import scheduled_delivery_of

        assert scheduled_delivery_of({"_scheduled_delivery": True}) is True

    @pytest.mark.parametrize("marker", [False, "true", 1, None])
    def test_anything_but_exact_true_is_not_durable_eligibility(self, marker):
        from specialist_registry import scheduled_delivery_of

        assert scheduled_delivery_of({"_scheduled_delivery": marker}) is False

    def test_absent_marker_is_not_eligibility(self):
        from specialist_registry import scheduled_delivery_of

        assert scheduled_delivery_of({}) is False
