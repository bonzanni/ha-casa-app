"""#975: a specialist change never writes a lossy before-state capture back over
the operator's saved settings.

A bundle transaction journals a capture of every tuple file before it mutates,
and its compensation — in-process, and at boot after a crash — writes that
capture back. The capture is sanitized before it is journalled (#372 D9a: no
journal ever holds a secret's plain value), and sanitizing can lose a saved
setting two ways: the file's own component cannot be read back, so every key is
treated as secret; or, on an install or upgrade, the INCOMING component declares
secret a key the file's own component holds as a plain value. Written back, the
emptied capture tombstones `active.yaml` or `active.prior.yaml` into a state the
loader refuses.

The operator ruled twice on the issue:

* refuse at the door, before any journal exists, a change whose before-state
  cannot be recorded without loss — removal excepted, it stays unconditional;
* allow the upgrade that makes secret a setting the installed version keeps as a
  plain value: if it fails before the new version is active, the installed
  version's saved settings are left exactly as they were; if it fails after,
  the new version is kept and the failure reported.

Every case counts `begin` and `rollback_disk` calls and the component-store
reads made INSIDE `rollback_disk`, and reads tuple files as raw bytes or raw
YAML — never a status alone, and never through the tuple loader where a typed
refusal could stand in for a surviving value.
"""
from __future__ import annotations

import inspect
import os
import shutil
from pathlib import Path

import pytest
import yaml

import personality_binding
import specialist_bundle_journal
import specialist_install

from test_specialist_bundle_commit import (
    _UpgradeFixture, _declare_config_schema, _subdir_stub,
)

try:
    from tests.specialist_fixtures import write_minimal_component
except ImportError:
    from specialist_fixtures import write_minimal_component

SENTINEL = personality_binding.PRE_GUARD_SENTINEL


# ---------------------------------------------------------------------------
# instrumentation
# ---------------------------------------------------------------------------

def _obs() -> dict:
    return {"begins": 0, "rollbacks": 0, "store_reads": 0, "armed": False}


def _instrument(monkeypatch, obs, *, unreadable_inside_rollback=()):
    """Count `begin`, `rollback_disk`, and classification reads made while a
    `rollback_disk` is running. A root in `unreadable_inside_rollback` (or
    every root, when it is the string "all") answers `None` there — the
    component store cannot be read at the moment the compensation runs."""
    real_begin = specialist_bundle_journal.begin

    def _begin(*a, **kw):
        obs["begins"] += 1
        return real_begin(*a, **kw)
    monkeypatch.setattr(specialist_bundle_journal, "begin", _begin)

    real_rollback = specialist_bundle_journal.BundleTxn.rollback_disk

    def _rollback(self):
        obs["rollbacks"] += 1
        obs["armed"] = True
        try:
            return real_rollback(self)
        finally:
            obs["armed"] = False
    monkeypatch.setattr(
        specialist_bundle_journal.BundleTxn, "rollback_disk", _rollback)

    real_decl = specialist_install._declared_secret_names_for_root

    def _decl(root, *, specialists_dir=None):
        if obs["armed"]:
            obs["store_reads"] += 1
            if (unreadable_inside_rollback == "all"
                    or root in unreadable_inside_rollback):
                return None
        return real_decl(root, specialists_dir=specialists_dir)
    monkeypatch.setattr(
        specialist_install, "_declared_secret_names_for_root", _decl)


def _copies(slug_dir: Path, name: str, key: str = "k", value: str = "v") -> int:
    """1 when the named tuple file's raw snapshot still holds key=value."""
    path = slug_dir / name
    if not path.is_file():
        return 0
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return int((doc.get("config_snapshot") or {}).get(key) == value)


def _raw(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _files_holding(slug_dir: Path, needle: str) -> list[str]:
    """Every regular file under the slug directory whose bytes contain `needle`."""
    hits = []
    for path in sorted(slug_dir.rglob("*")):
        if path.is_file() and needle.encode("utf-8") in path.read_bytes():
            hits.append(path.name)
    return hits


def _pending_install(tmp_path: Path, monkeypatch):
    """A first install that lands pending-configuration holding k=v: the
    component requires `k` and `extra`, and only `k` is supplied."""
    import specialist_receipt
    from specialist_install_consent import (
        SpecialistInstallAckStore, install_consent_identity,
    )
    from specialist_registry import InstalledSpecialistIndex

    comp, mpath = write_minimal_component(tmp_path, slug="mtg")
    _declare_config_schema(comp, mpath, required=["k", "extra"], secret_names=[])
    monkeypatch.setattr(specialist_install, "resolve_and_fetch", _subdir_stub(comp))
    idx = InstalledSpecialistIndex(specialists_dir=str(tmp_path / "installed-index"))
    idx.load()
    insp = specialist_install.inspect_specialist_repo(
        "org/repo", "main", staging_root=tmp_path / "staging", installed_index=idx,
        receipts_dir=tmp_path / "receipts")
    receipt = specialist_receipt.load(insp.receipt_id, receipts_dir=tmp_path / "receipts")
    acks = SpecialistInstallAckStore(path=tmp_path / "acks.json")
    acks.record(
        identity=install_consent_identity(
            component_id=insp.component_id, version=insp.version,
            root_digest=insp.root_digest, slug=insp.slug,
            receipt_digest=insp.receipt_digest),
        component_id=insp.component_id, version=insp.version,
        component_checksum=insp.root_digest, slug=insp.slug,
        receipt_digest=insp.receipt_digest)
    common = dict(
        secret_names_provided=frozenset(), acks=acks,
        specialists_dir=tmp_path / "specialists",
        agents_specialists_dir=tmp_path / "agents",
        registry_path=tmp_path / "registry.json",
        plugin_store_root=tmp_path / "store", ops_dir=tmp_path / "ops")
    inst, txn = specialist_install.commit_specialist_install(
        inspection=insp, receipt=receipt, config={"k": "v"}, **common)
    specialist_bundle_journal.complete(txn.journal_path)
    assert inst.state == "pending-configuration"
    return insp, receipt, common, tmp_path / "specialists" / "mtg"


def _rollback_kw(fx) -> dict:
    return dict(
        slug="mtg", bundle=True, acks=fx.acks,
        specialists_dir=fx.common["specialists_dir"],
        agents_specialists_dir=fx.common["agents_specialists_dir"],
        registry_path=fx.common["registry_path"],
        plugin_store_root=fx.common["plugin_store_root"], ops_dir=fx.ops_dir)


def _reconcile(tmp_path: Path, fx) -> list[dict]:
    return specialist_bundle_journal.reconcile_boot(
        ops_dir=fx.ops_dir, registry_path=fx.common["registry_path"],
        specialists_dir=fx.common["specialists_dir"], acks_path=fx.acks.path,
        receipts_dir=tmp_path / "receipts",
        agents_specialists_dir=fx.common["agents_specialists_dir"])


def _upgraded_non_reclassifying(tmp_path: Path, monkeypatch):
    """A completed A -> B where both hold k=v as a plain setting, so the
    retained prior is A with k=v."""
    fx = _UpgradeFixture(tmp_path, monkeypatch, v2_required=("k",))
    fx.approve_v2()
    inst, txn = fx.upgrade(config={})
    specialist_bundle_journal.complete(txn.journal_path)
    assert inst.state == "active", inst.last_activation_error
    assert _copies(fx.slug_dir, "active.prior.yaml") == 1
    return fx


# ---------------------------------------------------------------------------
# The door (R1): refuse before any journal exists, and carry what it classified
# ---------------------------------------------------------------------------

def test_install_over_active_refuses_before_capture(tmp_path, monkeypatch) -> None:
    """RC-N1: the install commit handed another version's approved candidate
    for a slug that is already active. Its in-lock guard refuses — but after the
    journal exists, and the incoming component's secret declaration empties the
    capture of the active tuple it never opened."""
    fx = _UpgradeFixture(tmp_path, monkeypatch, v2_required=(), v2_secret_names=("k",))
    fx.approve_v2()
    active_before = (fx.slug_dir / "active.yaml").read_bytes()
    obs = _obs()
    _instrument(monkeypatch, obs)

    with pytest.raises(specialist_install.SpecialistInstallError):
        specialist_install.commit_specialist_install(
            inspection=fx.insp2, receipt=fx.receipt2, config={}, **fx.common)

    assert (obs["begins"], obs["rollbacks"]) == (0, 0)
    assert (fx.slug_dir / "active.yaml").read_bytes() == active_before
    assert _copies(fx.slug_dir, "active.yaml") == 1


def test_pending_recommit_compensation_uses_carried_declarations(
        tmp_path, monkeypatch) -> None:
    """RC-R1b: the pending candidate's configure re-commit, admitted, fails on
    the in-lock pending read AFTER the journal exists; the component store can
    be read before the call and not while the compensation runs. The restore
    must answer from what the transaction classified, not from the store."""
    insp, receipt, common, slug_dir = _pending_install(tmp_path, monkeypatch)
    desired_before = (slug_dir / "desired.yaml").read_bytes()
    obs = _obs()
    _instrument(monkeypatch, obs, unreadable_inside_rollback="all")
    faults = []
    real_desired = personality_binding.InstanceDir.desired

    def _desired(self):
        caller = inspect.currentframe().f_back.f_code.co_name
        if caller == "_refuse_if_active_present" and obs["begins"] >= 1:
            faults.append(caller)
            raise OSError(5, "Input/output error")
        return real_desired(self)
    monkeypatch.setattr(personality_binding.InstanceDir, "desired", _desired)

    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        specialist_install.commit_specialist_install(
            inspection=insp, receipt=receipt, config={"extra": "e"}, **common)

    assert ei.value.kind == "concurrent_mutation"
    assert len(faults) == 1
    assert (obs["begins"], obs["rollbacks"], obs["store_reads"]) == (1, 1, 0)
    assert _copies(slug_dir, "desired.yaml") == 1
    assert (slug_dir / "desired.yaml").read_bytes() == desired_before


def test_pending_only_rollback_refuses_before_capture(tmp_path, monkeypatch) -> None:
    """RC-PO: a rollback of a slug that has only a pending candidate has nothing
    to roll back to — decidable before any journal exists."""
    insp, receipt, common, slug_dir = _pending_install(tmp_path, monkeypatch)
    desired_before = (slug_dir / "desired.yaml").read_bytes()
    obs = _obs()
    _instrument(monkeypatch, obs, unreadable_inside_rollback="all")

    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        specialist_install.rollback_specialist(
            slug="mtg", bundle=True, acks=common["acks"],
            specialists_dir=common["specialists_dir"],
            agents_specialists_dir=common["agents_specialists_dir"],
            registry_path=common["registry_path"],
            plugin_store_root=common["plugin_store_root"], ops_dir=common["ops_dir"])

    assert ei.value.kind == "no_prior_tuple"
    assert (obs["begins"], obs["rollbacks"]) == (0, 0)
    assert _copies(slug_dir, "desired.yaml") == 1
    assert (slug_dir / "desired.yaml").read_bytes() == desired_before


def test_rollback_unclassifiable_prior_refuses_before_capture(
        tmp_path, monkeypatch) -> None:
    """RC-N2a: the retained prior's component cannot be read back from the
    store, so which of its saved settings are secret cannot be determined."""
    fx = _upgraded_non_reclassifying(tmp_path, monkeypatch)
    before = fx.bytes_of("active.yaml", "active.prior.yaml")
    shutil.rmtree(specialist_install.cas_store_dir(
        fx.insp1.root_digest, store_root=fx.store_root))
    obs = _obs()
    _instrument(monkeypatch, obs)

    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        specialist_install.rollback_specialist(**_rollback_kw(fx))

    assert ei.value.kind == "prior_schema_unreadable"
    assert (obs["begins"], obs["rollbacks"]) == (0, 0)
    assert _copies(fx.slug_dir, "active.prior.yaml") == 1
    assert fx.bytes_of("active.yaml", "active.prior.yaml") == before


def test_rollback_compensation_uses_carried_declarations(tmp_path, monkeypatch) -> None:
    """RC-N2b: both components readable at the door; a racing writer is refused
    in-lock after the journal exists, and the prior's component cannot be read
    while the compensation runs."""
    fx = _upgraded_non_reclassifying(tmp_path, monkeypatch)
    before = fx.bytes_of("active.yaml", "active.prior.yaml")
    prior_root = _raw(fx.slug_dir / "active.prior.yaml")["root"]
    obs = _obs()
    _instrument(monkeypatch, obs, unreadable_inside_rollback=(prior_root,))
    faults = []

    def _race(*a, **kw):
        faults.append(1)
        raise specialist_install.SpecialistInstallError(
            "concurrent_mutation", "a racing writer (test)")
    monkeypatch.setattr(specialist_install, "_require_active_unchanged", _race)

    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        specialist_install.rollback_specialist(**_rollback_kw(fx))

    assert ei.value.kind == "concurrent_mutation"
    assert len(faults) == 1
    assert (obs["begins"], obs["rollbacks"], obs["store_reads"]) == (1, 1, 0)
    assert _copies(fx.slug_dir, "active.prior.yaml") == 1
    assert fx.bytes_of("active.yaml", "active.prior.yaml") == before


def _override_call(fx, monkeypatch):
    """The persona-override bundle arm, called as the reference probe does: the
    override binding is the active binding (so nothing about the persona pack
    matters), and the role is the installed component's under live options."""
    import persona_install
    from role_artifact import load_role_artifact
    from role_slot import _ha_model_options, materialize_role

    idir = personality_binding.InstanceDir(fx.slug_dir)
    active = idir.active()
    _, _, ck = specialist_install.parse_component_root(active.root)
    role = materialize_role(
        source=load_role_artifact(specialist_install.cas_store_dir(
            ck, store_root=fx.store_root) / "role"),
        options=_ha_model_options())
    monkeypatch.setattr(personality_binding, "materialize_override_binding",
                        lambda **kw: active.binding)

    def _call():
        return persona_install._apply_specialist_override_locked(
            target_role_id="specialist:mtg", persona=object(), role=role,
            instance_dir=idir, override_source="test", bundle=True, acks=fx.acks,
            registry_path=fx.common["registry_path"], ops_dir=fx.ops_dir)
    return _call


def _arm_override_race(monkeypatch, faults):
    def _race(*a, **kw):
        faults.append(1)
        raise specialist_install.SpecialistInstallError(
            "concurrent_mutation", "a racing writer (test)")
    monkeypatch.setattr(specialist_install, "_require_active_unchanged", _race)


def test_persona_override_unclassifiable_prior_refuses_before_capture(
        tmp_path, monkeypatch) -> None:
    """RC-N4a: the persona override's pre-journal checks read only the ACTIVE
    root's store; the retained prior's component is gone."""
    fx = _upgraded_non_reclassifying(tmp_path, monkeypatch)
    call = _override_call(fx, monkeypatch)
    before = fx.bytes_of("active.yaml", "active.prior.yaml")
    shutil.rmtree(specialist_install.cas_store_dir(
        fx.insp1.root_digest, store_root=fx.store_root))
    obs = _obs()
    _instrument(monkeypatch, obs)
    faults: list = []
    _arm_override_race(monkeypatch, faults)

    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        call()

    assert ei.value.kind == "prior_schema_unreadable"
    assert faults == []
    assert (obs["begins"], obs["rollbacks"]) == (0, 0)
    assert _copies(fx.slug_dir, "active.prior.yaml") == 1
    assert fx.bytes_of("active.yaml", "active.prior.yaml") == before


def test_persona_override_compensation_uses_carried_declarations(
        tmp_path, monkeypatch) -> None:
    """RC-N4b: both components readable at the door; the in-lock re-check
    refuses a racing writer after the journal exists, and the prior's component
    cannot be read while the compensation runs."""
    fx = _upgraded_non_reclassifying(tmp_path, monkeypatch)
    call = _override_call(fx, monkeypatch)
    before = fx.bytes_of("active.yaml", "active.prior.yaml")
    prior_root = _raw(fx.slug_dir / "active.prior.yaml")["root"]
    obs = _obs()
    _instrument(monkeypatch, obs, unreadable_inside_rollback=(prior_root,))
    faults: list = []
    _arm_override_race(monkeypatch, faults)

    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        call()

    assert ei.value.kind == "concurrent_mutation"
    assert len(faults) == 1
    assert (obs["begins"], obs["rollbacks"], obs["store_reads"]) == (1, 1, 0)
    assert _copies(fx.slug_dir, "active.prior.yaml") == 1
    assert fx.bytes_of("active.yaml", "active.prior.yaml") == before


# ---------------------------------------------------------------------------
# The reclassifying upgrade (R2): before activation nothing the installed
# version saved is rewritten; after activation the new version is kept
# ---------------------------------------------------------------------------

class _ProcessLost(BaseException):
    """Stands for the process dying: nothing after the raise runs in-process."""


def test_rejected_secret_config_preserves_active_and_allows_retry(
        tmp_path, monkeypatch) -> None:
    """RC-A5: the upgrade makes `k` secret while the installed version holds it
    as a plain value, and is refused because the caller passed `k` as plain
    config. No fault of any kind is injected. INV-SPEC-003: the failed upgrade
    retains the complete prior active tuple — and the corrected retry works."""
    from personality_binding import InstanceDir

    fx = _UpgradeFixture(tmp_path, monkeypatch, v2_required=(), v2_secret_names=("k",))
    fx.approve_v2()
    active_path = fx.slug_dir / "active.yaml"
    active_before = active_path.read_bytes()

    with pytest.raises(specialist_install.SpecialistInstallError) as raised:
        fx.upgrade(config={"k": "incoming-value"})

    assert raised.value.kind == "secret_value_in_config"
    assert active_path.read_bytes() == active_before
    assert dict(InstanceDir(fx.slug_dir).active().config_snapshot) == {"k": "v"}
    assert len(fx.journals()) == 0

    fx.approve_v2()
    inst, txn = fx.upgrade(config={}, secret_names_provided=frozenset({"k"}))
    assert inst.state == "active"
    assert inst.active == InstanceDir(fx.slug_dir).active()
    _, _, checksum = specialist_install.parse_component_root(inst.active.root)
    assert checksum == fx.insp2.root_digest
    specialist_bundle_journal.complete(txn.journal_path)
    assert len(fx.journals()) == 0


def test_pre_activation_boot_replay_preserves_reclassified_tuple_files(
        tmp_path, monkeypatch) -> None:
    """RC-N5a: the process dies inside the reclassifying upgrade after the
    journal exists and before the new version is written active; boot replays
    the journal. Neither the active tuple nor the retained prior was touched by
    the upgrade, so replay must leave both exactly as they were."""
    import specialist_receipt

    fx = _UpgradeFixture(tmp_path, monkeypatch, v2_required=(),
                         v2_secret_names=("k",), value="prior-v")
    # A same-root re-commit of A with k=v retains A(k=prior-v) as the prior.
    receipt1 = specialist_receipt.load(
        fx.insp1.receipt_id, receipts_dir=tmp_path / "receipts")
    inst, txn = specialist_install.upgrade_specialist(
        slug="mtg", inspection=fx.insp1, receipt=receipt1,
        config={"k": "v"}, **fx.common)
    specialist_bundle_journal.complete(txn.journal_path)
    assert inst.state == "active", inst.last_activation_error
    fx.approve_v2()
    active_path = fx.slug_dir / "active.yaml"
    prior_path = fx.slug_dir / "active.prior.yaml"
    active_before, prior_before = active_path.read_bytes(), prior_path.read_bytes()
    assert _copies(fx.slug_dir, "active.yaml") == 1
    assert _copies(fx.slug_dir, "active.prior.yaml", value="prior-v") == 1

    counts = {"begins": 0, "stage_fault": 0, "interrupted": 0, "real": 0}
    real_begin = specialist_bundle_journal.begin
    real_stage = personality_binding.InstanceDir.stage_desired
    real_rollback = specialist_bundle_journal.BundleTxn.rollback_disk

    def _begin(*a, **kw):
        counts["begins"] += 1
        return real_begin(*a, **kw)

    def _stage(self, tuple_):
        if counts["stage_fault"] == 0:
            counts["stage_fault"] += 1
            raise OSError(5, "Input/output error")
        return real_stage(self, tuple_)

    def _die(self):
        counts["interrupted"] += 1
        raise _ProcessLost()

    monkeypatch.setattr(specialist_bundle_journal, "begin", _begin)
    monkeypatch.setattr(personality_binding.InstanceDir, "stage_desired", _stage)
    monkeypatch.setattr(specialist_bundle_journal.BundleTxn, "rollback_disk", _die)
    with pytest.raises(_ProcessLost):
        fx.upgrade(config={}, secret_names_provided=frozenset({"k"}))
    monkeypatch.setattr(personality_binding.InstanceDir, "stage_desired", real_stage)

    assert (counts["begins"], counts["stage_fault"], counts["interrupted"],
            counts["real"]) == (1, 1, 1, 0)
    assert len(fx.journals()) == 1
    assert active_path.read_bytes() == active_before
    assert prior_path.read_bytes() == prior_before

    boot_rollbacks = []

    def _counted(self):
        boot_rollbacks.append(1)
        return real_rollback(self)
    monkeypatch.setattr(specialist_bundle_journal.BundleTxn, "rollback_disk", _counted)
    _reconcile(tmp_path, fx)

    assert len(boot_rollbacks) == 1
    assert len(fx.journals()) == 0
    assert active_path.read_bytes() == active_before
    assert prior_path.read_bytes() == prior_before


PLAIN = "plain-k-7c1e"


def _activated_reclassifying(tmp_path, monkeypatch, obs):
    fx = _UpgradeFixture(tmp_path, monkeypatch, v2_required=(),
                         v2_secret_names=("k",), value=PLAIN)
    fx.approve_v2()
    a_root = _raw(fx.slug_dir / "active.yaml")["root"]
    _instrument(monkeypatch, obs)
    inst, txn = fx.upgrade(config={}, secret_names_provided=frozenset({"k"}))
    assert inst.state == "active", inst.last_activation_error
    return fx, txn, a_root


def _assert_b_kept(fx, a_root, b_bytes) -> None:
    from personality_binding import InstanceDir

    assert (fx.slug_dir / "active.yaml").read_bytes() == b_bytes
    loaded = InstanceDir(fx.slug_dir).active()
    _, _, checksum = specialist_install.parse_component_root(loaded.root)
    assert checksum == fx.insp2.root_digest
    prior = _raw(fx.slug_dir / "active.prior.yaml")
    assert prior["root"] == a_root
    assert "k" not in (prior.get("config_snapshot") or {})
    assert prior["config_digest"] == SENTINEL
    assert prior["binding"]["effective_config_digest"] == SENTINEL
    assert _files_holding(fx.slug_dir, PLAIN) == []


def test_reclassifying_upgrade_sequence_failure_keeps_activated_tuple(
        tmp_path, monkeypatch) -> None:
    """RC-N3: the reclassifying upgrade is active; the tool layer's
    reload-and-verify step then fails. The installed version it replaced can no
    longer be restored whole, so the new version is kept and the failure
    reported — and a later boot must not undo that either."""
    from test_specialist_recovery_debt import _finish_inline, _inline_tools

    obs = _obs()
    fx, txn, a_root = _activated_reclassifying(tmp_path, monkeypatch, obs)
    b_bytes = (fx.slug_dir / "active.yaml").read_bytes()
    assert obs["begins"] == 1
    assert len(fx.journals()) == 1

    tools = _inline_tools(monkeypatch)

    async def _runtime_ok(*a, **kw):
        return {"ok": True}
    monkeypatch.setattr(tools, "_bundle_reload_and_verify", _runtime_ok)
    env = _finish_inline(tools._bundle_seq_failure(
        txn, {"ok": False, "kind": "bundle_sequence_failed"}, slug="mtg"))

    assert env["ok"] is False
    assert env["kind"] == "bundle_sequence_failed"
    assert obs["rollbacks"] == 0
    assert len(fx.journals()) == 0
    _assert_b_kept(fx, a_root, b_bytes)

    _reconcile(tmp_path, fx)
    assert obs["rollbacks"] == 0
    assert len(fx.journals()) == 0
    _assert_b_kept(fx, a_root, b_bytes)


def test_reclassifying_upgrade_boot_finishes_pending_prior_rotation(
        tmp_path, monkeypatch) -> None:
    """RC-N5b: the new version is written active, the rotation of the old one
    into the prior fails once (it is left in the rollback temporary, as a plain
    byte copy), and the process dies before anything completes the journal.
    Boot must keep the new version and leave no plain copy of the setting."""
    real_replace = os.replace
    faults = []

    def _replace(src, dst, *a, **kw):
        if (not faults and str(src).endswith("active.yaml.rollback-tmp")
                and str(dst).endswith("active.prior.yaml")):
            faults.append((src, dst))
            raise OSError(5, "Input/output error")
        return real_replace(src, dst, *a, **kw)
    monkeypatch.setattr(os, "replace", _replace)
    obs = _obs()
    fx, txn, a_root = _activated_reclassifying(tmp_path, monkeypatch, obs)
    monkeypatch.setattr(os, "replace", real_replace)
    b_bytes = (fx.slug_dir / "active.yaml").read_bytes()

    assert len(faults) == 1
    assert obs["begins"] == 1
    assert len(fx.journals()) == 1

    _reconcile(tmp_path, fx)

    assert obs["rollbacks"] == 0
    assert len(fx.journals()) == 0
    _assert_b_kept(fx, a_root, b_bytes)


# ---------------------------------------------------------------------------
# Regressions (green before the change): what must NOT change
# ---------------------------------------------------------------------------

def test_nonreclassifying_sequence_failure_restores_complete_a(
        tmp_path, monkeypatch) -> None:
    """The ordinary post-commit compensation is untouched: B does not declare
    `k` secret, so the installed version is restored whole, k=v included."""
    from test_specialist_recovery_debt import _finish_inline, _inline_tools

    fx = _UpgradeFixture(tmp_path, monkeypatch, v2_required=("k",))
    fx.approve_v2()
    a_bytes = (fx.slug_dir / "active.yaml").read_bytes()
    obs = _obs()
    _instrument(monkeypatch, obs)
    inst, txn = fx.upgrade(config={})
    assert inst.state == "active"
    tools = _inline_tools(monkeypatch)

    async def _runtime_ok(*a, **kw):
        return {"ok": True}
    monkeypatch.setattr(tools, "_bundle_reload_and_verify", _runtime_ok)
    env = _finish_inline(tools._bundle_seq_failure(
        txn, {"ok": False, "kind": "bundle_sequence_failed"}, slug="mtg"))
    _reconcile(tmp_path, fx)

    assert env["kind"] == "bundle_sequence_failed"
    assert (obs["begins"], obs["rollbacks"]) == (1, 1)
    assert len(fx.journals()) == 0
    assert (fx.slug_dir / "active.yaml").read_bytes() == a_bytes
    assert _copies(fx.slug_dir, "active.yaml") == 1


def test_pending_recommit_unreadable_target_refuses_before_begin(
        tmp_path, monkeypatch) -> None:
    """Row 1 with the component unreadable BEFORE the call: the install loads
    the target component from the store before any journal exists, so it
    refuses there — nothing to carry, nothing to compensate."""
    import specialist_component

    insp, receipt, common, slug_dir = _pending_install(tmp_path, monkeypatch)
    desired_before = (slug_dir / "desired.yaml").read_bytes()
    obs = _obs()
    _instrument(monkeypatch, obs)

    def _eio(*a, **kw):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(specialist_component, "load_specialist_component", _eio)

    with pytest.raises(OSError) as ei:
        specialist_install.commit_specialist_install(
            inspection=insp, receipt=receipt, config={"extra": "e"}, **common)

    assert ei.value.errno == 5
    assert (obs["begins"], obs["rollbacks"]) == (0, 0)
    assert (slug_dir / "desired.yaml").read_bytes() == desired_before


@pytest.mark.parametrize("bundle", [True, False])
def test_uninstall_unclassifiable_store_reaches_core(
        tmp_path, monkeypatch, bundle) -> None:
    """Removal stays unconditional: no door refuses an uninstall, even when the
    component a saved snapshot belongs to cannot be read back."""
    insp, receipt, common, slug_dir = _pending_install(tmp_path, monkeypatch)
    shutil.rmtree(specialist_install.cas_store_dir(
        insp.root_digest, store_root=common["specialists_dir"] / "store"))
    reached = []
    real_core = specialist_install._uninstall_core

    def _core(**kw):
        reached.append(1)
        return real_core(**kw)
    monkeypatch.setattr(specialist_install, "_uninstall_core", _core)

    specialist_install.uninstall_specialist(
        slug="mtg", bundle=bundle, acks=common["acks"],
        specialists_dir=common["specialists_dir"],
        agents_specialists_dir=common["agents_specialists_dir"],
        registry_path=common["registry_path"], ops_dir=common["ops_dir"])

    assert reached == [1]
    assert not (slug_dir / "desired.yaml").exists()
