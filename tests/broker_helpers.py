"""Shared test helper for resolving verdict_broker requests.

#469 removed ``VerdictBroker.deliver`` from production — the internal
``/internal/channel/permission_verdict`` endpoint was its only caller, and
verdicts now have exactly one writer (the in-process Telegram callback's
``claim``/``commit``). Tests still need a one-shot resolve; this helper
replicates the old claim→commit composition and return contract
(``"delivered"`` | ``"duplicate"`` | ``"stale"`` | ``"forbidden"``).
"""

from __future__ import annotations


def deliver(
    broker, *, namespace: str, scope: str, request_id: str,
    option_index: int, actor_id: int | None,
) -> str:
    claim = broker.claim(
        namespace=namespace, scope=scope, request_id=request_id,
        option_index=option_index, actor_id=actor_id,
    )
    if isinstance(claim, str):
        return claim
    return "delivered" if broker.commit(claim) else "stale"


async def wait_until(predicate, *, timeout: float = 5.0) -> None:
    """Poll *predicate* until it holds, bounded by WALL-CLOCK time (not a
    fixed real-time sleep), so a slow/loaded runner gets real slack while a
    fast run resolves in a handful of scheduler turns. Cheap when fast,
    tolerant when slow, and no weaker than the fixed sleep it replaces — it
    still raises (``TimeoutError``) if the condition never fires. Same
    contract as test_multi_select_ask._wait_until; shared here because the
    fixed-sleep pattern it replaces flaked across multiple ask-suite files
    on loaded CI runners."""
    import asyncio

    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)
