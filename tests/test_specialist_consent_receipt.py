"""Task 7: consent identity tuple + ledger concurrency (spec §4, §3.5)."""
import json

from approval_values import NO_SETTINGS_DIGEST
from specialist_install_consent import (
    SpecialistInstallAckStore, install_consent_identity)


def test_legacy_identity_byte_stable():
    # receipt_digest="" MUST hash identically to the formula without a receipt
    # field: an empty receipt joins nothing to the hashed payload.
    from canonical_bytes import checksum_json
    legacy = checksum_json({
        "component_id": "casa/mtg", "version": "0.2.0",
        "component_checksum": "sha256:" + "b" * 64, "slug": "mtg",
        "values_digest": NO_SETTINGS_DIGEST})
    assert install_consent_identity(
        component_id="casa/mtg", version="0.2.0",
        root_digest="sha256:" + "b" * 64, slug="mtg",
        values_digest=NO_SETTINGS_DIGEST) == legacy


def test_receipt_digest_changes_identity():
    a = install_consent_identity(component_id="c/x", version="1.0.0",
                                 root_digest="sha256:" + "b" * 64, slug="x",
                                 values_digest=NO_SETTINGS_DIGEST)
    b = install_consent_identity(component_id="c/x", version="1.0.0",
                                 root_digest="sha256:" + "b" * 64, slug="x",
                                 receipt_digest="sha256:" + "c" * 64,
                                 values_digest=NO_SETTINGS_DIGEST)
    assert a != b


def test_record_and_reload_with_receipt(tmp_path):
    p = tmp_path / "acks.json"
    s = SpecialistInstallAckStore(p)
    ident = install_consent_identity(
        component_id="c/x", version="1.0.0", root_digest="sha256:" + "b" * 64,
        slug="x", receipt_digest="sha256:" + "c" * 64,
        values_digest=NO_SETTINGS_DIGEST)
    s.record(identity=ident, component_id="c/x", version="1.0.0",
             component_checksum="sha256:" + "b" * 64, slug="x",
             receipt_digest="sha256:" + "c" * 64,
             values_digest=NO_SETTINGS_DIGEST)
    assert SpecialistInstallAckStore(p).is_acked(ident)


def test_retire_slug_removes_all_and_returns_records(tmp_path):
    p = tmp_path / "acks.json"
    s = SpecialistInstallAckStore(p)
    for ver in ("1.0.0", "2.0.0"):
        ident = install_consent_identity(
            component_id="c/x", version=ver,
            root_digest="sha256:" + "b" * 64, slug="x",
            values_digest=NO_SETTINGS_DIGEST)
        s.record(identity=ident, component_id="c/x", version=ver,
                 component_checksum="sha256:" + "b" * 64, slug="x",
                 values_digest=NO_SETTINGS_DIGEST)
    other = install_consent_identity(
        component_id="c/y", version="1.0.0",
        root_digest="sha256:" + "d" * 64, slug="y",
        values_digest=NO_SETTINGS_DIGEST)
    s.record(identity=other, component_id="c/y", version="1.0.0",
             component_checksum="sha256:" + "d" * 64, slug="y",
             values_digest=NO_SETTINGS_DIGEST)
    removed = s.retire_slug("x")
    assert len(removed) == 2
    fresh = SpecialistInstallAckStore(p)
    assert fresh.is_acked(other) and not any(
        r["slug"] == "x" for r in json.loads(p.read_text())["acks"].values())


def test_concurrent_instances_do_not_clobber(tmp_path):
    # Two store INSTANCES over the same file: a record via instance B must
    # survive a later record via instance A (reload-under-lock, delta apply).
    p = tmp_path / "acks.json"
    a = SpecialistInstallAckStore(p)
    b = SpecialistInstallAckStore(p)
    ib = install_consent_identity(component_id="c/b", version="1.0.0",
                                  root_digest="sha256:" + "b" * 64, slug="b",
                                  values_digest=NO_SETTINGS_DIGEST)
    b.record(identity=ib, component_id="c/b", version="1.0.0",
             component_checksum="sha256:" + "b" * 64, slug="b",
             values_digest=NO_SETTINGS_DIGEST)
    ia = install_consent_identity(component_id="c/a", version="1.0.0",
                                  root_digest="sha256:" + "a" * 64, slug="a",
                                  values_digest=NO_SETTINGS_DIGEST)
    a.record(identity=ia, component_id="c/a", version="1.0.0",
             component_checksum="sha256:" + "a" * 64, slug="a",
             values_digest=NO_SETTINGS_DIGEST)
    fresh = SpecialistInstallAckStore(p)
    assert fresh.is_acked(ia) and fresh.is_acked(ib)


def test_snapshot_slug_returns_copies_and_does_not_mutate(tmp_path):
    p = tmp_path / "acks.json"
    s = SpecialistInstallAckStore(p)
    ident = install_consent_identity(component_id="c/x", version="1.0.0",
                                      root_digest="sha256:" + "b" * 64, slug="x",
                                      values_digest=NO_SETTINGS_DIGEST)
    s.record(identity=ident, component_id="c/x", version="1.0.0",
             component_checksum="sha256:" + "b" * 64, slug="x",
             values_digest=NO_SETTINGS_DIGEST)
    snap = s.snapshot_slug("x")
    assert len(snap) == 1 and snap[0]["slug"] == "x"
    snap[0]["slug"] = "mutated"
    # Mutating the returned copy must not affect the ledger.
    assert s.snapshot_slug("x")[0]["slug"] == "x"
    assert s.snapshot_slug("other-slug") == []


def test_restore_records_reinserts_by_recomputed_identity(tmp_path):
    p = tmp_path / "acks.json"
    s = SpecialistInstallAckStore(p)
    ident = install_consent_identity(
        component_id="c/x", version="1.0.0", root_digest="sha256:" + "b" * 64,
        slug="x", receipt_digest="sha256:" + "c" * 64,
        values_digest=NO_SETTINGS_DIGEST)
    s.record(identity=ident, component_id="c/x", version="1.0.0",
             component_checksum="sha256:" + "b" * 64, slug="x",
             receipt_digest="sha256:" + "c" * 64,
             values_digest=NO_SETTINGS_DIGEST)
    removed = s.retire_slug("x")
    assert removed and not s.is_acked(ident)
    s.restore_records(removed)
    assert s.is_acked(ident)


def test_restore_records_defaults_missing_receipt_digest_to_empty(tmp_path):
    p = tmp_path / "acks.json"
    s = SpecialistInstallAckStore(p)
    legacy_ident = install_consent_identity(
        component_id="c/x", version="1.0.0", root_digest="sha256:" + "b" * 64, slug="x",
        values_digest=NO_SETTINGS_DIGEST)
    legacy_record = {"component_id": "c/x", "version": "1.0.0",
                     "component_checksum": "sha256:" + "b" * 64, "slug": "x",
                     "values_digest": NO_SETTINGS_DIGEST, "ts": 0}
    s.restore_records([legacy_record])
    assert s.is_acked(legacy_ident)


def test_restore_records_is_slug_scoped_delta_not_whole_map_rewrite(tmp_path):
    p = tmp_path / "acks.json"
    s = SpecialistInstallAckStore(p)
    other = install_consent_identity(component_id="c/y", version="1.0.0",
                                      root_digest="sha256:" + "d" * 64, slug="y",
                                      values_digest=NO_SETTINGS_DIGEST)
    s.record(identity=other, component_id="c/y", version="1.0.0",
             component_checksum="sha256:" + "d" * 64, slug="y",
             values_digest=NO_SETTINGS_DIGEST)
    ident = install_consent_identity(component_id="c/x", version="1.0.0",
                                      root_digest="sha256:" + "b" * 64, slug="x",
                                      values_digest=NO_SETTINGS_DIGEST)
    s.restore_records([{"component_id": "c/x", "version": "1.0.0",
                        "component_checksum": "sha256:" + "b" * 64, "slug": "x",
                        "values_digest": NO_SETTINGS_DIGEST, "ts": 0}])
    # Restoring slug "x" must not disturb the pre-existing unrelated "y" ack.
    assert s.is_acked(other) and s.is_acked(ident)


def test_restore_records_empty_list_is_a_noop(tmp_path):
    p = tmp_path / "acks.json"
    s = SpecialistInstallAckStore(p)
    s.restore_records([])
    assert not p.exists()


# ---------------------------------------------------------------------------
# Whole-branch N: receipt pruning (consumed delete + boot age sweep)
# ---------------------------------------------------------------------------

def _mint_receipt(tmp_path):
    import specialist_receipt
    r = specialist_receipt.build_receipt(
        slug="mtg", component_repo="acme/mtg", component_ref="v0.2.0",
        component_revision="git:" + "a" * 40, component_subdir="",
        component_staged_path=str(tmp_path / "staged"), plugins=())
    specialist_receipt.persist(r, receipts_dir=tmp_path)
    return r


def test_delete_prunes_a_consumed_receipt(tmp_path):
    import specialist_receipt
    r = _mint_receipt(tmp_path)
    assert (tmp_path / f"{r.receipt_id}.json").exists()
    assert specialist_receipt.delete(r.receipt_id, receipts_dir=tmp_path) is True
    assert not (tmp_path / f"{r.receipt_id}.json").exists()
    # Idempotent + fail-closed on a non-opaque id.
    assert specialist_receipt.delete(r.receipt_id, receipts_dir=tmp_path) is False
    assert specialist_receipt.delete("../etc/passwd", receipts_dir=tmp_path) is False


def test_sweep_aged_removes_only_old_receipts(tmp_path):
    import os
    import time
    import specialist_receipt
    old = _mint_receipt(tmp_path)
    fresh = _mint_receipt(tmp_path)
    old_path = tmp_path / f"{old.receipt_id}.json"
    old_ts = time.time() - 8 * 24 * 3600
    os.utime(old_path, (old_ts, old_ts))
    removed = specialist_receipt.sweep_aged(receipts_dir=tmp_path)
    assert removed == 1
    assert not old_path.exists()
    assert (tmp_path / f"{fresh.receipt_id}.json").exists()


def test_sweep_aged_keeps_only_the_newest_pending_slug_receipt(tmp_path):
    """#331 (Sol r5-2 / Terra r6-1): an aged receipt whose slug still has a
    LIVE pending-configuration candidate is retained — but only the NEWEST
    one per slug; repeated pre-commit inspections must not pin unbounded
    receipts (and through them, staging trees) forever."""
    import os
    import time
    import specialist_receipt

    older = _mint_receipt(tmp_path)     # slug "mtg"
    newer = _mint_receipt(tmp_path)     # slug "mtg" — a later re-inspect
    older_path = tmp_path / f"{older.receipt_id}.json"
    newer_path = tmp_path / f"{newer.receipt_id}.json"
    now = time.time()
    os.utime(older_path, (now - 40 * 24 * 3600,) * 2)
    os.utime(newer_path, (now - 30 * 24 * 3600,) * 2)

    removed = specialist_receipt.sweep_aged(
        receipts_dir=tmp_path, keep_slugs={"mtg"})
    assert removed == 1
    assert not older_path.exists()   # superseded inspection: swept
    assert newer_path.exists()       # the resumable receipt: kept
    # Without the keep, the survivor sweeps too.
    removed = specialist_receipt.sweep_aged(receipts_dir=tmp_path)
    assert removed == 1
    assert not newer_path.exists()


def test_sweep_aged_keep_receipt_ids_beats_mtime(tmp_path):
    """Sol r6-2: an exact-id keep (the pending marker's receipt) is exempt
    even when a NEWER same-slug receipt exists — newest-by-mtime alone could
    keep a different root's receipt that cannot resume the pending tuple."""
    import os
    import time
    import specialist_receipt

    marked = _mint_receipt(tmp_path)    # the receipt the pending commit used
    newer = _mint_receipt(tmp_path)     # later inspection, different root
    marked_path = tmp_path / f"{marked.receipt_id}.json"
    newer_path = tmp_path / f"{newer.receipt_id}.json"
    now = time.time()
    os.utime(marked_path, (now - 40 * 24 * 3600,) * 2)
    os.utime(newer_path, (now - 30 * 24 * 3600,) * 2)

    removed = specialist_receipt.sweep_aged(
        receipts_dir=tmp_path, keep_receipt_ids={marked.receipt_id})
    assert removed == 1
    assert marked_path.exists()
    assert not newer_path.exists()


def test_pin_inv_spec_005_tampered_receipt_loads_as_none(tmp_path):
    """Pins INV-SPEC-005: a receipt whose attested fields no longer match its
    recorded digest reads as absent, never as attested.

    Red case demonstrated: disabling the loader's digest-recompute comparison
    makes the tampered receipt load and fails this test.
    """
    import json

    import specialist_receipt as sr

    receipt = sr.build_receipt(
        slug="x", component_repo="o/r", component_ref="v1",
        component_revision="rev", component_subdir="", component_staged_path="/s",
        plugins=(),
    )
    sr.persist(receipt, receipts_dir=tmp_path)
    assert sr.load(receipt.receipt_id, receipts_dir=tmp_path) is not None

    path = tmp_path / f"{receipt.receipt_id}.json"
    raw = json.loads(path.read_text())
    raw["component_ref"] = "v2-tampered"
    path.write_text(json.dumps(raw))
    assert sr.load(receipt.receipt_id, receipts_dir=tmp_path) is None
