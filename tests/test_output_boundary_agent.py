"""The agent seam of the output boundary (#1038 §2, §3.1, §6.6):
``handle_message`` mints one ``TurnScope`` per turn, the final reply and every
streamed cumulative pass through it, the scope rides the origin snapshot into
the SDK turn, and the evidence matchers are part of the resident's hook bundle.

Harness: the same ``_make_agent`` / ``_StubChannel`` / ``FakeClient`` doubles
tests/test_agent_process.py drives ``handle_message`` and ``_process`` with.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import agent as agent_mod
import output_boundary as ob
from bus import BusMessage, MessageType
from output_boundary import Admitted, IntentKind as K
from output_boundary import (REPLY_MARKER_GUIDE, ReplyMark, may_be_reply_marker,
                             split_reply_marker)
from output_boundary_testing import scope as _scope
from test_agent_process import FakeClient, _StubChannel, _make_agent, _msg, _request_msg

pytestmark = [pytest.mark.unit]

INVOICE = ("/data/agent-inbox/assistant/ready/1758500000000-a1b2.pdf", "invoice.pdf")
ANSWERED = "(Test hasn't opened “invoice.pdf”.)"


def _arming_process(reply: str):
    """A ``_process`` double that behaves like a turn which listed a file: it
    finds the scope the agent minted and arms the obligation on it."""
    async def _fake(msg, **_kw):
        scope = msg.context["_turn_scope"]
        assert isinstance(scope, ob.TurnScope)
        scope.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
        return reply
    return _fake


async def test_the_final_reply_of_a_file_turn_reaches_the_channel_admitted_and_annotated(tmp_path):
    agent = _make_agent(tmp_path, role="assistant")
    stub = _StubChannel()
    agent._channel_manager.register(stub)
    with patch.object(agent, "_process", AsyncMock(side_effect=_arming_process("It's €412"))):
        await agent.handle_message(_request_msg("what's the total?"))
    delivered = stub.send_response.await_args.args[0]
    assert isinstance(delivered, Admitted)
    assert delivered == ANSWERED + "\n\nIt's €412"
    assert delivered.annotations == (ANSWERED,)


async def test_a_turn_owing_nothing_delivers_the_text_unchanged_but_admitted(tmp_path):
    agent = _make_agent(tmp_path, role="assistant")
    stub = _StubChannel()
    agent._channel_manager.register(stub)
    with patch.object(agent, "_process", AsyncMock(return_value="pong")):
        await agent.handle_message(_request_msg())
    delivered = stub.send_response.await_args.args[0]
    assert isinstance(delivered, Admitted)
    assert delivered == "pong"
    assert delivered.annotations == ()
    assert delivered.source == "model"


async def test_a_classified_error_reply_is_casa_text_never_annotated(tmp_path):
    agent = _make_agent(tmp_path, role="assistant")
    stub = _StubChannel()
    agent._channel_manager.register(stub)

    async def _boom(msg, **_kw):
        msg.context["_turn_scope"].arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
        raise TimeoutError("sdk")

    with patch.object(agent, "_process", AsyncMock(side_effect=_boom)):
        await agent.handle_message(_request_msg())
    delivered = stub.send.await_args.args[0]
    assert isinstance(delivered, Admitted)
    assert delivered.source == "casa"
    assert delivered.annotations == ()
    assert "Test hasn't opened" not in delivered


async def test_the_health_notice_is_prepended_outside_the_disclosure(tmp_path):
    agent = _make_agent(tmp_path, role="assistant")
    stub = _StubChannel()
    agent._channel_manager.register(stub)

    async def _notice(text):
        return f"NOTICE\n\n{text}", "NOTICE"

    with patch.object(agent, "_process", AsyncMock(side_effect=_arming_process("It's €412"))), \
            patch.object(agent, "_maybe_prepend_health_notice", _notice):
        await agent.handle_message(_request_msg())
    delivered = stub.send_response.await_args.args[0]
    assert delivered == "NOTICE\n\n" + ANSWERED + "\n\nIt's €412"
    assert isinstance(delivered, Admitted)
    assert delivered.annotations == (ANSWERED,)


async def test_a_silent_file_turn_stays_silent(tmp_path):
    agent = _make_agent(tmp_path, role="assistant")
    stub = _StubChannel()
    stub.turn_finished = AsyncMock()
    agent._channel_manager.register(stub)
    with patch.object(agent, "_process", AsyncMock(side_effect=_arming_process("<silent/>"))):
        await agent.handle_message(_request_msg())
    assert stub.send_response.await_count == 0
    assert stub.turn_finished.await_count == 1


async def test_the_scope_rides_the_origin_snapshot_into_the_sdk_turn(tmp_path):
    captured: dict[str, Any] = {}

    class CapturingClient(FakeClient):
        async def receive_response(self):
            captured["origin"] = agent_mod.origin_var.get(None)
            async for m in super().receive_response():
                yield m

    FakeClient.reset()
    a = _make_agent(tmp_path, role="assistant")
    msg = _msg("telegram", "777", "hello")
    with patch("sdk_client_pool._default_make_client", CapturingClient):
        await a._process(msg)
    scope = captured["origin"]["turn_scope"]
    assert isinstance(scope, ob.TurnScope)
    assert scope.id == msg.id
    assert scope.display_name == "Test"


async def test_streamed_cumulatives_carry_the_line_while_undischarged(tmp_path):
    FakeClient.reset()
    FakeClient.response_text = "It's €412"
    a = _make_agent(tmp_path, role="assistant")
    msg = _msg("telegram", "777", "what's the total?")
    scope = ob.TurnScope.mint(msg, a.config)
    scope.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    msg.context["_turn_scope"] = scope
    seen: list[str] = []

    async def on_token(cum: str) -> None:
        seen.append(cum)

    with patch("sdk_client_pool._default_make_client", FakeClient):
        out = await a._process(msg, on_token=on_token)
    assert seen == [ANSWERED + "\n\nIt's €412"]
    assert out == "It's €412"          # the model's own text; admission is at delivery


async def test_the_read_evidence_matchers_are_in_the_resident_hook_bundle(tmp_path):
    a = _make_agent(tmp_path, role="assistant")
    opts = await a._build_options(channel="telegram", channel_key="k", is_fresh=True,
                                  resume_sid=None, user_text="hi")
    for event in ("PostToolUse", "PostToolUseFailure"):
        assert any(m.matcher == "Read" for m in opts.hooks.get(event, [])), event


# ---------------------------------------------------------------------------
# The agent's <reply/> marker: admission splits it off the answer
# ---------------------------------------------------------------------------

def test_a_leading_marker_is_split_off():
    assert split_reply_marker('<reply quote="the Q3 part"/>\nDone.') == (ReplyMark("the Q3 part"), "Done.")
    assert split_reply_marker("<reply/> Yes.") == (ReplyMark(None), "Yes.")
    assert split_reply_marker("<reply />Yes.") == (ReplyMark(None), "Yes.")
    assert split_reply_marker("  \n<reply/>\n\nYes.") == (ReplyMark(None), "\nYes.")
    assert split_reply_marker('<reply quote="a "b" c"/>Yes.') == (ReplyMark('a "b" c'), "Yes.")
    assert split_reply_marker("I said <reply/> here") == (None, "I said <reply/> here")
    assert split_reply_marker('<reply quote="open') == (None, '<reply quote="open')
    assert split_reply_marker("<reply>Yes.") == (None, "<reply>Yes.")
    assert split_reply_marker("") == (None, "")


def test_a_marker_still_being_written_is_recognised():
    for text in ("<", "<rep", "  <reply", "<reply ", '<reply quote="Mon', '<reply quote="Mon"/'):
        assert may_be_reply_marker(text), text
    for text in ("", "Sure", "<b>", "<reply>", "<reply/>", '<reply quote="Mon"/>Su', "x <reply"):
        assert not may_be_reply_marker(text), text


def test_a_final_reply_loses_its_marker_and_keeps_it_as_reply():
    a = _scope().admit(K.FINAL_REPLY, '<reply quote="x"/>Hi')
    assert str(a) == "Hi" and a.reply == ReplyMark("x")
    assert a.with_text("N\n\nHi").reply == ReplyMark("x")
    assert _scope().admit(K.FINAL_REPLY, "Hi").reply is None


def test_a_marker_then_silence_is_chosen_silence():
    a = _scope().admit(K.FINAL_REPLY, "<reply/><silent/>")
    assert a.suppressed and a.chosen_silence and a.reply is None
    b = _scope().admit(K.FINAL_REPLY, "<reply/>")
    assert b.suppressed and not b.chosen_silence and b.reply is None


def test_only_an_answer_or_its_stream_loses_the_marker():
    assert _scope().admit(K.DISCRETE, "<reply/>Hi") == "<reply/>Hi"
    assert _scope().admit(K.STREAM_UPDATE, "<reply/>Hi") == "Hi"


def test_the_disclosure_goes_above_the_words_not_above_the_marker():
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    a = s.admit(K.FINAL_REPLY, '<reply quote="x"/>Hi')
    assert a == ANSWERED + "\n\nHi"
    assert a.reply == ReplyMark("x")


async def test_a_half_written_marker_never_reaches_the_draft(tmp_path, monkeypatch):
    from test_agent_partial_streaming import (QueuedScriptFactory, _mk_assistant,
                                              _mk_stream_event)
    cumulatives = ["<rep", '<reply quote="Mon', '<reply quote="Mon"/>Su',
                   '<reply quote="Mon"/>Sure']
    # Admission of each in-flight cumulative: nothing until the marker closes,
    # then only the words.
    s = _scope()
    assert [s.admit(K.STREAM_UPDATE, c) for c in cumulatives] == ["", "", "Su", "Sure"]
    # Through the real stream: the draft is handed the words only.
    deltas = ["<rep", 'ly quote="Mon', '"/>Su', "re"]
    monkeypatch.setattr("sdk_client_pool._default_make_client", QueuedScriptFactory([[
        *(_mk_stream_event(d) for d in deltas), _mk_assistant(cumulatives[-1])]]))
    a = _make_agent(tmp_path, role="assistant")
    msg = _msg("telegram", "777", "Mon or Tue?")
    msg.context["_turn_scope"] = ob.TurnScope.mint(msg, a.config)
    drafts: list[str] = []

    async def on_token(cum: str) -> None:
        drafts.append(cum)

    try:
        out = await a._process(msg, on_token=on_token)
    finally:
        await a.aclose()
    # The empty draft before "Su" is the stream's own, shown at its start.
    assert drafts == ["Su", "Sure"]
    assert out == cumulatives[-1]          # the model's own text; admission is at delivery


def test_the_telegram_channel_context_teaches_the_marker():
    assert REPLY_MARKER_GUIDE in agent_mod._channel_context_block("telegram")
    assert agent_mod._channel_context_block("telegram").endswith(
        "\n" + REPLY_MARKER_GUIDE + "\n</channel_context>")
    assert REPLY_MARKER_GUIDE not in agent_mod._channel_context_block("voice")


def test_the_marker_guide_is_the_fixed_copy():
    assert REPLY_MARKER_GUIDE == (
        'reply_to: your answer goes out as an ordinary message. When it answers one part of a '
        'longer message, or a message the person sent a while ago, start it with <reply/> to '
        'send it as a reply to their message, or with <reply quote="..."/> holding their exact '
        'words for the part you answer. In an ordinary back-and-forth write no marker.')
