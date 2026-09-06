"""#838 — the recovery-debt scanner itself, and the two surfaces around it.

The behavioural pins live in `tests/test_specialist_recovery_debt.py` (the
frozen red case, judged through real writers). This file pins the scanner's own
contract — its rows, that it delegates the classification rather than copying
it, that it writes nothing — plus the structural directory agreement and the
`compensation_failed` envelope's wording.
"""
from __future__ import annotations

import errno
import json as _json
import os
from pathlib import Path

import pytest

import specialist_bundle_journal as journal
import specialist_install


_OPID = "0" * 32


def _valid(slug: str) -> bytes:
    return _json.dumps({
        "schema_version": journal.SCHEMA_VERSION, "op": "install", "slug": slug,
        "state": "in-progress",
        "before": {"registry_entries": [], "tuple_files": {}, "ack_records": []},
        "receipt_digest": "", "consent_identity": "", "target_root": "",
        "steps_done": [],
    }, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _ops(tmp_path: Path) -> Path:
    d = tmp_path / "ops"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------

def test_a_replayable_journal_is_debt_scoped_to_its_filename_slug(tmp_path: Path) -> None:
    ops = _ops(tmp_path)
    (ops / f"mtg.{_OPID}.json").write_bytes(_valid("mtg"))
    assert journal.recovery_debt(ops_dir=ops) == [
        {"slug": "mtg", "journal": f"mtg.{_OPID}.json",
         "verdict": journal.JOURNAL_REPLAY}]


def test_an_unparseable_name_is_debt_with_no_slug(tmp_path: Path) -> None:
    """Boot's answer to one is `quarantine_all` — every owned entry there is —
    so the row carries no slug and concerns every slug."""
    ops = _ops(tmp_path)
    (ops / "not-a-journal.json").write_bytes(_valid("mtg"))
    assert journal.recovery_debt(ops_dir=ops) == [
        {"slug": None, "journal": "not-a-journal.json",
         "verdict": journal.JOURNAL_UNPARSEABLE}]


def test_a_write_temporary_is_not_debt(tmp_path: Path) -> None:
    """Boot's pre-scan sweep DELETES it, so it is neither replayed nor
    quarantined. Both the sweep and this scan read `JOURNAL_TMP_RE`."""
    ops = _ops(tmp_path)
    (ops / f"mtg.{_OPID}.json.tmp-{'a' * 32}").write_bytes(_valid("mtg"))
    assert journal.recovery_debt(ops_dir=ops) == []


def test_complete_and_quarantined_files_are_not_debt(tmp_path: Path) -> None:
    ops = _ops(tmp_path)
    payload = _json.loads(_valid("mtg"))
    payload["state"] = "complete"
    (ops / f"mtg.{_OPID}.json").write_bytes(
        _json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    (ops / f"mtg.{'1' * 32}.json.quarantined").write_bytes(_valid("mtg"))
    assert journal.recovery_debt(ops_dir=ops) == []


def test_rows_are_ordered_by_entry_name(tmp_path: Path) -> None:
    ops = _ops(tmp_path)
    (ops / f"zulu.{_OPID}.json").write_bytes(_valid("zulu"))
    (ops / f"alfa.{_OPID}.json").write_bytes(b"{ not json")
    assert [r["slug"] for r in journal.recovery_debt(ops_dir=ops)] == ["alfa", "zulu"]


# ---------------------------------------------------------------------------
# absence vs "cannot tell"
# ---------------------------------------------------------------------------

def test_an_absent_ops_directory_is_not_debt(tmp_path: Path) -> None:
    assert journal.recovery_debt(ops_dir=tmp_path / "nowhere") == []


def test_an_unlistable_ops_directory_is_debt(tmp_path: Path, monkeypatch) -> None:
    """The one row the frozen red case cannot reach behaviourally: an ops
    directory that cannot be enumerated. Boot's whole reconcile raises into its
    caller's belt and resolves NOTHING, so unknown must not read as empty."""
    ops = _ops(tmp_path)
    real = Path.iterdir

    def _iterdir(self):
        if Path(self) == ops:
            raise OSError(errno.EACCES, "injected listing failure")
        return real(self)

    monkeypatch.setattr(Path, "iterdir", _iterdir)
    assert journal.recovery_debt(ops_dir=ops) == [
        {"slug": None, "journal": None, "verdict": journal.JOURNAL_UNREADABLE}]


def test_an_ops_path_that_is_a_regular_file_is_debt(tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    ops.write_text("not a directory", encoding="utf-8")
    assert journal.recovery_debt(ops_dir=ops) == [
        {"slug": None, "journal": None, "verdict": journal.JOURNAL_UNREADABLE}]


def test_an_entry_that_cannot_be_stat_ed_is_debt_under_its_filename_slug(
    tmp_path: Path,
) -> None:
    """A symlink loop: `os.stat` raises ELOOP. Not absence — the entry may be a
    perfectly readable journal at boot."""
    ops = _ops(tmp_path)
    a, b = ops / f"mtg.{_OPID}.json", ops / f"mtg.{'1' * 32}.json"
    a.symlink_to(b)
    b.symlink_to(a)
    rows = journal.recovery_debt(ops_dir=ops)
    assert [(r["slug"], r["verdict"]) for r in rows] == [
        ("mtg", journal.JOURNAL_UNREADABLE)] * 2


def test_a_vanished_entry_is_not_debt(tmp_path: Path) -> None:
    ops = _ops(tmp_path)
    (ops / f"mtg.{_OPID}.json").symlink_to(ops / "gone.json")   # broken symlink
    assert journal.recovery_debt(ops_dir=ops) == []


def test_entries_that_are_not_regular_files_are_not_debt(tmp_path: Path) -> None:
    """Boot's own scan does `if not path.is_file(): continue`. Reporting them
    would be a refusal no restart could ever clear."""
    ops = _ops(tmp_path)
    (ops / f"mtg.{_OPID}.json").mkdir()
    os.mkfifo(ops / f"mtg.{'1' * 32}.json")
    assert journal.recovery_debt(ops_dir=ops) == []


# ---------------------------------------------------------------------------
# one authority, and no side effects
# ---------------------------------------------------------------------------

def test_the_scan_follows_classify_journal_rather_than_the_bytes(
    tmp_path: Path, monkeypatch,
) -> None:
    """#543's rule, mutation-checked: a copied classifier would read the bytes
    (a valid replayable journal) and disagree with the authority."""
    ops = _ops(tmp_path)
    (ops / f"mtg.{_OPID}.json").write_bytes(_valid("mtg"))
    monkeypatch.setattr(journal, "classify_journal",
                        lambda path: (journal.JOURNAL_COMPLETE, "mtg", None))
    assert journal.recovery_debt(ops_dir=ops) == []


def test_the_scan_writes_deletes_and_completes_nothing(tmp_path: Path) -> None:
    ops = _ops(tmp_path)
    names = {
        f"mtg.{_OPID}.json": _valid("mtg"),
        f"fin.{'1' * 32}.json": b"{ not json",
        "not-a-journal.json": b"whatever",
        f"mtg.{'2' * 32}.json.tmp-{'a' * 32}": _valid("mtg"),
        f"mtg.{'3' * 32}.json.quarantined": _valid("mtg"),
    }
    for name, data in names.items():
        (ops / name).write_bytes(data)
    before = {p.name: p.read_bytes() for p in sorted(ops.iterdir())}

    journal.recovery_debt(ops_dir=ops)
    journal.recovery_debt(ops_dir=ops)

    assert {p.name: p.read_bytes() for p in sorted(ops.iterdir())} == before


# ---------------------------------------------------------------------------
# the fence's contract
# ---------------------------------------------------------------------------

def test_the_fence_returns_the_directory_it_inspected(tmp_path: Path) -> None:
    ops = _ops(tmp_path)
    assert specialist_install.require_no_recovery_debt("mtg", ops_dir=ops) == ops


def test_the_fence_defaults_to_the_directory_begin_writes_into(monkeypatch, tmp_path: Path) -> None:
    """`ops_dir=None` must resolve to the module default `begin` itself
    defaults to — read at call time, never captured at import."""
    ops = _ops(tmp_path)
    monkeypatch.setattr(journal, "OPS_DIR", ops)
    assert specialist_install.require_no_recovery_debt("mtg") == ops


def test_the_fence_names_the_journal_and_what_clears_it(tmp_path: Path) -> None:
    ops = _ops(tmp_path)
    name = f"mtg.{_OPID}.json"
    (ops / name).write_bytes(_valid("mtg"))
    with pytest.raises(specialist_install.SpecialistInstallError) as exc:
        specialist_install.require_no_recovery_debt("mtg", ops_dir=ops)
    assert exc.value.kind == "recovery_pending"
    detail = exc.value.detail
    assert name in detail and "mtg" in detail and "restart" in detail.lower()


def test_an_unattributable_journal_refuses_every_slug(tmp_path: Path) -> None:
    ops = _ops(tmp_path)
    (ops / "not-a-journal.json").write_bytes(b"whatever")
    for slug in ("mtg", "finance"):
        with pytest.raises(specialist_install.SpecialistInstallError) as exc:
            specialist_install.require_no_recovery_debt(slug, ops_dir=ops)
        assert exc.value.kind == "recovery_pending"


def test_another_slugs_debt_does_not_refuse_this_one(tmp_path: Path) -> None:
    ops = _ops(tmp_path)
    (ops / f"finance.{_OPID}.json").write_bytes(_valid("finance"))
    assert specialist_install.require_no_recovery_debt("mtg", ops_dir=ops) == ops


# ---------------------------------------------------------------------------
# the envelope that CREATES the debt
# ---------------------------------------------------------------------------

def test_failed_compensation_states_what_now_holds(tmp_path: Path, monkeypatch) -> None:
    """`_bundle_seq_failure`'s `compensation_failed` arm was the only one of its
    three with no `outcome` sentence: the caller told least at the one moment it
    can still act. Its measured `plugin_data` disclosure (#676) is untouched."""
    import plugin_registry
    from test_specialist_bundle_commit import _prep
    from test_specialist_recovery_debt import _finish_inline, _inline_tools

    plugin_registry.reload_snapshot(registry_path=tmp_path / "snap-registry.json",
                                    store_root=tmp_path / "snap-store")
    tools = _inline_tools(monkeypatch)
    ctx = _prep(tmp_path, monkeypatch)
    _inst, txn = specialist_install.commit_specialist_install(**ctx.kw)

    (tmp_path / "registry.json").write_text("{ not a registry", encoding="utf-8")
    env = _finish_inline(tools._bundle_seq_failure(
        txn, {"ok": False, "kind": "bundle_sequence_failed"}, slug="mtg"))

    assert env["compensation_failed"] is True
    assert "rolled_back" not in env and "runtime_compensation_incomplete" not in env
    assert "restart" in env["outcome"].lower()
    assert "refused" in env["outcome"].lower()
