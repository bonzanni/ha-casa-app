"""The writer's commit under crashes and races (M5, INV-MEM-027/028).

Every occurrence pending in a ledger gets exactly one terminal disposition
there after its latest correction mark, and only through a commit that saw
the ledger's revisions and the event's mark as its run's one read
transaction did. A crash at any point leaves each event terminal or pending,
never both and never neither; a run never reuses inputs assembled for an
earlier one; and only the commit marks a job committed.

Everything runs against a real SQLite file. The kill cases SIGKILL a writer
subprocess at a named point of its run (its own child, by pid); the race
cases use real ``BEGIN IMMEDIATE`` contention and the real WAL snapshot.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import pytest

from memory import corrections, nightly, pending, writer
from memory_writer_support import (
    CODE_ROOT, ScriptedPort, add_each, count, dispositions, job_rows,
    new_store, new_writer, no_change_all, pending_seqs, reply, rows, run_new,
    seed, terminal_after_mark,
)

#: A writer subprocess: runs one job and stops at the named point, writing
#: a marker file first, so the test can kill it there.
CHILD = r"""
import asyncio, json, sys, time
store_dir, job_id, point, marker = sys.argv[1:5]
sys.path.insert(0, sys.argv[5])
from memory import writer
from memory.model_port import ModelReply
from memory.store import MemoryStore

store = MemoryStore(store_dir)
assert store.open(), store.unavailable_reason

def stop():
    open(marker, "w").close()
    time.sleep(300)

class Port:
    async def complete(self, *, system, prompt):
        p = json.loads(prompt)
        if point == "model":
            open(marker, "w").close()
            await asyncio.sleep(300)
        ops = [{"op": "add", "kind": "entry", "entry_kind": "fact",
                "subject": "operator", "body": e["text"], "line": "a fact",
                "source_seqs": [e["seq"]]} for e in p["events"]]
        return ModelReply(text=json.dumps({"ops": ops, "events": {
            str(e["seq"]): "patched" for e in p["events"]}}))

if point in writer._hooks:
    writer._hooks[point] = stop

async def keep(text):
    return None

asyncio.run(writer.Writer(store, Port(), classifier=keep,
                          boot_id="boot-child").run_job(job_id))
"""


def _kill_at(store, job_id: str, point: str, tmp_path) -> None:
    """Run *job_id* in a writer subprocess and SIGKILL it at *point*."""
    marker = tmp_path / f"at-{point}"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD, store.directory, job_id, point,
         str(marker), str(CODE_ROOT)], env=env, cwd=str(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 60
        while not marker.exists():
            if proc.poll() is not None:
                raise AssertionError(
                    f"writer child exited early: {proc.stderr.read()!r}")
            assert time.monotonic() < deadline, "writer child never got there"
            time.sleep(0.02)
        os.kill(proc.pid, signal.SIGKILL)
    finally:
        proc.wait(timeout=30)
        proc.stdout.close()
        proc.stderr.close()
    assert proc.returncode == -signal.SIGKILL


def _assert_each_once(store, seqs) -> None:
    """Every event is terminal exactly once and none is pending."""
    assert pending_seqs(store) == []
    for seq in seqs:
        assert terminal_after_mark(store, seq) == 1, seq


# --- kills ------------------------------------------------------------------

async def test_kill_after_model_call(tmp_path):
    """The model replied, nothing committed, the process dies: the job is
    still ``running`` under the dead process, its events pending and no row
    written. The next process requeues it and files each event once."""
    store = new_store(tmp_path)
    seqs = [seed(store, f"note {i}") for i in range(3)]
    job_id = writer.enqueue(store, "private", writer.ORDINARY)
    _kill_at(store, job_id, "after_model", tmp_path)

    assert [(j["state"], j["boot_id"]) for j in job_rows(store)] == [
        ("running", "boot-child")]
    assert pending_seqs(store) == seqs
    assert count(store, "SELECT count(*) AS n FROM dispositions") == 0
    assert count(store, "SELECT count(*) AS n FROM revisions") == 0

    assert writer.recover(store, "boot-b") == 1
    port = ScriptedPort(add_each)
    assert await new_writer(store, port, boot_id="boot-b").run_job(
        job_id) == writer.COMMITTED
    _assert_each_once(store, seqs)
    assert count(store, "SELECT count(*) AS n FROM objects") == 3
    assert [j["state"] for j in job_rows(store)] == ["committed"]


async def test_kill_mid_commit(tmp_path):
    """Killed inside the commit transaction after every row was written and
    before COMMIT: SQLite rolls all of it back, so nothing is half-filed."""
    store = new_store(tmp_path)
    seqs = [seed(store, f"note {i}") for i in range(3)]
    job_id = writer.enqueue(store, "private", writer.ORDINARY)
    _kill_at(store, job_id, "in_commit", tmp_path)

    assert count(store, "SELECT count(*) AS n FROM dispositions") == 0
    assert count(store, "SELECT count(*) AS n FROM objects") == 0
    assert count(store, "SELECT count(*) AS n FROM revisions") == 0
    assert pending_seqs(store) == seqs
    assert [j["state"] for j in job_rows(store)] == ["running"]

    writer.recover(store, "boot-b")
    await new_writer(store, ScriptedPort(add_each), boot_id="boot-b").run_job(
        job_id)
    _assert_each_once(store, seqs)
    assert count(store, "SELECT count(*) AS n FROM objects") == 3
    assert count(store, "SELECT count(*) AS n FROM revisions") == 3


async def test_running_job_requeued_at_boot(tmp_path):
    """A job killed during its model call is ``running`` under a dead
    process: boot recovery requeues it (and only jobs of other processes),
    and no event is terminal until the requeued run commits."""
    store = new_store(tmp_path)
    seqs = [seed(store, "a"), seed(store, "b")]
    job_id = writer.enqueue(store, "private", writer.ORDINARY)
    _kill_at(store, job_id, "model", tmp_path)
    assert [j["state"] for j in job_rows(store)] == ["running"]

    # The same process's running job is not touched.
    assert writer.recover(store, "boot-child") == 0
    assert writer.recover(store, "boot-b") == 1
    assert [(j["state"], j["boot_id"]) for j in job_rows(store)] == [
        ("queued", None)]
    assert pending_seqs(store) == seqs
    w = new_writer(store, ScriptedPort(no_change_all), boot_id="boot-b")
    assert (await w.step()) is True
    _assert_each_once(store, seqs)
    assert len(dispositions(store)) == 2


# --- the three checks --------------------------------------------------------

async def test_stale_base_rejected(tmp_path):
    """Another commit changes the ledger between a run's read and its commit:
    that commit is rejected (nothing written) and the job requeued; its
    re-run reassembles and every event ends filed once."""
    store = new_store(tmp_path)
    e1 = seed(store, "first")
    a = writer.enqueue(store, "private", writer.ORDINARY)
    b_port = ScriptedPort(add_each)

    async def a_reply(prompt):
        e2 = seed(store, "second")
        b = new_writer(store, b_port, boot_id="boot-a")
        _, outcome = await run_new(b)
        assert outcome == writer.COMMITTED
        assert {e["seq"] for e in b_port.prompts[0]["events"]} == {e1, e2}
        return no_change_all(prompt)

    class Port(ScriptedPort):
        async def complete(self, *, system, prompt):
            self.prompts.append(json.loads(prompt))
            from memory.model_port import ModelReply
            return ModelReply(text=await a_reply(json.loads(prompt)))

    w = new_writer(store, Port(None))
    assert await w.run_job(a) == writer.REQUEUED
    assert [d[2] for d in dispositions(store)] == ["patched", "patched"]
    assert next(j for j in job_rows(store) if j["job_id"] == a)["state"] == (
        "queued")
    # The re-run reassembles: nothing is pending, so it commits empty.
    assert await new_writer(store, ScriptedPort([])).run_job(a) == (
        writer.COMMITTED)
    assert len(dispositions(store)) == 2
    _assert_each_once(store, [r["seq"] for r in rows(
        store, "SELECT seq FROM events")])


async def test_mark_mid_job_rejects_and_requeues(tmp_path):
    """A mark landing while a job's model call runs: the job's commit sees a
    mark it did not read and is rejected; the correction job then files the
    event, once, after its mark."""
    store = new_store(tmp_path)
    e = seed(store, "the boiler is serviced in May")

    async def marking(prompt):
        result = await asyncio.to_thread(corrections.mark, store, e)
        assert result.marked and result.queued == ("private",)
        return no_change_all(prompt)

    class Port(ScriptedPort):
        async def complete(self, *, system, prompt):
            from memory.model_port import ModelReply
            return ModelReply(text=await marking(json.loads(prompt)))

    a = writer.enqueue(store, "private", writer.ORDINARY)
    assert await new_writer(store, Port(None)).run_job(a) == writer.REQUEUED
    assert dispositions(store) == []
    assert pending_seqs(store) == [e]

    port = ScriptedPort(no_change_all)
    w = new_writer(store, port)
    assert (await w.step()) is True        # the correction job, first
    assert [j["kind"] for j in job_rows(store) if j["state"] == "committed"] == [
        "correction"]
    assert port.prompts[0]["events"][0]["correction"]
    assert (await w.step()) is True        # the requeued job: empty now
    assert len(dispositions(store)) == 1
    assert terminal_after_mark(store, e) == 1
    assert pending_seqs(store) == []


async def test_mark_during_assembly_is_seen_or_rejected(tmp_path, monkeypatch):
    """A mark committed after the run selected its events but before it read
    the marks. Assembly is one read transaction, so the run's snapshot holds
    no mark for the event; its commit then sees one and is rejected. (Read
    separately, ``mark_seen`` would include the mark and the stale
    ``no_change`` would land as the event's terminal disposition.)"""
    store = new_store(tmp_path)
    e = seed(store, "my sister lives in Turin")
    fired = []

    def mark_now():
        if not fired:
            fired.append(corrections.mark(store, e))

    monkeypatch.setitem(writer._hooks, "selected", mark_now)
    a = writer.enqueue(store, "private", writer.ORDINARY)
    port = ScriptedPort(no_change_all)
    assert await new_writer(store, port).run_job(a) == writer.REQUEUED
    assert fired[0].marked
    assert "correction" not in port.prompts[0]["events"][0]
    assert dispositions(store) == []
    assert pending_seqs(store) == [e]
    assert terminal_after_mark(store, e) == 0

    w = new_writer(store, ScriptedPort(no_change_all))
    while await w.step():
        pass
    assert terminal_after_mark(store, e) == 1
    assert pending_seqs(store) == []


async def test_failed_job_retry_reassembles(tmp_path):
    """Job A fails three times with e pending; job B files e; the nightly
    retry of A reassembles from the pending view, finds nothing and commits
    with no disposition. e has exactly one terminal disposition."""
    store = new_store(tmp_path)
    e = seed(store, "dentist on Friday")
    a = writer.enqueue(store, "private", writer.ORDINARY)
    failing = ScriptedPort([RuntimeError("down")] * 3)
    w = new_writer(store, failing)
    assert [await w.run_job(a) for _ in range(3)] == [
        writer.RETRY, writer.RETRY, writer.FAILED]
    assert pending_seqs(store) == [e]

    b_port = ScriptedPort(no_change_all)
    _, outcome = await run_new(new_writer(store, b_port))
    assert outcome == writer.COMMITTED
    assert nightly.enqueue_nightly(store)["retried"] == 1

    retry_port = ScriptedPort([])
    assert await new_writer(store, retry_port).run_job(a) == writer.COMMITTED
    assert retry_port.prompts == []
    assert dispositions(store, e) == [(e, "private", "no_change", 1)]
    assert terminal_after_mark(store, e) == 1
    assert json.loads(next(j for j in job_rows(store)
                           if j["job_id"] == a)["input_seqs"]) == []


async def test_mark_behind_a_commit_reopens_and_queues(tmp_path, monkeypatch):
    """The mark's ``BEGIN IMMEDIATE`` waits behind a commit filing e as
    ``no_change`` from a read without the mark. The mark lands after it with
    that commit's number, so the disposition does not count: e is pending
    again, one correction job is queued, and its commit gives e its one
    terminal disposition after the mark."""
    store = new_store(tmp_path)
    e = seed(store, "the car is blue")
    marking: dict = {}

    def contend():
        def run():
            marking["result"] = corrections.mark(store, e)
        t = threading.Thread(target=run)
        t.start()
        marking["thread"] = t
        time.sleep(0.3)
        # Still waiting on the write lock this commit holds.
        assert t.is_alive() and "result" not in marking

    monkeypatch.setitem(writer._hooks, "in_commit", contend)
    a = writer.enqueue(store, "private", writer.ORDINARY)
    assert await new_writer(store, ScriptedPort(no_change_all)).run_job(a) == (
        writer.COMMITTED)
    marking["thread"].join(30)
    monkeypatch.setitem(writer._hooks, "in_commit", lambda: None)

    assert marking["result"].marked
    assert marking["result"].queued == ("private",)
    [mark] = rows(store, "SELECT * FROM correction_marks")
    assert dispositions(store) == [(e, "private", "no_change", 1)]
    assert mark["mark_commit_no"] == 1
    assert pending_seqs(store) == [e]
    queued = [j for j in job_rows(store) if j["state"] == "queued"]
    assert [(j["kind"], j["ledger"]) for j in queued] == [
        ("correction", "private")]

    port = ScriptedPort(no_change_all)
    assert await new_writer(store, port).run_job(queued[0]["job_id"]) == (
        writer.COMMITTED)
    assert port.prompts[0]["events"][0]["correction"]
    assert terminal_after_mark(store, e) == 1
    assert pending_seqs(store) == []


async def test_mark_on_a_long_filed_event_reopens(tmp_path):
    """e was filed long ago, a session started after that commit, then e is
    marked: e is a pending correction in the ledger (what the session's
    correction note reads), and a correction job is queued."""
    store = new_store(tmp_path)
    e = seed(store, "I take my coffee black")
    other = seed(store, "unrelated")
    await run_new(new_writer(store, ScriptedPort(add_each)))
    with store.transaction() as conn:
        base_commit_no = pending.current_commit_no(conn)
        assert pending.pending_corrections(conn, "private") == []
    assert base_commit_no == 1 and pending_seqs(store) == []

    result = corrections.mark(store, e)
    assert result.marked and result.queued == ("private",)
    with store.transaction() as conn:
        assert pending.pending_corrections(conn, "private") == [e]
        assert pending.pending_seqs(conn, "private") == [e]
        assert pending.current_commit_no(conn) == base_commit_no
    assert other not in pending_seqs(store)
    assert [(j["kind"], j["state"]) for j in job_rows(store)][-1] == (
        "correction", "queued")


async def test_retried_mark_is_idempotent(tmp_path):
    """A second mark while the first is still pending writes nothing and
    queues nothing; once the correction is filed, a new mark re-opens."""
    store = new_store(tmp_path)
    e = seed(store, "the gate code changed")
    first = corrections.mark(store, e)
    before_marks = rows(store, "SELECT * FROM correction_marks")
    before_jobs = job_rows(store)
    second = corrections.mark(store, e)
    assert first.marked and first.queued == ("private",)
    assert second.marked is False and second.queued == ()
    assert rows(store, "SELECT * FROM correction_marks") == before_marks
    assert job_rows(store) == before_jobs and len(before_jobs) == 1

    w = new_writer(store, ScriptedPort(no_change_all))
    assert await w.step()
    third = corrections.mark(store, e)
    assert third.marked
    [mark] = rows(store, "SELECT * FROM correction_marks")
    assert (mark["mark_no"], mark["mark_commit_no"]) == (2, 1)
    assert pending_seqs(store) == [e]
    assert corrections.mark(store, 999_999).found is False


async def test_condense_from_a_stale_base_is_rejected(tmp_path):
    """An ordinary commit lands between a condense job's read and its
    commit: the condense is rejected and requeued, and the ordinary commit's
    content survives. (A direct write from the nightly pass would overwrite
    it with the stale condensed text.)"""
    store = new_store(tmp_path)
    long_body = "a long account of the move " * 200
    e1 = seed(store, long_body)
    await run_new(new_writer(store, ScriptedPort(add_each)))
    [entry] = rows(store, "SELECT * FROM objects")
    assert writer.tokens(entry["body"]) > writer.CONDENSE_OVER_TOKENS
    assert nightly.enqueue_nightly(store)["condense"] == ["private"]
    [condense] = [j for j in job_rows(store) if j["state"] == "queued"]

    e2 = seed(store, "update 7f3a: the move is done")

    def extend(prompt):
        return reply({e2: "patched"}, [{
            "op": "replace", "object_id": entry["object_id"],
            "body": long_body + " update 7f3a", "source_seqs": [e2]}])

    async def condense_reply(prompt):
        _, outcome = await run_new(new_writer(store, ScriptedPort(extend)))
        assert outcome == writer.COMMITTED
        assert prompt["entries"] == [entry["object_id"]]
        return reply(ops=[{"op": "replace", "object_id": entry["object_id"],
                           "body": "moved (condensed)", "cues": ["move"]}])

    class Port(ScriptedPort):
        async def complete(self, *, system, prompt):
            from memory.model_port import ModelReply
            return ModelReply(text=await condense_reply(json.loads(prompt)))

    assert await new_writer(store, Port(None)).run_job(
        condense["job_id"]) == writer.REQUEUED
    [now] = rows(store, "SELECT * FROM objects")
    assert now["body"].endswith("update 7f3a") and now["revision"] == 2
    assert [j["state"] for j in job_rows(store)
            if j["job_id"] == condense["job_id"]] == ["queued"]
    # Condense writes no disposition; the two events are filed once each.
    _assert_each_once(store, [e1, e2])
    assert len(dispositions(store)) == 2


# --- only the commit marks a job committed ----------------------------------

_COMMITTING = re.compile(r"state\s*=\s*'committed'")


def _committing_sites(code_root) -> list[str]:
    """Where the memory package's code can set a job ``committed``: every
    SQL text that does, by enclosing name, and every use of such a text."""
    sites: list[str] = []
    for path in sorted((code_root / "memory").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        holders: set[str] = set()

        def walk(node, scope):
            for child in ast.iter_child_nodes(node):
                name = scope
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef)):
                    name = f"{scope}.{child.name}" if scope else child.name
                elif isinstance(child, ast.Assign) and not scope:
                    name = ",".join(t.id for t in child.targets
                                    if isinstance(t, ast.Name))
                if (isinstance(child, ast.Constant)
                        and isinstance(child.value, str)
                        and _COMMITTING.search(child.value)):
                    sites.append(f"{path.name}:{name}")
                    holders.add(name)
                walk(child, name)

        walk(tree, "")

        def uses(node, scope):
            for child in ast.iter_child_nodes(node):
                name = scope
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = f"{scope}.{child.name}" if scope else child.name
                if (isinstance(child, ast.Name) and child.id in holders
                        and isinstance(child.ctx, ast.Load)):
                    sites.append(f"{path.name}:{name} uses {child.id}")
                uses(child, name)

        uses(tree, "")
    return sites


def test_only_the_commit_marks_a_job_committed():
    """INV-MEM-028: one SQL text in the memory package sets a job
    ``committed``, and only the commit transaction runs it."""
    assert _committing_sites(CODE_ROOT) == [
        "writer.py:_SET_COMMITTED", "writer.py:_commit uses _SET_COMMITTED"]


def test_only_the_commit_marks_a_job_committed_red_case(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "x.py").write_text(
        "def finish(conn):\n"
        "    conn.execute(\"UPDATE jobs SET state = 'committed'\")\n")
    assert _committing_sites(tmp_path) == ["x.py:finish"]


# --- a mark and its job; two writers ------------------------------------------

async def test_a_mark_and_its_correction_job_are_stored_together(tmp_path):
    """The store stops taking writes the moment the mark's transaction
    commits. The mark's correction job must already be stored with it: a
    retried mark is idempotent and queues nothing, and the cadence never
    re-offers an event this process was already given, so a job queued after
    the mark would be lost with the outage."""
    if os.geteuid() == 0:
        pytest.skip("a permission flip does not stop root")
    store = new_store(tmp_path)
    e = seed(store, "my gym is on Elm Street")
    w = new_writer(store, ScriptedPort(no_change_all))
    await run_new(w)
    assert pending_seqs(store) == []

    real = store.transaction
    writes = []

    @contextmanager
    def outage_after_first_write(**kw):
        with real(**kw) as conn:
            yield conn
        if kw.get("write"):
            writes.append(1)
            if len(writes) == 1:
                os.chmod(store.path, 0)

    store.transaction = outage_after_first_write
    try:
        try:
            corrections.mark(store, e)
        except sqlite3.OperationalError:
            pass                            # the outage cut the call short
    finally:
        os.chmod(store.path, 0o600)
        del store.transaction
    assert corrections.mark(store, e).marked is False   # the retry
    assert pending_seqs(store) == [e]
    queued = [j for j in job_rows(store) if j["state"] == "queued"]
    assert [(j["kind"], j["ledger"]) for j in queued] == [
        ("correction", "private")]
    assert w.schedule_due() == []           # the cadence would not save it
    assert await w.pass_once() is True
    assert terminal_after_mark(store, e) == 1
    assert pending_seqs(store) == []


def _second_writer_files(store, script, a_script=no_change_all):
    """A port for writer A whose model call lets writer B (another process
    id) queue a job, assemble, file with *script* and commit first; A then
    answers with *a_script*."""
    from memory.model_port import ModelReply

    class Port(ScriptedPort):
        async def complete(self, *, system, prompt):
            parsed = json.loads(prompt)
            self.prompts.append(parsed)
            b = writer.enqueue(store, "private", writer.ORDINARY)
            assert b is not None            # a queued job while A runs
            other = new_writer(store, ScriptedPort(script), boot_id="boot-b")
            assert await other.run_job(b) == writer.COMMITTED
            return ModelReply(text=a_script(parsed))
    return Port(None)


@pytest.mark.parametrize("a_script", [no_change_all, add_each],
                         ids=["a-no-change", "a-adds"])
async def test_two_writers_never_file_an_event_twice(tmp_path, a_script):
    """Writers A and B (two process ids, one WAL store) both read e as
    pending; B commits ``no_change`` first, which changes no revision and no
    mark. A's commit re-reads the pending view, finds e filed and writes
    nothing (not even A's patch): e has one terminal disposition."""
    store = new_store(tmp_path)
    e = seed(store, "the bins go out on Tuesday")
    a = writer.enqueue(store, "private", writer.ORDINARY)
    port = _second_writer_files(store, no_change_all, a_script)
    assert await new_writer(store, port, boot_id="boot-a").run_job(a) == (
        writer.COMMITTED)
    assert [e_["seq"] for e_ in port.prompts[0]["events"]] == [e]
    assert dispositions(store) == [(e, "private", "no_change", 1)]
    assert terminal_after_mark(store, e) == 1
    assert rows(store, "SELECT * FROM revisions") == []
    assert [j["state"] for j in job_rows(store)] == ["committed"] * 2


@pytest.mark.parametrize("a_script", [no_change_all, add_each],
                         ids=["a-no-change", "a-adds"])
async def test_a_second_writer_filing_some_events_makes_the_run_stale(
        tmp_path, a_script):
    """B files one of A's two events and defers the other. A's answer was
    made for both and may cite the filed one, so A's commit writes nothing
    and requeues the job; its rerun is given only the surviving event and
    files it once. (Writing A's answer would add an object citing an event B
    already filed.)"""
    store = new_store(tmp_path)
    e1, e2 = seed(store, "one"), seed(store, "two")
    a = writer.enqueue(store, "private", writer.ORDINARY)
    port = _second_writer_files(
        store, lambda p: reply({e1: "no_change", e2: "deferred"}), a_script)
    assert await new_writer(store, port, boot_id="boot-a").run_job(a) == (
        writer.REQUEUED)
    assert dispositions(store) == [(e1, "private", "no_change", 1),
                                   (e2, "private", "deferred", 1)]
    assert rows(store, "SELECT * FROM revisions") == []
    assert pending_seqs(store) == [e2]

    rerun = ScriptedPort(a_script)
    assert await new_writer(store, rerun, boot_id="boot-a").run_job(a) == (
        writer.COMMITTED)
    assert [e["seq"] for e in rerun.prompts[0]["events"]] == [e2]
    assert terminal_after_mark(store, e1) == terminal_after_mark(store, e2) == 1
    assert pending_seqs(store) == []
    cited = [json.loads(r["source_seqs"]) for r in rows(
        store, "SELECT source_seqs FROM revisions")]
    assert all(e1 not in c for c in cited)
