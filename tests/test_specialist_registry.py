"""Tests for specialist_registry.py — Tier 2 loader + registry."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


# ---------------------------------------------------------------------------
# Fixture helper — seed a per-concern specialist directory under *base*.
# ---------------------------------------------------------------------------


def _seed_specialist_dir(
    base: Path,
    role: str,
    *,
    enabled: bool = True,
    channels: list[str] | None = None,
    strategy: str = "ephemeral",
    token_budget: int = 0,
) -> Path:
    d = base / role
    d.mkdir(parents=True)
    (d / "character.yaml").write_text(textwrap.dedent(f"""\
        schema_version: 1
        name: {role.capitalize()}
        role: {role}
        archetype: exec
        card: |
          x
        prompt: |
          x
    """), encoding="utf-8")
    (d / "voice.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (d / "response_shape.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    channels_part = channels if channels is not None else []
    (d / "runtime.yaml").write_text(textwrap.dedent(f"""\
        schema_version: 1
        kind: specialist
        model: {{source: fixed, value: sonnet}}
        enabled: {str(enabled).lower()}
        tools:
          allowed: [Read]
        channels: {channels_part}
        memory:
          token_budget: {token_budget}
        session:
          strategy: {strategy}
    """), encoding="utf-8")
    return d


def _use_synthetic_roles_dir(monkeypatch, tmp_path, *slots: str) -> None:
    """Personality Phase A, Task 5: SpecialistRegistry.load() -> agent_loader
    .load_all_specialists() -> load_agent_from_dir() now requires a canonical
    role artifact under defaults/roles/specialist/<slot>/ for every
    specialist it loads (cross-validated on kind/slot). The real shipped
    image only carries 'finance'; tests here deliberately use synthetic
    specialist names to probe registry mechanics (token budgets, summary
    logging, disabled-role sorting) unrelated to role-artifact content.
    SpecialistRegistry has no roles_dir override (out of Task 5's scope —
    it isn't in the brief's file list), so redirect agent_loader's module-
    level DEFAULT_ROLES_DIR for the duration of the test instead."""
    try:
        from tests.test_agent_loader import _seed_role_artifact
    except ImportError:
        from test_agent_loader import _seed_role_artifact

    roles_dir = tmp_path / "roles"
    for slot in slots:
        _seed_role_artifact(roles_dir, "specialist", slot)
    monkeypatch.setattr("agent_loader.DEFAULT_ROLES_DIR", str(roles_dir))


# ---------------------------------------------------------------------------
# TestLoader
# ---------------------------------------------------------------------------


class TestLoader:
    async def test_empty_dir_loads_nothing(self, tmp_path):
        from specialist_registry import SpecialistRegistry

        specialists = tmp_path / "specialists"
        specialists.mkdir()
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        assert reg.get("finance") is None

    async def test_missing_dir_is_noop(self, tmp_path):
        from specialist_registry import SpecialistRegistry

        reg = SpecialistRegistry(str(tmp_path / "does_not_exist"),
                                 tombstone_path=str(tmp_path / "del.json"))
        # Must NOT raise.
        reg.load()
        assert reg.get("finance") is None

    async def test_loads_enabled_specialist(self, tmp_path, monkeypatch):
        from specialist_registry import SpecialistRegistry

        specialists = tmp_path / "specialists"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        _use_synthetic_roles_dir(monkeypatch, tmp_path, "finance")
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        cfg = reg.get("finance")
        assert cfg is not None
        assert cfg.role == "finance"
        assert cfg.model == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# TestValidation — Tier-2-shape rejection
# ---------------------------------------------------------------------------


class TestValidation:
    async def test_rejects_non_empty_channels(self, tmp_path, caplog):
        """With channels declared, the loader re-infers the tier as
        ``resident`` and demands disclosure.yaml (which specialists do not
        carry). The file-set check raises ``LoadError`` before the
        Tier-2 shape validator ever runs — that's still a rejection."""
        import logging
        from specialist_registry import SpecialistRegistry

        specialists = tmp_path / "specialists"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "bogus", channels=["telegram"])
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        with caplog.at_level(logging.ERROR):
            reg.load()
        assert reg.get("bogus") is None
        # O-2b (v0.37.9): per-specialist load failures log via
        # ``Specialist %r failed to load: …`` and are also tracked in
        # ``load_failures()`` for reload to surface to casactl.
        assert any(
            "failed to load" in r.message.lower()
            for r in caplog.records
        )
        failures = reg.load_failures()
        assert any(name == "bogus" for name, _ in failures)

    async def test_accepts_non_zero_token_budget(self, tmp_path, caplog, monkeypatch):
        """M4b: specialists may opt into Honcho memory by setting
        memory.token_budget > 0. The validator must accept this."""
        import logging
        from specialist_registry import SpecialistRegistry

        specialists = tmp_path / "specialists"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "rich", token_budget=1000)
        _use_synthetic_roles_dir(monkeypatch, tmp_path, "rich")
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        with caplog.at_level(logging.ERROR):
            reg.load()
        cfg = reg.get("rich")
        assert cfg is not None, "specialist with token_budget>0 must load"
        assert cfg.memory.token_budget == 1000
        # No rejection log mentioning token_budget
        assert not any(
            "token_budget" in r.message and "Rejecting" in r.message
            for r in caplog.records
        )

    async def test_rejects_non_ephemeral_session(self, tmp_path, caplog):
        import logging
        from specialist_registry import SpecialistRegistry

        specialists = tmp_path / "specialists"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "sticky", strategy="persistent")
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        with caplog.at_level(logging.ERROR):
            reg.load()
        assert reg.get("sticky") is None

    async def test_stray_flat_yaml_rejected(self, tmp_path, caplog):
        """Flat-YAML files inside specialists/ are rejected by the new
        loader (directories only). SpecialistRegistry catches LoadError
        and logs ERROR; no specialists register."""
        import logging
        from specialist_registry import SpecialistRegistry

        specialists = tmp_path / "specialists"
        specialists.mkdir()
        (specialists / "broken.yaml").write_text(
            "not a directory — the loader rejects this\n",
            encoding="utf-8",
        )
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        with caplog.at_level(logging.ERROR):
            reg.load()
        assert reg.get("broken") is None
        assert any(
            "specialist load failed" in r.message.lower()
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# TestEnabledFiltering
# ---------------------------------------------------------------------------


class TestEnabledFiltering:
    async def test_disabled_specialist_parsed_but_skipped(
        self, tmp_path, caplog, monkeypatch,
    ):
        import logging
        from specialist_registry import SpecialistRegistry

        specialists = tmp_path / "specialists"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=False)
        _use_synthetic_roles_dir(monkeypatch, tmp_path, "finance")
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        with caplog.at_level(logging.INFO):
            reg.load()
        # Not registered for delegation dispatch.
        assert reg.get("finance") is None
        # One-line disabled-log present.
        assert any(
            "disabled" in r.message.lower() and "finance" in r.message.lower()
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# TestDelegationLifecycle (durable compatibility facade)
# ---------------------------------------------------------------------------


class TestDurableJobFacade:
    async def test_casa_runtime_exposes_job_registry(self):
        from runtime import CasaRuntime

        assert "job_registry" in CasaRuntime.__dataclass_fields__

    async def test_injected_job_registry_is_the_only_lifecycle_store(
        self, tmp_path,
    ):
        from job_registry import ExecutionState, JobRegistry
        from specialist_registry import DelegationRecord, SpecialistRegistry

        jobs = JobRegistry(tmp_path / "jobs.json")
        await jobs.load()
        reg = SpecialistRegistry(
            str(tmp_path / "specialists"), job_registry=jobs,
        )
        await reg.register_delegation(DelegationRecord(
            id="d-1", agent="finance", started_at=1.0,
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "chat-1", "cid": "route-1"},
        ))

        assert not hasattr(reg, "_delegations")
        assert reg.job_registry is jobs
        assert jobs.get("d-1").execution_state is ExecutionState.RUNNING
        assert reg.has_delegation("d-1")

        await reg.complete_delegation("d-1")
        assert jobs.get("d-1").execution_state is ExecutionState.SUCCEEDED
        assert not reg.has_delegation("d-1")

        reloaded = JobRegistry(tmp_path / "jobs.json")
        await reloaded.load()
        assert reloaded.get("d-1").execution_state is ExecutionState.SUCCEEDED

    async def test_orphan_compatibility_view_reads_durable_jobs_only(
        self, tmp_path,
    ):
        from job_registry import JobRegistry
        from specialist_registry import (
            DelegationRecord, SpecialistRegistry,
        )

        seeded = JobRegistry(tmp_path / "jobs.json")
        await seeded.load()
        seeded_reg = SpecialistRegistry(
            str(tmp_path / "specialists"), job_registry=seeded,
        )
        await seeded_reg.register_delegation(DelegationRecord(
            id="orphan-1", agent="finance", started_at=100.0,
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "chat-1", "cid": "route-1"},
        ))

        # A restart orphans the still-running row; the facade view reads it
        # from the durable snapshot alone.
        jobs = JobRegistry(tmp_path / "jobs.json")
        await jobs.load()
        await jobs.recover_after_restart()
        reg = SpecialistRegistry(
            str(tmp_path / "specialists"), job_registry=jobs,
        )

        assert [record.id for record in reg.orphans_from_disk()] == ["orphan-1"]


class TestDelegationLifecycle:
    def _make_registry(self, tmp_path):
        from specialist_registry import SpecialistRegistry
        return SpecialistRegistry(str(tmp_path / "specialists"),
                                  tombstone_path=str(tmp_path / "del.json"))

    async def test_register_then_complete_removes_record(self, tmp_path):
        from specialist_registry import DelegationRecord

        reg = self._make_registry(tmp_path)
        rec = DelegationRecord(
            id="d-1", agent="finance", started_at=1.0,
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "x", "cid": "c1", "user_text": "hi"},
        )
        await reg.register_delegation(rec)
        assert reg.has_delegation("d-1")
        await reg.complete_delegation("d-1")
        assert not reg.has_delegation("d-1")

    async def test_fail_delegation_removes_record(self, tmp_path):
        from specialist_registry import DelegationRecord

        reg = self._make_registry(tmp_path)
        rec = DelegationRecord(
            id="d-2", agent="finance", started_at=1.0,
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "x", "cid": "c1", "user_text": "hi"},
        )
        await reg.register_delegation(rec)
        await reg.fail_delegation("d-2", RuntimeError("boom"))
        assert not reg.has_delegation("d-2")

    async def test_cancel_delegation_removes_record(self, tmp_path):
        from specialist_registry import DelegationRecord

        reg = self._make_registry(tmp_path)
        rec = DelegationRecord(
            id="d-3", agent="finance", started_at=1.0,
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "x", "cid": "c1", "user_text": "hi"},
        )
        await reg.register_delegation(rec)
        await reg.cancel_delegation("d-3")
        assert not reg.has_delegation("d-3")

    async def test_terminal_calls_are_idempotent(self, tmp_path):
        """complete/fail/cancel on a non-existent id must not raise."""
        reg = self._make_registry(tmp_path)
        await reg.complete_delegation("missing")
        await reg.fail_delegation("missing", RuntimeError("x"))
        await reg.cancel_delegation("missing")

    async def test_repeated_terminal_calls_are_idempotent(self, tmp_path):
        from specialist_registry import DelegationRecord

        reg = self._make_registry(tmp_path)
        await reg.register_delegation(DelegationRecord(
            id="d-repeat", agent="finance", started_at=1.0, origin={},
        ))
        await reg.complete_delegation("d-repeat")
        await reg.complete_delegation("d-repeat")
        await reg.fail_delegation("d-repeat", RuntimeError("late callback"))
        await reg.cancel_delegation("d-repeat")

    async def test_competing_terminal_facade_calls_are_registry_atomic(
        self, tmp_path,
    ):
        from job_registry import ExecutionState, JobRegistry
        from specialist_registry import DelegationRecord, SpecialistRegistry

        jobs_path = tmp_path / "jobs.json"
        jobs = JobRegistry(jobs_path)
        await jobs.load()
        reg = SpecialistRegistry(
            str(tmp_path / "specialists"), job_registry=jobs,
        )
        await reg.register_delegation(DelegationRecord(
            id="d-race", agent="finance", started_at=1.0, origin={},
        ))

        await jobs._lock.acquire()
        complete = asyncio.create_task(reg.complete_delegation("d-race"))
        fail = asyncio.create_task(
            reg.fail_delegation("d-race", RuntimeError("late failure")),
        )
        try:
            await asyncio.sleep(0)
            assert len(jobs._lock._waiters) == 2
        finally:
            jobs._lock.release()

        assert await asyncio.gather(complete, fail, return_exceptions=True) == [
            None, None,
        ]
        terminal = jobs.get("d-race")
        assert terminal.execution_state in {
            ExecutionState.SUCCEEDED, ExecutionState.FAILED,
        }

        reloaded = JobRegistry(jobs_path)
        await reloaded.load()
        persisted = reloaded.get("d-race")
        assert persisted.execution_state is terminal.execution_state
        assert persisted.result == terminal.result
        assert persisted.failure == terminal.failure


# ---------------------------------------------------------------------------
# Task 12: register_delegation's typed creating/executing speaker provenance
# ---------------------------------------------------------------------------


class TestRegisterDelegationSpeakerProvenance:
    async def test_sources_creating_from_origin_and_executing_from_configs(
        self, tmp_path,
    ):
        """creating_speaker comes from record.origin["speaker_provenance"]
        (Task 10 Step 7's origin_var wiring); executing_speaker comes from
        the specialist's own AgentConfig.speaker_provenance, already held in
        self._configs (populated by load()) — record carries neither."""
        from config import AgentConfig
        from personality_types import SpeakerProvenance
        from specialist_registry import DelegationRecord, SpecialistRegistry

        try:
            from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
        except ImportError:
            from role_artifact_stub import STUB_ROLE_ARTIFACT

        reg = SpecialistRegistry(str(tmp_path / "specialists"),
                                  tombstone_path=str(tmp_path / "del.json"))
        reg.load()  # empty dir — no specialists on disk
        finance_provenance = SpeakerProvenance(
            speaker_kind="specialist", role_id="specialist:finance",
            persona_id="casa/finance", persona_version="0.1.0",
            display_name="Finance", binding_digest="sha256:" + "e" * 64,
        )
        reg._configs["finance"] = AgentConfig(
            role_artifact=STUB_ROLE_ARTIFACT, role="finance",
            speaker_provenance=finance_provenance,
        )

        caller = SpeakerProvenance(
            speaker_kind="resident", role_id="resident:assistant",
            persona_id="casa/ellen", persona_version="0.1.0",
            display_name="Ellen", binding_digest="sha256:" + "1" * 64,
        )
        record = DelegationRecord(
            id="d1", agent="finance", started_at=0.0,
            origin={"role": "assistant", "channel": "telegram",
                    "speaker_provenance": caller},
        )
        await reg.register_delegation(record)
        job = reg.job_registry.get("d1")
        assert job.creating_speaker == caller
        assert job.executing_speaker == finance_provenance
        assert job.executing_speaker.speaker_kind in {"specialist", "system"}
        assert job.executing_speaker.speaker_kind != "executor"

    async def test_falls_back_to_system_never_none(self, tmp_path):
        """No speaker_provenance on origin (legacy/test caller), and the
        specialist isn't in _configs at all — both fall back to an explicit
        system identity, never None, never a fabricated persona."""
        from specialist_registry import DelegationRecord, SpecialistRegistry

        reg = SpecialistRegistry(str(tmp_path / "specialists"),
                                  tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        record = DelegationRecord(
            id="d2", agent="unknown-specialist", started_at=0.0, origin={},
        )
        await reg.register_delegation(record)
        job = reg.job_registry.get("d2")
        assert job.creating_speaker.speaker_kind == "system"
        assert job.executing_speaker.speaker_kind == "system"

    async def test_non_provenance_origin_value_also_falls_back_to_system(
        self, tmp_path,
    ):
        """A non-SpeakerProvenance value under the reserved key (a stale/
        malformed legacy caller) must not be trusted as-is."""
        from specialist_registry import DelegationRecord, SpecialistRegistry

        reg = SpecialistRegistry(str(tmp_path / "specialists"),
                                  tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        record = DelegationRecord(
            id="d3", agent="finance", started_at=0.0,
            origin={"speaker_provenance": {"speaker_kind": "resident"}},
        )
        await reg.register_delegation(record)
        job = reg.job_registry.get("d3")
        assert job.creating_speaker.speaker_kind == "system"


# ---------------------------------------------------------------------------
# TestDelegationComplete dataclass shape
# ---------------------------------------------------------------------------


class TestDelegationCompleteShape:
    async def test_defaults(self):
        from specialist_registry import DelegationComplete

        c = DelegationComplete(
            delegation_id="d-1", agent="finance", status="ok",
        )
        assert c.text == ""
        assert c.kind == ""
        assert c.message == ""
        assert c.origin == {}
        assert c.elapsed_s == 0.0

    async def test_full_ok(self):
        from specialist_registry import DelegationComplete

        c = DelegationComplete(
            delegation_id="d-1", agent="finance", status="ok",
            text="result text",
            origin={"role": "assistant"},
            elapsed_s=2.5,
        )
        assert c.status == "ok"
        assert c.text == "result text"


# ---------------------------------------------------------------------------
# TestSummaryLog (Phase 3.4)
# ---------------------------------------------------------------------------


class TestSummaryLog:
    async def test_summary_line_present_with_mixed_enabled_disabled(
        self, tmp_path, caplog, monkeypatch,
    ):
        import logging
        from specialist_registry import SpecialistRegistry

        specialists = tmp_path / "specialists"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "foo", enabled=True)
        _seed_specialist_dir(specialists, "bar", enabled=False)
        _use_synthetic_roles_dir(monkeypatch, tmp_path, "foo", "bar")

        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        with caplog.at_level(logging.INFO):
            reg.load()

        summary = [r for r in caplog.records
                   if r.message.startswith("Specialists:")]
        assert len(summary) == 1, (
            f"expected exactly one 'Specialists:' summary line, "
            f"got {len(summary)}: {[r.message for r in summary]}"
        )
        msg = summary[0].message
        assert "'foo'" in msg
        assert "'bar'" in msg
        # Enabled vs disabled positioning — the helper wraps each set in
        # its own bracketed list so we can distinguish them.
        enabled_idx = msg.find("enabled=")
        disabled_idx = msg.find("disabled=")
        assert 0 <= enabled_idx < disabled_idx
        # 'foo' sits in the enabled segment; 'bar' in the disabled segment.
        assert msg.find("'foo'") < disabled_idx
        assert msg.find("'bar'") > disabled_idx

    async def test_summary_line_empty_when_dir_missing(
        self, tmp_path, caplog,
    ):
        import logging
        from specialist_registry import SpecialistRegistry

        reg = SpecialistRegistry(str(tmp_path / "does_not_exist"),
                                 tombstone_path=str(tmp_path / "del.json"))
        with caplog.at_level(logging.INFO):
            reg.load()

        summary = [r for r in caplog.records
                   if r.message.startswith("Specialists:")]
        # Missing dir is a no-op (existing test_missing_dir_is_noop); the
        # summary line is still emitted so operator visibility is uniform.
        assert len(summary) == 1
        msg = summary[0].message
        assert "enabled=[]" in msg
        assert "disabled=[]" in msg


# ---------------------------------------------------------------------------
# TestDisabledAccessors — Phase 5 / E-15 public accessors for disabled peers
# ---------------------------------------------------------------------------


class TestDisabledAccessors:
    async def test_is_disabled_false_for_unknown_role(self, tmp_path):
        from specialist_registry import SpecialistRegistry

        specialists = tmp_path / "specialists"
        specialists.mkdir()
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        assert reg.is_disabled("nonexistent") is False

    async def test_is_disabled_true_for_bundled_disabled(self, tmp_path, monkeypatch):
        """A specialist with enabled:false in runtime.yaml lands in
        _disabled_names; is_disabled returns True."""
        from specialist_registry import SpecialistRegistry

        specialists = tmp_path / "specialists"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=False)
        _use_synthetic_roles_dir(monkeypatch, tmp_path, "finance")
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        assert reg.is_disabled("finance") is True
        assert reg.get("finance") is None  # confirm not enabled

    async def test_is_disabled_false_for_enabled_specialist(self, tmp_path, monkeypatch):
        """An enabled specialist is NOT disabled — it's in _configs, not
        _disabled_names. is_disabled returns False."""
        from specialist_registry import SpecialistRegistry

        specialists = tmp_path / "specialists"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "finance", enabled=True)
        _use_synthetic_roles_dir(monkeypatch, tmp_path, "finance")
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()
        assert reg.is_disabled("finance") is False
        assert reg.get("finance") is not None

    async def test_disabled_roles_returns_sorted_defensive_copy(
        self, tmp_path, monkeypatch,
    ):
        from specialist_registry import SpecialistRegistry

        specialists = tmp_path / "specialists"
        specialists.mkdir()
        _seed_specialist_dir(specialists, "zeta", enabled=False)
        _seed_specialist_dir(specialists, "alpha", enabled=False)
        _seed_specialist_dir(specialists, "beta", enabled=False)
        _use_synthetic_roles_dir(monkeypatch, tmp_path, "zeta", "alpha", "beta")
        reg = SpecialistRegistry(str(specialists),
                                 tombstone_path=str(tmp_path / "del.json"))
        reg.load()

        out = reg.disabled_roles()
        assert out == ["alpha", "beta", "zeta"]
        # Defensive copy — mutations don't bleed back.
        out.append("intruder")
        assert "intruder" not in reg._disabled_names
        assert reg.disabled_roles() == ["alpha", "beta", "zeta"]


class TestSpecialistBootCapabilities:
    """D-2 (v0.69.7): specialists must emit the L5 `agent_capabilities` line
    at boot (load), not only on reload/construct — so post-install
    verification has a log oracle for specialist targets too."""

    async def test_load_logs_agent_capabilities_for_enabled_specialist(
        self, tmp_path, caplog, monkeypatch,
    ):
        import logging

        from specialist_registry import SpecialistRegistry

        _seed_specialist_dir(tmp_path, "finance", enabled=True)
        _use_synthetic_roles_dir(monkeypatch, tmp_path, "finance")
        reg = SpecialistRegistry(str(tmp_path), tombstone_path=str(tmp_path / "d.json"))
        with caplog.at_level(logging.INFO):
            reg.load()
        cap = [r.getMessage() for r in caplog.records
               if "agent_capabilities" in r.getMessage() and "role=finance" in r.getMessage()]
        assert cap, "no agent_capabilities line logged for the enabled specialist at load"
        # #459: the boot line reports the role.yaml DECLARATION, not the
        # effective set (which is composed per delegation), so its fields must
        # say so — `tool_count=0` beside `enabled=True` read as a specialist
        # that got nothing, when its tools arrive via its owned plugin's
        # server-level grant.
        assert "model=" in cap[0] and "declared_tool_count=" in cap[0]
        assert "declared_tools=" in cap[0] and "declared_mcp_servers=" in cap[0]
        assert " tool_count=" not in cap[0]

    async def test_disabled_specialist_gets_no_capabilities_line(self, tmp_path, caplog, monkeypatch):
        import logging

        from specialist_registry import SpecialistRegistry

        _seed_specialist_dir(tmp_path, "finance", enabled=False)
        _use_synthetic_roles_dir(monkeypatch, tmp_path, "finance")
        reg = SpecialistRegistry(str(tmp_path), tombstone_path=str(tmp_path / "d.json"))
        with caplog.at_level(logging.INFO):
            reg.load()
        assert not any("agent_capabilities" in r.getMessage() for r in caplog.records)


class TestInstalledSpecialistIndexCollisionSet:
    """Task 13: the slug-collision authority is EVERY image role's bare slot,
    across ALL THREE kinds (resident, executor, AND specialist) — a prior
    draft's hard-coded resident+executor-only set silently omitted the
    bundled specialist:finance, which this regression-gated permanently.

    Task N2's no-gap cutover removed finance (the only bundled specialist)
    from the image, so `all_collision_slugs` can no longer be proven against
    a REAL specialist-kind image slot — this test now monkeypatches the
    frozen `_IMAGE_ROLE_SLOTS` module global (computed once at import from
    the real image tree) to a synthetic set that still includes a
    specialist-kind slug, preserving the exact invariant under test:
    `all_collision_slugs` unions the FULL image role-slot set (whichever
    kinds it spans) with the installed-specialist set, never a hand-picked
    per-kind subset."""

    async def test_all_collision_slugs_includes_every_image_role_kind(self, monkeypatch) -> None:
        from specialist_registry import InstalledSpecialistIndex

        monkeypatch.setattr(
            "specialist_registry._IMAGE_ROLE_SLOTS",
            frozenset({"assistant", "butler", "concierge", "configurator",
                       "plugin-developer", "testspecialist"}),
        )
        index = InstalledSpecialistIndex(specialists_dir="/nonexistent")
        index.load()  # zero installed specialists — this still asserts the IMAGE role set
        collisions = index.all_collision_slugs()
        assert "testspecialist" in collisions  # a specialist-kind image slot — the bug this fixes
        assert {"assistant", "butler", "concierge", "configurator",
                "plugin-developer", "testspecialist"} <= collisions

    async def test_discover_image_role_slots_scans_every_kind_directory(self, tmp_path) -> None:
        """Unit-level proof the discovery walks resident/executor/specialist alike —
        not a hand-picked per-kind constant."""
        from specialist_registry import _discover_image_role_slots

        for kind, slot in (("resident", "assistant"), ("executor", "configurator"),
                            ("specialist", "finance")):
            role_dir = tmp_path / kind / slot
            role_dir.mkdir(parents=True)
            (role_dir / "role.yaml").write_text(f"slot: {slot}\n", encoding="utf-8")
        assert _discover_image_role_slots(str(tmp_path)) == {"assistant", "configurator", "finance"}


async def test_one_malformed_installed_tuple_does_not_abort_the_index_load(tmp_path):
    """#346: InstalledSpecialistIndex.load() ran InstanceDir.active()/
    .desired() unguarded — a single damaged active.yaml (bad YAML, schema
    violation, tampered digest) raised out of the boot-time scan and aborted
    the whole app start. The damaged slug must be isolated as state="error"
    (keeping its slug reserved against reinstall-overwrite) while healthy
    siblings load normally."""
    from personality_binding import InstanceDir, InstanceTuple
    from specialist_registry import InstalledSpecialistIndex
    from test_specialist_install import _specialist_binding

    root = tmp_path / "specialists"
    # Healthy installed specialist: a real, verifiable active tuple.
    binding = _specialist_binding("healthy", mode="component-default",
                                  component_root="casa/healthy@0.1.0#sha256:" + "a" * 64)
    good = InstanceDir(root / "healthy")
    good.stage_desired(InstanceTuple(
        root="casa/healthy@0.1.0#sha256:" + "a" * 64, binding=binding,
        config_snapshot={}, config_digest=binding.effective_config_digest,
    ))
    good.commit_desired_to_active()
    # Damaged sibling: active.yaml present but unparseable.
    (root / "damaged").mkdir(parents=True)
    (root / "damaged" / "active.yaml").write_text("{{{ not yaml", encoding="utf-8")

    index = InstalledSpecialistIndex(specialists_dir=str(root))
    index.load()  # must NOT raise

    assert "healthy" in index.installed_slugs()
    damaged = index.get_instance("damaged")
    assert damaged is not None
    assert damaged.state == "error"
    assert damaged.active is None and damaged.desired is None
    assert damaged.last_activation_error
    # The slug stays reserved: a fresh install cannot silently overwrite it.
    assert "damaged" in index.all_collision_slugs()


async def test_a_rejected_desired_does_not_take_down_a_healthy_active(tmp_path):
    """#372 (D7, Terra design r2): active and desired must be loaded
    INDEPENDENTLY — a tombstoned/pre-guard desired.yaml (e.g. a pending
    upgrade staged by an older version) must not drop the slug's healthy
    active generation from the fleet. The instance stays active, with the
    desired failure surfaced as diagnostic state."""
    from personality_binding import InstanceDir, InstanceTuple, PRE_GUARD_SENTINEL
    from specialist_registry import InstalledSpecialistIndex
    from test_specialist_install import _specialist_binding

    root = tmp_path / "specialists"
    binding = _specialist_binding("mixed", mode="component-default",
                                  component_root="casa/mixed@0.1.0#sha256:" + "a" * 64)
    d = InstanceDir(root / "mixed")
    d.stage_desired(InstanceTuple(
        root="casa/mixed@0.1.0#sha256:" + "a" * 64, binding=binding,
        config_snapshot={}, config_digest=binding.effective_config_digest,
    ))
    d.commit_desired_to_active()
    # A tombstoned pending desired alongside the healthy active.
    raw = yaml.safe_load((root / "mixed" / "active.yaml").read_text(encoding="utf-8"))
    raw["config_digest"] = PRE_GUARD_SENTINEL
    raw["binding"]["effective_config_digest"] = PRE_GUARD_SENTINEL
    (root / "mixed" / "desired.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    index = InstalledSpecialistIndex(specialists_dir=str(root))
    index.load()

    instance = index.get_instance("mixed")
    assert instance is not None
    assert instance.state == "active"
    assert instance.active is not None
    assert instance.desired is None
    assert instance.last_activation_error  # the desired failure is surfaced
    assert "secret-digest guard" in instance.last_activation_error


async def test_a_rejected_active_still_isolates_the_slug_as_error(tmp_path):
    """#372 (D7 companion): the active side keeps #346 semantics — a
    pre-guard active isolates the slug as state="error" (slug reserved)
    regardless of what desired holds."""
    from personality_binding import PRE_GUARD_SENTINEL
    from specialist_registry import InstalledSpecialistIndex

    root = tmp_path / "specialists"
    (root / "legacy").mkdir(parents=True)
    (root / "legacy" / "active.yaml").write_text(
        yaml.safe_dump({
            "api_version": "casa.instance-tuple/v1",
            "root": "casa/legacy@0.1.0#sha256:" + "b" * 64,
            "binding": {"effective_config_digest": PRE_GUARD_SENTINEL},
            "config_snapshot": {}, "config_digest": PRE_GUARD_SENTINEL,
        }, sort_keys=False), encoding="utf-8")

    index = InstalledSpecialistIndex(specialists_dir=str(root))
    index.load()

    instance = index.get_instance("legacy")
    assert instance is not None
    assert instance.state == "error"
    assert "secret-digest guard" in (instance.last_activation_error or "")
    assert "legacy" in index.all_collision_slugs()


# ---------------------------------------------------------------------------
# #439 — a rescan must never be observable half-done
# ---------------------------------------------------------------------------


class TestLoadPublishesAtomically:
    """``load()`` runs in a worker thread (``asyncio.to_thread``) while the
    event loop is free to serve ANOTHER role's reload, and ``reload.dispatch``
    serializes only on a per-``agent:<role>`` key — so a concurrent
    ``tools.sync_agent_role_map`` snapshots ``all_configs()`` mid-scan.

    The clear-then-refill this replaced made that snapshot the delegation
    authority missing specialists that are perfectly healthy on disk:
    ``delegate_to_agent`` refuses them as unknown, and since v0.157.0 the
    ``<delegates>`` block renders from the same map and silently drops them.
    """

    def _registry(self, tmp_path, monkeypatch, *roles):
        from specialist_registry import SpecialistRegistry

        specialists = tmp_path / "specialists"
        specialists.mkdir()
        for role in roles:
            _seed_specialist_dir(specialists, role, enabled=True)
        _use_synthetic_roles_dir(monkeypatch, tmp_path, *roles)
        return SpecialistRegistry(str(specialists),
                                  tombstone_path=str(tmp_path / "del.json"))

    async def test_snapshot_during_a_rescan_is_never_partial(
        self, tmp_path, monkeypatch,
    ):
        reg = self._registry(tmp_path, monkeypatch, "finance", "chef")
        reg.load()
        assert set(reg.all_configs()) == {"finance", "chef"}

        # Probe from INSIDE the scan's per-specialist loop — the exact window
        # a concurrent role-map sync lands in. Calling the two in sequence
        # would prove nothing.
        seen: list[set] = []
        original = reg._validate_tier2_shape

        def probe(cfg, role):
            seen.append(set(reg.all_configs()))
            return original(cfg, role)

        monkeypatch.setattr(reg, "_validate_tier2_shape", probe)
        reg.load()

        assert seen, "the probe never ran — the scan found no specialist"
        assert all(s == {"finance", "chef"} for s in seen), (
            f"a mid-scan snapshot saw a partial registry: {seen}")
        assert set(reg.all_configs()) == {"finance", "chef"}

    async def test_a_failing_rescan_does_not_empty_the_live_registry_early(
        self, tmp_path, monkeypatch,
    ):
        """A collection-level failure degrades to an empty result — but the
        live map must hold the PREVIOUS generation until that verdict is
        published, not be cleared the moment the scan starts."""
        import agent_loader

        reg = self._registry(tmp_path, monkeypatch, "finance")
        reg.load()
        assert set(reg.all_configs()) == {"finance"}

        during: list[set] = []

        def boom(*_args, **_kwargs):
            during.append(set(reg.all_configs()))
            raise agent_loader.LoadError("specialists/ is not a directory")

        monkeypatch.setattr(agent_loader, "load_all_specialists", boom)
        reg.load()

        assert during == [{"finance"}]
        # ...and the published verdict IS the empty one.
        assert reg.all_configs() == {}
        assert [n for n, _ in reg.load_failures()] == ["(collection)"]
