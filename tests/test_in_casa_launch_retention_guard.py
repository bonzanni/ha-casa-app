"""#678 H2 guard — the launch-death path must never fabricate a success.

Its own file so it can be MUTATED ON ITS OWN (drive rule): flipping the
reporting helper to ``outcome="completed"``, or routing it through
``_finalize_engagement``, must fail THIS test while the primary red case in
``test_in_casa_launch_terminal_artifact.py`` still passes.

Why it matters, stated as the outcome it protects: ``_finalize_engagement``
announces the outcome to the party that asked for the work. A launch death
recorded as ``completed`` with empty text would tell it of a success that
never happened, and the record would keep that false status.
A green suite is not evidence of its absence, which is why the counts below
are asserted rather than the statuses.
"""

from __future__ import annotations

import json

import pytest

try:
    from tests.test_in_casa_launch_terminal_artifact import (
        ScriptedCutoffClient, _Probe, _build, _launch,
    )
except ImportError:  # pragma: no cover — direct-path collection
    from test_in_casa_launch_terminal_artifact import (
        ScriptedCutoffClient, _Probe, _build, _launch,
    )

pytestmark = [pytest.mark.asyncio]


async def test_mid_tool_loop_cutoff_never_completes_or_retains(
    tmp_path, monkeypatch,
):
    import tools as tools_mod

    probe = _Probe()
    launcher, registry, _channel, _driver = _build(
        tmp_path, monkeypatch, probe, ScriptedCutoffClient,
    )

    # Every status this record is ever PERSISTED with, captured at each write.
    written_statuses: list[str] = []
    real_write = registry._write_tombstone_locked

    async def spy_write(*, strict=False):
        for rec in registry._records.values():
            written_statuses.append(rec.status)
        return await real_write(strict=strict)

    monkeypatch.setattr(registry, "_write_tombstone_locked", spy_write)

    finalize_calls: list[dict] = []

    async def spy_finalize(engagement, **kw):
        finalize_calls.append(kw)
        raise AssertionError(
            "#678: the launch-death path must not enter _finalize_engagement")

    monkeypatch.setattr(tools_mod, "_finalize_engagement", spy_finalize)

    envelope = await _launch(launcher)

    payload = json.loads(envelope["content"][0]["text"])
    assert payload["status"] == "pending", payload   # §2.A: detached owner
    await tools_mod.drain_launch_turns()
    await tools_mod.drain_launch_death_reports()

    created_id = next(iter(registry._records))

    # The load-bearing counts: no success was ever written, anywhere.
    assert written_statuses.count("completed") == 0, written_statuses
    assert written_statuses.count("error") == 1, written_statuses
    assert registry.get(created_id).status == "error"
    assert registry.get(created_id).origin["error_kind"] == (
        "launch_turn_incomplete")

    rows = json.loads((tmp_path / "engagements.json").read_text())
    matching = [r for r in rows if r["id"] == created_id]
    assert len(matching) == 1, rows
    assert matching[0]["status"] == "error"
    assert matching[0]["origin"]["error_kind"] == "launch_turn_incomplete"

    # Nothing reached the funnel, which announces the outcome as told.
    assert finalize_calls == []
