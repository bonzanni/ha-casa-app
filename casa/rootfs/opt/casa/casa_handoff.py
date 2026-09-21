"""The plugin file handoff contract (#486) — publish and capture.

Casa owns one folder, ``/data/handoff`` (``CASA_HANDOFF_DIR``), through which a
file moves from one plugin to another, or from Casa to a plugin. A producer
publishes a file and returns its path; a consumer that accepts handoff files takes
it with :func:`capture`. Reading never deletes: Casa's sweep removes a file seven
days after publication.

Layout::

    <root>/<producer>/<id>/<filename>        a published file
    <root>/<producer>/.staging-<id>/...      a publication in progress

``<id>`` is ``<13-digit epoch ms>-<16 hex>``; the epoch is the age clock.
``<filename>`` is the human name, so an emailed attachment keeps it.

The one outcome :func:`capture` exists to prevent is a file OUTSIDE the folder
being taken as a handoff file — a credential mailed out because a path resolved
somewhere else. It resolves the path, requires the exact layout under the root,
opens without following a final link, requires a regular single-link file on the
opened descriptor, and reads from that same descriptor. The caller uses the bytes
it returns and never opens the path again.

What this does NOT defend against, stated plainly: every in-Casa plugin runs as
the same user as Casa and can read any file directly. Per-producer directories are
a convention between honest plugins, not an access control. Credentials never
transit this folder — that is a rule for plugin authors; nothing here inspects
content.

This file is stdlib-only and self-contained on purpose: plugins copy it
verbatim into their own server code, so a plugin needs nothing from Casa's
install layout.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import time
import unicodedata

HANDOFF_ENV = "CASA_HANDOFF_DIR"
DEFAULT_ROOT = "/data/handoff"

#: Per file — Gmail's own attachment limit.
MAX_FILE_BYTES = 25_000_000
#: The whole folder. Enforced by the producer before it writes; Casa never
#: evicts to make room.
MAX_TOTAL_BYTES = 2_000_000_000
#: A publication is refused if it would leave less than this free on the
#: filesystem that holds the folder.
FREE_RESERVE_BYTES = 128 * 1024 * 1024
#: Age at which the sweep removes a published file.
RETENTION_S = 7 * 24 * 3600

MAX_NAME_CHARS = 128
#: The filesystem's component limit is 255 bytes; stay well under it.
MAX_NAME_BYTES = 200

STAGING_PREFIX = ".staging-"
PRODUCER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ID_RE = re.compile(r"^(\d{13})-[0-9a-f]{16}$")

_CHUNK = 1024 * 1024


class HandoffError(Exception):
    """A refusal carrying a stable ``kind``:

    ``handoff_unavailable`` (no folder — Casa too old or not provisioned),
    ``bad_producer``, ``file_too_large``, ``handoff_full`` (folder total or
    free-space reserve), ``storage_failed``, ``not_a_handoff_file``."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def root_dir() -> str:
    return os.environ.get(HANDOFF_ENV) or DEFAULT_ROOT


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def valid_filename(name: str) -> bool:
    """True iff ``name`` is a filename this folder may hold."""
    return (
        isinstance(name, str)
        and 0 < len(name) <= MAX_NAME_CHARS
        and len(name.encode("utf-8")) <= MAX_NAME_BYTES
        and not name.startswith(".")
        and "/" not in name
        and not any(unicodedata.category(c) == "Cc" for c in name)
    )


def _shorten(stem: str, ext: str) -> str:
    budget_chars = MAX_NAME_CHARS - len(ext)
    budget_bytes = MAX_NAME_BYTES - len(ext.encode("utf-8"))
    out = stem[:max(0, budget_chars)]
    while out and len(out.encode("utf-8")) > budget_bytes:
        out = out[:-1]
    return out + ext


def clean_filename(name: str | None, fallback: str) -> str:
    """Turn a human-supplied name into one this folder may hold: control
    characters dropped, separators replaced, leading dots stripped, shortened at
    a character boundary (keeping the extension) to fit both bounds. An empty
    result becomes ``fallback``, which must itself be valid."""
    s = "".join(c for c in (name or "") if unicodedata.category(c) != "Cc")
    s = s.replace("/", "_").replace("\\", "_").strip().lstrip(".").strip()
    if s:
        stem, ext = os.path.splitext(s)
        if len(ext) > 16 or not stem:
            stem, ext = s, ""
        s = _shorten(stem, ext)
    if not valid_filename(s):
        s = fallback
    if not valid_filename(s):
        raise ValueError(f"fallback filename is not valid: {fallback!r}")
    return s


def new_id(now: float | None = None) -> str:
    ms = int((time.time() if now is None else now) * 1000)
    return f"{ms:013d}-{secrets.token_hex(8)}"


def id_epoch_s(ident: str) -> float | None:
    m = ID_RE.match(ident)
    return int(m.group(1)) / 1000.0 if m else None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def read_regular(path: str, cap: int | None = None) -> bytes:
    """Open ``path`` without following a final link and read it, provided the
    OPENED file is regular, single-link and within ``cap``. Raises
    ``HandoffError("not_a_handoff_file")`` or ``("file_too_large")``.

    ``O_NONBLOCK`` keeps a FIFO from hanging the open; ``fstat`` then refuses it.
    """
    cap = MAX_FILE_BYTES if cap is None else cap
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise HandoffError("not_a_handoff_file", f"cannot open {path}: {exc.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise HandoffError("not_a_handoff_file", f"{path} is not a plain file")
        if st.st_size > cap:
            raise HandoffError("file_too_large", f"{path} is larger than {cap} bytes")
        chunks: list[bytes] = []
        total = 0
        while total <= cap:
            block = os.read(fd, min(_CHUNK, cap + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
        if total > cap:
            raise HandoffError("file_too_large", f"{path} is larger than {cap} bytes")
        return b"".join(chunks)
    finally:
        os.close(fd)


def capture(path: str, *, root: str | None = None) -> tuple[str, bytes]:
    """Take a handoff file: ``(filename, bytes)``. The ONLY way a consumer reads
    one — use the bytes returned and never open ``path`` again.

    Refuses (``not_a_handoff_file``) anything that does not resolve to exactly
    ``<root>/<producer>/<id>/<filename>``: ``..``, look-alike prefixes, staging
    paths, a directory link pointing out of the folder, a final symlink, a hard
    link, a FIFO or device. ``file_too_large`` above :data:`MAX_FILE_BYTES`."""
    if not isinstance(path, str) or not os.path.isabs(path):
        raise HandoffError("not_a_handoff_file", f"not an absolute path: {path!r}")
    real_root = os.path.realpath(root or root_dir())
    resolved = os.path.realpath(path)
    rel = os.path.relpath(resolved, real_root)
    parts = rel.split(os.sep)
    # A path resolving outside the root starts with ".." — which the producer
    # pattern can never match, so the layout check alone refuses it.
    if (len(parts) != 3
            or not PRODUCER_RE.match(parts[0])
            or not ID_RE.match(parts[1])
            or not valid_filename(parts[2])):
        raise HandoffError("not_a_handoff_file",
                           f"{path} is not a file in the handoff folder")
    # realpath resolved any link in the path; open the RESOLVED path so the
    # descriptor is the file the layout check approved, not a later lookup.
    return parts[2], read_regular(resolved)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def _folder_bytes(root: str) -> int:
    total = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(e.path)
                    elif stat.S_ISREG(st.st_mode):
                        total += st.st_size
        except OSError:
            continue
    return total


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_room(root: str, size: int) -> None:
    if _folder_bytes(root) + size > MAX_TOTAL_BYTES:
        raise HandoffError(
            "handoff_full",
            "the handoff folder is full; files are removed 7 days after they "
            "are published")
    vfs = os.statvfs(root)
    if vfs.f_bavail * vfs.f_frsize - size < FREE_RESERVE_BYTES:
        raise HandoffError("handoff_full", "the disk holding the handoff folder is nearly full")


def publish(producer: str, filename: str, *, data: bytes | None = None,
            src: str | None = None, root: str | None = None,
            now: float | None = None) -> dict:
    """Publish one file as ``producer``; exactly one of ``data`` / ``src``.

    ``src`` is a file the producer owns; it is read through :func:`read_regular`
    and its BYTES are written — a source is never linked or renamed into the
    folder. ``filename`` is cleaned with :func:`clean_filename` (fallback
    ``file``). Returns ``{path, filename, size_bytes, expires_at}``; the file is
    durable once this returns. Raises :class:`HandoffError`."""
    if (data is None) == (src is None):
        raise ValueError("pass exactly one of data= or src=")
    if not isinstance(producer, str) or not PRODUCER_RE.match(producer):
        raise HandoffError("bad_producer", f"not a producer name: {producer!r}")
    root = root or root_dir()
    try:
        st = os.lstat(root)
    except OSError:
        st = None
    if st is None or not stat.S_ISDIR(st.st_mode):
        raise HandoffError("handoff_unavailable",
                           f"the handoff folder {root} does not exist — this "
                           "needs a Casa version with the plugin file handoff")
    if src is not None:
        data = read_regular(src)
    assert data is not None
    if len(data) > MAX_FILE_BYTES:
        raise HandoffError("file_too_large",
                           f"files in the handoff folder are limited to {MAX_FILE_BYTES} bytes")
    name = clean_filename(filename, "file")
    _check_room(root, len(data))

    pdir = os.path.join(root, producer)
    staging = None
    try:
        os.makedirs(pdir, mode=0o770, exist_ok=True)
        pst = os.lstat(pdir)
        if not stat.S_ISDIR(pst.st_mode):
            raise HandoffError("storage_failed", f"{pdir} is not a directory")
        published_at = time.time() if now is None else now
        ident = new_id(published_at)
        staging = os.path.join(pdir, STAGING_PREFIX + ident)
        os.mkdir(staging, 0o770)
        fd = os.open(os.path.join(staging, name),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o660)
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_dir(staging)
        final = os.path.join(pdir, ident)
        os.rename(staging, final)
        staging = None
        _fsync_dir(pdir)
    except HandoffError:
        raise
    except OSError as exc:
        raise HandoffError("storage_failed", f"could not publish {name}: {exc.strerror}") from None
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    return {
        "path": os.path.join(final, name),
        "filename": name,
        "size_bytes": len(data),
        "expires_at": int(published_at + RETENTION_S),
    }
