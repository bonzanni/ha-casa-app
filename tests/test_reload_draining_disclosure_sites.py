"""Every reload site that replaces or retires an Agent DISCLOSES its drain.

``_track_draining`` records a swapped-out agent's plugin binding on
``runtime.draining`` so ``verify`` can report it as a consumer still on the
PREVIOUS artifact while its in-flight turn drains — but it records nothing
unless the call site supplies ``runtime`` and ``role``. Three sites passed
neither: the policies cascade's per-role swap, and the agents sweep's
resident-eviction and specialist-retirement paths. Their replaced agents
drained undisclosed.

Since #854 the drain no longer starts at the swap but when the dispatcher
RETURNS, so the undisclosed interval on those three sites grew by the whole
post-swap cascade — which is why the disclosure is pinned here.

Declared behaviour, pinned once PER SITE (so reverting one site's arguments
fails at least one test):

    A replaced or retired agent with a plugin binding is recorded on
    ``runtime.draining`` at its SWAP — before the dispatcher returns and
    before any close task exists — the record survives the dispatcher's exit
    while the drain runs, and it is dropped when the close completes.

The idiom, the harness and the fixtures are
``test_reload_close_fuse_starts_at_dispatch_return``'s; this file adds only
the per-site drivers. Assertions are counts and record contents, never
statuses.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import reload as reload_mod

from test_reload_close_fuse_starts_at_dispatch_return import (
    WATCHDOG, _Harness, _install)

pytestmark = pytest.mark.asyncio

BINDING = {"plugin": "p", "artifact": "a1"}


class _Old:
    """A replaced agent that HAS a plugin binding (so `_track_draining` has
    something to record) and whose ``aclose`` blocks until the test releases
    it — the whole drain window is observable."""

    def __init__(self, name: str) -> None:
        self._measured_name = name
        self.active_plugin_binding = dict(BINDING)
        self.handle_message = AsyncMock()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def aclose(self) -> None:
        self.entered.set()
        await self.release.wait()


def _resident_cfg(role: str):
    return SimpleNamespace(
        role=role, character=SimpleNamespace(name=role.upper(), card=""))


async def _quiet_teardown(runtime, monkeypatch) -> None:
    """The eviction/retirement paths run a real ``_teardown_role`` whose first
    step awaits ``bus.unregister_and_wait``; a bare MagicMock return is not
    awaitable and would make every teardown step report failed. The retirement
    residue set is module-level, so it is replaced per test."""
    runtime.bus.unregister_and_wait = AsyncMock()
    monkeypatch.setattr(reload_mod, "_INCOMPLETE_RETIREMENTS", set())


async def _assert_disclosed(h, runtime, old: _Old, role: str, task) -> None:
    """The three moments the disclosure has to hold, for one site."""
    # 1. At the post-lock probe: recorded at the SWAP, before any close task.
    await asyncio.wait_for(h.probe_entered.wait(), WATCHDOG)
    draining = getattr(runtime, "draining", [])
    assert draining == [{"role": role, "binding": dict(BINDING)}], draining
    assert len(reload_mod._AGENT_CLOSE_TASKS.creations) == 0
    assert not old.entered.is_set()

    h.probe_release.set()
    envelope = await asyncio.wait_for(task, WATCHDOG)
    assert envelope["status"] == "ok", envelope

    # 2. After the dispatcher's exit, with the drain actually running.
    creations = reload_mod._AGENT_CLOSE_TASKS.creations
    assert len(creations) == 1, creations
    await asyncio.wait_for(old.entered.wait(), WATCHDOG)
    draining = getattr(runtime, "draining", [])
    assert draining == [{"role": role, "binding": dict(BINDING)}], draining

    # 3. Dropped when the close completes.
    old.release.set()
    await asyncio.wait_for(
        asyncio.gather(*[c["task"] for c in creations]), WATCHDOG)
    await asyncio.sleep(0)                     # let the done-callback run
    assert getattr(runtime, "draining", None) == []


async def test_policies_cascade_swap_discloses_its_draining_agent(
    monkeypatch, tmp_path,
):
    """Site 1 — ``_reload_role_after_policies``: the per-role swap the
    policies cascade makes. Its replaced agent drains across the rest of the
    cascade and the post-lock report; the disclosure spans that."""
    h = _Harness()
    old = _Old("cascade-old")
    runtime = await _install(
        h, monkeypatch, tmp_path,
        replacements=[h.standin("standin1")], live=old)

    task = asyncio.create_task(reload_mod.dispatch("policies", runtime=runtime))
    await _assert_disclosed(h, runtime, old, "assistant", task)


async def test_agents_sweep_eviction_discloses_its_draining_agent(
    monkeypatch, tmp_path,
):
    """Site 2 — ``reload_agents``' resident-eviction path: a resident whose
    directory is gone. Its agent is dropped and drained, and the disclosure
    must name the evicted role, not nothing."""
    h = _Harness()
    old = _Old("evicted-old")
    runtime = await _install(
        h, monkeypatch, tmp_path,
        replacements=[], live=h.standin("live"))
    await _quiet_teardown(runtime, monkeypatch)
    # A known resident with NO directory on disk = the eviction candidate.
    # The live "assistant" keeps its directory, so it is not swept, and its
    # stand-in has an empty binding — the one record can only be the evict.
    runtime.role_configs["ghost"] = _resident_cfg("ghost")
    runtime.agents["ghost"] = old

    task = asyncio.create_task(reload_mod.dispatch("agents", runtime=runtime))
    await _assert_disclosed(h, runtime, old, "ghost", task)
    assert "ghost" not in runtime.agents
    assert "ghost" not in runtime.role_configs


async def test_agents_sweep_retirement_discloses_its_draining_agent(
    monkeypatch, tmp_path,
):
    """Site 3 — ``reload_agents``' specialist-retirement path: a live Agent
    for a role that is neither a resident nor on disk. Same drain, same
    disclosure."""
    h = _Harness()
    old = _Old("retired-old")
    runtime = await _install(
        h, monkeypatch, tmp_path,
        replacements=[], live=h.standin("live"))
    await _quiet_teardown(runtime, monkeypatch)
    # In runtime.agents, absent from role_configs and from both directories
    # = the retirement candidate. No resident is missing a directory, so the
    # eviction loop above it does not fire.
    runtime.agents["retiree"] = old

    task = asyncio.create_task(reload_mod.dispatch("agents", runtime=runtime))
    await _assert_disclosed(h, runtime, old, "retiree", task)
    assert "retiree" not in runtime.agents
