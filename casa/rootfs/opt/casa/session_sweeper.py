"""Periodic session-registry TTL sweep (spec 5.2 §6).

Stops `SessionRegistry.sessions.json` from growing unbounded. Every
`sweep_interval_hours` (default 6) a background task wakes up, iterates
the registry under its 5.1 lock, drops entries whose `last_active` is
older than the configured TTL — and never before the session's freshness
window plus the transcript retention (``TRANSCRIPT_RETENTION_DAYS``) has
passed, so a session is never removed while it can resume, and its
transcript is kept that long after it can no longer resume — persists the
reduced registry, and hard-deletes the on-disk transcript via the SDK's
``delete_session(sid, directory)`` (spec §3.4.1 — Casa owns transcript
reaping; the CLI's cleanupPeriodDays sweep never fires under Casa's SDK
invocation, §8.6).

The transcript is deleted only where the run-confirmation gate passes
(``memory/transcript_runs.py``), asked at the delete under the session's
write gate (so no run of that session starts between the check and the
delete) and outside the registry lock: while a run in its folder is
unconfirmed the transcript is kept and so is its pointer, for a later sweep.
The rows that named a deleted session go with it.

Pure policy module — no transport imports. Reaches into internal
`SessionRegistry` attributes (`_lock`, `_data`, `_save_locked`) by
design: 5.1 left those seams for internal consumers exactly so we
don't have to contort the public API for policy layers that sit above
the storage layer.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from memory import transcript_runs
from session_registry import SessionRegistry, freshness_window

logger = logging.getLogger(__name__)

# Sweep cadence — hard-coded per spec R5 (periodic sweep is cheap: one
# pass over < 100 entries, once every 6 h). Not on the env-var surface
# (§9.3 lists only SESSION_TTL_DAYS and WEBHOOK_SESSION_TTL_DAYS).
_SWEEP_INTERVAL_HOURS = 6

# Defaults, exported for casa_core fallback.
_DEFAULT_SESSION_TTL_DAYS = 30
_DEFAULT_WEBHOOK_SESSION_TTL_DAYS = 1


def _parse_last_active(value: object) -> datetime | None:
    """Parse an ISO `last_active` string; return None on garbage / missing."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _sdk_delete_session(session_id: str, directory: str | None = None) -> None:
    """Thin wrapper over the SDK hard-delete (extracted for test seam)."""
    from claude_agent_sdk import delete_session
    delete_session(session_id, directory)


async def _reap_transcript(sdk_session_id: str, directory: str | None) -> bool:
    """Hard-delete the on-disk transcript for an evicted session (spec §3.4.1 —
    Casa owns transcript reaping; the CLI's cleanupPeriodDays never fires under
    our SDK invocation, §8.6). Best-effort: a failure must not block eviction.

    Returns True only when the delete actually returned. The caller logs the
    destruction from THAT, never from this coroutine merely returning: the
    except arm swallows the failure, so a success and a swallowed failure are
    indistinguishable to a caller that only awaits."""
    if not sdk_session_id:
        return False
    try:
        await asyncio.to_thread(_sdk_delete_session, sdk_session_id, directory)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("transcript reap delete_session(%s) failed: %s", sdk_session_id, exc)
        return False
    return True


class SessionSweeper:
    """Periodic TTL-based eviction of stale `SessionRegistry` entries.

    Use::

        sweeper = SessionSweeper(
            registry=session_registry,
            session_ttl_days=30,
            webhook_session_ttl_days=1,
        )
        sweeper.start()
        ...
        await sweeper.stop()
    """

    def __init__(
        self,
        *,
        registry: SessionRegistry,
        session_ttl_days: int = _DEFAULT_SESSION_TTL_DAYS,
        webhook_session_ttl_days: int = _DEFAULT_WEBHOOK_SESSION_TTL_DAYS,
        sweep_interval_hours: float = _SWEEP_INTERVAL_HOURS,
        now: Callable[[], datetime] | None = None,
        directory_for: Callable[[str], str] | None = None,
    ) -> None:
        self._registry = registry
        self._session_ttl = timedelta(days=session_ttl_days)
        self._webhook_ttl = timedelta(days=webhook_session_ttl_days)
        self._interval_s = sweep_interval_hours * 3600.0
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._dir_for = directory_for or (lambda role: f"/config/agent-home/{role}")
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the sweep task. Idempotent — second call is a no-op."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="session-sweeper")
        logger.info(
            "Session sweeper started (session_ttl=%dd, webhook_ttl=%dd, "
            "interval=%.1fh)",
            self._session_ttl.days, self._webhook_ttl.days,
            self._interval_s / 3600.0,
        )

    async def stop(self) -> None:
        """Cancel the sweep task and wait for it to exit."""
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    # ------------------------------------------------------------------
    # Internals — exposed for test invocation
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await asyncio.sleep(self._interval_s)
                except asyncio.CancelledError:
                    raise
                try:
                    await self._sweep_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # A sweep failure must not crash the loop; next tick
                    # retries. Log loudly so operators notice.
                    logger.error(
                        "Session sweep failed; will retry next tick: %s", exc,
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            return

    async def _sweep_once(self) -> None:
        """Run one full sweep: classify under the registry lock; then, for
        each due entry and under that session's write gate (the one its turns
        hold), ask the confirmation gate, evict under the registry lock and
        delete the transcript. A held entry keeps its pointer, so a later
        sweep deletes it once the hold clears."""
        now = self._now()
        # (key, sdk_session_id, role, last_active as stored)
        due_entries: list[tuple[str, str, str, object]] = []
        retention = transcript_runs.retention()

        async with self._registry._lock:
            for key, entry in self._registry._data.items():
                channel, _, _remainder = key.partition("-")
                sid = str(entry.get("sdk_session_id") or "")
                role = entry.get("agent", "assistant")
                last_active = _parse_last_active(entry.get("last_active"))

                if last_active is None:
                    # No usable time: a transcript's retention cannot be
                    # measured, so an entry naming one is kept; a sid-less
                    # entry names nothing and goes.
                    due = not sid
                else:
                    # A2: v2 keys are channel-role-scope hashes — the raw
                    # remainder is never uuid-shaped, so the webhook-one-shot
                    # classification cannot be re-derived from the key.
                    # register() persists it explicitly as scope_class; an
                    # entry without the field (register() drops a None) is an
                    # ordinary session and gets the normal TTL.
                    is_webhook_oneshot = (
                        entry.get("scope_class") == "webhook_oneshot"
                    )
                    if is_webhook_oneshot:
                        ttl = self._webhook_ttl
                    else:
                        ttl = self._session_ttl
                    if channel in ("voice", "telegram"):
                        guard = max(ttl, freshness_window(channel))
                    else:
                        guard = ttl
                    # The transcript outlives the session's resumability by
                    # the retention: never removed before the freshness
                    # window (the resume test's own bound) plus it.
                    if sid:
                        guard = max(guard, freshness_window(channel) + retention)
                    stale_for = now - last_active
                    due = stale_for > guard

                if not due:
                    continue

                due_entries.append((key, sid, role, entry.get("last_active")))

        # Each due entry under its own session-write gate — the gate every
        # turn on that key holds (taken before the registry lock, as turns
        # take it), so no run of that session starts between the final
        # confirmation-gate check, the eviction and the delete. An entry
        # whose transcript is held keeps its pointer for a later sweep.
        from session_gate import session_write_gate
        evicted = 0
        for key, sid, role, stamp in due_entries:
            async with session_write_gate(key):
                directory = self._dir_for(role)
                if sid and await asyncio.to_thread(
                        transcript_runs.blocked, directory=directory,
                        session_id=sid):
                    logger.warning(
                        "Session sweep kept %s (session %s): a run in its "
                        "folder is not confirmed; retried next sweep", key, sid)
                    continue
                async with self._registry._lock:
                    entry = self._registry._data.get(key)
                    # Only the entry the check was about: one a turn
                    # re-registered or touched since stays.
                    if (entry is None
                            or str(entry.get("sdk_session_id") or "") != sid
                            or entry.get("last_active") != stamp):
                        continue
                    self._registry._data.pop(key, None)
                    await self._registry._save_locked()
                evicted += 1
                # The INFO is emitted from the reaper's own report of a
                # completed delete, never from the await returning: a
                # swallowed failure returns exactly like a success.
                if sid and await _reap_transcript(sid, directory):
                    logger.info(
                        "Session sweep deleted transcript for %s (session %s, "
                        "role %s)", key, sid, role,
                    )
                    await asyncio.to_thread(
                        transcript_runs.forget_deleted, directory=directory,
                        session_id=sid)

        if evicted:
            logger.info("Session sweep evicted %d entr(y|ies)", evicted)
