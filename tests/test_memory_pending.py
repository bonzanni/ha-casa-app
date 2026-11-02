"""The one pending predicate (INV-MEM-027).

``pending(e, L)``: e is eligible for L (its tier at or below L, classified
unless L is ``private``) and has no terminal disposition in L committed after
its latest correction mark. ``deferred`` counts as pending. The predicate is
the ``pending`` view, and no other record of whether an event was handled
exists in the memory package or its store. The two counters it is ordered by
are derived from the rows that carry them.
"""
from __future__ import annotations

import ast
import re
import shutil
import sqlite3

from memory import corrections, pending
from memory.store import MemoryStore
from memory_writer_support import (
    CODE_ROOT, ScriptedPort, new_store, new_writer, pending_seqs, reply,
    run_new, rows, seed,
)


async def test_deferred_counts_as_pending(tmp_path):
    """A ``deferred`` disposition leaves the event pending; a later
    ``patched`` one files it; ``no_change`` is terminal too."""
    store = new_store(tmp_path)
    a, b, c = (seed(store, t) for t in ("one", "two", "three"))
    port = ScriptedPort([
        lambda p: reply({a: "deferred", b: "no_change", c: "deferred"}),
        lambda p: reply({a: "patched", c: "deferred"}, [
            {"op": "add", "kind": "entry", "entry_kind": "fact",
             "body": "one", "source_seqs": [a]}]),
    ])
    w = new_writer(store, port)
    await run_new(w)
    assert pending_seqs(store) == [a, c]
    await run_new(w)
    assert [e["seq"] for e in port.prompts[1]["events"]] == [a, c]
    assert pending_seqs(store) == [c]
    assert rows(store, "SELECT outcome FROM dispositions WHERE seq = ? "
                       "ORDER BY id", a) == [{"outcome": "deferred"},
                                             {"outcome": "patched"}]


def test_eligibility_by_tier_and_classification(tmp_path):
    """A provisional event is pending in ``private`` only; a classified one
    in every ledger at or above its tier."""
    store = new_store(tmp_path)
    prov = seed(store, "provisional")
    friends = seed(store, "classified friends", tier="friends",
                   tier_status="classified")
    with store.transaction() as conn:
        by = {ledger: pending.pending_seqs(conn, ledger)
              for ledger in pending.TIERS}
    assert by == {"public": [], "friends": [friends], "family": [friends],
                  "private": [prov, friends]}


async def test_a_mark_reopens_a_filed_event(tmp_path):
    """A terminal disposition committed before the mark no longer counts;
    one committed after it does."""
    store = new_store(tmp_path)
    e = seed(store, "the alarm code")
    await run_new(new_writer(store, ScriptedPort(
        lambda p: reply({e: "no_change"}))))
    assert pending_seqs(store) == []
    corrections.mark(store, e)
    assert pending_seqs(store) == [e]
    w = new_writer(store, ScriptedPort(lambda p: reply({e: "no_change"})))
    assert await w.step() is True
    assert pending_seqs(store) == []


async def test_counters_are_derived_from_the_rows(tmp_path):
    """``commit_no`` and ``mark_no`` live only in the rows that carry them:
    a store built from a copy of those rows continues both, and a fresh
    store starts both at 1."""
    store = new_store(tmp_path / "a")
    e = seed(store, "first")
    await run_new(new_writer(store, ScriptedPort(
        lambda p: reply({e: "no_change"}))))
    corrections.mark(store, e)
    with store.transaction() as conn:
        assert (pending.next_commit_no(conn), pending.next_mark_no(conn)) == (
            2, 2)
    copy = tmp_path / "b" / "memory"
    copy.parent.mkdir()
    shutil.copytree(store.directory, copy)
    restored = MemoryStore(str(copy))
    assert restored.open()
    with restored.transaction() as conn:
        assert (pending.next_commit_no(conn), pending.next_mark_no(conn)) == (
            2, 2)
        assert pending.pending_seqs(conn, "private") == [e]
    fresh = new_store(tmp_path / "c")
    with fresh.transaction() as conn:
        assert (pending.next_commit_no(conn), pending.next_mark_no(conn)) == (
            1, 1)


# --- no second "processed" state ---------------------------------------------

_FORBIDDEN = re.compile(r"(^|_)(processed|watermark|done)($|_)")


def _defined_names(tree: ast.AST) -> set[str]:
    """Names the code defines: assignment targets, attributes assigned,
    functions, classes, arguments and dict-literal keys."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx,
                                                            ast.Store):
            names.add(node.attr)
        elif isinstance(node, ast.Dict):
            names.update(k.value for k in node.keys
                         if isinstance(k, ast.Constant)
                         and isinstance(k.value, str))
    return names


def _forbidden_state(code_root, store_path: str) -> list[str]:
    """Every name in the memory package's code, and every column, table or
    view of a fresh store, that records an event as handled other than by
    the pending view."""
    found = []
    for path in sorted((code_root / "memory").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found += [f"{path.name}: {n}" for n in sorted(_defined_names(tree))
                  if _FORBIDDEN.search(n.lower())]
    conn = sqlite3.connect(store_path)
    try:
        objects = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")]
        for name in objects:
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
            found += [f"{name}.{c}" for c in [name, *cols]
                      if _FORBIDDEN.search(c.lower())]
    finally:
        conn.close()
    return found


def test_no_second_processed_state(tmp_path):
    """No name in ``memory/`` and no table, view or column of the store
    records a ``processed``, ``watermark`` or ``done`` state."""
    store = new_store(tmp_path)
    assert _forbidden_state(CODE_ROOT, store.path) == []


def test_no_second_processed_state_red_case(tmp_path):
    """The scan reports a column, and a name, that would hold one."""
    store = new_store(tmp_path)
    conn = sqlite3.connect(store.path)
    conn.execute("ALTER TABLE events ADD COLUMN processed INTEGER")
    conn.commit()
    conn.close()
    fake = tmp_path / "code"
    (fake / "memory").mkdir(parents=True)
    (fake / "memory" / "x.py").write_text("last_watermark = 3\n")
    assert _forbidden_state(fake, store.path) == [
        "x.py: last_watermark", "events.processed"]
