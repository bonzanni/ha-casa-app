"""R2 of the output boundary (#1038 §8, INV-OUT-006): the three per-turn output
decisions that lived as inline checks — no streaming for scheduled and
event-wake turns (#534), closing silence (`<silent/>`), the webhook `send_message`
destination binding — now belong to the turn's scope: streaming and destination
are obligations registered at mint from the same facts the checks read, closing
silence is judged inside final-reply admission on the unannotated text, and the
checks are gone. No operator-visible
change: the existing pins (TestScheduledSilence, TestIssue666SentinelHold,
test_send_message_operator_binding) keep their assertions.
"""
from __future__ import annotations

import pytest

import output_boundary as ob
from bus import BusMessage, MessageType
from output_boundary import IntentKind as K
from output_boundary_testing import scope as _scope

pytestmark = [pytest.mark.unit]


def _config():
    from types import SimpleNamespace
    return SimpleNamespace(role="assistant", character=SimpleNamespace(name="Ellen"))


def _msg(type_, channel="telegram", **ctx):
    return BusMessage(type=type_, source="x", target="assistant", content="hi",
                      channel=channel, context={"chat_id": "1", "cid": "c", **ctx})


# ---------------------------------------------------------------------------
# NoStream
# ---------------------------------------------------------------------------

def test_a_scheduled_turn_does_not_stream():
    s = ob.TurnScope.mint(_msg(MessageType.SCHEDULED), _config())
    assert s.streaming_allowed is False
    assert any(isinstance(o, ob.NoStream) for o in s.obligations)


def test_an_event_wake_does_not_stream():
    s = ob.TurnScope.mint(_msg(MessageType.CHANNEL_IN, synthetic="event_wake"), _config())
    assert s.streaming_allowed is False


def test_an_ordinary_turn_streams():
    assert ob.TurnScope.mint(_msg(MessageType.REQUEST), _config()).streaming_allowed is True
    assert ob.TurnScope.mint(_msg(MessageType.CHANNEL_IN, synthetic="button"), _config()).streaming_allowed is True


# ---------------------------------------------------------------------------
# SilenceIsNoop — the final gate lives in admit; the recant contract stands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["<silent/>", "  <silent/>\n", "<silent/><silent/>", "", "   "])
def test_a_final_reply_that_strips_to_silence_is_suppressed(text):
    s = _scope()
    out = s.admit(K.FINAL_REPLY, text)
    assert out.suppressed is True
    assert out == ""


def test_prose_after_a_sentinel_is_delivered_whole():
    s = _scope()
    out = s.admit(K.FINAL_REPLY, "<silent/> actually, one thing: the bins.")
    assert out.suppressed is False
    assert out == "<silent/> actually, one thing: the bins."


def test_silence_is_tested_before_annotation_so_a_silent_file_turn_stays_silent():
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(("/ready/x.pdf", "x.pdf"),)))
    out = s.admit(K.FINAL_REPLY, "<silent/>")
    assert out.suppressed is True and out == ""


def test_the_shared_predicates_are_the_ones_agent_uses():
    import agent
    assert agent._strips_to_silence is ob.strips_to_silence
    assert agent._may_still_be_silence is ob.may_still_be_silence


# ---------------------------------------------------------------------------
# DestinationOperatorOnly
# ---------------------------------------------------------------------------

def test_an_untrusted_webhook_turn_resolves_every_channel_to_telegram():
    s = ob.TurnScope.mint(_msg(MessageType.SCHEDULED, channel="webhook",
                               _origin_route="webhook_trigger"), _config())
    assert s.resolve_channel("voice") == "telegram"
    assert s.resolve_channel("telegram") == "telegram"


def test_a_webhook_turn_with_no_route_binds_too():
    s = ob.TurnScope.mint(_msg(MessageType.SCHEDULED, channel="webhook"), _config())
    assert s.resolve_channel("voice") == "telegram"


def test_an_invoke_turn_honours_the_requested_channel():
    s = ob.TurnScope.mint(_msg(MessageType.REQUEST, channel="webhook",
                               _origin_route="invoke"), _config())
    assert s.resolve_channel("voice") == "voice"


def test_a_telegram_turn_is_unaffected():
    s = ob.TurnScope.mint(_msg(MessageType.CHANNEL_IN, _origin_route="telegram"), _config())
    assert s.resolve_channel("voice") == "voice"


def test_the_inline_checks_are_gone():
    """The relocation deletes the three inline decisions; what remains reads
    the scope. A grep, because the behaviour tests above cannot tell a
    relocated check from a duplicated one."""
    import inspect
    import agent
    import tools
    hm = inspect.getsource(agent.Agent.handle_message)
    assert 'msg.type != MessageType.SCHEDULED' not in hm
    assert '.get("synthetic") != "event_wake"' not in hm
    assert "if text and _strips_to_silence(text):" not in hm
    sm = inspect.getsource(tools.send_message.handler)
    assert '_origin_route") != "invoke"' not in sm
