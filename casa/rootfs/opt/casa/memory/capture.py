"""Per-turn durable capture into ``memory.db``, with the spool fallback.

:func:`append` stores one item in one local transaction and returns a
:class:`Receipt`. A receipt means the item is durable: committed to the store,
or written to ``/data/memory/spool.jsonl`` and fsynced. A store that is
unavailable, or busy past :data:`CAPTURE_BUSY_TIMEOUT_S`, never drops a turn:
the item goes to the spool, and the next append that reaches a healthy store
drains the spool into it (:func:`drain_spool`). Only a failure to write even
the spool returns no receipt.

Every spool line carries its item's ``source_key``, and the drain inserts with
an ``INSERT`` that ignores a key already stored, so a drain cut off after its
commit and run again adds each item once. Appends to the spool and the drain
are serialized by one process-wide lock, :data:`_spool_lock`:

* a spool write takes it before it opens or creates the file and holds it
  through the write, the fsync and the close (and the directory fsync when it
  created the file); no descriptor is kept across acquisitions;
* a drain holds it from its read of the spool, through its commit, to the
  spool's removal and the directory fsync.

A line is therefore removed only by the drain that stored it, and a line
written while a drain runs lands in a spool the next drain reads. A
``threading.Lock`` is enough because one process captures and drains; the
blocking work runs in a worker thread (:func:`record` uses
``asyncio.to_thread``).

No model is called on this path and nothing is compared by text: the same
words said twice are two events. Which text is captured is
:mod:`memory.eligibility`'s; every key comes from :mod:`memory.occurrence`.

:func:`append` and :func:`drain_spool` take the store as an argument and use
no state of the running Casa process. :func:`configure` holds the store and
the operator-notice callable the live seams use through :func:`record`.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from memory import occurrence
from memory.store import MemoryStore
from timekeeping import resolve_tz

logger = logging.getLogger(__name__)

SPOOL_NAME = "spool.jsonl"

#: Seconds an append waits on a busy store before it spools instead.
CAPTURE_BUSY_TIMEOUT_S = 1.0

#: A spool whose oldest line is older than this earns one private notice.
SPOOL_NOTICE_AFTER_S = 3600.0
SPOOL_NOTICE = (
    "Memory has not been able to reach its store for over an hour. What is "
    "said is being kept aside and will be stored once the store is back."
)

STORED = "stored"
SPOOLED = "spooled"

#: Serializes every spool write with every drain (see the module docstring).
_spool_lock = threading.Lock()


@dataclass(frozen=True)
class Event:
    """One row of ``events`` as capture builds it. Every captured event is
    ``private`` and ``provisional``; ``origin_tier`` is its floor."""

    source_key: str
    role: str
    channel: str
    speaker_kind: str
    origin_kind: str
    text: str
    eligibility: str
    origin_tier: str | None
    occurred_at: str
    tz: str
    local_day: str
    session_id: str | None = None
    speaker_id: str | None = None
    speaker_name: str | None = None
    scheduled: bool = False
    reply_to: str | None = None
    provider_message_id: str | None = None
    tier: str = "private"
    tier_status: str = "provisional"
    provenance: str = "captured"


@dataclass(frozen=True)
class Summary:
    """One row of ``engagement_summaries``."""

    source_key: str
    role: str
    procedural_epoch: str
    tier: str
    text: str
    engagement_id: str
    at: str


_TABLES: dict[type, str] = {Event: "events", Summary: "engagement_summaries"}
_BY_TABLE = {table: cls for cls, table in _TABLES.items()}


@dataclass(frozen=True)
class Receipt:
    """Proof that an item is durable. ``spool_since`` is the time (epoch
    seconds) of the spool's oldest line when the item was spooled."""

    source_key: str
    outcome: str
    spool_since: float | None = None


def _stamp() -> tuple[str, str, str]:
    tz = resolve_tz()
    now = datetime.now(timezone.utc)
    return (now.isoformat(timespec="microseconds"), tz.key,
            now.astimezone(tz).date().isoformat())


def event(*, role: str, channel: str, speaker_kind: str, origin_kind: str,
          text: str, eligibility: str, origin_tier: str | None,
          source_key: str | None = None, **optional) -> Event:
    """An :class:`Event` stamped now, under *source_key* or a fresh
    ``event:`` key minted here, once."""
    occurred_at, tz, local_day = _stamp()
    return Event(
        source_key=source_key or occurrence.event_key(), role=role,
        channel=channel, speaker_kind=speaker_kind, origin_kind=origin_kind,
        text=text, eligibility=eligibility, origin_tier=origin_tier,
        occurred_at=occurred_at, tz=tz, local_day=local_day, **optional)


def summary(*, role: str, procedural_epoch: str, tier: str, text: str,
            engagement_id: str) -> Summary:
    """A :class:`Summary` stamped now, under a fresh ``event:`` key."""
    return Summary(
        source_key=occurrence.event_key(), role=role,
        procedural_epoch=procedural_epoch or "", tier=tier, text=text,
        engagement_id=engagement_id, at=_stamp()[0])


def origin_kind_of(*, scheduled: bool, operator_authored: bool,
                   delegated_result: bool = False,
                   callback: bool = False) -> str:
    """The ``origin_kind`` of a turn, from the mechanical facts of its
    arrival: Casa's schedule fired it, a person typed it, a delegation's
    completion was synthesized into it, or a callback woke it."""
    if scheduled:
        return "scheduled"
    if operator_authored:
        return "operator"
    if delegated_result:
        return "delegated_result"
    if callback:
        return "callback"
    return "system"


# --- the store -------------------------------------------------------------

def spool_path(directory: str) -> str:
    return os.path.join(directory, SPOOL_NAME)


def _insert(conn: sqlite3.Connection, item: Event | Summary) -> int:
    """Insert *item*; an item whose key is already stored adds nothing.
    Returns the number of rows added."""
    row = dataclasses.asdict(item)
    if isinstance(item, Event):
        row["scheduled"] = int(bool(item.scheduled))
        row["recorded_at"] = datetime.now(timezone.utc).isoformat(
            timespec="microseconds")
    table = _TABLES[type(item)]
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({marks}) "
        "ON CONFLICT(source_key) DO NOTHING", tuple(row.values()))
    return cur.rowcount


def append(store: MemoryStore, item: Event | Summary) -> Receipt | None:
    """Make *item* durable: store it, or spool it. Blocking; never raises.

    After a healthy store write, a spool left by earlier failures is drained.
    ``None`` means not even the spool could be written."""
    try:
        with store.transaction(
                write=True, busy_timeout=CAPTURE_BUSY_TIMEOUT_S) as conn:
            _insert(conn, item)
    except sqlite3.IntegrityError:
        # A malformed item (a constraint, not the store's health): spooling
        # it would only make every later drain fail on it.
        logger.exception("memory capture: %s refused by the store schema",
                         item.source_key)
        return None
    except Exception as exc:  # noqa: BLE001 — any store failure spools
        logger.warning("memory capture: store not writable (%s: %s); "
                       "spooling %s", type(exc).__name__, exc, item.source_key)
        return _spool(store.directory, item)
    if os.path.exists(spool_path(store.directory)):
        try:
            drain_spool(store)
        except Exception:  # noqa: BLE001 — the spool stays for the next one
            logger.warning("memory capture: spool drain failed; kept for the "
                           "next healthy append", exc_info=True)
    return Receipt(item.source_key, STORED)


# --- the spool -------------------------------------------------------------

#: The fsync calls, named so a test can observe the descriptor they sync.
_fsync = os.fsync


def _fsync_dir(directory: str) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _fsync(fd)
    finally:
        os.close(fd)


def _spool_line(item: Event | Summary) -> bytes:
    record = {"table": _TABLES[type(item)], "spooled_at": time.time(),
              "item": dataclasses.asdict(item)}
    return (json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            + "\n").encode("utf-8")


def _oldest(fd: int) -> float | None:
    """``spooled_at`` of the spool's first line."""
    head = os.pread(fd, 1 << 16, 0).split(b"\n", 1)[0]
    try:
        value = json.loads(head)["spooled_at"]
    except (ValueError, KeyError, TypeError):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _spool(directory: str, item: Event | Summary) -> Receipt | None:
    try:
        return _spool_write(directory, _spool_line(item), item.source_key)
    except Exception:  # noqa: BLE001 — no receipt; the seam logs nothing more
        logger.exception("memory capture: %s could not be spooled either",
                         item.source_key)
        return None


def _spool_write(directory: str, line: bytes, key: str) -> Receipt:
    path = spool_path(directory)
    with _spool_lock:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        created = not os.path.lexists(path)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC,
                     0o600)
        try:
            size = os.fstat(fd).st_size
            # A line torn by a crash has no newline; start on a fresh one so
            # this item is never glued to it.
            if size and os.pread(fd, 1, size - 1) != b"\n":
                line = b"\n" + line
            view = memoryview(line)
            while view:
                view = view[os.write(fd, view):]
            _fsync(fd)
            since = _oldest(fd)
        finally:
            os.close(fd)
        if created:
            _fsync_dir(directory)
    return Receipt(key, SPOOLED, since)


def _parse(raw: bytes) -> tuple[list[Event | Summary], int]:
    items: list[Event | Summary] = []
    torn = 0
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            items.append(_BY_TABLE[record["table"]](**record["item"]))
        except (ValueError, KeyError, TypeError):
            torn += 1
    return items, torn


def drain_spool(store: MemoryStore) -> int:
    """Store every spooled item, then remove the spool. Blocking.

    Returns the number of rows added. Raises when the store cannot take them
    (the spool is then left as it was). The export drains through this too;
    no caller holds :data:`_spool_lock` when calling it."""
    path = spool_path(store.directory)
    with _spool_lock:
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            return 0
        items, torn = _parse(raw)
        added = refused = 0
        with store.transaction(
                write=True, busy_timeout=CAPTURE_BUSY_TIMEOUT_S) as conn:
            for item in items:
                try:
                    added += _insert(conn, item)
                except sqlite3.IntegrityError:
                    refused += 1
        os.unlink(path)
        _fsync_dir(store.directory)
    if torn or refused:
        # A torn line was never fsynced, so it was never a receipt.
        logger.warning("memory spool drained with %d torn and %d refused "
                       "line(s)", torn, refused)
    logger.info("memory spool drained: %d item(s), %d added", len(items), added)
    return added


# --- the live seams --------------------------------------------------------

_UNSET = object()
_wiring: dict[str, object] = {"store": None, "notify": None}
#: Spools (by their oldest line's time) the operator was already told about.
_noticed: set[float] = set()


def configure(*, store: MemoryStore | None | object = _UNSET,
              notify: Callable[[str], Awaitable[None]] | None | object = _UNSET
              ) -> None:
    """Wire the store the live seams capture into, and the coroutine that
    sends the operator a private notice. Unwired, :func:`record` captures
    nothing."""
    if store is not _UNSET:
        _wiring["store"] = store
    if notify is not _UNSET:
        _wiring["notify"] = notify


def active_store() -> MemoryStore | None:
    store = _wiring["store"]
    return store if isinstance(store, MemoryStore) else None


async def record(item: Event | Summary) -> Receipt | None:
    """Capture *item* off the event loop; the seams await this. Returns the
    receipt, or ``None`` when nothing is wired or not even the spool took
    it. Never raises, except the caller's own cancellation."""
    store = active_store()
    if store is None:
        return None
    receipt = await asyncio.to_thread(append, store, item)
    if receipt is not None and receipt.outcome == SPOOLED:
        await _notice_if_old(receipt.spool_since)
    return receipt


async def _notice_if_old(since: float | None) -> None:
    notify = _wiring["notify"]
    if (since is None or notify is None or since in _noticed
            or time.time() - since <= SPOOL_NOTICE_AFTER_S):
        return
    _noticed.add(since)
    try:
        await notify(SPOOL_NOTICE)  # type: ignore[operator]
    except Exception:  # noqa: BLE001 — told on a later spooled append instead
        _noticed.discard(since)
        logger.warning("memory spool notice could not be sent", exc_info=True)
