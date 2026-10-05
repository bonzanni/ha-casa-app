"""#1207 arm 6 under ruling #1252: an engagement topic or a plugin job's topic
whose protected call is waiting on the operator's approval shows nothing the
model wrote after that call; the words before it are emitted and finalized.

Red case specified by **astra** (drive redcase round, MODE: SPECIFY, against
``8e9971ef241e732f4000c937814bfab1f30ffdad``). It drives the REAL
``InCasaDriver.start`` (the launch turn through ``_deliver_turn``) with a
scripted SDK client and the REAL ``authz_grants.make_resident_authz_hook``.
The hook runs where production runs it: in a task created from a copy of the
context the client was ENTERED in — ``engagement_var`` bound to the record and
``cid_var`` to the client's turn holder, as ``InCasaDriver.open`` binds them —
never in the delivery task, where no per-turn variable set by
``_deliver_turn`` can reach it.
"""
from __future__ import annotations

import asyncio
import contextvars

import pytest
from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock,
    ToolResultBlock, ToolUseBlock, UserMessage,
)

from authz_grants import (
    AuthzDeps, ChallengeCoordinator, GrantStore, make_resident_authz_hook,
    _DENY_POSTED,
)

from test_authz_hook import _deny_reason
from test_in_casa_driver import _make_record, _mk_factory_with_fake_handle
from test_pending_approval_output import ARGS, PROTECTED, TOOL, _Channel

pytestmark = [pytest.mark.asyncio]

ROLE = "finance"
OPERATOR = 42
TAIL = "TAIL-A6"


def _text(t: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=t)], model="sonnet")


def _call(call_id: str) -> AssistantMessage:
    return AssistantMessage(content=[ToolUseBlock(
        id=call_id, name=TOOL, input=dict(ARGS))], model="sonnet")


def _result(call_id: str, *, error: bool = True) -> UserMessage:
    return UserMessage(content=[ToolResultBlock(
        tool_use_id=call_id, content="denied" if error else "ok",
        is_error=error)])


def _done() -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=False, num_turns=2, session_id="eng-sid")


class _Ask:
    """A script step: the SDK reader runs the protected call's hook in a task
    of its own, copied from the context the client was entered in."""

    def __init__(self, call_id: str) -> None:
        self.call_id = call_id


class _EntryClient:
    """A scripted engagement client that keeps its ``__aenter__`` context —
    the context the SDK's read task snapshots — and runs every hook from a
    copy of it."""

    instances: list = []

    def __init__(self, options) -> None:
        self.options = options
        self.script: list = list(type(self).script)
        self.hook = type(self).hook
        self.answers = type(self).answers
        type(self).instances.append(self)

    async def __aenter__(self):
        import tools
        from log_cid import cid_var
        self.ctx = contextvars.copy_context()
        self.entry_engagement = self.ctx.run(tools.engagement_var.get, None)
        self.entry_holder = self.ctx.run(cid_var.get, None)
        return self

    async def __aexit__(self, *exc):
        return None

    async def close(self):
        return None

    async def query(self, prompt):
        return None

    async def receive_response(self):
        for item in self.script:
            if isinstance(item, _Ask):
                task = asyncio.get_running_loop().create_task(
                    self.hook({"tool_name": TOOL, "tool_input": dict(ARGS)},
                              item.call_id, {}),
                    context=self.ctx.copy())
                assert task is not asyncio.current_task()
                self.answers.append(await task)
                continue
            yield item
        yield _done()


@pytest.mark.parametrize("kind", ["specialist", "plugin"])
async def test_launch_pending_approval_keeps_prefix_from_sdk_context(
        monkeypatch, kind):
    import verdict_broker
    from drivers.in_casa_driver import InCasaDriver, _EngagementTurn
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    channel = _Channel()
    grants, coord = GrantStore(), ChallengeCoordinator()
    hook = make_resident_authz_hook(
        ROLE, PROTECTED,
        lambda: AuthzDeps(channel=channel, grants=grants, challenges=coord))
    answers: list = []
    monkeypatch.setattr(_EntryClient, "instances", [], raising=False)
    monkeypatch.setattr(_EntryClient, "script", [
        _text("BEFORE"), _call("deny-1"), _Ask("deny-1"), _result("deny-1"),
        _text(TAIL)], raising=False)
    monkeypatch.setattr(_EntryClient, "hook", staticmethod(hook), raising=False)
    monkeypatch.setattr(_EntryClient, "answers", answers, raising=False)
    monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", _EntryClient)

    factory, handle = _mk_factory_with_fake_handle()
    drv = InCasaDriver(topic_stream_factory=factory)
    rec = _make_record(kind=kind, role_or_type=ROLE, topic_id=OPERATOR,
                       origin={"chat_id": str(OPERATOR), "user_id": OPERATOR})
    await asyncio.wait_for(
        drv.start(rec, prompt="go", options=ClaudeAgentOptions(model="sonnet")),
        10)

    client = _EntryClient.instances[-1]
    assert client.entry_engagement is rec
    assert isinstance(client.entry_holder, _EngagementTurn)
    # The protected call was denied POSTED: one keyboard is up.
    assert len(answers) == 1
    assert _deny_reason(answers[0]) == _DENY_POSTED
    assert len(channel.posts) == 1
    # The topic shows only what was written before the call.
    assert [str(c.args[0]) for c in handle.emit.await_args_list] == ["BEFORE"]
    assert [str(c.args[0]) for c in handle.finalize.await_args_list] == ["BEFORE"]
