"""#541 — send_media's specialist-context outbound quota.

A specialist context (interactive engagement record or delegated turn with
the server-stamped ``_delegation_id``) has a code-owned lifetime budget of
``_SPECIALIST_MEDIA_SEND_BUDGET`` attempts; a specialist-classified call
with NO quota key is refused, never unmetered; attempts count (no refunds);
residents and executors are untouched.
"""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

import agent as agent_mod
import plugin_outbox
import tools

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

PDF = b"%PDF-1.7\n" + b"x" * 100


def _payload(res):
    return json.loads(res["content"][0]["text"])


@pytest.fixture
def wired(tmp_path):
    ob = plugin_outbox.init_outbox(str(tmp_path / "plugin-outbox"))
    ch = MagicMock()
    ch.send_media = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = ch
    tools.init_tools(
        channel_manager=cm, bus=MagicMock(), specialist_registry=MagicMock(),
        mcp_registry=MagicMock(), trigger_registry=MagicMock(),
        engagement_registry=MagicMock(),
    )
    tools._MEDIA_SEND_DEBITS.clear()
    try:
        yield ob, ch
    finally:
        tools._MEDIA_SEND_DEBITS.clear()
        ob.close()
        plugin_outbox._OUTBOX = None


def _drop(outbox, name, data=PDF):
    p = os.path.join(outbox._root_realpath, name)
    with open(p, "wb") as fh:
        fh.write(data)
    return p


def _delegated_origin(**extra):
    return {"role": "assistant", "channel": "telegram",
            "chat_id": 1197017861, "delegation_depth": 1, **extra}


async def _send(ob, name):
    path = _drop(ob, name)
    return _payload(await tools.send_media.handler(
        {"path": path, "kind": "document"}))


async def test_delegated_specialist_budget_exhausts(wired):
    ob, ch = wired
    token = agent_mod.origin_var.set(
        _delegated_origin(_delegation_id="d1" * 16))
    try:
        for i in range(tools._SPECIALIST_MEDIA_SEND_BUDGET):
            body = await _send(ob, f"f{i}.pdf")
            assert body["status"] == "ok"
        body = await _send(ob, "over.pdf")
    finally:
        agent_mod.origin_var.reset(token)
    assert body["kind_error"] == "media_send_budget_exhausted"
    assert ch.send_media.await_count == tools._SPECIALIST_MEDIA_SEND_BUDGET


async def test_delegated_specialist_without_key_is_refused(wired):
    ob, ch = wired
    token = agent_mod.origin_var.set(_delegated_origin())
    try:
        body = await _send(ob, "f.pdf")
    finally:
        agent_mod.origin_var.reset(token)
    assert body["kind_error"] == "quota_key_missing"
    ch.send_media.assert_not_awaited()


async def test_failed_attempts_are_not_refunded(wired):
    ob, ch = wired
    from telegram.error import BadRequest
    ch.send_media.side_effect = BadRequest("nope")
    token = agent_mod.origin_var.set(
        _delegated_origin(_delegation_id="d2" * 16))
    try:
        for i in range(tools._SPECIALIST_MEDIA_SEND_BUDGET):
            body = await _send(ob, f"f{i}.pdf")
            assert body["kind_error"] == "rejected"
        body = await _send(ob, "over.pdf")
    finally:
        agent_mod.origin_var.reset(token)
    assert body["kind_error"] == "media_send_budget_exhausted"


async def test_specialist_engagement_keys_on_record_id(wired):
    ob, ch = wired
    eng = MagicMock()
    eng.kind = "specialist"
    eng.id = "abc123"
    eng.status = "active"
    eng.allocated_uid = None
    eng.origin = {"role": "finance", "channel": "telegram",
                  "chat_id": 1197017861}
    eng.context_rebuild_pending = False
    tok = tools.engagement_var.set(eng)
    try:
        for i in range(tools._SPECIALIST_MEDIA_SEND_BUDGET):
            body = await _send(ob, f"f{i}.pdf")
            assert body["status"] == "ok"
        body = await _send(ob, "over.pdf")
    finally:
        tools.engagement_var.reset(tok)
    assert body["kind_error"] == "media_send_budget_exhausted"


async def test_delegated_resident_is_unmetered(wired):
    # Terra diff r1: a delegated RESIDENT's grants are the operator's own
    # role.yaml — depth alone must not classify it as specialist context.
    ob, ch = wired
    token = agent_mod.origin_var.set(
        _delegated_origin(_delegation_kind="resident",
                          _delegation_id="d3" * 16))
    try:
        for i in range(tools._SPECIALIST_MEDIA_SEND_BUDGET + 2):
            body = await _send(ob, f"f{i}.pdf")
            assert body["status"] == "ok"
    finally:
        agent_mod.origin_var.reset(token)


async def test_delegated_unknown_kind_stays_metered(wired):
    # Only a POSITIVE resident kind exempts; absent/unknown fails closed.
    ob, ch = wired
    token = agent_mod.origin_var.set(
        _delegated_origin(_delegation_kind="", _delegation_id="d4" * 16))
    try:
        for i in range(tools._SPECIALIST_MEDIA_SEND_BUDGET):
            body = await _send(ob, f"f{i}.pdf")
            assert body["status"] == "ok"
        body = await _send(ob, "over.pdf")
    finally:
        agent_mod.origin_var.reset(token)
    assert body["kind_error"] == "media_send_budget_exhausted"


async def test_resident_turn_is_unmetered(wired):
    ob, ch = wired
    token = agent_mod.origin_var.set({"role": "assistant",
                                      "channel": "telegram",
                                      "chat_id": 1197017861})
    try:
        for i in range(tools._SPECIALIST_MEDIA_SEND_BUDGET + 2):
            body = await _send(ob, f"f{i}.pdf")
            assert body["status"] == "ok"
    finally:
        agent_mod.origin_var.reset(token)


async def test_executor_engagement_is_unmetered(wired):
    ob, ch = wired
    eng = MagicMock()
    eng.kind = "executor"
    eng.id = "exec1"
    eng.status = "active"
    eng.allocated_uid = None
    eng.origin = {"role": "assistant", "channel": "telegram",
                  "chat_id": 1197017861}
    eng.context_rebuild_pending = False
    tok = tools.engagement_var.set(eng)
    try:
        for i in range(tools._SPECIALIST_MEDIA_SEND_BUDGET + 2):
            body = await _send(ob, f"f{i}.pdf")
            assert body["status"] == "ok"
    finally:
        tools.engagement_var.reset(tok)
