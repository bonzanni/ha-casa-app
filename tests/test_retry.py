"""Unit tests for the retry helpers (spec 5.2 §3)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import retry as retry_mod
from retry import (
    RETRY_KINDS,
    compute_backoff_ms,
    parse_retry_after_ms,
    retry_sdk_call,
)
from agent import ErrorKind



@contextmanager
def patch_retry_sleep():
    """Make retry's backoff instant WITHOUT touching the global asyncio.

    Patching ``retry.asyncio.sleep`` mutates the shared ``asyncio`` module
    object process-wide; with the SDK-client-pool sweeper looping
    ``while True: await asyncio.sleep(interval)`` in a background task, an
    instant-return sleep turns that loop into a tight CPU spin whose
    AsyncMock records every call → unbounded RSS (~23 GB) + OOM. Instead we
    replace retry's *module-local* ``asyncio`` reference with a namespace
    carrying just the two attributes retry.py touches, so no other module's
    ``asyncio.sleep`` is affected. Yields the sleep AsyncMock for assertions.
    """
    sleep = AsyncMock()
    ns = SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError)
    with patch.object(retry_mod, "asyncio", ns):
        yield sleep


# ---------------------------------------------------------------------------
# compute_backoff_ms
# ---------------------------------------------------------------------------


class TestComputeBackoffMs:
    def test_first_attempt_is_initial(self):
        # Jitter is multiplicative 0.5–1.0x of the ceiling, so the minimum
        # for attempt 0 equals half the initial.
        vals = [
            compute_backoff_ms(0, initial_ms=500, cap_ms=8000)
            for _ in range(100)
        ]
        assert all(250 <= v <= 500 for v in vals), vals

    def test_exponential_growth_until_cap(self):
        # attempt 0 → 500, attempt 1 → 1000, attempt 2 → 2000, attempt 3 → 4000,
        # attempt 4 → 8000 (cap), attempt 5 → 8000 (stays capped).
        def ceiling(a):
            return min(500 * (2 ** a), 8000)

        for attempt in range(6):
            v = compute_backoff_ms(attempt, initial_ms=500, cap_ms=8000)
            c = ceiling(attempt)
            assert c / 2 <= v <= c, (attempt, v, c)

    def test_cap_applies_for_large_attempt_numbers(self):
        # attempt 20 with 500ms initial would be 500 * 2**20 without cap;
        # cap forces everything ≤ cap_ms.
        v = compute_backoff_ms(20, initial_ms=500, cap_ms=8000)
        assert 4000 <= v <= 8000


# ---------------------------------------------------------------------------
# parse_retry_after_ms
# ---------------------------------------------------------------------------


class TestParseRetryAfterMs:
    def test_plain_seconds(self):
        exc = RuntimeError("429 rate limit, retry-after: 5")
        assert parse_retry_after_ms(exc) == 5000

    def test_retry_after_key_value(self):
        exc = RuntimeError("Rate limited. Retry-After: 12s")
        assert parse_retry_after_ms(exc) == 12000

    def test_fractional_seconds(self):
        exc = RuntimeError("retry-after: 0.5")
        assert parse_retry_after_ms(exc) == 500

    def test_absent_returns_none(self):
        exc = RuntimeError("just a generic 429")
        assert parse_retry_after_ms(exc) is None

    def test_non_numeric_returns_none(self):
        exc = RuntimeError("retry-after: soon")
        assert parse_retry_after_ms(exc) is None


# ---------------------------------------------------------------------------
# RETRY_KINDS
# ---------------------------------------------------------------------------


class TestRetryKinds:
    def test_retryable_set(self):
        assert RETRY_KINDS == frozenset({
            ErrorKind.TIMEOUT,
            ErrorKind.RATE_LIMIT,
            ErrorKind.SDK_ERROR,
        })

    def test_unknown_is_not_retryable(self):
        assert ErrorKind.UNKNOWN not in RETRY_KINDS

    def test_memory_error_is_not_retryable(self):
        assert ErrorKind.MEMORY_ERROR not in RETRY_KINDS

    def test_channel_error_is_not_retryable(self):
        assert ErrorKind.CHANNEL_ERROR not in RETRY_KINDS


# ---------------------------------------------------------------------------
# retry_sdk_call
# ---------------------------------------------------------------------------


def _sdk_timeout() -> asyncio.TimeoutError:
    return asyncio.TimeoutError()


def _sdk_error() -> RuntimeError:
    # Exception type name contains "CLI" → _classify_error → SDK_ERROR
    CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
    return CLIConnectionError("upstream reset")


def _rate_limit(retry_after: str | None = None) -> RuntimeError:
    if retry_after is None:
        return RuntimeError("429 rate limit exceeded")
    return RuntimeError(f"429 rate limit. Retry-After: {retry_after}")


def _unknown() -> ValueError:
    return ValueError("bad input — not retryable")


class TestRetrySdkCall:
    async def test_success_on_first_attempt_returns_value(self):
        async def fn():
            return "ok"

        with patch_retry_sleep() as sleep:
            result = await retry_sdk_call(fn)

        assert result == "ok"
        sleep.assert_not_awaited()

    async def test_transient_sdk_error_retries_until_success(self):
        calls = iter([_sdk_error(), _sdk_error(), "ok"])

        async def fn():
            nxt = next(calls)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        with patch_retry_sleep():
            result = await retry_sdk_call(fn)
        assert result == "ok"

    async def test_exhausts_max_attempts_then_raises(self):
        # Three attempts total, all failing → the last exception surfaces.
        async def fn():
            raise _sdk_error()

        with patch_retry_sleep(), \
             patch("retry.MAX_ATTEMPTS", 3):
            with pytest.raises(Exception) as excinfo:
                await retry_sdk_call(fn)
        assert "CLIConnectionError" in type(excinfo.value).__name__

    async def test_unknown_exception_does_not_retry(self):
        async def fn():
            raise _unknown()

        with patch_retry_sleep() as sleep:
            with pytest.raises(ValueError):
                await retry_sdk_call(fn)
        sleep.assert_not_awaited()

    async def test_rate_limit_with_retry_after_uses_parsed_delay(self):
        attempts = {"n": 0}

        async def fn():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _rate_limit("7")
            return "ok"

        with patch_retry_sleep() as sleep:
            result = await retry_sdk_call(fn)

        assert result == "ok"
        # One retry fired; sleep was awaited once with the parsed delay
        # (7s = 7.0 as seconds argument), not the jittered backoff.
        sleep.assert_awaited_once_with(7.0)

    async def test_rate_limit_without_retry_after_uses_backoff(self):
        attempts = {"n": 0}

        async def fn():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _rate_limit(None)
            return "ok"

        with patch_retry_sleep() as sleep:
            result = await retry_sdk_call(fn)
        assert result == "ok"
        # Delay is a jittered backoff (0.25–0.5s for attempt 0 with
        # defaults), not a specific retry-after.
        assert sleep.await_count == 1
        delay = sleep.await_args.args[0]
        assert 0.25 <= delay <= 0.5

    async def test_on_retry_callback_is_invoked(self):
        attempts = {"n": 0}

        async def fn():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _sdk_error()
            return "ok"

        seen: list[tuple[int, str, int]] = []

        def on_retry(attempt: int, exc: Exception, delay_ms: int) -> None:
            seen.append((attempt, type(exc).__name__, delay_ms))

        with patch_retry_sleep():
            await retry_sdk_call(fn, on_retry=on_retry)

        assert len(seen) == 1
        attempt, name, delay_ms = seen[0]
        assert attempt == 0
        assert "CLIConnectionError" in name
        assert 250 <= delay_ms <= 500

    async def test_cancelled_error_propagates_immediately(self):
        """Pins INV-TURN-005 (cancellation half; RETRY_KINDS membership is pinned by the sibling kind tests). Red case demonstrated: replacing the CancelledError re-raise with a retry continue fails this test."""
        attempts = {"n": 0}

        async def fn():
            attempts["n"] += 1
            raise asyncio.CancelledError()

        with patch_retry_sleep() as sleep:
            with pytest.raises(asyncio.CancelledError):
                await retry_sdk_call(fn)
        # CancelledError is not retryable; no sleep should have run.
        sleep.assert_not_awaited()
        assert attempts["n"] == 1


# ---------------------------------------------------------------------------
# Hardening: retry-after cap + env validation
# ---------------------------------------------------------------------------


class TestRetryAfterCap:
    async def test_large_retry_after_is_capped_at_10x_cap_ms(self):
        """Server-supplied Retry-After: 3600 must not block the worker
        for an hour. Cap at 10 * CAP_MS (80 s with defaults)."""
        calls = {"n": 0}

        async def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("429 rate limit. Retry-After: 3600")
            return "ok"

        with patch_retry_sleep() as sleep:
            result = await retry_sdk_call(fn)
        assert result == "ok"
        # 10 * 8000 ms = 80 s — the hint was clamped.
        sleep.assert_awaited_once_with(80.0)

    async def test_small_retry_after_passes_through(self):
        """Retry-After: 5 is under the cap; delivered as-is."""
        calls = {"n": 0}

        async def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("429 rate limit. Retry-After: 5")
            return "ok"

        with patch_retry_sleep() as sleep:
            await retry_sdk_call(fn)
        sleep.assert_awaited_once_with(5.0)


class TestEnvValidation:
    def test_missing_env_uses_default(self, monkeypatch):
        monkeypatch.delenv("SDK_RETRY_MAX_ATTEMPTS", raising=False)
        import importlib
        import retry as _retry
        reloaded = importlib.reload(_retry)
        try:
            assert reloaded.MAX_ATTEMPTS == 3
        finally:
            importlib.reload(_retry)

    def test_malformed_env_logs_warning_and_uses_default(
        self, monkeypatch, caplog,
    ):
        monkeypatch.setenv("SDK_RETRY_MAX_ATTEMPTS", "not-a-number")
        import importlib
        import retry as _retry
        with caplog.at_level(logging.WARNING, logger="retry"):
            reloaded = importlib.reload(_retry)
        try:
            assert reloaded.MAX_ATTEMPTS == 3
            assert any(
                "Invalid SDK_RETRY_MAX_ATTEMPTS" in r.message
                for r in caplog.records
            ), [r.message for r in caplog.records]
        finally:
            monkeypatch.delenv("SDK_RETRY_MAX_ATTEMPTS", raising=False)
            importlib.reload(_retry)

    def test_zero_env_is_clamped_to_one(self, monkeypatch, caplog):
        monkeypatch.setenv("SDK_RETRY_MAX_ATTEMPTS", "0")
        import importlib
        import retry as _retry
        with caplog.at_level(logging.WARNING, logger="retry"):
            reloaded = importlib.reload(_retry)
        try:
            assert reloaded.MAX_ATTEMPTS == 1
            assert any(
                "below minimum" in r.message for r in caplog.records
            )
        finally:
            monkeypatch.delenv("SDK_RETRY_MAX_ATTEMPTS", raising=False)
            importlib.reload(_retry)


class TestEnvConfig:
    def test_module_level_defaults_match_spec(self):
        import retry
        # Defaults per spec 5.2 §9.3.
        assert retry.MAX_ATTEMPTS == 3
        assert retry.INITIAL_MS == 500
        assert retry.CAP_MS == 8000

    def test_env_override_reloads(self, monkeypatch):
        """Users reconfigure via env + addon restart. A module reload
        mimics the restart-time binding."""
        import importlib
        import retry as _retry

        monkeypatch.setenv("SDK_RETRY_MAX_ATTEMPTS", "5")
        monkeypatch.setenv("SDK_RETRY_INITIAL_MS", "100")
        monkeypatch.setenv("SDK_RETRY_CAP_MS", "4000")

        reloaded = importlib.reload(_retry)
        try:
            assert reloaded.MAX_ATTEMPTS == 5
            assert reloaded.INITIAL_MS == 100
            assert reloaded.CAP_MS == 4000
        finally:
            # Restore — other tests assume defaults.
            monkeypatch.delenv("SDK_RETRY_MAX_ATTEMPTS", raising=False)
            monkeypatch.delenv("SDK_RETRY_INITIAL_MS", raising=False)
            monkeypatch.delenv("SDK_RETRY_CAP_MS", raising=False)
            importlib.reload(_retry)
