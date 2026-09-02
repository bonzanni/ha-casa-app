"""#815 (INV-SPEC-012, diff review round 4) — the rollback's store-integrity
gate is unconditional, not only after a model change.

The role checksum covers the role artifact and the resolved model, not the
component manifest or the dependency closure, so a retained generation whose
store bytes drifted under an UNCHANGED role used to compile as stored and be
restored to a root that no longer names its bytes. The gate now runs before
every compile: the install root digest recomputed from the store must equal
the prior root's suffix, and the component id and version its root names.
This pins the no-flip arm; the model-flip arm is the accepted M4.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from test_specialist_binding_rederive import _install, _upgrade
from test_specialist_rollback_model_flip import _count_tuple_writes, _prior_and_paths

_SLUG = "mtg"


def test_a_drifted_store_is_refused_by_name_without_a_model_change(
        tmp_path: Path, monkeypatch) -> None:
    """M4's construction with NO option flip: the retained prior's manifest bytes
    are altered (a trailing newline), the role checksum is unchanged, and the
    rollback still refuses ``compile_failed`` naming both the prior's root and
    the recomputed digest, writing nothing. Mutant: make the gate conditional
    on the checksum again → this passes the compile and commits."""
    from specialist_install import (
        SpecialistInstallError, cas_store_dir, compute_install_root_digest,
        load_specialist_component, parse_component_root, resolve_dependency_closure,
        rollback_specialist,
    )

    specialists_root, agents_root, _ = _install(tmp_path, monkeypatch)
    assert _upgrade(tmp_path, specialists_root, agents_root, version="0.2.0").state == "active"
    prior, _, active_path, _ = _prior_and_paths(specialists_root)
    _, _, prior_checksum = parse_component_root(prior.root)
    cas_dir = cas_store_dir(prior_checksum, store_root=specialists_root / "store")
    manifest_path = cas_dir / "manifest.json"
    cas_dir.chmod(0o755)
    manifest_path.chmod(0o644)
    manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
    component = load_specialist_component(cas_dir, manifest_path)
    drifted_digest = compute_install_root_digest(
        component, resolve_dependency_closure(component, cas_dir),
        manifest_bytes=manifest_path.read_bytes())
    assert drifted_digest != prior_checksum
    active_before = active_path.read_bytes()
    writes = _count_tuple_writes(monkeypatch)

    with pytest.raises(SpecialistInstallError) as raised:
        rollback_specialist(
            slug=_SLUG, specialists_dir=specialists_root, agents_specialists_dir=agents_root)

    assert raised.value.kind == "compile_failed"
    assert prior.root in raised.value.detail
    assert drifted_digest in raised.value.detail
    assert "role_checksum" not in raised.value.detail
    assert len(writes) == 0
    assert active_path.read_bytes() == active_before


def test_a_drifted_component_file_is_refused_by_name_not_as_an_unstructured_error(
        tmp_path: Path, monkeypatch) -> None:
    """Diff review round 7 (Sol): a component-owned file — the retained role's
    doctrine — is altered, so the component loader itself rejects the store
    (its checksum no longer matches its manifest) before any digest gate. The
    rollback still refuses the TYPED ``compile_failed`` naming the prior's root
    — never a bare ``ValueError`` the tool would emit unstructured — with zero
    tuple writes and the active untouched. Mutant: let the loader's error
    escape → this raises ValueError, not SpecialistInstallError."""
    from specialist_install import (
        SpecialistInstallError, cas_store_dir, parse_component_root, rollback_specialist,
    )

    specialists_root, agents_root, _ = _install(tmp_path, monkeypatch)
    assert _upgrade(tmp_path, specialists_root, agents_root, version="0.2.0").state == "active"
    prior, _, active_path, _ = _prior_and_paths(specialists_root)
    _, _, prior_checksum = parse_component_root(prior.root)
    cas_dir = cas_store_dir(prior_checksum, store_root=specialists_root / "store")
    doctrine = cas_dir / "role" / "doctrine.md"
    cas_dir.chmod(0o755)
    (cas_dir / "role").chmod(0o755)
    doctrine.chmod(0o644)
    doctrine.write_bytes(doctrine.read_bytes() + b"\ndrifted\n")
    active_before = active_path.read_bytes()
    writes = _count_tuple_writes(monkeypatch)

    with pytest.raises(SpecialistInstallError) as raised:
        rollback_specialist(
            slug=_SLUG, specialists_dir=specialists_root, agents_specialists_dir=agents_root)

    assert raised.value.kind == "compile_failed"
    assert prior.root in raised.value.detail
    assert len(writes) == 0
    assert active_path.read_bytes() == active_before


def test_a_schema_invalid_retained_manifest_is_refused_by_name_not_as_an_unstructured_error(
        tmp_path: Path, monkeypatch) -> None:
    """Diff review round 8 (Sol): the retained manifest drifts to VALID JSON that
    violates the component schema — the component loader propagates
    ``jsonschema.ValidationError`` unwrapped, which is not a ``ValueError``.
    The rollback still refuses the typed ``compile_failed`` naming the prior's
    root, with zero tuple writes, zero journals and the active untouched.
    Mutant: enumerate the caught classes again (drop the schema error) → this
    raises ``ValidationError``, not ``SpecialistInstallError``."""
    import json
    import specialist_bundle_journal
    from specialist_install import (
        SpecialistInstallError, cas_store_dir, parse_component_root, rollback_specialist,
    )

    specialists_root, agents_root, _ = _install(tmp_path, monkeypatch)
    assert _upgrade(tmp_path, specialists_root, agents_root, version="0.2.0").state == "active"
    prior, _, active_path, _ = _prior_and_paths(specialists_root)
    _, _, prior_checksum = parse_component_root(prior.root)
    cas_dir = cas_store_dir(prior_checksum, store_root=specialists_root / "store")
    manifest_path = cas_dir / "manifest.json"
    cas_dir.chmod(0o755)
    manifest_path.chmod(0o644)
    manifest = json.loads(manifest_path.read_text())
    manifest["dependencies"] = "not-a-list"                   # schema-invalid, JSON-valid
    manifest_path.write_text(json.dumps(manifest))
    active_before = active_path.read_bytes()
    writes = _count_tuple_writes(monkeypatch)
    begins = {"count": 0}
    real_begin = specialist_bundle_journal.begin
    monkeypatch.setattr(specialist_bundle_journal, "begin",
                        lambda *a, **k: (begins.__setitem__("count", begins["count"] + 1),
                                         real_begin(*a, **k))[1])

    with pytest.raises(SpecialistInstallError) as raised:
        rollback_specialist(
            slug=_SLUG, specialists_dir=specialists_root, agents_specialists_dir=agents_root)

    assert raised.value.kind == "compile_failed"
    assert prior.root in raised.value.detail
    assert "ValidationError" in raised.value.detail
    assert len(writes) == 0
    assert begins["count"] == 0
    assert active_path.read_bytes() == active_before
