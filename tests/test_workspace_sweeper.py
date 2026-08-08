"""Tests for drivers.workspace._sweep_workspaces."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


def _write_meta(ws: Path, *, status: str, retention_iso: str | None = None):
    """Task 4 (containment stage 2): .casa-meta.json lives in the control
    dir, keyed by the engagement id — which ``load_casa_meta`` derives from
    the workspace dir's basename, so this helper provisions the control dir
    to match."""
    from drivers.workspace import casa_meta_path, provision_control_dir

    meta = {
        "engagement_id": ws.name,
        "executor_type": "hello-driver",
        "status": status,
        "created_at": "2026-04-01T00:00:00Z",
        "finished_at": None,
        "retention_until": retention_iso,
    }
    provision_control_dir(ws.name)
    Path(casa_meta_path(ws.name)).write_text(json.dumps(meta), encoding="utf-8")


async def test_sweeper_deletes_terminal_expired(tmp_path):
    from drivers.workspace import _sweep_workspaces, control_dir

    ws1 = tmp_path / "eng-done-old"
    ws1.mkdir()
    _write_meta(ws1, status="COMPLETED", retention_iso="2020-01-01T00:00:00Z")
    assert Path(control_dir(ws1.name)).is_dir(), "control dir must exist pre-sweep"

    await _sweep_workspaces(engagements_root=str(tmp_path))

    assert not ws1.exists(), "expired completed workspace should be deleted"
    # Task 4 (containment stage 2): the control dir follows the workspace.
    assert not Path(control_dir(ws1.name)).exists(), (
        "control dir must be removed alongside the workspace"
    )


async def test_sweeper_keeps_active_and_in_grace(tmp_path):
    from drivers.workspace import _sweep_workspaces

    ws_active = tmp_path / "eng-active"
    ws_active.mkdir()
    _write_meta(ws_active, status="UNDERGOING")

    ws_grace = tmp_path / "eng-grace"
    ws_grace.mkdir()
    # Far-future retention.
    _write_meta(ws_grace, status="COMPLETED", retention_iso="2099-01-01T00:00:00Z")

    await _sweep_workspaces(engagements_root=str(tmp_path))

    assert ws_active.exists()
    assert ws_grace.exists()


async def test_sweeper_skips_missing_meta(tmp_path):
    from drivers.workspace import _sweep_workspaces

    ws_orphan = tmp_path / "eng-no-meta"
    ws_orphan.mkdir()
    # no .casa-meta.json

    await _sweep_workspaces(engagements_root=str(tmp_path))

    assert ws_orphan.exists(), "no meta should leave alone (user prunes explicitly)"


async def test_sweeper_skips_terminal_without_retention(tmp_path):
    """Terminal state but retention_until=None is a bug; sweeper logs and skips."""
    from drivers.workspace import _sweep_workspaces

    ws = tmp_path / "eng-bug"
    ws.mkdir()
    _write_meta(ws, status="CANCELLED", retention_iso=None)

    await _sweep_workspaces(engagements_root=str(tmp_path))

    assert ws.exists()


async def test_sweeper_tolerates_missing_root(tmp_path):
    """Root not existing should be a silent no-op."""
    from drivers.workspace import _sweep_workspaces
    await _sweep_workspaces(engagements_root=str(tmp_path / "does-not-exist"))


async def test_sweeper_removes_engagement_log_dir(tmp_path):
    """v0.64.0: the per-engagement s6-log dir (/var/log/casa-engagement-<id>)
    follows workspace retention — removed with the workspace, kept in grace,
    and a missing log dir is not an error."""
    from drivers.workspace import _sweep_workspaces

    log_root = tmp_path / "var-log"
    log_root.mkdir()
    engagements = tmp_path / "engagements"
    engagements.mkdir()

    ws_old = engagements / "eng-done-old"
    ws_old.mkdir()
    _write_meta(ws_old, status="COMPLETED", retention_iso="2020-01-01T00:00:00Z")
    (log_root / "casa-engagement-eng-done-old").mkdir()

    ws_grace = engagements / "eng-grace"
    ws_grace.mkdir()
    _write_meta(ws_grace, status="COMPLETED", retention_iso="2099-01-01T00:00:00Z")
    (log_root / "casa-engagement-eng-grace").mkdir()

    ws_nolog = engagements / "eng-no-logdir"
    ws_nolog.mkdir()
    _write_meta(ws_nolog, status="COMPLETED", retention_iso="2020-01-01T00:00:00Z")
    # no matching log dir — must not raise

    await _sweep_workspaces(
        engagements_root=str(engagements), log_root=str(log_root),
    )

    assert not ws_old.exists()
    assert not (log_root / "casa-engagement-eng-done-old").exists()
    assert ws_grace.exists()
    assert (log_root / "casa-engagement-eng-grace").exists()
    assert not ws_nolog.exists()


async def test_sweeper_warns_when_log_dir_removal_fails(
    tmp_path, monkeypatch, caplog,
):
    """The log-dir half of retention must not fail silently: once the
    workspace is gone the sweep can never map to this log dir again, so a
    warning is the operator's only signal."""
    import logging

    from drivers import workspace as ws_mod

    log_root = tmp_path / "var-log"
    log_root.mkdir()
    engagements = tmp_path / "engagements"
    engagements.mkdir()
    ws = engagements / "eng-x"
    ws.mkdir()
    _write_meta(ws, status="COMPLETED", retention_iso="2020-01-01T00:00:00Z")
    (log_root / "casa-engagement-eng-x").mkdir()

    real_rmtree = ws_mod.shutil.rmtree

    def flaky(path, *args, **kwargs):
        if "casa-engagement-eng-x" in str(path):
            raise OSError("EBUSY")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(ws_mod.shutil, "rmtree", flaky)

    with caplog.at_level(logging.WARNING):
        await ws_mod._sweep_workspaces(
            engagements_root=str(engagements), log_root=str(log_root),
        )

    assert not ws.exists()
    assert any(
        "casa-engagement-eng-x" in r.getMessage() for r in caplog.records
    ), "log-dir removal failure must be warned about"


async def test_sweeper_survives_non_object_meta(tmp_path):
    """#344: one .casa-meta.json holding valid-but-non-object JSON ([]/
    string/number) must not abort the whole sweep — later workspaces
    still get swept."""
    from drivers.workspace import _sweep_workspaces, casa_meta_path, provision_control_dir

    # Names sort before the expired one so the bad entries are hit first.
    ws_list = tmp_path / "a-eng-list"
    ws_list.mkdir()
    provision_control_dir(ws_list.name)
    Path(casa_meta_path(ws_list.name)).write_text("[]", encoding="utf-8")
    ws_str = tmp_path / "b-eng-str"
    ws_str.mkdir()
    provision_control_dir(ws_str.name)
    Path(casa_meta_path(ws_str.name)).write_text('"oops"', encoding="utf-8")

    ws_expired = tmp_path / "z-eng-done-old"
    ws_expired.mkdir()
    _write_meta(ws_expired, status="COMPLETED",
                retention_iso="2020-01-01T00:00:00Z")

    await _sweep_workspaces(engagements_root=str(tmp_path))

    assert ws_list.exists(), "non-object meta is skipped, not deleted"
    assert ws_str.exists()
    assert not ws_expired.exists(), "the sweep must reach later workspaces"


async def test_sweeper_survives_non_string_retention(tmp_path):
    """Sol r6-1: a numeric retention_until must not TypeError the sweep —
    warned + skipped like null, and later workspaces still processed."""
    from drivers.workspace import _sweep_workspaces, casa_meta_path, provision_control_dir

    ws_bad = tmp_path / "a-eng-badret"
    ws_bad.mkdir()
    provision_control_dir(ws_bad.name)
    Path(casa_meta_path(ws_bad.name)).write_text(
        json.dumps({"status": "COMPLETED", "retention_until": 0}),
        encoding="utf-8",
    )

    ws_expired = tmp_path / "z-eng-done-old"
    ws_expired.mkdir()
    _write_meta(ws_expired, status="COMPLETED",
                retention_iso="2020-01-01T00:00:00Z")

    await _sweep_workspaces(engagements_root=str(tmp_path))

    assert ws_bad.exists(), "bad retention_until is skipped, not deleted"
    assert not ws_expired.exists(), "the sweep must reach later workspaces"
