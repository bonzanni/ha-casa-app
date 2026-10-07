"""#1312: a repeated plugin delivery is the same delivery.

A plugin may give a delivered slot's deposit a ``key`` (its own identifier
for the post). After a PROVEN delivery the key is remembered per installed
plugin (its runtime segment) and operator; a later deposit carrying a
remembered key is not sent again, and its call gets the original delivery's
receipt. A plugin job cut between posting and recording the post therefore
retries without posting twice.

The memory is recorded synchronously at the instant a send is proven —
first the in-process map, which cannot fail, then the file, whose write
failure is logged and never undoes the delivery. It survives a restart when
that write succeeded. Entries are kept ``RETENTION_S`` and at most
``MAX_PER_SCOPE`` per (plugin, operator), oldest dropped first.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
RETENTION_S = 30 * 86400
MAX_PER_SCOPE = 500


def key_ok(value: Any) -> bool:
    return isinstance(value, str) and KEY_RE.fullmatch(value) is not None


def _int_ge1(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


# The receipt detail each delivered kind's proof records — exactly these keys.
_DETAIL_OK = {
    "operator_message": lambda d: set(d) == {"pages"} and _int_ge1(d["pages"]),
    "operator_file": lambda d: set(d) == {"kind"} and isinstance(d["kind"], str) and bool(d["kind"]),
    "operator_link": lambda d: d == {},
    "operator_proposal": lambda d: (set(d) == {"proposal_id", "buttons"}
                                    and isinstance(d["proposal_id"], str)
                                    and bool(d["proposal_id"]) and _int_ge1(d["buttons"])),
}


def _ident_ok(ident: Any) -> bool:
    try:
        parts = json.loads(ident)
    except Exception:  # noqa: BLE001
        return False
    return (isinstance(parts, list) and len(parts) == 3 and isinstance(parts[0], str)
            and isinstance(parts[1], int) and not isinstance(parts[1], bool)
            and key_ok(parts[2]))


def _entry_ok(entry: Any) -> bool:
    """An entry is used only when it is exactly a shape ``record`` writes: a
    known delivered kind, that kind's receipt detail and nothing else, a finite
    time, and a proposal's (scope, request id). Anything else is no entry."""
    if not isinstance(entry, dict):
        return False
    kind, detail = entry.get("kind"), entry.get("detail")
    if not isinstance(kind, str) or not isinstance(detail, dict):
        return False
    allowed = {"kind", "detail", "at"} | ({"proposal"} if kind == "operator_proposal" else set())
    if set(entry) != allowed or kind not in _DETAIL_OK:
        return False
    if not _DETAIL_OK[kind](detail):
        return False
    at = entry.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return False
    try:
        at_f = float(at)
    except (OverflowError, ValueError):
        return False
    if not math.isfinite(at_f):
        return False
    entry["at"] = at_f
    if kind == "operator_proposal":
        proposal = entry.get("proposal")
        return (isinstance(proposal, list) and len(proposal) == 2
                and all(isinstance(x, str) and x for x in proposal))
    return True


class DeliveryKeys:
    """The remembered keyed deliveries. One instance per process (``KEYS``)."""

    def __init__(self, path: str | None = None) -> None:
        self._path = path
        self._entries: dict[str, dict] = {}
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @staticmethod
    def _id(seg: str, operator_id: int, key: str) -> str:
        return json.dumps([seg, int(operator_id), key])

    # -- persistence ---------------------------------------------------------
    def load(self, path: str) -> None:
        """Read the file at *path*. Missing: empty, silently. Unreadable or
        corrupt: empty, one WARNING. A malformed entry is dropped (one WARNING
        for all of them). Never raises."""
        self._path = path
        self._entries = {}
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001 — a damaged file starts empty
            logger.warning("delivery keys unreadable (%s); starting empty",
                           type(exc).__name__)
            return
        if not isinstance(raw, dict):
            logger.warning("delivery keys malformed; starting empty")
            return
        dropped = 0
        for ident, entry in raw.items():
            try:
                ok = isinstance(ident, str) and _ident_ok(ident) and _entry_ok(entry)
            except Exception:  # noqa: BLE001 — one bad entry never fails the load
                ok = False
            if ok:
                self._entries[ident] = entry
            else:
                dropped += 1
        # the bounds hold for what was read too: expired out, then the cap
        now = time.time()
        for ident in [i for i, e in self._entries.items() if now - e["at"] > RETENTION_S]:
            del self._entries[ident]
        for seg, operator_id in {tuple(json.loads(i)[:2]) for i in self._entries}:
            self._prune(seg, operator_id, now)
        if dropped:
            logger.warning("delivery keys: %d malformed entr%s dropped", dropped,
                           "y" if dropped == 1 else "ies")

    def _write(self) -> None:
        if not self._path:
            return
        try:
            from atomic_io import atomic_write_json
            atomic_write_json(self._path, self._entries, indent=None, mode=0o600)
        except Exception as exc:  # noqa: BLE001 — never undoes a proven delivery
            logger.warning("delivery keys not persisted (%s); the memory holds until "
                           "a restart", type(exc).__name__)

    # -- the memory ---------------------------------------------------------
    def lookup(self, seg: str, operator_id: int, key: str) -> dict | None:
        """The remembered delivery, or None. An entry that cannot be used is
        no entry: it is dropped, and the deposit is delivered fresh."""
        ident = self._id(seg, operator_id, key)
        entry = self._entries.get(ident)
        if entry is None:
            return None
        try:
            if not _entry_ok(entry):
                raise ValueError("malformed")
            if time.time() - entry["at"] > RETENTION_S:
                return None
            return entry
        except Exception:  # noqa: BLE001 — an unusable memory never blocks a post
            logger.warning("delivery keys: an unusable entry was dropped")
            self._entries.pop(ident, None)
            return None

    def record(self, seg: str, operator_id: int, key: str, *, kind: str, detail: dict,
               proposal: tuple[str, str] | None = None) -> None:
        """Synchronous: the map first, then the file (whose failure is logged)."""
        now = time.time()
        entry: dict[str, Any] = {"kind": kind, "detail": dict(detail), "at": now}
        if proposal is not None:
            entry["proposal"] = list(proposal)
        ident = self._id(seg, operator_id, key)
        self._entries.pop(ident, None)
        self._entries[ident] = entry
        self._prune(seg, int(operator_id), now)
        self._write()

    def _prune(self, seg: str, operator_id: int, now: float) -> None:
        for ident in [i for i, e in self._entries.items() if now - e["at"] > RETENTION_S]:
            del self._entries[ident]
        prefix = json.dumps([seg, operator_id])[:-1] + ","
        scoped = [i for i in self._entries if i.startswith(prefix)]
        for ident in sorted(scoped, key=lambda i: self._entries[i]["at"])[:-MAX_PER_SCOPE]:
            del self._entries[ident]

    # -- one delivery per key at a time --------------------------------------
    @contextlib.asynccontextmanager
    async def hold(self, seg: str, operator_id: int, key: str):
        """Serialise deliveries of one key; the lock is retired when its last
        holder or waiter leaves."""
        ident = self._id(seg, operator_id, key)
        lock, users = self._locks.get(ident, (None, 0))
        if lock is None:
            lock = asyncio.Lock()
        self._locks[ident] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock_now, users_now = self._locks[ident]
            if users_now <= 1:
                del self._locks[ident]
            else:
                self._locks[ident] = (lock_now, users_now - 1)


KEYS = DeliveryKeys()


def init_store(path: str) -> None:
    """Boot: load the remembered keys from *path* (private, 0600)."""
    KEYS.load(path)
