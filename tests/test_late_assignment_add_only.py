"""#1179 regression guards around the add-only late-assignment merge.

The red case (`test_pin_1179_late_assignment.py`) pins that a plugin assigned while a
requires target's launch awaits its topic is recorded and launched. These pin what the
merge must NOT do: drop or re-check a plugin the requires gate admitted, add a plugin
that would share an admitted one's runtime name, or fail after the topic exists.
"""

from __future__ import annotations

import pytest

from config import RequiresConfig

try:
    from tests.test_pin_1179_late_assignment import _launch, _plugin, _recorded_names
except ImportError:
    from test_pin_1179_late_assignment import _launch, _plugin, _recorded_names

pytestmark = [pytest.mark.unit]


@pytest.mark.asyncio
class TestLateAssignmentIsAddOnly:
    async def test_an_unassignment_in_the_window_drops_nothing_and_aborts_nothing(
            self, monkeypatch):
        # Add-only: no requires re-check follows the topic, so a required plugin
        # unassigned during the await stays (as at the base) and no topic is aborted.
        mtg = _plugin("mtg")
        obs = await _launch(
            monkeypatch, requires=RequiresConfig(plugins=["mtg"], tools=[]),
            before=[mtg], after=[])

        assert obs["payload"]["status"] == "pending"
        assert obs["create"].await_count == 1
        assert obs["abort"].await_count == 0
        assert _recorded_names(obs) == ["mtg"]
        assert obs["built"][0].plugins == [mtg]
        assert obs["built"][0].plugins[0] is mtg

    async def test_a_late_plugin_sharing_a_runtime_name_is_not_added(self, monkeypatch):
        # Owned `finance.mtg` (runtime name "mtg") was admitted; an unowned `mtg`
        # assigned in the window would share its MCP namespace, so it is skipped.
        owned = _plugin("finance.mtg", manifest_name="mtg")
        clash = _plugin("mtg")
        late = _plugin("late")
        obs = await _launch(
            monkeypatch, requires=RequiresConfig(plugins=["mtg"], tools=[]),
            before=[owned], after=[clash, late])

        assert obs["create"].await_count == 1
        assert _recorded_names(obs) == [rp.name for rp in obs["built"][0].plugins] == [
            "finance.mtg", "late"]
        assert obs["built"][0].plugins[0] is owned

    async def test_a_failed_post_topic_resolve_launches_the_gate_set(self, monkeypatch):
        # Nothing may fail between the topic and the record: a resolver error
        # after the topic exists keeps the gate's set and still records it.
        mtg = _plugin("mtg")
        obs = await _launch(
            monkeypatch, requires=RequiresConfig(plugins=["mtg"], tools=[]),
            before=[mtg], after=RuntimeError("snapshot reload failed"))

        assert obs["payload"]["status"] == "pending"
        assert obs["create"].await_count == 1
        assert obs["abort"].await_count == 0
        assert _recorded_names(obs) == ["mtg"]
        assert obs["built"][0].plugins[0] is mtg
        assert obs["driver"].start.await_count == 1
