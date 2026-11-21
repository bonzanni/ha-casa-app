"""The configurator's progress line in the operator's DM: a short checklist.

A configurator run works for a while between the words it writes. This line
shows what it is doing as a checklist, one row per activity, in start order:
``- [ ] <label>`` while it is the step Casa is on, ``- [x] <label>`` once it is
over and Casa has moved on to a later step. A row never says how its step
turned out, only that Casa went on past it; the newest row stays unticked
until something later starts. Each row changes only through its own handle,
so a new row never ends an older one. Only the last ``MAX_ROWS`` rows show.

The list lives in one message, edited in place and deleted when the run ends.
A row made with ``keep=False`` leaves the list when it is over; if that
empties the list, the message is deleted, and a later row posts a new one.
Nothing is persisted. Sends, edits and deletes run in one background task this
object owns — coalesced (the newest wanted text wins) and spaced — so the
run never waits on Telegram, and each transport call is bounded.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

_SPACING_S = 3.0      # at least this long between two writes of the line
_IO_TIMEOUT_S = 3.0   # bound on each Telegram call and on the close's drain

# --- the label of a step -------------------------------------------------
#
# A row names the activity a tool call is, in the operator's words. A label
# is built before the tool's handler validates its arguments, so an argument
# is shown only when it is shaped like a short name; anything else reads as
# "the plugin" / "the agent". Only the arguments named below are ever read.

GENERIC_LABEL = "Work on it"

# Shown by their own rows elsewhere (a plugin install or update is staged).
STAGE_ROW_TOOLS = frozenset({"plugin_add", "plugin_update"})

_CASA_TOOL_PREFIX = "mcp__casa-framework__"
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")

# Looking around and housekeeping: no row.
_HIDDEN_BUILTINS = frozenset({"Read", "Glob", "Grep", "Write", "Edit"})
_HIDDEN_CASA = frozenset({
    "plugin_list", "persona_list", "specialist_install_inspect",
    "persona_install_inspect", "get_item_fields", "emit_completion",
    "trigger_ack_revoke", "callback_ack_revoke", "event_ack_revoke",
    "persona_ack_revoke", "consent_reprompt",
})


def _short_name(value: object) -> str | None:
    if isinstance(value, str) and _NAME_RE.fullmatch(value):
        return value
    return None


def plugin_label(slug: str) -> str:
    """A plugin's name for a label: ``"todo-list"`` → ``"Todo List"``."""
    name = _short_name(slug)
    if name is None:
        return "the plugin"
    return " ".join(w.capitalize() for w in re.split(r"[-_]+", name) if w)


def target_label(target: str, agent_name: Callable[[str], str]) -> str:
    """An agent's name for a label, from ``resident:<role>``,
    ``specialist:<role>`` or a bare role."""
    role = target if isinstance(target, str) else ""
    for prefix in ("resident:", "specialist:"):
        if role.startswith(prefix):
            role = role[len(prefix):]
            break
    if _short_name(role) is None:
        return "the agent"
    shown = agent_name(role)
    return f"the {role} agent" if shown == role else shown


_Formatter = Callable[[dict, Callable[[str], str]], str]


def _fixed(label: str) -> _Formatter:
    return lambda _args, _agent_name: label


_LABELS: dict[str, _Formatter] = {
    "plugin_assign": lambda a, n: (
        f"Set up {plugin_label(a.get('name'))} for {target_label(a.get('target'), n)}"),
    "plugin_unassign": lambda a, n: (
        f"Take {plugin_label(a.get('name'))} away from "
        f"{target_label(a.get('target'), n)}"),
    "plugin_remove": lambda a, n: f"Remove {plugin_label(a.get('name'))}",
    "verify_plugin_state": lambda a, n: f"Check {plugin_label(a.get('plugin_name'))}",
    "list_vault_items": _fixed("Look in 1Password"),
    "set_plugin_env_reference": lambda a, n: (
        f"Save a setting for {plugin_label(a.get('plugin'))}"),
    "remove_plugin_env_reference": lambda a, n: (
        f"Remove a setting from {plugin_label(a.get('plugin'))}"),
    "config_git_commit": _fixed("Save the changes"),
    "casa_reload": _fixed("Apply the changes"),
    "casa_reload_triggers": _fixed("Apply the changes"),
    "casa_restart_supervised": _fixed("Arrange a restart"),
    "config_trigger_upsert": _fixed("Write the automation"),
    "config_trigger_delete": _fixed("Remove the automation"),
    "specialist_install_request_approval": _fixed(
        "Check whether your approval is needed"),
    "specialist_install_commit": _fixed("Install the new agent"),
    "specialist_upgrade": lambda a, n: f"Update {target_label(a.get('slug'), n)}",
    "specialist_rollback": lambda a, n: (
        f"Put {target_label(a.get('slug'), n)} back as it was"),
    "specialist_uninstall": lambda a, n: f"Remove {target_label(a.get('slug'), n)}",
    "persona_install_commit": _fixed("Add the personality"),
    "persona_apply": _fixed("Change the personality"),
    "resident_persona_swap": _fixed("Change the personality"),
    "resident_persona_reset": _fixed("Change the personality"),
    "persona_remove": _fixed("Clear out unused personalities"),
    "persona_prune": _fixed("Clear out unused personalities"),
}


def step_label(
    tool_name: str, tool_input: dict, *, agent_name: Callable[[str], str],
) -> str | None:
    """The row label for one tool call; ``None`` when the call gets no row.

    *agent_name* maps a role to its display name (the role itself when it has
    none). A tool the table does not list reads as ``GENERIC_LABEL`` and none
    of its arguments is looked at.
    """
    name = tool_name or ""
    if name in _HIDDEN_BUILTINS:
        return None
    if not name.startswith(_CASA_TOOL_PREFIX):
        return GENERIC_LABEL
    short = name[len(_CASA_TOOL_PREFIX):]
    if short in STAGE_ROW_TOOLS or short in _HIDDEN_CASA:
        return None
    formatter = _LABELS.get(short)
    if formatter is None:
        return GENERIC_LABEL
    args = tool_input if isinstance(tool_input, dict) else {}
    return formatter(args, agent_name)


# Closes run here, never awaited by the run — a hung Telegram must not delay
# it, and a cancellation landing while the line closes must not become the
# run's outcome. Strong references until each close finishes.
_closing: set[asyncio.Task] = set()

MAX_ROWS = 8           # the rows the message shows: the last ones made

# Characters a label could carry that rich Markdown would read as formatting
# (or as the start of a link, a tag or a table cell): each is written escaped,
# so the row shows the label exactly as it is.
_MARKUP = re.compile(r"([\\`*_\[\]<>~|])")


def _row_text(r: "StepRow", ticked: bool) -> str:
    box = "[x]" if ticked else "[ ]"
    label = _MARKUP.sub(r"\\\1", r.label)
    return f"- {box} {label}"


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class StepRow:
    """The handle of one row. Only the first ``done()`` counts."""

    __slots__ = ("label", "keep", "over", "_seq", "_line")

    def __init__(self, line: "TurnProgressLine", label: str, keep: bool,
                 seq: int) -> None:
        self.label = label
        self.keep = keep
        self.over = False
        self._seq = seq          # its place in start order
        self._line = line

    def done(self) -> None:
        """The step is over, however it went."""
        self._line._settle(self)


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
        self._made = 0           # rows made so far, ever
        self._closed = False
        # None: no message wanted / none shown
        self._wanted: str | None = None
        self._shown: str | None = None
        self._message_id: int | None = None
        self._post_failed = False
        self._last_write: float | None = None
        self._pump: asyncio.Task | None = None

    def row(self, label: str, *, keep: bool = True) -> StepRow:
        """Append an unticked row and return its handle. Never awaits."""
        r = StepRow(self, label, keep, self._made)
        self._made += 1
        self._list.append(r)
        self._refresh()
        return r

    def _settle(self, r: StepRow) -> None:
        if r.over:
            return
        r.over = True
        if not r.keep:
            self._list.remove(r)
        self._refresh()

    def _refresh(self) -> None:
        if self._closed:
            return
        shown = self._list[-MAX_ROWS:]
        # ticked: over, and a later row was made (Casa moved on past it)
        self._wanted = "\n".join(
            _row_text(r, r.over and r._seq < self._made - 1) for r in shown) or None
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
        if not self._made:
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
