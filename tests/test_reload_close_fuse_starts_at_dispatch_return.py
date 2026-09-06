"""#854 — a replaced agent's drain fuse starts when the RELOAD DISPATCHER
RETURNS, not at the swap.

Declared invariant (D34), pinned by this file:

    A replaced agent's drain, when its swap happens inside a reload dispatch,
    is STARTED when the dispatcher returns — after its locks are released and
    after its post-lock secret report — on every exit of the dispatcher and
    exactly once per replaced agent; so an entry of that agent's pool still
    locked at the return is force-closed no earlier than one drain timeout
    after the return, and every replaced agent's pool is closed. The bound is
    the pool's lock wait, not transport I/O nor the agent's pre-pool settle.
    Outside a dispatch the drain starts at once.

Probe shape (the survey's, refined by the red-case specification): the REAL
``reload.dispatch("full")`` -> real ``reload_full`` -> real policies cascade ->
real ``reload_agents`` -> real ``reload_executors`` (with a real
``asyncio.to_thread`` registry load as the post-swap work) -> real per-role
pass -> real ``_schedule_agent_close`` -> the real ``Agent.aclose`` body ->
a real ``SdkClientPool``. Fakes ONLY at the SDK-client boundary and at the
loaders the reload suites already stub. The 120 s production fuse is scaled
through ``SdkClientPool.aclose.__kwdefaults__`` — never by editing a constant.

Assertions are counts, identities and intervals, never statuses.
"""
from __future__ import annotations

import asyncio
import contextvars
import time
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import reload as reload_mod

pytestmark = pytest.mark.asyncio

# Scaled fuse. EPS absorbs scheduling jitter on a loaded xdist runner; there is
# deliberately NO upper timing bound — a late cut is correct, an early one is
# the defect.
FUSE = 0.6
EPS = 0.02
POST_SWAP = 0.9          # real to_thread work after the first swap, > FUSE
WATCHDOG = 30.0          # a fired watchdog is a diagnosis, never an intended red


class _RecordingTaskSet(set):
    """Stand-in for ``reload._AGENT_CLOSE_TASKS`` that keeps a CUMULATIVE
    record. The production set SHRINKS (the done-callback discards), so its
    length is not a historical count of close-task creations."""

    def __init__(self, observer) -> None:
        super().__init__()
        self._observer = observer
        self.creations: list[dict] = []

    def add(self, task) -> None:                     # noqa: D102
        self.creations.append(self._observer(task))
        super().add(task)


class _FakeSdkClient:
    """The SDK boundary. Records the moment the transport is cut and whether
    the entry's lock was still held (= the turn was still in flight)."""

    def __init__(self, harness, name, holder) -> None:
        self._h = harness
        self._name = name
        self._holder = holder

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        entry = self._holder.get("entry")
        self._h.disconnects.append(
            (self._name, time.monotonic(),
             bool(entry is not None and entry.lock.locked())))
        self._h.disconnected.setdefault(self._name, asyncio.Event()).set()


class _Harness:
    """Every observation this file makes, captured in callbacks and asserted
    OUTSIDE the dispatch so production exception handling cannot swallow an
    assertion failure."""

    def __init__(self) -> None:
        self.force_closes: list[tuple[str, float, bool]] = []
        self.disconnects: list[tuple[str, float, bool]] = []
        self.disconnected: dict[str, asyncio.Event] = {}
        self.aclose_entered: list[tuple[str, float]] = []
        self.retired: list[str] = []          # close-capable objects handed to the scheduler
        self.constructed: list[str] = []
        self.executor_loads = 0
        self.probe_entered = asyncio.Event()
        self.probe_release = asyncio.Event()
        self.probe_completed = False
        self.creations_at_probe_entry: int | None = None
        self.rw_held_at_probe_entry: bool | None = None
        self.measured: dict[str, dict] = {}
        self.pools: list = []

    # -- snapshots ---------------------------------------------------------
    def _rw_held(self) -> bool:
        rw = reload_mod._GLOBAL_RW
        if rw is None:
            return False
        return bool(rw._writer or rw._readers)

    def _scope_lock_held(self) -> bool:
        return any(lock.locked() for lock in reload_mod._LOCKS.values())

    def observe_creation(self, task) -> dict:
        return {
            "t": time.monotonic(),
            "rw_held": self._rw_held(),
            "scope_lock_held": self._scope_lock_held(),
            "probe_entered": self.probe_entered.is_set(),
            "probe_completed": self.probe_completed,
            "task": task,
        }

    # -- fixtures ----------------------------------------------------------
    async def measured_agent(self, name: str):
        """A real ``Agent`` body over a real ``SdkClientPool`` whose single
        entry is locked = a turn in flight for the whole test."""
        from agent import Agent
        from sdk_client_pool import SdkClientPool

        holder: dict = {}

        def make_client(options):
            return _FakeSdkClient(self, name, holder)

        def on_force_close(key, entry):
            self.force_closes.append(
                (name, time.monotonic(), bool(entry.lock.locked())))

        pool = SdkClientPool(
            MagicMock(), decide=lambda *a, **k: None,
            origin_ctxvar=contextvars.ContextVar(f"origin-{name}", default=None),
            cid_ctxvar=contextvars.ContextVar(f"cid-{name}", default=None),
            engagement_ctxvar=contextvars.ContextVar(f"eng-{name}", default=None),
            make_client=make_client,
            on_force_close=on_force_close,
        )
        self.pools.append(pool)
        entry = await pool._entry_stub("telegram-1")
        await entry.open()
        await entry.lock.acquire()
        holder["entry"] = entry

        agent = Agent.__new__(Agent)
        agent._pool = pool
        agent._bg_tasks = set()
        agent._unsub_reset = lambda: None
        agent.handle_message = AsyncMock()
        # ``active_plugin_binding`` is a read-only property over this snapshot.
        agent._plugin_snapshot = None
        real_aclose = agent.aclose

        async def observed_aclose():
            self.aclose_entered.append((name, time.monotonic()))
            await real_aclose()

        agent.aclose = observed_aclose
        agent._measured_name = name
        self.measured[name] = {"pool": pool, "entry": entry, "agent": agent}
        self.disconnected.setdefault(name, asyncio.Event())
        return agent

    def standin(self, name: str):
        """A replacement the reload can install and retire, with NO ``aclose``
        — the shape the reload suites already use."""
        return SimpleNamespace(
            handle_message=AsyncMock(), active_plugin_binding={},
            _measured_name=name,
        )

    async def release_all(self) -> None:
        for rec in self.measured.values():
            if rec["entry"].lock.locked():
                rec["entry"].lock.release()


async def _install(h, monkeypatch, tmp_path, *, replacements, live=None):
    """Wire a one-resident runtime whose live agent is measured, with the real
    handlers in place. ``replacements`` is the ordered list of objects
    ``_construct_agent`` hands back, one per construction the cascade makes."""
    from sdk_client_pool import SdkClientPool
    from runtime import CasaRuntime

    # Fresh dispatcher state: a module-global asyncio.Lock created in another
    # test's loop breaks under contention here.
    monkeypatch.setattr(reload_mod, "_GLOBAL_RW", None)
    monkeypatch.setattr(reload_mod, "_LOCKS", {})
    monkeypatch.setattr(
        reload_mod, "_AGENT_CLOSE_TASKS", _RecordingTaskSet(h.observe_creation))
    monkeypatch.setitem(
        SdkClientPool.aclose.__kwdefaults__, "drain_timeout", FUSE)

    agents_dir = tmp_path / "agents"
    (agents_dir / "assistant").mkdir(parents=True)
    new_cfg = SimpleNamespace(
        role="assistant", character=SimpleNamespace(name="A2", card=""),
        triggers=[], channels=[])
    monkeypatch.setattr(
        "agent_loader.load_agent_from_dir", lambda *a, **kw: new_cfg)
    monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
    monkeypatch.setattr("plugin_registry.reload_snapshot", lambda: None)
    import tools as tools_mod
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", lambda *a, **k: None)
    monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible", AsyncMock())

    runtime = CasaRuntime(
        agents={}, role_configs={}, specialist_registry=MagicMock(),
        executor_registry=MagicMock(), engagement_registry=MagicMock(),
        agent_registry=MagicMock(), trigger_registry=MagicMock(),
        mcp_registry=MagicMock(), session_registry=MagicMock(),
        channel_manager=MagicMock(), bus=MagicMock(),
        engagement_driver=MagicMock(), claude_code_driver=MagicMock(),
        policy_lib=MagicMock(), config_dir=str(tmp_path),
        agents_dir=str(agents_dir), home_root="/x/home",
        defaults_root="/opt/casa",
    )
    runtime.role_configs["assistant"] = SimpleNamespace(
        role="assistant", character=SimpleNamespace(name="A", card=""))
    runtime.specialist_registry.all_configs.return_value = {}
    runtime.agents["assistant"] = (
        live if live is not None else await h.measured_agent("original"))

    def _executor_load():
        h.executor_loads += 1
        time.sleep(POST_SWAP)              # real to_thread work after the swap

    runtime.executor_registry.load = _executor_load

    queue = list(replacements)

    def _construct(*a, **kw):
        obj = queue.pop(0)
        h.constructed.append(getattr(obj, "_measured_name", "?"))
        return obj

    monkeypatch.setattr(reload_mod, "_construct_agent", _construct)

    real_sched = reload_mod._schedule_agent_close

    def _observed_sched(old_agent, **kw):
        if getattr(old_agent, "aclose", None) is not None:
            h.retired.append(getattr(old_agent, "_measured_name", "?"))
        return real_sched(old_agent, **kw)

    monkeypatch.setattr(reload_mod, "_schedule_agent_close", _observed_sched)

    async def _probe(snapshot):
        h.creations_at_probe_entry = len(reload_mod._AGENT_CLOSE_TASKS.creations)
        h.rw_held_at_probe_entry = h._rw_held()
        h.probe_entered.set()
        await h.probe_release.wait()
        h.probe_completed = True
        return {}

    monkeypatch.setattr(reload_mod, "_trigger_secret_probe", _probe)
    return runtime


async def _drive(h, runtime, *, cancel_at):
    """Run the dispatch in its own task and timestamp its exit INSIDE that
    task — timestamping after awaiting the driver from another task would
    shorten the measured interval by a scheduling hop."""
    state: dict = {"envelope": None, "t_exit": None, "cancelled": False}

    async def _driver():
        try:
            state["envelope"] = await reload_mod.dispatch("full", runtime=runtime)
            state["t_exit"] = time.monotonic()
        except asyncio.CancelledError:
            state["t_exit"] = time.monotonic()
            state["cancelled"] = True
            raise

    task = asyncio.create_task(_driver())
    if cancel_at == "handler":
        await asyncio.wait_for(h.handler_entered.wait(), WATCHDOG)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif cancel_at == "probe":
        await asyncio.wait_for(h.probe_entered.wait(), WATCHDOG)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await asyncio.wait_for(h.probe_entered.wait(), WATCHDOG)
        h.probe_release.set()
        await asyncio.wait_for(asyncio.shield(task), WATCHDOG)
    return state


ARMS = {
    # arm: (measured replacements, standin replacements, exit kind)
    "full_single":  dict(measured_repl=0, standins=3, exit="ok"),
    "full_fanout":  dict(measured_repl=2, standins=1, exit="ok"),
    "reload_error": dict(measured_repl=0, standins=1, exit="reload_error"),
    "unexpected":   dict(measured_repl=0, standins=1, exit="unexpected"),
    "cancel_handler": dict(measured_repl=0, standins=1, exit="cancel_handler"),
    # cancel_probe keeps the REAL cascade: the cancellation must land in the
    # post-lock probe, so nothing may block earlier.
    "cancel_probe":   dict(measured_repl=0, standins=3, exit="cancel_probe"),
}


@pytest.mark.parametrize("arm", list(ARMS))
async def test_replaced_pool_gets_full_drain_window_at_dispatch_exit(
    arm, monkeypatch, tmp_path,
):
    """The declared invariant, on six exits of the dispatcher.

    ``full_single``, ``full_fanout`` and ``reload_error`` are the red case;
    the remaining arms pin the declared exit coverage."""
    from sdk_client_pool import SdkClientPool

    spec = ARMS[arm]
    h = _Harness()
    h.handler_entered = asyncio.Event()

    replacements = []
    for i in range(spec["measured_repl"]):
        replacements.append(await h.measured_agent(f"repl{i + 1}"))
    for i in range(spec["standins"]):
        replacements.append(h.standin(f"standin{i + 1}"))

    runtime = await _install(h, monkeypatch, tmp_path, replacements=replacements)

    kind = spec["exit"]
    if kind not in ("ok", "cancel_probe"):
        async def _agents_handler(rt, *, role=None, **kw):
            h.handler_entered.set()
            if kind == "reload_error":
                raise reload_mod.ReloadError("sentinel_kind", "sentinel")
            if kind == "unexpected":
                raise RuntimeError("sentinel")
            await asyncio.Event().wait()          # cancelled from the test
        monkeypatch.setitem(reload_mod._HANDLERS, "agents", _agents_handler)

    cancel_at = {"cancel_handler": "handler", "cancel_probe": "probe"}.get(kind)
    state = await _drive(h, runtime, cancel_at=cancel_at)
    t_exit = state["t_exit"]

    expected = ["original"] + [f"repl{i + 1}" for i in range(spec["measured_repl"])]
    reached_probe = kind not in ("cancel_handler",)
    completed_probe = kind in ("ok", "reload_error", "unexpected")

    # 1. Fixture/path — a swallowed cascade failure must not masquerade as
    #    the red case.
    if kind == "ok":
        assert state["envelope"]["status"] == "ok", state["envelope"]
        assert h.constructed == [
            r._measured_name for r in replacements], h.constructed
        assert h.executor_loads == 1
    elif kind == "reload_error":
        assert state["envelope"]["status"] == "error"
        assert state["envelope"]["kind"] == "sentinel_kind"
        assert h.executor_loads == 0
    elif kind == "unexpected":
        assert state["envelope"]["status"] == "error"
        assert state["envelope"]["kind"] == "unexpected"
        assert h.executor_loads == 0
    else:
        assert state["cancelled"] is True
        assert state["envelope"] is None
    assert Counter(h.retired) == Counter(expected), (h.retired, expected)

    creations = reload_mod._AGENT_CLOSE_TASKS.creations

    # 2. P — nothing was started before the dispatcher's exit work finished.
    #    Deterministic, and it separates the exit from the swap AND from the
    #    lock release.
    if reached_probe:
        assert h.creations_at_probe_entry == 0, h.creations_at_probe_entry
        assert h.rw_held_at_probe_entry is False

    # 3. C — complete, exactly-once scheduling AT the exit, outside both locks.
    assert len(creations) == len(expected), creations
    for rec in creations:
        assert rec["rw_held"] is False
        assert rec["scope_lock_held"] is False
        if completed_probe:
            assert rec["probe_completed"] is True

    # 4. B — no cut before the exit.
    assert [d for d in h.disconnects if d[1] < t_exit] == []

    # 5. T — each still-locked entry gets a FULL lock-wait window, counted
    #    from the exit, and is still hosting its turn when it is cut.
    for name in expected:
        await asyncio.wait_for(h.disconnected[name].wait(), WATCHDOG)
    forced = Counter(n for n, _, _ in h.force_closes)
    cut = Counter(n for n, _, _ in h.disconnects)
    entered = Counter(n for n, _ in h.aclose_entered)
    for name in expected:
        assert forced[name] == 1, h.force_closes
        assert cut[name] == 1, h.disconnects
        assert entered[name] == 1, h.aclose_entered
        t_force = [t for n, t, _ in h.force_closes if n == name][0]
        t_cut = [t for n, t, _ in h.disconnects if n == name][0]
        assert t_force - t_exit >= FUSE - EPS, (name, t_force - t_exit)
        assert t_cut - t_exit >= FUSE - EPS, (name, t_cut - t_exit)
        assert [lk for n, _, lk in h.force_closes if n == name] == [True]
        assert [lk for n, _, lk in h.disconnects if n == name] == [True]

    # 6. E — teardown actually completed for every recorded agent.
    tasks = [rec["task"] for rec in creations]
    results = await asyncio.wait_for(
        asyncio.gather(*tasks, return_exceptions=True), WATCHDOG)
    assert [r for r in results if isinstance(r, BaseException)] == []
    for name in expected:
        pool = h.measured[name]["pool"]
        assert pool.stats()["entries"] == 0
        assert pool not in SdkClientPool._instances

    await h.release_all()
    for pool in h.pools:
        if pool in SdkClientPool._instances:
            SdkClientPool._instances.remove(pool)


# --- mechanism pins -------------------------------------------------------


async def _dispatch_agents_with(h, monkeypatch, tmp_path, handler):
    runtime = await _install(
        h, monkeypatch, tmp_path, replacements=[], live=h.standin("live"))
    monkeypatch.setitem(reload_mod._HANDLERS, "agents", handler)
    task = asyncio.create_task(reload_mod.dispatch("agents", runtime=runtime))
    await asyncio.wait_for(h.probe_entered.wait(), WATCHDOG)
    at_probe = len(reload_mod._AGENT_CLOSE_TASKS.creations)
    h.probe_release.set()
    envelope = await asyncio.wait_for(task, WATCHDOG)
    return envelope, at_probe


async def test_dispatch_close_deduplicates_by_identity(monkeypatch, tmp_path):
    """One replaced agent recorded twice inside one dispatch is closed ONCE —
    a second window would disclose a second drain that does not exist."""
    h = _Harness()
    entered = []
    agent = SimpleNamespace(active_plugin_binding={})

    async def _aclose():
        entered.append(time.monotonic())

    agent.aclose = _aclose

    async def _handler(rt, *, role=None, **kw):
        reload_mod._schedule_agent_close(agent)
        reload_mod._schedule_agent_close(agent)
        return []

    envelope, at_probe = await _dispatch_agents_with(
        h, monkeypatch, tmp_path, _handler)

    assert envelope["status"] == "ok"
    assert at_probe == 0
    creations = reload_mod._AGENT_CLOSE_TASKS.creations
    assert len(creations) == 1, creations
    await asyncio.wait_for(
        asyncio.gather(*[c["task"] for c in creations]), WATCHDOG)
    assert len(entered) == 1


async def test_inherited_closed_ledger_starts_a_late_close_at_once(
    monkeypatch, tmp_path,
):
    """A continuation spawned inside a dispatch that swaps AFTER the dispatch
    has returned must start its close immediately — a deferral that lost it
    would leave a pool never closed."""
    h = _Harness()
    released = asyncio.Event()
    entered = []
    late = SimpleNamespace(active_plugin_binding={})

    async def _aclose():
        entered.append(time.monotonic())

    late.aclose = _aclose

    child_done = asyncio.Event()

    async def _child():
        await released.wait()
        reload_mod._schedule_agent_close(late)
        child_done.set()

    async def _handler(rt, *, role=None, **kw):
        asyncio.create_task(_child())          # inherits the dispatch context
        return []

    envelope, at_probe = await _dispatch_agents_with(
        h, monkeypatch, tmp_path, _handler)
    assert envelope["status"] == "ok"
    assert at_probe == 0
    assert len(reload_mod._AGENT_CLOSE_TASKS.creations) == 0

    released.set()
    await asyncio.wait_for(child_done.wait(), WATCHDOG)
    creations = reload_mod._AGENT_CLOSE_TASKS.creations
    assert len(creations) == 1, creations
    await asyncio.wait_for(
        asyncio.gather(*[c["task"] for c in creations]), WATCHDOG)
    assert len(entered) == 1


async def test_close_outside_dispatch_starts_immediately(monkeypatch, tmp_path):
    """Outside any dispatch — shutdown, a direct handler call — the drain
    still starts at the call, unchanged."""
    h = _Harness()
    monkeypatch.setattr(
        reload_mod, "_AGENT_CLOSE_TASKS", _RecordingTaskSet(h.observe_creation))
    entered = []
    agent = SimpleNamespace(active_plugin_binding={})

    async def _aclose():
        entered.append(time.monotonic())

    agent.aclose = _aclose

    reload_mod._schedule_agent_close(agent)
    creations = reload_mod._AGENT_CLOSE_TASKS.creations
    assert len(creations) == 1                 # created before any yield
    await asyncio.wait_for(
        asyncio.gather(*[c["task"] for c in creations]), WATCHDOG)
    assert len(entered) == 1


async def test_draining_disclosure_spans_the_swap_to_the_close(
    monkeypatch, tmp_path,
):
    """``runtime.draining`` is recorded at the SWAP — where the site supplies
    runtime+role — and dropped when the close completes, so verify discloses
    the old turn for the whole deferral, not only from the dispatcher's exit."""
    h = _Harness()
    released = asyncio.Event()
    old = SimpleNamespace(active_plugin_binding={"plugin": "p", "role": "assistant"})

    async def _aclose():
        await released.wait()

    old.aclose = _aclose

    runtime = await _install(
        h, monkeypatch, tmp_path,
        replacements=[h.standin("standin1")], live=old)

    task = asyncio.create_task(
        reload_mod.dispatch("agent", runtime=runtime, role="assistant"))
    await asyncio.wait_for(h.probe_entered.wait(), WATCHDOG)
    # Recorded at the swap, before the close is ever started.
    assert len(runtime.draining) == 1, runtime.draining
    assert len(reload_mod._AGENT_CLOSE_TASKS.creations) == 0
    h.probe_release.set()
    envelope = await asyncio.wait_for(task, WATCHDOG)

    assert envelope["status"] == "ok"
    creations = reload_mod._AGENT_CLOSE_TASKS.creations
    assert len(creations) == 1, creations
    assert len(runtime.draining) == 1          # still draining after the exit
    released.set()
    await asyncio.wait_for(
        asyncio.gather(*[c["task"] for c in creations]), WATCHDOG)
    await asyncio.sleep(0)                     # let the done-callback run
    assert runtime.draining == []
