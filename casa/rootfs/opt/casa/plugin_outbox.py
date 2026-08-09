"""Shared /data media outbox — FD-based, TOCTOU-safe (v0.73.0, spec §3.4).

A producing plugin writes a file into ``/data/plugin-outbox/`` (atomic
``.part`` -> rename) and returns ONLY its path. The ``send_media`` tool then:

  1. **claim** it by atomic rename into the private ``.claims/`` subdir under a
     ``<epoch_ms>-<uuid4>`` name (exclusive ownership — removes cleanup and
     concurrency races; exactly one caller wins, a loser gets ``missing``);
  2. **capture** the bytes from a guarded ``O_NOFOLLOW`` FD opened *by the claim
     name* (validate == read; never re-open by the original path);
  3. **remove** the claim on EVERY outcome.

The realpath-under-outbox check + an ``lstat`` regular-file type-gate +
``O_NOFOLLOW`` + ``st_nlink == 1`` + fstat-then-read-the-same-FD is the
exfiltration control; magic is a format/kind sanity gate, not the control.
Same-uid-root producers are outside the model (§3.4): both dirs are
``chmod 0770`` and the claim name is unpredictable, but a root racer is not
defended against.

Dependency-neutral: imports ``media_policies`` + stdlib only.
"""
from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
import threading
import uuid

from media_policies import MEDIA_POLICIES

logger = logging.getLogger(__name__)

OUTBOX_ENV = "CASA_PLUGIN_OUTBOX_DIR"
CLAIMS_SUBDIR = ".claims"
# Terra r6 (#330): reap-ownership entries live in their own sweep-owned
# subdirectory — a marker in the outbox ROOT could collide with a legal
# producer dotfile name and the sweep would hijack it.
REAP_SUBDIR = ".reap"
MAX_AGE_S = 2 * 3600  # orphan reap threshold for the sweep

_DIR_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class OutboxError(Exception):
    """A guard/capture failure carrying a stable ``kind_error`` string."""

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message or kind)
        self.kind = kind


def _safe_basename(name: str) -> bool:
    """True iff *name* is a single control-free path component (not . / ..)."""
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\0" in name:
        return False
    return not any(ord(c) < 0x20 for c in name)


def _read_capped(fd: int, cap: int) -> bytes:
    """Read at most ``cap + 1`` bytes from *fd* (one extra so the caller can
    detect an over-cap file). Loops over short reads."""
    limit = cap + 1
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        block = os.read(fd, limit - total)
        if not block:
            break
        chunks.append(block)
        total += len(block)
    return b"".join(chunks)


def _listdir_quiet(dirfd: int) -> list[str]:
    try:
        return os.listdir(dirfd)
    except OSError:
        return []


def _lstat_quiet(name: str, dirfd: int):
    try:
        return os.lstat(name, dir_fd=dirfd)
    except OSError:
        return None


def _reap_dir_name(origin: str) -> str:
    """The per-reap ownership DIRECTORY name inside ``.reap/`` — bounded
    length regardless of the candidate's own name (Sol r6: appending the
    original name to a private name overflows NAME_MAX for long-but-legal
    producer names, making those orphans uncollectable). The moved entry
    keeps its ORIGINAL name inside this directory, so recovery is
    self-describing: ``.reap/<origin>.<pid>.<uuid>/<original-name>``."""
    return f"{origin}.{os.getpid()}.{uuid.uuid4().hex}"


def _reap_dir_origin(dname: str) -> str | None:
    """Parse the origin ("root"/"claims") from a per-reap directory name;
    None when unparseable (sweep-owned garbage, age-reaped)."""
    origin = dname.split(".", 1)[0]
    return origin if origin in ("root", "claims") else None


def _claim_epoch_ms(name: str):
    """Parse the ``<epoch_ms>`` prefix of a claim name; None if unparseable."""
    prefix = name.split("-", 1)[0]
    try:
        return int(prefix)
    except ValueError:
        return None


class PluginOutbox:
    def __init__(self, root: str) -> None:
        # `_lock` serializes the dir-FD syscalls against close() so a concurrent
        # close (which nulls the FDs) can never interleave between the closed-
        # check and a syscall — otherwise a dir_fd=None op would resolve against
        # the process CWD (a fail-open). Held only for the FAST syscalls; the
        # slow capture read uses the returned file FD and runs outside the lock.
        self._lock = threading.Lock()
        self._closed = False
        self._root_realpath = os.path.realpath(root)
        self._claims_realpath = os.path.join(self._root_realpath, CLAIMS_SUBDIR)
        self._reap_realpath = os.path.join(self._root_realpath, REAP_SUBDIR)
        # Belt-and-suspenders: setup-configs.sh creates these at boot, but make
        # the module self-sufficient (idempotent) for tests and cold starts.
        os.makedirs(self._root_realpath, exist_ok=True)
        os.makedirs(self._claims_realpath, exist_ok=True)
        # Sol r7: ``.reap`` was a LEGAL producer basename before this
        # subdirectory existed — an upgrade over such a file would make
        # makedirs raise FileExistsError, disabling the whole outbox with no
        # self-heal. Displace a non-directory occupant (file OR symlink — the
        # lstat never follows) to a fresh producer-visible name: content is
        # preserved; the producer's returned path is invalidated exactly as
        # an expiry would have.
        try:
            _st = os.lstat(self._reap_realpath)
            if not stat.S_ISDIR(_st.st_mode):
                os.replace(
                    self._reap_realpath,
                    os.path.join(self._root_realpath,
                                 f"reap-displaced-{uuid.uuid4().hex}"))
        except OSError:
            pass                       # absent (the normal case) — or racing
        os.makedirs(self._reap_realpath, exist_ok=True)
        os.chmod(self._root_realpath, 0o770)
        os.chmod(self._claims_realpath, 0o770)
        os.chmod(self._reap_realpath, 0o770)
        # Long-lived dir FDs pinned to the real inodes: subsequent operations go
        # through these, immune to a later swap of the /data/plugin-outbox path
        # to a symlink.
        self._outbox_dirfd = os.open(self._root_realpath, _DIR_OPEN_FLAGS)
        self._claims_dirfd = os.open(self._claims_realpath, _DIR_OPEN_FLAGS)
        self._reap_dirfd = os.open(self._reap_realpath, _DIR_OPEN_FLAGS)

    def _ensure_open(self) -> None:
        # A closed instance must FAIL CLOSED — never fall through to a
        # ``dir_fd=None`` op, which resolves relative to the process CWD (an
        # exfiltration fail-open, e.g. grabbing a same-named CWD file).
        if self._closed:
            raise OutboxError("guard_error", "outbox is closed")

    # -- claim ----------------------------------------------------------------

    def claim(self, requested_path: str) -> str:
        """Atomically claim *requested_path* into ``.claims/`` and return the
        claim name. Raises
        ``OutboxError(bad_name|outside_outbox|missing|guard_error)``."""
        basename = os.path.basename(requested_path)
        if not _safe_basename(basename):
            raise OutboxError("bad_name", f"unsafe basename {basename!r}")
        # The path MUST carry a dirname that realpaths to the outbox root. A
        # BARE basename (empty dirname) is refused deterministically — else
        # ``realpath("")`` == CWD could accidentally match when CWD is the outbox.
        parent = os.path.dirname(requested_path)
        if not parent or os.path.realpath(parent) != self._root_realpath:
            raise OutboxError("outside_outbox", "path is not directly under the outbox")
        claim_name = f"{_now_ms()}-{uuid.uuid4().hex}"
        with self._lock:                       # atomic: closed-check + the FD rename
            self._ensure_open()
            try:
                os.rename(basename, claim_name,
                          src_dir_fd=self._outbox_dirfd, dst_dir_fd=self._claims_dirfd)
            except FileNotFoundError as exc:
                raise OutboxError("missing", "source vanished before claim") from exc
            except OSError as exc:
                # EXDEV/EACCES/EISDIR/etc. — a guard/FS failure, NOT clean "missing".
                raise OutboxError("guard_error",
                                  f"claim rename failed: errno {exc.errno}") from exc
        return claim_name

    # -- cleanup --------------------------------------------------------------

    def remove_claim(self, claim_name: str) -> None:
        """Remove a claim inode by type, ALL relative to the pinned ``.claims/``
        dir-FD (no path-based op — keeps the FD boundary). ``shutil.rmtree``'s
        ``dir_fd`` kwarg is **Python 3.11+** (verified against the 3.11 docs;
        the container base is 3.11). A directory-typed claim (a misbehaving
        producer) is rmtree'd — ``os.rmdir`` would fail on a non-empty dir.
        Unconditional (but fail-closed once the outbox is closed)."""
        with self._lock:
            self._ensure_open()
            try:
                st = os.lstat(claim_name, dir_fd=self._claims_dirfd)
            except FileNotFoundError:
                return  # already gone — nothing to do
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(claim_name, dir_fd=self._claims_dirfd)
            else:
                os.unlink(claim_name, dir_fd=self._claims_dirfd)

    # -- capture --------------------------------------------------------------

    def capture(self, claim_name: str, kind: str) -> bytes:
        """Validate == read the claimed inode via a guarded FD, then return its
        bytes. Never re-opens by the original path. Raises
        ``OutboxError(not_regular|multi_link|too_large|magic_mismatch|guard_error)``.
        Synchronous — the tool runs it via ``asyncio.to_thread``."""
        cap = MEDIA_POLICIES[kind].size_cap
        # Type-gate via lstat FIRST: only a regular file is deliverable. This
        # rejects symlink/socket/fifo/dir/device UNIFORMLY as not_regular — a
        # socket open() returns ENXIO (NOT ELOOP), so errno-matching on the open
        # alone is insufficient. O_NOFOLLOW + the post-open fstat re-check then
        # defend the lstat->open TOCTOU (a symlink swapped in after lstat -> ELOOP).
        # The dir-FD syscalls (lstat + open) run under the lock so a concurrent
        # close() cannot null the FDs between the closed-check and the open; the
        # returned file FD is independent of the dir-FD, so the slow read below
        # runs OUTSIDE the lock (captures stay concurrent).
        with self._lock:
            self._ensure_open()
            try:
                st0 = os.lstat(claim_name, dir_fd=self._claims_dirfd)
            except OSError as exc:
                raise OutboxError("guard_error",
                                  f"lstat failed: errno {exc.errno}") from exc
            if not stat.S_ISREG(st0.st_mode):
                raise OutboxError("not_regular", "claimed inode is not a regular file")
            try:
                fd = os.open(
                    claim_name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=self._claims_dirfd,
                )
            except OSError as exc:
                # A symlink swapped in AFTER lstat (TOCTOU) -> O_NOFOLLOW ELOOP.
                # Any other open failure is an unexpected guard fault, not a type.
                if exc.errno == errno.ELOOP:
                    raise OutboxError("not_regular",
                                      "symlink swapped in; refused by O_NOFOLLOW") from exc
                raise OutboxError("guard_error",
                                  f"open failed: errno {exc.errno}") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OutboxError("not_regular", "inode changed type after lstat")
            if st.st_nlink != 1:
                raise OutboxError("multi_link", f"st_nlink={st.st_nlink}")
            if st.st_size > cap:
                raise OutboxError("too_large", f"{st.st_size} > {cap}")
            content = _read_capped(fd, cap)
            if len(content) > cap:
                raise OutboxError("too_large", "read exceeded cap")
            if len(content) < st.st_size:
                # A claimed regular file shrinking between fstat and read is an
                # integrity anomaly (only a root racer could) — refuse.
                raise OutboxError("guard_error", "file shrank during read")
            if not MEDIA_POLICIES[kind].accepts(content):
                raise OutboxError("magic_mismatch",
                                  f"head bytes not valid for kind {kind!r}")
            return content
        except OutboxError:
            raise
        except OSError as exc:
            raise OutboxError("guard_error", f"read failed: {exc.errno}") from exc
        finally:
            os.close(fd)

    # -- sweep ----------------------------------------------------------------

    def sweep_once(self, now_ms: int) -> int:
        """Reap orphans: outbox-root entries by lstat mtime, ``.claims/`` entries
        by embedded epoch; both older than ``MAX_AGE_S``. Never follows symlinks.
        Returns the count reaped. Synchronous (run via ``asyncio.to_thread``).
        The whole scan runs under the lock so a concurrent close() cannot null the
        dir-FDs mid-scan (``os.listdir(None)`` would enumerate the CWD). The outbox
        is normally near-empty, so this is cheap."""
        with self._lock:
            if self._closed:
                return 0
            return self._sweep_locked(now_ms)

    def _sweep_locked(self, now_ms: int) -> int:
        cutoff_ms = MAX_AGE_S * 1000
        reaped = 0
        # Terra r5/r6 + Sol r6 (#330): FIRST recover any stranded per-reap
        # ownership directories — a crash between the ownership rename and
        # its restore/unlink leaves the publication parked inside
        # ``.reap/<origin>.<pid>.<uuid>/`` under its ORIGINAL name;
        # age-reaping it would lose a FRESH file. Recovery is total: fresh ⇒
        # back under its name in the origin dir (no-replace), superseded ⇒
        # dropped, expired ⇒ back under its name and reaped by the ordinary
        # expiry pass below (which re-lists after this). Non-directory or
        # unparseable ``.reap/`` residue is sweep-owned garbage — age-reaped.
        for dname in _listdir_quiet(self._reap_dirfd):
            self._recover_reap_dir(dname, now_ms, cutoff_ms)
        # Outbox root — skip the sweep-owned subdirs; reap producer leftovers
        # by mtime.
        for name in _listdir_quiet(self._outbox_dirfd):
            if name in (CLAIMS_SUBDIR, REAP_SUBDIR):
                continue
            st = _lstat_quiet(name, self._outbox_dirfd)
            if st is None:
                continue
            if now_ms - int(st.st_mtime * 1000) > cutoff_ms:
                reaped += self._reap(self._outbox_dirfd, name, st)
        # Claims — age is the embedded epoch (rename preserves source mtime, so
        # mtime is NOT claim age). Unparseable names fall back to mtime.
        for name in _listdir_quiet(self._claims_dirfd):
            epoch_ms = _claim_epoch_ms(name)
            st = _lstat_quiet(name, self._claims_dirfd)
            if st is None:
                continue
            age_ref = epoch_ms if epoch_ms is not None else int(st.st_mtime * 1000)
            if now_ms - age_ref > cutoff_ms:
                reaped += self._reap(self._claims_dirfd, name, st)
        return reaped

    def _reap(self, dirfd: int, name: str, st: os.stat_result) -> int:
        # #330 (Sol r1): producers publish via atomic rename OUTSIDE
        # self._lock (they are separate processes — the in-process lock
        # cannot serialize them), so between the expiry lstat and a deletion
        # the name can come to denote a FRESH inode. Deleting by name would
        # vanish a path the producer just returned. Take OWNERSHIP first:
        # atomically rename the entry into a fresh per-reap directory under
        # the sweep-owned ``.reap/``, keeping its ORIGINAL name (bounded dir
        # name — Sol r6 — so NAME_MAX-length producer names stay
        # collectable), then decide on the inode we now exclusively hold:
        # matching the expiry stat ⇒ delete; a fresh inode ⇒ restore it
        # (no-replace: a newer same-name publication wins). A crash mid-
        # protocol leaves the entry inside the per-reap dir; the next
        # sweep's recovery pass restores it (Terra r5/r6).
        origin = "claims" if dirfd == self._claims_dirfd else "root"
        pdir = _reap_dir_name(origin)
        try:
            os.mkdir(pdir, 0o700, dir_fd=self._reap_dirfd)
            pfd = os.open(pdir, _DIR_OPEN_FLAGS, dir_fd=self._reap_dirfd)
        except OSError as exc:
            logger.warning(
                "plugin-outbox: could not create reap-ownership dir for %r: "
                "%s", name, exc)
            return 0
        try:
            try:
                os.rename(name, name, src_dir_fd=dirfd, dst_dir_fd=pfd)
            except OSError:
                return 0              # already gone / replaced mid-scan
            current = _lstat_quiet(name, pfd)
            if current is None:
                return 0
            if current.st_ino != st.st_ino or current.st_dev != st.st_dev:
                self._restore_entry(pfd, name, dirfd)
                return 0
            return self._delete_owned_entry(pfd, name, current)
        finally:
            os.close(pfd)
            try:
                os.rmdir(pdir, dir_fd=self._reap_dirfd)
            except OSError:
                pass                  # non-empty (a failed delete/restore) —
                                      # the recovery pass retries next sweep

    def _recover_reap_dir(
        self, dname: str, now_ms: int, cutoff_ms: int,
    ) -> None:
        """Recover one ``.reap/`` residue entry: restore a stranded held
        publication to its origin dir, or age-reap unparseable garbage."""
        origin = _reap_dir_origin(dname)
        dst_dirfd = (self._claims_dirfd if origin == "claims"
                     else self._outbox_dirfd)
        try:
            pfd = os.open(dname, _DIR_OPEN_FLAGS, dir_fd=self._reap_dirfd)
        except OSError:
            # Not a directory / unopenable — sweep-owned garbage by age.
            st = _lstat_quiet(dname, self._reap_dirfd)
            if (st is not None
                    and now_ms - int(st.st_mtime * 1000) > cutoff_ms):
                try:
                    if stat.S_ISDIR(st.st_mode):
                        shutil.rmtree(dname, dir_fd=self._reap_dirfd)
                    else:
                        os.unlink(dname, dir_fd=self._reap_dirfd)
                except OSError as exc:
                    logger.warning(
                        "plugin-outbox: failed to clear reap residue %r: %s",
                        dname, exc)
            return
        try:
            for ename in os.listdir(pfd):
                if origin is not None and _safe_basename(ename):
                    self._restore_entry(pfd, ename, dst_dirfd)
                else:
                    st = _lstat_quiet(ename, pfd)
                    if st is not None:
                        self._delete_owned_entry(pfd, ename, st)
        except OSError as exc:
            logger.warning(
                "plugin-outbox: reap recovery scan of %r failed: %s",
                dname, exc)
        finally:
            os.close(pfd)
        try:
            os.rmdir(dname, dir_fd=self._reap_dirfd)
        except OSError:
            pass                      # non-empty — retried next sweep

    @staticmethod
    def _delete_owned_entry(pfd: int, name: str, st: os.stat_result) -> int:
        """Delete an exclusively-owned entry inside a per-reap directory
        (rmtree dir_fd is 3.11+)."""
        try:
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(name, dir_fd=pfd)
            else:
                os.unlink(name, dir_fd=pfd)
            return 1
        except OSError as exc:
            logger.warning("plugin-outbox: failed to reap %r: %s", name, exc)
            return 0

    @staticmethod
    def _restore_entry(pfd: int, name: str, dst_dirfd: int) -> None:
        """Give a privately-held FRESH inode (inside a per-reap dir) back its
        published name without ever replacing a newer publication (Terra/Sol
        r2): ``os.link`` is the atomic no-replace primitive — it fails
        EEXIST when a newer same-name publication landed meanwhile, in which
        case the held copy is simply superseded (identical outcome to
        producer-overwrites-producer) and dropped. Directories cannot be
        hardlinked; a held directory falls back to a replacing rename —
        producers publish regular files, so a same-name DIRECTORY
        republication racing its own reap is not a real traffic pattern, and
        the entry is otherwise restored intact."""
        current = _lstat_quiet(name, pfd)
        if current is None:
            return
        if stat.S_ISDIR(current.st_mode):
            try:
                os.rename(name, name, src_dir_fd=pfd, dst_dir_fd=dst_dirfd)
            except OSError as exc:
                logger.warning(
                    "plugin-outbox: could not restore fresh dir %r after "
                    "reap-ownership check: %s", name, exc)
            return
        superseded_or_restored = False
        try:
            os.link(name, name, src_dir_fd=pfd, dst_dir_fd=dst_dirfd,
                    follow_symlinks=False)
            superseded_or_restored = True
        except FileExistsError:
            superseded_or_restored = True     # newer publication wins
        except OSError as exc:
            logger.warning(
                "plugin-outbox: could not restore fresh entry %r after "
                "reap-ownership check: %s", name, exc)
        if superseded_or_restored:
            try:
                os.unlink(name, dir_fd=pfd)
            except OSError:
                pass

    def sweep_now(self) -> int:
        """Production sweep entry — uses the module clock. Tests drive
        ``sweep_once(now_ms)`` directly with a fixed clock."""
        return self.sweep_once(_now_ms())

    def close(self) -> None:
        # Serialized against every FD-using op via the lock: an in-flight op has
        # either already completed its syscall (lock released) or has not yet
        # passed its closed-check — so nulling the FDs here can never make a live
        # op perform a dir_fd=None (CWD-relative) syscall. If another thread holds
        # the lock mid-syscall, close() just waits for it.
        with self._lock:
            self._closed = True
            for fd_attr in ("_outbox_dirfd", "_claims_dirfd", "_reap_dirfd"):
                fd = getattr(self, fd_attr, None)
                if isinstance(fd, int):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    setattr(self, fd_attr, None)


# ---------------------------------------------------------------------------
# Module singleton + boot wiring (initialised once at boot by casa_core).
# ---------------------------------------------------------------------------

_OUTBOX: PluginOutbox | None = None


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


def init_outbox(root: str) -> PluginOutbox:
    global _OUTBOX
    _OUTBOX = PluginOutbox(root)
    logger.info("plugin-outbox initialised at %s", _OUTBOX._root_realpath)
    return _OUTBOX


def get_outbox() -> PluginOutbox | None:
    return _OUTBOX


async def sweep_job() -> int:
    """Off-loop sweep entry (boot + hourly). No-op when the outbox is
    uninitialised. Runs the reap in a worker thread so the loop never blocks on
    FS I/O. Self-contained: a failure is logged and swallowed (returns 0) so it
    is safe both as the boot call and as the APScheduler job."""
    import asyncio
    ob = get_outbox()
    if ob is None:
        return 0
    try:
        reaped = await asyncio.to_thread(ob.sweep_now)
        if reaped:
            logger.info("plugin-outbox sweep reaped %d orphan(s)", reaped)
        return reaped
    except Exception:  # noqa: BLE001 — a sweep failure must not crash boot/scheduler
        logger.warning("plugin-outbox sweep failed", exc_info=True)
        return 0


def register_sweep(scheduler) -> None:
    """Register the hourly outbox sweep on an APScheduler instance. Extracted so
    casa_core's wiring is unit-testable with a fake scheduler."""
    scheduler.add_job(
        sweep_job, trigger="interval", id="plugin_outbox_sweep", hours=1,
        replace_existing=True, coalesce=True, max_instances=1,
        misfire_grace_time=3600,
    )


# ---------------------------------------------------------------------------
# Per-engagement PRIVATE outbox (containment stage 2, Task 11 / design §7).
# ---------------------------------------------------------------------------
# A producer plugin assigned to a uid-dropped engagement runs AS that
# engagement's allocated uid (``--clear-groups``), so it can no longer write
# the SHARED ``/data/plugin-outbox`` (``0770 root:root``) — and that dir must
# stay non-group/world-writable (a shared writable outbox would let one
# engagement's producer drop a file into another's delivery flow). The fix is
# a PRIVATE per-engagement dir the child DOES own, kept entirely separate
# from the shared tree so the shared dir's mode never has to change:
# ``<ENGAGEMENT_OUTBOX_ROOT>/<uid>/``, owned ``uid:uid``. The claim path
# (``tools.send_media``) derives which private dir to open from the
# AUTHENTICATED engagement record's ``allocated_uid`` — never from the
# caller-submitted ``path`` argument — and reuses this module's existing
# ``PluginOutbox.claim``/``capture`` (dir-FD, ``O_NOFOLLOW``, single-hard-
# link, no-later-pathname-access) completely unchanged: pointing a fresh
# instance at a different root is the only thing that differs.

ENGAGEMENT_OUTBOX_ROOT = "/data/plugin-outbox-eng"

_engagement_outboxes: dict[int, "PluginOutbox"] = {}
_engagement_outboxes_lock = threading.Lock()


def engagement_outbox_dir(uid: int, *, root: str | None = None) -> str:
    """Absolute path of *uid*'s private outbox dir."""
    return os.path.join(root if root is not None else ENGAGEMENT_OUTBOX_ROOT,
                        str(uid))


def provision_engagement_outbox(uid: int, *, root: str | None = None) -> str:
    """Create (idempotently) *uid*'s private outbox dir, owned ``uid:uid``.

    This function itself sets the dir to ``0700``, but the FIRST
    :func:`get_engagement_outbox` access wraps it in a plain
    :class:`PluginOutbox`, whose ``__init__`` unconditionally re-chmods its
    root to ``0770`` (the SAME constructor the shared outbox uses — it has
    no notion of "private", so it always widens to group-rwx). The
    documented end state after normal use is therefore **``0770``, not
    ``0700``** — the docstring here used to claim ``0700``, which is only
    true before that first wrap. This is still cross-engagement-safe TODAY
    because (a) the dir is owned ``uid:uid`` with ``uid`` also standing in
    as the GID — a dedicated, single-member group per engagement (containment
    stage 2 design §2: "GID = uid, dedicated primary group per engagement —
    no shared group") — so the ``0770`` group-rwx bits only ever grant access
    back to the SAME uid, and (b) the OTHER bits stay ``0`` throughout, so no
    other uid (with ``--clear-groups``) can reach it regardless. It would
    STOP being safe the moment any engagement's GID were changed to a
    SHARED value — :func:`test_get_engagement_outbox_root_is_uid_owned_with_private_group`
    (``tests/test_plugin_outbox.py``) pins uid==gid on the real
    ``get_engagement_outbox`` path specifically so that future change trips
    a test instead of silently reopening cross-engagement outbox access.

    The PARENT dir is created ``0711`` root-owned: any uid can still search
    THROUGH it to its own named subdirectory (execute-only — no listing, no
    write), but no uid can list its siblings or create an entry directly
    inside it. Never touches (and never needs to touch) the unrelated
    shared ``/data/plugin-outbox``. Idempotent — safe to call again for an
    already-provisioned uid (e.g. on every boot-replay resume); never wipes
    existing contents."""
    base = root if root is not None else ENGAGEMENT_OUTBOX_ROOT
    os.makedirs(base, exist_ok=True)
    os.chmod(base, 0o711)
    d = engagement_outbox_dir(uid, root=root)
    os.makedirs(d, exist_ok=True)
    try:
        os.chown(d, uid, uid, follow_symlinks=False)
    except PermissionError:
        # Production always runs this as root (the containment-stage-2
        # threat model assumes it — see design §"gating measurement"), so a
        # real chown to an arbitrary allocated uid always succeeds there.
        # An unprivileged process (unit tests; a dev shell) cannot chown to
        # an arbitrary uid it does not own — degrade instead of crashing so
        # the store/provisioning path stays exercisable without root.
        if os.geteuid() == 0:
            raise
        logger.debug(
            "engagement outbox: chown to uid %s skipped — process is not "
            "root (euid=%s)", uid, os.geteuid())
    os.chmod(d, 0o700)
    return d


def get_engagement_outbox(uid: int, *, root: str | None = None) -> "PluginOutbox":
    """Return the cached :class:`PluginOutbox` rooted at *uid*'s private
    outbox dir, provisioning the dir first if this is the first access for
    *uid* in this process (lazy fallback — the normal path is eager
    provisioning at workspace setup). Cached per-uid so repeated
    ``send_media`` calls across one engagement's lifetime reuse the same
    pinned dir-FDs instead of reopening them on every claim."""
    with _engagement_outboxes_lock:
        ob = _engagement_outboxes.get(uid)
        if ob is not None:
            return ob
        path = provision_engagement_outbox(uid, root=root)
        ob = PluginOutbox(path)
        _engagement_outboxes[uid] = ob
        return ob


def teardown_engagement_outbox(uid: int, *, root: str | None = None) -> None:
    """Close the cached instance (if any) and remove *uid*'s private outbox
    dir — called alongside the other per-engagement teardown paths (the
    workspace retention sweep, ``delete_engagement_workspace``). Best-effort,
    matching the sibling control-dir/passwd-entry cleanup at those same call
    sites: a removal failure is logged, never raised."""
    with _engagement_outboxes_lock:
        ob = _engagement_outboxes.pop(uid, None)
    if ob is not None:
        try:
            ob.close()
        except Exception:  # noqa: BLE001 — best-effort, mirrors sibling cleanups
            logger.warning("engagement outbox close failed for uid %s", uid,
                           exc_info=True)
    d = engagement_outbox_dir(uid, root=root)
    try:
        if os.path.islink(d):
            os.unlink(d)
        elif os.path.isdir(d):
            shutil.rmtree(d)
    except OSError as exc:
        logger.warning("engagement outbox rmtree failed for uid %s: %s", uid, exc)


async def wire(scheduler, root: str) -> None:
    """One-call boot wiring casa_core invokes in section 7 (BEFORE channels/HTTP
    go live): init the outbox, run the boot reap, register the hourly sweep. A
    failure never blocks boot. Unit-tested with a fake scheduler + tmp root."""
    try:
        init_outbox(root)
    except Exception:  # noqa: BLE001
        logger.warning("plugin-outbox init failed; send_media disabled", exc_info=True)
        return
    await sweep_job()          # boot-time immediate reap
    register_sweep(scheduler)
