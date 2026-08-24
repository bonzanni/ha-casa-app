"""Tests for the v0.65.0 topic-retention integration seams.

Covers the 2026-07-10 topic-retention-cleanup design at the points where
the ledger meets the platform:

- [AR-4] ``tools._finalize_engagement`` appends to the topic ledger the
  moment the registry flips terminal — gated ONLY on ``topic_id`` (never
  on channel-manager presence), and a ledger failure never aborts the
  finalize funnel (the idempotency guard makes a partial finalize
  unretryable).
- The pre-driver abort path (``tools._abort_engagement_topic``) appends
  too (outcome="error") — those topics are today's most orphan-prone.
- [AR-7] the configurator's on-demand ``cleanup_engagement_topics`` tool.
- [AR-8] casa_core's periodic topics pass: skips cleanly when telegram is
  unconfigured, sweeps scope="due", and nags the operator for the
  "Delete messages" right at most once per boot.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import casa_core
import topic_ledger
import tools as tools_mod
from tools import cancel_engagement, init_tools

SUPERGROUP = -1001234567890


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_ledger(monkeypatch, tmp_path):
    """Private tmp ledger per test (nothing ever touches /data) and a fresh
    module lock (asyncio primitives bind to the first loop that acquires
    them; pytest-asyncio gives each test its own loop). Also clears the env
    fallback so chat_id resolution is deterministic per test."""
    monkeypatch.setattr(topic_ledger, "_LOCK", asyncio.Lock())
    monkeypatch.setattr(
        topic_ledger, "LEDGER_PATH", str(tmp_path / "topic-ledger.json"),
    )
    monkeypatch.delenv("TELEGRAM_ENGAGEMENT_SUPERGROUP_ID", raising=False)


@pytest.fixture(autouse=True)
def _quiet_agent_singletons(monkeypatch):
    """The finalize funnel consults agent.* singletons via getattr; pin them
    to None so no background retain/observer work runs regardless of test
    order."""
    import agent as agent_mod

    for attr in (
        "active_semantic_memory", "active_observer",
        "active_engagement_driver", "active_claude_code_driver",
    ):
        monkeypatch.setattr(agent_mod, attr, None, raising=False)


@pytest.fixture(autouse=True)
def _reset_permission_nag(monkeypatch):
    """[AR-8] once-per-boot flag — reset between tests."""
    monkeypatch.setattr(casa_core, "_topic_permission_notified", False)


# ---------------------------------------------------------------------------
# Harness — drive _finalize_engagement the way test_cancel_engagement_tool
# does: through cancel_engagement.handler, draining _specialist_bg_tasks.
# ---------------------------------------------------------------------------


def _telegram_channel(supergroup=SUPERGROUP):
    tch = MagicMock()
    tch.engagement_supergroup_id = supergroup
    tch.send = AsyncMock()          # #342: direct nag delivery path
    tch.send_to_topic = AsyncMock()
    # #678: the funnel PREFERS the paged rich sender, and a bare MagicMock
    # fabricates it — so `hasattr` found it, awaiting its non-awaitable return
    # raised TypeError, the funnel swallowed that and painted and closed the
    # topic anyway. These tests' close assertions were passing because the
    # double was broken. The real TelegramChannel has this method and returns
    # a message id, which is what the funnel now reads as confirmation.
    tch.send_response_to_topic = AsyncMock(return_value=4242)
    tch.update_topic_state = AsyncMock()
    tch.close_topic = AsyncMock()
    return tch


def _wire(reg, *, channel_manager):
    bus = MagicMock()
    bus.notify = AsyncMock()
    init_tools(
        channel_manager=channel_manager, bus=bus,
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
        trigger_registry=MagicMock(), engagement_registry=reg,
    )
    return bus


async def _make_engagement(tmp_path, *, topic_id):
    from engagement_registry import EngagementRegistry

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="t", origin={"role": "assistant", "channel": "telegram"},
        topic_id=topic_id,
    )
    return reg, rec


async def _cancel_and_drain(rec):
    res = await cancel_engagement.handler({"engagement_id": rec.id})
    payload = json.loads(res["content"][0]["text"])
    # L33: the funnel schedules retains as background tasks — drain them
    # before asserting (harness pattern from test_cancel_engagement_tool).
    pending = list(tools_mod._specialist_bg_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return payload


# ---------------------------------------------------------------------------
# [AR-4] finalize appends
# ---------------------------------------------------------------------------


async def test_finalize_appends_ledger_entry_with_chat_id(tmp_path):
    reg, rec = await _make_engagement(tmp_path, topic_id=42)
    tch = _telegram_channel()
    cm = MagicMock()
    cm.get.return_value = tch
    _wire(reg, channel_manager=cm)

    payload = await _cancel_and_drain(rec)

    assert payload["status"] == "ok"
    (entry,) = await topic_ledger.load()
    assert entry["engagement_id"] == rec.id
    assert entry["chat_id"] == SUPERGROUP
    assert entry["topic_id"] == 42
    assert entry["outcome"] == "cancelled"
    # The funnel continued past the append: topic still closed as before.
    assert tch.close_topic.await_count == 1


async def test_finalize_skips_append_when_topic_id_none(tmp_path):
    reg, rec = await _make_engagement(tmp_path, topic_id=None)
    cm = MagicMock()
    cm.get.return_value = _telegram_channel()
    _wire(reg, channel_manager=cm)

    payload = await _cancel_and_drain(rec)

    assert payload["status"] == "ok"
    assert await topic_ledger.load() == []


async def test_finalize_completes_when_ledger_append_raises(
    tmp_path, monkeypatch, caplog,
):
    """[AR-4] a ledger failure must never abort the finalize funnel — the
    idempotency guard makes a partial finalize unretryable."""
    reg, rec = await _make_engagement(tmp_path, topic_id=42)
    tch = _telegram_channel()
    cm = MagicMock()
    cm.get.return_value = tch
    bus = _wire(reg, channel_manager=cm)

    async def _boom(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(topic_ledger, "append", _boom)

    with caplog.at_level(logging.WARNING, logger="tools"):
        payload = await _cancel_and_drain(rec)

    assert payload["status"] == "ok"
    assert rec.status == "cancelled", "registry must still reach terminal"
    assert tch.close_topic.await_count == 1
    assert bus.notify.await_count >= 1, "Ellen NOTIFY must still go out"
    assert any(
        "topic ledger append failed" in r.getMessage() for r in caplog.records
    ), "the ledger failure must be warned about, not swallowed silently"


async def test_finalize_appends_even_when_close_topic_raises(tmp_path):
    """The append lands before (and independently of) the close — a raising
    close_topic must not lose the ledger record."""
    reg, rec = await _make_engagement(tmp_path, topic_id=42)
    tch = _telegram_channel()
    tch.close_topic = AsyncMock(side_effect=RuntimeError("telegram down"))
    cm = MagicMock()
    cm.get.return_value = tch
    _wire(reg, channel_manager=cm)

    payload = await _cancel_and_drain(rec)

    assert payload["status"] == "ok"
    assert rec.status == "cancelled", "registry must still reach terminal"
    (entry,) = await topic_ledger.load()
    assert entry["engagement_id"] == rec.id
    assert entry["topic_id"] == 42
    assert entry["outcome"] == "cancelled"


async def test_finalize_appends_without_channel_manager_env_fallback(
    tmp_path, monkeypatch,
):
    """Gate is ONLY topic_id — telegram momentarily unwired must not skip
    the append; chat_id falls back to the boot env."""
    monkeypatch.setenv("TELEGRAM_ENGAGEMENT_SUPERGROUP_ID", "-100777")
    reg, rec = await _make_engagement(tmp_path, topic_id=42)
    _wire(reg, channel_manager=None)

    payload = await _cancel_and_drain(rec)

    assert payload["status"] == "ok"
    (entry,) = await topic_ledger.load()
    assert entry["engagement_id"] == rec.id
    assert entry["chat_id"] == -100777


async def test_finalize_appends_without_channel_manager_chat_id_none(tmp_path):
    """No channel manager AND no env: append still lands, chat_id None (the
    ledger keeps such entries but never auto-deletes them)."""
    reg, rec = await _make_engagement(tmp_path, topic_id=42)
    _wire(reg, channel_manager=None)

    payload = await _cancel_and_drain(rec)

    assert payload["status"] == "ok"
    (entry,) = await topic_ledger.load()
    assert entry["chat_id"] is None
    assert entry["topic_id"] == 42


# ---------------------------------------------------------------------------
# Abort path appends
# ---------------------------------------------------------------------------


async def test_abort_path_appends_error_entry(tmp_path):
    tch = _telegram_channel()

    await tools_mod._abort_engagement_topic(tch, "eng-abort", 55)

    (entry,) = await topic_ledger.load()
    assert entry["engagement_id"] == "eng-abort"
    assert entry["chat_id"] == SUPERGROUP
    assert entry["topic_id"] == 55
    assert entry["outcome"] == "error"
    # Existing best-effort behaviour unchanged.
    assert tch.update_topic_state.await_count == 1
    assert tch.close_topic.await_count == 1


async def test_abort_path_appends_even_when_channel_none(tmp_path):
    """Same gate-only-on-topic_id doctrine as finalize: a dead channel must
    not lose the ledger record."""
    await tools_mod._abort_engagement_topic(None, "eng-nochan", 56)

    (entry,) = await topic_ledger.load()
    assert entry["engagement_id"] == "eng-nochan"
    assert entry["chat_id"] is None
    assert entry["outcome"] == "error"


async def test_abort_path_no_append_without_topic(tmp_path):
    await tools_mod._abort_engagement_topic(_telegram_channel(), "eng-x", None)
    assert await topic_ledger.load() == []


async def test_abort_path_never_raises_when_append_fails(tmp_path, monkeypatch):
    async def _boom(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(topic_ledger, "append", _boom)
    tch = _telegram_channel()

    await tools_mod._abort_engagement_topic(tch, "eng-abort", 55)  # no raise

    assert tch.close_topic.await_count == 1, "close still attempted"


# ---------------------------------------------------------------------------
# [AR-7] cleanup_engagement_topics tool
# ---------------------------------------------------------------------------


_CANNED_SWEEP = {
    "deleted": 2, "kept": 1, "dropped_mismatched": 0,
    "failures": [], "needs_permission": False,
}


def _sweep_recorder(monkeypatch, result=None):
    calls: list[dict] = []
    canned = dict(result or _CANNED_SWEEP)

    async def fake_sweep(channel, *, chat_id, scope="due", dry_run=False, **kw):
        calls.append({
            "channel": channel, "chat_id": chat_id,
            "scope": scope, "dry_run": dry_run,
        })
        return dict(canned)

    monkeypatch.setattr(topic_ledger, "sweep_topics", fake_sweep)
    return calls


def _wire_tool(tch):
    cm = MagicMock()
    cm.get.return_value = tch
    _wire(MagicMock(), channel_manager=cm)
    return cm


async def test_tool_registered_in_casa_tools():
    names = {
        getattr(t, "name", None) or getattr(t, "__name__", "")
        for t in tools_mod.CASA_TOOLS
    }
    assert "cleanup_engagement_topics" in names


async def test_tool_default_scope_due(monkeypatch):
    calls = _sweep_recorder(monkeypatch)
    tch = _telegram_channel()
    _wire_tool(tch)

    res = await tools_mod.cleanup_engagement_topics.handler({})
    payload = json.loads(res["content"][0]["text"])

    assert calls == [{
        "channel": tch, "chat_id": SUPERGROUP,
        "scope": "due", "dry_run": False,
    }]
    assert payload == {"status": "ok", **_CANNED_SWEEP}
    assert not res.get("is_error")


async def test_tool_all_terminal_scope(monkeypatch):
    calls = _sweep_recorder(monkeypatch)
    _wire_tool(_telegram_channel())

    # v0.69.12: all_terminal is configurator-only.
    import agent as agent_mod
    tok = agent_mod.origin_var.set({"role": "configurator"})
    try:
        res = await tools_mod.cleanup_engagement_topics.handler(
            {"scope": "all_terminal"},
        )
    finally:
        agent_mod.origin_var.reset(tok)
    payload = json.loads(res["content"][0]["text"])

    assert payload["status"] == "ok"
    assert calls[0]["scope"] == "all_terminal"
    assert calls[0]["dry_run"] is False


async def test_tool_due_scope_allowed_for_assistant(monkeypatch):
    """v0.69.12: Ellen (assistant) can run the safe due-scope cleanup directly
    (no delegation) — X2 resolved cleared the AR-7 deferral."""
    calls = _sweep_recorder(monkeypatch)
    _wire_tool(_telegram_channel())

    import agent as agent_mod
    tok = agent_mod.origin_var.set({"role": "assistant"})
    try:
        res = await tools_mod.cleanup_engagement_topics.handler({"scope": "due"})
    finally:
        agent_mod.origin_var.reset(tok)
    payload = json.loads(res["content"][0]["text"])

    assert payload["status"] == "ok"
    assert calls[0]["scope"] == "due"


async def test_tool_all_terminal_refused_for_assistant(monkeypatch):
    """v0.69.12: the irreversible all_terminal purge stays configurator-only —
    a non-privileged caller (Ellen) is refused and nudged to due."""
    calls = _sweep_recorder(monkeypatch)
    _wire_tool(_telegram_channel())

    import agent as agent_mod
    tok = agent_mod.origin_var.set({"role": "assistant"})
    try:
        res = await tools_mod.cleanup_engagement_topics.handler(
            {"scope": "all_terminal"},
        )
    finally:
        agent_mod.origin_var.reset(tok)
    payload = json.loads(res["content"][0]["text"])

    assert payload["status"] == "error"
    assert payload["kind"] == "not_authorized"
    assert "due" in payload["message"]
    assert calls == []          # never reached the sweep


async def test_tool_dry_run_passthrough(monkeypatch):
    calls = _sweep_recorder(monkeypatch)
    _wire_tool(_telegram_channel())

    res = await tools_mod.cleanup_engagement_topics.handler(
        {"scope": "due", "dry_run": True},
    )
    payload = json.loads(res["content"][0]["text"])

    assert payload["status"] == "ok"
    assert calls[0]["dry_run"] is True


async def test_tool_bad_scope_rejected(monkeypatch):
    calls = _sweep_recorder(monkeypatch)
    _wire_tool(_telegram_channel())

    res = await tools_mod.cleanup_engagement_topics.handler(
        {"scope": "everything"},
    )
    payload = json.loads(res["content"][0]["text"])

    assert payload["status"] == "error"
    assert payload["kind"] == "bad_scope"
    assert calls == [], "sweep must not run on a bad scope"
    assert res.get("is_error") is True


async def test_tool_telegram_channel_missing(monkeypatch):
    calls = _sweep_recorder(monkeypatch)
    cm = MagicMock()
    cm.get.return_value = None
    _wire(MagicMock(), channel_manager=cm)

    res = await tools_mod.cleanup_engagement_topics.handler({})
    payload = json.loads(res["content"][0]["text"])

    assert payload["status"] == "error"
    assert payload["kind"] == "telegram_not_configured"
    assert calls == []


async def test_tool_supergroup_unconfigured(monkeypatch):
    calls = _sweep_recorder(monkeypatch)
    _wire_tool(_telegram_channel(supergroup=None))

    res = await tools_mod.cleanup_engagement_topics.handler({})
    payload = json.loads(res["content"][0]["text"])

    assert payload["status"] == "error"
    assert payload["kind"] == "telegram_not_configured"
    assert calls == []


async def test_tool_all_terminal_purges_real_ledger_end_to_end():
    """Tool → real sweep_topics → real tmp ledger file, no sweep monkeypatch:
    the payload carries the true counts + targets and the file empties."""
    ledger = Path(topic_ledger.LEDGER_PATH)
    now = 1_000_000.0
    ledger.write_text(json.dumps([
        {
            "engagement_id": "e1", "chat_id": SUPERGROUP, "topic_id": 611,
            "outcome": "completed", "closed_at": now,
            "delete_after": now + 1,
        },
        {
            "engagement_id": "e2", "chat_id": SUPERGROUP, "topic_id": 612,
            "outcome": "error", "closed_at": now,
            "delete_after": now + 1,
        },
    ]), encoding="utf-8")
    tch = _telegram_channel()
    tch.delete_topic = AsyncMock()
    _wire_tool(tch)

    import agent as agent_mod
    tok = agent_mod.origin_var.set({"role": "configurator"})  # all_terminal (v0.69.12)
    try:
        res = await tools_mod.cleanup_engagement_topics.handler(
            {"scope": "all_terminal"},
        )
    finally:
        agent_mod.origin_var.reset(tok)
    payload = json.loads(res["content"][0]["text"])

    assert not res.get("is_error")
    assert payload["status"] == "ok"
    assert payload["deleted"] == 2
    assert payload["kept"] == 0
    assert payload["failures"] == []
    assert payload["dry_run"] is False
    assert payload["targets"] == [
        {"engagement_id": "e1", "topic_id": 611},
        {"engagement_id": "e2", "topic_id": 612},
    ]
    assert tch.delete_topic.await_count == 2
    assert json.loads(ledger.read_text(encoding="utf-8")) == []


# ---------------------------------------------------------------------------
# [AR-8] casa_core periodic topics pass
# ---------------------------------------------------------------------------


async def test_sweep_pass_skips_when_channel_none(monkeypatch):
    calls = _sweep_recorder(monkeypatch)
    cm = MagicMock()
    cm.get.return_value = None

    await casa_core._sweep_engagement_topics(cm)

    assert calls == []


async def test_sweep_pass_skips_when_supergroup_unconfigured(monkeypatch):
    calls = _sweep_recorder(monkeypatch)
    tch = _telegram_channel(supergroup=None)
    cm = MagicMock()
    cm.get.return_value = tch

    await casa_core._sweep_engagement_topics(cm)

    assert calls == []
    assert tch.send.await_count == 0


async def test_sweep_pass_sweeps_due_scope(monkeypatch):
    calls = _sweep_recorder(monkeypatch)
    tch = _telegram_channel()
    cm = MagicMock()
    cm.get.return_value = tch

    await casa_core._sweep_engagement_topics(cm)

    assert calls == [{
        "channel": tch, "chat_id": SUPERGROUP,
        "scope": "due", "dry_run": False,
    }]
    assert tch.send.await_count == 0, "no nag without needs_permission"


async def test_sweep_pass_notifies_once_per_boot_on_needs_permission(
    monkeypatch,
):
    result = dict(_CANNED_SWEEP)
    result.update({"deleted": 0, "kept": 3, "needs_permission": True})
    calls = _sweep_recorder(monkeypatch, result=result)
    tch = _telegram_channel()
    cm = MagicMock()
    cm.get.return_value = tch

    await casa_core._sweep_engagement_topics(cm)
    await casa_core._sweep_engagement_topics(cm)

    assert len(calls) == 2, "the nag dedupe must not stop the sweeps"
    # #342: the nag is a DIRECT channel send, consumed on send success.
    assert tch.send.await_count == 1, "operator is nagged at most once per boot"
    message, _ctx = tch.send.await_args.args
    assert "Delete messages" in message


async def test_sweep_pass_retries_nag_when_notify_fails(monkeypatch, caplog):
    """#342: a failed nag SEND must NOT consume the once-per-boot flag — the
    operator never saw it. Retried at the next sweep, then consumed on the
    first successful delivery."""
    result = dict(_CANNED_SWEEP)
    result.update({"deleted": 0, "kept": 3, "needs_permission": True})
    _sweep_recorder(monkeypatch, result=result)
    tch = _telegram_channel()
    tch.send = AsyncMock(side_effect=[RuntimeError("telegram down"), None, None])
    cm = MagicMock()
    cm.get.return_value = tch

    with caplog.at_level(logging.WARNING, logger="casa_core"):
        await casa_core._sweep_engagement_topics(cm)  # send raises
    await casa_core._sweep_engagement_topics(cm)      # retried, delivers
    await casa_core._sweep_engagement_topics(cm)      # consumed — silent

    assert tch.send.await_count == 2, (
        "exactly one retry, then the flag is consumed")
    assert any(
        "permission nag notify failed" in r.getMessage() for r in caplog.records
    )


async def test_sweep_pass_survives_sweep_exception(monkeypatch, caplog):
    """The ledger's sweep handles per-entry telegram errors itself but can
    raise if the channel object is broken — the wrapper must guard."""

    async def _boom(channel, **kwargs):
        raise RuntimeError("broken channel object")

    monkeypatch.setattr(topic_ledger, "sweep_topics", _boom)
    tch = _telegram_channel()
    cm = MagicMock()
    cm.get.return_value = tch

    with caplog.at_level(logging.WARNING, logger="casa_core"):
        await casa_core._sweep_engagement_topics(cm)  # no raise

    assert tch.send.await_count == 0
    assert any(
        "topic sweep" in r.getMessage() for r in caplog.records
    ), "the failure must be warned about"


async def test_sweep_pass_logs_info_counts_when_deleted(monkeypatch, caplog):
    result = dict(_CANNED_SWEEP)
    result.update({"deleted": 3, "kept": 1})
    _sweep_recorder(monkeypatch, result=result)
    cm = MagicMock()
    cm.get.return_value = _telegram_channel()

    with caplog.at_level(logging.INFO, logger="casa_core"):
        await casa_core._sweep_engagement_topics(cm)

    assert any(
        "deleted=3" in r.getMessage() and "kept=1" in r.getMessage()
        for r in caplog.records
    )


async def test_assistant_config_grants_cleanup_tool():
    """v0.69.12: Ellen's runtime.yaml grants the (due-only) cleanup tool so she
    can tidy the engagement group without delegation."""
    import yaml
    root = Path(__file__).resolve().parent.parent
    rt = root / "casa/rootfs/opt/casa/defaults/agents/assistant/runtime.yaml"
    allowed = yaml.safe_load(rt.read_text(encoding="utf-8"))["tools"]["allowed"]
    assert "mcp__casa-framework__cleanup_engagement_topics" in allowed


async def test_sweep_pass_nag_not_consumed_when_channel_unready(monkeypatch):
    """Sol r1-2: an unready channel (send() would log-and-drop) must not
    consume the once-per-boot nag flag."""
    result = dict(_CANNED_SWEEP)
    result.update({"deleted": 0, "kept": 3, "needs_permission": True})
    _sweep_recorder(monkeypatch, result=result)
    tch = _telegram_channel()
    tch.is_ready = False
    cm = MagicMock()
    cm.get.return_value = tch

    await casa_core._sweep_engagement_topics(cm)
    assert tch.send.await_count == 0
    assert casa_core._topic_permission_notified is False
