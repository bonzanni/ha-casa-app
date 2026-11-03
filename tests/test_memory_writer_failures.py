"""How the writer fails: visibly, with its events kept pending.

A failed run backs off and is retried; after three failures the job is
``failed``, the operator gets one private notice in the agreed wording, and
the nightly pass retries it once a night. A reply that cannot be read, a
patch that names something outside the ledger, and a commit that would leave
an object unreachable each fail the run and write nothing. The meter counts
failed jobs, pending events and pending corrections.

The store failing under the worker is the worker's to recover from, inside
the running process: a store that could not be opened is opened again at a
doubling interval and its spool then drained, and a job cut off while
``running`` is requeued once the store takes writes. Both outages are a real
permission flip on the store file.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from datetime import datetime

import pytest

from memory import capture, corrections, meter, nightly, writer
from memory.store import MemoryStore
from memory_writer_support import (
    ScriptedPort, add_each, dispositions, job_rows, new_store, new_writer,
    no_change_all, pending_seqs, reply, rows, run_new, seed,
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

async def test_nightly_queues_condense_and_regroup_only_when_due(
        tmp_path, monkeypatch):
    monkeypatch.setattr(meter, "_role_budgets", {})
    store = new_store(tmp_path)
    seed(store, "short")
    await run_new(new_writer(store, ScriptedPort(add_each)))
    assert nightly.enqueue_nightly(store) == {
        "condense": [], "regroup": [], "retried": 0}
    seed(store, "long " * 1000)
    await run_new(new_writer(store, ScriptedPort(add_each)))
    # A large ledger whose entry lines still fit every role's budget is not
    # regrouped: groups are added only when the level below no longer fits.
    assert nightly.enqueue_nightly(store, role_budgets={"assistant": 1000}) \
        == {"condense": ["private"], "regroup": [], "retried": 0}
    # A budget even the coarsest level overflows queues the regroup, and the
    # meter's configured budgets are the default.
    meter.configure({"assistant": 1000, "tiny": 5, "off": 0})
    assert meter.role_budgets() == {"assistant": 1000, "tiny": 5}
    assert nightly.enqueue_nightly(store) == {
        "condense": [], "regroup": ["private"], "retried": 0}
    # Already queued: not queued twice.
    assert nightly.enqueue_nightly(store) == {
        "condense": [], "regroup": [], "retried": 0}
    assert [j["kind"] for j in job_rows(store)
            if j["state"] == "queued"] == ["condense", "regroup"]


async def test_the_meter_counts_what_memory_owes(tmp_path, monkeypatch):
    monkeypatch.setattr(meter, "_role_budgets", {})
    meter.configure({"assistant": 50_000, "tiny": 5})
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
    # Each served role's view of the ledger: whole within a large budget;
    # past a tiny one, the index, overflowing even at its coarsest level.
    assert {r: (v["mode"], v["overflow"]) for r, v in led["roles"].items()} \
        == {"assistant": ("whole", False), "tiny": ("index", True)}
    assert led["overflow"] is True
    assert got["commit_no"] == 1 and got["provisional_events"] == 3
    assert got["maintenance_intent"] is False
    assert meter.read(store) == got


# --- the store failing under the worker --------------------------------------

def _outage(store) -> None:
    """Make the real store file unopenable (a permission flip)."""
    os.chmod(store.path, 0)


def _restore(store) -> None:
    os.chmod(store.path, 0o600)


@pytest.fixture
def not_root():
    if os.geteuid() == 0:
        pytest.skip("a permission flip does not stop root")


async def test_a_store_failing_mid_run_is_recovered_in_process(tmp_path,
                                                                not_root):
    """The store fails after the model replied, so neither the commit nor
    the failure count can be written: the job is left ``running`` under this
    process. Passes while the store is still failing change nothing; the
    first pass after it is back requeues the job, which files the event
    once. The outage counts as no attempt."""
    store = new_store(tmp_path)
    e = seed(store, "the spare key is under the mat")

    def flip(prompt):
        _outage(store)
        return reply({e: "no_change"})

    w = new_writer(store, ScriptedPort([flip, lambda p: reply(
        {e: "no_change"})]))
    job = writer.enqueue(store, "private", writer.ORDINARY)
    try:
        with pytest.raises(sqlite3.OperationalError):
            await w.pass_once()
        with pytest.raises(sqlite3.OperationalError):
            await w.pass_once()             # still failing: nothing moves
    finally:
        _restore(store)
    assert [(j["state"], j["attempts"]) for j in job_rows(store)] == [
        ("running", 0)]
    assert pending_seqs(store) == [e] and dispositions(store) == []

    assert await w.pass_once() is True
    assert [(j["job_id"], j["state"], j["attempts"])
            for j in job_rows(store)] == [(job, "committed", 0)]
    assert dispositions(store) == [(e, "private", "no_change", 1)]
    assert pending_seqs(store) == []


async def test_requeueing_an_abandoned_job_leaves_a_landed_one_alone(
        tmp_path):
    """The requeue moves a job only while it is still ``running`` under
    this process: one whose commit landed, or one another process runs,
    stays as it is."""
    store = new_store(tmp_path)
    seed(store, "x")
    w = new_writer(store, ScriptedPort(add_each), boot_id="boot-a")
    done, _ = await run_new(w)
    other = writer.enqueue(store, "private", writer.CONDENSE)
    with store.transaction(write=True) as conn:
        conn.execute("UPDATE jobs SET state = 'running', boot_id = 'boot-b' "
                     "WHERE job_id = ?", (other,))
    w._abandoned.update({done, other})
    assert w.requeue_abandoned() == 0
    assert [(j["state"], j["boot_id"]) for j in job_rows(store)] == [
        ("committed", "boot-a"), ("running", "boot-b")]
    assert w._abandoned == set()


async def test_a_store_unavailable_at_boot_is_reopened_and_the_spool_filed(
        tmp_path, not_root):
    """The store cannot be opened at boot, so capture spools. The worker
    retries the open at a doubling interval; once the store opens it drains
    the spool, and the event is filed."""
    first = new_store(tmp_path)
    _outage(first)
    try:
        store = MemoryStore(first.directory)
        assert store.open() is False
        item = capture.event(role="assistant", channel="telegram",
                             speaker_kind="operator", origin_kind="operator",
                             text="the boiler code is 4417",
                             eligibility="write_trusted_turn",
                             origin_tier=None)
        assert capture.append(store, item).outcome == capture.SPOOLED
        clock = Clock()
        w = new_writer(store, ScriptedPort(add_each), clock=clock)
        assert await w.pass_once() is False      # retried at once: fails
        clock.now += writer.REOPEN_FIRST_S
        assert await w.pass_once() is False      # due, fails: wait doubles
    finally:
        _restore(first)
    clock.now += writer.REOPEN_FIRST_S
    assert await w.pass_once() is False          # back, but not due yet
    assert store.available is False
    clock.now += writer.REOPEN_FIRST_S
    w.session_ended()
    assert await w.pass_once() is True
    assert store.available is True
    assert not os.path.exists(capture.spool_path(store.directory))
    [event] = rows(store, "SELECT * FROM events")
    assert event["source_key"] == item.source_key
    assert dispositions(store) == [(event["seq"], "private", "patched", 1)]
    assert pending_seqs(store) == []


async def test_an_event_the_snapshot_missed_is_still_due(tmp_path,
                                                         monkeypatch):
    """A capture stamped before the run started but committed after the
    run's read snapshot was never given to the run, so the maximum wait
    still applies to it. (A cutoff at the run's start time would hide it
    until a session ends.)"""
    store = new_store(tmp_path)
    old = seed(store, "old words")
    job = writer.enqueue(store, "private", writer.ORDINARY)
    item = capture.event(role="assistant", channel="telegram",
                         speaker_kind="operator", origin_kind="operator",
                         text="late words", eligibility="write_trusted_turn",
                         origin_tier=None)
    inserted, release, committed = (threading.Event() for _ in range(3))

    def paused_capture():
        with store.transaction(write=True) as conn:
            capture._insert(conn, item)
            inserted.set()
            release.wait(30)
        committed.set()

    t = threading.Thread(target=paused_capture)
    t.start()
    assert inserted.wait(30)

    def commit_after_snapshot():
        release.set()
        assert committed.wait(30)

    monkeypatch.setitem(writer._hooks, "selected", commit_after_snapshot)
    clock = Clock()
    port = ScriptedPort(no_change_all)
    w = new_writer(store, port, clock=clock)
    assert await w.run_job(job) == writer.COMMITTED
    t.join(30)
    monkeypatch.setitem(writer._hooks, "selected", lambda: None)
    assert [e["seq"] for e in port.prompts[0]["events"]] == [old]
    [late] = rows(store, "SELECT seq, recorded_at FROM events WHERE "
                         "source_key = ?", item.source_key)
    assert pending_seqs(store) == [late["seq"]]

    clock.now = datetime.fromisoformat(
        late["recorded_at"]).timestamp() + writer.MAX_WAIT_S
    [due] = w.schedule_due()
    assert await w.run_job(due) == writer.COMMITTED
    assert pending_seqs(store) == []


async def test_a_stalled_model_call_fails_the_run(tmp_path, monkeypatch):
    """The worker bounds the model call itself, whatever port it was given:
    a call that never answers fails the run once the bound passes, the job
    backs off as for any failure, and nothing is written."""
    monkeypatch.setattr(writer, "MODEL_TIMEOUT_S", 0.05)
    store = new_store(tmp_path)
    e = seed(store, "x")

    class Stalled:
        async def complete(self, *, system, prompt):
            await asyncio.Event().wait()

    job = writer.enqueue(store, "private", writer.ORDINARY)
    outcome = await asyncio.wait_for(
        new_writer(store, Stalled()).run_job(job), 10)
    assert outcome == writer.RETRY
    assert [(j["state"], j["attempts"]) for j in job_rows(store)] == [
        ("queued", 1)]
    assert "TimeoutError" in job_rows(store)[0]["last_error"]
    assert dispositions(store) == [] and pending_seqs(store) == [e]
