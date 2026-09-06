"""#838 / INV-SPEC-014 — a slug that owes boot recovery accepts no new generation.

`tools._bundle_compensate` and every journaled writer's sync-phase `except` arm
leave an in-progress bundle journal on disk ON PURPOSE when `rollback_disk()`
raises (P1-1: never complete a journal you did not replay). That journal means
*undo me at boot*. Nothing used to fence the slug afterwards, so a later
generation committed normally and the next `reconcile_boot` restored the older
capture over it — or, on a second rollback failure, quarantined the slug and
dropped the newer generation's owned rows.

Every case here drives a REAL door: the real compensation coroutine, the real
lifecycle writers, the real journal module and the real boot reconciliation.
Only the FAILURE is injected, through the two triggers the journal module's own
docstrings name (an invalid registry, a raising `rollback_disk`).
"""
from __future__ import annotations

import asyncio
import errno
import json as _json
import os
import shutil
from pathlib import Path

import pytest

import plugin_registry
import specialist_bundle_journal as journal
import specialist_install
import specialist_receipt
from specialist_fixtures import write_bundled_plugin, write_minimal_component
from test_specialist_bundle_commit import _owned, _prep


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class _InlineAsyncio:
    """`tools.asyncio` with `to_thread` executed INLINE.

    The compensation path is a coroutine whose only suspension point on the
    failing-rollback arm is `asyncio.to_thread(txn.rollback_disk)`. Running it
    inline lets the real coroutine execute with no event loop, no executor and
    no socket — the reviewer sandbox denies a listener, and a red case must not
    need one. Everything else is delegated to the real module, so this is not a
    patch of `asyncio` itself (never patch `<module>.asyncio.sleep`).
    """

    def __init__(self, real=asyncio) -> None:
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def to_thread(self, fn, /, *args, **kwargs):
        return fn(*args, **kwargs)


def _finish_inline(coro):
    """Drive a coroutine that must complete without ever suspending."""
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise AssertionError("the compensation harness suspended; it must not")


def _inline_tools(monkeypatch):
    import tools
    monkeypatch.setattr(tools, "asyncio", _InlineAsyncio())
    return tools


def _journals(ops: Path) -> list[Path]:
    return sorted(p for p in ops.glob("*.json") if p.is_file())


def _states(ops: Path) -> list[str]:
    return [_json.loads(p.read_text(encoding="utf-8"))["state"] for p in _journals(ops)]


def _tuple_bytes(slug_dir: Path) -> dict:
    return {name: (slug_dir / name).read_bytes() if (slug_dir / name).is_file() else None
            for name in sorted(journal.TUPLE_FILENAMES)}


def _snapshot(ctx) -> dict:
    """Everything a refusal must leave untouched: registry bytes, every
    journalled tuple/sidecar file, the ack ledger, and the ops inventory."""
    reg = Path(ctx.kw["registry_path"])
    acks_path = Path(ctx.acks.path)
    ops = Path(ctx.kw["ops_dir"])
    return {
        "registry": reg.read_bytes() if reg.is_file() else None,
        "tuple": _tuple_bytes(Path(ctx.kw["specialists_dir"]) / ctx.slug),
        "acks": acks_path.read_bytes() if acks_path.is_file() else None,
        "ops": {p.name: p.read_bytes() for p in sorted(ops.iterdir())} if ops.is_dir() else {},
    }


def _prep_gen(tmp_path: Path, monkeypatch, base_ctx, *, ref: str, marker: str,
              version: str, sha: str) -> dict:
    """A further upgrade generation of the SAME slug: a new component tree with
    changed plugin content and a distinct version, inspected in upgrade mode and
    acknowledged. Generalises `_prep_v2` so three generations can exist.

    The fetch stub copies BYTES, never modes (`copy_function=shutil.copyfile`):
    the candidate gate executes on a read-only materialization.
    """
    import plugin_store
    from specialist_install_consent import install_consent_identity
    from specialist_registry import InstalledSpecialistIndex

    comp, mpath = write_minimal_component(tmp_path / marker, slug=base_ctx.slug)
    write_bundled_plugin(comp, "mtg")
    (comp / "plugins" / "mtg" / "README.md").write_text(marker, encoding="utf-8")
    digest = "sha256:" + plugin_store.content_checksum(comp / "plugins" / "mtg")
    manifest = _json.loads(mpath.read_text(encoding="utf-8"))
    manifest["version"] = version
    manifest["dependencies"].append({
        "kind": "plugin/implementation", "identifier": "mtg", "digest": digest,
        "source": {"type": "bundled", "path": "plugins/mtg"}})
    mpath.write_text(_json.dumps(manifest), encoding="utf-8")

    def _stub(repo, ref_, subdir, dest, *, expected_revision=None):
        src = comp / subdir if subdir else comp
        shutil.copytree(src, dest, copy_function=shutil.copyfile)
        return sha

    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _stub)
    idx = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "installed-index"))
    idx.load()
    insp = specialist_install.inspect_specialist_repo(
        "org/repo", ref, staging_root=tmp_path / f"staging-{marker}", installed_index=idx,
        mode="upgrade", target_slug=base_ctx.slug,
        specialists_dir=tmp_path / "specialists", receipts_dir=tmp_path / "receipts")
    receipt = specialist_receipt.load(insp.receipt_id, receipts_dir=tmp_path / "receipts")
    assert receipt is not None
    identity = install_consent_identity(
        component_id=insp.component_id, version=insp.version, root_digest=insp.root_digest,
        slug=base_ctx.slug, receipt_digest=insp.receipt_digest)
    base_ctx.acks.record(
        identity=identity, component_id=insp.component_id, version=insp.version,
        component_checksum=insp.root_digest, slug=base_ctx.slug,
        receipt_digest=insp.receipt_digest)
    return dict(base_ctx.kw, inspection=insp, receipt=receipt, slug=base_ctx.slug)


@pytest.fixture(autouse=True)
def _fresh_snapshot(tmp_path):
    """Point the process-global registry snapshot at this test's tree."""
    plugin_registry.reload_snapshot(registry_path=tmp_path / "snap-registry.json",
                                    store_root=tmp_path / "snap-store")
    yield


# ---------------------------------------------------------------------------
# 1. The tool-layer door: _bundle_compensate whose disk rollback failed
# ---------------------------------------------------------------------------

def test_failed_tool_compensation_fences_a_later_generation(tmp_path: Path, monkeypatch) -> None:
    """G0 installed, G1 upgraded, G1's compensation fails on an invalid
    registry and leaves its journal in progress. The registry is then REPAIRED
    — the injected fault is gone — and a G2 upgrade is attempted through the
    real writer.

    On the pre-fix tree G2 commits, and the very next `reconcile_boot` restores
    G1's captured before-state (G0) over it. The fence must refuse G2 instead,
    with nothing written; after a boot the same G2 upgrade succeeds and survives
    the boot after that.
    """
    tools = _inline_tools(monkeypatch)
    ops = tmp_path / "ops"
    reg = tmp_path / "registry.json"
    slug_dir = tmp_path / "specialists" / "mtg"

    ctx = _prep(tmp_path, monkeypatch)
    _g0, txn0 = specialist_install.commit_specialist_install(**ctx.kw)
    journal.complete(txn0.journal_path)          # the tool layer's job in production
    g0_root = _json.loads(_json.dumps(_owned(reg, "mtg")))[0]["artifact_id"]

    kw1 = _prep_gen(tmp_path, monkeypatch, ctx, ref="v2", marker="v2",
                    version="0.2.0", sha="b" * 40)
    g1, txn1 = specialist_install.upgrade_specialist(**kw1)
    assert g1.state == "active"
    good_registry = reg.read_bytes()
    g1_artifact = _owned(reg, "mtg")[0]["artifact_id"]
    assert g1_artifact != g0_root

    # --- the door: rollback_disk raises on an invalid registry ---
    reg.write_text("{ this is not a registry", encoding="utf-8")
    assert _finish_inline(tools._bundle_compensate(txn1)) == {
        "disk_ok": False, "runtime_ok": False}
    assert _states(ops) == ["in-progress"]
    standing_name = _journals(ops)[0].name
    standing_bytes = _journals(ops)[0].read_bytes()

    # --- the fault is repaired; the debt is not ---
    reg.write_bytes(good_registry)
    assert _owned(reg, "mtg")[0]["artifact_id"] == g1_artifact

    kw2 = _prep_gen(tmp_path, monkeypatch, ctx, ref="v3", marker="v3",
                    version="0.3.0", sha="c" * 40)
    before = _snapshot(ctx)
    refusals: list = []
    successes: list = []
    try:
        g2_attempt, txn2_attempt = specialist_install.upgrade_specialist(**kw2)
    except specialist_install.SpecialistInstallError as exc:
        refusals.append(exc)
    else:
        # The pre-fix outcome. Finish it the way the tool layer would, so the
        # reproduction runs on to the boot that reverts it instead of stopping
        # at the first red assertion.
        successes.append(g2_attempt)
        journal.complete(txn2_attempt.journal_path)
    after_attempt = _snapshot(ctx)

    # --- boot resolves the debt: exactly one rollback, to G1's capture (G0) ---
    actions = journal.reconcile_boot(
        ops_dir=ops, registry_path=reg, specialists_dir=tmp_path / "specialists",
        acks_path=ctx.acks.path, receipts_dir=tmp_path / "receipts",
        personas_dir=tmp_path / "personas",
        agents_specialists_dir=tmp_path / "agents")
    post_boot = [e["artifact_id"] for e in _owned(reg, "mtg")]

    # PRIMARY RED. On the pre-fix tree the later generation commits and this
    # boot restores G1's capture over it.
    assert (len(refusals), len(successes)) == (1, 0), (
        f"a later generation committed while recovery was owed; boot then left "
        f"owned artifacts {post_boot} (G0={g0_root!r}, G1={g1_artifact!r})")
    assert refusals[0].kind == "recovery_pending"
    assert "mtg" in refusals[0].detail
    assert after_attempt == before                # nothing durable was written
    assert after_attempt["ops"] == {standing_name: standing_bytes}   # no second journal
    assert [a for a in actions if a["slug"] == "mtg"] == [
        {"slug": "mtg", "action": "rolled_back"}]
    assert _journals(ops) == []
    assert [e["artifact_id"] for e in _owned(reg, "mtg")] == [g0_root]

    # --- and the same upgrade now goes through ---
    kw3 = _prep_gen(tmp_path, monkeypatch, ctx, ref="v4", marker="v4",
                    version="0.3.0", sha="d" * 40)
    g2, txn2 = specialist_install.upgrade_specialist(**kw3)
    journal.complete(txn2.journal_path)
    assert g2.state == "active"
    g2_artifact = _owned(reg, "mtg")[0]["artifact_id"]
    assert g2_artifact not in (g0_root, g1_artifact)
    after_retry = (_owned(reg, "mtg"), _tuple_bytes(slug_dir))

    # --- the second boot leaves the surviving generation exactly as it is ---
    actions2 = journal.reconcile_boot(
        ops_dir=ops, registry_path=reg, specialists_dir=tmp_path / "specialists",
        acks_path=ctx.acks.path, receipts_dir=tmp_path / "receipts",
        personas_dir=tmp_path / "personas",
        agents_specialists_dir=tmp_path / "agents")
    assert [a for a in actions2 if a["slug"] == "mtg"] == []
    assert (_owned(reg, "mtg"), _tuple_bytes(slug_dir)) == after_retry


# ---------------------------------------------------------------------------
# 2. The sync-phase door: a writer's except arm whose rollback_disk raised
# ---------------------------------------------------------------------------

def test_failed_sync_rollback_fences_the_next_writer(tmp_path: Path, monkeypatch) -> None:
    """The second debt producer: a sync-phase failure whose own `rollback_disk`
    raises leaves the journal in progress (P1-1). The injection is then REMOVED,
    so the later writer's refusal cannot be attributed to a live fault."""
    ops = tmp_path / "ops"
    reg = tmp_path / "registry.json"
    ctx = _prep(tmp_path, monkeypatch)

    import personality_binding
    real_commit = personality_binding.InstanceDir.commit_desired_to_active
    real_rollback = journal.BundleTxn.rollback_disk

    def _boom_commit(self):
        raise RuntimeError("tuple commit exploded")

    def _boom_rollback(self):
        raise RuntimeError("registry unreadable — rollback failed")

    monkeypatch.setattr(personality_binding.InstanceDir,
                        "commit_desired_to_active", _boom_commit)
    monkeypatch.setattr(journal.BundleTxn, "rollback_disk", _boom_rollback)
    with pytest.raises(RuntimeError):
        specialist_install.commit_specialist_install(**ctx.kw)
    assert _states(ops) == ["in-progress"]

    # the injections are gone; only the debt remains
    monkeypatch.setattr(personality_binding.InstanceDir,
                        "commit_desired_to_active", real_commit)
    monkeypatch.setattr(journal.BundleTxn, "rollback_disk", real_rollback)
    assert _owned(reg, "mtg")                       # the swap did commit

    before = _snapshot(ctx)
    with pytest.raises(specialist_install.SpecialistInstallError) as refusal:
        specialist_install.uninstall_specialist(
            slug="mtg", bundle=True, acks=ctx.acks,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents",
            registry_path=reg, ops_dir=ops)

    assert refusal.value.kind == "recovery_pending"
    assert _snapshot(ctx) == before                 # acks not retired, nothing swapped
    assert _states(ops) == ["in-progress"]          # no second journal


# ---------------------------------------------------------------------------
# 3. Every writer, in BOTH arms, refuses before its first durable write
# ---------------------------------------------------------------------------

def _stand_a_journal(ctx, *, slug: str = "mtg") -> Path:
    """One real, replayable journal for `slug` in the writers' ops directory,
    written by the real `begin` — the shape both debt producers leave behind."""
    return journal.begin(
        "install", slug, before_entries=[], before_tuple_files={}, ack_records=[],
        ops_dir=Path(ctx.kw["ops_dir"]))


def test_install_refuses_before_publishing_anything(tmp_path: Path, monkeypatch) -> None:
    """Placement probe: the fence must precede the component CAS publish, which
    `commit_specialist_install` performs BEFORE `begin()`."""
    ctx = _prep(tmp_path, monkeypatch)
    _stand_a_journal(ctx)
    store = Path(ctx.kw["specialists_dir"]) / "store"
    before = _snapshot(ctx)

    with pytest.raises(specialist_install.SpecialistInstallError) as refusal:
        specialist_install.commit_specialist_install(**ctx.kw)

    assert refusal.value.kind == "recovery_pending"
    assert _snapshot(ctx) == before
    assert not store.exists()                       # zero CAS publications
    assert not (Path(ctx.kw["specialists_dir"]) / "mtg").exists()
    assert len(_journals(Path(ctx.kw["ops_dir"]))) == 1


def test_upgrade_refuses_in_both_arms(tmp_path: Path, monkeypatch) -> None:
    ops = tmp_path / "ops"
    ctx = _prep(tmp_path, monkeypatch)
    _inst, txn0 = specialist_install.commit_specialist_install(**ctx.kw)
    journal.complete(txn0.journal_path)
    kw2 = _prep_gen(tmp_path, monkeypatch, ctx, ref="v2", marker="v2",
                    version="0.2.0", sha="b" * 40)
    _stand_a_journal(ctx)
    before = _snapshot(ctx)

    # journaled arm (a receipt is present)
    with pytest.raises(specialist_install.SpecialistInstallError) as journaled:
        specialist_install.upgrade_specialist(**kw2)
    assert journaled.value.kind == "recovery_pending"

    # unjournaled arm: `receipt is None` returns before begin() is ever reached,
    # so a begin()-keyed fence would not cover it. Its own refusal
    # (receipt_required) must NOT be what answers here.
    with pytest.raises(specialist_install.SpecialistInstallError) as legacy:
        specialist_install.upgrade_specialist(**dict(kw2, receipt=None))
    assert legacy.value.kind == "recovery_pending"

    assert _snapshot(ctx) == before
    assert len(_journals(ops)) == 1


def test_rollback_refuses_in_both_arms_before_completing_a_rotation(
    tmp_path: Path, monkeypatch,
) -> None:
    """Placement probe: `rollback_specialist` completes a pending tuple/sidecar
    rotation under MATERIALIZE_LOCK BEFORE `begin()`. A fence placed at `begin`
    would already have promoted it."""
    ops = tmp_path / "ops"
    reg = tmp_path / "registry.json"
    slug_dir = tmp_path / "specialists" / "mtg"
    ctx = _prep(tmp_path, monkeypatch)
    _inst, txn0 = specialist_install.commit_specialist_install(**ctx.kw)
    journal.complete(txn0.journal_path)
    kw2 = _prep_gen(tmp_path, monkeypatch, ctx, ref="v2", marker="v2",
                    version="0.2.0", sha="b" * 40)
    _inst2, txn1 = specialist_install.upgrade_specialist(**kw2)
    journal.complete(txn1.journal_path)
    assert (slug_dir / "active.prior.yaml").is_file()   # a retained generation

    _stand_a_journal(ctx)
    before = _snapshot(ctx)

    with pytest.raises(specialist_install.SpecialistInstallError) as journaled:
        specialist_install.rollback_specialist(
            slug="mtg", bundle=True, acks=ctx.acks,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents",
            registry_path=reg, plugin_store_root=tmp_path / "store", ops_dir=ops)
    assert journaled.value.kind == "recovery_pending"

    with pytest.raises(specialist_install.SpecialistInstallError) as direct:
        specialist_install.rollback_specialist(
            slug="mtg", bundle=False,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents",
            registry_path=reg, ops_dir=ops)
    assert direct.value.kind == "recovery_pending"

    assert _snapshot(ctx) == before
    assert len(_journals(ops)) == 1


def test_uninstall_refuses_in_both_arms_before_retiring_acks(
    tmp_path: Path, monkeypatch,
) -> None:
    """Placement probe: `uninstall_specialist` retires the slug's consent acks
    BEFORE `begin()`, and its `bundle=False` arm writes no journal at all."""
    ops = tmp_path / "ops"
    reg = tmp_path / "registry.json"
    ctx = _prep(tmp_path, monkeypatch, with_plugin=False)
    _inst, txn0 = specialist_install.commit_specialist_install(**ctx.kw)
    journal.complete(txn0.journal_path)
    assert _owned(reg, "mtg") == []                 # the bundle=False arm is legal here
    assert ctx.acks.snapshot_slug("mtg")

    _stand_a_journal(ctx)
    before = _snapshot(ctx)

    with pytest.raises(specialist_install.SpecialistInstallError) as journaled:
        specialist_install.uninstall_specialist(
            slug="mtg", bundle=True, acks=ctx.acks,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents",
            registry_path=reg, ops_dir=ops)
    assert journaled.value.kind == "recovery_pending"

    with pytest.raises(specialist_install.SpecialistInstallError) as direct:
        specialist_install.uninstall_specialist(
            slug="mtg", bundle=False, acks=ctx.acks,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents",
            registry_path=reg, ops_dir=ops)
    assert direct.value.kind == "recovery_pending"

    assert _snapshot(ctx) == before                 # acks intact, slug tree intact
    assert (tmp_path / "specialists" / "mtg").is_dir()
    assert len(_journals(ops)) == 1


def test_persona_override_refuses_in_both_arms(tmp_path: Path, monkeypatch) -> None:
    from test_wholebranch_security_fixes import (
        _load_specialist_persona_role, _publish_installed_copy,
    )

    ops = tmp_path / "ops"
    reg = tmp_path / "registry.json"
    specialists_dir = tmp_path / "specialists"
    ctx = _prep(tmp_path, monkeypatch)
    _inst, txn0 = specialist_install.commit_specialist_install(**ctx.kw)
    journal.complete(txn0.journal_path)
    persona, role = _load_specialist_persona_role(specialists_dir, "mtg")
    _publish_installed_copy(persona, specialists_dir, "mtg", tmp_path, monkeypatch)

    from persona_install import apply_persona_override

    _stand_a_journal(ctx)
    before = _snapshot(ctx)

    with pytest.raises(specialist_install.SpecialistInstallError) as journaled:
        apply_persona_override(
            target_role_id="specialist:mtg", persona=persona, role=role,
            instance_dir_root=specialists_dir / "mtg",
            candidate_validator=lambda p, b: None,
            bundle=True, acks=ctx.acks, registry_path=reg, ops_dir=ops)
    assert journaled.value.kind == "recovery_pending"

    with pytest.raises(specialist_install.SpecialistInstallError) as direct:
        apply_persona_override(
            target_role_id="specialist:mtg", persona=persona, role=role,
            instance_dir_root=specialists_dir / "mtg",
            candidate_validator=lambda p, b: None, ops_dir=ops)
    assert direct.value.kind == "recovery_pending"

    assert _snapshot(ctx) == before
    assert len(_journals(ops)) == 1


# ---------------------------------------------------------------------------
# 4. What counts as debt — driven through a real writer, one probe per row
# ---------------------------------------------------------------------------

_OPID = "0" * 32
_OTHER = "1" * 32


def _valid_journal_bytes(slug: str) -> bytes:
    return _json.dumps({
        "schema_version": journal.SCHEMA_VERSION, "op": "install", "slug": slug,
        "state": "in-progress",
        "before": {"registry_entries": [], "tuple_files": {}, "ack_records": []},
        "receipt_digest": "", "consent_identity": "", "target_root": "",
        "steps_done": [],
    }, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _complete_journal_bytes(slug: str) -> bytes:
    payload = _json.loads(_valid_journal_bytes(slug))
    payload["state"] = "complete"
    return _json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _row_replay_same(ops, monkeypatch):
    (ops / f"mtg.{_OPID}.json").write_bytes(_valid_journal_bytes("mtg"))


def _row_invalid_same(ops, monkeypatch):
    (ops / f"mtg.{_OPID}.json").write_bytes(b"{ not json")


def _row_slug_mismatch(ops, monkeypatch):
    # filename says mtg, payload says finance -> INVALID, quarantines mtg at boot
    (ops / f"mtg.{_OPID}.json").write_bytes(_valid_journal_bytes("finance"))


def _row_unreadable_same(ops, monkeypatch):
    target = ops / f"mtg.{_OPID}.json"
    target.write_bytes(_valid_journal_bytes("mtg"))
    real = Path.read_text

    def _read_text(self, *args, **kwargs):
        if Path(self) == target:
            raise OSError(errno.EIO, "injected read failure")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)


def _row_unparseable_name(ops, monkeypatch):
    (ops / "not-a-journal.json").write_bytes(_valid_journal_bytes("mtg"))


def _row_write_temporary(ops, monkeypatch):
    # `_fsync_write`'s crash residue. Boot's pre-scan sweep DELETES it before
    # any classification, so it is neither replayed nor quarantined and it is
    # not debt. (Residual, stated in the prose: if that unlink fails, boot's
    # classification loop reaches quarantine_all instead.)
    (ops / f"mtg.{_OPID}.json.tmp-{'a' * 32}").write_bytes(_valid_journal_bytes("mtg"))


def _row_stat_error_same(ops, monkeypatch):
    # A symlink loop: os.stat raises ELOOP, a real "cannot tell" that is not
    # absence. No chmod, no syscall patching.
    a = ops / f"mtg.{_OPID}.json"
    b = ops / f"mtg.{_OTHER}.json"
    a.symlink_to(b)
    b.symlink_to(a)


def _row_symlink_to_journal(ops, monkeypatch):
    real = ops.parent / "elsewhere.json"
    real.write_bytes(_valid_journal_bytes("mtg"))
    (ops / f"mtg.{_OPID}.json").symlink_to(real)


def _row_complete_same(ops, monkeypatch):
    (ops / f"mtg.{_OPID}.json").write_bytes(_complete_journal_bytes("mtg"))


def _row_quarantined_residue(ops, monkeypatch):
    (ops / f"mtg.{_OPID}.json.quarantined").write_bytes(_valid_journal_bytes("mtg"))


def _row_replay_other_slug(ops, monkeypatch):
    (ops / f"finance.{_OPID}.json").write_bytes(_valid_journal_bytes("finance"))


def _row_invalid_other_slug(ops, monkeypatch):
    (ops / f"finance.{_OPID}.json").write_bytes(b"{ not json")


def _row_directory_entry(ops, monkeypatch):
    (ops / f"mtg.{_OPID}.json").mkdir()


def _row_fifo_entry(ops, monkeypatch):
    os.mkfifo(ops / f"mtg.{_OPID}.json")


def _row_broken_symlink(ops, monkeypatch):
    (ops / f"mtg.{_OPID}.json").symlink_to(ops / "nothing-here.json")


def _row_ops_absent(ops, monkeypatch):
    shutil.rmtree(ops)


def _row_empty(ops, monkeypatch):
    pass


_MATRIX = [
    ("replay_same_slug", _row_replay_same, True),
    ("invalid_same_slug", _row_invalid_same, True),
    ("payload_slug_mismatch", _row_slug_mismatch, True),
    ("unreadable_same_slug", _row_unreadable_same, True),
    ("unparseable_filename", _row_unparseable_name, True),
    ("write_temporary_residue", _row_write_temporary, False),
    ("stat_error_same_slug", _row_stat_error_same, True),
    ("symlink_to_replayable_journal", _row_symlink_to_journal, True),
    ("complete_same_slug", _row_complete_same, False),
    ("quarantined_residue", _row_quarantined_residue, False),
    ("replay_other_slug", _row_replay_other_slug, False),
    ("invalid_other_slug", _row_invalid_other_slug, False),
    ("directory_named_like_a_journal", _row_directory_entry, False),
    ("fifo_named_like_a_journal", _row_fifo_entry, False),
    ("broken_symlink", _row_broken_symlink, False),
    ("ops_directory_absent", _row_ops_absent, False),
    ("empty_ops_directory", _row_empty, False),
]


@pytest.mark.parametrize("name,place,blocks", _MATRIX, ids=[r[0] for r in _MATRIX])
def test_recovery_debt_matrix_through_a_real_writer(
    tmp_path: Path, monkeypatch, name, place, blocks,
) -> None:
    """One ops-directory state per row, judged by whether a real journaled
    uninstall of `mtg` — which succeeds against a clean ops directory — is
    refused. Behavioural on purpose: the row is what the WRITER does, not what
    a helper returns."""
    ops = tmp_path / "ops"
    reg = tmp_path / "registry.json"
    slug_dir = tmp_path / "specialists" / "mtg"
    ctx = _prep(tmp_path, monkeypatch, with_plugin=False)
    _inst, txn0 = specialist_install.commit_specialist_install(**ctx.kw)
    journal.complete(txn0.journal_path)
    assert slug_dir.is_dir()

    place(ops, monkeypatch)

    def _uninstall():
        return specialist_install.uninstall_specialist(
            slug="mtg", bundle=True, acks=ctx.acks,
            specialists_dir=tmp_path / "specialists",
            agents_specialists_dir=tmp_path / "agents",
            registry_path=reg, ops_dir=ops)

    if blocks:
        before_acks = Path(ctx.acks.path).read_bytes()
        with pytest.raises(specialist_install.SpecialistInstallError) as refusal:
            _uninstall()
        assert refusal.value.kind == "recovery_pending"
        assert slug_dir.is_dir()                                # not deleted
        assert Path(ctx.acks.path).read_bytes() == before_acks  # not retired
    else:
        txn = _uninstall()
        journal.complete(txn.journal_path)
        assert not slug_dir.exists()


# ---------------------------------------------------------------------------
# 5. Negative controls — an empty ops directory refuses nothing
# ---------------------------------------------------------------------------

def test_a_clean_ops_directory_refuses_nothing(tmp_path: Path, monkeypatch) -> None:
    """The counterweight to every refusal above: with no debt standing, each
    writer still does its job. A fence that refused unconditionally would pass
    every other test in this file and fail this one."""
    from test_wholebranch_security_fixes import (
        _load_specialist_persona_role, _publish_installed_copy,
    )

    ops = tmp_path / "ops"
    reg = tmp_path / "registry.json"
    specialists_dir = tmp_path / "specialists"
    slug_dir = specialists_dir / "mtg"

    ctx = _prep(tmp_path, monkeypatch)
    inst, txn0 = specialist_install.commit_specialist_install(**ctx.kw)
    journal.complete(txn0.journal_path)
    assert inst.state == "active"

    kw2 = _prep_gen(tmp_path, monkeypatch, ctx, ref="v2", marker="v2",
                    version="0.2.0", sha="b" * 40)
    inst2, txn1 = specialist_install.upgrade_specialist(**kw2)
    journal.complete(txn1.journal_path)
    assert inst2.state == "active"

    persona, role = _load_specialist_persona_role(specialists_dir, "mtg")
    _publish_installed_copy(persona, specialists_dir, "mtg", tmp_path, monkeypatch)
    staged = __import__("persona_install").apply_persona_override(
        target_role_id="specialist:mtg", persona=persona, role=role,
        instance_dir_root=slug_dir, candidate_validator=lambda p, b: None,
        bundle=True, acks=ctx.acks, registry_path=reg, ops_dir=ops)
    assert staged.binding.mode == "override"

    inst3, txn2 = specialist_install.rollback_specialist(
        slug="mtg", bundle=True, acks=ctx.acks, specialists_dir=specialists_dir,
        agents_specialists_dir=tmp_path / "agents", registry_path=reg,
        plugin_store_root=tmp_path / "store", ops_dir=ops)
    journal.complete(txn2.journal_path)
    assert inst3.state == "active"

    txn3 = specialist_install.uninstall_specialist(
        slug="mtg", bundle=True, acks=ctx.acks, specialists_dir=specialists_dir,
        agents_specialists_dir=tmp_path / "agents", registry_path=reg, ops_dir=ops)
    journal.complete(txn3.journal_path)
    assert not slug_dir.exists()
    assert _journals(ops) == []
