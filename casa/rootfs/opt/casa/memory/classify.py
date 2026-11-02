"""Tier classification of captured events, in the ``private`` job.

Capture stores every event ``private`` and ``provisional``. Before its commit,
the ``private`` job classifies each provisional event it files with Casa's
tier classifier, and the commit writes the verdict: the event's tier becomes
the verdict, never below its ``origin_tier``, and it is ``classified``. A
classification that fails (the classifier's fallback) leaves the event
provisional: still ``private``, and pending in no lower ledger, since only a
classified event is eligible below ``private``.

The classifier is a port: a coroutine from text to a tier, or ``None`` when it
fell back. :func:`default_classifier` adapts Casa's ``tier_classifier``,
imported only when it runs, so the package imports nothing of the running
Casa process.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable

from memory import pending

logger = logging.getLogger(__name__)

#: Text to a tier, or ``None`` when the classification fell back.
Classifier = Callable[[str], Awaitable["str | None"]]


async def default_classifier(text: str) -> str | None:
    """Casa's tier classifier; its fallback verdict is ``None``."""
    from tier_classifier import FallbackTier, classify_tier
    verdict = await classify_tier(text)
    if isinstance(verdict, FallbackTier):
        return None
    return str(verdict)


def floor(verdict: str, origin_tier: str | None) -> str:
    """*verdict*, raised to *origin_tier* when that is higher."""
    if origin_tier is None or pending.rank(origin_tier) <= pending.rank(verdict):
        return verdict
    return origin_tier


async def classify_events(events: Iterable[dict],
                          classifier: Classifier) -> dict[int, str]:
    """The tier to write for each provisional event in *events*, by ``seq``.
    An event whose classification fell back, or raised, is left out: it stays
    provisional."""
    verdicts: dict[int, str] = {}
    for event in events:
        if event["tier_status"] != "provisional":
            continue
        try:
            verdict = await classifier(event["text"])
        except Exception:  # noqa: BLE001 — a failed call is a fallback
            logger.warning("memory classify: event %s left provisional",
                           event["seq"], exc_info=True)
            continue
        if verdict is None:
            continue
        if verdict not in pending.TIERS:
            logger.warning("memory classify: unknown tier for event %s; left "
                           "provisional", event["seq"])
            continue
        verdicts[event["seq"]] = floor(verdict, event.get("origin_tier"))
    return verdicts
