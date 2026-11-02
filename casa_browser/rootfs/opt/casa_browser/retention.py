"""Retention: closed runs and everything attached to them expire after ``retention_days``.

One recoverable sequence. First, in a single store transaction, every private file and
screenshot path of each expired run is queued in ``pending_deletions`` and the run rows
(events, records, file and screenshot metadata) are deleted. Then each queued path is
unlinked and dequeued. A crash between the two halves leaves the paths queued, and the
next sweep retries them, so no expired file outlives its metadata unnoticed.

Never deleted by retention: registered sites, pairing state, profiles, and runs
that have not ended. A queued path is unlinked only when it lies inside the artifacts
directory; a symlink is removed as a link and never followed, so a planted link cannot
make the sweep delete anything outside ``/data/artifacts``.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .options import Paths
from .store import Store


@dataclass(frozen=True)
class SweepResult:
    runs_deleted: int
    paths_deleted: int
    paths_refused: int
    paths_retry: int


def _inside(root: Path, candidate: Path) -> bool:
    """True when ``candidate``'s parent directory resolves inside ``root`` (the final
    component itself is not resolved: a symlink there is removed, not followed)."""
    try:
        parent = candidate.parent.resolve(strict=False)
        base = root.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return parent == base or base in parent.parents


def _unlink_one(root: Path, raw: str) -> str:
    """'deleted' | 'refused' | 'retry'."""
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts or not _inside(root, path):
        return "refused"
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "deleted"
    except OSError:
        return "retry"
    if stat.S_ISDIR(st.st_mode):
        return "refused"
    try:
        os.unlink(path)  # removes a symlink itself, never its target
    except FileNotFoundError:
        return "deleted"
    except OSError:
        return "retry"
    return "deleted"


def sweep(store: Store, now: datetime, retention_days: int,
          artifacts_root: Path = Paths().artifacts) -> SweepResult:
    if not isinstance(retention_days, int) or retention_days < 1:
        raise ValueError("retention_days must be a positive integer")
    if artifacts_root.is_symlink():
        # A symlinked root would make every "inside" check resolve elsewhere.
        raise ValueError("artifacts root must not be a symlink")
    cutoff = now - timedelta(days=retention_days)
    expired = store.expired_run_ids(cutoff)
    deleted_runs = store.delete_runs_queue_files(expired, cutoff)
    deleted = refused = retry = 0
    for raw in store.pending_deletions():
        outcome = _unlink_one(artifacts_root, raw)
        if outcome == "retry":
            retry += 1
            continue
        if outcome == "deleted":
            deleted += 1
        else:
            refused += 1
        store.clear_pending_deletion(raw)
    return SweepResult(deleted_runs, deleted, refused, retry)
