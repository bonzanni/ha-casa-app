"""Run confirmation: which SDK transcripts a deleter may remove.

Casa's live capture (:mod:`memory.capture`) is the only source of memory, and
nothing here reads a transcript. A transcript is kept until every run that
could capture from the conversation in it has confirmed that its captures are
durable; after that it follows retention (:func:`retention`).

**A run's row.** ``transcript_runs`` holds ids and times, never content. Every
run that can capture registers a row before its first capture
(:meth:`Run.start`): an eligible resident turn (``resident``), an engagement
turn of a memory-writing specialist (``engagement``), every delegated run
(``delegation``) and an engagement's finalize summaries (``summary``). The row
carries this process's :data:`BOOT_ID`, the run's working directory (the key
every deleter resolves the transcript folder from) and, once known, its SDK
session (:meth:`Run.name`), named before the run's first capture from that
session and before any pointer or record a deleter follows names it.

**Confirmed** means every capture the run made returned a receipt (stored, or
spooled and fsynced). Each capture is awaited through :meth:`Run.kept`; one
that failed, or that a cancellation interrupted, leaves the run unconfirmed.
:meth:`Run.close`, from the run's outermost ``finally``, confirms it
otherwise. A run whose role captures nothing registers already confirmed. A
row left unconfirmed by an earlier process is *unresolved*: the meter counts
it, its transcript is kept until a wipe, and nothing resolves it.

**The gate.** Every deleter asks :func:`blocks_deletion` (through
:func:`blocked`) at the delete and removes the rows that named what it deleted
afterwards (:func:`forget`). A transcript is blocked while an unconfirmed row
names its session; a whole-directory removal is blocked by any unconfirmed row
naming that directory. An engagement's ``summary`` run registers one row per
session the engagement ran under (or one naming only its directory when it
names none), so it keeps that engagement's own transcripts and never a
sibling's in a shared folder.

Registration and naming are awaited by the run (bounded like a capture, so a
busy store never holds a turn: a failure is logged and the run continues,
unprotected). Confirmation and the end time are written off the run's path, in
detached tasks (:func:`settle` awaits them). Blocking store work runs in a
worker thread.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable
from datetime import datetime, timedelta, timezone
from typing import Any

from memory.store import MemoryStore

logger = logging.getLogger(__name__)

RESIDENT = "resident"
ENGAGEMENT = "engagement"
DELEGATION = "delegation"
SUMMARY = "summary"
KINDS = (RESIDENT, ENGAGEMENT, DELEGATION, SUMMARY)

#: This process's id. A row left unconfirmed under another id is unresolved:
#: no run outlives the process that started it.
BOOT_ID = uuid.uuid4().hex

#: Seconds a run's registration or naming waits on a busy store; the same
#: bound a capture's store attempt has.
BUSY_TIMEOUT_S = 0.05

RETENTION_ENV = "TRANSCRIPT_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 30


def retention_days() -> int:
    """Days a transcript is kept once its session can no longer resume and
    every run in it is confirmed (``TRANSCRIPT_RETENTION_DAYS``, read at each
    use; an unset, malformed or non-positive value means the default)."""
    raw = os.environ.get(RETENTION_ENV, "")
    try:
        days = int(raw)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return days if days >= 1 else DEFAULT_RETENTION_DAYS


def retention() -> timedelta:
    return timedelta(days=retention_days())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def directory_key(directory: str | os.PathLike | None) -> str | None:
    """The one form a row and a deleter compare a directory in."""
    if directory is None or str(directory) == "":
        return None
    return os.path.normpath(str(directory))


# --- the store (blocking) ---------------------------------------------------

def register(store: MemoryStore, *, kind: str, role: str,
             directory: str | None, session_id: str | None = None,
             confirmed: bool = False, boot_id: str = BOOT_ID,
             busy_timeout: float = BUSY_TIMEOUT_S) -> str:
    """Insert a run's row and return its id. Raises when the store cannot
    take it."""
    if kind not in KINDS:
        raise ValueError(f"unknown run kind {kind!r}")
    run_id = uuid.uuid4().hex
    now = _now()
    with store.transaction(write=True, busy_timeout=busy_timeout) as conn:
        conn.execute(
            "INSERT INTO transcript_runs (run_id, kind, role, directory, "
            "session_id, boot_id, started_at, confirmed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, kind, role, directory_key(directory), session_id,
             boot_id, now, now if confirmed else None))
    return run_id


def name_session(store: MemoryStore, run_id: str, session_id: str, *,
                 busy_timeout: float = BUSY_TIMEOUT_S) -> None:
    with store.transaction(write=True, busy_timeout=busy_timeout) as conn:
        conn.execute("UPDATE transcript_runs SET session_id = ? "
                     "WHERE run_id = ?", (session_id, run_id))


def confirm(store: MemoryStore, *run_ids: str) -> None:
    now = _now()
    with store.transaction(write=True) as conn:
        for run_id in run_ids:
            conn.execute("UPDATE transcript_runs SET confirmed_at = ? "
                         "WHERE run_id = ? AND confirmed_at IS NULL",
                         (now, run_id))


def end(store: MemoryStore, *run_ids: str) -> None:
    now = _now()
    with store.transaction(write=True) as conn:
        for run_id in run_ids:
            conn.execute("UPDATE transcript_runs SET ended_at = ? "
                         "WHERE run_id = ? AND ended_at IS NULL",
                         (now, run_id))


def blocks_deletion(store: MemoryStore, *, directory: str | None,
                    session_id: str | None = None) -> bool:
    """Whether an unconfirmed run forbids deleting *session_id*'s transcript
    in *directory* (or, with no session, the whole *directory*)."""
    directory = directory_key(directory)
    with store.transaction() as conn:
        if session_id is None:
            row = conn.execute(
                "SELECT 1 FROM transcript_runs WHERE confirmed_at IS NULL "
                "AND directory = ? LIMIT 1", (directory,)).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM transcript_runs WHERE confirmed_at IS NULL "
                "AND session_id = ? LIMIT 1", (session_id,)).fetchone()
    return row is not None


def forget(store: MemoryStore, *, directory: str | None,
           session_id: str | None = None) -> int:
    """Remove the confirmed rows that named a transcript a deleter has just
    removed: *session_id*'s, or every one naming *directory* when the whole
    directory went. Returns the number removed."""
    with store.transaction(write=True) as conn:
        if session_id is None:
            cur = conn.execute(
                "DELETE FROM transcript_runs WHERE confirmed_at IS NOT NULL "
                "AND directory = ?", (directory_key(directory),))
        else:
            cur = conn.execute(
                "DELETE FROM transcript_runs WHERE confirmed_at IS NOT NULL "
                "AND session_id = ?", (session_id,))
    return cur.rowcount


def unresolved_count(conn, boot_id: str = BOOT_ID) -> int:
    """Rows an earlier process left unconfirmed, read on *conn* (the meter's
    read transaction)."""
    return conn.execute(
        "SELECT count(*) FROM transcript_runs WHERE confirmed_at IS NULL "
        "AND boot_id != ?", (boot_id,)).fetchone()[0]


def delegated_due(store: MemoryStore, *, now: datetime,
                  boot_id: str = BOOT_ID) -> list[tuple[str, str, str | None]]:
    """``(run_id, session_id, directory)`` of every delegated run whose
    retention has passed: counted from its end, or from its start for a row
    an earlier process never ended. Whether it may go is the gate's."""
    cutoff = (now - retention()).isoformat(timespec="microseconds")
    with store.transaction() as conn:
        rows = conn.execute(
            "SELECT run_id, session_id, directory FROM transcript_runs "
            "WHERE kind = ? AND session_id IS NOT NULL AND ("
            " (ended_at IS NOT NULL AND ended_at <= ?) OR "
            " (ended_at IS NULL AND boot_id != ? AND started_at <= ?))",
            (DELEGATION, cutoff, boot_id, cutoff)).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def drop(store: MemoryStore, run_id: str) -> None:
    with store.transaction(write=True) as conn:
        conn.execute("DELETE FROM transcript_runs WHERE run_id = ?", (run_id,))


def prune_confirmed(store: MemoryStore, *, now: datetime) -> int:
    """Drop confirmed rows other than delegated ones once their retention
    has passed. A confirmed row blocks nothing; this only bounds the table
    for sessions no deleter reaches (a resident session a later one
    replaced, a summary in a shared folder)."""
    cutoff = (now - retention()).isoformat(timespec="microseconds")
    with store.transaction(write=True) as conn:
        cur = conn.execute(
            "DELETE FROM transcript_runs WHERE kind != ? "
            "AND confirmed_at IS NOT NULL AND confirmed_at <= ?",
            (DELEGATION, cutoff))
    return cur.rowcount


# --- the live seams -----------------------------------------------------------

def _store() -> MemoryStore | None:
    from memory import capture
    return capture.active_store()


def blocked(*, directory: str | None, session_id: str | None = None) -> bool:
    """The gate every deleter asks at the delete. Blocking. With no store
    wired nothing was registered, so nothing blocks; a store that is wired
    but cannot answer blocks (the transcript waits for a later pass)."""
    store = _store()
    if store is None:
        return False
    try:
        return blocks_deletion(store, directory=directory,
                               session_id=session_id)
    except Exception:  # noqa: BLE001 — unanswered means kept
        logger.warning("transcript gate unreadable; keeping %s",
                       session_id or directory, exc_info=True)
        return True


def forget_deleted(*, directory: str | None,
                   session_id: str | None = None) -> None:
    """:func:`forget` on the wired store, for a deleter. Blocking; a failure
    is logged (the rows then name a missing file, which the next pass treats
    as done)."""
    store = _store()
    if store is None:
        return
    try:
        forget(store, directory=directory, session_id=session_id)
    except Exception:  # noqa: BLE001
        logger.warning("transcript run rows not removed for %s",
                       session_id or directory, exc_info=True)


def session_of(sdk_msg: Any) -> str | None:
    """The SDK session a stream message names: the init system message's
    ``data["session_id"]``, else the message's own ``session_id``."""
    if getattr(sdk_msg, "subtype", None) == "init":
        data = getattr(sdk_msg, "data", None)
        sid = data.get("session_id") if isinstance(data, dict) else None
    else:
        sid = getattr(sdk_msg, "session_id", None)
    return sid if isinstance(sid, str) and sid else None


#: Detached confirmation and end writes, referenced until they finish.
_background: set[asyncio.Task] = set()


def _detach(fn, *args) -> None:
    async def write() -> None:
        try:
            await asyncio.to_thread(fn, *args)
        except Exception:  # noqa: BLE001 — the run then stays unconfirmed
            logger.warning("transcript run write %s failed", fn.__name__,
                           exc_info=True)
    try:
        task = asyncio.get_running_loop().create_task(write())
    except RuntimeError:
        return
    _background.add(task)
    task.add_done_callback(_background.discard)


async def settle() -> None:
    """Wait for every detached run write in flight."""
    loop = asyncio.get_running_loop()
    while pending := [t for t in _background if t.get_loop() is loop]:
        await asyncio.gather(*pending, return_exceptions=True)


class Run:
    """One run's row, as its seam drives it."""

    def __init__(self, kind: str, role: str, directory: str | None,
                 session_id: str | None) -> None:
        self.kind = kind
        self.role = role
        self.directory = directory_key(directory)
        self.session_id = session_id
        self.run_ids: list[str] = []
        self._store: MemoryStore | None = None
        self._open = 0
        self._failed = False
        self._closed = False

    @property
    def run_id(self) -> str | None:
        return self.run_ids[0] if self.run_ids else None

    @classmethod
    async def start(cls, kind: str, *, role: str, directory: str | None,
                    session_id: str | None = None,
                    sessions: tuple[str, ...] = (),
                    confirmed: bool = False) -> "Run":
        """Register a run before its first capture. A run whose role
        captures nothing (*confirmed*) registers confirmed. A run that
        already spans several sessions (an engagement's finalize, over the
        sessions the engagement ran under) gives them as *sessions*: one row
        each, one confirmation. A registration that fails is logged once and
        the run goes on unprotected."""
        run = cls(kind, role, directory, session_id)
        store = _store()
        if store is None:
            return run
        try:
            run.run_ids = [await asyncio.to_thread(
                register, store, kind=kind, role=role,
                directory=run.directory, session_id=sid,
                confirmed=confirmed) for sid in (sessions or (session_id,))]
        except Exception as exc:  # noqa: BLE001 — never blocks the run
            logger.warning("%s run of %s not registered (%s: %s); its "
                           "transcript is not protected", kind, role,
                           type(exc).__name__, exc)
            return run
        run._store = store
        run._closed = confirmed
        return run

    async def name(self, session_id: str | None) -> None:
        """Name (or move to) the session the run's next captures come from.
        Awaited before that capture."""
        if (not session_id or session_id == self.session_id):
            return
        self.session_id = session_id
        if self.run_id is None or self._store is None:
            return
        try:
            await asyncio.to_thread(name_session, self._store, self.run_id,
                                    session_id)
        except Exception as exc:  # noqa: BLE001 — never blocks the run
            logger.warning("%s run %s not named %s (%s: %s)", self.kind,
                           self.run_id[:8], session_id, type(exc).__name__,
                           exc)

    async def kept(self, capture: Awaitable[bool]) -> bool:
        """Await one capture the run owes. It is kept when it returned a
        receipt or the live rules captured nothing; otherwise (or when a
        cancellation interrupts it) the run stays unconfirmed."""
        self._open += 1
        ok = await capture
        self._open -= 1
        if not ok:
            self._failed = True
        return ok

    def close(self) -> None:
        """From the run's outermost ``finally``: confirm it when every
        capture it made was kept. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self.run_id is None or self._store is None:
            return
        if self._open or self._failed:
            logger.warning(
                "%s run %s of %s left unconfirmed: a capture %s; its "
                "transcript is kept", self.kind, self.run_id[:8], self.role,
                "was interrupted" if self._open else "was not kept")
            return
        _detach(confirm, self._store, *self.run_ids)

    def end(self) -> None:
        """Record when a delegated run's client exited (its retention
        clock)."""
        if self.run_ids and self._store is not None:
            _detach(end, self._store, *self.run_ids)


def unregistered(kind: str, *, role: str, directory: str | None = None,
                 session_id: str | None = None) -> Run:
    """A run with no row: what a seam holds when it has nothing to register.
    Its captures and close do nothing to the store."""
    run = Run(kind, role, directory, session_id)
    run._closed = True
    return run


# --- the delegated retention pass ----------------------------------------------

def _sdk_delete(session_id: str, directory: str | None) -> None:
    from claude_agent_sdk import delete_session
    delete_session(session_id, directory)


def retention_pass(store: MemoryStore, *, now: datetime | None = None,
                   delete=_sdk_delete) -> dict[str, int]:
    """Delete every delegated transcript whose retention has passed, where
    the gate passes (its run is confirmed): only the session a row names, in
    that row's directory, never from a folder listing. Then drop confirmed
    rows other kinds no longer need. Blocking."""
    now = now or datetime.now(timezone.utc)
    counts = {"deleted": 0, "absent": 0, "kept": 0, "errors": 0}
    for run_id, sid, directory in delegated_due(store, now=now):
        if blocks_deletion(store, directory=directory, session_id=sid):
            counts["kept"] += 1
            continue
        try:
            delete(sid, directory)
        except FileNotFoundError:
            counts["absent"] += 1
        except Exception as exc:  # noqa: BLE001 — retried on the next pass
            counts["errors"] += 1
            logger.warning("delegated transcript %s not deleted: %s", sid,
                           exc)
            continue
        else:
            counts["deleted"] += 1
        drop(store, run_id)
    prune_confirmed(store, now=now)
    if counts["deleted"] or counts["errors"]:
        logger.info("delegated transcript retention: %s", counts)
    return counts
