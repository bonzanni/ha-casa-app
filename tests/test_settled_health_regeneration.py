"""#1055 — a cancelled plugin-health regeneration never overwrites a newer report.

`asyncio.to_thread` cancels the await, never the thread. A guarded caller
cancelled mid-computation used to release `_PLUGIN_TOOLS_LOCK` while its thread
still ran, so a competing guarded regeneration wrote the correct report and the
older one landed last (`plugin_health._REPORT_LOCK` orders only the write). The
live callers now go through `tools._regenerate_plugin_health_guarded` (caller
holds nothing) or `tools._regenerate_plugin_health_held` (caller holds the
lock); the static arm keeps any new caller off the raw pattern.
"""
from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path

import pytest

import tools


@pytest.fixture(autouse=True)
def _fresh_lock(monkeypatch):
    monkeypatch.setattr(tools, "_PLUGIN_TOOLS_LOCK", asyncio.Lock())
    monkeypatch.setattr(tools, "_PLUGIN_TOOLS_LOCK_OWNER", None)


def _guarded_via(name):
    async def _call():
        if name == "helper":
            await tools._regenerate_plugin_health_guarded()
        else:
            import importlib
            await importlib.import_module(name)._regen_health_safe()
    return _call


GUARDED_CALLERS = ["helper", "event_reconcile", "trigger_reconcile",
                   "callback_reconcile"]


class _Writer:
    """Stands in for `_regenerate_plugin_health`: the first pass blocks in its
    thread until released; every pass records when its write finished."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.writes: list[str] = []
        self._first = True

    def __call__(self, extra_issues):
        if self._first:
            self._first = False
            self.entered.set()
            assert self.release.wait(timeout=20), "writer never released"
            self.writes.append("old")
        else:
            self.writes.append("new")


@pytest.mark.parametrize("caller", GUARDED_CALLERS)
async def test_a_cancelled_regeneration_cannot_land_after_a_newer_one(
        caller, monkeypatch) -> None:
    """The issue's sequence: cancel a regeneration while its thread computes,
    then run a competing one. The newer report must be the last write."""
    writer = _Writer()
    monkeypatch.setattr(tools, "_regenerate_plugin_health", writer)

    old = asyncio.create_task(_guarded_via(caller)())
    await asyncio.to_thread(writer.entered.wait, 5)
    assert writer.entered.is_set()
    old.cancel()

    new = asyncio.create_task(tools._regenerate_plugin_health_guarded())
    for _ in range(20):
        await asyncio.sleep(0)
    assert writer.writes == []           # the newer pass waits for the guard

    writer.release.set()
    with pytest.raises(asyncio.CancelledError):
        await old
    await new
    assert writer.writes == ["old", "new"]   # RED before #1055: ["new", "old"]
    assert int(tools._PLUGIN_TOOLS_LOCK.locked()) == 0


@pytest.mark.parametrize("caller", GUARDED_CALLERS)
async def test_a_regeneration_cancelled_while_queued_still_runs(
        caller, monkeypatch) -> None:
    writes: list[str] = []
    monkeypatch.setattr(tools, "_regenerate_plugin_health",
                        lambda extra: writes.append("regen"))
    held, let_go = asyncio.Event(), asyncio.Event()

    async def _holder():
        async with tools._plugin_tools_guard():
            held.set()
            await let_go.wait()

    holder = asyncio.create_task(_holder())
    await held.wait()
    task = asyncio.create_task(_guarded_via(caller)())
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()
    for _ in range(20):
        await asyncio.sleep(0)
    assert writes == []

    let_go.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await holder
    assert writes == ["regen"]           # RED before #1055: []


async def test_the_guard_owner_regenerates_inline_without_deadlocking(
        monkeypatch) -> None:
    """A fenced reload dispatch re-enters its handler in the task that owns the
    guard: a child task taking the guard would queue behind its own parent."""
    order: list[str] = []
    monkeypatch.setattr(tools, "_regenerate_plugin_health",
                        lambda extra: order.append("regen"))

    async def _notify():
        order.append("notify")

    async def _owner():
        async with tools._plugin_tools_guard():
            await tools._regenerate_plugin_health_guarded(then=_notify)
            order.append("still-held")

    await asyncio.wait_for(_owner(), timeout=5)
    assert order == ["regen", "notify", "still-held"]


async def test_a_cancel_after_the_write_reaches_the_notify(monkeypatch) -> None:
    """Only the regeneration is settled: a notify still sending when the caller
    is cancelled is cancelled with it, so it cannot hold the lock through a
    shutdown (diff round 1, Terra S2)."""
    writes: list[str] = []
    monkeypatch.setattr(tools, "_regenerate_plugin_health",
                        lambda extra: writes.append("regen"))
    in_notify = asyncio.Event()
    notify_cancelled: list[bool] = []

    async def _stuck_notify():
        in_notify.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            notify_cancelled.append(True)
            raise

    task = asyncio.create_task(
        tools._regenerate_plugin_health_guarded(then=_stuck_notify))
    await asyncio.wait_for(in_notify.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)   # RED on f4aef762: hangs — the notify is settled
    for _ in range(5):
        await asyncio.sleep(0)
    assert writes == ["regen"]
    assert notify_cancelled == [True]
    assert int(tools._PLUGIN_TOOLS_LOCK.locked()) == 0

async def test_a_holder_cancelled_mid_regeneration_keeps_the_lock_until_it_lands(
        monkeypatch) -> None:
    """`_regenerate_plugin_health_held`, for callers under the raw lock (the
    mutation sequencers, the revokes, the routing recovery)."""
    writer = _Writer()
    monkeypatch.setattr(tools, "_regenerate_plugin_health", writer)

    async def _mutation():
        async with tools._PLUGIN_TOOLS_LOCK:
            await tools._regenerate_plugin_health_held(["issue"])

    old = asyncio.create_task(_mutation())
    await asyncio.to_thread(writer.entered.wait, 5)
    old.cancel()
    new = asyncio.create_task(tools._regenerate_plugin_health_guarded())
    for _ in range(20):
        await asyncio.sleep(0)
    assert writer.writes == []

    writer.release.set()
    with pytest.raises(asyncio.CancelledError):
        await old
    await new
    assert writer.writes == ["old", "new"]


# The boot reconcile regenerations run before the HTTP server, the channels and
# the agent loops start, so nothing can race them (casa_core step 13 comment).
_BOOT_ONLY = {("casa_core.py", "_boot_reconcile_plugin_triggers"),
              ("casa_core.py", "_boot_reconcile_plugin_callbacks"),
              ("casa_core.py", "_boot_reconcile_plugin_events")}
_HELPERS = {("tools.py", "_regenerate_plugin_health_held"),
            ("tools.py", "_regenerate_plugin_health_guarded")}


def test_no_live_caller_hops_to_the_regeneration_thread_directly() -> None:
    root = Path(tools.__file__).resolve().parent
    offenders: list[tuple[str, str]] = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text())
        tops = [n for n in tree.body for n in (
            n.body if isinstance(n, ast.ClassDef) else [n])]
        for top in tops:
            if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(top):
                if not (isinstance(sub, ast.Call) and sub.args):
                    continue
                fn, first = sub.func, sub.args[0]
                if not (isinstance(fn, ast.Attribute) and fn.attr == "to_thread"):
                    continue
                target = first.attr if isinstance(first, ast.Attribute) else (
                    first.id if isinstance(first, ast.Name) else None)
                if target == "_regenerate_plugin_health":
                    offenders.append((path.name, top.name))
    offenders = sorted(set(offenders) - _BOOT_ONLY - _HELPERS)
    assert offenders == [], (
        "regenerate plugin health through _regenerate_plugin_health_guarded "
        "or _regenerate_plugin_health_held, never a bare asyncio.to_thread")
