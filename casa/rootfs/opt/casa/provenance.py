"""Turn provenance: transport x execution-role classifier (A:§1).

This module is the provenance foundation every later ask_user/authz task
gates on. It has two responsibilities:

1. **Sanitization at every external ingress** — ``sanitize_external_context``
   strips a fixed set of Casa-reserved keys from any context dict a caller
   supplies (Telegram update, ``/invoke`` payload, voice SSE/WS payload).
   Callers merge Casa-owned keys back in AFTER sanitizing, so an external
   caller can never spoof provenance-bearing keys.

2. **Turn classification** — ``turn_provenance`` reads the current turn's
   origin (``agent.origin_var``) and engagement binding
   (``tools.engagement_var``) and returns a :class:`Provenance` describing
   *how* the current turn arrived (transport) and *who* is actually
   executing it (execution role).

This module is intentionally leaf-level: it must import neither ``agent``
nor ``tools`` at module scope (both of those modules — directly or
transitively — import a lot, and either could plausibly end up importing
provenance.py in a later task). ``turn_provenance`` imports them lazily,
inside the function body, to avoid any import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

# Keys Casa itself uses to record turn provenance. A caller-supplied
# context (Telegram update payload, /invoke body, voice SSE/WS payload)
# must never be allowed to set these directly — sanitize_external_context
# strips them before Casa's own values are merged in.
RESERVED_CONTEXT_KEYS = frozenset({
    "synthetic",
    "button_answer",
    "execution_role",
    "message_type",
    "source",
    "_voice_route_id",
    "_voice_route_capabilities",
    "_voice_job_control_id",
    "_origin_device_id",
    "_voice_transport",
    "_voice_handoff_reservation",
    # #233/#224: the per-utterance delivery offer says what the ASKING device
    # can receive, and Casa promises on the strength of it. It is stamped from
    # the authenticated route AFTER sanitization; if an external caller could
    # set it in `context`, a client could manufacture a capability it does not
    # have and be promised a delivery it can never receive.
    "_voice_delivery_offer",
    # Release A: unspoofable, server-set origin markers. An external caller
    # (webhook payload, /invoke body) must never set these — the ingress
    # handler stamps them after sanitization so a webhook-origin turn cannot
    # forge "invoke"/"private" clearance (spec A0/A4).
    "_origin_route",
    "_origin_clearance",
    # #485: the scheduled-delivery marker. A resident's own time-based trigger
    # turn carries a session-keying LABEL in `chat_id`, not a chat id, so
    # `send_media` had nowhere to deliver. This marker — stamped ONLY by Casa's
    # two time-based dispatch sites, and only for a telegram-channel trigger —
    # is what admits it, with the operator identity resolved at the point of
    # use. Reserved because a caller who could set it would aim a delivery at
    # the operator's DM from a webhook payload: the authenticated webhook route
    # dispatches `MessageType.SCHEDULED` too, which is exactly why eligibility
    # is a marker and never an inference from the message type.
    "_scheduled_delivery",
    # #573: the trigger-lifecycle epoch of the schedule that fired this turn.
    # `ask_user` refuses to raise a keyboard when it is stale, which is how a
    # trigger deleted or rewritten WHILE its turn was already running stops
    # producing questions. Reserved for the same reason as the marker above: a
    # caller who could set it would hand itself a fresh-looking epoch.
    "_scheduled_epoch",
    # #283: live-operator turn marker — the agent-spawn cap exempts a spawn
    # ONLY when this is present (absence = agent context, fail closed), so a
    # caller who could set it through an external context would exempt
    # itself from the cap. Stamped exclusively by the Telegram channel for
    # an authenticated operator sender; synthesized/scheduled/webhook turns
    # never carry it.
    "_operator_turn",
})


def sanitize_external_context(ctx: dict | None) -> dict:
    """Return a copy of *ctx* with every reserved provenance key removed.

    ``None``, any falsy value, or a non-dict normalizes to ``{}`` — the
    value under ``"context"`` is caller-controlled on every ingress that
    passes it here (SSE body, WS utterance frame, /invoke payload), and a
    truthy non-dict used to crash the handler on ``.items()`` (#287). The
    input dict is never mutated — callers may still hold a reference to it.
    """
    if not isinstance(ctx, dict) or not ctx:
        return {}
    return {k: v for k, v in ctx.items() if k not in RESERVED_CONTEXT_KEYS}


def scheduled_delivery_markers(
    channel: str | None, epoch: str | None = None,
) -> dict:
    """The context markers a TIME-BASED scheduled dispatch stamps (#485).

    Both sites that fire a resident's own schedule — the scheduler
    (``trigger_registry``) and the overdue reminder sweep (``reminders``) —
    call THIS, so the eligibility rule has one home. Two sites deciding
    separately is how a reminder ends up able to send an invoice when Casa
    happened to be up at the due moment and unable to when it was not.

    Only the telegram channel gets the marker: it is the only channel with an
    operator DM to deliver into. Everything else — voice, an unset channel —
    returns empty and behaves exactly as before.

    Deliberately NOT stamped here: any operator identity. The marker says
    "Casa's own schedule fired this"; who the operator IS is resolved at the
    point of use from the live configuration, so a stale or since-changed id
    can never be delivered to.

    ``epoch`` (#573) is the caller's trigger-lifecycle epoch for this trigger
    (``scheduled_asks.epoch_for(role, label)`` — an opaque string compared only
    for equality). It rides along so ``ask_user`` can refuse
    to raise a keyboard for a trigger that was removed or rewritten after this
    turn was dispatched. Omitted (``None``) it is simply absent, which every
    consumer tolerates — a turn without one is a turn nothing can revoke.
    """
    if channel != "telegram":
        return {}
    markers: dict = {"_scheduled_delivery": True}
    if epoch is not None:
        markers["_scheduled_epoch"] = epoch
    return markers


def strict_positive_id(v: object) -> int | None:
    """Coerce *v* to a strictly-positive int, or ``None`` if malformed.

    Accepts a real ``int`` (excluding ``bool`` — a ``bool`` subclasses
    ``int`` in Python but is never a legitimate chat/user id) or a
    pure-digit ``str``. Anything else (float, ``Mock``, non-digit string,
    zero, negative) returns ``None``.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v > 0 else None
    if isinstance(v, str) and v.isdigit():
        n = int(v)
        return n if n > 0 else None
    return None


@dataclass(frozen=True)
class Provenance:
    """The classified shape of the current turn.

    ``transport``: "dm" (a direct 1:1 Telegram message), "button" (a
    synthetic turn replaying an inline-button answer), or "other"
    (anything else — voice, webhook, group/engagement-topic traffic,
    malformed/missing ids, ...).

    ``execution``: "direct" (the resident handling its own turn),
    "delegated" (a specialist/executor running on behalf of a different
    role via delegate_to_agent), or "engagement" (running inside a bound
    engagement — takes priority over the delegated check).
    """

    transport: str
    execution: str


def turn_provenance() -> Provenance:
    """Classify the transport and execution role of the CURRENT turn.

    Reads ``agent.origin_var`` and ``tools.engagement_var`` — both lazily
    imported here to keep this module leaf-level (see module docstring).
    A missing/unset origin classifies as ``Provenance("other", "direct")``.
    """
    import agent as agent_mod
    import tools as tools_mod

    origin = agent_mod.origin_var.get(None) or {}

    transport = "other"
    if (
        origin.get("message_type") == "channel_in"
        and origin.get("channel") == "telegram"
        and origin.get("source") == "telegram"
        and strict_positive_id(origin.get("chat_id")) is not None
        and strict_positive_id(origin.get("user_id")) is not None
    ):
        marker = origin.get("synthetic")
        if marker is None:
            transport = "dm"
        elif marker == "button":
            transport = "button"
        # any other marker value falls through, leaving transport "other"

    if tools_mod.engagement_var.get(None) is not None:
        execution = "engagement"
    elif origin.get("execution_role") != origin.get("role"):
        execution = "delegated"
    else:
        execution = "direct"

    return Provenance(transport=transport, execution=execution)
