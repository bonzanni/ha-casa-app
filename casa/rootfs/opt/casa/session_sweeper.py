"""Periodic session-registry TTL sweep (spec 5.2 §6).

Stops `SessionRegistry.sessions.json` from growing unbounded. Every
`sweep_interval_hours` (default 6) a background task wakes up, iterates
the registry under its 5.1 lock, drops entries whose `last_active` is
older than the configured TTL (or freshness window, whichever is larger —
spec §3.4(3)), persists the reduced registry, and hard-deletes the
on-disk transcript via the SDK's ``delete_session(sid, directory)``
(spec §3.4.1 — Casa owns transcript reaping; the CLI's cleanupPeriodDays
sweep never fires under Casa's SDK invocation, §8.6).

One class of entry is exempt and is never evicted or reaped, however
stale: one that names a transcript on a channel whose conversations are
written to the long-term bank (INV-MEM-017, #886). Such an entry has
not been banked — a successful retain removes the entry — so deleting
its transcript would destroy the only copy of a conversation whose
retention Casa still owes.

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

from channel_policy import writes_to_bank
from session_registry import SessionRegistry
from session_saver import freshness_window

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
        """Run one full sweep: classify + evict under lock, then reap transcripts."""
        now = self._now()
        to_evict: list[tuple[str, str, str]] = []  # (key, sdk_session_id, role)
        # Entries that were DUE for eviction and are held back by INV-MEM-017:
        # (key, sdk_session_id, staleness or None when last_active is garbage).
        protected: list[tuple[str, str, timedelta | None]] = []

        async with self._registry._lock:
            for key, entry in self._registry._data.items():
                channel, _, _remainder = key.partition("-")
                sid = str(entry.get("sdk_session_id") or "")
                role = entry.get("agent", "assistant")
                last_active = _parse_last_active(entry.get("last_active"))

                if last_active is None:
                    due, stale_for = True, None
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
                    stale_for = now - last_active
                    due = stale_for > guard

                if not due:
                    continue

                # INV-MEM-017 (#886). The property that licenses destroying a
                # conversation's only copy is that its retention is no longer
                # owed — never the age of its pointer. On a bank-writable
                # channel the entry's own SURVIVAL is that fact: `finish_save`
                # REMOVES the entry on a successful retain while
                # `clear_save_claim` KEEPS it on a failed one, so an entry that
                # is still here was never banked. Hold it back, whatever its
                # age, provenance, claims, or timestamp.
                #
                # A sid-less entry stays evictable: it names no transcript, so
                # keeping it would protect no bytes and accumulate a pointer.
                #
                # This reads a frozenset and a dict already in hand. It takes
                # no second lock and awaits nothing, so the sweeper stays
                # OUTSIDE the global lock order (INV-CONC-004/005) rather than
                # becoming a new participant in it — taking a gate here, inside
                # `registry._lock`, would be a permanent deadlock by that
                # invariant's own text.
                if sid and writes_to_bank(channel):
                    protected.append((key, sid, stale_for))
                    continue

                to_evict.append((key, sid, role))

            if to_evict:
                for key, _sdk_sid, _role in to_evict:
                    self._registry._data.pop(key, None)
                await self._registry._save_locked()

        # The retention debt is said out loud, once per held-back entry per
        # sweep: a bank that has been failing for a month is the only way an
        # entry gets here, and nothing else in Casa reports it.
        for key, sid, stale_for in protected:
            logger.warning(
                "Session sweep kept %s (session %s, stale %s) — its conversation "
                "is not in long-term memory yet, so its transcript is not deleted",
                key, sid,
                f"{stale_for.days}d" if stale_for is not None else "unknown",
            )

        if not to_evict:
            return  # no save, no transcript-reap work

        logger.info("Session sweep evicted %d entr(y|ies)", len(to_evict))

        # Transcript reap happens OUTSIDE the lock — best-effort, never
        # blocks the registry. The INFO is emitted from the reaper's own
        # report of a completed delete, never from the await returning: a
        # swallowed failure returns exactly like a success.
        for key, sdk_sid, role in to_evict:
            if sdk_sid and await _reap_transcript(sdk_sid, self._dir_for(role)):
                logger.info(
                    "Session sweep deleted transcript for %s (session %s, role %s)",
                    key, sdk_sid, role,
                )
