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
from test_agent_process import FakeClient, _StubChannel, _make_agent, _msg, _request_msg

pytestmark = [pytest.mark.unit]

INVOICE = ("/data/agent-inbox/assistant/ready/1758500000000-a1b2.pdf", "invoice.pdf")
ANSWERED = "Casa: Test answered without opening “invoice.pdf” in this turn."


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
    delivered = stub.finalize_response_stream.await_args.args[0]
    assert isinstance(delivered, Admitted)
    assert delivered == ANSWERED + "\n\nIt's €412"
    assert delivered.annotations == (ANSWERED,)


async def test_a_turn_owing_nothing_delivers_the_text_unchanged_but_admitted(tmp_path):
    agent = _make_agent(tmp_path, role="assistant")
    stub = _StubChannel()
    agent._channel_manager.register(stub)
    with patch.object(agent, "_process", AsyncMock(return_value="pong")):
        await agent.handle_message(_request_msg())
    delivered = stub.finalize_response_stream.await_args.args[0]
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
    delivered = stub.finalize_stream.await_args.args[0]
    assert isinstance(delivered, Admitted)
    assert delivered.source == "casa"
    assert delivered.annotations == ()
    assert "Casa: Test answered" not in delivered


async def test_the_health_notice_is_prepended_outside_the_disclosure(tmp_path):
    agent = _make_agent(tmp_path, role="assistant")
    stub = _StubChannel()
    agent._channel_manager.register(stub)

    async def _notice(text):
        return f"NOTICE\n\n{text}", "NOTICE"

    with patch.object(agent, "_process", AsyncMock(side_effect=_arming_process("It's €412"))), \
            patch.object(agent, "_maybe_prepend_health_notice", _notice):
        await agent.handle_message(_request_msg())
    delivered = stub.finalize_response_stream.await_args.args[0]
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
    assert stub.finalize_response_stream.await_count == 0
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
