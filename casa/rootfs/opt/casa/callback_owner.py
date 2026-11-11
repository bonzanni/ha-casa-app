"""Which operation a plugin's callback answers.

A plugin's callback answers a sign-in that a capability delivered as an
operator link. The delivery records the agent that executes the operation and
where the link was delivered (:func:`note_link`); the callback ingress asks
:func:`operation_for` before composing the callback's turn. The latest
unexpired link of a plugin decides: when two agents' sign-ins of one plugin
race, the later link wins.

Memory-only and bounded by each link's time to live: after a restart, or once
the link has expired, a callback goes where it went before any link was noted.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from channels.tg_address import TgAddress

#: one entry per plugin, so the table is bounded by the plugins installed;
#: the cap guards against a plugin name nobody will ever look up again
MAX_PLUGINS = 256

_lock = threading.Lock()
_links: "dict[str, tuple[str, TgAddress, float]]" = {}
_now: Callable[[], float] = time.monotonic


def note_link(plugin: str, executor: str, delivery: "TgAddress", ttl_s: int) -> None:
    """A capability of *plugin* run by *executor* delivered an operator link
    at *delivery*; it answers a callback for *ttl_s* seconds."""
    if not plugin or not executor or delivery is None or ttl_s <= 0:
        return
    with _lock:
        now = _now()
        for name in [p for p, (_, _, until) in _links.items() if until <= now]:
            del _links[name]
        _links.pop(plugin, None)
        while len(_links) >= MAX_PLUGINS:
            del _links[next(iter(_links))]
        _links[plugin] = (executor, delivery, now + float(ttl_s))


def operation_for(plugin: str) -> "tuple[str, TgAddress] | None":
    """The executor and delivery of *plugin*'s latest unexpired link, else ``None``."""
    with _lock:
        hit = _links.get(plugin)
        if hit is None:
            return None
        executor, delivery, until = hit
        if until <= _now():
            del _links[plugin]
            return None
        return executor, delivery


def reset(now: Callable[[], float] = time.monotonic) -> None:
    """Forget every link, with *now* as the clock (tests)."""
    global _now
    with _lock:
        _links.clear()
        _now = now
