"""Approval records: what the operator approved, frozen, and spent once.

An approval card asks the operator about one action — an install, an upgrade,
a persona install — and the exact settings it will stage. The record keeps
those settings (``values``) and the identity of what was inspected
(``consent_identity``), so the run Casa carries on after Approve commits
exactly what the card showed.

Each record belongs to a component (``component_key``: the component id and
slug, or the persona id) and carries that component's next **generation**; the
record with the highest generation is the component's **current** one. Only the
current record can be approved or used.

The states::

    pending ──approve──▶ approved ──attempt──▶ used
       │                    │
       ├──deny──▶ denied    └──(newer request)──▶ superseded
       ├──expire──▶ expired
       └──(newer request)──▶ superseded

``used``, ``denied``, ``expired`` and ``superseded`` are terminal. One tap is one
attempt: an approval is used up by the first governed library call of its run,
immediately before that call, whatever the call's outcome; nothing records the
outcome here — the run's own result carries it.

Every function is synchronous: a transition checks and writes with no ``await``
in between, so transitions are atomic against each other on the event loop.
:func:`request` and :func:`attempt` run under the plugin-tools lock, which the
governed tools hold from the attempt through the library write, so a newer
request can never replace an approval between its use and its write. Each
transition persists before it returns and changes memory only after the write
succeeded; a write that fails raises :class:`ApprovalStoreError` and changes
nothing.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Literal

from canonical_bytes import checksum_json
from channels.tg_address import TgAddress
from provenance import APPROVAL_KEY  # noqa: F401 — re-exported: the origin key naming a run's approval

logger = logging.getLogger(__name__)

PATH = "/data/approval-records.json"
SCHEMA_VERSION = 1

Kind = Literal["specialist_install", "specialist_upgrade", "persona_install"]
State = Literal["pending", "approved", "used", "superseded", "denied", "expired"]
KINDS = frozenset({"specialist_install", "specialist_upgrade", "persona_install"})
STATES = frozenset({"pending", "approved", "used", "superseded", "denied", "expired"})
TERMINAL = frozenset({"used", "superseded", "denied", "expired"})

ASKED_MAX = 500
#: a terminal record older than this is dropped at load (the component's
#: current record is kept, so its generations keep counting up)
PRUNE_AFTER_S = 30 * 24 * 3600.0


@dataclass(frozen=True)
class ApprovalRecord:
    rid: str
    kind: Kind
    component_key: str
    consent_identity: str
    generation: int
    values: dict                    # install/upgrade: the effective {"config", "secrets"}
    values_digest: str
    state: State
    card: "TgAddress | None"
    asked: str                      # the operator's words that led to the request
    used_at: "float | None"         # when its one attempt began
    created_at: float
    updated_at: float


class ApprovalStoreError(RuntimeError):
    """The store could not persist a transition; nothing changed."""


LINES = {"superseded": "this approval was replaced by a newer request; nothing was installed",
         "used": "this approval was already used for an attempt; ask again for a new approval",
         "unknown": "this approval could not be read; nothing was installed"}


class ApprovalNotCurrent(RuntimeError):
    """Raised by the configurator lease when a run's approval is no longer startable."""

    def __init__(self, verdict: str) -> None:
        self.verdict = verdict      # "superseded" | "used" | "unknown"
        super().__init__(LINES.get(verdict, LINES["unknown"]))


USED_DETAIL = "this approval was already used for an attempt; ask again"
OTHER_KIND_DETAIL = "this approval was for a different action"
OTHER_IDENTITY_DETAIL = "the approved request was for another artifact or other values"
UNREADABLE_DETAIL = "this approval could not be read; ask again"

_LOCK = threading.Lock()
_RECORDS: "dict[str, ApprovalRecord] | None" = None


def values_digest(values: dict) -> str:
    """The digest of an approval's values: canonical JSON, so key order and
    container types never change it."""
    return checksum_json(values)


# -- persistence --------------------------------------------------------------

def _to_json(r: ApprovalRecord) -> dict:
    return {"rid": r.rid, "kind": r.kind, "component_key": r.component_key,
            "consent_identity": r.consent_identity, "generation": r.generation,
            "values": r.values, "values_digest": r.values_digest, "state": r.state,
            "card": r.card.to_json() if r.card is not None else None,
            "asked": r.asked, "used_at": r.used_at,
            "created_at": r.created_at, "updated_at": r.updated_at}


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_time(v: object) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and v == v and abs(v) != float("inf"))


def _nonempty(v: object) -> bool:
    return isinstance(v, str) and bool(v)


def _from_json(d: object) -> ApprovalRecord:
    if not isinstance(d, dict):
        raise ValueError("a record is not an object")
    if not (_nonempty(d.get("rid")) and d.get("kind") in KINDS
            and _nonempty(d.get("component_key")) and _nonempty(d.get("consent_identity"))
            and _is_int(d.get("generation")) and d["generation"] >= 1
            and isinstance(d.get("values"), dict) and _nonempty(d.get("values_digest"))
            and d.get("state") in STATES and isinstance(d.get("asked"), str)
            and (d.get("used_at") is None or _is_time(d.get("used_at")))
            and _is_time(d.get("created_at")) and _is_time(d.get("updated_at"))):
        raise ValueError(f"a record has a missing or malformed field: {d.get('rid')!r}")
    if values_digest(d["values"]) != d["values_digest"]:
        raise ValueError(f"record {d['rid']!r}: its values do not match their digest")
    if (d["state"] == "used") != (d["used_at"] is not None):
        raise ValueError(f"record {d['rid']!r}: used_at does not match its state")
    card = d.get("card")
    return ApprovalRecord(
        rid=d["rid"], kind=d["kind"], component_key=d["component_key"],
        consent_identity=d["consent_identity"], generation=d["generation"],
        values=d["values"], values_digest=d["values_digest"], state=d["state"],
        card=TgAddress.from_json(card) if card is not None else None,
        asked=d["asked"], used_at=d["used_at"],
        created_at=float(d["created_at"]), updated_at=float(d["updated_at"]))


def _set_aside_corrupt(path: str, why: str) -> None:
    logger.warning("approval records at %s are unreadable (%s); set aside as %s.corrupt, "
                   "starting empty", path, why, path)
    try:
        os.replace(path, path + ".corrupt")
    except OSError:
        logger.warning("could not set aside %s", path, exc_info=True)


def _prune(records: "dict[str, ApprovalRecord]", now: float) -> "dict[str, ApprovalRecord]":
    top: dict[str, ApprovalRecord] = {}
    for r in records.values():
        t = top.get(r.component_key)
        if t is None or r.generation > t.generation:
            top[r.component_key] = r
    keep_rids = {r.rid for r in top.values()}
    return {rid: r for rid, r in records.items()
            if rid in keep_rids or r.state not in TERMINAL
            or now - r.updated_at <= PRUNE_AFTER_S}


def _read(path: str) -> "dict[str, ApprovalRecord]":
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        _set_aside_corrupt(path, f"{type(exc).__name__}: {exc}")
        return {}
    try:
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION \
                or not isinstance(raw.get("records"), list):
            raise ValueError("not an approval-records document")
        out: dict[str, ApprovalRecord] = {}
        for d in raw["records"]:
            r = _from_json(d)
            if r.rid in out:
                raise ValueError(f"record {r.rid!r} appears twice")
            out[r.rid] = r
    except (ValueError, TypeError) as exc:
        _set_aside_corrupt(path, str(exc))
        return {}
    return _prune(out, time.time())


def _records() -> "dict[str, ApprovalRecord]":
    """Caller holds ``_LOCK``."""
    global _RECORDS
    if _RECORDS is None:
        _RECORDS = _read(PATH)
    return _RECORDS


def _commit(candidate: "dict[str, ApprovalRecord]") -> None:
    """Persist *candidate*, then make it the store's state. Caller holds ``_LOCK``."""
    global _RECORDS
    from atomic_io import PRIVATE, atomic_write_json
    doc = {"schema_version": SCHEMA_VERSION,
           "records": [_to_json(r) for r in sorted(candidate.values(),
                                                    key=lambda r: (r.component_key,
                                                                   r.generation))]}
    try:
        os.makedirs(os.path.dirname(PATH) or ".", exist_ok=True)
        atomic_write_json(PATH, doc, mode=PRIVATE, sort_keys=True)
    except Exception as exc:  # noqa: BLE001 — every write failure is the store's one error
        raise ApprovalStoreError(f"could not save the approval records: {exc}") from exc
    _RECORDS = candidate


def reset() -> None:
    """Forget the in-memory state; the next call reads ``PATH`` again."""
    global _RECORDS
    with _LOCK:
        _RECORDS = None


# -- the plugin-tools lock ------------------------------------------------------

def _require_plugin_tools_lock(what: str) -> None:
    import tools
    if not tools._PLUGIN_TOOLS_LOCK.locked():
        raise AssertionError(f"approval_records.{what} must run under the plugin-tools lock")


# -- reads ----------------------------------------------------------------------

def _current_of(records: "dict[str, ApprovalRecord]", component_key: str
                ) -> "ApprovalRecord | None":
    best: "ApprovalRecord | None" = None
    for r in records.values():
        if r.component_key == component_key and (best is None or r.generation > best.generation):
            best = r
    return best


def current(component_key: str) -> "ApprovalRecord | None":
    """The component's record with the highest generation."""
    with _LOCK:
        return _current_of(_records(), component_key)


def get(rid: str) -> "ApprovalRecord | None":
    with _LOCK:
        return _records().get(rid)


def _origin_record(records: "dict[str, ApprovalRecord]", origin_approval: object
                   ) -> "ApprovalRecord | None":
    """The record an origin's ``_approval`` value names, or ``None`` when the value
    is malformed or names no record of that generation."""
    if not isinstance(origin_approval, dict):
        return None
    rid, gen = origin_approval.get("rid"), origin_approval.get("generation")
    if not _nonempty(rid) or not _is_int(gen):
        return None
    r = records.get(rid)
    if r is None or r.generation != gen:
        return None
    return r


def _refusal(records: "dict[str, ApprovalRecord]", origin_approval: object, *,
             kind: str, consent_identity: str) -> "tuple[dict | None, ApprovalRecord | None]":
    if origin_approval is None:
        return {"ok": False, "kind": "approval_required"}, None
    r = _origin_record(records, origin_approval)
    if r is None:
        return {"ok": False, "kind": "approval_required", "detail": UNREADABLE_DETAIL}, None
    if r.state == "used":
        return {"ok": False, "kind": "approval_used", "detail": USED_DETAIL}, r
    cur = _current_of(records, r.component_key)
    if r.state != "approved" or cur is None or cur.rid != r.rid:
        return {"ok": False, "kind": "approval_superseded"}, r
    if r.kind != kind:
        return {"ok": False, "kind": "approval_required", "detail": OTHER_KIND_DETAIL}, r
    if r.consent_identity != consent_identity:
        return {"ok": False, "kind": "approval_required", "detail": OTHER_IDENTITY_DETAIL}, r
    return None, r


def startable(rid: str, generation: int) -> str:
    """``"ok"`` while the approval is approved and its component's current
    record; else ``"used"``, ``"superseded"`` or ``"unknown"``. A read."""
    with _LOCK:
        records = _records()
        r = _origin_record(records, {"rid": rid, "generation": generation})
        if r is None:
            return "unknown"
        if r.state == "used":
            return "used"
        cur = _current_of(records, r.component_key)
        if r.state == "approved" and cur is not None and cur.rid == r.rid:
            return "ok"
        return "superseded"


def fence(origin_approval: "dict | None", *, kind: Kind,
          consent_identity: str) -> "dict | None":
    """A governed tool's early refusal at entry, or ``None`` when its run's
    approval would let it proceed. A read; :func:`attempt` is what uses it."""
    with _LOCK:
        refusal, _ = _refusal(_records(), origin_approval, kind=kind,
                              consent_identity=consent_identity)
        return refusal


def approved_values(origin_approval: "dict | None") -> "dict | None":
    """The values the run's approval froze, or ``None`` when it names none."""
    with _LOCK:
        r = _origin_record(_records(), origin_approval)
        return copy.deepcopy(r.values) if r is not None else None


# -- transitions ----------------------------------------------------------------

def _json_copy(values: dict) -> dict:
    if not isinstance(values, dict):
        raise TypeError("approval values are a dict")
    return json.loads(json.dumps(values))


def request(kind: Kind, component_key: str, consent_identity: str, values: dict, *,
            asked: str = "", carrying_rid: str = ""
            ) -> "tuple[ApprovalRecord, str, ApprovalRecord | None]":
    """Ask for an approval of *kind* for *component_key*: returns ``(record,
    outcome, superseded)``.

    The component's current record of the same kind and identity that is
    already approved is returned as it is — ``"in_progress"`` when the asking
    run is the one Casa started for it (*carrying_rid*), ``"already_approved"``
    otherwise — and nothing is written. Anything else creates a ``pending``
    record of the next generation (``"new"``), and a pending or approved current
    record is superseded in the same write (``"superseded_previous"``, with that
    record as it now is)."""
    _require_plugin_tools_lock("request")
    if kind not in KINDS:
        raise ValueError(f"not an approval kind: {kind!r}")
    if not _nonempty(component_key) or not _nonempty(consent_identity):
        raise ValueError("an approval needs a component key and a consent identity")
    frozen = _json_copy(values)
    digest = values_digest(frozen)
    with _LOCK:
        records = _records()
        cur = _current_of(records, component_key)
        if (cur is not None and cur.kind == kind and cur.consent_identity == consent_identity
                and cur.state == "approved"):
            outcome = "in_progress" if carrying_rid and carrying_rid == cur.rid \
                else "already_approved"
            return cur, outcome, None
        now = time.time()
        new = ApprovalRecord(
            rid=uuid.uuid4().hex, kind=kind, component_key=component_key,
            consent_identity=consent_identity,
            generation=(cur.generation + 1) if cur is not None else 1,
            values=frozen, values_digest=digest, state="pending", card=None,
            asked=str(asked or "")[:ASKED_MAX], used_at=None,
            created_at=now, updated_at=now)
        candidate = dict(records)
        candidate[new.rid] = new
        superseded = None
        if cur is not None and cur.state in ("pending", "approved"):
            superseded = replace(cur, state="superseded", updated_at=now)
            candidate[cur.rid] = superseded
        _commit(candidate)
        return new, ("superseded_previous" if superseded is not None else "new"), superseded


def set_card(rid: str, addr: TgAddress) -> ApprovalRecord:
    """Record the card that asks about *rid*; returns the record as it now is
    (it may already be superseded by a newer request)."""
    if not isinstance(addr, TgAddress):
        raise TypeError("a card is a TgAddress")
    with _LOCK:
        records = _records()
        r = records.get(rid)
        if r is None:
            raise KeyError(rid)
        updated = replace(r, card=addr, updated_at=time.time())
        _commit({**records, rid: updated})
        return updated


def _move(rid: str, to: str, *, require_current: bool) -> "ApprovalRecord | None":
    with _LOCK:
        records = _records()
        r = records.get(rid)
        if r is None or r.state != "pending":
            return None
        if require_current:
            cur = _current_of(records, r.component_key)
            if cur is None or cur.rid != rid:
                return None
        updated = replace(r, state=to, updated_at=time.time())
        _commit({**records, rid: updated})
        return updated


def approve(rid: str) -> "ApprovalRecord | None":
    """pending and current → approved; ``None`` (nothing written) otherwise."""
    return _move(rid, "approved", require_current=True)


def deny(rid: str) -> None:
    """pending → denied; nothing otherwise."""
    _move(rid, "denied", require_current=False)


def expire(rid: str) -> None:
    """pending → expired; nothing otherwise."""
    _move(rid, "expired", require_current=False)


def attempt(origin_approval: "dict | None", *, kind: Kind,
            consent_identity: str) -> "dict | None":
    """Use the run's approval for one attempt: the same checks as :func:`fence`;
    when they pass, approved → used in one write and ``None`` is returned, and
    the caller makes its library call. Otherwise the refusal, nothing written.
    Called under the plugin-tools lock, immediately before the library call."""
    _require_plugin_tools_lock("attempt")
    with _LOCK:
        records = _records()
        refusal, r = _refusal(records, origin_approval, kind=kind,
                              consent_identity=consent_identity)
        if refusal is not None:
            return refusal
        now = time.time()
        _commit({**records, r.rid: replace(r, state="used", used_at=now, updated_at=now)})
        return None
