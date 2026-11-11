"""Browser executor §5.3–§5.6 (Plan 3 C1, C2, C3, C8, C9): what a plugin's own
code asks Casa while one of its declared tools runs.

A plugin tool declares ``"callsCasa": true`` in its ``casa.resultContract``
entry. When such a call is admitted (``result_broker.make_plugin_admission_hook``)
Casa records it here with the facts of the turn it runs in: the grant identity
(the operator and chat), the operator's own words for the turn
(``handoff.operator_words``), the chain of requesters and the turn's binding, if
it is a continuation. A turn with no grant identity — voice, a webhook, a
schedule, a sender who is not the operator — has the call denied before it runs.

During the call the plugin's code, never its model, presents its session's
broker client id on the internal socket; the call is found by that id alone and
must be the client's only recorded one (the deposit route's rule):

* ``POST /internal/broker/requester``            (C1) — whom the call serves.
* ``POST /internal/broker/run-admission``        (C2) — run admission.
* ``POST /internal/broker/run-admission-status`` (C3) — a confirmation's state.
* ``POST /internal/broker/continue``             (C8) — queue a continuation turn.
* ``POST /internal/broker/delivery-status``      (C9) — a delivered slot's outcome.
* ``POST /internal/broker/action-judgement``     — the check of one browsing
  action (``action_judgement``).
* ``POST /internal/broker/pad``                  — put text into the conversation's
  pad, or read it back, with no model in between (``scratchpad``).

The record is forgotten at the call's PostToolUse / PostToolUseFailure, or
after ``OPERATOR_CALL_CAP_S``. The operator question Casa itself posts for run
admission (``post_question``) is Casa's keyboard, answered by a tap that runs no
turn and uses no desk."""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
import uuid
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

OPERATOR_CALL_CAP_S = 3600.0
ASK_MAX_OPTIONS = 6
ASK_MAX_LABEL_CHARS = 32
RUN_REF_MAX = 64
CONTINUE_REASONS = ("operator_answer", "next_run", "sweep")
# The internal app's body limit: a judgement carries a page's picture (2 MiB decoded, base64)
# beside its packet (512 KiB, up to three times that JSON-escaped), over aiohttp's default
# 1 MiB, which reads as ``bad_json``.
INTERNAL_MAX_BODY_BYTES = 6 * 1024 * 1024


@dataclasses.dataclass
class OperatorCall:
    client_id: str
    tool_use_id: str
    tool_name: str
    role: str
    plugin: str                         # the plugin segment of the tool
    identity: Any                       # authz_grants.GrantIdentity
    operator_words: "str | None"
    chain: tuple
    requester: str
    turn_key: str                       # one binding per (plugin, turn)
    binding_ref: "str | None"           # set on a continuation turn
    origin: dict
    opened_at: float


class OperatorCalls:
    """Thread-safe, memory-only record of the in-flight ``callsCasa`` calls;
    restart-losing by construction, like the reference store."""

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._now = now
        self._calls: dict[tuple[str, str], OperatorCall] = {}

    def _sweep_locked(self) -> None:
        now = self._now()
        for key, call in list(self._calls.items()):
            if call.opened_at + OPERATOR_CALL_CAP_S <= now:
                del self._calls[key]

    def open(self, *, client_id: str, tool_use_id: str, tool_name: str, role: str,
             plugin: str, identity: Any, origin: dict) -> OperatorCall:
        from handoff import child_chain, operator_words, requester_of
        # The chain ends with the agent making the call (``role``). A delegated
        # run's origin already carries it; an engagement turn's ambient origin
        # is the launching resident's turn, which does not — and a binding
        # whose chain ends with the resident would continue the run as a turn
        # of the resident instead of the agent that runs it.
        chain = child_chain(origin, role)
        binding = origin.get("_binding_ref")
        call = OperatorCall(
            client_id=client_id, tool_use_id=tool_use_id, tool_name=tool_name,
            role=role, plugin=plugin, identity=identity,
            operator_words=operator_words(origin), chain=chain,
            requester=requester_of(chain),
            turn_key=str(origin.get("_delegation_id") or origin.get("cid") or ""),
            binding_ref=binding if isinstance(binding, str) else None,
            origin={k: v for k, v in origin.items()
                    if isinstance(v, (str, int, float, bool, type(None)))},
            opened_at=self._now())
        with self._lock:
            self._sweep_locked()
            self._calls[(client_id, tool_use_id)] = call
        return call

    def close(self, client_id: str, tool_use_id: str) -> None:
        with self._lock:
            self._calls.pop((client_id, tool_use_id), None)

    def find(self, client_id: str) -> "tuple[OperatorCall | None, str | None]":
        with self._lock:
            self._sweep_locked()
            matches = [c for (cid, _), c in self._calls.items() if cid == client_id]
        if not matches:
            return None, "no_call_in_flight"
        if len(matches) > 1:
            return None, "ambiguous_call"
        return matches[0], None


CALLS = OperatorCalls()


def is_configured_operator(chat_id: Any, operator_id: Any) -> bool:
    """Is (chat, user) the operator as configured NOW (the Telegram channel's
    one operator rule, ``authz_grants._live_operator_identity``)? False with
    no configured operator (accept-all) or no channel up."""
    from authz_grants import _live_operator_identity
    live = _live_operator_identity()
    try:
        return live is not None and live == (int(chat_id), int(operator_id))
    except (TypeError, ValueError):
        return False


def binding_for(call: OperatorCall):
    """C1's binding: the continuation's own on a continuation turn, else the
    one minted for this plugin and this turn."""
    from plugin_bindings import BINDINGS
    if call.binding_ref is not None:
        b = BINDINGS.get(call.binding_ref, plugin=call.plugin,
                         operator_id=call.identity.operator_id)
        if b is not None:
            return b
    return BINDINGS.for_turn(plugin=call.plugin, turn_key=call.turn_key,
                             identity=call.identity, chain=call.chain, origin=call.origin)


def call_target(call: OperatorCall) -> "tuple[Any, int]":
    """The endpoint and bot id the call's binding resolves to — the
    continuation's own binding, else the one a binding minted for this turn
    would name (``plugin_bindings.binding_target``: the call's delivery, or
    its route when the call posts nothing) — read now, without minting one.
    Raises ``plugin_bindings.ChannelUnavailable`` when there is none to read."""
    from plugin_bindings import (
        BINDINGS, binding_target, delivery_target, route_of, route_target)
    if call.binding_ref is not None:
        b = BINDINGS.get(call.binding_ref, plugin=call.plugin,
                         operator_id=call.identity.operator_id)
        if b is not None:
            return binding_target(b)
    delivery = getattr(call.identity, "delivery", None)
    if delivery is not None:
        return delivery_target(delivery)
    return route_target(route_of(call.origin, call.identity))


def requester_facts(call: OperatorCall) -> dict:
    b = binding_for(call)
    return {"binding_ref": b.ref, "requester_id": b.requester,
            "requester_kind": b.requester_kind, "chain": list(b.chain)}


# ---------------------------------------------------------------------------
# Casa's own operator question (run admission's confirmation card)
# ---------------------------------------------------------------------------

async def post_question(*, endpoint: Any, identity: Any, role: str, text: str,
                        options: list[str], timeout_s: float,
                        on_settle: Callable[[dict], Awaitable[None]],
                        after_choice: "dict[int, list[tuple[str, str]]] | None" = None,
                        chat_id: "int | None" = None) -> bool:
    """Post *text* under *role*'s label with one button per option, through
    the Telegram *endpoint* the caller resolved (the call's delivery for a run
    admission, Casa's own for its restart card), to the operator *identity*
    names, in *chat_id* (the identity's chat when not given). In *role*'s own
    chat the label is left out: the chat is the agent. The question's record
    keeps *role*, the agent it is asked for, and the address it was delivered
    at, as every card's does. ``on_settle`` runs
    once with the outcome — ``{"outcome":
    "answered", "choice": i}``, ``no_answer`` at the timeout or
    ``cancelled``. Returns whether the keyboard landed. Detached: the caller
    does not wait. Only that operator can answer, on that bot (the
    resident-ask handler's actor and bot checks).

    The question's lane and its stored bot are the endpoint's bot id, read
    before anything is registered; an endpoint with no id yet posts nothing
    and returns False, as a failed post does.

    ``after_choice``: for an answered choice with an entry, the settled card
    keeps that keyboard (``(label, callback data)`` buttons) instead of none
    (the start card's Stop). That choice's ``on_settle`` runs BEFORE the edit
    that shows the keyboard, and is also given the card's ``message_id``, its
    ``address`` and its settled ``text``, so what the buttons act on is saved
    before they appear."""
    from channels.tg_address import EndpointNotReady, TgAddress, scope as lane_scope
    from result_broker import compose_operator_message, label_at
    from verdict_broker import BROKER

    channel = endpoint
    if channel is None or identity is None:
        return False
    try:
        bot_id = channel.bot_id
    except EndpointNotReady:
        logger.warning("operator question not posted: the bot has no id yet")
        return False
    chat_id = identity.chat_id if chat_id is None else chat_id
    operator_id = identity.operator_id
    body = compose_operator_message(text, label_at(role, channel))
    rid = uuid.uuid4().hex
    scope = lane_scope("ask", bot_id, chat_id)
    req, _ = BROKER.register(
        namespace="resident_ask", scope=scope, request_id=rid, timeout_s=timeout_s,
        detached=True, meta={"options": list(options), "chat_id": chat_id,
                             "bot_id": bot_id, "operator_id": operator_id,
                             "kind": "casa_question", "_scope": scope,
                             # the agent the question is asked for, and where it was
                             # delivered, as every card records them
                             "role": role, "delivery": TgAddress(bot_id, int(chat_id))})

    async def _post():
        return await channel.deliver_operator_question(chat_id, body, list(options), rid)

    def _finish_factory(message_id: int):
        async def _finish(outcome: dict) -> None:
            answered = outcome.get("outcome") == "answered"
            if answered:
                choice = int(outcome["option_index"])
                shown = f"{body}\n\n☑ {options[choice]}"
                keep = (after_choice or {}).get(choice)
                if keep:
                    await on_settle({"outcome": "answered", "choice": choice,
                                     "message_id": message_id,
                                     "address": TgAddress(bot_id, int(chat_id), message_id),
                                     "text": shown})
                    await channel.edit_dm_message(chat_id, message_id, shown,
                                                  buttons=list(keep))
                    return
            else:
                import ask_retirement
                shown = ask_retirement.retired_body(
                    body, outcome.get("outcome"), outcome.get("reason"))
            await channel.edit_dm_message(chat_id, message_id, shown)
            await on_settle({"outcome": "answered", "choice": int(outcome["option_index"])}
                            if answered else {"outcome": str(outcome.get("outcome")
                                                             or "cancelled")})
        return _finish

    await BROKER.ensure_posted(req, _post, _finish_factory)
    return "message_id" in req.meta


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

def _reply(payload: dict):
    from aiohttp import web
    return web.json_response(payload)


async def _found(request) -> "tuple[OperatorCall | None, dict, str | None]":
    """The call in flight for the body's client, the body, and the error code if none."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if not isinstance(body, dict):
        return None, {}, "bad_json"
    client = body.get("client")
    if not isinstance(client, str) or not client:
        return None, body, "bad_client"
    call, err = CALLS.find(client)
    return call, body, err


async def _call_and_body(request) -> "tuple[OperatorCall | None, dict, Any]":
    call, body, err = await _found(request)
    return call, body, (None if call is not None else _reply({"error": err}))


def build_requester_handler():
    """C1 ``{"client"}`` ⇒ ``{"binding_ref", "requester_id", "requester_kind",
    "chain"}``. Nothing in the body names a requester; Casa reads the call."""
    async def handler(request):
        call, _body, bad = await _call_and_body(request)
        if call is None:
            return bad
        return _reply(requester_facts(call))
    return handler


def build_run_admission_handler():
    """C2 ``{"client", "request_key", "runs"}`` ⇒ ``{"status": "admitted",
    "admissions", …}`` | ``{"status": "confirmation_pending", "confirmation_id",
    …}`` | ``{"status": "declined"|"expired", …}`` (a replayed key) |
    ``{"status": "refused", "reason"}`` | ``{"error"}``; every answer carries
    ``request_key``. A replayed key returns its first answer."""
    async def handler(request):
        call, body, bad = await _call_and_body(request)
        if call is None:
            return bad
        import run_admission
        key = body.get("request_key")
        if (not isinstance(key, str) or not 0 < len(key) <= run_admission.REQUEST_KEY_MAX
                or not key.isprintable()):
            return _reply({"error": "bad_request_key"})
        runs, err = run_admission.validate_runs(body.get("runs"))
        if err:
            return _reply({"error": err})
        return _reply(await run_admission.request_admission(call, runs, key))
    return handler


def build_run_admission_status_handler():
    """C3 ``{"client", "confirmation_id"}`` ⇒ the confirmation's state."""
    async def handler(request):
        call, body, bad = await _call_and_body(request)
        if call is None:
            return bad
        import run_admission
        return _reply(run_admission.confirmation_status(call, body.get("confirmation_id")))
    return handler


def build_continue_handler():
    """C8 ``{"client", "binding_ref", "run_ref", "reason"}`` ⇒
    ``{"status": "queued"|"already_queued"}`` | ``{"error"}``
    (``channel_unavailable`` while the binding's bot has no id yet)."""
    async def handler(request):
        call, body, bad = await _call_and_body(request)
        if call is None:
            return bad
        from plugin_bindings import BINDINGS
        run_ref, reason = body.get("run_ref"), body.get("reason")
        if (not isinstance(run_ref, str) or not 0 < len(run_ref) <= RUN_REF_MAX
                or not run_ref.isprintable()):
            return _reply({"error": "bad_run_ref"})
        if reason not in CONTINUE_REASONS:
            return _reply({"error": "bad_reason"})
        binding = BINDINGS.get(body.get("binding_ref"), plugin=call.plugin,
                               operator_id=call.identity.operator_id)
        if binding is None:
            return _reply({"error": "unknown_binding"})
        import plugin_continuations
        from plugin_bindings import ChannelUnavailable
        try:
            status = plugin_continuations.CONTINUATIONS.request(binding, run_ref, reason)
        except ChannelUnavailable:
            # the binding's bot has no id yet: nothing is queued; the plugin's
            # next request, or the driver sweep, finds the run again
            return _reply({"error": "channel_unavailable"})
        return _reply({"status": status})
    return handler


def build_delivery_status_handler():
    """C9 ``{"client", "reference"}`` ⇒ ``{"status", "repeat"}``."""
    async def handler(request):
        call, body, bad = await _call_and_body(request)
        if call is None:
            return bad
        import result_broker
        return _reply(result_broker.DELIVERY_OUTCOMES.status(
            body.get("reference"), plugin=call.plugin,
            operator_id=call.identity.operator_id,
            artifact_id=call.identity.artifact_id))
    return handler


def build_action_judgement_handler():
    """``{"client", "kind", "packet", "spec_digest", "picture"?}`` ⇒ ``{"verdict",
    "packet_sha256", "latency_ms"}`` | ``{"error"}`` (``bad_kind``,
    ``bad_packet``, ``bad_digest``, ``check_unavailable``). The packet is the
    app's JSON text as a string; the picture, base64 PNG, is outside its digest; the
    words come from the call's plugin and operator, never the body."""
    async def handler(request):
        call, body, bad = await _call_and_body(request)
        if call is None:
            return bad
        import action_judgement
        return _reply(await action_judgement.judge(
            call, body.get("kind"), body.get("packet"), body.get("spec_digest"),
            picture=body.get("picture")))
    return handler


_PAD_REFUSALS = {
    "bad_json": "The body is one JSON object.",
    "bad_client": "client is the session's broker client id.",
    "no_call_in_flight": "The pad answers only during a call of this client's tool.",
    "ambiguous_call": "This client has two calls in flight; the pad answers one at a time.",
}


def build_pad_handler():
    """``{"client", "op": "write"|"append"|"read", "section", "text"?, "offset"?}`` ⇒
    ``{"lines", "bytes"}`` (the section after a write or append) | ``{"text", "next"}`` (at
    most ``scratchpad.ROUTE_READ_CHARS`` from ``offset``) | ``{"error", "message"}``. The pad is
    the one the client's own tool reaches, found by the client alone."""
    async def handler(request):
        call, body, err = await _found(request)
        if call is None:
            return _reply({"error": err, "message": _PAD_REFUSALS[err]})
        import scratchpad
        return _reply(scratchpad.PADS.from_plugin(body["client"], body))
    return handler
