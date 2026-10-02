"""#1162: regression pins for the in_casa engagement transcript reaper.

Green at the base by construction (the reaper did not exist), so these are
mutation-checked regression tests, not red cases; the red case lives in
``test_engagement_transcript_reaper.py`` and its helpers are shared from there.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from engagement_registry import EngagementRegistry
from test_engagement_transcript_reaper import (  # noqa: F401 — home is a fixture
    _project_dir,
    _projects,
    _seed_session,
    _specialists,
    _terminal,
    home,
)


def _registry(tmp_path: Path) -> EngagementRegistry:
    return EngagementRegistry(
        tombstone_path=str(tmp_path / "engagements.json"), bus=None)


async def _record(registry, *, kind="specialist", role="researcher",
                  origin=None, sid=None, outcome="completed"):
    rec = await registry.create(
        kind=kind, role_or_type=role, driver="in_casa", task="t",
        origin=dict(origin or {}), topic_id=None)
    if sid is not None:
        await registry.persist_session_id(rec.id, sid)
    if outcome in ("completed", "cancelled", "error"):
        await _terminal(registry, rec.id, outcome)
    elif outcome == "idle":
        await registry.mark_idle(rec.id)
    return rec


def _sid() -> str:
    return str(uuid.uuid4())


async def _reap(registry) -> dict:
    from engagement_transcript_reaper import reap_engagement_transcripts
    return await reap_engagement_transcripts(registry)


async def test_named_sessions_go_in_the_record_dir_zero_byte_included(
        home, tmp_path, monkeypatch):
    """Specialist and executor: every sid the record names — its current
    session and its job's list — is removed, ``.jsonl`` and ``<sid>/``,
    including a named zero-byte file the SDK's ``delete_session`` refuses."""
    from engagement_registry import JOB_SIDS_KEY

    _specialists(monkeypatch, researcher=SimpleNamespace(role="researcher", cwd=""))
    registry = _registry(tmp_path)
    a, b, c, empty, ex = (_sid() for _ in range(5))
    await _record(registry, origin={"job": {JOB_SIDS_KEY: [a, b, c, empty]}}, sid=c)
    await _record(registry, kind="executor", role="configurator", sid=ex)
    spec = _project_dir(home, "/config/agent-home/researcher")
    conf = _project_dir(home, "/config")
    for sid in (a, b, c):
        _seed_session(spec, sid)
    _seed_session(spec, empty, body="", sibling=False)
    _seed_session(conf, ex)

    await _reap(registry)

    assert [p.name for p in spec.iterdir()] == []
    assert [p.name for p in conf.iterdir()] == []


async def test_nothing_a_terminal_record_does_not_name_is_touched(
        home, tmp_path, monkeypatch):
    """The shared dirs keep every session no terminal record names, however
    old — a synchronous delegation's, a pre-downgrade one — and a plugin-job
    dir with no record stays too (outside the pass's scope, not retained on
    purpose)."""
    _specialists(monkeypatch, researcher=SimpleNamespace(role="researcher", cwd=""))
    registry = _registry(tmp_path)
    named = _sid()
    await _record(registry, sid=named)
    spec = _project_dir(home, "/config/agent-home/researcher")
    conf = _project_dir(home, "/config")
    orphan_plugin = _project_dir(
        home, f"/data/engagements/{uuid.uuid4().hex}/plugin-job")
    _seed_session(spec, named)
    unnamed = [_sid() for _ in range(3)]
    _seed_session(spec, unnamed[0])
    _seed_session(conf, unnamed[1])
    _seed_session(orphan_plugin, unnamed[2])
    for d in (spec, conf, orphan_plugin):
        for f in d.rglob("*"):
            os.utime(f, (0, 0))

    await _reap(registry)

    assert sorted(p.name for p in spec.iterdir()) == sorted(
        [f"{unnamed[0]}.jsonl", unnamed[0]])
    assert sorted(p.name for p in conf.iterdir()) == sorted(
        [f"{unnamed[1]}.jsonl", unnamed[1]])
    assert sorted(p.name for p in orphan_plugin.iterdir()) == sorted(
        [f"{unnamed[2]}.jsonl", unnamed[2]])


@pytest.mark.parametrize("status", ["active", "idle"])
async def test_a_live_engagement_is_never_touched(
        home, tmp_path, monkeypatch, status):
    """An active or idle engagement resumes its session on the next turn:
    neither its plugin dir nor its named sessions may be removed."""
    _specialists(monkeypatch, researcher=SimpleNamespace(role="researcher", cwd=""))
    registry = _registry(tmp_path)
    ps, ss = _sid(), _sid()
    plugin = await _record(registry, kind="plugin", sid=ps, outcome=status)
    await _record(registry, origin={"job": {"sids": [ss]}}, sid=ss,
                  outcome=status)
    pdir = _project_dir(home, f"/data/engagements/{plugin.id}/plugin-job")
    sdir = _project_dir(home, "/config/agent-home/researcher")
    _seed_session(pdir, ps)
    _seed_session(sdir, ss)
    before = sorted(str(p) for p in _projects(home).rglob("*"))

    await _reap(registry)

    assert sorted(str(p) for p in _projects(home).rglob("*")) == before
    assert len(before) == 10


@pytest.mark.parametrize("origin", [{}, {"job": {}}, {"job": {"sids": []}},
                                    {"job": None}])
async def test_a_record_without_a_job_list_is_reaped_by_its_session(
        home, tmp_path, monkeypatch, origin):
    """Independent of the job-list writer: no ``origin["job"]``, no key, an
    empty list — the record's own session still goes."""
    _specialists(monkeypatch, researcher=SimpleNamespace(role="researcher", cwd=""))
    registry = _registry(tmp_path)
    sid = _sid()
    await _record(registry, origin=origin, sid=sid)
    spec = _project_dir(home, "/config/agent-home/researcher")
    _seed_session(spec, sid)

    await _reap(registry)

    assert list(spec.iterdir()) == []


async def test_a_session_named_after_a_pass_goes_on_the_next(
        home, tmp_path, monkeypatch):
    """No "reaped" marker: a sid listed on a terminal record after a pass
    has handled it is removed by the next pass."""
    from engagement_registry import JOB_SIDS_KEY

    _specialists(monkeypatch, researcher=SimpleNamespace(role="researcher", cwd=""))
    registry = _registry(tmp_path)
    first, late = _sid(), _sid()
    rec = await _record(registry, origin={"job": {JOB_SIDS_KEY: [first]}}, sid=first)
    spec = _project_dir(home, "/config/agent-home/researcher")
    _seed_session(spec, first)
    await _reap(registry)
    assert list(spec.iterdir()) == []

    rec.origin["job"][JOB_SIDS_KEY].append(late)
    _seed_session(spec, late)
    await _reap(registry)

    assert list(spec.iterdir()) == []


async def test_one_failing_record_does_not_stop_the_others(
        home, tmp_path, monkeypatch):
    """A removal error on one record is counted; the other records' sessions
    are still removed and the pass returns."""
    import engagement_transcript_reaper as reaper

    _specialists(monkeypatch, researcher=SimpleNamespace(role="researcher", cwd=""))
    registry = _registry(tmp_path)
    bad_sid, good_sid = _sid(), _sid()
    bad = await _record(registry, kind="plugin", sid=bad_sid)
    await _record(registry, sid=good_sid)
    bad_dir = _project_dir(home, f"/data/engagements/{bad.id}/plugin-job")
    spec = _project_dir(home, "/config/agent-home/researcher")
    _seed_session(bad_dir, bad_sid)
    _seed_session(spec, good_sid)
    real_remove = reaper._remove

    def _remove(path, counts):
        if bad.id in path:
            raise PermissionError(path)
        real_remove(path, counts)

    monkeypatch.setattr(reaper, "_remove", _remove)
    counts = await _reap(registry)

    assert list(spec.iterdir()) == []
    assert bad_dir.is_dir()
    assert counts["errors"] == 1


async def test_an_absent_session_never_blocks_a_present_sibling(
        home, tmp_path, monkeypatch):
    """On one record, a sid already gone is counted absent and the present
    one beside it is still removed, whatever the iteration order."""
    from engagement_registry import JOB_SIDS_KEY

    _specialists(monkeypatch, researcher=SimpleNamespace(role="researcher", cwd=""))
    registry = _registry(tmp_path)
    sids = sorted(_sid() for _ in range(4))
    present = sids[1::2]            # sids[0::2] are named but already gone
    await _record(registry, origin={"job": {JOB_SIDS_KEY: sids}})
    spec = _project_dir(home, "/config/agent-home/researcher")
    for sid in present:
        _seed_session(spec, sid)

    counts = await _reap(registry)

    assert list(spec.iterdir()) == []
    assert counts["errors"] == 0


async def test_a_terminal_status_not_yet_on_disk_is_not_acted_on(
        home, tmp_path, monkeypatch):
    """A terminal write that failed leaves the disk saying active; a restart
    would reload the record as resumable, so its session stays until a later
    write makes the terminal status durable — then the next pass reaps it."""
    _specialists(monkeypatch, researcher=SimpleNamespace(role="researcher", cwd=""))
    registry = _registry(tmp_path)
    sid = _sid()
    rec = await _record(registry, sid=sid, outcome="active")
    spec = _project_dir(home, "/config/agent-home/researcher")
    _seed_session(spec, sid)

    real_write = registry._write_tombstone
    failing = [True]

    def _write(snapshot):
        if failing[0]:
            raise OSError("disk full")
        real_write(snapshot)

    monkeypatch.setattr(registry, "_write_tombstone", _write)
    await registry.mark_cancelled(rec.id)
    failing[0] = False
    assert rec.status == "cancelled"
    assert {r["id"]: r["status"] for r in json.loads(
        (tmp_path / "engagements.json").read_text())}[rec.id] == "active"

    counts = await _reap(registry)
    assert (spec / f"{sid}.jsonl").exists() and (spec / sid).is_dir()
    assert counts["unsettled"] == 1

    await _record(registry, role="other", outcome="active")  # any later write
    await _reap(registry)
    assert list(spec.iterdir()) == []


@pytest.mark.parametrize("cwd", [
    "/data/engagements/0123456789abcdef0123456789abcdef/plugin-job",
    "/config/agent-home/researcher",
    "/config",
])
def test_the_reaper_finds_the_dir_the_sdk_names(home, cwd):
    """The reaper acts through the SDK's private lookup; tests seed through
    its public mapping. The two must agree (and the private names exist) on
    the pinned SDK, or every test above would seed a dir the reaper never
    looks in."""
    from claude_agent_sdk._internal.sessions import (
        _canonicalize_path,
        _find_project_dir,
    )

    seeded = _project_dir(home, cwd)
    seeded.mkdir(parents=True)
    assert _find_project_dir(_canonicalize_path(cwd)) == seeded


def test_a_committed_write_is_not_reported_failed_by_the_directory_close(
        tmp_path, monkeypatch):
    """After ``os.replace`` committed, a failing directory close is logged,
    not raised: a strict caller would otherwise roll its memory back while
    the disk holds the new content."""
    import atomic_io

    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")
    real_close = os.close
    failed = []

    def _close(fd):
        is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
        real_close(fd)
        if is_dir and not failed:
            failed.append(fd)
            raise OSError(errno.EIO, "close")

    monkeypatch.setattr(os, "close", _close)
    try:
        atomic_io.atomic_write_text(target, "new")
    finally:
        monkeypatch.setattr(os, "close", real_close)

    assert failed and target.read_text(encoding="utf-8") == "new"
