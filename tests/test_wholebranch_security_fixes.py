"""Whole-branch adversarial-review security regressions (fix wave 1).

One test per finding, each proving a fail-closed invariant that the pre-fix
code violated (RED-first). Findings:

* F1 — slug / persona-ref / corpus-identifier traversal in lifecycle functions
* F2 — symlink-target containment on uninstall + re-materialize GC
* F3 — persona identity binding (checksum match is not identity proof)
* F4 — corpus identifier containment (schema-unconstrained -> path join)
* F5 — fail-closed self-heal on op-file / tuple divergence
* F6 — rollback verification gate (dependency closure + compile)
* Md — concurrent-install CAS race

Shares the component fixture helpers from test_specialist_install /
test_specialist_lifecycle_matrix so a component here is byte-identical to the
ones the rest of the suite installs."""
import json
import os
import shutil
from pathlib import Path

import pytest

import specialist_install
from specialist_install import (
    SpecialistInstallError,
    commit_specialist_install,
    is_safe_corpus_identifier,
    parse_component_root,
    resolve_dependency_closure,
    rollback_specialist,
    uninstall_specialist,
    upgrade_specialist,
    validate_specialist_slug,
)
from specialist_component import load_specialist_component
from test_specialist_install import _write_component
from test_specialist_lifecycle_matrix import _approved_inspection


# --------------------------------------------------------------------------
# F1 — traversal in lifecycle functions
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../../etc", "/data", "a/b", "..", "", "MTG", "a b"])
def test_validate_specialist_slug_rejects_unsafe_slugs(bad: str) -> None:
    with pytest.raises(SpecialistInstallError) as exc:
        validate_specialist_slug(bad)
    assert exc.value.kind == "invalid_slug"


def test_uninstall_specialist_rejects_traversal_slug_leaving_siblings_intact(
    tmp_path: Path,
) -> None:
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    specialists_dir.mkdir()
    agents_dir.mkdir()
    # A canary directory a naive `specialists_dir / slug` rmtree would delete.
    canary = tmp_path / "canary"
    canary.mkdir()
    (canary / "keep.txt").write_text("precious", encoding="utf-8")

    with pytest.raises(SpecialistInstallError) as exc:
        uninstall_specialist(
            slug="../canary", specialists_dir=specialists_dir,
            agents_specialists_dir=agents_dir)
    assert exc.value.kind == "invalid_slug"
    # RED on pre-fix: `specialists_dir / "../canary"` == the canary dir, which
    # the old shutil.rmtree would have deleted.
    assert (canary / "keep.txt").is_file()


def test_rollback_specialist_rejects_traversal_slug(tmp_path: Path) -> None:
    with pytest.raises(SpecialistInstallError) as exc:
        rollback_specialist(
            slug="../evil", specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents")
    assert exc.value.kind == "invalid_slug"


def test_upgrade_specialist_rejects_traversal_slug(tmp_path: Path) -> None:
    inspection, acks = _approved_inspection(tmp_path, slug="mtg")
    with pytest.raises(SpecialistInstallError) as exc:
        upgrade_specialist(
            slug="../evil", inspection=inspection, config={},
            secret_names_provided=frozenset(), acks=acks,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents")
    assert exc.value.kind == "invalid_slug"


def test_inspect_specialist_repo_rejects_traversal_target_slug(tmp_path: Path) -> None:
    from specialist_install import inspect_specialist_repo

    with pytest.raises(SpecialistInstallError) as exc:
        inspect_specialist_repo(
            "casa-test/x", "main", mode="upgrade", target_slug="../evil",
            staging_root=tmp_path / "staging", specialists_dir=tmp_path / "specialists")
    assert exc.value.kind == "invalid_slug"


def test_commit_persona_install_rejects_traversal_persona_id(tmp_path: Path) -> None:
    from persona_install import (
        PersonaInspectionResult, PersonaInstallAckStore, commit_persona_install,
        persona_install_consent_identity,
    )

    checksum = "sha256:" + "1" * 64
    insp = PersonaInspectionResult(
        persona_id="../evil", version="0.1.0", checksum=checksum,
        display_name="x", staged_dir=tmp_path / "staged")
    acks = PersonaInstallAckStore(path=tmp_path / "acks.json")
    ident = persona_install_consent_identity(
        persona_id="../evil", version="0.1.0", checksum=checksum)
    acks.record(identity=ident, persona_id="../evil", version="0.1.0", checksum=checksum)

    with pytest.raises(SpecialistInstallError) as exc:
        commit_persona_install(
            inspection=insp, acks=acks, personas_root=tmp_path / "personas")
    assert exc.value.kind == "invalid_persona_ref"


def test_validate_persona_path_segments_rejects_unsafe(tmp_path: Path) -> None:
    from persona_install import validate_persona_path_segments

    for pid in ("../evil", "/abs/x", "a/b/c", "casa/x/y", ""):
        with pytest.raises(SpecialistInstallError) as exc:
            validate_persona_path_segments(pid, "0.1.0")
        assert exc.value.kind == "invalid_persona_ref"
    for ver in ("../1", "0.1", "latest", ""):
        with pytest.raises(SpecialistInstallError) as exc:
            validate_persona_path_segments("casa/x", ver)
        assert exc.value.kind == "invalid_persona_ref"
    # A well-formed namespaced ref + semver passes.
    validate_persona_path_segments("casa/alex", "0.1.0")


# --------------------------------------------------------------------------
# F2 — symlink-target containment
# --------------------------------------------------------------------------


def test_uninstall_specialist_symlink_to_absolute_target_is_never_rmtreed(
    tmp_path: Path,
) -> None:
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    specialists_dir.mkdir()
    agents_dir.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("precious", encoding="utf-8")

    # A pre-existing malicious/accidental symlink whose target is an absolute
    # external directory (finance -> /external).
    os.symlink(str(external), agents_dir / "finance")

    uninstall_specialist(
        slug="finance", specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    # RED on pre-fix: `agents_dir / os.readlink(...)` == the absolute external
    # dir, which the old rmtree would have deleted.
    assert (external / "keep.txt").is_file()
    # The symlink itself is removed (uninstall is not a silent no-op).
    assert not (agents_dir / "finance").exists()
    assert not (agents_dir / "finance").is_symlink()


def test_materialize_prior_symlink_to_absolute_target_is_never_gcd(tmp_path: Path) -> None:
    from specialist_materialize import materialize_specialist_operational_files
    from role_slot import RoleSlot, ResolvedModel
    from persona_pack import PersonaPack, PersonaManifest

    role = RoleSlot(
        role_id="specialist:mtg", kind="specialist", slot="mtg",
        mission="Answer questions.",
        resolved_model=ResolvedModel(source="fixed", effective="sonnet",
                                      sdk_model="claude-sonnet-4-6", option=None),
        normalized={
            "model": {"source": "fixed", "value": "sonnet"},
            "tools": {"allowed": [], "disallowed": ["Bash"], "permission_mode": "dontAsk",
                       "max_turns": 8, "skills": "none", "voice_guard": "none"},
            "mcp_servers": [], "memory": {"token_budget": 0, "read_strategy": "per_turn"},
            "session": {"strategy": "ephemeral", "idle_timeout_seconds": 0},
            "tts": {"tag_dialect": "none", "error_phrases": {}},
            "response": {"text": {"register": "precise"}, "voice": {"register": "spoken"},
                          "restricted_webhook": {"register": "plain"}},
            "requires": {"plugins": [], "tools": []},
        },
        doctrine="# Core doctrine\n\nAnswer.\n", checksum="sha256:" + "1" * 64,
    )
    persona = PersonaPack(
        persona_id="casa/judge", version="0.1.0", trait_schema_version=1,
        identity={"display_name": "Judge", "pronouns": {
            "subject": "they", "object": "them", "possessive_adjective": "their",
            "possessive_pronoun": "theirs", "reflexive": "themself"}},
        relationship_posture="established", archetype="adjudicator",
        traits={"warmth": 2, "formality": 4, "candor": 5, "attunement": 3,
                 "curiosity": 3, "levity": 1, "social_energy": 2, "optimism": 3},
        quirks=(), markdown="# Core\n\nJudges.\n\n## Negative space\n\nNever guesses.\n",
        examples=(), manifest=PersonaManifest(files=(), checksum="sha256:" + "3" * 64),
        checksum="sha256:" + "2" * 64,
    )

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("precious", encoding="utf-8")
    # A pre-existing hostile slug symlink whose target escapes the directory.
    os.symlink(str(external), agents_dir / "mtg")

    materialize_specialist_operational_files(
        agents_specialists_dir=agents_dir, slug="mtg", role=role, persona=persona)

    # The symlink was retargeted to the new content dir (materialize still
    # works), but the escaping prior target was NOT garbage-collected.
    assert (external / "keep.txt").is_file()
    assert (agents_dir / "mtg" / "runtime.yaml").is_file()  # new content live


# --------------------------------------------------------------------------
# F3 — persona identity binding
# --------------------------------------------------------------------------


def test_persona_dependency_identity_mismatch_is_unavailable(tmp_path: Path) -> None:
    root = _write_component(tmp_path / "component")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    # The bundled pack is casa/judge@0.1.0 with the RIGHT digest, but the
    # dependency line NAMES a different persona — a substitution.
    good_digest = manifest["dependencies"][0]["digest"]
    manifest["dependencies"] = [
        {"kind": "persona", "identifier": "casa/impostor@0.1.0", "digest": good_digest}]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    component = load_specialist_component(root, manifest_path)
    rows = [r for r in resolve_dependency_closure(component, root) if r.kind == "persona"]
    assert len(rows) == 1
    # RED on pre-fix: the checksum matched, so it was marked available=True.
    assert rows[0].available is False
    assert "not the declared dependency" in rows[0].detail


def test_persona_dependency_disagreeing_with_default_persona_is_unavailable(
    tmp_path: Path,
) -> None:
    root = _write_component(tmp_path / "component")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    # Dependency identifier matches the bundled pack, but the component's
    # declared default_persona ref points elsewhere — an internal inconsistency
    # the operator's consent (which shows default_persona) must not paper over.
    manifest["default_persona"]["ref"] = "casa/other@0.1.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    component = load_specialist_component(root, manifest_path)
    rows = [r for r in resolve_dependency_closure(component, root) if r.kind == "persona"]
    assert rows[0].available is False
    assert "default_persona" in rows[0].detail


# --------------------------------------------------------------------------
# F4 — corpus identifier containment
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../canary", "/etc", "a/b", "..", "", "A", "a/../b"])
def test_is_safe_corpus_identifier_rejects_unsafe(bad: str) -> None:
    assert is_safe_corpus_identifier(bad) is False


def test_is_safe_corpus_identifier_accepts_conservative_names() -> None:
    assert is_safe_corpus_identifier("mtg-rules-corpus") is True
    assert is_safe_corpus_identifier("corpus.v1_2") is True


def test_unsafe_corpus_identifier_never_stats_outside_the_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugin_store

    component_dir = tmp_path / "deep" / "component"
    root = _write_component(component_dir, dependencies=[
        {"kind": "corpus/data", "identifier": "../../canary", "digest": "sha256:" + "9" * 64},
    ])
    # A real directory at the escape target — pre-fix, `component_dir /
    # "corpus" / "../../canary"` resolves here and gets content-hashed.
    canary = tmp_path / "deep" / "canary"
    canary.mkdir(parents=True)
    (canary / "secret").write_text("x", encoding="utf-8")

    called: list = []
    real_cc = plugin_store.content_checksum

    def _tracking_cc(path):
        called.append(str(path))
        return real_cc(path)

    monkeypatch.setattr(plugin_store, "content_checksum", _tracking_cc)

    component = load_specialist_component(root, root / "manifest.json")
    rows = [r for r in resolve_dependency_closure(component, root) if r.kind == "corpus/data"]
    assert rows[0].available is False
    assert "unsafe corpus identifier" in rows[0].detail
    # RED on pre-fix: content_checksum was invoked on the escaping path.
    assert not any("canary" in c for c in called)


# --------------------------------------------------------------------------
# F5 — fail-closed self-heal on op-file / tuple divergence
# --------------------------------------------------------------------------


def _install_active(tmp_path: Path, slug: str = "mtg"):
    inspection, acks = _approved_inspection(tmp_path, slug=slug)
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
    assert instance.state == "active"
    return specialists_dir, agents_dir


def test_self_heal_drops_stale_slug_when_rematerialize_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import specialist_materialize

    specialists_dir, agents_dir = _install_active(tmp_path)
    slug_dir = agents_dir / "mtg"
    assert slug_dir.is_symlink()

    # Corrupt the on-disk binding marker so it no longer matches the active
    # tuple — simulating op-files left over from a superseded tuple.
    content = agents_dir / os.readlink(slug_dir)
    (content / ".binding-digest").write_text(
        json.dumps({"binding_digest": "STALE", "root": "STALE"}), encoding="utf-8")

    # Force every subsequent re-materialize to fail.
    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(specialist_materialize, "_write_specialist_operational_files", _boom)

    specialist_materialize.current_specialist_roles_dir(
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    # RED on pre-fix: the stale symlink survived (only a warning was logged),
    # leaving the slug running with stale capabilities.
    assert not (agents_dir / "mtg").exists()
    assert not (agents_dir / "mtg").is_symlink()


def test_self_heal_keeps_current_slug_when_rematerialize_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import specialist_materialize

    specialists_dir, agents_dir = _install_active(tmp_path)
    slug_dir = agents_dir / "mtg"
    # Marker is CURRENT for the active tuple (written at install) — a
    # re-materialize failure here is benign, the files already match.

    def _boom(*a, **k):
        raise RuntimeError("transient")

    monkeypatch.setattr(specialist_materialize, "_write_specialist_operational_files", _boom)

    specialist_materialize.current_specialist_roles_dir(
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    # A current marker means the op-files are kept; the slug stays loadable.
    assert (agents_dir / "mtg").is_symlink()
    assert (agents_dir / "mtg" / "runtime.yaml").is_file()


# --------------------------------------------------------------------------
# F6 — rollback verification gate
# --------------------------------------------------------------------------


def test_rollback_refuses_when_prior_dependency_now_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    v1, acks1 = _approved_inspection(tmp_path, slug="mtg", version_suffix="-v1")
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    commit_specialist_install(
        inspection=v1, config={}, secret_names_provided=frozenset(), acks=acks1,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    v2, acks2 = _approved_inspection(
        tmp_path, slug="mtg", version_suffix="-v2", version_override="0.2.0")
    upgrade_specialist(
        slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(), acks=acks2,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    from personality_binding import InstanceDir
    active_before = InstanceDir(specialists_dir / "mtg").active()
    assert active_before is not None

    # Make the dependency closure report an unavailable dep at rollback time.
    from specialist_install import DependencyResolution

    def _unavailable(component, component_dir):
        return (DependencyResolution(
            kind="plugin/implementation", identifier="mtg", digest="sha256:" + "9" * 64,
            available=False, detail="plugin no longer registered"),)

    monkeypatch.setattr(specialist_install, "resolve_dependency_closure", _unavailable)

    with pytest.raises(SpecialistInstallError) as exc:
        rollback_specialist(
            slug="mtg", specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
    assert exc.value.kind == "dependency_unavailable"

    # RED on pre-fix: rollback ignored the closure entirely and committed the
    # prior tuple, swapping the running active out from under a broken dep.
    still_active = InstanceDir(specialists_dir / "mtg").active()
    assert still_active is not None
    assert still_active.binding.binding_digest == active_before.binding.binding_digest


# --------------------------------------------------------------------------
# Md — concurrent-install CAS race
# --------------------------------------------------------------------------


def test_commit_survives_concurrent_cas_publish_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspection, acks = _approved_inspection(tmp_path, slug="mtg")
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    cas_dir = specialist_install.cas_store_dir(
        inspection.root_digest, store_root=specialists_dir / "store")

    real_replace = os.replace
    raced = {"done": False}

    def _racing_replace(src, dst):
        # Simulate a concurrent install winning the race for THIS exact CAS
        # dir: publish identical bytes first, so our own os.replace then hits a
        # now-non-empty target and raises ENOTEMPTY.
        if not raced["done"] and Path(dst) == cas_dir and Path(src).exists():
            raced["done"] = True
            shutil.copytree(src, dst)
        return real_replace(src, dst)

    monkeypatch.setattr(specialist_install.os, "replace", _racing_replace)

    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    # RED on pre-fix: the bare os.replace raised OSError uncaught. Post-fix the
    # race is absorbed and the install re-verifies the winner's bytes.
    assert raced["done"] is True
    assert instance.state == "active"


# --------------------------------------------------------------------------
# Round-3 F1 — stage_desired must be INSIDE MATERIALIZE_LOCK so that
# stage+commit+materialize is one atomic unit against a concurrent same-slug
# mutation. Deterministic proof: patch InstanceDir.stage_desired to record
# whether the lock is held at the moment it runs on the ACTIVATING path, and
# assert it was held (pre-fix it ran before the `with MATERIALIZE_LOCK:`).
# --------------------------------------------------------------------------


def _record_stage_lockstate(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Patches InstanceDir.stage_desired to record MATERIALIZE_LOCK.locked()
    on every call while still performing the real staging, and returns the
    recording list."""
    import specialist_materialize
    from personality_binding import InstanceDir

    seen: list[bool] = []
    real_stage = InstanceDir.stage_desired

    def _wrapped(self, tup, *a, **k):
        seen.append(specialist_materialize.MATERIALIZE_LOCK.locked())
        return real_stage(self, tup, *a, **k)

    monkeypatch.setattr(InstanceDir, "stage_desired", _wrapped)
    return seen


def test_commit_stages_desired_inside_the_materialize_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _record_stage_lockstate(monkeypatch)
    inspection, acks = _approved_inspection(tmp_path, slug="mtg")
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"

    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    assert instance.state == "active"
    # The activating path stages exactly once, and it ran with the lock HELD.
    assert seen == [True]


def test_upgrade_stages_desired_inside_the_materialize_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    v1, acks1 = _approved_inspection(tmp_path, slug="mtg", version_suffix="-v1")
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    commit_specialist_install(
        inspection=v1, config={}, secret_names_provided=frozenset(), acks=acks1,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    seen = _record_stage_lockstate(monkeypatch)
    v2, acks2 = _approved_inspection(
        tmp_path, slug="mtg", version_suffix="-v2", version_override="0.2.0")
    instance = upgrade_specialist(
        slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(), acks=acks2,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    assert instance.state == "active"
    assert seen == [True]


def test_rollback_stages_desired_inside_the_materialize_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    v1, acks1 = _approved_inspection(tmp_path, slug="mtg", version_suffix="-v1")
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    commit_specialist_install(
        inspection=v1, config={}, secret_names_provided=frozenset(), acks=acks1,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
    v2, acks2 = _approved_inspection(
        tmp_path, slug="mtg", version_suffix="-v2", version_override="0.2.0")
    upgrade_specialist(
        slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(), acks=acks2,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    seen = _record_stage_lockstate(monkeypatch)
    instance = rollback_specialist(
        slug="mtg", specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    assert instance.state == "active"
    assert seen == [True]


# --------------------------------------------------------------------------
# Round-3 F2 — uninstall removes the op-dir AND active.yaml under
# MATERIALIZE_LOCK, so a self-heal reconcile pass over a STALE index snapshot
# that still lists the slug re-reads active()==None inside the lock and skips
# it — the op dir stays absent (no resurrection from retained CAS bytes).
# --------------------------------------------------------------------------


def test_uninstall_under_lock_prevents_reconcile_resurrection(
    tmp_path: Path,
) -> None:
    import specialist_materialize
    from specialist_registry import InstalledSpecialistIndex

    specialists_dir, agents_dir = _install_active(tmp_path, slug="mtg")
    assert (agents_dir / "mtg").is_symlink()

    # Snapshot the index while "mtg" is still installed — mirrors
    # current_specialist_roles_dir loading the index once before its reconcile
    # loop runs. This snapshot still lists "mtg".
    stale_index = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    stale_index.load()
    assert "mtg" in set(stale_index.installed_slugs())

    # Uninstall removes the op symlink/content AND specialists/mtg (active.yaml)
    # under MATERIALIZE_LOCK.
    uninstall_specialist(
        slug="mtg", specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
    assert not (agents_dir / "mtg").exists()
    assert not (specialists_dir / "mtg").exists()

    # A reconcile pass over the STALE snapshot (still lists "mtg"): its in-lock
    # active()-re-read yields None because active.yaml is gone, so the slug is
    # skipped and never rematerialized. RED-relevant: without removing
    # active.yaml under the same lock, the stale snapshot's retained CAS bytes
    # would resurrect the op dir here.
    specialist_materialize._reconcile_specialist_operational_files(
        installed_index=stale_index, specialists_dir=specialists_dir,
        agents_specialists_dir=agents_dir)

    assert not (agents_dir / "mtg").exists()
    assert not (agents_dir / "mtg").is_symlink()


# ==========================================================================
# Round 4 — close the class: EVERY InstanceDir write under MATERIALIZE_LOCK,
# in-lock re-validation of pre-lock reads, and the roles-overlay rebuild
# serialized.
# ==========================================================================


def _approved_inspection_requiring_config(
    tmp_path: Path, *, slug: str = "mtg", version_suffix: str = "",
    version_override: str | None = None,
):
    """Like `_approved_inspection`, but the component's config-schema REQUIRES
    an `api_key` secret — so a commit/upgrade that supplies none fails closed
    into pending-configuration (exercising the placeholder stage path)."""
    from specialist_component import compute_component_checksum, load_specialist_component
    from specialist_install import InspectionResult, compute_install_root_digest
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

    root = _write_component(tmp_path / f"component-cfg-{slug}{version_suffix}", slug=slug)
    (root / "config-schema.json").write_text(
        json.dumps({"required": ["api_key"], "secret_names": ["api_key"]}), encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = {
        "role/role.yaml": (root / "role" / "role.yaml").read_bytes(),
        "role/doctrine.md": (root / "role" / "doctrine.md").read_bytes(),
        "config-schema.json": (root / "config-schema.json").read_bytes(),
    }
    manifest["checksum"] = compute_component_checksum(files)
    if version_override is not None:
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
        required_config_names=(), required_secret_names=("api_key",),
        dependencies=deps, staged_dir=root)
    acks = SpecialistInstallAckStore(path=tmp_path / f"acks-cfg-{slug}{version_suffix}.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug)
    acks.record(identity=identity, component_id=inspection.component_id, version=inspection.version,
                component_checksum=inspection.root_digest, slug=inspection.slug)
    return inspection, acks


def _record_stage_and_discard_lockstate(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch InstanceDir.stage_desired AND discard_desired to record
    MATERIALIZE_LOCK.locked() on every call while still performing the real
    write. Returns {"stage": [...], "discard": [...]}."""
    import specialist_materialize
    from personality_binding import InstanceDir

    seen = {"stage": [], "discard": []}
    real_stage = InstanceDir.stage_desired
    real_discard = InstanceDir.discard_desired

    def _stage(self, tup, *a, **k):
        seen["stage"].append(specialist_materialize.MATERIALIZE_LOCK.locked())
        return real_stage(self, tup, *a, **k)

    def _discard(self, *a, **k):
        seen["discard"].append(specialist_materialize.MATERIALIZE_LOCK.locked())
        return real_discard(self, *a, **k)

    monkeypatch.setattr(InstanceDir, "stage_desired", _stage)
    monkeypatch.setattr(InstanceDir, "discard_desired", _discard)
    return seen


# -- F1: pending-config placeholder + upgrade error path stage under the lock --


def test_commit_pending_config_stages_desired_inside_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _record_stage_and_discard_lockstate(monkeypatch)
    inspection, acks = _approved_inspection_requiring_config(tmp_path, slug="mtg")
    instance = commit_specialist_install(
        inspection=inspection, config={}, secret_names_provided=frozenset(), acks=acks,
        specialists_dir=tmp_path / "specialists", agents_specialists_dir=tmp_path / "agents")
    assert instance.state == "pending-configuration"
    # RED pre-fix: the placeholder stage_desired ran OUTSIDE MATERIALIZE_LOCK.
    assert seen["stage"] == [True]


def test_upgrade_pending_config_stages_desired_inside_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    v1, acks1 = _approved_inspection(tmp_path, slug="mtg", version_suffix="-v1")
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    commit_specialist_install(
        inspection=v1, config={}, secret_names_provided=frozenset(), acks=acks1,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    seen = _record_stage_and_discard_lockstate(monkeypatch)
    v2, acks2 = _approved_inspection_requiring_config(
        tmp_path, slug="mtg", version_suffix="-v2", version_override="0.2.0")
    instance = upgrade_specialist(
        slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(), acks=acks2,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
    assert instance.state == "pending-configuration"
    # RED pre-fix: the upgrade placeholder stage_desired ran OUTSIDE the lock.
    assert seen["stage"] == [True]


def test_upgrade_error_path_stages_and_discards_inside_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import prompt_compiler

    v1, acks1 = _approved_inspection(tmp_path, slug="mtg", version_suffix="-v1")
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    commit_specialist_install(
        inspection=v1, config={}, secret_names_provided=frozenset(), acks=acks1,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
    v2, acks2 = _approved_inspection(
        tmp_path, slug="mtg", version_suffix="-v2", version_override="0.2.0")

    # Force the pre-activation compile to fail so upgrade takes the ERROR path
    # (stage the active binding as desired, then discard it into
    # desired.error.yaml). Patch the SOURCE module attribute (upgrade imports
    # it locally as `from prompt_compiler import compile_prompt_bundle`).
    def _boom(*a, **k):
        raise ValueError("ceiling exceeded")

    monkeypatch.setattr(prompt_compiler, "compile_prompt_bundle", _boom)

    seen = _record_stage_and_discard_lockstate(monkeypatch)
    instance = upgrade_specialist(
        slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(), acks=acks2,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
    assert instance.state == "error"
    # RED pre-fix: BOTH the error-path stage_desired and discard_desired ran
    # OUTSIDE MATERIALIZE_LOCK.
    assert seen["stage"] == [True]
    assert seen["discard"] == [True]


# -- F2: uninstall/install racing between the pre-lock read and the in-lock
#        stage -> typed concurrent_mutation refusal, no recreated dir. --------


def test_upgrade_refuses_when_uninstall_races_before_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import prompt_compiler
    from personality_binding import InstanceDir

    v1, acks1 = _approved_inspection(tmp_path, slug="mtg", version_suffix="-v1")
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    commit_specialist_install(
        inspection=v1, config={}, secret_names_provided=frozenset(), acks=acks1,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
    v2, acks2 = _approved_inspection(
        tmp_path, slug="mtg", version_suffix="-v2", version_override="0.2.0")

    # Hook the pre-activation compile (runs AFTER active_before was read, BEFORE
    # the in-lock re-check) to run a full uninstall — the concurrent mutation
    # that removes active.yaml out from under the upgrade.
    real_compile = prompt_compiler.compile_prompt_bundle
    fired = {"done": False}

    def _compile_then_uninstall(*a, **k):
        result = real_compile(*a, **k)
        if not fired["done"]:
            fired["done"] = True
            uninstall_specialist(
                slug="mtg", specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
        return result

    monkeypatch.setattr(prompt_compiler, "compile_prompt_bundle", _compile_then_uninstall)

    with pytest.raises(SpecialistInstallError) as exc:
        upgrade_specialist(
            slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(), acks=acks2,
            specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
    assert exc.value.kind == "concurrent_mutation"
    assert fired["done"] is True
    # RED pre-fix: with no in-lock re-check, upgrade staged+committed after the
    # uninstall, RESURRECTING the removed slug. Post-fix nothing is recreated.
    assert InstanceDir(specialists_dir / "mtg").active() is None
    assert not (specialists_dir / "mtg" / "desired.yaml").exists()
    assert not (agents_dir / "mtg").exists()


def test_rollback_refuses_when_uninstall_races_before_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import prompt_compiler
    from personality_binding import InstanceDir

    v1, acks1 = _approved_inspection(tmp_path, slug="mtg", version_suffix="-v1")
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    commit_specialist_install(
        inspection=v1, config={}, secret_names_provided=frozenset(), acks=acks1,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
    v2, acks2 = _approved_inspection(
        tmp_path, slug="mtg", version_suffix="-v2", version_override="0.2.0")
    upgrade_specialist(
        slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(), acks=acks2,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    # rollback re-runs a compile as its verification gate (before the lock) —
    # hook it to uninstall the slug first.
    real_compile = prompt_compiler.compile_prompt_bundle
    fired = {"done": False}

    def _compile_then_uninstall(*a, **k):
        result = real_compile(*a, **k)
        if not fired["done"]:
            fired["done"] = True
            uninstall_specialist(
                slug="mtg", specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
        return result

    monkeypatch.setattr(prompt_compiler, "compile_prompt_bundle", _compile_then_uninstall)

    with pytest.raises(SpecialistInstallError) as exc:
        rollback_specialist(
            slug="mtg", specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
    assert exc.value.kind == "concurrent_mutation"
    assert fired["done"] is True
    # RED pre-fix: rollback staged the prior tuple after the uninstall,
    # resurrecting the removed slug.
    assert InstanceDir(specialists_dir / "mtg").active() is None
    assert not (specialists_dir / "mtg" / "desired.yaml").exists()
    assert not (agents_dir / "mtg").exists()


def test_commit_refuses_when_a_concurrent_install_activates_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import prompt_compiler
    from personality_binding import InstanceDir

    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    # Two independent approved inspections for the SAME slug but DIFFERENT
    # versions (distinct root digests) — the concurrent-install collision the
    # inspect-time uniqueness check cannot catch once two installs both cleared
    # it and race into commit.
    loser, loser_acks = _approved_inspection(tmp_path, slug="mtg", version_suffix="-a")
    winner, winner_acks = _approved_inspection(
        tmp_path, slug="mtg", version_suffix="-b", version_override="0.2.0")

    real_compile = prompt_compiler.compile_prompt_bundle
    fired = {"done": False}

    def _compile_then_win(*a, **k):
        result = real_compile(*a, **k)
        if not fired["done"]:
            fired["done"] = True  # guard: the nested commit's own compile must not re-fire
            commit_specialist_install(
                inspection=winner, config={}, secret_names_provided=frozenset(), acks=winner_acks,
                specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
        return result

    monkeypatch.setattr(prompt_compiler, "compile_prompt_bundle", _compile_then_win)

    with pytest.raises(SpecialistInstallError) as exc:
        commit_specialist_install(
            inspection=loser, config={}, secret_names_provided=frozenset(), acks=loser_acks,
            specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
    assert exc.value.kind == "concurrent_mutation"
    assert fired["done"] is True
    # RED pre-fix: the loser staged+committed over the winner, rotating the
    # winner's active into prior (silent double-activate). Post-fix the winner's
    # active is preserved untouched.
    active = InstanceDir(specialists_dir / "mtg").active()
    assert active is not None
    _, version, _ = parse_component_root(active.root)
    assert version == "0.2.0"  # the winner's version, not the loser's 0.1.0


# -- F3: concurrent current_specialist_roles_dir threads must serialize the
#        destructive roles-overlay rmtree+rebuild — no exception, overlay
#        complete. -------------------------------------------------------------


def test_concurrent_current_specialist_roles_dir_threads_build_complete_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time
    import specialist_materialize
    from concurrent.futures import ThreadPoolExecutor

    specialists_dir, agents_dir = _install_active(tmp_path, slug="mtg")
    _install_active(tmp_path, slug="research")  # same dirs — a multi-slug overlay
    _install_active(tmp_path, slug="finance")
    expected_slugs = {"mtg", "research", "finance"}

    # Widen the copy window so a concurrent thread's rmtree of the shared
    # specialist-overlay dir reliably lands BETWEEN a copy's dest.mkdir and its
    # file writes — deterministically reproducing the pre-fix race (the
    # write_bytes then hits a vanished parent -> FileNotFoundError surfaced as a
    # future exception). Post-fix the whole rebuild is serialized under
    # MATERIALIZE_LOCK, so the sleep only slows things down and never collides.
    def _slow_copy(src: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True, mode=0o700)
        time.sleep(0.02)
        for name in ("role.yaml", "doctrine.md"):
            source_file = src / name
            if not source_file.is_file():
                raise ValueError(f"{src}: missing required role-artifact file {name!r}")
            (dest / name).write_bytes(source_file.read_bytes())

    monkeypatch.setattr(specialist_materialize, "_copy_role_dir", _slow_copy)

    for _ in range(4):
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = [
                ex.submit(
                    specialist_materialize.current_specialist_roles_dir,
                    specialists_dir=specialists_dir,
                    agents_specialists_dir=agents_dir,
                )
                for _ in range(4)
            ]
            overlay_roots = [f.result() for f in futures]  # RED pre-fix: raises here

        for overlay_root in overlay_roots:
            overlay = Path(overlay_root) / "specialist"
            for slug in expected_slugs:
                assert (overlay / slug / "role.yaml").is_file()
                assert (overlay / slug / "doctrine.md").is_file()


# ==========================================================================
# Round 5 — whole-branch fix wave 5: full-tuple revalidation (F1), the missed
# InstanceDir writers persona_install.apply_persona_override + tools._stage_and_
# report (F2), and the in-lock index reload in current_specialist_roles_dir (F3).
# ==========================================================================


def _publish_installed_copy(persona, specialists_dir: Path, slug: str,
                            tmp_path: Path, monkeypatch) -> None:
    """Copy an installed specialist's CAS-bundled persona into the operator
    installed-personas root, through the `$CASA_CONFIG_DIR` seam (#543)."""
    import shutil

    from personality_binding import InstanceDir
    from specialist_install import cas_store_dir, parse_component_root

    active = InstanceDir(specialists_dir / slug).active()
    _, _, checksum = parse_component_root(active.root)
    cas_dir = cas_store_dir(checksum, store_root=specialists_dir / "store")
    config_root = tmp_path / "config-root"
    monkeypatch.setenv("CASA_CONFIG_DIR", str(config_root))
    dest = config_root / "personas" / persona.persona_id / persona.version
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(cas_dir / "persona" / "pack", dest / "pack", dirs_exist_ok=True)
    shutil.copy2(cas_dir / "persona" / "manifest.json", dest / "manifest.json")


def _load_specialist_persona_role(specialists_dir: Path, slug: str):
    """Load the (persona, role) pair from an installed specialist's CAS bytes —
    exactly what apply_persona_override's specialist caller (tools.persona_apply)
    resolves before applying an override."""
    from persona_pack import load_persona_pack
    from role_slot import materialize_role
    from role_artifact import load_role_artifact
    from personality_binding import InstanceDir
    from specialist_install import parse_component_root, cas_store_dir

    active = InstanceDir(specialists_dir / slug).active()
    _, _, checksum = parse_component_root(active.root)
    cas_dir = cas_store_dir(checksum, store_root=specialists_dir / "store")
    persona = load_persona_pack(cas_dir / "persona" / "pack", cas_dir / "persona" / "manifest.json")
    role = materialize_role(source=load_role_artifact(cas_dir / "role"), options={})
    return persona, role


# -- F1: full-tuple revalidation catches a concurrent SAME-ROOT mutation that a
#        root-only check waved through (silent overwrite of the concurrent win).-


def test_upgrade_refuses_when_a_same_root_config_mutation_races_before_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import prompt_compiler
    from personality_binding import InstanceDir, InstanceTuple

    v1, acks1 = _approved_inspection(tmp_path, slug="mtg", version_suffix="-v1")
    specialists_dir = tmp_path / "specialists"
    agents_dir = tmp_path / "agents"
    commit_specialist_install(
        inspection=v1, config={}, secret_names_provided=frozenset(), acks=acks1,
        specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
    v2, acks2 = _approved_inspection(
        tmp_path, slug="mtg", version_suffix="-v2", version_override="0.2.0")

    # Hook the pre-activation compile (runs AFTER active_before was read, BEFORE
    # the in-lock re-check) to re-commit the active tuple with an ALTERED
    # config_snapshot but the SAME root — the same-root case a root-only
    # revalidation waved through and the F1 full-tuple fix must catch. #372
    # rewrite: the digest and binding are recomputed honestly over the mutated
    # snapshot (the old shape — mutated snapshot under the old digest — is now
    # unwritable by design), which still leaves `root` unchanged.
    real_compile = prompt_compiler.compile_prompt_bundle
    fired = {"done": False}

    def _compile_then_config_mutate(*a, **k):
        result = real_compile(*a, **k)
        if not fired["done"]:
            fired["done"] = True
            from dataclasses import replace
            from personality_binding import (
                compute_binding_digest, compute_effective_config_digest,
                make_instance_tuple,
            )
            idir = InstanceDir(specialists_dir / "mtg")
            active = idir.active()
            new_snapshot = {**dict(active.config_snapshot), "_probe": "raced"}
            ecd = compute_effective_config_digest(new_snapshot)
            b = active.binding
            raced_binding = replace(
                b, effective_config_digest=ecd,
                binding_digest=compute_binding_digest(
                    stable_agent_id=b.stable_agent_id, role_checksum=b.role_checksum,
                    persona_id=b.persona_id, persona_version=b.persona_version,
                    persona_checksum=b.persona_checksum,
                    compiler_schema_version=b.compiler_schema_version,
                    dependency_digests=b.dependency_digests,
                    effective_config_digest=ecd))
            idir.stage_desired(make_instance_tuple(
                root=active.root, binding=raced_binding,
                config_snapshot=new_snapshot))
            idir.commit_desired_to_active()
        return result

    monkeypatch.setattr(prompt_compiler, "compile_prompt_bundle", _compile_then_config_mutate)

    with pytest.raises(SpecialistInstallError) as exc:
        upgrade_specialist(
            slug="mtg", inspection=v2, config={}, secret_names_provided=frozenset(), acks=acks2,
            specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
    assert exc.value.kind == "concurrent_mutation"
    assert fired["done"] is True
    # RED pre-fix: root-only revalidation passed (both roots are v1), so the
    # upgrade committed v2 OVER the raced config mutation — silently losing it.
    # Post-fix the raced config survives untouched and the version is still v1.
    active = InstanceDir(specialists_dir / "mtg").active()
    assert active is not None
    assert active.config_snapshot.get("_probe") == "raced"
    _, version, _ = parse_component_root(active.root)
    assert version == "0.1.0"


# -- F2a: apply_persona_override's specialist branch stages+commits UNDER the
#         lock, with full-tuple revalidation against its pre-lock active_before.-


def test_apply_persona_override_specialist_stages_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from persona_install import apply_persona_override

    specialists_dir, agents_dir = _install_active(tmp_path, slug="mtg")
    persona, role = _load_specialist_persona_role(specialists_dir, "mtg")
    # #543: an override must name a persona the specialist loader can resolve,
    # and it resolves overrides from the INSTALLED root only — a component's
    # own CAS-bundled pack is not one of the approved roots. Publish the same
    # bytes there (identical checksum, so the binding's pin still holds).
    _publish_installed_copy(persona, specialists_dir, "mtg", tmp_path, monkeypatch)

    seen = _record_stage_lockstate(monkeypatch)
    committed = apply_persona_override(
        target_role_id="specialist:mtg", persona=persona, role=role,
        instance_dir_root=specialists_dir / "mtg",
        candidate_validator=lambda persona, binding: None)

    assert committed.binding.mode == "override"
    # RED pre-fix: the specialist branch staged+committed with NO lock held.
    assert seen == [True]


def test_apply_persona_override_specialist_refuses_when_uninstall_races_before_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import personality_binding
    from persona_install import apply_persona_override
    from personality_binding import InstanceDir

    specialists_dir, agents_dir = _install_active(tmp_path, slug="mtg")
    persona, role = _load_specialist_persona_role(specialists_dir, "mtg")

    # materialize_override_binding is the read-only gate apply_persona_override
    # runs AFTER reading active_before but BEFORE taking the lock — hook it to
    # uninstall the slug (the concurrent mutation that removes active.yaml).
    real_mob = personality_binding.materialize_override_binding
    fired = {"done": False}

    def _mob_then_uninstall(*a, **k):
        result = real_mob(*a, **k)
        if not fired["done"]:
            fired["done"] = True
            uninstall_specialist(
                slug="mtg", specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)
        return result

    monkeypatch.setattr(personality_binding, "materialize_override_binding", _mob_then_uninstall)

    with pytest.raises(SpecialistInstallError) as exc:
        apply_persona_override(
            target_role_id="specialist:mtg", persona=persona, role=role,
            instance_dir_root=specialists_dir / "mtg",
            candidate_validator=lambda persona, binding: None)
    assert exc.value.kind == "concurrent_mutation"
    assert fired["done"] is True
    # RED pre-fix: the un-locked branch staged+committed after the uninstall,
    # RESURRECTING the removed slug's InstanceDir. Post-fix nothing is recreated.
    assert InstanceDir(specialists_dir / "mtg").active() is None
    assert not (specialists_dir / "mtg" / "desired.yaml").exists()
    assert not (agents_dir / "mtg").exists()


# -- F2b: the resident staging in tools._stage_and_report is offloaded to a
#         worker thread and runs its InstanceDir write UNDER the shared lock. ---


def test_stage_and_report_resident_stages_under_the_lock_off_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import agent_loader
    import tools
    from personality_binding import InstanceDir, materialize_override_binding
    from test_reconcile_resident_binding import _persona, _role

    monkeypatch.setenv("CASA_BINDINGS_DIR", str(tmp_path / "bindings"))
    role = _role()
    # #543: the in-lock persona re-verify requires a pack that is actually
    # installed under the approved roots — see install_persona_for_apply.
    from test_persona_install import install_persona_for_apply
    persona = install_persona_for_apply(
        tmp_path, monkeypatch, persona_id="casa/gary", version="0.2.0")
    binding = materialize_override_binding(
        role=role, persona=persona, override_source="operator:casa/gary@0.2.0")

    seen = _record_stage_lockstate(monkeypatch)
    result = asyncio.run(tools._stage_and_report(role.role_id, role.slot, binding))

    assert result["ok"] is True
    assert result["persona"] == "casa/gary@0.2.0"
    # RED pre-fix: the resident stage ran with NO lock held (and on the loop).
    assert seen == [True]
    # The desired tuple was actually staged under the CASA_BINDINGS_DIR seam.
    idir = InstanceDir(agent_loader._resident_bindings_root(None) / f"resident-{role.slot}")
    assert idir.desired() is not None


# -- F3: current_specialist_roles_dir RE-LOADS the index in-lock, so an
#        install/uninstall committed after the caller's off-lock snapshot is
#        reflected in the overlay (new slug present / removed slug omitted). ----


def test_roles_dir_in_lock_reload_includes_a_slug_installed_after_the_snapshot(
    tmp_path: Path,
) -> None:
    import specialist_materialize
    from specialist_registry import InstalledSpecialistIndex

    specialists_dir, agents_dir = _install_active(tmp_path, slug="mtg")
    # Snapshot the index BEFORE "research" is installed — mirrors reload/boot
    # loading the index OFF-lock before current_specialist_roles_dir runs.
    snapshot = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    snapshot.load()
    assert set(snapshot.installed_slugs()) == {"mtg"}

    # "research" commits AFTER the snapshot, BEFORE the reconcile below.
    _install_active(tmp_path, slug="research")

    overlay = Path(specialist_materialize.current_specialist_roles_dir(
        installed_index=snapshot, specialists_dir=specialists_dir,
        agents_specialists_dir=agents_dir)) / "specialist"

    # RED pre-fix: the overlay was built from the stale snapshot, omitting the
    # just-installed "research". Post-fix the in-lock reload includes it.
    assert (overlay / "research" / "role.yaml").is_file()
    assert (overlay / "mtg" / "role.yaml").is_file()


def test_roles_dir_in_lock_reload_omits_a_slug_uninstalled_after_the_snapshot(
    tmp_path: Path,
) -> None:
    import specialist_materialize
    from specialist_registry import InstalledSpecialistIndex

    specialists_dir, agents_dir = _install_active(tmp_path, slug="mtg")
    _install_active(tmp_path, slug="research")
    snapshot = InstalledSpecialistIndex(specialists_dir=str(specialists_dir))
    snapshot.load()
    assert set(snapshot.installed_slugs()) == {"mtg", "research"}

    # "research" is uninstalled AFTER the snapshot, BEFORE the reconcile below.
    uninstall_specialist(
        slug="research", specialists_dir=specialists_dir, agents_specialists_dir=agents_dir)

    overlay = Path(specialist_materialize.current_specialist_roles_dir(
        installed_index=snapshot, specialists_dir=specialists_dir,
        agents_specialists_dir=agents_dir)) / "specialist"

    # RED pre-fix: the stale snapshot still listed "research", so the overlay
    # RESURRECTED it from retained CAS bytes. Post-fix the in-lock reload drops it.
    assert not (overlay / "research").exists()
    assert (overlay / "mtg" / "role.yaml").is_file()


# ---------------------------------------------------------------------------
# Whole-branch review round 6, F3 — loop-safety of the boot lock acquisitions.
# casa_core boot must reach BOTH lock-acquiring calls off the event loop:
#   - current_specialist_roles_dir (acquires MATERIALIZE_LOCK directly), and
#   - load_all_agents (acquires it via reconcile_resident_binding, round 6 F1)
# must each run inside `await asyncio.to_thread(...)`, never synchronously on the
# loop. A source-level assertion is the cheap seam the enumeration relies on.
# ---------------------------------------------------------------------------


def test_casa_core_boot_offloads_both_lock_acquiring_calls_to_a_worker_thread():
    import re as _re

    import specialist_materialize

    src = (Path(specialist_materialize.__file__).with_name("casa_core.py")).read_text(encoding="utf-8")

    # current_specialist_roles_dir must be the target of an await asyncio.to_thread(...)
    assert _re.search(
        r"await\s+asyncio\.to_thread\(\s*current_specialist_roles_dir\b", src
    ), "boot must call current_specialist_roles_dir via asyncio.to_thread"
    # load_all_agents likewise (its reconcile_resident_binding takes the lock).
    assert _re.search(
        r"await\s+asyncio\.to_thread\(\s*load_all_agents\b", src
    ), "boot must call load_all_agents via asyncio.to_thread"
    # And neither may be invoked bare (synchronously) on the loop.
    assert not _re.search(r"^\s*roles_overlay\s*=\s*current_specialist_roles_dir\(", src, _re.M)
    assert not _re.search(r"^\s*role_configs\s*=\s*load_all_agents\(", src, _re.M)
