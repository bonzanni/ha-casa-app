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


# ---------------------------------------------------------------------------
# The rest of arm 6, on the same production shape: every hook runs in a task
# created from a copy of its client's __aenter__ context.
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock  # noqa: E402

from authz_grants import (  # noqa: E402
    GrantKey, canonical_args_hash, _DENY_DELIVERY_FAILED, _DENY_PENDING,
)
from drivers.in_casa_driver import (  # noqa: E402
    LAUNCH_MISSING_RESULT, LAUNCH_NO_VISIBLE_OUTPUT,
)
from test_authz_hook import _OriginCtx  # noqa: E402


class _AskArgs(_Ask):
    def __init__(self, call_id: str, args: dict) -> None:
        super().__init__(call_id)
        self.args = args


class _Do:
    """A script step that runs ``fn`` (the operator approving, say)."""

    def __init__(self, fn) -> None:
        self.fn = fn


NO_RESULT = object()   # the stream ends with no ResultMessage


class _Session:
    """One scripted client per open/resume/open_fresh, each with its own entry
    context; each query runs the next script from one shared queue."""

    def __init__(self, hook) -> None:
        self.hook = hook
        self.scripts: list = []
        self.clients: list = []
        self.answers: list = []
        session = self

        class _Client:
            def __init__(self, options) -> None:
                self.options = options
                self.script: list = []
                session.clients.append(self)

            async def __aenter__(self):
                self.ctx = contextvars.copy_context()
                return self

            async def __aexit__(self, *exc):
                return None

            async def close(self):
                return None

            async def query(self, prompt):
                self.script = session.scripts.pop(0)

            async def receive_response(self):
                for item in self.script:
                    if isinstance(item, _Ask):
                        args = getattr(item, "args", ARGS)
                        task = asyncio.get_running_loop().create_task(
                            session.hook({"tool_name": TOOL,
                                          "tool_input": dict(args)},
                                         item.call_id, {}),
                            context=self.ctx.copy())
                        session.answers.append(await task)
                        continue
                    if isinstance(item, _Do):
                        await item.fn()
                        continue
                    if item is NO_RESULT:
                        return
                    yield item
                yield _done()

        self.client_cls = _Client


def _reason(answer) -> str:
    return "allow" if answer == {} else _deny_reason(answer)


class _Topic:
    def __init__(self, monkeypatch, kind="specialist", rec_id="e1") -> None:
        import verdict_broker
        from drivers.in_casa_driver import InCasaDriver
        monkeypatch.setattr(verdict_broker, "BROKER",
                            verdict_broker.VerdictBroker())
        self.channel = _Channel()
        self.grants, self.coord = GrantStore(), ChallengeCoordinator()
        self.hook = make_resident_authz_hook(
            ROLE, PROTECTED,
            lambda: AuthzDeps(channel=self.channel, grants=self.grants,
                              challenges=self.coord))
        self.session = _Session(self.hook)
        monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient",
                            self.session.client_cls)
        self.factory, self.handle = _mk_factory_with_fake_handle()
        self.drv = InCasaDriver(topic_stream_factory=self.factory)
        self.rec = _make_record(id=rec_id, kind=kind, role_or_type=ROLE,
                                topic_id=OPERATOR,
                                origin={"chat_id": str(OPERATOR),
                                        "user_id": OPERATOR})

    def key(self, args=None) -> GrantKey:
        return GrantKey(operator_id=OPERATOR, chat_id=OPERATOR,
                        enforcement_role=ROLE, artifact_id="artifact-1",
                        tool_name=TOOL,
                        args_hash=canonical_args_hash(dict(ARGS if args is None else args)),
                        engagement_id=self.rec.id)

    def approve(self, args=None) -> _Do:
        async def _mint():
            self.grants.mint(self.key(args))
        return _Do(_mint)

    async def launch(self, *script) -> None:
        self.session.scripts.append(list(script))
        await asyncio.wait_for(self.drv.start(
            self.rec, prompt="go", options=ClaudeAgentOptions(model="sonnet")), 10)

    async def follow_up(self, *script, **kw) -> None:
        self.session.scripts.append(list(script))
        await asyncio.wait_for(self.drv.send_user_turn(self.rec, "next", **kw), 10)

    @property
    def emits(self) -> list[str]:
        return [str(c.args[0]) for c in self.handle.emit.await_args_list]

    @property
    def finals(self) -> list[str]:
        return [str(c.args[0]) for c in self.handle.finalize.await_args_list]


def _waiting(call_id="deny-1"):
    return [_call(call_id), _Ask(call_id), _result(call_id)]


# --- the launch: nothing after the waiting call, and it is not reported dead --

@pytest.mark.parametrize("kind", ["specialist", "plugin"])
@pytest.mark.parametrize("tail", [True, False])
async def test_a_launch_whose_only_words_are_withheld_shows_nothing_and_lives(
        monkeypatch, kind, tail):
    """R6-1 with no prefix, R6-4 and R6-4b: no emit, no finalize — and the
    launch observation the owner reads is empty, so it reports no death and
    withdraws no challenge. tail=False is the mute launch after a POSTED deny,
    reported dead at the base."""
    t = _Topic(monkeypatch, kind)
    await t.launch(*_waiting(), *([_text(TAIL)] if tail else []))
    assert [_reason(a) for a in t.session.answers] == [_DENY_POSTED]
    assert len(t.channel.posts) == 1
    assert t.emits == [] and t.finals == []
    assert t.drv.launch_turn_incomplete(t.rec.id) == ""


async def test_a_reused_keyboard_whose_post_settled_cuts_too(monkeypatch):
    """R6-2: the identical challenge was already up (posted outside the turn);
    the turn's call is answered PENDING once that post settled."""
    t = _Topic(monkeypatch)
    with _OriginCtx({}, engagement=t.rec):
        assert _deny_reason(await t.hook(
            {"tool_name": TOOL, "tool_input": dict(ARGS)}, "prime-1", {})) \
            == _DENY_POSTED
    await t.launch(_text("BEFORE"), *_waiting(), _text(TAIL))
    assert [_reason(a) for a in t.session.answers] == [_DENY_PENDING]
    assert len(t.channel.posts) == 1
    assert t.emits == ["BEFORE"] and t.finals == ["BEFORE"]


async def test_a_follow_up_turn_and_a_continuation_reaching_a_second_call(
        monkeypatch):
    """R6-3: a ticketed turn is cut the same way, and so is a continuation that
    spends the approved grant and then reaches another protected call."""
    t = _Topic(monkeypatch)
    other = {"amount": 11}
    await t.launch(_text("Hi."))
    await t.follow_up(_text("BEFORE"), *_waiting(), _text(TAIL))
    await t.follow_up(t.approve(), _call("ok-1"), _Ask("ok-1"),
                      _result("ok-1", error=False), _text("Done the first."),
                      _call("deny-2"), _AskArgs("deny-2", other),
                      _result("deny-2"), _text(TAIL))
    assert [_reason(a) for a in t.session.answers] == [
        _DENY_POSTED, "allow", _DENY_POSTED]
    assert t.finals == ["Hi.", "BEFORE", "Done the first."]
    assert TAIL not in "".join(t.emits)


async def test_a_consume_of_the_same_grant_releases_the_words(monkeypatch):
    """R6-5: the operator approved during the turn and the model ran the call
    again; nothing is waiting any more."""
    t = _Topic(monkeypatch)
    await t.launch(_text("BEFORE"), *_waiting(), _text("MID"), t.approve(),
                   _call("ok-1"), _Ask("ok-1"), _result("ok-1", error=False),
                   _text(TAIL))
    assert [_reason(a) for a in t.session.answers] == [_DENY_POSTED, "allow"]
    assert t.emits == ["BEFORE", "BEFORE\n\nMID\n\n" + TAIL]
    assert t.finals == ["BEFORE\n\nMID\n\n" + TAIL]


async def test_a_deny_that_left_no_keyboard_delivers_the_words(monkeypatch):
    """R6-6: DELIVERY_FAILED leaves no keyboard; the words are the operator's
    only explanation."""
    t = _Topic(monkeypatch)
    t.channel.post_dm_keyboard = AsyncMock(side_effect=RuntimeError("down"))
    await t.launch(_text("BEFORE"), *_waiting(), _text(TAIL))
    assert [_reason(a) for a in t.session.answers] == [_DENY_DELIVERY_FAILED]
    assert t.finals == ["BEFORE\n\n" + TAIL]


# --- R6-4c: a launch mute for any other reason is reported as at the base ------

@pytest.mark.parametrize("case", ["no_keyboard", "released", "no_deny",
                                  "no_result"])
async def test_a_launch_mute_for_any_other_reason_is_still_observed(
        monkeypatch, case):
    t = _Topic(monkeypatch)
    if case == "no_keyboard":
        t.channel.post_dm_keyboard = AsyncMock(side_effect=RuntimeError("down"))
        script, expected = _waiting(), LAUNCH_NO_VISIBLE_OUTPUT
    elif case == "released":
        script = [*_waiting(), t.approve(), _call("ok-1"), _Ask("ok-1"),
                  _result("ok-1", error=False)]
        expected = LAUNCH_NO_VISIBLE_OUTPUT
    elif case == "no_deny":
        script, expected = [_call("x-1"), _result("x-1", error=False)], \
            LAUNCH_NO_VISIBLE_OUTPUT
    else:
        # The stream ended with no ResultMessage after the cut: a cut-off turn
        # keeps its own reason.
        script, expected = [*_waiting(), NO_RESULT], LAUNCH_MISSING_RESULT
    await t.launch(*script)
    assert t.drv.launch_turn_incomplete(t.rec.id) == expected


# --- R6-7: the cut counts exactly what the stream folds -------------------------

async def test_the_cut_counts_what_the_stream_folds(monkeypatch):
    """A sub-agent's fault frame is never folded and the output cap freezes the
    stream: the cut index follows the same rule, so the kept prefix is exactly
    what was shown before the call."""
    import specialist_limits
    monkeypatch.setattr(specialist_limits, "_MAX_OUTPUT_CHARS", 20)
    fault = AssistantMessage(content=[TextBlock(text="API Error: overloaded")],
                             model="sonnet", parent_tool_use_id="sub-1",
                             error="overloaded")
    t = _Topic(monkeypatch)
    await t.launch(_text("B" * 15), fault, *_waiting(), _text("X" * 30))
    assert t.emits == ["B" * 15] and t.finals == ["B" * 15]

    t2 = _Topic(monkeypatch, rec_id="e2")
    await t2.launch(_text("B" * 25), *_waiting(), _text(TAIL))
    frozen = "B" * 20 + " … [truncated]"
    assert t2.emits == [frozen] and t2.finals == [frozen]


# --- R6-8 / R6-14: a resumed or rebuilt session gets its own carrier -----------

@pytest.mark.parametrize("reopen", ["resume", "open_fresh"])
async def test_a_reopened_session_is_cut_and_the_old_clients_hook_lands_nowhere(
        monkeypatch, reopen):
    import tools
    t = _Topic(monkeypatch)
    monkeypatch.setattr(tools, "build_engagement_resume_options",
                        lambda *_a, **_k: ClaudeAgentOptions(model="sonnet"))
    monkeypatch.setattr(type(t.drv), "_persist_applied_profiles",
                        AsyncMock(return_value=None))
    await t.launch(_text("Hi."))
    old = t.session.clients[-1]
    t.drv._clients.pop(t.rec.id)            # the old session is gone
    if reopen == "resume":
        await t.drv.resume(t.rec, "eng-sid")
    else:
        await t.drv.open_fresh(t.rec)
    assert t.session.clients[-1] is not old

    async def _old_clients_hook():
        # R6-14: the old client's hook, for a call of the old session that was
        # interrupted, answers during the new session's turn.
        task = asyncio.get_running_loop().create_task(
            t.hook({"tool_name": TOOL, "tool_input": {"amount": 99}},
                   "old-1", {}), context=old.ctx.copy())
        t.session.answers.append(await task)

    await t.follow_up(_text("BEFORE"), _Do(_old_clients_hook), _call("old-1"),
                      _result("old-1"), _text("STILL"), *_waiting(),
                      _text(TAIL))
    assert [_reason(a) for a in t.session.answers] == [_DENY_POSTED, _DENY_POSTED]
    assert t.finals == ["Hi.", "BEFORE\n\nSTILL"]


# --- R6-10: a record with no turn running, or never folded, changes nothing ---

async def test_a_stale_write_changes_nothing(monkeypatch):
    t = _Topic(monkeypatch)
    await t.launch(_text("Hi."))
    client = t.session.clients[-1]
    # between turns: the holder is idle and the write is dropped
    task = asyncio.get_running_loop().create_task(
        t.hook({"tool_name": TOOL, "tool_input": dict(ARGS)}, "late-1", {}),
        context=client.ctx.copy())
    assert _deny_reason(await task) == _DENY_POSTED
    # during the next turn, a record for a call this turn never folds
    await t.follow_up(_text("BEFORE"), _Ask("never-folded"), _text(TAIL))
    assert t.finals == ["Hi.", "BEFORE\n\n" + TAIL]


# --- R6-11: a fresh plugin job's batch turn -------------------------------------

async def test_a_fresh_jobs_batch_turn_is_cut(monkeypatch):
    import background_jobs
    t = _Topic(monkeypatch, kind="plugin")
    monkeypatch.setattr("drivers.in_casa_driver._is_fresh_job", lambda _r: True)
    monkeypatch.setattr(type(t.drv), "_reset_conversation",
                        AsyncMock(return_value=None))
    monkeypatch.setattr(background_jobs, "job_brief", lambda _r: "BRIEF")
    t.rec.origin["job"] = {}
    await t.launch(_text("Started."))
    await t.follow_up(_text("BEFORE"), *_waiting(), _text(TAIL), batch=2)
    assert t.finals == ["Started.", "BEFORE"]


# --- R6-12: two engagements of one specialist at once ---------------------------

async def test_a_record_reaches_only_its_own_engagements_topic(monkeypatch):
    a = _Topic(monkeypatch, rec_id="e-a")
    b = _Topic(monkeypatch, rec_id="e-b")
    # one shared session factory so both clients exist side by side
    monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient",
                        a.session.client_cls)
    b.drv, b.session = a.drv, a.session
    gate = asyncio.Event()

    async def _hold():
        await gate.wait()

    a.session.scripts.append([_text("A-BEFORE"), *_waiting("a-1"), _Do(_hold),
                              _text("A-TAIL")])
    a.session.scripts.append([_text("B-WORDS"), _call("b-1"),
                              _result("b-1", error=False), _text("B-TAIL")])
    opts = ClaudeAgentOptions(model="sonnet")
    first = asyncio.create_task(a.drv.start(a.rec, prompt="go", options=opts))
    await asyncio.sleep(0)
    for _ in range(50):
        if a.session.answers:
            break
        await asyncio.sleep(0.01)
    second = asyncio.create_task(a.drv.start(b.rec, prompt="go", options=opts))
    await asyncio.wait_for(second, 10)
    gate.set()
    await asyncio.wait_for(first, 10)
    assert [_reason(x) for x in a.session.answers] == [_DENY_POSTED]
    finals = [str(c.args[0]) for c in a.handle.finalize.await_args_list]
    assert sorted(finals) == ["A-BEFORE", "B-WORDS\n\nB-TAIL"]


# --- R6-13: the hook finishing before the call is even folded ------------------

async def test_a_hook_that_returns_before_the_call_is_folded_keeps_the_prefix(
        monkeypatch):
    """The SDK can run the hook before the consumer has folded the message
    carrying the text and the call: the record is read only at the result."""
    t = _Topic(monkeypatch)
    both = AssistantMessage(content=[TextBlock(text="BEFORE"), ToolUseBlock(
        id="deny-1", name=TOOL, input=dict(ARGS))], model="sonnet")
    await t.launch(_Ask("deny-1"), both, _result("deny-1"), _text(TAIL))
    assert [_reason(a) for a in t.session.answers] == [_DENY_POSTED]
    assert t.emits == ["BEFORE"] and t.finals == ["BEFORE"]


# --- R6-9 lives in test_pending_approval_pins.py (the ambient scope pin) -------


@pytest.mark.parametrize("when", ["earlier_turn", "between_turns"])
async def test_a_record_from_before_this_turn_never_cuts_it(monkeypatch, when):
    """The holder's record is the RUNNING turn's: one written by an earlier
    turn's hook, or between turns, is gone when the next turn starts — even if
    that turn's stream then carries a result for the same call id."""
    t = _Topic(monkeypatch)
    if when == "earlier_turn":
        await t.launch(_text("Hi."), _call("orphan-1"), _Ask("orphan-1"))
    else:
        await t.launch(_text("Hi."))
        task = asyncio.get_running_loop().create_task(
            t.hook({"tool_name": TOOL, "tool_input": dict(ARGS)}, "orphan-1", {}),
            context=t.session.clients[-1].ctx.copy())
        t.session.answers.append(await task)
    await t.follow_up(_text("BEFORE"), _result("orphan-1"), _text(TAIL))
    assert [_reason(a) for a in t.session.answers] == [_DENY_POSTED]
    assert t.finals == ["Hi.", "BEFORE\n\n" + TAIL]
