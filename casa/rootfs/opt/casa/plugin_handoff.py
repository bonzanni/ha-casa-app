"""The plugin file handoff folder — provisioning and sweep (#486).

The contract a producer and a consumer follow is :mod:`casa_handoff` (plugins
vendor that file). This module is Casa's side of it: create the folder at boot
and keep it within its rules.

The sweep, once at boot and hourly:

* removes a published ``<id>/`` seven days after the epoch in its name — the
  name, not ``mtime``, so nothing re-dates a file — and never before;
* removes a ``.staging-<id>/`` an hour old (an abandoned publication);
* removes anything that does not conform, logged at WARN: a non-directory where
  a directory belongs, an off-pattern name, an ``<id>/`` that does not hold
  exactly one regular single-link file under a valid name, a symlink at any
  level;
* keeps a conforming ``<id>/`` dated more than an hour in the future, with a
  WARN — a backward clock correction must not delete a legitimate file;
* logs WARN when the folder is over :data:`casa_handoff.MAX_TOTAL_BYTES`. It
  never deletes a conforming young file to make room — refusal is the
  producer's (:func:`casa_handoff.publish`).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import time

import casa_handoff as ch

logger = logging.getLogger(__name__)

STAGING_MAX_AGE_S = 3600
FUTURE_SKEW_S = 3600
SWEEP_INTERVAL_H = 1

_root: str | None = None


def provision(root: str) -> None:
    os.makedirs(root, mode=0o770, exist_ok=True)
    st = os.lstat(root)
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"handoff path is not a real directory: {root}")
    if stat.S_IMODE(st.st_mode) != 0o770:
        os.chmod(root, 0o770)


def _remove(path: str, st: os.stat_result, why: str) -> None:
    logger.warning("handoff: removing %s (%s)", path, why)
    try:
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(path)       # does not follow symlinks inside the tree
        else:
            os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("handoff: could not remove %s: %s", path, exc)


def _lstat(path: str) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _conforming_file(id_dir: str) -> int | None:
    """The size of the one file ``id_dir`` holds, or None if it does not hold
    exactly one regular single-link file under a valid name."""
    try:
        entries = os.listdir(id_dir)
    except OSError:
        return None
    if len(entries) != 1 or not ch.valid_filename(entries[0]):
        return None
    st = _lstat(os.path.join(id_dir, entries[0]))
    if st is None or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        return None
    return st.st_size


def sweep(root: str, now: float | None = None) -> dict[str, int]:
    now_s = time.time() if now is None else now
    stats = {"expired": 0, "abandoned": 0, "foreign": 0, "kept": 0, "bytes": 0}
    for pname in sorted(os.listdir(root)):
        ppath = os.path.join(root, pname)
        pst = _lstat(ppath)
        if pst is None:
            continue
        if not stat.S_ISDIR(pst.st_mode) or not ch.PRODUCER_RE.match(pname):
            _remove(ppath, pst, "not a producer directory")
            stats["foreign"] += 1
            continue
        for ename in os.listdir(ppath):
            epath = os.path.join(ppath, ename)
            est = _lstat(epath)
            if est is None:
                continue
            is_dir = stat.S_ISDIR(est.st_mode)
            if ename.startswith(ch.STAGING_PREFIX):
                epoch = ch.id_epoch_s(ename[len(ch.STAGING_PREFIX):])
                if not is_dir or epoch is None:
                    _remove(epath, est, "not a staging directory")
                    stats["foreign"] += 1
                elif now_s - epoch >= STAGING_MAX_AGE_S:
                    _remove(epath, est, "abandoned publication")
                    stats["abandoned"] += 1
                continue
            epoch = ch.id_epoch_s(ename)
            size = _conforming_file(epath) if is_dir and epoch is not None else None
            if size is None:
                _remove(epath, est, "not a published file")
                stats["foreign"] += 1
            elif now_s - epoch >= ch.RETENTION_S:
                try:
                    shutil.rmtree(epath)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    logger.warning("handoff: could not remove %s: %s", epath, exc)
                stats["expired"] += 1
            else:
                if epoch - now_s > FUTURE_SKEW_S:
                    # Kept: a backward clock correction after publication
                    # must not delete a legitimate file early.
                    logger.warning("handoff: %s is dated in the future", epath)
                stats["kept"] += 1
                stats["bytes"] += size
    if stats["bytes"] > ch.MAX_TOTAL_BYTES:
        logger.warning("handoff: folder holds %d bytes, over its %d-byte limit",
                       stats["bytes"], ch.MAX_TOTAL_BYTES)
    return stats


def root() -> str | None:
    """The provisioned folder, or None when boot could not create it."""
    return _root


async def sweep_job() -> None:
    if _root is None:
        return
    try:
        await asyncio.to_thread(sweep, _root)
    except Exception:  # noqa: BLE001 — a failed pass waits for the next
        logger.warning("handoff: sweep failed", exc_info=True)


def register_sweep(scheduler) -> None:
    scheduler.add_job(
        sweep_job, trigger="interval", id="plugin_handoff_sweep",
        hours=SWEEP_INTERVAL_H, replace_existing=True, coalesce=True,
        max_instances=1, misfire_grace_time=3600,
    )


async def wire(scheduler, root_path: str) -> None:
    """Boot wiring: provision, sweep once, register the hourly sweep.

    Never blocks boot. If the folder cannot be created it is absent: Casa
    reports it unavailable and a producer gets ``handoff_unavailable``. Once it
    exists it is available — plugins publish into any folder that exists — so a
    failed first sweep or registration is logged, never turned into "absent"."""
    global _root
    try:
        await asyncio.to_thread(provision, root_path)
    except Exception:  # noqa: BLE001
        logger.warning("plugin handoff folder %s could not be provisioned; "
                       "file handoff disabled", root_path, exc_info=True)
        return
    _root = root_path
    try:
        await asyncio.to_thread(sweep, root_path)
    except Exception:  # noqa: BLE001 — the hourly sweep tries again
        logger.warning("handoff: boot sweep failed", exc_info=True)
    try:
        register_sweep(scheduler)
    except Exception:  # noqa: BLE001
        logger.warning("handoff: hourly sweep could not be registered; the "
                       "folder is swept at boot only", exc_info=True)


def _reset_for_tests() -> None:
    global _root
    _root = None
