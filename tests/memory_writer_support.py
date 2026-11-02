"""Shared helpers for the memory writer's tests: a real store, seeded
events, a deterministic model port and row readers. No model is called."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from memory import capture, writer
from memory.model_port import ModelReply
from memory.store import MemoryStore

CODE_ROOT = Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"


def new_store(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(str(tmp_path / "memory"))
    assert store.open() is True, store.unavailable_reason
    return store


def seed(store: MemoryStore, text: str, *, speaker: str = "operator",
         session: str | None = "s1", tier_status: str = "provisional",
         tier: str = "private", origin_tier: str | None = None) -> int:
    """Store one event through capture's own append; returns its seq."""
    item = capture.event(role="assistant", channel="telegram",
                         speaker_kind=speaker, origin_kind="operator",
                         text=text, eligibility="write_trusted_turn",
                         origin_tier=origin_tier, session_id=session,
                         tier=tier, tier_status=tier_status)
    receipt = capture.append(store, item)
    assert receipt is not None and receipt.outcome == capture.STORED
    return rows(store, "SELECT seq FROM events WHERE source_key = ?",
                item.source_key)[0]["seq"]


def rows(store: MemoryStore, sql: str, *args) -> list[dict]:
    conn = sqlite3.connect(store.path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def count(store: MemoryStore, sql: str, *args) -> int:
    return rows(store, sql, *args)[0]["n"]


def dispositions(store: MemoryStore, seq: int | None = None) -> list[tuple]:
    sql = "SELECT seq, ledger, outcome, commit_no FROM dispositions"
    args: tuple = ()
    if seq is not None:
        sql += " WHERE seq = ?"
        args = (seq,)
    return [(r["seq"], r["ledger"], r["outcome"], r["commit_no"])
            for r in rows(store, sql + " ORDER BY id", *args)]


def terminal_after_mark(store: MemoryStore, seq: int,
                        ledger: str = "private") -> int:
    """Terminal dispositions of *seq* in *ledger* committed after its mark."""
    return count(store, (
        "SELECT count(*) AS n FROM dispositions AS d WHERE d.seq = ? "
        "AND d.ledger = ? AND d.outcome IN ('patched', 'no_change') "
        "AND d.commit_no > coalesce((SELECT mark_commit_no FROM "
        "correction_marks WHERE seq = d.seq), 0)"), seq, ledger)


def pending_seqs(store: MemoryStore, ledger: str = "private") -> list[int]:
    return [r["seq"] for r in rows(
        store, "SELECT seq FROM pending WHERE ledger = ? ORDER BY seq", ledger)]


def job_rows(store: MemoryStore) -> list[dict]:
    return rows(store, "SELECT * FROM jobs ORDER BY rowid")


def reply(events: dict | None = None, ops: list | None = None) -> str:
    return json.dumps({"ops": ops or [],
                       "events": {str(k): v for k, v in (events or {}).items()}})


def no_change_all(prompt: dict) -> str:
    """A reply filing every event it was given as ``no_change``."""
    return reply({e["seq"]: "no_change" for e in prompt.get("events", [])})


def add_each(prompt: dict) -> str:
    """A reply adding one fact per event it was given."""
    ops = [{"op": "add", "kind": "entry", "entry_kind": "fact",
            "subject": "operator", "body": e["text"], "line": "a fact",
            "source_seqs": [e["seq"]]} for e in prompt.get("events", [])]
    return reply({e["seq"]: "patched" for e in prompt.get("events", [])}, ops)


class ScriptedPort:
    """A deterministic model port. *script* is a callable from the parsed
    prompt to the reply text, or a list of such callables / texts used in
    turn. Every prompt is recorded."""

    def __init__(self, script) -> None:
        self.script = script
        self.prompts: list[dict] = []
        self.systems: list[str] = []

    async def complete(self, *, system: str, prompt: str) -> ModelReply:
        parsed = json.loads(prompt)
        self.prompts.append(parsed)
        self.systems.append(system)
        step = self.script
        if isinstance(step, list):
            step = step.pop(0)
        if isinstance(step, BaseException):
            raise step
        text = step(parsed) if callable(step) else step
        return ModelReply(text=text, usage={"input_tokens": 1,
                                            "output_tokens": 1}, cost_usd=0.0)


async def keep_provisional(text: str) -> None:
    """A classifier that always falls back."""
    return None


def classifier_of(tier: str):
    async def classify(text: str) -> str:
        return tier
    return classify


def new_writer(store: MemoryStore, port, *, classifier=keep_provisional,
               boot_id: str = "boot-a", notify=None, clock=None) -> writer.Writer:
    kw = {"clock": clock} if clock is not None else {}
    return writer.Writer(store, port, classifier=classifier, notify=notify,
                         boot_id=boot_id, **kw)


async def run_new(w: writer.Writer, kind: str = writer.ORDINARY,
                  ledger: str = "private") -> tuple[str, str]:
    """Queue a job and run it; returns (job_id, outcome)."""
    job_id = writer.enqueue(w.store, ledger, kind)
    assert job_id is not None
    return job_id, await w.run_job(job_id)
