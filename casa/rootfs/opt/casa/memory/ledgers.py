"""The maintained ledgers: one ledger per clearance a reader is served.

Text served at a tier is generated only from inputs readable at that tier
(M6). So Casa keeps one isolated ledger per maintained clearance rather than
one ledger filtered on the way out: a filter over entries cannot recover a
restriction the entry's writer never saw. ``private`` is always maintained.
A lower clearance is maintained while some role that reads at it is served
a view (``memory.token_budget`` above 0): in the shipped fleet, ``friends``
for the butler, whose voice surface reads at ``friends``.

A session reads the ledger of the highest maintained clearance at or below
its own (:func:`memory.view.ledger_for`); with none, it is served no view.
The writer files each maintained ledger on its own, ``private`` first
(:mod:`memory.writer`); a lower ledger is given only events classified at or
below it (:mod:`memory.pending`).

Casa declares its readers once at boot (:func:`configure_roles`); until then
only ``private`` is maintained. This module imports nothing.
"""
from __future__ import annotations

from collections.abc import Iterable

PRIVATE = "private"
#: Lower clearances that get a ledger of their own while a reader at that
#: clearance is served a view, highest first. ``family`` and ``public`` have
#: no reader in Casa: a session at ``family`` reads the ``friends`` ledger,
#: and one at ``public`` reads none.
ON_DEMAND: tuple[str, ...] = ("friends",)

_served: frozenset[str] = frozenset()


def configure(readers: Iterable[tuple[str, int]]) -> tuple[str, ...]:
    """Declare every reader as ``(clearance, budget)``: a clearance a role
    reads at, and that role's served-view budget. Returns the maintained
    ledgers that follow."""
    global _served
    _served = frozenset(clearance for clearance, budget in readers
                        if isinstance(budget, int) and budget > 0)
    return maintained()


def configure_roles(roles: Iterable, clearances_of) -> tuple[str, ...]:
    """Declare the fleet: every role config in *roles* reads, with its
    ``memory.token_budget``, at each clearance ``clearances_of(channels)``
    names for its declared ``channels``. Returns the maintained ledgers."""
    return configure(
        (clearance, getattr(getattr(cfg, "memory", None), "token_budget", 0))
        for cfg in roles
        for clearance in clearances_of(getattr(cfg, "channels", None) or ()))


def maintained() -> tuple[str, ...]:
    """The maintained ledgers, ``private`` first, then each lower one in
    the order the writer files them."""
    return (PRIVATE, *(c for c in ON_DEMAND if c in _served))
