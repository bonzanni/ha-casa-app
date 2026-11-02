"""Transcript retention (INV-MEM-031, INV-ENG-027; §14 test 14).

Once every run in it is confirmed, a transcript is deleted
``TRANSCRIPT_RETENTION_DAYS`` (30) after its session can no longer resume —
never fewer: a resident session past its freshness window, an engagement's
from its durable terminal record (the claude_code workspace keeps its own
seven-day ``retention_until``). That holds for a transcript no run
registered in too. Every deleter asks the run-confirmation gate at the
delete. Real transcript files, real SQLite, the real deleters.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

import tools
from memory import transcript_runs
from session_registry import SessionRegistry
from session_saver import freshness_window
from session_sweeper import SessionSweeper
from test_memory_capture import mem  # noqa: F401 — a fixture
from transcript_runs_support import rows, write_transcript

pytestmark = [pytest.mark.asyncio]

HOME_DIR = "/config/agent-home/assistant"


@pytest.fixture
def env(tmp_path, monkeypatch, mem):  # noqa: F811 — the imported fixture
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.delenv(transcript_runs.RETENTION_ENV, raising=False)
    monkeypatch.delenv("FRESHNESS_TELEGRAM_HOURS", raising=False)
    return mem


async def _sweep(tmp_path, sid: str, channel: str, stale: timedelta,
                 now: datetime) -> None:
    reg = SessionRegistry(str(tmp_path / f"s-{uuid.uuid4().hex}.json"))
    async with reg._lock:
        reg._data[f"{channel}-{sid[:8]}"] = {
            "agent": "assistant", "sdk_session_id": sid,
            "last_active": (now - stale).isoformat()}
        await reg._save_locked()
    await SessionSweeper(registry=reg, now=lambda: now,
                         directory_for=lambda role: HOME_DIR)._sweep_once()


@pytest.mark.parametrize("days", [None, "45"])
async def test_a_resident_transcript_lives_its_retention_after_it_can_no_longer_resume(
        env, tmp_path, monkeypatch, days):
    if days is not None:
        monkeypatch.setenv(transcript_runs.RETENTION_ENV, days)
    # never before the pointer's own TTL (30 days) either
    keep = max(timedelta(days=30),
               freshness_window("telegram") + transcript_runs.retention())
    now = datetime.now(timezone.utc)
    sid = str(uuid.uuid4())
    path = write_transcript(HOME_DIR, sid)
    run = transcript_runs.register(env, kind="resident", role="assistant",
                                   directory=HOME_DIR, session_id=sid)
    transcript_runs.confirm(env, run)

    await _sweep(tmp_path, sid, "telegram", keep - timedelta(hours=1), now)
    assert path.exists() and len(rows(env)) == 1
    await _sweep(tmp_path, sid, "telegram", keep + timedelta(hours=1), now)
    assert not path.exists() and rows(env) == []


async def test_an_unconfirmed_resident_run_keeps_its_transcript(env, tmp_path):
    sid = str(uuid.uuid4())
    path = write_transcript(HOME_DIR, sid)
    transcript_runs.register(env, kind="resident", role="assistant",
                             directory=HOME_DIR, session_id=sid)
    await _sweep(tmp_path, sid, "telegram", timedelta(days=400),
                 datetime.now(timezone.utc))
    assert path.exists() and len(rows(env)) == 1


async def test_a_transcript_with_no_registered_run_follows_retention(env, tmp_path):
    """A voice session registers no run; its transcript is kept for the
    retention after its freshness window and then deleted."""
    keep = max(timedelta(days=30),
               freshness_window("voice") + transcript_runs.retention())
    now = datetime.now(timezone.utc)
    sid = str(uuid.uuid4())
    path = write_transcript(HOME_DIR, sid)
    await _sweep(tmp_path, sid, "voice", keep - timedelta(hours=1), now)
    assert path.exists()
    await _sweep(tmp_path, sid, "voice", keep + timedelta(hours=1), now)
    assert not path.exists()


async def test_an_engagement_transcript_lives_its_retention_after_its_terminal_record(
        env, tmp_path, monkeypatch):
    """Counted from the durable terminal record's ``completed_at``; an
    unconfirmed run naming the session keeps it past that; a confirmed one
    does not."""
    from engagement_registry import EngagementRegistry
    from engagement_transcript_reaper import reap_engagement_transcripts
    from test_engagement_transcript_reaper import _specialists
    from types import SimpleNamespace

    cwd = str(tmp_path / "agent-home" / "researcher")
    _specialists(monkeypatch, researcher=SimpleNamespace(role="researcher", cwd=cwd))
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(kind="specialist", role_or_type="researcher",
                           driver="in_casa", task="t", origin={}, topic_id=None)
    sid = str(uuid.uuid4())
    await reg.persist_session_id(rec.id, sid)
    done = time.time()
    await reg.mark_completed(rec.id, done)
    path = write_transcript(cwd, sid)
    days = transcript_runs.retention_days()

    await reap_engagement_transcripts(reg, now=done + days * 86400 - 3600)
    assert path.exists()
    run = transcript_runs.register(env, kind="engagement", role="researcher",
                                   directory=cwd, session_id=sid)
    await reap_engagement_transcripts(reg, now=done + days * 86400 + 3600)
    assert path.exists()
    transcript_runs.confirm(env, run)
    await reap_engagement_transcripts(reg, now=done + days * 86400 + 3600)
    assert not path.exists() and rows(env) == []


async def test_a_claude_code_workspace_keeps_its_seven_day_retention_once_gated(
        env, tmp_path, monkeypatch):
    from drivers import workspace
    from test_workspace_sweeper import _write_meta

    root = tmp_path / "engagements"
    ws = root / uuid.uuid4().hex
    (ws / ".home" / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setattr(tools, "_ENGAGEMENTS_ROOT", str(root))
    run = transcript_runs.register(env, kind="summary", role="configurator",
                                   directory=str(ws))
    _write_meta(ws, status="COMPLETED", retention_iso="2999-01-01T00:00:00Z")
    await workspace._sweep_workspaces(engagements_root=str(root))
    assert ws.is_dir()
    meta = Path(workspace.casa_meta_path(ws.name))
    meta.write_text(meta.read_text().replace("2999-01-01", "2020-01-01"))
    await workspace._sweep_workspaces(engagements_root=str(root))
    assert ws.is_dir()                                  # gated: kept whole
    transcript_runs.confirm(env, run)
    await workspace._sweep_workspaces(engagements_root=str(root))
    assert not ws.exists() and rows(env) == []


async def test_a_tombstone_outlives_its_transcripts_retention(monkeypatch):
    """The reaper acts only on a terminal status on disk, so a record's
    tombstone stays past its transcripts' retention."""
    import engagement_registry as er
    monkeypatch.delenv(transcript_runs.RETENTION_ENV, raising=False)
    assert er._terminal_retention_days() >= (
        transcript_runs.retention_days() + er._TRANSCRIPT_REAP_MARGIN_DAYS)
    monkeypatch.setenv(transcript_runs.RETENTION_ENV, "45")
    assert er._terminal_retention_days() == 45 + er._TRANSCRIPT_REAP_MARGIN_DAYS


@pytest.mark.parametrize("raw,days", [("", 30), ("x", 30), ("0", 30),
                                      ("-4", 30), ("7", 7), ("90", 90)])
async def test_the_retention_variable_is_read_at_each_use(monkeypatch, raw, days):
    monkeypatch.setenv(transcript_runs.RETENTION_ENV, raw)
    assert transcript_runs.retention_days() == days
    assert transcript_runs.retention() == timedelta(days=days)


async def test_the_workspace_sweep_never_holds_the_event_loop(env, tmp_path):
    """The sweep's gate, its deletion and its row removal run off the loop:
    with the store's write lock held for 1.5 s by another connection, a
    resident turn running beside the sweep finishes inside a second, and
    the workspace still goes once the lock is released."""
    import sqlite3
    import threading
    from unittest.mock import patch

    from drivers import workspace
    from test_agent_process import FakeClient, _make_agent
    from test_memory_capture import _operator_msg
    from test_workspace_sweeper import _write_meta

    root = tmp_path / "engagements"
    ws = root / uuid.uuid4().hex
    ws.mkdir(parents=True)
    _write_meta(ws, status="COMPLETED", retention_iso="2020-01-01T00:00:00Z")
    holder = sqlite3.connect(env.path, isolation_level=None,
                             check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    release = threading.Timer(1.5, lambda: holder.execute("ROLLBACK"))
    release.start()
    try:
        agent = _make_agent(tmp_path)
        FakeClient.reset()
        loop = asyncio.get_running_loop()
        started = loop.time()
        with patch("sdk_client_pool._default_make_client", FakeClient):
            sweep = asyncio.create_task(
                workspace._sweep_workspaces(engagements_root=str(root)))
            await agent._process(_operator_msg("ping", "777"))
        took = loop.time() - started
        await asyncio.wait_for(sweep, 10)
    finally:
        release.join()
        holder.close()
    assert took < 1.0, took
    assert not ws.exists()


async def test_detached_run_writes_never_take_a_turns_thread(env, tmp_path):
    """Eight delegated runs end and confirm while another connection holds
    the store's write lock for 1.5 s, on a loop whose default executor has
    eight threads: their sixteen detached writes queue on transcript_runs'
    own thread, and a resident turn's bounded registration and captures
    still finish inside a second. Once the lock goes, every run is
    confirmed and ended."""
    import concurrent.futures
    import sqlite3
    import threading
    from unittest.mock import patch

    from test_agent_process import FakeClient, _make_agent
    from test_memory_capture import _operator_msg

    loop = asyncio.get_running_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(8))
    runs = [await transcript_runs.Run.start(
        transcript_runs.DELEGATION, role="finance", directory="/d/finance",
        session_id=str(uuid.uuid4())) for _ in range(8)]
    holder = sqlite3.connect(env.path, isolation_level=None,
                             check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    release = threading.Timer(1.5, lambda: holder.execute("ROLLBACK"))
    release.start()
    try:
        for run in runs:
            run.end()
            run.close()
        await asyncio.sleep(0.05)
        agent = _make_agent(tmp_path)
        FakeClient.reset()
        started = loop.time()
        with patch("sdk_client_pool._default_make_client", FakeClient):
            await agent._process(_operator_msg("ping", "778"))
        took = loop.time() - started
    finally:
        release.join()
        holder.close()
    await transcript_runs.settle()
    assert took < 1.0, took
    delegated = [r for r in rows(env) if r["kind"] == "delegation"]
    assert len(delegated) == 8
    assert all(r["confirmed_at"] and r["ended_at"] for r in delegated)
