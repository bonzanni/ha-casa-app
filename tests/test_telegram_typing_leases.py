"""W3 (v0.77.0): turn-owned bounded typing leases + button-continuation start.

Sol r1-1: leases are keyed by the turn's cid so overlapping same-chat turns
(the fast-tap timeline — the original turn still narrating while the
button-continuation turn starts) don't cancel each other's indicator.
Sol r1-2: every lease carries a TTL the loop itself enforces, so an
accepted-but-never-consumed turn leaks at most one lease-TTL of "typing…"
even when no teardown ever runs.

Never patch the global ``asyncio.sleep`` (repo hard rule). These tests bound
the loop via short per-lease TTLs and the module-local ``_TYPING_INTERVAL``
constant, and use plain async coroutines (not AsyncMock) for the chat-action
send so the loop's call history can't accumulate unbounded.
"""

from __future__ import annotations

import asyncio
import types

import pytest

import channels.telegram as tg
from channels.telegram import TelegramChannel
from output_boundary_testing import admitted

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _channel() -> TelegramChannel:
    return TelegramChannel(
        known_bot_id=111,
        bot_token="t", chat_id="1", default_agent="a",
    )


class _CountingBot:
    """A bot whose send_chat_action is a plain coroutine (no AsyncMock
    accumulation) that counts invocations."""

    def __init__(self) -> None:
        self.actions = 0

    async def send_chat_action(self, **_kw) -> None:
        self.actions += 1


def _install_bot(ch: TelegramChannel) -> _CountingBot:
    bot = _CountingBot()
    ch._app = type("_App", (), {"bot": bot})()
    return bot


# --------------------------------------------------------------------------
# (a) OVERLAP — original + continuation leases coexist; each turn releases
#     only its own lease; the loop survives until the LAST lease is gone.
# --------------------------------------------------------------------------


async def test_overlap_original_release_keeps_continuation_lease_alive():
    ch = _channel()
    _install_bot(ch)

    ch._start_typing("1", "cidA")  # original turn
    ch._start_typing("1", "cidB")  # button continuation (same chat)

    loop_task = ch._typing_loops["1"]
    assert set(ch._typing_leases["1"]) == {"cidA", "cidB"}
    assert not loop_task.done()

    # Original turn finishes and releases ITS lease by cid (r1-1) — the
    # continuation's indicator must survive.
    await ch.turn_finished({"chat_id": "1", "cid": "cidA"})
    await asyncio.sleep(0)
    assert set(ch._typing_leases["1"]) == {"cidB"}
    assert ch._typing_loops.get("1") is loop_task
    assert not loop_task.done()

    # Continuation delivers and releases its lease → loop exits.
    await ch.turn_finished({"chat_id": "1", "cid": "cidB"})
    await asyncio.sleep(0)
    assert "1" not in ch._typing_leases
    assert "1" not in ch._typing_loops
    assert loop_task.done()


# --------------------------------------------------------------------------
# (b) accepted-but-never-consumed — no teardown ever runs; the lease expires
#     at TTL, the loop exits WITHOUT being cancelled, and nothing leaks.
# --------------------------------------------------------------------------


async def test_lease_expires_at_ttl_loop_exits_without_cancel(monkeypatch):
    # Fast passes via the module-local interval constant (NOT asyncio.sleep).
    monkeypatch.setattr(tg, "_TYPING_INTERVAL", 0.0)
    ch = _channel()
    bot = _install_bot(ch)

    ch._start_typing("1", "orphan", ttl_s=0.05)
    loop_task = ch._typing_loops["1"]

    # No teardown is ever called — the loop must self-bound on the TTL.
    await asyncio.wait_for(loop_task, timeout=2.0)

    assert loop_task.done()
    assert loop_task.cancelled() is False  # natural exit, not a cancel
    assert bot.actions >= 1  # it actually ran before expiring
    assert "1" not in ch._typing_leases
    assert "1" not in ch._typing_loops


# --------------------------------------------------------------------------
# (c) idempotent double-release; release-all fallback when no cid is present.
# --------------------------------------------------------------------------


async def test_double_release_is_idempotent():
    ch = _channel()
    _install_bot(ch)
    ch._start_typing("1", "cidA")

    ch._stop_typing("1", "cidA")
    ch._stop_typing("1", "cidA")  # second release must be a no-op, not raise
    ch._stop_typing("1", "never-existed")  # unknown lease id — no-op

    await asyncio.sleep(0)
    assert "1" not in ch._typing_leases
    assert "1" not in ch._typing_loops


async def test_release_all_fallback_when_context_lacks_cid():
    ch = _channel()
    _install_bot(ch)
    ch._start_typing("1", "cidA")
    ch._start_typing("1", "cidB")
    loop_task = ch._typing_loops["1"]

    # A legacy delivery context with no cid → release ALL leases for the chat
    # (preserves today's semantics).
    await ch.turn_finished({"chat_id": "1"})
    await asyncio.sleep(0)

    assert "1" not in ch._typing_leases
    assert "1" not in ch._typing_loops
    assert loop_task.done()


# --------------------------------------------------------------------------
# (reconnect regression, v0.77.x) — a terminal loop exit must NOT leave a
# stale lease + loop entry that a future turn's _start_typing revives.
# Every terminal `return` of _typing_loop performs identity-guarded cleanup;
# outbound teardown releases the lease BEFORE any availability guard.
# --------------------------------------------------------------------------


async def test_reconnect_app_gone_cleans_both_maps_and_next_turn_is_clean(
    monkeypatch,
):
    # (a) loop running with an active lease → channel drops (`_app = None`) →
    # loop exits and BOTH maps are clean → a new organic turn starts+finishes
    # with no typing surviving on a stale lease.
    monkeypatch.setattr(tg, "_TYPING_INTERVAL", 0.0)
    ch = _channel()
    _install_bot(ch)

    ch._start_typing("1", "cidA")
    loop_task = ch._typing_loops["1"]
    assert set(ch._typing_leases["1"]) == {"cidA"}
    await asyncio.sleep(0)  # let the loop run at least one live pass

    # Channel availability drops mid-lease (reconnect window).
    ch._app = None

    # The loop must exit (terminal return, not a cancel) and clean BOTH maps.
    await asyncio.wait_for(loop_task, timeout=2.0)
    assert loop_task.done()
    assert loop_task.cancelled() is False
    assert "1" not in ch._typing_leases
    assert "1" not in ch._typing_loops

    # A NEW organic turn on the reconnected channel starts from a clean slate.
    _install_bot(ch)
    ch._start_typing("1", "cidB")
    new_task = ch._typing_loops["1"]
    assert new_task is not loop_task
    assert set(ch._typing_leases["1"]) == {"cidB"}

    # When it finishes, no typing continues — no stale lease, loop gone.
    await ch.turn_finished({"chat_id": "1", "cid": "cidB"})
    await asyncio.sleep(0)
    assert "1" not in ch._typing_leases
    assert "1" not in ch._typing_loops
    assert new_task.done()


async def test_breaker_trip_mid_lease_cleans_both_maps(monkeypatch):
    # (b) a 401-class breaker trip mid-lease must clean both maps too, so a
    # later _start_typing (once un-suspended) can't revive the dead loop.
    monkeypatch.setattr(tg, "_TYPING_BACKOFF_INIT", 0.0)
    monkeypatch.setattr(tg, "_TYPING_BACKOFF_MAX", 0.0)
    monkeypatch.setattr(tg, "_TYPING_INTERVAL", 0.0)
    ch = _channel()

    async def failing_send(**_kw):
        raise tg.TelegramError("Unauthorized")

    ch._app = types.SimpleNamespace(
        bot=types.SimpleNamespace(send_chat_action=failing_send)
    )

    ch._start_typing("1", "cidA")
    loop_task = ch._typing_loops["1"]

    await asyncio.wait_for(loop_task, timeout=2.0)
    assert loop_task.done()
    assert loop_task.cancelled() is False
    assert ch._typing_suspended is True
    assert ch._typing_consecutive_failures >= tg._TYPING_CIRCUIT_BREAK
    assert "1" not in ch._typing_leases
    assert "1" not in ch._typing_loops


async def test_teardown_releases_lease_even_when_app_gone():
    # (c) a turn ends during a reconnect window (`_app is None`). The lease is
    # STILL released — release precedes the availability guard in the outbound
    # teardown paths (the final reply's send + first-token callback both
    # covered).
    ch = _channel()  # block mode
    _install_bot(ch)
    ch._start_typing("1", "cidA")
    ch._app = None  # reconnect window at teardown

    await ch.send_response(admitted("hi"), {"chat_id": "1", "cid": "cidA"})
    await asyncio.sleep(0)
    assert "1" not in ch._typing_leases
    assert "1" not in ch._typing_loops


async def test_first_token_releases_lease_even_when_app_gone():
    # Streaming first-token teardown must release the lease before its
    # `_app is None` short-circuit as well.
    ch = TelegramChannel(
        known_bot_id=111,
        bot_token="t", chat_id="1", default_agent="a",
    )
    _install_bot(ch)
    ctx = {"chat_id": "1", "cid": "cidA"}
    ch._start_typing("1", "cidA")
    on_token = ch.create_on_token(ctx)

    ch._app = None  # reconnect window before the first token arrives
    await on_token("hello")
    await asyncio.sleep(0)  # let any loop cancellation propagate
    assert "1" not in ch._typing_leases
    assert "1" not in ch._typing_loops
    on_token.end()  # the turn is over: no stream, and no task of it, stays
    assert ch._streams == {}


async def test_start_typing_noop_when_suspended():
    ch = _channel()
    _install_bot(ch)
    ch._typing_suspended = True
    ch._start_typing("1", "cidA")
    assert "1" not in ch._typing_leases
    assert "1" not in ch._typing_loops


# --------------------------------------------------------------------------
# (d) button continuation — lease started ONLY on acceptance, keyed by the
#     cid the dispatched turn carries; no lease on exhaustion / cancel.
# --------------------------------------------------------------------------


class _Bus:
    def __init__(self, results):
        self._results = list(results)
        self.sent = []

    async def send_checked(self, msg):
        self.sent.append(msg)
        result = self._results.pop(0)
        if isinstance(result, BaseException):  # incl. CancelledError
            raise result
        return result


async def test_dispatch_success_starts_lease_keyed_by_turn_cid():
    ch = _channel()
    bus = _Bus(["accepted"])
    ch._bus = bus
    started = []
    ch._start_typing = lambda chat, lease, **k: started.append((chat, lease))

    ok = await ch._dispatch_button_continuation(
        chat_id=1, user_id=2, target_role="ellen",
        request_id="req-1", text="approve",
    )

    assert ok is True
    # The lease id MUST equal the cid the dispatched (accepted) turn carries.
    dispatched_cid = bus.sent[-1].context["cid"]
    assert started == [("1", dispatched_cid)]


async def test_dispatch_exhaustion_starts_no_lease():
    ch = _channel()
    bus = _Bus(["no_target", "no_target", "no_target"])
    ch._bus = bus
    started = []
    ch._start_typing = lambda chat, lease, **k: started.append((chat, lease))

    ok = await ch._dispatch_button_continuation(
        chat_id=1, user_id=2, target_role="ellen",
        request_id="req-1", text="approve",
        _sleep=lambda _d: asyncio.sleep(0),
    )

    assert ok is False
    assert started == []


async def test_dispatch_cancel_starts_no_lease():
    ch = _channel()
    bus = _Bus([asyncio.CancelledError()])
    ch._bus = bus
    started = []
    ch._start_typing = lambda chat, lease, **k: started.append((chat, lease))

    with pytest.raises(asyncio.CancelledError):
        await ch._dispatch_button_continuation(
            chat_id=1, user_id=2, target_role="ellen",
            request_id="req-1", text="approve",
        )

    assert started == []


async def test_dispatch_reuses_one_cid_across_retries():
    """The cid must be stable across retries so the lease id (started after a
    late acceptance) still equals the accepted turn's cid."""
    ch = _channel()
    bus = _Bus(["no_target", "accepted"])
    ch._bus = bus
    started = []
    ch._start_typing = lambda chat, lease, **k: started.append((chat, lease))

    ok = await ch._dispatch_button_continuation(
        chat_id=1, user_id=2, target_role="ellen",
        request_id="req-1", text="approve",
        _sleep=lambda _d: asyncio.sleep(0),
    )

    assert ok is True
    cids = {m.context["cid"] for m in bus.sent}
    assert len(cids) == 1  # same cid on both attempts
    assert started == [("1", bus.sent[-1].context["cid"])]
