"""One consent identity: the inspected artifact plus the effective settings.

An approval is for one artifact and the settings the library will actually
stage — the call's config merged over what the install already keeps. These
cases run the real ack stores (reloaded from disk) and the real library
lifecycle on a local component: what is staged is exactly what was approved,
and settings that changed since the approval refuse before anything is staged
or journalled."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

import plugin_registry
import specialist_bundle_journal
import specialist_install
import specialist_receipt
from approval_records import values_digest
from canonical_bytes import canonical_json_bytes, canonical_text, checksum_bytes, checksum_json
from persona_install import (
    PersonaInspectionResult, PersonaInstallAckStore, PersonaLedgerInvalid,
    commit_persona_install, list_installed_personas, persona_install_consent_identity,
)
from personality_binding import InstanceDir
from specialist_fixtures import write_minimal_component
from specialist_install import SpecialistInstallError
from specialist_install_consent import (
    SpecialistInstallAckStore, identity_for_inspection, install_consent_identity,
)
from specialist_registry import InstalledSpecialistIndex


@pytest.fixture(autouse=True)
def _fresh_registry_snapshot(tmp_path):
    plugin_registry.reload_snapshot(registry_path=tmp_path / "snap-registry.json",
                                    store_root=tmp_path / "snap-store")
    yield


def _component(tmp_path: Path, name: str, *, required: list[str], version: str = "0.1.0"
               ) -> Path:
    comp, manifest_path = write_minimal_component(tmp_path / name, slug="mtg",
                                                  required_config=required)
    if version != "0.1.0":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = version
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return comp


def _inspect(tmp_path: Path, monkeypatch, comp: Path, *, upgrade: bool = False,
             sha: str = "a" * 40):
    def _fetch(repo, ref, subdir, dest, *, expected_revision=None):
        shutil.copytree(comp, dest)
        return sha

    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _fetch)
    idx = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "installed-index"))
    idx.load()
    extra = (dict(mode="upgrade", target_slug="mtg", specialists_dir=tmp_path / "specialists")
             if upgrade else {})
    inspection = specialist_install.inspect_specialist_repo(
        "org/repo", "main", staging_root=tmp_path / ("staging-" + sha[:4]),
        installed_index=idx, receipts_dir=tmp_path / "receipts", **extra)
    receipt = specialist_receipt.load(inspection.receipt_id, receipts_dir=tmp_path / "receipts")
    assert receipt is not None
    return inspection, receipt


def _paths(tmp_path: Path) -> dict:
    return dict(specialists_dir=tmp_path / "specialists",
                agents_specialists_dir=tmp_path / "agents",
                registry_path=tmp_path / "registry.json",
                plugin_store_root=tmp_path / "store", ops_dir=tmp_path / "ops")


def _approve(tmp_path: Path, inspection, values: dict) -> SpecialistInstallAckStore:
    """Record the operator's approval of *values* as the consent card does,
    then reopen the ledger from disk."""
    path = tmp_path / "acks.json"
    SpecialistInstallAckStore(path=path).record(
        identity=identity_for_inspection(inspection, values=values),
        component_id=inspection.component_id, version=inspection.version,
        component_checksum=inspection.root_digest, slug=inspection.slug,
        receipt_digest=inspection.receipt_digest, values_digest=values_digest(values))
    return SpecialistInstallAckStore(path=path)


def _install(tmp_path, inspection, receipt, acks, *, config, approved, secrets=frozenset()):
    instance, txn = specialist_install.commit_specialist_install(
        inspection=inspection, receipt=receipt, config=config,
        secret_names_provided=secrets, acks=acks, approved_values=approved,
        **_paths(tmp_path))
    specialist_bundle_journal.complete(txn.journal_path)
    return instance


def _journals(tmp_path: Path) -> list[str]:
    ops = tmp_path / "ops"
    return sorted(os.listdir(ops)) if ops.exists() else []


def _tree(tmp_path: Path) -> dict:
    slug_dir = tmp_path / "specialists" / "mtg"
    return {p.name: p.read_bytes() for p in sorted(slug_dir.glob("*.yaml"))} \
        if slug_dir.exists() else {}


# -- install -------------------------------------------------------------------------

def test_an_approval_for_the_effective_values_installs_exactly_those_values(
        tmp_path, monkeypatch):
    inspection, receipt = _inspect(tmp_path, monkeypatch,
                                   _component(tmp_path, "c", required=["A"]))
    values = {"config": {"A": "1"}, "secrets": []}
    assert specialist_install.preview_effective_config(
        "specialist_install", inspection, slug="mtg", config={"A": "1"},
        secret_names_provided=[], specialists_dir=tmp_path / "specialists") == values["config"]
    acks = _approve(tmp_path, inspection, values)
    instance = _install(tmp_path, inspection, receipt, acks, config={"A": "1"}, approved=values)
    assert instance.state == "active"
    assert dict(InstanceDir(tmp_path / "specialists" / "mtg").active().config_snapshot) == \
        {"A": "1"}


def test_saved_settings_that_change_the_effective_values_refuse_before_anything_is_staged(
        tmp_path, monkeypatch):
    inspection, receipt = _inspect(tmp_path, monkeypatch,
                                   _component(tmp_path, "c", required=["A", "B"]))
    # an earlier attempt saved A and is waiting for B
    first = {"config": {"A": "1"}, "secrets": []}
    pending = _install(tmp_path, inspection, receipt, _approve(tmp_path, inspection, first),
                       config={"A": "1"}, approved=first)
    assert pending.state == "pending-configuration"
    tree, journals = _tree(tmp_path), _journals(tmp_path)

    # approving only the submitted argument is not approving what would be staged
    naive = {"config": {"B": "2"}, "secrets": []}
    with pytest.raises(SpecialistInstallError) as raised:
        _install(tmp_path, inspection, receipt, _approve(tmp_path, inspection, naive),
                 config={"B": "2"}, approved=naive)
    assert raised.value.kind == "approval_values_differ"
    assert "A" in raised.value.detail and "B" not in raised.value.detail.split("(")[1]
    assert _tree(tmp_path) == tree and _journals(tmp_path) == journals

    # the preview reads the same tuples the library merges
    effective = specialist_install.preview_effective_config(
        "specialist_install", inspection, slug="mtg", config={"B": "2"},
        secret_names_provided=[], specialists_dir=tmp_path / "specialists")
    assert effective == {"A": "1", "B": "2"}
    values = {"config": effective, "secrets": []}
    instance = _install(tmp_path, inspection, receipt, _approve(tmp_path, inspection, values),
                        config={"B": "2"}, approved=values)
    assert instance.state == "active"
    assert dict(InstanceDir(tmp_path / "specialists" / "mtg").active().config_snapshot) == \
        {"A": "1", "B": "2"}


def test_an_ack_for_other_values_is_consent_missing(tmp_path, monkeypatch):
    inspection, receipt = _inspect(tmp_path, monkeypatch,
                                   _component(tmp_path, "c", required=["A"]))
    acks = _approve(tmp_path, inspection, {"config": {"A": "1"}, "secrets": []})
    other = {"config": {"A": "2"}, "secrets": []}
    with pytest.raises(SpecialistInstallError) as raised:
        _install(tmp_path, inspection, receipt, acks, config={"A": "2"}, approved=other)
    assert raised.value.kind == "consent_missing"
    assert _tree(tmp_path) == {} and _journals(tmp_path) == []


def _with_secret(comp: Path) -> Path:
    """Declare the secret ``S`` beside the required ``A``, keeping the tree
    checksum-valid."""
    from specialist_component import compute_component_checksum
    (comp / "config-schema.json").write_text(
        json.dumps({"required": ["A", "S"], "secret_names": ["S"]}), encoding="utf-8")
    manifest = json.loads((comp / "manifest.json").read_text(encoding="utf-8"))
    manifest["checksum"] = compute_component_checksum({
        name: (comp / name).read_bytes()
        for name in ("role/role.yaml", "role/doctrine.md", "config-schema.json")})
    (comp / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return comp


@pytest.mark.parametrize("approved_secrets, sent_secrets", [
    (["S"], frozenset()), ([], frozenset({"S"}))], ids=["approved-not-sent", "sent-not-approved"])
def test_secret_names_are_part_of_the_approved_values(tmp_path, monkeypatch, approved_secrets,
                                                      sent_secrets):
    inspection, receipt = _inspect(
        tmp_path, monkeypatch, _with_secret(_component(tmp_path, "c", required=["A"])))
    values = {"config": {"A": "1"}, "secrets": approved_secrets}
    acks = _approve(tmp_path, inspection, values)
    with pytest.raises(SpecialistInstallError) as raised:
        _install(tmp_path, inspection, receipt, acks, config={"A": "1"}, approved=values,
                 secrets=sent_secrets)
    assert raised.value.kind == "approval_values_differ" and "secrets" in raised.value.detail
    assert _tree(tmp_path) == {} and _journals(tmp_path) == []
    instance = _install(tmp_path, inspection, receipt, acks, config={"A": "1"},
                        approved=values, secrets=frozenset(approved_secrets))
    assert instance.state == ("active" if approved_secrets else "pending-configuration")


def test_a_legacy_ack_without_its_values_approves_nothing(tmp_path, monkeypatch):
    inspection, receipt = _inspect(tmp_path, monkeypatch,
                                   _component(tmp_path, "c", required=[]))
    legacy_fields = {"component_id": inspection.component_id, "version": inspection.version,
                     "component_checksum": inspection.root_digest, "slug": inspection.slug,
                     "receipt_digest": inspection.receipt_digest, "ts": 1}
    legacy_key = checksum_json({k: legacy_fields[k] for k in (
        "component_id", "version", "component_checksum", "slug", "receipt_digest")})
    (tmp_path / "acks.json").write_text(json.dumps(
        {"schema_version": 1, "acks": {legacy_key: legacy_fields}}), encoding="utf-8")
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    with pytest.raises(SpecialistInstallError) as raised:
        _install(tmp_path, inspection, receipt, acks, config={},
                 approved={"config": {}, "secrets": []})
    assert raised.value.kind == "consent_missing"
    assert _tree(tmp_path) == {}


def test_a_changed_root_digest_with_equal_values_is_another_identity(tmp_path, monkeypatch):
    inspection, _ = _inspect(tmp_path, monkeypatch, _component(tmp_path, "c", required=["A"]))
    values = {"config": {"A": "1"}, "secrets": []}
    other_root = dataclasses.replace(inspection, root_digest="sha256:" + "9" * 64)
    other_receipt = dataclasses.replace(inspection, receipt_digest="sha256:" + "8" * 64)
    other_version = dataclasses.replace(inspection, version="0.2.0")
    ids = {identity_for_inspection(i, values=values)
           for i in (inspection, other_root, other_receipt, other_version)}
    assert len(ids) == 4


# -- upgrade --------------------------------------------------------------------------

def _installed_v1(tmp_path, monkeypatch, *, config):
    comp = _component(tmp_path, "v1", required=sorted(config))
    inspection, receipt = _inspect(tmp_path, monkeypatch, comp)
    values = {"config": dict(config), "secrets": []}
    _install(tmp_path, inspection, receipt, _approve(tmp_path, inspection, values),
             config=config, approved=values)


def _upgrade(tmp_path, inspection, receipt, acks, *, config, approved):
    out = specialist_install.upgrade_specialist(
        slug="mtg", inspection=inspection, receipt=receipt, config=config,
        secret_names_provided=frozenset(), acks=acks, approved_values=approved,
        **_paths(tmp_path))
    if receipt is not None:
        instance, txn = out
        specialist_bundle_journal.complete(txn.journal_path)
        return instance
    return out


@pytest.mark.parametrize("with_receipt", [True, False], ids=["bundle", "direct"])
def test_an_upgrade_stages_the_inherited_settings_it_was_approved_with(
        tmp_path, monkeypatch, with_receipt):
    _installed_v1(tmp_path, monkeypatch, config={"A": "1"})
    v2, receipt = _inspect(tmp_path, monkeypatch,
                           _component(tmp_path, "v2", required=["A"], version="0.2.0"),
                           upgrade=True, sha="b" * 40)
    receipt = receipt if with_receipt else None
    active_before = (tmp_path / "specialists" / "mtg" / "active.yaml").read_bytes()
    tree, journals = _tree(tmp_path), _journals(tmp_path)

    # the call supplies nothing; the upgrade keeps A from the active install
    naive = {"config": {}, "secrets": []}
    begun: list = []
    real_begin = specialist_bundle_journal.begin

    def _begin(*a, **k):
        begun.append(a)
        return real_begin(*a, **k)

    with monkeypatch.context() as mp:
        mp.setattr(specialist_bundle_journal, "begin", _begin)
        with pytest.raises(SpecialistInstallError) as raised:
            _upgrade(tmp_path, v2, receipt, _approve(tmp_path, v2, naive), config={},
                     approved=naive)
    assert raised.value.kind == "approval_values_differ" and "A" in raised.value.detail
    assert begun == []                          # refused before any journal existed
    assert (tmp_path / "specialists" / "mtg" / "active.yaml").read_bytes() == active_before
    assert _tree(tmp_path) == tree and _journals(tmp_path) == journals

    effective = specialist_install.preview_effective_config(
        "specialist_upgrade", v2, slug="mtg", config={}, secret_names_provided=[],
        specialists_dir=tmp_path / "specialists")
    assert effective == {"A": "1"}
    values = {"config": effective, "secrets": []}
    instance = _upgrade(tmp_path, v2, receipt, _approve(tmp_path, v2, values), config={},
                        approved=values)
    assert instance.state == "active"
    active = InstanceDir(tmp_path / "specialists" / "mtg").active()
    assert dict(active.config_snapshot) == {"A": "1"}
    assert specialist_install.parse_component_root(active.root)[1] == "0.2.0"


def test_an_approval_for_one_version_does_not_approve_another_with_equal_values(
        tmp_path, monkeypatch):
    _installed_v1(tmp_path, monkeypatch, config={"A": "1"})
    v2, receipt = _inspect(tmp_path, monkeypatch,
                           _component(tmp_path, "v2", required=["A"], version="0.2.0"),
                           upgrade=True, sha="b" * 40)
    values = {"config": {"A": "1"}, "secrets": []}
    v1_like = dataclasses.replace(v2, version="0.1.0")
    acks = _approve(tmp_path, v1_like, values)
    with pytest.raises(SpecialistInstallError) as raised:
        _upgrade(tmp_path, v2, receipt, acks, config={}, approved=values)
    assert raised.value.kind == "consent_missing"


def test_the_preview_refuses_what_the_library_would_refuse(tmp_path, monkeypatch):
    inspection, _ = _inspect(tmp_path, monkeypatch, _component(tmp_path, "c", required=["A"]))
    with pytest.raises(SpecialistInstallError) as raised:
        specialist_install.preview_effective_config(
            "specialist_install", inspection, slug="mtg", config={"A": "1"},
            secret_names_provided=["S"], specialists_dir=tmp_path / "specialists")
    assert raised.value.kind == "unknown_secret_name"
    with pytest.raises(SpecialistInstallError) as raised:
        specialist_install.preview_effective_config(
            "specialist_upgrade", inspection, slug="mtg", config={},
            secret_names_provided=[], specialists_dir=tmp_path / "specialists")
    assert raised.value.kind == "no_active_tuple"
    with pytest.raises(ValueError):
        specialist_install.preview_effective_config(
            "persona_install", inspection, slug="mtg", config={}, secret_names_provided=[],
            specialists_dir=tmp_path / "specialists")


# -- the pure merges --------------------------------------------------------------------

def test_the_install_merge_carries_only_known_plain_pending_settings():
    schema = {"required": ["A", "S"], "secret_names": ["S"]}
    assert specialist_install.install_effective_config(
        config={"B": "x"}, schema=schema,
        pending_snapshot={"A": "1", "S": "plain", "Z": "z"}) == {"A": "1", "B": "x"}
    assert specialist_install.install_effective_config(
        config={"A": "2"}, schema=schema, pending_snapshot={"A": "1"}) == {"A": "2"}
    assert specialist_install.install_effective_config(
        config={"A": "2"}, schema=schema, pending_snapshot=None) == {"A": "2"}


def test_the_upgrade_merge_orders_active_pending_call_and_filters_to_the_schema():
    schema = {"required": ["A", "B", "C", "S"], "secret_names": ["S"]}
    got = specialist_install.upgrade_effective_config(
        config={"C": "call"}, schema=schema,
        active_snapshot={"A": "active", "B": "active", "OLD": "gone", "P": "prior-secret"},
        prior_secret_names={"P"},
        pending_snapshot={"B": "pending", "S": "plain"})
    assert got == {"A": "active", "B": "pending", "C": "call"}
    # an unreadable prior schema carries nothing from the active install
    assert specialist_install.upgrade_effective_config(
        config={}, schema=schema, active_snapshot={"A": "active"}, prior_secret_names=None,
        pending_snapshot=None) == {}


# -- persona ------------------------------------------------------------------------------

def _persona(tmp_path: Path) -> PersonaInspectionResult:
    from persona_pack import load_persona_pack

    root = tmp_path / "persona-staged"
    pack_dir = root / "pack"
    pack_dir.mkdir(parents=True)
    persona_yaml = {
        "api_version": "casa.persona/v1", "id": "casa/newton", "version": "0.1.0",
        "trait_schema_version": 1,
        "identity": {"display_name": "Newton", "pronouns": {
            "subject": "he", "object": "him", "possessive_adjective": "his",
            "possessive_pronoun": "his", "reflexive": "himself"}},
        "relationship_posture": "established", "archetype": "tutor",
        "traits": {"warmth": 3, "formality": 3, "candor": 4, "attunement": 3,
                   "curiosity": 5, "levity": 2, "social_energy": 3, "optimism": 3},
        "quirks": [],
    }
    (pack_dir / "persona.yaml").write_text(yaml.safe_dump(persona_yaml, sort_keys=False),
                                           encoding="utf-8")
    (pack_dir / "persona.md").write_text(
        f"# Core\n\n{'Y' * 350}\n\n## Negative space\n\nNever condescends.\n", encoding="utf-8")
    rows = []
    for name in sorted(os.listdir(pack_dir)):
        text = canonical_text((pack_dir / name).read_text(encoding="utf-8"))
        rows.append({"path": name, "type": "file", "executable": False,
                     "checksum": checksum_bytes(text.encode("utf-8"))})
    payload = {"api_version": "casa.persona.manifest/v1", "files": rows}
    payload["checksum"] = checksum_bytes(canonical_json_bytes(payload))
    (root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    pack = load_persona_pack(pack_dir, root / "manifest.json")
    return PersonaInspectionResult(persona_id=pack.persona_id, version=pack.version,
                                   checksum=pack.checksum, display_name="Newton",
                                   staged_dir=root)


def _persona_identity(i: PersonaInspectionResult, values: dict) -> str:
    return persona_install_consent_identity(persona_id=i.persona_id, version=i.version,
                                            checksum=i.checksum,
                                            values_digest=values_digest(values))


def test_a_persona_install_commits_only_under_an_approval_of_its_values(tmp_path):
    inspection = _persona(tmp_path)
    path = tmp_path / "persona-acks.json"
    PersonaInstallAckStore(path=path).record(
        identity=_persona_identity(inspection, {"x": 1}), persona_id=inspection.persona_id,
        version=inspection.version, checksum=inspection.checksum,
        values_digest=values_digest({"x": 1}))
    acks = PersonaInstallAckStore(path=path)                 # reopened from disk
    with pytest.raises(SpecialistInstallError) as raised:
        commit_persona_install(inspection=inspection, acks=acks, approved_values={},
                               personas_root=tmp_path / "personas")
    assert raised.value.kind == "consent_missing"
    assert not (tmp_path / "personas").exists()
    pack = commit_persona_install(inspection=inspection, acks=acks, approved_values={"x": 1},
                                  personas_root=tmp_path / "personas")
    assert pack.checksum == inspection.checksum
    listed = list_installed_personas(personas_root=tmp_path / "personas", references={},
                                     acks=acks)
    assert [(e["ref"], e["acked"]) for e in listed] == [("casa/newton@0.1.0", True)]


def test_the_persona_identity_needs_its_values_digest():
    with pytest.raises(TypeError):
        persona_install_consent_identity(persona_id="casa/newton", version="0.1.0",  # type: ignore[call-arg]
                                         checksum="sha256:" + "a" * 64)
    with pytest.raises(ValueError):
        persona_install_consent_identity(persona_id="casa/newton", version="0.1.0",
                                         checksum="sha256:" + "a" * 64, values_digest="")


def test_a_legacy_persona_ack_is_refused_for_mutation_not_dropped(tmp_path):
    path = tmp_path / "persona-acks.json"
    legacy = {"persona_id": "casa/newton", "version": "0.1.0",
              "checksum": "sha256:" + "a" * 64, "ts": 1}
    legacy_key = checksum_json({k: legacy[k] for k in ("persona_id", "version", "checksum")})
    doc = {"schema_version": 1, "acks": {legacy_key: legacy}}
    path.write_text(json.dumps(doc), encoding="utf-8")
    acks = PersonaInstallAckStore(path=path)
    assert acks.is_acked(legacy_key) is False
    vd = values_digest({})
    with pytest.raises(PersonaLedgerInvalid):
        acks.record(identity=persona_install_consent_identity(
            persona_id="casa/other", version="0.1.0", checksum="sha256:" + "b" * 64,
            values_digest=vd), persona_id="casa/other", version="0.1.0",
            checksum="sha256:" + "b" * 64, values_digest=vd)
    with pytest.raises(PersonaLedgerInvalid):
        acks.revoke(persona_id="casa/newton")
    assert json.loads(path.read_text(encoding="utf-8")) == doc    # the entry is still there


def test_a_persona_ack_persists_its_values_digest_and_the_ledger_stays_exact(tmp_path):
    path = tmp_path / "persona-acks.json"
    acks = PersonaInstallAckStore(path=path)
    vd = values_digest({})
    ident = persona_install_consent_identity(persona_id="casa/newton", version="0.1.0",
                                             checksum="sha256:" + "a" * 64, values_digest=vd)
    assert acks.record(identity=ident, persona_id="casa/newton", version="0.1.0",
                       checksum="sha256:" + "a" * 64, values_digest=vd) is True
    assert json.loads(path.read_text(encoding="utf-8"))["acks"][ident]["values_digest"] == vd
    # a second record goes through the exact validator over the first
    ident2 = persona_install_consent_identity(persona_id="casa/newton", version="0.2.0",
                                              checksum="sha256:" + "c" * 64, values_digest=vd)
    assert acks.record(identity=ident2, persona_id="casa/newton", version="0.2.0",
                       checksum="sha256:" + "c" * 64, values_digest=vd) is True
    assert acks.is_acked(ident) and acks.is_acked(ident2)
    assert [r["version"] for r in acks.revoke(persona_id="casa/newton", version="0.1.0")] == \
        ["0.1.0"]
    assert not acks.is_acked(ident) and acks.is_acked(ident2)


def test_the_lifecycle_entry_points_still_run_whole_under_the_lifecycle_lock(
        tmp_path, monkeypatch):
    import threading

    import specialist_materialize

    held: list[bool] = []
    real = threading.Lock()

    class _Recording:
        def __enter__(self):
            real.acquire()
            held.append(True)
            return self

        def __exit__(self, *exc):
            real.release()
            return False

    monkeypatch.setattr(specialist_materialize, "SPECIALIST_LIFECYCLE_LOCK", _Recording())
    inspection, receipt = _inspect(tmp_path, monkeypatch, _component(tmp_path, "c", required=[]))
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    for call in (specialist_install.commit_specialist_install,
                 lambda **kw: specialist_install.upgrade_specialist(slug="mtg", **kw)):
        held.clear()
        with pytest.raises(SpecialistInstallError):
            call(inspection=inspection, receipt=receipt, config={},
                 secret_names_provided=frozenset(), acks=acks,
                 approved_values={"config": {}, "secrets": []}, **_paths(tmp_path))
        assert held == [True]
    held.clear()
    specialist_install.install_effective_config(config={}, schema={}, pending_snapshot=None)
    assert held == []
