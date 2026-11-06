"""FR9: the 2026-07-12 invoice-plugin incident is structurally impossible.

Trace: v1.1.0 -> v1.2.0 update where the version field never bumped ->
version-keyed cache hit -> verify read the highest CACHED version ->
ready:true on stale code. Each leg is re-run against the new flow."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin_fixtures import entry, mk_artifact, mk_registry

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = pytest.mark.unit


def _old_entry():
    return {"name": "lantern", "source": {
        "type": "github", "repo": "o/r", "ref": "v1",
        "revision": "git:" + "c" * 40, "subdir": ""},
        "artifact_id": "c" * 64, "version": "1.1.0",
        "targets": ["specialist:finance"]}


@pytest.mark.asyncio
async def test_update_with_unchanged_manifest_version_still_swaps_artifact(
        monkeypatch, tmp_path):
    """The stale-cache leg is dead: a NEW commit yields a NEW artifact_id even
    when the manifest version is unchanged (identity includes revision, never
    a version key)."""
    from test_plugin_tools import _State, _pr, _wire
    st = _State()
    st.raw["plugins"].append(_old_entry())
    # publish returns the SAME version "1.1.0" but a new artifact ("a"*64).
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      publish=_pr("lantern", version="1.1.0"))
    r = await tools_mod.plugin_update.handler(
        {"name": "lantern", "new_ref": "master-at-a-new-commit"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["version"] == "1.1.0"          # version UNCHANGED
    entry_now = st.raw["plugins"][0]
    assert entry_now["artifact_id"] == "a" * 64   # but the artifact SWAPPED
    assert entry_now["artifact_id"] != "c" * 64


@pytest.mark.asyncio
async def test_phantom_tag_is_hard_prepublish_failure(monkeypatch, tmp_path):
    """A ref that doesn't resolve fails BEFORE any mutation — no phantom-tag
    partial state, registry byte-identical."""
    from plugin_store import RefNotFound
    from test_plugin_tools import _State, _wire
    st = _State()
    st.raw["plugins"].append(_old_entry())
    before = copy.deepcopy(st.raw)
    tools_mod = _wire(monkeypatch, tmp_path, st,
                      publish_exc=RefNotFound("404"))
    r = await tools_mod.plugin_update.handler(
        {"name": "lantern", "new_ref": "phantom-tag"})
    assert json.loads(r["content"][0]["text"])["kind"] == "ref_not_found"
    assert st.raw == before                       # registry unchanged


def test_stale_active_binding_is_never_green(monkeypatch, tmp_path):
    """THE incident, through the REAL seam: a constructed agent still bound to
    the OLD artifact after a registry update reports reload_required — verify
    can never green a stale binding (FR3). Reconstruction clears it."""
    import agent as agent_mod
    import plugin_registry
    from agent_registry import AgentRegistry
    from config import AgentConfig
    from test_agent_plugin_binding import _make_agent

    store = tmp_path / "store"
    e_old = entry("lantern", ["specialist:finance"], revision="git:" + "c" * 40)
    e_new = entry("lantern", ["specialist:finance"], revision="git:" + "d" * 40)
    mk_artifact(store, "lantern", e_old["artifact_id"], revision="git:" + "c" * 40)
    mk_artifact(store, "lantern", e_new["artifact_id"], revision="git:" + "d" * 40)
    reg_old = tmp_path / "registry-old.json"
    reg_old.write_text(json.dumps({"schema_version": 1, "plugins": [e_old]}))
    reg_new = tmp_path / "registry-new.json"
    reg_new.write_text(json.dumps({"schema_version": 1, "plugins": [e_new]}))

    ar = AgentRegistry.build(residents={},
                             specialists={"finance": AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role="finance")})
    agent = _make_agent(tmp_path, role="finance", agent_registry=ar)

    def _verify(reg_path):
        from tools import _tool_verify_plugin_state
        return _tool_verify_plugin_state(plugin_name="lantern",
                                         _registry_path=reg_path,
                                         _store_root=store)

    async def run():
        # 1. Agent resolves against the OLD snapshot → binding = OLD.
        plugin_registry.reload_snapshot(registry_path=reg_old, store_root=store)
        await agent._get_plugin_resolution()
        assert agent.active_plugin_binding == {"lantern": e_old["artifact_id"]}

        # 2. Registry updated to NEW (snapshot refreshed) WITHOUT reconstructing.
        plugin_registry.reload_snapshot(registry_path=reg_new, store_root=store)
        monkeypatch.setattr(agent_mod, "active_runtime",
                            SimpleNamespace(agents={"finance": agent},
                                            executor_registry=None),
                            raising=False)
        stale = _verify(reg_new)
        assert stale["ready"] is False
        assert stale["stale_targets"] == ["specialist:finance"]
        assert stale["targets"][0]["reasons"] == ["reload_required"]

        # 3. Reconstruct (fresh Agent, resolution re-run against NEW) → green.
        fresh = _make_agent(tmp_path, role="finance", agent_registry=ar)
        await fresh._get_plugin_resolution()
        monkeypatch.setattr(agent_mod, "active_runtime",
                            SimpleNamespace(agents={"finance": fresh},
                                            executor_registry=None),
                            raising=False)
        assert _verify(reg_new)["ready"] is True

    asyncio.run(run())


def test_verify_never_reads_highest_version_dir():
    """Meta-guard: the version-keyed indirection is gone for good."""
    root = (Path(__file__).resolve().parent.parent / "casa" / "rootfs"
            / "opt" / "casa")
    assert "highest_version" not in (root / "tools.py").read_text(encoding="utf-8")
    assert "_version_key" not in (root / "plugin_grants.py").read_text(encoding="utf-8")
