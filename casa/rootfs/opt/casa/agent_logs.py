"""Agent logs: each agent's work, posted to its own topic in an optional forum group.

When the operator configures a forum supergroup and makes the bot an
administrator with the "Manage topics" right, every agent that works in the
background gets one read-only topic there. Each topic holds rolling plain-text
messages, one line per entry, every line prefixed with the name of the run it
belongs to::

    Q3 accounting · ▶ started
    Q3 accounting · 🔧 Searching memory

The module has three parts, all owned by one process-wide :class:`AgentLogs`:

* **The sink.** :meth:`AgentLogs.log` is synchronous: it appends one entry to
  an in-memory queue and returns. It never awaits and never raises, so a
  producer on the conversation path can call it without any risk to the
  operator's direct chat. When logging is off it returns at once.
* **The writer.** One asyncio task drains the queue. Every request it makes to
  the group (send, edit, delete, bulk delete, topic creation) spends a token
  from one budget of :data:`OPS_PER_WINDOW` operations per
  :data:`WINDOW_SECONDS`, which keeps the bot inside Telegram's per-group
  limit. A flood wait pauses the whole writer. Keys with queued lines are
  served round-robin, one operation per turn, so one busy agent cannot starve
  the others. Nothing outside this module waits on the budget.
* **The registry and retention.** ``/data/log-topics.json`` remembers each
  key's topic and every post's id and send time. A bot may delete a message
  only while it is younger than 48 hours, so :meth:`AgentLogs.retention_pass`
  deletes posts once they are :data:`DELETE_AFTER_SECONDS` old and forgets
  those it can no longer delete.

Logs are best effort: an overflowing queue, a restart, a Telegram error or a
long flood wait loses lines, and nothing here ever reports to the direct chat.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import functools
import json
import logging
import os
import time
import warnings
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from telegram.error import RetryAfter

from atomic_io import PRIVATE, atomic_write_json

logger = logging.getLogger(__name__)

LOG_TOPICS_PATH = "/data/log-topics.json"

#: Entries waiting to be posted, across every key.
QUEUE_LIMIT = 2000
#: Telegram allows about 20 messages a minute in a group; whether edits and
#: deletions count is undocumented, so every request is counted, with margin.
OPS_PER_WINDOW = 18
WINDOW_SECONDS = 60.0
#: Telegram's text limit for one message.
MESSAGE_LIMIT = 4096
#: A rolling message is continued by editing until it is idle this long ...
ROLL_IDLE_SECONDS = 5 * 60.0
#: ... or this old, counted from its first send.
ROLL_AGE_SECONDS = 60 * 60.0
RUN_NAME_LIMIT = 40
TOPIC_NAME_LIMIT = 128
#: Posts older than this are deleted by the retention pass.
DELETE_AFTER_SECONDS = 44 * 3600.0
#: Posts older than this cannot be deleted in time any more and are forgotten.
FORGET_AFTER_SECONDS = 47.5 * 3600.0
#: Telegram's bulk delete takes 1-100 ids.
DELETE_BATCH_LIMIT = 100
#: The registry is written at most once per interval, plus once at stop.
SAVE_INTERVAL_SECONDS = 10.0
#: Internal failures in the sink are reported at most this often.
_INTERNAL_WARNING_INTERVAL = 60.0

_SEPARATOR = " · "
_ELLIPSIS = "…"

# python-telegram-bot strips the "Bad Request: " prefix and capitalises the
# server's text, so errors are told apart by lower-case substrings.
_NOT_MODIFIED = ("not modified",)
_EDIT_GONE = ("message to edit not found", "message can't be edited")
_THREAD_GONE = ("thread not found", "topic_closed", "topic_deleted")
_UNREACHABLE = ("not enough rights", "chat not found", "bot was kicked",
                "not a member")
_DELETE_GONE = ("message to delete not found", "message can't be deleted")

LogKind = Literal["agent", "plugins"]


@dataclass(frozen=True)
class LogKey:
    """Whose log an entry belongs to.

    ``kind`` is ``"agent"`` for a specialist or the configurator, keyed by
    its role, or ``"plugins"`` for a resident agent that hosts plugin jobs,
    keyed by the resident's role.
    """

    kind: LogKind
    role: str

    @property
    def state_key(self) -> str:
        """The key's name in the registry file."""
        return f"{self.kind}:{self.role}"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + _ELLIPSIS


def topic_name(key: LogKey, display_name: Callable[[str], str]) -> str:
    """The topic title for *key*: the agent's display name, or
    ``"<resident> · plugins"``, cut to Telegram's 128-character limit."""
    try:
        name = (display_name(key.role) or "").strip() or key.role
    except Exception:  # noqa: BLE001 - a broken name source must not stop the log
        name = key.role
    if key.kind == "plugins":
        name = f"{name}{_SEPARATOR}plugins"
    return _cut(name, TOPIC_NAME_LIMIT)


def run_name(run: str) -> str:
    """A run's label in its log lines: the first line of its title or task,
    cut to 40 characters with an ellipsis."""
    first = (run or "").strip().split("\n", 1)[0].strip()
    return _cut(first, RUN_NAME_LIMIT)


def _tg_len(text: str) -> int:
    """Length as Telegram counts it (UTF-16 code units), which is never less
    than the number of code points."""
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def _split_at(text: str, limit: int) -> tuple[str, str]:
    """The longest prefix of *text* within *limit* Telegram units, and the rest."""
    units = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            return text[:i], text[i:]
    return text, ""


def _error_text(exc: BaseException) -> str:
    return str(getattr(exc, "message", None) or exc).lower()


def _matches(exc: BaseException, needles: tuple[str, ...]) -> bool:
    text = _error_text(exc)
    return any(n in text for n in needles)


def retry_after_seconds(exc: RetryAfter) -> float:
    """The delay a flood wait asks for, in seconds.

    python-telegram-bot 22 hands back either a number or a ``timedelta``
    depending on an environment switch, and warns on every read of the
    attribute; this reads it quietly and portably.
    """
    with warnings.catch_warnings():
        # The library's own warning class derives from DeprecationWarning.
        warnings.simplefilter("ignore", DeprecationWarning)
        value = getattr(exc, "retry_after", None)
    if isinstance(value, dt.timedelta):
        return max(0.0, value.total_seconds())
    try:
        return max(0.0, float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1.0


class _Budget:
    """At most ``capacity`` operations in any ``window`` seconds, plus a
    shared pause for flood waits."""

    def __init__(self, capacity: int, window: float,
                 monotonic: Callable[[], float],
                 sleep: Callable[[float], Awaitable[Any]]) -> None:
        self._capacity = capacity
        self._window = window
        self._monotonic = monotonic
        self._sleep = sleep
        self._stamps: deque[float] = deque()
        self._paused_until = float("-inf")

    def _wait_time(self) -> float:
        now = self._monotonic()
        while self._stamps and self._stamps[0] <= now - self._window:
            self._stamps.popleft()
        wait = max(0.0, self._paused_until - now)
        if len(self._stamps) >= self._capacity:
            wait = max(wait, self._stamps[0] + self._window - now)
        return wait

    def paused(self) -> bool:
        return self._paused_until > self._monotonic()

    async def acquire(self, *, unless_paused: bool = False) -> bool:
        """Take a token, waiting for one. With *unless_paused*, give up and
        return ``False`` instead of waiting out a flood wait."""
        # The check and the record happen with no await between them, so two
        # callers on the same event loop can never both take the last token.
        while True:
            if unless_paused and self.paused():
                return False
            wait = self._wait_time()
            if wait <= 0:
                self._stamps.append(self._monotonic())
                return True
            await self._sleep(wait)

    def pause(self, seconds: float) -> None:
        self._paused_until = max(self._paused_until, self._monotonic() + seconds)


@dataclass
class _Entry:
    seq: int
    text: str


@dataclass
class _KeyState:
    pending: deque[_Entry] = field(default_factory=deque)
    skipped: int = 0
    message_id: int | None = None
    message_text: str = ""
    message_started: float = 0.0
    message_touched: float = 0.0
    topic_retried: bool = False

    def has_work(self) -> bool:
        return bool(self.pending) or self.skipped > 0

    def forget_message(self) -> None:
        self.message_id = None
        self.message_text = ""


@dataclass
class _Plan:
    """One send or edit: its full text and what it consumes from the queue."""

    edit: bool
    text: str
    skip_taken: int
    seqs: set[int]
    split: tuple[int, str] | None


async def _to_thread_uncancellable(fn: Callable[..., Any], *args: Any) -> Any:
    """Run *fn* in a worker thread and, if cancelled, wait for the thread
    before re-raising, so a write in flight is never overtaken by the next."""
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - the cancellation wins
                break
        raise


class AgentLogs:
    """The process-wide log sink, its writer task, registry and retention.

    Everything that touches time or Telegram is injected: ``monotonic`` and
    ``wall`` clocks, an async ``sleep``, and the bot handed to
    :meth:`check_enabled` (anything with python-telegram-bot's async methods).
    ``display_name`` maps a role to the agent's display name for topic titles.
    """

    def __init__(
        self,
        *,
        display_name: Callable[[str], str],
        path: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._display_name = display_name
        self._path = path if path is not None else LOG_TOPICS_PATH
        self._monotonic = monotonic
        self._wall = wall
        self._sleep = sleep
        self._budget = _Budget(OPS_PER_WINDOW, WINDOW_SECONDS, monotonic, sleep)

        self._enabled = False
        self._unreachable = False
        self._warned_reason: str | None = None
        self._bot: Any = None
        self._chat_id: int | None = None
        self._ignored_chat: int | None = None

        self._keys: dict[LogKey, _KeyState] = {}
        self._order: deque[LogKey] = deque()
        self._scheduled: set[LogKey] = set()
        self._seq = 0
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_internal_warning = float("-inf")
        self._retention_lock = asyncio.Lock()

        self._registry_chat: int | None = None
        self._topics: dict[str, int] = {}
        self._posts: list[list[float]] = []
        self._dirty = False
        self._last_save = float("-inf")
        self._save_lock = asyncio.Lock()
        self._load()

    # ------------------------------------------------------------------
    # Producer API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def log(self, key: LogKey, run: str, text: str) -> None:
        """Queue one line for *key*'s topic. Never awaits, never raises."""
        if not self._enabled:
            return
        try:
            body = (text or "").strip()
            if not body:
                return
            name = run_name(run)
            line = f"{name}{_SEPARATOR}{body}" if name else body
            self._append(key, line)
            self._wake.set()
        except Exception:  # noqa: BLE001 - a producer must never see a log failure
            self._warn_internal("could not queue a log line")

    def _append(self, key: LogKey, line: str) -> None:
        state = self._keys.get(key)
        if state is None:
            state = self._keys[key] = _KeyState()
        if sum(len(s.pending) for s in self._keys.values()) >= QUEUE_LIMIT:
            # Make room from the same key's oldest lines; a key with nothing
            # queued takes the room from the key holding the most.
            victim = state if state.pending else max(
                self._keys.values(), key=lambda s: len(s.pending))
            victim.pending.popleft()
            victim.skipped += 1
        self._seq += 1
        state.pending.append(_Entry(self._seq, line))
        self._schedule(key)

    def _schedule(self, key: LogKey) -> None:
        if key not in self._scheduled:
            self._scheduled.add(key)
            self._order.append(key)

    def _warn_internal(self, what: str) -> None:
        try:
            now = self._monotonic()
            if now - self._last_internal_warning < _INTERNAL_WARNING_INTERVAL:
                return
            self._last_internal_warning = now
            logger.warning("agent log: %s", what, exc_info=True)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Enablement
    # ------------------------------------------------------------------

    async def check_enabled(self, bot: Any, chat_id: int | None) -> bool:
        """Decide whether logging is on, and bind *bot*.

        The first call with a group binds that group for the life of the
        process: requests already on their way belong to it, so a later call
        naming another group changes nothing and warns once (the option is read
        at start). Logging is on only when the bot is an administrator of the
        bound group with the right to manage topics. Otherwise one WARNING
        (unless no group is configured at all) and :meth:`log` does nothing
        until the next check.
        """
        self._bot = bot
        self._unreachable = False
        if self._chat_id is None and chat_id:
            if chat_id != self._registry_chat:
                if self._registry_chat is not None:
                    logger.info(
                        "agent log: the log group is not the one on record; "
                        "topics and posts of the previous group are forgotten")
                self._reset_registry(chat_id)
            self._chat_id = chat_id
        elif self._chat_id is not None and chat_id != self._chat_id:
            if chat_id != self._ignored_chat:
                self._ignored_chat = chat_id
                logger.warning(
                    "agent log: the log group changed; the new group applies "
                    "after a restart")
        if self._chat_id is None:
            self._disable(None)
            return False
        try:
            member = await bot.get_chat_member(chat_id=self._chat_id, user_id=bot.id)
        except Exception as exc:  # noqa: BLE001 - any failure means no logging
            if isinstance(exc, RetryAfter):
                self._pause(exc)
            self._unreachable = _matches(exc, _UNREACHABLE)
            self._disable(f"the log group could not be checked ({exc})")
            return False
        if getattr(member, "status", None) != "administrator":
            self._disable("the bot is not an administrator of the log group")
            return False
        if not getattr(member, "can_manage_topics", False):
            self._disable("the bot lacks the Manage topics right in the log group")
            return False
        self._enabled = True
        self._warned_reason = None
        return True

    def _disable(self, reason: str | None) -> None:
        self._enabled = False
        for state in self._keys.values():
            state.pending.clear()
            state.skipped = 0
            state.forget_message()
            state.topic_retried = False
        self._order.clear()
        self._scheduled.clear()
        if reason is not None and reason != self._warned_reason:
            self._warned_reason = reason
            logger.warning("agent log: logging is off: %s", reason)

    def _disable_unreachable(self, exc: BaseException) -> None:
        self._unreachable = True
        self._disable(f"the log group refused the bot ({exc})")

    # ------------------------------------------------------------------
    # The writer
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the writer task (idempotent). It idles while nothing is queued."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="agent-logs-writer")

    async def stop(self) -> None:
        """Cancel the writer and write the registry once. Queued lines are lost."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.warning("agent log: writer ended with an error", exc_info=True)
        if self._dirty:
            await self._save()

    def _has_work(self) -> bool:
        return bool(self._order)

    async def _run(self) -> None:
        while True:
            try:
                if self._has_work():
                    await self.step()
                    await self._save_if_due()
                elif self._dirty:
                    due_in = self._last_save + SAVE_INTERVAL_SECONDS - self._monotonic()
                    if due_in <= 0:
                        await self._save()
                    else:
                        await self._wait_for_work(due_in)
                else:
                    await self._wait_for_work(None)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the writer must outlive one bad turn
                self._warn_internal("writer turn failed")
                await self._sleep(1.0)

    async def _wait_for_work(self, timeout: float | None) -> None:
        self._wake.clear()
        if timeout is None:
            await self._wake.wait()
            return
        waiter = asyncio.ensure_future(self._wake.wait())
        sleeper = asyncio.ensure_future(self._sleep(timeout))
        try:
            await asyncio.wait({waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            sleeper.cancel()

    def _next_key(self) -> LogKey | None:
        while self._order:
            key = self._order.popleft()
            self._scheduled.discard(key)
            state = self._keys.get(key)
            if state is not None and state.has_work():
                return key
        return None

    async def step(self) -> bool:
        """Serve the next key in turn with one Telegram operation.

        Returns ``False`` when nothing is queued. The writer task calls this in
        a loop; it is public so the order of operations can be driven directly.
        """
        key = self._next_key()
        if key is None:
            return False
        try:
            await self._budget.acquire()
            state = self._keys[key]
            if self._enabled and self._bot is not None and state.has_work():
                await self._serve(key, state)
        finally:
            state = self._keys.get(key)
            if self._enabled and state is not None and state.has_work():
                self._schedule(key)
        return True

    async def _serve(self, key: LogKey, state: _KeyState) -> None:
        thread_id = self._topics.get(key.state_key)
        if thread_id is None:
            await self._create_topic(key, state)
            return
        plan = self._plan(state, self._monotonic())
        try:
            if plan.edit:
                await self._bot.edit_message_text(
                    text=plan.text, chat_id=self._chat_id,
                    message_id=state.message_id)
            else:
                message = await self._send(thread_id, plan.text)
        except Exception as exc:  # noqa: BLE001 - classified below
            self._on_post_error(key, state, plan, exc)
            return
        now = self._monotonic()
        if not plan.edit:
            state.message_id = message.message_id
            state.message_started = now
            self._posts.append([message.message_id, self._wall()])
            self._dirty = True
        state.message_text = plan.text
        state.message_touched = now
        state.topic_retried = False
        self._consume(state, plan)

    async def _send(self, thread_id: int | None, text: str) -> Any:
        # Without a thread id Telegram posts into the group's General topic;
        # a log line never goes there.
        if thread_id is None:
            raise ValueError("a log post needs a topic")
        return await self._bot.send_message(
            chat_id=self._chat_id, text=text, message_thread_id=thread_id,
            disable_notification=True)

    def _plan(self, state: _KeyState, now: float) -> _Plan:
        lines: list[tuple[int | None, str]] = []
        if state.skipped:
            noun = "line" if state.skipped == 1 else "lines"
            lines.append((None, f"{_ELLIPSIS} {state.skipped} {noun} skipped"))
        lines.extend((e.seq, e.text) for e in state.pending)

        can_edit = (
            state.message_id is not None
            and now - state.message_touched < ROLL_IDLE_SECONDS
            and now - state.message_started < ROLL_AGE_SECONDS
        )
        if can_edit:
            plan = self._fill(state.message_text, lines, edit=True,
                              skipped=state.skipped)
            if plan is not None:
                return plan
        plan = self._fill("", lines, edit=False, skipped=state.skipped)
        if plan is not None:
            return plan
        # The first line alone is longer than a message: send it in pieces.
        seq, text = lines[0]
        head, rest = _split_at(text, MESSAGE_LIMIT)
        assert seq is not None  # the skip line is always short
        if rest.strip():
            return _Plan(False, head, 0, set(), (seq, rest))
        return _Plan(False, head, 0, {seq}, None)

    @staticmethod
    def _fill(base: str, lines: list[tuple[int | None, str]], *, edit: bool,
              skipped: int) -> _Plan | None:
        text = base
        units = _tg_len(base)
        seqs: set[int] = set()
        skip_taken = 0
        for seq, line in lines:
            added = _tg_len(line) + (1 if text else 0)
            if units + added > MESSAGE_LIMIT:
                break
            text = f"{text}\n{line}" if text else line
            units += added
            if seq is None:
                skip_taken = skipped
            else:
                seqs.add(seq)
        if not seqs and not skip_taken:
            return None
        return _Plan(edit, text, skip_taken, seqs, None)

    @staticmethod
    def _consume(state: _KeyState, plan: _Plan) -> None:
        state.skipped = max(0, state.skipped - plan.skip_taken)
        if plan.seqs:
            state.pending = deque(e for e in state.pending if e.seq not in plan.seqs)
        if plan.split is not None:
            seq, rest = plan.split
            for entry in state.pending:
                if entry.seq == seq:
                    entry.text = rest
                    break

    def _on_post_error(self, key: LogKey, state: _KeyState, plan: _Plan,
                       exc: BaseException) -> None:
        if isinstance(exc, RetryAfter):
            self._pause(exc)
            return
        if plan.edit and _matches(exc, _NOT_MODIFIED):
            state.message_text = plan.text
            state.message_touched = self._monotonic()
            self._consume(state, plan)
            return
        if plan.edit and _matches(exc, _EDIT_GONE):
            state.forget_message()
            return
        if _matches(exc, _THREAD_GONE):
            self._forget_topic(key, state)
            if not state.topic_retried:
                # A new topic is created on this key's next turn and the same
                # lines are posted there.
                state.topic_retried = True
                return
            state.topic_retried = False
            self._consume(state, plan)
            logger.warning(
                "agent log: %s: the topic was lost again after it was recreated; "
                "lines dropped (%s)", key.state_key, exc)
            return
        if _matches(exc, _UNREACHABLE):
            self._disable_unreachable(exc)
            return
        self._consume(state, plan)
        logger.warning("agent log: %s: post failed, lines dropped: %s",
                       key.state_key, exc)

    async def _create_topic(self, key: LogKey, state: _KeyState) -> None:
        name = topic_name(key, self._display_name)
        try:
            topic = await self._bot.create_forum_topic(chat_id=self._chat_id, name=name)
            thread_id = int(topic.message_thread_id)
        except Exception as exc:  # noqa: BLE001 - classified below
            if isinstance(exc, RetryAfter):
                self._pause(exc)
            elif _matches(exc, _UNREACHABLE):
                self._disable_unreachable(exc)
            else:
                state.pending.clear()
                state.skipped = 0
                state.topic_retried = False
                logger.warning("agent log: %s: could not create its topic, "
                               "lines dropped: %s", key.state_key, exc)
            return
        self._topics[key.state_key] = thread_id
        state.forget_message()
        self._dirty = True

    def _forget_topic(self, key: LogKey, state: _KeyState) -> None:
        if self._topics.pop(key.state_key, None) is not None:
            self._dirty = True
        state.forget_message()

    def _pause(self, exc: RetryAfter) -> None:
        seconds = retry_after_seconds(exc)
        self._budget.pause(seconds)
        logger.warning("agent log: Telegram asked to wait %.0f s; the log pauses",
                       seconds)

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    async def retention_pass(self) -> None:
        """Delete posts older than :data:`DELETE_AFTER_SECONDS`; forget those
        older than :data:`FORGET_AFTER_SECONDS` without a call.

        A post's age counts from its first send; edits do not extend it. Each
        request spends a token from the writer's budget. A bulk delete fails as
        a whole when any one of its ids cannot be deleted, so a failed batch is
        retried one id at a time. A flood wait ends the pass instead of being
        waited out: by the time it is over a post may be past the 48 hours in
        which it can be deleted, and the next pass judges every age afresh.
        """
        if self._retention_lock.locked():
            return
        async with self._retention_lock:
            if self._bot is None or not self._chat_id or self._unreachable:
                return
            now = self._wall()
            forgotten = {int(mid) for mid, sent in self._posts
                         if now - sent > FORGET_AFTER_SECONDS}
            if forgotten:
                self._drop_posts(forgotten)
                logger.warning(
                    "agent log: %d posts were too old to delete and stay in the "
                    "group", len(forgotten))
            due = [int(mid) for mid, sent in sorted(self._posts, key=lambda p: p[1])
                   if now - sent > DELETE_AFTER_SECONDS]
            await self._delete_due(due)
            await self._save_if_due()

    async def _delete_due(self, due: list[int]) -> None:
        for start in range(0, len(due), DELETE_BATCH_LIMIT):
            batch = due[start:start + DELETE_BATCH_LIMIT]
            outcome = await self._delete_call(
                functools.partial(self._bot.delete_messages,
                                  chat_id=self._chat_id, message_ids=batch))
            if outcome == "stop":
                return
            if outcome == "ok":
                self._drop_posts(set(batch))
                continue
            for mid in batch:
                outcome = await self._delete_call(
                    functools.partial(self._bot.delete_message,
                                      chat_id=self._chat_id, message_id=mid))
                if outcome == "stop":
                    return
                if outcome in ("ok", "gone"):
                    self._drop_posts({mid})

    async def _delete_call(self, call: Callable[[], Awaitable[Any]]) -> str:
        """One delete request under the budget: ``ok``, ``gone`` (the id cannot
        or need not be deleted), ``failed``, or ``stop`` (a flood wait is in
        force, or the group is unreachable: the pass ends)."""
        if not await self._budget.acquire(unless_paused=True):
            return "stop"
        try:
            await call()
            return "ok"
        except RetryAfter as exc:
            self._pause(exc)
            return "stop"
        except Exception as exc:  # noqa: BLE001 - classified below
            if _matches(exc, _UNREACHABLE):
                self._disable_unreachable(exc)
                return "stop"
            if _matches(exc, _DELETE_GONE):
                return "gone"
            logger.info("agent log: delete failed: %s", exc)
            return "failed"

    def _drop_posts(self, ids: set[int]) -> None:
        self._posts = [p for p in self._posts if int(p[0]) not in ids]
        for state in self._keys.values():
            if state.message_id in ids:
                state.forget_message()
        self._dirty = True

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------

    def _reset_registry(self, chat_id: int) -> None:
        self._registry_chat = chat_id
        self._topics = {}
        self._posts = []
        for state in self._keys.values():
            state.forget_message()
        self._dirty = True

    def _load(self) -> None:
        """Read the registry. Missing → empty; corrupt → archived aside as
        ``<path>.casabak`` and treated as empty."""
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            chat = data.get("chat_id")
            topics = {str(k): int(v) for k, v in data["topics"].items()}
            posts = [[int(mid), float(sent)] for mid, sent in data["posts"]]
            if chat is not None:
                chat = int(chat)
        except FileNotFoundError:
            return
        except (ValueError, TypeError, KeyError, AttributeError):
            bak = self._path + ".casabak"
            try:
                os.replace(self._path, bak)
                logger.warning("agent log: registry %s is corrupt; archived to %s",
                               self._path, bak)
            except OSError:
                logger.warning("agent log: registry %s is corrupt and could not be "
                               "archived; starting empty", self._path, exc_info=True)
            return
        except OSError:
            logger.warning("agent log: registry %s could not be read; starting empty",
                           self._path, exc_info=True)
            return
        self._registry_chat = chat
        self._topics = topics
        self._posts = posts

    def _snapshot(self) -> dict[str, Any]:
        return {
            "chat_id": self._registry_chat,
            "topics": dict(self._topics),
            "posts": [[int(mid), sent] for mid, sent in self._posts],
        }

    async def _save_if_due(self) -> None:
        if self._dirty and self._monotonic() - self._last_save >= SAVE_INTERVAL_SECONDS:
            await self._save()

    async def _save(self) -> None:
        async with self._save_lock:
            snapshot = self._snapshot()
            self._dirty = False
            self._last_save = self._monotonic()
            try:
                await _to_thread_uncancellable(
                    functools.partial(atomic_write_json, self._path, snapshot,
                                      mode=PRIVATE))
            except Exception:  # noqa: BLE001 - retried at the next save
                self._dirty = True
                self._warn_internal(f"could not write {self._path}")
