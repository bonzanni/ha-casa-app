"""The operator-message journal of a background job, and the transcript check
that settles it.

An operator message written into a job's session must survive a Casa restart,
a Casa crash or the death of the job's CLI process until the session's own
transcript holds it. Each such message is journaled before it is admitted:
one JSON file per job, ``<root>/<engagement id>.json``, rewritten atomically
(temp file, fsync, rename) on every change. An entry is

    {"uuid": ..., "text": ..., "notice": {...} | null, "taken_up": bool}

where ``uuid`` is the id the message is written with, ``text`` what is
written, and ``notice`` where a lost-message notice would be a reply to.

Every call here is SYNCHRONOUS on purpose: the delivery seams journal, admit
and enqueue in one step with nothing awaited, so a slow resume can never
delay the next message being accepted. The writes are small.

The transcript is the authority on what a session read. The CLI records a
written message's uuid in the session transcript only when it takes the
message into a turn, about 0.1 s after take-up (measured on the pinned CLI;
the line written when the message is queued carries no uuid). The shapes:

- a message that runs as its own turn: a ``user`` line whose ``uuid`` is ours;
- a message absorbed mid-turn: an ``attachment`` of type ``queued_command``
  whose ``source_uuid`` is ours, and a ``queue-operation`` ``remove`` line
  whose ``commandUuid`` is ours.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

INBOX_ROOT = Path("/data/job-inbox")


class JournalError(RuntimeError):
    """The journal could not be written; the message was not accepted."""


def path_for(engagement_id: str) -> Path:
    return INBOX_ROOT / f"{engagement_id}.json"


def load(engagement_id: str) -> list[dict]:
    """The job's entries in arrival order; ``[]`` when there is no file. An
    unreadable file is logged and read as empty."""
    path = path_for(engagement_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logger.warning("job inbox %s unreadable: %s", path, exc)
        return []
    return [e for e in data if isinstance(e, dict) and isinstance(e.get("uuid"), str)] \
        if isinstance(data, list) else []


def _write(engagement_id: str, entries: list[dict]) -> None:
    from atomic_io import PRIVATE, atomic_write_json
    path = path_for(engagement_id)
    try:
        if not entries:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, entries, indent=None, mode=PRIVATE,
                          prefix=f".{engagement_id}.")
    except OSError as exc:
        raise JournalError(f"job inbox {path} not written: {exc}") from exc


def append(engagement_id: str, *, uuid: str, text: str,
           notice: dict | None) -> None:
    """Commit one entry. Raises :class:`JournalError` when it could not be
    written, in which case nothing was accepted."""
    entries = load(engagement_id)
    entries.append({"uuid": uuid, "text": text, "notice": notice,
                    "taken_up": False})
    _write(engagement_id, entries)


def mark_taken_up(engagement_id: str, uuid: str) -> None:
    """The session took the message into a turn. The entry stays until that
    turn has ended, by when the transcript certainly holds it."""
    entries = load(engagement_id)
    changed = False
    for entry in entries:
        if entry["uuid"] == uuid and not entry.get("taken_up"):
            entry["taken_up"] = True
            changed = True
    if changed:
        _write_logged(engagement_id, entries)


def remove(engagement_id: str, uuids: Iterable[str]) -> None:
    drop = set(uuids)
    if not drop:
        return
    entries = load(engagement_id)
    kept = [e for e in entries if e["uuid"] not in drop]
    if len(kept) != len(entries):
        _write_logged(engagement_id, kept)


def journaled_ids() -> list[str]:
    """Every engagement id that still has a journal file, sorted."""
    try:
        return sorted(p.stem for p in INBOX_ROOT.glob("*.json"))
    except OSError as exc:
        logger.warning("job inbox %s not listable: %s", INBOX_ROOT, exc)
        return []


def discard(engagement_id: str) -> None:
    """The job's file goes with its record."""
    _write_logged(engagement_id, [])


def _write_logged(engagement_id: str, entries: list[dict]) -> None:
    """A write after acceptance: a failure keeps the older file, whose extra
    entries the next reconcile settles against the transcript."""
    try:
        _write(engagement_id, entries)
    except JournalError as exc:
        logger.warning("%s", exc)


def transcript_path(cwd: str | None, session_id: str | None) -> Path | None:
    """The session's transcript file, resolved by the SDK's own cwd → project
    dir helpers; ``None`` when it cannot be resolved."""
    if not cwd or not session_id:
        return None
    try:
        from claude_agent_sdk._internal.sessions import (
            _canonicalize_path, _find_project_dir)
    except ImportError as exc:
        logger.warning("transcript lookup unavailable: %s", exc)
        return None
    project = _find_project_dir(_canonicalize_path(cwd))
    if project is None:
        return None
    return Path(project) / f"{session_id}.jsonl"


def uuids_in_transcript(path: Path | None) -> set[str]:
    """Every host uuid the transcript records as taken into a turn. A missing
    or unreadable transcript holds nothing."""
    found: set[str] = set()
    if path is None:
        return found
    try:
        handle = open(path, encoding="utf-8")
    except OSError:
        return found
    with handle:
        for line in handle:
            try:
                obj: Any = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            if kind == "user" and isinstance(obj.get("uuid"), str):
                found.add(obj["uuid"])
            elif kind == "attachment":
                attachment = obj.get("attachment")
                if (isinstance(attachment, dict)
                        and attachment.get("type") == "queued_command"
                        and isinstance(attachment.get("source_uuid"), str)):
                    found.add(attachment["source_uuid"])
            elif (kind == "queue-operation" and obj.get("operation") == "remove"
                    and isinstance(obj.get("commandUuid"), str)):
                found.add(obj["commandUuid"])
    return found

