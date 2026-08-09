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


# ---------------------------------------------------------------------------
# Module-level passwd/group identity helpers (containment stage 2, Task 8).
# ---------------------------------------------------------------------------
# Lifted out of UidAllocator so provisioning/teardown (drivers/workspace.py)
# can append/remove a uid's NSS identity without needing an allocator
# instance in hand — the allocator methods below now delegate to these.


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


def _remove_prefix(path: str, prefix: str) -> None:
    with open(path, "r", encoding="utf-8") as fh:
        keep = [line for line in fh if not line.startswith(prefix)]
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(keep)


def ensure_identity(
    uid: int, home: str, *,
    passwd_path: str = "/etc/passwd", group_path: str = "/etc/group",
) -> None:
    """Append passwd/group entries for *uid* if not already present.

    Idempotent: a second call for the same uid is a no-op.
    """
    prefix = f"casa-eng-{uid}:"
    passwd_line = f"casa-eng-{uid}:x:{uid}:{uid}::{home}:/usr/sbin/nologin\n"
    group_line = f"casa-eng-{uid}:x:{uid}:\n"
    _append_if_absent(passwd_path, prefix, passwd_line)
    _append_if_absent(group_path, prefix, group_line)


def prune_identity(
    uid: int, *,
    passwd_path: str = "/etc/passwd", group_path: str = "/etc/group",
) -> None:
    """Remove the passwd/group entries for *uid*, if present."""
    prefix = f"casa-eng-{uid}:"
    _remove_prefix(passwd_path, prefix)
    _remove_prefix(group_path, prefix)


def scan_passwd_uids(passwd_path: str = "/etc/passwd") -> list[int]:
    """Every uid that appears in a ``casa-eng-<uid>:...`` entry in *passwd_path*.

    Containment Stage 2 (S1 code-gate fix, design §2): a ``casa-eng`` passwd
    entry is one of the four evidence sources the high-water reconstruction
    must fold in. It uniquely PRESERVES the high-water even when the record and
    the workspace/control dirs have both been pruned (they are pruned at
    teardown; the passwd entry is pruned at teardown too, but a *detached
    survivor* — a process still holding the uid after its record went — keeps
    its passwd entry until :func:`prune_identity` runs). Scanning it stops a
    lost counter file from reissuing a uid still evidenced in ``/etc/passwd``.

    The uid is read from the passwd RECORD's uid field (3rd colon-field), not
    parsed back out of the name, so a hand-mangled name can't spoof it. A
    missing file, or a line that does not parse, contributes nothing (the
    reconstruction floor still applies).
    """
    uids: list[int] = []
    try:
        with open(passwd_path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return uids
    for line in lines:
        if not line.startswith("casa-eng-"):
            continue
        fields = line.split(":")
        if len(fields) < 3:
            continue
        try:
            uids.append(int(fields[2]))
        except ValueError:
            continue
    return uids


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

        Sources (design §2): the persisted counter file (if any),
        ``UID_BASE - 1`` (the floor — allocate() always returns >= UID_BASE),
        *known_uids* (uids recorded on any record, incl. terminal/retained),
        *dir_owner_uids* (uids found by stat-ing on-disk engagement/control
        directories), and the ``casa-eng-<uid>`` entries in this allocator's
        ``/etc/passwd`` (:func:`scan_passwd_uids`). Taking the max over ALL of
        them — not just liveness — is deliberate: a uid that only shows up as a
        stale directory owner, or only as a passwd entry for a detached
        survivor whose record and workspace were both pruned, still must never
        be reissued.

        Missing vs corrupt counter (design §2 — align impl with design without
        breaking a genuine fresh install):
          - counter file MISSING: the persisted floor stays ``UID_BASE - 1``.
            If ANY evidence exists (a known uid, a dir owner, or a passwd
            entry) the reconstructed high-water is ``max(evidence)`` and is
            PERSISTED — a lost counter can never reset BELOW a still-evidenced
            uid. With ZERO evidence this is a genuine fresh install and the
            high-water stays ``UID_BASE - 1`` (first ``allocate`` → UID_BASE).
          - counter file present but MALFORMED (unparseable / wrong shape):
            :class:`UidStateError` — fail-closed, never silently reset to base.

        Residual (documented): a detached survivor whose record AND workspace
        AND ``casa-eng`` passwd entry are ALL gone is unevidenced — but that
        requires a setsid-escaping descendant (the run template creates none)
        plus a full triple-prune, so no reachable path reissues its uid.
        """
        persisted = UID_BASE - 1
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                persisted = int(raw["high_water"])
            except (ValueError, KeyError, OSError, TypeError, json.JSONDecodeError) as exc:
                raise UidStateError(f"counter file {self._path} unreadable: {exc}") from exc

        passwd_uids = scan_passwd_uids(self._passwd)
        candidates = [
            persisted, UID_BASE - 1,
            *known_uids, *dir_owner_uids, *passwd_uids,
        ]
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

        Idempotent: a second call for the same uid is a no-op. Delegates to
        the module-level :func:`ensure_identity` (Task 8) bound to this
        allocator's own passwd/group paths.
        """
        ensure_identity(uid, home, passwd_path=self._passwd, group_path=self._group)

    def prune_identity(self, uid: int) -> None:
        """Remove the passwd/group entries for *uid*, if present."""
        prune_identity(uid, passwd_path=self._passwd, group_path=self._group)
