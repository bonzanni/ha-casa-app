"""Shared dispatch-target selection for a plugin's registry ``targets``.

Leaf module (stdlib only). Casa dispatches several kinds of casa-authored
turns to a plugin's assigned agent(s) — a post-consent setup tool run
(:mod:`plugin_setup_episodes`), an authorization-callback collection nudge
(:mod:`callback_episodes`), and (a later task) an event-delivery nudge — and
all three pick the SAME execution target off the SAME ``entry["targets"]``
shape (a list of ``"resident:<role>"`` / ``"specialist:<name>"`` strings)
in the SAME order. This module is that one decision, extracted so the three
callers can never drift apart the way ``callback_episodes._compose`` and
``plugin_setup_episodes._compose`` had — by construction, not by convention
— drifted in wording only, never in target order.
"""
from __future__ import annotations


def _split_targets(entry: dict) -> "tuple[list[str], list[str]]":
    """Sorted (residents, specialists) role lists off ``entry['targets']`` —
    the ONE parse both :func:`compose` and :func:`execution_role` read, so
    the courier choice and the executing-session choice cannot drift."""
    targets = entry.get("targets") or []
    residents = sorted(t.split(":", 1)[1] for t in targets
                       if t.startswith("resident:"))
    specialists = sorted(t.split(":", 1)[1] for t in targets
                         if t.startswith("specialist:"))
    return residents, specialists


def compose(entry: dict, base: str) -> "tuple[str | None, str]":
    """Deterministic execution-target selection for the instruction *base*.
    Returns ``(role, instruction)`` or ``(None, reason)``.

    Target order: ``resident:assistant`` when targeted; else the
    lexicographically first resident; else the first specialist via
    assistant delegation (the specialist has no channel — the instruction
    names the EXACT specialist and forbids substitution); else no target at
    all.
    """
    residents, specialists = _split_targets(entry)
    if "assistant" in residents:
        return "assistant", base
    if residents:
        return residents[0], base
    if specialists:
        sp = specialists[0]
        # #1050: said to be Casa's, and sync by name. Unframed, the
        # courier took the text for something the operator pasted; left to
        # choose, it once picked mode='interactive', which the specialist's
        # own open engagement refuses as busy.
        return "assistant", (
            "[casa system notice — not a message from the operator] "
            f"Delegate to the specialist '{sp}' with mode='sync' and the "
            f"instruction: {base} Do not substitute another agent.")
    return None, "no resident or specialist target"


def execution_role(entry: dict) -> "str | None":
    """The role whose SESSION actually runs the plugin tool for this entry
    (#423 r2): identical to :func:`compose`'s dispatch role on the resident
    branches, but the named SPECIALIST on the delegation branch — readiness
    gates must ask about the executing session, not the assistant courier."""
    return execution_target(entry)[1]


def execution_target(entry: dict) -> "tuple[str | None, str | None]":
    """``(tier, role)`` of the executing session (#423 r3, Terra r2-1) —
    readiness semantics differ by tier: a RESIDENT executes in a long-lived
    Agent whose published binding snapshot can predate the plugin's secrets
    (gate on :func:`execution_ready`), while a SPECIALIST builds its options
    fresh per delegation against the current environment (nothing cached to
    go stale — no binding gate applies). ``(None, None)`` with no target."""
    residents, specialists = _split_targets(entry)
    if "assistant" in residents:
        return "resident", "assistant"
    if residents:
        return "resident", residents[0]
    if specialists:
        return "specialist", specialists[0]
    return None, None


def execution_ready(agent, plugin: str, artifact_id: str) -> bool:
    """Whether *agent*'s NEXT session build will carry ``plugin@artifact_id``
    (#423 r2, Sol 1/Terra 1). An agent with no published binding snapshot is
    ready — it is unresolved and resolves the CURRENT registry snapshot and
    environment on its next turn (verify's FR3 readiness rule); a published
    snapshot must already contain the exact artifact, because a snapshot
    built while the plugin was env-withheld keeps excluding it until an
    agent reload. A missing agent is not ready."""
    if agent is None:
        return False
    snap = getattr(agent, "plugin_binding_snapshot", None)
    if snap is None:
        return True
    binding = getattr(snap, "binding", None) or {}
    return binding.get(plugin) == artifact_id
