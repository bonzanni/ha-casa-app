"""#1409: text a model or the operator reads names the CONFIGURED assistant,
never the default persona hard-coded."""
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _role_map(**names):
    return {r: SimpleNamespace(character=SimpleNamespace(name=n))
            for r, n in names.items()}


def test_persona_name_reads_the_configured_persona(monkeypatch):
    import tools
    monkeypatch.setattr(tools, "_agent_role_map", _role_map(assistant="Marta",
                                                            butler="Alfred"))
    assert tools.persona_name() == "Marta"
    assert tools.persona_name("assistant") == "Marta"
    assert tools.persona_name("butler") == "Alfred"
    monkeypatch.setattr(tools, "_agent_role_map", {})
    assert tools.persona_name() == "the assistant"


def test_no_default_persona_name_in_static_text():
    import tools
    from channels.telegram import TelegramChannel
    assert "Ellen" not in tools.emit_completion.description
    assert "Ellen" not in tools.SPECIALIST_OPEN_CONVERSATION_NOTICE_TEMPLATE
    assert "Ellen" not in json.dumps(TelegramChannel.ENGAGEMENT_COMMANDS)


@pytest.mark.asyncio
async def test_observer_prompt_names_the_engagers_persona(monkeypatch):
    import claude_agent_sdk
    import sdk_logging
    import tools
    from observer import Observer

    monkeypatch.setattr(tools, "_agent_role_map", _role_map(assistant="Marta"))
    captured = {}
    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = claude_agent_sdk.ClaudeAgentOptions
    fake.TextBlock = claude_agent_sdk.TextBlock
    fake.AssistantMessage = claude_agent_sdk.AssistantMessage

    class Client:
        def __init__(self, options):
            captured["system"] = options.system_prompt

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def query(self, prompt):
            pass

        async def receive_response(self):
            if False:
                yield None

    fake.ClaudeSDKClient = Client
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    monkeypatch.setattr(sdk_logging, "with_stderr_callback",
                        lambda options, engagement_id=None: options)
    obs = Observer(bus=MagicMock(), engagement_registry=MagicMock(),
                   model_name="haiku")
    rec = SimpleNamespace(id="e-1", task="t", role_or_type="x",
                          origin={"role": "assistant"})
    await obs._decide_interjection("warn", {}, rec)
    assert captured["system"].startswith("You are Marta's observer."), captured


try:
    from tests.test_job_starter_line import (  # noqa: F401
        CONTEXT, TASK, LaunchClient, _origin, runtime)
except ImportError:
    from test_job_starter_line import (  # noqa: F401
        CONTEXT, TASK, LaunchClient, _origin, runtime)


@pytest.mark.asyncio
async def test_engagement_brief_names_the_engagers_persona(runtime, monkeypatch):
    import agent
    import tools
    monkeypatch.setattr(tools._agent_role_map["assistant"].character, "name", "Marta")
    token = agent.origin_var.set(_origin(_operator_turn=True))
    try:
        envelope = await tools.delegate_to_agent.handler(
            {"agent": "finance", "task": TASK, "context": CONTEXT, "mode": "interactive"})
    finally:
        agent.origin_var.reset(token)
    assert json.loads(envelope["content"][0]["text"])["status"] == "pending"
    await tools.drain_launch_turns()
    prompt = LaunchClient.instances[-1].prompts[0]
    assert "Context from Marta:\n" in prompt, prompt
