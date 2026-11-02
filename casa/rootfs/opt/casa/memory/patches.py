"""Id-addressed patches to one ledger, and the ledger's reachability.

A writer job never rewrites a ledger: its model answers with a list of
operations, each naming the object it changes by id, and :func:`apply` runs
them inside the job's commit transaction. Every changed object gets its next
revision in ``objects`` and a row in ``revisions`` carrying the operation,
the job and the commit's ``commit_no``. The operations are:

* ``add``: a new entry, group or topic (its id is minted here; a ``ref``
  lets later operations in the same answer name it as ``@<ref>``);
* ``replace``: new current text for an object (the model keeps the old value
  in the text as ``earlier:``);
* ``retire``: the object is no longer current;
* ``split``: new objects from one, which is retired;
* ``merge``: one new object from several, which are retired;
* ``add_cue``: cues added to an object (cues are add-only; nothing removes
  one);
* ``set_line``: an object's need line or label;
* ``move``: an object's parent.

Every operation acts on objects of the job's own ledger only: an id that is
not an object of that ledger is refused, as is any malformed operation
(:class:`PatchError`), which fails the job. Whether an operation is right is
the model's judgement; this module checks only that it is well-formed.

Every object written keeps only the sources its ledger can read (M6,
INV-MEM-029): below ``private``, a source id is kept only when it names an
event classified at or below the ledger, the rule of the pending view. An
id naming an event the job was never given (a ``private`` one, one left
provisional, a ``seq`` no event holds yet, which a later event could take,
or a value that cannot be a ``seq`` at all, such as an integer beyond
SQLite's range) is dropped, before anything is bound, not refused: the model never saw that event, so a cite it
invented costs the object that reference and nothing else. One warning per
commit counts what was dropped. The ``private`` ledger reads every event,
and keeps every id it cites.

The commit's reachability check is :func:`memory.view.unreachable`: every
current object must hang from current parents of the same ledger, each a
level above it (entry, then group, then topic), or sit at the top.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Any

from memory import pending

logger = logging.getLogger(__name__)

OPS = ("add", "replace", "retire", "split", "merge", "add_cue", "set_line",
       "move")
KINDS = ("entry", "group", "topic")
ENTRY_KINDS = ("fact", "errand", "episode")
ERRAND_STATES = ("open", "done", "dropped")
#: Fields an ``add`` (or a split or merge target) may set.
_SPEC_FIELDS = ("kind", "entry_kind", "errand_state", "topic", "subject",
                "body", "line", "cues", "parent", "source_seqs")
#: Fields a ``replace`` may change. Cues and sources only grow.
_REPLACE_FIELDS = ("entry_kind", "errand_state", "topic", "subject", "body",
                   "line")
_COLUMNS = ("kind", "entry_kind", "errand_state", "topic", "subject", "body",
            "line", "cues", "parent", "origin", "source_seqs", "status")


class PatchError(ValueError):
    """A malformed operation, or one naming an object outside the ledger."""


def _new_id() -> str:
    return f"o{uuid.uuid4().hex[:16]}"


def _str_list(value: Any, what: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PatchError(f"{what} must be a list")
    return value


def _union(old: list, new: list) -> list:
    return old + [v for v in new if v not in old]


class _Ledger:
    """One commit's view of a ledger: loads, writes and ref resolution."""

    def __init__(self, conn: sqlite3.Connection, ledger: str, *, job_id: str,
                 commit_no: int, committed_at: str) -> None:
        self.conn = conn
        self.ledger = ledger
        self.job_id = job_id
        self.commit_no = commit_no
        self.committed_at = committed_at
        self.refs: dict[str, str] = {}
        self.touched: list[str] = []
        #: Source ids dropped as unreadable at this ledger, and the objects
        #: written without them.
        self.dropped = 0
        self.trimmed: set[str] = set()

    def readable_sources(self, object_id: str, seqs: list) -> list:
        """*seqs* without the ids this ledger cannot read: below
        ``private``, an id is kept only when it names an event classified at
        or below the ledger."""
        if self.ledger == "private" or not seqs:
            return seqs
        ids = {s for s in seqs if pending.is_seq(s)}
        readable: set[int] = set()
        ordered = sorted(ids)
        for i in range(0, len(ordered), 500):
            chunk = ordered[i:i + 500]
            readable.update(r[0] for r in self.conn.execute(
                f"SELECT seq FROM events WHERE seq IN "
                f"({', '.join('?' for _ in chunk)}) "
                f"AND {_TIER_RANK} <= ? AND tier_status = 'classified'",
                (*chunk, pending.rank(self.ledger))))
        kept = [s for s in seqs if pending.is_seq(s) and s in readable]
        if len(kept) != len(seqs):
            self.dropped += len(seqs) - len(kept)
            self.trimmed.add(object_id)
        return kept

    def resolve(self, value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise PatchError(f"not an object id: {value!r}")
        if value.startswith("@"):
            try:
                return self.refs[value[1:]]
            except KeyError:
                raise PatchError(f"unknown ref {value!r}") from None
        return value

    def load(self, object_id: str, *, current: bool = True) -> dict:
        self.conn.row_factory = sqlite3.Row
        try:
            row = self.conn.execute(
                "SELECT * FROM objects WHERE ledger = ? AND object_id = ?",
                (self.ledger, object_id)).fetchone()
        finally:
            self.conn.row_factory = None
        if row is None:
            raise PatchError(f"no object {object_id!r} in this ledger")
        obj = dict(row)
        if current and obj["status"] != "current":
            raise PatchError(f"object {object_id!r} is retired")
        obj["cues"] = json.loads(obj["cues"])
        obj["source_seqs"] = json.loads(obj["source_seqs"])
        return obj

    def write(self, obj: dict, op: str, reason: str | None) -> None:
        obj["source_seqs"] = self.readable_sources(
            obj["object_id"], obj.get("source_seqs") or [])
        obj["revision"] = obj.get("revision", 0) + 1
        row = {c: obj.get(c) for c in _COLUMNS}
        row["cues"] = json.dumps(obj.get("cues") or [], ensure_ascii=False)
        row["source_seqs"] = json.dumps(obj.get("source_seqs") or [])
        cols = ("ledger", "object_id", "revision", *_COLUMNS)
        vals = (self.ledger, obj["object_id"], obj["revision"],
                *(row[c] for c in _COLUMNS))
        self.conn.execute(
            f"INSERT INTO objects ({', '.join(cols)}) VALUES "
            f"({', '.join('?' for _ in cols)}) ON CONFLICT(ledger, object_id) "
            "DO UPDATE SET "
            + ", ".join(f"{c} = excluded.{c}" for c in cols[2:]), vals)
        self.conn.execute(
            f"INSERT INTO revisions ({', '.join(cols)}, op, reason, job_id, "
            "committed_at, commit_no) VALUES "
            f"({', '.join('?' for _ in cols)}, ?, ?, ?, ?, ?)",
            (*vals, op, reason, self.job_id, self.committed_at,
             self.commit_no))
        self.touched.append(obj["object_id"])

    def add(self, spec: dict, op: str, reason: str | None,
            origin: list[str] | None = None) -> str:
        if not isinstance(spec, dict):
            raise PatchError("an object spec must be an object")
        unknown = set(spec) - set(_SPEC_FIELDS) - {"ref", "op", "reason"}
        if unknown:
            raise PatchError(f"unknown fields {sorted(unknown)}")
        kind = spec.get("kind", "entry")
        if kind not in KINDS:
            raise PatchError(f"unknown kind {kind!r}")
        obj = {f: spec.get(f) for f in _SPEC_FIELDS}
        obj["kind"] = kind
        _check_entry_fields(obj)
        obj["cues"] = _str_list(spec.get("cues"), "cues")
        obj["source_seqs"] = _str_list(spec.get("source_seqs"), "source_seqs")
        if obj["parent"] is not None:
            obj["parent"] = self.resolve(obj["parent"])
        obj["origin"] = json.dumps(origin) if origin else None
        obj["status"] = "current"
        obj["object_id"] = _new_id()
        ref = spec.get("ref")
        if ref is not None:
            if not isinstance(ref, str) or not ref or ref in self.refs:
                raise PatchError(f"bad or repeated ref {ref!r}")
            self.refs[ref] = obj["object_id"]
        self.write(obj, op, reason)
        return obj["object_id"]


#: An event's tier rank, as SQL.
_TIER_RANK = ("(CASE tier WHEN 'public' THEN 0 WHEN 'friends' THEN 1 "
              "WHEN 'family' THEN 2 ELSE 3 END)")


def _check_entry_fields(obj: dict) -> None:
    if obj["kind"] == "entry":
        if obj.get("entry_kind") not in ENTRY_KINDS:
            raise PatchError(f"unknown entry_kind {obj.get('entry_kind')!r}")
    elif obj.get("entry_kind") is not None:
        raise PatchError("only an entry has an entry_kind")
    state = obj.get("errand_state")
    if obj.get("entry_kind") == "errand":
        if state is None:
            obj["errand_state"] = "open"
        elif state not in ERRAND_STATES:
            raise PatchError(f"unknown errand_state {state!r}")
    elif state is not None:
        raise PatchError("only an errand has an errand_state")


def apply(conn: sqlite3.Connection, ledger: str, ops: list, *, job_id: str,
          commit_no: int, committed_at: str) -> list[str]:
    """Run *ops* against *ledger* inside the caller's write transaction.
    Returns the ids of every object written, in order."""
    if not isinstance(ops, list):
        raise PatchError("ops must be a list")
    led = _Ledger(conn, ledger, job_id=job_id, commit_no=commit_no,
                  committed_at=committed_at)
    for op in ops:
        if not isinstance(op, dict) or op.get("op") not in OPS:
            raise PatchError(f"unknown operation {op!r}")
        name = op["op"]
        reason = op.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise PatchError("reason must be text")
        if name == "add":
            led.add(op, "add", reason)
        elif name == "split":
            target = led.load(led.resolve(op.get("object_id")))
            into = _str_list(op.get("into"), "into")
            if not into:
                raise PatchError("split needs at least one target")
            for spec in into:
                led.add(spec, "split", reason, origin=[target["object_id"]])
            target["status"] = "retired"
            led.write(target, "split", reason)
        elif name == "merge":
            ids = [led.resolve(v)
                   for v in _str_list(op.get("object_ids"), "object_ids")]
            if len(set(ids)) < 2:
                raise PatchError("merge needs at least two objects")
            sources = [led.load(i) for i in ids]
            led.add(op.get("into"), "merge", reason, origin=ids)
            for obj in sources:
                obj["status"] = "retired"
                led.write(obj, "merge", reason)
        else:
            obj = led.load(led.resolve(op.get("object_id")))
            if name == "replace":
                changed = {f: op[f] for f in _REPLACE_FIELDS if f in op}
                if not changed:
                    raise PatchError("replace changes nothing")
                obj.update(changed)
                _check_entry_fields(obj)
                obj["cues"] = _union(obj["cues"],
                                     _str_list(op.get("cues"), "cues"))
                obj["source_seqs"] = _union(
                    obj["source_seqs"],
                    _str_list(op.get("source_seqs"), "source_seqs"))
            elif name == "retire":
                obj["status"] = "retired"
            elif name == "add_cue":
                cues = _str_list(op.get("cues"), "cues")
                if not cues:
                    raise PatchError("add_cue adds nothing")
                obj["cues"] = _union(obj["cues"], cues)
            elif name == "set_line":
                if not isinstance(op.get("line"), str):
                    raise PatchError("set_line needs a line")
                obj["line"] = op["line"]
            elif name == "move":
                parent = op.get("parent")
                obj["parent"] = None if parent is None else led.resolve(parent)
            led.write(obj, name, reason)
    if led.dropped:
        logger.warning(
            "memory patches: job %s dropped %d source ref(s) the %s ledger "
            "cannot read, from %d object(s)", job_id, led.dropped, ledger,
            len(led.trimmed))
    return led.touched

