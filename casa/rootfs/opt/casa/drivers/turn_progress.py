"""The configurator's progress line in the operator's DM: a short checklist.

A configurator run works for a while between the words it writes. This line
shows what it is doing as a list of rows, one per activity, in start order:
``⏳ <label>`` while it runs, ``✓ <label>`` once done, ``✗ <label>`` if it
failed. Each row changes only through its own handle, so a new row never
settles an older one. Only the last ``MAX_ROWS`` rows show.

The list lives in one message, edited in place and deleted when the run ends.
A row made with ``keep=False`` leaves the list when it settles; if that
empties the list, the message is deleted, and a later row posts a new one.
Nothing is persisted. Sends, edits and deletes run in one background task this
object owns — coalesced (the newest wanted text wins) and spaced — so the
run never waits on Telegram, and each transport call is bounded.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

_SPACING_S = 3.0      # at least this long between two writes of the line
_IO_TIMEOUT_S = 3.0   # bound on each Telegram call and on the close's drain

# Tool-use activity phrases.
_MCP_SERVER_CAP = 32      # §5 P1-B: cap the mcp server substring AT SOURCE.

# §5 S5: coarse tool_use → activity mapping.
_ACTIVITY_READING = frozenset({"Read", "Glob", "Grep"})
_ACTIVITY_EDITING = frozenset({"Write", "Edit", "NotebookEdit"})
_ACTIVITY_RESEARCH = frozenset({"WebFetch", "WebSearch"})


# #1350: Casa's own tools, in the operator's words — "using casa-framework
# tools" told the operator nothing about what was happening. Keyed on
# the tool name after the ``mcp__casa-framework__`` prefix: exact names first,
# then name prefixes; anything unlisted keeps the generic phrase.
_CASA_TOOL_PREFIX = "mcp__casa-framework__"
_CASA_ACTIVITY_EXACT: dict[str, str] = {
    "config_git_commit": "committing the change",
    "casa_reload": "reloading Casa",
    "casa_reload_triggers": "reloading Casa",
    "casa_restart_supervised": "reloading Casa",
    "list_vault_items": "wiring secrets",
    "get_item_fields": "wiring secrets",
    "set_plugin_env_reference": "wiring secrets",
    "remove_plugin_env_reference": "wiring secrets",
    "emit_completion": "wrapping up",
}
_CASA_ACTIVITY_PREFIX: tuple[tuple[str, str], ...] = (
    ("plugin_", "updating plugins"),
    ("specialist_", "managing specialists"),
    ("persona_", "managing personas"),
    ("resident_persona_", "managing personas"),
    ("config_trigger_", "updating triggers"),
)


def _casa_activity(name: str) -> str | None:
    if not name.startswith(_CASA_TOOL_PREFIX):
        return None
    short = name[len(_CASA_TOOL_PREFIX):]
    if short in _CASA_ACTIVITY_EXACT:
        return _CASA_ACTIVITY_EXACT[short]
    for prefix, phrase in _CASA_ACTIVITY_PREFIX:
        if short.startswith(prefix):
            return phrase
    return None


def activity_for_tool(tool_name: str) -> str:
    """Coarse activity phrase for a ``tool_use`` block (§5 S5).

    Read/Glob/Grep → ``reading files``; Write/Edit/NotebookEdit → ``editing
    files``; Bash → ``running commands``; Task*/TodoWrite → ``planning``;
    WebFetch/WebSearch → ``researching``; ``mcp__<server>__…`` → ``using
    <server> tools``; anything else → ``working``.
    """
    name = tool_name or ""
    if name in _ACTIVITY_READING:
        return "reading files"
    if name in _ACTIVITY_EDITING:
        return "editing files"
    if name == "Bash":
        return "running commands"
    if name.startswith("Task") or name == "TodoWrite":
        return "planning"
    if name in _ACTIVITY_RESEARCH:
        return "researching"
    casa = _casa_activity(name)
    if casa is not None:
        return casa
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) > 1 and parts[1] else "mcp"
        # §5 P1-B: cap the (unbounded) server substring AT SOURCE so the status
        # line can never blow the 4096 wire bound.
        return f"using {server[:_MCP_SERVER_CAP]} tools"
    return "working"


# Closes run here, never awaited by the run — a hung Telegram must not delay
# it, and a cancellation landing while the line closes must not become the
# run's outcome. Strong references until each close finishes.
_closing: set[asyncio.Task] = set()

MAX_ROWS = 8           # the rows the message shows: the last ones made

_RUNNING, _DONE, _FAILED = "⏳", "✓", "✗"


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class StepRow:
    """The handle of one row. Only the first ``done()`` / ``failed()`` counts."""

    __slots__ = ("label", "keep", "mark", "_line")

    def __init__(self, line: "TurnProgressLine", label: str, keep: bool) -> None:
        self.label = label
        self.keep = keep
        self.mark = _RUNNING
        self._line = line

    def done(self) -> None:
        self._line._settle(self, _DONE)

    def failed(self) -> None:
        self._line._settle(self, _FAILED)


class TurnProgressLine:
    """The progress line of one run. ``send(text)`` posts it and returns the
    message id (or None); ``edit(message_id, text)`` edits it;
    ``delete(message_id)`` removes it."""

    def __init__(
        self, *,
        send: Callable[[str], Awaitable[int | None]],
        edit: Callable[[int, str], Awaitable[bool]],
        delete: Callable[[int], Awaitable[bool]],
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = _default_sleep,
        spacing_s: float = _SPACING_S,
        io_timeout_s: float = _IO_TIMEOUT_S,
    ) -> None:
        self._send = send
        self._edit = edit
        self._delete = delete
        self._now = now
        self._sleep = sleep
        self._spacing_s = spacing_s
        self._io_timeout_s = io_timeout_s
        self._list: list[StepRow] = []
        self._made_any = False
        self._closed = False
        # None: no message wanted / none shown
        self._wanted: str | None = None
        self._shown: str | None = None
        self._message_id: int | None = None
        self._post_failed = False
        self._last_write: float | None = None
        self._pump: asyncio.Task | None = None

    def row(self, label: str, *, keep: bool = True) -> StepRow:
        """Append a running row and return its handle. Never awaits."""
        r = StepRow(self, label, keep)
        self._made_any = True
        self._list.append(r)
        self._refresh()
        return r

    def _settle(self, r: StepRow, mark: str) -> None:
        if r.mark != _RUNNING:
            return
        r.mark = mark
        if not r.keep:
            self._list.remove(r)
        self._refresh()

    def _refresh(self) -> None:
        if self._closed:
            return
        shown = self._list[-MAX_ROWS:]
        self._wanted = "\n".join(f"{r.mark} {r.label}" for r in shown) or None
        if self._pump is None or self._pump.done():
            self._pump = asyncio.ensure_future(self._run())

    async def _write(self, text: str | None) -> None:
        if text is None and self._message_id is None:
            # the list emptied while no message was up (a row came and went
            # during the spacing): nothing to delete
            self._shown = None
            return
        try:
            if text is None:
                # the list emptied: the message goes, never an empty edit
                await asyncio.wait_for(
                    self._delete(self._message_id), self._io_timeout_s)
                self._message_id = None
            elif self._message_id is None:
                mid = await asyncio.wait_for(self._send(text), self._io_timeout_s)
                if mid is None:
                    self._post_failed = True
                    return
                self._message_id = mid
            else:
                await asyncio.wait_for(
                    self._edit(self._message_id, text), self._io_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — progress is never the run's fault
            if self._message_id is None:
                self._post_failed = True
            logger.info("turn progress line write failed: %s", type(exc).__name__)
            return
        finally:
            self._last_write = self._now()
        self._shown = text

    async def _run(self) -> None:
        while self._wanted != self._shown and not self._post_failed:
            if self._wanted is None and self._message_id is None:
                return  # nothing up, nothing wanted
            if self._last_write is not None:
                wait = self._spacing_s - (self._now() - self._last_write)
                if wait > 0:
                    await self._sleep(wait)
            text = self._wanted
            await self._write(text)
            if self._shown != text and self._message_id is not None:
                return  # a write failed: leave it to the next change or the close

    def finish(self) -> None:
        """The run ended: close the line in the background. Never awaits, never
        raises; a run that made no row posted nothing and spawns nothing."""
        if not self._made_any:
            return
        self._closed = True
        task = asyncio.ensure_future(self.close())
        _closing.add(task)
        task.add_done_callback(_closing.discard)

    async def close(self) -> None:
        """Drains the pending write (bounded), waits out the spacing, then
        deletes the message once (bounded)."""
        self._closed = True
        pump = self._pump
        if pump is not None and not pump.done():
            try:
                await asyncio.wait_for(asyncio.shield(pump), self._io_timeout_s)
            except asyncio.CancelledError:
                pump.cancel()
                raise
            except Exception:  # noqa: BLE001 — a timeout included
                pump.cancel()
                # settle it, so the spacing below counts its last write
                await asyncio.gather(pump, return_exceptions=True)
        if self._message_id is None:
            return
        if self._last_write is not None:
            wait = self._spacing_s - (self._now() - self._last_write)
            if wait > 0:
                await self._sleep(wait)
        try:
            await asyncio.wait_for(self._delete(self._message_id), self._io_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.info("turn progress line delete failed: %s", type(exc).__name__)
