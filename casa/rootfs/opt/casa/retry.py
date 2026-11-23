"""Retry + backoff policy for the SDK call path (spec 5.2 §3).

Exposes a single public async coroutine :func:`retry_sdk_call` plus two
pure helpers used to compute the wait schedule and to extract a
server-supplied ``Retry-After`` hint from an exception message.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from typing import Awaitable, Callable, TypeVar

from error_kinds import ErrorKind, _classify_error

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Config — env-driven, read at import time
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, *, min_value: int = 1) -> int:
    """Read a positive int from env; log + fall back on bad input.

    Missing / empty value → default. Non-numeric → default with a
    WARNING. Value < min_value → min_value with a WARNING. Never
    raises, so bad add-on config cannot crash module import.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r; using default %d", name, raw, default,
        )
        return default
    if value < min_value:
        logger.warning(
            "%s=%d below minimum %d; using %d",
            name, value, min_value, min_value,
        )
        return min_value
    return value


MAX_ATTEMPTS: int = _env_int("SDK_RETRY_MAX_ATTEMPTS", 3)
INITIAL_MS: int = _env_int("SDK_RETRY_INITIAL_MS", 500)
CAP_MS: int = _env_int("SDK_RETRY_CAP_MS", 8000)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

RETRY_KINDS: frozenset[ErrorKind] = frozenset({
    ErrorKind.TIMEOUT,
    ErrorKind.RATE_LIMIT,
    ErrorKind.SDK_ERROR,
})


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


def compute_backoff_ms(
    attempt: int, *, initial_ms: int, cap_ms: int,
) -> int:
    """Exponential backoff with multiplicative 0.5–1.0x jitter.

    ``attempt`` is zero-based (the first retry uses ``attempt=0``).
    Ceiling doubles each attempt; the returned value is a uniform
    random in ``[ceiling/2, ceiling]``. Cap applies after exponentiation.
    """
    ceiling = min(initial_ms * (2 ** attempt), cap_ms)
    lower = ceiling // 2
    return random.randint(lower, ceiling)


# ---------------------------------------------------------------------------
# Retry-After parsing
# ---------------------------------------------------------------------------

_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry[-_ ]?after[:=\s]*([0-9]*\.?[0-9]+)", re.IGNORECASE),
)


def parse_retry_after_ms(exc: Exception) -> int | None:
    """Extract a Retry-After hint (in ms) from an exception message.

    Matches ``retry-after: 5`` / ``Retry-After=12s`` / ``retry after 0.5``.
    Returns ``None`` when absent or non-numeric.
    """
    text = str(exc)
    for pat in _RETRY_AFTER_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        try:
            seconds = float(m.group(1))
        except ValueError:
            return None
        return int(seconds * 1000)
    return None


# ---------------------------------------------------------------------------
# Retry loop — implemented in Task 4
# ---------------------------------------------------------------------------


async def retry_sdk_call(
    fn: Callable[[], Awaitable[T]],
    *,
    on_retry: Callable[[int, Exception, int], None] | None = None,
    may_retry: Callable[[Exception], bool] | None = None,
) -> T:
    """Run ``fn`` with exponential-backoff retry on transient SDK faults.

    Retries only exceptions classified as TIMEOUT, RATE_LIMIT, or
    SDK_ERROR (via ``agent._classify_error``). Honours a
    ``Retry-After`` hint if the exception message carries one; falls
    back to jittered exponential backoff otherwise.

    ``may_retry``, when given, is asked about each fault of a retryable
    kind: the caller knows whether the attempt that failed may be run
    again (one that already ran a tool may not). False raises that
    exception at once, as the last attempt does: nothing is slept and
    ``on_retry`` is not called.

    ``asyncio.CancelledError`` is re-raised immediately without
    counting as an attempt; a retry loop must not swallow caller
    cancellation (spec 5.2 §3.2 — voice barge-in).
    """
    last_exc: BaseException | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — retry classifies below
            kind = _classify_error(exc)
            if kind not in RETRY_KINDS:
                raise
            last_exc = exc
            is_last = attempt == MAX_ATTEMPTS - 1
            if is_last:
                raise
            if may_retry is not None and not may_retry(exc):
                raise
            hinted = parse_retry_after_ms(exc)
            if hinted is not None:
                # Cap server-supplied hints at 10x the backoff cap to
                # prevent a misbehaving upstream from parking the worker
                # indefinitely (spec 5.2 §3 — pragmatic bound, not spec'd).
                delay_ms = min(hinted, 10 * CAP_MS)
            else:
                delay_ms = compute_backoff_ms(
                    attempt, initial_ms=INITIAL_MS, cap_ms=CAP_MS,
                )
            if on_retry is not None:
                on_retry(attempt, exc, delay_ms)
            await asyncio.sleep(delay_ms / 1000.0)
    # Unreachable when MAX_ATTEMPTS >= 1 (guaranteed by _env_int).
    # Raise instead of asserting so -O doesn't silently return None.
    raise RuntimeError(
        f"retry_sdk_call: MAX_ATTEMPTS={MAX_ATTEMPTS} did not allow any attempts"
    )
