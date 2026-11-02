"""How the writer fails: visibly, with its events kept pending.

A failed run backs off and is retried; after three failures the job is
``failed``, the operator gets one private notice in the agreed wording, and
the nightly pass retries it once a night. A reply that cannot be read, a
patch that names something outside the ledger, and a commit that would leave
an object unreachable each fail the run and write nothing. The meter counts
failed jobs, pending events and pending corrections.
"""
from __future__ import annotations

from memory import corrections, meter, nightly, writer
from memory_writer_support import (
    ScriptedPort, add_each, dispositions, job_rows, new_store, new_writer,
    pending_seqs, reply, rows, run_new, seed,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


async def test_backoff_then_failed_after_three_attempts_with_one_notice(
        tmp_path):
    store = new_store(tmp_path)
    e = seed(store, "remember the plumber")
    sent: list[str] = []

    async def notify(text):
        sent.append(text)

    clock = Clock()
    w = new_writer(store, ScriptedPort([RuntimeError("down")] * 4),
                   notify=notify, clock=clock)
    job = writer.enqueue(store, "private", writer.ORDINARY)
    assert await w.run_job(job) == writer.RETRY
    assert w.next_job() is None                     # backing off
    clock.now += writer.BACKOFF_S
    assert w.next_job()["job_id"] == job
    assert await w.run_job(job) == writer.RETRY
    clock.now += writer.BACKOFF_S
    assert w.next_job() is None                     # the backoff doubled
    clock.now += writer.BACKOFF_S
    assert await w.run_job(job) == writer.FAILED

    [row] = job_rows(store)
    assert (row["state"], row["attempts"]) == ("failed", 3)
    assert row["last_error"] == "RuntimeError: down"
    assert pending_seqs(store) == [e]
    assert dispositions(store) == []
    [event] = rows(store, "SELECT * FROM events")
    from datetime import datetime
    from zoneinfo import ZoneInfo
    at = datetime.fromisoformat(event["occurred_at"]).astimezone(
        ZoneInfo(event["tz"])).strftime("%H:%M")
    assert sent == [f"Memory couldn't file what was said at {at}; it's kept "
                    "and I'll try again tonight."]

    # The nightly retry runs it once more; failing again sends nothing new.
    assert nightly.enqueue_nightly(store)["retried"] == 1
    assert await w.run_job(job) == writer.FAILED
    assert len(sent) == 1
    assert job_rows(store)[0]["attempts"] == 4
    assert meter.read(store)["ledgers"]["private"]["failed_jobs"] == 1


async def test_an_unreadable_reply_fails_the_run(tmp_path):
    store = new_store(tmp_path)
    seed(store, "x")
    w = new_writer(store, ScriptedPort(["I filed it all, thanks!"]))
    _, outcome = await run_new(w)
    assert outcome == writer.RETRY
    assert "no JSON object" in job_rows(store)[0]["last_error"]
    assert dispositions(store) == []


async def test_an_unknown_outcome_fails_the_run(tmp_path):
    store = new_store(tmp_path)
    e = seed(store, "x")
    w = new_writer(store, ScriptedPort([reply({e: "maybe"})]))
    assert (await run_new(w))[1] == writer.RETRY
    assert dispositions(store) == []


async def test_a_patch_outside_the_ledger_writes_nothing(tmp_path):
    store = new_store(tmp_path)
    e = seed(store, "x")
    w = new_writer(store, ScriptedPort([reply({e: "patched"}, [
        {"op": "add", "kind": "entry", "entry_kind": "fact", "body": "x"},
        {"op": "retire", "object_id": "o-not-here"}])]))
    assert (await run_new(w))[1] == writer.RETRY
    assert rows(store, "SELECT * FROM objects") == []
    assert dispositions(store) == []
    assert "o-not-here" in job_rows(store)[0]["last_error"]


async def test_an_unreachable_ledger_fails_the_commit(tmp_path):
    """Retiring a group that still has a current member would leave the
    member unreachable: the commit fails and writes nothing."""
    store = new_store(tmp_path)
    e = seed(store, "x")
    w = new_writer(store, ScriptedPort([reply({e: "patched"}, [
        {"op": "add", "ref": "g", "kind": "group", "line": "home"},
        {"op": "add", "kind": "entry", "entry_kind": "fact", "body": "x",
         "parent": "@g", "source_seqs": [e]}])]))
    assert (await run_new(w))[1] == writer.COMMITTED
    group = rows(store, "SELECT object_id FROM objects WHERE kind = 'group'"
                 )[0]["object_id"]
    f = seed(store, "y")
    w.port = ScriptedPort([reply({f: "patched"}, [
        {"op": "retire", "object_id": group}])])
    assert (await run_new(w))[1] == writer.RETRY
    assert "unreachable" in job_rows(store)[-1]["last_error"]
    assert rows(store, "SELECT status FROM objects WHERE object_id = ?",
                group) == [{"status": "current"}]
    assert pending_seqs(store) == [f]


async def test_an_outcome_for_an_event_not_given_is_ignored(tmp_path):
    store = new_store(tmp_path)
    e = seed(store, "x")
    w = new_writer(store, ScriptedPort([reply({e: "no_change",
                                               e + 50: "patched"})]))
    assert (await run_new(w))[1] == writer.COMMITTED
    assert dispositions(store) == [(e, "private", "no_change", 1)]


async def test_an_event_the_reply_does_not_name_stays_pending(tmp_path):
    store = new_store(tmp_path)
    e, f = seed(store, "x"), seed(store, "y")
    w = new_writer(store, ScriptedPort([reply({e: "no_change"})]))
    assert (await run_new(w))[1] == writer.COMMITTED
    assert dispositions(store) == [(e, "private", "no_change", 1),
                                   (f, "private", "deferred", 1)]
    assert pending_seqs(store) == [f]


# --- the nightly pass and the meter ------------------------------------------

async def test_nightly_queues_condense_and_regroup_only_when_due(tmp_path):
    store = new_store(tmp_path)
    seed(store, "short")
    await run_new(new_writer(store, ScriptedPort(add_each)))
    assert nightly.enqueue_nightly(store) == {
        "condense": [], "regroup": [], "retried": 0}
    seed(store, "long " * 1000)
    await run_new(new_writer(store, ScriptedPort(add_each)))
    assert nightly.enqueue_nightly(store, regroup_over_tokens=100) == {
        "condense": ["private"], "regroup": ["private"], "retried": 0}
    # Already queued: not queued twice.
    assert nightly.enqueue_nightly(store, regroup_over_tokens=100) == {
        "condense": [], "regroup": [], "retried": 0}
    assert [j["kind"] for j in job_rows(store)
            if j["state"] == "queued"] == ["condense", "regroup"]


async def test_the_meter_counts_what_memory_owes(tmp_path):
    store = new_store(tmp_path)
    a = seed(store, "one")
    seed(store, "two")
    await run_new(new_writer(store, ScriptedPort(add_each)))
    corrections.mark(store, a)
    seed(store, "three")
    got = meter.persist(store)
    led = got["ledgers"]["private"]
    assert (led["entries"], led["unreachable"], led["failed_jobs"]) == (
        2, 0, 0)
    assert led["pending"]["events"] == 2
    assert led["pending_corrections"] == 1
    assert len(led["largest"]) == 2 and led["week_added"]["entries"] == 2
    assert got["commit_no"] == 1 and got["provisional_events"] == 3
    assert got["maintenance_intent"] is False
    assert meter.read(store) == got
