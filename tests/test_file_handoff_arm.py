"""S6 §2.4 — the `arm_file` button kind: deposited without a call, carried as a parallel kind
in the metadata, admitted by the options count, its tap writes the arming at commit before any
await, and its finish hook edits the keyboard and sends NOTHING (operator ruling R4-1;
INV-FILE-003)."""
from __future__ import annotations

import asyncio
import json
import time
import types

import pytest

import result_broker as rb
from test_proposal_slot import SLOT, _open, _proposal, env as slot_env  # noqa: F401
from test_proposal_tap import OPERATOR, RID, _cq, _settle, _tap, env as tap_env  # noqa: F401

ARM = {"label": "📎 Add a document", "arm_file": True}
CALL = {"label": "Yes", "call": {"tool": "apply", "arguments": {"choice": "yes", "match_id": 17}}}


def test_an_arm_file_button_is_deposited_without_a_call_and_kept_as_its_kind(slot_env):
    _open(slot_env.store)
    ref, err = slot_env.store.deposit(client_id="c1", slot=SLOT,
                                      value=json.dumps(_proposal(buttons=[CALL, ARM])))
    assert err is None and rb.is_reference(ref)
    kept = slot_env.store._refs[ref].proposal["buttons"]
    assert kept[0]["call"]["wire_name"] == "apply" and "arm_file" not in kept[0]
    assert kept[1] == {"label": "📎 Add a document", "arm_file": True}


@pytest.mark.parametrize("buttons", [
    [{"label": "Both", "arm_file": True, "call": {"tool": "apply", "arguments": {}}}],
    [{"label": "Neither"}],
    [ARM, {"label": "📎 Another", "arm_file": True}],
    [{"label": "📎 Add a document", "arm_file": "yes"}],
])
def test_a_button_with_both_or_neither_keys_or_a_second_arm_button_is_a_bad_proposal(slot_env, buttons):
    _open(slot_env.store)
    ref, err = slot_env.store.deposit(client_id="c1", slot=SLOT, value=json.dumps(_proposal(buttons=buttons)))
    assert ref is None and err == "bad_proposal"


def _mixed_meta():
    return {"options": ["Yes", "📎 Add a document"], "kinds": ["call", "arm_file"],
            "calls": [{"server": "api", "wire_name": "apply", "runtime_name": "mcp__plugin_probe_api__apply",
                       "proposal": False, "arguments": {"choice": "yes"}, "canonical": '{"choice":"yes"}'}, None]}


async def test_an_arm_file_tap_writes_the_arming_at_commit_before_any_await(tap_env):
    tap_env.register(meta=_mixed_meta())
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1")) == "✔"
    arming = tap_env.ch._armings.get(OPERATOR)                        # before the hook ran
    assert arming is not None and arming["role"] == "finance" and arming["operator_id"] == OPERATOR
    assert time.monotonic() < arming["expires_at"] <= time.monotonic() + 600.5
    await _settle()
    assert len(tap_env.bot.edited) == 1 and "☑ 📎 Add a document" in tap_env.bot.edited[0]["text"]
    assert tap_env.taps == []                                         # nothing dispatched
    assert tap_env.bot.sent == []                                     # NO message (R4-1)


async def test_a_second_tap_on_the_arm_button_is_already_answered_and_the_arming_stands(tap_env):
    tap_env.register(meta=_mixed_meta())
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1")) == "✔"
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1")) == "already answered"
    assert OPERATOR in tap_env.ch._armings


async def test_the_call_button_of_a_mixed_proposal_still_dispatches_the_pinned_turn(tap_env):
    tap_env.register(meta=_mixed_meta())
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|0")) == "✔"
    await _settle()
    assert len(tap_env.taps) == 1 and tap_env.taps[0]["idx"] == 0
    assert OPERATOR not in tap_env.ch._armings


async def test_a_failed_keyboard_edit_on_an_arm_tap_sends_exactly_the_past_fact_line(tap_env, monkeypatch):
    tap_env.register(meta=_mixed_meta())

    async def failing_edit(*a, **k):
        return False
    monkeypatch.setattr(tap_env.ch, "edit_dm_message", failing_edit)
    notices = []

    async def notice(chat_id, text):
        notices.append(text)
        return True
    monkeypatch.setattr(tap_env.ch, "deliver_desk_notice", notice)
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|1")) == "✔"
    await _settle()
    import specialist_desk as sd
    assert notices == [f"{sd.label_for('finance')} ☑ 📎 Add a document — the buttons could not be cleared."]
    assert tap_env.taps == [] and OPERATOR in tap_env.ch._armings


async def test_an_index_beyond_the_options_is_invalid_even_with_a_none_call_slot(tap_env):
    tap_env.register(meta=_mixed_meta())
    assert await _tap(tap_env, _cq(data=f"v1|proposal|{RID}|2")) == "invalid"
