"""Tests for config_git.py — git-backed config history."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git CLI not installed",
)


def _seed(base: Path) -> None:
    (base / "agents").mkdir(parents=True)
    (base / "policies").mkdir(parents=True)
    (base / "schema").mkdir(parents=True)
    (base / "agents" / "marker.txt").write_text("x", encoding="utf-8")


class TestInitRepo:
    def test_creates_git_dir(self, tmp_path):
        from config_git import init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        assert (tmp_path / ".git").is_dir()

    def test_is_idempotent(self, tmp_path):
        from config_git import init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        sha1 = subprocess.check_output(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        ).decode().strip()

        init_repo(str(tmp_path))  # second call
        sha2 = subprocess.check_output(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        ).decode().strip()

        assert sha1 == sha2  # no new commit on re-init

    def test_initial_commit_tracks_agents_policies_schema(self, tmp_path):
        from config_git import init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        tracked = subprocess.check_output(
            ["git", "-C", str(tmp_path), "ls-files"],
        ).decode().splitlines()
        assert "agents/marker.txt" in tracked
        assert ".gitignore" in tracked


class TestCommitConfig:
    def test_returns_sha(self, tmp_path):
        from config_git import init_repo, commit_config

        _seed(tmp_path)
        init_repo(str(tmp_path))

        (tmp_path / "agents" / "new.txt").write_text("y", encoding="utf-8")
        sha = commit_config(str(tmp_path), "add new")

        assert len(sha) == 40  # full sha

    def test_commit_with_no_changes_returns_empty(self, tmp_path):
        from config_git import init_repo, commit_config

        _seed(tmp_path)
        init_repo(str(tmp_path))
        sha = commit_config(str(tmp_path), "no-op")
        assert sha == ""


class TestChangedPaths:
    """#231/#222: the reload guard uses changed_paths to tell a plugin-registry
    persist commit (already activated in-process) from a commit that also edits
    agents/ or policies/ and therefore genuinely owes a reload."""

    def test_lists_paths_touched_by_a_commit(self, tmp_path):
        from config_git import init_repo, commit_config, changed_paths

        _seed(tmp_path)
        init_repo(str(tmp_path))
        (tmp_path / "plugins").mkdir(parents=True, exist_ok=True)
        (tmp_path / "plugins" / "registry.json").write_text("{}", encoding="utf-8")
        (tmp_path / "agents" / "new.txt").write_text("y", encoding="utf-8")
        sha = commit_config(str(tmp_path), "mixed commit")

        paths = changed_paths(str(tmp_path), sha)
        assert "plugins/registry.json" in paths
        assert "agents/new.txt" in paths

    def test_plugins_only_commit(self, tmp_path):
        from config_git import init_repo, commit_config, changed_paths

        _seed(tmp_path)
        init_repo(str(tmp_path))
        (tmp_path / "plugins").mkdir(parents=True, exist_ok=True)
        (tmp_path / "plugins" / "registry.json").write_text("{}", encoding="utf-8")
        sha = commit_config(str(tmp_path), "persist plugin")

        paths = changed_paths(str(tmp_path), sha)
        assert paths == ["plugins/registry.json"]
        assert all(p.startswith("plugins/") for p in paths)

    def test_bad_sha_returns_empty_failsafe(self, tmp_path):
        from config_git import init_repo, changed_paths

        _seed(tmp_path)
        init_repo(str(tmp_path))
        assert changed_paths(str(tmp_path), "deadbeef" * 5) == []


class TestSnapshotManualEdits:
    def test_records_snapshot_when_dirty(self, tmp_path):
        from config_git import init_repo, snapshot_manual_edits

        _seed(tmp_path)
        init_repo(str(tmp_path))
        (tmp_path / "agents" / "marker.txt").write_text("z", encoding="utf-8")
        sha = snapshot_manual_edits(str(tmp_path))
        assert sha is not None

    def test_returns_none_when_clean(self, tmp_path):
        from config_git import init_repo, snapshot_manual_edits

        _seed(tmp_path)
        init_repo(str(tmp_path))
        assert snapshot_manual_edits(str(tmp_path)) is None


class TestRestoreFile:
    def test_restores_prior_content(self, tmp_path):
        from config_git import init_repo, commit_config, restore_file

        _seed(tmp_path)
        init_repo(str(tmp_path))
        original_sha = subprocess.check_output(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        ).decode().strip()

        # Modify + commit so there's a new HEAD.
        (tmp_path / "agents" / "marker.txt").write_text(
            "modified", encoding="utf-8",
        )
        commit_config(str(tmp_path), "modify marker")

        # Restore original.
        restore_file(str(tmp_path), original_sha, "agents/marker.txt")

        # The file should again contain "x", and the restore itself is
        # committed.
        assert (tmp_path / "agents" / "marker.txt").read_text() == "x"


class TestPluginsWhitelist:
    """Unified plugin architecture (v0.71.0): the plugin registry is config
    (tracked + versioned for an audit trail); the content-addressed store +
    staging are binaries, never tracked."""

    def test_registry_json_is_tracked(self, tmp_path):
        from config_git import commit_config, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        pl = tmp_path / "plugins"
        pl.mkdir(parents=True)
        (pl / "registry.json").write_text('{"plugins": []}', encoding="utf-8")
        sha = commit_config(str(tmp_path), "plugins: registry init")
        assert sha, "registry.json write must produce a real commit"
        tracked = subprocess.check_output(
            ["git", "-C", str(tmp_path), "ls-files"],
        ).decode().splitlines()
        assert "plugins/registry.json" in tracked

    def test_plugin_env_conf_stays_untracked(self, tmp_path):
        from config_git import commit_config, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        (tmp_path / "plugin-env.conf").write_text(
            "SECRET=op://x/y/z\n", encoding="utf-8",
        )
        sha = commit_config(str(tmp_path), "should be a no-op")
        assert sha == ""  # mode-0600 secrets file must never enter history
        tracked = subprocess.check_output(
            ["git", "-C", str(tmp_path), "ls-files"],
        ).decode().splitlines()
        assert "plugin-env.conf" not in tracked

    def test_store_and_staging_stay_untracked(self, tmp_path):
        """Pins INV-CFG-004. Red case demonstrated: widening the whitelist to !plugins/** and dropping the store/.staging excludes fails this test."""
        from config_git import commit_config, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        pl = tmp_path / "plugins"
        (pl / "store" / "superpowers" / "abc").mkdir(parents=True)
        (pl / "store" / "superpowers" / "abc" / "skill.md").write_text("x")
        (pl / ".staging" / "xyz").mkdir(parents=True)
        (pl / "registry.json").write_text("{}", encoding="utf-8")
        commit_config(str(tmp_path), "plugins: registry init")
        tracked = subprocess.check_output(
            ["git", "-C", str(tmp_path), "ls-files"],
        ).decode().splitlines()
        assert "plugins/registry.json" in tracked
        assert not any(t.startswith("plugins/store/") for t in tracked)
        assert not any(t.startswith("plugins/.staging/") for t in tracked)

    def test_init_repo_refreshes_stale_gitignore(self, tmp_path):
        """Existing deployments initialized the repo with the OLD whitelist;
        init_repo must reconcile .gitignore on boot, not only on fresh init."""
        from config_git import _GITIGNORE_CONTENT, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        old = "# old whitelist\n*\n!agents/\n!agents/**\n!.gitignore\n"
        (tmp_path / ".gitignore").write_text(old, encoding="utf-8")
        subprocess.check_call(
            ["git", "-C", str(tmp_path), "commit", "-aqm", "simulate old deploy"],
        )

        init_repo(str(tmp_path))  # boot on an existing repo
        assert (tmp_path / ".gitignore").read_text(
            encoding="utf-8") == _GITIGNORE_CONTENT
        status = subprocess.check_output(
            ["git", "-C", str(tmp_path), "status", "--porcelain"],
        ).decode().strip()
        assert status == "", "refreshed .gitignore must be committed, not left dirty"

    def test_setup_configs_heredoc_matches_python_whitelist(self):
        """Two writers own the whitelist (setup-configs.sh fresh-install
        heredoc, config_git fresh-init + boot reconcile) — they drifted once
        (P-3); this pins them together."""
        import re

        from config_git import _GITIGNORE_CONTENT

        sh = (Path(__file__).resolve().parent.parent
              / "casa" / "rootfs" / "etc" / "s6-overlay" / "scripts"
              / "setup-configs.sh").read_text(encoding="utf-8")
        m = re.search(r"cat > \.gitignore <<'EOF'\n(.*?)EOF\n", sh, re.S)
        assert m, "setup-configs.sh .gitignore heredoc not found"
        assert m.group(1) == _GITIGNORE_CONTENT


class TestRestoreFileAddedAfterTarget:
    def test_rolls_back_a_file_added_since_the_target_commit(self, tmp_path):
        """#351 (low): restore_file always ran `git checkout <sha> -- path`,
        which ERRORS when the path did not exist at the target commit — so a
        newly added role config could never be rolled back. The added file
        must be removed (worktree + index) and the removal committed."""
        from config_git import commit_config, init_repo, restore_file

        _seed(tmp_path)
        init_repo(str(tmp_path))
        original_sha = subprocess.check_output(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        ).decode().strip()

        added = tmp_path / "agents" / "newrole.txt"
        added.write_text("new role config", encoding="utf-8")
        commit_config(str(tmp_path), "add newrole")

        restore_file(str(tmp_path), original_sha, "agents/newrole.txt")

        assert not added.exists()
        tracked = subprocess.check_output(
            ["git", "-C", str(tmp_path), "ls-files"],
        ).decode().splitlines()
        assert "agents/newrole.txt" not in tracked
        # The removal is itself a commit (clean tree afterwards).
        status = subprocess.check_output(
            ["git", "-C", str(tmp_path), "status", "--porcelain"],
        ).decode().strip()
        assert status == ""


class TestCommitConfigChecked:
    """#351: the validate→commit TOCTOU. What is validated must be EXACTLY
    what is committed, no matter what an external writer (an SSH operator)
    does to the worktree mid-window."""

    def test_commits_the_validated_snapshot_not_later_edits(self, tmp_path):
        from config_git import commit_config_checked, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        target = tmp_path / "agents" / "marker.txt"
        target.write_text("validated-content", encoding="utf-8")

        seen_trees = []

        def validator(tree):
            # An external edit lands WHILE validation runs — after staging.
            seen_trees.append(
                (Path(tree) / "agents" / "marker.txt").read_text())
            target.write_text("unvalidated-edit", encoding="utf-8")
            return []

        sha, errors = commit_config_checked(
            str(tmp_path), "checked commit", validator)
        assert errors == []
        assert sha
        # The validator saw the staged snapshot…
        assert seen_trees == ["validated-content"]
        # …and the commit contains that snapshot, NOT the mid-window edit.
        committed = subprocess.check_output(
            ["git", "-C", str(tmp_path), "show", f"{sha}:agents/marker.txt"],
        ).decode()
        assert committed == "validated-content"
        # The unvalidated edit stays in the worktree, uncommitted.
        assert target.read_text() == "unvalidated-edit"

    def test_refusal_unstages_and_reports_errors(self, tmp_path):
        from config_git import commit_config_checked, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        head_before = subprocess.check_output(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        ).decode().strip()
        (tmp_path / "agents" / "marker.txt").write_text(
            "broken", encoding="utf-8")

        sha, errors = commit_config_checked(
            str(tmp_path), "refused", lambda tree: ["schema says no"])
        assert sha == ""
        assert errors == ["schema says no"]
        head_after = subprocess.check_output(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        ).decode().strip()
        assert head_after == head_before          # nothing committed
        # The change is back to unstaged (index reset), worktree intact.
        status = subprocess.check_output(
            ["git", "-C", str(tmp_path), "status", "--porcelain"],
        ).decode()
        assert " M agents/marker.txt" in status
        assert (tmp_path / "agents" / "marker.txt").read_text() == "broken"

    def test_error_paths_are_rewritten_to_config_dir(self, tmp_path):
        """Validator messages carry the export-tree tmp path; the caller
        (and the operator reading the refusal) must see /config-relative
        paths instead."""
        from config_git import commit_config_checked, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        (tmp_path / "agents" / "marker.txt").write_text(
            "broken", encoding="utf-8")

        def validator(tree):
            return [f"{tree}/agents/marker.txt: invalid"]

        _sha, errors = commit_config_checked(
            str(tmp_path), "refused", validator)
        assert errors == [f"{tmp_path}/agents/marker.txt: invalid"]

    def test_noop_skips_validation(self, tmp_path):
        from config_git import commit_config_checked, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        calls = []
        sha, errors = commit_config_checked(
            str(tmp_path), "noop", lambda tree: calls.append(tree) or [])
        assert (sha, errors) == ("", [])
        assert calls == []


class TestCommitConfigCheckedPreservesStaging:
    def test_refusal_leaves_prior_manual_staging_intact(self, tmp_path):
        """Terra r1-1: the gate must not corrupt index state it did not
        create. An operator's intentionally staged (uncommitted) edit must
        survive a refused checked commit byte-for-byte."""
        from config_git import commit_config_checked, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        staged = tmp_path / "agents" / "staged.txt"
        staged.write_text("operator staged this", encoding="utf-8")
        subprocess.check_call(
            ["git", "-C", str(tmp_path), "add", "agents/staged.txt"])

        (tmp_path / "agents" / "marker.txt").write_text(
            "invalid", encoding="utf-8")

        sha, errors = commit_config_checked(
            str(tmp_path), "refused", lambda tree: ["no"])
        assert sha == "" and errors == ["no"]
        cached = subprocess.check_output(
            ["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
        ).decode().splitlines()
        assert "agents/staged.txt" in cached, (
            "refusal wiped the operator's pre-existing staging")

    def test_unexpected_validator_exception_leaves_repo_untouched(
        self, tmp_path,
    ):
        """Sol r1-1: an exception mid-gate must not leave the real index
        holding an unvalidated staged snapshot."""
        from config_git import commit_config_checked, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        (tmp_path / "agents" / "marker.txt").write_text(
            "edited", encoding="utf-8")

        def validator(tree):
            raise OSError("filesystem hiccup")

        with pytest.raises(OSError):
            commit_config_checked(str(tmp_path), "boom", validator)
        # Real index untouched: nothing staged.
        cached = subprocess.check_output(
            ["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
        ).decode().strip()
        assert cached == ""

    def test_concurrent_external_staging_cannot_enter_the_commit(
        self, tmp_path,
    ):
        """Sol r1-2: content added to the SHARED index while validation runs
        (boot snapshot, an SSH operator's `git add`) must not ride into the
        checked commit — the commit is built from the private validated
        tree."""
        from config_git import commit_config_checked, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        (tmp_path / "agents" / "marker.txt").write_text(
            "validated", encoding="utf-8")
        sneaky = tmp_path / "agents" / "sneaky.txt"

        def validator(tree):
            sneaky.write_text("injected mid-window", encoding="utf-8")
            subprocess.check_call(
                ["git", "-C", str(tmp_path), "add", "agents/sneaky.txt"])
            return []

        sha, errors = commit_config_checked(
            str(tmp_path), "checked", validator)
        assert errors == [] and sha
        committed = subprocess.check_output(
            ["git", "-C", str(tmp_path), "ls-tree", "-r", "--name-only", sha],
        ).decode().splitlines()
        assert "agents/sneaky.txt" not in committed


class TestRestoreFileBadSha:
    def test_malformed_sha_raises_instead_of_deleting(self, tmp_path):
        """Sol r1-4: a bogus target sha must raise — pre-fix the failed
        cat-file probe was read as 'path absent at target' and the LIVE file
        was deleted and the deletion committed."""
        from config_git import init_repo, restore_file

        _seed(tmp_path)
        init_repo(str(tmp_path))
        target = tmp_path / "agents" / "marker.txt"
        assert target.exists()
        with pytest.raises(subprocess.CalledProcessError):
            restore_file(str(tmp_path), "not-a-sha", "agents/marker.txt")
        assert target.exists()

    def test_head_moved_mid_gate_refuses_instead_of_orphaning(self, tmp_path):
        """The ref update is compare-and-swap: an external commit landing
        while the gate validates must make the gate FAIL (caller retries on
        the new HEAD) — a plain overwrite would orphan the external commit."""
        from config_git import commit_config_checked, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        (tmp_path / "agents" / "marker.txt").write_text(
            "gate content", encoding="utf-8")

        def validator(tree):
            # External writer commits mid-window.
            (tmp_path / "agents" / "external.txt").write_text(
                "external commit", encoding="utf-8")
            subprocess.check_call(
                ["git", "-C", str(tmp_path), "add", "agents/external.txt"])
            subprocess.check_call(
                ["git", "-C", str(tmp_path), "commit", "-qm", "external"])
            return []

        with pytest.raises(subprocess.CalledProcessError):
            commit_config_checked(str(tmp_path), "gate", validator)
        # The external commit survives at HEAD.
        msg = subprocess.check_output(
            ["git", "-C", str(tmp_path), "log", "-1", "--format=%s"],
        ).decode().strip()
        assert msg == "external"
        # Sol r4-1: the pre-CAS index refresh must be UNDONE on CAS failure
        # — otherwise the gate's content stays staged over the external
        # commit and a later ordinary commit silently reverts it. The gate's
        # worktree edit must be visible as an UNSTAGED modification only.
        status = subprocess.check_output(
            ["git", "-C", str(tmp_path), "status", "--porcelain"],
        ).decode().splitlines()
        assert status == [" M agents/marker.txt"]

    def test_success_preserves_concurrent_manual_staging(self, tmp_path):
        """Terra r2-1: a successful checked commit must not drop an
        operator's mid-window `git add` from the real index. The reset that
        refreshes the index is limited to the paths the commit touched, so
        an unrelated concurrently-staged entry stays staged (and the tree
        shows no phantom diffs)."""
        from config_git import commit_config_checked, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        (tmp_path / "agents" / "marker.txt").write_text(
            "validated", encoding="utf-8")
        other = tmp_path / "agents" / "other.txt"

        def validator(tree):
            other.write_text("operator work", encoding="utf-8")
            subprocess.check_call(
                ["git", "-C", str(tmp_path), "add", "agents/other.txt"])
            return []

        sha, errors = commit_config_checked(
            str(tmp_path), "checked", validator)
        assert errors == [] and sha
        status = subprocess.check_output(
            ["git", "-C", str(tmp_path), "status", "--porcelain"],
        ).decode().splitlines()
        # Exactly the operator's staged add remains — nothing else dirty,
        # no phantom staged deletions from an over-broad reset.
        assert status == ["A  agents/other.txt"]

    def test_glob_looking_filenames_are_treated_literally(self, tmp_path):
        """Sol r3-1: reset pathspecs must be literal — a committed
        `foo[1].txt` must not glob-match a concurrently staged `foo1.txt`
        (dropping its staging) nor miss its own refresh."""
        from config_git import commit_config_checked, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        (tmp_path / "agents" / "foo[1].txt").write_text(
            "globby name", encoding="utf-8")
        sibling = tmp_path / "agents" / "foo1.txt"

        def validator(tree):
            sibling.write_text("operator work", encoding="utf-8")
            subprocess.check_call(
                ["git", "-C", str(tmp_path), "add", "agents/foo1.txt"])
            return []

        sha, errors = commit_config_checked(
            str(tmp_path), "globby", validator)
        assert errors == [] and sha
        status = subprocess.check_output(
            ["git", "-C", str(tmp_path), "status", "--porcelain"],
        ).decode().splitlines()
        # The sibling's staging survives; the committed globby file is
        # fully refreshed (absent from status).
        assert status == ["A  agents/foo1.txt"]

    def test_pre_staged_version_of_committed_path_survives(self, tmp_path):
        """Terra r5-1: operator stages version A of a path, worktree moves
        to B, the gate commits B. A must SURVIVE in the index (staged
        against the new HEAD) — main's add -A flow destroyed it; the gate
        must do better, not equal."""
        from config_git import commit_config_checked, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        target = tmp_path / "agents" / "marker.txt"
        target.write_text("version A", encoding="utf-8")
        subprocess.check_call(
            ["git", "-C", str(tmp_path), "add", "agents/marker.txt"])
        target.write_text("version B", encoding="utf-8")

        sha, errors = commit_config_checked(
            str(tmp_path), "commits B", validate=lambda tree: [])
        assert errors == [] and sha
        committed = subprocess.check_output(
            ["git", "-C", str(tmp_path), "show", f"{sha}:agents/marker.txt"],
        ).decode()
        assert committed == "version B"
        staged = subprocess.check_output(
            ["git", "-C", str(tmp_path), "show", ":agents/marker.txt"],
        ).decode()
        assert staged == "version A", (
            "the operator's staged version was destroyed by the index "
            "refresh")
        status = subprocess.check_output(
            ["git", "-C", str(tmp_path), "status", "--porcelain"],
        ).decode().splitlines()
        assert status == ["MM agents/marker.txt"]


class TestCasUndoPreservesLateStaging:
    def test_operator_staging_after_refresh_survives_cas_undo(
        self, tmp_path, monkeypatch,
    ):
        """Sol r5-2: the CAS-failure undo must reset only entries still
        holding the gate's own content — an operator who stages newer
        content for a touched path between the refresh and the failed CAS
        keeps their staging."""
        import config_git
        from config_git import commit_config_checked, init_repo

        _seed(tmp_path)
        init_repo(str(tmp_path))
        target = tmp_path / "agents" / "marker.txt"
        target.write_text("gate content", encoding="utf-8")

        real_run = config_git._run

        def hooked(cwd, args, **kw):
            if args[:2] == ["update-ref", "HEAD"]:
                # Between refresh and CAS: an external commit moves HEAD
                # (via a second file so the index-wide commit is explicit),
                # then the operator stages NEWER content for the touched
                # path.
                ext = tmp_path / "agents" / "external.txt"
                ext.write_text("external", encoding="utf-8")
                subprocess.check_call(
                    ["git", "-C", str(tmp_path), "add", "agents/external.txt"])
                subprocess.check_call(
                    ["git", "-C", str(tmp_path), "commit", "-qm", "external"])
                target.write_text("operator newer", encoding="utf-8")
                subprocess.check_call(
                    ["git", "-C", str(tmp_path), "add", "agents/marker.txt"])
                target.write_text("gate content", encoding="utf-8")
            return real_run(cwd, args, **kw)

        monkeypatch.setattr(config_git, "_run", hooked)

        with pytest.raises(subprocess.CalledProcessError):
            commit_config_checked(
                str(tmp_path), "gate", validate=lambda tree: [])
        staged = subprocess.check_output(
            ["git", "-C", str(tmp_path), "show", ":agents/marker.txt"],
        ).decode()
        assert staged == "operator newer", (
            "the CAS undo erased the operator's post-refresh staging")


# ---------------------------------------------------------------------------
# #1097: the config repo's admitted set, measured through real git.
# ---------------------------------------------------------------------------

_C5_EXPECTED = {
    ".gitignore",
    "agents/probe/nested/config.txt",
    "policies/probe/nested/config.txt",
    "bindings/probe/nested/config.txt",
    "schema/probe/nested/config.txt",
    "plugins/registry.json",
}
_C5_SUMMARY = "agents/, policies/, bindings/, schema/, and plugins/registry.json"
_C5_EXCLUDED = {
    "specialists/registry.json",
    "specialists/probe/active.yaml",
    "specialists/probe/desired.yaml",
    "specialists/probe/active.prior.yaml",
    "specialists/.roles-overlay/probe/role.yaml",
    "specialists/store/probe/component.yaml",
    "specialists/.staging/probe/component.yaml",
    "plugins/store/probe/component.json",
    "plugins/.staging/probe/component.json",
    "plugin-env.conf",
    "unlisted.txt",
}
_C5_SENTINEL = b"C5_LEGACY_PLAINTEXT_SENTINEL"
_C5_TUPLE_BYTES = b"config_snapshot:\n  password: " + _C5_SENTINEL + b"\n"
_C5_TUPLES = (
    "specialists/probe/active.yaml",
    "specialists/probe/desired.yaml",
    "specialists/probe/active.prior.yaml",
)

# The whitelist the config repo carried at 59ed58d8 — frozen here, never
# derived from the candidate constant, so an existing install's reconcile is
# exercised from the bytes it actually holds.
_C5_BASE_WHITELIST = """\
# Casa config repo — track configs only.
*
!agents/
!agents/**
!policies/
!policies/**
!bindings/
!bindings/**
!schema/
!schema/**
# Unified plugin architecture (v0.71.0): the registry is config — the single
# plugin-assignment authority — and versioning it gives an audit trail.
# ONLY registry.json: the artifact store and staging under plugins/ are
# content-addressed binaries, never tracked.
!plugins/
!plugins/registry.json
plugins/store/
plugins/.staging/
# Installed-specialist data model (Task 13): registry.json is config — same
# audit-trail rationale as plugins/registry.json above. ONLY the per-slug
# active/desired/prior tuples and the top-level registry are tracked; the
# content-addressed component store and staging are binaries, never tracked.
!specialists/
!specialists/registry.json
!specialists/*/active.yaml
!specialists/*/desired.yaml
!specialists/*/active.prior.yaml
specialists/store/
specialists/.staging/
!.gitignore
"""


def _c5_git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def _c5_nul_set(raw: bytes) -> list[str]:
    return [p.decode() for p in raw.split(b"\0") if p]


def _c5_count(root: Path) -> int:
    return int(_c5_git(root, "rev-list", "--count", "HEAD").decode().strip())


def _c5_index(root: Path) -> list[str]:
    return _c5_nul_set(_c5_git(root, "ls-files", "-z"))


def _c5_head(root: Path) -> list[str]:
    return _c5_nul_set(_c5_git(root, "ls-tree", "-r", "--name-only", "-z", "HEAD"))


def _c5_changed(root: Path, sha: str) -> list[str]:
    return _c5_nul_set(_c5_git(
        root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", sha))


def _c5_seed(root: Path, content: bytes) -> None:
    for rel in (_C5_EXPECTED - {".gitignore"}) | _C5_EXCLUDED:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)


def _c5_assert_tracked(root: Path) -> None:
    index, head = _c5_index(root), _c5_head(root)
    assert (len(index), len(head)) == (6, 6), (index, head)
    assert set(index) == set(head) == _C5_EXPECTED


def _c5_assert_positive_commit(root: Path, sha: str, count: int) -> None:
    assert sha and sha == _c5_git(root, "rev-parse", "HEAD").decode().strip()
    assert _c5_count(root) == count
    changed = _c5_changed(root, sha)
    assert len(changed) == 5, changed
    assert set(changed) == _C5_EXPECTED - {".gitignore"}
    _c5_assert_tracked(root)


class TestConfigRepoAdmittedSet:
    """#1097: the whitelist admits exactly the configuration it names — no
    path under specialists/ — and the tracked-path summary says exactly
    that. Pins INV-CFG-004 (declared scope: admission of untracked paths;
    nothing here promises eviction of an already-tracked path)."""

    def test_config_whitelist_admits_exact_literal_set_fresh(self, tmp_path):
        from config_git import commit_config, init_repo, snapshot_manual_edits

        root = tmp_path
        _c5_seed(root, b"v1\n")
        init_repo(str(root))
        assert _c5_count(root) == 1
        _c5_assert_tracked(root)

        _c5_seed(root, b"v2\n")
        sha = commit_config(str(root), "probe commit")
        _c5_assert_positive_commit(root, sha, 2)

        _c5_seed(root, b"v3\n")
        sha = snapshot_manual_edits(str(root))
        _c5_assert_positive_commit(root, sha, 3)

    def test_config_whitelist_admits_exact_literal_set_after_reconcile(
            self, tmp_path):
        import config_git
        from config_git import commit_config, init_repo, snapshot_manual_edits

        root = tmp_path
        _c5_git(root, "init", "-q")
        _c5_git(root, "config", "user.email", "casa@local")
        _c5_git(root, "config", "user.name", "Casa")
        (root / ".gitignore").write_text(_C5_BASE_WHITELIST, encoding="utf-8")
        _c5_git(root, "add", ".gitignore")
        _c5_git(root, "commit", "-qm", "base whitelist")
        assert _c5_count(root) == 1

        init_repo(str(root))
        n = _c5_count(root)
        assert n == 1 + int(
            config_git._GITIGNORE_CONTENT != _C5_BASE_WHITELIST)

        _c5_seed(root, b"v1\n")
        sha = commit_config(str(root), "probe add")
        assert sha and _c5_count(root) == n + 1
        added = _c5_changed(root, sha)
        assert len(added) == 5, added
        assert set(added) == _C5_EXPECTED - {".gitignore"}
        _c5_assert_tracked(root)

        _c5_seed(root, b"v2\n")
        sha = commit_config(str(root), "probe commit")
        _c5_assert_positive_commit(root, sha, n + 2)

        _c5_seed(root, b"v3\n")
        sha = snapshot_manual_edits(str(root))
        _c5_assert_positive_commit(root, sha, n + 3)

    async def test_config_git_summary_matches_literal_set_on_both_tool_surfaces(
            self, tmp_path, monkeypatch):
        import json

        import agent as agent_mod
        import config_git
        import tools as tools_mod
        from config_git import commit_config, init_repo

        root = tmp_path
        init_repo(str(root))
        assert _c5_count(root) == 1
        assert set(_c5_index(root)) == set(_c5_head(root)) == {".gitignore"}

        tup = root / "specialists" / "probe" / "active.yaml"
        tup.parent.mkdir(parents=True)
        tup.write_bytes(_C5_TUPLE_BYTES)
        assert commit_config(str(root), "tuple-only direct") == ""
        assert _c5_count(root) == 1

        real_checked = config_git.commit_config_checked
        calls = []

        def checked(config_dir, message, validate):
            assert config_dir == "/config"
            calls.append(message)
            return real_checked(str(root), message, validate)

        monkeypatch.setattr(config_git, "commit_config_checked", checked)
        tok = agent_mod.origin_var.set({"role": "configurator"})
        try:
            result = await tools_mod.config_git_commit.handler(
                {"message": "tuple-only tool"})
        finally:
            agent_mod.origin_var.reset(tok)
        payload = json.loads(result["content"][0]["text"])

        assert calls == ["tuple-only tool"]
        assert payload["sha"] == ""
        assert payload["message"] == "tuple-only tool"
        assert _c5_count(root) == 1
        assert set(_c5_index(root)) == set(_c5_head(root)) == {".gitignore"}

        description = tools_mod.config_git_commit.description
        assert config_git.TRACKED_PATHS_SUMMARY == _C5_SUMMARY
        assert description.count(_C5_SUMMARY) == 1
        assert payload["warning"].count(_C5_SUMMARY) == 1
        assert description.count("specialists/") == 0
        assert payload["warning"].count("specialists/") == 0

    def test_base_whitelist_reconcile_never_records_plaintext_tuples(
            self, tmp_path):
        """Green at 59ed58d8 and after the fix — the safety pin: an existing
        install's reconcile and boot snapshot never admit a tuple. Covers the
        Python boot path; the shell fresh-init heredoc is bound to the same
        constant by test_setup_configs_heredoc_matches_python_whitelist."""
        import config_git
        from config_git import init_repo, snapshot_manual_edits

        root = tmp_path
        for rel in _C5_EXPECTED - {".gitignore"}:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"v1\n")
        for rel in _C5_TUPLES:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(_C5_TUPLE_BYTES)
        _c5_git(root, "init", "-q")
        _c5_git(root, "config", "user.email", "casa@local")
        _c5_git(root, "config", "user.name", "Casa")
        (root / ".gitignore").write_text(_C5_BASE_WHITELIST, encoding="utf-8")
        _c5_git(root, "add", "-A")
        _c5_git(root, "commit", "-qm", "base install")
        assert _c5_count(root) == 1
        _c5_assert_tracked(root)
        base_head = _c5_git(root, "rev-parse", "HEAD").decode().strip()

        delta = int(config_git._GITIGNORE_CONTENT != _C5_BASE_WHITELIST)
        init_repo(str(root))
        assert _c5_count(root) == 1 + delta
        assert snapshot_manual_edits(str(root)) is None
        assert _c5_count(root) == 1 + delta
        init_repo(str(root))
        assert _c5_count(root) == 1 + delta

        new = _c5_git(root, "rev-list", f"{base_head}..HEAD").decode().split()
        assert len(new) == delta
        for sha in new:
            assert _c5_changed(root, sha) == [".gitignore"]
        _c5_assert_tracked(root)
        for rel in _C5_TUPLES:
            assert (root / rel).read_bytes() == _C5_TUPLE_BYTES

        objects = _c5_git(root, "rev-list", "--objects", "--all").decode()
        blobs = set()
        for line in objects.splitlines():
            oid = line.split()[0]
            if _c5_git(root, "cat-file", "-t", oid).strip() == b"blob":
                blobs.add(oid)
        assert blobs
        hits = [o for o in blobs
                if _C5_SENTINEL in _c5_git(root, "cat-file", "blob", o)]
        assert len(hits) == 0
