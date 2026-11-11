"""Jobs and delegations keep the bot they belong to.

Telegram numbers messages per bot, and two bots' private chats with the
operator share the chat id. So a job's posts are filed by ``(bot, chat,
message)``, a job's journaled notice names the bot that received the message,
and every stored origin keeps its route (``_tg_bot``). Each store round-trips
its addresses and routes through a reload, and its loader refuses, loudly and
without crashing, an entry that does not name its bot.

REAL ``EngagementRegistry``, ``JobRegistry`` and ``job_inbox`` on tmp files.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace

import pytest

from channels.tg_address import CASA, TgAddress

pytestmark = [pytest.mark.unit]

CASA_BOT = 111
FINANCE_BOT = 222
CHAT = 100


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _job_state() -> dict:
    import background_jobs as jobs
    decl = jobs.JobDecl("sample:work", "sample", "work", "sample:work",
                        "Process rows", None)
    return jobs.initial_job_state(decl, started_by="operator")


def _origin(route=CASA_BOT, **extra) -> dict:
    origin = {"role": "assistant", "channel": "telegram", "chat_id": str(CHAT),
              "user_id": CHAT, "job": _job_state(), **extra}
    if route is not None:
        origin["_tg_bot"] = route
    return origin


def _task(route=CASA_BOT) -> dict:
    """A job's task fields, decided at its birth: finance runs it for the
    resident, and its posts go to the operator's chat on the bot of *route*."""
    from task_model import Requester
    bot = CASA_BOT if route == CASA else route
    return {"executor": "finance",
            "requester": Requester("assistant", {"_tg_bot": route}),
            "delivery": TgAddress(bot, CHAT)}


async def _registry(tmp_path):
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    await reg.load()
    return reg


async def _flush(reg):
    while reg._deferred_persists:
        await asyncio.gather(*list(reg._deferred_persists),
                             return_exceptions=True)


async def _reload(tmp_path):
    from engagement_registry import EngagementRegistry
    again = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    await again.load()
    return again


def _rows(tmp_path) -> list[dict]:
    return json.loads((tmp_path / "e.json").read_text())


def _write_rows(tmp_path, rows) -> None:
    (tmp_path / "e.json").write_text(json.dumps(rows))


# ---------------------------------------------------------------------------
# Cross-bot collision: equal message ids on two bots are two posts
# ---------------------------------------------------------------------------

class TestEqualMessageIdsOnTwoBots:
    async def test_live_owners_are_distinct_per_bot(self, tmp_path):
        reg = await _registry(tmp_path)
        a = await reg.create("specialist", "finance", "in_casa", "a",
                             _origin(CASA_BOT), **_task(CASA_BOT))
        b = await reg.create("specialist", "finance", "in_casa", "b",
                             _origin(FINANCE_BOT), **_task(FINANCE_BOT))
        assert reg.record_job_post(a.id, TgAddress(CASA_BOT, CHAT, 5))
        assert reg.record_job_post(b.id, TgAddress(FINANCE_BOT, CHAT, 5))
        assert reg.job_post_owner(TgAddress(CASA_BOT, CHAT, 5)) is a
        assert reg.job_post_owner(TgAddress(FINANCE_BOT, CHAT, 5)) is b
        # a bot that posted nothing owns nothing at that message id
        assert reg.job_post_owner(TgAddress(333, CHAT, 5)) is None

    async def test_a_reply_on_another_bot_does_not_reach_the_live_job(self, tmp_path):
        reg = await _registry(tmp_path)
        a = await reg.create("specialist", "finance", "in_casa", "a",
                             _origin(CASA_BOT), **_task(CASA_BOT))
        assert reg.record_job_post(a.id, TgAddress(CASA_BOT, CHAT, 5))
        assert reg.job_post_owner(TgAddress(FINANCE_BOT, CHAT, 5)) is None
        # a bare message id or a (chat, message) pair names no post at all
        assert reg.job_post_owner(5) is None
        assert reg.job_post_owner((CHAT, 5)) is None

    async def test_ended_owners_are_distinct_per_bot_and_survive_a_reload(
            self, tmp_path):
        reg = await _registry(tmp_path)
        a = await reg.create("specialist", "finance", "in_casa", "a",
                             _origin(CASA_BOT), **_task(CASA_BOT))
        assert reg.record_job_post(a.id, TgAddress(CASA_BOT, CHAT, 5))
        assert await reg.try_transition_terminal(a.id, "completed", strict=True)
        assert reg.ended_job_post_owner(TgAddress(CASA_BOT, CHAT, 5)) is a
        assert reg.ended_job_post_owner(TgAddress(FINANCE_BOT, CHAT, 5)) is None
        again = await _reload(tmp_path)
        assert again.ended_job_post_owner(
            TgAddress(CASA_BOT, CHAT, 5)).id == a.id
        assert again.ended_job_post_owner(
            TgAddress(FINANCE_BOT, CHAT, 5)) is None

    async def test_the_live_index_is_rebuilt_per_bot_at_load(self, tmp_path):
        reg = await _registry(tmp_path)
        a = await reg.create("specialist", "finance", "in_casa", "a",
                             _origin(CASA_BOT), **_task(CASA_BOT))
        b = await reg.create("specialist", "finance", "in_casa", "b",
                             _origin(FINANCE_BOT), **_task(FINANCE_BOT))
        reg.record_job_post(a.id, TgAddress(CASA_BOT, CHAT, 5))
        reg.record_job_post(b.id, TgAddress(FINANCE_BOT, CHAT, 5))
        await _flush(reg)
        again = await _reload(tmp_path)
        assert again.job_post_owner(TgAddress(CASA_BOT, CHAT, 5)).id == a.id
        assert again.job_post_owner(TgAddress(FINANCE_BOT, CHAT, 5)).id == b.id


# ---------------------------------------------------------------------------
# Engagement records: origin route, dm_posts, ui_posts
# ---------------------------------------------------------------------------

class TestEngagementRoundTrip:
    @pytest.mark.parametrize("route", [CASA_BOT, CASA])
    async def test_the_origin_keeps_its_route_through_a_reload(self, tmp_path, route):
        reg = await _registry(tmp_path)
        rec = await reg.create("specialist", "finance", "in_casa", "t",
                               _origin(route), **_task(route))
        assert _rows(tmp_path)[0]["origin"]["_tg_bot"] == route
        again = await _reload(tmp_path)
        assert again.get(rec.id).origin["_tg_bot"] == route

    async def test_posts_and_keyboards_round_trip_with_their_bot(self, tmp_path):
        from engagement_registry import JOB_POST_QUESTION, JOB_POST_START
        reg = await _registry(tmp_path)
        rec = await reg.create("specialist", "finance", "in_casa", "t",
                               _origin(FINANCE_BOT), **_task(FINANCE_BOT))
        start = TgAddress(FINANCE_BOT, CHAT, 7)
        question = TgAddress(FINANCE_BOT, CHAT, 8)
        plain = TgAddress(FINANCE_BOT, CHAT, 9)
        reg.record_job_post(rec.id, start, keyboard=JOB_POST_START)
        reg.record_job_post(rec.id, question, keyboard=JOB_POST_QUESTION)
        reg.record_job_post(rec.id, plain)
        await _flush(reg)
        row = _rows(tmp_path)[0]
        assert row["dm_posts"] == [[222, 100, 7], [222, 100, 8], [222, 100, 9]]
        assert row["ui_posts"] == [[222, 100, 7, JOB_POST_START],
                                   [222, 100, 8, JOB_POST_QUESTION]]
        again = (await _reload(tmp_path)).get(rec.id)
        assert again.dm_posts == [start, question, plain]
        assert again.ui_posts == [(start, JOB_POST_START),
                                  (question, JOB_POST_QUESTION)]

    async def test_take_ui_posts_hands_back_addresses(self, tmp_path):
        from engagement_registry import JOB_POST_START
        reg = await _registry(tmp_path)
        rec = await reg.create("specialist", "finance", "in_casa", "t",
                               _origin(FINANCE_BOT), **_task(FINANCE_BOT))
        start = TgAddress(FINANCE_BOT, CHAT, 7)
        reg.record_job_post(rec.id, start, keyboard=JOB_POST_START)
        reg._terminal_observer = None
        assert await reg.try_transition_terminal(rec.id, "completed", strict=True)
        again = await _reload(tmp_path)
        [loaded] = again.records_with_ui_posts()
        assert loaded.ui_posts == [(start, JOB_POST_START)]
        assert again.take_ui_posts(loaded.id) == [(start, JOB_POST_START)]

    async def test_an_address_that_names_no_message_is_not_filed(self, tmp_path):
        reg = await _registry(tmp_path)
        rec = await reg.create("specialist", "finance", "in_casa", "t",
                               _origin(CASA_BOT), **_task(CASA_BOT))
        assert reg.record_job_post(rec.id, TgAddress(CASA_BOT, CHAT)) is False
        assert reg.record_job_post(rec.id, 5) is False
        assert rec.dm_posts == []


class TestEngagementStrictLoad:
    @pytest.mark.parametrize("bad", ["missing", None, "x", 0, True, "111"])
    async def test_a_telegram_origin_without_a_valid_route_is_refused(
            self, tmp_path, caplog, bad):
        reg = await _registry(tmp_path)
        rec = await reg.create("specialist", "finance", "in_casa", "t",
                               _origin(CASA_BOT), **_task(CASA_BOT))
        keep = await reg.create("specialist", "finance", "in_casa", "k",
                                _origin(CASA_BOT), **_task(CASA_BOT))
        rows = _rows(tmp_path)
        for row in rows:
            if row["id"] == rec.id:
                if bad == "missing":
                    del row["origin"]["_tg_bot"]
                else:
                    row["origin"]["_tg_bot"] = bad
        _write_rows(tmp_path, rows)
        with caplog.at_level(logging.ERROR, logger="engagement_registry"):
            again = await _reload(tmp_path)
        assert again.get(rec.id) is None
        assert again.get(keep.id) is not None
        assert any(rec.id in r.getMessage() and r.levelname == "ERROR"
                   for r in caplog.records)

    async def test_a_non_telegram_origin_needs_no_route(self, tmp_path):
        reg = await _registry(tmp_path)
        origin = _origin(None)
        origin["channel"] = "voice"
        rec = await reg.create("specialist", "finance", "in_casa", "t", origin)
        again = await _reload(tmp_path)
        assert again.get(rec.id) is not None

    async def test_a_post_without_its_bot_is_refused_and_the_record_kept(
            self, tmp_path, caplog):
        from engagement_registry import JOB_POST_START
        reg = await _registry(tmp_path)
        rec = await reg.create("specialist", "finance", "in_casa", "t",
                               _origin(CASA_BOT), **_task(CASA_BOT))
        rows = _rows(tmp_path)
        rows[0]["dm_posts"] = [5, [100, 6], [None, 100, 7], [111, 100, 8]]
        rows[0]["ui_posts"] = [[5, JOB_POST_START], [None, 100, 7, JOB_POST_START],
                               [111, 100, 8, "nope"], [111, 100, 8, JOB_POST_START]]
        _write_rows(tmp_path, rows)
        with caplog.at_level(logging.ERROR, logger="engagement_registry"):
            again = await _reload(tmp_path)
        loaded = again.get(rec.id)
        assert loaded.dm_posts == [TgAddress(111, 100, 8)]
        assert loaded.ui_posts == [(TgAddress(111, 100, 8), JOB_POST_START)]
        refused = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(refused) == 3 + 3
        assert again.job_post_owner(TgAddress(111, 100, 8)) is loaded


# ---------------------------------------------------------------------------
# The job journal's notice
# ---------------------------------------------------------------------------

@pytest.fixture
def inbox(tmp_path, monkeypatch):
    import job_inbox
    monkeypatch.setattr(job_inbox, "INBOX_ROOT", tmp_path / "job-inbox")
    return job_inbox


class TestJournalNotice:
    def test_an_addressed_notice_round_trips(self, inbox):
        notice = {"address": [FINANCE_BOT, CHAT, 31], "answer": False}
        inbox.append("e1", uuid="u1", text="go on", notice=notice)
        [entry] = inbox.load("e1")
        assert entry["notice"] == notice
        assert inbox.notice_address(entry["notice"]) == TgAddress(
            FINANCE_BOT, CHAT, 31)

    @pytest.mark.parametrize("notice", [
        {"chat_id": CHAT, "message_id": 31, "answer": False},
        {"address": [None, CHAT, 31], "answer": False},
        {"address": [CHAT, 31], "answer": False},
        {"address": [FINANCE_BOT, CHAT, None], "answer": False},
        {"address": [CASA, CHAT, 31], "answer": False},
    ])
    def test_a_notice_without_its_address_is_refused_at_append(
            self, inbox, caplog, notice):
        with caplog.at_level(logging.ERROR, logger="job_inbox"):
            inbox.append("e1", uuid="u1", text="go on", notice=notice)
        [entry] = inbox.load("e1")
        # the message is still owed to the job; only its notice is refused
        assert entry["text"] == "go on" and entry["notice"] is None
        assert any(r.levelname == "ERROR" for r in caplog.records)

    def test_a_stored_notice_without_its_address_is_refused_at_load(
            self, inbox, caplog):
        path = inbox.path_for("e1")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps([
            {"uuid": "u1", "text": "a", "taken_up": False,
             "notice": {"chat_id": CHAT, "message_id": 31, "answer": False}},
            {"uuid": "u2", "text": "b", "taken_up": False,
             "notice": {"address": [CASA_BOT, CHAT, 32], "answer": True}},
            {"uuid": "u3", "text": "c", "taken_up": False, "notice": None},
        ]))
        with caplog.at_level(logging.ERROR, logger="job_inbox"):
            entries = inbox.load("e1")
        assert [e["uuid"] for e in entries] == ["u1", "u2", "u3"]
        assert entries[0]["notice"] is None
        assert entries[1]["notice"] == {"address": [CASA_BOT, CHAT, 32],
                                        "answer": True}
        assert entries[2]["notice"] is None
        assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1


# ---------------------------------------------------------------------------
# Delegation rows (jobs.json) and the one rebuilder
# ---------------------------------------------------------------------------

def _voice_job(**changes):
    from job_registry import DeliveryState, ExecutionState, VoiceJob
    from personality_types import SpeakerProvenance
    from task_model import Requester
    system = SpeakerProvenance(speaker_kind="system")
    base = VoiceJob(
        id="job-1", parent_job_id=None,
        creating_speaker=system, executing_speaker=system,
        creating_role="assistant", specialist_role="finance",
        specialist_display_name="Finance",
        creator_peer="telegram", creator_user_id="100",
        scope_id=str(CHAT), origin_route_id="cid-1", origin_device_id=None,
        task="what is my balance", context="",
        created_at=100.0, started_at=101.0, terminal_at=None,
        expires_at=None, execution_state=ExecutionState.RUNNING,
        delivery_state=DeliveryState.NONE,
        result=None, failure=None, awaiting_input=False,
        continuable_until=None, delivery_sequence=0,
        delivery_attempt_id=None, lease_until=None, cancel_pending=False,
        route={"_tg_bot": CASA_BOT},
        executor="finance",
        requester=Requester("assistant", {"_tg_bot": CASA_BOT}),
        delivery=TgAddress(CASA_BOT, CHAT),
    )
    return replace(base, **changes)


async def _jobs(tmp_path, *jobs):
    from job_registry import JobRegistry
    registry = JobRegistry(tmp_path / "jobs.json", clock=lambda: 200.0)
    await registry.load()
    for job in jobs:
        await registry.create(job)
    return registry


async def _jobs_reloaded(tmp_path):
    from job_registry import JobRegistry
    registry = JobRegistry(tmp_path / "jobs.json", clock=lambda: 200.0)
    await registry.load()
    return registry


class TestDelegationRoute:
    @pytest.mark.parametrize("route", [CASA_BOT, FINANCE_BOT, CASA])
    async def test_the_route_round_trips_through_jobs_json(self, tmp_path, route):
        await _jobs(tmp_path, _voice_job(route={"_tg_bot": route}))
        rows = json.loads((tmp_path / "jobs.json").read_text())
        assert rows[0]["route"] == {"_tg_bot": route}
        again = await _jobs_reloaded(tmp_path)
        assert again.get("job-1").route == {"_tg_bot": route}

    @pytest.mark.parametrize("bad", ["missing-route", "missing-key", None, "x",
                                     0, True])
    async def test_a_telegram_row_without_a_valid_route_is_refused(
            self, tmp_path, caplog, bad):
        await _jobs(tmp_path, _voice_job(),
                    _voice_job(id="job-2", delivery_sequence=1))
        rows = json.loads((tmp_path / "jobs.json").read_text())
        for row in rows:
            if row["id"] == "job-1":
                if bad == "missing-route":
                    del row["route"]
                elif bad == "missing-key":
                    row["route"] = {}
                else:
                    row["route"] = {"_tg_bot": bad}
        (tmp_path / "jobs.json").write_text(json.dumps(rows))
        with caplog.at_level(logging.ERROR, logger="job_registry"):
            again = await _jobs_reloaded(tmp_path)
        assert again.get("job-1") is None
        assert again.get("job-2") is not None
        assert any("job-1" in r.getMessage() and r.levelname == "ERROR"
                   for r in caplog.records)

    async def test_a_voice_row_needs_no_route(self, tmp_path):
        await _jobs(tmp_path, _voice_job(creator_peer="voice_speaker", route={}))
        again = await _jobs_reloaded(tmp_path)
        assert again.get("job-1").route == {}

    async def test_register_delegation_stores_the_creating_turns_route(
            self, tmp_path):
        from job_registry import JobRegistry
        from specialist_registry import DelegationRecord, SpecialistRegistry
        reg = SpecialistRegistry(
            str(tmp_path / "specialists"),
            job_registry=JobRegistry(tmp_path / "jobs.json"))
        await reg.register_delegation(DelegationRecord(
            id="d-1", agent="finance", started_at=1.0,
            origin={"role": "assistant", "channel": "telegram",
                    "chat_id": "100", "cid": "c", "_tg_bot": CASA,
                    "user_text": "x"}))
        assert reg.job_registry.get("d-1").route == {"_tg_bot": CASA}
        rows = json.loads((tmp_path / "jobs.json").read_text())
        assert rows[0]["route"] == {"_tg_bot": CASA}


class TestOriginFromJob:
    def test_it_carries_the_stored_route_not_the_roles_binding(self, two_endpoints):
        """The row says 111; the creating role is bound to 222 now. The
        rebuilt origin still routes through 111."""
        from specialist_registry import origin_from_job
        job = _voice_job(creating_role="finance",
                         route={"_tg_bot": CASA_BOT})
        assert two_endpoints.transport.route_for("finance") == FINANCE_BOT
        origin = origin_from_job(job)
        assert origin["_tg_bot"] == CASA_BOT
        assert two_endpoints.transport.endpoint_for_route(
            origin, site="test") is two_endpoints.casa

    def test_a_casa_route_is_returned_unchanged(self, two_endpoints):
        from specialist_registry import origin_from_job
        origin = origin_from_job(_voice_job(route={"_tg_bot": CASA}))
        assert origin["_tg_bot"] == CASA
        assert two_endpoints.transport.endpoint_for_route(
            origin, site="test") is two_endpoints.casa

    def test_it_is_the_union_of_both_former_rebuilders(self):
        from specialist_registry import origin_from_job
        job = _voice_job(origin_route_id=None, origin_device_id="dev",
                         scheduled_delivery=True, output_note="a note")
        assert origin_from_job(job) == {
            "role": "assistant", "channel": "telegram", "chat_id": "100",
            "cid": "-", "device_id": "dev", "user_id": "100",
            "user_text": "what is my balance", "_scheduled_delivery": True,
            "_inherited_note": "a note", "_tg_bot": CASA_BOT,
        }

    def test_a_routeless_row_rebuilds_a_routeless_origin(self):
        from specialist_registry import origin_from_job
        origin = origin_from_job(_voice_job(creator_peer="voice_speaker",
                                            route={}))
        assert "_tg_bot" not in origin

    async def test_orphans_from_disk_use_it(self, tmp_path):
        from specialist_registry import SpecialistRegistry
        await _jobs(tmp_path, _voice_job(route={"_tg_bot": FINANCE_BOT}))
        # A restart orphans the still-running row.
        jobs = await _jobs_reloaded(tmp_path)
        await jobs.recover_after_restart()
        reg = SpecialistRegistry(str(tmp_path / "specialists"),
                                 job_registry=jobs)
        [orphan] = reg.orphans_from_disk()
        assert orphan.origin["_tg_bot"] == FINANCE_BOT


from two_endpoints import two_endpoints  # noqa: E402,F401  (fixture)
