"""S6 §2.2 — a file-started desk turn speaks of the file through ONE emitter; the three S4
lines this slice rewords; the widened delegability check (INV-FILE-001)."""
from __future__ import annotations

import inspect
import re

import pytest

import result_broker as rb
import specialist_desk as sd
import tools as tools_mod
from test_desk_turn import LABEL, OPERATOR, _reply, env  # noqa: F401 — the real desk harness

FILE = "invoice.pdf"


def _echo():
    return sd.prompt_prefix(OPERATOR)


async def test_a_file_turn_that_answers_echoes_the_receipt_before_the_answer(env):
    await _reply(env, text="[casa file] The operator sent you a file: invoice.pdf", file_name=FILE)
    assert len(env.channel.replies) == 1
    echo = _echo()                                                  # read-and-clear: read once
    assert f"{LABEL} received your file {FILE}; answered (1 page)." in echo
    assert "your reply" not in echo


async def test_a_file_turn_whose_plugin_posted_silently_still_echoes_the_receipt_once(env):
    async def respond(call):
        rb.POSTS.record(call.turn_id, rb.PostEvent("c", "probe", "report", LABEL, 2, None))
        return tools_mod.DelegatedOutput(text="<silent/>")
    env.respond = respond
    await _reply(env, file_name=FILE)
    assert env.channel.notices == []
    echo = _echo()                                                  # read-and-clear: read once
    assert echo.count(f"received your file {FILE}") == 1
    assert "posted to your chat (2 pages)" in echo


async def test_a_file_turn_with_nothing_to_say_and_a_failing_turn_name_the_file(env):
    async def respond(call):
        return tools_mod.DelegatedOutput(text="")
    env.respond = respond
    await _reply(env, file_name=FILE)
    assert env.channel.notices == [(OPERATOR, f"{LABEL} received your file {FILE}; had nothing to add.")]

    async def raising(call):
        raise RuntimeError("boom")
    env.respond = raising
    env.channel.notices.clear()
    await _reply(env, file_name=FILE)
    assert env.channel.notices == [(OPERATOR, f"{LABEL} could not handle your file {FILE} (unknown).")]   # the classifier's kind, as for a reply


async def test_a_reply_turn_keeps_its_reply_wording(env):
    await _reply(env)
    echo = _echo()
    assert f"{LABEL} answered your reply (1 page)." in echo and "received your file" not in echo


async def test_an_unconfirmed_delivery_is_told_as_unconfirmed_for_every_turn(env):
    env.channel.fail_send = True
    await _reply(env)
    assert env.channel.notices == [(OPERATOR, f"{LABEL} answered; complete delivery could not be confirmed.")]
    assert "did not go out" not in _echo()


async def test_a_refused_permit_is_a_past_event_and_opens_no_exchange(env):
    env.limiter.refuse = True
    await _reply(env, file_name=FILE)
    assert env.calls == [] and env.desk.log == []
    assert env.channel.notices == [(OPERATOR, f"{LABEL} was at its concurrent-work limit when the turn was attempted.")]
    assert _echo().count("concurrent-work limit") == 1


async def test_a_delegate_removed_but_still_loaded_is_refused_under_the_lock(env, monkeypatch):
    tools_mod._agent_role_map["assistant"].delegates = ()          # removed from the delegates, still loaded
    await _reply(env, file_name=FILE)
    assert env.calls == []
    assert env.channel.notices == [(OPERATOR, f"{LABEL} could not continue (not delegable).")]


def test_handle_reply_records_only_through_its_one_emitter():
    src = inspect.getsource(sd.handle_reply)
    body = src.split("def _tell", 1)
    assert len(body) == 2, "handle_reply must define its one emitter _tell"
    emitter, rest = body[1].split("\n\n", 1)                        # the emitter's own body, then the rest
    assert "record_echo(" in emitter and "deliver_desk_notice(" in emitter
    assert "record_echo(" not in rest and "deliver_desk_notice(" not in rest
    assert "_notice(" not in rest or "def _notice" not in rest        # no second notice helper
