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
