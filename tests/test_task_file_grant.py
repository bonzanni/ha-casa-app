"""A file sent to a running task is readable by that task's build, and only
that file is added.

Through the REAL ``hooks.make_path_scope_hook_v2``, built by
``hooks.resolve_hooks`` from ``agent_inbox.delegated_build_grants`` exactly as
a delegated or job build does, for a job and for a delegation: the build
exists first, its session attaches, a file is granted AFTER that, and the
very next Read of it is allowed. A job build reads nothing else of the inbox
(a job build gets no inbox), and another task's build is
denied the granted file. A delegated build keeps today's read of its own
inbox; the grant only adds the one file.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from channels.tg_address import TgAddress
from config import HooksConfig
from job_registry import DeliveryState, ExecutionState, JobRegistry, VoiceJob
from personality_types import SpeakerProvenance
from task_model import Requester

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

ELLEN = Requester("assistant", {"_tg_bot": 111})
ALEX_CHAT = TgAddress(222, 100)


def _delegation(job_id: str) -> VoiceJob:
    speaker = SpeakerProvenance(speaker_kind="system")
    return VoiceJob(
        id=job_id, parent_job_id=None,
        creating_speaker=speaker, executing_speaker=speaker,
        creating_role="assistant", specialist_role="finance",
        specialist_display_name="Alex", creator_peer="telegram",
        creator_user_id="100", scope_id="100", origin_route_id="c1",
        origin_device_id=None, task="brief", context="",
        created_at=1.0, started_at=1.0, terminal_at=None, expires_at=None,
        execution_state=ExecutionState.RUNNING,
        delivery_state=DeliveryState.NONE, result=None, failure=None,
        awaiting_input=False, continuable_until=None, delivery_sequence=0,
        delivery_attempt_id=None, lease_until=None, cancel_pending=False,
        route={"_tg_bot": 111}, executor="finance", requester=ELLEN,
        delivery=ALEX_CHAT,
    )


def _job_state():
    import background_jobs as jobs
    decl = jobs.JobDecl("sample:work", "sample", "work", "sample:work",
                        "Process rows", None)
    return jobs.initial_job_state(decl, started_by="operator")


@pytest.fixture
async def world(tmp_path, monkeypatch):
    import agent_inbox
    import task_input
    import task_posts
    import tools
    from engagement_registry import EngagementRegistry

    agent_inbox._reset_for_tests()
    inbox = agent_inbox.open_inbox("finance", str(tmp_path / "inboxes"))
    agent_inbox._inboxes["finance"] = inbox
    engagements = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    jobs = JobRegistry(tmp_path / "jobs.json")
    await jobs.load()
    monkeypatch.setattr(tools, "_engagement_registry", engagements)
    monkeypatch.setattr(tools, "_specialist_registry",
                        SimpleNamespace(job_registry=jobs))
    task_input.reset()
    task_posts.reset()
    job = await engagements.create(
        "specialist", "finance", "in_casa", "t",
        {"role": "assistant", "channel": "telegram", "chat_id": "100",
         "user_id": 100, "_tg_bot": 111, "job": _job_state()},
        executor="finance", requester=ELLEN, delivery=ALEX_CHAT)
    await jobs.create(_delegation("d-1"))
    await jobs.create(_delegation("d-2"))
    yield SimpleNamespace(inbox=inbox, job=job, tmp=tmp_path)
    await jobs.close()
    task_input.reset()
    agent_inbox._reset_for_tests()


def _cfg(tmp_path):
    return SimpleNamespace(role="finance", kind="specialist", hooks=HooksConfig(),
                           cwd=str(tmp_path / "cwd"))


def _build(cfg, *, engagement: bool, task_id: str):
    """The build's path_scope hook, as the delegated builder resolves it."""
    import agent_inbox
    from hooks import resolve_hooks
    grants = agent_inbox.delegated_build_grants(cfg, engagement=engagement,
                                                task_id=task_id)
    resolved = resolve_hooks(cfg.hooks, default_cwd=cfg.cwd,
                             extra_readable=grants.prefixes,
                             extra_read_check=grants.read_check)
    [scope] = [m for m in resolved["PreToolUse"] if m.matcher == "Read|Write|Edit"]
    [hook] = scope.hooks
    return hook


async def _reads(hook, path) -> bool:
    out = await hook({"tool_name": "Read", "tool_input": {"file_path": str(path)}},
                     None, {})
    return out == {}


def _file(inbox, name: str) -> Path:
    path = Path(inbox.ready_dir) / name
    path.write_bytes(b"%PDF-1.4")
    return path


class _Client:
    async def query(self, stream):
        async for _ in stream:
            pass


@pytest.mark.parametrize("kind", ["job", "delegation"])
async def test_a_file_granted_after_the_build_is_readable_at_once(world, kind):
    import agent_inbox
    import task_input
    task_id = world.job.id if kind == "job" else "d-1"
    hook = _build(_cfg(world.tmp), engagement=kind == "job", task_id=task_id)  # 1. build
    task_input.open_pending(task_id)
    task_input.attach(task_id, _Client())                                      # 2. attach
    sent = _file(world.inbox, "1700000000000-aaaaaaaaaaaaaaaa.pdf")
    if kind == "job":
        assert not await _reads(hook, sent)            # not readable before the grant
    agent_inbox.grant_task_file(task_id, sent)                                 # 3. grant
    assert await _reads(hook, sent)                                            # 4. Read


async def test_a_job_build_reads_no_other_file_of_the_inbox(world):
    import agent_inbox
    hook = _build(_cfg(world.tmp), engagement=True, task_id=world.job.id)
    sent = _file(world.inbox, "1700000000000-aaaaaaaaaaaaaaaa.pdf")
    other = _file(world.inbox, "1700000000001-bbbbbbbbbbbbbbbb.pdf")
    agent_inbox.grant_task_file(world.job.id, sent)
    assert await _reads(hook, sent)
    assert not await _reads(hook, other)
    assert not await _reads(hook, world.inbox.ready_dir)
    assert not await _reads(hook, str(sent) + "/../" + other.name)


@pytest.mark.parametrize("granted_to", ["job", "delegation"])
async def test_another_tasks_build_is_denied_the_granted_file(world, granted_to):
    import agent_inbox
    task_id = world.job.id if granted_to == "job" else "d-1"
    sent = _file(world.inbox, "1700000000000-aaaaaaaaaaaaaaaa.pdf")
    agent_inbox.grant_task_file(task_id, sent)
    # another task's job build of the same agent
    other_job = _build(_cfg(world.tmp), engagement=True, task_id="d-2")
    assert not await _reads(other_job, sent)
    assert agent_inbox.task_file_granted("d-2", sent) is False
    assert agent_inbox.task_file_granted(task_id, sent) is True


async def test_a_delegated_build_keeps_its_own_inbox_and_gains_the_file(world):
    """A delegated build keeps what it has today: it reads its own ``ready/`` as
    today; the grant adds nothing it could not already read there."""
    import agent_inbox
    hook = _build(_cfg(world.tmp), engagement=False, task_id="d-1")
    other = _file(world.inbox, "1700000000001-bbbbbbbbbbbbbbbb.pdf")
    assert await _reads(hook, other)
    assert not await _reads(hook, world.tmp / "elsewhere.pdf")
    assert agent_inbox.task_file_granted("d-1", other) is False


async def test_only_a_file_of_the_executors_own_inbox_is_granted(world):
    import agent_inbox
    elsewhere = world.tmp / "elsewhere.pdf"
    elsewhere.write_bytes(b"x")
    agent_inbox.grant_task_file(world.job.id, elsewhere)
    assert agent_inbox.task_file_granted(world.job.id, elsewhere) is False
    # another agent's inbox is not the executor's own
    tina = agent_inbox.open_inbox("tina", str(world.tmp / "inboxes"))
    agent_inbox._inboxes["tina"] = tina
    theirs = _file(tina, "1700000000000-cccccccccccccccc.pdf")
    agent_inbox.grant_task_file(world.job.id, theirs)
    assert agent_inbox.task_file_granted(world.job.id, theirs) is False
    # a task no registry holds has no executor, so nothing is granted
    sent = _file(world.inbox, "1700000000000-aaaaaaaaaaaaaaaa.pdf")
    agent_inbox.grant_task_file("no-such-task", sent)
    assert agent_inbox.task_file_granted("no-such-task", sent) is False


async def test_without_a_task_the_build_is_todays(world):
    import agent_inbox
    from hooks import resolve_hooks
    cfg = _cfg(world.tmp)
    grants = agent_inbox.delegated_build_grants(cfg, engagement=True)
    assert grants.read_check is None
    sent = _file(world.inbox, "1700000000000-aaaaaaaaaaaaaaaa.pdf")
    agent_inbox.grant_task_file(world.job.id, sent)
    resolved = resolve_hooks(cfg.hooks, default_cwd=cfg.cwd,
                             extra_readable=grants.prefixes,
                             extra_read_check=grants.read_check)
    [scope] = [m for m in resolved["PreToolUse"] if m.matcher == "Read|Write|Edit"]
    assert not await _reads(scope.hooks[0], sent)
    assert os.path.exists(sent)
