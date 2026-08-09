"""Tests for the list_engagement_workspaces MCP tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


def _make_workspace(root: Path, eid: str, status: str):
    from drivers.workspace import casa_meta_path, provision_control_dir

    ws = root / eid
    ws.mkdir()
    (ws / "file.txt").write_text("hello", encoding="utf-8")
    meta = {
        "engagement_id": eid,
        "executor_type": "hello-driver",
        "status": status,
        "created_at": "2026-04-01T00:00:00Z",
        "finished_at": ("2026-04-01T00:05:00Z" if status != "UNDERGOING" else None),
        "retention_until": ("2099-01-01T00:00:00Z" if status != "UNDERGOING" else None),
    }
    # Task 4 (containment stage 2): .casa-meta.json lives in the control dir.
    provision_control_dir(eid)
    Path(casa_meta_path(eid)).write_text(json.dumps(meta), encoding="utf-8")
    return ws


async def test_list_returns_all_workspaces_with_meta(tmp_path, monkeypatch):
    from tools import list_engagement_workspaces
    import tools as tools_mod

    _make_workspace(tmp_path, "eng-a", status="UNDERGOING")
    _make_workspace(tmp_path, "eng-b", status="COMPLETED")

    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await list_engagement_workspaces.handler({})
    payload = json.loads(result["content"][0]["text"])
    assert "workspaces" in payload
    ids = {w["engagement_id"] for w in payload["workspaces"]}
    assert ids == {"eng-a", "eng-b"}
    for w in payload["workspaces"]:
        assert w["size_bytes"] > 0
        assert w["status"] in ("UNDERGOING", "COMPLETED")


async def test_list_filters_by_status(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import list_engagement_workspaces

    _make_workspace(tmp_path, "eng-a", status="UNDERGOING")
    _make_workspace(tmp_path, "eng-b", status="COMPLETED")
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await list_engagement_workspaces.handler({"status": "COMPLETED"})
    payload = json.loads(result["content"][0]["text"])
    ids = {w["engagement_id"] for w in payload["workspaces"]}
    assert ids == {"eng-b"}


async def test_list_truncates_above_100(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import list_engagement_workspaces

    for i in range(105):
        _make_workspace(tmp_path, f"eng-{i:03d}", status="COMPLETED")
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await list_engagement_workspaces.handler({})
    payload = json.loads(result["content"][0]["text"])
    assert payload["truncated"] is True
    assert payload["total"] == 105
    assert len(payload["workspaces"]) == 100


async def test_list_returns_empty_for_missing_root(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import list_engagement_workspaces
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT",
                        str(tmp_path / "nope"), raising=False)

    result = await list_engagement_workspaces.handler({})
    payload = json.loads(result["content"][0]["text"])
    assert payload["workspaces"] == []
