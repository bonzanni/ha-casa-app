"""Task 10 checkpoint 2f: ENTRY-POINT-ONLY manual-reload fencing (spec §3.1).

Every caller that dispatches a FULL reload (the casa_reload tool + the
/admin/reload route) must acquire _PLUGIN_TOOLS_LOCK BEFORE dispatch("full"),
establishing the global lock order `_PLUGIN_TOOLS_LOCK -> reload writer/reader
lock` for every path. The fence is NEVER placed inside reload.py — that would
recreate the AB/BA deadlock against reload's own global writer/reader lock,
which a bundle transaction's dispatch("agent") already takes on the reader side
while holding _PLUGIN_TOOLS_LOCK.
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_plugin_tools_lock(monkeypatch):
    """_PLUGIN_TOOLS_LOCK is a module-level asyncio.Lock() that binds to the
    first event loop it touches; pytest-asyncio runs each test in a fresh loop.
    Rebind it to a new lock on the CURRENT loop so cross-test loop reuse never
    raises 'bound to a different event loop'."""
    import tools as tools_mod
    monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
    yield


class _JsonReq:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


async def test_full_reload_is_fenced_behind_plugin_tools_lock(monkeypatch) -> None:
    """A concurrent FULL reload cannot dispatch while a bundle op holds
    _PLUGIN_TOOLS_LOCK — it is serialized behind the lock."""
    import reload as reload_mod
    import tools as tools_mod
    from internal_handlers import build_admin_reload_handler

    log: list = []

    async def _fake_dispatch(scope, *, runtime, role=None, include_env=False):
        log.append(scope)
        return {"status": "ok", "scope": scope}

    monkeypatch.setattr(reload_mod, "dispatch", _fake_dispatch)
    handler = build_admin_reload_handler(runtime=object())

    await tools_mod._PLUGIN_TOOLS_LOCK.acquire()
    try:
        task = asyncio.create_task(handler(_JsonReq({"scope": "full"})))
        await asyncio.sleep(0.02)
        assert log == []                       # fenced out while the lock is held
    finally:
        tools_mod._PLUGIN_TOOLS_LOCK.release()
    await task
    assert log == ["full"]                      # ran once the lock was released


async def test_non_full_reload_is_not_fenced(monkeypatch) -> None:
    """A non-full scope must NOT be fenced (it never reloads the plugin
    snapshot) — it dispatches even while the plugin lock is held."""
    import reload as reload_mod
    import tools as tools_mod
    from internal_handlers import build_admin_reload_handler

    log: list = []

    async def _fake_dispatch(scope, *, runtime, role=None, include_env=False):
        log.append(scope)
        return {"status": "ok", "scope": scope}

    monkeypatch.setattr(reload_mod, "dispatch", _fake_dispatch)
    handler = build_admin_reload_handler(runtime=object())

    async with tools_mod._PLUGIN_TOOLS_LOCK:
        await handler(_JsonReq({"scope": "agent", "role": "mtg"}))
        assert log == ["agent"]                 # ran without waiting on the lock


async def test_bundle_agent_dispatch_completes_while_holding_plugin_lock(monkeypatch) -> None:
    """Deadlock regression: a bundle op holding _PLUGIN_TOOLS_LOCK can still
    await its OWN dispatch("agent") to completion, while a concurrent FULL
    reload is fenced OUT (blocked on the plugin lock, so it never holds reload's
    writer lock while waiting) — both complete, in order, with no deadlock."""
    import reload as reload_mod
    import tools as tools_mod
    from internal_handlers import build_admin_reload_handler

    order: list = []

    async def _fake_dispatch(scope, *, runtime, role=None, include_env=False):
        order.append(scope)
        return {"status": "ok", "scope": scope}

    monkeypatch.setattr(reload_mod, "dispatch", _fake_dispatch)
    handler = build_admin_reload_handler(runtime=object())

    async with tools_mod._PLUGIN_TOOLS_LOCK:
        full_task = asyncio.create_task(handler(_JsonReq({"scope": "full"})))
        await asyncio.sleep(0.02)
        assert order == []                      # full reload fenced behind the lock
        # the bundle op's own agent reload is NOT fenced — runs to completion
        await reload_mod.dispatch("agent", runtime=object(), role="mtg")
        assert order == ["agent"]

    await asyncio.wait_for(full_task, timeout=1.0)   # no deadlock
    assert order == ["agent", "full"]                # full reload ran after release


# ---------------------------------------------------------------------------
# #489: full-scope reload with include_env self-deadlocked — the entry point
# holds _PLUGIN_TOOLS_LOCK across dispatch("full"), and reload_full's
# include_env arm reaches reload_plugin_env, which re-acquired the SAME
# non-reentrant lock for its health block. The guard makes that nesting legal
# for one logical operation while preserving cross-task mutual exclusion.
# ---------------------------------------------------------------------------


async def test_full_reload_include_env_completes(monkeypatch, tmp_path) -> None:
    """#489 pinning test: scope="full" + include_env through a fenced entry
    point must COMPLETE. Pre-fix this deadlocked deterministically: the entry
    point held _PLUGIN_TOOLS_LOCK, and the REAL plugin_env handler blocked
    forever re-acquiring it. The full handler here mirrors exactly
    reload_full's include_env arm (the arm itself is pinned by
    test_reload.py::test_include_env_calls_plugin_env); plugin_env runs REAL."""
    import reload as reload_mod
    import tools as tools_mod
    import plugin_env_conf as pec
    from internal_handlers import build_admin_reload_handler

    async def _mini_full(runtime, *, role=None, include_env=False):
        actions: list = []
        if include_env:
            sub = await reload_mod._HANDLERS["plugin_env"](runtime, role=None)
            actions += [f"plugin_env:{a}" for a in sub]
        return actions

    monkeypatch.setitem(reload_mod._HANDLERS, "full", _mini_full)
    # Real reload_plugin_env seams: empty env conf; stub the health I/O.
    monkeypatch.setattr(pec, "PLUGIN_ENV_CONF_PATH", tmp_path / "plugin-env.conf")
    monkeypatch.setattr(reload_mod, "_PLUGIN_ENV_LAST_KEYS", set())
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", lambda issues: None)

    async def _notify():
        return None

    monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible", _notify)

    handler = build_admin_reload_handler(runtime=object())
    resp = await asyncio.wait_for(
        handler(_JsonReq({"scope": "full", "include_env": True})), timeout=30.0)
    assert resp.status == 200


async def test_plugin_tools_guard_reentrant_within_one_operation() -> None:
    """The guard is reentrant for the SAME logical operation (nested guard
    completes without a second acquire)…"""
    import tools as tools_mod

    async with tools_mod._plugin_tools_guard():
        assert tools_mod._PLUGIN_TOOLS_LOCK.locked()
        async with tools_mod._plugin_tools_guard():      # nested: must not block
            assert tools_mod._PLUGIN_TOOLS_LOCK.locked()
        assert tools_mod._PLUGIN_TOOLS_LOCK.locked()     # inner exit releases nothing
    assert not tools_mod._PLUGIN_TOOLS_LOCK.locked()


async def test_plugin_tools_guard_excludes_other_tasks() -> None:
    """…while preserving raw-lock mutual exclusion ACROSS tasks: a second
    task's guard waits until the first releases, and a guard waits on a
    raw-lock holder too (one underlying lock, both directions)."""
    import tools as tools_mod

    order: list = []

    async def _other():
        async with tools_mod._plugin_tools_guard():
            order.append("other")

    async with tools_mod._plugin_tools_guard():
        task = asyncio.create_task(_other())
        await asyncio.sleep(0.02)
        assert order == []                       # excluded while we hold it
        order.append("first")
    await asyncio.wait_for(task, timeout=5.0)
    assert order == ["first", "other"]

    # A guard also queues behind a RAW acquisition (mixed usage stays safe).
    await tools_mod._PLUGIN_TOOLS_LOCK.acquire()
    try:
        task2 = asyncio.create_task(_other())
        await asyncio.sleep(0.02)
        assert len(order) == 2                   # still excluded
    finally:
        tools_mod._PLUGIN_TOOLS_LOCK.release()
    await asyncio.wait_for(task2, timeout=5.0)
    assert order == ["first", "other", "other"]


# ---------------------------------------------------------------------------
# #706 red cases (INV-CFG-011). `dispatch` takes the reload RW lock for EVERY
# scope and holds it across the handler (reload.py:293-305), and the executors
# and plugin_env handlers acquire the plugin-tools guard UNDERNEATH that held
# reader (reload.py:1868, reload.py:1486) — the reverse of the documented
# `_PLUGIN_TOOLS_LOCK -> reload RW lock` order the entry points state and, at
# the base commit, fence for scope="full" ONLY. Two tasks, no timeout on
# either side:
#
#   Y: entry point, scope="executors"/"plugin_env" -> RW reader -> wants guard
#   X: holds the guard                             -> dispatch("full") -> wants RW writer
#
# ONE red case per inversion site, so removing either scope from the fenced
# set identifies the uncovered half; a combined mutation would not.
# ---------------------------------------------------------------------------


@pytest.fixture
def _fresh_reload_locks(monkeypatch):
    """Reload's global RW lock and per-scope locks bind to the loop that first
    touches them; give each test its own, and clear any guard owner left by an
    earlier test in this process."""
    import reload as reload_mod
    import tools as tools_mod
    monkeypatch.setattr(reload_mod, "_GLOBAL_RW", None)
    monkeypatch.setattr(reload_mod, "_LOCKS", {})
    monkeypatch.setattr(tools_mod, "_PLUGIN_TOOLS_LOCK_OWNER", None)
    yield


@pytest.fixture
def configurator_origin():
    """casa_reload refuses an unprivileged caller before it reaches dispatch."""
    import agent as agent_mod
    tok = agent_mod.origin_var.set({"role": "configurator"})
    try:
        yield
    finally:
        agent_mod.origin_var.reset(tok)


def _probe_guard(monkeypatch):
    """Wrap `tools._plugin_tools_guard` so the FIRST attempt made by the probed
    task records how many reload READERS are live at that moment and signals,
    then delegates to the real guard.

    That count IS the invariant: acquiring the plugin lock at the entry point
    means zero readers are held when the attempt is made; acquiring it inside
    the handler means the attempting task is itself holding one.
    """
    from contextlib import asynccontextmanager

    import reload as reload_mod
    import tools as tools_mod

    real = tools_mod._plugin_tools_guard
    probe: dict = {"task": None, "readers": None, "attempted": asyncio.Event()}

    @asynccontextmanager
    async def _wrapped():
        if (asyncio.current_task() is probe["task"]
                and probe["readers"] is None):
            probe["readers"] = reload_mod._global_rw()._readers
            probe["attempted"].set()
        async with real():
            yield

    monkeypatch.setattr(tools_mod, "_plugin_tools_guard", _wrapped)
    return probe


def _count_health_io(monkeypatch):
    """Counted, in-memory stand-ins for the health regen + notify the two
    handlers perform under the guard (the real ones write /data)."""
    import tools as tools_mod
    regen: list = []
    notify: list = []

    def _regen(issues):
        regen.append(issues)

    async def _notify():
        notify.append(True)

    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", _regen)
    monkeypatch.setattr(tools_mod, "_notify_plugin_health_if_possible", _notify)
    return regen, notify


def _counted_full_handler(monkeypatch):
    """X's counterparty is a REAL fenced full-scope dispatch; only the full
    handler's body is replaced, so the RW writer acquisition stays real."""
    import reload as reload_mod
    calls: list = []

    async def _full(runtime, *, role=None, include_env=False):
        calls.append(True)
        return []

    monkeypatch.setitem(reload_mod._HANDLERS, "full", _full)
    return calls


async def _race_against_a_guard_holder(monkeypatch, *, runtime, start_y):
    """Run the X/Y interleaving and return the measured signature."""
    import reload as reload_mod
    import tools as tools_mod

    probe = _probe_guard(monkeypatch)
    holding = asyncio.Event()
    returns: list = []

    async def _x():
        # The documented entry-point shape: guard FIRST, then the reload lock.
        async with tools_mod._plugin_tools_guard():
            holding.set()
            await asyncio.wait_for(probe["attempted"].wait(), timeout=3.0)
            await reload_mod.dispatch("full", runtime=runtime)

    x_task = asyncio.create_task(_x())
    await asyncio.wait_for(holding.wait(), timeout=3.0)

    y_task = asyncio.create_task(start_y(returns))
    probe["task"] = y_task           # set before y_task's first step runs

    done, pending = await asyncio.wait({x_task, y_task}, timeout=3.0)
    rw = reload_mod._global_rw()
    readers_at_end, queued_at_end = rw._readers, len(rw._queue)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    return {
        "readers_at_guard_attempt": probe["readers"],
        "done": len(done), "pending": len(pending),
        "readers_at_end": readers_at_end, "queued_at_end": queued_at_end,
        "returns": len(returns),
        "errors": [t.exception() for t in done if not t.cancelled()],
    }


async def test_casa_reload_executors_takes_plugin_guard_before_rw_reader(
    monkeypatch, _fresh_reload_locks, configurator_origin,
) -> None:
    """#706 red case 1 of 2 — the `executors` inversion site (reload.py:1868),
    through the casa_reload tool entry point, with the REAL reload_executors.

    Pre-fix signature: the guard attempt is made with ONE reader held, X's
    writer sits queued behind it, and neither task completes.
    """
    import agent as agent_mod
    import plugin_setup_episodes
    import reload as reload_mod
    from tools import casa_reload

    regen, notify = _count_health_io(monkeypatch)
    full_calls = _counted_full_handler(monkeypatch)
    monkeypatch.setattr(plugin_setup_episodes, "kick", lambda: None)
    monkeypatch.setitem(
        reload_mod._HANDLERS, "executors", reload_mod.reload_executors)

    class _Registry:
        failed_types: set = set()

        def load(self):
            return None

        def definition_any(self, name):
            return None

    class _Runtime:
        executor_registry = _Registry()
        executor_cc_policies = None
        role_configs: dict = {}

    runtime = _Runtime()
    monkeypatch.setattr(agent_mod, "active_runtime", runtime, raising=False)

    async def _start_y(returns):
        returns.append(await casa_reload.handler({"scope": "executors"}))

    got = await _race_against_a_guard_holder(
        monkeypatch, runtime=runtime, start_y=_start_y)

    assert (got["readers_at_guard_attempt"], got["done"], got["pending"],
            got["readers_at_end"], got["queued_at_end"],
            len(full_calls), got["returns"], len(regen), len(notify),
            ) == (0, 2, 0, 0, 0, 1, 1, 1, 1), got
    assert [e for e in got["errors"] if e is not None] == []


async def test_admin_reload_plugin_env_takes_plugin_guard_before_rw_reader(
    monkeypatch, _fresh_reload_locks,
) -> None:
    """#706 red case 2 of 2 — the `plugin_env` inversion site (reload.py:1486),
    the half the issue never named, through the /admin/reload entry point, with
    the REAL reload_plugin_env.

    Same pre-fix signature as red case 1, reached by a different entry point
    and a different handler.
    """
    import plugin_env_conf
    import plugin_setup_episodes
    import reload as reload_mod
    import secrets_resolver
    from internal_handlers import build_admin_reload_handler

    regen, notify = _count_health_io(monkeypatch)
    full_calls = _counted_full_handler(monkeypatch)
    monkeypatch.setattr(plugin_setup_episodes, "kick", lambda: None)
    monkeypatch.setitem(
        reload_mod._HANDLERS, "plugin_env", reload_mod.reload_plugin_env)
    monkeypatch.setattr(plugin_env_conf, "read_entries", lambda: {})
    monkeypatch.setattr(secrets_resolver, "invalidate_cache", lambda: None)
    monkeypatch.setattr(reload_mod, "_PLUGIN_ENV_LAST_KEYS", set())

    runtime = object()
    handler = build_admin_reload_handler(runtime=runtime)

    async def _start_y(returns):
        returns.append(await handler(_JsonReq({"scope": "plugin_env"})))

    got = await _race_against_a_guard_holder(
        monkeypatch, runtime=runtime, start_y=_start_y)

    assert (got["readers_at_guard_attempt"], got["done"], got["pending"],
            got["readers_at_end"], got["queued_at_end"],
            len(full_calls), got["returns"], len(regen), len(notify),
            ) == (0, 2, 0, 0, 0, 1, 1, 1, 1), got
    assert [e for e in got["errors"] if e is not None] == []
