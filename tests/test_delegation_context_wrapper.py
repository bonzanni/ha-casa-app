"""Verify <delegation_context> block is prepended to delegated calls."""

from __future__ import annotations

import pytest

import tools
import agent as agent_mod
from agent_registry import AgentRegistry
from config import AgentConfig, CharacterConfig

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = pytest.mark.asyncio


def _make_cfg(role: str, name: str) -> AgentConfig:
    return AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role=role, model="x",
        character=CharacterConfig(name=name),
        system_prompt="x",
    )


async def test_delegation_context_block_includes_caller_and_register(monkeypatch):
    """Origin: assistant on telegram → suggested_register=text, caller=Ellen."""
    captured_prompts: list[str] = []

    async def _fake_query(self, prompt):
        captured_prompts.append(prompt)

    class _FakeClient:
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass
        query = _fake_query
        async def receive_response(self):
            if False:
                yield None
            return

    residents = {
        "assistant": _make_cfg("assistant", "Ellen"),
        "butler": _make_cfg("butler", "Tina"),
    }
    reg = AgentRegistry.build(residents=residents, specialists={})
    monkeypatch.setattr(tools, "_agent_registry", reg, raising=False)
    monkeypatch.setattr(tools, "_agent_role_map", dict(residents), raising=False)
    monkeypatch.setattr(tools, "ClaudeSDKClient", _FakeClient)

    target_cfg = _make_cfg("butler", "Tina")
    token = agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram", "chat_id": "1",
        "user_id": 1, "cid": "abc", "user_text": "x",
        "delegation_depth": 0,
    })
    try:
        await tools._run_delegated_agent(
            target_cfg, "turn off the lights", "(none)",
        )
    finally:
        agent_mod.origin_var.reset(token)

    assert captured_prompts, "expected at least one query"
    prompt = captured_prompts[0]
    assert "<delegation_context>" in prompt
    assert "caller_role: assistant" in prompt
    assert "caller_name: Ellen" in prompt
    assert "originating_channel: telegram" in prompt
    assert "suggested_register: text" in prompt
    assert "Task: turn off the lights" in prompt


async def test_delegated_child_origin_carries_delegation_quota_key(monkeypatch):
    """#541: the launching call site binds tools._delegation_quota_key around
    its create_task; the delegated run stamps it onto child_origin as
    ``_delegation_id`` — send_media's quota key for the ephemeral context."""
    captured_origins: list[dict] = []

    async def _fake_query(self, prompt):
        captured_origins.append(dict(agent_mod.origin_var.get(None) or {}))

    class _FakeClient:
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass
        query = _fake_query
        async def receive_response(self):
            if False:
                yield None
            return

    residents = {
        "assistant": _make_cfg("assistant", "Ellen"),
        "butler": _make_cfg("butler", "Tina"),
    }
    reg = AgentRegistry.build(residents=residents, specialists={})
    monkeypatch.setattr(tools, "_agent_registry", reg, raising=False)
    monkeypatch.setattr(tools, "_agent_role_map", dict(residents), raising=False)
    monkeypatch.setattr(tools, "ClaudeSDKClient", _FakeClient)

    import asyncio
    origin_token = agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram", "chat_id": "1",
        "user_id": 1, "cid": "abc", "user_text": "x",
        "delegation_depth": 0,
    })
    qk_token = tools._delegation_quota_key.set("deleg-123")
    try:
        # Mirror the production shape: the key is bound around create_task
        # and travels via the task's context snapshot.
        task = asyncio.create_task(tools._run_delegated_agent(
            _make_cfg("butler", "Tina"), "turn off the lights", "(none)"))
    finally:
        tools._delegation_quota_key.reset(qk_token)
        agent_mod.origin_var.reset(origin_token)
    await task

    assert captured_origins
    assert captured_origins[0]["_delegation_id"] == "deleg-123"
    assert captured_origins[0]["delegation_depth"] == 1


async def test_caller_name_follows_a_rename_without_a_full_restart(monkeypatch):
    """#436: `tools._agent_registry` is a BOOT snapshot — `sync_agent_role_map`
    refreshes the role map beside it and never touches it — so a caller
    renamed by a per-role reload kept introducing itself to every specialist
    under its old name until the add-on restarted. The name must come from the
    live map, which is also what the delegation ACL resolves against."""
    captured_prompts: list[str] = []

    async def _fake_query(self, prompt):
        captured_prompts.append(prompt)

    class _FakeClient:
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass
        query = _fake_query
        async def receive_response(self):
            if False:
                yield None
            return

    # The boot registry still says "Ellen"; the live map says "Elle".
    stale = AgentRegistry.build(
        residents={"assistant": _make_cfg("assistant", "Ellen")},
        specialists={},
    )
    monkeypatch.setattr(tools, "_agent_registry", stale, raising=False)
    monkeypatch.setattr(
        tools, "_agent_role_map",
        {"assistant": _make_cfg("assistant", "Elle"),
         "butler": _make_cfg("butler", "Tina")},
        raising=False,
    )
    monkeypatch.setattr(tools, "ClaudeSDKClient", _FakeClient)

    token = agent_mod.origin_var.set({
        "role": "assistant", "channel": "telegram", "chat_id": "1",
        "user_id": 1, "cid": "abc", "user_text": "x",
        "delegation_depth": 0,
    })
    try:
        await tools._run_delegated_agent(
            _make_cfg("butler", "Tina"), "turn off the lights", "ctx",
        )
    finally:
        agent_mod.origin_var.reset(token)

    prompt = captured_prompts[0]
    assert "caller_name: Elle" in prompt
    assert "Ellen" not in prompt
    # The body's attribution line is built from the same value.
    assert "Context from Elle:" in prompt


async def test_delegation_context_voice_channel_yields_voice_register(monkeypatch):
    captured: list[str] = []

    async def _fake_query(self, prompt):
        captured.append(prompt)

    class _FakeClient:
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass
        query = _fake_query
        async def receive_response(self):
            if False:
                yield None
            return

    reg = AgentRegistry.build(
        residents={"butler": _make_cfg("butler", "Tina")},
        specialists={},
    )
    monkeypatch.setattr(tools, "_agent_registry", reg, raising=False)
    monkeypatch.setattr(tools, "ClaudeSDKClient", _FakeClient)

    target_cfg = _make_cfg("butler", "Tina")
    token = agent_mod.origin_var.set({
        "role": "butler", "channel": "voice", "chat_id": "1",
        "user_id": 1, "cid": "abc", "user_text": "x",
        "delegation_depth": 0,
    })
    try:
        await tools._run_delegated_agent(target_cfg, "x", "")
    finally:
        agent_mod.origin_var.reset(token)

    assert "suggested_register: voice" in captured[0]


async def test_child_origin_extends_parent_with_execution_role(monkeypatch):
    """Pins INV-ENG-004 (the stamp). Red case demonstrated: stamping the
    child origin with depth 0 instead of parent+1 fails this test."""
    """A:§1/r1-B5: child_origin must EXTEND the parent dict (delegation_depth
    stays parent+1) while adding execution_role == the DELEGATE's role,
    distinct from the inherited (unchanged) "role" == the CALLER's role.
    The parent's origin_var snapshot must be restored after the call."""
    captured: dict[str, dict] = {}

    async def _fake_query(self, prompt):
        captured["during"] = dict(agent_mod.origin_var.get(None))

    class _FakeClient:
        def __init__(self, options): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass
        query = _fake_query
        async def receive_response(self):
            if False:
                yield None
            return

    reg = AgentRegistry.build(
        residents={
            "assistant": _make_cfg("assistant", "Ellen"),
            "butler": _make_cfg("butler", "Tina"),
        },
        specialists={},
    )
    monkeypatch.setattr(tools, "_agent_registry", reg, raising=False)
    monkeypatch.setattr(tools, "ClaudeSDKClient", _FakeClient)

    target_cfg = _make_cfg("butler", "Tina")
    parent_origin = {
        "role": "assistant", "channel": "telegram", "chat_id": "1",
        "user_id": 1, "cid": "abc", "user_text": "x",
        "delegation_depth": 0, "execution_role": "assistant",
    }
    token = agent_mod.origin_var.set(parent_origin)
    try:
        await tools._run_delegated_agent(target_cfg, "turn off the lights", "(none)")
        # Restored to the exact parent snapshot after the delegated turn.
        after = agent_mod.origin_var.get(None)
        assert after is parent_origin
        assert after["execution_role"] == "assistant"
    finally:
        agent_mod.origin_var.reset(token)

    during = captured["during"]
    assert during["role"] == "assistant"          # caller's role, unchanged
    assert during["execution_role"] == "butler"    # delegate's own role
    assert during["delegation_depth"] == 1         # parent(0) + 1, r1-B5
    assert during["channel"] == "telegram"         # rest of parent preserved
    assert during["chat_id"] == "1"
