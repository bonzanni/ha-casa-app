"""#823 — INV-TRIG-017: a trigger reconcile publishes the unavailable marker
before it may create or rekey a per-trigger secret, replaces it only with the
map that pass computed, and a cancelled writing pass holds the reconcile lock
until its writes have landed.

Every test drives the REAL ``trigger_reconcile.reconcile_plugin_triggers`` (the
real ``_mint_secrets`` writing real secret files under ``tmp_path``), a REAL
``TriggerRegistry`` installed on ``agent.active_runtime`` that records every
plugin-overlay publication by identity, the REAL setup-dispatch gate
``casa_core._callback_and_trigger_routes_live`` and — where a dispatch is
counted — the REAL ``plugin_setup_episodes._worker_pass`` configured with the
applied-routing read (#803). The block point is a wrapper on ``tr._mint_secrets``
that calls the real function and then waits: it sits after the mint on both the
pre-fix tree (inside the threaded compute) and the fixed one (the writing hop).
Counts, not statuses; named tests only.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from broker_helpers import wait_until

PLUGIN = "gmail"
EFFECTIVE = "plg-gmail--push"
TRIGGER_AUTH = {"mode": "static_header", "header": "X-API-Key"}
ENTRY = {
    "artifact_id": "art-1",
    "setup_tool": "setup_gmail",
    "granted_tools": ["gmailsrv"],
    "targets": ["resident:assistant"],
}
ROLE_CONFIGS = {"assistant": SimpleNamespace(channels=["webhook"])}


def _plugin(artifact: str = "art-1"):
    return SimpleNamespace(
        name=PLUGIN, artifact_id=artifact, path=f"/store/gmail/{artifact}",
        version="1.0.0", manifest_name=PLUGIN,
        manifest={"name": PLUGIN, "casa": {"triggers": [
            {"name": "push", "type": "webhook", "target": "resident:assistant",
             "auth": dict(TRIGGER_AUTH)}]}})


def _resolver_for(plugin):
    def resolve(target=None):
        return SimpleNamespace(registry_valid=True, plugins=[plugin], issues=[])
    return resolve


class RecordingRegistry:
    """The real registry, with every plugin-overlay publication recorded as
    ``"marker"`` (the singleton, by identity) or ``"map"``."""

    def __init__(self):
        from trigger_registry import TriggerRegistry

        self._reg = TriggerRegistry(scheduler=None, app=None, bus=None)
        self.publications: list[str] = []

    def replace_plugin_overlay(self, overlay):
        import trigger_registry as treg

        self.publications.append(
            "marker" if overlay is treg.ROUTING_UNAVAILABLE else "map")
        return self._reg.replace_plugin_overlay(overlay)

    def __getattr__(self, name):
        return getattr(self._reg, name)

    def markers(self) -> int:
        return self.publications.count("marker")

    def maps(self) -> int:
        return self.publications.count("map")

    def routes(self) -> int:
        return len(self._reg.plugin_overlay_names())


class Fence:
    """The shared harness: one approved static-header trigger, real acks, real
    secret files, the real gate and worker, module doubles for everything the
    reconciler resolves through its defaults."""

    def __init__(self, tmp_path: Path, monkeypatch):
        import agent as agent_mod
        import callback_reconcile as cr
        import casa_core
        import event_reconcile
        import plugin_registry
        import plugin_setup_episodes as pse
        import trigger_reconcile as tr
        from callback_acks import CallbackAckStore
        from trigger_acks import TriggerAckStore

        self.tmp = tmp_path
        self.mp = monkeypatch
        self.plugin = _plugin()
        self.secrets = tmp_path / "webhook_secrets"
        self.secrets.mkdir()
        self.acks = TriggerAckStore(path=tmp_path / "trigger_acks.json")
        # The consent the reconciler looks for: the pending row's ``auth`` is
        # the NORMALIZED map, so the identity has to come from the compute's
        # own row — a hand-built one hashes differently and never matches.
        row = tr.compute_desired(
            role_configs=ROLE_CONFIGS,
            acks=SimpleNamespace(get=lambda ident: None),
            resolver=_resolver_for(self.plugin),
            global_secret_ok=lambda: True).pending[0]
        self.acks.record(
            identity=tr.ack_identity(
                plugin=row["plugin"], artifact_id=row["artifact_id"],
                effective=row["effective"], target=row["target"],
                auth=row["auth"]),
            plugin=row["plugin"], artifact_id=row["artifact_id"],
            effective=row["effective"], target=row["target"], auth=row["auth"])
        self.resolver = _resolver_for(self.plugin)
        # The identity the artifact is bound to is the entry's own (the ack
        # identity plus its approval generation) — read it from the compute.
        self.identity = tr.compute_desired(
            role_configs=ROLE_CONFIGS, acks=self.acks, resolver=self.resolver,
            global_secret_ok=lambda: True).overlay[EFFECTIVE]["identity"]

        # A fresh reconcile lock for this test's loop: ``asyncio.Lock`` binds
        # to the loop of the first CONTENDED acquire, and the cancellation and
        # successor tests below contend — the module lock would otherwise stay
        # bound to this test's closed loop and break a later file's contention.
        monkeypatch.setattr(tr, "_RECONCILE_LOCK", asyncio.Lock())
        monkeypatch.setattr(tr, "SECRETS_DIR", self.secrets)
        monkeypatch.setattr(tr, "_default_acks", lambda: self.acks)
        monkeypatch.setattr(tr, "_default_resolver", lambda: self.resolver)
        monkeypatch.setattr(tr, "_default_global_secret_ok",
                            lambda: (lambda: True))
        monkeypatch.setattr(cr, "_default_acks", lambda: CallbackAckStore(
            path=tmp_path / "callback_acks.json"))
        monkeypatch.setattr(cr, "_default_spool", lambda: None)
        monkeypatch.setattr(cr, "_default_entries", lambda: (lambda: []))
        monkeypatch.setattr(cr, "_default_resolver", lambda: self.resolver)
        monkeypatch.setattr(plugin_registry, "resolve_all",
                            lambda: SimpleNamespace(issues=[], warnings=[]))
        monkeypatch.setattr(plugin_registry, "load_registry",
                            lambda *a, **k: SimpleNamespace(
                                raw={}, valid=False, entries=[]))
        monkeypatch.setattr(event_reconcile, "current_issues", lambda: [])
        plugin = self.plugin

        def _fake_pin():
            def _resolve(target=None):
                return SimpleNamespace(registry_valid=True, plugins=[plugin],
                                       issues=[], generation=1)
            _resolve.generation = 1
            _resolve.entries = lambda: [
                {"name": PLUGIN, "artifact_id": "art-1",
                 "targets": ["resident:assistant"]}]
            return _resolve
        monkeypatch.setattr(plugin_registry, "pinned_resolver", _fake_pin)

        self.reg = RecordingRegistry()
        monkeypatch.setattr(agent_mod, "active_runtime", SimpleNamespace(
            role_configs=ROLE_CONFIGS, channel_manager=None,
            trigger_registry=self.reg), raising=False)

        self.dispatches: list = []
        self.retries: list[bool] = []
        monkeypatch.setattr(pse, "STORE_PATH", tmp_path / "episodes.json")
        monkeypatch.setattr(pse, "_lock", None)
        monkeypatch.setattr(pse, "_kick", None)
        monkeypatch.setattr(pse, "_retry_task", None)
        monkeypatch.setattr(pse, "_worker_task", None)
        fence = self

        async def _dispatch(role, instruction, ctx):
            fence.dispatches.append(role)
            return True

        async def _notify(text):
            pass

        async def _sleep(delay):
            pass

        pse.configure(
            dispatch=_dispatch, notify_operator=_notify,
            resolve_registry_entry=lambda p: dict(ENTRY),
            ack_lookup=lambda ident: None,
            routes_live=casa_core._callback_and_trigger_routes_live,
            applied_routing=casa_core._applied_plugin_routing,
            sleep=_sleep)
        self.tr, self.pse, self.casa_core = tr, pse, casa_core
        self.real_mint = tr._mint_secrets

    # -- state ---------------------------------------------------------------
    def live_prior(self):
        """A LIVE previous overlay (the survey's A2 arm — the state every
        consumer-side read calls 'published'); the callback half authoritative."""
        self.reg.replace_plugin_overlay({})
        self.reg.replace_callback_overlay({})
        self.reg.publications.clear()

    def bound(self) -> int:
        import webhook_auth

        return int(webhook_auth.secret_bound_to_identity(
            EFFECTIVE, identity=self.identity, secrets_dir=self.secrets))

    async def gate(self) -> int:
        return int(await asyncio.to_thread(
            self.casa_core._callback_and_trigger_routes_live, PLUGIN))

    async def release_obligation(self):
        self.pse.ensure_obligation(plugin=PLUGIN, artifact_id="art-1")
        self.pse.open_round(plugin=PLUGIN, artifact_id="art-1", identities=[])
        await self.pse._recover_and_settle()
        assert self.pse.episodes()[0]["gate"] == "released"

    def pending_released(self) -> int:
        return sum(1 for e in self.pse.episodes()
                   if e["status"] == "pending" and e["gate"] == "released")

    async def worker_pass(self) -> bool:
        deferred = await self.pse._worker_pass()
        self.retries.append(bool(deferred))
        return bool(deferred)

    # -- the reconcile ---------------------------------------------------------
    def reconcile(self, *, resolver=None, regen_health: bool = False):
        return self.tr.reconcile_plugin_triggers(
            trigger_registry=self.reg, role_configs=ROLE_CONFIGS,
            acks=self.acks, secrets_dir=self.secrets, prompt=False,
            resolver=resolver if resolver is not None else self.resolver,
            global_secret_ok=lambda: True, regen_health=regen_health)

    def block_after_mint(self, *, first_only: bool = False, raise_after=None):
        """Replace ``tr._mint_secrets`` with a wrapper that runs the REAL mint,
        signals ``entered``, then waits on ``release`` (optionally raising after
        the wait). Returns ``(entered, release)``."""
        entered, release = threading.Event(), threading.Event()
        real = self.real_mint
        calls = {"n": 0}

        def wrapped(desired, secrets_dir):
            real(desired, secrets_dir)
            calls["n"] += 1
            if first_only and calls["n"] > 1:
                return
            entered.set()
            assert release.wait(timeout=20), "mint thread never released"
            if raise_after is not None:
                raise raise_after
        self.mp.setattr(self.tr, "_mint_secrets", wrapped)
        return entered, release


# ---------------------------------------------------------------------------
# 1. The marker leads the first secret write
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_writing_pass_publishes_the_marker_before_its_first_secret_write(
        tmp_path, monkeypatch):
    """Survey probe A2 arm 1b through the real reconciler: with a LIVE previous
    map and an unbound secret, the pass blocked right after its mint has left
    a bound artifact (the gate reads it as live) while the applied overlay
    carries no route. Pre-fix: no marker stands there — publications ``[]``,
    ``plugin_overlay_unavailable() is False`` — because the mint runs inside
    the threaded compute before the pass's first publication. Fixed: the pass
    published the marker (and only the marker) before it wrote."""
    f = Fence(tmp_path, monkeypatch)
    f.live_prior()
    entered, release = f.block_after_mint()
    task = asyncio.create_task(f.reconcile())
    try:
        await wait_until(entered.is_set)
        assert f.bound() == 1
        assert await f.gate() == 1
        assert f.reg.markers() == 1
        assert f.reg.maps() == 0
        assert f.reg.plugin_overlay_unavailable() is True
        assert f.reg.routes() == 0
        assert f.reg.publications == ["marker"]
    finally:
        release.set()
    await task
    assert f.reg.publications == ["marker", "map"]
    assert f.reg.routes() == 1
    assert f.reg.plugin_overlay_unavailable() is False


# ---------------------------------------------------------------------------
# 2. The pair: no setup dispatch lands between the mint and the publish
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_setup_dispatch_lands_between_the_mint_and_the_publish(
        tmp_path, monkeypatch):
    """The window #823 exists for, through the real worker configured with the
    applied-routing read (#803): a pass woken inside it — the gate answers
    live from the bound artifact — must DEFER on the standing marker rather
    than dispatch against a route the applied overlay does not carry. Pre-fix
    (the carried #803 reads without the reconciler fix): the previous map is
    live, no marker stands, the generation does not move, and the one worker
    pass dispatches (1) with no retry (0) — episode consumed against a 404."""
    f = Fence(tmp_path, monkeypatch)
    f.live_prior()
    await f.release_obligation()
    entered, release = f.block_after_mint()
    task = asyncio.create_task(f.reconcile())
    try:
        await wait_until(entered.is_set)
        assert await f.gate() == 1
        deferred = await f.worker_pass()
        assert len(f.dispatches) == 0
        assert deferred is True
        assert f.pending_released() == 1
        assert int(f.pse.episodes()[0].get("attempts") or 0) == 0
        assert f.reg.publications == ["marker"]
    finally:
        release.set()
    await task
    deferred = await f.worker_pass()
    assert len(f.dispatches) == 1
    assert deferred is False
    assert f.reg.routes() == 1
    assert f.reg.publications == ["marker", "map"]


# ---------------------------------------------------------------------------
# 3. A cancelled caller does not release the lock while the writes are in flight
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_caller_cancelled_mid_mint_holds_the_lock_until_its_writes_land(
        tmp_path, monkeypatch):
    """Survey C6 + sol's orphan sequence: ``asyncio.to_thread`` cannot stop
    the writing thread, so a cancellation delivered at the await must not
    release ``_RECONCILE_LOCK`` — a successor would otherwise publish a map
    over writes still landing. Pre-fix: the cancelled caller completes at once,
    the lock is free, the health regeneration runs at once, and the successor
    publishes its map while the first thread is still blocked. Fixed: the
    caller stays pending, the lock stays held, the successor publishes nothing
    and the regeneration does not run until the writes have landed; the
    cancelled pass then publishes NO map (the marker stands for the
    successor) and the successor publishes exactly one."""
    f = Fence(tmp_path, monkeypatch)
    f.live_prior()
    await f.release_obligation()
    regens = {"n": 0}

    async def _regen():
        regens["n"] += 1
    monkeypatch.setattr(f.tr, "_regen_health_safe", _regen)
    entered, release = f.block_after_mint(first_only=True)
    first = asyncio.create_task(f.reconcile(regen_health=True),
                                name="first-writing-pass")
    successor = None
    try:
        await wait_until(entered.is_set)
        assert f.bound() == 1
        first.cancel()
        successor = asyncio.create_task(f.reconcile(), name="successor-pass")
        # Let the cancellation be delivered and the successor reach the lock.
        for _ in range(20):
            await asyncio.sleep(0)
        # The cancellation-specific facts first: the caller must still be
        # pending and the lock still held while the writes are in flight.
        assert first.done() is False
        assert f.tr._RECONCILE_LOCK.locked() is True
        assert successor.done() is False
        assert regens["n"] == 0
        assert f.reg.maps() == 0
        assert f.reg.markers() == 1
        assert f.reg.publications == ["marker"]
        assert f.bound() == 1
        assert await f.gate() == 1
    finally:
        release.set()
    cancelled = 0
    try:
        await first
    except asyncio.CancelledError:
        cancelled += 1
    assert cancelled == 1
    await successor
    assert f.reg.publications == ["marker", "map"]
    assert f.reg.markers() == 1
    assert f.reg.maps() == 1
    assert f.reg.routes() == 1
    assert regens["n"] == 1
    assert f.tr._RECONCILE_LOCK.locked() is False
    deferred = await f.worker_pass()
    assert len(f.dispatches) == 1
    assert deferred is False


# ---------------------------------------------------------------------------
# 4. A pass whose secrets are all bound publishes no marker (the narrowing)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_pass_whose_secrets_are_all_bound_publishes_no_marker(
        tmp_path, monkeypatch):
    """The cost the design refuses to pay: a pass that will write nothing must
    not close ingress. With the secret already bound and the route live, a
    second pass blocked inside its compute leaves the route applied, the gate
    live and a worker pass dispatching; it publishes exactly ``["map"]``.
    Green pre-fix; kills an implementation that publishes the marker on every
    pass."""
    f = Fence(tmp_path, monkeypatch)
    f.live_prior()
    await f.reconcile()          # the WRITING pass: binds the secret, routes it
    assert f.bound() == 1
    assert f.reg.routes() == 1
    f.reg.publications.clear()
    await f.release_obligation()

    entered, release = threading.Event(), threading.Event()
    real = f.resolver
    once = {"blocked": False}

    def blocking_resolver(target=None):
        if not once["blocked"]:
            once["blocked"] = True
            entered.set()
            assert release.wait(timeout=20), "compute thread never released"
        return real(target)

    task = asyncio.create_task(f.reconcile(resolver=blocking_resolver))
    try:
        await wait_until(entered.is_set)
        assert f.bound() == 1
        assert f.reg.markers() == 0
        assert f.reg.maps() == 0
        assert f.reg.routes() == 1
        assert await f.gate() == 1
        deferred = await f.worker_pass()
        assert len(f.dispatches) == 1
        assert deferred is False
    finally:
        release.set()
    await task
    assert f.reg.publications == ["map"]
    assert f.reg.routes() == 1


# ---------------------------------------------------------------------------
# 5. A writing hop that raises re-publishes the marker and re-raises
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_mint_hop_failure_republishes_and_leaves_the_marker(
        tmp_path, monkeypatch):
    """The sentinel-then-re-raise contract (INV-TRIG-006) on the writing hop:
    a failure after the marker was published re-publishes it — ``["marker",
    "marker"]`` — and propagates; no route is applied and a worker pass
    defers. Pre-fix the failure happens inside the single compute hop, so the
    fail-closed arm publishes the marker ONCE (``["marker"]``)."""
    f = Fence(tmp_path, monkeypatch)
    f.live_prior()
    await f.release_obligation()
    entered, release = f.block_after_mint(
        raise_after=RuntimeError("secret store exploded after the mint"))
    task = asyncio.create_task(f.reconcile())
    await wait_until(entered.is_set)
    release.set()
    raised = 0
    try:
        await task
    except RuntimeError:
        raised += 1
    assert raised == 1
    assert f.reg.publications == ["marker", "marker"]
    assert f.reg.markers() == 2
    assert f.reg.maps() == 0
    assert f.reg.routes() == 0
    deferred = await f.worker_pass()
    assert len(f.dispatches) == 0
    assert deferred is True


# ---------------------------------------------------------------------------
# 6. A second cancellation during the drain still holds the lock (review r1, sol)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_second_cancellation_during_the_drain_still_holds_the_lock(
        tmp_path, monkeypatch):
    """The drain must survive every cancellation, not only the first: a second
    ``cancel()`` delivered while the writes are still in flight used to escape
    the drain, release the lock and let a successor publish a map over
    artifacts still landing (reproduced by sol: publications
    ``[marker, marker, map]`` with the writer still running). Fixed: however
    many cancellations arrive, the caller stays pending and the lock held until
    the same future settles; the cancelled pass then publishes no map and the
    successor exactly one."""
    f = Fence(tmp_path, monkeypatch)
    f.live_prior()
    await f.release_obligation()
    entered, release = f.block_after_mint(first_only=True)
    first = asyncio.create_task(f.reconcile(), name="first-writing-pass")
    successor = None
    try:
        await wait_until(entered.is_set)
        assert f.bound() == 1
        first.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        first.cancel()                       # the second cancellation, mid-drain
        successor = asyncio.create_task(f.reconcile(), name="successor-pass")
        for _ in range(20):
            await asyncio.sleep(0)
        assert first.done() is False
        assert f.tr._RECONCILE_LOCK.locked() is True
        assert successor.done() is False
        assert f.reg.publications == ["marker"]
    finally:
        release.set()
    cancelled = 0
    try:
        await first
    except asyncio.CancelledError:
        cancelled += 1
    assert cancelled == 1
    await successor
    assert f.reg.publications == ["marker", "map"]
    assert f.reg.routes() == 1
    assert f.tr._RECONCILE_LOCK.locked() is False
    deferred = await f.worker_pass()
    assert len(f.dispatches) == 1
    assert deferred is False
