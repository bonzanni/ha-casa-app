"""Transcript retention (INV-MEM-031, INV-ENG-027; §14 test 14).

Once every run in it is confirmed, a transcript is deleted
``TRANSCRIPT_RETENTION_DAYS`` (30) after its session can no longer resume —
never fewer: a resident session past its freshness window, an engagement's
from its durable terminal record. That holds for a transcript no run
registered in too. Every deleter asks the run-confirmation gate at the
delete. Real transcript files, real SQLite, the real deleters.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

from memory import transcript_runs
from session_registry import SessionRegistry
from session_registry import freshness_window
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


async def _terminal_engagement(tmp_path, monkeypatch, *, cwd, days_ago):
    from engagement_registry import EngagementRegistry
    from test_engagement_transcript_reaper import _specialists

    _specialists(monkeypatch, researcher=SimpleNamespace(role="researcher",
                                                          cwd=cwd))
    path = str(tmp_path / "e.json")
    reg = EngagementRegistry(tombstone_path=path, bus=None)
    rec = await reg.create(kind="specialist", role_or_type="researcher",
                           driver="in_casa", task="t", origin={}, topic_id=None)
    sid = str(uuid.uuid4())
    await reg.persist_session_id(rec.id, sid)
    done = time.time() - days_ago * 86400
    await reg.mark_completed(rec.id, done)
    return reg, rec, sid, done, path


async def _reloaded(path):
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=path, bus=None)
    await reg.load()
    return reg


async def _rewrite(reg) -> None:
    """Any write re-runs the tombstone's expiry over every record (startup
    reconciliation, a new engagement)."""
    await reg.create(kind="specialist", role_or_type="researcher",
                     driver="in_casa", task="later", origin={}, topic_id=None)


def _on_disk(path, rec_id) -> bool:
    import json
    return any(r["id"] == rec_id for r in json.loads(Path(path).read_text()))


async def test_a_tombstone_waits_for_its_transcripts_across_downtime(
        env, tmp_path, monkeypatch):
    """Swept at day 29 (inside retention), Casa is down until day 33; at
    restart a write runs the tombstone expiry before the reaper's first
    pass. The record survives it, the reaper then deletes the transcript and
    marks the record reaped, and only then does the tombstone age out. Red
    case: a tombstone aged out at a fixed margin past retention, which
    strands the transcript for good."""
    from engagement_transcript_reaper import reap_engagement_transcripts

    cwd = str(tmp_path / "agent-home" / "researcher")
    reg, rec, sid, done, path = await _terminal_engagement(
        tmp_path, monkeypatch, cwd=cwd, days_ago=33)
    transcript = write_transcript(cwd, sid)
    await reap_engagement_transcripts(reg, now=done + 29 * 86400)
    assert transcript.exists()

    restarted = await _reloaded(path)            # day 33, after downtime
    await _rewrite(restarted)
    assert _on_disk(path, rec.id)
    await reap_engagement_transcripts(restarted)
    assert not transcript.exists()
    assert restarted.get(rec.id).transcripts_reaped is True
    await _rewrite(restarted)
    assert not _on_disk(path, rec.id)


async def test_a_held_transcript_keeps_its_tombstone_until_it_goes(
        env, tmp_path, monkeypatch):
    from engagement_transcript_reaper import reap_engagement_transcripts

    cwd = str(tmp_path / "agent-home" / "researcher")
    reg, rec, sid, _done, path = await _terminal_engagement(
        tmp_path, monkeypatch, cwd=cwd, days_ago=40)
    transcript = write_transcript(cwd, sid)
    run = transcript_runs.register(env, kind="engagement", role="researcher",
                                   directory=cwd)
    await reap_engagement_transcripts(reg)
    await _rewrite(reg)
    assert transcript.exists() and _on_disk(path, rec.id)
    transcript_runs.confirm(env, run)
    await reap_engagement_transcripts(reg)
    await _rewrite(reg)
    assert not transcript.exists() and not _on_disk(path, rec.id)


@pytest.mark.parametrize("case", ["folder_gone", "uninstalled"])
async def test_a_record_with_nothing_to_reap_still_ages_out(
        env, tmp_path, monkeypatch, case):
    """A record whose folder is already gone, or whose specialist is
    uninstalled with no launch cwd recorded, has nothing the reaper could
    ever delete: it is marked reaped and its tombstone ages out."""
    from engagement_transcript_reaper import reap_engagement_transcripts
    from test_engagement_transcript_reaper import _specialists

    cwd = str(tmp_path / "agent-home" / "researcher")
    reg, rec, _sid, _done, path = await _terminal_engagement(
        tmp_path, monkeypatch, cwd=cwd, days_ago=40)
    if case == "uninstalled":
        _specialists(monkeypatch)
    await reap_engagement_transcripts(reg)
    assert reg.get(rec.id).transcripts_reaped is True
    await _rewrite(reg)
    assert not _on_disk(path, rec.id)


@pytest.mark.parametrize("raw,days", [("", 30), ("x", 30), ("0", 30),
                                      ("-4", 30), ("7", 7), ("90", 90)])
async def test_the_retention_variable_is_read_at_each_use(monkeypatch, raw, days):
    monkeypatch.setenv(transcript_runs.RETENTION_ENV, raw)
    assert transcript_runs.retention_days() == days
    assert transcript_runs.retention() == timedelta(days=days)


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


async def test_registration_never_waits_on_a_saturated_default_executor(
        env, monkeypatch):
    """With the loop's default executor's only thread busy, a run still
    registers at once. A registration stalled past the stall guard counts as
    failed for the turn, which goes on; the row that commits later is
    adopted by the run, so its end and close still reach it."""
    import concurrent.futures
    import threading
    import time as _time

    loop = asyncio.get_running_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(1))
    gate = threading.Event()
    busy = loop.run_in_executor(None, gate.wait, 30)
    try:
        started = loop.time()
        run = await transcript_runs.Run.start(
            transcript_runs.DELEGATION, role="finance", directory="/d/home",
            session_id=str(uuid.uuid4()))
        assert loop.time() - started < 0.2
        assert run.run_id is not None
    finally:
        gate.set()
        await busy

    real = transcript_runs.register
    delay = {"s": 0.2}

    def slow(*a, **k):
        _time.sleep(delay["s"])
        return real(*a, **k)
    monkeypatch.setattr(transcript_runs, "register", slow)
    # a slow disk is not a stall: a 200 ms write still registers the run
    disk = await transcript_runs.Run.start(
        transcript_runs.DELEGATION, role="finance", directory="/d/home",
        session_id=str(uuid.uuid4()))
    assert disk.run_id is not None
    delay["s"] = transcript_runs.STALL_GUARD_S + 0.3
    started = loop.time()
    late = await transcript_runs.Run.start(
        transcript_runs.DELEGATION, role="finance", directory="/d/home",
        session_id=str(uuid.uuid4()))
    assert loop.time() - started < transcript_runs.STALL_GUARD_S + 0.2
    assert late.run_id is None
    late.end()
    late.close()
    await transcript_runs.settle()
    (row,) = [r for r in rows(env) if r["session_id"] == late.session_id]
    assert row["confirmed_at"] is not None and row["ended_at"] is not None
