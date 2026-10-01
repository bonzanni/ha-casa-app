# casa/rootfs/opt/casa/mental_models.py
"""#1126: the mental models Casa declares, and when they are reconciled.

The memory server keeps a *mental model* as a standing question it re-answers
from the bank on its own schedule; the overlay (``SemanticMemory.profile``)
renders the answers into a fresh private session. Casa declares two models,
under ids it owns through the reserved ``casa-`` prefix, and reconciles the
backend against them — create the missing, re-define the drifted, delete
undeclared ``casa-`` ids — at boot and after a memory wipe. The HTTP work is
the backend's (``reconcile_mental_models`` on the seam); this module owns the
declarations, the one lock that serialises passes, and the passes' lifetime:
each runs as a strongly held background task, never awaited by boot or by a
wipe door, and shutdown cancels and awaits every one before the memory client
closes.

A pass is best-effort. Every failure is logged and dropped: the next boot or
the next wipe runs another.
"""
from __future__ import annotations

import asyncio
import logging

from semantic_memory import MentalModelSpec

logger = logging.getLogger(__name__)

# Every mental-model id under this prefix belongs to Casa: one Casa does not
# declare is deleted at the next reconcile. Ids without it are never touched.
RESERVED_PREFIX = "casa-"

DECLARED: tuple[MentalModelSpec, ...] = (
    MentalModelSpec(
        id="casa-open-commitments",
        name="Open commitments",
        source_query=(
            "What open commitments and follow-ups does the operator have? List "
            "each one with the dates and deadlines that were stated for it. "
            "Leave out the reminders and briefings the assistant itself sent."
        ),
        max_tokens=1024,
        # UTC: the refresh finishes before 08:00 local in both CET and CEST.
        trigger={"refresh_cron": "0 5 * * *"},
    ),
    MentalModelSpec(
        id="casa-operator-profile",
        name="Operator profile",
        source_query=(
            "Write a profile of the operator and their household, in these "
            "sections: who the operator is (role, work); the people around them "
            "and how they relate; how the operator likes to be addressed and "
            "communicated with; standing preferences, routines and recurring "
            "obligations, with their cadence. Give each item the date it was "
            "last stated. Leave out health, money and other people's private "
            "matters."
        ),
        max_tokens=1024,
        trigger={
            "refresh_after_consolidation": True,
            "min_refresh_interval_seconds": 86400,
        },
    ),
)

_tasks: set[asyncio.Task] = set()
_frozen = False
# One lock per event loop: an asyncio.Lock binds to the first loop that
# contends on it, so a module-global one would break the next loop to use it.
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None


def _reconcile_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock, _lock_loop = asyncio.Lock(), loop
    return _lock


async def reconcile(semantic_memory, bank: str, *, reason: str) -> None:
    """One pass, serialised against every other pass (FIFO: a pass queued
    behind a running one runs after it). Never raises except on cancellation —
    a backend without the method, or any failure inside it, is logged."""
    async with _reconcile_lock():
        try:
            await semantic_memory.reconcile_mental_models(bank, DECLARED)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — best-effort by design
            logger.warning(
                "mental_model_reconcile bank=%s reason=%s outcome=aborted error=%s",
                bank, reason, type(exc).__name__,
            )
            return
    logger.info("mental_model_reconcile bank=%s reason=%s outcome=done", bank, reason)


def schedule_reconcile(semantic_memory, bank: str, *, reason: str) -> asyncio.Task | None:
    """Start a pass in the background and return its task, or None when
    shutdown has begun or the task could not be created. Never raises, so a
    caller that has already finished its own work (a wipe holding its report)
    cannot be failed by it."""
    if _frozen:
        logger.info("mental_model_reconcile reason=%s outcome=refused_shutdown", reason)
        return None
    coro = reconcile(semantic_memory, bank, reason=reason)
    try:
        task = asyncio.get_running_loop().create_task(
            coro, name=f"mental-model-reconcile-{reason}",
        )
    except Exception as exc:  # noqa: BLE001
        coro.close()
        logger.warning(
            "mental_model_reconcile reason=%s outcome=not_scheduled error=%s",
            reason, type(exc).__name__,
        )
        return None
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def drain_reconcile_tasks() -> None:
    """Shutdown: refuse new passes, cancel the running ones and wait for them
    to finish, so none is still using the memory client when it closes. A
    cancelled pass is simply redone at the next boot."""
    global _frozen
    _frozen = True
    pending = [t for t in _tasks if not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
