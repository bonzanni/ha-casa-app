"""Deletes a finished in_casa engagement's CLI transcripts (#1162) and a
plugin job's working dir (#1170).

Casa owns transcript deletion — whether the CLI's own ``cleanupPeriodDays``
sweep removes anything under Casa is stated beside INV-MEM-017
(``docs/architecture/memory-lifecycle.md``), and Casa's resident sweep
(``session_sweeper``) only reaps resident sessions. An in_casa engagement
writes its sessions under ``$HOME/.claude/projects/<project dir named after
its cwd>``; once the engagement is terminal nothing resumes or reads them
again.

For each terminal ``driver == "in_casa"`` record this pass deletes, and selects
nothing else:

- ``kind == "plugin"``: the job's whole per-engagement project dir (its cwd is
  unique to the engagement), so every batch's session goes with it, and its
  working dir ``/data/engagements/<id>`` (``tools.plugin_job_cwd(id).parent``) —
  the latter needs no SDK lookup and does not depend on the project dir;
- specialist / executor: each session the record names —
  ``sdk_session_id``, every sid in ``origin["job"][JOB_SIDS_KEY]`` and every sid
  a clearance downgrade retired (``origin[RETIRED_SIDS_KEY]``, #1167) — as
  ``<sid>.jsonl`` (any size) and ``<sid>/``, in that record's own project dir.
  Those dirs are shared with other sessions, which are never selected.

Nothing is selected by age, by the absence of a record, or by listing a dir.
A record is acted on only once its terminal status is also on disk: an
in-memory terminal status whose write failed, or that a strict transition will
roll back, is not yet a decision (``_durable_terminal_ids``). Every pass
re-visits every terminal record — there is no "reaped" marker — so a session
named after a pass, or a removal that failed, is handled by the next one.

The project dir is resolved by the SDK's own (private) cwd → dir helpers,
imported per pass and never at module scope: if an SDK version no longer
provides them, the pass logs one WARNING and deletes no transcript, while
Casa boots and the job keeps its schedule (an import at module load would crash boot —
``casa_core.main`` imports this module before scheduling it).

Takes neither ``RetainFence`` nor ``TURN_ADMISSION``: it deletes local files
and retains nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass

from engagement_registry import JOB_SIDS_KEY, RETIRED_SIDS_KEY

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


@dataclass
class _Counts:
    deleted: int = 0
    absent: int = 0
    skipped: int = 0
    unsettled: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"deleted": self.deleted, "absent": self.absent,
                "skipped": self.skipped, "unsettled": self.unsettled,
                "errors": self.errors}


def _named_sids(rec) -> tuple[str, ...]:
    origin = rec.origin or {}
    job = origin.get("job")
    listed = job.get(JOB_SIDS_KEY) if isinstance(job, dict) else None
    retired = origin.get(RETIRED_SIDS_KEY)
    candidates = [rec.sdk_session_id,
                  *(listed if isinstance(listed, list) else ()),
                  *(retired if isinstance(retired, list) else ())]
    # Filtered before the set: a malformed element must not hide the rest.
    return tuple(sorted({s for s in candidates
                         if isinstance(s, str) and _SID_RE.match(s)}))


def _target_for(rec, counts: _Counts) -> _Target | None:
    import tools

    if not isinstance(rec.id, str) or not _ENGAGEMENT_ID_RE.match(rec.id):
        counts.skipped += 1
        return None
    if rec.kind == "plugin":
        cwd = tools.plugin_job_cwd(rec.id)
        return _Target(rec.id, str(cwd), whole_dir=True,
                       workdir=str(cwd.parent))
    if rec.kind == "executor":
        cwd = tools.EXECUTOR_CWD
    else:
        registry = tools._specialist_registry
        cfg = registry.get(rec.role_or_type) if registry is not None else None
        if cfg is None:
            # Uninstalled: its launch cwd can no longer be derived. Never
            # guessed — the sessions stay (disclosed residual).
            counts.skipped += 1
            return None
        cwd = tools.specialist_cwd(cfg)
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


def _reap_transcripts(t: _Target, lookup, counts: _Counts) -> None:
    canonicalize, find_project_dir = lookup
    project = find_project_dir(canonicalize(t.cwd))
    if project is None:
        counts.absent += 1 if t.whole_dir else len(t.sids)
        return
    if t.whole_dir:
        _remove(str(project), counts)
        return
    for sid in t.sids:
        _remove(os.path.join(project, f"{sid}.jsonl"), counts)
        sibling = os.path.join(project, sid)
        if os.path.lexists(sibling):
            _remove(sibling, counts)


def _reap(targets: list[_Target], tombstone_path: str, counts: _Counts) -> None:
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
        try:
            _reap_transcripts(t, lookup, counts)
        except Exception as exc:  # noqa: BLE001 — one record never stops the pass
            counts.errors += 1
            logger.warning(
                "engagement transcript reap: %s failed: %s",
                t.engagement_id[:8], exc)


async def reap_engagement_transcripts(registry) -> dict[str, int]:
    """One pass. Scheduled every six hours, first at scheduler start."""
    counts = _Counts()
    try:
        targets = []
        for rec in registry.terminal_records():
            if rec.driver != "in_casa":
                continue        # claude_code: its workspace holds the transcript
            try:
                target = _target_for(rec, counts)
            except Exception:  # noqa: BLE001 — one record never stops the pass
                counts.errors += 1
                logger.warning("engagement transcript reap: %s skipped",
                               str(rec.id)[:8], exc_info=True)
                continue
            if target is not None:
                targets.append(target)
        await asyncio.to_thread(_reap, targets, registry._tombstone_path, counts)
    except Exception:  # noqa: BLE001 — the scheduled job must survive
        counts.errors += 1
        logger.warning("engagement transcript reap failed", exc_info=True)
    if counts.deleted or counts.skipped or counts.errors:
        logger.info("engagement transcript reap: %s", counts.as_dict())
    return counts.as_dict()
