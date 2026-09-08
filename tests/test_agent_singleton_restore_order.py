"""#911 defect B — the restore fixture's teardown must run AFTER `monkeypatch`'s.

`tests/conftest.py`'s `_agent_active_singletons_restored` deletes, at teardown,
every `active_*` name a test introduced on the `agent` module. `monkeypatch`'s
undo of a `setattr(obj, name, value, raising=False)` that CREATED an attribute
is an unconditional `delattr` (`_pytest/monkeypatch.py`, `undo`) — so whichever
of the two runs second raises `AttributeError` on a name that is already gone,
and pytest reports it as an ERROR at teardown.

Teardown order is the reverse of setup order, and pytest builds a module's
fixture list from `dir(conftest)`, which is sorted
(`_pytest/fixtures.py:parsefactories` iterates `for name in dir(holderobj)`).
So the property the fixture needs — set up before the `monkeypatch` instance
exists — is a property of its NAME, and a rename or a new autouse fixture
sorting ahead of it silently breaks it.

This file pins the property by EXHIBITING it rather than by asserting a name:
the test below creates an `agent.active_*` attribute through
`monkeypatch.setattr(..., raising=False)`, which is exactly the shape
`tests/test_topic_cleanup_tool.py`'s module fixture uses for `active_observer`.
Under the wrong order this test's BODY still passes and its TEARDOWN errors —
measured: with the fixture named `_restore_...`, all 29 tests of that file
ERRORed in `monkeypatch.undo` with
`AttributeError: 'module' object has no attribute 'active_observer'`.
"""
from __future__ import annotations

import pytest

import agent as agent_mod

pytestmark = pytest.mark.unit

#: A name `agent.py` does not declare, so `raising=False` is what creates it.
PROBE = "active_restore_order_probe"


def test_a_monkeypatch_creates_an_active_name_and_the_teardown_order_holds(monkeypatch):
    """Reaching the END of this test's teardown cleanly IS the measurement.

    The body asserts the premise — that the name did not exist and now does —
    so a future rename of `PROBE` onto an existing attribute cannot turn this
    into a test that exercises nothing."""
    assert not hasattr(agent_mod, PROBE)
    monkeypatch.setattr(agent_mod, PROBE, object(), raising=False)
    assert hasattr(agent_mod, PROBE)


def test_b_the_probe_name_is_gone(monkeypatch):
    """Both undos ran, in either role, and left the module as it was."""
    assert not hasattr(agent_mod, PROBE)
