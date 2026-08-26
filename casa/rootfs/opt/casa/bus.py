"""Lightweight async message bus for inter-agent communication."""

from __future__ import annotations

import asyncio
import collections
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine

from log_cid import cid_var
from personality_types import TrustedUserOriginInput

logger = logging.getLogger(__name__)


class MessageType(Enum):
    REQUEST = "request"
    RESPONSE = "response"
    NOTIFICATION = "notification"
    CHANNEL_IN = "channel_in"
    CHANNEL_OUT = "channel_out"
    SCHEDULED = "scheduled"


@dataclass
class BusMessage:
    type: MessageType
    source: str
    target: str
    content: Any
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    reply_to: str = ""
    channel: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    priority: int = 1
    # Personality Task 9: server-created per-turn ingress identity, set by an
    # authenticated channel AFTER external-context sanitization (never decoded
    # from payload/free-text). ``None`` for author-less turns (scheduled,
    # webhook trigger, synthetic, internal) — those record a ``system``
    # user identity, not a fabricated user.
    trusted_user_origin: TrustedUserOriginInput | None = None
    # #343(a): set by request() when the caller is cancelled while this
    # message is still QUEUED (no dispatch task exists yet to cancel).
    # The consumer drops a flagged message on dequeue instead of running
    # the handler — a cancelled voice utterance must not run a full SDK
    # turn later. In-memory only; BusMessage is never persisted.
    cancelled: bool = False
    # #701: a process-local delivery acknowledgement. Set ONLY by a producer
    # that holds a durable obligation to announce something (restart recovery
    # and the delegation terminal callback), and invoked by the consuming
    # agent once its channel reports that the turn reached the transport — not
    # when this message was accepted onto a queue, which is all `send_checked`
    # can report. Never serialised, never decoded from payload or context, and
    # `None` for every other producer on the bus.
    on_delivery: Callable[[], Coroutine[Any, Any, None]] | None = field(
        default=None, repr=False, compare=False)


class BusShutdownError(RuntimeError):
    """Raised by request() once begin_shutdown() has been called — new
    request/response work is refused during teardown so an ingress handler
    gets a fast typed failure instead of enqueueing for a consumer that is
    about to be (or already was) cancelled (#316)."""


# Type alias for handler callbacks
Handler = Callable[[BusMessage], Coroutine[Any, Any, BusMessage | None]]


class MessageBus:
    """Simple priority-queue-based message bus."""

    def __init__(self, max_log_size: int = 10000) -> None:
        self.queues: dict[str, asyncio.PriorityQueue] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.handlers: dict[str, Handler | None] = {}
        # msg_id -> dispatch task. Populated by run_agent_loop when a
        # REQUEST is picked up; consulted by request() so a cancelled
        # caller tears down the in-flight handler task (voice `cancel`
        # frame semantics — spec §10.3).
        self._dispatch_tasks: dict[str, asyncio.Task] = {}
        # name -> run_agent_loop consumer task. H10/H11 (v0.49.0): the
        # bus owns the consumer lifecycle so reload can spawn loops for
        # roles added after boot and cancel them on eviction. Populated
        # by start_agent_loop; drained by unregister.
        self._loop_tasks: dict[str, asyncio.Task] = {}
        # #343(b): name -> live dispatch tasks spawned by that agent's
        # consumer. Serves two purposes: unregister can cancel in-flight
        # handler work (not just the consumer loop), and every dispatch
        # task — non-REQUESTs included — holds a strong reference while
        # running (asyncio would otherwise be free to GC an unreferenced
        # task mid-flight).
        self._dispatch_by_agent: dict[str, set[asyncio.Task]] = {}
        # #316: once set, request() refuses with BusShutdownError.
        self._shutting_down = False
        self._log: collections.deque[BusMessage] = collections.deque(
            maxlen=max_log_size
        )
        self._seq: int = 0

    def register(self, name: str, handler: Handler | None = None) -> None:
        """Register an agent (queue + optional handler).

        Idempotent on the queue: re-registering an existing name only
        rebinds the handler, preserving the queue so a running
        ``run_agent_loop`` task continues to consume from it. This is
        the contract granular reload (v0.35.0+) relies on — replacing
        the queue would orphan the dispatch loop and hang every turn.
        """
        if name not in self.queues:
            self.queues[name] = asyncio.PriorityQueue()
        self.handlers[name] = handler

    def start_agent_loop(self, name: str) -> asyncio.Task:
        """Spawn (or return the already-running) consumer task for *name*.

        Idempotent: at most one live ``run_agent_loop`` task per name,
        so callers may invoke it after EVERY ``register`` — an existing
        role keeps its running consumer, while a role added after boot
        (H10, v0.49.0: reload-created residents/specialists) gets one
        spawned here instead of enqueueing forever. ``register`` must
        run first so the queue exists when the loop starts.
        """
        existing = self._loop_tasks.get(name)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(self.run_agent_loop(name))
        self._loop_tasks[name] = task
        return task

    def unregister(self, name: str) -> asyncio.Task | None:
        """Reverse of ``register`` + ``start_agent_loop``: cancel the
        consumer task and drop the queue + handler.

        H11 (v0.49.0): reload eviction previously called a method that
        did not exist; the swallowed AttributeError left 'deleted'
        residents consuming their queue forever (ghost agents). After
        this call, ``send`` to *name* silently drops (the unknown-target
        contract in ``send``) and the consumer ends. run_agent_loop
        caches its queue reference at entry, so popping the queue here
        cannot crash the loop in the window before the cancel lands.

        Returns the cancelled task (if one was tracked) so async
        callers can await its completion; ``None`` otherwise.
        """
        task = self._loop_tasks.pop(name, None)
        if task is not None and not task.done():
            task.cancel()
        # #343(b): dispatch tasks are independent of the consumer — a
        # deleted role's handler must not keep running (and acting as that
        # role) after eviction. Cancel them here; unregister_and_wait is
        # the awaiting variant reload teardown uses.
        for dispatch in self._dispatch_by_agent.pop(name, set()):
            if not dispatch.done():
                dispatch.cancel()
        # Sol r5-2: requests still QUEUED (never dispatched) also have
        # waiting callers — settle their futures with the handler-error
        # convention so eviction cannot strand a caller until the full
        # request timeout.
        queue = self.queues.pop(name, None)
        if queue is not None:
            while True:
                try:
                    _priority, _seq, qmsg = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                fut = self.pending.pop(qmsg.id, None)
                if fut is not None and not fut.done():
                    fut.set_result(BusMessage(
                        type=MessageType.RESPONSE,
                        source=qmsg.target,
                        target=qmsg.source,
                        content=f"handler error: cancelled: {qmsg.id}",
                        reply_to=qmsg.id,
                    ))
        self.handlers.pop(name, None)
        return task

    async def unregister_and_wait(self, name: str) -> None:
        """``unregister`` + await completion of the consumer AND every
        in-flight dispatch task, so callers (reload teardown) know no
        handler work for *name* survives the evict."""
        dispatches = list(self._dispatch_by_agent.get(name, set()))
        task = self.unregister(name)
        pending = [t for t in [task, *dispatches] if t is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def dispatch_tasks_for(self, name: str) -> list[asyncio.Task]:
        """Snapshot of the live dispatch tasks spawned for *name*."""
        return list(self._dispatch_by_agent.get(name, set()))

    def begin_shutdown(self) -> None:
        """#316: refuse new request/response work. Notifications and sends
        keep flowing (outbound operator messages during the drain)."""
        self._shutting_down = True

    def fail_pending(self) -> None:
        """#316: resolve every still-pending request future with
        BusShutdownError so stranded ingress handlers return promptly
        instead of waiting out the bus timeout (which would stall
        ``runner.cleanup()`` to aiohttp's shutdown bound)."""
        for msg_id, fut in list(self.pending.items()):
            if not fut.done():
                fut.set_exception(BusShutdownError(
                    f"bus shutting down; request {msg_id} abandoned"
                ))
        self.pending.clear()

    def agent_loop_tasks(self) -> list[asyncio.Task]:
        """Snapshot of every tracked consumer task (boot-time and
        reload-added). Shutdown cancels these; cancel is idempotent for
        any that were already torn down by ``unregister``."""
        return list(self._loop_tasks.values())

    async def send(self, msg: BusMessage) -> None:
        """Send a message to msg.target's queue (unknown targets are silently
        dropped). Thin wrapper over :meth:`send_checked` that discards the
        delivery signal."""
        await self.send_checked(msg)

    async def send_checked(self, msg: BusMessage) -> str:
        """Enqueue like :meth:`send` but REPORT delivery.

        Returns ``"accepted"`` when ``msg.target`` has a registered queue (the
        message was enqueued) or ``"no_target"`` when it was silently dropped
        (unknown target — the same drop ``send`` performs, just observable).
        The button-continuation dispatcher (telegram
        ``_dispatch_button_continuation``) uses this signal to retry a target
        that may still be (re)registering vs. give up.
        """
        if msg.target not in self.queues:
            return "no_target"  # silently drop for unknown targets
        self._log.append(msg)
        self._seq += 1
        await self.queues[msg.target].put((msg.priority, self._seq, msg))
        return "accepted"

    async def request(
        self, msg: BusMessage, timeout: float = 300
    ) -> BusMessage:
        """Send a REQUEST and await the response (or timeout).

        If the caller is cancelled mid-flight, the in-flight dispatch
        task (if registered by ``run_agent_loop``) is also cancelled so
        the handler stops doing work. This is the cancel contract the
        voice channel relies on (spec §10.3).
        """
        if self._shutting_down:
            raise BusShutdownError(
                "bus shutting down; new requests refused"
            )
        msg.type = MessageType.REQUEST
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[BusMessage] = loop.create_future()
        self.pending[msg.id] = fut
        await self.send(msg)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self.pending.pop(msg.id, None)
            raise
        except asyncio.CancelledError:
            self.pending.pop(msg.id, None)
            dispatch = self._dispatch_tasks.get(msg.id)
            if dispatch is not None and not dispatch.done():
                dispatch.cancel()
            # #343(a): if no dispatch task exists the message is still
            # queued — flag it so the consumer drops it on dequeue
            # instead of running the handler for a caller that is gone.
            elif dispatch is None:
                msg.cancelled = True
            raise

    async def respond(self, original_id: str, response: BusMessage) -> None:
        """Complete a pending request future."""
        fut = self.pending.pop(original_id, None)
        if fut and not fut.done():
            fut.set_result(response)

    async def notify(self, msg: BusMessage) -> None:
        """Send a NOTIFICATION (fire-and-forget)."""
        msg.type = MessageType.NOTIFICATION
        await self.send(msg)

    async def run_agent_loop(self, agent_name: str) -> None:
        """Pull messages from the agent's queue and dispatch to its handler.

        Each message is dispatched as an independent asyncio task so that
        multiple concurrent messages are processed in parallel (no implicit
        serialisation at the bus level).  Runs until the current task is
        cancelled.
        """
        queue = self.queues[agent_name]

        async def _dispatch(msg: BusMessage) -> None:
            # Resolve the handler per-message so tests (and future hot
            # reloads) can rebind ``bus.handlers[name]`` and have the
            # next dispatch pick up the new handler.
            handler = self.handlers.get(agent_name)
            # Set cid for every log record emitted during the turn
            # (spec 5.2 §7.2). Reset via token in finally so a reused
            # dispatch task (future refactors) cannot leak.
            cid = ""
            if msg.context:
                cid = msg.context.get("cid", "") or ""
            cid_token = cid_var.set(cid or "-")
            try:
                if handler is not None:
                    result = await handler(msg)
                    # Auto-respond to REQUESTs if handler returned something
                    if msg.type == MessageType.REQUEST and result is not None:
                        result.reply_to = msg.id
                        result.type = MessageType.RESPONSE
                        await self.respond(msg.id, result)
                    elif (
                        msg.type == MessageType.REQUEST
                        and msg.id in self.pending
                    ):
                        # M6 (v0.53.0): the handler completed but produced no
                        # response (e.g. Agent.handle_message returning None on
                        # a silent/empty turn — G-3 sentinel suppression or a
                        # no-text SDK turn). Resolve the pending future with an
                        # empty RESPONSE so bus.request() callers (voice SSE/WS,
                        # /invoke) return immediately instead of hanging until
                        # the ~300s timeout. Belt-and-suspenders with the
                        # agent-level fix (agent.handle_message now returns a
                        # typed empty RESPONSE for REQUEST turns): defends any
                        # handler that returns None on a REQUEST. The
                        # ``msg.id in self.pending`` guard is a no-op if
                        # something already responded, or if request() already
                        # timed out / was cancelled and popped the future.
                        # NOTIFICATION / fire-and-forget targets never populate
                        # ``pending``, so their None returns stay a no-op.
                        await self.respond(msg.id, BusMessage(
                            type=MessageType.RESPONSE,
                            source=msg.target,
                            target=msg.source,
                            content="",
                            reply_to=msg.id,
                            channel=msg.channel,
                            context=msg.context,
                        ))
            except asyncio.CancelledError:
                # Two distinct cancellation sources land here. Caller of
                # bus.request cancelled us: request() already popped the
                # pending future, this pop is a no-op, the caller is gone.
                # Sol r1-1: unregister/evict cancelled us (#343b) while the
                # CALLER is still waiting — resolve its future with the
                # handler-error convention so it returns promptly instead
                # of sitting out the full request timeout.
                fut = self.pending.pop(msg.id, None)
                if fut is not None and not fut.done():
                    fut.set_result(BusMessage(
                        type=MessageType.RESPONSE,
                        source=msg.target,
                        target=msg.source,
                        content=f"handler error: cancelled: {msg.id}",
                        reply_to=msg.id,
                    ))
                raise
            except Exception:
                logger.exception(
                    "Bus handler for target=%r raised on msg id=%s",
                    msg.target,
                    msg.id,
                )
                # Unblock REQUEST callers so they don't wait for the full timeout.
                if msg.type == MessageType.REQUEST:
                    error_resp = BusMessage(
                        type=MessageType.RESPONSE,
                        source=msg.target,
                        target=msg.source,
                        content=f"handler error: {msg.id}",
                        reply_to=msg.id,
                    )
                    await self.respond(msg.id, error_resp)
            finally:
                cid_var.reset(cid_token)
                if msg.type == MessageType.REQUEST:
                    self._dispatch_tasks.pop(msg.id, None)
                queue.task_done()

        while True:
            _priority, _seq, msg = await queue.get()
            # #343(a): the caller cancelled while this message was queued —
            # nobody is waiting and the work must not run.
            if msg.cancelled:
                queue.task_done()
                continue
            task = asyncio.create_task(_dispatch(msg))
            if msg.type == MessageType.REQUEST:
                self._dispatch_tasks[msg.id] = task
            # #343(b): strong per-agent reference for cancel-on-evict (and
            # GC safety of non-REQUEST dispatches).
            tracked = self._dispatch_by_agent.setdefault(agent_name, set())
            tracked.add(task)
            task.add_done_callback(tracked.discard)

    def get_log(self, last_n: int = 50) -> list[BusMessage]:
        """Return the last *last_n* log entries."""
        items = list(self._log)
        return items[-last_n:]
