"""The nightly memory pass: it only queues writer jobs.

Once a night (03:30 in the operator's zone, from Casa's scheduler) it queues,
per maintained ledger, a ``condense`` job when an entry is over
:data:`memory.writer.CONDENSE_OVER_TOKENS`, and a ``regroup`` job when the
ledger's current entries are over :data:`REGROUP_OVER_TOKENS`; and it sends
every ``failed`` job back to ``queued`` once, which the worker then runs from
a freshly assembled input like any other run. It writes no ledger row
itself: every change to a ledger is made by the worker's commit, with its
checks.
"""
from __future__ import annotations

import asyncio
import logging

from memory import pending, writer
from memory.store import MemoryStore

logger = logging.getLogger(__name__)

#: Ledger tokens past which the entries are regrouped (the largest served
#: memory budget, Ellen's).
REGROUP_OVER_TOKENS = 50_000

HOUR = 3
MINUTE = 30


def enqueue_nightly(store: MemoryStore, *,
                    regroup_over_tokens: int = REGROUP_OVER_TOKENS) -> dict:
    """Queue the night's jobs. Returns what was queued. Blocking."""
    out: dict[str, object] = {"condense": [], "regroup": [], "retried": 0}
    with store.transaction(write=True) as conn:
        for ledger in pending.MAINTAINED_LEDGERS:
            bodies = [r[0] for r in conn.execute(
                "SELECT body FROM objects WHERE ledger = ? AND kind = 'entry' "
                "AND status = 'current'", (ledger,))]
            sizes = [writer.tokens(b) for b in bodies]
            if any(s > writer.CONDENSE_OVER_TOKENS for s in sizes):
                if writer._enqueue(conn, ledger, writer.CONDENSE):
                    out["condense"].append(ledger)
            if sum(sizes) > regroup_over_tokens:
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
