"""#1075 — a buffered turn whose LAST message is a closing ``<silent/>``.

The operator's ruling on #1075 (options 2 and 3 combined), for scheduled turns
and event wakes — the turns whose scope carries ``NoStream``: when the turn's
last text-bearing message strips to ``<silent/>`` and earlier text exists,

1. after at least one Casa send, every one confirmed delivered (no failed or
   unresolved send, no error, no retry), the closing sentinel wins and the turn
   is silent;
2. otherwise the earlier text is delivered WITHOUT the literal sentinel.

Before the fix the final reply was judged only on the ``"\\n\\n"``-join of every
assistant message, so ``"Done.\\n\\n<silent/>"`` reached the operator tag and all.

Red cases specified by **astra** (drive redcase round, MODE: SPECIFY, against
``17bd039afdf3710c3dd3ddee902420c9991e58f5``). Every case drives the REAL
``Agent.handle_message`` → ``_process`` → ``_make_on_message`` fold with a
scripted SDK client behind ``sdk_client_pool._default_make_client`` — never a
patched ``_process`` — and every send goes through the REGISTERED tool handler.
Each case runs on a SCHEDULED turn and on an event wake, and asserts the exact
list of final replies the channel was handed (a count and the bodies), apart
from the tool sends.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage as _SDKAssistantMessage,
    ResultMessage as _SDKResultMessage,
    TextBlock as _SDKTextBlock,
)

import retry as retry_mod
from agent import Agent
from bus import BusMessage, MessageType
from channels import ChannelManager, DeliveryOutcome
from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
from mcp_registry import McpServerRegistry
from session_registry import SessionRegistry

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:  # pragma: no cover — run from inside tests/
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio]

ROLE = "assistant"
SENT = "Bins out tonight (sent-by-tool-8812)."


# ---------------------------------------------------------------------------
# The scripted SDK boundary
# ---------------------------------------------------------------------------


def _mk_assistant(text: str) -> _SDKAssistantMessage:
    try:
        return _SDKAssistantMessage(content=[_SDKTextBlock(text=text)])
    except TypeError:  # pragma: no cover — older SDK constructor
        m = _SDKAssistantMessage.__new__(_SDKAssistantMessage)
        m.content = [_SDKTextBlock(text)]  # type: ignore[call-arg]
        return m


def _mk_result(sid: str) -> _SDKResultMessage:
    m = _SDKResultMessage.__new__(_SDKResultMessage)
    m.session_id = sid  # type: ignore[attr-defined]
    m.is_error = False  # type: ignore[attr-defined]
    m.result = ""  # type: ignore[attr-defined]
    return m


class _Hook:
    """A script step that runs a coroutine INSIDE the response stream — where
    the SDK runs a tool call — instead of yielding a message."""

    def __init__(self, fn) -> None:
        self.fn = fn


class _ScriptedClient:
    def __init__(self, options, script: list, sid: str) -> None:
        self.options = options
        self.queries: list[str] = []
        self._script = script
        self._sid = sid

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)

    async def receive_response(self):
        for item in self._script:
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, _Hook):
                await item.fn()
                continue
            yield item
        yield _mk_result(self._sid)


class _Factory:
    """One script per client construction."""

    def __init__(self, scripts: list[list]) -> None:
        self._scripts = list(scripts)
        self.clients: list[_ScriptedClient] = []

    def __call__(self, options) -> _ScriptedClient:
        script = self._scripts.pop(0) if self._scripts else []
        c = _ScriptedClient(options, script, sid=f"sid-{len(self.clients) + 1}")
        self.clients.append(c)
        return c


@contextmanager
def _patch_retry_sleep():
    # retry.py's MODULE-LOCAL asyncio only — never the shared asyncio.sleep.
    ns = SimpleNamespace(sleep=AsyncMock(), CancelledError=asyncio.CancelledError)
    with patch.object(retry_mod, "asyncio", ns):
        yield


# ---------------------------------------------------------------------------
# The resident, its channel, and the two buffered turn kinds
# ---------------------------------------------------------------------------


class _TelegramStub:
    """Tool sends land on ``send``; the final reply of a buffered turn on
    ``send_response`` (no token callback, so never a stream finalize)."""

    name = "telegram"

    def __init__(self, send_outcome=DeliveryOutcome.DELIVERED) -> None:
        self.send = AsyncMock(return_value=send_outcome)
        self.send_response = AsyncMock(return_value=DeliveryOutcome.DELIVERED)
        self.finalize_stream = AsyncMock(return_value=DeliveryOutcome.DELIVERED)
        self.finalize_response_stream = AsyncMock(
            return_value=DeliveryOutcome.DELIVERED)
        self.turn_finished = AsyncMock()
        self.on_token_created = 0

    def create_on_token(self, _context):
        self.on_token_created += 1

        async def _on_token(_text: str) -> None:
            return None
        return _on_token

    def final_texts(self) -> list[str]:
        calls = (self.send_response.await_args_list
                 + self.finalize_stream.await_args_list
                 + self.finalize_response_stream.await_args_list)
        return [str(c.args[0]) for c in calls]

    def tool_sends(self) -> list[str]:
        return [str(c.args[0]) for c in self.send.await_args_list]


def _make_agent(tmp_path) -> Agent:
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT,
        role=ROLE,
        model="claude-sonnet-4-6",
        system_prompt="You are helpful.",
        character=CharacterConfig(name="Test"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=1000, read_strategy="per_turn"),
    )
    return Agent(
        config=cfg,
        session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
        mcp_registry=McpServerRegistry(),
        channel_manager=ChannelManager(),
    )


async def _resident(tmp_path, send_outcome=DeliveryOutcome.DELIVERED):
    agent = _make_agent(tmp_path)
    stub = _TelegramStub(send_outcome)
    agent._channel_manager.register(stub)
    import tools
    tools.init_tools(
        channel_manager=agent._channel_manager, bus=MagicMock(),
        specialist_registry=MagicMock(), mcp_registry=MagicMock(),
    )
    return agent, stub


def _scheduled() -> BusMessage:
    return BusMessage(
        type=MessageType.SCHEDULED, source="scheduler", target=ROLE,
        content="Run the bins reminder.", channel="telegram",
        context={"chat_id": "bins-reminder", "cid": "sched-1"},
    )


def _event_wake() -> BusMessage:
    return BusMessage(
        type=MessageType.CHANNEL_IN, source="events", target=ROLE,
        content="A plugin event arrived.", channel="telegram",
        context={"chat_id": "123", "cid": "wake-1", "synthetic": "event_wake"},
    )


TURNS = [pytest.param(_scheduled, id="scheduled"),
         pytest.param(_event_wake, id="event_wake")]


async def _run(agent, msg, factory, monkeypatch):
    monkeypatch.setattr("sdk_client_pool._default_make_client", factory)
    with _patch_retry_sleep():
        return await agent.handle_message(msg)


def _send(results: list, channel: str = "telegram", message: str = SENT):
    async def _go():
        import tools
        results.append(await tools.send_message.handler(
            {"message": message, "channel": channel}))
    return _Hook(_go)


def _assert_buffered(stub, factory):
    # The turn really was buffered, and ran once.
    assert stub.on_token_created == 0
    assert len(factory.clients) == 1


# ---------------------------------------------------------------------------
# The ruling's table, row by row
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("make_msg", TURNS)
async def test_row1_a_confirmed_send_then_closing_silence_is_silent(
    tmp_path, monkeypatch, make_msg,
):
    """Reminder sent (confirmed) → "Done." → ``<silent/>``: the reminder only."""
    agent, stub = await _resident(tmp_path)
    results: list = []
    factory = _Factory([[_send(results), _mk_assistant("Done."),
                         _mk_assistant("<silent/>")]])
    try:
        await _run(agent, make_msg(), factory, monkeypatch)
    finally:
        await agent.aclose()

    _assert_buffered(stub, factory)
    assert len(results) == 1 and not results[0].get("is_error")
    assert len(stub.tool_sends()) == 1 and SENT in stub.tool_sends()[0]
    assert stub.final_texts() == []


@pytest.mark.parametrize("make_msg", TURNS)
async def test_row2_a_send_refused_before_admission_keeps_the_text(
    tmp_path, monkeypatch, make_msg,
):
    """A confirmed send, then a send refused before it was ever admitted (an
    unknown channel): "any send failed", so the text is delivered, untagged."""
    agent, stub = await _resident(tmp_path)
    results: list = []
    factory = _Factory([[_send(results), _send(results, channel="nosuch"),
                         _mk_assistant("Done."), _mk_assistant("<silent/>")]])
    try:
        await _run(agent, make_msg(), factory, monkeypatch)
    finally:
        await agent.aclose()

    _assert_buffered(stub, factory)
    assert [bool(r.get("is_error")) for r in results] == [False, True]
    assert len(stub.tool_sends()) == 1
    assert stub.final_texts() == ["Done."]


@pytest.mark.parametrize("make_msg", TURNS)
async def test_row2_an_unknown_outcome_keeps_the_text(
    tmp_path, monkeypatch, make_msg,
):
    """A send whose channel cannot report (UNKNOWN) is never confirmed."""
    agent, stub = await _resident(tmp_path, DeliveryOutcome.UNKNOWN)
    results: list = []
    factory = _Factory([[_send(results), _mk_assistant("Done."),
                         _mk_assistant("<silent/>")]])
    try:
        await _run(agent, make_msg(), factory, monkeypatch)
    finally:
        await agent.aclose()

    _assert_buffered(stub, factory)
    assert len(results) == 1 and not results[0].get("is_error")
    assert len(stub.tool_sends()) == 1
    assert stub.final_texts() == ["Done."]


@pytest.mark.parametrize("make_msg", TURNS)
async def test_row3_no_send_keeps_the_narration_without_the_tag(
    tmp_path, monkeypatch, make_msg,
):
    """No Casa send (heartbeat, briefing): the narration arrives, untagged."""
    agent, stub = await _resident(tmp_path)
    factory = _Factory([[_mk_assistant("I'll run the checklist."),
                         _mk_assistant("<silent/>")]])
    try:
        await _run(agent, make_msg(), factory, monkeypatch)
    finally:
        await agent.aclose()

    _assert_buffered(stub, factory)
    assert stub.tool_sends() == []
    assert stub.final_texts() == ["I'll run the checklist."]


@pytest.mark.parametrize("make_msg", TURNS)
async def test_a_synchronous_delegates_refused_send_keeps_the_text(
    tmp_path, monkeypatch, make_msg,
):
    """A confirmed parent send, then a SYNCHRONOUS delegate whose send is
    refused before admission. The delegate runs under the scope view
    ``delegate_to_agent`` binds for it (``TurnScope.for_child(...,
    synchronous=True)`` on a frozen copy of the launcher's origin), so its
    refusal belongs to the launching turn: the text is delivered, untagged."""
    import agent as agent_mod
    import tools
    from output_boundary import TurnScope

    agent, stub = await _resident(tmp_path)
    parent_results: list = []
    child_results: list = []
    child_scopes: list = []

    async def _child_send():
        origin = tools._snapshot_origin()
        parent_scope = origin["turn_scope"]
        child_origin = dict(origin)
        child_origin["turn_scope"] = TurnScope.for_child(
            parent_scope, "", synchronous=True)
        child_scopes.append((parent_scope, child_origin["turn_scope"]))
        tok = agent_mod.origin_var.set(child_origin)
        try:
            child_results.append(await tools.send_message.handler(
                {"message": "Child note.", "channel": "nosuch"}))
        finally:
            agent_mod.origin_var.reset(tok)

    factory = _Factory([[_send(parent_results), _Hook(_child_send),
                         _mk_assistant("Done."), _mk_assistant("<silent/>")]])
    try:
        await _run(agent, make_msg(), factory, monkeypatch)
    finally:
        await agent.aclose()

    _assert_buffered(stub, factory)
    (parent_scope, child_scope), = child_scopes
    assert child_scope.operator_sends is parent_scope.operator_sends
    assert [bool(r.get("is_error")) for r in parent_results] == [False]
    assert [bool(r.get("is_error")) for r in child_results] == [True]
    assert len(stub.tool_sends()) == 1
    assert stub.final_texts() == ["Done."]
