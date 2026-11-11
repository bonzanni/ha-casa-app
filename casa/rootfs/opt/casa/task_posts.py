"""The posts of every task, filed under the task that made them.

Every message a task posts — its start line, each page of its answer, every
intermediate plugin post (a proposal, a link, a file, a run card) — is filed
under that task, so a reply to any of them finds the task: a job's posts in
the engagement registry, a delegation's in the job registry. The index entry
exists before the task's first post (:func:`open`).

Both registries keep their posts on the durable record and rebuild their
indices at load, keyed by bot, chat and message, so a reply finds its task
after a restart and two bots' equal message ids never meet.
"""
from __future__ import annotations

import collections
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from channels.tg_address import TgAddress

if TYPE_CHECKING:
    from task_model import Requester

logger = logging.getLogger(__name__)

#: How many opened tasks' deliveries are remembered (memory-only).
OPENED_MAX = 512


@dataclass(frozen=True)
class TaskRef:
    """The task that owns a message: its kind and id, its executor, and who
    receives its result."""
    kind: Literal["job", "delegation"]
    task_id: str
    executor: str
    requester: "Requester"


_opened: "collections.OrderedDict[str, TgAddress]" = collections.OrderedDict()


def _registries() -> tuple[Any, Any]:
    """The engagement registry (jobs) and the job registry (delegations) the
    running Casa holds; either is ``None`` before boot wires it."""
    import tools
    engagements = getattr(tools, "_engagement_registry", None)
    specialists = getattr(tools, "_specialist_registry", None)
    jobs = getattr(specialists, "job_registry", None) if specialists is not None else None
    return engagements, jobs


def _kind_of(task_id: str) -> tuple[str | None, Any]:
    """``("job", record)``, ``("delegation", row)`` or ``(None, None)``."""
    engagements, jobs = _registries()
    rec = engagements.get(task_id) if engagements is not None else None
    if rec is not None and isinstance(getattr(rec, "origin", None), dict) \
            and isinstance(rec.origin.get("job"), dict):
        return "job", rec
    row = jobs.get(task_id) if jobs is not None else None
    if row is not None:
        return "delegation", row
    return None, None


def open(task_id: str, delivery: "TgAddress") -> None:  # noqa: A001 — the index's verb
    """Open the index entry of the task *task_id*, delivered at *delivery*,
    before its first post. The task's record must already be held by its
    registry, so its first post is filed the moment it lands."""
    if not isinstance(delivery, TgAddress):
        raise TypeError("a task's posts need a delivery address")
    kind, _ = _kind_of(task_id)
    if kind is None:
        logger.error("task %s opened before its record exists: its posts "
                     "are not filed", str(task_id)[:8])
        return
    _opened[task_id] = delivery
    _opened.move_to_end(task_id)
    while len(_opened) > OPENED_MAX:
        _opened.popitem(last=False)


def delivery_of(task_id: str) -> "TgAddress | None":
    """Where the opened task *task_id* posts, else ``None``."""
    return _opened.get(task_id)


def record(owner: str, addr: "TgAddress") -> None:
    """File the landed message *addr* under the task *owner*: a job's on its
    engagement record, a delegation's on its job row. Anything else files
    nothing. Never raises: filing never fails a post."""
    if not isinstance(owner, str) or not owner:
        return
    try:
        engagements, jobs = _registries()
        kind, _ = _kind_of(owner)
        if kind == "job":
            engagements.record_job_post(owner, addr)
        elif kind == "delegation":
            jobs.record_task_post(owner, addr)
    except Exception:  # noqa: BLE001 — filing never fails a post
        logger.warning("task post filing failed", exc_info=True)


def _ref(kind: str, rec: Any) -> TaskRef:
    if kind == "job":
        executor = rec.executor or rec.role_or_type
    else:
        executor = rec.executor or rec.specialist_role
    return TaskRef(kind=kind, task_id=rec.id, executor=executor,
                   requester=rec.requester)


def owner(addr: "TgAddress") -> "TaskRef | None":
    """The live task that owns the message *addr*, else ``None``."""
    engagements, jobs = _registries()
    rec = engagements.job_post_owner(addr) if engagements is not None else None
    if rec is not None:
        return _ref("job", rec)
    row = jobs.task_post_owner(addr) if jobs is not None else None
    return _ref("delegation", row) if row is not None else None


def ended_owner(addr: "TgAddress") -> "TaskRef | None":
    """The ended task that owned the message *addr*, else ``None``."""
    engagements, jobs = _registries()
    rec = engagements.ended_job_post_owner(addr) if engagements is not None else None
    if rec is not None:
        return _ref("job", rec)
    row = jobs.ended_task_post_owner(addr) if jobs is not None else None
    return _ref("delegation", row) if row is not None else None


def executor_of(task_id: str) -> str | None:
    """The agent that runs the task *task_id*, live or ended, else ``None``."""
    kind, rec = _kind_of(task_id)
    return _ref(kind, rec).executor if kind is not None else None


def reset() -> None:
    """Forget the opened deliveries (tests)."""
    _opened.clear()
