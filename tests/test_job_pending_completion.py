"""#1180: a fresh job's completion refused for unread input is not forgotten
(INV-BGJOB-005).

A ``session: "fresh"`` job runs every turn after its launch in a fresh
conversation. When the completion gate refuses ``emit_completion`` with
``unread_inbound``, the next turn starts with no memory of that refusal; the
job then reached its batch cap and was finalised as an error although its work
was complete. The refused completion is now recorded on the job, named in every
later brief, and the batch cap spares the job one batch while it is recorded.

Drives the real ``InCasaDriver._deliver_turn``, registry, completion gate and
Telegram owners through the batch-loop harness. The fake worker re-completes
ONLY when its prompt says a completion is pending — the behaviour the brief
asks of it — so a brief that does not say so reproduces the live failure.
"""
from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import ClaudeAgentOptions

import background_jobs as jobs
import tools
from engagement_registry import EngagementRegistry

try:
    from tests.test_background_jobs_loop import harness, payload, result, text_frame  # noqa: F401
    from tests.test_job_fresh_conversation import (
        RESET, CONTEXT, FreshClient, fresh, fresh_setup, queries, spy_finalize)  # noqa: F401
except ImportError:
    from test_background_jobs_loop import harness, payload, result, text_frame  # noqa: F401
    from test_job_fresh_conversation import (  # noqa: F401
        RESET, CONTEXT, FreshClient, fresh, fresh_setup, queries, spy_finalize)

pytestmark = [pytest.mark.unit]   # asyncio_mode = auto (pytest.ini)

DONE = "All 40 rows classified\nsecond line SECRET-PENDING detail"
PENDING = "A completion (status ok) was pending when an earlier turn ended"


class Worker:
    """What the job's worker does, as scripted steps of the fake client."""

    def __init__(self, h):
        self.h = h
        self.refusals = []
        self.accepted = []

    async def complete(self, text=DONE):
        reply = payload(await tools.emit_completion.handler({"text": text}))
        if reply.get("kind") == "unread_inbound":
            self.refusals.append(reply)
        else:
            self.accepted.append(reply)

    async def complete_if_told(self):
        """A fresh turn knows only its prompt: it re-completes only when the
        brief says a completion is pending."""
        if PENDING in self.h.client.prompts[-1]:
            await self.complete()


def gate_counts(h, finals, worker):
    return {
        "refusals": len(worker.refusals),
        "accepted": len(worker.accepted),
        "errors": sum(1 for o, _ in finals if o == "error"),
        "completed": sum(1 for o, _ in finals if o == "completed"),
        "status": h.rec.status,
    }


# -- red case 1: the live sequence ----------------------------------------

async def test_a_refused_completion_is_named_in_the_operator_turn_and_completes(
        fresh, monkeypatch, tmp_path):
    h = fresh
    h.rec.origin["job"]["batches"] = 1
    finals = spy_finalize(monkeypatch)
    worker = Worker(h)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()
    h.client.scripts = [
        [text_frame("Working"), h.report, hold, worker.complete, result()],
        [text_frame("Answer"), worker.complete_if_told, result()]]
    await h.start()
    await asyncio.wait_for(entered.wait(), 5)
    await h.operator("How is it going?")
    release.set()
    await h.drain()
    q = [p for p in queries(h) if p != RESET]
    assert len(q) == 2
    # The operator's turn is briefed with the refused completion's first line,
    # and only its first line.
    assert f'{PENDING}: All 40 rows classified.' in q[1]
    assert "SECRET-PENDING" not in q[1]
    assert q[1].endswith("\n\nHow is it going?")
    assert gate_counts(h, finals, worker) == {
        "refusals": 1, "accepted": 1, "errors": 0, "completed": 1,
        "status": "completed"}
    assert h.rec.origin["job"]["started"] == 1
    h.assert_terminal("completed", "All 40 rows classified")


async def test_the_refusal_is_recorded_and_persisted_with_the_bounded_text(
        fresh, tmp_path):
    h = fresh
    worker = Worker(h)
    h.client.scripts = [[text_frame("Starting the job"), result()]]
    await h.driver.start(h.rec, prompt="Acknowledge the job", options=ClaudeAgentOptions())
    h.driver.admit_inbound(h.rec.id, "unread")
    long = "x" * (tools._COMPLETION_TEXT_MAX + 50)
    h.client.scripts = [[lambda: worker.complete(long), result()]]
    await h.driver.send_user_turn(h.rec, jobs.batch_prompt(1, "Process rows"))
    assert len(worker.refusals) == 1
    pending = h.rec.origin["job"]["completion_pending"]
    assert pending == {"status": "ok",
                       "text": long[:tools._COMPLETION_TEXT_MAX] + " … [truncated]"}
    loaded = EngagementRegistry(tombstone_path=str(tmp_path / "jobs.json"), bus=None)
    await loaded.load()
    assert loaded.get(h.rec.id).origin["job"]["completion_pending"] == pending


# -- red case 2: the refusal came from an ingress reservation --------------

async def test_a_reservation_refusal_at_the_cap_gets_one_spared_batch(
        fresh, monkeypatch):
    h = fresh
    h.rec.origin["job"]["batches"] = 1
    finals = spy_finalize(monkeypatch)
    worker = Worker(h)

    async def reserve():
        h.driver.reserve_inbound(h.rec.id)

    async def release_without_a_ticket():
        h.driver.release_inbound_reservation(h.rec.id)
    h.client.scripts = [
        [text_frame("Working"), h.report, reserve, worker.complete,
         release_without_a_ticket, result()],
        [worker.complete_if_told, result()]]
    await h.start()
    await h.drain()
    assert h.batches() == []        # every batch prompt follows a brief
    prompts = [p for p in queries(h) if p != RESET]
    assert [p.rsplit("\n\n", 1)[1] for p in prompts] == [
        jobs.batch_prompt(1, "Process rows"), jobs.batch_prompt(2, "Process rows")]
    assert PENDING in prompts[1] and PENDING not in prompts[0]
    assert gate_counts(h, finals, worker) == {
        "refusals": 1, "accepted": 1, "errors": 0, "completed": 1,
        "status": "completed"}


# -- red case 3: the spare is one batch, never more ------------------------

async def test_the_spare_is_bounded_to_one_batch(fresh, monkeypatch):
    h = fresh
    h.rec.origin["job"]["batches"] = 1
    finals = spy_finalize(monkeypatch)
    worker = Worker(h)

    async def reserve():
        h.driver.reserve_inbound(h.rec.id)

    async def release():
        h.driver.release_inbound_reservation(h.rec.id)
    h.client.scripts = [
        [text_frame("Working"), h.report, reserve, worker.complete, release, result()],
        # The spared batch ignores the pending completion and only reports.
        [text_frame("Working"), h.report, result()]]
    await h.start()
    await h.drain()
    assert h.rec.origin["job"]["started"] == 2
    assert gate_counts(h, finals, worker) == {
        "refusals": 1, "accepted": 0, "errors": 1, "completed": 0, "status": "error"}
    h.assert_terminal("error", "reached its limit of 1 batches")


async def test_no_pending_completion_keeps_the_cap_exact(fresh, monkeypatch):
    h = fresh
    h.rec.origin["job"]["batches"] = 1
    finals = spy_finalize(monkeypatch)
    h.client.scripts = [[text_frame("Working"), h.report, result()]]
    await h.start()
    await h.drain()
    assert h.rec.origin["job"]["started"] == 1
    assert "completion_pending" not in h.rec.origin["job"]
    assert [o for o, _ in finals] == ["error"]
    h.assert_terminal("error", "reached its limit of 1 batches")


# -- red case 4: clearance --------------------------------------------------

async def test_a_clearance_downgrade_withholds_the_pending_text(fresh, tmp_path):
    h = fresh
    h.rec.origin["_origin_clearance"] = "private"
    worker = Worker(h)
    h.client.scripts = [[text_frame("Starting the job"), result()]]
    await h.driver.start(h.rec, prompt="Acknowledge the job", options=ClaudeAgentOptions())
    h.driver.admit_inbound(h.rec.id, "unread")
    h.client.scripts = [[worker.complete, result()]]
    await h.driver.send_user_turn(h.rec, jobs.batch_prompt(1, "Process rows"))
    assert len(worker.refusals) == 1
    assert await h.reg.lower_origin_clearance(h.rec.id, "public")
    assert h.rec.origin["job"]["completion_pending"] == {"status": "ok"}
    brief = jobs.job_brief(h.rec)
    assert "All 40 rows" not in brief and "SECRET-PENDING" not in brief
    assert PENDING in brief and "withheld" in brief
    loaded = EngagementRegistry(tombstone_path=str(tmp_path / "jobs.json"), bus=None)
    await loaded.load()
    assert loaded.get(h.rec.id).origin["job"]["completion_pending"] == {"status": "ok"}


# -- red case 5: resume-mode jobs and non-fresh refusals are unchanged -----

async def test_a_resume_mode_job_records_nothing_and_keeps_the_exact_cap(
        harness, monkeypatch):
    h = harness
    h.rec.origin["job"]["batches"] = 1
    finals = spy_finalize(monkeypatch)
    worker = Worker(h)

    async def reserve():
        h.driver.reserve_inbound(h.rec.id)

    async def release():
        h.driver.release_inbound_reservation(h.rec.id)
    h.client.scripts = [
        [text_frame("Working"), h.report, reserve, worker.complete, release, result()]]
    await h.start()
    await h.drain()
    assert "completion_pending" not in h.rec.origin["job"]
    assert h.client.prompts[1:] == [jobs.batch_prompt(1, "Process rows")]
    assert gate_counts(h, finals, worker) == {
        "refusals": 1, "accepted": 0, "errors": 1, "completed": 0, "status": "error"}


async def test_other_refusals_record_nothing(fresh):
    h = fresh
    worker = Worker(h)
    h.client.scripts = [[text_frame("Starting the job"), result()]]
    await h.driver.start(h.rec, prompt="Acknowledge the job", options=ClaudeAgentOptions())

    async def bad_status():
        reply = payload(await tools.emit_completion.handler({"status": "success"}))
        worker.refusals.append(reply)
    h.client.scripts = [[bad_status, result()]]
    await h.driver.send_user_turn(h.rec, jobs.batch_prompt(1, "Process rows"))
    assert worker.refusals[0]["kind"] == "invalid_status"
    assert "completion_pending" not in h.rec.origin["job"]
    assert PENDING not in jobs.job_brief(h.rec)


# -- red case 6: the latest refusal is the one named -----------------------

async def test_a_second_refusal_replaces_the_first(fresh):
    h = fresh
    worker = Worker(h)
    h.client.scripts = [[text_frame("Starting the job"), result()]]
    await h.driver.start(h.rec, prompt="Acknowledge the job", options=ClaudeAgentOptions())
    h.driver.admit_inbound(h.rec.id, "unread")
    h.client.scripts = [[lambda: worker.complete("First summary"), result()],
                        [lambda: worker.complete("Second summary"), result()]]
    await h.driver.send_user_turn(h.rec, jobs.batch_prompt(1, "Process rows"))
    await h.driver.send_user_turn(h.rec, jobs.batch_prompt(2, "Process rows"))
    assert len(worker.refusals) == 2
    brief = jobs.job_brief(h.rec)
    assert f"{PENDING}: Second summary." in brief
    assert "First summary" not in brief


# -- design r1: the no-progress guard spares the same one batch -------------

async def test_a_pending_completion_survives_the_no_progress_guard_once(
        fresh, monkeypatch):
    h = fresh
    h.rec.origin["job"]["batches"] = None       # unlimited: only the guard ends it
    finals = spy_finalize(monkeypatch)
    worker = Worker(h)

    async def stalled():
        await h.report(progressed=False)

    async def reserve():
        h.driver.reserve_inbound(h.rec.id)

    async def release():
        h.driver.release_inbound_reservation(h.rec.id)
    h.client.scripts = [
        [text_frame("Working"), stalled, result()],
        [text_frame("Working"), stalled, result()],
        [text_frame("Working"), stalled, reserve, worker.complete, release, result()],
        [worker.complete_if_told, result()]]
    await h.start()
    await h.drain()
    assert h.rec.origin["job"]["started"] == 4
    assert gate_counts(h, finals, worker) == {
        "refusals": 1, "accepted": 1, "errors": 0, "completed": 1,
        "status": "completed"}


async def test_the_spare_is_spent_once_per_job_whichever_limit(fresh, monkeypatch):
    h = fresh
    h.rec.origin["job"]["batches"] = None
    finals = spy_finalize(monkeypatch)
    worker = Worker(h)

    async def stalled():
        await h.report(progressed=False)

    async def reserve():
        h.driver.reserve_inbound(h.rec.id)

    async def release():
        h.driver.release_inbound_reservation(h.rec.id)
    h.client.scripts = [
        [text_frame("Working"), stalled, result()],
        [text_frame("Working"), stalled, result()],
        [text_frame("Working"), stalled, reserve, worker.complete, release, result()],
        # The spared batch is refused again and does not complete.
        [text_frame("Working"), stalled, reserve, worker.complete, release, result()]]
    await h.start()
    await h.drain()
    assert h.rec.origin["job"]["started"] == 4
    assert h.rec.origin["job"]["pending_spared"] is True
    assert gate_counts(h, finals, worker) == {
        "refusals": 2, "accepted": 0, "errors": 1, "completed": 0, "status": "error"}
    h.assert_terminal("error", "no progress in 3 consecutive batches")


# -- design r1: a held reservation does not spend the spare -----------------

async def test_a_held_reservation_waits_instead_of_spending_the_spare(
        fresh, monkeypatch):
    h = fresh
    h.rec.origin["job"]["batches"] = 1
    finals = spy_finalize(monkeypatch)
    worker = Worker(h)

    async def reserve():
        h.driver.reserve_inbound(h.rec.id)
    h.client.scripts = [
        [text_frame("Working"), h.report, reserve, worker.complete, result()],
        [worker.complete_if_told, result()]]
    await h.start()
    await h.drain()
    # The reservation is still held: no batch is started, nothing finalised,
    # and neither the loop nor the sweep counts it as a refused continuation.
    assert h.rec.origin["job"]["started"] == 1 and finals == []
    assert await jobs.start_next_batch(h.rec, h.channel) is False
    h.rec.origin["job"]["last_advance"] = 1    # long stalled
    await jobs.sweep_jobs(h.reg, h.channel)
    assert h.rec.origin["job"]["stalls"] == 0 and finals == []
    h.driver.release_inbound_reservation(h.rec.id)
    await jobs.sweep_jobs(h.reg, h.channel)
    await h.drain()
    assert h.rec.origin["job"]["started"] == 2
    assert gate_counts(h, finals, worker) == {
        "refusals": 1, "accepted": 1, "errors": 0, "completed": 1,
        "status": "completed"}


async def test_a_reservation_without_a_pending_completion_is_not_waited_for(
        fresh, monkeypatch):
    h = fresh
    h.rec.origin["job"]["batches"] = 1
    finals = spy_finalize(monkeypatch)

    async def reserve():
        h.driver.reserve_inbound(h.rec.id)
    h.client.scripts = [[text_frame("Working"), h.report, reserve, result()]]
    await h.start()
    await h.drain()
    assert [o for o, _ in finals] == ["error"]
    h.driver.release_inbound_reservation(h.rec.id)


# -- design r1: a turn begun before a downgrade is refused after it ---------

async def test_a_refusal_after_a_downgrade_records_no_text(fresh):
    h = fresh
    h.rec.origin["_origin_clearance"] = "private"
    worker = Worker(h)
    h.client.scripts = [[text_frame("Starting the job"), result()]]
    await h.driver.start(h.rec, prompt="Acknowledge the job", options=ClaudeAgentOptions())
    h.driver.admit_inbound(h.rec.id, "unread")

    async def downgrade_then_complete():
        # The turn was briefed at the old tier; the clamp lands mid-turn.
        assert await h.reg.lower_origin_clearance(h.rec.id, "public")
        await worker.complete()
    h.client.scripts = [[downgrade_then_complete, result()]]
    await h.driver.send_user_turn(h.rec, jobs.batch_prompt(1, "Process rows"))
    assert len(worker.refusals) == 1
    assert h.rec.origin["job"]["completion_pending"] == {"status": "ok"}
    brief = jobs.job_brief(h.rec)
    assert "All 40 rows" not in brief and PENDING in brief and "withheld" in brief


# -- diff r1: the registry-veto refusal site records too --------------------

async def test_a_registry_veto_refusal_is_recorded_too(fresh, monkeypatch):
    """Input admitted after the handler's own gate is vetoed inside the terminal
    transition (FinalizeResult.PRECONDITION_FAILED): the second refusal site."""
    h = fresh
    h.rec.origin["job"]["batches"] = 1
    worker = Worker(h)
    finals = []
    real = tools._finalize_engagement
    raced = []

    async def finalize(rec, *, outcome, **kw):
        finals.append(outcome)
        if outcome == "completed" and not raced:
            raced.append(True)
            await h.operator("arrived while completing")
        return await real(rec, outcome=outcome, **kw)
    monkeypatch.setattr(tools, "_finalize_engagement", finalize)
    h.client.scripts = [
        [text_frame("Working"), h.report, worker.complete, result()],
        [worker.complete_if_told, result()]]
    await h.start()
    await h.drain()
    assert len(raced) == 1 and len(worker.refusals) == 1
    prompts = [p for p in queries(h) if p != RESET]
    assert len(prompts) == 2 and f"{PENDING}: All 40 rows classified." in prompts[1]
    assert prompts[1].endswith("\n\narrived while completing")
    assert finals == ["completed", "completed"]
    assert h.rec.status == "completed" and len(worker.accepted) == 1


# -- diff r1: a refused hand-off does not spend the spare -------------------

async def test_a_refused_hand_off_keeps_the_spare_and_the_counters(fresh, monkeypatch):
    h = fresh
    h.rec.origin["job"].update(batches=1, started=1, judged=0, advanced=True,
                               completion_pending={"status": "ok", "text": "Done"})
    finals = spy_finalize(monkeypatch)
    worker = Worker(h)
    real = h.channel.deliver_system_turn
    refused = []

    async def deliver(rec, text):
        if not refused:
            refused.append(text)
            return False
        return await real(rec, text)
    monkeypatch.setattr(h.channel, "deliver_system_turn", deliver)
    h.client.scripts = [[text_frame("Starting the job"), result()]]
    await h.driver.start(h.rec, prompt="Acknowledge the job", options=ClaudeAgentOptions())
    before = dict(h.rec.origin["job"])
    assert await jobs.start_next_batch(h.rec, h.channel) is False
    assert refused == [jobs.batch_prompt(2, "Process rows")]
    assert h.rec.origin["job"] == before          # nothing spent, nothing counted
    assert "pending_spared" not in h.rec.origin["job"] and finals == []
    h.client.scripts = [[worker.complete_if_told, result()]]
    assert await jobs.start_next_batch(h.rec, h.channel) is True
    await h.drain()
    assert h.rec.origin["job"]["started"] == 2
    assert h.rec.origin["job"]["pending_spared"] is True
    assert gate_counts(h, finals, worker) == {
        "refusals": 0, "accepted": 1, "errors": 0, "completed": 1,
        "status": "completed"}
