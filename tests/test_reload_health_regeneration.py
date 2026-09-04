"""#824 — a reload that ran the paired plugin reconcile regenerates plugin health.

The trigger half's health rows are read from the LIVE overlay at regeneration
time (`trigger_reconcile._unavailable_rows`), and a WRITING trigger pass
publishes the unavailable marker for the duration of its secret writes
(INV-TRIG-017). Nothing regenerated health after a reload's paired reconcile, so
a regeneration that sampled that marker left a `trigger_routing_unavailable` row
standing after the map went live, and a reload that HEALED a stuck marker left
the row that was true standing — in both cases until an unrelated regeneration,
because the scheduled recovery regenerates only while a marker still stands.

Every arm drives a REAL production entry point — `tools.casa_reload`,
`tools.casa_reload_triggers`, `POST /admin/reload` — through the real
`reload.dispatch`, whose paired reconcile block runs the real
`trigger_reconcile.reconcile_from_runtime` against the `Fence` harness's real
registry, real acks, real mint and real secret files, and writes through the
real `tools._regenerate_plugin_health` and `plugin_health.write_report`. The
scope's own handler is the ONLY stub: a real `triggers` handler needs a whole
runtime, and it is not the region under test.

Rows are COUNTED out of the written report; no assertion here reads a status.
"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from broker_helpers import wait_until
from test_trigger_reconcile_publication_fence import Fence

pytestmark = pytest.mark.asyncio

ROLE = "assistant"
ROW = "trigger_routing_unavailable"

# The three production reload entry points, by adapter key. `casa_reload_triggers`
# dispatches a hard-coded "triggers", so scope-varying arms use the other two.
ENTRY_POINTS = ["casa_reload", "casa_reload_triggers", "admin_reload"]


class _JsonReq:
    """The one thing `build_admin_reload_handler` reads off a request."""

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class Rig:
    """`Fence` plus the three entry points, on a real dispatch."""

    def __init__(self, tmp_path, monkeypatch, *, live_prior: bool = True):
        import internal_handlers
        import reload as reload_mod
        import tools as tools_mod

        self.f = Fence(tmp_path, monkeypatch)
        if live_prior:
            self.f.live_prior()
        self.mp = monkeypatch
        self.tools = tools_mod
        self.reload = reload_mod
        self.report = tmp_path / "plugin-health.json"

        # Every module primitive this file CONTENDS on binds to the loop of its
        # first contended acquire; pytest-asyncio gives each test a fresh loop.
        monkeypatch.setattr(reload_mod, "_GLOBAL_RW", reload_mod._RWLock())
        monkeypatch.setattr(reload_mod, "_LOCKS", {})
        monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
        monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK_OWNER", None)
        monkeypatch.setattr(tools_mod, "_PLUGIN_HEALTH_PATH", str(self.report))

        # The privileged caller both tools require. Patched on the module
        # rather than set on `origin_var`: a contextvar token created in the
        # fixture cannot be reset from the test's own context.
        monkeypatch.setattr(
            tools_mod, "_effective_caller_role", lambda: "configurator")
        monkeypatch.setattr(
            tools_mod, "_channel_manager", None, raising=False)

        # Counters. `_regenerate_plugin_health` and the notify are resolved as
        # module globals at call time, so wrapping them here sees every caller.
        self.regens: list[str] = []
        self.notifies: list[int] = []
        self.paired: dict[str, int] = {"trigger": 0, "callback": 0, "event": 0}
        real_regen = tools_mod._regenerate_plugin_health

        def _counting_regen(extra_issues):
            self.regens.append("call")
            return real_regen(extra_issues)

        async def _counting_notify():
            self.notifies.append(1)

        monkeypatch.setattr(
            tools_mod, "_regenerate_plugin_health", _counting_regen)
        monkeypatch.setattr(
            tools_mod, "_notify_plugin_health_if_possible", _counting_notify)
        self._count_paired(monkeypatch)

        # The scope handler is the only stub. Registered for every scope this
        # file drives, so `dispatch` reaches its real paired-reconcile block.
        self.handler_error: Exception | None = None

        async def _stub_handler(runtime, *, role=None, include_env=False):
            if self.handler_error is not None:
                raise self.handler_error
            return ["stub_handler_ran"]

        for scope in ("triggers", "agent", "agents", "policies"):
            monkeypatch.setitem(reload_mod._HANDLERS, scope, _stub_handler)

        import agent as agent_mod

        self.admin = internal_handlers.build_admin_reload_handler(
            runtime=agent_mod.active_runtime)

    def _count_paired(self, monkeypatch):
        import callback_reconcile as cr
        import event_reconcile as er
        import trigger_reconcile as tr

        real_tr = tr.reconcile_from_runtime
        real_cr = cr.reconcile_from_runtime
        real_er = er.reconcile_plugin_events

        async def _tr(runtime, **kw):
            self.paired["trigger"] += 1
            return await real_tr(runtime, **kw)

        async def _cr(runtime, **kw):
            self.paired["callback"] += 1
            return await real_cr(runtime, **kw)

        async def _er(runtime, **kw):
            self.paired["event"] += 1
            return await real_er(runtime, **kw)

        monkeypatch.setattr(tr, "reconcile_from_runtime", _tr)
        monkeypatch.setattr(cr, "reconcile_from_runtime", _cr)
        monkeypatch.setattr(er, "reconcile_plugin_events", _er)

    # -- the entry points ----------------------------------------------------
    async def call(self, entry: str, *, scope: str = "triggers",
                   role: str | None = ROLE, include_env: bool = False) -> dict:
        """Drive ONE production entry point; return the reload envelope."""
        if entry == "casa_reload":
            args = {"scope": scope, "include_env": include_env}
            if role is not None:
                args["role"] = role
            res = await self.tools.casa_reload.handler(args)
            return json.loads(res["content"][0]["text"])
        if entry == "casa_reload_triggers":
            res = await self.tools.casa_reload_triggers.handler({"role": role})
            return json.loads(res["content"][0]["text"])
        if entry == "admin_reload":
            payload = {"scope": scope, "include_env": include_env}
            if role is not None:
                payload["role"] = role
            resp = await self.admin(_JsonReq(payload))
            return json.loads(resp.body.decode("utf-8"))
        raise AssertionError(f"unknown entry point {entry!r}")

    # -- observation ---------------------------------------------------------
    def rows(self) -> int:
        """`trigger_routing_unavailable` rows in the WRITTEN report."""
        import plugin_health

        report = plugin_health.load_report(self.report)
        if report is None:
            return 0
        return sum(1 for r in report.get("issues") or []
                   if r.get("reason_code") == ROW)

    async def regenerate_now(self) -> None:
        """A guarded regeneration with no preceding trigger reconcile — the
        shape every non-reconciling regenerator in the tree has."""
        async with self.tools._plugin_tools_guard():
            await asyncio.to_thread(self.tools._regenerate_plugin_health, [])


@pytest.fixture
def rig_factory(tmp_path, monkeypatch):
    def _make(**kw):
        return Rig(tmp_path, monkeypatch, **kw)

    return _make


# ---------------------------------------------------------------------------
# 1. RED — the reload's own transient marker is sampled and outlives the map
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ENTRY_POINTS)
async def test_a_row_sampled_from_this_reloads_marker_does_not_outlive_it(
        entry, rig_factory) -> None:
    """A LIVE prior map and an unbound secret make the reload's paired reconcile
    a WRITING pass, so it publishes the unavailable marker before it mints. A
    competing guarded regeneration that runs in that window samples the marker
    and persists one row. Pre-fix, the entry point returns with that row still
    in the report: nothing regenerates after the map is live, and the scheduled
    recovery — which regenerates only while a marker STANDS — sees none."""
    rig = rig_factory()
    entered, release = rig.f.block_after_mint()

    task = asyncio.create_task(rig.call(entry))
    await wait_until(entered.is_set)
    # The marker stands: a regeneration sampling it writes the row.
    await rig.regenerate_now()
    assert rig.rows() == 1
    assert rig.f.reg.publications == ["marker"]

    release.set()
    envelope = await task

    assert rig.paired == {"trigger": 1, "callback": 1, "event": 1}
    assert rig.f.reg.publications == ["marker", "map"]
    assert int(rig.f.reg.plugin_overlay_unavailable()) == 0
    assert rig.f.reg.routes() == 1
    assert rig.rows() == 0               # RED at base: 1
    assert len(rig.regens) == 2          # the competing pass, then this reload's
    assert envelope["status"] == "ok"
    assert envelope["actions"].count("plugin_health_regenerated") == 1
    assert rig.notifies == []


# ---------------------------------------------------------------------------
# 2. RED — a marker this reload HEALED leaves no row behind
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ENTRY_POINTS)
async def test_a_row_left_true_by_a_marker_this_reload_heals_is_cleared(
        entry, rig_factory) -> None:
    """The other direction, reachable from every reload scope that reconciles.
    The registry starts at the marker (its own initial state — no `live_prior`),
    a regeneration writes the row that is then TRUE, and the reload's paired
    reconcile heals the marker. Pre-fix the row survives its own truth: the
    overlay is authoritative again and the report still says routing is down."""
    rig = rig_factory(live_prior=False)
    await rig.regenerate_now()
    assert rig.rows() == 1

    envelope = await rig.call(entry)

    assert rig.paired == {"trigger": 1, "callback": 1, "event": 1}
    assert int(rig.f.reg.plugin_overlay_unavailable()) == 0
    assert rig.f.reg.routes() == 1
    assert rig.rows() == 0               # RED at base: 1
    assert len(rig.regens) == 2
    assert envelope["status"] == "ok"
    assert rig.notifies == []


# ---------------------------------------------------------------------------
# 3. RED — the regeneration runs under the plugin-tools guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ENTRY_POINTS)
async def test_the_post_reload_regeneration_waits_for_the_plugin_tools_lock(
        entry, rig_factory) -> None:
    """Driven at the UNFENCED `triggers` scope on purpose: `full` would be held
    at the entry point's own `_plugin_tools_reload_guard`, so removing the
    helper's guard would still look serialized and the mutation would pass.
    Here nothing but the helper's own acquisition can hold the entry point."""
    rig = rig_factory()
    held = asyncio.Event()
    let_go = asyncio.Event()

    async def _holder():
        async with rig.tools._plugin_tools_guard():
            held.set()
            await let_go.wait()

    holder = asyncio.create_task(_holder())
    await held.wait()

    task = asyncio.create_task(rig.call(entry))
    # A POSITIVE wait first, so the negative window below starts from a known
    # point: dispatch's paired reconcile has finished and the only thing left
    # for the entry point to do is the regeneration.
    await wait_until(lambda: rig.paired["event"] == 1)
    await asyncio.sleep(0.05)
    assert len(rig.regens) == 0
    assert task.done() is False          # RED at base: the entry point returns

    let_go.set()
    envelope = await task
    await holder
    assert len(rig.regens) == 1
    assert envelope["status"] == "ok"


# ---------------------------------------------------------------------------
# 4. RED — a cancelled caller keeps the guard until its writer has settled
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ENTRY_POINTS)
async def test_a_cancelled_reload_holds_the_guard_until_its_writer_settles(
        entry, rig_factory) -> None:
    """`asyncio.to_thread` cancels the AWAIT, never the thread. A cancelled
    caller that released the guard would let a competing guarded operation
    compute and write the correct report, and then have its own orphaned,
    older report land last. Measured at the base with the real guard:
    completion order ['new-operation-entered', 'old-write-finished']."""
    rig = rig_factory()
    writer_entered = threading.Event()
    let_writer_finish = threading.Event()
    order: list[str] = []

    def _slow_regen(extra_issues):
        writer_entered.set()
        assert let_writer_finish.wait(timeout=20), "writer never released"
        order.append("writer-finished")

    rig.mp.setattr(rig.tools, "_regenerate_plugin_health", _slow_regen)

    task = asyncio.create_task(rig.call(entry))
    await asyncio.to_thread(writer_entered.wait, 5)
    assert writer_entered.is_set()       # RED at base: never set

    task.cancel()
    task.cancel()

    competitor_in = asyncio.Event()

    async def _competitor():
        async with rig.tools._plugin_tools_guard():
            order.append("competitor-entered")
            competitor_in.set()

    competitor = asyncio.create_task(_competitor())
    for _ in range(20):
        await asyncio.sleep(0)
    assert task.done() is False
    assert competitor_in.is_set() is False

    let_writer_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await competitor
    assert order == ["writer-finished", "competitor-entered"]
    assert int(rig.tools._PLUGIN_TOOLS_LOCK.locked()) == 0
    assert rig.notifies == []


# ---------------------------------------------------------------------------
# 4b. RED — a caller cancelled while still QUEUED for the guard keeps the refresh
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ENTRY_POINTS)
async def test_a_reload_cancelled_while_queued_for_the_guard_still_refreshes(
        entry, rig_factory) -> None:
    """The other cancellation door, and the reachable one: the guard can be held
    by an operation that refreshes no health of its own (`_engage_executor_impl`
    holds the raw lock across topic creation and the resolver gate), so a reload
    that healed routing can sit queued behind it. Cancelled there, a refresh that
    is merely awaited is dropped and the row the reload just made false stands.
    Settling the whole acquire-write-release unit through cancellation keeps it.
    """
    rig = rig_factory(live_prior=False)
    await rig.regenerate_now()
    assert rig.rows() == 1

    held = asyncio.Event()
    let_go = asyncio.Event()

    async def _holder():
        async with rig.tools._plugin_tools_guard():
            held.set()
            await let_go.wait()

    holder = asyncio.create_task(_holder())
    await held.wait()

    task = asyncio.create_task(rig.call(entry))
    await wait_until(lambda: rig.paired["event"] == 1)
    await asyncio.sleep(0.05)
    assert len(rig.regens) == 1          # the seeding pass only; still queued

    task.cancel()
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(rig.regens) == 1          # cancelled, not abandoned

    let_go.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await holder

    assert len(rig.regens) == 2          # RED before the fix: 1
    assert int(rig.f.reg.plugin_overlay_unavailable()) == 0
    assert rig.rows() == 0               # RED before the fix: 1
    assert int(rig.tools._PLUGIN_TOOLS_LOCK.locked()) == 0
    assert rig.notifies == []


# ---------------------------------------------------------------------------
# 5. RED — a regeneration that raises does not fail the reload
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ENTRY_POINTS)
async def test_a_failed_regeneration_does_not_fail_the_reload(
        entry, rig_factory) -> None:
    """The reload's own outcome is the operator's answer; a health-report write
    that fails is a warning, not a verdict."""
    rig = rig_factory()
    calls: list[str] = []

    def _exploding_regen(extra_issues):
        calls.append("call")
        raise RuntimeError("health exploded")

    rig.mp.setattr(rig.tools, "_regenerate_plugin_health", _exploding_regen)

    envelope = await rig.call(entry)

    assert rig.paired == {"trigger": 1, "callback": 1, "event": 1}
    assert len(calls) == 1               # RED at base: 0
    assert envelope["status"] == "ok"
    assert envelope["actions"].count("plugin_health_regenerated") == 0
    assert rig.notifies == []


# ---------------------------------------------------------------------------
# 6. WANTED (mutation checks — these PASS on the pre-fix tree)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ["casa_reload", "admin_reload"])
async def test_a_scope_that_runs_no_paired_reconcile_regenerates_nothing(
        entry, rig_factory) -> None:
    """`policies` is not in `reload._TRIGGER_RECONCILE_SCOPES`. Mutation: drop
    the scope predicate and this count becomes 1."""
    rig = rig_factory()
    envelope = await rig.call(entry, scope="policies", role=None)
    assert envelope["status"] == "ok"
    assert rig.paired == {"trigger": 0, "callback": 0, "event": 0}
    assert len(rig.regens) == 0
    assert rig.rows() == 0


@pytest.mark.parametrize("entry", ENTRY_POINTS)
async def test_a_reload_whose_handler_failed_regenerates_nothing(
        entry, rig_factory) -> None:
    """A handler that raises never reaches dispatch's paired-reconcile block —
    the two sit in the same `try` — so there is nothing to re-derive. Mutation:
    drop the status predicate and this count becomes 1."""
    rig = rig_factory()
    rig.handler_error = RuntimeError("handler exploded")
    envelope = await rig.call(entry)
    assert envelope["status"] == "error"
    assert rig.paired == {"trigger": 0, "callback": 0, "event": 0}
    assert len(rig.regens) == 0


async def test_dispatch_itself_still_regenerates_nothing(rig_factory) -> None:
    """The regeneration is ENTRY-POINT-ONLY: `reload.dispatch` must stay clear
    of the plugin-tools lock, which it holds the reload RW lock across
    (INV-CFG-011). The mutation sequencers reach dispatch directly, from under
    the RAW plugin lock, where a guard acquisition self-deadlocks."""
    rig = rig_factory()
    import agent as agent_mod

    envelope = await rig.reload.dispatch(
        "triggers", runtime=agent_mod.active_runtime, role=ROLE)
    assert envelope["status"] == "ok"
    assert rig.paired == {"trigger": 1, "callback": 1, "event": 1}
    assert len(rig.regens) == 0
    assert envelope["actions"].count("plugin_health_regenerated") == 0


@pytest.mark.parametrize(
    "include_env, regens, notifies", [(False, 2, 1), (True, 3, 2)])
async def test_a_full_reload_regenerates_once_per_step_plus_once_after_the_pair(
        include_env, regens, notifies, rig_factory) -> None:
    """`reload_full` invokes the `executors` sub-handler directly, and the
    `plugin_env` one when asked; each regenerates AND notifies, both BEFORE
    dispatch's paired reconcile. The entry point's regeneration then runs after
    it. So a `full` reload regenerates twice and announces once ordinarily, and
    three times announcing twice with plugin environment included — deliberately:
    only the LAST can describe the completed routing pair, and the entry point's
    own regeneration adds no announcement of its own.

    The sub-handler doubles reproduce the two production blocks exactly — the
    guard, the threaded regeneration, the notify, the `plugin_health_regenerated`
    action (reload.py:reload_executors, reload.py:reload_plugin_env). A real
    `executors` handler rebuilds the executor registry, which is not the region
    under test.
    """
    rig = rig_factory()
    import reload as reload_mod

    async def _regen_and_notify(runtime, *, role=None):
        async with rig.tools._plugin_tools_guard():
            await asyncio.to_thread(rig.tools._regenerate_plugin_health, [])
            await rig.tools._notify_plugin_health_if_possible()
        return ["plugin_health_regenerated"]

    async def _noop(runtime, *, role=None):
        return []

    async def _full(runtime, *, role=None, include_env=False):
        actions = [f"executors:{a}"
                   for a in await _regen_and_notify(runtime, role=None)]
        if include_env:
            actions += [f"plugin_env:{a}"
                        for a in await _regen_and_notify(runtime, role=None)]
        return actions

    rig.mp.setitem(reload_mod._HANDLERS, "executors", _regen_and_notify)
    rig.mp.setitem(reload_mod._HANDLERS, "plugin_env", _regen_and_notify)
    rig.mp.setitem(reload_mod._HANDLERS, "full", _full)
    rig.mp.setitem(reload_mod._HANDLERS, "policies", _noop)

    envelope = await rig.call(
        "casa_reload", scope="full", role=None, include_env=include_env)

    assert envelope["status"] == "ok"
    assert rig.paired == {"trigger": 1, "callback": 1, "event": 1}
    assert len(rig.regens) == regens
    assert len(rig.notifies) == notifies
    assert envelope["actions"].count("plugin_health_regenerated") == 1
    assert rig.rows() == 0
