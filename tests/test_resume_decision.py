"""Task 9: structured, digest-aware resume-decision gate.

Exercises the pure ``agent._resume_decision`` seam directly — the
``{stable_agent_id, role_checksum, binding_digest}`` identity gate on every
session-resume path (spec §3.3/§4.2 + personality Task 9)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent import _resume_decision, agent_home_for_role_id

pytestmark = [pytest.mark.unit]


def _stored_entry(**overrides) -> dict:
    base = {
        "agent": "resident:butler", "sdk_session_id": "sid-1",
        "last_active": datetime.now(timezone.utc).isoformat(),
        "binding_digest": "sha256:" + "1" * 64,
        "speaker_provenance": {
            "speaker_kind": "resident", "role_id": "resident:butler",
            "persona_id": "casa/tina", "persona_version": "0.1.0",
            "display_name": "Tina", "binding_digest": "sha256:" + "1" * 64,
            "user_peer": None, "user_id": None,
        },
        "user_provenance": {
            "speaker_kind": "user", "role_id": None, "persona_id": None,
            "persona_version": None, "display_name": None, "binding_digest": None,
            "user_peer": "telegram_1", "user_id": "1",
        },
    }
    base.update(overrides)
    return base


def test_binding_mismatch_retains_immutable_old_snapshot() -> None:
    now = datetime.now(timezone.utc)
    entry = _stored_entry()
    decision = _resume_decision(
        "telegram", entry, now, role_id="resident:butler",
        binding_digest="sha256:" + "9" * 64,
    )
    assert decision.action == "new"
    assert decision.reason == "binding_mismatch"
    assert decision.old.sdk_session_id == entry["sdk_session_id"]
    # The snapshot is an immutable copy — mutating the source dict afterwards
    # must never bleed into the retained decision.
    entry["speaker_provenance"]["display_name"] = "Mutated"
    assert decision.old.speaker_provenance.display_name != "Mutated"


def test_role_mismatch_still_rejects_before_digest_is_checked() -> None:
    now = datetime.now(timezone.utc)
    entry = _stored_entry()
    decision = _resume_decision(
        "telegram", entry, now, role_id="resident:assistant",
        binding_digest=entry["binding_digest"],
    )
    assert decision.reason == "role_mismatch"
    assert decision.action == "new"


def test_matching_role_and_digest_within_window_resumes() -> None:
    now = datetime.now(timezone.utc)
    entry = _stored_entry()
    decision = _resume_decision(
        "telegram", entry, now, role_id=entry["agent"],
        binding_digest=entry["binding_digest"],
    )
    assert decision.action == "resume"
    assert decision.reason == "fresh"
    assert decision.resume_sid == entry["sdk_session_id"]


def test_missing_entry_is_new_with_no_old_snapshot() -> None:
    now = datetime.now(timezone.utc)
    decision = _resume_decision(
        "telegram", None, now, role_id="resident:assistant",
        binding_digest="sha256:" + "1" * 64,
    )
    assert decision.action == "new"
    assert decision.reason == "missing"
    assert decision.old is None


def test_expired_matching_entry_retains_old_for_save() -> None:
    from datetime import timedelta

    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    entry = _stored_entry(
        last_active=(now - timedelta(hours=13)).isoformat(),
    )  # telegram window 12h → expired
    decision = _resume_decision(
        "telegram", entry, now, role_id=entry["agent"],
        binding_digest=entry["binding_digest"],
    )
    assert decision.action == "new"
    assert decision.reason == "expired"
    assert decision.old.sdk_session_id == entry["sdk_session_id"]


def test_invalid_last_active_is_new_and_retains_old() -> None:
    now = datetime.now(timezone.utc)
    entry = _stored_entry(last_active="not-a-timestamp")
    decision = _resume_decision(
        "telegram", entry, now, role_id=entry["agent"],
        binding_digest=entry["binding_digest"],
    )
    assert decision.action == "new"
    assert decision.reason == "invalid_entry"


def test_naive_last_active_is_invalid_entry_not_a_crash() -> None:
    # A tz-less ISO timestamp parses fine, but comparing it against the aware
    # ``now`` must not raise — it is an invalid entry like any other bad value.
    now = datetime.now(timezone.utc)
    entry = _stored_entry(last_active="2026-07-31T12:00:00")
    decision = _resume_decision(
        "telegram", entry, now, role_id=entry["agent"],
        binding_digest=entry["binding_digest"],
    )
    assert decision.action == "new"
    assert decision.reason == "invalid_entry"


def test_legacy_short_role_entry_cannot_resume_under_canonical_role_id() -> None:
    now = datetime.now(timezone.utc)
    legacy_entry = {
        "agent": "butler", "sdk_session_id": "sid-legacy",
        "last_active": now.isoformat(),
    }  # no binding_digest, no provenance — a pre-Task-9 record
    decision = _resume_decision(
        "telegram", legacy_entry, now, role_id="resident:butler",
        binding_digest="sha256:" + "1" * 64,
    )
    assert decision.action == "new"
    assert decision.reason == "role_mismatch"


# I1 (final review, Task 9): agent_home_for_role_id must return EXACTLY the
# path agent_home.py's provision_agent_home() creates (bare
# ``<home_root>/<slot>`` for every kind — residents AND specialists; today's
# provisioning never nests under ``{kind}s/``), since the transcript
# sweepers derive the transcript directory from this function.


def test_agent_home_for_role_id_resident_is_bare_slug() -> None:
    assert agent_home_for_role_id("resident:butler") == "/config/agent-home/butler"


def test_agent_home_for_role_id_specialist_is_bare_slug_not_nested() -> None:
    # Matches provision_agent_home's ``home_root / role`` (agent_home.py) —
    # NOT the ``specialists/<slot>`` nesting no provisioning code creates.
    assert agent_home_for_role_id("specialist:finance") == "/config/agent-home/finance"


def test_agent_home_for_role_id_executor_is_bare_slug_not_nested() -> None:
    assert (
        agent_home_for_role_id("executor:configurator")
        == "/config/agent-home/configurator"
    )


@pytest.mark.parametrize("bad_role_id", ["butler", "bogus:", ":slot", ""])
def test_agent_home_for_role_id_rejects_malformed_id(bad_role_id: str) -> None:
    with pytest.raises(ValueError):
        agent_home_for_role_id(bad_role_id)
