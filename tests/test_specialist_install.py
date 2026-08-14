import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

import specialist_install
from specialist_install import (
    DependencyResolution,
    SpecialistInstallError,
    commit_specialist_install,
    parse_component_root,
    resolve_dependency_closure,
)
from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity
from specialist_component import compute_component_checksum, load_specialist_component
from personality_binding import (
    EMPTY_CONFIG_DIGEST, BindingRecord, InstanceDir, InstanceTuple, compute_binding_digest,
)
from specialist_registry import InstalledSpecialistIndex
from canonical_bytes import canonical_json_bytes, canonical_text, checksum_bytes


def _write_component(root: Path, *, slug: str = "mtg-test",
                      dependencies: list[dict] | None = None,
                      model: dict | None = None) -> Path:
    (root / "role").mkdir(parents=True)
    (root / "persona" / "pack").mkdir(parents=True)
    role_yaml = {
        "api_version": "casa.role/v1", "id": f"specialist:{slug}", "kind": "specialist",
        "slot": slug, "mission": "Answer test questions.", "enabled": True,
        "model": model or {"source": "fixed", "value": "sonnet"},
        "tools": {"allowed": [], "disallowed": ["Bash"], "permission_mode": "dontAsk",
                   "max_turns": 8, "skills": "none", "voice_guard": "none"},
        "mcp_servers": [], "channels": [], "memory": {"token_budget": 0, "read_strategy": "per_turn"},
        "session": {"strategy": "ephemeral", "idle_timeout_seconds": 0},
        "disclosure": {"policy": "delegated", "overrides": {}},
        "delegates": [], "executors": [], "triggers": [], "hooks": {"pre_tool_use": []},
        "tts": {"tag_dialect": "none", "error_phrases": {}},
        "response": {"text": {"register": "precise"}, "voice": {"register": "spoken"},
                      "restricted_webhook": {"register": "plain"}},
        "persona": {"policy": "required", "compatibility": ["casa/judge@>=0.1.0 <1.0.0"]},
        "requires": {"plugins": [], "tools": []}, "doctrine_file": "doctrine.md",
    }
    (root / "role" / "role.yaml").write_text(yaml.safe_dump(role_yaml, sort_keys=False), encoding="utf-8")
    (root / "role" / "doctrine.md").write_text("# Core doctrine\n\nAnswer test questions.\n", encoding="utf-8")
    config_schema = {"required": [], "secret_names": []}
    (root / "config-schema.json").write_text(json.dumps(config_schema), encoding="utf-8")

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
    # persona_pack._admit_files sorts admitted files by NAME
    # ("persona.md" < "persona.yaml" alphabetically) — the manifest row
    # order must match that sort, not source-declaration order, or
    # load_persona_pack's own recomputed manifest payload (and hence its
    # checksum) will never equal what's written to disk here.
    for name in sorted(os.listdir(root / "persona" / "pack")):
        text = canonical_text((root / "persona" / "pack" / name).read_text(encoding="utf-8"))
        manifest_rows.append({"path": name, "type": "file", "executable": False,
                               "checksum": checksum_bytes(text.encode("utf-8"))})
    persona_manifest_payload = {"api_version": "casa.persona.manifest/v1", "files": manifest_rows}
    persona_checksum = checksum_bytes(canonical_json_bytes(persona_manifest_payload))
    persona_manifest_payload["checksum"] = persona_checksum
    (root / "persona" / "manifest.json").write_text(json.dumps(persona_manifest_payload), encoding="utf-8")

    files = {
        "role/role.yaml": (root / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    from specialist_component import compute_component_checksum
    component_checksum = compute_component_checksum(files)
    manifest = {
        "api_version": "casa.specialist-component/v1", "component_id": f"casa-test/{slug}",
        "version": "0.1.0",
        "default_persona": {"ref": "casa/judge@0.1.0", "checksum": persona_checksum},
        # Controller resolution B: a dependency row is exactly
        # {kind, identifier, digest} — specialist-component.v1.json sets
        # additionalProperties: false, so any extra key (the brief's stale
        # "checksum_field_unused") would fail schema validation.
        "dependencies": dependencies if dependencies is not None else [
            {"kind": "persona", "identifier": "casa/judge@0.1.0", "digest": persona_checksum},
        ],
        "checksum": component_checksum,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_resolve_dependency_closure_marks_bundled_persona_available(tmp_path: Path) -> None:
    root = _write_component(tmp_path / "component")
    component = load_specialist_component(root, root / "manifest.json")
    resolutions = resolve_dependency_closure(component, root)
    persona_rows = [r for r in resolutions if r.kind == "persona"]
    assert len(persona_rows) == 1
    assert persona_rows[0].available is True


def test_resolve_dependency_closure_reports_missing_corpus(tmp_path: Path) -> None:
    root = _write_component(tmp_path / "component", dependencies=[
        {"kind": "corpus/data", "identifier": "mtg-rules-corpus", "digest": "sha256:" + "9" * 64},
    ])
    component = load_specialist_component(root, root / "manifest.json")
    resolutions = resolve_dependency_closure(component, root)
    assert resolutions[0].available is False
    assert "corpus" in resolutions[0].detail


def test_resolve_dependency_closure_matches_corpus_digest(tmp_path: Path) -> None:
    from plugin_store import content_checksum

    corpus_dir = tmp_path / "component" / "corpus" / "mtg-rules-corpus"
    corpus_dir.mkdir(parents=True)
    (corpus_dir / "cr.txt").write_text("702.1 Some rule text.\n", encoding="utf-8")
    # plugin_store.content_checksum returns a BARE hex digest; the
    # specialist-component.v1.json schema constrains a dependency row's
    # `digest` field to `sha256:<hex>` — the brief's test used the bare
    # digest directly, which would fail schema validation at
    # load_specialist_component. Prefix it here, matching the normalization
    # resolve_dependency_closure itself applies to the same bare value.
    digest = "sha256:" + content_checksum(corpus_dir)
    root = _write_component(tmp_path / "component", dependencies=[
        {"kind": "corpus/data", "identifier": "mtg-rules-corpus", "digest": digest},
    ])
    component = load_specialist_component(root, root / "manifest.json")
    resolutions = resolve_dependency_closure(component, root)
    assert resolutions[0].available is True


# ---------------------------------------------------------------------------
# inspect_specialist_repo — direct regression coverage (fix round 1).
#
# resolve_and_fetch does real network/git I/O (plugin_store.resolve_ref +
# fetch_commit_tree). Every test here monkeypatches the MODULE ATTRIBUTE
# specialist_install.resolve_and_fetch with a stub that simply copies an
# already-built local component tree (via _write_component, above) into the
# caller-supplied `dest` and returns a fake 40-hex commit sha — never
# patching asyncio.sleep or any stdlib global (see CLAUDE.md's memory-cage
# note on why that specific pattern is forbidden in this repo).
# ---------------------------------------------------------------------------


def _stub_resolve_and_fetch(component_root: Path):
    """Build a stub matching resolve_and_fetch's exact signature
    (repo, ref, subdir, dest, *, expected_revision=None) -> str, so a
    monkeypatched call site sees identical call ergonomics to the real
    thing — it just copies a pre-built tree instead of fetching one."""

    def _stub(repo: str, ref: str, subdir: str, dest: Path, *, expected_revision: str | None = None) -> str:
        shutil.copytree(component_root, dest)
        return "a" * 40

    return _stub


def _specialist_binding(slug: str, **overrides) -> BindingRecord:
    """Mirrors tests/test_personality_binding.py's own `_binding` helper:
    build a schema-valid BindingRecord by actually recomputing
    binding_digest from the other fields via compute_binding_digest,
    rather than hand-picking an arbitrary digest string — a mismatched
    digest would fail verify_binding_record's own integrity check the
    moment InstanceDir.active()/desired() re-reads it off disk."""
    fields = dict(
        stable_agent_id=f"specialist:{slug}",
        role_checksum="sha256:" + "1" * 64,
        persona_id="casa/judge",
        persona_version="0.1.0",
        persona_checksum="sha256:" + "2" * 64,
        compiler_schema_version="v1",
        dependency_digests=(),
        # #372: the tuple helper persists config_snapshot={} — the binding's
        # digest must be the digest OF that snapshot or the strict loader
        # (and the write backstop) reject the fixture as pre-guard-shaped.
        effective_config_digest=EMPTY_CONFIG_DIGEST,
    )
    fields.update({k: v for k, v in overrides.items() if k in fields})
    digest = compute_binding_digest(**fields)
    return BindingRecord(
        **fields, mode="component-default", binding_digest=digest,
        component_root=overrides.get("component_root", f"casa-test/{slug}@0.1.0"),
    )


def _specialist_tuple(binding: BindingRecord) -> InstanceTuple:
    return InstanceTuple(
        root=binding.component_root or "", binding=binding,
        config_snapshot={}, config_digest=binding.effective_config_digest,
    )


def _write_component_with_role_yaml_comment_marker(root: Path, *, slug: str) -> Path:
    """A forbidden marker hidden in a YAML COMMENT. role_artifact.
    load_role_artifact's own marker check (authored_markers.
    reject_markers_in_parsed) walks the PARSED tree's string leaves —
    YAML comments are stripped by the parser and never appear there, so
    that check does not see this marker and load_specialist_component
    succeeds. specialist_install._validate_untrusted_bytes's raw-TEXT
    scan of role.yaml (component.role.role_path.read_text(...)) is the
    only thing that catches it — empirically confirmed (see fix-round-1
    report) that a doctrine.md-body marker is instead caught earlier, by
    load_role_artifact itself, and surfaces as kind='manifest_invalid',
    not 'forbidden_markers' — so this fixture exercises the genuine
    belt-and-suspenders gap _validate_untrusted_bytes exists to close."""
    root = _write_component(root, slug=slug)
    role_yaml_path = root / "role" / "role.yaml"
    original = role_yaml_path.read_text(encoding="utf-8")
    tampered = f"# a marker hidden in a comment: ${{SECRET}}\n{original}"
    role_yaml_path.write_text(tampered, encoding="utf-8")

    files = {
        "role/role.yaml": role_yaml_path.read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_inspect_specialist_repo_install_mode_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component_root = _write_component(tmp_path / "component", slug="fresh-specialist")
    component = load_specialist_component(component_root, component_root / "manifest.json")
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))

    index = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    index.load()

    result = specialist_install.inspect_specialist_repo(
        "org/repo", "main",
        staging_root=tmp_path / "staging",
        installed_index=index,
        receipts_dir=tmp_path / "receipts",
    )
    assert result.slug == "fresh-specialist"
    assert result.component_checksum == component.checksum
    assert result.root_digest != result.component_checksum


def test_inspect_specialist_repo_install_mode_rejects_already_installed_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "collide-me"
    component_root = _write_component(tmp_path / "component", slug=slug)
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))

    specialists_dir = tmp_path / "specialists"
    InstanceDir(specialists_dir / slug).stage_desired(_specialist_tuple(_specialist_binding(slug)))
    index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    index.load()
    assert slug in index.installed_slugs()

    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "main",
            staging_root=tmp_path / "staging",
            installed_index=index,
        )
    assert exc_info.value.kind == "slug_collision"


def test_inspect_failure_reclaims_staging_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#306: a rejected inspection (slug collision here) must delete the
    fetched staging tree — pre-fix every rejection leaked a full repo copy
    under /config/specialists/.staging until the volume filled."""
    slug = "collide-me"
    component_root = _write_component(tmp_path / "component", slug=slug)
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))

    specialists_dir = tmp_path / "specialists"
    InstanceDir(specialists_dir / slug).stage_desired(_specialist_tuple(_specialist_binding(slug)))
    index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    index.load()

    staging = tmp_path / ".staging"
    with pytest.raises(SpecialistInstallError):
        specialist_install.inspect_specialist_repo(
            "org/repo", "main", staging_root=staging, installed_index=index,
        )
    assert list(staging.iterdir()) == []


def test_inspect_success_retains_staging_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#306 companion pin: a SUCCESSFUL inspection keeps its staged tree —
    commit prefers the retained attested bytes over a refetch."""
    component_root = _write_component(tmp_path / "component", slug="fresh-specialist")
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))
    index = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    index.load()

    result = specialist_install.inspect_specialist_repo(
        "org/repo", "main", staging_root=tmp_path / ".staging",
        installed_index=index, receipts_dir=tmp_path / "receipts",
    )
    assert Path(result.staged_dir).is_dir()


def test_commit_active_reclaims_staging_tree(tmp_path: Path) -> None:
    """#306: a commit that reaches state="active" consumes the inspection
    staging tree (pre-fix it survived forever)."""
    import dataclasses

    inspection = _staged_inspection(tmp_path)
    staging = tmp_path / ".staging"
    staging.mkdir()
    staged = staging / "deadbeef"
    shutil.copytree(inspection.staged_dir, staged)
    inspection = dataclasses.replace(inspection, staged_dir=staged)

    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)

    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents-specialists",
    )
    assert instance.state == "active"
    assert not staged.exists()


def test_commit_pending_configuration_retains_staging_tree(tmp_path: Path) -> None:
    """#306 companion pin: a pending-configuration commit RETAINS the staged
    tree so the follow-up commit can reuse the attested bytes."""
    import dataclasses

    root = _write_component(tmp_path / "component", slug="mtg")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    schema = json.loads((root / "config-schema.json").read_text())
    schema["required"] = ["region"]
    (root / "config-schema.json").write_text(json.dumps(schema), encoding="utf-8")
    files = {
        "role/role.yaml": (root / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    component = load_specialist_component(root, root / "manifest.json")
    deps = resolve_dependency_closure(component, root)
    root_digest = specialist_install.compute_install_root_digest(
        component, deps, manifest_bytes=manifest_path.read_bytes())
    staging = tmp_path / ".staging"
    staging.mkdir()
    staged = staging / "cafebabe"
    shutil.copytree(root, staged)
    inspection = specialist_install.InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest,
        mission=str(component.role.role["mission"]),
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=("region",), required_secret_names=(),
        dependencies=deps, staged_dir=staged,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)

    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents-specialists",
    )
    assert instance.state == "pending-configuration"
    assert staged.is_dir()


def test_reclaim_staging_tree_containment_guard(tmp_path: Path) -> None:
    """#306: only a direct child of a `.staging` directory is ever removed —
    an arbitrary staged_dir (hand-built InspectionResult) is left alone."""
    outside = tmp_path / "precious"
    outside.mkdir()
    specialist_install.reclaim_staging_tree(outside)
    assert outside.is_dir()

    inside = tmp_path / ".staging" / "tree"
    inside.mkdir(parents=True)
    specialist_install.reclaim_staging_tree(inside)
    assert not inside.exists()


def test_sweep_staging_aged_removes_only_old_trees(tmp_path: Path) -> None:
    """#306: the boot sweep removes trees older than the cutoff and leaves
    fresh ones (an in-flight consent flow) untouched."""
    import time

    root_a = tmp_path / "specialists" / ".staging"
    root_b = tmp_path / "personas" / ".staging"
    old_a = root_a / "old"; old_a.mkdir(parents=True)
    (old_a / "f.txt").write_text("x", encoding="utf-8")
    fresh_a = root_a / "fresh"; fresh_a.mkdir()
    old_b = root_b / "old"; old_b.mkdir(parents=True)
    now = time.time()
    week_plus = now - 8 * 24 * 3600
    os.utime(old_a, (week_plus, week_plus))
    os.utime(old_b, (week_plus, week_plus))

    removed = specialist_install.sweep_staging_aged(
        roots=(root_a, root_b, tmp_path / "absent"), now=now)
    assert removed == 2
    assert not old_a.exists() and not old_b.exists()
    assert fresh_a.is_dir()


def test_inspect_specialist_repo_upgrade_mode_requires_target_slug(tmp_path: Path) -> None:
    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "main", mode="upgrade",
            staging_root=tmp_path / "staging",
        )
    assert exc_info.value.kind == "target_slug_required"


def test_inspect_specialist_repo_upgrade_mode_rejects_slug_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    component_root = _write_component(tmp_path / "component", slug="actual-slug")
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))
    index = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    index.load()

    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "main", mode="upgrade", target_slug="other-slug",
            staging_root=tmp_path / "staging",
            installed_index=index,
        )
    assert exc_info.value.kind == "slug_mismatch"


def test_inspect_specialist_repo_upgrade_mode_requires_active_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "needs-active"
    component_root = _write_component(tmp_path / "component", slug=slug)
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))

    specialists_dir = tmp_path / "specialists"
    index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    index.load()

    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "main", mode="upgrade", target_slug=slug,
            staging_root=tmp_path / "staging",
            installed_index=index,
            specialists_dir=specialists_dir,
        )
    assert exc_info.value.kind == "no_active_tuple"


def test_inspect_specialist_repo_upgrade_mode_succeeds_and_excludes_only_target_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_slug = "upgrade-target"
    other_slug = "still-collides"
    specialists_dir = tmp_path / "specialists"

    # target_slug has an ACTIVE tuple committed -> upgrade is sanctioned.
    target_dir = InstanceDir(specialists_dir / target_slug)
    target_dir.stage_desired(_specialist_tuple(_specialist_binding(target_slug)))
    target_dir.commit_desired_to_active()

    # other_slug is ALSO installed (pending-configuration is enough to
    # count towards installed_slugs()) but is NOT the upgrade target.
    InstanceDir(specialists_dir / other_slug).stage_desired(
        _specialist_tuple(_specialist_binding(other_slug)))

    index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    index.load()
    assert index.installed_slugs() == {target_slug, other_slug}

    component_root = _write_component(tmp_path / "component", slug=target_slug)
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))

    result = specialist_install.inspect_specialist_repo(
        "org/repo", "main", mode="upgrade", target_slug=target_slug,
        staging_root=tmp_path / "staging",
        installed_index=index,
        specialists_dir=specialists_dir,
        receipts_dir=tmp_path / "receipts",
    )
    assert result.slug == target_slug

    # Same index, same OTHER (non-excluded) slug, a plain fresh install
    # attempt must still collide — upgrade mode narrowly excludes only
    # target_slug, never any other already-installed slug.
    other_component_root = _write_component(tmp_path / "component-other", slug=other_slug)
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(other_component_root))
    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo2", "main",
            staging_root=tmp_path / "staging",
            installed_index=index,
        )
    assert exc_info.value.kind == "slug_collision"


def test_inspect_specialist_repo_rejects_dependency_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    component_root = _write_component(tmp_path / "component", slug="needs-corpus", dependencies=[
        {"kind": "corpus/data", "identifier": "missing-corpus", "digest": "sha256:" + "9" * 64},
    ])
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))
    index = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    index.load()

    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "main",
            staging_root=tmp_path / "staging",
            installed_index=index,
        )
    assert exc_info.value.kind == "dependency_unavailable"


def test_inspect_specialist_repo_rejects_forbidden_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    component_root = _write_component_with_role_yaml_comment_marker(
        tmp_path / "component", slug="marker-test")
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))
    index = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    index.load()

    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "main",
            staging_root=tmp_path / "staging",
            installed_index=index,
        )
    assert exc_info.value.kind == "forbidden_markers"


# ---------------------------------------------------------------------------
# commit_specialist_install (Step 12)
# ---------------------------------------------------------------------------


def _staged_inspection(
    tmp_path: Path, model: dict | None = None,
) -> "specialist_install.InspectionResult":
    from specialist_install import InspectionResult, compute_install_root_digest

    root = _write_component(tmp_path / "component", slug="mtg", model=model)
    component = load_specialist_component(root, root / "manifest.json")
    deps = resolve_dependency_closure(component, root)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(root / "manifest.json").read_bytes())
    return InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest,
        mission=str(component.role.role["mission"]),
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=deps, staged_dir=root,
    )


def test_commit_refuses_without_a_recorded_consent_ack(tmp_path: Path) -> None:
    inspection = _staged_inspection(tmp_path)
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")  # never recorded
    with pytest.raises(SpecialistInstallError) as raised:
        commit_specialist_install(
            inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents-specialists",
        )
    assert raised.value.kind == "consent_missing"
    assert not (tmp_path / "specialists" / "mtg").exists()  # nothing persisted


def test_commit_persists_cas_writes_active_tuple_and_materializes_operational_files(
    tmp_path: Path,
) -> None:
    inspection = _staged_inspection(tmp_path)
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)

    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents-specialists",
    )
    assert instance.state == "active"
    assert instance.active is not None
    assert instance.active.binding.mode == "component-default"
    assert instance.last_activation_error is None  # happy path: no self-heal note needed
    component_id, version, checksum = parse_component_root(instance.active.root)
    assert checksum == inspection.root_digest

    cas_role = tmp_path / "specialists" / "store" / checksum.removeprefix("sha256:") / "role"
    assert (cas_role / "role.yaml").is_file()
    op_dir = tmp_path / "agents-specialists" / "mtg"
    for name in ("character.yaml", "voice.yaml", "response_shape.yaml", "runtime.yaml"):
        assert (op_dir / name).is_file(), name


def test_commit_survives_a_materialize_failure_and_self_heals_on_next_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-4 fix (finding #2): commit_desired_to_active runs BEFORE
    materialize, so a materialize failure must NOT roll back the already-
    committed tuple — and the NEXT current_specialist_roles_dir call must
    repair the operational files with no operator action.

    N1b slice C converges this test onto the REAL `InstalledSpecialistIndex.
    installed_component_role_dirs()` (Step 17, specialist_registry.py) — the
    test-local `_IndexWithRoleDirs` forward shim slice B carried is gone;
    this drives `current_specialist_roles_dir` end-to-end against the real
    index, no subclass needed.
    """
    import specialist_materialize

    inspection = _staged_inspection(tmp_path)
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)

    original_materialize = specialist_materialize.materialize_specialist_operational_files
    call_count = {"n": 0}

    def _flaky_materialize(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("simulated disk-full on first materialize")
        return original_materialize(**kwargs)

    monkeypatch.setattr(specialist_materialize, "materialize_specialist_operational_files",
                         _flaky_materialize)
    # commit_specialist_install does a LOCAL `import specialist_materialize` — same
    # module object from sys.modules, so patching the attribute above is sufficient;
    # no separate patch of specialist_install's own namespace is needed or correct
    # (it has no module-level `specialist_materialize` name to patch).

    specialists_dir = tmp_path / "specialists"
    agents_specialists_dir = tmp_path / "agents-specialists"
    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
    )
    # The tuple is committed and active DESPITE the materialize failure —
    # never rolled back for a derived-cache write failure.
    assert instance.state == "active"
    assert instance.active is not None
    assert instance.last_activation_error is not None
    assert "pending reconcile" in instance.last_activation_error
    assert not (agents_specialists_dir / "mtg").exists()  # materialize genuinely never ran

    index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    index.load()
    roles_dir = specialist_materialize.current_specialist_roles_dir(
        installed_index=index, specialists_dir=specialists_dir,
        agents_specialists_dir=agents_specialists_dir,
    )
    assert roles_dir  # roles overlay still reconciled even though op-files needed a retry
    op_dir = agents_specialists_dir / "mtg"
    for name in ("character.yaml", "voice.yaml", "response_shape.yaml", "runtime.yaml"):
        assert (op_dir / name).is_file(), name  # self-healed with no operator action


def test_commit_binds_role_checksum_with_live_ha_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#355: install must materialize the role with the SAME option
    resolution the agent loader uses (`_ha_model_options`). Materializing
    with options={} binds an ha_option model at its DEFAULT — with a
    non-default PRIMARY_AGENT_MODEL the loader later resolves a different
    checksum and compile_prompt_bundle drops the valid specialist as a
    binding-activation failure."""
    from role_artifact import load_role_artifact
    from role_slot import _ha_model_options, materialize_role

    monkeypatch.setenv("PRIMARY_AGENT_MODEL", "sonnet")
    inspection = _staged_inspection(tmp_path, model={
        "source": "ha_option", "option": "primary_agent_model",
        "default": "opus", "allowed": ["opus", "sonnet"],
    })
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id,
                version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)

    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(),
        acks=acks,
        specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents-specialists",
    )
    assert instance.state == "active"
    _, _, checksum = parse_component_root(instance.active.root)
    cas_role = (tmp_path / "specialists" / "store"
                / checksum.removeprefix("sha256:") / "role")
    loader_role = materialize_role(
        source=load_role_artifact(cas_role), options=_ha_model_options())
    assert instance.active.binding.role_checksum == loader_role.checksum, (
        "persisted binding is bound to a role checksum the loader will "
        "never compute (install froze the ha_option default)")


def test_commit_with_missing_required_config_yields_pending_configuration(tmp_path: Path) -> None:
    root = _write_component(tmp_path / "component", slug="mtg")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    # This test's component declares no required config in its schema — rebuild
    # with one required, non-secret key to exercise the pending path.
    (root / "config-schema.json").write_text(
        json.dumps({"required": ["timezone"], "secret_names": []}), encoding="utf-8")
    files = {
        "role/role.yaml": (root / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    from specialist_install import InspectionResult, compute_install_root_digest

    component = load_specialist_component(root, manifest_path)
    deps = resolve_dependency_closure(component, root)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=manifest_path.read_bytes())
    inspection = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=("timezone",), required_secret_names=(), dependencies=deps,
        staged_dir=root,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)

    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents-specialists",
    )
    assert instance.state == "pending-configuration"
    assert instance.active is None
    assert instance.desired is not None
    assert not (tmp_path / "agents-specialists" / "mtg").exists()  # not materialized while pending


def _secret_schema_inspection(tmp_path: Path) -> "specialist_install.InspectionResult":
    """A component whose schema declares one required SECRET key (api_token)."""
    root = _write_component(tmp_path / "component", slug="mtg")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    (root / "config-schema.json").write_text(
        json.dumps({"required": ["api_token"], "secret_names": ["api_token"]}), encoding="utf-8")
    files = {
        "role/role.yaml": (root / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    from specialist_install import InspectionResult, compute_install_root_digest

    component = load_specialist_component(root, manifest_path)
    deps = resolve_dependency_closure(component, root)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=manifest_path.read_bytes())
    return InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=("api_token",), dependencies=deps,
        staged_dir=root,
    )


def _plain_schema_inspection(tmp_path: Path) -> "specialist_install.InspectionResult":
    """A component whose schema declares one required PLAIN key (api_token) —
    the pre-reclassification half of the #372 plain→secret scenario."""
    root = _write_component(tmp_path / "component-plain", slug="mtg")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    (root / "config-schema.json").write_text(
        json.dumps({"required": ["api_token"], "secret_names": []}), encoding="utf-8")
    files = {
        "role/role.yaml": (root / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    from specialist_install import InspectionResult, compute_install_root_digest

    component = load_specialist_component(root, manifest_path)
    deps = resolve_dependency_closure(component, root)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=manifest_path.read_bytes())
    return InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=("api_token",), required_secret_names=(),
        dependencies=deps, staged_dir=root,
    )


def _acked(inspection: "specialist_install.InspectionResult", tmp_path: Path) -> SpecialistInstallAckStore:
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)
    return acks


def test_commit_rejects_a_secret_valued_config_key(tmp_path: Path) -> None:
    """Pins INV-SPEC-006 (plain-channel refusal, #337). Red case demonstrated:
    pre-fix, a secret-named key with a plaintext VALUE in `config` both
    satisfied the requirement and was persisted verbatim into
    desired.yaml/active.yaml (under /config, included in snapshots/backups) —
    this test failed with state == "active" and the plaintext in the tuple."""
    inspection = _secret_schema_inspection(tmp_path)
    acks = _acked(inspection, tmp_path)

    with pytest.raises(SpecialistInstallError) as exc_info:
        commit_specialist_install(
            inspection=inspection, config={"api_token": "hunter2"},
            secret_names_provided=frozenset(), acks=acks,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents-specialists",
        )
    assert exc_info.value.kind == "secret_value_in_config"
    # Nothing staged: the refusal happens before any instance-dir mutation.
    assert not (tmp_path / "specialists" / "mtg" / "desired.yaml").exists()
    assert not (tmp_path / "specialists" / "mtg" / "active.yaml").exists()


def test_commit_rejects_an_undeclared_secret_names_provided_entry(tmp_path: Path) -> None:
    """#337 (validation gap noted in #331/#324): secret_names_provided may only
    name keys the schema declares in secret_names — pre-fix, claiming a required
    NON-secret key there falsely satisfied it with no value ever provided."""
    inspection = _secret_schema_inspection(tmp_path)
    # Rebuild the schema with a required non-secret key alongside the secret.
    root = inspection.staged_dir
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    (root / "config-schema.json").write_text(
        json.dumps({"required": ["timezone", "api_token"], "secret_names": ["api_token"]}),
        encoding="utf-8")
    files = {
        "role/role.yaml": (root / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    from specialist_install import InspectionResult, compute_install_root_digest
    component = load_specialist_component(root, manifest_path)
    deps = resolve_dependency_closure(component, root)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=manifest_path.read_bytes())
    inspection = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=("timezone",), required_secret_names=("api_token",),
        dependencies=deps, staged_dir=root,
    )
    acks = _acked(inspection, tmp_path)

    with pytest.raises(SpecialistInstallError) as exc_info:
        commit_specialist_install(
            inspection=inspection, config={},
            secret_names_provided=frozenset({"timezone", "api_token"}), acks=acks,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents-specialists",
        )
    assert exc_info.value.kind == "unknown_secret_name"
    assert "timezone" in exc_info.value.detail
    assert not (tmp_path / "specialists" / "mtg" / "desired.yaml").exists()


# ---------------------------------------------------------------------------
# upgrade_specialist / rollback_specialist / uninstall_specialist (Task N1c)
# ---------------------------------------------------------------------------


def _installed_mtg(tmp_path: Path) -> tuple[Path, Path, "specialist_install.InspectionResult"]:
    """Shared setup: a committed, active mtg install at version 0.1.0."""
    from specialist_component import load_specialist_component
    from specialist_install import InspectionResult, commit_specialist_install, resolve_dependency_closure
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

    staged = _write_component(tmp_path / "staged-v1", slug="mtg")
    component = load_specialist_component(staged, staged / "manifest.json")
    deps = resolve_dependency_closure(component, staged)
    from specialist_install import compute_install_root_digest
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(staged / "manifest.json").read_bytes())
    inspection = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=deps, staged_dir=staged,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)
    specialists_dir, agents_specialists_dir = tmp_path / "specialists", tmp_path / "agents-specialists"
    commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
    )
    return specialists_dir, agents_specialists_dir, inspection


def _v2_inspection(tmp_path: Path) -> "specialist_install.InspectionResult":
    from specialist_component import load_specialist_component
    from specialist_install import InspectionResult, resolve_dependency_closure

    staged = _write_component(tmp_path / "staged-v2", slug="mtg")
    manifest_path = staged / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = "0.2.0"
    files = {
        "role/role.yaml": (staged / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (staged / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (staged / "config-schema.json").read_bytes(),
    }
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    component = load_specialist_component(staged, manifest_path)
    deps = resolve_dependency_closure(component, staged)
    from specialist_install import compute_install_root_digest
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=manifest_path.read_bytes())
    return InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=deps, staged_dir=staged,
    )


def test_upgrade_commits_a_new_active_tuple_and_retains_the_prior_as_rollback_target(
    tmp_path: Path,
) -> None:
    from specialist_install import upgrade_specialist
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

    specialists_dir, agents_specialists_dir, v1 = _installed_mtg(tmp_path)
    v2 = _v2_inspection(tmp_path)
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(component_id=v2.component_id, version=v2.version,
                                         root_digest=v2.root_digest, slug=v2.slug)
    acks.record(identity=identity, component_id=v2.component_id, version=v2.version,
                component_checksum=v2.root_digest, slug=v2.slug)

    instance = upgrade_specialist(
        slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
    )
    assert instance.state == "active"
    assert instance.active.binding.persona_checksum  # sanity: compiled successfully
    assert (specialists_dir / "mtg" / "active.prior.yaml").exists()


def test_upgrade_with_missing_new_required_config_leaves_the_active_tuple_running(
    tmp_path: Path,
) -> None:
    from specialist_install import upgrade_specialist
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

    specialists_dir, agents_specialists_dir, v1 = _installed_mtg(tmp_path)
    v2 = _v2_inspection(tmp_path)
    manifest_path = v2.staged_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    (v2.staged_dir / "config-schema.json").write_text(
        json.dumps({"required": ["new_secret_flag"], "secret_names": ["new_secret_flag"]}),
        encoding="utf-8")
    files = {
        "role/role.yaml": (v2.staged_dir / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (v2.staged_dir / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (v2.staged_dir / "config-schema.json").read_bytes(),
    }
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    from specialist_install import (
        InspectionResult, compute_install_root_digest, resolve_dependency_closure,
    )
    component = load_specialist_component(v2.staged_dir, manifest_path)
    v2_deps = resolve_dependency_closure(component, v2.staged_dir)
    v2_root_digest = compute_install_root_digest(
        component, v2_deps, manifest_bytes=manifest_path.read_bytes())
    v2 = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=v2_root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=("new_secret_flag",),
        dependencies=v2_deps, staged_dir=v2.staged_dir,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(component_id=v2.component_id, version=v2.version,
                                         root_digest=v2.root_digest, slug=v2.slug)
    acks.record(identity=identity, component_id=v2.component_id, version=v2.version,
                component_checksum=v2.root_digest, slug=v2.slug)

    instance = upgrade_specialist(
        slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
    )
    assert instance.state == "pending-configuration"
    assert instance.active is not None  # the OLD (v1) active tuple keeps running
    assert instance.active.root != instance.desired.root  # desired is the staged v2 candidate


def _v2_secret_schema_inspection(tmp_path: Path, v2: "specialist_install.InspectionResult") -> "specialist_install.InspectionResult":
    """Rebuild the staged v2 with a schema declaring api_token as a required secret."""
    manifest_path = v2.staged_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    (v2.staged_dir / "config-schema.json").write_text(
        json.dumps({"required": ["api_token"], "secret_names": ["api_token"]}), encoding="utf-8")
    files = {
        "role/role.yaml": (v2.staged_dir / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (v2.staged_dir / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (v2.staged_dir / "config-schema.json").read_bytes(),
    }
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    from specialist_install import InspectionResult, compute_install_root_digest, resolve_dependency_closure
    component = load_specialist_component(v2.staged_dir, manifest_path)
    deps = resolve_dependency_closure(component, v2.staged_dir)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=manifest_path.read_bytes())
    return InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=("api_token",),
        dependencies=deps, staged_dir=v2.staged_dir,
    )


def test_upgrade_rejects_a_secret_valued_config_key(tmp_path: Path) -> None:
    """#337 (upgrade arm): the same plaintext-secret refusal applies to
    upgrade_specialist — and the refusal leaves the v1 active tuple untouched."""
    from specialist_install import upgrade_specialist

    specialists_dir, agents_specialists_dir, v1 = _installed_mtg(tmp_path)
    v2 = _v2_secret_schema_inspection(tmp_path, _v2_inspection(tmp_path))
    acks = _acked(v2, tmp_path)
    active_before = (specialists_dir / "mtg" / "active.yaml").read_bytes()

    with pytest.raises(SpecialistInstallError) as exc_info:
        upgrade_specialist(
            slug="mtg", inspection=v2, config={"api_token": "hunter2"},
            secret_names_provided=frozenset(), acks=acks,
            specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
        )
    assert exc_info.value.kind == "secret_value_in_config"
    assert (specialists_dir / "mtg" / "active.yaml").read_bytes() == active_before


def test_upgrade_refuses_a_pre_guard_active_tuple(tmp_path: Path) -> None:
    """#372: a pre-guard active — its config_digest computed over a mapping
    that no longer equals the persisted snapshot — is refused with a typed
    error instead of being upgraded over; recovery is uninstall + reinstall.
    (Replaces the pre-#372 pinning of the upgrade-time plaintext strip, which
    relied on such tuples being loadable.)"""
    from specialist_install import upgrade_specialist

    specialists_dir, agents_specialists_dir, v1 = _installed_mtg(tmp_path)
    active_path = specialists_dir / "mtg" / "active.yaml"
    raw = yaml.safe_load(active_path.read_text(encoding="utf-8"))
    raw["config_snapshot"] = {"api_token": "hunter2-legacy-plaintext"}
    active_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    before = active_path.read_bytes()

    v2 = _v2_secret_schema_inspection(tmp_path, _v2_inspection(tmp_path))
    acks = _acked(v2, tmp_path)

    with pytest.raises(SpecialistInstallError) as exc_info:
        upgrade_specialist(
            slug="mtg", inspection=v2, config={},
            secret_names_provided=frozenset({"api_token"}), acks=acks,
            specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
        )
    assert exc_info.value.kind == "active_unreadable"
    assert "#372" in str(exc_info.value)
    assert active_path.read_bytes() == before  # refusal never mutates the active


def test_upgrade_reclassifying_a_secret_as_plain_does_not_carry_its_plaintext(tmp_path: Path) -> None:
    """Terra r2 (#337): the carried-snapshot strip must use the PRIOR schema's
    secret_names too — if the incoming component reclassifies a key from
    secret to plain-required, the old plaintext must not ride the carry-over
    into the new active.yaml. The operator supplies a fresh plain value (or
    the upgrade lands pending-configuration). #372 rewrite: the pre-guard
    hand-edit is gone (such an active no longer loads — see
    test_upgrade_refuses_a_pre_guard_active_tuple); what stays pinned is that
    the merge NEVER carries a key either schema calls secret."""
    from specialist_install import upgrade_specialist
    from personality_binding import load_instance_tuple

    # v1 whose CAS schema DECLARES api_token secret; installed via the secret
    # channel (post-guard: the snapshot never holds the value).
    v1 = _secret_schema_inspection(tmp_path)
    acks_v1 = _acked(v1, tmp_path)
    specialists_dir = tmp_path / "specialists"
    agents_specialists_dir = tmp_path / "agents-specialists"
    commit_specialist_install(
        inspection=v1, config={}, secret_names_provided=frozenset({"api_token"}),
        acks=acks_v1, specialists_dir=specialists_dir,
        agents_specialists_dir=agents_specialists_dir,
    )

    # v2 reclassifies api_token as required NON-secret.
    v2 = _v2_inspection(tmp_path)
    manifest_path = v2.staged_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    (v2.staged_dir / "config-schema.json").write_text(
        json.dumps({"required": ["api_token"], "secret_names": []}), encoding="utf-8")
    files = {
        "role/role.yaml": (v2.staged_dir / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (v2.staged_dir / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (v2.staged_dir / "config-schema.json").read_bytes(),
    }
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    from specialist_install import InspectionResult, compute_install_root_digest
    component = load_specialist_component(v2.staged_dir, manifest_path)
    deps = resolve_dependency_closure(component, v2.staged_dir)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=manifest_path.read_bytes())
    v2 = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=("api_token",), required_secret_names=(),
        dependencies=deps, staged_dir=v2.staged_dir,
    )
    acks_v2 = _acked(v2, tmp_path)

    instance = upgrade_specialist(
        slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(),
        acks=acks_v2, specialists_dir=specialists_dir,
        agents_specialists_dir=agents_specialists_dir,
    )
    # No fresh plain value supplied → pending-configuration, never a silent
    # carry-over of the old secret's plaintext.
    assert instance.state == "pending-configuration"
    staged = load_instance_tuple(specialists_dir / "mtg" / "desired.yaml")
    assert "api_token" not in staged.config_snapshot


def test_reclassifying_upgrade_sentinels_the_prior_and_rollback_refuses(tmp_path: Path) -> None:
    """#372 (D5b + D5): an upgrade whose incoming schema reclassifies a
    persisted PLAIN key as secret leaves a prior whose digest was computed
    over a mapping containing that (now-secret) value. The upgrade must
    sentinel the prior's digests in the same write that strips the plaintext,
    and rollback must refuse that prior with a typed legacy_prior error while
    leaving the new active untouched."""
    from specialist_install import rollback_specialist, upgrade_specialist
    from personality_binding import PRE_GUARD_SENTINEL, load_instance_tuple

    # v1 declares api_token PLAIN-required; install persists the value
    # honestly (equation holds; this is legal post-guard state).
    v1 = _plain_schema_inspection(tmp_path)
    acks_v1 = _acked(v1, tmp_path)
    specialists_dir = tmp_path / "specialists"
    agents_specialists_dir = tmp_path / "agents-specialists"
    commit_specialist_install(
        inspection=v1, config={"api_token": "plain-then-reclassified"},
        secret_names_provided=frozenset(), acks=acks_v1,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
    )

    # v2 reclassifies api_token as SECRET; supply it via the secret channel.
    v2 = _v2_secret_schema_inspection(tmp_path, _v2_inspection(tmp_path))
    acks = _acked(v2, tmp_path)
    instance = upgrade_specialist(
        slug="mtg", inspection=v2, config={},
        secret_names_provided=frozenset({"api_token"}), acks=acks,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
    )
    assert instance.state == "active"

    prior_path = specialists_dir / "mtg" / "active.prior.yaml"
    raw_prior = yaml.safe_load(prior_path.read_text(encoding="utf-8"))
    assert "api_token" not in (raw_prior.get("config_snapshot") or {})
    assert raw_prior["config_digest"] == PRE_GUARD_SENTINEL
    assert raw_prior["binding"]["effective_config_digest"] == PRE_GUARD_SENTINEL

    active_path = specialists_dir / "mtg" / "active.yaml"
    active_before = active_path.read_bytes()
    with pytest.raises(SpecialistInstallError) as exc_info:
        rollback_specialist(
            slug="mtg", specialists_dir=specialists_dir,
            agents_specialists_dir=agents_specialists_dir)
    assert exc_info.value.kind == "legacy_prior"
    assert active_path.read_bytes() == active_before
    # The committed v2 active stays loadable and secret-free.
    committed = load_instance_tuple(active_path)
    assert "api_token" not in committed.config_snapshot


def test_rollback_refuses_an_unsanitized_pre_guard_prior(tmp_path: Path) -> None:
    """#372 (D5): a prior in the pre-v0.137 shape — plaintext secret present
    AND a digest validly computed over that secret-bearing mapping — must be
    refused, never restored-with-strip (the strip would persist a digest not
    derived from the stripped snapshot)."""
    from specialist_install import rollback_specialist, upgrade_specialist
    from personality_binding import compute_effective_config_digest

    # v1's OWN schema declares api_token secret — the forged prior below is
    # the pre-v0.137 shape of exactly this component.
    v1 = _secret_schema_inspection(tmp_path)
    acks_v1 = _acked(v1, tmp_path)
    specialists_dir = tmp_path / "specialists"
    agents_specialists_dir = tmp_path / "agents-specialists"
    commit_specialist_install(
        inspection=v1, config={}, secret_names_provided=frozenset({"api_token"}),
        acks=acks_v1, specialists_dir=specialists_dir,
        agents_specialists_dir=agents_specialists_dir,
    )
    v2 = _v2_secret_schema_inspection(tmp_path, _v2_inspection(tmp_path))
    acks = _acked(v2, tmp_path)
    upgrade_specialist(
        slug="mtg", inspection=v2, config={},
        secret_names_provided=frozenset({"api_token"}), acks=acks,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
    )

    # Forge the pre-v0.137 shape: secret-named key present, equation VALID.
    prior_path = specialists_dir / "mtg" / "active.prior.yaml"
    raw = yaml.safe_load(prior_path.read_text(encoding="utf-8"))
    snapshot = {"api_token": "hunter2-legacy-plaintext"}
    raw["config_snapshot"] = snapshot
    raw["config_digest"] = compute_effective_config_digest(snapshot)
    raw["binding"]["effective_config_digest"] = raw["config_digest"]
    from personality_binding import compute_binding_digest
    raw["binding"]["binding_digest"] = compute_binding_digest(
        stable_agent_id=raw["binding"]["stable_agent_id"],
        role_checksum=raw["binding"]["role_checksum"],
        persona_id=raw["binding"]["persona_id"],
        persona_version=raw["binding"]["persona_version"],
        persona_checksum=raw["binding"]["persona_checksum"],
        compiler_schema_version=raw["binding"]["compiler_schema_version"],
        dependency_digests=tuple(raw["binding"].get("dependency_digests") or ()),
        effective_config_digest=raw["binding"]["effective_config_digest"],
    )
    prior_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(SpecialistInstallError) as exc_info:
        rollback_specialist(
            slug="mtg", specialists_dir=specialists_dir,
            agents_specialists_dir=agents_specialists_dir)
    assert exc_info.value.kind == "legacy_prior"


def test_rollback_restores_the_prior_tuple(tmp_path: Path) -> None:
    """Pins INV-SPEC-003 (rollback half). Red case demonstrated: reading the current active instead of active.prior.yaml fails this test."""
    from specialist_install import parse_component_root, rollback_specialist, upgrade_specialist
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

    specialists_dir, agents_specialists_dir, v1 = _installed_mtg(tmp_path)
    v2 = _v2_inspection(tmp_path)
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(component_id=v2.component_id, version=v2.version,
                                         root_digest=v2.root_digest, slug=v2.slug)
    acks.record(identity=identity, component_id=v2.component_id, version=v2.version,
                component_checksum=v2.root_digest, slug=v2.slug)
    upgrade_specialist(slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(),
                        acks=acks, specialists_dir=specialists_dir,
                        agents_specialists_dir=agents_specialists_dir)

    rolled_back = rollback_specialist(
        slug="mtg", specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir)
    assert rolled_back.active.binding.component_root is not None
    _, _, checksum = parse_component_root(rolled_back.active.root)
    assert checksum == v1.root_digest  # back to the pre-upgrade version


def test_rollback_with_no_prior_tuple_raises(tmp_path: Path) -> None:
    from specialist_install import rollback_specialist

    specialists_dir, agents_specialists_dir, _v1 = _installed_mtg(tmp_path)
    with pytest.raises(SpecialistInstallError) as raised:
        rollback_specialist(slug="mtg", specialists_dir=specialists_dir,
                             agents_specialists_dir=agents_specialists_dir)
    assert raised.value.kind == "no_prior_tuple"


def test_uninstall_removes_the_instance_dir_and_operational_files(tmp_path: Path) -> None:
    from specialist_install import uninstall_specialist

    specialists_dir, agents_specialists_dir, _v1 = _installed_mtg(tmp_path)
    op_dir = agents_specialists_dir / "mtg"
    assert op_dir.is_symlink()  # sanity: the fixture installed via the real pipeline
    content_dir = agents_specialists_dir / os.readlink(op_dir)

    uninstall_specialist(slug="mtg", specialists_dir=specialists_dir,
                          agents_specialists_dir=agents_specialists_dir)
    assert not (specialists_dir / "mtg").exists()
    assert not os.path.lexists(op_dir)  # Round-4 fix (finding #1): symlink itself is gone, not
                                          # just dangling (shutil.rmtree silently no-ops on a symlink)
    assert not content_dir.exists()  # and its versioned content directory with it


# ---------------------------------------------------------------------------
# cas_pin_roots / persona_pin_roots (Task N1d, spec §4.4)
# ---------------------------------------------------------------------------


def test_cas_pin_roots_includes_active_desired_and_prior_checksums(tmp_path: Path) -> None:
    from specialist_install import cas_pin_roots, upgrade_specialist
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

    specialists_dir, agents_specialists_dir, v1 = _installed_mtg(tmp_path)
    v2 = _v2_inspection(tmp_path)
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(component_id=v2.component_id, version=v2.version,
                                         root_digest=v2.root_digest, slug=v2.slug)
    acks.record(identity=identity, component_id=v2.component_id, version=v2.version,
                component_checksum=v2.root_digest, slug=v2.slug)
    upgrade_specialist(slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(),
                        acks=acks, specialists_dir=specialists_dir,
                        agents_specialists_dir=agents_specialists_dir)

    # cas_pin_roots parses component_root, which embeds the full-closure
    # root_digest (Round-2, finding #2) — not the narrow component_checksum.
    pinned = cas_pin_roots(specialists_dir)
    assert v1.root_digest in pinned  # retained via active.prior.yaml
    assert v2.root_digest in pinned  # current active


def test_cas_pin_roots_on_missing_directory_returns_empty(tmp_path: Path) -> None:
    from specialist_install import cas_pin_roots

    assert cas_pin_roots(tmp_path / "does-not-exist") == frozenset()


def test_cas_pin_roots_pins_an_override_bound_specialists_component_root(tmp_path: Path, monkeypatch) -> None:
    """Round-2 fix (finding #8/#4's exposed bug): an OVERRIDE-mode specialist
    binding has `binding.component_root is None` (only component-default
    populates it) — cas_pin_roots must still pin the component blob via
    `InstanceTuple.root` (which apply_persona_override never rewrites for a
    specialist target), not via `binding.component_root`."""
    from persona_install import apply_persona_override
    from persona_pack import load_persona_pack
    from specialist_install import cas_pin_roots, cas_store_dir, parse_component_root
    from personality_binding import InstanceDir
    from role_artifact import load_role_artifact
    from role_slot import materialize_role

    specialists_dir, _agents_dir, v1 = _installed_mtg(tmp_path)
    active = InstanceDir(specialists_dir / "mtg").active()
    _, _, checksum = parse_component_root(active.root)
    cas_dir = cas_store_dir(checksum, store_root=specialists_dir / "store")
    role = materialize_role(source=load_role_artifact(cas_dir / "role"), options={})

    # persona_id/version must satisfy the mtg role's persona_requirements
    # ("casa/judge@>=0.1.0 <1.0.0", from _write_component above) — a
    # DIFFERENT version of the SAME compatible slug, not an unrelated one.
    # #543: build it straight into the approved installed root (through the
    # $CASA_CONFIG_DIR seam) — apply_persona_override re-proves in-lock that
    # the persona it is about to pin is resolvable there.
    monkeypatch.setenv("CASA_CONFIG_DIR", str(tmp_path / "config-root"))
    override_dir = tmp_path / "config-root" / "personas" / "casa" / "judge" / "0.2.0"
    persona_yaml = {
        "api_version": "casa.persona/v1", "id": "casa/judge", "version": "0.2.0",
        "trait_schema_version": 1,
        "identity": {"display_name": "Judge Two", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        "relationship_posture": "established", "archetype": "adjudicator",
        "traits": {"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                    "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        "quirks": [],
    }
    (override_dir / "pack").mkdir(parents=True)
    (override_dir / "pack" / "persona.yaml").write_text(
        yaml.safe_dump(persona_yaml, sort_keys=False), encoding="utf-8")
    core = "Q" * 350
    (override_dir / "pack" / "persona.md").write_text(
        f"# Core\n\n{core}\n\n## Negative space\n\nNever guesses.\n", encoding="utf-8")
    rows = []
    for name in sorted(os.listdir(override_dir / "pack")):
        text = canonical_text((override_dir / "pack" / name).read_text(encoding="utf-8"))
        rows.append({"path": name, "type": "file", "executable": False,
                      "checksum": checksum_bytes(text.encode("utf-8"))})
    payload = {"api_version": "casa.persona.manifest/v1", "files": rows}
    payload["checksum"] = checksum_bytes(canonical_json_bytes(payload))
    (override_dir / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    persona = load_persona_pack(override_dir / "pack", override_dir / "manifest.json")

    apply_persona_override(
        target_role_id="specialist:mtg", persona=persona, role=role,
        instance_dir_root=specialists_dir / "mtg",
    )
    pinned = cas_pin_roots(specialists_dir)
    assert v1.root_digest in pinned  # the component blob is STILL pinned post-override


def test_persona_pin_roots_always_includes_image_defaults(tmp_path: Path) -> None:
    from specialist_install import persona_pin_roots
    from personality_binding import IMAGE_DEFAULT_PERSONA_BY_SLOT

    pinned = persona_pin_roots(
        bindings_dir=tmp_path / "bindings", specialists_dir=tmp_path / "specialists")
    for ref in IMAGE_DEFAULT_PERSONA_BY_SLOT.values():
        assert ref in pinned


def test_persona_pin_roots_includes_a_resident_override_binding(
        tmp_path: Path, monkeypatch) -> None:
    from persona_install import apply_persona_override
    from persona_pack import load_persona_pack
    from role_artifact import load_role_artifact
    from role_slot import materialize_role
    from specialist_install import persona_pin_roots
    from test_persona_install import _write_persona_repo

    role_dir = (
        Path(__file__).resolve().parent.parent
        / "casa/rootfs/opt/casa/defaults/roles/resident/assistant"
    )
    role = materialize_role(source=load_role_artifact(role_dir), options={})
    # #543: apply_persona_override re-proves the persona is resolvable under
    # the approved roots before staging, so install it where the loader looks.
    from test_persona_install import install_persona_for_apply
    persona = install_persona_for_apply(
        tmp_path, monkeypatch, persona_id="casa/ellen", version="0.1.0")

    bindings_dir = tmp_path / "bindings"
    apply_persona_override(
        target_role_id="resident:assistant", persona=persona, role=role,
        instance_dir_root=bindings_dir / "resident-assistant",
    )

    pinned = persona_pin_roots(bindings_dir=bindings_dir, specialists_dir=tmp_path / "specialists")
    assert "casa/ellen@0.1.0" in pinned


def test_persona_pin_roots_includes_an_override_bound_specialist(tmp_path: Path, monkeypatch) -> None:
    from persona_install import apply_persona_override
    from persona_pack import load_persona_pack
    from personality_binding import InstanceDir
    from role_artifact import load_role_artifact
    from role_slot import materialize_role
    from specialist_install import cas_store_dir, parse_component_root, persona_pin_roots
    from test_persona_install import _write_persona_repo

    specialists_dir, _agents_dir, _v1 = _installed_mtg(tmp_path)
    active = InstanceDir(specialists_dir / "mtg").active()
    _, _, checksum = parse_component_root(active.root)
    cas_dir = cas_store_dir(checksum, store_root=specialists_dir / "store")
    role = materialize_role(source=load_role_artifact(cas_dir / "role"), options={})

    # persona_id/version must satisfy the mtg role's persona_requirements
    # ("casa/judge@>=0.1.0 <1.0.0", from _write_component above).
    # #543: see install_persona_for_apply — an override must name a persona
    # the specialist loader can actually resolve at activation.
    from test_persona_install import install_persona_for_apply
    persona = install_persona_for_apply(
        tmp_path, monkeypatch, persona_id="casa/judge", version="0.3.0")

    apply_persona_override(
        target_role_id="specialist:mtg", persona=persona, role=role,
        instance_dir_root=specialists_dir / "mtg",
    )

    pinned = persona_pin_roots(bindings_dir=tmp_path / "bindings", specialists_dir=specialists_dir)
    assert "casa/judge@0.3.0" in pinned


# ---------------------------------------------------------------------------
# Whole-branch review ROUND 2 — F1 (inspection.slug traversal),
# F2 (persona dependency row required structurally).
# ---------------------------------------------------------------------------


def _canary_paths(tmp_path: Path) -> list[Path]:
    """Locations a traversal slug (`../evil`) joined onto `specialists_dir` /
    `agents_specialists_dir` would escape to — asserting these stay absent is
    the F1 canary that nothing was written outside the two roots."""
    return [
        tmp_path / "evil",  # ../evil from tmp_path/specialists
        tmp_path.parent / "evil",
    ]


def test_commit_refuses_a_traversal_inspection_slug_and_writes_nothing_outside_roots(
    tmp_path: Path,
) -> None:
    """F1: a hand-built InspectionResult whose `slug` is a traversal string —
    WITH a matching recorded consent ack — must be refused with a typed error
    at the lifecycle-function boundary, before any Path join escapes the roots."""
    from specialist_install import InspectionResult, compute_install_root_digest

    root = _write_component(tmp_path / "component", slug="mtg")
    component = load_specialist_component(root, root / "manifest.json")
    deps = resolve_dependency_closure(component, root)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(root / "manifest.json").read_bytes())
    evil_slug = "../evil"
    inspection = InspectionResult(
        component_id=component.component_id, version=component.version, slug=evil_slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=deps, staged_dir=root,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)

    specialists_dir = tmp_path / "specialists"
    agents_specialists_dir = tmp_path / "agents-specialists"
    with pytest.raises(SpecialistInstallError) as raised:
        commit_specialist_install(
            inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
            specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
        )
    assert raised.value.kind == "invalid_slug"
    for canary in _canary_paths(tmp_path):
        assert not canary.exists(), canary


def test_commit_refuses_when_cas_component_slug_disagrees_with_inspection_slug(
    tmp_path: Path,
) -> None:
    """F1 (second half): a VALID-but-WRONG inspection.slug (passes the slug
    regex, differs from the component's own declared slug) is caught by the
    post-publish CAS reload assert — binding slug X's approval to component Y's
    bytes is refused with slug_mismatch, and slug X's instance dir is never
    created."""
    from specialist_install import InspectionResult, compute_install_root_digest

    root = _write_component(tmp_path / "component", slug="mtg")
    component = load_specialist_component(root, root / "manifest.json")
    deps = resolve_dependency_closure(component, root)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(root / "manifest.json").read_bytes())
    wrong_slug = "notmtg"  # valid single-segment slug, but != component.slug ("mtg")
    inspection = InspectionResult(
        component_id=component.component_id, version=component.version, slug=wrong_slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=deps, staged_dir=root,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)

    specialists_dir = tmp_path / "specialists"
    with pytest.raises(SpecialistInstallError) as raised:
        commit_specialist_install(
            inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
            specialists_dir=specialists_dir, agents_specialists_dir=tmp_path / "agents-specialists",
        )
    assert raised.value.kind == "slug_mismatch"
    assert not (specialists_dir / wrong_slug).exists()  # no instance dir for the wrong slug


def test_resolve_dependency_closure_refuses_empty_dependencies_no_persona_row(
    tmp_path: Path,
) -> None:
    """F2: a component with `dependencies: []` (no persona row) still declares
    a default_persona — resolve_dependency_closure must flag the missing
    persona binding as unavailable rather than silently activating the bundled
    default with no identity/checksum attestation."""
    root = _write_component(tmp_path / "component", slug="mtg", dependencies=[])
    component = load_specialist_component(root, root / "manifest.json")
    resolutions = resolve_dependency_closure(component, root)
    persona_rows = [r for r in resolutions if r.kind == "persona"]
    assert len(persona_rows) == 1
    assert persona_rows[0].available is False
    assert "persona dependency row missing/mismatched" == persona_rows[0].detail


def test_inspect_refuses_a_component_with_no_persona_dependency_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2 (inspect leg): the missing-persona-row failure flows into
    dependency_unavailable at inspection time."""
    component_root = _write_component(tmp_path / "component", slug="mtg", dependencies=[])
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))
    index = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    index.load()
    with pytest.raises(SpecialistInstallError) as raised:
        specialist_install.inspect_specialist_repo(
            "org/repo", "main", staging_root=tmp_path / "staging", installed_index=index)
    assert raised.value.kind == "dependency_unavailable"
    assert "persona dependency row missing/mismatched" in raised.value.detail


def test_commit_refuses_a_component_with_no_persona_dependency_row(tmp_path: Path) -> None:
    """F2 (commit leg): even bypassing inspect with a hand-built InspectionResult
    and a matching ack, the CAS-staging re-verification refuses the unbound
    default persona."""
    from specialist_install import InspectionResult, compute_install_root_digest

    root = _write_component(tmp_path / "component", slug="mtg", dependencies=[])
    component = load_specialist_component(root, root / "manifest.json")
    deps = resolve_dependency_closure(component, root)  # already contains the failing persona row
    # Recompute the digest as commit will: from the on-disk manifest bytes.
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=(root / "manifest.json").read_bytes())
    inspection = InspectionResult(
        component_id=component.component_id, version=component.version, slug="mtg",
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=deps, staged_dir=root,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)
    with pytest.raises(SpecialistInstallError) as raised:
        commit_specialist_install(
            inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents-specialists",
        )
    assert raised.value.kind == "dependency_unavailable"


def test_selfheal_reads_the_active_tuple_inside_the_lock_not_the_stale_snapshot(
    tmp_path: Path,
) -> None:
    """F3: `_reconcile_specialist_operational_files` must RE-READ the active
    tuple from disk inside MATERIALIZE_LOCK, so a tuple committed AFTER the
    installed_index was snapshotted (simulating a concurrent upgrade/commit)
    wins — its binding_digest is what lands in the materialized op-dir marker,
    never the stale snapshot's."""
    import specialist_materialize
    from personality_binding import InstanceDir

    specialists_dir, agents_specialists_dir, _v1 = _installed_mtg(tmp_path)

    # Snapshot the index while tuple A is active (mirrors current_specialist_
    # roles_dir loading the index once before the reconcile loop runs).
    index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    index.load()
    tuple_a = InstanceDir(specialists_dir / "mtg").active()
    assert tuple_a is not None

    # Commit a DIFFERENT tuple B to active.yaml AFTER the snapshot — same
    # component root (so CAS resolves) but a distinct binding via a different
    # effective_config_digest, hence a different binding_digest.
    # #372: the distinct digest must be honestly derived from a distinct
    # snapshot — the write backstop refuses hand-picked digest strings.
    from personality_binding import compute_effective_config_digest
    snapshot_b = {"variant": "b"}
    binding_b = _specialist_binding(
        "mtg", component_root=tuple_a.root,
        effective_config_digest=compute_effective_config_digest(snapshot_b))
    assert binding_b.binding_digest != tuple_a.binding.binding_digest
    tuple_b = InstanceTuple(
        root=tuple_a.root, binding=binding_b, config_snapshot=snapshot_b,
        config_digest=binding_b.effective_config_digest)
    instance_dir = InstanceDir(specialists_dir / "mtg")
    instance_dir.stage_desired(tuple_b)
    instance_dir.commit_desired_to_active()
    assert InstanceDir(specialists_dir / "mtg").active().binding.binding_digest == binding_b.binding_digest

    # Reconcile using the STALE index (still holds tuple A). The in-lock
    # re-read must pick up tuple B from disk.
    specialist_materialize._reconcile_specialist_operational_files(
        installed_index=index, specialists_dir=specialists_dir,
        agents_specialists_dir=agents_specialists_dir)

    marker = specialist_materialize._read_binding_marker(agents_specialists_dir / "mtg")
    assert marker is not None
    assert marker["binding_digest"] == binding_b.binding_digest  # B won, not the stale A


def test_inspect_maps_a_schema_invalid_manifest_to_manifest_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#346: load_specialist_component raises jsonschema.ValidationError (its
    pinned type for a schema-violating manifest), but inspection caught only
    ValueError — a malformed FETCHED manifest escaped the structured error
    contract as a raw ValidationError instead of kind='manifest_invalid'."""
    import json as _json

    component_root = _write_component(tmp_path / "component", slug="fresh-specialist")
    manifest_path = component_root / "manifest.json"
    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["component_id"]  # schema-required field
    manifest_path.write_text(_json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(component_root))

    index = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    index.load()
    with pytest.raises(SpecialistInstallError) as exc_info:
        specialist_install.inspect_specialist_repo(
            "org/repo", "main",
            staging_root=tmp_path / "staging",
            installed_index=index,
            receipts_dir=tmp_path / "receipts",
        )
    assert exc_info.value.kind == "manifest_invalid"


def test_parse_component_root_rejects_a_non_hex_checksum_suffix(tmp_path: Path) -> None:
    """#346: parse_component_root accepted ANY 'sha256:'-prefixed suffix and
    cas_store_dir joins it straight into the store path — a tampered tuple
    root like '...#sha256:../../outside' resolved OUTSIDE the CAS store. The
    checksum segment must be exactly 64 hex chars."""
    from specialist_install import cas_store_dir, parse_component_root

    good = "casa/mtg@0.1.0#sha256:" + "a" * 64
    assert parse_component_root(good)[2] == "sha256:" + "a" * 64

    for evil in (
        "casa/mtg@0.1.0#sha256:../../outside",
        "casa/mtg@0.1.0#sha256:" + "a" * 63,
        "casa/mtg@0.1.0#sha256:" + "A" * 64,   # canonical form is lower-hex
        "casa/mtg@0.1.0#sha256:a/b" + "a" * 60,
    ):
        with pytest.raises(ValueError, match="component_root"):
            parse_component_root(evil)

    # Containment: whatever parse accepts, the store join stays in the store.
    store = tmp_path / "store"
    resolved = cas_store_dir(parse_component_root(good)[2], store_root=store)
    assert store in resolved.parents


def test_boot_sweep_scrubs_legacy_plaintext_from_all_tuple_snapshots(tmp_path: Path) -> None:
    """Sol r2 (#337): a pre-guard install keeps plaintext in tracked tuple
    files until that slug's next upgrade — and the post-commit prior
    sanitization has a crash window. The boot sweep (which runs BEFORE the
    boot config-git snapshot) scrubs every persisted snapshot against its own
    component's secret_names declaration."""
    from specialist_install import sanitize_specialist_snapshots
    from personality_binding import load_instance_tuple

    v1 = _secret_schema_inspection(tmp_path)
    acks = _acked(v1, tmp_path)
    specialists_dir = tmp_path / "specialists"
    commit_specialist_install(
        inspection=v1, config={}, secret_names_provided=frozenset({"api_token"}),
        acks=acks, specialists_dir=specialists_dir,
        agents_specialists_dir=tmp_path / "agents-specialists",
    )
    slug_dir = specialists_dir / "mtg"
    active_raw = yaml.safe_load((slug_dir / "active.yaml").read_text(encoding="utf-8"))
    active_raw["config_snapshot"] = {"api_token": "hunter2-legacy-plaintext"}
    (slug_dir / "active.yaml").write_text(yaml.safe_dump(active_raw, sort_keys=False), encoding="utf-8")
    (slug_dir / "active.prior.yaml").write_text(yaml.safe_dump(active_raw, sort_keys=False), encoding="utf-8")
    error_raw = dict(active_raw)
    error_raw["_error_reason"] = "compile boom"
    (slug_dir / "desired.error.yaml").write_text(yaml.safe_dump(error_raw, sort_keys=False), encoding="utf-8")

    cleaned = sanitize_specialist_snapshots(specialists_dir=specialists_dir)

    assert cleaned == 3
    for filename in ("active.yaml", "active.prior.yaml", "desired.error.yaml"):
        payload = yaml.safe_load((slug_dir / filename).read_text(encoding="utf-8"))
        assert "api_token" not in payload["config_snapshot"], filename
    # The scrub preserves everything else: the tuples still load, and the
    # error file keeps its reason.
    assert load_instance_tuple(slug_dir / "active.yaml") is not None
    assert load_instance_tuple(slug_dir / "active.prior.yaml") is not None
    error_payload = yaml.safe_load((slug_dir / "desired.error.yaml").read_text(encoding="utf-8"))
    assert error_payload["_error_reason"] == "compile boom"
    # Idempotent: a second sweep finds nothing to clean.
    assert sanitize_specialist_snapshots(specialists_dir=specialists_dir) == 0


def _delete_stored_schema(specialists_dir: Path) -> None:
    """Simulate a damaged CAS: remove the stored component's config schema."""
    for schema_file in (specialists_dir / "store").rglob("config-schema.json"):
        schema_file.unlink()


def test_upgrade_with_unloadable_prior_schema_carries_nothing(tmp_path: Path) -> None:
    """Sol r3 (#337): an unloadable prior CAS schema must fail CLOSED — the
    carry-over is dropped entirely rather than trusting an empty secret-name
    set and riding legacy plaintext into the new tuple."""
    from specialist_install import upgrade_specialist
    from personality_binding import load_instance_tuple

    # #372 rewrite: v1 persists api_token as an HONEST plain value (the
    # pre-guard hand-edit is gone — such an active no longer loads). With the
    # prior schema unloadable, the carry must fail closed and drop it anyway.
    v1 = _plain_schema_inspection(tmp_path)
    acks_v1 = _acked(v1, tmp_path)
    specialists_dir = tmp_path / "specialists"
    agents_specialists_dir = tmp_path / "agents-specialists"
    commit_specialist_install(
        inspection=v1, config={"api_token": "honest-plain-value"},
        secret_names_provided=frozenset(), acks=acks_v1,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir,
    )
    _delete_stored_schema(specialists_dir)

    # v2 declares api_token as plain-required — the reclassification case.
    v2 = _v2_inspection(tmp_path)
    manifest_path = v2.staged_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    (v2.staged_dir / "config-schema.json").write_text(
        json.dumps({"required": ["api_token"], "secret_names": []}), encoding="utf-8")
    files = {
        "role/role.yaml": (v2.staged_dir / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (v2.staged_dir / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (v2.staged_dir / "config-schema.json").read_bytes(),
    }
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    from specialist_install import InspectionResult, compute_install_root_digest
    component = load_specialist_component(v2.staged_dir, manifest_path)
    deps = resolve_dependency_closure(component, v2.staged_dir)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=manifest_path.read_bytes())
    v2 = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=("api_token",), required_secret_names=(),
        dependencies=deps, staged_dir=v2.staged_dir,
    )
    acks = _acked(v2, tmp_path)

    instance = upgrade_specialist(
        slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(),
        acks=acks, specialists_dir=specialists_dir,
        agents_specialists_dir=agents_specialists_dir,
    )
    assert instance.state == "pending-configuration"
    staged = load_instance_tuple(specialists_dir / "mtg" / "desired.yaml")
    assert "api_token" not in staged.config_snapshot


def test_boot_sweep_with_unloadable_schema_scrubs_every_key(tmp_path: Path) -> None:
    """Sol r3 (#337): the boot sweep must not silently skip a snapshot whose
    component schema is unloadable — it scrubs EVERY snapshot key instead, so
    the config-git snapshot that follows can never commit unclassifiable
    plaintext."""
    from specialist_install import sanitize_specialist_snapshots

    v1 = _secret_schema_inspection(tmp_path)
    acks = _acked(v1, tmp_path)
    specialists_dir = tmp_path / "specialists"
    commit_specialist_install(
        inspection=v1, config={}, secret_names_provided=frozenset({"api_token"}),
        acks=acks, specialists_dir=specialists_dir,
        agents_specialists_dir=tmp_path / "agents-specialists",
    )
    slug_dir = specialists_dir / "mtg"
    raw = yaml.safe_load((slug_dir / "active.yaml").read_text(encoding="utf-8"))
    raw["config_snapshot"] = {"api_token": "hunter2", "timezone": "UTC"}
    (slug_dir / "active.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    _delete_stored_schema(specialists_dir)

    cleaned = sanitize_specialist_snapshots(specialists_dir=specialists_dir)

    assert cleaned == 1
    payload = yaml.safe_load((slug_dir / "active.yaml").read_text(encoding="utf-8"))
    assert payload["config_snapshot"] == {}


def test_tampered_but_parseable_schema_fails_closed(tmp_path: Path) -> None:
    """Sol r4 (#337): a damaged schema that still parses (e.g. truncated to
    `{}`) must not be trusted as "declares no secrets" — the component
    checksum no longer matches, so the strip treats every key as potentially
    secret. A GENUINE no-secret schema (checksum intact) keeps its meaning."""
    from specialist_install import sanitize_specialist_snapshots

    v1 = _secret_schema_inspection(tmp_path)
    acks = _acked(v1, tmp_path)
    specialists_dir = tmp_path / "specialists"
    commit_specialist_install(
        inspection=v1, config={}, secret_names_provided=frozenset({"api_token"}),
        acks=acks, specialists_dir=specialists_dir,
        agents_specialists_dir=tmp_path / "agents-specialists",
    )
    slug_dir = specialists_dir / "mtg"
    raw = yaml.safe_load((slug_dir / "active.yaml").read_text(encoding="utf-8"))
    raw["config_snapshot"] = {"api_token": "hunter2", "timezone": "UTC"}
    (slug_dir / "active.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    for schema_file in (specialists_dir / "store").rglob("config-schema.json"):
        schema_file.chmod(0o600)
        schema_file.write_text("{}", encoding="utf-8")  # parseable, checksum-broken

    assert sanitize_specialist_snapshots(specialists_dir=specialists_dir) == 1
    payload = yaml.safe_load((slug_dir / "active.yaml").read_text(encoding="utf-8"))
    assert payload["config_snapshot"] == {}


def test_boot_sweep_scrubs_a_snapshot_with_an_unusable_root(tmp_path: Path) -> None:
    """Sol r4 (#337): a non-empty snapshot whose `root` is missing or
    malformed cannot be classified — it must be scrubbed entirely, never
    silently skipped into the config-git snapshot that follows."""
    from specialist_install import sanitize_specialist_snapshots

    slug_dir = tmp_path / "specialists" / "mtg"
    slug_dir.mkdir(parents=True)
    (slug_dir / "active.prior.yaml").write_text(yaml.safe_dump({
        "api_version": "casa.instance-tuple/v1",
        "config_snapshot": {"api_token": "hunter2"},
        "config_digest": "sha256:beef",
    }, sort_keys=False), encoding="utf-8")

    assert sanitize_specialist_snapshots(specialists_dir=tmp_path / "specialists") == 1
    payload = yaml.safe_load((slug_dir / "active.prior.yaml").read_text(encoding="utf-8"))
    assert payload["config_snapshot"] == {}


def test_consistently_rewritten_schema_and_manifest_fails_closed(tmp_path: Path) -> None:
    """Sol r5 (#337): rewriting config-schema.json AND updating manifest.json's
    internal checksum keeps load_specialist_component happy — only recomputing
    the FULL root digest and comparing it to the digest embedded in the
    component root detects the tamper. The strip must fail closed."""
    from specialist_install import sanitize_specialist_snapshots

    v1 = _secret_schema_inspection(tmp_path)
    acks = _acked(v1, tmp_path)
    specialists_dir = tmp_path / "specialists"
    commit_specialist_install(
        inspection=v1, config={}, secret_names_provided=frozenset({"api_token"}),
        acks=acks, specialists_dir=specialists_dir,
        agents_specialists_dir=tmp_path / "agents-specialists",
    )
    slug_dir = specialists_dir / "mtg"
    raw = yaml.safe_load((slug_dir / "active.yaml").read_text(encoding="utf-8"))
    raw["config_snapshot"] = {"api_token": "hunter2", "timezone": "UTC"}
    (slug_dir / "active.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    for schema_file in (specialists_dir / "store").rglob("config-schema.json"):
        cas_dir = schema_file.parent
        schema_file.chmod(0o600)
        schema_file.write_text("{}", encoding="utf-8")
        manifest_path = cas_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        files = {
            "role/role.yaml": (cas_dir / "role" / "role.yaml").read_bytes(),
            "role/doctrine.md": (cas_dir / "role" / "doctrine.md").read_bytes(),
            "config-schema.json": schema_file.read_bytes(),
        }
        manifest["checksum"] = compute_component_checksum(files)
        manifest_path.chmod(0o600)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert sanitize_specialist_snapshots(specialists_dir=specialists_dir) == 1
    payload = yaml.safe_load((slug_dir / "active.yaml").read_text(encoding="utf-8"))
    assert payload["config_snapshot"] == {}


def test_boot_sweep_handles_mixed_type_snapshot_keys(tmp_path: Path) -> None:
    """Sol r5 (#337): mixed-type mapping keys must not TypeError out of the
    fail-closed scrub into the broad skip handler."""
    from specialist_install import sanitize_specialist_snapshots

    slug_dir = tmp_path / "specialists" / "mtg"
    slug_dir.mkdir(parents=True)
    (slug_dir / "active.prior.yaml").write_text(yaml.safe_dump({
        "api_version": "casa.instance-tuple/v1",
        "config_snapshot": {"api_token": "hunter2", 1: "x"},
        "config_digest": "sha256:beef",
    }, sort_keys=False), encoding="utf-8")

    assert sanitize_specialist_snapshots(specialists_dir=tmp_path / "specialists") == 1
    payload = yaml.safe_load((slug_dir / "active.prior.yaml").read_text(encoding="utf-8"))
    assert payload["config_snapshot"] == {}


# --- #372 (D3): the boot scrub detects and tombstones pre-guard digests -----


def _minimal_tuple_payload(snapshot: dict, config_digest: str) -> dict:
    return {
        "api_version": "casa.instance-tuple/v1",
        "root": "casa/mtg@0.1.0#sha256:" + "c" * 64,
        "binding": {"effective_config_digest": config_digest},
        "config_snapshot": snapshot, "config_digest": config_digest,
    }


def test_boot_sweep_tombstones_an_already_sanitized_pre_guard_digest(tmp_path: Path) -> None:
    """#372 (D3b): the v0.137 sanitization stripped the plaintext but KEPT the
    digest — nothing is left to strip, so key-presence cannot be the detector.
    The digest equation is: a mismatch sentinels both digest fields before the
    boot config-git snapshot can commit the oracle."""
    from personality_binding import PRE_GUARD_SENTINEL, compute_effective_config_digest
    from specialist_install import sanitize_specialist_snapshots

    # A real install (schema loadable, no secret keys present — nothing to
    # strip), then break ONLY the digest: the v0.137-sanitized shape.
    specialists_dir, _agents_dir, _v1 = _installed_mtg(tmp_path)
    active_path = specialists_dir / "mtg" / "active.yaml"
    raw = yaml.safe_load(active_path.read_text(encoding="utf-8"))
    stale = compute_effective_config_digest(
        {**(raw.get("config_snapshot") or {}), "api_token": "hunter2-legacy-plaintext"})
    raw["config_digest"] = stale
    raw["binding"]["effective_config_digest"] = stale
    active_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    snapshot_before = dict(raw.get("config_snapshot") or {})

    assert sanitize_specialist_snapshots(specialists_dir=specialists_dir) == 1
    payload = yaml.safe_load(active_path.read_text(encoding="utf-8"))
    assert payload["config_digest"] == PRE_GUARD_SENTINEL
    assert payload["binding"]["effective_config_digest"] == PRE_GUARD_SENTINEL
    assert stale not in active_path.read_text(encoding="utf-8")
    assert payload["config_snapshot"] == snapshot_before  # snapshot untouched


def test_boot_sweep_deletes_mismatched_residue_files(tmp_path: Path) -> None:
    """#372 (D3b): desired.error.yaml and active.yaml.rollback-tmp are
    diagnostic/crash residue no loader reads — a pre-guard digest there is
    removed by deleting the file outright."""
    from personality_binding import compute_effective_config_digest
    from specialist_install import sanitize_specialist_snapshots

    slug_dir = tmp_path / "specialists" / "mtg"
    slug_dir.mkdir(parents=True)
    stale = compute_effective_config_digest({"api_token": "hunter2-legacy-plaintext"})
    for name in ("desired.error.yaml", "active.yaml.rollback-tmp"):
        (slug_dir / name).write_text(yaml.safe_dump(
            _minimal_tuple_payload({}, stale), sort_keys=False), encoding="utf-8")

    assert sanitize_specialist_snapshots(specialists_dir=tmp_path / "specialists") == 2
    assert not (slug_dir / "desired.error.yaml").exists()
    assert not (slug_dir / "active.yaml.rollback-tmp").exists()


def test_boot_sweep_tombstones_an_unparseable_tuple_and_deletes_unparseable_residue(
    tmp_path: Path,
) -> None:
    """#372 (D3a, Sol design r3): an unparseable payload has no classifiable
    snapshot — fail closed. Tuple files become a minimal sentinel tombstone
    (typed loader error, error-state slug); residue files are deleted."""
    from personality_binding import PRE_GUARD_SENTINEL
    from specialist_install import sanitize_specialist_snapshots

    slug_dir = tmp_path / "specialists" / "mtg"
    slug_dir.mkdir(parents=True)
    (slug_dir / "active.yaml").write_text("{{{ not yaml", encoding="utf-8")
    (slug_dir / "desired.error.yaml").write_text("{{{ not yaml", encoding="utf-8")

    assert sanitize_specialist_snapshots(specialists_dir=tmp_path / "specialists") == 2
    payload = yaml.safe_load((slug_dir / "active.yaml").read_text(encoding="utf-8"))
    assert payload["config_digest"] == PRE_GUARD_SENTINEL
    assert not (slug_dir / "desired.error.yaml").exists()


def test_boot_sweep_tombstoning_desired_releases_the_pending_marker(tmp_path: Path) -> None:
    """#372 (D3c, Terra design r3): a tombstoned desired.yaml is not a live
    pending candidate — its configure re-commit can never succeed — so the
    scrub removes the pending-receipt marker; the receipt and staging trees
    then fall to the ordinary age sweep instead of being pinned forever."""
    from personality_binding import compute_effective_config_digest
    from specialist_install import sanitize_specialist_snapshots

    slug_dir = tmp_path / "specialists" / "mtg"
    slug_dir.mkdir(parents=True)
    stale = compute_effective_config_digest({"api_token": "hunter2-legacy-plaintext"})
    (slug_dir / "desired.yaml").write_text(yaml.safe_dump(
        _minimal_tuple_payload({}, stale), sort_keys=False), encoding="utf-8")
    (slug_dir / "pending-receipt.json").write_text(
        json.dumps({"receipt_id": "r-1"}), encoding="utf-8")

    assert sanitize_specialist_snapshots(specialists_dir=tmp_path / "specialists") == 1
    assert not (slug_dir / "pending-receipt.json").exists()


def test_boot_sweep_leaves_healthy_files_byte_identical(tmp_path: Path) -> None:
    """#372 (D3): a post-guard install — digest derived from its snapshot,
    schema loadable — is never rewritten by the detector."""
    from specialist_install import sanitize_specialist_snapshots

    specialists_dir, _agents_dir, _v1 = _installed_mtg(tmp_path)
    active_path = specialists_dir / "mtg" / "active.yaml"
    before = active_path.read_bytes()

    assert sanitize_specialist_snapshots(specialists_dir=specialists_dir) == 0
    assert active_path.read_bytes() == before


# ---------------------------------------------------------------------------
# casa.callbacks is PERMITTED on a sourced/bundled plugin
# dependency (unlike casa.triggers) — regression pin for the lifted
# prohibition — but a bundled dep's OWNED registry entry routes under the
# SCOPED name (`slug.identifier`, plugin_callbacks.py's own "up to 73
# chars" comment), longer than the bare identifier `validate_manifest`
# checks internally. `_validate_sourced_plugin_tree` therefore carries its
# own inspect-time gate — `CALLBACK_NAME_TOO_LONG` — that refuses BEFORE
# the (scope-blind) generic `callbacks_invalid` path could ever see it.
# ---------------------------------------------------------------------------

try:
    from tests.specialist_fixtures import write_bundled_plugin, write_minimal_component
except ImportError:
    from specialist_fixtures import write_bundled_plugin, write_minimal_component


def _reset_plugin_registry_snapshot_for(tmp_path: Path) -> None:
    """Mirrors tests/test_specialist_bundled_inspect.py's own reset —
    `_manifest_name_collisions` (invoked unconditionally for any component
    with a sourced dep) reads `plugin_registry`'s process-global cached
    snapshot, which would otherwise carry over whatever an earlier test (in
    this same pytest process) last pointed it at."""
    import plugin_registry
    plugin_registry.reload_snapshot(registry_path=tmp_path / "registry.json",
                                    store_root=tmp_path / "store")


def _bundled_dep_row(identifier: str, digest: str, path: str) -> dict:
    return {
        "kind": "plugin/implementation", "identifier": identifier, "digest": digest,
        "source": {"type": "bundled", "path": path},
    }


def _add_bundled_dependency_row(manifest_path: Path, row: dict) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dependencies"].append(row)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _patch_bundled_callbacks(component_dir: Path, name: str, callbacks: list) -> str:
    """Write `casa.callbacks` onto an already-`write_bundled_plugin`-built
    tree and return the recomputed digest a dependency row must pin —
    `write_bundled_plugin` (tests/specialist_fixtures.py) has no callbacks
    parameter of its own (out of this task's file scope)."""
    import plugin_store
    plugin_dir = component_dir / "plugins" / name
    manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.setdefault("casa", {})["callbacks"] = callbacks
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return "sha256:" + plugin_store.content_checksum(plugin_dir)


def _inspect_bundled(tmp_path: Path, component_dir: Path,
                      monkeypatch: pytest.MonkeyPatch) -> "specialist_install.InspectionResult":
    monkeypatch.setattr(specialist_install, "resolve_and_fetch",
                        _stub_resolve_and_fetch(component_dir))
    index = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    index.load()
    return specialist_install.inspect_specialist_repo(
        "org/repo", "main",
        staging_root=tmp_path / "staging",
        installed_index=index,
        receipts_dir=tmp_path / "receipts",
    )


def test_bundled_callbacks_not_prohibited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression pin for the lifted prohibition: casa.callbacks is a PEER
    of casa.triggers on a bundled dependency, not subject to its
    bundled_triggers_unsupported treatment — a valid declaration must
    resolve, and no bundled_*_unsupported kind is ever raised."""
    _reset_plugin_registry_snapshot_for(tmp_path)
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    write_bundled_plugin(component_dir, "mtg")
    digest = _patch_bundled_callbacks(component_dir, "mtg", [{"name": "oauth-return"}])
    _add_bundled_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    result = _inspect_bundled(tmp_path, component_dir, monkeypatch)

    assert result.slug == "mtg-test"
    row = result.plugin_resolutions[0]
    assert row.scoped_name == "mtg-test.mtg"
    assert row.identifier == "mtg"


def test_bundled_callback_scoped_name_too_long_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A callback whose effective name fits comfortably under the bare
    identifier can still overflow once routed under the SCOPED registry
    name (slug.identifier) — the inspect-time gate must catch this BEFORE
    the artifact can ever reach the registry, coded distinctly from the
    generic (scope-blind) callbacks_invalid."""
    _reset_plugin_registry_snapshot_for(tmp_path)
    slug = "s" * 30  # within specialist_component's 32-char slug cap
    identifier = "mtg"
    declared = "x" * 100
    # Sanity for the fixture itself: passes under the BARE identifier.
    assert len(f"plg-{identifier}--{declared}") <= 128
    # But overflows once scoped (slug.identifier instead of identifier).
    assert len(f"plg-{slug}.{identifier}--{declared}") > 128

    component_dir, manifest_path = write_minimal_component(tmp_path, slug=slug)
    write_bundled_plugin(component_dir, identifier)
    digest = _patch_bundled_callbacks(component_dir, identifier, [{"name": declared}])
    _add_bundled_dependency_row(
        manifest_path, _bundled_dep_row(identifier, digest, f"plugins/{identifier}"))

    with pytest.raises(SpecialistInstallError) as exc:
        _inspect_bundled(tmp_path, component_dir, monkeypatch)
    assert exc.value.kind == "callback_name_too_long"


def test_bundled_callback_short_name_passes_both_scoped_and_unscoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity companion to the overflow test above: a short declared name
    stays well under 128 chars against BOTH the bare identifier and the
    scoped name — the new gate must not false-positive on ordinary bundles."""
    _reset_plugin_registry_snapshot_for(tmp_path)
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    write_bundled_plugin(component_dir, "mtg")
    digest = _patch_bundled_callbacks(component_dir, "mtg", [{"name": "short"}])
    _add_bundled_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    result = _inspect_bundled(tmp_path, component_dir, monkeypatch)
    assert result.slug == "mtg-test"


# ---------------------------------------------------------------------------
# casa.emits / casa.subscribes are PERMITTED on a sourced/bundled plugin
# dependency (unlike casa.triggers) — same carve-out as casa.callbacks. A
# bundled dep's OWNED registry entry routes under the SCOPED name
# (`slug.identifier`), so `_validate_sourced_plugin_tree` carries its own
# inspect-time gate — `EVENT_NAME_TOO_LONG` — mirroring
# `CALLBACK_NAME_TOO_LONG` exactly. `casa.triggers` on a sourced dep must
# STILL be refused — regression pin below.
# ---------------------------------------------------------------------------


def _patch_bundled_events(component_dir: Path, name: str, *,
                          emits: list | None = None,
                          subscribes: list | None = None) -> str:
    """Write `casa.emits`/`casa.subscribes` onto an already-
    `write_bundled_plugin`-built tree and return the recomputed digest a
    dependency row must pin — mirrors `_patch_bundled_callbacks`."""
    import plugin_store
    plugin_dir = component_dir / "plugins" / name
    manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    casa = manifest.setdefault("casa", {})
    if emits is not None:
        casa["emits"] = emits
    if subscribes is not None:
        casa["subscribes"] = subscribes
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return "sha256:" + plugin_store.content_checksum(plugin_dir)


def test_bundled_emits_and_subscribes_not_prohibited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression pin for the carve-out: casa.emits/casa.subscribes are
    PEERS of casa.callbacks on a bundled dependency, not subject to the
    triggers-style bundled_*_unsupported treatment — a valid declaration of
    BOTH blocks must resolve, and no bundled_*_unsupported kind is raised."""
    _reset_plugin_registry_snapshot_for(tmp_path)
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    write_bundled_plugin(component_dir, "mtg")
    digest = _patch_bundled_events(
        component_dir, "mtg",
        emits=[{"name": "invoice-ready"}],
        subscribes=[{"plugin": "other", "event": "ping"}])
    _add_bundled_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    result = _inspect_bundled(tmp_path, component_dir, monkeypatch)

    assert result.slug == "mtg-test"
    row = result.plugin_resolutions[0]
    assert row.scoped_name == "mtg-test.mtg"
    assert row.identifier == "mtg"


def test_bundled_event_scoped_name_too_long_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An emitted event whose effective name fits comfortably under the bare
    identifier can still overflow once routed under the SCOPED registry name
    (slug.identifier) — the inspect-time gate must catch this BEFORE the
    artifact can ever reach the registry, coded distinctly from the generic
    (scope-blind) emits_invalid."""
    import plugin_events

    _reset_plugin_registry_snapshot_for(tmp_path)
    slug = "s" * 30  # within specialist_component's 32-char slug cap
    identifier = "mtg"
    declared = "x" * 100
    # Sanity for the fixture itself: passes under the BARE identifier.
    assert len(f"plg-{identifier}--{declared}") <= plugin_events.MAX_EFFECTIVE_LEN
    # But overflows once scoped (slug.identifier instead of identifier).
    assert len(f"plg-{slug}.{identifier}--{declared}") > plugin_events.MAX_EFFECTIVE_LEN

    component_dir, manifest_path = write_minimal_component(tmp_path, slug=slug)
    write_bundled_plugin(component_dir, identifier)
    digest = _patch_bundled_events(component_dir, identifier, emits=[{"name": declared}])
    _add_bundled_dependency_row(
        manifest_path, _bundled_dep_row(identifier, digest, f"plugins/{identifier}"))

    with pytest.raises(SpecialistInstallError) as exc:
        _inspect_bundled(tmp_path, component_dir, monkeypatch)
    assert exc.value.kind == "event_name_too_long"


def test_bundled_event_short_name_passes_both_scoped_and_unscoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity companion to the overflow test above: a short declared name
    stays well under 128 chars against BOTH the bare identifier and the
    scoped name — the new gate must not false-positive on ordinary bundles."""
    _reset_plugin_registry_snapshot_for(tmp_path)
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    write_bundled_plugin(component_dir, "mtg")
    digest = _patch_bundled_events(component_dir, "mtg", emits=[{"name": "short"}])
    _add_bundled_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    result = _inspect_bundled(tmp_path, component_dir, monkeypatch)
    assert result.slug == "mtg-test"


def test_bundled_triggers_still_refused_alongside_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression pin: lifting the casa.triggers prohibition for
    emits/subscribes must NOT loosen it for casa.triggers itself — a sourced
    dependency declaring casa.triggers is still refused with
    bundled_triggers_unsupported, even though emits/subscribes are now
    permitted peers of callbacks."""
    _reset_plugin_registry_snapshot_for(tmp_path)
    component_dir, manifest_path = write_minimal_component(tmp_path, slug="mtg-test")
    write_bundled_plugin(component_dir, "mtg", triggers=[{"name": "on-thing"}])
    plugin_dir = component_dir / "plugins" / "mtg"
    import plugin_store
    digest = "sha256:" + plugin_store.content_checksum(plugin_dir)
    _add_bundled_dependency_row(manifest_path, _bundled_dep_row("mtg", digest, "plugins/mtg"))

    with pytest.raises(SpecialistInstallError) as exc:
        _inspect_bundled(tmp_path, component_dir, monkeypatch)
    assert exc.value.kind == "bundled_triggers_unsupported"


# ---------------------------------------------------------------------------
# #431: the .mcp.json ${VAR} carve-out must cover BOTH documented expansion
# forms. It used to match only the bare form, so a `${VAR:-default}` left
# `${` in the leaf and tripped the forbidden-marker gate — a BUNDLED plugin
# was refused at install for syntax a standalone plugin may use freely.
# ---------------------------------------------------------------------------

def _leaf_rejected(leaf: str) -> bool:
    """True iff *leaf* trips the real gate. Calls the PRODUCTION function
    rather than re-deriving its predicate: the first version of this helper
    reimplemented the carve-out, and silently stopped mirroring the gate the
    moment the gate changed shape."""
    import specialist_install as si
    try:
        si._walk_reject_markers_in_json(leaf)
    except ValueError:
        return True
    return False


@pytest.mark.parametrize("leaf", [
    "${CLAUDE_PLUGIN_ROOT}/server/main.py",   # the bare form, as before
    "${MY_SECRET}",
    "${MY_OPTIONAL_TOKEN:-}",                 # empty default
    "${BANKFEED_EB_ENVIRONMENT:-sandbox}",    # real default
    "${API_BASE:-https://api.example.com/v1}",
])
def test_documented_expansion_forms_survive_the_marker_gate(leaf):
    assert not _leaf_rejected(leaf)


@pytest.mark.parametrize("leaf", [
    "${VAR:-<script>}",        # the carve-out must not swallow an HTML open
    "${VAR:-{{evil}}}",        # ...nor a Jinja marker
    "${VAR:-{%raw%}}",
    "a bare ${ on its own",
    "{{template}}",
    "!include secrets.yaml",
])
def test_the_carve_out_cannot_smuggle_a_marker_past_the_gate(leaf):
    """The carve-out DELETES what it matches before the scan runs, so the
    default body is a conservative charset — anything it admits is something
    the marker scan can no longer see."""
    assert _leaf_rejected(leaf)


@pytest.mark.parametrize("leaf", [
    "<${CASA_NEVER_SET:-script}>",          # realizes to <script>
    "!${CASA_NEVER_SET:-include} secrets.yaml",
    "<${CASA_NEVER_SET:-platform_frame}>",
])
def test_a_marker_split_across_the_expansion_is_still_caught(leaf):
    """r1 (Sol): DELETING the expansion let a marker be assembled from the
    text AROUND it — neither half is a marker on its own, so no charset
    restriction inside the default could catch it. The gate scans the
    REALIZED string, which is closed under this by construction."""
    assert _leaf_rejected(leaf)


@pytest.mark.parametrize("leaf", [
    "${API_URL:-https://api.example.com/v1?mode=read&x=1}",
    "${HOME_DIR:-~/.cache/thing}",
    "${GREETING:-hello there}",
])
def test_realistic_defaults_are_not_collateral(leaf):
    """The realized-form scan is also strictly MORE permissive than the
    charset attempt it replaced, which rejected ordinary URL defaults."""
    assert not _leaf_rejected(leaf)


def test_env_name_collision_sees_defaulted_refs_on_both_sides(monkeypatch,
                                                              tmp_path):
    """#431: a collision is about which names are CLAIMED, not which must
    resolve. Reading either side with the requirement set would let a
    `${VAR:-}` hide the clash — the incoming tree's side was fixed first and
    the INSTALLED side was still bare-form."""
    import json as _json
    from types import SimpleNamespace

    import plugin_registry
    import specialist_install as si

    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / ".mcp.json").write_text(_json.dumps({"mcpServers": {"s": {
        "command": "node",
        # The installed plugin claims the name via the DEFAULTED form.
        "env": {"K": "${SHARED_NAME:-}"}}}}), encoding="utf-8")

    monkeypatch.setattr(plugin_registry, "owned_entries_for",
                        lambda *_a, **_k: [])
    monkeypatch.setattr(plugin_registry, "snapshot_registry", lambda: None)
    monkeypatch.setattr(
        plugin_registry, "resolve_all",
        lambda: SimpleNamespace(plugins=[
            SimpleNamespace(name="other", path=str(installed))]))

    # The incoming tree requires the same name, bare.
    assert si._env_name_conflicts({"SHARED_NAME"}, exclude_owner="slug") == [
        "SHARED_NAME"]
    # Sanity: an unrelated name is not a conflict.
    assert si._env_name_conflicts({"OTHER_NAME"}, exclude_owner="slug") == []
