"""Plan round 1 findings on R1 (#1038), each pinned before it was fixed:

* an authorization challenge raised under a bound ENGAGEMENT with no ambient turn
  resolves the engagement's scope, so the persisted note reaches its body (Astra S1);
* on the failed-head path nothing of the reply is lost and a page's link destination
  survives (Astra S1 ×2 — originally pinned on the prefixed units; restated in plan
  round 3 for the line unit that replaced them);
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
from test_telegram_topic_stream import _mk_channel_with_fake_bot
from text_util import utf16_len

pytestmark = [pytest.mark.unit]

INVOICE = ("/data/agent-inbox/assistant/ready/1758500000000-a1b2.pdf", "invoice.pdf")
ANSWERED = "Casa: Test answered without opening “invoice.pdf” in this turn."
WROTE = "Casa: Test wrote this without opening “invoice.pdf”."
LINE = "Casa: Ellen answered without opening “invoice.pdf” in this turn."


def _admitted(text: str, *lines: str) -> Admitted:
    body = ("\n\n".join(lines) + "\n\n" + text) if lines else text
    return Admitted(body, scope_id="t1", kind=K.FINAL_REPLY, annotations=tuple(lines))


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
# The failed-head overflow: nothing lost, links intact
# ---------------------------------------------------------------------------

def _long_rich(n: int = 400) -> str:
    return "\n\n".join(f"**para {i}** the invoice is €412 due Friday" for i in range(n))


async def test_the_line_unit_precedes_the_pages_and_nothing_is_lost_after_a_failed_head():
    from telegram.error import TelegramError

    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    on_token = ch.create_on_token(ctx)
    await on_token("I can help.")
    bot.send_message.reset_mock()
    bot.edit_message_text = AsyncMock(side_effect=TelegramError("boom"))
    await ch.finalize_response_stream(_admitted(_long_rich(), LINE), ctx, on_token)
    texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert texts[0] == LINE
    assert all(utf16_len(t) <= 4096 for t in texts), [utf16_len(t) for t in texts]
    assert not any(t.startswith("Casa:") for t in texts[1:])
    # page 1 was the failed EDIT; the overflow carries pages 2+ — the last
    # paragraph is there, and the pages cover the tail without a gap
    joined = "\n".join(texts[1:])
    assert "para 399 " in joined
    seen = {int(w) for w in __import__("re").findall(r"para (\d+) ", joined)}
    assert seen == set(range(min(seen), 400)), sorted(set(range(min(seen), 400)) - seen)[:5]


async def test_the_line_unit_precedes_the_plain_chunks_and_nothing_is_lost():
    from telegram.error import TelegramError

    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    on_token = ch.create_on_token(ctx)
    await on_token("I can help.")
    bot.send_message.reset_mock()
    bot.edit_message_text = AsyncMock(side_effect=TelegramError("boom"))
    plain = "\n\n".join(f"para {i} the invoice is €412" for i in range(400))
    await ch.finalize_stream(_admitted(plain, LINE), ctx, on_token)
    texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert texts[0] == LINE
    assert all(utf16_len(t) <= 4096 for t in texts) and not any(t.startswith("Casa:") for t in texts[1:])
    assert "para 399 " in "\n".join(texts[1:])


async def test_a_link_destination_survives_a_failed_head():
    from telegram.error import TelegramError

    ch, bot = _mk_channel_with_fake_bot()
    ctx = {"chat_id": "42"}
    on_token = ch.create_on_token(ctx)
    await on_token("I can help.")
    bot.send_message.reset_mock()
    bot.edit_message_text = AsyncMock(side_effect=TelegramError("boom"))
    text = _long_rich(300) + "\n\n[Pay invoice](https://example.com/pay/invoice)"
    await ch.finalize_response_stream(_admitted(text, LINE), ctx, on_token)
    calls = bot.send_message.await_args_list
    assert calls[0].kwargs["text"] == LINE
    # the page goes out rich, untouched: the destination is on its entity
    urls = [getattr(e, "url", None) for c in calls[1:] for e in (c.kwargs.get("entities") or [])]
    assert "https://example.com/pay/invoice" in urls, urls


# ---------------------------------------------------------------------------
# The result contract on the scheduled arm and on media
# ---------------------------------------------------------------------------

class _Channel:
    name = "telegram"

    def __init__(self):
        self.posted = []

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options, short_labels=False):
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
        rid="rid-1", scope="dm:500", body=body, options=["a", "b"], chat_id=500,
        operator_id=7, target_role="assistant", timeout_s=60.0)
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
