"""Persistent operator-consent acks for plugin-declared webhook triggers
(Release B).

One ack = the operator approved exactly one consent IDENTITY
(:func:`plugin_triggers.ack_identity` — plugin + artifact + effective name +
target + normalized auth policy). Records are STRUCTURED (not bare hashes) so
lifecycle revocation can answer "which plugin/artifact/effective names were
consented" — `revoke_artifact` returns the removed records and the caller
retires the matching per-trigger secrets.

Properties:

* **Atomic** — every mutation persists via ``atomic_io.atomic_write_text``
  (sidecar + fsync + ``os.replace``); a crash mid-write can never leave a
  half-written store that later parses into unintended consent.
* **Fail-closed** — a missing, unreadable, or malformed store means NO acks
  (triggers stay unrouted, ``trigger_pending_ack``); it never raises into the
  reconciler. The next successful ``record`` rewrites a valid store.
* **Thread-safe** — a ``threading.Lock`` guards state: ``record`` runs on the
  event loop (Telegram approve callback), revocation runs from the plugin
  lifecycle path, and health regeneration reads from a worker thread.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ACKS_PATH = Path("/data/webhook_trigger_acks.json")

_SCHEMA_VERSION = 1


class TriggerAckStore:
    def __init__(self, path: Path = ACKS_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._acks: dict[str, dict[str, Any]] = self._load()

    # -- load / persist ------------------------------------------------------

    def _load(self) -> dict[str, dict[str, Any]]:
        """Whole-store fail-closed load (Terra shipB-r1 P1-3a): wrong schema,
        any malformed record, or any key that does not equal the RECOMPUTED
        identity of its own record ⇒ NO acks at all. A truncated / merged /
        hand-edited store can never manufacture consent; the operator simply
        re-consents."""
        from plugin_triggers import ack_identity

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            return {}
        acks = raw.get("acks")
        if not isinstance(acks, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for ident, rec in acks.items():
            if not (isinstance(ident, str) and isinstance(rec, dict)):
                return {}
            fields = {k: rec.get(k)
                      for k in ("plugin", "artifact_id", "effective", "target")}
            auth = rec.get("auth")
            gen = rec.get("gen")
            if (not all(isinstance(v, str) and v for v in fields.values())
                    or not isinstance(auth, dict)
                    or not (isinstance(gen, str) and gen)):
                return {}
            try:
                expected = ack_identity(auth=auth, **fields)
            except Exception:  # noqa: BLE001 — unhashable garbage ⇒ closed
                return {}
            if expected != ident:
                return {}
            out[ident] = rec
        return out

    def _persist_candidate_locked(self, candidate: dict[str, dict[str, Any]]) -> None:
        """Durably write *candidate*; the caller publishes it to ``_acks``
        ONLY afterwards (Terra shipB-r1 P1-3b) — a failed write raises with
        the in-memory view unchanged, so a racing reconcile can never route
        (or drop) state that would revert on reboot."""
        from atomic_io import PRIVATE, atomic_write_text
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.path,
            json.dumps({"schema_version": _SCHEMA_VERSION, "acks": candidate},
                       indent=2, sort_keys=True) + "\n",
            mode=PRIVATE,
        )

    # -- queries -------------------------------------------------------------

    def is_acked(self, identity: str) -> bool:
        with self._lock:
            return identity in self._acks

    def get(self, identity: str) -> "dict[str, Any] | None":
        """The full ack record for *identity* (a copy), or ``None``. The
        reconciler reads ``rec["gen"]`` — the per-approval generation — to
        bind the minted secret to THIS approval, not just the identity."""
        with self._lock:
            rec = self._acks.get(identity)
            return dict(rec) if rec is not None else None

    # -- mutations (each persists atomically before returning) ---------------

    def record(
        self, *, identity: str, plugin: str, artifact_id: str,
        effective: str, target: str, auth: dict[str, Any],
    ) -> None:
        """Record the operator's consent for *identity* (idempotent)."""
        rec = {
            "plugin": plugin,
            "artifact_id": artifact_id,
            "effective": effective,
            "target": target,
            "auth": dict(auth),
            "ts": int(time.time()),
        }
        with self._lock:
            # Per-approval GENERATION (Sol shipB-r3): unique per approval so
            # re-approval after a revoke (record gone) necessarily yields a
            # new (identity, gen) pair — the secret mint binds to it and
            # must rekey. Re-recording a LIVE ack (idempotent duplicate)
            # keeps the generation, so the bound secret stays stable.
            existing = self._acks.get(identity)
            rec["gen"] = (existing["gen"] if existing and existing.get("gen")
                          else uuid.uuid4().hex)
            candidate = dict(self._acks)
            candidate[identity] = rec
            self._persist_candidate_locked(candidate)
            self._acks = candidate

    def revoke_plugin(self, plugin: str) -> list[dict[str, Any]]:
        """Drop every ack recorded for *plugin*; returns the removed records."""
        return self._revoke(lambda rec: rec.get("plugin") == plugin)

    def revoke_artifact(self, artifact_id: str) -> list[dict[str, Any]]:
        """Drop every ack bound to *artifact_id*; returns the removed records
        (the caller retires the matching per-trigger secrets)."""
        return self._revoke(lambda rec: rec.get("artifact_id") == artifact_id)

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


# Process-wide singleton (mirrors GRANTS/CHALLENGES): the reconciler, the
# consent approve callback, and the lifecycle revocation path all share it.
ACKS = TriggerAckStore()
