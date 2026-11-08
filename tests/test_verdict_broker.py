"""Tests for verdict_broker — Casa-owned async request/answer registry.

Design ref: docs/current-state-spec.md §W5 broker (Sol B1/B2/B3, r2-B1..r10-B3).
"""

from __future__ import annotations

import asyncio
import pytest
from broker_helpers import deliver
import verdict_broker
from verdict_broker import VerdictBroker

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


async def test_register_deliver_await_roundtrip():
    b = VerdictBroker()
    req, created = b.register(namespace="resident_ask", scope="e1",
                              request_id="r1", timeout_s=5, meta={"operator_id": 42})
    assert created is True
    assert deliver(b, namespace="resident_ask", scope="e1",
                     request_id="r1", option_index=2, actor_id=42) == "delivered"
    assert await b.await_result(req) == {
        "outcome": "answered", "option_index": 2, "actor_id": 42}


async def test_reattach_same_live_key_returns_existing_not_created():
    b = VerdictBroker()
    r1, c1 = b.register(namespace="resident_ask", scope="e1",
                        request_id="r1", timeout_s=5, meta={"operator_id": 7})
    r2, c2 = b.register(namespace="resident_ask", scope="e1",
                        request_id="r1", timeout_s=5, meta={"operator_id": 7})
    assert c1 is True and c2 is False and r1 is r2


async def test_reattach_after_completion_reads_tombstone():
    """B2: a retry whose HTTP response was lost still gets the real answer."""
    b = VerdictBroker()
    req, _ = b.register(namespace="resident_ask", scope="e1",
                        request_id="r1", timeout_s=5, meta={"operator_id": 7})
    deliver(b, namespace="resident_ask", scope="e1", request_id="r1",
              option_index=1, actor_id=7)
    await b.await_result(req)                       # completes → retired
    req2, created = b.register(namespace="resident_ask", scope="e1",
                               request_id="r1", timeout_s=5, meta={"operator_id": 1})
    assert created is False
    assert (await b.await_result(req2))["option_index"] == 1


async def test_multiple_same_id_reattach_through_window(monkeypatch):
    """r2-B1: EVERY same-id retry within the retirement window reattaches to
    the real answer (tombstone READ, never deleted); duplicate taps stay
    'duplicate' the whole window; a genuinely fresh DIFFERENT key is untouched
    by the old retire timer."""
    monkeypatch.setattr(verdict_broker, "_RETIRE_S", 0.2)
    b = VerdictBroker()
    r1, c1 = b.register(namespace="proposal", scope="e1",
                        request_id="r1", timeout_s=5, meta={"operator_id": 1})
    assert c1 is True
    deliver(b, namespace="proposal", scope="e1", request_id="r1",
              option_index=0, actor_id=1)                    # retire r1
    await b.await_result(r1)
    for _ in range(3):                                       # 3 reattaches
        rr, created = b.register(namespace="proposal", scope="e1",
                                 request_id="r1", timeout_s=5, meta={"operator_id": 1})
        assert created is False
        assert (await b.await_result(rr))["option_index"] == 0
        assert deliver(b, namespace="proposal", scope="e1", request_id="r1",
                         option_index=9, actor_id=1) == "duplicate"
        await asyncio.sleep(0.03)
    r3, c3 = b.register(namespace="proposal", scope="e1",
                        request_id="r3", timeout_s=5, meta={"operator_id": 1})         # fresh distinct key
    assert c3 is True
    await asyncio.sleep(0.25)                                # r1 tombstone expires
    assert deliver(b, namespace="proposal", scope="e1", request_id="r3",
                     option_index=1, actor_id=1) == "delivered"   # r3 untouched
    assert deliver(b, namespace="proposal", scope="e1", request_id="r1",
                     option_index=0, actor_id=1) == "stale"       # r1 now gone


async def test_duplicate_then_stale():
    b = VerdictBroker()
    req, _ = b.register(namespace="resident_ask", scope="e1",
                        request_id="r1", timeout_s=5, meta={"operator_id": 1})
    assert deliver(b, namespace="resident_ask", scope="e1", request_id="r1",
                     option_index=0, actor_id=1) == "delivered"
    assert deliver(b, namespace="resident_ask", scope="e1", request_id="r1",
                     option_index=1, actor_id=1) == "duplicate"


async def test_stale_after_retirement(monkeypatch):
    monkeypatch.setattr(verdict_broker, "_RETIRE_S", 0)
    b = VerdictBroker()
    req, _ = b.register(namespace="resident_ask", scope="e1",
                        request_id="r1", timeout_s=5, meta={"operator_id": 1})
    deliver(b, namespace="resident_ask", scope="e1", request_id="r1",
              option_index=0, actor_id=1)
    await b.await_result(req)
    await asyncio.sleep(0)                            # let retirement pop run
    assert deliver(b, namespace="resident_ask", scope="e1", request_id="r1",
                     option_index=0, actor_id=1) == "stale"


async def test_unknown_key_is_stale():
    b = VerdictBroker()
    assert deliver(b, namespace="resident_ask", scope="e1",
                     request_id="ghost", option_index=0, actor_id=1) == "stale"


async def test_timeout_no_answer_and_gone_from_pending():
    b = VerdictBroker()
    req, _ = b.register(namespace="resident_ask", scope="e1",
                        request_id="r1", timeout_s=0.05, meta={"operator_id": 7})
    assert b.pending(namespace="resident_ask", scope="e1") == ["r1"]
    assert await b.await_result(req) == {"outcome": "no_answer"}
    assert b.pending(namespace="resident_ask", scope="e1") == []


async def test_no_theft_across_namespaces_same_scope():
    b = VerdictBroker()
    prop, _ = b.register(namespace="proposal", scope="e1",
                         request_id="p1", timeout_s=5, meta={"operator_id": 7})
    ask, _ = b.register(namespace="resident_ask", scope="e1",
                        request_id="a1", timeout_s=5, meta={"operator_id": 7})
    deliver(b, namespace="resident_ask", scope="e1", request_id="a1",
              option_index=1, actor_id=7)
    deliver(b, namespace="proposal", scope="e1", request_id="p1",
              option_index=0, actor_id=7)
    assert (await b.await_result(ask))["option_index"] == 1
    assert (await b.await_result(prop))["option_index"] == 0


async def test_two_asks_out_of_order_same_namespace():
    """S1: both under one namespace, distinct rids."""
    b = VerdictBroker()
    r1, _ = b.register(namespace="resident_ask", scope="e1",
                       request_id="r1", timeout_s=5, meta={"operator_id": 1})
    r2, _ = b.register(namespace="resident_ask", scope="e1",
                       request_id="r2", timeout_s=5, meta={"operator_id": 1})
    deliver(b, namespace="resident_ask", scope="e1", request_id="r2",
              option_index=1, actor_id=1)
    deliver(b, namespace="resident_ask", scope="e1", request_id="r1",
              option_index=0, actor_id=1)
    assert (await b.await_result(r2))["option_index"] == 1
    assert (await b.await_result(r1))["option_index"] == 0


async def test_cancel_scope_and_supersede():
    b = VerdictBroker()
    r1, _ = b.register(namespace="resident_ask", scope="c1",
                       request_id="r1", timeout_s=5, detached=True, meta={"operator_id": 9})
    r2, created = b.register(namespace="resident_ask", scope="c1",
                             request_id="r2", timeout_s=5, detached=True,
                             supersede=True, meta={"operator_id": 9})
    assert created is True
    assert (await b.await_result(r1))["outcome"] == "cancelled"     # superseded
    assert b.cancel_scope(namespace="resident_ask", scope="c1",
                          reason="new") == 1
    assert (await b.await_result(r2))["outcome"] == "cancelled"


async def test_logical_cancel():
    b = VerdictBroker()
    req, _ = b.register(namespace="proposal", scope="e1",
                        request_id="r1", timeout_s=5, meta={"operator_id": 9})
    assert b.cancel(namespace="proposal", scope="e1", request_id="r1",
                    reason="caller_cancelled") is True
    assert (await b.await_result(req))["outcome"] == "cancelled"


async def test_cancel_all_then_drain_hooks(monkeypatch):
    """r4-B1/B3: cancel_all resolves every live request 'cancelled'; drain_hooks
    awaits any finish-hook tasks so a keyboard edit can't race a torn-down
    channel (casa_core calls cancel_all + await drain_hooks before stop_all)."""
    b = VerdictBroker()
    edited = []
    req, _ = b.register(namespace="proposal", scope="e1",
                        request_id="r1", timeout_s=60, meta={"operator_id": 9})
    async def _hook(outcome):
        edited.append(outcome["outcome"])
    b.set_finish_hook(req, _hook)
    b.cancel_all(reason="casa_shutdown")
    assert (await b.await_result(req))["outcome"] == "cancelled"
    await b.drain_hooks()
    assert edited == ["cancelled"]


async def test_detached_expires_on_ttl():
    b = VerdictBroker()
    b.register(namespace="resident_ask", scope="c1", request_id="r1",
               timeout_s=0.05, detached=True, meta={"operator_id": 9})
    assert b.pending(namespace="resident_ask", scope="c1") == ["r1"]
    await asyncio.sleep(0.12)
    assert b.pending(namespace="resident_ask", scope="c1") == []


async def test_shield_survives_awaiter_cancellation_then_delivers():
    """r3-B1: cancelling a task awaiting the request must NOT cancel the
    shared future — a reattach still gets the answer."""
    b = VerdictBroker()
    req, _ = b.register(namespace="resident_ask", scope="e1",
                        request_id="r1", timeout_s=5, meta={"operator_id": 9})
    waiter = asyncio.create_task(b.await_result(req))
    await asyncio.sleep(0)
    waiter.cancel()                                  # simulate HTTP disconnect
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert b.pending(namespace="resident_ask", scope="e1") == ["r1"]  # live
    deliver(b, namespace="resident_ask", scope="e1", request_id="r1",
              option_index=1, actor_id=9)
    req2, created = b.register(namespace="resident_ask", scope="e1",
                               request_id="r1", timeout_s=5, meta={"operator_id": 1})
    assert created is False
    assert (await b.await_result(req2))["option_index"] == 1


async def test_retired_timeout_tap_is_stale_not_duplicate(monkeypatch):
    """r3-B2: a tap on a TIMED-OUT keyboard gets stale/expired UX."""
    monkeypatch.setattr(verdict_broker, "_RETIRE_S", 5)
    b = VerdictBroker()
    req, _ = b.register(namespace="resident_ask", scope="e1",
                        request_id="r1", timeout_s=0.05, meta={"operator_id": 1})
    await b.await_result(req)                         # no_answer → retired
    assert deliver(b, namespace="resident_ask", scope="e1", request_id="r1",
                     option_index=0, actor_id=1) == "stale"


async def test_get_meta_available_live_and_retired(monkeypatch):
    monkeypatch.setattr(verdict_broker, "_RETIRE_S", 5)
    b = VerdictBroker()
    req, _ = b.register(namespace="resident_ask", scope="e1",
                        request_id="r1", timeout_s=5, meta={"options": ["A", "B"]})
    assert b.get_meta(namespace="resident_ask", scope="e1",
                      request_id="r1") == {"options": ["A", "B"]}
    deliver(b, namespace="resident_ask", scope="e1", request_id="r1",
              option_index=0, actor_id=1)
    await b.await_result(req)                         # retired, meta retained
    assert b.get_meta(namespace="resident_ask", scope="e1",
                      request_id="r1") == {"options": ["A", "B"]}


async def test_unregister_resolves_concurrent_reattacher_and_reregisters_fresh():
    """r3-B2/r4-B2: keyboard-post failure unregisters — a concurrent reattached
    waiter is resolved delivery_failed (never stranded), no tombstone remains,
    and a later same-id register is genuinely fresh (created=True)."""
    b = VerdictBroker()
    req, _ = b.register(namespace="resident_ask", scope="e1", request_id="r1",
                        timeout_s=5, meta={"operator_id": 1})
    # a second handler reattaches and is already awaiting the shared future
    req2, created2 = b.register(namespace="resident_ask", scope="e1",
                                request_id="r1", timeout_s=5, meta={"operator_id": 1})
    assert created2 is False
    waiter = asyncio.create_task(b.await_result(req2))
    await asyncio.sleep(0)
    b.unregister(namespace="resident_ask", scope="e1", request_id="r1")
    assert (await waiter)["outcome"] == "delivery_failed"        # not stranded
    assert (await b.await_result(req))["outcome"] == "delivery_failed"
    assert b.pending(namespace="resident_ask", scope="e1") == []
    _, created = b.register(namespace="resident_ask", scope="e1",
                            request_id="r1", timeout_s=5, meta={"operator_id": 1})
    assert created is True                                        # no tombstone


async def test_set_finish_hook_fires_when_request_already_completed():
    """r4-B1: fast tap — the request completes DURING the post await, so the
    creator installs the hook after _finish already ran. set_finish_hook must
    detect the done future and fire the hook itself, exactly once."""
    b = VerdictBroker()
    seen = []
    req, _ = b.register(namespace="resident_ask", scope="e1",
                        request_id="r1", timeout_s=5, meta={"operator_id": 1})
    deliver(b, namespace="resident_ask", scope="e1", request_id="r1",
              option_index=0, actor_id=1)                        # completes first
    async def _hook(outcome):
        seen.append(outcome)
    b.set_finish_hook(req, _hook)                                # installed after
    await b.drain_hooks()
    assert len(seen) == 1 and seen[0]["outcome"] == "answered"


async def test_claim_does_not_resolve_until_commit_and_stale_after_retire(monkeypatch):
    """r5-B1: claim reserves the LIVE winner WITHOUT resolving the future; only
    commit resolves it. A second claim while claimed → 'duplicate'. A claim on a
    retired (timed-out) request → 'stale' with NO effect (so a late tap never
    runs the callback's side effect).

    Pins INV-TG-003. Red case demonstrated: removing claim()'s duplicate guard fails this test.
    """
    monkeypatch.setattr(verdict_broker, "_RETIRE_S", 5)
    b = VerdictBroker()
    req, _ = b.register(namespace="resident_ask", scope="e1",
                        request_id="r1", timeout_s=5, meta={"operator_id": 7})
    claim = b.claim(namespace="resident_ask", scope="e1", request_id="r1",
                    option_index=1, actor_id=7)
    assert not isinstance(claim, str)                 # won the live request
    assert not req._future.done()                     # NOT resolved yet
    assert b.claim(namespace="resident_ask", scope="e1", request_id="r1",
                   option_index=0, actor_id=7) == "duplicate"   # concurrent tap
    assert b.commit(claim) is True
    assert (await b.await_result(req))["option_index"] == 1
    assert b.commit(claim) is False                   # r6-B1: double commit → no-op
    # a fresh timed-out request → claim is stale, no side effect
    req2, _ = b.register(namespace="resident_ask", scope="e2",
                         request_id="r2", timeout_s=0.05, meta={"operator_id": 7})
    await b.await_result(req2)                         # no_answer → retired
    assert b.claim(namespace="resident_ask", scope="e2", request_id="r2",
                   option_index=0, actor_id=7) == "stale"


async def test_claim_stops_timer_and_commit_false_after_teardown(monkeypatch):
    """r6-B1: claim cancels the timeout timer (no no_answer can race the window),
    and a cancel_scope BETWEEN claim and commit makes commit return False (the tap
    is moot) instead of double-finishing."""
    monkeypatch.setattr(verdict_broker, "_RETIRE_S", 5)
    b = VerdictBroker()
    req, _ = b.register(namespace="resident_ask", scope="e1",
                        request_id="r1", timeout_s=0.05, meta={"operator_id": 7})   # short timeout
    claim = b.claim(namespace="resident_ask", scope="e1", request_id="r1",
                    option_index=0, actor_id=7)
    assert not isinstance(claim, str)
    await asyncio.sleep(0.12)                          # would have timed out — timer stopped
    assert not req._future.done()                      # still live (timer was cancelled)
    b.cancel_scope(namespace="resident_ask", scope="e1", reason="engagement_terminal")
    assert req._future.done()                          # torn down between claim and commit
    assert b.commit(claim) is False                    # moot — no double-finish
    assert (await b.await_result(req))["outcome"] == "cancelled"


async def test_abort_claim_rearm_preserves_absolute_deadline():
    """r8-B1: abort_claim re-arms only the REMAINING time to the original
    deadline — never a fresh full timeout_s — and finishes no_answer
    immediately if the deadline already passed during the claim window."""
    b = VerdictBroker()
    req, _ = b.register(namespace="resident_ask", scope="e1",
                        request_id="r1", timeout_s=0.2, meta={"operator_id": 7})
    claim = b.claim(namespace="resident_ask", scope="e1", request_id="r1",
                    option_index=0, actor_id=7)
    await asyncio.sleep(0.1)                           # burn half the budget claimed
    b.abort_claim(claim)                               # re-arm ~0.1s, NOT 0.2s
    await asyncio.sleep(0.15)                          # past the ORIGINAL deadline
    # r9-B3: assert done() BEFORE awaiting — a buggy full-timeout re-arm would
    # still be pending here (its fresh 0.2s hasn't elapsed) and merely delay the
    # await below into a false green.
    assert req._future.done()
    assert (await b.await_result(req))["outcome"] == "no_answer"
    # near-deadline: claim, hold past the deadline, abort → IMMEDIATE no_answer
    req2, _ = b.register(namespace="resident_ask", scope="e2",
                         request_id="r2", timeout_s=0.05, meta={"operator_id": 7})
    claim2 = b.claim(namespace="resident_ask", scope="e2", request_id="r2",
                     option_index=0, actor_id=7)
    await asyncio.sleep(0.1)                           # deadline passed while claimed
    b.abort_claim(claim2)
    assert req2._future.done()                         # finished no_answer at once
    assert (await b.await_result(req2))["outcome"] == "no_answer"


async def test_drain_awaits_setup_tasks_then_hooks():
    """r9-B1: cancel_scope completes a request while its keyboard post is still
    BLOCKED in the setup task → drain_hooks must first await the setup task
    (which installs the finish hook on completion, r4-B1 fires-when-done), then
    the hook — so the edit lands BEFORE the caller closes the topic."""
    b = VerdictBroker()
    posted, edited = [], []
    gate = asyncio.Event()
    async def _post():
        await gate.wait(); posted.append(1); return 42   # blocked post
    def _finish(mid):
        async def _hook(outcome): edited.append(outcome["outcome"])
        return _hook
    req, _ = b.register(namespace="resident_ask", scope="e1",
                        request_id="r1", timeout_s=60, meta={"operator_id": 1})
    ensure = asyncio.create_task(b.ensure_posted(req, _post, _finish))
    await asyncio.sleep(0)
    b.cancel_scope(namespace="resident_ask", scope="e1", reason="engagement_terminal")
    gate.set()                                          # unblock the post NOW
    await b.drain_hooks()                               # setup → hook, looped
    assert posted == [1] and edited == ["cancelled"]    # hook flushed before return
    ensure.cancel()


async def test_drain_isolates_raising_hook_and_waits_blocked_one():
    """r10-B2: one hook RAISES while another is still blocked — drain must
    consume the exception (log, not propagate) and keep waiting until the
    blocked hook completes; both requests' hooks observed."""
    b = VerdictBroker()
    done, gate = [], asyncio.Event()
    r1, _ = b.register(namespace="resident_ask", scope="e1",
                       request_id="a", timeout_s=60, meta={"operator_id": 1})
    r2, _ = b.register(namespace="resident_ask", scope="e1",
                       request_id="b", timeout_s=60, meta={"operator_id": 1})
    async def _boom(outcome): raise RuntimeError("edit failed")
    async def _slow(outcome):
        await gate.wait(); done.append("slow")
    b.set_finish_hook(r1, _boom)
    b.set_finish_hook(r2, _slow)
    b.cancel_scope(namespace="resident_ask", scope="e1", reason="terminal")
    drain = asyncio.create_task(b.drain_hooks())
    await asyncio.sleep(0.05)
    assert not drain.done()                             # still waiting on _slow
    gate.set()
    await drain                                         # returns, no exception raised
    assert done == ["slow"]


async def test_drain_hooks_terminates_with_done_undiscarded_task():
    """3.12+ hard-livelock guard: a task that is ALREADY DONE but whose
    ``discard`` done-callback has not yet run must be sync-discarded from the
    set BEFORE gathering. On Python 3.12+, ``await asyncio.gather(*done_tasks)``
    completes EAGERLY without yielding to the loop, so the queued discard
    callback never runs, the set never empties, and the old ``while
    self._setup_tasks: await gather(...)`` loop tight-spins forever — starving
    the whole event loop (even same-loop ``wait_for`` timers never fire).

    HANG SAFETY: because the regression is a same-loop livelock, an
    ``asyncio.wait_for`` here would itself be starved and could NOT bound the
    test. Instead the drain scenario runs in a dedicated thread with its own
    event loop via ``asyncio.run``, and we ``join(timeout=10)`` + assert the
    thread finished.
    """
    import threading

    def _run() -> None:
        async def _scenario() -> None:
            b = VerdictBroker()

            async def _noop() -> None:
                return None

            t = asyncio.ensure_future(_noop())
            await asyncio.sleep(0)          # let it actually finish
            assert t.done()
            # Reconstruct the race: the task is done, it lives in the set, and
            # its discard callback is QUEUED (add_done_callback on an
            # already-done future schedules via call_soon — it does NOT run
            # synchronously) but has not yet executed.
            b._setup_tasks.add(t)
            t.add_done_callback(b._setup_tasks.discard)
            assert t in b._setup_tasks       # discard still pending
            await b.drain_hooks()            # must RETURN, not livelock

        asyncio.run(_scenario())

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout=10)
    assert not th.is_alive(), (
        "drain_hooks livelocked on a done-but-undiscarded task "
        "(3.12+ gather eager-completion)"
    )


async def test_retired_reattach_never_reposts(monkeypatch):
    """r9-B2: after answered/no_answer/cancelled retirement, a same-id retry gets
    a pre-resolved tombstone request; ensure_posted must NO-OP (no second
    keyboard)."""
    monkeypatch.setattr(verdict_broker, "_RETIRE_S", 5)
    b = VerdictBroker()
    posts = []
    async def _post(): posts.append(1); return 42
    def _finish(mid):
        async def _hook(outcome): pass
        return _hook
    for i, finisher in enumerate((
        lambda ns, sc, rid: deliver(b, namespace=ns, scope=sc, request_id=rid,
                                      option_index=0, actor_id=1),   # answered
        lambda ns, sc, rid: None,                                    # no_answer (timeout)
        lambda ns, sc, rid: b.cancel(namespace=ns, scope=sc, request_id=rid,
                                     reason="x"),                    # cancelled
    )):
        sc = f"e{i}"
        req, created = b.register(namespace="resident_ask", scope=sc,
                                  request_id="r", timeout_s=0.05, meta={"operator_id": 1})
        assert created
        await b.ensure_posted(req, _post, _finish)
        finisher("resident_ask", sc, "r")
        await b.await_result(req)                       # retire (answer/timeout/cancel)
        req2, created2 = b.register(namespace="resident_ask", scope=sc,
                                    request_id="r", timeout_s=0.05, meta={"operator_id": 1})
        assert created2 is False                        # tombstone reattach
        await b.ensure_posted(req2, _post, _finish)     # must NO-OP
    assert posts == [1, 1, 1]                           # exactly one post per ask


async def test_finish_hook_fires_once_even_if_awaiter_cancelled():
    """r3-B3: the broker-owned edit hook fires on completion regardless of
    whether any handler is still awaiting."""
    b = VerdictBroker()
    seen = []
    req, _ = b.register(namespace="resident_ask", scope="e1",
                        request_id="r1", timeout_s=5, meta={"operator_id": 1})
    async def _hook(outcome):
        seen.append(outcome)
    b.set_finish_hook(req, _hook)
    waiter = asyncio.create_task(b.await_result(req))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    deliver(b, namespace="resident_ask", scope="e1", request_id="r1",
              option_index=0, actor_id=1)
    await asyncio.sleep(0)                            # let the hook task run
    assert len(seen) == 1 and seen[0]["outcome"] == "answered"


# ---------------------------------------------------------------------------
# #469 — fail-closed actor binding: the broker is the enforcement home
# ---------------------------------------------------------------------------


async def test_deliver_removed_from_production_surface():
    """#469 pinning: the one-shot deliver() sugar is gone — its only caller
    was the removed /internal/channel/permission_verdict route. Resolution
    goes through claim()/commit(), which enforce the actor binding."""
    assert not hasattr(VerdictBroker, "deliver")


async def test_claim_forbidden_on_actor_mismatch():
    b = VerdictBroker()
    req, _ = b.register(namespace="proposal", scope="e1", request_id="r1",
                        timeout_s=5, meta={"operator_id": 42})
    assert b.claim(namespace="proposal", scope="e1", request_id="r1",
                   option_index=0, actor_id=43) == "forbidden"
    assert not req._future.done()  # still live, still awaitable
    assert b.pending(namespace="proposal", scope="e1") == ["r1"]


async def test_claim_forbidden_when_no_operator_bound():
    """Absence is not consent: a request registered WITHOUT an operator_id
    binding (or with None — the deliberate nobody-may-answer binding) is
    claimable by NO actor."""
    b = VerdictBroker()
    b.register(namespace="proposal", scope="e1", request_id="r1",
               timeout_s=5)
    assert b.claim(namespace="proposal", scope="e1", request_id="r1",
                   option_index=0, actor_id=42) == "forbidden"
    b.register(namespace="proposal", scope="e1", request_id="r2",
               timeout_s=5, meta={"operator_id": None})
    assert b.claim(namespace="proposal", scope="e1", request_id="r2",
                   option_index=0, actor_id=42) == "forbidden"


async def test_claim_forbidden_on_none_or_bool_actor():
    """None never matches; bool is an int subclass (True == 1) and must not
    impersonate user id 1."""
    b = VerdictBroker()
    b.register(namespace="proposal", scope="e1", request_id="r1",
               timeout_s=5, meta={"operator_id": 1})
    assert b.claim(namespace="proposal", scope="e1", request_id="r1",
                   option_index=0, actor_id=None) == "forbidden"
    assert b.claim(namespace="proposal", scope="e1", request_id="r1",
                   option_index=0, actor_id=True) == "forbidden"
    b.register(namespace="proposal", scope="e1", request_id="r2",
               timeout_s=5, meta={"operator_id": True})
    assert b.claim(namespace="proposal", scope="e1", request_id="r2",
                   option_index=0, actor_id=1) == "forbidden"


async def test_claim_forbidden_checked_before_duplicate():
    """A forbidden actor learns nothing about claim state — mismatch reports
    'forbidden' whether or not a winning claim exists."""
    b = VerdictBroker()
    b.register(namespace="proposal", scope="e1", request_id="r1",
               timeout_s=5, meta={"operator_id": 42})
    winning = b.claim(namespace="proposal", scope="e1", request_id="r1",
                      option_index=0, actor_id=42)
    assert not isinstance(winning, str)
    assert b.claim(namespace="proposal", scope="e1", request_id="r1",
                   option_index=0, actor_id=43) == "forbidden"
    assert b.commit(winning) is True


async def test_retired_outcome_needs_no_actor_recheck(monkeypatch):
    """A tombstone claim reports duplicate/stale exactly as before — the
    outcome was already authorized when it was committed; a late tap never
    re-authorizes anything."""
    b = VerdictBroker()
    b.register(namespace="proposal", scope="e1", request_id="r1",
               timeout_s=5, meta={"operator_id": 42})
    assert deliver(b, namespace="proposal", scope="e1", request_id="r1",
                   option_index=0, actor_id=42) == "delivered"
    # Wrong actor on the retired key: 'duplicate', not 'forbidden' — no
    # state change either way, and no live request to protect.
    assert b.claim(namespace="proposal", scope="e1", request_id="r1",
                   option_index=0, actor_id=43) == "duplicate"


async def test_bound_actor_roundtrip_still_works():
    b = VerdictBroker()
    req, _ = b.register(namespace="resident_ask", scope="e1",
                        request_id="r1", timeout_s=5,
                        meta={"operator_id": 42, "options": ["A", "B"]})
    assert deliver(b, namespace="resident_ask", scope="e1",
                   request_id="r1", option_index=1, actor_id=42) == "delivered"
    assert (await b.await_result(req))["option_index"] == 1
