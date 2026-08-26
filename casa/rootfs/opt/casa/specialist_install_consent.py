"""Structural operator-consent gate for specialist/persona installs — the
SAME shape as trigger_consent.py's plugin-trigger consent: a DM
Approve/Deny keyboard whose Approve synchronously persists a fail-closed,
atomically-written ack BEFORE any CAS write or activation. Never a checksum
string an LLM tool call could echo back itself."""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from canonical_bytes import checksum_json

logger = logging.getLogger(__name__)

INSTALL_CONSENT_TTL_S = 600.0
_ACKS_PATH = Path("/data/specialist_install_acks.json")
_SCHEMA_VERSION = 1

# Task 7: ONE process-wide ledger lock (spec §3.5). Multiple
# SpecialistInstallAckStore *instances* over the same file (e.g. one built
# per tool call) must never race each other's read-modify-write — a
# per-instance `threading.Lock()` only serializes calls made through that
# one instance, not concurrent instances. Every store method acquires this
# module-level lock, reloads the ledger file fresh, applies its delta, and
# persists — the store instance holds no authoritative in-memory cache.
_LEDGER_LOCK = threading.Lock()


def install_consent_identity(*, component_id: str, version: str, root_digest: str,
                              slug: str, receipt_digest: str = "") -> str:
    """The ONE identity-derivation function for a specialist-install consent
    decision (mirrors plugin_triggers.ack_identity). Binds the approval to
    the EXACT inspected component — a re-fetch that changes any of these
    fields yields a different identity, so a stale approval can never
    ack a different artifact.

    The bound digest is `inspection.root_digest` — compute_install_root_digest's
    full-closure digest (role+doctrine+config-schema+manifest+persona+
    dependencies), NOT `inspection.component_checksum` (the narrow 3-file
    digest, which is still a separate field on InspectionResult for
    CAS-neutral display/logging only). Binding consent to the narrow digest
    would let a persona/corpus/plugin substitution slip past an operator's
    approval unnoticed.

    Minor Ma (whole-branch review): the PARAMETER is now honestly named
    `root_digest` to match what every caller actually passes. The persisted
    ack-store JSON field and the identity-hash INPUT KEY both stay
    `"component_checksum"` for backward compatibility — an existing ack file's
    recomputed identity must remain byte-stable — so the value is mapped onto
    that historical key here rather than renamed through the hash.

    Task 7: `receipt_digest` binds a bundled-plugin receipt (Task 8) into the
    consent identity so re-approval is required whenever the bundled-plugin
    closure changes. It joins the hashed payload ONLY when non-empty — the
    default `receipt_digest=""` MUST hash byte-identically to the pre-Task-7
    four-key payload, so every legacy ack file recorded before this change
    keeps recomputing to the same identity on load."""
    payload = {
        "component_id": component_id, "version": version,
        "component_checksum": root_digest, "slug": slug,
    }
    if receipt_digest:
        payload["receipt_digest"] = receipt_digest
    return checksum_json(payload)


@dataclass(frozen=True)
class SpecialistInstallConsentKey:
    component_id: str
    slug: str
    identity: str


class SpecialistInstallAckStore:
    """Fail-closed, atomically-persisted install-consent acks — structurally
    identical to trigger_acks.TriggerAckStore (whole-store fail-closed load,
    identity recomputed and compared on load, atomic_write_text on every
    mutation).

    Task 7: the store instance holds NO authoritative in-memory cache. Every
    method (mutation AND read) acquires the module-level `_LEDGER_LOCK`,
    re-reads the ledger file fresh (`self._load()`), applies its delta, and
    (for mutations) persists — never a whole-map rewrite from stale instance
    state. This makes multiple `SpecialistInstallAckStore` instances over the
    same file (as the tool layer constructs per call) safe to interleave:
    correctness over micro-perf, since this file is tiny and reads are rare."""

    def __init__(self, path: Path = _ACKS_PATH) -> None:
        self.path = Path(path)

    def _load(self, *, strict_read: bool = False) -> dict[str, dict[str, Any]]:
        # Caller must hold _LEDGER_LOCK. Sol r1 (#310): every MUTATION passes
        # strict_read=True — a transient read failure (an OSError that is not
        # file-missing) must abort the read-modify-write rather than persist
        # the fail-closed empty view over previously recorded acks. Reads keep
        # the fail-closed {} (no consent manufactured), and a content-invalid
        # store still starts empty: the next successful record rewrites a
        # valid store (the documented corruption recovery).
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except OSError:
            if strict_read:
                raise
            return {}
        except ValueError:
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
            fields = {k: rec.get(k) for k in ("component_id", "version",
                                                "component_checksum", "slug")}
            if not all(isinstance(v, str) and v for v in fields.values()):
                return {}
            receipt_digest = rec.get("receipt_digest", "")
            if not isinstance(receipt_digest, str):
                return {}
            # Ma: the persisted field stays "component_checksum" (backward
            # compat); map it onto the honestly-named `root_digest` parameter.
            # A legacy record has no "receipt_digest" key at all, so
            # `rec.get("receipt_digest", "")` defaults to "" — the SAME
            # default `install_consent_identity` uses — recomputing the
            # exact byte-stable legacy identity.
            if install_consent_identity(
                component_id=fields["component_id"], version=fields["version"],
                root_digest=fields["component_checksum"], slug=fields["slug"],
                receipt_digest=receipt_digest,
            ) != ident:
                return {}
            out[ident] = rec
        return out

    def _persist_locked(self, candidate: dict[str, dict[str, Any]]) -> None:
        # Caller must hold _LEDGER_LOCK.
        from atomic_io import PRIVATE, atomic_write_text
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.path,
            json.dumps({"schema_version": _SCHEMA_VERSION, "acks": candidate},
                       indent=2, sort_keys=True) + "\n",
            mode=PRIVATE,
        )

    def is_acked(self, identity: str) -> bool:
        with _LEDGER_LOCK:
            return identity in self._load()

    def get(self, identity: str) -> "dict[str, Any] | None":
        with _LEDGER_LOCK:
            rec = self._load().get(identity)
            return dict(rec) if rec is not None else None

    def record(self, *, identity: str, component_id: str, version: str,
               component_checksum: str, slug: str, receipt_digest: str = "") -> None:
        rec = {"component_id": component_id, "version": version,
               "component_checksum": component_checksum, "slug": slug,
               "ts": int(time.time())}
        if receipt_digest:
            rec["receipt_digest"] = receipt_digest
        with _LEDGER_LOCK:
            candidate = dict(self._load(strict_read=True))
            candidate[identity] = rec
            self._persist_locked(candidate)

    def revoke(self, identity: str) -> bool:
        with _LEDGER_LOCK:
            acks = self._load(strict_read=True)
            if identity not in acks:
                return False
            candidate = dict(acks)
            del candidate[identity]
            self._persist_locked(candidate)
            return True

    def retire_slug(self, slug: str) -> list[dict]:
        """Remove ALL records whose `slug` matches, returning the removed
        records (for journaling — the caller can `restore_records` them back
        on a rollback)."""
        with _LEDGER_LOCK:
            acks = self._load(strict_read=True)
            removed = [dict(rec) for rec in acks.values() if rec.get("slug") == slug]
            if not removed:
                return []
            kept = {i: r for i, r in acks.items() if r.get("slug") != slug}
            self._persist_locked(kept)
            return removed

    def snapshot_slug(self, slug: str) -> list[dict]:
        """Read-only copy of the slug's records — journal the before-state
        BEFORE a mutating `begin` so a rollback has something to restore."""
        with _LEDGER_LOCK:
            return [dict(rec) for rec in self._load().values() if rec.get("slug") == slug]

    def restore_records(self, records: list[dict]) -> None:
        """Slug-scoped delta re-insert used by journal rollback/boot
        reconciliation: each record's identity is recomputed from its OWN
        fields (including its own `receipt_digest`, defaulting to "" for
        records without the key) rather than trusted verbatim, and the
        result is merged into the current ledger under the lock — never a
        whole-map rewrite that could clobber a concurrent unrelated write."""
        if not records:
            return
        with _LEDGER_LOCK:
            acks = self._load(strict_read=True)
            candidate = dict(acks)
            for rec in records:
                rec = dict(rec)
                identity = install_consent_identity(
                    component_id=rec["component_id"], version=rec["version"],
                    root_digest=rec["component_checksum"], slug=rec["slug"],
                    receipt_digest=rec.get("receipt_digest", ""),
                )
                candidate[identity] = rec
            self._persist_locked(candidate)


def render_install_consent_message(inspection: Any) -> str:
    # Round-3 fix (finding #7): the identity this consent binds
    # (`install_consent_identity(..., component_checksum=inspection.
    # root_digest, ...)` below) is keyed on `root_digest` — the FULL-CLOSURE
    # digest of the component PLUS every resolved dependency — not the
    # narrow `component_checksum`. Show `root_digest` (what the tap actually
    # binds) plus each resolved dependency's own digest, so the operator can
    # see exactly what the approval covers; `component_checksum` stays for
    # reference.
    deps = ", ".join(f"{d.kind}:{d.identifier}" for d in inspection.dependencies)
    dep_digest_lines = "".join(
        f"  - {d.kind}:{d.identifier} = {d.digest}\n" for d in inspection.dependencies)
    # Task 8: one block per sourced plugin dependency, from the REAL
    # `specialist_receipt.PluginReceiptRow`s Task 8 attaches at
    # `inspection.plugin_resolutions` (Task 7's sketch used provisional
    # per-dependency `getattr` field names before that type existed — this
    # replaces it with the real field set: identifier/scoped_name/
    # manifest_name/version/source_type/content_digest/mcp_servers/
    # protected_tools/env_names).
    #
    # Fix-round-1 (consent-review CRITICAL, spec §3.2): the DM must show
    # "scoped name, manifest name, version, MCP servers/commands, protected
    # tools, secrets surface" per plugin — the three list-valued lines below
    # close that gap; each is omitted entirely when empty (nothing to
    # approve in that category) rather than rendered as a bare label.
    plugin_blocks: list[str] = []
    for row in inspection.plugin_resolutions or ():
        lines = [
            f"  Bundled plugin {row.scoped_name}:",
            f"    manifest name: {row.manifest_name}",
            f"    version: {row.version}",
            f"    source: {row.source_type}",
            f"    content digest: {row.content_digest}",
        ]
        if row.mcp_servers:
            lines.append(f"    MCP servers: {', '.join(row.mcp_servers)}")
        if row.protected_tools:
            lines.append(f"    Protected tools: {', '.join(row.protected_tools)}")
        if row.env_names:
            lines.append(f"    Secrets required: {', '.join(row.env_names)}")
        plugin_blocks.append("\n".join(lines))
    plugin_section = ("\n".join(plugin_blocks) + "\n") if plugin_blocks else ""
    # #541: the role's own casa-framework tool grants — the one place spawn/
    # privilege tools could have entered was the one thing the operator never
    # saw. Post-ceiling these are always consumer-safe, but the approving
    # operator still sees what powers the specialist arrives with. Omitted
    # entirely when the role grants none (same style as the plugin lines);
    # getattr-defaulted so a hand-built inspection object predating the
    # field renders unchanged.
    role_grants = getattr(inspection, "role_tool_grants", ()) or ()
    casa_tools_line = (
        f"Casa tools: {', '.join(role_grants)}\n" if role_grants else "")
    return (
        "\U0001F510 Specialist install consent\n\n"
        f"Install '{inspection.component_id}@{inspection.version}' as "
        f"specialist:{inspection.slug}?\n"
        f"Mission: {inspection.mission}\n"
        f"{casa_tools_line}"
        f"Default persona: {inspection.default_persona_ref}\n"
        f"Dependencies: {deps or '(none)'}\n"
        f"{dep_digest_lines}"
        f"{plugin_section}"
        f"Root digest (approved — component + dependencies): {inspection.root_digest}\n"
        f"Component checksum: {inspection.component_checksum}\n\n"
        "Approve to install; Deny to discard the staged fetch."
    )


def prompt_specialist_install_consent(
    *, coordinator: Any, channel: Any, chat_id: int, operator_id: int, inspection: Any,
    acks: "SpecialistInstallAckStore",
    reconcile_cb: "Callable[[], Awaitable[bool]] | None" = None,
    inbound_reservation: Any | None = None,
) -> Any:
    # #663: ``inbound_reservation`` is the requesting engagement's SYNCHRONOUS
    # ingress lease, built by the caller (which is the only party that knows
    # the engagement) and taken at the tap-commit below. This module stays
    # ignorant of engagements: it holds a duck-typed object with ``take()`` and
    # ``release()`` and nothing else.
    # Task 7: thread `receipt_digest` (Task 8's bundled-plugin receipt digest)
    # into the identity so a bundled-plugin closure change forces
    # re-approval.
    receipt_digest = inspection.receipt_digest
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug,
        receipt_digest=receipt_digest,
    )
    key = SpecialistInstallConsentKey(
        component_id=inspection.component_id, slug=inspection.slug, identity=identity)
    text = render_install_consent_message(inspection)

    def _on_commit_sync(idx: int, meta: dict) -> None:
        if idx == 0:
            acks.record(identity=identity, component_id=inspection.component_id,
                        version=inspection.version,
                        component_checksum=inspection.root_digest, slug=inspection.slug,
                        receipt_digest=receipt_digest)
            meta["acked"] = True
            # #663: AFTER the ack, never before it — this step must not
            # reorder around the authoritative record write. Approve only:
            # Deny dispatches no continuation, so there is nothing to reserve
            # against. This is the one synchronous instant inside the window
            # (the Telegram callback runs it with no await after
            # ``BROKER.commit``), so the reservation exists before the finish
            # hook's task has even been scheduled.
            if inbound_reservation is not None:
                inbound_reservation.take()

    def _finish_factory(message_id: int, req: Any) -> Callable[[dict], Any]:
        async def _finish(outcome: dict) -> None:
            # #663: the reservation's release-guarantee. Wrapping the WHOLE
            # body is what covers every arm that never reaches the delivery
            # seam — deny, expiry, an unrecorded ack, a raising body,
            # cancellation, a channel that is down. On the ordinary approve
            # path the seam has already released it and this is a no-op; the
            # lease is idempotent precisely so both owners can call it. An
            # unreleased reservation would make a successful completion
            # permanently impossible under INV-ENG-003.
            try:
                await _finish_inner(outcome)
            finally:
                if inbound_reservation is not None:
                    inbound_reservation.release()

        async def _finish_inner(outcome: dict) -> None:
            o = outcome.get("outcome") if isinstance(outcome, dict) else None
            if o != "answered":
                await channel.edit_dm_message(
                    chat_id, message_id,
                    f"⌛ Expired — install consent for {inspection.slug!r} was not "
                    "answered; nothing was installed",
                )
                return
            if outcome.get("option_index") == 0:
                if not req.meta.get("acked"):
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        "internal error recording install consent — re-run the install to "
                        "be prompted again",
                    )
                    return
                # #662: the outcome decides the wording, so the edit happens
                # AFTER reconciliation, never before it, and only a literal
                # True selects the success text — a truthiness test would
                # accept any truthy value a future callback returned in place
                # of the contract's bool.
                #
                # What True does and does not establish is stated once, in
                # INV-SPEC-010 (docs/architecture/specialist-lifecycle.md).
                # Three review rounds were spent on restatements of it that
                # each overclaimed; this comment deliberately does not add a
                # fourth.
                outcome: object = False
                raised = False
                if reconcile_cb is not None:
                    try:
                        outcome = await reconcile_cb()
                    except Exception:  # noqa: BLE001 — the callback contract is
                        # never-raise; this is defence in depth, and it leaves
                        # CancelledError as control flow.
                        logger.exception(
                            "post-consent specialist install reconcile raised "
                            "(slug=%s)", inspection.slug)
                        outcome, raised = False, True
                if outcome is True:
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        "✅ Approved — requested an automatic configurator "
                        f"continuation for {inspection.slug!r}",
                    )
                elif raised:
                    # A raise is a CONTRACT VIOLATION (no production callback
                    # can produce one), and it is the single branch where the
                    # outcome is genuinely unknown rather than known-negative:
                    # the callback may have handed the turn off before it blew
                    # up. Say uncertain, not "not started".
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        f"⚠️ Approved and saved — but the install of "
                        f"{inspection.slug!r} hit an internal error and its "
                        "automatic start could not be confirmed. Check the "
                        "configurator topic; re-running the install is safe "
                        "either way, and the approval recorded for this exact "
                        "version is reused if it still applies.",
                    )
                else:
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        f"⚠️ Approved and saved — but the install of "
                        f"{inspection.slug!r} was not started automatically. "
                        "Start a new configurator engagement and re-run the "
                        "install; the approval recorded for this exact version "
                        "is reused if it still applies.",
                    )
            else:
                await channel.edit_dm_message(
                    chat_id, message_id, f"❌ Denied — {inspection.slug!r} was not installed",
                )

        return _finish

    return coordinator.register_challenge(
        key, chat_id=chat_id, operator_id=operator_id, channel=channel,
        challenge_text=text, options=["Approve", "Deny"],
        on_commit_sync=_on_commit_sync, finish_factory=_finish_factory,
        kind="specialist_install_consent",
        meta_extra={"install_slug": inspection.slug, "install_component_id": inspection.component_id},
        timeout_s=INSTALL_CONSENT_TTL_S,
    )
