"""#1095 — when a specialist change reaches NEW conversations (release-text pin).

A persona applied to a specialist, and an upgrade or a rollback that completes,
each reload the specialist themselves: the persona apply after its commit (the
override lives in the specialist's instance tuple under `specialists/`, which the
config repository never tracks, so no `config_git_commit` can see it —
INV-TOOL-014), the upgrade and rollback in the bundle sequencer. These tests pin
both through the real handler and the real sequencer, counting `reload.dispatch`
calls.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_pin_1095_warn_then_act import _open, _out, _persona_args, persona, reg  # noqa: F401

pytestmark = pytest.mark.asyncio


def _record(monkeypatch, envelope=None) -> list:
    import reload as reload_mod
    import tools as tools_mod
    calls: list = []

    async def rec(scope, **kw):
        # (scope, role, the plugin-tools lock held while the reload ran)
        calls.append((scope, kw.get("role"), tools_mod._PLUGIN_TOOLS_LOCK.locked()))
        return dict(envelope or {"status": "ok"})
    monkeypatch.setattr(reload_mod, "dispatch", rec)
    monkeypatch.setattr(tools_mod, "_regenerate_plugin_health_guarded",
                        AsyncMock())
    return calls


@pytest.fixture
def live_runtime(monkeypatch):
    import agent as agent_mod
    monkeypatch.setattr(agent_mod, "active_runtime", SimpleNamespace(), raising=False)


async def test_a_committed_specialist_persona_apply_reloads_the_specialist(
        reg, persona, monkeypatch, live_runtime):
    """The commit of the override, then ONE `agent` reload of that specialist,
    after the plugin-tools lock is released, its envelope in the reply."""
    import tools as tools_mod
    calls = _record(monkeypatch)
    a = await _open(reg)
    out = _out(await tools_mod.persona_apply.handler(
        _persona_args(acknowledged_conversations=[a.id])))
    assert out.get("ok") is True, out
    assert len(persona) == 1
    assert calls == [("agent", "fin", False)]
    assert out["reload"]["status"] == "ok" and "warning" not in out


async def test_a_refused_specialist_persona_apply_reloads_nothing(
        reg, persona, monkeypatch, live_runtime):
    import tools as tools_mod
    calls = _record(monkeypatch)
    await _open(reg)
    out = _out(await tools_mod.persona_apply.handler(_persona_args()))
    assert out.get("ok") is not True and len(persona) == 0
    assert calls == []


async def test_a_failed_persona_reload_is_reported(reg, persona, monkeypatch, live_runtime):
    import tools as tools_mod
    calls = _record(monkeypatch, {"status": "error", "kind": "load_error",
                                  "message": "boom"})
    a = await _open(reg)
    out = _out(await tools_mod.persona_apply.handler(
        _persona_args(acknowledged_conversations=[a.id])))
    assert out.get("ok") is True and len(calls) == 1
    assert out["reload"]["kind"] == "load_error"
    assert "not live" in out["warning"]


async def test_the_bundle_sequencer_reloads_an_installed_specialist(monkeypatch, tmp_path):
    import agent as agent_mod
    import tools as tools_mod
    from test_plugin_tools import _State, _pr, _wire
    _wire(monkeypatch, tmp_path, _State(), publish=_pr())
    calls = _record(monkeypatch)
    monkeypatch.setattr(agent_mod, "active_runtime", SimpleNamespace(), raising=False)
    monkeypatch.setattr(tools_mod, "_specialist_target_pending", lambda rt, s: False)
    seq = await tools_mod._bundle_reload_and_verify(
        "fin", removed_artifact_ids=[], targets_removed=[])
    assert [c[:2] for c in calls].count(("agent", "fin")) == 1
    assert seq.get("reloaded") == ["specialist:fin"]
