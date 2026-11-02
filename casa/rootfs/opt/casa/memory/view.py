"""The served view: one ledger rendered for a session, with no model call.

A session of a role whose ``memory.token_budget`` is above 0 reads ledger
*L*, the highest maintained ledger at or below its clearance
(:func:`ledger_for`). The view is built locally from *L*'s rows:

* **whole**: every current entry in full, grouped under its topic heading,
  while that fits the budget;
* **index**: past the budget. Levels are added only when the level below no
  longer fits: entry lines (each entry's need line, never its value), then
  group lines (an entry under a group folds into its group's line), then
  topic lines. The finest level that fits is the start; if even the coarsest
  overflows it is served anyway, because overflow beats invisibility, and
  the view says so (``overflow``). From the start, the cheapest refinement
  that still fits is applied, again and again: a topic line opened into its
  members' lines, a group line into its entries' lines, an entry line into
  the full entry. Nothing is pinned and nothing is ranked by age.

Every rendering goes through :func:`render_as_of`, which rebuilds *L* as it
stood at a ``commit_no`` from ``revisions`` (every revision row holds the
object's whole state). Serving the current ledger is rendering as of the
current ``commit_no``, so a session reconnecting later can re-render its own
view from its recorded ``base_commit_no`` and check the hash.

:func:`serve` is the session-start producer: in one read transaction it
renders the view and takes the snapshot's ``commit_no`` as the session's
base, then records the session's row in ``session_views``
(:mod:`memory.sessions`), which :func:`bind` binds to the SDK session once
its id is known. A session is frozen on that view: a later commit reaches
it only through the per-turn correction note (:mod:`memory.note`) or at its
next session.

:func:`open_objects` is ``open_memory``'s read: objects of the session's
own ledger at their latest committed revision, marked when they changed
after the session's view.

:func:`unreachable` is the writer commit's reachability check.

Blocking; async callers use ``asyncio.to_thread``.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import logging
import sqlite3
from dataclasses import dataclass, field

from memory import ledgers, pending, sessions
from memory.store import MemoryStore

logger = logging.getLogger(__name__)

#: Bumped whenever the rendering changes, so a frozen view recorded by an
#: earlier renderer is not mistaken for this one's.
RENDERER_VERSION = "1"

#: The turn-path bound: seconds any connection on a turn's path — serving,
#: the correction note, and every memory tool's store and index connection —
#: waits on another connection's lock. It waits no longer than capture does
#: (``memory.capture.CAPTURE_BUSY_TIMEOUT_S``): a contended store degrades the
#: turn to no view at once.
BUSY_TIMEOUT_S = 0.05

WHOLE = "whole"
INDEX = "index"

_LEVEL = {"entry": 0, "group": 1, "topic": 2}
_OTHER = "Other"

HEAD_WHOLE = (
    "[Your long-term memory: what he has told Casa, as dated entries grouped "
    "by topic, current value first; an earlier value is noted as \"earlier: "
    "... (until <date>)\". Kept current from past conversations; not live "
    "state.]")
HEAD_INDEX = (
    "[Your long-term memory: what he has told Casa, grouped by topic. Some "
    "entries are in full, dated, current value first; an earlier value is "
    "noted as \"earlier: ... (until <date>)\". A line that starts with an id "
    "is an index line: it names what that entry, or that group of entries, "
    "holds, never the value itself; open_memory with the id returns it. Open "
    "an entry whenever its need might touch the turn; opening is cheap. Kept "
    "current from past conversations; not live state.]")
HEAD_OVERFLOW = (
    "[Your memory is larger than this view can hold in full detail; every "
    "entry is still reachable through the lines above.]")


def tokens(text: str | None) -> int:
    """A cheap token estimate: four characters a token."""
    return len(text or "") // 4


def ledger_for(clearance: str | None) -> str | None:
    """The ledger a session at *clearance* reads: the highest maintained
    ledger at or below it (:mod:`memory.ledgers`), or ``None`` when there is
    none. An unknown clearance reads none."""
    if clearance not in pending.TIERS:
        return None
    best = None
    for ledger in ledgers.maintained():
        if pending.rank(ledger) <= pending.rank(clearance) and (
                best is None or pending.rank(ledger) > pending.rank(best)):
            best = ledger
    return best


# --- the ledger as of a commit ---------------------------------------------

@dataclass
class Obj:
    """One current object of the ledger, as of the view's commit."""

    object_id: str
    kind: str
    entry_kind: str | None
    errand_state: str | None
    topic: str | None
    subject: str | None
    body: str | None
    line: str | None
    cues: list
    parent: str | None
    source_seqs: list
    #: Sort key: the commit that created the object, then its id.
    order: tuple = ()
    #: The latest local day among the events it was written from.
    day: str | None = None


_COLUMNS = ("object_id", "kind", "entry_kind", "errand_state", "topic",
            "subject", "body", "line", "cues", "parent", "source_seqs",
            "status")


def objects_as_of(conn: sqlite3.Connection, ledger: str,
                  commit_no: int) -> list[Obj]:
    """*ledger*'s current objects as they stood after commit *commit_no*,
    in creation order, each dated from its source events."""
    rows = conn.execute(
        f"SELECT {', '.join('r.' + c for c in _COLUMNS)}, m.first "
        "FROM revisions AS r JOIN (SELECT object_id, max(revision) AS rev, "
        "min(commit_no) AS first FROM revisions WHERE ledger = ? "
        "AND commit_no <= ? GROUP BY object_id) AS m "
        "ON r.object_id = m.object_id AND r.revision = m.rev "
        "WHERE r.ledger = ?", (ledger, commit_no, ledger)).fetchall()
    objs = []
    for row in rows:
        values = dict(zip((*_COLUMNS, "first"), row))
        if values.pop("status") != "current":
            continue
        first = values.pop("first")
        values["cues"] = _json_list(values["cues"])
        values["source_seqs"] = _json_list(values["source_seqs"])
        objs.append(Obj(**values, order=(first, values["object_id"])))
    objs.sort(key=lambda o: o.order)
    _date(conn, objs, ledger)
    return objs


def _json_list(text: str | None) -> list:
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


#: An event's tier rank, as SQL.
_TIER_RANK = ("(CASE tier WHEN 'public' THEN 0 WHEN 'friends' THEN 1 "
              "WHEN 'family' THEN 2 ELSE 3 END)")


def _date(conn: sqlite3.Connection, objs: list[Obj], ledger: str) -> None:
    """Date each object by the latest local day among its source events
    that are readable at *ledger* (classified at or below it, or any for
    ``private``): a source id naming an event *ledger* cannot read dates
    nothing, and nor does one that cannot be a ``seq`` (never bound)."""
    seqs: set[int] = set()
    for o in objs:
        for s in o.source_seqs:
            try:
                value = int(s)
            except (TypeError, ValueError, OverflowError):
                continue
            if pending.is_seq(value):
                seqs.add(value)
    if not seqs:
        return
    days: dict[int, str] = {}
    ordered = sorted(seqs)
    for i in range(0, len(ordered), 500):
        chunk = ordered[i:i + 500]
        days.update(conn.execute(
            f"SELECT seq, local_day FROM events WHERE seq IN "
            f"({', '.join('?' for _ in chunk)}) AND {_TIER_RANK} <= ? "
            "AND (tier_status = 'classified' OR ? = 'private')",
            (*chunk, pending.rank(ledger), ledger)).fetchall())
    for o in objs:
        found = []
        for s in o.source_seqs:
            try:
                day = days.get(int(s))
            except (TypeError, ValueError, OverflowError):
                day = None
            if day:
                found.append(day)
        o.day = max(found) if found else None


# --- composition -------------------------------------------------------------

@dataclass
class Composition:
    """What a view shows: each entry in full, as its line, or folded into
    a group or topic line."""

    mode: str
    full: set = field(default_factory=set)
    lines: set = field(default_factory=set)
    overflow: bool = False
    chars: int = 0


class _Tree:
    def __init__(self, objs: list[Obj]) -> None:
        self.by_id = {o.object_id: o for o in objs}
        self.children: dict[str, list[Obj]] = {}
        for o in objs:
            if o.parent is not None and o.parent in self.by_id:
                self.children.setdefault(o.parent, []).append(o)
        self.entries = [o for o in objs if o.kind == "entry"]
        self._members: dict[str, list[Obj]] = {}

    def parent(self, o: Obj) -> Obj | None:
        up = self.by_id.get(o.parent) if o.parent is not None else None
        if up is None or _LEVEL[up.kind] <= _LEVEL[o.kind]:
            return None
        return up

    def members(self, o: Obj) -> list[Obj]:
        """The entries *o* stands for: itself, or every entry beneath it."""
        if o.kind == "entry":
            return [o]
        if o.object_id not in self._members:
            out = []
            for child in self.children.get(o.object_id, []):
                if _LEVEL[child.kind] < _LEVEL[o.kind]:
                    out.extend(self.members(child))
            self._members[o.object_id] = sorted(out, key=lambda e: e.order)
        return self._members[o.object_id]

    def heading(self, o: Obj) -> str | None:
        members = self.members(o)
        if not members:
            return None
        return (members[0].topic or "").strip() or _OTHER


def _full_line(o: Obj) -> str:
    text = (o.body or "").strip()
    subject = (o.subject or "").strip()
    if subject:
        text = f"{subject}: {text}" if text else subject
    if o.entry_kind == "errand":
        text = f"(errand, {o.errand_state or 'open'}) {text}"
    return f"- {text}"


def _label(o: Obj) -> str:
    return " ".join((o.line or o.subject or o.topic or "").split())


def _index_line(o: Obj, tree: _Tree) -> str:
    if o.kind == "entry":
        when = f" [{o.day}]" if o.day else ""
        text = f"- {o.object_id}{when} {_label(o)}"
        cues = [str(c) for c in o.cues if str(c).strip()]
        if cues:
            text += " — " + ", ".join(cues)
        return text
    members = tree.members(o)
    days = sorted(m.day for m in members if m.day)
    when = ""
    if days:
        when = (f" [{days[0]}]" if days[0] == days[-1]
                else f" [{days[0]} … {days[-1]}]")
    return f"- {o.object_id}{when} {_label(o)} ({len(members)} entries)"


def _heading_chars(heading: str) -> int:
    return len(f"## {heading}\n") + 1


class _Packing:
    """The shown items and their running size, headings included."""

    def __init__(self, tree: _Tree) -> None:
        self.tree = tree
        #: object_id -> "full" | "line"
        self.shown: dict[str, str] = {}
        self.per_heading: dict[str, int] = {}
        self.chars = 0
        self._costs: dict[tuple[str, str], int] = {}
        self._headings: dict[str, str | None] = {}

    def cost(self, o: Obj, state: str) -> int:
        key = (o.object_id, state)
        if key not in self._costs:
            line = (_full_line(o) if state == "full"
                    else _index_line(o, self.tree))
            self._costs[key] = len(line) + 1
        return self._costs[key]

    def heading(self, o: Obj) -> str | None:
        if o.object_id not in self._headings:
            self._headings[o.object_id] = self.tree.heading(o)
        return self._headings[o.object_id]

    def add(self, o: Obj, state: str) -> None:
        heading = self.heading(o)
        if self.per_heading.get(heading, 0) == 0:
            self.chars += _heading_chars(heading)
        self.per_heading[heading] = self.per_heading.get(heading, 0) + 1
        self.shown[o.object_id] = state
        self.chars += self.cost(o, state)

    def remove(self, o: Obj) -> None:
        heading = self.heading(o)
        state = self.shown.pop(o.object_id)
        self.chars -= self.cost(o, state)
        self.per_heading[heading] -= 1
        if self.per_heading[heading] == 0:
            self.chars -= _heading_chars(heading)

    def refinement(self, o: Obj) -> list[tuple[Obj, str]]:
        """What showing *o* one step finer puts in its place."""
        if o.kind == "entry":
            return [(o, "full")]
        return [(c, "line") for c in self.tree.children.get(o.object_id, [])
                if _LEVEL[c.kind] < _LEVEL[o.kind] and self.tree.members(c)]

    def delta(self, o: Obj) -> int:
        """The size change of refining *o*, headings included."""
        steps = self.refinement(o)
        change = (sum(self.cost(c, s) for c, s in steps)
                  - self.cost(o, self.shown[o.object_id]))
        own = self.heading(o)
        added: dict[str | None, int] = {}
        for c, _ in steps:
            h = self.heading(c)
            added[h] = added.get(h, 0) + 1
        for h in added:
            if self.per_heading.get(h, 0) - (h == own) == 0:
                change += _heading_chars(h)
        if self.per_heading[own] == 1 and own not in added:
            change -= _heading_chars(own)
        return change


def _visible(o: Obj, tree: _Tree, level: int) -> Obj:
    """The node an entry is folded into at *level* (0 entries, 1 groups,
    2 topics)."""
    shown = o
    up = tree.parent(o)
    while up is not None and _LEVEL[up.kind] <= level:
        shown = up
        up = tree.parent(up)
    return shown


def compose(objs: list[Obj], budget: int) -> Composition:
    """The view of *objs* within *budget* tokens: whole, else the index."""
    tree = _Tree(objs)
    budget_chars = max(budget, 0) * 4
    whole = _Packing(tree)
    for e in tree.entries:
        whole.add(e, "full")
    if whole.chars <= budget_chars:
        return Composition(WHOLE, full={e.object_id for e in tree.entries},
                           chars=whole.chars)

    base = None
    for level in (0, 1, 2):
        packing = _Packing(tree)
        seen: set[str] = set()
        for e in tree.entries:
            node = _visible(e, tree, level)
            if node.object_id not in seen:
                seen.add(node.object_id)
                packing.add(node, "line")
        base = packing
        if packing.chars <= budget_chars:
            break
    packing = base
    overflow = packing.chars > budget_chars

    # The cheapest refinement that still fits, again and again. A cost read
    # off the heap is re-checked before use: headings shown since it was
    # pushed can make it cheaper or dearer.
    heap = [(packing.delta(tree.by_id[i]), tree.by_id[i].order, i)
            for i, state in list(packing.shown.items()) if state == "line"]
    heapq.heapify(heap)
    while heap:
        cost, order, object_id = heapq.heappop(heap)
        if packing.shown.get(object_id) != "line":
            continue
        o = tree.by_id[object_id]
        now = packing.delta(o)
        if now != cost:
            heapq.heappush(heap, (now, order, object_id))
            continue
        if packing.chars + now > budget_chars:
            continue
        steps = packing.refinement(o)
        packing.remove(o)
        for c, s in steps:
            packing.add(c, s)
            if s == "line":
                heapq.heappush(heap, (packing.delta(c), c.order, c.object_id))
    return Composition(
        INDEX,
        full={i for i, s in packing.shown.items() if s == "full"},
        lines={i for i, s in packing.shown.items() if s == "line"},
        overflow=overflow, chars=packing.chars)


def render_text(objs: list[Obj], comp: Composition) -> str:
    """The ``<memory>`` block, or ``""`` for an empty ledger."""
    tree = _Tree(objs)
    if not tree.entries:
        return ""
    groups: dict[str, list[tuple[tuple, str]]] = {}
    order: dict[str, tuple] = {}
    for object_id in (*comp.full, *comp.lines):
        o = tree.by_id[object_id]
        heading = tree.heading(o)
        if heading is None:
            continue
        line = (_full_line(o) if object_id in comp.full
                else _index_line(o, tree))
        groups.setdefault(heading, []).append((o.order, line))
        first = tree.members(o)[0].order
        if heading not in order or first < order[heading]:
            order[heading] = first
    body = []
    for heading in sorted(groups, key=lambda h: order[h]):
        body.append(f"## {heading}")
        body.extend(line for _, line in sorted(groups[heading]))
        body.append("")
    head = HEAD_WHOLE if comp.mode == WHOLE else HEAD_INDEX
    if comp.overflow:
        head += "\n" + HEAD_OVERFLOW
    return "<memory>\n" + head + "\n\n" + "\n".join(body).strip() \
        + "\n</memory>"


# --- the served view -----------------------------------------------------------

@dataclass(frozen=True)
class View:
    ledger: str
    commit_no: int
    budget: int
    text: str
    mode: str
    overflow: bool
    block_hash: str
    renderer_version: str = RENDERER_VERSION

    @property
    def tokens(self) -> int:
        return tokens(self.text)


def block_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_as_of(conn: sqlite3.Connection, ledger: str, commit_no: int,
                 budget: int) -> View:
    """*ledger* as it stood after *commit_no*, within *budget* tokens."""
    objs = objects_as_of(conn, ledger, commit_no)
    comp = compose(objs, budget)
    text = render_text(objs, comp)
    return View(ledger=ledger, commit_no=commit_no, budget=budget,
                text=text, mode=comp.mode, overflow=comp.overflow,
                block_hash=block_hash(text))


def render(conn: sqlite3.Connection, ledger: str, budget: int) -> View:
    """*ledger* as it stands in the caller's read transaction."""
    return render_as_of(conn, ledger, pending.current_commit_no(conn), budget)


@dataclass(frozen=True)
class Served:
    """A session's view. ``reset`` is true when a resumed session's frozen
    view could not be reproduced: nothing was rendered or recorded, and the
    caller must drop the session's resume pointer before serving it afresh."""

    view: View | None
    reset: bool = False


def serve(store: MemoryStore, session_key: str, *, clearance: str,
          budget: int, resume_sid: str | None = None,
          as_of: int | None = None) -> Served:
    """The frozen view of session *session_key*.

    A view belongs to one SDK session. For a new session (``resume_sid`` is
    ``None``) the view is rendered and recorded, unbound; :func:`bind` binds
    it once the SDK names the session. A new session's view is rendered as
    of commit *as_of* when given (the snapshot its first turn's correction
    note was read in, so view and note describe one state of the store),
    else as of the current commit.

    A session resumes on its view only when the record is bound to exactly
    *resume_sid*, was made for this ledger, clearance, budget and renderer,
    and its view rendered again as of its base matches the recorded hash.
    Otherwise nothing is rendered or recorded and ``Served(None,
    reset=True)`` comes back: the caller drops the resume pointer first, so
    no later attempt can resume that session, and only then serves a new
    session.

    ``Served(None)`` when the session reads no ledger. Raises when the store
    is unavailable or fails."""
    ledger = ledger_for(clearance)
    if ledger is None or budget <= 0:
        return Served(None)
    if resume_sid is not None:
        with store.transaction(busy_timeout=BUSY_TIMEOUT_S) as conn:
            row = sessions.get(conn, session_key)
            if (row is not None and row.sdk_session_id == resume_sid
                    and row.ledger == ledger
                    and row.clearance == clearance and row.budget == budget
                    and row.renderer_version == RENDERER_VERSION):
                view = render_as_of(conn, ledger, row.base_commit_no, budget)
                if view.block_hash == row.block_hash:
                    return Served(view)
        logger.warning(
            "memory view of session %s could not be reproduced (%s); the "
            "session starts afresh", session_key,
            "no record" if row is None else
            "recorded for another session"
            if row.sdk_session_id != resume_sid else "changed")
        return Served(None, reset=True)
    with store.transaction(busy_timeout=BUSY_TIMEOUT_S) as conn:
        if as_of is None:
            view = render(conn, ledger, budget)
        else:
            view = render_as_of(conn, ledger, as_of, budget)
    with store.transaction(write=True, busy_timeout=BUSY_TIMEOUT_S) as conn:
        sessions.put(conn, sessions.SessionView(
            session_key=session_key, ledger=ledger,
            base_commit_no=view.commit_no, clearance=clearance,
            budget=budget, renderer_version=view.renderer_version,
            block_hash=view.block_hash))
    return Served(view)


def settle(store: MemoryStore, session_key: str, sdk_session_id: str, *,
           clearance: str, budget: int, served: View | None = None) -> bool:
    """End of a turn of session *session_key*, run as SDK session
    *sdk_session_id*: bind the view this turn served, if any, then say
    whether the session may be used again — warm or resumed. It may only
    when a record bound to exactly that SDK session exists, made for this
    session's ledger, clearance, budget and renderer (its block hash is
    verified when a session connects). A view that could not be bound
    (the write lock was busy) leaves the record unbound, so the answer is
    no. Raises only when the store cannot be read: the caller then cannot
    tell, and keeps the session on its own frozen view."""
    ledger = ledger_for(clearance)
    if ledger is None or budget <= 0:
        return True
    if served is not None:
        try:
            bind(store, session_key, served, sdk_session_id)
        except Exception:  # noqa: BLE001 — unbound; the read below decides
            logger.warning("memory view of session %s could not be bound",
                           session_key, exc_info=True)
    with store.transaction(busy_timeout=BUSY_TIMEOUT_S) as conn:
        row = sessions.get(conn, session_key)
    return (row is not None and row.sdk_session_id == sdk_session_id
            and row.ledger == ledger and row.clearance == clearance
            and row.budget == budget
            and row.renderer_version == RENDERER_VERSION)


def bind(store: MemoryStore, session_key: str, view: View,
         sdk_session_id: str) -> bool:
    """Bind the record of *view*, served to session *session_key*, to the
    SDK session it was served to. Returns whether it is bound; a record
    replaced since, or never written, is not, so that session's next
    reconnect starts afresh. Raises when the store is unavailable or
    fails."""
    with store.transaction(write=True, busy_timeout=BUSY_TIMEOUT_S) as conn:
        return sessions.bind(conn, session_key,
                             base_commit_no=view.commit_no,
                             budget=view.budget, block_hash=view.block_hash,
                             sdk_session_id=sdk_session_id)


# --- opening entries -----------------------------------------------------------

#: Ids one ``open_memory`` call opens.
MAX_OPEN = 20


def _history(conn: sqlite3.Connection, ledger: str,
             object_id: str) -> list[dict]:
    """Every earlier revision of an object, oldest first, dated by the
    commit that replaced it."""
    revs = conn.execute(
        "SELECT revision, committed_at, subject, body, op FROM revisions "
        "WHERE ledger = ? AND object_id = ? ORDER BY revision",
        (ledger, object_id)).fetchall()
    out = []
    for older, newer in zip(revs, revs[1:]):
        text = ": ".join(p for p in (older[2], older[3]) if p)
        out.append({"revision": older[0], "until": (newer[1] or "")[:10],
                    "text": text, "then": newer[4]})
    return out


def open_objects(store: MemoryStore, ids: list[str], *, clearance: str,
                 session_key: str | None = None,
                 sdk_session_id: str | None = None,
                 with_history: bool = False) -> list[dict] | None:
    """Open *ids* in the ledger a session at *clearance* reads, as each
    stands at its latest committed revision: an entry in full, a group or a
    topic as the lines of what it holds, a retired object as retired. One
    committed after the session's view (its record by *session_key*, used
    only when bound to the SDK session *sdk_session_id* the call runs in) is
    marked so. An id that is no object of that ledger answers ``unknown``,
    whether it exists in another ledger or not. ``None`` when the clearance
    reads no ledger. Raises when the store is unavailable or fails."""
    ledger = ledger_for(clearance)
    if ledger is None:
        return None
    with store.transaction(busy_timeout=BUSY_TIMEOUT_S) as conn:
        base = None
        row = sessions.get(conn, session_key) if session_key else None
        if (row is not None and sdk_session_id is not None
                and row.sdk_session_id == sdk_session_id
                and row.ledger == ledger and row.clearance == clearance):
            base = row.base_commit_no
        tree = _Tree(objects_as_of(conn, ledger,
                                   pending.current_commit_no(conn)))
        latest = dict(conn.execute(
            "SELECT object_id, max(commit_no) FROM revisions "
            "WHERE ledger = ? GROUP BY object_id", (ledger,)).fetchall())
        out = []
        for object_id in ids:
            obj = tree.by_id.get(object_id)
            if obj is not None:
                item: dict = {"id": object_id, "kind": obj.kind}
                if obj.kind == "entry":
                    item["text"] = _full_line(obj)[2:]
                    if obj.day:
                        item["date"] = obj.day
                else:
                    item["label"] = _label(obj)
                    item["members"] = [
                        _index_line(c, tree)[2:]
                        for c in tree.children.get(object_id, [])
                        if _LEVEL[c.kind] < _LEVEL[obj.kind]
                        and tree.members(c)]
            elif object_id in latest:
                item = {"id": object_id, "status": "retired"}
            else:
                out.append({"id": object_id, "status": "unknown"})
                continue
            if base is not None and latest.get(object_id, 0) > base:
                item["changed_since_your_memory_above"] = True
            if with_history:
                item["history"] = _history(conn, ledger, object_id)
            out.append(item)
    return out


# --- reachability --------------------------------------------------------------

def unreachable(conn: sqlite3.Connection, ledger: str) -> list[str]:
    """Current objects of *ledger* that do not hang from current parents of
    the same ledger, each a level above, up to the top: the objects no view
    of the ledger could reach."""
    rows = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT object_id, kind, parent, status FROM objects "
        "WHERE ledger = ?", (ledger,))}
    bad = []
    for object_id, (kind, parent, status) in rows.items():
        if status != "current":
            continue
        level = _LEVEL[kind]
        while parent is not None:
            up = rows.get(parent)
            if up is None or up[2] != "current" or _LEVEL[up[0]] <= level:
                bad.append(object_id)
                break
            level, parent = _LEVEL[up[0]], up[1]
    return sorted(bad)


def reachable(conn: sqlite3.Connection, ledger: str) -> bool:
    """Whether every current object of *ledger* is reachable."""
    return not unreachable(conn, ledger)
