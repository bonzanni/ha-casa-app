"""Shared fixtures for the memory export and restore tests: a stand-in root
whose store holds a row in every exported table (a running and a failed job,
a correction mark, a ledger entry), the container-start step run as a real
process that can be killed at a chosen point of :mod:`memory.maintenance` or
:mod:`memory.restore`, and the comparison of a store with an export.

Every process killed here is one these helpers spawned; a kill is always the
child signalling itself."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import memory_wipe_support as w
from memory import capture, export, maintenance, registry, restore
from memory.store import MemoryStore

RESTORE_SOURCE = registry.RESTORE_SOURCE


def store_at(root: Path) -> MemoryStore:
    store = MemoryStore(str(w.under(root, registry.MEMORY_DIR)))
    assert store.open() is True, store.unavailable_reason
    return store


def event(text: str, **kw):
    return capture.event(role="assistant", channel="telegram",
                         speaker_kind="operator", origin_kind="operator",
                         text=text, eligibility="write_trusted_turn",
                         origin_tier="private", **kw)


def put(store: MemoryStore, text: str, **kw) -> int:
    item = event(text, **kw)
    assert capture.append(store, item).outcome == capture.STORED
    with store.transaction() as conn:
        return conn.execute("SELECT seq FROM events WHERE source_key = ?",
                            (item.source_key,)).fetchone()[0]


AT = "2026-10-01T09:00:00.000000+00:00"


def populate(store: MemoryStore, word: str = "roses") -> dict:
    """A row in every exported table: four events (one classified at
    ``family``), a ledger entry with its revision, its dispositions, a
    committed, a running, a failed and a queued job, a correction mark and
    an engagement summary."""
    seqs = [put(store, f"{word} {i}") for i in range(3)]
    seqs.append(put(store, f"{word} classified", tier="family",
                    tier_status="classified"))
    with store.transaction(write=True) as conn:
        entry = ("private", "o1", 1, "entry", "fact", "garden",
                 f"the {word} are pruned in March", "pruning",
                 json.dumps([seqs[0]]), "current")
        conn.execute(
            "INSERT INTO objects (ledger, object_id, revision, kind, "
            "entry_kind, subject, body, line, source_seqs, status) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", entry)
        conn.execute(
            "INSERT INTO revisions (ledger, object_id, revision, kind, "
            "entry_kind, subject, body, line, source_seqs, status, op, "
            "job_id, committed_at, commit_no) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'add', 'job-a', ?, 1)",
            (*entry, AT))
        for seq, outcome in ((seqs[0], "patched"), (seqs[1], "no_change")):
            conn.execute(
                "INSERT INTO dispositions (seq, ledger, job_id, outcome, "
                "commit_no, committed_at) VALUES (?, 'private', 'job-a', ?, "
                "1, ?)", (seq, outcome, AT))
        for job_id, state in (("job-a", "committed"), ("job-b", "running"),
                              ("job-c", "failed"), ("job-d", "queued")):
            conn.execute(
                "INSERT INTO jobs (job_id, ledger, kind, state, boot_id, "
                "attempts) VALUES (?, 'private', 'ordinary', ?, 'boot-x', 1)",
                (job_id, state))
        conn.execute(
            "INSERT INTO correction_marks (seq, mark_no, marked_at, "
            "mark_commit_no) VALUES (?, 1, ?, 1)", (seqs[1], AT))
        conn.execute(
            "INSERT INTO engagement_summaries (source_key, role, tier, text, "
            "engagement_id, at) VALUES ('event:summary-1', 'finance', "
            "'private', ?, 'e1', ?)", (f"summary of {word}", AT))
    return {"seqs": seqs}


def table_lines(db: str, table: str) -> bytes:
    conn = sqlite3.connect(db)
    try:
        return b"".join(export.dump_table(conn, table))
    finally:
        conn.close()


def assert_store_equals_export(db: str, directory: str) -> None:
    """Every exported table of *db* is the export's file byte for byte,
    except that a ``running`` job is back in the queue; the obligations and
    counts equal the manifest's."""
    manifest = export.read_manifest(directory)
    for table in manifest["counts"]["tables"]:
        want = (Path(directory) / f"{table}.jsonl").read_bytes()
        got = table_lines(db, table)
        if table == "jobs":
            want = b"".join(_requeued(line) for line in want.splitlines(True))
        assert got == want, table
    conn = sqlite3.connect(db)
    try:
        assert export.counts(conn, list(manifest["counts"]["tables"])) == (
            manifest["counts"])
        assert export.obligations(conn) == manifest["obligations"]
    finally:
        conn.close()


def _requeued(line: bytes) -> bytes:
    row = json.loads(line)
    if row["state"] == "running":
        row.update(state="queued", boot_id=None)
    return (json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            + "\n").encode()


def sha(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stage(root: Path, path: str) -> str:
    return restore.stage(path, root=str(root), confirm=True)


def nothing_scheduled(root: Path) -> bool:
    return (maintenance.read_intent(str(root)) is None
            and not w.under(root, RESTORE_SOURCE).exists())


# --- the step in a real process, killable inside either module ----------------

def run_step(root: Path, proc: Path, *, kill: str = "",
             now: float | None = None) -> subprocess.CompletedProcess:
    """``memory.maintenance.run`` in its own process. *kill* is code run
    first, with ``m`` (maintenance), ``r`` (restore), ``_die()`` and the
    helper ``kill_after(module, name, k)`` in scope."""
    body = "\n".join([
        "from memory import maintenance as m, restore as r",
        "def kill_after(mod, name, k, when=None):",
        "    real = getattr(mod, name)",
        "    def wrapped(*a, **kw):",
        "        if when is not None and when(*a, **kw):",
        "            _die()",
        "        out = real(*a, **kw)",
        "        if when is None:",
        "            _calls['n'] += 1",
        "            if _calls['n'] >= k:",
        "                _die()",
        "        return out",
        "    setattr(mod, name, wrapped)",
        kill,
        f"print(m.run(root={str(root)!r}, proc_root={str(proc)!r}, "
        f"now={now!r}))",
    ])
    return w.run_child(body)


def kill_before_removing_the_source(root: Path) -> str:
    """Die as the step is about to delete ``restore-source/`` with no intent
    left: between the two last deletions, on success or on abandonment."""
    intent = maintenance.intent_path(str(root))
    return ("import os\n"
            "kill_after(m, '_remove', 0, when=lambda p, *a, **k: "
            f"p.endswith('restore-source') and not os.path.exists({intent!r}))")


def listing(root: Path, declared: str) -> list[str]:
    path = w.under(root, declared)
    return sorted(os.listdir(path)) if path.exists() else []
