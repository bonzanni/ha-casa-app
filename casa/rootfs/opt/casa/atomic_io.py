"""Crash-safe atomic file writes.

Route on-disk state writes (registries, tombstones, manifests) through these
helpers so a crash or power loss mid-write can never leave a truncated or
partially-written file. Each helper writes to a temporary file *in the same
directory* as the target — so :func:`os.replace` is a same-filesystem atomic
rename, not a cross-device copy — flushes and ``os.fsync``s the temp file's
data to disk, then ``os.replace``s it over the target.

Deliberately tiny and dependency-free (stdlib only): these are called from
sync code, often via :func:`asyncio.to_thread`. If the write fails at any
point before the final replace, the original target file is left untouched
and the temp file is cleaned up.

After a successful replace the containing directory is fsynced too (#330 /
#341 root cause): fsyncing only the temp file makes the *data* durable but
not the *rename* — across a power crash the new directory entry can be
missing while a later write (e.g. a registry entry referencing this file)
survived, breaking write-ordering assumptions everywhere these helpers are
used. The directory fsync is best-effort: at that point the content is
already committed and callers roll back in-memory state on exceptions, so
misreporting a completed write as failed would be strictly worse than the
lost ordering guarantee (which only matters across a power crash).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any

logger = logging.getLogger(__name__)

#: Mode for state that only root may read. Pass it explicitly — the ``mode=None``
#: default below is deliberately world-readable and must stay that way, because
#: the same helper writes ``/config`` artifacts (the plugin registry and store)
#: that a uid-dropped engagement has to load. See ``private_state`` for the
#: inventory of which paths are private and GHSA-569r-7crq-xr43 for why.
PRIVATE = 0o600


def fsync_directory(directory: str | os.PathLike[str]) -> None:
    """Best-effort fsync of *directory* so a just-committed rename in it is
    durable across a power crash. Failures are logged, never raised — see the
    module docstring for why."""
    try:
        fd = os.open(os.fspath(directory), os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        logger.warning("directory fsync open(%s) failed: %s", directory, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        logger.warning("directory fsync (%s) failed: %s", directory, exc)
    finally:
        os.close(fd)


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """Atomically write *text* to *path*.

    Writes a sidecar temp file in the same directory, fsyncs it, then
    ``os.replace``s it over *path*. When *mode* is given, the target file's
    permission bits are set to it (applied to the temp file before the
    replace so the mode is in effect the instant the file appears).

    When *mode* is ``None`` the prior ``open("w")`` permission semantics are
    preserved: rewriting an existing file keeps that file's current mode, and
    a fresh file lands at ``0o644``. This is necessary because
    :func:`tempfile.mkstemp` creates the sidecar at ``0o600`` and
    :func:`os.replace` adopts the temp inode — without this the atomic write
    would silently downgrade every replaced file to ``0o600``.
    """
    target = os.fspath(path)
    directory = os.path.dirname(target) or "."
    if mode is None:
        try:
            mode = os.stat(target).st_mode & 0o777
        except OSError:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".", suffix=".tmp")
    try:
        os.chmod(tmp, mode)
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        # Any failure before the replace leaves the original intact; drop
        # the orphaned temp file so a crashed write can't litter the dir.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_directory(directory)


def atomic_write_json(
    path: str | os.PathLike[str],
    data: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = False,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """Atomically write *data* as JSON to *path* (see :func:`atomic_write_text`)."""
    atomic_write_text(
        path,
        json.dumps(data, indent=indent, sort_keys=sort_keys),
        encoding=encoding,
        mode=mode,
    )
