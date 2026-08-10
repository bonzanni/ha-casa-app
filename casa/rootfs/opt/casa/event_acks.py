"""Persistent operator-consent acks for plugin-declared event subscriptions.

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


class EventAckStore:
    def __init__(self, path: Path = ACKS_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._acks: dict[str, dict[str, Any]] = self._load()

    # -- load / persist ------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        """Whole-store fail-closed load (mirrors ``callback_acks.py``): wrong
        schema, any malformed record, or any key that does not equal the
        RECOMPUTED identity of its own record ⇒ NO acks at all. A truncated
        / merged / hand-edited store can never manufacture consent; the
        operator simply re-consents.

        TOTAL by construction: ``_load`` runs at singleton construction (boot),
        so it must return a dict for ANY file content whatsoever and never
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
            return {}

    def _load_body(self) -> dict[str, dict[str, Any]]:
        from plugin_events import ack_identity

        try:
            with open(self.path, "rb") as fh:
                # Bound the read itself (not read-then-check): an oversized
                # store must not be slurped into memory before it is refused.
                data = fh.read(_MAX_ACKS_BYTES + 1)
        except OSError:
            return {}
        if len(data) > _MAX_ACKS_BYTES:
            return {}
        raw = json.loads(data.decode("utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            return {}
        acks = raw.get("acks")
        if not isinstance(acks, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for ident, rec in acks.items():
            if not (isinstance(ident, str) and isinstance(rec, dict)):
                return {}
            # Exact schema: any key outside the record's declared set makes the
            # WHOLE store untrusted (an extra field means the file was written
            # by something other than this store).
            if set(rec) != _ACK_RECORD_KEYS:
                return {}
            fields = {k: rec.get(k) for k in
                      ("subscriber", "artifact_id", "emitter", "event", "digest")}
            targets = rec.get("targets")
            gen = rec.get("gen")
            ts = rec.get("ts")
            if (not all(isinstance(v, str) and v for v in fields.values())
                    or not _valid_targets(targets)
                    or not (isinstance(gen, str) and gen)
                    # ``ts`` must be a real number, never a bool and never
                    # NaN/inf. Checked crash-proof (a huge int must not
                    # OverflowError ``math.isfinite`` and take down boot).
                    or not _valid_ts(ts)):
                return {}
            try:
                expected = ack_identity(fields["subscriber"], fields["artifact_id"],
                                        fields["emitter"], fields["event"],
                                        fields["digest"], targets)
            except Exception:  # noqa: BLE001 — unhashable garbage ⇒ closed
                return {}
            if expected != ident:
                return {}
            out[ident] = rec
        return out

    def _persist_candidate_locked(self, candidate: dict[str, dict[str, Any]]) -> None:
        """Durably write *candidate*; the caller publishes it to ``_acks``
        ONLY afterwards — a failed write raises with the in-memory view
        unchanged, so a racing reconcile can never route (or drop) state
        that would revert on reboot."""
        from atomic_io import PRIVATE, atomic_write_text
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.path,
            json.dumps({"schema_version": _SCHEMA_VERSION, "acks": candidate},
                       indent=2, sort_keys=True) + "\n",
            mode=PRIVATE,
        )

    # -- queries -------------------------------------------------------------

    def get(self, identity: str) -> "dict[str, Any] | None":
        """The full ack record for *identity* (a copy), or ``None``."""
        with self._lock:
            rec = self._acks.get(identity)
            return dict(rec) if rec is not None else None

    # -- mutations (each persists atomically before returning) ---------------

    def record(self, subscriber: str, artifact_id: str, emitter: str,
              event: str, digest: str, targets: list[str],
              now: float) -> dict[str, Any]:
        """Record the operator's consent for this (subscriber, artifact_id,
        emitter, event, digest, targets) identity (idempotent); returns the
        stored record (a copy). ``targets`` is stored SORTED — the canonical
        form :func:`plugin_events.ack_identity` hashes."""
        from plugin_events import ack_identity
        sorted_targets = sorted(targets)
        identity = ack_identity(subscriber, artifact_id, emitter, event,
                                digest, sorted_targets)
        rec = {
            "subscriber": subscriber,
            "artifact_id": artifact_id,
            "emitter": emitter,
            "event": event,
            "digest": digest,
            "targets": sorted_targets,
            "ts": now,
        }
        with self._lock:
            # Per-approval GENERATION (mirrors callback_acks/trigger_acks):
            # unique per approval so re-approval after a revoke (record gone)
            # necessarily yields a new (identity, gen) pair. Re-recording a
            # LIVE ack (idempotent duplicate) keeps the generation.
            existing = self._acks.get(identity)
            rec["gen"] = (existing["gen"] if existing and existing.get("gen")
                          else uuid.uuid4().hex)
            candidate = dict(self._acks)
            candidate[identity] = rec
            self._persist_candidate_locked(candidate)
            self._acks = candidate
            return dict(rec)

    def revoke_subscriber(self, subscriber: str) -> list[dict[str, Any]]:
        """Drop every ack recorded for *subscriber*; returns the removed
        records."""
        return self._revoke(lambda rec: rec.get("subscriber") == subscriber)

    def revoke_pair(self, subscriber: str, emitter: str, event: str) -> list[dict[str, Any]]:
        """Drop every ack for one (subscriber, emitter, event) subscription —
        across any declaration digest or target set. Returns the removed
        records."""
        return self._revoke(
            lambda rec: rec.get("subscriber") == subscriber
            and rec.get("emitter") == emitter and rec.get("event") == event)

    def prune_stale(self, valid_identities: set[str]) -> list[dict[str, Any]]:
        """Opportunistic reconcile prune: drop every ack whose identity is
        not in *valid_identities* (no installed declaration can still
        compute it); returns the removed records."""
        with self._lock:
            matched = [i for i in self._acks if i not in valid_identities]
            if not matched:
                return []
            candidate = dict(self._acks)
            removed = [candidate.pop(i) for i in matched]
            self._persist_candidate_locked(candidate)
            self._acks = candidate
            return removed

    def _revoke(self, predicate) -> list[dict[str, Any]]:
        with self._lock:
            matched = [i for i, rec in self._acks.items() if predicate(rec)]
            if not matched:
                return []
            candidate = dict(self._acks)
            removed = [candidate.pop(i) for i in matched]
            self._persist_candidate_locked(candidate)
            self._acks = candidate
            return removed


# Process-wide singleton (mirrors ACKS in callback_acks/trigger_acks): the
# reconciler, the event consent approve callback, and the plugin lifecycle
# revocation path all share it.
ACKS = EventAckStore()
