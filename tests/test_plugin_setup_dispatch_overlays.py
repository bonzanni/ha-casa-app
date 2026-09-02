"""#803 — INV-PLUG-016: a released setup obligation is dispatched only from a
read, with no yield between it and the bus send, in which neither applied
plugin routing overlay carries the unavailable marker and no overlay
publication has landed since the route recomputation began.

Every test here drives the REAL episode worker, the REAL route gate
``casa_core._callback_and_trigger_routes_live`` and a REAL ``TriggerRegistry``
installed on ``agent.active_runtime`` — a registry handed only to a reconciler
is not the one the seam reads. Both reconcilers' ``issue_state`` are doubled
healthy-and-observing (as ``TestI1RoutesLiveGate`` does) so the recompute is
green and the applied overlays and their publication generation are the only
variables. Named tests only: a parametrised node id in the manifest's
flow-style list breaks the shard.
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from broker_helpers import wait_until

CASA = Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"

PLUGIN = "gmail"
ENTRY = {
    "artifact_id": "art-1",
    "setup_tool": "setup_gmail",
    "granted_tools": ["gmailsrv"],
    "targets": ["resident:assistant"],
}
ROLE_CONFIGS = {
    "assistant": SimpleNamespace(channels=["webhook", "telegram"]),
}
SENTINEL_ERROR = "waiting for plugin routing to be published"
REPUBLISHED_ERROR = "plugin routing was republished during the dispatch check"


def _registry():
    from trigger_registry import TriggerRegistry

    return TriggerRegistry(scheduler=None, app=None, bus=None)


def _resolver(*, boom: bool = False):
    def resolve(target=None):
        if boom:
            raise RuntimeError("callback resolver exploded")
        return SimpleNamespace(registry_valid=True, plugins=[], issues=[])
    return resolve


class Harness:
    """One released obligation, one real registry, counters for everything
    the assertions count. ``rebuild`` starts a fresh store + registry (rows of
    a matrix must not share an episode)."""

    def __init__(self, tmp_path: Path, monkeypatch, *, name: str = "h"):
        self.tmp_path = tmp_path
        self.mp = monkeypatch
        self.name = name
        self.dispatches: list = []
        self.dispatch_results: list[bool] = []
        self.sleeps: list[float] = []
        self.trigger_calls = 0
        self.callback_calls = 0
        self.on_sleep = None            # async callable run inside _sleep
        self.block_trigger: tuple | None = None   # (entered, release) events
        self._install_modules()

    # -- module-level doubles shared by every row -------------------------
    def _install_modules(self):
        import callback_reconcile as cr
        import event_reconcile
        import plugin_registry
        import trigger_reconcile as tr
        from callback_acks import CallbackAckStore
        from trigger_acks import TriggerAckStore

        d = self.tmp_path / self.name
        d.mkdir(parents=True, exist_ok=True)
        self.dir = d
        self.mp.setattr(tr, "SECRETS_DIR", d / "secrets")
        self.mp.setattr(tr, "_default_acks",
                        lambda: TriggerAckStore(path=d / "tacks.json"))
        self.mp.setattr(cr, "_default_acks",
                        lambda: CallbackAckStore(path=d / "cacks.json"))
        self.mp.setattr(cr, "_default_spool", lambda: None)
        self.mp.setattr(cr, "_default_entries", lambda: (lambda: []))
        self.mp.setattr(tr, "_default_resolver", lambda: _resolver())
        self.mp.setattr(cr, "_default_resolver", lambda: _resolver())
        self.mp.setattr(tr, "_default_global_secret_ok",
                        lambda: (lambda: True))
        self.mp.setattr(plugin_registry, "resolve_all",
                        lambda: SimpleNamespace(issues=[], warnings=[]))
        self.mp.setattr(plugin_registry, "load_registry",
                        lambda *a, **k: SimpleNamespace(
                            raw={}, valid=False, entries=[]))
        self.mp.setattr(event_reconcile, "current_issues", lambda: [])

        def _fake_pin():
            def _resolve(target=None):
                return SimpleNamespace(registry_valid=True, plugins=[],
                                       issues=[], generation=1)
            _resolve.generation = 1
            _resolve.entries = lambda: []
            return _resolve
        self.mp.setattr(plugin_registry, "pinned_resolver", _fake_pin)

        h = self

        def _trigger_state(resolver=None):
            h.trigger_calls += 1
            if h.block_trigger is not None:
                entered, release = h.block_trigger
                entered.set()
                assert release.wait(timeout=20), "gate thread never released"
            return tr.IssueState(True, [], {PLUGIN, "other"})

        def _callback_state(resolver=None):
            h.callback_calls += 1
            return cr.IssueState(True, [], {PLUGIN, "other"})
        self.mp.setattr(tr, "issue_state", _trigger_state)
        self.mp.setattr(cr, "issue_state", _callback_state)
        self.trigger_state = _trigger_state
        self.callback_state = _callback_state

    # -- per-row state -----------------------------------------------------
    def rebuild(self, *, registry=True, applied=True, routes_live=None):
        import agent as agent_mod
        import casa_core
        import plugin_setup_episodes as pse

        self.dispatches.clear()
        self.sleeps.clear()
        self.reg = _registry() if registry else None
        runtime = SimpleNamespace(role_configs=ROLE_CONFIGS,
                                  channel_manager=None)
        if registry:
            runtime.trigger_registry = self.reg
        self.mp.setattr(agent_mod, "active_runtime", runtime, raising=False)
        store = self.dir / f"episodes-{len(self.dispatch_results)}-{id(runtime)}.json"
        self.mp.setattr(pse, "STORE_PATH", store)
        self.mp.setattr(pse, "_lock", None)
        self.mp.setattr(pse, "_kick", None)
        self.mp.setattr(pse, "_retry_task", None)
        self.mp.setattr(pse, "_worker_task", None)
        h = self

        async def _dispatch(role, instruction, ctx):
            h.dispatches.append(role)
            if h.dispatch_results:
                return h.dispatch_results.pop(0)
            return True

        async def _notify(text):
            pass

        async def _sleep(delay):
            h.sleeps.append(delay)
            if h.on_sleep is not None:
                await h.on_sleep(delay)

        kw = {}
        if applied and "applied_routing" in inspect.signature(
                pse.configure).parameters:
            kw["applied_routing"] = casa_core._applied_plugin_routing
        pse.configure(
            dispatch=_dispatch, notify_operator=_notify,
            resolve_registry_entry=lambda p: dict(ENTRY),
            ack_lookup=lambda ident: None,
            routes_live=(routes_live if routes_live is not None
                         else casa_core._callback_and_trigger_routes_live),
            sleep=_sleep, **kw)
        return self

    async def release_obligation(self):
        import plugin_setup_episodes as pse

        pse.ensure_obligation(plugin=PLUGIN, artifact_id="art-1")
        pse.open_round(plugin=PLUGIN, artifact_id="art-1", identities=[])
        await pse._recover_and_settle()
        assert pse.episodes()[0]["gate"] == "released"

    def live(self):
        self.reg.replace_plugin_overlay({})
        self.reg.replace_callback_overlay({})

    def row(self) -> dict:
        import plugin_setup_episodes as pse

        rows = pse.episodes()
        assert len(rows) == 1
        return rows[0]

    def pending_released(self) -> int:
        return sum(1 for e in __import__("plugin_setup_episodes").episodes()
                   if e["status"] == "pending" and e["gate"] == "released")

    async def publish_callback_sentinel(self):
        import callback_reconcile as cr
        from callback_acks import CallbackAckStore

        with pytest.raises(RuntimeError, match="callback resolver exploded"):
            await cr.reconcile_plugin_callbacks(
                trigger_registry=self.reg, role_configs=ROLE_CONFIGS,
                acks=CallbackAckStore(path=self.dir / "cacks.json"),
                spool=None, resolver=_resolver(boom=True),
                entries=lambda: [], prompt=False)
        assert self.reg.callback_overlay_unavailable() is True

    async def publish_callback_live(self):
        import callback_reconcile as cr
        from callback_acks import CallbackAckStore

        await cr.reconcile_plugin_callbacks(
            trigger_registry=self.reg, role_configs=ROLE_CONFIGS,
            acks=CallbackAckStore(path=self.dir / "cacks.json"),
            spool=None, resolver=_resolver(), entries=lambda: [],
            prompt=False)
        assert self.reg.callback_overlay_unavailable() is False

    async def raced_pass(self, publish):
        """Run one worker pass with the gate's recompute BLOCKED inside its
        thread; run ``publish`` on the loop while it is blocked; release."""
        import plugin_setup_episodes as pse

        entered, release = threading.Event(), threading.Event()
        self.block_trigger = (entered, release)
        try:
            task = asyncio.create_task(pse._worker_pass())
            await wait_until(entered.is_set)
            await publish()
            release.set()
            return await task
        finally:
            self.block_trigger = None


# ---------------------------------------------------------------------------
# 1. A standing sentinel in either overlay defers before the recompute runs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_standing_sentinel_in_either_overlay_defers_the_dispatch_without_the_recompute(
        tmp_path, monkeypatch):
    """The dossier's probe A, through the real worker: at the base the gate
    answers True in all four applied states (4 dispatches); after the change
    only both-live dispatches, the three sentinel rows defer, and the
    recompute is not paid for a sentinel that already stands."""
    import plugin_setup_episodes as pse
    from trigger_registry import ROUTING_UNAVAILABLE as U

    h = Harness(tmp_path, monkeypatch)
    dispatches, retries, pending, rows = [], [], [], []
    for n, (trig, cb) in enumerate([({}, {}), (U, {}), ({}, U), (U, U)]):
        h.rebuild()
        h.reg.replace_plugin_overlay(trig)
        h.reg.replace_callback_overlay(cb)
        await h.release_obligation()
        retries.append(await pse._worker_pass())
        dispatches.append(len(h.dispatches))
        pending.append(h.pending_released())
        rows.append(h.row())
    assert dispatches == [1, 0, 0, 0]
    assert retries == [False, True, True, True]
    assert (h.trigger_calls, h.callback_calls) == (1, 1)
    assert pending == [0, 1, 1, 1]
    for r in rows[1:]:
        assert int(r.get("attempts") or 0) == 0
        assert int(r.get("resolve_deferrals") or 0) == 0
        assert r["last_error"] == SENTINEL_ERROR


# ---------------------------------------------------------------------------
# 2. No registry bound: the recompute alone decides, exactly as today
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_recompute_path_alone_decides_when_no_registry_is_bound(
        tmp_path, monkeypatch):
    import casa_core
    import plugin_setup_episodes as pse
    import trigger_reconcile as tr

    assert callable(getattr(casa_core, "_applied_plugin_routing", None))

    h = Harness(tmp_path, monkeypatch)
    h.rebuild(registry=False)
    await h.release_obligation()
    assert await pse._worker_pass() is False
    assert len(h.dispatches) == 1
    assert (h.trigger_calls, h.callback_calls) == (1, 1)

    # A doubled trigger issue: the pre-existing HOLD, its string, no retry.
    h.rebuild(registry=False)
    h.trigger_calls = h.callback_calls = 0
    issue = SimpleNamespace(name=PLUGIN, reason_code="trigger_pending_ack",
                            target=None, stage="triggers", artifact_id="art-1")
    monkeypatch.setattr(tr, "issue_state", lambda resolver=None: tr.IssueState(
        True, [issue], {PLUGIN}))
    await h.release_obligation()
    assert await pse._worker_pass() is False
    assert len(h.dispatches) == 0
    assert h.callback_calls == 1
    assert h.pending_released() == 1
    assert h.row()["last_error"] == "waiting for live trigger route"


# ---------------------------------------------------------------------------
# 3 / 4. A sentinel published DURING the recompute is seen before the send;
#        the clearing publication lets the deferred obligation dispatch once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_sentinel_published_during_the_route_recompute_defers_the_dispatch(
        tmp_path, monkeypatch):
    """Probe B as a test: the worker's route gate is blocked inside
    ``asyncio.to_thread``; a REAL callback reconcile with a raising resolver
    publishes ``ROUTING_UNAVAILABLE`` on the loop meanwhile. At the base the
    recompute (which read healthy artifacts) wins and the setup is dispatched
    against a closed endpoint; after the change the send is refused."""
    h = Harness(tmp_path, monkeypatch)
    h.rebuild()
    h.live()
    await h.release_obligation()
    retry = await h.raced_pass(h.publish_callback_sentinel)
    assert len(h.dispatches) == 0
    assert retry is True
    assert h.pending_released() == 1
    r = h.row()
    assert (int(r.get("attempts") or 0), int(r.get("resolve_deferrals") or 0)) == (0, 0)
    assert r["last_error"] == SENTINEL_ERROR


@pytest.mark.asyncio
async def test_the_clearing_publication_lets_the_deferred_obligation_dispatch_exactly_once(
        tmp_path, monkeypatch):
    import plugin_setup_episodes as pse

    h = Harness(tmp_path, monkeypatch)
    h.rebuild()
    h.live()
    await h.release_obligation()
    await h.raced_pass(h.publish_callback_sentinel)
    before = len(h.dispatches)
    pse._kick.clear()
    await h.publish_callback_live()          # a REAL heal: publishes, kicks
    assert pse._kick.is_set()
    assert await pse._worker_pass() is False
    assert (before, len(h.dispatches)) == (0, 1)
    r = h.row()
    assert r["status"] == "dispatched"
    assert int(r.get("attempts") or 0) == 1
    assert r["last_error"] == ""


# ---------------------------------------------------------------------------
# 5. A raising probe defers — at the capture and at the final read
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_raising_registry_probe_defers_and_never_dispatches(
        tmp_path, monkeypatch):
    import plugin_setup_episodes as pse

    h = Harness(tmp_path, monkeypatch)
    dispatches, retries, pending, attempts, recomputes = [], [], [], [], []
    for row in ("capture", "final"):
        h.rebuild()
        h.live()
        h.trigger_calls = h.callback_calls = 0
        calls = {"n": 0}

        def _probe(_row=row):
            calls["n"] += 1
            if _row == "capture" or calls["n"] >= 2:
                raise RuntimeError("probe exploded")
            return False
        monkeypatch.setattr(h.reg, "plugin_overlay_unavailable", _probe)
        await h.release_obligation()
        retries.append(await pse._worker_pass())
        dispatches.append(len(h.dispatches))
        pending.append(h.pending_released())
        attempts.append(int(h.row().get("attempts") or 0))
        recomputes.append(h.trigger_calls + h.callback_calls)
    assert dispatches == [0, 0]
    assert retries == [True, True]
    assert pending == [1, 1]
    assert attempts == [0, 0]
    assert recomputes == [0, 2]


# ---------------------------------------------------------------------------
# 6. The read is yield-free up to the send, by source; the bus accepts
#    without yielding; the gate body is unchanged
# ---------------------------------------------------------------------------

def _await_tokens(src: str) -> list[str]:
    import re

    return re.findall(r"\bawait\b\s+[\w.]+\(", src)


@pytest.mark.asyncio
async def test_the_dispatch_site_read_is_yield_free_before_the_send(
        tmp_path, monkeypatch):
    import casa_core
    import plugin_setup_episodes as pse
    from bus import BusMessage, MessageBus, MessageType

    params = inspect.signature(pse.configure).parameters
    assert list(params).count("applied_routing") == 1

    src = inspect.getsource(pse._run_episode)
    assert src.count("_applied_routing_state()") == 2
    final = src.rindex("_applied_routing_state()")
    send = src.index("await _dispatch(", final)
    assert _await_tokens(src[final:send + len("await _dispatch(")]) == [
        "await _dispatch("]

    main_src = inspect.getsource(casa_core.main)
    start = main_src.index("async def _setup_dispatch")
    end = main_src.index("await bus.send_checked(", start)
    assert _await_tokens(main_src[start:end + len("await bus.send_checked(")]) == [
        "await bus.send_checked("]
    assert main_src.count("applied_routing=_applied_plugin_routing") == 1

    gate_src = inspect.getsource(casa_core._callback_and_trigger_routes_live)
    for token in ("overlay_unavailable", "_applied_plugin_routing",
                  "active_runtime"):
        assert gate_src.count(token) == 0, token

    # The bus fact the invariant's "send" relies on: an unbounded registered
    # queue accepts before a callback scheduled BEFORE the send can run.
    bus = MessageBus()
    bus.register("assistant")
    ran = []
    asyncio.get_running_loop().call_soon(lambda: ran.append(1))
    results = [await bus.send_checked(BusMessage(
        type=MessageType.NOTIFICATION, source="test", target="assistant",
        content="hi"))]
    assert results.count("accepted") == 1
    assert bus.queues["assistant"].qsize() == 1
    assert len(ran) == 0


# ---------------------------------------------------------------------------
# 7 / 11. A publication during dispatch BACKOFF refuses the second attempt
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_sentinel_published_during_dispatch_backoff_refuses_the_second_attempt(
        tmp_path, monkeypatch):
    import plugin_setup_episodes as pse

    h = Harness(tmp_path, monkeypatch)
    h.rebuild()
    h.live()
    h.dispatch_results[:] = [False]          # attempt 1 rejected, 2 would land

    async def _publish(delay):
        await h.publish_callback_sentinel()
    h.on_sleep = _publish
    await h.release_obligation()
    retry = await pse._worker_pass()
    assert len(h.dispatches) == 1
    assert len(h.sleeps) == 1
    assert retry is True
    assert h.pending_released() == 1
    assert int(h.row().get("attempts") or 0) == 0
    assert h.row()["last_error"] == SENTINEL_ERROR


@pytest.mark.asyncio
async def test_a_publication_during_dispatch_backoff_refuses_the_second_attempt_even_when_live(
        tmp_path, monkeypatch):
    import plugin_setup_episodes as pse

    h = Harness(tmp_path, monkeypatch)
    h.rebuild()
    h.live()
    h.dispatch_results[:] = [False]
    publications = {"n": 0}

    async def _publish(delay):
        h.reg.replace_plugin_overlay({})     # live → live, no reconcile, no kick
        publications["n"] += 1
    h.on_sleep = _publish
    await h.release_obligation()
    pse._kick.clear()
    retry = await pse._worker_pass()
    assert len(h.dispatches) == 1
    assert publications["n"] == 1
    assert pse._kick.is_set() is False
    assert retry is True
    assert h.pending_released() == 1
    assert int(h.row().get("attempts") or 0) == 0
    assert h.row()["last_error"] == REPUBLISHED_ERROR


# ---------------------------------------------------------------------------
# 8. The final read refuses even when the routes gate is doubled green
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_final_read_holds_with_the_routes_gate_doubled_live(
        tmp_path, monkeypatch):
    import plugin_setup_episodes as pse

    h = Harness(tmp_path, monkeypatch)
    gate_calls = {"n": 0}

    def _green(plugin):
        gate_calls["n"] += 1
        return True
    h.rebuild(routes_live=_green)
    h.live()
    answers = [False, True]                  # capture: live; final read: unavailable
    monkeypatch.setattr(h.reg, "plugin_overlay_unavailable",
                        lambda: answers.pop(0) if answers else False)
    await h.release_obligation()
    retry = await pse._worker_pass()
    assert gate_calls["n"] == 1
    assert len(h.dispatches) == 0
    assert retry is True
    assert h.pending_released() == 1
    assert int(h.row().get("attempts") or 0) == 0
    assert h.row()["last_error"] == SENTINEL_ERROR

    assert await pse._worker_pass() is False   # predicates consistently live
    assert gate_calls["n"] == 2
    assert len(h.dispatches) == 1


# ---------------------------------------------------------------------------
# 9. A live→live publication during the recompute defers (no kick was fired)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_live_to_live_publication_during_the_route_recompute_defers_the_dispatch(
        tmp_path, monkeypatch):
    """The revoke sweep's shape: an ordinary map is published through
    ``replace_plugin_overlay`` while the gate's recompute is blocked; nothing
    reconciles and nothing kicks. Both predicates still read live, so only
    the publication generation can see it."""
    import plugin_setup_episodes as pse

    h = Harness(tmp_path, monkeypatch)
    h.rebuild()
    h.live()
    await h.release_obligation()
    pse._kick.clear()

    async def _publish():
        h.reg.replace_plugin_overlay({})
    retry = await h.raced_pass(_publish)
    assert len(h.dispatches) == 0
    assert retry is True
    assert pse._kick.is_set() is False
    assert h.pending_released() == 1
    r = h.row()
    assert (int(r.get("attempts") or 0), int(r.get("resolve_deferrals") or 0)) == (0, 0)
    assert r["last_error"] == REPUBLISHED_ERROR

    assert await pse._worker_pass() is False   # no further publication
    assert len(h.dispatches) == 1


# ---------------------------------------------------------------------------
# 10. Every overlay publication advances one registry generation
# ---------------------------------------------------------------------------

def test_every_overlay_publication_advances_the_routing_generation():
    from trigger_registry import ROUTING_UNAVAILABLE as U

    reg = _registry()
    gen = getattr(reg, "routing_generation", lambda: 0)
    base = gen()
    deltas, identities, calls = [], 0, 0
    for half in ("plugin", "callback"):
        publish = getattr(reg, f"replace_{half}_overlay")
        for payload in ({}, U, U, {}):
            publish(payload)
            calls += 1
            deltas.append(gen() - base)
            if payload is U:
                # The half's OWN store, looked up by name: a bound-method
                # identity test is always False (each attribute access binds
                # anew) and would silently read the other overlay.
                identities += getattr(reg, f"_{half}_overlay") is U
    assert deltas == [1, 2, 3, 4, 5, 6, 7, 8]
    assert calls == 8
    assert identities == 4
    assert (reg.plugin_overlay_unavailable(),
            reg.callback_overlay_unavailable()) == (False, False)


# ---------------------------------------------------------------------------
# 12. A probe that raises once recovers on the worker's OWN timer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_probe_that_raises_once_recovers_on_the_workers_timer_without_a_publication(
        tmp_path, monkeypatch):
    """The deferral is worker-owned: no publication, no external kick — the
    5 s self-kick alone re-runs the ladder and the setup lands."""
    import plugin_setup_episodes as pse

    h = Harness(tmp_path, monkeypatch)
    h.rebuild()
    h.live()
    probe = {"n": 0}
    real_probe = h.reg.plugin_overlay_unavailable

    def _probe():
        probe["n"] += 1
        if probe["n"] == 1:
            raise RuntimeError("transient probe failure")
        return real_probe()
    monkeypatch.setattr(h.reg, "plugin_overlay_unavailable", _probe)
    timer_gate = asyncio.Event()
    timer_delays: list[float] = []

    async def _timer(delay):
        if delay == pse._RETRY_INTERVAL_S:
            timer_delays.append(delay)
            await timer_gate.wait()
    h.on_sleep = _timer
    await h.release_obligation()
    publications = {"n": 0}
    orig_plugin, orig_cb = h.reg.replace_plugin_overlay, h.reg.replace_callback_overlay
    monkeypatch.setattr(h.reg, "replace_plugin_overlay",
                        lambda o: (publications.__setitem__("n", publications["n"] + 1),
                                   orig_plugin(o)))
    monkeypatch.setattr(h.reg, "replace_callback_overlay",
                        lambda o: (publications.__setitem__("n", publications["n"] + 1),
                                   orig_cb(o)))

    pse.start_worker()                         # the one external kick
    try:
        await wait_until(lambda: len(h.dispatches) or timer_delays)
        assert len(h.dispatches) == 0
        assert timer_delays == [pse._RETRY_INTERVAL_S]
        timer_gate.set()
        await wait_until(lambda: len(h.dispatches) == 1)
    finally:
        pse._worker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pse._worker_task
    assert publications["n"] == 0
    assert len(h.dispatches) == 1
    assert probe["n"] == 3                     # raise; capture + compare on retry
    assert h.row()["status"] == "dispatched"
