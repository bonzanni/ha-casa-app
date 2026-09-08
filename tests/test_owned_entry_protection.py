"""Task 6: the plugin lifecycle tools (update/assign/unassign/remove) refuse
to mutate a specialist-owned registry entry — a specialist's bundled plugin
may only be moved by specialist_upgrade / specialist_uninstall, never by the
generic operator-facing plugin tools. plugin_add needs NO guard: NAME_RE
structurally forbids a dotted name, so an operator can never even construct
an add call that collides with a scoped owned name (see the last test)."""
from __future__ import annotations

import json

import pytest

import test_plugin_tools as tpt
from plugin_fixtures import mk_registry, owned_entry

pytestmark = pytest.mark.unit


def _wire_real_registry(monkeypatch, tmp_path, reg_path, *, with_runtime=True):
    """`test_plugin_tools._wire`'s proven wiring (resolve/publish/sysreqs/
    dispatch/health-path all stubbed to SUCCEED, so a pre-guard regression
    would actually run the whole mutation pipeline) — with load_registry/
    save_registry redirected from the in-memory `_State` stub to the REAL
    functions bound to a real on-disk registry file, so the test can assert
    actual file bytes rather than an in-memory dict."""
    st = tpt._State()
    import plugin_registry as preg
    real_load, real_save = preg.load_registry, preg.save_registry
    tools_mod = tpt._wire(monkeypatch, tmp_path, st, publish=tpt._pr(),
                         with_runtime=with_runtime)
    monkeypatch.setattr(preg, "load_registry", lambda: real_load(reg_path))
    monkeypatch.setattr(preg, "save_registry",
                        lambda data: real_save(data, reg_path))
    return tools_mod


async def test_plugin_update_refuses_owned_entry(monkeypatch, tmp_path):
    reg_path = mk_registry(tmp_path, [owned_entry()])
    before = reg_path.read_bytes()
    tools_mod = _wire_real_registry(monkeypatch, tmp_path, reg_path)
    r = await tools_mod.plugin_update.handler(
        {"name": "mtg.mtg", "new_ref": "dev"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["kind"] == "owned_by_specialist"
    assert payload["owner"] == "specialist:mtg"
    assert reg_path.read_bytes() == before


async def test_plugin_assign_refuses_owned_entry(monkeypatch, tmp_path):
    reg_path = mk_registry(tmp_path, [owned_entry()])
    before = reg_path.read_bytes()
    tools_mod = _wire_real_registry(monkeypatch, tmp_path, reg_path)
    r = await tools_mod.plugin_assign.handler(
        {"name": "mtg.mtg", "target": "resident:assistant"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["kind"] == "owned_by_specialist"
    assert payload["owner"] == "specialist:mtg"
    assert reg_path.read_bytes() == before


async def test_plugin_unassign_refuses_owned_entry(monkeypatch, tmp_path):
    reg_path = mk_registry(tmp_path, [owned_entry()])
    before = reg_path.read_bytes()
    tools_mod = _wire_real_registry(monkeypatch, tmp_path, reg_path)
    r = await tools_mod.plugin_unassign.handler(
        {"name": "mtg.mtg", "target": "specialist:mtg"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["kind"] == "owned_by_specialist"
    assert payload["owner"] == "specialist:mtg"
    assert reg_path.read_bytes() == before


async def test_plugin_remove_refuses_owned_entry(monkeypatch, tmp_path):
    reg_path = mk_registry(tmp_path, [owned_entry()])
    before = reg_path.read_bytes()
    tools_mod = _wire_real_registry(monkeypatch, tmp_path, reg_path)
    r = await tools_mod.plugin_remove.handler({"name": "mtg.mtg"})
    payload = json.loads(r["content"][0]["text"])
    assert payload["kind"] == "owned_by_specialist"
    assert payload["owner"] == "specialist:mtg"
    assert reg_path.read_bytes() == before


def test_plugin_add_rejects_dotted_name_structurally():
    """plugin_add needs no owned-entry guard: an operator name structurally
    cannot contain '.' — NAME_RE rejects it before any registry lookup, so a
    dotted (scoped, specialist-owned) name can never collide."""
    import tools as tools_mod
    result = tools_mod._plugin_add_sync(
        name="mtg.mtg", repo="o/r", ref="v1", subdir="",
        targets=["resident:assistant"])
    assert result == {"ok": False, "kind": "invalid_name", "name": "mtg.mtg"}
