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
    # #1445: the requesting engagement's id ("" when none). The coordinator
    # dedups by key, so without it a second engagement asking for the same
    # artifact shared the first one's card and callbacks and was never told
    # how it settled. Not named ``engagement_id``: ``cancel_matching``
    # (#1049) matches that attribute, and withdrawing these cards when their
    # engagement ends is a different change. The approval identity above
    # never sees it.
    requester: str = ""


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


#: a digest the approval does not bind directly is shown by its first characters: the root
#: digest (shown in full) covers the component and every dependency
SHORT_DIGEST_HEX = 16


def _short_digest(digest: str) -> str:
    algo, sep, hexpart = str(digest).partition(":")
    if not sep:
        algo, hexpart = "", algo
    if len(hexpart) <= SHORT_DIGEST_HEX:
        return str(digest)
    return f"{algo}{sep}{hexpart[:SHORT_DIGEST_HEX]}…"


def render_install_consent_message(inspection: Any, *, mode: str = "install") -> str:
    """The consent DM, in the order the operator decides by: one line saying what is asked
    (install or update, which component at which version, as which specialist) and the
    mission; then what it comes with — the Casa tools its role grants, its persona, its
    dependencies and, per bundled plugin, its MCP servers, protected tools and the secrets it
    requires or only references; then the action; and the digests last. The root digest —
    what the approval binds, the component plus every resolved dependency — is shown in full;
    the dependency, plugin-content and component digests it covers are shown short.

    Round-3 fix (finding #7): the identity this consent binds
    (``install_consent_identity(..., root_digest=inspection.root_digest, ...)``) is keyed
    on ``root_digest``, the FULL-CLOSURE digest, not the narrow ``component_checksum``, so
    the root digest is the one shown whole. Task 8 / fix-round-1 (spec §3.2): each sourced
    plugin's scoped name, manifest name, version, source, MCP servers/commands, protected
    tools and secrets surface are shown; a list-valued line is omitted when empty. #994: "Secrets
    required" is what the withhold gate holds the plugin on; the exempt names (a
    ``${VAR:-default}`` the CLI satisfies itself, a ``casa.setupProvides`` name the plugin's own
    setup forges) are disclosed on a line whose label says they are not asks. #541: the role's
    casa-framework tool grants are shown (``Casa tools:``), omitted when it grants none.
    #1225: an upgrade, and a settings change at the installed version, use this same keyboard
    and are worded as an update; only the Casa-authored first and action lines vary, and
    neither is longer than its install counterpart, so a closure that fits the challenge
    limit as an install fits as an update. The identity never sees the mode."""
    target = f"{inspection.component_id}@{inspection.version}"
    if mode == "upgrade":
        question = f"\U0001F510 Update specialist:{inspection.slug} to {target}?"
        action = "Approve to update; Deny to discard the staged fetch."
    else:
        question = f"\U0001F510 Install {target} as specialist:{inspection.slug}?"
        action = "Approve to install; Deny to discard the staged fetch."
    items: list[str] = []
    role_grants = getattr(inspection, "role_tool_grants", ()) or ()
    if role_grants:
        items.append(f"Casa tools: {', '.join(role_grants)}")
    items.append(f"Default persona: {inspection.default_persona_ref}")
    deps = ", ".join(f"{d.kind}:{d.identifier}" for d in inspection.dependencies)
    items.append(f"Dependencies: {deps or '(none)'}")
    digests = [
        f"Root digest (approved — component + dependencies): {inspection.root_digest}",
        f"Component checksum: {_short_digest(inspection.component_checksum)}",
    ]
    digests += [f"{d.kind}:{d.identifier}: {_short_digest(d.digest)}"
                for d in inspection.dependencies]
    for row in inspection.plugin_resolutions or ():
        items.append(f"Bundled plugin {row.scoped_name} (manifest name {row.manifest_name}, "
                     f"version {row.version}, source {row.source_type}):")
        if row.mcp_servers:
            items.append(f"  MCP servers: {', '.join(row.mcp_servers)}")
        if row.protected_tools:
            items.append(f"  Protected tools: {', '.join(row.protected_tools)}")
        if row.env_names:
            items.append(f"  Secrets required: {', '.join(row.env_names)}")
        exempt = getattr(row, "exempt_env_names", ()) or ()
        if exempt:
            items.append(
                "  Env referenced, not required (defaulted or provisioned by "
                f"the plugin's own setup): {', '.join(exempt)}")
        digests.append(f"plugin {row.scoped_name} content: "
                       f"{_short_digest(row.content_digest)}")
    return "\n".join([
        question,
        f"Mission: {inspection.mission}",
        "",
        *items,
        "",
        action,
        "",
        "Digests:",
        *digests,
    ])


def prompt_specialist_install_consent(
    *, coordinator: Any, channel: Any, chat_id: int, operator_id: int, inspection: Any,
    acks: "SpecialistInstallAckStore",
    reconcile_cb: "Callable[[], Awaitable[bool]] | None" = None,
    mode: str = "install",
    requester: str = "",
) -> Any:
    # #1445: ``requester`` keys the card to the run that asked (see
    # SpecialistInstallConsentKey).
    # #1225: ``mode`` is the inspect's own ("install" | "upgrade") and changes
    # wording only — it never enters the identity or the key below.
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
        component_id=inspection.component_id, slug=inspection.slug, identity=identity,
        requester=requester)
    text = render_install_consent_message(inspection, mode=mode)
    # #1225: the operation each finish edit names.
    noun, done = (("update", "updated") if mode == "upgrade"
                  else ("install", "installed"))

    def _on_commit_sync(idx: int, meta: dict) -> None:
        if idx == 0:
            acks.record(identity=identity, component_id=inspection.component_id,
                        version=inspection.version,
                        component_checksum=inspection.root_digest, slug=inspection.slug,
                        receipt_digest=receipt_digest)
            meta["acked"] = True

    def _finish_factory(message_id: int, req: Any) -> Callable[[dict], Any]:
        async def _finish(outcome: dict) -> None:
            o = outcome.get("outcome") if isinstance(outcome, dict) else None
            if o != "answered":
                import ask_retirement

                reason = outcome.get("reason") if isinstance(outcome, dict) else None
                # #933: the reason the broker delivered names the cause; a
                # withdrawal is not an expiry.
                await channel.edit_dm_message(
                    chat_id, message_id,
                    ask_retirement.retirement_headline(
                        f"{noun} consent for {inspection.slug!r}",
                        o, reason, consequence=f"nothing was {done}"),
                )
                return
            if outcome.get("option_index") == 0:
                if not req.meta.get("acked"):
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        f"internal error recording {noun} consent — re-run the {noun} to "
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
                        f"✅ Approved — the Configurator is carrying on with "
                        f"{inspection.slug!r}.",
                    )
                elif raised:
                    # A raise is a CONTRACT VIOLATION (no production callback
                    # can produce one), and it is the single branch where the
                    # outcome is genuinely unknown rather than known-negative:
                    # the callback may have handed the turn off before it blew
                    # up. Say uncertain, not "not started".
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        f"⚠️ Approved and saved — but the {noun} of "
                        f"{inspection.slug!r} hit an internal error and its "
                        "automatic start could not be confirmed. Ask the "
                        f"Configurator how it went; re-running the {noun} is safe "
                        "either way, and the approval recorded for this exact "
                        "version is reused if it still applies.",
                    )
                else:
                    await channel.edit_dm_message(
                        chat_id, message_id,
                        f"⚠️ Approved and saved — but the {noun} of "
                        f"{inspection.slug!r} was not started automatically. "
                        "Ask the Configurator to re-run the "
                        f"{noun}; the approval recorded for this exact version "
                        "is reused if it still applies.",
                    )
            else:
                await channel.edit_dm_message(
                    chat_id, message_id, f"❌ Denied — {inspection.slug!r} was not {done}",
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
