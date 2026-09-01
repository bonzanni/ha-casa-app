"""#597 — an installed specialist survives a model change.

The role checksum deliberately covers the RESOLVED model
(``role_slot.normalize_role_for_checksum``), so an HA ``primary_agent_model``
flip moves every installed specialist's role checksum. The specialist load
path compiled the persisted binding as-is, so every installed specialist was
dropped as a binding-activation failure until re-installed. These tests pin
the re-derivation arm: the loader re-derives the binding for the current role
when — and only when — the component root, the persona identity triple, the
configuration and the dependency closure are unchanged, writes it back IN
PLACE (same generation: ``active.prior.yaml`` and ``desired.yaml`` survive),
writes nothing on a validation-only or disabled load, and still refuses any
identity movement (INV-PERS-016).

Every test drives a REAL install (``commit_specialist_install``), a REAL
``InstanceDir`` under ``tmp_path`` and, where the registry is involved, the
REAL overlay/registry path (``current_specialist_roles_dir`` +
``SpecialistRegistry.load``). Assertions are counts and bytes, never "no
exception".
"""
from __future__ import annotations

import ast
import dataclasses
import json
import os
from pathlib import Path

import pytest
import yaml

from test_agent_loader import (
    _policies_file, _seed_remaining_residents, _seed_resident, _seed_role_artifact, _w,
)
from test_specialist_install import _staged_inspection, _write_component

_HA_MODEL = {
    "source": "ha_option",
    "option": "primary_agent_model",
    "default": "opus",
    "allowed": ["opus", "sonnet"],
}
_SLUG = "mtg"


# ---------------------------------------------------------------------------
# Helpers — real install, real overlay, real registry
# ---------------------------------------------------------------------------


def _ack(inspection, tmp_path: Path):
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id,
                version=inspection.version, component_checksum=inspection.root_digest,
                slug=inspection.slug)
    return acks


def _roots(tmp_path: Path, *, agents_specialists_dir: Path | None = None):
    specialists_root = tmp_path / "specialists"
    agents_root = agents_specialists_dir or (tmp_path / "config" / "agents" / "specialists")
    return specialists_root, agents_root


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
             agents_specialists_dir: Path | None = None):
    """A committed, ACTIVE ``mtg`` install of an ``ha_option`` role under opus."""
    from specialist_install import commit_specialist_install

    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "opus")
    specialists_root, agents_root = _roots(tmp_path, agents_specialists_dir=agents_specialists_dir)
    inspection = _staged_inspection(tmp_path, model=_HA_MODEL)
    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(),
        acks=_ack(inspection, tmp_path),
        specialists_dir=specialists_root, agents_specialists_dir=agents_root,
    )
    assert instance.state == "active"
    return specialists_root, agents_root, inspection


def _inspection_for(staged: Path, *, version: str, required: tuple[str, ...] = ()):
    """An inspection for a component staged at *staged*, at *version*, whose
    config schema requires *required* (non-secret) keys."""
    from specialist_component import compute_component_checksum, load_specialist_component
    from specialist_install import (
        InspectionResult, compute_install_root_digest, resolve_dependency_closure,
    )

    manifest_path = staged / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = version
    (staged / "config-schema.json").write_text(
        json.dumps({"required": list(required), "secret_names": []}), encoding="utf-8")
    files = {
        "role/role.yaml": (staged / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (staged / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (staged / "config-schema.json").read_bytes(),
    }
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    component = load_specialist_component(staged, manifest_path)
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=manifest_path.read_bytes())
    return InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest,
        mission=str(component.role.role["mission"]),
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=required, required_secret_names=(), dependencies=deps,
        staged_dir=staged,
    )


def _upgrade(tmp_path: Path, specialists_root: Path, agents_root: Path, *, version: str,
             required: tuple[str, ...] = ()):
    from specialist_install import upgrade_specialist

    staged = _write_component(tmp_path / f"staged-{version}", slug=_SLUG, model=_HA_MODEL)
    inspection = _inspection_for(staged, version=version, required=required)
    return upgrade_specialist(
        slug=_SLUG, inspection=inspection, config={}, secret_names_provided=frozenset(),
        acks=_ack(inspection, tmp_path),
        specialists_dir=specialists_root, agents_specialists_dir=agents_root,
    )


def _overlay(specialists_root: Path, agents_root: Path):
    """Build the REAL roles overlay + op-file self-heal, as boot/reload do."""
    import specialist_materialize
    from specialist_registry import InstalledSpecialistIndex

    index = InstalledSpecialistIndex(specialists_dir=str(specialists_root))
    index.load()
    roles_dir = specialist_materialize.current_specialist_roles_dir(
        installed_index=index, specialists_dir=specialists_root,
        agents_specialists_dir=agents_root)
    return index, Path(roles_dir)


def _scoped_activation(monkeypatch: pytest.MonkeyPatch, specialists_root: Path,
                       *, commit_values: list | None = None):
    """Route the loader's activation at the tmp specialists root. Any
    ``commit=`` the loader forwards is preserved (and recorded); at the base,
    whose activation has no such keyword, the call is retried without it so a
    behavioural test fails on its COUNTS, not incidentally on the signature."""
    import specialist_install

    real = specialist_install.activate_binding_for_config

    def scoped(cfg, **kwargs):
        kwargs["specialists_root"] = specialists_root
        if commit_values is not None and "commit" in kwargs:
            commit_values.append(kwargs["commit"])
        try:
            return real(cfg, **kwargs)
        except TypeError as exc:
            if "commit" in kwargs and "commit" in str(exc):
                kwargs.pop("commit")
                return real(cfg, **kwargs)
            raise

    monkeypatch.setattr(specialist_install, "activate_binding_for_config", scoped)


def _registry_load(tmp_path: Path, index, agents_root: Path, roles_dir: Path):
    from job_registry import JobRegistry
    from specialist_registry import SpecialistRegistry

    registry = SpecialistRegistry(
        str(agents_root), job_registry=JobRegistry(str(tmp_path / "jobs.json")))
    registry.load(roles_dir=str(roles_dir))
    index.load()
    counts = (len(index.installed_slugs()), len(registry.all_configs()),
              len(registry.load_failures()))
    return registry, counts


def _tree_bytes(instance_path: Path) -> dict[str, bytes]:
    return {str(p.relative_to(instance_path)): p.read_bytes()
            for p in sorted(instance_path.rglob("*")) if p.is_file()}


def _persona_triple_bytes(active_path: Path) -> bytes:
    return b"".join(
        line for line in active_path.read_bytes().splitlines(keepends=True)
        if line.lstrip().startswith((b"persona_id:", b"persona_version:", b"persona_checksum:")))


def _expected_rederived(observed, role):
    from personality_binding import compute_binding_digest

    b = observed.binding
    digest = compute_binding_digest(
        stable_agent_id=b.stable_agent_id, role_checksum=role.checksum,
        persona_id=b.persona_id, persona_version=b.persona_version,
        persona_checksum=b.persona_checksum,
        compiler_schema_version=b.compiler_schema_version,
        dependency_digests=b.dependency_digests,
        effective_config_digest=b.effective_config_digest)
    return dataclasses.replace(
        observed, binding=dataclasses.replace(b, role_checksum=role.checksum, binding_digest=digest))


def _cas_role(specialists_root: Path, active):
    """The installed component's role, materialized with the LIVE options —
    exactly what the loader derives from the overlay."""
    from role_artifact import load_role_artifact
    from role_slot import _ha_model_options, materialize_role
    from specialist_install import cas_store_dir, parse_component_root

    _, _, checksum = parse_component_root(active.root)
    cas_dir = cas_store_dir(checksum, store_root=specialists_root / "store")
    return materialize_role(source=load_role_artifact(cas_dir / "role"), options=_ha_model_options())


def _count_compiles(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    import prompt_compiler

    real = prompt_compiler.compile_prompt_bundle
    calls: list[int] = []

    def counting(**kwargs):
        calls.append(1)
        return real(**kwargs)

    monkeypatch.setattr(prompt_compiler, "compile_prompt_bundle", counting)
    return calls


# ---------------------------------------------------------------------------
# 1-2. The defect: a model flip drops the specialist; now it is re-derived
# ---------------------------------------------------------------------------


def test_model_flip_rederives_default_binding_once_and_keeps_specialist_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reproduction from the survey, through the REAL install → overlay →
    registry path: (installed, active, failed) is (1, 1, 0) under opus, and
    stays (1, 1, 0) after the flip to sonnet — pre-fix it was (1, 0, 1). The
    on-disk binding is the field-for-field re-derivation (only role_checksum
    and its derived binding_digest move; the persona triple bytes are
    unchanged) and a third load is a byte-for-byte no-op."""
    from personality_binding import InstanceDir

    specialists_root, agents_root, _ = _install(tmp_path, monkeypatch)
    _scoped_activation(monkeypatch, specialists_root)
    index, roles_dir = _overlay(specialists_root, agents_root)
    instance_path = specialists_root / _SLUG
    active_path = instance_path / "active.yaml"

    _, counts = _registry_load(tmp_path, index, agents_root, roles_dir)
    assert counts == (1, 1, 0)
    observed = InstanceDir(instance_path).active()
    opus_bytes = active_path.read_bytes()
    opus_persona = _persona_triple_bytes(active_path)

    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    sonnet_role = _cas_role(specialists_root, observed)
    assert sonnet_role.checksum != observed.binding.role_checksum  # the flip moved it
    expected = _expected_rederived(observed, sonnet_role)

    registry, counts = _registry_load(tmp_path, index, agents_root, roles_dir)
    assert counts == (1, 1, 0), registry.load_failures()
    repaired = InstanceDir(instance_path).active()
    assert repaired == expected
    assert repaired.binding == expected.binding
    assert active_path.read_bytes() != opus_bytes
    assert _persona_triple_bytes(active_path) == opus_persona
    cfg = registry.get(_SLUG)
    assert cfg.binding == expected.binding
    assert cfg.compiled_prompt_bundle.binding_digest == expected.binding.binding_digest

    repaired_bytes = active_path.read_bytes()
    repaired_tree = _tree_bytes(instance_path)
    _, third_counts = _registry_load(tmp_path, index, agents_root, roles_dir)
    assert third_counts == (1, 1, 0)
    assert active_path.read_bytes() == repaired_bytes
    assert _tree_bytes(instance_path) == repaired_tree


def test_model_flip_rederives_override_binding_from_stored_override_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An override-bound specialist re-derives too, and the re-derived binding
    carries the STORED override identity (mode, override_source, persona
    triple, dependency and config digests) — never one rebuilt from the pack
    the loader happened to load."""
    from persona_install import apply_persona_override
    from persona_pack import load_persona_pack
    from personality_binding import InstanceDir
    from test_persona_install import _write_persona_repo

    monkeypatch.setenv("CASA_CONFIG_DIR", str(tmp_path / "cfgroot"))
    specialists_root, agents_root, _ = _install(tmp_path, monkeypatch)
    _scoped_activation(monkeypatch, specialists_root)
    instance_path = specialists_root / _SLUG

    override_dir = tmp_path / "cfgroot" / "personas" / "casa/judge" / "0.1.0"
    override_dir.parent.mkdir(parents=True, exist_ok=True)
    _write_persona_repo(override_dir, persona_id="casa/judge")
    persona = load_persona_pack(override_dir / "pack", override_dir / "manifest.json")
    opus_role = _cas_role(specialists_root, InstanceDir(instance_path).active())
    apply_persona_override(
        target_role_id=f"specialist:{_SLUG}", persona=persona, role=opus_role,
        instance_dir_root=instance_path, candidate_validator=lambda p, b: None)

    index, roles_dir = _overlay(specialists_root, agents_root)
    _, counts = _registry_load(tmp_path, index, agents_root, roles_dir)
    assert counts == (1, 1, 0)
    observed = InstanceDir(instance_path).active()
    assert observed.binding.mode == "override"

    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    sonnet_role = _cas_role(specialists_root, observed)
    expected = _expected_rederived(observed, sonnet_role)
    registry, counts = _registry_load(tmp_path, index, agents_root, roles_dir)
    assert counts == (1, 1, 0), registry.load_failures()
    repaired = InstanceDir(instance_path).active()
    assert repaired.binding.mode == "override"
    assert repaired.root == observed.root
    assert repaired.binding.override_source == observed.binding.override_source
    assert repaired.binding.persona_id == observed.binding.persona_id
    assert repaired.binding.persona_version == observed.binding.persona_version
    assert repaired.binding.persona_checksum == observed.binding.persona_checksum
    assert repaired.binding.dependency_digests == observed.binding.dependency_digests
    assert repaired.binding.effective_config_digest == observed.binding.effective_config_digest
    assert repaired.binding.role_checksum == sonnet_role.checksum
    assert repaired.binding.binding_digest == expected.binding.binding_digest
    assert repaired == expected


# ---------------------------------------------------------------------------
# 3-5. What is REFUSED, and refused before any write
# ---------------------------------------------------------------------------


def _forge_active(active_path: Path, observed, **binding_changes):
    """Rewrite active.yaml with a schema-valid, digest-consistent binding whose
    named fields moved — the shape a tampered or stale tuple has on disk."""
    from personality_binding import InstanceTuple, atomic_write_instance_tuple, compute_binding_digest

    b = dataclasses.replace(observed.binding, **binding_changes)
    b = dataclasses.replace(b, binding_digest=compute_binding_digest(
        stable_agent_id=b.stable_agent_id, role_checksum=b.role_checksum,
        persona_id=b.persona_id, persona_version=b.persona_version,
        persona_checksum=b.persona_checksum, compiler_schema_version=b.compiler_schema_version,
        dependency_digests=b.dependency_digests, effective_config_digest=b.effective_config_digest))
    forged = InstanceTuple(root=observed.root, binding=b, config_snapshot=observed.config_snapshot,
                           config_digest=observed.config_digest)
    atomic_write_instance_tuple(active_path, forged)
    return forged


@pytest.mark.parametrize("tamper", ["persona_triple", "stable_agent_id"])
def test_identity_movement_is_refused_without_rederive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str,
) -> None:
    """Preservation pin + mutation guard: a binding whose persona identity or
    agent id differs from what the loader resolved is refused, never
    re-derived, and the on-disk tuple is not touched. (Green at the base by
    design — this pins what the new arm must NOT launder.)"""
    from personality_binding import InstanceDir

    specialists_root, agents_root, _ = _install(tmp_path, monkeypatch)
    _scoped_activation(monkeypatch, specialists_root)
    index, roles_dir = _overlay(specialists_root, agents_root)
    instance_path = specialists_root / _SLUG
    active_path = instance_path / "active.yaml"
    observed = InstanceDir(instance_path).active()
    if tamper == "persona_triple":
        changes = {"persona_id": "casa/other", "persona_version": "9.9.9",
                   "persona_checksum": "sha256:" + "a" * 64}
        expected_names = ("persona_id", "persona_version", "persona_checksum")
    else:
        changes = {"stable_agent_id": "specialist:other"}
        expected_names = ("stable_agent_id",)
    _forge_active(active_path, observed, **changes)
    forged_bytes = active_path.read_bytes()

    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    registry, counts = _registry_load(tmp_path, index, agents_root, roles_dir)
    assert counts == (1, 0, 1)
    assert active_path.read_bytes() == forged_bytes
    (name, text), = registry.load_failures()
    assert name == _SLUG
    assert "does not match the compiled role+persona" in text
    for field in expected_names:
        assert field in text


def test_stale_roles_overlay_is_refused_at_l2_before_compile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #331(d) shape: the role the loader compiled is NOT the installed
    component's role artifact (here: an overlay whose doctrine drifted from
    the CAS). L2 refuses before the compiler runs and nothing is written —
    pre-fix the compiler ran (compile_calls == 1) and raised."""
    specialists_root, agents_root, _ = _install(tmp_path, monkeypatch)
    _scoped_activation(monkeypatch, specialists_root)
    index, roles_dir = _overlay(specialists_root, agents_root)
    instance_path = specialists_root / _SLUG
    doctrine = roles_dir / "specialist" / _SLUG / "doctrine.md"
    doctrine.write_text(doctrine.read_text(encoding="utf-8") + "\nDrifted overlay line.\n",
                        encoding="utf-8")
    before = _tree_bytes(instance_path)
    compile_calls = _count_compiles(monkeypatch)

    registry, counts = _registry_load(tmp_path, index, agents_root, roles_dir)
    assert counts == (1, 0, 1)
    assert len(compile_calls) == 0
    assert _tree_bytes(instance_path) == before
    assert len(registry.load_failures()) == 1


@pytest.mark.parametrize("mutation", ["digest", "component_id", "version"])
def test_l1_refuses_root_or_cas_identity_mismatch_before_compile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    """L1: the install root digest recomputed from the CAS bytes must equal
    the tuple root's suffix, and the component id/version must equal the
    root's — else the re-derive arm refuses before compiling and writes
    nothing. Pre-fix nothing checked the root identity: the compiler ran."""
    from personality_binding import InstanceDir, InstanceTuple, atomic_write_instance_tuple
    from specialist_install import cas_store_dir, parse_component_root

    specialists_root, agents_root, _ = _install(tmp_path, monkeypatch)
    _scoped_activation(monkeypatch, specialists_root)
    index, roles_dir = _overlay(specialists_root, agents_root)
    instance_path = specialists_root / _SLUG
    active_path = instance_path / "active.yaml"
    observed = InstanceDir(instance_path).active()
    component_id, version, suffix = parse_component_root(observed.root)
    if mutation == "digest":
        # The CAS store is published read-only; the tamper has to open it up
        # first — exactly what an in-place edit on the box would do.
        manifest = cas_store_dir(suffix, store_root=specialists_root / "store") / "manifest.json"
        os.chmod(manifest.parent, 0o755)
        os.chmod(manifest, 0o644)
        manifest.write_bytes(manifest.read_bytes() + b"\n")
    else:
        root = (f"other@{version}#{suffix}" if mutation == "component_id"
                else f"{component_id}@9.9.9#{suffix}")
        atomic_write_instance_tuple(active_path, InstanceTuple(
            root=root, binding=observed.binding, config_snapshot=observed.config_snapshot,
            config_digest=observed.config_digest))

    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    before = _tree_bytes(instance_path)
    compile_calls = _count_compiles(monkeypatch)
    registry, counts = _registry_load(tmp_path, index, agents_root, roles_dir)
    assert counts == (1, 0, 1)
    assert len(compile_calls) == 0
    assert _tree_bytes(instance_path) == before
    assert len(registry.load_failures()) == 1


# ---------------------------------------------------------------------------
# 6. Same generation: the retained prior and a staged candidate survive
# ---------------------------------------------------------------------------


def _three_generations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """v1 installed → upgraded to v2 (prior = v1) → a v3 upgrade whose required
    config is missing (desired = v3 placeholder, active = v2)."""
    specialists_root, agents_root, _ = _install(tmp_path, monkeypatch)
    v2 = _upgrade(tmp_path, specialists_root, agents_root, version="0.2.0")
    assert v2.state == "active"
    v3 = _upgrade(tmp_path, specialists_root, agents_root, version="0.3.0",
                  required=("timezone",))
    assert v3.state == "pending-configuration"
    instance_path = specialists_root / _SLUG
    assert (instance_path / "active.prior.yaml").is_file()
    assert (instance_path / "desired.yaml").is_file()
    return specialists_root, agents_root, instance_path


def test_rederive_rewrites_only_active_and_preserves_pending_and_prior_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model-flip re-derivation is not a generation change: only active.yaml
    is rewritten. INV-SPEC-003's rollback target (active.prior.yaml) and a
    pending-configuration candidate (desired.yaml) are byte-identical after
    it — a stage+commit rewrite would have rotated the prior and overwritten
    the staged candidate."""
    specialists_root, agents_root, instance_path = _three_generations(tmp_path, monkeypatch)
    _scoped_activation(monkeypatch, specialists_root)
    index, roles_dir = _overlay(specialists_root, agents_root)
    active_path = instance_path / "active.yaml"
    desired_path = instance_path / "desired.yaml"
    prior_path = instance_path / "active.prior.yaml"
    _, counts = _registry_load(tmp_path, index, agents_root, roles_dir)
    assert counts == (1, 1, 0)
    before = _tree_bytes(instance_path)

    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    registry, counts = _registry_load(tmp_path, index, agents_root, roles_dir)
    assert counts == (1, 1, 0), registry.load_failures()
    after = _tree_bytes(instance_path)
    assert active_path.read_bytes() != before["active.yaml"]
    assert desired_path.read_bytes() == before["desired.yaml"]
    assert prior_path.read_bytes() == before["active.prior.yaml"]
    assert {k: v for k, v in after.items() if k != "active.yaml"} == \
           {k: v for k, v in before.items() if k != "active.yaml"}


# ---------------------------------------------------------------------------
# 7. Concurrency: a competitor that wins between compile and lock is retained
# ---------------------------------------------------------------------------


def test_competitor_winning_between_compile_and_lock_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Between the compile proof and the in-lock write another writer commits
    a different active tuple (a config-only upgrade here). The re-derivation
    re-observes under the lock, sees the tuple changed, refuses, and the
    competitor's bytes are intact. Pre-fix (with the compiler's raise
    suppressed) the stale binding was simply accepted."""
    import prompt_compiler
    from types import SimpleNamespace
    from personality_binding import (
        InstanceDir, atomic_write_instance_tuple, compute_binding_digest,
        compute_effective_config_digest, make_instance_tuple,
    )

    specialists_root, agents_root, _ = _install(tmp_path, monkeypatch)
    _scoped_activation(monkeypatch, specialists_root)
    index, roles_dir = _overlay(specialists_root, agents_root)
    instance_path = specialists_root / _SLUG
    active_path = instance_path / "active.yaml"
    observed = InstanceDir(instance_path).active()
    desired_before = (instance_path / "desired.yaml").exists()
    prior_before = (instance_path / "active.prior.yaml").exists()

    snapshot = {"timezone": "Europe/Rome"}
    ecd = compute_effective_config_digest(snapshot)
    b = observed.binding
    competitor_binding = dataclasses.replace(b, effective_config_digest=ecd, binding_digest=compute_binding_digest(
        stable_agent_id=b.stable_agent_id, role_checksum=b.role_checksum, persona_id=b.persona_id,
        persona_version=b.persona_version, persona_checksum=b.persona_checksum,
        compiler_schema_version=b.compiler_schema_version, dependency_digests=b.dependency_digests,
        effective_config_digest=ecd))
    competitor = make_instance_tuple(root=observed.root, binding=competitor_binding, config_snapshot=snapshot)

    compile_calls: list[int] = []
    competitor_bytes: list[bytes] = []

    def racing_compile(**kwargs):
        compile_calls.append(1)
        atomic_write_instance_tuple(active_path, competitor)
        competitor_bytes.append(active_path.read_bytes())
        return SimpleNamespace(binding_digest=kwargs["binding"].binding_digest)

    monkeypatch.setattr(prompt_compiler, "compile_prompt_bundle", racing_compile)
    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    _, counts = _registry_load(tmp_path, index, agents_root, roles_dir)
    assert len(compile_calls) == 1
    assert counts == (1, 0, 1)
    assert active_path.read_bytes() == competitor_bytes[0]
    assert InstanceDir(instance_path).active() == competitor
    assert (instance_path / "desired.yaml").exists() == desired_before
    assert (instance_path / "active.prior.yaml").exists() == prior_before


# ---------------------------------------------------------------------------
# 8-9. Validation-only and disabled loads write nothing
# ---------------------------------------------------------------------------


def _resident_repo(tmp_path: Path, roles_dir: Path) -> Path:
    """The bootable single-resident repo `TestValidateConfigRepoIssue338` uses,
    with the fixture resident role artifacts seeded under *roles_dir*."""
    repo = tmp_path / "addon_configs" / "casa"
    resident_dir = _seed_resident(repo / "agents", "assistant")
    _policies_file(repo / "policies")
    _seed_role_artifact(roles_dir, "resident", "assistant")
    _w(resident_dir / "runtime.yaml", """\
        schema_version: 1
        kind: resident
        model: {source: fixed, value: sonnet}
        tools:
          allowed: [Read, Write]
        channels: [telegram]
    """)
    _seed_remaining_residents(repo, roles_dir)
    return repo


def test_validate_config_repo_rederives_in_memory_with_zero_writes_or_failure_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-commit replay (#338) reaches specialists through
    load_all_specialists. With a stale binding it must compile the re-derived
    candidate in memory (the specialist is reported loadable, as boot will
    find it) and write NOTHING — the flag travels through all three
    signatures as False. Pre-fix the flag never reached the specialist path."""
    import agent_loader
    from agent_loader import validate_config_repo

    repo = tmp_path / "addon_configs" / "casa"
    specialists_root, agents_root, _ = _install(
        tmp_path, monkeypatch, agents_specialists_dir=repo / "agents" / "specialists")
    commit_values: list = []
    _scoped_activation(monkeypatch, specialists_root, commit_values=commit_values)
    index, roles_dir = _overlay(specialists_root, agents_root)
    _resident_repo(tmp_path, roles_dir)
    instance_path = specialists_root / _SLUG

    scan_records: list = []
    captured: dict = {}
    real_scan = agent_loader.load_all_specialists

    def recording_scan(specialists_dir, **kwargs):
        found, failed = real_scan(specialists_dir, **kwargs)
        scan_records.append((kwargs.get("binding_commit"), len(found), len(failed)))
        captured.update(found)
        return found, failed

    monkeypatch.setattr(agent_loader, "load_all_specialists", recording_scan)

    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    from personality_binding import InstanceDir
    sonnet_role = _cas_role(specialists_root, InstanceDir(instance_path).active())
    before = _tree_bytes(instance_path)
    errors = validate_config_repo(str(repo), roles_dir=str(roles_dir))
    assert errors == []
    assert commit_values == [False]
    assert scan_records == [(False, 1, 0)]
    assert len(captured) == 1
    assert captured[_SLUG].binding.role_checksum == sonnet_role.checksum
    assert captured[_SLUG].compiled_prompt_bundle is not None
    assert _tree_bytes(instance_path) == before


def test_disabled_specialist_rederives_only_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A specialist the operator disabled is neither served nor re-materialized
    on disk: it is reported disabled (not failed), its in-memory config carries
    the re-derived binding, and the instance tree is byte-identical."""
    import agent_loader
    from personality_binding import InstanceDir

    specialists_root, agents_root, _ = _install(tmp_path, monkeypatch)
    _scoped_activation(monkeypatch, specialists_root)
    index, roles_dir = _overlay(specialists_root, agents_root)
    instance_path = specialists_root / _SLUG
    runtime_path = agents_root / _SLUG / "runtime.yaml"
    runtime = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    runtime["enabled"] = False
    runtime_path.write_text(yaml.safe_dump(runtime, sort_keys=False), encoding="utf-8")

    captured: dict = {}
    real_scan = agent_loader.load_all_specialists

    def recording_scan(specialists_dir, **kwargs):
        found, failed = real_scan(specialists_dir, **kwargs)
        captured.update(found)
        return found, failed

    monkeypatch.setattr(agent_loader, "load_all_specialists", recording_scan)
    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    sonnet_role = _cas_role(specialists_root, InstanceDir(instance_path).active())
    before = _tree_bytes(instance_path)
    registry, _ = _registry_load(tmp_path, index, agents_root, roles_dir)
    assert len(index.installed_slugs()) == 1
    assert len(registry.all_configs()) == 0
    assert registry.disabled_roles() == [_SLUG]
    assert len(registry.load_failures()) == 0
    assert _tree_bytes(instance_path) == before
    assert captured[_SLUG].binding.role_checksum == sonnet_role.checksum
    assert captured[_SLUG].compiled_prompt_bundle is not None


# ---------------------------------------------------------------------------
# 10-11. The lock and the same-generation primitive
# ---------------------------------------------------------------------------


class _CountingLock:
    def __init__(self) -> None:
        self.enters = 0
        self._held = False

    def __enter__(self):
        self.enters += 1
        self._held = True
        return self

    def __exit__(self, *exc):
        self._held = False
        return False

    def locked(self) -> bool:
        return self._held


def _direct_cfg(role, *, enabled: bool = True):
    from types import SimpleNamespace

    return SimpleNamespace(role_slot=role, enabled=enabled, persona_pack=None, binding=None,
                           compiled_prompt_bundle=None, binding_digest=None, speaker_provenance=None)


def test_lock_acquisition_count_is_zero_for_agreement_and_one_for_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common path (checksums agree) acquires MATERIALIZE_LOCK zero times,
    exactly as today; a repair acquires it exactly once; the load after a
    repair is back on the common path."""
    import personality_binding
    import prompt_compiler
    from types import SimpleNamespace
    from personality_binding import InstanceDir
    from specialist_install import activate_binding_for_config

    specialists_root, agents_root, _ = _install(tmp_path, monkeypatch)
    instance_path = specialists_root / _SLUG
    active_path = instance_path / "active.yaml"
    lock = _CountingLock()
    monkeypatch.setattr(personality_binding, "MATERIALIZE_LOCK", lock)
    compile_calls: list[int] = []
    monkeypatch.setattr(
        prompt_compiler, "compile_prompt_bundle",
        lambda **kw: (compile_calls.append(1), SimpleNamespace(binding_digest=kw["binding"].binding_digest))[1])

    opus_role = _cas_role(specialists_root, InstanceDir(instance_path).active())
    bytes_0 = active_path.read_bytes()
    activate_binding_for_config(_direct_cfg(opus_role), specialists_root=specialists_root)
    assert lock.enters == 0
    assert active_path.read_bytes() == bytes_0

    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    sonnet_role = _cas_role(specialists_root, InstanceDir(instance_path).active())
    activate_binding_for_config(_direct_cfg(sonnet_role), specialists_root=specialists_root)
    assert lock.enters == 1
    bytes_2 = active_path.read_bytes()
    assert bytes_2 != bytes_0

    activate_binding_for_config(_direct_cfg(sonnet_role), specialists_root=specialists_root)
    assert lock.enters == 1
    assert active_path.read_bytes() == bytes_2
    assert len(compile_calls) == 3


def test_replace_active_same_generation_refuses_every_non_role_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The primitive admits exactly one delta — role_checksum and its derived
    binding_digest — and refuses every other movement without touching any
    file; the one admitted candidate replaces active.yaml alone."""
    from personality_binding import InstanceDir, compute_binding_digest, compute_effective_config_digest

    specialists_root, agents_root, instance_path = _three_generations(tmp_path, monkeypatch)
    instance = InstanceDir(instance_path)
    observed = instance.active()
    active_path, desired_path, prior_path = (
        instance_path / "active.yaml", instance_path / "desired.yaml", instance_path / "active.prior.yaml")
    observed_bytes, desired_before, prior_before = (
        active_path.read_bytes(), desired_path.read_bytes(), prior_path.read_bytes())
    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    sonnet_role = _cas_role(specialists_root, observed)
    valid = _expected_rederived(observed, sonnet_role)

    def with_binding(**changes):
        b = dataclasses.replace(valid.binding, **changes)
        b = dataclasses.replace(b, binding_digest=compute_binding_digest(
            stable_agent_id=b.stable_agent_id, role_checksum=b.role_checksum, persona_id=b.persona_id,
            persona_version=b.persona_version, persona_checksum=b.persona_checksum,
            compiler_schema_version=b.compiler_schema_version, dependency_digests=b.dependency_digests,
            effective_config_digest=b.effective_config_digest))
        return dataclasses.replace(valid, binding=b)

    snapshot = {"timezone": "Europe/Rome"}
    ecd = compute_effective_config_digest(snapshot)
    candidates = [
        dataclasses.replace(valid, root=valid.root.replace("@", "@x")),           # tuple root
        dataclasses.replace(with_binding(effective_config_digest=ecd),            # config snapshot/digest
                            config_snapshot=snapshot, config_digest=ecd),
        with_binding(stable_agent_id="specialist:other"),
        with_binding(mode="override", override_source="casa/judge@0.1.0"),        # mode + source
        with_binding(persona_id="casa/other"),
        with_binding(persona_version="9.9.9"),
        with_binding(persona_checksum="sha256:" + "b" * 64),
        with_binding(compiler_schema_version="v0"),
        with_binding(dependency_digests=("sha256:" + "c" * 64,)),
        with_binding(effective_config_digest=ecd),                                # digest without snapshot
        with_binding(component_root=None),
        with_binding(override_source="casa/judge@0.1.0"),
        observed,                                                                 # no delta at all
    ]
    refusals = 0
    for candidate in candidates:
        try:
            instance.replace_active_same_generation(observed, candidate)
        except ValueError:
            refusals += 1
    assert refusals == len(candidates)
    assert active_path.read_bytes() == observed_bytes
    assert desired_path.read_bytes() == desired_before
    assert prior_path.read_bytes() == prior_before

    written = instance.replace_active_same_generation(observed, valid)
    assert written == valid
    assert instance.active() == valid
    assert desired_path.read_bytes() == desired_before
    assert prior_path.read_bytes() == prior_before
    # A stale observation is refused too — the tuple moved under the caller.
    with pytest.raises(ValueError):
        instance.replace_active_same_generation(observed, valid)
    assert instance.active() == valid


# ---------------------------------------------------------------------------
# 12-13. Plumbing: the flag reaches every signature; boot loads off the loop
# ---------------------------------------------------------------------------


def test_specialist_binding_commit_keywords_are_forwarded_through_all_three_signatures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    import agent_loader
    import specialist_install

    specialists_root, agents_root, _ = _install(tmp_path, monkeypatch)
    index, roles_dir = _overlay(specialists_root, agents_root)

    activation_values: list = []
    monkeypatch.setattr(specialist_install, "activate_binding_for_config",
                        lambda cfg, **kw: activation_values.append(kw.get("commit")))
    agent_loader.load_agent_from_dir(str(agents_root / _SLUG), policies=None,
                                     roles_dir=str(roles_dir), binding_commit=False)
    assert activation_values == [False]

    per_agent_values: list = []

    def fake_load(path, **kw):
        per_agent_values.append(kw.get("binding_commit"))
        return SimpleNamespace(role=_SLUG, tools=SimpleNamespace(allowed=[]))

    monkeypatch.setattr(agent_loader, "load_agent_from_dir", fake_load)
    agent_loader.load_all_specialists(str(agents_root), roles_dir=str(roles_dir), binding_commit=False)
    assert per_agent_values == [False]

    validation_values: list = []

    def fake_scan(specialists_dir, **kw):
        validation_values.append(kw.get("binding_commit"))
        return {}, []

    monkeypatch.setattr(agent_loader, "load_all_specialists", fake_scan)
    repo = _resident_repo(tmp_path, roles_dir)
    assert agent_loader.validate_config_repo(str(repo), roles_dir=str(roles_dir)) == []
    assert validation_values == [False]


def test_casa_core_boot_offloads_specialist_registry_load() -> None:
    """Boot must not acquire MATERIALIZE_LOCK on the event loop: the one bare
    `specialist_registry.load(...)` in `casa_core.main` is offloaded through
    `await asyncio.to_thread(specialist_registry.load, roles_dir=...)`, the
    shape every reload-side refresh already uses. Pinned structurally, over
    the source, so a revert to the bare call is caught."""
    import casa_core

    tree = ast.parse(Path(casa_core.__file__).read_text(encoding="utf-8"))
    main_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "main")

    def is_registry_load(node) -> bool:
        return (isinstance(node, ast.Attribute) and node.attr == "load"
                and isinstance(node.value, ast.Name) and node.value.id == "specialist_registry")

    offloaded, bare = [], []
    for node in ast.walk(main_fn):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            call = node.value
            if (isinstance(call.func, ast.Attribute) and call.func.attr == "to_thread"
                    and call.args and is_registry_load(call.args[0])):
                offloaded.append(call)
        elif isinstance(node, ast.Call) and is_registry_load(node.func):
            bare.append(node)
    assert (len(offloaded), len(bare)) == (1, 0)
    assert [kw.arg for kw in offloaded[0].keywords] == ["roles_dir"]
