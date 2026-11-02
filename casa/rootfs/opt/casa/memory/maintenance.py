"""The memory wipe and restore: their intent, the container-start step that
runs them, and what ``casa-main`` does about an intent it finds.

**The intent.** A door that the operator consented at writes
``/data/memory/maintenance.json`` (kind ``wipe``) and asks the Supervisor for
an app restart. The intent records every attempt to carry it out, one per
container start, and every container start that already told the operator
about a failed attempt.

**The container-start step** (:func:`run`, the ``init-memory-maintenance``
oneshot, ``python -m memory.maintenance``) is the only caller of the wipe
loop. It runs before every longrun and every engagement service. Before its
first deletion it records this container's attempt in the intent, so an
attempt that is cut off is still seen as one. The wipe loop then:

* A. deletes every terminal engagement workspace whose retention has passed,
  by the workspace sweep's own rule;
* B. locates the executor-memory section of every remaining workspace
  ``CLAUDE.md``, and stops before any deletion if one cannot be located;
* C. walks the registry and applies each row's wipe rule: files and
  directories deleted whole, transcript roots emptied, the session pointers
  reset, each producer's staging files deleted, every executor-memory section
  blanked (every other byte of its file kept) and every cache emptied.

Then a fresh, empty store is created and the intent is removed last. Every
step treats an absent path as already done, so a run cut off anywhere re-runs
to the same result. Any error stops the run, is recorded in the attempt, and
the step still exits 0.

A **restore** intent (kind ``restore``, :mod:`memory.restore`) runs here too:
in phase ``staged`` the step builds the new store beside the current one (or
abandons the restore with memory unchanged); in phase ``wiping`` it runs the
same wipe loop, keeping the staged source, installs the built store, and
removes the intent, then the source.

**In casa-main** (:func:`read_intent`, :func:`notification_only`,
:func:`request_restart`): an intent with no attempt recorded has not touched
memory, so Casa boots normally and asks for the restart again; an intent with
any attempt recorded means memory may be partly deleted, so Casa opens no
memory, starts nothing, sends the operator one line per container start and
waits. The window between consent and the restart has no admission
guarantee: whatever runs there is wiped by the next container start.

This module imports nothing from the running Casa process.
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import shutil
import signal
import stat
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass

import atomic_io
import safe_fs
import supervisor_restart
from memory import executor_block, registry
from memory.registry import Kind, Wipe, under

logger = logging.getLogger(__name__)

INTENT_PATH = f"{registry.MEMORY_DIR}/maintenance.json"
INTENT_STAGING_PREFIX = ".maintenance.json."

#: The workspace sweep's per-engagement facts, kept beside the workspace in
#: its control directory (a legacy copy may still sit in the workspace).
CASA_META = ".casa-meta.json"
EXECUTOR_MEMORY = ".executor_memory"
#: A real allocated engagement uid, and its private plugin outbox; the
#: retention step removes it with its workspace, as the sweep does
#: (``engagement_uids.UID_BASE``, ``plugin_outbox.ENGAGEMENT_OUTBOX_ROOT``).
UID_BASE = 200000
ENGAGEMENT_OUTBOX_ROOT = "/data/plugin-outbox-eng"

#: The courtesy flag is valid this long after the Supervisor accepted.
RESTART_GRACE_S = 300.0

#: A test hook for the long-oneshot measurement on a test install: seconds
#: the step sleeps after recording its attempt and before its first deletion.
TEST_SLEEP_ENV = "CASA_TEST_MAINTENANCE_SLEEP_S"

# --- what the operator reads ------------------------------------------------
CONFIRMATION = (
    "This erases everything I remember: our saved conversations, my notes "
    "about you, the search index and my memory backups. Every conversation "
    "starts fresh. Your reminders, triggers, files, settings and prompts, any "
    "tasks still pending, plugin data, logs, and everything else outside "
    "memory stay. They may still mention something you want gone, so delete "
    "those yourself. Casa restarts to do this. Copies outside Casa are "
    "untouched: restoring a Home Assistant backup or a downloaded export "
    "brings that copy's memory back."
)
FORGETTING_LINE = "Forgetting now. I'll restart and be back in a minute."
ALREADY_SCHEDULED_LINE = "A memory wipe is already scheduled. Restarting now."
RESTORE_PENDING_LINE = (
    "A memory restore is already scheduled for the next app restart, so no "
    "wipe was started."
)
RESTART_FAILED_LINE = (
    "I couldn't restart myself, so memory isn't wiped yet. It will be at the "
    "next app restart; you can restart the app now."
)
COURTESY_LINE = "Restarting to wipe memory — send it again in a minute."
RESTART_FAILED_LINES = {
    "wipe": RESTART_FAILED_LINE,
    "restore": "I couldn't restart myself, so memory isn't restored yet. It "
               "will be at the next app restart; you can restart the app now.",
}
COURTESY_LINES = {
    "wipe": COURTESY_LINE,
    "restore": "Restarting to restore memory — send it again in a minute.",
}
FORGOTTEN_LINE = (
    "Done. I've forgotten everything. Your reminders, triggers, files and "
    "settings are still here."
)


def restored_line(export_created_at: str | None) -> str:
    when = (export_created_at or "").replace("T", " ").replace("Z", " UTC")
    return ("Done. Memory is restored"
            + (f" from the export of {when.strip()}." if when else "."))


def abandoned_line(reason: str | None) -> str:
    return (f"The restore didn't happen ({reason or 'unknown reason'}); "
            "memory is unchanged.")


INTERRUPTED_LINES = {
    "wipe": "The memory wipe was interrupted before it finished; restart the "
            "app to retry.",
    "restore": "The restore was interrupted before it finished; restart the "
               "app to retry.",
}
UNREADABLE_REASON = "the maintenance request could not be read"
#: A restart cannot fix an unreadable record, so the line names the one
#: action that does.
UNREADABLE_LINE = (
    "Memory maintenance could not read its record, so nothing was deleted. "
    f"Remove {INTENT_PATH}, then restart the app."
)


def failed_line(kind: str, error: str) -> str:
    what = "The restore" if kind == "restore" else "The memory wipe"
    return (f"{what} failed at startup ({error}). I'm not taking messages "
            "until it succeeds; restart the app to retry.")


def notification_line(intent: dict) -> str:
    """The one line for an intent with an attempt recorded: the latest
    attempt's error, or the fixed line for an attempt cut off before it
    could record one."""
    kind = intent.get("kind")
    if kind == "unreadable":
        return UNREADABLE_LINE
    error = intent["attempts"][-1].get("error")
    if error:
        return failed_line(kind, error)
    return INTERRUPTED_LINES.get(kind, INTERRUPTED_LINES["wipe"])


class IntentUnreadable(ValueError):
    """``maintenance.json`` exists but does not hold an intent."""


class MaintenanceError(RuntimeError):
    """The step cannot complete; the message is the operator's reason."""


class Unlocatable(MaintenanceError):
    def __init__(self, engagement_id: str) -> None:
        super().__init__(
            f"executor memory of engagement {engagement_id} could not be "
            "located")
        self.engagement_id = engagement_id


# ---------------------------------------------------------------------------
# The intent
# ---------------------------------------------------------------------------

def intent_path(root: str = "/") -> str:
    return under(root, INTENT_PATH)


def read_intent(root: str = "/") -> dict | None:
    """The pending intent, or ``None``. Raises :class:`IntentUnreadable` for
    a file that is not one."""
    try:
        with open(intent_path(root), "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:          # a decoding error is one too
        raise IntentUnreadable(UNREADABLE_REASON) from None
    if (not isinstance(data, dict)
            or not isinstance(data.get("attempts", []), list)
            or not isinstance(data.get("notified_container_ids", []), list)):
        raise IntentUnreadable(UNREADABLE_REASON)
    data.setdefault("attempts", [])
    data.setdefault("notified_container_ids", [])
    return data


def unreadable_intent() -> dict:
    """What stands in for an unreadable file: an attempt with its error, so
    nothing is deleted on its behalf and the operator is told."""
    return {"kind": "unreadable", "requested_at": None,
            "attempts": [{"container_id": None, "at": None,
                          "error": UNREADABLE_REASON}],
            "notified_container_ids": []}


def write_intent(intent: dict, root: str = "/") -> None:
    """Atomic write plus fsync of the file and its directory."""
    path = intent_path(root)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    atomic_io.atomic_write_json(
        path, intent, mode=atomic_io.PRIVATE, prefix=INTENT_STAGING_PREFIX)


#: Serializes the doors' check-then-write of the intent, so a wipe and a
#: restore consented at the same moment cannot overwrite each other.
_intent_lock = threading.Lock()


def write_wipe_intent(root: str = "/", *, now: float | None = None) -> str:
    """Record the operator's consent. ``"written"``, ``"already"`` when a
    wipe is pending, or ``"restore_pending"``: a pending restore is never
    replaced by a wipe."""
    with _intent_lock:
        try:
            existing = read_intent(root)
        except IntentUnreadable:
            return "already"
        if existing is not None:
            return ("already" if existing.get("kind") == "wipe"
                    else "restore_pending")
        write_intent({"kind": "wipe", "requested_at": _iso(now),
                      "attempts": [], "notified_container_ids": []}, root)
    return "written"


def write_restore_intent(intent: dict, root: str = "/") -> str:
    """Record a staged restore. ``"written"``, or ``"wipe_pending"`` /
    ``"restore_pending"`` when an intent already exists (it is kept)."""
    with _intent_lock:
        try:
            existing = read_intent(root)
        except IntentUnreadable:
            return "wipe_pending"
        if existing is not None:
            return ("wipe_pending" if existing.get("kind") == "wipe"
                    else "restore_pending")
        write_intent(intent, root)
    return "written"


def container_id(proc_root: str = "/proc") -> str:
    """This container start: the host's boot id and the start time of PID 1
    of the container's PID namespace. It changes with every container start
    and survives a respawn of any service inside it."""
    with open(os.path.join(proc_root, "sys/kernel/random/boot_id"),
              encoding="ascii") as fh:
        boot_id = fh.read().strip()
    with open(os.path.join(proc_root, "1/stat"), encoding="utf-8",
              errors="replace") as fh:
        stat_line = fh.read()
    # Field 2 (the command) is parenthesised and may hold spaces; field 22,
    # the start time, is the 20th field after it.
    starttime = stat_line[stat_line.rindex(")") + 2:].split()[19]
    return hashlib.sha256(
        f"{boot_id}\0{starttime}".encode()).hexdigest()[:32]


def _iso(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() if now is None else now))


# ---------------------------------------------------------------------------
# The container-start step
# ---------------------------------------------------------------------------

@dataclass
class Counts:
    workspaces_swept: int = 0
    paths_deleted: int = 0
    transcript_entries_deleted: int = 0
    staging_files_deleted: int = 0
    pointers_reset: int = 0
    sessions_reset: int = 0
    sections_blanked: int = 0
    caches_emptied: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def run(*, root: str = "/", proc_root: str = "/proc",
        now: float | None = None) -> str:
    """The oneshot. ``"none"`` (no intent), ``"done"``, ``"failed"``, or
    ``"abandoned"`` (a restore that could not be built; memory unchanged)."""
    try:
        intent = read_intent(root)
    except IntentUnreadable:
        intent = unreadable_intent()
        intent["attempts"][0].update(container_id=_container_id(proc_root),
                                     at=_iso(now))
        write_intent(intent, root)
        logger.error("memory maintenance: %s", UNREADABLE_REASON)
        return "failed"
    if intent is None:
        # A staged restore source without its intent is never used again.
        _remove(under(root, registry.RESTORE_SOURCE))
        return "none"

    cid = _container_id(proc_root)
    logger.info("memory maintenance: %s requested at %s, container %s",
                intent.get("kind"), intent.get("requested_at"), cid)
    # Recorded before the first deletion, so an attempt that is cut off is
    # still seen as one.
    attempt = {"container_id": cid, "at": _iso(now), "error": None}
    intent["attempts"].append(attempt)
    write_intent(intent, root)
    kind = intent.get("kind")
    try:
        _test_sleep()
        if kind == "unreadable":
            raise MaintenanceError(UNREADABLE_REASON)
        if kind == "wipe":
            counts = wipe_loop(root=root, now=now)
            _fresh_store(root, now, counts)
        elif kind == "restore":
            from memory import restore
            if intent.get("phase") == "staged":
                if not restore.build_phase(intent, root=root, now=now):
                    logger.info("memory maintenance: restore abandoned; "
                                "memory unchanged")
                    return "abandoned"
            restore.check_built(intent, root=root)
            counts = wipe_loop(root=root, now=now, restoring=True)
            restore.install_phase(intent, root=root, counts=counts)
        else:
            raise MaintenanceError(
                f"{kind!r} is not a request this version can carry out")
    except Exception as exc:  # noqa: BLE001 — recorded, the step exits 0
        attempt["error"] = str(exc) or type(exc).__name__
        logger.error("memory maintenance failed: %s", attempt["error"],
                     exc_info=not isinstance(exc, MaintenanceError))
        write_intent(intent, root)
        return "failed"
    _remove(intent_path(root))
    intent_row = next(e for e in registry.REGISTRY if e.wipe is Wipe.REMOVE_LAST)
    for pattern in intent_row.staging_patterns:
        for path in _staging_matches(root, pattern):
            _remove(path)
    atomic_io.fsync_directory(under(root, registry.MEMORY_DIR))
    if kind == "restore":
        # Only after the intent: a source with no intent is deleted by the
        # next step, never used.
        _remove(under(root, registry.RESTORE_SOURCE))
        atomic_io.fsync_directory(under(root, registry.MEMORY_DIR))
    logger.info("memory maintenance: %s done %s", kind, counts.as_dict())
    return "done"


def _container_id(proc_root: str) -> str:
    try:
        return container_id(proc_root)
    except (OSError, ValueError, IndexError) as exc:
        logger.warning("memory maintenance: no container id (%s)", exc)
        return "unknown"


def _test_sleep() -> None:
    raw = os.environ.get(TEST_SLEEP_ENV, "").strip()
    if raw:
        seconds = float(raw)
        logger.warning("memory maintenance: test hook sleeps %.0f s", seconds)
        time.sleep(seconds)


def _fresh_store(root: str, now: float | None, counts: Counts) -> None:
    from memory.store import MemoryStore

    store = MemoryStore(under(root, registry.MEMORY_DIR))
    if not store.open():
        raise MaintenanceError(
            f"a fresh memory store could not be created "
            f"({store.unavailable_reason})")
    record = {"kind": "wipe", "outcome": "done", "reason": None,
              "at": _iso(now), "counts": counts.as_dict(),
              "notice_sent": False}
    with store.transaction(write=True) as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key)"
            " DO UPDATE SET value = excluded.value",
            ("last_maintenance", json.dumps(record, sort_keys=True)))


def notice_for(record: dict) -> str | None:
    """The one line a recorded maintenance outcome owes the operator."""
    kind, outcome = record.get("kind"), record.get("outcome")
    if kind == "wipe" and outcome == "done":
        return FORGOTTEN_LINE
    if kind == "restore" and outcome == "done":
        return restored_line(record.get("export_created_at"))
    if kind == "restore" and outcome == "abandoned":
        return abandoned_line(record.get("reason"))
    return None


def claim_outcome_notice(store) -> str | None:
    """The outcome's line (a wipe or restore done, a restore abandoned),
    once: ``notice_sent`` is set in the same transaction that reads it,
    before the line is sent, so a crash costs the line rather than sending
    it twice."""
    if not store.available:
        return None
    with store.transaction(write=True) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = "
                           "'last_maintenance'").fetchone()
        if row is None:
            return None
        record = json.loads(row[0])
        line = notice_for(record)
        if line is None or record.get("notice_sent"):
            return None
        record["notice_sent"] = True
        conn.execute("UPDATE meta SET value = ? WHERE key = "
                     "'last_maintenance'", (json.dumps(record, sort_keys=True),))
    return line


# ---------------------------------------------------------------------------
# The wipe loop (steps A, B and C)
# ---------------------------------------------------------------------------

def wipe_loop(*, root: str, now: float | None = None,
              restoring: bool = False) -> Counts:
    """Erase exactly the registry. Only :func:`run` calls this. A restore
    (*restoring*) keeps the rows its intent owns: the staged source."""
    counts = Counts()
    _sweep_expired_workspaces(root, now, counts)          # A
    for engagement_id, ws in _workspaces(root):           # B
        if os.path.lexists(os.path.join(ws, "CLAUDE.md")):
            if _locate(ws).case == 4:
                raise Unlocatable(engagement_id)
    for entry in registry.REGISTRY:                       # C
        if restoring and entry.wipe is Wipe.KEEP_WHILE_INTENT:
            continue
        handler = _HANDLERS.get((entry.kind, entry.wipe))
        if handler is None:
            raise MaintenanceError(f"no wipe rule for {entry.name!r}")
        handler(root, entry, counts)
    _reset_open_in_casa_sessions(root, counts)
    return counts


#: The engagement registry's record file.
ENGAGEMENTS_RECORD = "/data/engagements.json"


def _reset_open_in_casa_sessions(root: str, counts: Counts) -> None:
    """Every open in_casa engagement's session transcript is gone with the
    transcript roots, so a resume of it cannot start. Each active or idle
    in_casa record gets ``context_rebuild_pending``: its next turn opens a
    fresh session, and its record is kept. (A claude_code engagement drops
    its session pointer at boot replay instead.)

    The file is read strictly and never repaired: one that cannot be read or
    parsed, or that holds a malformed row, is left byte for byte as it is and
    nothing is marked. The marks are written back as the same parsed rows
    with only that field added, atomically, so no record can be dropped."""
    path = under(root, ENGAGEMENTS_RECORD)
    try:
        with open(path, "rb") as fh:
            rows = json.loads(fh.read().decode("utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        logger.warning("memory maintenance: %s not readable (%s); open "
                       "in_casa engagements are not marked for a fresh "
                       "session", path, exc)
        return
    if not isinstance(rows, list) or not all(
            isinstance(r, dict) and isinstance(r.get("status"), str)
            and isinstance(r.get("driver"), str) for r in rows):
        logger.warning("memory maintenance: %s holds a malformed record; "
                       "open in_casa engagements are not marked for a fresh "
                       "session", path)
        return
    marked = 0
    for row in rows:
        if (row["status"] in ("active", "idle") and row["driver"] != "claude_code"
                and row.get("context_rebuild_pending") is not True):
            row["context_rebuild_pending"] = True
            marked += 1
    if marked:
        atomic_io.atomic_write_json(path, rows, indent=2, mode=0o600)
    counts.sessions_reset += marked


def _workspaces(root: str) -> Iterator[tuple[str, str]]:
    """``(engagement id, workspace dir)`` for every real directory under the
    engagements root, from the filesystem, whatever the engagement's status."""
    yield from _real_subdirs(under(root, registry.ENGAGEMENTS_ROOT))


def _real_subdirs(parent: str) -> Iterator[tuple[str, str]]:
    try:
        entries = sorted(os.scandir(parent), key=lambda e: e.name)
    except FileNotFoundError:
        return
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            yield entry.name, entry.path


def _cache_path(root: str, engagement_id: str) -> str:
    return os.path.join(under(root, registry.ENGAGEMENT_CONTROL_ROOT),
                        engagement_id, EXECUTOR_MEMORY)


# --- A: retention --------------------------------------------------------

def _sweep_expired_workspaces(root: str, now: float | None,
                              counts: Counts) -> None:
    """The workspace sweep's rule: a workspace whose recorded status is not
    UNDERGOING and whose ``retention_until`` has passed goes whole, with its
    control directory and its uid's outbox."""
    now_iso = _iso(now)
    ctl_root = under(root, registry.ENGAGEMENT_CONTROL_ROOT)
    for engagement_id, ws in list(_workspaces(root)):
        meta = _load_meta(os.path.join(ctl_root, engagement_id), ws)
        if meta is None or meta.get("status") == "UNDERGOING":
            continue
        retention_until = meta.get("retention_until")
        if not isinstance(retention_until, str) or retention_until > now_iso:
            continue
        _remove(ws)
        _remove(os.path.join(ctl_root, engagement_id))
        uid = meta.get("allocated_uid")
        if isinstance(uid, int) and not isinstance(uid, bool) and uid >= UID_BASE:
            _remove(os.path.join(under(root, ENGAGEMENT_OUTBOX_ROOT), str(uid)))
        counts.workspaces_swept += 1
        logger.info("memory maintenance: removed workspace %s (past its "
                    "retention)", engagement_id)


def _load_meta(ctl_dir: str, ws: str) -> dict | None:
    try:
        with open(os.path.join(ctl_dir, CASA_META), encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        try:
            text = safe_fs.read_text_beneath(ws, CASA_META)
        except OSError:
            return None
    except OSError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# --- B and C: the executor-memory section --------------------------------

OPEN = executor_block.OPEN.encode()
CLOSE = executor_block.CLOSE.encode()


@dataclass(frozen=True)
class _Section:
    #: 1 one marker pair, 0 no marker pair (no executor memory), 4 not
    #: locatable.
    case: int
    #: The file's bytes with the section blanked, when they differ.
    blanked: bytes | None = None


def _locate(ws: str) -> _Section:
    """Case 1: exactly one marker pair, its content emptied. A file with no
    marker pair holds no executor memory (case 0). Anything else is case 4."""
    try:
        text = _read_beneath(ws, "CLAUDE.md")
    except OSError:
        return _Section(4)
    spans = _marker_spans(text)
    if spans is None:
        return _Section(4)
    if not spans:
        return _Section(0)
    (start, end), = spans
    blanked = text[:start] + text[end:]
    return _Section(1, blanked if blanked != text else None)


def _marker_spans(text: bytes) -> list[tuple[int, int]] | None:
    """The ``(start, end)`` of the content between the one marker pair,
    ``[]`` for a file with no markers, or ``None`` when the markers do not
    make exactly one pair in order."""
    marks = sorted([(i, OPEN) for i in _find_all(text, OPEN)]
                   + [(i, CLOSE) for i in _find_all(text, CLOSE)])
    if len(marks) % 2:
        return None
    spans = []
    for k in range(0, len(marks), 2):
        (o, om), (c, cm) = marks[k], marks[k + 1]
        if om is not OPEN or cm is not CLOSE:
            return None
        spans.append((o + len(OPEN), c))
    # This release writes one pair (templates carry one slot, and every
    # other input is neutralised); more than one is not ours to choose from.
    return spans if len(spans) <= 1 else None


def _find_all(text: bytes, needle: bytes) -> Iterator[int]:
    i = text.find(needle)
    while i != -1:
        yield i
        i = text.find(needle, i + len(needle))


def _read_beneath(ws: str, name: str) -> bytes:
    fd = safe_fs.open_beneath(ws, name)
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def _blank_sections(root: str, entry, counts: Counts) -> None:
    """Every workspace ``CLAUDE.md`` keeps every byte outside its section;
    the section is blanked, then the cache is emptied. A file that cannot be
    located now stops the run here."""
    for pattern in entry.staging_patterns:
        for path in _staging_matches(root, pattern):
            _remove(path)
            counts.staging_files_deleted += 1
    for engagement_id, ws in _workspaces(root):
        if not os.path.lexists(os.path.join(ws, "CLAUDE.md")):
            continue
        cache = _cache_path(root, engagement_id)
        section = _locate(ws)
        if section.case == 4:
            raise Unlocatable(engagement_id)
        if section.blanked is not None:
            _rewrite_beneath(ws, "CLAUDE.md", section.blanked)
            counts.sections_blanked += 1
        _empty_cache(cache, counts)
    # Every cache, enumerated on its own: one with no CLAUDE.md beside it is
    # an orphan (a sweep or a provisioning cut off) and is emptied at once.
    for engagement_id, _ctl in _real_subdirs(
            under(root, registry.ENGAGEMENT_CONTROL_ROOT)):
        ws_file = os.path.join(under(root, registry.ENGAGEMENTS_ROOT),
                               engagement_id, "CLAUDE.md")
        if not os.path.lexists(ws_file):
            _empty_cache(_cache_path(root, engagement_id), counts)


def _rewrite_beneath(ws: str, name: str, data: bytes) -> None:
    """Atomic rewrite without following a symlink, keeping the file's owner
    and mode."""
    ws_owner = os.lstat(ws).st_uid
    owner = ws_owner if ws_owner != 0 else None
    fd = safe_fs.open_beneath(ws, name)
    try:
        st = os.fstat(fd)
    finally:
        os.close(fd)
    safe_fs.atomic_write_beneath(ws, name, data, owner_uid=owner,
                                 mode=stat.S_IMODE(st.st_mode))
    fd = safe_fs.open_beneath(ws, name)
    try:
        if (st.st_uid, st.st_gid) != (os.getuid(), os.getgid()):
            os.fchown(fd, st.st_uid, st.st_gid)
    finally:
        os.close(fd)


def _empty_cache(path: str, counts: Counts) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
        return
    atomic_io.atomic_write_text(path, "", mode=stat.S_IMODE(st.st_mode),
                                prefix=f"{EXECUTOR_MEMORY}.")
    counts.caches_emptied += 1


# --- C: the other rows -----------------------------------------------------

def _skip(root: str, entry, counts: Counts) -> None:
    """Tables go with ``memory.db``; ``meta`` comes back with the fresh store;
    the intent is removed last, by :func:`run`."""


def _delete_whole(root: str, entry, counts: Counts) -> None:
    for declared in entry.paths:
        if _remove(under(root, declared)):
            counts.paths_deleted += 1
    for pattern in entry.staging_patterns:
        for path in _staging_matches(root, pattern):
            _remove(path)
            counts.staging_files_deleted += 1


def _empty_transcript_roots(root: str, entry, counts: Counts) -> None:
    """The content of every real transcript directory; a root that is a
    symlink is not one (``/root/.claude/projects`` after its migration)."""
    for declared in entry.paths:
        for path in _expand(root, declared):
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(st.st_mode):
                continue
            for child in list(os.scandir(path)):
                _remove(child.path)
                counts.transcript_entries_deleted += 1


def _reset_pointers(root: str, entry, counts: Counts) -> None:
    for declared in entry.paths:
        path = under(root, declared)
        prefix = "." + os.path.basename(declared) + "."
        atomic_io.atomic_write_json(path, {}, mode=atomic_io.PRIVATE,
                                    prefix=prefix)
        counts.pointers_reset += 1
    for pattern in entry.staging_patterns:
        for path in _staging_matches(root, pattern):
            _remove(path)
            counts.staging_files_deleted += 1


_HANDLERS: dict[tuple[Kind, Wipe], Callable] = {
    (Kind.TABLE, Wipe.DELETE): _skip,
    (Kind.TABLE, Wipe.FRESH): _skip,
    (Kind.FILE, Wipe.REMOVE_LAST): _skip,
    (Kind.FILE, Wipe.DELETE): _delete_whole,
    (Kind.FILE, Wipe.DELETE_REBUILD): _delete_whole,
    (Kind.DIRECTORY, Wipe.DELETE): _delete_whole,
    # A wipe intent never owns a restore source; a restore skips the row.
    (Kind.DIRECTORY, Wipe.KEEP_WHILE_INTENT): _delete_whole,
    (Kind.TRANSCRIPTS, Wipe.DELETE): _empty_transcript_roots,
    (Kind.POINTERS, Wipe.RESET): _reset_pointers,
    (Kind.SERVED_COPY, Wipe.DELETE): _delete_whole,
    (Kind.SERVED_COPY, Wipe.EMPTY_BLOCK): _blank_sections,
}


# --- paths -----------------------------------------------------------------

def _remove(path: str) -> bool:
    """Delete *path* whole; an absent path is already done. A symlink is
    removed as a link, never followed."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    try:
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    except FileNotFoundError:
        return False
    return True


def _expand(root: str, declared: str) -> list[str]:
    """*declared* beneath *root*, each ``*`` standing for one engagement id.
    From the first ``*`` on no symlink is followed: those directories belong
    to an engagement."""
    parts = declared.strip("/").split("/")
    if "*" not in parts:
        return [under(root, declared)]
    first = parts.index("*")
    paths = [under(root, "/" + "/".join(parts[:first]))]
    for i, part in enumerate(parts[first:], start=first):
        last = i == len(parts) - 1
        nxt = []
        for base in paths:
            if part == "*":
                nxt.extend(path for _n, path in _real_subdirs(base))
                continue
            path = os.path.join(base, part)
            if not last:
                try:
                    if not stat.S_ISDIR(os.lstat(path).st_mode):
                        continue
                except FileNotFoundError:
                    continue
            nxt.append(path)
        paths = nxt
    return paths


def _staging_matches(root: str, pattern: str) -> list[str]:
    """Files matching a declared staging pattern (``*`` in the directory part
    is an engagement id, in the name a wildcard)."""
    directory, name = os.path.split(pattern)
    out = []
    for d in _expand(root, directory):
        try:
            entries = list(os.scandir(d))
        except (FileNotFoundError, NotADirectoryError):
            continue
        out.extend(e.path for e in entries
                   if fnmatch.fnmatchcase(e.name, name)
                   and not e.is_dir(follow_symlinks=False))
    return sorted(out)


# ---------------------------------------------------------------------------
# casa-main: the restart request and the courtesy flag
# ---------------------------------------------------------------------------

#: ``time.monotonic()`` when the Supervisor accepted the restart request, or
#: ``None``. Read only through :func:`restart_pending`, by the operator DM
#: path and the engagement-topic path.
restart_accepted_at: float | None = None
#: What the last restart request was for (``wipe`` or ``restore``): the
#: courtesy line and the failure line name it.
_restart_kind = "wipe"

_notify: Callable[[str], Awaitable[object]] | None = None
_resume_updates: Callable[[], Awaitable[object]] | None = None
_expiry: asyncio.Task | None = None


def configure(*, notify: Callable[[str], Awaitable[object]] | None) -> None:
    """How Casa sends the operator a line outside a door's own reply."""
    global _notify
    _notify = notify


def courtesy_line() -> str:
    """The courtesy line for the restart under way."""
    return COURTESY_LINES.get(_restart_kind, COURTESY_LINE)


def restart_pending() -> bool:
    """The courtesy check: the Supervisor accepted a restart request in this
    process less than :data:`RESTART_GRACE_S` ago."""
    accepted = restart_accepted_at
    return (accepted is not None
            and time.monotonic() - accepted < RESTART_GRACE_S)


async def request_wipe(
    *, reply: Callable[[str], Awaitable[object]],
    notify: Callable[[str], Awaitable[object]] | None = None,
    pause_updates: Callable[[], Awaitable[bool]] | None = None,
    resume_updates: Callable[[], Awaitable[object]] | None = None,
    root: str = "/",
) -> str:
    """A door's consent: write the intent, reply, and request the app
    restart at once. Returns :func:`write_wipe_intent`'s answer."""
    state = await asyncio.to_thread(write_wipe_intent, root)
    if state == "restore_pending":
        await reply(RESTORE_PENDING_LINE)
        return state
    await reply(FORGETTING_LINE if state == "written"
                else ALREADY_SCHEDULED_LINE)
    await request_restart(notify=notify, pause_updates=pause_updates,
                          resume_updates=resume_updates)
    return state


async def request_restart(
    *, notify: Callable[[str], Awaitable[object]] | None = None,
    pause_updates: Callable[[], Awaitable[bool]] | None = None,
    resume_updates: Callable[[], Awaitable[object]] | None = None,
    kind: str | None = "wipe",
) -> bool:
    """Ask the Supervisor for the app restart (for a ``wipe`` or a
    ``restore``, *kind*). On success the courtesy flag
    is set for :data:`RESTART_GRACE_S`; on a failure, or when the flag
    expires with no restart, the operator gets one line and Casa keeps
    running. *pause_updates* stops update intake first (it returns whether
    it paused anything), so the update that carried the consent is confirmed
    to Telegram before the container stops; *resume_updates* undoes it."""
    global restart_accepted_at, _resume_updates, _expiry, _restart_kind
    _restart_kind = kind if kind in RESTART_FAILED_LINES else "wipe"
    if pause_updates is not None:
        try:
            if await pause_updates():
                _resume_updates = resume_updates
        except Exception:  # noqa: BLE001 — the restart is still requested
            logger.warning("pausing updates before the restart failed",
                           exc_info=True)
    result = await supervisor_restart.request_app_restart()
    if supervisor_restart.accepted(result):
        stamp = time.monotonic()
        restart_accepted_at = stamp
        if _expiry is not None and not _expiry.done():
            _expiry.cancel()
        _expiry = asyncio.get_running_loop().create_task(
            _expire(stamp), name="memory-restart-grace")
        logger.info("memory %s: app restart requested (%s)", _restart_kind,
                    result)
        return True
    logger.warning("memory %s: app restart request failed: %s",
                   _restart_kind, result)
    await _resume()
    await _say(notify or _notify, RESTART_FAILED_LINES[_restart_kind])
    return False


async def _expire(stamp: float) -> None:
    global restart_accepted_at
    await asyncio.sleep(RESTART_GRACE_S)
    if restart_accepted_at != stamp:
        return
    restart_accepted_at = None
    logger.warning("memory %s: no restart %.0f s after it was accepted",
                   _restart_kind, RESTART_GRACE_S)
    await _resume()
    await _say(_notify, RESTART_FAILED_LINES[_restart_kind])


async def _resume() -> None:
    global _resume_updates
    resume, _resume_updates = _resume_updates, None
    if resume is not None:
        try:
            await resume()
        except Exception:  # noqa: BLE001 — logged; a rebuild restarts intake
            logger.warning("resuming updates failed", exc_info=True)


async def _say(send: Callable[[str], Awaitable[object]] | None,
               text: str) -> None:
    if send is None:
        logger.warning("no way to tell the operator: %s", text)
        return
    try:
        await send(text)
    except Exception:  # noqa: BLE001 — one attempt, logged
        logger.warning("could not tell the operator: %s", text, exc_info=True)


# ---------------------------------------------------------------------------
# casa-main: notification-only mode
# ---------------------------------------------------------------------------

async def notification_only(
    intent: dict, *, root: str = "/", proc_root: str = "/proc",
    send: Callable[[str], Awaitable[object]] | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """A recorded attempt means memory may be partly deleted: nothing that
    reads memory starts. The operator gets one line per container start,
    recorded before it is sent and never retried, and the process idles
    until it is told to stop, so s6 does not respawn it into a loop.

    Nothing here raises: a line that cannot be recorded is not sent (so a
    failing write can never flood the operator), the reason is logged, and
    the process idles all the same; the next start tries again."""
    try:
        line = notification_line(intent)
    except Exception:  # noqa: BLE001 — a malformed record still idles
        line = failed_line(intent.get("kind"), UNREADABLE_REASON)
    try:
        cid = _container_id(proc_root)
        notified = intent.setdefault("notified_container_ids", [])
        if cid not in notified:
            notified.append(cid)
            await asyncio.to_thread(write_intent, intent, root)
            await _say(send or send_operator_line, line)
    except Exception as exc:  # noqa: BLE001 — logged; never a crash loop
        logger.error("memory maintenance: could not record the notice, so "
                     "it was not sent (%s)", exc)
    logger.error("memory maintenance pending (%s); Casa is not taking "
                 "messages until the app restarts", line)
    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
    await stop.wait()


async def send_operator_line(text: str) -> bool:
    """One raw Bot API ``sendMessage`` to the configured operator chat: no
    channel object, no polling, no update consumed."""
    import aiohttp

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.warning("no Telegram operator chat configured; not sent")
        return False
    base = (os.environ.get("TELEGRAM_BOT_API_BASE", "").strip()
            or "https://api.telegram.org")
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{base}/bot{token}/sendMessage",
                                json={"chat_id": chat_id, "text": text}) as r:
            if r.status != 200:
                logger.warning("operator line not accepted (HTTP %s)",
                               r.status)
            return r.status == 200


# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="[memory-maintenance] %(levelname)s %(message)s")
    logger.info("memory maintenance: start")
    try:
        run()
    except BaseException:  # noqa: BLE001 — a failure never fails the boot
        logger.exception("memory maintenance: unexpected failure")
    return 0


if __name__ == "__main__":
    sys.exit(main())
