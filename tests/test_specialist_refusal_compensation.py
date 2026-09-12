"""#929: the in-window refusal this change adds must not cost the operator the
settings it declined to replace.

Attempt 4 of this change was blocked at the ship by a reviewer who reproduced
exactly that: the new `concurrent_mutation` refusal on the pending-configuration
merge read is raised INSIDE `upgrade_specialist`'s journal window, so it reaches
`BundleTxn.rollback_disk`, whose capture sanitizer wrote emptied tuple documents
over files the refused call never opened. Saved-setting copies went 1 -> 2 -> 0
with no journal left to recover from. That defect was filed as #966, found to be
live with no fault injection at all, and fixed and published before this attempt.

The fix resolves the declared secret names for every root a capture needs BEFORE
the journal opens and CARRIES them on the journal payload and on both
transactions, so `_names_for_root` answers from the carry instead of reading the
content-addressed store at restore time. This file is the pin for that carry as
THIS change's refusal exercises it, which is the only thing #966's own tests do
not cover: they assert that the four PREFLIGHT refusals open no journal at all,
and a refusal raised inside the window is the remaining class.

Counts, not statuses. `test_the_carry_is_what_preserves` is the mutation control
in the same file — with the carried declarations emptied at the moment the
compensation is entered and nothing else changed, the same sequence must reach
0, or the assertion above it is not reaching its target.

What this file does NOT claim, stated so nobody reads it as wider than it is:
it certifies the classification-UNAVAILABLE case on the upgrade path only. A
capture whose incoming root DECLARES a captured key secret is emptied by the
same compensation with the carry complete and no store read at all, and the
install and uninstall transactions pass no carry — all measured, all inherited,
and all tracked as #975.

It also does not cover the journal PAYLOAD's copy of the declarations. Mutation-checked:
removing `declared_secret_names=` from `specialist_bundle_journal.begin` leaves this file
green, because `rollback_disk` answers from the transaction's own field and the payload's
copy exists for boot replay, which this file does not exercise. That arm is #966's and is
covered by its own tests. Removing the same argument from the `rollback_txn` construction,
computing the declarations after the journal opens, or making `_names_for_root` ignore a
carried entry each turn this file red.
"""
from pathlib import Path

import pytest
import yaml

import personality_binding
import specialist_bundle_journal
import specialist_install

from test_specialist_bundle_commit import _UpgradeFixture


def _saved_value_copies(slug_dir: Path, key: str = "k", value: str = "v") -> int:
    """Persisted tuple documents whose snapshot still holds the operator's value.

    Read as raw YAML rather than through the tuple loader: a typed refusal from
    the loader must not be able to stand in for a surviving value.
    """
    n = 0
    for name in ("active.yaml", "desired.yaml"):
        path = slug_dir / name
        if not path.is_file():
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — an unreadable file holds no copy
            continue
        if (doc.get("config_snapshot") or {}).get(key) == value:
            n += 1
    return n


def _run_the_sequence(tmp_path, monkeypatch, *, empty_the_carry: bool):
    """A pending upgrade holding the operator's value, then a second upgrade
    whose pending-configuration read fails while the store cannot classify.

    Returns (copies_before, copies_after, observations).
    """
    fx = _UpgradeFixture(tmp_path, monkeypatch, v2_required=("k", "extra"))
    slug_dir = fx.slug_dir

    fx.approve_v2()
    instance, txn = fx.upgrade(config={"k": "v"})
    specialist_bundle_journal.complete(txn.journal_path)
    assert instance.state == "pending-configuration", instance.last_activation_error
    copies_before = _saved_value_copies(slug_dir)
    bytes_before = fx.bytes_of("active.yaml", "desired.yaml")
    assert copies_before == 2                 # active.yaml AND the candidate
    assert len(bytes_before) == 2
    assert fx.journals() == set()

    fx.approve_v2()

    # (a) the pending-configuration merge read fails — the read this change
    #     turns into a fail-closed refusal.
    def _eio(self):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(personality_binding.InstanceDir, "desired", _eio)

    obs = {"armed": False, "store_reads": 0, "rollbacks": 0, "carried": None}

    # (b) for the WHOLE of the compensation, the content-addressed store cannot
    #     say which keys are secret. `_declared_secret_names_for_root` reports
    #     that by returning None rather than raising, and None is what made
    #     `_captured_secret_union` fall closed and strip every key.
    real_declared = specialist_install._declared_secret_names_for_root

    def _declared(root, *, specialists_dir=None):
        if obs["armed"]:
            obs["store_reads"] += 1
            return None
        return real_declared(root, specialists_dir=specialists_dir)
    monkeypatch.setattr(
        specialist_install, "_declared_secret_names_for_root", _declared)

    real_rollback = specialist_bundle_journal.BundleTxn.rollback_disk

    def _rollback(self):
        obs["rollbacks"] += 1
        obs["carried"] = dict(self.declared_secret_names or {})
        if empty_the_carry:
            object.__setattr__(self, "declared_secret_names", {})
        obs["armed"] = True
        try:
            return real_rollback(self)
        finally:
            obs["armed"] = False
    monkeypatch.setattr(
        specialist_bundle_journal.BundleTxn, "rollback_disk", _rollback)

    with pytest.raises(specialist_install.SpecialistInstallError) as exc:
        fx.upgrade(config={"k": "v", "extra": "e"})

    obs["kind"] = exc.value.kind
    obs["bytes_before"] = bytes_before
    obs["bytes_after"] = fx.bytes_of("active.yaml", "desired.yaml")
    obs["journals_after"] = fx.journals()
    return copies_before, _saved_value_copies(slug_dir), obs


def test_an_in_window_refusal_leaves_the_saved_settings_alone(
        tmp_path, monkeypatch) -> None:
    """The refusal reaches the compensation, and the compensation answers from
    the declarations the transaction carries rather than from the store."""
    before, after, obs = _run_the_sequence(
        tmp_path, monkeypatch, empty_the_carry=False)

    assert obs["kind"] == "concurrent_mutation"
    # The compensation DID run — this is not a test that passes by the refusal
    # happening somewhere harmless.
    assert obs["rollbacks"] == 1
    # ...and it never needed the store, because the transaction carried the
    # answer for both roots.
    assert obs["store_reads"] == 0
    assert len(obs["carried"]) == 2

    assert (before, after) == (2, 2)
    assert obs["bytes_after"] == obs["bytes_before"]
    assert obs["journals_after"] == set()


def test_the_carry_is_what_preserves(tmp_path, monkeypatch) -> None:
    """Mutation control for the assertion above.

    Identical sequence with one difference: the transaction's carried
    declarations are emptied at the moment the compensation is entered. The
    store read is then reached, reports that it cannot classify, and the
    sanitizer's fail-closed branch empties both documents. If this does not
    happen, the test above is not observing the carry.
    """
    before, after, obs = _run_the_sequence(
        tmp_path, monkeypatch, empty_the_carry=True)

    assert obs["kind"] == "concurrent_mutation"
    assert obs["rollbacks"] == 1
    assert obs["store_reads"] > 0
    assert (before, after) == (2, 0)
    assert obs["bytes_after"] != obs["bytes_before"]

    for name in ("active.yaml", "desired.yaml"):
        doc = yaml.safe_load(
            (tmp_path / "specialists" / "mtg" / name).read_text(encoding="utf-8"))
        assert doc["config_snapshot"] == {}
        assert doc["config_digest"] == personality_binding.PRE_GUARD_SENTINEL
