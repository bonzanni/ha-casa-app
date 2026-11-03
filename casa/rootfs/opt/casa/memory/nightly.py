"""The nightly memory pass: it only queues writer jobs.

Once a night (03:30 in the operator's zone, from Casa's scheduler) it queues,
per maintained ledger, a ``condense`` job when an entry is over
:data:`memory.writer.CONDENSE_OVER_TOKENS`, and a ``regroup`` job when a
role's view of the ledger overflows its budget even at the coarsest index
level (:mod:`memory.view`), so groups and topics are added only when the
level below no longer fits; and it sends
every ``failed`` job back to ``queued`` once, which the worker then runs from
a freshly assembled input like any other run. It writes no ledger row
itself: every change to a ledger is made by the worker's commit, with its
checks.
"""
from __future__ import annotations

import asyncio
import logging

from memory import meter, pending, view, writer
from memory.ledgers import maintained
from memory.store import MemoryStore

logger = logging.getLogger(__name__)

HOUR = 3
MINUTE = 30


def enqueue_nightly(store: MemoryStore, *,
                    role_budgets: dict[str, int] | None = None) -> dict:
    """Queue the night's jobs. *role_budgets* defaults to the meter's
    configured served-view budgets. Returns what was queued. Blocking."""
    if role_budgets is None:
        role_budgets = meter.role_budgets()
    out: dict[str, object] = {"condense": [], "regroup": [], "retried": 0}
    with store.transaction(write=True) as conn:
        for ledger in maintained():
            bodies = [r[0] for r in conn.execute(
                "SELECT body FROM objects WHERE ledger = ? AND kind = 'entry' "
                "AND status = 'current'", (ledger,))]
            sizes = [writer.tokens(b) for b in bodies]
            if any(s > writer.CONDENSE_OVER_TOKENS for s in sizes):
                if writer._enqueue(conn, ledger, writer.CONDENSE):
                    out["condense"].append(ledger)
            if role_budgets:
                objs = view.objects_as_of(
                    conn, ledger, pending.current_commit_no(conn))
                if any(view.compose(objs, b).overflow
                       for b in role_budgets.values()):
                    if writer._enqueue(conn, ledger, writer.REGROUP):
                        out["regroup"].append(ledger)
        out["retried"] = conn.execute(
            "UPDATE jobs SET state = 'queued', boot_id = NULL "
            "WHERE state = 'failed'").rowcount
    return out


async def run() -> None:
    """The scheduler's nightly job: queue, then wake the writer."""
    w = writer.active()
    if w is None or not w.store.available:
        return
    try:
        queued = await asyncio.to_thread(enqueue_nightly, w.store)
    except Exception:  # noqa: BLE001 — tomorrow night tries again
        logger.warning("memory nightly pass failed", exc_info=True)
        return
    logger.info("memory nightly pass: %s", queued)
    w.wake()
