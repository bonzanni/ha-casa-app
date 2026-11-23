"""The size meter: what memory holds and what it still owes, with no model.

:func:`measure` reads the store in one read transaction; :func:`persist`
writes the result to ``meta`` under ``meter`` after every writer commit and
failure, for the status surfaces to read. Per maintained ledger it counts the
current entries and their tokens, the objects left unreachable (which must be
0), each role's view of it (whole or index, its size, and whether even the
coarsest index level overflows the role's budget) for each role that reads
it, the twenty largest entries
(ids and sizes only) and the week's growth,
and from the one pending view the pending events (count and the oldest), the
pending corrections and the failed jobs. Across the store it counts the
events left provisional, the spool, the transcript runs an earlier process
left unconfirmed (unresolved, :mod:`memory.transcript_runs`), each open
session's ``base_commit_no`` and whether a wipe or restore intent is
pending.

Thresholds only start jobs; the meter deletes nothing. Its values are
counts, ids and times, never memory text.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from memory import capture, pending, transcript_runs, view
from memory.ledgers import maintained
from memory.store import MemoryStore

META_KEY = "meter"
TOP = 20

#: The served-view budget (``memory.token_budget``) of every role that is
#: served a view, set by Casa at boot; the meter reports each role's mode
#: and whether its view overflows, and the nightly pass regroups a ledger
#: that overflows any of the roles that read it.
_role_budgets: dict[str, int] = {}
#: The read clearances of each role (``sensitivity.clearances_served``),
#: set with the budgets.
_role_clearances: dict[str, tuple[str, ...]] = {}


def configure(role_budgets: dict[str, int],
              role_clearances: dict | None = None) -> None:
    """Record the served-view budget of every role (those above 0 count)
    and, when given, the clearances each role reads at."""
    _role_budgets.clear()
    _role_budgets.update({role: int(b) for role, b in role_budgets.items()
                          if isinstance(b, int) and b > 0})
    _role_clearances.clear()
    _role_clearances.update({role: tuple(c) for role, c in
                             (role_clearances or {}).items()})


def role_budgets() -> dict[str, int]:
    return dict(_role_budgets)


def reader_budgets(ledger: str,
                   budgets: dict[str, int] | None = None) -> dict[str, int]:
    """The budgets (from *budgets*, default the configured ones) of the
    roles that read *ledger*: a role reads the ledger its clearance is
    served (:func:`memory.view.ledger_for`), so a ledger is never held to
    the budget of a role that never sees it (#1659). A role whose
    clearances were not given counts as a reader of every ledger."""
    budgets = _role_budgets if budgets is None else budgets
    out = {}
    for role, budget in budgets.items():
        clearances = _role_clearances.get(role)
        if clearances is None or any(
                view.ledger_for(c) == ledger for c in clearances):
            out[role] = budget
    return out


def views(conn, ledger: str) -> dict[str, dict]:
    """Per role reading *ledger*, the mode its view is in now, its size,
    and whether even the coarsest level overflows its budget."""
    objs = view.objects_as_of(conn, ledger, pending.current_commit_no(conn))
    out = {}
    for role, budget in sorted(reader_budgets(ledger).items()):
        comp = view.compose(objs, budget)
        out[role] = {"budget": budget, "mode": comp.mode,
                     "tokens": comp.chars // 4, "overflow": comp.overflow}
    return out


def _tokens(text: str | None) -> int:
    return len(text or "") // 4


def _file_size(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def measure(store: MemoryStore) -> dict:
    """The meter, measured now. Blocking."""
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).isoformat(timespec="microseconds")
    ledgers: dict[str, dict] = {}
    with store.transaction() as conn:
        for ledger in maintained():
            entries = [(r[0], _tokens(r[1])) for r in conn.execute(
                "SELECT object_id, body FROM objects WHERE ledger = ? "
                "AND kind = 'entry' AND status = 'current'", (ledger,))]
            times = sorted(t for _, t in pending.pending_arrivals(
                conn, ledger))
            added = conn.execute(
                "SELECT count(*), coalesce(sum(length(body)), 0) / 4 "
                "FROM revisions WHERE ledger = ? AND op = 'add' "
                "AND kind = 'entry' AND committed_at >= ?",
                (ledger, week_ago)).fetchone()
            roles = views(conn, ledger)
            ledgers[ledger] = {
                "entries": len(entries),
                "tokens": sum(t for _, t in entries),
                "unreachable": len(view.unreachable(conn, ledger)),
                "roles": roles,
                "overflow": any(r["overflow"] for r in roles.values()),
                "largest": [{"object_id": o, "tokens": t} for o, t in sorted(
                    entries, key=lambda e: -e[1])[:TOP]],
                "week_added": {"entries": added[0], "tokens": added[1]},
                "pending": {"events": len(times),
                            "oldest": times[0] if times else None},
                "pending_corrections": len(
                    pending.pending_corrections(conn, ledger)),
                "failed_jobs": conn.execute(
                    "SELECT count(*) FROM jobs WHERE ledger = ? "
                    "AND state = 'failed'", (ledger,)).fetchone()[0],
            }
        provisional = conn.execute(
            "SELECT count(*) FROM events WHERE tier_status = 'provisional'"
        ).fetchone()[0]
        sessions = {r[0]: r[1] for r in conn.execute(
            "SELECT session_key, base_commit_no FROM session_views")}
        commit_no = pending.current_commit_no(conn)
        unresolved = transcript_runs.unresolved_count(conn)
    intent = os.path.join(store.directory, "maintenance.json")
    return {
        "measured_at": now.isoformat(timespec="seconds"),
        "commit_no": commit_no,
        "ledgers": ledgers,
        "provisional_events": provisional,
        "spool_bytes": _file_size(capture.spool_path(store.directory)),
        "spool_snapshot_bytes": _file_size(
            capture.snapshot_path(store.directory)),
        "unresolved_runs": unresolved,
        "sessions": sessions,
        "maintenance_intent": os.path.lexists(intent),
    }


def persist(store: MemoryStore) -> dict:
    """Measure, and store the result in ``meta``. Blocking."""
    value = measure(store)
    with store.transaction(write=True) as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) "
            "DO UPDATE SET value = excluded.value",
            (META_KEY, json.dumps(value, sort_keys=True)))
    return value


def read(store: MemoryStore) -> dict | None:
    """The last persisted meter, or ``None``."""
    with store.transaction() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?",
                           (META_KEY,)).fetchone()
    return json.loads(row[0]) if row else None

