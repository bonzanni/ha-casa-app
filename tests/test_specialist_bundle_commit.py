"""Task 10 — journaled bundle transaction: owned-set sidecar, atomic owned
registry swap, and the commit/upgrade/rollback/uninstall integration.

Checkpoint 2a covers the two self-contained primitives (sidecar triple +
apply_owned_swap); the lifecycle-integration slices (2b-2d) follow."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import plugin_registry
from plugin_registry import apply_owned_swap, compute_artifact_id, scoped_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _owned_entry(slug: str, manifest_name: str, *, repo: str = "acme/mtg",
                 revision: str = "git:" + "a" * 40, subdir: str = "plugins/mtg",
                 version: str = "1.0.0") -> dict:
    name = scoped_name(slug, manifest_name)
    return {
        "name": name,
        "owner": f"specialist:{slug}",
        "manifest_name": manifest_name,
        "targets": [f"specialist:{slug}"],
        "version": version,
        "source": {"type": "github", "repo": repo,
                   "ref": "v1", "revision": revision, "subdir": subdir},
        "artifact_id": compute_artifact_id(
            repo=repo, revision=revision, subdir=subdir, name=name),
    }


def _unowned_entry(name: str = "weather", *, repo: str = "acme/weather") -> dict:
    revision = "git:" + "b" * 40
    return {
        "name": name,
        "targets": ["resident:assistant"],
        "version": "2.0.0",
        "source": {"type": "github", "repo": repo,
                   "ref": "v2", "revision": revision, "subdir": ""},
        "artifact_id": compute_artifact_id(
            repo=repo, revision=revision, subdir="", name=name),
    }


def _write_registry(path: Path, entries: list[dict]) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1, "seeded_defaults": [], "plugins": entries,
    }), encoding="utf-8")


# ---------------------------------------------------------------------------
# 2a — apply_owned_swap
# ---------------------------------------------------------------------------

def test_apply_owned_swap_install_adds_owned_entries(tmp_path: Path) -> None:
    reg = tmp_path / "registry.json"
    _write_registry(reg, [_unowned_entry()])
    entry = _owned_entry("mtg", "mtg")

    before, data = apply_owned_swap(slug="mtg", new_entries=[entry], registry_path=reg)

    assert before == []                 # nothing owned before
    names = {e["name"] for e in plugin_registry.load_registry(reg).entries}
    assert names == {"weather", "mtg.mtg"}
    # owner + manifest_name + targets survived validation
    owned = plugin_registry.owned_entries_for("mtg", plugin_registry.load_registry(reg))
    assert len(owned) == 1
    assert owned[0]["manifest_name"] == "mtg"
    assert owned[0]["targets"] == ["specialist:mtg"]


def test_apply_owned_swap_replaces_prior_owned_set_and_returns_before(tmp_path: Path) -> None:
    reg = tmp_path / "registry.json"
    old = _owned_entry("mtg", "mtg")
    _write_registry(reg, [_unowned_entry(), old])
    new = _owned_entry("mtg", "mtg", version="2.0.0", revision="git:" + "c" * 40)

    before, _ = apply_owned_swap(slug="mtg", new_entries=[new], registry_path=reg)

    assert [e["name"] for e in before] == ["mtg.mtg"]
    assert before[0]["version"] == "1.0.0"
    owned = plugin_registry.owned_entries_for("mtg", plugin_registry.load_registry(reg))
    assert len(owned) == 1 and owned[0]["version"] == "2.0.0"


def test_apply_owned_swap_uninstall_removes_owned_only(tmp_path: Path) -> None:
    reg = tmp_path / "registry.json"
    _write_registry(reg, [_unowned_entry(), _owned_entry("mtg", "mtg")])

    before, _ = apply_owned_swap(slug="mtg", new_entries=[], registry_path=reg)

    assert [e["name"] for e in before] == ["mtg.mtg"]
    names = {e["name"] for e in plugin_registry.load_registry(reg).entries}
    assert names == {"weather"}         # unowned survivor untouched


def test_apply_owned_swap_leaves_other_specialists_entries_alone(tmp_path: Path) -> None:
    reg = tmp_path / "registry.json"
    _write_registry(reg, [_owned_entry("mtg", "mtg"), _owned_entry("finance", "ledger")])

    apply_owned_swap(slug="mtg", new_entries=[], registry_path=reg)

    names = {e["name"] for e in plugin_registry.load_registry(reg).entries}
    assert names == {"finance.ledger"}


def test_apply_owned_swap_refuses_a_malformed_new_entry(tmp_path: Path) -> None:
    reg = tmp_path / "registry.json"
    _write_registry(reg, [])
    bad = _owned_entry("mtg", "mtg")
    bad["artifact_id"] = "deadbeef"     # identity mismatch -> entry_invalid

    with pytest.raises(ValueError, match="owned_swap_invalid"):
        apply_owned_swap(slug="mtg", new_entries=[bad], registry_path=reg)
    # registry file untouched (never saved on refusal)
    assert plugin_registry.load_registry(reg).entries == []


def test_apply_owned_swap_refuses_a_manifest_name_collision(tmp_path: Path) -> None:
    reg = tmp_path / "registry.json"
    _write_registry(reg, [])
    a = _owned_entry("mtg", "mtg")
    b = _owned_entry("mtg", "mtg", revision="git:" + "d" * 40)  # same scoped name
    with pytest.raises(ValueError, match="owned_swap_invalid"):
        apply_owned_swap(slug="mtg", new_entries=[a, b], registry_path=reg)


# ---------------------------------------------------------------------------
# 2a — owned-plugins sidecar triple
# ---------------------------------------------------------------------------

def _doc(plugins: list[dict] | None = None) -> dict:
    return {
        "schema_version": 1,
        "component_source": {"repo": "acme/mtg-specialist", "ref": "v0.2.0",
                             "revision": "git:" + "a" * 40, "subdir": ""},
        "plugins": plugins if plugins is not None else [
            {"name": "mtg.mtg", "manifest_name": "mtg", "version": "1.0.0",
             "artifact_id": "a" * 64, "digest": "sha256:" + "y" * 64,
             "source": {"type": "github", "repo": "acme/mtg-specialist",
                        "ref": "v0.2.0", "revision": "git:" + "a" * 40,
                        "subdir": "plugins/mtg"}},
        ],
    }


def test_owned_plugins_sidecar_roundtrip(tmp_path: Path) -> None:
    from personality_binding import (
        owned_plugins_path, read_owned_plugins, write_owned_plugins,
    )
    p = owned_plugins_path(tmp_path)
    assert read_owned_plugins(p) is None
    write_owned_plugins(p, _doc())
    loaded = read_owned_plugins(p)
    assert loaded is not None
    assert loaded["component_source"]["repo"] == "acme/mtg-specialist"
    assert loaded["plugins"][0]["name"] == "mtg.mtg"


def test_owned_plugins_supports_plugin_less_component(tmp_path: Path) -> None:
    from personality_binding import owned_plugins_path, read_owned_plugins, write_owned_plugins
    p = owned_plugins_path(tmp_path)
    write_owned_plugins(p, _doc(plugins=[]))
    loaded = read_owned_plugins(p)
    assert loaded["plugins"] == []
    assert loaded["component_source"]["repo"]      # provenance still present


def test_commit_owned_plugins_rotates_desired_to_active_and_prior(tmp_path: Path) -> None:
    from personality_binding import (
        InstanceDir, owned_plugins_path, owned_plugins_prior_path,
        owned_plugins_desired_path, read_owned_plugins, write_owned_plugins,
    )
    d = InstanceDir(tmp_path)
    # generation 1 already active
    write_owned_plugins(owned_plugins_path(tmp_path), _doc())
    gen1 = read_owned_plugins(owned_plugins_path(tmp_path))
    # stage generation 2 as desired
    gen2 = _doc(plugins=[])
    d.stage_desired_owned_plugins(gen2)
    assert read_owned_plugins(owned_plugins_desired_path(tmp_path)) == gen2

    d.commit_owned_plugins_desired_to_active()

    assert read_owned_plugins(owned_plugins_path(tmp_path)) == gen2       # new active
    assert read_owned_plugins(owned_plugins_prior_path(tmp_path)) == gen1  # old -> prior
    assert not owned_plugins_desired_path(tmp_path).exists()               # consumed


def test_commit_owned_plugins_is_noop_without_a_staged_desired(tmp_path: Path) -> None:
    from personality_binding import InstanceDir, owned_plugins_path, read_owned_plugins, write_owned_plugins
    d = InstanceDir(tmp_path)
    write_owned_plugins(owned_plugins_path(tmp_path), _doc())
    d.commit_owned_plugins_desired_to_active()     # no desired staged
    assert read_owned_plugins(owned_plugins_path(tmp_path)) is not None


# ===========================================================================
# 2b — journaled bundle install transaction (pure-python against
# commit_specialist_install with a real inspect-built receipt)
# ===========================================================================

import json as _json
import shutil as _shutil

import specialist_install
import specialist_receipt

try:
    from tests.specialist_fixtures import write_bundled_plugin, write_minimal_component
except ImportError:
    from specialist_fixtures import write_bundled_plugin, write_minimal_component


@pytest.fixture(autouse=True)
def _fresh_registry_snapshot(tmp_path):
    """Point the process-global plugin_registry snapshot at a fresh tmp
    registry (mirrors tests/test_specialist_bundled_inspect.py)."""
    plugin_registry.reload_snapshot(registry_path=tmp_path / "snap-registry.json",
                                    store_root=tmp_path / "snap-store")
    yield


def _subdir_stub(component_root: Path, sha: str = "a" * 40):
    """A resolve_and_fetch stub that respects `subdir` — copies
    `component_root/subdir` (or the whole tree when subdir is empty) into
    `dest`, exactly like a real fetch of `repo@ref:subdir`. `sha` distinguishes
    generations (a real fetch would resolve a different commit per ref)."""
    def _stub(repo, ref, subdir, dest, *, expected_revision=None):
        src = component_root / subdir if subdir else component_root
        _shutil.copytree(src, dest)
        return sha
    return _stub


class _Ctx:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _prep(tmp_path: Path, monkeypatch, *, with_plugin: bool = True,
          slug: str = "mtg", ack: bool = True):
    """Build a component (optionally with a bundled `mtg` plugin), inspect it
    (stubbed fetch), load its receipt, and record consent. Returns a context
    with everything commit_specialist_install needs."""
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity
    from specialist_registry import InstalledSpecialistIndex

    comp, mpath = write_minimal_component(tmp_path, slug=slug)
    if with_plugin:
        digest = write_bundled_plugin(comp, "mtg")
        manifest = _json.loads(mpath.read_text(encoding="utf-8"))
        manifest["dependencies"].append({
            "kind": "plugin/implementation", "identifier": "mtg", "digest": digest,
            "source": {"type": "bundled", "path": "plugins/mtg"},
        })
        mpath.write_text(_json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _subdir_stub(comp))
    idx = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "installed-index"))
    idx.load()
    inspection = specialist_install.inspect_specialist_repo(
        "org/repo", "main", staging_root=tmp_path / "staging",
        installed_index=idx, receipts_dir=tmp_path / "receipts")
    receipt = specialist_receipt.load(inspection.receipt_id, receipts_dir=tmp_path / "receipts")
    assert receipt is not None

    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    if ack:
        identity = install_consent_identity(
            component_id=inspection.component_id, version=inspection.version,
            root_digest=inspection.root_digest, slug=inspection.slug,
            receipt_digest=inspection.receipt_digest)
        acks.record(identity=identity, component_id=inspection.component_id,
                    version=inspection.version, component_checksum=inspection.root_digest,
                    slug=inspection.slug, receipt_digest=inspection.receipt_digest)

    return _Ctx(
        comp=comp, inspection=inspection, receipt=receipt, acks=acks, slug=slug,
        kw=dict(
            inspection=inspection, receipt=receipt, config={},
            secret_names_provided=frozenset(), acks=acks,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents",
            registry_path=tmp_path / "registry.json",
            plugin_store_root=tmp_path / "store",
            ops_dir=tmp_path / "ops"),
    )


def _owned(reg_path: Path, slug: str) -> list[dict]:
    data = plugin_registry.load_registry(reg_path)
    return plugin_registry.owned_entries_for(slug, data)


def test_bundle_install_happy_path(tmp_path: Path, monkeypatch) -> None:
    from personality_binding import owned_plugins_path, read_owned_plugins

    ctx = _prep(tmp_path, monkeypatch)
    instance, txn = specialist_install.commit_specialist_install(**ctx.kw)

    assert instance.state == "active"
    # owned entry appears with owner + manifest_name + scoped name + target
    owned = _owned(ctx.kw["registry_path"], "mtg")
    assert len(owned) == 1
    e = owned[0]
    assert e["name"] == "mtg.mtg" and e["manifest_name"] == "mtg"
    assert e["owner"] == "specialist:mtg" and e["targets"] == ["specialist:mtg"]
    # artifact published to the store under the scoped name
    assert (tmp_path / "store" / "mtg.mtg" / e["artifact_id"]).is_dir()
    # sidecar written (active generation) with the owned plugin + provenance
    sidecar = read_owned_plugins(owned_plugins_path(tmp_path / "specialists" / "mtg"))
    assert sidecar["plugins"][0]["name"] == "mtg.mtg"
    assert sidecar["component_source"]["repo"] == "org/repo"
    # sync phase leaves the journal in-progress+committed; the TOOL layer
    # completes it after the sequencer (2e). txn carries its path + artifacts.
    assert Path(txn.journal_path).is_file()
    payload = _json.loads(Path(txn.journal_path).read_text())
    assert payload["state"] == "in-progress" and "committed" in payload["steps_done"]
    assert txn.new_artifact_ids == (e["artifact_id"],)
    assert txn.removed_artifact_ids == ()


def test_bundle_install_refuses_without_consent(tmp_path: Path, monkeypatch) -> None:
    ctx = _prep(tmp_path, monkeypatch, ack=False)
    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        specialist_install.commit_specialist_install(**ctx.kw)
    assert ei.value.kind == "consent_missing"
    assert _owned(ctx.kw["registry_path"], "mtg") == []


def test_bundle_install_receipt_drift_on_mutated_tree(tmp_path: Path, monkeypatch) -> None:
    ctx = _prep(tmp_path, monkeypatch)
    # Tamper the staged plugin tree AND make recovery reproduce the drift.
    staged_plugin = ctx.inspection.staged_dir / "plugins" / "mtg"
    (staged_plugin / "tampered.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(specialist_install, "resolve_and_fetch",
                        _subdir_stub(ctx.inspection.staged_dir))
    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        specialist_install.commit_specialist_install(**ctx.kw)
    assert ei.value.kind == "receipt_drift"
    assert _owned(ctx.kw["registry_path"], "mtg") == []       # registry untouched


def test_bundle_install_published_vs_attested_tamper(tmp_path: Path, monkeypatch) -> None:
    import plugin_store
    ctx = _prep(tmp_path, monkeypatch)
    monkeypatch.setattr(plugin_store, "read_metadata",
                        lambda root: {"content_checksum": "deadbeef"})
    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        specialist_install.commit_specialist_install(**ctx.kw)
    assert ei.value.kind == "receipt_drift"
    assert _owned(ctx.kw["registry_path"], "mtg") == []       # rolled back
    assert list((tmp_path / "ops").glob("*.json")) == []      # journal completed


def test_bundle_install_post_swap_failure_rolls_back(tmp_path: Path, monkeypatch) -> None:
    import personality_binding
    ctx = _prep(tmp_path, monkeypatch)
    orig = personality_binding.InstanceDir.commit_desired_to_active

    def _boom(self):
        raise RuntimeError("tuple commit exploded")

    monkeypatch.setattr(personality_binding.InstanceDir, "commit_desired_to_active", _boom)
    with pytest.raises(RuntimeError):
        specialist_install.commit_specialist_install(**ctx.kw)
    # registry restored (owned entry removed) + journal pruned
    assert _owned(ctx.kw["registry_path"], "mtg") == []
    assert list((tmp_path / "ops").glob("*.json")) == []
    monkeypatch.setattr(personality_binding.InstanceDir, "commit_desired_to_active", orig)


def test_bundle_install_pending_config_keeps_owned_entries(tmp_path: Path, monkeypatch) -> None:
    from personality_binding import owned_plugins_desired_path, read_owned_plugins
    import specialist_lifecycle
    ctx = _prep(tmp_path, monkeypatch)
    monkeypatch.setattr(specialist_lifecycle, "satisfy_config",
                        lambda **kw: (False, ["API_KEY"]))
    instance, txn = specialist_install.commit_specialist_install(**ctx.kw)

    assert instance.state == "pending-configuration"
    # owned entries STILL registered (activate at commit regardless)
    assert len(_owned(ctx.kw["registry_path"], "mtg")) == 1
    # sidecar staged as DESIRED (picked up on a later activation rotation)
    desired = read_owned_plugins(owned_plugins_desired_path(tmp_path / "specialists" / "mtg"))
    assert desired is not None and desired["plugins"][0]["name"] == "mtg.mtg"
    # sync phase leaves the journal in-progress+committed (tool completes it)
    assert Path(txn.journal_path).is_file()


def test_bundle_install_plugin_less_component(tmp_path: Path, monkeypatch) -> None:
    from personality_binding import owned_plugins_path, read_owned_plugins
    ctx = _prep(tmp_path, monkeypatch, with_plugin=False)
    assert ctx.receipt.plugins == ()
    instance, txn = specialist_install.commit_specialist_install(**ctx.kw)

    assert instance.state == "active"
    assert _owned(ctx.kw["registry_path"], "mtg") == []       # no owned plugins
    # sidecar STILL written: plugins:[] with a real component_source
    sidecar = read_owned_plugins(owned_plugins_path(tmp_path / "specialists" / "mtg"))
    assert sidecar["plugins"] == []
    assert sidecar["component_source"]["repo"] == "org/repo"


def test_bundle_install_recovers_vanished_staging(tmp_path: Path, monkeypatch) -> None:
    ctx = _prep(tmp_path, monkeypatch)
    # Wipe ALL staging post-consent; commit must recover from the receipt.
    _shutil.rmtree(ctx.inspection.staged_dir, ignore_errors=True)
    # recovery fetch reproduces the ORIGINAL (clean) component bytes
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _subdir_stub(ctx.comp))
    instance, txn = specialist_install.commit_specialist_install(**ctx.kw)

    assert instance.state == "active"
    owned = _owned(ctx.kw["registry_path"], "mtg")
    assert len(owned) == 1 and owned[0]["name"] == "mtg.mtg"


def test_bundle_crash_reconcile_restores(tmp_path: Path, monkeypatch) -> None:
    """A half-applied bundle op (owned entry swapped in, journal still
    in-progress) is rolled back by reconcile_boot."""
    import specialist_bundle_journal
    ctx = _prep(tmp_path, monkeypatch)
    reg = ctx.kw["registry_path"]
    # simulate the swap having happened with NO prior owned set...
    entry = _owned_entry("mtg", "mtg")
    plugin_registry.apply_owned_swap(slug="mtg", new_entries=[entry], registry_path=reg)
    assert len(_owned(reg, "mtg")) == 1
    # ...and an in-progress journal capturing the empty before-state.
    specialist_bundle_journal.begin(
        "install", "mtg", before_entries=[], before_tuple_files={},
        ack_records=[], ops_dir=tmp_path / "ops2")

    actions = specialist_bundle_journal.reconcile_boot(
        ops_dir=tmp_path / "ops2", registry_path=reg,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")

    assert actions == [{"slug": "mtg", "action": "rolled_back"}]
    assert _owned(reg, "mtg") == []                            # owned entry removed


# ===========================================================================
# 2c — bundle upgrade / rollback transactions
# ===========================================================================

def _prep_v2(tmp_path: Path, monkeypatch, base_ctx, *, ref: str = "v2",
             marker: str = "v2") -> dict:
    """Build a v2 mtg component (changed plugin content + bumped version, SAME
    slug + tmp paths), inspect it in upgrade mode, record consent, and return
    the kwargs for upgrade_specialist(receipt=...)."""
    import plugin_store
    from specialist_install_consent import install_consent_identity
    from specialist_registry import InstalledSpecialistIndex

    comp2, mpath2 = write_minimal_component(tmp_path / marker, slug="mtg")
    write_bundled_plugin(comp2, "mtg")
    (comp2 / "plugins" / "mtg" / "README.md").write_text(marker, encoding="utf-8")
    digest = "sha256:" + plugin_store.content_checksum(comp2 / "plugins" / "mtg")
    manifest = _json.loads(mpath2.read_text(encoding="utf-8"))
    manifest["version"] = "0.2.0"
    manifest["dependencies"].append({
        "kind": "plugin/implementation", "identifier": "mtg", "digest": digest,
        "source": {"type": "bundled", "path": "plugins/mtg"}})
    mpath2.write_text(_json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _subdir_stub(comp2, "b" * 40))
    idx = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "installed-index"))
    idx.load()
    insp2 = specialist_install.inspect_specialist_repo(
        "org/repo", ref, staging_root=tmp_path / "staging2", installed_index=idx,
        mode="upgrade", target_slug="mtg", specialists_dir=tmp_path / "specialists",
        receipts_dir=tmp_path / "receipts")
    receipt2 = specialist_receipt.load(insp2.receipt_id, receipts_dir=tmp_path / "receipts")
    acks = base_ctx.acks
    identity = install_consent_identity(
        component_id=insp2.component_id, version=insp2.version,
        root_digest=insp2.root_digest, slug="mtg", receipt_digest=insp2.receipt_digest)
    acks.record(identity=identity, component_id=insp2.component_id, version=insp2.version,
                component_checksum=insp2.root_digest, slug="mtg",
                receipt_digest=insp2.receipt_digest)
    return dict(
        slug="mtg", inspection=insp2, receipt=receipt2, config={},
        secret_names_provided=frozenset(), acks=acks,
        specialists_dir=tmp_path / "specialists", agents_specialists_dir=tmp_path / "agents",
        registry_path=tmp_path / "registry.json", plugin_store_root=tmp_path / "store",
        ops_dir=tmp_path / "ops")


def test_bundle_upgrade_replaces_owned_set_in_one_swap(tmp_path: Path, monkeypatch) -> None:
    ctx = _prep(tmp_path, monkeypatch)
    specialist_install.commit_specialist_install(**ctx.kw)
    reg = ctx.kw["registry_path"]
    v1_aid = _owned(reg, "mtg")[0]["artifact_id"]

    kw2 = _prep_v2(tmp_path, monkeypatch, ctx)
    instance, txn = specialist_install.upgrade_specialist(**kw2)

    assert instance.state == "active"
    owned = _owned(reg, "mtg")
    assert len(owned) == 1                        # one entry, swapped atomically
    assert owned[0]["artifact_id"] != v1_aid      # new artifact
    assert owned[0]["version"] == "0.1.0"         # plugin manifest version (unchanged)
    assert v1_aid in txn.removed_artifact_ids     # old artifact invalidation-driving
    assert owned[0]["artifact_id"] in txn.new_artifact_ids


def test_bundle_upgrade_failing_preflight_leaves_old_generation(tmp_path: Path, monkeypatch) -> None:
    ctx = _prep(tmp_path, monkeypatch)
    specialist_install.commit_specialist_install(**ctx.kw)
    reg = ctx.kw["registry_path"]
    v1_aid = _owned(reg, "mtg")[0]["artifact_id"]

    kw2 = _prep_v2(tmp_path, monkeypatch, ctx)
    # tamper the v2 staged plugin tree AND make recovery reproduce the drift
    staged_plugin = kw2["inspection"].staged_dir / "plugins" / "mtg"
    (staged_plugin / "tampered.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(specialist_install, "resolve_and_fetch",
                        _subdir_stub(kw2["inspection"].staged_dir))
    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        specialist_install.upgrade_specialist(**kw2)
    assert ei.value.kind == "receipt_drift"
    # old generation fully untouched
    owned = _owned(reg, "mtg")
    assert len(owned) == 1 and owned[0]["artifact_id"] == v1_aid


def test_upgrade_sync_phase_rollback_failure_leaves_journal_in_progress(
    tmp_path: Path, monkeypatch,
) -> None:
    """P1-1 (upgrade sync phase): mirrors
    test_sync_phase_rollback_failure_leaves_journal_in_progress for the
    UPGRADE handler — the upgrade sync-phase failure handler must use the
    same rollback-then-complete ordering as install/rollback/uninstall: a
    sync-phase failure whose rollback_disk() ITSELF raises must NOT complete
    the journal — it stays in-progress on disk so boot reconciliation
    re-runs the rollback (or quarantines the slug). Completing it regardless
    (the old try/finally) would strand a half-rolled-back mutation with no
    recovery."""
    import personality_binding
    from specialist_bundle_journal import BundleTxn

    ctx = _prep(tmp_path, monkeypatch)
    specialist_install.commit_specialist_install(**ctx.kw)
    kw2 = _prep_v2(tmp_path, monkeypatch, ctx)

    def _boom_commit(self):
        raise RuntimeError("tuple commit exploded")

    def _boom_rollback(self):
        raise RuntimeError("registry unreadable — rollback failed")

    monkeypatch.setattr(personality_binding.InstanceDir,
                        "commit_desired_to_active", _boom_commit)
    monkeypatch.setattr(BundleTxn, "rollback_disk", _boom_rollback)

    before_journals = set((tmp_path / "ops").glob("*.json"))

    with pytest.raises(RuntimeError):
        specialist_install.upgrade_specialist(**kw2)

    # A NEW journal was created for the upgrade attempt, and it was NOT
    # completed — the in-progress file survives for boot reconcile.
    new_journals = set((tmp_path / "ops").glob("*.json")) - before_journals
    assert len(new_journals) == 1
    payload = _json.loads(next(iter(new_journals)).read_text())
    assert payload["state"] == "in-progress"


def test_bundle_rollback_restores_prior_owned_rows(tmp_path: Path, monkeypatch) -> None:
    ctx = _prep(tmp_path, monkeypatch)
    specialist_install.commit_specialist_install(**ctx.kw)
    reg = ctx.kw["registry_path"]
    v1_aid = _owned(reg, "mtg")[0]["artifact_id"]
    kw2 = _prep_v2(tmp_path, monkeypatch, ctx)
    _, up_txn = specialist_install.upgrade_specialist(**kw2)
    v2_aid = _owned(reg, "mtg")[0]["artifact_id"]
    assert v2_aid != v1_aid

    instance, txn = specialist_install.rollback_specialist(
        slug="mtg", bundle=True, acks=ctx.acks,
        specialists_dir=tmp_path / "specialists", agents_specialists_dir=tmp_path / "agents",
        registry_path=reg, plugin_store_root=tmp_path / "store", ops_dir=tmp_path / "ops")

    assert instance.state == "active"
    owned = _owned(reg, "mtg")
    assert len(owned) == 1 and owned[0]["artifact_id"] == v1_aid   # prior owned set restored
    assert v2_aid in txn.removed_artifact_ids


def test_bundle_rollback_refuses_missing_retained_artifact(tmp_path: Path, monkeypatch) -> None:
    ctx = _prep(tmp_path, monkeypatch)
    specialist_install.commit_specialist_install(**ctx.kw)
    reg = ctx.kw["registry_path"]
    v1_aid = _owned(reg, "mtg")[0]["artifact_id"]
    kw2 = _prep_v2(tmp_path, monkeypatch, ctx)
    specialist_install.upgrade_specialist(**kw2)
    v2_aid = _owned(reg, "mtg")[0]["artifact_id"]
    # simulate the retained v1 artifact being missing/corrupt on disk
    import plugin_store
    monkeypatch.setattr(plugin_store, "artifact_verdict",
                        lambda *a, **k: "corrupt_artifact")

    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        specialist_install.rollback_specialist(
            slug="mtg", bundle=True, acks=ctx.acks,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents", registry_path=reg,
            plugin_store_root=tmp_path / "store", ops_dir=tmp_path / "ops")
    assert ei.value.kind == "rollback_artifact_missing"
    # active generation untouched (still v2)
    assert _owned(reg, "mtg")[0]["artifact_id"] == v2_aid


# ===========================================================================
# 2d — uninstall cascade + ack retirement
# ===========================================================================

def test_commit_refuses_receipt_not_matching_inspection(tmp_path: Path, monkeypatch) -> None:
    # Whole-branch D: a receipt whose id/digest/slug drifts from the acked
    # inspection is refused (receipt_mismatch) BEFORE consent/publish.
    import dataclasses
    ctx = _prep(tmp_path, monkeypatch)
    mismatched = dataclasses.replace(ctx.receipt, receipt_digest="sha256:" + "f" * 64)
    kw = dict(ctx.kw)
    kw["receipt"] = mismatched
    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        specialist_install.commit_specialist_install(**kw)
    assert ei.value.kind == "receipt_mismatch"
    assert _owned(ctx.kw["registry_path"], "mtg") == []       # registry untouched


def test_uninstall_journals_the_retired_acks_atomically(tmp_path: Path, monkeypatch) -> None:
    # Whole-branch J: the journal's before-state ack_records is the retire
    # RETURN (every slug ack present at retire time), not an earlier snapshot —
    # so an extra same-slug approval is journaled and restorable, never lost.
    from specialist_install_consent import install_consent_identity
    ctx = _prep(tmp_path, monkeypatch)
    specialist_install.commit_specialist_install(**ctx.kw)
    # A SECOND ack for the same slug (a different identity — e.g. a re-approval).
    extra = install_consent_identity(
        component_id=ctx.inspection.component_id, version="9.9.9",
        root_digest=ctx.inspection.root_digest, slug="mtg")
    ctx.acks.record(identity=extra, component_id=ctx.inspection.component_id,
                    version="9.9.9", component_checksum=ctx.inspection.root_digest,
                    slug="mtg")
    assert len(ctx.acks.snapshot_slug("mtg")) == 2

    txn = specialist_install.uninstall_specialist(
        slug="mtg", bundle=True, acks=ctx.acks,
        specialists_dir=tmp_path / "specialists", agents_specialists_dir=tmp_path / "agents",
        registry_path=ctx.kw["registry_path"], ops_dir=tmp_path / "ops")
    payload = _json.loads(Path(txn.journal_path).read_text())
    # BOTH acks were journaled as the before-state (retire return), and both
    # removed from the live ledger.
    assert len(payload["before"]["ack_records"]) == 2
    assert ctx.acks.snapshot_slug("mtg") == []
    # rollback restores exactly those two.
    txn.rollback_disk()
    assert len(ctx.acks.snapshot_slug("mtg")) == 2


def test_read_owned_plugins_rejects_traversal_and_bad_artifact_id(tmp_path: Path) -> None:
    # Whole-branch F + P1-4: a tampered PRESENT sidecar with a traversal name or
    # a non-hex artifact_id fails the whole doc closed — but as a typed RAISE
    # (never a store-path join, and never silently read as an empty owned set).
    from personality_binding import (
        OwnedPluginsSidecarError, owned_plugins_path, read_owned_plugins,
        write_owned_plugins,
    )
    good = _doc()
    write_owned_plugins(owned_plugins_path(tmp_path), good)
    assert read_owned_plugins(owned_plugins_path(tmp_path)) is not None

    poisoned = _doc()
    poisoned["plugins"][0]["name"] = "../../../etc/passwd"
    write_owned_plugins(owned_plugins_path(tmp_path), poisoned)
    with pytest.raises(OwnedPluginsSidecarError):
        read_owned_plugins(owned_plugins_path(tmp_path))

    bad_aid = _doc()
    bad_aid["plugins"][0]["artifact_id"] = "../evil"
    write_owned_plugins(owned_plugins_path(tmp_path), bad_aid)
    with pytest.raises(OwnedPluginsSidecarError):
        read_owned_plugins(owned_plugins_path(tmp_path))


def test_read_owned_plugins_distinguishes_absent_from_malformed(tmp_path: Path) -> None:
    # P1-4: an ABSENT sidecar returns None (legacy/pre-feature ⇒ empty owned
    # set); a PRESENT file that is not the full v1 shape RAISES — the two must
    # be distinguishable so a rollback never treats an unreadable sidecar as
    # "nothing was owned".
    from personality_binding import (
        OwnedPluginsSidecarError, owned_plugins_path, read_owned_plugins,
    )
    p = owned_plugins_path(tmp_path)
    assert read_owned_plugins(p) is None                       # absent → None

    # A present file missing schema_version / component_source (the pre-fix
    # "valid-empty" shapes that used to slip through as a bare dict) now raises.
    # P2: a row whose `source` is a non-mapping ("bogus") must ALSO raise here
    # rather than pass row validation and blow up later as an untyped
    # exception in a caller that indexes src["repo"]/src["ref"]/src["revision"]
    # (rollback's `_prior_owned_entry`).
    for bad in ({}, {"schema_version": 2, "component_source": {}, "plugins": []},
                {"schema_version": 1, "plugins": []},           # no component_source
                {"schema_version": 1, "component_source": {}},  # no plugins list
                {"schema_version": 1, "component_source": {}, "plugins": [
                    {"name": "mtg.mtg", "manifest_name": "mtg", "version": "1.0.0",
                     "artifact_id": "a" * 64, "source": "bogus"},
                ]}):
        p.write_text(_json.dumps(bad), encoding="utf-8")
        with pytest.raises(OwnedPluginsSidecarError):
            read_owned_plugins(p)

    # A present, unreadable (corrupt YAML) file also raises, not None.
    p.write_text("{ this: is: not: valid", encoding="utf-8")
    with pytest.raises(OwnedPluginsSidecarError):
        read_owned_plugins(p)


def test_rollback_refuses_a_malformed_prior_sidecar(tmp_path: Path, monkeypatch) -> None:
    # P1-4: a PRESENT-but-malformed prior owned-plugins sidecar must fail the
    # rollback with a typed rollback_sidecar_invalid error BEFORE any durable
    # mutation — never silently roll back to an empty owned set (dropping the
    # prior owned plugins the rollback exists to restore).
    from personality_binding import owned_plugins_prior_path

    ctx = _prep(tmp_path, monkeypatch)
    specialist_install.commit_specialist_install(**ctx.kw)
    reg = ctx.kw["registry_path"]
    kw2 = _prep_v2(tmp_path, monkeypatch, ctx)
    specialist_install.upgrade_specialist(**kw2)          # v1 rotated into the prior sidecar
    v2_aid = _owned(reg, "mtg")[0]["artifact_id"]

    # Corrupt the prior sidecar in place (a present file that is not valid v1).
    prior = owned_plugins_prior_path(tmp_path / "specialists" / "mtg")
    assert prior.exists()
    prior.write_text("{ not: valid: yaml", encoding="utf-8")

    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        specialist_install.rollback_specialist(
            slug="mtg", bundle=True, acks=ctx.acks,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents", registry_path=reg,
            plugin_store_root=tmp_path / "store", ops_dir=tmp_path / "ops")
    assert ei.value.kind == "rollback_sidecar_invalid"
    # active generation untouched (still v2) — refusal happened pre-mutation
    assert _owned(reg, "mtg")[0]["artifact_id"] == v2_aid

    # P2: an otherwise well-formed prior sidecar whose row `source` is a
    # non-mapping ("bogus") must ALSO refuse as rollback_sidecar_invalid — not
    # leak an untyped exception from _prior_owned_entry's src["repo"] indexing.
    bogus_doc = {
        "schema_version": 1,
        "component_source": {"repo": "org/repo", "ref": "main",
                              "revision": "git:" + "a" * 40, "subdir": ""},
        "plugins": [
            {"name": "mtg.mtg", "manifest_name": "mtg", "version": "0.1.0",
             "artifact_id": "b" * 64, "digest": "sha256:" + "c" * 64,
             "source": "bogus"},
        ],
    }
    prior.write_text(_json.dumps(bogus_doc), encoding="utf-8")

    with pytest.raises(specialist_install.SpecialistInstallError) as ei2:
        specialist_install.rollback_specialist(
            slug="mtg", bundle=True, acks=ctx.acks,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents", registry_path=reg,
            plugin_store_root=tmp_path / "store", ops_dir=tmp_path / "ops")
    assert ei2.value.kind == "rollback_sidecar_invalid"
    assert _owned(reg, "mtg")[0]["artifact_id"] == v2_aid


def test_bundle_uninstall_cascade(tmp_path: Path, monkeypatch) -> None:
    from specialist_install_consent import install_consent_identity

    ctx = _prep(tmp_path, monkeypatch)
    specialist_install.commit_specialist_install(**ctx.kw)
    reg = ctx.kw["registry_path"]
    owned = _owned(reg, "mtg")
    v1_aid = owned[0]["artifact_id"]
    assert len(owned) == 1

    # An OPERATOR-installed (unowned) plugin that targets the specialist —
    # survives the cascade untouched.
    data = plugin_registry.load_registry(reg)
    survivor = _unowned_entry("operator-tool")
    survivor["targets"] = ["specialist:mtg"]
    data.raw["plugins"].append(survivor)
    plugin_registry.save_registry(data, reg)

    # A consent ack exists for the slug (from _prep) — assert it is present.
    assert ctx.acks.snapshot_slug("mtg")

    txn = specialist_install.uninstall_specialist(
        slug="mtg", bundle=True, acks=ctx.acks,
        specialists_dir=tmp_path / "specialists", agents_specialists_dir=tmp_path / "agents",
        registry_path=reg, ops_dir=tmp_path / "ops")

    # owned entry removed; operator-owned survivor untouched
    assert _owned(reg, "mtg") == []
    names = {e["name"] for e in plugin_registry.load_registry(reg).entries}
    assert "operator-tool" in names
    # all slug acks retired
    assert ctx.acks.snapshot_slug("mtg") == []
    # slug tree deleted; txn carries every pre-swap artifact id (retained on disk)
    assert not (tmp_path / "specialists" / "mtg").exists()
    assert txn.removed_artifact_ids == (v1_aid,)
    assert (tmp_path / "store" / "mtg.mtg" / v1_aid).is_dir()   # artifact RETAINED
    # journal in-progress+committed (tool completes it after the sequencer)
    assert Path(txn.journal_path).is_file()


# ===========================================================================
# 2e — receipt-required guard (Task 10 review round 1, F1 defense-in-depth):
# a direct in-process caller must not be able to walk the legacy no-receipt
# path with a component that declares a source-bearing plugin dependency —
# the sourced dep resolves "available" straight off the component's own
# staged tree, but no plugin is ever published/registered, leaving an inert
# dangling pin. `commit_specialist_install`/`upgrade_specialist` must refuse
# BEFORE any InstanceDir/registry mutation; sourceless components must keep
# installing/upgrading via the legacy path exactly as before.
# ===========================================================================

def test_commit_refuses_sourced_dep_component_without_a_receipt(tmp_path: Path, monkeypatch) -> None:
    ctx = _prep(tmp_path, monkeypatch)          # component DOES declare a sourced plugin dep
    kw = dict(ctx.kw)
    kw["receipt"] = None
    reg_path = kw["registry_path"]
    before_registry_bytes = reg_path.read_bytes() if reg_path.is_file() else None

    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        specialist_install.commit_specialist_install(**kw)

    assert ei.value.kind == "receipt_required"
    # nothing durable: no owned entries, no instance tree, registry byte-identical
    assert _owned(reg_path, "mtg") == []
    assert not (tmp_path / "specialists" / "mtg").exists()
    after_registry_bytes = reg_path.read_bytes() if reg_path.is_file() else None
    assert after_registry_bytes == before_registry_bytes
    assert list((tmp_path / "ops").glob("*.json")) == []   # no journal even begun


def test_commit_sourceless_component_still_installs_without_a_receipt(
    tmp_path: Path, monkeypatch,
) -> None:
    """Regression companion to the guard above, using THIS file's bundle-
    shaped fixtures (with_plugin=False -> no `source`-bearing dependency at
    all) as a belt-and-suspenders check alongside
    tests/test_specialist_install.py::
    test_commit_persists_cas_writes_active_tuple_and_materializes_operational_files
    (a differently-shaped fixture that also calls commit_specialist_install
    with no `receipt` kwarg at all — verified still passing)."""
    ctx = _prep(tmp_path, monkeypatch, with_plugin=False)
    kw = dict(ctx.kw)
    kw["receipt"] = None

    instance = specialist_install.commit_specialist_install(**kw)

    assert instance.state == "active"
    assert _owned(kw["registry_path"], "mtg") == []   # nothing to own; nothing to guard


def test_upgrade_refuses_sourced_dep_component_without_a_receipt(
    tmp_path: Path, monkeypatch,
) -> None:
    ctx = _prep(tmp_path, monkeypatch)
    specialist_install.commit_specialist_install(**ctx.kw)   # v1 active, published via bundle mode
    reg = ctx.kw["registry_path"]
    v1_aid = _owned(reg, "mtg")[0]["artifact_id"]
    slug_dir = tmp_path / "specialists" / "mtg"
    before_tuple_bytes = specialist_install._tuple_files_snapshot(slug_dir)

    kw2 = _prep_v2(tmp_path, monkeypatch, ctx)   # v2 component ALSO declares a sourced plugin dep
    kw2["receipt"] = None

    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        specialist_install.upgrade_specialist(**kw2)

    assert ei.value.kind == "receipt_required"
    # old generation fully untouched
    owned = _owned(reg, "mtg")
    assert len(owned) == 1 and owned[0]["artifact_id"] == v1_aid
    assert specialist_install._tuple_files_snapshot(slug_dir) == before_tuple_bytes


# ===========================================================================
# 2f — uninstall stranding guard (due-diligence companion): a non-bundle
# uninstall DELETES the InstanceDir tree outright, so any owned registry
# entries a prior bundle install/upgrade published for this slug would
# become PERMANENTLY orphaned (owner points at a specialist that no longer
# exists) — a strictly worse, unrecoverable version of the same dangling-pin
# gap. rollback_specialist is NOT guarded: its non-bundle core never touches
# the registry and never deletes the slug, so nothing is ever stranded —
# worst case is a stale registry generation that the next bundle-aware
# upgrade/rollback/uninstall reconciles.
# ===========================================================================

def test_uninstall_without_bundle_refuses_when_owned_entries_would_be_stranded(
    tmp_path: Path, monkeypatch,
) -> None:
    ctx = _prep(tmp_path, monkeypatch)
    specialist_install.commit_specialist_install(**ctx.kw)   # publishes one owned entry
    reg = ctx.kw["registry_path"]
    assert len(_owned(reg, "mtg")) == 1
    slug_dir = tmp_path / "specialists" / "mtg"
    assert slug_dir.exists()

    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        specialist_install.uninstall_specialist(
            slug="mtg", specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents", registry_path=reg)

    assert ei.value.kind == "bundle_required"
    # nothing removed: the owned entry AND the instance tree both survive
    assert len(_owned(reg, "mtg")) == 1
    assert slug_dir.exists()


def test_uninstall_without_bundle_still_works_when_nothing_is_owned(
    tmp_path: Path, monkeypatch,
) -> None:
    """Regression: legacy uninstall callers that never published anything
    through the bundle path must be unaffected by the new stranding guard.
    Already covered from a plain (non-bundle-installed) fixture by
    tests/test_specialist_install.py::
    test_uninstall_removes_the_instance_dir_and_operational_files; this one
    re-checks it against a REAL (bundle-installed, plugin-less) registry_path
    that resolves but has nothing owned."""
    ctx = _prep(tmp_path, monkeypatch, with_plugin=False)
    specialist_install.commit_specialist_install(**ctx.kw)   # bundle install, no owned plugins
    reg = ctx.kw["registry_path"]
    assert _owned(reg, "mtg") == []
    slug_dir = tmp_path / "specialists" / "mtg"
    assert slug_dir.exists()

    specialist_install.uninstall_specialist(
        slug="mtg", specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents", registry_path=reg)

    assert not slug_dir.exists()


# ===========================================================================
# 2g — P1-1 (sync-phase journal-complete) + P2-5 (uninstall retire/begin)
# ===========================================================================

def test_sync_phase_rollback_failure_leaves_journal_in_progress(
    tmp_path: Path, monkeypatch,
) -> None:
    """P1-1 (sync phase): a sync-phase failure whose rollback_disk() ITSELF
    raises must NOT complete the journal — it stays in-progress on disk so boot
    reconciliation re-runs the rollback (or quarantines). Completing it would
    strand a half-rolled-back mutation with no recovery."""
    import personality_binding
    from specialist_bundle_journal import BundleTxn

    ctx = _prep(tmp_path, monkeypatch)

    def _boom_commit(self):
        raise RuntimeError("tuple commit exploded")

    def _boom_rollback(self):
        raise RuntimeError("registry unreadable — rollback failed")

    monkeypatch.setattr(personality_binding.InstanceDir,
                        "commit_desired_to_active", _boom_commit)
    monkeypatch.setattr(BundleTxn, "rollback_disk", _boom_rollback)

    with pytest.raises(RuntimeError):
        specialist_install.commit_specialist_install(**ctx.kw)

    # Journal NOT completed — the in-progress file survives for boot reconcile.
    journals = list((tmp_path / "ops").glob("*.json"))
    assert len(journals) == 1
    payload = _json.loads(journals[0].read_text())
    assert payload["state"] == "in-progress"


def test_sync_phase_rollback_success_completes_journal(
    tmp_path: Path, monkeypatch,
) -> None:
    """P1-1 companion: when rollback_disk SUCCEEDS on a sync-phase failure, the
    journal IS completed (pruned) as before — the fix only withholds completion
    when the rollback itself fails."""
    import personality_binding

    ctx = _prep(tmp_path, monkeypatch)

    def _boom_commit(self):
        raise RuntimeError("tuple commit exploded")

    monkeypatch.setattr(personality_binding.InstanceDir,
                        "commit_desired_to_active", _boom_commit)

    with pytest.raises(RuntimeError):
        specialist_install.commit_specialist_install(**ctx.kw)

    assert _owned(ctx.kw["registry_path"], "mtg") == []          # registry restored
    assert list((tmp_path / "ops").glob("*.json")) == []          # journal completed/pruned


def test_uninstall_begin_failure_restores_retired_acks(
    tmp_path: Path, monkeypatch,
) -> None:
    """P2-5: uninstall retires the slug's consent acks BEFORE begin(); if
    begin() then raises, the retired records must be restored so a begin
    failure leaves the ack ledger untouched — never silently drops the
    operator's approval."""
    import specialist_bundle_journal

    ctx = _prep(tmp_path, monkeypatch)
    specialist_install.commit_specialist_install(**ctx.kw)
    assert len(ctx.acks.snapshot_slug("mtg")) == 1               # one ack present

    def _boom_begin(*a, **k):
        raise OSError("ops dir unwritable")

    monkeypatch.setattr(specialist_bundle_journal, "begin", _boom_begin)

    with pytest.raises(OSError):
        specialist_install.uninstall_specialist(
            slug="mtg", bundle=True, acks=ctx.acks,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents",
            registry_path=ctx.kw["registry_path"], ops_dir=tmp_path / "ops")

    # The retired ack was restored — the failed uninstall left the ledger intact.
    assert len(ctx.acks.snapshot_slug("mtg")) == 1


def test_bundle_install_rollback_removes_fresh_op_symlink(tmp_path: Path, monkeypatch) -> None:
    """#490 end-to-end (+ Sol/Terra design r1): the lifecycle must hand its
    OWN agents_specialists_dir to the BundleTxn, and rolling back a fresh
    install must remove the op-symlink materialized during the commit — a
    rollback that cleans only the default path leaves the live symlink to
    poison every later reload."""
    ctx = _prep(tmp_path, monkeypatch)
    instance, txn = specialist_install.commit_specialist_install(**ctx.kw)
    assert instance.state == "active"
    link = tmp_path / "agents" / "mtg"
    assert link.is_symlink()                     # materialized during commit
    target_name = os.readlink(link)

    txn.rollback_disk()

    assert not link.is_symlink() and not link.exists()
    assert not (tmp_path / "agents" / target_name).exists()


def test_a_pending_upgrade_does_not_claim_it_swapped_the_owned_set(
        tmp_path: Path, monkeypatch) -> None:
    """#676 (Terra diff-review r11): a pending/error upgrade leaves the old
    owned generation untouched and hands the UNCHANGED owned set through as
    `before_entries`. Read as a removal, that warns an operator about surviving
    plugin data after an operation that removed nothing — so the transaction
    says whether it actually swapped, and this one did not."""
    import specialist_lifecycle
    ctx = _prep(tmp_path, monkeypatch)
    specialist_install.commit_specialist_install(**ctx.kw)

    kw2 = _prep_v2(tmp_path, monkeypatch, ctx)
    _, active_txn = specialist_install.upgrade_specialist(**kw2)
    assert active_txn.owned_swap_committed is True

    kw3 = _prep_v2(tmp_path, monkeypatch, ctx, ref="v3", marker="v3")
    monkeypatch.setattr(specialist_lifecycle, "satisfy_config",
                        lambda **kw: (False, ["API_KEY"]))
    instance, pending_txn = specialist_install.upgrade_specialist(**kw3)

    assert instance.state == "pending-configuration"
    assert pending_txn.owned_swap_committed is False
    # …and it is NOT that before_entries is empty: the field carries the
    # unchanged owned set, which is exactly why the flag has to exist.
    assert len(pending_txn.before_entries) == 1


# ---------------------------------------------------------------------------
# #676 (INV-TOOL-006), Terra handback review: an owned-set swap that DROPS a
# plugin is a persisting committed removal whatever door reached it. The
# transaction records exactly which names it dropped, so the success payloads
# can disclose them. These pin the recording against the REAL install/upgrade/
# rollback/uninstall — the tool-level tests use doubles for the txn, and a
# double that drifts from the producer proves nothing about the producer.
# ---------------------------------------------------------------------------

def _prep_multi(tmp_path: Path, monkeypatch, names: "list[str]", *,
                root: str = "v1", ref: str = "main", sha: str = "a" * 40,
                base_ctx=None):
    """A component declaring one bundled plugin per entry in `names`, inspected
    (install mode when `base_ctx` is None, upgrade mode otherwise) with consent
    recorded. Returns the kwargs the corresponding lifecycle call takes."""
    import plugin_store
    from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity
    from specialist_registry import InstalledSpecialistIndex

    comp, mpath = write_minimal_component(tmp_path / root, slug="mtg")
    manifest = _json.loads(mpath.read_text(encoding="utf-8"))
    for name in names:
        write_bundled_plugin(comp, name)
        # Distinguish generations so an upgrade is a real content change.
        (comp / "plugins" / name / "README.md").write_text(root, encoding="utf-8")
        digest = "sha256:" + plugin_store.content_checksum(comp / "plugins" / name)
        manifest["dependencies"].append({
            "kind": "plugin/implementation", "identifier": name, "digest": digest,
            "source": {"type": "bundled", "path": f"plugins/{name}"}})
    if base_ctx is not None:
        manifest["version"] = "0.2.0"
    mpath.write_text(_json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _subdir_stub(comp, sha))
    idx = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "installed-index"))
    idx.load()
    extra = ({} if base_ctx is None
             else dict(mode="upgrade", target_slug="mtg",
                       specialists_dir=tmp_path / "specialists"))
    inspection = specialist_install.inspect_specialist_repo(
        "org/repo", ref, staging_root=tmp_path / f"staging-{root}",
        installed_index=idx, receipts_dir=tmp_path / "receipts", **extra)
    receipt = specialist_receipt.load(inspection.receipt_id,
                                      receipts_dir=tmp_path / "receipts")
    assert receipt is not None

    acks = base_ctx.acks if base_ctx is not None else SpecialistInstallAckStore(
        path=tmp_path / "acks.json")
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug,
        receipt_digest=inspection.receipt_digest)
    acks.record(identity=identity, component_id=inspection.component_id,
                version=inspection.version, component_checksum=inspection.root_digest,
                slug=inspection.slug, receipt_digest=inspection.receipt_digest)

    common = dict(
        inspection=inspection, receipt=receipt, config={},
        secret_names_provided=frozenset(), acks=acks,
        specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents",
        registry_path=tmp_path / "registry.json",
        plugin_store_root=tmp_path / "store", ops_dir=tmp_path / "ops")
    if base_ctx is not None:
        common["slug"] = "mtg"
    return _Ctx(acks=acks, inspection=inspection, receipt=receipt, kw=common)


def test_a_real_upgrade_records_the_owned_plugin_it_dropped(
        tmp_path: Path, monkeypatch) -> None:
    """The positive case Terra's handback finding names: v1 owns two plugins,
    v2 owns one, and the swap that publishes v2 removes the other's registry
    entry. `removed_owned_names` is that name — and `before_entries` is not,
    because it still carries the plugin the upgrade re-published."""
    ctx = _prep_multi(tmp_path, monkeypatch, ["mtg", "extra"])
    specialist_install.commit_specialist_install(**ctx.kw)
    reg = ctx.kw["registry_path"]
    assert sorted(e["name"] for e in _owned(reg, "mtg")) == ["mtg.extra", "mtg.mtg"]

    up = _prep_multi(tmp_path, monkeypatch, ["mtg"], root="v2", ref="v2",
                     sha="b" * 40, base_ctx=ctx)
    instance, txn = specialist_install.upgrade_specialist(**up.kw)

    assert instance.state == "active"
    assert [e["name"] for e in _owned(reg, "mtg")] == ["mtg.mtg"]
    assert txn.owned_swap_committed is True
    assert txn.removed_owned_names == ("mtg.extra",)
    assert sorted(e["name"] for e in txn.before_entries) == ["mtg.extra", "mtg.mtg"]


def test_a_real_upgrade_that_keeps_its_owned_set_records_no_drop(
        tmp_path: Path, monkeypatch) -> None:
    """The negative arm, measured separately: an upgrade that re-publishes the
    same owned names removed nothing, and a survival warning there would be
    the false half of the invariant."""
    ctx = _prep(tmp_path, monkeypatch)
    specialist_install.commit_specialist_install(**ctx.kw)
    kw2 = _prep_v2(tmp_path, monkeypatch, ctx)
    _, txn = specialist_install.upgrade_specialist(**kw2)

    assert txn.owned_swap_committed is True
    assert txn.removed_owned_names == ()
    assert len(txn.before_entries) == 1


def test_a_real_rollback_records_the_owned_plugin_it_dropped(
        tmp_path: Path, monkeypatch) -> None:
    """A rollback republishes the RETAINED prior owned set, so a plugin the
    current generation added and the prior one never had is dropped by that
    swap — the same removal, reached by the fourth door."""
    ctx = _prep_multi(tmp_path, monkeypatch, ["mtg"])
    specialist_install.commit_specialist_install(**ctx.kw)
    reg = ctx.kw["registry_path"]

    up = _prep_multi(tmp_path, monkeypatch, ["mtg", "extra"], root="v2", ref="v2",
                     sha="b" * 40, base_ctx=ctx)
    specialist_install.upgrade_specialist(**up.kw)
    assert sorted(e["name"] for e in _owned(reg, "mtg")) == ["mtg.extra", "mtg.mtg"]

    instance, txn = specialist_install.rollback_specialist(
        slug="mtg", bundle=True, acks=ctx.acks,
        specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents", registry_path=reg,
        plugin_store_root=tmp_path / "store", ops_dir=tmp_path / "ops")

    assert instance.state == "active"
    assert [e["name"] for e in _owned(reg, "mtg")] == ["mtg.mtg"]
    assert txn.owned_swap_committed is True
    assert txn.removed_owned_names == ("mtg.extra",)


def test_a_real_uninstall_records_every_owned_name_it_dropped(
        tmp_path: Path, monkeypatch) -> None:
    """The uninstall's swap publishes an EMPTY owned set, so every pre-swap
    name is dropped. This is the claim the tool-level double asserts, pinned
    against the real `uninstall_specialist`."""
    ctx = _prep_multi(tmp_path, monkeypatch, ["mtg", "extra"])
    specialist_install.commit_specialist_install(**ctx.kw)
    reg = ctx.kw["registry_path"]

    txn = specialist_install.uninstall_specialist(
        slug="mtg", bundle=True, acks=ctx.acks,
        specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents", registry_path=reg,
        ops_dir=tmp_path / "ops")

    assert _owned(reg, "mtg") == []
    assert txn.owned_swap_committed is True
    assert sorted(txn.removed_owned_names) == ["mtg.extra", "mtg.mtg"]


def test_a_pending_upgrade_records_no_dropped_names(
        tmp_path: Path, monkeypatch) -> None:
    """The fail-closed pairing with owned_swap_committed=False: a pending
    upgrade never swapped, so there is nothing it dropped — even though its
    `before_entries` carries the whole unchanged owned set."""
    import specialist_lifecycle
    ctx = _prep(tmp_path, monkeypatch)
    specialist_install.commit_specialist_install(**ctx.kw)
    kw2 = _prep_v2(tmp_path, monkeypatch, ctx)
    specialist_install.upgrade_specialist(**kw2)

    kw3 = _prep_v2(tmp_path, monkeypatch, ctx, ref="v3", marker="v3")
    monkeypatch.setattr(specialist_lifecycle, "satisfy_config",
                        lambda **kw: (False, ["API_KEY"]))
    _, pending_txn = specialist_install.upgrade_specialist(**kw3)

    assert pending_txn.owned_swap_committed is False
    assert pending_txn.removed_owned_names == ()
    assert len(pending_txn.before_entries) == 1
