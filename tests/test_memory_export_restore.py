"""§14 test 10 (INV-MEM-037): export and restore on a real tree.

An export drains the spool, then copies every exported table in one read
transaction, with a manifest written last. A restore is validated and staged
at the request and carried out only by the container-start step: built
beside the current store and checked against the manifest exactly, then the
whole wipe, then the install. Anything refused at the request schedules
nothing; a staged export that fails its checks is abandoned with memory
unchanged and reported once."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

import memory_restore_support as r
import memory_wipe_support as w
from memory import capture, export, maintenance, registry, restore
from memory.registry import Entry, Export, Kind, Restore, Wipe
from memory.store import MemoryStore

pytestmark = [pytest.mark.unit]


def _setup(tmp_path: Path):
    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    store = r.store_at(root)
    facts = r.populate(store)
    return root, proc, store, facts


def _events(db: str) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [t for (t,) in conn.execute("SELECT text FROM events "
                                           "ORDER BY seq")]
    finally:
        conn.close()


def _saved(path: str, tmp_path: Path) -> str:
    """The export moved out of the root, where the wipe would delete it."""
    return shutil.move(path, str(tmp_path / os.path.basename(path)))


# --- the round trip ----------------------------------------------------------------

def test_a_round_trip_is_equal_and_runs_only_in_the_step(tmp_path):
    root, proc, store, _ = _setup(tmp_path)
    path = _saved(export.export(store), tmp_path)
    manifest = export.read_manifest(path)
    assert manifest["counts"]["tables"]["events"] == 4
    assert manifest["counts"]["events_by_tier"] == {"family": 1, "private": 3}
    assert manifest["obligations"]["failed_jobs"]["count"] == 1
    assert manifest["obligations"]["pending_corrections"]["private"][
        "count"] == 1
    assert sorted(os.listdir(path)) == sorted(
        [*manifest["files"], export.MANIFEST])
    r.put(store, "said after the export")

    # Staged: nothing changes until the container-start step runs.
    assert r.stage(root, path) == "staged"
    before = _events(store.path)
    assert "said after the export" in before
    intent = maintenance.read_intent(str(root))
    assert (intent["kind"], intent["phase"], intent["attempts"]) == (
        "restore", "staged", [])

    p = r.run_step(root, proc)
    assert p.stdout.strip() == "done", p.stderr
    assert r.nothing_scheduled(root)
    r.assert_store_equals_export(store.path, path)
    assert "said after the export" not in _events(store.path)
    # The installed store is the built one: one file, then WAL once opened.
    after = r.store_at(root)
    with after.transaction() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert [s for (s,) in conn.execute(
            "SELECT state FROM jobs ORDER BY job_id")] == [
            "committed", "queued", "failed", "queued"]
    lines = [maintenance.claim_outcome_notice(after) for _ in range(2)]
    assert lines == [maintenance.restored_line(manifest["created_at"]), None]


def test_an_export_holds_the_readable_ledger_and_its_format(tmp_path):
    _, _, store, _ = _setup(tmp_path)
    path = Path(export.export(store))
    assert "the roses are pruned in March" in (path / "ledger.md").read_text()
    assert "manifest.json" in (path / "FORMAT.md").read_text()
    assert not [n for n in os.listdir(path.parent) if n.endswith(".part")]


def test_one_export_at_a_time(tmp_path):
    _, _, store, _ = _setup(tmp_path)
    assert export._one_at_a_time.acquire()
    try:
        with pytest.raises(export.ExportBusy):
            export.export(store)
    finally:
        export._one_at_a_time.release()
    assert not Path(export.exports_dir(store)).exists()


# --- refused at the request, nothing scheduled -----------------------------------------

def _copy_export(path: str, tmp_path: Path, name: str) -> Path:
    dst = tmp_path / name
    shutil.copytree(path, dst)
    return dst


def _rewrite_manifest(directory: Path, change) -> None:
    m = json.loads((directory / "manifest.json").read_text())
    change(m)
    (directory / "manifest.json").write_text(json.dumps(m))


def test_refused_at_the_request_with_nothing_scheduled(tmp_path):
    root, _, store, _ = _setup(tmp_path)
    good = export.export(store)

    corrupt = _copy_export(good, tmp_path, "corrupt")
    with open(corrupt / "events.jsonl", "ab") as fh:
        fh.write(b"{}\n")
    unregistered = _copy_export(good, tmp_path, "unregistered")
    (unregistered / "stray.jsonl").write_text("")
    _rewrite_manifest(unregistered, lambda m: (
        m["counts"]["tables"].__setitem__("stray", 0),
        m["files"].__setitem__("stray.jsonl", r.sha(unregistered / "stray.jsonl"))))
    newer = _copy_export(good, tmp_path, "newer")
    _rewrite_manifest(newer, lambda m: m.__setitem__("schema_version", 99))
    missing = _copy_export(good, tmp_path, "missing")
    (missing / "jobs.jsonl").unlink()

    cases = {corrupt: "events.jsonl does not match its hash",
             unregistered: "stray, which this Casa has no restore rule for",
             newer: "schema v99 is not one this Casa can migrate",
             missing: "jobs.jsonl is missing",
             tmp_path / "nowhere": "is not an export directory"}
    for directory, reason in cases.items():
        with pytest.raises(restore.Refused, match=reason):
            r.stage(root, str(directory))
        assert r.nothing_scheduled(root), directory

    # A pending wipe refuses it too, before the export is even read, and is
    # kept.
    assert maintenance.write_wipe_intent(str(root)) == "written"
    for directory in (good, str(tmp_path / "nowhere")):
        with pytest.raises(restore.Refused, match="memory wipe is already"):
            r.stage(root, directory)
    assert maintenance.read_intent(str(root))["kind"] == "wipe"
    assert not w.under(root, r.RESTORE_SOURCE).exists()


def test_a_wipe_consented_while_a_restore_is_staged_is_kept(tmp_path,
                                                          monkeypatch):
    """The intent is written under the doors' lock with a fresh check, so a
    wipe consented while the export is being copied is never overwritten."""
    root, _, store, _ = _setup(tmp_path)
    good = export.export(store)
    real = restore.validate
    calls = []

    def validate_then_a_wipe(directory):
        out = real(directory)
        calls.append(directory)
        if len(calls) == 2:          # the staged copy's check
            assert maintenance.write_wipe_intent(str(root)) == "written"
        return out

    monkeypatch.setattr(restore, "validate", validate_then_a_wipe)
    with pytest.raises(restore.Refused, match="memory wipe is already"):
        r.stage(root, good)
    assert maintenance.read_intent(str(root))["kind"] == "wipe"
    assert not w.under(root, r.RESTORE_SOURCE).exists()


def test_a_pending_restore_is_neither_replaced_by_a_wipe_nor_a_restore(
        tmp_path):
    root, _, store, _ = _setup(tmp_path)
    path = export.export(store)
    assert r.stage(root, path) == "staged"
    intent = maintenance.read_intent(str(root))
    assert maintenance.write_wipe_intent(str(root)) == "restore_pending"
    with pytest.raises(restore.Refused, match="memory restore is already"):
        r.stage(root, path)
    assert maintenance.read_intent(str(root)) == intent
    assert sorted(os.listdir(w.under(root, r.RESTORE_SOURCE))) == sorted(
        [*export.read_manifest(path)["files"], export.MANIFEST])


def test_without_consent_the_export_is_checked_and_nothing_scheduled(tmp_path):
    root, _, store, _ = _setup(tmp_path)
    path = export.export(store)
    assert restore.stage(path, root=str(root), confirm=False) == (
        "needs_confirm")
    assert r.nothing_scheduled(root)


# --- abandoned in the step, memory unchanged ---------------------------------------

@pytest.mark.parametrize("spoil", ["revalidation", "count_check", "load",
                                   "dump", "obligations"])
def test_a_staged_export_that_fails_its_checks_is_abandoned(tmp_path, spoil):
    root, proc, store, _ = _setup(tmp_path)
    path = Path(export.export(store))
    if spoil == "dump":
        # Every file matches its hash, but a row loads as other bytes than
        # it was exported as (an integer written as 1.0).
        lines = (path / "jobs.jsonl").read_bytes().splitlines(True)
        row = json.loads(lines[0])
        row["attempts"] = 1.0
        lines[0] = (json.dumps(row) + "\n").encode()
        (path / "jobs.jsonl").write_bytes(b"".join(lines))
        _rewrite_manifest(path, lambda m: m["files"].__setitem__(
            "jobs.jsonl", r.sha(path / "jobs.jsonl")))
    elif spoil == "obligations":
        _rewrite_manifest(path, lambda m: m["obligations"][
            "failed_jobs"].__setitem__("count", 2))
    elif spoil == "count_check":
        # Every file matches its hash, but the counts claim one more event.
        _rewrite_manifest(path, lambda m: m["counts"]["tables"].__setitem__(
            "events", 5))
    elif spoil == "load":
        # A row the store refuses (a second event with the same key).
        lines = (path / "events.jsonl").read_bytes().splitlines(True)
        dup = json.loads(lines[0])
        dup["seq"] = 99
        (path / "events.jsonl").write_bytes(
            b"".join(lines) + (json.dumps(dup) + "\n").encode())
        _rewrite_manifest(path, lambda m: m["files"].__setitem__(
            "events.jsonl", r.sha(path / "events.jsonl")))
    assert r.stage(root, str(path)) == "staged"
    if spoil == "revalidation":
        # A file the build never loads: only its hash can tell.
        with open(w.under(root, r.RESTORE_SOURCE) / "ledger.md", "ab") as f:
            f.write(b"\n")
    before = {t: r.table_lines(store.path, t)
              for t in export.exported_tables()}

    p = r.run_step(root, proc)
    assert p.stdout.strip() == "abandoned", p.stderr
    assert r.nothing_scheduled(root)
    assert {t: r.table_lines(store.path, t)
            for t in export.exported_tables()} == before
    s = r.store_at(root)
    [line, again] = [maintenance.claim_outcome_notice(s) for _ in range(2)]
    assert line.startswith("The restore didn't happen (") and again is None
    assert line.endswith("memory is unchanged.")


# --- no copy the export does not carry survives ----------------------------------------

async def test_no_non_exported_copy_survives(tmp_path, monkeypatch):
    from test_memory_wipe import build
    tree = w.Tree(tmp_path / "root")
    root, proc = tree.root, w.fake_proc(tmp_path)
    store = r.store_at(root)
    r.populate(store, word="tulips")
    exported = Path(shutil.move(export.export(store),
                                str(tmp_path / "exported")))
    await build(tree, monkeypatch)   # a canary in every registry row
    assert len(w.hits(root, tree.canaries)) >= 17

    assert r.stage(root, str(exported)) == "staged"
    p = r.run_step(root, proc)
    assert p.stdout.strip() == "done", p.stderr
    assert w.hits(root, tree.canaries) == []
    assert w.kept_changes(tree) == []
    r.assert_store_equals_export(store.path, str(exported))
    assert json.loads(w.under(root, "/data/sessions.json").read_text()) == {}


def test_a_source_in_exports_succeeds_from_the_staged_copy(tmp_path):
    root, proc, store, _ = _setup(tmp_path)
    path = export.export(store)
    kept = shutil.copytree(path, tmp_path / "kept")
    assert r.stage(root, os.path.basename(path)) == "staged"  # exports/<name>
    p = r.run_step(root, proc)
    assert p.stdout.strip() == "done", p.stderr
    assert not w.under(root, registry.EXPORTS_DIR).exists()   # wiped
    r.assert_store_equals_export(store.path, str(kept))


# --- the spool --------------------------------------------------------------------------

def test_a_spool_only_event_round_trips(tmp_path):
    root, proc, store, _ = _setup(tmp_path)
    store._unavailable_reason = "a simulated outage"
    receipt = capture.append(store, r.event("only in the spool"))
    store._unavailable_reason = None
    assert receipt.outcome == capture.SPOOLED

    path = _saved(export.export(store), tmp_path)
    assert not os.path.lexists(capture.spool_path(store.directory))
    assert export.read_manifest(path)["counts"]["tables"]["events"] == 5
    assert not [n for n in os.listdir(path) if "spool" in n]
    assert r.stage(root, path) == "staged"
    assert r.run_step(root, proc).stdout.strip() == "done"
    assert "only in the spool" in _events(store.path)
    r.assert_store_equals_export(store.path, path)


def test_an_export_whose_drain_fails_publishes_nothing(tmp_path, monkeypatch):
    root, _, store, _ = _setup(tmp_path)
    store._unavailable_reason = "a simulated outage"
    try:
        assert capture.append(store, r.event("spooled")).outcome == (
            capture.SPOOLED)
        with pytest.raises(export.ExportFailed, match="spool could not"):
            export.export(store)
    finally:
        store._unavailable_reason = None
    exports = Path(export.exports_dir(store))
    assert not exports.exists() or os.listdir(exports) == []
    # Kept on disk (as the snapshot the failed drain took) for a later drain.
    assert os.path.lexists(capture.snapshot_path(store.directory))


# --- a wipe, then the restore -----------------------------------------------------------

def test_an_export_taken_before_a_wipe_brings_its_memory_back(tmp_path):
    root, proc, store, _ = _setup(tmp_path)
    path = Path(shutil.move(export.export(store), str(tmp_path / "saved")))
    assert maintenance.write_wipe_intent(str(root)) == "written"
    assert r.run_step(root, proc).stdout.strip() == "done"
    assert _events(store.path) == []
    assert r.stage(root, str(path)) == "staged"
    assert r.run_step(root, proc).stdout.strip() == "done"
    r.assert_store_equals_export(store.path, str(path))


# --- registry-driven: a new table row needs no code here ---------------------------------

def test_a_new_unexported_table_row_needs_no_restore_code(tmp_path,
                                                         monkeypatch):
    """A row like S8's run records (export no, restore none, wipe delete) is
    left out of the export and comes back empty, with no change here."""
    from memory import schema, store as store_mod
    row = Entry("run records", Kind.TABLE, ("run_records",), Export.NO,
                Restore.NONE, Wipe.DELETE)
    monkeypatch.setattr(registry, "REGISTRY", (*registry.REGISTRY, row))
    monkeypatch.setattr(store_mod, "MIGRATIONS", {1: (
        *schema.V1, "CREATE TABLE run_records (run_id TEXT PRIMARY KEY) "
                    "STRICT")})
    root, proc = tmp_path / "root", w.fake_proc(tmp_path)
    store = r.store_at(root)
    r.populate(store)
    with store.transaction(write=True) as conn:
        conn.execute("INSERT INTO run_records VALUES ('run-1')")
    path = _saved(export.export(store), tmp_path)
    assert "run_records.jsonl" not in export.read_manifest(path)["files"]
    assert r.stage(root, path) == "staged"
    # In this process, so the patched registry and schema hold.
    assert maintenance.run(root=str(root), proc_root=str(proc)) == "done"
    with r.store_at(root).transaction() as conn:
        assert conn.execute("SELECT count(*) FROM run_records").fetchone() == (0,)
    r.assert_store_equals_export(store.path, path)


def test_a_restore_migrates_what_it_loads(tmp_path, monkeypatch):
    from memory import schema, store as store_mod
    root, proc, store, _ = _setup(tmp_path)
    path = export.export(store)                      # at v1
    monkeypatch.setattr(store_mod, "MIGRATIONS", {
        1: schema.V1, 2: ("ALTER TABLE events ADD COLUMN note TEXT",)})
    monkeypatch.setattr(restore, "MIGRATIONS", store_mod.MIGRATIONS)
    assert r.stage(root, path) == "staged"
    assert maintenance.run(root=str(root), proc_root=str(proc)) == "done"
    conn = sqlite3.connect(store.path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM events WHERE note IS NULL"
                            ).fetchone() == (4,)
    finally:
        conn.close()


def test_a_restore_rule_the_step_does_not_know_stops_it(tmp_path,
                                                         monkeypatch):
    root, proc, store, _ = _setup(tmp_path)
    path = export.export(store)
    assert r.stage(root, path) == "staged"
    odd = Entry("odd", Kind.SERVED_COPY, ("/data/odd",), Export.NO,
                Restore.REBUILD, Wipe.DELETE)
    monkeypatch.setattr(registry, "REGISTRY", (*registry.REGISTRY, odd))
    assert maintenance.run(root=str(root), proc_root=str(proc)) == "failed"
    intent = maintenance.read_intent(str(root))
    assert "no restore rule for 'odd'" in intent["attempts"][-1]["error"]


def test_the_export_refuses_an_unavailable_store(tmp_path):
    store = MemoryStore(str(tmp_path / "memory"))
    with pytest.raises(export.ExportFailed, match="not opened"):
        export.export(store)
    assert os.listdir(export.exports_dir(store)) == []


# --- the nightly export ------------------------------------------------------------------

async def test_the_nightly_export_keeps_the_last_seven(tmp_path):
    root, _, store, _ = _setup(tmp_path)
    admin = export.export(store)
    made = [await export.run_nightly(store) for _ in range(9)]
    assert all(made)
    names = sorted(os.listdir(export.exports_dir(store)))
    assert names == sorted([os.path.basename(admin),
                            *[os.path.basename(p) for p in made[-7:]]])
    # An unavailable store is skipped, and a failure never raises.
    store._unavailable_reason = "a simulated outage"
    assert await export.run_nightly(store) is None


def test_casa_core_schedules_the_nightly_export():
    import ast
    source = (w.CODE_ROOT / "casa_core.py").read_text(encoding="utf-8")
    jobs = [ast.unparse(n) for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") ==
            "add_job" and "run_nightly" in ast.unparse(n)]
    assert len(jobs) == 1 and "memory_export.HOUR" in jobs[0]


# --- a migration that adds an exported table (S7 round 1, Terra S2) ----------------------

def _v2_adds_an_exported_table(monkeypatch):
    """A simulated v2 that creates a new table registered as exported."""
    from memory import schema, store as store_mod
    row = Entry("future rows", Kind.TABLE, ("future_rows",), Export.YES,
                Restore.LOAD, Wipe.DELETE)
    monkeypatch.setattr(registry, "REGISTRY", (*registry.REGISTRY, row))
    monkeypatch.setattr(store_mod, "MIGRATIONS", {
        1: schema.V1,
        2: ("CREATE TABLE future_rows (id INTEGER PRIMARY KEY, note TEXT) "
            "STRICT",)})
    monkeypatch.setattr(restore, "MIGRATIONS", store_mod.MIGRATIONS)


def test_a_migration_that_adds_an_exported_table_is_not_blocked(
        tmp_path, monkeypatch):
    """The pre-migration export copies the exported tables the file has,
    and records its schema version; the store then migrates."""
    root, proc, store, _ = _setup(tmp_path)          # a populated v1 store
    _v2_adds_an_exported_table(monkeypatch)
    later = MemoryStore(store.directory)
    assert later.open(before_migration=export.before_migration) is True, (
        later.unavailable_reason)
    with later.transaction() as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (2,)
    [name] = os.listdir(export.exports_dir(later))
    path = os.path.join(export.exports_dir(later), name)
    manifest = export.read_manifest(path)
    assert manifest["schema_version"] == 1
    assert "future_rows" not in manifest["counts"]["tables"]
    assert manifest["counts"]["tables"]["events"] == 4

    # Admin and nightly exports stay strict: every exported table is there.
    with later.transaction(write=True) as conn:
        conn.execute("INSERT INTO future_rows (note) VALUES ('kept')")
    current = export.read_manifest(export.export(later))
    assert current["schema_version"] == 2
    assert current["counts"]["tables"]["future_rows"] == 1

    # And the v1 export restores: built at v1, loaded, migrated to v2.
    saved = _saved(path, tmp_path)
    assert r.stage(root, saved) == "staged"
    assert maintenance.run(root=str(root), proc_root=str(proc)) == "done"
    with r.store_at(root).transaction() as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (2,)
        assert conn.execute("SELECT count(*) FROM future_rows").fetchone() == (0,)
    r.assert_store_equals_export(store.path, saved)


def test_an_admin_export_missing_an_exported_table_fails(tmp_path, monkeypatch):
    """Strict outside a migration: a current store without a registered
    exported table is not exported as if it were complete."""
    _, _, store, _ = _setup(tmp_path)
    row = Entry("future rows", Kind.TABLE, ("future_rows",), Export.YES,
                Restore.LOAD, Wipe.DELETE)
    monkeypatch.setattr(registry, "REGISTRY", (*registry.REGISTRY, row))
    with pytest.raises(export.ExportFailed, match="future_rows"):
        export.export(store)
    assert os.listdir(export.exports_dir(store)) == []


# --- an older export must hold exactly its schema's exported tables (S7 r2) ------------

def _drop_table(directory: Path, table: str) -> None:
    (directory / f"{table}.jsonl").unlink()

    def change(m):
        del m["counts"]["tables"][table]
        del m["files"][f"{table}.jsonl"]
    _rewrite_manifest(directory, change)


def test_an_older_export_missing_one_of_its_schemas_tables_is_refused(
        tmp_path, monkeypatch):
    """A v1 export whose manifest and files both omit a table v1 has is
    refused at the request, and at the build if staged anyway."""
    root, proc, store, _ = _setup(tmp_path)
    path = Path(_saved(export.export(store), tmp_path))     # complete, v1
    _drop_table(path, "engagement_summaries")
    _v2_adds_an_exported_table(monkeypatch)                 # now it is older
    with pytest.raises(restore.Refused, match="engagement_summaries"):
        r.stage(root, str(path))
    assert r.nothing_scheduled(root)

    # Staged behind the request's back: the build abandons it.
    monkeypatch.setattr(restore, "validate", _lenient(restore.validate))
    assert r.stage(root, str(path)) == "staged"
    monkeypatch.undo()
    _v2_adds_an_exported_table(monkeypatch)
    assert maintenance.run(root=str(root), proc_root=str(proc)) == "abandoned"
    assert r.table_lines(store.path, "engagement_summaries") != b""


def _lenient(real):
    def validate(directory):
        try:
            return real(directory)
        except restore.Refused:
            manifest = export.read_manifest(directory)
            return manifest, r.sha(Path(directory) / export.MANIFEST)
    return validate


def test_a_complete_older_export_restores(tmp_path, monkeypatch):
    root, proc, store, _ = _setup(tmp_path)
    path = _saved(export.export(store), tmp_path)
    _v2_adds_an_exported_table(monkeypatch)
    assert export.exported_tables_at(1) == [
        t for t in export.exported_tables() if t != "future_rows"]
    assert r.stage(root, path) == "staged"
    assert maintenance.run(root=str(root), proc_root=str(proc)) == "done"
    r.assert_store_equals_export(store.path, path)


def test_the_tables_of_each_version_come_from_its_migrations(monkeypatch):
    from memory import schema, store as store_mod
    monkeypatch.setattr(store_mod, "MIGRATIONS", {
        1: schema.V1,
        2: ("CREATE TABLE future_rows (id INTEGER PRIMARY KEY) STRICT",
            "DROP TABLE engagement_summaries")})
    assert "future_rows" not in store_mod.tables_at(1)
    assert "engagement_summaries" in store_mod.tables_at(1)
    assert "future_rows" in store_mod.tables_at(2)
    assert "engagement_summaries" not in store_mod.tables_at(2)


def test_an_older_export_holding_a_later_table_or_a_stray_file_is_refused(
        tmp_path, monkeypatch):
    root, _, store, _ = _setup(tmp_path)
    good = Path(_saved(export.export(store), tmp_path))     # v1
    _v2_adds_an_exported_table(monkeypatch)

    later = _copy_export(str(good), tmp_path, "later")
    (later / "future_rows.jsonl").write_text("")
    _rewrite_manifest(later, lambda m: (
        m["counts"]["tables"].__setitem__("future_rows", 0),
        m["files"].__setitem__("future_rows.jsonl",
                               r.sha(later / "future_rows.jsonl"))))
    with pytest.raises(restore.Refused, match="future_rows, which schema v1 "
                                              "does not have"):
        r.stage(root, str(later))

    stray = _copy_export(str(good), tmp_path, "stray")
    (stray / "notes.jsonl").write_text("")
    _rewrite_manifest(stray, lambda m: m["files"].__setitem__(
        "notes.jsonl", r.sha(stray / "notes.jsonl")))
    with pytest.raises(restore.Refused, match="notes.jsonl, which is no "
                                              "table of schema v1"):
        r.stage(root, str(stray))
    assert r.nothing_scheduled(root)
