"""Regression tests for v0.15.2: AsyncIOScheduler sweeper wiring.

casa_core.py registers its scheduled jobs with the AsyncIOScheduler —
`engagement_idle_sweep` (cron 08:00 daily) among them.
Until v0.15.2 these were registered as
``lambda: asyncio.create_task(coro(...))``. APScheduler's
``AsyncIOExecutor`` runs sync callables in a worker thread, so the
lambda fired in a thread with no running loop and raised
``RuntimeError: no running event loop`` on every fire — silent no-op
since v0.13.0.

The fix: pass the coroutine function directly (with ``kwargs={...}``);
``AsyncIOExecutor`` dispatches coroutines on the running loop natively.
This file pins the contract so the regression can't sneak back.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path


def test_engagement_idle_sweep_target_is_coroutine_function():
    from engagement_registry import EngagementRegistry

    assert asyncio.iscoroutinefunction(EngagementRegistry.sweep_idle_and_suspend), (
        "EngagementRegistry.sweep_idle_and_suspend must be a coroutine "
        "function so AsyncIOScheduler dispatches it on the running loop. "
        "If this fails after a refactor, do NOT re-wrap with a sync "
        "lambda — find another fix."
    )


_CASA_CORE_SRC = (
    Path(__file__).resolve().parent.parent
    / "casa"
    / "rootfs"
    / "opt"
    / "casa"
    / "casa_core.py"
)


def test_casa_core_does_not_register_sync_lambda_sweepers():
    """Static guard: casa_core.py must not register sweeper jobs as
    ``lambda: asyncio.create_task(...)``. That pattern caused
    ``RuntimeError: no running event loop`` on every fire from v0.13.0
    through v0.15.1."""
    text = _CASA_CORE_SRC.read_text(encoding="utf-8")
    assert not re.search(r"lambda:\s*asyncio\.create_task\b", text), (
        "Found `lambda: asyncio.create_task(...)` in casa_core.py — this "
        "re-introduces the v0.13.0 sweeper bug. Pass the coroutine "
        "function directly to scheduler.add_job(...) with kwargs={...} "
        "instead."
    )


def test_engagement_transcript_reap_is_a_scheduled_coroutine_job():
    """#1162: the transcript reaper is registered with the scheduler as a
    coroutine function (same v0.15.2 hazard as the sweepers above), under
    its own id, first at scheduler start."""
    from engagement_transcript_reaper import reap_engagement_transcripts

    assert asyncio.iscoroutinefunction(reap_engagement_transcripts)
    text = _CASA_CORE_SRC.read_text(encoding="utf-8")
    job = re.search(
        r"scheduler\.add_job\(\s*reap_engagement_transcripts,"
        r"(?P<args>.*?)\n    \)",
        text,
        re.S,
    )
    assert job, ("reap_engagement_transcripts must be registered directly "
                 "with scheduler.add_job")
    assert 'id="engagement_transcript_reap"' in job.group("args")
    assert "args=(engagement_registry,)" in job.group("args")
    assert "next_run_time=" in job.group("args")

