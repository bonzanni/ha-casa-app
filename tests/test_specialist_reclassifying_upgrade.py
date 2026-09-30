"""#975: the reclassifying upgrade's edges — what the red cases in
`test_specialist_capture_admission.py` do not reach.

The upgrade that makes secret a setting the installed version keeps as a plain
value is allowed (the operator's 2026-09-29 ruling). These pin: which lossy
captures the door admits and which it refuses; that the journal names the lossy
files and never holds the value; that a failure inside the library after the
new version is active keeps it, reports it, and that re-running the same
upgrade finishes it; and that finishing the retained prior strips only what its
own component and the incoming one declare secret.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import personality_binding
import plugin_registry
import specialist_bundle_journal
import specialist_install

from test_specialist_bundle_commit import _UpgradeFixture

SENTINEL = personality_binding.PRE_GUARD_SENTINEL


def _tuple_doc(root: str, snapshot: dict) -> str:
    return yaml.safe_dump({"root": root, "config_snapshot": snapshot}, sort_keys=False)


# ---------------------------------------------------------------------------
# the door's second half: which lossy captures may be carried
# ---------------------------------------------------------------------------

A, B, Z = "c/m@1#sha256:aa", "c/m@2#sha256:bb", "c/m@0#sha256:zz"
DECLARED = {A: [], B: ["k"], Z: []}


def _admit(files: dict, *, op="upgrade", target=B, declared=DECLARED):
    return specialist_install._admit_lossy_capture(
        files, op=op, target_root=target, declared=declared, slug="mtg")


def test_the_installed_version_holding_the_value_is_admitted() -> None:
    files = {"active.yaml": _tuple_doc(A, {"k": "v"}),
             "active.prior.yaml": _tuple_doc(Z, {"k": "old"}), "desired.yaml": None}
    assert _admit(files) == ("active.prior.yaml", "active.yaml")


@pytest.mark.parametrize("files, why", [
    ({"active.yaml": _tuple_doc(A, {"j": "1"}),
      "active.prior.yaml": _tuple_doc(Z, {"k": "old"})}, "prior alone"),
    ({"active.yaml": _tuple_doc(A, {"k": "v"}),
      "desired.yaml": _tuple_doc(Z, {"k": "pending"})}, "a pending candidate"),
])
def test_a_loss_outside_the_installed_generation_is_refused(files, why) -> None:
    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        _admit(files)
    assert ei.value.kind == "capture_lossy", why
    assert "uninstall" not in ei.value.detail.lower()


def test_an_install_loss_is_refused() -> None:
    """An install is never the upgrade the ruling allows, whatever it loses."""
    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        _admit({"active.yaml": _tuple_doc(A, {"k": "v"})}, op="install")
    assert ei.value.kind == "capture_lossy"


def test_a_capture_that_loses_nothing_carries_nothing() -> None:
    files = {"active.yaml": _tuple_doc(A, {"j": "1"}), "desired.yaml": None}
    assert _admit(files) == ()
    assert _admit({"active.yaml": _tuple_doc(A, {"k": "v"})}, op="rollback") == ()


# ---------------------------------------------------------------------------
# the journal names the lossy files and never holds the value
# ---------------------------------------------------------------------------

def test_the_journal_records_lossy_names_and_no_value(tmp_path) -> None:
    value = "plain-value-9d2a"
    path = specialist_bundle_journal.begin(
        "upgrade", "mtg", before_entries=[],
        before_tuple_files={"active.yaml": _tuple_doc(A, {"k": value, "j": "1"}),
                            "desired.yaml": None},
        ack_records=[], target_root=B, specialists_dir=tmp_path,
        declared_secret_names=DECLARED, ops_dir=tmp_path / "ops")
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert value not in raw
    assert payload["lossy_tuple_files"] == ["active.yaml"]
    verdict, _slug, _payload = specialist_bundle_journal.classify_journal(path)
    assert verdict == specialist_bundle_journal.JOURNAL_REPLAY


def test_a_journal_naming_a_file_outside_the_tuple_set_is_invalid(tmp_path) -> None:
    path = specialist_bundle_journal.begin(
        "upgrade", "mtg", before_entries=[], before_tuple_files={},
        ack_records=[], target_root=B, specialists_dir=tmp_path,
        declared_secret_names=DECLARED, ops_dir=tmp_path / "ops")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["lossy_tuple_files"] = ["../escape.yaml"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    verdict, _slug, _payload = specialist_bundle_journal.classify_journal(path)
    assert verdict == specialist_bundle_journal.JOURNAL_INVALID


# ---------------------------------------------------------------------------
# finishing the retained prior: per-file, never the union of every carried root
# ---------------------------------------------------------------------------

def test_finish_forward_strips_only_what_the_prior_and_incoming_declare(tmp_path) -> None:
    """An OLDER root C declared `x` secret; the prior belongs to A, which holds
    `x` as a plain value, and B dropped `x`. Stripping against every carried
    root would erase `x=w`; the capture's per-file rule keeps it."""
    slug_dir = tmp_path / "mtg"
    slug_dir.mkdir()
    (slug_dir / "active.prior.yaml").write_text(
        _tuple_doc(A, {"k": "v", "x": "w"}), encoding="utf-8")
    txn = specialist_bundle_journal.BundleTxn(
        journal_path=tmp_path / "j.json", slug="mtg", before_entries=[],
        before_tuple_files={}, ack_records=[], op="upgrade", target_root=B,
        declared_secret_names={A: [], B: ["k"], "c/m@0#sha256:cc": ["x"]},
        lossy_tuple_files=("active.yaml",), specialists_dir=tmp_path)

    txn.finish_forward()

    prior = yaml.safe_load((slug_dir / "active.prior.yaml").read_text(encoding="utf-8"))
    assert prior["config_snapshot"] == {"x": "w"}
    assert prior["config_digest"] == SENTINEL


# ---------------------------------------------------------------------------
# a failure inside the library after activation: kept, reported, retry finishes
# ---------------------------------------------------------------------------

def test_a_failure_after_activation_keeps_the_new_version_and_the_retry_finishes(
        tmp_path, monkeypatch) -> None:
    from personality_binding import InstanceDir

    fx = _UpgradeFixture(tmp_path, monkeypatch, v2_required=(), v2_secret_names=("k",),
                         value="plain-8e4b")
    fx.approve_v2()
    rollbacks = []
    real_rollback = specialist_bundle_journal.BundleTxn.rollback_disk
    monkeypatch.setattr(specialist_bundle_journal.BundleTxn, "rollback_disk",
                        lambda self: (rollbacks.append(1), real_rollback(self))[1])
    real_swap = plugin_registry.apply_owned_swap
    swaps = []

    def _swap_fails_once(**kw):
        swaps.append(1)
        if len(swaps) == 1:
            raise OSError(5, "Input/output error")
        return real_swap(**kw)
    monkeypatch.setattr(plugin_registry, "apply_owned_swap", _swap_fails_once)

    with pytest.raises(specialist_install.SpecialistInstallError) as ei:
        fx.upgrade(config={}, secret_names_provided=frozenset({"k"}))

    assert ei.value.kind == "upgrade_kept_new_version"
    assert "uninstall" not in ei.value.detail.lower()
    assert rollbacks == []
    assert fx.journals() == set()
    kept = InstanceDir(fx.slug_dir).active()
    _, _, checksum = specialist_install.parse_component_root(kept.root)
    assert checksum == fx.insp2.root_digest
    b_bytes = (fx.slug_dir / "active.yaml").read_bytes()
    prior = yaml.safe_load((fx.slug_dir / "active.prior.yaml").read_text(encoding="utf-8"))
    assert "k" not in prior["config_snapshot"] and prior["config_digest"] == SENTINEL

    fx.approve_v2()
    inst, txn = fx.upgrade(config={}, secret_names_provided=frozenset({"k"}))
    assert inst.state == "active"
    assert len(swaps) == 2
    assert (fx.slug_dir / "active.yaml").read_bytes() == b_bytes   # the tuple commit was a no-op
    specialist_bundle_journal.complete(txn.journal_path)
    assert fx.journals() == set()


class _ProcessLost(BaseException):
    """Stands for the process dying: nothing after the raise runs in-process."""


def test_boot_finishes_the_prior_after_a_crash_between_the_swap_and_its_rotation(
        tmp_path, monkeypatch) -> None:
    """The process dies right after `active.yaml` is replaced and before the
    outgoing version — a plain byte copy in the rollback temporary — is
    promoted and stripped, and again while the library tries to finish it.
    Boot must keep the new version, promote and strip the prior, and leave no
    plain copy anywhere."""
    import os

    from personality_binding import InstanceDir

    value = "plain-41f0"
    fx = _UpgradeFixture(tmp_path, monkeypatch, v2_required=(), v2_secret_names=("k",),
                         value=value)
    fx.approve_v2()
    a_root = yaml.safe_load((fx.slug_dir / "active.yaml").read_text(encoding="utf-8"))["root"]
    real_replace = os.replace

    def _die_on_rotation(src, dst, *a, **kw):
        if str(src).endswith("active.yaml.rollback-tmp"):
            raise _ProcessLost()
        return real_replace(src, dst, *a, **kw)
    monkeypatch.setattr(os, "replace", _die_on_rotation)
    with pytest.raises(_ProcessLost):
        fx.upgrade(config={}, secret_names_provided=frozenset({"k"}))
    monkeypatch.setattr(os, "replace", real_replace)

    assert len(fx.journals()) == 1
    assert (fx.slug_dir / "active.yaml.rollback-tmp").is_file()
    b_bytes = (fx.slug_dir / "active.yaml").read_bytes()

    actions = specialist_bundle_journal.reconcile_boot(
        ops_dir=fx.ops_dir, registry_path=fx.common["registry_path"],
        specialists_dir=fx.common["specialists_dir"], acks_path=fx.acks.path,
        receipts_dir=tmp_path / "receipts",
        agents_specialists_dir=fx.common["agents_specialists_dir"])

    assert {"slug": "mtg", "action": "kept_activated"} in actions
    assert fx.journals() == set()
    assert (fx.slug_dir / "active.yaml").read_bytes() == b_bytes
    assert InstanceDir(fx.slug_dir).active().root != a_root
    prior = yaml.safe_load((fx.slug_dir / "active.prior.yaml").read_text(encoding="utf-8"))
    assert prior["root"] == a_root
    assert "k" not in prior["config_snapshot"] and prior["config_digest"] == SENTINEL
    assert not (fx.slug_dir / "active.yaml.rollback-tmp").exists()
    assert [p.name for p in fx.slug_dir.rglob("*")
            if p.is_file() and value.encode() in p.read_bytes()] == []
