"""#1411: an interactive engagement is a conversation the person keeps writing
in, and its completion notice says the engagement has ended.

Live (casa-test, v0.344.79) the specialist read "open a chat and wait" as a
task its first reply finished, called ``emit_completion`` at once, and its
summary said the topic "will stay open" — which the resident relayed. The
measured fix is in what the models read (``test-local/eval/
specialist_engagement_open.py``); these tests pin that the production paths
carry it.
"""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

import agent
import tools
from bus import BusMessage, MessageType
from specialist_registry import DelegationComplete

try:
    from tests.test_job_starter_line import (  # noqa: F401
        CONTEXT, TASK, LaunchClient, _origin, runtime)
except ImportError:
    from test_job_starter_line import (  # noqa: F401
        CONTEXT, TASK, LaunchClient, _origin, runtime)

CLOSED = ("This was an engagement, and it has ended: the person can no longer "
          "talk with the specialist in its topic.")


def test_the_launch_prompt_says_the_topic_stays_open_and_what_ends_it():
    prompt = tools.engagement_launch_prompt("Chat about groceries", "", "Ellen")
    assert "Task: Chat about groceries\n" in prompt
    assert "Context from Ellen:\n(none)\n" in prompt
    assert "stays open" in prompt
    assert "closes the topic" in prompt
    assert "emit_completion(text=..." in prompt


def test_emit_completion_says_it_closes_the_topic():
    assert "Casa closes its topic" in tools.emit_completion.description


@pytest.mark.asyncio
async def test_an_interactive_launch_sends_that_prompt(runtime):
    before = len(LaunchClient.instances)
    token = agent.origin_var.set(_origin(_operator_turn=True))
    try:
        envelope = await tools.delegate_to_agent.handler(
            {"agent": "finance", "task": TASK, "context": CONTEXT, "mode": "interactive"})
    finally:
        agent.origin_var.reset(token)
    reply = json.loads(envelope["content"][0]["text"])
    assert reply["status"] == "pending", reply
    await tools.drain_launch_turns()
    assert len(LaunchClient.instances) == before + 1
    prompt = LaunchClient.instances[-1].prompts[0]
    assert prompt == tools.engagement_launch_prompt(
        TASK, CONTEXT, tools.persona_name("assistant"))


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
