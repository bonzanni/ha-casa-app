"""#1046: ``casa.eraseTool`` — the optional argument-free eraser a plugin
declares. Strict on the install/update path, a verdict on the stored-artifact
path, exactly like ``casa.setupTool``."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import plugin_store
from plugin_store import METADATA_FILENAME, StoreError, content_checksum

pytestmark = pytest.mark.unit


def _contract(**tools):
    return {"version": 1, "tools": {n: {"result": r} for n, r in tools.items()}}


def _manifest(erase="delete_all_data", contract=None, setup=None):
    casa: dict = {}
    if erase is not None:
        casa["eraseTool"] = erase
    if setup is not None:
        casa["setupTool"] = setup
    if contract is not None:
        casa["resultContract"] = contract
    return {"name": "p", "version": "1.0.0", "casa": casa}


def test_absent_erase_tool_is_none():
    assert plugin_store.manifest_erase_tool({"name": "p", "casa": {}}) is None
    assert plugin_store.manifest_erase_tool({"name": "p"}) is None


def test_valid_erase_tool_returned():
    m = _manifest(contract=_contract(delete_all_data="safe"))
    assert plugin_store.manifest_erase_tool(m) == "delete_all_data"


@pytest.mark.parametrize("bad", ["", "Delete", "1erase", "erase-all",
                                 "e" * 65, 7, ["x"]])
def test_malformed_erase_tool_refused(bad):
    m = _manifest(erase=bad, contract=_contract(x="safe"))
    with pytest.raises(StoreError) as exc:
        plugin_store.manifest_erase_tool(m)
    assert exc.value.reason_code == "erase_tool_invalid"


def test_explicit_null_erase_tool_refused():
    m = {"name": "p", "casa": {"eraseTool": None,
                               "resultContract": _contract(x="safe")}}
    with pytest.raises(StoreError) as exc:
        plugin_store.manifest_erase_tool(m)
    assert exc.value.reason_code == "erase_tool_invalid"


def test_erase_tool_equal_to_setup_tool_refused():
    m = _manifest(erase="setup_p", setup="setup_p",
                  contract=_contract(setup_p="safe"))
    with pytest.raises(StoreError) as exc:
        plugin_store.manifest_erase_tool(m)
    assert exc.value.reason_code == "erase_tool_invalid"


def test_erase_tool_without_result_contract_refused():
    """Design rev 2 (r1 Astra S2): the broker refuses every non-setup tool of
    a non-adopting plugin, so an eraser there could never run."""
    with pytest.raises(StoreError) as exc:
        plugin_store.manifest_erase_tool(_manifest())
    assert exc.value.reason_code == "erase_tool_invalid"


def test_erase_tool_missing_from_contract_refused():
    m = _manifest(contract=_contract(other="safe"))
    with pytest.raises(StoreError) as exc:
        plugin_store.manifest_erase_tool(m)
    assert exc.value.reason_code == "erase_tool_invalid"


def test_erase_tool_contract_entry_must_be_safe():
    m = _manifest(contract={"version": 1, "tools": {
        "delete_all_data": {"result": "capability", "provides": ["s"]}}})
    with pytest.raises(StoreError) as exc:
        plugin_store.manifest_erase_tool(m)
    assert exc.value.reason_code == "erase_tool_invalid"


def _publish(tmp_path, manifest):
    root = tmp_path / "src"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    return plugin_store.publish_from_tree(
        name="p", repo="o/r", ref="v1", revision="git:" + "a" * 40,
        subdir="", src_root=root, store_root=tmp_path / "store",
        staging_root=tmp_path / "staging")


def test_install_path_refuses_malformed_erase_tool(tmp_path):
    with pytest.raises(StoreError) as exc:
        _publish(tmp_path, _manifest(erase="Bad"))
    assert exc.value.reason_code == "erase_tool_invalid"


def test_install_path_accepts_valid_erase_tool(tmp_path):
    res = _publish(tmp_path, _manifest(contract=_contract(delete_all_data="safe")))
    assert res.artifact_id


def test_artifact_verdict_rechecks_erase_tool(tmp_path):
    """A stored artifact whose declaration is malformed is excluded from
    resolution (same upgrade-path posture as casa.setupTool)."""
    res = _publish(tmp_path, {"name": "p", "version": "1.0.0"})
    art = Path(res.path)
    pj = art / ".claude-plugin" / "plugin.json"
    os.chmod(art, 0o755)
    os.chmod(art / ".claude-plugin", 0o755)
    os.chmod(pj, 0o644)
    pj.write_text(json.dumps(_manifest(erase="Bad")), encoding="utf-8")
    meta_path = art / METADATA_FILENAME
    os.chmod(meta_path, 0o644)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["content_checksum"] = content_checksum(art)
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True),
                         encoding="utf-8")
    verdict = plugin_store.artifact_verdict(
        art, name="p", repo="o/r", revision="git:" + "a" * 40,
        subdir="", artifact_id=res.artifact_id)
    assert verdict == "erase_tool_invalid"
