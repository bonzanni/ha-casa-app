# casa/rootfs/opt/casa/delegated_memory.py
"""Delegated-context memory bridge (tiered-memory design §3, plan 3).

Specialists / executors / engagements are NOT residents: they are ephemeral
(no session registry → the freshness reaper never sees them). They become
ordinary participants on the ONE shared bank ``casa``, distinguished only by the
clearance/write-trust INHERITED from their originating context (the resident
turn / engagement that spawned them — its channel is on ``origin_var`` /
``engagement.origin``).

- Read  → ``delegated_recall`` : a single ``recall`` at the originating context's
  read-clearance. When the caller names the originating ROUTE the clearance comes
  from that server-stamped marker (``clearance_for_origin``) — necessary since
  #336, because a telegram turn's clearance is PER SENDER and a channel-keyed
  lookup would hand a non-operator's delegation the operator's private tier. A
  caller that names no route keeps the channel-keyed lookup.
- Write → ``retain_delegated`` : an EXPLICIT, write-trust-gated, per-item
  tier-classified ``retain`` (the reaper can't catch ephemeral turns). Voice
  (recall-only) writes nothing. Both are best-effort — they never crash a
  delegated turn.
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

from channel_policy import writes_to_bank
from hindsight_ids import bank_id
from memory_provenance import build_retain_items
from personality_types import RetainedTurn, SpeakerProvenance
from recall_health import RecallPath, default_telemetry, observed_recall
from recall_renderer import Surface, render_recall
from semantic_memory import RecallUnavailable
from sensitivity import (
    clearance_for_channel, clearance_for_origin, readable_tiers,
)
from tier_classifier import classify_tier

logger = logging.getLogger(__name__)


async def delegated_recall(
    semantic_memory: Any, *, query: str, origin_channel: str, max_tokens: int,
    budget: str = "mid", surface: Surface = "text",
    path: RecallPath = "delegated", current_speaker: SpeakerProvenance | None = None,
    origin_route: str | None = None, origin_clearance: str | None = None,
    with_stats: bool = False,
) -> str | tuple[str, int]:
    """Recall the shared bank at the ORIGINATING context's read-clearance,
    returning an ATTRIBUTED digest (personality Task 11).

    Three-outcome contract (v0.99.0): returns the digest ('' = a GENUINE
    zero-hit search) or raises :class:`RecallUnavailable` when memory could
    not be checked — the two must never be conflated, or the delegated agent
    denies knowledge Casa actually has. Call sites decide how to degrade
    (typically: proceed with no memory block, without claiming absence).

    ``with_stats=True`` returns ``(digest, hit_count)`` instead (#472, Sol
    diff-gate r1): a rendered ``""`` alone cannot distinguish "zero readable
    hits" from "readable hits that exceeded the render budget", and a caller
    that words emptiness as absence needs the count to avoid the overclaim.

    A blank ``query`` is a fourth thing — an invalid call, not a result — and
    raises :class:`ValueError` (#201). It cannot be reported as '' without
    claiming a search happened.

    Task 11: swaps the flat ``recall()`` string for typed ``recall_items()``
    routed through the NEW ``recall_health.observed_recall`` breaker/telemetry
    (``path`` distinguishes the specialist_archive / query_engager /
    executor_archive callers), then renders each hit with its recorded
    attribution. The exact
    unavailable-vs-zero-hit discipline is UNCHANGED — only success-path
    rendering differs.

    ``budget`` defaults to ``mid``. The v0.68.1 ``low`` default (D-3) was a
    stop-gap for a hindsight-side rerank-latency bug that crossed the 20s
    client budget under concurrent load; once that was fixed hindsight-side,
    ``mid`` (→300 reranked candidates) is the better default — materially
    higher recall quality — and no longer risks the timeout. Reverted v0.69.4.
    Explicit ``budget=`` (e.g. voice → ``low``) still overrides."""
    # #201: a blank query used to return the SAME "" as a genuine zero-hit
    # search. That is a lie the contract above cannot survive — no search ran,
    # so "nothing was found" is unearned. It reached a user-visible surface:
    # `query_engager` accepted a blank question and reported status=unknown,
    # which its own tool description defines as "the memory was searched and
    # holds nothing relevant" (Sol + Terra, both Blocking).
    #
    # A blank query is now a CALLER BUG, not an outcome. Raising makes the
    # conflation impossible by construction instead of forbidden by comment;
    # callers that legitimately want silence for an empty task must say so
    # themselves by not calling. Every call site is guarded — see
    # `tools.query_engager` (rejects `empty_query` at the tool boundary) and
    # the two prompt-assembly callers in `tools.py`, which skip the recall.
    if not (query or "").strip():
        raise ValueError(
            "delegated_recall requires a non-blank query; a blank one performs "
            "no search and must not be reported as a zero-hit result (#201)"
        )
    # #336 (Terra, review r2): when the caller can name the originating
    # ROUTE, clearance resolves off that server-stamped marker rather than the
    # channel alone — a telegram turn's clearance is per sender, so a
    # channel-keyed lookup would hand an engagement created by a non-operator
    # the operator's private tier. Callers that pass no route keep the exact
    # channel-keyed behavior they had.
    clearance = (
        clearance_for_origin(origin_route, origin_clearance, origin_channel)
        if origin_route is not None
        else clearance_for_channel(origin_channel)
    )
    tags = readable_tiers(clearance)
    if current_speaker is None:
        current_speaker = SpeakerProvenance(speaker_kind="system")
    try:
        hits = await observed_recall(
            path=path, telemetry=default_telemetry(),
            operation=lambda: semantic_memory.recall_items(
                bank_id("casa"), query, tags=tags, max_tokens=max_tokens,
                clearance=clearance, budget=budget,
            ),
        )
    except RecallUnavailable:
        # Backend already logged outcome/reason/latency (recorded by
        # observed_recall). RecallProtocolError (a RecallUnavailable subclass)
        # is caught here too — a malformed/untrustworthy envelope is UNAVAILABLE,
        # never a fake zero-hit.
        logger.warning("delegated recall unavailable (channel=%s)", origin_channel)
        raise
    except Exception as exc:  # noqa: BLE001 — typed for callers, never a raw crash
        # Exception TYPE only — repr/traceback could embed the query text,
        # which must never be logged.
        logger.warning(
            "delegated recall failed (channel=%s): %s",
            origin_channel, type(exc).__name__,
        )
        raise RecallUnavailable("backend_error") from exc
    digest = render_recall(
        hits, current_speaker=current_speaker, surface=surface,
        clearance=clearance, token_budget=max_tokens,
    )
    if with_stats:
        return digest, len(hits)
    return digest


async def retain_delegated(
    semantic_memory: Any, *, origin_channel: str, turns: Sequence[RetainedTurn],
) -> None:
    """Explicitly retain delegated ``turns`` to the shared bank, each classified at
    its TRUE tier AND attributed to its real :class:`SpeakerProvenance` — IFF the
    originating channel is write-trusted (voice → recall-only → nothing).

    Personality Task 10: dropped ``doc_prefix`` — :func:`build_retain_items`
    content-addresses each turn (user_peer- or persona-identity-keyed), so
    re-retain idempotency no longer needs a caller-scoped prefix, and the same
    fact retained across delegations collapses to one document. ``classify_tier``
    is passed by name so tests that monkeypatch ``delegated_memory.classify_tier``
    still take effect. Best-effort; never raises."""
    if not writes_to_bank(origin_channel):
        return
    items = await build_retain_items(turns, classify=classify_tier)
    if not items:
        return
    try:
        await semantic_memory.retain(bank_id("casa"), items, async_=True)
    except Exception:  # noqa: BLE001 — best-effort background write
        logger.warning(
            "delegated retain failed (origin_channel=%s)", origin_channel, exc_info=True,
        )
