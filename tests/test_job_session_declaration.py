"""A job's declaration and the options every job session runs with.

A job is one long session: its declaration names the job and nothing about
batches, and every job session runs with the high step ceiling, the shared
long-session auto-compaction window and the CLI's take-up echo."""

import uuid

import pytest

from claude_agent_sdk import ClaudeAgentOptions

import background_jobs as jobs
import claude_runtime

DECL = jobs.JobDecl("sample:work", "sample", "work", "sample:work",
                    "Process rows", None)


def test_the_declaration_carries_no_batch_fields():
    fields = set(jobs.JobDecl.__dataclass_fields__)
    assert not fields & {"batches", "turns_per_batch", "session"}


def test_initial_state_has_identity_and_no_loop_counters():
    state = jobs.initial_job_state(DECL, started_by="operator")
    assert set(state) == {"name", "title", "skill", "brief_uuid", "context",
                          "last_summary", "started_by"}
    assert str(uuid.UUID(state["brief_uuid"])) == state["brief_uuid"]


def test_each_job_gets_its_own_brief_uuid():
    a = jobs.initial_job_state(DECL)["brief_uuid"]
    b = jobs.initial_job_state(DECL)["brief_uuid"]
    assert a != b


def test_job_session_options_set_ceiling_window_and_echo():
    base = ClaudeAgentOptions(max_turns=5, env={"KEEP": "1"},
                              extra_args={"other": "x"})
    opts = jobs.job_session_options(base, _rec())
    assert opts.max_turns == jobs.JOB_MAX_TURNS == 1000
    assert opts.env == {"KEEP": "1",
                        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"}
    assert opts.extra_args == {"other": "x", "replay-user-messages": None}
    # The input is not mutated: builders may share option objects.
    assert base.max_turns == 5 and base.env == {"KEEP": "1"}


def test_the_window_is_one_shared_constant():
    assert claude_runtime.long_session_env() == {
        claude_runtime.AUTO_COMPACT_WINDOW_ENV:
            claude_runtime.LONG_SESSION_COMPACT_WINDOW}


def test_the_cli_command_carries_the_echo_flag_and_ceiling():
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport)
    opts = jobs.job_session_options(
        ClaudeAgentOptions(cli_path="/usr/local/bin/claude"), _rec())
    command = SubprocessCLITransport(prompt="x", options=opts)._build_command()
    assert "--replay-user-messages" in command
    assert command[command.index("--max-turns") + 1] == "1000"


def _rec(started_by="operator"):
    from types import SimpleNamespace
    state = jobs.initial_job_state(DECL, started_by=started_by)
    return SimpleNamespace(id="f" * 32, task="Tag the March rows",
                           origin={"job": state})


async def _fire(options, source):
    [matcher] = options.hooks["SessionStart"]
    [hook] = matcher.hooks
    return await hook({"hook_event_name": "SessionStart", "source": source},
                      None, {"signal": None})


@pytest.mark.asyncio
async def test_after_a_compaction_the_session_is_told_who_the_job_is():
    rec = _rec()
    opts = jobs.job_session_options(ClaudeAgentOptions(), rec)
    out = await _fire(opts, "compact")
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert out["hookSpecificOutput"]["additionalContext"].splitlines() == [
        'You are running the background job "Process rows" (skill sample:work).',
        f"Job id: {rec.id}",
        "Started by: operator",
        "Request: Tag the March rows",
        "Your context was just summarised; carry on.",
    ]


@pytest.mark.asyncio
async def test_any_other_session_start_adds_nothing():
    opts = jobs.job_session_options(ClaudeAgentOptions(), _rec())
    assert await _fire(opts, "startup") == {}
    assert await _fire(opts, "resume") == {}


def test_the_header_has_no_starter_line_when_none_was_recorded():
    rec = _rec(started_by=None)
    lines = jobs.job_header(rec).splitlines()
    assert lines[1] == f"Job id: {rec.id}" and lines[2].startswith("Request:")


def test_the_hook_joins_the_session_s_own_hooks():
    from claude_agent_sdk import HookMatcher
    own = HookMatcher(matcher="Bash", hooks=[])
    opts = jobs.job_session_options(
        ClaudeAgentOptions(hooks={"PreToolUse": [own]}), _rec())
    assert opts.hooks["PreToolUse"] == [own]
    assert len(opts.hooks["SessionStart"]) == 1
