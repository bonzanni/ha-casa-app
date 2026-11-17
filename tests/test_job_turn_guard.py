"""The turn-end guard, parking, the job's derived state, the silent watch,
the one automatic resume and the boot resume of a background job
(``background_jobs.JobRunner``).

A job is one long session: Casa never ends it on its own judgement. When a
turn ends without the job finishing, Casa continues it (a step-ceiling stop,
up to a generous backstop, or one retry after an API fault), nudges it once,
or parks it — it then waits for the operator, who gets one line in their
direct chat with Cancel. A job whose open turn shows no activity for an hour
is told to the operator once and left alone. The collaborators are small
fakes that record what the runner asked of them; the real driver is exercised
in ``test_job_session_driver.py``."""

from __future__ import annotations

import asyncio
import copy
import time
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ResultMessage

import background_jobs as bj
import job_inbox
from channels.tg_address import TgAddress
from drivers.in_casa_driver import JobFacts
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
        self.facts: JobFacts | None = None

    def submit_casa(self, rec, text, **kw):
        self.writes.append(text)

    def job_facts(self, eid):
        return self.facts

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
        self.live: list | None = None       # the walk's records, when a test sets them

    def get(self, eid):
        return self.rec if eid == self.rec.id else None

    def active_and_idle(self):
        if self.live is not None:
            return list(self.live)
        return [self.rec] if self.rec.status in ("active", "idle") else []

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
        self.attempts: list[str] = []       # every send that was tried
        self.parked: list[str] = []         # the lines that landed
        self.notices: list[str] = []
        self.mid: int | None = 42           # None: the send lands nothing
        self.error: BaseException | None = None
        self.hold: asyncio.Event | None = None

    async def post_job_parked(self, rec, text):
        self.attempts.append(text)
        if self.hold is not None:
            await self.hold.wait()
        if self.error is not None:
            raise self.error
        if self.mid is not None:
            self.parked.append(text)
        return self.mid

    def endpoint(self, bot_id):
        # the transport's endpoint for the job's delivery bot: this one
        return self

    def __getattr__(self, name):
        # Any other post or send is recorded, so "nothing posted" is checked.
        if not name.lstrip("_").startswith(("send", "post", "deliver")):
            raise AttributeError(name)

        async def _record(*a, **kw):
            self.notices.append((name, a, kw))
        return _record


def _rec(kind="specialist", **job):
    return SimpleNamespace(
        id="j" * 32, kind=kind, role_or_type="finance", status="active",
        task="Check the accounts",
        started_at=1000.0, completed_at=None,
        delivery=TgAddress(111, 42),          # the job's lines go to the operator's chat
        origin={"job": {"title": "Accounting check", "skill": "q:job",
                        "brief_uuid": "b-1", "context": "", **job}})


@pytest.fixture
def rig(monkeypatch):
    rec = _rec()
    reg, drv, ch = Registry(rec), Driver(), Channel()
    runner = bj.JobRunner(registry=reg, channel=ch, driver=drv)
    bj.configure(runner)
    finalized: list[dict] = []

    async def finalize(engagement, **kw):
        finalized.append(kw)
        engagement.status = kw["outcome"]

    import tools
    monkeypatch.setattr(tools, "_finalize_engagement", finalize)
    monkeypatch.setattr(bj, "FAULT_RETRY_S", 0.0)
    return SimpleNamespace(rec=rec, reg=reg, drv=drv, ch=ch, runner=runner,
                           finalized=finalized)


async def settle(n: int = 20) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def until(predicate, timeout: float = 2.0) -> None:
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), timeout)


# -- INV-BGJOB-030: nudge once, then park -----------------------------------

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

CEILING = "error_max_turns"
BACKSTOP_LINE = (
    "«Accounting check» has run 10,000 steps over 6 h 20 min without "
    "finishing, far more than a job normally needs. Reply to continue it, or "
    "Cancel.")


async def test_a_ceiling_continues_without_any_progress_call(rig):
    """The step ceiling is a continuation boundary whatever the job
    reported: a job that never called ``report_job_progress`` is continued."""
    rig.runner.turn_ended(rig.rec, _result(CEILING))
    rig.runner.turn_ended(rig.rec, _result(CEILING))
    await settle()
    assert rig.drv.writes == [bj.CONTINUE_LINE, bj.CONTINUE_LINE]
    assert bj.awaiting(rig.rec) is None and rig.ch.parked == []


async def test_the_tenth_ceiling_since_operator_input_parks_with_the_backstop_line(rig):
    """The backstop: 10 ceilings in a row with no operator input park the
    job with Casa's own facts (steps and running time) and none of the
    job's words. Parking is not an end."""
    assert bj.CEILINGS_BEFORE_PARK == 10 and bj.JOB_MAX_TURNS == 1000
    rig.rec.started_at = time.time() - (6 * 3600 + 20 * 60)
    rig.rec.origin["job"]["last_summary"] = "12 of 40 rows classified"
    for _ in range(9):
        rig.runner.turn_ended(rig.rec, _result(CEILING))
    await settle()
    assert rig.drv.writes == [bj.CONTINUE_LINE] * 9 and rig.ch.parked == []
    rig.runner.turn_ended(rig.rec, _result(CEILING))
    await settle()
    assert rig.drv.writes == [bj.CONTINUE_LINE] * 9      # no tenth continue
    assert bj.awaiting(rig.rec) == bj.AWAITING_STOPPED
    assert rig.ch.parked == [BACKSTOP_LINE]
    assert rig.rec.status == "active" and rig.finalized == []


async def test_operator_input_resets_the_ceiling_count(rig):
    for _ in range(9):
        rig.runner.turn_ended(rig.rec, _result(CEILING))
    bj.note_operator_input(rig.rec)
    for _ in range(9):
        rig.runner.turn_ended(rig.rec, _result(CEILING))
    await settle()
    assert rig.drv.writes == [bj.CONTINUE_LINE] * 18
    assert bj.awaiting(rig.rec) is None and rig.ch.parked == []


async def test_a_reply_to_the_backstop_continues_the_job_for_another_round(rig):
    for _ in range(10):
        rig.runner.turn_ended(rig.rec, _result(CEILING))
    bj.note_operator_input(rig.rec)                     # the operator's reply
    for _ in range(9):
        rig.runner.turn_ended(rig.rec, _result(CEILING))
    await settle()
    assert rig.drv.writes == [bj.CONTINUE_LINE] * 18 and len(rig.ch.parked) == 1
    assert bj.awaiting(rig.rec) is None


@pytest.mark.parametrize("started_at", [None, 0.0, "yesterday", 6000.0])
async def test_the_backstop_line_without_a_usable_start_time_leaves_the_time_out(
        rig, started_at):
    """No start time, a zero one, a non-number, or one after now."""
    rig.rec.started_at = started_at
    assert bj._backstop_line(rig.rec, 5000.0) == (
        "«Accounting check» has run 10,000 steps without finishing, far more "
        "than a job normally needs. Reply to continue it, or Cancel.")


# -- API faults ----------------------------------------------------------------

async def test_an_api_fault_is_continued_once_then_parks(rig):
    rig.runner.turn_ended(rig.rec, _result(), ErrorKind.RATE_LIMIT)
    await settle()
    assert rig.drv.writes == [bj.CONTINUE_LINE]
    rig.runner.turn_ended(rig.rec, _result(), ErrorKind.RATE_LIMIT)
    await settle()
    assert rig.drv.writes == [bj.CONTINUE_LINE]
    assert rig.ch.parked == [
        f"«Accounting check» stopped on an error "
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


async def test_each_turn_end_persists_the_job(rig):
    rig.runner.turn_ended(rig.rec, _result())
    await settle()
    assert rig.reg.persists >= 1


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
    # Nothing is posted to Telegram.
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
    assert rig.drv.recovered == []


async def test_boot_leaves_a_waiting_job_with_an_empty_journal_alone(rig):
    rig.rec.origin["job"][bj.AWAITING_KEY] = bj.AWAITING_ASKED
    await rig.runner.resume_at_boot(rig.rec)
    assert rig.drv.recovered == []


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


# -- INV-BGJOB-028: one derived state -----------------------------------------------

NOW = 1_000_000.0
FRESH = NOW - 10
STALE = NOW - bj.SILENT_AFTER_S - 1


@pytest.mark.parametrize("status,waiting,facts,state", [
    # ended reads the record's status alone, before everything else
    ("completed", None, JobFacts(True, True, FRESH), bj.JOB_ENDED),
    ("cancelled", "asked", None, bj.JOB_ENDED),
    ("error", None, JobFacts(True, False, STALE), bj.JOB_ENDED),
    # recovering before an open turn and before waiting
    ("active", None, JobFacts(True, True, STALE), bj.JOB_RECOVERING),
    ("active", "asked", JobFacts(False, True, None), bj.JOB_RECOVERING),
    # an open turn: working up to the limit, silent past it
    ("active", None, JobFacts(True, False, FRESH), bj.JOB_WORKING),
    ("active", None, JobFacts(True, False, NOW - bj.SILENT_AFTER_S), bj.JOB_WORKING),
    ("active", None, JobFacts(True, False, STALE), bj.JOB_SILENT),
    ("active", None, JobFacts(True, False, None), bj.JOB_WORKING),
    # an open turn is matched BEFORE waiting: the job asked and works on
    ("active", "asked", JobFacts(True, False, FRESH), bj.JOB_WORKING),
    ("active", "approval", JobFacts(True, False, STALE), bj.JOB_SILENT),
    ("active", "stopped", JobFacts(True, False, FRESH), bj.JOB_WORKING),
    # no open turn: waiting when it waits for the operator, else resting
    ("active", "asked", JobFacts(False, False, FRESH), bj.JOB_WAITING),
    ("idle", "stopped", JobFacts(False, False, None), bj.JOB_WAITING),
    ("active", None, JobFacts(False, False, STALE), bj.JOB_RESTING),
    ("idle", None, JobFacts(False, False, None), bj.JOB_RESTING),
    # no facts reads as no open turn and not recovering
    ("active", None, None, bj.JOB_RESTING),
    ("idle", "approval", None, bj.JOB_WAITING),
])
async def test_the_state_of_a_job_is_the_first_match(status, waiting, facts, state):
    rec = _rec(**({bj.AWAITING_KEY: waiting} if waiting else {}))
    rec.status = status
    assert bj.job_state(rec, facts, NOW) == state


@pytest.mark.parametrize("facts,in_use", [
    (JobFacts(True, False, FRESH), True),       # working
    (JobFacts(True, False, STALE), True),       # silent
    (JobFacts(False, True, None), True),        # recovering
    (JobFacts(False, False, STALE), False),     # resting
    (None, False),
])
async def test_a_working_silent_or_recovering_session_is_in_use(rig, facts, in_use):
    rig.drv.facts = facts
    assert bj.session_in_use(rig.drv, rig.rec, NOW) is in_use


async def test_a_driver_that_gives_no_reading_leaves_the_session_not_in_use(rig):
    """No driver, a driver without the accessor, one that raises, and one
    that returns something that is not the driver's facts."""
    def boom(eid):
        raise RuntimeError("no reading")
    for driver in (None, object(), SimpleNamespace(job_facts=boom),
                   SimpleNamespace(job_facts=lambda eid: SimpleNamespace(
                       turn_open=True, recovering=True, active_ts=FRESH))):
        assert bj.session_in_use(driver, rig.rec, NOW) is False


# -- INV-BGJOB-029: the silent watch -------------------------------------------------

SILENT_LINE = (
    "«Accounting check» has shown no activity for 1 h. It may be stuck on a "
    "slow step. It carries on by itself if the step returns; Cancel stops it.")


def _silent(rig, since: float = STALE) -> float:
    """The job's turn is open and its session last showed activity at
    *since*, more than the limit ago."""
    rig.drv.facts = JobFacts(turn_open=True, recovering=False, active_ts=since)
    return since


async def test_a_silent_job_is_told_once_and_is_not_parked(rig):
    since = _silent(rig)
    rig.runner._watch_once(NOW)
    await settle()
    rig.runner._watch_once(NOW + bj.SILENT_CHECK_S)
    rig.runner._watch_once(NOW + 7200)
    await settle()
    assert rig.ch.attempts == [SILENT_LINE] and rig.ch.parked == [SILENT_LINE]
    assert rig.runner.told == {rig.rec.id: since} and rig.runner.telling == set()
    # Not parked: not marked waiting, nothing written into the session,
    # nothing persisted, nothing ended.
    assert bj.AWAITING_KEY not in rig.rec.origin["job"]
    assert rig.drv.writes == [] and rig.reg.persists == 0
    assert rig.rec.status == "active" and rig.finalized == []


@pytest.mark.parametrize("facts,waiting", [
    (JobFacts(True, False, FRESH), None),                   # working
    (JobFacts(True, False, NOW - bj.SILENT_AFTER_S), None),  # at the limit
    (JobFacts(False, False, STALE), None),                  # resting, old time
    (JobFacts(False, False, STALE), bj.AWAITING_STOPPED),   # parked
    (JobFacts(True, True, STALE), None),                    # recovering
    (None, bj.AWAITING_ASKED),                              # suspended, waiting
])
async def test_a_job_that_is_not_silent_is_not_told(rig, facts, waiting):
    rig.drv.facts = facts
    if waiting:
        rig.rec.origin["job"][bj.AWAITING_KEY] = waiting
    rig.runner._watch_once(NOW)
    await settle()
    assert rig.ch.attempts == [] and rig.runner.told == {}
    assert rig.runner.telling == set()
    assert rig.rec.origin["job"].get(bj.AWAITING_KEY) == waiting


@pytest.mark.parametrize("failure", ["nothing_landed", "raised"])
async def test_a_failed_send_is_tried_again_at_a_later_walk(rig, failure):
    """The episode is recorded as told only once the line landed."""
    since = _silent(rig)
    if failure == "raised":
        rig.ch.error = RuntimeError("telegram is down")
    else:
        rig.ch.mid = None
    rig.runner._watch_once(NOW)
    await settle()
    assert rig.ch.parked == [] and rig.runner.told == {}
    assert rig.runner.telling == set()
    rig.ch.error, rig.ch.mid = None, 42
    rig.runner._watch_once(NOW + bj.SILENT_CHECK_S)
    await settle()
    assert rig.ch.attempts == [SILENT_LINE] * 2 and rig.ch.parked == [SILENT_LINE]
    assert rig.runner.told == {rig.rec.id: since}


async def test_a_later_silent_episode_is_told_again(rig):
    """An episode is named by the activity time it began at: a frame gives a
    later time, so the next silence is another episode."""
    _silent(rig)
    rig.runner._watch_once(NOW)
    await settle()
    later = NOW + 100                       # a frame: the job works again
    rig.drv.facts = JobFacts(True, False, later)
    rig.runner._watch_once(NOW + bj.SILENT_CHECK_S)
    await settle()
    assert rig.ch.parked == [SILENT_LINE]
    rig.runner._watch_once(later + bj.SILENT_AFTER_S + 1)    # ... and stalls again
    await settle()
    assert rig.ch.parked == [SILENT_LINE] * 2
    assert rig.runner.told == {rig.rec.id: later}


async def test_an_attempt_that_completes_late_records_only_its_own_episode(rig):
    """While one attempt's send is held, the job works again and falls
    silent again: no second attempt starts meanwhile, the late attempt
    records the episode it was about, and the later one still gets its own
    line."""
    first = _silent(rig)
    rig.ch.hold = asyncio.Event()
    rig.runner._watch_once(NOW)
    await settle()
    assert rig.runner.telling == {rig.rec.id} and rig.runner.told == {}
    later = NOW + 100
    rig.drv.facts = JobFacts(True, False, later)
    rig.runner._watch_once(later + bj.SILENT_AFTER_S + 1)
    await settle()
    assert rig.ch.attempts == [SILENT_LINE]         # one attempt outstanding
    rig.ch.hold.set()
    await settle()
    assert rig.runner.told == {rig.rec.id: first} and rig.runner.telling == set()
    rig.runner._watch_once(later + bj.SILENT_AFTER_S + 1 + bj.SILENT_CHECK_S)
    await settle()
    assert rig.ch.parked == [SILENT_LINE] * 2
    assert rig.runner.told == {rig.rec.id: later}


async def test_a_silent_job_with_no_delivery_is_settled_without_a_post(rig):
    rig.rec.delivery = None
    since = _silent(rig)
    rig.runner._watch_once(NOW)
    rig.runner._watch_once(NOW + bj.SILENT_CHECK_S)
    await settle()
    assert rig.ch.attempts == [] and rig.ch.notices == []
    assert rig.runner.told == {rig.rec.id: since} and rig.runner.telling == set()


async def test_a_quiet_run_that_goes_silent_stops_being_quiet(rig):
    rig.rec.origin["job"]["quiet"] = True
    _silent(rig)
    rig.runner._watch_once(NOW)
    await settle()
    assert rig.reg.promoted == [rig.rec.id]
    assert rig.ch.parked == [SILENT_LINE]


async def test_a_job_that_is_no_longer_live_is_forgotten(rig):
    _silent(rig)
    rig.runner._watch_once(NOW)
    await settle()
    assert rig.rec.id in rig.runner.told
    rig.rec.status = "completed"
    rig.runner.telling.add(rig.rec.id)
    rig.runner._watch_once(NOW + bj.SILENT_CHECK_S)
    await settle()
    assert rig.runner.told == {} and rig.runner.telling == set()
    assert rig.ch.attempts == [SILENT_LINE]


async def test_a_failure_for_one_job_does_not_stop_the_walk(rig):
    class Broken(SimpleNamespace):
        @property
        def status(self):
            raise RuntimeError("unreadable record")

    broken = Broken(id="b" * 32, origin={"job": {"title": "Broken"}})
    rig.reg.live = [broken, rig.rec]
    _silent(rig)
    rig.runner._watch_once(NOW)
    await settle()
    assert rig.ch.parked == [SILENT_LINE]


async def test_the_watch_walks_on_its_cadence_until_casa_stops_it(rig, monkeypatch):
    """The watch is one task: a second start starts nothing, and Casa's stop
    cancels it. The cadence is shortened by its own constant."""
    monkeypatch.setattr(bj, "SILENT_CHECK_S", 0.01)
    assert bj.SILENT_AFTER_S == 3600
    rig.drv.facts = JobFacts(True, False, time.time() - bj.SILENT_AFTER_S - 60)
    rig.runner.start_watch()
    task = rig.runner._watch_task
    rig.runner.start_watch()
    assert task is not None and rig.runner._watch_task is task
    await until(lambda: rig.ch.parked == [SILENT_LINE])
    bj.stop_watch()
    await settle()
    assert task.cancelled() and rig.runner._watch_task is None


async def test_putting_the_runner_to_work_starts_its_watch(rig):
    """Executed, not read: the function casa_core's ``main`` calls once the
    driver exists attaches the driver, installs the runner and leaves its
    watch task running. Nothing else starts the watch."""
    import casa_core
    bj.configure(None)
    runner = bj.JobRunner(registry=rig.reg, channel=rig.ch)
    casa_core._start_job_runner(runner, rig.drv)
    try:
        task = runner._watch_task
        assert bj._runner is runner and runner.driver is rig.drv
        assert isinstance(task, asyncio.Task)
        await settle()
        assert not task.done()              # it is waiting for its first walk
    finally:
        runner.stop_watch()


async def test_main_puts_the_runner_to_work_unconditionally():
    """The one edge the case above cannot witness: ``main`` calling the
    function. A STRUCTURAL pin, weaker than an execution and said so here: it
    reads ``main``'s AST and requires the call to sit as a direct statement
    of ``main``'s own body, so a call that is deleted, or nested under any
    conditional, fails."""
    import ast
    import inspect
    import casa_core
    tree = ast.parse(inspect.getsource(casa_core))
    main = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "main")
    calls = [st for st in main.body
             if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call)
             and isinstance(st.value.func, ast.Name)
             and st.value.func.id == "_start_job_runner"]
    assert len(calls) == 1, "main must call _start_job_runner once, unconditionally"


async def test_the_graceful_stop_cancels_the_watch(rig, monkeypatch):
    """Executed, not read: Casa's real cleanup at a graceful stop cancels the
    runner's watch, so no walk runs during the teardown."""
    from unittest.mock import AsyncMock, MagicMock
    import casa_core
    import plugin_continuations
    import tools
    rig.runner.start_watch()
    task = rig.runner._watch_task
    await settle()
    assert isinstance(task, asyncio.Task) and not task.done()
    # The cleanup declares the stop to the continuation sweep too: a module
    # flag, put back after this test.
    monkeypatch.setattr(plugin_continuations, "_stopping", False)
    monkeypatch.setattr(casa_core, "_drain_broker_before_channel_shutdown", AsyncMock())
    monkeypatch.setattr(casa_core, "_close_butler_ha_facade", AsyncMock())
    monkeypatch.setattr(tools, "stop_engagement_launches", AsyncMock())
    monkeypatch.setattr(tools, "drain_delegation_settlements", AsyncMock())
    engagement_registry = MagicMock(begin_launch_shutdown=MagicMock(return_value=0),
                                    drain_launches=AsyncMock())
    await casa_core._shutdown_cleanup(
        job_registry=MagicMock(close=AsyncMock()), engagement_registry=engagement_registry,
        scheduler=MagicMock(shutdown=MagicMock()), session_sweeper=MagicMock(stop=AsyncMock()),
        runtime=MagicMock(agents={}), ha_facade=None,
        bus=MagicMock(begin_shutdown=MagicMock(), agent_loop_tasks=MagicMock(return_value=[]),
                      fail_pending=MagicMock(), live_dispatch_tasks=MagicMock(return_value=[])),
        loop_tasks=[], channel_manager=MagicMock(stop_all=AsyncMock()), runners=[])
    await settle()
    assert task.cancelled() and rig.runner._watch_task is None


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

