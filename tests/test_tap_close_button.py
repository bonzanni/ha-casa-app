"""#1375: the `close` button kind — a card's author may include a Close button,
``{"label": ..., "close": true}``, which stores no call. Its tap is admitted by the same
chain and the same single claim and commit as any settling tap; the finish hook then
removes the card's keyboard by one markup-only edit, leaving the text as posted, and runs
no desk use, no plugin call and no model turn; any later tap on the card answers "already
answered" (INV-PROP-011)."""
from __future__ import annotations

import json

import pytest

import result_broker as rb
import specialist_desk as sd
from test_proposal_tap import OPERATOR, RID, _cq, _settle, _tap, env as tap_env  # noqa: F401
from test_tap_keep_card import CONFIRM, SEE, _call, _deposit, _identity, labels  # noqa: F401
from test_tap_keep_card import APPLY, SEG

CLOSE = {"label": "Close", "close": True}


# --- the deposit -------------------------------------------------------------------

def test_a_close_button_is_deposited_without_a_call_and_kept_as_its_kind(labels):
    parsed, why = _deposit(CONFIRM, SEE, CLOSE)
    assert why is None
    assert parsed["buttons"][2] == {"label": "Close", "close": True}
    assert "close" not in parsed["buttons"][0] and "close" not in parsed["buttons"][1]


def test_a_card_of_only_call_buttons_is_deposited_as_before(labels):
    parsed, why = _deposit(CONFIRM)
    assert why is None and set(parsed["buttons"][0]) == {"label", "call"}


@pytest.mark.parametrize("button", [
    {"label": "Close", "close": "yes"},
    {"label": "Close", "close": 1},
    {"label": "Close", "close": False},
    {"label": "Close", "close": None},
    {"label": "Close", "close": True, "call": {"tool": "apply", "arguments": {}}},
    {"label": "Close", "close": True, "arm_file": True},
    {"label": "Close", "close": True, "keep_card": True},
    {"label": "Close", "arm_file": True, "close": True},
    {"label": "", "close": True},
    {"close": True},
])
def test_close_not_json_true_or_beside_another_kind_is_a_bad_proposal(labels, button):
    assert _deposit(CONFIRM, button) == (None, "bad_proposal")


def test_a_second_close_button_is_a_bad_proposal(labels):
    assert _deposit(CONFIRM, CLOSE, {"label": "Done", "close": True}) == (None, "bad_proposal")


async def test_the_registered_kinds_name_the_close_button(labels, monkeypatch):
    import verdict_broker as vb
    monkeypatch.setattr(vb, "BROKER", vb.VerdictBroker())
    parsed, _ = _deposit(CONFIRM, SEE, CLOSE)
    seen = {}

    async def fake_post(chat_id, text, labels_, rid, *, post=None):
        seen["meta"] = vb.BROKER.get_meta(namespace="proposal", scope=f"proposal:{chat_id}",
                                         request_id=rid)
        seen["labels"] = list(labels_)
        return 501
    monkeypatch.setattr(rb, "_post_operator_proposal", fake_post)
    monkeypatch.setattr(rb, "_telegram_channel", lambda: None)
    post = rb.PostRecord(role="finance", operator_id=42, plugin=SEG, slot="proposal",
                         tool_use_id="call-1", owner="d-1", posted_at=1.0, kind="proposal")
    delivered, *_ = await rb._post_proposal(_identity(), SEG, "proposal", _call(), parsed,
                                            "📊 Finance", post)
    assert delivered
    assert seen["meta"]["kinds"] == ["call", "keep_card", "close"]
    assert seen["meta"]["calls"][2] is None
    assert seen["labels"] == ["Confirm", "See PDF", "Close"]


# --- the tap -----------------------------------------------------------------------

def _close_meta():
    return {"options": ["Confirm", "Close"], "kinds": ["call", "close"],
            "calls": [{"server": "api", "wire_name": "apply", "runtime_name": APPLY,
                       "proposal": False, "arguments": {"pid": 7}, "canonical": '{"pid":7}'},
                      None]}


def _no_desk(monkeypatch):
    reserved = []
    real = sd.DeskRegistry.get_or_create

    def spy(self, *a, **k):
        reserved.append(a)
        return real(self, *a, **k)
    monkeypatch.setattr(sd.DeskRegistry, "get_or_create", spy)
    return reserved


async def test_a_close_tap_removes_the_keyboard_and_leaves_the_text(tap_env, monkeypatch):
    reserved = _no_desk(monkeypatch)
    tap_env.register(meta=_close_meta())
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1")) == "✔"
    await _settle()
    (markup,) = tap_env.bot.markups
    assert markup["chat_id"] == OPERATOR and markup["message_id"] == 501
    assert list(markup["reply_markup"].inline_keyboard) == []      # explicitly empty
    assert tap_env.bot.edited == []                                  # the text is never re-sent
    assert tap_env.taps == [] and reserved == []                     # no desk use, no pinned turn
    assert tap_env.bot.sent == []                                    # no notice
    assert OPERATOR not in tap_env.ch._armings
    assert sd.drain_echo_lines(OPERATOR) == []                     # the resident learns nothing


async def test_any_later_tap_on_a_closed_card_is_already_answered_and_runs_nothing(tap_env):
    tap_env.register(meta=_close_meta())
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1")) == "✔"
    await _settle()
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|0")) == "already answered"
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1")) == "already answered"
    await _settle()
    assert tap_env.taps == [] and len(tap_env.bot.markups) == 1 and tap_env.bot.edited == []


async def test_the_call_button_of_a_card_with_close_still_dispatches_as_before(tap_env):
    tap_env.register(meta=_close_meta())
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|0")) == "✔"
    await _settle()
    assert [t["idx"] for t in tap_env.taps] == [0]
    assert tap_env.bot.markups == []
    assert len(tap_env.bot.edited) == 1 and tap_env.bot.edited[0]["text"].endswith("\n⏳ Confirm")
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1")) == "already answered"
    await _settle()
    assert tap_env.bot.markups == [] and len(tap_env.taps) == 1


async def test_a_close_tap_still_runs_the_whole_admission_chain(tap_env):
    tap_env.register(meta=_close_meta())
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1", user_id=7)) == "not for you"
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1", message_id=999)) == "expired"
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|2")) == "invalid"
    await _settle()
    assert tap_env.bot.markups == [] and tap_env.bot.edited == [] and tap_env.taps == []
    assert tap_env.broker.pending(namespace="proposal", scope=f"proposal:{OPERATOR}") == [RID]


async def test_a_failed_keyboard_removal_sends_nothing(tap_env, monkeypatch):
    tap_env.register(meta=_close_meta())

    async def refused(**kwargs):
        from telegram.error import BadRequest
        raise BadRequest("message to edit not found")
    tap_env.bot.edit_message_reply_markup = refused
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1")) == "✔"
    await _settle()
    assert tap_env.bot.sent == [] and tap_env.bot.edited == [] and tap_env.taps == []
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|0")) == "already answered"


def test_a_close_card_needs_no_settle_reserve_beyond_todays(labels):
    """A Close tap appends no line, so the composed-size rule is unchanged."""
    text = "x" * (rb.PROPOSAL_TEXT_CHARS - 1)
    with_close = rb.proposal_ok(json.dumps({"text": text, "buttons": [CONFIRM, CLOSE]}), _call())
    without = rb.proposal_ok(json.dumps({"text": text, "buttons": [CONFIRM]}), _call())
    assert (with_close[0] is None) == (without[0] is None)
