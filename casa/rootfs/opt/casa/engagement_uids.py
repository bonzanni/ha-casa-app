"""Persistent, monotonic, never-reused uid allocator for per-engagement isolation.

Casa containment Stage 2 gives each engagement its own OS uid so a sandboxed
process cannot act as another engagement's identity. The single invariant
this module exists to hold: **a uid, once handed out, is never handed out
again** — not across process restarts, not across a corrupted or missing
counter file, and not when a uid shows up "in the wild" (an existing
``/etc/passwd`` entry or a directory owner) that the counter never recorded.

The counter is a tiny JSON file (``{"high_water": <int>}``) written with
:func:`atomic_io.atomic_write_json` so a crash mid-write can never leave a
torn file, and the new high-water is persisted *before* :meth:`allocate`
returns — so a crash immediately after allocation still can't reuse the uid
on the next boot.

Fail-closed by construction: a missing counter file is fine (first boot), but
a *malformed* one raises :class:`UidStateError` rather than silently
resetting to ``UID_BASE`` (which would eventually reuse a live uid). Calling
:meth:`allocate` before :meth:`reconstruct` also raises — there is no
implicit base state to allocate from.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Iterable

from atomic_io import atomic_write_json

UID_BASE = 200000
UNALLOCATED_UID = -1


def owner_uid_or_none(uid: int) -> int | None:
    """Containment stage 2, Task 5: the ``owner_uid`` argument ``safe_fs``
    expects, derived from an ``EngagementRecord.allocated_uid``.

    Returns *uid* unchanged only when it is a REAL allocated uid
    (``>= UID_BASE``); returns ``None`` for the ``UNALLOCATED_UID`` sentinel
    (specialist / legacy record — its workspace was never uid-chowned, so
    there is no uid to check ownership against) and for any other bogus
    value (e.g. ``0``, which must never be passed to ``safe_fs`` as an
    owner-uid check — that would mean "must be owned by root", defeating
    the whole point of the check for a record with no real uid)."""
    return uid if uid >= UID_BASE else None


class UidStateError(Exception):
    """Raised when the uid counter is missing invariants it must hold.

    Covers: a persisted counter file that exists but cannot be parsed as the
    expected ``{"high_water": <int>}`` shape, and a call to :meth:`allocate`
    before :meth:`reconstruct` has established a high-water mark.
    """


class UidAllocator:
    """Hands out uids from a persistent, monotonic high-water mark.

    Must be seeded with :meth:`reconstruct` before :meth:`allocate` is
    callable — this forces callers to fold in every uid source (persisted
    counter, known live uids, directory owners found on disk) before the
    allocator can hand out a new one, so a restart can never step backwards.
    """

    def __init__(
        self,
        counter_path: str,
        passwd_path: str = "/etc/passwd",
        group_path: str = "/etc/group",
    ) -> None:
        self._path = counter_path
        self._passwd = passwd_path
        self._group = group_path
        self._hw: int | None = None  # None until reconstruct() succeeds
        self._lock = threading.Lock()

    def reconstruct(self, known_uids: Iterable[int], dir_owner_uids: Iterable[int]) -> None:
        """Set the high-water mark to the max of every uid source.

        Sources: the persisted counter file (if any), ``UID_BASE - 1`` (the
        floor — allocate() always returns >= UID_BASE), *known_uids* (e.g.
        uids already recorded live), and *dir_owner_uids* (e.g. uids found by
        stat-ing on-disk engagement directories). Taking the max over all of
        them — not just liveness — is deliberate: a uid that only shows up as
        a stale directory owner still must never be reissued.

        Raises :class:`UidStateError` if the counter file exists but is not
        valid ``{"high_water": <int>}`` JSON.
        """
        persisted = UID_BASE - 1
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                persisted = int(raw["high_water"])
            except (ValueError, KeyError, OSError, TypeError, json.JSONDecodeError) as exc:
                raise UidStateError(f"counter file {self._path} unreadable: {exc}") from exc

        candidates = [persisted, UID_BASE - 1, *known_uids, *dir_owner_uids]
        with self._lock:
            self._hw = max(candidates)
            self._persist()

    def allocate(self) -> int:
        """Return a fresh uid, persisting the new high-water before returning.

        Raises :class:`UidStateError` if :meth:`reconstruct` has not run.
        """
        with self._lock:
            if self._hw is None:
                raise UidStateError("allocate() called before reconstruct()")
            self._hw += 1
            self._persist()  # persist BEFORE returning: crash-safe, never reuse
            return self._hw

    def _persist(self) -> None:
        atomic_write_json(self._path, {"high_water": self._hw}, mode=0o600)

    def ensure_identity(self, uid: int, home: str) -> None:
        """Append passwd/group entries for *uid* if not already present.

        Idempotent: a second call for the same uid is a no-op.
        """
        prefix = f"casa-eng-{uid}:"
        passwd_line = f"casa-eng-{uid}:x:{uid}:{uid}::{home}:/usr/sbin/nologin\n"
        group_line = f"casa-eng-{uid}:x:{uid}:\n"
        self._append_if_absent(self._passwd, prefix, passwd_line)
        self._append_if_absent(self._group, prefix, group_line)

    def prune_identity(self, uid: int) -> None:
        """Remove the passwd/group entries for *uid*, if present."""
        prefix = f"casa-eng-{uid}:"
        self._remove_prefix(self._passwd, prefix)
        self._remove_prefix(self._group, prefix)

    @staticmethod
    def _append_if_absent(path: str, prefix: str, line: str) -> None:
        with open(path, "r", encoding="utf-8") as fh:
            existing = fh.read()
        if any(l.startswith(prefix) for l in existing.splitlines()):
            return
        # A file missing its trailing newline (nothing guarantees /etc/passwd
        # or /etc/group end in one) would otherwise merge the previous last
        # entry with the new one into a single corrupted line — insert the
        # separator ourselves rather than trusting "a" mode to append cleanly.
        needs_separator = bool(existing) and not existing.endswith("\n")
        with open(path, "a", encoding="utf-8") as fh:
            if needs_separator:
                fh.write("\n")
            fh.write(line)

    @staticmethod
    def _remove_prefix(path: str, prefix: str) -> None:
        with open(path, "r", encoding="utf-8") as fh:
            keep = [line for line in fh if not line.startswith(prefix)]
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(keep)
