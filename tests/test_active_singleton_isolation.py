"""#911 defect B — a test that assigns an `agent` `active_*` singleton must not
be able to change what a LATER test observes.

`agent.py:133-140` declares six process-global references "written by
`casa_core.main` so tool handlers can reach them without circular imports"
(`active_engagement_driver`, `active_executor_registry`,
`active_claude_code_driver`, `active_runtime`, `active_semantic_memory`,
`active_session_registry`), and `casa_core.py:4748` writes a seventh,
`active_observer`, which `agent.py` does not declare at all. Every consumer
resolves them at CALL time — `tools.py:10316-10326` and the sibling resolutions
at :10798-10804 and :11215-11218 — and gates each use on `hasattr`, a predicate
a `MagicMock` satisfies unconditionally. So a leaked mock does not fail loudly;
it reaches an `await` inside a log-and-continue arm and the caller degrades to
its fallback text.

Measured at `94439add`:
`pytest tests/test_delegate_to_agent_interactive.py tests/test_emit_completion_tool.py`
→ `1 failed, 127 passed in 5.29s`, the failure being
`TestEmitCompletionValidation::test_partial_status_completes_with_partial_marker`
reading the finalize funnel's fallback text instead of the partial marker.
The reverse order and each file alone are green.
`tests/test_delegate_to_agent_interactive.py:75,138,230,300` are the leaking
assignments; `tests/test_requires_contract.py:780` leaks the same global and
bites nobody today only because it sorts after the victim.

`tests/conftest.py`'s `_restore_active_runtime` (#818) already did this for ONE
of the seven. This file pins the family-wide guarantee — a D34 DECLARATION, not
a pinning of prior support: the base establishes the discipline for
`active_runtime` alone.

Each pair is ORDERED: the first test leaks, the second measures what the next
test actually gets. The victims assert COUNTS over object identity, in one
assertion each so none hides another. `active_future_probe` is deliberately a
name `agent.py` does not declare: a fixture that enumerates today's seven names
passes every other arm here and fails on it, which is the difference between a
list that goes stale and a sweep that does not.
"""
from __future__ import annotations

import sys

import pytest

import agent as agent_mod

pytestmark = pytest.mark.unit


class _Sentinel:
    """A distinct, non-`None`, truthy object. Deliberately not a `MagicMock`:
    identity is the whole measurement here, and a mock's auto-attributes would
    let a wrong object satisfy a sloppier assertion."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<sentinel {self.name}>"


#: The seven singletons the tree actually carries, plus one name nothing
#: declares. Ordered so the failure text reads the same way every run.
NAMES = (
    "active_claude_code_driver",
    "active_engagement_driver",
    "active_executor_registry",
    "active_future_probe",
    "active_observer",
    "active_runtime",
    "active_semantic_memory",
    "active_session_registry",
)

_MISSING = object()


def _active_names() -> set[str]:
    return {n for n in vars(agent_mod) if n.startswith("active_")}


@pytest.fixture(scope="module", autouse=True)
def baseline():
    """A module-scoped baseline, bound BEFORE any function-scoped snapshot of it
    is taken (higher-scoped fixtures set up first). This is what separates
    "restore the snapshot" from "restore `None`": every name below is non-`None`
    for the whole file, and a fixture that forced `None` would fail every
    identity count here.

    Restores exactly what it found, including the absence of the names
    `agent.py` never declared."""
    previous = {n: getattr(agent_mod, n, _MISSING) for n in NAMES}
    sentinels = {n: _Sentinel(n) for n in NAMES}
    for name, value in sentinels.items():
        setattr(agent_mod, name, value)
    try:
        yield sentinels
    finally:
        for name, value in previous.items():
            if value is _MISSING:
                # `hasattr` first, not a bare `delattr`: at the base a victim
                # below leaves names deleted, and a teardown that raised there
                # would report itself instead of the defect.
                if hasattr(agent_mod, name):
                    delattr(agent_mod, name)
            else:
                setattr(agent_mod, name, value)


@pytest.fixture(scope="module")
def baseline_names(baseline):
    """The `active_*` names present once the baseline is bound — the set against
    which an INTRODUCED name is counted."""
    return _active_names()


def _restored(baseline: dict) -> int:
    return sum(getattr(agent_mod, n, _MISSING) is baseline[n] for n in NAMES)


def _introduced(baseline_names: set[str]) -> int:
    return len(_active_names() - baseline_names)


# --- pair 1: rebinding, and introducing a name that was not there -----------


def test_pair1_leaker_rebinds_every_singleton_and_adds_one(baseline, baseline_names):
    """The defect's shape, verbatim: bare-assign, never restore — and add a name
    the module never carried, which no restore-by-enumeration would remove."""
    from unittest.mock import MagicMock

    for name in NAMES:
        setattr(agent_mod, name, MagicMock())
    agent_mod.active_added_probe = MagicMock()


def test_pair1_victim_gets_the_baseline_back(baseline, baseline_names):
    """What the NEXT test gets. At the base: `(1, 1)` — only `active_runtime` is
    restored (`tests/conftest.py`'s #818 fixture), and `active_added_probe` is
    still on the module."""
    observed = (_restored(baseline), _introduced(baseline_names))
    assert observed == (8, 0), (observed, sorted(_active_names()))


# --- pair 2: deletion, which a restore must also undo -----------------------


def test_pair2_leaker_deletes_every_singleton(baseline, baseline_names):
    """Deliberately asserts NO precondition: at the base this test runs against
    pair 1's residue, and a precondition assertion here would fail for pair 1's
    reason and hide pair 2's."""
    for name in NAMES:
        if hasattr(agent_mod, name):
            delattr(agent_mod, name)


def test_pair2_victim_gets_every_deleted_binding_back(baseline, baseline_names):
    """A teardown that only re-`setattr`s the names it saw CHANGE, rather than
    every name in its snapshot, leaves the deleted ones missing here."""
    observed = _restored(baseline)
    assert observed == 8, (observed, sorted(_active_names()))


# --- the fixture in a lane that cannot import `agent` -----------------------


class TestRestoreFixtureWithoutAgent:
    """`qa.yml`'s root lane (`.github/workflows/qa.yml:58-61`) runs
    `tests/test_private_state_dropped_uid.py` in a bare `python:3.11-slim` with
    only pytest installed, where `agent`'s `from claude_agent_sdk import ...`
    has no SDK to find — yet every autouse fixture in `tests/conftest.py` still
    runs at each of that file's tests' setup. The sibling `_fresh_reload_locks`
    guards its own import and degrades to a bare yield; the generalised fixture
    must keep doing the same, and must not restore anything it never snapshotted.

    The arrangement drives the fixture through pytest itself, as the lane does:
    a class-scoped fixture — a higher scope, so pytest sets it up BEFORE the
    function-scoped autouse fixtures — makes `agent` unimportable for the
    duration of this class."""

    @pytest.fixture(scope="class", autouse=True)
    @classmethod
    def _agent_unimportable(cls):
        # `None` in `sys.modules` makes `import agent` raise
        # ModuleNotFoundError — the lane's failure class — without unloading the
        # SDK the rest of this process has already imported. Restored by hand:
        # `monkeypatch` is function-scoped and would run AFTER the fixture under
        # test.
        previous = sys.modules["agent"]
        sys.modules["agent"] = None
        try:
            yield
        finally:
            sys.modules["agent"] = previous

    def test_a_fixture_yields_when_agent_cannot_be_imported(self):
        """Reaching this body IS half the outcome: the fixture set up and
        yielded with `agent` unimportable, rather than ERRORing at setup the way
        an unguarded `import agent` did to all nine tests of the root lane's
        file. The leak below is deliberate — it is what the next test measures.
        """
        with pytest.raises(ImportError):
            import agent  # noqa: F401 — the premise, measured where the fixture ran
        agent_mod.active_runtime = _Sentinel("degraded-lane-marker")

    def test_b_the_degraded_fixture_restored_nothing(self, baseline):
        """The other half: a fixture that cannot import `agent` must snapshot
        nothing and restore nothing. If it had snapshotted before the class
        fixture ran, or restored on the way out, this marker would be gone."""
        observed = (getattr(agent_mod.active_runtime, "name", None),
                    agent_mod.__name__)
        assert observed == ("degraded-lane-marker", "agent"), observed
        agent_mod.active_runtime = baseline["active_runtime"]
