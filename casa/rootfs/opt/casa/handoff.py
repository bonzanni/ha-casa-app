"""The general hand-off: who asked, through which agents, in whose words.

Design (browser executor v9.1 §5.4–§5.5): any of the operator's agents can hand
work to any specialist through Casa. Each hand-off carries the operator's own
words (for run admission) and the chain of requesters (so a result returns to
whoever asked). The bounds are mechanical and agent-agnostic: a chain never
hands work back to an agent already in it, and holds at most
``MAX_HANDOFFS`` hand-offs after the resident it started from.

Everything here is a pure function of a turn's origin snapshot. The origin's
keys it reads are server-set: ``_operator_turn`` and ``synthetic`` are reserved
markers no external context can carry (``provenance.sanitize_external_context``),
``user_text`` is the turn's own text recorded by ``Agent._process``, and
``_handoff_chain`` is stamped only by ``tools._run_delegated_agent``."""
from __future__ import annotations

from typing import Any

# Ellen -> A -> B -> C: three hand-offs after the resident, the design's
# "[my call: 3 hand-offs after Ellen]".
MAX_HANDOFFS = 3

# The synthetic marker a tapped model-written suggested reply's turn will carry
# (design §5.5: "A tapped suggested reply counts as his own words"); that
# feature's dispatch stamps ``_operator_words`` with the tapped text.
SUGGESTED_REPLY = "suggested_reply"


def handoff_chain(origin: dict | None) -> tuple[str, ...]:
    """The requesters of the turn *origin* belongs to, root first, ending with
    the agent executing it.

    A delegated run's origin carries the stamped chain. Any other origin is a
    turn of its own agent: a resident's direct turn is ``(role,)``; a desk
    turn (``role`` the resident, ``execution_role`` the specialist the
    operator replied to) is ``(role, execution_role)``."""
    origin = origin or {}
    stamped = origin.get("_handoff_chain")
    if isinstance(stamped, (tuple, list)) and stamped and all(
            isinstance(r, str) and r for r in stamped):
        return tuple(stamped)
    role = str(origin.get("role") or "")
    execution = str(origin.get("execution_role") or role)
    if not role:
        return (execution,) if execution else ()
    return (role,) if execution == role else (role, execution)


def child_chain(parent: dict | None, role: str) -> tuple[str, ...]:
    """The chain a run of *role* launched from *parent* belongs to — *role*
    appended unless the parent already ends with it (a desk turn's own run)."""
    chain = handoff_chain(parent)
    if chain and chain[-1] == role:
        return chain
    return (*chain, role)


def requester_of(chain: tuple[str, ...]) -> str:
    """Who asked the agent at the end of *chain*: the element before it, or
    the root itself for a one-element chain."""
    if len(chain) >= 2:
        return chain[-2]
    return chain[0] if chain else ""


def operator_words(origin: dict | None) -> str | None:
    """The operator's own current words for this turn, or ``None``.

    Read only from the reserved ``_operator_words`` key, which exactly two
    places stamp: the Telegram channel for a message (or a file's caption) the
    configured operator typed, and the specialist desk for a reply he typed — a
    tapped suggested reply's dispatch is the third, when that feature lands
    (``SUGGESTED_REPLY``). Absent on every other turn: a button or approval
    continuation, a stored-call tap, a setup turn, a narration, a continuation,
    a scheduled, webhook, voice or engagement turn. The key is reserved
    (``provenance.sanitize_external_context``), and it travels down a hand-off
    chain because a delegated run's origin copies its parent's. ``_operator_turn``
    and the Telegram channel are required as well, belt and braces."""
    origin = origin or {}
    if origin.get("_operator_turn") is not True or origin.get("channel") != "telegram":
        return None
    text = origin.get("_operator_words")
    if not isinstance(text, str) or not text.strip():
        return None
    return text


def handoff_refusal(chain: tuple[str, ...], target: str) -> dict[str, Any] | None:
    """The mechanical bounds of one more hand-off from the agent ending
    *chain* to *target*: never to an agent already in the chain, and never
    past ``MAX_HANDOFFS`` hand-offs after the root. ``None`` when allowed."""
    if target in chain:
        return {
            "status": "error", "kind": "handoff_cycle",
            "message": (f"{target!r} is already working on this request "
                        f"({' > '.join(chain)}); a hand-off never goes back "
                        "to an agent in the chain."),
        }
    if len(chain) > MAX_HANDOFFS:
        return {
            "status": "error", "kind": "handoff_chain_too_long",
            "message": (f"This request has already been handed on "
                        f"{len(chain) - 1} times ({' > '.join(chain)}); at "
                        f"most {MAX_HANDOFFS} hand-offs are allowed. Answer "
                        "with what you have."),
        }
    return None
