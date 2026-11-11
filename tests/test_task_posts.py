"""Every post of a task is filed under that task, in the registry that holds it.

A job's posts go on its engagement record, a delegation's on its job row; a
reply's quoted message finds the live task (``owner``) or the ended one
(``ended_owner``), after a reload too, and only on the bot that posted it.

REAL ``EngagementRegistry`` and ``JobRegistry`` on tmp files, bound where the
running Casa holds them (``tools``).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from channels.tg_address import TgAddress
from job_registry import DeliveryState, ExecutionState, JobRegistry, VoiceJob
from personality_types import SpeakerProvenance
from task_model import Requester

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

ELLEN = Requester("assistant", {"_tg_bot": 111})
ALEX_CHAT = TgAddress(222, 100)


def A(mid: int, *, bot: int = 222, chat: int = 100) -> TgAddress:
    return TgAddress(bot, chat, mid)


def _job_state():
    import background_jobs as jobs
    decl = jobs.JobDecl("sample:work", "sample", "work", "sample:work",
                        "Process rows", None)
    return jobs.initial_job_state(decl, started_by="operator")


def _delegation(job_id: str = "d-1") -> VoiceJob:
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
        route={"_tg_bot": 111},
        executor="finance", requester=ELLEN, delivery=ALEX_CHAT,
    )


@pytest.fixture
async def world(tmp_path, monkeypatch):
    import task_posts
    import tools
    from engagement_registry import EngagementRegistry

    engagements = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    jobs = JobRegistry(tmp_path / "jobs.json")
    await jobs.load()
    monkeypatch.setattr(tools, "_engagement_registry", engagements)
    monkeypatch.setattr(tools, "_specialist_registry",
                        SimpleNamespace(job_registry=jobs))
    task_posts.reset()
    yield SimpleNamespace(engagements=engagements, jobs=jobs, tmp=tmp_path,
                          mp=monkeypatch)
    await jobs.close()
    task_posts.reset()


async def _job(world):
    origin = {"role": "assistant", "channel": "telegram", "chat_id": "100",
              "user_id": 100, "_tg_bot": 111, "job": _job_state()}
    return await world.engagements.create(
        "specialist", "finance", "in_casa", "t", origin,
        executor="finance", requester=ELLEN, delivery=ALEX_CHAT)


async def _flush_jobs(jobs):
    while jobs._deferred_writes:
        await asyncio.gather(*list(jobs._deferred_writes), return_exceptions=True)


async def _flush_engagements(reg):
    while reg._deferred_persists:
        await asyncio.gather(*list(reg._deferred_persists), return_exceptions=True)


class TestJobs:
    async def test_a_jobs_posts_are_filed_on_its_record_and_find_it(self, world):
        import task_posts
        rec = await _job(world)
        task_posts.open(rec.id, ALEX_CHAT)
        task_posts.record(rec.id, A(501))
        task_posts.record(rec.id, A(502))
        assert rec.dm_posts == [A(501), A(502)]
        ref = task_posts.owner(A(502))
        assert ref == task_posts.TaskRef("job", rec.id, "finance", ELLEN)
        assert task_posts.ended_owner(A(502)) is None

    async def test_an_ended_jobs_post_finds_the_ended_job(self, world):
        import task_posts
        rec = await _job(world)
        task_posts.open(rec.id, ALEX_CHAT)
        task_posts.record(rec.id, A(501))
        await world.engagements.mark_completed(rec.id, 2.0)
        assert task_posts.owner(A(501)) is None
        assert task_posts.ended_owner(A(501)).task_id == rec.id

    async def test_a_jobs_posts_find_it_after_a_reload(self, world):
        import task_posts
        import tools
        from engagement_registry import EngagementRegistry
        rec = await _job(world)
        task_posts.record(rec.id, A(501))
        await _flush_engagements(world.engagements)
        again = EngagementRegistry(tombstone_path=str(world.tmp / "e.json"))
        await again.load()
        world.mp.setattr(tools, "_engagement_registry", again)
        ref = task_posts.owner(A(501))
        assert (ref.kind, ref.task_id, ref.executor, ref.requester) == (
            "job", rec.id, "finance", ELLEN)


class TestDelegations:
    async def test_a_delegations_posts_are_filed_on_its_row_and_find_it(self, world):
        import task_posts
        await world.jobs.create(_delegation())
        task_posts.open("d-1", ALEX_CHAT)
        assert task_posts.delivery_of("d-1") == ALEX_CHAT
        task_posts.record("d-1", A(601))
        task_posts.record("d-1", A(602))
        assert world.jobs.task_posts("d-1") == [A(601), A(602)]
        assert task_posts.owner(A(601)) == task_posts.TaskRef(
            "delegation", "d-1", "finance", ELLEN)
        assert task_posts.ended_owner(A(601)) is None

    async def test_an_ended_delegations_post_finds_the_ended_delegation(self, world):
        import task_posts
        await world.jobs.create(_delegation())
        task_posts.record("d-1", A(601))
        await world.jobs.finish_compat("d-1", "done")
        assert task_posts.owner(A(601)) is None
        assert task_posts.ended_owner(A(601)).task_id == "d-1"
        # a post landing after the end is the ended delegation's too
        task_posts.record("d-1", A(603))
        assert task_posts.ended_owner(A(603)).task_id == "d-1"

    async def test_a_delegations_posts_find_it_after_a_reload(self, world):
        import task_posts
        import tools
        await world.jobs.create(_delegation())
        task_posts.record("d-1", A(601))
        await _flush_jobs(world.jobs)
        row = json.loads((world.tmp / "jobs.json").read_text())[0]
        assert row["task_posts"] == [[222, 100, 601]]
        assert row["delivery"] == [222, 100, None]
        assert row["requester"] == {"role": "assistant", "route": {"_tg_bot": 111}}
        again = JobRegistry(world.tmp / "jobs.json")
        await again.load()
        world.mp.setattr(tools, "_specialist_registry",
                         SimpleNamespace(job_registry=again))
        assert task_posts.owner(A(601)) == task_posts.TaskRef(
            "delegation", "d-1", "finance", ELLEN)
        assert again.get("d-1").delivery == ALEX_CHAT

    async def test_a_null_delivery_round_trips_as_null(self, world):
        from dataclasses import replace
        await world.jobs.create(replace(_delegation(), delivery=None))
        row = json.loads((world.tmp / "jobs.json").read_text())[0]
        assert row["delivery"] is None
        again = JobRegistry(world.tmp / "jobs.json")
        await again.load()
        assert again.get("d-1") is not None and again.get("d-1").delivery is None

    @pytest.mark.parametrize("drop", ["executor", "requester", "delivery"])
    async def test_a_telegram_row_without_a_task_field_is_refused_at_load(
            self, world, drop):
        await world.jobs.create(_delegation())
        rows = json.loads((world.tmp / "jobs.json").read_text())
        rows[0].pop(drop)
        (world.tmp / "jobs.json").write_text(json.dumps(rows))
        again = JobRegistry(world.tmp / "jobs.json")
        await again.load()
        assert again.get("d-1") is None

    async def test_a_deleted_row_takes_its_posts_with_it(self, world):
        import task_posts
        from dataclasses import replace
        await world.jobs.create(replace(_delegation(), expires_at=5.0,
                                        execution_state=ExecutionState.SUCCEEDED,
                                        terminal_at=2.0))
        task_posts.record("d-1", A(601))
        world.jobs._clock = lambda: 10.0
        await world.jobs.expire_due()
        assert world.jobs.get("d-1") is None
        assert task_posts.ended_owner(A(601)) is None
        assert world.jobs.task_posts("d-1") == []


class TestIndex:
    async def test_equal_message_ids_on_two_bots_are_two_tasks(self, world):
        import task_posts
        rec = await _job(world)
        await world.jobs.create(_delegation())
        task_posts.record(rec.id, A(700, bot=222))
        task_posts.record("d-1", A(700, bot=333))
        assert task_posts.owner(A(700, bot=222)).task_id == rec.id
        assert task_posts.owner(A(700, bot=333)).task_id == "d-1"
        assert task_posts.owner(A(700, bot=444)) is None

    async def test_an_unknown_owner_files_nothing_and_never_raises(self, world):
        import task_posts
        task_posts.record("nobody", A(1))
        task_posts.record("", A(2))
        assert task_posts.owner(A(1)) is None
        task_posts.open("nobody", ALEX_CHAT)       # logged, not raised
        assert task_posts.delivery_of("nobody") is None

    async def test_a_message_without_an_id_is_no_ones(self, world):
        import task_posts
        assert task_posts.owner(ALEX_CHAT) is None
        assert task_posts.ended_owner(ALEX_CHAT) is None

    async def test_without_registries_nothing_is_found(self, world):
        import task_posts
        import tools
        world.mp.setattr(tools, "_engagement_registry", None)
        world.mp.setattr(tools, "_specialist_registry", None)
        task_posts.record("d-1", A(1))
        assert task_posts.owner(A(1)) is None
        assert task_posts.ended_owner(A(1)) is None
