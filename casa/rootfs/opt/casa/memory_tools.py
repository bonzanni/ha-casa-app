"""The memory read tools: search_memory, browse_memory_day, open_memory and
correct_memory, with the clearance each read runs at."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from claude_agent_sdk import tool

from tool_result import _result

logger = logging.getLogger("tools")

#: INV-MEM-010: on every search and browse result, empty or not, and the same
#: at every clearance — a note that varied would itself say something was
#: filtered out of this answer.
MEMORY_READ_NOTE = (
    "This is the saved conversation readable here, and the search or day is "
    "bounded. Finding nothing, or nothing on the topic, is not proof it was "
    "never said: say you found nothing you can share, never that it didn't "
    "happen."
)
#: #1116: says WHEN to tell, so an outage on a check nobody asked for (a
#: scheduled briefing) does not become a message.
_MEMORY_UNAVAILABLE = (
    "Memory could not be checked right now. Do NOT say the information "
    "doesn't exist or that you don't have it. If a person asked for something "
    "that needs memory, tell them memory couldn't be checked. On a check "
    "nobody asked for, this alone is not a reason to send anything; still "
    "report what the task itself requires."
)
_DAY_FORMS = (
    "a day YYYY-MM-DD, or today, yesterday, this_week, last_week, "
    "this_month, last_month (in the operator's timezone)")
_OPTIONAL_DAY = {"type": ["string", "null"]}
_OPTIONAL_PAGE = {"type": ["integer", "null"],
                  "description": "Optional page, from 1."}

# Pre-built schemas: the SDK makes every dict-of-types key required, and
# ``null`` (what a client sends for an unset optional) means not given.
SEARCH_MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string",
                  "description": "Words to look for in saved conversation."},
        "from": {**_OPTIONAL_DAY, "description": (
            "Optional first day to search: " + _DAY_FORMS
            + "; a named period means its first day.")},
        "to": {**_OPTIONAL_DAY, "description": (
            "Optional last day to search, inclusive: " + _DAY_FORMS
            + "; a named period means its last day.")},
        "page": _OPTIONAL_PAGE,
    },
    "required": ["query"],
}
BROWSE_MEMORY_DAY_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string", "description": (
            "The day to read: " + _DAY_FORMS + "; a named period reads "
            "all of it, up to 7 days.")},
        "to_date": {**_OPTIONAL_DAY, "description": (
            "Optional last day, inclusive, at most 7 days in all.")},
        "from_time": {**_OPTIONAL_DAY, "description": (
            "Optional HH:MM on the first day to start from.")},
        "to_time": {**_OPTIONAL_DAY, "description": (
            "Optional HH:MM on the last day to stop at.")},
        "page": _OPTIONAL_PAGE,
    },
    "required": ["date"],
}
OPEN_MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "ids": {"type": "array", "items": {"type": "string"},
                "description": "Ids from your memory's index lines."},
        "with_history": {"type": ["boolean", "null"], "description": (
            "Optional: also return each one's earlier versions.")},
    },
    "required": ["ids"],
}


def _memory_reader(origin: dict) -> str:
    """The clearance a memory read runs at: the same gate the turn path
    uses. A bound engagement's own record is the authority, read live (its
    markers and its channel): an in_casa engagement's call inherits the
    launching turn's ambient origin, but the clamp lowers only the record
    (#336, #369). Otherwise the origin's markers."""
    from sensitivity import clearance_for_origin
    import tools
    eng = tools.engagement_var.get(None)
    eng_origin = getattr(eng, "origin", None) if eng is not None else None
    if isinstance(eng_origin, dict):
        origin = eng_origin
    route, stamped = tools._origin_clearance_markers(origin)
    return clearance_for_origin(route, stamped, origin.get("channel", "telegram"))


async def _read_memory(origin: dict, read: Callable[[str], Any]) -> Any:
    """Run the blocking *read* at the reader's clearance, off the event loop.
    A clearance lowered while it ran (an engagement clamp, #369) reads again
    at the new one: the clamp is one-way over a few tiers."""
    from sensitivity import TIERS
    clearance = _memory_reader(origin)
    for _ in range(len(TIERS)):
        out = await asyncio.to_thread(read, clearance)
        now = _memory_reader(origin)
        if now == clearance:
            return out
        clearance = now
    return await asyncio.to_thread(read, clearance)


def _memory_store():
    from memory import capture
    store = capture.active_store()
    return store if store is not None and store.available else None


def _days(args: dict, first_key: str, last_key: str):
    """``(first, last, error)``: the local days *args* names, each ``None``
    when not given. A named period is its first day as a first bound and
    its last day as a last one."""
    from datetime import datetime

    from timekeeping import resolve_period, resolve_tz
    now = datetime.now(resolve_tz())
    out = []
    for key, end in ((first_key, False), (last_key, True)):
        spec = args.get(key)
        if spec is None or (isinstance(spec, str) and not spec.strip()):
            out.append(None)
            continue
        try:
            window = resolve_period(spec, now)
        except ValueError:
            return None, None, f"{key} must be {_DAY_FORMS}."
        out.append((window.end if end else window.start).date())
    first, last = out
    if first is not None and last is not None and last < first:
        return None, None, f"{last_key} is before {first_key}."
    return first, last, None


def _page(args: dict) -> int:
    try:
        return max(1, int(args.get("page") or 1))
    except (TypeError, ValueError):
        return 1


@tool(
    "search_memory",
    "Search the saved conversation (what he and Casa said, verbatim) for "
    "words, best match first. Optional from/to limit it to days. Each hit "
    "has its date, weekday, time, speaker and text, with the lines around "
    "it. To read a whole day in order use browse_memory_day; to open an "
    "entry of your memory by id use open_memory.",
    SEARCH_MEMORY_SCHEMA,
)
async def search_memory(args: dict) -> dict:
    from memory import search as memory_search
    import tools

    query = args.get("query") or ""
    if not memory_search.terms(query):
        return _result({"status": "error", "kind": "empty_query",
                        "message": "search_memory needs words to look for. "
                                   "Nothing was searched."})
    first, last, refused = _days(args, "from", "to")
    if refused:
        return _result({"status": "error", "kind": "invalid_days",
                        "message": refused + " Nothing was searched."})
    page = _page(args)
    origin = tools._snapshot_origin()
    store = _memory_store()
    if store is None:
        return _result({"status": "unavailable",
                        "message": _MEMORY_UNAVAILABLE})
    reader_role = origin.get("role")
    try:
        found = await _read_memory(origin, lambda clearance: (
            memory_search.search(
                store, query, clearance=clearance, first_day=first,
                last_day=last, page=page, reader_role=reader_role)))
    except Exception:  # noqa: BLE001 — never a fake empty result
        logger.warning("search_memory: memory could not be read",
                       exc_info=True)
        return _result({"status": "unavailable",
                        "message": _MEMORY_UNAVAILABLE})
    out: dict = {"status": "ok", "page": page, "hits": found.hits}
    if first is not None or last is not None:
        out["days"] = {"from": first and first.isoformat(),
                       "to": last and last.isoformat()}
    if found.more:
        out["more"] = f"More hits on page {page + 1}."
    out["note"] = MEMORY_READ_NOTE
    return _result(out)


@tool(
    "browse_memory_day",
    "Read the saved conversation of one day, or up to 7 days, in time "
    "order, optionally from a time on the first day to a time on the last. "
    "Long stretches come in pages. To find something by its words use "
    "search_memory.",
    BROWSE_MEMORY_DAY_SCHEMA,
)
async def browse_memory_day(args: dict) -> dict:
    from datetime import datetime

    from memory import browse as memory_browse
    import tools

    first, last, refused = _days(args, "date", "date")
    if refused or first is None:
        return _result({"status": "error", "kind": "invalid_days",
                        "message": (refused or f"date must be {_DAY_FORMS}.")
                        + " Nothing was read."})
    if args.get("to_date") not in (None, ""):
        _, last, refused = _days({"to": args.get("to_date")}, "from", "to")
        if refused or last < first:
            return _result({"status": "error", "kind": "invalid_days",
                            "message": (refused or "to_date is before date.")
                            + " Nothing was read."})
    if (last - first).days + 1 > memory_browse.MAX_DAYS:
        return _result({"status": "error", "kind": "too_many_days",
                        "message": (
                            f"browse_memory_day reads at most "
                            f"{memory_browse.MAX_DAYS} days; use search_memory "
                            "with from and to for a longer stretch. Nothing "
                            "was read.")})
    times = []
    for key in ("from_time", "to_time"):
        spec = args.get(key)
        if spec in (None, ""):
            times.append(None)
            continue
        try:
            times.append(datetime.strptime(str(spec).strip(), "%H:%M").time())
        except ValueError:
            return _result({"status": "error", "kind": "invalid_time",
                            "message": f"{key} must be HH:MM. Nothing was "
                                       "read."})
    page = _page(args)
    origin = tools._snapshot_origin()
    store = _memory_store()
    if store is None:
        return _result({"status": "unavailable",
                        "message": _MEMORY_UNAVAILABLE})
    reader_role = origin.get("role")
    try:
        got = await _read_memory(origin, lambda clearance: (
            memory_browse.browse(
                store, clearance=clearance, first_day=first, last_day=last,
                from_time=times[0], to_time=times[1], page=page,
                reader_role=reader_role)))
    except Exception:  # noqa: BLE001 — never a fake empty result
        logger.warning("browse_memory_day: memory could not be read",
                       exc_info=True)
        return _result({"status": "unavailable",
                        "message": _MEMORY_UNAVAILABLE})
    out: dict = {"status": "ok",
                 "days": {"from": first.isoformat(), "to": last.isoformat()},
                 "page": got.page, "pages": got.pages, "text": got.text}
    if got.total:
        out["events"] = f"{got.first}-{got.last} of {got.total}"
    out["note"] = MEMORY_READ_NOTE
    return _result(out)


@tool(
    "open_memory",
    "Open entries of your memory by the ids its index lines show: an entry "
    "comes back in full, a group or topic as the lines of what it holds. "
    "One that changed after your memory above was written is marked so. "
    "with_history adds earlier versions. To search saved conversation use "
    "search_memory.",
    OPEN_MEMORY_SCHEMA,
)
async def open_memory(args: dict) -> dict:
    from memory import view as memory_view
    import tools

    ids = args.get("ids")
    if isinstance(ids, str):
        ids = [ids]
    if (not isinstance(ids, list) or not ids
            or not all(isinstance(i, str) and i.strip() for i in ids)):
        return _result({"status": "error", "kind": "no_ids",
                        "message": "open_memory needs the ids from your "
                                   "memory's index lines. To find something "
                                   "by its words use search_memory."})
    if len(ids) > memory_view.MAX_OPEN:
        return _result({"status": "error", "kind": "too_many_ids",
                        "message": f"open_memory opens at most "
                                   f"{memory_view.MAX_OPEN} ids per call."})
    origin = tools._snapshot_origin()
    store = _memory_store()
    if store is None:
        return _result({"status": "unavailable",
                        "message": _MEMORY_UNAVAILABLE})
    session_key = sdk_session_id = None
    if tools.engagement_var.get(None) is None and origin.get("channel") \
            and origin.get("role"):
        import agent as agent_mod
        from session_registry import build_scoped_session_key
        session_key = build_scoped_session_key(
            origin["channel"], origin["role"], origin.get("chat_id"))
        registry = getattr(agent_mod, "active_session_registry", None)
        entry = (registry.get(session_key) if registry is not None
                 else None) or {}
        sdk_session_id = entry.get("sdk_session_id")
    try:
        opened = await _read_memory(origin, lambda clearance: (
            memory_view.open_objects(
                store, [i.strip() for i in ids], clearance=clearance,
                session_key=session_key, sdk_session_id=sdk_session_id,
                with_history=args.get("with_history") is True)))
    except Exception:  # noqa: BLE001 — never a fake "unknown"
        logger.warning("open_memory: memory could not be read", exc_info=True)
        return _result({"status": "unavailable",
                        "message": _MEMORY_UNAVAILABLE})
    if opened is None:
        return _result({"status": "ok", "entries": [], "message": (
            "No memory ledger is read here; search_memory reads the saved "
            "conversation.")})
    return _result({"status": "ok", "entries": opened})


#: Every arm in which the mark was not written: the store unavailable, busy
#: past the turn-path bound, or his message not stored yet. Worded like the
#: read tools' unavailable message: what happened, and what not to claim.
_CORRECTION_UNAVAILABLE = (
    "Memory could not mark the correction right now. Do NOT say memory was "
    "updated. Use his new value in this conversation; if he asks, tell him "
    "memory couldn't be updated just now."
)


@tool(
    "correct_memory",
    "Use when his message in this turn says something stored in memory is "
    "wrong or out of date. It marks his message as a correction: memory is "
    "updated from his own words, and every conversation sees the correction "
    "from its next turn. Returns at once; there is nothing to tell him. "
    "`what`: a few words naming what he corrected.",
    {"what": str},
)
async def correct_memory(args: dict) -> dict:
    import agent as agent_mod
    from memory import corrections
    import tools

    origin = tools._snapshot_origin()
    key = origin.get(agent_mod.MEMORY_OPENING_KEY)
    if tools.engagement_var.get(None) is not None or not key:
        return _result({"status": "error", "kind": "no_message_of_his",
                        "message": (
                            "correct_memory marks his own message in this "
                            "conversation, and this turn has none stored. "
                            "Nothing was marked.")})
    store = _memory_store()
    if store is None:
        return _result({"status": "unavailable",
                        "message": _CORRECTION_UNAVAILABLE})
    try:
        marked = await asyncio.to_thread(
            corrections.mark_operator_event, store, key)
    except Exception:  # noqa: BLE001 — reported, never a fake "queued"
        logger.warning("correct_memory: the mark could not be written",
                       exc_info=True)
        return _result({"status": "unavailable",
                        "message": _CORRECTION_UNAVAILABLE})
    if not marked.found:
        # His message may still be in the spool (captured while the store
        # was busy). It is never drained here — a drain takes as long as its
        # backlog and waits on spool writes — so ask for the background
        # drain and answer now; a later call marks it once it is stored.
        from memory import capture
        capture.request_drain(store)
        return _result({"status": "unavailable",
                        "message": _CORRECTION_UNAVAILABLE})
    return _result({"status": "queued", "message": (
        "His message is marked as a correction; memory will be updated from "
        "it. Nothing to tell him.")})
