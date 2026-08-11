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
import logging

from claude_runtime import CLAUDE_CLI_PATH
# Re-export the canonical sensitivity-tier set (single source of truth is
# sensitivity.py:TIERS) so consumers gate on ``from tier_classifier import
# TIERS, classify_tier`` — classify_tier only ever returns a member of it.
from sensitivity import DEFAULT_TIER, SENSITIVITY_PROMPT, TIERS, parse_tier

__all__ = ["classify_tier", "TIERS"]

logger = logging.getLogger(__name__)

# D-5 (v0.69.2): backoff before the single retry. A transient SDK/CLI failure
# permanently mis-tiers the item to ``private`` (over-restriction — the fact
# becomes invisible below private clearance forever), and this path is off the
# turn's critical path, so one bounded retry is cheap insurance. Module-level
# so tests zero it instead of patching any sleep (see the CLAUDE.md memory
# cage note: never patch ``<module>.asyncio.sleep``).
_RETRY_BACKOFF_S = 2.0
_ATTEMPTS = 2


async def classify_tier(content: str) -> str:
    """Classify one fact/item into a sensitivity tier. Returns a member of TIERS;
    DEFAULT_TIER on blank input, an unparseable reply, or any backend error
    (after one retry — D-5)."""
    text = (content or "").strip()
    if not text:
        return DEFAULT_TIER
    import claude_agent_sdk as sdk

    opts = sdk.ClaudeAgentOptions(
        cli_path=CLAUDE_CLI_PATH,
        # max_turns=2 (#497): on 0.174.0 the runtime sometimes errored with
        # "Reached maximum number of turns (1)" instead of returning any text
        # (turn 1 apparently spent on preamble/thinking). One spare turn is
        # harmless headroom — allowed_tools=[] means there is nothing agentic
        # a second turn could do except finish emitting text.
        system_prompt=SENSITIVITY_PROMPT, max_turns=2, allowed_tools=[],
        # NOT bypassPermissions: that makes the SDK pass
        # ``--dangerously-skip-permissions`` to the bundled ``claude`` CLI, which
        # refuses to run as root/sudo — and HA add-ons run as root, so the call
        # fails and every item silently defaults to ``private``. With
        # ``allowed_tools=[]`` there is nothing to approve, so acceptEdits (the
        # mode the rest of casa runs as root) never prompts and works.
        permission_mode="acceptEdits",
    )
    for attempt in range(1, _ATTEMPTS + 1):
        reply = ""
        try:
            async for msg in sdk.query(prompt=text, options=opts):
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
                _ATTEMPTS, type(exc).__name__, exc, DEFAULT_TIER, exc_info=True,
            )
            return DEFAULT_TIER
        tier = parse_tier(reply)
        if tier:
            return tier
        # A garbled reply used to default SILENTLY — indistinguishable from a
        # correct ``private`` classification when auditing tiering accuracy.
        # #497: length alone made the failure undiagnosable (the raw replies
        # were never captured); log a bounded head of the reply too. The
        # snippet is the CLASSIFIER'S words, may paraphrase the item, and goes
        # through the app's redaction pipeline like every other log line.
        logger.warning(
            "tier classification reply unparseable (%d chars, head=%r); "
            "defaulting to %s",
            len(reply), reply[:200], DEFAULT_TIER,
        )
        return DEFAULT_TIER
    return DEFAULT_TIER  # pragma: no cover — loop always returns
