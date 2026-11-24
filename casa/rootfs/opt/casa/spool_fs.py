"""Low-level file plumbing shared by the two durable spools, ``callback_spool``
and ``event_spool``: the canonical on-disk marker encoding, the openat-relative
directory, listing and ``lstat`` helpers, the per-directory ``.dir-id`` identity
token probe, and the directory mode and open flags they use.

Only the helpers whose behaviour is the same in both spools live here; anything
that logs, raises a spool's own exception type or reads a spool's own state
stays in that spool. Standard library only: a plugin imports
``callback_spool`` from ``/opt/casa`` at runtime, and this module with it.
"""

from __future__ import annotations

import errno
import json
import math
import os
import re
import stat

DIR_ID_NAME = ".dir-id"      # per-plugin-dir identity token (see callback_spool.Claim)
REPLACE_TEMP_INFIX = ".tmp-"  # staging grammar of _replace_json: `.<name>.tmp-…`

DIR_MODE = 0o770

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
#: Marker reads are NON-BLOCKING: O_NONBLOCK means opening a FIFO (or any other
#: pipe/device masquerading as ready.json) returns immediately instead of
#: blocking for a writer, so a hostile/garbage on-disk marker can never hang
#: the reconciler; the ``S_ISREG`` gate then rejects it before any read.
_MARKER_READ_FLAGS = (os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
                      | os.O_CLOEXEC)
_DIR_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: Sentinel for a token whose state could not be ESTABLISHED (an I/O failure
#: mid-probe). Distinct from ``None`` (positively absent or malformed) for the
#: same reason ``callback_spool._LSTAT_ERROR`` is distinct from ENOENT: an
#: unknowable state is never grounds for the repair path to retire a
#: possibly-valid token.
_TOKEN_ERROR = object()


def canonical_marker_bytes(payload: dict) -> bytes:
    """The ONE canonical on-disk form of a durable marker's payload — the exact
    bytes each spool's marker writer (``_replace_json``, i.e. ``write_ready``)
    emits, and the exact bytes the reconcile compares an on-disk marker
    against.

    Sorted keys + the most compact separators + no ASCII-escaping, UTF-8. Because
    writer and compare share this single helper, casa's own freshly-written
    marker is always byte-identical to ``canonical_marker_bytes(desired)`` — a
    steady-state pass finds it unchanged (no churn, no serialization-drift
    footgun) while ANY on-disk drift (a key reorder, a ``true``/``1.0`` type
    diff, an extra key, a whitespace diff) differs and is rewritten.

    ``allow_nan=False`` (in lockstep with ``callback_attempts._canonical_text``,
    its pure-side twin): a non-finite float would otherwise be emitted as the
    NON-STANDARD ``NaN``/``Infinity`` literal — bytes no conforming reader
    accepts and no fail-closed validator takes back, so the record would be
    write-only. Raising here instead is what every writer already handles
    (``TypeError``/``ValueError`` ⇒ the write is refused, nothing is
    published)."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _is_replace_temp(name: str) -> bool:
    """True for ``_replace_json`` / ``_strict_replace_at`` staging residue
    (``.ready.json.tmp-<pid>-<uuid>``, ``.<name>.tmp-…``) — a crash between
    staging and the rename leaves one, and nothing else would ever remove
    it."""
    return name.startswith(".") and REPLACE_TEMP_INFIX in name


def _is_clock(value) -> bool:
    """A usable clock field: a real, FINITE number (never a bool). Finiteness
    is not pedantry — a ``NaN`` slipped into ``ts``/``noted_ts`` makes every
    age comparison False, which would make the record immortal in exactly the
    store whose whole point is a hard age bound."""
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _open_dir(name: str, dir_fd: int) -> int:
    return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)


def _lstat_quiet(name: str, dir_fd: int):
    try:
        return os.lstat(name, dir_fd=dir_fd)
    except OSError:
        return None


def _listdir_quiet(dir_fd: int) -> list[str]:
    try:
        return os.listdir(dir_fd)
    except OSError:
        return []


def _mkdir(name: str, dir_fd: int) -> bool:
    """Create *name* if absent; True when this call created it (so the
    caller can fsync the parent exactly once, when it matters)."""
    try:
        os.mkdir(name, DIR_MODE, dir_fd=dir_fd)
        return True
    except FileExistsError:
        return False


def _classify_dir_token(dir_fd: int):
    """Three-state probe of the plugin dir's identity token (``.dir-id``):
    a valid token string; ``None`` when the entry is POSITIVELY absent or
    malformed (non-regular, wrong size, bad grammar — safe to repair); or
    :data:`_TOKEN_ERROR` when an ``open``/``fstat``/``read`` failure left its
    state unknowable (never safe to repair). A stat-pair compare alone cannot
    carry directory identity — ext4 recycles a freed inode number
    immediately, so a recreated directory can present the exact
    ``(st_dev, st_ino)`` of the one it replaced."""
    try:
        fd = os.open(DIR_ID_NAME, _MARKER_READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None                          # positively absent
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None                      # positively a symlink — malformed
        return _TOKEN_ERROR
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size != 32:
            return None                      # positively malformed
        chunks = bytearray()
        while len(chunks) < 33:
            piece = os.read(fd, 33 - len(chunks))
            if not piece:
                break
            chunks += piece
        # A regular 32-byte file with no legitimate concurrent writer must
        # read back exactly 32 bytes; anything else means the probe raced
        # something and the state is unknowable, not proven malformed.
        if len(chunks) != 32:
            return _TOKEN_ERROR
    except OSError:
        return _TOKEN_ERROR
    finally:
        os.close(fd)
    token = chunks.decode("ascii", errors="replace")
    return token if _DIR_ID_RE.match(token) else None
