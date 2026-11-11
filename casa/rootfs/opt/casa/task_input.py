"""The operator's replies to a running task, written into its live session.

A task's input destination exists before its first post. Replies that arrive
while it is ``pending`` (its session's client not attached yet) are queued in
order, in memory; :func:`attach` flushes them into the session and later
replies are written directly (``live``). Once the task has ended, a reply is
refused with ``"ended"`` and goes to the requester's conversation instead.
Pending is never treated as ended.

The write itself (:func:`write`) is the one a background job's delivery
worker uses: one stream-json user message, stamped with a uuid, handed to the
live client, which the CLI takes up at its next tool boundary. A task's
destination is memory-only: a restart ends it, like the run it belongs to.

What the operator wrote into a task is also kept, apart from its destination,
so the requester can be told once the task is done: :func:`submit` records
its ``said`` words, :func:`close` leaves them in place, and :func:`take_said`
hands them over once. The record is bounded and memory-only too.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import uuid as uuid_mod
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

PENDING = "pending"
LIVE = "live"


def user_payload(text: str, *, uuid: str) -> dict:
    """The stream-json line for one write. The SDK passes a dict through to
    the CLI unchanged; the CLI echoes ``uuid`` back at take-up."""
    return {"type": "user", "message": {"role": "user", "content": text},
            "parent_tool_use_id": None, "session_id": "default",
            "uuid": uuid}


async def _one_message(payload: dict) -> Any:
    yield payload


async def write(client: Any, text: str, *, uuid: str | None = None) -> None:
    """Write *text* into the live session of *client* as one user message.
    A failure is the caller's: nothing here retries."""
    payload = user_payload(text, uuid=uuid or str(uuid_mod.uuid4()))
    await client.query(_one_message(payload))


@dataclass(eq=False)
class _Destination:
    state: str = PENDING
    queue: collections.deque = field(default_factory=collections.deque)
    client: Any = None
    wake: asyncio.Event | None = None
    worker: asyncio.Task | None = None


_destinations: dict[str, _Destination] = {}

SAID_CHARS = 300          # one entry, whitespace collapsed, clipped with "…"
SAID_PER_TASK = 5         # the last entries kept per task, oldest first
SAID_TASKS = 64           # tasks kept; the oldest is forgotten first

_said: collections.OrderedDict[str, collections.deque] = collections.OrderedDict()


def _record_said(task_id: str, said: Any) -> None:
    if not isinstance(said, str):
        return
    words = " ".join(said.split())
    if not words:
        return
    if len(words) > SAID_CHARS:
        words = words[:SAID_CHARS - 1].rstrip() + "…"
    entries = _said.get(task_id)
    if entries is None:
        entries = _said[task_id] = collections.deque(maxlen=SAID_PER_TASK)
        while len(_said) > SAID_TASKS:
            _said.popitem(last=False)
    entries.append(words)


def open_pending(task_id: str) -> None:
    """Create the input destination of *task_id*, pending. Opening one that
    exists keeps it as it is."""
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("a task input needs a task id")
    _destinations.setdefault(task_id, _Destination())


def attach(task_id: str, client: Any) -> None:
    """*task_id*'s session client exists: flush the queued replies into it,
    in order, then write each later reply directly.

    Called once the session's own first message has been written, so a reply
    queued before that never goes ahead of the task's brief. A destination
    that was never opened, or has ended, attaches nothing."""
    dest = _destinations.get(task_id)
    if dest is None:
        logger.info("task %s: no open input to attach to", task_id[:8])
        return
    if dest.state == LIVE:
        logger.warning("task %s: input already attached", task_id[:8])
        return
    dest.client = client
    dest.state = LIVE
    dest.wake = asyncio.Event()
    dest.worker = asyncio.get_running_loop().create_task(
        _work(task_id, dest), name=f"task-input-{task_id[:8]}")
    if dest.queue:
        dest.wake.set()


def submit(task_id: str, text: str, *, said: str | None = None,
           ) -> Literal["queued", "written", "ended"]:
    """Deliver the operator's reply *text* to the task *task_id*:
    ``"queued"`` while the task is pending (written once it attaches),
    ``"written"`` when it is handed to the live session's writer, ``"ended"``
    when the task has no input any more (it ended, or never had one).

    *said* is the operator's own words in that reply; unless the result is
    ``"ended"`` they are kept for :func:`take_said`."""
    dest = _destinations.get(task_id)
    if dest is None:
        return "ended"
    _record_said(task_id, said)
    dest.queue.append(str(text))
    if dest.state == PENDING:
        return "queued"
    dest.wake.set()
    return "written"


def close(task_id: str) -> None:
    """End *task_id*'s input destination; later submits return ``"ended"``.
    A reply still unwritten at this point is dropped, and logged."""
    dest = _destinations.pop(task_id, None)
    if dest is None:
        return
    if dest.queue:
        logger.warning("task %s ended with %d unwritten repl%s", task_id[:8],
                       len(dest.queue), "y" if len(dest.queue) == 1 else "ies")
        dest.queue.clear()
    if dest.wake is not None:
        # the worker sees its destination gone and returns; a write already
        # under way is let finish rather than cut mid-message
        dest.wake.set()


def state(task_id: str) -> Literal["pending", "live", "ended"]:
    """Where *task_id*'s input stands."""
    dest = _destinations.get(task_id)
    return "ended" if dest is None else dest.state


def take_said(task_id: str) -> list[str]:
    """The operator's words recorded for *task_id*, oldest first, and forget
    them: a second take returns ``[]``. Never raises."""
    try:
        entries = _said.pop(task_id, None)
    except Exception:  # noqa: BLE001 — an unhashable id has nothing recorded
        return []
    return list(entries) if entries else []


async def _work(task_id: str, dest: _Destination) -> None:
    """The destination's only writer: one reply at a time, in order."""
    while _destinations.get(task_id) is dest:
        if not dest.queue:
            dest.wake.clear()
            await dest.wake.wait()
            continue
        text = dest.queue.popleft()
        try:
            await write(dest.client, text)
        except Exception:  # noqa: BLE001 — one failed write never stops the next
            logger.warning("task %s: a reply could not be written", task_id[:8],
                           exc_info=True)


def reset() -> None:
    """Forget every destination and every recorded word (tests)."""
    for dest in _destinations.values():
        if dest.worker is not None and not dest.worker.done():
            dest.worker.cancel()
    _destinations.clear()
    _said.clear()
