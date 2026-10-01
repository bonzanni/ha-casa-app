"""#1141 red case — INV-ENG-020: an ``in_casa`` engagement turn that stops at
its turn limit stays open, and the operator is told once in its topic.

Specified by Astra (rounds-C13/redcase-specify-astra.md, run 2026-09-30); each
case fails at the base on an assertion, never an import. Real code under test:
the real ``InCasaDriver`` turn, the real anchored launch owner
(``tools._own_in_casa_launch``, reached through ``engage_executor`` or directly
for a job), and the real follow-up owner (``TelegramChannel._deliver_turn_bg`` →
``_report_incomplete_turn``) behind the production seam
(``casa_core.read_followup_incomplete``).

Nothing here imports a symbol the change adds: the expected line, the result
frame and the observation's fields are written out, so a pre-fix run fails on
the behaviour (a launch death, a silent topic), not on a name.

No ``asyncio.sleep`` is patched anywhere; a wedged counterparty is an
``asyncio.Event`` that is never set, and the production bounds are shortened
instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest

try:
    from tests.test_in_casa_launch_terminal_artifact import _Probe, _build, _launch
    from tests.test_in_casa_inbound_admission import _drain, _mk_channel
    from tests.test_launch_death_reporter import _drain_owner
except ImportError:  # pragma: no cover — direct-path collection
    from test_in_casa_launch_terminal_artifact import _Probe, _build, _launch
    from test_in_casa_inbound_admission import _drain, _mk_channel
    from test_launch_death_reporter import _drain_owner

pytestmark = [pytest.mark.asyncio]

LINE = (
    "I hit my step limit before finishing — say 'continue' and I'll pick up "
    "where I stopped."
)
"""ruling-1141's line, C11's ``agent._LIMIT_CONTINUE`` — written out so a
reworded constant fails a test; the equality is pinned in RC1."""

TURNS = 11
SID = "sid-mt"
TEXT = "Partial work."
_WATCHDOG_S = 5.0
_SHORT_S = 0.05


# --- frames -------------------------------------------------------------------

def _limit_result(**over):
    """The CLI-2.1.273 shape of a turn-limit stop: is_error=True, num_turns,
    result=None, stop_reason="tool_use". Built field by field so every
    attribute the turn reads is set explicitly."""
    from claude_agent_sdk import ResultMessage
    fields = dict(
        subtype="error_max_turns", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=TURNS, session_id=SID, total_cost_usd=0.0,
        usage={}, result=None, stop_reason="tool_use", parent_tool_use_id=None,
    )
    fields.update(over)
    rm = ResultMessage.__new__(ResultMessage)
    for k, v in fields.items():
        setattr(rm, k, v)
    return rm


def _tool_frames():
    from claude_agent_sdk import (
        AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage,
    )
    use = ToolUseBlock(id="t-1", name="Read", input={"file_path": "/x"})
    res = ToolResultBlock(tool_use_id="t-1", content="ok", is_error=False)
    return [AssistantMessage(content=[use], model="claude-sonnet-4-6"),
            UserMessage(content=[res])]


def _text_frame():
    from claude_agent_sdk import AssistantMessage, TextBlock
    return AssistantMessage(content=[TextBlock(text=TEXT)],
                            model="claude-sonnet-4-6")


def _frames(shape, **result_over):
    """``tool`` = tool-only; ``text`` = tools then text."""
    frames = _tool_frames()
    if shape == "text":
        frames.append(_text_frame())
    return frames + [_limit_result(**result_over)]


def _launch_client(shape, **result_over):
    """An SDK client class for the launch harness: one turn, ending in the
    limit result."""
    class _Client:
        queries = 0
        results = 0

        def __init__(self, options):
            self.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def query(self, prompt):
            type(self).queries += 1

        async def receive_response(self):
            for f in _frames(shape, **result_over):
                if getattr(f, "subtype", None) == "error_max_turns":
                    type(self).results += 1
                yield f

        async def close(self):
            pass
    return _Client


# --- shared assertions ----------------------------------------------------------

def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def _one_limit_warning(caplog, rec, *, line):
    """Exactly ONE WARNING in total, and it is the limit's: the engagement,
    the role, the turn count and the telling's outcome."""
    warns = _warnings(caplog)
    assert len(warns) == 1, [w.getMessage() for w in warns]
    msg = warns[0].getMessage()
    assert rec.id[:8] in msg, msg
    assert rec.role_or_type in msg, msg
    assert str(TURNS) in msg, msg
    assert f"line={line}" in msg, msg
    return msg


def _obs_fields(obs):
    """The consumed observation keeps the turn's own reason beside the limit
    fact. Read by attribute so a pre-fix plain string reads (None, None)."""
    return getattr(obs, "reason", None), getattr(obs, "turns", None)


# --- the launch rig ---------------------------------------------------------------

class _Seen:
    """Counted spies on everything a launch death or an engager telling does,
    wrapping the real functions (their answers are not replaced)."""

    def __init__(self):
        self.obs: list = []
        self.tell = 0
        self.send = 0
        self.abort = 0


def _spy_launch(monkeypatch, driver, probe, *, delivery=None):
    import tools
    seen = _Seen()
    inner_obs = driver.launch_turn_incomplete

    def _obs(engagement_id):
        value = inner_obs(engagement_id)
        seen.obs.append(value)
        return value
    monkeypatch.setattr(driver, "launch_turn_incomplete", _obs)

    real_tell = tools._tell_engager_launch_outcome

    async def _tell(*a, **k):
        seen.tell += 1
        return await real_tell(*a, **k)
    monkeypatch.setattr(tools, "_tell_engager_launch_outcome", _tell)

    real_send = tools.send_engagement_outcome

    async def _send(*a, **k):
        seen.send += 1
        return await real_send(*a, **k)
    monkeypatch.setattr(tools, "send_engagement_outcome", _send)

    real_abort = tools._abort_launch_on_cancel

    def _abort(*a, **k):
        seen.abort += 1
        return real_abort(*a, **k)
    monkeypatch.setattr(tools, "_abort_launch_on_cancel", _abort)

    inner_factory = driver._topic_stream_factory

    def _factory(topic_id):
        handle = inner_factory(topic_id)
        real_finalize = handle.finalize

        async def _finalize(text):
            probe.events.append("finalize")
            await real_finalize(text)
            return delivery
        handle.finalize = _finalize
        return handle
    driver._topic_stream_factory = _factory
    return seen, inner_obs


def _assert_launch_live(rec, probe, seen, driver, client_cls):
    assert client_cls.queries == 1 and client_cls.results == 1
    assert rec.status == "active", rec.status
    assert probe.transition_calls == [], probe.transition_calls
    assert probe.mark_error_calls == []
    assert probe.driver_cancel_calls == 0
    assert "topic_close" not in probe.events, probe.events
    assert [e for e in probe.events if e.startswith("paint")] == [], probe.events
    assert seen.tell == 0 and seen.send == 0 and seen.abort == 0
    assert driver.is_alive(rec)
    assert rec.sdk_session_id == SID


def _not_delivered():
    from channels import DeliveryOutcome
    return DeliveryOutcome.NOT_DELIVERED


@pytest.mark.parametrize("shape,refused,reason", [
    ("tool", False, "no_visible_output"),
    ("text", False, ""),
    ("text", True, "text_not_delivered"),
], ids=["RC1-tool-only", "RC2-with-text", "RC3-text-not-delivered"])
async def test_launch(tmp_path, monkeypatch, caplog, shape, refused, reason):
    """RC1-RC3: a configurator launch turn that stops at its limit stays open;
    whatever it posted stands, then ONE line, ONE WARNING, no death, nothing
    to the engager."""
    import agent
    assert agent._LIMIT_CONTINUE == LINE
    probe = _Probe()
    client_cls = _launch_client(shape)
    engage_executor, registry, channel, driver = _build(
        tmp_path, monkeypatch, probe, client_cls)
    seen, inner_obs = _spy_launch(
        monkeypatch, driver, probe,
        delivery=_not_delivered() if refused else None)
    caplog.set_level(logging.DEBUG)
    caplog.clear()

    envelope = await _launch(engage_executor)
    assert json.loads(envelope["content"][0]["text"]).get("status") == "pending"
    await asyncio.wait_for(_drain_owner(), _WATCHDOG_S)

    rec = registry.get(next(iter(registry._records)))
    _assert_launch_live(rec, probe, seen, driver, client_cls)
    assert probe.notice_texts == [LINE], probe.notice_texts
    if shape == "text":
        assert probe.events.count("finalize") == 1
        assert probe.events.index("finalize") < probe.events.index("notice")
    assert len(seen.obs) == 1
    assert _obs_fields(seen.obs[0]) == (reason, TURNS)
    assert inner_obs(rec.id) == ""
    _one_limit_warning(caplog, rec, line="posted")


async def _job_rig(tmp_path, monkeypatch, probe, kind, shape="tool"):
    """A job engagement opened on the real driver, ready for its launch turn."""
    from claude_agent_sdk import ClaudeAgentOptions
    import background_jobs as jobs
    client_cls = _launch_client(shape)
    _engage, registry, channel, driver = _build(
        tmp_path, monkeypatch, probe, client_cls)
    decl = jobs.JobDecl("sample:work", "sample", "work", "sample:work",
                        "Process rows", None, None, 10)
    origin = {"role": "assistant", "channel": "telegram", "chat_id": "c1",
              "user_id": 77, "job": jobs.initial_job_state(decl)}
    if kind == "plugin":
        origin["plugin_job"] = {"plugin": "sample", "model": "claude-sonnet-4-6"}
    rec = await registry.create(kind, "worker", "in_casa", "Process rows",
                                origin, topic_id=42)
    await driver.open(rec, options=ClaudeAgentOptions(model="claude-sonnet-4-6"))
    return registry, channel, driver, rec, client_cls


@pytest.mark.parametrize("kind", ["specialist", "plugin"])
async def test_job_launch(tmp_path, monkeypatch, caplog, kind):
    """RC4: a job's launch turn is a launch, not a batch — it stops at its
    limit, stays open, gets the line and the WARNING, then the job's
    existing hand-off runs exactly once."""
    from unittest.mock import AsyncMock
    import background_jobs as jobs
    import tools
    probe = _Probe()
    registry, channel, driver, rec, client_cls = await _job_rig(
        tmp_path, monkeypatch, probe, kind)
    seen, inner_obs = _spy_launch(monkeypatch, driver, probe)
    after = AsyncMock()
    monkeypatch.setattr(jobs, "job_after_turn", after)
    caplog.set_level(logging.DEBUG)
    caplog.clear()

    await asyncio.wait_for(tools._own_in_casa_launch(
        driver, rec, "Acknowledge the job", channel, 42,
        started=[], handle_box=[]), _WATCHDOG_S)
    await asyncio.wait_for(_drain_owner(), _WATCHDOG_S)

    _assert_launch_live(rec, probe, seen, driver, client_cls)
    assert probe.notice_texts == [LINE], probe.notice_texts
    assert after.await_count == 1
    assert after.await_args.args == (rec, channel)
    assert _obs_fields(seen.obs[0]) == ("no_visible_output", TURNS)
    _one_limit_warning(caplog, rec, line="posted")


# --- the follow-up rig --------------------------------------------------------------

class _EventStream:
    def __init__(self, events, delivery):
        self._events = events
        self._delivery = delivery

    async def emit(self, text):
        pass

    async def finalize(self, text):
        self._events.append("finalize")
        return self._delivery


class _FollowUp:
    """Real TelegramChannel + registry + InCasaDriver; the production seam;
    spies that record, never answer."""

    def __init__(self):
        self.events: list = []
        self.obs: list = []
        self.results: list = []
        self.settled: list = []

    def notices(self):
        return [e[1] for e in self.events if isinstance(e, tuple) and e[0] == "notice"]


def _job_state():
    import background_jobs as jobs
    decl = jobs.JobDecl("sample:work", "sample", "work", "sample:work",
                        "Process rows", None, None, 10)
    return jobs.initial_job_state(decl)


async def _followup_rig(tmp_path, fake_telegram_bot, monkeypatch, frames, *,
                        job=False, refused=False, terminal_first=False):
    from unittest.mock import AsyncMock
    import background_jobs as jobs
    import casa_core
    try:
        from tests.test_in_casa_inbound_admission import _ScriptedClient
    except ImportError:  # pragma: no cover
        from test_in_casa_inbound_admission import _ScriptedClient
    f = _FollowUp()
    f.client = _ScriptedClient(scripts=[frames])
    ch, reg, rec, drv = await _mk_channel(tmp_path, fake_telegram_bot, f.client)
    f.ch, f.reg, f.rec, f.drv = ch, reg, rec, drv
    ch._driver_turn_incomplete = (
        lambda r, t: casa_core.read_followup_incomplete(drv, r, t))
    if job:
        rec.origin["job"] = _job_state()
    inner = drv.followup_turn_incomplete

    def _obs(engagement_id, token):
        value = inner(engagement_id, token)
        f.obs.append(value)
        return value
    monkeypatch.setattr(drv, "followup_turn_incomplete", _obs)
    drv._topic_stream_factory = lambda tid: _EventStream(
        f.events, _not_delivered() if refused else None)

    async def _notice(r, text):
        f.events.append(("notice", text))
    ch._post_engagement_notice = AsyncMock(side_effect=_notice)
    real_report = ch._report_incomplete_turn

    async def _report(r, token, **kw):
        if terminal_first:
            await reg.try_transition_terminal(r.id, "completed", strict=True)
            f.settled.append(await reg.settled_terminal_state(r.id))
        value = await real_report(r, token, **kw)
        f.results.append(value)
        return value
    ch._report_incomplete_turn = _report
    f.after = AsyncMock()
    monkeypatch.setattr(jobs, "job_after_turn", f.after)
    return f


async def _operator_turn(f, text="please carry on"):
    token = f.ch._driver_admit_inbound(f.rec, text)
    task = asyncio.create_task(f.ch._deliver_turn_bg(
        f.rec, text, tg_message_id=7, inbound_token=token))
    f.ch._turn_tasks.add(task)
    task.add_done_callback(f.ch._turn_tasks.discard)
    await asyncio.wait_for(_drain(f.ch), _WATCHDOG_S)


async def _system_turn(f, text="continue the configuration"):
    assert await f.ch.deliver_system_turn(f.rec, text) is True
    await asyncio.wait_for(_drain(f.ch), _WATCHDOG_S)


def _assert_told_once(f, caplog, *, reason, text):
    assert len(f.client.query_prompts) == 1
    assert f.notices() == [LINE], f.events
    if text:
        assert f.events.count("finalize") == 1
        assert f.events.index("finalize") < f.events.index(("notice", LINE))
    assert f.results == [False]
    assert len(f.obs) == 1 and _obs_fields(f.obs[0]) == (reason, TURNS)
    assert f.reg.get(f.rec.id).status == "active"
    _one_limit_warning(caplog, f.rec, line="posted")


@pytest.mark.parametrize("shape,via,job,refused,reason", [
    ("tool", "operator", False, False, ""),
    ("text", "operator", False, False, ""),
    ("tool", "operator", True, False, ""),
    ("tool", "system", False, False, ""),
    ("text", "operator", False, True, "followup_text_not_delivered"),
], ids=["RC5-operator-tool-only", "RC6-operator-with-text",
        "RC7-operator-in-a-job-topic", "RC8-non-job-system-turn",
        "RC9-limit-and-text-not-delivered"])
async def test_followup(tmp_path, fake_telegram_bot, monkeypatch, caplog,
                        shape, via, job, refused, reason):
    """RC5-RC9: every non-batch follow-up that stops at its limit gets ONE
    line after whatever it posted, ONE WARNING, and is never reported cut
    off — an operator turn in a job topic included (the job then goes on)."""
    f = await _followup_rig(tmp_path, fake_telegram_bot, monkeypatch,
                            _frames(shape), job=job, refused=refused)
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    if via == "operator":
        await _operator_turn(f)
    else:
        await _system_turn(f)
    _assert_told_once(f, caplog, reason=reason, text=shape == "text")
    if job:
        assert f.after.await_count == 1
        assert f.after.await_args.args == (f.rec, f.ch)
        assert f.after.await_args.kwargs == {"turn_cut_off": False}


async def test_subtype_alone(tmp_path, fake_telegram_bot, monkeypatch, caplog):
    """RC13: the subtype alone is the limit stop — not is_error, not the stop
    reason, not an empty result."""
    f = await _followup_rig(
        tmp_path, fake_telegram_bot, monkeypatch,
        _frames("tool", is_error=False, stop_reason="end_turn",
                result="nonempty result"))
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    await _operator_turn(f)
    _assert_told_once(f, caplog, reason="", text=False)


async def test_terminal_untold_and_not_delivered(
        tmp_path, fake_telegram_bot, monkeypatch, caplog):
    """RC15: a limit stop whose text was refused, over a record that went
    terminal (untold) before the owner adjudicated: nothing is posted — no
    line, no unconfirmed-outcome notice — one WARNING, not cut off."""
    f = await _followup_rig(tmp_path, fake_telegram_bot, monkeypatch,
                            _frames("text"), refused=True, terminal_first=True)
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    await _operator_turn(f)
    assert f.settled == [("completed", False)]
    assert f.notices() == [], f.events
    assert f.results == [False]
    assert _obs_fields(f.obs[0]) == ("followup_text_not_delivered", TURNS)
    _one_limit_warning(caplog, f.rec, line="terminal")


# --- cancellation, ownership and faults during the launch's telling ---------------

async def _race_entry(entered, owner, registry, rec_id):
    """Wait until the owner ENTERS the limit telling. If it finishes first,
    the base's launch death is the failure: assert the record live (red
    there), and never wait on a telling that does not exist."""
    waiter = asyncio.ensure_future(entered.wait())
    done, _ = await asyncio.wait({waiter, owner}, timeout=_WATCHDOG_S,
                                 return_when=asyncio.FIRST_COMPLETED)
    if waiter not in done:
        waiter.cancel()
        assert registry.get(rec_id).status == "active", registry.get(rec_id).status
        pytest.fail("the launch owner never entered the limit telling")


@pytest.mark.parametrize("where", ["post", "read"],
                         ids=["RC10-cancel-during-the-line",
                              "RC11-cancel-during-the-read"])
async def test_cancel(tmp_path, monkeypatch, caplog, where):
    """RC10/RC11: the owner is cancelled (the graceful stop's shape) while
    it reads the record's state for the line, or posts it. The cancellation
    propagates and never takes the launch's cancellation-abort arm: the
    record stays live and the WARNING is still logged."""
    from unittest.mock import AsyncMock
    import tools
    probe = _Probe()
    client_cls = _launch_client("tool")
    engage_executor, registry, channel, driver = _build(
        tmp_path, monkeypatch, probe, client_cls)
    seen, _inner = _spy_launch(monkeypatch, driver, probe)
    entered, never = asyncio.Event(), asyncio.Event()
    posts: list = []

    async def _notice(rec, text):
        posts.append(text)
        if where == "post" and text == LINE:
            entered.set()
            await never.wait()
    channel._post_engagement_notice = AsyncMock(side_effect=_notice)
    if where == "read":
        async def _blocked(engagement_id):
            entered.set()
            await never.wait()
            return ("", False)
        monkeypatch.setattr(registry, "settled_terminal_state", _blocked)
    caplog.set_level(logging.DEBUG)
    caplog.clear()

    await _launch(engage_executor)
    owner = next(iter(tools._LAUNCH_TURN_TASKS))
    rec_id = next(iter(registry._records))
    try:
        await _race_entry(entered, owner, registry, rec_id)
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        assert owner.cancelled()
        await asyncio.wait_for(_drain_owner(), _WATCHDOG_S)
    finally:
        never.set()
        if not owner.done():
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)

    rec = registry.get(rec_id)
    assert rec.status == "active", rec.status
    assert seen.abort == 0 and seen.tell == 0 and seen.send == 0
    assert probe.transition_calls == [] and probe.mark_error_calls == []
    assert probe.driver_cancel_calls == 0
    assert "topic_close" not in probe.events
    assert posts == ([LINE] if where == "post" else []), posts
    assert len(seen.obs) == 1
    assert rec.sdk_session_id == SID
    _one_limit_warning(caplog, rec, line="cancelled")


async def test_job_telling_holds_ownership(tmp_path, monkeypatch, caplog):
    """RC12 (seam r1, Terra): while a job's launch telling is in flight the
    launch still owns the record — the stall sweep admits NO batch — and once
    it ends, exactly one batch is admitted."""
    from unittest.mock import AsyncMock
    import background_jobs as jobs
    import tools
    probe = _Probe()
    registry, channel, driver, rec, client_cls = await _job_rig(
        tmp_path, monkeypatch, probe, "specialist")
    seen, _inner = _spy_launch(monkeypatch, driver, probe)
    entered, release = asyncio.Event(), asyncio.Event()

    async def _notice(r, text):
        probe.notice_texts.append(text)
        if text == LINE:
            entered.set()
            await release.wait()
    channel._post_engagement_notice = AsyncMock(side_effect=_notice)
    channel._stopping = False
    channel._engagement_driver = driver
    channel._engagement_registry = registry
    channel._turn_tasks = set()
    channel.deliver_system_turn = AsyncMock(return_value=True)
    rec.started_at = time.time() - 10 * jobs._JOB_STALL_S
    caplog.set_level(logging.DEBUG)
    caplog.clear()

    tools._hand_off_launch_turn(driver, rec, "Acknowledge the job", channel,
                                42, None)
    owner = next(iter(tools._LAUNCH_TURN_TASKS))
    try:
        await _race_entry(entered, owner, registry, rec.id)
        assert jobs.turn_owners(rec.id) == 1
        assert registry.launch_in_flight(rec.id)
        await jobs.sweep_jobs(registry, channel)
        assert channel.deliver_system_turn.await_count == 0
    finally:
        release.set()
    await asyncio.wait_for(owner, _WATCHDOG_S)
    await asyncio.wait_for(_drain_owner(), _WATCHDOG_S)

    assert channel.deliver_system_turn.await_count == 1
    assert rec.origin["job"]["started"] == 1
    assert jobs.turn_owners(rec.id) == 0
    assert not registry.launch_in_flight(rec.id)
    assert rec.status == "active"
    assert probe.notice_texts == [LINE]
    _one_limit_warning(caplog, rec, line="posted")


_FAULTS = [(owner, step, mode)
           for owner in ("launch", "followup")
           for step in ("read", "post")
           for mode in ("raises", "never-returns")]


@pytest.mark.parametrize("owner,step,mode", _FAULTS,
                         ids=["RC14-%s-%s-%s" % f for f in _FAULTS])
async def test_telling_fault(tmp_path, fake_telegram_bot, monkeypatch, caplog,
                             owner, step, mode):
    """RC14: the telling's state read or its post fails or never returns.
    The owner still finishes (each await is bounded), the engagement stays
    live, the line is attempted exactly once — or posted anyway after an
    unread state — and the ONE WARNING records what happened. No fallback
    death, no retry, no second WARNING."""
    from unittest.mock import AsyncMock
    import channels.telegram as tg
    import tools
    monkeypatch.setattr(tools, "_TOPIC_OP_TIMEOUT_S", _SHORT_S)
    monkeypatch.setattr(tg, "_SETTLED_STATUS_TIMEOUT_S", _SHORT_S)
    monkeypatch.setattr(tg, "_TURN_INCOMPLETE_NOTICE_TIMEOUT_S", _SHORT_S)
    never = asyncio.Event()
    posts: list = []

    async def _notice(r, text):
        posts.append(text)
        if step == "post" and text == LINE:
            if mode == "raises":
                raise RuntimeError("topic refused the post")
            await never.wait()

    async def _state(engagement_id):
        if mode == "raises":
            raise RuntimeError("registry unavailable")
        await never.wait()
        return ("", False)

    caplog.set_level(logging.DEBUG)
    try:
        if owner == "launch":
            probe = _Probe()
            client_cls = _launch_client("tool")
            engage_executor, registry, channel, driver = _build(
                tmp_path, monkeypatch, probe, client_cls)
            seen, _inner = _spy_launch(monkeypatch, driver, probe)
            channel._post_engagement_notice = AsyncMock(side_effect=_notice)
            if step == "read":
                monkeypatch.setattr(registry, "settled_terminal_state", _state)
            caplog.clear()
            await _launch(engage_executor)
            await asyncio.wait_for(_drain_owner(), _WATCHDOG_S)
            rec = registry.get(next(iter(registry._records)))
            _assert_launch_live(rec, probe, seen, driver, client_cls)
        else:
            f = await _followup_rig(tmp_path, fake_telegram_bot, monkeypatch,
                                    _frames("tool"))
            f.ch._post_engagement_notice = AsyncMock(side_effect=_notice)
            if step == "read":
                monkeypatch.setattr(f.reg, "settled_terminal_state", _state)
            caplog.clear()
            await _operator_turn(f)
            rec = f.rec
            assert f.results == [False]
            assert f.reg.get(rec.id).status == "active"
    finally:
        never.set()

    assert posts == [LINE], posts
    if step == "post":
        _one_limit_warning(caplog, rec, line="failed")
    else:
        msg = _one_limit_warning(caplog, rec, line="posted")
        assert "state=unread" in msg, msg
