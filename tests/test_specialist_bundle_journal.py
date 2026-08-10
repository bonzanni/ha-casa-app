"""Task 9: crash-safe bundle-op journal + boot reconciliation with
quarantine semantics (design spec §3.1)."""
from __future__ import annotations

import json

import pytest

import plugin_registry
import specialist_bundle_journal as journal
from plugin_fixtures import owned_entry
from specialist_install_consent import SpecialistInstallAckStore, install_consent_identity

pytestmark = pytest.mark.unit


def _registry_doc(entries):
    return {"schema_version": 1, "seeded_defaults": [], "plugins": entries}


def _write_registry(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_registry_doc(entries)), encoding="utf-8")


def _read_registry(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _finance_entry():
    return owned_entry(name="finance.finance", owner="specialist:finance",
                       manifest_name="finance", repo="bonzanni/casa-specialist-finance")


# --------------------------------------------------------------------------
# begin / mark_step / complete lifecycle
# --------------------------------------------------------------------------

def test_begin_writes_journal_with_full_before_state(tmp_path):
    ops_dir = tmp_path / "ops"
    entries = [owned_entry()]
    ack_records = [{"component_id": "c", "version": "1",
                    "component_checksum": "x", "slug": "mtg"}]
    path = journal.begin(
        "install", "mtg",
        before_entries=entries,
        before_tuple_files={"active.yaml": "old-content"},
        ack_records=ack_records,
        receipt_digest="deadbeef",
        ops_dir=ops_dir,
    )
    assert path.parent == ops_dir
    assert journal.JOURNAL_NAME_RE.match(path.name)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["op"] == "install"
    assert payload["slug"] == "mtg"
    assert payload["state"] == "in-progress"
    assert payload["before"]["registry_entries"] == entries
    assert payload["before"]["tuple_files"] == {"active.yaml": "old-content"}
    assert payload["before"]["ack_records"] == ack_records
    assert payload["receipt_digest"] == "deadbeef"
    assert payload["steps_done"] == []


def test_mark_step_appends(tmp_path):
    ops_dir = tmp_path / "ops"
    path = journal.begin("install", "mtg", before_entries=[], before_tuple_files={},
                         ack_records=[], ops_dir=ops_dir)
    journal.mark_step(path, "cas_published")
    journal.mark_step(path, "registry_swapped")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["steps_done"] == ["cas_published", "registry_swapped"]


def test_complete_writes_complete_state_then_unlinks(tmp_path):
    ops_dir = tmp_path / "ops"
    path = journal.begin("install", "mtg", before_entries=[], before_tuple_files={},
                         ack_records=[], ops_dir=ops_dir)
    journal.complete(path)
    assert not path.exists()


def test_begin_records_consent_identity_and_target_root(tmp_path):
    # Whole-branch I: the payload records the consent identity + target root.
    ops_dir = tmp_path / "ops"
    path = journal.begin(
        "install", "mtg", before_entries=[], before_tuple_files={},
        ack_records=[], consent_identity="ident-abc",
        target_root="casa/mtg@0.2.0#sha256:deadbeef", ops_dir=ops_dir)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["consent_identity"] == "ident-abc"
    assert payload["target_root"] == "casa/mtg@0.2.0#sha256:deadbeef"
    assert journal._valid_payload(payload, "mtg") is True


def test_valid_payload_tolerates_absent_provenance_but_rejects_nonstring(tmp_path):
    # Whole-branch I additive: absent (old journal) tolerated; non-string bad.
    base = {"schema_version": 1, "op": "install", "slug": "mtg",
            "state": "in-progress",
            "before": {"registry_entries": [], "tuple_files": {}, "ack_records": []}}
    assert journal._valid_payload(base, "mtg") is True          # no provenance keys
    assert journal._valid_payload({**base, "consent_identity": 5}, "mtg") is False
    assert journal._valid_payload({**base, "target_root": []}, "mtg") is False


def test_fsync_write_is_atomic_on_a_torn_write(tmp_path, monkeypatch):
    # Whole-branch K: a crash mid-write must leave the ORIGINAL bytes intact,
    # never a torn hybrid that reconcile_boot would quarantine.
    ops_dir = tmp_path / "ops"
    path = journal.begin("install", "mtg", before_entries=[], before_tuple_files={},
                         ack_records=[], ops_dir=ops_dir)
    original = path.read_text(encoding="utf-8")

    real_write = journal.os.write

    def _boom(fd, data):
        real_write(fd, data[: len(data) // 2])   # partial write
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(journal.os, "write", _boom)
    with pytest.raises(OSError):
        journal.mark_step(path, "cas_published")
    monkeypatch.undo()
    # The journal on disk is still the intact pre-crash payload — os.replace
    # never swapped in the torn temp file.
    assert path.read_text(encoding="utf-8") == original
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "in-progress"
    # No orphaned temp file was left in ops_dir.
    assert [p.name for p in ops_dir.iterdir()] == [path.name]


def test_rollback_over_invalid_registry_quarantines(tmp_path):
    # Whole-branch G: an in-progress journal whose registry is unreadable must
    # route to the quarantine path, never save a partial reconstructed doc.
    ops_dir = tmp_path / "ops"
    reg = tmp_path / "registry.json"
    reg.write_text("{ not valid json")
    entries = [owned_entry()]
    journal.begin("install", "mtg", before_entries=entries, before_tuple_files={},
                  ack_records=[], ops_dir=ops_dir)
    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=reg,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")
    assert actions == [{"slug": "mtg", "action": "quarantine"}]
    # The unreadable registry was NOT overwritten with partial data.
    assert reg.read_text() == "{ not valid json"


def test_reconcile_boot_sweeps_aged_receipts(tmp_path):
    # Whole-branch N: boot age-sweeps orphan receipt sidecars.
    import os
    import time
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    old = receipts / ("a" * 32 + ".json")
    old.write_text("{}")
    fresh = receipts / ("b" * 32 + ".json")
    fresh.write_text("{}")
    old_ts = time.time() - 8 * 24 * 3600           # 8 days old
    os.utime(old, (old_ts, old_ts))
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=tmp_path / "registry.json",
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json",
        receipts_dir=receipts)
    assert not old.exists()          # aged sidecar swept
    assert fresh.exists()            # fresh sidecar kept
    assert {"slug": None, "action": "swept_receipts", "count": 1} in actions


def test_reconcile_boot_keeps_pending_installs_receipt_and_staging(tmp_path):
    """#331 (Sol r5-2): a slug with a live pending-configuration candidate
    (desired.yaml on disk) keeps its aged receipt AND the staged tree that
    receipt references — pre-fix, boot age-swept both, so a pending install
    older than a week plus one restart became permanently unfinishable
    (receipt_required, staged_dir_invalid, and re-inspect refuses the
    occupied slug)."""
    import json
    import os
    import time

    receipts = tmp_path / "receipts"
    receipts.mkdir()
    specialists = tmp_path / "specialists"
    staging = specialists / ".staging"
    staged_tree = staging / "deadbeef01"
    staged_tree.mkdir(parents=True)
    (staged_tree / "manifest.json").write_text("{}", encoding="utf-8")
    (specialists / "mtg").mkdir(parents=True)
    (specialists / "mtg" / "desired.yaml").write_text("x: 1\n", encoding="utf-8")
    # The durable marker written at pending-commit time (Sol r6-2) names the
    # EXACT receipt the pending candidate was committed with.
    (specialists / "mtg" / "pending-receipt.json").write_text(
        json.dumps({"receipt_id": "c" * 32}), encoding="utf-8")

    receipt_path = receipts / ("c" * 32 + ".json")
    receipt_path.write_text(json.dumps({
        "receipt_id": "c" * 32, "slug": "mtg",
        "component_staged_path": str(staged_tree), "plugins": []}),
        encoding="utf-8")
    # A NEWER same-slug inspection for a different root (Terra r6-1 +
    # Sol r6-2): mtime must not beat the marker — this one sweeps, the
    # marker'd (older) one stays.
    superseded_tree = staging / "0ldbeef03"
    superseded_tree.mkdir()
    superseded_receipt = receipts / ("d" * 32 + ".json")
    superseded_receipt.write_text(json.dumps({
        "receipt_id": "d" * 32, "slug": "mtg",
        "component_staged_path": str(superseded_tree), "plugins": []}),
        encoding="utf-8")
    # An unrelated aged staging tree still sweeps.
    orphan = staging / "feedface02"
    orphan.mkdir()
    month_ago = time.time() - 30 * 24 * 3600
    older_ts = month_ago - 10 * 24 * 3600
    for p in (receipt_path, staged_tree):
        os.utime(p, (older_ts, older_ts))     # marker'd receipt is the OLDER one
    for p in (superseded_receipt, superseded_tree, orphan):
        os.utime(p, (month_ago, month_ago))   # newer, but not the marker'd one

    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=tmp_path / "registry.json",
        specialists_dir=specialists, acks_path=tmp_path / "acks.json",
        receipts_dir=receipts, personas_dir=tmp_path / "personas")
    assert receipt_path.exists()      # the marker'd receipt retained (not the newest!)
    assert staged_tree.is_dir()       # its attested staging retained
    assert not superseded_receipt.exists()   # newer same-slug receipt swept
    assert not superseded_tree.exists()      # and its staging with it
    assert not orphan.exists()        # unreferenced aged tree swept


# --------------------------------------------------------------------------
# reconcile_boot: no-op cases
# --------------------------------------------------------------------------

def test_reconcile_boot_noop_absent_ops_dir(tmp_path):
    actions = journal.reconcile_boot(
        ops_dir=tmp_path / "nope", registry_path=tmp_path / "registry.json",
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")
    assert actions == []
    assert journal.last_boot_reconcile_actions == []


def test_reconcile_boot_noop_empty_ops_dir(tmp_path):
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=tmp_path / "registry.json",
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")
    assert actions == []


def test_reconcile_boot_skips_preexisting_quarantined_file(tmp_path):
    """Idempotency: a file already renamed .quarantined by an earlier boot is
    left completely untouched on the next boot."""
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [])
    q = ops_dir / "mtg.deadbeef.json.quarantined"
    q.write_text("not even valid json", encoding="utf-8")

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")

    assert actions == []
    assert q.read_text(encoding="utf-8") == "not even valid json"


# --------------------------------------------------------------------------
# reconcile_boot: in-progress journal -> rollback
# --------------------------------------------------------------------------

def test_reconcile_boot_rolls_back_inprogress_journal(tmp_path):
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    specialists_dir = tmp_path / "specialists"
    acks_path = tmp_path / "acks.json"

    before_entry = owned_entry()
    # "current" mid-mutation registry state: the before-entry is already gone.
    _write_registry(registry_path, [])

    slug_dir = specialists_dir / "mtg"
    slug_dir.mkdir(parents=True)
    (slug_dir / "active.yaml").write_text("mid-mutation", encoding="utf-8")
    (slug_dir / "desired.yaml").write_text("mid-mutation-desired", encoding="utf-8")

    ack_record = {"component_id": "casa-specialist-mtg", "version": "0.2.0",
                  "component_checksum": "root-digest", "slug": "mtg", "ts": 1}

    journal.begin(
        "install", "mtg",
        before_entries=[before_entry],
        before_tuple_files={"active.yaml": "pre-mutation", "desired.yaml": None},
        ack_records=[ack_record],
        ops_dir=ops_dir,
    )

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=specialists_dir, acks_path=acks_path)

    assert actions == [{"slug": "mtg", "action": "rolled_back"}]
    assert list(ops_dir.iterdir()) == []

    doc = _read_registry(registry_path)
    assert doc["plugins"] == [before_entry]

    assert (slug_dir / "active.yaml").read_text(encoding="utf-8") == "pre-mutation"
    assert not (slug_dir / "desired.yaml").exists()

    identity = install_consent_identity(
        component_id=ack_record["component_id"], version=ack_record["version"],
        root_digest=ack_record["component_checksum"], slug=ack_record["slug"])
    restored = SpecialistInstallAckStore(acks_path).get(identity)
    assert restored is not None and restored["slug"] == "mtg"


def test_rollback_restores_the_pending_receipt_marker(tmp_path):
    """#331 (Sol r7-2): the pending-receipt marker is journalled with the
    tuple files — an activating retry clears it, and when that retry's
    mutation is rolled back (sequencer failure / crash), compensation must
    restore the marker with desired.yaml, or the boot sweep falls back to
    newest-by-mtime and can retain the wrong root's receipt."""
    import json as _json

    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [])
    specialists_dir = tmp_path / "specialists"
    slug_dir = specialists_dir / "mtg"
    slug_dir.mkdir(parents=True)
    # Mid-mutation state: the activating retry cleared the marker and
    # consumed desired.yaml.
    (slug_dir / "active.yaml").write_text("mid-mutation", encoding="utf-8")

    marker_json = _json.dumps({"receipt_id": "e" * 32})
    journal.begin(
        "install", "mtg", before_entries=[],
        before_tuple_files={"active.yaml": None,
                            "desired.yaml": "pending-tuple",
                            "pending-receipt.json": marker_json},
        ack_records=[], ops_dir=ops_dir)

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=specialists_dir, acks_path=tmp_path / "acks.json")
    assert {"slug": "mtg", "action": "rolled_back"} in actions
    assert (slug_dir / "desired.yaml").read_text(encoding="utf-8") == "pending-tuple"
    assert _json.loads((slug_dir / "pending-receipt.json").read_text(
        encoding="utf-8")) == {"receipt_id": "e" * 32}
    assert not (slug_dir / "active.yaml").exists()

    # And the snapshot helper records the marker for future journals.
    from specialist_install import _tuple_files_snapshot
    snap = _tuple_files_snapshot(slug_dir)
    assert snap["pending-receipt.json"] == marker_json


def test_reconcile_boot_idempotent_second_run_is_noop(tmp_path):
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    specialists_dir = tmp_path / "specialists"
    acks_path = tmp_path / "acks.json"
    _write_registry(registry_path, [])
    journal.begin("install", "mtg", before_entries=[owned_entry()],
                 before_tuple_files={}, ack_records=[], ops_dir=ops_dir)

    first = journal.reconcile_boot(ops_dir=ops_dir, registry_path=registry_path,
                                    specialists_dir=specialists_dir, acks_path=acks_path)
    assert first == [{"slug": "mtg", "action": "rolled_back"}]

    second = journal.reconcile_boot(ops_dir=ops_dir, registry_path=registry_path,
                                     specialists_dir=specialists_dir, acks_path=acks_path)
    assert second == []


def test_reconcile_boot_stashes_actions_on_module_attribute(tmp_path):
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [])
    journal.begin("install", "mtg", before_entries=[owned_entry()],
                 before_tuple_files={}, ack_records=[], ops_dir=ops_dir)
    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")
    assert journal.last_boot_reconcile_actions == actions
    assert actions == [{"slug": "mtg", "action": "rolled_back"}]


# --------------------------------------------------------------------------
# reconcile_boot: state == "complete" crash window -> prune WITHOUT rollback
# --------------------------------------------------------------------------

def test_reconcile_boot_prunes_complete_journal_without_rollback(tmp_path):
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    specialists_dir = tmp_path / "specialists"
    acks_path = tmp_path / "acks.json"

    # "after" state: the op finished — the owned entry IS now present.
    after_entry = owned_entry()
    _write_registry(registry_path, [after_entry])

    path = journal.begin(
        "install", "mtg",
        before_entries=[],   # before the op there was NO owned entry
        before_tuple_files={},
        ack_records=[],
        ops_dir=ops_dir,
    )
    # Simulate the crash window: state flipped to "complete" but the unlink
    # never happened.
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["state"] = "complete"
    path.write_text(json.dumps(payload), encoding="utf-8")

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=specialists_dir, acks_path=acks_path)

    assert actions == [{"slug": "mtg", "action": "pruned_complete"}]
    assert list(ops_dir.iterdir()) == []
    # NOT rolled back — the after-state (owned entry present) survives.
    doc = _read_registry(registry_path)
    assert doc["plugins"] == [after_entry]


# --------------------------------------------------------------------------
# reconcile_boot: filename matches, payload corrupt/invalid -> quarantine(slug)
# --------------------------------------------------------------------------

def test_reconcile_boot_corrupt_json_quarantines_exactly_that_slug(tmp_path):
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [owned_entry(), _finance_entry()])
    bad = ops_dir / f"mtg.{'a' * 32}.json"
    bad.write_text("{not json", encoding="utf-8")

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")

    assert actions == [{"slug": "mtg", "action": "quarantine"}]
    assert not bad.exists()
    assert bad.with_name(bad.name + ".quarantined").exists()

    doc = _read_registry(registry_path)
    names = [e["name"] for e in doc["plugins"]]
    assert "mtg.mtg" not in names
    assert "finance.finance" in names
    assert doc["quarantined_bundles"] == ["mtg"]


def test_reconcile_boot_payload_slug_mismatch_quarantines(tmp_path):
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [owned_entry()])
    path = journal.begin("install", "mtg", before_entries=[], before_tuple_files={},
                         ack_records=[], ops_dir=ops_dir)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["slug"] = "other"
    path.write_text(json.dumps(payload), encoding="utf-8")

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")

    assert actions == [{"slug": "mtg", "action": "quarantine"}]
    assert path.with_name(path.name + ".quarantined").exists()
    assert _read_registry(registry_path)["quarantined_bundles"] == ["mtg"]


def test_reconcile_boot_malformed_before_shape_quarantines(tmp_path):
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [owned_entry()])
    path = journal.begin("install", "mtg", before_entries=[], before_tuple_files={},
                         ack_records=[], ops_dir=ops_dir)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["before"] = ["not", "a", "mapping"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")

    assert actions == [{"slug": "mtg", "action": "quarantine"}]
    assert path.with_name(path.name + ".quarantined").exists()


def test_reconcile_boot_tuple_files_key_outside_fixed_set_quarantines(tmp_path):
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [owned_entry()])
    path = journal.begin("install", "mtg", before_entries=[],
                         before_tuple_files={"unexpected.yaml": "x"},
                         ack_records=[], ops_dir=ops_dir)

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")

    assert actions == [{"slug": "mtg", "action": "quarantine"}]
    assert path.with_name(path.name + ".quarantined").exists()


def test_reconcile_boot_traversal_tuple_key_quarantines(tmp_path):
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [owned_entry()])
    path = journal.begin("install", "mtg", before_entries=[],
                         before_tuple_files={"../evil.yaml": "x"},
                         ack_records=[], ops_dir=ops_dir)

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")

    assert actions == [{"slug": "mtg", "action": "quarantine"}]
    assert path.with_name(path.name + ".quarantined").exists()


def test_reconcile_boot_ack_restore_failure_quarantines_slug(tmp_path):
    """A structurally-valid-looking ack record (a dict — passes the strict
    shape check) missing required keys blows up deep inside
    SpecialistInstallAckStore.restore_records (KeyError). reconcile_boot must
    catch that and quarantine the slug rather than crash boot (per Task 7:
    restore_records is atomic — nothing persisted on raise)."""
    ops_dir = tmp_path / "ops"
    registry_path = tmp_path / "registry.json"
    specialists_dir = tmp_path / "specialists"
    acks_path = tmp_path / "acks.json"
    _write_registry(registry_path, [])

    bad_ack = {"slug": "mtg"}   # missing component_id/version/component_checksum
    journal.begin(
        "install", "mtg",
        before_entries=[owned_entry()],
        before_tuple_files={},
        ack_records=[bad_ack],
        ops_dir=ops_dir,
    )

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=specialists_dir, acks_path=acks_path)

    assert actions == [{"slug": "mtg", "action": "quarantine"}]
    doc = _read_registry(registry_path)
    assert doc["plugins"] == []   # quarantine cleans up the partial rollback
    assert doc["quarantined_bundles"] == ["mtg"]
    remaining = list(ops_dir.iterdir())
    assert len(remaining) == 1 and remaining[0].name.endswith(".quarantined")


# --------------------------------------------------------------------------
# reconcile_boot: unparseable filename -> quarantine_all (never delete)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("filename", ["garbage.json", "noslug", "mtg.nothex.json"])
def test_reconcile_boot_unparseable_filename_quarantines_all(tmp_path, filename):
    ops_dir = tmp_path / "ops"
    ops_dir.mkdir()
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [owned_entry(), _finance_entry()])
    bad = ops_dir / filename
    bad.write_text("whatever bytes", encoding="utf-8")

    actions = journal.reconcile_boot(
        ops_dir=ops_dir, registry_path=registry_path,
        specialists_dir=tmp_path / "specialists", acks_path=tmp_path / "acks.json")

    assert actions == [{"slug": None, "action": "quarantine_all"}]
    assert not bad.exists()
    assert (ops_dir / f"{filename}.quarantined").exists()

    doc = _read_registry(registry_path)
    assert doc["plugins"] == []
    assert set(doc["quarantined_bundles"]) == {"mtg", "finance"}


# --------------------------------------------------------------------------
# quarantine / quarantine_all direct unit tests
# --------------------------------------------------------------------------

def test_quarantine_removes_owned_entries_and_flags_slug(tmp_path):
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [owned_entry(), _finance_entry()])
    journal.quarantine("mtg", registry_path=registry_path)
    doc = _read_registry(registry_path)
    assert [e["name"] for e in doc["plugins"]] == ["finance.finance"]
    assert doc["quarantined_bundles"] == ["mtg"]


def test_quarantine_is_idempotent(tmp_path):
    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [owned_entry()])
    journal.quarantine("mtg", registry_path=registry_path)
    journal.quarantine("mtg", registry_path=registry_path)
    doc = _read_registry(registry_path)
    assert doc["quarantined_bundles"] == ["mtg"]


def test_quarantine_all_removes_every_owned_entry_keeps_unowned(tmp_path):
    registry_path = tmp_path / "registry.json"
    unowned = {
        "name": "gmail",
        "source": {"type": "github", "repo": "o/r3", "ref": "v1",
                   "revision": "git:" + "b" * 40, "subdir": ""},
        "artifact_id": plugin_registry.compute_artifact_id(
            repo="o/r3", revision="git:" + "b" * 40, subdir="", name="gmail"),
        "version": "1.0.0", "targets": ["resident:tina"],
    }
    _write_registry(registry_path, [owned_entry(), _finance_entry(), unowned])
    journal.quarantine_all(registry_path=registry_path)
    doc = _read_registry(registry_path)
    assert [e["name"] for e in doc["plugins"]] == ["gmail"]
    assert set(doc["quarantined_bundles"]) == {"mtg", "finance"}


# --------------------------------------------------------------------------
# BundleTxn.rollback_disk: direct unit test with non-default paths
# --------------------------------------------------------------------------

def test_bundletxn_rollback_disk_restores_registry_tuple_files_and_acks(tmp_path):
    registry_path = tmp_path / "reg" / "registry.json"
    specialists_dir = tmp_path / "spec"
    acks_path = tmp_path / "acks" / "acks.json"
    _write_registry(registry_path, [])

    slug_dir = specialists_dir / "mtg"
    slug_dir.mkdir(parents=True)
    (slug_dir / "active.yaml").write_text("mid-mutation", encoding="utf-8")

    before_entry = owned_entry()
    ack_record = {"component_id": "casa-specialist-mtg", "version": "0.2.0",
                  "component_checksum": "root-digest", "slug": "mtg", "ts": 1}

    txn = journal.BundleTxn(
        journal_path=tmp_path / "unused.json",
        slug="mtg",
        before_entries=[before_entry],
        before_tuple_files={"active.yaml": "pre-mutation"},
        ack_records=[ack_record],
        registry_path=registry_path,
        specialists_dir=specialists_dir,
        acks_path=acks_path,
    )
    txn.rollback_disk()

    doc = _read_registry(registry_path)
    assert doc["plugins"] == [before_entry]
    assert (slug_dir / "active.yaml").read_text(encoding="utf-8") == "pre-mutation"

    identity = install_consent_identity(
        component_id=ack_record["component_id"], version=ack_record["version"],
        root_digest=ack_record["component_checksum"], slug=ack_record["slug"])
    assert SpecialistInstallAckStore(acks_path).get(identity) is not None


def test_bundletxn_rollback_disk_deletes_files_recorded_as_absent(tmp_path):
    registry_path = tmp_path / "registry.json"
    specialists_dir = tmp_path / "spec"
    acks_path = tmp_path / "acks.json"
    _write_registry(registry_path, [])

    slug_dir = specialists_dir / "mtg"
    slug_dir.mkdir(parents=True)
    (slug_dir / "desired.yaml").write_text("created-mid-mutation", encoding="utf-8")

    txn = journal.BundleTxn(
        journal_path=tmp_path / "unused.json",
        slug="mtg",
        before_entries=[],
        before_tuple_files={"desired.yaml": None},
        ack_records=[],
        registry_path=registry_path,
        specialists_dir=specialists_dir,
        acks_path=acks_path,
    )
    txn.rollback_disk()

    assert not (slug_dir / "desired.yaml").exists()


def test_fsync_write_completes_under_short_writes(tmp_path, monkeypatch):
    """P2-6: _fsync_write must loop until the WHOLE buffer is written — a
    single os.write() may write fewer bytes than requested (a short write),
    which would silently truncate the journal (the torn-payload state
    os.replace was chosen to avoid). Force ≤8-byte writes and assert the full
    payload still lands intact."""
    import os

    real_write = os.write

    def _short_write(fd, data):
        return real_write(fd, data[:8])   # at most 8 bytes/call → forces the loop

    monkeypatch.setattr(journal.os, "write", _short_write)

    payload = ("x" * 250) + "\n"           # far larger than one short write
    target = tmp_path / "journal.json"
    journal._fsync_write(target, payload)

    assert target.read_text(encoding="utf-8") == payload


def test_journal_snapshots_and_restores_the_rollback_tmp(tmp_path):
    """Sol round-2 (#339/#346): active.yaml.rollback-tmp is the pending
    prior-rotation journal InstanceDir.commit_desired_to_active leaves behind
    when a rotation fails or a crash interrupts it. A bundle transaction that
    later mutates the slug dir overwrites it, so bundle compensation must
    restore (or re-remove) it like every other tuple/sidecar file — otherwise
    a compensated crash silently discards the pending rotation and the
    rollback generation it carried."""
    import specialist_bundle_journal as journal
    from specialist_install import _tuple_files_snapshot

    assert "active.yaml.rollback-tmp" in journal.TUPLE_FILENAMES

    slug_dir = tmp_path / "specialists" / "mtg"
    slug_dir.mkdir(parents=True)
    (slug_dir / "active.yaml").write_text("active: current\n", encoding="utf-8")
    (slug_dir / "active.yaml.rollback-tmp").write_text(
        "pending: rotation\n", encoding="utf-8")

    snap = _tuple_files_snapshot(slug_dir)
    assert snap["active.yaml.rollback-tmp"] == "pending: rotation\n"

    txn = journal.BundleTxn(
        slug="mtg", journal_path=tmp_path / "j.json",
        registry_path=tmp_path / "registry.json",
        specialists_dir=tmp_path / "specialists",
        acks_path=tmp_path / "acks.json",
        before_entries=[], before_tuple_files=snap, ack_records=[],
        new_artifact_ids=(), removed_artifact_ids=(),
    )
    # The transaction clobbers the tmp (a later commit's copy step would).
    (slug_dir / "active.yaml.rollback-tmp").write_text(
        "clobbered: yes\n", encoding="utf-8")
    txn.rollback_disk()
    assert (slug_dir / "active.yaml.rollback-tmp").read_text(
        encoding="utf-8") == "pending: rotation\n"

    # And a tmp recorded ABSENT is removed on rollback.
    snap_absent = dict(snap, **{"active.yaml.rollback-tmp": None})
    txn2 = journal.BundleTxn(
        slug="mtg", journal_path=tmp_path / "j2.json",
        registry_path=tmp_path / "registry.json",
        specialists_dir=tmp_path / "specialists",
        acks_path=tmp_path / "acks.json",
        before_entries=[], before_tuple_files=snap_absent, ack_records=[],
        new_artifact_ids=(), removed_artifact_ids=(),
    )
    txn2.rollback_disk()
    assert not (slug_dir / "active.yaml.rollback-tmp").exists()


# --------------------------------------------------------------------------
# #490: fresh-install rollback must remove the op-symlink materialization
# created — otherwise every later reload re-discovers the slug through it,
# attempts the load, and fails (role overlay rebuilt WITHOUT the slug).
# --------------------------------------------------------------------------

def _mk_op_symlink(agents_dir, slug="mtg"):
    """A materialized operational dir exactly as materialize writes it:
    `.{slug}.material-<32hex>` content dir + `<slug>` symlink at the top."""
    agents_dir.mkdir(parents=True, exist_ok=True)
    content = agents_dir / f".{slug}.material-{'c' * 32}"
    content.mkdir()
    (content / "runtime.yaml").write_text("role: mtg\n", encoding="utf-8")
    (agents_dir / slug).symlink_to(content.name)
    return content


def test_bundletxn_rollback_disk_fresh_install_removes_op_symlink(tmp_path):
    """#490: before-state with NO active.yaml (fresh install) — rollback
    removes the op-symlink AND its contained content dir."""
    registry_path = tmp_path / "registry.json"
    agents_dir = tmp_path / "agents"
    _write_registry(registry_path, [])
    content = _mk_op_symlink(agents_dir)

    txn = journal.BundleTxn(
        journal_path=tmp_path / "unused.json",
        slug="mtg",
        before_entries=[],
        before_tuple_files={"active.yaml": None},
        ack_records=[],
        registry_path=registry_path,
        specialists_dir=tmp_path / "spec",
        acks_path=tmp_path / "acks.json",
        agents_specialists_dir=agents_dir,
    )
    txn.rollback_disk()

    link = agents_dir / "mtg"
    assert not link.is_symlink() and not link.exists()
    assert not content.exists()


def test_bundletxn_rollback_disk_upgrade_keeps_op_symlink(tmp_path):
    """Converse: an upgrade rollback (before-state HAD an active.yaml — the
    specialist stays installed) must NOT touch the op-symlink; the self-heal
    reconcile re-materializes it from the restored active tuple."""
    registry_path = tmp_path / "registry.json"
    agents_dir = tmp_path / "agents"
    _write_registry(registry_path, [])
    content = _mk_op_symlink(agents_dir)

    txn = journal.BundleTxn(
        journal_path=tmp_path / "unused.json",
        slug="mtg",
        before_entries=[],
        before_tuple_files={"active.yaml": "id: specialist:mtg\n"},
        ack_records=[],
        registry_path=registry_path,
        specialists_dir=tmp_path / "spec",
        acks_path=tmp_path / "acks.json",
        agents_specialists_dir=agents_dir,
    )
    txn.rollback_disk()

    assert (agents_dir / "mtg").is_symlink()
    assert content.is_dir()


def test_bundletxn_rollback_disk_noncontained_symlink_target_survives(tmp_path):
    """F2 containment discipline carried over: a cross-pointed/out-of-tree
    symlink target is NEVER rmtree'd — the symlink alone is unlinked."""
    registry_path = tmp_path / "registry.json"
    agents_dir = tmp_path / "agents"
    _write_registry(registry_path, [])
    agents_dir.mkdir(parents=True)
    outside = tmp_path / "outside-tree"
    outside.mkdir()
    (outside / "keep.txt").write_text("live data", encoding="utf-8")
    (agents_dir / "mtg").symlink_to(outside)

    txn = journal.BundleTxn(
        journal_path=tmp_path / "unused.json",
        slug="mtg",
        before_entries=[],
        before_tuple_files={"active.yaml": None},
        ack_records=[],
        registry_path=registry_path,
        specialists_dir=tmp_path / "spec",
        acks_path=tmp_path / "acks.json",
        agents_specialists_dir=agents_dir,
    )
    txn.rollback_disk()

    assert not (agents_dir / "mtg").exists()
    assert (outside / "keep.txt").is_file()   # out-of-tree target untouched


def test_bundletxn_rollback_disk_serializes_behind_materialize_lock(tmp_path):
    """Sol diff r1 (#490): rollback's tuple/symlink restoration must hold
    MATERIALIZE_LOCK — unlocked, a concurrent reconcile that re-read a
    still-present active tuple could re-materialize the symlink AFTER the
    rollback removed it, resurrecting the orphan the fix exists to prevent.
    Serialized, either order converges (reconcile re-reads active under the
    lock; rollback deletes under the lock)."""
    import threading
    from personality_binding import MATERIALIZE_LOCK

    registry_path = tmp_path / "registry.json"
    agents_dir = tmp_path / "agents"
    _write_registry(registry_path, [])
    content = _mk_op_symlink(agents_dir)

    txn = journal.BundleTxn(
        journal_path=tmp_path / "unused.json",
        slug="mtg",
        before_entries=[],
        before_tuple_files={"active.yaml": None},
        ack_records=[],
        registry_path=registry_path,
        specialists_dir=tmp_path / "spec",
        acks_path=tmp_path / "acks.json",
        agents_specialists_dir=agents_dir,
    )

    done = threading.Event()

    def _run():
        txn.rollback_disk()
        done.set()

    assert MATERIALIZE_LOCK.acquire(timeout=5)
    try:
        t = threading.Thread(target=_run)
        t.start()
        # The restore phase must queue behind the held lock.
        assert not done.wait(0.3)
    finally:
        MATERIALIZE_LOCK.release()
    assert done.wait(5)
    t.join(5)
    assert not (agents_dir / "mtg").exists()
    assert not content.exists()
