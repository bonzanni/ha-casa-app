"""#1207 arm 6 under ruling #1252: the agent log of a job (a specialist's, or a
plugin job's) whose protected call is waiting on the operator's approval shows
nothing the model wrote after that call; the words before it are logged. A
consume of the same grant releases the held words at once; a turn that ends
with the cut in place never logs them.

It drives the REAL ``InCasaDriver`` (``open``, then the job's brief through
``enqueue_brief`` and later writes through ``submit_casa``) with a scripted SDK
client and the REAL ``authz_grants.make_resident_authz_hook``. The hook runs
where production runs it: in a task created from a copy of the context the
client was ENTERED in — ``engagement_var`` bound to the record and ``cid_var``
to the session's turn holder, as ``InCasaDriver.open`` binds them — never in
the delivery worker or the reader, where no per-turn variable can reach it.
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
from agent_log_double import RecordingLogs
from test_in_casa_driver import _make_record
from test_pending_approval_output import ARGS, PROTECTED, TOOL, _Channel

try:
    from tests.test_job_session_driver import until
    from tests.turn_scripted_session import TurnScriptedSession
except ImportError:  # pragma: no cover — direct-path collection
    from test_job_session_driver import until
    from turn_scripted_session import TurnScriptedSession

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


_OPEN: list = []


@pytest.fixture(autouse=True)
async def _close_sessions():
    _OPEN.clear()
    yield
    for drv, rec in _OPEN:
        await drv.cancel(rec)
    _OPEN.clear()


def _turn_counter():
    ends: list = []

    def on_turn_end(rec, result, api_error):
        ends.append(rec.id)
    return ends, on_turn_end


class _EntryClient(TurnScriptedSession):
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

    logs = RecordingLogs()
    ends, on_turn_end = _turn_counter()
    drv = InCasaDriver(agent_logs=logs, on_turn_end=on_turn_end)
    rec = _make_record(kind=kind, role_or_type=ROLE,
                       origin={"chat_id": str(OPERATOR), "user_id": OPERATOR})
    await drv.open(rec, ClaudeAgentOptions(model="sonnet"))
    _OPEN.append((drv, rec))
    drv.enqueue_brief(rec, "go")
    await until(lambda: len(ends) == 1, 10)

    client = _EntryClient.instances[-1]
    assert client.entry_engagement is rec
    assert isinstance(client.entry_holder, _EngagementTurn)
    # The protected call was denied POSTED: one keyboard is up.
    assert len(answers) == 1
    assert _deny_reason(answers[0]) == _DENY_POSTED
    assert len(channel.posts) == 1
    # The log shows only what was written before the call.
    assert logs.words() == ["BEFORE"]


# ---------------------------------------------------------------------------
# The rest of arm 6, on the same production shape: every hook runs in a task
# created from a copy of its client's __aenter__ context.
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock  # noqa: E402

from authz_grants import (  # noqa: E402
    GrantKey, canonical_args_hash, _DENY_DELIVERY_FAILED, _DENY_PENDING,
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


class _Session:
    """One scripted client per open/resume, each with its own entry context;
    each turn (one written message) runs the next script from one shared
    queue."""

    def __init__(self, hook) -> None:
        self.hook = hook
        self.scripts: list = []
        self.clients: list = []
        self.answers: list = []
        session = self

        class _Client(TurnScriptedSession):
            def __init__(self, options) -> None:
                self.options = options
                session.clients.append(self)

            async def __aenter__(self):
                self.ctx = contextvars.copy_context()
                return self

            async def __aexit__(self, *exc):
                return None

            async def close(self):
                return None

            async def receive_response(self):
                for item in session.scripts.pop(0):
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
        self.logs = RecordingLogs()
        self.ends, on_turn_end = _turn_counter()
        self.drv = InCasaDriver(agent_logs=self.logs, on_turn_end=on_turn_end)
        self.rec = _make_record(id=rec_id, kind=kind, role_or_type=ROLE,
                                task=rec_id,
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

    async def _turn(self, write) -> None:
        before = len(self.ends)
        write()
        await until(lambda: len(self.ends) == before + 1, 10)

    async def launch(self, *script) -> None:
        """Open the job's session and run its brief as the first turn."""
        self.session.scripts.append(list(script))
        await self.drv.open(self.rec, ClaudeAgentOptions(model="sonnet"))
        _OPEN.append((self.drv, self.rec))
        await self._turn(lambda: self.drv.enqueue_brief(self.rec, "go"))

    async def follow_up(self, *script) -> None:
        """One more message written into the running session, as its own
        turn."""
        self.session.scripts.append(list(script))
        await self._turn(lambda: self.drv.submit_casa(self.rec, "next"))

    @property
    def words(self) -> list[str]:
        """The words logged, without the step lines."""
        return self.logs.words()


def _waiting(call_id="deny-1"):
    return [_call(call_id), _Ask(call_id), _result(call_id)]


# --- the launch: nothing after the waiting call, and it is not reported dead --

@pytest.mark.parametrize("kind", ["specialist", "plugin"])
@pytest.mark.parametrize("tail", [True, False])
async def test_a_launch_whose_only_words_are_withheld_shows_nothing_and_lives(
        monkeypatch, kind, tail):
    """R6-1 with no prefix, R6-4 and R6-4b: no words logged — and the
    session stays open, the keyboard up. tail=False is the mute launch after a
    POSTED deny."""
    t = _Topic(monkeypatch, kind)
    await t.launch(*_waiting(), *([_text(TAIL)] if tail else []))
    assert [_reason(a) for a in t.session.answers] == [_DENY_POSTED]
    assert len(t.channel.posts) == 1
    assert t.words == []
    assert t.drv.is_alive(t.rec)


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
    assert t.words == ["BEFORE"]


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
    assert t.words == ["Hi.", "BEFORE", "Done the first."]


async def test_a_consume_of_the_same_grant_releases_the_words(monkeypatch):
    """R6-5: the operator approved during the turn and the model ran the call
    again; nothing is waiting any more."""
    t = _Topic(monkeypatch)
    await t.launch(_text("BEFORE"), *_waiting(), _text("MID"), t.approve(),
                   _call("ok-1"), _Ask("ok-1"), _result("ok-1", error=False),
                   _text(TAIL))
    assert [_reason(a) for a in t.session.answers] == [_DENY_POSTED, "allow"]
    assert t.words == ["BEFORE", "MID", TAIL]


async def test_a_cleared_cut_logs_the_held_words_at_once(monkeypatch):
    """The words held behind a waiting call are logged as soon as the consume
    of its grant is read at the call's result — not later, at the next words
    or the turn's end."""
    t = _Topic(monkeypatch)
    seen: list = []

    async def _snapshot():
        seen.append(list(t.words))

    await t.launch(_text("BEFORE"), *_waiting(), _text("MID"), t.approve(),
                   _call("ok-1"), _Ask("ok-1"), _result("ok-1", error=False),
                   _Do(_snapshot), _call("ok-2"))
    assert seen == [["BEFORE", "MID"]]


async def test_a_turn_that_ends_under_the_cut_never_logs_the_held_words(
        monkeypatch):
    """The words after the waiting call are dropped at the turn's end; a
    later turn's words are logged as usual."""
    t = _Topic(monkeypatch)
    await t.launch(_text("BEFORE"), *_waiting(), _text("HELD-1"), _text("HELD-2"))
    assert t.words == ["BEFORE"]
    await t.follow_up(_text("NEXT"))
    assert t.words == ["BEFORE", "NEXT"]


async def test_a_deny_that_left_no_keyboard_delivers_the_words(monkeypatch):
    """R6-6: DELIVERY_FAILED leaves no keyboard; the words are the operator's
    only explanation."""
    t = _Topic(monkeypatch)
    t.channel.post_dm_keyboard = AsyncMock(side_effect=RuntimeError("down"))
    await t.launch(_text("BEFORE"), *_waiting(), _text(TAIL))
    assert [_reason(a) for a in t.session.answers] == [_DENY_DELIVERY_FAILED]
    assert t.words == ["BEFORE", TAIL]


# --- R6-7: the cut counts exactly what the reader folds -------------------------

async def test_the_cut_counts_what_the_stream_folds(monkeypatch):
    """A sub-agent's fault frame is never folded: the cut index follows the
    same rule, so the kept prefix is exactly what was logged before the
    call."""
    fault = AssistantMessage(content=[TextBlock(text="API Error: overloaded")],
                             model="sonnet", parent_tool_use_id="sub-1",
                             error="overloaded")
    t = _Topic(monkeypatch)
    await t.launch(_text("B" * 15), fault, *_waiting(), _text("X" * 30))
    assert t.words == ["B" * 15]


async def test_the_kept_prefix_is_finalized_as_it_was_shown(monkeypatch):
    t = _Topic(monkeypatch)
    await t.launch(_text("B" * 25), *_waiting(), _text(TAIL))
    assert t.words == ["B" * 25]


# --- R6-8 / R6-14: a resumed or rebuilt session gets its own carrier -----------

@pytest.mark.parametrize("reopen", ["resume"])
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
    await t.drv.cancel(t.rec)               # the old session is gone
    await t.drv.resume(t.rec, "eng-sid")
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
    assert t.words == ["Hi.", "BEFORE", "STILL"]


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
    assert t.words == ["Hi.", "BEFORE", TAIL]


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
    for rec in (a.rec, b.rec):
        _OPEN.append((a.drv, rec))
    await a.drv.open(a.rec, opts)
    a.drv.enqueue_brief(a.rec, "go")
    await until(lambda: a.session.answers, 10)
    await a.drv.open(b.rec, opts)
    a.drv.enqueue_brief(b.rec, "go")
    await until(lambda: a.ends == ["e-b"], 10)
    gate.set()
    await until(lambda: sorted(a.ends) == ["e-a", "e-b"], 10)
    assert [_reason(x) for x in a.session.answers] == [_DENY_POSTED]
    by_run: dict = {}
    for _key, run, text in a.logs.lines:
        if not text.startswith("🔧"):
            by_run.setdefault(run, []).append(text)
    assert by_run == {"e-a": ["A-BEFORE"], "e-b": ["B-WORDS", "B-TAIL"]}


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
    assert t.words == ["BEFORE"]


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
    assert t.words == ["Hi.", "BEFORE", TAIL]
