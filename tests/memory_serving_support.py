"""Shared helpers for the memory-serving tests: a real store whose ledger is
written through the writer's own patch and commit counters, wired as the
process's store. No model is called."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from memory import capture, patches, pending
from memory.store import MemoryStore


def ledger_store(tmp_path: Path, entries: list[dict] | None = None,
                 ledger: str = "private") -> MemoryStore:
    store = MemoryStore(str(tmp_path / "memory"))
    assert store.open() is True, store.unavailable_reason
    if entries:
        add(store, entries, ledger=ledger)
    return store


def fact(subject: str, body: str, *, topic: str = "Home",
         line: str | None = None, **extra) -> dict:
    return {"op": "add", "kind": "entry", "entry_kind": "fact",
            "topic": topic, "subject": subject, "body": body,
            "line": line or f"what {subject} is", **extra}


def add(store: MemoryStore, ops: list[dict], *,
        ledger: str = "private") -> tuple[int, list[str]]:
    """Apply *ops* to *ledger* as one commit; returns its ``commit_no`` and
    the ids written."""
    with store.transaction(write=True) as conn:
        commit_no = pending.next_commit_no(conn)
        touched = patches.apply(
            conn, ledger, ops, job_id=f"job-{commit_no}", commit_no=commit_no,
            committed_at=datetime.now(timezone.utc).isoformat())
    return commit_no, touched


def wire(monkeypatch, store: MemoryStore | None) -> None:
    """Make *store* the process's memory store for one test."""
    monkeypatch.setitem(capture._wiring, "store", store)
    monkeypatch.setitem(capture._wiring, "notify", None)
