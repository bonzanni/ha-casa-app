"""#1207 arm 4 under ruling #1252: a resident turn whose SYNCHRONOUS delegate
left an approval waiting shows the operator the keyboard and nothing the
resident wrote after the delegate's result; the words before it still show,
and the delegate result the resident's model receives is unchanged.

Red case specified by **astra** (drive redcase round, MODE: SPECIFY, against
``8e9971ef241e732f4000c937814bfab1f30ffdad``). It drives the REAL
``Agent.handle_message`` with a scripted resident client, the REAL
``delegate_to_agent`` handler and delegated runner with a scripted child
client, and the REAL ``authz_grants.make_resident_authz_hook`` answering the
child's protected call. The handler runs as the SDK runs an in-process MCP
tool: in a task whose context is a copy of the one the client was connected
in (where ``origin_var`` is the pooled client's holder), never in the task
that folds the resident's messages. The resident's ``ToolResultBlock``
carries the handler's own returned content.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ToolResultBlock, ToolUseBlock,
    UserMessage,
)

from authz_grants import (
    AuthzDeps, ChallengeCoordinator, GrantStore, make_resident_authz_hook,
    _DENY_POSTED,
)
from session_registry import SessionRegistry

from test_authz_hook import _deny_reason
from test_chosen_silence_announcements import (
    _Factory, _Hook, _ScriptedClient, _make_agent, _patch_retry_sleep,
)
from test_delegate_to_agent import _caller_cfg, _specialist_cfg
from test_pending_approval_desk import _Scripted, _Step
from test_pending_approval_output import (
    ARGS, PROTECTED, TOOL, _Channel, _call, _msg, _result, _text,
)

pytestmark = [pytest.mark.asyncio]

DELEGATE = "mcp__casa-framework__delegate_to_agent"
SPECIALIST = "finance"
DELEGATE_ARGS = {"agent": SPECIALIST, "task": "reset invoice", "context": "",
                 "mode": "sync"}
TAIL = "TAIL-A4"


class _ConnectedClient(_ScriptedClient):
    """A scripted resident client that keeps the context it was connected in —
    the context the SDK's read task snapshots, and so the one every
    in-process MCP handler task copies."""

    async def connect(self):
        self.ctx = contextvars.copy_context()
        return None


class _ConnectedFactory(_Factory):
    def __call__(self, options) -> _ConnectedClient:
        script = self._scripts.pop(0) if self._scripts else []
        c = _ConnectedClient(options, script, sid=f"sid-{len(self.clients) + 1}")
        self.clients.append(c)
        return c


def _delegate_call(call_id: str) -> AssistantMessage:
    return AssistantMessage(content=[ToolUseBlock(
        id=call_id, name=DELEGATE, input=dict(DELEGATE_ARGS))], model="sonnet")


async def test_sync_delegate_pending_approval_keeps_resident_prefix(tmp_path):
    import tools
    import verdict_broker
    agent = _make_agent(tmp_path)
    channel = _Channel()
    agent._channel_manager.register(channel)
    registry = MagicMock()
    registry.register_delegation = AsyncMock()
    registry.complete_delegation = AsyncMock()
    tools.init_tools(channel_manager=agent._channel_manager, bus=MagicMock(),
                     specialist_registry=registry, mcp_registry=MagicMock())
    cfg = _specialist_cfg(SPECIALIST)
    grants, coord = GrantStore(), ChallengeCoordinator()
    hook = make_resident_authz_hook(
        SPECIALIST, PROTECTED,
        lambda: AuthzDeps(channel=channel, grants=grants, challenges=coord))
    hook_answers: list = []
    delegate_results: list = []

    async def _child_asks():
        hook_answers.append(await hook(
            {"tool_name": TOOL, "tool_input": dict(ARGS)}, "deny-1", {}))

    _Scripted.load(_text("CHILD-BEFORE"), _call("deny-1"), _Step(_child_asks),
                   _result("deny-1"), _text("CHILD-AFTER"))

    # The resident's result block for the delegate call: filled with what the
    # handler actually returned, before the stream yields it.
    delegate_block = ToolResultBlock(tool_use_id="delegate-1", content=None,
                                     is_error=False)
    factory = _ConnectedFactory([[
        _text("BEFORE"),
        _delegate_call("delegate-1"),
        None,  # replaced below by the handler step
        UserMessage(content=[delegate_block]),
        _text(TAIL),
    ]])

    async def _run_handler():
        client = factory.clients[-1]
        task = asyncio.get_running_loop().create_task(
            tools.delegate_to_agent.handler(dict(DELEGATE_ARGS)),
            context=client.ctx.copy())
        result = await task
        assert task is not asyncio.current_task()
        delegate_block.content = result["content"]
        delegate_block.is_error = bool(result.get("is_error", False))
        delegate_results.append(json.loads(result["content"][0]["text"]))

    factory._scripts[0][2] = _Hook(_run_handler)

    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(verdict_broker, "BROKER",
                                              verdict_broker.VerdictBroker()))
            stack.enter_context(patch.object(SessionRegistry, "_save_locked",
                                              AsyncMock()))
            stack.enter_context(patch.object(
                tools, "_agent_role_map",
                {"assistant": _caller_cfg(), SPECIALIST: cfg}))
            stack.enter_context(patch.object(
                tools, "_prelaunch",
                AsyncMock(return_value=(SPECIALIST, cfg, None, None, None))))
            stack.enter_context(patch.object(
                tools, "_build_specialist_options",
                return_value=ClaudeAgentOptions(model="sonnet")))
            stack.enter_context(patch.object(tools, "_delegated_resolution",
                                              return_value=None))
            stack.enter_context(patch("tools.ClaudeSDKClient", _Scripted))
            stack.enter_context(patch("sdk_client_pool._default_make_client",
                                      factory))
            stack.enter_context(_patch_retry_sleep())
            await asyncio.wait_for(agent.handle_message(_msg("dm")), 10)
    finally:
        await agent.aclose()

    # The child's protected call was denied POSTED: one keyboard is up.
    assert len(hook_answers) == 1
    assert _deny_reason(hook_answers[0]) == _DENY_POSTED
    assert len(channel.posts) == 1
    # The resident's model received the delegate result unchanged and whole.
    assert len(delegate_results) == 1
    assert delegate_results[0]["status"] == "ok"
    assert delegate_results[0]["text"] == "CHILD-BEFORE\n\nCHILD-AFTER"
    # The operator sees only what the resident wrote before the delegate's
    # result: streamed and final.
    assert channel.tokens == ["BEFORE"]
    assert channel.final_texts() == ["BEFORE"]


# ---------------------------------------------------------------------------
# The rest of arm 4: one resident turn, any number of sync delegates, each
# delegate's child running its own script; the same production shape as the
# red case above (handler in a task copied from the connect context, the
# resident's result block carrying the handler's own content).
# ---------------------------------------------------------------------------

from claude_agent_sdk import ResultMessage, SystemMessage  # noqa: E402

from authz_grants import (  # noqa: E402
    GrantKey, canonical_args_hash, _DENY_DELIVERY_FAILED, _DENY_PENDING,
)
from test_authz_hook import _OriginCtx  # noqa: E402


class _Child(_Scripted):
    """One script per delegated run, in launch order; ``subtype`` is the CLI's
    verdict on that run (an ``error_*`` subtype is a CLI abort)."""

    queue: list = []

    async def receive_response(self):
        script, subtype = type(self).queue.pop(0)
        yield SystemMessage(subtype="init", data={"session_id": "exec-sid"})
        for item in script:
            if isinstance(item, _Step):
                await item.fn()
                continue
            yield item
        yield ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1,
                            is_error=False, num_turns=2, session_id="exec-sid")


def _key(args=None) -> GrantKey:
    return GrantKey(operator_id=42, chat_id=42, enforcement_role=SPECIALIST,
                    artifact_id="artifact-1", tool_name=TOOL,
                    args_hash=canonical_args_hash(dict(ARGS if args is None else args)),
                    engagement_id="")


class _Turn:
    """The resident turn's world: channel, grants, the child's hook."""

    def __init__(self, tmp_path, *, warm: bool = False) -> None:
        self.agent = _warm_agent(tmp_path) if warm else _make_agent(tmp_path)
        self.channel = _Channel()
        self.agent._channel_manager.register(self.channel)
        self.grants, self.coord = GrantStore(), ChallengeCoordinator()
        self.hook = make_resident_authz_hook(
            SPECIALIST, PROTECTED,
            lambda: AuthzDeps(channel=self.channel, grants=self.grants,
                              challenges=self.coord))
        self.answers: list = []
        self.results: list = []
        self.factory: _ConnectedFactory | None = None

    def ask(self, call_id: str, args=None) -> _Step:
        async def _run():
            self.answers.append(await self.hook(
                {"tool_name": TOOL, "tool_input": dict(ARGS if args is None else args)},
                call_id, {}))
        return _Step(_run)

    def step(self, fn) -> _Step:
        async def _run():
            await fn()
        return _Step(_run)

    def delegate(self, call_id: str, child: list, *, subtype: str = "success",
                 mode: str = "sync") -> list:
        """The resident's call, the handler run as the SDK runs it, and the
        result block carrying what the handler returned."""
        args = dict(DELEGATE_ARGS, mode=mode)
        block = ToolResultBlock(tool_use_id=call_id, content=None, is_error=False)

        async def _run_handler():
            _Child.queue.append((list(child), subtype))
            client = self.factory.clients[-1]
            result = await asyncio.get_running_loop().create_task(
                tools_mod().delegate_to_agent.handler(args),
                context=client.ctx.copy())
            block.content = result["content"]
            block.is_error = bool(result.get("is_error", False))
            self.results.append(json.loads(result["content"][0]["text"]))

        return [AssistantMessage(content=[ToolUseBlock(
                    id=call_id, name=DELEGATE, input=args)], model="sonnet"),
                _Hook(_run_handler),
                UserMessage(content=[block])]

    async def run(self, resident: list, *, extra=(), before=None,
                  retries=()) -> None:
        import verdict_broker
        tools = tools_mod()
        registry = MagicMock()
        registry.register_delegation = AsyncMock()
        registry.complete_delegation = AsyncMock()
        tools.init_tools(channel_manager=self.agent._channel_manager,
                         bus=MagicMock(), specialist_registry=registry,
                         mcp_registry=MagicMock())
        cfg = _specialist_cfg(SPECIALIST)
        # A retried attempt cold-connects a fresh client: attempt N runs
        # script N (``retries`` are the scripts of attempts 2, 3, ...).
        self.factory = _ConnectedFactory([resident, *retries])
        _Child.queue = []
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(verdict_broker, "BROKER",
                                                  verdict_broker.VerdictBroker()))
                stack.enter_context(patch.object(SessionRegistry, "_save_locked",
                                                  AsyncMock()))
                stack.enter_context(patch.object(
                    tools, "_agent_role_map",
                    {"assistant": _caller_cfg(), SPECIALIST: cfg}))
                stack.enter_context(patch.object(
                    tools, "_prelaunch",
                    AsyncMock(return_value=(SPECIALIST, cfg, None, None, None))))
                stack.enter_context(patch.object(
                    tools, "_build_specialist_options",
                    return_value=ClaudeAgentOptions(model="sonnet")))
                stack.enter_context(patch.object(tools, "_delegated_resolution",
                                                  return_value=None))
                stack.enter_context(patch("tools.ClaudeSDKClient", _Child))
                stack.enter_context(patch("sdk_client_pool._default_make_client",
                                          self.factory))
                stack.enter_context(_patch_retry_sleep())
                for cm in extra:
                    stack.enter_context(cm)
                if before is not None:
                    await before()
                await asyncio.wait_for(self.agent.handle_message(_msg("dm")), 10)
        finally:
            await self.agent.aclose()


def tools_mod():
    import tools
    return tools


def _warm_agent(tmp_path):
    """A resident the pool keeps warm between turns (the eligibility the
    pooling suite's agent has: a role id, a binding digest, a provenance)."""
    from agent import Agent
    from channels import ChannelManager
    from config import AgentConfig, CharacterConfig, MemoryConfig, ToolsConfig
    from mcp_registry import McpServerRegistry
    from role_artifact_stub import STUB_ROLE_ARTIFACT
    from session_reg_helpers import (
        RESIDENT_DIGEST, resident_prov, resident_role_id,
    )
    cfg = AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role="assistant",
                      model="claude-sonnet-4-6", system_prompt="You are helpful.",
                      character=CharacterConfig(name="Test"),
                      tools=ToolsConfig(allowed=["Read"],
                                        permission_mode="acceptEdits"),
                      memory=MemoryConfig(token_budget=1000,
                                          read_strategy="per_turn"),
                      role_id=resident_role_id("assistant"), kind="resident",
                      binding_digest=RESIDENT_DIGEST,
                      speaker_provenance=resident_prov("assistant"))
    return Agent(config=cfg,
                 session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
                 mcp_registry=McpServerRegistry(), channel_manager=ChannelManager())


def _child_denied(call_id="deny-1", args=None, turn=None) -> list:
    return [_text("CHILD-BEFORE"), _call(call_id), turn.ask(call_id, args),
            _result(call_id), _text("CHILD-AFTER")]


async def _prime(turn: _Turn) -> None:
    """Post the identical challenge OUTSIDE the turn, so the child's call is
    answered PENDING once that keyboard's post has settled."""
    origin = {"role": SPECIALIST, "channel": "telegram", "chat_id": "42",
              "user_id": 42, "cid": "prime", "message_type": "channel_in",
              "source": "telegram", "execution_role": SPECIALIST}
    with _OriginCtx(origin):
        assert _deny_reason(await turn.hook(
            {"tool_name": TOOL, "tool_input": dict(ARGS)}, "prime-1", {})) \
            == _DENY_POSTED


# --- the kept prefix: both pending answers, with and without a prefix --------

@pytest.mark.parametrize("prefix", ["", "BEFORE"])
@pytest.mark.parametrize("answer", ["posted", "pending"])
async def test_a_waiting_child_keeps_only_the_residents_words_before_the_result(
        tmp_path, answer, prefix):
    turn = _Turn(tmp_path)
    resident = (([_text(prefix)] if prefix else [])
                + turn.delegate("d-1", _child_denied(turn=turn)) + [_text(TAIL)])
    await turn.run(resident,
                   before=(lambda: _prime(turn)) if answer == "pending" else None)
    assert [_deny_reason(a) for a in turn.answers] == [
        _DENY_POSTED if answer == "posted" else _DENY_PENDING]
    assert len(turn.channel.posts) == 1
    if prefix:
        assert turn.channel.tokens == [prefix]
        assert turn.channel.final_texts() == [prefix]
    else:
        # Nothing before the delegate: no streamed token, no reply at all — the
        # turn ends on the keyboard, through turn_finished.
        assert turn.channel.tokens == []
        assert turn.channel.final_texts() == []
        assert turn.channel.send.await_count == 0
        assert turn.channel.send_response.await_count == 0
        assert turn.channel.turn_finished.await_count == 1


async def test_the_delegate_result_the_resident_receives_is_unchanged(tmp_path):
    """The specialist's answer reaches the resident's model whole: the cut is on
    the resident's words to the operator, never on the tool result."""
    turn = _Turn(tmp_path)
    await turn.run([_text("BEFORE")] + turn.delegate("d-1", _child_denied(turn=turn))
                   + [_text(TAIL)])
    (payload,) = turn.results
    assert set(payload) == {"status", "delegation_id", "agent", "elapsed_s",
                            "text", "output_truncated"}
    assert (payload["status"], payload["agent"], payload["text"],
            payload["output_truncated"]) == (
        "ok", SPECIALIST, "CHILD-BEFORE\n\nCHILD-AFTER", False)


# --- runs whose child left nothing waiting deliver the resident's words -------

async def _deliver_tail(turn: _Turn, child: list, **kw) -> None:
    await turn.run([_text("BEFORE")] + turn.delegate("d-1", child, **kw)
                   + [_text(TAIL)])
    assert turn.channel.tokens[-1] == "BEFORE\n\n" + TAIL
    assert turn.channel.final_texts() == ["BEFORE\n\n" + TAIL]


async def test_a_child_whose_keyboard_was_not_delivered_leaves_the_words(tmp_path):
    turn = _Turn(tmp_path)
    turn.channel.post_dm_keyboard = AsyncMock(side_effect=RuntimeError("down"))
    await _deliver_tail(turn, _child_denied(turn=turn))
    assert [_deny_reason(a) for a in turn.answers] == [_DENY_DELIVERY_FAILED]


async def test_a_child_that_consumed_its_grant_leaves_the_words(tmp_path):
    turn = _Turn(tmp_path)
    turn.grants.mint(_key())
    await _deliver_tail(turn, [_call("ok-1"), turn.ask("ok-1"),
                               _result("ok-1", error=False), _text("Done.")])
    assert turn.answers == [{}]


async def test_an_async_delegation_leaves_the_words(tmp_path):
    """An async child runs past the turn; its result is a later announcement."""
    turn = _Turn(tmp_path)
    await _deliver_tail(turn, _child_denied(turn=turn), mode="async")
    assert turn.results[0]["status"] == "pending"


async def test_a_sync_wait_that_degraded_to_pending_leaves_the_words(tmp_path):
    """#1049's note path owns a decision that lands after the wait gave up."""
    turn = _Turn(tmp_path)
    release = asyncio.Event()

    async def _late():
        await release.wait()

    child = [turn.step(_late)] + _child_denied(turn=turn)
    tools = tools_mod()
    try:
        await turn.run([_text("BEFORE")] + turn.delegate("d-1", child)
                       + [_text(TAIL)],
                       extra=[patch.object(tools, "_SYNC_WAIT_TIMEOUT_S", 0.05),
                              patch.object(tools, "_attach_completion_callback",
                                           MagicMock())])
    finally:
        release.set()
    assert turn.results[0]["status"] == "pending"
    assert turn.channel.final_texts() == ["BEFORE\n\n" + TAIL]


# --- a child that aborted or raised after a waiting deny: the keyboard is up --

@pytest.mark.parametrize("ending", ["cli_abort", "raised"])
async def test_a_child_that_ended_badly_after_a_waiting_deny_still_cuts(
        tmp_path, ending):
    turn = _Turn(tmp_path)
    child = _child_denied(turn=turn)
    kw = {}
    if ending == "cli_abort":
        kw["subtype"] = "error_max_turns"
    else:
        async def _boom():
            raise RuntimeError("child client failed")
        child = child + [turn.step(_boom)]
    await turn.run([_text("BEFORE")] + turn.delegate("d-1", child, **kw)
                   + [_text(TAIL)])
    assert turn.results[0]["status"] == "error"
    assert len(turn.channel.posts) == 1
    assert turn.channel.tokens == ["BEFORE"]
    assert turn.channel.final_texts() == ["BEFORE"]


# --- a later consume of the same grant releases; another grant does not -------

async def test_a_redelegation_that_consumes_the_same_grant_releases_the_words(
        tmp_path):
    """The operator approved while the resident was still writing; the
    resident delegated again and the child spent the grant."""
    turn = _Turn(tmp_path)

    async def _approve():
        turn.grants.mint(_key())

    resident = ([_text("BEFORE")] + turn.delegate("d-1", _child_denied(turn=turn))
                + [_text("BETWEEN"), _Hook(_approve)]
                + turn.delegate("d-2", [_call("ok-1"), turn.ask("ok-1"),
                                        _result("ok-1", error=False),
                                        _text("Done.")])
                + [_text(TAIL)])
    await turn.run(resident)
    assert [_deny_reason(a) if a else "allow" for a in turn.answers] == [
        _DENY_POSTED, "allow"]
    assert turn.channel.final_texts() == ["BEFORE\n\nBETWEEN\n\n" + TAIL]


async def test_a_redelegation_that_consumes_then_raises_still_releases(tmp_path):
    """R6-2: the consume is a fact about the grant store, whatever the run did
    afterwards; nothing is waiting any more."""
    turn = _Turn(tmp_path)

    async def _approve():
        turn.grants.mint(_key())

    async def _boom():
        raise RuntimeError("child client failed")

    resident = ([_text("BEFORE")] + turn.delegate("d-1", _child_denied(turn=turn))
                + [_Hook(_approve)]
                + turn.delegate("d-2", [_call("ok-1"), turn.ask("ok-1"),
                                        _result("ok-1", error=False),
                                        turn.step(_boom)])
                + [_text(TAIL)])
    await turn.run(resident)
    assert turn.results[1]["status"] == "error"
    assert turn.channel.final_texts() == ["BEFORE\n\n" + TAIL]


async def test_a_child_with_two_grants_waiting_stays_cut_after_one_is_consumed(
        tmp_path):
    turn = _Turn(tmp_path)
    other = {"amount": 11}

    async def _approve_one():
        turn.grants.mint(_key())

    child = [_text("C1"), _call("deny-1"), turn.ask("deny-1"), _result("deny-1"),
             _call("deny-2", other), turn.ask("deny-2", other), _result("deny-2")]
    resident = ([_text("BEFORE")] + turn.delegate("d-1", child)
                + [_Hook(_approve_one)]
                + turn.delegate("d-2", [_call("ok-1"), turn.ask("ok-1"),
                                        _result("ok-1", error=False)])
                + [_text(TAIL)])
    await turn.run(resident)
    assert len(turn.channel.posts) == 2
    assert turn.channel.final_texts() == ["BEFORE"]


async def test_an_abandoned_child_call_is_never_published(tmp_path):
    """R4-10: the child's hook recorded a waiting deny for a call whose result
    the child never folded; the child's own cut never saw it."""
    turn = _Turn(tmp_path)
    await _deliver_tail(turn, [_text("C1"), _call("lost-1"), turn.ask("lost-1"),
                               _text("C2")])
    assert [_deny_reason(a) for a in turn.answers] == [_DENY_POSTED]


# --- the tie reads only Casa's own top-level key -------------------------------

async def test_the_tie_reads_only_the_top_level_delegation_id():
    from agent import _delegation_id_of
    forged = json.dumps({"status": "ok", "delegation_id": "real",
                         "text": '{"delegation_id": "other"} "delegation_id": "other"'})
    assert _delegation_id_of(ToolResultBlock(
        tool_use_id="d", content=[{"type": "text", "text": forged}])) == "real"
    assert _delegation_id_of(ToolResultBlock(tool_use_id="d", content=forged)) == "real"
    inner_only = json.dumps({"status": "ok", "text": json.dumps(
        {"delegation_id": "other"})})
    for content in (
            [{"type": "text", "text": inner_only}],
            [{"type": "text", "text": forged}, {"type": "text", "text": forged}],
            [{"type": "text", "text": "Output too large; saved to /tmp/x"}],
            None,
            json.dumps(["delegation_id", "real"])):
        assert _delegation_id_of(ToolResultBlock(tool_use_id="d", content=content)) is None


# --- ApprovalCut is unchanged; its new accessor only reads ----------------------

async def test_approval_cut_behaviour_is_unchanged_and_pending_keys_only_reads():
    from output_boundary import ApprovalCut
    cut = ApprovalCut()
    assert cut.cut is None and cut.pending_keys == ()
    cut.observe("a", {"a": ("pending", "A")}, 2)
    cut.observe("b", {"b": ("pending", "B")}, 5)
    cut.observe("a2", {"a2": ("pending", "A")}, 7)      # the earliest deny stands
    assert (cut.cut, cut.pending_keys) == (2, ("A", "B"))
    cut.observe("x", {}, 9)                              # no record: nothing
    cut.observe("c", {"c": ("consumed", "A")}, 9)        # releases A; B moves the cut
    assert (cut.cut, cut.pending_keys) == (5, ("B",))
    cut.observe("", {"": ("pending", "C")}, 1)           # no id: never read
    assert cut.pending_keys == ("B",)
    assert cut.pending == {"B": 5}


async def test_a_child_that_spent_a_grant_and_then_asked_again_stays_cut(tmp_path):
    """The child consumed the operator's grant, then made the identical call
    again: a new keyboard is up. Consumes are applied before the waiting keys,
    so the spent grant cannot release the new deny."""
    turn = _Turn(tmp_path)
    turn.grants.mint(_key())
    child = [_call("ok-1"), turn.ask("ok-1"), _result("ok-1", error=False),
             _text("C1"), _call("deny-1"), turn.ask("deny-1"), _result("deny-1"),
             _text("C2")]
    await turn.run([_text("BEFORE")] + turn.delegate("d-1", child) + [_text(TAIL)])
    assert [_deny_reason(a) if a else "allow" for a in turn.answers] == [
        "allow", _DENY_POSTED]
    assert len(turn.channel.posts) == 1
    assert turn.channel.final_texts() == ["BEFORE"]


class _WarmClient(_ConnectedClient):
    """One pooled client serving several turns: each query runs the next
    script, and every handler still runs from the context of the ONE connect —
    so whatever a later turn's fold sets in its own task never reaches it."""

    def __init__(self, options, scripts: list, sid: str) -> None:
        super().__init__(options, [], sid)
        self._scripts = list(scripts)

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)
        self._script = self._scripts.pop(0)


class _WarmFactory(_ConnectedFactory):
    def __call__(self, options) -> _WarmClient:
        c = _WarmClient(options, self._scripts, sid=f"sid-{len(self.clients) + 1}")
        self._scripts = []
        self.clients.append(c)
        return c


async def test_a_warm_clients_second_turn_is_cut_on_its_own_scope(tmp_path):
    """The handler reaches the launching turn's scope through the pooled
    holder its entry snapshot copies — rewritten in place at each turn — not
    through anything bound when the client connected during an earlier turn."""
    turn = _Turn(tmp_path, warm=True)
    first = [_text("Hello.")]
    second = [_text("BEFORE")] + turn.delegate("d-1", _child_denied(turn=turn)) \
        + [_text(TAIL)]
    tools = tools_mod()
    turn.factory = _WarmFactory([first, second])

    async def _two_turns(_resident, **kw):
        import verdict_broker
        registry = MagicMock()
        registry.register_delegation = AsyncMock()
        registry.complete_delegation = AsyncMock()
        tools.init_tools(channel_manager=turn.agent._channel_manager,
                         bus=MagicMock(), specialist_registry=registry,
                         mcp_registry=MagicMock())
        cfg = _specialist_cfg(SPECIALIST)
        _Child.queue = []
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(verdict_broker, "BROKER",
                                                  verdict_broker.VerdictBroker()))
                stack.enter_context(patch.object(SessionRegistry, "_save_locked",
                                                  AsyncMock()))
                stack.enter_context(patch.object(
                    tools, "_agent_role_map",
                    {"assistant": _caller_cfg(), SPECIALIST: cfg}))
                stack.enter_context(patch.object(
                    tools, "_prelaunch",
                    AsyncMock(return_value=(SPECIALIST, cfg, None, None, None))))
                stack.enter_context(patch.object(
                    tools, "_build_specialist_options",
                    return_value=ClaudeAgentOptions(model="sonnet")))
                stack.enter_context(patch.object(tools, "_delegated_resolution",
                                                  return_value=None))
                stack.enter_context(patch("tools.ClaudeSDKClient", _Child))
                stack.enter_context(patch("sdk_client_pool._default_make_client",
                                          turn.factory))
                stack.enter_context(_patch_retry_sleep())
                await asyncio.wait_for(turn.agent.handle_message(_msg("dm")), 10)
                await asyncio.wait_for(turn.agent.handle_message(_msg("dm")), 10)
        finally:
            await turn.agent.aclose()

    await _two_turns(None)
    assert len(turn.factory.clients) == 1          # one connect, two turns
    assert len(turn.factory.clients[0].queries) == 2
    assert len(turn.channel.posts) == 1
    assert turn.channel.final_texts() == ["Hello.", "BEFORE"]


# ---------------------------------------------------------------------------
# #1274: a delegate result the fold cannot read. The bundled CLI rewrites an
# oversized MCP result before the turn reads it (stage-1 persist or
# truncation, stage-2 ``<persisted-output>``), and an aborted call's result is
# its interruption text; none of them names the delegation id at its top
# level. The child's outcome is still on the turn's scope, copied there before
# the handler returned. Red case specified by **astra** (drive redcase round,
# MODE: SPECIFY, against ``759be5f06746d6c3803971ae5a3e86817808f466``).
# ---------------------------------------------------------------------------

from claude_agent_sdk import CLIConnectionError  # noqa: E402

from agent import _delegation_id_of  # noqa: E402
from output_boundary import current_turn_scope  # noqa: E402


def _x0t(content):
    n = len(content[0]["text"])
    return [{"type": "text", "text": (
        f"Error: result ({n} characters) exceeds maximum allowed tokens. "
        "Output has been saved to /data/tool-results/mcp-x.json.\nFormat: JSON")}]


def _truncate(content):
    return [{"type": "text", "text": content[0]["text"][:100]},
            {"type": "text",
             "text": "[OUTPUT TRUNCATED - exceeded 25000 token limit]"}]


def _persisted(content):
    pretty = json.dumps(content, indent=2)
    return [{"type": "text", "text": (
        "<persisted-output>\nOutput too large (120 KB). Full output saved to: "
        "/data/tool-results/x.txt\n\nPreview (first 2KB):\n"
        + pretty[:2000] + "\n...\n</persisted-output>")}]


def _interrupted(content):
    return [{"type": "text",
             "text": "The tool call was interrupted before a result was received"}]


REWRITES = {"x0t": _x0t, "truncate": _truncate, "persisted": _persisted,
            "interrupted": _interrupted}


def _unreadable(steps: list, rewrite) -> list:
    """``_Turn.delegate``'s steps with the resident's result block rewritten
    after the real handler returned — what the CLI does before the SDK yields
    it. ``turn.results`` keeps the handler's real payload."""
    call, handler, result = steps
    (block,) = result.content

    async def _then_rewrite():
        await handler.fn()
        block.content = rewrite(block.content)
        assert _delegation_id_of(block) is None

    return [call, _Hook(_then_rewrite), result]


class _Seen:
    """What the turn's scope held at chosen points of the resident's stream."""

    def __init__(self) -> None:
        self.records: list[dict] = []
        self.scopes: list = []

    def step(self) -> _Hook:
        async def _look():
            scope = current_turn_scope()
            self.scopes.append(scope)
            self.records.append(dict(scope.delegated_approvals))
        return _Hook(_look)


@pytest.mark.parametrize("prefix", ["BEFORE", ""])
@pytest.mark.parametrize("shape", list(REWRITES))
async def test_unreadable_sync_delegate_waiting_child_keeps_only_prefix(
        tmp_path, shape, prefix):
    turn = _Turn(tmp_path)
    seen = _Seen()
    call, handler, result = _unreadable(
        turn.delegate("d-1", _child_denied(turn=turn)), REWRITES[shape])
    resident = (([_text(prefix)] if prefix else [])
                + [call, handler, seen.step(), result, _text(TAIL)])
    await turn.run(resident)
    assert len(turn.factory.clients) == 1
    assert [len(c.queries) for c in turn.factory.clients] == [1]
    assert len(turn.results) == 1
    assert turn.results[0]["text"] == "CHILD-BEFORE\n\nCHILD-AFTER"
    assert seen.records == [{turn.results[0]["delegation_id"]: (("pending", _key()),)}]
    assert [_deny_reason(a) for a in turn.answers] == [_DENY_POSTED]
    assert len(turn.channel.posts) == 1
    expected = [prefix] if prefix else []
    assert turn.channel.tokens == expected
    assert turn.channel.final_texts() == expected
    assert turn.channel.turn_finished.await_count == (0 if prefix else 1)
    if not prefix:
        assert turn.channel.send.await_count == 0
        assert turn.channel.send_response.await_count == 0


@pytest.mark.parametrize("order", ["d1-first", "d2-first"])
async def test_unreadable_waiting_sibling_cuts_in_both_result_orders(
        tmp_path, order):
    """Two delegates of one response: d-1's child left the ONLY waiting grant
    and its result is unreadable; d-2's child left nothing and its result is
    readable. Their results fold at one index, in either order."""
    turn = _Turn(tmp_path)
    seen = _Seen()
    c1, h1, r1 = _unreadable(turn.delegate("d-1", _child_denied(turn=turn)), _x0t)
    c2, h2, r2 = turn.delegate("d-2", [_text("CHILD-ONLY")])
    both = AssistantMessage(content=[*c1.content, *c2.content], model="sonnet")
    blocks = [*r1.content, *r2.content]
    if order == "d2-first":
        blocks.reverse()
    await turn.run([_text("BEFORE"), both, h1, h2, seen.step(),
                    UserMessage(content=blocks), _text(TAIL)])
    assert len(turn.factory.clients) == 1
    assert len(turn.results) == 2
    assert [_deny_reason(a) for a in turn.answers] == [_DENY_POSTED]
    assert len(turn.channel.posts) == 1
    assert seen.records == [{turn.results[0]["delegation_id"]: (("pending", _key()),)}]
    assert turn.channel.tokens == ["BEFORE"]
    assert turn.channel.final_texts() == ["BEFORE"]
    assert turn.channel.turn_finished.await_count == 0


# --- #1274 regression pins: what the fallback must not feed -----------------

@pytest.mark.parametrize("shape", list(REWRITES))
async def test_unreadable_delegate_without_waiting_child_keeps_tail(tmp_path, shape):
    """Nothing waiting, nothing cut: an unreadable result alone is no evidence."""
    turn = _Turn(tmp_path)
    seen = _Seen()
    call, handler, result = _unreadable(
        turn.delegate("d-1", [_text("CHILD-ONLY")]), REWRITES[shape])
    await turn.run([_text("BEFORE"), call, handler, seen.step(), result,
                    _text(TAIL)])
    assert turn.answers == []
    assert len(turn.channel.posts) == 0
    assert seen.records == [{}]
    assert turn.channel.tokens == ["BEFORE", "BEFORE\n\n" + TAIL]
    assert turn.channel.final_texts() == ["BEFORE\n\n" + TAIL]


async def test_failed_attempt_record_is_not_fed_at_later_unreadable_delegate_result(
        tmp_path):
    """The scope's records are per TURN and survive a retry; the fold's state is
    per attempt. A record attempt 1 left (its result never folded) is not
    evidence about attempt 2's d-2, whose child left nothing waiting."""
    turn = _Turn(tmp_path)
    seen = _Seen()
    c1, h1, _r1 = turn.delegate("d-1", _child_denied(turn=turn))
    attempt_1 = [_text("A1"), c1, h1, seen.step(), CLIConnectionError("reset")]
    c2, h2, r2 = _unreadable(turn.delegate("d-2", [_text("CHILD-ONLY")]), _x0t)
    attempt_2 = [_text("BEFORE"), c2, h2, seen.step(), r2, _text(TAIL)]
    await turn.run(attempt_1, retries=[attempt_2])
    assert len(turn.factory.clients) == 2
    assert [len(c.queries) for c in turn.factory.clients] == [1, 1]
    assert len(turn.results) == 2
    assert [_deny_reason(a) for a in turn.answers] == [_DENY_POSTED]
    assert len(turn.channel.posts) == 1
    assert seen.scopes[0] is seen.scopes[1]
    d1 = turn.results[0]["delegation_id"]
    assert seen.records == [{d1: (("pending", _key()),)}] * 2
    assert turn.channel.tokens == ["A1", "BEFORE", "BEFORE\n\n" + TAIL]
    assert turn.channel.final_texts() == ["BEFORE\n\n" + TAIL]
    # Never fed, never popped: the dead attempt's record is still there.
    assert seen.scopes[0].delegated_approvals == {d1: (("pending", _key()),)}


async def test_later_readable_delegate_consume_releases_unreadable_cut(tmp_path):
    """The fallback feeds consumes as well as pendings: a later readable
    delegate whose child spent the same grant releases the cut."""
    turn = _Turn(tmp_path)

    async def _approve():
        turn.grants.mint(_key())

    resident = ([_text("BEFORE")]
                + _unreadable(turn.delegate("d-1", _child_denied(turn=turn)), _x0t)
                + [_text("BETWEEN"), _Hook(_approve)]
                + turn.delegate("d-2", [_call("ok-1"), turn.ask("ok-1"),
                                        _result("ok-1", error=False),
                                        _text("Done.")])
                + [_text(TAIL)])
    await turn.run(resident)
    assert [_deny_reason(a) if a else "allow" for a in turn.answers] == [
        _DENY_POSTED, "allow"]
    assert len(turn.channel.posts) == 1
    assert turn.channel.tokens == ["BEFORE", "BEFORE\n\nBETWEEN\n\n" + TAIL]
    assert turn.channel.final_texts() == ["BEFORE\n\nBETWEEN\n\n" + TAIL]


def _bare_call(call_id: str, name: str) -> AssistantMessage:
    return AssistantMessage(content=[ToolUseBlock(id=call_id, name=name, input={})],
                            model="sonnet")


async def test_unreadable_non_delegate_result_preserves_delegate_records(tmp_path):
    """Only Casa's delegate result can stand for a delegation; another tool's
    unreadable result feeds and pops nothing."""
    turn = _Turn(tmp_path)
    seen = _Seen()
    c1, h1, _r1 = turn.delegate("d-1", _child_denied(turn=turn))
    other = ToolResultBlock(tool_use_id="read-1", content=_x0t(
        [{"type": "text", "text": "x" * 10}]), is_error=False)
    await turn.run([_text("BEFORE"), c1, h1, _bare_call("read-1", "Read"),
                    UserMessage(content=[other]), seen.step(), _text(TAIL)])
    d1 = turn.results[0]["delegation_id"]
    assert seen.records == [{d1: (("pending", _key()),)}]
    assert len(turn.channel.posts) == 1
    assert turn.channel.tokens == ["BEFORE", "BEFORE\n\n" + TAIL]
    assert turn.channel.final_texts() == ["BEFORE\n\n" + TAIL]


async def test_readable_unknown_delegate_id_preserves_sibling_record(tmp_path):
    """A delegate result that names an id is tied by that id only, even when no
    record carries it: it never falls back to another delegation's record."""
    turn = _Turn(tmp_path)
    seen = _Seen()
    c1, h1, _r1 = turn.delegate("d-1", _child_denied(turn=turn))
    unknown = ToolResultBlock(tool_use_id="d-2", content=[{"type": "text", "text":
        json.dumps({"status": "ok", "delegation_id": "unknown"})}], is_error=False)
    assert _delegation_id_of(unknown) == "unknown"
    await turn.run([_text("BEFORE"), c1, h1, _bare_call("d-2", DELEGATE),
                    UserMessage(content=[unknown]), seen.step(), _text(TAIL)])
    d1 = turn.results[0]["delegation_id"]
    assert seen.records == [{d1: (("pending", _key()),)}]
    assert len(turn.channel.posts) == 1
    assert turn.channel.tokens == ["BEFORE", "BEFORE\n\n" + TAIL]
    assert turn.channel.final_texts() == ["BEFORE\n\n" + TAIL]
