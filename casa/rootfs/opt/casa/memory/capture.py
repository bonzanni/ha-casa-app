"""Per-turn durable capture into ``memory.db``, with the spool fallback.

:func:`append` makes one item durable and returns a :class:`Receipt`. A
receipt means the item is durable: committed to the store, or written to
``/data/memory/spool.jsonl`` and fsynced. That write is all a seam waits for:
one store attempt bounded by :data:`CAPTURE_BUSY_TIMEOUT_S`, and, when the
store is unavailable or busy past it, one fsynced spool line. Only a failure
to write even the spool returns no receipt.

Everything else runs detached, never awaited by a turn (:func:`record`):
draining the spool into a healthy store, which the first append to reach one
starts (one drain at a time), and the private notice an old spool earns, sent
under its own bound. :func:`settle` awaits whatever is in flight.

Every spool line carries its item's ``source_key``. A drain takes the spool by
renaming it to a snapshot (:data:`SNAPSHOT_NAME`) and stores the snapshot's
lines with an ``INSERT`` that ignores a key already stored, in batches of
:data:`DRAIN_BATCH` lines per transaction with the store left free for
:data:`DRAIN_PAUSE_S` between them; it removes the snapshot only once every
line is committed. A drain cut off at any point leaves the snapshot, the next
drain finishes it before taking a new one (so at most one exists), and a
re-run adds each item once. Spool writes and the drain's rename are serialized
by one process-wide lock, :data:`_spool_lock`:

* a spool write takes it before it opens or creates the file and holds it
  through the write, the fsync and the close (and the directory fsync when it
  created the file); no descriptor is kept across acquisitions;
* a drain holds it only to rename the spool to the snapshot and fsync the
  directory. Storing the snapshot happens outside it, so a capture that has to
  spool never waits for a drain: it creates a fresh spool.

A line written before the rename is in the snapshot the drain stores; a line
written after it is in the fresh spool the next drain takes. None is removed
by a drain that did not store it. A
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
#: The spool as a drain took it: renamed under the spool lock, then stored
#: outside it. At most one exists; the next drain finishes a leftover one.
SNAPSHOT_NAME = "spool.draining.jsonl"

#: Seconds an append (or a drain batch) waits on a busy store before it
#: spools instead (or gives up until the next healthy append). The store's own
#: writers hold its lock for milliseconds — a capture is one row, a drain batch
#: a few hundred — so 50 ms rides out ordinary contention, and a lock held for
#: longer costs a turn at most this per captured item before the item spools.
CAPTURE_BUSY_TIMEOUT_S = 0.05

#: Spool lines a drain stores per transaction.
DRAIN_BATCH = 500

#: Seconds a drain leaves the store free between its batches. A capture
#: waiting on the store polls it at intervals of at most about 20 ms inside its
#: bound, so a gap this long lets it in; back-to-back batches would starve it.
DRAIN_PAUSE_S = 0.03

#: Seconds the detached old-spool notice may take before it counts as failed.
SPOOL_NOTICE_TIMEOUT_S = 60.0

#: A spool whose oldest line is older than this earns one private notice.
SPOOL_NOTICE_AFTER_S = 3600.0
SPOOL_NOTICE = (
    "Memory has not been able to reach its store for over an hour. What is "
    "said is being kept aside and will be stored once the store is back."
)

STORED = "stored"
SPOOLED = "spooled"

#: Serializes every spool write with a drain's rename of the spool.
_spool_lock = threading.Lock()
#: Serializes drains among themselves (the background drain, the export's).
_drain_lock = threading.Lock()


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


def summary(*, role: str, tier: str, text: str, engagement_id: str,
            procedural_epoch: str = "") -> Summary:
    """A :class:`Summary` stamped now, under a fresh ``event:`` key.
    ``procedural_epoch`` is a legacy column (the executor epoch is retired);
    Casa leaves it empty, and the field stays so spooled items replay."""
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


def snapshot_path(directory: str) -> str:
    return os.path.join(directory, SNAPSHOT_NAME)


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

    It does nothing else: draining a spool left by earlier failures is
    :func:`record`'s detached follow-up. ``None`` means not even the spool
    could be written."""
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
    return Receipt(item.source_key, STORED)


# --- the spool -------------------------------------------------------------

#: The fsync and rename calls, named so a test can observe them.
_fsync = os.fsync
_rename = os.rename


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


def _write_all(fd: int, data: bytes) -> None:
    """Write *data* whole to *fd* (named so a test can pause between a spool
    write's open and its write)."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


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
            _write_all(fd, line)
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
    """Store every spooled item. Blocking.

    A snapshot a cut-off drain left behind is finished first. Then, holding
    :data:`_spool_lock` only for this, the spool is renamed to the snapshot
    and the directory fsynced; spool writes after that create a fresh spool
    and never wait for the rest. The snapshot is stored outside the lock and
    removed once every line is committed.

    Returns the number of rows added. Raises when the store cannot take them;
    the snapshot is then left for the next drain, which adds nothing twice.
    Drains are serialized among themselves (:data:`_drain_lock`). The export
    drains through this too; no caller holds :data:`_spool_lock` when calling
    it."""
    directory = store.directory
    snapshot = snapshot_path(directory)
    with _drain_lock:
        added = 0
        if os.path.lexists(snapshot):
            added += _store_snapshot(store, snapshot)
        with _spool_lock:
            if not os.path.lexists(spool_path(directory)):
                return added
            _rename(spool_path(directory), snapshot)
            _fsync_dir(directory)
        return added + _store_snapshot(store, snapshot)


def drain_waiting(store: MemoryStore) -> int:
    """Drain the spool (or a cut-off drain's snapshot) when one is waiting;
    0 when none is. Blocking; raises as :func:`drain_spool` does. The writer
    calls this on each pass, so a spool is stored once the store is back even
    when no further capture comes to start a drain."""
    directory = store.directory
    if not (os.path.lexists(spool_path(directory))
            or os.path.lexists(snapshot_path(directory))):
        return 0
    return drain_spool(store)


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _store_snapshot(store: MemoryStore, snapshot: str) -> int:
    """Store an immutable snapshot's lines in batches, then remove it."""
    items, torn = _parse(_read(snapshot))
    added = refused = 0
    for start in range(0, len(items), DRAIN_BATCH):
        if start:
            time.sleep(DRAIN_PAUSE_S)
        with store.transaction(
                write=True, busy_timeout=CAPTURE_BUSY_TIMEOUT_S) as conn:
            for item in items[start:start + DRAIN_BATCH]:
                try:
                    added += _insert(conn, item)
                except sqlite3.IntegrityError:
                    refused += 1
    os.unlink(snapshot)
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
#: Detached follow-ups (drains, notices), referenced until they finish.
_background: set[asyncio.Task] = set()
#: The one drain that may run at a time.
_drain: list[asyncio.Task | None] = [None]


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


def _durable_write(store: MemoryStore, item: Event | Summary
                   ) -> tuple[Receipt | None, bool]:
    """The item's own durable write, and whether a spool now waits for a
    store that just took a write."""
    receipt = append(store, item)
    waiting = (receipt is not None and receipt.outcome == STORED
               and (os.path.lexists(spool_path(store.directory))
                    or os.path.lexists(snapshot_path(store.directory))))
    return receipt, waiting


async def record(item: Event | Summary) -> Receipt | None:
    """Capture *item*; the seams await this. Awaited is only the item's own
    durable write, off the event loop; a drain it makes due and a notice it
    makes due are detached. Returns the receipt, or ``None`` when nothing is
    wired or not even the spool took it. Never raises, except the caller's
    own cancellation."""
    store = active_store()
    if store is None:
        return None
    receipt, spool_waiting = await asyncio.to_thread(_durable_write, store, item)
    if spool_waiting:
        _start_drain(store)
    if receipt is not None and receipt.outcome == SPOOLED:
        _start_notice(receipt.spool_since)
    return receipt


def record_detached(item: Event | Summary) -> None:
    """Capture *item* without awaiting it, for a seam on a synchronous path
    that must not wait (the channel's post landing). :func:`settle` awaits
    it like any other detached write. Never raises."""
    async def _run() -> None:
        try:
            await record(item)
        except Exception:  # noqa: BLE001 — logged; capture never fails a send
            logger.exception("memory capture: %s not recorded",
                             item.source_key)
    try:
        _detach(_run())
    except Exception:  # noqa: BLE001 — no running loop: nothing captured
        logger.exception("memory capture: %s not scheduled", item.source_key)


def _detach(coro) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


def request_drain(store: MemoryStore) -> None:
    """Start the detached drain of a waiting spool, unless one is running.
    Never waits: for a caller on a turn's path that needs a spooled item
    stored, and answers without it now."""
    _start_drain(store)


def _start_drain(store: MemoryStore) -> None:
    running = _drain[0]
    if (running is not None and not running.done()
            and running.get_loop() is asyncio.get_running_loop()):
        return
    _drain[0] = _detach(_drain_detached(store))


async def _drain_detached(store: MemoryStore) -> None:
    try:
        await asyncio.to_thread(drain_spool, store)
    except Exception:  # noqa: BLE001 — the spool stays for the next one
        logger.warning("memory capture: spool drain failed; kept for the "
                       "next healthy append", exc_info=True)


def _start_notice(since: float | None) -> None:
    notify = _wiring["notify"]
    if (since is None or notify is None or since in _noticed
            or time.time() - since <= SPOOL_NOTICE_AFTER_S):
        return
    _noticed.add(since)
    _detach(_send_notice(notify, since))


async def _send_notice(notify, since: float) -> None:
    try:
        await asyncio.wait_for(notify(SPOOL_NOTICE), SPOOL_NOTICE_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — told on a later spooled append instead
        _noticed.discard(since)
        logger.warning("memory spool notice could not be sent", exc_info=True)


async def settle() -> None:
    """Wait for every detached drain and notice in flight."""
    loop = asyncio.get_running_loop()
    while pending := [t for t in _background if t.get_loop() is loop]:
        await asyncio.gather(*pending, return_exceptions=True)
