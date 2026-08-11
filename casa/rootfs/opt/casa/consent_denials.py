"""In-process registry of the operator's most recent consent DENIALS.

Consulted by exactly ONE caller class: the on-demand consent re-prompt path
(``consent_reprompt`` → each reconciler's ``reprompt_pending``), so an
agent-invoked re-issue can never nag past an operator's explicit Deny.
Operator-driven prompt paths — plugin lifecycle mutations, reloads, boot —
NEVER consult this registry: a new mutation legitimately re-asks.

Ordering is commit-ordered by construction: entries are written ONLY from the
consent keyboards' synchronous commit steps (``_on_commit_sync``, the same
yield-free step that persists the ack), Approve → :func:`clear`, Deny →
:func:`record`. Expiry writes NOTHING — an expired keyboard is exactly the
state the re-prompt exists to recover. Process-lifetime, in-memory: a restart
empties it, which only widens the on-demand path back to today's behavior.

Keys are kind-prefixed (``"trigger:<identity>"``, ``"callback:<identity>"``,
``"event:<identity>"``) so the three kinds' identity hash spaces can never
collide.
"""
from __future__ import annotations

_denied: set[str] = set()


def key(kind: str, identity: str) -> str:
    """The registry key for one consent identity of one kind."""
    return f"{kind}:{identity}"


def record(k: str) -> None:
    """The operator's latest decision for *k* is a Deny."""
    _denied.add(k)


def clear(k: str) -> None:
    """The operator's latest decision for *k* is an Approve (idempotent)."""
    _denied.discard(k)


def denied(k: str) -> bool:
    """True when the operator's most recent recorded decision is a Deny."""
    return k in _denied
