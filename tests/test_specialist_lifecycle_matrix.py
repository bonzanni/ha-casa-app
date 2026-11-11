"""One gate proving every lifecycle invariant this plan's install pipeline
claims (spec §4.1, §2.4). Each test is a single, named invariant — a
failure here names EXACTLY which promise broke, not a generic install
smoke test.

Deliberately overlaps individual N1a-N1d test files (test_specialist_install.py
etc.) by design — this module is the ONE consolidated gate naming every
invariant together, not a replacement for the per-task regression coverage."""
import json
from pathlib import Path

import pytest

import specialist_install
from specialist_component import compute_component_checksum, load_specialist_component
from specialist_install import (
    SpecialistInstallError,
    cas_pin_roots,
    commit_specialist_install,
    inspect_specialist_repo,
    parse_component_root,
    resolve_dependency_closure,
    upgrade_specialist,
    uninstall_specialist,
)
# SpecialistInstallAckStore lives in
# specialist_install_consent, not specialist_install (controller resolution 1).
from specialist_install_consent import SpecialistInstallAckStore
from approval_values import NO_SETTINGS, record_specialist_ack, settings
from specialist_registry import InstalledSpecialistIndex
from test_specialist_install import _write_component, _stub_resolve_and_fetch  # shared fixture helpers


def _approved_inspection(
    tmp_path: Path, *, slug: str = "mtg", version_suffix: str = "",
    version_override: str | None = None,
) -> tuple:
    from specialist_install import InspectionResult, compute_install_root_digest

    root = _write_component(tmp_path / f"component-{slug}{version_suffix}", slug=slug)
    manifest_path = root / "manifest.json"
    if version_override is not None:
        # Bumping only the manifest's `version` field changes manifest_bytes
        # (and hence compute_install_root_digest's manifest_checksum/root
        # digest) WITHOUT touching manifest["checksum"] (which only covers
        # role.yaml/doctrine.md/config-schema.json bytes, per
        # compute_component_checksum) — load_specialist_component's own
        # checksum validation still passes. Used by the upgrade-failure test
        # below to give v1/v2 genuinely different root digests; two
        # `_write_component` calls with identical content would otherwise
        # collide on the SAME content-addressed CAS directory, and the
        # upgrade path would silently short-circuit past the corrupted bytes.
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = version_override
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    component = load_specialist_component(root, manifest_path)
    deps = resolve_dependency_closure(component, root)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=manifest_path.read_bytes())
    inspection = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=deps, staged_dir=root,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / f"acks-{slug}{version_suffix}.json")
    record_specialist_ack(acks, inspection)
    return inspection, acks


# --- installed -> pending-configuration -> configured -> active -------------


def test_fresh_install_with_no_required_config_goes_straight_to_active(tmp_path: Path) -> None:
    inspection, acks = _approved_inspection(tmp_path)
    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        approved_values=NO_SETTINGS,
        specialists_dir=tmp_path / "specialists", agents_specialists_dir=tmp_path / "agents-specialists")
    assert instance.state == "active"


def test_fresh_install_missing_config_is_pending_configuration_with_no_active_tuple(
    tmp_path: Path,
) -> None:
    """Pins INV-SPEC-002. Red case demonstrated: treating unsatisfied config as satisfied fails this test."""
    root = tmp_path / "component-mtg-cfg"
    _write_component(root, slug="mtg")
    (root / "config-schema.json").write_text(
        json.dumps({"required": ["api_key"], "secret_names": ["api_key"]}), encoding="utf-8")
    files = {
        "role/role.yaml": (root / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    component = load_specialist_component(root, manifest_path)
    from specialist_install import InspectionResult, compute_install_root_digest
    cfg_deps = resolve_dependency_closure(component, root)
    cfg_root_digest = compute_install_root_digest(
        component, cfg_deps, manifest_bytes=manifest_path.read_bytes())
    inspection = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=cfg_root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=("api_key",),
        dependencies=cfg_deps, staged_dir=root,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    record_specialist_ack(acks, inspection)

    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        approved_values=NO_SETTINGS,
        specialists_dir=tmp_path / "specialists", agents_specialists_dir=tmp_path / "agents-specialists")
    assert instance.state == "pending-configuration"
    assert instance.active is None
    with_secret = record_specialist_ack(acks, inspection, settings(secrets=["api_key"]))
    then_satisfied = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset({"api_key"}), acks=acks,
        approved_values=with_secret,
        specialists_dir=tmp_path / "specialists", agents_specialists_dir=tmp_path / "agents-specialists")
    assert then_satisfied.state == "active"  # configured -> active on re-commit with config supplied


# --- consent gate -------------------------------------------------------


def test_install_never_writes_cas_without_a_recorded_consent(tmp_path: Path) -> None:
    """Pins INV-SPEC-001. Red case demonstrated: skipping the consent check before staging fails this test."""
    inspection, _acks = _approved_inspection(tmp_path)
    unacked = SpecialistInstallAckStore(path=tmp_path / "no-acks.json")
    with pytest.raises(SpecialistInstallError) as raised:
        commit_specialist_install(
            inspection=inspection, config={}, secret_names_provided=frozenset(), acks=unacked,
            approved_values=NO_SETTINGS,
            specialists_dir=tmp_path / "specialists", agents_specialists_dir=tmp_path / "agents-specialists")
    assert raised.value.kind == "consent_missing"
    assert not (tmp_path / "specialists" / "store").exists()  # NOTHING persisted


# --- slug collision -------------------------------------------------------


def test_install_rejects_a_slug_colliding_with_a_fixed_resident_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "butler" is one of the image's fixed resident role slots
    # (defaults/roles/resident/butler/role.yaml) — the post-cutover image
    # ships zero specialist role artifacts (controller resolution 6), so
    # this collision can only be against a resident/executor slot, never a
    # bundled specialist.
    root = _write_component(tmp_path / "component-butler", slug="butler")
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(root))
    index = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    index.load()
    with pytest.raises(SpecialistInstallError) as raised:
        inspect_specialist_repo(
            "casa-test/butler-clash", "main", staging_root=tmp_path / "staging",
            installed_index=index,
        )
    assert raised.value.kind == "slug_collision"


# --- pinned-digest-unavailable -------------------------------------------


def test_install_rejects_when_a_dependency_digest_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _write_component(tmp_path / "component-baddep", slug="mtg", dependencies=[
        {"kind": "corpus/data", "identifier": "mtg-rules-corpus", "digest": "sha256:" + "9" * 64},
    ])
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub_resolve_and_fetch(root))
    index = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "specialists"))
    index.load()
    with pytest.raises(SpecialistInstallError) as raised:
        inspect_specialist_repo(
            "casa-test/mtg-baddep", "main", staging_root=tmp_path / "staging",
            installed_index=index,
        )
    assert raised.value.kind == "dependency_unavailable"


# --- upgrade / rollback / uninstall ---------------------------------------


def test_upgrade_failure_leaves_the_complete_active_tuple_running(tmp_path: Path) -> None:
    v1, acks1 = _approved_inspection(tmp_path, slug="mtg", version_suffix="-v1")
    specialists_dir, agents_specialists_dir = tmp_path / "specialists", tmp_path / "agents-specialists"
    commit_specialist_install(inspection=v1, config={}, secret_names_provided=frozenset(), acks=acks1,
                               approved_values=NO_SETTINGS,
                               specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir)

    # v2 needs a DIFFERENT root_digest than v1 (see _approved_inspection's
    # version_override comment) — otherwise it would collide on the SAME
    # content-addressed CAS directory v1 already published, and
    # upgrade_specialist would never re-read the corrupted staged bytes
    # below at all (cas_dir.exists() short-circuits the copy+validate step).
    v2, acks2 = _approved_inspection(tmp_path, slug="mtg", version_suffix="-v2", version_override="0.2.0")
    # Corrupt v2's doctrine.md AFTER inspection built its InspectionResult so
    # commit-time compilation fails — proving the ALREADY-active v1 survives.
    (v2.staged_dir / "role" / "doctrine.md").write_text("", encoding="utf-8")  # empty -> load fails

    # Disclosure (controller resolution 4): the empty doctrine.md is caught by
    # role_artifact.load_role_artifact's "role doctrine is empty" ValueError,
    # raised from load_specialist_component when upgrade_specialist reloads
    # the staged bytes inside its CAS-staging try/except (which cleans up the
    # staging dir and re-raises the SAME exception unchanged) — this surfaces
    # as a bare ValueError, NOT a SpecialistInstallError (that type is only
    # raised by specialist_install.py's own gates, not role_artifact.py's).
    with pytest.raises(ValueError) as raised:
        upgrade_specialist(slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(),
                            acks=acks2, approved_values=NO_SETTINGS, specialists_dir=specialists_dir,
                            agents_specialists_dir=agents_specialists_dir)
    assert not isinstance(raised.value, SpecialistInstallError)
    assert "doctrine" in str(raised.value) and "empty" in str(raised.value)

    from personality_binding import InstanceDir
    still_active = InstanceDir(specialists_dir / "mtg").active()
    assert still_active is not None
    _, _, checksum = parse_component_root(still_active.root)
    assert checksum == v1.root_digest  # v1, unchanged


def test_uninstall_then_reinstall_at_the_same_digest_is_allowed(tmp_path: Path) -> None:
    inspection, acks = _approved_inspection(tmp_path)
    specialists_dir, agents_specialists_dir = tmp_path / "specialists", tmp_path / "agents-specialists"
    commit_specialist_install(inspection=inspection, config={}, secret_names_provided=frozenset(),
                               acks=acks, approved_values=NO_SETTINGS, specialists_dir=specialists_dir,
                               agents_specialists_dir=agents_specialists_dir)
    uninstall_specialist(slug="mtg", specialists_dir=specialists_dir,
                          agents_specialists_dir=agents_specialists_dir)
    pinned_after_uninstall = cas_pin_roots(specialists_dir)
    assert inspection.root_digest not in pinned_after_uninstall  # no tuple references it anymore

    second_acks = SpecialistInstallAckStore(path=tmp_path / "acks-reinstall.json")
    record_specialist_ack(second_acks, inspection)
    reinstalled = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=second_acks,
        approved_values=NO_SETTINGS,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_specialists_dir)
    assert reinstalled.state == "active"


# --- #346: fresh-install guard must also refuse a conflicting pending -------


def test_second_fresh_install_cannot_replace_a_different_pending_candidate(
    tmp_path: Path,
) -> None:
    """#346: the fresh-install guard rejected an existing active.yaml but not
    an existing desired.yaml — two inspections of the same free slug both
    passed uniqueness, and the second commit silently replaced the first
    pending candidate (and its config/owned-plugin sidecar). A DIFFERENT
    pending candidate must be refused; only the same component's own
    re-commit (the configure step) may replace its desired tuple."""
    # First install: v0.1.0, pending-configuration (required secret missing).
    root = tmp_path / "component-mtg-a"
    _write_component(root, slug="mtg")
    (root / "config-schema.json").write_text(
        json.dumps({"required": ["api_key"], "secret_names": ["api_key"]}), encoding="utf-8")
    files = {
        "role/role.yaml": (root / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["checksum"] = compute_component_checksum(files)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    component = load_specialist_component(root, manifest_path)
    from specialist_install import InspectionResult, compute_install_root_digest
    deps = resolve_dependency_closure(component, root)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=manifest_path.read_bytes())
    inspection_a = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=("api_key",),
        dependencies=deps, staged_dir=root,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "acks-a.json")
    record_specialist_ack(acks, inspection_a)
    pending = commit_specialist_install(
        inspection=inspection_a, config={}, secret_names_provided=frozenset(), acks=acks,
        approved_values=NO_SETTINGS,
        specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents-specialists")
    assert pending.state == "pending-configuration"

    # Second, DIFFERENT component (same slug, bumped version) fully satisfied
    # — before the fix this silently replaced A's pending candidate.
    inspection_b, acks_b = _approved_inspection(
        tmp_path, slug="mtg", version_suffix="-b", version_override="0.2.0")
    with pytest.raises(SpecialistInstallError) as excinfo:
        commit_specialist_install(
            inspection=inspection_b, config={}, secret_names_provided=frozenset(),
            acks=acks_b, approved_values=NO_SETTINGS, specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents-specialists")
    assert excinfo.value.kind == "concurrent_mutation"

    # A's own pending candidate survives untouched...
    from personality_binding import InstanceDir
    desired = InstanceDir(tmp_path / "specialists" / "mtg").desired()
    assert desired is not None and "@0.1.0#" in desired.root  # still A's candidate

    # ...and A's own configure re-commit (same component) still works.
    with_secret = record_specialist_ack(acks, inspection_a, settings(secrets=["api_key"]))
    satisfied = commit_specialist_install(
        inspection=inspection_a, config={}, secret_names_provided=frozenset({"api_key"}),
        acks=acks, approved_values=with_secret, specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents-specialists")
    assert satisfied.state == "active"


def test_pending_retry_merges_already_supplied_config(tmp_path: Path) -> None:
    """#331: a pending-configuration retry that supplies only the STILL-MISSING
    setting keeps the earlier attempt's persisted config. Pre-fix
    satisfiability was recomputed from the retry's config argument alone, so
    {"region": "eu"} → pending → retry with just the secret treated region as
    missing (stayed pending) and overwrote the desired snapshot."""
    from specialist_install import InspectionResult, compute_install_root_digest

    root = _write_component(tmp_path / "component", slug="mtg")
    manifest_path = root / "manifest.json"
    schema = json.loads((root / "config-schema.json").read_text(encoding="utf-8"))
    schema["required"] = ["region", "api_key"]
    schema["secret_names"] = ["api_key"]
    (root / "config-schema.json").write_text(json.dumps(schema), encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["checksum"] = compute_component_checksum({
        "role/role.yaml": (root / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    })
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    component = load_specialist_component(root, manifest_path)
    deps = resolve_dependency_closure(component, root)
    root_digest = compute_install_root_digest(
        component, deps, manifest_bytes=manifest_path.read_bytes())
    inspection = InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=root_digest, mission="x",
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=("region",), required_secret_names=("api_key",),
        dependencies=deps, staged_dir=root,
    )
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    first = record_specialist_ack(acks, inspection, settings({"region": "eu"}))

    pending = commit_specialist_install(
        inspection=inspection, config={"region": "eu"}, secret_names_provided=frozenset(),
        acks=acks, approved_values=first, specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents-specialists")
    assert pending.state == "pending-configuration"
    assert pending.desired.config_snapshot == {"region": "eu"}

    # Retry supplies ONLY the missing secret; region must carry forward.
    # The retry's approval names what it stages: the carried region plus the secret.
    retry = record_specialist_ack(acks, inspection, settings({"region": "eu"}, ["api_key"]))
    satisfied = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset({"api_key"}),
        acks=acks, approved_values=retry, specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents-specialists")
    assert satisfied.state == "active"
    assert satisfied.active.config_snapshot == {"region": "eu"}


def test_fresh_install_refuses_an_unreadable_pending_candidate(tmp_path: Path) -> None:
    """#346: a desired.yaml the guard cannot LOAD (bad YAML) is an occupant it
    cannot prove is ours — refuse structurally (concurrent_mutation), never
    raise a raw parser error or silently replace it."""
    slug_dir = tmp_path / "specialists" / "mtg"
    slug_dir.mkdir(parents=True)
    (slug_dir / "desired.yaml").write_text("{{{ not yaml", encoding="utf-8")

    inspection, acks = _approved_inspection(tmp_path)
    with pytest.raises(SpecialistInstallError) as excinfo:
        commit_specialist_install(
            inspection=inspection, config={}, secret_names_provided=frozenset(),
            acks=acks, approved_values=NO_SETTINGS, specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents-specialists")
    assert excinfo.value.kind == "concurrent_mutation"
