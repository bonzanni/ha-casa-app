"""Plan round 1 findings on R1 (#1038), each pinned before it was fixed:

* an authorization challenge raised under a bound ENGAGEMENT with no ambient turn
  resolves the engagement's scope, so the persisted note reaches its body (Astra S1);
* the scheduled `ask_user` arm and `send_media` report the line they added, as the
  documented result contract promises (Astra S2);
* the LIVE completion callback carries the launch note on the notification it posts
  — driven for real, not through a no-op (Astra S2).
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import agent as agent_mod
import output_boundary as ob
import tools
from channels import DeliveryOutcome
from output_boundary import Admitted, IntentKind as K
from output_boundary_testing import scope as _scope

pytestmark = [pytest.mark.unit]

INVOICE = ("/data/agent-inbox/assistant/ready/1758500000000-a1b2.pdf", "invoice.pdf")
ANSWERED = "(Test hasn't opened “invoice.pdf”.)"
WROTE = "(Test wrote this without opening “invoice.pdf”.)"


# ---------------------------------------------------------------------------
# The authz challenge under a bound engagement
# ---------------------------------------------------------------------------

async def test_a_challenge_under_a_bound_engagement_carries_the_engagements_note(monkeypatch):
    from test_authz_grants import _create, _fresh_env, _settle
    broker, coord, channel = _fresh_env(monkeypatch)
    eng = SimpleNamespace(id="eng-9", status="active",
                          origin={"role": "assistant", "channel": "telegram", "chat_id": 100,
                                  "_inherited_note": WROTE})
    eng_token = tools.engagement_var.set(eng)
    origin_token = agent_mod.origin_var.set(None)          # no ambient turn at all
    try:
        _create(coord, channel, canonical_json='{"amount":412,"id":"INV-1"}', engagement_id="eng-9")
        await _settle()
    finally:
        agent_mod.origin_var.reset(origin_token)
        tools.engagement_var.reset(eng_token)
    assert channel.posts, "the challenge was not posted"
    body = channel.posts[0][2]
    assert isinstance(body, Admitted)
    assert body.startswith(WROTE) and "INV-1" in body


# ---------------------------------------------------------------------------
# The result contract on the scheduled arm and on media
# ---------------------------------------------------------------------------

class _Channel:
    name = "telegram"

    def __init__(self):
        self.posted = []

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options, short_labels=False,
                              shorts=None):
        self.posted.append(text)
        return 77

    async def edit_dm_message(self, *a, **k):
        return True


async def test_the_scheduled_ask_result_names_the_line(monkeypatch):
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    import scheduled_asks
    monkeypatch.setattr(scheduled_asks, "STORE", None)
    ch = _Channel()
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    body = s.admit(K.KEYBOARD, "Which one?\n\n1. a\n2. b")
    res = await tools._ask_user_scheduled(
        channel=ch, origin={"chat_id": "date-reminder-1", "_scheduled_epoch": "e1"},
        rid="rid-1", scope="dm:111:500", body=body, options=["a", "b"], chat_id=500,
        operator_id=7, bot_id=111, target_role="assistant", timeout_s=60.0)
    payload = json.loads(res["content"][0]["text"])
    assert payload["status"] == "awaiting_user", payload
    assert payload["casa_prefixed"] == [ANSWERED]
    assert ch.posted[0].startswith(ANSWERED)


async def test_the_media_result_names_the_line(tmp_path):
    import plugin_outbox
    ob_ = plugin_outbox.init_outbox(str(tmp_path / "plugin-outbox"))
    ch = MagicMock()
    ch.send_media = AsyncMock()
    cm = MagicMock()
    cm.get.return_value = ch
    tools.init_tools(channel_manager=cm, bus=MagicMock(), specialist_registry=MagicMock(),
                     mcp_registry=MagicMock(), trigger_registry=MagicMock(),
                     engagement_registry=MagicMock())
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    path = os.path.join(ob_._root_realpath, "invoice.pdf")
    with open(path, "wb") as fh:
        fh.write(b"%PDF-1.4\n" + b"x" * 64)
    token = agent_mod.origin_var.set({"role": "assistant", "channel": "telegram",
                                      "chat_id": 1197017861, "turn_scope": s})
    try:
        res = await tools.send_media.handler({"path": path, "kind": "document", "caption": "Here"})
    finally:
        agent_mod.origin_var.reset(token)
        ob_.close()
        plugin_outbox._OUTBOX = None
    payload = json.loads(res["content"][0]["text"])
    assert payload["status"] == "ok", payload
    assert payload["casa_prefixed"] == [ANSWERED]


# ---------------------------------------------------------------------------
# The live completion, driven for real
# ---------------------------------------------------------------------------

async def test_the_live_completion_notification_carries_the_launch_note(tmp_path, monkeypatch):
    from specialist_registry import SpecialistRegistry
    from test_delegate_to_agent import _caller_cfg, _specialist_cfg
    resident_cfg = _specialist_cfg(role="butler")
    reg = SpecialistRegistry(str(tmp_path / "specs"), tombstone_path=str(tmp_path / "tombs.json"))
    bus = MagicMock()
    bus.notify = AsyncMock()
    tools.init_tools(channel_manager=None, bus=bus, specialist_registry=reg, mcp_registry=None,
                     agent_role_map={"butler": resident_cfg,
                                     "assistant": _caller_cfg(delegates=("butler",))})

    async def _fake_bounded(cfg, task_text, context_text, resolution=None, output_format=None):
        return tools.DelegatedOutput(text="the invoice is €412")

    monkeypatch.setattr(tools, "_run_delegated_agent_bounded", _fake_bounded)
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    token = agent_mod.origin_var.set({"role": "assistant", "channel": "telegram", "chat_id": "1",
                                      "user_id": 1, "cid": "A", "user_text": "check", "turn_scope": s})
    try:
        res = await tools.delegate_to_agent.handler(
            {"agent": "butler", "task": "check the €412 invoice", "context": "", "mode": "async"})
        for _ in range(200):                   # the callback settles durably (a thread) then announces
            if bus.notify.await_count:
                break
            await asyncio.sleep(0.01)
    finally:
        agent_mod.origin_var.reset(token)
    assert json.loads(res["content"][0]["text"])["status"] == "pending"
    assert bus.notify.await_count == 1, bus.notify.await_args_list
    msg = bus.notify.await_args.args[0]
    assert msg.content.origin["_inherited_note"] == WROTE
