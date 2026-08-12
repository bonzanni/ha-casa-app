"""Tests for peek_engagement_workspace MCP tool (read-only inspection)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _seed(tmp_path: Path, eid: str):
    ws = tmp_path / eid
    ws.mkdir()
    (ws / "a.txt").write_text("hello world", encoding="utf-8")
    (ws / "nested").mkdir()
    (ws / "nested" / "b.txt").write_text("deep", encoding="utf-8")
    return ws


async def test_peek_returns_tree_when_no_path(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import peek_engagement_workspace

    _seed(tmp_path, "eng1")
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await peek_engagement_workspace.handler(
        {"engagement_id": "eng1"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert "tree" in payload
    names = [n["name"] for n in payload["tree"]]
    assert "a.txt" in names
    assert "nested" in names


async def test_tree_does_not_follow_symlinks_out_of_workspace(tmp_path, monkeypatch):
    """#324: the empty-path tree listing must not follow a symlink out of the
    workspace — a `secrets -> <outside>` link leaked outside names/types."""
    import tools as tools_mod
    from tools import peek_engagement_workspace

    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "top-secret.txt").write_text("x", encoding="utf-8")
    (outside / "sub" / "deeper.txt").write_text("y", encoding="utf-8")

    root = tmp_path / "engroot"
    root.mkdir()
    ws = root / "eng1"
    ws.mkdir()
    (ws / "real.txt").write_text("ok", encoding="utf-8")
    (ws / "link").symlink_to(outside)
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(root),
                        raising=False)

    result = await peek_engagement_workspace.handler({"engagement_id": "eng1"})
    payload = json.loads(result["content"][0]["text"])
    flat = json.dumps(payload["tree"])
    assert "top-secret.txt" not in flat
    assert "deeper.txt" not in flat
    by_name = {n["name"]: n for n in payload["tree"]}
    assert by_name["link"]["type"] == "symlink"
    assert "children" not in by_name["link"]
    assert by_name["real.txt"]["type"] == "file"


async def test_tree_labels_in_workspace_dir_symlink_without_recursing(tmp_path, monkeypatch):
    """A symlink to a directory INSIDE the workspace is still reported as a
    symlink and never expanded (no duplicate subtree, no cycle risk)."""
    import tools as tools_mod
    from tools import peek_engagement_workspace

    root = tmp_path / "engroot"
    root.mkdir()
    ws = root / "eng1"
    (ws / "nested").mkdir(parents=True)
    (ws / "nested" / "b.txt").write_text("deep", encoding="utf-8")
    (ws / "alias").symlink_to(ws / "nested")
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(root),
                        raising=False)

    result = await peek_engagement_workspace.handler({"engagement_id": "eng1"})
    payload = json.loads(result["content"][0]["text"])
    by_name = {n["name"]: n for n in payload["tree"]}
    assert by_name["alias"]["type"] == "symlink"
    assert "children" not in by_name["alias"]
    assert by_name["nested"]["type"] == "dir"


async def test_peek_returns_file_contents(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import peek_engagement_workspace

    _seed(tmp_path, "eng1")
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await peek_engagement_workspace.handler(
        {"engagement_id": "eng1", "path": "a.txt"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["contents"] == "hello world"


async def test_peek_rejects_path_traversal(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import peek_engagement_workspace

    _seed(tmp_path, "eng1")
    # Secret file outside the workspace.
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await peek_engagement_workspace.handler(
        {"engagement_id": "eng1", "path": "../secret.txt"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "error"
    assert payload["kind"] == "path_outside_workspace"


async def test_peek_caps_max_bytes(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import peek_engagement_workspace

    ws = tmp_path / "eng1"
    ws.mkdir()
    (ws / "big.txt").write_text("A" * 10000, encoding="utf-8")
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await peek_engagement_workspace.handler(
        {"engagement_id": "eng1", "path": "big.txt", "max_bytes": 100},
    )
    payload = json.loads(result["content"][0]["text"])
    assert len(payload["contents"]) == 100


async def test_peek_unknown_engagement(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import peek_engagement_workspace
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await peek_engagement_workspace.handler(
        {"engagement_id": "nope"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "error"
    assert payload["kind"] == "unknown_workspace"


async def test_peek_rejects_engagement_id_traversal(tmp_path, monkeypatch):
    """H15: engagement_id must not re-root the workspace. A secret seeded
    ABOVE the engagements root must never leak through '..', an absolute
    re-root, or an empty-path tree of a traversed location."""
    import tools as tools_mod
    from tools import peek_engagement_workspace

    # layout: tmp/data/engagements/eng1 (root), tmp/data/options.json (secret)
    data = tmp_path / "data"
    eng = data / "engagements"
    (eng / "eng1").mkdir(parents=True)
    (data / "options.json").write_text(
        '{"telegram_bot_token":"SECRET"}', encoding="utf-8")
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(eng),
                        raising=False)

    # dot-dot traversal into /data
    r = await peek_engagement_workspace.handler(
        {"engagement_id": "..", "path": "options.json"})
    p = json.loads(r["content"][0]["text"])
    assert p["status"] == "error"
    assert "SECRET" not in json.dumps(p)

    # nested dot-dot traversal
    r = await peek_engagement_workspace.handler(
        {"engagement_id": "../../config", "path": "plugin-env.conf"})
    p = json.loads(r["content"][0]["text"])
    assert p["status"] == "error"

    # absolute re-root
    r = await peek_engagement_workspace.handler(
        {"engagement_id": str(data), "path": "options.json"})
    p = json.loads(r["content"][0]["text"])
    assert p["status"] == "error"

    # empty-path tree of a traversed location must not leak
    r = await peek_engagement_workspace.handler({"engagement_id": ".."})
    p = json.loads(r["content"][0]["text"])
    assert p["status"] == "error"

    # legit id still works
    (eng / "eng1" / "a.txt").write_text("hello", encoding="utf-8")
    r = await peek_engagement_workspace.handler(
        {"engagement_id": "eng1", "path": "a.txt"})
    assert json.loads(r["content"][0]["text"])["contents"] == "hello"


async def test_peek_reads_only_byte_prefix_not_whole_file(tmp_path, monkeypatch):
    """M26: peek must read at most max_bytes off disk (bounded read), never
    load the whole file via read_text, and cap in BYTES not characters."""
    import pathlib
    import tools as tools_mod
    from tools import peek_engagement_workspace

    ws = tmp_path / "eng1"
    ws.mkdir()
    # 1000 x 'é' = 2000 bytes UTF-8 but 1000 code points.
    (ws / "multi.txt").write_text("é" * 1000, encoding="utf-8")
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    # Guard 1: whole-file read_text must never be called.
    def _boom(self, *a, **k):
        raise AssertionError("peek must not read the whole file via read_text()")
    monkeypatch.setattr(pathlib.Path, "read_text", _boom)

    result = await peek_engagement_workspace.handler(
        {"engagement_id": "eng1", "path": "multi.txt", "max_bytes": 1000},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "ok"
    # Guard 2: cap is in BYTES — 1000 bytes of 2-byte chars is <= ~500 chars
    # (499 + a possible trailing U+FFFD from a split char).
    assert len(payload["contents"].encode("utf-8", errors="replace")) <= 1003
