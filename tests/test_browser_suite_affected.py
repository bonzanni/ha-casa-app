"""scripts/browser-suite-affected.sh: which changes the gate may skip the browser suite for.

The gate approves a commit, so a path that reaches the browser suite but is classified as
Casa-only is a silent failure: the gate says PASS with the suite never run. The script is
fail-closed — only paths under its Casa-only roots skip — and these tests pin that over a
real git repository: every shared surface runs, a move reports both its ends, and a
failed diff is an error rather than an empty change list.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "browser-suite-affected.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    for rel in ("casa/config.yaml", "casa_browser/config.yaml", "casa_browser/tests/test_x.py",
                "tests/test_y.py", "docs/a.md", "test-local/e2e/run.sh", "Makefile",
                "scripts/setup-browser-dev.sh", "pytest.ini", ".githooks/binary-allowlist.txt",
                ".gitattributes", ".github/workflows/qa.yml"):
        _write(repo, rel, "v1\n")
    (repo / "scripts").mkdir(exist_ok=True)
    shutil.copy2(SCRIPT, repo / "scripts" / SCRIPT.name)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _write(repo: Path, rel: str, body: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)


def _commit(repo: Path, msg: str = "change") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg, "--allow-empty")
    return _git(repo, "rev-parse", "HEAD")


def _classify(repo: Path, base: str, head: str = "HEAD", cwd: Path | None = None):
    return subprocess.run(["bash", str(repo / "scripts" / SCRIPT.name), base, head],
                          cwd=cwd or repo, capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "HOME": str(repo)})


def _decision(repo: Path, base: str) -> str:
    r = _classify(repo, base)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert len(lines) == 1, r.stdout
    return lines[0]


def _base(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("rel", ["casa/config.yaml", "casa/rootfs/opt/casa/new.py",
                                 "tests/test_y.py", "docs/a.md", "test-local/e2e/run.sh"])
def test_a_casa_only_change_skips(repo, rel):
    base = _base(repo)
    _write(repo, rel, "v2\n")
    _commit(repo)
    decision = _decision(repo, base)
    assert decision.startswith("skip: no change under casa_browser/ or shared files in "), decision
    assert f"{base}..HEAD" in decision


@pytest.mark.parametrize("rel", [
    "casa_browser/config.yaml", "casa_browser/tests/test_x.py", "casa_browser/CHANGELOG.md",
    "Makefile", "scripts/setup-browser-dev.sh", "scripts/verify_docs.py",
    "pytest.ini", ".githooks/binary-allowlist.txt", ".gitattributes",
    ".github/workflows/qa.yml", "brand-new-top-level.txt", "casa",      # a FILE named casa
    "casa_browserx/a.py", "docsy/a.md", "testsuite/a.py",
])
def test_every_other_path_runs(repo, rel):
    base = _base(repo)
    if rel == "casa":
        shutil.rmtree(repo / "casa")
    _write(repo, rel, "v2\n")
    _commit(repo)
    assert _decision(repo, base) == f"run: {rel} changed in {base}..HEAD"


def test_one_shared_path_among_casa_only_ones_runs(repo):
    base = _base(repo)
    for rel in ("casa/config.yaml", "docs/a.md", "tests/test_y.py"):
        _write(repo, rel, "v2\n")
    _write(repo, "Makefile", "v2\n")
    _commit(repo)
    assert _decision(repo, base).startswith("run: Makefile changed")


def test_a_move_out_of_casa_browser_reports_its_old_path(repo):
    """With rename detection the diff would name only casa/test_x.py, a Casa-only path."""
    base = _base(repo)
    _git(repo, "mv", "casa_browser/tests/test_x.py", "casa/test_x.py")
    _commit(repo)
    assert _decision(repo, base).startswith("run: casa_browser/tests/test_x.py changed")


def test_a_deletion_and_a_mode_change_under_casa_browser_run(repo):
    base = _base(repo)
    (repo / "casa_browser" / "config.yaml").unlink()
    _commit(repo)
    assert _decision(repo, base).startswith("run: casa_browser/config.yaml changed")
    base = _base(repo)
    _git(repo, "update-index", "--chmod=+x", "casa_browser/tests/test_x.py")
    _git(repo, "commit", "-qm", "mode")
    assert _decision(repo, base).startswith("run: casa_browser/tests/test_x.py changed")


def test_the_endpoint_tree_decides_not_the_commits_between(repo):
    """The suite tests the HEAD tree, so a browser edit reverted inside the range skips."""
    base = _base(repo)
    _write(repo, "casa_browser/config.yaml", "v2\n")
    _commit(repo)
    _write(repo, "casa_browser/config.yaml", "v1\n")
    _write(repo, "docs/a.md", "v2\n")
    _commit(repo)
    assert _decision(repo, base).startswith("skip: ")


def test_an_empty_range_skips(repo):
    base = _base(repo)
    assert _decision(repo, base).startswith("skip: ")


def test_odd_names_are_read_whole(repo):
    """-z: a newline or a space in a name cannot split one path into two."""
    base = _base(repo)
    _write(repo, "docs/new\nMakefile", "x\n")
    _write(repo, "casa/a b.txt", "x\n")
    _commit(repo)
    assert _decision(repo, base).startswith("skip: ")
    base = _base(repo)
    _write(repo, "notes\ndocs/x.md", "x\n")
    _commit(repo)
    assert _decision(repo, base).startswith("run: notes\\ndocs/x.md changed")


def test_config_cannot_narrow_the_diff(repo):
    """diff.relative from a subdirectory would hide every path outside it."""
    _git(repo, "config", "diff.relative", "true")
    base = _base(repo)
    _write(repo, "Makefile", "v2\n")
    _commit(repo)
    r = _classify(repo, base, cwd=repo / "docs")
    assert r.returncode == 0 and r.stdout.startswith("run: Makefile changed"), r


def test_an_unknown_revision_is_an_error_not_an_empty_change(repo):
    r = _classify(repo, "0" * 40)
    assert r.returncode != 0
    assert not r.stdout.startswith("skip"), r.stdout


def test_the_roots_the_gate_removes_are_the_roots_that_skip():
    r = subprocess.run(["bash", str(SCRIPT), "--casa-only-roots"], capture_output=True,
                       text=True, check=True)
    assert r.stdout.splitlines() == ["casa", "tests", "docs", "test-local"]


def test_the_gate_runs_the_browser_suite_without_the_casa_only_roots():
    """What makes the Casa-only list self-checking: when the suite does run, it runs in a
    tree from which exactly those roots were removed. Reading the gate text is the cheap
    pin; running the gate end to end is the gate itself."""
    gate = (ROOT / "scripts" / "gate.sh").read_text()
    assert "scripts/browser-suite-affected.sh --casa-only-roots" in gate
    assert 'make -C "$browser_tree"' in gate and "make test-casa" in gate
    assert "\nmake test-unit\n" not in gate
