"""#1095 — when a specialist change reaches NEW conversations (release-text pin).

The user docs and the changelog say: a persona applied to a specialist is
committed but not loaded — new conversations get it once the agents are
reloaded (the configurator's recipe runs `casa_reload(scope="agents")`) — while
an upgrade or a rollback that completes reloads the specialist itself, in the
bundle sequencer. These two tests pin both halves through the real handler and
the real sequencer, counting `reload.dispatch` calls.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from test_pin_1095_warn_then_act import _open, _out, _persona_args, persona, reg  # noqa: F401

pytestmark = pytest.mark.asyncio


def _record(monkeypatch) -> list:
    import reload as reload_mod
    calls: list = []

    async def rec(scope, **kw):
        calls.append((scope, kw.get("role")))
        return {"status": "ok"}
    monkeypatch.setattr(reload_mod, "dispatch", rec)
    return calls


async def test_a_committed_specialist_persona_apply_reloads_nothing(reg, persona, monkeypatch):
    import tools as tools_mod
    calls = _record(monkeypatch)
    a = await _open(reg)
    out = _out(await tools_mod.persona_apply.handler(
        _persona_args(acknowledged_conversations=[a.id])))
    assert out.get("ok") is True, out
    assert len(persona) == 1
    assert calls == []


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
    assert calls.count(("agent", "fin")) == 1
    assert seq.get("reloaded") == ["specialist:fin"]
