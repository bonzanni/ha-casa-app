"""Stopping work a specialist's plugin carries on by itself (browsing), at once.

A plugin that declares ``casa.stopTool`` (``plugin_store.manifest_stop_tool``) is
called by Casa **directly**, exactly as its ``casa.driverSweep`` is: Casa starts
the plugin's stdio server itself, with no model, no session and no desk
(``plugin_continuations.sweep_server`` / ``call_plugin_tool``), so the stop never
waits for the turn that is driving the work and costs no model turn. The tool
answers one JSON object: with no arguments, ``{"requests": [{"ref", "tasks",
"state"}]}`` — what is running; with ``{"ref": "request:<key>"}``, the outcome of
stopping that request — ``stopped``, ``ended`` (it had ended on its own),
``already_stopped``, ``not_taken`` (the plugin could not reach its app; it keeps
refusing to drive the request and retries by itself) or ``unknown``; a
``stopped`` answer carries ``refs``, every continuation reference naming the
request, and Casa drops the continuations still queued for them.

Three paths reach it, all bound to the configured operator: the Stop on the
request's start or status card (``channels.telegram``'s ``run_stop`` callback),
the Stop of a plugin's own card whose stored call names the stop tool
(``TelegramChannel._dispatch_proposal_tap``), and the resident's ``stop_work``
tool on the operator's own live turn. One request is stopped by one path at a
time (``in_flight``); the path that holds the flight marks the card
(``settle_run_card``) and tells him, while it still holds it.

Every lookup names the Telegram bot the Stop came through as well as the plugin
and the operator: one plugin can serve agents on two bots, and its request keys
may be equal on both, so a Stop on one bot never stops, marks or drops the
other's request. The card is edited, and the line posted, through the bot that
holds the card.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

STOP_CALL_TIMEOUT_S = 60.0
REF_PREFIX = "request:"
REF_MAX = 64
OUTCOMES = frozenset({"stopped", "ended", "already_stopped", "not_taken", "unknown"})
#: the outcomes after which the plugin keeps refusing to drive the request: its queued
#: continuations would only start turns that end silent, so Casa drops them
KEEPS_SEAL = frozenset({"stopped", "already_stopped", "not_taken"})

#: the (plugin segment, bot id, ref) stops in flight in this process
_FLIGHTS: set[tuple[str, int, str]] = set()
#: the stop calls still running, the stdio teardown included (drained at Casa's stop)
_TASKS: set[asyncio.Task] = set()


async def stop_plugins() -> list[tuple]:
    """``(role, cfg, rp, seg, tool)`` for every loaded specialist's resolved plugin that
    declares ``casa.stopTool``, by the driver sweep's own resolution."""
    from plugin_continuations import declaring_plugins
    from plugin_store import manifest_stop_tool
    return await declaring_plugins(manifest_stop_tool, context="stop tool")


def _parse(text: str, args: dict) -> dict:
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = None
    if "ref" not in args:
        if not isinstance(data, dict):
            return {"requests": [], "failed": True}    # the plugin could not make its list
        requests = data.get("requests")
        return {"requests": [r for r in requests if isinstance(r, dict)]
                if isinstance(requests, list) else []}
    outcome = data.get("outcome") if isinstance(data, dict) else None
    if outcome not in OUTCOMES:
        return {"outcome": "not_taken"}
    out: dict = {"outcome": outcome}
    tasks = data.get("tasks")
    if isinstance(tasks, list):
        out["tasks"] = [str(t)[:200] for t in tasks if isinstance(t, str)][:8]
    refs = data.get("refs")
    if outcome in KEEPS_SEAL and isinstance(refs, list):
        out["refs"] = [r for r in refs if isinstance(r, str) and 0 < len(r) <= REF_MAX
                       and r.isprintable()]
    return out


async def call_stop(rp, tool: str, args: dict) -> dict:
    """Call the plugin's stop tool directly with *args* and parse its answer. The answer is
    handed over from inside the client session, so the caller never waits on the stdio
    teardown, which then runs in the same tracked task. A timeout, a failure or any other
    shape is ``not_taken`` (a listing: no requests)."""
    from plugin_continuations import call_plugin_tool, sweep_server
    server = sweep_server(rp)
    if server is None:
        return {"requests": []} if "ref" not in args else {"outcome": "not_taken"}
    handed = asyncio.get_running_loop().create_future()
    task = asyncio.ensure_future(call_plugin_tool(server, tool, args, handed=handed))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    task.add_done_callback(_consume)
    done, _ = await asyncio.wait({handed, task}, timeout=STOP_CALL_TIMEOUT_S,
                                 return_when=asyncio.FIRST_COMPLETED)
    if handed.done() and not handed.cancelled():
        return _parse(handed.result(), args)
    if not task.done():
        task.cancel()
    logger.warning("stop tool call did not answer (%s)", "timeout" if not done else "failed")
    return _parse("", args)


def _consume(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.warning("stop tool call failed (%s)", type(task.exception()).__name__)


async def stop(role: str, seg: str, ref: str, *, bot_id: int, operator_id: int,
               tell: "Callable[[dict], Awaitable[None]] | None" = None) -> dict:
    """Stop the work *ref* names in *role*'s plugin *seg*, asked on bot *bot_id*. One flight
    per ``(seg, bot, ref)``: a second call while one runs answers ``in_flight`` at once and
    tells nothing. The flight is held through *tell* — the caller's card mark and line — so
    only the path that got the outcome tells him. A plugin no longer resolved for *role* is
    ``unavailable``."""
    key = (seg, int(bot_id), ref)
    if key in _FLIGHTS:
        return {"outcome": "in_flight"}
    _FLIGHTS.add(key)
    try:
        found = next(((rp, tool) for r, _cfg, rp, s, tool in await stop_plugins()
                      if r == role and s == seg), None)
        if found is None:
            outcome: dict = {"outcome": "unavailable"}
        else:
            outcome = await call_stop(found[0], found[1], {"ref": ref})
        if outcome.get("outcome") in KEEPS_SEAL:
            _drop_continuations(seg, bot_id, operator_id, ref, outcome.get("refs") or [])
        logger.info("stop of %s: %s", seg, outcome.get("outcome"))
        if tell is not None:
            await tell(outcome)
        return outcome
    finally:
        _FLIGHTS.discard(key)


def _drop_continuations(seg: str, bot_id: int, operator_id: int, ref: str,
                        refs: list) -> None:
    import plugin_continuations
    wanted = list(refs)
    c = confirmation_for(seg, bot_id, operator_id, ref)
    if c is not None:
        wanted.append(f"admission:{c.cid}")      # the start card's own continuation
    plugin_continuations.CONTINUATIONS.drop(seg, bot_id, operator_id, wanted)


def confirmation_for(seg: str, bot_id: int, operator_id: int, ref: str):
    """The admission (``run_admission.Confirmation``) of a ``request:<key>`` ref asked on
    bot *bot_id*, if Casa holds it."""
    import run_admission
    if not ref.startswith(REF_PREFIX):
        return None
    return run_admission.CONFIRMATIONS.by_key(plugin=seg, bot_id=int(bot_id),
                                              operator_id=int(operator_id),
                                              request_key=ref[len(REF_PREFIX):])


def _transport():
    from channels.telegram_transport import telegram_transport
    return telegram_transport()


#: the card mark and the one line, by outcome (``None``: the card keeps its Stop)
MARKS = {"stopped": "☑ Stopped", "ended": "☑ Ended", "already_stopped": "☑ Stopped",
         "not_taken": "☑ Stop asked"}
LINES = {"stopped": "Stopped.", "ended": "It had already ended.",
         "not_taken": "Casa Browser isn't answering; the browsing stops as soon as it does.",
         "unavailable": "I couldn't stop it: {name}'s browser isn't set up any more."}


def line_for(outcome: str, role: str) -> "str | None":
    import tools as tools_mod
    line = LINES.get(outcome)
    return None if line is None else line.format(name=tools_mod._display_name_for_role(role))


async def settle_run_card(c: Any, mark: str) -> bool:
    """Edit the request's start or status card to its text plus *mark*, with no keyboard, so
    a card never keeps a live Stop for a request that is stopped or ended. Nothing when no
    card landed (or a natural end already forgot it, ``settle_ended``). The edit goes through
    the bot that posted the card; a bot with no endpoint any more raises
    ``EndpointUnavailable`` and no other bot is tried. True when the edit landed."""
    from plugin_bindings import BINDINGS
    card = c.card_address() if c is not None else None
    if card is None:
        return False
    b = BINDINGS.peek(c.binding_ref)
    transport = _transport()
    if b is None or transport is None:
        return False
    return bool(await transport.endpoint(card).edit_dm_message(
        card.chat_id, card.message_id, f"{c.card_text}\n{mark}"))


async def settle_ended(seg: str, bot_id: int, operator_id: int, ref: str) -> bool:
    """The plugin says the request *ref* ended on its own (its driver sweep's ``ended``): its
    card is marked ``☑ Ended`` and loses its Stop — the mark a Stop that finds it ended gives
    it. Nothing while a Stop of it is in flight (that path marks the card), and nothing for a
    request Casa did not admit or whose card this has already marked. The plugin names the
    request on every pass for a day, so the card is forgotten after one attempt
    (``Confirmations.clear_card``), even when the edit fails: a card that cannot be edited is
    not retried every pass, and its Stop then answers that it had already ended."""
    import run_admission
    from plugin_bindings import BINDINGS
    if (seg, int(bot_id), ref) in _FLIGHTS:
        return False
    c = confirmation_for(seg, bot_id, operator_id, ref)
    card = c.card_address() if c is not None and c.status == "admitted" else None
    if card is None:
        return False
    transport = _transport()
    channel = transport.endpoint(card) if transport is not None else None
    if (BINDINGS.peek(c.binding_ref) is None or channel is None
            or getattr(channel, "bot", None) is None):
        return False                    # no edit can be tried yet (no channel, or its bot is
        #                                 being rebuilt): the next pass tries
    landed = await settle_run_card(c, MARKS["ended"])
    run_admission.CONFIRMATIONS.clear_card(c.cid)
    logger.info("request of %s ended on its own: card %s", seg,
                "marked" if landed else "not marked")
    return landed


async def tell_line(bot_id: int, chat_id: int, role: str, outcome: str, *,
                    reply_to: "int | None") -> None:
    """The one line, Casa's fixed text under the specialist's label, as a reply to the card,
    through the bot *bot_id* that holds it; the resident hears it at its next turn, as every
    Casa line."""
    import specialist_desk
    from result_broker import compose_operator_message, post_label
    line = line_for(outcome, role)
    transport = _transport()
    if line is None or transport is None:
        return
    await transport.endpoint(int(bot_id)).send_reply_line(
        int(chat_id), compose_operator_message(line, post_label(role)), reply_to)
    specialist_desk.record_echo(int(bot_id), int(chat_id),
                                f"{specialist_desk.label_for(role)}: {line}")


async def drain() -> None:
    for t in list(_TASKS):
        t.cancel()
    await asyncio.gather(*list(_TASKS), return_exceptions=True)
