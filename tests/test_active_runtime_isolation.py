"""#818 — a test that bare-assigns `agent.active_runtime` must not be able to
affect a later test.

`agent.active_runtime` (`agent.py:106`) is a module global, `None` until
`casa_core.main` binds the runtime once per process. Three tests in
`tests/test_casa_reload_triggers_resident.py` (and five in its `_tool`
sibling) assign it directly and nothing restores it, so the leaked
`CasaRuntime` — whose `trigger_registry` is a `MagicMock` — makes two
truthiness probes fire in later, unpatched tests: `callback_reconcile`'s
`callback_overlay_unavailable()` (`callback_reconcile.py:1114`) injects a
`callback_routing_unavailable` row, and `tools._tool_plugin_status`
(`tools.py:14469-14477`) adds a `routing_unavailable` key. Both observers
assert exact shapes and fail with one extra row/key. `--dist loadfile` hid it
by placing the leaking file and its observers on different workers; the
default serial order is red.

The cure is an autouse fixture in `tests/conftest.py` that snapshots the
global at setup and restores THE SNAPSHOT at teardown — never a forced `None`
(a module-scoped baseline must survive), never a rebind of the module.

Each pair is ORDERED: the first test leaks, the second measures what the next
test actually gets. The victims assert observable OUTCOMES (a count of rows, a
count of keys, object identity), not "the global is clean". The module-scoped
preservation pair is what separates "restore the snapshot" from "restore
`None`": its baseline is a non-`None` sentinel bound before the function-scoped
snapshot is taken, and the second test must see exactly that object back.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

import agent as agent_mod
import callback_reconcile
import plugin_setup_episodes
import tools as tools_mod

pytestmark = pytest.mark.unit


def _runtime(tmp_path):
    from runtime import CasaRuntime
    return CasaRuntime(
        agents={}, role_configs={}, specialist_registry=MagicMock(),
        executor_registry=MagicMock(), engagement_registry=MagicMock(),
        agent_registry=MagicMock(), trigger_registry=MagicMock(),
        mcp_registry=MagicMock(), session_registry=MagicMock(),
        channel_manager=MagicMock(), bus=MagicMock(),
        engagement_driver=MagicMock(), claude_code_driver=MagicMock(),
        policy_lib=MagicMock(), config_dir=str(tmp_path),
        agents_dir=str(tmp_path / "agents"), home_root=str(tmp_path / "home"),
        defaults_root="/opt/casa",
    )


# --- pair 1: a leak from a clean baseline ---------------------------------


def test_pair1_leaker_bare_assigns_a_runtime(tmp_path):
    """The defect's shape, verbatim: assign, never restore."""
    assert agent_mod.active_runtime is None
    agent_mod.active_runtime = _runtime(tmp_path)


def test_pair1_victim_starts_from_the_baseline(tmp_path, monkeypatch):
    """What the NEXT test gets. At the base: the leaked runtime (identity
    assertion red), one `callback_routing_unavailable` row and one
    `routing_unavailable` key — the two observer failures #818 measured."""
    monkeypatch.setattr(tools_mod, "_PLUGIN_HEALTH_PATH", str(tmp_path / "health.json"))
    monkeypatch.setattr(plugin_setup_episodes, "read_episodes",
                        lambda status=None: plugin_setup_episodes.EpisodesRead([], None, None))
    rows = callback_reconcile.current_issues()
    payload = tools_mod._tool_plugin_status()
    observed = (
        agent_mod.active_runtime is None,
        sum(r.reason_code == "callback_routing_unavailable" for r in rows),
        sum(k == "routing_unavailable" for k in payload),
    )
    # One assertion over all three observations, so none hides another.
    assert observed == (True, 0, 0), (observed, rows, payload)


# --- pair 2: preservation of a non-None baseline ---------------------------


@pytest.fixture(scope="module")
def module_sentinel(tmp_path_factory):
    """A module-scoped baseline, bound BEFORE any function-scoped snapshot of
    it is taken (module fixtures set up first). Restores what it found."""
    previous = agent_mod.active_runtime
    sentinel = _runtime(tmp_path_factory.mktemp("sentinel"))
    agent_mod.active_runtime = sentinel
    try:
        yield sentinel
    finally:
        agent_mod.active_runtime = previous


def test_pair2_a_replaces_the_module_baseline(module_sentinel, tmp_path):
    assert agent_mod.active_runtime is module_sentinel
    agent_mod.active_runtime = _runtime(tmp_path)


def test_pair2_b_gets_exactly_the_module_baseline_back(module_sentinel):
    """A fixture that forces `None` instead of restoring its snapshot fails
    here; so does one that snapshots after yielding."""
    assert agent_mod.active_runtime is module_sentinel


# --- the fixture in a lane that cannot import `agent` -----------------------


class TestRestoreFixtureWithoutAgent:
    """`qa.yml`'s root lane runs `tests/test_private_state_dropped_uid.py` in a
    bare `python:3.11-slim` with only pytest installed and
    `PYTHONPATH=casa/rootfs/opt/casa`. `agent` cannot be imported there (its
    `from claude_agent_sdk import ...` has no SDK to find), yet every autouse
    fixture in `tests/conftest.py` still runs at each of that file's tests'
    setup. An unguarded `import agent` in `_restore_active_runtime` made all
    nine ERROR at setup; the sibling `_fresh_reload_locks` guards its own
    import and degrades to a bare yield, and this fixture must do the same.

    The arrangement drives the fixture through pytest itself, as the lane
    does: a class-scoped fixture — a higher scope, so pytest sets it up BEFORE
    the function-scoped autouse fixtures — makes `agent` unimportable for the
    duration of this class. At the base this test ERRORs at setup with
    `ModuleNotFoundError: No module named 'agent'`; with the guard it runs,
    and the body measures that the premise held in this very process."""

    @pytest.fixture(scope="class", autouse=True)
    @classmethod
    def _agent_unimportable(cls):
        # `None` in `sys.modules` makes `import agent` raise
        # ModuleNotFoundError — the lane's failure class — without unloading
        # the SDK the rest of this process has already imported. Restored by
        # hand: `monkeypatch` is function-scoped and would run AFTER the
        # fixture under test.
        previous = sys.modules["agent"]
        sys.modules["agent"] = None
        try:
            yield
        finally:
            sys.modules["agent"] = previous

    def test_restore_fixture_yields_when_agent_cannot_be_imported(self):
        with pytest.raises(ImportError):
            import agent  # noqa: F401 — the premise, measured where the fixture ran
        # Reaching here IS the outcome: `_restore_active_runtime` set up and
        # yielded with `agent` unimportable. The module object this file holds
        # is untouched by the degraded fixture.
        assert agent_mod.__name__ == "agent"
