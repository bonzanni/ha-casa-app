# casa/rootfs/opt/casa/tier_classifier.py
"""Per-item sensitivity-tier classifier (design §2.4, revised 2026-06-04).

Runs the eval-validated SENSITIVITY_PROMPT (sensitivity.py, v0.42.1, live
accuracy 0.94–0.97) as a one-shot SDK query over a single retained item's text,
returning its access tier. Used by the freshness reaper / save path — OFF the
turn's critical path — so the per-turn hot path makes no classification call.

Leak-safe: any uncertainty, empty content, or backend error → DEFAULT_TIER
(``private``), so a mis-handled fact is forgotten at lower clearances rather than
leaked. Voice never reaches here (voice is recall-only — see channel_policy)."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses
import logging
from collections.abc import Iterator

from claude_runtime import CLAUDE_CLI_PATH
# Re-export the canonical sensitivity-tier set (single source of truth is
# sensitivity.py:TIERS) so consumers gate on ``from tier_classifier import
# TIERS, classify_tier`` — classify_tier only ever returns a member of it.
from sensitivity import (
    DEFAULT_TIER,
    SENSITIVITY_PROMPT,
    TIER_FORMAT_REMINDER,
    TIERS,
    parse_tier,
    tier_evidence,
)

__all__ = ["ClassifyStats", "classify_stats", "classify_tier", "TIERS"]

logger = logging.getLogger(__name__)


# #508: aggregate observability for a batch of classifications. Measured live
# (v0.177.0): ~12% of calls in a 48-item save defaulted with only scattered
# per-item WARNs to notice it by — and small saves look clean by chance
# (0.88^5 ≈ 53% chance of zero warnings in a 5-item save), so per-item lines
# alone cannot tell an operator the rate. The save path opens a counting scope
# (``classify_stats``); each ``classify_tier`` call inside it increments the
# scope's counters. The scope is a contextvar so concurrent saves in separate
# tasks never share counters, and ``asyncio.gather`` children (the bounded
# classify fan-out) inherit the opener's scope. Counts are STRUCTURAL only —
# never reply or item content (the leak-safety rule below).
@dataclasses.dataclass
class ClassifyStats:
    total: int = 0      # classify_tier calls inside the scope
    defaulted: int = 0  # calls that fell to DEFAULT_TIER on FAILURE (backend
                        # errors exhausted, or reply still unparseable after
                        # the #508 re-ask) — a genuine ``private``
                        # classification is never counted


_STATS: contextvars.ContextVar[ClassifyStats | None] = contextvars.ContextVar(
    "tier_classify_stats", default=None,
)


@contextlib.contextmanager
def classify_stats() -> Iterator[ClassifyStats]:
    """Open a counting scope for the classify_tier calls made underneath it
    (#508). Yields the live ``ClassifyStats``; read it after the batch."""
    stats = ClassifyStats()
    token = _STATS.set(stats)
    try:
        yield stats
    finally:
        _STATS.reset(token)

# D-5 (v0.69.2): backoff before the single retry. A transient SDK/CLI failure
# permanently mis-tiers the item to ``private`` (over-restriction — the fact
# becomes invisible below private clearance forever), and this path is off the
# turn's critical path, so one bounded retry is cheap insurance. Module-level
# so tests zero it instead of patching any sleep (see the CLAUDE.md memory
# cage note: never patch ``<module>.asyncio.sleep``).
_RETRY_BACKOFF_S = 2.0
_ATTEMPTS = 2


# A garbled reply used to default SILENTLY — indistinguishable from a correct
# ``private`` classification when auditing tiering accuracy. #497: length
# alone made the failure undiagnosable, but the reply TEXT must never reach
# the logs (review r1, Sol+Terra): the classifier's words paraphrase the
# retained item, and this module's own doctrine is leak-safety. Log bounded
# STRUCTURAL metadata only — enough to tell "verbose prose, no answer line"
# from "answer line present but malformed" without carrying content.
def _warn_unparseable(reply: str, *, will_reask: bool) -> None:
    lines = [ln for ln in reply.splitlines() if ln.strip()]
    has_label = any(
        ln.strip().lower().startswith("tier:") for ln in lines)
    logger.warning(
        "tier classification reply unparseable (%d chars, %d lines, "
        "tier label %s); %s",
        len(reply), len(lines),
        "present" if has_label else "absent",
        "re-asking once with the answer-line mandate restated"
        if will_reask else f"defaulting to {DEFAULT_TIER}",
    )


async def classify_tier(content: str) -> str:
    """Classify one fact/item into a sensitivity tier. Returns a member of TIERS;
    DEFAULT_TIER on blank input, a reply still unparseable after one stricter
    re-ask (#508), or any backend error (after one retry per ask — D-5)."""
    text = (content or "").strip()
    if not text:
        return DEFAULT_TIER
    import claude_agent_sdk as sdk

    opts = sdk.ClaudeAgentOptions(
        cli_path=CLAUDE_CLI_PATH,
        # max_turns=8 (#497 reopen + operator ruling 2026-08-11): on 0.174.0
        # the runtime errored with "Reached maximum number of turns (1)"; the
        # 0.176.0 fix granted one spare turn and the live session still hit
        # "Reached maximum number of turns (2)" on 2 of 24 retentions (both
        # retries also exhausted, so both items defaulted to private). Turn
        # accounting is the runtime's, not ours, and a missed answer costs
        # far more than a spare turn — so the cap is sized as a RUNAWAY
        # BACKSTOP only, never an efficiency device: exhaustion must be a
        # rare terminal state, not a routine one.
        #
        # tools=[] + disallowed Bash/Task/Agent (Sol r1 S1): the turns must
        # be genuinely inert. ``allowed_tools=[]`` alone is only
        # auto-approval — built-in tools stay reachable, and acceptEdits
        # would auto-approve edits — so built-ins are REMOVED outright,
        # with Agent/Task denied (they bypass allowed_tools) and Bash
        # belt-and-braces, mirroring the restricted-webhook containment
        # doctrine (agent.py).
        system_prompt=SENSITIVITY_PROMPT, max_turns=8,
        tools=[], allowed_tools=[],
        disallowed_tools=["Bash", "Task", "Agent"],
        # NOT bypassPermissions: that makes the SDK pass
        # ``--dangerously-skip-permissions`` to the bundled ``claude`` CLI, which
        # refuses to run as root/sudo — and HA add-ons run as root, so the call
        # fails and every item silently defaults to ``private``. With
        # ``allowed_tools=[]`` there is nothing to approve, so acceptEdits (the
        # mode the rest of casa runs as root) never prompts and works.
        permission_mode="acceptEdits",
    )
    stats = _STATS.get()
    if stats is not None:
        stats.total += 1

    async def _ask(prompt: str) -> str | None:
        """One classification ask under the D-5 exception ladder: one retry
        with backoff on any backend error, then None (the caller defaults)."""
        for attempt in range(1, _ATTEMPTS + 1):
            reply = ""
            try:
                async for msg in sdk.query(prompt=prompt, options=opts):
                    if isinstance(msg, sdk.AssistantMessage):
                        for block in getattr(msg, "content", []) or []:
                            t = getattr(block, "text", None)
                            if isinstance(t, str):
                                reply += t
            except Exception as exc:  # noqa: BLE001 — classifier must never crash a save
                # The exception type+message live IN the log line: the D-5
                # occurrences' tracebacks were truncated by log tooling and the
                # container logs were gone before anyone could read them.
                if attempt < _ATTEMPTS:
                    logger.warning(
                        "tier classification attempt %d/%d failed (%s: %s); retrying",
                        attempt, _ATTEMPTS, type(exc).__name__, exc,
                    )
                    await asyncio.sleep(_RETRY_BACKOFF_S)
                    continue
                logger.warning(
                    "tier classification failed after %d attempts (%s: %s); "
                    "defaulting to %s",
                    _ATTEMPTS, type(exc).__name__, exc, DEFAULT_TIER,
                    exc_info=True,
                )
                return None
            return reply
        return None  # pragma: no cover — loop always returns

    reply = await _ask(text)
    if reply is not None:
        tier = parse_tier(reply)
        if tier:
            return tier
        # #508: the exception ladder above never covered THIS case — a reply
        # the parser correctly refuses (measured live at ~12% of calls on a
        # 48-item save: the model omitted the mandated answer line entirely)
        # defaulted to private FIRST STRIKE. Re-ask exactly once with the
        # output format restated after the fact text; the final failure keeps
        # the leak-safe DEFAULT_TIER (a leak is worse than forgetting).
        _warn_unparseable(reply, will_reask=True)
        # Review r1 (Sol S1): the discarded reply may still carry answer-shaped
        # lines ("This must stay confidential.\nprivate"). The re-ask is a
        # fresh, stateless sample — accepting it unchecked would let a second
        # opinion of ``public`` overrule a first reply whose only extractable
        # answer was ``private``, storing the fact at a LESS sensitive tier.
        # Evidence is a FLOOR only: a re-ask answer at or above it is accepted;
        # one below it is a cross-ask conflict — ambiguity, never "last one
        # wins" (#350/r2 doctrine) — and falls to the leak-safe default.
        floor = max(
            (TIERS.index(t) for t in tier_evidence(reply)), default=-1)
        reply = await _ask(f"{text}\n\n{TIER_FORMAT_REMINDER}")
        if reply is not None:
            tier = parse_tier(reply)
            if tier and TIERS.index(tier) >= floor:
                return tier
            if tier:
                logger.warning(
                    "re-asked tier classification is less sensitive than "
                    "answer-shaped evidence in the discarded reply; "
                    "defaulting to %s", DEFAULT_TIER,
                )
            else:
                _warn_unparseable(reply, will_reask=False)
    if stats is not None:
        stats.defaulted += 1
    return DEFAULT_TIER
