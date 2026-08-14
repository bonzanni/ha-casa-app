"""Task N1d: bare-persona repo install/apply pipeline (spec §2.1/§9.4
decision 4) — `inspect_persona_repo`/`commit_persona_install`/
`apply_persona_override` (persona_install.py).

DRIFT WARNING disclosed in task-n1d-report.md: the brief's own
`_write_persona_repo` fixture built its manifest rows in DECLARATION order
("persona.yaml", "persona.md") rather than the order
`persona_pack._admit_files` actually admits files in (alphabetical by
filename — "persona.md" sorts before "persona.yaml"). `load_persona_pack`
recomputes the manifest from admitted files and requires an EXACT match
(including row order), so the brief's fixture as written would raise
PersonaPackError("persona manifest does not match admitted files") on
every use. Fixed here by mirroring tests/test_persona_pack.py's
`build_manifest` helper convention: iterate `sorted(os.listdir(pack_dir))`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from canonical_bytes import canonical_json_bytes, canonical_text, checksum_bytes
from persona_install import PersonaInspectionResult, inspect_persona_repo
from specialist_install import SpecialistInstallError


def _write_persona_repo(root: Path, *, persona_id: str = "casa/newton",
                        version: str = "0.1.0", negative_space: str = "Never condescends.") -> Path:
    pack_dir = root / "pack"
    pack_dir.mkdir(parents=True)
    persona_yaml = {
        "api_version": "casa.persona/v1", "id": persona_id, "version": version,
        "trait_schema_version": 1,
        "identity": {"display_name": "Newton", "pronouns": {
            "subject": "he", "object": "him", "possessive_adjective": "his",
            "possessive_pronoun": "his", "reflexive": "himself"}},
        "relationship_posture": "established", "archetype": "tutor",
        "traits": {"warmth": 3, "formality": 3, "candor": 4, "attunement": 3,
                    "curiosity": 5, "levity": 2, "social_energy": 3, "optimism": 3},
        "quirks": [],
    }
    (pack_dir / "persona.yaml").write_text(yaml.safe_dump(persona_yaml, sort_keys=False), encoding="utf-8")
    core = "Y" * 350
    (pack_dir / "persona.md").write_text(
        f"# Core\n\n{core}\n\n## Negative space\n\n{negative_space}\n", encoding="utf-8")
    # persona_pack._admit_files sorts admitted files by NAME — the manifest
    # row order must match that, not source-declaration order (see the
    # module docstring's DRIFT WARNING disclosure above).
    rows = []
    for name in sorted(os.listdir(pack_dir)):
        text = canonical_text((pack_dir / name).read_text(encoding="utf-8"))
        rows.append({"path": name, "type": "file", "executable": False,
                      "checksum": checksum_bytes(text.encode("utf-8"))})
    payload = {"api_version": "casa.persona.manifest/v1", "files": rows}
    checksum = checksum_bytes(canonical_json_bytes(payload))
    payload["checksum"] = checksum
    (root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def test_inspect_persona_repo_validates_and_returns_checksum(tmp_path: Path, monkeypatch) -> None:
    fetched = tmp_path / "fetched"
    _write_persona_repo(fetched)

    def _fake_resolve_and_fetch(repo, ref, subdir, dest, *, expected_revision=None):
        import shutil
        shutil.copytree(fetched, dest)
        return "0" * 40

    monkeypatch.setattr("persona_install.resolve_and_fetch", _fake_resolve_and_fetch)
    result = inspect_persona_repo("casa-test/newton-persona", "main",
                                   staging_root=tmp_path / "staging")
    assert isinstance(result, PersonaInspectionResult)
    assert result.persona_id == "casa/newton"
    assert result.version == "0.1.0"
    assert result.checksum.startswith("sha256:")
    assert result.display_name == "Newton"


def test_inspect_persona_repo_rejects_forbidden_markers(tmp_path: Path, monkeypatch) -> None:
    fetched = tmp_path / "fetched"
    _write_persona_repo(fetched)
    (fetched / "pack" / "persona.md").write_text(
        "# Core\n\n${INJECTED}" + "Z" * 340 + "\n\n## Negative space\n\nNone.\n", encoding="utf-8")

    def _fake_resolve_and_fetch(repo, ref, subdir, dest, *, expected_revision=None):
        import shutil
        shutil.copytree(fetched, dest)
        return "0" * 40

    monkeypatch.setattr("persona_install.resolve_and_fetch", _fake_resolve_and_fetch)
    with pytest.raises(SpecialistInstallError) as raised:
        inspect_persona_repo("casa-test/newton-persona", "main", staging_root=tmp_path / "staging")
    assert raised.value.kind in {"persona_invalid", "forbidden_markers"}


def test_inspect_persona_repo_missing_manifest_raises(tmp_path: Path, monkeypatch) -> None:
    fetched = tmp_path / "fetched"
    fetched.mkdir()

    def _fake_resolve_and_fetch(repo, ref, subdir, dest, *, expected_revision=None):
        import shutil
        shutil.copytree(fetched, dest)
        return "0" * 40

    monkeypatch.setattr("persona_install.resolve_and_fetch", _fake_resolve_and_fetch)
    with pytest.raises(SpecialistInstallError) as raised:
        inspect_persona_repo("casa-test/newton-persona", "main", staging_root=tmp_path / "staging")
    assert raised.value.kind == "manifest_missing"


def test_inspect_persona_repo_failure_reclaims_staging_tree(
        tmp_path: Path, monkeypatch) -> None:
    """#306: a rejected persona inspection (missing manifest here) must not
    leave the fetched tree behind under personas/.staging."""
    fetched = tmp_path / "fetched"
    fetched.mkdir()

    def _fake_resolve_and_fetch(repo, ref, subdir, dest, *, expected_revision=None):
        import shutil
        shutil.copytree(fetched, dest)
        return "0" * 40

    monkeypatch.setattr("persona_install.resolve_and_fetch", _fake_resolve_and_fetch)
    staging = tmp_path / ".staging"
    with pytest.raises(SpecialistInstallError):
        inspect_persona_repo("casa-test/newton-persona", "main", staging_root=staging)
    assert list(staging.iterdir()) == []


def test_default_roots_resolve_through_casa_config_dir(tmp_path: Path, monkeypatch) -> None:
    """#323 (Sol r3-2): the SUPPORTED install flow — inspect then commit with
    the tools' DEFAULT roots — must stage and publish under
    $CASA_CONFIG_DIR/personas. Pre-fix the defaults froze /config/personas at
    import time, so under a custom config root the tools published to one
    directory while persona_apply and the loaders read another
    (persona_unavailable for a pack the operator just installed)."""
    from persona_install import (
        PersonaInstallAckStore, commit_persona_install, persona_install_consent_identity,
    )

    custom = tmp_path / "custom"
    monkeypatch.setenv("CASA_CONFIG_DIR", str(custom))
    fetched = tmp_path / "fetched"
    _write_persona_repo(fetched)

    def _fake_resolve_and_fetch(repo, ref, subdir, dest, *, expected_revision=None):
        import shutil
        shutil.copytree(fetched, dest)
        return "0" * 40

    monkeypatch.setattr("persona_install.resolve_and_fetch", _fake_resolve_and_fetch)
    inspection = inspect_persona_repo("casa-test/newton-persona", "main")
    assert str(inspection.staged_dir).startswith(
        str(custom / "personas" / ".staging"))

    acks = PersonaInstallAckStore(path=tmp_path / "acks.json")
    identity = persona_install_consent_identity(
        persona_id=inspection.persona_id, version=inspection.version,
        checksum=inspection.checksum)
    acks.record(identity=identity, persona_id=inspection.persona_id,
                version=inspection.version, checksum=inspection.checksum)

    pack = commit_persona_install(inspection=inspection, acks=acks)
    assert pack.checksum == inspection.checksum
    dest = custom / "personas" / "casa/newton" / "0.1.0"
    assert (dest / "manifest.json").is_file()
    assert (dest / "pack" / "persona.yaml").is_file()


def test_commit_persona_install_reclaims_staging_tree(tmp_path: Path) -> None:
    """#306: a successful persona commit consumes the inspection staging tree
    (pre-fix commit cleaned only its own commit-time staging copy)."""
    import dataclasses

    from persona_install import (
        PersonaInstallAckStore, commit_persona_install, persona_install_consent_identity,
    )

    inspection = _inspection_from_repo(tmp_path)
    staging = tmp_path / ".staging"
    staging.mkdir()
    staged = staging / "deadbeef"
    import shutil as _shutil
    _shutil.copytree(inspection.staged_dir, staged)
    inspection = dataclasses.replace(inspection, staged_dir=staged)

    acks = PersonaInstallAckStore(path=tmp_path / "acks.json")
    identity = persona_install_consent_identity(
        persona_id=inspection.persona_id, version=inspection.version, checksum=inspection.checksum)
    acks.record(identity=identity, persona_id=inspection.persona_id,
                version=inspection.version, checksum=inspection.checksum)

    pack = commit_persona_install(
        inspection=inspection, acks=acks, personas_root=tmp_path / "personas")
    assert pack.checksum == inspection.checksum
    assert not staged.exists()


# ---------------------------------------------------------------------------
# PersonaInstallAckStore — multi-instance ledger safety (#310)
# ---------------------------------------------------------------------------


def test_ack_store_record_does_not_clobber_sibling_instance_acks(tmp_path: Path) -> None:
    """#310: the tool layer builds one store per prompt; a consent keyboard's
    approve callback records through its own prompt-time instance. Recording
    through instance B must MERGE with (not clobber) an ack instance A already
    persisted after B was constructed."""
    from persona_install import PersonaInstallAckStore, persona_install_consent_identity

    path = tmp_path / "acks.json"
    store_a = PersonaInstallAckStore(path=path)
    store_b = PersonaInstallAckStore(path=path)  # sibling prompt, pre-A state
    ident_a = persona_install_consent_identity(
        persona_id="casa/alpha", version="0.1.0", checksum="a" * 8)
    ident_b = persona_install_consent_identity(
        persona_id="casa/beta", version="0.1.0", checksum="b" * 8)
    store_a.record(identity=ident_a, persona_id="casa/alpha", version="0.1.0",
                   checksum="a" * 8)
    store_b.record(identity=ident_b, persona_id="casa/beta", version="0.1.0",
                   checksum="b" * 8)
    # A commit constructs a fresh store and reads the ledger from disk — both
    # acks must be there.
    fresh = PersonaInstallAckStore(path=path)
    assert fresh.is_acked(ident_a)
    assert fresh.is_acked(ident_b)


def test_record_aborts_rather_than_wiping_on_transient_read_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    """#310 (Sol r1): a TRANSIENT ledger read failure (an OSError that is not
    file-missing) during record's read-modify-write must abort the mutation —
    persisting the fail-closed empty view would erase every previously
    recorded ack. Only a genuinely absent file starts a fresh ledger."""
    from persona_install import PersonaInstallAckStore, persona_install_consent_identity

    path = tmp_path / "acks.json"
    store = PersonaInstallAckStore(path=path)
    ident_a = persona_install_consent_identity(
        persona_id="casa/alpha", version="0.1.0", checksum="a" * 8)
    ident_b = persona_install_consent_identity(
        persona_id="casa/beta", version="0.1.0", checksum="b" * 8)
    store.record(identity=ident_a, persona_id="casa/alpha", version="0.1.0",
                 checksum="a" * 8)

    real_read_text = Path.read_text

    def _flaky(self, *args, **kwargs):
        if self == path:
            raise PermissionError("transient read failure")
        return real_read_text(self, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(Path, "read_text", _flaky)
        with pytest.raises(OSError):
            store.record(identity=ident_b, persona_id="casa/beta",
                         version="0.1.0", checksum="b" * 8)

    assert PersonaInstallAckStore(path=path).is_acked(ident_a)


# ---------------------------------------------------------------------------
# commit_persona_install
# ---------------------------------------------------------------------------


def _inspection_from_repo(tmp_path: Path, *, persona_id: str = "casa/newton",
                          version: str = "0.1.0",
                          negative_space: str = "Never condescends.") -> PersonaInspectionResult:
    from persona_pack import load_persona_pack

    staged = tmp_path / f"staged-{persona_id.replace('/', '-')}-{version}-{abs(hash(negative_space))}"
    _write_persona_repo(staged, persona_id=persona_id, version=version, negative_space=negative_space)
    pack = load_persona_pack(staged / "pack", staged / "manifest.json")
    return PersonaInspectionResult(
        persona_id=pack.persona_id, version=pack.version, checksum=pack.checksum,
        display_name=pack.identity.get("display_name", pack.persona_id), staged_dir=staged,
    )


def test_commit_persona_install_requires_consent(tmp_path: Path) -> None:
    from persona_install import PersonaInstallAckStore, commit_persona_install

    inspection = _inspection_from_repo(tmp_path)
    acks = PersonaInstallAckStore(path=tmp_path / "acks.json")

    with pytest.raises(SpecialistInstallError) as raised:
        commit_persona_install(
            inspection=inspection, acks=acks, personas_root=tmp_path / "personas")
    assert raised.value.kind == "consent_missing"
    assert not (tmp_path / "personas").exists()


def test_commit_persona_install_persists_the_verified_pack(tmp_path: Path) -> None:
    from persona_install import (
        PersonaInstallAckStore, commit_persona_install, persona_install_consent_identity,
    )

    inspection = _inspection_from_repo(tmp_path)
    acks = PersonaInstallAckStore(path=tmp_path / "acks.json")
    identity = persona_install_consent_identity(
        persona_id=inspection.persona_id, version=inspection.version, checksum=inspection.checksum)
    acks.record(identity=identity, persona_id=inspection.persona_id,
                version=inspection.version, checksum=inspection.checksum)

    personas_root = tmp_path / "personas"
    pack = commit_persona_install(inspection=inspection, acks=acks, personas_root=personas_root)

    assert pack.persona_id == "casa/newton"
    assert pack.checksum == inspection.checksum
    dest = personas_root / "casa/newton" / "0.1.0"
    assert (dest / "manifest.json").is_file()
    assert (dest / "pack" / "persona.yaml").is_file()
    # Re-loading independently from the committed location must match too —
    # this is the EXACT layout Task 8's `_load_override` reads.
    from persona_pack import load_persona_pack
    reloaded = load_persona_pack(dest / "pack", dest / "manifest.json")
    assert reloaded.checksum == inspection.checksum


def test_commit_persona_install_is_idempotent_once_already_committed(tmp_path: Path) -> None:
    """A second commit call for an already-persisted persona_id/version must
    not re-copy or fail — it just re-loads the existing, already-verified
    directory (mirrors the CAS "already exists" short-circuit the specialist
    pipeline uses)."""
    from persona_install import (
        PersonaInstallAckStore, commit_persona_install, persona_install_consent_identity,
    )

    inspection = _inspection_from_repo(tmp_path)
    acks = PersonaInstallAckStore(path=tmp_path / "acks.json")
    identity = persona_install_consent_identity(
        persona_id=inspection.persona_id, version=inspection.version, checksum=inspection.checksum)
    acks.record(identity=identity, persona_id=inspection.persona_id,
                version=inspection.version, checksum=inspection.checksum)
    personas_root = tmp_path / "personas"
    commit_persona_install(inspection=inspection, acks=acks, personas_root=personas_root)

    pack_again = commit_persona_install(inspection=inspection, acks=acks, personas_root=personas_root)
    assert pack_again.checksum == inspection.checksum


def _ack_and_commit(tmp_path: Path, acks_path: Path, personas_root: Path, inspection):
    from persona_install import (
        PersonaInstallAckStore, commit_persona_install, persona_install_consent_identity,
    )

    acks = PersonaInstallAckStore(path=acks_path)
    identity = persona_install_consent_identity(
        persona_id=inspection.persona_id, version=inspection.version, checksum=inspection.checksum)
    acks.record(identity=identity, persona_id=inspection.persona_id,
                version=inspection.version, checksum=inspection.checksum)
    return commit_persona_install(inspection=inspection, acks=acks, personas_root=personas_root)


def test_commit_persona_install_same_version_different_content_raises_version_content_conflict(
    tmp_path: Path,
) -> None:
    """Fix-round-1 CRITICAL regression (b): re-committing the SAME
    persona_id@version with DIFFERENT approved content must never silently
    return the stale on-disk pack — dest is keyed by a MUTABLE version
    string, not a content digest, so "dest already exists" does not imply
    "dest matches this inspection". Must raise version_content_conflict and
    leave the on-disk bytes from the FIRST install completely unchanged."""
    from persona_install import commit_persona_install
    from specialist_install import SpecialistInstallError

    personas_root = tmp_path / "personas"
    first = _inspection_from_repo(tmp_path, negative_space="Never condescends.")
    first_pack = _ack_and_commit(tmp_path, tmp_path / "acks1.json", personas_root, first)
    assert first_pack.checksum == first.checksum

    second = _inspection_from_repo(
        tmp_path, negative_space="Always double-checks the units.")
    assert second.checksum != first.checksum  # genuinely different content, same id@version

    with pytest.raises(SpecialistInstallError) as raised:
        _ack_and_commit(tmp_path, tmp_path / "acks2.json", personas_root, second)
    assert raised.value.kind == "version_content_conflict"

    # The on-disk bytes from the first, approved install must be UNCHANGED —
    # never silently replaced by the second, unapproved-for-this-path content.
    from persona_pack import load_persona_pack
    dest = personas_root / first.persona_id / first.version
    reloaded = load_persona_pack(dest / "pack", dest / "manifest.json")
    assert reloaded.checksum == first.checksum
    assert reloaded.checksum != second.checksum


def test_commit_persona_install_corrupt_dest_fails_closed(tmp_path: Path) -> None:
    """Fix-round-1 CRITICAL regression: if `dest` exists but is unreadable/
    corrupt (load_persona_pack raises), commit_persona_install must fail
    closed with the same typed error rather than crashing raw with an
    unstructured exception."""
    from persona_install import commit_persona_install
    from specialist_install import SpecialistInstallError

    personas_root = tmp_path / "personas"
    inspection = _inspection_from_repo(tmp_path)
    dest = personas_root / inspection.persona_id / inspection.version
    dest.mkdir(parents=True)
    (dest / "manifest.json").write_text("not valid json at all {{{", encoding="utf-8")

    with pytest.raises(SpecialistInstallError) as raised:
        _ack_and_commit(tmp_path, tmp_path / "acks.json", personas_root, inspection)
    assert raised.value.kind == "version_content_conflict"


# ---------------------------------------------------------------------------
# apply_persona_override
# ---------------------------------------------------------------------------


def _resident_role():
    from role_artifact import load_role_artifact
    from role_slot import materialize_role

    role_dir = (
        Path(__file__).resolve().parent.parent
        / "casa/rootfs/opt/casa/defaults/roles/resident/assistant"
    )
    return materialize_role(source=load_role_artifact(role_dir), options={})


def install_persona_for_apply(tmp_path: Path, monkeypatch, *, persona_id: str,
                              version: str):
    """#543: `apply_persona_override` re-proves, INSIDE the materialize lock,
    that the persona it is about to pin is resolvable under the approved roots
    — because a `persona_remove` can land between the caller resolving the
    pack and the binding being staged, and a binding pinning absent bytes is
    boot-fatal for a resident.

    So a test that applies a pack parked in an arbitrary directory is
    describing an arrangement production never produces (`persona_apply` and
    `resident_persona_swap` both resolve from the approved roots) and that the
    loader could not activate. Install it where the loader looks, through the
    same `$CASA_CONFIG_DIR` seam."""
    from persona_pack import load_persona_pack

    config_root = tmp_path / "config-root"
    monkeypatch.setenv("CASA_CONFIG_DIR", str(config_root))
    dest = config_root / "personas" / persona_id / version
    dest.mkdir(parents=True)
    _write_persona_repo(dest, persona_id=persona_id, version=version)
    return load_persona_pack(dest / "pack", dest / "manifest.json")


def test_apply_persona_override_resident_sets_override_source_as_root(
        tmp_path: Path, monkeypatch) -> None:
    from persona_install import apply_persona_override

    role = _resident_role()
    persona = install_persona_for_apply(
        tmp_path, monkeypatch, persona_id="casa/ellen", version="0.1.0")

    committed = apply_persona_override(
        target_role_id="resident:assistant", persona=persona, role=role,
        instance_dir_root=tmp_path / "bindings" / "resident-assistant",
    )
    assert committed.root == "casa/ellen@0.1.0"
    assert committed.binding.mode == "override"
    assert committed.binding.override_source == "casa/ellen@0.1.0"


def _write_specialist_component(root: Path, *, slug: str = "mtg-n1d") -> Path:
    """Minimal specialist component compatible with the bundled persona
    fixture above (persona.compatibility left unset so any persona is
    accepted, unlike test_specialist_install.py's judge-only fixture)."""
    (root / "role").mkdir(parents=True)
    (root / "persona" / "pack").mkdir(parents=True)
    role_yaml = {
        "api_version": "casa.role/v1", "id": f"specialist:{slug}", "kind": "specialist",
        "slot": slug, "mission": "Answer test questions.", "enabled": True,
        "model": {"source": "fixed", "value": "sonnet"},
        "tools": {"allowed": [], "disallowed": ["Bash"], "permission_mode": "dontAsk",
                   "max_turns": 8, "skills": "none", "voice_guard": "none"},
        "mcp_servers": [], "channels": [], "memory": {"token_budget": 0, "read_strategy": "per_turn"},
        "session": {"strategy": "ephemeral", "idle_timeout_seconds": 0},
        "disclosure": {"policy": "delegated", "overrides": {}},
        "delegates": [], "executors": [], "triggers": [], "hooks": {"pre_tool_use": []},
        "tts": {"tag_dialect": "none", "error_phrases": {}},
        "response": {"text": {"register": "precise"}, "voice": {"register": "spoken"},
                      "restricted_webhook": {"register": "plain"}},
        # role.v1.json requires a non-empty `compatibility` list whenever
        # policy=="required" — a "ns/*@..." wildcard entry accepts ANY slug
        # in the "casa" namespace (personality_binding.check_persona_
        # requirements's namespace/slug_pattern match), so both the bundled
        # casa/judge default AND an apply_persona_override casa/newton
        # override satisfy it.
        "persona": {"policy": "required", "compatibility": ["casa/*@>=0.0.0 <99.0.0"]},
        "requires": {"plugins": [], "tools": []}, "doctrine_file": "doctrine.md",
    }
    (root / "role" / "role.yaml").write_text(yaml.safe_dump(role_yaml, sort_keys=False), encoding="utf-8")
    (root / "role" / "doctrine.md").write_text("# Core doctrine\n\nAnswer test questions.\n", encoding="utf-8")
    (root / "config-schema.json").write_text(json.dumps({"required": [], "secret_names": []}), encoding="utf-8")

    persona_yaml = {
        "api_version": "casa.persona/v1", "id": "casa/judge", "version": "0.1.0",
        "trait_schema_version": 1,
        "identity": {"display_name": "Judge", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        "relationship_posture": "established", "archetype": "adjudicator",
        "traits": {"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                    "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        "quirks": [],
    }
    (root / "persona" / "pack" / "persona.yaml").write_text(
        yaml.safe_dump(persona_yaml, sort_keys=False), encoding="utf-8")
    core = "X" * 350
    (root / "persona" / "pack" / "persona.md").write_text(
        f"# Core\n\n{core}\n\n## Negative space\n\nNever guesses.\n", encoding="utf-8")
    manifest_rows = []
    for name in sorted(os.listdir(root / "persona" / "pack")):
        text = canonical_text((root / "persona" / "pack" / name).read_text(encoding="utf-8"))
        manifest_rows.append({"path": name, "type": "file", "executable": False,
                               "checksum": checksum_bytes(text.encode("utf-8"))})
    persona_manifest_payload = {"api_version": "casa.persona.manifest/v1", "files": manifest_rows}
    persona_checksum = checksum_bytes(canonical_json_bytes(persona_manifest_payload))
    persona_manifest_payload["checksum"] = persona_checksum
    (root / "persona" / "manifest.json").write_text(json.dumps(persona_manifest_payload), encoding="utf-8")

    from specialist_component import compute_component_checksum
    files = {
        "role/role.yaml": (root / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    component_checksum = compute_component_checksum(files)
    manifest = {
        "api_version": "casa.specialist-component/v1", "component_id": f"casa-test/{slug}",
        "version": "0.1.0",
        "default_persona": {"ref": "casa/judge@0.1.0", "checksum": persona_checksum},
        "dependencies": [
            {"kind": "persona", "identifier": "casa/judge@0.1.0", "digest": persona_checksum},
        ],
        "checksum": component_checksum,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _installed_specialist(tmp_path: Path, *, slug: str = "mtg-n1d"):
    from specialist_install import (
        InspectionResult, commit_specialist_install, compute_install_root_digest,
        resolve_dependency_closure,
    )
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity
    from specialist_component import load_specialist_component

    staged = _write_specialist_component(tmp_path / "staged", slug=slug)
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())
    inspection = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=deps, staged_dir=staged,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "specialist-acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)
    specialists_dir = tmp_path / "specialists"
    agents_specialists_dir = tmp_path / "agents-specialists"
    commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
    )
    return specialists_dir, agents_specialists_dir


def _specialist_role(specialists_dir: Path, slug: str):
    from personality_binding import InstanceDir
    from role_artifact import load_role_artifact
    from role_slot import materialize_role
    from specialist_install import cas_store_dir, parse_component_root

    active = InstanceDir(specialists_dir / slug).active()
    _, _, checksum = parse_component_root(active.root)
    cas_dir = cas_store_dir(checksum, store_root=specialists_dir / "store")
    return materialize_role(source=load_role_artifact(cas_dir / "role"), options={}), active


def test_apply_persona_override_specialist_preserves_root_and_dependency_state(
        tmp_path: Path, monkeypatch) -> None:
    from persona_install import apply_persona_override

    specialists_dir, _agents_dir = _installed_specialist(tmp_path)
    role, active_before = _specialist_role(specialists_dir, "mtg-n1d")

    persona = install_persona_for_apply(
        tmp_path, monkeypatch, persona_id="casa/newton", version="0.1.0")
    # This fixture's role.yaml sets persona.compatibility to a wildcard
    # entry ("casa/*@>=0.0.0 <99.0.0" — see _write_specialist_component
    # above), which check_persona_requirements matches against ANY slug in
    # the "casa" namespace within that version range. That's an explicit,
    # broad admission rule — not an absence of a compatibility list — and
    # this override's casa/newton@0.1.0 satisfies it same as the bundled
    # casa/judge@0.1.0 default would.

    committed = apply_persona_override(
        target_role_id="specialist:mtg-n1d", persona=persona, role=role,
        instance_dir_root=specialists_dir / "mtg-n1d",
    )
    assert committed.root == active_before.root  # component root UNCHANGED
    assert committed.binding.mode == "override"
    assert committed.binding.override_source == "casa/newton@0.1.0"
    assert committed.binding.dependency_digests == active_before.binding.dependency_digests
    assert committed.config_snapshot == active_before.config_snapshot


def test_apply_persona_override_specialist_without_active_tuple_raises(tmp_path: Path) -> None:
    from persona_install import apply_persona_override
    from persona_pack import load_persona_pack
    from role_artifact import load_role_artifact
    from role_slot import materialize_role

    staged = _write_specialist_component(tmp_path / "staged-never-installed", slug="never-installed")
    role = materialize_role(source=load_role_artifact(staged / "role"), options={})
    persona_dir = tmp_path / "newton-repo"
    _write_persona_repo(persona_dir, persona_id="casa/newton", version="0.1.0")
    persona = load_persona_pack(persona_dir / "pack", persona_dir / "manifest.json")

    with pytest.raises(SpecialistInstallError) as raised:
        apply_persona_override(
            target_role_id="specialist:never-installed", persona=persona, role=role,
            instance_dir_root=tmp_path / "specialists" / "never-installed",
        )
    assert raised.value.kind == "no_active_tuple"


# ---------------------------------------------------------------------------
# Sol P2 (#217): commit_persona_install publication-race safety. Two concurrent
# commits of the SAME persona_id@version can both miss the is_file() precheck,
# both stage, then race on os.replace — the loser's rename onto a now-populated
# dest fails (ENOTEMPTY). It must never leak its staging dir or raise a raw
# OSError: identical winning content is an idempotent success; different content
# surfaces the SAME typed version_content_conflict as the pre-race branch.
# ---------------------------------------------------------------------------


def test_commit_persona_install_publication_race_resolves_idempotently(
    tmp_path: Path, monkeypatch,
) -> None:
    from persona_install import commit_persona_install  # noqa: F401 — exercised via _ack_and_commit

    personas_root = tmp_path / "personas"
    inspection = _inspection_from_repo(tmp_path, negative_space="Never condescends.")
    target_dest = personas_root / inspection.persona_id / inspection.version

    real_replace = os.replace

    def _racing_replace(src, dst):
        # Only intercept the publication rename; everything else (ack-store
        # atomic writes, etc.) delegates to the real os.replace.
        if Path(dst) == target_dest:
            real_replace(src, dst)  # the concurrent winner publishes IDENTICAL bytes
            raise OSError(39, "Directory not empty")  # ENOTEMPTY — our rename loses
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _racing_replace)

    pack = _ack_and_commit(tmp_path, tmp_path / "acks.json", personas_root, inspection)
    assert pack.checksum == inspection.checksum  # typed idempotent pack, never a raw OSError
    staging = personas_root / ".staging"
    assert not staging.exists() or not any(staging.iterdir())  # no leaked staging dir


def test_commit_persona_install_publication_race_different_content_is_typed_conflict(
    tmp_path: Path, monkeypatch,
) -> None:
    import shutil

    from persona_pack import load_persona_pack

    personas_root = tmp_path / "personas"
    ours = _inspection_from_repo(tmp_path, negative_space="Never condescends.")
    theirs = _inspection_from_repo(
        tmp_path, negative_space="Always double-checks the units.")
    assert theirs.checksum != ours.checksum
    target_dest = personas_root / ours.persona_id / ours.version

    real_replace = os.replace

    def _racing_replace(src, dst):
        if Path(dst) == target_dest:
            # A concurrent winner publishes DIFFERENT bytes first.
            target_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(theirs.staged_dir / "pack", target_dest / "pack")
            shutil.copy2(theirs.staged_dir / "manifest.json", target_dest / "manifest.json")
            raise OSError(39, "Directory not empty")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _racing_replace)

    with pytest.raises(SpecialistInstallError) as raised:
        _ack_and_commit(tmp_path, tmp_path / "acks.json", personas_root, ours)
    assert raised.value.kind == "version_content_conflict"  # typed, never raw OSError
    staging = personas_root / ".staging"
    assert not staging.exists() or not any(staging.iterdir())  # loser's staging cleaned up
    published = load_persona_pack(target_dest / "pack", target_dest / "manifest.json")
    assert published.checksum == theirs.checksum  # winner's bytes intact
