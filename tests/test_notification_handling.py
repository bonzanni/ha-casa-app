"""Tests for the NOTIFICATION + DelegationComplete branch in Agent.handle_message."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from agent import Agent
from bus import BusMessage, MessageType
from channels import ChannelManager
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from specialist_registry import DelegationComplete
from mcp_registry import McpServerRegistry
from session_registry import SessionRegistry

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = pytest.mark.asyncio



class _FakeClient:
    captured_prompts: list[str] = []

    @classmethod
    def reset(cls):
        cls.captured_prompts = []

    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def query(self, text):
        _FakeClient.captured_prompts.append(text)

    async def receive_response(self):
        from claude_agent_sdk import (
            AssistantMessage, ResultMessage, SystemMessage, TextBlock,
        )
        # Use the try-kwargs / except / __new__ pattern to tolerate SDK
        # constructor variance (matches test_agent_process.py convention).
        try:
            sys_msg = SystemMessage(subtype="init", data={"session_id": "sid"})
        except TypeError:
            sys_msg = SystemMessage.__new__(SystemMessage)
            sys_msg.subtype = "init"
            sys_msg.data = {"session_id": "sid"}
        try:
            block = TextBlock(text="got it")
        except TypeError:
            block = TextBlock.__new__(TextBlock)
            block.text = "got it"
        try:
            am = AssistantMessage(content=[block])
        except TypeError:
            am = AssistantMessage.__new__(AssistantMessage)
            am.content = [block]
        try:
            rm = ResultMessage(session_id="sid")
        except TypeError:
            rm = ResultMessage.__new__(ResultMessage)
            rm.session_id = "sid"
        yield sys_msg
        yield am
        yield rm


def _make_agent(tmp_path, role="assistant") -> Agent:
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role=role, model="claude-sonnet-4-6",
        system_prompt="Be helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000),
    )
    return Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sess.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
    )


# ---------------------------------------------------------------------------
# TestNotificationBranch
# ---------------------------------------------------------------------------


class TestNotificationBranch:
    async def test_ok_completion_synthesizes_turn(self, tmp_path):
        agent = _make_agent(tmp_path)
        complete = DelegationComplete(
            delegation_id="d-1", agent="finance", status="ok",
            text="Invoice drafted successfully.",
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "777", "cid": "c1",
                    "user_text": "draft me an invoice"},
            elapsed_s=2.5,
        )
        msg = BusMessage(
            type=MessageType.NOTIFICATION,
            source="finance", target="assistant",
            content=complete, channel="telegram",
            context={"cid": "c1", "chat_id": "777", "delegation_id": "d-1"},
        )

        _FakeClient.reset()
        with patch("sdk_client_pool._default_make_client", _FakeClient):
            await agent.handle_message(msg)

        # SDK was queried — fresh turn was synthesized.
        assert _FakeClient.captured_prompts
        prompt = _FakeClient.captured_prompts[0]
        assert "[System notification: your delegation to" in prompt
        assert "status=ok" in prompt
        assert "finance" in prompt
        assert "Invoice drafted" in prompt
        assert "draft me an invoice" in prompt

    async def test_error_completion_synthesizes_error_prompt(self, tmp_path):
        agent = _make_agent(tmp_path)
        complete = DelegationComplete(
            delegation_id="d-2", agent="finance", status="error",
            kind="sdk_error", message="SDK failed",
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "777", "cid": "c1",
                    "user_text": "draft me an invoice"},
            elapsed_s=0.5,
        )
        msg = BusMessage(
            type=MessageType.NOTIFICATION,
            source="finance", target="assistant",
            content=complete, channel="telegram",
            context={"cid": "c1", "chat_id": "777", "delegation_id": "d-2"},
        )

        _FakeClient.reset()
        with patch("sdk_client_pool._default_make_client", _FakeClient):
            await agent.handle_message(msg)

        prompt = _FakeClient.captured_prompts[0]
        assert "[System notification: your delegation to" in prompt
        assert "status=error" in prompt
        assert "failed" in prompt.lower()
        assert "sdk_error" in prompt

    async def test_restart_orphan_synthesizes_special_prompt(self, tmp_path):
        agent = _make_agent(tmp_path)
        complete = DelegationComplete(
            delegation_id="d-3", agent="finance", status="error",
            kind="restart_orphan", message="Lost on restart",
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "777", "cid": "c1",
                    "user_text": "what's the status"},
            elapsed_s=0.0,
        )
        msg = BusMessage(
            type=MessageType.NOTIFICATION,
            source="finance", target="assistant",
            content=complete, channel="telegram",
            context={"cid": "c1", "chat_id": "777", "delegation_id": "d-3"},
        )

        _FakeClient.reset()
        with patch("sdk_client_pool._default_make_client", _FakeClient):
            await agent.handle_message(msg)

        prompt = _FakeClient.captured_prompts[0]
        assert "[System notification: your delegation to" in prompt
        assert "restart" in prompt.lower() or "lost track" in prompt.lower()


# ---------------------------------------------------------------------------
# TestNonDelegationNotificationPassthrough
# ---------------------------------------------------------------------------


class TestNonDelegationNotificationPassthrough:
    async def test_notification_with_non_delegation_content(self, tmp_path):
        """A NOTIFICATION whose content is NOT a DelegationComplete must
        not be intercepted — falls through to the normal turn flow."""
        agent = _make_agent(tmp_path)
        msg = BusMessage(
            type=MessageType.NOTIFICATION,
            source="some-other-system", target="assistant",
            content="a plain string, not a DelegationComplete",
            channel="telegram",
            context={"cid": "c1", "chat_id": "777"},
        )

        _FakeClient.reset()
        with patch("sdk_client_pool._default_make_client", _FakeClient):
            await agent.handle_message(msg)

        # SDK still ran (default flow), but the prompt is the original
        # content — not a delegation synthesis.
        assert _FakeClient.captured_prompts
        prompt = _FakeClient.captured_prompts[0]
        assert "[System notification: your delegation to" not in prompt
        assert "a plain string, not a DelegationComplete" in prompt


class TestSynthesisPreservesOriginMarkers:
    """Release A Layer 3: completion synthesis must carry the origin markers
    from DelegationComplete.origin into the synthesized turn's context, so a
    delegating /invoke keeps private clearance instead of fail-closing."""

    def _synth(self, agent, origin):
        complete = DelegationComplete(
            delegation_id="d-mk", agent="finance", status="ok", text="done",
            origin={"role": "assistant", "channel": "webhook", "chat_id": "x",
                    "cid": "c", "user_text": "q", **origin},
        )
        msg = BusMessage(
            type=MessageType.NOTIFICATION, source="finance", target="assistant",
            content=complete, channel="webhook",
            context={"cid": "c", "chat_id": "x"},
        )
        return agent._synthesize_delegation_turn(msg)

    async def test_invoke_route_preserved(self, tmp_path):
        out = self._synth(_make_agent(tmp_path), {"_origin_route": "invoke"})
        assert out.context["_origin_route"] == "invoke"

    async def test_webhook_trigger_clearance_preserved(self, tmp_path):
        out = self._synth(_make_agent(tmp_path), {
            "_origin_route": "webhook_trigger", "_origin_clearance": "family"})
        assert out.context["_origin_route"] == "webhook_trigger"
        assert out.context["_origin_clearance"] == "family"

    async def test_no_markers_no_stray_keys(self, tmp_path):
        out = self._synth(_make_agent(tmp_path), {})
        assert "_origin_route" not in out.context
        assert "_origin_clearance" not in out.context
        assert "_scheduled_delivery" not in out.context

    async def test_scheduled_delivery_marker_preserved(self, tmp_path):
        """#485: scheduled turn -> delegate -> completion resumes the resident.
        The completion origin is the PERSISTED, server-built one, so its marker
        is the trustworthy value; without carrying it the resumed turn cannot
        deliver what the specialist just produced — the motivating case (a
        specialist builds the invoice PDF) fails at the last step."""
        out = self._synth(
            _make_agent(tmp_path), {"_scheduled_delivery": True},
        )
        assert out.context["_scheduled_delivery"] is True
