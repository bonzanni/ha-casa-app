"""#815 — a specialist rollback after a model change restores the retained prior
with its binding re-derived for the role in force at rollback time.

INV-SPEC-012 (declared): a rollback whose retained prior's role checksum differs
from the prior component's role materialized under the current option
resolution — with the component store's bytes still hashing to the prior's root
and the persona identity and agent id unchanged — restores the prior with its
binding re-derived for that role, committing the re-derived tuple; a prior whose
store bytes drifted, or whose persona identity or agent id moved, is refused by
name with the active tuple untouched.

Every case drives a REAL install and upgrade of an ``ha_option`` role (the
shipped fixtures of ``test_specialist_binding_rederive.py``), a REAL option
flip through the environment the role resolver reads, and the REAL
``rollback_specialist`` — library arm or the tool handler over its production
core. Assertions are tuple equality against the field-for-field re-derivation,
persona-triple bytes, marker contents, write counts and journal counts.

Specified externally (sol, red-case round, MODE: SPECIFY); acceptance runs
against the tests-only commit that carries this file.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from test_specialist_binding_rederive import (
    _cas_role, _count_compiles, _expected_rederived, _install, _overlay,
    _persona_triple_bytes, _registry_load, _scoped_activation, _tree_bytes, _upgrade,
)
from test_wholebranch_security_fixes import (
    _load_specialist_persona_role, _publish_installed_copy,
)

_SLUG = "mtg"


def _prior_and_paths(specialists_root: Path):
    from personality_binding import load_instance_tuple
    instance_path = specialists_root / _SLUG
    return (load_instance_tuple(instance_path / "active.prior.yaml"),
            instance_path, instance_path / "active.yaml", instance_path / "active.prior.yaml")


def _count_tuple_writes(monkeypatch) -> list[Path]:
    import personality_binding
    real = personality_binding.atomic_write_instance_tuple
    writes: list[Path] = []

    def counting(path, tuple_):
        writes.append(Path(path))
        return real(path, tuple_)

    monkeypatch.setattr(personality_binding, "atomic_write_instance_tuple", counting)
    return writes


def _installed_then_upgraded(tmp_path: Path, monkeypatch):
    """A v1 install and a v2 upgrade under opus: v1 is the retained prior."""
    specialists_root, agents_root, _ = _install(tmp_path, monkeypatch)
    upgraded = _upgrade(tmp_path, specialists_root, agents_root, version="0.2.0")
    assert upgraded.state == "active"
    return specialists_root, agents_root


# ---------------------------------------------------------------------------
# M1-M2 — the flip case, direct arm and tool arm
# ---------------------------------------------------------------------------


def test_m1_direct_rollback_after_a_model_flip_restores_the_rederived_prior(
        tmp_path: Path, monkeypatch) -> None:
    """M1: install v1 → upgrade v2 under opus → flip to sonnet →
    ``rollback_specialist(bundle=False)``: the committed active is the prior
    re-derived field-for-field for the sonnet role (persona triple bytes
    unchanged), and the operational-file marker carries the re-derived digest.
    RED at base: the prior binding is compiled verbatim against the sonnet role
    and refused as ``compile_failed`` on ``role_checksum``."""
    import specialist_materialize
    from specialist_install import rollback_specialist

    specialists_root, agents_root = _installed_then_upgraded(tmp_path, monkeypatch)
    prior, _instance_path, active_path, prior_path = _prior_and_paths(specialists_root)
    prior_persona_triple = _persona_triple_bytes(prior_path)

    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    sonnet_v1_role = _cas_role(specialists_root, prior)
    assert sonnet_v1_role.checksum != prior.binding.role_checksum
    expected = _expected_rederived(prior, sonnet_v1_role)

    rolled_back = rollback_specialist(
        slug=_SLUG, specialists_dir=specialists_root, agents_specialists_dir=agents_root)

    assert rolled_back.active == expected
    assert _persona_triple_bytes(active_path) == prior_persona_triple
    marker = specialist_materialize._read_binding_marker(agents_root / _SLUG)
    assert marker == {"binding_digest": expected.binding.binding_digest, "root": expected.root}


@pytest.mark.asyncio
async def test_m2_bundle_rollback_after_a_model_flip_succeeds_through_the_handler(
        tmp_path: Path, monkeypatch) -> None:
    """M2: the same state through the REAL ``specialist_rollback`` tool handler,
    its production core bound to this test's trees, the sequencer stubbed and
    the journal's real ``complete`` counted: one successful result, the active
    is the re-derived prior, one journal completed, none in progress, the ops
    directory empty. RED at base: the library raises ``compile_failed``, the
    handler returns ``ok: False`` and no rollback journal is ever completed."""
    import specialist_bundle_journal
    import specialist_install
    import specialist_install_consent
    import tools as tools_mod
    from personality_binding import InstanceDir
    from specialist_install_consent import SpecialistInstallAckStore
    from test_specialist_bundle_commit import _write_registry
    from test_tools_specialist_install import _payload
    from tools import specialist_rollback

    specialists_root, agents_root = _installed_then_upgraded(tmp_path, monkeypatch)
    prior, instance_path, _, _ = _prior_and_paths(specialists_root)
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [])
    ops_dir = tmp_path / "ops"
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")

    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    expected = _expected_rederived(prior, _cas_role(specialists_root, prior))

    real_rollback = specialist_install.rollback_specialist

    def _bound(*, slug, bundle=False, acks=None, **_ignored):
        assert bundle is True
        return real_rollback(
            slug=slug, bundle=True, acks=acks, specialists_dir=specialists_root,
            agents_specialists_dir=agents_root, registry_path=registry_path,
            plugin_store_root=tmp_path / "store", ops_dir=ops_dir)

    async def _seq(slug, *, removed_artifact_ids, targets_removed):
        return {"ok": True, "reloaded": [], "verify": {}, "reload_errors": [],
                "removed_artifact_ids": list(removed_artifact_ids)}

    completed: list[Path] = []
    real_complete = specialist_bundle_journal.complete

    def _counting_complete(path):
        completed.append(Path(path))
        return real_complete(path)

    monkeypatch.setattr(specialist_install, "rollback_specialist", _bound)
    monkeypatch.setattr(specialist_install_consent, "SpecialistInstallAckStore",
                        lambda *a, **k: acks)
    monkeypatch.setattr(tools_mod, "_bundle_reload_and_verify", _seq)
    monkeypatch.setattr(specialist_bundle_journal, "complete", _counting_complete)

    payload = _payload(await specialist_rollback.handler({"slug": _SLUG}))

    assert sum(1 for _ in [payload] if payload.get("ok") is True) == 1, payload
    assert InstanceDir(instance_path).active() == expected
    assert len(completed) == 1
    in_progress = [p for p in ops_dir.glob("*.json")
                   if json.loads(p.read_text()).get("state") == "in-progress"]
    assert len(in_progress) == 0
    assert list(ops_dir.glob("*.json")) == []


# ---------------------------------------------------------------------------
# M3-M5 — the preserved behaviours: no-flip byte identity, drift and identity refusals
# ---------------------------------------------------------------------------


def test_m3_a_rollback_without_a_flip_is_byte_identical_and_rederives_nothing(
        tmp_path: Path, monkeypatch) -> None:
    """M3: with the option resolution unchanged the restored active is the
    retained prior's bytes exactly and the re-derivation helper is never
    called. GREEN at base; pins the preserved common path."""
    import personality_binding
    from specialist_install import rollback_specialist

    specialists_root, agents_root = _installed_then_upgraded(tmp_path, monkeypatch)
    prior_before, _, active_path, prior_path = _prior_and_paths(specialists_root)
    prior_bytes_before = prior_path.read_bytes()

    calls: list[int] = []
    real = personality_binding.rederive_binding_for_role

    def counting(**kwargs):
        calls.append(1)
        return real(**kwargs)

    monkeypatch.setattr(personality_binding, "rederive_binding_for_role", counting)
    rolled_back = rollback_specialist(
        slug=_SLUG, specialists_dir=specialists_root, agents_specialists_dir=agents_root)

    assert active_path.read_bytes() == prior_bytes_before
    assert rolled_back.active == prior_before
    assert len(calls) == 0


def test_m4_a_prior_whose_store_bytes_drifted_is_refused_by_name_with_nothing_written(
        tmp_path: Path, monkeypatch) -> None:
    """M4 (L1): the prior's component-store manifest bytes are altered so the
    recomputed install root digest differs from the prior's root while the
    role and persona material still parse; after a flip the rollback refuses
    ``compile_failed`` naming BOTH the prior's root and the drifted digest,
    and writes nothing. RED at base: the refusal is incidental — the verbatim
    compile's ``role_checksum`` mismatch — and its detail names neither root."""
    from specialist_install import (
        SpecialistInstallError, cas_store_dir, compute_install_root_digest,
        load_specialist_component, parse_component_root, resolve_dependency_closure,
        rollback_specialist,
    )

    specialists_root, agents_root = _installed_then_upgraded(tmp_path, monkeypatch)
    prior, _, active_path, _ = _prior_and_paths(specialists_root)
    _, _, prior_checksum = parse_component_root(prior.root)
    cas_dir = cas_store_dir(prior_checksum, store_root=specialists_root / "store")
    manifest_path = cas_dir / "manifest.json"
    cas_dir.chmod(0o755)                      # the store is published read-only
    manifest_path.chmod(0o644)
    manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
    component = load_specialist_component(cas_dir, manifest_path)
    deps = resolve_dependency_closure(component, cas_dir)
    drifted_digest = compute_install_root_digest(
        component, deps, manifest_bytes=manifest_path.read_bytes())
    assert drifted_digest != prior_checksum
    active_before = active_path.read_bytes()

    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    writes = _count_tuple_writes(monkeypatch)
    with pytest.raises(SpecialistInstallError) as raised:
        rollback_specialist(
            slug=_SLUG, specialists_dir=specialists_root, agents_specialists_dir=agents_root)

    assert raised.value.kind == "compile_failed"
    assert prior.root in raised.value.detail
    assert drifted_digest in raised.value.detail
    assert len(writes) == 0
    assert active_path.read_bytes() == active_before


def test_m5_a_prior_whose_agent_id_moved_is_refused_naming_the_agent_id(
        tmp_path: Path, monkeypatch) -> None:
    """M5 (L3): the retained prior's binding is forged to another agent id (its
    digest recomputed so the tuple is structurally valid); after a flip the
    rollback refuses ``compile_failed`` naming ``stable_agent_id`` and writes
    nothing — re-derivation never launders a moved identity. GREEN at base;
    pins the preserved refusal."""
    from personality_binding import atomic_write_instance_tuple, compute_binding_digest
    from specialist_install import SpecialistInstallError, rollback_specialist

    specialists_root, agents_root = _installed_then_upgraded(tmp_path, monkeypatch)
    prior, _, active_path, prior_path = _prior_and_paths(specialists_root)
    b = prior.binding
    forged_digest = compute_binding_digest(
        stable_agent_id="specialist:other", role_checksum=b.role_checksum,
        persona_id=b.persona_id, persona_version=b.persona_version,
        persona_checksum=b.persona_checksum,
        compiler_schema_version=b.compiler_schema_version,
        dependency_digests=b.dependency_digests,
        effective_config_digest=b.effective_config_digest)
    forged = dataclasses.replace(prior, binding=dataclasses.replace(
        b, stable_agent_id="specialist:other", binding_digest=forged_digest))
    atomic_write_instance_tuple(prior_path, forged)
    active_before = active_path.read_bytes()

    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    writes = _count_tuple_writes(monkeypatch)
    with pytest.raises(SpecialistInstallError) as raised:
        rollback_specialist(
            slug=_SLUG, specialists_dir=specialists_root, agents_specialists_dir=agents_root)

    assert raised.value.kind == "compile_failed"
    assert "stable_agent_id" in raised.value.detail
    assert len(writes) == 0
    assert active_path.read_bytes() == active_before


# ---------------------------------------------------------------------------
# M6-M7 — the override-mode prior, and the next load's lock-free common path
# ---------------------------------------------------------------------------


def test_m6_an_override_prior_after_a_model_flip_is_restored_with_its_override_identity(
        tmp_path: Path, monkeypatch) -> None:
    """M6: install under opus → apply an override persona → upgrade (which
    preserves the override, so the retained prior is override-bound) → flip →
    rollback: the active is the override prior re-derived, with its mode,
    override source, persona triple and root carried. RED at base: the stale
    override prior is compiled verbatim and refused on ``role_checksum``."""
    from persona_install import apply_persona_override
    from personality_binding import InstanceDir
    from specialist_install import rollback_specialist

    specialists_root, agents_root, _ = _install(tmp_path, monkeypatch)
    instance_path = specialists_root / _SLUG
    persona, _ = _load_specialist_persona_role(specialists_root, _SLUG)
    _publish_installed_copy(persona, specialists_root, _SLUG, tmp_path, monkeypatch)
    opus_role = _cas_role(specialists_root, InstanceDir(instance_path).active())
    overridden = apply_persona_override(
        target_role_id=f"specialist:{_SLUG}", persona=persona, role=opus_role,
        instance_dir_root=instance_path, candidate_validator=lambda p, b: None)
    assert overridden.binding.mode == "override"
    upgraded = _upgrade(tmp_path, specialists_root, agents_root, version="0.2.0")
    assert upgraded.state == "active"
    override_prior, _, _, prior_path = _prior_and_paths(specialists_root)
    assert override_prior.binding.mode == "override"
    prior_persona_triple = (override_prior.binding.persona_id,
                            override_prior.binding.persona_version,
                            override_prior.binding.persona_checksum)

    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    sonnet_prior_role = _cas_role(specialists_root, override_prior)
    expected = _expected_rederived(override_prior, sonnet_prior_role)

    rolled_back = rollback_specialist(
        slug=_SLUG, specialists_dir=specialists_root, agents_specialists_dir=agents_root)
    active = rolled_back.active

    assert active == expected
    assert active.binding.mode == "override"
    assert active.binding.override_source == override_prior.binding.override_source
    assert (active.binding.persona_id, active.binding.persona_version,
            active.binding.persona_checksum) == prior_persona_triple
    assert active.root == override_prior.root


class _CountingLock:
    """A stand-in for a module lock that counts entries and forwards to the
    real lock, so a lock the code takes is a lock this test sees."""

    def __init__(self, real) -> None:
        self._real = real
        self.entries = 0

    def __enter__(self):
        self.entries += 1
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def acquire(self, *a, **k):
        self.entries += 1
        return self._real.acquire(*a, **k)

    def release(self):
        return self._real.release()

    def locked(self):
        return self._real.locked()


def test_m7_the_next_load_of_the_rederived_active_takes_no_lock_and_writes_nothing(
        tmp_path: Path, monkeypatch) -> None:
    """M7: the exact M1 output seeded as the active is loaded under the same
    sonnet role through the real overlay + registry path: zero lifecycle-lock
    entries, zero materialize-lock entries, zero tuple writes, the instance
    tree byte-identical, exactly one compile. GREEN at base for an
    already-current binding; pins the preserved common path."""
    import personality_binding
    import specialist_materialize
    from personality_binding import InstanceDir, atomic_write_instance_tuple

    specialists_root, agents_root = _installed_then_upgraded(tmp_path, monkeypatch)
    prior, instance_path, active_path, _ = _prior_and_paths(specialists_root)
    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    # Seed M1's exact output by hand, so this case pins the common path on its
    # own and is green at the base whatever the rollback arm does there.
    seeded = _expected_rederived(prior, _cas_role(specialists_root, prior))
    atomic_write_instance_tuple(active_path, seeded)
    assert InstanceDir(instance_path).active() == seeded

    _scoped_activation(monkeypatch, specialists_root)
    index, roles_dir = _overlay(specialists_root, agents_root)   # takes the lock itself
    tree_before = _tree_bytes(instance_path)

    materialize = _CountingLock(personality_binding.MATERIALIZE_LOCK)
    monkeypatch.setattr(personality_binding, "MATERIALIZE_LOCK", materialize)
    monkeypatch.setattr(specialist_materialize, "MATERIALIZE_LOCK", materialize)
    lifecycle = None
    if getattr(personality_binding, "SPECIALIST_LIFECYCLE_LOCK", None) is not None:
        lifecycle = _CountingLock(personality_binding.SPECIALIST_LIFECYCLE_LOCK)
        monkeypatch.setattr(personality_binding, "SPECIALIST_LIFECYCLE_LOCK", lifecycle)
        if getattr(specialist_materialize, "SPECIALIST_LIFECYCLE_LOCK", None) is not None:
            monkeypatch.setattr(specialist_materialize, "SPECIALIST_LIFECYCLE_LOCK", lifecycle)
    writes = _count_tuple_writes(monkeypatch)
    compiles = _count_compiles(monkeypatch)

    _, counts = _registry_load(tmp_path, index, agents_root, roles_dir)

    assert counts == (1, 1, 0)
    assert (lifecycle.entries if lifecycle is not None else 0) == 0
    assert materialize.entries == 0
    assert len(writes) == 0
    assert _tree_bytes(instance_path) == tree_before
    assert len(compiles) == 1
