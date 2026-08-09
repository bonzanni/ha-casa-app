"""Tests for delete_engagement_workspace MCP tool."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _make_ws(tmp_path: Path, eid: str, status: str = "COMPLETED"):
    ws = tmp_path / eid
    ws.mkdir()
    (ws / "x.txt").write_text("y", encoding="utf-8")
    (ws / ".casa-meta.json").write_text(json.dumps({
        "engagement_id": eid, "status": status,
    }), encoding="utf-8")
    return ws


async def test_delete_terminal_workspace(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import delete_engagement_workspace
    from engagement_registry import EngagementRegistry, EngagementRecord

    _make_ws(tmp_path, "eng-done")
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "t.json"), bus=None)
    reg._records["eng-done"] = EngagementRecord(
        id="eng-done", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="completed", topic_id=None,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=0.0, sdk_session_id=None, origin={}, task="t",
    )
    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await delete_engagement_workspace.handler(
        {"engagement_id": "eng-done"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "ok"
    assert not (tmp_path / "eng-done").exists()


async def test_delete_also_removes_log_dir(tmp_path, monkeypatch):
    """v0.64.0: /var/log/casa-engagement-<id> follows the workspace on the
    caller-managed deletion path too — the sweep can never find it once the
    workspace is gone."""
    import tools as tools_mod
    from drivers import workspace as ws_mod
    from tools import delete_engagement_workspace
    from engagement_registry import EngagementRegistry, EngagementRecord

    _make_ws(tmp_path, "eng-done")
    log_root = tmp_path / "var-log"
    log_root.mkdir()
    (log_root / "casa-engagement-eng-done").mkdir()
    monkeypatch.setattr(ws_mod, "ENGAGEMENT_LOG_ROOT", str(log_root))

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "t.json"), bus=None)
    reg._records["eng-done"] = EngagementRecord(
        id="eng-done", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="completed", topic_id=None,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=0.0, sdk_session_id=None, origin={}, task="t",
    )
    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await delete_engagement_workspace.handler(
        {"engagement_id": "eng-done"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "ok"
    assert not (tmp_path / "eng-done").exists()
    assert not (log_root / "casa-engagement-eng-done").exists()


async def test_delete_prunes_identity_for_real_allocated_uid(tmp_path, monkeypatch):
    """Task 8 (containment stage 2): this caller-managed deletion path has
    its own EngagementRecord — a real allocated_uid must be pruned from
    passwd/group once the workspace is gone."""
    import engagement_uids as eu_mod
    import tools as tools_mod
    from tools import delete_engagement_workspace
    from engagement_registry import EngagementRegistry, EngagementRecord

    _make_ws(tmp_path, "eng-uid")
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "t.json"), bus=None)
    reg._records["eng-uid"] = EngagementRecord(
        id="eng-uid", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="completed", topic_id=None,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=0.0, sdk_session_id=None, origin={}, task="t",
        allocated_uid=eu_mod.UID_BASE + 9,
    )
    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    calls: list[int] = []
    monkeypatch.setattr(eu_mod, "prune_identity", lambda uid: calls.append(uid))

    result = await delete_engagement_workspace.handler(
        {"engagement_id": "eng-uid"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "ok"
    assert calls == [eu_mod.UID_BASE + 9]


async def test_delete_does_not_prune_unallocated_uid(tmp_path, monkeypatch):
    """A legacy/unallocated engagement (UNALLOCATED_UID default) must never
    trigger a prune attempt — there is no real uid to prune."""
    import engagement_uids as eu_mod
    import tools as tools_mod
    from tools import delete_engagement_workspace
    from engagement_registry import EngagementRegistry, EngagementRecord

    _make_ws(tmp_path, "eng-legacy")
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "t.json"), bus=None)
    reg._records["eng-legacy"] = EngagementRecord(
        id="eng-legacy", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="completed", topic_id=None,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=0.0, sdk_session_id=None, origin={}, task="t",
    )
    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    calls: list[int] = []
    monkeypatch.setattr(eu_mod, "prune_identity", lambda uid: calls.append(uid))

    result = await delete_engagement_workspace.handler(
        {"engagement_id": "eng-legacy"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "ok"
    assert calls == []


async def test_refuses_undergoing_without_force(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import delete_engagement_workspace
    from engagement_registry import EngagementRegistry, EngagementRecord

    _make_ws(tmp_path, "eng-running", status="UNDERGOING")
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "t.json"), bus=None)
    reg._records["eng-running"] = EngagementRecord(
        id="eng-running", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="active", topic_id=None,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={}, task="t",
    )
    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await delete_engagement_workspace.handler(
        {"engagement_id": "eng-running"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "error"
    assert payload["kind"] == "refused"
    assert (tmp_path / "eng-running").exists()  # untouched


async def test_unknown_engagement_error(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import delete_engagement_workspace
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "t.json"), bus=None)
    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await delete_engagement_workspace.handler(
        {"engagement_id": "nope"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "error"
    assert payload["kind"] == "unknown_engagement"


# ---------------------------------------------------------------------------
# Bug 12 (v0.14.6): the live-state guard must include "idle".
# Pre-fix it only checked "active" — an idle engagement (SDK-suspended
# after 24h) had its s6 service still running, but a non-force delete
# still tore down the workspace under it.
# ---------------------------------------------------------------------------


async def test_refuses_idle_without_force(tmp_path, monkeypatch):
    import tools as tools_mod
    from tools import delete_engagement_workspace
    from engagement_registry import EngagementRegistry, EngagementRecord

    _make_ws(tmp_path, "eng-idle", status="UNDERGOING")
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "t.json"), bus=None)
    reg._records["eng-idle"] = EngagementRecord(
        id="eng-idle", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="idle", topic_id=None,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id="sess-x", origin={}, task="t",
    )
    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await delete_engagement_workspace.handler(
        {"engagement_id": "eng-idle"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "error"
    assert payload["kind"] == "refused"
    assert "idle" in payload["message"]
    assert (tmp_path / "eng-idle").exists()  # workspace untouched


async def test_force_deletes_idle(tmp_path, monkeypatch):
    """force=true on idle still finalises and deletes (parity with active)."""
    import tools as tools_mod
    from tools import delete_engagement_workspace
    from engagement_registry import EngagementRegistry, EngagementRecord

    _make_ws(tmp_path, "eng-idle", status="UNDERGOING")
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "t.json"), bus=None)
    reg._records["eng-idle"] = EngagementRecord(
        id="eng-idle", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="idle", topic_id=None,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id="sess-x", origin={}, task="t",
    )
    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)

    result = await delete_engagement_workspace.handler(
        {"engagement_id": "eng-idle", "force": True},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "ok"
    assert not (tmp_path / "eng-idle").exists()


async def test_force_delete_persist_failure_leaves_workspace(
        tmp_path, monkeypatch):
    """#301: PERSIST_FAILED means the record rolled back and is STILL LIVE —
    the workspace and log dir must survive, and the caller gets a retryable
    error, exactly as cancel_engagement/emit_completion already honor it."""
    import tools as tools_mod
    from tools import FinalizeResult, delete_engagement_workspace
    from engagement_registry import EngagementRegistry, EngagementRecord

    _make_ws(tmp_path, "eng-live", status="UNDERGOING")
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "t.json"), bus=None)
    reg._records["eng-live"] = EngagementRecord(
        id="eng-live", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="active", topic_id=None,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={}, task="t",
    )
    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)
    monkeypatch.setattr(
        tools_mod, "_finalize_engagement",
        AsyncMock(return_value=FinalizeResult.PERSIST_FAILED),
    )

    result = await delete_engagement_workspace.handler(
        {"engagement_id": "eng-live", "force": True},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "error"
    assert payload["kind"] == "finalize_persist_failed"
    assert payload.get("retryable") is True
    assert (tmp_path / "eng-live").exists()  # workspace untouched


@pytest.mark.parametrize("bad_force", ["false", "true", 1, "yes", [True]])
async def test_non_boolean_force_is_rejected(tmp_path, monkeypatch, bad_force):
    """#301: the internal/MCP forwarding path does not enforce tool schemas,
    so truthiness coercion turned the string "false" into an authorization to
    cancel+delete a live engagement. Non-boolean force is a bad_request."""
    import tools as tools_mod
    from tools import delete_engagement_workspace
    from engagement_registry import EngagementRegistry, EngagementRecord

    _make_ws(tmp_path, "eng-live", status="UNDERGOING")
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "t.json"), bus=None)
    reg._records["eng-live"] = EngagementRecord(
        id="eng-live", kind="executor", role_or_type="hello-driver",
        driver="claude_code", status="active", topic_id=None,
        started_at=0.0, last_user_turn_ts=0.0, last_idle_reminder_ts=0.0,
        completed_at=None, sdk_session_id=None, origin={}, task="t",
    )
    monkeypatch.setattr(tools_mod, "_engagement_registry", reg)
    monkeypatch.setattr(tools_mod, "_ENGAGEMENTS_ROOT", str(tmp_path),
                        raising=False)
    finalize = AsyncMock()
    monkeypatch.setattr(tools_mod, "_finalize_engagement", finalize)

    result = await delete_engagement_workspace.handler(
        {"engagement_id": "eng-live", "force": bad_force},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "error"
    assert payload["kind"] == "bad_request"
    finalize.assert_not_awaited()
    assert (tmp_path / "eng-live").exists()  # workspace untouched


async def test_force_delete_writes_meta_scope_summary(tmp_path, monkeypatch):
    """M2.G4 (rewritten for the shared-bank rearch) — force=True on a
    still-live engagement must write a summary before pulling the workspace.
    The regression intent is preserved: force-delete is NOT silent."""
    import sys
    import agent as agent_mod
    import delegated_memory
    from engagement_registry import EngagementRegistry
    from tools import delete_engagement_workspace, init_tools

    reg = EngagementRegistry(
        tombstone_path=str(tmp_path / "e.json"), bus=None,
    )
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="t",
        origin={
            "role": "assistant", "channel": "telegram",
            "chat_id": "456", "cid": "abc",
        },
        topic_id=99,
    )
    # Engagement starts in 'active' (live) status — force=True path.

    # Recording semantic-memory fake exposed on the agent module the way
    # the production singleton would be.
    class _Sem:
        def __init__(self):
            self.retain_calls = []

        async def retain(self, bank, items, *, async_=True):
            self.retain_calls.append({"bank": bank, "items": items})

    sem = _Sem()
    monkeypatch.setattr(agent_mod, "active_semantic_memory", sem, raising=False)

    async def _fake_classify(text):
        return "private"

    monkeypatch.setattr(delegated_memory, "classify_tier", _fake_classify)

    fake_agent_mod = MagicMock()
    fake_agent_mod.active_semantic_memory = sem
    fake_agent_mod.active_engagement_driver = None
    fake_agent_mod.active_claude_code_driver = None
    monkeypatch.setitem(sys.modules, "agent", fake_agent_mod)

    tch = MagicMock()
    tch.send_to_topic = AsyncMock()
    tch.close_topic = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = tch
    bus = MagicMock()
    bus.notify = AsyncMock()
    init_tools(
        channel_manager=cm, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )

    res = await delete_engagement_workspace.handler({
        "engagement_id": rec.id, "force": True,
    })
    payload = json.loads(res["content"][0]["text"])
    assert payload["status"] == "ok"

    # L33 moved the retains off the turn's critical path into background
    # tasks (_finalize_engagement schedules retain_delegated via
    # asyncio.create_task) — drain them before asserting, else the fake
    # deterministically records nothing.
    import tools as tools_mod
    pending = list(tools_mod._specialist_bg_tasks)
    if pending:
        await asyncio.gather(*pending)

    # A structured engagement summary was retained on the shared `casa` bank
    # with status=='cancelled' — force-delete finalises as cancelled,
    # confirming that force-delete is NOT silent.
    assert sem.retain_calls, "expected a retain on force-delete; got none"
    summaries = [
        json.loads(i["content"])
        for c in sem.retain_calls for i in c["items"]
    ]
    eng_summary = next(
        (s for s in summaries if s.get("kind") == "engagement_summary"),
        None,
    )
    assert eng_summary is not None, (
        f"expected engagement_summary in retain; got: {summaries}"
    )
    assert eng_summary["status"] == "cancelled", (
        f"force-delete finalises as cancelled; got: {eng_summary['status']}"
    )
    assert eng_summary["engagement_id"] == rec.id
