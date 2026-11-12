"""Structural operator-consent gate for specialist/persona installs — the
SAME shape as trigger_consent.py's plugin-trigger consent: a DM
Approve/Deny keyboard whose Approve synchronously persists a fail-closed,
atomically-written ack BEFORE any CAS write or activation. Never a checksum
string an LLM tool call could echo back itself.

Each card asks about one approval record (``approval_records``): it shows the
record's effective settings, binds the record's consent identity, and on
Approve moves the record to approved and has Casa carry the configurator on
(``configurator_continue.start``). One tap is one attempt: the run's governed
call uses the approval up. A newer request for the same component replaces the
card (:func:`supersede_card`)."""
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
                              slug: str, values_digest: str, receipt_digest: str = "") -> str:
    """The ONE identity-derivation function for a specialist-install consent
    decision (mirrors plugin_triggers.ack_identity). Binds the approval to
    the EXACT inspected component AND the exact settings it will stage — a
    re-fetch that changes any of these fields, or other effective settings,
    yields a different identity, so a stale approval can never ack a
    different artifact or other values.

    The bound digest is `inspection.root_digest` — compute_install_root_digest's
    full-closure digest (role+doctrine+config-schema+manifest+persona+
    dependencies), NOT `inspection.component_checksum` (the narrow 3-file
    digest, which is still a separate field on InspectionResult for
    CAS-neutral display/logging only). Binding consent to the narrow digest
    would let a persona/corpus/plugin substitution slip past an operator's
    approval unnoticed. The persisted ack-store field and the hashed input key
    keep their historical name, ``"component_checksum"``.

    `receipt_digest` binds a bundled-plugin receipt into the identity, so
    re-approval is required whenever the bundled-plugin closure changes; it
    joins the hashed payload only when non-empty. `values_digest` is
    ``approval_records.values_digest`` of the effective settings
    (``{"config": <the merged non-secret config the library will stage>,
    "secrets": <the sorted secret names provided>}``) and is required."""
    if not (isinstance(values_digest, str) and values_digest):
        raise ValueError("install_consent_identity needs the values digest")
    payload = {
        "component_id": component_id, "version": version,
        "component_checksum": root_digest, "slug": slug,
        "values_digest": values_digest,
    }
    if receipt_digest:
        payload["receipt_digest"] = receipt_digest
    return checksum_json(payload)


def identity_for_inspection(inspection: Any, *, values: dict) -> str:
    """The consent identity of *inspection* with the effective settings *values*
    (``{"config", "secrets"}``): the one builder every caller holding an
    inspection uses."""
    from approval_records import values_digest
    return install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug,
        receipt_digest=inspection.receipt_digest,
        values_digest=values_digest(values))


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
    # The approval record the card asks about: a newer request for the same
    # artifact and values gets a card of its own rather than this one.
    approval_rid: str = ""


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
            # The values digest is part of the identity: a record without one
            # cannot recompute to its key, so it fails the whole store closed
            # like any other identity mismatch.
            values_digest = rec.get("values_digest")
            if not (isinstance(values_digest, str) and values_digest):
                return {}
            if install_consent_identity(
                component_id=fields["component_id"], version=fields["version"],
                root_digest=fields["component_checksum"], slug=fields["slug"],
                receipt_digest=receipt_digest, values_digest=values_digest,
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
               component_checksum: str, slug: str, values_digest: str,
               receipt_digest: str = "") -> None:
        rec = {"component_id": component_id, "version": version,
               "component_checksum": component_checksum, "slug": slug,
               "values_digest": values_digest, "ts": int(time.time())}
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
        fields (its own `values_digest`, and its `receipt_digest`, "" for
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
                    values_digest=rec["values_digest"],
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


#: the marker on a setting the request did not submit: the install keeps it
KEPT_MARK = "(kept from the current settings)"


def _shown_value(value: Any) -> str:
    # Settings are text, so a value reads as it is typed. It is quoted as JSON
    # only where bare text would hide what it is: an empty value, one with
    # leading or trailing spaces, or one with a line break or another
    # unprintable character. Unsafe codepoints are spelled out either way, so
    # what the operator reads is what the approval binds.
    from text_util import escape_unsafe_text
    if isinstance(value, str) and value and value == value.strip() and value.isprintable():
        return escape_unsafe_text(value)
    return escape_unsafe_text(json.dumps(value, ensure_ascii=False, sort_keys=True))


def render_settings_lines(values: dict, submitted_config: "Any" = None) -> "list[str]":
    """The settings an approval binds, as card lines: each effective config key
    with its value, a key the request did not submit marked as kept from the
    current settings, and the secret names provided (never their values).
    *submitted_config* is the request's own config; ``None`` marks nothing."""
    config = values.get("config") or {}
    secrets = list(values.get("secrets") or [])
    submitted = None if submitted_config is None else set(submitted_config)
    if not config and not secrets:
        return ["Settings: none"]
    lines: list[str] = []
    if config:
        lines.append("Settings:")
        for name in sorted(config):
            line = f"  {_shown_name(name)} = {_shown_value(config[name])}"
            if submitted is not None and name not in submitted:
                line += f" {KEPT_MARK}"
            lines.append(line)
    if secrets:
        lines.append(f"Secrets provided (names only): {', '.join(_shown_name(n) for n in secrets)}")
    return lines


def _shown_name(name: Any) -> str:
    from text_util import escape_unsafe_text
    return escape_unsafe_text(str(name))


def render_install_consent_message(inspection: Any, *, mode: str = "install",
                                   values: "dict | None" = None,
                                   submitted_config: Any = None) -> str:
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
    limit as an install fits as an update. The identity never sees the mode.

    *values*, when given, are the effective settings the approval binds
    (``{"config", "secrets"}``), shown after what the component comes with
    (:func:`render_settings_lines`); *submitted_config* names the keys the
    request itself supplied."""
    target = f"{inspection.component_id}@{inspection.version}"
    if mode == "upgrade":
        question = f"\U0001F510 Update specialist:{inspection.slug} to {target}?"
        action = ("Approve to update with these settings; Deny to discard the staged fetch."
                  if values is not None else
                  "Approve to update; Deny to discard the staged fetch.")
    else:
        question = f"\U0001F510 Install {target} as specialist:{inspection.slug}?"
        action = ("Approve to install with these settings; Deny to discard the staged fetch."
                  if values is not None else
                  "Approve to install; Deny to discard the staged fetch.")
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
        *([""] + render_settings_lines(values, submitted_config) if values is not None else []),
        "",
        action,
        "",
        "Digests:",
        *digests,
    ])


def install_consent_text(inspection: Any, *, mode: str = "install",
                         values: "dict | None" = None, submitted_config: Any = None) -> str:
    """The consent text an install or update card falls back to, and what its
    registration measures: :func:`render_install_consent_message`, cut to its
    leading whole lines when it is over the challenge size limit (#1541), so a
    large bundle is still asked about rather than refused. A text within the
    limit is the rendered message unchanged."""
    import approval_card
    from authz_grants import _CHALLENGE_MAX_CHARS
    return approval_card.fit_lines(
        render_install_consent_message(
            inspection, mode=mode, values=values, submitted_config=submitted_config),
        _CHALLENGE_MAX_CHARS)


def install_card_request(
    inspection: Any, *, mode: str = "install", values: "dict | None" = None,
    submitted_config: Any = None, requester_name: str = "Casa",
    from_version: str = "",
    protected_tools: "dict[str, list[dict]] | None" = None,
) -> Any:
    """The approval card asked for installing or updating *inspection*: the
    facts a model writes it from, and, as the fallback, the consent text
    (:func:`install_consent_text`), kept literally. Display only — the
    approval stays bound to the record's consent identity.

    The facts are the renderer's own inputs, digests left out, plus what the
    caller knows beyond them: *from_version* (the installed version an update
    replaces) and *protected_tools* (per bundled plugin's scoped name, its
    protected tools as ``{"name", "summary"}`` from the plugin's manifest; a
    plugin missing from it is listed by tool name)."""
    import approval_card

    text = install_consent_text(
        inspection, mode=mode, values=values, submitted_config=submitted_config)
    grants = getattr(inspection, "role_tool_grants", ()) or ()
    env_set = _env_names_set() if inspection.plugin_resolutions else set()
    facts: dict = {"requested_by": requester_name,
                   "request": "update" if mode == "upgrade" else "install",
                   "specialist": inspection.slug,
                   "component": inspection.component_id}
    if mode == "upgrade":
        if from_version:
            facts["from_version"] = from_version
        facts["to_version"] = inspection.version
    else:
        facts["version"] = inspection.version
    facts.update({
        "mission": inspection.mission,
        "casa_tools_granted": list(grants),
        "default_persona": inspection.default_persona_ref,
        "dependencies": [f"{d.kind}:{d.identifier}" for d in inspection.dependencies],
        "bundled_plugins": [
            _plugin_facts(row, (protected_tools or {}).get(row.scoped_name), env_set)
            for row in inspection.plugin_resolutions or ()],
    })
    if values is not None:
        config = values.get("config") or {}
        submitted = None if submitted_config is None else set(submitted_config)
        facts["settings"] = dict(config)
        if submitted is not None:
            facts["settings_kept_from_current"] = sorted(
                str(n) for n in config if n not in submitted)
        facts["secrets_provided_names_only"] = list(values.get("secrets") or [])
    return approval_card.CardRequest(
        kind="specialist_upgrade" if mode == "upgrade" else "specialist_install",
        who=requester_name, facts=facts, fallback=approval_card.text_fallback(text))


def _plugin_facts(row: Any, tools: "list[dict] | None", already_set: "set[str]") -> dict:
    if tools is None:
        tools = [{"name": n, "summary": None} for n in row.protected_tools]
    required = list(row.env_names)
    facts = {"name": row.scoped_name, "manifest_name": row.manifest_name,
             "version": row.version, "source": row.source_type,
             "mcp_servers": list(row.mcp_servers),
             "protected_tools": [dict(t) for t in tools],
             "secrets_required": required,
             "env_referenced_not_required": list(getattr(row, "exempt_env_names", ()) or ())}
    # #1566: an update usually keeps settings an earlier install wired; without
    # this the card read every required name as something still to give.
    set_now = [n for n in required if n in already_set]
    if set_now:
        facts["secrets_required_already_set"] = set_now
    return facts


def _env_names_set() -> "set[str]":
    """The plugin env names that already hold a value. Never raises: an
    unreadable file only means the card says less."""
    import plugin_env_conf
    try:
        return {n for n, v in plugin_env_conf.read_entries().items() if v}
    except Exception:  # noqa: BLE001 — display only
        logger.warning("plugin env read for the approval card failed", exc_info=True)
        return set()


# --- the approval card, shared by the specialist and persona cards -------------

#: the broker cancellation reason of a card a newer request replaced
SUPERSEDED_REASON = "approval_superseded"

NOT_STARTED_LINE = ("⚠️ Approved and saved — but the configurator could not be started. "
                    "Ask Ellen again; the approval is kept.")

_DONE_WORD = {"specialist_install": "installed", "specialist_upgrade": "updated",
              "persona_install": "installed"}


def replaced_line(record: Any) -> str:
    """What a card a newer request replaced says."""
    return (f"Replaced by a newer request; nothing was "
            f"{_DONE_WORD.get(getattr(record, 'kind', ''), 'installed')}.")


def _cancel_open_card(rid: str) -> bool:
    """Settle the open question of the card asking about *rid*, if there is
    one: its finish hook then edits the card. Synchronous."""
    import verdict_broker
    return bool(verdict_broker.BROKER.cancel_where(
        namespace="resident_ask", reason=SUPERSEDED_REASON,
        predicate=lambda req: isinstance(req.meta, dict)
        and req.meta.get("approval_rid") == rid))


def _card_endpoint(card: Any) -> Any:
    try:
        import tools
        transport = tools._transport()
        return transport.endpoint(card) if transport is not None else None
    except Exception:  # noqa: BLE001 — no endpoint, no edit
        logger.warning("no Telegram endpoint for the approval card %s", card, exc_info=True)
        return None


_EDITS: "set[Any]" = set()


def supersede_card(record: Any, *, channel: Any = None) -> "Awaitable[None]":
    """Replace the card of *record*, a record a newer request superseded.

    A card whose question is still open is settled now (synchronously), and its
    own finish hook edits it to say it was replaced, so a later tap finds
    nothing to answer; a card answered already (approved, its run not started)
    is edited directly through *channel*, or the endpoint of the card's bot.
    The edit runs as its own task; the returned awaitable waits for it, and
    awaiting it is optional."""
    import asyncio
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    done.set_result(None)
    if _cancel_open_card(record.rid):
        return done
    card = getattr(record, "card", None)
    if card is None or card.message_id is None:
        return done
    endpoint = channel if channel is not None else _card_endpoint(card)
    if endpoint is None:
        return done

    async def _edit() -> None:
        try:
            await endpoint.edit_dm_message(card.chat_id, card.message_id, replaced_line(record),
                                           literal=True)
        except Exception:  # noqa: BLE001 — a failed edit leaves the card as it was
            logger.warning("could not edit the replaced approval card %s", card, exc_info=True)

    task = loop.create_task(_edit())
    _EDITS.add(task)
    task.add_done_callback(_EDITS.discard)
    return task


def _store_step(step: "Callable[[str], Any]", rid: str) -> None:
    import approval_records
    try:
        step(rid)
    except approval_records.ApprovalStoreError:
        logger.exception("approval record %s could not be saved", rid)


def approval_card_hooks(
    *, record: Any, channel: Any, chat_id: int, continuation: str, name: str,
    noun: str, record_ack: "Callable[[], bool]", not_recorded_text: str,
) -> "tuple[Callable[[int, dict], None], Callable[[int, Any], Callable[[dict], Any]]]":
    """The tap and finish hooks of an approval card about *record*.

    Approve records the consent ack in the tap's synchronous step
    (*record_ack*), then the finish moves the record to approved and starts the
    configurator's continuation (*continuation*) with it; the card is edited
    when the continuation tells whether it started. Deny and an unanswered
    card close the record. When the card is posted its address is set on the
    record, and a record already superseded by then has its question settled
    at once, so the card says it was replaced."""
    import approval_records
    from channels.tg_address import TgAddress

    rid = record.rid
    done = _DONE_WORD.get(record.kind, "installed")
    replaced = replaced_line(record)

    def _on_commit_sync(idx: int, meta: dict) -> None:
        if idx == 0:
            try:
                meta["acked"] = bool(record_ack())
            except Exception:  # noqa: BLE001 — an unrecorded ack is the finish's error line
                logger.exception("could not record the consent ack of approval %s", rid)

    def _note_card(message_id: int, req: Any) -> None:
        try:
            noted = approval_records.set_card(
                rid, TgAddress(int(channel.bot_id), int(chat_id), int(message_id)))
        except Exception:  # noqa: BLE001 — the card works without its address
            logger.exception("could not record the card of approval %s", rid)
            return
        if noted.state == "superseded" and getattr(req, "request_id", None):
            # A newer request replaced this one while its card was being
            # posted: settle the question now; the finish hook says so.
            import verdict_broker
            verdict_broker.BROKER.cancel(
                namespace=req.namespace, scope=req.scope, request_id=req.request_id,
                reason=SUPERSEDED_REASON)

    def _finish_factory(message_id: int, req: Any) -> Callable[[dict], Any]:
        async def _edit(text: str) -> None:
            await channel.edit_dm_message(chat_id, message_id, text, literal=True)

        async def _on_outcome(outcome: str) -> None:
            if outcome == "started":
                await _edit(f"✅ Approved — the Configurator is carrying on with {name!r}.")
            elif outcome in ("busy", "failed"):
                await _edit(NOT_STARTED_LINE)

        async def _finish(outcome: dict) -> None:
            o = outcome.get("outcome") if isinstance(outcome, dict) else None
            if o != "answered":
                reason = outcome.get("reason") if isinstance(outcome, dict) else None
                if reason == SUPERSEDED_REASON:
                    await _edit(replaced)
                    return
                _store_step(approval_records.expire, rid)
                import ask_retirement
                # #933: the reason the broker delivered names the cause; a
                # withdrawal is not an expiry.
                await _edit(ask_retirement.retirement_headline(
                    f"{noun} consent for {name!r}", o, reason,
                    consequence=f"nothing was {done}"))
                return
            if outcome.get("option_index") != 0:
                _store_step(approval_records.deny, rid)
                await _edit(f"❌ Denied — {name!r} was not {done}")
                return
            if not req.meta.get("acked"):
                await _edit(not_recorded_text)
                return
            try:
                approved = approval_records.approve(rid)
            except approval_records.ApprovalStoreError:
                logger.exception("approval record %s could not be saved", rid)
                await _edit(not_recorded_text)
                return
            if approved is None:
                await _edit(replaced)
                return
            import configurator_continue
            try:
                started = configurator_continue.start(
                    continuation, key=rid, approval=approved, asked=approved.asked,
                    on_outcome=_on_outcome)
            except Exception:  # noqa: BLE001 — a tap hook never raises
                logger.exception("the configurator continuation for approval %s "
                                 "could not be started", rid)
                await _edit(NOT_STARTED_LINE)
                return
            if started == "superseded":
                await _edit(replaced)

        _note_card(message_id, req)
        return _finish

    return _on_commit_sync, _finish_factory


def prompt_specialist_install_consent(
    *, coordinator: Any, channel: Any, chat_id: int, operator_id: int,
    record: Any, inspection_facts: Any, continuation: str,
    submitted_config: Any = None,
    acks: "SpecialistInstallAckStore | None" = None,
    requester: str = "",
    requester_name: str = "Casa",
    from_version: str = "",
    protected_tools: "dict[str, list[dict]] | None" = None,
) -> Any:
    """Post the card asking the operator to approve *record*, a
    ``specialist_install`` or ``specialist_upgrade`` approval record, for the
    staged component *inspection_facts* (an ``InspectionResult``).

    The card shows the record's effective settings, marking those the request
    did not submit (*submitted_config*, the request's own config) as kept from
    the current settings. Approve records the consent ack for the record's
    identity, approves the record and has Casa carry the configurator on with
    *continuation*. Raises ``ValueError`` when the record's identity is not the
    identity of *inspection_facts* with the record's values.

    The card is written for the operator by a model (:func:`install_card_request`),
    titled for *requester_name*, the agent whose turn asked; *from_version*
    and *protected_tools* only add to what it is told. A consent text over the
    size limit is cut to fit (:func:`install_consent_text`), never refused.
    *requester* is the asking run's id, which keeps its card apart."""
    inspection = inspection_facts
    mode = "upgrade" if record.kind == "specialist_upgrade" else "install"
    receipt_digest = inspection.receipt_digest
    identity = install_consent_identity(
        component_id=inspection.component_id, version=inspection.version,
        root_digest=inspection.root_digest, slug=inspection.slug,
        receipt_digest=receipt_digest, values_digest=record.values_digest,
    )
    if identity != record.consent_identity:
        # The ack store recomputes every identity from its fields at load; an
        # ack whose fields do not give its key would fail the whole store.
        raise ValueError("the approval record is not for this staged component")
    acks = acks if acks is not None else SpecialistInstallAckStore()
    # #1445: ``requester`` keys the card to the run that asked; the record's
    # id keeps a newer request's card apart from the one it replaces.
    key = SpecialistInstallConsentKey(
        component_id=inspection.component_id, slug=inspection.slug, identity=identity,
        requester=requester, approval_rid=record.rid)
    card_request = install_card_request(
        inspection, mode=mode, values=record.values, submitted_config=submitted_config,
        requester_name=requester_name, from_version=from_version,
        protected_tools=protected_tools)
    text = install_consent_text(
        inspection, mode=mode, values=record.values, submitted_config=submitted_config)
    noun = "update" if mode == "upgrade" else "install"

    def _record_ack() -> bool:
        acks.record(identity=identity, component_id=inspection.component_id,
                    version=inspection.version,
                    component_checksum=inspection.root_digest, slug=inspection.slug,
                    receipt_digest=receipt_digest, values_digest=record.values_digest)
        return True

    on_commit_sync, finish_factory = approval_card_hooks(
        record=record, channel=channel, chat_id=chat_id, continuation=continuation,
        name=inspection.slug, noun=noun, record_ack=_record_ack,
        not_recorded_text=(f"internal error recording {noun} consent — re-run the {noun} "
                           "to be prompted again"))

    # The card is Casa's own: *channel* is Casa's endpoint, and the card's
    # broker scope carries that bot.
    return coordinator.register_challenge(
        key, bot_id=channel.bot_id, chat_id=chat_id, operator_id=operator_id,
        channel=channel,
        challenge_text=text, options=["Approve", "Deny"],
        on_commit_sync=on_commit_sync, finish_factory=finish_factory,
        kind="specialist_install_consent",
        meta_extra={"install_slug": inspection.slug,
                    "install_component_id": inspection.component_id,
                    "approval_rid": record.rid},
        timeout_s=INSTALL_CONSENT_TTL_S,
        card_request=card_request,
    )
