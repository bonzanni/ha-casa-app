"""A job's declaration and the options every job session runs with.

A job is one long session: its declaration names the job and nothing about
batches, and every job session runs with the high step ceiling, the shared
long-session auto-compaction window and the CLI's take-up echo."""

import uuid

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
    assert set(state) == {"name", "title", "skill", "brief_uuid",
                          "last_summary", "started_by"}
    assert str(uuid.UUID(state["brief_uuid"])) == state["brief_uuid"]


def test_each_job_gets_its_own_brief_uuid():
    a = jobs.initial_job_state(DECL)["brief_uuid"]
    b = jobs.initial_job_state(DECL)["brief_uuid"]
    assert a != b


def test_job_session_options_set_ceiling_window_and_echo():
    base = ClaudeAgentOptions(max_turns=5, env={"KEEP": "1"},
                              extra_args={"other": "x"})
    opts = jobs.job_session_options(base, object())
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
        ClaudeAgentOptions(cli_path="/usr/local/bin/claude"), object())
    command = SubprocessCLITransport(prompt="x", options=opts)._build_command()
    assert "--replay-user-messages" in command
    assert command[command.index("--max-turns") + 1] == "1000"
