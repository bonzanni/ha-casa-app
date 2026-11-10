"""Slice 5f: the `url` button kind — ``{"label": ..., "url": "https://..."}`` opens its link and
stores no call. Accepted only under the operator_link rule and only from a plugin that already
declares an ``operator_link`` slot, at most one per card; the keyboard carries it as a Telegram
link button, and a callback that names its index anyway is void."""
from __future__ import annotations

import json
import types

import pytest

import result_broker as rb
from plugin_grants import PluginContract, ResultContractMap
from test_proposal_slot import ARTIFACT, SEG, SRV, _identity
from test_proposal_slot import _tool as _slot_tool
from test_proposal_tap import OPERATOR, RID, _cq, _settle, _tap, env as tap_env  # noqa: F401
from test_tap_keep_card import APPLY, CONFIRM, labels  # noqa: F401
from two_endpoints import CASA_BOT as BOT

LINK = f"mcp__plugin_{SEG}_{SRV}__link"
URL = "https://my.home-assistant.io/redirect/supervisor_app/?app=abc_casa_browser"
OPEN = {"label": "Open live view", "url": URL}

#: an endpoint with a bot id and none of the proposal hooks
_NO_HOOKS = types.SimpleNamespace(bot_id=BOT)


def _call(with_link: bool = True):
    tools = {APPLY: _slot_tool("apply")}
    if with_link:
        tools[LINK] = _slot_tool("link", "capability", ("live",), {"live": "operator_link"})
    cmap = ResultContractMap(tools=tools, plugins={SEG: PluginContract(ARTIFACT, True, frozenset(),
                                                                       name="probe")})
    entry = _slot_tool("offer", "capability", ("proposal",), {"proposal": "operator_proposal"})
    return types.SimpleNamespace(identity=_identity(), entry=entry, contract_map=cmap, protected={},
                                 tool_use_id="call-1")


def _deposit(*buttons, with_link: bool = True):
    return rb.proposal_ok(json.dumps({"text": "Pay?", "buttons": list(buttons)}),
                          _call(with_link))


def test_a_link_button_is_kept_with_its_address_and_no_call(labels):
    parsed, why = _deposit(CONFIRM, OPEN)
    assert why is None
    assert parsed["buttons"][1] == {"label": "Open live view", "url": URL}


def test_only_a_plugin_that_may_post_links_may_put_one_on_a_card(labels):
    assert _deposit(CONFIRM, OPEN, with_link=False) == (None, "bad_proposal")


@pytest.mark.parametrize("button", [
    {"label": "Open", "url": "http://example.org/"},
    {"label": "Open", "url": "homeassistant://navigate/x"},
    {"label": "Open", "url": "https:///nohost"},
    {"label": "Open", "url": "https://a.example/ x"},
    {"label": "Open", "url": 7},
    {"label": "Open", "url": URL, "call": {"tool": "apply", "arguments": {}}},
    {"label": "Open", "url": URL, "close": True},
    {"label": "Open", "url": URL, "keep_card": True},
    {"label": "", "url": URL},
])
def test_a_bad_link_button_is_a_bad_proposal(labels, button):
    assert _deposit(CONFIRM, button) == (None, "bad_proposal")


def test_a_second_link_button_is_a_bad_proposal(labels):
    assert _deposit(CONFIRM, OPEN, {"label": "Again", "url": URL}) == (None, "bad_proposal")


async def test_the_card_is_posted_with_the_link_button(labels, monkeypatch):
    import verdict_broker as vb
    monkeypatch.setattr(vb, "BROKER", vb.VerdictBroker())
    parsed, _ = _deposit(CONFIRM, OPEN)
    seen = {}

    async def fake_post(to, text, labels_, rid, *, post=None, urls=None):
        seen["meta"] = vb.BROKER.get_meta(namespace="proposal", scope=f"proposal:{to.bot_id}:{to.chat_id}",
                                         request_id=rid)
        seen["urls"] = urls
        return 501
    monkeypatch.setattr(rb, "_post_operator_proposal", fake_post)
    monkeypatch.setattr(rb, "_endpoint_for", lambda target: _NO_HOOKS)
    post = rb.PostRecord(role="finance", operator_id=42, plugin=SEG, slot="proposal",
                         tool_use_id="call-1", owner="d-1", posted_at=1.0, kind="proposal")
    delivered, *_ = await rb._post_proposal(_identity(), SEG, "proposal", _call(), parsed,
                                            "📊 Finance", post)
    assert delivered
    assert seen["urls"] == [None, URL]
    assert seen["meta"]["kinds"] == ["call", "url"] and seen["meta"]["calls"][1] is None


def test_the_keyboard_renders_a_telegram_link_button():
    from channels.telegram import _proposal_keyboard
    rows = _proposal_keyboard(["Yes", "Open live view"], RID, [None, URL]).inline_keyboard
    assert rows[0][0].callback_data == f"v1|proposal|{RID}|0" and rows[0][0].url is None
    assert rows[1][0].url == URL and rows[1][0].callback_data is None
    plain = _proposal_keyboard(["Yes"], RID).inline_keyboard
    assert plain[0][0].callback_data == f"v1|proposal|{RID}|0"


async def test_a_callback_naming_the_link_button_is_void(tap_env):
    tap_env.register(meta={"options": ["Confirm", "Open"], "kinds": ["call", "url"],
                           "urls": [None, URL],
                           "calls": [{"server": "api", "wire_name": "apply", "runtime_name": APPLY,
                                      "proposal": False, "arguments": {"pid": 7},
                                      "canonical": '{"pid":7}'}, None]})
    await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1"))
    await _settle()
    assert tap_env.taps == [] and tap_env.bot.sent == [] and tap_env.bot.markups == []
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|0")) != "already answered"
