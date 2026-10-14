"""#1436: a tracked file Casa writes itself is committed by Casa.

A resident's reminder lives in its ``triggers.yaml``, which the config repo
tracks, and a resident cannot commit. Before the fix, setting or cancelling a
reminder left the file dirty until an unrelated configurator commit swept it in
under its own message. (The plugin registry half is pinned in
test_plugin_persist_commit_real_repo.py.)
"""
from __future__ import annotations

import shutil
import subprocess

import pytest
import yaml

import config_git
import reminders

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git CLI not installed")


def _git(repo, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "config"
    role = root / "agents" / "assistant"
    role.mkdir(parents=True)
    (role / "triggers.yaml").write_text(yaml.safe_dump({
        "schema_version": 2,
        "triggers": [{"name": "heartbeat", "type": "interval", "minutes": 60,
                      "channel": "telegram", "prompt": "hb"}]},
        sort_keys=False), encoding="utf-8")
    (role / "notes.md").write_text("v1\n", encoding="utf-8")
    config_git.init_repo(str(root))
    monkeypatch.setattr(config_git, "CONFIG_DIR", str(root))
    return root


def _entry(name):
    return {"name": name, "type": "date", "at": "2099-08-03T20:00:00+02:00",
            "one_shot": True, "channel": "telegram",
            "prompt": 'Send this: "Bins."', "managed_by": "agent"}


def test_set_and_cancel_each_land_in_their_own_commit(repo):
    path = str(repo / "agents" / "assistant" / "triggers.yaml")
    base = _git(repo, "rev-parse", "HEAD")
    # An unrelated edit meanwhile must not ride in the reminder's commit.
    (repo / "agents" / "assistant" / "notes.md").write_text("v2\n")

    reminders.add_entry(path, _entry("reminder-a1b2c3d4"))
    assert reminders.remove_entry(path, "reminder-a1b2c3d4") == "removed"

    subjects = _git(repo, "log", "--format=%s", f"{base}..HEAD").splitlines()
    assert subjects == ["casa: reminder reminder-a1b2c3d4 removed",
                        "casa: reminder reminder-a1b2c3d4 set"]
    for sha in ("HEAD", "HEAD~1"):
        assert _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r",
                    sha) == "agents/assistant/triggers.yaml"
    assert _git(repo, "status", "--porcelain") == "M agents/assistant/notes.md"


def test_a_refused_or_absent_removal_commits_nothing(repo):
    path = str(repo / "agents" / "assistant" / "triggers.yaml")
    base = _git(repo, "rev-parse", "HEAD")
    assert reminders.remove_entry(path, "heartbeat") == "not_owned"
    assert reminders.remove_entry(path, "nope") == "not_found"
    assert _git(repo, "rev-parse", "HEAD") == base


def test_a_write_outside_the_config_repo_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(config_git, "CONFIG_DIR", str(tmp_path / "config"))
    other = tmp_path / "elsewhere" / "triggers.yaml"
    other.parent.mkdir()
    other.write_text("schema_version: 2\ntriggers: []\n")
    assert config_git.commit_casa_write(str(other), "x") == ""
    reminders.add_entry(str(other), _entry("reminder-00000000"))  # no raise


def test_a_git_failure_never_fails_the_write(repo, monkeypatch):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "git")
    monkeypatch.setattr(config_git, "commit_config", boom)
    path = str(repo / "agents" / "assistant" / "triggers.yaml")
    reminders.add_entry(path, _entry("reminder-11111111"))
    assert "reminder-11111111" in (repo / "agents" / "assistant"
                                   / "triggers.yaml").read_text()
