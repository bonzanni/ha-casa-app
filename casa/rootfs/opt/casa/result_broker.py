"""#792: the plugin result contract and its escrow broker.

A third-party plugin's MCP tool can return a live capability — a sign-in link,
a one-time code, a bearer token — and until this module existed Casa's first
sight of those bytes was the model's echo of them, on its way to the chat.
Casa never proxies a plugin's MCP server (the CLI talks to it directly), so the
seam Casa owns is the CLI's hook protocol, and the boundary is built there:

* **The contract** (``casa.resultContract`` in ``plugin.json``, extracted by
  ``plugin_store.manifest_result_contract``, mapped by
  ``plugin_grants.result_contract_map``) is the PRODUCER's declaration of which
  of its tools return a capability and which consume one. Casa validates its
  shape and never infers or supplements it (#785).
* **Admission** (:func:`make_plugin_admission_hook`, PreToolUse): a non-setup
  tool of a plugin that has NOT adopted the contract is refused BEFORE it runs.
  The exact ``casa.setupTool`` is exempt — its consent URL is delivered to the
  operator by design. For an adopting plugin the same callback runs the
  authorization decision for a protected tool and, only if that allowed the
  call, arms the references the call consumes — in ONE callback, because the
  SDK dispatches same-event matchers concurrently and arming must follow the
  authorization decision.
* **Escrow** (:class:`ReferenceStore`): a ``capability`` tool DEPOSITS the
  value with Casa over the internal Unix socket DURING the call and returns a
  ``casa-cap-<32hex>`` reference in its place. The value never enters the MCP
  result, the model's context, the transcript or its ``mcpMeta`` sidecar.
  A reference is bound to the call's grant identity (whoever could consume an
  approval for the same call — ``authz_grants.resolve_grant_identity``),
  single-use, TTL-bound (one approval keyboard plus one grant window),
  memory-only and so restart-losing.
* **Redemption**: a ``consumes`` parameter of a tool of the same plugin, in a
  session with the same grant identity, is rewritten by admission to
  ``<reference>:<ticket>``; the consumer plugin redeems it over the internal
  socket. The ticket exists only in the rewritten input the tool receives —
  the model's own message carries the bare reference (measured: the rewritten
  argument is not recorded in the transcript), so a parallel tool cannot
  present it.
* **Replacement** (:func:`make_result_hook`, PostToolUse): the second
  boundary. A result for a non-adopting plugin's non-setup tool is replaced by
  a withheld notice before the model sees it; a ``capability`` result passes
  only when every declared slot carries the reference of a deposit bound to
  that very call. :func:`make_failure_hook` (PostToolUseFailure) can replace
  nothing — the event has no replacement field — and only closes the call.
* **Delivery** (#1015): a slot the tool declares ``delivers`` as an
  ``operator_link`` never reaches the model at all. After the structural
  check, the same PostToolUse hook takes the deposit once and posts ONE
  labelled-link message to the chat of the call's grant identity — the chat
  the operator asked in, never a task topic — with the destination host
  printed by Casa from the URL. Proven delivery REPLACES the result with a
  receipt (``casa_delivery.status = "delivered"``); anything short of it
  withholds the result and drops the deposit. The producer's own result is
  delivery-neutral: the CLI abandons a hook past its matcher timeout and
  lets the original result through, so the receipt is the only carrier of
  the positive claim, and a result without one claims nothing.

Scope (the invariant's own): sessions that carry the authorization seam —
resident, delegated specialist, specialist engagement. Executor sessions are
outside it (#923). A producer that breaks its declared contract is the
plugin-author trust boundary #785 rules on; Casa detects no such violation.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hmac
import json
import logging
import re
import secrets
import threading
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from aiohttp import web

logger = logging.getLogger(__name__)

PLUGIN_TOOL_PREFIX = "mcp__plugin_"
PLUGIN_TOOL_MATCHER = "mcp__plugin_.*"    # measured on CLI 2.1.220: a regex
REFERENCE_PREFIX = "casa-cap-"
_REF_RE = re.compile(r"^casa-cap-[0-9a-f]{32}$")
_TICKET_RE = re.compile(r"^[0-9a-f]{32}$")
BROKER_SOCKET_PATH = "/run/casa/internal.sock"
ENV_CLIENT = "CASA_BROKER_CLIENT"
ENV_SOCKET = "CASA_BROKER_SOCKET"
# An in-flight call or an arm is never held open by a call that will not
# return (authz denial and cancellation produce no terminal hook event): both
# are time-capped and superseded by the next qualifying call.
INFLIGHT_CAP_S = 600.0
MAX_VALUE_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
# #1015: operator-link delivery. The hook's own bound on a slow Telegram —
# NOT the ordering guarantee: the CLI's matcher deadline runs independently of
# this process, which is why only the replacement receipt carries the claim.
DELIVERY_TIMEOUT_S = 20.0
# The matcher timeout the three broker matchers set EXPLICITLY (the SDK's
# default is also 60 s; a default is not a commitment).
HOOK_TIMEOUT_S = 60.0
MAX_LINK_BYTES = 2048
MAX_CAPTION_CHARS = 200
MAX_LABEL_CHARS = 40
DEFAULT_LINK_LABEL = "Open"
DELIVERED_TO = "operator_chat"
OPERATOR_LINK = "operator_link"
_WS_RE = re.compile(r"\s")
_DOMAINISH_RE = re.compile(r"\.[A-Za-z]")


def reference_ttl_s() -> float:
    """Longest a conforming flow can legitimately wait between minting and
    redeeming: one approval keyboard plus one grant window — both constants
    are ``authz_grants``', never a fresh literal here."""
    from authz_grants import DEFAULT_GRANT_TTL_S, _CHALLENGE_TTL_S
    return float(_CHALLENGE_TTL_S) + float(DEFAULT_GRANT_TTL_S)


def new_client_id() -> str:
    return secrets.token_hex(16)


def new_reference() -> str:
    return REFERENCE_PREFIX + secrets.token_hex(16)


def is_reference(value: Any) -> bool:
    return isinstance(value, str) and bool(_REF_RE.fullmatch(value))


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _InFlight:
    client_id: str
    artifact_id: str
    tool_name: str
    tool_use_id: str
    identity: Any                 # authz_grants.GrantIdentity
    provides: tuple
    opened_at: float
    deposits: dict = dataclasses.field(default_factory=dict)   # slot -> ref
    delivers: dict = dataclasses.field(default_factory=dict)   # slot -> kind (#1015)


@dataclasses.dataclass
class _Reference:
    value: str
    slot: str
    identity: Any
    minted_at: float
    expires_at: float
    armed: tuple | None = None    # (client_id, tool_use_id, ticket)
    used: bool = False
    caption: str = ""             # #1015: delivered slots only, validated
    label: str = ""

    def __repr__(self) -> str:      # never the value
        return (f"_Reference(slot={self.slot!r}, armed={self.armed is not None}, "
                f"used={self.used})")


# -- #1015: what a delivered slot's deposit may carry ------------------------

def _link_ok(value: Any) -> bool:
    """``https``, a hostname, ≤ MAX_LINK_BYTES, printable, no whitespace."""
    if not isinstance(value, str) or not value:
        return False
    if len(value.encode("utf-8", "surrogateescape")) > MAX_LINK_BYTES:
        return False
    if not value.isprintable() or _WS_RE.search(value):
        return False
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        return False
    return parts.scheme == "https" and bool(host)


def _text_ok(value: Any, limit: int) -> bool:
    """A single printable line of at most *limit* characters that cannot
    itself read as a link: no scheme separator, no ``www.``."""
    if not isinstance(value, str) or len(value) > limit or not value.isprintable():
        return False
    lowered = value.lower()
    return "://" not in lowered and "www." not in lowered


def _caption_ok(value: Any) -> bool:
    return _text_ok(value, MAX_CAPTION_CHARS)


def _label_ok(value: Any) -> bool:
    """A label additionally may not look like a domain (``.`` followed by a
    letter): the host is Casa's to print from the URL, never the plugin's."""
    return _text_ok(value, MAX_LABEL_CHARS) and not _DOMAINISH_RE.search(value)


class ReferenceStore:
    """Thread-safe, memory-only escrow of deposited capabilities (#792).

    Everything here dies with the process (restart-losing by construction)
    and nothing is ever serialized. ``now`` is injectable so tests drive TTL
    expiry without sleeping, the ``GrantStore`` precedent."""

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._now = now
        self._refs: dict[str, _Reference] = {}
        self._inflight: dict[tuple[str, str], _InFlight] = {}

    # -- sweeping ---------------------------------------------------------
    def _sweep_locked(self) -> None:
        now = self._now()
        for ref, r in list(self._refs.items()):
            if r.used or r.expires_at <= now:
                del self._refs[ref]
        for key, call in list(self._inflight.items()):
            if call.opened_at + INFLIGHT_CAP_S <= now:
                # A swept call's result, if it ever arrives, is withheld
                # (no call in flight): its deposits go with it, exactly as
                # `drop_call_deposits` drops them on any withholding.
                for ref in call.deposits.values():
                    self._refs.pop(ref, None)
                del self._inflight[key]

    def sweep(self) -> None:
        with self._lock:
            self._sweep_locked()

    # -- in-flight calls --------------------------------------------------
    def open_call(self, *, client_id: str, artifact_id: str, tool_name: str,
                  tool_use_id: str, identity, provides: tuple,
                  delivers: dict | None = None) -> None:
        """Register a ``capability`` call at admission. The identity is stored
        HERE, atomically with the call, so a deposit can bind it: the plugin's
        deposit request carries no identity and asserts none. ``delivers``
        (#1015) is the contract's ``{slot: kind}`` — the slot whose deposit
        the result hook delivers instead of passing."""
        with self._lock:
            self._sweep_locked()
            self._inflight[(client_id, tool_use_id)] = _InFlight(
                client_id=client_id, artifact_id=artifact_id,
                tool_name=tool_name, tool_use_id=tool_use_id,
                identity=identity, provides=tuple(provides),
                opened_at=self._now(), delivers=dict(delivers or {}))

    def close_call(self, client_id: str, tool_use_id: str):
        """Close a call (PostToolUse / PostToolUseFailure). Returns the record
        or ``None``. Deposits the result did not reference die with it."""
        with self._lock:
            return self._inflight.pop((client_id, tool_use_id), None)

    # -- deposit ----------------------------------------------------------
    def deposit(self, *, client_id: str, slot: str, value: str,
                caption: str | None = None,
                label: str | None = None) -> tuple[str | None, str | None]:
        """Bind ``value`` to the UNIQUE in-flight capability call of
        ``client_id`` whose contract provides ``slot``; mint and return a
        reference. Zero or more than one such call ⇒ refused (fail closed).
        Returns ``(reference, None)`` or ``(None, error_code)``.

        For a slot the call DELIVERS (#1015) the value must be an ``https``
        link Casa can post, and the optional ``caption``/``label`` must be
        printable single lines that cannot read as a link themselves —
        ``bad_link`` / ``bad_caption`` / ``bad_label`` otherwise, BEFORE any
        reference is minted. For any other slot both are ignored, so a
        producer library can send them uniformly."""
        with self._lock:
            self._sweep_locked()
            matches = [c for c in self._inflight.values()
                       if c.client_id == client_id and slot in c.provides]
            if not matches:
                return None, "no_call_in_flight"
            if len(matches) > 1:
                return None, "ambiguous_call"
            call = matches[0]
            if call.identity is None:
                return None, "no_identity"
            if slot in call.deposits:
                return None, "slot_already_deposited"
            caption_s, label_s = "", ""
            if slot in call.delivers:
                # Only a DELIVERED slot judges the metadata (type included):
                # for any other slot both fields are ignored whatever they
                # are, exactly as the base ignored unknown request members.
                if not _link_ok(value):
                    return None, "bad_link"
                if caption is not None and caption != "":
                    if not _caption_ok(caption):
                        return None, "bad_caption"
                    caption_s = caption
                if label is not None and label != "":
                    if not _label_ok(label):
                        return None, "bad_label"
                    label_s = label
            ref = new_reference()
            now = self._now()
            self._refs[ref] = _Reference(
                value=value, slot=slot, identity=call.identity,
                minted_at=now, expires_at=now + reference_ttl_s(),
                caption=caption_s, label=label_s)
            call.deposits[slot] = ref
            return ref, None

    def take_for_delivery(self, reference: str):
        """#1015: release a delivered slot's deposit ONCE to the result hook —
        the reference must exist, be unexpired, unused and unarmed; it is
        marked used (so ``arm``, ``redeem`` and a second take all refuse it;
        the next sweep removes it) and its value blanked. Returns
        ``(value, caption, label, identity)`` or ``None``."""
        with self._lock:
            self._sweep_locked()
            r = self._refs.get(reference)
            if r is None or r.used or r.armed is not None:
                return None
            r.used = True
            value, r.value = r.value, ""
            return value, r.caption, r.label, r.identity

    def validate_result(self, call: _InFlight, parsed: dict) -> bool:
        """True iff every declared slot of ``call`` is present in ``parsed``
        with EXACTLY the reference of the deposit bound to this call."""
        if not isinstance(parsed, dict):
            return False
        for slot in call.provides:
            expected = call.deposits.get(slot)
            if expected is None or parsed.get(slot) != expected:
                return False
        return True

    def is_no_link_result(self, call: _InFlight, parsed: dict) -> bool:
        """#1015 addendum (v0.319.0): True iff ``call`` delivers a slot, no
        deposit is bound to it, and ``parsed`` carries EVERY slot the call
        provides as JSON ``null`` — the producer's explicit statement that it
        created no link this time. A missing member or any other falsy value
        is not that statement."""
        if not isinstance(parsed, dict):
            return False
        with self._lock:
            return (bool(call.delivers) and not call.deposits
                    and bool(call.provides)
                    and all(slot in parsed and parsed[slot] is None
                            for slot in call.provides))

    def drop_call_deposits(self, call: _InFlight) -> None:
        """A capability result that failed validation was withheld: the
        references it minted must not be redeemable through a notice the
        model never saw."""
        with self._lock:
            for ref in call.deposits.values():
                self._refs.pop(ref, None)

    # -- arming and redemption ---------------------------------------------
    def arm(self, *, reference: str, identity, slot: str, client_id: str,
            tool_use_id: str) -> str | None:
        """Arm ``reference`` for redemption by the call ``tool_use_id`` in
        ``client_id``. Requires the same grant identity and the same slot.
        Re-arming replaces the previous arm and its ticket. Returns the
        ticket, or ``None`` when the reference is not redeemable here."""
        with self._lock:
            self._sweep_locked()
            r = self._refs.get(reference)
            if r is None or r.used or r.identity != identity or r.slot != slot:
                return None
            ticket = secrets.token_hex(16)
            r.armed = (client_id, tool_use_id, ticket)
            return ticket

    def redeem(self, *, client_id: str, reference: str, ticket: str) -> tuple[str | None, str | None]:
        """Release the value ONCE to the consumer whose admission armed it:
        same client, same ticket, arming call still in flight. Returns
        ``(value, None)`` or ``(None, error_code)``; never a value on error."""
        with self._lock:
            self._sweep_locked()
            r = self._refs.get(reference)
            if r is None or r.used:
                return None, "unknown_or_used"
            if r.armed is None:
                return None, "not_armed"
            armed_client, armed_call, armed_ticket = r.armed
            if armed_client != client_id:
                return None, "wrong_client"
            if not (isinstance(ticket, str)
                    and hmac.compare_digest(armed_ticket, ticket)):
                return None, "bad_ticket"
            if (armed_client, armed_call) not in self._inflight:
                return None, "call_not_in_flight"
            r.used = True
            value, r.value = r.value, ""
            del self._refs[reference]
            return value, None

    # -- lifecycle --------------------------------------------------------
    def purge_artifact(self, artifact_id: str) -> int:
        with self._lock:
            victims = [k for k, r in self._refs.items()
                       if getattr(r.identity, "artifact_id", None) == artifact_id]
            for k in victims:
                del self._refs[k]
            calls = [k for k, c in self._inflight.items()
                     if c.artifact_id == artifact_id]
            for k in calls:
                del self._inflight[k]
            return len(victims)

    def purge_role(self, role: str) -> int:
        with self._lock:
            victims = [k for k, r in self._refs.items()
                       if getattr(r.identity, "enforcement_role", None) == role]
            for k in victims:
                del self._refs[k]
            calls = [k for k, c in self._inflight.items()
                     if getattr(c.identity, "enforcement_role", None) == role]
            for k in calls:
                del self._inflight[k]
            return len(victims)

    # -- counts (tests) ---------------------------------------------------
    def reference_count(self) -> int:
        with self._lock:
            self._sweep_locked()
            return len(self._refs)

    def inflight_count(self) -> int:
        with self._lock:
            self._sweep_locked()
            return len(self._inflight)


STORE = ReferenceStore()


# ---------------------------------------------------------------------------
# Hook payload helpers
# ---------------------------------------------------------------------------


def _withheld(plugin_seg: str, reason: str) -> dict[str, Any]:
    """The PostToolUse replacement. Quotes no result bytes and no field names;
    the plugin segment is the one the model already sees in the tool name."""
    body = json.dumps({
        "casa_result_withheld": True,
        "plugin": plugin_seg,
        "reason": reason,
    })
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                   "updatedToolOutput": body}}


_REASON_NON_ADOPTING = (
    "The plugin has not adopted the Casa result contract (casa.resultContract), "
    "so its tool results are withheld. The tool ran; its result was not "
    "delivered. Tell the operator the plugin needs updating; do not retry.")
_REASON_UNDECLARED = (
    "The tool is not declared under the plugin's result contract, so its "
    "result is withheld. Tell the operator the plugin needs updating; do not "
    "retry.")
_REASON_UNKNOWN_PLUGIN = (
    "The plugin could not be matched to a resolved artifact, so its result is "
    "withheld. Tell the operator; do not retry.")
_REASON_BAD_CAPABILITY = (
    "The tool is declared to return a capability, but its result did not carry "
    "the references its contract declares, so the result is withheld. Tell the "
    "operator the plugin needs updating; do not retry.")
_REASON_LINK_NOT_DELIVERED = (
    "The tool produced a link for the operator, but Casa could not confirm it "
    "reached their chat, so the result is withheld. Tell the operator: if a "
    "link message arrived just now it is valid; otherwise ask again for a "
    "fresh one. Do not retry on this turn.")

_DENY_NON_ADOPTING = (
    "not executed: this plugin has not adopted the Casa result contract "
    "(casa.resultContract), so its tools other than its setup tool are refused. "
    "Tell the operator the plugin needs updating; do not retry.")
_DENY_UNDECLARED = (
    "not executed: this tool is not declared under the plugin's result "
    "contract. Tell the operator the plugin needs updating; do not retry.")
_DENY_UNKNOWN_PLUGIN = (
    "not executed: the plugin could not be matched to a resolved artifact. "
    "Tell the operator; do not retry.")
_DENY_NO_IDENTITY = (
    "not executed: a tool that returns a capability can only be called on a "
    "turn bound to the configured operator (a direct message, a button, or an "
    "active engagement). Do not retry on this turn.")
_DENY_STALE_REFERENCE = (
    "not executed: a capability reference passed to this call is not "
    "redeemable here (unknown, already used, expired, minted for another "
    "session, or for a different capability). Fetch a fresh one; do not retry "
    "with the same reference.")
_DENY_INTERNAL = "not executed: internal result-broker error"


def _deny(reason: str) -> dict[str, Any]:
    from hooks import _deny as _hooks_deny
    return _hooks_deny(reason)


def _response_text(tool_response: Any) -> str | None:
    """Fold the CLI's projection of an MCP result to one string: a ``str``
    (measured: a JSON string for a structured result) or a list of content
    blocks (measured: a dict-returning tool). Anything else ⇒ ``None``."""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, list):
        parts = []
        for block in tool_response:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    return None
                parts.append(text)
            else:
                return None
        return "".join(parts)
    return None


def _parse_object(text: str) -> dict | None:
    if len(text) > MAX_RESPONSE_BYTES:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# The hooks
# ---------------------------------------------------------------------------


def make_plugin_admission_hook(
    role: str,
    contract_map,
    *,
    client_id: str,
    authz_hook: Callable | None = None,
    protected: dict | None = None,
    store: ReferenceStore | None = None,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """The composite PreToolUse callback for plugin tools (#792 §3.3).

    In order: contract admission (setup exemption; non-adopting or undeclared
    ⇒ deny before execution; a capability call registers in flight), then the
    authorization decision for a PROTECTED tool (``authz_hook`` — the
    ``make_resident_authz_hook`` callable, invoked as a function; its texts and
    behaviour unchanged), then — only if both allowed — arming of the
    references in declared ``consumes`` parameters, with ``updatedInput``.
    Every exception is a deny, never a pass."""
    store = store or STORE
    protected = protected or {}

    async def _hook(input_data, tool_use_id, context):
        tool_name = (input_data or {}).get("tool_name", "")
        if not isinstance(tool_name, str) or not tool_name.startswith(PLUGIN_TOOL_PREFIX):
            return {}
        try:
            seg = contract_map.plugin_seg_of(tool_name)
            plugin = contract_map.plugins.get(seg) if seg is not None else None
            entry = contract_map.tools.get(tool_name) if plugin is not None else None
            # The exempt setup tool: declared absent or safe. A setup tool
            # declared as a CAPABILITY (#1015, it delivers its link) takes
            # the capability path like any other tool.
            is_setup = (plugin is not None and tool_name in plugin.setup_tools
                        and (entry is None or entry.kind != "capability"))
            if is_setup:
                entry = None
            else:
                if plugin is None:
                    return _deny(_DENY_UNKNOWN_PLUGIN)
                if not plugin.adopted:
                    return _deny(_DENY_NON_ADOPTING)
                if entry is None:
                    return _deny(_DENY_UNDECLARED)

            # Authorization for a protected tool — sequenced BEFORE arming.
            if authz_hook is not None and tool_name in protected:
                verdict = await authz_hook(input_data, tool_use_id, context)
                if verdict:
                    return verdict

            if entry is None:
                return {}                      # the exempt setup tool
            tool_input = (input_data or {}).get("tool_input") or {}
            if not isinstance(tool_input, dict):
                tool_input = {}
            needs_identity = entry.kind == "capability" or bool(entry.consumes)
            identity = None
            if needs_identity:
                from authz_grants import resolve_grant_identity
                identity, _why = resolve_grant_identity(
                    role, artifact_id=entry.artifact_id)
                if identity is None:
                    return _deny(_DENY_NO_IDENTITY)
            if entry.kind == "capability":
                store.open_call(
                    client_id=client_id, artifact_id=entry.artifact_id,
                    tool_name=tool_name, tool_use_id=str(tool_use_id or ""),
                    identity=identity, provides=entry.provides,
                    delivers=getattr(entry, "delivers", None))
            if entry.consumes:
                updated = dict(tool_input)
                armed_any = False
                for param, slot in entry.consumes.items():
                    value = tool_input.get(param)
                    if not is_reference(value):
                        continue           # an inert string; not ours to judge
                    ticket = store.arm(
                        reference=value, identity=identity, slot=slot,
                        client_id=client_id, tool_use_id=str(tool_use_id or ""))
                    if ticket is None:
                        return _deny(_DENY_STALE_REFERENCE)
                    updated[param] = f"{value}:{ticket}"
                    armed_any = True
                if armed_any:
                    if entry.kind != "capability":
                        # The consumer's redemption window: open until this
                        # call's PostToolUse/PostToolUseFailure (or the cap).
                        # provides=() so no deposit can ever bind to it.
                        store.open_call(
                            client_id=client_id, artifact_id=entry.artifact_id,
                            tool_name=tool_name,
                            tool_use_id=str(tool_use_id or ""),
                            identity=identity, provides=())
                    return {"hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "updatedInput": updated,
                    }}
            return {}
        except Exception:  # noqa: BLE001 — fail closed, never let it escape
            logger.exception(
                "result broker admission error (tool=%s role=%s) — denying",
                tool_name, role)
            return _deny(_DENY_INTERNAL)

    _hook._casa_result_broker = "admission"          # type: ignore[attr-defined]
    _hook._casa_result_broker_client = client_id     # type: ignore[attr-defined]
    if authz_hook is not None:
        # The option-wiring tests find the authz decision by these markers.
        _hook._casa_authz_role = getattr(authz_hook, "_casa_authz_role", role)  # type: ignore[attr-defined]
        _hook._casa_authz_deps_factory = getattr(                              # type: ignore[attr-defined]
            authz_hook, "_casa_authz_deps_factory", None)
    return _hook


def compose_operator_link(value: str, *, caption: str = "", label: str = ""):
    """#1015: the ONE message Casa posts for a delivered link — ``(text,
    entities, plain)`` for ``TelegramChannel.deliver_operator_link``. The
    link text is ``"<label> (<host>)"`` with the host printed by Casa from
    the URL (lower-cased ``urlsplit().hostname``), never supplied by the
    plugin; one ``text_link`` entity spans it, its length in UTF-16 code
    units (Telegram's unit); the caption follows on its own line as the
    bytes it is (this never goes through the markdown renderer). ``plain``
    is the fallback the channel sends when the entity is refused: the same
    text with the URL spelled out."""
    from telegram import MessageEntity
    from text_util import utf16_len
    host = (urlsplit(value).hostname or "").lower()
    label = label or DEFAULT_LINK_LABEL
    link_text = f"{label} ({host})"
    tail = f"\n{caption}" if caption else ""
    entities = [MessageEntity(type=MessageEntity.TEXT_LINK, offset=0,
                              length=utf16_len(link_text), url=value)]
    return link_text + tail, entities, f"{link_text}: {value}{tail}"


async def _post_operator_link(chat_id: int, text: str, entities, plain: str):
    """Reach the Telegram channel the way the delegated authz factory does
    (``tools._channel_manager``); absent ⇒ ``NOT_DELIVERED``."""
    import tools as tools_mod
    from channels import DeliveryOutcome
    manager = getattr(tools_mod, "_channel_manager", None)
    channel = manager.get("telegram") if manager is not None else None
    if channel is None:
        return DeliveryOutcome.NOT_DELIVERED
    return await channel.deliver_operator_link(chat_id, text, entities, plain)


async def _deliver_and_replace(store: ReferenceStore, seg: str, call: _InFlight,
                               parsed: dict) -> dict[str, Any]:
    """#1015, after the structural check passed: take the delivered slot's
    deposit once, post it, and REPLACE the result — with the receipt on
    proven delivery, with the not-delivered notice on anything else (the
    deposit dropped either way: a delivered reference is used, a withheld
    one must not be redeemable through a notice). The hook's own
    cancellation (the CLI's deadline) drops the deposit and re-raises: no
    replacement, the model holds the delivery-neutral original."""
    from channels import DeliveryOutcome
    slot = next(iter(call.delivers))
    delivered = False
    try:
        taken = store.take_for_delivery(call.deposits.get(slot, ""))
        if taken is not None:
            value, caption, label, identity = taken
            text, entities, plain = compose_operator_link(
                value, caption=caption, label=label)
            outcome = await asyncio.wait_for(
                _post_operator_link(identity.chat_id, text, entities, plain),
                DELIVERY_TIMEOUT_S)
            delivered = outcome is DeliveryOutcome.DELIVERED
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — not proven ⇒ withheld
        # The class only: an upstream error's text could quote the request
        # (the URL is in the entity and the plain fallback), and no error
        # echoes a value.
        logger.warning(
            "operator link delivery failed (plugin=%s slot=%s): %s — withholding",
            seg, slot, type(exc).__name__)
        delivered = False
    finally:
        if not delivered:
            store.drop_call_deposits(call)
    if not delivered:
        return _withheld(seg, _REASON_LINK_NOT_DELIVERED)
    receipt = dict(parsed)
    receipt["casa_delivery"] = {
        "slot": slot, "status": "delivered", "to": DELIVERED_TO}
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                   "updatedToolOutput": json.dumps(receipt)}}


def make_result_hook(
    contract_map, *, client_id: str, store: ReferenceStore | None = None,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """The PostToolUse callback (#792 §3.3): the second boundary. Any
    exception is the withheld replacement, never a pass. A ``capability``
    result whose call delivers a slot (#1015) is, after the structural
    check, replaced by the delivery receipt or the not-delivered notice —
    never passed as returned."""
    store = store or STORE

    async def _hook(input_data, tool_use_id, context):
        tool_name = (input_data or {}).get("tool_name", "")
        if not isinstance(tool_name, str) or not tool_name.startswith(PLUGIN_TOOL_PREFIX):
            return {}
        seg = "?"
        try:
            seg = contract_map.plugin_seg_of(tool_name) or "?"
            plugin = contract_map.plugins.get(seg)
            entry = contract_map.tools.get(tool_name) if plugin is not None else None
            if (plugin is not None and tool_name in plugin.setup_tools
                    and (entry is None or entry.kind != "capability")):
                return {}      # the exempt setup tool (declared absent or safe)
            if plugin is None:
                return _withheld(seg, _REASON_UNKNOWN_PLUGIN)
            if not plugin.adopted:
                return _withheld(seg, _REASON_NON_ADOPTING)
            if entry is None:
                return _withheld(seg, _REASON_UNDECLARED)
            call = store.close_call(client_id, str(tool_use_id or ""))
            if entry.kind == "safe":
                return {}
            text = _response_text((input_data or {}).get("tool_response"))
            parsed = _parse_object(text) if text is not None else None
            if (call is not None and parsed is not None
                    and store.is_no_link_result(call, parsed)):
                # A delivering tool that created no link says so with null
                # slots and no deposit: its result passes unchanged, with no
                # receipt and nothing sent (INV-PLUG-028).
                return {}
            if call is None or parsed is None or not store.validate_result(call, parsed):
                if call is not None:
                    store.drop_call_deposits(call)
                return _withheld(seg, _REASON_BAD_CAPABILITY)
            if not call.delivers:
                return {}
            return await _deliver_and_replace(store, seg, call, parsed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — fail closed
            logger.exception(
                "result broker replacement error (tool=%s) — withholding",
                tool_name)
            return _withheld(seg, _REASON_BAD_CAPABILITY)

    _hook._casa_result_broker = "result"             # type: ignore[attr-defined]
    return _hook


def make_failure_hook(
    contract_map, *, client_id: str, store: ReferenceStore | None = None,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """PostToolUseFailure housekeeping: the event carries no ``tool_response``
    and its output has no replacement field (measured: an MCP tool error
    fires this event and the error text reaches the model), so this only
    closes the in-flight call so its deposits do not outlive it."""
    store = store or STORE

    async def _hook(input_data, tool_use_id, context):
        try:
            tool_name = (input_data or {}).get("tool_name", "")
            if isinstance(tool_name, str) and tool_name.startswith(PLUGIN_TOOL_PREFIX):
                call = store.close_call(client_id, str(tool_use_id or ""))
                if call is not None:
                    store.drop_call_deposits(call)
        except Exception:  # noqa: BLE001
            logger.exception("result broker failure-hook error (tool=%s)",
                             (input_data or {}).get("tool_name"))
        return {}

    _hook._casa_result_broker = "failure"            # type: ignore[attr-defined]
    return _hook


def broker_matchers(
    role: str, resolution, *, client_id: str,
    authz_hook: Callable | None = None, protected: dict | None = None,
    store: ReferenceStore | None = None,
) -> dict[str, list]:
    """The three ``HookMatcher`` lists a plugin-bearing SDK session appends —
    code-side, beside the authz hook, never from a hooks document."""
    from claude_agent_sdk import HookMatcher
    from plugin_grants import result_contract_map

    contract_map = result_contract_map(resolution)
    # #1015: the timeout is set explicitly — the result hook now awaits a
    # Telegram send (bounded by DELIVERY_TIMEOUT_S) and the CLI cancels a
    # hook past this deadline and proceeds with the ORIGINAL result.
    return {
        "PreToolUse": [HookMatcher(
            matcher=PLUGIN_TOOL_MATCHER, timeout=HOOK_TIMEOUT_S,
            hooks=[make_plugin_admission_hook(
                role, contract_map, client_id=client_id,
                authz_hook=authz_hook, protected=protected, store=store)])],
        "PostToolUse": [HookMatcher(
            matcher=PLUGIN_TOOL_MATCHER, timeout=HOOK_TIMEOUT_S,
            hooks=[make_result_hook(contract_map, client_id=client_id, store=store)])],
        "PostToolUseFailure": [HookMatcher(
            matcher=PLUGIN_TOOL_MATCHER, timeout=HOOK_TIMEOUT_S,
            hooks=[make_failure_hook(contract_map, client_id=client_id, store=store)])],
    }


def broker_env(client_id: str) -> dict[str, str]:
    """The two environment keys the CLI's stdio MCP servers inherit (measured:
    they do) so a producer can deposit and a consumer can redeem."""
    return {ENV_CLIENT: client_id, ENV_SOCKET: BROKER_SOCKET_PATH}


# ---------------------------------------------------------------------------
# The two routes on the internal Unix socket
# ---------------------------------------------------------------------------


def _bad(code: str, status: int = 200) -> web.Response:
    return web.json_response({"error": code}, status=status)


def build_broker_deposit_handler(store: ReferenceStore | None = None):
    """``POST /internal/broker/deposit`` ``{"client", "slot", "value"}`` plus
    the optional ``"caption"``/``"label"`` strings a delivered slot may carry
    (#1015) ⇒ ``{"reference"}`` or ``{"error"}``. Write-only: nothing here
    reads a value back, and no error echoes one."""
    store = store or STORE

    async def handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _bad("bad_json")
        if not isinstance(body, dict):
            return _bad("bad_json")
        client = body.get("client")
        slot = body.get("slot")
        value = body.get("value")
        if not isinstance(client, str) or not client:
            return _bad("bad_client")
        if not isinstance(slot, str) or not slot:
            return _bad("bad_slot")
        if not isinstance(value, str) or not value:
            return _bad("bad_value")
        if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
            return _bad("value_too_large")
        # Passed through as they are: the store judges them only for a
        # delivered slot (#1015); for any other slot they are ignored, so a
        # producer library can send them uniformly and the base's behaviour
        # for such a deposit is unchanged.
        ref, err = store.deposit(client_id=client, slot=slot, value=value,
                                 caption=body.get("caption"),
                                 label=body.get("label"))
        if err:
            return _bad(err)
        return web.json_response({"reference": ref})

    return handler


def build_broker_redeem_handler(store: ReferenceStore | None = None):
    """``POST /internal/broker/redeem`` ``{"client", "reference", "ticket"}``
    ⇒ ``{"value"}`` exactly once, or ``{"error"}``."""
    store = store or STORE

    async def handler(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _bad("bad_json")
        if not isinstance(body, dict):
            return _bad("bad_json")
        client = body.get("client")
        reference = body.get("reference")
        ticket = body.get("ticket")
        if not isinstance(client, str) or not client:
            return _bad("bad_client")
        if not is_reference(reference):
            return _bad("bad_reference")
        if not isinstance(ticket, str) or not _TICKET_RE.fullmatch(ticket):
            return _bad("bad_ticket")
        value, err = store.redeem(client_id=client, reference=reference, ticket=ticket)
        if err:
            return _bad(err)
        return web.json_response({"value": value})

    return handler
