"""#1207 under ruling #1252 (option 2): a turn whose protected call is waiting
on the operator's approval shows the operator the keyboard and nothing the
model wrote after that call; words the model wrote before it still show.

Red cases specified by **astra** (drive redcase round, MODE: SPECIFY, against
``350a9c74c89e9ecb573a67cd53d3be47193083ea``). Every case drives the REAL
``Agent.handle_message`` → ``_process`` with a scripted SDK client behind
``sdk_client_pool._default_make_client``, and the REAL
``authz_grants.make_resident_authz_hook`` answering the protected call inside
the response stream — where the SDK runs it — with a real ``GrantStore``, a
real ``ChallengeCoordinator`` and a fresh ``VerdictBroker``. The script holds
the stream on events after the hook returns, never on a sleep.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import (
    AssistantMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage,
)

import authz_grants
from authz_grants import (
    AuthzDeps, ChallengeCoordinator, GrantKey, GrantStore,
    canonical_args_hash, make_resident_authz_hook,
    _DENY_PENDING, _DENY_POSTED,
)
from bus import BusMessage, MessageType
from channels import DeliveryOutcome

from test_authz_hook import _OriginCtx, _deny_reason
from test_chosen_silence_announcements import (
    ROLE, _Factory, _Hook, _make_agent, _patch_retry_sleep,
)

pytestmark = [pytest.mark.asyncio]

TOOL = "mcp__plugin_p_p__invoice_reset"
ARTIFACT = "artifact-1"
PROTECTED = {TOOL: {"artifact_id": ARTIFACT, "summary": None}}
ARGS = {"amount": 10}
TAIL = "TAIL-A12"
OPERATOR = 42


def _text(t: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=t)], model="sonnet")


def _call(call_id: str, args=None) -> AssistantMessage:
    return AssistantMessage(content=[ToolUseBlock(
        id=call_id, name=TOOL, input=dict(ARGS if args is None else args))],
        model="sonnet")


def _result(call_id: str, *, error: bool = True) -> UserMessage:
    return UserMessage(content=[ToolResultBlock(
        tool_use_id=call_id, content="denied" if error else "ok",
        is_error=error)])


def _key(args=None) -> GrantKey:
    return GrantKey(operator_id=OPERATOR, chat_id=OPERATOR,
                    enforcement_role=ROLE, artifact_id=ARTIFACT,
                    tool_name=TOOL,
                    args_hash=canonical_args_hash(ARGS if args is None else args),
                    engagement_id="")


class _Channel:
    """One Telegram double for the turn AND the approval keyboard: the
    streaming/final surface ``handle_message`` drives, plus the keyboard post
    and operator check the authorization hook's coordinator drives."""

    name = "telegram"
    chat_id = str(OPERATOR)

    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.posts: list = []
        self.send = AsyncMock(return_value=DeliveryOutcome.DELIVERED)
        self.send_response = AsyncMock(return_value=DeliveryOutcome.DELIVERED)
        self.finalize_stream = AsyncMock(return_value=DeliveryOutcome.DELIVERED)
        self.finalize_response_stream = AsyncMock(
            return_value=DeliveryOutcome.DELIVERED)
        self.turn_finished = AsyncMock()

    def create_on_token(self, _context):
        async def _on_token(text) -> None:
            self.tokens.append(str(text))
        return _on_token

    def _user_id_is_operator(self, user_id) -> bool:
        return True

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options):
        self.posts.append((chat_id, request_id, str(text), tuple(options)))
        return 55

    async def edit_dm_message(self, chat_id, message_id, text):
        return True

    def final_texts(self) -> list[str]:
        return [str(c.args[0]) for c in
                self.finalize_response_stream.await_args_list]


@pytest.fixture
async def turn(tmp_path, monkeypatch):
    import tools
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    agent = _make_agent(tmp_path)
    channel = _Channel()
    agent._channel_manager.register(channel)
    tools.init_tools(
        channel_manager=agent._channel_manager, bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
    )
    grants, coord = GrantStore(), ChallengeCoordinator()
    hook = make_resident_authz_hook(
        ROLE, PROTECTED,
        lambda: AuthzDeps(channel=channel, grants=grants, challenges=coord))
    yield agent, channel, hook, grants
    await agent.aclose()


async def _ask(hook, call_id, args=None):
    return await hook({"tool_name": TOOL,
                       "tool_input": dict(ARGS if args is None else args)},
                      call_id, {})


async def _prime_challenge(hook) -> None:
    """Post the identical challenge OUTSIDE the turn, so the turn's own call
    is answered PENDING (an identical challenge is already up)."""
    origin = {"role": ROLE, "channel": "telegram", "chat_id": str(OPERATOR),
              "user_id": OPERATOR, "cid": "prime", "message_type": "channel_in",
              "source": "telegram", "execution_role": ROLE}
    with _OriginCtx(origin):
        assert _deny_reason(await _ask(hook, "prime-1")) == _DENY_POSTED


def _msg(transport: str) -> BusMessage:
    context = {"chat_id": str(OPERATOR), "user_id": OPERATOR, "cid": "probe"}
    if transport == "button":
        context.update(synthetic="button", button_answer="yes")
    elif transport == "setup":
        context.update(synthetic="plugin_setup", plugin_setup_target=ROLE)
    return BusMessage(type=MessageType.CHANNEL_IN, source="telegram",
                      target=ROLE, content="add the invoice", channel="telegram",
                      context=context)


class _Gate:
    """The hold: the script runs the hook, records its answer, then waits for
    the test to release the stream past it."""

    def __init__(self) -> None:
        self.hook_returned = asyncio.Event()
        self.release_result = asyncio.Event()
        self.answers: list = []

    def step(self, fn):
        async def _run():
            self.answers.append(await fn())
            self.hook_returned.set()
            await self.release_result.wait()
        return _Hook(_run)


async def _drive(agent, monkeypatch, factory, msg, gate, on_hold=None):
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    with _patch_retry_sleep():
        task = asyncio.create_task(agent.handle_message(msg))
        await asyncio.wait_for(gate.hook_returned.wait(), 10)
        if on_hold is not None:
            on_hold()
        gate.release_result.set()
        return await asyncio.wait_for(task, 10)


# --- 1. the kept prefix, on every transport and both pending answers ------------

@pytest.mark.parametrize("prefix", ["", "BEFORE", "<silent/>"])
@pytest.mark.parametrize("answer", ["posted", "pending"])
@pytest.mark.parametrize("transport", ["dm", "button", "setup"])
async def test_pending_approval_keeps_only_pre_result_text(
        turn, monkeypatch, transport, answer, prefix):
    agent, channel, hook, _grants = turn
    if answer == "pending":
        await _prime_challenge(hook)
    gate = _Gate()
    script = ([_text(prefix)] if prefix else []) + [
        _call("deny-1"),
        gate.step(lambda: _ask(hook, "deny-1")),
        _result("deny-1"),
        _text(TAIL),
    ]
    factory = _Factory([script])
    held = {}

    def _on_hold():
        held["posts"] = len(channel.posts)
        held["tokens"] = list(channel.tokens)

    await _drive(agent, monkeypatch, factory, _msg(transport), gate, _on_hold)

    want = _DENY_POSTED if answer == "posted" else _DENY_PENDING
    assert [_deny_reason(a) for a in gate.answers] == [want]
    assert held["posts"] == 1
    expected_tokens = ["BEFORE"] if prefix == "BEFORE" else []
    assert held["tokens"] == expected_tokens
    assert channel.tokens == expected_tokens
    assert channel.send.await_count == 0
    assert channel.send_response.await_count == 0
    assert channel.finalize_stream.await_count == 0
    if prefix == "BEFORE":
        assert channel.final_texts() == ["BEFORE"]
        assert channel.turn_finished.await_count == 0
    else:
        assert channel.finalize_response_stream.await_count == 0
        assert channel.turn_finished.await_count == 1
    assert len(channel.posts) == 1
    assert len(factory.clients) == 1
    assert len(factory.clients[0].queries) == 1


# --- 2. a record applies at its call's RESULT, not when its hook ran -------------

@pytest.mark.parametrize("variant", ["pending-only", "same-grant-consumed-no-result",
                                     "other-grant-consumed"])
async def test_approval_records_apply_at_matching_result(turn, monkeypatch, variant):
    agent, channel, hook, grants = turn
    gate = _Gate()
    other = {"amount": 11}

    async def _hooks():
        out = [await _ask(hook, "deny-1")]
        if variant == "same-grant-consumed-no-result":
            grants.mint(_key())
            out.append(await _ask(hook, "consume-1"))
        elif variant == "other-grant-consumed":
            grants.mint(_key(other))
            out.append(await _ask(hook, "consume-1", other))
        return out

    script = [_text("BEFORE"), _call("deny-1")]
    if variant == "same-grant-consumed-no-result":
        script.append(_call("consume-1"))
    elif variant == "other-grant-consumed":
        script.append(_call("consume-1", other))
    script += [gate.step(_hooks), _text("BEFORE-2"), _result("deny-1")]
    if variant == "other-grant-consumed":
        script.append(_result("consume-1", error=False))
    script.append(_text(TAIL))

    await _drive(agent, monkeypatch, _Factory([script]), _msg("dm"), gate)

    (answers,) = gate.answers
    assert _deny_reason(answers[0]) == _DENY_POSTED
    assert answers[1:] == ([] if variant == "pending-only" else [{}])
    assert channel.tokens == ["BEFORE", "BEFORE\n\nBEFORE-2"]
    assert channel.final_texts() == ["BEFORE\n\nBEFORE-2"]
    assert len(channel.posts) == 1


# --- 3. the cut is the WINNING attempt's ------------------------------------------

async def test_pending_cut_uses_winning_attempt_messages(turn, monkeypatch):
    agent, channel, hook, _grants = turn
    CLIConnectionError = type("CLIConnectionError", (RuntimeError,), {})
    gate = _Gate()
    attempt_1 = [_text("LOSER-1"), _text("LOSER-2"), CLIConnectionError("reset")]
    attempt_2 = [_text("BEFORE"), _call("deny-1"),
                 gate.step(lambda: _ask(hook, "deny-1")),
                 _text("BEFORE-2"), _result("deny-1"), _text(TAIL)]
    factory = _Factory([attempt_1, attempt_2])

    await _drive(agent, monkeypatch, factory, _msg("dm"), gate)

    assert len(factory.clients) == 2
    assert [len(c.queries) for c in factory.clients] == [1, 1]
    assert channel.final_texts() == ["BEFORE\n\nBEFORE-2"]
    assert channel.tokens == ["LOSER-1", "LOSER-1\n\nLOSER-2",
                              "BEFORE", "BEFORE\n\nBEFORE-2"]
