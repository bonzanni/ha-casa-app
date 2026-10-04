"""#1207 under ruling #1252 (option 2), on the specialist desk: a desk turn
whose protected call is waiting on the operator's approval posts only what the
specialist wrote before that call, and when that is nothing, no labelled post
at all — the resident's echo says the specialist asked a question, never that
it had nothing to add.

Red cases specified by **astra** (drive redcase round, MODE: SPECIFY, against
``350a9c74c89e9ecb573a67cd53d3be47193083ea``). Each drives the REAL
``specialist_desk.handle_reply`` over the REAL bounded runner and
``tools._run_delegated_agent`` with a scripted specialist client; the protected
call is answered by the REAL ``authz_grants.make_resident_authz_hook`` inside
the response stream, under the desk's own origin.
"""
from __future__ import annotations

import pytest
from claude_agent_sdk import (
    AssistantMessage, ResultMessage, SystemMessage, TextBlock, ToolResultBlock,
    ToolUseBlock, UserMessage,
)
from unittest.mock import patch

import specialist_desk as sd
import tools as tools_mod
from authz_grants import (
    AuthzDeps, ChallengeCoordinator, GrantStore, make_resident_authz_hook,
    _DENY_PENDING, _DENY_POSTED,
)

from test_authz_hook import _FakeChannel, _OriginCtx, _deny_reason, _origin
from test_delegate_to_agent import _FakeSpecialistClient, _specialist_cfg
from test_desk_turn import _REAL_BOUNDED, LABEL, OPERATOR, _reply, env  # noqa: F401

pytestmark = pytest.mark.asyncio

TOOL = "mcp__plugin_p_p__invoice_reset"
PROTECTED = {TOOL: {"artifact_id": "artifact-1", "summary": None}}
ARGS = {"amount": 10}
TAIL = "TAIL-A12"


class _Step:
    """A script step run INSIDE the response stream, where the SDK runs the
    protected call's hook, instead of a yielded message."""

    def __init__(self, fn) -> None:
        self.fn = fn


class _Scripted(_FakeSpecialistClient):
    script: list = []

    @classmethod
    def load(cls, *items):
        _FakeSpecialistClient.reset()
        cls.script = list(items)

    async def receive_response(self):
        yield SystemMessage(subtype="init", data={"session_id": "exec-sid"})
        for item in type(self).script:
            if isinstance(item, _Step):
                await item.fn()
                continue
            yield item
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                            is_error=False, num_turns=2, session_id="exec-sid")


def _text(t):
    return AssistantMessage(content=[TextBlock(text=t)], model="sonnet")


def _call(call_id):
    return AssistantMessage(content=[ToolUseBlock(id=call_id, name=TOOL, input=dict(ARGS))],
                            model="sonnet")


def _result(call_id, *, error=True):
    return UserMessage(content=[ToolResultBlock(tool_use_id=call_id,
                                                content="denied" if error else "ok",
                                                is_error=error)])


@pytest.fixture
def desk(env, monkeypatch):
    """The real desk harness with the REAL bounded runner and the REAL
    `_run_delegated_agent` underneath, on a real specialist config, and a
    real authorization hook for the specialist's protected tool."""
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    monkeypatch.setattr(tools_mod, "_run_delegated_agent_bounded", _REAL_BOUNDED)
    cfg = _specialist_cfg("finance")
    cfg.kind = "specialist"
    tools_mod._agent_role_map["finance"] = cfg
    keyboard = _FakeChannel()
    grants, coord = GrantStore(), ChallengeCoordinator()
    env.keyboard = keyboard
    env.grants = grants
    env.hook = make_resident_authz_hook(
        "finance", PROTECTED,
        lambda: AuthzDeps(channel=keyboard, grants=grants, challenges=coord))
    env.answers = []
    with patch("tools.ClaudeSDKClient", _Scripted):
        yield env


def _ask(env, call_id):
    async def _run():
        env.answers.append(await env.hook(
            {"tool_name": TOOL, "tool_input": dict(ARGS)}, call_id, {}))
    return _Step(_run)


async def _prime_challenge(env):
    """The identical challenge, posted BEFORE the desk turn under a plain DM
    origin (no desk scope): the desk turn's own call is then answered
    PENDING, and the desk scope holds no keyboard send of its own."""
    with _OriginCtx(_origin(chat_id=str(OPERATOR), user_id=OPERATOR)):
        out = await env.hook({"tool_name": TOOL, "tool_input": dict(ARGS)}, "prime-1", {})
    assert _deny_reason(out) == _DENY_POSTED


@pytest.mark.parametrize("prefix", ["", "BEFORE", "<silent/>"])
@pytest.mark.parametrize("answer", ["posted", "pending"])
async def test_desk_pending_approval_keeps_prefix(desk, answer, prefix):
    if answer == "pending":
        await _prime_challenge(desk)
    _Scripted.load(*([_text(prefix)] if prefix else []),
                   _call("deny-1"), _ask(desk, "deny-1"), _result("deny-1"), _text(TAIL))

    await _reply(desk, text="what is the March total?")

    want = _DENY_POSTED if answer == "posted" else _DENY_PENDING
    assert [_deny_reason(a) for a in desk.answers] == [want]
    assert len(desk.keyboard.posts) == 1
    assert desk.channel.notices == []
    if prefix == "BEFORE":
        assert len(desk.channel.replies) == 1
        assert str(desk.channel.replies[0][0]) == LABEL + "\nBEFORE"
    else:
        assert len(desk.channel.replies) == 0
        assert sd.prompt_prefix(OPERATOR) == (
            "(front desk) " + LABEL + " asked you a question.\n\n")
