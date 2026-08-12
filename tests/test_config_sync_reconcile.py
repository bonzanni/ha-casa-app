"""Tests for the three-way /config default-sync reconciler (config_sync.py).

Spec: docs/superpowers/specs/2026-06-08-config-sync-reconciler-design.md.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import config_sync

pytestmark = pytest.mark.unit


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_list_tree_files_only_scopes_agents_and_policies(tmp_path: Path) -> None:
    _write(tmp_path, "agents/butler/voice.yaml", "a")
    _write(tmp_path, "policies/disclosure.yaml", "b")
    _write(tmp_path, "cc-home/.claude/settings.json", "c")   # out of scope
    _write(tmp_path, "marketplace/x.json", "d")              # out of scope
    rels = config_sync._list_tree_files(tmp_path)
    assert rels == {"agents/butler/voice.yaml", "policies/disclosure.yaml"}


def test_list_tree_files_skips_git_and_casabak(tmp_path: Path) -> None:
    _write(tmp_path, "agents/butler/voice.yaml", "a")
    _write(tmp_path, "agents/.git/HEAD", "ref")              # never happens, defensive
    _write(tmp_path, "agents/butler/voice.yaml.casabak", "x")
    rels = config_sync._list_tree_files(tmp_path)
    assert rels == {"agents/butler/voice.yaml"}


def test_bytes_equal(tmp_path: Path) -> None:
    a = tmp_path / "a"; a.write_text("same", encoding="utf-8")
    b = tmp_path / "b"; b.write_text("same", encoding="utf-8")
    c = tmp_path / "c"; c.write_text("diff", encoding="utf-8")
    assert config_sync._bytes_equal(a, b) is True
    assert config_sync._bytes_equal(a, c) is False


def test_report_has_overwrites_and_json_roundtrip() -> None:
    r = config_sync.SyncReport(image_version="v9.9.9")
    assert r.has_overwrites() is False
    r.conflicts.append({"path": "agents/x/y.yaml", "pre_sync_sha": "abc"})
    assert r.has_overwrites() is True
    import json
    parsed = json.loads(r.to_json())
    assert parsed["image_version"] == "v9.9.9"
    assert parsed["conflicts"][0]["path"] == "agents/x/y.yaml"


class _FakeGit:
    """Records snapshot() calls; returns deterministic SHAs."""
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.snapshots: list[str] = []
        self._head = "BASEHEAD"

    def snapshot(self, message: str) -> str | None:
        if not self.available:
            return None
        self.snapshots.append(message)
        self._head = f"SHA{len(self.snapshots)}"
        return self._head

    def head(self) -> str | None:
        return None if not self.available else self._head


def _no_schema(_rel: str) -> None:
    return None  # backstop: nothing is ever schema-invalid


def _run(tmp_path: Path, *, version: str = "v9.9.9", git=None, validate=_no_schema):
    return config_sync.reconcile(
        defaults_dir=tmp_path / "defaults",
        config_dir=tmp_path / "live",
        baseline_dir=tmp_path / "baseline",
        image_version=version,
        git=git or _FakeGit(),
        validate=validate,
    )


def test_create_seeds_missing_file(tmp_path: Path) -> None:
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "NEW")
    # no baseline, no live
    report = _run(tmp_path)
    assert (tmp_path / "live/agents/butler/voice.yaml").read_text() == "NEW"
    assert "agents/butler/voice.yaml" in report.updated


def test_untouched_tracks_image_change(tmp_path: Path) -> None:
    _write(tmp_path / "baseline", "agents/butler/voice.yaml", "OLD")
    _write(tmp_path / "live", "agents/butler/voice.yaml", "OLD")     # == baseline (untouched)
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "NEW") # image changed
    report = _run(tmp_path)
    assert (tmp_path / "live/agents/butler/voice.yaml").read_text() == "NEW"
    assert "agents/butler/voice.yaml" in report.updated


def test_untouched_file_removed_from_defaults_is_deleted(tmp_path: Path) -> None:
    _write(tmp_path / "baseline", "agents/old/runtime.yaml", "X")
    _write(tmp_path / "live", "agents/old/runtime.yaml", "X")        # untouched
    # defaults: file absent
    report = _run(tmp_path)
    assert not (tmp_path / "live/agents/old/runtime.yaml").exists()
    assert "agents/old/runtime.yaml" in report.deleted


def test_user_edit_kept_when_image_unchanged(tmp_path: Path) -> None:
    _write(tmp_path / "baseline", "agents/butler/voice.yaml", "OLD")
    _write(tmp_path / "live", "agents/butler/voice.yaml", "USER")    # != baseline (edited)
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "OLD") # == baseline (image unchanged)
    report = _run(tmp_path)
    assert (tmp_path / "live/agents/butler/voice.yaml").read_text() == "USER"
    assert report.updated == [] and report.conflicts == []


def test_user_edit_kept_when_image_removes_file(tmp_path: Path) -> None:
    _write(tmp_path / "baseline", "agents/butler/voice.yaml", "OLD")
    _write(tmp_path / "live", "agents/butler/voice.yaml", "USER")    # edited
    # defaults: absent
    report = _run(tmp_path)
    assert (tmp_path / "live/agents/butler/voice.yaml").read_text() == "USER"
    assert report.deleted == []


def test_converged_is_noop(tmp_path: Path) -> None:
    _write(tmp_path / "baseline", "agents/butler/voice.yaml", "OLD")
    _write(tmp_path / "live", "agents/butler/voice.yaml", "NEW")     # edited
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "NEW") # live == new
    report = _run(tmp_path)
    assert report.updated == [] and report.conflicts == []


def test_adopt_mode_no_baseline_keeps_existing_live(tmp_path: Path) -> None:
    # First reconciler run on an existing deployment: baseline absent.
    _write(tmp_path / "live", "agents/butler/voice.yaml", "USER")
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "NEW") # differs from live
    git = _FakeGit()
    report = _run(tmp_path, git=git)
    assert (tmp_path / "live/agents/butler/voice.yaml").read_text() == "USER"  # NOT clobbered
    assert report.conflicts == [] and git.snapshots == []           # no overwrite, no pre-sync commit


def test_conflict_image_wins_with_single_presync_commit(tmp_path: Path) -> None:
    # Two conflicting files → exactly ONE pre-sync snapshot, shared SHA.
    for rel in ("agents/butler/voice.yaml", "agents/butler/character.yaml"):
        _write(tmp_path / "baseline", rel, "OLD")
        _write(tmp_path / "live", rel, "USER")    # edited
        _write(tmp_path / "defaults", rel, "NEW") # image also changed → conflict
    git = _FakeGit(available=True)
    report = _run(tmp_path, git=git)
    # image won on both files
    assert (tmp_path / "live/agents/butler/voice.yaml").read_text() == "NEW"
    assert (tmp_path / "live/agents/butler/character.yaml").read_text() == "NEW"
    # exactly one PRE-SYNC commit (a later post-sync commit may also exist);
    # both report entries share its SHA.
    assert sum("pre-sync snapshot" in m for m in git.snapshots) == 1
    shas = {c["pre_sync_sha"] for c in report.conflicts}
    assert shas == {"SHA1"}
    assert report.pre_sync_sha == "SHA1"
    assert {c["path"] for c in report.conflicts} == {
        "agents/butler/voice.yaml", "agents/butler/character.yaml"}


def test_conflict_no_git_writes_casabak(tmp_path: Path) -> None:
    _write(tmp_path / "baseline", "agents/butler/voice.yaml", "OLD")
    _write(tmp_path / "live", "agents/butler/voice.yaml", "USER")
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "NEW")
    git = _FakeGit(available=False)
    report = _run(tmp_path, git=git)
    assert (tmp_path / "live/agents/butler/voice.yaml").read_text() == "NEW"
    assert (tmp_path / "live/agents/butler/voice.yaml.casabak").read_text() == "USER"
    assert "agents/butler/voice.yaml" in report.casabak
    assert report.conflicts[0]["pre_sync_sha"] is None
    assert git.snapshots == []


def test_baseline_updated_to_new_and_second_run_is_noop(tmp_path: Path) -> None:
    _write(tmp_path / "baseline", "agents/butler/voice.yaml", "OLD")
    _write(tmp_path / "live", "agents/butler/voice.yaml", "OLD")     # untouched
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "NEW") # image changed
    # also a removed-from-defaults untouched file
    _write(tmp_path / "baseline", "agents/butler/old.yaml", "G")
    _write(tmp_path / "live", "agents/butler/old.yaml", "G")

    git1 = _FakeGit()
    r1 = _run(tmp_path, git=git1)
    assert "agents/butler/voice.yaml" in r1.updated
    assert "agents/butler/old.yaml" in r1.deleted
    # baseline now mirrors defaults: contains voice.yaml=NEW, no old.yaml
    assert (tmp_path / "baseline/agents/butler/voice.yaml").read_text() == "NEW"
    assert not (tmp_path / "baseline/agents/butler/old.yaml").exists()
    # a post-sync commit happened because files changed
    assert any("default reconcile" in m for m in git1.snapshots)

    # Second run with the SAME defaults → fully converged, no changes, no commit.
    git2 = _FakeGit()
    r2 = _run(tmp_path, git=git2)
    assert r2.updated == [] and r2.deleted == [] and r2.conflicts == []
    assert git2.snapshots == []


def test_no_changes_means_no_commit(tmp_path: Path) -> None:
    _write(tmp_path / "baseline", "agents/butler/voice.yaml", "SAME")
    _write(tmp_path / "live", "agents/butler/voice.yaml", "SAME")
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "SAME")
    git = _FakeGit()
    _run(tmp_path, git=git)
    assert git.snapshots == []


# ---------------------------------------------------------------------------
# M12 — git present but FAILING must degrade to .casabak, never clobber
#       a user edit uncaptured.
# ---------------------------------------------------------------------------


class _BrokenGit:
    """git binary + .git present, but every command fails at runtime (e.g.
    dubious-ownership or a stale index.lock). The fixed RealGit.snapshot()
    returns None on failure; head() must NOT be trusted as a capture."""
    available = True

    def snapshot(self, message: str) -> str | None:
        return None

    def head(self) -> str | None:
        return "STALEHEAD"


def test_conflict_with_broken_git_writes_casabak(tmp_path: Path) -> None:
    _write(tmp_path / "baseline", "agents/butler/voice.yaml", "OLD")
    _write(tmp_path / "live", "agents/butler/voice.yaml", "USER")
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "NEW")
    report = _run(tmp_path, git=_BrokenGit())
    assert (tmp_path / "live/agents/butler/voice.yaml").read_text() == "NEW"
    # user edit recoverable via sidecar
    assert (tmp_path / "live/agents/butler/voice.yaml.casabak").read_text() == "USER"
    assert "agents/butler/voice.yaml" in report.casabak
    # no misleading recovery pointer (must not record STALEHEAD)
    assert report.conflicts[0]["pre_sync_sha"] is None
    assert report.pre_sync_sha is None


def test_schema_backstop_with_broken_git_writes_casabak(tmp_path: Path) -> None:
    # live edited to match defaults? No — make live a distinct edited value that
    # the validator rejects, forcing the schema backstop overwrite path.
    _write(tmp_path / "baseline", "agents/butler/voice.yaml", "OLD")
    _write(tmp_path / "live", "agents/butler/voice.yaml", "USER-INVALID")
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "OLD")  # image unchanged → kept live
    report = _run(
        tmp_path, git=_BrokenGit(),
        validate=lambda rel: "boom" if rel.endswith("voice.yaml") else None,
    )
    # backstop forced the default over the invalid live file
    assert (tmp_path / "live/agents/butler/voice.yaml").read_text() == "OLD"
    assert (tmp_path / "live/agents/butler/voice.yaml.casabak").read_text() == "USER-INVALID"
    assert "agents/butler/voice.yaml" in report.casabak
    assert report.schema_forced[0]["pre_sync_sha"] is None


def _git(repo: Path, *args: str):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def test_real_git_snapshot_fails_closed_on_stale_index_lock(tmp_path: Path) -> None:
    """RealGit.snapshot() must return None (not a stale pre-edit HEAD) when a
    crash leftover .git/index.lock makes `git add` fail."""
    repo = tmp_path / "cfg"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "agents").mkdir()
    (repo / "agents/x.yaml").write_text("v1", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    (repo / "agents/x.yaml").write_text("USER-EDIT", encoding="utf-8")
    (repo / ".git" / "index.lock").write_text("", encoding="utf-8")  # crash leftover
    g = config_sync.RealGit(repo)
    assert g.available is True
    assert g.snapshot("snap") is None  # must not return the stale pre-edit HEAD


# --- #311: deletion-respecting seed arm (absence-valid paths only) ----------


def test_deleted_delegates_stays_deleted_when_image_unchanged(tmp_path: Path) -> None:
    """#311: an operator-deleted agents/*/delegates.yaml (tracked, image copy
    unchanged since baseline) is a deliberate deletion — the seed arm must
    NOT resurrect it every boot."""
    rel = "agents/butler/delegates.yaml"
    _write(tmp_path / "defaults", rel, "SAME")
    _write(tmp_path / "baseline", rel, "SAME")
    # live: absent — the operator deleted the tracked file.
    report = _run(tmp_path)
    assert not (tmp_path / "live" / rel).exists()
    assert rel not in report.updated


def test_deleted_delegates_reseeded_when_image_changes(tmp_path: Path) -> None:
    """The deletion is honored only while the image is unchanged: a new image
    copy reintroduces the file (image change wins, same as the edit rules)."""
    rel = "agents/butler/delegates.yaml"
    _write(tmp_path / "defaults", rel, "NEW")
    _write(tmp_path / "baseline", rel, "OLD")
    report = _run(tmp_path)
    assert (tmp_path / "live" / rel).read_text() == "NEW"
    assert rel in report.updated


def test_deleted_required_file_is_still_reseeded(tmp_path: Path) -> None:
    """Design r2 (Sol S2): deletion-wins is scoped to absence-valid paths.
    A deleted REQUIRED file (runtime.yaml — boot fatals without it) must
    keep being reseeded even when the image is unchanged."""
    rel = "agents/butler/runtime.yaml"
    _write(tmp_path / "defaults", rel, "SAME")
    _write(tmp_path / "baseline", rel, "SAME")
    report = _run(tmp_path)
    assert (tmp_path / "live" / rel).read_text() == "SAME"
    assert rel in report.updated


def test_specialist_delegates_deletion_also_honored(tmp_path: Path) -> None:
    """The absence-valid class is agents/<any-role>/delegates.yaml, not a
    hardcoded resident list."""
    rel = "agents/finance/delegates.yaml"
    _write(tmp_path / "defaults", rel, "SAME")
    _write(tmp_path / "baseline", rel, "SAME")
    report = _run(tmp_path)
    assert not (tmp_path / "live" / rel).exists()
    assert rel not in report.updated


# --- Finding 2: post-sync boot-parity backstop -------------------------------

def _run_with_repo(tmp_path: Path, validate_repo, *, git=None):
    return config_sync.reconcile(
        defaults_dir=tmp_path / "defaults",
        config_dir=tmp_path / "live",
        baseline_dir=tmp_path / "baseline",
        image_version="v9.9.9",
        git=git or _FakeGit(),
        validate=_no_schema,
        validate_repo=validate_repo,
    )


def test_post_sync_heals_reinjected_delegates(tmp_path: Path) -> None:
    """config_sync re-injects the image-owned agents/assistant/delegates.yaml
    the committed tree validly dropped; the backstop must delete the re-seeded
    copy (byte-equal to default) to keep boot alive."""
    _write(tmp_path / "defaults", "agents/assistant/delegates.yaml", "DEFAULT")
    # live has NO delegates.yaml (operator committed its deletion) -> re-seeded.
    live = tmp_path / "live"
    rel = "agents/assistant/delegates.yaml"

    def validate_repo() -> list[str]:
        # boot loader fatals iff the re-injected delegates.yaml is present.
        if (live / rel).exists():
            return ["agent 'assistant': delegates.yaml is non-empty but "
                    "runtime.yaml tools.allowed is missing "
                    "'mcp__casa-framework__delegate_to_agent'"]
        return []

    report = _run_with_repo(tmp_path, validate_repo)
    assert rel in report.post_sync_healed
    assert report.post_sync_errors == []
    assert not (live / rel).exists(), "re-injected delegates.yaml must be removed"


def test_heal_is_stable_on_the_next_boot(tmp_path: Path) -> None:
    """#311 end-to-end: boot 1 seeds the image-owned delegates.yaml, the
    backstop heals it away, the baseline mirrors the image. Boot 2 must be a
    NOOP — no reseed, no re-heal, no `changed` report, no git snapshot —
    instead of re-seeding and re-healing the same file forever."""
    rel = "agents/assistant/delegates.yaml"
    _write(tmp_path / "defaults", rel, "DEFAULT")
    live = tmp_path / "live"

    def validate_repo() -> list[str]:
        if (live / rel).exists():
            return ["agent 'assistant': delegates.yaml is non-empty but "
                    "runtime.yaml tools.allowed is missing "
                    "'mcp__casa-framework__delegate_to_agent'"]
        return []

    git = _FakeGit()
    report1 = _run_with_repo(tmp_path, validate_repo, git=git)
    assert rel in report1.post_sync_healed
    snapshots_after_boot1 = len(git.snapshots)

    report2 = _run_with_repo(tmp_path, validate_repo, git=git)
    assert not (live / rel).exists()
    assert report2.updated == []
    assert report2.post_sync_healed == []
    assert len(git.snapshots) == snapshots_after_boot1, (
        "boot 2 recorded a snapshot for a byte-identical tree — the "
        "seed→heal loop is still alive"
    )


def test_post_sync_does_not_delete_genuine_user_delegates(tmp_path: Path) -> None:
    """A live delegates.yaml that DIFFERS from the image default is genuine
    operator content — never delete it; surface the error instead."""
    _write(tmp_path / "defaults", "agents/assistant/delegates.yaml", "DEFAULT")
    _write(tmp_path / "baseline", "agents/assistant/delegates.yaml", "DEFAULT")
    _write(tmp_path / "live", "agents/assistant/delegates.yaml", "USER-EDIT")
    rel = "agents/assistant/delegates.yaml"

    def validate_repo() -> list[str]:
        return ["agent 'assistant': delegates.yaml is non-empty but "
                "runtime.yaml tools.allowed is missing "
                "'mcp__casa-framework__delegate_to_agent'"]

    report = _run_with_repo(tmp_path, validate_repo)
    assert report.post_sync_healed == []
    assert len(report.post_sync_errors) == 1
    assert (tmp_path / "live" / rel).read_text() == "USER-EDIT"


def test_post_sync_surfaces_unhealable_error(tmp_path: Path) -> None:
    """An error the backstop can't self-heal must be recorded loudly, not
    silently dropped."""
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "X")

    def validate_repo() -> list[str]:
        return ["agent 'butler': character.yaml role 'assistant' != dir 'butler'"]

    report = _run_with_repo(tmp_path, validate_repo)
    assert report.post_sync_healed == []
    assert report.post_sync_errors == [
        "agent 'butler': character.yaml role 'assistant' != dir 'butler'"]


def test_post_sync_validator_absent_is_noop(tmp_path: Path) -> None:
    """No validate_repo injected -> no post-sync fields (back-compat)."""
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "X")
    report = _run_with_repo(tmp_path, None)
    assert report.post_sync_errors == []
    assert report.post_sync_healed == []


def test_post_sync_backstop_never_crashes(tmp_path: Path) -> None:
    """A raising validate_repo must be swallowed — the backstop can't block boot."""
    _write(tmp_path / "defaults", "agents/butler/voice.yaml", "X")

    def boom() -> list[str]:
        raise RuntimeError("validator blew up")

    report = _run_with_repo(tmp_path, boom)  # must not raise
    assert report.post_sync_errors == []


def test_post_sync_real_validator_ignores_stale_specialist_material_dir(
    tmp_path: Path,
) -> None:
    """#213 end-to-end: with the REAL validate_config_repo injected, a live
    tree carrying a pipeline-materialized specialist (dot-named
    ``.{slug}.material-*`` content dir + slug symlink) whose runtime.yaml
    lacks ``kind`` must leave post_sync_errors empty — the exact prod
    symptom on config-sync-report.json this issue closes."""
    from agent_loader import validate_config_repo
    from test_agent_loader import (
        _policies_file, _seed_remaining_residents, _seed_resident,
    )

    live = tmp_path / "live"
    _seed_resident(live / "agents", "assistant")
    _seed_remaining_residents(live)
    _policies_file(live / "policies")
    content = f".demo.material-{'0' * 32}"
    _write(live, f"agents/specialists/{content}/runtime.yaml",
           "schema_version: 1\nmodel: {source: fixed, value: sonnet}\n")
    (live / "agents" / "specialists" / "demo").symlink_to(content)

    report = _run_with_repo(
        tmp_path, lambda: validate_config_repo(str(live)))
    assert report.post_sync_errors == []
