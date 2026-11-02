"""The memory restore: an export replaces all of memory, at container start.

A restore never merges. It runs the whole wipe, then installs a store built
from the export, so no copy of memory the export does not carry survives.

**The request** (:func:`request_restore`, in ``casa-main``). The export is
validated at once (:func:`validate`): its manifest is one this Casa reads,
every table it holds is registered with a restore rule, its schema is one this
Casa can migrate, and every file matches its hash. A pending wipe or restore
refuses it too. Any refusal schedules nothing. Without consent the reply is
the confirmation text; on consent the export is copied into
``/data/memory/restore-source/`` (it may sit in ``exports/``, which the wipe
deletes), validated again there, and the intent is written (kind
``restore``, phase ``staged``, the manifest's hash), then the app restart is
requested as for a wipe.

**Phase ``staged``** (:func:`build_phase`, in the container-start step). The
staged export is validated again and ``restore-source/built.db`` is built
from it with the registry's restore rules: the store is created at the
export's schema version in a rollback journal (never WAL), every exported
table is loaded, and what was loaded is compared with the manifest: row
counts, each table dumped again and hashed, and the obligations. Loaded must
equal the manifest exactly. Then every ``running`` job goes back to
``queued``, the store is migrated forward when the export is older, and
``meta`` is written fresh. ``built.db`` is closed, checked to be in the
rollback journal with no ``-wal``, ``-shm`` or ``-journal`` beside it,
fsynced and hashed, and one atomic write moves the intent to phase
``wiping`` with that hash. Current memory is not touched.

If anything fails, the restore is abandoned and nothing has changed: the
outcome is recorded in the current store's ``meta``, then the intent is
deleted, then ``restore-source/``. A kill between those two leaves a source
with no intent, which the next step deletes.

**Phase ``wiping``** (:func:`check_built`, then the wipe loop, then
:func:`install_phase`). ``built.db`` is checked against its hash; the whole
wipe runs over every row except the restore source and the intent; a copy of
``built.db`` is installed as ``memory.db`` (copy, fsync, hash check, rename);
the session pointers are reset. The step then deletes the intent, then
``restore-source/``. A kill anywhere in this phase re-runs it: the wipe
removes a partial or complete install, and ``built.db`` is still there. The
search index is rebuilt by ``casa-main``'s boot check, which rebuilds a
missing index from the installed store.

Imports nothing from the running Casa process.
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
from collections.abc import Awaitable, Callable

from memory import export as mexport
from memory import maintenance, registry
from memory.registry import Kind, Restore, under
from memory.schema import MIGRATIONS
from memory.store import MemoryStore, schema_version

logger = logging.getLogger(__name__)

BUILT = "built.db"
#: The copy of ``built.db`` that is renamed into place as ``memory.db``.
INSTALL_COPY = "install.db"

# --- what the operator reads ------------------------------------------------
CONFIRMATION = (
    "This replaces all current memory with this export's, including anything "
    "wiped since it was made. My saved conversations and memory backups go "
    "too, as in a wipe. Casa restarts to do it."
)
STAGED_LINE = "Restoring now. I'll restart and be back in a minute."
WIPE_PENDING = "a memory wipe is already scheduled for the next app restart"
RESTORE_PENDING = ("a memory restore is already scheduled for the next app "
                   "restart")


def refused_line(reason: str) -> str:
    return f"The restore was refused: {reason}. Nothing was scheduled."


class Refused(ValueError):
    """The export cannot be restored; the message is the operator's reason."""


class Mismatch(RuntimeError):
    """What was loaded differs from the export's manifest."""


def _sha256(path: str) -> str:
    return mexport.sha256_file(path)


def source_dir(root: str = "/") -> str:
    return under(root, registry.RESTORE_SOURCE)


def resolve(path: str, root: str = "/") -> str:
    """An export directory: *path* itself, or a bare name in the exports
    directory."""
    if "/" not in path:
        return os.path.join(under(root, registry.EXPORTS_DIR), path)
    return path


# --- validation ------------------------------------------------------------------

def _loadable() -> set[str]:
    return {t for e in registry.REGISTRY if e.kind is Kind.TABLE
            and e.restore in (Restore.LOAD, Restore.LOAD_REQUEUE)
            for t in e.paths}


def validate(directory: str) -> tuple[dict, str]:
    """The manifest of the export in *directory* and the manifest file's
    hash. Raises :class:`Refused` with the reason when it cannot be
    restored."""
    if not os.path.isdir(directory):
        raise Refused(f"{directory} is not an export directory")
    try:
        manifest = mexport.read_manifest(directory)
    except ValueError as exc:
        raise Refused(str(exc)) from None
    version, current = manifest["schema_version"], schema_version()
    if not (1 <= version <= current and version in MIGRATIONS):
        raise Refused(f"its schema v{version} is not one this Casa can "
                      f"migrate (v1 to v{current})")
    tables = manifest["counts"]["tables"]
    unregistered = sorted(set(tables) - _loadable())
    if unregistered:
        raise Refused(f"it holds {', '.join(unregistered)}, which this Casa "
                      "has no restore rule for")
    if version == current:
        missing = [t for t in mexport.exported_tables() if t not in tables]
        if missing:
            raise Refused(f"it lacks {', '.join(missing)}")
    try:
        mexport.verify_files(directory, manifest)
    except ValueError as exc:
        raise Refused(str(exc)) from None
    return manifest, _sha256(os.path.join(directory, mexport.MANIFEST))


# --- the request (casa-main) -------------------------------------------------------

_staging = threading.Lock()


def _refuse_if_pending(root: str) -> None:
    try:
        intent = maintenance.read_intent(root)
    except maintenance.IntentUnreadable:
        raise Refused(maintenance.UNREADABLE_REASON) from None
    if intent is not None:
        raise Refused(WIPE_PENDING if intent.get("kind") == "wipe"
                      else RESTORE_PENDING)


def stage(path: str, *, root: str = "/", confirm: bool,
          now: float | None = None) -> str:
    """Validate the export at *path*; on consent, stage it and write the
    intent. ``"needs_confirm"`` or ``"staged"``; raises :class:`Refused`,
    having scheduled nothing. Blocking."""
    source = resolve(path, root)
    _refuse_if_pending(root)
    manifest, manifest_sha = validate(source)
    if not confirm:
        return "needs_confirm"
    target = source_dir(root)
    if os.path.realpath(source) == os.path.realpath(target):
        raise Refused("that is the restore's own staging directory")
    with _staging:
        _refuse_if_pending(root)
        maintenance._remove(target)
        os.makedirs(target, mode=0o700)
        try:
            for name in (*manifest["files"], mexport.MANIFEST):
                _copy(os.path.join(source, name), os.path.join(target, name))
            mexport._fsync_dir(target)
            _, staged_sha = validate(target)
            if staged_sha != manifest_sha:
                raise Refused("the export changed while it was copied")
            state = maintenance.write_restore_intent({
                "kind": "restore", "phase": "staged",
                "manifest_sha256": manifest_sha,
                "export_created_at": manifest.get("created_at"),
                "requested_at": maintenance._iso(now), "attempts": [],
                "notified_container_ids": []}, root)
        except BaseException:
            maintenance._remove(target)
            raise
        if state != "written":
            maintenance._remove(target)
            raise Refused(WIPE_PENDING if state == "wipe_pending"
                          else RESTORE_PENDING)
    logger.info("memory restore staged from %s (manifest %s)", source,
                manifest_sha[:12])
    return "staged"


def _copy(src: str, dst: str) -> None:
    shutil.copyfile(src, dst, follow_symlinks=False)
    os.chmod(dst, 0o600)
    with open(dst, "rb") as fh:
        os.fsync(fh.fileno())


async def request_restore(
    path: str, *, reply: Callable[[str], Awaitable[object]],
    notify: Callable[[str], Awaitable[object]] | None = None,
    confirm: bool = True, root: str = "/",
) -> str:
    """A door's request: validate, and on consent stage, reply, and request
    the app restart at once. ``"refused"``, ``"needs_confirm"`` (the reply
    was the confirmation text) or ``"staged"``."""
    try:
        state = await asyncio.to_thread(stage, path, root=root,
                                        confirm=confirm)
    except Refused as exc:
        await reply(refused_line(str(exc)))
        return "refused"
    if state == "needs_confirm":
        await reply(CONFIRMATION)
        return state
    await reply(STAGED_LINE)
    await maintenance.request_restart(notify=notify, kind="restore")
    return state


# --- phase staged: build (the container-start step) -------------------------------

#: What each table row's restore rule does in the build, in order: load the
#: exported rows; after the check, put running jobs back in the queue; after
#: any forward migration, write ``meta`` fresh.
_TABLE_STEPS: dict[Restore, tuple[str, ...]] = {
    Restore.LOAD: ("load",),
    Restore.LOAD_REQUEUE: ("load", "requeue"),
    Restore.NONE: (),
    Restore.FRESH: ("fresh",),
}


def build_phase(intent: dict, *, root: str, now: float | None = None) -> bool:
    """Build ``built.db`` and move the intent to phase ``wiping``: ``True``.
    ``False`` when the restore was abandoned: nothing has changed, the
    outcome is recorded for ``casa-main``, and the intent and the source are
    gone. Only :func:`memory.maintenance.run` calls this."""
    try:
        built_sha = _build(source_dir(root), intent, now)
    except Exception as exc:  # noqa: BLE001 — every failure abandons
        reason = str(exc) or type(exc).__name__
        logger.error("memory restore abandoned: %s", reason,
                     exc_info=not isinstance(exc, (Refused, Mismatch)))
        _abandon(root, reason, now)
        return False
    intent["phase"] = "wiping"
    intent["built_sha256"] = built_sha
    maintenance.write_intent(intent, root)
    return True


def _steps(entry) -> tuple[str, ...]:
    steps = _TABLE_STEPS.get(entry.restore)
    if steps is None:
        raise maintenance.MaintenanceError(
            f"no restore rule for {entry.name!r}")
    return steps


def _build(source: str, intent: dict, now: float | None) -> str:
    manifest, manifest_sha = validate(source)
    if manifest_sha != intent.get("manifest_sha256"):
        raise Refused("the staged export is not the one requested")
    built = os.path.join(source, BUILT)
    for path in (built, *(built + s for s in registry.SQLITE_SIDECARS)):
        maintenance._remove(path)
    version = manifest["schema_version"]
    store = MemoryStore(source, name=BUILT)
    if not store.open(journal_mode="delete", target=version):
        raise RuntimeError(
            f"the store could not be built ({store.unavailable_reason})")
    tables = [e for e in registry.REGISTRY if e.kind is Kind.TABLE]
    with store.transaction(write=True) as conn:
        for entry in tables:
            if "load" in _steps(entry):
                for table in entry.paths:
                    if table in manifest["counts"]["tables"]:
                        _load(conn, table, source)
    _check(store, manifest)
    with store.transaction(write=True) as conn:
        for entry in tables:
            if "requeue" in _steps(entry):
                for table in entry.paths:
                    if "state" in _columns(conn, table):
                        conn.execute(
                            f'UPDATE "{table}" SET state = \'queued\', '
                            "boot_id = NULL WHERE state = 'running'")
    if version < schema_version() and not store.open(journal_mode="delete"):
        raise RuntimeError(f"the export's schema v{version} could not be "
                           f"migrated ({store.unavailable_reason})")
    with store.transaction(write=True) as conn:
        for entry in tables:
            if "fresh" in _steps(entry):
                _fresh_meta(conn, manifest, now)
    return _seal(built)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _load(conn: sqlite3.Connection, table: str, source: str) -> None:
    cols = _columns(conn, table)
    names = ", ".join(f'"{c}"' for c in cols)
    marks = ", ".join("?" for _ in cols)
    sql = f'INSERT INTO "{table}" ({names}) VALUES ({marks})'
    with open(os.path.join(source, f"{table}.jsonl"), "rb") as fh:
        for n, raw in enumerate(fh, 1):
            try:
                row = json.loads(raw)
            except ValueError:
                raise Mismatch(f"{table}.jsonl line {n} is not JSON") from None
            if not isinstance(row, dict) or set(row) != set(cols):
                raise Mismatch(f"{table}.jsonl line {n} does not hold "
                               f"{table}'s columns")
            conn.execute(sql, [row[c] for c in cols])


def _check(store: MemoryStore, manifest: dict) -> None:
    """Loaded equals the manifest exactly: the counts, every table dumped
    again byte for byte, and the obligations."""
    tables = list(manifest["counts"]["tables"])
    with store.transaction() as conn:
        if mexport.counts(conn, tables) != manifest["counts"]:
            raise Mismatch("the loaded row counts differ from the manifest")
        for table in tables:
            h = hashlib.sha256()
            for line in mexport.dump_table(conn, table):
                h.update(line)
            if h.hexdigest() != manifest["files"][f"{table}.jsonl"]:
                raise Mismatch(f"{table} as loaded differs from the export")
        if mexport.obligations(conn) != manifest["obligations"]:
            raise Mismatch("what the loaded store owes differs from the "
                           "manifest")


def _put_meta(conn: sqlite3.Connection, key: str, value: dict) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) "
        "DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, sort_keys=True)))


def _fresh_meta(conn: sqlite3.Connection, manifest: dict,
                now: float | None) -> None:
    conn.execute("DELETE FROM meta")
    at = maintenance._iso(now)
    _put_meta(conn, "first_boot", {"complete": True, "at": at})
    _put_meta(conn, "last_maintenance", {
        "kind": "restore", "outcome": "done", "reason": None, "at": at,
        "export_created_at": manifest.get("created_at"),
        "counts": manifest["counts"]["tables"], "notice_sent": False})


def _seal(built: str) -> str:
    """Check that ``built.db`` is one file, make it durable, and hash it."""
    conn = sqlite3.connect(built, isolation_level=None)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    if str(mode).lower() != "delete":
        raise RuntimeError(f"the built store is in journal mode {mode!r}")
    beside = [s for s in registry.SQLITE_SIDECARS
              if os.path.lexists(built + s)]
    if beside:
        raise RuntimeError(f"the built store has {', '.join(beside)} beside it")
    with open(built, "rb") as fh:
        os.fsync(fh.fileno())
    mexport._fsync_dir(os.path.dirname(built))
    return _sha256(built)


def _abandon(root: str, reason: str, now: float | None) -> None:
    """Record the outcome in the current store, then delete the intent, then
    the source: never the source first, so a kill between them leaves a
    source with no intent, which the next step deletes."""
    _record_abandoned(root, reason, now)
    memory_dir = under(root, registry.MEMORY_DIR)
    maintenance._remove(maintenance.intent_path(root))
    mexport._fsync_dir(memory_dir)
    maintenance._remove(source_dir(root))
    mexport._fsync_dir(memory_dir)


def _record_abandoned(root: str, reason: str, now: float | None) -> None:
    record = {"kind": "restore", "outcome": "abandoned", "reason": reason,
              "at": maintenance._iso(now), "notice_sent": False}
    memory_dir = under(root, registry.MEMORY_DIR)
    try:
        store = MemoryStore(memory_dir)
        if os.path.lexists(store.path):
            # The file as it is: the step never migrates the current store.
            store = store.as_found()
        elif not store.open():
            raise RuntimeError(store.unavailable_reason)
        with store.transaction(write=True) as conn:
            _put_meta(conn, "last_maintenance", record)
    except Exception:  # noqa: BLE001 — memory is unchanged either way
        logger.error("memory restore: the abandonment could not be recorded "
                     "in the current store, so it will not be reported",
                     exc_info=True)


# --- phase wiping: install (the container-start step) -----------------------------

def check_built(intent: dict, *, root: str) -> None:
    """Before the wipe: the intent is in phase ``wiping`` and ``built.db`` is
    there with its recorded hash. Only :func:`memory.maintenance.run` calls
    this."""
    if intent.get("phase") != "wiping":
        raise maintenance.MaintenanceError(
            f"restore phase {intent.get('phase')!r} is unknown")
    built = os.path.join(source_dir(root), BUILT)
    if not os.path.isfile(built):
        raise maintenance.MaintenanceError("the built store is missing")
    if _sha256(built) != intent.get("built_sha256"):
        raise maintenance.MaintenanceError(
            "the built store does not match its record")


def _install(root: str, entry, intent: dict, counts) -> None:
    source = source_dir(root)
    copy = os.path.join(source, INSTALL_COPY)
    target = under(root, entry.paths[0])
    maintenance._remove(copy)
    _copy(os.path.join(source, BUILT), copy)
    if _sha256(copy) != intent.get("built_sha256"):
        raise maintenance.MaintenanceError(
            "the installed copy does not match the built store")
    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
    for sidecar in registry.SQLITE_SIDECARS:
        maintenance._remove(target + sidecar)
    os.replace(copy, target)
    mexport._fsync_dir(os.path.dirname(target))


def _reset_again(root: str, entry, intent: dict, counts) -> None:
    maintenance._reset_pointers(root, entry, counts)


def _nothing(root: str, entry, intent: dict, counts) -> None:
    """Loaded into the built store, wiped and not restored, rebuilt by
    ``casa-main``'s boot check from the installed store, or the source and
    intent, which the step removes last."""


_AFTER_WIPE: dict[tuple[Kind, Restore], Callable] = {
    (Kind.FILE, Restore.INSTALL): _install,
    (Kind.TABLE, Restore.LOAD): _nothing,
    (Kind.TABLE, Restore.LOAD_REQUEUE): _nothing,
    (Kind.TABLE, Restore.NONE): _nothing,
    (Kind.TABLE, Restore.FRESH): _nothing,
    (Kind.FILE, Restore.NONE): _nothing,
    (Kind.FILE, Restore.REBUILD): _nothing,
    (Kind.DIRECTORY, Restore.NONE): _nothing,
    (Kind.DIRECTORY, Restore.SOURCE): _nothing,
    (Kind.TRANSCRIPTS, Restore.NONE): _nothing,
    (Kind.SERVED_COPY, Restore.NONE): _nothing,
    (Kind.POINTERS, Restore.RESET): _reset_again,
}


def install_phase(intent: dict, *, root: str, counts) -> None:
    """After the wipe loop: each row's restore rule; ``memory.db`` gets a
    copy of the built store. Only :func:`memory.maintenance.run` calls
    this."""
    for entry in registry.REGISTRY:
        step = _AFTER_WIPE.get((entry.kind, entry.restore))
        if step is None:
            raise maintenance.MaintenanceError(
                f"no restore rule for {entry.name!r}")
        step(root, entry, intent, counts)
