"""Pins `conftest._agent_tools_init_globals_restored`: a global `tools.init_tools`
binds in one test is back to its prior value in the next.

The pairs run in file order (`--dist loadfile` keeps a file on one worker, and
the serial gate runs it in order): the first test of each pair leaks by calling
`init_tools` with no restore, as the polluting tests do; the second asserts the
global is what it was before the leak."""
from __future__ import annotations

from types import SimpleNamespace

import tools

import conftest

_BEFORE: dict = {}


def test_the_sweep_covers_the_globals_the_leaks_were_measured_on():
    names = conftest._init_tools_globals()
    assert "_engagement_registry" in names and "_channel_manager" in names
    assert len(names) >= 14


def test_leak_by_init_tools():
    _BEFORE.update({n: getattr(tools, n) for n in conftest._init_tools_globals()})
    rec = SimpleNamespace(kind="specialist", role_or_type="fin")
    tools.init_tools(
        channel_manager=SimpleNamespace(get=lambda name: None), bus=object(),
        specialist_registry=object(),
        engagement_registry=SimpleNamespace(active_and_idle=lambda: [rec]))
    assert tools._open_specialist_engagements("fin") == [rec]


def test_the_leak_is_gone_in_the_next_test():
    for name, value in _BEFORE.items():
        assert getattr(tools, name) is value, name
    assert tools._open_specialist_engagements("fin") == []


def test_leak_by_bare_assignment():
    _BEFORE.clear()
    _BEFORE["_channel_manager"] = tools._channel_manager
    tools._channel_manager = SimpleNamespace(get=lambda name: None)


def test_a_bare_assignment_is_restored_too():
    assert tools._channel_manager is _BEFORE["_channel_manager"]


def test_leak_then_monkeypatch(monkeypatch):
    # The diff-review r1 shape: init_tools leaks, then a monkeypatch of the same
    # global saves the LEAKED value as the one to restore at its undo.
    _BEFORE.clear()
    _BEFORE["_engagement_registry"] = tools._engagement_registry
    tools.init_tools(
        channel_manager=None, bus=object(), specialist_registry=object(),
        engagement_registry=SimpleNamespace(active_and_idle=lambda: []))
    monkeypatch.setattr(tools, "_engagement_registry", None)


def test_the_monkeypatched_leak_is_gone_too():
    assert tools._engagement_registry is _BEFORE["_engagement_registry"]


def test_the_restorer_is_set_up_before_monkeypatch(request, monkeypatch):
    # Setup order IS fixturenames order; teardown is its reverse, so the
    # restorer must come before monkeypatch to undo after it.
    order = request.fixturenames
    assert (order.index("_agent_tools_init_globals_restored")
            < order.index("monkeypatch")), order
