"""#1391: a boot that re-binds a resident commits the binding files itself.

The re-bind happens inside ``load_all_agents``, after the boot snapshot, so
before the fix ``bindings/resident-*/active*.yaml`` stayed uncommitted until
the next unrelated commit swept them in under its own message.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git CLI not installed",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True).strip()


def test_boot_rebind_lands_in_its_own_commit(tmp_path, monkeypatch):
    import casa_core
    from config_git import init_repo, snapshot_manual_edits

    resident = tmp_path / "bindings" / "resident-assistant"
    resident.mkdir(parents=True)
    (resident / "active.yaml").write_text("role_checksum: old\n")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "note.yaml").write_text("a: 1\n")
    init_repo(str(tmp_path))
    snapshot_manual_edits(str(tmp_path))
    base = _git(tmp_path, "rev-parse", "HEAD")

    def fake_load(agents_dir, *, policies=None):
        # What reconcile_resident_binding does when the shipped role changed.
        (resident / "active.prior.yaml").write_text("role_checksum: old\n")
        (resident / "active.yaml").write_text("role_checksum: new\n")
        # An unrelated edit landing meanwhile must not ride in this commit.
        (tmp_path / "agents" / "note.yaml").write_text("a: 2\n")
        return {"assistant": object()}

    monkeypatch.setattr(casa_core, "load_all_agents", fake_load)

    out = casa_core._load_agents_at_boot(
        str(tmp_path / "agents"), policies=None, config_dir=str(tmp_path))

    assert list(out) == ["assistant"]
    commits = _git(tmp_path, "log", "--format=%s", f"{base}..HEAD").splitlines()
    assert len(commits) == 1
    assert "re-bound" in commits[0]
    changed = sorted(_git(
        tmp_path, "diff-tree", "--no-commit-id", "--name-only", "-r",
        "HEAD").splitlines())
    assert changed == [
        "bindings/resident-assistant/active.prior.yaml",
        "bindings/resident-assistant/active.yaml",
    ]
    # Only the unrelated edit is left pending.
    assert _git(tmp_path, "status", "--porcelain") == "M agents/note.yaml"


def test_boot_without_rebind_makes_no_commit(tmp_path, monkeypatch):
    import casa_core
    from config_git import init_repo

    (tmp_path / "bindings" / "resident-assistant").mkdir(parents=True)
    (tmp_path / "bindings" / "resident-assistant" / "active.yaml").write_text("x\n")
    init_repo(str(tmp_path))
    base = _git(tmp_path, "rev-parse", "HEAD")

    monkeypatch.setattr(casa_core, "load_all_agents",
                        lambda agents_dir, *, policies=None: {})
    casa_core._load_agents_at_boot(
        str(tmp_path / "agents"), policies=None, config_dir=str(tmp_path))

    assert _git(tmp_path, "rev-parse", "HEAD") == base


def test_main_loads_agents_through_the_recording_helper():
    import inspect

    import casa_core

    src = inspect.getsource(casa_core.main)
    assert "_load_agents_at_boot" in src
    assert "load_all_agents, agents_dir" not in src
