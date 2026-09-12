"""#929: the refusal this change adds never reaches the transaction compensation.

Attempt 4 of this change was complete, green, and blocked at the ship by a
reviewer who reproduced this: the new `concurrent_mutation` refusal on the
pending-configuration merge read was raised INSIDE `upgrade_specialist`'s journal
window, so it reached `BundleTxn.rollback_disk`, whose capture sanitizer wrote
emptied tuple documents over files the refused call never opened.

That first defect — the sanitizer failing closed because the incoming root was
not yet resolvable — was filed as #966, found live with no fault injection at
all, fixed and published as this change's base. But the coupling survived it. A
diff-review round measured a second route through the same compensation that
#966's fix cannot reach: when the INCOMING component declares a key secret, the
capture union takes that key out of a document belonging to the PREVIOUS root
(deliberately — a journal must never hold a plaintext secret), and the restore
writes the emptied document back. A refused upgrade left `active.yaml` carrying
`config_snapshot {}` and `config_digest pre-guard:removed`, which is exactly what
INV-SPEC-003 says an upgrade failure must not do, with complete carried
declarations and zero classification reads.

So the read was moved instead of the compensation being sharpened again. On the
receipt-bearing bundle arm it now happens BEFORE `specialist_bundle_journal.begin`,
beside the receipt check, the consent gate and the active-tuple read that #966
hoisted for the same reason, and its result is handed down to `_upgrade_core` so
the arm does not read a second time and put the same failure back in the window.

What this file pins, in order:

* a failed pending read refuses with NO journal created and NO compensation run,
  and every byte the refusal names is unchanged;
* the bundle arm performs exactly ONE pending-configuration read, in the wrapper
  and none in the core — the assertion that kills a "hoist but read anyway"
  mutation, which would otherwise leave this file green while restoring the
  defect;
* the legacy no-receipt arm, which has no journal, keeps its own read and its own
  identical refusal;
* and the compensation, when something else inside the window does fail, still
  restores the operator's settings rather than an emptied document — the #966
  carry, with its own mutation control.

Counts, not statuses. What this file does NOT claim: it does not certify the
compensation generally. The install and uninstall transactions carry no
declarations at all, and the incoming-secret union above empties a capture with
the carry complete. All of that is measured and tracked as #975; this change no
longer contributes a route into any of it.
"""
import inspect
from pathlib import Path

import pytest
import yaml

import personality_binding
import specialist_bundle_journal
import specialist_install

from test_specialist_bundle_commit import _UpgradeFixture


def _saved_value_copies(slug_dir: Path, key: str = "k", value: str = "v") -> int:
    """Persisted tuple documents whose snapshot still holds the operator's value.

    Read as raw YAML rather than through the tuple loader, so a typed refusal
    from the loader cannot stand in for a surviving value.
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


def _count_journal_starts(monkeypatch, obs):
    real = specialist_bundle_journal.begin

    def _begin(*a, **kw):
        obs["begins"] += 1
        return real(*a, **kw)
    monkeypatch.setattr(specialist_bundle_journal, "begin", _begin)


def _count_rollbacks(monkeypatch, obs, *, empty_the_carry=False):
    real = specialist_bundle_journal.BundleTxn.rollback_disk

    def _rollback(self):
        obs["rollbacks"] += 1
        obs["carried"] = dict(self.declared_secret_names or {})
        if empty_the_carry:
            object.__setattr__(self, "declared_secret_names", {})
        obs["armed"] = True
        try:
            return real(self)
        finally:
            obs["armed"] = False
    monkeypatch.setattr(
        specialist_bundle_journal.BundleTxn, "rollback_disk", _rollback)


def _watch_pending_reads(monkeypatch, obs):
    """Record which function each `InstanceDir.desired()` call came from.

    By frame, not by count: "one read" is only the property worth pinning if it
    is the WRAPPER's read, and the whole point of the change is that the core
    does not read on this arm.
    """
    real = personality_binding.InstanceDir.desired

    def _desired(self):
        obs["readers"].append(inspect.currentframe().f_back.f_code.co_name)
        if obs.get("raise_in") and obs["readers"][-1] in obs["raise_in"]:
            raise OSError(5, "Input/output error")
        return real(self)
    monkeypatch.setattr(personality_binding.InstanceDir, "desired", _desired)


def _pending_fixture(tmp_path, monkeypatch):
    """An active `mtg` holding `k=v`, plus a pending candidate holding it too."""
    fx = _UpgradeFixture(tmp_path, monkeypatch, v2_required=("k", "extra"))
    fx.approve_v2()
    instance, txn = fx.upgrade(config={"k": "v"})
    specialist_bundle_journal.complete(txn.journal_path)
    assert instance.state == "pending-configuration", instance.last_activation_error
    assert _saved_value_copies(fx.slug_dir) == 2
    assert fx.journals() == set()
    fx.approve_v2()
    return fx


def test_a_failed_pending_read_refuses_before_any_journal_exists(
        tmp_path, monkeypatch) -> None:
    fx = _pending_fixture(tmp_path, monkeypatch)
    slug_dir = fx.slug_dir
    bytes_before = fx.bytes_of("active.yaml", "desired.yaml")
    marker_before = (slug_dir / "pending-receipt.json").read_bytes()
    staged_before = sorted(p.name for p in Path(fx.insp2.staged_dir).rglob("*"))

    obs = {"begins": 0, "rollbacks": 0, "carried": None, "readers": [],
           "raise_in": {"upgrade_specialist"}}
    _watch_pending_reads(monkeypatch, obs)
    _count_journal_starts(monkeypatch, obs)
    _count_rollbacks(monkeypatch, obs)

    with pytest.raises(specialist_install.SpecialistInstallError) as exc:
        fx.upgrade(config={"k": "v", "extra": "e"})

    assert exc.value.kind == "concurrent_mutation"
    assert isinstance(exc.value.__cause__, OSError)
    assert exc.value.__cause__.errno == 5

    # The refusal happened before anything could be recorded or undone.
    assert obs["begins"] == 0
    assert obs["rollbacks"] == 0
    assert fx.journals() == set()

    # ...and every byte the refusal tells the operator to keep is still there.
    assert fx.bytes_of("active.yaml", "desired.yaml") == bytes_before
    assert (slug_dir / "pending-receipt.json").read_bytes() == marker_before
    assert sorted(p.name for p in Path(fx.insp2.staged_dir).rglob("*")) == staged_before
    assert _saved_value_copies(slug_dir) == 2


def test_the_bundle_arm_reads_the_pending_candidate_exactly_once(
        tmp_path, monkeypatch) -> None:
    """The read is hoisted AND handed down — not hoisted and then repeated.

    A "hoist but read again anyway" implementation satisfies the refusal test
    above and still puts a fallible read inside the journal window. This is the
    assertion that separates them, and it is why the reads are recorded by
    calling frame rather than counted.
    """
    fx = _pending_fixture(tmp_path, monkeypatch)
    obs = {"begins": 0, "rollbacks": 0, "carried": None, "readers": [],
           "raise_in": set()}
    _watch_pending_reads(monkeypatch, obs)
    _count_journal_starts(monkeypatch, obs)
    _count_rollbacks(monkeypatch, obs)

    instance, txn = fx.upgrade(config={"k": "v", "extra": "e"})
    specialist_bundle_journal.complete(txn.journal_path)

    assert instance.state == "active", instance.last_activation_error
    assert obs["readers"].count("upgrade_specialist") == 1
    assert obs["readers"].count("_upgrade_core") == 0
    assert obs["begins"] == 1
    assert obs["rollbacks"] == 0
    # The observation was USED, not merely taken: the candidate's saved value
    # survives into the committed generation under the caller's new one.
    snapshot = yaml.safe_load(
        (fx.slug_dir / "active.yaml").read_text(encoding="utf-8"))["config_snapshot"]
    assert snapshot == {"k": "v", "extra": "e"}


def test_the_legacy_no_receipt_arm_keeps_its_own_read_and_refusal(
        tmp_path, monkeypatch) -> None:
    """`_upgrade_core` is still the authority for every caller that has no
    journal — the hoist is the bundle arm's ordering, not its authority."""
    fx = _pending_fixture(tmp_path, monkeypatch)
    obs = {"begins": 0, "rollbacks": 0, "carried": None, "readers": [],
           "raise_in": {"_upgrade_core"}}
    _watch_pending_reads(monkeypatch, obs)
    _count_journal_starts(monkeypatch, obs)
    bytes_before = fx.bytes_of("active.yaml", "desired.yaml")

    with pytest.raises(specialist_install.SpecialistInstallError) as exc:
        specialist_install._upgrade_core(
            slug="mtg", inspection=fx.insp2, config={"k": "v", "extra": "e"},
            secret_names_provided=frozenset(), acks=fx.acks,
            specialists_dir=fx.common["specialists_dir"],
            agents_specialists_dir=fx.common["agents_specialists_dir"],
            receipt=fx.receipt2)

    assert exc.value.kind == "concurrent_mutation"
    assert obs["readers"].count("_upgrade_core") == 1
    assert obs["begins"] == 0
    assert fx.bytes_of("active.yaml", "desired.yaml") == bytes_before


def _fail_inside_the_window(tmp_path, monkeypatch, *, empty_the_carry: bool):
    """A failure AFTER the journal opens, so the compensation genuinely runs.

    The hoisted read can no longer produce one, which is the point of the
    change — so the carry is pinned against a different in-window failure.
    """
    fx = _pending_fixture(tmp_path, monkeypatch)
    obs = {"begins": 0, "rollbacks": 0, "carried": None, "readers": [],
           "raise_in": set(), "armed": False, "store_reads": 0}
    _count_journal_starts(monkeypatch, obs)
    _count_rollbacks(monkeypatch, obs, empty_the_carry=empty_the_carry)

    real_declared = specialist_install._declared_secret_names_for_root

    def _declared(root, *, specialists_dir=None):
        if obs["armed"]:
            obs["store_reads"] += 1
            # The real "I cannot classify these keys" answer is None, not a
            # raise, and None is what made the sanitizer fail closed.
            return None
        return real_declared(root, specialists_dir=specialists_dir)
    monkeypatch.setattr(
        specialist_install, "_declared_secret_names_for_root", _declared)

    def _boom(self, *a, **kw):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(personality_binding.InstanceDir, "stage_desired", _boom)

    with pytest.raises(OSError):
        fx.upgrade(config={"k": "v", "extra": "e"})
    return fx, obs


def test_an_in_window_failure_still_restores_the_operators_settings(
        tmp_path, monkeypatch) -> None:
    """#966's carry, pinned where it still matters: a failure the hoist cannot
    move, with the store unable to classify while the compensation runs."""
    fx, obs = _fail_inside_the_window(
        tmp_path, monkeypatch, empty_the_carry=False)

    assert obs["begins"] == 1
    assert obs["rollbacks"] == 1
    assert obs["store_reads"] == 0          # answered from the carry
    assert len(obs["carried"]) == 2
    assert _saved_value_copies(fx.slug_dir) == 2
    assert fx.journals() == set()


def test_the_carry_is_what_restores_them(tmp_path, monkeypatch) -> None:
    """Mutation control for the test above: empty the transaction's carried
    declarations at the moment the compensation is entered, change nothing
    else, and both documents come back tombstoned."""
    fx, obs = _fail_inside_the_window(
        tmp_path, monkeypatch, empty_the_carry=True)

    assert obs["rollbacks"] == 1
    assert obs["store_reads"] > 0
    assert _saved_value_copies(fx.slug_dir) == 0
    for name in ("active.yaml", "desired.yaml"):
        doc = yaml.safe_load(
            (fx.slug_dir / name).read_text(encoding="utf-8"))
        assert doc["config_snapshot"] == {}
        assert doc["config_digest"] == personality_binding.PRE_GUARD_SENTINEL
