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
