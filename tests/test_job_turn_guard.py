"""The turn-end guard, parking, the one automatic resume and the boot resume
of a background job (``background_jobs.JobRunner``).

A job is one long session: Casa never ends it on its own judgement. When a
turn ends without the job finishing, Casa continues it (a step-ceiling stop
after reported progress, or one retry after an API fault), nudges it once,
or parks it — it then waits for the operator, who gets one line in their
direct chat with Cancel. The collaborators are small fakes that record what
the runner asked of them; the real driver is exercised in
``test_job_session_driver.py``."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ResultMessage

import background_jobs as bj
import job_inbox
from error_kinds import ErrorKind

pytestmark = pytest.mark.asyncio


def _result(subtype: str = "success") -> ResultMessage:
    return ResultMessage(
        subtype=subtype, duration_ms=1, duration_api_ms=1,
        is_error=subtype != "success", num_turns=1, session_id="sess-1",
        total_cost_usd=0.5, usage={}, result="")


class Driver:
    def __init__(self):
        self.writes: list[str] = []
        self.unread = 0
        self.reserved = 0
        self.recovered: list[dict] = []
        self.recover_error: BaseException | None = None

    def submit_casa(self, rec, text, **kw):
        self.writes.append(text)

    def inbound_unread_depth(self, eid):
        return self.unread

    def inbound_reservations(self, eid):
        return self.reserved

    async def recover(self, rec, **kw):
        self.recovered.append(kw)
        if self.recover_error is not None:
            raise self.recover_error


class Registry:
    def __init__(self, rec):
        self.rec = rec
        self.persists = 0
        self.pending = False
        self.promoted: list[str] = []

    def get(self, eid):
        return self.rec if eid == self.rec.id else None

    async def persist_origin(self, eid):
        self.persists += 1

    def terminal_pending(self, eid):
        return self.pending

    async def promote_from_quiet(self, eid):
        self.promoted.append(eid)
        self.rec.origin["job"]["quiet"] = False
        return True


class Channel:
    def __init__(self):
        self.parked: list[str] = []
        self.refreshed = 0
        self.notices: list[str] = []

    async def post_job_parked(self, rec, text):
        self.parked.append(text)
        return 42

    async def refresh_job_start_line(self, rec):
        self.refreshed += 1

    async def _post_engagement_notice(self, rec, text):
        self.notices.append(text)


def _rec(kind="specialist", **job):
    return SimpleNamespace(
        id="j" * 32, kind=kind, role_or_type="finance", status="active",
        task="Check the accounts",
        started_at=1000.0, completed_at=None, topic_id=7,
        origin={"job": {"title": "Accounting check", "skill": "q:job",
                        "brief_uuid": "b-1", "context": "", **job}})


@pytest.fixture
def rig(monkeypatch):
    rec = _rec()
    reg, drv, ch = Registry(rec), Driver(), Channel()
    from agent_log_double import RecordingLogs
    logs = RecordingLogs()
    runner = bj.JobRunner(registry=reg, channel=ch, driver=drv, agent_logs=logs)
    bj.configure(runner)
    finalized: list[dict] = []

    async def finalize(engagement, **kw):
        finalized.append(kw)
        engagement.status = kw["outcome"]

    import tools
    monkeypatch.setattr(tools, "_finalize_engagement", finalize)
    monkeypatch.setattr(bj, "FAULT_RETRY_S", 0.0)
    return SimpleNamespace(rec=rec, reg=reg, drv=drv, ch=ch, runner=runner,
                           finalized=finalized, logs=logs)


async def settle(n: int = 20) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


# -- INV-BGJOB-017: nudge once, then park -----------------------------------

async def test_a_turn_ended_unfinished_is_nudged_once_then_parked(rig):
    rig.runner.turn_ended(rig.rec, _result())
    assert rig.drv.writes == [bj._NUDGE_ASK]
    assert bj.awaiting(rig.rec) is None
    rig.runner.turn_ended(rig.rec, _result())
    await settle()
    assert rig.drv.writes == [bj._NUDGE_ASK]          # no second nudge
    assert bj.awaiting(rig.rec) == bj.AWAITING_STOPPED
    assert rig.ch.parked == [
        "«Accounting check» stopped without finishing. Reply to continue it, "
        "or Cancel."]
    assert rig.rec.status == "active" and rig.finalized == []


async def test_the_parking_line_carries_no_model_text(rig):
    """The parking line goes to the direct chat as a plain send, so it is
    Casa's own text only: the job's progress summary (its words) stays in
    its topic."""
    rig.rec.origin["job"]["last_summary"] = "12 of 40 rows classified"
    rig.runner.turn_ended(rig.rec, _result())
    rig.runner.turn_ended(rig.rec, _result())
    await settle()
    assert rig.ch.parked == [
        "«Accounting check» stopped without finishing. Reply to continue it, "
        "or Cancel."]


async def test_operator_input_resets_the_nudge(rig):
    rig.runner.turn_ended(rig.rec, _result())
    bj.note_operator_input(rig.rec)
    rig.runner.turn_ended(rig.rec, _result())
    assert rig.drv.writes == [bj._NUDGE_ASK, bj._NUDGE_ASK]
    assert bj.awaiting(rig.rec) is None


async def test_a_plugin_worker_is_nudged_without_ask_operator(rig):
    rig.rec.kind = "plugin"
    rig.runner.turn_ended(rig.rec, _result())
    assert rig.drv.writes == [bj._NUDGE_NO_ASK]
    assert "ask_operator" not in bj._NUDGE_NO_ASK


async def test_a_waiting_job_is_not_nudged(rig):
    for kind in (bj.AWAITING_ASKED, bj.AWAITING_APPROVAL, bj.AWAITING_STOPPED):
        rig.rec.origin["job"][bj.AWAITING_KEY] = kind
        rig.runner.turn_ended(rig.rec, _result())
        rig.runner.turn_ended(rig.rec, _result())
    await settle()
    assert rig.drv.writes == [] and rig.ch.parked == []


async def test_queued_operator_input_is_not_a_stop(rig):
    """A message admitted or written and not yet taken up runs as the next
    turn; the turn end is not a stop."""
    rig.drv.unread = 1
    rig.runner.turn_ended(rig.rec, _result())
    rig.runner.turn_ended(rig.rec, _result())
    rig.drv.unread, rig.drv.reserved = 0, 1
    rig.runner.turn_ended(rig.rec, _result())
    await settle()
    assert rig.drv.writes == [] and rig.ch.parked == []


async def test_operator_input_clears_awaiting(rig):
    rig.rec.origin["job"][bj.AWAITING_KEY] = bj.AWAITING_STOPPED
    bj.note_operator_input(rig.rec)
    await settle()
    assert bj.awaiting(rig.rec) is None
    assert rig.reg.persists >= 1


async def test_a_terminal_or_terminal_pending_job_is_left_alone(rig):
    rig.reg.pending = True
    rig.runner.turn_ended(rig.rec, _result())
    rig.reg.pending = False
    rig.rec.status = "completed"
    rig.runner.turn_ended(rig.rec, _result("error_max_turns"))
    await settle()
    assert rig.drv.writes == [] and rig.ch.parked == []


async def test_a_record_that_is_not_a_job_is_ignored(rig):
    del rig.rec.origin["job"]
    rig.runner.turn_ended(rig.rec, _result())
    assert rig.drv.writes == []


# -- the step ceiling ----------------------------------------------------------

async def test_a_ceiling_after_progress_continues(rig):
    bj.note_progress(rig.rec.id)
    rig.runner.turn_ended(rig.rec, _result("error_max_turns"))
    assert rig.drv.writes == [bj.CONTINUE_LINE]
    assert bj.awaiting(rig.rec) is None


async def test_a_ceiling_without_progress_since_the_last_one_parks(rig):
    bj.note_progress(rig.rec.id)
    rig.runner.turn_ended(rig.rec, _result("error_max_turns"))
    rig.runner.turn_ended(rig.rec, _result("error_max_turns"))
    await settle()
    assert rig.drv.writes == [bj.CONTINUE_LINE]
    assert bj.awaiting(rig.rec) == bj.AWAITING_STOPPED
    assert len(rig.ch.parked) == 1 and rig.finalized == []


async def test_a_first_ceiling_with_no_progress_parks(rig):
    rig.runner.turn_ended(rig.rec, _result("error_max_turns"))
    await settle()
    assert rig.drv.writes == [] and len(rig.ch.parked) == 1


# -- API faults ----------------------------------------------------------------

async def test_an_api_fault_is_continued_once_then_parks(rig):
    rig.runner.turn_ended(rig.rec, _result(), ErrorKind.RATE_LIMIT)
    await settle()
    assert rig.drv.writes == [bj.CONTINUE_LINE]
    rig.runner.turn_ended(rig.rec, _result(), ErrorKind.RATE_LIMIT)
    await settle()
    assert rig.drv.writes == [bj.CONTINUE_LINE]
    assert rig.ch.parked == [
        f"«Accounting check» stopped on an error from Claude "
        f"({ErrorKind.RATE_LIMIT.value}). Reply to continue it, or Cancel."]
    assert rig.rec.status == "active"


async def test_a_successful_turn_resets_the_fault_count(rig):
    rig.runner.turn_ended(rig.rec, _result(), ErrorKind.RATE_LIMIT)
    await settle()
    rig.rec.origin["job"][bj.AWAITING_KEY] = bj.AWAITING_ASKED
    rig.runner.turn_ended(rig.rec, _result())            # healthy turn
    rig.runner.turn_ended(rig.rec, _result(), ErrorKind.RATE_LIMIT)
    await settle()
    assert rig.drv.writes == [bj.CONTINUE_LINE, bj.CONTINUE_LINE]
    assert rig.ch.parked == []


async def test_operator_input_during_the_fault_wait_cancels_the_continue(
        rig, monkeypatch):
    monkeypatch.setattr(bj, "FAULT_RETRY_S", 0.05)
    rig.runner.turn_ended(rig.rec, _result(), ErrorKind.RATE_LIMIT)
    bj.note_operator_input(rig.rec)
    await asyncio.sleep(0.1)
    assert rig.drv.writes == []


# -- parking -------------------------------------------------------------------

async def test_a_quiet_run_that_parks_stops_being_quiet(rig):
    rig.rec.origin["job"]["quiet"] = True
    rig.runner.park(rig.rec, "parked")
    await settle()
    assert rig.reg.promoted == [rig.rec.id]
    assert rig.ch.parked == ["parked"]


async def test_cost_never_ends_or_parks_a_job(rig):
    """INV-BGJOB-020: nothing reads the cost to decide anything."""
    rig.rec.origin["job"]["cost_usd"] = 10_000.0
    rig.runner.turn_ended(rig.rec, _result())
    await settle()
    assert rig.drv.writes == [bj._NUDGE_ASK] and rig.ch.parked == []


async def test_each_turn_end_persists_and_refreshes_the_start_line(rig):
    rig.runner.turn_ended(rig.rec, _result())
    await settle()
    assert rig.reg.persists >= 1 and rig.ch.refreshed == 1


# -- INV-BGJOB-018: one automatic resume ----------------------------------------

async def test_a_session_death_is_resumed_once_with_the_restart_line(rig):
    await rig.runner.session_lost(rig.rec)
    assert rig.drv.recovered == [{
        "restart_line": bj.SESSION_RESTART_LINE,
        "brief": bj.job_brief(rig.rec),
        "redelivery_prefix": bj.SESSION_RESTART_PREFIX}]
    assert rig.finalized == []


async def test_a_second_death_ends_the_job_with_its_last_progress(rig):
    """The second death ends the job ``error`` through the finalize funnel,
    which appends the job's last progress itself — the runner hands it the
    reason only, so the progress line appears once."""
    rig.rec.origin["job"]["last_summary"] = "half done"
    await rig.runner.session_lost(rig.rec)
    await rig.runner.session_lost(rig.rec)
    assert len(rig.drv.recovered) == 1
    assert rig.finalized[0]["outcome"] == "error"
    assert rig.finalized[0]["text"] == "its session ended unexpectedly"


async def test_a_failed_resume_ends_the_job(rig):
    rig.drv.recover_error = RuntimeError("no such session")
    await rig.runner.session_lost(rig.rec)
    assert rig.finalized[0]["outcome"] == "error"
    assert rig.finalized[0]["text"].startswith("its session ended unexpectedly")


# -- boot ------------------------------------------------------------------------

async def test_boot_resumes_a_working_job_with_the_restart_line(rig):
    await rig.runner.resume_at_boot(rig.rec)
    # One line in the job's log; nothing is posted to Telegram.
    from agent_logs import LogKey
    assert rig.logs.lines == [(LogKey("agent", "finance"), "Accounting check",
                               "↻ Resuming after a restart")]
    assert rig.ch.notices == []
    assert rig.drv.recovered == [{
        "restart_line": bj.CASA_RESTART_LINE, "brief": bj.job_brief(rig.rec),
        "redelivery_prefix": bj.CASA_RESTART_PREFIX}]


async def test_boot_skips_a_job_that_ended_before_its_turn_in_the_walk(rig):
    """A Cancel lands while boot is still resuming earlier jobs: the job's
    turn in the walk comes with a stale record, and the live one decides —
    no resume notice, no recovery."""
    stale = copy.copy(rig.rec)
    rig.rec.status = "cancelled"
    await rig.runner.resume_at_boot(stale)
    assert rig.logs.lines == [] and rig.drv.recovered == []


async def test_boot_leaves_a_waiting_job_with_an_empty_journal_alone(rig):
    rig.rec.origin["job"][bj.AWAITING_KEY] = bj.AWAITING_ASKED
    await rig.runner.resume_at_boot(rig.rec)
    assert rig.drv.recovered == [] and rig.logs.lines == []


async def test_boot_resumes_a_waiting_job_whose_journal_has_entries(rig):
    rig.rec.origin["job"][bj.AWAITING_KEY] = bj.AWAITING_STOPPED
    job_inbox.append(rig.rec.id, uuid="u-1", text="go on", notice=None)
    await rig.runner.resume_at_boot(rig.rec)
    assert len(rig.drv.recovered) == 1


async def test_a_job_not_resumable_after_a_restart_ends_error(rig):
    rig.drv.recover_error = RuntimeError("gone")
    await rig.runner.resume_at_boot(rig.rec)
    assert rig.finalized[0]["outcome"] == "error"
    assert rig.finalized[0]["text"] == "could not be resumed after a restart"


# -- the brief and the start line ----------------------------------------------

async def test_the_brief_has_no_batch_wording_and_names_the_starter():
    rec = _rec(started_by="operator", context="Q3 only")
    brief = bj.job_brief(rec)
    lines = brief.splitlines()
    assert lines[0] == 'You are running the background job "Accounting check" (skill q:job).'
    assert lines[1] == f"Job id: {rec.id}"
    assert lines[2] == "Started by: operator"
    assert "Request: Check the accounts" in brief and "Context: Q3 only" in brief
    assert "batch" not in brief.lower()
    assert "ask_operator" in brief
    rec.kind = "plugin"
    assert "ask_operator" not in bj.job_brief(rec)


@pytest.mark.parametrize("seconds,text", [
    (30, "1 min"), (40 * 60, "40 min"), (65 * 60, "1 h 5 min"), (120 * 60, "2 h")])
async def test_elapsed_time_reads_like_the_operator_says_it(seconds, text):
    assert bj.format_elapsed(seconds) == text


async def test_the_cost_suffix_shows_cost_only_after_a_turn_end():
    rec = _rec()
    assert bj.cost_suffix(rec, now=1000.0 + 40 * 60) == " · 40 min"
    rec.origin["job"]["cost_usd"] = 2.4
    assert bj.cost_suffix(rec, now=1000.0 + 40 * 60) == " · 40 min · $2.40"
