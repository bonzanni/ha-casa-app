"""Deletes a finished in_casa engagement's CLI transcripts (#1162) once their
retention has passed, and a plugin job's working dir (#1170).

Casa owns transcript deletion — the CLI's own ``cleanupPeriodDays`` sweep is
never enabled by a Casa launch through the SDK (INV-MEM-021,
``docs/architecture/memory-lifecycle.md``), and Casa's resident sweep
(``session_sweeper``) only reaps resident sessions. An in_casa engagement
writes its sessions under ``$HOME/.claude/projects/<project dir named after
its cwd>``; once the engagement is terminal nothing resumes or reads them
again.

For each terminal ``driver == "in_casa"`` record this pass deletes, and selects
nothing else:

- ``kind == "plugin"``: the job's whole per-engagement project dir (its cwd is
  unique to the engagement), so every session it ran goes with it, and its
  working dir ``/data/engagements/<id>`` (``tools.plugin_job_cwd(id).parent``) —
  the latter needs no SDK lookup and does not depend on the project dir;
- specialist / executor: the session the record names (``sdk_session_id``;
  a job is one session, kept across resumes) as ``<sid>.jsonl`` (any size)
  and ``<sid>/``, in that record's own project dir.
  Those dirs are shared with other sessions, which are never selected.

Nothing is selected by the absence of a record or by listing a dir.
A record is acted on only once its terminal status is also on disk: an
in-memory terminal status whose write failed, or that a strict transition will
roll back, is not yet a decision (``_durable_terminal_ids``). Every pass
re-visits every terminal record — the ``transcripts_reaped`` mark below only
lets the tombstone age out, it skips nothing — so a session named after a
pass, or a removal that failed, is handled by the next one.

A record's transcripts are kept for ``TRANSCRIPT_RETENTION_DAYS`` after its
``completed_at`` (a record with no usable time keeps them), and then deleted
only where the run-confirmation gate passes. Once all of them are gone (or
the record names none this pass can find) the pass marks the record
``transcripts_reaped``, and only then may its tombstone age out; a held
transcript keeps it until a later pass deletes it. The gate
(``memory/transcript_runs.py``) passes while no run registered in the folder
is unconfirmed. It is asked at each delete; after a delete the rows that
named it go. The pass reads no transcript.

The project dir is resolved by the SDK's own (private) cwd → dir helpers,
imported per pass and never at module scope: if an SDK version no longer
provides them, the pass logs one WARNING and deletes no transcript, while
Casa boots and the job keeps its schedule (an import at module load would crash boot —
``casa_core.main`` imports this module before scheduling it).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass

from engagement_registry import LAUNCH_CWD_KEY

logger = logging.getLogger(__name__)

_TERMINAL = frozenset({"completed", "cancelled", "error"})
_ENGAGEMENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE)


@dataclass
class _Target:
    engagement_id: str
    cwd: str
    sids: tuple[str, ...] = ()      # empty for a plugin job: the dir goes whole
    whole_dir: bool = False
    workdir: str | None = None      # a plugin job's /data/engagements/<id>
    completed_at: object = None     # the record's terminal time, as persisted


def _retention_passed(completed_at: object, now: float) -> bool:
    """Whether a record's transcripts have outlived their retention: its
    ``completed_at`` is a usable time at least the retention ago."""
    from engagement_registry import _usable_time
    from memory import transcript_runs
    return (_usable_time(completed_at)
            and completed_at <= now - transcript_runs.retention_days() * 86400)


@dataclass
class _Counts:
    deleted: int = 0
    absent: int = 0
    skipped: int = 0
    unsettled: int = 0
    errors: int = 0
    retained: int = 0       # inside retention, kept for a later pass
    unconfirmed: int = 0    # kept: a run in it is not confirmed

    def as_dict(self) -> dict[str, int]:
        return {"deleted": self.deleted, "absent": self.absent,
                "skipped": self.skipped, "unsettled": self.unsettled,
                "errors": self.errors, "retained": self.retained,
                "unconfirmed": self.unconfirmed}


def _named_sids(rec) -> tuple[str, ...]:
    sid = rec.sdk_session_id
    return (sid,) if isinstance(sid, str) and _SID_RE.match(sid) else ()


def transcript_cwd(rec) -> str | None:
    """The working directory *rec*'s sessions ran in: the key its transcript
    folder is resolved from, and the directory its runs are registered
    under. The cwd persisted at the engagement's first
    open wins over anything derived from the current configuration, which a
    reload can change. ``None`` for an uninstalled specialist with no
    persisted cwd."""
    import tools

    launched = (getattr(rec, "origin", None) or {}).get(LAUNCH_CWD_KEY)
    if isinstance(launched, str) and launched:
        return launched
    if rec.kind == "plugin":
        return str(tools.plugin_job_cwd(rec.id))
    if rec.kind == "executor":
        return tools.EXECUTOR_CWD
    registry = tools._specialist_registry
    cfg = registry.get(rec.role_or_type) if registry is not None else None
    return tools.specialist_cwd(cfg) if cfg is not None else None


def _target_for(rec, counts: _Counts) -> _Target | None:
    import tools

    if not isinstance(rec.id, str) or not _ENGAGEMENT_ID_RE.match(rec.id):
        counts.skipped += 1
        return None
    if rec.kind == "plugin":
        cwd = tools.plugin_job_cwd(rec.id)
        return _Target(rec.id, str(cwd), whole_dir=True,
                       workdir=str(cwd.parent))
    cwd = transcript_cwd(rec)
    if cwd is None:
        # Uninstalled: its launch cwd can no longer be derived. Never
        # guessed — the sessions stay (disclosed residual).
        counts.skipped += 1
        return None
    return _Target(rec.id, cwd, sids=_named_sids(rec))


def _durable_terminal_ids(tombstone_path: str) -> set[str] | None:
    """Ids whose every row in the on-disk tombstone is terminal. ``None`` when
    the file cannot be read — the pass then selects nothing."""
    try:
        with open(tombstone_path, encoding="utf-8") as fh:
            rows = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(rows, list):
        return None
    terminal: set[str] = set()
    live: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        (terminal if row.get("status") in _TERMINAL else live).add(row["id"])
    return terminal - live


def _remove(path: str, counts: _Counts) -> None:
    if not os.path.lexists(path):
        counts.absent += 1
        return
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)
    counts.deleted += 1


def _ignore_vanished(func, path, exc) -> None:
    # 3.11 hands ``onerror`` an exc_info tuple, 3.12+ hands ``onexc`` the
    # exception. An entry that vanished mid-walk (a concurrent operator delete)
    # is skipped and the walk goes on; anything else propagates.
    err = exc[1] if isinstance(exc, tuple) else exc
    if not isinstance(err, FileNotFoundError):
        raise err


_RMTREE_TOLERANT = ({"onexc": _ignore_vanished} if sys.version_info >= (3, 12)
                    else {"onerror": _ignore_vanished})


def _remove_tree(path: str, counts: _Counts) -> None:
    """Remove a plugin job's working dir. Already gone counts nothing: every
    pass re-visits the record, and after the first removal it stays gone."""
    if not os.path.lexists(path):
        return
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, **_RMTREE_TOLERANT)
    else:
        try:
            os.unlink(path)
        except FileNotFoundError:
            return
    counts.deleted += 1


def _reap_transcripts(t: _Target, lookup, counts: _Counts) -> bool:
    """Delete *t*'s transcripts; whether all of them are now gone (removed,
    or already absent), so the record has nothing left to reap. A held one
    waits for a later pass."""
    from memory import transcript_runs
    canonicalize, find_project_dir = lookup
    project = find_project_dir(canonicalize(t.cwd))
    if t.whole_dir:
        if transcript_runs.blocked(directory=t.cwd):
            counts.unconfirmed += 1
            return False
        if project is None:
            counts.absent += 1
        else:
            _remove(str(project), counts)
        transcript_runs.forget_deleted(directory=t.cwd)
        return True
    done = True
    for sid in t.sids:
        if transcript_runs.blocked(directory=t.cwd, session_id=sid):
            counts.unconfirmed += 1
            done = False
            continue
        if project is None:
            counts.absent += 1
        else:
            _remove(os.path.join(project, f"{sid}.jsonl"), counts)
            sibling = os.path.join(project, sid)
            if os.path.lexists(sibling):
                _remove(sibling, counts)
        transcript_runs.forget_deleted(directory=t.cwd, session_id=sid)
    return done


def _reap(targets: list[_Target], tombstone_path: str, counts: _Counts,
          now: float | None = None,
          reaped: set[str] | None = None) -> None:
    """One pass over *targets*; the id of every record whose transcripts are
    all gone after it is added to *reaped*."""
    try:
        from claude_agent_sdk._internal.sessions import (
            _canonicalize_path,
            _find_project_dir,
        )
        lookup = (_canonicalize_path, _find_project_dir)
    except ImportError as exc:
        # Warned on every pass, whatever the targets; only the transcripts
        # need the lookup, so the pass goes on for the working dirs.
        lookup = None
        counts.errors += 1
        logger.warning(
            "engagement transcript reap: claude_agent_sdk._internal.sessions "
            "(_canonicalize_path, _find_project_dir) unavailable — no "
            "transcript deleted this pass: %s", exc)
    durable = _durable_terminal_ids(tombstone_path)
    if durable is None:
        # #1174: a never-written tombstone is the registry's healthy state;
        # unreadable is worth a warning only when a terminal record waits.
        if targets:
            logger.warning(
                "engagement transcript reap: tombstone %s unreadable — nothing "
                "selected this pass", tombstone_path)
        counts.unsettled += len(targets)
        return
    now = time.time() if now is None else now
    for t in targets:
        if t.engagement_id not in durable:
            counts.unsettled += 1
            continue
        # Each removal is its own failure: neither one gates the other.
        if t.workdir is not None:
            try:
                _remove_tree(t.workdir, counts)
            except Exception as exc:  # noqa: BLE001 — one record never stops the pass
                counts.errors += 1
                logger.warning(
                    "engagement transcript reap: %s working dir failed: %s",
                    t.engagement_id[:8], exc)
        if lookup is None:
            continue
        if not _retention_passed(t.completed_at, now):
            counts.retained += 1
            continue
        try:
            if _reap_transcripts(t, lookup, counts) and reaped is not None:
                reaped.add(t.engagement_id)
        except Exception as exc:  # noqa: BLE001 — one record never stops the pass
            counts.errors += 1
            logger.warning(
                "engagement transcript reap: %s failed: %s",
                t.engagement_id[:8], exc)


async def reap_engagement_transcripts(registry, *,
                                      now: float | None = None) -> dict[str, int]:
    """One pass. Scheduled every six hours, first at scheduler start. *now*
    (epoch seconds, default the current time) is when retention is judged."""
    counts = _Counts()
    # Records with nothing left to reap: their tombstones may now age out.
    reaped: set[str] = set()
    try:
        targets = []
        for rec in registry.terminal_records():
            if rec.driver != "in_casa":
                continue        # only in_casa sessions leave CLI transcripts here
            try:
                target = _target_for(rec, counts)
            except Exception:  # noqa: BLE001 — one record never stops the pass
                counts.errors += 1
                logger.warning("engagement transcript reap: %s skipped",
                               str(rec.id)[:8], exc_info=True)
                continue
            if target is None:
                reaped.add(rec.id)       # nothing this pass could ever reap
                continue
            target.completed_at = rec.completed_at
            targets.append(target)
        await asyncio.to_thread(_reap, targets, registry._tombstone_path, counts,
                                now, reaped)
        mark = getattr(registry, "mark_transcripts_reaped", None)
        if reaped and callable(mark):
            await mark(reaped)
    except Exception:  # noqa: BLE001 — the scheduled job must survive
        counts.errors += 1
        logger.warning("engagement transcript reap failed", exc_info=True)
    if counts.deleted or counts.skipped or counts.errors:
        logger.info("engagement transcript reap: %s", counts.as_dict())
    return counts.as_dict()
