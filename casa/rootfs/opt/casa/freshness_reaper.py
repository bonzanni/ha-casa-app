# casa/rootfs/opt/casa/freshness_reaper.py
"""Primary long-term-save trigger (spec §4.2 #1). A background pass on a fixed
~hourly cadence (and once at boot) scans the registry; any conversational entry
idle past its channel's freshness window is handled by channel type: cold entries
on write-trusted channels (telegram) are retained via save_fn; cold entries on
recall-only channels (voice) are dropped from the registry (not persisted). Safe
because a past-freshness session is never resumed (§3.3).

C3: a save that crashes between claim and finish strands the entry with a
``consolidated_at`` marker; a marker older than the stale threshold (~2× the reap
interval) is treated as a failed claim and released so the sweep retries."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from channel_policy import writes_to_bank
from session_saver import freshness_window, retry_spooled_cold_retains, save_session

logger = logging.getLogger(__name__)

_REAP_INTERVAL_S = 3600.0  # fixed ~hourly (NOT freshness/4 — spec §4.2)
_CONVERSATIONAL = ("voice", "telegram")
_STALE_CLAIM_MULTIPLIER = 2  # a claim older than N× the interval = crashed save (C3)


class FreshnessReaper:
    """Primary long-term-save trigger for conversational sessions.

    Runs a sweep at boot (to catch sessions that went cold during downtime) then
    once every ``interval_s`` seconds.  Cold entries are handled by channel type:
    write-trusted channels (telegram) are retained via ``save_fn``; recall-only
    channels (voice) are dropped from the registry (not persisted).
    Injectable ``now``/``save_fn`` make the class fully testable without I/O.
    Includes C3 stale-claim recovery: a ``consolidated_at`` marker older than
    ``_STALE_CLAIM_MULTIPLIER × interval_s`` is treated as a crashed save and
    released so the next sweep can retry.
    """

    def __init__(
        self, *, registry, semantic_memory,
        directory_for: Callable[[str], str],
        now: Callable[[], datetime] | None = None,
        save_fn: Callable[..., Awaitable[bool]] = save_session,
        interval_s: float = _REAP_INTERVAL_S,
    ) -> None:
        self._reg = registry
        self._sem = semantic_memory
        self._dir_for = directory_for
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._save = save_fn
        self._interval = interval_s
        self._stale_after = timedelta(seconds=_STALE_CLAIM_MULTIPLIER * interval_s)
        self._task: asyncio.Task | None = None

    def _is_stale_claim(self, claimed_iso: object, now: datetime) -> bool:
        """True if a consolidated_at marker is old enough to be a crashed save (C3)."""
        if not isinstance(claimed_iso, str):
            return True  # unparseable marker → reclaim
        try:
            claimed = datetime.fromisoformat(claimed_iso)
        except ValueError:
            return True
        return (now - claimed) > self._stale_after

    async def sweep_once(self) -> None:
        now = self._now()
        entries = list(self._reg.all_entries().items())
        # #526: capture registration generations in the SAME no-await block as
        # the entry copy — the per-key guards below must compare against the
        # generation of the entry THIS sweep judged, not whatever registered
        # during the awaits between keys.
        generations = {key: self._reg.generation(key) for key, _ in entries}
        for key, entry in entries:
            try:
                channel = key.partition("-")[0]
                if channel not in _CONVERSATIONAL:
                    continue  # webhook/scheduler one-shots are not retained (§4.2)
                la = entry.get("last_active")
                if not isinstance(la, str):
                    continue
                try:
                    last = datetime.fromisoformat(la)
                except ValueError:
                    continue
                if now - last <= freshness_window(channel):
                    continue  # still live → never save (would risk an active session)
                claimed = entry.get("consolidated_at")
                if claimed:
                    if self._is_stale_claim(claimed, now):
                        logger.warning("freshness reaper: releasing stale save-claim for %s", key)
                        await self._reg.clear_save_claim(key)
                    else:
                        continue  # a save is genuinely in-flight → let it finish
                # Task 10: decode the entry into an immutable snapshot. A legacy
                # pre-Task-9 entry or one with corrupt/absent provenance yields
                # either None (unusable entry) or a snapshot whose provenance
                # fields are None (save_session refuses to retain those — "never
                # retain with invented authorship" — and would otherwise be
                # retried every sweep forever). Drop the stale pointer in both
                # cases rather than handing it to a save that can't succeed.
                from agent import snapshot_session_entry
                snapshot = snapshot_session_entry(entry)
                # #353 (Sol r2): both direct-removal branches below are
                # sid-guarded against the SNAPSHOTTED entry — the sweep
                # iterates a stale all_entries() copy across awaits, so a new
                # turn can re-register the key at any point; an unconditional
                # remove would delete that fresh live session. A snapshot
                # without a sid guards on None (declines once any real
                # session registers).
                if (
                    snapshot is None
                    or snapshot.speaker_provenance is None
                    or snapshot.user_provenance is None
                ):
                    await self._reg.remove(
                        key, expected_sid=entry.get("sdk_session_id"),
                        expected_generation=generations[key],
                    )
                    continue
                if not writes_to_bank(channel):
                    # Recall-only channel (voice): nothing to persist; drop the cold
                    # pointer so the registry does not accumulate dead voice entries.
                    await self._reg.remove(
                        key, expected_sid=snapshot.sdk_session_id,
                        expected_generation=generations[key],
                    )
                    continue
                # The reduced save_session reads speaker/user provenance from the
                # entry snapshot itself; only the transcript directory (routed
                # through the injected role→home resolver) and channel are passed.
                # expected_sid (#353) pins the save to the session THIS sweep
                # judged cold — a new turn racing the claim keeps its session.
                await self._save(
                    key, self._reg, self._sem,
                    directory=self._dir_for(snapshot.agent), channel=channel,
                    expected_sid=snapshot.sdk_session_id,
                    expected_generation=generations[key],
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad entry must not block the rest
                logger.error("freshness reaper: entry %s failed: %s", key, exc, exc_info=True)

        # #345: drive the durable retry spool for failed gap retains
        # (retain_cold_session runs registry-decoupled, so this sweep is its
        # only retry path). Guarded — a spool failure must not kill the sweep.
        try:
            await retry_spooled_cold_retains(self._sem)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.error("freshness reaper: cold-retain retry pass failed", exc_info=True)

    async def _run(self) -> None:
        try:
            try:
                await self.sweep_once()           # run-at-boot (catches downtime-cold sessions)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("freshness boot-sweep failed: %s", exc, exc_info=True)
            while True:
                await asyncio.sleep(self._interval)
                try:
                    await self.sweep_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.error("freshness sweep failed; retry next tick: %s", exc, exc_info=True)
        except asyncio.CancelledError:
            return

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="freshness-reaper")
        logger.info("Freshness reaper started (interval=%.0fs, boot-sweep)", self._interval)

    async def stop(self) -> None:
        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
