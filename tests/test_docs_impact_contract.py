"""INV-DOC-008 — a Docs-impact waiver is checked against the diff it ships with.

`tests/test_docs_impact_ack.sh` is the behavioural harness and covers the full
grammar; it runs from `.github/workflows/docs.yml`. These pins exist because a
declared invariant needs a *collectable* binding, and because the predicates
below are the ones #685 was filed about — each is asserted through its own named
counter, so removing one guard changes one count rather than merely flipping an
exit status.

The decision is lifted from the shipping script, never re-implemented: a copy
would drift, and the copy that drifted would be the one nobody was watching.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "docs_impact.sh"
ANCHOR = ': > "$tmp/acked.txt"'

D1 = "architecture/telegram.md"
D2 = "architecture/turn-loop.md"


def _block() -> str:
    src = SCRIPT.read_text()
    assert ANCHOR in src, (
        "scripts/docs_impact.sh was restructured: the waiver block's start "
        "marker is gone. Update this pin rather than deleting it."
    )
    return src[src.index(ANCHOR):].rstrip()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(("git", *args), cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    # Deliberately not address-shaped: the repo's pre-commit hook refuses
    # anything matching an email pattern, and git does not validate the field.
    _git(work, "config", "user.email", "harness")
    _git(work, "config", "user.name", "harness")
    _git(work, "commit", "-q", "--allow-empty", "-m", "base")
    _git(work, "checkout", "-q", "-B", "pr")
    return work


def _commit(repo: Path, message: str) -> None:
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)


def _decide(repo: Path, impacted: str, touched: str = "", deleted: str = "",
            ack: str | None = None) -> tuple[int, str]:
    """Run the shipping decision over a synthetic commit graph."""
    script = "\n".join([
        "set -euo pipefail",
        'tmp="$(mktemp -d)"',
        'err() { echo "docs-impact: $*" >&2; }',
        'impacted="$1" touched="$2" deleted="$3"',
        'base=main',
        'ack_commit="${4:-$(git rev-parse HEAD)}"',
        _block(),
    ])
    proc = subprocess.run(("bash", "-c", script, "decide", impacted, touched,
                           deleted, ack or ""), cwd=repo, capture_output=True,
                          text=True)
    return proc.returncode, proc.stdout + proc.stderr


def test_inv_doc_008_a_waiver_for_an_updated_document_is_refused(repo: Path) -> None:
    """The #683 shape: a document is both waived and updated by one change."""
    _commit(repo, f"change\n\nDocs-impact: {D1} — prose reviewed, but document is updated")
    rc, out = _decide(repo, impacted=D1, touched=D1)
    assert rc == 1, out
    assert "contradictory=1" in out, out


def test_inv_doc_008_a_waiver_for_an_unimpacted_document_is_refused(repo: Path) -> None:
    _commit(repo, f"change\n\nDocs-impact: {D2} — a document this change does not impact")
    rc, out = _decide(repo, impacted=D1)
    assert rc == 1, out
    assert "irrelevant=1" in out, out


def test_inv_doc_008_only_the_tip_commit_may_carry_a_waiver(repo: Path) -> None:
    """The #694 shape. The intermediate line is MALFORMED on purpose: it must be
    refused for sitting in the wrong commit, not reported as a grammar error —
    the grammar error is what the push arm reported, after publication."""
    _commit(repo, "an earlier commit\n\nDocs-impact: none (tests only)")
    _commit(repo, f"the real change\n\nDocs-impact: {D1} — a good waiver on the tip")
    rc, out = _decide(repo, impacted=D1)
    assert rc == 1, out
    assert "non_tip=1" in out, out


def test_inv_doc_008_a_document_waived_twice_is_refused(repo: Path) -> None:
    _commit(repo, f"change\n\nDocs-impact: {D1} — reason one"
                  f"\nDocs-impact: {D1} — reason two, contradicting the first")
    rc, out = _decide(repo, impacted=D1)
    assert rc == 1, out
    assert "duplicate=1" in out, out


def test_inv_doc_008_the_reserved_none_token_must_be_true(repo: Path) -> None:
    """`none` BESIDE a real waiver is the case that makes this guard
    load-bearing: every impacted document is waived, so `missing` is zero and
    nothing else in the contract refuses the contradiction."""
    _commit(repo, f"change\n\nDocs-impact: none — nothing needs a waiver"
                  f"\nDocs-impact: {D1} — except this one, apparently")
    rc, out = _decide(repo, impacted=D1)
    assert rc == 1, out
    assert "missing=0" in out, out
    assert "These do:" in out, out

    # A FRESH branch off main: the commit above carries a waiver line, and
    # stacking on it would make this a non-tip refusal instead.
    _git(repo, "checkout", "-q", "-B", "pr", "main")
    _commit(repo, "tests only\n\nDocs-impact: none — adds a test, no claimed surface moves")
    rc, out = _decide(repo, impacted="")
    assert rc == 0, out
    assert "ack_lines=1" in out, out


def test_inv_doc_008_a_subject_line_is_never_a_waiver(repo: Path) -> None:
    """`%B` exposes the subject at column zero, so it parses on the pull-request
    arm; the squash formatter prefixes every constituent subject with "* ", so
    the push arm sees nothing. Skipping line one is what closes that."""
    _commit(repo, f"Docs-impact: {D1} — written as the subject line")
    rc, out = _decide(repo, impacted=D1)
    assert rc == 1, out
    assert "ack_lines=0" in out and "missing=1" in out, out


def test_inv_doc_008_the_waiver_parse_runs_when_nothing_is_impacted(repo: Path) -> None:
    """An early return used to sit above this block, so on a diff impacting
    nothing not one line was read: a malformed line rode to `main` and only the
    push arm's larger cumulative diff ever parsed it."""
    _commit(repo, "tests only\n\nDocs-impact: none (tests only)")
    rc, out = _decide(repo, impacted="")
    assert rc == 1, out
    assert "needs" in out, out


def test_inv_doc_008_every_tip_carries_a_line(repo: Path) -> None:
    _commit(repo, "tests only, and silent about it")
    rc, out = _decide(repo, impacted="")
    assert rc == 1, out
    assert "carries no Docs-impact line" in out, out


def test_a_root_ack_commit_has_no_provenance_question(repo: Path) -> None:
    """A dry run builds the prospective squash message as a PARENTLESS object and
    asks whether it would pass — the only way to see the post-merge text before
    the merge. A root commit summarises nothing, so there is no earlier message
    that could be concatenated with it and nothing to refuse."""
    _commit(repo, f"change\n\nDocs-impact: {D1} — a good waiver")
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    probe = _git(repo, "commit-tree", tree, "-m",
                 f"squashed\n\nDocs-impact: {D1} — a good waiver").strip()
    rc, out = _decide(repo, impacted=D1, ack=probe)
    assert rc == 0, out
    assert "non_tip=0" in out and "is a root commit" in out, out


def test_a_detached_non_root_ack_is_refused(repo: Path) -> None:
    """Not a root and no shared history with the base: intermediate commits do
    exist and cannot be enumerated. Reporting non_tip=0 there would be a count
    that was never measured, so it fails closed instead."""
    _commit(repo, f"change\n\nDocs-impact: {D1} — a good waiver")
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    root = _git(repo, "commit-tree", tree, "-m", "unrelated root").strip()
    probe = _git(repo, "commit-tree", tree, "-p", root, "-m",
                 f"detached\n\nDocs-impact: {D1} — a good waiver").strip()
    rc, out = _decide(repo, impacted=D1, ack=probe)
    assert rc == 1, out
    assert "have no common ancestor" in out, out


def test_the_shipping_script_has_no_early_return_above_the_waiver_block() -> None:
    """A source-order pin. The harness and the pins above inject `impacted`
    directly, so an implementation that restored the early return at the old
    site would keep every one of them green while reopening the hole."""
    head = SCRIPT.read_text().split(ANCHOR)[0]
    assert '[ -n "$impacted" ] || exit 0' not in head, (
        "the empty-impact early return is back above the waiver block: waiver "
        "lines would again go unparsed on a diff that impacts no document"
    )


def test_decide_removes_its_shell_temporary_directory(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#795. `_decide` copies `tmp="$(mktemp -d)"` from the shipping script; the
    script pairs it with `trap 'rm -rf "$tmp"' EXIT` and the copy must too. The
    assertion is a COUNT of entries in an isolated `TMPDIR` across one call, so
    it measures the leak itself rather than the presence of a line."""
    _commit(repo, "tests only\n\nDocs-impact: none — no documented surface changes")

    isolated_tmpdir = tmp_path / "system-tmp"
    isolated_tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(isolated_tmpdir))

    before = {entry.name for entry in isolated_tmpdir.iterdir()}
    rc, out = _decide(repo, impacted="")
    after = {entry.name for entry in isolated_tmpdir.iterdir()}
    added = after - before

    assert (len(before), len(after), len(added)) == (0, 0, 0), (
        f"_decide leaked TMPDIR entries: before={len(before)}, "
        f"after={len(after)}, added={len(added)}; added_names={sorted(added)}"
    )
    assert rc == 0, out
    assert "ack_lines=1" in out, out
