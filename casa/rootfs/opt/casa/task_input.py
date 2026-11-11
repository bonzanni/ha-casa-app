"""The operator's replies to a running task, written into its live session.

A task's input destination exists before its first post. Replies that arrive
while it is ``pending`` (its session's client not attached yet) are queued in
order, in memory; :func:`attach` flushes them into the session and later
replies are written directly (``live``). Once the task has ended, a reply is
refused with ``"ended"`` and goes to the requester's conversation instead.
Pending is never treated as ended.
"""
from __future__ import annotations

from typing import Any, Literal


def open_pending(task_id: str) -> None:
    """Create the input destination of *task_id*, pending."""
    raise NotImplementedError


def attach(task_id: str, client: Any) -> None:
    """*task_id*'s session client exists: flush the queued replies into it."""
    raise NotImplementedError


def submit(task_id: str, text: str) -> Literal["queued", "written", "ended"]:
    """Deliver the operator's reply *text* to the task *task_id*."""
    raise NotImplementedError


def close(task_id: str) -> None:
    """End *task_id*'s input destination; later submits return ``"ended"``."""
    raise NotImplementedError
