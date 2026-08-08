"""safe_fs — no-symlink, workspace-confined root access.

Lets casa-core (running as root) read/traverse files inside a uid-owned
engagement workspace without following an attacker-planted symlink out to a
sibling engagement's tree.

Two code paths, both fail-closed:

- Fast path: a single `openat2(2)` syscall with
  `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS`. The kernel refuses ANY symlink
  component (final or intermediate) and any `..` escape past the starting
  directory in one atomic resolution — immune to a TOCTOU race between
  components. Invoked via a raw `ctypes` syscall because the Python stdlib
  does not wrap `openat2`.
- Fallback path (kernels without openat2, e.g. < 5.6): walk the path one
  component at a time, opening each with `openat(..., O_NOFOLLOW)` relative
  to the previous component's directory fd, never by re-resolving a full
  pathname. This is deliberately NOT a single check-then-open: an attacker
  who can swap a path component between a check and a later open (a
  classic TOCTOU) cannot win here because each step's fd is what the next
  step opens relative to, and nothing is ever reopened by name afterward.

Owner-uid enforcement (`owner_uid=...`) is layered on top of both paths via
`fstat` on the final fd: even a legitimately-resolved (non-symlink) path is
refused if it is not owned by the expected uid.
"""

import ctypes
import ctypes.util
import errno
import os

__all__ = [
    "SymlinkRefused",
    "HAS_OPENAT2",
    "open_beneath",
    "read_text_beneath",
    "list_dir_beneath",
]


class SymlinkRefused(OSError):
    """Raised when a path component is a symlink, or resolution would
    escape the confinement root — on either the openat2 or fallback path."""


# --- openat2 syscall plumbing -------------------------------------------------

RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08

# __NR_openat2 is 437 on both supported build arches (Casa targets amd64 +
# arm64). Confirmed against /usr/include/asm-generic/unistd.h (the arm64/
# generic syscall table) and /usr/include/x86_64-linux-gnu/asm/unistd_64.h
# (the x86_64 table) on 2026-08-09 — both define __NR_openat2 as 437.
_NR_OPENAT2_BY_ARCH = {"x86_64": 437, "aarch64": 437}
_NR_openat2 = _NR_OPENAT2_BY_ARCH.get(os.uname().machine, 437)


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def _openat2(dirfd, path, flags, resolve):
    how = _OpenHow(flags=flags | os.O_CLOEXEC, mode=0, resolve=resolve)
    rc = _libc.syscall(
        ctypes.c_long(_NR_openat2),
        ctypes.c_int(dirfd),
        ctypes.c_char_p(os.fsencode(path)),
        ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(how)),
    )
    if rc < 0:
        e = ctypes.get_errno()
        if e in (errno.ELOOP, errno.EXDEV, errno.EAGAIN):
            # ELOOP: a symlink component was rejected by RESOLVE_NO_SYMLINKS.
            # EXDEV: resolution would cross outside the beneath-root (or a
            #        mount point) under RESOLVE_BENEATH.
            # EAGAIN: RESOLVE_BENEATH detected the walk was raced (retry-worthy
            #        in general, but we treat it as a refusal here: fail closed
            #        rather than retry into an attacker-controlled loop).
            raise SymlinkRefused(e, os.strerror(e), path)
        raise OSError(e, os.strerror(e), path)
    return rc


def _probe_openat2():
    try:
        fd = _openat2(
            os.open(".", os.O_DIRECTORY | os.O_CLOEXEC),
            ".",
            os.O_RDONLY | os.O_DIRECTORY,
            RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS,
        )
        os.close(fd)
        return True
    except OSError as exc:
        return getattr(exc, "errno", None) not in (errno.ENOSYS, errno.EPERM, errno.EINVAL)


HAS_OPENAT2 = _probe_openat2()


def _open_root_dir(root):
    """Open `root` itself, refusing it too if it is a symlink. Symmetric
    between the openat2 and fallback paths so neither treats a
    symlink-as-root as implicitly trusted."""
    try:
        return os.open(root, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        # O_NOFOLLOW + O_DIRECTORY on a symlink reports ELOOP on some
        # kernels and ENOTDIR on others (the symlink itself is not a
        # directory, so the O_DIRECTORY check can fail first) — both mean
        # "root resolved to a symlink", so both are refused.
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise SymlinkRefused(exc.errno, "root is a symlink", root) from exc
        raise


# --- FD-relative fallback -----------------------------------------------------


def _open_fallback(root, rel_path, want_dir, owner_uid):
    """Per-component openat(O_NOFOLLOW) walk, holding only fds — never a
    pathname — across the walk. Any raise path below closes whatever fd it
    currently holds so the fallback never leaks a descriptor."""
    parts = [p for p in rel_path.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise SymlinkRefused(errno.EXDEV, "dotdot escape", rel_path)

    parent = _open_root_dir(root)
    try:
        if not parts:
            # rel_path resolved to "." itself — root already open as parent.
            if owner_uid is not None and os.fstat(parent).st_uid != owner_uid:
                raise SymlinkRefused(errno.EPERM, f"owner != {owner_uid}", rel_path)
            return parent

        for i, name in enumerate(parts):
            last = i == len(parts) - 1
            if last and not want_dir:
                flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_RDONLY
            else:
                flags = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_DIRECTORY | os.O_RDONLY
            try:
                fd = os.open(name, flags, dir_fd=parent)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                    raise SymlinkRefused(exc.errno, "symlink component", name) from exc
                raise
            os.close(parent)
            parent = fd
            if owner_uid is not None and os.fstat(parent).st_uid != owner_uid:
                raise SymlinkRefused(errno.EPERM, f"owner != {owner_uid}", name)
        return parent
    except BaseException:
        os.close(parent)
        raise


# --- public API ----------------------------------------------------------------


def open_beneath(root, rel_path, *, want_dir=False, owner_uid=None):
    """Return an fd for `rel_path` resolved beneath `root`, refusing any
    symlink component (intermediate or final) and any escape attempt.
    Raises SymlinkRefused on refusal, or OSError for ordinary I/O errors."""
    if HAS_OPENAT2:
        rfd = _open_root_dir(root)
        try:
            flags = os.O_RDONLY | (os.O_DIRECTORY if want_dir else 0)
            fd = _openat2(
                rfd, rel_path or ".", flags, RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS
            )
        finally:
            os.close(rfd)
        if owner_uid is not None and os.fstat(fd).st_uid != owner_uid:
            os.close(fd)
            raise SymlinkRefused(errno.EPERM, f"owner != {owner_uid}", rel_path)
        return fd
    return _open_fallback(root, rel_path, want_dir, owner_uid)


def read_text_beneath(root, rel_path, *, owner_uid=None, max_bytes=None):
    fd = open_beneath(root, rel_path, want_dir=False, owner_uid=owner_uid)
    with os.fdopen(fd, "rb") as fh:
        data = fh.read() if max_bytes is None else fh.read(max_bytes)
    return data.decode("utf-8", errors="replace")


def list_dir_beneath(root, rel_path=".", *, owner_uid=None):
    fd = open_beneath(root, rel_path, want_dir=True, owner_uid=owner_uid)
    try:
        return os.listdir(fd)
    finally:
        # os.listdir(fd) does NOT close the fd (unlike os.fdopen in
        # read_text_beneath) — the caller retains ownership and must close it.
        os.close(fd)
