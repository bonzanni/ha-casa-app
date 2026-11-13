"""A reply shown while it is written: the draft that follows one turn.

The model's message loop must never wait on the network, so the token
callback (:meth:`DraftStream.on_token`) only stores the latest text, and one
small task per turn does the sending on its own clock. Once per ``interval``
it sends the stored text if it changed since the last send; otherwise, when
``keepalive`` has passed since the last send and the text changed less than
``idle_limit`` ago, it sends the same text again so the draft does not
vanish.

The send itself is injected: the channel renders the text and calls the
messaging API. ``send`` returns ``None``, or a number of seconds meaning
"wait this long before the next call" (a flood wait); either way the text
then counts as sent. It raises :class:`DraftTooLong` when the text no longer
fits one message — from then on new text is no longer offered and the last
text that fitted keeps being re-sent under the same rules. Any other failure
is logged once per stream and otherwise ignored: the text counts as sent, so
a failed draft is never retried — it goes out again only as new text or at
the keepalive.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


class DraftTooLong(Exception):
    """Raised by ``send`` when the text no longer fits one message."""


class DraftStream:
    def __init__(self, *, send: Callable[[Any], Awaitable[float | None]],
                 cid: str | None, on_end: Callable[[], None] = lambda: None,
                 interval: float = 0.6, keepalive: float = 10.0,
                 idle_limit: float = 600.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.cid = cid
        self.ended = False
        self._send = send
        self._on_end = on_end
        self._interval = interval
        self._keepalive = keepalive
        self._idle_limit = idle_limit
        self._clock = clock
        self._sleep = sleep
        self._text: Any = None
        self._changed_at = 0.0
        self._sent: Any = None
        self._sent_at = 0.0
        self._has_sent = False
        self._too_long = False
        self._warned = False
        self._task: asyncio.Task | None = None

    async def on_token(self, text: Any) -> None:
        """Store the latest text; start the sending task at the first call."""
        if self.ended:
            return
        if self._task is None or text != self._text:
            self._changed_at = self._clock()
        self._text = text
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="telegram-draft-stream")

    def end(self) -> None:
        """Stop the stream: cancel the task and call ``on_end`` once."""
        if self.ended:
            return
        self.ended = True
        if self._task is not None:
            self._task.cancel()
        self._on_end()

    async def _run(self) -> None:
        while True:
            await self._sleep(self._interval)
            wait = await self._round()
            if wait:
                await self._sleep(wait)

    async def _round(self) -> float | None:
        now = self._clock()
        if not self._too_long and (not self._has_sent or self._text != self._sent):
            text = self._text
        elif (self._has_sent
              and now - self._sent_at >= self._keepalive
              and now - self._changed_at < self._idle_limit):
            text = self._sent
        else:
            return None
        try:
            wait = await self._send(text)
        except DraftTooLong:
            self._too_long = True
            return None
        except Exception:
            if not self._warned:
                self._warned = True
                logger.warning("draft send failed (cid=%s); carrying on",
                               self.cid, exc_info=True)
            wait = None
        self._sent = text
        self._sent_at = now
        self._has_sent = True
        return wait
