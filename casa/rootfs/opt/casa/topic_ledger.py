# casa/rootfs/opt/casa/topic_ledger.py
"""Terminal-engagement topic ledger (topic retention & cleanup, 2026-07-10).

Finished engagements leave closed forum topics in the Telegram engagement
supergroup forever, and the Bot API cannot enumerate topics — cleanup can
only target topics Casa *remembers*. This module owns that memory:
``/data/topic-ledger.json``, a small JSON list recording each terminal
engagement's topic, the chat it was created against ([AR-2]) and a
``delete_after`` deadline (the same 7-day window as its workspace).

Finalize / abort / orphan paths ``append`` (idempotent by engagement id);
the periodic sweep and the configurator's on-demand tool call
``sweep_topics``, which deletes due topics through the Telegram channel and
prunes the ledger per the [AR-5] outcome contract — an entry is NEVER
dropped on an unrecognized error (until it has kept failing for
``STUCK_ENTRY_MAX_AGE_SECONDS`` past its deadline, which bounds ledger
growth under permanent failure). All read-modify-write cycles go through
one module lock; writes are atomic (:func:`atomic_io.atomic_write_json`);
a corrupt ledger file is archived aside as ``<path>.casabak`` (the
config_sync convention) and treated as empty, never truncated in place.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import time
from typing import Any

from atomic_io import PRIVATE, atomic_write_json

logger = logging.getLogger(__name__)

# Intentionally equal to tools._WORKSPACE_RETENTION_DAYS — topics and
# workspaces expire together. The constant lives HERE so telegram.py and
# tools.py can both import it without a cycle.
TOPIC_RETENTION_DAYS = 7

LEDGER_PATH = "/data/topic-ledger.json"

# [AR-6] bulk-rate safety: deletions are serialized with this inter-call gap.
DELETE_SPACING_SECONDS = 0.3

# Bounds infinite retry/growth under permanent failure: an entry still
# failing this long past its delete_after is dropped from the ledger — the
# topic stays in Telegram; the operator cleans it up manually.
STUCK_ENTRY_MAX_AGE_SECONDS = 90 * 86400

# Telegram can demand flood waits of hours; a sweep (or tool call) must
# never stall that long. Over the cap the RetryAfter is left transient —
# no sleep, no in-sweep retry — and the entry retries at the next sweep.
RETRY_AFTER_CAP_SECONDS = 60.0

# [AR-5] the Bot API folds distinct failures into BadRequest; only the
# message substring (case-insensitive) tells them apart. Extend the
# permission list once the real insufficient-rights string is captured on
# the N150 live verify (the bot lacks the right today).
_NOT_FOUND_SUBSTRINGS = (
    "message thread not found",
    # A deleted supergroup: the topic is unreachable forever — treat as gone.
    "chat not found",
)
_PERMISSION_SUBSTRINGS = (
    "not enough rights",
    "need administrator rights",
    "can_delete_messages",
)

# Guards every ledger read-modify-write cycle (append/remove; sweep mutates
# through them). Deletion calls themselves run outside the lock so a slow
# Telegram sweep can never stall a finalize append.
_LOCK = asyncio.Lock()

# #311: serializes whole sweeps — the scheduled sweep and the configurator
# sweep tool could both load the same due entry before either removed it and
# double-issue the Telegram deletion (the loser folded into `deleted` as
# not_found, spending flood budget and double-counting). Distinct from _LOCK
# so a sweep's Telegram calls never stall a finalize append; ordering is
# strictly _SWEEP_LOCK -> _LOCK, never inverted.
_SWEEP_LOCK = asyncio.Lock()


def _resolve(path: str | None) -> str:
    # Read the module global at call time so tests can monkeypatch it.
    return path if path is not None else LEDGER_PATH


def _read_entries(target: str) -> list[dict[str, Any]]:
    """Sync read. Missing file → []. Corrupt file → moved aside as
    ``<target>.casabak`` + warning + [] — never truncated in place [AR-9]."""
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list) or not all(
            isinstance(entry, dict) for entry in data
        ):
            raise ValueError("topic ledger is not a JSON list of objects")
    except FileNotFoundError:
        return []
    except ValueError:  # includes JSONDecodeError and UnicodeDecodeError
        bak = target + ".casabak"
        try:
            os.replace(target, bak)
        except OSError:
            logger.warning(
                "topic ledger corrupt at %s and could not be archived aside; "
                "treating as empty", target, exc_info=True,
            )
            return []
        logger.warning(
            "topic ledger corrupt at %s — archived to %s, treating as empty",
            target, bak,
        )
        return []
    return data


def _write_entries(target: str, entries: list[dict[str, Any]]) -> None:
    atomic_write_json(target, entries, indent=2, mode=PRIVATE)


async def _to_thread_uncancellable(fn, *args):
    """Run *fn* in a worker thread; on cancellation, WAIT for the thread to
    finish before propagating (v0.183.0 diff gate, Terra S1).

    ``await asyncio.to_thread(...)`` cancels the *await*, never the thread —
    a cancelled read-modify-write under ``_LOCK`` used to release the lock
    while its stale write was still in flight, clobbering whatever a
    concurrent ``append``/``remove`` wrote next. Holding the caller (and so
    ``_LOCK``) until the thread completes makes the RMW atomic under
    cancellation; the original CancelledError is re-raised, and a late
    exception from the worker is subordinated to it.
    """
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 — the cancellation wins
                break
        raise


async def append(
    *,
    engagement_id: str,
    chat_id: int | None,
    topic_id: int,
    outcome: str,
    closed_at: float | None = None,
    path: str | None = None,
) -> None:
    """Record a terminal engagement's topic for retention-expiry deletion.

    Idempotent by ``engagement_id`` (a re-append of a known id is a no-op),
    so overlapping terminal paths can all call it. May raise on I/O failure
    — callers wrap it (a ledger failure must never abort the finalize
    funnel [AR-4]); the module stays honest about persistence errors.
    """
    target = _resolve(path)
    ts = time.time() if closed_at is None else closed_at
    entry = {
        "engagement_id": engagement_id,
        "chat_id": chat_id,
        "topic_id": topic_id,
        "outcome": outcome,
        "closed_at": ts,
        "delete_after": ts + TOPIC_RETENTION_DAYS * 86400,
    }
    async with _LOCK:
        entries = await _to_thread_uncancellable(_read_entries, target)
        if any(e.get("engagement_id") == engagement_id for e in entries):
            return
        entries.append(entry)
        await _to_thread_uncancellable(_write_entries, target, entries)


async def load(path: str | None = None) -> list[dict]:
    """Read the ledger. Missing file → []; corrupt file → archived aside as
    ``<path>.casabak`` with a warning and treated as empty."""
    async with _LOCK:
        return await asyncio.to_thread(_read_entries, _resolve(path))


async def remove(engagement_ids: set[str], path: str | None = None) -> None:
    """Drop entries by engagement id; unknown ids are ignored."""
    if not engagement_ids:
        return
    target = _resolve(path)
    async with _LOCK:
        entries = await _to_thread_uncancellable(_read_entries, target)
        remaining = [
            e for e in entries if e.get("engagement_id") not in engagement_ids
        ]
        if len(remaining) != len(entries):
            await _to_thread_uncancellable(_write_entries, target, remaining)


def _telegram_error_module() -> Any | None:
    # Lazy/defensive: the module must import (and classify sanely) even
    # where python-telegram-bot is absent.
    try:
        from telegram import error as tg_error
    except Exception:  # noqa: BLE001 — any import failure means "no telegram"
        return None
    return tg_error


def classify_delete_error(exc: BaseException) -> str:
    """Map a ``delete_topic`` failure to the [AR-5] outcome contract:
    ``"not_found" | "permission" | "transient" | "unknown"``.

    python-telegram-bot gives class-level distinction only for transients;
    not-found and no-rights are both ``BadRequest``, distinguishable only by
    message substring. ``BadRequest`` subclasses ``NetworkError`` in PTB, so
    it must be tested BEFORE the transient classes — an unrecognized
    BadRequest is "unknown" (kept), never "transient".
    """
    tg_error = _telegram_error_module()
    if tg_error is None:
        return "unknown"

    bad_request = getattr(tg_error, "BadRequest", None)
    if bad_request is not None and isinstance(exc, bad_request):
        message = str(exc).lower()
        if any(s in message for s in _NOT_FOUND_SUBSTRINGS):
            return "not_found"
        if any(s in message for s in _PERMISSION_SUBSTRINGS):
            return "permission"
        return "unknown"

    forbidden = getattr(tg_error, "Forbidden", None)
    if forbidden is not None and isinstance(exc, forbidden):
        return "permission"

    transients = tuple(
        cls
        for cls in (
            getattr(tg_error, name, None)
            for name in ("RetryAfter", "TimedOut", "NetworkError")
        )
        if cls is not None
    )
    if transients and isinstance(exc, transients):
        return "transient"
    return "unknown"


async def _delete_with_flood_retry(
    channel: Any, topic_id: Any,
) -> tuple[str, BaseException | None]:
    """One deletion attempt, honoring a single ``RetryAfter`` [AR-6].
    Returns ``("ok", None)`` or ``(classification, exc)``."""
    tg_error = _telegram_error_module()
    retry_after_cls = getattr(tg_error, "RetryAfter", None) if tg_error else None
    try:
        await channel.delete_topic(topic_id)
        return "ok", None
    except Exception as first:  # noqa: BLE001 — classified below, never dropped
        if retry_after_cls is None or not isinstance(first, retry_after_cls):
            return classify_delete_error(first), first
        delay = getattr(first, "retry_after", None)
        # PTB 22 may hand back a timedelta instead of seconds.
        seconds = (
            delay.total_seconds()
            if isinstance(delay, datetime.timedelta)
            else float(delay or 1.0)
        )
        if seconds > RETRY_AFTER_CAP_SECONDS:
            # Telegram can demand hours; a sweep/tool call must never stall
            # that long — the entry retries at the next sweep.
            return "transient", first
        await asyncio.sleep(seconds)
        try:
            await channel.delete_topic(topic_id)
            return "ok", None
        except Exception as second:  # noqa: BLE001 — one retry only, then classify
            return classify_delete_error(second), second


def _effective_delete_after(entry: dict[str, Any]) -> float | None:
    """Resolve an entry's delete-after deadline, tolerating malformed data —
    the sweep must never abort on one bad entry. A malformed ``delete_after``
    falls back to ``closed_at`` + retention; ``None`` means malformed beyond
    use (no usable timestamp at all) — the caller drops the entry without a
    delete attempt."""
    try:
        return float(entry.get("delete_after"))
    except (TypeError, ValueError):
        pass
    try:
        return float(entry.get("closed_at")) + TOPIC_RETENTION_DAYS * 86400
    except (TypeError, ValueError):
        return None


async def sweep_topics(
    channel: Any,
    *,
    chat_id: int | None,
    scope: str = "due",
    dry_run: bool = False,
    now: float | None = None,
    path: str | None = None,
) -> dict:
    """Delete due terminal topics through ``channel`` and prune the ledger.

    ``channel`` duck-type: ``await channel.delete_topic(thread_id)``, raising
    Telegram errors on failure. Returns ``{"deleted", "kept",
    "dropped_mismatched", "dropped_stuck", "dropped_malformed", "failures",
    "needs_permission", "dry_run", "targets"}`` where deleted + kept +
    dropped_mismatched + dropped_stuck + dropped_malformed == entries seen;
    ``deleted`` counts entries resolved (topic deleted now, or already gone
    — "not_found"); ``targets`` echoes the resolved entries as
    ``{engagement_id, topic_id}`` pairs (dry-run: the would-be-deleted
    ones); ``dry_run`` echoes the flag.

    - ``scope="due"``: only entries past ``delete_after`` (``now`` defaults
      to ``time.time()``); ``"all_terminal"``: every entry.
    - [AR-2] ``entry.chat_id`` must equal *chat_id* to be deletable.
      Mismatched entries refer to a previous supergroup where topic ids may
      collide with live topics in the new one — dropped from the ledger with
      a warning, NEVER deleted. Entries with no recorded chat_id are kept
      (one aggregate warning per sweep, never auto-deleted). *chat_id* None
      (telegram unconfigured) skips cleanly: everything kept, no warnings
      [AR-8].
    - ``dry_run``: classify what WOULD happen — no deletion, no ledger
      mutation, ``failures`` empty, ``needs_permission`` False.
    - [AR-6] deletions are serialized with ``DELETE_SPACING_SECONDS`` between
      calls; one ``RetryAfter`` per entry is honored (sleep + single retry)
      unless it demands more than ``RETRY_AFTER_CAP_SECONDS``.
    - [AR-5] per-entry outcome: ok/not_found → entry removed; permission →
      kept + ``needs_permission=True`` + recorded in ``failures``;
      transient/unknown → kept + recorded. An entry is NEVER removed on an
      unrecognized error — until it has kept failing for
      ``STUCK_ENTRY_MAX_AGE_SECONDS`` past its deadline, when it is dropped
      (``dropped_stuck``, warned; needs_permission still fires for the
      permission class) so a permanent failure cannot grow the ledger
      forever. The topic itself stays in Telegram.
    - A malformed ``delete_after`` falls back to ``closed_at`` + retention;
      with neither usable the entry is dropped (``dropped_malformed``,
      warned) without a delete attempt.
    - #311: sweeps are serialized process-wide (``_SWEEP_LOCK``) — a
      concurrent entrant waits, then sees the ledger the first sweep left.
      Each resolved entry is checkpointed out of the ledger immediately
      (before the next cancellable await), so a cancelled sweep forgets at
      most the single in-flight entry, whose re-delete self-heals as
      not_found.
    """
    async with _SWEEP_LOCK:
        return await _sweep_topics_locked(
            channel, chat_id=chat_id, scope=scope, dry_run=dry_run,
            now=now, path=path,
        )


async def _sweep_topics_locked(
    channel: Any,
    *,
    chat_id: int | None,
    scope: str,
    dry_run: bool,
    now: float | None,
    path: str | None,
) -> dict:
    target = _resolve(path)
    ts = time.time() if now is None else now
    entries = await load(path=target)

    result: dict[str, Any] = {
        "deleted": 0,
        "kept": 0,
        "dropped_mismatched": 0,
        "dropped_stuck": 0,
        "dropped_malformed": 0,
        "failures": [],
        "needs_permission": False,
        "dry_run": dry_run,
        "targets": [],
    }
    if chat_id is None:
        result["kept"] = len(entries)
        return result

    # #311: resolved entries are checkpointed out of the ledger immediately
    # (per entry, before the next cancellable await) rather than batched into
    # one remove() at the end — a cancelled sweep must not forget deletions
    # it already performed.
    null_chat_count = 0
    calls_made = 0
    for entry in entries:
        engagement_id = entry.get("engagement_id")
        topic_id = entry.get("topic_id")
        delete_after = _effective_delete_after(entry)
        if delete_after is None:
            logger.warning(
                "topic ledger: entry %s (topic %s) has no usable delete_after "
                "or closed_at — %s without deleting",
                engagement_id, topic_id,
                "would drop (dry run)" if dry_run else "dropping",
            )
            result["dropped_malformed"] += 1
            if not dry_run:
                await remove({engagement_id}, path=target)
            continue
        if scope != "all_terminal" and delete_after > ts:
            result["kept"] += 1
            continue
        entry_chat = entry.get("chat_id")
        if entry_chat is None:
            null_chat_count += 1
            result["kept"] += 1
            continue
        if entry_chat != chat_id:
            logger.warning(
                "topic ledger: entry %s (topic %s) was recorded for chat %s "
                "but the current chat is %s — %s without deleting",
                engagement_id, topic_id, entry_chat, chat_id,
                "would drop (dry run)" if dry_run else "dropping",
            )
            result["dropped_mismatched"] += 1
            if not dry_run:
                await remove({engagement_id}, path=target)
            continue
        if dry_run:
            result["deleted"] += 1
            result["targets"].append(
                {"engagement_id": engagement_id, "topic_id": topic_id},
            )
            continue

        if calls_made:
            await asyncio.sleep(DELETE_SPACING_SECONDS)
        outcome, exc = await _delete_with_flood_retry(channel, topic_id)
        calls_made += 1

        if outcome == "ok":
            result["deleted"] += 1
            result["targets"].append(
                {"engagement_id": engagement_id, "topic_id": topic_id},
            )
            await remove({engagement_id}, path=target)
        elif outcome == "not_found":
            logger.info(
                "topic ledger: topic %s (entry %s) already gone — entry dropped",
                topic_id, engagement_id,
            )
            result["deleted"] += 1
            result["targets"].append(
                {"engagement_id": engagement_id, "topic_id": topic_id},
            )
            await remove({engagement_id}, path=target)
        elif ts > delete_after + STUCK_ENTRY_MAX_AGE_SECONDS:
            # Bounded retry under permanent failure: still nag for the
            # permission class, then drop the entry — the topic stays in
            # Telegram for manual cleanup.
            if outcome == "permission":
                result["needs_permission"] = True
            logger.warning(
                "topic ledger: topic %s (entry %s) has kept failing (%s) for "
                "over %d days past its deadline — dropping the entry; the "
                "topic stays in Telegram for manual cleanup: %s",
                topic_id, engagement_id, outcome,
                STUCK_ENTRY_MAX_AGE_SECONDS // 86400, exc,
            )
            result["dropped_stuck"] += 1
            await remove({engagement_id}, path=target)
        elif outcome == "permission":
            logger.warning(
                "topic ledger: no permission to delete topic %s (entry %s): %s",
                topic_id, engagement_id, exc,
            )
            result["kept"] += 1
            result["needs_permission"] = True
            result["failures"].append({
                "engagement_id": engagement_id,
                "topic_id": topic_id,
                "reason": "permission",
            })
        else:  # transient / unknown — kept; NEVER dropped on an unrecognized error
            logger.warning(
                "topic ledger: deleting topic %s (entry %s) failed (%s): %s",
                topic_id, engagement_id, outcome, exc,
            )
            result["kept"] += 1
            result["failures"].append({
                "engagement_id": engagement_id,
                "topic_id": topic_id,
                "reason": outcome,
            })

    if null_chat_count:
        logger.warning(
            "topic ledger: %d entries have no chat_id — kept, never "
            "auto-deleted", null_chat_count,
        )
    return result
