"""The memory export: a verified copy of the store, as files a person can read.

One export runs at a time (:func:`export`). In order:

1. **Drain the spool** into the store through capture's own drain, for every
   registry row whose export rule is ``drain``. A drain that fails fails the
   export, and nothing is published: no export carries a spool.
2. **One read transaction** over the registry: each row whose export rule is
   ``yes`` writes one ``<table>.jsonl`` per table, one JSON object per row in
   ``rowid`` order. ``ledger.md`` (the ``private`` ledger, as text) and
   ``FORMAT.md`` are written beside them.
3. **``manifest.json`` last**: the format and schema versions, the row count
   of every exported table, events by tier and current objects by ledger,
   the **obligations** (every event pending in each ledger, the pending
   corrections, the failed jobs, each as a count and a SHA-256 of the sorted
   ids) and a SHA-256 of every other file.
4. **Re-read and verify** every file against the manifest.
5. **Publish** by renaming the hidden ``.<name>.part`` directory to
   ``<name>``.

Exports land in ``/data/memory/exports/``: ``export-<time>`` from the admin
route, ``nightly-<time>`` from the nightly job (the last
:data:`NIGHTLY_KEEP` are kept), and ``premigration-v<n>-<time>`` before a
schema migration. That one is taken of the file as it is: it copies the
exported tables the file's schema has (a migration may be about to add one)
and records that schema's version, and it does not drain the spool, since the
spool holds this code's rows, which an older schema may not accept; the spool
is drained after the migration, as at every boot. Every other export is
strict: a registered exported table the store lacks fails it.

The restore (:mod:`memory.restore`) loads exactly these files, and checks what
it loaded against the manifest with the functions here (:func:`dump_table`,
:func:`counts`, :func:`obligations`), so an export and a restore agree by
construction.

Blocking; async callers use ``asyncio.to_thread``. Imports nothing from the
running Casa process.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterator

from memory import capture, pending, registry, view
from memory.registry import Export, Kind
from memory.store import MemoryStore, schema_version, tables_at

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
MANIFEST = "manifest.json"
LEDGER = "ledger.md"
FORMAT = "FORMAT.md"
#: Nightly exports kept in the exports directory.
NIGHTLY_KEEP = 7
#: When the nightly export runs (the operator's zone, Casa's scheduler),
#: before the writer's nightly pass.
HOUR = 3
MINUTE = 0

#: Export kinds, and so the prefix of each published name.
ADMIN = "export"
NIGHTLY = "nightly"
PREMIGRATION = "premigration"

_one_at_a_time = threading.Lock()


class ExportBusy(RuntimeError):
    """Another export is running."""


class ExportFailed(RuntimeError):
    """The export could not be made; nothing was published."""


def exports_dir(store: MemoryStore) -> str:
    return os.path.join(store.directory, os.path.basename(registry.EXPORTS_DIR))


def exported_tables() -> list[str]:
    """Every table a registry row exports, in registry order."""
    return [t for e in registry.REGISTRY
            if e.kind is Kind.TABLE and e.export is Export.YES
            for t in e.paths]


def exported_tables_at(version: int) -> list[str]:
    """The exported tables schema *version* has (from its migrations): an
    export of a store at that version holds exactly these."""
    have = tables_at(version)
    return [t for t in exported_tables() if t in have]


# --- what an export holds, shared with the restore's check -------------------

def _line(cols: list[str], row: tuple) -> bytes:
    return (json.dumps(dict(zip(cols, row)), ensure_ascii=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def dump_table(conn: sqlite3.Connection, table: str) -> Iterator[bytes]:
    """*table*'s rows as export lines, in ``rowid`` order."""
    cur = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
    cols = [d[0] for d in cur.description]
    for row in cur:
        yield _line(cols, row)


def _digest(ids: list) -> dict:
    data = json.dumps(ids, separators=(",", ":")).encode()
    return {"count": len(ids), "sha256": hashlib.sha256(data).hexdigest()}


def counts(conn: sqlite3.Connection, tables: list[str]) -> dict:
    """Rows per table, events by tier, current objects by ledger."""
    return {
        "tables": {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                   for t in tables},
        "events_by_tier": {tier: n for tier, n in conn.execute(
            "SELECT tier, count(*) FROM events GROUP BY tier ORDER BY tier")},
        "objects_by_ledger": {ledger: n for ledger, n in conn.execute(
            "SELECT ledger, count(*) FROM objects WHERE status = 'current' "
            "GROUP BY ledger ORDER BY ledger")},
    }


def obligations(conn: sqlite3.Connection) -> dict:
    """What the store still owes, read from the one pending predicate: per
    ledger the pending events and the pending corrections, and the failed
    jobs."""
    return {
        "pending": {ledger: _digest(pending.pending_seqs(conn, ledger))
                    for ledger in pending.TIERS},
        "pending_corrections": {
            ledger: _digest(sorted(pending.pending_corrections(conn, ledger)))
            for ledger in pending.TIERS},
        "failed_jobs": _digest([r[0] for r in conn.execute(
            "SELECT job_id FROM jobs WHERE state = 'failed' ORDER BY job_id")]),
    }


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_manifest(directory: str) -> dict:
    """The manifest of the export in *directory*; raises ``ValueError`` for
    one that is missing or is not a manifest."""
    try:
        with open(os.path.join(directory, MANIFEST), "rb") as fh:
            data = json.loads(fh.read().decode("utf-8"))
    except OSError as exc:
        raise ValueError(f"no readable {MANIFEST} ({exc.strerror})") from None
    except ValueError:
        raise ValueError(f"{MANIFEST} is not JSON") from None
    if (not isinstance(data, dict) or data.get("format") != FORMAT_VERSION
            or not isinstance(data.get("schema_version"), int)
            or not isinstance(data.get("files"), dict)
            or not isinstance(data.get("counts"), dict)
            or not isinstance(data["counts"].get("tables"), dict)
            or not isinstance(data.get("obligations"), dict)):
        raise ValueError(f"{MANIFEST} is not an export manifest of format "
                         f"{FORMAT_VERSION}")
    return data


def verify_files(directory: str, manifest: dict) -> None:
    """Every file the manifest names is a regular file whose SHA-256
    matches, and every table it counts has its file. Raises ``ValueError``
    naming the first that does not."""
    for name, digest in manifest["files"].items():
        if (not isinstance(name, str) or "/" in name or name.startswith(".")
                or not name.endswith((".jsonl", ".md"))):
            raise ValueError(f"the manifest names an odd file {name!r}")
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or os.path.islink(path):
            raise ValueError(f"{name} is missing")
        if sha256_file(path) != digest:
            raise ValueError(f"{name} does not match its hash")
    for table in manifest["counts"]["tables"]:
        if f"{table}.jsonl" not in manifest["files"]:
            raise ValueError(f"the manifest counts {table} but has no file "
                             "for it")


# --- the export ----------------------------------------------------------------

FORMAT_TEXT = """# Casa memory export

This directory is one export of Casa's memory store, taken in one read
transaction. `manifest.json` describes it and is written last.

- `<table>.jsonl`: every row of one table, one JSON object per line, in the
  order the store keeps them. Keys are the table's columns.
  - `events`: everything captured, verbatim, with when, who and which tier.
  - `correction_marks`: events the operator marked as corrected.
  - `objects` and `revisions`: each ledger's entries, current and every
    earlier state, with the commit that made it.
  - `dispositions` and `jobs`: what the memory writer did with each event,
    and its jobs.
  - `engagement_summaries`: summaries of finished engagements.
- `ledger.md`: the private ledger as text, for reading.
- `manifest.json`: the format and schema versions, the row count of every
  table, events by tier and current entries by ledger, what the store still
  owed (pending events and corrections per ledger, failed jobs; each as a
  count and a SHA-256 of the sorted ids), and a SHA-256 of every other file.

Restoring an export (`casactl memory-restore <directory>`) replaces all of
Casa's memory with exactly these rows; the restore refuses an export whose
files do not match the manifest.
"""


def _stamp(now: float | None) -> str:
    t = time.time() if now is None else now
    return (time.strftime("%Y%m%dT%H%M%S", time.gmtime(t))
            + f"{int((t % 1) * 1e6):06d}Z")


def _iso(now: float | None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() if now is None else now))


def _ledger_text(conn: sqlite3.Connection) -> str:
    objs = view.objects_as_of(conn, "private", pending.current_commit_no(conn))
    rendered = view.render_text(objs, view.compose(objs, 1 << 40))
    body = ""
    if rendered:
        # Drop the <memory> wrapper and the model-facing head line.
        body = rendered.split("\n\n", 1)[1].rsplit("\n</memory>", 1)[0]
    return "# Private ledger\n\n" + (body or "(empty)") + "\n"


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(directory: str, name: str, chunks) -> str:
    path = os.path.join(directory, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                 0o600)
    with os.fdopen(fd, "wb") as fh:
        for chunk in chunks:
            fh.write(chunk)
        fh.flush()
        os.fsync(fh.fileno())
    return sha256_file(path)


def export(store: MemoryStore, *, kind: str = ADMIN, drain: bool = True,
           as_found: bool = False, now: float | None = None) -> str:
    """Make one export of *store* and publish it; returns its directory.
    *as_found* (the pre-migration export) copies only the registered
    exported tables the file's schema has; otherwise each must exist.
    Raises :class:`ExportBusy` while another runs and :class:`ExportFailed`
    when it cannot be made (nothing is then published)."""
    if not _one_at_a_time.acquire(blocking=False):
        raise ExportBusy("an export is already running")
    try:
        return _export(store, kind=kind, drain=drain, as_found=as_found,
                       now=now)
    finally:
        _one_at_a_time.release()


def _tables_in(conn: sqlite3.Connection, version: int, *,
               as_found: bool) -> list[str]:
    """The exported tables this export copies: exactly those schema
    *version* has, each of which the file must hold. Outside a migration
    (*as_found* false) that must be every registered exported table."""
    expected = exported_tables_at(version)
    if not as_found:
        lacking = [t for t in exported_tables() if t not in expected]
        if lacking or version != schema_version():
            raise ExportFailed(
                f"the store is at schema v{version} and has no "
                f"{', '.join(lacking) or 'current'} table")
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = [t for t in expected if t not in have]
    if missing:
        raise ExportFailed(f"the store has no {', '.join(missing)} table")
    return expected


def _export(store: MemoryStore, *, kind: str, drain: bool, as_found: bool,
            now: float | None) -> str:
    rules = {e.export for e in registry.REGISTRY}
    unknown = rules - set(Export)
    if unknown:
        raise ExportFailed(f"no export rule {sorted(unknown)!r}")
    if drain and Export.DRAIN in rules:
        try:
            capture.drain_spool(store)
        except Exception as exc:  # noqa: BLE001 — reported, nothing published
            raise ExportFailed(
                f"the capture spool could not be stored first ({exc})"
            ) from exc
    name = f"{kind}-{_stamp(now)}"
    parent = exports_dir(store)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    part = os.path.join(parent, f".{name}.part")
    shutil.rmtree(part, ignore_errors=True)
    os.makedirs(part, mode=0o700)
    try:
        files: dict[str, str] = {}
        with store.transaction() as conn:   # one read snapshot
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = _tables_in(conn, version, as_found=as_found)
            for table in tables:
                files[f"{table}.jsonl"] = _write(
                    part, f"{table}.jsonl", dump_table(conn, table))
            stored_counts = counts(conn, tables)
            owed = obligations(conn)
            files[LEDGER] = _write(
                part, LEDGER, [_ledger_text(conn).encode("utf-8")])
        files[FORMAT] = _write(part, FORMAT, [FORMAT_TEXT.encode("utf-8")])
        manifest = {
            "format": FORMAT_VERSION, "schema_version": version,
            "kind": kind, "created_at": _iso(now), "counts": stored_counts,
            "obligations": owed, "files": files,
        }
        _write(part, MANIFEST, [(json.dumps(manifest, indent=2, sort_keys=True)
                                 + "\n").encode("utf-8")])
        _fsync_dir(part)
        # Re-read what is on disk, not what was meant to be written.
        verify_files(part, read_manifest(part))
        for table in tables:
            with open(os.path.join(part, f"{table}.jsonl"), "rb") as fh:
                lines = sum(1 for _ in fh)
            if lines != stored_counts["tables"][table]:
                raise ValueError(f"{table}.jsonl holds {lines} rows, not "
                                 f"{stored_counts['tables'][table]}")
        final = os.path.join(parent, name)
        os.rename(part, final)
        _fsync_dir(parent)
    except BaseException as exc:
        shutil.rmtree(part, ignore_errors=True)
        if isinstance(exc, Exception) and not isinstance(exc, ExportFailed):
            raise ExportFailed(str(exc) or type(exc).__name__) from exc
        raise
    logger.info("memory export published: %s (%s)", final,
                stored_counts["tables"])
    if kind == NIGHTLY:
        prune(parent)
    return final


def prune(parent: str, keep: int = NIGHTLY_KEEP) -> list[str]:
    """Remove all but the newest *keep* nightly exports, and any hidden
    ``.part`` directory an export cut off left behind. It runs inside the
    export that just published, and exports run one at a time, so no
    ``.part`` directory is being written."""
    try:
        names = sorted(os.listdir(parent))
    except FileNotFoundError:
        return []
    nightly = [n for n in names if n.startswith(NIGHTLY + "-")]
    gone = nightly[:-keep] if keep else nightly
    gone += [n for n in names if n.startswith(".") and n.endswith(".part")]
    for n in gone:
        shutil.rmtree(os.path.join(parent, n), ignore_errors=True)
    return gone


def before_migration(store: MemoryStore, version: int) -> str:
    """The automatic export taken before a schema migration, of the file as
    it is; raising stops the migration (memory is then unavailable)."""
    path = export(store, kind=f"{PREMIGRATION}-v{version}", drain=False,
                  as_found=True)
    logger.info("memory export before migrating from v%d: %s", version, path)
    return path


async def run_nightly(store: MemoryStore) -> str | None:
    """The scheduler's nightly export. Never raises: tomorrow night tries
    again, and a spool that cannot be stored already earns its own notice."""
    if not store.available:
        return None
    try:
        return await asyncio.to_thread(export, store, kind=NIGHTLY)
    except ExportBusy:
        logger.info("memory nightly export skipped: another export is running")
    except Exception:  # noqa: BLE001 — logged; nothing was published
        logger.warning("memory nightly export failed", exc_info=True)
    return None
