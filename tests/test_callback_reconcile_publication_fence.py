"""#825 — INV-CB-010: a cancelled callback reconcile holds ``_RECONCILE_LOCK``
until the writes it already handed to a worker thread have settled, however many
cancellations arrive, and a pass that had already published its overlay delivers
one setup-worker kick after they settle and before the cancellation propagates.

Every test drives the REAL ``callback_reconcile.reconcile_plugin_callbacks``
through the suite's own doubles (``tests/test_callback_reconcile.py``) with a
REAL ``CallbackAckStore``, a real ``TriggerRegistry`` recording every callback
publication, and a spool that blocks the worker thread INSIDE one call — after
that call's own mutation has landed and before it returns — so a cancellation is
delivered while a real thread is mid-write. Counts, not statuses; named tests
only; no ``asyncio.sleep`` is patched anywhere.

The lock and the kick are installed FRESH per test: a module-level
``asyncio.Lock`` binds to the loop of the first CONTENDED acquire, and these
tests contend, so the module lock would stay bound to a closed loop and break a
later file.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

import callback_reconcile as cr
import plugin_setup_episodes as pse
import trigger_registry as _treg
from broker_helpers import wait_until
from callback_acks import CallbackAckStore
from trigger_registry import TriggerRegistry

from test_callback_reconcile import (BASE, _SpoolStub, _ack, _entries,
                                     _plugin, _resolver, _role_configs)

PLUGIN = "gmail"
ARTIFACT = "art-1"


class BlockingSpool(_SpoolStub):
    """The suite's durable-inventory stub, able to park the worker thread inside
    ONE spool call. Only the first call to that op blocks — a successor pass has
    to be able to finish while the first thread is still held."""

    def __init__(self, calls, *, block_in: str | None = None, events=None):
        super().__init__(calls)
        self.block_in = block_in
        self.events = events if events is not None else []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.returns = 0
        self._hits = 0

    def _park(self, op: str) -> None:
        if op != self.block_in:
            return
        self._hits += 1
        if self._hits > 1:
            return
        self.events.append("publish-enter")
        self.entered.set()
        assert self.release.wait(timeout=30), f"{op} thread never released"

    def _ret(self, op: str) -> None:
        if op == self.block_in:
            self.returns += 1
            self.events.append("publish-exit")

    def write_ready(self, plugin, payload):
        super().write_ready(plugin, payload)
        self._park("ready")
        self._ret("ready")

    def write_index_entry(self, artifact_realpath, payload):
        super().write_index_entry(artifact_realpath, payload)
        self._park("index")
        self._ret("index")

    def delete_ready(self, plugin):
        out = super().delete_ready(plugin)
        self._park("del_ready")
        self._ret("del_ready")
        return out

    def delete_index_entry(self, artifact_realpath):
        out = super().delete_index_entry(artifact_realpath)
        self._park("del_index")
        self._ret("del_index")
        return out


class BlockingAcks(CallbackAckStore):
    """A real ack store whose ``prune_stale`` can park the worker thread after
    its store mutation has landed."""

    def __init__(self, path, *, block: bool = False):
        super().__init__(path=path)
        self.block = block
        self.entered = threading.Event()
        self.release = threading.Event()
        self.prune_calls = 0
        self.removed = 0

    def prune_stale(self, valid_identities):
        out = super().prune_stale(valid_identities)
        self.prune_calls += 1
        self.removed += len(out)
        if self.block and self.prune_calls == 1:
            self.entered.set()
            assert self.release.wait(timeout=30), "prune thread never released"
        return out


class CountingRegistry(TriggerRegistry):
    """The real registry, counting every callback-overlay publication by kind."""

    def __init__(self):
        super().__init__(scheduler=None, app=None, bus=None)
        self.publications: list[str] = []

    def replace_callback_overlay(self, overlay):
        self.publications.append(
            "marker" if overlay is _treg.ROUTING_UNAVAILABLE else "map")
        return super().replace_callback_overlay(overlay)

    def maps(self) -> int:
        return self.publications.count("map")


class Fence:
    """One assigned, acked ``gmail/authorize`` callback, a real ack store, the
    real reconciler, and per-test fresh lock/kick objects."""

    def __init__(self, tmp_path, monkeypatch, *, block_in=None,
                 block_prune=False, kick_raises=False, stale_ack=False):
        self.tmp = tmp_path
        self.mp = monkeypatch
        self.plugin = _plugin(name=PLUGIN, artifact_id=ARTIFACT)
        self.calls: list = []
        self.events: list[str] = []
        self.spool = BlockingSpool(self.calls, block_in=block_in,
                                   events=self.events)
        self.acks = BlockingAcks(tmp_path / "callback_acks.json",
                                 block=block_prune)
        _ack(self.acks, plugin=PLUGIN, declared="authorize")
        if stale_ack:
            # An identity no installed declaration can compute — the prune's
            # keep-set excludes it, so a prunable pass removes exactly this one.
            self.acks.record(plugin="ghost", effective="plg-ghost--gone",
                             declaration_digest="deadbeef" * 8)
        self.reg = CountingRegistry()
        self.kicks = 0
        self.kick_raises = kick_raises
        fence = self

        def _kick():
            fence.kicks += 1
            fence.events.append("kick")
            if fence.kick_raises:
                raise RuntimeError("synthetic kick failure")
            if pse._kick is not None:
                pse._kick.set()

        # A fresh lock and a fresh kick Event per test (see the module docstring).
        monkeypatch.setattr(cr, "_RECONCILE_LOCK", asyncio.Lock())
        monkeypatch.setattr(pse, "_kick", asyncio.Event())
        monkeypatch.setattr(pse, "kick", _kick)
        monkeypatch.setattr(pse, "STORE_PATH", tmp_path / "episodes.json")
        monkeypatch.setattr(pse, "_lock", None)
        monkeypatch.setattr(cr, "_base_url", lambda: BASE)
        self.regens = 0

        async def _regen():
            fence.regens += 1
            fence.events.append("regen")
        monkeypatch.setattr(cr, "_regen_health_safe", _regen)

    # -- driving -------------------------------------------------------------
    def reconcile(self, *, regen_health: bool = False):
        return cr.reconcile_plugin_callbacks(
            trigger_registry=self.reg,
            role_configs=_role_configs(assistant=["telegram"]),
            acks=self.acks, spool=self.spool,
            resolver=_resolver([self.plugin]),
            entries=_entries(self.plugin), prompt=False,
            regen_health=regen_health)

    def seed_stale_pair(self):
        """Publish a pair for a DIFFERENT base so the next pass's pre-swap half
        retires it (delete ready + index) before re-publishing."""
        self.spool.write_ready(PLUGIN, {"stale": True})
        self.spool.write_index_entry(self.plugin.path, {"stale": True})
        self.calls.clear()

    # -- observation ---------------------------------------------------------
    def n(self, what: str) -> int:
        return sum(1 for c in self.calls if c[0] == what)

    def vector(self, first, successor=None) -> tuple:
        return (first.done(),
                cr._RECONCILE_LOCK.locked(),
                None if successor is None else successor.done(),
                self.reg.maps(),
                self.n("ready"), self.n("index"),
                self.n("del_ready"), self.n("del_index"),
                self.acks.prune_calls,
                self.kicks)

    def gaps(self) -> int:
        desired = cr.compute_desired(
            role_configs=_role_configs(assistant=["telegram"]),
            acks=self.acks, resolver=_resolver([self.plugin]),
            entries=_entries(self.plugin))
        before = len(desired.issues)
        cr.verify_published_markers(desired, self.spool)
        return len(desired.issues) - before


async def _settle(n: int = 25) -> None:
    """Yield the loop n times so a delivered cancellation reaches its handler
    and a successor task reaches the lock. No sleeps, no patched clocks."""
    for _ in range(n):
        await asyncio.sleep(0)


async def _cancelled(task) -> int:
    try:
        await task
    except asyncio.CancelledError:
        return 1
    return 0


# ---------------------------------------------------------------------------
# 1. Cancelled at the pre-swap retire — the lock is held until the deletes land
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancelled_pre_swap_retire_holds_lock_until_worker_settles(
        tmp_path, monkeypatch):
    """The retire half deletes marker pairs from a thread that cannot be
    stopped. Pre-fix the cancellation escapes the bare await, ``_RECONCILE_LOCK``
    is released while those deletes are still landing, and a successor computes
    against files the orphan has not deleted yet — it can read a pair as already
    equal to desired, republish nothing, publish its map, spend its kicks, and
    only then have the orphan delete that pair: a live overlay, an absent pair,
    and the kick already gone. Fixed: the caller stays pending and the lock held
    until the same future settles; this pass publishes NO overlay and delivers NO
    kick (nothing of its own became durable-and-live), and the successor is the
    only publisher."""
    f = Fence(tmp_path, monkeypatch, block_in="del_index")
    f.seed_stale_pair()
    first = asyncio.create_task(f.reconcile(), name="retire-pass")
    successor = None
    try:
        await wait_until(f.spool.entered.is_set)
        first.cancel()
        successor = asyncio.create_task(f.reconcile(), name="successor")
        await _settle()
        assert f.vector(first, successor) == (
            False, True, False, 0, 0, 0, 1, 1, 0, 0)
    finally:
        f.spool.release.set()
    assert await _cancelled(first) == 1
    await successor
    assert (cr._RECONCILE_LOCK.locked(), f.reg.maps(), f.kicks) == (
        False, 1, 2)
    assert f.gaps() == 0


# ---------------------------------------------------------------------------
# 2. Cancelled at the post-swap publish — hold, then exactly one kick
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancelled_post_swap_publish_holds_lock_then_kicks_once(
        tmp_path, monkeypatch):
    """#825's own arm. The pass swapped a LIVE map, so nothing carries the
    unavailable sentinel and the scheduled recovery has nothing to collect; the
    setup worker's route gate holds on the marker pair with no timer. Pre-fix the
    cancelled pass leaves the pair complete and kicks ZERO times. Fixed: the lock
    is held until the writes land, then exactly one kick, then the cancellation
    propagates."""
    f = Fence(tmp_path, monkeypatch, block_in="index")
    first = asyncio.create_task(f.reconcile(), name="publish-pass")
    successor = None
    try:
        await wait_until(f.spool.entered.is_set)
        first.cancel()
        successor = asyncio.create_task(f.reconcile(), name="successor")
        await _settle()
        assert f.vector(first, successor) == (
            False, True, False, 1, 1, 1, 0, 0, 0, 0)
    finally:
        f.spool.release.set()
    assert await _cancelled(first) == 1
    await successor
    assert (cr._RECONCILE_LOCK.locked(), f.reg.maps(), f.kicks) == (
        False, 2, 3)
    assert f.gaps() == 0


# ---------------------------------------------------------------------------
# 3. Cancelled at the ack prune — the wake is not lost to the later hop either
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancelled_ack_prune_holds_lock_then_kicks_once(
        tmp_path, monkeypatch):
    """The prune is the second threaded write of the same pass. Pre-fix a
    cancellation there releases the lock over an ack-store rewrite in flight —
    an orphan that can delete an ack a successor recorded after this pass
    computed its keep-set — and again kicks zero times, with the marker pair
    already complete. Fixed: held until the prune settles, then one kick."""
    f = Fence(tmp_path, monkeypatch, block_prune=True, stale_ack=True)
    first = asyncio.create_task(f.reconcile(), name="prune-pass")
    successor = None
    try:
        await wait_until(f.acks.entered.is_set)
        first.cancel()
        successor = asyncio.create_task(f.reconcile(), name="successor")
        await _settle()
        assert f.vector(first, successor) == (
            False, True, False, 1, 1, 1, 0, 0, 1, 0)
    finally:
        f.acks.release.set()
    assert await _cancelled(first) == 1
    await successor
    assert (cr._RECONCILE_LOCK.locked(), f.reg.maps(), f.kicks,
            f.acks.removed) == (False, 2, 3, 1)
    assert f.gaps() == 0


# ---------------------------------------------------------------------------
# 4-6. A SECOND cancellation mid-drain — "however many" is pinned per hop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_second_cancellation_during_retire_drain_is_absorbed(
        tmp_path, monkeypatch):
    """The trigger fence's own r1 finding, stated for the retire hop: a single
    re-await lets the SECOND cancellation escape and release the lock over
    deletes still landing. ``task.cancel()`` returns True only for a task still
    running, so the accepted-cancellation count separates a drain that survives
    both from one that ended at the first."""
    f = Fence(tmp_path, monkeypatch, block_in="del_index")
    f.seed_stale_pair()
    first = asyncio.create_task(f.reconcile(), name="retire-pass")
    successor = None
    try:
        await wait_until(f.spool.entered.is_set)
        accepted = int(first.cancel())
        await _settle(5)
        accepted += int(first.cancel())
        successor = asyncio.create_task(f.reconcile(), name="successor")
        await _settle()
        assert accepted == 2
        assert f.vector(first, successor) == (
            False, True, False, 0, 0, 0, 1, 1, 0, 0)
    finally:
        f.spool.release.set()
    assert await _cancelled(first) == 1
    await successor
    assert (cr._RECONCILE_LOCK.locked(), f.reg.maps(), f.kicks) == (
        False, 1, 2)


@pytest.mark.asyncio
async def test_second_cancellation_during_publish_drain_is_absorbed(
        tmp_path, monkeypatch):
    """The same, for the hop #825 is about: however many cancellations arrive,
    the pass holds the lock until its pair is on disk and then delivers exactly
    ONE kick."""
    f = Fence(tmp_path, monkeypatch, block_in="index")
    first = asyncio.create_task(f.reconcile(), name="publish-pass")
    successor = None
    try:
        await wait_until(f.spool.entered.is_set)
        accepted = int(first.cancel())
        await _settle(5)
        accepted += int(first.cancel())
        successor = asyncio.create_task(f.reconcile(), name="successor")
        await _settle()
        assert accepted == 2
        assert f.vector(first, successor) == (
            False, True, False, 1, 1, 1, 0, 0, 0, 0)
    finally:
        f.spool.release.set()
    assert await _cancelled(first) == 1
    await successor
    assert (cr._RECONCILE_LOCK.locked(), f.reg.maps(), f.kicks) == (
        False, 2, 3)


@pytest.mark.asyncio
async def test_second_cancellation_during_prune_drain_is_absorbed(
        tmp_path, monkeypatch):
    """The same, for the prune hop."""
    f = Fence(tmp_path, monkeypatch, block_prune=True, stale_ack=True)
    first = asyncio.create_task(f.reconcile(), name="prune-pass")
    successor = None
    try:
        await wait_until(f.acks.entered.is_set)
        accepted = int(first.cancel())
        await _settle(5)
        accepted += int(first.cancel())
        successor = asyncio.create_task(f.reconcile(), name="successor")
        await _settle()
        assert accepted == 2
        assert f.vector(first, successor) == (
            False, True, False, 1, 1, 1, 0, 0, 1, 0)
    finally:
        f.acks.release.set()
    assert await _cancelled(first) == 1
    await successor
    assert (cr._RECONCILE_LOCK.locked(), f.reg.maps(), f.kicks) == (
        False, 2, 3)


# ---------------------------------------------------------------------------
# 7. Ordering only: no kick reaches the worker mid-write (mutation-only pin)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_cancellation_never_kicks_inside_write_index_entry(
        tmp_path, monkeypatch):
    """The dispatch gate reads the pair under the spool lock, so a worker woken
    between ``write_ready`` and ``write_index_entry`` can read a HALF-published
    pair — which ``_publish_markers_post_swap`` then deletes on failure and
    records as one ``callback_spool_error``: the same waker-less hold by another
    route. This asserts ONLY that no kick is delivered while the thread is still
    inside the write. It PASSES on the pre-fix tree (which kicks not at all) and
    is therefore a mutation check, not a red case: it is what refuses a
    ``finally: kick()`` and a kick placed ahead of the drain. Test 2 owns the
    fact that the kick happens at all; the two halves are mutated separately."""
    f = Fence(tmp_path, monkeypatch, block_in="index")
    first = asyncio.create_task(f.reconcile(), name="publish-pass")
    try:
        await wait_until(f.spool.entered.is_set)
        first.cancel()
        await _settle()
        assert (f.spool.returns, f.kicks, f.reg.maps(),
                f.n("ready"), f.n("index")) == (0, 0, 1, 1, 1)
    finally:
        f.spool.release.set()
    assert await _cancelled(first) == 1
    # The orphan thread's own return, waited for positively: on the pre-fix tree
    # nothing holds the caller for it, so the count is read after it lands.
    await wait_until(lambda: f.spool.returns == 1)


# ---------------------------------------------------------------------------
# 8. The uncancelled control — the normal path is unchanged (mutation-only pin)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uncancelled_reconcile_preserves_publication_order_and_counts(
        tmp_path, monkeypatch):
    """One publication, one routing-generation step, one pair, one prune, TWO
    kicks — the order the record calls a contract, unchanged. Passes at the base;
    it refuses a kick added, removed or moved onto the cancellation path, a
    second publication, and a terminal act mis-indented onto the success path."""
    f = Fence(tmp_path, monkeypatch, stale_ack=True)
    before = f.reg.routing_generation()
    issues = await f.reconcile()
    assert (len(issues), f.reg.maps(),
            f.reg.routing_generation() - before,
            len(f.reg.callback_overlay_names()),
            f.n("ready"), f.n("index"),
            f.acks.prune_calls, f.acks.removed, f.kicks) == (
        0, 1, 1, 1, 1, 1, 1, 1, 2)
    assert f.gaps() == 0
    assert f.events == ["kick", "kick"]


# ---------------------------------------------------------------------------
# 9. The health regeneration waits for the drain (it is in the finally, OUTSIDE
#    the lock) — red at the base
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_regen_health_waits_for_cancelled_publish_drain(
        tmp_path, monkeypatch):
    """``regen_health`` runs in the ``finally``, outside the lock — so it runs
    when the pass actually EXITS. Pre-fix the cancelled pass exits at once and
    regenerates plugin health while the publish thread is still writing, with no
    kick at all. Fixed: nothing runs until the writes settle, then the kick, then
    the regeneration, in that order."""
    f = Fence(tmp_path, monkeypatch, block_in="index")
    first = asyncio.create_task(f.reconcile(regen_health=True), name="pass")
    try:
        await wait_until(f.spool.entered.is_set)
        first.cancel()
        await _settle()
        assert (first.done(), cr._RECONCILE_LOCK.locked(), f.spool.returns,
                f.kicks, f.regens, f.reg.maps()) == (
            False, True, 0, 0, 0, 1)
    finally:
        f.spool.release.set()
    assert await _cancelled(first) == 1
    assert (f.spool.returns, f.kicks, f.regens,
            cr._RECONCILE_LOCK.locked()) == (1, 1, 1, False)
    assert f.events == ["publish-enter", "publish-exit", "kick", "regen"]


# ---------------------------------------------------------------------------
# 10. The factored kick helper still swallows everything (mutation-only pin)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_factored_kick_helper_preserves_swallowing_at_both_normal_sites(
        tmp_path, monkeypatch):
    """A kick is an advisory ``Event.set()`` and must never break a reconcile
    (INV-PLUG-016). With ``kick`` raising at every call site, the pass still
    publishes, prunes and returns its issues, and both sites were attempted.
    Passes at the base; it refuses a factoring that swallows at one site only."""
    f = Fence(tmp_path, monkeypatch, kick_raises=True, stale_ack=True)
    before = f.reg.routing_generation()
    issues = await f.reconcile()
    assert (f.kicks, len(issues), f.reg.maps(),
            f.reg.routing_generation() - before,
            f.n("ready"), f.n("index"), f.acks.prune_calls) == (
        2, 0, 1, 1, 1, 1, 1)
