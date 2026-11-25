"""scripts/keep-suite-output.sh: a failing suite's output is kept, a passing one's is not.

The gate runs its suites through this script so that a test which fails once in a
parallel run and passes on the re-run still leaves its traceback behind.
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "keep-suite-output.sh"


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t", *args],
        cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "c")
    return repo


def _run(cwd, *cmd):
    return subprocess.run([str(SCRIPT), *cmd], cwd=cwd, capture_output=True, text=True)


def _kept(repo):
    return sorted((repo / ".git" / "casa-gate-failures").glob("*.log"))


def test_a_failing_command_keeps_its_output_and_its_status(tmp_path):
    repo = _repo(tmp_path)
    done = _run(repo, "casa", "sh", "-c", "echo to-stdout; echo to-stderr >&2; exit 7")
    assert done.returncode == 7
    assert "to-stdout" in done.stdout and "to-stderr" in done.stdout
    (kept,) = _kept(repo)
    assert kept.read_text() == "to-stdout\nto-stderr\n"
    assert f"-{_git(repo, 'rev-parse', '--short=9', 'HEAD')}-casa." in kept.name
    assert str(kept) in done.stderr


def test_a_passing_command_keeps_nothing(tmp_path):
    repo = _repo(tmp_path)
    done = _run(repo, "casa", "sh", "-c", "echo fine")
    assert done.returncode == 0
    assert done.stdout == "fine\n"
    assert _kept(repo) == []
    assert list((repo / ".git").glob("casa-gate-*")) == []


def test_a_failure_in_a_worktree_is_kept_in_the_common_directory(tmp_path):
    repo = _repo(tmp_path)
    tree = tmp_path / "tree"
    _git(repo, "worktree", "add", "-q", "--detach", str(tree))
    done = _run(tree, "browser", "sh", "-c", "echo boom; exit 1")
    assert done.returncode == 1
    (kept,) = _kept(repo)
    assert kept.read_text() == "boom\n" and "-browser." in kept.name


def test_two_failures_keep_two_files(tmp_path):
    """With the clock held still, so the two names differ only by the unique suffix."""
    repo = _repo(tmp_path)
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "date").write_text("#!/bin/sh\necho 20260101T000000Z\n")
    (fake / "date").chmod(0o755)
    env = {**os.environ, "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}
    for word in ("first", "second"):
        done = subprocess.run(
            [str(SCRIPT), "casa", "sh", "-c", f"echo {word}; exit 1"],
            cwd=repo, env=env, capture_output=True, text=True)
        assert done.returncode == 1
    assert all(k.name.startswith("20260101T000000Z-") for k in _kept(repo))
    assert sorted(k.read_text() for k in _kept(repo)) == ["first\n", "second\n"]


def test_output_is_shown_while_the_command_still_runs(tmp_path):
    repo = _repo(tmp_path)
    proc = subprocess.Popen(
        [str(SCRIPT), "casa", "sh", "-c", "echo started; read _; exit 3"],
        cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline() == "started\n"   # read before the command may end
        assert proc.poll() is None
        proc.stdin.write("go\n")
        proc.stdin.close()
        assert proc.wait(timeout=30) == 3
    finally:
        proc.kill()


def test_the_gate_runs_both_suites_through_it_and_a_failure_stops_it():
    """Whole lines, so a status-swallowing suffix on either call fails here."""
    gate = (ROOT / "scripts" / "gate.sh").read_text()
    assert "\nset -euo pipefail\n" in gate
    assert "\nscripts/keep-suite-output.sh casa make test-casa\n" in gate
    assert ("\n    scripts/keep-suite-output.sh browser \\\n"
            '      make -C "$browser_tree" --no-print-directory test-browser PYTEST_ARGS= \\\n'
            '        BROWSER_PY="$PWD/venv_browser/bin/python"\n    ;;\n') in gate
