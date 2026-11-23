"""Persistent operator-consent acks for plugin-declared event subscriptions,
and the operator's denials of them.

One ack = the operator approved exactly one consent IDENTITY
(:func:`plugin_events.ack_identity` — subscriber + subscriber artifact id +
emitter + event + declaration digest + sorted delivery targets). Unlike a
callback ack, an event subscription reaches into a subscriber role/target
the operator must approve, so the identity folds in the subscriber's
artifact id (a plugin upgrade mints a new identity — no consent carries
over silently) and the delivery target set (retargeting a subscription is a
new consent question) — mirroring ``trigger_acks.py`` more than
``callback_acks.py`` on that count, while keeping ``callback_acks.py``'s
exact structure otherwise (read whole before touching this file — its
idioms are mirrored here):

* **Atomic** — every mutation persists via ``atomic_io.atomic_write_text``
  (sidecar + fsync + ``os.replace``); a crash mid-write can never leave a
  half-written store that later parses into unintended consent.
* **Fail-closed** — a missing, unreadable, or malformed store means NO acks
  (subscriptions stay undelivered); it never raises into the reconciler.
  The next successful ``record`` rewrites a valid store.
* **Thread-safe** — a ``threading.Lock`` guards state: ``record`` runs on
  the event loop (Telegram approve callback), revocation runs from the
  plugin lifecycle path, and pruning runs from the reconciler.

**Denials** (#1666). The same file keeps, in a separate ``denied`` section,
the identities the operator turned off — a Deny on the consent card, or
``event_ack_revoke`` — so the answer survives a restart. A denial is never an
ack: the store holds the two in separate maps, :meth:`EventAckStore.get` reads
approvals only, and no identity may sit in both. Its one use is
:meth:`EventAckStore.is_denied`, which tells the reconciler that a
subscription with no ack was answered rather than left waiting. An Approve
clears it, as does the subscriber's removal or an update to a new artifact. A
store with no ``denied`` section has no denials; a malformed section fails the
whole store, acks included.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ACKS_PATH = Path(os.environ.get("CASA_EVENT_ACKS_PATH", "/data/event_acks.json"))

_SCHEMA_VERSION = 1

#: Read cap for the store file. A well-behaved store is a few KiB; anything
#: past this is refused wholesale (fail closed) and never slurped into memory —
#: the read itself is bounded so a multi-GB file cannot OOM the boot.
_MAX_ACKS_BYTES = 4 * 1024 * 1024

#: The EXACT key set a stored ack record may carry. A record with any key
#: outside this set is malformed and fails the whole store: a hand-edited /
#: merged file that smuggled an extra field is no more trustworthy than one
#: with a bad identity.
_ACK_RECORD_KEYS = frozenset(
    {"subscriber", "artifact_id", "emitter", "event", "digest", "targets",
     "gen", "ts"})

#: A stored denial record is an ack record without the approval generation.
_DENIAL_RECORD_KEYS = _ACK_RECORD_KEYS - {"gen"}


def _valid_ts(ts: Any) -> bool:
    """True iff *ts* is a real timestamp number this store may trust.

    ``_load`` runs at construction (Casa boot), so this check — like every
    other in ``_load`` — must be TOTAL: it may never raise, or a garbage
    stored record would crash the boot instead of failing the whole store
    closed. That rules out ``math.isfinite`` on an arbitrary ``int``: a JSON
    integer like ``10**1000`` overflows the int→C-double conversion and
    raises ``OverflowError``. So an ``int`` of ANY magnitude is accepted
    without ``isfinite`` (an int cannot be NaN/inf); only a ``float`` is put
    through ``isfinite`` (to reject NaN/±inf). A ``bool`` (a subclass of
    ``int``) and any non-number are rejected."""
    if isinstance(ts, bool):
        return False
    if isinstance(ts, int):
        return True
    if isinstance(ts, float):
        return math.isfinite(ts)
    return False


def _valid_targets(targets: Any) -> bool:
    """A stored ``targets`` value must be a list of non-empty strings — the
    same shape :func:`plugin_events.ack_identity` hashes."""
    return (isinstance(targets, list)
            and all(isinstance(t, str) and t for t in targets))


def _valid_section(section: Any, keys: frozenset) -> "dict[str, dict] | None":
    """The records of one stored section (``acks`` or ``denied``), or ``None``
    when anything in it is malformed: a record whose key set is not exactly
    *keys*, a field of the wrong type, or a key that does not equal the
    RECOMPUTED identity of its own record."""
    from plugin_events import ack_identity

    if not isinstance(section, dict):
        return None
    out: dict[str, dict[str, Any]] = {}
    for ident, rec in section.items():
        if not (isinstance(ident, str) and isinstance(rec, dict)):
            return None
        # Exact schema: any key outside the record's declared set makes the
        # WHOLE store untrusted (an extra field means the file was written
        # by something other than this store).
        if set(rec) != keys:
            return None
        fields = {k: rec.get(k) for k in
                  ("subscriber", "artifact_id", "emitter", "event", "digest")}
        targets = rec.get("targets")
        gen = rec.get("gen")
        ts = rec.get("ts")
        if (not all(isinstance(v, str) and v for v in fields.values())
                or not _valid_targets(targets)
                or ("gen" in keys and not (isinstance(gen, str) and gen))
                # ``ts`` must be a real number, never a bool and never
                # NaN/inf. Checked crash-proof (a huge int must not
                # OverflowError ``math.isfinite`` and take down boot).
                or not _valid_ts(ts)):
            return None
        try:
            expected = ack_identity(fields["subscriber"], fields["artifact_id"],
                                    fields["emitter"], fields["event"],
                                    fields["digest"], targets)
        except Exception:  # noqa: BLE001 — unhashable garbage ⇒ closed
            return None
        if expected != ident:
            return None
        out[ident] = rec
    return out


class EventAckStore:
    def __init__(self, path: Path = ACKS_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        # Two maps, never one: a denial must not sit where an ack lookup
        # looks. ``get`` reads ``_acks`` only.
        self._acks: dict[str, dict[str, Any]]
        self._denied: dict[str, dict[str, Any]]
        self._acks, self._denied = self._load()

    # -- load / persist ------------------------------------------------------

    def _load(self) -> "tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]":
        """Whole-store fail-closed load (mirrors ``callback_acks.py``) of the
        ``(acks, denied)`` pair: wrong schema, any malformed record in either
        section, any key that does not equal the RECOMPUTED identity of its
        own record, or an identity present in both sections ⇒ NO acks and no
        denials at all. A truncated / merged / hand-edited store can never
        manufacture consent; the operator simply re-consents.

        TOTAL by construction: ``_load`` runs at singleton construction (boot),
        so it must return the pair for ANY file content whatsoever and never
        propagate. The per-field checks below fail the store closed, but a
        catch-all wraps the whole body as belt-and-braces — ``json.loads`` on
        deeply-nested JSON raises ``RecursionError`` (not ``ValueError``), and
        any other content could raise deeper in validation; either would crash
        the ACKS singleton at boot instead of failing closed."""
        try:
            return self._load_body()
        except Exception:  # noqa: BLE001 — totality over the whole load
            logger.warning(
                "event acks: load failed; treating as no acks", exc_info=True)
            return {}, {}

    def _load_body(self) -> "tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]":
        try:
            with open(self.path, "rb") as fh:
                # Bound the read itself (not read-then-check): an oversized
                # store must not be slurped into memory before it is refused.
                data = fh.read(_MAX_ACKS_BYTES + 1)
        except OSError:
            return {}, {}
        if len(data) > _MAX_ACKS_BYTES:
            return {}, {}
        raw = json.loads(data.decode("utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            return {}, {}
        acks = _valid_section(raw.get("acks"), _ACK_RECORD_KEYS)
        # No ``denied`` section (a store written before denials were kept, or
        # one holding none) means no denials; a present one is validated like
        # the acks and fails the whole store the same way.
        denied = _valid_section(raw.get("denied", {}), _DENIAL_RECORD_KEYS)
        if acks is None or denied is None or acks.keys() & denied.keys():
            return {}, {}
        return acks, denied

    def _commit_locked(self, acks: dict[str, dict[str, Any]],
                       denied: dict[str, dict[str, Any]]) -> None:
        """Durably write the candidate maps in ONE atomic write, and only then
        publish them — a failed write raises with the in-memory view
        unchanged, so a racing reconcile can never route (or drop) state
        that would revert on reboot."""
        from atomic_io import PRIVATE, atomic_write_text
        payload: dict[str, Any] = {"schema_version": _SCHEMA_VERSION,
                                   "acks": acks}
        if denied:
            payload["denied"] = denied
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            mode=PRIVATE,
        )
        self._acks, self._denied = acks, denied

    # -- queries -------------------------------------------------------------

    def get(self, identity: str) -> "dict[str, Any] | None":
        """The full ack record for *identity* (a copy), or ``None``."""
        with self._lock:
            rec = self._acks.get(identity)
            return dict(rec) if rec is not None else None

    def is_denied(self, identity: str) -> bool:
        """True when the operator turned *identity* off and has not approved
        it since. Never grounds for delivery: only :meth:`get` is."""
        with self._lock:
            return identity in self._denied

    def subscribers_on(self, artifact_ids) -> set[str]:
        """The subscribers holding an ack or a denial on any of
        *artifact_ids*."""
        ids = set(artifact_ids)
        with self._lock:
            return {rec["subscriber"]
                    for rec in (*self._acks.values(), *self._denied.values())
                    if rec.get("artifact_id") in ids}

    # -- mutations (each persists atomically before returning) ---------------

    def record(self, subscriber: str, artifact_id: str, emitter: str,
              event: str, digest: str, targets: list[str],
              now: float) -> dict[str, Any]:
        """Record the operator's consent for this (subscriber, artifact_id,
        emitter, event, digest, targets) identity (idempotent); returns the
        stored record (a copy). ``targets`` is stored SORTED — the canonical
        form :func:`plugin_events.ack_identity` hashes. An earlier denial of
        the same identity is cleared in the same write."""
        identity, rec = _identity_and_record(
            subscriber, artifact_id, emitter, event, digest, targets, now)
        with self._lock:
            # Per-approval GENERATION (mirrors callback_acks/trigger_acks):
            # unique per approval so re-approval after a revoke (record gone)
            # necessarily yields a new (identity, gen) pair. Re-recording a
            # LIVE ack (idempotent duplicate) keeps the generation.
            existing = self._acks.get(identity)
            rec["gen"] = (existing["gen"] if existing and existing.get("gen")
                          else uuid.uuid4().hex)
            acks = dict(self._acks)
            acks[identity] = rec
            denied = dict(self._denied)
            denied.pop(identity, None)
            self._commit_locked(acks, denied)
            return dict(rec)

    def deny(self, subscriber: str, artifact_id: str, emitter: str,
             event: str, digest: str, targets: list[str], now: float) -> None:
        """Record the operator's Deny of this identity, dropping its ack if
        it holds one. The subscription stays undelivered and no longer counts
        as waiting for an answer."""
        identity, rec = _identity_and_record(
            subscriber, artifact_id, emitter, event, digest, targets, now)
        with self._lock:
            acks = dict(self._acks)
            acks.pop(identity, None)
            denied = dict(self._denied)
            denied[identity] = rec
            self._commit_locked(acks, denied)

    def revoke_subscriber(self, subscriber: str, *,
                          deny_ts: "float | None" = None,
                          deny=()) -> list[dict[str, Any]]:
        """Drop every ack recorded for *subscriber*; returns the removed
        ack records. ``deny_ts``/``deny``: see :meth:`_revoke`."""
        return self._revoke(lambda rec: rec.get("subscriber") == subscriber,
                            deny_ts, deny)

    def revoke_pair(self, subscriber: str, emitter: str, event: str, *,
                    deny_ts: "float | None" = None,
                    deny=()) -> list[dict[str, Any]]:
        """Drop every ack for one (subscriber, emitter, event) subscription —
        across any declaration digest or target set. Returns the removed
        ack records. ``deny_ts``/``deny``: see :meth:`_revoke`."""
        return self._revoke(
            lambda rec: rec.get("subscriber") == subscriber
            and rec.get("emitter") == emitter and rec.get("event") == event,
            deny_ts, deny)

    def prune_stale(self, valid_identities: set[str]) -> list[dict[str, Any]]:
        """Opportunistic reconcile prune: drop every ack and every denial
        whose identity is not in *valid_identities* (no installed declaration
        can still compute it); returns the removed ack records."""
        with self._lock:
            acks = {i: r for i, r in self._acks.items()
                    if i in valid_identities}
            denied = {i: r for i, r in self._denied.items()
                      if i in valid_identities}
            if len(acks) == len(self._acks) and len(denied) == len(self._denied):
                return []
            removed = [r for i, r in self._acks.items() if i not in acks]
            self._commit_locked(acks, denied)
            return removed

    def _revoke(self, predicate, deny_ts: "float | None",
                deny) -> list[dict[str, Any]]:
        """Drop the acks matching *predicate*, in one write with what becomes
        of the denials:

        * ``deny_ts`` is ``None`` (the subscriber was removed, or updated to
          another artifact): the matching denials are dropped too, so a
          return to that artifact asks afresh.
        * ``deny_ts`` is a timestamp (the operator's revoke): each dropped
          ack becomes a denial, and so does each subscription in *deny* —
          the ones the caller found installed, which may hold no ack at all.
          Denials already stored stay.
        """
        with self._lock:
            acks = dict(self._acks)
            removed = [acks.pop(i) for i, rec in self._acks.items()
                       if predicate(rec)]
            denied = dict(self._denied)
            if deny_ts is None:
                for i in [i for i, rec in denied.items() if predicate(rec)]:
                    del denied[i]
            else:
                for sub in (*removed, *deny):
                    identity, rec = _identity_and_record(
                        sub["subscriber"], sub["artifact_id"], sub["emitter"],
                        sub["event"], sub["digest"], sub["targets"], deny_ts)
                    held = acks.pop(identity, None)
                    if held is not None:
                        removed.append(held)
                    denied.setdefault(identity, rec)
            if len(acks) == len(self._acks) and denied == self._denied:
                return []
            self._commit_locked(acks, denied)
            return removed


def _identity_and_record(subscriber: str, artifact_id: str, emitter: str,
                         event: str, digest: str, targets: list[str],
                         ts: float) -> "tuple[str, dict[str, Any]]":
    """The consent identity and the stored record (without ``gen``) for one
    subscription, with ``targets`` SORTED — the canonical form
    :func:`plugin_events.ack_identity` hashes."""
    from plugin_events import ack_identity
    sorted_targets = sorted(targets)
    identity = ack_identity(subscriber, artifact_id, emitter, event,
                            digest, sorted_targets)
    return identity, {
        "subscriber": subscriber,
        "artifact_id": artifact_id,
        "emitter": emitter,
        "event": event,
        "digest": digest,
        "targets": sorted_targets,
        "ts": ts,
    }


# Process-wide singleton (mirrors ACKS in callback_acks/trigger_acks): the
# reconciler, the event consent approve callback, and the plugin lifecycle
# revocation path all share it.
ACKS = EventAckStore()
