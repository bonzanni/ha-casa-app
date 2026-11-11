"""The agent log's producers: what each one writes, under which key, and the
approval cut (#1207) in a delegated run.

The sink is ``agent_log_double.RecordingLogs`` — synchronous like the real
one. The reader's lines (a job's words and steps) and the launch, progress,
resume and launch-death lines are pinned beside their producers' own tests
(``test_in_casa_driver``, ``test_pending_approval_topic``,
``test_start_job``, ``test_job_tools``, ``test_job_turn_guard``,
``test_launch_death_reporter``). Here: a delegated specialist or configurator
run, the finalize funnel's end line, the configurator's step line, the
channel's check and the periodic pass."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock

import tools as tools_mod
from agent_log_double import RecordingLogs
from agent_logs import LogKey
from authz_grants import _DENY_POSTED

from test_authz_hook import _FakeChannel, _OriginCtx, _deny_reason, _origin
from test_delegate_to_agent import _specialist_cfg
from scripted_delegate import (  # noqa: F401
    ARGS, PROTECTED, TOOL, _Scripted, _Step, _call, _result, _text,
)
from telegram_channel_double import make_transport

pytestmark = pytest.mark.asyncio

FINANCE = LogKey("agent", "finance")


@pytest.fixture
def logs(monkeypatch):
    sink = RecordingLogs()
    monkeypatch.setattr(tools_mod, "_agent_logs", sink)
    return sink


def _read(call_id):
    return AssistantMessage(content=[ToolUseBlock(id=call_id, name="Read", input={})],
                            model="sonnet")


def _sub_step(call_id):
    msg = AssistantMessage(content=[ToolUseBlock(id=call_id, name="Bash", input={})],
                           model="sonnet")
    object.__setattr__(msg, "parent_tool_use_id", "sub-1")
    return msg


OPERATOR = 42
BOT = 111          # the bot the operator wrote to


@pytest.fixture
def delegated(monkeypatch):
    """The REAL bounded runner and ``_run_delegated_agent`` on a real
    specialist config, under a scripted specialist client, with a real
    authorization hook for the specialist's protected tool; the run is the
    operator's delegation to ``finance`` from his private chat."""
    from unittest.mock import patch

    import verdict_broker
    from authz_grants import (
        AuthzDeps, ChallengeCoordinator, GrantStore, make_resident_authz_hook,
    )
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    cfg = _specialist_cfg("finance")
    cfg.kind = "specialist"
    monkeypatch.setattr(tools_mod, "_agent_role_map", {"finance": cfg}, raising=False)
    keyboard = _FakeChannel()
    # The challenge is posted through the endpoint of the identity's bot: the
    # run's route names bot 111, which the transport resolves to this endpoint.
    keyboard.default_agent, keyboard.bot_id, keyboard.has_bot_id = "assistant", BOT, True
    keyboard.chat_id = OPERATOR
    manager = make_transport(keyboard)
    monkeypatch.setattr(tools_mod, "_channel_manager", manager, raising=False)
    transport = manager.get("telegram")
    grants, coord = GrantStore(), ChallengeCoordinator()
    env = SimpleNamespace(cfg=cfg, keyboard=keyboard, grants=grants, answers=[])
    env.hook = make_resident_authz_hook(
        "finance", PROTECTED,
        lambda: AuthzDeps(channel=transport, grants=grants, challenges=coord))
    with patch("tools.ClaudeSDKClient", _Scripted):
        yield env


def _ask(env, call_id):
    """The protected call's hook, answered inside the response stream."""
    async def _run():
        env.answers.append(await env.hook(
            {"tool_name": TOOL, "tool_input": dict(ARGS)}, call_id, {}))
    return _Step(_run)


def _grant_key():
    from authz_grants import GrantKey, canonical_args_hash
    return GrantKey(operator_id=OPERATOR, bot_id=BOT, chat_id=OPERATOR,
                    enforcement_role="finance", artifact_id="artifact-1",
                    tool_name=TOOL, args_hash=canonical_args_hash(ARGS),
                    engagement_id="")


async def _delegated_run(env, text="what is the March total?"):
    """One synchronous delegation from the resident's turn in the operator's
    private chat: the child runs under the launcher's scope view, as
    ``delegate_to_agent`` binds it."""
    from output_boundary import TurnScope
    launcher = TurnScope(id="t-1", cid="cid-1", role="assistant", display_name="Ellen",
                         channel="telegram", message_type="channel_in")
    origin = _origin(role="assistant", execution_role="finance", chat_id=OPERATOR,
                     user_id=OPERATOR, _tg_bot=BOT, delegation_depth=1,
                     _origin_route="telegram", _origin_clearance="private",
                     _operator_turn=True,
                     turn_scope=TurnScope.for_child(launcher, "", synchronous=True))
    with _OriginCtx(origin):
        return await asyncio.wait_for(
            tools_mod._run_delegated_agent_bounded(env.cfg, text, ""), 10)


# --- a delegated specialist run ----------------------------------------------------

async def test_a_specialists_run_logs_its_start_words_steps_and_end(delegated, logs):
    _Scripted.load(_text("Looking."), _read("t1"), _result("t1", error=False),
                   _sub_step("t2"), _text("The total is 40."))
    await _delegated_run(delegated)
    keys = {k for k, _r, _t in logs.lines}
    assert keys == {FINANCE}
    texts = logs.texts()
    # the run is named by the time of the request, never by its words
    assert texts[0] == "▶ started"
    # the main loop's step is logged; the sub-agent's is not
    assert texts[1:] == ["Looking.", "🔧 Reading files", "The total is 40.",
                         "✓ answered"]
    runs = {r for _k, r, _t in logs.lines}
    assert len(runs) == 1
    assert re.fullmatch(r"Request \d\d:\d\d", next(iter(runs)))


async def test_words_written_after_a_waiting_call_are_never_logged(delegated, logs):
    """The run ends with the protected call still waiting on the operator:
    the words before it are logged, the ones after it never are."""
    _Scripted.load(_text("BEFORE"), _call("deny-1"), _ask(delegated, "deny-1"),
                   _result("deny-1"), _text("TAIL-1"), _text("TAIL-2"))
    await _delegated_run(delegated)
    assert [_deny_reason(a) for a in delegated.answers] == [_DENY_POSTED]
    words = logs.words()
    assert words == ["BEFORE"]
    assert logs.texts()[-1] == "✓ answered"
    assert not any("TAIL" in t for t in logs.texts())


async def test_a_consume_of_the_same_grant_logs_the_held_words_at_once(delegated, logs):
    """The operator approved; the model called again with the same arguments
    and the hook consumed the grant. The words held behind the first call are
    logged when that consume is read at its call's result — before the run
    ends."""
    seen: list = []

    async def _consume():
        delegated.grants.mint(_grant_key())
        delegated.answers.append(await delegated.hook(
            {"tool_name": TOOL, "tool_input": dict(ARGS)}, "retry-1", {}))

    async def _snapshot():
        seen.append(list(logs.words()))

    _Scripted.load(_text("BEFORE"), _call("deny-1"), _ask(delegated, "deny-1"),
                   _Step(_consume), _result("deny-1"), _text("MID"),
                   _call("retry-1"), _result("retry-1", error=False),
                   _Step(_snapshot), _text("Filed it."))
    await _delegated_run(delegated)
    assert _deny_reason(delegated.answers[0]) == _DENY_POSTED and delegated.answers[1] == {}
    assert seen == [["BEFORE", "MID"]]
    assert logs.words() == ["BEFORE", "MID", "Filed it."]


async def test_a_run_ended_by_an_api_fault_logs_aborted(delegated, logs):
    fault = AssistantMessage(content=[TextBlock(text="API Error: overloaded")],
                             model="sonnet")
    object.__setattr__(fault, "error", "rate_limit")
    _Scripted.load(_text("Starting."), fault)
    from error_kinds import ApiErrorTurn
    with pytest.raises(ApiErrorTurn):
        await _delegated_run(delegated)
    texts = logs.texts()
    assert texts[-1] == "⚠️ aborted"
    assert "API Error: overloaded" not in texts


# --- who has a log -------------------------------------------------------------------

@pytest.mark.parametrize("kind,key", [
    ("specialist", LogKey("agent", "finance")),
    ("executor", LogKey("agent", "finance")),
    ("resident", None),
    ("", None),
])
async def test_only_a_specialist_or_the_configurator_logs_a_delegated_run(
        logs, kind, key):
    run = tools_mod._DelegatedRunLog(SimpleNamespace(kind=kind, role="finance"),
                                     started=datetime(2026, 3, 4, 9, 7))
    run.start()
    run.words(["one"], None)
    run.step("Read")
    run.close(["one"], None)
    run.end(True)
    if key is None:
        assert logs.lines == []
    else:
        assert logs.lines == [
            (key, "Request 09:07", "▶ started"),
            (key, "Request 09:07", "one"),
            (key, "Request 09:07", "🔧 Reading files"),
            (key, "Request 09:07", "✓ answered"),
        ]


async def test_a_delegated_runs_silence_markers_are_not_logged(logs):
    """#1535: the log shows a run's words the way the operator would see
    them — a message that is only ``<silent/>`` adds no line, a leading or
    closing run of markers is dropped, and the words are logged unchanged."""
    run = tools_mod._DelegatedRunLog(SimpleNamespace(kind="specialist", role="finance"))
    messages = ["<silent/>", "  <silent/>\n<silent/> ", "<silent/>\nDone.",
                "Filed it. <silent/>", "say `<silent/>` here"]
    run.words(messages[:2], None)
    run.close(messages, None)
    run.end(True)
    assert logs.texts() == ["Done.", "Filed it.", "say `<silent/>` here",
                            "✓ answered"]


async def test_a_delegated_run_is_named_by_its_start_in_casas_time(
        logs, monkeypatch):
    """Casa's own clock and zone name the run, at its start."""
    import timekeeping
    from zoneinfo import ZoneInfo
    zone = ZoneInfo("Pacific/Auckland")
    monkeypatch.setattr(timekeeping, "resolve_tz", lambda: zone)
    before = datetime.now(zone)
    run = tools_mod._DelegatedRunLog(SimpleNamespace(kind="specialist", role="finance"))
    after = datetime.now(zone)
    run.start()
    (_key, name, _text), = logs.lines
    assert name in {f"Request {before:%H:%M}", f"Request {after:%H:%M}"}


async def test_a_delegated_runs_end_is_logged_once(logs):
    run = tools_mod._DelegatedRunLog(SimpleNamespace(kind="specialist", role="finance"))
    run.end(False)
    run.end(True)
    assert logs.texts() == ["⚠️ aborted"]


@pytest.mark.parametrize("kind,key", [
    ("specialist", LogKey("agent", "finance")),
    ("plugin", LogKey("plugins", "finance")),
    ("executor", None),
])
async def test_a_jobs_log_key(kind, key):
    import background_jobs as jobs
    rec = SimpleNamespace(kind=kind, role_or_type="finance", task="the task",
                          origin={"job": {"title": "Q3 accounting"}})
    assert jobs.job_log_key(rec) == key
    assert jobs.job_run_name(rec) == "Q3 accounting"
    rec.origin = {}
    assert jobs.job_run_name(rec) == "the task"


# --- the end line ----------------------------------------------------------------------

@pytest.mark.parametrize("outcome,mark", [
    ("completed", "✅"), ("cancelled", "🛑"), ("error", "❌")])
async def test_the_finalize_funnel_logs_the_end(monkeypatch, tmp_path, outcome, mark):
    from engagement_registry import EngagementRegistry
    import background_jobs as jobs
    logs = RecordingLogs()
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    decl = jobs.JobDecl("q:run", "q", "run", "q:job", "Q3 accounting", None)
    rec = await reg.create(
        "specialist", "finance", "in_casa", "t",
        {"role": "assistant", "channel": "telegram", "chat_id": "1", "user_id": 7,
         "job": jobs.initial_job_state(decl, started_by="operator")})
    tools_mod.init_tools(channel_manager=None, bus=None, specialist_registry=MagicMock(),
                         engagement_registry=reg, agent_logs=logs)
    monkeypatch.setattr(tools_mod, "send_engagement_outcome", AsyncMock())
    summary = "Tagged 41 rows; " + "y" * 400 + "\nsecond line"
    await tools_mod._finalize_engagement(rec, outcome=outcome, text=summary,
                                         next_steps=[], driver=None)
    (key, run, text), = logs.lines
    assert (key, run) == (FINANCE, "Q3 accounting")
    first = ("Tagged 41 rows; " + "y" * 400)[:299] + "…"
    assert text == f"{mark} {outcome} — {first}"


async def test_the_end_line_without_a_summary():
    assert tools_mod._end_log_line("completed", "") == "✅ completed"
    assert tools_mod._end_log_line("cancelled", "\n  Done.\nmore") == "🛑 cancelled — Done."


# --- the configurator's step line ------------------------------------------------------

class _DM:
    # Casa's own endpoint as the transport sees it.
    chat_id = "123"
    bot_id = 111
    has_bot_id = True
    default_agent = "assistant"
    on_identity = None

    def address(self, chat_id, message_id=None):
        from channels.tg_address import TgAddress
        return TgAddress(self.bot_id, int(chat_id), message_id)

    async def send_step_line(self, chat_id, text):
        return 77

    async def edit_dm_message(self, chat_id, message_id, text, buttons=None):
        return True

    async def delete_dm_message(self, chat_id, message_id):
        return True


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_configurators_step_line_moves_to_its_log(monkeypatch, enabled):
    import agent_loader
    from telegram_channel_double import make_transport
    monkeypatch.setattr(tools_mod, "_channel_manager", make_transport(_DM()))
    monkeypatch.setattr(tools_mod, "_agent_logs", RecordingLogs(enabled=enabled))
    line = tools_mod._configurator_step_line(agent_loader.load_configurator(), {})
    if enabled:
        assert line is None
    else:
        assert line is not None
        line.finish(ok=True)


# --- turning the log on, and the periodic pass ------------------------------------------

class _Logs:
    def __init__(self, *, enabled=False, ok=True):
        self.enabled = enabled
        self.ok = ok
        self.checks: list = []
        self.started = 0
        self.passes = 0

    async def check_enabled(self, bot, chat_id):
        self.checks.append(chat_id)
        if self.ok:
            self.enabled = True
        return self.ok

    def start(self):
        self.started += 1

    async def retention_pass(self):
        self.passes += 1


@pytest.mark.parametrize("ok", [True, False])
async def test_the_channel_check_starts_the_writer_only_when_on(ok):
    from channels.telegram import TelegramChannel
    ch = TelegramChannel(bot=MagicMock(), chat_id="1", engagement_supergroup_id=-1001)
    ch._app = SimpleNamespace(bot=ch.bot)
    ch._agent_logs = _Logs(ok=ok)
    assert await ch.check_agent_logs() is ok
    assert ch._agent_logs.checks == [-1001]
    assert ch._agent_logs.started == (1 if ok else 0)


async def test_the_periodic_pass_rechecks_a_log_that_is_off():
    """A transient failure at the rebuild's check leaves the log off; the
    30-minute pass checks again, then deletes what is due."""
    import casa_core
    channel = SimpleNamespace(check_agent_logs=AsyncMock(return_value=True))
    off = _Logs(enabled=False)
    await casa_core._agent_log_retention(channel, off)
    channel.check_agent_logs.assert_awaited_once()
    assert off.passes == 1
    on = _Logs(enabled=True)
    channel.check_agent_logs.reset_mock()
    await casa_core._agent_log_retention(channel, on)
    channel.check_agent_logs.assert_not_awaited()
    assert on.passes == 1


async def test_the_periodic_pass_never_raises():
    import casa_core

    class _Broken(_Logs):
        async def retention_pass(self):
            raise RuntimeError("boom")

    await casa_core._agent_log_retention(None, _Broken(enabled=True))
    await casa_core._agent_log_retention(None, None)
