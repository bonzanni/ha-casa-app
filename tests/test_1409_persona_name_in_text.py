"""#1409: text a model or the operator reads names the CONFIGURED assistant,
never the default persona hard-coded."""
import json
from types import SimpleNamespace

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
