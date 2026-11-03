"""Task 2: retention."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from casa_browser.protocol import (
    Admission,
    FileRecord,
    RunRecord,
    RunSpec,
    SafeEvent,
    SafeNotice,
    Site,
    SiteGrant,
)
from casa_browser.retention import sweep
from casa_browser.store import Store

NOW = datetime(2026, 11, 30, 12, 0, tzinfo=timezone.utc)


def _spec(n: int) -> RunSpec:
    return RunSpec(f"req{n}", "finance", "account", "t", "k", (SiteGrant("site-a", "account"),), "",
                   Admission("operator_telegram", "o", "sha256:" + "0" * 64))


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "state.db")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    store.put_site(Site("site-a", "A", ("https://a.example",), (), None, (), None, 1))
    yield store, artifacts
    store.close()


def _run_with_artifacts(store: Store, artifacts: Path, n: int, ended: datetime | None,
                        state: str = "completed") -> list[Path]:
    store.create_run(_spec(n), f"run{n}", now=ended or NOW)
    store.append_event(f"run{n}", SafeEvent(0, f"run{n}", "step", None, None, "x", "ok", None, NOW))
    paths = []
    for kind in ("file", "shot"):
        p = artifacts / f"{kind}{n}.bin"
        p.write_bytes(b"data")
        paths.append(p)
    store.put_file(FileRecord(f"file{n}", f"run{n}", "finance", "a.pdf", "application/pdf", 4,
                              "0" * 64, str(paths[0])))
    store.put_screenshot(f"shot{n}", f"run{n}", str(paths[1]))
    store.put_record(RunRecord(f"run{n}", "finance", ("site-a",), "s", (), (f"file{n}",), (), ended or NOW))
    if ended is not None:
        store._db.execute("UPDATE runs SET state=?, ended_at=? WHERE run_id=?",
                          (state, ended.isoformat(), f"run{n}"))
    else:
        store._db.execute("UPDATE runs SET state=? WHERE run_id=?", (state, f"run{n}"))
    return paths


def test_retention_boundaries(env):
    store, artifacts = env
    old = _run_with_artifacts(store, artifacts, 1, NOW - timedelta(days=30))  # exactly 30 d: expires
    young = _run_with_artifacts(store, artifacts, 2, NOW - timedelta(days=29, hours=23))
    active = _run_with_artifacts(store, artifacts, 3, None, state="running")
    ancient_active = _run_with_artifacts(store, artifacts, 4, None, state="waiting_live")
    store._db.execute("UPDATE runs SET created_at=? WHERE run_id='run4'",
                      ((NOW - timedelta(days=400)).isoformat(),))
    result = sweep(store, NOW, 30, artifacts)
    assert result.runs_deleted == 1 and result.paths_deleted == 2
    assert store.get_run("run1") is None
    assert all(not p.exists() for p in old)
    assert store.get_file("file1") is None and store.screenshot_path("run1", "shot1") is None
    assert store.events_after("run1", 0) == []
    assert store.records_for("finance", NOW - timedelta(days=365))[0].run_id != "run1"
    for keep in (young, active, ancient_active):
        assert all(p.exists() for p in keep)
    assert {store.get_run(r).state for r in ("run2", "run3", "run4")} == {"completed", "running",
                                                                          "waiting_live"}
    # Sites and profiles are never retention's business.
    assert store.get_site("site-a") is not None


def test_symlink_cleanup_cannot_escape_data(env, tmp_path):
    store, artifacts = env
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep me")
    _run_with_artifacts(store, artifacts, 1, NOW - timedelta(days=31))
    # A symlink inside artifacts pointing outside, a traversal path and an absolute outside path.
    link = artifacts / "link.bin"
    link.symlink_to(sentinel)
    dir_link = artifacts / "dirlink"
    dir_link.symlink_to(outside, target_is_directory=True)
    store.put_file(FileRecord("fileL", "run1", "finance", "x", "x", 1, "0" * 64, str(link)))
    store.put_file(FileRecord("fileT", "run1", "finance", "x", "x", 1, "0" * 64,
                              str(artifacts / ".." / "outside" / "sentinel.txt")))
    store.put_file(FileRecord("fileO", "run1", "finance", "x", "x", 1, "0" * 64, str(sentinel)))
    store.put_screenshot("shotD", "run1", str(dir_link / "sentinel.txt"))
    result = sweep(store, NOW, 30, artifacts)
    assert sentinel.read_text() == "keep me"
    assert not link.exists() and not link.is_symlink()  # the link itself is removed
    assert result.paths_refused == 3
    assert store.pending_deletions() == []


def test_interrupted_sweep_retries(env, monkeypatch):
    store, artifacts = env
    paths = _run_with_artifacts(store, artifacts, 1, NOW - timedelta(days=40))
    import casa_browser.retention as retention

    real_unlink = os.unlink
    calls = {"n": 0}

    def flaky(path, *a, **k):
        calls["n"] += 1
        raise PermissionError("busy")

    monkeypatch.setattr(retention.os, "unlink", flaky)
    first = sweep(store, NOW, 30, artifacts)
    assert first.runs_deleted == 1 and first.paths_retry == 2
    assert all(p.exists() for p in paths) and len(store.pending_deletions()) == 2
    monkeypatch.setattr(retention.os, "unlink", real_unlink)
    second = sweep(store, NOW, 30, artifacts)
    assert second.paths_deleted == 2 and store.pending_deletions() == []
    assert all(not p.exists() for p in paths)


def test_old_notices_expire(env):
    store, artifacts = env
    store.add_notice(SafeNotice(0, "credential_saved", None, "site-a", "x", NOW - timedelta(days=31)))
    store.add_notice(SafeNotice(0, "credential_saved", None, "site-a", "y", NOW - timedelta(days=1)))
    sweep(store, NOW, 30, artifacts)
    assert [n.text_masked for n in store.notices_after(0)] == ["y"]


def test_sweep_rejects_bad_days(env):
    store, artifacts = env
    with pytest.raises(ValueError):
        sweep(store, NOW, 0, artifacts)


def test_symlinked_artifacts_root_refused(env, tmp_path):
    """A symlinked root would make every inside-check resolve elsewhere (review r3-c1-r1)."""
    store, artifacts = env
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_text("keep")
    link_root = tmp_path / "artifacts-link"
    link_root.symlink_to(outside, target_is_directory=True)
    store.create_run(_spec(9), "run9", now=NOW - timedelta(days=40))
    store.put_file(FileRecord("f9", "run9", "finance", "x", "x", 1, "0" * 64, str(link_root / "victim")))
    store._db.execute("UPDATE runs SET state='completed', ended_at=? WHERE run_id='run9'",
                      ((NOW - timedelta(days=40)).isoformat(),))
    with pytest.raises(ValueError):
        sweep(store, NOW, 30, link_root)
    assert victim.read_text() == "keep"
    assert store.get_run("run9") is not None


def test_old_cancelled_request_ids_expire_with_the_runs(env):
    """The Stop's record of a request id is swept with the runs' retention: an id recorded
    before the cutoff goes, a recent one stays."""
    store, artifacts = env
    store.cancel_request_id("req-old", now=NOW - timedelta(days=31))
    store.cancel_request_id("req-new", now=NOW - timedelta(days=1))
    sweep(store, NOW, 30, artifacts)
    assert not store.request_cancelled("req-old") and store.request_cancelled("req-new")
