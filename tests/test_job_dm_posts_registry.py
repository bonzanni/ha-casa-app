"""A background job's direct-chat posts and questions on its engagement record.

The record, not the memory-only post map, says which live job owns a DM
message: posts are filed on the record (uncapped while it lives), indexed,
persisted, rebuilt at load and dropped by the write that settles the terminal
transition — and a record whose strict terminal write is still pending counts
as live. Also here: the random question ids, the keyboard posts kept past the
end until taken once, the strict quiet-run promotion, and the owed-telling
decision made inside the terminal transition.

REAL ``EngagementRegistry`` on a tmp tombstone; writes are held or failed by
replacing the instance's ``_write_tombstone`` (the to_thread body), never by
patching ``asyncio``.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from channels.tg_address import TgAddress

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def A(mid: int, *, chat: int = 100, bot: int = 111) -> TgAddress:
    """A post's address: bot 111 in the operator's chat 100, by default."""
    return TgAddress(bot, chat, mid)


def _job(quiet: bool = False) -> dict:
    import background_jobs as jobs
    decl = jobs.JobDecl("sample:work", "sample", "work", "sample:work",
                        "Process rows", None, quiet_when_scheduled=quiet)
    return jobs.initial_job_state(
        decl, started_by="scheduled" if quiet else "operator")


async def _job_record(tmp_path, *, quiet: bool = False, job: bool = True):
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    origin = {"role": "assistant", "channel": "telegram", "chat_id": "100",
              "user_id": 100, "_tg_bot": 111}
    if job:
        origin["job"] = _job(quiet)
    rec = await reg.create("specialist", "finance", "in_casa", "t", origin)
    return reg, rec


async def _flush(reg):
    """Wait for every scheduled tombstone write to land."""
    while reg._deferred_persists:
        await asyncio.gather(*list(reg._deferred_persists),
                             return_exceptions=True)


def _disk(tmp_path, rec_id):
    rows = json.loads((tmp_path / "e.json").read_text())
    return next(r for r in rows if r["id"] == rec_id)


class _HeldWrite:
    """Replace a registry's synchronous tombstone body with one that blocks
    until released, then either writes or raises."""

    def __init__(self, reg):
        self.reg = reg
        self.real = reg._write_tombstone
        self.started = threading.Event()
        self.release = threading.Event()
        self.fail = False
        reg._write_tombstone = self._write

    def _write(self, snapshot):
        self.started.set()
        self.release.wait(5)
        if self.fail:
            raise OSError("disk full")
        self.real(snapshot)

    def restore(self):
        self.reg._write_tombstone = self.real


async def _wait_started(held):
    for _ in range(500):
        if held.started.is_set():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the write never started")


class TestFilingAndIndex:
    async def test_a_live_jobs_post_is_filed_indexed_and_persisted(self, tmp_path):
        from engagement_registry import JOB_POST_START
        reg, rec = await _job_record(tmp_path)
        assert reg.record_job_post(rec.id, A(501), keyboard=JOB_POST_START) is True
        assert reg.record_job_post(rec.id, A(502)) is True
        assert reg.job_post_owner(A(501)) is rec
        assert reg.job_post_owner(A(502)) is rec
        await _flush(reg)
        row = _disk(tmp_path, rec.id)
        assert row["dm_posts"] == [[111, 100, 501], [111, 100, 502]]
        assert row["ui_posts"] == [[111, 100, 501, JOB_POST_START]]

    async def test_posts_are_uncapped_while_the_job_lives(self, tmp_path):
        reg, rec = await _job_record(tmp_path)
        for mid in range(1, 5001):
            assert reg.record_job_post(rec.id, A(mid))
        assert len(rec.dm_posts) == 5000
        assert reg.job_post_owner(A(1)) is rec

    async def test_a_record_that_is_not_a_job_files_nothing(self, tmp_path):
        reg, rec = await _job_record(tmp_path, job=False)
        assert reg.record_job_post(rec.id, A(501)) is False
        assert reg.job_post_owner(A(501)) is None
        assert reg.record_job_post("no-such-id", A(502)) is False

    async def test_the_index_is_rebuilt_from_live_records_at_load(self, tmp_path):
        from engagement_registry import EngagementRegistry
        reg, rec = await _job_record(tmp_path)
        reg.record_job_post(rec.id, A(501))
        qid = reg.new_job_question(rec.id, "Which account?", ["A", "B"])
        await _flush(reg)
        again = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        await again.load()
        loaded = again.job_post_owner(A(501))
        assert loaded is not None and loaded.id == rec.id
        assert loaded.questions[qid] == {"text": "Which account?",
                                         "options": ["A", "B"]}

    async def test_settling_the_terminal_transition_drops_posts_and_questions(
            self, tmp_path):
        from engagement_registry import EngagementRegistry, JOB_POST_QUESTION
        reg, rec = await _job_record(tmp_path)
        reg.record_job_post(rec.id, A(501), keyboard=JOB_POST_QUESTION)
        reg.new_job_question(rec.id, "Which?", ["A"])
        assert await reg.try_transition_terminal(rec.id, "completed", strict=True)
        assert reg.job_post_owner(A(501)) is None
        # The questions go; the posts stay as the ended job's (#1522).
        assert rec.dm_posts == [A(501)] and rec.questions == {}
        # The keyboard is kept until it is retired.
        assert rec.ui_posts == [(A(501), JOB_POST_QUESTION)]
        row = _disk(tmp_path, rec.id)
        assert row["dm_posts"] == [[111, 100, 501]] and row["questions"] == {}
        assert row["ui_posts"] == [[111, 100, 501, JOB_POST_QUESTION]]
        # A terminal record files nothing more as live: its keyboard is the
        # filer's to retire.
        assert reg.record_job_post(rec.id, A(502), keyboard=JOB_POST_QUESTION) is False
        assert rec.ui_posts == [(A(501), JOB_POST_QUESTION)]
        again = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        await again.load()
        assert again.job_post_owner(A(501)) is None

    async def test_a_direct_mutator_also_drops_the_posts(self, tmp_path):
        reg, rec = await _job_record(tmp_path)
        reg.record_job_post(rec.id, A(501))
        assert await reg.mark_error(rec.id, kind="resume_failed", message="x")
        assert reg.job_post_owner(A(501)) is None


class TestEndedJobPosts:
    """#1522: an ended job's posts stay known as that job's, so a reply to one
    is told the job ended rather than sent where the job cannot be seen."""

    async def test_settlement_moves_the_posts_to_the_ended_index_and_a_reload_keeps_them(
            self, tmp_path):
        from engagement_registry import EngagementRegistry, JOB_POST_START
        reg, rec = await _job_record(tmp_path)
        reg.record_job_post(rec.id, A(501), keyboard=JOB_POST_START)
        reg.record_job_post(rec.id, A(502))
        assert reg.ended_job_post_owner(A(501)) is None          # live: not ended
        assert await reg.try_transition_terminal(rec.id, "completed", strict=True)
        assert reg.job_post_owner(A(501)) is None
        assert reg.ended_job_post_owner(A(501)) is rec
        assert reg.ended_job_post_owner(A(502)) is rec
        assert reg.ended_job_post_owner(A(503)) is None
        assert reg.ended_job_post_owner((111, 100, 501)) is None
        again = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        await again.load()
        loaded = again.ended_job_post_owner(A(501))
        assert loaded is not None and loaded.id == rec.id
        assert again.ended_job_post_owner(A(502)) is loaded
        assert again.job_post_owner(A(501)) is None

    async def test_a_direct_mutator_moves_them_too(self, tmp_path):
        reg, rec = await _job_record(tmp_path)
        reg.record_job_post(rec.id, A(501))
        assert await reg.mark_error(rec.id, kind="resume_failed", message="x")
        assert reg.ended_job_post_owner(A(501)) is rec

    async def test_a_post_landing_after_the_end_is_kept_as_the_ended_jobs(self, tmp_path):
        from engagement_registry import EngagementRegistry, JOB_POST_QUESTION
        reg, rec = await _job_record(tmp_path)
        assert await reg.try_transition_terminal(rec.id, "completed", strict=True)
        # not filed as live: the caller retires a keyboard itself
        assert reg.record_job_post(rec.id, A(777), keyboard=JOB_POST_QUESTION) is False
        assert rec.ui_posts == []
        assert reg.job_post_owner(A(777)) is None
        assert reg.ended_job_post_owner(A(777)) is rec
        await _flush(reg)
        assert _disk(tmp_path, rec.id)["dm_posts"] == [[111, 100, 777]]
        again = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        await again.load()
        assert again.ended_job_post_owner(A(777)).id == rec.id

    async def test_what_is_not_a_job_post_is_still_refused(self, tmp_path):
        reg, rec = await _job_record(tmp_path, job=False)
        assert await reg.try_transition_terminal(rec.id, "completed", strict=True)
        assert reg.record_job_post(rec.id, A(801)) is False
        assert reg.ended_job_post_owner(A(801)) is None
        assert rec.dm_posts == []
        (tmp_path / "two").mkdir()
        reg2, job = await _job_record(tmp_path / "two")
        assert await reg2.try_transition_terminal(job.id, "completed", strict=True)
        assert reg2.record_job_post(job.id, "802") is False
        assert reg2.record_job_post("no-such-id", A(803)) is False
        assert reg2.ended_job_post_owner(A(803)) is None
        assert job.dm_posts == []

    async def test_a_pending_terminal_record_is_live_not_ended(self, tmp_path):
        reg, rec = await _job_record(tmp_path)
        reg.record_job_post(rec.id, A(501))
        await _flush(reg)
        held = _HeldWrite(reg)
        transition = asyncio.ensure_future(
            reg.try_transition_terminal(rec.id, "completed", strict=True))
        await _wait_started(held)
        assert reg.job_post_owner(A(501)) is rec
        assert reg.ended_job_post_owner(A(501)) is None
        assert reg.record_job_post(rec.id, A(502)) is True        # filed as live
        held.release.set()
        assert await transition is True
        held.restore()
        assert reg.ended_job_post_owner(A(502)) is rec

    async def test_a_direct_mutators_unsettled_end_is_ended_in_its_own_chat_only(
            self, tmp_path):
        """A direct terminal mutator flips the status before its write
        settles and the posts move: the post already counts as the ended
        job's, and only at its own address (message ids are per bot and
        chat)."""
        reg, rec = await _job_record(tmp_path)
        reg.record_job_post(rec.id, A(501))
        await _flush(reg)
        held = _HeldWrite(reg)
        cancel = asyncio.ensure_future(reg.mark_cancelled(rec.id))
        await _wait_started(held)
        assert reg.ended_job_post_owner(A(501)) is rec
        assert reg.ended_job_post_owner(A(501, chat=42)) is None
        held.release.set()
        await cancel
        held.restore()
        assert reg.ended_job_post_owner(A(501)) is rec
        assert reg.ended_job_post_owner(A(501, chat=42)) is None

    async def test_a_record_aged_out_of_the_tombstone_leaves_the_index(self, tmp_path):
        import time

        from engagement_registry import _TERMINAL_RETENTION_DAYS, EngagementRegistry
        reg, rec = await _job_record(tmp_path)
        reg.record_job_post(rec.id, A(501))
        old = time.time() - (_TERMINAL_RETENTION_DAYS + 1) * 86400
        assert await reg.try_transition_terminal(
            rec.id, "completed", completed_at=old, strict=True)
        await reg.mark_transcripts_reaped([rec.id])           # the write that prunes it
        again = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
        await again.load()
        assert again.get(rec.id) is None
        assert again.ended_job_post_owner(A(501)) is None


class TestPendingTerminalCountsAsLive:
    async def test_a_pending_record_routes_and_files_and_a_rollback_keeps_them(
            self, tmp_path):
        from engagement_registry import JOB_POST_QUESTION
        reg, rec = await _job_record(tmp_path)
        reg.record_job_post(rec.id, A(501))
        await _flush(reg)
        held = _HeldWrite(reg)
        held.fail = True
        transition = asyncio.ensure_future(
            reg.try_transition_terminal(rec.id, "completed", strict=True))
        await _wait_started(held)
        # Terminal in memory, write pending: still live for routing and filing.
        assert rec.status == "completed"
        assert reg.terminal_pending(rec.id) is True
        assert reg.counts_as_live(rec.id) is True
        assert reg.job_post_owner(A(501)) is rec
        assert reg.record_job_post(rec.id, A(502), keyboard=JOB_POST_QUESTION) is True
        held.release.set()
        with pytest.raises(OSError):
            await transition
        held.restore()
        # Rolled back: live again, both posts still steerable.
        assert rec.status == "active"
        assert reg.job_post_owner(A(501)) is rec
        assert reg.job_post_owner(A(502)) is rec
        assert (A(502), JOB_POST_QUESTION) in rec.ui_posts

    async def test_a_post_filed_while_pending_is_among_the_keyboards_the_end_takes(
            self, tmp_path):
        """The observer runs after the durable write and before the record
        leaves the pending set, so a keyboard filed in that window is taken
        by the terminal-time retirement, never stranded."""
        from engagement_registry import JOB_POST_QUESTION
        reg, rec = await _job_record(tmp_path)
        taken: list = []
        reg.set_terminal_observer(lambda r: taken.extend(reg.take_ui_posts(r.id)))
        held = _HeldWrite(reg)
        transition = asyncio.ensure_future(
            reg.try_transition_terminal(rec.id, "completed", strict=True))
        await _wait_started(held)
        assert reg.record_job_post(rec.id, A(777), keyboard=JOB_POST_QUESTION)
        held.release.set()
        assert await transition is True
        held.restore()
        assert taken == [(A(777), JOB_POST_QUESTION)]
        assert rec.ui_posts == []
        assert reg.terminal_pending(rec.id) is False

    async def test_settle_pending_waits_for_the_write(self, tmp_path):
        reg, rec = await _job_record(tmp_path)
        held = _HeldWrite(reg)
        transition = asyncio.ensure_future(
            reg.try_transition_terminal(rec.id, "completed", strict=True))
        await _wait_started(held)
        waiter = asyncio.ensure_future(reg.settle_pending(rec.id))
        await asyncio.sleep(0.01)
        assert not waiter.done()
        held.release.set()
        await transition
        await asyncio.wait_for(waiter, 5)
        held.restore()
        assert reg.counts_as_live(rec.id) is False


class TestKeyboardsAndQuestions:
    async def test_keyboard_posts_are_taken_exactly_once(self, tmp_path):
        from engagement_registry import JOB_POST_START
        reg, rec = await _job_record(tmp_path)
        reg.record_job_post(rec.id, A(501), keyboard=JOB_POST_START)
        await reg.try_transition_terminal(rec.id, "cancelled", strict=True)
        assert reg.records_with_ui_posts() == [rec]
        assert reg.take_ui_posts(rec.id) == [(A(501), JOB_POST_START)]
        assert reg.take_ui_posts(rec.id) == []
        assert reg.records_with_ui_posts() == []
        await _flush(reg)
        assert _disk(tmp_path, rec.id)["ui_posts"] == []

    async def test_a_question_id_is_redrawn_until_free(self, tmp_path, monkeypatch):
        import engagement_registry
        reg, rec = await _job_record(tmp_path)
        draws = iter(["AAAAAAAA", "AAAAAAAA", "AAAAAAAA", "BBBBBBBB"])
        monkeypatch.setattr(engagement_registry, "_new_question_id",
                            lambda: next(draws))
        first = reg.new_job_question(rec.id, "one?", ["x"])
        second = reg.new_job_question(rec.id, "two?", ["y"])
        assert first == "AAAAAAAA" and second == "BBBBBBBB"
        assert rec.questions[first]["text"] == "one?"
        assert rec.questions[second]["text"] == "two?"

    async def test_question_ids_are_eight_base32_characters(self, tmp_path):
        import re
        reg, rec = await _job_record(tmp_path)
        ids = {reg.new_job_question(rec.id, "q?", []) for _ in range(50)}
        assert len(ids) == 50
        assert all(re.fullmatch(r"[A-Z2-7]{8}", qid) for qid in ids)

    async def test_a_terminal_record_takes_no_question(self, tmp_path):
        reg, rec = await _job_record(tmp_path)
        await reg.try_transition_terminal(rec.id, "completed", strict=True)
        assert reg.new_job_question(rec.id, "q?", []) is None


class TestQuietPromotion:
    async def test_promotion_is_persisted(self, tmp_path):
        reg, rec = await _job_record(tmp_path, quiet=True)
        assert await reg.promote_from_quiet(rec.id) is True
        assert rec.origin["job"]["quiet"] is False
        assert _disk(tmp_path, rec.id)["origin"]["job"]["quiet"] is False

    async def test_a_failed_write_rolls_the_stamp_back_and_refuses(self, tmp_path):
        reg, rec = await _job_record(tmp_path, quiet=True)

        def _fail(snapshot):
            raise OSError("disk full")
        reg._write_tombstone = _fail
        assert await reg.promote_from_quiet(rec.id) is False
        assert rec.origin["job"]["quiet"] is True

    async def test_promotion_after_a_pending_completion_commits_is_refused_and_owes_nothing(
            self, tmp_path):
        """A quiet run completing while the operator interacts: the
        promotion waits for the pending write; the completion committed, so
        nothing is promoted and the end stays told to no one."""
        import tools
        reg, rec = await _job_record(tmp_path, quiet=True)
        held = _HeldWrite(reg)
        transition = asyncio.ensure_future(reg.try_transition_terminal(
            rec.id, "completed", strict=True,
            owes_terminal_notification=lambda r: not tools._told_nowhere(
                r.origin, "completed")))
        await _wait_started(held)
        promotion = asyncio.ensure_future(reg.promote_from_quiet(rec.id))
        await asyncio.sleep(0.01)
        assert not promotion.done()
        held.release.set()
        assert await transition is True
        assert await promotion is False
        held.restore()
        assert rec.origin["job"]["quiet"] is True
        assert rec.terminal_notification_pending is False

    async def test_promotion_after_a_pending_completion_rolls_back_succeeds(
            self, tmp_path):
        reg, rec = await _job_record(tmp_path, quiet=True)
        held = _HeldWrite(reg)
        held.fail = True
        transition = asyncio.ensure_future(
            reg.try_transition_terminal(rec.id, "completed", strict=True))
        await _wait_started(held)
        promotion = asyncio.ensure_future(reg.promote_from_quiet(rec.id))
        await asyncio.sleep(0.01)
        held.release.set()
        with pytest.raises(OSError):
            await transition
        held.fail = False
        assert await promotion is True
        held.restore()
        assert rec.status == "active"
        assert rec.origin["job"]["quiet"] is False


class TestOwedDecisionInsideTheTransition:
    async def test_the_decision_reads_the_stamp_the_transition_commits(self, tmp_path):
        import tools
        reg, rec = await _job_record(tmp_path, quiet=True)
        assert await reg.promote_from_quiet(rec.id) is True
        assert await reg.try_transition_terminal(
            rec.id, "completed", strict=True,
            owes_terminal_notification=lambda r: not tools._told_nowhere(
                r.origin, "completed"))
        assert rec.terminal_notification_pending is True

    async def test_a_quiet_completion_owes_nothing(self, tmp_path):
        import tools
        reg, rec = await _job_record(tmp_path, quiet=True)
        assert await reg.try_transition_terminal(
            rec.id, "completed", strict=True,
            owes_terminal_notification=lambda r: not tools._told_nowhere(
                r.origin, "completed"))
        assert rec.terminal_notification_pending is False
