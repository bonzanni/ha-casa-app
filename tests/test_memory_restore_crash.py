"""§14 test 10, crash points (INV-MEM-037; hotspot H4): the container-start
step carrying out a restore, killed (a real SIGKILL of the process the test
spawned) at each point, then run again.

* Any point before the intent records phase ``wiping``: memory is the
  pre-restore memory, untouched, and the next run builds again.
* Any point at or after it, right after the install included: the next run
  wipes again and installs again from ``built.db``, which is still there with
  its recorded hash.
* Between deleting the intent and deleting ``restore-source/``, on success or
  on abandonment: the next run deletes the orphan source and leaves memory as
  it is.

Each ends equal to a clean run: memory is exactly the export, and the
transcripts the export does not carry are gone."""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

import memory_restore_support as r
import memory_wipe_support as w
from memory import export, maintenance

pytestmark = [pytest.mark.unit]

TRANSCRIPT = f"/config/cc-home/.claude/projects/-a/{w.SID}.jsonl"


def _tree(tmp_path: Path, name: str):
    root, proc = tmp_path / name, w.fake_proc(tmp_path)
    store = r.store_at(root)
    r.populate(store)
    path = shutil.move(export.export(store), str(root.parent / f"{name}-x"))
    r.put(store, "said after the export")
    canary = os.urandom(8).hex()
    w.write(w.under(root, TRANSCRIPT), canary)
    assert r.stage(root, path) == "staged"
    return root, proc, store, path, canary


def _texts(db: str) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [t for (t,) in conn.execute("SELECT text FROM events "
                                           "ORDER BY seq")]
    finally:
        conn.close()


def _clean_result(root: Path, store, path: str, canary: str) -> None:
    assert r.nothing_scheduled(root)
    r.assert_store_equals_export(store.path, path)
    assert w.hits(root, [canary]) == []


def _phase(root: Path) -> str | None:
    intent = maintenance.read_intent(str(root))
    return None if intent is None else intent.get("phase")


# --- before phase wiping: memory intact, the build runs again ------------------------

@pytest.mark.parametrize("point", ["_load", "_check", "_seal"])
def test_a_kill_in_the_build_leaves_memory_intact_and_builds_again(
        tmp_path, point):
    root, proc, store, path, canary = _tree(tmp_path, point)
    first = r.run_step(root, proc, kill=f"kill_after(r, {point!r}, 1)")
    assert w.killed(first), first.stderr
    assert _phase(root) == "staged"
    assert "said after the export" in _texts(store.path)
    assert w.hits(root, [canary]) != []
    second = r.run_step(root, proc)
    assert second.stdout.strip() == "done", second.stderr
    _clean_result(root, store, path, canary)


def test_a_kill_after_the_wiping_write_reruns_from_the_wipe(tmp_path):
    root, proc, store, path, canary = _tree(tmp_path, "built")
    first = r.run_step(root, proc,
                       kill="kill_after(r, 'build_phase', 1)")
    assert w.killed(first), first.stderr
    intent = maintenance.read_intent(str(root))
    assert intent["phase"] == "wiping" and intent["built_sha256"]
    # Nothing is wiped yet, and the built store is one file.
    assert "said after the export" in _texts(store.path)
    assert r.listing(root, r.RESTORE_SOURCE).count("built.db") == 1
    assert not [n for n in r.listing(root, r.RESTORE_SOURCE)
                if n.startswith("built.db-")]
    second = r.run_step(root, proc)
    assert second.stdout.strip() == "done", second.stderr
    _clean_result(root, store, path, canary)


# --- at or after phase wiping: the wipe and the install run again --------------------

def _crash_in_wiping(tmp_path: Path, k: int) -> bool:
    root, proc, store, path, canary = _tree(tmp_path, f"wipe-{k}")
    # Build first, so every deletion counted here is the wipe's or later.
    built = r.run_step(root, proc, kill="kill_after(r, 'build_phase', 1)")
    assert w.killed(built), built.stderr
    first = r.run_step(root, proc, kill=f"kill_after(m, '_remove', {k})")
    if not w.killed(first):
        assert first.stdout.strip() == "done", first.stderr
        return False
    if maintenance.read_intent(str(root)) is not None:
        assert _phase(root) == "wiping"
        built_db = w.under(root, r.RESTORE_SOURCE) / "built.db"
        assert r.sha(built_db) == maintenance.read_intent(str(root))[
            "built_sha256"]
    second = r.run_step(root, proc)
    assert second.stdout.strip() in ("done", "none"), second.stderr
    _clean_result(root, store, path, canary)
    return True


def test_every_kill_mid_wipe_reruns_to_a_clean_result(tmp_path):
    kills = 0
    for k in range(1, 60):
        if not _crash_in_wiping(tmp_path, k):
            break
        kills += 1
    else:
        pytest.fail("the step never completed without a kill")
    assert kills >= 15, kills


def test_a_kill_right_after_the_install_reruns_to_a_clean_result(tmp_path):
    root, proc, store, path, canary = _tree(tmp_path, "installed")
    # The rule table holds the install step itself, so it is wrapped there.
    first = r.run_step(root, proc, kill="\n".join([
        "_install = r._install",
        "def _then_die(*a, **k):",
        "    _install(*a, **k)",
        "    _die()",
        "r._AFTER_WIPE[(r.Kind.FILE, r.Restore.INSTALL)] = _then_die"]))
    assert w.killed(first), first.stderr
    assert _phase(root) == "wiping"
    # The install is complete, and the next run wipes it and installs again.
    r.assert_store_equals_export(store.path, path)
    second = r.run_step(root, proc)
    assert second.stdout.strip() == "done", second.stderr
    _clean_result(root, store, path, canary)


def test_a_kill_between_the_intent_and_the_source_on_success(tmp_path):
    root, proc, store, path, canary = _tree(tmp_path, "tail")
    first = r.run_step(root, proc, kill=r.kill_before_removing_the_source(root))
    assert w.killed(first), first.stderr
    assert maintenance.read_intent(str(root)) is None
    assert w.under(root, r.RESTORE_SOURCE).exists()
    second = r.run_step(root, proc)
    assert second.stdout.strip() == "none", second.stderr
    _clean_result(root, store, path, canary)


# --- abandonment ------------------------------------------------------------------------

def test_an_abandonment_cut_between_the_intent_and_the_source(tmp_path):
    """H4-Astra S2: the intent goes first, so the next run finds an orphan
    source, deletes it, and memory is intact; a retry with the source
    already gone is a plain run."""
    root, proc, store, path, canary = _tree(tmp_path, "abandon")
    with open(w.under(root, r.RESTORE_SOURCE) / "events.jsonl", "ab") as fh:
        fh.write(b"\n")                       # fails the re-validation
    before = _texts(store.path)
    first = r.run_step(root, proc, kill=r.kill_before_removing_the_source(root))
    assert w.killed(first), first.stderr
    assert maintenance.read_intent(str(root)) is None
    assert w.under(root, r.RESTORE_SOURCE).exists()
    for _ in range(2):                         # the retry, with it gone
        again = r.run_step(root, proc)
        assert again.stdout.strip() == "none", again.stderr
        assert not w.under(root, r.RESTORE_SOURCE).exists()
    assert _texts(store.path) == before
    assert w.hits(root, [canary]) != []        # nothing was wiped
    line = maintenance.claim_outcome_notice(r.store_at(root))
    assert line.startswith("The restore didn't happen (events.jsonl")


# --- counters survive a restore (R2-H12) ------------------------------------------------

async def test_counters_survive_a_restore(tmp_path):
    """e is marked at mark_commit_no 10 and still pending; f was filed at
    commit_no 10. After the restore, e's correction commit takes commit_no
    11 and files it, and marking f re-opens it and queues its job."""
    import memory_writer_support as ws
    from memory import corrections, pending, writer
    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    store = r.store_at(root)
    e = r.put(store, "the gate code is 4411")
    f = r.put(store, "the boiler is serviced in May")
    with store.transaction(write=True) as conn:
        for n in range(1, 11):
            conn.execute(
                "INSERT INTO dispositions (seq, ledger, job_id, outcome, "
                "commit_no, committed_at) VALUES (?, 'private', ?, "
                "'no_change', ?, ?)", (f if n == 10 else e, f"job-{n}", n,
                                       r.AT))
        conn.execute("INSERT INTO correction_marks (seq, mark_no, marked_at, "
                     "mark_commit_no) VALUES (?, 3, ?, 10)", (e, r.AT))
        assert pending.is_pending(conn, e, "private")
        assert not pending.is_pending(conn, f, "private")
    path = shutil.move(export.export(store), str(tmp_path / "x"))
    assert r.stage(root, path) == "staged"
    assert r.run_step(root, proc).stdout.strip() == "done"

    after = r.store_at(root)
    with after.transaction() as conn:
        assert pending.next_commit_no(conn) == 11
        assert pending.next_mark_no(conn) == 4
    w_ = ws.new_writer(after, ws.ScriptedPort(ws.no_change_all))
    with after.transaction(write=True) as conn:
        job = writer._enqueue(conn, "private", writer.CORRECTION)
    assert await w_.run_job(job) == "committed"
    assert (e, "private", "no_change", 11) in ws.dispositions(after, e)
    assert ws.terminal_after_mark(after, e) == 1
    assert e not in ws.pending_seqs(after)
    marked = corrections.mark(after, f)
    assert marked.marked and marked.queued == ("private",)
    assert f in ws.pending_seqs(after)


# --- built.db is one file (H4-Terra S1) -------------------------------------------------

def test_the_built_store_is_one_file_and_installs_every_row(tmp_path):
    root, proc, store, path, canary = _tree(tmp_path, "single")
    first = r.run_step(root, proc, kill="kill_after(r, 'build_phase', 1)")
    assert w.killed(first), first.stderr
    source = w.under(root, r.RESTORE_SOURCE)
    assert [n for n in os.listdir(source) if n.startswith("built.db")] == [
        "built.db"]
    conn = sqlite3.connect(source / "built.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    finally:
        conn.close()
    # A copy of that one file alone holds every exported row.
    alone = tmp_path / "alone.db"
    shutil.copyfile(source / "built.db", alone)
    r.assert_store_equals_export(str(alone), path)
    assert r.run_step(root, proc).stdout.strip() == "done"
    _clean_result(root, store, path, canary)


def test_a_built_store_that_no_longer_matches_is_never_installed(tmp_path):
    root, proc, store, path, canary = _tree(tmp_path, "tampered")
    first = r.run_step(root, proc, kill="kill_after(r, 'build_phase', 1)")
    assert w.killed(first), first.stderr
    with open(w.under(root, r.RESTORE_SOURCE) / "built.db", "ab") as fh:
        fh.write(b"\0" * 512)
    second = r.run_step(root, proc)
    assert second.stdout.strip() == "failed", second.stderr
    intent = maintenance.read_intent(str(root))
    assert intent["attempts"][-1]["error"] == (
        "the built store does not match its record")
    # Stopped before the wipe: nothing was deleted.
    assert "said after the export" in _texts(store.path)
    assert w.hits(root, [canary]) != []
