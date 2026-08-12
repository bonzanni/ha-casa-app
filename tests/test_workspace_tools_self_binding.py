"""#481 — engagement-bound callers are target-bound on the workspace tools.

The three bridge-reachable workspace tools execute as casa-core (root); when
the authenticated caller IS an engagement, peek/delete refuse any target but
the caller itself and list filters to the caller's own entry — safety by
construction, not by grant configuration.
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

import tools

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _payload(res):
    return json.loads(res["content"][0]["text"])


def _bound(eng_id="aaaa1111"):
    eng = MagicMock()
    eng.id = eng_id
    eng.kind = "executor"
    eng.context_rebuild_pending = False
    return eng


async def test_peek_refuses_foreign_target_when_engagement_bound(
        tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_ENGAGEMENTS_ROOT", str(tmp_path))
    (tmp_path / "victim01").mkdir()
    tok = tools.engagement_var.set(_bound("attacker"))
    try:
        res = await tools.peek_engagement_workspace.handler(
            {"engagement_id": "victim01"})
    finally:
        tools.engagement_var.reset(tok)
    assert _payload(res)["kind"] == "cross_engagement_denied"


async def test_peek_allows_own_workspace_when_engagement_bound(
        tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_ENGAGEMENTS_ROOT", str(tmp_path))
    (tmp_path / "self0001").mkdir()
    tok = tools.engagement_var.set(_bound("self0001"))
    try:
        res = await tools.peek_engagement_workspace.handler(
            {"engagement_id": "self0001"})
    finally:
        tools.engagement_var.reset(tok)
    assert _payload(res)["status"] == "ok"


async def test_delete_refuses_foreign_target_when_engagement_bound(
        monkeypatch):
    registry = MagicMock()
    tools.init_tools(
        channel_manager=MagicMock(), bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=registry,
    )
    tok = tools.engagement_var.set(_bound("attacker"))
    try:
        res = await tools.delete_engagement_workspace.handler(
            {"engagement_id": "victim01", "force": True})
    finally:
        tools.engagement_var.reset(tok)
    assert _payload(res)["kind"] == "cross_engagement_denied"
    registry.get.assert_not_called()


async def test_list_filters_to_own_entry_when_engagement_bound(
        tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_ENGAGEMENTS_ROOT", str(tmp_path))
    for name in ("self0001", "other001"):
        d = tmp_path / name
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"status": "active"}))
    tok = tools.engagement_var.set(_bound("self0001"))
    try:
        res = await tools.list_engagement_workspaces.handler({})
    finally:
        tools.engagement_var.reset(tok)
    body = _payload(res)
    ids = [w["engagement_id"] for w in body["workspaces"]]
    assert ids == ["self0001"]


async def test_list_unbound_caller_sees_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_ENGAGEMENTS_ROOT", str(tmp_path))
    for name in ("a0000001", "b0000001"):
        (tmp_path / name).mkdir()
    res = await tools.list_engagement_workspaces.handler({})
    body = _payload(res)
    assert body["total"] == 2
