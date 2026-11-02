# casa/rootfs/opt/casa/session_gate.py
"""#573/#579: the module-level, path-independent authority over a session key.

v0.66.0 made the SDK client pool the authority over who may run a turn on
session key K. That is a property of the KEY, which outlives any pool instance
and is reachable by paths that never enter a pool at all — a scheduled turn, a
webhook one-shot, a ``PoolUnavailable`` fallback, or an ordinary turn on the far
side of a reload that replaced the pool underneath it (#579: a reload builds a
fresh Agent with its OWN pool while a turn still runs on the old one, so two
clients could resume one session id; the pool entry lock serializes within one
pool, and across a reload there are two).

:func:`session_write_gate` answers it at module scope, where a reload cannot
replace it and no turn path can route around it: per-``channel_key``
serialization, held across the SDK attempt AND the registry publish.

Lock order, global and mandatory:

    session_write_gate -> pool entry lock

Never the reverse: taking them in two orders is a permanent resident
deadlock.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


# #573: one writer per session key.
#
# Refcounted rather than a plain dict-of-locks: the key space is per-boot
# unbounded, and an entry that nothing holds is deleted rather than retained.
# The bookkeeping below contains no awaits, so a cancelled waiter cannot
# interleave inside it and strand an entry.
_SESSION_GATES: "dict[str, list]" = {}


@asynccontextmanager
async def session_write_gate(channel_key: str):
    entry = _SESSION_GATES.get(channel_key)
    if entry is None:
        entry = [asyncio.Lock(), 0]
        _SESSION_GATES[channel_key] = entry
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] <= 0 and _SESSION_GATES.get(channel_key) is entry:
            del _SESSION_GATES[channel_key]
