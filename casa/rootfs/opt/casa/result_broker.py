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

Scope (the invariant's own): sessions that carry the authorization seam —
resident, delegated specialist, specialist engagement. Executor sessions are
outside it (#923). A producer that breaks its declared contract is the
plugin-author trust boundary #785 rules on; Casa detects no such violation.
"""
from __future__ import annotations

import dataclasses
import hmac
import json
import logging
import re
import secrets
import threading
import time
from typing import Any, Awaitable, Callable

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


@dataclasses.dataclass
class _Reference:
    value: str
    slot: str
    identity: Any
    minted_at: float
    expires_at: float
    armed: tuple | None = None    # (client_id, tool_use_id, ticket)
    used: bool = False

    def __repr__(self) -> str:      # never the value
        return (f"_Reference(slot={self.slot!r}, armed={self.armed is not None}, "
                f"used={self.used})")


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
                del self._inflight[key]

    def sweep(self) -> None:
        with self._lock:
            self._sweep_locked()

    # -- in-flight calls --------------------------------------------------
    def open_call(self, *, client_id: str, artifact_id: str, tool_name: str,
                  tool_use_id: str, identity, provides: tuple) -> None:
        """Register a ``capability`` call at admission. The identity is stored
        HERE, atomically with the call, so a deposit can bind it: the plugin's
        deposit request carries no identity and asserts none."""
        with self._lock:
            self._sweep_locked()
            self._inflight[(client_id, tool_use_id)] = _InFlight(
                client_id=client_id, artifact_id=artifact_id,
                tool_name=tool_name, tool_use_id=tool_use_id,
                identity=identity, provides=tuple(provides),
                opened_at=self._now())

    def close_call(self, client_id: str, tool_use_id: str):
        """Close a call (PostToolUse / PostToolUseFailure). Returns the record
        or ``None``. Deposits the result did not reference die with it."""
        with self._lock:
            return self._inflight.pop((client_id, tool_use_id), None)

    # -- deposit ----------------------------------------------------------
    def deposit(self, *, client_id: str, slot: str, value: str) -> tuple[str | None, str | None]:
        """Bind ``value`` to the UNIQUE in-flight capability call of
        ``client_id`` whose contract provides ``slot``; mint and return a
        reference. Zero or more than one such call ⇒ refused (fail closed).
        Returns ``(reference, None)`` or ``(None, error_code)``."""
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
            ref = new_reference()
            now = self._now()
            self._refs[ref] = _Reference(
                value=value, slot=slot, identity=call.identity,
                minted_at=now, expires_at=now + reference_ttl_s())
            call.deposits[slot] = ref
            return ref, None

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
            is_setup = plugin is not None and tool_name in plugin.setup_tools
            entry = None
            if not is_setup:
                if plugin is None:
                    return _deny(_DENY_UNKNOWN_PLUGIN)
                if not plugin.adopted:
                    return _deny(_DENY_NON_ADOPTING)
                entry = contract_map.tools.get(tool_name)
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
                    identity=identity, provides=entry.provides)
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


def make_result_hook(
    contract_map, *, client_id: str, store: ReferenceStore | None = None,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """The PostToolUse callback (#792 §3.3): the second boundary. Any
    exception is the withheld replacement, never a pass."""
    store = store or STORE

    async def _hook(input_data, tool_use_id, context):
        tool_name = (input_data or {}).get("tool_name", "")
        if not isinstance(tool_name, str) or not tool_name.startswith(PLUGIN_TOOL_PREFIX):
            return {}
        seg = "?"
        try:
            seg = contract_map.plugin_seg_of(tool_name) or "?"
            plugin = contract_map.plugins.get(seg)
            if plugin is not None and tool_name in plugin.setup_tools:
                return {}
            if plugin is None:
                return _withheld(seg, _REASON_UNKNOWN_PLUGIN)
            if not plugin.adopted:
                return _withheld(seg, _REASON_NON_ADOPTING)
            entry = contract_map.tools.get(tool_name)
            if entry is None:
                return _withheld(seg, _REASON_UNDECLARED)
            call = store.close_call(client_id, str(tool_use_id or ""))
            if entry.kind == "safe":
                return {}
            text = _response_text((input_data or {}).get("tool_response"))
            parsed = _parse_object(text) if text is not None else None
            if call is None or parsed is None or not store.validate_result(call, parsed):
                if call is not None:
                    store.drop_call_deposits(call)
                return _withheld(seg, _REASON_BAD_CAPABILITY)
            return {}
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
    return {
        "PreToolUse": [HookMatcher(
            matcher=PLUGIN_TOOL_MATCHER,
            hooks=[make_plugin_admission_hook(
                role, contract_map, client_id=client_id,
                authz_hook=authz_hook, protected=protected, store=store)])],
        "PostToolUse": [HookMatcher(
            matcher=PLUGIN_TOOL_MATCHER,
            hooks=[make_result_hook(contract_map, client_id=client_id, store=store)])],
        "PostToolUseFailure": [HookMatcher(
            matcher=PLUGIN_TOOL_MATCHER,
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
    """``POST /internal/broker/deposit`` ``{"client", "slot", "value"}`` ⇒
    ``{"reference"}`` or ``{"error"}``. Write-only: nothing here reads a
    value back, and no error echoes one."""
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
        ref, err = store.deposit(client_id=client, slot=slot, value=value)
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
