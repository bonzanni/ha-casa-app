"""The posts of every task, filed under the task that made them.

Every message a task posts — its start line, each page of its answer, every
intermediate plugin post (a proposal, a link, a file, a run card) — is filed
under that task, so a reply to any of them finds the task: a job's posts in
the engagement registry, a delegation's in the job registry. The index entry
exists before the task's first post (:func:`open`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from channels.tg_address import TgAddress
    from task_model import Requester


@dataclass(frozen=True)
class TaskRef:
    """The task that owns a message: its kind and id, its executor, and who
    receives its result."""
    kind: Literal["job", "delegation"]
    task_id: str
    executor: str
    requester: "Requester"


def open(task_id: str, delivery: "TgAddress") -> None:  # noqa: A001 — the index's verb
    """Open the index entry of the task *task_id*, delivered at *delivery*,
    before its first post."""
    raise NotImplementedError


def record(owner: str, addr: "TgAddress") -> None:
    """File the landed message *addr* under the task *owner*."""
    raise NotImplementedError


def owner(addr: "TgAddress") -> "TaskRef | None":
    """The live task that owns the message *addr*, else ``None``."""
    raise NotImplementedError


def ended_owner(addr: "TgAddress") -> "TaskRef | None":
    """The ended task that owned the message *addr*, else ``None``."""
    raise NotImplementedError
