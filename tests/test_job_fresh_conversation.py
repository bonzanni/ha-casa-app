"""The job's brief (INV-BGJOB-005): the one message Casa writes at launch.

It names the job by its engagement id exactly once, right after its first
line, and carries the launch context byte for byte. The record keeps what the
brief is rendered from, so a resume that finds the brief never read writes
the same brief again (``background_jobs.job_brief``). Runs ``start_job`` on the
real launch, registry, in-casa driver and Telegram channel
(``test_start_job.runtime``).
"""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

import agent
import background_jobs as jobs
import tools
from engagement_registry import EngagementRegistry

try:
    from tests.test_start_job import DECL, drain_launch_turns, runtime  # noqa: F401
    from tests.test_start_job import Client as LaunchClient
except ImportError:
    from test_start_job import DECL, drain_launch_turns, runtime  # noqa: F401
    from test_start_job import Client as LaunchClient

pytestmark = [pytest.mark.unit]   # asyncio_mode = auto (pytest.ini)

TASK = "Classify the SECRET-TASK ledger rows"
# Byte-for-byte: CRLF, leading/trailing blanks, braces, a non-ASCII dash, a
# backslash sequence and a trailing newline must all survive into the brief.
CONTEXT = "  rows 1–9 only\r\n{account}: $x \\n keep\t tabs \n"


async def _launch(runtime, monkeypatch):
    decl = dataclasses.replace(DECL)
    host = jobs.JobHost('specialist', 'finance', decl, SimpleNamespace(name='ledger'))
    monkeypatch.setattr(jobs, 'find_job_host', lambda name, caller, roles:
                        host if name == decl.qualified_name and 'finance' in roles else None)
    token = agent.origin_var.set(dict(role='assistant', execution_role='assistant',
                                      channel='telegram', chat_id='1', cid='test',
                                      user_text='classify', _operator_turn=True))
    try:
        envelope = await tools.start_job.handler(
            {"job": decl.qualified_name, "task": TASK, "context": CONTEXT})
    finally:
        agent.origin_var.reset(token)
    reply = json.loads(envelope['content'][0]['text'])
    assert reply['status'] == 'pending', reply
    await drain_launch_turns()
    return runtime.registry.get(reply['engagement_id'])


def _job_id_lines(text, rec):
    return [line for line in text.splitlines() if line.startswith("Job id:")], f"Job id: {rec.id}"


async def test_the_launch_prompt_names_the_job_id_once(runtime, monkeypatch):
    """The brief carries exactly one ``Job id: <engagement id>`` line, right
    after its first line: a plugin claims work by that id and matches the
    job-end notice's delegation id to it."""
    rec = await _launch(runtime, monkeypatch)
    assert len(LaunchClient.instances) == 1
    assert len(LaunchClient.instances[0].prompts) == 1
    launch = LaunchClient.instances[0].prompts[0]
    lines, expected = _job_id_lines(launch, rec)
    assert lines == [expected]
    assert launch.splitlines()[1] == expected
    assert launch.splitlines()[0].startswith('You are running the background job')


async def test_brief_context_is_the_launch_context_verbatim_across_batches_and_restart(
        runtime, monkeypatch):
    """The launch context reaches the brief verbatim, is recorded on the job,
    survives a reload from disk, and the brief rendered from the reloaded
    record is the one written at launch."""
    rec = await _launch(runtime, monkeypatch)
    launch = LaunchClient.instances[0].prompts[0]
    assert launch.count(f"Context: {CONTEXT}") == 1
    assert launch.count(f"Request: {TASK}") == 1
    assert rec.origin["job"]["context"] == CONTEXT
    loaded = EngagementRegistry(tombstone_path=runtime.registry._tombstone_path)
    await loaded.load()
    again = loaded.get(rec.id)
    assert again.origin["job"]["context"] == CONTEXT
    assert again.origin["job"]["brief_uuid"] == rec.origin["job"]["brief_uuid"]
    assert jobs.job_brief(again) == launch
