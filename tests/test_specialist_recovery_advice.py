"""#929: recovery advice must not destroy the state the refusal preserved.

Both pending-configuration merge reads — the install arm's and the upgrade
arm's — refuse when `InstanceDir.desired()` cannot be read, and that refusal is
correct: the candidate, its saved configuration and the receipt and staging tree
a resume needs are all left byte-for-byte intact, and the very next read can
succeed. What the refusal SAYS is the surface this file pins. `tools.py` relays
`SpecialistInstallError.detail` verbatim into the tool result the configurator
paraphrases to the operator, and `_uninstall_core` deletes the whole instance
directory; so a detail that offers an uninstall as an alternative routes the
operator, after ONE transient read error, into the permanent unjournalled loss of
the only copy of the settings the refusal exists to protect.

The decisive assertion is literal equality over the WHOLE emitted detail, not a
containment check: a containment check for banned words admits "or reset
everything", and the reviewer measured that difference (10/10 wording mutations
rejected by the literal, 1 admitted by containment). The expected text is a
literal HERE and is never imported from the module under test — importing it
would make the assertion a tautology.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from test_personality_admin_handlers import _install, restore_installed_index  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[1]

_SAVED = "operator-only-value"

# The full detail both arms must emit, with the injected exception's own
# rendering interpolated. Deterministic because the injected error is fixed.
_EXPECTED_DETAIL = (
    "'mtg': the pending candidate's configuration could not be read "
    "([Errno 5] Input/output error); refusing to restage over it — "
    "preserve the pending candidate, its saved configuration, and the "
    "receipt and staging tree needed to resume; resolve the read error "
    "and retry"
)

# Secondary, for a legible failure message only. The literal above is the
# control; these say WHY when it breaks.
_DESTRUCTIVE = ("uninstall", "reinstall", "afresh", "delete", "remove",
                "wipe", "start over", "fresh install", "reset")


def _saved_value_copies(root: Path) -> int:
    """How many persisted files hold the operator's supplied value."""
    n = 0
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        try:
            if _SAVED.encode() in p.read_bytes():
                n += 1
        except OSError:
            continue
    return n


def _recovery_journals(ops_dir: Path) -> int:
    """Journals left behind for a later replay to recover from."""
    if not ops_dir.is_dir():
        return 0
    return sum(1 for p in ops_dir.rglob("*") if p.is_file())


@pytest.mark.parametrize("path", ["install", "upgrade"])
def test_pending_read_failure_advice_preserves_resume_state(
        tmp_path, monkeypatch, restore_installed_index, path) -> None:  # noqa: F811
    import personality_binding
    import specialist_install

    if path == "install":
        ctx = _install(tmp_path, monkeypatch, home=tmp_path / "a", slug="mtg",
                       required_config=("saved", "region"),
                       config={"saved": _SAVED})
        assert ctx.state == "pending-configuration"
        resume = lambda **kw: specialist_install.commit_specialist_install(**kw)  # noqa: E731
        active_before = None
    else:
        first = _install(tmp_path, monkeypatch, home=tmp_path / "a", slug="mtg",
                         required_config=(), config={})
        assert first.state == "active"
        ctx = _install(tmp_path, monkeypatch, home=tmp_path / "b", slug="mtg",
                       version="0.2.0", required_config=("saved", "region"),
                       config={"saved": _SAVED})
        assert ctx.state == "pending-configuration"
        resume = lambda **kw: specialist_install.upgrade_specialist(slug="mtg", **kw)  # noqa: E731
        active_before = (ctx.specialists_dir / "mtg" / "active.yaml").read_bytes()

    slug_dir = ctx.specialists_dir / "mtg"
    desired = slug_dir / "desired.yaml"
    ops_dir = ctx.kw["ops_dir"]

    # The state the refusal exists to protect, counted rather than asserted by
    # name: exactly one persisted copy of the value, and nothing to recover it
    # from if that copy goes.
    copies_before = _saved_value_copies(slug_dir)
    assert copies_before == 1
    journals_before = _recovery_journals(ops_dir)
    assert journals_before == 0
    before = desired.read_bytes()
    marker_before = (slug_dir / "pending-receipt.json").read_bytes()
    receipt_before = sorted(p.name for p in ctx.receipts_dir.rglob("*") if p.is_file())
    staged_before = sorted(p.name for p in Path(ctx.inspection.staged_dir).rglob("*"))
    assert staged_before

    # Exactly one read failure, on the FIRST read only: the shape a "the later
    # guard will catch it" argument cannot cover, because the guard reads again.
    real = personality_binding.InstanceDir.desired
    failures: list[bool] = []

    def _one_bad_read(self):
        if not failures:
            failures.append(True)
            raise OSError(5, "Input/output error")
        return real(self)

    monkeypatch.setattr(personality_binding.InstanceDir, "desired", _one_bad_read)
    kw = dict(ctx.kw)
    kw["config"] = {"region": "EU"}
    with pytest.raises(specialist_install.SpecialistInstallError) as exc:
        resume(**kw)

    assert len(failures) == 1
    assert exc.value.kind == "concurrent_mutation"
    assert isinstance(exc.value.__cause__, OSError)
    assert exc.value.__cause__.errno == 5

    # THE control: the whole operator-facing sentence, not a keyword scan.
    assert exc.value.detail == _EXPECTED_DETAIL
    lowered = exc.value.detail.lower()
    assert [w for w in _DESTRUCTIVE if w in lowered] == []

    # Nothing the advice tells the operator to keep was touched.
    assert desired.read_bytes() == before
    assert (slug_dir / "pending-receipt.json").read_bytes() == marker_before
    assert sorted(p.name for p in ctx.receipts_dir.rglob("*") if p.is_file()) == receipt_before
    assert sorted(p.name for p in Path(ctx.inspection.staged_dir).rglob("*")) == staged_before
    assert _saved_value_copies(slug_dir) == copies_before      # 1 -> 1
    assert _recovery_journals(ops_dir) == journals_before      # 0 -> 0
    if active_before is not None:
        assert (slug_dir / "active.yaml").read_bytes() == active_before

    # And the advice is true: with the fault cleared the same call succeeds and
    # the earlier value survives beside the new one. No re-inspect is needed.
    monkeypatch.setattr(personality_binding.InstanceDir, "desired", real)
    instance, txn = resume(**kw)
    import specialist_bundle_journal
    specialist_bundle_journal.complete(txn.journal_path)
    assert instance.state == "active"
    snapshot = dict(instance.active.config_snapshot)
    assert snapshot["saved"] == _SAVED
    assert snapshot["region"] == "EU"


def _flat(p: Path) -> str:
    return " ".join(p.read_text(encoding="utf-8").split())


# The one normative sentence, carried by the published corpus and by the
# shipped runtime doctrine the configurator reads before it selects a recipe.
_RULE = (
    "Recovery advice must preserve retained operator state and the resources "
    "needed to resume using it. Do not recommend an action that would discard, "
    "overwrite or make that state unrecoverable unless a usable recovery copy "
    "has been verified to survive the action. Failure to read or validate state "
    "is not evidence that it is expendable."
)


def test_pending_read_recovery_rule_is_shipped() -> None:
    """A rule nothing checks is the control class that failed here.

    Counts, not presence: equality between two independently read documents is
    satisfied when BOTH lose the rule, and a duplicated block is a second
    formulation waiting to drift. Exactly one occurrence in each.
    """
    import tools as tools_mod

    corpus = REPO_ROOT / "docs" / "doctrine" / "operating-casa.md"
    runtime = (Path(tools_mod.__file__).parent / "defaults/agents/executors"
               / "configurator/doctrine/safety.md")
    flat_rule = " ".join(_RULE.split())

    assert (_flat(corpus).count(flat_rule), _flat(runtime).count(flat_rule)) == (1, 1)

    # The runtime counterpart is only reachable because the prompt routes the
    # configurator into safety doctrine before it picks a recipe.
    prompt = _flat(Path(tools_mod.__file__).parent / "defaults/agents/executors"
                   / "configurator/prompt.md")
    assert "doctrine/safety.md" in prompt
