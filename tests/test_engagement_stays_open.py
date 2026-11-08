"""#1411: ``emit_completion`` says it closes the engagement's topic, and an
engagement's completion notice says the engagement has ended, so the resident
never relays a summary that calls a closed topic open.

Live (casa-test, v0.344.79) a specialist's summary said the topic "will stay
open" — which the resident relayed. These tests pin that the production paths
carry the correction.
"""
from __future__ import annotations

from unittest.mock import Mock


import agent
import tools
from bus import BusMessage, MessageType
from specialist_registry import DelegationComplete

CLOSED = ("This was an engagement, and it has ended: the person can no longer "
          "talk with the specialist in its topic.")


def test_emit_completion_says_it_closes_the_topic():
    assert "Casa closes its topic" in tools.emit_completion.description


def _body(context: dict) -> str:
    complete = DelegationComplete(
        delegation_id="e" * 32, agent="finance", status="ok",
        text="The topic is open and will stay open.",
        origin={"role": "assistant", "user_text": "chat with Alex"})
    msg = BusMessage(type=MessageType.NOTIFICATION, source="finance",
                     target="assistant", content=complete, channel="telegram",
                     context=context)
    return agent.Agent._synthesize_delegation_turn(Mock(), msg).content


def test_an_engagement_completion_says_the_engagement_ended():
    body = _body({"cid": "c", "chat_id": "1", "engagement_id": "e" * 32})
    assert "Result text from finance:\nThe topic is open and will stay open.\n" in body
    assert CLOSED in body
    assert body.index(CLOSED) > body.index("Result text from finance:")


def test_a_delegation_completion_says_nothing_about_a_topic():
    body = _body({"cid": "c", "chat_id": "1", "delegation_id": "d" * 32})
    assert CLOSED not in body
