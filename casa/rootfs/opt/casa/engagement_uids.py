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


def scan_proc_uids(proc_root: str = "/proc") -> set[int]:
    """Every uid/gid ``>= UID_BASE`` a live process can use for DAC.

    Containment Stage 2 (S1 code-gate fix r2, both reviewers): the passwd/
    workspace/record evidence sources are all pruned at teardown, so a
    ``setsid``/double-fork descendant that ESCAPED the supervised process group
    (plugin-developer has Bash) survives ``ensure_service_down`` yet leaves NO
    filesystem or passwd trace. A lost counter would then reconstruct to base
    and reissue that survivor's uid → cross-engagement read. Reading the live
    ``/proc/<pid>/status`` uid/gid lines closes it: a uid a live process can use
    can never be reissued even with total filesystem + passwd evidence loss.

    S1 r4 (fsuid finding): Linux DAC checks the FILESYSTEM uid, not the real
    uid. ``/proc/<pid>/status`` reports ``Uid: <real> <effective> <saved>
    <fsuid>`` (and likewise ``Gid:``). A genuinely NON-root survivor can hold a
    LOW real uid but a HIGH fsuid — e.g. ``setpriv --ruid 65534 --euid 200000
    ... -- reader`` yields ``Uid: 65534 200000 200000 200000``, whose fsuid
    200000 passes DAC as engagement 200000's owner. Reading only the real uid
    would miss it and reissue 200000. So EVERY field on the ``Uid:`` line (and,
    defense-in-depth, the ``Gid:`` line — workspaces are uid-owned 0700, but a
    shared-gid DAC path would matter the same way) that is ``>= UID_BASE`` is
    folded into the returned set.

    Only values ``>= UID_BASE`` are returned (Casa's engagement range); the
    container's own root/service ids are irrelevant. Per-pid read failures (the
    process exited mid-scan, an unreadable ``status``) are SKIPPED — a vanished
    pid holds nothing reissuable. But an inability to scan ``proc_root`` AT ALL
    raises ``OSError`` (``/proc`` is always present in-container): the caller
    treats that as unconfirmable and refuses allocation fail-closed, rather than
    proceeding blind to what ids are live.
    """
    ids: set[int] = set()
    with os.scandir(proc_root) as it:   # raises OSError if proc_root is absent
        for entry in it:
            if not entry.name.isdigit():
                continue
            try:
                with open(
                    os.path.join(proc_root, entry.name, "status"),
                    "r", encoding="utf-8",
                ) as fh:
                    for line in fh:
                        if not (line.startswith("Uid:")
                                or line.startswith("Gid:")):
                            continue
                        # ``Uid:\t<real>\t<effective>\t<saved>\t<fsuid>`` — fold
                        # EVERY field (real/effective/saved/fsuid), since any of
                        # them can be the id a DAC check uses. Per-field parse is
                        # best-effort: a short/garbled line contributes whatever
                        # parses (never fails the pid).
                        for tok in line.split()[1:]:
                            try:
                                val = int(tok)
                            except ValueError:
                                continue
                            if val >= UID_BASE:
                                ids.add(val)
            except OSError:
                # Process exited between scandir and open, or status
                # unreadable — a vanished/opaque pid holds nothing reissuable.
                continue
    return ids


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
        proc_scanner=None,
    ) -> None:
        self._path = counter_path
        self._passwd = passwd_path
        self._group = group_path
        # S1 r2: the live-uid source (design fail-closed). ``None`` → resolve
        # the module-level :func:`scan_proc_uids` at reconstruct time (so a
        # test that monkeypatches ``engagement_uids.scan_proc_uids`` is honored
        # even for an allocator constructed earlier); an explicit callable is
        # used verbatim (direct test injection).
        self._proc_scanner = proc_scanner
        self._hw: int | None = None  # None until reconstruct() succeeds
        # S1 r4 — the allocator is either PROVEN-GOOD (a successful
        # reconstruct/refold whose high-water >= every evidenced/live uid, and
        # whose value is persisted) or POISONED (refuses ALL allocation). Any
        # failure in reconstruct/refold/_persist — scan failure OR persistence
        # failure — poisons it under the lock and converts to UidStateError, so
        # a later ``create()`` → ``allocate()`` can never hand out a uid against
        # a stale/unconfirmed high-water. A subsequent SUCCESSFUL reconstruct/
        # refold clears the poison.
        self._poisoned = False
        self._lock = threading.Lock()

    def _poison_locked(self) -> None:
        """Mark the allocator unusable (caller MUST hold ``self._lock``).
        ``allocate`` then refuses until a successful reconstruct/refold restores
        the proven-good state."""
        self._hw = None
        self._poisoned = True

    def reconstruct(self, known_uids: Iterable[int], dir_owner_uids: Iterable[int]) -> None:
        """Set the high-water mark to the max of every uid source.

        Sources (design §2): the persisted counter file (if any),
        ``UID_BASE - 1`` (the floor — allocate() always returns >= UID_BASE),
        *known_uids* (uids recorded on any record, incl. terminal/retained),
        *dir_owner_uids* (uids found by stat-ing on-disk engagement/control
        directories), the ``casa-eng-<uid>`` entries in this allocator's
        ``/etc/passwd`` (:func:`scan_passwd_uids`), AND the real uids of every
        LIVE process ``>= UID_BASE`` (:func:`scan_proc_uids`). Taking the max
        over ALL of them — not just liveness, and not just filesystem evidence —
        is deliberate: a uid that only shows up as a stale directory owner, only
        as a passwd entry, or ONLY as a live setsid-escaped survivor process
        (S1 r2) still must never be reissued.

        Missing vs corrupt counter (design §2 — align impl with design without
        breaking a genuine fresh install):
          - counter file MISSING: the persisted floor stays ``UID_BASE - 1``.
            If ANY evidence exists (a known uid, a dir owner, a passwd entry,
            or a live process uid) the reconstructed high-water is
            ``max(evidence)`` and is PERSISTED — a lost counter can never reset
            BELOW a still-evidenced uid. With ZERO evidence AND a clean /proc
            scan this is a genuine fresh install and the high-water stays
            ``UID_BASE - 1`` (first ``allocate`` → UID_BASE).
          - counter file present but MALFORMED (unparseable / wrong shape):
            :class:`UidStateError` — fail-closed, never silently reset to base.
          - the ``/proc`` live-uid scan CANNOT be performed (unexpected — /proc
            is always present in-container): :class:`UidStateError` — treat as
            unconfirmable and refuse allocation rather than reconstruct blind to
            which uids are live (same fail-closed posture as a malformed
            counter).

        S1 r2: folding live /proc uids closes the last reissue path — a
        ``setsid``/double-fork descendant that escaped the supervised group
        survives ``ensure_service_down`` and normal teardown (which prunes its
        record, workspace, control/outbox dirs AND its ``casa-eng`` passwd
        entry), so with counter loss the process would be the SOLE remaining
        evidence of its uid; /proc is where that evidence lives.
        """
        # S1 r4: the ENTIRE reconstruct — counter read, evidence scans, and the
        # high-water persist — runs under the lock inside one try. ANY failure
        # (malformed counter, unscannable /proc, a persist OSError) poisons the
        # allocator and surfaces as UidStateError; nothing escapes as a bare
        # OSError and no partial state is left proven-good.
        with self._lock:
            try:
                persisted = UID_BASE - 1
                if os.path.exists(self._path):
                    with open(self._path, "r", encoding="utf-8") as fh:
                        raw = json.load(fh)
                    persisted = int(raw["high_water"])

                passwd_uids = scan_passwd_uids(self._passwd)
                # Resolve the live-uid scanner at CALL time so a module-level
                # monkeypatch is honored (see __init__). A scan that cannot run
                # at all ⇒ fail-closed, never reconstruct blind to live uids.
                scanner = self._proc_scanner or scan_proc_uids
                proc_uids = scanner()

                candidates = [
                    persisted, UID_BASE - 1,
                    *known_uids, *dir_owner_uids, *passwd_uids, *proc_uids,
                ]
                self._hw = max(candidates)
                self._poisoned = False   # proven-good
                self._persist()          # persist failure re-poisons below
            except Exception as exc:  # noqa: BLE001 — any failure poisons
                self._poison_locked()
                raise UidStateError(
                    f"uid allocator reconstruct failed ({exc!r}) — refusing to "
                    "allocate against an unconfirmed high-water") from exc

    def refold_live_uids(self) -> None:
        """Re-fold live ``/proc`` uids into the high-water, AFTER boot replay's
        down-first sweep has confirmed every engagement service down.

        Containment Stage 2 (S1 code-gate fix r3 — TOCTOU): :meth:`reconstruct`
        runs at ``casa_core.main`` startup, BEFORE ``replay_undergoing_
        engagements`` drives every existing engagement service to a confirmed
        down. On a Stage-2 upgrade with a lost counter and legacy ROOT services
        still alive, a legacy engagement could ``setsid``/double-fork a non-root
        survivor under a not-yet-issued uid AFTER the boot scan ran but BEFORE
        its service was killed — the boot scan would miss it, and a subsequent
        backfill could reissue that live uid. Calling this once the sweep has
        confirmed every service down closes the window: no service can spawn a
        NEW survivor past that point, and this re-scan captures any that escaped
        earlier, so the high-water folds it in.

        Fail-closed (S1 r4): an unscannable ``/proc`` OR a persist failure
        poisons the allocator and raises :class:`UidStateError` — the caller
        refuses every resume needing a fresh allocation, AND every later
        ``allocate`` (normal ``create()`` included) refuses until a successful
        reconstruct/refold restores the proven-good state. A refold before a
        successful reconstruct likewise raises. Raising the high-water is
        monotonic and persisted; a scan that finds no live casa uid is a no-op.
        """
        with self._lock:
            try:
                if self._hw is None or self._poisoned:
                    raise UidStateError(
                        "refold_live_uids() before a successful reconstruct()")
                scanner = self._proc_scanner or scan_proc_uids
                proc_uids = scanner()
                if proc_uids:
                    new_hw = max(self._hw, max(proc_uids))
                    if new_hw != self._hw:
                        self._hw = new_hw
                        self._persist()   # persist failure re-poisons below
            except UidStateError:
                self._poison_locked()
                raise
            except Exception as exc:  # noqa: BLE001 — any failure poisons
                self._poison_locked()
                raise UidStateError(
                    f"live /proc uid refold failed ({exc!r}) — refusing to "
                    "allocate blind to which uids are held by live processes"
                ) from exc

    def allocate(self) -> int:
        """Return a fresh uid, persisting the new high-water before returning.

        Raises :class:`UidStateError` whenever the allocator is uninitialized
        (no successful :meth:`reconstruct`) OR poisoned by a prior
        reconstruct/refold/persist failure — the single fail-closed gate that
        makes EVERY allocation (legacy backfill in replay AND normal
        ``create()``) refuse against an unconfirmed high-water (S1 r4). A
        persist failure here also poisons, so the returned uid is always one
        that reached disk.
        """
        with self._lock:
            if self._hw is None or self._poisoned:
                raise UidStateError(
                    "allocate() called on an uninitialized or poisoned "
                    "allocator — refusing to allocate against an unconfirmed "
                    "high-water")
            try:
                self._hw += 1
                self._persist()  # persist BEFORE returning: crash-safe, never reuse
            except Exception as exc:  # noqa: BLE001 — persist failure poisons
                self._poison_locked()
                raise UidStateError(
                    f"uid counter persist failed during allocate ({exc!r}) — "
                    "allocator poisoned, refusing further allocation") from exc
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
