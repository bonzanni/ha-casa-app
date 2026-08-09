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


def _fold_status_ids(status_path: str, ids: set[int]) -> None:
    """Fold every ``Uid:``/``Gid:`` field ``>= UID_BASE`` from one
    ``/proc/.../status`` file into *ids*.

    ENOENT (the thread/process exited between enumeration and open) is a no-op —
    a vanished task holds nothing reissuable. ANY OTHER ``OSError`` (EACCES, EIO,
    a mid-read failure) PROPAGATES: an unreadable-but-present status is
    unconfirmable, and the caller must fail closed rather than under-count live
    ids. Per-field parse is best-effort (a short/garbled line contributes
    whatever integers parse)."""
    try:
        fh = open(status_path, "r", encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return  # confirmed exit (ENOENT / ESRCH) — nothing to fold
    with fh:
        for line in fh:
            if not (line.startswith("Uid:") or line.startswith("Gid:")):
                continue
            # ``Uid:\t<real>\t<effective>\t<saved>\t<fsuid>`` — fold EVERY field
            # (real/effective/saved/fsuid), since any can be the id a DAC check
            # uses (S1 r4 fsuid finding).
            for tok in line.split()[1:]:
                try:
                    val = int(tok)
                except ValueError:
                    continue
                if val >= UID_BASE:
                    ids.add(val)


def scan_proc_uids(proc_root: str = "/proc") -> set[int]:
    """Every uid/gid ``>= UID_BASE`` a live THREAD can use for DAC.

    Defense-in-depth for the uid-reuse class (the load-bearing guarantee is the
    durable, monotonic high-water — see :meth:`UidAllocator.reconstruct`). A
    ``setsid``/double-fork descendant that ESCAPED the supervised process group
    survives ``ensure_service_down`` yet may leave no filesystem/passwd trace;
    reading the live ``/proc`` uid/gid lines folds its id into the high-water so
    it can never be reissued.

    S1 r5 (per-thread): ``/proc/<pid>`` lists only the thread-group LEADER, so a
    per-thread ``setresuid``/``setfsuid`` worker (a non-leader ``<tid>``) is
    invisible at the process level. This enumerates every
    ``/proc/<pid>/task/<tid>/status`` so a worker thread that dropped to an
    engagement uid is seen too.

    S1 r4 (fsuid): DAC checks the FILESYSTEM uid, not the real uid, so EVERY
    field of the ``Uid:`` line (real/effective/saved/fsuid) and the ``Gid:``
    line (defense-in-depth) that is ``>= UID_BASE`` is folded.

    Fail-closed: a confirmed thread/process EXIT (ENOENT) is skipped, but an
    inability to scan ``proc_root`` at all, or an unreadable-but-present status
    (any non-ENOENT ``OSError``), PROPAGATES so the caller refuses allocation
    rather than under-counting live ids.
    """
    ids: set[int] = set()
    with os.scandir(proc_root) as it:   # raises OSError if proc_root is absent
        for entry in it:
            if not entry.name.isdigit():
                continue
            task_dir = os.path.join(proc_root, entry.name, "task")
            try:
                task_it = os.scandir(task_dir)
            except (FileNotFoundError, ProcessLookupError):
                # Process exited between the pid enumeration and here (or a
                # kernel without a task/ dir) → fall back to the process-level
                # status, itself exit-safe.
                _fold_status_ids(
                    os.path.join(proc_root, entry.name, "status"), ids)
                continue
            # A task dir present but unreadable (non-ENOENT) propagates → the
            # caller fails closed.
            with task_it as tit:
                for t in tit:
                    if not t.name.isdigit():
                        continue
                    _fold_status_ids(
                        os.path.join(task_dir, t.name, "status"), ids)
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
        anchor_path: str | None = None,
        marker_path: str | None = None,
    ) -> None:
        self._path = counter_path
        self._passwd = passwd_path
        self._group = group_path
        # v0.170.1-r2 — a THIRD durable artifact, SEPARATE from the two
        # high-water copies (counter + anchor): a boolean "Stage-2 uid allocator
        # initialised" marker, written ONCE at first successful init and NEVER
        # written or removed by any teardown/sweep/prune path (allocator-owned,
        # not per-engagement). It is what makes two otherwise-indistinguishable
        # states distinguishable when BOTH high-water copies are lost:
        #   (a) Stage 2 never initialised (fresh install / first Stage-2 boot) →
        #       marker ABSENT → initialise (no uid was ever allocated);
        #   (b) a uid WAS allocated, then everything (workspaces, ctl, outbox,
        #       passwd, the terminal record) was cleaned AND both high-water
        #       copies lost → marker PRESENT → POISON (evidence cannot prove the
        #       historic maximum; reissuing would collide with a survivor).
        # Both the v0.170.0 artifact-existence signal and the v0.170.1 no-marker
        # init were wrong for one of (a)/(b); the marker settles both. A FULL
        # /data wipe (uninstall) loses all three artifacts → marker absent →
        # initialise, which is correct because a wipe removes every engagement
        # too. The marker only needs to survive loss of the two high-water copies
        # SHORT of a full wipe — a separate, never-cleaned file does exactly that.
        self._marker = marker_path or os.path.join(
            os.path.dirname(counter_path) or ".", ".engagement-uids-initialized")
        # S1 r6 — a SECOND, independently-updated durable high-water copy (the
        # "anchor"), written atomically alongside the counter on EVERY persist.
        # The r5 boolean marker detected counter LOSS but did not PRESERVE the
        # high-water, so loss recovered to max(evidence) — and evidence can be
        # INCOMPLETE (a higher uid's record aged out while a lower uid's
        # workspace survives), moving the high-water BACKWARDS and reissuing a
        # uid. Two durable copies fix this: reconstruct takes the max of every
        # VALID durable copy (>= UID_BASE-1); a single-file loss recovers fully
        # from the other; evidence only ever RAISES, never lowers below a valid
        # durable copy. Both live under /data, so a full /data wipe (uninstall)
        # clears both → genuine fresh; any partial loss recovers or fails closed.
        self._anchor = anchor_path or (counter_path + ".initialized")
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

    def _read_durable(self, path: str) -> int | None:
        """Read one durable high-water copy. Returns the int iff it parses as
        ``{"high_water": N}`` with ``N >= UID_BASE - 1`` (the valid floor); any
        absent / malformed / below-floor file (e.g. a stale ``{"high_water": 0}``
        — Terra S2) returns ``None`` (INVALID, ignored). Never raises."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                v = int(json.load(fh)["high_water"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
        return v if v >= UID_BASE - 1 else None

    def _ensure_marker_locked(self) -> None:
        """Write the durable "initialised" marker if absent (caller holds the
        lock). Its mere EXISTENCE — not its content — is the signal, so it is
        written once and never rewritten. Any write failure propagates → the
        caller poisons (fail-closed): without the marker a later both-copies-lost
        boot could not tell a fresh install from a fully-cleaned one."""
        if not os.path.exists(self._marker):
            atomic_write_json(self._marker, {"initialized": True}, mode=0o600)

    def reconstruct(self, known_uids: Iterable[int], dir_owner_uids: Iterable[int]) -> None:
        """Establish the monotonic, DURABLE high-water — or fail closed.

        The load-bearing non-reissue guarantee: a uid is NEVER reissued. The
        high-water lives in TWO durable copies (counter + anchor), both written
        on every persist; a THIRD durable artifact — a boolean "initialised"
        marker (:attr:`_marker`), written once and never removed — records that
        Stage 2 has run at least once. reconstruct takes the max of every VALID
        durable copy; evidence only ever RAISES it, never lowers it below a valid
        copy. Closes the whole survivor-uid-reissue class WITHOUT depending on
        seeing the survivor in /proc.

        Policy:
          - ANY valid durable copy → ``hw = max(valid_durables, evidence,
            UID_BASE-1)`` (a single-file loss recovers fully from the other; a
            stale-low copy is ignored); BACKFILL the marker if absent (so a
            v0.170.0 install that already allocated gets the marker on its first
            v0.170.1 boot — crash-safe ordering: copies first, then marker).
          - a durable file PRESENT but unreadable (malformed/stale-low) with no
            valid copy → POISON: a counter was written and is now unreadable, so
            a uid may exist whose maximum we cannot prove (Terra S2, unchanged).
          - NO valid durable copy, no present-but-invalid copy:
              * marker PRESENT → Stage 2 WAS initialised and both high-water
                copies are now gone → POISON (:class:`UidStateError`); never init,
                never reissue (operator repairs). (Closes the r1 reissue hole:
                allocate 200000 → everything cleaned + both copies lost → marker
                still present → refuse.)
              * marker ABSENT + a REAL uid is evidenced anywhere (record/owner/
                passwd/proc ``>= UID_BASE``) → POISON (v0.170.1-r3 TRANSITION
                hole): the marker is NEW in v0.170.1, so an absent marker with a
                real uid evidenced means a v0.170.0 install already allocated
                (wrote copies, no marker) whose copies are now lost — a uid WAS
                allocated whose maximum we cannot prove → refuse, never reissue.
              * marker ABSENT + NO real-uid evidence → Stage 2 never initialised
                (genuine fresh install OR first Stage-2 boot of a pre-Stage-2
                install: dirs root-owned uid 0, records UNALLOCATED) → INITIALISE
                at ``UID_BASE - 1``, write both copies + the marker. (Preserves
                the N150 first-boot unbrick: zero real-uid evidence there.)
          - the /proc scan unconfirmable, or a persist failure → POISON
            (r4 invariant).

        Why a marker AND real-uid evidence: (a) "Stage 2 never initialised" and
        (b) "a uid was allocated, then fully cleaned + both copies lost" are
        indistinguishable from evidence alone; the never-removed marker separates
        them for post-marker installs. But the marker is new, so a PRE-marker
        v0.170.0 allocation shows no marker — real-uid evidence is the signal that
        catches that transition. Evidence sources (in the valid-durable branch
        they only RAISE the high-water; in the marker-absent branch their mere
        presence refuses): *known_uids*, *dir_owner_uids*, ``casa-eng`` passwd,
        live /proc.

        Any failure poisons the allocator under the lock and surfaces as
        UidStateError; nothing escapes as a bare OSError.
        """
        with self._lock:
            try:
                counter_v = self._read_durable(self._path)
                anchor_v = self._read_durable(self._anchor)
                valid_durables = [v for v in (counter_v, anchor_v)
                                  if v is not None]
                # A durable file PRESENT but yielding no valid value
                # (malformed/stale-low) → a counter was written and is now
                # unreadable → cannot prove the prior maximum → poison.
                durable_file_present_but_invalid = (
                    (os.path.exists(self._path) and counter_v is None)
                    or (os.path.exists(self._anchor) and anchor_v is None))

                # Evidence (only RAISES within the valid-durable branch): record
                # allocated_uids, workspace/ctl/outbox dir OWNER uids, casa-eng
                # passwd, live /proc — only values >= UID_BASE ("a uid was
                # allocated"; a root-owned pre-Stage-2 dir's st_uid 0 is not).
                passwd_uids = scan_passwd_uids(self._passwd)
                # Resolve the live-uid scanner at CALL time so a module-level
                # monkeypatch is honored (see __init__). An unconfirmable scan
                # raises → fail-closed below.
                scanner = self._proc_scanner or scan_proc_uids
                proc_uids = scanner()
                evidence = [
                    v for v in (*known_uids, *dir_owner_uids,
                                *passwd_uids, *proc_uids)
                    if v >= UID_BASE
                ]
                evidence_max = max(evidence) if evidence else None

                if valid_durables:
                    hw = max(*valid_durables, UID_BASE - 1)
                    if evidence_max is not None:
                        hw = max(hw, evidence_max)
                elif durable_file_present_but_invalid:
                    raise UidStateError(
                        "an engagement-uid durable copy is present but unreadable "
                        "(malformed/stale-low) and no valid copy remains — "
                        "refusing to allocate rather than risk resetting the "
                        "high-water backwards and reissuing a uid")
                elif os.path.exists(self._marker):
                    # Stage 2 WAS initialised (marker present) but both high-water
                    # copies are gone → cannot prove the historic maximum. Evidence
                    # (even a real uid) cannot bound it, so refuse — never init and
                    # never reissue. Operator repairs the durable copies.
                    raise UidStateError(
                        "the engagement-uid allocator was initialised (marker "
                        "present) but both durable high-water copies are lost — "
                        "refusing to allocate rather than risk reissuing a "
                        "previously-allocated uid; restore the counter to repair")
                elif evidence_max is not None:
                    # v0.170.1-r3 TRANSITION hole (Sol): the marker is NEW in
                    # v0.170.1, so an ABSENT marker is ambiguous — it can mean a
                    # virgin/pre-Stage-2 install OR a v0.170.0 install that already
                    # allocated a uid (v0.170.0 wrote counter+anchor but NO marker).
                    # With no valid durable copy but a REAL uid evidenced anywhere
                    # (record/owner/passwd/proc >= UID_BASE), a uid WAS allocated
                    # (pre-marker, or a partially-cleaned state) and we cannot prove
                    # its maximum → REFUSE, never init at base and reissue it.
                    raise UidStateError(
                        "no valid durable engagement-uid high-water and no "
                        "'initialized' marker, but a real uid is evidenced (a uid "
                        "was allocated before the marker existed) — refusing to "
                        "allocate rather than risk reissuing it; restore the "
                        "counter to repair")
                else:
                    # marker ABSENT and NO real-uid evidence → Stage 2 never
                    # initialised (genuine fresh install OR the first Stage-2 boot
                    # of a pre-Stage-2 install: all dirs root-owned uid 0, records
                    # UNALLOCATED, no casa-eng passwd, no casa /proc uid). No uid
                    # was ever allocated → INITIALISE at UID_BASE-1 and write both
                    # copies + the marker below. (Preserves the N150 first-boot
                    # unbrick: its pre-Stage-2 state has zero real-uid evidence.)
                    hw = UID_BASE - 1

                self._hw = hw
                self._poisoned = False           # proven-good
                self._persist()                  # counter + anchor (poisons on dual-fail)
                self._ensure_marker_locked()     # THIRD durable artifact (poisons on fail)
            except Exception as exc:  # noqa: BLE001 — any failure poisons
                self._poison_locked()
                if isinstance(exc, UidStateError):
                    raise                        # preserve the precise reason
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
        """Write the high-water to BOTH durable copies (counter + anchor), each
        atomically, under the caller's lock.

        S1 r6 write policy (stated exactly): attempt both writes; tolerate ONE
        failing (a single transient error must not brick allocation, and on the
        next read ``max()`` handles the divergence — the surviving copy carries
        the current high-water). Raise (→ the caller poisons) ONLY if BOTH
        writes fail, so we never proceed with NEITHER copy updated."""
        payload = {"high_water": self._hw}
        errors: list[OSError] = []
        for path in (self._path, self._anchor):
            try:
                atomic_write_json(path, payload, mode=0o600)
            except OSError as exc:
                errors.append(exc)
        if len(errors) == 2:
            raise errors[-1]   # both durable writes failed → caller poisons

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
