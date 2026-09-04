"""Task 10 checkpoint 2f: ENTRY-POINT-ONLY manual-reload fencing (spec §3.1).

Every caller that dispatches a reload whose handler reaches the plugin-tools
lock — scopes `full`, `executors` and `plugin_env` (#706), the set
`tools._PLUGIN_TOOLS_RELOAD_SCOPES` — must acquire _PLUGIN_TOOLS_LOCK BEFORE
dispatch, establishing the global lock order `_PLUGIN_TOOLS_LOCK -> reload
writer/reader lock` for every path (INV-CFG-011). The two entry points are the
casa_reload tool and the /admin/reload route, and they share one classification
helper so they cannot drift apart.

The fence is NEVER placed inside reload.py BENEATH its writer/reader lock —
that is the AB/BA deadlock against the reader side a bundle transaction's
dispatch("agent") already takes while holding _PLUGIN_TOOLS_LOCK. Until #706
the fence covered `full` only, so a dispatched `executors` or `plugin_env`
reload took the RW reader and then asked for the plugin lock underneath it,
which deadlocked against any full-scope entry with no timeout on either side.
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
    """An UNFENCED scope must not be fenced — it dispatches even while the
    plugin lock is held.

    #706: "non-full" was the right rationale only while `full` was the whole
    fenced set. `agent` is unfenced because its handler never touches the
    plugin lock, which is also why a bundle op holding that lock can complete
    its own dispatch("agent"); `executors` and `plugin_env` ARE fenced now, and
    their rows are asserted separately below.
    """
    import reload as reload_mod
    import tools as tools_mod
    from internal_handlers import build_admin_reload_handler

    log: list = []

    async def _fake_dispatch(scope, *, runtime, role=None, include_env=False):
        log.append(scope)
        return {"status": "ok", "scope": scope}

    monkeypatch.setattr(reload_mod, "dispatch", _fake_dispatch)
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", lambda extra: None)
    handler = build_admin_reload_handler(runtime=object())

    async with tools_mod._PLUGIN_TOOLS_LOCK:
        task = asyncio.create_task(
            handler(_JsonReq({"scope": "agent", "role": "mtg"})))
        # Bounded: an over-fencing regression (an unfenced scope added to
        # _PLUGIN_TOOLS_RELOAD_SCOPES) never reaches the dispatch at all, and a
        # hung CI job is a worse signal than a failed assertion.
        await asyncio.wait_for(asyncio.sleep(0.02), timeout=5.0)
        assert log == ["agent"]                 # ran without waiting on the lock
        # #824: what the entry point does AFTER the dispatch — re-derive the
        # plugin health report — does take the guard, so the handler's own
        # completion waits on this hold. The bundle transaction that made
        # `agent` unfenced is untouched: it reaches reload.dispatch DIRECTLY,
        # never an entry point (the next test pins exactly that).
        assert task.done() is False
    await asyncio.wait_for(task, timeout=5.0)


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




def _arm_executors(monkeypatch):
    """Seams for the REAL reload_executors: registry load is a no-op, no shared
    hook-policy map, no residents to fan out to. The guard site at
    reload.py:1868 stays real."""
    import agent as agent_mod
    import reload as reload_mod

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
    return runtime


def _arm_plugin_env(monkeypatch):
    """Seams for the REAL reload_plugin_env: empty env conf, no resolver cache
    drop, no remembered keys. The guard site at reload.py:1486 stays real."""
    import agent as agent_mod
    import plugin_env_conf
    import reload as reload_mod
    import secrets_resolver

    monkeypatch.setitem(
        reload_mod._HANDLERS, "plugin_env", reload_mod.reload_plugin_env)
    monkeypatch.setattr(plugin_env_conf, "read_entries", lambda: {})
    monkeypatch.setattr(secrets_resolver, "invalidate_cache", lambda: None)
    monkeypatch.setattr(reload_mod, "_PLUGIN_ENV_LAST_KEYS", set())

    runtime = object()
    monkeypatch.setattr(agent_mod, "active_runtime", runtime, raising=False)
    return runtime


def _via_casa_reload(scope):
    async def _start(returns):
        from tools import casa_reload
        returns.append(await casa_reload.handler({"scope": scope}))
    return _start


def _via_admin_route(scope, runtime):
    async def _start(returns):
        from internal_handlers import build_admin_reload_handler
        handler = build_admin_reload_handler(runtime=runtime)
        returns.append(await handler(_JsonReq({"scope": scope})))
    return _start


async def _assert_no_inversion(monkeypatch, *, runtime, start_y,
                               full_calls, regen, notify):
    got = await _race_against_a_guard_holder(
        monkeypatch, runtime=runtime, start_y=start_y)
    assert (got["readers_at_guard_attempt"], got["done"], got["pending"],
            got["readers_at_end"], got["queued_at_end"],
            len(full_calls), got["returns"], len(regen), len(notify),
            ) == (0, 2, 0, 0, 0, 1, 1, 1, 1), got
    assert [e for e in got["errors"] if e is not None] == []


# Both inversion sites x both production entry points. The scope x entry matrix
# is covered because a fix is free to fence one scope in one entry point and the
# other scope in the other: each of the two diagonal pairs alone leaves a live
# deadlock behind two green tests (acceptor's clause (a), round 1).


async def test_casa_reload_executors_takes_plugin_guard_before_rw_reader(
    monkeypatch, _fresh_reload_locks, configurator_origin,
) -> None:
    """#706 — the `executors` inversion site (reload.py:1868) through the
    casa_reload tool entry point, with the REAL reload_executors.

    Pre-fix signature: the guard attempt is made with ONE reader held, X's
    writer sits queued behind it, and neither task completes.
    """
    import plugin_setup_episodes

    regen, notify = _count_health_io(monkeypatch)
    full_calls = _counted_full_handler(monkeypatch)
    monkeypatch.setattr(plugin_setup_episodes, "kick", lambda: None)
    runtime = _arm_executors(monkeypatch)

    await _assert_no_inversion(
        monkeypatch, runtime=runtime, start_y=_via_casa_reload("executors"),
        full_calls=full_calls, regen=regen, notify=notify)


async def test_admin_reload_executors_takes_plugin_guard_before_rw_reader(
    monkeypatch, _fresh_reload_locks,
) -> None:
    """#706 — the same `executors` site through POST /admin/reload."""
    import plugin_setup_episodes

    regen, notify = _count_health_io(monkeypatch)
    full_calls = _counted_full_handler(monkeypatch)
    monkeypatch.setattr(plugin_setup_episodes, "kick", lambda: None)
    runtime = _arm_executors(monkeypatch)

    await _assert_no_inversion(
        monkeypatch, runtime=runtime,
        start_y=_via_admin_route("executors", runtime),
        full_calls=full_calls, regen=regen, notify=notify)


async def test_admin_reload_plugin_env_takes_plugin_guard_before_rw_reader(
    monkeypatch, _fresh_reload_locks,
) -> None:
    """#706 — the `plugin_env` inversion site (reload.py:1486), the half the
    issue never names, through POST /admin/reload with the REAL
    reload_plugin_env."""
    import plugin_setup_episodes

    regen, notify = _count_health_io(monkeypatch)
    full_calls = _counted_full_handler(monkeypatch)
    monkeypatch.setattr(plugin_setup_episodes, "kick", lambda: None)
    runtime = _arm_plugin_env(monkeypatch)

    await _assert_no_inversion(
        monkeypatch, runtime=runtime,
        start_y=_via_admin_route("plugin_env", runtime),
        full_calls=full_calls, regen=regen, notify=notify)


async def test_casa_reload_plugin_env_takes_plugin_guard_before_rw_reader(
    monkeypatch, _fresh_reload_locks, configurator_origin,
) -> None:
    """#706 — the same `plugin_env` site through the casa_reload tool entry
    point, which is the documented required follow-up to
    set_plugin_env_reference."""
    import plugin_setup_episodes

    regen, notify = _count_health_io(monkeypatch)
    full_calls = _counted_full_handler(monkeypatch)
    monkeypatch.setattr(plugin_setup_episodes, "kick", lambda: None)
    runtime = _arm_plugin_env(monkeypatch)

    await _assert_no_inversion(
        monkeypatch, runtime=runtime, start_y=_via_casa_reload("plugin_env"),
        full_calls=full_calls, regen=regen, notify=notify)


# ---------------------------------------------------------------------------
# #706 supporting pins: the fenced set at BOTH entry points, and the retained
# in-handler guard that an outer entry fence would otherwise mask.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["full", "executors", "plugin_env"])
@pytest.mark.parametrize("entry", ["tool", "route"])
async def test_every_fenced_scope_waits_at_every_entry_point(
    monkeypatch, configurator_origin, entry, scope,
) -> None:
    """Each of the six (entry point x fenced scope) cells waits for the plugin
    lock BEFORE dispatching, and dispatches exactly once after release.

    Six rows rather than one per scope: the classification lives in one helper
    precisely so the two entry points cannot disagree, and this is what would
    catch it if one of them stopped calling it."""
    import agent as agent_mod
    import reload as reload_mod
    import tools as tools_mod
    from internal_handlers import build_admin_reload_handler
    from tools import casa_reload

    log: list = []

    async def _fake_dispatch(scope_, *, runtime, role=None, include_env=False):
        log.append(scope_)
        return {"status": "ok", "scope": scope_}

    monkeypatch.setattr(reload_mod, "dispatch", _fake_dispatch)
    monkeypatch.setattr(agent_mod, "active_runtime", object(), raising=False)

    if entry == "tool":
        async def _call():
            await casa_reload.handler({"scope": scope})
    else:
        handler = build_admin_reload_handler(runtime=object())

        async def _call():
            await handler(_JsonReq({"scope": scope}))

    await tools_mod._PLUGIN_TOOLS_LOCK.acquire()
    try:
        task = asyncio.create_task(_call())
        await asyncio.sleep(0.02)
        assert log == []                    # fenced out while the lock is held
    finally:
        tools_mod._PLUGIN_TOOLS_LOCK.release()
    await asyncio.wait_for(task, timeout=5.0)
    assert log == [scope]                   # exactly one dispatch, after release


async def test_casa_reload_agent_scope_is_not_fenced(
    monkeypatch, configurator_origin,
) -> None:
    """The tool entry's counterpart to test_non_full_reload_is_not_fenced: an
    unfenced scope must still dispatch while the plugin lock is held, or a
    bundle op's own agent reload would deadlock behind itself."""
    import agent as agent_mod
    import reload as reload_mod
    import tools as tools_mod
    from tools import casa_reload

    log: list = []

    async def _fake_dispatch(scope_, *, runtime, role=None, include_env=False):
        log.append(scope_)
        return {"status": "ok", "scope": scope_}

    monkeypatch.setattr(reload_mod, "dispatch", _fake_dispatch)
    monkeypatch.setattr(agent_mod, "active_runtime", object(), raising=False)
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health", lambda extra: None)

    async with tools_mod._PLUGIN_TOOLS_LOCK:
        task = asyncio.create_task(
            casa_reload.handler({"scope": "agent", "role": "mtg"}))
        await asyncio.wait_for(asyncio.sleep(0.02), timeout=5.0)  # bounded, as above
        assert log == ["agent"]
        assert task.done() is False                 # #824, as above
    await asyncio.wait_for(task, timeout=5.0)


async def test_direct_reload_executors_still_serializes_its_health_regen(
    monkeypatch, _fresh_reload_locks,
) -> None:
    """The entry fence must not become the ONLY thing serializing the health
    regeneration: reload_executors' own guard (reload.py:1868) is retained, and
    a DIRECT call to the handler still waits for a plugin-tools lock another
    task holds.

    Without this, removing that guard leaves every other test green — the outer
    entry fence masks it (measured: real handler under a held lock →
    regen/notify calls 0; guard-removed → 2, with one action either way).
    reload_plugin_env's half is pinned by
    test_reload.py::test_health_regen_serialized_under_plugin_tools_lock.
    """
    import reload as reload_mod
    import tools as tools_mod

    regen, notify = _count_health_io(monkeypatch)
    runtime = _arm_executors(monkeypatch)
    done: list = []

    async def _direct():
        await reload_mod.reload_executors(runtime, role=None)
        done.append(True)

    async with tools_mod._plugin_tools_guard():
        task = asyncio.create_task(_direct())
        await asyncio.sleep(0.02)
        assert (len(done), len(regen), len(notify)) == (0, 0, 0)
    await asyncio.wait_for(task, timeout=5.0)
    assert (len(done), len(regen), len(notify)) == (1, 1, 1)
