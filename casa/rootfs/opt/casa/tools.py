"""In-process MCP tools for the Casa framework."""

from __future__ import annotations

import asyncio
import functools
import enum
import importlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from contextlib import asynccontextmanager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from datetime import datetime
    from trigger_registry import TriggerRegistry

from plugin_env_conf import set_entry as _set_env_entry  # noqa: F401 — available for future use
from system_requirements.orchestrator import install_requirements, OrchestrationError
from system_requirements.manifest import (
    add_plugin_entry as add_manifest, remove_plugin_entry as remove_manifest,
    read_manifest as read_sysreq_manifest, retire_stale_bin,
)
import background_jobs
import job_owner_turn
import plugin_registry
import plugin_store
import tool_descriptions
from plugin_grants import (
    CASA_OWNED_ENV_OPTIONS as _CASA_OWNED_ENV,
    apply_profile_plan,
    declared_tools_for_resolution, env_remediation_hint,
    grants_for_resolution, grants_for_resolved,
    make_fail_closed_can_use_tool, mcp_json_malformed,
    profile_plan,
    required_env_vars_for_resolved, sanitized_env_for_paths,
    sanitized_env_for_resolution,
)
from authz_grants import CHALLENGES, GRANTS, normalize_role
from handoff import child_chain, operator_words, requester_of
from memory.eligibility import Source as _CaptureSource

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SdkMcpTool,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

from bus import BusMessage, MessageBus, MessageType
from channels import ChannelManager, DeliveryOutcome
from channels.telegram_transport import telegram_transport
from channels.tg_address import (
    AddressError, EndpointNotReady, EndpointUnavailable, RouteMissing, TgAddress,
    scope as tg_scope,
)
from provenance import carry_route, route_context, task_delivery_marker
from output_boundary import (
    Admitted, ApprovalCut, IntentKind, ReadBeforeDescribe, TurnScope,
    UnadmittedText, casa_text, resolve_scope,
)
from config import current_resident_model, effort_for, resolve_model
from claude_runtime import (
    with_system_prompt_file,
    CLAUDE_CLI_PATH,
    SDK_MAX_BUFFER_SIZE,
    cli_session_settings,
    ephemeral_extra_args,
    ephemeral_setting_sources,
    with_cross_session_tools_denied,
)
from media_policies import MEDIA_POLICIES
import plugin_outbox
import scratchpad
import specialist_session
from error_kinds import (
    ApiErrorTurn, ErrorKind, _USER_MESSAGES, _classify_error, api_error_kind,
    result_api_error_kind,
)
from mcp_registry import McpServerRegistry
import sdk_logging
import specialist_limits
from tokens import extract_usage
from engagement_registry import TerminalPreconditionFailed, EngagementRecord, EngagementRegistry
from specialist_registry import (
    CLOSING_MARKER,
    DelegationComplete,
    DelegationRecord,
    SpecialistRegistry,
)
from job_registry import (
    DeliveryState,
    ExecutionState,
    JobAuthorizationError,
    JobFailure,
    JobRouteCapacityError,
    JobTransitionError,
    VoiceJob,
)
from voice_job_result import (
    VOICE_JOB_OUTPUT_FORMAT,
    VoiceJobResultError,
    parse_voice_job_result,
    spoken_text_for,
    voice_identity_clearance,
)

logger = logging.getLogger(__name__)

# Module-level references, initialized via init_tools()
_channel_manager: ChannelManager | None = None
_bus: MessageBus | None = None
_specialist_registry: SpecialistRegistry | None = None
_mcp_registry: McpServerRegistry | None = None
_agent_role_map: dict[str, "AgentConfig"] = {}  # merged residents + specialists
_trigger_registry: "TriggerRegistry | None" = None
_engagement_registry: EngagementRegistry | None = None
_agent_registry = None  # AgentRegistry | None
_runtime = None  # CasaRuntime | None — set by init_tools(runtime=...)
# Task 6 (spec §4.6): specialist concurrency cap + per-role telemetry. Both
# default to None (no limiting / no telemetry) so callers that don't wire
# them — including every pre-Task-6 test — keep the old unbounded behaviour.
_specialist_limiter: "specialist_limits.SpecialistLimiter | None" = None
_specialist_telemetry: "specialist_limits.SpecialistTelemetry | None" = None
# #283: the agent-spawn occupancy limiter (shared with EngagementRegistry,
# which restores boot debt into it). None = cap feature off (tests, legacy
# construction sites) — every check degrades to a no-op.
_agent_spawn_limiter: "specialist_limits.AgentSpawnLimiter | None" = None
_voice_job_route_cap = 5
# The agent log sink (agent_logs.AgentLogs): each specialist's and plugin
# worker's work, posted to its own topic in an optional forum group. ``None``
# (tests, no Telegram) means nothing is logged.
_agent_logs = None
engagement_var: ContextVar[EngagementRecord | None] = ContextVar(
    "engagement_var", default=None,
)


def _transport() -> Any:
    """The Telegram transport, or ``None`` before ``init_tools`` or when
    Telegram is not configured."""
    if _channel_manager is None:
        return None
    return telegram_transport(_channel_manager)


def _casa_endpoint() -> Any:
    """Casa's own Telegram endpoint, ready or not (Casa's cards and notices
    always go through Casa's bot), or ``None`` without Telegram."""
    transport = _transport()
    if transport is None:
        return None
    try:
        return transport.casa_endpoint()
    except RuntimeError:  # a transport with no Casa endpoint
        return None


#: Why one of Casa's cards cannot be posted, as the start of a refusal's detail.
_CASA_CARD_PROBLEMS = {
    "no_channel": "no telegram channel is configured",
    "no_operator": ("no valid operator DM identity (chat_id unset, "
                    "non-numeric, or a group chat)"),
    "not_ready": "Casa's Telegram bot has not connected yet",
}


def _casa_card_target() -> "tuple[Any, TgAddress | None, str | None]":
    """Where one of Casa's own cards (a consent, install or erase question)
    is posted: ``(Casa's endpoint, the operator's chat on Casa's bot, None)``,
    or ``(endpoint or None, None, why)`` when it cannot be, *why* being
    ``"no_channel"`` (no Telegram), ``"no_operator"`` (no valid operator chat
    configured) or ``"not_ready"`` (Casa's bot has not connected yet, so the
    card's identity, which names the bot, cannot be formed before it is
    posted)."""
    channel = _casa_endpoint()
    if channel is None:
        return None, None, "no_channel"
    try:
        return channel, _transport().casa_operator_address(), None
    except EndpointNotReady:
        return channel, None, "not_ready"
    except AddressError:
        return channel, None, "no_operator"


def init_tools(
    channel_manager,
    bus,
    specialist_registry,
    mcp_registry=None,
    *,
    agent_role_map: dict | None = None,
    agent_registry=None,
    trigger_registry=None,
    engagement_registry=None,
    runtime=None,                         # NEW — Task C.1
    specialist_limiter=None,              # Task 6 (spec §4.6)
    specialist_telemetry=None,            # Task 6 (spec §4.6)
    agent_spawn_limiter=None,             # #283 — occupancy-debt spawn cap
    voice_job_route_cap: int = 5,
    agent_logs=None,
) -> None:
    """Initialize module-level references used by tool implementations.

    ``mcp_registry`` is required for specialist MCP-tool resolution at
    delegation time. ``trigger_registry`` is required for the
    ``get_schedule`` tool; callers that don't pass it get a degraded
    tool that returns "not initialized" on every call.
    Accepts ``None`` for legacy callers that don't pass
    it (the `_build_specialist_options` code path degrades to empty MCP
    servers — the specialist still runs but with only built-in tools).

    ``agent_role_map`` is a merged dict of role→AgentConfig covering both
    residents and specialists. It is REQUIRED for delegation authorization
    (A1): the ACL keys the caller's declared delegates off this map, so an
    omitted or empty map means no caller can be authorized to delegate at
    all. Only target *resolution* retains the ``specialist_registry``
    fallback for back-compat.

    ``runtime`` is the CasaRuntime container. Optional during migration
    (Task C.1); becomes required once all callsites use it (Task C.4).

    ``specialist_limiter``/``specialist_telemetry`` (Task 6, spec §4.6):
    optional concurrency cap + per-role telemetry. ``None`` (the default)
    disables both — ``_prelaunch`` skips the concurrency gate entirely and
    ``_run_delegated_agent`` skips cost/usage recording, matching the
    pre-Task-6 unbounded behaviour for every caller that doesn't wire them.

    ``agent_logs`` is the agent log sink; ``None`` logs nothing.
    """
    global _channel_manager, _bus, _specialist_registry, _mcp_registry, \
        _agent_role_map, _agent_registry, _trigger_registry, \
        _engagement_registry, _runtime, \
        _specialist_limiter, _specialist_telemetry, \
        _agent_spawn_limiter, \
        _voice_job_route_cap, _agent_logs  # noqa: PLW0603
    if (isinstance(voice_job_route_cap, bool)
            or not isinstance(voice_job_route_cap, int)
            or not 1 <= voice_job_route_cap <= 20):
        raise ValueError("voice_job_route_cap must be an integer from 1 to 20")
    _channel_manager = channel_manager
    _bus = bus
    _specialist_registry = specialist_registry
    _mcp_registry = mcp_registry
    _agent_role_map = dict(agent_role_map or {})
    _agent_registry = agent_registry
    _trigger_registry = trigger_registry
    _engagement_registry = engagement_registry
    # #1049: an ended engagement retires its unanswered authorization challenges.
    if engagement_registry is not None and hasattr(
            engagement_registry, "set_terminal_observer"):
        engagement_registry.set_terminal_observer(_on_engagement_terminal)
    _runtime = runtime
    _specialist_limiter = specialist_limiter
    _specialist_telemetry = specialist_telemetry
    _agent_spawn_limiter = agent_spawn_limiter
    _voice_job_route_cap = voice_job_route_cap
    _agent_logs = agent_logs


def _job_log(rec: Any, text: str) -> None:
    """One line in a job's agent log (see :func:`background_jobs.log_job_line`).
    Synchronous and never raises."""
    background_jobs.log_job_line(_agent_logs, rec, text)


def build_agent_role_map(residents: dict, specialists: dict, *,
                         strict: bool = False) -> dict:
    """The delegation role map, built the same way at boot
    (``casa_core._build_role_registry``) and on every reload
    (:func:`sync_agent_role_map`): residents, then the image-owned configurator
    (kind ``executor``), then specialists. A role in
    both tiers raises when *strict* (boot: a configuration bug) and otherwise
    keeps the resident entry with a warning (a reload must not brick). A
    resident or specialist named like the configurator keeps its entry."""
    overlap = set(residents) & set(specialists)
    if overlap and strict:
        raise ValueError(
            f"duplicate role(s) across residents and specialists: "
            f"{sorted(overlap)} — each role must be unique"
        )
    merged = dict(residents)
    configurator = _configurator_config()
    if (configurator is not None and configurator.role not in merged
            and configurator.role not in specialists):
        merged[configurator.role] = configurator
    for name, cfg in specialists.items():
        if name in merged:
            logger.warning(
                "sync_agent_role_map: role %r exists in both tiers — "
                "resident entry wins", name,
            )
            continue
        merged[name] = cfg
    return merged


def sync_agent_role_map(runtime: Any) -> None:
    """Rebuild the delegation role map from live runtime state.

    Called by the reload handlers after an agent/agents swap. Without
    this, the map stays a boot-time snapshot and ``delegate_to_agent``
    keeps resolving PRE-reload AgentConfigs — a specialist
    ``tools.allowed`` grant stays inert for every fresh delegation until
    a full add-on restart, even though ``casa_reload`` reports ok (P-6,
    live run 2026-07-11). Overlapping roles keep the resident entry and
    warn instead of raising: a reload must not brick on a collision
    boot would have rejected.

    #439: the TIER registry is refreshed here too. ``_agent_registry`` was
    captured once by :func:`init_tools` and never re-synced, while every reload
    path rebuilds ``runtime.agent_registry`` immediately before calling this —
    so ``tier_for_role`` answered from the boot snapshot for the life of the
    process. A role added after boot is simply absent from it, and the
    ``or "specialist"`` fallback then resolves a RESIDENT's plugin assignment
    against ``specialist:<role>``: the delegation launches without the plugins
    that role is actually assigned. Adopting the runtime's own registry can
    never be staler than what this held before.
    """
    global _agent_role_map, _agent_registry  # noqa: PLW0603
    residents = dict(getattr(runtime, "role_configs", {}) or {})
    registry = getattr(runtime, "specialist_registry", None)
    specialists = dict(registry.all_configs()) if registry is not None else {}
    _agent_role_map = build_agent_role_map(residents, specialists)
    fresh_registry = getattr(runtime, "agent_registry", None)
    if fresh_registry is not None:
        # Only ever REPLACE with a real registry: a runtime stand-in without one
        # (the reload test suite has several) must not blank the boot capture.
        _agent_registry = fresh_registry


_CONFIGURATOR_CFG: Any = None


def _configurator_config() -> Any:
    """The image-owned configurator's config, loaded once per process (it ships
    with Casa and cannot change without a release). ``None`` when it does
    not load, which is logged: configuring then stops, nothing else does."""
    global _CONFIGURATOR_CFG  # noqa: PLW0603
    if _CONFIGURATOR_CFG is None:
        try:
            import agent_loader
            _CONFIGURATOR_CFG = agent_loader.load_configurator()
        except Exception:  # noqa: BLE001 — never brick a reload or the boot
            logger.error("the configurator did not load", exc_info=True)
            return None
    return _CONFIGURATOR_CFG


def _text_result(text: str, *, is_error: bool = False) -> dict:
    """One plain-text tool result, marked as an error where it is one.

    ``is_error`` is what turns a failure into a FAILURE in turn telemetry: the
    SDK's MCP adapter reads it and the wire field follows, so without it
    ``sdk_logging.log_tool_result`` emits ``ok=True`` for a message that
    reached nobody (the same F-7/O-1 reasoning as :func:`_result`, which takes
    a JSON payload rather than plain text).
    """
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["is_error"] = True
    return out


_DELIVERS_TO_OPERATOR_REFUSAL = (
    "this webhook's final reply is delivered to the operator; reply instead "
    "of calling send_message")


@tool(
    "send_message",
    "Send a message to a user through a communication channel.",
    {"message": str, "channel": str},
)
async def send_message(args: dict) -> dict:
    """Send a message through a named channel."""
    message = args.get("message", "")
    channel = args.get("channel", "telegram")

    # #1038: the entry snapshot — the scope this send commits under.
    _origin = _snapshot_origin()
    scope = _current_scope(_origin)
    if scope is None:
        return _text_result(f"Error: {_NO_SCOPE_MESSAGE}.", is_error=True)
    # #1142 (INV-TRIG-018): on a `deliver: operator` webhook turn the final
    # reply IS the delivery, so a tool send would be a second message. The
    # tool is not offered to such a turn; this refusal is the defence in depth,
    # read from the scope's registered facts, never from tool input.
    if scope.delivers_to_operator:
        return _text_result(f"Error: {_DELIVERS_TO_OPERATOR_REFUSAL}.",
                            is_error=True)
    # Release A / Layer 1 egress binding (#1038 R2: now the scope's
    # DestinationOperatorOnly obligation, registered at mint): an UNTRUSTED
    # webhook turn may notify only the operator's Telegram surface — the
    # caller-selected channel is replaced so third-party content can't be
    # relayed to arbitrary channels (confused deputy). Trusted /invoke and
    # all non-webhook origins are unaffected.
    channel = scope.resolve_channel(channel)

    if _channel_manager is None:
        return _text_result("Error: tools not initialized", is_error=True)

    # Telegram is one transport over every bot; the send goes through the
    # bot of this turn's route, decided where the turn was born.
    ch = _transport() if channel == "telegram" else _channel_manager.get(channel)
    if ch is None:
        return _text_result(f"Error: channel '{channel}' not found",
                            is_error=True)

    # #990: report what the channel actually did. The unconditional "Message
    # sent" this replaced was a FALSE positive on two shipped paths — Telegram
    # returns NOT_DELIVERED having made zero Bot API calls while its
    # application is not started, and voice `send` is a documented no-op — and
    # a scheduled turn told the message was sent ends with `<silent/>` as the
    # closing-silence convention instructs, so the operator receives nothing at
    # all. The turn's only signal was the false one.
    #
    # Only a PROVEN negative is an error. `UNKNOWN` (or a `None` from a channel
    # that has not opted into the contract) keeps today's text, explicitly
    # rather than by omission — the #556 rule: "cannot report" is not "failed",
    # and reporting it as failure would make every send on such a channel look
    # lost.
    # #1038: admission — the turn's obligations applied to the text at the
    # moment it is committed, against the evidence gathered so far.
    admitted = scope.admit(IntentKind.DISCRETE, message)
    # The delivery context carries the turn's route and nothing else: the
    # destination is the route's operator chat, never a field of the origin.
    ctx: dict[str, Any] = route_context(_origin)
    try:
        outcome = await ch.send(admitted, ctx)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a send fault is a tool result
        # This arm makes NO claim about what reached the operator, and that is
        # the whole of its design. Three review findings across two rounds each
        # said the same thing about an earlier wording here — that it asserted
        # more than an exception establishes: "nothing reached the operator" is
        # false for a response lost in transit after Telegram processed the
        # request; "it may have been delivered" is false for a `Forbidden`,
        # which the API evaluated and declined; "then X stopped the rest" is
        # false when the raising call was the last chunk. So the judgment is
        # cut rather than sharpened a third time. Casa DOES own that taxonomy —
        # `channels/telegram.py`'s `_SERVER_REFUSALS` and
        # `_edit_failure_outcome`, built by review for this exact question
        # — and it belongs to the channel, which knows its own transport, not
        # to a channel-agnostic tool inferring from an exception class. The
        # only delivery claim this tool makes is the one the channel itself
        # reports through `DeliveryOutcome`.
        #
        # Still an error, so the turn speaks rather than falling silent: a
        # duplicate is the lesser harm, the direction `send_media`'s
        # `delivery_uncertain` already chose.
        logger.warning("send_message: %s send failed: %s", channel,
                       type(exc).__name__)
        return _text_result(
            f"Error: sending via {channel} failed ({type(exc).__name__}).",
            is_error=True)
    if outcome is DeliveryOutcome.NOT_DELIVERED:
        logger.warning("send_message: %s channel delivered nothing", channel)
        return _text_result(
            f"Error: the {channel} channel delivered nothing — the message "
            f"did NOT reach the operator.", is_error=True)
    # #1079: only the channel's positive report confirms the commitment the
    # admission recorded; UNKNOWN keeps today's tool result but leaves it
    # unconfirmed, so a turn ending in silence after it stays owed.
    if outcome is DeliveryOutcome.DELIVERED:
        admitted.mark_delivered()
    # #1038 §6.5: the record the model builds on names the exact text Casa
    # added, so the transcript says what the operator saw.
    if admitted.annotations:
        lines = "”, “".join(admitted.annotations)
        return _text_result(f"Message sent via {channel}. Casa prefixed: “{lines}”")
    return _text_result(f"Message sent via {channel}.")


# ---------------------------------------------------------------------------
# stop_work — the resident stops work a specialist carries on by itself (browsing)
# ---------------------------------------------------------------------------

STOP_WORK_ONLY_OPERATOR = "Only the person's own message can stop work."
_STOP_WORK_RESULTS = {
    "stopped": ("Stopped: {task}. Tell the person in one plain line what stopped, in his "
                "words, and nothing about how you found it."),
    "ended": "It had already ended. Tell the person in one plain line.",
    "already_stopped": ("It is already stopped and the person has been told. Add nothing "
                        "about it; answer only what else his message asks, or with exactly "
                        "<silent/>."),
    "not_taken": ("Casa Browser isn't answering; the browsing stops as soon as it does. Tell "
                  "the person in one plain line."),
}
_STOP_WORK_RESULTS["in_flight"] = _STOP_WORK_RESULTS["already_stopped"]


@tool(
    "stop_work",
    "Stop work a specialist carries on by itself, such as browsing. Without ref: what is "
    "running, each with its ref. With a ref from that list: stops it at once. Use it "
    "yourself whenever the person asks to stop or cancel such work; never delegate a stop. "
    "Unless you must ask him which one, write nothing to the person between the two calls.",
    {"type": "object", "properties": {"ref": {"type": "string"}}, "required": []},
)
async def stop_work(args: dict) -> dict:
    """The resident's direct stop (``plugin_stop``): no model turn, no specialist turn. Only on the
    operator's own live turn — the spawn cap's ``_is_agent_context`` False and the sender the
    operator as configured now; voice, narration, scheduled and delegated turns are refused."""
    import plugin_stop
    from operator_calls import is_configured_operator

    origin = _snapshot_origin()
    chat_id, user_id = origin.get("chat_id"), origin.get("user_id")
    if _is_agent_context(origin) or not is_configured_operator(chat_id, user_id):
        return _text_result(f"Error: {STOP_WORK_ONLY_OPERATOR}", is_error=True)
    # The requests are those admitted on the bot of this turn — its id is read
    # from the endpoint the turn's route resolves to, never from the route —
    # and those this turn's agent asked for that run in another agent's chat.
    transport = _transport()
    bot_id = None
    if transport is not None:
        try:
            bot_id = transport.endpoint_for_route(origin, site="stop_work").bot_id
        except (RouteMissing, AddressError, EndpointUnavailable, EndpointNotReady) as exc:
            logger.warning("stop_work: no Telegram endpoint for the turn: %s", exc)
    if bot_id is None:
        return _text_result("Error: the stop cannot be asked right now; try again in a "
                            "moment.", is_error=True)
    me = str(origin.get("role") or "")
    operator_id = int(user_id)

    def _visible(seg: str, ref: Any):
        if not isinstance(ref, str):
            return None
        return plugin_stop.visible_confirmation(seg, ref, bot_id=bot_id,
                                                operator_id=operator_id, role=me)

    plugins = await plugin_stop.stop_plugins()
    ref = args.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        lines = []
        for role, _cfg, rp, seg, tool_name in plugins:
            listing = await plugin_stop.call_stop(rp, tool_name, {})
            name = _display_name_for_role(role)
            if listing.get("failed"):
                lines.append(f"- {name}: what it is doing could not be listed right now.")
            for r in listing.get("requests") or []:
                if _visible(seg, r.get("ref")) is None:
                    continue            # not his, or not this agent's to see
                tasks = "; ".join(t for t in (r.get("tasks") or []) if isinstance(t, str))
                lines.append(f"- {r.get('ref')}: {name} — {tasks} ({r.get('state')})")
        return _text_result("\n".join(lines) if lines else "Nothing is running.")
    ref = ref.strip()
    if not ref.startswith(plugin_stop.REF_PREFIX) or len(ref) > plugin_stop.REF_MAX:
        return _text_result("Error: that is not a ref from the list; call stop_work without "
                            "ref to see what is running.", is_error=True)
    for role, _cfg, _rp, seg, _tool in plugins:
        found = _visible(seg, ref)
        if found is None:
            continue                    # no request of this plugin he may stop has it
        on_bot = found.bot_id           # the bot the request was asked on

        async def _tell(outcome: dict, seg=seg, on_bot=on_bot) -> None:
            mark = plugin_stop.MARKS.get(outcome.get("outcome"))
            if mark is not None:
                await plugin_stop.settle_run_card(
                    plugin_stop.confirmation_for(seg, on_bot, operator_id, ref), mark)

        out = await plugin_stop.stop(role, seg, ref, bot_id=on_bot, operator_id=operator_id,
                                     tell=_tell)
        o = out.get("outcome")
        if o in ("unknown", "unavailable"):
            continue
        task = "; ".join(out.get("tasks") or []) or "the browsing"
        return _text_result(_STOP_WORK_RESULTS.get(o, _STOP_WORK_RESULTS["not_taken"])
                            .format(task=task))
    return _text_result("Error: nothing running has that ref; call stop_work without ref to "
                        "see what is running.", is_error=True)


# ---------------------------------------------------------------------------
# send_media — reusable agent->channel media delivery (v0.73.0, spec §3).
# ---------------------------------------------------------------------------

_CAPTION_MAX = 1024
_FILENAME_MAX_BYTES = 255


def _validate_delivery_filename(name: str, kind: str) -> str | None:
    """Return *name* if it is a safe delivered filename for *kind*, else None.
    basename-only, control-free, <=255 bytes, extension (case-insensitive) in the
    kind's allowlist."""
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        return None
    if any(ord(c) < 0x20 for c in name):
        return None
    if len(name.encode("utf-8")) > _FILENAME_MAX_BYTES:
        return None
    ext = os.path.splitext(name)[1].lower()
    if ext not in MEDIA_POLICIES[kind].extensions:
        return None
    return name


def outbox_for_current_context() -> "plugin_outbox.PluginOutbox | None":
    """The plugin outbox a media claim is made from: the shared outbox.
    ``None`` when no outbox is initialised. Shared by ``send_media`` and the
    result broker's ``operator_file`` delivery (S3)."""
    return plugin_outbox.get_outbox()


# S7a (B2): the one Casa-authored note under the post echoes a resident reads
# in a sync delegation's result or a completion notice — the person has seen
# those posts, so a result that adds nothing to them ends in silence. The
# resident's doctrine names the rule in words; the sentinel itself lives only
# here, in Casa's text (a compiled doctrine may not carry markup).
POST_ECHO_SILENCE_NOTE = (
    "(Casa: the person has already seen these posts in their chat. Do not retell "
    "them. If the rest of this result says nothing beyond them, reply with exactly "
    "<silent/>.)")


# #1348: the note a DM turn's ask_user result carries once its question is
# live in the person's chat. The question already stands there with its
# buttons, so anything the resident writes after the call only narrates it;
# like the post echo's note, the sentinel lives here in Casa's text because a
# compiled doctrine may not carry markup.
ASK_USER_SILENCE_NOTE = (
    "(Casa: the person now sees this question with its buttons in their chat. "
    "Do not announce or retell it. If you have nothing else for them, reply "
    "with exactly <silent/>.)")


# ---------------------------------------------------------------------------
# Tasks. A task is work that does not belong to an agent's conversation: a
# delegation or a plugin job. Its launcher decides two destinations once, when
# the task is born (``task_model``), and never recomputes them: its delivery,
# where every post of the task goes (``None``: it posts nothing, for its whole
# life), and its requester, who receives its result. A task of an agent with
# its own open chat shows there: a start line naming who asked, the message
# its delegate closed its turn with, and the operator's replies reach its live
# session.
# ---------------------------------------------------------------------------

# The brief as quoted in a task's start line and in its executor's note.
_TASK_START_BRIEF_CHARS = 200
_TASK_NOTE_BRIEF_CHARS = 120
# One task post (the start line, or every page of the answer) is bounded: a
# slow chat never holds the work or its requester's result.
_TASK_POST_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class TaskBirth:
    """What a task's launcher decided when the task was born: its id and
    brief, the agent that runs it, who receives its result, and where its
    posts go."""
    task_id: str
    brief: str
    executor: str
    requester: Any            # task_model.Requester
    delivery: "TgAddress | None"

    @property
    def in_own_chat(self) -> bool:
        """The task shows in its executor's own chat: it has a delivery and
        its executor is not a resident (a resident's work keeps today's
        posts in its own chat)."""
        cfg = _agent_role_map.get(self.executor)
        return self.delivery is not None and getattr(cfg, "kind", "") != "resident"

    @property
    def leaves_note(self) -> bool:
        """The executor's conversation did not ask for this work, so it learns
        the outcome as a note on its next turn."""
        return self.in_own_chat and self.requester.role != self.executor


def _task_birth(task_id: str, brief: str, executor: str, origin: dict) -> TaskBirth:
    """The task *task_id* of *executor*, born now from the turn *origin*: the
    requester is that turn's agent on that turn's route, and the delivery is
    decided here, once."""
    import task_model
    route = carry_route(origin or {})
    requester = task_model.Requester(str((origin or {}).get("role") or "assistant"), route)
    return TaskBirth(task_id=task_id, brief=brief if isinstance(brief, str) else "",
                     executor=executor, requester=requester,
                     delivery=task_model.delivery_for(executor, route))


def _task_record_fields(birth: "TaskBirth | None") -> dict:
    """The task record's fields, as born: the agent that runs it, who
    receives its result, where its posts go. Nothing for work that is no
    task (a voice delegation)."""
    if birth is None:
        return {}
    return {"executor": birth.executor, "requester": birth.requester,
            "delivery": birth.delivery}


def _clip_line(text: str, limit: int) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[:limit - 1].rstrip() + "…"


# Delegations whose input destination is open: task id → executing role. The
# run attaches its client under that id; the task's end closes it.
_TASK_INPUTS: dict[str, str] = {}
# The line a delegate's context carries when its closing message is posted to
# the person in its own chat.
ANSWER_SHOWN_LINE = (
    "answer_shown: your closing message is posted to the operator in your own chat with "
    "them; write it as you would say it to them, the answer itself, with no working notes\n")
# Delegations whose run's closing message is posted to the person in its
# executor's own chat, by task id: the run is told so. Cleared at the input's end.
_ANSWER_IN_CHAT: set[str] = set()


def _open_task(birth: TaskBirth) -> None:
    """The task's post index and its input destination exist before its first
    post: a reply to its start line, sent before its session's client exists,
    is queued for the task, never taken for the conversation's."""
    import task_input
    import task_posts
    task_posts.open(birth.task_id, birth.delivery)
    task_input.open_pending(birth.task_id)
    _TASK_INPUTS[birth.task_id] = birth.executor


def _end_task_input(task_id: str, *_ignored: Any) -> None:
    """The task's run is over: a later reply to its posts is a reply to an
    ended task. Idempotent; a done-callback, so a run cancelled before it
    started still ends it."""
    _ANSWER_IN_CHAT.discard(task_id)
    if _TASK_INPUTS.pop(task_id, None) is None:
        return
    import task_input
    try:
        task_input.close(task_id)
    except Exception:  # noqa: BLE001 — closing never fails the work
        logger.warning("task %s input not closed", task_id[:8], exc_info=True)


def _attach_task_input(task_id: str, role: str, client: Any) -> bool:
    """*client*, the live session of *role*'s run for the task *task_id*,
    takes the task's queued and later replies. A run of another agent under
    the same id (a hand-off inside the task) attaches nothing."""
    if not task_id or _TASK_INPUTS.get(task_id) != role:
        return False
    import task_input
    try:
        task_input.attach(task_id, client)
    except Exception:  # noqa: BLE001 — the run goes on without the operator's replies
        logger.warning("task %s input not attached", task_id[:8], exc_info=True)
        return False
    return True


def _task_post_admitted(birth: TaskBirth, text: str, intent: IntentKind) -> Any:
    """*text* as a post of the task would carry it, or ``None`` when it would
    post nothing: a closing silence, or no words once the marker is gone."""
    from output_boundary import without_sentinels
    scope = TurnScope(id=birth.task_id, cid="-", role=birth.executor,
                      display_name=_display_name_for_role(birth.executor),
                      channel="telegram", message_type="task")
    admitted = scope.admit(intent, text)
    if intent is IntentKind.FINAL_REPLY and not admitted.suppressed:
        admitted = admitted.with_text(without_sentinels(admitted))
    if admitted.suppressed or not admitted.strip():
        return None
    return admitted


def _task_endpoint(birth: TaskBirth) -> Any:
    """The endpoint that posts at the task *birth*'s delivery, or ``None``
    when there is none now: no delivery, no Telegram, or no endpoint serves
    the delivery's bot (its bot was released)."""
    delivery, transport = birth.delivery, _transport()
    if delivery is None or transport is None:
        return None
    try:
        return transport.endpoint(delivery)
    except (AddressError, EndpointUnavailable) as exc:
        logger.warning("task %s: no endpoint for its chat: %s", birth.task_id[:8],
                       type(exc).__name__)
        return None


def _answer_posts(birth: "TaskBirth | None", answer: str) -> bool:
    """Whether settling the task *birth* with *answer* posts it in its
    executor's own chat — the same rule the post itself applies: an endpoint
    serves its delivery now, and *answer*, the delegate's whole closing
    message, is not silence."""
    try:
        return (birth is not None and birth.in_own_chat
                and _task_endpoint(birth) is not None
                and _task_post_admitted(birth, answer, IntentKind.FINAL_REPLY) is not None)
    except Exception:  # noqa: BLE001 — a steer, never the work's fault
        logger.warning("task answer post not predicted", exc_info=True)
        return False


async def _post_task_text(birth: TaskBirth, text: str, *, intent: IntentKind,
                          slot: str, kind: str = "") -> bool:
    """Post *text* at the task's delivery, through its executor's endpoint,
    every page filed under the task (its post record's owner is the task), so
    a reply to any page reaches the task. *slot* names which of the task's
    posts this is (its start line or its answer): the two share owner and
    tool-use id, so the slot alone keeps them two posts, each with its own
    memory line. Bounded; a failure is logged and reported as ``False``."""
    import result_broker as rb
    ep = _task_endpoint(birth)
    if ep is None:
        return False
    delivery = birth.delivery
    admitted = _task_post_admitted(birth, text, intent)
    if admitted is None:
        return False
    post = rb.PostRecord(role=birth.executor, operator_id=delivery.chat_id, plugin="",
                         slot=slot, tool_use_id=birth.task_id, owner=birth.task_id,
                         posted_at=time.time(), kind=kind)
    try:
        outcome = await asyncio.wait_for(
            ep.send_response(admitted, {"chat_id": delivery.chat_id, "_post": post}),
            _TASK_POST_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — a task post is never the work's fault
        logger.warning("task %s post not delivered: %s", birth.task_id[:8],
                       type(exc).__name__)
        return False
    return outcome is DeliveryOutcome.DELIVERED


async def _post_task_start(birth: TaskBirth) -> None:
    """The task's start line in its executor's chat: who asked, and what."""
    who = _display_name_for_role(birth.requester.role)
    await _post_task_text(
        birth,
        f"{who} asked me: {_clip_line(birth.brief, _TASK_START_BRIEF_CHARS)}",
        intent=IntentKind.DISCRETE, slot="task-start")


async def _settle_task(birth: "TaskBirth | None", *, answer: "str | None" = None,
                       failure: str = "") -> None:
    """A task of an agent with its own chat has ended: the message its
    delegate closed its turn with, whole (nothing when that is silence), is
    posted at its delivery, and its executor's conversation, when it did not
    ask for the work, gets a body-free note for its next turn. Never raises."""
    if birth is None or not birth.in_own_chat:
        return
    try:
        if answer is not None:
            import result_broker as rb
            await _post_task_text(birth, answer, intent=IntentKind.FINAL_REPLY,
                                  slot="task-answer", kind=rb.TASK_ANSWER)
        _note_task(birth, "asked you",
                   "you answered" if answer is not None
                   else f"you couldn't finish it ({failure or 'unknown'})")
    except Exception:  # noqa: BLE001 — the requester's result never waits on this
        logger.warning("task %s not settled in its chat", birth.task_id[:8], exc_info=True)


def _note_task(birth: TaskBirth, asked: str, outcome: str) -> None:
    """The body-free line the executor's conversation reads on its next turn,
    when it did not ask for the task itself: who asked what, and how it
    ended."""
    if not birth.leaves_note:
        return
    import chat_notes
    who = _display_name_for_role(birth.requester.role)
    chat_notes.record(
        birth.delivery.bot_id, birth.delivery.chat_id,
        f"{who} {asked}: {_clip_line(birth.brief, _TASK_NOTE_BRIEF_CHARS)} — {outcome}")


def _record_birth(rec: Any) -> "TaskBirth | None":
    """A task record's destinations, as its launcher stamped them; ``None``
    for a record that carries none."""
    requester = getattr(rec, "requester", None)
    if requester is None or not hasattr(rec, "delivery"):
        return None
    return TaskBirth(task_id=str(getattr(rec, "id", "") or ""),
                     brief=str(getattr(rec, "task", "") or ""),
                     executor=str(getattr(rec, "executor", "")
                                  or getattr(rec, "role_or_type", "") or ""),
                     requester=requester, delivery=getattr(rec, "delivery", None))


def _surface_refusal(executor: str) -> Any:
    """The typed refusal of a surface of *executor* that may not post: the
    capability rule's own, or — its chat having opened after the task was
    born — that the task posts nothing."""
    import own_voice
    label = _display_name_for_role(executor)
    refusal = own_voice.require_open_chat(executor, label=label)
    if refusal is None:
        refusal = own_voice.Refusal(
            "own_chat_not_open",
            f"{label}'s chat was not open when this task started, so this task "
            "posts nothing there; put it in your answer instead.")
    return refusal


def _with_post_echo_payload(payload: dict, owner: str, *, shown_in: str = "") -> dict:
    """The sync delegation's result, ok or error: the echo rides ``text``
    when the result has one, else ``message`` — a post proven before the
    work failed is still a post the resident must learn of."""
    key = "text" if "text" in payload else "message"
    payload[key] = _with_post_echo(str(payload.get(key) or ""), owner, shown_in=shown_in)
    return payload


# The note after the post echo when the posts went to the person in the
# delegate's own chat: the person asked the resident here, so the resident
# closes the loop rather than staying silent.
OWN_CHAT_ECHO_NOTE = (
    "(Casa: the person saw these posts in {name}'s chat, not here, and asked you, so close "
    "the loop here in your own words: usually one short line on what came of it. Do not "
    "retell the posts. Reply with exactly <silent/> only if nothing at all is worth saying.)")


def _with_post_echo(text: str, owner: str, *, shown_in: str = "") -> str:
    """S3 §6: append the body-free echo of every post a plugin proved under
    *owner* (a delegation id or an engagement id) to *text* — Casa's own
    lines after the delegate's answer, drained atomically so a post is
    echoed once. The text is returned unchanged when nothing was posted.
    *shown_in*, the name of the delegate whose own chat the posts went to,
    says so and asks the resident to close the loop."""
    import result_broker as _rb
    events = _rb.POSTS.drain(owner)
    if shown_in:
        lines = _rb.echo_lines(events, where=f"to the person in {shown_in}'s chat")
        note = OWN_CHAT_ECHO_NOTE.format(name=shown_in)
    else:
        lines, note = _rb.echo_lines(events), POST_ECHO_SILENCE_NOTE
    if not lines:
        return text
    return (f"{text}\n\n" if text else "") + "\n".join([*lines, note])


def _own_chat_name(birth: "TaskBirth | None") -> str:
    """The display name of the agent whose own chat the task *birth* shows
    in, or ``""`` when it shows in none."""
    if birth is not None and birth.in_own_chat:
        return _display_name_for_role(birth.executor)
    return ""


# Slice 5t: the note a delegation result or notice carries when the delegate
# closed its turn with `<silent/>` (casa-test 2026-11-01: Fido posted a start
# card, narrated a line, closed silent; the assistant read the narration as
# progress and wrote "Fido is still on it."). The delegate's closing silence is
# its answer to the person: nothing.
SILENT_CLOSE_NOTE = (
    "(Casa: {name} closed its turn with <silent/>: it has nothing for the person "
    "now, and what it wrote before that is not for them. Do not pass it on, "
    "narrate it, or say {name} is still working. If you have nothing else for "
    "them, reply with exactly <silent/>.)")


# The same, for a task shown in the delegate's own chat: the person asked the
# resident, so the resident is not steered to silence; whether to close the
# loop is the resident's call.
OWN_CHAT_SILENT_CLOSE_NOTE = (
    "(Casa: {name} closed its turn with <silent/>: it has nothing more for the person, and "
    "what it wrote before that is not for them. Do not pass it on, narrate it, or say "
    "{name} is still working.)")

# The note on a delegation still running when its call returns, for a task
# shown in the delegate's own chat: the person watches it work there and gets
# its answer there, so the resident owes no line saying it is still running
# (one written while the answer was already landing reached the person after it).
OWN_CHAT_PENDING_NOTE = (
    "(Casa: {name} is working on this in {name}'s own chat, where the person sees it "
    "and will get its answer. Nothing is owed here: do not say {name} is still working. "
    "If you have nothing else for them, reply with exactly <silent/>.)")


def _own_chat_pending(birth: "TaskBirth | None", role: str) -> dict:
    """The pending result's note when the task shows in *role*'s own chat."""
    if birth is None or not birth.in_own_chat:
        return {}
    return {"own_chat_note": OWN_CHAT_PENDING_NOTE.format(
        name=_display_name_for_role(role))}


# The note for a task whose delegate's closing message was posted to the
# person in the delegate's own chat: the resident closes the loop briefly
# instead of retelling the answer.
OWN_CHAT_ANSWER_NOTE = (
    "(Casa: the person already read {name}'s answer in {name}'s chat. Close the loop here "
    "in a line or two of your own: what came of it, plus anything only you know. Do not "
    "retell the answer.)")

# The line that tells the requester what the person wrote straight into the
# task while it ran (a reply in the delegate's own chat).
OPERATOR_WORDS_NOTE = ("(Casa: the person also wrote to {name} directly while it worked "
                       "on this: {words}. {name} got those words in the task itself and worked "
                       "with them, so they are not yours to pass on or to start again as a "
                       "new task.)")


def _with_silent_close_note(text: str, output: "DelegatedOutput", role: str, *,
                            own_chat: bool = False) -> str:
    """*text* with :data:`SILENT_CLOSE_NOTE` after it when the delegate's
    whole closing message (:func:`_closing_text`) is silence with a sentinel
    in it — :data:`OWN_CHAT_SILENT_CLOSE_NOTE` when the task shows in the
    delegate's own chat (*own_chat*). An empty closing message is not a
    closing silence. Judged on the whole message, never the copy the result
    cap bounds: a long answer that opens with the sentinel is an answer, and
    is posted as one. Never raises."""
    from output_boundary import SILENCE_SENTINEL, strips_to_silence
    try:
        words = _closing_text(output)
        if (not isinstance(words, str) or SILENCE_SENTINEL not in words
                or not strips_to_silence(words)):
            return text
        note = (OWN_CHAT_SILENT_CLOSE_NOTE if own_chat else SILENT_CLOSE_NOTE).format(
            name=_display_name_for_role(role))
        return f"{text}\n\n{note}" if text else note
    except Exception:  # noqa: BLE001 — a steer, never the work's fault
        logger.warning("silent-close note not added", exc_info=True)
        return text


def _with_own_chat_answer_note(text: str, shown_in: str, posted: bool) -> str:
    """*text* with :data:`OWN_CHAT_ANSWER_NOTE` after it when the delegate's
    closing message is posted (*posted*) to the person in *shown_in*'s own
    chat."""
    if not (shown_in and posted):
        return text
    note = OWN_CHAT_ANSWER_NOTE.format(name=shown_in)
    return f"{text}\n\n{note}" if text else note


def _with_operator_words(text: str, said: "list[str]", executor: str) -> str:
    """*text* with one Casa line holding *said*, what the person wrote
    straight into the task while *executor* ran it, when they did. Never
    raises."""
    try:
        if not said:
            return text
        words = " ".join(f"«{w}»" for w in said)
        line = OPERATOR_WORDS_NOTE.format(name=_display_name_for_role(executor), words=words)
        return f"{text}\n\n{line}" if text else line
    except Exception:  # noqa: BLE001 — a steer, never the work's fault
        logger.warning("operator words not added", exc_info=True)
        return text


def _take_said(task_id: str) -> "list[str]":
    """What the person wrote into the task *task_id* while it ran, taken
    once. Never raises."""
    try:
        import task_input
        return list(task_input.take_said(task_id) or [])
    except Exception:  # noqa: BLE001 — a steer, never the work's fault
        logger.warning("task %s: operator words not read", str(task_id)[:8], exc_info=True)
        return []


def _closing_text(output: "DelegatedOutput") -> str:
    """The message the delegate closed its turn with, whole; its whole text
    when the producer reports no messages."""
    final = getattr(output, "final_message", None)
    return output.text if final is None else final


def _closing_words(output: "DelegatedOutput") -> str:
    """The message a delegate closed its turn with — its last text-bearing
    message, bounded at the result cap — never the narration it wrote between
    tool calls. A closing ``<silent/>`` is that message. An output with no
    reported closing message is one message: its whole text. This is the copy
    that travels; whether the delegate closed silent is judged on the whole
    message (:func:`_closing_text`), never on this bounded copy. Never raises:
    an output it cannot read yields ``""``."""
    try:
        words = _closing_text(output)
        return specialist_limits.truncate_output(words)[0] if isinstance(words, str) else ""
    except Exception:  # noqa: BLE001
        logger.warning("delegate closing message not read", exc_info=True)
        return ""


def _closing_beyond_cut(output: "DelegatedOutput", prefix: str, truncated: bool) -> str:
    """The delegate's closing message, to travel beside a result *prefix* the
    length cap cut — ``""`` when nothing was cut, when the kept prefix already
    ends with the closing message, or when that message is silence. The cut
    keeps the start of the turn, so an answer the delegate closed with would
    otherwise be lost behind a long narration; it is returned whole (bounded
    at the same cap), its sentinels dropped. Whether it is silence is judged
    on the whole message, never the bounded copy; a bounded copy with no
    words left once its sentinels go carries nothing. Never raises."""
    if not truncated:
        return ""
    from output_boundary import strips_to_silence, without_sentinels
    try:
        whole = _closing_text(output)
        if not isinstance(whole, str) or strips_to_silence(whole):
            return ""
        words = _closing_words(output)
        if prefix.endswith(words):
            return ""
        kept = without_sentinels(words)
        return kept if kept.strip() else ""
    except Exception:  # noqa: BLE001 — the result stands without it
        logger.warning("delegate closing message not attached", exc_info=True)
        return ""


async def _classify_send(ch, content, kind, filename, origin, caption, post=None) -> dict:
    """Attempt the channel send and classify the outcome into a payload dict.
    Never raises a send/channel exception (the caller's finally still cleans up
    the claim). The landed file is filed under *post*."""
    from telegram.error import (
        BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut,
    )
    try:
        await ch.send_media(content, kind, filename, context=origin, caption=caption,
                            post=post)
        return {"status": "ok", "kind_error": None, "kind": kind,
                "filename": filename,
                "summary": f"delivered {kind} {filename!r}",
                # #1038 §6.5: the line the caption carried, if any.
                **({"casa_prefixed": list(caption.annotations)}
                   if getattr(caption, "annotations", None) else {})}
    except NotImplementedError:
        return {"status": "error", "kind_error": "unsupported_channel", "kind": kind,
                "message": "channel cannot deliver media"}
    except (BadRequest, Forbidden) as exc:
        return {"status": "error", "kind_error": "rejected", "kind": kind,
                "message": f"telegram refused: {type(exc).__name__}"}
    except (TimedOut, NetworkError, RetryAfter) as exc:
        return {"status": "error", "kind_error": "delivery_uncertain", "kind": kind,
                "message": f"delivery uncertain: {type(exc).__name__}; not retried"}
    except TelegramError as exc:
        return {"status": "error", "kind_error": "delivery_uncertain", "kind": kind,
                "message": f"delivery uncertain: {type(exc).__name__}; not retried"}
    except UnadmittedText:
        # #1038: never "the channel was down" — the caption was not admitted.
        return {"status": "error", "kind_error": "refused", "kind": kind,
                "message": "caption refused: not admitted text"}
    except (RouteMissing, AddressError, EndpointUnavailable) as exc:
        # The origin names no bot that can carry the file: nothing was sent.
        logger.warning("send_media: no Telegram endpoint for the origin: %s", exc)
        return {"status": "error", "kind_error": "channel_unavailable", "kind": kind,
                "message": "no Telegram bot for this conversation"}
    except RuntimeError:
        return {"status": "error", "kind_error": "channel_unavailable", "kind": kind,
                "message": "channel not started"}


# #541 (design r3-r4): mechanical outbound quota for send_media in
# SPECIALIST context — a consented bundle may hold the grant, but role
# content cannot buy an unbounded outbound Telegram channel (max_turns has
# no schema maximum). Lifetime budget per context, keyed by engagement id
# (interactive) or the server-stamped ``_delegation_id`` (ephemeral); a
# specialist-classified call with NO key is refused, never unmetered (r3,
# both reviewers). In-memory by design (quota, not authorization — a
# restart refilling it is acceptable); the key map is FIFO-bounded so it
# cannot grow without limit. Debit is a synchronous check-and-increment
# BEFORE the first claim/delivery await; attempts count and failed or
# uncertain deliveries are never refunded — a refund path would let
# delivery errors mint budget (r3 Sol).
_SPECIALIST_MEDIA_SEND_BUDGET: int = 8
_MEDIA_SEND_DEBITS: "OrderedDict[str, int]" = OrderedDict()
_MEDIA_SEND_DEBITS_MAX_KEYS: int = 512

# #541: the server-generated delegation/voice-job id for the CURRENT
# delegated run — set by the launching call site around its create_task
# (the task's context snapshot carries it into the delegated coroutine and
# every descendant, exactly the inheritance origin_var rides), read by
# _run_delegated_agent when it stamps ``_delegation_id`` onto child_origin.
# A ContextVar rather than a parameter: the runner seam is monkeypatched by
# exact signature in ~30 test doubles.
_delegation_quota_key: ContextVar[str] = ContextVar(
    "_delegation_quota_key", default="")

# S7a (INV-BGJOB-008): set ONLY around the one ``_prelaunch`` call of a
# specialist's start of its own job (``_launch_interactive_engagement``), so
# that call skips the two gates a self-start has no use for — the delegation
# ACL (no other agent is targeted) and the depth cap. Reset before any task is
# created, so nothing the launch spawns inherits it.
_self_host_prelaunch: ContextVar[bool] = ContextVar("_self_host_prelaunch", default=False)

# S5 §5.2: the ``PinnedRun`` of a stored-call tap, set by
# ``stored_call_settler.handle_tap`` around its create_task of the delegated
# runner; the options builder consumes its captured build input instead of
# deriving its own, injects the pin, and the runner lets it own the client.
# A ContextVar for the same reason as the two above: the runner and builder
# seams are monkeypatched by signature across the suite.
_pinned_run: ContextVar["Any | None"] = ContextVar("_pinned_run", default=None)

# #1197: the lane's unwinding guard — a list bound by one use of an agent's
# lane (a delegation, a plugin continuation, Casa continuing the configurator)
# around its call of the bounded runner, into which
# ``_run_delegated_agent_bounded`` puts the inner task it creates — so the
# lane, not the runner, can tell a run that ended from one still unwinding
# past the teardown bound. A ContextVar for the same reason as the ones above:
# the bounded runner is monkeypatched by its five-argument signature across
# the suite. Callers outside a lane leave it None.
_lane_run_sink: ContextVar["list | None"] = ContextVar("_lane_run_sink", default=None)

# #1212: the ``pinned_run.ProcessTree`` ``_run_delegated_agent_bounded`` owns for
# the run it creates — set around that create_task and reset at once, read and
# cleared by the run at its entry, so a run nested inside it gets its own. A
# ContextVar for the same reason as ``_lane_run_sink``.
@dataclass(frozen=True)
class ConversationTurn:
    """A continuation turn's conversation: the binding it belongs to, and the session
    it resumes, or None for a fresh one the binding then adopts."""
    binding_ref: str
    resume: str | None


# Set by ``plugin_continuations`` around its own turn alone; the runner reads and clears it.
_conversation: ContextVar["ConversationTurn | None"] = ContextVar(
    "_conversation", default=None)

_bounded_tree: ContextVar["Any | None"] = ContextVar("_bounded_tree", default=None)

# #1212: the post-teardown cleanups still running (a strong reference: a caller
# that stopped waiting must not let one be collected mid-kill)
_REAPS: set = set()


def _media_delivery(eng, origin: dict) -> "tuple[str, bool, TgAddress | None]":
    """``(executor, is_task, delivery)`` of a ``send_media`` call. A bound
    engagement is a job: its record's delivery. Otherwise the origin's task
    delivery, stamped by the task's launcher — and a delegated turn that
    carries none is a task with no delivery. A turn of the agent that is
    its own executor, with no task stamp, is no task."""
    from provenance import task_delivery
    if eng is not None:
        return str(getattr(eng, "role_or_type", "") or ""), True, getattr(eng, "delivery", None)
    executor = str(origin.get("execution_role") or origin.get("role") or "")
    is_task, delivery = task_delivery(origin)
    if is_task or executor != str(origin.get("role") or ""):
        return executor, True, delivery
    return executor, False, None


def _media_post(eng, origin: dict, executor: str) -> Any:
    """The post record a ``send_media`` file is filed under: the sending
    agent's, owned by the task the call runs in — its job (the bound
    engagement) or its delegation (the run's ``_delegation_id``) — so a reply
    to the file reaches that task. A turn of no task owns nothing."""
    import result_broker as rb
    from provenance import strict_positive_id
    owner = (str(getattr(eng, "id", "") or "") if eng is not None
             else str(origin.get("_delegation_id") or ""))
    return rb.PostRecord(
        role=executor, operator_id=strict_positive_id(origin.get("user_id")) or 0,
        plugin="", slot="media", tool_use_id=uuid.uuid4().hex, owner=owner,
        posted_at=time.time(), kind=rb.OPERATOR_FILE)


def _closed_own_chat(executor: str, origin: dict) -> Any:
    """The capability rule's refusal when *origin*'s route is an agent's own
    bot and *executor*'s own chat is not open now; ``None`` otherwise (the
    resident's chat, Casa's bot, no Telegram)."""
    resolve = getattr(_transport(), "endpoint_for_route", None)
    if not callable(resolve) or origin.get("channel") != "telegram":
        return None
    try:
        ep = resolve(origin, site="send_media")
    except (RouteMissing, AddressError, EndpointUnavailable, RuntimeError):
        return None
    if getattr(ep, "is_casa", True):
        return None
    import own_voice
    return own_voice.require_open_chat(executor, label=_display_name_for_role(executor))


def _debit_specialist_media_send(eng, origin: dict) -> "dict | None":
    """Synchronous specialist-context debit for one send_media attempt.

    Returns the refusal payload, or None when the call may proceed
    (non-specialist contexts pass untouched). No await between the check
    and the charge — parallel calls cannot overrun the budget (r3 Sol).
    """
    if eng is not None:
        if getattr(eng, "kind", "") != "specialist":
            return None  # executor engagements are image-owned, unmetered
        key = f"eng:{eng.id}"
    elif int((origin or {}).get("delegation_depth", 0) or 0) <= 0:
        # A direct turn: the resident's own is unmetered; a specialist's
        # own conversation turn is that specialist's context, metered per
        # turn (its correlation id), as a delegated turn is per delegation.
        role = str((origin or {}).get("execution_role") or (origin or {}).get("role") or "")
        if getattr(_agent_role_map.get(role), "kind", "") != "specialist":
            return None
        cid = (origin or {}).get("cid")
        if not isinstance(cid, str) or not cid or cid == "-":
            return {
                "status": "error", "kind_error": "quota_key_missing",
                "message": ("specialist conversation turn carries no "
                            "correlation id — media send refused"),
            }
        key = f"turn:{role}:{cid}"
    else:
        # #541 (Terra diff r1): a delegated RESIDENT is not specialist
        # context — its tool grants come from the operator's own image/
        # config role.yaml, not a third-party bundle. Exempt only on the
        # POSITIVE server-stamped kind; absent/unknown stays metered.
        if (origin or {}).get("_delegation_kind") == "resident":
            return None
        raw_key = (origin or {}).get("_delegation_id")
        if not isinstance(raw_key, str) or not raw_key:
            return {
                "status": "error", "kind_error": "quota_key_missing",
                "message": ("delegated specialist context carries no "
                            "delegation id — media send refused (#541)"),
            }
        key = f"dlg:{raw_key}"
    used = _MEDIA_SEND_DEBITS.get(key, 0)
    if used >= _SPECIALIST_MEDIA_SEND_BUDGET:
        return {
            "status": "error", "kind_error": "media_send_budget_exhausted",
            "message": (
                f"media send budget ({_SPECIALIST_MEDIA_SEND_BUDGET}) for "
                "this specialist context is exhausted — reference remaining "
                "artifacts in your completion/result text instead"),
        }
    _MEDIA_SEND_DEBITS[key] = used + 1
    _MEDIA_SEND_DEBITS.move_to_end(key)
    while len(_MEDIA_SEND_DEBITS) > _MEDIA_SEND_DEBITS_MAX_KEYS:
        _MEDIA_SEND_DEBITS.popitem(last=False)
    return None


def _scheduled_operator_target(origin: dict) -> "tuple[int, int] | None":
    """The operator ``(chat_id, user_id)`` a SCHEDULED turn may deliver to (#485).

    A resident's own time-based trigger turn carries a session-keying label in
    ``origin["chat_id"]`` (``f"{type}-{name}"``), not a chat id — the label keys
    the SDK session and the voice-job scope, so it is never overwritten. This
    resolves where such a turn's media should actually go, and returns ``None``
    for everything else. Every arm is fail-closed; ``None`` means the turn stays
    text-only, which is the behaviour before this feature existed.

    Admission requires ALL of:

    * ``_scheduled_delivery`` exactly ``True`` — the reserved marker stamped by
      Casa's two time-based dispatch sites and stripped from every external
      context. Eligibility is never inferred from ``message_type`` because the
      authenticated webhook route dispatches ``MessageType.SCHEDULED`` too;
    * the telegram channel — the only one with an operator DM;
    * genuinely DIRECT execution: no engagement bound, and the executing role
      is the origin's own role. A delegated specialist inherits the whole parent
      origin, marker included, and must not inherit the delivery target with it;
    * a configured operator. ``operator_user_id()`` is the single home of that
      rule and already fails closed (empty, group, non-numeric, non-operator ⇒
      ``None``) — identity v0.136: with no operator configured, NOBODY is the
      operator.

    Resolved HERE, per call, rather than stamped at dispatch: the trigger that
    fired an hour ago must not be able to deliver to an operator id that has
    since changed. For a private Telegram chat the chat id equals the user id,
    so both returned values are that id (the transport's
    ``operator_user_id()``: one operator per deployment, on every bot).

    Deliberately NOT part of ``turn_provenance()``: that predicate is shared
    with the protected-action authorization hook (``authz_grants``), and
    widening it would widen who can raise an approval. Its consumers are named
    one by one: ``send_media`` (#485) and the scheduled arm of ``ask_user``
    (#573) — the second reuses this helper verbatim rather than inventing a
    second eligibility rule that could drift from it.
    """
    from provenance import strict_positive_id

    if origin.get("_scheduled_delivery") is not True:
        return None
    if origin.get("channel") != "telegram":
        return None
    if engagement_var.get(None) is not None:
        return None
    role = origin.get("role")
    if not role or origin.get("execution_role") != role:
        return None
    transport = _transport()
    resolver = getattr(transport, "operator_user_id", None)
    if not callable(resolver):
        return None
    operator_id = strict_positive_id(resolver())
    if operator_id is None:
        return None
    return operator_id, operator_id


@tool(
    "send_media",
    "Deliver a media file from the plugin outbox to the user over the "
    "originating channel. Kinds: document (PDF only), photo, audio, voice, zip, "
    "text (UTF-8 .txt/.md/.csv/.log/.json/.yaml/.yml/.sta/.940, max 5 MB). Pass the "
    "outbox path returned by a producer tool; the bytes never enter the model "
    "context — use this for any artifact you would otherwise paste.",
    {"type": "object",
     "properties": {
         "path": {"type": "string"},
         "kind": {"type": "string", "enum": list(MEDIA_POLICIES)},
         "caption": {"type": "string"},
         "filename": {"type": "string"}},
     "required": ["path", "kind"]},
)
async def send_media(args: dict) -> dict:
    try:
        # 0. Validate arguments up front (the Casa MCP forwarding path does NOT
        #    enforce the JSON schema). `kind` MUST be a str before the dict
        #    membership test — `[] in MEDIA_POLICIES` raises TypeError.
        path = args.get("path")
        kind = args.get("kind")
        caption = args.get("caption")
        filename_arg = args.get("filename")
        if not isinstance(path, str) or not path:
            return _result({"status": "error", "kind_error": "invalid_arguments",
                            "message": "path must be a non-empty string"})
        if not isinstance(kind, str) or kind not in MEDIA_POLICIES:
            return _result({"status": "error", "kind_error": "invalid_arguments",
                            "message": f"unknown kind {kind!r}"})
        if caption is not None and not isinstance(caption, str):
            return _result({"status": "error", "kind_error": "invalid_arguments",
                            "message": "caption must be a string"})
        if filename_arg is not None and not isinstance(filename_arg, str):
            return _result({"status": "error", "kind_error": "invalid_arguments",
                            "message": "filename must be a string"})

        # 1. Resolve target chat — NO default fallback. Active engagement first
        #    (HTTP engagement handlers bind engagement_var, not origin_var); a
        #    delegated specialist turn carries the origin via origin_var.
        eng = engagement_var.get(None)
        # #369: a live status re-check — media produced by a
        # terminal (or torn-down) engagement must not still reach the origin
        # chat through a call that raced the transition.
        if eng is not None and getattr(eng, "status", "") != "active":
            return _result({"status": "error", "kind_error": "not_active",
                            "message": "engagement is not active"})
        origin = dict(eng.origin) if eng is not None else _snapshot_origin()
        if not origin:
            return _result({"status": "error", "kind_error": "no_origin",
                            "message": "no turn origin"})
        # A task's file goes where the task's posts go, decided when it was
        # born — never the route of the turn that started it. A task born
        # with no delivery posts nothing, even if its executor's chat has
        # opened since. A turn that is no task (the resident's own, an
        # agent's own conversation) delivers in its own chat.
        executor, is_task, delivery = _media_delivery(eng, origin)
        if is_task:
            if delivery is None:
                refusal = _surface_refusal(executor)
                return _result({"status": "error", "kind_error": refusal.kind,
                                "message": refusal.text})
            origin.update(channel="telegram", chat_id=delivery.chat_id,
                          _tg_bot=delivery.bot_id)
        # The capability rule, as at every surface: a file bound for an
        # agent's own chat posts only while that chat is open — a task born
        # there before the chat closed refuses too.
        closed = _closed_own_chat(executor, origin)
        if closed is not None:
            return _result({"status": "error", "kind_error": closed.kind,
                            "message": closed.text})
        if origin.get("channel") != "telegram":
            return _result({"status": "error", "kind_error": "invalid_origin",
                            "message": f"origin channel {origin.get('channel')!r} "
                                       "cannot receive media"})
        raw_chat = origin.get("chat_id")
        if isinstance(raw_chat, bool):
            chat_id = None
        elif isinstance(raw_chat, int):
            chat_id = raw_chat
        elif isinstance(raw_chat, str):
            try:
                chat_id = int(raw_chat)
            except ValueError:
                chat_id = None
        else:
            chat_id = None
        if not chat_id:
            # #485: a resident's own scheduled telegram trigger carries a
            # session LABEL here, not a chat id. Resolve the operator's DM
            # instead of refusing — see _scheduled_operator_target for the
            # (fail-closed) admission rules. Everything else still refuses.
            _scheduled = _scheduled_operator_target(origin)
            if _scheduled is not None:
                chat_id = _scheduled[0]
        if not chat_id:
            return _result({"status": "error", "kind_error": "invalid_origin",
                            "message": "no numeric, nonzero chat_id in origin"})
        origin["chat_id"] = chat_id  # normalise to the validated int for dispatch

        # #541: specialist-context outbound quota — synchronous debit AFTER
        # target validation (bogus arguments never burn budget) and BEFORE
        # any claim/delivery await. Attempts count; no refunds.
        _quota_refusal = _debit_specialist_media_send(eng, origin)
        if _quota_refusal is not None:
            return _result(_quota_refusal)

        # 2. Resolve channel (fail fast, before claiming).
        if _channel_manager is None:
            return _result({"status": "error", "kind_error": "channel_unavailable",
                            "message": "tools not initialised"})
        # The origin is Telegram's (checked above): the transport sends the
        # file through the bot of the origin's route.
        ch = _transport()
        if ch is None:
            return _result({"status": "error", "kind_error": "channel_unavailable",
                            "message": "channel not registered"})
        if caption is not None:
            # #1038: the caption is model text — admitted under this emission's
            # scope (engagement or turn), the line at its head. The existing
            # cap falls on the model's BODY, never on the line: a line cut by
            # the cap under a result that reports it whole is a false record
            # (plan round 2, Astra). A slice of an Admitted is a plain str, so
            # the shortened caption is re-wrapped under the same admission.
            _scope = _current_scope(origin)
            if _scope is None:
                return _result({"status": "error", "kind_error": "unsupported_origin",
                                "message": _NO_SCOPE_MESSAGE})
            body = caption
            caption = _scope.admit(IntentKind.CAPTION, body)
            if len(caption) > _CAPTION_MAX:
                head = str(caption)[:len(caption) - len(body)]
                caption = caption.with_text(head + body[:max(_CAPTION_MAX - len(head), 0)])
            _commitment = caption.send
        else:
            # #1079: a file the model chose is a commitment to the operator
            # even with no text to admit — recorded the same way, on the
            # scope this emission commits under.
            _scope = _current_scope(origin)
            _commitment = (_scope.open_send("media")
                           if _scope is not None else None)

        # Containment stage 2 (Task 11): which outbox to claim from is
        # derived from the AUTHENTICATED engagement record (bound via
        # `engagement_var`, never from the caller-submitted `path`) — see
        # outbox_for_current_context, which the result broker's operator_file
        # delivery (S3) shares so the two claims cannot drift.
        outbox = outbox_for_current_context()
        if outbox is None:
            return _result({"status": "error", "kind_error": "internal_error",
                            "message": "outbox not initialised"})

        # 3. Claim FIRST — the path guard (outside_outbox / missing / bad
        #    basename) runs BEFORE filename validation, so an out-of-outbox path
        #    reports `outside_outbox`, not `bad_name`. Pre-claim errors own
        #    nothing and return directly.
        try:
            claim = await asyncio.to_thread(outbox.claim, path)
        except plugin_outbox.OutboxError as exc:
            return _result({"status": "error", "kind_error": exc.kind,
                            "message": str(exc)})

        # 4. Claim is OWNED — remove it on EVERY outcome (including bad_name and
        #    every guard/capture/send error).
        cleanup_warning = None
        try:
            try:
                # Delivered filename: an explicit arg (incl. "" -> bad_name) else
                # the path basename; validated against the kind's extensions.
                candidate = (filename_arg if filename_arg is not None
                             else os.path.basename(path))
                filename = _validate_delivery_filename(candidate, kind)
                if filename is None:
                    payload = {"status": "error", "kind_error": "bad_name",
                               "kind": kind,
                               "message": "filename not valid for the media kind"}
                else:
                    content = await asyncio.to_thread(outbox.capture, claim, kind)
                    payload = await _classify_send(
                        ch, content, kind, filename, origin, caption,
                        post=_media_post(eng, origin, executor))
                    # #1079: confirmed at the channel's normal return (its
                    # failure contract is to raise) — before claim cleanup,
                    # whose failure says nothing about what was delivered.
                    if payload.get("status") == "ok" and _commitment is not None:
                        _commitment.delivered = True
            except plugin_outbox.OutboxError as exc:
                payload = {"status": "error", "kind_error": exc.kind,
                           "kind": kind, "message": str(exc)}
        finally:
            try:
                await asyncio.to_thread(outbox.remove_claim, claim)
            except Exception as ce:  # noqa: BLE001 — cleanup best-effort
                cleanup_warning = f"claim cleanup failed: {type(ce).__name__}"
                logger.warning("send_media claim cleanup failed for %s: %s",
                               claim, ce)
        if cleanup_warning and payload.get("status") == "ok":
            payload["cleanup_warning"] = cleanup_warning
        return _result(payload)
    except Exception:  # noqa: BLE001 — nothing escapes; worst case is a result
        logger.exception("send_media: unexpected failure")
        return _result({"status": "error", "kind_error": "internal_error",
                        "message": "unexpected failure"})


# ---------------------------------------------------------------------------
# ask_user — resident DM button questions (v0.76.0 W5b, A:§2)
#
# Two-turn, detached: REGISTER -> POST -> RETURN. The tool never awaits the
# operator's tap — it registers a `resident_ask` broker request scoped
# `dm:<bot_id>:<chat_id>`, posts the inline keyboard, and returns `awaiting_user`
# immediately. The operator's tap (or a same-DM typed reply, or /new, or a
# timeout, or Casa shutdown) resolves the request later; the broker finish
# hook installed here is the SINGLE owner of the post-commit keyboard edit +
# synthetic-turn dispatch (r2-B1/r3-B1 single-owner contract).
# ---------------------------------------------------------------------------

# W5 ask contract limits (design §W5, shared by ask_user — A:§2).
_ASK_QUESTION_MAX = 1024
_ASK_OPTIONS_MIN = 2
_ASK_OPTIONS_MAX = 8
# #1390: the FULL option (listed in the message, never on a button) — room
# for a sentence; the button gets its ``short``.
_ASK_OPTION_LABEL_MAX = 100
_ASK_TIMEOUT_DEFAULT = 300.0
_ASK_TIMEOUT_MIN = 30.0
_ASK_TIMEOUT_MAX = 570.0

# #682 (INV-TOOL-008). A two-turn arm's LAST synchronous act is to ask the
# broker whether its request is still live. `message_id` cannot answer that:
# `_run_setup` writes it AFTER the post await, onto the same `PendingRequest`
# object the tool holds, and `_finish` retires a request by popping it from
# `_live` and snapshotting a COPY of `meta` — so the marker keeps arriving for
# a request that is already gone.
#
# Absence from `_live` is all the read supports, and it is consistent with
# cancelled, superseded, timed out AND answered. So the status is `settled`,
# which names no outcome, and the message says so rather than claiming that
# nothing happened: an Approve tapped inside the post window may already have
# recorded the wipe and asked for the restart.
#
# `settled` is deliberately NOT an error kind. Under INV-TOOL-001 `_result`
# marks `status == "error"` as an outer MCP error, and every way a request is
# retired here — a `/new`, a typed answer, a replacement question, the
# request's own timeout, an operator tap, shutdown — is a benign operator or
# system action that telemetry must not record as a tool failure.
_SETTLED_ASK_MESSAGE = (
    "the question settled before ask_user returned; no outcome is reported"
)
_SETTLED_WIPE_MESSAGE = (
    "the consent request settled before wipe_memory returned; "
    "no outcome is reported"
)

ASK_USER_SCHEMA = {
    # r1-B3: explicit JSON Schema — only question+options are required;
    # timeout_s is optional (defaults to _ASK_TIMEOUT_DEFAULT, clamped).
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        # #1390: an option is a plain string, or a full label plus the few
        # words its button shows (the engagement ``ask`` tool's ``short``).
        "options": {"type": "array", "items": {"anyOf": [
            {"type": "string"},
            {"type": "object",
             "properties": {
                 "label": {"type": "string", "description":
                           "the full option, listed under the question and "
                           "returned when tapped"},
                 "short": {"type": "string", "description":
                           "a few words for its button"},
             },
             "required": ["label"]},
        ]}},
        "timeout_s": {"type": "number"},
    },
    "required": ["question", "options"],
}


def _ask_user_validate(
    args: dict,
) -> tuple[str | None, list[str] | None, list | None, float | None, str | None]:
    """Validate ask_user args against the W5 ask contract.

    Returns ``(question, options, shorts, timeout_s, error_message)``. On
    success ``error_message`` is ``None`` and the other four are populated
    (``timeout_s`` already clamped to ``[_ASK_TIMEOUT_MIN,
    _ASK_TIMEOUT_MAX]``). On failure only ``error_message`` is set.

    #1390: an option is a plain string or ``{"label": str, "short": str}``.
    ``options`` is always the FULL labels (what the body lists, the settle
    shows and the answer carries); ``shorts`` is the parallel button words,
    ``None`` where an option has none. A non-string ``short`` counts as
    absent; a short that does not fit is the button resolver's concern, as
    for the engagement ``ask``. An option wider than its button WITHOUT a
    short is refused, with the fix in the message.
    """
    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        return None, None, None, None, "question must be a non-empty string"
    if len(question) > _ASK_QUESTION_MAX:
        return None, None, None, None, (
            f"question must be at most {_ASK_QUESTION_MAX} characters"
        )

    from channels.telegram import _ASK_BUTTON_WORDS_FIT

    raw = args.get("options")
    if not isinstance(raw, list):
        return None, None, None, None, "options must be a list"
    if not (_ASK_OPTIONS_MIN <= len(raw) <= _ASK_OPTIONS_MAX):
        return None, None, None, None, (
            f"options must have between {_ASK_OPTIONS_MIN} and "
            f"{_ASK_OPTIONS_MAX} entries"
        )
    options: list[str] = []
    shorts: list = []
    for opt in raw:
        short = None
        if isinstance(opt, dict):
            short = opt.get("short")
            short = short if isinstance(short, str) else None
            opt = opt.get("label")
        if not isinstance(opt, str) or not opt.strip():
            return None, None, None, None, (
                "every option must be a non-empty string, or an object with "
                "a non-empty label"
            )
        if len(opt) > _ASK_OPTION_LABEL_MAX:
            return None, None, None, None, (
                f"option labels must be at most {_ASK_OPTION_LABEL_MAX} "
                "characters"
            )
        if short is None and len(opt) > _ASK_BUTTON_WORDS_FIT:
            # #1390: a plain option too long for its button would turn every
            # button into "Option n". Saying how to fix it gets the buttons
            # words, where the description alone did not hold in a long
            # session.
            return None, None, None, None, (
                f"an option longer than {_ASK_BUTTON_WORDS_FIT} characters "
                "does not fit its button: give it as {\"label\": <the full "
                "option>, \"short\": <a few words for its button>}"
            )
        options.append(opt)
        shorts.append(short)
    if len(set(options)) != len(options):
        return None, None, None, None, "options must be unique"

    timeout_arg = args.get("timeout_s")
    if timeout_arg is None:
        timeout_s = _ASK_TIMEOUT_DEFAULT
    else:
        if isinstance(timeout_arg, bool) or not isinstance(timeout_arg, (int, float)):
            return None, None, None, None, "timeout_s must be a number"
        timeout_s = float(timeout_arg)
    timeout_s = max(_ASK_TIMEOUT_MIN, min(_ASK_TIMEOUT_MAX, timeout_s))

    return question, options, shorts, timeout_s, None


async def _ask_user_scheduled(
    *, channel, origin: dict, rid: str, scope: str, body: str,
    options: list, chat_id: int, operator_id: int, target_role,
    timeout_s: float, shorts: "list | None" = None, bot_id: int,
) -> dict:
    """The scheduled arm of ``ask_user`` (#573).

    Differs from the operator-DM arm in four ways, each of them the reason
    this needed its own issue:

    * it never SUPERSEDES. A machine-timed question must not displace a live
      human one, so it registers with ``require_idle`` across BOTH halves of
      the operator's attention lane (``dm:`` plain asks and ``authz:``
      protected-action challenges) and refuses with ``operator_busy``;
    * it writes a DURABLE record before the keyboard is posted, so a restart
      neither loses the request nor leaves a tappable keyboard with nothing
      behind it;
    * its finish hook dispatches on EVERY terminal outcome, not just an
      answer, back into the scheduled session — an unanswered question that
      told nobody would leave that session waiting forever;
    * the continuation is a machine-authored SCHEDULED turn, so the operator's
      tap never relabels the session's authorship.
    """
    import time as _time

    import scheduled_asks
    from verdict_broker import BROKER

    req, _created = BROKER.register(
        namespace="resident_ask", scope=scope, request_id=rid,
        timeout_s=timeout_s, detached=True, supersede=False,
        require_idle=True, idle_scopes=(tg_scope("authz", bot_id, chat_id),),
        meta=scheduled_asks.broker_meta({
            "rid": rid, "scope": scope, "bot_id": bot_id, "chat_id": chat_id,
            "operator_id": operator_id, "role": target_role,
            "session_scope": str(origin.get("chat_id") or ""),
            "options": list(options),
            "epoch": origin.get("_scheduled_epoch"),
        }),
    )
    if req is None:
        return _result({
            "status": "error", "kind": "operator_busy",
            "message": (
                "the operator already has a pending question; this one was "
                "not asked — try again on the next run"
            ),
        })

    record = {
        "rid": rid,
        "state": scheduled_asks.STATE_POSTING,
        "role": target_role,
        "session_scope": str(origin.get("chat_id") or ""),
        "scope": scope,
        # The bot that posts the keyboard: written with the record, before
        # the post, so a restart edits the keyboard through the same bot.
        "bot_id": bot_id,
        "chat_id": chat_id,
        "operator_id": operator_id,
        "message_id": None,
        "options": list(options),
        "body": body,
        "epoch": origin.get("_scheduled_epoch"),
        "created_at": _time.time(),
        "expires_at": _time.time() + timeout_s,
    }
    store = scheduled_asks.STORE
    if store is not None:
        await store.put(record)

    async def _post():
        mid = await channel.post_dm_keyboard(
            chat_id=chat_id, request_id=rid, text=body, options=list(options),
            short_labels=True, shorts=shorts,
        )
        if isinstance(mid, int):        # the broker's own "posted" test
            body.mark_delivered()       # #1079
        return mid

    def _finish_factory(message_id: int):
        return scheduled_asks.make_finish_hook(
            channel, {**record, "message_id": message_id},
        )

    await BROKER.ensure_posted(req, _post, _finish_factory)

    message_id = req.meta.get("message_id")
    if message_id is None:
        # _run_setup already unregistered the request (post returned None or
        # raised), and `unregister` fires no hook — so this is the one path
        # that owns its own record cleanup.
        if store is not None:
            await store.drop(rid)
        return _result({
            "status": "error", "kind": "delivery_failed",
            "message": "could not deliver the question to the operator",
        })
    # Compare-and-set from `posting`: the request can terminalize DURING its
    # own post (an immediate cancel, a zero-ish timeout), in which case the
    # hook has already settled and dropped the record and this write must not
    # resurrect it.
    if store is not None:
        await store.set_state(
            rid, scheduled_asks.STATE_LIVE, message_id=message_id,
        )
    return _result({"status": "awaiting_user", "request_id": rid,
                    "delivered_to": "operator_dm",
                    # #1038 §6.5: the line the keyboard carried, if any.
                    **({"casa_prefixed": list(body.annotations)}
                       if getattr(body, "annotations", ()) else {})})


@tool(
    "ask_user",
    "Ask the operator a multiple-choice question with tappable buttons in "
    "their DM. An option of a few words (32 characters or fewer) can be a "
    "plain string: its button shows it as written. Give a longer option as "
    "{\"label\": <the full option>, \"short\": <a few words for its "
    "button>}. Casa shows the options itself, so give any other detail "
    "in the question as plain sentences, never as a list of the options. "
    "Two-turn: returns awaiting_user immediately; the answer "
    "arrives as the user's next message. A `settled` status instead means the "
    "question was already over by the time this call returned (a /new, a "
    "typed answer, a replacement question, a timeout, a tap, a shutdown): do "
    "NOT wait on that request id and do not re-ask automatically; if the "
    "operator did answer, it reaches you as an ordinary next message. Works "
    "from one of your own scheduled "
    "trigger turns too — there the answer (or a timeout, or a cancellation) "
    "comes back into that same scheduled session, and if the operator already "
    "has a question open you get operator_busy instead. NOT an authorization "
    "mechanism: an "
    "Approve tapped here commits NO consent, however the question is "
    "worded — to re-surface a plugin-consent DM use consent_reprompt.",
    ASK_USER_SCHEMA,
)
async def ask_user(args: dict) -> dict:
    question, options, shorts, timeout_s, err = _ask_user_validate(args)
    if err is not None:
        return _result({"status": "error", "kind": "invalid_arguments",
                        "message": err})

    from provenance import strict_positive_id, turn_provenance

    origin = _snapshot_origin()
    prov = turn_provenance()
    # #573: a resident's own scheduled telegram turn may ask too. Its origin
    # carries a session LABEL where a chat id would be, so it fails the DM gate
    # by construction; admission comes from the SAME fail-closed #485 helper
    # `send_media` uses (Casa-stamped marker + telegram + no engagement +
    # execution_role == role + a configured operator), never from an inference.
    _scheduled_target = (
        _scheduled_operator_target(origin)
        if (prov.transport not in ("dm", "button") or prov.execution != "direct")
        else None
    )
    scheduled = _scheduled_target is not None
    if not scheduled and (
        prov.transport not in ("dm", "button") or prov.execution != "direct"
    ):
        return _result({
            "status": "error", "kind": "unsupported_origin",
            "message": (
                "ask_user requires a direct, genuine inbound-DM turn "
                f"(got transport={prov.transport!r} execution={prov.execution!r})"
            ),
        })

    if scheduled:
        # The trigger that fired this turn may have been deleted or rewritten
        # while the turn was already running. Raising its question anyway would
        # put a keyboard in the operator's DM on behalf of a schedule that no
        # longer exists.
        import scheduled_asks

        # The label is this turn's own session key — the trigger it came from.
        # Comparing per (role, label) is what keeps a cancelled reminder from
        # silencing the role's other schedules mid-turn.
        if not scheduled_asks.epoch_is_current(
            origin.get("role"), str(origin.get("chat_id") or ""),
            origin.get("_scheduled_epoch"),
        ):
            return _result({
                "status": "error", "kind": "trigger_changed",
                "message": (
                    "the trigger that started this turn has been changed or "
                    "removed; the question was not asked"
                ),
            })
        chat_id, operator_id = _scheduled_target
    else:
        chat_id = strict_positive_id(origin.get("chat_id"))
        operator_id = strict_positive_id(origin.get("user_id"))
    if chat_id is None or operator_id is None:
        return _result({
            "status": "error", "kind": "unsupported_origin",
            "message": "ask_user requires a valid chat_id/user_id origin",
        })

    if _channel_manager is None:
        return _result({"status": "error", "kind": "delivery_failed",
                        "message": "tools not initialized"})
    transport = _transport()
    if transport is None:
        return _result({"status": "error", "kind": "delivery_failed",
                        "message": "channel not available"})
    # The question is posted by the bot of the turn's route, and its lane is
    # keyed by that bot before anything is registered or stored: the bot's id
    # is read from the endpoint that will post, never from the route itself.
    try:
        channel = transport.endpoint_for_route(origin, site="ask_user")
        bot_id = channel.bot_id
    except EndpointNotReady:
        # That bot has not connected yet: the question cannot be posted, as
        # when its post fails, and nothing is registered or stored.
        return _result({
            "status": "error", "kind": "delivery_failed",
            "message": "could not deliver the question to the operator",
        })
    except (RouteMissing, AddressError, EndpointUnavailable) as exc:
        logger.error("ask_user: no Telegram endpoint for the turn: %s", exc)
        return _result({"status": "error", "kind": "delivery_failed",
                        "message": "channel not available"})

    from verdict_broker import BROKER
    from channels.telegram import render_ask_body, short_option_labels

    rid = uuid.uuid4().hex
    target_role = origin.get("role")
    scope = tg_scope("dm", bot_id, chat_id)
    # v0.81.0 (W-R3b): the DM MESSAGE body carries the FULL options VERBATIM +
    # 1-based numbered (the SAME single-source renderer the engagement asks use),
    # while the buttons carry only short number-prefixed labels. Both the post
    # and the settle edits derive from this one ``body`` so they can never
    # disagree (mirrors the engagement single-source discipline).
    # #1392: the buttons' captions go in too — when they are the options' own
    # words, the body leaves out the list that would only repeat them.
    # #1390: an option's ``short`` (if any) is only ever its button's words.
    body = render_ask_body(
        None, question, list(options),
        short_option_labels(list(options), shorts))
    # #1038: the question body is model text — admitted under the turn's scope
    # BEFORE the two arms branch, so the posted keyboard, the stored scheduled
    # record and every settle edit derived from ``body`` carry the same line.
    _scope = _current_scope(origin)
    if _scope is None:
        return _result({"status": "error", "kind": "unsupported_origin",
                        "message": _NO_SCOPE_MESSAGE})
    body = _scope.admit(IntentKind.KEYBOARD, body)

    if scheduled:
        return await _ask_user_scheduled(
            channel=channel, origin=origin, rid=rid, scope=scope, body=body,
            options=list(options), chat_id=chat_id, operator_id=operator_id,
            bot_id=bot_id,
            target_role=target_role, timeout_s=timeout_s, shorts=shorts,
        )

    # #648: `supersede=True` retired EVERY live request in this scope at
    # REGISTRATION — including a scheduled question, and before this one was
    # known to have reached the screen. The two effects are split. Against
    # another HUMAN ask the behaviour is unchanged: retire it here, in the same
    # no-await block as the register, so the replacement step stays indivisible
    # against the tap handler and a typed-reply cancel. Against a SCHEDULED
    # question, displacement waits for delivery (below).
    #
    # Static meta BEFORE post (register() shallow-copies whatever dict we
    # pass, so the complete dict is supplied up front rather than mutated
    # after the fact). No `on_commit_sync` — plain asks record nothing at
    # commit time (that's the authz challenge's job, Task 5+).
    import scheduled_asks

    scheduled_asks.cancel_non_scheduled_for_chat(bot_id, chat_id, "superseded")
    req, _created = BROKER.register(
        namespace="resident_ask", scope=scope, request_id=rid,
        timeout_s=timeout_s, detached=True, supersede=False,
        meta={
            "options": list(options),
            "bot_id": bot_id,
            "chat_id": chat_id,
            "operator_id": operator_id,
            "target_role": target_role,
            "kind": "ask",
            "_scope": scope,
        },
    )

    async def _post():
        mid = await channel.post_dm_keyboard(
            chat_id=chat_id, request_id=rid, text=body, options=list(options),
            short_labels=True, shorts=shorts,
        )
        if isinstance(mid, int):        # the broker's own "posted" test
            body.mark_delivered()       # #1079
        return mid

    def _finish_factory(message_id: int):
        async def _finish(outcome: dict) -> None:
            # r2-B1/r3-B1 single-owner finish hook: on `answered` edit the
            # keyboard to the answered label FIRST, then dispatch the
            # synthetic continuation, then overwrite with a visible failure
            # text ONLY if dispatch failed. Anything else is a retirement, and
            # this edit is the ONLY thing the operator is ever told about it —
            # unlike the scheduled ask, nothing here dispatches a continuation
            # carrying the reason (#933).
            if outcome.get("outcome") != "answered":
                import ask_retirement

                await channel.edit_dm_message(
                    chat_id, message_id,
                    ask_retirement.retired_body(
                        body, outcome.get("outcome"), outcome.get("reason")),
                )
                return
            idx = outcome["option_index"]
            chosen = options[idx]
            await channel.edit_dm_message(
                chat_id, message_id, f"{body}\n\nAnswered: {chosen}",
            )
            ok = await channel._dispatch_button_continuation(
                chat_id=chat_id, user_id=operator_id, target_role=target_role,
                request_id=rid, text=f"[button answer to {rid}]: {chosen}",
            )
            if not ok:
                await channel.edit_dm_message(
                    chat_id, message_id,
                    "answer received but delivery failed — please type it",
                )
        return _finish

    await BROKER.ensure_posted(req, _post, _finish_factory)

    if "message_id" not in req.meta:
        # _run_setup already unregistered the request on a post failure
        # (post() returned None or raised) — no dangling pending record.
        return _result({
            "status": "error", "kind": "delivery_failed",
            "message": "could not deliver the question to the operator",
        })
    # #648: a human question takes the lane from a machine-timed one only once
    # it is BOTH delivered and still live. `message_id` alone does not prove
    # that — `_run_setup` records it even for a request cancelled or answered
    # while its post was in flight — so liveness is asked of the broker's live
    # map, synchronously, immediately before the displacement.
    #
    # #682: ONE sample, TWO consumers. The same boolean decides the
    # displacement and the status reported to the agent, so they can never
    # disagree — before this the guard knew the request was gone and the return
    # value still said otherwise. The sample is synchronous, so this block
    # gains no await and stays indivisible against the tap handler.
    live = rid in BROKER.pending(namespace="resident_ask", scope=scope)
    if not live:
        return _result({"status": "settled", "request_id": rid,
                        "message": _SETTLED_ASK_MESSAGE})
    scheduled_asks.displace_scheduled_for_chat(bot_id, chat_id, "superseded")
    return _result({"status": "awaiting_user", "request_id": rid,
                    "note": ASK_USER_SILENCE_NOTE,
                    **({"casa_prefixed": list(body.annotations)}
                       if body.annotations else {})})


# ---------------------------------------------------------------------------
# Memory tools: search_memory, browse_memory_day, open_memory, correct_memory
# ---------------------------------------------------------------------------

#: INV-MEM-010: on every search and browse result, empty or not, and the same
#: at every clearance — a note that varied would itself say something was
#: filtered out of this answer.
MEMORY_READ_NOTE = (
    "This is the saved conversation readable here, and the search or day is "
    "bounded. Finding nothing, or nothing on the topic, is not proof it was "
    "never said: say you found nothing you can share, never that it didn't "
    "happen."
)
#: #1116: says WHEN to tell, so an outage on a check nobody asked for (a
#: scheduled briefing) does not become a message.
_MEMORY_UNAVAILABLE = (
    "Memory could not be checked right now. Do NOT say the information "
    "doesn't exist or that you don't have it. If a person asked for something "
    "that needs memory, tell them memory couldn't be checked. On a check "
    "nobody asked for, this alone is not a reason to send anything; still "
    "report what the task itself requires."
)
_DAY_FORMS = (
    "a day YYYY-MM-DD, or today, yesterday, this_week, last_week, "
    "this_month, last_month (in the operator's timezone)")
_OPTIONAL_DAY = {"type": ["string", "null"]}
_OPTIONAL_PAGE = {"type": ["integer", "null"],
                  "description": "Optional page, from 1."}

# Pre-built schemas: the SDK makes every dict-of-types key required, and
# ``null`` (what a client sends for an unset optional) means not given.
SEARCH_MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string",
                  "description": "Words to look for in saved conversation."},
        "from": {**_OPTIONAL_DAY, "description": (
            "Optional first day to search: " + _DAY_FORMS
            + "; a named period means its first day.")},
        "to": {**_OPTIONAL_DAY, "description": (
            "Optional last day to search, inclusive: " + _DAY_FORMS
            + "; a named period means its last day.")},
        "page": _OPTIONAL_PAGE,
    },
    "required": ["query"],
}
BROWSE_MEMORY_DAY_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string", "description": (
            "The day to read: " + _DAY_FORMS + "; a named period reads "
            "all of it, up to 7 days.")},
        "to_date": {**_OPTIONAL_DAY, "description": (
            "Optional last day, inclusive, at most 7 days in all.")},
        "from_time": {**_OPTIONAL_DAY, "description": (
            "Optional HH:MM on the first day to start from.")},
        "to_time": {**_OPTIONAL_DAY, "description": (
            "Optional HH:MM on the last day to stop at.")},
        "page": _OPTIONAL_PAGE,
    },
    "required": ["date"],
}
OPEN_MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "ids": {"type": "array", "items": {"type": "string"},
                "description": "Ids from your memory's index lines."},
        "with_history": {"type": ["boolean", "null"], "description": (
            "Optional: also return each one's earlier versions.")},
    },
    "required": ["ids"],
}


def _memory_reader(origin: dict) -> str:
    """The clearance a memory read runs at: the same gate the turn path
    uses. A bound engagement's own record is the authority, read live (its
    markers and its channel): an in_casa engagement's call inherits the
    launching turn's ambient origin, but the clamp lowers only the record
    (#336, #369). Otherwise the origin's markers."""
    from sensitivity import clearance_for_origin
    eng = engagement_var.get(None)
    eng_origin = getattr(eng, "origin", None) if eng is not None else None
    if isinstance(eng_origin, dict):
        origin = eng_origin
    route, stamped = _origin_clearance_markers(origin)
    return clearance_for_origin(route, stamped, origin.get("channel", "telegram"))


async def _read_memory(origin: dict, read: Callable[[str], Any]) -> Any:
    """Run the blocking *read* at the reader's clearance, off the event loop.
    A clearance lowered while it ran (an engagement clamp, #369) reads again
    at the new one: the clamp is one-way over a few tiers."""
    from sensitivity import TIERS
    clearance = _memory_reader(origin)
    for _ in range(len(TIERS)):
        out = await asyncio.to_thread(read, clearance)
        now = _memory_reader(origin)
        if now == clearance:
            return out
        clearance = now
    return await asyncio.to_thread(read, clearance)


def _memory_store():
    from memory import capture
    store = capture.active_store()
    return store if store is not None and store.available else None


def _days(args: dict, first_key: str, last_key: str):
    """``(first, last, error)``: the local days *args* names, each ``None``
    when not given. A named period is its first day as a first bound and
    its last day as a last one."""
    from datetime import datetime

    from timekeeping import resolve_period, resolve_tz
    now = datetime.now(resolve_tz())
    out = []
    for key, end in ((first_key, False), (last_key, True)):
        spec = args.get(key)
        if spec is None or (isinstance(spec, str) and not spec.strip()):
            out.append(None)
            continue
        try:
            window = resolve_period(spec, now)
        except ValueError:
            return None, None, f"{key} must be {_DAY_FORMS}."
        out.append((window.end if end else window.start).date())
    first, last = out
    if first is not None and last is not None and last < first:
        return None, None, f"{last_key} is before {first_key}."
    return first, last, None


def _page(args: dict) -> int:
    try:
        return max(1, int(args.get("page") or 1))
    except (TypeError, ValueError):
        return 1


@tool(
    "search_memory",
    "Search the saved conversation (what he and Casa said, verbatim) for "
    "words, best match first. Optional from/to limit it to days. Each hit "
    "has its date, weekday, time, speaker and text, with the lines around "
    "it. To read a whole day in order use browse_memory_day; to open an "
    "entry of your memory by id use open_memory.",
    SEARCH_MEMORY_SCHEMA,
)
async def search_memory(args: dict) -> dict:
    from memory import search as memory_search

    query = args.get("query") or ""
    if not memory_search.terms(query):
        return _result({"status": "error", "kind": "empty_query",
                        "message": "search_memory needs words to look for. "
                                   "Nothing was searched."})
    first, last, refused = _days(args, "from", "to")
    if refused:
        return _result({"status": "error", "kind": "invalid_days",
                        "message": refused + " Nothing was searched."})
    page = _page(args)
    origin = _snapshot_origin()
    store = _memory_store()
    if store is None:
        return _result({"status": "unavailable",
                        "message": _MEMORY_UNAVAILABLE})
    reader_role = origin.get("role")
    try:
        found = await _read_memory(origin, lambda clearance: (
            memory_search.search(
                store, query, clearance=clearance, first_day=first,
                last_day=last, page=page, reader_role=reader_role)))
    except Exception:  # noqa: BLE001 — never a fake empty result
        logger.warning("search_memory: memory could not be read",
                       exc_info=True)
        return _result({"status": "unavailable",
                        "message": _MEMORY_UNAVAILABLE})
    out: dict = {"status": "ok", "page": page, "hits": found.hits}
    if first is not None or last is not None:
        out["days"] = {"from": first and first.isoformat(),
                       "to": last and last.isoformat()}
    if found.more:
        out["more"] = f"More hits on page {page + 1}."
    out["note"] = MEMORY_READ_NOTE
    return _result(out)


@tool(
    "browse_memory_day",
    "Read the saved conversation of one day, or up to 7 days, in time "
    "order, optionally from a time on the first day to a time on the last. "
    "Long stretches come in pages. To find something by its words use "
    "search_memory.",
    BROWSE_MEMORY_DAY_SCHEMA,
)
async def browse_memory_day(args: dict) -> dict:
    from datetime import datetime

    from memory import browse as memory_browse

    first, last, refused = _days(args, "date", "date")
    if refused or first is None:
        return _result({"status": "error", "kind": "invalid_days",
                        "message": (refused or f"date must be {_DAY_FORMS}.")
                        + " Nothing was read."})
    if args.get("to_date") not in (None, ""):
        _, last, refused = _days({"to": args.get("to_date")}, "from", "to")
        if refused or last < first:
            return _result({"status": "error", "kind": "invalid_days",
                            "message": (refused or "to_date is before date.")
                            + " Nothing was read."})
    if (last - first).days + 1 > memory_browse.MAX_DAYS:
        return _result({"status": "error", "kind": "too_many_days",
                        "message": (
                            f"browse_memory_day reads at most "
                            f"{memory_browse.MAX_DAYS} days; use search_memory "
                            "with from and to for a longer stretch. Nothing "
                            "was read.")})
    times = []
    for key in ("from_time", "to_time"):
        spec = args.get(key)
        if spec in (None, ""):
            times.append(None)
            continue
        try:
            times.append(datetime.strptime(str(spec).strip(), "%H:%M").time())
        except ValueError:
            return _result({"status": "error", "kind": "invalid_time",
                            "message": f"{key} must be HH:MM. Nothing was "
                                       "read."})
    page = _page(args)
    origin = _snapshot_origin()
    store = _memory_store()
    if store is None:
        return _result({"status": "unavailable",
                        "message": _MEMORY_UNAVAILABLE})
    reader_role = origin.get("role")
    try:
        got = await _read_memory(origin, lambda clearance: (
            memory_browse.browse(
                store, clearance=clearance, first_day=first, last_day=last,
                from_time=times[0], to_time=times[1], page=page,
                reader_role=reader_role)))
    except Exception:  # noqa: BLE001 — never a fake empty result
        logger.warning("browse_memory_day: memory could not be read",
                       exc_info=True)
        return _result({"status": "unavailable",
                        "message": _MEMORY_UNAVAILABLE})
    out: dict = {"status": "ok",
                 "days": {"from": first.isoformat(), "to": last.isoformat()},
                 "page": got.page, "pages": got.pages, "text": got.text}
    if got.total:
        out["events"] = f"{got.first}-{got.last} of {got.total}"
    out["note"] = MEMORY_READ_NOTE
    return _result(out)


@tool(
    "open_memory",
    "Open entries of your memory by the ids its index lines show: an entry "
    "comes back in full, a group or topic as the lines of what it holds. "
    "One that changed after your memory above was written is marked so. "
    "with_history adds earlier versions. To search saved conversation use "
    "search_memory.",
    OPEN_MEMORY_SCHEMA,
)
async def open_memory(args: dict) -> dict:
    from memory import view as memory_view

    ids = args.get("ids")
    if isinstance(ids, str):
        ids = [ids]
    if (not isinstance(ids, list) or not ids
            or not all(isinstance(i, str) and i.strip() for i in ids)):
        return _result({"status": "error", "kind": "no_ids",
                        "message": "open_memory needs the ids from your "
                                   "memory's index lines. To find something "
                                   "by its words use search_memory."})
    if len(ids) > memory_view.MAX_OPEN:
        return _result({"status": "error", "kind": "too_many_ids",
                        "message": f"open_memory opens at most "
                                   f"{memory_view.MAX_OPEN} ids per call."})
    origin = _snapshot_origin()
    store = _memory_store()
    if store is None:
        return _result({"status": "unavailable",
                        "message": _MEMORY_UNAVAILABLE})
    session_key = sdk_session_id = None
    if engagement_var.get(None) is None and origin.get("channel") \
            and origin.get("role"):
        import agent as agent_mod
        from session_registry import build_scoped_session_key
        session_key = build_scoped_session_key(
            origin["channel"], origin["role"], origin.get("chat_id"))
        registry = getattr(agent_mod, "active_session_registry", None)
        entry = (registry.get(session_key) if registry is not None
                 else None) or {}
        sdk_session_id = entry.get("sdk_session_id")
    try:
        opened = await _read_memory(origin, lambda clearance: (
            memory_view.open_objects(
                store, [i.strip() for i in ids], clearance=clearance,
                session_key=session_key, sdk_session_id=sdk_session_id,
                with_history=args.get("with_history") is True)))
    except Exception:  # noqa: BLE001 — never a fake "unknown"
        logger.warning("open_memory: memory could not be read", exc_info=True)
        return _result({"status": "unavailable",
                        "message": _MEMORY_UNAVAILABLE})
    if opened is None:
        return _result({"status": "ok", "entries": [], "message": (
            "No memory ledger is read here; search_memory reads the saved "
            "conversation.")})
    return _result({"status": "ok", "entries": opened})


#: Every arm in which the mark was not written: the store unavailable, busy
#: past the turn-path bound, or his message not stored yet. Worded like the
#: read tools' unavailable message: what happened, and what not to claim.
_CORRECTION_UNAVAILABLE = (
    "Memory could not mark the correction right now. Do NOT say memory was "
    "updated. Use his new value in this conversation; if he asks, tell him "
    "memory couldn't be updated just now."
)


@tool(
    "correct_memory",
    "Use when his message in this turn says something stored in memory is "
    "wrong or out of date. It marks his message as a correction: memory is "
    "updated from his own words, and every conversation sees the correction "
    "from its next turn. Returns at once; there is nothing to tell him. "
    "`what`: a few words naming what he corrected.",
    {"what": str},
)
async def correct_memory(args: dict) -> dict:
    import agent as agent_mod
    from memory import corrections

    origin = _snapshot_origin()
    key = origin.get(agent_mod.MEMORY_OPENING_KEY)
    if engagement_var.get(None) is not None or not key:
        return _result({"status": "error", "kind": "no_message_of_his",
                        "message": (
                            "correct_memory marks his own message in this "
                            "conversation, and this turn has none stored. "
                            "Nothing was marked.")})
    store = _memory_store()
    if store is None:
        return _result({"status": "unavailable",
                        "message": _CORRECTION_UNAVAILABLE})
    try:
        marked = await asyncio.to_thread(
            corrections.mark_operator_event, store, key)
    except Exception:  # noqa: BLE001 — reported, never a fake "queued"
        logger.warning("correct_memory: the mark could not be written",
                       exc_info=True)
        return _result({"status": "unavailable",
                        "message": _CORRECTION_UNAVAILABLE})
    if not marked.found:
        # His message may still be in the spool (captured while the store
        # was busy). It is never drained here — a drain takes as long as its
        # backlog and waits on spool writes — so ask for the background
        # drain and answer now; a later call marks it once it is stored.
        from memory import capture
        capture.request_drain(store)
        return _result({"status": "unavailable",
                        "message": _CORRECTION_UNAVAILABLE})
    return _result({"status": "queued", "message": (
        "His message is marked as a correction; memory will be updated from "
        "it. Nothing to tell him.")})


@tool(
    "wipe_memory",
    "Irreversibly wipe ALL memory: saved conversations, notes about the "
    "operator, the search index and memory backups; every conversation starts "
    "fresh. Reminders, triggers, files, settings, prompts, pending tasks and "
    "plugin data stay. Operator-only and consent-gated: posts an "
    "Approve/Cancel keyboard to the operator's DM and acts only on the "
    "operator's Approve tap, which records the request and restarts Casa; the "
    "wipe runs as the app starts again. Two-turn: returns awaiting_user "
    "immediately. A `settled` status instead means the consent request was "
    "already over when this call returned: nothing is pending on it, and what "
    "happened is reported in that keyboard message, not here.",
    {},
)
async def wipe_memory(args: dict) -> dict:
    """The agent door. Code-enforced gates, in order: a direct genuine
    operator-DM turn (never a delegated/engagement/scheduled context), a
    configured operator identity (empty ⇒ NOBODY is operator ⇒ refused for
    everyone), and the operator-bound broker claim (#469 — only the configured
    operator's tap can commit). On Approve the broker's finish hook — outside
    any turn — records the wipe intent, says so in the keyboard message, and
    asks the Supervisor for the app restart; the wipe itself runs at the next
    container start (``memory.maintenance``)."""
    from provenance import turn_provenance

    prov = turn_provenance()
    if prov.transport not in ("dm", "button") or prov.execution != "direct":
        return _result({
            "status": "error", "kind": "unsupported_origin",
            "message": (
                "wipe_memory requires a direct, genuine inbound-DM turn "
                f"(got transport={prov.transport!r} execution={prov.execution!r})"
            ),
        })
    if engagement_var.get(None) is not None:
        return _result({
            "status": "error", "kind": "forbidden",
            "message": "wipe_memory is not available from an engagement",
        })

    from memory import maintenance as memory_maintenance

    # The wipe card is Casa's own: it goes through Casa's bot, in the
    # operator's direct chat there, whichever bot the request came in on.
    transport = _transport()
    channel = _casa_endpoint()
    address = None
    if channel is not None:
        try:
            address = transport.casa_operator_address()
        except EndpointNotReady:
            # Casa's bot has not connected yet: the card cannot be posted,
            # as when its post fails, and nothing is registered.
            return _result({
                "status": "error", "kind": "delivery_failed",
                "message": "could not deliver the consent keyboard to the operator",
            })
        except AddressError:
            address = None
    if channel is None or address is None:
        # Identity v0.136: with telegram_chat_id empty NOBODY is the
        # operator, so consent cannot be granted and the wipe is denied for
        # everyone — same posture as protected plugin tools.
        return _result({
            "status": "error", "kind": "consent_channel_unavailable",
            "message": (
                "no configured operator DM — the wipe consent keyboard "
                "cannot be posted, so the wipe is refused"
            ),
        })
    bot_id = address.bot_id
    chat_id = operator_id = address.chat_id

    from verdict_broker import BROKER
    from channels.telegram import render_ask_body, short_option_labels

    rid = uuid.uuid4().hex
    options = ["Approve — wipe everything", "Cancel"]
    question = f"⚠️ Wipe ALL memory?\n\n{memory_maintenance.CONFIRMATION}"
    scope = tg_scope("authz", bot_id, chat_id)
    body = render_ask_body(None, question, options, short_option_labels(options))
    req, _created = BROKER.register(
        namespace="resident_ask", scope=scope, request_id=rid,
        timeout_s=300.0, detached=True, supersede=False,
        meta={
            "options": options,
            "bot_id": bot_id,
            "chat_id": chat_id,
            "operator_id": operator_id,
            "kind": "memory_wipe",
            "_scope": scope,
        },
    )

    async def _post():
        # #1038: the wipe confirmation is Casa's own text, not the model's.
        return await channel.post_dm_keyboard(
            chat_id=chat_id, request_id=rid, text=casa_text(body), options=options,
            short_labels=True,
        )

    def _finish_factory(message_id: int):
        async def _wipe_status(line: str) -> None:
            await channel.edit_dm_message(
                chat_id, message_id, f"{body}\n\n{line}")

        async def _finish(outcome: dict) -> None:
            kind = outcome.get("outcome")
            if kind == "answered" and outcome.get("option_index") != 0:
                # A tap on "Cancel". This one really was a cancellation.
                await channel.edit_dm_message(
                    chat_id, message_id,
                    f"{body}\n\n(cancelled — nothing was deleted)",
                )
                return
            if kind != "answered":
                # #933 mirrored: this keyboard used to call every non-answered
                # outcome a cancellation, so an operator who simply never came
                # back was told they had declined. The consequence clause is
                # true either way and stays.
                import ask_retirement

                await channel.edit_dm_message(
                    chat_id, message_id,
                    ask_retirement.retired_body(
                        body, kind, outcome.get("reason"),
                        consequence="nothing was deleted"),
                )
                return
            # Approve: the finish hook runs broker-owned, OUTSIDE any turn.
            # Update intake pauses before the restart is requested, so the
            # tap is confirmed to Telegram and never redelivered after it —
            # on every bot, since the restart ends every bot's intake.
            try:
                await memory_maintenance.request_wipe(
                    reply=_wipe_status,
                    notify=getattr(channel, "notify_operator", None),
                    pause_updates=getattr(
                        transport, "pause_updates_for_restart", None),
                    resume_updates=getattr(transport, "resume_updates", None),
                )
            except Exception as exc:  # noqa: BLE001 — report truthfully
                logger.exception("memory wipe could not be scheduled")
                await _wipe_status(
                    f"❌ The wipe could not be scheduled ({exc}); nothing "
                    "was deleted.")
        return _finish

    await BROKER.ensure_posted(req, _post, _finish_factory)
    if "message_id" not in req.meta:
        return _result({
            "status": "error", "kind": "delivery_failed",
            "message": "could not deliver the consent keyboard to the operator",
        })
    # #682: the same last-moment liveness question the plain arm asks. This
    # lane (`authz:<bot>:<chat>` on Casa's bot) is retired by `/new`'s scope
    # cancel, by the 300 s timeout, by an operator tap and by shutdown's
    # `cancel_all`.
    if rid not in BROKER.pending(namespace="resident_ask", scope=scope):
        return _result({"status": "settled", "request_id": rid,
                        "message": _SETTLED_WIPE_MESSAGE})
    return _result({
        "status": "awaiting_user", "request_id": rid,
        "message": (
            "consent keyboard posted to the operator; the wipe runs only on "
            "an explicit Approve tap and reports its result in that message"
        ),
    })


# ---------------------------------------------------------------------------
# delegate_to_agent — Phase 3.1
# ---------------------------------------------------------------------------


# Phase 3.1: sync-mode wait ceiling. 60 s per spec §6.3. Exposed as a
# module-level constant so tests can monkeypatch to drive the degraded
# path without waiting a minute.
_SYNC_WAIT_TIMEOUT_S: float = 60.0

# Phase 3.5 (Plan 4b): max delegation depth. depth=0 is a direct call from
# a resident; depth>=1 is a delegated turn. Cap at 1 to prevent chains.
_MAX_DELEGATION_DEPTH: int = 1

# #283: global cap on concurrently LIVE (active|idle) engagements created
# from AGENT context (a bound engagement turn, a delegated turn, or any turn
# not positively marked as live-operator ingress). Enforced via the
# occupancy-debt AgentSpawnLimiter wired by init_tools — reservation
# acquired before any external side effect, transferred to the record at
# create(), released only by terminal transitions. Code-owned constant: no
# loadable artifact can raise it.
_AGENT_SPAWN_CAP: int = 3

# #324: cap on the `previous_result` slice of the continue_voice_job
# internal envelope (which is exempt from the caller-facing context bound —
# see _prelaunch's `internal_context`). Stored results have no size
# contract of their own, so the envelope bounds this component itself.
_CONTINUATION_RESULT_CHARS: int = 16_000

# A4: voice turn budget. Voice has no follow-up channel to deliver a LATER
# completion notification on, so a synchronous specialist wait on voice is
# bounded by the turn's own deadline (channels/voice/channel.py's
# _voice_turn_budget_s(), propagated as origin["voice_deadline"]) instead
# of the general _SYNC_WAIT_TIMEOUT_S ceiling. _VOICE_FALLBACK_RESERVE_S is
# held back from the wait so there is always time left to cancel, tear
# down, and speak the deadline_exceeded response before the client/infra
# timeout fires. _VOICE_TEARDOWN_BOUND_S bounds how long we wait for a
# cancelled specialist task to actually finish unwinding.
_VOICE_FALLBACK_RESERVE_S: float = 5.0
_VOICE_TEARDOWN_BOUND_S: float = 2.0


def _snapshot_origin() -> dict:
    """Copy the current origin at handler entry (AR-2, pooling spec §Q7).

    With a pooled SDK client, ``origin_var`` in the read-task context is
    bound to a MUTABLE holder rewritten at each turn start. Any handler
    that keeps the reference across an await that can outlive its turn
    (delegations most of all) would read the NEXT turn's origin — for
    the delegation retain gate that is a clearance violation, not just
    misattribution. Snapshot once, at entry, before any await."""
    import agent as agent_mod
    return dict(agent_mod.origin_var.get(None) or {})


def _current_scope(origin: "dict | None" = None) -> "TurnScope | None":
    """#1038: the scope this emission commits under. Resolution order (design
    §3.4): a bound engagement → its scope, minted from the record (so a resumed
    engagement keeps sending); else the turn's own scope off *origin* — the
    handler's ENTRY snapshot, never a fresh read of the holder, which the next
    turn on a pooled client rewrites in place while this handler is mid-await
    (INV-OUT-004); else ``None`` — and a caller that gets ``None`` refuses, so
    the absence is loud. Never a permissive default."""
    from output_boundary import resolve_scope
    return resolve_scope(origin if origin is not None else _snapshot_origin())


_NO_SCOPE_MESSAGE = ("output not submitted: this call carries no turn provenance "
                     "(no turn scope is bound)")


def _launch_note(origin: dict, text: str) -> "tuple[dict, str]":
    """#1038 §7: a brief handed to a delegation or an engagement is model text
    STORED for a later turn to narrate, so its disclosure resolves NOW, at
    launch, into a note carried on the record's origin as a plain string
    (``_inherited_note`` survives persistence; the live scope does not).
    Returns ``(origin copy, note)``; the copy is untouched when nothing is
    owed, and a launch with no scope bound owes nothing here — its child's
    own emissions are what refuse."""
    scope = origin.get("turn_scope")
    out = dict(origin)
    if not isinstance(scope, TurnScope):
        return out, ""
    note = scope.admit(IntentKind.STORED, text).note
    if note:
        out["_inherited_note"] = note
    return out, note


def _origin_clearance_markers(origin: dict) -> tuple[str | None, str | None]:
    """The ``(route, clearance)`` pair an access-control decision must use.

    #336 (Terra, review r2): an engagement's tool call arrives over the
    internal socket, which binds ``engagement_var`` but NOT ``origin_var`` —
    so the ambient origin is empty and a clearance keyed off it fell through
    to the channel default, which on telegram is ``private``. An engagement
    started or steered by a NON-OPERATOR sender could therefore recall the
    operator's private memory through its own tools, going around the
    per-sender clearance the ingress had just established.

    An engagement inherits the clearance of the turn that created it, read
    from the markers its record persisted (the ingress stamps them
    server-side and the tombstone preserves them). Only consulted when the
    ambient origin carries no route of its own, so a delegated in-process
    turn — which does carry one — is unaffected; a record with no markers
    (created before this release, or from an origin that stamps none) yields
    ``(None, None)`` and keeps today's channel-keyed behavior.
    """
    route = origin.get("_origin_route")
    if route is not None:
        return route, origin.get("_origin_clearance")
    eng = engagement_var.get(None)
    if eng is None:
        return None, None
    eng_origin = getattr(eng, "origin", None) or {}
    return (
        eng_origin.get("_origin_route"),
        eng_origin.get("_origin_clearance"),
    )


def _engagement_rebuild_fence() -> dict | None:
    """#369: refuse an engagement-bound tool call while the record's
    ``context_rebuild_pending`` is set (a memory wipe removed the session
    transcripts the caller's session was resumed from). Engagements dispatch tools
    IN-PROCESS, so the engagement-sensitive tools call this fence.
    Returns the refusal payload, or None when the call may proceed."""
    eng = engagement_var.get(None)
    if eng is None or not getattr(eng, "context_rebuild_pending", False):
        return None
    return {
        "status": "error", "kind": "engagement_context_rebuilding",
        "message": (
            "This engagement's context is being rebuilt; retry shortly."
        ),
    }


def _effective_delegation_depth(origin: dict) -> int:
    """The delegation depth an anti-chaining decision must use (#283).

    Ambient origin first; when it carries no depth and the calling turn is
    a bound engagement (in_casa turns INHERIT ``origin_var`` from the parent
    task, so a depth stamped on the interactive child's RECORD origin is
    invisible to the ambient read), fall back to the record's persisted
    origin — the same fallback shape as ``_origin_clearance_markers``.
    """
    depth = (origin or {}).get("delegation_depth")
    if depth is not None:
        return int(depth or 0)
    eng = engagement_var.get(None)
    if eng is None:
        return 0
    eng_origin = getattr(eng, "origin", None) or {}
    return int(eng_origin.get("delegation_depth", 0) or 0)


def _is_agent_context(origin: dict) -> bool:
    """Is the calling turn agent context for the spawn cap (#283)?

    A spawn is operator-exempt IFF the ambient origin carries the reserved,
    server-stamped ``_operator_turn`` marker AND the turn is neither a bound
    engagement turn nor a delegated turn. Everything else — synthesized
    delegation-completion turns, scheduled/trigger turns, webhook and
    plugin-event turns, engagement turns, delegated turns — is agent
    context. Absence means capped: the marker is stamped only at live
    authenticated operator ingress, so this fails CLOSED (design r2, Sol:
    a prompt-injected synthetic resident turn must not classify as the
    operator).
    """
    if engagement_var.get(None) is not None:
        return True
    if _effective_delegation_depth(origin) > 0:
        return True
    return not bool((origin or {}).get("_operator_turn"))


def _agent_spawn_cap_refusal() -> dict:
    """Typed refusal for an agent-context spawn at the cap (#283)."""
    return _result({
        "status": "error", "kind": "agent_spawn_cap_exceeded",
        "message": (
            f"Refused: {_AGENT_SPAWN_CAP} agent-spawned engagements are "
            "already live. Complete or cancel one first, or ask the "
            "operator to start this engagement themselves."
        ),
    })


def _result(payload: dict, *, is_error: bool | None = None) -> dict:
    """Wrap a JSON-serializable payload as the tool's MCP content.

    F-7 (v0.32.0): when ``payload["status"] == "error"`` (or the caller
    explicitly passes ``is_error=True``), set ``is_error: True`` on the
    envelope. Without this flag the SDK's ``ToolResultBlock`` defaults
    ``is_error=False`` and ``sdk_logging.log_tool_result`` emits
    ``ok=True`` even for failures — operators reading turn telemetry
    would think a registry-rejected launch actually spawned.
    Auto-detection via ``payload["status"]`` keeps the existing call sites
    untouched while making every error path consistently observable.

    O-1 (v0.37.9): also recognise ``payload["ok"] is False`` so the
    install/uninstall plugin envelopes (which use ``{"ok": False,
    "error": ...}`` instead of ``{"status": "error", ...}``) surface as
    MCP errors too. Live evidence (2026-05-14 P29.1 cid ``52240634``):
    ``tool_result name=install_casa_plugin ok=True ms=12594`` for a
    ``plugin_not_in_marketplace`` failure — telemetry reported the
    failure as success, contradicting F-7's intent.

    The dict key MUST be ``is_error`` (snake_case): the Anthropic Agent
    SDK's MCP server adapter reads
    ``result.get("is_error", False)`` (see
    ``claude_agent_sdk/__init__.py:512``) and converts to the MCP wire
    field ``isError`` itself. Passing ``isError`` here gets silently
    dropped on the way to the model.
    """
    if is_error is None:
        is_error = (
            payload.get("status") == "error"
            or payload.get("ok") is False
        )
    envelope: dict = {"content": [{"type": "text", "text": json.dumps(payload)}]}
    if is_error:
        envelope["is_error"] = True
    return envelope


def _job_origin_refusal(origin: dict) -> dict | None:
    """R-2 (v0.69.7): a job talks to the operator in their Telegram direct
    chat, so a request that came from another channel cannot start one. Full
    non-Telegram origination is backlogged. ``None`` when the origin can."""
    origin_channel = origin.get("channel", "telegram")
    if origin_channel == "telegram":
        return None
    return _result({
        "status": "error", "kind": "engagement_wrong_origin",
        "message": (
            "engagements can only be initiated from Telegram; this request "
            f"originated from {origin_channel!r}. Non-Telegram origination "
            "is not supported yet."
        ),
    })


# Q-1 (v0.69.8, operator decision 2026-07-12): the SDK meta-tools that spawn a
# sub-agent. They bypass `allowed_tools` AND the v0.68.0 fail-closed
# can_use_tool callback (empirically: the CLI does not consult the callback for
# them), so a restricted agent could spawn a sub-agent that reaches a broad
# default toolset its own allowlist excludes. `disallowed_tools` IS enforced by
# the CLI (removes them from the surface), so specialists — and butler, via its
# runtime.yaml — are denied these. NOT `ToolSearch` (operator kept it: it is
# the deferred-tool-load mechanism and cannot spawn a sub-agent on its own).
_SUBAGENT_SPAWN_TOOLS = ("Agent", "Task")


def _cli_grants_bash(allowed_tools) -> bool:
    """Whether the pinned CLI (2.1.273) reads a Bash grant in ``allowed_tools``.

    #1294: mirrors the CLI's own two steps. The SDK joins the list with ``,``
    into ``--allowedTools``; the CLI splits that on commas and spaces outside
    parentheses (its tokenizer honours no escapes), then parses each rule: it
    grants Bash only for a bare ``Bash`` or ``Bash(...)`` whose first
    unescaped ``(`` follows the name and whose last unescaped ``)`` ends the
    rule. ``Bash()`` and ``Bash(*)`` grant the whole tool. Any other shape —
    ``Bash(``, ``Bash(x\\)`` — is malformed and grants nothing.
    """
    tokens, cur, in_parens = [], "", False
    for ch in ",".join(allowed_tools):
        if ch == "(":
            in_parens = True
        elif ch == ")":
            in_parens = False
        elif ch in ", " and not in_parens:
            if cur.strip():
                tokens.append(cur.strip())
            cur = ""
            continue
        cur += ch
    if cur.strip():
        tokens.append(cur.strip())

    def _unescaped(tok: str, i: int) -> bool:
        k = i - 1
        while k >= 0 and tok[k] == "\\":
            k -= 1
        return (i - 1 - k) % 2 == 0

    return any(
        tok == "Bash" or (
            tok.startswith("Bash(") and len(tok) > 5 and tok.endswith(")")
            and _unescaped(tok, len(tok) - 1))
        for tok in tokens)


def _with_subagent_spawn_disallowed(disallowed) -> list[str]:
    """Return ``disallowed`` (any iterable) plus the sub-agent-spawn tools and
    the CLI's cross-session tools (``claude_runtime.CROSS_SESSION_TOOLS``),
    de-duplicated, order-stable."""
    out = list(disallowed)
    for t in _SUBAGENT_SPAWN_TOOLS:
        if t not in out:
            out.append(t)
    return with_cross_session_tools_denied(out)


def _resolution_from_recorded(plugin_artifacts) -> "plugin_registry.ResolutionResult":
    """Sol round-3 H7b: rebuild a ResolutionResult from an engagement's RECORDED
    plugin_artifacts (§3.8) so a resumed specialist loads exactly what it started
    with (paths AND derived grants), never re-resolving current assignments.
    Fails closed (raises) if a recorded artifact path is gone/corrupt/identity-
    mismatched — those are pre-v0.76 failure modes, unchanged here.

    A:§3.7 (r2-B6/r3-4): a malformed ``casa.protectedTools`` in one RECORDED
    artifact's manifest is PER-PLUGIN degradation, not a whole-resume abort —
    that one plugin is excluded (recorded as an issue) while every other
    recorded artifact, however many, still resolves and loads normally."""
    from plugin_registry import PluginIssue, ResolutionResult, ResolvedPlugin
    plugins = []
    issues = []
    for pa in plugin_artifacts or ():
        path = pa.get("path") if isinstance(pa, dict) else None
        if not path or not os.path.isdir(path):
            raise RuntimeError(
                f"cannot resume: recorded plugin artifact missing "
                f"(plugin_artifact_missing): {pa!r}")
        # Sol round-4: DEEP-validate before resume — a tampered/corrupt pinned
        # artifact, or one whose metadata identity no longer matches the recorded
        # artifact_id, must fail the resume closed (not silently load).
        if not plugin_store.validate_artifact(Path(path)):
            raise RuntimeError(
                f"cannot resume: recorded artifact corrupt: {pa!r}")
        _meta = plugin_store.read_metadata(Path(path)) or {}
        if pa.get("artifact_id") and _meta.get("artifact_id") != pa.get("artifact_id"):
            raise RuntimeError(
                f"cannot resume: recorded artifact identity mismatch: {pa!r}")
        manifest = {}
        try:
            manifest = json.loads((Path(path) / ".claude-plugin" / "plugin.json")
                                  .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        try:
            plugin_store.manifest_protected_tools(manifest)
        except plugin_store.StoreError:
            issues.append(PluginIssue(
                name=pa.get("name", ""), target=None, stage="resolve",
                reason_code="protected_tools_invalid",
                artifact_id=pa.get("artifact_id", "")))
            continue
        plugins.append(ResolvedPlugin(
            name=pa.get("name", ""), artifact_id=pa.get("artifact_id", ""),
            path=path, version=str(manifest.get("version", "")), manifest=manifest,
            # Task 5: thread the recorded manifest_name (added to the
            # serialized shape below) so a RESUMED session reproduces the
            # exact same runtime identity (grant/namespace strings) the
            # original launch used. `.get("manifest_name", "")` tolerates
            # engagements recorded before this field existed — those fall
            # back to the registry name via runtime_name(), same as before.
            manifest_name=pa.get("manifest_name", "") if isinstance(pa, dict) else "",
            # S8: the profile the build that wrote this row decided — the
            # EFFECTIVE one it applied if it loaded the plugin, or (#1186) the
            # name its plan gave the plugin if it withheld it — the fallback a
            # resume's plan uses when the live registry no longer assigns the
            # plugin to this target (never widens).
            profile=(pa.get("profile") if isinstance(pa, dict)
                     and isinstance(pa.get("profile"), str) else None)))
    return ResolutionResult(registry_valid=True, plugins=plugins, issues=issues)


# The casa-framework tools every engagement of a given kind is granted at
# runtime, over and above whatever its definition lists (#458 follow-up /
# v0.166.0). Interactive specialists in particular are created with an EMPTY
# tools_allowed and receive these only via the options builder. Keep the
# extra_casa_tools call sites below referencing these constants so they never
# drift from what is actually granted.
SPECIALIST_CASA_GRANTS: tuple[str, ...] = (
    "mcp__casa-framework__query_engager",
    "mcp__casa-framework__emit_completion",
)

# The two casa-framework tools EVERY engagement receives at
# launch, both kinds, independent of its definition (query_engager to read the
# conversation, emit_completion to end it). They are the same for specialists
# and executors.
MANDATORY_BRIDGE_CASA_GRANTS: tuple[str, ...] = SPECIALIST_CASA_GRANTS

# S7a: the one grant a specialist's pinned or delegated build may be offered for
# starting its own plugins' jobs (``background_jobs.offers_start_job``).
START_JOB_GRANT: str = "mcp__casa-framework__start_job"


def _delegated_resolution(cfg):
    """The plugin resolution a delegated session of *cfg* is built from, with
    the CONCRETE tier + role (see ``_build_specialist_options``)."""
    _role = getattr(cfg, "role", "unknown")
    _tier = (_agent_registry.tier_for_role(_role)
             if _agent_registry is not None else None) or "specialist"
    return plugin_registry.resolve_for(f"{_tier}:{_role}")


def _capture_build_input(cfg):
    """S5 §4.2.3: ONE capture, under the agent's lane, of exactly the derivations
    that decide WHICH artifacts and tools a delegated session of *cfg* admits
    — in the builder's own order: the resolution, the env withholding, the
    protected map, the contract map, the profile plan. The tap's re-checks
    read it and the pinned builder consumes it unchanged (no second resolve,
    no rebuild — a replacement artifact carrying the same tool name could
    otherwise be reached after the check)."""
    from pinned_run import BuildInput
    from plugin_grants import (
        profile_plan, protected_map, result_contract_map, withhold_env_unresolved,
    )
    _role = getattr(cfg, "role", "unknown")
    _tier = (_agent_registry.tier_for_role(_role)
             if _agent_registry is not None else None) or "specialist"
    resolution, withheld = withhold_env_unresolved(
        _delegated_resolution(cfg), context=f"delegated {_role} options")
    protected = protected_map(resolution)
    tool_descriptions.schedule_fill(resolution)
    contract_map = result_contract_map(resolution)
    target = f"{_tier}:{_role}"
    plan = profile_plan(resolution, target=target, withheld=[rp for rp, _ in withheld])
    return BuildInput(cfg=cfg, resolution=resolution, withheld=tuple(withheld),
                      protected=protected, contract_map=contract_map, plan=plan,
                      target=target)


# #1308: a stored-call tap's pinned turn — the operator's tap on a proposal the
# specialist posted, never a delegation, and never judged against the role's
# scope. The section rides the system prompt, beside the role text it must
# outrank; the context replaces the delegation block in the one prompt.
STORED_CALL_SYSTEM_SECTION = (
    "## Stored call\n"
    "This turn carries out a stored call. The operator tapped a button on a proposal "
    "you posted, and the button's call is a tool of a plugin assigned to you. The "
    "operator's tap is the authorisation. This is not a delegation and not a request "
    "to judge against your role's scope or remit: place the call exactly as given, "
    "then stop.")
STORED_CALL_CONTEXT = (
    "<stored_call_context>\n"
    "caller: the operator, by tapping a button on a proposal you posted\n"
    "authorisation: the operator's tap\n"
    "</stored_call_context>")


_HANDOFF_GRANT = "mcp__casa-framework__hand_off"


def _handoff_grants(cfg, *, engagement: bool) -> tuple[str, ...]:
    """Browser executor §5.4: ``hand_off`` for every specialist's delegated
    or pinned build — granted in code, never declared by a bundle (#541 keeps
    spawn tools off the bundle allowlist) — and for no engagement or job
    build, the same condition as the inbox tools (``agent_inbox``)."""
    if engagement or getattr(cfg, "kind", "") != "specialist":
        return ()
    return (_HANDOFF_GRANT,)


def _build_specialist_options(
    cfg,
    *,
    resolution=None,
    extra_casa_tools: tuple[str, ...] = (),
    output_format=None,
    max_turns: int | None = None,
    plan_out: "list | None" = None,
    task_id: "str | None" = None,
) -> ClaudeAgentOptions:
    """Build ClaudeAgentOptions for a Tier 2 specialist invocation.

    ``task_id``: the task this build runs — a job's id, passed by its
    launcher; a delegated build's task is the delegation its run serves (the
    run's delegation id, bound around the run). A file the operator sent to
    that task is readable by it — exactly the files granted to it, asked at
    each Read, so one granted while the build runs is readable at once.

    ``plan_out`` (S8): when a list is given, the ``ProfilePlan`` this build
    APPLIED is appended to it, so the caller persists exactly what the plan
    decided — what was enforced for each loaded plugin, and (#1186) the
    profile name given to each withheld one — instead of re-reading a
    snapshot a mutation could move.

    SDK-level session resume stays disabled here (``resume=None``) — what a
    specialist knows enters via its prompt and its memory tools, not SDK
    continuity; the one exception is a plugin continuation, which the
    delegated runner resumes on its own copy of these options. Hooks are
    resolved from the specialist's own ``cfg.hooks``. MCP servers are
    resolved via the shared registry — same pattern as
    :meth:`Agent._process` (agent.py step 4). Degrades to empty-dict
    when the registry is not bound (legacy callers / test harnesses)."""
    from hooks import resolve_hooks
    import agent_inbox

    # S6 §2.3: an inbox specialist reads its own ready/ and has the two
    # inbound-file tools — path and tools from the one constructor (INV-FILE-002)
    # only a pinned or delegated turn gets the inbox; a job or
    # engagement build — the only callers that pass launch grants — gets neither the
    # path nor the tools, and the #541 dispatch ceiling stays as it is
    if not task_id and not extra_casa_tools:
        task_id = _delegation_quota_key.get()
    _grants = agent_inbox.delegated_build_grants(
        cfg, engagement=bool(extra_casa_tools), task_id=task_id or None)
    _inbox_prefixes, _inbox_tools = _grants.prefixes, _grants.tools
    resolved_hooks = resolve_hooks(cfg.hooks, default_cwd=cfg.cwd, extra_readable=_inbox_prefixes,
                                   extra_read_check=_grants.read_check)
    # Sol #5: inject the /config/plugins + settings.json guard code-side (like
    # residents — agent.py step 5). A delegated specialist with Bash could
    # otherwise `echo > /config/plugins/registry.json`, bypassing validation and
    # §3.9 sequencing. Fresh dict so the shared resolved hooks aren't mutated.
    from hooks import agent_home_settings_guard_matcher
    resolved_hooks = dict(resolved_hooks)
    resolved_hooks["PreToolUse"] = [
        *resolved_hooks.get("PreToolUse", []),
        agent_home_settings_guard_matcher(),
    ]

    # #631: this builder routes DELEGATED RESIDENTS as well as specialists —
    # `delegate_to_agent` sends e.g. butler through here (see the tier
    # resolution below, and Sol #12's comment on it). A delegated resident with
    # Bash reaches its own agent home, so it needs the same agent-home write
    # guards a direct resident turn gets (agent.py step 4) — and this builder
    # has never carried any of them, which is a hole #403 and #610 left open
    # here while closing it on the two wired paths.
    #
    # TIER-AWARE, and that is load-bearing rather than tidiness: the
    # trigger-file and response-shape guards decide their Bash half on a BARE
    # BASENAME anywhere in the command, so an actual specialist that inherited
    # them would be refused for writing its own `triggers.yaml` or
    # `response_shape.yaml` inside /data/engagements (measured: 2 of 2 denied,
    # while the equivalent Write calls were 2 of 2 allowed). An unresolvable
    # tier falls back to "specialist", i.e. to the behaviour before this
    # change, which is the conservative direction for a false-refusal risk.
    _hook_tier = (_agent_registry.tier_for_role(getattr(cfg, "role", "unknown"))
                  if _agent_registry is not None else None) or "specialist"
    if _hook_tier == "resident":
        from hooks import (
            resident_prompt_write_guard_matcher,
            response_shape_write_guard_matcher,
            trigger_file_write_guard_matcher,
        )
        resolved_hooks["PreToolUse"] += [
            trigger_file_write_guard_matcher(),
            response_shape_write_guard_matcher(),
            resident_prompt_write_guard_matcher(),
        ]

    # Unified plugin architecture (§3.3): resolve with the CONCRETE tier + role.
    # Sol #12: delegate_to_agent also routes RESIDENTS through this builder
    # (sync/async delegation of e.g. butler), so a hardcoded "specialist:"
    # dropped a delegated resident's resident:<role> plugins. Use the
    # authoritative AgentRegistry tier; fall back to specialist for unknown roles.
    # Sol round-3 H7b: a caller-supplied ``resolution`` is used verbatim so the
    # engagement record + the options share ONE resolve (no create-vs-builder
    # drift), and a resumed engagement rebuilds from its RECORDED artifacts.
    _role = getattr(cfg, "role", "unknown")
    # S5 §4.2.3: on a pinned one-call turn the tap captured — under the
    # agent's lane, after its re-checks — the resolution (already env-withheld), the
    # withheld list, the protected map, the contract map and the profile
    # plan. This builder consumes them UNCHANGED: nothing is resolved,
    # filtered, mapped or planned again, so a replacement artifact carrying
    # the same tool name cannot be reached after the check.
    _owner = _pinned_run.get()
    if _owner is not None:
        resolution = _owner.build_input.resolution
        _withheld = list(_owner.build_input.withheld)
    else:
        if resolution is None:
            resolution = _delegated_resolution(cfg)
        # #424 r2 (Terra 2): a delegated specialist's build is a session build
        # like any other — withhold plugins whose required env vars are
        # unresolved (INV-PLUG-008), or the delegation starts their MCP servers
        # with literal ${VAR} placeholders. The helper filters a COPY (never the
        # caller's resolution: H7b shares that object with the engagement
        # record), so grants/protected_map below derive from what the session
        # actually loads. Re-resolved fresh per delegation, so a plugin_env
        # reload takes effect on the next delegation without further ceremony.
        from plugin_grants import withhold_env_unresolved
        resolution, _withheld = withhold_env_unresolved(
            resolution, context=f"delegated {_role} options")
    sdk_plugins = [{"type": "local", "path": rp.path}
                   for rp in resolution.plugins]

    # Authorization grants (A:§3.2): APPEND the fail-closed PreToolUse authz
    # matcher for this specialist's PROTECTED plugin tools, preserving the
    # settings guard already appended above. protected_map derives from the
    # SAME resolution — supplying GrantKey.artifact_id. The hook branches on
    # provenance, so this ONE builder (ephemeral delegations AND in_casa
    # specialist engagements) is correct: engagement turns deny cleanly. Only
    # appended when there are protected tools (matcher=None routes every call).
    from authz_grants import (
        AuthzDeps, CHALLENGES, GRANTS, make_resident_authz_hook,
    )
    if _owner is not None:
        _protected = _owner.build_input.protected
    else:
        from plugin_grants import protected_map
        _protected = protected_map(resolution)
        tool_descriptions.schedule_fill(resolution)
    _authz_role = getattr(cfg, "role", "unknown")
    _authz_hook = None
    if _protected:

        def _authz_deps_factory():
            # The transport: a challenge is posted and edited through the
            # bot of the grant identity it is for.
            ch = _transport()
            if ch is None:
                return None  # no DM reachable ⇒ unsupported-origin deny
            # Read the CURRENT loaded character name at call time (W2); a
            # specialist reload rebuilds cfg, so the next challenge names the
            # new display name. Defensive getattr: unusual cfgs degrade to the
            # role string at render time, never raise.
            _char = getattr(cfg, "character", None)
            return AuthzDeps(
                channel=ch, grants=GRANTS, challenges=CHALLENGES,
                display_name=getattr(_char, "name", None),
            )

        _authz_hook = make_resident_authz_hook(
            _authz_role, _protected, _authz_deps_factory)

    # #792: the plugin result contract — the same composite admission,
    # replacement and failure matchers the resident builder appends (see
    # agent.py), for the delegated specialist AND the specialist engagement
    # (this one builder serves both). Executor sessions get none (#923).
    _broker_env: dict[str, str] = {}
    if resolution.plugins:
        import result_broker
        _client_id = result_broker.new_client_id()
        for _event, _matchers in result_broker.broker_matchers(
                _authz_role, resolution, client_id=_client_id,
                authz_hook=_authz_hook, protected=_protected, owner=_owner).items():
            resolved_hooks[_event] = [
                *resolved_hooks.get(_event, []), *_matchers]
        _broker_env = result_broker.broker_env(_client_id)

    agent_home = specialist_cwd(cfg)

    # Skills via skills="all" below; strip any config-supplied "Skill"
    # (deprecated) — (f) v0.69.9.
    allowed_tools = [t for t in cfg.tools.allowed if t != "Skill"]
    # P-5a: installed ⇒ granted, by construction. Server-level grants from the
    # SAME resolved artifacts; disallowed_tools still wins at the CC layer.
    for grant in grants_for_resolution(resolution):
        if grant not in allowed_tools:
            allowed_tools.append(grant)
    for grant in extra_casa_tools:
        if grant not in allowed_tools:
            allowed_tools.append(grant)
    _handoff_tools = _handoff_grants(cfg, engagement=bool(extra_casa_tools))
    for grant in _handoff_tools:
        if grant not in allowed_tools:
            allowed_tools.append(grant)
    for grant in _inbox_tools:                       # S6 §2.3, the same condition as the path
        if grant not in allowed_tools:
            allowed_tools.append(grant)
    # S7a §3.1 (INV-BGJOB-008): a specialist's pinned or delegated build that
    # loads a job-declaring plugin is offered start_job — visibility only;
    # the handler decides which job at the call. On a tap the S5 pin refuses it.
    _job_tools = ((START_JOB_GRANT,) if background_jobs.offers_start_job(
        cfg, resolution, engagement=bool(extra_casa_tools)) else ())
    for grant in _job_tools:
        if grant not in allowed_tools:
            allowed_tools.append(grant)

    # S8 plugin access profiles: the plan is read HERE from the live snapshot
    # at construction (a recorded-artifact resume reaches this builder too —
    # the loaded artifact gives the namespaces, the live entry the profile).
    # The guard matcher is the barrier; the list hygiene decides visibility.
    if _owner is not None:
        _plan = _owner.build_input.plan                  # S5: captured, never re-read
    else:
        _plan = profile_plan(resolution, target=f"{_hook_tier}:{_role}",
                             withheld=[rp for rp, _ in _withheld])
    if plan_out is not None:
        plan_out.append(_plan)
    allowed_tools, _profile_denies = apply_profile_plan(allowed_tools, [], _plan)
    from hooks import profile_guard_matcher
    _profile_guard = profile_guard_matcher(_plan)
    if _profile_guard is not None:
        resolved_hooks["PreToolUse"] = [
            *resolved_hooks.get("PreToolUse", []), _profile_guard]
    if _owner is not None:
        # S5 §5.3: the pin for every NON-plugin tool (built-ins, ToolSearch,
        # Skill, the messaging tools) — FIRST, before every other
        # PreToolUse matcher; the plugin admission hook carries the same pin
        # as its own first check, because the CLI runs matching hooks
        # concurrently and one hook's deny does not stop its siblings.
        from claude_agent_sdk import HookMatcher
        resolved_hooks["PreToolUse"] = [
            HookMatcher(matcher=None, hooks=[_owner.pin_hook]),
            *resolved_hooks.get("PreToolUse", [])]

    if _mcp_registry is not None:
        # S6 (diff round 1, Astra): a casa-framework tool granted in code — the
        # engagement and job launch grants, the inbox tools, start_job,
        # hand_off (R2 acceptance F1: a role declaring no servers had hand_off
        # granted and invisible) — needs the server that exposes it, whatever
        # the config's own server list says
        _server_names = list(cfg.mcp_server_names)
        if ((extra_casa_tools or _inbox_tools or _job_tools or _handoff_tools)
                and "casa-framework" not in _server_names):
            _server_names.append("casa-framework")
        mcp_servers = _mcp_registry.resolve(
            _server_names,
            role=getattr(cfg, "role", ""),
            allowed_tools=allowed_tools,
        )
    else:
        mcp_servers = {}
    skills = "all" if getattr(cfg.tools, "skills", "all") == "all" else None

    # Compute the CLI-enforced disallowed set ONCE so the effective log line
    # below and the returned options cannot drift (Sol r1 S2): a tool that is
    # both granted and disallowed is denied at the CLI, so the log must not
    # count it as effectively usable. The profile's visibility denies ride
    # the same list, BEFORE the sub-agent and cross-session clamps.
    # #1294: Bash is hard-denied unless the role grants it as the CLI reads a
    # grant — a read-only command skips can_use_tool, and a backgrounded one
    # makes a live CLI run a turn Casa never sent. A bare ``Bash`` or a
    # well-formed ``Bash(<pattern>)`` keeps the tool; a malformed ``Bash(``
    # grants nothing at the CLI, so it does not keep it either.
    _bash_clamp = () if ("Bash" in cfg.tools.disallowed
                         or _cli_grants_bash(allowed_tools)) else ("Bash",)
    disallowed_tools = _with_subagent_spawn_disallowed(
        [*cfg.tools.disallowed, *_profile_denies, *_bash_clamp])
    if _owner is not None and "ToolSearch" not in disallowed_tools:
        # #1220: with ToolSearch on the surface the pinned CLI defers every
        # MCP tool that is not alwaysLoad, and the pin denies the one
        # ToolSearch that would load the stored tool, so the turn could never
        # call it. Without ToolSearch the CLI loads every tool up front. Only
        # here: ordinary turns keep it (the ruling above _SUBAGENT_SPAWN_TOOLS).
        disallowed_tools.append("ToolSearch")

    # #459: the boot-time `agent_capabilities` line (specialist_registry.py)
    # reports only the role.yaml DECLARATION — for a specialist whose tools
    # arrive via its owned plugin's server-level grant it structurally says
    # `declared_tool_count=0`, which is indistinguishable from a specialist
    # that got nothing. The effective set exists only HERE, after grants and
    # env-withholding are applied, and it is re-resolved fresh per delegation,
    # so emit it per build. Same never-break contract as the boot line.
    try:
        _denied = set(disallowed_tools)
        effective_tools = sorted(t for t in allowed_tools if t not in _denied)
        logger.info(
            "agent_capabilities_effective role=%s model=%s tool_count=%d "
            "tools=%s mcp_servers=%s plugins=%s",
            _role, getattr(cfg, "model", "?"),
            len(effective_tools), effective_tools,
            sorted(mcp_servers.keys()),
            sorted(rp.name for rp in resolution.plugins),
        )
    except Exception:  # noqa: BLE001 — an observability line must never break the build
        logger.warning("agent_capabilities_effective log failed for role=%s",
                       _role, exc_info=True)

    # Plan 2, Task N1b Step 22: prefer the compiled prompt bundle (an
    # installed component's activated binding — agent_loader's
    # activate_binding_for_config seam) over the legacy _compose_prompt
    # output. A specialist with no active binding (still bundled-in-image,
    # or pending-configuration) keeps cfg.compiled_prompt_bundle is None and
    # falls back unchanged.
    from prompt_compiler import projection_for

    compiled_bundle = getattr(cfg, "compiled_prompt_bundle", None)
    resolved_system_prompt = (
        projection_for(compiled_bundle, channel="text", origin_route=None).system_prompt
        if compiled_bundle is not None else cfg.system_prompt
    )
    if _owner is not None and isinstance(resolved_system_prompt, str):
        # #1308: a role prompt that scopes which delegations it answers is
        # applied by the model to a tap too, unless the tap is said to be
        # outside that judgement — said HERE, at the role prompt's own weight.
        resolved_system_prompt = f"{resolved_system_prompt}\n\n{STORED_CALL_SYSTEM_SECTION}"

    return ClaudeAgentOptions(
        model=cfg.model,
        effort=effort_for(cfg.model),
        cli_path=CLAUDE_CLI_PATH,
        max_buffer_size=SDK_MAX_BUFFER_SIZE,
        system_prompt=resolved_system_prompt,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        settings=cli_session_settings(),
        permission_mode=cfg.tools.permission_mode or "acceptEdits",
        max_turns=cfg.tools.max_turns if max_turns is None else max_turns,
        mcp_servers=mcp_servers if mcp_servers else {},
        hooks=resolved_hooks,
        cwd=agent_home,
        resume=None,
        setting_sources=["project"],
        skills=skills,
        plugins=sdk_plugins,
        # #429: pin every still-unresolved casa.setupProvides var
        # to "" so a plugin the withhold gate now admits cannot reach its MCP
        # server as a literal ${VAR} placeholder (#423). Derived from the
        # SAME filtered resolution the plugins list came from.
        env={**sanitized_env_for_resolution(resolution), **_broker_env},
        output_format=output_format,
        stderr=(_discard_structured_stderr if output_format is not None else None),
        # P-5b: no relay exists on this path — deny ungranted tools fast
        # instead of hanging on an unanswerable CC prompt.
        can_use_tool=make_fail_closed_can_use_tool(
            getattr(cfg, "role", "unknown")),
    )


_PLUGIN_JOB_ROOT = Path("/data/engagements")
# #1162: the in_casa launch cwds, each built in ONE place. The SDK names a
# session's project dir after its cwd, so the transcript reaper derives the
# dir it deletes in from these same constructors — never from a re-typed path.
EXECUTOR_CWD = "/config"


def plugin_job_cwd(engagement_id: str) -> Path:
    """A plugin job's per-engagement cwd (its own CLI project dir)."""
    return _PLUGIN_JOB_ROOT / engagement_id / "plugin-job"


def specialist_cwd(cfg) -> str:
    """A specialist's cwd: its configured ``cwd``, else its agent-home."""
    return (cfg.cwd
            or f"/config/agent-home/{getattr(cfg, 'role', 'unknown')}")


_PLUGIN_JOB_PROMPT = (
    "You are a plugin job worker, not the hosting resident. The job is one "
    "long session: work until it is finished. Do not ask questions: set aside "
    "items needing an answer and say so instead. Post progress through "
    "report_job_progress when there is something worth telling; finish "
    "through emit_completion. You have no delegation, messaging, or resident "
    "memory. The operator answers by replying to this job's messages in their "
    "direct chat; their messages reach you between your steps — read them, "
    "act on them, and carry on, using only the declaring plugin and the job "
    "tools."
)


def _build_plugin_job_options(rec, resolution,
                              plan_out: "list | None" = None,
                              brief: "tuple[str, str | None] | None" = None,
                              ) -> ClaudeAgentOptions:
    """Build a worker from its pinned plugin and launch settings. ``plan_out``
    (S8): the applied ``ProfilePlan`` is appended when a list is given.
    ``brief``: the launch's ``(task, operator words)`` that open the job's
    scratchpad; a resume passes none (``background_jobs.job_session_options``)."""
    import result_broker
    from authz_grants import AuthzDeps, CHALLENGES, GRANTS, make_resident_authz_hook
    from plugin_grants import protected_map

    identity = rec.origin["plugin_job"]
    if (len(resolution.plugins) != 1
            or resolution.plugins[0].name != identity["plugin"]):
        raise RuntimeError("declaring plugin unavailable for plugin job")
    role = rec.role_or_type
    protected = protected_map(resolution)
    tool_descriptions.schedule_fill(resolution)

    def deps():
        # The transport: a challenge goes through the bot of the job's route.
        channel = _transport()
        if channel is None:
            return None
        return AuthzDeps(channel=channel, grants=GRANTS, challenges=CHALLENGES,
                        display_name=_display_name_for_role(role))

    authz_hook = make_resident_authz_hook(role, protected, deps) if protected else None
    client_id = result_broker.new_client_id()
    hooks = result_broker.broker_matchers(
        role, resolution, client_id=client_id,
        authz_hook=authz_hook, protected=protected)
    allowed = ["Skill", "ToolSearch", *grants_for_resolution(resolution),
               *background_jobs.PLUGIN_JOB_CASA_GRANTS]
    # S8 plugin access profiles: a resident-hosted worker is built for its
    # host's ``resident:<role>`` target; same plan, same barrier, same hygiene
    # as the other builders, read from the live snapshot at construction.
    _plan = profile_plan(resolution, target=f"resident:{role}")
    if plan_out is not None:
        plan_out.append(_plan)
    allowed, _profile_denies = apply_profile_plan(allowed, [], _plan)
    from hooks import profile_guard_matcher
    _profile_guard = profile_guard_matcher(_plan)
    if _profile_guard is not None:
        hooks["PreToolUse"] = [*hooks.get("PreToolUse", []), _profile_guard]
    # The local artifact supplies its own MCP servers; only Casa's server
    # comes from the host registry, filtered to the worker's two tools.
    servers = (_mcp_registry.resolve(
        ["casa-framework"], role=role,
        allowed_tools=background_jobs.PLUGIN_JOB_CASA_GRANTS)
        if _mcp_registry is not None else {})
    cwd = plugin_job_cwd(rec.id)
    cwd.mkdir(parents=True, exist_ok=True)
    _job_model = current_resident_model(identity["model"])
    return background_jobs.job_session_options(ClaudeAgentOptions(
        model=_job_model, effort=effort_for(_job_model),
        cli_path=CLAUDE_CLI_PATH,
        max_buffer_size=SDK_MAX_BUFFER_SIZE,
        system_prompt=_PLUGIN_JOB_PROMPT,
        allowed_tools=allowed,
        # #1294: never Bash — a read-only command skips can_use_tool, and a
        # backgrounded one's completion makes the live CLI run an unprompted
        # turn of its own.
        disallowed_tools=with_cross_session_tools_denied(
            ["Agent", "Task", "AskUserQuestion", *_profile_denies, "Bash"]),
        settings=cli_session_settings(),
        permission_mode="default",
        mcp_servers=servers, hooks=hooks, cwd=str(cwd), resume=None,
        setting_sources=[], skills="all",
        plugins=[{"type": "local", "path": resolution.plugins[0].path}],
        env={**sanitized_env_for_resolution(resolution),
             **result_broker.broker_env(client_id)},
        can_use_tool=make_fail_closed_can_use_tool(role),
    ), rec, brief=brief)


def build_engagement_resume_options(
    engagement, session_id: str | None,
) -> ClaudeAgentOptions:
    """Rebuild the FULL option set for a resumed engagement, then attach
    ``resume=session_id`` (Finding 2, codex review v0.69.10).

    ``InCasaDriver.resume()`` used to open a BARE ``ClaudeAgentOptions(resume=)``,
    so a resumed specialist lost ``disallowed_tools``
    (Agent/Task — Q-1), the fail-closed ``can_use_tool`` callback, ``hooks``
    (the I-2 settings guard), ``skills``, ``mcp_servers``, and ``cwd`` — running
    on the CLI's broad default surface. Rebuilt from the engagement's CURRENT
    config via the same builder the initial start used.

    Fails CLOSED: if the specialist config is gone (removed while the
    engagement was suspended), raise rather than resume with dropped
    restrictions; so does an executor engagement left from before the
    configurator became a delegated run (there is no migration). §3.8: a
    resume rebuilds from its RECORDED plugin artifacts. Reads registry
    snapshot files — call off the event loop.
    """
    import dataclasses

    kind = getattr(engagement, "kind", "")
    role = getattr(engagement, "role_or_type", "")
    opts: ClaudeAgentOptions | None = None
    # S8: the plan the builder returns through ``plan_out`` (never a re-read)
    # is applied onto the record's rows below, in memory; the driver's
    # ``_persist_applied_profiles`` writes them strictly BEFORE the client
    # opens.
    _applied_plans: list = []
    if kind == "plugin":
        from plugin_grants import withhold_env_unresolved
        resolution = _resolution_from_recorded(engagement.plugin_artifacts)
        resolution, _ = withhold_env_unresolved(
            resolution, context=f"plugin job {engagement.id} resume")
        opts = _build_plugin_job_options(engagement, resolution,
                                         plan_out=_applied_plans)
    else:
        cfg = _specialist_registry.get(role) if _specialist_registry is not None else None
        if cfg is not None:
            # Sol round-3 H7b / round-4: a resumed specialist rebuilds from its
            # RECORDED artifacts (§3.8) — never re-resolving
            # current assignments. An EMPTY record ([]) is AUTHORITATIVE (started
            # with no plugins → resume with none); only a pre-v0.71.0 record
            # (field absent → None) falls back to a fresh resolve.
            job = (getattr(engagement, "origin", None) or {}).get("job")
            grants = SPECIALIST_CASA_GRANTS + (background_jobs.JOB_CASA_GRANTS if job else ())
            recorded = getattr(engagement, "plugin_artifacts", None)
            if recorded is not None:
                opts = _build_specialist_options(
                    cfg,
                    resolution=_resolution_from_recorded(recorded),
                    extra_casa_tools=grants,
                    plan_out=_applied_plans,
                    task_id=getattr(engagement, "id", None),
                )
            else:
                opts = _build_specialist_options(
                    cfg,
                    extra_casa_tools=grants,
                    plan_out=_applied_plans,
                    task_id=getattr(engagement, "id", None),
                )
            if job:
                opts = background_jobs.job_session_options(opts, engagement)
    if opts is None:
        raise RuntimeError(
            f"cannot rebuild options to resume {kind or 'specialist'} engagement "
            f"role={role!r}: config not found (fail-closed — refusing a bare "
            "resume that would drop Agent/Task denies + the fail-closed callback)"
        )
    if _applied_plans and getattr(engagement, "plugin_artifacts", None):
        # S8: the rows say what THIS build's plan decided (the builder's own
        # plan, never a re-read) — the profile it enforced for each plugin it
        # loaded; the driver persists them before the client opens.
        # A plugin this build did not LOAD (withheld) is never erased —
        # that would widen the next build's fallback; its row changes only
        # when the plan names a profile for it (#1186: the live assignment's,
        # or the prior binding's when unassigned), else it keeps its own.
        effective = _applied_plans[-1].effective_profiles()
        engagement.plugin_artifacts = tuple(
            {**row, "profile": effective[row.get("name")]}
            if isinstance(row, dict) and row.get("name") in effective else row
            for row in engagement.plugin_artifacts)
    return dataclasses.replace(opts, resume=session_id)


def _build_world_state_summary() -> str:
    """Return a short (<=500 tokens) snapshot of Casa's config surface.

    Appended to the configurator's system prompt at each run's options
    build. Read-only - does not include in-flight session or delegation
    state.
    """
    lines: list[str] = []
    try:
        specialists = sorted(
            getattr(_specialist_registry, "_configs", {}).keys()
        ) if _specialist_registry else []
    except Exception:  # noqa: BLE001
        specialists = []
    lines.append(f"Enabled specialists:  {', '.join(specialists) or '(none)'}")

    residents: list[str] = []
    agents_dir = "/config/agents"
    try:
        if os.path.isdir(agents_dir):
            for name in sorted(os.listdir(agents_dir)):
                if name in ("specialists", "executors"):
                    continue
                if os.path.isdir(os.path.join(agents_dir, name)):
                    residents.append(name)
    except OSError:
        pass
    lines.append(f"Residents:            {', '.join(residents) or '(none)'}")

    # The version is exported by svc-casa/run as CASA_VERSION; the VERSION
    # files this block used to read never existed, so every engagement read
    # "unknown" (production, v0.311.0, 2026-09-15).
    version = os.environ.get("CASA_VERSION", "").strip() or "unknown"
    lines.append(f"Addon version:        {version}")

    # Defaults are the configurator's to know, not the operator's to repeat:
    # the configured 1Password vault is rendered here so a secrets recipe can
    # name it (and omit it — the vault tools fall back to it).
    vault = _default_vault()
    lines.append(f"Default vault:        {vault or '(none configured)'}")

    return "\n".join(lines)


# #701: the delegation SETTLE tails — persist the terminal, announce it, and
# let the delivery acknowledgement clear the durable obligation. Anchored
# because asyncio may collect an unreferenced task mid-flight, and, since #767,
# they ARE drained at the graceful stop — `drain_delegation_settlements`, called
# twice from `casa_core._shutdown_cleanup` — because INV-JOB-009 publishes that
# a success or non-cancellation verdict still commits mid-stop, and this tail is
# the one hop between such a verdict and the registry (INV-JOB-011).
_delegation_settle_tasks: set[asyncio.Task[Any]] = set()

# #767: the delegation RUN tasks a completion callback is attached to (the async
# and degraded-sync origins). Read by the stop's drain for VISIBILITY only: a
# member that is `done()` is a RIPE producer — its verdict has landed, its
# `_done` is queued on the loop, its settle tail does not exist yet — and a
# tail-set snapshot alone would miss it. The stop never cancels or awaits a
# member that is still running; that run is left for the boot reconciliation.
_delegation_callback_tasks: set[asyncio.Task[Any]] = set()


# The CLI's own terminal verdicts on a run. Everything that is not
# ``success`` means the CLI ABORTED the turn and no envelope was ever
# produced — a state that used to be indistinguishable from a specialist
# emitting a malformed one (#254).
_CLI_RUN_SUCCESS = "success"
_KNOWN_RESULT_SUBTYPES = frozenset({
    _CLI_RUN_SUCCESS, "error_max_turns", "error_max_budget_usd",
    "error_max_structured_output_retries", "error_during_execution",
})

# LOG-time allow-list for stop reasons, mirroring `_KNOWN_RESULT_SUBTYPES`:
# the decision predicates read the raw field; only rendering goes through
# `_known_token`, where echoing an unrecognised token would be the risk.
_KNOWN_STOP_REASONS = frozenset({
    "end_turn", "stop_sequence", "tool_use", "max_tokens", "refusal",
})

# Each abort mode has a DIFFERENT repair, so each gets its own failure kind:
# a turn limit is raised in the specialist's role.yaml, a contract failure is
# a collision between the envelope and the specialist's own result contract,
# and a budget stop is a spend ceiling. One shared kind made the live mtg
# failure unactionable.
_RUN_ABORT_KINDS = {
    "error_max_turns": "specialist_turn_limit",
    "error_max_budget_usd": "specialist_budget_exhausted",
    "error_max_structured_output_retries": "specialist_result_contract_failed",
    "error_during_execution": "specialist_run_failed",
}
_RUN_ABORT_FALLBACK_KIND = "specialist_run_failed"

# The terminal reasons that mean the query loop finished the turn. An ALLOW-list
# because `ResultMessage.terminal_reason` is an OPEN namespace: the SDK types it
# `str | None` and its parser passes the CLI's value through verbatim
# (`message_parser.py`, `data.get("terminal_reason")`), documenting only a few
# examples. A deny-list of known cancellation reasons therefore reads every
# reason it has not heard of — a new one, or simply one nobody listed — as a
# completed turn, which is the wrong direction for a memory write. `None` is
# included because an older CLI reports no reason at all, as does a result that
# bypassed the query loop; excluding it would stop retaining every completed
# answer. Fail-closed here costs one true document, and does so audibly — the
# exclusion is logged and the caller's turn is still SUBMITTED to the retain
# (whether it lands is the best-effort writer's to say, not this site's).
# Failing open costs a false one that nothing can find again.
_COMPLETED_TERMINAL_REASONS = frozenset({"completed"})

# The stop reasons under which the MODEL finished its answer, same ALLOW-list
# rationale as `_COMPLETED_TERMINAL_REASONS`: `stop_reason` is an open
# `str | None` namespace, and a deny-list of {"max_tokens"} would read every
# unheard-of non-completed reason as a finished answer — the silent direction
# for a memory write. `None` counts completed (an older CLI reports no reason,
# as does a result that bypassed the query loop), expressed at the read site as
# `is not None and not in`, exactly the terminal_reason shape. `tool_use` is
# deliberately EXCLUDED: on the memory-writing delegation paths
# (`output_format=None`) a terminal result that stopped on a tool call is an
# explicitly unfinished answer with `deferred_tool_use` beside it; the
# structured-output runs that legitimately end on a tool call are voice-only,
# where retention is a no-op (Sol seam round 1). The known cost is one true
# document lost audibly when a future CLI mints a novel completed reason.
_COMPLETED_STOP_REASONS = frozenset({"end_turn", "stop_sequence"})

# Capture-time normalization sentinel for MALFORMED terminal evidence. The SDK
# parser passes non-string values through verbatim, and the old `_str_or_none`
# capture read a malformed subtype/terminal_reason/stop_reason as `None` —
# which every predicate deliberately treats as legacy-COMPLETED, the fail-open
# direction (Sol + Terra, seam round 1, independently). The sentinel is not
# `None`, not `success`, and in no allow-list, so every downstream predicate
# fails CLOSED on it with no extra validity fields.
_MALFORMED_EVIDENCE = "<malformed>"


def _terminal_str(value: object) -> str | None:
    """``None`` iff absent (the legacy/completed direction), ``str`` verbatim,
    anything else the ``_MALFORMED_EVIDENCE`` sentinel — never coerced, so no
    foreign ``__str__`` is ever executed (the `_str_or_none` discipline)."""
    if value is None:
        return None
    return value if isinstance(value, str) else _MALFORMED_EVIDENCE


def _required_terminal_str(value: object) -> str:
    """A REQUIRED terminal field: ``str`` verbatim; anything else — ``null``
    included — is the ``_MALFORMED_EVIDENCE`` sentinel. The parser reads
    ``subtype`` with ``data["subtype"]``, so on a parsed result the field is
    always present and a ``None`` is the CLI writing null into a required
    field, not an older CLI omitting it (omission raises in the parser and no
    ResultMessage exists at all — the abort path). The absent-is-legacy
    direction belongs only to OPTIONAL fields (``data.get(...)`` —
    ``terminal_reason``, ``stop_reason``), which keep ``_terminal_str``.
    Generalised from two same-shape findings (Terra r1: falsey ``is_error``;
    Sol r2: null ``subtype``): every required field fails CLOSED on a
    mistyped value; only optional fields have a legacy reading for absence."""
    return value if isinstance(value, str) else _MALFORMED_EVIDENCE


def _flag_failed_closed(value: object) -> bool:
    """A genuine ``bool`` verbatim; any other value is malformed evidence and
    reads as ``True`` — fail closed. The SDK parser passes ``is_error``
    through from a REQUIRED key, so on a parsed result the value is always
    present and a non-bool (``0``, ``""``, ``None``) is the CLI writing the
    wrong type into a bool field; ``bool()`` coercion read exactly those as
    success (Terra review r1, S1). Absence is the caller's ``getattr``
    default — an object with no such attribute claims no error, which is the
    legacy/test-construction shape and stays non-error."""
    if isinstance(value, bool):
        return value
    return True


def _run_abort_kind(subtype: str | None) -> str:
    """The failure kind for a CLI-aborted run, never raising."""
    return _RUN_ABORT_KINDS.get(subtype or "", _RUN_ABORT_FALLBACK_KIND)


@dataclass(frozen=True)
class DelegatedOutput:
    """Raw text plus the SDK's optional structured result envelope."""

    text: str
    structured_output: Any = None
    # The CLI's terminal verdict, verbatim (``None`` when this producer does
    # not report one — legacy/test constructions). Kept RAW rather than
    # allow-listed here so an unknown future subtype still compares unequal to
    # ``success`` and is treated as an abort; the allow-list is applied at LOG
    # time, where echoing an unrecognised token would be the risk.
    run_subtype: str | None = None
    # Whether a ResultMessage arrived at all. A stream that ends without one
    # certifies nothing, so it is an abort — the runner does NOT raise on that
    # path, and treating it as a completed turn sent it to the envelope parser
    # to be misreported as a malformed result (Sol review, P1).
    result_message_seen: bool = True
    # The rest of the terminal shape, recorded RAW at the single construction
    # site (cluster S): predicates are derived properties below, single-sourced
    # with the evidence, never re-expressed at a consumer. Neutral defaults
    # keep every legacy/test construction valid.
    run_is_error: bool = False
    run_api_error_status: int | None = None
    run_terminal_reason: str | None = None
    run_stop_reason: str | None = None
    tool_calls: dict[str, int] = field(default_factory=dict)
    # Slice 5p: the last text-bearing message of the turn, whole — never cut
    # by #1252 — the answer the delegate closed its turn with; it travels
    # beside a result the length cap cut, and its silence is told to the
    # resident as silence. ``None`` from a producer that does not report its
    # messages, whose whole text is then that one message.
    final_message: str | None = field(default=None, compare=False)

    @property
    def run_aborted(self) -> bool:
        """Whether the CLI ended this run without completing the turn."""
        if not self.result_message_seen:
            return True
        return (
            self.run_subtype is not None
            and self.run_subtype != _CLI_RUN_SUCCESS
        )

    @property
    def caller_error_kind(self) -> ErrorKind | None:
        """The typed caller fault this terminal result reports, or ``None``.

        Abort precedence first: a non-success subtype keeps its specific
        specialist abort taxonomy — ``is_error=True`` beside it must not
        flatten ``error_max_turns`` into a generic API fault. Then
        ``is_error`` (the SDK's documented shape for a failing API call under
        ``subtype="success"``), mapped by ``api_error_status``: 429/529 are
        the transient overload pair (RATE_LIMIT); other 500-599 SDK_ERROR;
        anything else — missing, malformed, out of range — fails CLOSED to
        API_ERROR. Last, a terminal_reason outside the completed allow-list
        (the CLI's loop stopped for another cause) is SDK_ERROR.
        """
        if self.run_aborted:
            return None
        if self.run_is_error:
            status = self.run_api_error_status
            if status in (429, 529):
                return ErrorKind.RATE_LIMIT
            if status is not None and 500 <= status <= 599:
                return ErrorKind.SDK_ERROR
            return ErrorKind.API_ERROR
        if (self.run_terminal_reason is not None
                and self.run_terminal_reason not in _COMPLETED_TERMINAL_REASONS):
            return ErrorKind.SDK_ERROR
        return None

    @property
    def answer_incomplete(self) -> bool:
        """Memory-boundary completeness: at least as wide as every
        caller-facing verdict BY CONSTRUCTION (first two clauses), not by
        call-order — a run judged incomplete for callers must never be banked
        as complete, even if a future caller stops raising. The last clause is
        memory-only: an output-token-truncated answer (``max_tokens``, or any
        stop reason outside the allow-list) stays a caller-visible success
        while the unfinished text is withheld from memory (#710)."""
        if self.run_aborted or self.run_is_error:
            return True
        if (self.run_terminal_reason is not None
                and self.run_terminal_reason not in _COMPLETED_TERMINAL_REASONS):
            return True
        return (
            self.run_stop_reason is not None
            and self.run_stop_reason not in _COMPLETED_STOP_REASONS
        )


@dataclass(frozen=True)
class JobActor:
    """Trusted creator identity used for every voice-job lookup/mutation."""

    creator_peer: str
    creator_user_id: str | None
    scope_id: str
    job_control_id: str | None = None


@dataclass
class _PermitHandoff:
    """Shared marker for lexical-to-registry permit ownership transfer."""

    transferred: bool = False


# Transport-level route capabilities ONLY (route protocol 3). Endpoint
# capability — can THIS device hear or read an answer — is negotiated per
# utterance and lives on the job as `delivery_modality`. `satellite_announce`
# used to live here and was the bug: a device fact asserted at connection
# level (#233/#224).
_ROUTE_TRANSPORT_CAPABILITIES = frozenset({
    "background_jobs", "endpoint_delivery",
})
CONCIERGE_ROLE = "concierge"


def requires_voice_handoff(origin: dict, caller_role: str) -> bool:
    """Whether this trusted Concierge call must use WS handoff delivery."""
    return (
        caller_role == CONCIERGE_ROLE
        and origin.get("channel") == "voice"
        and origin.get("voice_transport") == "ws"
        and bool(origin.get("_voice_handoff_reservation"))
    )


def validate_voice_handoff_static(
    agent_name: str, origin: dict, requested_mode: str,
) -> tuple[str, Any | None, dict | None]:
    """Normalize an eligible Concierge voice delegation before any await.

    The policy keys on the stable registered role, not an operator-editable
    display name or model prompt.  It checks only static trusted provenance;
    the existing asynchronous prelaunch pipeline still owns ACL, dependency,
    and concurrency gates.
    """
    caller_role = str(origin.get("execution_role") or origin.get("role") or "")
    if (
        requested_mode != "sync"
        or caller_role != CONCIERGE_ROLE
        or origin.get("role") != CONCIERGE_ROLE
        or origin.get("channel") != "voice"
    ):
        # #233/#224 telemetry: this passthrough leaves the delegation on the
        # ORDINARY sync path (voice budget, no handoff, no progress block) and
        # is invisible today. Production ran sync for a concierge voice turn
        # that this function should have normalized — the two candidate causes
        # (a `mode` string that isn't exactly "sync"; an origin whose `role`
        # disagrees with `execution_role`) are indistinguishable without this
        # line. Enumerated, non-payload fields only.
        _log_handoff_decision(
            "passthrough_not_eligible", agent_name, origin, requested_mode,
            caller_role)
        return requested_mode, None, None

    caller_cfg = _agent_role_map.get(caller_role)
    declared = {d.agent for d in (getattr(caller_cfg, "delegates", None) or [])}
    # Preserve the established ACL error and ordering for an undeclared
    # target; capability errors must not disclose target configuration.
    if agent_name not in declared:
        _log_handoff_decision(
            "passthrough_not_declared", agent_name, origin, requested_mode,
            caller_role)
        return requested_mode, None, None

    reservation = origin.get("_voice_handoff_reservation")
    # Computed ONCE and reused by both the branch and its telemetry, so the log
    # can never disagree with the decision actually taken.
    endpoint_can_receive = deferred_delivery_available(origin)
    if (
        not requires_voice_handoff(origin, caller_role)
        or not callable(getattr(reservation, "reserve", None))
        or not callable(getattr(reservation, "release", None))
        or not callable(getattr(reservation, "commit", None))
        or not endpoint_can_receive
    ):
        _log_handoff_decision(
            "background_unavailable", agent_name, origin, requested_mode,
            caller_role, endpoint_can_receive=endpoint_can_receive)
        return requested_mode, None, _background_delivery_unavailable_result(origin)
    _log_handoff_decision(
        "async_handoff", agent_name, origin, requested_mode, caller_role,
        endpoint_can_receive=endpoint_can_receive)
    return "async", reservation, None


# #233/#224 (v0.118.0) diagnostics. Tool-call arguments (`mode`, `agent`) are
# MODEL-SUPPLIED, and role/transport reach us on the origin, so none of them
# may be echoed. A *syntax* filter is not enough — a short plain token can
# still be meaningful content (`admin:credential`), so every value is matched
# against the CLOSED SET of values Casa itself defines and reported as
# `<other>` when it isn't one (Sol review, Blocking). The diagnosis never
# depends on the raw value: the booleans beside it carry the signal.
_KNOWN_MODES = frozenset({"sync", "async"})
# Interactive specialist engagements are retired in favour of delegations. The
# value stays in the tool schema's enum only so that a stale call — a model's,
# or one a stored scheduled prompt still asks for — reaches the handler's typed
# refusal instead of the SDK's bare validation error, which names no
# alternative.
_RETIRED_MODE = "interactive"
_INTERACTIVE_RETIRED_MESSAGE = (
    "mode='interactive' is retired: Casa no longer holds an open "
    "conversation with a specialist. Delegate the request with mode='sync' "
    "(mode='async' for long work) and tell the person the answer in your own "
    "words; they carry on by writing to you. A job the specialist owns is "
    "still started by asking it in mode='sync'.")
_KNOWN_TRANSPORTS = frozenset({"sse", "ws"})
_OTHER_TOKEN = "<other>"


def _known_token(value: object, allowed: Any) -> str:
    """``value`` iff it is one of ``allowed``, else ``<other>``.

    TOTAL by construction: a non-``str`` is rejected outright rather than
    coerced, so no foreign ``__str__`` is ever executed (this helper is also
    called outside the logging try/except) (Sol review, Low).
    """
    if not isinstance(value, str):
        return _OTHER_TOKEN
    return value if value in allowed else _OTHER_TOKEN


def _str_or_none(value: object) -> str | None:
    """``value`` iff it is genuinely a ``str``, else ``None``.

    TOTAL by construction, like ``_known_token``: a non-``str`` is rejected
    rather than coerced, so no foreign ``__str__`` is ever executed.
    """
    return value if isinstance(value, str) else None


def _int_or_none(value: object) -> int | None:
    """``value`` iff it is a real ``int`` (never a ``bool``), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


class _DelegatedRunLog:
    """A delegated run of a specialist or the configurator, in that agent's
    log, named ``Request HH:MM`` by its start in Casa's local time:
    ``▶ started`` at its start, its words and its main loop's steps as they
    come, and ``✓ answered`` or ``⚠️ aborted`` at its end. Any other run (a
    resident's) logs nothing.

    #1207: words written after a call still waiting on the operator's
    approval are held; they are logged when a consume of the same grant
    clears the cut, and never when the run ends with the cut in place. Steps
    are not held: a step names the tool, never its outcome. Silence markers
    are not logged (``output_boundary.without_sentinels``). Every call is
    synchronous and never waits on Telegram."""

    def __init__(self, cfg: Any, *, started: "datetime | None" = None) -> None:
        from datetime import datetime
        from agent_logs import LogKey
        from timekeeping import resolve_tz
        role = getattr(cfg, "role", None)
        self._key = (LogKey("agent", role)
                     if getattr(cfg, "kind", "") in ("specialist", "executor")
                     and isinstance(role, str) and role else None)
        when = started if started is not None else datetime.now(resolve_tz())
        self._run = f"Request {when:%H:%M}"
        self._logged = 0
        self._ended = False

    def _log(self, text: str) -> None:
        if self._key is not None and _agent_logs is not None:
            _agent_logs.log(self._key, self._run, text)

    def start(self) -> None:
        self._log("▶ started")

    def words(self, messages: list, cut: "int | None") -> None:
        """Log the words not yet logged, unless an approval holds them."""
        if cut is None:
            self._flush(messages, len(messages))

    def step(self, tool_name: str) -> None:
        from drivers.turn_progress import step_log_line
        self._log(step_log_line(tool_name))

    def close(self, messages: list, cut: "int | None") -> None:
        """At the run's exit: the words before any call still waiting on
        approval; what was written after it is never logged."""
        self._flush(messages, len(messages) if cut is None else cut)

    def end(self, answered: bool) -> None:
        if not self._ended:
            self._ended = True
            self._log("✓ answered" if answered else "⚠️ aborted")

    def _flush(self, messages: list, stop: int) -> None:
        # Words as the operator would see them: a leading or closing run of
        # silence markers is dropped, and a message of markers alone adds no line.
        from output_boundary import without_sentinels
        for text in messages[self._logged:stop]:
            text = without_sentinels(text)
            if text:
                self._log(text)
        self._logged = max(self._logged, stop)


def _configurator_step_line(cfg: Any, origin: dict) -> Any:
    """A step line for a configurator run (kind ``executor``), posted
    silently to the operator's DM and deleted when the run ends; ``None`` for
    any other run, when the DM is unavailable, or when the agent log is on
    (the run's steps go to the configurator's log instead)."""
    if getattr(cfg, "kind", "") != "executor":
        return None
    if _agent_logs is not None and _agent_logs.enabled:
        return None
    # Casa's own line, through Casa's bot in the operator's direct chat.
    channel = _casa_endpoint()
    send = getattr(channel, "send_step_line", None)
    delete = getattr(channel, "delete_dm_message", None)
    edit = getattr(channel, "edit_dm_message", None)
    chat_id = _operator_user_id()
    if send is None or delete is None or edit is None or chat_id is None:
        return None
    import configurator_run
    from drivers.turn_progress import TurnProgressLine

    async def _send(text: str) -> "int | None":
        mid = await send(chat_id, text)
        if mid is not None:
            # a stop that cuts the run off leaves the line; the boot deletes
            # it, through the bot that posted it
            configurator_run.record_step_line(channel.address(chat_id, mid))
        return mid

    return TurnProgressLine(
        send=_send,
        edit=lambda mid, text: edit(chat_id, mid, text),
        delete=lambda mid: delete(chat_id, mid))


def _known_role(value: object) -> str:
    """A registered agent role, else ``<other>``.

    An unknown role is exactly the H-B signal we are hunting, and ``<other>``
    reports it without echoing whatever the field actually held.
    """
    return _known_token(value, _agent_role_map.keys())


def _log_handoff_decision(decision: str, agent_name: str, origin: dict,
                          requested_mode: str, caller_role: str, *,
                          endpoint_can_receive: bool | None = None) -> None:
    """One INFO line explaining which branch the voice-handoff normalizer took.

    Enumerated provenance/capability facts ONLY — never a prompt, task, context
    or tool input. This is the line that tells apart "the model passed a mode
    that isn't exactly 'sync'" (``mode_is_sync=False``) from "origin.role
    disagrees with execution_role" (``role_matches=False``), which produce
    IDENTICAL observable behaviour: both fall through to the ordinary sync path
    with the voice budget and no progress block. The per-predicate booleans make
    the failing eligibility term directly queryable rather than inferred.
    Never raises.

    ``endpoint_can_receive`` is passed in by callers that already computed it, so
    telemetry never re-evaluates the capability check and can never disagree
    with the branch that was actually taken (Sol review, Low).
    """
    if not logger.isEnabledFor(logging.INFO):
        return
    try:
        # Route capabilities arrive on the WS route-registration frame, i.e.
        # they are CLIENT-supplied. Never echo them — report one fixed boolean
        # per capability Casa actually requires, plus a count of anything else
        # (Sol review, High). That is both leak-proof and more directly usable
        # than a rendered list.
        raw_caps = origin.get("voice_route_capabilities")
        caps = {c for c in raw_caps if isinstance(c, str)} if isinstance(
            raw_caps, (set, frozenset, list, tuple)) else set()
        reservation = origin.get("_voice_handoff_reservation")
        origin_role = origin.get("role")
        exec_role = origin.get("execution_role")
        logger.info(
            "voice_handoff_decision decision=%s agent=%s mode=%s "
            "mode_is_sync=%s caller_role=%s origin_role=%s execution_role=%s "
            "role_matches=%s origin_role_is_concierge=%s caller_is_concierge=%s "
            "channel_is_voice=%s transport=%s requires_handoff=%s "
            "reserve_ok=%s release_ok=%s commit_ok=%s route_id=%s "
            "cap_background_jobs=%s cap_endpoint_delivery=%s "
            "cap_voice_handoff=%s cap_other=%d offer_modality=%s "
            "endpoint_can_receive=%s",
            decision, _known_role(agent_name),
            _known_token(requested_mode, _KNOWN_MODES),
            requested_mode == "sync",
            _known_role(caller_role), _known_role(origin_role),
            _known_role(exec_role),
            origin_role == exec_role,
            origin_role == CONCIERGE_ROLE,
            caller_role == CONCIERGE_ROLE,
            origin.get("channel") == "voice",
            _known_token(origin.get("voice_transport"), _KNOWN_TRANSPORTS),
            requires_voice_handoff(origin, caller_role),
            # Which specific predicate failed — otherwise a
            # background_unavailable line can't say WHY (Sol review, Medium).
            callable(getattr(reservation, "reserve", None)),
            callable(getattr(reservation, "release", None)),
            callable(getattr(reservation, "commit", None)),
            bool(origin.get("voice_route_id")),
            "background_jobs" in caps,
            "endpoint_delivery" in caps,
            "voice_handoff" in caps,
            len(caps),
            selected_delivery_modality(origin),
            (deferred_delivery_available(origin)
             if endpoint_can_receive is None else endpoint_can_receive),
        )
    except Exception:  # noqa: BLE001 — diagnostics must never break a turn
        logger.debug("voice_handoff_decision log failed")


def selected_delivery_modality(origin: dict | None) -> str | None:
    """The modality THIS turn's endpoint offered, or None.

    Set from the per-utterance delivery offer the client attached and the
    channel sanitized. `None` means the device that asked cannot receive a
    deferred answer at all.
    """
    if not isinstance(origin, dict):
        return None
    offer = origin.get("voice_delivery_offer")
    if not isinstance(offer, dict):
        return None
    modality = offer.get("modality")
    return modality if modality in ("audio", "text") else None


def deferred_delivery_available(origin: dict | None) -> bool:
    """Whether the device that ASKED can receive a deferred answer.

    Replaces the old route-capability check (#233/#224). Route capabilities
    described the WebSocket connection, which every device on the Home
    Assistant shares — so a "capable" route said nothing about the device that
    actually spoke. Casa promised a spoken answer to a phone with no speaker
    and then discarded the finished ruling. Capability is a property of the
    ENDPOINT, and v1 answers on the origin endpoint only, so the offer for THIS
    turn is the whole question.

    The route still matters, but only as transport: a launch after the
    60-second disconnect grace must still fail closed.
    """
    if not isinstance(origin, dict):
        return False
    if origin.get("channel") != "voice" or origin.get("voice_transport") != "ws":
        return False
    route_id = origin.get("voice_route_id")
    device_id = origin.get("origin_device_id")
    if not isinstance(route_id, str) or not route_id.strip():
        return False
    if not isinstance(device_id, str) or not device_id.strip():
        return False
    if selected_delivery_modality(origin) is None:
        return False
    routes = getattr(_runtime, "voice_route_registry", None)
    if routes is not None:
        return bool(routes.is_recently_capable(route_id.strip()))
    return True


def _background_delivery_unavailable_result(origin: dict | None = None) -> dict:
    """Refuse the hand-off, naming the obstacle the model can act on.

    These two refusals are not interchangeable. "No acknowledged route" is an
    infrastructure fault the user can do nothing about. "This device cannot
    receive a deferred answer" is a property of where they are standing, and
    the model must say so instead of promising a follow-up — a promise nobody
    can keep is what left the operator waiting in silence (#233/#224).
    """
    if isinstance(origin, dict) and selected_delivery_modality(origin) is None:
        return _result({
            "status": "error",
            "kind": "background_delivery_unavailable",
            "reason": "no_delivery_endpoint",
            "message": (
                "This device cannot receive an answer after the conversation "
                "ends: it has no speaker to announce on and no notification "
                "target. Do not promise to follow up and do not say you will "
                "get back to them. Tell the user plainly that you cannot "
                "reach them on this device later, then either answer now "
                "yourself or suggest they ask from a device that can."
            ),
        })
    return _result({
        "status": "error",
        "kind": "background_delivery_unavailable",
        "reason": "no_acknowledged_route",
        "message": (
            "Background specialist delivery requires a current, "
            "acknowledged voice WebSocket route."
        ),
    })


def _specialist_display_name(cfg: Any, role: str) -> str:
    character = getattr(cfg, "character", None)
    display_name = getattr(character, "name", None)
    if isinstance(display_name, str) and display_name.strip():
        return display_name.strip()
    return role


def _job_actor_from_origin(origin: dict | None) -> JobActor | None:
    if not isinstance(origin, dict) or origin.get("channel") != "voice":
        return None
    scope_id = origin.get("chat_id")
    if not isinstance(scope_id, (str, int)) or not str(scope_id):
        return None
    raw_user_id = origin.get("user_id")
    user_id = None if raw_user_id is None else str(raw_user_id)
    return JobActor(
        creator_peer="voice",
        creator_user_id=user_id,
        scope_id=str(scope_id),
        job_control_id=(
            str(origin["voice_job_control_id"])
            if isinstance(origin.get("voice_job_control_id"), str)
            and origin["voice_job_control_id"].strip()
            else None
        ),
    )


def _actor_owns_job(actor: JobActor, job: VoiceJob) -> bool:
    if actor.creator_peer != job.creator_peer:
        return False
    if actor.creator_user_id != job.creator_user_id:
        return False
    if job.job_control_id is not None:
        return actor.job_control_id == job.job_control_id
    return actor.scope_id == job.scope_id


def _discard_structured_stderr(_line: str) -> None:
    """Drain structured-job stderr without placing model content in logs."""


# A JSON-Schema property name safe to interpolate into a prompt block: no
# whitespace, no markup, nothing that could close the block early.
_FIELD_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def _result_contract_block(output_format: Any) -> str:
    """State the structured envelope in the prompt, or nothing (#254).

    Casa used to request ``output_format`` through the CLI's ``--json-schema``
    flag ALONE, which reaches the specialist only as a ``StructuredOutput``
    tool definition. Nothing in the specialist's prompt said an envelope was
    expected, so the only result contract in its context was whatever its own
    doctrine defined — and a specialist doctrine is free to define a
    conflicting one. mtg's did ("your ENTIRE final message is exactly this
    YAML", a different field set); the model obeyed its doctrine, called
    ``StructuredOutput`` with an empty payload, and the CLI aborted the run
    after exhausting its retries. The answer was thrown away.

    So the envelope is stated where the specialist reads its instructions, and
    stated as OUTRANKING its own contract. Field NAMES are read off the schema
    rather than duplicated, so this block cannot drift from the parser.
    """
    if output_format is None:
        return ""
    # Past this point a structured envelope IS being demanded of the
    # specialist, so returning "" would silently recreate the exact collision
    # this block exists to prevent (Sol review, P2). A schema we cannot
    # describe is a programming error at the single internal call site, not a
    # runtime condition to degrade around — fail closed and loudly.
    schema = output_format.get("schema") if isinstance(output_format, dict) else None
    required = schema.get("required") if isinstance(schema, dict) else None
    if not isinstance(required, list) or not required:
        raise ValueError(
            "output_format must carry a non-empty schema.required field list")
    # TOTAL by construction, like `_known_token`: a non-`str` name is rejected
    # rather than coerced (no foreign `__str__` is ever executed), and the
    # grammar keeps a field name from carrying newlines or markup that would
    # break out of the block it is interpolated into (Sol review, P2).
    names: list[str] = []
    for name in required:
        if not isinstance(name, str) or not _FIELD_NAME_RE.fullmatch(name):
            raise ValueError("schema.required entries must be plain field names")
        if name in names:
            raise ValueError("schema.required contains a duplicate field name")
        names.append(name)
    fields = ", ".join(names)
    return (
        "<result_contract>\n"
        "This task is answered by CALLING THE `StructuredOutput` TOOL with "
        "every one of these fields populated:\n"
        f"{fields}\n"
        "This contract REPLACES any result format your own doctrine, skills, "
        "or persona describe — including any instruction that your entire "
        "final message be YAML, JSON, or any other layout. Do the work the "
        "way your doctrine says; then map the finished answer onto the fields "
        "above and pass them as the tool's arguments. Prose in your final "
        "message is not a result: an empty or missing tool payload discards "
        "the answer and the person who asked hears nothing. If the tool "
        "reports that your payload failed validation, correct it and call the "
        "tool again — that retry is the intended recovery, not a duplicate "
        "answer.\n"
        "</result_contract>\n\n"
    )


def _settle_setup_from_delegated_results(cfg, sdk_msg, tool_calls: dict,
                                        binding: dict) -> bool:
    """#1052: hand every NON-error plugin-tool result of a delegated session
    to the setup-obligation store — a specialist that runs its plugin's setup
    tool by hand clears the ``failed`` row that asked for that run. Only a
    terminal row is a candidate (``delegated=True``); see
    ``plugin_setup_episodes.settle_from_tool_evidence``. Returns True iff a
    row was cleared. Never raises."""
    cleared = False
    try:
        for block in getattr(sdk_msg, "content", None) or []:
            if not isinstance(block, ToolResultBlock) \
                    or getattr(block, "is_error", None) is True:
                continue
            name, invoked_at = tool_calls.get(
                getattr(block, "tool_use_id", ""), ("", None))
            if not name.startswith("mcp__plugin_"):
                continue
            import plugin_setup_episodes
            if plugin_setup_episodes.settle_from_tool_evidence(
                    role=getattr(cfg, "role", ""), tool=name,
                    invoked_at=invoked_at, binding=binding, delegated=True):
                cleared = True
    except Exception:  # noqa: BLE001 — the turn path must never see a raise
        logger.exception("delegated setup-evidence handover failed")
    return cleared


#: #1052: how long a turn waits for the health refresh a cleared setup
#: obligation starts. The refresh itself is never abandoned — past this bound
#: it finishes in the background.
_SETUP_CLEARED_REFRESH_WAIT_S = 10.0
_SETUP_CLEARED_REFRESHES: set = set()


async def _refresh_plugin_health_live() -> None:
    """A live, guarded regeneration of the plugin-health report, waited for
    with a bound — the same unit a cleared setup obligation uses (S5 §14.8:
    a faulted agent's row lands at once, not at the next mutation)."""
    await _refresh_health_after_setup_cleared()


async def _refresh_health_after_setup_cleared() -> None:
    """#1052: a setup evidence cleared a failed obligation — regenerate the
    persisted plugin-health report now, so the reply that follows the run
    (whose health notice reads the report) no longer says setup could not
    finish.

    Through ``_regenerate_plugin_health_guarded``, like every other live
    regeneration (#1055): the report lock orders only the write, so an unserialized pass could compute
    from the still-failed row, write after this one, and restore the notice
    (diff round 1, Astra S2). The guard is taken in a task of its own and
    waited for with a bound, never by the turn directly: a guard holder may
    itself be waiting on this turn, and the turn must not deadlock or stall
    on it. Past the bound the refresh completes in the background. Never
    raises."""
    def _done(t: asyncio.Task) -> None:
        _SETUP_CLEARED_REFRESHES.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error("plugin health refresh after a cleared setup "
                         "failed", exc_info=t.exception())

    try:
        task = asyncio.ensure_future(_regenerate_plugin_health_guarded())
        _SETUP_CLEARED_REFRESHES.add(task)
        task.add_done_callback(_done)
        await asyncio.wait_for(asyncio.shield(task),
                               timeout=_SETUP_CLEARED_REFRESH_WAIT_S)
    except asyncio.TimeoutError:
        logger.info("plugin health refresh after a cleared setup is waiting "
                    "for the plugin lock; it finishes in the background")
    except Exception:  # noqa: BLE001 — a turn must never fail on health
        logger.debug("plugin health refresh wait failed", exc_info=True)


async def _register_delegated_run(cfg, parent: dict):
    """A delegated run's row (``memory/transcript_runs.py``), registered
    before its request is captured and before its client starts, under the
    session id Casa chooses for this launch, or for a continuation the one its
    binding keeps, in the launch's own cwd. A role
    whose exchange is never captured (no memory budget, a stored call, an
    originating channel that is not write-trusted) registers confirmed: its
    row is only the retention pass's index."""
    from channel_policy import writes_to_bank
    from memory import transcript_runs
    captures = (cfg.memory.token_budget > 0 and not parent.get("stored_call")
                and writes_to_bank(str(parent.get("channel", ""))))
    conv = _conversation.get()
    return await transcript_runs.Run.start(
        transcript_runs.DELEGATION, role=cfg.role,
        directory=specialist_cwd(cfg),
        session_id=(conv.resume if conv is not None else None) or str(uuid.uuid4()),
        confirmed=not captures)


async def _capture_delegated(
    source: _CaptureSource, cfg, parent: dict, text: str, *,
    speaker_kind: str, speaker_id: str, resident_request: bool = True,
    answer_incomplete: bool = False, session_id: str | None = None,
) -> bool:
    """Capture one side of a delegated exchange into memory, under its
    gate: a memory-writing role (``token_budget > 0``), no stored call, a
    write-trusted originating channel. Its floor is the
    delegating turn's read clearance — the clearance the run reads at. A
    capture failure is logged and never fails the run. Returns whether the
    capture was kept: a receipt, or nothing to capture."""
    from channel_policy import writes_to_bank
    from memory import capture, eligibility
    from sensitivity import clearance_for_origin
    try:
        channel = str(parent.get("channel", ""))
        rule = eligibility.admit(
            source, text=text, channel_writes=writes_to_bank(channel),
            role_writes_memory=cfg.memory.token_budget > 0,
            stored_call=bool(parent.get("stored_call")),
            resident_request=resident_request,
            answer_incomplete=answer_incomplete)
        if rule is None:
            return True
        route, clearance = _origin_clearance_markers(parent)
        scheduled = parent.get("_scheduled_delivery") is True
        origin_kind = ("delegated_result"
                       if source is _CaptureSource.DELEGATED_ANSWER
                       else capture.origin_kind_of(
                           scheduled=scheduled,
                           operator_authored=(
                               parent.get("_unauthored_opening") is False)))
        item = capture.event(
            role=cfg.role, channel=channel, speaker_kind=speaker_kind,
            speaker_id=speaker_id, origin_kind=origin_kind, text=text,
            eligibility=rule,
            origin_tier=clearance_for_origin(route, clearance, channel),
            scheduled=scheduled, session_id=session_id)
    except Exception:  # noqa: BLE001 — capture never fails a delegation
        logger.exception("memory capture of a delegated %s failed",
                         source.value)
        return False
    return await capture.record(item) is not None


# #1220: how often a pinned turn asks the CLI whether its MCP servers settled
_MCP_SETTLE_POLL_S = 0.25


async def _await_mcp_servers_settled(client) -> None:
    """Return once the CLI's MCP status lists no server as ``pending``.

    #1220: the CLI starts plugin MCP servers in the background and a server
    still connecting or listing its tools contributes none to the request
    being built, so a pinned turn — one prompt, one allowed call — asking
    before the stored tool's server is ready could only end with no call.
    ``pending`` lasts through the connect and the tool listing, each bounded
    by the CLI's own MCP timeout, so this ends; a server that failed is
    settled too, and the turn then finds no stored tool (the refusal notice).
    The tap's ceiling bounds the whole turn, this wait included."""
    while True:
        status = await client.get_mcp_status()
        if not any(s.get("status") == "pending"
                   for s in status.get("mcpServers", [])):
            return
        await asyncio.sleep(_MCP_SETTLE_POLL_S)


async def _delegated_memory_block(cfg, parent: dict) -> str:
    """A delegated specialist's memory: its view of the ledger the
    delegating turn's clearance reads (#336: never the channel's), within
    the specialist's budget, then the correction note, read in one snapshot
    off the event loop. ``""`` when that clearance reads no ledger, and for
    an empty ledger with nothing owed: no block is not a claim that nothing
    is known. When the store cannot be read the specialist is told so, so it
    never reports that Casa lacks information."""
    from memory import capture
    from memory import note as memory_note
    from memory import view as memory_view
    from sensitivity import clearance_for_origin
    route, clearance = _origin_clearance_markers(parent)
    clearance = clearance_for_origin(
        route, clearance, str(parent.get("channel", "")))
    if memory_view.ledger_for(clearance) is None:
        return ""
    unavailable = (
        f'<memory_context agent="{cfg.role}" status="unavailable">\n'
        "Long-term memory could not be checked for this task. Do not "
        "conclude Casa lacks information on any topic — if asked, say "
        "memory could not be checked.\n"
        "</memory_context>\n\n"
    )
    store = capture.active_store()
    if store is None or not store.available:
        return unavailable
    try:
        view, note = await asyncio.to_thread(
            memory_note.with_view, store, clearance=clearance,
            budget=cfg.memory.token_budget,
        )
    except Exception:  # noqa: BLE001 — the run proceeds without memory
        logger.warning("memory view not served to %s", cfg.role,
                       exc_info=True)
        return unavailable
    parts = [p for p in ((view.text if view is not None else ""), note.text)
             if p]
    return "\n\n".join(parts) + "\n\n" if parts else ""


async def _run_delegated_agent(
    cfg, task_text: str, context_text: str, resolution=None,
    output_format=None, tool_counts: dict[str, int] | None = None,
) -> DelegatedOutput:
    """Run one ephemeral delegated turn and return text plus structured output.

    ``resolution`` (spec A5): when the caller's requires gate already
    resolved this agent's plugins, that SAME ``ResolutionResult`` is
    passed through to ``_build_specialist_options`` — no second resolve,
    so the gate's decision and what actually launches can never drift.
    ``None`` (the common no-``requires`` case) lets the options builder
    resolve fresh, exactly as before Task 5.

    The run is registered (INV-ENG-028) before its request is captured and
    before its client starts, and this ``finally`` — the run's outermost,
    enclosing the answer's capture — confirms it once every capture it made
    was kept. Its transcript is kept; only the retention pass or a wipe
    deletes it.
    """
    # AR-2: snapshot BEFORE any await — this coroutine outlives the parent
    # turn (async delegations especially), and a pooled client's origin_var
    # holder can be rewritten by the NEXT turn while this one is in flight.
    parent = _snapshot_origin()
    run = await _register_delegated_run(cfg, parent)
    try:
        return await _run_delegated_turn(
            cfg, task_text, context_text, resolution, output_format,
            tool_counts, parent=parent, run=run)
    finally:
        run.close()
        # The conversation ends with its run unless a binding can resume it: this also
        # covers a binding minted during the run, which names this session from then on.
        specialist_session.release_pad(run.session_id)


async def _run_delegated_turn(
    cfg, task_text: str, context_text: str, resolution, output_format,
    tool_counts: dict[str, int] | None, *, parent: dict, run,
) -> DelegatedOutput:
    """The body of :func:`_run_delegated_agent`, as one registered run."""
    import agent as agent_mod
    from plugin_bindings import BINDINGS

    conv = _conversation.get()
    _conversation.set(None)         # a turn nested in this one stays fresh

    # The EXECUTING agent's own identity, via the SAME cfg.kind-based fallback
    # policy SessionRegistry.register's callers use (Task 9): a resident (or a
    # bound specialist, once Plan 2 lands) keeps its real persona; an executor
    # gets a stable executor identity; an unbound Plan-1 specialist gets the
    # honest "system" identity — never mislabeled "executor:<slug>". One policy,
    # not two; resolves itself when Plan 2 populates cfg.speaker_provenance.
    executing_provenance = agent_mod.speaker_provenance_for_role(cfg)

    child_origin = {
        **parent,
        "delegation_depth": int(parent.get("delegation_depth", 0)) + 1,
        # #541: the server-generated delegation/job id — the quota key for
        # this ephemeral specialist context (send_media's per-context
        # budget). Read from the ContextVar the launching call site set
        # around its create_task (the task snapshot carries it — the same
        # context inheritance origin_var rides), NOT a parameter: ~30 test
        # doubles monkeypatch this seam by exact signature. Stamped ONLY
        # here; a specialist-classified call with no key is refused, never
        # unmetered.
        **({"_delegation_id": _delegation_quota_key.get()}
           if _delegation_quota_key.get() else {}),
        # Provenance foundation (A:§1, v0.76.0): the delegate's own role,
        # distinct from parent["role"] (the caller) — turn_provenance()
        # compares the two to classify this turn as "delegated".
        "execution_role": cfg.role,
        # #541 (Terra diff r1): the EXECUTING config's tier kind, stamped
        # from the loaded config (trusted), so the send_media quota can
        # tell a delegated RESIDENT (unmetered — its grants are the
        # operator's own role.yaml) from a delegated specialist. Absent or
        # unknown kinds stay metered: only a positive "resident" exempts.
        "_delegation_kind": getattr(cfg, "kind", "") or "",
        # Mirrors execution_role: child_origin carries the EXECUTING agent's own
        # provenance forward, not the caller's — so a NESTED delegation's
        # "parent" sees the immediately-enclosing agent's identity.
        "speaker_provenance": executing_provenance,
        # Browser executor §5.4: the requesters this run serves, root first,
        # ending with this run's own role — the hand-off bounds read it and a
        # result returns to its element before the last.
        "_handoff_chain": child_chain(parent, cfg.role),
        "_delegated_session_id": run.session_id,   # a binding minted here keeps it
    }

    # Resolve caller display name; fall back to role.
    # #436: from the LIVE role map, not `_agent_registry`, which carried a BOOT
    # snapshot of the display names — so a renamed caller kept introducing
    # itself to every specialist under its old name for the rest of the
    # process's life. Same defect as the stale `<delegates>` block, same cure:
    # one live source. (#439 now re-syncs `_agent_registry` on every reload too,
    # but the role map remains the one authority for names — a KnownAgent's
    # `name` is a copy taken at build time, and the map holds the config.)
    # Browser executor §5.4: the caller is whoever asked THIS run — the
    # chain's element before this role — which is the resident on every path
    # but a hand-off, where it is the specialist that handed the work on.
    _chain = child_origin["_handoff_chain"]
    caller_role = requester_of(_chain) or "(unknown)"
    caller_name = _display_name_for_role(caller_role)
    originating_channel = str(parent.get("channel", "")) or "(unknown)"
    suggested_register = "voice" if originating_channel == "voice" else "text"

    delegation_context = (
        "<delegation_context>\n"
        f"caller_role: {caller_role}\n"
        f"caller_name: {caller_name}\n"
        + (f"requested_through: {' > '.join(_chain[:-1])}\n"
           if len(_chain) > 2 else "")
        + f"originating_channel: {originating_channel}\n"
        f"suggested_register: {suggested_register}\n"
        + (ANSWER_SHOWN_LINE
           if str(child_origin.get("_delegation_id") or "") in _ANSWER_IN_CHAT else "")
        + "</delegation_context>"
    )
    if parent.get("stored_call"):
        # #1308: a tap is the operator's own act on this specialist's proposal,
        # not a delegation from the resident — a role that answers only its
        # own kind of delegation refused it read as one
        delegation_context = STORED_CALL_CONTEXT

    if context_text:
        body_tail = (
            f"Task: {task_text}\n\n"
            f"Context from {caller_name}:\n{context_text}"
        )
    else:
        body_tail = f"Task: {task_text}"

    # A memory role's specialist gets its view of the
    # ledger its PARENT turn's clearance reads, within its own budget, and
    # the correction note, both read in one snapshot. Opt-in via
    # cfg.memory.token_budget > 0. #1282: a stored-call turn reads NOTHING,
    # keyed exactly as its capture skip — the pinned turn's prompt is Casa's
    # instruction only.
    memory_block = ""
    if cfg.memory.token_budget > 0 and not parent.get("stored_call"):
        memory_block = await _delegated_memory_block(cfg, parent)
    prompt = (
        f"{delegation_context}\n\n{memory_block}"
        f"{_result_contract_block(output_format)}{body_tail}"
    )

    # The request is captured raw, before the run starts, and never depends
    # on the answer (INV-MEM-016); the run's row already exists.
    await run.kept(_capture_delegated(
        _CaptureSource.DELEGATED_REQUEST, cfg, parent, task_text,
        speaker_kind="resident", speaker_id=caller_role,
        resident_request=(len(child_origin["_handoff_chain"]) <= 2
                          and parent.get("synthetic") != "continuation")))

    # Off-loop: _build_specialist_options resolves the registry (file IO) —
    # keep it off the shared event loop (H2/M20).
    # Sol #4 residual (documented): this ephemeral delegation client pins the
    # artifacts it resolves here for its (short) lifetime; it is NOT recorded in
    # runtime.agents or the engagement registry, so a plugin_update landing
    # mid-delegation is not disclosed by verify until the delegation ends. This
    # window is transient and self-healing (the next delegation resolves the new
    # artifact); the PERSISTENT stale-binding incident is fully closed. Tracked
    # for a future live-binding registry (docs/ROADMAP-backlog.md).
    # #233 (v0.118.0) phase telemetry. A delegated specialist is INVISIBLE in
    # the logs — sdk_logging's system_init/tool_use/turn_done are emitted only
    # by agent.py's resident loop, and a voice delegation additionally
    # suppresses SDK protocol diagnostics (output_format set) and skips the
    # stderr callback. So a delegation cancelled at the voice budget left NO
    # trace of where its time went. These monotonic stage marks are the
    # minimum needed to tell "cold CLI/plugin startup" from "slow model/tool
    # work". Durations and message TYPES only — never prompt/task/tool payload.
    _ph_start = time.monotonic()
    _ph: dict[str, float] = {}
    _msg_count = 0
    _first_tool = False
    text = ""
    _api_error = None      # #568 — the ErrorKind, when the CLI faulted
    result_msg: ResultMessage | None = None
    token = None
    # The options build is INSIDE the try (Sol review, Medium): a voice budget
    # cancellation landing during `_build_specialist_options` would otherwise
    # produce no phase line at all — and "cancelled before the client even
    # started" is one of the answers we are looking for.
    # #1052: the session's plugin binding ({plugin: artifact_id}) — from the
    # SAME resolution the options are built from, so a setup-tool result
    # observed below is attributed to the artifact this session really ran.
    binding: dict[str, str] = {}
    tool_calls: dict[str, tuple[str, float]] = {}
    # #1252: the text-bearing messages `text` is joined from, and where they
    # stop being the operator's because a protected call is waiting on the
    # operator's approval — read at each call's result, as the resident's
    # turn reads it; the delegation reads which approvals are still waiting.
    messages: list[str] = []
    approval_cut = ApprovalCut()
    approval_scope: TurnScope | None = None
    # INV-ENG-028: the session this call launches, chosen with its row.
    own_sid: str | None = None
    # S5 §5.2.4: a pinned one-call turn's controller, when this run is one
    _owner = _pinned_run.get()
    # #1212: the bounded runner's process owner for this run, cleared here so
    # a run nested inside this one gets its own (a tap's controller owns its
    # processes itself)
    _tree = _bounded_tree.get() if _owner is None else None
    _bounded_tree.set(None)
    # The configurator's live step line in the
    # operator's DM, removed when the run ends.
    _steps = _configurator_step_line(cfg, parent)
    # The session's extras: the run in its agent's log (a specialist's or the
    # configurator's), the long-run compaction window and the pad.
    _extras = specialist_session.session_env_and_pad(cfg, child_origin, run_id=run.session_id)
    _log = _extras.log
    _raised = False

    def _options_and_binding():
        res = resolution if resolution is not None \
            else _delegated_resolution(cfg)
        opts = _build_specialist_options(cfg, resolution=res,
                                         output_format=output_format)
        if getattr(cfg, "kind", "") == "executor":
            # the configurator reads the live config surface at each run
            opts = replace(opts, system_prompt=(
                f"{opts.system_prompt}\n\n## Current world state\n\n"
                f"{_build_world_state_summary()}\n"))
        return (opts,
                {rp.name: rp.artifact_id
                 for rp in (getattr(res, "plugins", None) or [])})
    try:
        options, binding = await asyncio.to_thread(_options_and_binding)
        # One Casa-chosen session per launch — the one its row names — set
        # HERE and never in `_build_specialist_options`: in_casa engagements
        # launch and resume through that builder, and a fixed id there would
        # collide with `resume`. The project folder is the launch's own cwd.
        # The cwd is the one the run registered (its row's directory): the
        # retention pass deletes in that folder, so the launch is held to it.
        # A continuation resumes its binding's session (the CLI refuses a
        # session id beside ``resume``); a fresh one becomes the binding's.
        if conv is not None and conv.resume:
            options = replace(options, resume=run.session_id, session_id=None,
                              cwd=run.directory or options.cwd)
        else:
            options = replace(options, session_id=run.session_id,
                              cwd=run.directory or options.cwd)
            if conv is not None:
                BINDINGS.adopt(conv.binding_ref, run.session_id)
        # This client's pad is this run's session, fixed here; a stored call runs Casa's one
        # pinned call, so it has no tool. A conversation's first turn opens it with the brief;
        # a continuation's pad is the request's, as the model left it.
        options = _extras.apply(
            options, **({"task": task_text, "words": operator_words(child_origin)}
                        if conv is None else {}))
        own_sid = run.session_id
        _ph["options"] = time.monotonic()
        token = agent_mod.origin_var.set(child_origin)
        approval_scope = resolve_scope(child_origin)
        client_options = with_system_prompt_file(
            options if output_format is not None
            else sdk_logging.with_stderr_callback(options, engagement_id=None)
        )
        sdk_log_guard = (
            sdk_logging.suppress_structured_voice_sdk_payload_logs()
            if output_format is not None else nullcontext()
        )
        with sdk_log_guard:
            if _owner is not None:
                # S5 §5.2.4: the controller enters the client, pins the CLI
                # and its descendants, and OWNS it to the end — this runner
                # never enters, exits or disconnects it (a cancelled `async
                # with` and the controller's close would otherwise tear the
                # same client down concurrently). The transcript is the
                # controller's to delete after termination is confirmed.
                client = await _owner.enter(client_options, client_factory=ClaudeSDKClient)
                _client_cm = nullcontext(client)
            else:
                _client_cm = ClaudeSDKClient(client_options)
                if _tree is not None:
                    _tree.attach(_client_cm)      # #1212: a late pin finds the CLI
            async with _client_cm as client:
                if _tree is not None:
                    _tree.pin_client()            # #1212: the CLI and its servers, now
                _ph["connect"] = time.monotonic()
                if _owner is not None:
                    await _await_mcp_servers_settled(client)    # #1220
                await client.query(prompt)
                _ph["query"] = time.monotonic()
                # a task's session (a delegation's, or a continuation's)
                # takes the operator's replies to its posts from here: those
                # queued so far, then each as it comes
                _attach_task_input(str(child_origin.get("_delegation_id") or ""),
                                   cfg.role, client)
                async for sdk_msg in client.receive_response():
                    _msg_count += 1
                    _ph.setdefault("first_msg", time.monotonic())
                    if isinstance(sdk_msg, AssistantMessage):
                        # #568: an API-level fault (safety refusal included)
                        # arrives as an assistant message whose text block is
                        # the CLI's own error prose. It is never this
                        # specialist's answer — record the fault and fold no
                        # text, so the run reports as aborted rather than as a
                        # specialist that answered with an error string. A
                        # sub-agent-scoped fault is that sub-agent's problem;
                        # the specialist can still answer, so it is suppressed
                        # without ending the run.
                        _kind = api_error_kind(sdk_msg)
                        if _kind is not None:
                            if (_api_error is None and getattr(
                                    sdk_msg, "parent_tool_use_id", None) is None):
                                _api_error = _kind
                            continue
                        # #1221: one message's TextBlocks join as written;
                        # successive text-bearing messages join with one
                        # blank line, as the resident's turn does — so text
                        # written before and after a tool call keeps its
                        # boundary. A message with no text adds nothing.
                        msg_text = "".join(
                            b.text for b in getattr(sdk_msg, "content", [])
                            if isinstance(b, TextBlock))
                        if msg_text:
                            text += ("\n\n" if text else "") + msg_text
                            messages.append(msg_text)
                            _log.words(messages, approval_cut.cut)
                        _main_loop = getattr(sdk_msg, "parent_tool_use_id", None) is None
                        for block in getattr(sdk_msg, "content", []):
                            if isinstance(block, ToolUseBlock):
                                tool_calls[getattr(block, "id", "")] = (
                                    str(getattr(block, "name", "")),
                                    time.time())
                                if tool_counts is not None:
                                    name = block.name.rsplit("__", 1)[-1]
                                    tool_counts[name] = tool_counts.get(name, 0) + 1
                                if not _first_tool:
                                    _first_tool = True
                                    _ph["first_tool"] = time.monotonic()
                                if _steps is not None:
                                    from drivers.turn_progress import activity_for_tool
                                    _steps.step(activity_for_tool(
                                        str(getattr(block, "name", ""))))
                                if _main_loop:
                                    _log.step(str(getattr(block, "name", "")))
                    elif isinstance(sdk_msg, UserMessage):
                        if approval_scope is not None:
                            for block in getattr(sdk_msg, "content", None) or []:
                                if isinstance(block, ToolResultBlock):
                                    approval_cut.observe(
                                        getattr(block, "tool_use_id", ""),
                                        approval_scope.approvals, len(messages))
                            # a consume that cleared the cut releases the
                            # words held behind it
                            _log.words(messages, approval_cut.cut)
                        if _settle_setup_from_delegated_results(
                                cfg, sdk_msg, tool_calls, binding):
                            await _refresh_health_after_setup_cleared()
                    elif isinstance(sdk_msg, ResultMessage):
                        # Task 6 (spec §4.6): previously discarded — captured
                        # below (in `finally`, so it's recorded even when the
                        # client raises before the loop reaches a ResultMessage).
                        result_msg = sdk_msg
    except BaseException:
        _raised = True
        raise
    finally:
        if _steps is not None:
            _steps.finish(ok=True)        # the line goes as the answer comes
        _log.close(messages, approval_cut.cut)
        if _raised:
            _log.end(False)
        # #1207: the grants this run left waiting on the operator, for a
        # synchronous launcher's fold — published however the run ended (an
        # abort or a raise leaves the keyboard up all the same). Only calls
        # whose result this run folded count.
        if approval_scope is not None:
            approval_scope.approval_waiting = approval_cut.pending_keys
        # Emitted on EVERY exit — including the CancelledError the voice budget
        # raises, which is precisely the case we could not see before. It runs
        # BEFORE the origin_var reset so a reset failure can't skip it (Sol
        # review, Medium); `token` is None when we were cancelled during the
        # options build, so the reset is conditional.
        try:
            def _at_ms(key: str) -> int | None:
                """Milestone time since delegation start (NOT a phase duration —
                subtract two marks for that)."""
                mark = _ph.get(key)
                return None if mark is None else int(
                    (mark - _ph_start) * 1000)

            # `got_result` only ever said a ResultMessage ARRIVED — it was
            # True for a run the CLI had aborted, which is how a max-turns
            # stop and a structured-output retry exhaustion both reached the
            # operator as "the specialist returned an invalid result" (#254).
            # The subtype is allow-listed (an unrecognised token would be
            # echoing a CLI string we have not vetted); num_turns is an int.
            logger.info(
                "delegated_phases role=%s options_at_ms=%s connect_at_ms=%s "
                "query_at_ms=%s first_msg_at_ms=%s used_tool=%s "
                "first_tool_at_ms=%s msgs=%d got_result=%s subtype=%s "
                "is_error=%s num_turns=%s structured=%s total_ms=%d",
                _known_role(getattr(cfg, "role", None)),
                _at_ms("options"), _at_ms("connect"), _at_ms("query"),
                _at_ms("first_msg"), _first_tool, _at_ms("first_tool"),
                _msg_count, result_msg is not None,
                _known_token(
                    getattr(result_msg, "subtype", None),
                    _KNOWN_RESULT_SUBTYPES,
                ),
                bool(getattr(result_msg, "is_error", False)),
                _int_or_none(getattr(result_msg, "num_turns", None)),
                getattr(result_msg, "structured_output", None) is not None,
                int((time.monotonic() - _ph_start) * 1000),
            )
        except Exception:  # noqa: BLE001 — diagnostics never break a turn
            logger.debug("delegated_phases log failed")
        finally:
            # In its own finally so even a BaseException escaping the logging
            # block cannot strand the contextvar (Sol review, Low). `token` is
            # None when we were cancelled during the options build.
            if token is not None:
                agent_mod.origin_var.reset(token)
        # Task 6 (spec §4.6): aggregate cost/usage from the captured
        # ResultMessage. COUNTING is separate (`record_launch`, done by the
        # caller at ownership transfer) so a delegation that fails during
        # setup — or whose ResultMessage never arrived — is still counted;
        # here we only aggregate cost when a ResultMessage actually arrived.
        if _specialist_telemetry is not None and result_msg is not None:
            cost_usd = BINDINGS.note_cost(
                own_sid, float(getattr(result_msg, "total_cost_usd", 0.0) or 0.0))
            usage = extract_usage(result_msg)
            _specialist_telemetry.record_cost(
                cfg.role, cost_usd=cost_usd, usage=usage,
            )
        # INV-ENG-028: the client has exited — on return, raise and cancel
        # alike — so the run's retention clock starts here. The transcript
        # is kept: only the retention pass or a wipe deletes it.
        run.end()

    # #568: the CLI ended this run by reporting an API-level fault (a safety
    # refusal included) — this specialist never answered. RAISE rather than
    # returning the fault beside the text: this runner has four consumers (the
    # sync branch, the async/degraded completion callback, and both voice
    # paths), every one of which already classifies an exception with
    # `_classify_error` and fails its durable record, while a field beside the
    # text is something each of them must remember to read — and in review two
    # of the four did not. Raising here also precedes the retain below, so a
    # half-finished exchange is never written to memory as a complete one.
    # The second carrier, read exactly as the resident turn reads it
    # (sdk_client_pool.run_turn_locked): a refusal reported ONLY on the
    # terminal result must still end this run, not return an empty success.
    if _api_error is None:
        _api_error = result_api_error_kind(result_msg)
    if _api_error is not None:
        logger.warning(
            "delegated agent %s run ended by an API error kind=%s",
            _known_role(getattr(cfg, "role", None)), _api_error.value,
        )
        _log.end(False)
        raise ApiErrorTurn(_api_error)

    # Task 6 (spec §4.6): output bounding is applied by the CALLER (see
    # `specialist_limits.truncate_output` at the sync-result / async-
    # DelegationComplete assembly sites) rather than here, so the
    # `output_truncated` flag survives this task's str boundary and reaches
    # the wire. The raw text is returned; memory captures it as exchanged,
    # and only when the run completed (#699) (the bound is a caller-facing
    # surface concern, not a memory one).

    # #699: the terminal verdict is constructed HERE, before the capture.
    # Reading this object's own `run_aborted` rather than re-expressing the
    # predicate keeps the gate and the returned verdict single-sourced. The no-subtype-is-completed asymmetry now lives ONLY on
    # direct `DelegatedOutput` constructions (the field default): on a PARSED
    # result `subtype` is a required field, so null is malformed evidence and
    # captures as the sentinel, i.e. an abort (Sol review r2, S1).
    output = DelegatedOutput(
        text=text,
        structured_output=(
            getattr(result_msg, "structured_output", None)
            if result_msg is not None else None
        ),
        # `_required_terminal_str`: a malformed value — null included, this
        # being a required field — must become the fail-closed sentinel,
        # never the legacy-completed `None` (seam round 1; review rounds 1-2).
        run_subtype=(
            _required_terminal_str(getattr(result_msg, "subtype", None))
            if result_msg is not None else None
        ),
        result_message_seen=result_msg is not None,
        run_is_error=(
            _flag_failed_closed(getattr(result_msg, "is_error", False))
            if result_msg is not None else False
        ),
        run_api_error_status=(
            _int_or_none(getattr(result_msg, "api_error_status", None))
            if result_msg is not None else None
        ),
        run_terminal_reason=(
            _terminal_str(getattr(result_msg, "terminal_reason", None))
            if result_msg is not None else None
        ),
        run_stop_reason=(
            _terminal_str(getattr(result_msg, "stop_reason", None))
            if result_msg is not None else None
        ),
        tool_calls=dict(tool_counts or {}),
        final_message=messages[-1] if messages else "",
    )

    # Cluster S (#709): a terminal result that carries `is_error=True` or a
    # non-completed terminal_reason under `subtype="success"` is a failing
    # run, not an empty success — end it as a typed caller fault, exactly as
    # the #568 refusal raise above already ends a result-only refusal. The
    # raise PRECEDES the capture below, so a half-finished exchange is never
    # written to memory as a complete one; every consumer's existing exception
    # arm classifies `ApiErrorTurn` by its carried kind. CLI aborts do NOT
    # raise here — `caller_error_kind` yields to the specialist abort
    # taxonomy, which the consumers read from `run_aborted`.
    _caller_fault = output.caller_error_kind
    if _caller_fault is not None:
        logger.warning(
            "delegated agent %s run ended by a terminal API fault kind=%s",
            _known_role(getattr(cfg, "role", None)), _caller_fault.value,
        )
        _log.end(False)
        raise ApiErrorTurn(_caller_fault)
    _log.end(not output.run_aborted)

    # The answer is captured at completion — the whole live fold — only when
    # the run's own evidence says it is complete (INV-MEM-016).
    await run.kept(_capture_delegated(
        _CaptureSource.DELEGATED_ANSWER, cfg, parent, text,
        speaker_kind=("resident" if getattr(cfg, "kind", "") == "resident"
                      else "specialist"),
        speaker_id=cfg.role, answer_incomplete=output.answer_incomplete,
        session_id=own_sid))

    # An unfinished answer is not captured (``answer_incomplete`` above,
    # INV-MEM-016); say why once, where the run's own evidence is read. A
    # stored-call turn captures nothing either way.
    if (cfg.memory.token_budget > 0 and not parent.get("stored_call")
            and text and output.answer_incomplete):
        if output.run_aborted:
            logger.warning(
                "delegated agent %s run did not complete (kind=%s) — "
                "its partial answer is excluded from memory",
                _known_role(getattr(cfg, "role", None)),
                _run_abort_kind(output.run_subtype),
            )
        else:
            logger.warning(
                "delegated agent %s answer did not finish "
                "(stop_reason=%s) — its partial answer is excluded "
                "from memory",
                _known_role(getattr(cfg, "role", None)),
                _known_token(output.run_stop_reason, _KNOWN_STOP_REASONS),
            )

    return output


def _record_launch_safe(agent_name: str) -> None:
    """Count one delegation launch, never raising (spec §4.6).

    Called AFTER permit ownership has already transferred (``owned = None``),
    and guarded so a telemetry/logging failure can never propagate into the
    caller and — worse — never reach the ``owned`` finally to release a
    permit whose task/driver is already live."""
    if _specialist_telemetry is None:
        return
    try:
        _specialist_telemetry.record_launch(agent_name)
    except Exception:  # noqa: BLE001 — telemetry must never affect permit ownership or the launch
        logger.warning(
            "specialist telemetry record_launch raised for %s",
            agent_name, exc_info=True,
        )


def _permit_release_callback(permit) -> "callable":
    """Return an asyncio task done-callback that releases *permit* (spec §4.6).

    This is the SOLE authoritative release for a launched sync/async task —
    the ``SpecialistRegistry`` terminal transitions deliberately do not
    release (that would race the voice teardown's ``cancel_delegation``
    against a still-unwinding task). Attaching it to the task is strictly more
    robust than a ``finally`` inside the coroutine: a task cancelled BEFORE
    its coroutine ever starts running has no coroutine-`finally` to run, but
    its done callbacks still fire. It fires exactly once per task (normal
    completion, exception, or cancellation), and ``Permit.release()`` is
    idempotent so an overlap with the pre-launch ``owned`` guard is a
    safe no-op."""
    def _cb(_task: asyncio.Task) -> None:
        permit.release()
    return _cb


async def _fail_delegation_durably(
    delegation_id: str, failure: JobFailure, *, announce_creator: bool = False,
) -> VoiceJob | None:
    """Persist a typed FAILED terminal for a delegation, never raising away
    the caller's answerable state.

    The explicit ``JobFailure`` goes through ``fail_compat`` — the route the
    voice arms already use — because ``fail_delegation(id, exc)`` persists the
    exception CLASS NAME as the durable kind (``_failure_envelope``), so the
    ledger would say ``ApiErrorTurn`` while the caller was told
    ``rate_limit`` (Sol, seam round 1). A failed terminal write retries only
    through failure reconciliation, CARRYING THE ORIGINAL typed failure — an
    abort or caller fault must never be completed by a background retry, and
    a bare detached ``fail_compat`` task would lose its own write failure
    silently.

    #701: ``announce_creator`` arms the durable obligation to announce this
    terminal, and rides the reconciliation retry with it — only the path that
    actually posts a notification passes it. The durable row is RETURNED (None
    when the write failed or the registry does not know the job) so the
    announcing caller can ask whether the creator cancelled meanwhile."""
    try:
        return await _specialist_registry.job_registry.fail_compat(
            delegation_id, failure, announce_creator=announce_creator)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Delegation %s failure terminal write failed (%s); scheduling "
            "failure reconciliation", delegation_id[:8], exc,
        )
        try:
            (_specialist_registry.job_registry
             .schedule_failure_reconciliation(
                 delegation_id, failure,
                 announce_creator=announce_creator))
        except Exception:  # noqa: BLE001 — restart recovery remains authoritative
            logger.error(
                "Delegation %s failure reconciliation scheduling failed; "
                "restart recovery required", delegation_id[:8],
            )
    return None


async def _complete_delegation_durably(
    delegation_id: str, result: str = "", *, announce_creator: bool = True,
    closing_message: str = "",
) -> VoiceJob | None:
    """The ok arm's durable terminal, with the announcement obligation armed.

    #321's rule holds unchanged: the specialist's work is DONE and its answer
    is in the caller's hands, so a failed terminal write must not raise it
    away — the registry-owned retry completes the row instead. #701 adds only
    that the retry carries the obligation, since this caller was promised a
    notification.

    #688: ``result`` is the BOUNDED answer this caller's live notice carries,
    and it rides BOTH the direct write and the retry. If the retry did not
    carry it, a single failed write would lose the answer silently after the
    operator had already been told a notice was coming, and the boot replay
    would announce an outcome whose answer it could not produce.

    ``closing_message`` — the delegate's closing message when the cap cut it
    from ``result`` — rides beside it on both writes, for the same reason;
    passed only when there is one, so a write with none is the usual call.
    """
    closing = {"closing_message": closing_message} if closing_message else {}
    try:
        return await _specialist_registry.complete_delegation(
            delegation_id, result, announce_creator=announce_creator, **closing)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Delegation %s completed but the terminal write failed (%s); "
            "scheduling completion reconciliation", delegation_id[:8], exc,
        )
        try:
            (_specialist_registry.job_registry
             .schedule_completion_reconciliation(
                 delegation_id, result, announce_creator=announce_creator,
                 **closing))
        except Exception:  # noqa: BLE001 — restart recovery remains authoritative
            logger.error(
                "Delegation %s completion reconciliation scheduling failed; "
                "restart recovery required", delegation_id[:8],
            )
    return None


async def _settle_then_announce(
    record: DelegationRecord,
    complete: DelegationComplete,
    settle: Awaitable[VoiceJob | None],
    *,
    birth: "TaskBirth | None" = None,
    raw_text: "str | None" = None,
) -> None:
    """Persist the terminal, THEN announce it, THEN acknowledge on delivery.

    A task of an agent with its own chat (*birth*) also settles there before
    the announcement: *raw_text*, the delegate's closing message, whole (never
    the bounded, annotated copy the requester is told), is posted at the
    task's delivery, and its conversation's note is written.

    #701. Three properties, each of which the previous shape — two independent
    `create_task` calls — could not hold:

    * **Ordered.** The durable obligation is armed by the terminal write, and
      the acknowledgement clears it. Racing them lets the acknowledgement run
      first and find nothing, after which the marker is set forever and the
      operator is announced to again at every boot.
    * **Cancellation-aware.** The creator may have cancelled while the
      specialist was finishing: the registry then writes a CANCELLED terminal,
      and a creator who cancelled must not be told the job completed. The
      durable row is the authority, and it is asked before anything is
      enqueued. A cancellation landing after that question is a genuine tie
      between "the answer arrived" and "I withdrew the question", which no
      ordering can settle without the write; every non-racing case is settled.
    * **Still announced when the write fails.** The caller must be told either
      way. The acknowledgement is then a durable no-op and the registry-owned
      retry carries the obligation, which costs at most one repeat at the next
      boot.

    Drained at the graceful stop (#767, INV-JOB-011): a settle task killed
    before its write leaves the row LIVE, and boot recovery then converts a
    delegation that in fact SUCCEEDED — or failed with a specific typed kind —
    into "lost on restart", which is the opposite of what INV-JOB-009
    publishes ("every success or non-cancellation verdict still commits
    mid-stop"). `casa_core._shutdown_cleanup` therefore awaits every anchored
    tail through `drain_delegation_settlements`, first while the bus
    consumers and resident pools are still up, and again immediately before
    the job ledger closes.
    """
    row = await settle
    if row is not None and row.execution_state is ExecutionState.CANCELLED:
        logger.info(
            "Delegation %s settled as cancelled — completion not announced",
            record.id[:8],
        )
        return
    if complete.status == "ok":
        await _settle_task(birth, answer=raw_text if raw_text is not None else "")
    else:
        await _settle_task(birth, failure=str(complete.kind or ""))
    target_role = record.origin.get("role") or "assistant"
    registry = _specialist_registry.job_registry

    async def _ack() -> None:
        await registry.ack_terminal_notification(record.id)

    await _bus.notify(BusMessage(
        type=MessageType.NOTIFICATION,
        source=record.agent,
        target=target_role,
        content=complete,
        channel=record.origin.get("channel", ""),
        context={
            "cid": record.origin.get("cid", "-"),
            "chat_id": record.origin.get("chat_id", ""),
            "delegation_id": record.id,
            # the reply goes back through the bot the delegation came from
            **carry_route(record.origin),
        },
        on_delivery=_ack,
    ))


def _delegation_failure_kind(exc: BaseException) -> str:
    """The typed kind of a delegation that raised: its agent's lane was full
    (``busy``) or is out of service until restart (``agent_faulted``), the
    approval Casa started it for was used or replaced meanwhile
    (``approval_used`` / ``approval_superseded``), else the classified error."""
    import agent_lane
    import approval_records
    if isinstance(exc, agent_lane.LaneBusy):
        return "busy"
    if isinstance(exc, agent_lane.LaneFaulted):
        return "agent_faulted"
    if isinstance(exc, approval_records.ApprovalNotCurrent):
        return "approval_used" if exc.verdict == "used" else "approval_superseded"
    return _classify_error(exc).value


def _run_abort_failure(output: DelegatedOutput) -> JobFailure:
    """The explicit typed failure for a CLI-aborted delegated run."""
    return JobFailure(
        kind=_run_abort_kind(output.run_subtype),
        message=_failure_message(
            "Specialist could not complete the delegated task.", output.tool_calls),
    )


def _failure_message(message: str, tool_calls: dict[str, int]) -> str:
    calls = sorted(tool_calls.items(), key=lambda item: (-item[1], item[0]))[:12]
    if not calls:
        return message
    details = ", ".join(f"{name} ×{count}" for name, count in calls)
    return f"{message} Before stopping it called: {details}"


def _attach_completion_callback(
    task: asyncio.Task,
    record: DelegationRecord,
    *,
    quiet_if: "Callable[[DelegatedOutput], bool] | None" = None,
    birth: "TaskBirth | None" = None,
) -> None:
    """Wire the bus NOTIFICATION post on delegation completion.

    Used by the degraded-sync and async paths. Task 7's sync-ok /
    sync-error paths bookkeep inline.

    Task 6 (spec §4.6): the permit release is NOT here — it rides on the
    dedicated `_permit_release_callback` done-callback attached to the same
    task (robust even against a cancelled-before-start task, which has no
    coroutine `finally`). This callback bounds the notified output via
    `truncate_output` and propagates the `output_truncated` flag onto the
    `DelegationComplete`.

    ``quiet_if`` (a browsing continuation, Plan 3 C4a): the caller's own
    record that the specialist declared progress only (its answer was
    ``<silent/>``) — the row then completes with no announcement owed, through
    the same reconciled terminal write, and the requester hears nothing. An
    aborted or failed run is announced as always.
    """
    loop = asyncio.get_running_loop()

    def _done(t: asyncio.Task) -> None:
        # #767: the producer leaves the visibility set only in `finally`, AFTER
        # its tail (or its cancel operation) has been created and anchored, so
        # the stop's drain can never observe "no ripe producer, no tail" while
        # a verdict is in flight between the two.
        try:
            _settle_after(t)
        finally:
            _delegation_callback_tasks.discard(t)

    def _settle_after(t: asyncio.Task) -> None:
        if t.cancelled():
            loop.create_task(_specialist_registry.cancel_delegation(record.id))
            return
        complete: DelegationComplete | None = None
        settle: Awaitable[VoiceJob | None] | None = None
        raw_text: str | None = None
        # The notes name the delegate's own chat when the task shows there; a
        # continuation passes no birth, so its record supplies it. Whether the
        # answer is posted still keys on *birth* alone.
        try:
            _shown = _own_chat_name(birth or _record_birth(record))
        except Exception:  # noqa: BLE001 — the notes fall back to the usual ones
            logger.warning("delegation %s: own chat not read", record.id[:8], exc_info=True)
            _shown = ""
        try:
            output = t.result()
            if output.run_aborted:
                # Cluster S (#675): the CLI aborted the run — the streamed
                # prefix is not an answer. Exactly one error notification,
                # no partial text, and a typed FAILED terminal; the cancelled
                # and exception arms are untouched (#671 latch preserved).
                failure = _run_abort_failure(output)
                complete = DelegationComplete(
                    delegation_id=record.id,
                    agent=record.agent,
                    status="error",
                    kind=failure.kind,
                    message=_with_post_echo(failure.message, record.id, shown_in=_shown),
                    origin=record.origin,
                    elapsed_s=time.time() - record.started_at,
                )
                settle = _fail_delegation_durably(
                    record.id, failure, announce_creator=True)
            elif quiet_if is not None and quiet_if(output):
                # What the person wrote into the run still reaches the
                # requester, as the whole of the announcement.
                words = _with_operator_words(
                    "", _take_said(record.id),
                    getattr(record, "executor", "") or record.agent)
                if not words:
                    logger.info("delegation %s declared progress only — not announced",
                                record.id[:8])
                    settle = _complete_delegation_durably(record.id, "",
                                                          announce_creator=False)
                else:
                    logger.info("delegation %s declared progress only — announced for "
                                "the person's words", record.id[:8])
                    complete = DelegationComplete(
                        delegation_id=record.id,
                        agent=record.agent,
                        status="ok",
                        text=words,
                        origin=record.origin,
                        elapsed_s=time.time() - record.started_at,
                    )
                    settle = _complete_delegation_durably(record.id, words)
            else:
                said = _take_said(record.id)
                text = output.text
                raw_text = _closing_text(output)  # the message it closed with, before the cap
                bounded, output_truncated = (
                    specialist_limits.truncate_output(text))
                if output_truncated:
                    logger.warning(
                        "delegated agent %s output truncated: %d > %d chars "
                        "(spec §4.6)", record.agent, len(text),
                        specialist_limits._MAX_OUTPUT_CHARS,
                    )
                # The closing message, when the cap cut it away: computed on
                # the delegate's own words, before Casa's echo lines follow.
                closing = _closing_beyond_cut(output, bounded, output_truncated)
                # S3 §6: the echo rides the notice, after the bounded answer
                # (Casa's lines are not the delegate's text the cap bounds).
                bounded = _with_post_echo(bounded, record.id, shown_in=_shown)
                bounded = _with_silent_close_note(bounded, output, record.agent,
                                                  own_chat=bool(_shown))
                bounded = _with_own_chat_answer_note(
                    bounded, _shown, _answer_posts(birth, raw_text))
                bounded = _with_operator_words(
                    bounded, said, getattr(record, "executor", "") or record.agent)
                complete = DelegationComplete(
                    delegation_id=record.id,
                    agent=record.agent,
                    status="ok",
                    text=bounded,
                    origin=record.origin,
                    elapsed_s=time.time() - record.started_at,
                    output_truncated=output_truncated,
                    closing_message=closing,
                )
                settle = _complete_delegation_durably(
                    record.id, bounded, closing_message=closing)
        except Exception as exc:
            kind = _delegation_failure_kind(exc)
            complete = DelegationComplete(
                delegation_id=record.id,
                agent=record.agent,
                status="error",
                kind=kind,
                message=_with_post_echo(str(exc), record.id, shown_in=_shown),
                origin=record.origin,
                elapsed_s=time.time() - record.started_at,
            )
            # The typed kind the caller was just told is also what the ledger
            # records — never the exception class name (#709).
            settle = _fail_delegation_durably(
                record.id, JobFailure(kind=kind, message=str(exc)),
                announce_creator=True)

        if settle is None:
            return
        # The durable terminal is written either way: with no bus to announce
        # on there is still work whose outcome a restart must not ORPHAN.
        coro = (
            _settle_then_announce(record, complete, settle,
                                  birth=birth, raw_text=raw_text)
            if _bus is not None and complete is not None
            else settle
        )
        bg = loop.create_task(coro)
        _delegation_settle_tasks.add(bg)
        bg.add_done_callback(_delegation_settle_tasks.discard)
    # Registered BEFORE the callback is installed: a task that is already done
    # gets `_done` scheduled by `add_done_callback` itself, and it must be a
    # ripe producer from that instant.
    _delegation_callback_tasks.add(task)
    task.add_done_callback(_done)


async def wait_for_delegation_slot(chat_id: int, role: str, timeout: float) -> bool:
    """#1049: wait until a delegation to *role* from *chat_id*
    would not be refused ``busy`` by the per-scope cap — the scope computed by
    the one function ``_prelaunch`` uses. ``True`` with no limiter wired."""
    if _specialist_limiter is None:
        return True
    return await _specialist_limiter.wait_until_free(
        _delegation_scope({"chat_id": chat_id}, role, "sync"), timeout)


def _delegation_scope(origin: dict, agent_name: str, mode: str = "sync") -> str:
    """Role-wide for text engagements; calling-session scope for quick work.

    The voice channel's per-turn rate limiter (``channels/voice/channel.py``
    ``_resolve_scope_id`` / ``VoiceRateLimiter``) already keys off a
    ``scope_id`` — a per-session identifier — and threads that SAME value
    through as ``origin["chat_id"]`` for every voice-channel turn. Reusing
    it here makes spec §4.6's "one active MTG delegation per voice scope"
    literal: at most one delegation to a GIVEN specialist may be in flight
    per calling session at a time (a session may still run concurrent
    delegations to two DIFFERENT specialists — each gets its own scope key
    — bounded only by the global cap). Non-voice channels' chat_id serves
    the same "one calling session" role, so the same cap applies uniformly.

    Falls back to ``cid`` (the per-turn correlation id) when chat_id is
    empty/missing so an unscoped caller still gets its own bucket instead
    of colliding with every other unscoped caller under one ``"-"`` key.
    """
    if mode == "interactive" and origin.get("channel") != "voice":
        return f"{agent_name}:engagement"
    chat_id = str((origin or {}).get("chat_id") or "")
    if not chat_id:
        chat_id = str((origin or {}).get("cid") or "-")
    return f"{chat_id}:{agent_name}"


def _missing_required_plugins(required: list[str], plugins) -> list[str]:
    """A5 requires.plugins gate (spec §2.1, Task 5): the entries of
    `required` NOT present among `plugins`' RUNTIME identities
    (`plugin_registry.runtime_name` — an owned artifact's `manifest_name`,
    else its registry name). A `requires.plugins: ["mtg"]` declaration is
    satisfied by an owned `mtg.mtg` registry entry (runtime name "mtg"),
    never by matching the scoped registry name directly."""
    names = {plugin_registry.runtime_name(rp) for rp in plugins}
    return [p for p in required if p not in names]


def _with_late_assignments(admitted, fresh):
    """#1179: *admitted* (the requires gate's resolution) plus each plugin in
    *fresh* (a resolve taken after the launch's earlier awaits) that it lacks
    by registry name AND by runtime name — two entries sharing a runtime name
    would share one MCP namespace. Add-only: every admitted entry is kept as
    the gate's own object and nothing is re-checked, so no denial can follow
    the gate's admission. With
    nothing to add, *admitted* itself is returned."""
    names = {rp.name for rp in admitted.plugins}
    runtime = {plugin_registry.runtime_name(rp) for rp in admitted.plugins}
    added = []
    for rp in fresh.plugins:
        rt = plugin_registry.runtime_name(rp)
        if rp.name in names or rt in runtime:
            continue
        names.add(rp.name)
        runtime.add(rt)
        added.append(rp)
    if not added:
        return admitted
    import dataclasses
    return dataclasses.replace(admitted, plugins=[*admitted.plugins, *added])


def _display_name_for_role(role: str) -> str:
    """*role*'s persona display name, else *role* itself."""
    cfg = _agent_role_map.get(role)
    return (getattr(getattr(cfg, "character", None), "name", "") or "").strip() or role


def agent_display_names() -> dict[str, str] | None:
    """#436: role → persona display name for every CURRENTLY dispatchable
    agent, for the ``<delegates>`` renderer in ``agent.py``.

    Built from ``_agent_role_map``, which ``sync_agent_role_map`` rebuilds on
    every reload path and which ``_canonical_delegate_target`` resolves a
    caller-supplied display name against. Rendering the block from the SAME
    map is what stops the name Casa advertises from drifting away from the
    name Casa accepts — the two used to come from different objects with
    different lifetimes (an ``AgentRegistry`` snapshot pinned at the agent's
    construction vs. this map), so a per-role reload that renamed a persona
    desynchronised them until the caller was reconstructed.

    ``None`` when the map is EMPTY, so the caller keeps trusting its own
    construction-time registry rather than concluding that nothing is
    dispatchable. Note what this cannot express: an empty map after
    ``init_tools`` is indistinguishable from never having been initialized at
    all. Boot always registers at least the assistant, so in a running
    deployment the empty map is the uninitialized case; the ambiguity is real
    only for callers that initialize tools with no roles, which is a test
    shape rather than a deployment.
    """
    # `sync_agent_role_map` replaces the map wholesale — it builds `merged`
    # locally and rebinds at the end — and neither it nor this function
    # awaits, so on the single-threaded loop a caller sees a complete pre- or
    # post-reload map and never a half-built one.
    if not _agent_role_map:
        return None
    # Names come from `_display_name_for_role`, the SAME derivation the ACL's
    # alias resolution uses. Reimplementing the fallback here would recreate
    # the very divergence this function exists to close.
    return {role: _display_name_for_role(role) for role in _agent_role_map}


def _persona_name_for_role(role: str) -> str | None:
    """*role*'s persona display name, or None when it has no config.

    Distinct from :func:`_display_name_for_role`, whose role-id fallback is a
    RENDERING convenience. A declared delegate with no config has no persona
    name at all, and letting its role id act as one would let an absent
    delegate collide with a real delegate's display name and force a false
    ambiguity refusal (#433).
    """
    cfg = _agent_role_map.get(role)
    if cfg is None:
        return None
    return (getattr(getattr(cfg, "character", None), "name", "") or "").strip() or None


def persona_name(role: str | None = None) -> str:
    """#1409: the configured persona name of *role* (default: the assistant),
    for text a model or the operator reads. Never the default persona's name
    hard-coded: a renamed assistant must not be called something that does
    not exist. ``"the assistant"`` when no persona is configured (e.g. text
    built at import time, before any role is registered)."""
    return _persona_name_for_role(role or "assistant") or "the assistant"


def _declared_name_candidates(name: str, declared) -> list[str]:
    """#433: the DECLARED roles whose persona display name matches *name*,
    compared case- and whitespace-insensitively (matching
    ``AgentRegistry.name_to_role``).

    Scoped to *declared* by construction: the only values this can yield are
    roles the caller already declares, so alias resolution cannot widen the
    authorization boundary no matter what display names exist elsewhere in
    the deployment. That is why this does NOT use
    ``AgentRegistry.name_to_role`` — that map is global and its
    ``setdefault`` silently keeps the first binding on a collision, which is
    precisely the resolution an authz boundary must not perform silently.
    """
    # Total over arbitrary JSON: this is reachable from the tool handler,
    # whose `agent` argument is not guaranteed to be a string on paths that
    # skip the schema validator. A non-string resolves to nothing.
    if not isinstance(name, str):
        return []
    key = name.strip().lower()
    if not key:
        return []
    return sorted(r for r in declared
                  if (_persona_name_for_role(r) or "").lower() == key)


def _declared_delegates_for_origin(origin: dict) -> set[str]:
    """The roles the ORIGIN's running agent declares as delegates.

    Caller identity comes only from the trusted origin, keyed on
    ``execution_role`` so a delegated specialist is judged by its OWN
    delegates rather than its parent's.
    """
    caller_role = str((origin or {}).get("execution_role")
                      or (origin or {}).get("role", ""))
    caller_cfg = _agent_role_map.get(caller_role) if caller_role else None
    return {d.agent for d in (getattr(caller_cfg, "delegates", None) or [])}


def declares_delegate(caller_role: str, target_role: str) -> bool:
    """#1051: whether *caller_role*'s live declarations name *target_role* —
    the same membership the delegation ACL tests, read from the same map, so
    a setup courier is dispatched only once its delegation can pass."""
    return target_role in _declared_delegates_for_origin(
        {"execution_role": caller_role})


def _canonical_delegate_target(agent_name: str, origin: dict) -> str:
    """#433: *agent_name* as a role id when it is unambiguously a declared
    delegate's display name, else UNCHANGED.

    Resolution has to happen before ``validate_voice_handoff_static``, not
    only inside ``_prelaunch``'s ACL. That function runs FIRST — before any
    await, so a live voice turn reserves foreground ownership before work can
    race ahead — and applies its own exact-role-id membership test. With a
    display name it would take ``passthrough_not_declared``, hand back no
    reservation, and then the ACL would ACCEPT the alias and run the
    delegation on the ordinary sync path under the voice budget: the
    Concierge voice-handoff policy silently bypassed. That is precisely the
    #233/#224 production failure, which is why ``mode`` became an enum.

    Deliberately TOTAL and silent: every denial — undeclared, ambiguous —
    stays the ACL's to emit, in ``_prelaunch``, in the established order.
    This only ever rewrites a name it can resolve to exactly one declared
    role, so re-resolving there is a no-op (an exact role id short-circuits).
    """
    declared = _declared_delegates_for_origin(origin)
    if not declared or agent_name in declared:
        return agent_name
    candidates = _declared_name_candidates(agent_name, declared)
    return candidates[0] if len(candidates) == 1 else agent_name


# #434: `unresolved_env` is the first surface that shows manifest-controlled
# tokens to the MODEL rather than only to the operator log. The extractor
# accepts any `[A-Z_][A-Z0-9_]*`, and a malformed or hostile artifact can park
# an uppercase-alphanumeric literal there — an AWS access key id is exactly
# that shape — so Casa cannot tell a genuine variable name from one. It bounds
# what a single denial can carry rather than treating the tokens as trusted.
_MAX_REPORTED_ENV_VARS = 20
_MAX_REPORTED_ENV_NAME_LEN = 64


def _bounded_env_names(missing_vars) -> list[str]:
    """Manifest-sourced env names, bounded for a model-facing payload.

    Over-long tokens are dropped outright rather than truncated: a truncated
    credential is still a credential prefix, and a name that long is not a
    real environment variable anyway.
    """
    names = [v for v in missing_vars
             if isinstance(v, str) and len(v) <= _MAX_REPORTED_ENV_NAME_LEN]
    return names[:_MAX_REPORTED_ENV_VARS]


def _withhold_reasons(withheld, missing_plugins: list[str],
                      missing_tools: list[str]) -> list[dict]:
    """#434: the env-withhold causes RELEVANT to this denial, as JSON-safe
    dicts for the ``dependency_unavailable`` payload.

    *withheld* is ``withhold_env_unresolved``'s second element,
    ``[(resolved_plugin, missing_vars)]``. A plugin is relevant when it is
    one of the *missing_plugins*, or when it declares one of the
    *missing_tools* — a withheld plugin the delegate never required is not
    this denial's business and naming it would only mislead.

    The variable NAMES come from the plugin's own ``.mcp.json``, which is
    manifest content, not a secret value; the values are never read here.
    """
    reasons = []
    for rp, missing_vars in withheld or []:
        name = plugin_registry.runtime_name(rp)
        declares = declared_tools_for_resolution(
            plugin_registry.ResolutionResult(
                registry_valid=True, plugins=[rp], issues=[]))
        if name not in missing_plugins and not (declares & set(missing_tools)):
            continue
        reported = _bounded_env_names(missing_vars)
        reasons.append({
            "plugin": name,
            "reason": "env_unresolved",
            "unresolved_env": reported,
            # Built from the SAME bounded list, so the hint can never
            # reintroduce a token the payload deliberately dropped.
            "remediation": env_remediation_hint(reported),
        })
    return reasons


def _dependency_unavailable_message(agent_name: str,
                                    reasons: list[dict]) -> str:
    """#434: the one human-readable field a relaying model actually quotes.
    'launch deps unavailable' alone is what let a resident conclude the
    specialist was unwired when its secrets simply were not set."""
    if not reasons:
        return f"Agent {agent_name!r} launch deps unavailable."
    parts = [
        f"plugin {r['plugin']!r} is withheld because required env is "
        f"unresolved ({', '.join(r['unresolved_env'])}) — {r['remediation']}"
        for r in reasons
    ]
    return (f"Agent {agent_name!r} launch deps unavailable: "
            + "; ".join(parts) + ".")


def _log_delegation_denial(caller_role: str, agent_name: str,
                           kind: str) -> None:
    """#433: make an ACL denial observable, which it previously was not — an
    operator could not tell how often a delegation was refused over a naming
    mismatch, on any deployment.

    Deliberately a LOG LINE and not ``_specialist_telemetry.record_denial``.
    This denial is PRE-AUTHORIZATION and ``agent_name`` is caller-supplied, so
    keying per-role counters on it would let an unauthorized caller move
    another role's telemetry and grow the stats dict without bound — the
    invariant pinned by ``test_input_bounds_run_after_acl`` and
    ``test_missing_origin_oversized_input_is_not_declared``. The requires
    gate's ``record_denial`` is fine by contrast: it runs AFTER the ACL, on a
    target the caller demonstrably declares.

    Enumerated facts only: the caller role comes from the trusted origin, and
    ``_known_role`` collapses an unregistered target to ``<other>`` rather
    than echoing whatever the model actually emitted.
    """
    logger.warning(
        "delegation_denied caller=%s target=%s kind=%s",
        _known_role(caller_role), _known_role(agent_name), kind,
    )


async def _requires_gate(agent_name: str, cfg) -> tuple[Any, "dict | None"]:
    """The A5 requires gate, shared by ``_prelaunch`` and ``hand_off``:
    ``(resolution, None)`` — ``resolution`` ``None`` when ``cfg`` declares no
    requirements — or ``(None, error_result)``."""
    # A5 requires gate: a delegated agent that declares `requires:` refuses
    # to launch (typed `dependency_unavailable`) unless its required
    # plugins/tools are ACTUALLY resolved for its own tier:role target —
    # never "assume model memory has the tool". Skipped entirely (resolution
    # stays None) when `cfg.requires` is empty, so a delegate with no
    # requires: block behaves exactly as before Task 5. `grants_for_resolution`
    # is SERVER-level (`mcp__plugin_mtg_mtg`), so a tool-level requirement is
    # checked against `declared_tools_for_resolution` (manifest
    # `casa.provides_tools`) AND, for the server prefix, the actual grant —
    # both must hold for the tool to be genuinely usable.
    resolution = None
    req = getattr(cfg, "requires", None)
    if req is not None and (req.plugins or req.tools):
        tier = (_agent_registry.tier_for_role(agent_name)
                if _agent_registry is not None else None) or "specialist"
        resolution = await asyncio.to_thread(
            plugin_registry.resolve_for, f"{tier}:{agent_name}")
        # #424 r4 (Sol r4-1 / Terra r4-1): apply the env withhold BEFORE the
        # missing checks — a required plugin whose secrets are unresolved
        # would be withheld at the session build, so admitting it here
        # launches a delegation without its declared dependency. Filtering
        # first makes it land in missing_plugins/missing_tools (typed
        # dependency_unavailable), and the filtered resolution flows to the
        # record + options builder (A5/H7b: one resolve, one filter).
        # #434: BIND the second element. It is the only structured home of
        # WHY a plugin was withheld — `[(rp, missing_vars)]` — and discarding
        # it into `_` is what left the caller able to say only "the plugin
        # isn't available" while the cause sat spelled out in a WARNING the
        # model cannot read. The resident could not learn that three
        # environment variables needed wiring.
        from plugin_grants import withhold_env_unresolved
        resolution, withheld = await asyncio.to_thread(
            withhold_env_unresolved, resolution,
            context=f"delegated {agent_name} requires gate")
        declared = declared_tools_for_resolution(resolution)
        servers = set(grants_for_resolution(resolution))  # server actually attached
        missing_plugins = _missing_required_plugins(
            req.plugins, resolution.plugins)
        missing_tools = [
            t for t in req.tools
            if t not in declared or t.rsplit("__", 1)[0] not in servers
        ]
        if missing_plugins or missing_tools or not resolution.registry_valid:
            reasons = _withhold_reasons(withheld, missing_plugins, missing_tools)
            # Safe here, unlike the ACL denial (#433): this gate runs AFTER
            # the ACL, so `agent_name` is a target the caller demonstrably
            # declares — not an unauthenticated caller-supplied string. Same
            # stage as the `busy` denial recorded immediately below.
            if _specialist_telemetry is not None:
                _specialist_telemetry.record_denial(
                    agent_name, kind="dependency_unavailable")
            return None, _result({
                "status": "error", "kind": "dependency_unavailable",
                "agent": agent_name,
                "missing_plugins": missing_plugins,
                "missing_tools": missing_tools,
                "registry_valid": resolution.registry_valid,
                # Always present, empty when no cause is known, so a consumer
                # never has to tell "no reasons" from "payload predates #434".
                "unavailable_reasons": reasons,
                "message": _dependency_unavailable_message(agent_name, reasons),
            })

    return resolution, None


async def _prelaunch(
    agent_name: str, origin: dict, mode: str,
    task_text: str = "", context_text: str = "",
    internal_context: bool = False, skip_permit: bool = False,
) -> tuple[str, Any, Any, "specialist_limits.Permit | None", dict | None]:
    """The single unified prelaunch pipeline for delegate_to_agent (spec A4)
    and for a specialist-hosted background job's launch, whose mode here is
    ``"interactive"`` (the engagement the job runs as).

    Runs EVERY pre-launch gate, in this order, so no gate is bypassable by
    another and no side effect (engagement/delegation record, task,
    driver start, progress emission) can precede a clean return:

        ACL (Task 1) -> not-initialized -> input-size bounds (Task 6) ->
        depth cap -> mode gate (voice; setup turns sync-only, #1015) ->
        target resolution -> requires (Task 5) ->
        concurrency (Task 6, spec §4.6; one open engagement per specialist
        for a job) -> progress -> launch

    The input-size bounds run AFTER the ACL (so an unauthorized caller is
    denied ``delegation_not_declared`` regardless of payload size, and no
    telemetry is ever keyed on a caller-supplied target name pre-auth) but
    BEFORE target resolution / concurrency / progress (so an oversized
    payload never resolves plugins, acquires a slot, or speaks).

    The mode gate deliberately precedes target resolution. On voice, a job
    launch is always denied as ``mode_unsupported_on_voice``; async without a
    trusted capable route is denied as ``background_delivery_unavailable``.
    Those denials stay independent of what (or whether) the target resolves
    to.

    Returns ``(cfg, resolution, permit, error)``:
    - ``error`` is a terminal tool-result dict the caller returns
      immediately when a gate denies; ``cfg``/``resolution``/``permit``
      are ``None``. A concurrency (``busy``) denial in particular has NO
      side effects yet — it is checked before progress/record/task, same
      as every earlier gate.
    - On success ``error`` is ``None``, ``cfg`` is the resolved target
      config, ``resolution`` is the plugin ResolutionResult the requires
      gate (spec A5) resolved for ``agent_name`` — ``None`` when
      ``cfg.requires`` is empty (gate skipped, no resolve performed) —
      and ``permit`` is the concurrency slot acquired for this delegation
      (``None`` only when no ``SpecialistLimiter`` is wired at all — see
      ``init_tools``). ``permit`` is ALWAYS acquired when a limiter is
      wired, independent of whether ``resolution`` is ``None`` — a permit
      does not depend on the requires gate having run.
      Callers with a non-``None`` resolution MUST reuse it verbatim
      (``_build_specialist_options(cfg, resolution=resolution)``) rather
      than re-resolving, so the gate's decision and what actually launches
      never drift. The one exception is a job's engagement launch,
      whose session is built after the launch's awaits: it keeps every plugin
      this resolution admitted and may only ADD plugins assigned since
      (#1179, ``_with_late_assignments``) — never drop or re-check one. The
      caller (``delegate_to_agent``) owns the permit's lifetime from here.
      Legacy sync/non-voice async tasks use ``_permit_release_callback``;
      accepted async voice jobs transfer the permit to ``JobRegistry`` at
      ``bind_task``; a job's launch hands it to ``rec.permit`` for release by
      an ``EngagementRegistry`` terminal transition (or
      ``_finalize_engagement``).
    """
    channel = str((origin or {}).get("channel", ""))

    # A1 delegation ACL — the caller's DECLARED delegates are an
    # authorization boundary, enforced FIRST so a missing, unknown, or
    # undeclared caller is denied uniformly with one kind and cannot
    # distinguish existing agents. Caller identity comes ONLY from the
    # trusted origin, never from tool args. Key on execution_role — the
    # agent actually RUNNING this turn — so a delegated specialist is
    # judged by its OWN delegates, not its parent's: on a direct turn
    # execution_role == role (agent.py sets both to self.config.role); on
    # a delegated turn _run_delegated_agent overwrites execution_role with
    # the delegate's role while `role` stays the delegator (read by the
    # v0.76 provenance/authz system — leave it). The InCasaDriver
    # interactive-executor inheritance path (in_casa_driver.py:113) is a
    # tracked residual — unreachable today as no executor grants
    # delegate_to_agent.
    caller_role = str((origin or {}).get("execution_role")
                      or (origin or {}).get("role", ""))
    caller_cfg = _agent_role_map.get(caller_role) if caller_role else None
    # Same derivation the handler's pre-handoff canonicalization uses, so the
    # two can never disagree about who the caller's delegates are (#433).
    declared = _declared_delegates_for_origin(origin)
    # The gate is TOTAL, independent of any caller having normalized first:
    # the membership test below needs a HASHABLE value, so a non-string
    # target must become one that is simply never declared rather than
    # raising TypeError out of the authorization boundary.
    if not isinstance(agent_name, str):
        agent_name = ""
    # S7a: a specialist starting its OWN job targets itself — no delegation,
    # so neither the ACL nor the depth cap applies; every later gate does.
    self_host = bool(_self_host_prelaunch.get() and caller_cfg is not None
                     and agent_name == caller_role)

    # #433: accept a delegate's PERSONA DISPLAY NAME as well as its role id.
    # Casa advertises both to the model — `_render_delegates_block` names the
    # persona and the shipped assistant prompt used to model delegation with
    # it — and an exact-role-id-only ACL refused the name as a WIRING fault,
    # which sent the resident and the configurator into a repair loop that
    # could not converge (the wiring was already correct).
    #
    # Resolution is scoped to `declared`: the candidate set is built FROM the
    # caller's own declarations, so every value it can yield is already
    # inside this ACL and no display-name collision elsewhere can widen the
    # boundary. An exact role id is matched FIRST, so a delegate whose
    # display name happens to be another delegate's role id cannot shadow it.
    # A name matching two declared delegates is REFUSED, not guessed —
    # picking one silently is the failure mode this fix exists to remove.
    if not self_host and caller_cfg is not None and agent_name not in declared:
        candidates = _declared_name_candidates(agent_name, declared)
        if len(candidates) > 1:
            _log_delegation_denial(caller_role, agent_name, "delegation_ambiguous_name")
            return None, None, None, None, _result({
                "status": "error", "kind": "delegation_ambiguous_name",
                "candidates": candidates,
                "message": (
                    f"{agent_name!r} is the display name of more than one "
                    f"delegate ({', '.join(candidates)}) — pass the role id "
                    f"instead."),
            })
        if candidates:
            # Canonical from here on: every downstream record, telemetry key
            # and launch target uses the role id, never the display name.
            agent_name = candidates[0]

    if not self_host and (caller_cfg is None or agent_name not in declared):
        _log_delegation_denial(caller_role, agent_name, "delegation_not_declared")
        return None, None, None, None, _result({
            "status": "error", "kind": "delegation_not_declared",
            # The caller's OWN declarations, minus any that would fail at the
            # very NEXT gate — advertising one of those sends the model to a
            # second dead end, which is the failure this whole change exists
            # to remove.
            #
            # Derived from `_agent_role_map`, the map target resolution itself
            # reads, at the point of use — one source of truth, and it is the
            # consumer's own. NOT from `_agent_registry`, which is a separate
            # store answering a different question (tier); reading membership
            # from it would let the two disagree about who is callable.
            # (Under-advertising is safe — resolution has a registry fallback,
            # so a listed role always resolves but an unlisted one may too.)
            "declared_delegates": [
                {"role": r, "name": _display_name_for_role(r)}
                for r in sorted(declared) if r in _agent_role_map
            ],
            "message": (f"Agent {caller_role or '(unknown)'!r} does not "
                        f"declare {agent_name!r} as a delegate.")})

    if _specialist_registry is None:
        return None, None, None, None, _result({
            "status": "error",
            "kind": "not_initialized",
            "message": "specialist registry not initialized",
        })

    # Field TYPES, immediately before the size bounds that assume them and in
    # the same post-ACL position for the same reason. `args` can carry any
    # JSON on the paths that don't run the schema validator first — the
    # condition the `mode` and `agent` coercions already handle. Untyped,
    # `len(task_text)` raised TypeError straight out of the gate for `7`, and
    # `["x"]` was WORSE: it has a len(), so it sailed through the bounds check
    # and reached the launch as a "task". Denied, never coerced — there is no
    # sensible default for a task, and silently substituting one would run a
    # delegation the caller never asked for.
    for _field, _value in (("task", task_text), ("context", context_text)):
        if not isinstance(_value, str):
            return None, None, None, None, _result({
                "status": "error", "kind": "invalid_argument",
                "field": _field, "agent": agent_name,
                "message": (f"{_field} must be a string. The tool schema "
                            f"should have rejected this."),
            })

    # Task 6 input bounds (spec §4.6): reject an oversized task/context —
    # AFTER the ACL (an unauthorized caller is already denied above, so we
    # never leak `input_too_large` to an unknown caller, and any denial
    # telemetry keyed on `agent_name` is now safe: the caller is authorized
    # and `agent_name` is one of its DECLARED delegates) but BEFORE target
    # resolution / concurrency / progress (an oversized payload never
    # resolves plugins, acquires a slot, or speaks).
    if len(task_text) > specialist_limits._MAX_TASK_CHARS:
        if _specialist_telemetry is not None:
            _specialist_telemetry.record_denial(agent_name, kind="input_too_large")
        return None, None, None, None, _result({
            "status": "error", "kind": "input_too_large", "field": "task",
            "agent": agent_name, "length": len(task_text),
            "limit": specialist_limits._MAX_TASK_CHARS,
            "message": (
                f"task ({len(task_text)} chars) exceeds the "
                f"{specialist_limits._MAX_TASK_CHARS}-char limit."
            ),
        })
    # #324: `internal_context` marks a context Casa built itself from
    # already-bounded stored fields (the continue_voice_job envelope) — the
    # caller-facing bound does not apply to it (JSON escaping can expand a
    # legitimately-bounded envelope past any summed-caps arithmetic; the
    # builder bounds its own components instead). Caller-supplied context is
    # always bounded.
    if not internal_context and (
            len(context_text) > specialist_limits._MAX_CONTEXT_CHARS):
        if _specialist_telemetry is not None:
            _specialist_telemetry.record_denial(agent_name, kind="input_too_large")
        return None, None, None, None, _result({
            "status": "error", "kind": "input_too_large", "field": "context",
            "agent": agent_name, "length": len(context_text),
            "limit": specialist_limits._MAX_CONTEXT_CHARS,
            "message": (
                f"context ({len(context_text)} chars) exceeds the "
                f"{specialist_limits._MAX_CONTEXT_CHARS}-char limit."
            ),
        })

    # Depth cap: prevent delegation chains beyond depth=1. #283: read the
    # EFFECTIVE depth — ambient origin, else the bound engagement record's
    # origin (interactive children are depth-stamped on the record, and
    # in_casa turns inherit the parent task's origin_var, not the record's).
    current_depth = _effective_delegation_depth(origin)
    if not self_host and current_depth >= _MAX_DELEGATION_DEPTH:
        return None, None, None, None, _result({
            "status": "error",
            "kind": "delegation_depth_exceeded",
            "message": (
                f"Delegation depth {current_depth} exceeds cap "
                f"{_MAX_DELEGATION_DEPTH}; cannot chain further."
            ),
        })

    # A4 mode gate — BEFORE target resolution. A job's engagement needs a
    # text channel (a scheduled fire on a voice trigger reaches here; the
    # start_job tool refuses voice itself). Async is accepted only when ingress
    # bound a current acknowledged delivery route on the server-owned WS
    # connection; SSE and route-shaped tool/context spoofing fail here before
    # requires/concurrency/progress or any launch side effect.
    if channel == "voice" and mode == "interactive":
        return None, None, None, None, _result({
            "status": "error",
            "kind": "mode_unsupported_on_voice",
            "message": (
                "A background job cannot start on the voice channel — it "
                "needs a text channel."
            ),
        })
    if (channel == "voice" and mode == "async"
            and not deferred_delivery_available(origin)):
        return None, None, None, None, _background_delivery_unavailable_result(origin)
    # #1015: a Casa-dispatched plugin-setup turn may delegate only in sync
    # mode. An async delegation or a job launch returns a non-error "pending"
    # result that the courier's outcome report would count as a delegation
    # that went through, while a later turn can never carry the setup
    # identity; an error result here, BEFORE any record exists,
    # returns the courier row to pending under its bounded budget instead of
    # consuming it.
    from provenance import CASA_PLUGIN_TURN_TARGETS
    if (mode != "sync" and (origin or {}).get("synthetic")
            in CASA_PLUGIN_TURN_TARGETS):
        return None, None, None, None, _result({
            "status": "error",
            "kind": "mode_unsupported_on_setup_turn",
            "message": (
                f"mode={mode!r} is not supported on a plugin-setup turn — "
                "delegate the setup tool with mode='sync'."
            ),
        })

    # Resolve target. Look in the merged role map (residents + specialists)
    # first; fall back to the specialist registry for back-compat with any
    # caller still relying on the old wiring.
    cfg = _agent_role_map.get(agent_name) or (
        _specialist_registry.get(agent_name)
        if _specialist_registry is not None else None
    )
    if cfg is None:
        return None, None, None, None, _result({
            "status": "error",
            "kind": "unknown_agent",
            "message": f"No enabled agent named {agent_name!r}",
        })

    resolution, _requires_error = await _requires_gate(agent_name, cfg)
    if _requires_error is not None:
        return None, None, None, None, _requires_error

    # Task 6 concurrency gate (spec §4.6): AFTER requires, BEFORE progress —
    # a denied gate must never speak the "checking" line, and a delegation
    # that fails the requires gate must never occupy a concurrency slot.
    # Acquired regardless of whether `resolution` is None (a permit does
    # NOT depend on the requires gate having run — see docstring). No
    # limiter wired (`_specialist_limiter is None`) means no cap: `permit`
    # stays None and every downstream release is a guarded no-op.
    if mode == "interactive" and _engagement_registry is not None:
        for rec in _engagement_registry.active_and_idle():
            if rec.kind == "specialist" and rec.role_or_type == agent_name:
                title = (rec.origin.get("job") or {}).get("title") or rec.task[:80]
                return None, None, None, None, _result({
                    "status": "error", "kind": "engagement_busy",
                    "agent": agent_name, "engagement_id": rec.id,
                    "message": f"{_display_name_for_role(agent_name)} already has an open "
                               f"engagement: {title}. Wait for it to finish, or cancel it "
                               "with the Cancel button on its start line or by asking me.",
                })
    permit = None
    # The permit is taken here, before the agent's lane: a delegation refused
    # busy creates no task and posts no start line. A caller that takes its
    # own permit says so with ``skip_permit``.
    if _specialist_limiter is not None and not skip_permit:
        scope = _delegation_scope(origin, agent_name, mode)
        permit = _specialist_limiter.try_acquire(scope)
        if permit is None:
            if _specialist_telemetry is not None:
                _specialist_telemetry.record_denial(agent_name, kind="busy")
            return None, None, None, None, _result({
                "status": "error",
                "kind": "busy",
                "agent": agent_name,
                "message": (
                    f"Agent {agent_name!r} is already at its concurrency "
                    "cap — try again shortly."
                ),
            })

    # A4 progress: speak a deterministic "still working" block AFTER every
    # gate above has passed, immediately before the launch side effects —
    # a denied gate must never speak a misleading "checking" line. The
    # sink itself (channels/voice/channel.py) enforces exactly-once-per-
    # turn and suppresses this if the turn already spoke real content.
    #
    # Task 6 (spec §4.6): the progress sink is the ONE await between
    # acquiring the permit above and returning it to the caller. If THIS
    # coroutine is cancelled here (voice barge-in), the caller never
    # receives `permit` to release it — so guard the await and release on
    # any BaseException (CancelledError included) before re-raising. The
    # sink's own errors are still swallowed (best-effort) and do NOT
    # release the permit — a launch still proceeds.
    try:
        if (channel == "voice"
                and not requires_voice_handoff(origin, caller_role)):
            sink = (origin or {}).get("_progress_sink")
            if callable(sink):
                try:
                    await sink("One moment — checking.")
                except Exception:  # noqa: BLE001 — progress is best-effort
                    logger.warning(
                        "voice progress sink raised for delegate_to_agent(%s)",
                        agent_name, exc_info=True,
                    )
    except BaseException:
        if permit is not None:
            permit.release()
        raise

    return agent_name, cfg, resolution, permit, None


def _voice_wait_from_deadline(raw_deadline: Any, loop) -> float | None:
    """A4: remaining voice budget, recomputed from the ABSOLUTE monotonic
    deadline (``origin["voice_deadline"]``). Returns the wait in seconds,
    or ``None`` when the budget is exhausted, missing, or NON-FINITE —
    the caller then fails closed with ``deadline_exceeded``.

    Must be called at each decision point (pre-register AND post-register)
    because the value goes stale: ``loop.time()`` advances as the handler
    awaits (register_delegation's tombstone lock + I/O), so a wait computed
    before an await can be obsolete after it. A non-finite deadline/wait is
    rejected explicitly — ``min(nan, 60)`` is unreliable and
    ``asyncio.wait(timeout=nan)`` never expires (hang), so NaN must fail
    closed here rather than silently disable the timeout."""
    if raw_deadline is None:
        return None
    try:
        deadline = float(raw_deadline)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(deadline):
        return None
    wait_s = min(
        deadline - loop.time() - _VOICE_FALLBACK_RESERVE_S,
        _SYNC_WAIT_TIMEOUT_S,
    )
    if not math.isfinite(wait_s) or wait_s <= 0:
        return None
    return wait_s


def _deadline_exceeded_result(delegation_id: str, agent_name: str) -> dict:
    """A4: the typed ``deadline_exceeded`` tool result. Shared by the
    pre-launch expiry short-circuit (no task created) and the post-wait
    teardown path (``_voice_deadline_exceeded``)."""
    return _result({
        "status": "error",
        "delegation_id": delegation_id,
        "agent": agent_name,
        "kind": "deadline_exceeded",
        "message": "Voice turn budget exceeded before the specialist finished.",
    })


async def _voice_deadline_exceeded(
    task: asyncio.Task, delegation_id: str, agent_name: str,
) -> dict:
    """A4: voice turn-budget expiry AFTER the task was launched. Cancel it,
    wait a bounded amount for it to actually unwind, and report a typed
    error. Voice never degrades to `pending` — there is no channel to
    deliver a later completion notification on."""
    # Attach the exception-retrieval callback UNCONDITIONALLY, before the
    # cancel — a task that catches CancelledError and raises within the
    # teardown bound lands in `done` (not `pending`) with an exception that
    # would otherwise never be retrieved (asyncio logs "exception was never
    # retrieved"). The callback fires regardless of which set it ends in.
    task.add_done_callback(_retrieve_late_task_exception)
    # #671: the voice turn budget expired — a cause this function knows
    # statically. Latched BEFORE the cancel so the deferral guard cannot claim a
    # deadline expiry that happens to land inside a graceful stop; the caller
    # gets the typed deadline result either way and must not also be told the
    # work was lost to a restart.
    _specialist_registry.job_registry.note_cancel_cause(
        delegation_id, "voice_deadline")
    task.cancel()
    await asyncio.wait({task}, timeout=_VOICE_TEARDOWN_BOUND_S)
    # #321: a snapshot-write failure must not escape — the caller is owed the
    # documented typed result either way, and the still-RUNNING record is
    # handed to the registry-owned cancel reconciliation instead of waiting
    # for restart recovery.
    try:
        await _specialist_registry.cancel_delegation(delegation_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — never render persistence content
        logger.error(
            "Delegation %s cancel persistence failed; scheduling "
            "reconciliation", delegation_id[:8],
        )
        try:
            _specialist_registry.job_registry.schedule_cancel_reconciliation(
                delegation_id)
        except Exception:  # noqa: BLE001 — restart recovery remains authoritative
            logger.error(
                "Delegation %s cancel reconciliation scheduling failed; "
                "restart recovery required", delegation_id[:8],
            )
    logger.info(
        "Delegation %s → %s exceeded the voice turn budget — cancelled",
        delegation_id[:8], agent_name,
    )
    return _deadline_exceeded_result(delegation_id, agent_name)


def _retrieve_late_task_exception(t: asyncio.Task) -> None:
    """Done-callback for a cancelled specialist task in the voice teardown
    path — retrieves ``.exception()`` so asyncio never logs it as
    "never retrieved", whether the task finished within the teardown bound
    (landing in ``done`` with an exception it caught then re-raised) or
    survived past it (landing in ``pending`` and finishing later)."""
    if t.cancelled():
        return
    exc = t.exception()
    if exc is not None:
        logger.warning(
            "voice deadline teardown: specialist task raised after "
            "cancellation had already been requested",
        )


# S-2 (block-S live finding 2026-07-15): wall-clock ceiling on every
# launched sync/async delegation task. Async mode (and sync degraded to
# pending at the 60s wait) previously had NO time bound at all — a runaway
# specialist (live: >12 min repetition loop, delegation 07bfeb0b) held its
# per-scope Permit + a global slot until someone killed the CLI subprocess.
# 10× the sync degrade wait: minutes-scale so legitimate long specialist
# turns (the EMIT:2500 sibling run finished after producing 127k chars)
# still complete, but far below the observed indefinite hang. Deliberately
# a module constant, not an add-on option — voice has its own (configurable)
# budget, and the sync/interactive paths have their own bounds; this is a
# runaway backstop, not a tuning knob.
_DELEGATION_CEILING_S: float = 10 * _SYNC_WAIT_TIMEOUT_S
# Bounded wait for the cancelled inner task to unwind (SDK client __aexit__
# terminates the CLI subprocess — its own terminate→kill escalation runs in
# seconds) before the failure is reported. 30s comfortably covers a healthy
# teardown while never letting a wedged one postpone the typed failure
# indefinitely; giving up early is LOUD (see the warning below) because the
# permit then frees while the zombie is still unwinding — a deliberate,
# bounded overshoot of the concurrency cap in the double-failure case only.
_CEILING_TEARDOWN_BOUND_S: float = 30.0

# How often a paused ceiling re-reads its clock while a hand-off it waits on runs.
_CEILING_PAUSE_POLL_S: float = 5.0


class _CeilingClock:
    """One bounded run's ceiling, with the time it spends waiting on its own
    hand-offs left out (browser executor §5.4): a requester waiting on the
    specialist it handed work to is not running away. Pauses nest — parallel
    hand-offs from one run each pause and resume it once."""

    def __init__(self, ceiling: float, *, now=time.monotonic) -> None:
        self.ceiling = ceiling
        self._now = now
        self._started = now()
        self._paused_total = 0.0
        self._depth = 0
        self._paused_at = 0.0

    def pause(self) -> None:
        if self._depth == 0:
            self._paused_at = self._now()
        self._depth += 1

    def resume(self) -> None:
        if self._depth <= 0:
            return
        self._depth -= 1
        if self._depth == 0:
            self._paused_total += self._now() - self._paused_at

    def left(self) -> "float | None":
        """Seconds of running time left, or ``None`` while paused."""
        if self._depth > 0:
            return None
        return self.ceiling - (self._now() - self._started - self._paused_total)


# The ceiling clock of the bounded run a tool handler runs inside — set around
# the inner task's creation, as ``_bounded_tree`` is, so ``hand_off`` pauses
# its REQUESTER's clock and a run nested inside gets its own.
_ceiling_clock: ContextVar["_CeilingClock | None"] = ContextVar(
    "_ceiling_clock", default=None)


@contextmanager
def _ceiling_paused():
    clock = _ceiling_clock.get()
    if clock is None:
        yield
        return
    clock.pause()
    try:
        yield
    finally:
        clock.resume()


# The longest one install-like call keeps its run's ceiling clock paused.
# Fetch and install time does not count toward the ceiling, but it is not
# unbounded: a call still running past this — say, waiting on the plugin lock
# behind a stalled mutation — counts again, so the run and its lease still end.
_OFF_THE_CEILING_MAX_S: float = 30 * 60.0


def _off_the_ceiling(handler):
    """A tool whose call fetches or installs
    (a repository fetch, npm or venv setup, system requirements) runs with its
    run's ceiling clock paused, for at most ``_OFF_THE_CEILING_MAX_S`` per
    call; past that the clock runs again while the call goes on. The ceiling
    stays the backstop for a run that loops. The body is read off
    ``__wrapped__`` at call time."""
    @functools.wraps(handler)
    async def _paused(args: dict) -> dict:
        clock = _ceiling_clock.get()
        if clock is None:
            return await _paused.__wrapped__(args)
        clock.pause()
        resumed = False

        def _resume() -> None:
            nonlocal resumed
            if not resumed:
                resumed = True
                clock.resume()

        timer = asyncio.get_running_loop().call_later(_OFF_THE_CEILING_MAX_S, _resume)
        try:
            return await _paused.__wrapped__(args)
        finally:
            timer.cancel()
            _resume()
    return _paused


class DelegationCeilingExceeded(asyncio.TimeoutError):
    """A delegated turn exceeded the wall-clock ceiling and was cancelled.

    Subclasses ``asyncio.TimeoutError`` so ``_classify_error`` yields the
    typed kind ``"timeout"`` — the task then FAILS (rather than ending
    cancelled), which routes ``_attach_completion_callback`` into its
    exception branch: ``fail_delegation`` + a ``DelegationComplete`` error
    NOTIFICATION to the engager — the exact path the live kill-subprocess
    probe proved releases the permit and notifies correctly. A bare
    ``task.cancel()`` would instead hit the cancelled-branch, which posts
    NO notification."""

    def __init__(self, message: str, tool_calls: dict[str, int]):
        self.tool_calls = tool_calls
        super().__init__(_failure_message(message, tool_calls))


async def _run_delegated_agent_bounded(
    cfg, task_text: str, context_text: str, resolution=None,
    output_format=None,
) -> DelegatedOutput:
    """Run ``_run_delegated_agent`` under ``_DELEGATION_CEILING_S``.

    Transparent wrapper on the happy path (result and exceptions propagate
    unchanged, so error classification is untouched). On ceiling expiry the
    inner task is cancelled, given ``_CEILING_TEARDOWN_BOUND_S`` to unwind
    (the SDK client's ``__aexit__`` terminates the CLI subprocess), and
    ``DelegationCeilingExceeded`` is raised. On cancellation of the OUTER
    task (voice deadline teardown / caller cancel) the inner task is
    cancelled and awaited to completion before re-raising, so the permit —
    released by the outer task's done-callback — is never freed while the
    delegated work is still unwinding (preserves the pre-S-2 release
    timing exactly).

    Either way it can return while the inner task is still unwinding past
    the bound; a use of an agent's lane learns of that task through
    ``_lane_run_sink`` and keeps the lane refused until it ends (#1197).

    #1212: either way, too, the processes the run's CLI started are Casa's to
    end, not the SDK close's (which signals the CLI only): the run's
    ``ProcessTree`` is re-walked before the cancel, and after the teardown
    every process it pinned is signalled and confirmed gone — a cleanup that
    a re-cancel of this task cannot interrupt (``_reap_bounded_tree``).

    Both bounds are read off the module at call time so tests can
    monkeypatch them."""
    import pinned_run
    # One configurator run at a time, from every
    # origin, taken before the ceiling clock and before anything of the run
    # (its options build reads /config) — waiting for it never counts.
    lease = await _configurator_lease(cfg, task_text)
    try:
        return await _bounded_run(cfg, task_text, context_text, resolution,
                                  output_format, pinned_run, lease)
    finally:
        if lease is not None and not lease.watched:
            lease.release()          # nothing of the run started


async def _configurator_lease(cfg, task_text: str):
    """The configurator lease for a run of *cfg*, or ``None`` for any other role.
    A run Casa started after an approval starts only while that approval is
    still approved and current; the approval itself is used by the governed
    call's attempt, not here."""
    import approval_records
    import configurator_run
    if not configurator_run.is_configurator(cfg):
        return None
    origin = _snapshot_origin() or {}
    run_key = str(origin.get("_delegation_id") or _delegation_quota_key.get() or "")
    lease = await configurator_run.acquire(run_key=run_key, via="delegation", task=task_text)
    approval = origin.get(approval_records.APPROVAL_KEY)
    if isinstance(approval, dict):
        gen = approval.get("generation")
        verdict = (approval_records.startable(str(approval.get("rid") or ""), gen)
                   if isinstance(gen, int) and not isinstance(gen, bool) else "unknown")
        if verdict != "ok":
            lease.release()                   # nothing of the run started
            raise approval_records.ApprovalNotCurrent(verdict)
    return lease


async def _bounded_run(cfg, task_text, context_text, resolution, output_format,
                       pinned_run, lease) -> DelegatedOutput:
    tool_counts: dict[str, int] = {}
    sink = _lane_run_sink.get()
    role = _known_role(getattr(cfg, "role", None))
    tree = pinned_run.ProcessTree(run_id=f"bounded-{uuid.uuid4().hex[:12]}")
    # #1197: the inner task is created with no sink, so a run nested inside it
    # never reports into the lane's; #1212: and with this run's tree
    sink_tok = _lane_run_sink.set(None)
    tree_tok = _bounded_tree.set(tree)
    ceiling = _DELEGATION_CEILING_S
    if not math.isfinite(ceiling) or ceiling <= 0:
        ceiling = 600.0  # fail closed to the shipped default, never hang
    clock = _CeilingClock(ceiling)
    clock_tok = _ceiling_clock.set(clock)
    try:
        inner = asyncio.create_task(
            _run_delegated_agent(cfg, task_text, context_text,
                                 resolution=resolution,
                                 output_format=output_format,
                                 tool_counts=tool_counts))
    finally:
        _ceiling_clock.reset(clock_tok)
        _bounded_tree.reset(tree_tok)
        _lane_run_sink.reset(sink_tok)
    if lease is not None:
        import configurator_run
        configurator_run.release_when_done(lease, inner)
    if sink is not None:
        sink.append(inner)
    try:
        return await _bounded_wait(inner, tree, clock, tool_counts, role)
    finally:
        if not tree.reaping:
            tree.close_fds()


async def _wait_under_ceiling(inner: asyncio.Task,
                              clock: "_CeilingClock") -> "tuple[set, set]":
    """Wait for *inner* until its clock runs out; time the clock spends paused
    (a hand-off in flight) does not count. Returns ``asyncio.wait``'s pair."""
    while True:
        left = clock.left()
        timeout = _CEILING_PAUSE_POLL_S if left is None else max(left, 0.0)
        done, pending = await asyncio.wait({inner}, timeout=timeout)
        if done:
            return done, pending
        left = clock.left()
        if left is not None and left <= 0:
            return done, pending


async def _bounded_wait(inner: asyncio.Task, tree: Any, clock: "_CeilingClock",
                        tool_counts: dict[str, int], role: str) -> DelegatedOutput:
    """``_run_delegated_agent_bounded``'s wait, cancel and teardown."""
    ceiling = clock.ceiling
    try:
        done, pending = await _wait_under_ceiling(inner, clock)
    except asyncio.CancelledError:
        # The bounded runner may be abandoned by a voice deadline or caller
        # while the delegated coroutine turns cancellation into an exception.
        # Retrieve that exception without rendering it so private task/result
        # content cannot reach the event loop's exception handler.
        inner.add_done_callback(_retrieve_late_task_exception)
        tree.begin_termination()          # #1212: the walk while the CLI lives
        inner.cancel()
        # Await the actual unwind so the permit (released by the OUTER
        # task's done-callback) never frees while the delegated work is
        # still running — matching the pre-S-2 release timing, where the
        # outer task WAS the delegated coroutine. Tolerates overlapping
        # re-cancellation (Codex review finding 2) up to the teardown
        # bound; the voice teardown's own asyncio.wait bounds how long
        # the CALLER waits regardless.
        if not await _await_task_teardown(inner, _CEILING_TEARDOWN_BOUND_S):
            logger.warning(
                "delegation cancel: delegated task still unwinding after "
                "%.0fs teardown bound — permit will free early (S-2)",
                _CEILING_TEARDOWN_BOUND_S,
            )
        await _reap_bounded_tree(tree, role)
        raise
    if pending:
        inner.add_done_callback(_retrieve_late_task_exception)
        tree.begin_termination()          # #1212: the walk while the CLI lives
        inner.cancel()
        if not await _await_task_teardown(inner, _CEILING_TEARDOWN_BOUND_S):
            logger.warning(
                "delegation ceiling: delegated task still unwinding after "
                "%.0fs teardown bound — permit will free early (S-2)",
                _CEILING_TEARDOWN_BOUND_S,
            )
        await _reap_bounded_tree(tree, role)
        raise DelegationCeilingExceeded(
            f"delegated turn exceeded the {ceiling:.0f}s wall-clock "
            f"ceiling and was cancelled (S-2 runaway backstop)", tool_counts)
    return inner.result()


async def _reap_bounded_tree(tree: Any, role: str) -> None:
    """#1212 §3.3 steps 3–6 as their OWN task, awaited through
    ``_await_task_teardown``: a re-cancel of the runner is absorbed and never
    reaches the kill, and a wait that outlives its bound leaves the task
    running detached — it closes the fds itself, and logs at ERROR when the
    processes could not be confirmed gone."""
    tree.reaping = True
    task = asyncio.create_task(tree.reap(role))
    _REAPS.add(task)
    task.add_done_callback(_REAPS.discard)
    task.add_done_callback(_retrieve_late_task_exception)
    await _await_task_teardown(task, tree.GRACE_S + tree.EXIT_WAIT_S + 1.0)


async def _await_task_teardown(inner: asyncio.Task, bound_s: float) -> bool:
    """Wait up to *bound_s* for *inner* to actually finish, tolerating
    repeated cancellation of the CALLING task.

    Each re-cancellation of the caller lands here as a CancelledError out
    of ``asyncio.wait``; it is absorbed and the wait re-entered with the
    REMAINING budget, so an overlapping second cancel (voice deadline
    teardown racing a shutdown sweep) cannot end the outer task — and
    thereby free its permit — while the delegated work is still unwinding.
    Absorbing the extra cancels is safe: the cancel-path caller re-raises
    its original CancelledError immediately after this returns, and the
    wait is hard-bounded by *bound_s*. One deliberate consequence (Codex
    round-2 residual, accepted): a cancel that FIRST arrives during the
    ceiling-path teardown is absorbed too, so the task ends with
    ``DelegationCeilingExceeded`` rather than cancelled — bounded by
    *bound_s*, and it routes completion to the NOTIFYING failure branch
    instead of the silent cancelled branch, which is strictly more
    informative for the engager. Returns True when *inner* really ended."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + bound_s
    while not inner.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait({inner}, timeout=remaining)
        except asyncio.CancelledError:
            continue
    return inner.done()


def _new_voice_job(
    *,
    job_id: str,
    parent_job_id: str | None,
    cfg: Any,
    specialist_role: str,
    origin: dict,
    task_text: str,
    context_text: str,
    handoff_id: str | None = None,
    creating_speaker: SpeakerProvenance | None = None,
) -> VoiceJob:
    """Build the durable ACCEPTED row from trusted turn provenance.

    Task 12: ``creating_speaker`` defaults to the CURRENT turn's caller
    identity off ``origin["speaker_provenance"]`` (Task 10 Step 7's
    origin_var wiring), falling back to the explicit unattributed "system"
    identity — the same policy Task 10 Step 7b uses. An explicit override is
    passed by the continuation call site, which must copy the ORIGINAL job's
    creator rather than whoever is invoking ``continue_voice_job`` now.
    ``executing_speaker`` is always the target ``cfg``'s own identity via the
    one fallback policy (``agent_mod.speaker_provenance_for_role``): real
    persona if bound, a stable executor identity for an executor, else the
    honest "system" identity for an unbound Plan-1 specialist — never
    mislabeled "executor:<slug>".
    """
    import agent as agent_mod
    from personality_types import SpeakerProvenance

    raw_user_id = origin.get("user_id")
    if creating_speaker is None:
        creating_speaker = origin.get("speaker_provenance")
    if not isinstance(creating_speaker, SpeakerProvenance):
        creating_speaker = SpeakerProvenance(speaker_kind="system")
    executing_speaker = (
        agent_mod.speaker_provenance_for_role(cfg)
        if cfg is not None else SpeakerProvenance(speaker_kind="system")
    )
    return VoiceJob(
        id=job_id,
        parent_job_id=parent_job_id,
        creating_speaker=creating_speaker,
        executing_speaker=executing_speaker,
        creating_role=str(
            origin.get("execution_role") or origin.get("role") or "assistant"
        ),
        specialist_role=specialist_role,
        specialist_display_name=_specialist_display_name(cfg, specialist_role),
        creator_peer="voice",
        creator_user_id=(None if raw_user_id is None else str(raw_user_id)),
        scope_id=str(origin.get("chat_id") or ""),
        origin_route_id=str(origin["voice_route_id"]),
        origin_device_id=str(origin["origin_device_id"]),
        # What this endpoint said it can receive (#233/#224). Delivery
        # dispatches on it, and the spoken promise is rendered from it.
        delivery_modality=selected_delivery_modality(origin),
        task=task_text,
        context=context_text,
        created_at=time.time(),
        started_at=None,
        terminal_at=None,
        expires_at=None,
        execution_state=ExecutionState.ACCEPTED,
        delivery_state=DeliveryState.NONE,
        result=None,
        failure=None,
        awaiting_input=False,
        continuable_until=None,
        delivery_sequence=0,
        delivery_attempt_id=None,
        lease_until=None,
        cancel_pending=False,
        job_control_id=(
            str(origin["voice_job_control_id"])
            if isinstance(origin.get("voice_job_control_id"), str)
            and origin["voice_job_control_id"].strip()
            else None
        ),
        handoff_id=handoff_id,
    )


# #321: how long a terminal write may keep absorbing cancellation before the
# wait is abandoned. Generous — a healthy atomic snapshot commits in
# milliseconds; only a genuinely wedged disk/loop reaches this. The write task
# itself is NOT cancelled (a to_thread commit can only be abandoned, never
# cancelled mid-flight); its eventual exception is retrieved by callback.
_VOICE_PERSIST_CANCEL_BOUND_S: float = 30.0


def _retrieve_abandoned_persistence_exception(t: asyncio.Task) -> None:
    """Done-callback for an abandoned terminal-write task — retrieves the
    eventual exception so asyncio never logs 'exception was never retrieved'
    (content deliberately not rendered)."""
    if t.cancelled():
        return
    if t.exception() is not None:
        logger.error(
            "abandoned terminal persistence write eventually failed",
        )


async def _await_voice_persistence(
    operation: Awaitable[Any], *, already_cancelled: bool = False,
) -> Any:
    """Finish one terminal write even if lifecycle cancellation arrives.

    #321: cancellation absorption is BOUNDED. The pre-fix loop re-entered
    ``shield()`` forever, so a wedged write made cancellation permanently
    ineffective — the registry-owned lifecycle task never completed and the
    job kept its concurrency permit. Past the bound the wait is abandoned
    (the write cannot be cancelled mid-commit; if it later lands, the
    terminal state is simply durable) and the cancellation propagates.

    ``already_cancelled`` (Terra r2): a call made FROM a cancellation
    handler (the lifecycle's terminal fallback) gets no fresh
    ``CancelledError`` to start its clock — without this flag that wait was
    unbounded again, and a wedged write still stranded the lifecycle task
    and its permit. When set, the bound runs from entry and giving up
    raises ``CancelledError`` so the task finishes cancelled."""
    task = asyncio.create_task(operation)
    cancellation: asyncio.CancelledError | None = None
    loop = asyncio.get_running_loop()
    deadline: float | None = (
        loop.time() + _VOICE_PERSIST_CANCEL_BOUND_S
        if already_cancelled else None
    )
    while not task.done():
        try:
            if deadline is None:
                await asyncio.shield(task)
            else:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except asyncio.CancelledError as exc:
            cancellation = exc
            if deadline is None:
                deadline = loop.time() + _VOICE_PERSIST_CANCEL_BOUND_S
        except TimeoutError:
            break
        except Exception:
            break
    if not task.done():
        # Only reachable via the deadline branches.
        task.add_done_callback(_retrieve_abandoned_persistence_exception)
        logger.error(
            "terminal persistence still running %.0fs after cancellation — "
            "abandoning the wait (restart recovery remains authoritative)",
            _VOICE_PERSIST_CANCEL_BOUND_S,
        )
        if cancellation is not None:
            raise cancellation
        raise asyncio.CancelledError()
    # Sol r3: an absorbed cancellation takes PRECEDENCE over the operation's
    # own outcome. Raising the op failure here instead would send
    # _persist_voice_terminal into its fallback as an ordinary failure — an
    # UNBOUNDED wait, with the original cancellation already consumed —
    # recreating the stranded-permit bug. The op exception is retrieved for
    # hygiene only (content deliberately not rendered).
    if cancellation is not None:
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "terminal persistence failed while cancellation was pending",
            )
        raise cancellation
    return task.result()


async def _persist_voice_terminal(
    operation: Awaitable[Any],
    *,
    registry,
    job_id: str,
    specialist_role: str,
    already_cancelled: bool = False,
) -> str:
    """Persist a terminal state, then try one metadata-only safe fallback.

    ``already_cancelled`` (Terra/Sol r2): set by the lifecycle's
    CancelledError handler so BOTH waits here are bounded from entry — the
    abandoned first write may still hold the registry lock, and an unbounded
    second wait would strand the lifecycle task and its permit exactly the
    way #321 forbids."""
    try:
        await _await_voice_persistence(
            operation, already_cancelled=already_cancelled)
        return "primary"
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — never render a persistence exception
        logger.error(
            "Voice job %s role=%s terminal persistence failed; "
            "attempting safe fallback",
            job_id[:8], specialist_role,
        )

    try:
        await _await_voice_persistence(
            registry.fail_compat(
                job_id,
                JobFailure(
                    "persistence_failed",
                    "Specialist result could not be saved.",
                ),
            ),
            already_cancelled=already_cancelled,
        )
        return "fallback"
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — restart recovery remains authoritative
        logger.error(
            "Voice job %s role=%s safe terminal fallback failed; "
            "scheduling reconciliation",
            job_id[:8], specialist_role,
        )
        try:
            registry.schedule_failure_reconciliation(job_id)
        except Exception:  # noqa: BLE001 — restart recovery remains authoritative
            logger.error(
                "Voice job %s role=%s reconciliation scheduling failed; "
                "restart recovery required",
                job_id[:8], specialist_role,
            )
        return "unpersisted"


async def _persist_cancelled_terminal(
    *, registry, job_id: str, specialist_role: str,
) -> None:
    """Persist the CANCELLED terminal for a cancelled lifecycle, bounded.

    Terra/Sol r2 + Terra r3: this runs from the lifecycle's CancelledError
    handler, so (a) the wait gets no fresh ``CancelledError`` to bound it —
    ``already_cancelled=True`` runs it against its own deadline; and (b) any
    failure — ordinary exception OR bounded give-up — must hand the row to
    the CANCEL reconciliation, never the failure-flavored
    ``persistence_failed`` fallback (`_persist_voice_terminal`), which would
    terminalize the job FAILED and silently drop the cancel contract.
    Never raises except propagating nothing: swallows everything (the caller
    re-raises the original cancellation regardless).

    Terra r7 note: ``fail_compat(JobFailure("cancelled", ...))`` is NOT a
    FAILED terminal — the registry's kind-aware ``_fail_current_locked``
    maps kind "cancelled" to ``ExecutionState.CANCELLED`` (delivery
    CANCELLED), so this primary write and the reconciliation retry
    (``registry.cancel``) converge on the same durable shape (pinned by
    ``test_fail_compat_cancelled_kind_persists_cancelled_state``)."""
    try:
        await _await_voice_persistence(
            registry.fail_compat(
                job_id,
                JobFailure("cancelled", "Specialist job was cancelled."),
            ),
            already_cancelled=True,
        )
        return
    except asyncio.CancelledError:
        logger.error(
            "Voice job %s role=%s cancellation write gave up within the "
            "bound; scheduling cancel reconciliation",
            job_id[:8], specialist_role,
        )
    except Exception:  # noqa: BLE001 — never render a persistence exception
        logger.error(
            "Voice job %s role=%s cancellation write failed; scheduling "
            "cancel reconciliation", job_id[:8], specialist_role,
        )
    try:
        registry.schedule_cancel_reconciliation(job_id)
    except Exception:  # noqa: BLE001 — restart recovery remains authoritative
        logger.error(
            "Voice job %s cancel reconciliation scheduling failed; "
            "restart recovery required", job_id[:8],
        )


async def _run_voice_job_lifecycle(
    *,
    registry,
    job_id: str,
    cfg: Any,
    specialist_role: str,
    task_text: str,
    context_text: str,
    origin: dict,
    resolution: Any,
    started_at: float,
) -> None:
    """Run and persist one job inside the registry-owned task lifetime."""
    try:
        try:
            # #541: this lifecycle coroutine IS the registry-owned task —
            # the binding scopes the bounded runner (and its inner task).
            _delegation_quota_key.set(job_id)
            output = await _run_delegated_agent_bounded(
                cfg,
                task_text,
                context_text,
                resolution=resolution,
                output_format=VOICE_JOB_OUTPUT_FORMAT,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — model text stays private
            kind = _classify_error(exc).value
            await _persist_voice_terminal(
                registry.fail_compat(
                    job_id,
                    JobFailure(
                        kind=kind,
                        message="Specialist could not complete the voice job.",
                    ),
                ),
                registry=registry,
                job_id=job_id,
                specialist_role=specialist_role,
            )
            logger.info(
                "Voice job %s role=%s terminal=failed kind=%s elapsed_s=%.2f",
                job_id[:8], specialist_role, kind, time.time() - started_at,
            )
            return

        # A CLI-aborted run is checked BEFORE the parser (#254): it has no
        # envelope to be malformed, and "structured_output must be an object"
        # points the operator at the specialist's field shapes when the actual
        # repair is a turn limit, a spend ceiling, or a result-contract
        # collision. Same terminal outcome, honest cause.
        if output.run_aborted:
            kind = _run_abort_kind(output.run_subtype)
            await _persist_voice_terminal(
                registry.fail_compat(
                    job_id,
                    JobFailure(
                        kind=kind,
                        message="Specialist could not complete the voice job.",
                    ),
                ),
                registry=registry,
                job_id=job_id,
                specialist_role=specialist_role,
            )
            logger.warning(
                "Voice job %s role=%s terminal=failed kind=%s subtype=%s "
                "elapsed_s=%.2f",
                job_id[:8], specialist_role, kind,
                _known_token(output.run_subtype, _KNOWN_RESULT_SUBTYPES),
                time.time() - started_at,
            )
            return

        try:
            voice_result = parse_voice_job_result(output.structured_output)
        except VoiceJobResultError as exc:
            await _persist_voice_terminal(
                registry.fail_compat(
                    job_id,
                    JobFailure(
                        "invalid_specialist_result",
                        "Specialist returned an invalid structured result.",
                    ),
                ),
                registry=registry,
                job_id=job_id,
                specialist_role=specialist_role,
            )
            # `exc` names the field that failed and never interpolates the
            # rejected payload (voice_job_result's stated contract), so it is
            # the one safe diagnostic available. Without it this failure reads
            # as "invalid" and nothing else, and the specialist cannot be fixed.
            logger.warning(
                "Voice job %s role=%s terminal=failed "
                "kind=invalid_specialist_result reason=%s elapsed_s=%.2f",
                job_id[:8], specialist_role, exc, time.time() - started_at,
            )
            return

        # Resolve disclosure without placing either full result or approved
        # spoken text on a concierge-facing surface. Task 4 re-evaluates this
        # durable envelope immediately before delivery.
        spoken = spoken_text_for(
            voice_result,
            prompted=False,
            identity_clearance=voice_identity_clearance(origin),
        )
        durable_result = json.dumps(
            output.structured_output,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        persistence = await _persist_voice_terminal(
            registry.finish_voice_result(
                job_id,
                durable_result,
                awaiting_input=voice_result.awaiting_input,
                delivery_ttl_s=voice_result.delivery_ttl_s,
            ),
            registry=registry,
            job_id=job_id,
            specialist_role=specialist_role,
        )
        logger.info(
            "Voice job %s role=%s terminal=%s status=%s sensitivity=%s "
            "result_chars=%d spoken_chars=%d elapsed_s=%.2f",
            job_id[:8], specialist_role,
            "succeeded" if persistence == "primary" else persistence,
            voice_result.status, voice_result.sensitivity,
            len(durable_result), len(spoken), time.time() - started_at,
        )
    except asyncio.CancelledError:
        # This is also safe after a cancellation arrived during a successful
        # shielded terminal write: fail_compat then observes a terminal row
        # and is an idempotent no-op.
        await _persist_cancelled_terminal(
            registry=registry, job_id=job_id,
            specialist_role=specialist_role,
        )
        raise


async def _run_voice_job_after_bind(
    *,
    start_gate: asyncio.Event,
    lifecycle_kwargs: dict[str, Any],
) -> None:
    """Prevent specialist work from starting before RUNNING is durable."""
    await start_gate.wait()
    await _run_voice_job_lifecycle(**lifecycle_kwargs)


def _create_voice_lifecycle_task(
    *,
    start_gate: asyncio.Event,
    lifecycle_kwargs: dict[str, Any],
) -> asyncio.Task:
    """Create the bind-gated lifecycle task without leaking a coroutine."""
    coroutine = _run_voice_job_after_bind(
        start_gate=start_gate,
        lifecycle_kwargs=lifecycle_kwargs,
    )
    try:
        return asyncio.create_task(coroutine)
    except BaseException:
        coroutine.close()
        raise


async def _start_voice_async_job(
    *,
    cfg: Any,
    specialist_role: str,
    task_text: str,
    context_text: str,
    origin: dict,
    resolution: Any,
    permit: Any,
    handoff: _PermitHandoff,
    handoff_reservation: Any | None = None,
    parent_job_id: str | None = None,
    creating_speaker: SpeakerProvenance | None = None,
) -> dict:
    """Persist ACCEPTED, bind RUNNING task/permit ownership, return metadata.

    Task 12: ``creating_speaker`` is the continuation caller's override — the
    ORIGINAL parent job's creator — passed by the ``continue_voice_job``
    resume branch (``parent_job_id`` is not None there). A fresh delegation
    (``parent_job_id`` is None) leaves it unset so ``_new_voice_job`` derives
    it from the current turn's own origin."""
    registry = _specialist_registry.job_registry
    await registry.load()
    # Apply result TTL before the atomic route-capacity check so an expired
    # READY row cannot consume one of the configured live backlog slots.
    await registry.expire_due()
    job_id = str(uuid.uuid4())
    handoff_id = str(uuid.uuid4()) if handoff_reservation is not None else None
    job = _new_voice_job(
        job_id=job_id,
        parent_job_id=parent_job_id,
        cfg=cfg,
        specialist_role=specialist_role,
        origin=origin,
        task_text=task_text,
        context_text=context_text,
        handoff_id=handoff_id,
        creating_speaker=creating_speaker,
    )
    actor = _job_actor_from_origin(origin)
    if parent_job_id is not None and actor is None:
        raise JobAuthorizationError("voice continuation has no actor")
    created = False
    task: asyncio.Task | None = None
    reservation_committed = False
    start_gate = asyncio.Event()
    try:
        try:
            if parent_job_id is None:
                await registry.create(
                    job,
                    max_active_ready_per_route=_voice_job_route_cap,
                )
            else:
                await registry.create_continuation(
                    parent_job_id,
                    job,
                    actor=actor,
                    max_active_ready_per_route=_voice_job_route_cap,
                )
            created = True
        except JobRouteCapacityError:
            if handoff_reservation is not None:
                handoff_reservation.release()
            return _result({
                "status": "error",
                "kind": "route_capacity_reached",
                "message": (
                    f"This voice route already has {_voice_job_route_cap} "
                    "specialist jobs "
                    "awaiting completion or delivery."
                ),
            })
        except BaseException:
            # Atomic persistence can publish and then re-raise cancellation.
            created = registry.get(job_id) is not None
            raise

        started_at = time.time()
        task = _create_voice_lifecycle_task(
            start_gate=start_gate,
            lifecycle_kwargs={
                "registry": registry,
                "job_id": job_id,
                "cfg": cfg,
                "specialist_role": specialist_role,
                "task_text": task_text,
                "context_text": context_text,
                "origin": dict(origin),
                "resolution": resolution,
                "started_at": started_at,
            },
        )
        try:
            await registry.bind_task(job_id, task, permit=permit)
        except BaseException:
            if registry.owns_task(job_id, task):
                handoff.transferred = True
                start_gate.set()
                _record_launch_safe(specialist_role)
            raise
        handoff.transferred = True
        if handoff_reservation is not None:
            pending_job = await registry.mark_handoff_pending(job_id, handoff_id)
            handoff_reservation.commit(pending_job)
            reservation_committed = True
        start_gate.set()
    except BaseException as launch_exc:
        # #671: an ORDINARY launch failure is a cause this arm knows statically,
        # so latch it and let the rollback settle the row as it always has. A
        # `CancelledError` is deliberately NOT latched: during a graceful stop
        # that IS the process dying, and latching it would settle the row here
        # and leave the boot reconciliation nothing to find — the very defect
        # this cluster fixes. `CancelledError` derives from `BaseException`, so
        # `isinstance(..., Exception)` is exactly the ordinary-failure test (it
        # also excludes KeyboardInterrupt and SystemExit, which are process
        # death by another name).
        if created and isinstance(launch_exc, Exception):
            registry.note_cancel_cause(job_id, "launch_rollback")
        if handoff_reservation is not None and not reservation_committed:
            handoff_reservation.release()
        if handoff.transferred:
            if handoff_reservation is None:
                raise
            if task is not None and not task.done():
                task.cancel()
                await _await_task_teardown(task, _CEILING_TEARDOWN_BOUND_S)
            if created:
                if parent_job_id is None:
                    await registry.cancel(job_id)
                else:
                    await registry.compensate_unbound_continuation(
                        parent_job_id,
                        job_id,
                        actor=actor,
                    )
            raise
        if task is not None and not task.done():
            task.cancel()
            await _await_task_teardown(task, _CEILING_TEARDOWN_BOUND_S)
        if created:
            if parent_job_id is None:
                await registry.cancel(job_id)
            else:
                await registry.compensate_unbound_continuation(
                    parent_job_id,
                    job_id,
                    actor=actor,
                )
        raise

    _record_launch_safe(specialist_role)
    logger.info(
        "Voice job %s role=%s accepted route_bound=true",
        job_id[:8], specialist_role,
    )
    return _result({
        "status": "pending",
        "job_id": job_id,
        "specialist_display_name": job.specialist_display_name,
    })


async def _launch_interactive_engagement(
    agent_name: str, task_text: str, context_text: str, origin: dict,
    *, job: background_jobs.JobDecl,
    plugin_host: background_jobs.JobHost | None = None,
    self_host: bool = False,
    task_birth: "TaskBirth | None" = None,
) -> dict:
    """One launch owner for background jobs, hosted on a specialist or run
    by a resident's plugin worker (``plugin_host``): the job runs as an
    engagement with its own long session. ``self_host`` (S7a): a specialist starting
    its own job — the prelaunch skips the ACL and the depth cap, and the
    record is stamped at depth 1. *task_birth* is the job's birth as its
    launcher decided it; without one it is decided here, once."""
    import agent as agent_mod

    if plugin_host is None:
        _self_tok = _self_host_prelaunch.set(self_host)
        try:
            agent_name, cfg, resolution, permit, error = await _prelaunch(
                agent_name, origin, "interactive", task_text, context_text)
        finally:
            _self_host_prelaunch.reset(_self_tok)
        if error is not None:
            return error
    else:
        cfg = _agent_role_map.get(agent_name)
        if cfg is None:
            return _result({"status": "error", "kind": "unknown_agent"})
        for field, value, limit in (
            ("task", task_text, specialist_limits._MAX_TASK_CHARS),
            ("context", context_text, specialist_limits._MAX_CONTEXT_CHARS),
        ):
            if not isinstance(value, str):
                return _result({"status": "error", "kind": "invalid_argument",
                                "field": field, "message": f"{field} must be a string."})
            if len(value) > limit:
                return _result({"status": "error", "kind": "input_too_large",
                                "field": field, "length": len(value), "limit": limit})
        if _effective_delegation_depth(origin) >= _MAX_DELEGATION_DEPTH:
            return _result({"status": "error", "kind": "delegation_depth_exceeded"})
        if origin.get("channel") == "voice":
            return _result({"status": "error", "kind": "job_needs_text_channel"})
        from provenance import CASA_PLUGIN_TURN_TARGETS
        if origin.get("synthetic") in CASA_PLUGIN_TURN_TARGETS:
            return _result({"status": "error", "kind": "mode_unsupported_on_setup_turn"})
        from plugin_grants import withhold_env_unresolved
        try:
            resolution = plugin_registry.ResolutionResult(
                registry_valid=True, plugins=[plugin_host.plugin], issues=[])
            resolution, _ = withhold_env_unresolved(
                resolution, context=f"plugin job {agent_name} launch")
            if not resolution.plugins:
                return _result({"status": "error", "kind": "plugin_env_unresolved",
                                "plugin": plugin_host.plugin.name})
        except Exception as exc:
            return _result({"status": "error", "kind": "plugin_unavailable",
                            "message": str(exc)})
        permit, refusal = background_jobs.acquire_job_permit(
            plugin_host, _specialist_limiter)
        if refusal is not None:
            return _result(refusal)
    owned = permit
    spawn_owned = None
    casa_grants = SPECIALIST_CASA_GRANTS
    # #1277: who started the job, evaluated once, here, from the launching
    # origin; both renderers read it from the record, never from markers.
    origin["job"] = background_jobs.initial_job_state(
        job, started_by=background_jobs.job_started_by(origin),
        context=context_text if isinstance(context_text, str) else "",
        self_host=self_host)
    casa_grants += background_jobs.JOB_CASA_GRANTS
    if plugin_host is not None:
        casa_grants = background_jobs.PLUGIN_JOB_CASA_GRANTS
        origin["plugin_job"] = {"plugin": plugin_host.plugin.name, "model": cfg.model}
    try:
        # #283: agent-spawn cap — before any of the launch's awaits below. Operator-exempt only when positively marked
        # (_operator_turn); absence = agent context, fail closed.
        if _agent_spawn_limiter is not None and _is_agent_context(origin):
            spawn_owned = _agent_spawn_limiter.try_acquire()
            if spawn_owned is None:
                return _agent_spawn_cap_refusal()
            origin["_agent_spawned"] = True
        # #283 (INV-ENG-004 gap 2): interactive children carry a
        # delegation depth exactly as ephemeral children do — stamped on
        # the origin the record persists; the depth gate reads it back
        # through the engagement-record fallback, so an interactively
        # engaged specialist cannot delegate onward. Unconditional:
        # operator-initiated children run at depth 1 too.
        # S7a: a specialist's own job is never deeper than one, as a
        # resident-started job is — it cannot delegate onward either way.
        origin["delegation_depth"] = (
            1 if self_host else _effective_delegation_depth(origin) + 1)
        # Task 6 (spec §4.6): the `owned` lexical ownership guard (try/finally
        # around this whole body) releases `permit` on ANY exit before launch
        # transfer — including a CancelledError raised at any await below and
        # every early error return here — so these paths need no inline
        # release. Ownership transfers to the engagement record only AFTER
        # driver.start() succeeds (see the `owned = None` there).
        # A job talks to the operator in their direct chat: it needs the
        # Telegram channel, and a request that came from elsewhere is refused.
        # An optional log group adds the agent's log; jobs never need it.
        if _channel_manager is None:
            return _result({"status": "error", "kind": "no_channel_manager",
                            "message": "channel manager missing"})
        refusal = _job_origin_refusal(origin)
        if refusal is not None:
            return refusal
        # The job talks through the bot of its origin's route, which the
        # record persists with the origin.
        transport = _transport()
        if transport is None:
            return _result({"status": "error", "kind": "channel_unavailable",
                            "message": "the Telegram channel is not running"})
        try:
            channel = transport.endpoint_for_route(origin, site="job_launch")
        except (RouteMissing, AddressError, EndpointUnavailable) as exc:
            logger.error("job launch for %s: no Telegram endpoint for the "
                         "origin: %s", agent_name, exc)
            return _result({"status": "error", "kind": "channel_unavailable",
                            "message": "the Telegram channel is not running"})
        # §3.8 (Sol #4): record the specialist's plugin binding so verify can
        # disclose this engagement if a later plugin_update supersedes its
        # artifact (informational — mirrors the executor-engagement case). The
        # specialist runs on the same tier:role resolution _build_specialist_
        # options uses below.
        # Sol round-3 H7b: resolve ONCE and feed the SAME result to both the
        # engagement record and the options builder, so a concurrent update can't
        # make the recorded binding disagree with what actually launches.
        # A5: when the requires gate (Task 5) already resolved this agent's
        # plugins (non-empty `cfg.requires`), reuse that SAME ResolutionResult
        # instead of resolving again — the engagement record's binding must
        # never disagree with what the requires gate actually validated.
        # `resolution` (from `_prelaunch`) is None whenever `cfg.requires` is
        # empty, in which case this resolves fresh exactly as before Task 5.
        # #1179: the gate resolved before the launch's earlier awaits, and this
        # session is built after them, so a requires target resolves here too
        # and ADDS what was assigned in between (`_with_late_assignments`) —
        # keeping every plugin the gate admitted. Synchronous on purpose, and a
        # failed resolve keeps the gate's set.
        if plugin_host is not None:
            _spec_res = resolution
        elif resolution is not None:
            try:
                _spec_tier = (_agent_registry.tier_for_role(agent_name)
                              if _agent_registry is not None else None) or "specialist"
                _spec_res = _with_late_assignments(
                    resolution,
                    plugin_registry.resolve_for(f"{_spec_tier}:{agent_name}"))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "specialist %s launch: late plugin resolve failed, "
                    "launching with the requires gate's set: %s", agent_name, exc)
                _spec_res = resolution
        else:
            _spec_tier = (_agent_registry.tier_for_role(agent_name)
                          if _agent_registry is not None else None) or "specialist"
            _spec_res = plugin_registry.resolve_for(f"{_spec_tier}:{agent_name}")
        # #424 r3 (Terra r3-1 / Sol r3-2): filter ONCE, before the record
        # is written — the record must pin what the session actually
        # launches with, or a later resume (after the secret is wired)
        # loads a plugin mid-engagement that the engagement never started
        # with. The filtered result feeds BOTH the record and the options
        # builder below (H7b), whose own filter normally no-ops; a secret
        # that goes unresolved in between is withheld there, and its row then
        # records the profile name the build's plan gives it (#1186).
        from plugin_grants import withhold_env_unresolved
        if plugin_host is None:
            _spec_res, _ = withhold_env_unresolved(
                _spec_res, context=f"specialist {agent_name} engagement")
        _spec_arts = tuple(
            {"name": rp.name, "artifact_id": rp.artifact_id, "path": rp.path,
             # Task 5: recorded so a resumed session reproduces the same
             # runtime identity via _resolution_from_recorded (above).
             "manifest_name": rp.manifest_name}
            for rp in _spec_res.plugins)
        # Create record. #326: create() persists STRICTLY — a tombstone
        # write failure raises (no ghost record); the permit is released by
        # the outer finally (`owned` is still set here).
        try:
            rec = await _engagement_registry.create(
                kind="plugin" if plugin_host is not None else "specialist",
                role_or_type=agent_name, driver="in_casa",
                # #1038: the brief's resolved disclosure rides the persisted
                # origin; the engagement's DM sends inherit it (for_engagement).
                task=task_text, origin=_launch_note(origin, task_text)[0],
                plugin_artifacts=_spec_arts,
                # v0.166.0: pin the specialist's OWN casa-framework grant so
                # the bridge gate admits exactly what _build_specialist_options
                # launches with — its declared tools (a specialist config may
                # grant more than query/completion) plus the launch-mandatory
                # grants. Without this the record is empty and every framework
                # tool beyond the two mandatory ones is wrongly rejected.
                tools_allowed=(tuple(grants_for_resolution(_spec_res))
                               if plugin_host is not None else tuple(
                                   t for t in (cfg.tools.allowed or ()) if t != "Skill"
                               )) + casa_grants,
                # #283: reservation rides the record from here (r4).
                agent_spawn_permit=spawn_owned,
                # a job is a task: born here when its launcher did not
                **_task_record_fields(task_birth if task_birth is not None
                                      else _task_birth("", task_text, agent_name, origin)),
            )
        except asyncio.CancelledError:
            # create() already compensated the record.
            raise
        except Exception as exc:  # noqa: BLE001
            return _result({
                "status": "error", "kind": "record_persist_failed",
                "message": str(exc)})
        # Task 6 (spec §4.6): stash the permit on the engagement record so
        # EVERY registry terminal transition (mark_error, mark_cancelled,
        # mark_completed, try_transition_terminal) releases it — including
        # the direct mark_error routes (resume/orphan failures in
        # channels/telegram.py) that bypass `_finalize_engagement`.
        # `_finalize_engagement` also releases it as an idempotent fallback.
        # Ownership is NOT yet transferred here: `owned` stays set until
        # driver.start() succeeds, so a cancellation/error at any await
        # before that still releases via the outer finally (all releases
        # are idempotent).
        rec.permit = permit
        # #283 (design r4): the spawn reservation transferred at create —
        # a cancellation between here and driver-live must NOT release a
        # token whose durable record stays live; terminal transitions own
        # it now (release-both in _release_permit). The finally sees None.
        spawn_owned = None
        # #678 (Terra, seam rounds 3+4): ONE CancelledError handler
        # covering the WHOLE interval from the committed create() through
        # the ownership transfer. Before it, this branch had no
        # cancellation compensation at all past create(): a cancellation
        # at _build_specialist_options or inside driver.start() escaped
        # every handler, the outer finally released the permit, and a
        # durably-ACTIVE record was left with no client. Per-await handlers were the wrong
        # answer — the first round of this finding named driver.start()
        # alone and the second named two more awaits, so the interval is
        # covered once rather than a third handler added.
        # #698: enrol this launch with the graceful stop, before the
        # interval's first await.
        _launch_handle = _register_launch_safe(rec.id)
        try:
            # Build options + start driver (off-loop: registry resolve is file IO,
            # and so is opening the job's scratchpad with its brief).
            _applied_plans: list = []
            _ctx = origin["job"].get("context")
            _pad_brief = (f"{task_text}\nContext: {_ctx}" if _ctx else task_text,
                          operator_words(origin))

            def _specialist_job_options():
                return background_jobs.job_session_options(
                    _build_specialist_options(
                        cfg, resolution=_spec_res, extra_casa_tools=casa_grants,
                        plan_out=_applied_plans, task_id=rec.id),
                    rec, brief=_pad_brief)
            try:
                if plugin_host is not None:
                    options = await asyncio.to_thread(
                        _build_plugin_job_options, rec, _spec_res,
                        plan_out=_applied_plans, brief=_pad_brief)
                else:
                    options = await asyncio.to_thread(_specialist_job_options)
            except Exception as exc:
                _abort = await _abort_launch_inline(
                    channel, rec,
                    kind="options_build_failed", message=str(exc))
                if _abort is LaunchAbortResult.PERSIST_FAILED:
                    owned = None
                return _result({"status": "error", "kind": "options_build_failed",
                                "message": str(exc)})
            # S8: the record stores what was BUILT — the plan the builder
            # handed back through ``plan_out``, persisted before the client
            # starts; never a re-read (a mutation could move the snapshot
            # between the build and the read). The profile captured at the
            # requires gate reaches it only as the plan's own fallback for a
            # plugin no longer assigned live (S8 §5, #1186).
            try:
                _applied = _applied_plans[-1].effective_profiles() if _applied_plans else {}
                await _engagement_registry.update_plugin_profiles(rec.id, _applied)
            except Exception as exc:  # noqa: BLE001
                # A record that cannot say what this build enforces must not
                # start: the next resume's fallback would read it.
                _abort = await _abort_launch_inline(
                    channel, rec,
                    kind="profile_persist_failed", message=str(exc))
                if _abort is LaunchAbortResult.PERSIST_FAILED:
                    owned = None
                return _result({"status": "error", "kind": "profile_persist_failed",
                                "message": str(exc)})

            prompt = background_jobs.launch_prompt(
                job, task_text, context_text,
                job_id=rec.id, started_by=rec.origin["job"].get("started_by"),
                can_ask=plugin_host is None)

            driver = getattr(agent_mod, "active_engagement_driver", None)
            if driver is None:
                _abort = await _abort_launch_inline(
                    channel, rec,
                    kind="no_driver",
                    message="engagement driver not initialized")
                # A durable win releases the permit inside the registry
                # transition; the outer finally releases it again
                # (idempotent). PERSIST_FAILED is the exception:
                if _abort is LaunchAbortResult.PERSIST_FAILED:
                    # #757: the transition rolled back, so the record is
                    # still LIVE — and a live record keeps its permit. The
                    # outer `finally` would otherwise free this engagement's
                    # scope slot while its record is still resumable, the
                    # hazard engagement_registry warns about at the strict
                    # release. Transfer it exactly as the record_live arm
                    # above does; whatever takes the record terminal later
                    # (reap or cancel, both through _finalize_engagement)
                    # releases it.
                    owned = None
                return _result({"status": "error", "kind": "no_driver",
                                "message": "engagement driver not initialized"})
            # Open the job's session: its reader and its delivery worker
            # start. A failure before the session is registered is a launch
            # abort; once registered, session loss owns every failure.
            try:
                await driver.open(rec, options=options)
            except Exception as exc:  # noqa: BLE001
                await _abort_launch_inline(
                    channel, rec,
                    kind="driver_start_failed", message=str(exc))
                return _result({"status": "error", "kind": "driver_start_failed",
                                "message": str(exc)})
            if (_engagement_registry is not None
                    and _engagement_registry.launch_shutdown_active()):
                _detail = "Casa was stopping when this launch was cancelled"
                await _report_launch_death_inline(
                    channel, rec, kind="launch_cancelled",
                    detail=_detail, driver=driver)
                return _result({
                    "status": "error", "kind": "launch_cancelled",
                    "message": _detail,
                })
            # The brief: the job's first and only launch write. The delivery
            # worker writes it, admitted at write time — a cancel landing
            # first means it is never written. From here the session's reader
            # owns the job.
            driver.enqueue_brief(rec, prompt)
            _job_log(rec, "▶ started")

            owned = None  # __TRANSFER_INTERACTIVE__
            _record_launch_safe(agent_name)
            return _result({
                "status": "pending",
                "engagement_id": rec.id,
                "agent": agent_name,
                "mode": "interactive",
            })
        except asyncio.CancelledError:
            _abort_launch_on_cancel(channel, rec)
            raise
        finally:
            _unregister_launch_safe(_launch_handle)
            # The launch lets go of the job: its delivery worker, which wrote
            # nothing while the launch was in flight, re-reads the job.
            _wake = getattr(getattr(agent_mod, "active_engagement_driver", None),
                            "wake_worker", None)
            if callable(_wake):
                _wake(rec.id)

    finally:
        if owned is not None:
            owned.release()
        if spawn_owned is not None:
            spawn_owned.release()


async def _start_job_on_host(host: Any, task: str, context: str, origin: dict,
                             *, self_host: bool = False) -> dict:
    """S6 §3.3 — the ONE job launcher, from the resolved host onward: the claim
    (INV-BGJOB-006), the specialist's plugin pin before any await, the one
    launch owner with ``plugin_host`` only for a resident host, and the claim's
    release in ``finally``. ``start_job`` calls it after its own refusals; the
    scheduled fire (``start_scheduled_job``) calls it with a scheduled origin —
    one place knows how a job is launched, not two edits that must agree."""
    role, job = host.role, host.decl
    # A job reports to the operator (its start, parked, question and ended
    # lines): a surface of its host, so it starts only where that host's own
    # chat is open — never falling back to the resident's chat.
    surface = _host_surface_refusal(role)
    if surface is not None:
        return _result({"status": "error", "kind": surface.kind,
                        "message": surface.text})
    refusal = background_jobs.claim_job_start(host, _engagement_registry)
    if refusal is not None:
        return _result(refusal)
    if host.kind == "specialist":
        # S1b: pin the SELECTED plugin's registry identity now, before any
        # await, so the job guard still sees it when the plugin is unassigned
        # while the launch awaits (the record's artifact rows come from a later
        # resolution). No `model`: that key is the resident worker's, and the
        # record stays a specialist engagement (the worker path keys on kind).
        origin["plugin_job"] = {"plugin": background_jobs.host_plugin_name(host)}
    try:
        return await _launch_interactive_engagement(
            role, task, context, origin,
            job=job, plugin_host=host if host.kind == "resident" else None,
            task_birth=_task_birth("", task, role, origin),
            **({"self_host": True} if self_host else {}))
    finally:
        background_jobs.release_job_start(background_jobs.host_plugin_name(host))


def _host_surface_refusal(role: str) -> Any:
    """The capability rule for a job's host *role*: ``None`` when its own
    chat is open (the resident's always is), else the typed refusal."""
    import own_voice
    return own_voice.require_open_chat(role, label=_display_name_for_role(role))


async def start_scheduled_job(role: str, trig: Any) -> None:
    """S6 §3.3 (INV-TRIG-022): a scheduled trigger whose target is a plugin job.
    No resident turn: the host is resolved now (it may have changed since the
    entry was written), the operator's identity and clearance are built exactly
    as the DM ingress builds them for the authenticated operator, the origin
    carries what the job's delivered slots and the recall gate read, and the
    ONE launcher starts the job. A fire that cannot start it tells the operator
    once — the admission decision as a past event at this occurrence — through
    the hosting specialist's label (the resident's when there is no host); with
    no operator to tell, it only logs. Nothing is retried before the next
    occurrence."""
    import authz_grants
    import background_jobs
    import chat_notes
    import ingress_identity as ingress_mod
    import result_broker as rb
    from log_cid import new_cid
    from provenance import scheduled_delivery_markers
    import scheduled_asks

    cfg = _agent_role_map.get(role)
    delegates = [d.agent for d in (getattr(cfg, "delegates", None) or [])]
    host = background_jobs.find_job_host(str(trig.job), role, delegates)
    identity = authz_grants._live_operator_identity()
    if identity is None:
        logger.warning("scheduled job %s for %s: no operator to tell; not started", trig.name, role)
        return
    chat_id, user_id = identity
    title = str(getattr(getattr(host, "decl", None), "title", "") or trig.job)
    label = rb.post_label(host.role) if host is not None else rb.post_label(role)

    async def tell(reason: str) -> None:
        line = chat_notes.bounded_line('{label}: "{title}" did not start — ' + reason,
                                       label=label, fields={"title": title})
        # Casa's own notice, through Casa's bot; the chat's agent reads the
        # same line as a note at its next turn in that bot's chat.
        channel = _casa_endpoint()
        try:
            if channel is not None:
                await channel.deliver_casa_notice(chat_id, line)
        except Exception as exc:  # noqa: BLE001 — nothing more is attempted
            logger.warning("scheduled job notice failed: %s", type(exc).__name__)
        if channel is not None and channel.has_bot_id:
            chat_notes.record(channel.bot_id, chat_id, line)

    if host is None:
        await tell("no host was available at this occurrence.")
        return
    try:
        trusted = ingress_mod.ingress_identity(
            "telegram", sender_id=str(user_id),
            sender_display_name="operator",
            sender_is_operator=True)
        clearance = trusted.server_origin.clearance
    except Exception:  # noqa: BLE001 — no identity ⇒ the "no operator" refusal (C2)
        logger.warning("scheduled job %s for %s: operator identity unavailable; not started",
                       trig.name, role, exc_info=True)
        return
    origin = {
        "role": role, "execution_role": role, "channel": trig.channel,
        "source": "scheduler", "message_type": "scheduled",
        "chat_id": chat_id, "user_id": user_id, "user_name": "operator",
        "cid": new_cid(), "trigger": trig.name,
        "_origin_route": "telegram", "_origin_clearance": clearance,
        **scheduled_delivery_markers(
            trig.channel, scheduled_asks.epoch_for(role, f"{trig.type}-{trig.name}")),
        "_scheduled_job": True,
    }
    # The job has no parent turn: its route is decided here, once, and
    # persists with its record, so every later post and question of the job
    # goes through the same bot whatever the role is bound to later.
    transport = _transport()
    if transport is not None:
        origin["_tg_bot"] = transport.route_for(role)
    result = await _start_job_on_host(host, str(trig.task or title), str(trig.context or ""), origin)
    try:
        payload = json.loads(result["content"][0]["text"])
    except Exception:  # noqa: BLE001 — an unreadable result is a failed launch
        payload = {"status": "error", "kind": "unknown"}
    if payload.get("status") == "error":
        kind = str(payload.get("kind") or "unknown")
        if kind.startswith("own_chat_"):
            # the host's own chat is not open: its job posts nowhere, and the
            # resident's chat is not its place either
            logger.info("scheduled job %s for %s not started: %s",
                        trig.name, role, kind)
            return
        reason = {
            "job_busy": "another job of this plugin held the claim at this occurrence.",
            "busy": "Casa was at its work limit at this occurrence.",
            "job_not_declared": "no host was available at this occurrence.",
        }.get(kind, f"({kind}).")
        await tell(reason)


@tool(
    "start_job",
    "Start a background job: one listed in <jobs>, or, for a specialist, one its own "
    "plugins declare. A job <jobs> lists as a delegate's own is started by delegating the "
    "request to that delegate. It runs as one long session, and the chat's resident is "
    "notified when it ends.",
    {"job": str, "task": str, "context": str},
)
async def start_job(args: dict) -> dict:
    origin = _snapshot_origin()
    if origin.get("channel") == "voice":
        return _result({"status": "error", "kind": "job_needs_text_channel"})
    caller = str(origin.get("execution_role") or origin.get("role", ""))
    cfg = _agent_role_map.get(caller)
    # S7a (INV-BGJOB-008): a specialist's own delegated or pinned turn (no
    # engagement bound) starts only a job its own plugins declare, hosted on
    # itself; every other caller resolves hosts exactly as before.
    self_host = (engagement_var.get(None) is None
                 and getattr(cfg, "kind", "") == "specialist")
    if self_host:
        hosts = background_jobs.own_job_hosts(caller)
        host = next((h for h in hosts
                     if h.decl.qualified_name == args.get("job", "")), None)
        # the work's origin: the calling turn's, without the markers that identify
        # the calling turn (its quota and echo key, its conversation, its
        # task's delivery: the job is a task of its own, born at its launch)
        from provenance import TASK_DELIVERY_KEY
        origin = {k: v for k, v in origin.items()
                  if k not in ("_delegation_id", "_delegated_session_id",
                               TASK_DELIVERY_KEY)}
    else:
        delegates = [d.agent for d in (getattr(cfg, "delegates", None) or [])]
        host = background_jobs.find_job_host(args.get("job", ""), caller, delegates)
    if host is None:
        names = [h.decl.qualified_name for h in (
            hosts if self_host else background_jobs.startable_jobs(caller, delegates))]
        return _result({
            "status": "error", "kind": "job_not_declared",
            "message": "Startable jobs: " + (", ".join(names) or "none"),
        })
    role, job = host.role, host.decl
    result = await _start_job_on_host(host, args.get("task", ""), args.get("context", "") or "", origin,
                                      **({"self_host": True} if self_host else {}))
    payload = json.loads(result["content"][0]["text"])
    if payload.get("status") == "pending":
        await _post_job_start_line(payload.get("engagement_id"))
    if self_host:
        # #1229: a specialist relays its own start to the operator; the id is
        # Casa's, not theirs — every result names the job instead
        payload.pop("engagement_id", None)
    if payload.get("kind") == "busy":
        payload["message"] = "Casa is at its concurrent-work limit. Try again shortly."
        return _result(payload)
    if payload.get("status") == "pending":
        payload.update(
            job=job.qualified_name, title=job.title, agent=role,
            message=f"Started {job.title}." + (
                f" Progress appears in {_display_name_for_role(role)}'s "
                + ("plugins log." if host.kind == "resident" else "log.")
                if _agent_logs is not None and _agent_logs.enabled else ""))
        return _result(payload)
    return _result(payload) if self_host else result


_START_LINE_TIMEOUT_S = 10.0


async def _post_job_start_line(engagement_id: Any) -> None:
    """The silent start line, with its Cancel button, of a job the operator
    started — directly or through an agent acting on their message. A job the
    scheduler started gets none, and nor does one an agent started on its
    own: neither was asked for in the chat. Bounded; a failure is logged."""
    rec = (_engagement_registry.get(engagement_id)
           if _engagement_registry is not None and isinstance(engagement_id, str)
           else None)
    if rec is None or (rec.origin.get("job") or {}).get("started_by") != "operator":
        return
    channel = _transport()
    poster = getattr(channel, "post_job_start_line", None)
    if not callable(poster):
        return
    try:
        await asyncio.wait_for(poster(rec), _START_LINE_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — a start line is never the job's fault
        logger.warning("job %s start line not posted: %s", rec.id[:8],
                       type(exc).__name__)


def _delegation_mode_for(cfg: Any, mode: str) -> str:
    """``async`` for a target of kind ``executor`` (the configurator), else
    *mode* unchanged."""
    if getattr(cfg, "kind", "") == "executor" and mode == "sync":
        return "async"
    return mode


@tool(
    "delegate_to_agent",
    "Delegate a task to another agent (resident or specialist) and return its result.",
    # #233/#224 (v0.119.0): `mode` is an ENUM, not a free string. Live evidence
    # (2026-07-25) showed the model emitting a value that wasn't exactly "sync";
    # because `validate_voice_handoff_static` strict-compares against "sync",
    # that ONE bad token silently bypassed the whole concierge voice-handoff
    # policy — no background hand-off, no spoken acknowledgement, and the
    # delegation fell into the sync path where the voice budget killed it. The
    # MCP input validator now rejects an unrecognised value before the handler
    # runs, so the class of bug cannot recur. Explicit JSON Schema (not the
    # {key: type} shorthand) because only that form can express an enum; every
    # key stays REQUIRED, exactly as the shorthand made them.
    {"type": "object",
     "properties": {
         "agent": {"type": "string"},
         "task": {"type": "string"},
         "context": {"type": "string"},
         "mode": {"type": "string",
                  "enum": ["sync", "async", "interactive"],
                  "description": (
                      "'sync' waits for the answer; 'async' returns at once "
                      "and the answer arrives later. 'interactive' is retired "
                      "and always refused: to let the person talk with a "
                      "specialist, delegate their words in 'sync' mode; they "
                      "carry on by writing to you.")},
     },
     "required": ["agent", "task", "context", "mode"]},
)
async def delegate_to_agent(args: dict) -> dict:
    """Invoke a Tier 2 specialist via the SDK and return its text.

    Sync mode (default): ``asyncio.wait`` up to 60s, return ok/error
    content; on timeout, attach completion callback and return a
    ``pending`` marker so the delegating resident can narrate
    "still working" and move on.

    Async mode (``mode="async"``): skip the wait, attach callback,
    return ``pending`` immediately.

    ``mode="interactive"`` is retired and refused (``interactive_retired``)
    before any gate; a specialist's background job still runs as an
    engagement, through ``start_job``.
    """
    # Import lazily — matches the `agent.py` origin_var ContextVar.
    import agent as agent_mod

    # TOTAL over arbitrary JSON, exactly as the `mode` coercion below is and
    # for the same reason: `args` can carry any type on the paths that don't
    # run the schema validator first. A non-string target must reach the
    # ACL's uniform denial, never raise — alias resolution calls `.strip()`
    # and the ACL's set membership needs a HASHABLE value, so a bare `7` or a
    # `["finance"]` would otherwise escape the gate with an AttributeError or
    # TypeError BEFORE it ran. Normalized to "" (never declared), not echoed.
    _requested_agent = args.get("agent", "")
    agent_name = _requested_agent if isinstance(_requested_agent, str) else ""
    task_text = args.get("task", "")
    context_text = args.get("context", "") or ""
    # #233/#224 (v0.119.0) defence in depth behind the schema enum above: an
    # unrecognised mode is normalized to the documented default rather than
    # carried through, because carrying it through is precisely what silently
    # bypassed the voice-handoff policy in production. Loud, never silent — and
    # the value itself is never echoed (it is model-supplied).
    # TOTAL over arbitrary JSON (Sol/Terra): `args` can carry any type on the
    # paths that don't run the schema validator first, and a truthy UNHASHABLE
    # value (`["sync"]`) would raise TypeError on the membership test.
    # AR-2: snapshot at entry — this handler awaits (channel setup,
    # engagement/delegation dispatch) and must not read a holder that a
    # later turn has since rewritten in place.
    origin = _snapshot_origin()

    # #433: canonicalize BEFORE the voice-handoff decision below, which does
    # its own exact-role-id membership test. A display name reaching it would
    # be read as "not declared", skip the handoff, and then pass the ACL —
    # bypassing the Concierge policy exactly as #233/#224 did. Silent and
    # total: every denial remains the ACL's, in `_prelaunch`.
    #
    # Also ahead of the mode coercion below, whose warning names the target:
    # that line exists to diagnose this very bug family, so it must report
    # the real role rather than the `<other>` an unresolved alias collapses to.
    agent_name = _canonical_delegate_target(agent_name, origin)

    _requested_mode = args.get("mode", "sync") or "sync"
    if _requested_mode == _RETIRED_MODE:
        # Refused before every gate and every side effect, never coerced: a
        # call coerced to 'sync' would run a task the caller meant as a
        # conversation. The target is not named (no ACL has run yet).
        logger.info("delegate_to_agent: retired mode 'interactive' refused; agent=%s",
                    _known_role(agent_name))
        return _result({"status": "error", "kind": "interactive_retired",
                        "message": _INTERACTIVE_RETIRED_MESSAGE})
    mode = (_requested_mode
            if isinstance(_requested_mode, str) and _requested_mode in _KNOWN_MODES
            else "sync")
    if mode != _requested_mode:
        logger.warning(
            "delegate_to_agent: unrecognised mode (not one of %s) — coerced to "
            "'sync'; agent=%s. The tool schema should have rejected this.",
            sorted(_KNOWN_MODES), _known_role(agent_name),
        )

    # Concierge is the sole server-authorized voice handoff role.  Do this
    # synchronously, before `_prelaunch` reaches a plugin/limiter await, so a
    # live voice turn reserves foreground ownership before any work can race
    # ahead.  Task 3 owns durable bind/commit and will consume this object.
    mode, handoff_reservation, static_error = validate_voice_handoff_static(
        agent_name, origin, mode,
    )
    if static_error is not None:
        return static_error
    if handoff_reservation is not None:
        if handoff_reservation.reserve() is False:
            return _result({
                "status": "error",
                "kind": "handoff_after_speech",
                "message": (
                    "A specialist handoff cannot start after this voice turn "
                    "has begun speaking."
                ),
            })

    # A4: THE unified prelaunch pipeline — one call that runs EVERY
    # pre-launch gate (ACL, not-initialized, input-size bounds, depth, mode,
    # target resolution, requires-seam, concurrency, progress) in a fixed
    # order, so no gate is bypassable by another and NO side effect
    # (delegation record, task, progress emission) can precede a clean
    # return. The input-size bounds
    # live INSIDE _prelaunch (after the ACL) so an unauthorized caller is
    # denied `delegation_not_declared` regardless of payload size and no
    # telemetry is ever keyed on a caller-supplied target pre-auth. `permit`
    # (Task 6, spec §4.6) is the concurrency slot this delegation now owns —
    # the caller owns its lifetime from here: it is released via the task
    # done-callback.
    try:
        # #433: `agent_name` is REBOUND to the canonical role id — the ACL
        # accepts a persona display name, and every downstream record,
        # telemetry key, engagement scope and launch target must use the role.
        agent_name, cfg, resolution, permit, prelaunch_error = await _prelaunch(
            agent_name, origin, mode, task_text, context_text)
    except BaseException:
        if handoff_reservation is not None:
            handoff_reservation.release()
        raise
    if prelaunch_error is not None:
        if handoff_reservation is not None:
            handoff_reservation.release()
        return prelaunch_error
    # The configurator always runs detached. A commit
    # that reloads the caller would otherwise cancel a run its turn awaits.
    mode = _delegation_mode_for(cfg, mode)

    # Task 6 (spec §4.6): lexical ownership guard. `owned` holds the permit
    # from _prelaunch's acquire until launch transfers it. The finally
    # releases on ANY exit before transfer — including a CancelledError
    # raised at an await between here and launch (which `except Exception`
    # would miss), and every early error return. At each true ownership
    # transfer the body sets `owned = None`: for sync/async the moment the
    # task's `_permit_release_callback` done-callback is attached (which then
    # becomes the sole release), or an accepted voice job's hand-off.
    # Every release is idempotent, so an overlapping release on the SAME
    # permit is a safe no-op.
    owned = permit
    _lane_reservation = None  # the lane queue place, until transferred
    _task_input_open = None   # a task's input destination, until its run owns it
    try:
        is_voice = str(origin.get("channel", "")) == "voice"
        if is_voice and mode == "async":
            handoff = _PermitHandoff()
            try:
                result = await _start_voice_async_job(
                    cfg=cfg,
                    specialist_role=agent_name,
                    task_text=task_text,
                    context_text=context_text,
                    origin=origin,
                    resolution=resolution,
                    permit=permit,
                    handoff=handoff,
                    handoff_reservation=handoff_reservation,
                )
                if handoff_reservation is not None:
                    try:
                        launch_status = json.loads(
                            result["content"][0]["text"],
                        ).get("status")
                    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
                        launch_status = None
                    if launch_status == "error":
                        # The real launch path releases before returning an
                        # error; preserve that contract at this seam too.
                        handoff_reservation.release()
            finally:
                if handoff.transferred:
                    owned = None  # __TRANSFER_VOICE_JOB__
            return result

        delegation_id = str(uuid.uuid4())

        # A4: synchronous voice turn budget. The deadline is ABSOLUTE
        # (origin["voice_deadline"], a monotonic loop.time() value); remaining
        # wait is recomputed because loop.time() advances across awaits. Async
        # voice returned above and is deliberately not subject to this gate.
        # Non-voice turns use the general 60s ceiling below.
        loop = asyncio.get_running_loop() if is_voice else None
        raw_deadline = origin.get("voice_deadline") if is_voice else None

        if is_voice:
            # Pre-register short-circuit: an already-expired / missing /
            # non-finite deadline never even registers a record (keeps the
            # tombstone store clean for the common "spoke too late" case).
            # Task 6: no task is created on this path — the outer finally
            # (`owned` still set) releases the permit.
            if _voice_wait_from_deadline(raw_deadline, loop) is None:
                logger.info(
                    "Delegation → %s voice budget already exceeded at entry — "
                    "not registered", agent_name,
                )
                return _deadline_exceeded_result(delegation_id, agent_name)

        started_at = time.time()
        # Every delegation to an agent with a lane (a specialist, the
        # configurator) is one use of that agent's lane — shared with its
        # taps, its continuations and every other delegation to it, whatever
        # chat the work came from — reserved on the lane's queue BEFORE any
        # task exists. An agent out of service refuses every use at once —
        # sync, degraded and async alike (an async launch must not return
        # ``pending`` for work that will never run).
        import agent_lane
        _lane = agent_lane.LANES.get(agent_name) if agent_lane.has_lane(cfg) else None
        if _lane is not None and _lane.faulted:
            return _result({
                "status": "error",
                "kind": "agent_faulted",
                "agent": agent_name,
                "message": agent_lane.faulted_line(_display_name_for_role(agent_name)),
            })
        if _lane is not None:
            _lane_reservation = _lane.reserve()
        if _lane is not None and _lane_reservation is None:
            return _result({
                "status": "error",
                "kind": "busy",
                "agent": agent_name,
                "message": (
                    f"Agent {agent_name!r} has a full queue — try again shortly."
                ),
            })
        # The delegation is a task: where its posts go and who receives its
        # result are decided here, once. A voice delegation keeps its own
        # path (its result is spoken; it posts nothing).
        _birth = None if is_voice else _task_birth(
            delegation_id, task_text, agent_name, origin)
        # the delegate's own chat its task shows in, named in the result's notes
        _shown = _own_chat_name(_birth)
        # #1038 §7, §3.3: the brief is model text STORED for a later turn to
        # narrate — ANY delegation can detach (a sync one degrades to pending
        # at the wait), so every launch resolves its note now. The note rides
        # the record's origin as a plain string (it survives persistence),
        # and the child runs under a scope VIEW carrying it, never the
        # parent's live obligations (a parent Read after launch must not
        # un-annotate a brief written unread).
        _launch_scope = origin.get("turn_scope")
        if not isinstance(_launch_scope, TurnScope):
            _launch_scope = None
        _rec_origin, _note = _launch_note(origin, task_text)
        record = DelegationRecord(
            id=delegation_id, agent=agent_name, started_at=started_at,
            origin=_rec_origin, **_task_record_fields(_birth),
        )
        # Task 6 (spec §4.6): stash the permit on the record for
        # observability only. The SOLE release for this legacy path is the
        # `_permit_release_callback` done-callback attached to the task below
        # (robust even if the task is cancelled before its coroutine starts);
        # `SpecialistRegistry` terminal transitions deliberately do NOT
        # release (that would race the voice teardown's `cancel_delegation`
        # against a still-unwinding task).
        record.permit = permit
        await _specialist_registry.register_delegation(record)

        voice_wait_s: float | None = None
        if is_voice:
            # RECOMPUTE after register_delegation — its tombstone lock + I/O
            # consumed wall-clock time, so the pre-register wait is now stale.
            # If registration itself ate the remaining budget, remove the
            # just-registered record (no orphan tombstone) and bail WITHOUT
            # ever launching the specialist task. Task 6: no task created →
            # the outer finally releases the permit (owned still set).
            voice_wait_s = _voice_wait_from_deadline(raw_deadline, loop)
            if voice_wait_s is None:
                # #671: this cancel is caused by the voice budget, NOT by a
                # process stop — no task was ever created and the caller is told
                # synchronously below. Latch the cause so a stop landing in this
                # window cannot defer the row and have the next boot announce
                # "Lost on restart" for work that never started.
                _specialist_registry.job_registry.note_cancel_cause(
                    delegation_id, "voice_budget_prelaunch")
                await _specialist_registry.cancel_delegation(delegation_id)
                logger.info(
                    "Delegation %s → %s voice budget exhausted during "
                    "registration — specialist not started",
                    delegation_id[:8], agent_name,
                )
                return _deadline_exceeded_result(delegation_id, agent_name)

        if _birth is not None and _birth.in_own_chat:
            # A task of an agent with its own open chat: its post index and its
            # input destination open first, then its start line there.
            _open_task(_birth)
            _task_input_open = delegation_id
            # this run's closing message is posted to the person
            _ANSWER_IN_CHAT.add(delegation_id)
            # a task Casa started (a plugin's setup or erase turn) has no asker to name:
            # only its answer shows
            from provenance import CASA_PLUGIN_TURN_TARGETS
            if origin.get("synthetic") not in CASA_PLUGIN_TURN_TARGETS:
                await _post_task_start(_birth)

        # Task 6 (spec §4.6): create the task, then ATOMICALLY (no await between)
        # attach the unconditional permit-release done-callback and transfer
        # ownership (`owned = None`). Attaching the release as a done-callback —
        # not the coroutine's own finally — means even a task cancelled before
        # its coroutine ever runs still releases the slot. Clear `owned` BEFORE
        # counting the launch so a telemetry failure can never reach the outer
        # finally to release the now-live task's permit (the count goes through
        # the non-raising wrapper).
        # #541: bind the quota key into the task's context snapshot (reset
        # immediately — only the created task carries it forward).
        _qk_tok = _delegation_quota_key.set(delegation_id)
        # #1038 §3.3: bind origin_var to a FROZEN COPY of this handler's entry
        # snapshot for the duration of create_task — the same pattern as the
        # quota key above — so the child's context binds the copy (with its
        # scope view), never the pooled holder the next turn rewrites.
        import agent as _agent_mod
        _child_origin = dict(origin)
        if _launch_scope is not None:
            _child_origin["turn_scope"] = TurnScope.for_child(
                _launch_scope, _note, synchronous=(mode != "async"))
        if _birth is not None:
            # the task's own delivery, never one inherited from the turn
            # that launched it
            _child_origin.update(task_delivery_marker(_birth.delivery))
        _ov_tok = _agent_mod.origin_var.set(_child_origin)
        try:
            if _lane is not None:
                _run_coro = _delegation_in_lane(
                    _lane, _lane_reservation, cfg, task_text, context_text,
                    resolution=resolution,
                    output_format=(VOICE_JOB_OUTPUT_FORMAT if is_voice else None))
            else:
                _run_coro = _run_delegated_agent_bounded(
                    cfg, task_text, context_text, resolution=resolution,
                    output_format=(VOICE_JOB_OUTPUT_FORMAT
                                   if is_voice else None))
            task = asyncio.create_task(_run_coro)
        finally:
            _agent_mod.origin_var.reset(_ov_tok)
            _delegation_quota_key.reset(_qk_tok)
        if _lane_reservation is not None:
            # a task cancelled before its coroutine starts has no finally
            task.add_done_callback(_lane_reservation.release)
            _lane_reservation = None  # __TRANSFER_LANE_RESERVATION__
        if _task_input_open is not None:
            # the task's input ends with its run, however it ends
            task.add_done_callback(functools.partial(_end_task_input, _task_input_open))
            _task_input_open = None
        if permit is not None:
            task.add_done_callback(_permit_release_callback(permit))
        owned = None  # __TRANSFER_SYNC__
        _record_launch_safe(agent_name)

        if mode == "async":
            _attach_completion_callback(task, record, birth=_birth)
            logger.info(
                "Delegation %s → %s (async mode)",
                delegation_id[:8], agent_name,
            )
            return _result({
                "status": "pending",
                "delegation_id": delegation_id,
                "agent": agent_name,
                "mode": "async",
                **({"casa_note": _note} if _note else {}),
                **_own_chat_pending(_birth, agent_name),
            })

        # mode == "sync"
        if voice_wait_s is not None:
            # A4: voice never degrades to `pending` (async without a capable
            # route is already rejected in _prelaunch) — there is no follow-up channel
            # to deliver a LATER completion notification on. Wait only up to
            # the budget computed above (deadline − reserve, capped at 60s); on
            # expiry cancel + bounded teardown + speak the typed error.
            try:
                done, pending = await asyncio.wait({task}, timeout=voice_wait_s)
            except asyncio.CancelledError:
                task.cancel()
                await _specialist_registry.cancel_delegation(delegation_id)
                raise
            if pending:
                return await _voice_deadline_exceeded(task, delegation_id, agent_name)
        else:
            try:
                done, pending = await asyncio.wait({task}, timeout=_SYNC_WAIT_TIMEOUT_S)
            except asyncio.CancelledError:
                task.cancel()
                await _specialist_registry.cancel_delegation(delegation_id)
                raise

            if pending:
                # 60s elapsed; detach and degrade to pending with callback.
                _attach_completion_callback(task, record, birth=_birth)
                logger.info(
                    "Delegation %s → %s timed out at 60s — degraded to pending",
                    delegation_id[:8], agent_name,
                )
                return _result({
                    "status": "pending",
                    "delegation_id": delegation_id,
                    "agent": agent_name,
                    "timeout_s": 60,
                    "note": (
                        "Delegation continues in background; you will receive a "
                        "NOTIFICATION when complete."
                    ),
                    **({"casa_note": _note} if _note else {}),
                    **_own_chat_pending(_birth, agent_name),
                })

        # Task finished within budget — return ok or error synchronously.
        finished = next(iter(done))
        # #1207: what the child's run left of the operator's approvals, onto
        # the launching turn's scope by delegation id, BEFORE this returns —
        # the CLI writes the result only after, so the resident's fold finds
        # it at this call's result, whichever payload below it carries, and
        # whether or not the result it reads still names that id (#1274).
        _child_scope = _child_origin.get("turn_scope")
        if _launch_scope is not None and isinstance(_child_scope, TurnScope):
            _outcome = tuple(
                ("consumed", key) for kind, key in _child_scope.approvals.values()
                if kind == "consumed") + tuple(
                ("pending", key) for key in _child_scope.approval_waiting)
            if _outcome:
                _launch_scope.delegated_approvals[delegation_id] = _outcome
        if finished.exception() is not None:
            exc = finished.exception()
            kind = _delegation_failure_kind(exc)
            elapsed = time.time() - started_at
            if is_voice:
                failure = JobFailure(
                    kind=kind,
                    message="Specialist could not complete the voice job.",
                )
                await _specialist_registry.job_registry.fail_compat(
                    delegation_id, failure)
                logger.info(
                    "Delegation %s → %s failed status=failed kind=%s (%.2fs)",
                    delegation_id[:8], agent_name, kind, elapsed,
                )
                return _result(_with_post_echo_payload({
                    "status": "error",
                    "delegation_id": delegation_id,
                    "agent": agent_name,
                    "kind": kind,
                    "message": failure.message,
                    "elapsed_s": elapsed,
                }, delegation_id))

            # The typed kind the caller is told below is also what the ledger
            # records — `fail_delegation(id, exc)` would persist the exception
            # CLASS NAME instead (#709); a failed write retries through
            # failure reconciliation carrying this same typed failure.
            await _fail_delegation_durably(
                delegation_id, JobFailure(kind=kind, message=str(exc)))
            await _settle_task(_birth, failure=kind)
            logger.info(
                "Delegation %s → %s failed: %s (%s)",
                delegation_id[:8], agent_name, kind, exc,
            )
            return _result(_with_post_echo_payload({
                "status": "error",
                "delegation_id": delegation_id,
                "agent": agent_name,
                "kind": kind,
                "message": str(exc),
                "elapsed_s": elapsed,
            }, delegation_id, shown_in=_shown))

        delegated_output = finished.result()
        voice_meta: dict = {}
        if is_voice:
            # See _run_voice_job_lifecycle: a run the CLI aborted has no
            # envelope to be malformed, and must not be reported as one (#254).
            if delegated_output.run_aborted:
                failure = JobFailure(
                    kind=_run_abort_kind(delegated_output.run_subtype),
                    message="Specialist could not complete the voice job.",
                )
                await _specialist_registry.job_registry.fail_compat(
                    delegation_id, failure)
                elapsed = time.time() - started_at
                logger.warning(
                    "Delegation %s → %s aborted by the CLI kind=%s subtype=%s "
                    "status=failed (%.2fs)",
                    delegation_id[:8], agent_name, failure.kind,
                    _known_token(
                        delegated_output.run_subtype, _KNOWN_RESULT_SUBTYPES),
                    elapsed,
                )
                return _result(_with_post_echo_payload({
                    "status": "error",
                    "delegation_id": delegation_id,
                    "agent": agent_name,
                    "kind": failure.kind,
                    "message": failure.message,
                    "elapsed_s": elapsed,
                }, delegation_id))

            try:
                voice_result = parse_voice_job_result(
                    delegated_output.structured_output)
            except VoiceJobResultError as exc:
                failure = JobFailure(
                    kind="invalid_specialist_result",
                    message="Specialist returned an invalid structured result.",
                )
                await _specialist_registry.job_registry.fail_compat(
                    delegation_id, failure)
                elapsed = time.time() - started_at
                logger.warning(
                    "Delegation %s → %s returned an invalid voice result "
                    "reason=%s status=failed (%.2fs)",
                    delegation_id[:8], agent_name, exc, elapsed,
                )
                return _result(_with_post_echo_payload({
                    "status": "error",
                    "delegation_id": delegation_id,
                    "agent": agent_name,
                    "kind": failure.kind,
                    "message": failure.message,
                    "elapsed_s": elapsed,
                }, delegation_id))

            # The validated structured envelope is durable job data, not concierge
            # context. Persist it once, then resolve the only text allowed onto
            # the voice tool wire from sensitivity + server-bound identity.
            await _specialist_registry.job_registry.finish_voice_result(
                delegation_id,
                json.dumps(delegated_output.structured_output),
                awaiting_input=voice_result.awaiting_input,
                delivery_ttl_s=voice_result.delivery_ttl_s,
            )
            identity_clearance = voice_identity_clearance(origin)
            text = spoken_text_for(
                voice_result,
                prompted=False,
                identity_clearance=identity_clearance,
            )
            # #352: the concierge is prompted to vary speech for tentative /
            # not-found / dependency-unavailable outcomes — surface the
            # machine-readable classification, not just rendered text. The
            # envelope CONTENT (citations/assumptions) stays behind the same
            # disclosure gate as the spoken summary; the classification
            # itself is not sensitive.
            voice_meta["specialist_status"] = voice_result.status
            if (voice_result.sensitivity != "private"
                    or identity_clearance == "private"):
                # This tool response is an explicitly bounded boundary
                # (`text` goes through truncate_output below) — the
                # advisory metadata must not ride it unbounded, since the
                # schema puts no item/length limits on these arrays.
                voice_meta["citations"] = _bounded_str_list(
                    voice_result.citations)
                voice_meta["assumptions"] = _bounded_str_list(
                    voice_result.assumptions)
        else:
            # Cluster S (#675): the abort check sits BEFORE truncation and
            # `complete_delegation`, mirroring the voice arm above (#254) —
            # an aborted run can therefore never reach
            # `schedule_completion_reconciliation`; the hole is closed
            # structurally, not by another gate. No partial text on the wire:
            # the streamed prefix is not an answer.
            if delegated_output.run_aborted:
                failure = _run_abort_failure(delegated_output)
                await _fail_delegation_durably(delegation_id, failure)
                await _settle_task(_birth, failure=failure.kind)
                elapsed = time.time() - started_at
                logger.warning(
                    "Delegation %s → %s aborted by the CLI kind=%s subtype=%s "
                    "status=failed (%.2fs)",
                    delegation_id[:8], agent_name, failure.kind,
                    _known_token(
                        delegated_output.run_subtype, _KNOWN_RESULT_SUBTYPES),
                    elapsed,
                )
                return _result(_with_post_echo_payload({
                    "status": "error",
                    "delegation_id": delegation_id,
                    "agent": agent_name,
                    "kind": failure.kind,
                    "message": failure.message,
                    "elapsed_s": elapsed,
                }, delegation_id, shown_in=_shown))
            text = delegated_output.text
        # Task 6 (spec §4.6): bound the synchronous result + expose the flag on
        # the wire so the narrating resident can disclose a clipped answer.
        original_text_length = len(text)
        text, output_truncated = specialist_limits.truncate_output(text)
        if output_truncated:
            logger.warning(
                "delegated agent %s output truncated: %d > %d chars (spec §4.6)",
                agent_name, original_text_length, specialist_limits._MAX_OUTPUT_CHARS,
            )
        # The closing message travels beside a result the cap cut, as its own
        # key (the result text keeps the cap); a voice run has its own
        # spoken-text bound and carries none.
        closing = ("" if is_voice else
                   _closing_beyond_cut(delegated_output, text, output_truncated))
        # S3 §6: what the plugin posted under this delegation, as body-free
        # Casa lines after the answer (after the cap: not the delegate's text).
        text = _with_post_echo(text, delegation_id, shown_in=_shown)
        if not is_voice:
            text = _with_silent_close_note(text, delegated_output, agent_name,
                                           own_chat=bool(_shown))
            _answer = _closing_text(delegated_output)
            text = _with_own_chat_answer_note(text, _shown, _answer_posts(_birth, _answer))
            text = _with_operator_words(text, _take_said(delegation_id), agent_name)
        if not is_voice:
            # #321: the specialist's work is DONE and its answer is in hand —
            # a failed terminal snapshot write must not raise it away (the
            # caller would lose the result and restart recovery would ORPHAN
            # the job, discarding the success). Return the result; the
            # durable record is completed by the registry-owned retry.
            try:
                await _specialist_registry.complete_delegation(delegation_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Delegation %s → %s completed but the terminal write "
                    "failed (%s); scheduling completion reconciliation",
                    delegation_id[:8], agent_name, exc,
                )
                try:
                    (_specialist_registry.job_registry
                     .schedule_completion_reconciliation(delegation_id))
                except Exception:  # noqa: BLE001 — restart recovery remains authoritative
                    logger.error(
                        "Delegation %s completion reconciliation scheduling "
                        "failed; restart recovery required", delegation_id[:8],
                    )
            # the delegate's closing message, whole, in its own chat
            await _settle_task(_birth, answer=_answer)
        elapsed = time.time() - started_at
        logger.info(
            "Delegation %s → %s ok (%.2fs)",
            delegation_id[:8], agent_name, elapsed,
        )
        return _result({
            "status": "ok",
            "delegation_id": delegation_id,
            "agent": agent_name,
            "elapsed_s": elapsed,
            "text": text,
            "output_truncated": output_truncated,
            **({"closing_message": closing} if closing else {}),
            **voice_meta,
        })
    finally:
        if owned is not None:
            owned.release()
        # the lane queue place, when still lexically owned (never
        # transferred to a created task — a cancel during registration).
        if _lane_reservation is not None:
            _lane_reservation.release()
        # a task whose run was never created: its input ends here
        if _task_input_open is not None:
            _end_task_input(_task_input_open)


async def _delegation_in_lane(lane: Any, reservation: Any, cfg: Any, task_text: str,
                              context_text: str, *, resolution: Any = None,
                              output_format: Any = None) -> "DelegatedOutput":
    """One delegation as one use of its agent's lane: the lock first (the
    queue place released on admission, or on any earlier exit), the fault
    re-read under it (a delegation queued before the fault must not run),
    the run, and — before the lock is released — a run still unwinding past
    the bounded runner's teardown holds the lane until it ends. A
    configurator delegation's whole wait, lane then lease, is one bound."""
    import agent_lane
    import configurator_run
    deadline = configurator_run.wait_deadline() if configurator_run.is_configurator(cfg) else None
    try:
        async with lane.use(reservation, deadline) as admitted:
            if not admitted:
                raise configurator_run.busy_error()
            if lane.faulted:
                raise agent_lane.LaneFaulted(
                    agent_lane.faulted_line(_display_name_for_role(cfg.role)))
            runs: list = []
            sink_tok = _lane_run_sink.set(runs)
            try:
                with (configurator_run.waiting_until(deadline) if deadline is not None
                      else nullcontext()):
                    return await _run_delegated_agent_bounded(
                        cfg, task_text, context_text, resolution=resolution,
                        output_format=output_format)
            finally:
                _lane_run_sink.reset(sink_tok)
                lane.hold_unwinding(runs)
    finally:
        if reservation is not None:
            reservation.release()


# ---------------------------------------------------------------------------
# Browser executor §5.4: the general hand-off — any specialist answering a
# request hands part of it to any other specialist and waits for the answer.
# ---------------------------------------------------------------------------

_HANDOFF_EXPECTED_MAX_CHARS: int = 1_000


def _handoff_unavailable(message: str) -> dict:
    return _result({"status": "error", "kind": "handoff_unavailable",
                    "message": message})


def _handoff_target(agent_name: str) -> "tuple[str | None, dict | None]":
    """The specialist role *agent_name* names — its role id, else its persona
    display name when exactly one specialist carries it — or a refusal. The
    candidate set is every loaded specialist: a hand-off needs no
    declaration (design §5.4, "any specialist, present or future")."""
    specialists = {r for r, c in _agent_role_map.items()
                   if getattr(c, "kind", "") == "specialist"}
    if agent_name in specialists:
        return agent_name, None
    candidates = _declared_name_candidates(agent_name, specialists)
    if len(candidates) == 1:
        return candidates[0], None
    if len(candidates) > 1:
        return None, _result({
            "status": "error", "kind": "delegation_ambiguous_name",
            "candidates": candidates,
            "message": (f"{agent_name!r} is the display name of more than one "
                        f"specialist ({', '.join(candidates)}) — pass the role id."),
        })
    return None, _result({
        "status": "error", "kind": "unknown_agent",
        "specialists": [{"role": r, "name": _display_name_for_role(r)}
                        for r in sorted(specialists)],
        "message": f"No specialist named {agent_name!r}.",
    })


@tool(
    "hand_off",
    "Hand part of what you are working on — a request you are answering, or "
    "what you were asked in your own chat — to another specialist and wait "
    "for its answer, which comes back to you (not to the person). Use it when "
    "another specialist is the one who can do that part — say what a correct "
    "result looks like in expected_result. The answer is yours to check and to "
    "use in your own reply.",
    {"type": "object",
     "properties": {
         "agent": {"type": "string"},
         "task": {"type": "string"},
         "context": {"type": "string"},
         "expected_result": {"type": "string"},
     },
     "required": ["agent", "task"]},
)
async def hand_off(args: dict) -> dict:
    """Run one hand-off synchronously and return the target's answer.

    Available to a specialist in its delegated or pinned run and in a turn of
    its own chat (never an engagement, a job, a stored-call turn or a
    voice-originated run). The bounds are mechanical
    (``handoff.handoff_refusal``): no agent twice in the chain, at most
    ``MAX_HANDOFFS`` hand-offs after the agent the chain started from. The run
    is ephemeral like any delegation, writes no job row and posts no hand-off
    line — the requester is waiting on it, and the requester's own ceiling
    does not run while it waits (``_bounded_wait``). Inside a delegated chain
    it holds only its per-scope permit (the chain's root delegation holds the
    global slot); from the specialist's own chat no delegation holds one, so
    the hand-off is the chain's root and takes the full permit."""
    from handoff import handoff_chain, handoff_refusal

    origin = _snapshot_origin()
    if engagement_var.get(None) is not None or origin.get("stored_call"):
        return _handoff_unavailable(
            "hand_off is not available in an engagement, a job or a tapped call.")
    if str(origin.get("channel", "")) == "voice":
        return _handoff_unavailable("hand_off is not available on voice.")
    caller = str(origin.get("execution_role") or "")
    caller_cfg = _agent_role_map.get(caller)
    if caller_cfg is None or getattr(caller_cfg, "kind", "") != "specialist":
        return _handoff_unavailable("hand_off is available to specialists only.")

    fields = {k: args.get(k, "") if args.get(k) is not None else ""
              for k in ("agent", "task", "context", "expected_result")}
    for _field, _value in fields.items():
        if not isinstance(_value, str):
            return _result({"status": "error", "kind": "invalid_argument",
                            "field": _field,
                            "message": f"{_field} must be a string."})
    task_text, context_text = fields["task"], fields["context"]
    expected = fields["expected_result"].strip()
    if not task_text.strip():
        return _result({"status": "error", "kind": "invalid_argument",
                        "field": "task", "message": "task must not be empty."})
    for _field, _value, _limit in (
            ("task", task_text, specialist_limits._MAX_TASK_CHARS),
            ("context", context_text, specialist_limits._MAX_CONTEXT_CHARS),
            ("expected_result", expected, _HANDOFF_EXPECTED_MAX_CHARS)):
        if len(_value) > _limit:
            return _result({"status": "error", "kind": "input_too_large",
                            "field": _field, "length": len(_value),
                            "limit": _limit,
                            "message": f"{_field} exceeds the {_limit}-char limit."})

    target, refusal = _handoff_target(fields["agent"])
    if refusal is not None:
        return refusal
    chain = handoff_chain(origin)
    bound = handoff_refusal(chain, target)
    if bound is not None:
        return _result(bound)
    cfg = _agent_role_map[target]
    resolution, requires_error = await _requires_gate(target, cfg)
    if requires_error is not None:
        return requires_error

    permit = None
    if _specialist_limiter is not None:
        # a turn of the caller's own chat runs under no delegation
        _in_chain = int(origin.get("delegation_depth", 0) or 0) > 0
        _acquire = (_specialist_limiter.try_acquire_scope_only if _in_chain
                    else _specialist_limiter.try_acquire)
        permit = _acquire(_delegation_scope(origin, target))
        if permit is None:
            return _result({
                "status": "error", "kind": "busy", "agent": target,
                "message": (f"{_display_name_for_role(target)} is already "
                            "working on something for this chat — try again "
                            "shortly."),
            })
    try:
        if expected:
            context_text = (
                f"{context_text}\n\n" if context_text else ""
            ) + (f"What {_display_name_for_role(caller)} will accept as a "
                 f"correct result:\n{expected}")
        handoff_id = str(uuid.uuid4())
        _launch_scope = origin.get("turn_scope")
        _, _note = _launch_note(origin, task_text)
        import agent as _agent_mod
        _child_origin = dict(origin)
        if isinstance(_launch_scope, TurnScope):
            _child_origin["turn_scope"] = TurnScope.for_child(
                _launch_scope, _note, synchronous=True)
        # The target's surfaces go to its own chat, decided now, never to the
        # chat of the specialist that handed the work on.
        _child_origin.update(task_delivery_marker(
            _task_birth(handoff_id, task_text, target, origin).delivery))
        _qk_tok = _delegation_quota_key.set(handoff_id)
        _ov_tok = _agent_mod.origin_var.set(_child_origin)
        try:
            task = asyncio.create_task(_run_delegated_agent_bounded(
                cfg, task_text, context_text, resolution=resolution))
        finally:
            _agent_mod.origin_var.reset(_ov_tok)
            _delegation_quota_key.reset(_qk_tok)
        if permit is not None:
            # released when the run ACTUALLY ends, as the sync arm's is
            task.add_done_callback(_permit_release_callback(permit))
            permit = None
        _record_launch_safe(target)
        started = time.monotonic()
        logger.info("Hand-off %s %s → %s (chain length %d)",
                    handoff_id[:8], _known_role(caller), _known_role(target),
                    len(chain) + 1)
        try:
            with _ceiling_paused():
                output = await task
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 — typed to the caller
            kind = _classify_error(exc).value
            logger.info("Hand-off %s → %s failed: %s", handoff_id[:8],
                        _known_role(target), kind)
            return _result(_with_post_echo_payload({
                "status": "error", "agent": target, "kind": kind,
                "message": str(exc),
                "elapsed_s": time.monotonic() - started,
            }, handoff_id))
        if output.run_aborted:
            failure = _run_abort_failure(output)
            return _result(_with_post_echo_payload({
                "status": "error", "agent": target, "kind": failure.kind,
                "message": failure.message,
                "elapsed_s": time.monotonic() - started,
            }, handoff_id))
        text, output_truncated = specialist_limits.truncate_output(output.text)
        text = _with_post_echo(text, handoff_id)
        return _result({
            "status": "ok", "agent": target, "text": text,
            "output_truncated": output_truncated,
            "elapsed_s": time.monotonic() - started,
        })
    finally:
        if permit is not None:
            permit.release()


# ---------------------------------------------------------------------------
# Metadata-only voice job control. Full case/result data never crosses these
# tool envelopes; continuation injects it directly into the specialist child.
# ---------------------------------------------------------------------------


def _bounded_str_list(
    values, *, max_items: int = 10, max_chars: int = 400,
) -> list[str]:
    """Cap advisory metadata lists (citations/assumptions) for the wire.

    The voice-result schema bounds neither item count nor item length, so
    without this a specialist could attach megabytes of metadata to a
    one-line answer at a boundary that truncates `text` (spec §4.6).
    """
    return [value[:max_chars] for value in values[:max_items]]


def _job_not_found_result() -> dict:
    return _result({
        "status": "error",
        "kind": "job_not_found",
        "message": "No matching specialist job is available.",
    })


def _no_matching_job_result() -> dict:
    return _result({
        "status": "error",
        "kind": "no_matching_job",
        "message": "No eligible specialist job is available.",
    })


def _ambiguous_job_result(jobs: list[VoiceJob]) -> dict:
    return _result({
        "status": "error",
        "kind": "ambiguous_job",
        "message": "More than one specialist job matches; choose a job ID.",
        "choices": [
            {
                "job_id": job.id,
                "specialist_display_name": job.specialist_display_name,
            }
            for job in jobs
        ],
    })


def _requested_job_id(args: dict) -> str | None:
    raw = args.get("job_id")
    if not isinstance(raw, str):
        return None
    normalized = raw.strip()
    return normalized or None


def _authorized_jobs(registry, actor: JobActor) -> list[VoiceJob]:
    return [job for job in registry.all() if _actor_owns_job(actor, job)]


def _resolve_authorized_job(
    registry,
    actor: JobActor,
    requested_id: str | None,
    *,
    predicate,
    explicit_mismatch_kind: str = "job_not_found",
) -> tuple[VoiceJob | None, dict | None]:
    if requested_id is not None:
        job = registry.get(requested_id)
        if job is None or not _actor_owns_job(actor, job):
            return None, _job_not_found_result()
        if not predicate(job):
            if explicit_mismatch_kind == "job_not_continuable":
                return None, _result({
                    "status": "error",
                    "kind": "job_not_continuable",
                    "message": "That specialist job cannot be continued.",
                })
            return None, _job_not_found_result()
        return job, None

    matches = [
        job for job in _authorized_jobs(registry, actor)
        if predicate(job)
    ]
    if not matches:
        return None, _no_matching_job_result()
    if len(matches) != 1:
        return None, _ambiguous_job_result(matches)
    return matches[0], None


def _job_status_value(job: VoiceJob) -> str:
    if job.execution_state is ExecutionState.ACCEPTED:
        return "pending"
    if job.execution_state is ExecutionState.ORPHANED:
        return "failed"
    return job.execution_state.value.lower()


def _job_metadata(job: VoiceJob) -> dict:
    delivery_status = job.delivery_state.value.lower()
    routes = getattr(_runtime, "voice_route_registry", None)
    connected_route = (
        routes.get_connected(job.origin_route_id)
        if routes is not None and job.origin_route_id else None
    )
    connected_capabilities = getattr(connected_route, "capabilities", ())
    if (
        job.delivery_state is DeliveryState.READY
        and job.origin_route_id
        and routes is not None
        and (
            connected_route is None
            # Transport-level only now: endpoint capability lives on the job
            # (delivery_modality), not on the route (#233/#224).
            or not _ROUTE_TRANSPORT_CAPABILITIES
            <= frozenset(connected_capabilities)
        )
    ):
        delivery_status = "waiting_for_route"
    return {
        "status": _job_status_value(job),
        "job_id": job.id,
        "specialist_display_name": job.specialist_display_name,
        "awaiting_input": bool(job.awaiting_input),
        "delivery_status": delivery_status,
    }


def _status_candidate(job: VoiceJob) -> bool:
    return (
        job.delivery_state not in {
            DeliveryState.DELIVERED,
            DeliveryState.CANCELLED,
            DeliveryState.EXPIRED,
        }
        or job.execution_state in {
            ExecutionState.ACCEPTED,
            ExecutionState.RUNNING,
        }
    )


def _cancellable_job(job: VoiceJob) -> bool:
    return (
        job.execution_state in {
            ExecutionState.ACCEPTED,
            ExecutionState.RUNNING,
        }
        or job.delivery_state in {
            DeliveryState.READY,
            DeliveryState.CLAIMED,
            DeliveryState.AUTHORIZED,
        }
    )


def _continuable_job(job: VoiceJob, *, now: float) -> bool:
    return (
        job.execution_state is ExecutionState.SUCCEEDED
        and job.awaiting_input
        and job.continuable_until is not None
        and job.continuable_until > now
        and (job.expires_at is None or job.expires_at > now)
        and job.delivery_state not in {
            DeliveryState.CANCELLED,
            DeliveryState.EXPIRED,
        }
        and not job.cancel_pending
        and job.result is not None
    )


def _detail_available_job(job: VoiceJob, *, now: float) -> bool:
    available = (
        job.execution_state is ExecutionState.SUCCEEDED
        and job.result is not None
        and (job.expires_at is None or job.expires_at > now)
        and job.delivery_state not in {
            DeliveryState.CANCELLED,
            DeliveryState.EXPIRED,
        }
        and not job.cancel_pending
    )
    if not available:
        return False
    try:
        result = parse_voice_job_result(json.loads(job.result))
    except (json.JSONDecodeError, TypeError, VoiceJobResultError):
        return False
    return result.status != "needs_clarification"


@tool(
    "voice_job_status",
    "Check specialist job status.",
    {"job_id": str},
)
async def voice_job_status(args: dict) -> dict:
    origin = _snapshot_origin()
    actor = _job_actor_from_origin(origin)
    if actor is None or _specialist_registry is None:
        return _job_not_found_result()
    registry = _specialist_registry.job_registry
    await registry.load()
    await registry.expire_due()
    requested_id = _requested_job_id(args)
    if requested_id is not None:
        job = registry.get(requested_id)
        if job is None or not _actor_owns_job(actor, job):
            return _job_not_found_result()
    else:
        job, error = _resolve_authorized_job(
            registry,
            actor,
            None,
            predicate=_status_candidate,
        )
        if error is not None:
            return error
    return _result(_job_metadata(job))


@tool(
    "cancel_voice_job",
    "Cancel a specialist job.",
    {"job_id": str},
)
async def cancel_voice_job(args: dict) -> dict:
    origin = _snapshot_origin()
    actor = _job_actor_from_origin(origin)
    if actor is None or _specialist_registry is None:
        return _job_not_found_result()
    registry = _specialist_registry.job_registry
    await registry.load()
    await registry.expire_due()
    requested_id = _requested_job_id(args)
    if requested_id is not None:
        job = registry.get(requested_id)
        if job is None or not _actor_owns_job(actor, job):
            return _job_not_found_result()
    else:
        job, error = _resolve_authorized_job(
            registry,
            actor,
            None,
            predicate=_cancellable_job,
        )
        if error is not None:
            return error

    if job.delivery_state is DeliveryState.DELIVERED:
        cancel_status = "already_delivered"
    else:
        try:
            cancel_status = (
                await registry.request_cancel(job.id, actor=actor)
            ).status
        except JobAuthorizationError:
            return _job_not_found_result()
    return _result({
        "status": cancel_status,
        "job_id": job.id,
        "specialist_display_name": job.specialist_display_name,
    })


@tool(
    "continue_voice_job",
    "Continue a specialist job with new input.",
    {"job_id": str, "input": str},
)
async def continue_voice_job(args: dict) -> dict:
    origin = _snapshot_origin()
    actor = _job_actor_from_origin(origin)
    if actor is None or _specialist_registry is None:
        return _job_not_found_result()
    continuation_input = args.get("input")
    if not isinstance(continuation_input, str) or not continuation_input.strip():
        return _result({
            "status": "error",
            "kind": "invalid_arguments",
            "message": "input must be a non-empty string.",
        })
    continuation_input = continuation_input.strip()
    if not deferred_delivery_available(origin):
        return _background_delivery_unavailable_result(origin)

    registry = _specialist_registry.job_registry
    await registry.load()
    await registry.expire_due()
    now = time.time()
    requested_id = _requested_job_id(args)
    parent, error = _resolve_authorized_job(
        registry,
        actor,
        requested_id,
        predicate=(
            (lambda job: (
                _continuable_job(job, now=now)
                or _detail_available_job(job, now=now)
            ))
            if requested_id is not None
            else (lambda job: _continuable_job(job, now=now))
        ),
        explicit_mismatch_kind="job_not_continuable",
    )
    if error is not None:
        return error

    if not parent.awaiting_input:
        job_id = str(uuid.uuid4())
        child = _new_voice_job(
            job_id=job_id,
            parent_job_id=parent.id,
            cfg=_agent_role_map.get(parent.specialist_role),
            specialist_role=parent.specialist_role,
            origin=origin,
            task_text=continuation_input,
            context_text="",
            # Task 12: the continuation keeps the ORIGINAL job's creator —
            # never whoever is invoking continue_voice_job now — while
            # executing_speaker (derived above from the current cfg) picks
            # up the specialist's CURRENT binding.
            creating_speaker=parent.creating_speaker,
        )
        try:
            prompted = await registry.create_prompted_delivery(
                parent.id,
                child,
                actor=actor,
                max_active_ready_per_route=_voice_job_route_cap,
            )
        except JobRouteCapacityError:
            return _result({
                "status": "error",
                "kind": "route_capacity_reached",
                "message": (
                    f"This voice route already has {_voice_job_route_cap} "
                    "specialist jobs "
                    "awaiting completion or delivery."
                ),
            })
        except (JobAuthorizationError, JobTransitionError):
            return _result({
                "status": "error",
                "kind": "job_not_continuable",
                "message": "That specialist job cannot be continued.",
            })
        return _result({
            "status": "pending",
            "job_id": prompted.id,
            "specialist_display_name": prompted.specialist_display_name,
        })

    # The complete prior case/result travels backend-to-specialist only. The
    # tool result below contains opaque metadata, never this private envelope.
    # #324: this envelope is internally built and bypasses the caller-facing
    # context bound (`internal_context=True` below), so its own components
    # must stay bounded: task/context were bounded at the parent's prelaunch,
    # but a stored result has no size contract — truncate it here.
    prev_result = parent.result
    if isinstance(prev_result, str) and len(prev_result) > _CONTINUATION_RESULT_CHARS:
        prev_result = prev_result[:_CONTINUATION_RESULT_CHARS] + "\n…[truncated]"
    private_context = json.dumps({
        "parent_job_id": parent.id,
        "original_task": parent.task,
        "original_context": parent.context,
        "previous_result": prev_result,
    }, ensure_ascii=False, separators=(",", ":"))

    # #433: the canonical role is discarded here — this path already passes a
    # stored role id (`parent.specialist_role`), never a model-supplied name,
    # so resolution is necessarily an identity and the rest of this function
    # keys on `parent.specialist_role` directly.
    _canonical_role, cfg, resolution, permit, prelaunch_error = await _prelaunch(
        parent.specialist_role,
        origin,
        "async",
        continuation_input,
        private_context,
        internal_context=True,
    )
    if prelaunch_error is not None:
        return prelaunch_error

    owned = permit
    try:
        handoff = _PermitHandoff()
        try:
            try:
                result = await _start_voice_async_job(
                    cfg=cfg,
                    specialist_role=parent.specialist_role,
                    task_text=continuation_input,
                    context_text=private_context,
                    origin=origin,
                    resolution=resolution,
                    permit=permit,
                    handoff=handoff,
                    parent_job_id=parent.id,
                    # Task 12: copy the ORIGINAL job's creator verbatim —
                    # never whoever is invoking continue_voice_job now.
                    creating_speaker=parent.creating_speaker,
                )
            finally:
                if handoff.transferred:
                    owned = None
        except JobAuthorizationError:
            return _job_not_found_result()
        except JobTransitionError:
            return _result({
                "status": "error",
                "kind": "job_not_continuable",
                "message": "That specialist job cannot be continued.",
            })
        return result
    finally:
        if owned is not None:
            owned.release()


# ---------------------------------------------------------------------------
# list_inbound_files — #1036
# ---------------------------------------------------------------------------


def _age_words(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def _inbox_days() -> int:
    import agent_inbox
    return agent_inbox.RETENTION_S // 86400


def _inbox_for_executing_agent():
    """The inbox of the agent actually running this turn. A delegated agent
    carries its caller's ``role`` in the origin and its own in
    ``execution_role`` (``_run_delegated_agent``), so keying on ``role`` alone
    would hand a delegate its caller's files (#486)."""
    import agent_inbox

    origin = _snapshot_origin() or {}
    role = origin.get("execution_role") or origin.get("role") or ""
    return agent_inbox.get_inbox(role) if role else None


@tool(
    "list_inbound_files",
    "List the files the operator has sent you in Telegram, newest first: the "
    "name they gave it, its kind, size, age, and the path to open it with the "
    "Read tool. Listing a file is not reading it — open it before saying "
    f"anything about its contents. Files are kept for {_inbox_days()} days.",
    {},
)
async def list_inbound_files(args: dict) -> dict:
    _origin = _snapshot_origin()   # #1038: the entry snapshot, like every emitter
    inbox = _inbox_for_executing_agent()
    if inbox is None:
        return _text_result("You have no inbound files — files sent in Telegram "
                            "reach only the agent that receives the operator's DMs.")
    files = await asyncio.to_thread(inbox.list_files)
    if not files:
        return _text_result("The operator hasn't sent you any files in the last "
                            f"{_inbox_days()} days.")
    # #1038: this is the tool that shows the model the paths, so it is the
    # tool that arms the disclosure — from the records it lists, never from
    # the rendered text. Discharged by a successful Read of any listed file,
    # or by handing one to a plugin (#1218). On a turn that received a file of
    # its own, the scope keeps that file alone (``TurnScope.arm``).
    _scope = _current_scope(_origin)
    if _scope is not None:
        _scope.arm(ReadBeforeDescribe(
            files=tuple((f.path, f.display_name) for f in files)))
    now = time.time()
    lines = ["Files the operator sent you, newest first. Listing a file is not "
             "reading it: open one with the Read tool before describing it."]
    for i, f in enumerate(files, 1):
        shown = f'"{f.display_name}"' if f.display_name != f.name else "a photo"
        size = (f"{f.size / (1024 * 1024):.1f} MB" if f.size >= 1024 * 1024
                else f"{max(1, f.size // 1024)} KB")
        lines.append(f"{i}. {shown} — {f.ext.lstrip('.').upper()}, {size}, "
                     f"received {_age_words(now - f.published_at)}\n"
                     f"   path: {f.path}")
    return _text_result("\n".join(lines))


# ---------------------------------------------------------------------------
# share_inbound_file — #486
# ---------------------------------------------------------------------------


@tool(
    "share_inbound_file",
    "Give a plugin a file the operator sent you in Telegram. Pass the path "
    "list_inbound_files shows; you get back a path in Casa's handoff folder, "
    "under the name the operator gave the file, to pass to the plugin tool "
    "that needs it (for example as an email attachment). Call it whenever a "
    "plugin tool needs one of those files — the operator does not need to be "
    "asked. The shared copy is kept 7 days; your own copy is unchanged.",
    {"path": str},
)
async def share_inbound_file(args: dict) -> dict:
    import casa_handoff
    import plugin_handoff

    _origin = _snapshot_origin()   # #1218: the entry snapshot, like every emitter
    inbox = _inbox_for_executing_agent()
    if inbox is None:
        return _text_result("You have no inbound files to share — files sent in "
                            "Telegram reach only the agent that receives the "
                            "operator's DMs.")
    path = args.get("path")
    files = await asyncio.to_thread(inbox.list_files)
    match = next((f for f in files if f.path == path), None)
    if match is None:
        return _text_result("That is not one of your inbound files. Use "
                            "list_inbound_files and pass a path exactly as it "
                            "shows it.")
    root = plugin_handoff.root()
    if root is None:
        return _text_result("The handoff folder is not available, so the file "
                            "cannot be shared with a plugin.")
    # No display name (a photo has none; a document's may have been lost —
    # its meta is best-effort): name it by its kind, never by Casa's name.
    kind = "photo" if match.ext in (".png", ".jpg", ".jpeg") else "file"
    fallback = kind + match.ext
    name = fallback if match.display_name == match.name else match.display_name
    try:
        out = await asyncio.to_thread(
            casa_handoff.publish, "casa", casa_handoff.clean_filename(name, fallback),
            src=match.path, root=root)
    except casa_handoff.HandoffError as exc:
        return _text_result(f"The file could not be shared: {exc}")
    # #1218: the copy exists — handing the file to a plugin acts on it, so it
    # discharges the turn's "(Ellen hasn't opened …)" line as a Read does
    _scope = _current_scope(_origin)
    if _scope is not None:
        _scope.note_handed_off(match.path)
    return _text_result(
        f'Shared "{out["filename"]}". Pass this path to the plugin tool:\n'
        f'{out["path"]}\nThe shared copy is kept {casa_handoff.RETENTION_S // 86400} days.')


# ---------------------------------------------------------------------------
# get_schedule — Phase 3.3
# ---------------------------------------------------------------------------


@tool(
    "get_schedule",
    "Return your upcoming scheduled triggers (interval + cron) within a "
    "time window. Returns a markdown bullet list with name, type, cron/interval "
    "description, and next fire time. Own-role only. These are Casa's own "
    "triggers and reminders for your role — not the operator's calendar or "
    "agenda.",
    {"within_hours": int},
)
async def get_schedule(args: dict) -> dict:
    if _trigger_registry is None:
        return {"content": [{"type": "text",
                             "text": "Error: trigger registry not initialized"}]}

    origin = _snapshot_origin()
    if not origin:
        return {"content": [{"type": "text",
                             "text": "Error: get_schedule called outside a turn context"}]}

    role = origin.get("role") or ""
    if not role:
        return {"content": [{"type": "text",
                             "text": "Error: turn origin has no role"}]}

    raw_hours = args.get("within_hours", 24)
    try:
        within_hours = int(raw_hours) if raw_hours is not None else 24
    except (TypeError, ValueError):
        within_hours = 24
    within_hours = max(1, min(720, within_hours))

    summaries = _trigger_registry.list_jobs_for(
        role=role, within_hours=within_hours,
    )

    if not summaries:
        text = f"(no scheduled triggers in the next {within_hours} hours)"
    else:
        lines = []
        for s in summaries:
            if s.type == "cron":
                desc = f"(cron, `{s.schedule_desc}`)"
            elif s.type == "date":
                # #396: a one-off reminder. Without this branch it would be
                # mislabelled "(interval, once at …)".
                desc = f"(one-off, {s.schedule_desc})"
            else:
                desc = f"(interval, {s.schedule_desc})"
            lines.append(
                f"- **{s.name}** {desc} — next: "
                f"{s.next_fire.isoformat(timespec='seconds')}"
            )
        text = "\n".join(lines)

    return {"content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------------------
# set_reminder / cancel_reminder — #396
#
# A reminder IS a trigger, so these are a narrow front door onto the machinery
# that already exists rather than a parallel system. The bounds that keep them
# from being a general config writer (INV-TRIG-010):
#   * the CALLING role's own triggers.yaml, never another agent's;
#   * only entries carrying ``managed_by: agent`` — the name prefix is NOT the
#     bound, because the schema permits an operator to author a
#     `reminder-`-prefixed dated one-shot of their own;
#   * only reminder-shaped fields.
# ``config_git_commit``'s configurator-only guard is untouched — this is a
# separate, narrower path, not a relaxation of it.
# ---------------------------------------------------------------------------


def _reminder_origin_role() -> str:
    origin = _snapshot_origin()
    return (origin or {}).get("role") or ""


@tool(
    "set_reminder",
    "Set a durable reminder that survives restarts and updates. `text` is the "
    "message the operator will receive: apart from surrounding whitespace it "
    "is delivered to them exactly as written, as plain text with no buttons — "
    "so write the words you want them to read, never an instruction about "
    "what to send and never a promise of a tappable choice this tool cannot "
    "deliver. `at` is the first (or only) occurrence as an ISO-8601 time WITH "
    "a UTC offset — resolve it yourself from what the user said, and echo the "
    "absolute time back to them so a misreading is caught immediately. "
    "`repeat` is one of none (default), daily, weekdays, weekly, monthly; "
    "when the user's request is genuinely ambiguous about repeating, ask them "
    "with tappable buttons before calling this. A repeating reminder must "
    "land on a whole minute, and a monthly one on day 28 or earlier — later "
    "days are missing from some months, so ask the user for a different day "
    "rather than guessing. Own-role only.",
    {"at": str, "text": str, "repeat": str},
)
async def set_reminder(args: dict) -> dict:
    from datetime import datetime

    import reminders

    role = _reminder_origin_role()
    if not role:
        return _result({"status": "error", "kind": "no_turn_context",
                        "message": "set_reminder called outside a turn"})

    text = (args.get("text") or "").strip()
    if not text:
        return _result({"status": "error", "kind": "invalid_argument",
                        "message": "text is required"})
    # #1038 §7: the reminder's text is model text STORED for a later turn to
    # send. Its obligation resolves NOW, at commit, into a note carried beside
    # the entry (``output_note``); the text itself is stored unchanged. The
    # turn that fires it registers the note and prepends it to whatever its
    # model sends, so a paraphrase cannot drop it.
    _scope = _current_scope(_snapshot_origin())
    if _scope is None:
        return _result({"status": "error", "kind": "unsupported_origin",
                        "message": _NO_SCOPE_MESSAGE})
    _note = _scope.admit(IntentKind.STORED, text).note

    from timekeeping import resolve_tz
    tz = resolve_tz()

    repeat = (args.get("repeat") or "none").strip().lower()
    try:
        when = reminders.parse_at(args.get("at") or "")
        # Refuse anything a cron cannot express EXACTLY rather than
        # approximating it — an approximation makes the time the user is told
        # differ from the time that fires.
        reminders.validate_recurring(when, repeat, tz)
        fields = reminders.derive_schedule(when, repeat, tz)
    except ValueError as exc:
        return _result({"status": "error", "kind": "invalid_argument",
                        "message": str(exc)})

    if when <= datetime.now(when.tzinfo):
        return _result({
            "status": "error", "kind": "invalid_argument",
            "message": f"{args.get('at')!r} is in the past",
        })

    runtime = _runtime
    if runtime is None:
        return _result({"status": "error", "kind": "not_initialized",
                        "message": "runtime not wired"})
    cfg = getattr(runtime, "role_configs", {}).get(role)
    if cfg is None:
        return _result({"status": "error", "kind": "unknown_role",
                        "message": f"no config for role {role!r}"})
    channels = list(getattr(cfg, "channels", []) or [])
    if not channels:
        return _result({"status": "error", "kind": "no_channel",
                        "message": f"role {role!r} declares no channel"})
    # Prefer telegram when available; INV-TRIG-001 requires the trigger's
    # channel to be one this role actually declares.
    channel = "telegram" if "telegram" in channels else channels[0]

    path = reminders.triggers_path(runtime.agents_dir, role)
    # Generate against EVERY name already in the file, the operator's included.
    # A collision would fail registration on a duplicate job id, and the
    # rollback below would then delete the PRE-EXISTING entry of that name along
    # with this one.
    try:
        name = reminders.new_reminder_name(reminders.existing_names(path))
    except ValueError as exc:
        return _result({"status": "error", "kind": "name_exhausted",
                        "message": str(exc)})
    entry = {
        "name": name,
        **fields,
        "one_shot": repeat == "none",
        "channel": channel,
        # Provenance as DATA, written here and nowhere else. Everything that
        # may sweep, re-register or delete this entry keys off this field.
        "managed_by": reminders.OWNER_AGENT,
        # Imperative on purpose: a scheduled turn may legitimately produce no
        # output, so delivery must not be left to the model's judgement. This
        # is the morning-briefing silence lesson (v0.132.0) applied.
        # #511: the scheduled turn's closing text is ALSO delivered to the
        # channel (agent.py), so without the `<silent/>` convention every fire
        # costs a second message ("Sent."). The send stays first and
        # imperative — worst case on a disobedient model is the old double
        # message (fail-noisy), never a lost reminder (fail-silent).
        "prompt": (f'Send this exact message via {channel}: "{text}". '
                   "After the send, output the sentinel `<silent/>` and "
                   "nothing else."),
        # #1038: the resolved disclosure, absent when nothing is owed.
        **({"output_note": _note} if _note else {}),
    }

    try:
        # Off the loop: the mutator takes trigger_write_lock.PASS_LOCK, which
        # a config_sync pass may hold, so acquiring it here would stall the
        # loop (#458).
        await asyncio.to_thread(reminders.add_entry, path, entry)
    except (OSError, ValueError) as exc:
        return _result({"status": "error", "kind": "write_failed",
                        "message": str(exc)})

    # Register in-process so the reminder is live NOW, not at the next boot.
    # #1038: through the ONE constructor every reminder route uses, so the
    # spec that fires tonight carries the note without a reload.
    spec = reminders.spec_from_entry(entry)
    try:
        runtime.trigger_registry.register_agent(role, [spec], channels)
    except Exception as exc:  # noqa: BLE001
        # The write now lands off the loop (#458), so a sweep's
        # `_reconcile_registrations` — "the file is the truth; the scheduler a
        # cache of it" — can register this very entry from the file in the gap
        # before we do, making our own register raise "already scheduled". That
        # is not a failure: the reminder is written AND live. Only roll back
        # when no job exists, i.e. the registration genuinely did not happen.
        try:
            already_live = runtime.trigger_registry.has_job(role, name)
        except Exception:  # noqa: BLE001 — treat an unusable registry as not-live
            already_live = False
        if already_live:
            logger.info("reminder set: %s already registered by a concurrent "
                        "reconcile; keeping it", name)
        else:
            # Fail closed: a reminder recorded but never registered would look
            # set and silently not fire until the next restart.
            try:
                await asyncio.to_thread(reminders.remove_entry, path, name)
            except (OSError, ValueError):
                logger.warning("set_reminder: rollback failed for %s", name,
                               exc_info=True)
            # #513: the rollback rewrites the file too, so it owes the same
            # notice as a cancel does.
            try:
                import casa_core
                await casa_core.notify_placeholder_rewrites(_channel_manager)
            except Exception:  # noqa: BLE001
                logger.warning("placeholder-rewrite notice failed",
                               exc_info=True)
            return _result({"status": "error", "kind": "register_failed",
                            "message": str(exc)})

    # Report the EFFECTIVE time, not the one passed in. Nothing is rounded —
    # ``validate_recurring`` REFUSES a sub-minute anchor rather than adjusting
    # it — but a recurring anchor is re-expressed in the SCHEDULER's timezone,
    # which renders a different wall-clock time whenever that zone and the
    # caller's offset disagree. Echoing the caller's value would then tell the
    # user a time the reminder does not fire at.
    effective = entry.get("at") or when.isoformat()
    logger.info("reminder set: role=%s name=%s at=%s repeat=%s",
                role, name, effective, repeat)
    return _result({"status": "ok", "name": name,
                    "at": effective, "repeat": repeat,
                    # #1038 §6.5: the line the firing turn will prefix.
                    "note": _note})


@tool(
    "cancel_reminder",
    "Cancel a reminder you previously set, by its name as shown by "
    "get_schedule. Own-role only, and reminders only — other scheduled "
    "triggers are operator configuration and cannot be removed here.",
    {"name": str},
)
async def cancel_reminder(args: dict) -> dict:
    import reminders

    role = _reminder_origin_role()
    if not role:
        return _result({"status": "error", "kind": "no_turn_context",
                        "message": "cancel_reminder called outside a turn"})

    name = (args.get("name") or "").strip()
    if not name:
        return _result({"status": "error", "kind": "invalid_argument",
                        "message": "name is required"})

    runtime = _runtime
    if runtime is None:
        return _result({"status": "error", "kind": "not_initialized",
                        "message": "runtime not wired"})

    path = reminders.triggers_path(runtime.agents_dir, role)
    # Authorization is the store's ``managed_by`` check, not a name pattern
    # here: the schema permits an operator to author a `reminder-`-prefixed
    # entry, so the prefix cannot decide what may be deleted (INV-TRIG-010).
    try:
        outcome = await asyncio.to_thread(reminders.remove_entry, path, name)
    except (OSError, ValueError) as exc:
        return _result({"status": "error", "kind": "write_failed",
                        "message": str(exc)})
    if outcome == "not_owned":
        return _result({
            "status": "error", "kind": "not_authorized",
            "message": (f"{name!r} is operator configuration, not a reminder "
                        f"you set. Only your own reminders can be cancelled "
                        f"here."),
        })
    if outcome != "removed":
        return _result({"status": "error", "kind": "not_found",
                        "message": f"no reminder named {name!r}"})

    # #513: a cancel is one of the two live paths that rewrites a
    # ${...}-bearing triggers.yaml. Never fatal to the cancel itself.
    try:
        import casa_core
        await casa_core.notify_placeholder_rewrites(_channel_manager)
    except Exception:  # noqa: BLE001
        logger.warning("placeholder-rewrite notice failed", exc_info=True)

    # Drop the live job too, so the cancellation takes effect now. Best
    # effort: the entry is already gone, so a boot would not resurrect it.
    try:
        runtime.trigger_registry.remove_job_for(role, name)
    except Exception:  # noqa: BLE001
        logger.warning("cancel_reminder: job removal failed for %s:%s",
                       role, name, exc_info=True)

    # #573: a cancelled reminder must not leave a question it raised hanging in
    # the operator's DM. Revoking settles it — keyboard retired, scheduled
    # session told — and bumps the epoch so a firing turn still in flight
    # cannot post a new one for the reminder just cancelled.
    try:
        import scheduled_asks
        scheduled_asks.revoke_trigger(role, name, "trigger_cancelled")
    except Exception:  # noqa: BLE001
        logger.warning("cancel_reminder: scheduled-ask revocation failed for "
                       "%s:%s", role, name, exc_info=True)

    logger.info("reminder cancelled: role=%s name=%s", role, name)
    return _result({"status": "ok", "name": name})


# ---------------------------------------------------------------------------
# ack_event — plugin-events facility (#419)
#
# The receipt half of a headless event wake: the wake instruction tells the
# agent the exact ``emitter``/``event``/``token`` to echo back here. INV-EV-005
# (capability hygiene): the token is a bare bearer credential for exactly one
# pending delivery — it must NEVER appear in this tool's reply, in a log
# line, or in any operator-facing surface, so a leaked transcript or log
# line can never be replayed to forge a receipt. Only the emitter/event
# names (never plugin-authored prose) and, on success, the acked
# subscriber's own registry name are ever echoed back.
# ---------------------------------------------------------------------------


@tool(
    "ack_event",
    "Acknowledge a plugin event delivery. Call this when you are done "
    "processing a headless event wake, passing emitter, event, and token "
    "EXACTLY as given in the wake instruction. Stops further redelivery "
    "nudges for this one delivery.",
    {"type": "object",
     "properties": {"emitter": {"type": "string"}, "event": {"type": "string"},
                    "token": {"type": "string"}},
     "required": ["emitter", "event", "token"]},
)
async def ack_event(args: dict) -> dict:
    """Maps :meth:`event_spool.EventSpool.ack`'s typed result. No mutation
    on ``no_match`` (a stale/rotated token, an unknown pair, or a
    quarantined record) — and the refusal names ONLY the emitter/event, per
    INV-EV-005; the token value itself is never echoed, logged, or raised
    into an exception message anywhere in this function."""
    emitter = (args.get("emitter") or "").strip()
    event = (args.get("event") or "").strip()
    token = args.get("token")
    if not emitter or not event:
        return _result({"status": "error", "kind": "invalid_argument",
                        "message": "emitter and event are required"})
    if not isinstance(token, str) or not token:
        return _result({"status": "error", "kind": "invalid_argument",
                        "message": "token is required"})

    import event_spool
    spool = event_spool.get_spool()
    if spool is None:
        return _result({"status": "error", "kind": "not_initialized",
                        "message": "event spool not available"})

    outcome, subscriber = await asyncio.to_thread(
        spool.ack, emitter, event, token)
    if outcome == "acked":
        return _result({"status": "ok", "outcome": "acked",
                        "subscriber": subscriber,
                        "message": f"acknowledged for {subscriber!r}"})
    if outcome == "already_done":
        return _result({"status": "ok", "outcome": "already_done",
                        "message": "already acknowledged"})
    return _result({
        "status": "error", "kind": "no_match", "outcome": "no_match",
        "message": (f"no pending delivery of event {event!r} from "
                    f"emitter {emitter!r} matches that acknowledgement"),
    })


# ---------------------------------------------------------------------------
# config_git_commit - Plan 3 (Tier 3 executor support)
# ---------------------------------------------------------------------------


# Bug 7 (v0.14.6): role guard for the privileged config tools.
# Pre-fix: gated only by each agent's runtime.yaml::tools.allowed,
# meaning a copy-paste error or permissive default in a new resident /
# specialist / executor silently exposed addon-restart and config-commit
# powers. Defense in depth at the tool itself.
_PRIVILEGED_CONFIG_ROLES = frozenset({"configurator"})


def _effective_caller_role() -> str | None:
    """Return the calling agent's role for authorisation checks.

    The EXECUTING role first: Casa stamps ``execution_role`` on the origin of
    every delegated run and pinned turn from the loaded config it runs
    (``_run_delegated_turn``, the stored-call settler's origin), while
    ``origin["role"]`` there is the caller (the resident). Then an active
    engagement's role (a background job, whose turn inherits the engager's
    origin), then the origin's own role.

    Returns None if nothing is bound — caller must refuse rather than fall
    back to a permissive default.
    """
    try:
        origin = _snapshot_origin()
    except Exception:  # noqa: BLE001 - defensive against import-time issues
        origin = {}
    r = (origin or {}).get("execution_role")
    if r:
        return r
    eng = engagement_var.get(None)
    if eng is not None:
        r = getattr(eng, "role_or_type", None)
        if r:
            return r
    r = (origin or {}).get("role")
    return r or None


def _refuse_unprivileged(tool_name: str, caller: str | None) -> dict:
    return _result({
        "status": "error",
        "kind": "not_authorized",
        "message": (
            f"{tool_name} is restricted to roles "
            f"{sorted(_PRIVILEGED_CONFIG_ROLES)}; calling role={caller!r}. "
            "Ask the configurator to perform this action instead."
        ),
    })


from config_git import TRACKED_PATHS_SUMMARY as _TRACKED_PATHS_SUMMARY


@tool(
    "config_git_commit",
    "Stage and commit all tracked changes under /config/ (tracked: "
    f"{_TRACKED_PATHS_SUMMARY}; everything else incl. plugins/store/, "
    "plugins/.staging/, and plugin-env.conf is gitignored), then reload what "
    "the commit changed, so the change is live when this returns. "
    "Returns the commit SHA and the reload results under `reloads` "
    "(empty when nothing needed one) — empty SHA plus a warning when nothing "
    "tracked changed, which is the expected outcome for gitignored-only "
    "writes. Restricted to the configurator executor role.",
    {"message": str},
)
async def config_git_commit(args: dict) -> dict:
    caller = _effective_caller_role()
    if caller not in _PRIVILEGED_CONFIG_ROLES:
        return _refuse_unprivileged("config_git_commit", caller)

    message = args.get("message") or "configurator: commit"
    config_dir = "/config"
    try:
        import config_git
        import agent_loader

        # E-G (v0.31.0): pre-commit schema-validation gate — refuse the
        # commit if any schema-bearing YAML would fail boot-time
        # agent_loader validation (the v0.30.0 ``TRAIT:`` top-level-key
        # repro FATALed the addon on next boot). #351 closed the gate's
        # validate→commit TOCTOU: commit_config_checked stages first, then
        # validates the exported index snapshot, then commits that exact
        # snapshot — an SSH edit landing mid-window can no longer be
        # committed unvalidated (it stays uncommitted for the next round).
        sha, errors = await asyncio.to_thread(
            config_git.commit_config_checked, config_dir, message,
            agent_loader.validate_config_repo,
        )
        if errors:
            return _result({
                "status": "error",
                "kind": "schema_invalid",
                "message": (
                    f"Refusing commit: {len(errors)} schema validation "
                    f"failure(s). Fix the offending YAML and retry."
                ),
                "errors": errors,
            })
        if not sha:
            # P-3 (v0.69.1): a bare {"sha": ""} left agents looping to
            # reconcile "committed ok" against "file still untracked" when
            # their writes landed on gitignored paths. Say it loudly instead.
            return _result({
                "sha": "", "message": message,
                "warning": (
                    "No tracked changes to commit. The config repo tracks ONLY "
                    f"{_TRACKED_PATHS_SUMMARY}; every "
                    "other path is gitignored by design — plugins/store/, "
                    "plugins/.staging/, and plugin-env.conf (a secrets file) "
                    "must never enter git history. If you only wrote "
                    "gitignored paths, an empty SHA is the expected, correct "
                    "outcome: report it as such and do NOT retry the commit."
                ),
            })
        changed = await asyncio.to_thread(config_git.changed_paths, config_dir, sha)
    except Exception as exc:  # noqa: BLE001
        return _result({
            "status": "error",
            "kind": "git_error",
            "message": str(exc),
        })

    # INV-TOOL-014: the commit reloads what it changed — whoever the caller is.
    reloads = await _reload_committed(changed)
    out = {"sha": sha, "message": message, "reloads": reloads}
    if any(r.get("status") != "ok" or r.get("failures") for r in reloads):
        out["warning"] = (
            "The commit landed, but its reload did not fully succeed, so part "
            "of the change is not live yet. Read each reload's kind, message "
            "and failures and tell the operator plainly; then fix and commit "
            "again, or retry with casa_reload and the scope shown. A "
            "restart_required kind means only a restart makes it live: that "
            "is the operator's call, so ask before casa_restart_supervised.")
    return _result(out)


async def _reload_committed(changed: "list[str]") -> "list[dict]":
    """Run the reloads ``reload.commit_reload_scopes`` names for a commit's
    changed paths, one after another, and return their envelopes. A failure is
    an envelope, never an exception: the commit already landed."""
    import agent as agent_mod
    import reload as reload_mod
    runtime = getattr(agent_mod, "active_runtime", None)
    if runtime is None:
        return [{"status": "error", "kind": "not_initialized",
                 "message": "CasaRuntime not bound - boot ordering bug"}]
    try:
        plan = reload_mod.commit_reload_scopes(changed, runtime=runtime)
    except Exception as exc:  # noqa: BLE001 — fall back to the catch-all
        logger.warning("commit reload plan failed (%s); reloading full", exc)
        plan = [("full", None)]
    return [await _run_reload(scope, runtime=runtime, role=role)
            for scope, role in plan]


# ---------------------------------------------------------------------------
# casa_reload - Plan 3 (hard reload via Supervisor addon restart)
# ---------------------------------------------------------------------------


def _reload_role_arg(raw) -> "str | None":
    """The role a reload tool was asked for, as the agent DIRECTORY name.

    #1004: every plugin surface names a role in its tier-prefixed form
    (``plugin_add``'s ``targets``, its ``reloaded:`` list, the engagement
    brief that quotes them), while the reload scopes key on the directory
    name under ``agents/``. The tool is the trust boundary for the argument,
    so it canonicalises here: a leading ``resident:`` or ``specialist:`` is
    stripped; anything else (including an unknown prefix) is the role as
    given, and the reload answers for that. ``None`` when nothing is left."""
    role = (raw or "").strip() if isinstance(raw, str) else ""
    for prefix in ("resident:", "specialist:"):
        if role.startswith(prefix):
            role = role[len(prefix):].strip()
            break
    return role or None


@tool(
    "casa_reload",
    "In-process reload of Casa runtime state at a given scope. "
    "Valid scopes: 'agent' (requires role), 'triggers' (requires role), "
    "'policies', 'plugin_env', 'agents', 'config_sync', 'full'. Use 'full' "
    "as a catch-all when unsure. Does NOT restart the addon - for that, "
    "see casa_restart_supervised. Restricted to the configurator role.",
    # Explicit schema: the shorthand marked role and include_env required, so
    # the recipes' own `casa_reload(scope="plugin_env")` was rejected by the
    # MCP validator before this handler ran (batch-1 diff round 1, Astra D5).
    {"type": "object",
     "properties": {"scope": {"type": "string"},
                    "role": {"type": "string"},
                    "include_env": {"type": "boolean"}},
     "required": ["scope"]},
)
async def casa_reload(args: dict) -> dict:
    caller = _effective_caller_role()
    if caller not in _PRIVILEGED_CONFIG_ROLES:
        return _refuse_unprivileged("casa_reload", caller)

    scope = (args.get("scope") or "").strip()
    if not scope:
        return _result({
            "status": "error", "kind": "scope_required",
            "message": (
                "casa_reload requires a 'scope' argument. Valid: "
                "'agent', 'triggers', 'policies', 'plugin_env', "
                "'agents', 'config_sync', 'full'. See doctrine/reload.md."
            ),
        })

    role = _reload_role_arg(args.get("role"))
    if scope in ("agent", "triggers") and role is None:
        return _result({
            "status": "error", "kind": "role_required",
            "message": f"casa_reload(scope={scope!r}) requires a non-empty "
                       "'role' — the agent's directory name (a leading "
                       "resident:/specialist: prefix is accepted and stripped)",
        })
    include_env = bool(args.get("include_env", False))

    import agent as agent_mod
    runtime = getattr(agent_mod, "active_runtime", None)
    if runtime is None:
        return _result({
            "status": "error", "kind": "not_initialized",
            "message": "CasaRuntime not bound - boot ordering bug",
        })

    return _result(await _run_reload(
        scope, runtime=runtime, role=role, include_env=include_env))


async def _run_reload(scope: str, *, runtime, role: "str | None" = None,
                      include_env: bool = False) -> dict:
    """One reload as the reload tools and ``config_git_commit`` run it: the
    dispatch inside the plugin-tools fence, then the plugin-health refresh.

    Task 10 manual-reload fencing (spec §3.1, ENTRY-POINT-ONLY): a reload that
    reaches plugin state must serialize against an in-flight bundle
    transaction. Acquire _PLUGIN_TOOLS_LOCK BEFORE dispatch (never inside
    reload.py beneath its writer/reader lock — that is the AB/BA deadlock,
    against the reader side a bundle op's dispatch("agent") already takes while
    holding _PLUGIN_TOOLS_LOCK). Global lock order everywhere:
    _PLUGIN_TOOLS_LOCK -> reload writer/reader lock (INV-CFG-011).
    #489: the GUARD, not the raw lock — reload_full's include_env arm reaches
    the plugin_env handler, whose health block re-enters via the same guard.
    #706: WHICH scopes, not just "full" — _plugin_tools_reload_guard owns that
    classification.

    #824: the health refresh runs AFTER the fence, not inside it. Dispatch
    released the reload RW lock when it returned, so either position keeps the
    plugin lock off the reload lock; outside is what lets the regeneration
    settle through cancellation as ONE unit in a task of its own, which inside
    a fenced scope's hold would deadlock — the guard's re-entrancy is by TASK
    IDENTITY, and a child task is not the entry point's. A mutation that
    interleaves here regenerates under the same guard, so whichever runs second
    computes from the fresher state. Never raises: dispatch returns an error
    envelope, and the refresh logs its own failure."""
    from reload import dispatch
    extra = {"include_env": True} if include_env else {}
    async with _plugin_tools_reload_guard(scope):
        result = await dispatch(scope, runtime=runtime, role=role, **extra)
    await _regenerate_plugin_health_after_reload(scope, result)
    return result


async def _post_supervisor_restart() -> dict:
    """POST the Supervisor restart. Returns a result-shaped dict for logging;
    never raises. The request itself lives in :mod:`supervisor_restart`, which
    the memory wipe and the configurator's Restart now button use too."""
    import supervisor_restart
    return await supervisor_restart.request_app_restart()


@tool(
    "casa_restart_supervised",
    "Full Supervisor-driven addon restart. Use ONLY when changes require "
    "process-restart semantics (s6 service tree changes, addon "
    "options.json mutations, a resident persona change). For routine config "
    "edits a commit already reloads what it changed. Inside your run this "
    "restarts nothing itself: Casa puts a Restart now button under your "
    "answer, and the operator restarts when it suits him. Restricted to the "
    "configurator role.",
    {},
)
async def casa_restart_supervised(_: dict) -> dict:
    caller = _effective_caller_role()
    if caller not in _PRIVILEGED_CONFIG_ROLES:
        return _refuse_unprivileged("casa_restart_supervised", caller)

    origin = _snapshot_origin() or {}
    run_key = origin.get("_delegation_id")
    if run_key:
        # Never inline from a run — the
        # operator restarts by tapping the button that follows the answer.
        import configurator_run
        configurator_run.note_restart(str(run_key), str(origin.get("user_text") or ""))
        return _result({
            "deferred": True,
            "message": (
                "Nothing restarted yet. Casa puts a Restart now button under "
                "your answer; end your turn with that answer, saying what the "
                "restart is for and what it costs."),
        })

    # Outside a delegated run (operator-driven /invoke etc): POST inline.
    return _result(await _post_supervisor_restart())


# Strong references to the cancelled-launch owners
# (``_abort_launch_on_cancel``): the cancelled launcher re-raises at once, so
# without an anchor the task would be collectable mid-flight. The stop drains
# it with the launch-death reporters.
_CANCELLED_LAUNCH_TASKS: set = set()


def _launch_cause_of(engagement_id: str) -> str:
    """#698: the cause the stop recorded against this launch, or ``""``."""
    if _engagement_registry is None:
        return ""
    try:
        cause = _engagement_registry.launch_cause(engagement_id)
    except Exception:  # noqa: BLE001 — a missing seam is simply no cause
        return ""
    # A non-string is not a cause (a test double's auto-attribute, a corrupt
    # value): resolve toward "no stop", which is the arm that keeps today's
    # behaviour rather than the arm that claims retention.
    return cause if isinstance(cause, str) else ""


def _register_launch_safe(engagement_id: str) -> Any:
    """#698: enrol an in-flight launch with the engagement registry.

    Never raises and never blocks a launch: an enrolment failure costs this
    launch its shutdown cause, which is strictly the behaviour every launch has
    today, whereas raising here would abort a launch that was about to succeed.
    """
    if _engagement_registry is None:
        return None
    try:
        return _engagement_registry.register_launch(
            engagement_id, asyncio.current_task())
    except Exception:  # noqa: BLE001 — enrolment is never worth a launch
        logger.warning("register_launch(%s) failed", engagement_id[:8],
                       exc_info=True)
        return None


def _unregister_launch_safe(handle: Any) -> None:
    """#698: drop the handle by identity. The CAUSE is deliberately kept."""
    if handle is None or _engagement_registry is None:
        return
    try:
        _engagement_registry.unregister_launch(handle)
    except Exception:  # noqa: BLE001 — an unwind must complete
        logger.warning("unregister_launch failed", exc_info=True)


async def stop_engagement_launches(registry: Any) -> None:
    """#698: the graceful stop's engagement-launch step, as ONE production
    helper so that `casa_core`'s stop block holds a call rather than a copy.

    Cause first, then the cancellation it explains, then wait: the launches
    unwind while Telegram, the bus, the drivers and the registry are all still
    up, which is what gives each dying launch's notice a live transport. Every
    bound here is a no-output condition on a counterparty, never an elapsed
    budget (D21).

    Never raises: a stop that cannot complete this step still completes.
    """
    try:
        issued = registry.begin_launch_shutdown()
        if issued:
            logger.info("graceful stop: cancelled %d in-flight engagement "
                        "launch(es) with a recorded cause", issued)
        await registry.drain_launches()
    except Exception:  # noqa: BLE001 — shutdown must complete
        logger.warning("graceful stop: draining engagement launches failed",
                       exc_info=True)
    # Every anchored reporter, ack, abort and cancelled-launch owner, drained
    # together to joint quiescence (one anchor's completion can mint into
    # another).
    await _drain_anchored(_LAUNCH_DEATH_TASKS, _CANCELLED_LAUNCH_TASKS)
    try:
        registry.mark_launch_drains_complete()
    except Exception:  # noqa: BLE001 — a fake registry without the mark
        pass


async def drain_launch_death_reports() -> None:
    """#698: wait for every anchored launch-TERMINAL owner to finish.

    Without this the reporters are cancelled by ``asyncio.run``'s final sweep
    mid-notice — measured: the reporter dies inside its notice ``wait_for`` and
    the operator is told nothing at all.

    #757: the set now anchors TWO owners — the launch-death reporters and the
    named-launch-failure aborts (``_spawn_launch_abort``) — so the termination
    argument is restated for both rather than counted per launch. Every
    anchored owner is internally bounded (a reporter by the driver-cancel and
    quiesce timeouts), NONE of them spawns another anchored owner, and a
    completed task never re-enters a later snapshot — so the loop terminates by
    construction rather than by a clock, however many owners one launch's
    unwind produced.
    """
    await _drain_anchored(_LAUNCH_DEATH_TASKS)


async def _drain_anchored(*anchors: set) -> None:
    """§2.A, diff r3 (Astra J2): wait for every task in the given anchors,
    INCLUDING what the done callbacks of just-completed ones are about to mint,
    and (diff r5, Astra J2) to JOINT quiescence across all of them: a task in
    one anchor may mint into another (a cancelled-launch owner's completion
    can mint a telling into the reporter set), so three drains in sequence let work into an already-drained set —
    measured: the stop marked with 1 reporter pending. One pass that finds
    nothing pending in ANY anchor, after a yield, is the only exit.

    A task that has completed but whose done callbacks are still queued reads
    as done to a snapshot, yet one of those callbacks may create the next
    anchored task (an abandoned inline abort's telling; a never-started
    owner's compensator). A drain that returned on that snapshot let the
    stop mark its drains complete with a telling still to be minted —
    measured: 0 bus messages, 1 pending telling at the stop's return. So an
    empty snapshot is confirmed only after one yield to the loop, which runs
    every queued callback; a second empty snapshot is final. Termination
    holds as before: every anchored task is internally bounded and mints at
    most one successor, so the chain is finite.
    """
    def _pending() -> list:
        return [t for a in anchors for t in list(a) if not t.done()]

    while True:
        pending = _pending()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            continue
        await asyncio.sleep(0)
        if not _pending():
            return


async def drain_delegation_settlements() -> None:
    """#767: wait for every delegation SETTLE tail whose verdict has landed.

    The job-ledger twin of `drain_launch_death_reports`, with one more thing
    to look at. A settle tail is minted by a delegation run's done callback
    (`_attach_completion_callback._done`), which asyncio schedules on the loop
    when the run completes — so at any instant a verdict can be in one of two
    places: a PENDING TAIL in `_delegation_settle_tasks`, or a RIPE PRODUCER in
    `_delegation_callback_tasks` (the run is `done()`, its callback is queued,
    its tail does not exist yet). A snapshot of the tail set alone returns
    empty in the second case and the tail is then killed by `asyncio.run`'s
    final sweep before its first step — measured, and exactly the defect.

    Bounded by a no-output condition, never a clock (D21): each pending tail
    is one shielded registry write plus at most one unbounded queue put, no
    tail mints a tail, a completed tail never re-enters, and a ripe producer's
    queued callback — scheduled before this coroutine's own `sleep(0)`
    reschedule, and run FIFO — has run and retired it by the next snapshot.
    A run that is NOT done is deliberately not waited for: the stop never
    waits on delegated work, and a run still running when the ledger closes is
    INV-JOB-009's boot-reconciliation case. A terminal write that FAILED and
    left a registry-owned retry pending is likewise outside this drain: that
    retry is unbounded by design and `JobRegistry.close` cancels it, as it
    does every retry (INV-JOB-011 names both limits).

    Never raises: a stop that cannot complete this step still completes, and
    an exception is a reason to STOP, not to loop on its recurrence.
    """
    while True:
        try:
            loop = asyncio.get_running_loop()
            tails = [t for t in list(_delegation_settle_tasks)
                     if not t.done() and t.get_loop() is loop]
            ripe = [t for t in list(_delegation_callback_tasks)
                    if t.done() and t.get_loop() is loop]
            if not tails and not ripe:
                return
            if tails:
                await asyncio.gather(*tails, return_exceptions=True)
            else:
                # One loop turn: the queued done callbacks run, each ripe
                # producer mints its tail (or its cancel op) and leaves.
                await asyncio.sleep(0)
        except Exception:  # noqa: BLE001 — shutdown must complete
            logger.warning("graceful stop: draining delegation settlements "
                           "failed", exc_info=True)
            return


def _abort_launch_on_cancel(channel: Any, rec: "EngagementRecord") -> None:
    """Sol r1 (#363 family): background compensation for a launch cancelled
    AFTER its record was created but BEFORE its driver was confirmed live.
    Without this, the cancellation unwound the tool call leaving a
    durably-active record with nothing driving it.

    Terra/Sol r2: the driver may in fact have gone LIVE before the
    cancellation landed, so the compensation runs the driver's idempotent
    terminal teardown (``cancel()``) as well as marking the record errored.
    Fire-and-forget and strong-ref'd, since a cancelled task cannot await.

    #678: the body is the SHARED launch-death reporter. It awaited
    ``driver.cancel`` UNBOUNDED, ahead of everything else, so a wedged
    teardown meant the record was never even marked; and it used
    ``mark_error``, which persists best-effort and so returns True over a
    swallowed tombstone-write failure. The reporter marks first
    (strictly), then tells, then tears down under a bound — the finalize
    funnel's own order, and safer here too: engagement tool authority is bound
    to ``active``, so flipping first revokes the dying engagement's tools
    sooner."""
    import agent as agent_mod
    driver = getattr(agent_mod, "active_engagement_driver", None)
    # #698: the cause is READ, never inferred from the cancellation's shape.
    # A stop wrote it against this exact row before it cancelled it; anything
    # else — a creator cancel, a barge-in, a role eviction — carries none and
    # keeps today's byte-identical sentence. The KIND is unchanged on both
    # arms: `launch_cancelled` is the outcome, and the stop is a separate
    # signal the registry stamps beside it (D32 — both facts travel, neither
    # overwrites the other).
    detail = ("Casa was stopping when this launch was cancelled"
              if _launch_cause_of(rec.id)
              else "the tool call was cancelled during launch")
    if _engagement_registry is not None and _engagement_registry.launch_drains_complete() is True:
        # Diff r1 (Astra J2): a launcher cancelled after the stop's last drain
        # (it was not yet enrolled when the ledger was walked) must not mint a task the interpreter's final sweep destroys pending —
        # the same rule as the owner's done callback. Loud; boot re-idles.
        logger.error(
            "launch %s was cancelled after the stop's drains completed — no "
            "compensation minted; record left live for boot", rec.id[:8])
        return
    task = asyncio.ensure_future(_cancelled_launch_owner(
        channel, rec, detail=detail, driver=driver))
    _LAUNCH_DEATH_TASKS.add(task)
    task.add_done_callback(
        lambda t, _eid=rec.id: _launch_death_done(_eid, t))
    _CANCELLED_LAUNCH_TASKS.add(task)
    task.add_done_callback(_CANCELLED_LAUNCH_TASKS.discard)


async def _cancelled_launch_owner(
    channel: Any, rec: "EngagementRecord",
    *, detail: str, driver: Any | None,
) -> None:
    """§2.A: the cancellation owner reports the death with the obligation
    ARMED and then tells the engager what the report's outcome allows."""
    outcome = await _report_launch_death(
        channel, rec, kind="launch_cancelled", detail=detail,
        driver=driver, owes_terminal_notification=True)
    # REPORTED: tell live with the acknowledgement. PERSIST_FAILED (Astra
    # J1.1): the record is live — the bounded uncommitted
    # notice and the unarmed `launch_outcome_uncommitted` telling. A LOST race
    # tells nobody: the winner owns its telling, and an armed bit alone is NOT
    # permission to send — a surviving funnel leaves it armed while its own
    # delivery is pending (Astra J1.2). An inline arm whose launcher was
    # cancelled transfers its telling explicitly (`_await_inline_terminal`).
    await _tell_engager_launch_outcome(
        rec, outcome, kind="launch_cancelled", detail=detail, channel=channel)


class LaunchDeathResult(enum.Enum):
    """#678: the outcome of reporting a launch that left no terminal artifact.

    Three-way ON PURPOSE (Sol, seam round 4): an untyped ``None`` conflates a
    LOST terminal race — where the engagement really did report itself and the
    engager must still be told ``pending`` — with a ROLLED-BACK persist, where
    the record is still live and the engager must be told the launch failed.
    Mapping one return value to either envelope lies in the other direction.
    """

    REPORTED = "reported"                 # durable error + operator told
    ALREADY_TERMINAL = "already_terminal"  # lost the race; zero side effects
    PERSIST_FAILED = "persist_failed"      # rolled back; record still LIVE


_LAUNCH_DEATH_TASKS: set = set()
"""#678: anchored launch-death reporters, held so the loop cannot collect one
mid-flight. Casa-owned ``asyncio.create_task`` — never ``Query.spawn_task`` —
so an SDK client teardown cannot find or cancel them, exactly as #632's
detached finalize tail is anchored in ``_finalize_tail_tasks``."""


def _launch_death_done(engagement_id: str, task: Any) -> None:
    """#678: the done-callback is the ONLY observer a reporter has once its
    launcher has unwound cancelled, so it must retrieve the outcome — an
    unobserved reporter failure would be a second silent-death mode, which is
    the very defect this exists to fix (same reasoning as
    ``_finalize_tail_done``)."""
    _LAUNCH_DEATH_TASKS.discard(task)
    if task.cancelled():
        logger.error(
            "launch-death report for %s was CANCELLED — the operator may "
            "not have been told", engagement_id[:8])
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "launch-death report for %s raised: %r", engagement_id[:8], exc)


async def _report_launch_death(
    channel: Any, rec: "EngagementRecord",
    *, kind: str, detail: str, driver: Any | None,
    owes_terminal_notification: bool = False,
) -> "LaunchDeathResult":
    """#678: the ONE owner of a launch that ended without a terminal artifact.

    Asks the registry exactly ONE transactional question — flip this record to
    ``error``, strictly, unless it is already terminal — and lets its three
    outcomes decide everything. Earlier designs asked two separate questions,
    "is it terminal?" then "did I win?", and both reads were unsound: a
    lock-free status read can observe the uncommitted window of a strict
    transition that a persist failure then rolls back, and ``mark_error``
    persists BEST-EFFORT, so it returns True over a swallowed tombstone-write
    failure and would report a death over a record that disk still calls
    ``active``. ``casa_core.py``'s boot-replay refusal already
    reasoned to the same conclusion in a committed comment and uses this same
    primitive with ``strict=True``.

    Deliberately NOT ``_finalize_engagement``: that funnel would double-notify
    the engager over the bus and overwrite ``error_kind`` with
    ``emit_completion_error``. Nothing here constructs ``completed``.

    Never raises. Returns a :class:`LaunchDeathResult`.
    """
    if _engagement_registry is None:
        return LaunchDeathResult.PERSIST_FAILED

    try:
        won = await _engagement_registry.try_transition_terminal(
            rec.id, "error", strict=True, error_kind=kind,
            error_message=detail,
            # §2.A: the detached launch owner and the cancellation owner tell
            # the engager themselves, so they arm the durable obligation the
            # live telling then acknowledges; an inline launcher that returns
            # an envelope passes False.
            owes_terminal_notification=owes_terminal_notification,
        )
    except Exception as exc:  # noqa: BLE001 — strict persist failed + rolled back
        # The record is STILL LIVE and disk agrees. Report nothing. But RETIRE
        # the driver client — with no exception out of the driver, start()'s
        # rollback never ran, so the client is registered over a transport
        # that may already be dead and ``is_alive`` would hand the next
        # operator message to an ended client instead of resuming.
        logger.error(
            "launch-death report %s: strict terminal transition did not "
            "persist (rolled back, record left live): %s", rec.id[:8], exc)
        await _cancel_driver_bounded(driver, rec)
        return LaunchDeathResult.PERSIST_FAILED
    if not won:
        logger.info(
            "launch-death report %s: record already terminal — the "
            "engagement reported itself; nothing to tell", rec.id[:8])
        return LaunchDeathResult.ALREADY_TERMINAL

    # One line in the job's log. Inbound messages that die with the launch
    # are not listed here: the journal's terminal branch below tells each one
    # once.
    _job_log(rec, "⚠️ stopped before reporting a result")

    await _cancel_driver_bounded(driver, rec)
    await _settle_job_journal(rec, driver)
    logger.warning(
        "Engagement %s launch reported dead: kind=%s detail=%s",
        rec.id[:8], kind, detail)
    return LaunchDeathResult.REPORTED


async def _settle_job_journal(engagement: Any, driver: Any) -> None:
    """The job's message journal, terminal branch, run by every terminal
    path after the job's client is closed: each operator message the session
    transcript lacks is told as lost once, in reply to it;
    one it holds goes silently, and the journal goes with the record. Each
    notice is bounded by the channel. Then the job's scratchpad goes, in
    memory and on disk, whatever the journal's settle did. Never raises."""
    try:
        settle = getattr(driver, "settle_journal_terminal", None)
        if not callable(settle):
            return
        transport = _transport()

        async def _tell(_rec: Any, _entry: dict) -> None:
            # Told in reply to the operator's message, through the bot that
            # received it: the notice's stored address names it.
            if transport is None:
                raise RuntimeError("no channel to tell a lost message")
            notice = _entry.get("notice")
            if not isinstance(notice, dict):
                logger.warning("lost message of %s has no message to reply "
                               "to; not told", str(getattr(_rec, "id", "?"))[:8])
                return
            address = TgAddress.from_json(notice.get("address"))
            await transport.endpoint(address).tell_journal_lost(_rec, _entry)

        try:
            await settle(engagement, _tell)
        except Exception as exc:  # noqa: BLE001 — never stops a terminal path
            logger.warning("engagement %s: settling the message journal failed: %s",
                           getattr(engagement, "id", "?")[:8], exc)
    finally:
        eid = getattr(engagement, "id", None)
        if isinstance(eid, str):
            scratchpad.PADS.discard(eid, path=background_jobs.job_pad_path(eid))


async def _cancel_driver_bounded(driver: Any, rec: Any) -> None:
    """#678: tear the driver client down under the funnel's own bound.

    ``_abort_launch_on_cancel`` awaited ``driver.cancel`` UNBOUNDED ahead of
    every operator-visible effect, so a wedged teardown meant the operator
    was never told at all. Same treatment ``_finalize_engagement_tail`` gives it."""
    if driver is None or not hasattr(driver, "cancel"):
        return
    try:
        await asyncio.wait_for(driver.cancel(rec), _DRIVER_CANCEL_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.error(
            "launch-death report %s: driver.cancel exceeded %.0fs — "
            "continuing so the operator is still told", rec.id[:8],
            _DRIVER_CANCEL_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "launch-death report %s: driver.cancel failed: %s",
            rec.id[:8], exc)


LAUNCH_UNCOMMITTED_KIND = "launch_outcome_uncommitted"
"""§2.A: the engager's notice when the launch turn ended and the strict
terminal write did not persist. NOT a terminal outcome: the record is live
and resumable; the consumer narrates it as such."""


def _register_launch_task_safe(engagement_id: str, task: Any) -> Any:
    """§2.A: enrol a specific task (the launch owner) with the ledger; the
    sibling of ``_register_launch_safe``, which enrols the current task."""
    if _engagement_registry is None:
        return None
    try:
        return _engagement_registry.register_launch(engagement_id, task)
    except Exception:  # noqa: BLE001 — enrolment is never worth a launch
        logger.warning("register_launch(%s) failed", engagement_id[:8],
                       exc_info=True)
        return None


def _schedule_terminal_ack(engagement_id: str) -> None:
    """§2.A (Astra R4/T4): an inline named abort armed the obligation; the
    launcher is about to return its envelope, which IS the telling. The
    acknowledgement awaits a persist, so it is SCHEDULED — anchored and
    drained by the stop — and the envelope returns without awaiting it. A
    launcher cancelled before this call leaves the obligation armed; one
    cancelled after it has already returned."""
    if _engagement_registry is None:
        return

    async def _ack() -> None:
        try:
            await _engagement_registry.ack_terminal_notification(engagement_id)
        except Exception:  # noqa: BLE001 — a failed ack costs one boot replay
            logger.warning("terminal-ack for %s failed", engagement_id[:8],
                           exc_info=True)

    task = asyncio.ensure_future(_ack())
    _LAUNCH_DEATH_TASKS.add(task)
    task.add_done_callback(_LAUNCH_DEATH_TASKS.discard)


def _spawn_abandoned_inline_telling(
    task: Any, rec: "EngagementRecord", channel: Any, *, kind: str, detail: str, won: Any,
) -> None:
    """§2.A, diff r1 (Astra J1.2/J3): the launcher that awaited this inline
    terminal was cancelled, so the envelope that IS its telling will never be
    returned. Ownership of the telling is transferred HERE, explicitly, by the
    launcher that abandoned it — never inferred later from the record's armed
    bit, which a surviving funnel also leaves armed while its own delivery is
    pending. If the arm WON its transition the fact of the outcome is sent
    live with the acknowledgement that clears it; a lost transition is the
    winner's to tell; a rolled-back one left the record live."""
    if task.cancelled() or task.exception() is not None:
        return
    if not won(task.result()):
        return
    tell = asyncio.ensure_future(_tell_engager_launch_outcome(
        rec, LaunchDeathResult.REPORTED, kind=kind, detail=detail, channel=channel))
    _LAUNCH_DEATH_TASKS.add(tell)
    tell.add_done_callback(_LAUNCH_DEATH_TASKS.discard)


async def _await_inline_terminal(
    task: Any, rec: "EngagementRecord", channel: Any, *, kind: str, detail: str, won: Any,
) -> Any:
    """§2.A: await an anchored inline terminal (a named abort or the latch
    block's death report) on behalf of a launcher that will return its
    envelope. Acknowledged on return ONLY for a transition this arm won
    (Astra J3: a losing arm must not clear another writer's obligation); a
    launcher cancelled inside the await hands the telling to the task's done
    callback and re-raises."""
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(
            lambda t, _rec=rec, _ch=channel, _k=kind, _d=detail, _w=won:
            _spawn_abandoned_inline_telling(t, _rec, _ch, kind=_k, detail=_d, won=_w))
        raise
    if won(result):
        _schedule_terminal_ack(rec.id)
    return result


def _refuse_inline_terminal_after_drains(rec: "EngagementRecord", kind: str) -> None:
    """Diff r2 (Astra J2): a launcher enrolled after the stop's ledger walk is
    cancelled at enrolment, but the cancellation is only DELIVERED at its next
    suspension — and a named-fault arm between the two would spawn its anchored
    abort first, after the drains that would have awaited it. Nothing is minted
    after the mark: the launch is treated as what it is, cancelled by the stop,
    through the arm that already owns that (itself fenced)."""
    if _engagement_registry is not None and _engagement_registry.launch_drains_complete() is True:
        logger.error(
            "launch %s reached a %s abort after the stop's drains completed — no "
            "abort minted; the launch is cancelled and the record left live for boot",
            rec.id[:8], kind)
        raise asyncio.CancelledError()


async def _abort_launch_inline(
    channel: Any, rec: "EngagementRecord",
    *, kind: str, message: str,
) -> "LaunchAbortResult":
    """The 17 named-fault arms: spawn the anchored abort, await it inline."""
    _refuse_inline_terminal_after_drains(rec, kind)
    return await _await_inline_terminal(
        _spawn_launch_abort(channel, rec, kind=kind, message=message),
        rec, channel, kind=kind, detail=message,
        won=lambda r: r is LaunchAbortResult.ABORTED)


async def _report_launch_death_inline(
    channel: Any, rec: "EngagementRecord",
    *, kind: str, detail: str, driver: Any | None,
) -> "LaunchDeathResult":
    """The latch blocks: the stop latched during ``open()``; report inline."""
    _refuse_inline_terminal_after_drains(rec, kind)
    return await _await_inline_terminal(
        _spawn_launch_death_report(
            channel, rec, kind=kind, detail=detail, driver=driver,
            owes_terminal_notification=True),
        rec, channel, kind=kind, detail=detail,
        won=lambda r: r is LaunchDeathResult.REPORTED)


async def send_engagement_outcome(
    bus: Any, *, complete: "DelegationComplete", origin: dict,
    ack: Any, context_extra: dict | None = None, source: str | None = None,
    fallback_role: str = "assistant",
) -> None:
    """§2.A: the ONE envelope for an engagement outcome on the bus. Shared by
    the finalize funnel, the boot replay and the launch owner; each caller
    builds its OWN ``DelegationComplete`` (they differ in text,
    result_available, elapsed_s, message and truncation — measured in design
    review), and this function carries only the routing: target role from the
    origin with the CALLER'S fallback (the boot replay passes the configured
    assistant role, as it always did — diff r3, Terra: a hard-coded
    ``"assistant"`` sent a role-less record's replay to a queue that may not
    exist, which the bus drops silently), channel, the cid/chat_id/engagement_id
    context merged with ``context_extra`` (the funnel's ``next_steps``), and
    the delivery acknowledgement. The context carries the origin's route, so
    the outcome is told through the bot the engagement came from."""
    _role = origin.get("role")
    target_role = _role if isinstance(_role, str) and _role else fallback_role
    _channel = origin.get("channel")
    context = {
        "cid": origin.get("cid", "-"),
        "chat_id": origin.get("chat_id", ""),
        "engagement_id": complete.delegation_id,
        # the outcome is told through the bot the engagement came from
        **carry_route(origin),
    }
    if context_extra:
        context.update(context_extra)
    await bus.notify(BusMessage(
        type=MessageType.NOTIFICATION,
        source=source or complete.agent,
        target=target_role,
        content=complete,
        channel=_channel if isinstance(_channel, str) else "",
        context=context,
        on_delivery=ack,
    ))


async def _tell_engager_launch_outcome(
    rec: "EngagementRecord", outcome: "LaunchDeathResult", *,
    kind: str, detail: str, channel: Any,
) -> None:
    """§2.A: what the detached launch owner tells the engager, live.

    ``REPORTED``: the record is terminal with the obligation armed; send the
    fact of the outcome (status error, the fault's kind and detail, no result)
    with the acknowledgement that clears the obligation — a lost live send is
    replayed at boot. ``PERSIST_FAILED``: the record is LIVE; write one line
    in the job's log saying so, and send an
    UNARMED notice of kind ``launch_outcome_uncommitted`` (there is no terminal
    write to carry an obligation). ``ALREADY_TERMINAL``: the winner owns the
    telling; nothing.
    """
    if outcome is LaunchDeathResult.ALREADY_TERMINAL or _bus is None:
        return
    origin = dict(getattr(rec, "origin", None) or {})
    if outcome is LaunchDeathResult.PERSIST_FAILED:
        _job_log(rec, "⚠️ could not be recorded")
        complete = DelegationComplete(
            delegation_id=rec.id, agent=rec.role_or_type, status="error",
            kind=LAUNCH_UNCOMMITTED_KIND,
            message=("the launch turn ended and its outcome could not be "
                     "recorded; the engagement is still open"),
            result_available=False, origin=origin, elapsed_s=0.0)
        ack = None
    else:
        complete = DelegationComplete(
            delegation_id=rec.id, agent=rec.role_or_type, status="error",
            kind=kind, message=detail, result_available=False,
            origin=origin, elapsed_s=0.0)
        _registry = _engagement_registry

        async def _ack(_eid=rec.id) -> None:
            await _registry.ack_terminal_notification(_eid)

        ack = _ack if _registry is not None else None
    try:
        await send_engagement_outcome(_bus, complete=complete, origin=origin, ack=ack)
    except Exception as exc:  # noqa: BLE001 — the obligation stays armed
        logger.warning("launch outcome notice for %s: bus.notify failed: %s",
                       rec.id[:8], exc)


def _spawn_launch_death_report(
    channel: Any, rec: "EngagementRecord",
    *, kind: str, detail: str, driver: Any | None,
    owes_terminal_notification: bool = False,
) -> Any:
    """#678: start the reporter as an ANCHORED Casa-owned task.

    The launcher awaits it through ``asyncio.shield`` so a cancellation
    unwinds the launcher while the reporter runs to completion; the
    cancellation compensator spawns it and does not await at all (a cancelled
    task cannot await). Both need this shape for the same reason: the registry
    deliberately KEEPS a durable terminal state and re-raises when a
    cancellation lands after its write committed, so an inline owner could
    commit the flip and then never settle the job — and a second,
    compensating reporter would LOSE the transition and correctly do nothing,
    leaving the durable error with no side-effect owner at all.
    """
    task = asyncio.ensure_future(_report_launch_death(
        channel, rec, kind=kind, detail=detail, driver=driver,
        owes_terminal_notification=owes_terminal_notification))
    _LAUNCH_DEATH_TASKS.add(task)
    task.add_done_callback(
        lambda t, _eid=rec.id: _launch_death_done(_eid, t))
    return task


class LaunchAbortResult(enum.Enum):
    """#757: the outcome of a NAMED launch failure's terminal transition.

    Three-way for the reason :class:`LaunchDeathResult` is (see its docstring):
    a bool reconflates a LOST terminal race — where another writer owns every
    terminal side effect — with a ROLLED-BACK persist, where the record is
    still LIVE.
    """

    ABORTED = "aborted"                    # durable win; job settled
    ALREADY_TERMINAL = "already_terminal"  # another writer owns the effects
    PERSIST_FAILED = "persist_failed"      # rolled back; record still LIVE


async def _abort_launch_with_fault(
    channel: Any, rec: "EngagementRecord",
    *, kind: str, message: str,
) -> "LaunchAbortResult":
    """#757: the ONE owner of a launch that failed with a NAMED fault.

    Every one of the named launch-failure arms routes here. It asks the
    registry exactly ONE transactional question — flip this record to
    ``error``, strictly — and lets the three outcomes decide whether the job's
    journal is settled. ``mark_error`` would not do: a LOST race returns False
    while the winner already owns the terminal side effects (INV-ENG-001), and
    a swallowed tombstone-write failure still returns True, because it
    persists best-effort.

    Deliberately NOT ``_report_launch_death``. These arms carry a *named fault*
    returned synchronously to a live caller (see
    ``docs/architecture/engagement-failure-and-restart.md``). Nothing here
    posts, and nothing here touches the bus.

    Never raises. The caller gets its own named fault back on all three
    outcomes — this launch really did fail for that reason, whoever won the
    record — so no arm's envelope changes.
    """
    if _engagement_registry is None:
        return LaunchAbortResult.PERSIST_FAILED
    try:
        won = await _engagement_registry.try_transition_terminal(
            rec.id, "error", strict=True, error_kind=kind,
            error_message=message,
            # §2.A (Astra R4): ARMED. The launcher acknowledges it in the same
            # synchronous step that returns the envelope (the envelope
            # reaching the caller IS the telling); a launcher cancelled before
            # that step leaves it armed, and the cancellation owner or the boot
            # replay tells the engager the fact of the outcome.
            owes_terminal_notification=True,
        )
    except Exception as exc:  # noqa: BLE001 — strict persist failed + rolled back
        # Memory and disk agree that the record is LIVE: settle nothing.
        logger.error(
            "launch abort %s (%s): strict terminal transition did not persist "
            "(rolled back, record left live): %s", rec.id[:8], kind, exc)
        return LaunchAbortResult.PERSIST_FAILED
    if not won:
        logger.info(
            "launch abort %s (%s): record already terminal — another writer "
            "owns its end", rec.id[:8], kind)
        return LaunchAbortResult.ALREADY_TERMINAL
    await _settle_job_journal(rec, _resolve_engagement_driver(rec))
    return LaunchAbortResult.ABORTED


def _launch_abort_done(engagement_id: str, task: Any) -> None:
    """#757: the anchored abort's only observer once its launcher has unwound.

    Same reasoning as ``_launch_death_done``, different message: this path
    posts no notice, so "the operator may not have been told" would be false
    here. What is at stake is a terminal record whose job was not settled.
    """
    _LAUNCH_DEATH_TASKS.discard(task)
    if task.cancelled():
        logger.error(
            "launch abort for %s was CANCELLED — its job may not be settled",
            engagement_id[:8])
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "launch abort for %s raised: %r", engagement_id[:8], exc)


def _spawn_launch_abort(
    channel: Any, rec: "EngagementRecord",
    *, kind: str, message: str,
) -> Any:
    """#757: anchor the abort so it outlives its launcher.

    A cancellation delivered between the durable terminal commit and the
    abort's own side effects would leave them undone: the handler's
    ``except asyncio.CancelledError`` arm hands off to
    ``_abort_launch_on_cancel``, whose reporter finds the record already
    terminal and by contract does nothing at all. Casa-owned
    ``asyncio.create_task`` — never ``Query.spawn_task``, so an SDK client
    teardown cannot find or cancel it — anchored in the same set the launch-death
    reporters use, which ``drain_launch_death_reports`` already waits on at a
    graceful stop.
    """
    task = asyncio.create_task(_abort_launch_with_fault(
        channel, rec, kind=kind, message=message))
    _LAUNCH_DEATH_TASKS.add(task)
    task.add_done_callback(
        lambda tsk, _eid=rec.id: _launch_abort_done(_eid, tsk))
    return task


# ---------------------------------------------------------------------------
# _finalize_engagement — shared funnel for completion + cancel
# ---------------------------------------------------------------------------


class FinalizeResult(enum.Enum):
    """G4 D5 (v0.96.0): typed outcome of ``_finalize_engagement``. Truthiness
    preserves the historical bool contract for existing callers — only a WON
    finalize is truthy (Sol g4-r1-6: an untyped falsy return conflated
    "already terminal" with "persist rolled back, record still LIVE", and
    emit_completion acked the latter as success)."""
    FINALIZED = "finalized"
    ALREADY_TERMINAL = "already_terminal"
    PRECONDITION_FAILED = "precondition_failed"   # D2 (unread inbound) — v0.96.0
    PERSIST_FAILED = "persist_failed"

    def __bool__(self) -> bool:
        return self is FinalizeResult.FINALIZED


# #599: how long the finalize funnel gives the whole driver teardown.
_DRIVER_CANCEL_TIMEOUT_S = 20.0

# #632: detached finalize tails, anchored so the event loop cannot collect
# them mid-flight. A tail is created only on the in_casa self-emit path (see
# _finalize_engagement's detach_teardown_tail); the done callback is the only
# observer a tail severed from its (cancelled) awaiter has, so it must
# retrieve the outcome — an unobserved tail failure would be a second
# silent-finalization mode, which is the very defect this exists to fix.
_finalize_tail_tasks: set[asyncio.Task] = set()

# A job that completes from inside a running turn: its teardown waits, at most
# this long, for that turn to end, so the end paint and the end notice carry
# the completing turn's cost. Past the bound it finalizes with the last cost
# known.
_COMPLETION_TURN_END_WAIT_S = 30.0

# Jobs whose end paint (start line and question keyboards) waits for their
# completing turn to end: the terminal observer leaves their keyboards to the
# finalize tail, which retires them once the turn's cost is recorded.
_end_paint_deferred: set[str] = set()


def _completing_turn_running(driver: Any, engagement_id: str) -> bool:
    """A turn is running in the job's session and the driver can wait for
    its end: the completion came from inside that turn."""
    probe = getattr(driver, "turn_open", None)
    if not callable(probe) or not callable(getattr(driver, "wait_turn_end", None)):
        return False
    try:
        return probe(engagement_id) is True
    except Exception:  # noqa: BLE001 — no reading, nothing to wait for
        return False


async def _await_completing_turn(driver: Any, engagement: Any) -> None:
    """Wait, bounded, for the turn that completed the job to end, then
    store the cost its end recorded. Never raises."""
    try:
        ended = await driver.wait_turn_end(engagement.id,
                                           _COMPLETION_TURN_END_WAIT_S)
    except Exception:  # noqa: BLE001 — the teardown goes on regardless
        logger.warning("finalize engagement %s: waiting for the completing "
                       "turn failed", engagement.id[:8], exc_info=True)
        ended = False
    if not ended:
        logger.info("finalize engagement %s: the completing turn did not end "
                    "within %.0fs; finalizing with the last cost known",
                    engagement.id[:8], _COMPLETION_TURN_END_WAIT_S)
    if _engagement_registry is not None:
        try:
            await _engagement_registry.persist_origin(engagement.id)
        except Exception:  # noqa: BLE001 — the in-memory cost still shows
            logger.warning("finalize engagement %s: storing the final cost "
                           "failed", engagement.id[:8], exc_info=True)


def _paint_job_end(engagement: Any) -> None:
    """Retire an ended job's keyboards (the start line painted with its
    outcome, time and cost) — the step the terminal observer left to the
    finalize tail. Never raises."""
    _end_paint_deferred.discard(engagement.id)
    channel = _transport()
    retire = getattr(channel, "retire_job_keyboards", None)
    if not callable(retire):
        return
    rec = (_engagement_registry.get(engagement.id)
           if _engagement_registry is not None else None) or engagement
    try:
        retire(rec)
    except Exception:  # noqa: BLE001 — cosmetic
        logger.warning("retiring job keyboards failed for %s",
                       engagement.id[:8], exc_info=True)


def _finalize_tail_done(engagement_id: str, task: asyncio.Task) -> None:
    _finalize_tail_tasks.discard(task)
    if task.cancelled():
        logger.error(
            "finalize engagement %s: detached finalize tail was CANCELLED — "
            "completion side effects may be incomplete", engagement_id[:8])
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "finalize engagement %s: detached finalize tail raised: %r",
            engagement_id[:8], exc)


async def _finalize_engagement(
    engagement: EngagementRecord,
    *,
    outcome: str,                       # "completed" | "cancelled" | "error"
    text: str,
    next_steps: list[dict],
    driver: Any | None,
    stale_before: float | None = None,
    output_truncated: bool = False,     # Task 6 (spec §4.6)
    inbound_gate: bool = False,         # G4 D1/D2 (v0.96.0): emit/completed only
    detach_teardown_tail: bool = False,  # #632: in_casa self-emit only
) -> "FinalizeResult":
    """End an engagement: update registry, log its end, NOTIFY the assistant.

    Never raises on channel side-effects — logs warnings and continues
    so the registry always reaches a terminal state.

    ``stale_before`` (reap only): the terminal transition wins ONLY if the
    record's ``last_user_turn_ts`` is still older than this cutoff — a record
    revived by a user turn since the reap snapshot is left live.

    Returns a ``FinalizeResult``; only ``FINALIZED`` (truthy) means this
    call won the terminal transition and ran the finalize side-effects.
    ``ALREADY_TERMINAL`` covers already-terminal AND a lost ``stale_before``
    guard; ``PERSIST_FAILED`` means the tombstone write rolled back and the
    record is STILL LIVE (retryable).
    """
    last_summary = engagement.origin.get("job", {}).get("last_summary")
    if outcome in ("cancelled", "error") and last_summary:
        text += f"\nLast progress: {last_summary}"
    now = time.time()

    # 1. Registry transition — atomic and authoritative. Only the first
    #    caller to flip the record terminal runs the finalize side effects
    #    below (L75/L24: guards against a concurrent /cancel racing this
    #    call across a real suspension point, which the naive check-then-act
    #    in emit_completion cannot).
    #    v0.79.0 (§3, Sol r6-2): STRICT persistence — a tombstone-write failure
    #    rolls the record back (full-field) and re-raises rather than leaving an
    #    ended job with no terminal record; treat that as "did not win" so
    #    the record stays live for a retry / boot replay instead of a torn end.
    # G4 D2/D4 (v0.96.0): SYNCHRONOUS terminal hook, evaluated inside the
    # registry's terminal critical section (no suspension between check and
    # flip): the completion inbound GATE (inbound_gate=True: veto on unread
    # depth or a pending ingress reservation). Accessor failures fail OPEN
    # with a warning: a driver bug must not wedge termination forever. #807:
    # open for THAT accessor only — each read below is guarded on its own and
    # contributes only its own value. Inbound messages that die with the
    # engagement are not listed here: each one's ticket holder tells it once,
    # in reply to the message.

    def _terminal_hook():
        if driver is None or not inbound_gate:
            return None
        try:
            _ut = (driver.inbound_unread_texts(engagement.id)
                   if hasattr(driver, "inbound_unread_texts") else [])
            texts = [t for t in list(_ut) if isinstance(t, str)]
        except Exception:  # noqa: BLE001 — fail open, WITHOUT losing the below
            logger.warning(
                "finalize engagement %s: inbound_unread_texts failed — the "
                "reservation and depth arms still stand",
                engagement.id[:8], exc_info=True)
            texts = []
        # #740: the RAW reservation count is read in its OWN try, so an
        # unread-accessor failure can never skip the reservation arm — the arm
        # that survives a session teardown.
        try:
            _hr = (driver.inbound_reservations(engagement.id)
                   if hasattr(driver, "inbound_reservations") else 0)
            resv = _hr if type(_hr) is int else 0
        except Exception:  # noqa: BLE001 — fail open, WITHOUT losing the above
            logger.warning(
                "finalize engagement %s: reservation accessor failed — the "
                "unread gate below still stands", engagement.id[:8],
                exc_info=True)
            resv = 0
        # #740: the VETO reads the DEPTH, not len(texts): a population read
        # from the durable file must never refuse a completion, because
        # nothing the gate can force could clear it. An ABSENT depth accessor
        # falls back to the texts read above (empty when that read failed —
        # a population the gate could not read must not veto); a PRESENT one
        # that raises contributes zero.
        try:
            _du = (driver.inbound_unread_depth(engagement.id)
                   if hasattr(driver, "inbound_unread_depth") else len(texts))
            depth = _du if type(_du) is int else 0
        except Exception:  # noqa: BLE001 — fail open, WITHOUT losing the above
            logger.warning(
                "finalize engagement %s: unread-depth accessor failed — the "
                "reservation arm still stands",
                engagement.id[:8], exc_info=True)
            depth = 0
        if depth or resv:
            return f"unread_inbound depth={depth} reservations={resv}"
        return None

    # The terminal notification is owed unless a quiet scheduled run
    # completed. Decided INSIDE the transition, from the record as it
    # commits: an operator's interaction ends a run's quietness under the
    # same lock, so the decision and the stamp cannot disagree.
    _owes: dict[str, bool] = {}

    def _owes_notification(rec: Any) -> bool:
        _owes["notify"] = not _told_nowhere(rec.origin, outcome)
        return _owes["notify"]

    # A completion from inside a running turn: the teardown waits for that
    # turn to end (bounded), and the terminal observer leaves the end paint
    # to it, so both carry the completing turn's cost.
    await_turn_end = (detach_teardown_tail
                      and _completing_turn_running(driver, engagement.id))
    if await_turn_end:
        _end_paint_deferred.add(engagement.id)

    if _engagement_registry is not None:
        try:
            won = await _engagement_registry.try_transition_terminal(
                engagement.id, outcome,
                completed_at=now if outcome == "completed" else None,
                error_kind="emit_completion_error", error_message=text,
                stale_before=stale_before,
                strict=True,
                terminal_hook=_terminal_hook,
                # #766: THIS funnel is the one terminal writer that announces
                # the outcome to the party that asked for the work, so it is
                # the one that arms the durable obligation to do so — in the
                # same write that commits the terminal, never after it. Every
                # other terminal writer leaves the default and owes nothing.
                # #1301: a quiet run's completed end is told nowhere.
                owes_terminal_notification=_owes_notification,
            )
        except TerminalPreconditionFailed as exc:
            _end_paint_deferred.discard(engagement.id)
            logger.info(
                "Engagement %s completion vetoed by inbound gate: %s",
                engagement.id[:8], exc)
            return FinalizeResult.PRECONDITION_FAILED
        except BaseException as exc:
            _end_paint_deferred.discard(engagement.id)
            if not isinstance(exc, Exception):
                raise
            logger.warning(
                "Engagement %s terminal transition failed to persist "
                "(rolled back, left live): %s",
                engagement.id[:8], exc,
            )
            return FinalizeResult.PERSIST_FAILED
        if not won:
            _end_paint_deferred.discard(engagement.id)
            logger.info(
                "Engagement %s not finalized — already terminal or revived "
                "since snapshot (outcome=%s)",
                engagement.id[:8], outcome,
            )
            return FinalizeResult.ALREADY_TERMINAL

    # Task 6 (spec §4.6): THIS call just won the terminal flip — release
    # the specialist concurrency permit (if any; executor engagements and
    # any engagement created before a SpecialistLimiter was wired never
    # have one) unconditionally, before any of the best-effort side effects
    # below that might raise. Idempotent — safe even if something else
    # already released it via the pre-finalize failure paths in
    # the job launch owner, ``_launch_interactive_engagement`` (which also null it out).
    if engagement.permit is not None:
        engagement.permit.release()

    # #632: freeze the tail's payload inputs NOW — before the first await
    # after the flip. This funnel suspends (the driver teardown, the
    # journal's settle) between the flip and the notification's reads, and the live record's
    # ``origin`` can still be written meanwhile; without this snapshot such a
    # post-terminal write lands in the DelegationComplete payload. The completion notice carries the values
    # that won the flip.
    frozen: dict[str, Any] = {
        "id": engagement.id,
        "kind": engagement.kind,
        "driver": engagement.driver,
        "role_or_type": engagement.role_or_type,
        "started_at": engagement.started_at,
        "origin": dict(engagement.origin),
        # Whether the transition owed the engager a telling — the decision it
        # made under the registry lock; without a registry, the record's.
        "notify": _owes.get("notify",
                            not _told_nowhere(engagement.origin, outcome)),
    }

    # The job's log ends with its outcome and the first line of its summary.
    _job_log(engagement, _end_log_line(outcome, text))

    # #632: everything from driver teardown to the finalized log runs in
    # _finalize_engagement_tail. On the in_casa self-emit path this funnel's
    # host task is one of the SDK client's own child tasks, and the teardown
    # below closes that very client: Query._close_impl cancels _child_tasks
    # (pinned SDK query.py:998-999), and on the container's CPython 3.11
    # (where asyncio.wait_for task-wraps) the CancelledError re-raises past
    # the except clauses around driver.cancel and severs the bus
    # notification and finalized log — permanently, since the record is
    # already terminal. emit_completion
    # opts in to detaching the tail into a Casa-owned task
    # (asyncio.create_task — never Query.spawn_task, so _close_impl cannot
    # find or cancel it), anchored in _finalize_tail_tasks and awaited
    # through asyncio.shield: the SDK handler still unwinds cancelled (and
    # the SDK then correctly skips the control-response write — the tool
    # result is documented-lost on this path) while the tail completes every
    # side effect. Every other caller awaits the same helper inline:
    # identical task, ordering and cancellation semantics as before.
    #
    # A completion from inside a running turn does not await its tail: the
    # tail waits for that turn to end, and the turn cannot end until this
    # tool call has returned. The transition is committed, so the result is
    # already FINALIZED; the Casa-owned tail finishes the side effects.
    if detach_teardown_tail:
        tail = asyncio.create_task(_finalize_engagement_tail(
            engagement, outcome=outcome, text=text,
            next_steps=next_steps, driver=driver, now=now,
            output_truncated=output_truncated, frozen=frozen,
            await_turn_end=await_turn_end,
        ))
        _finalize_tail_tasks.add(tail)
        tail.add_done_callback(
            lambda t, _eid=engagement.id: _finalize_tail_done(_eid, t))
        if await_turn_end:
            return FinalizeResult.FINALIZED
        return await asyncio.shield(tail)
    return await _finalize_engagement_tail(
        engagement, outcome=outcome, text=text,
        next_steps=next_steps, driver=driver, now=now,
        output_truncated=output_truncated, frozen=frozen,
    )


def _with_job_cost(text: str, frozen: dict, now: float,
                   cost_job: dict | None = None) -> str:
    """A job's end notice to its resident carries how long it ran and its
    cost as of its last turn end, so a job with no start line (scheduled or
    agent-started) still shows it when the outcome is reported. ``cost_job``
    is the live job state to read the cost from when the completing turn's
    end recorded it after the terminal flip."""
    job = (frozen.get("origin") or {}).get("job")
    if not isinstance(job, dict):
        return text
    line = "Job ran " + background_jobs.format_elapsed(
        max(0.0, now - float(frozen.get("started_at") or now)))
    cost = (cost_job if isinstance(cost_job, dict) else job).get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        line += f" · ${cost:.2f}"
    return f"{text}\n\n{line}" if text else line


_END_LOG_MARKS = {"completed": "✅", "cancelled": "🛑", "error": "❌"}
_END_LOG_SUMMARY_MAX = 300


def _end_log_line(outcome: str, text: str) -> str:
    """A job's last log line: its outcome, and the first line of its summary
    cut to 300 characters."""
    line = f"{_END_LOG_MARKS.get(outcome, '❌')} {outcome}"
    first = ((text or "").strip().splitlines() or [""])[0].strip()
    if len(first) > _END_LOG_SUMMARY_MAX:
        first = first[:_END_LOG_SUMMARY_MAX - 1] + "…"
    return f"{line} — {first}" if first else line


def _told_nowhere(origin: Any, outcome: str) -> bool:
    """#1301: a quiet scheduled run that completed is told to no one — no
    resident notification, so no resident turn. A cancelled or failed one is
    told as any engagement's end is."""
    return outcome == "completed" and background_jobs.is_quiet_origin(origin)


async def _finalize_engagement_tail(
    engagement: EngagementRecord,
    *,
    outcome: str,
    text: str,
    next_steps: list[dict],
    driver: Any | None,
    now: float,
    output_truncated: bool,
    frozen: dict[str, Any],
    await_turn_end: bool = False,
) -> "FinalizeResult":
    """#632: the finalize funnel's tail — driver teardown, the
    completion notification and the finalized log, in that order.

    ``await_turn_end`` (a completion from inside a running turn): first wait,
    bounded, for that turn to end so its cost is recorded; after the teardown,
    paint the job's end (which the terminal observer left to this tail).

    Payload reads come from ``frozen`` (snapshotted at the terminal flip,
    before the funnel's first post-flip await) — never from the live record,
    whose ``origin`` may still be written post-terminal. The live
    ``engagement`` is passed for ``driver.cancel`` (drivers key off the
    record) and for log lines reading immutable identity only.
    """
    eng_id: str = frozen["id"]
    if await_turn_end:
        await _await_completing_turn(driver, engagement)
    # 3. Tear down driver client
    if driver is not None:
        try:
            # #599: bound the teardown. The in_casa driver's ``cancel`` closes
            # the engagement's SDK client, an unbounded await that sits ahead
            # of the bus notification below; a timeout costs only a client
            # left to close on its own.
            await asyncio.wait_for(
                driver.cancel(engagement), _DRIVER_CANCEL_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.error(
                "finalize engagement %s: driver.cancel exceeded %.0fs — "
                "continuing so the notification still runs",
                engagement.id[:8], _DRIVER_CANCEL_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize engagement %s: driver.cancel failed: %s",
                engagement.id[:8], exc,
            )
    await _settle_job_journal(engagement, driver)
    if await_turn_end:
        _paint_job_end(engagement)
    # The job's host's own conversation learns how a job it did not start
    # ended; the result itself goes to the job's requester, below.
    _job_birth = _record_birth(engagement)
    if _job_birth is not None:
        try:
            _note_task(_job_birth, "started your job",
                       "it finished" if outcome == "completed" else f"it ended ({outcome})")
        except Exception:  # noqa: BLE001 — a note never holds the finalize
            logger.warning("finalize engagement %s: note not recorded",
                           engagement.id[:8], exc_info=True)

    # 4. NOTIFY the assistant (via existing DelegationComplete-shaped pathway).
    # #1301: not for a quiet run's completed end — its plugin already posted
    # whatever the operator needs to see.
    tell = _bus is not None and frozen.get(
        "notify", not _told_nowhere(frozen["origin"], outcome))
    # A job its own host started for someone else: the host hears the end
    # first, in a turn of its own, and that turn decides what the requester is
    # told (``job_owner_turn``). The owed notice is settled only when that
    # turn has ended: an answer is sent in the job's place and acknowledged on
    # delivery as any notice is; a turn that closes silent has more work
    # coming back to the host, so nothing is sent and nothing stays owed; a
    # turn that did not run, failed or was cancelled sends the job's own
    # result with a line saying so.
    owner_cancelled: asyncio.CancelledError | None = None
    if tell and job_owner_turn.owner_role(frozen, outcome) is not None:
        try:
            owner = await job_owner_turn.run_owner_turn(frozen, text)
        except asyncio.CancelledError as exc:
            owner, owner_cancelled = job_owner_turn.OwnerResult("failed"), exc
        if owner.kind == "answer":
            text = owner.text
        elif owner.kind == "silent":
            if _engagement_registry is not None:
                await _engagement_registry.ack_terminal_notification(eng_id)
            logger.info(
                "Engagement %s: %s has more work coming back to it; "
                "its requester is not told yet",
                engagement.id[:8], frozen["role_or_type"])
            tell = False
        else:
            text = text + "\n\n" + job_owner_turn.FOLLOW_UP_FAILED_LINE
    if tell:
        target_role = frozen["origin"].get("role") or "assistant"
        # S3 §6: a specialist's or job's proven posts, as body-free Casa
        # lines after the terminal text — the one announcement the creator
        # is owed (INV-JOB-010) carries them.
        text = _with_post_echo(text, eng_id)
        live = (_engagement_registry.get(eng_id)
                if await_turn_end and _engagement_registry is not None else None)
        text = _with_job_cost(
            text, frozen, now,
            cost_job=((live.origin or {}).get("job") if live is not None else None))
        complete = DelegationComplete(
            delegation_id=eng_id,
            agent=frozen["role_or_type"],
            status="ok" if outcome == "completed" else "error",
            text=text,
            kind="" if outcome == "completed" else outcome,
            message=text,
            origin=dict(frozen["origin"]),
            elapsed_s=now - frozen["started_at"],
            output_truncated=output_truncated,  # Task 6 (spec §4.6)
        )
        # #766: the durable obligation armed at the flip is discharged HERE,
        # and only by the consuming resident reporting that its turn reached
        # the transport. `bus.notify` reports that a message was ACCEPTED onto
        # a queue: an unregistered target is dropped with no exception and no
        # log line, an enqueued message dies with the process at shutdown, and
        # a dispatched turn can still fail to deliver. None of those is a
        # telling, and none of them clears the bit — the boot owner replays it.
        _registry = _engagement_registry

        def _ack_for(engagement_id: str):
            """A FACTORY, not a closure over a loop variable: the callback must
            capture THIS engagement's id. The same shape is required in the
            boot replay owner, where an inline closure binds the last record
            and acknowledges the wrong one."""
            async def _ack() -> None:
                await _registry.ack_terminal_notification(engagement_id)
            return _ack

        try:
            sending = send_engagement_outcome(
                _bus, complete=complete, origin=dict(frozen["origin"]),
                ack=(_ack_for(eng_id) if _registry is not None else None),
                context_extra={"next_steps": next_steps},
                source=frozen["role_or_type"])
            # a cancelled owner's turn still tells the requester: the send
            # is finished before the cancellation goes on
            await (asyncio.shield(sending) if owner_cancelled is not None
                   else sending)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "finalize engagement %s: bus.notify failed: %s",
                engagement.id[:8], exc,
            )
    if owner_cancelled is not None:
        raise owner_cancelled

    # G-4 (v0.33.0): surface the cause when outcome=error so operators
    # have a starting point for triage. Pre-fix the only log line for
    # this path was an unconditional `logger.info(... outcome=error)`
    # with no reason — exploration2 found a configurator engagement
    # finalized error 24s after system_init with zero log evidence of
    # *why*. Upgrade to WARNING and pull whatever reason fields exist
    # off the registry origin (mark_error stashes kind/message there)
    # plus the text that the emit_completion caller (or the cancel
    # path) passed in.
    if outcome == "error":
        error_kind = frozen["origin"].get("error_kind") or "unknown"
        error_message = frozen["origin"].get("error_message") or ""
        reason_from_text = (text or "").strip()
        # Prefer registry-stored kind/message (set by mark_error before
        # finalize), then fall back to the text the model emitted.
        composite_reason = (
            error_message or reason_from_text or "no_reason_provided"
        )
        logger.warning(
            "Engagement %s finalized outcome=error kind=%s reason=%s",
            engagement.id[:8], error_kind, composite_reason,
        )
    else:
        logger.info(
            "Engagement %s finalized outcome=%s",
            engagement.id[:8], outcome,
        )
    return FinalizeResult.FINALIZED


# ---------------------------------------------------------------------------
# Stale-engagement reap (D-4, v0.69.0)
# ---------------------------------------------------------------------------


_ENGAGEMENT_REAP_DAYS_DEFAULT = 7.0


def _engagement_reap_days() -> float:
    """Reap TTL in days from the ``engagement_reap_days`` add-on option
    (env ``ENGAGEMENT_REAP_DAYS``); 0 disables the reap."""
    raw = os.environ.get("ENGAGEMENT_REAP_DAYS", "")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _ENGAGEMENT_REAP_DAYS_DEFAULT


def _resolve_engagement_driver(rec: Any) -> Any | None:
    """Resolve the driver that owns ``rec``'s engagement lifecycle (the
    in-casa driver)."""
    import agent as agent_mod
    return getattr(agent_mod, "active_engagement_driver", None)


def _session_is_working(driver: Any, engagement_id: str) -> bool:
    """A job whose session has a turn running, or that Casa is recovering —
    what the stale reap and the idle suspension both skip."""
    for probe in ("turn_open", "recovering"):
        fn = getattr(driver, probe, None)
        try:
            if callable(fn) and fn(engagement_id) is True:
                return True
        except Exception:  # noqa: BLE001 — no reading, not working
            logger.debug("%s probe failed for %s", probe, engagement_id[:8],
                         exc_info=True)
    return False


async def reap_stale_engagements(*, ttl_days: float | None = None) -> int:
    """Cancel engagements with no user turn for ``ttl_days`` (D-4, v0.69.0).

    Interrupted/abandoned engagements used to linger active/idle forever
    (25-day-stale engagement, 2026-07-10; restart orphan reaped manually,
    2026-07-11). Runs in the daily engagement sweep BEFORE the idle-reminder
    pass so a to-be-reaped record doesn't get a pointless reminder in the
    same run. Goes through ``_finalize_engagement`` — the same funnel as a
    manual cancel — so the RIGHT driver stops the process and the
    assistant is notified.
    ``stale_before=cutoff`` makes the staleness check part of the
    locked terminal transition, so a record revived by a user turn between
    the snapshot below and the transition is NOT cancelled. Returns the
    number actually reaped.
    """
    if _engagement_registry is None:
        return 0
    if ttl_days is None:
        ttl_days = _engagement_reap_days()
    if ttl_days <= 0:
        return 0
    now = time.time()
    cutoff = now - ttl_days * 86400
    reaped = 0
    for rec in list(_engagement_registry.active_and_idle()):
        if rec.last_user_turn_ts >= cutoff:
            continue
        if _session_is_working(_resolve_engagement_driver(rec), rec.id):
            # A turn is running (one can run for hours with no turn end), or
            # Casa is recovering the job's session: a working job is never
            # stale.
            continue
        idle_days = int((now - rec.last_user_turn_ts) // 86400)
        logger.info(
            "reaping stale engagement %s (%s/%s, idle %dd > ttl %gd)",
            rec.id[:8], rec.kind, rec.role_or_type, idle_days, ttl_days,
        )
        try:
            won = await _finalize_engagement(
                rec, outcome="cancelled",
                text=(
                    f"Auto-closed after {idle_days} days with no activity "
                    f"(reap TTL {ttl_days:g}d). Start a new engagement to "
                    "continue this work."
                ),
                next_steps=[],
                driver=_resolve_engagement_driver(rec),
                stale_before=cutoff,
            )
            if won:
                reaped += 1
        except Exception:  # noqa: BLE001 — one bad record must not stop the sweep
            logger.warning("reap of engagement %s failed", rec.id[:8], exc_info=True)
    return reaped


# ---------------------------------------------------------------------------
# emit_completion — called by the engaged agent
# ---------------------------------------------------------------------------


# B-3 (v0.69.3): the doctrine's status vocabulary (completion.md:31), each
# mapped to its TRUE registry outcome. "error" kept as a legacy alias — the
# tool historically treated any non-ok status as error, so agents may pass it.
_COMPLETION_STATUS_TO_OUTCOME = {
    "ok": "completed",
    "partial": "completed",   # objectives partly met — text carries the marker
    "failed": "error",
    "error": "error",
    "cancelled": "cancelled",
}
_COMPLETION_TEXT_MAX = 8000

# The completion gate's refusal. In a job's continuous session the refusal is
# itself the delivery: it is a tool result, so the message waiting for the job
# is taken up right after it.
_UNREAD_REFUSAL = ("An operator message reaches you at your next step. Read "
                   "it, then call emit_completion again.")

@tool(
    "report_job_progress",
    "Record a one-line progress note for this background job, any time "
    "something is worth telling; it appears in your log when a log group is "
    "set up. Optional: the job runs and finishes "
    "whether or not you call it. `done`/`remaining` are optional and only "
    "shown to the operator: pass them when this work has a real unit and a "
    "known total.",
    {"type": "object", "properties": {
        "summary": {"type": "string"},
        "done": {"type": "integer", "minimum": 0},
        "remaining": {"type": "integer", "minimum": 0}},
     "required": ["summary"]},
)
async def report_job_progress(args: dict) -> dict:
    rec = engagement_var.get(None)
    rec = _engagement_registry.get(rec.id) if rec and _engagement_registry else None
    if rec is None or rec.status not in ("active", "idle") or not rec.origin.get("job"):
        return _result({"ok": False, "kind": "not_a_job"})
    summary = args.get("summary")
    done, remaining = args.get("done"), args.get("remaining")
    # `type(...) is not int` keeps booleans out of the counts.
    if not isinstance(summary, str) or any(
        value is not None and (type(value) is not int or value < 0)
        for value in (done, remaining)
    ):
        return _result({"ok": False, "kind": "invalid_arguments"})
    summary = (summary.splitlines() or [""])[0][:300]
    # Recorded before any await: the record's "Last progress" (the parking
    # line, a failed end) and the turn-end guard's step-ceiling rule read it.
    rec.origin["job"]["last_summary"] = summary
    background_jobs.note_progress(rec.id)
    text = f"📊 {summary}"
    if done is not None:
        text += f" · {done} done"
    if remaining is not None:
        text += f" · {remaining} left"
    _job_log(rec, text)
    await _engagement_registry.persist_origin(rec.id)
    return _result({"ok": True})


@tool(
    "ask_operator",
    "Ask the operator something this job cannot settle on its own: a decision, a "
    "choice, missing information. Casa posts the question in the operator's direct "
    "chat, under your name, with one button per option and a Cancel job button; their "
    "answer — a tap, or a reply in words — reaches you as a message. Carry on with "
    "other work if there is any; if there is none, end your turn and wait. Casa does "
    "not prompt you while the question is open. Asking again posts the question "
    "again; either copy answers it. Only a specialist's background job can ask.",
    {"type": "object", "properties": {
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}}},
     "required": ["question"]},
)
async def ask_operator(args: dict) -> dict:
    rec = engagement_var.get(None)
    rec = _engagement_registry.get(rec.id) if rec and _engagement_registry else None
    if (rec is None or rec.kind != "specialist" or not rec.origin.get("job")
            or not _engagement_registry.counts_as_live(rec.id)):
        return _result({"status": "error", "kind": "not_a_job"})
    question = args.get("question")
    options = args.get("options")
    options = [] if options is None else options
    if (not isinstance(question, str) or not question.strip()
            or len(question) > _ASK_QUESTION_MAX
            or not isinstance(options, list) or len(options) > _ASK_OPTIONS_MAX
            or any(not isinstance(o, str) or not o.strip()
                   or len(o) > _ASK_OPTION_LABEL_MAX for o in options)):
        return _result({
            "status": "error", "kind": "invalid_arguments",
            "message": (f"question: a non-empty string of at most "
                        f"{_ASK_QUESTION_MAX} characters; options: at most "
                        f"{_ASK_OPTIONS_MAX} non-empty strings of at most "
                        f"{_ASK_OPTION_LABEL_MAX} characters each")})
    if getattr(rec, "delivery", None) is None:
        # a job that posts nothing has no chat to ask in
        refusal = _surface_refusal(str(getattr(rec, "executor", "") or rec.role_or_type))
        return _result({"status": "error", "kind": refusal.kind,
                        "message": refusal.text})
    channel = _transport()
    if channel is None or not hasattr(channel, "post_job_question"):
        return _result({"status": "error", "kind": "delivery_failed",
                        "message": "the operator's chat is not available"})
    scope = _current_scope(_snapshot_origin())
    if scope is None:
        return _result({"status": "error", "kind": "unsupported_origin",
                        "message": _NO_SCOPE_MESSAGE})
    # A quiet run that asks stops being quiet, durably, before it asks — so
    # its end is told like any job's. Entered for any run carrying the stamp:
    # one another promotion cleared in memory may still roll back, and the
    # registry decides under its lock.
    if (background_jobs.was_quiet_run(rec)
            and not await _engagement_registry.promote_from_quiet(rec.id)):
        return _result({"status": "error", "kind": "not_asked",
                        "message": "the question could not be recorded; ask again"})
    import result_broker
    from channels.telegram import render_ask_body, resolve_button_labels
    captions = resolve_button_labels(list(options), multi=False)
    # labelled except in the agent's own chat, where the chat is the agent
    label = result_broker.label_at(rec.role_or_type, rec.delivery)
    body = ((f"{label}\n" if label else "")
            + render_ask_body(None, question, list(options), captions))
    qid = _engagement_registry.new_job_question(rec.id, question, options)
    if qid is None:
        return _result({"status": "error", "kind": "not_a_job"})
    # Posted at the job's delivery. A job that posts nothing, or a delivery
    # whose bot Casa no longer runs, is reported like a post that failed:
    # nothing reached the operator.
    try:
        message_id = await channel.post_job_question(
            rec, qid, scope.admit(IntentKind.KEYBOARD, body), list(options),
            captions)
    except (RouteMissing, AddressError, EndpointUnavailable) as exc:
        logger.error("job %s question not posted: %s", rec.id[:8], exc)
        message_id = None
    if message_id is None:
        return _result({"status": "error", "kind": "delivery_failed",
                        "message": "the question was not posted; you may ask again"})
    # The job now waits for the operator: a turn that ends from here is not
    # nudged, and a restart resumes it only when the operator answers.
    background_jobs.mark_awaiting(rec.id, background_jobs.AWAITING_ASKED)
    return _result({"status": "asked", "message_id": message_id})


@tool(
    "emit_completion",
    "End this engagement: the job ends, and the user's messages no longer "
    "reach you. The agent that started the engagement receives the "
    "summary. Must be called "
    "from inside an active engagement. status: 'ok' | 'partial' | 'failed' | "
    "'cancelled'.",
    # Explicit schema: next_steps and status are optional in the handler and
    # in every recipe's example call; the shorthand would make all three
    # required.
    {"type": "object",
     "properties": {"text": {"type": "string"},
                    "next_steps": {"type": "array"},
                    "status": {"type": "string"}},
     # Round 2 E3: recipes/executor/scaffold.md's `emit_completion(status="partial")`
     # carries no text; the handler defaults every field, so none is required.
     "required": []},
)
async def emit_completion(args: dict) -> dict:
    engagement = engagement_var.get(None)
    if engagement is None:
        return _result({
            "status": "error",
            "kind": "not_in_engagement",
            "message": "emit_completion called outside an engagement",
        })

    # Bug 9 (v0.14.6): idempotency. Re-emitting completion (e.g. SDK
    # retry, hook misfire) used to re-run _finalize_engagement, which
    # double-NOTIFYs the assistant. Re-read the
    # live registry
    # state so we catch transitions that happened on another in-flight
    # turn since this engagement_var snapshot was taken.
    if _engagement_registry is not None:
        live = _engagement_registry.get(engagement.id)
        if live is not None and live.status in (
            "completed", "cancelled", "error",
        ):
            return _result({
                "status": "acknowledged",
                "kind": "already_terminal",
                "message": (
                    f"engagement is already {live.status!r}; "
                    "emit_completion is a no-op."
                ),
            })

    # B-3 (v0.69.3): validate BEFORE any side effect. The old mapping sent
    # EVERY status other than exactly "ok" — including the doctrine's own
    # "partial"/"cancelled", or a model writing "success" — into a terminal
    # outcome=error kind=emit_completion_error, failing fully-successful
    # engagements (2026-07-12 00:14Z incident). A malformed call now comes
    # back as a TOOL error the agent can correct; the engagement stays live.
    status_in = args.get("status", "ok") or "ok"
    if not isinstance(status_in, str) or status_in not in _COMPLETION_STATUS_TO_OUTCOME:
        return _result({
            "status": "error", "kind": "invalid_status",
            "message": (
                f"status={status_in!r} is not a valid completion status; use "
                "'ok' | 'partial' | 'failed' | 'cancelled' (completion.md). "
                "The engagement is still active — call emit_completion again "
                "with a valid status."
            ),
        })
    text = args.get("text", "") or ""
    if not isinstance(text, str):
        return _result({
            "status": "error", "kind": "invalid_args",
            "message": ("text must be a string (got "
                        f"{type(text).__name__}). The engagement is still "
                        "active — call emit_completion again."),
        })
    next_steps = args.get("next_steps") or []
    if not isinstance(next_steps, list):
        return _result({
            "status": "error", "kind": "invalid_args",
            "message": ("next_steps must be a list (got "
                        f"{type(next_steps).__name__}). The engagement is "
                        "still active — call emit_completion again."),
        })
    # Task 6 (spec §4.6): the interactive output bound. This ALSO records
    # the `output_truncated` flag that flows to the DelegationComplete
    # notification, so a clipped engagement answer is disclosed to the
    # narrating resident (mirrors the sync/async truncation flag).
    output_truncated = len(text) > _COMPLETION_TEXT_MAX
    if output_truncated:
        logger.warning(
            "emit_completion text truncated (%d > %d chars) for engagement %s",
            len(text), _COMPLETION_TEXT_MAX, engagement.id[:8],
        )
        text = text[:_COMPLETION_TEXT_MAX] + " … [truncated]"
    if status_in == "partial":
        text = f"[partial] {text}" if text else "[partial]"
    outcome = _COMPLETION_STATUS_TO_OUTCOME[status_in]

    # Driver is discovered via the agent singleton accessible through the
    # agent module (plan-1 pattern).
    driver = None
    try:
        import agent as agent_mod  # noqa: F401
        driver = getattr(agent_mod, "active_engagement_driver", None)
    except Exception:
        pass

    # G4 D1 (v0.96.0): completion inbound gate (INV-ENG-003) — an operator
    # message admitted, or written and not yet taken up, refuses a successful
    # completion; the refusal's tool result is where the job reads it. Gates
    # ONLY successful outcomes; failed/error exits stay ungated (a broken
    # engagement must be able to die) and rely on the D4 annotation.
    if (outcome == "completed" and driver is not None
            and hasattr(driver, "inbound_unread_depth")):
        try:
            _d = driver.inbound_unread_depth(engagement.id)
            _r = (driver.inbound_reservations(engagement.id)
                  if hasattr(driver, "inbound_reservations") else 0)
        except Exception:  # noqa: BLE001 — fail open, never wedge completion
            _d = _r = 0
        # STRICT int typing (fail open otherwise): a duck/mock driver whose
        # accessors return non-ints must read as "no unread input", never as
        # a fabricated depth (int(MagicMock()) == 1 bit us in the gate).
        _depth = _d if type(_d) is int else 0
        _resv = _r if type(_r) is int else 0
        if _depth > 0 or _resv > 0:
            return _result({
                "status": "error", "kind": "unread_inbound",
                "retryable": True,
                "message": _UNREAD_REFUSAL,
            })

    finalization = _finalize_engagement(
        engagement,
        outcome=outcome,
        text=text,
        next_steps=next_steps,
        driver=driver,
        output_truncated=output_truncated,  # Task 6 (spec §4.6)
        inbound_gate=(outcome == "completed"),   # G4 D1/D2
        # #632: an in_casa emit_completion runs inside a child task of the
        # very client the funnel's teardown closes — the one topology in
        # which the teardown cancels the funnel's own host. Detach the tail
        # so the completion side effects survive that self-cancel. Every
        # other caller (cancel/reap) keeps the default fully-synchronous path.
        detach_teardown_tail=(driver is not None),
    )
    # The WHOLE finalization — the terminal transition and every side effect
    # after it — runs in a Casa-owned task created before the transition; this
    # callback only awaits it shielded. Retiring the job's session disconnects
    # its client, which cancels the client's outstanding tool callbacks, this
    # one included: a committed completion must still reach its notification.
    from drivers.in_casa_driver import InCasaDriver
    if isinstance(driver, InCasaDriver):
        fin = await asyncio.shield(driver.spawn_owned(finalization))
    else:
        fin = await finalization
    # G4 D2 (v0.96.0): the registry-internal gate vetoed the flip (a message
    # landed between the handler gate above and the terminal critical
    # section). Refuse with the same retryable contract.
    if fin is FinalizeResult.PRECONDITION_FAILED:
        return _result({
            "status": "error", "kind": "unread_inbound", "retryable": True,
            "message": _UNREAD_REFUSAL,
        })
    # G4 D5 (v0.96.0): a persist rollback leaves the record LIVE — surface a
    # retryable error instead of acking a completion that did not happen.
    if fin is FinalizeResult.PERSIST_FAILED:
        return _result({
            "status": "error", "kind": "finalize_persist_failed",
            "retryable": True,
            "message": ("completion could not be persisted; the engagement "
                        "is still active — call emit_completion again"),
        })
    return _result({"status": "acknowledged"})


# ---------------------------------------------------------------------------
# query_engager — retrieval + bounded synthesis
# ---------------------------------------------------------------------------


_QUERY_ENGAGER_SYSTEM = (
    "You answer factually using ONLY the provided context. If the context "
    "does not answer the question, reply with exactly: UNKNOWN"
)


async def _synthesize_answer(
    question: str, context: str, max_tokens: int,
) -> str:
    """Run a constrained Anthropic pass via the SDK. Returns the synthesized
    answer, or the literal string 'UNKNOWN' if the context is insufficient.

    Runs on Haiku (#1353: pinned through ``resolve_model``, never a bare CLI
    alias). No tools. No streaming — the caller needs a single string.
    """
    model = resolve_model("haiku")
    # The pinned claude-agent-sdk's ClaudeAgentOptions has no
    # max_tokens/max_output_tokens field; cap output via the documented
    # Claude Code CLI env knob instead (env merges over the inherited
    # environment for this one CLI subprocess only).
    options = ClaudeAgentOptions(
        model=model,
        effort=effort_for(model),
        cli_path=CLAUDE_CLI_PATH,
        max_buffer_size=SDK_MAX_BUFFER_SIZE,
        system_prompt=_QUERY_ENGAGER_SYSTEM,
        max_turns=1,
        mcp_servers={},
        disallowed_tools=with_cross_session_tools_denied(()),
        settings=cli_session_settings(),
        setting_sources=ephemeral_setting_sources(),
        extra_args=ephemeral_extra_args(),
        env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max(1, max_tokens))},
    )
    prompt = (
        f"Context:\n{context}\n\nQuestion: {question}\n\n"
        f"Answer concisely, in at most about {max_tokens} tokens."
    )
    out = ""
    _api_error = None      # #568
    _synth_result = None
    eng = engagement_var.get(None)
    eng_id = eng.id[:8] if eng is not None else None
    async with ClaudeSDKClient(with_system_prompt_file(
        sdk_logging.with_stderr_callback(options, engagement_id=eng_id)),
    ) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                # #568: the CLI's own error prose is not a synthesized answer.
                # Folding it here would hand the caller an "answer" that is a
                # CLI error string; skipping it alone would hand back "" and be
                # reported as a successful empty answer. Neither is honest —
                # raise so the caller reports the synthesis as unavailable.
                _kind = api_error_kind(msg)
                if _kind is not None:
                    _api_error = _api_error or _kind
                    continue
                for b in getattr(msg, "content", []):
                    if isinstance(b, TextBlock):
                        out += b.text
            elif isinstance(msg, ResultMessage):
                _synth_result = msg
    # The second carrier, as in every other read loop: a refusal reported only
    # on the terminal result must not come back as a successful empty answer.
    if _api_error is None:
        _api_error = result_api_error_kind(_synth_result)
    if _api_error is not None:
        raise ApiErrorTurn(_api_error)
    out = out.strip()
    # Belt-and-braces hard stop in case the CLI/model still overshoots.
    from tokens import estimate_tokens
    if estimate_tokens(out) > max_tokens:
        out = out[: max_tokens * 4].rstrip()
    return out


#: Tokens of the engager's view handed to the synthesizer.
_ENGAGER_VIEW_TOKENS = 2000


def _engager_context(store, question: str, clearance: str) -> str:
    """What the engager knows that bears on *question*, read at *clearance*
    (blocking): its view of the ledger that clearance reads, within
    :data:`_ENGAGER_VIEW_TOKENS`, then the first page of a search of the
    saved conversation. ``""`` when neither holds anything."""
    from memory import note as memory_note
    from memory import search as memory_search
    view, _ = memory_note.with_view(
        store, clearance=clearance, budget=_ENGAGER_VIEW_TOKENS)
    parts = [view.text] if view is not None and view.text else []
    if memory_search.terms(question):
        found = memory_search.search(store, question, clearance=clearance)
        lines = [f"[{h['date']} {h['time']}] {h['speaker']}: {h['text']}"
                 for h in found.hits]
        if lines:
            parts.append("Saved conversation matching the question:\n"
                         + "\n".join(lines))
    return "\n\n".join(parts)


@tool(
    "query_engager",
    "Ask the engaging agent a question. status=ok returns the answer "
    "synthesized from the engager's clearance-filtered memory; status=unknown "
    "means the search of readable memory found nothing — NOT proof of "
    "absence, since entries above this engagement's clearance are never "
    "searched; status=unavailable means "
    "memory could not be checked (do not conclude the information doesn't "
    "exist). Callable only from inside an active engagement.",
    {"question": str, "max_tokens": int},
)
async def query_engager(args: dict) -> dict:
    engagement = engagement_var.get(None)
    if engagement is None:
        return _result({"status": "error", "kind": "not_in_engagement",
                        "message": "query_engager called outside an engagement"})
    question = args.get("question", "") or ""
    # #201 (Sol + Terra, Blocking): a blank question performed no search, yet
    # fell through to `status=unknown` — which this tool's own description
    # defines as "the memory was searched and holds nothing relevant". An
    # executor asking a malformed question was told the engager remembers
    # nothing. Reject it at the boundary, exactly as `search_memory` does.
    if not question.strip():
        return _result({
            "status": "error", "kind": "empty_query",
            "message": (
                "query_engager requires a non-blank question. Nothing was "
                "searched, so this is not a statement about what the engager "
                "remembers — ask again with a real question."
            ),
        })
    max_tokens = max(1, min(int(args.get("max_tokens") or 500), 4000))

    # Retrieve engager-side context: the engager's view of its ledger plus a
    # search of the saved conversation, at the clearance of the turn that
    # created the engagement (#336: the record's own persisted markers).
    store = _memory_store()
    context = ""
    memory_unavailable = store is None
    if store is not None:
        try:
            # #369: the record is live and the clamp monotonic — a downgrade
            # landing while the read runs re-reads at the new floor.
            _q_clearance = engagement.origin.get("_origin_clearance")
            context = await _read_memory(
                engagement.origin,
                lambda clearance: _engager_context(store, question, clearance))
        except Exception:  # noqa: BLE001 — never a fake "unknown"
            logger.warning("query_engager: memory could not be read",
                           exc_info=True)
            memory_unavailable = True

    if memory_unavailable:
        return _result({
            "status": "unavailable", "text": "",
            "message": (
                "The engager's memory could not be checked (store "
                "unavailable). Do not conclude the information doesn't exist."
            ),
        })
    # #472: "unknown" is a statement about the READABLE slice only — an
    # empty context cannot distinguish "never told" from "not readable at
    # this engagement's clearance". Say so, or the executor asserts absence.
    _unknown_note = (
        "The engager's readable memory returned nothing for this question. "
        "NOT proof of absence: entries above this engagement's clearance "
        "are never searched. Do not conclude the information doesn't exist."
    )
    if not context:
        return _result({"status": "unknown", "text": "", "message": _unknown_note})
    # #369: synthesis awaits too — a downgrade landing mid-synthesis means the
    # answer was drawn from context above the engagement's new floor. Discard
    # and re-run recall+synthesis at the current markers (bounded: the clamp
    # is one-way over a handful of tiers).
    from sensitivity import TIERS as _ALL_TIERS_SYN
    answer = ""
    for _ in range(len(_ALL_TIERS_SYN)):
        try:
            answer = await _synthesize_answer(question, context, max_tokens)
        except ApiErrorTurn as exc:
            # #568: synthesis was refused or faulted upstream. Saying
            # "unknown" here would assert the engager's memory holds no
            # answer — a claim this run never established.
            logger.warning("query_engager synthesis api error kind=%s",
                           exc.kind.value)
            return _result({
                "status": "unavailable", "text": "",
                "message": (
                    "The engager's answer could not be synthesized (the "
                    "model call did not complete). Do not conclude the "
                    "information doesn't exist."
                ),
            })
        if engagement.origin.get("_origin_clearance") == _q_clearance:
            break
        _q_clearance = engagement.origin.get("_origin_clearance")
        try:
            context = await _read_memory(
                engagement.origin,
                lambda clearance: _engager_context(store, question, clearance))
        except Exception:  # noqa: BLE001 — never a fake "unknown"
            logger.warning("query_engager: memory could not be read",
                           exc_info=True)
            return _result({
                "status": "unavailable", "text": "",
                "message": (
                    "The engager's memory could not be checked. Do not "
                    "conclude the information doesn't exist."
                ),
            })
        if not context:
            return _result(
                {"status": "unknown", "text": "", "message": _unknown_note})
    if answer.strip().upper().startswith("UNKNOWN"):
        return _result({"status": "unknown", "text": "", "message": _unknown_note})
    return _result({"status": "ok", "text": answer})


@tool(
    "cancel_engagement",
    "Cancel an in-flight engagement by id and NOTIFY the engager.",
    {"engagement_id": str},
)
async def cancel_engagement(args: dict) -> dict:
    engagement_id = args.get("engagement_id", "") or ""
    if _engagement_registry is None:
        return _result({"status": "error", "kind": "not_initialized",
                        "message": "engagement registry not initialized"})
    rec = _engagement_registry.get(engagement_id)
    if rec is None:
        return _result({"status": "error", "kind": "unknown_engagement",
                        "message": f"no engagement named {engagement_id!r}"})
    if rec.status in ("completed", "cancelled", "error"):
        # L75/L24: a late cancel against an engagement that already
        # finalized (e.g. it raced emit_completion and lost) gets a
        # truthful reply instead of a silent no-op / duplicate finalize.
        return _result({"status": "acknowledged", "kind": "already_terminal",
                        "message": f"engagement is already {rec.status!r}"})

    driver = None
    try:
        import agent as agent_mod  # noqa: F401
        driver = getattr(agent_mod, "active_engagement_driver", None)
    except Exception:
        pass

    fin = await _finalize_engagement(
        rec, outcome="cancelled", text="Engagement cancelled.",
        next_steps=[], driver=driver,
    )
    # #289: mirror emit_completion's contract instead of acking
    # unconditionally — a persist rollback leaves the record LIVE, and a
    # lost race means something else already finalized it.
    if fin is FinalizeResult.ALREADY_TERMINAL:
        return _result({"status": "acknowledged", "kind": "already_terminal",
                        "message": "engagement was already terminal"})
    if not fin:  # PERSIST_FAILED (no other non-won outcome is reachable
        #          here: the inbound gate only arms for outcome="completed")
        return _result({
            "status": "error", "kind": "finalize_persist_failed",
            "retryable": True,
            "message": ("cancellation could not be persisted; the "
                        "engagement is still active — call "
                        "cancel_engagement again"),
        })
    return _result({"status": "ok", "engagement_id": engagement_id})


# ---------------------------------------------------------------------------
# config_trigger_upsert / config_trigger_delete — the configurator's ONE way
# to change agents/<role>/triggers.yaml (#403)
# ---------------------------------------------------------------------------
#
# That file has a second writer: the resident's own reminder tools, running in
# THIS process on THIS loop. The configurator runs in a separate CLI child
# process, so its Read→Edit spans model thinking time and no lock may be held
# across it; a reminder set inside that window was silently discarded by the
# stale rewrite, and `git add -A` committed the loss. Hand-editing is now denied
# by `hooks.trigger_file_write_guard`, and the change is made HERE instead —
# where the whole read-modify-write is a single synchronous step on the loop and
# cannot interleave with the reminder writer at all.
#
# Ownership is preserved in both directions: these refuse an entry carrying
# `managed_by: agent` (the resident's reminders), exactly as the reminder tools
# refuse everything else.


# The trigger fields, mirroring defaults/schema/triggers.v1.json. Declared
# explicitly rather than as a free-form object so the model gets a typed
# surface — and so a key the schema would reject as `additionalProperties`
# never reaches the writer in the first place.
_TRIGGER_ENTRY_FIELDS = {
    "name": {"type": "string"},
    "type": {"type": "string",
             "enum": ["interval", "cron", "webhook", "date"]},
    "minutes": {"type": "integer"},
    "schedule": {"type": "string"},
    "at": {"type": "string"},
    "one_shot": {"type": "boolean"},
    # Legacy `schema_version: 1` documents only. There a webhook trigger
    # REQUIRES `path`; v2 removed it (the trigger name is the endpoint) and
    # forbids it. Omitting the field from this surface left a v1 file with no
    # supported route at all — the hand edit is denied and the tool could not
    # express what the schema demands. Present here, refused by the schema on a
    # v2 document, which is the correct answer rather than a dead end.
    "path": {"type": "string"},
    "channel": {"type": "string"},
    "prompt": {"type": "string"},
    "prompt_file": {"type": "string"},
    "clearance": {"type": "string",
                  "enum": ["public", "friends", "family"]},
    # S6: a scheduled entry that starts a plugin job instead of a resident turn
    "job": {"type": "string"},
    "task": {"type": "string"},
    "context": {"type": "string"},
    "auth": {"type": "object", "properties": {
        "mode": {"type": "string",
                 "enum": ["hmac_body", "static_header", "timestamped_hmac"]},
        "header": {"type": "string"},
        "tolerance_secs": {"type": "integer"},
        "secret_owner": {"type": "string", "enum": ["casa", "provider"]}}},
}


def _resident_triggers_path(role: str) -> "tuple[str | None, dict | None]":
    """``(path, error_result)`` for *role*'s trigger file.

    Residents only: ``triggers.yaml`` is FORBIDDEN for a specialist or an
    executor and creating one there is boot-fatal, so an unknown or non-resident
    role is refused rather than written."""
    import agent as agent_mod
    runtime = getattr(agent_mod, "active_runtime", None)
    if runtime is None:
        return None, _result({
            "status": "error", "kind": "not_initialized",
            "message": "CasaRuntime not bound - boot ordering bug"})
    if role not in (getattr(runtime, "role_configs", None) or {}):
        return None, _result({
            "status": "error", "kind": "unknown_role",
            "message": (
                f"{role!r} is not a resident. Triggers belong to residents "
                f"only — a specialist or executor must not have a "
                f"triggers.yaml at all. Known residents: "
                f"{sorted(getattr(runtime, 'role_configs', None) or {})}"),
        })
    import reminders
    return reminders.triggers_path(runtime.agents_dir, role), None


@tool(
    "config_trigger_upsert",
    "Add a trigger to a resident's triggers.yaml, or replace the existing "
    "trigger of the same name (keeping its position). Validates the entry "
    "against the triggers schema and leaves every other entry untouched — this "
    "is the ONLY way to write that file; hand-editing it is denied, because a "
    "resident's reminder tools write the same file from another writer and a "
    "hand edit silently discards whatever landed since you read it. A `job` "
    "entry (`<plugin manifest name>:<job name>`, no prompt) is checked here: "
    "the call refuses a job the resident cannot start (`job_not_declared`, "
    "listing the jobs it can), and its ok result names where each fire starts "
    "the job (`job_host`). Does NOT "
    "commit: follow with config_git_commit, which reloads the role's triggers. "
    "Restricted to the configurator executor role.",
    {"type": "object",
     "properties": {"role": {"type": "string"}, **_TRIGGER_ENTRY_FIELDS},
     "required": ["role", "name", "type"]},
)
async def config_trigger_upsert(args: dict) -> dict:
    caller = _effective_caller_role()
    if caller not in _PRIVILEGED_CONFIG_ROLES:
        return _refuse_unprivileged("config_trigger_upsert", caller)

    role = str(args.get("role") or "")
    path, err = _resident_triggers_path(role)
    if err is not None:
        return err

    entry = {k: args[k] for k in _TRIGGER_ENTRY_FIELDS if k in args}
    host = None
    if entry.get("job"):
        # S6 §3.2: a job entry is judged here, at the write — the launcher's own
        # text-channel rule, then startability by THIS resident now (the fire
        # judges it again at every occurrence; registration never does, R1-2)
        if str(entry.get("channel") or "") != "telegram":
            return _result({"status": "error", "kind": "job_needs_text_channel",
                            "message": "A scheduled job runs only on telegram."})
        import background_jobs
        cfg = _agent_role_map.get(role)
        delegates = [d.agent for d in (getattr(cfg, "delegates", None) or [])]
        host = background_jobs.find_job_host(str(entry["job"]), role, delegates)
        if host is None:
            names = [h.decl.qualified_name for h in background_jobs.startable_jobs(role, delegates)]
            return _result({"status": "error", "kind": "job_not_declared",
                            "message": "Startable jobs: " + (", ".join(names) or "none")})
    import reminders
    try:
        # Off the loop, under trigger_write_lock.PASS_LOCK (#458). This edit
        # used to run synchronously on the loop precisely so it could not
        # interleave with the reminder writer (#403); the shared lock now gives
        # that serialization directly — across the reminder writer AND the
        # config_sync pass — so the write no longer has to hold the loop to be
        # safe, and must not, since the lock a pass holds would otherwise stall
        # it.
        outcome = await asyncio.to_thread(reminders.upsert_entry, path, entry)
    except (OSError, ValueError) as exc:
        return _result({"status": "error", "kind": "trigger_write_refused",
                        "message": str(exc)})
    out = {"status": "ok", "outcome": outcome, "role": role, "name": entry["name"]}
    host_ref = None
    if host is not None:
        host_ref = f"{host.kind}:{host.role}"
        out["job_host"] = host_ref
    out["message"] = _upsert_ok_message(entry["name"], outcome, role,
                                        job=entry.get("job"), host=host_ref)
    return _result(out)


def _upsert_ok_message(name: str, outcome: str, role: str, *,
                       job: "str | None" = None, host: "str | None" = None) -> str:
    """The ok message of ``config_trigger_upsert``. #1437: for a job entry it
    states where the job starts, so the configurator never reports a startable
    job as one that will not run, nor a plugin grant to the resident as needed."""
    msg = (f"{name} {outcome} in {role}/triggers.yaml — now commit it: "
           "config_git_commit, which reloads the role's triggers")
    if job and host:
        if host.startswith("specialist:"):
            where = (f"{host}, one of {role}'s delegates, which holds the plugin; "
                     f"{role} needs no assignment of that plugin for it")
        else:
            where = f"{role} itself, through its own assignment of that plugin"
        msg += (f". Job {job} is startable now, on {where}. Each fire resolves "
                f"the host again and starts the job if it still can.")
    return msg


@tool(
    "config_trigger_delete",
    "Remove one trigger from a resident's triggers.yaml by name, leaving every "
    "other entry untouched. Refuses a reminder the resident owns "
    "(managed_by: agent) — ask the resident to cancel that one. Also deletes "
    "the prompt file the trigger named (prompt_file under the role's prompts/) "
    "unless something else still uses it; the result's prompt_file says which. "
    "This is the "
    "ONLY way to change that file; hand-editing it is denied. Does NOT commit: "
    "follow with config_git_commit, which reloads the role's triggers. "
    "Restricted to the configurator executor role.",
    {"role": str, "name": str},
)
async def config_trigger_delete(args: dict) -> dict:
    caller = _effective_caller_role()
    if caller not in _PRIVILEGED_CONFIG_ROLES:
        return _refuse_unprivileged("config_trigger_delete", caller)

    role = str(args.get("role") or "")
    name = str(args.get("name") or "")
    if not name:
        return _result({"status": "error", "kind": "name_required",
                        "message": "config_trigger_delete requires 'name'"})
    path, err = _resident_triggers_path(role)
    if err is not None:
        return err

    import reminders
    try:
        # Off the loop, under trigger_write_lock.PASS_LOCK — see
        # config_trigger_upsert for why (#458 / #403).
        # #1372: the prompt file the entry named goes in the same locked step.
        outcome, prompt = await asyncio.to_thread(
            reminders.delete_entry_and_prompt, path, name)
    except (OSError, ValueError) as exc:
        return _result({"status": "error", "kind": "trigger_write_refused",
                        "message": str(exc)})
    if outcome == "not_found":
        return _result({"status": "error", "kind": "not_found",
                        "message": f"{role} has no trigger named {name!r}"})
    if outcome == "not_owned":
        return _result({
            "status": "error", "kind": "not_owned",
            "message": (f"{name!r} is a reminder {role} set for itself "
                        f"(managed_by: agent). Ask {role} to cancel it — this "
                        f"tool must not touch the resident's own entries."),
        })
    if prompt is None:
        file_note = ""
    elif prompt["outcome"] == "removed":
        file_note = f", and its prompt file {prompt['path']} was deleted"
    else:
        file_note = (f"; its prompt file {prompt['path']} was left in place "
                     f"({prompt['reason']})")
    out = {
        "status": "ok", "outcome": "removed", "role": role, "name": name,
        "message": (f"{name} removed from {role}/triggers.yaml{file_note} — "
                    "now commit it: config_git_commit, which reloads the "
                    "role's triggers"),
    }
    if prompt is not None:
        out["prompt_file"] = prompt
    return _result(out)


# ---------------------------------------------------------------------------
# casa_reload_triggers - back-compat shim for Plan 3 soft-reload (now via dispatch)
# ---------------------------------------------------------------------------


@tool(
    "casa_reload_triggers",
    "Re-register triggers for one agent in-process (no addon restart). "
    "config_git_commit already reloads a committed triggers.yaml or trigger "
    "prompt; use this to retry. Restricted to the configurator role.",
    {"role": str},
)
async def casa_reload_triggers(args: dict) -> dict:
    caller = _effective_caller_role()
    if caller not in _PRIVILEGED_CONFIG_ROLES:
        return _refuse_unprivileged("casa_reload_triggers", caller)

    role = _reload_role_arg(args.get("role"))
    if not role:
        return _result({
            "status": "error", "kind": "role_required",
            "message": "casa_reload_triggers requires 'role' — the agent's "
                       "directory name (a leading resident:/specialist: prefix "
                       "is accepted and stripped)",
        })

    import agent as agent_mod
    runtime = getattr(agent_mod, "active_runtime", None)
    if runtime is None:
        return _result({
            "status": "error", "kind": "not_initialized",
            "message": "CasaRuntime not bound - boot ordering bug",
        })

    # #824: an unfenced trigger-reconcile scope, so the health refresh's own
    # guard acquisition is the only one on this path.
    result = await _run_reload("triggers", runtime=runtime, role=role)
    result.setdefault("role", role)
    # Back-compat: emit registered=[trigger_names] from runtime.role_configs /
    # specialists.
    #
    # #609, two corrections. (1) This used to sit inside `if status == "ok"`,
    # so the surface that explains a failed reload was withheld by the very
    # failure it explains — and the same gate withheld the per-trigger secret
    # rows attached alongside it. Both read state that does not depend on the
    # reload having succeeded. (2) The old guard was `getattr(cfg, "triggers")`
    # TRUTHINESS, so a role with an empty trigger list omitted the key
    # entirely — indistinguishable from "the config could not be read" and from
    # "the specialist is disabled". An empty list says the thing it means.
    try:
        cfg = runtime.role_configs.get(role)
        if cfg is None:
            cfg = runtime.specialist_registry.all_configs().get(role)
        if cfg is not None:
            result["registered"] = [t.name for t in (getattr(cfg, "triggers", None) or [])]
    except Exception as exc:  # noqa: BLE001 — say so rather than dropping it
        result["registered_error"] = f"{type(exc).__name__}: {exc}"
    return _result(result)


# ---------------------------------------------------------------------------
# Plan 4b §7.1: marketplace_* Configurator MCP tools
# ---------------------------------------------------------------------------


# H16: serialize the mutating plugin/marketplace tools once their blocking
# bodies move off the loop via asyncio.to_thread. On the single event loop
# these handlers were previously mutually exclusive for free (they never
# awaited mid-body); the lock preserves that invariant for concurrent
# marketplace-file / manifest writes. Read-only vault helpers don't take it.
_PLUGIN_TOOLS_LOCK = asyncio.Lock()

# #489: the full-reload entry points hold _PLUGIN_TOOLS_LOCK across
# dispatch("full"), and reload_full's include_env arm reaches the plugin_env
# handler, whose health block serializes on the SAME lock (Sol r4-2) — with a
# plain non-reentrant Lock that nesting deadlocked the reload, the calling
# engagement, and (via the never-released reload writer lock) every later
# reload and plugin mutation. The guard makes the nesting legal for ONE
# logical operation, keyed on TASK IDENTITY: dispatch runs its handler
# inline, so the re-entering plugin_env health block is the same asyncio
# task that acquired at the entry point. Deliberately NOT a contextvar:
# `asyncio.create_task` COPIES the caller's context, so a contextvar
# "already held" flag would leak into tasks spawned inside the guarded
# region and let them skip the lock entirely (pinned by
# test_plugin_tools_guard_excludes_other_tasks). Cross-task semantics are
# the raw lock's, both directions (a guard queues behind a raw acquire and
# vice versa). Raw `async with _PLUGIN_TOOLS_LOCK:` stays correct for every
# path that cannot re-enter (the mutation sequences dispatch only
# agent/agents scopes, which never touch this lock).
_PLUGIN_TOOLS_LOCK_OWNER: "asyncio.Task | None" = None


@asynccontextmanager
async def _plugin_tools_guard():
    """Acquire `_PLUGIN_TOOLS_LOCK` unless the CURRENT TASK already holds it
    via this guard (see the #489 note above). Reads the module attributes at
    call time so tests that rebind the lock per event loop keep working."""
    global _PLUGIN_TOOLS_LOCK_OWNER
    if _PLUGIN_TOOLS_LOCK_OWNER is asyncio.current_task():
        yield
        return
    async with _PLUGIN_TOOLS_LOCK:
        _PLUGIN_TOOLS_LOCK_OWNER = asyncio.current_task()
        try:
            yield
        finally:
            _PLUGIN_TOOLS_LOCK_OWNER = None


# #706: the reload SCOPES whose handler acquires the guard, directly or through
# the `full` cascade. `reload.dispatch` takes the reload RW lock for EVERY scope
# and holds it across the handler, so a scope in this set that is dispatched
# WITHOUT the plugin lock already held runs `RW reader -> plugin lock` — the
# reverse of the global order stated above, and a two-party deadlock against any
# full-scope entry (which holds the plugin lock and wants the RW WRITER). The
# `plugin_env` handler is reachable that way: it is dispatched unguarded by the
# casa_reload tool and by POST /admin/reload, and the acquisition has no
# timeout.
#
# The set is the classification, in ONE place: the entry points below carry no
# scope logic of their own, so the two of them cannot drift apart.
_PLUGIN_TOOLS_RELOAD_SCOPES = frozenset({"full", "plugin_env"})


@asynccontextmanager
async def _plugin_tools_reload_guard(scope: str):
    """Hold `_PLUGIN_TOOLS_LOCK` across a reload dispatch of a scope that will
    reach it, and nothing at all for a scope that will not.

    ENTRY-POINT-ONLY still holds, and for its recorded reason: the guard is
    taken BEFORE `reload.dispatch` acquires the reload writer/reader lock, never
    underneath it. `agent`, `agents`, `policies`, `triggers` and `config_sync`
    stay unfenced, so a bundle transaction holding the raw lock can still
    complete its own `dispatch("agent")` (pinned by
    test_bundle_reload_fence.py::test_bundle_agent_dispatch_completes_while_holding_plugin_lock).
    """
    if scope in _PLUGIN_TOOLS_RELOAD_SCOPES:
        async with _plugin_tools_guard():
            yield
    else:
        yield


async def _settle_through_cancellation(coro):
    """Run *coro* as a task and wait for it even through cancellation; the
    original `CancelledError` is re-raised once it has settled.

    The same primitive as a thread hop that settles through cancellation,
    for the same reason (v0.183.0): a cancelled caller that
    releases a lock while its write is still in flight lets a concurrent
    operation write the correct state and then has its own stale write land
    last. The difference is WHAT it covers — a whole acquire-write-release unit
    rather than the thread hop inside it — because a caller cancelled while
    still QUEUED for the lock drops the write entirely, which is the same
    obligation lost by the other door. One settling wrapper around the unit, not
    one per step inside it.

    The task is a CHILD of the caller, so it must not be started inside a hold of
    a guard whose re-entrancy is by task identity — it would not be recognised as
    the owner and would deadlock against its own parent. Its callers start it
    with no such hold, deliberately; `_regenerate_plugin_health_guarded` checks
    ownership before choosing it.

    The wait is bounded by the current lock holder's own teardown: cancellation
    unwinds a holder's `async with` and releases the lock, so a shutdown that
    cancels everything is not held open by this.
    """
    task = asyncio.ensure_future(coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 — the cancellation wins
                break
        raise


async def _to_thread_settled(fn, /, *args, **kwargs):
    """`asyncio.to_thread` for work that runs while `_PLUGIN_TOOLS_LOCK` is held.

    A cancelled `asyncio.to_thread` await returns at once while the thread keeps
    running, so a plugin tool cancelled mid-hop (an operator cancel, a run's
    time limit, shutdown) would release the lock with its worker still writing:
    the next mutation could then save the registry and report success, and the
    old worker's save of the registry it read earlier would land last and drop
    that entry. Here the caller waits for the thread to finish and only then
    sees the cancellation, so the lock is released after the worker is done.

    Every thread hop under the lock goes through this, the read-only ones too,
    so the rule has no exceptions to keep track of. It starts no task that takes
    the lock, so it is safe inside a hold of either the raw lock or the guard.
    """
    return await _settle_through_cancellation(
        asyncio.to_thread(fn, *args, **kwargs))


async def _regenerate_plugin_health_held(extra_issues: list | None = None) -> None:
    """#1055: regenerate the plugin health report for a caller that ALREADY holds
    `_PLUGIN_TOOLS_LOCK` (raw or through the guard), letting the worker thread
    finish before a cancellation propagates.

    `asyncio.to_thread` cancels the await, never the thread: a cancelled holder
    would release the lock with its computation still running, a competing
    guarded regeneration would write the correct report, and this one's older
    report would land last (`plugin_health._REPORT_LOCK` orders only the write).
    Only the thread hop is settled here — it starts no task that takes the lock,
    so it cannot deadlock against the caller's own hold."""
    await _settle_through_cancellation(
        asyncio.to_thread(_regenerate_plugin_health, list(extra_issues or [])))


async def _regenerate_plugin_health_guarded(*, then=None) -> None:
    """#1055: the one settled, guarded plugin-health regeneration for a caller
    that does not hold `_PLUGIN_TOOLS_LOCK` — acquire, regenerate, optionally
    await *then* (a notify that must read the report under the same hold),
    release.

    The unit runs in a child task that a cancellation cannot stop before its
    write has landed, for the two measured doors
    `_regenerate_plugin_health_after_reload` documents: cancelled
    during the write, a stale report would land after a newer one; cancelled
    while queued for the guard, the regeneration would be dropped.

    When the CURRENT task already owns the guard (a fenced reload dispatch
    re-entering a handler), the unit cannot run in a child task — the guard's
    re-entrancy is by task identity, so the child would queue behind its own
    parent forever. There the hold is already in place and only the thread hop
    is settled, through `_regenerate_plugin_health_held`.

    Only the regeneration is settled, never *then*: once the report is written a
    cancellation reaches the notify as it always did, so a slow send cannot hold
    the lock through a shutdown (diff round 1, Terra S2)."""
    if _PLUGIN_TOOLS_LOCK_OWNER is asyncio.current_task():
        await _regenerate_plugin_health_held()
        if then is not None:
            await then()
        return

    written = asyncio.Event()

    async def _unit() -> None:
        async with _plugin_tools_guard():
            await _to_thread_settled(_regenerate_plugin_health, [])
            written.set()
            if then is not None:
                await then()

    task = asyncio.ensure_future(_unit())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        waiter = asyncio.ensure_future(written.wait())
        while not task.done() and not written.is_set():
            try:
                await asyncio.wait({task, waiter},
                                   return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                continue
        waiter.cancel()
        if not task.done():
            task.cancel()
        try:
            await asyncio.shield(task)
        except BaseException:  # noqa: BLE001 — the caller's cancellation wins
            pass
        raise


async def _regenerate_plugin_health_after_reload(scope: str, result: dict) -> None:
    """#824, ENTRY-POINT-ONLY: re-derive the plugin health report after a reload
    whose dispatch reached its paired trigger/callback/event reconcile.

    The trigger half's health rows read the LIVE overlay at regeneration time
    (`trigger_reconcile._unavailable_rows`), and a WRITING trigger pass publishes
    the unavailable marker for the duration of its secret writes (INV-TRIG-017).
    With nothing regenerating after such a pass, a regeneration that sampled the
    marker left a `trigger_routing_unavailable` row standing after the map went
    live, and a reload that HEALED a stuck marker left the row that had been true
    standing — in both cases until some unrelated regeneration, because the
    scheduled recovery pass regenerates only while a marker still stands.

    HERE and not in `reload.dispatch`: dispatch holds the reload RW lock across
    the reconcile, so acquiring the plugin lock there is the INV-CFG-011
    inversion, it self-deadlocks the mutation sequencers' `dispatch("agent")`
    (which run under the RAW lock, where the guard records no owner), and it
    fails the static arm of tests/test_reload_lock_order.py. By the time dispatch
    RETURNS the RW lock is released, so the guard below is taken with no reload
    lock held — the global order `_PLUGIN_TOOLS_LOCK -> reload RW ->
    _RECONCILE_LOCK` is preserved. For a fenced scope the entry point already
    owns the guard through `_plugin_tools_reload_guard`, so this is a same-task
    re-entrant no-op there. The classification lives here rather than at the call
    sites for the same reason `_PLUGIN_TOOLS_RELOAD_SCOPES` does: three entry
    points carrying their own copy of it is three copies that can drift.

    `status == "ok"` is the exact witness that the paired reconcile was REACHED:
    in dispatch that block sits in the same `try` as the handler call, so a
    handler that raised never got to it. A reconcile that itself failed
    non-fatally still returns "ok", and regenerating there is deliberate — that
    is the case where a marker may be standing and the report most needs to say
    so.

    The whole guarded unit — acquire, write, release — goes through
    `_settle_through_cancellation`, so a cancelled caller neither drops the
    obligation nor lets a stale write win. Both doors are real and were measured:
    cancelled DURING the write, `asyncio.to_thread` cancels the AWAIT and never
    the thread, so releasing the guard there lets a competing guarded operation
    write the correct report and then has this one's older report land last
    (`plugin_health._REPORT_LOCK` orders the WRITE, not the compute before it, so
    it cannot close this); cancelled while still QUEUED for the guard — behind, say,
    a plugin-tool critical section, which holds the raw lock and
    refreshes no health of its own — the refresh is skipped entirely and a row the
    reload just made false stands until something unrelated regenerates.

    That settling runs the unit in a CHILD task, which is why this helper is called
    from OUTSIDE `_plugin_tools_reload_guard` rather than inside it: the guard's
    re-entrancy is by task identity, so a child started under a fenced scope's hold
    would queue behind its own parent forever (measured — it hung the `full` arm).

    Never notifies — regeneration and `_notify_plugin_health_if_possible` are
    separate calls, and the consent-approve regenerations set the same precedent —
    and never turns a successful reload into an error: the reload's own outcome is
    the operator's answer, and a health-report write that fails is a warning.
    """
    import reload as reload_mod

    if scope not in reload_mod._TRIGGER_RECONCILE_SCOPES:
        return
    if result.get("status") != "ok":
        return
    try:
        await _regenerate_plugin_health_guarded()
    except Exception:  # noqa: BLE001 — a diagnostic never fails the reload
        logger.warning("post-reload plugin-health regeneration failed",
                       exc_info=True)
    else:
        result["actions"] = [
            *(result.get("actions") or []), "plugin_health_regenerated"]


# ---------------------------------------------------------------------------
# Unified plugin architecture (§3.9/§3.13): registry-mutating tools.
# ---------------------------------------------------------------------------

_PLUGIN_HEALTH_PATH = "/data/plugin-health.json"


# #676 (INV-TOOL-007): a removal that COMMITS says what survives it. No Casa
# code path touches the Claude CLI's per-plugin persistent data directory
# (CLAUDE_PLUGIN_DATA) — it lives on the config volume and outlives every
# lifecycle operation — so a
# stored authorization survives a remove and a same-name reinstall silently
# re-adopts it. Casa does not derive or delete that path (a wrong derivation
# is data loss, and deleting it still could not revoke the grant at the
# provider); it DISCLOSES instead, which is the operator-delegated choice on
# #676. The wording is operation-scoped on purpose: `may remain` because Casa
# cannot see whether the plugin ever stored anything, and
# `provider_revocation_performed: False` names an operation Casa did not
# perform rather than the grant's state, which INV-TOOL-005 forbids it to
# claim either way.
_PLUGIN_DATA_NOTE_COMMITTED = (
    "The plugin's CLI-managed persistent data directory (CLAUDE_PLUGIN_DATA — "
    "it may hold stored authorizations such as OAuth tokens) was not deleted, "
    "and a reinstall of the same plugin re-attaches to it. No provider "
    "revocation was performed; revoke at the provider if that access should "
    "end.")
_PLUGIN_DATA_NOTE_ATTEMPTED = (
    "The rollback did not complete and these registry entries are still "
    "removed. Their plugins' CLI-managed "
    "persistent data directories (CLAUDE_PLUGIN_DATA — they may hold stored "
    "authorizations such as OAuth tokens) were not deleted, and a reinstall "
    "re-attaches to them. No provider revocation was performed; revoke at the "
    "provider if that access should end.")
# Sol diff-review r1: the registry could not be read back, so whether these
# entries are still removed is UNKNOWN — and the envelope says so rather than
# picking a side. The two facts that do not depend on the answer are stated
# flatly; they are true either way.
_PLUGIN_DATA_NOTE_INDETERMINATE = (
    "The rollback did not complete and Casa could not read the registry back, "
    "so whether these entries are still removed is unknown. Either way Casa "
    "deleted no plugin data: their CLI-managed persistent data directories "
    "(CLAUDE_PLUGIN_DATA — they may hold stored authorizations such as OAuth "
    "tokens) were not deleted, and no provider revocation was performed.")


def _plugin_data_disclosure(note: str, plugins: "list[str] | None" = None) -> dict:
    """The three (or four) disclosure fields for a PERSISTING committed
    removal. Callers add them AFTER the registry commit, never before: a
    pre-commit refusal changed nothing, so disclosing survival there would be
    a claim about state the operator still has."""
    fields = {"plugin_data_may_remain": True,
              "provider_revocation_performed": False,
              "plugin_data_note": note}
    if plugins is not None:
        fields["plugin_data_plugins"] = list(plugins)
    return fields


def _swap_dropped_names(txn) -> "list[str]":
    """The owned-plugin names this transaction's registry swap DROPPED, or []
    when it never swapped. The single definition of a removal candidate: the
    success payloads disclose exactly these, and the failed-compensation arm
    measures exactly these.

    Sol diff-review a3-r2: `before_entries` is not that set on any op but an
    uninstall. An upgrade with pre-swap {kept, dropped} and post-swap {kept,
    new} whose compensation then fails on an UNREADABLE registry disclosed
    both names, telling the operator a plugin the upgrade had just
    re-published may be gone — and inviting a provider revocation for an
    unrelated grant. The readable arm hid it because the read-back filtered
    the surplus out; the indeterminate arm has nothing to filter with, which
    is precisely why the candidate set has to be right before it is measured.
    """
    if not getattr(txn, "owned_swap_committed", False):
        return []
    return [n for n in (getattr(txn, "removed_owned_names", None) or ())
            if isinstance(n, str) and n]


def _swap_removal_disclosure(txn) -> dict:
    """#676 (INV-TOOL-007): the disclosure a SUCCESSFUL bundle owed for the
    owned plugins its registry swap dropped, or `{}` when it dropped none.

    A successful swap is the strongest evidence there is that the removal
    persisted — it is atomic, it saved, the sequencer then succeeded and the
    journal completed, and the whole window is inside `_PLUGIN_TOOLS_LOCK`, so
    unlike the failed-compensation arm there is nothing a read-back could
    correct. `removed_owned_names` is the swap's own answer, recorded at that
    moment; `before_entries` is NOT a substitute, because for an upgrade or a
    rollback it is the pre-swap set and most of it was re-published.

    Terra handback review (attempt 2): an upgrade or a rollback whose new owned
    generation omits an old plugin removes that plugin's registry entry exactly
    as an uninstall's cascade does, and returned ok with nothing said — the
    same persisting removal, reached by a door this disclosure did not cover.
    """
    names = _swap_dropped_names(txn)
    if not names:
        return {}
    return _plugin_data_disclosure(_PLUGIN_DATA_NOTE_COMMITTED, names)


def _absent_owned_names(txn, names: "list[str]") -> "tuple[list[str], bool]":
    """Which of `names` are absent from the registry NOW, and whether the
    registry could be read at all. Sync — callers on the loop use to_thread.

    `(_, False)` means unreadable or invalid: it is not evidence of absence and
    must never be reported as any. Never raises; an unexpected read failure is
    the same answer as an invalid document."""
    try:
        data = plugin_registry.load_registry(
            getattr(txn, "registry_path", None) or plugin_registry.REGISTRY_PATH)
        if not data.valid:
            return [], False
        present = {e.get("name") for e in (data.raw.get("plugins") or [])
                   if isinstance(e, dict)}
    except Exception:  # noqa: BLE001 — an unreadable registry is "unknown"
        logger.warning("could not read the registry back after a failed bundle "
                       "compensation; plugin-data disclosure is indeterminate",
                       exc_info=True)
        return [], False
    return [n for n in names if n not in present], True


# #554: the variable NAMES are the one actionable fact in an env-shaped health
# row, and nothing else carries them to the operator. The detail was attached to
# `env_unresolved` only, so a `setup_env_unprovisioned` row — the commonest
# shape during a plugin setup flow — reached the report and the notice naming no
# variable at all. Each reason draws on the secret status that PRODUCED it
# (_tool_verify_plugin_state re-grades a setupProvides name from "unresolved" to
# "unprovisioned"), so the reason and the names it shows always agree.
_ENV_DETAIL_STATUSES = {
    "env_unresolved": ("unresolved",),
    "setup_env_unprovisioned": ("unprovisioned",),
}


def _secret_names_detail(verify: dict, statuses: "tuple[str, ...]") -> "str | None":
    """#533/#554: the BOUNDED variable-name list for a health issue's detail —
    first five names (the withhold WARNING's bounding idiom), ``+N more``
    beyond that, never a value. ``None`` when no row carries one of
    *statuses*."""
    names = sorted({s.get("var") for s in verify.get("secrets") or []
                    if s.get("status") in statuses and s.get("var")})
    if not names:
        return None
    shown = names[:5]
    extra = len(names) - len(shown)
    return ", ".join(shown) + (f" +{extra} more" if extra > 0 else "")


def _env_detail_for(verify: dict, reason: str) -> "str | None":
    statuses = _ENV_DETAIL_STATUSES.get(reason)
    return None if statuses is None else _secret_names_detail(verify, statuses)


def _row_health_reason(row: dict, verify: "dict | None" = None) -> str:
    """The health reason for a not-ready verify target row. #1051: a
    `reload_required` row whose agent has never bound the plugin at all
    (`active_artifact_id` None) is a plugin NOT LOADED YET — a first install
    awaiting its reload — not an agent "still running its previous version";
    verify's own grading is unchanged, only the operator-facing code differs.

    #1053: the row carries ONE code, and the operator DM defers install-phase
    codes while the plugin's install is running — so an install-phase code must
    never stand in front of a real fault. When the first code is install-phase,
    the row takes the first code that is not, from its own remaining reasons and
    then *verify*'s configured reasons (which a `reload_required` grading
    replaces on the row, so they are otherwise unseen here)."""
    import plugin_health
    reasons = list(row.get("reasons") or ["not_ready"])
    reason = reasons[0]
    if (reason == "reload_required" and row.get("state") == "active"
            and row.get("active_artifact_id") is None):
        reason = "not_loaded"
    if plugin_health.is_install_phase(reason):
        return _install_phase_fault(reasons[1:], verify) or reason
    return reason


def _install_phase_fault(codes: list, verify: "dict | None") -> "str | None":
    """#1053: the fault an install-phase code would otherwise stand in front
    of, or None when the plugin's non-readiness is install-phase through and
    through. Covers every input of verify's configured readiness — its reason
    codes, its system requirements (reported as `tools`, with no code of their
    own) and its secrets — so a failure verify grades without a code is not
    deferred either."""
    import plugin_health
    verify = verify or {}
    for code in list(codes) + list(verify.get("reasons") or []):
        if not plugin_health.is_install_phase(code):
            return code
    if any(t.get("status") != "ready" for t in verify.get("tools") or []):
        return "system_requirement_missing"
    if any(s.get("status") not in ("resolved", "unprovisioned")
           for s in verify.get("secrets") or []):
        return "env_unresolved"
    return None


def _regenerate_plugin_health(extra_issues: list) -> None:
    """§3.10/R2-4 + Sol #13 + D2/B3 (v0.74.0): rewrite the health report from
    the CURRENT resolver state PLUS the RUNTIME verify state of EVERY
    registered plugin — not just the plugin this mutation touched. Otherwise
    a successful mutation of plugin B would rewrite health from resolver
    issues + B's (empty) extras and ERASE plugin A's still-active runtime
    failure. D2: readiness derives from THIS regeneration's OWN fresh pass —
    a caller-supplied stage="verify" extra row is DROPPED when the fresh
    pass can REDISCOVER it (its plugin is registered AND its target is None
    or still among that entry's targets): a still-true failure re-lands
    fresh, a transient one (e.g. a pre-v0.74.0 torn-read reload_required)
    must not linger. Carried forward verbatim: stage="reload" rows (a failed
    reconstruction is not rediscoverable by verify), rows for unregistered
    plugins, and rows whose target was since UNASSIGNED (the fresh pass no
    longer grades that target — B3)."""
    import plugin_health
    from plugin_registry import PluginIssue, load_registry
    res = plugin_registry.resolve_all()
    reg = load_registry()
    entry_targets: dict = {}
    entry_artifacts: dict = {}
    if reg.valid:
        for e in reg.entries:
            entry_targets[e.get("name")] = list(e.get("targets") or [])
            entry_artifacts[e.get("name")] = e.get("artifact_id")

    def _rediscoverable(issue) -> bool:
        if getattr(issue, "stage", None) != "verify":
            return False
        nm = getattr(issue, "name", None)
        if nm not in entry_targets:
            return False
        tgt = getattr(issue, "target", None)
        return tgt is None or tgt in entry_targets[nm]

    extra_issues = [e for e in extra_issues if not _rediscoverable(e)]
    seen = {(getattr(e, "name", None), getattr(e, "target", None),
             getattr(e, "reason_code", None)) for e in extra_issues}
    runtime_issues: list = []
    def _add(name, target, reason, detail=None):
        key = (name, target, reason)
        if key not in seen:
            seen.add(key)
            runtime_issues.append(PluginIssue(
                name=name, target=target, stage="verify", reason_code=reason,
                detail=detail))

    if reg.valid:
        for entry in reg.entries:
            name = entry.get("name")
            try:
                verify = _tool_verify_plugin_state(plugin_name=name)
            except Exception:  # noqa: BLE001
                # Sol round-3 H13: a verifier crash is ITSELF a health problem —
                # surface it, never silently drop the plugin from the report.
                _add(name, None, "verify_exception")
                continue
            # #533/#554: the env-shaped rows carry the var NAMES so the
            # report and DM can honor the 0.153.0 "names the missing
            # values" promise.
            rows = verify.get("targets") or []
            for row in rows:
                if not row.get("ready"):
                    reason = _row_health_reason(row, verify)
                    _add(name, row.get("target"), reason,
                         detail=_env_detail_for(verify, reason))
            # Sol round-3 H13: a top-level not-ready with NO target rows (e.g. an
            # unassigned plugin with a missing secret / mcp_invalid) would else be
            # erased — surface it against the plugin itself.
            if verify.get("ready") is not True and not rows:
                reasons = list(verify.get("reasons") or ["not_ready"])
                # #1053: a failure verify grades without a code (a missing
                # program) gets a row of its own when only install-phase
                # codes would otherwise speak for the plugin.
                fault = _install_phase_fault(reasons, verify)
                if fault is not None and fault not in reasons:
                    reasons.append(fault)
                for reason in reasons:
                    _add(name, None, reason,
                         detail=_env_detail_for(verify, reason))
    # S5 §14.8: an agent out of service (a stored call's processes
    # unconfirmed ended) is a RECOMPUTABLE runtime row — read fresh from the
    # agents' faults on every regeneration, standing until a restart clears
    # it, never passed as a one-shot extra.
    import agent_lane
    for issue in agent_lane.FAULTS.issues():
        _add(issue.name, issue.target, issue.reason_code, detail=issue.detail)
    # #211: a registered plugin targeting a NOT-yet-installed specialist is
    # the documented plugin-before-specialist install order — WARNING-class
    # ("target_pending"), never a blocking issue. RECOMPUTABLE like the
    # trigger issues below (derived fresh on every regeneration, never a
    # one-shot extra), so it self-clears the moment the specialist installs.
    import agent as agent_mod
    runtime = getattr(agent_mod, "active_runtime", None)
    pending_warnings: list = []
    if reg.valid:
        for entry in reg.entries:
            for target in entry.get("targets") or []:
                tier, _, role = target.partition(":")
                if (tier == "specialist"
                        and _specialist_target_pending(runtime, role)):
                    pending_warnings.append(PluginIssue(
                        name=entry.get("name"), target=target,
                        stage="reload", reason_code="target_pending"))
        # S8: a plugin's recommendation its target does not hold is a
        # WARNING, recomputed on every regeneration (an assignment clears it).
        try:
            import plugin_requirements
            pending_warnings.extend(plugin_requirements.unmet_requirement_issues(
                reg.raw if isinstance(reg.raw, dict) else {},
                plugin_registry.snapshot_store_root()))
        except Exception:  # noqa: BLE001 — health must never crash on a recommendation
            logger.debug("requirement warnings skipped", exc_info=True)
    # Release B: plugin-trigger issues are a RECOMPUTABLE input — derived
    # fresh on EVERY regeneration (never passed as one-shot extras), so an
    # unrelated health refresh can never erase trigger_pending_ack /
    # trigger_channel_missing. current_issues() never raises.
    import trigger_reconcile
    trigger_issues = trigger_reconcile.current_issues()
    # Callback issues (callback_pending_ack / callback_no_target /
    # callback_invalid / callback_base_url_invalid / callback_spool_error) are
    # a RECOMPUTABLE input just like the trigger issues — derived fresh on
    # every regeneration so an unrelated refresh can never erase them.
    # current_issues() never raises.
    import callback_reconcile
    callback_issues = callback_reconcile.current_issues()
    # #419: event-subscription issues (event_pending_ack / event_no_target /
    # event_invalid / event_emitter_missing / event_routing_unavailable /
    # event_spool_issue) are a RECOMPUTABLE input too, but — unlike the
    # trigger/callback rows — plain DICTS, not PluginIssue instances (see
    # event_reconcile.current_issues()'s own docstring for the contract).
    # Concatenated DIRECTLY into write_report's issues= below, never through
    # the PluginIssue-attribute-only _add()/_rediscoverable() helpers above,
    # which would silently degrade a dict row's fields to None instead of
    # raising. current_issues() never raises.
    import event_reconcile
    event_issues = event_reconcile.current_issues()
    # v0.112.0: setup-episode state (pending/failed/stale) is likewise a
    # RECOMPUTABLE input, derived fresh from the durable episode store — a
    # dropped or failed post-consent setup dispatch stays visible until it
    # resolves. Never raises into a health pass.
    setup_issues: list = []
    try:
        import plugin_setup_episodes
        for row in plugin_setup_episodes.health_issues():
            # #653: a removed plugin's failed episode is durable state, and
            # merging it unfiltered left it standing as a LIVE health issue —
            # and a notification — for a plugin no longer installed, until the
            # 72h decay. Filter to registered names.
            #
            # The filter is gated on the SAME `reg.valid` that already gated
            # `entry_targets` above, and reuses that very map. That is the
            # fail-open guarantee, structurally rather than as a second
            # condition that could drift: when the registry read is torn or
            # invalid, entry_targets is empty AND this branch does not run, so
            # a bad read can never erase every setup row. The membership test
            # is only ever applied to a set a valid registry produced. Nothing
            # else here is filtered — not the resolver issues, the extras, the
            # runtime rows, the trigger/callback/event rows, nor the episode
            # HISTORY the status tool reads separately.
            if row.get("plugin") == "*":
                # #747 / INV-PLUG-015: the registry-GLOBAL "history could not
                # be read" row. It names no plugin, so the registered-name and
                # artifact filters below would drop it; it bypasses both, like
                # the reconcilers' own `name="*"` routing rows.
                setup_issues.append(PluginIssue(
                    name="*", target=None, stage="setup",
                    reason_code=row.get("kind") or "setup_history_unavailable",
                    artifact_id=None, detail=row.get("detail") or None))
                continue
            if reg.valid:
                name = row.get("plugin") or ""
                if name not in entry_targets:
                    continue
                # #653 r1: and it must be THIS artifact's obligation. A
                # terminal `failed` row survives removal (stamped, #928) and a
                # new-artifact reinstall until that artifact's sweep
                # supersedes it, so without this a plugin removed after a failed setup and
                # reinstalled at a NEW artifact within the decay window had the
                # OLD artifact's failure re-enter health — and, because removal
                # pruned its notification mark, announced afresh as though the
                # new install had failed. Fail open on either side being
                # unattributable: a row or an entry with no artifact_id cannot
                # be judged stale, so it stands.
                want = entry_artifacts.get(name)
                got = row.get("artifact_id")
                if want and got and want != got:
                    continue
            # #554: health_issues() emits the episode's last_error as `detail`
            # and this call site dropped it, so a failed setup reached the
            # operator as a bare reason code while the explanation sat unread.
            # `detail` is outside the fingerprint (#533), so carrying it here
            # changes no issue identity and no notification dedup.
            setup_issues.append(PluginIssue(
                name=row.get("plugin") or "", target=None, stage="setup",
                reason_code=row.get("kind") or "setup_episode",
                artifact_id=None, detail=row.get("detail") or None))
    except Exception:  # noqa: BLE001
        logger.debug("setup-episode health merge skipped", exc_info=True)
    plugin_health.write_report(
        issues=(list(res.issues) + list(extra_issues) + runtime_issues
                + list(trigger_issues) + list(callback_issues) + setup_issues
                + list(event_issues)),
        warnings=list(res.warnings) + pending_warnings,
        path=_PLUGIN_HEALTH_PATH,
    )


async def _notify_plugin_health_if_possible() -> None:
    # #342: notify_plugin_health now takes the channel manager — delivery
    # is a direct channel send gated on a REAL configured channel, not the
    # always-registered telegram bus queue (whose outbound handler drops
    # silently when no channel exists).
    if _channel_manager is None:
        return
    try:
        import casa_core
        await casa_core.notify_plugin_health(
            _channel_manager, path=_PLUGIN_HEALTH_PATH)
    except Exception:  # noqa: BLE001 — never fail a mutation on notify
        logger.debug("plugin health notify skipped", exc_info=True)


# #1053: the plugins a configurator run is installing are held back from the
# plugin-health DM while its lease is held; the
# lease's release schedules the deferred notify pass. In-process only: after a
# restart nothing is deferred, so a still-running install's rows are announced
# (a spare DM, never a lost one).


def _record_plugin_installer(names) -> None:
    """Record each of *names* as installed by the configurator run holding
    the lease, if one does."""
    import configurator_run
    configurator_run.record_installing(names)


def plugins_under_live_install() -> set:
    """Names a configurator run holding the lease is installing."""
    import configurator_run
    return configurator_run.installing()


def _on_engagement_terminal(rec) -> None:
    """EngagementRegistry terminal observer.

    #1049: an ended engagement retires its own unanswered authorization
    challenges — a later tap could only mint a grant bound to an engagement
    nothing will ever resume. Synchronously, in this same step: a deferred
    retirement left a window after the transition in which a tap still
    committed. ``cancel_matching`` never awaits; the keyboard edits it causes
    run later on the broker's own hook tasks.

    An ended job's keyboards in the direct chat (its start line, its
    questions) are retired on every terminal winner too: the channel takes
    them off the record in this step and edits them away on a task of its
    own — except for a job that completed from inside a running turn, whose
    finalize tail retires them once that turn has ended and its cost is
    recorded."""
    CHALLENGES.cancel_matching(engagement=rec.id, reason="engagement_ended")
    if rec.id in _end_paint_deferred:
        return                  # the finalize tail paints it after the turn ends
    channel = _transport()
    retire = getattr(channel, "retire_job_keyboards", None)
    if callable(retire):
        try:
            retire(rec)
        except Exception:  # noqa: BLE001 — never fail a terminal transition
            logger.warning("retiring job keyboards failed for %s", rec.id[:8],
                           exc_info=True)


def _specialist_target_pending(runtime, role: str) -> bool:
    """True when ``specialist:<role>`` has no agent directory yet (#211).

    Plugin-before-specialist is the DOCUMENTED install order — the
    specialist's dependency closure hashes the already-installed plugin
    artifact — so a plugin mutation may legitimately target a specialist
    that is not installed yet. That constraint is invisible from here (the
    registry only sees the target string), hence this explicit predicate.
    A ``specialist:`` target is installed ONLY at agents/specialists/<role>
    — a bare agents/<role> hit is a RESIDENT sharing the name, and letting
    it through would dispatch a cross-tier resident reload for a specialist
    target (Sol/Terra round-1 P1). A runtime without an ``agents_dir``
    (narrow test stand-ins) never classifies pending — dispatch keeps its
    established behavior."""
    agents_dir = getattr(runtime, "agents_dir", None)
    if not agents_dir:
        return False
    return not os.path.isdir(os.path.join(agents_dir, "specialists", role))


def _stale_absent_targets(targets: list, name: str, runtime) -> list[str]:
    """The concrete resident/specialist targets that STILL bind `name` after
    an absent-expectation mutation (unassign/remove) — read from the live
    agents' coherent binding property (D3, v0.74.0: concrete targets, never
    a registry-wide null)."""
    if runtime is None:
        return []
    agents = getattr(runtime, "agents", {}) or {}
    stale: list[str] = []
    for target in targets:
        tier, _, role = target.partition(":")
        if tier in ("resident", "specialist"):
            agent = agents.get(role)
            if agent is not None and name in getattr(
                    agent, "active_plugin_binding", {}):
                stale.append(target)
    return stale


def _postcondition_holds(verify: dict, targets: list, *, expect: str,
                         name: str | None = None, runtime=None) -> bool:
    """§3.9 mutation postcondition. 'present' (add/update/assign): every in-casa
    target row must be ready. 'absent' (unassign/remove): no RECONSTRUCTED
    in-casa agent still binds `name` — delegates to _stale_absent_targets so
    failures carry concrete targets (D3, v0.74.0)."""
    if expect == "present":
        # Sol #9: a plugin with zero target rows (e.g. fully unassigned, or an
        # update whose new artifact has unresolved secrets) made all([]) == True
        # → false-green. Require the TOP-LEVEL readiness, which folds in the
        # artifact/tools/secrets checks AND every target row's readiness — so an
        # empty-rows verify no longer passes vacuously.
        return verify.get("ready") is True
    if runtime is None or name is None:
        return True
    return not _stale_absent_targets(targets, name, runtime)


def _issues_from_mutation(name: str, *, reload_errors: list, verify: dict,
                          expect: str, postcondition_ok: bool,
                          stale_absent_targets: "tuple | list" = (),
                          snapshot_raced: bool = False) -> list:
    """R2-4 + D3 (v0.74.0): translate reload/verify/postcondition failures
    into structured PluginIssues. postcondition_failed(target=None) is
    emitted ONLY when nothing concrete (reload error / not-ready target row /
    top-level verify reason / snapshot race) already explains the failure —
    pre-v0.74.0 it duplicated reload_required registry-wide, warning EVERY
    resident (the operator's original symptom). Absent-case failures name
    their concrete stale targets; a snapshot race is its own reason."""
    from plugin_registry import PluginIssue
    issues: list = []
    for err in reload_errors:
        issues.append(PluginIssue(
            name=name, target=err.get("target"), stage="reload",
            reason_code="reload_failed"))
    row_failures = [row for row in (verify.get("targets") or [])
                    if not row.get("ready")]
    for row in row_failures:
        issues.append(PluginIssue(
            name=name, target=row.get("target"), stage="verify",
            reason_code=_row_health_reason(row, verify)))
    if snapshot_raced:
        issues.append(PluginIssue(
            name=name, target=None, stage="verify",
            reason_code="snapshot_raced"))
        return issues
    if postcondition_ok or reload_errors:
        return issues
    if expect == "absent" and stale_absent_targets:
        for t in stale_absent_targets:
            issues.append(PluginIssue(
                name=name, target=t, stage="verify",
                reason_code="postcondition_failed"))
    elif not row_failures and not (verify.get("reasons") or []):
        issues.append(PluginIssue(
            name=name, target=None, stage="verify",
            reason_code="postcondition_failed"))
    return issues


async def _reload_and_verify_targets(name: str, targets: list,
                                     *, expect: str) -> dict:
    """§3.9 mutation sequencing — THE ordering that kills the incident. The
    atomic registry write already happened; now: reload the resolver snapshot
    FIRST, reconstruct affected in-casa agents, desired==active verify, then
    regenerate + notify health. Order is load-bearing (stale-snapshot hazard)."""
    _record_plugin_installer([name])   # #1053
    await _to_thread_settled(plugin_registry.reload_snapshot)   # 1. FIRST
    import agent as agent_mod
    import reload as reload_mod
    runtime = getattr(agent_mod, "active_runtime", None)
    reloaded: list = []
    reload_errors: list = []
    pending_targets: list = []
    for target in targets:
        tier, _, role = target.partition(":")
        if tier in ("resident", "specialist") and runtime is not None:
            if (expect == "present" and tier == "specialist"
                    and _specialist_target_pending(runtime, role)):
                # #211: a not-yet-installed specialist target is the
                # documented plugin-first install order, not a reload
                # failure — dispatching would only raise unknown_role. The
                # specialist install itself constructs the role (and its
                # dependency closure resolves this plugin then). Residents/
                # executors and genuinely unknown roles keep the hard
                # failure; expect="absent" keeps its dispatch (removal
                # semantics unchanged).
                pending_targets.append(target)
                continue
            res = await reload_mod.dispatch("agent", runtime=runtime, role=role)
            if res.get("status") == "ok":
                reloaded.append(target)
            else:
                reload_errors.append({"target": target, **res})
        # executors: nothing to reconstruct (per-launch resolution).
    # Sol round-3 B2a + D2 (v0.74.0): reconstruction leaves the new Agent's
    # binding snapshot lazy (None until its first turn) — verify would then
    # classify a live, registered agent as "dormant" and green it BEFORE its
    # binding is captured. Force resolution, then enforce the D2 generation
    # fence: every reloaded target's snapshot must carry the CURRENT
    # post-reload generation, and the global generation must not move across
    # the verify. A mismatch means an intervening reload — retry ONCE with a
    # real re-dispatch (a cached snapshot would short-circuit a bare
    # re-resolve), then fail EXPLICITLY (kind=snapshot_raced), never grading
    # against a stale generation.
    agents = getattr(runtime, "agents", {}) or {}
    verify: dict = {}
    snapshot_raced = False
    resolve_failed: set = set()
    for _verify_attempt in range(2):
        gen_now = plugin_registry.snapshot_generation()
        for target in reloaded:
            _, _, role = target.partition(":")
            agent = agents.get(role)
            if agent is not None and getattr(
                    agent, "plugin_binding_snapshot", None) is None:
                try:
                    await agent._get_plugin_resolution()
                except Exception as exc:  # noqa: BLE001
                    # Sol round-4 M: a fail-OPEN resolve would leave the
                    # snapshot None → verify classifies the live agent as
                    # dormant → green. Record a reload error instead.
                    logger.warning(
                        "post-reconstruct resolve failed for %s: %s",
                        role, exc)
                    if target not in resolve_failed:
                        resolve_failed.add(target)
                        reload_errors.append(
                            {"target": target, "status": "error",
                             "kind": "resolve_failed"})
        stale_gen = []
        for t in reloaded:
            _, _, _role = t.partition(":")
            _snap = getattr(agents.get(_role), "plugin_binding_snapshot", None)
            if _snap is not None and _snap.generation != gen_now:
                stale_gen.append(t)
        verify = await _to_thread_settled(
            _tool_verify_plugin_state, plugin_name=name)
        raced = (bool(stale_gen)
                 or plugin_registry.snapshot_generation() != gen_now)
        if not raced:
            snapshot_raced = False
            break
        snapshot_raced = True
        if _verify_attempt == 0:
            for t in (stale_gen or reloaded):
                _, _, role = t.partition(":")
                try:
                    await reload_mod.dispatch("agent", runtime=runtime,
                                              role=role)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("generation-race re-reload failed for "
                                   "%s: %s", role, exc)
    stale_absent = (_stale_absent_targets(targets, name, runtime)
                    if expect == "absent" else [])
    # Release B: reconcile plugin-declared webhook triggers LAST — after the
    # snapshot reload, agent reconstruction, and verify — so the overlay
    # derives from the POST-mutation resolver state (every one of the 5
    # lifecycle mutations funnels through this sequencer). Its issues land in
    # the health regen below via trigger_reconcile.current_issues() (a
    # recomputable input, never a one-shot extra). A reconcile failure keeps
    # the old overlay (fail-safe for ingress) and never fails the mutation.
    try:
        import trigger_reconcile
        await trigger_reconcile.reconcile_from_runtime(runtime)
    except Exception:  # noqa: BLE001
        logger.warning("plugin-trigger reconcile failed", exc_info=True)
    # Pair the CALLBACK reconcile at the SAME site with the
    # SAME runtime — every one of the 5 lifecycle mutations funnels through
    # here, so this is where an update-changed-declaration re-derives to
    # `callback_pending_ack` (stale-digest identity → no ack), and a removal's
    # callbacks are swept by absence. A same-declaration update leaves the
    # overlay identical (no dark window). Non-fatal, like the trigger half.
    try:
        import callback_reconcile
        await callback_reconcile.reconcile_from_runtime(runtime)
    except Exception:  # noqa: BLE001
        logger.warning("plugin-callback reconcile failed", exc_info=True)
    # Pair the EVENT reconcile at the SAME site with the SAME runtime —
    # every one of the 5 lifecycle mutations funnels through here, so this
    # is where an update-changed-subscribe-declaration re-derives to
    # `event_pending_ack` (stale-digest identity → no ack), a removed
    # emitter's subscribers fall to `event_emitter_missing`, and a removed
    # subscriber's own routes are swept by absence. Unlike trigger/callback,
    # ``reconcile_plugin_events`` itself takes the runtime directly (no
    # separate ``reconcile_from_runtime`` wrapper — it already pulls
    # role_configs/channel_manager off ``runtime`` when not given). Non-fatal,
    # like its siblings.
    try:
        import event_reconcile
        await event_reconcile.reconcile_plugin_events(runtime)
    except Exception:  # noqa: BLE001
        logger.warning("plugin-event reconcile failed", exc_info=True)
    ok = (not snapshot_raced and not reload_errors
          and _postcondition_holds(verify, targets, expect=expect,
                                   name=name, runtime=runtime))
    # #1436: Casa commits the registry write itself, scoped to that file — the
    # configurator skipped the persist commit whenever it had made no edit of
    # its own, leaving /config dirty. The change is already live (or this
    # result says why not), so the commit reloads nothing: a configurator
    # commit that picks the registry up instead reloads nothing for it either
    # (INV-TOOL-014).
    import config_git
    await _to_thread_settled(
        config_git.commit_casa_write, str(plugin_registry.REGISTRY_PATH),
        f"casa: plugin {name} registry change")
    mutation_issues = _issues_from_mutation(
        name, reload_errors=reload_errors, verify=verify,
        expect=expect, postcondition_ok=ok,
        stale_absent_targets=stale_absent, snapshot_raced=snapshot_raced)
    await _regenerate_plugin_health_held(mutation_issues)
    await _notify_plugin_health_if_possible()
    result = {
        # Spec §E pinned payload: activation_committed = the registry pin
        # landed (this sequencer only runs post-commit); runtime_ready =
        # reload + verify agree desired==active AND no target is pending;
        # ok = the mutation succeeded (a pending specialist target — #211's
        # plugin-before-specialist install order — is NOT an error, but the
        # plugin only becomes runtime-ready on that target once the
        # specialist installs); kind is present on EVERY payload (None on
        # success). pending_targets = specialist targets whose role is not
        # installed yet (reload deferred to the specialist install).
        # is_error is NOT a payload field — _result derives the outer MCP
        # flag from ok:false.
        "ok": ok,
        "kind": None,
        "activation_committed": True,
        "runtime_ready": ok and not pending_targets,
        "reloaded": reloaded, "reload_errors": reload_errors,
        "pending_targets": pending_targets,
        "verify": verify,
    }
    if not ok:
        result["kind"] = ("snapshot_raced" if snapshot_raced
                          else "reload_failed" if reload_errors
                          else "postcondition_failed")
    return result


_BUNDLE_NONBLOCKING_REASONS = frozenset({
    "not_ready", "setup_env_unprovisioned",
    # #533: the named form of what previously surfaced as the generic
    # "not_ready" row fallback — an unresolved secret is config-pending
    # readiness, a verified-legal terminal state, never a rollback cause.
    "env_unresolved",
})


def _bundle_binding_blocked(v: dict) -> bool:
    """P1-3: True iff a `_tool_verify_plugin_state` result shows a genuine
    integrity/binding failure that must roll a bundle commit back — as opposed
    to mere config-pending readiness (unresolved secrets / missing sysreq
    bins), which is a verified-legal terminal state (spec §3.2) and must NOT
    trigger compensation.

    verify's top-level `reasons` list carries the integrity/binding codes
    (registry_invalid / not_registered / entry-issue reason codes /
    artifact_missing / artifact_invalid / corrupt_artifact / mcp_invalid /
    mcp_command_missing / mcp_reserved_env) — never secrets/tools readiness,
    which surface separately as `env_unresolved` (#533; previously the
    generic 'not_ready' row fallback) — PLUS
    one readiness code, `setup_env_unprovisioned` (#488): on a fresh install
    of a `casa.setupProvides` plugin, unprovisioned is the documented normal
    state (#429 — the plugin must load unprovisioned for its own setup tool
    to ever run), so it must not block the bundle at either level. The rows
    copy the top-level reasons, so both levels share one exemption set;
    everything outside it blocks."""
    if any(code not in _BUNDLE_NONBLOCKING_REASONS
           for code in (v.get("reasons") or [])):
        return True
    for row in v.get("targets") or []:
        if any(code not in _BUNDLE_NONBLOCKING_REASONS
               for code in (row.get("reasons") or [])):
            return True
    return False


# #828 (INV-SPEC-013): how long a CANCELLED bundle handler keeps waiting for its
# own transaction to reach a terminal journal state before it stops waiting. The
# bound limits the WAIT only — never the transaction, which is not cancelled by
# it, keeps the mutation lock, and still completes or compensates its journal.
#
# Unbounded absorption is not the safe alternative: it is #321's defect one file
# over (`_await_voice_persistence` below), where a wedged operation made
# cancellation permanently ineffective. This unit ends in `reload.dispatch(...)`
# and `_notify_plugin_health_if_possible()`, neither of which carries a timeout
# and the second of which can reach the Telegram transport, so a shutdown-time
# cancel must not be held hostage by either. 60s is two-sided: generous against
# a sequencer measured in seconds (snapshot reload + agent reload + per-entry
# verify + health regeneration), short enough that a shutdown is not held for
# minutes.
_SPECIALIST_BUNDLE_CANCEL_GRACE_S: float = 60.0

# Strong references to bundle transactions still running after their handler
# finished cancelled. `asyncio.create_task` alone does NOT keep a task alive —
# a garbage-collected transaction is exactly the stranded journal this fixes.
_BUNDLE_MUTATION_TASKS: "set[asyncio.Task]" = set()


def _bundle_transaction_done(task: "asyncio.Task") -> None:
    """Done-callback for a bundle transaction: drop the strong reference and
    RETRIEVE any exception, so asyncio never logs "exception was never
    retrieved" for a transaction whose caller had already gone (content
    deliberately not rendered)."""
    _BUNDLE_MUTATION_TASKS.discard(task)
    if task.cancelled():
        return
    if task.exception() is not None:
        logger.error("specialist bundle transaction failed after its handler "
                     "had finished")


async def _run_bundle_transaction(operation: "Callable[[], Awaitable[Any]]") -> Any:
    """#828: run ONE specialist-bundle transaction so that cancelling its
    handler cannot strand the journal in progress.

    The journal's `in-progress` state means "undo me at boot". The library
    writes it BEFORE the visible swap and returns with it still standing, and
    completion is deliberately the tool layer's — deferred past a sequencer that
    may have to compensate a committed generation. `CancelledError` is a
    `BaseException`, so the handlers' `except Exception` arm caught neither
    door: a cancelled handler completed nothing, compensated nothing, and left a
    committed generation beside a live undo record that `reconcile_boot` then
    replayed over any generation committed after it.

    `operation` is the handler's WHOLE in-lock body: library call, post-commit
    sequencer, journal completion or compensation, receipt prune and staging
    reclaim. It runs in a CHILD task which acquires `_PLUGIN_TOOLS_LOCK` itself.

    Three properties, and each is load-bearing:

    * **The child owns the mutation lock.** The task that took it is the task
      that releases it, so when the wait below is abandoned the transaction
      still holds it and no later specialist-generation mutation can commit
      before it ends. A parent-held lock would be released by the cancelled
      handler while an abandoned `_bundle_compensate` could still
      `rollback_disk` over a newer generation — this defect, re-created
      in-process.
    * **A cancel that lands BEFORE the lock was acquired aborts outright.**
      Nothing has been committed there, and shielding would run a mutation the
      caller aborted. `entered` is set synchronously between the acquire
      returning and the operation's first await, and the loop is
      single-threaded, so when this coroutine gets control the child is
      suspended at exactly one await: `entered` unset means the library call was
      never submitted to the executor. (That submission is the real boundary —
      `asyncio.to_thread` hands the callable to the executor before it suspends,
      which is why a cancel during the library call commits anyway, and why the
      shield has to start before it.)
    * **The absorption is bounded and the transaction is not.** Past
      `_SPECIALIST_BUNDLE_CANCEL_GRACE_S` from the FIRST absorbed cancellation
      the wait is abandoned; the child is never cancelled and keeps the lock.

    The absorbed cancellation is re-raised either way, so the handler still
    finishes cancelled exactly once and the SDK still sees the cancellation it
    asked for — delayed, never converted into a success."""
    entered = asyncio.Event()
    lock = _PLUGIN_TOOLS_LOCK           # read at call time: tests rebind it

    async def _owned() -> Any:
        async with lock:
            entered.set()
            return await operation()

    task = asyncio.create_task(_owned())
    _BUNDLE_MUTATION_TASKS.add(task)
    task.add_done_callback(_bundle_transaction_done)

    loop = asyncio.get_running_loop()
    cancellation: "asyncio.CancelledError | None" = None
    deadline: "float | None" = None
    while not task.done():
        try:
            if deadline is None:
                await asyncio.shield(task)
            else:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except asyncio.CancelledError as exc:
            if not entered.is_set():
                # Nothing begun: no lock held, no library call dispatched.
                task.cancel()
                raise
            if cancellation is None:
                # The deadline is anchored to the FIRST absorbed cancellation
                # and never re-armed: a caller that keeps cancelling must not be
                # able to extend its own wait indefinitely.
                cancellation = exc
                deadline = loop.time() + _SPECIALIST_BUNDLE_CANCEL_GRACE_S
        except TimeoutError:
            break
        except Exception:  # noqa: BLE001 — the transaction's own failure
            break
    if not task.done():
        # Only reachable through the deadline branches.
        logger.error(
            "specialist bundle transaction still running %.0fs after "
            "cancellation — abandoning the wait; it keeps the mutation lock "
            "and still completes or compensates its journal",
            _SPECIALIST_BUNDLE_CANCEL_GRACE_S)
        if cancellation is not None:
            raise cancellation
        raise asyncio.CancelledError()
    # An absorbed cancellation takes PRECEDENCE over the transaction's own
    # outcome: the caller asked to be cancelled and is, exactly once. The
    # transaction's failure is retrieved for hygiene only.
    if cancellation is not None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("specialist bundle transaction failed while "
                         "cancellation was pending")
        raise cancellation
    return task.result()


async def _bundle_reload_and_verify(
    slug: str, *, removed_artifact_ids: list, targets_removed: list) -> dict:
    """Task 10 bundle sequencer (spec §3.2d) — the post-mutation half of a
    specialist-bundle transaction, run by the tool layer while holding
    `_PLUGIN_TOOLS_LOCK` (the sync lifecycle function already committed the
    registry swap + tuple/sidecar and returned its BundleTxn). Bundle-shaped
    (many owned plugins, one specialist role), mirroring
    `_reload_and_verify_targets` but for the whole owned set:

      1. grant/challenge invalidation FIRST — per removed/replaced artifact id
         (the SAME `_invalidate_lifecycle` call plugin_update/plugin_remove use)
         AND the specialist role + any removed targets, BEFORE the first reload;
      2. ONE plugin-snapshot reload;
      3. ONE specialist-agent reload (only when the slug is installed — skipped
         for an uninstall or a still-pending specialist target);
      4. desired==active verify for every owned entry the slug now has;
      5. ONE health regeneration + notify.
    """
    import agent as agent_mod
    import reload as reload_mod

    # 1. invalidation FIRST (before any reload await).
    for aid in removed_artifact_ids:
        _invalidate_lifecycle(artifact_id=aid)
    _invalidate_lifecycle(roles=[f"specialist:{slug}", *targets_removed])

    # 2. ONE snapshot reload.
    await asyncio.to_thread(plugin_registry.reload_snapshot)
    # #1053: the bundle's owned entries, by their scoped names, belong to the
    # engagement installing it (an uninstall has none left to record).
    try:
        _owned = await asyncio.to_thread(
            lambda: [e.get("name") for e in plugin_registry.owned_entries_for(
                slug, plugin_registry.load_registry())])
    except Exception:  # noqa: BLE001 — recording never fails the sequencer
        logger.debug("bundle installer record skipped", exc_info=True)
        _owned = []
    _record_plugin_installer(_owned)

    # 3. agents-level reconstruction / eviction.
    runtime = getattr(agent_mod, "active_runtime", None)
    reloaded: list = []
    reload_errors: list = []
    # #1146: the component root of the specialist's live agent, read when its
    # own reload returned — None when no reload ran or there is nothing to read.
    loaded_root_after_reload = None
    uninstalling = bool(targets_removed)
    if runtime is not None:
        if uninstalling:
            # Whole-branch C: the removed specialist's live agent + bus
            # registration must be EVICTED. A single-role dispatch("agent")
            # would raise unknown_role for a just-deleted slug and skip
            # eviction entirely (the old code did exactly that — it only
            # reloaded on the non-uninstall branch). Run the full agents
            # add/evict sweep (reload_agents) instead — it pops the removed
            # specialist from runtime.agents and deregisters its bus consumer
            # (evicted_specialist_<role>).
            res = await reload_mod.dispatch("agents", runtime=runtime)
            if res.get("status") == "ok":
                reloaded.append(f"evicted:specialist:{slug}")
            else:
                reload_errors.append({"target": f"specialist:{slug}", **res})
        elif not _specialist_target_pending(runtime, slug):
            res = await reload_mod.dispatch("agent", runtime=runtime, role=slug)
            loaded_root_after_reload = _live_component_root(runtime, slug)
            if res.get("status") == "ok":
                reloaded.append(f"specialist:{slug}")
            else:
                reload_errors.append({"target": f"specialist:{slug}", **res})

    # 4. desired==active verify for every owned entry the slug now carries.
    reg = plugin_registry.load_registry()
    verify: dict = {}
    not_ready: list = []
    for entry in plugin_registry.owned_entries_for(slug, reg):
        name = entry.get("name")
        v = await asyncio.to_thread(_tool_verify_plugin_state, plugin_name=name)
        verify[name] = v
        # Whole-branch B: a not-ready owned binding (desired != active) is a
        # sequencer FAILURE, not a silent success. P1-3: gate ONLY on
        # integrity/binding failures (artifact/mcp/registry-entry reasons +
        # reload_required/verify_unstable), NOT operator-config readiness.
        # verify's top-level `ready` folds in unresolved secrets and missing
        # sysreq bins, but a config-pending specialist is a verified-legal
        # terminal state (spec §3.2): its owned entries activate at commit and
        # are inert until configured. Rolling such an install back would be a
        # spurious compensation. `_bundle_binding_blocked` inspects the reason
        # codes so an env-declaring bundled plugin awaiting its secret passes.
        if _bundle_binding_blocked(v):
            not_ready.append(name)

    # Whole-branch B/C (uninstall): every removed scoped name must be ABSENT
    # from the specialist's resolution and the specialist agent must be gone.
    absent_violations: list = []
    if uninstalling:
        resolved_names = await asyncio.to_thread(
            lambda: {rp.name for rp in
                     plugin_registry.resolve_for(f"specialist:{slug}").plugins})
        absent_violations = sorted(
            n for n in resolved_names if n.startswith(f"{slug}."))
        if runtime is not None and slug in (getattr(runtime, "agents", {}) or {}):
            absent_violations.append(f"agent:{slug}")

    # 5. ONE health regeneration + notify.
    await _regenerate_plugin_health_held()
    await _notify_plugin_health_if_possible()

    ok = not reload_errors and not not_ready and not absent_violations
    kind = (None if ok else "reload_failed" if reload_errors
            else "postcondition_failed")
    if not ok:
        # #491: the blocking verdict must be diagnosable from logs — the
        # caller compensates on this result, and pre-fix the decisive reason
        # existed solely inside the tool envelope.
        blocking = {
            name: sorted({code for code in (verify.get(name, {}).get("reasons") or [])
                          if code not in _BUNDLE_NONBLOCKING_REASONS}
                         | {code for row in (verify.get(name, {}).get("targets") or [])
                            for code in (row.get("reasons") or [])
                            if code not in _BUNDLE_NONBLOCKING_REASONS})
            for name in not_ready}
        logger.warning(
            "bundle sequencer failed for %s: kind=%s not_ready=%s "
            "blocking_reasons=%s reload_errors=%s absent_violations=%s",
            slug, kind, not_ready, blocking,
            [e.get("target") for e in reload_errors], absent_violations)
    return {
        "ok": ok,
        "kind": kind,
        "reloaded": reloaded, "reload_errors": reload_errors,
        "not_ready": not_ready, "absent_violations": absent_violations,
        "verify": verify, "removed_artifact_ids": list(removed_artifact_ids),
        "loaded_root_after_reload": loaded_root_after_reload,
    }


def _live_component_root(runtime, slug: str) -> "str | None":
    """#1146: the component root of *slug*'s live agent, or None.

    The binding's `component_root` first; #1324: when the binding carries none
    (a persona override), the root of the tuple the config was activated from
    (`active_tuple_root`, set by `activate_binding_for_config` with the
    binding). Synchronous, so the caller reads it in the same step as the
    reload's return. None — no evidence, never "a different version" — when
    there is no agent, no binding, or no root on either."""
    config = getattr((getattr(runtime, "agents", None) or {}).get(slug), "config", None)
    binding = getattr(config, "binding", None)
    if binding is None:
        return None
    root = getattr(binding, "component_root", None)
    if root is None:
        root = getattr(config, "active_tuple_root", None)
    return root if isinstance(root, str) else None


async def _bundle_compensate(txn) -> bool:
    """Whole-branch B: shared compensation for a FAILED bundle sequencer — roll
    the disk state back, run a best-effort compensating sequencer over the
    restored state (un-publishing the runtime state with the new set's artifact
    ids), then complete the journal. Never raises out. Returns True when the
    disk rollback succeeded (journal completed), False when it raised (journal
    left in-progress for boot reconciliation; the caller surfaces
    `compensation_failed`).

    P1-1: complete the journal ONLY after a SUCCESSFUL disk rollback. A
    rollback that raises (e.g. an unreadable registry — `rollback_disk` fails
    closed rather than persist a partial doc) must leave the in-progress
    journal on disk so boot reconciliation re-runs the rollback or quarantines
    the slug; completing it here would strand a half-rolled-back mutation with
    no recovery.

    P1-2: derive the compensating-sequencer direction from the RESTORED
    before-state, not the forward op. When the before-state has no installed
    specialist (its `active.yaml` was absent — e.g. a failed FRESH install),
    the just-created live agent must be EVICTED and verified absent
    (uninstall-shaped sweep); otherwise the prior generation's agent is
    reconstructed (install-shaped). Reusing the forward install-shaped call
    left a fresh-install rollback's live agent reachable.

    #491 (Sol design r1): the compensating sequencer's own outcome is graded,
    not discarded — a disk rollback that succeeded while the runtime sweep
    failed is NOT "prior state fully restored", and the envelope must be able
    to say so. Returns {"disk_ok": bool, "runtime_ok": bool}; journal
    semantics are unchanged (completed exactly when the disk rollback
    succeeded)."""
    import specialist_bundle_journal
    # #975 (the operator's 2026-09-29 ruling): a reclassifying upgrade whose
    # new version is already active is KEPT — the version it replaced cannot be
    # restored whole. No rollback and no compensating sequencer (which would
    # un-publish the kept version's artifacts): finish the retained prior and
    # complete the journal. If that cleanup fails the journal stays for boot,
    # which finishes it the same way.
    activation_kept = getattr(txn, "activation_kept", None)
    try:
        kept = callable(activation_kept) and await asyncio.to_thread(activation_kept)
    except Exception:  # noqa: BLE001 — ActivationUnknown: undo nothing
        # Whether the new version is active cannot be read back, and either
        # guess can lose a setting: restore nothing, leave the journal for
        # boot, and report it as the compensation that did not complete.
        logger.warning(
            "bundle upgrade of %s: cannot establish whether its new version is "
            "active; leaving the journal for boot", txn.slug, exc_info=True)
        return {"disk_ok": False, "runtime_ok": False}
    if kept:
        try:
            await asyncio.to_thread(txn.finish_forward)
        except Exception:  # noqa: BLE001 — boot finishes it
            logger.warning(
                "bundle upgrade of %s kept its new version, but finishing its "
                "retained prior failed; leaving the journal for boot",
                txn.slug, exc_info=True)
            return {"disk_ok": False, "runtime_ok": False, "kept_new_version": True}
        await asyncio.to_thread(specialist_bundle_journal.complete, txn.journal_path)
        return {"disk_ok": True, "runtime_ok": False, "kept_new_version": True}
    try:
        await asyncio.to_thread(txn.rollback_disk)
    except Exception:  # noqa: BLE001 — leave the journal for boot reconcile
        logger.warning(
            "bundle disk rollback failed for %s; leaving the in-progress "
            "journal for boot reconciliation", txn.slug, exc_info=True)
        return {"disk_ok": False, "runtime_ok": False}
    # Direction from the restored before-state: an absent prior active.yaml
    # means the specialist should NOT exist after compensation → evict + verify
    # absent; a present one means a prior generation is being restored →
    # reconstruct.
    before_tuple_files = getattr(txn, "before_tuple_files", None) or {}
    installed_before = before_tuple_files.get("active.yaml") is not None
    comp_targets = [] if installed_before else [f"specialist:{txn.slug}"]
    runtime_ok = False
    try:
        comp_seq = await _bundle_reload_and_verify(
            txn.slug, removed_artifact_ids=list(txn.new_artifact_ids),
            targets_removed=comp_targets)
        runtime_ok = bool(comp_seq.get("ok"))
    except Exception:  # noqa: BLE001 — best-effort; boot is the backstop
        logger.warning("bundle compensating sequencer failed for %s", txn.slug,
                       exc_info=True)
    await asyncio.to_thread(specialist_bundle_journal.complete, txn.journal_path)
    return {"disk_ok": True, "runtime_ok": runtime_ok}


# #975: the one shape of failure after which nothing is rolled back.
_KEPT_NEW_VERSION_ENVELOPE = {
    "kept_new_version": True,
    "outcome": (
        "the new version is active and stays active: the version it replaced "
        "cannot be restored whole, because a setting it kept as a plain value is "
        "now secret. Nothing was rolled back; the failure above is reported, and "
        "re-running the same upgrade finishes whatever it left undone"),
}

# #1146: the same kept failure when the specialist's own reload failed and its
# live agent was not the new version when that reload returned. Past and
# standing facts only: the reload result is the observation, and active.yaml
# naming the new version is what the next load reads. The live root is
# compared with the new version's only, so the text names no older version:
# after an earlier failed upgrade the live one need not be the one replaced.
_KEPT_NOT_LOADED_OUTCOME = (
    "the new version is kept: its files are committed and active.yaml names "
    "it, because the version it replaced cannot be restored whole (a setting it "
    "kept as a plain value is now secret). Reloading the specialist failed, and "
    "when that reload returned the specialist was not running the new version. "
    "Nothing was rolled back; the failure above is reported, and Casa tries to "
    "load the new version at the next reload or restart")

# #1146: the same kept failure when the specialist's own reload failed and left
# no root to compare — the specialist had no live agent, its live config names
# no root (#1324: a persona override's is the tuple root it was activated from,
# so it has one), or the sequencer recorded nothing. That is no evidence
# either way, so it says neither "active" nor "previous version".
_KEPT_RUNNING_UNKNOWN_OUTCOME = (
    "the new version is kept: its files are committed and active.yaml names "
    "it, because the version it replaced cannot be restored whole (a setting it "
    "kept as a plain value is now secret). Reloading the specialist failed, and "
    "which version it was running when that reload returned could not be "
    "established. Nothing was rolled back; the failure above is reported, and "
    "Casa tries to load the new version at the next reload or restart")


# #1095 (ruling-1095-5, ruling-1095-6): the LIBRARY-kept arm of
# specialist_upgrade returns before the sequencer, so Casa has loaded none of
# the kept version. Said in the present tense, at the moment of telling: an
# unrelated reload before the re-run can change what new conversations get,
# so neither text promises how long the previous version stays in use.
#
# #1296: the in-use clause is true only when the specialist Casa finds running
# is the version this upgrade replaced. After an earlier failed upgrade it can
# be an older one, so the clause is chosen by comparing the live agent's
# component root with the replaced root the library carries: equal keeps the
# ruled text; a different root uses _KEPT_NOT_LOADED_OUTCOME's phrase, and no
# root to compare uses _KEPT_RUNNING_UNKNOWN_OUTCOME's, both dated by this
# upgrade. Every other byte is the ruled text's.
#
# #1298 (the operator's ruling): a reload of the specialist that lands while
# the library runs can load the kept version itself. A live root equal to the
# root this upgrade committed is "new", tested first; its head drops "not active
# yet ... has not loaded it", which is false there, and its clause is dated like
# the others — never a claim about what is running when the result is read.
# "unknown" (no root to compare: no live agent, nothing carried) uses the
# same head: such a reload may have loaded the kept version, and no evidence
# must never read as "not loaded" (#1146).
_KEPT_NOT_ACTIVE_HEAD = (
    "the upgrade is not active yet: the new version is kept on disk, but Casa "
    "has not loaded it")
_KEPT_LOADED_HEAD = "the upgrade did not finish: the new version is kept on disk"
_KEPT_IN_USE_CLAUSES = {
    "matched": ", so new and open conversations still use the previous version",
    "other": (", and when this upgrade returned the specialist was not running "
              "the new version"),
    "unknown": (", and which version the specialist was running when this "
                "upgrade returned could not be established"),
    "new": (", and when this upgrade returned the specialist was running the new "
            "version"),
}
_KEPT_NOT_ACTIVE_TAILS = {
    False: ". Re-running the same upgrade finishes it.",
    True: (". Finishing the version it replaced failed too, so further changes "
           "to this specialist are refused until Casa restarts: restart Casa, "
           "then re-run the upgrade."),
}
_KEPT_NOT_ACTIVE_OUTCOMES = {
    (restart, live): (_KEPT_LOADED_HEAD if live in ("new", "unknown")
                      else _KEPT_NOT_ACTIVE_HEAD)
    + clause + tail
    for live, clause in _KEPT_IN_USE_CLAUSES.items()
    for restart, tail in _KEPT_NOT_ACTIVE_TAILS.items()}
_KEPT_NOT_ACTIVE_OUTCOME = _KEPT_NOT_ACTIVE_OUTCOMES[(False, "matched")]
_KEPT_NOT_ACTIVE_RESTART_OUTCOME = _KEPT_NOT_ACTIVE_OUTCOMES[(True, "matched")]


def _kept_live_match(runtime, slug: str, replaced_root, *,
                     committed_root=None) -> str:
    """#1296: how *slug*'s live agent compares with the root a library-kept
    upgrade replaced — "matched", "other", or "unknown" when either side has no
    root (no live agent, nothing carried). Synchronous: the caller reads it in
    the step that composes the result.

    #1298: "new" first, when the live root is the root the upgrade committed. No
    carried committed root is never "new"; the other states are unchanged."""
    live = _live_component_root(runtime, slug)
    if not isinstance(live, str):
        return "unknown"
    if isinstance(committed_root, str) and live == committed_root:
        return "new"
    if not isinstance(replaced_root, str):
        return "unknown"
    return "matched" if live == replaced_root else "other"


async def _bundle_seq_failure(txn, seq: dict, *, slug: str) -> dict:
    """Whole-branch B: a sequencer that returned ok:false — compensate and
    build the ok:false envelope. P1-1: when the disk rollback could not
    complete (journal left in-progress for boot reconciliation), tag the
    envelope `compensation_failed` so the caller surfaces that the runtime
    state may be inconsistent until the next boot.

    #491: a SUCCESSFUL compensation is stated just as loudly — `rolled_back:
    true` plus a human-readable `outcome` — because the remaining fields
    (`not_ready`, per-variable verify rows) read naturally as a description
    of a present-but-degraded install, and a caller without out-of-band
    knowledge of the compensation contract reported exactly that (a
    rolled-back install as committed). When the disk rolled back but the
    runtime sweep did not converge, `runtime_compensation_incomplete` says
    so rather than overclaiming a full restore (Sol design r1)."""
    compensated = await _bundle_compensate(txn)
    env = {"ok": False, "kind": seq.get("kind") or "bundle_sequence_failed",
           "slug": slug, "reload_errors": seq.get("reload_errors"),
           "not_ready": seq.get("not_ready"),
           "absent_violations": seq.get("absent_violations"),
           "verify": seq.get("verify")}
    if compensated.get("kept_new_version"):
        # #975: nothing was rolled back, so none of the rollback wording below
        # is true — say what holds instead.
        env.update(_KEPT_NEW_VERSION_ENVELOPE)
        # #1146: after a failed reload "active" holds only when the root the
        # reload left is the new version's (the swap landed, e.g. a
        # reregister_failed). A different root says the new version was not
        # running; no root (no agent, no key) is no
        # evidence either way, and the text says it could not be established.
        loaded_root = seq.get("loaded_root_after_reload")
        if seq.get("reload_errors"):
            if not isinstance(loaded_root, str):
                env["outcome"] = _KEPT_RUNNING_UNKNOWN_OUTCOME
            elif loaded_root != getattr(txn, "target_root", None):
                env["outcome"] = _KEPT_NOT_LOADED_OUTCOME
        # The kept version's owned-plugin swap is a committed removal of
        # whatever it dropped (INV-TOOL-007), exactly as on success.
        env.update(_swap_removal_disclosure(txn))
        if not compensated["disk_ok"]:
            env["outcome"] += (
                "; finishing the retained prior version failed, so its undo "
                "record is still standing and further changes to this "
                "specialist are refused until Casa restarts and finishes it")
        return env
    if not compensated["disk_ok"]:
        env["compensation_failed"] = True
        # #838: this is the arm that CREATES recovery debt, and it was the only
        # one of the three with no `outcome` sentence — the caller told least at
        # the moment it can still act. Say what now holds: the journal stands,
        # this specialist refuses further changes (INV-SPEC-014), and a restart
        # is what lets boot reconciliation finish or quarantine it.
        env["outcome"] = (
            "the mutation could not be rolled back on disk, so its undo record "
            "is still standing; further changes to this specialist are refused "
            "until Casa is restarted and boot reconciliation finishes or "
            "quarantines it")
        # #676: this arm is the one ok:false envelope whose registry mutation
        # PERSISTS. On an uninstall that cascaded owned plugins out, that is a
        # committed removal the operator is being told about — so it owes the
        # same disclosure, in attempted-removal wording. The other two arms
        # (rolled_back, runtime_compensation_incomplete) restored the entries,
        # so the same fields there would be false. An install/upgrade/rollback
        # reaching this arm removed nothing: its `before_entries` is the
        # PRE-SWAP owned set, not a removal.
        # Terra diff-review r11: the candidate set is what the swap DROPPED,
        # not what the transaction captured. A pending/error upgrade leaves the
        # owned set UNCHANGED and hands it through in `before_entries`, so
        # reading that as a removal warns about surviving plugin data after an
        # operation that removed nothing — and the indeterminate arm cannot
        # measure its way out of that, because there is nothing to measure.
        # Sol diff-review a3-r2: the same is true one step finer. Even a
        # transaction that DID swap kept most of `before_entries`, so the
        # indeterminate arm named plugins the operation had re-published. One
        # accessor now answers "what did this swap drop" for the success
        # payloads and for this arm alike.
        owned = _swap_dropped_names(txn)
        if owned:
            # Sol diff-review r1: `compensation_failed` does NOT establish that
            # the removal persisted. rollback_disk() restores the registry in
            # its FIRST step and then does fallible work (tuple/sidecar writes,
            # the fresh-install symlink GC, the ack re-insert), any of which can
            # raise after the entries are already back. Disclosing on the flag
            # alone would tell an operator their plugin data survived a removal
            # that had just been undone — the false-warning half of this
            # invariant. So MEASURE it: read the registry back and disclose only
            # for the names actually still absent. A read that fails establishes
            # nothing either way, and says so.
            #
            # Sol diff-review r8: and the measurement is the WHOLE gate — an
            # earlier `op == "uninstall"` test was wrong in the other direction.
            # An upgrade or a rollback swaps the owned set wholesale, so either
            # can drop a plugin the previous set had; when compensation then
            # fails with that entry still gone, it is a persisting committed
            # removal reached by a third door and owes the same disclosure. On
            # an install whose swap replaced nothing, or any op whose entries
            # came back, `still_absent` is empty and nothing is said.
            still_absent, readable = await asyncio.to_thread(
                _absent_owned_names, txn, owned)
            if not readable:
                env.update(_plugin_data_disclosure(
                    _PLUGIN_DATA_NOTE_INDETERMINATE, owned))
            elif still_absent:
                env.update(_plugin_data_disclosure(
                    _PLUGIN_DATA_NOTE_ATTEMPTED, still_absent))
        return env
    env["rolled_back"] = True
    if compensated["runtime_ok"]:
        env["outcome"] = (
            "the mutation was rolled back; the prior state was restored and "
            "the requested change is NOT in effect")
    else:
        env["runtime_compensation_incomplete"] = True
        env["outcome"] = (
            "the mutation was rolled back on disk (the requested change is "
            "NOT in effect), but the runtime did not fully converge to the "
            "restored state; the next reload or restart converges it")
    return env


def _prune_bundle_receipt(receipt_id: "str | None") -> None:
    """Whole-branch N: delete a CONSUMED source receipt after its bundle
    transaction completes (a receipt is single-use — install/upgrade). Best
    effort: a prune failure never fails the (already-committed) mutation; the
    boot-time age sweep is the backstop."""
    if not receipt_id:
        return
    try:
        import specialist_receipt
        specialist_receipt.delete(receipt_id)
    except Exception:  # noqa: BLE001 — pruning is best-effort
        logger.warning("receipt prune failed for %s", receipt_id, exc_info=True)


def _safe_remove_manifest(name: str) -> None:
    """Sol round-4: remove the plugin's sysreq manifest row, tolerating failure
    (unwritable/malformed manifest) so a cleanup error never bypasses the
    mandatory reload/verify sequencing that follows a registry mutation."""
    try:
        remove_manifest(name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("plugin sysreq manifest cleanup for %s failed "
                       "(non-fatal): %s", name, exc)


def _install_plugin_sysreqs(name: str, manifest: dict) -> dict | None:
    """§3.3: install a plugin's system requirements BEFORE registry activation.
    Returns an error envelope on failure (registry left unchanged), else None."""
    try:
        reqs = plugin_store.manifest_sysreqs(manifest)
    except plugin_store.StoreError as exc:
        # #354: unreachable on the add/update paths (publish already
        # strict-validated the manifest); belt for any future caller
        # handing in a stored pre-strictness manifest.
        return {"ok": False, "kind": "system_requirements_invalid",
                "detail": str(exc)}
    tools_root = Path("/config/tools")
    # #354 (Sol P1-4): snapshot this plugin's PRIOR manifest rows so a
    # verify_bin rename can retire the old published link — otherwise the
    # old name stays resolvable (or dangling) forever, invisible to
    # verification, which only checks the new name.
    prior_bins = {p.get("verify_bin") for p in read_sysreq_manifest()["plugins"]
                  if p.get("name") == name and p.get("verify_bin")}
    if not reqs:
        # Sol round-3 M: an update to a manifest with NO requirements must clear
        # any stale row (add_plugin_entry replaces by name on the has-reqs path,
        # so only this branch leaks). No-op for a brand-new plugin. Sol round-4:
        # cleanup failure is non-fatal — never break the mutation sequence.
        _safe_remove_manifest(name)
        for stale in prior_bins:
            retire_stale_bin(stale, name, tools_root)
        return None
    try:
        outcomes = install_requirements(
            plugin_name=name, requirements=reqs, tools_root=tools_root)
    except OrchestrationError as exc:
        return {"ok": False, "kind": "system_requirements_failed",
                "detail": str(exc)}
    new_bins = {o.verify_bin for o in outcomes if o.verify_bin}
    for stale in prior_bins - new_bins:
        retire_stale_bin(stale, name, tools_root)
    for outcome in outcomes:
        add_manifest(outcome.manifest_entry(name))
    return None


def _resolve_and_guard(*, repo: str, ref: str,
                       expected_revision: str | None) -> "str | dict":
    """C.2 steps 1-2 (v0.74.0): resolve ref -> sha, then the
    expected_revision guard. Returns the 40-hex commit on success, else a
    failure envelope dict. See ``_resolve_ref_and_guard`` for the pair."""
    got = _resolve_ref_and_guard(repo=repo, ref=ref, expected_revision=expected_revision)
    return got if isinstance(got, dict) else got[1]


def _resolve_ref_and_guard(*, repo: str, ref: str,
                           expected_revision: str | None) -> "tuple[str, str] | dict":
    """Resolve ``ref`` -> ``(effective_ref, commit)``, then the
    expected_revision guard. Design 2026-09-15 §2.C: the literal ``latest``
    resolves to a published release tag (``plugin_store.resolve_latest_release``,
    tag namespace, never a branch) and the TAG is the effective ref — what the
    registry stores and the result reports; an exact ref is its own effective
    ref. Returns a failure envelope dict otherwise; ``no_release_found`` is a
    hard refusal that mutates nothing."""
    try:
        if ref == plugin_store.LATEST_REF:
            effective, commit = plugin_store.resolve_latest_release(repo)
        else:
            effective, commit = ref, plugin_store.resolve_ref(repo, ref)
    except plugin_store.NoReleaseFound:
        return {"ok": False, "kind": "no_release_found", "repo": repo}
    except plugin_store.RefNotFound:
        return {"ok": False, "kind": "ref_not_found"}
    except plugin_store.ResolveAuthFailed:
        return {"ok": False, "kind": "resolve_auth_failed"}
    except plugin_store.SourceEmpty:
        return {"ok": False, "kind": "source_empty"}
    except plugin_store.ResolveUnavailable as exc:
        env = {"ok": False, "kind": "resolve_unavailable"}
        if getattr(exc, "retry_after_s", None) is not None:
            env["retry_after_s"] = exc.retry_after_s
        return env
    except plugin_store.StoreError as exc:
        return {"ok": False, "kind": getattr(exc, "reason_code", "store_error")}
    if expected_revision is not None:
        want = plugin_store.normalize_revision(expected_revision)
        if want is None:
            return {"ok": False, "kind": "invalid_expected_revision",
                    "expected_revision": expected_revision}
        if want != commit:
            # A tag that MOVED between the producer's build and this pin
            # (spec C.2 step 2) — hard abort, nothing mutated.
            return {"ok": False, "kind": "revision_mismatch",
                    "expected_revision": want, "resolved_revision": commit}
    return effective, commit


def _tag_version_guard(ref: str, manifest: dict) -> dict | None:
    """C.2 step 4 (v0.74.0): a release-tag ref must equal 'v' + the FETCHED
    manifest's version. None when the guard passes (or ref is not a release
    tag). Runs BEFORE sysreq install and registry mutation."""
    if not plugin_store.RELEASE_TAG_RE.match(ref or ""):
        return None
    version = str((manifest or {}).get("version", ""))
    if ref != f"v{version}":
        return {"ok": False, "kind": "tag_version_mismatch",
                "ref": ref, "manifest_version": version}
    return None


def _plugin_add_local(*, name: str, subdir: str = "",
                      targets: list) -> "tuple[dict | None, dict]":
    """plugin_add's LOCAL guards, read-only: ``(refusal, state)``. One source,
    called by the core and — #1145 — by the handler first, to learn which
    specialists the change reaches before warning about them."""
    if not plugin_registry.NAME_RE.match(name or ""):
        return {"ok": False, "kind": "invalid_name", "name": name}, {}
    targets = list(targets or [])
    # Sol round-3 M: a non-string target must not raise TypeError out of the
    # envelope (TARGET_RE.match(1) throws) — reject it as an invalid target.
    bad = [t for t in targets
           if not isinstance(t, str) or not plugin_registry.TARGET_RE.match(t)]
    if bad or not targets:
        return {"ok": False, "kind": "invalid_target", "invalid": bad}, {}
    # #923 (operator's decision, 2026-09-10): operators cannot target executors
    # ("workers") with plugins for now — workers come with the bundled set Casa
    # ships. Every entry plugin_add can create is github-sourced, so no source
    # test is needed here. Raised from the HANDLER, before the registry read,
    # the publish, the sysreq install and the write, so a refused call leaves
    # the machine and the operator's registry byte-identical; a JSON-schema
    # `pattern` would refuse earlier but with no reason the operator can read,
    # and would also refuse the string shape the bundled path needs. The whole
    # request is refused: a mixed resident+executor call is not partly installed.
    worker = [t for t in targets if t.startswith("executor:")]
    if worker:
        return {"ok": False, "kind": "executor_target_not_allowed",
                "invalid": worker,
                "detail": ("for now a plugin cannot be assigned to a worker — "
                           "workers use only the plugins Casa ships")}, {}
    # Sol round-3 M: validate subdir here so `../x` returns an envelope instead
    # of an uncaught ValueError from normalize_subdir inside publish.
    try:
        plugin_registry.normalize_subdir(subdir or "")
    except ValueError:
        return {"ok": False, "kind": "invalid_subdir", "subdir": subdir}, {}
    data = plugin_registry.load_registry()                     # from DISK
    if not data.valid:
        return {"ok": False, "kind": "registry_invalid"}, {}
    if any(isinstance(e, dict) and e.get("name") == name
           for e in data.raw.get("plugins", [])):
        return {"ok": False, "kind": "plugin_exists", "name": name}, {}
    return None, {"data": data, "targets": targets}


def _plugin_add_sync(*, name: str, repo: str, ref: str, subdir: str = "",
                     targets: list,
                     expected_revision: str | None = None) -> dict:
    """Blocking core of plugin_add. C.2 (v0.74.0) enforcement ORDER — all
    identity guards run before any sysreq install or registry mutation:
    resolve -> revision-check -> manifest-fetch (publish) -> tag/version-check
    -> sysreqs -> activate. Registry stays byte-identical on any
    pre-activation failure (FR2)."""
    refusal, state = _plugin_add_local(name=name, subdir=subdir, targets=targets)
    if refusal is not None:
        return refusal
    data, targets = state["data"], state["targets"]
    guarded = _resolve_ref_and_guard(repo=repo, ref=ref,
                                     expected_revision=expected_revision)
    if isinstance(guarded, dict):
        return guarded
    ref, commit = guarded          # "latest" -> the release tag it resolved to
    try:
        result = plugin_store.publish(name=name, repo=repo, ref=ref,
                                      subdir=subdir, commit=commit)
    except plugin_store.RefNotFound:
        return {"ok": False, "kind": "ref_not_found"}
    except plugin_store.ResolveUnavailable:
        return {"ok": False, "kind": "resolve_unavailable"}
    except plugin_store.StoreError as exc:
        return {"ok": False, "kind": getattr(exc, "reason_code", "store_error"),
                **getattr(exc, "detail", {})}
    err = _tag_version_guard(ref, result.manifest)             # BEFORE sysreqs
    if err is not None:
        return err
    err = _install_plugin_sysreqs(name, result.manifest)       # BEFORE activate
    if err is not None:
        return err
    data.raw.setdefault("plugins", []).append({
        "name": name,
        "source": {"type": "github", "repo": repo, "ref": ref,
                   "revision": result.revision, "subdir": subdir},
        "artifact_id": result.artifact_id, "version": result.version,
        "targets": targets,
    })
    plugin_registry.save_registry(data)
    return {"ok": True, "name": name, "targets": targets,
            "artifact_id": result.artifact_id, "version": result.version,
            "revision": result.revision, "path": result.path,
            "resolved_ref": ref,
            # #241: hand the JUST-published manifest to _resolved_observability
            # so the setup declaration is read from the activated artifact, not
            # a possibly-stale resolve_all() snapshot. Popped before the result.
            "_published_manifest": result.manifest}


def _plugin_update_local(*, name: str) -> "tuple[dict | None, dict]":
    """plugin_update's LOCAL guards, read-only: ``(refusal, state)``. One
    source, called by the core and — #1145 — by the handler first. The
    revision guard is not here: it needs the network (``_resolve_ref_and_guard``)."""
    data = plugin_registry.load_registry()
    if not data.valid:
        return {"ok": False, "kind": "registry_invalid"}, {}
    entry = next((e for e in data.raw.get("plugins", [])
                  if isinstance(e, dict) and e.get("name") == name), None)
    if entry is None:
        return {"ok": False, "kind": "not_registered", "name": name}, {}
    owner = plugin_registry.entry_owner(entry)
    if owner is not None:
        return {"ok": False, "kind": "owned_by_specialist", "owner": owner,
                "detail": (f"{name!r} is managed by {owner}'s bundle — use "
                          "specialist_upgrade / specialist_uninstall")}, {}
    # #923, both halves, and each covers the other's residue.
    #
    # (1) Refuse when a BUNDLED entry serves a worker. Without this, updating a
    # seeded entry to an operator-chosen ref repoints Casa's shipped plugin at
    # the operator's bytes while the entry keeps its `bundled` label, so those
    # bytes keep reaching the worker — the exact outcome the ruling forbids,
    # reached through a supported tool. Refused HERE, before resolve, publish,
    # sysreqs and save. Casa updates its own worker plugins by shipping a
    # release; `seed_defaults` adds unseen names and never re-pins, so this
    # never promises that an upgrade re-pins an existing default.
    #
    # (2) Every other successful update stamps `source.type = "github"` below,
    # in the same save as the new pin, so the label never outlives the bytes it
    # described. The two compose: (1) means the stamp can never drop a plugin
    # Casa ships out of a worker's set, because such an entry cannot be updated
    # at all.
    if (plugin_registry.is_bundled(entry)
            and any(isinstance(t, str) and t.startswith("executor:")
                    for t in entry.get("targets") or [])):
        return {"ok": False, "kind": "executor_target_not_allowed",
                "name": name,
                "detail": ("this plugin is shipped with Casa and serves a "
                           "worker — for now it is updated by updating Casa, "
                           "not from here")}, {}
    return None, {"data": data, "entry": entry}


def _plugin_update_sync(*, name: str, new_ref: str,
                        expected_revision: str | None = None) -> dict:
    """Blocking core of plugin_update. C.2 (v0.74.0) enforcement ORDER — all
    identity guards run before any sysreq install or registry mutation:
    resolve -> revision-check -> manifest-fetch (publish) -> tag/version-check
    -> sysreqs -> registry repoint. Version DERIVED from the fetched manifest
    (FR5). Old artifact retained."""
    refusal, state = _plugin_update_local(name=name)
    if refusal is not None:
        return refusal
    data, entry = state["data"], state["entry"]
    # A:§3.3 (r1-B8): capture the OLD artifact_id BEFORE the mutation — the
    # caller invalidates its grants/challenges only after this commits.
    old_artifact_id = entry.get("artifact_id")
    src = entry.get("source") or {}
    repo, subdir = src.get("repo", ""), src.get("subdir", "")
    guarded = _resolve_ref_and_guard(repo=repo, ref=new_ref,
                                     expected_revision=expected_revision)
    if isinstance(guarded, dict):
        return guarded
    new_ref, commit = guarded      # "latest" -> the release tag it resolved to
    # Read BEFORE publish, which restores a missing artifact: a plugin Casa is
    # not serving needs the activation below even when nothing else moves.
    was_serving = any(
        rp.name == name and rp.artifact_id == old_artifact_id
        for rp in plugin_registry.resolve_all().plugins)
    try:
        result = plugin_store.publish(name=name, repo=repo, ref=new_ref,
                                      subdir=subdir, commit=commit)
    except plugin_store.RefNotFound:
        return {"ok": False, "kind": "ref_not_found"}
    except plugin_store.ResolveUnavailable:
        return {"ok": False, "kind": "resolve_unavailable"}
    except plugin_store.StoreError as exc:
        return {"ok": False, "kind": getattr(exc, "reason_code", "store_error"),
                **getattr(exc, "detail", {})}
    err = _tag_version_guard(new_ref, result.manifest)         # BEFORE sysreqs
    if err is not None:
        return err
    # Already on this release, with nothing to record: the handler stops here.
    # An approval, a webhook secret and a loaded agent are all bound to the
    # artifact, and this is the same artifact, so there is nothing to redo.
    if (was_serving and result.artifact_id == old_artifact_id
            and result.version == entry.get("version")
            and src.get("type") == "github"
            and src.get("ref") == new_ref
            and src.get("revision") == result.revision):
        return {"ok": True, "unchanged": True, "name": name,
                "targets": list(entry.get("targets") or []),
                "artifact_id": result.artifact_id, "version": result.version,
                "revision": result.revision, "path": result.path,
                "resolved_ref": new_ref,
                "old_artifact_id": old_artifact_id}
    # S8: a new manifest that no longer declares a profile some target HOLDS
    # is refused BEFORE sysreqs and the repoint — never silently widened to
    # full, never silently narrowed to nothing. A profile that stays declared
    # applies its NEW tool list to sessions built after the update.
    import plugin_requirements
    held_profiles = entry.get("profiles") if isinstance(entry.get("profiles"), dict) else {}
    profile_tools: dict = {}
    profile_tools_removed: dict = {}
    _store_root = plugin_registry.snapshot_store_root()
    _old_manifest = plugin_requirements.manifest_at(_store_root, name, entry.get("artifact_id"))
    _servers = plugin_requirements._servers_at(_store_root, name, entry.get("artifact_id"))
    _rname = entry.get("manifest_name") or name
    for _target, _profile in held_profiles.items():
        _tools = plugin_requirements.profile_bare_tools(result.manifest, _profile)
        if _tools is None:
            return {"ok": False, "kind": "profile_missing", "name": name,
                    "target": _target, "profile": _profile,
                    "detail": (f"{new_ref} no longer declares the {_profile!r} "
                               f"profile that {_target} holds — unassign and "
                               "reassign first")}
        profile_tools[_target] = _tools
        # What the new list drops: the old list expanded over the servers the
        # LIVE artifact declares, minus the new list expanded over the servers
        # the NEW artifact declares — a server the update drops counts as
        # dropped tools too. The confirmation names them.
        from plugin_store import expand_tool_names as _expand, mcp_servers_map as _servers_of
        _new_servers = sorted(_servers_of(Path(result.path) / ".mcp.json"))
        _old = {fq for b in (plugin_requirements.profile_bare_tools(_old_manifest, _profile) or [])
                for fq in _expand(_rname, _servers, b)}
        _new = {fq for b in _tools for fq in _expand(_rname, _new_servers, b)}
        profile_tools_removed[_target] = sorted(_old - _new)
    err = _install_plugin_sysreqs(name, result.manifest)       # BEFORE repoint
    if err is not None:
        return err
    entry["source"]["ref"] = new_ref
    entry["source"]["revision"] = result.revision
    # #923 (2): these bytes are the operator's choice now, whatever the entry
    # was seeded as, and `source.type` is the ONE thing that says which
    # population an entry belongs to.
    entry["source"]["type"] = "github"
    entry["artifact_id"] = result.artifact_id
    entry["version"] = result.version
    plugin_registry.save_registry(data)
    return {"ok": True, "name": name, "targets": list(entry.get("targets") or []),
            "profile_tools": profile_tools,
            "profile_tools_removed": profile_tools_removed,
            "artifact_id": result.artifact_id, "version": result.version,
            "revision": result.revision, "path": result.path,
            "resolved_ref": new_ref,
            "old_artifact_id": old_artifact_id,
            # #241: see _plugin_add_sync — read the setup declaration from THIS
            # freshly-published manifest, not a possibly-stale resolve_all().
            "_published_manifest": result.manifest}


def _invalidate_lifecycle(*, artifact_id: "str | None" = None,
                          roles: "list[str] | None" = None) -> None:
    """Purge authorization grants + cancel pending challenges for an
    invalidated artifact and/or role (A:§3.3/§3.4, r1-B8/r2-B5). Call sites
    invoke this AFTER the registry mutation COMMITS and BEFORE the first
    post-commit await/reload — an ABORTED mutation (validation failure)
    must invalidate NOTHING. A stale challenge referencing the invalidated
    artifact/role must never survive to be approved (an obsolete keyboard
    dispatching an unusable continuation is a broken lifecycle even though
    artifact/role binding already blocks consumption). ``roles`` entries may
    be tier-qualified plugin targets (``"specialist:finance"``) — normalized
    via :func:`authz_grants.normalize_role` before purge/cancel, since
    ``GrantKey.enforcement_role`` is always plain."""
    if artifact_id:
        GRANTS.purge_artifact(artifact_id)
        CHALLENGES.cancel_matching(artifact=artifact_id)
        # #792: a capability reference outlives neither the artifact that
        # minted it nor (below) the role it was minted for — an engagement
        # that resumes from its RECORDED artifact path after an update cannot
        # redeem a reference minted under the old artifact.
        import result_broker
        result_broker.STORE.purge_artifact(artifact_id)
        # Release B: an artifact change/removal drops its webhook-trigger
        # consents (the ack identity is artifact-bound) and retires the
        # per-trigger secrets — live + .next + rotation state — BEFORE any
        # re-approval can mint replacements, so a new artifact never
        # inherits the old one's credentials. cancel_matching above already
        # killed any pending consent keyboard for the old artifact
        # (TriggerConsentKey carries artifact_id). Never fatal: the grant
        # purge invariants must hold even if the ack sweep fails.
        try:
            import trigger_acks
            import trigger_reconcile
            import webhook_auth
            for rec in trigger_acks.ACKS.revoke_artifact(artifact_id):
                webhook_auth.retire_secret(
                    rec.get("effective") or "",
                    secrets_dir=trigger_reconcile.SECRETS_DIR)
        except Exception:  # noqa: BLE001
            logger.warning("trigger-ack artifact revoke failed (%s)",
                           artifact_id, exc_info=True)
    for target in roles or ():
        role = normalize_role(target)
        GRANTS.purge_role(role)
        CHALLENGES.cancel_matching(role=role)
        import result_broker
        result_broker.STORE.purge_role(role)


def _setup_fields(manifest: dict) -> dict:
    """The declared ``casa.setupTool`` from ONE manifest, as observability.

    #451 (v0.161.0): this used to also return ``setup_via_consent``, which the
    configurator recipes branched on to decide whether to hand setup work back
    to an agent. That decision is gone — CASA runs a declared setup tool, and
    nothing else does — so there is no classification left to make here. The
    tool NAME is still worth reporting: it says what Casa now owes."""
    import plugin_store as _pstore
    try:
        setup = _pstore.manifest_setup_tool(manifest)
    except Exception:  # noqa: BLE001 — verify already refused it
        setup = None
    return {"setup_tool": setup}


def _resolved_observability(name: str, *, manifest: dict | None = None) -> dict:
    """granted_tools + required_env_vars for the freshly-activated plugin (best
    effort — reads the just-reloaded snapshot; resolve_all finds it regardless
    of target), plus the declared ``setup_tool``.

    #241: the setup field comes from ``manifest`` — the manifest of the artifact
    this mutation JUST published/activated — when provided, NOT from the resolved
    snapshot. During a plugin_update, ``resolve_all()`` can momentarily still
    carry the OLD artifact. The freshly-published manifest is authoritative;
    fall back to the resolved ``rp.manifest`` only when no manifest was handed
    in (legacy callers)."""
    for rp in plugin_registry.resolve_all().plugins:
        if rp.name == name:
            setup = _setup_fields(
                manifest if manifest is not None else rp.manifest)
            return {"granted_tools": grants_for_resolved(rp),
                    "required_env_vars": required_env_vars_for_resolved(rp),
                    **setup}
    # Not in the resolved snapshot yet (shouldn't happen right after activation)
    # — still surface the just-published artifact's setup contract if we have it.
    if manifest is not None:
        return {"granted_tools": [], "required_env_vars": [],
                **_setup_fields(manifest)}
    return {"granted_tools": [], "required_env_vars": [], "setup_tool": None}


_SECRET_CANDIDATE_MAX_QUERIES = 3
_SECRET_CANDIDATE_MAX_ITEMS = 5


def _secret_candidate_queries(name: str, unresolved: list[str]) -> list[str]:
    """The plugin name, then each distinct vendor stem of the unresolved vars
    (``GMAIL_CLIENT_ID`` → ``gmail``), deduplicated, at most three."""
    queries: list[str] = []
    for q in [name.lower()] + [v.split("_", 1)[0].lower() for v in unresolved]:
        if q and q not in queries:
            queries.append(q)
    return queries[:_SECRET_CANDIDATE_MAX_QUERIES]


def _declared_setup_provides(manifest) -> frozenset[str]:
    """The ``casa.setupProvides`` names of ``manifest`` — the manifest the
    mutation just published (#241), never the resolved snapshot, which can
    still be the old artifact during a plugin_update. #1024: those names are
    made by the plugin's own setup tool and wired from its report, so the
    exploration neither searches for them nor lists them as unresolved.

    Absent, not a mapping, or a declaration that does not parse → none: the
    exploration then runs exactly as it would with no declaration, and never
    fails the mutation (``artifact_verdict`` already refuses such an artifact;
    this is the belt). Only the exception's class is logged."""
    if not isinstance(manifest, dict):
        return frozenset()
    try:
        return frozenset(plugin_store.manifest_setup_provides(manifest))
    except Exception as exc:  # noqa: BLE001 — the exploration never fails the mutation
        logger.warning("casa.setupProvides unreadable for secret exploration "
                       "(%s) — treating it as absent", type(exc).__name__)
        return frozenset()


def _secret_candidates(name: str, required_env_vars: list[str], *,
                       manifest: dict | None = None) -> dict:
    """Explore the configured default vault for a just-installed plugin's
    unresolved secrets and return what is there — per item the query term it
    matched and its id, per field its id, its role and its type; never a
    title, a label, a section or a value (the projection is
    ``_project_item_fields``, the same one ``get_item_fields`` uses). Wires
    nothing: ``set_plugin_env_reference`` stays the deciding step, so "ask
    when not sure" is the configurator's call.

    Absent (``{}``) when there is nothing to explore for: no required var, no
    default vault, or no 1Password token. A failed ``op`` is reported as the
    classified ``op_failed`` and never fails the mutation that called this.
    Runs AFTER the registry write and reload, so it cannot delay activation.

    #1024: a name ``manifest`` declares in ``casa.setupProvides`` is neither
    searched for nor listed as unresolved (:func:`_declared_setup_provides`).
    """
    declared = _declared_setup_provides(manifest)
    unresolved = [v for v in required_env_vars
                  if v not in declared and not os.environ.get(v)]
    vault = _default_vault()
    if not unresolved or not vault or not os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"):
        return {}
    queries = _secret_candidate_queries(name, unresolved)
    try:
        return {"secret_candidates": _explore_vault(vault, queries, unresolved)}
    except Exception as exc:  # noqa: BLE001 — the exploration never fails the mutation
        # Terra diff r1 D2: anything the helpers did not classify (a foreign
        # exception, a non-string id) is still reported as a fixed
        # classification. Round 2 E2: only the exception's CLASS is logged;
        # its message can carry what op printed, and so can a traceback.
        logger.warning("secret exploration for %s failed (%s)", name,
                       type(exc).__name__)
        return {"secret_candidates": _op_failed(-1)}


def _explore_vault(vault: str, queries: list[str], unresolved: list[str]) -> dict:
    items: list[dict] = []
    seen: set[str] = set()
    for q in queries:
        listed = _op_item_list(q, vault)
        if "error" in listed:
            return listed
        for it in listed.get("rows", []):
            if it.get("id") in seen or len(items) >= _SECRET_CANDIDATE_MAX_ITEMS:
                continue
            seen.add(it.get("id"))
            got = _op_item_get(str(it.get("id")), vault)
            if "error" in got:
                return got
            doc = got["doc"]
            iid = it.get("id")
            items.append({"matched_query": _matched_term(it.get("title"), [q]),
                          "id": iid if isinstance(iid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", iid) else None,
                          "fields": _project_item_fields(doc)})
    return {"vault": vault, "queries": queries, "items": items,
            "unresolved": unresolved}


# #1095's acknowledgement input, shared by the specialist tools below and the
# plugin mutation tools (#1145). Defined here, before the first tool that
# declares it, because a tool's schema is evaluated when it is decorated.
_ACKNOWLEDGED_SCHEMA = {
    "type": "array", "items": {"type": "string"},
    "description": ("the engagement ids of the open conversations the operator "
                    "confirmed, exactly as a previous call's "
                    "open_conversations_unconfirmed result listed them")}

# #1145 (ruling #1255, option 2): what a plugin change means for a specialist's
# open conversations, one sentence per kind of change. The ONE source for each
# tool description and result that carries it. ``<plugin>`` is the plugin's
# name in a result and "the plugin" in a description; the ruled leading "…" is
# the add/assign sentence's subject, "Its open conversations".
PLUGIN_ADDED_CONVERSATION_NOTICE = (
    "Its open conversations keep the plugins they started with and will not "
    "get <plugin>.")
PLUGIN_REMOVED_CONVERSATION_NOTICE = (
    "Its open conversations keep <plugin> loaded but lose its approvals, so a "
    "protected call asks again and earlier references to it stop working.")
PLUGIN_UPDATED_CONVERSATION_NOTICE = (
    "Its open conversations keep the previous version and lose its approvals.")
_PLUGIN_CONVERSATION_NOTICES = {
    "added": PLUGIN_ADDED_CONVERSATION_NOTICE,
    "removed": PLUGIN_REMOVED_CONVERSATION_NOTICE,
    "updated": PLUGIN_UPDATED_CONVERSATION_NOTICE,
}


def _plugin_notice(kind: str, plugin: str) -> str:
    return _PLUGIN_CONVERSATION_NOTICES[kind].replace("<plugin>", plugin)


def _plugin_change_tool_note(kind: str, *, removal: bool = False) -> str:
    return (" If a specialist this call changes has open conversations, a call "
            "without acknowledged_conversations changes nothing and returns kind "
            "'open_conversations_unconfirmed' with a warning to relay to the "
            "operator verbatim, which says of each such specialist: "
            + _plugin_notice(kind, "the plugin")
            + " Only on the operator's yes, call again with the same arguments"
            + (" (the same erase_data choice)" if removal else "")
            + " plus acknowledged_conversations.")


@tool(
    "plugin_add",
    "Add a plugin to the registry: publish its pinned artifact, install any "
    "system requirements, assign it to targets, then reload + verify. Version "
    "ref may be a tag, a sha, a branch, or the literal 'latest' — the newest "
    "published release tag (GitHub's latest release, else the highest v<semver> "
    "tag), never a branch; the result's resolved_ref names it and the registry "
    "stores it. "
    "is derived from the plugin manifest (never supplied). Targets are "
    "resident: or specialist: roles; for now a plugin cannot be given to a "
    "worker (executor:) — workers use only the plugins Casa ships with them."
    + _plugin_change_tool_note("added"),
    # Sol #15: an explicit JSON Schema — the shorthand {key: type} form marks
    # EVERY key required, so a root-plugin call omitting `subdir` (defaulted by
    # the handler) is rejected by the MCP input validator before the handler
    # runs. `subdir` is the only optional field.
    {"type": "object",
     "properties": {
         "name": {"type": "string"},
         "repo": {"type": "string"},
         "ref": {"type": "string"},
         "subdir": {"type": "string"},
         # C.2 (v0.74.0): the producer's handed-off revision — a tag that
         # moved after the build aborts before activation.
         "expected_revision": {"type": "string"},
         # Sol round-3 M: constrain items so a non-string target (`[1]`) is
         # rejected by the MCP validator, not by an uncaught TARGET_RE.match.
         "targets": {"type": "array", "items": {"type": "string"}},
         "acknowledged_conversations": _ACKNOWLEDGED_SCHEMA},
     "required": ["name", "repo", "ref", "targets"]},
)
@_off_the_ceiling
async def plugin_add(args: dict) -> dict:
    def reached() -> list:
        refusal, state = _plugin_add_local(
            name=args["name"], subdir=args.get("subdir", ""),
            targets=args.get("targets") or [])
        return [] if refusal is not None else _specialist_slugs(state["targets"])

    async with _PLUGIN_TOOLS_LOCK:
        # #1145: warn first when the new plugin reaches a specialist with open
        # conversations; the core (looked up here, so a test's double is used)
        # then runs exactly as before.
        core, ack, seen = await _plugin_gate_phase("plugin_add", "added", args, reached)
        if core is None:
            core = await _to_thread_settled(
                _plugin_add_sync, name=args["name"], repo=args["repo"],
                ref=args["ref"], subdir=args.get("subdir", ""),
                targets=args.get("targets") or [],
                expected_revision=args.get("expected_revision"))
        elif core.get("kind") == OPEN_CONVERSATIONS_PENDING:
            return _result(core)
        if core.get("ok") is not True:
            # Spec §E: the pinned payload shape holds on EVERY path.
            core.setdefault("kind", "unknown")
            core.setdefault("activation_committed", False)
            core.setdefault("runtime_ready", False)
            core.setdefault("verify", {})
            return _result(_after_plugin_change(
                core, "added", args["name"], [], ack, seen))
        seq = await _reload_and_verify_targets(
            core["name"], core["targets"], expect="present")
        core.update(seq)
        published_manifest = core.pop("_published_manifest", None)
        core.update(_resolved_observability(
            core["name"], manifest=published_manifest))
        # Design 2026-09-15 §2.D: the tool explores the default vault for the
        # unresolved secrets; the configurator decides what to wire.
        core.update(await _to_thread_settled(
            _secret_candidates, core["name"],
            list(core.get("required_env_vars") or []),
            manifest=published_manifest))
        # S8: the just-published manifest's recommendations, one row per
        # (requirement, target) with where it stands; the configurator offers.
        core["requirement_candidates"] = await _to_thread_settled(
            _requirement_candidates_for, core["name"], list(core["targets"]),
            manifest=published_manifest)
        return _result(_after_plugin_change(
            core, "added", core["name"], _specialist_slugs(core["targets"]),
            ack, seen))


@tool(
    "plugin_update",
    "Update a registered plugin to a new ref (a tag, sha or branch, or the "
    "literal 'latest' for the newest published release tag — never a branch): "
    "re-publish, install new system "
    "requirements, repoint the registry, reload + verify. A plugin already on "
    "that release returns unchanged:true: no registry change and no reload, and "
    "verify reports its current state. Version derives "
    "from the fetched manifest. Pass expected_revision (the sha the tag must "
    "point at, when the operator gives one) so a tag that moved aborts before "
    "activation. A plugin Casa ships to a worker is not updated here — it is "
    "updated by updating Casa; updating any other plugin makes it the "
    "operator's, so it is no longer part of what Casa ships."
    + _plugin_change_tool_note("updated"),
    {"type": "object",
     "properties": {
         "name": {"type": "string"},
         "new_ref": {"type": "string"},
         "expected_revision": {"type": "string"},
         "acknowledged_conversations": _ACKNOWLEDGED_SCHEMA},
     "required": ["name", "new_ref"]},
)
@_off_the_ceiling
async def plugin_update(args: dict) -> dict:
    def reached() -> list:
        refusal, state = _plugin_update_local(name=args["name"])
        return ([] if refusal is not None
                else _specialist_slugs(state["entry"].get("targets")))

    async with _PLUGIN_TOOLS_LOCK:
        # #1145: warn first when the plugin is assigned to a specialist with
        # open conversations. The revision guard and the rest of the network
        # phase stay in the core, after this: a confirmed call can still be
        # refused there, with nothing committed.
        core, ack, seen = await _plugin_gate_phase(
            "plugin_update", "updated", args, reached)
        if core is None:
            core = await _to_thread_settled(
                _plugin_update_sync, name=args["name"], new_ref=args["new_ref"],
                expected_revision=args.get("expected_revision"))
        elif core.get("kind") == OPEN_CONVERSATIONS_PENDING:
            return _result(core)
        if core.get("ok") is not True:
            # Spec §E: the pinned payload shape holds on EVERY path.
            core.setdefault("kind", "unknown")
            core.setdefault("activation_committed", False)
            core.setdefault("runtime_ready", False)
            core.setdefault("verify", {})
            return _result(_after_plugin_change(
                core, "updated", args["name"], [], ack, seen))
        if core.get("unchanged") is True:
            # Nothing moved, so nothing was activated: no reload, and no
            # open-conversation notice for a change that did not happen. Verify
            # still reports the plugin's real state.
            core["activation_committed"] = False
            core["verify"] = await _to_thread_settled(
                _tool_verify_plugin_state, plugin_name=core["name"])
            core["runtime_ready"] = core["verify"].get("ready") is True
            return _result(_after_plugin_change(
                core, "updated", core["name"], [], ack, seen))
        # A:§3.3 (r1-B8): when the artifact just changed under this plugin,
        # invalidate the OLD artifact's grants/challenges right after the
        # mutation commits, before the first post-commit await (reload). A
        # repoint to the same artifact (another tag on the same commit) keeps
        # them: they are bound to the artifact, which is still the one running.
        if core.get("old_artifact_id") != core.get("artifact_id"):
            _invalidate_lifecycle(artifact_id=core.get("old_artifact_id"))
        seq = await _reload_and_verify_targets(
            core["name"], core["targets"], expect="present")
        core.update(seq)
        published_manifest = core.pop("_published_manifest", None)
        core.update(_resolved_observability(
            core["name"], manifest=published_manifest))
        # Design 2026-09-15 §2.D: the tool explores the default vault for the
        # unresolved secrets; the configurator decides what to wire.
        core.update(await _to_thread_settled(
            _secret_candidates, core["name"],
            list(core.get("required_env_vars") or []),
            manifest=published_manifest))
        return _result(_after_plugin_change(
            core, "updated", core["name"], _specialist_slugs(core["targets"]),
            ack, seen))


@tool(
    "specialist_install_inspect",
    "Fetch a specialist component repo for inspection (non-persistent staging, no CAS write). "
    "Returns the component's mission, default persona, required config/secret names, "
    "dependency-closure availability and the staged facts (slug, root_digest, staged_dir, "
    "receipt_id), or a structured error. It posts nothing: agree the values with the operator, "
    "then call specialist_install_request_approval. Pass mode='upgrade' + target_slug=<slug> "
    "when re-inspecting the SAME repo "
    "for an already-installed specialist (otherwise the collision check refuses it).",
    {"type": "object", "properties": {
        "repo": {"type": "string"}, "ref": {"type": "string"},
        "subdir": {"type": "string"}, "expected_revision": {"type": "string"},
        "mode": {"type": "string", "enum": ["install", "upgrade"]},
        "target_slug": {"type": "string"}},
     "required": ["repo", "ref"]},
)
@_off_the_ceiling
async def specialist_install_inspect(args: dict) -> dict:
    from specialist_install import SpecialistInstallError, inspect_specialist_repo

    try:
        result = await asyncio.to_thread(
            inspect_specialist_repo, args["repo"], args["ref"], subdir=args.get("subdir", ""),
            expected_revision=args.get("expected_revision"),
            mode=args.get("mode", "install"), target_slug=args.get("target_slug"),
        )
    except SpecialistInstallError as exc:
        return _result({"ok": False, "kind": exc.kind, "detail": exc.detail})

    ok_payload = {
        "ok": True, "component_id": result.component_id, "version": result.version,
        "slug": result.slug, "component_checksum": result.component_checksum,
        "root_digest": result.root_digest,
        "mission": result.mission, "default_persona_ref": result.default_persona_ref,
        "required_config_names": list(result.required_config_names),
        "required_secret_names": list(result.required_secret_names),
        "dependencies": [{"kind": d.kind, "identifier": d.identifier, "available": d.available}
                          for d in result.dependencies],
        "staged_dir": str(result.staged_dir),
        # INV-SPEC-018 (#1007): the ref actually fetched — the release tag
        # when the caller passed ``latest`` — for the configurator's commit
        # message and completion, exactly as plugin_add reports resolved_ref.
        "resolved_ref": result.resolved_ref,
        # Fix round 1 (task-12): specialist_install_commit/specialist_upgrade
        # REQUIRE args["receipt_id"] (see their "required" schemas below) —
        # without it in this payload every real install/upgrade dead-ends at
        # receipt_required. receipt_digest is included too since it is part
        # of the consent identity binding.
        "receipt_id": result.receipt_id, "receipt_digest": result.receipt_digest,
        # Tool-payload mirror of the plugin blocks the approval card
        # enumerates (render_install_consent_message,
        # specialist_install_consent.py) — lets the calling LLM narrate what
        # the approval covers without re-deriving it from prose.
        "plugins": [
            {"scoped_name": row.scoped_name, "manifest_name": row.manifest_name,
             "version": row.version, "mcp_servers": list(row.mcp_servers),
             "protected_tools": list(row.protected_tools),
             # #994: `env_names` is what the withhold gate holds on — the
             # install recipe asks the operator for these. `exempt_env_names`
             # (defaulted or setup-provisioned) is disclosure only.
             "env_names": list(row.env_names),
             "exempt_env_names": list(row.exempt_env_names)}
            for row in result.plugin_resolutions
        ],
    }

    ok_payload["next"] = ("agree the values with the operator, then call "
                          "specialist_install_request_approval")
    return _result(ok_payload)


def _requester_name(origin: dict) -> str:
    """The display name of the agent whose turn made a request, for the
    approval card's title: *origin*'s ``role`` (in a configurator run, the
    agent that delegated to it), else ``"Casa"``."""
    from authz_grants import _resolve_display_name
    role = str((origin or {}).get("role") or "")
    cfg = _agent_role_map.get(role) if role else None
    if cfg is None:
        return "Casa"
    name = getattr(getattr(cfg, "character", None), "name", None)
    return _resolve_display_name(name, role)


def _install_card_facts(checked: Any, staged_dir: Path, *, slug: str, mode: str) -> dict:
    """What an install or update card is told beyond the inspection the
    governed tools work on: the installed version an update replaces, and each
    bundled plugin's protected tools with the summaries its staged manifest
    declares. Presentation only: none of it reaches the consent text, the
    receipt or the approval's identity. A read; whatever cannot be read is left
    out (a plugin's tools then by name only)."""
    import plugin_store
    from personality_binding import InstanceDir
    from specialist_install import parse_component_root, sourced_plugin_tree
    component = checked.component
    out: dict = {}
    if mode == "upgrade":
        try:
            active = InstanceDir(_SPECIALISTS_DIR / slug).active()
            if active is not None:
                out["from_version"] = parse_component_root(active.root)[1]
        except (ValueError, OSError) as exc:
            logger.info("approval card: installed version of %s unreadable (%s)", slug, exc)
    deps = {d.identifier: d for d in getattr(component, "dependencies", ()) or ()
            if getattr(d, "source", None) is not None}
    tools_by_plugin: dict = {}
    for row in checked.receipt.plugins:
        try:
            tree = sourced_plugin_tree(staged_dir, deps[row.identifier])
            manifest = json.loads((tree / ".claude-plugin" / "plugin.json")
                                  .read_text(encoding="utf-8"))
            tools_by_plugin[row.scoped_name] = [
                {"name": t["name"], "summary": t.get("summary")}
                for t in plugin_store.manifest_protected_tools(manifest)]
        except Exception as exc:  # noqa: BLE001 — the card then names the tools only
            logger.info("approval card: protected tool summaries of %s unreadable (%s: %s)",
                        row.scoped_name, type(exc).__name__, exc)
    out["protected_tools"] = tools_by_plugin
    return out


def _configurator_run_key() -> str:
    """The configurator run this tool runs in (its delegation id), or ``""``."""
    origin = _snapshot_origin() or {}
    return str(origin.get("_delegation_id") or "")


#: Where the installed specialists live; the approval request reads their
#: active and pending settings here (tests point it elsewhere).
_SPECIALISTS_DIR = Path("/config/specialists")


def _inspection_from_checked(checked: Any, staged_dir: Path) -> Any:
    """The inspection the governed tools work on, rebuilt from the staged bytes
    and the trusted receipt a resume check validated — never from the
    caller's arguments. It carries the Casa tools the role grants, so the
    consent text names them as the inspect tool's does."""
    from specialist_install import InspectionResult, role_tool_grants
    receipt, component = checked.receipt, checked.component
    return InspectionResult(
        component_id=component.component_id, version=component.version, slug=component.slug,
        component_checksum=component.checksum, root_digest=checked.root_digest,
        mission=str(component.role.role.get("mission", "")),
        default_persona_ref=component.default_persona_ref,
        default_persona_checksum=component.default_persona_checksum,
        required_config_names=(), required_secret_names=(), dependencies=checked.dependencies,
        staged_dir=staged_dir, receipt_id=receipt.receipt_id,
        receipt_digest=receipt.receipt_digest, plugin_resolutions=receipt.plugins,
        role_tool_grants=role_tool_grants(component))


def _run_approval() -> "dict | None":
    """The approval Casa started this run for (the origin's ``_approval``),
    or ``None``."""
    import approval_records
    value = (_snapshot_origin() or {}).get(approval_records.APPROVAL_KEY)
    return value if isinstance(value, dict) else None


def _install_fence(kind: str, inspection: Any, approval: "dict | None"
                   ) -> "tuple[dict | None, dict | None, str]":
    """A governed specialist call's entry check: ``(refusal, approved values,
    consent identity)``. A read; the attempt inside the transaction uses the
    approval."""
    import approval_records
    from specialist_install_consent import identity_for_inspection
    values = approval_records.approved_values(approval)
    identity = identity_for_inspection(inspection, values=values) if values is not None else ""
    return (approval_records.fence(approval, kind=kind, consent_identity=identity),
            values, identity)


def _upgrade_changes_nothing(inspection: Any, slug: str, effective: dict,
                             secrets: list) -> bool:
    """Whether upgrading *slug* to *inspection* with the *effective* settings
    would change nothing: the active install has the same root and the same
    settings, and no secret names are involved on either side (Casa keeps no
    record of the installed ones, so it cannot show they match). A read."""
    from personality_binding import InstanceDir
    from specialist_install import component_root_string, load_specialist_component
    if secrets:
        return False
    staged = Path(inspection.staged_dir)
    try:
        component = load_specialist_component(staged, staged / "manifest.json")
        active = InstanceDir(_SPECIALISTS_DIR / slug).active()
    except (ValueError, OSError):
        return False
    if component.config_schema.get("secret_names") or active is None:
        return False
    root = component_root_string(component_id=inspection.component_id,
                                 version=inspection.version,
                                 component_checksum=inspection.root_digest)
    return active.root == root and dict(active.config_snapshot or {}) == effective


async def _supersede_card(record: Any) -> None:
    """Edit a replaced request's card to say so and settle it, so a later tap
    on it answers "replaced". A card still being written is settled too, so it
    is never posted. Never raises."""
    if record is None:
        return
    try:
        import inspect as _inspect
        from specialist_install_consent import supersede_card
        done = supersede_card(record)
        if _inspect.isawaitable(done):
            await done
    except Exception:  # noqa: BLE001 — the newer card stands either way
        logger.warning("replaced approval card not edited (%s)", record.rid[:8],
                       exc_info=True)


async def _post_approval_card(record: Any, post: "Callable[..., Any]", *, what: str,
                              continuation: str) -> dict:
    """Post Casa's approval card for the new *record* through *post* (the
    card function of its kind); its Approve carries the configurator on with
    *continuation*."""
    import approval_records
    channel, address, problem = _casa_card_target()
    if problem is not None:
        return {"ok": False, "kind": "consent_channel_unavailable",
                "detail": (_CASA_CARD_PROBLEMS[problem]
                           + f" — the approval card for {what} cannot be posted; fix the "
                           "operator DM channel and ask again")}
    try:
        handle = post(coordinator=CHALLENGES, channel=channel, chat_id=address.chat_id,
                      operator_id=address.chat_id, record=record, continuation=continuation,
                      requester=_configurator_run_key())
    except Exception as exc:  # noqa: BLE001 — structured, never a silent ok:true
        logger.exception("approval card for %s failed to post", what)
        return {"ok": False, "kind": "consent_prompt_failed",
                "detail": f"{type(exc).__name__}: {exc}"}
    failure = await _settle_install_consent_post(handle)
    # The card records where it landed and, when a newer request replaced this
    # one while it was posting, edits itself to say so at once. One replaced
    # while its card was still being written is never posted: its question
    # settled first, which reads as inactive.
    now = approval_records.get(record.rid)
    replaced = now is not None and now.state == "superseded"
    if failure is not None and not (replaced
                                    and failure.get("kind") == "consent_prompt_inactive"):
        return failure
    if replaced:
        return {"ok": False, "kind": "approval_superseded",
                "detail": ("a newer request replaced this one while its card was "
                           "posting; the newer card is the one the operator answers")}
    return {"ok": True, "approval": "requested",
            "next": "stop; Casa continues you after the operator approves"}


def _approval_notice(what: str) -> "Callable[[str], Awaitable[None]]":
    """The ``on_outcome`` of a continuation started without a card to edit: the
    operator hears once when it could not be started."""
    async def _told(outcome: str) -> None:
        if outcome in ("busy", "failed"):
            import casa_core
            await casa_core.operator_notify(
                _channel_manager,
                f"The configurator could not carry on with the approved {what}; ask Ellen "
                "again — the approval is kept.")
    return _told


@tool(
    "specialist_install_request_approval",
    "Ask the operator to approve installing or upgrading an inspected specialist with the "
    "settings you agreed with them. Pass slug, root_digest, staged_dir and receipt_id exactly as "
    "specialist_install_inspect returned them, mode ('install' or 'upgrade'), the config values "
    "and the secret names provided. Casa computes the settings the install will end up with "
    "(including those kept from the current install) and posts the card; after Approve Casa "
    "continues you with exactly those values. Results: approval 'requested' (stop; Casa "
    "continues you after Approve), 'already_approved' (stop; Casa continues you once this run "
    "ends), 'in_progress' (this run is the one Casa started for the approval: make the call), "
    "'not_needed' (an upgrade that would change nothing: tell the operator it is already done).",
    {"type": "object", "properties": {
        "slug": {"type": "string"}, "root_digest": {"type": "string"},
        "staged_dir": {"type": "string"}, "receipt_id": {"type": "string"},
        "mode": {"type": "string", "enum": ["install", "upgrade"]},
        "config": {"type": "object", "additionalProperties": {"type": "string"}},
        "secret_names_provided": {"type": "array", "items": {"type": "string"}}},
     "required": ["slug", "root_digest", "staged_dir", "receipt_id", "mode"]},
)
@_off_the_ceiling
async def specialist_install_request_approval(args: dict) -> dict:
    import approval_records
    import configurator_continue
    from specialist_install import (SpecialistInstallError, preview_effective_config,
                                    validate_resume_inputs)
    from specialist_install_consent import identity_for_inspection

    mode = args.get("mode") or "install"
    if mode not in ("install", "upgrade"):
        return _result({"ok": False, "kind": "invalid_mode",
                        "detail": "mode is 'install' or 'upgrade'"})
    slug = str(args.get("slug") or "")
    staged_dir = Path(str(args.get("staged_dir") or ""))
    checked = validate_resume_inputs(
        staged_dir=staged_dir, receipt_id=args.get("receipt_id"),
        root_digest=args.get("root_digest"), expect_slug=slug if mode == "install" else None)
    if not checked.ok:
        return _result({"ok": False, "kind": checked.kind, "detail": checked.detail})
    inspection = _inspection_from_checked(checked, staged_dir)
    kind = "specialist_upgrade" if mode == "upgrade" else "specialist_install"
    config = {str(k): v for k, v in dict(args.get("config") or {}).items()}
    secrets = sorted({str(n) for n in (args.get("secret_names_provided") or [])})
    try:
        effective = await asyncio.to_thread(
            preview_effective_config, kind, inspection, slug=slug, config=config,
            secret_names_provided=secrets, specialists_dir=_SPECIALISTS_DIR)
    except SpecialistInstallError as exc:
        return _result({"ok": False, "kind": exc.kind, "detail": exc.detail})
    values = {"config": effective, "secrets": secrets}
    identity = identity_for_inspection(inspection, values=values)
    try:
        card_facts = await asyncio.to_thread(
            _install_card_facts, checked, staged_dir, slug=slug, mode=mode)
    except Exception:  # noqa: BLE001 — the card is told less, the request stands
        logger.warning("approval card facts for specialist:%s unavailable", slug, exc_info=True)
        card_facts = {}
    origin = _snapshot_origin() or {}
    requester_name = _requester_name(origin)
    # #1565: only the operator's own server-stamped words; any other root (an
    # event wake, a narration) records none, so its run is never told them as the
    # operator's
    asked = (operator_words(origin) or "")[:approval_records.ASKED_MAX]
    carrying = origin.get(approval_records.APPROVAL_KEY)
    carrying_rid = str(carrying.get("rid") or "") if isinstance(carrying, dict) else ""
    async with _plugin_tools_guard():
        if mode == "upgrade" and await _to_thread_settled(
                _upgrade_changes_nothing, inspection, slug, effective, secrets):
            return _result({"ok": True, "approval": "not_needed",
                            "detail": (f"already at {inspection.version} with these settings; "
                                       "nothing to change")})
        record, outcome, superseded = approval_records.request(
            kind, f"{inspection.component_id}:{inspection.slug}", identity, values,
            asked=asked, carrying_rid=carrying_rid)
    continuation = configurator_continue.continuation_text(
        kind, slug=inspection.slug, component_id=inspection.component_id,
        version=inspection.version, root_digest=inspection.root_digest,
        staged_dir=str(inspection.staged_dir), receipt_id=inspection.receipt_id)
    if outcome == "in_progress":
        return _result({"ok": True, "approval": "in_progress",
                        "next": "this run is the one Casa started for this approval; make the call"})
    if outcome == "already_approved":
        configurator_continue.start(
            continuation, key=record.rid, approval=record,
            on_outcome=_approval_notice(f"{mode} of specialist:{inspection.slug}"))
        return _result({"ok": True, "approval": "already_approved",
                        "next": "stop; Casa continues you once this run ends"})
    await _supersede_card(superseded)
    from specialist_install_consent import prompt_specialist_install_consent

    def _post(**kw):
        return prompt_specialist_install_consent(inspection_facts=inspection,
                                                 submitted_config=config,
                                                 requester_name=requester_name,
                                                 **card_facts, **kw)
    return _result(await _post_approval_card(
        record, _post, what=f"specialist:{inspection.slug}", continuation=continuation))

# Round-5b: bound on awaiting the consent keyboard's settled post. The
# coordinator's setup driver only posts (the operator tap resolves later,
# elsewhere), so this normally settles in one Telegram round-trip; the bound
# exists so a wedged channel yields a structured failure, not a hung tool.
_INSTALL_CONSENT_POST_TIMEOUT_S = 30.0


async def _settle_install_consent_post(handle) -> "dict | None":
    """Verify an install-consent keyboard actually posted (round-5b, shared
    by specialist_install_inspect / persona_install_inspect).

    Returns ``None`` when the keyboard is live ("posted"), else the
    structured ok:false payload for the precise failure: registration
    refused (oversized challenge), Telegram delivery failure, a request that
    settled terminally before/while posting, or a post that never settled
    within the bound. ``settled_post`` shields the coordinator-owned driver,
    so the timeout here never cancels the post itself."""
    refused = getattr(handle, "refused", None)
    if refused is not None:
        return {"ok": False, "kind": "consent_prompt_refused",
                "detail": f"consent challenge registration refused: {refused}"}
    try:
        settled = await asyncio.wait_for(
            handle.settled_post(), timeout=_INSTALL_CONSENT_POST_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"ok": False, "kind": "consent_post_unsettled",
                "detail": (f"consent keyboard post did not settle within "
                           f"{_INSTALL_CONSENT_POST_TIMEOUT_S:.0f}s — check the "
                           "Telegram channel, then re-run the inspect tool")}
    if settled == "posted":
        return None
    if settled == "delivery_failed":
        return {"ok": False, "kind": "consent_delivery_failed",
                "detail": ("Telegram delivery of the consent keyboard failed — "
                           "check the operator DM channel, then re-run the "
                           "inspect tool")}
    return {"ok": False, "kind": "consent_prompt_inactive",
            "detail": ("the consent request settled terminally before the "
                       "keyboard could post (TTL expiry or reset) — re-run "
                       "the inspect tool to be prompted again")}


# ---------------------------------------------------------------------------
# #1095: a change to a specialist that has open conversations warns first;
# removing a specialist closes them
# ---------------------------------------------------------------------------
#
# A persona apply, an upgrade, a rollback and an uninstall commit on the call,
# so the warning can only precede the change as a PENDING-FIRST result: the
# call lists the specialist's open conversations and changes nothing, the
# configurator relays the warning verbatim and asks, and on yes calls again
# with `acknowledged_conversations` naming them. Nothing about the
# acknowledgement is kept server-side — the ids live in the configurator's
# context, exactly as INV-PERS-018's notice lives in the tool result.

# Ruling-1095-4's sentence, verbatim. The ONE source for every tool
# description and result that carries it (pinned by
# tests/test_pin_1095_warn_then_act.py), so no entry point can drift.
# #1409: the ruling named the default persona; the sentence names the
# CONFIGURED assistant through ``specialist_open_conversation_notice()``.
# Tool descriptions are built at import, before any persona is known, so
# they carry the neutral "the assistant".
SPECIALIST_OPEN_CONVERSATION_NOTICE_TEMPLATE = (
    "When it resumes, it picks up Casa's updated settings, but it keeps its "
    "personality and the plugin versions it started with. To get everything "
    "new, cancel it (with its Cancel button, or ask {assistant}) and start it "
    "again."
)


def specialist_open_conversation_notice() -> str:
    """Ruling-1095-4's sentence naming the configured assistant (#1409)."""
    return SPECIALIST_OPEN_CONVERSATION_NOTICE_TEMPLATE.format(
        assistant=persona_name())


SPECIALIST_OPEN_CONVERSATION_NOTICE = specialist_open_conversation_notice()
SPECIALIST_UNINSTALL_CONVERSATION_NOTICE = (
    "Uninstalling it closes each of these conversations once the specialist is "
    "removed; a turn still running in one of them is stopped."
)
OPEN_CONVERSATIONS_PENDING = "open_conversations_unconfirmed"
_ORDINARY_CHANGE_TOOL_NOTE = (
    " If the specialist has open conversations, a call without "
    "acknowledged_conversations changes nothing and returns kind "
    "'open_conversations_unconfirmed' with a warning to relay to the operator "
    "verbatim, which says of each open conversation: "
    + SPECIALIST_OPEN_CONVERSATION_NOTICE
    + " Only on the operator's yes, call again with the same arguments plus "
    "acknowledged_conversations.")
_UNINSTALL_TOOL_NOTE = (
    " If the specialist has open conversations, a call without "
    "acknowledged_conversations naming them all removes nothing (it only "
    "withdraws a pending erase-data question) and returns kind "
    "'open_conversations_unconfirmed' with a warning to relay verbatim: "
    + SPECIALIST_UNINSTALL_CONVERSATION_NOTICE
    + " Only on the operator's yes, call again with the same erase_data choice "
    "plus acknowledged_conversations.")


def _open_specialist_engagements(slug: str) -> list:
    """The specialist's open conversations: its specialist-kind engagements
    that are active or idle — delegated conversations, background jobs and
    launches still in flight. Executors (the configurator itself) and plugin
    jobs are another kind and never listed. The registry is set at boot before
    any tool can be called; unset (a unit harness) reads as none open."""
    reg = _engagement_registry
    if reg is None:
        return []
    return [r for r in reg.active_and_idle()
            if r.kind == "specialist" and r.role_or_type == slug]


def specialist_roles_with_open_engagements() -> set:
    """Every specialist role that has an open specialist-kind engagement."""
    reg = _engagement_registry
    if reg is None:
        return set()
    return {r.role_or_type for r in reg.active_and_idle() if r.kind == "specialist"}


def _acknowledged(args: dict) -> "tuple[list[str] | None, dict | None]":
    """``(ids, None)`` — ``ids`` None when no acknowledgement was given — or
    ``(None, refusal)`` for a malformed one, refused before anything runs."""
    raw = args.get("acknowledged_conversations")
    if raw is None:
        return None, None
    if not isinstance(raw, list) or not all(isinstance(i, str) for i in raw):
        return None, {"ok": False, "kind": "invalid_argument",
                      "detail": ("acknowledged_conversations must be a list of the "
                                 "engagement ids a previous warning listed")}
    return list(dict.fromkeys(raw)), None


def _names_this_specialist(ack: "list | None", slug: str) -> bool:
    """True when the acknowledgement names at least one engagement of *slug*
    the registry knows — open or since closed — i.e. ids a warning could have
    listed. A made-up id, or another specialist's, acknowledges nothing (diff
    review r2: any non-empty list used to let an ordinary change through)."""
    reg = _engagement_registry
    if not ack or reg is None:
        return False
    for i in ack:
        rec = reg.get(i)
        if rec is not None and rec.kind == "specialist" and rec.role_or_type == slug:
            return True
    return False


def _operator_user_id() -> "int | None":
    """The operator's Telegram user id (one per deployment, the same on
    every bot), or ``None`` without Telegram or a valid operator chat."""
    transport = _transport()
    try:
        return transport.operator_user_id() if transport is not None else None
    except Exception:  # noqa: BLE001 — the identity only refines a line
        return None


def _conversation_row(rec, operator_id: "int | None") -> dict:
    task = " ".join(str(rec.task or "").split())
    if len(task) > 80:
        task = task[:79] + "…"
    job = bool((rec.origin or {}).get("job"))
    line = f"{task or '(no task)'}" + (" (background job)" if job else "")
    started_by = (rec.origin or {}).get("user_id")
    if (started_by not in (None, "") and operator_id is not None
            and str(started_by) != str(operator_id)):
        line += " — started by someone else"
    return {"engagement_id": rec.id, "task": task,
            "background_job": job, "line": line}


def _open_conversations_pending(tool: str, slug: str, recs: list, *,
                                removal: bool, withdrew: bool = False) -> dict:
    op = _operator_user_id()
    rows = [_conversation_row(r, op) for r in recs]
    n = len(rows)
    notice = (SPECIALIST_UNINSTALL_CONVERSATION_NOTICE if removal
              else specialist_open_conversation_notice())
    warning = (f"The specialist {slug} has {n} open conversation"
               f"{'' if n == 1 else 's'}:\n"
               + "\n".join(f"- {row['line']}" for row in rows) + "\n" + notice)
    ids = [row["engagement_id"] for row in rows]
    detail = ("Nothing was changed. " if not withdrew else
              "Nothing was removed; the pending erase-data question was withdrawn. ")
    detail += ("Relay `warning` to the operator verbatim and ask whether to go "
               f"ahead. On yes, call {tool} again with the same arguments"
               + (" (the same erase_data choice)" if removal else "")
               + f" plus acknowledged_conversations={ids!r}; on no, stop — a "
               "declined warning voids these ids.")
    return {"ok": False, "kind": OPEN_CONVERSATIONS_PENDING, "slug": slug,
            "conversations": rows, "warning": warning, "detail": detail}


def _ordinary_change_gate(tool: str, slug: str,
                          args: dict) -> "tuple[dict | None, list | None, set]":
    """The warn-first check of an ORDINARY change (persona, upgrade, rollback):
    ``(refusal, acknowledgement, seen)``. While conversations are open a call
    with no acknowledgement is refused with the warning; any acknowledgement
    lets it commit — a conversation opened after the operator confirmed is
    named in the result, never a reason to refuse (ruling-1095-3). An
    acknowledgement counts only when it names an engagement of this specialist
    (``_names_this_specialist``). ``seen`` is what the result does NOT name
    afterwards."""
    ack, bad = _acknowledged(args)
    if bad is not None:
        return bad, None, set()
    recs = _open_specialist_engagements(slug)
    if recs and not _names_this_specialist(ack, slug):
        return _open_conversations_pending(tool, slug, recs, removal=False), None, set()
    return None, ack, (set(ack) if ack else {r.id for r in recs})


def _after_ordinary_change(out: Any, slug: str, ack: "list | None",
                           seen: set) -> Any:
    """Echo the acknowledgement (the pending-configuration follow-up and the
    kept re-run carry it), and name each conversation open after a committed
    change that the operator was not told about."""
    if not isinstance(out, dict):
        return out
    if ack:
        out.setdefault("acknowledged_conversations", list(ack))
    if out.get("ok") is True:
        new = [r for r in _open_specialist_engagements(slug) if r.id not in seen]
        if new:
            op = _operator_user_id()
            key = "opened_after_confirmation" if ack else "opened_while_this_change_ran"
            out[key] = [_conversation_row(r, op) for r in new]
            out["open_conversation_notice"] = specialist_open_conversation_notice()
    return out


# #1145: the plugin mutation tools warn the same way, for every specialist the
# change reaches — one call can reach several (an update or a removal reaches
# every specialist the plugin is assigned to). Built from the per-specialist
# pieces above, which stay as they are; the sentence is the one ruled for the
# kind of change (#1255), and an acknowledgement naming an engagement of ANY
# reached specialist confirms the change (#1263) — the rule above, applied to
# each reached specialist in turn.

def _specialist_slugs(targets: Any) -> list:
    """The specialists among plugin *targets*, in order, each once."""
    out: list = []
    for t in targets or []:
        if isinstance(t, str) and t.startswith("specialist:"):
            slug = t.partition(":")[2]
            if slug and slug not in out:
                out.append(slug)
    return out


def _plugin_conversation_blocks(by_slug: dict, kind: str,
                                plugin: str) -> "tuple[list, str]":
    """One block per specialist — its open conversations, then the sentence for
    this kind of change — so the sentence's "Its" always names one specialist."""
    op = _operator_user_id()
    notice = _plugin_notice(kind, plugin)
    rows: list = []
    blocks: list = []
    for slug, recs in by_slug.items():
        mine = [{**_conversation_row(r, op), "slug": slug} for r in recs]
        rows.extend(mine)
        n = len(mine)
        blocks.append(f"The specialist {slug} has {n} open conversation"
                      f"{'' if n == 1 else 's'}:\n"
                      + "\n".join(f"- {row['line']}" for row in mine)
                      + "\n" + notice)
    return rows, "\n".join(blocks)


def _plugin_change_gate(tool: str, kind: str, plugin: str, slugs: list,
                        ack: "list | None", *, removal: bool = False,
                        withdrew: bool = False) -> "tuple[dict | None, set]":
    """The warn-first check of a plugin change reaching *slugs*:
    ``(refusal, seen)``. While any of them has open conversations, a call whose
    acknowledgement names no engagement of any of them is refused with the
    warning; otherwise it goes ahead, and ``seen`` is what the result does NOT
    name afterwards (``_ordinary_change_gate``'s rule)."""
    by_slug = {}
    for slug in slugs:
        recs = _open_specialist_engagements(slug)
        if recs:
            by_slug[slug] = recs
    if by_slug and not any(_names_this_specialist(ack, s) for s in slugs):
        rows, warning = _plugin_conversation_blocks(by_slug, kind, plugin)
        ids = [row["engagement_id"] for row in rows]
        detail = ("Nothing was changed. " if not withdrew else
                  "Nothing was removed; the pending erase-data question was withdrawn. ")
        detail += ("Relay `warning` to the operator verbatim and ask whether to go "
                   f"ahead. On yes, call {tool} again with the same arguments"
                   + (" (the same erase_data choice)" if removal else "")
                   + f" plus acknowledged_conversations={ids!r}; on no, stop — a "
                   "declined warning voids these ids.")
        # INV-TOOL-003's pinned envelope is set here, for every plugin tool:
        # plugin_remove's refusal leaves through the erase gate, which never
        # reaches the handlers' own envelope defaults.
        return {"ok": False, "kind": OPEN_CONVERSATIONS_PENDING,
                "slug": next(iter(by_slug)), "slugs": list(by_slug),
                "conversations": rows, "warning": warning, "detail": detail,
                "activation_committed": False, "runtime_ready": False,
                "verify": {}}, set()
    seen = set(ack) if ack else {r.id for recs in by_slug.values() for r in recs}
    return None, seen


def _after_plugin_change(out: Any, kind: str, plugin: str, slugs: list,
                         ack: "list | None", seen: set) -> Any:
    """``_after_ordinary_change`` for a plugin change: echo the acknowledgement,
    and on a committed change — ready or not (INV-TOOL-004) — name each open
    conversation of the specialists it reached that the operator was not told
    about, with the sentence for this kind of change."""
    if not isinstance(out, dict):
        return out
    if ack:
        out.setdefault("acknowledged_conversations", list(ack))
    if out.get("ok") is True or out.get("activation_committed") is True:
        by_slug = {}
        for slug in slugs:
            new = [r for r in _open_specialist_engagements(slug) if r.id not in seen]
            if new:
                by_slug[slug] = new
        if by_slug:
            rows, text = _plugin_conversation_blocks(by_slug, kind, plugin)
            key = "opened_after_confirmation" if ack else "opened_while_this_change_ran"
            out[key] = rows
            out["open_conversation_notice"] = text
    return out


async def _plugin_gate_phase(tool: str, kind: str, args: dict,
                             reached: Callable[[], list],
                             ) -> "tuple[dict | None, list | None, set]":
    """The handler's warn-first step, under ``_PLUGIN_TOOLS_LOCK``:
    ``(refusal, acknowledgement, seen)``. *reached* runs the tool's LOCAL guards
    (one source with its core) in a thread and returns the specialists the
    change reaches — none when a guard would refuse or the call changes nothing,
    so the core then returns its own result as it always did. The check itself
    runs here, on the event loop, which owns the engagement registry. With no
    specialist conversation open anywhere there is nothing to warn about, and
    the guards are not run twice."""
    ack, bad = _acknowledged(args)
    if bad is not None:
        return bad, None, set()
    if not specialist_roles_with_open_engagements():
        return None, ack, set(ack or ())
    slugs = await _to_thread_settled(reached)
    refusal, seen = _plugin_change_gate(tool, kind, args["name"], slugs, ack)
    return refusal, ack, seen


def _engagement_driver_for(rec) -> Any:
    try:
        import agent as agent_mod
        return getattr(agent_mod, "active_engagement_driver", None)
    except Exception:  # noqa: BLE001
        return None


async def close_specialist_engagements(slug: str, *, reason: str,
                                       acknowledged: "list | None" = None) -> list:
    """Close every open conversation of *slug* through the terminal funnel
    (outcome ``cancelled``; the funnel ends it and notifies the engager). Read now, never from a copied list. Awaited inline, one at a
    time: the funnel takes no reload or plugin-tools lock, so a caller holding
    either may await this. One row per conversation —
    ``closed`` / ``already_closed`` / ``close_failed`` — and one failure never
    stops the rest."""
    rows = []
    for rec in _open_specialist_engagements(slug):
        row = {"engagement_id": rec.id}
        if acknowledged is not None:
            row["opened_after_confirmation"] = rec.id not in acknowledged
        try:
            fin = await _finalize_engagement(
                rec, outcome="cancelled", text=reason,
                next_steps=[], driver=_engagement_driver_for(rec))
        except Exception:  # noqa: BLE001 — reported per conversation
            logger.exception("closing engagement %s of %s failed", rec.id[:8], slug)
            fin = None
        if fin is FinalizeResult.ALREADY_TERMINAL:
            row["outcome"] = "already_closed"
        elif fin:
            row["outcome"] = "closed"
        else:
            row["outcome"] = "close_failed"
        rows.append(row)
    return rows


async def close_disabled_specialist_engagements(role: str) -> list:
    """The disable close (ruling-1095-3: closed when the disable takes effect,
    and the operator told at once): the funnel ends each one; a close
    that failed leaves that conversation open, so the operator is told here.
    Shared by the reload's retirement step and the boot pass."""
    rows = await close_specialist_engagements(
        role, reason=f"Closed: the specialist {role} was disabled.")
    failed = [r for r in rows if r["outcome"] == "close_failed"]
    if failed:
        names = ", ".join(str(r["engagement_id"]) for r in failed)
        text = (f"The specialist {role} was disabled, but Casa could not close "
                f"{len(failed)} of its open conversation"
                f"{'' if len(failed) == 1 else 's'}: {names}. Casa retries on the "
                "next reload of the agents.")
        try:
            import casa_core
            await casa_core.operator_notify(_channel_manager, text)
        except Exception:  # noqa: BLE001 — the failure is also named and retried
            logger.warning("could not tell the operator about %s's failed closes",
                           role, exc_info=True)
    return rows


def _pending_resume_inputs(inspection, receipt, *, tool_name: str) -> dict:
    """#929 (INV-SPEC-015): the arguments a re-commit of a pending-configuration
    candidate takes, as this call validated them — never as the caller asserted
    them (`component_id`/`version` are read from the staged manifest; only the
    slug and the root digest are checked against the arguments) — AND the name
    of the tool that takes them.

    A pending outcome retains its receipt and its staging tree, and the SAME
    `(inspection, receipt)` pair re-commits to `active` — but a fresh
    re-inspect refuses the now-occupied slug (`slug_collision`, and
    `no_active_tuple` in upgrade mode for a first install), so an engagement
    that was not handed these five values had no supported route back. The
    args schema is unchanged: `staged_dir` and `receipt_id` remain REQUIRED,
    and the receipt-to-inspection binding is untouched. `casactl specialist
    status` names the same five for a slug holding a desired candidate.

    `tool` is the sixth member and it is not decoration. A pending UPGRADE
    leaves the ACTIVE tuple in place, and `commit_specialist_install` refuses
    an active tuple outright (`_refuse_if_active_present` ->
    `concurrent_mutation`), so the five that resume a pending upgrade go to
    `specialist_upgrade` and the five that resume a pending first install go
    to `specialist_install_commit`. Naming five values and not the route that
    takes them sends a later engagement to a tool that refuses — the one
    outcome this disclosure exists to prevent. Here the answer is free: it is
    the tool the caller just invoked.
    """
    return {"receipt_id": receipt.receipt_id, "staged_dir": str(inspection.staged_dir),
            "component_id": inspection.component_id, "version": inspection.version,
            "root_digest": inspection.root_digest, "tool": tool_name}


def _activation_error_disclosure(instance) -> dict:
    """The library's own reason a commit did not activate cleanly — an upgrade
    whose prompt compile failed (state "error"), a pending-configuration
    candidate's missing names, operational files left to reconcile — passed
    through verbatim. Without it the configurator saw state "error" and
    nothing to tell the operator (2026-11-01)."""
    error = instance.last_activation_error
    return {"last_activation_error": error} if error else {}


@tool(
    "specialist_install_commit",
    "Persist an INSPECTED specialist component to CAS, compile, and activate it — only in the "
    "run Casa started after the operator approved it (specialist_install_request_approval), "
    "with exactly the approved values; one approval is one attempt. Supply the component_id/"
    "version/root_digest/slug exactly as returned by specialist_install_inspect.",
    {"type": "object", "properties": {
        "component_id": {"type": "string"}, "version": {"type": "string"},
        "root_digest": {"type": "string"}, "slug": {"type": "string"},
        "staged_dir": {"type": "string"}, "receipt_id": {"type": "string"},
        "config": {"type": "object", "additionalProperties": {"type": "string"}},
        "secret_names_provided": {"type": "array", "items": {"type": "string"}}},
     "required": ["component_id", "version", "root_digest", "slug", "staged_dir", "receipt_id"]},
)
@_off_the_ceiling
async def specialist_install_commit(args: dict) -> dict:
    import approval_records
    from specialist_install import (
        SpecialistInstallError, commit_specialist_install, validate_resume_inputs,
    )
    from specialist_install_consent import SpecialistInstallAckStore
    import specialist_bundle_journal
    import specialist_install as specialist_install_mod
    import specialist_receipt

    # Task 10: the trusted source receipt is loaded by opaque id ONLY — never
    # from caller-supplied coordinates. Required for EVERY install (plugin-less
    # components included); a missing/unloadable id fails closed. That load,
    # and every other pre-transaction check, now lives in the shared predicate
    # below.
    # #929: the pre-transaction acceptance predicate is now ONE function
    # (`specialist_install.validate_resume_inputs`), because the status route
    # that DISCLOSES a resume set has to apply exactly the predicate the tool
    # consuming it applies — a second copy would drift, and disclosing a set
    # this handler refuses is the defect the disclosure exists to prevent.
    # Behaviour here is unchanged: same order, same kinds, same details, and
    # `expect_slug` reproduces this handler's slug comparison (which
    # specialist_upgrade deliberately does not make).
    staged_dir = Path(args["staged_dir"])
    checked = validate_resume_inputs(
        staged_dir=staged_dir, receipt_id=args.get("receipt_id"),
        root_digest=args["root_digest"], expect_slug=args["slug"])
    if not checked.ok:
        detail = (checked.detail if checked.kind != "receipt_required"
                  else "specialist_install_commit requires a valid receipt_id "
                       "from specialist_install_inspect")
        return _result({"ok": False, "kind": checked.kind, "detail": detail})
    receipt = checked.receipt
    inspection = _inspection_from_checked(checked, staged_dir)
    # The run's own approval, for this action and these settings: a read here,
    # used by the attempt right before the library call.
    approval = _run_approval()
    refusal, approved, identity = _install_fence("specialist_install", inspection, approval)
    if refusal is not None:
        return _result(refusal)

    async def _txn() -> dict:
        # #346: same in-lock receipt re-check as specialist_upgrade — a
        # concurrent same-receipt bundle that completed while we waited has
        # pruned it; fail closed instead of double-consuming.
        if specialist_receipt.load(receipt.receipt_id) is None:
            return {"ok": False, "kind": "receipt_required",
                            "detail": "the receipt was consumed by a concurrent "
                                      "bundle while waiting for the mutation lock "
                                      "— re-run specialist_install_inspect"}
        # One tap, one attempt: the approval is used now, under the lock this
        # body holds through the library write, whatever the call's outcome.
        refused = approval_records.attempt(approval, kind="specialist_install",
                                           consent_identity=identity)
        if refused is not None:
            return refused
        try:
            instance, txn = await asyncio.to_thread(
                commit_specialist_install, inspection=inspection, receipt=receipt,
                config=args.get("config") or {},
                secret_names_provided=frozenset(args.get("secret_names_provided") or []),
                acks=SpecialistInstallAckStore(), approved_values=approved,
            )
        except SpecialistInstallError as exc:
            return {"ok": False, "kind": exc.kind, "detail": exc.detail}
        try:
            seq = await _bundle_reload_and_verify(
                txn.slug, removed_artifact_ids=list(txn.removed_artifact_ids),
                targets_removed=[])
        except Exception:
            await _bundle_compensate(txn)
            raise
        # Whole-branch B: a not-ready owned binding / postcondition failure is a
        # FAILED mutation — compensate + surface ok:false, never complete the
        # journal + report success.
        if not seq.get("ok", True):
            return await _bundle_seq_failure(txn, seq, slug=txn.slug)
        await asyncio.to_thread(specialist_bundle_journal.complete, txn.journal_path)
        # #331: a pending-configuration outcome RETAINS the receipt — the
        # follow-up configure re-commit requires it (receipt_required
        # otherwise), a fresh re-inspect refuses the desired-only slug as
        # already installed, and upgrade inspection requires an active tuple
        # — so pruning here made a first-commit-pending component impossible
        # to activate through the supported flow. The 7-day receipt age
        # sweep still reclaims an abandoned pending receipt. #306/Sol r2-1:
        # the staging tree is consumed HERE too — only after the sequencer
        # succeeded and the journal completed — because a sequencer failure
        # compensates back to pending and the retry needs the staged bytes.
        if instance.state == "active":
            _prune_bundle_receipt(receipt.receipt_id)
            specialist_install_mod.reclaim_staging_tree(staged_dir)
        return {"ok": True, "slug": instance.slug, "state": instance.state,
                "activation_committed": instance.state == "active",
                "reloaded": seq["reloaded"], "verify": seq["verify"],
                **_activation_error_disclosure(instance),
                # #499: parity with plugin_add's required_env_vars — the
                # bundled plugins' declared env names, per plugin, so the
                # install recipe wires them in THIS engagement instead of
                # leaving the first use to hit the requires gate. Names
                # come from the consented receipt rows (the same rows the
                # inspect payload and consent DM enumerate). Keyed by the
                # SCOPED name (`<slug>.<plugin>`) — the registry identity
                # that set_plugin_env_reference / verify_plugin_state
                # take for an owned plugin (review r1, Sol+Terra: the
                # bare manifest_name returns not_registered there).
                "required_env_vars": {
                         row.scoped_name: list(row.env_names)
                         for row in receipt.plugins if row.env_names
                },
                # #929: a pending outcome names its own resume. An active one
                # does not — its receipt was just pruned and its staging tree
                # reclaimed two lines above, so there is nothing to re-commit.
                **(_pending_resume_inputs(inspection, receipt,
                                          tool_name="specialist_install_commit")
                   if instance.state == "pending-configuration" else {}),
                # #676: an install's swap normally replaces an EMPTY owned
                # set and drops nothing, so this adds no fields. It is not
                # decoration: the swap runs unconditionally here, and a
                # slug carrying stale owned entries (a crash between a
                # bundle's registry save and its journal completing, then
                # reconciled away from the tuple side) has those entries
                # dropped by it. Every successful owned-set swap answers
                # for what it dropped — the class, not three of its four
                # doors.
                **_swap_removal_disclosure(txn)}

    slug = str(args["slug"])
    persona_before = await asyncio.to_thread(_bound_persona, slug)
    out = await _run_bundle_transaction(_txn)
    if isinstance(out, dict) and out.get("ok") is True and out.get("state") == "active":
        await _after_own_chat_change(out, slug, "on_install_committed", persona_before)
    return _result(out)


@tool(
    "specialist_upgrade",
    "Transactionally upgrade an installed specialist to a new version — or to the version it "
    "already has, passing new config, to change its settings — the current install keeps "
    "running until the new version validates+compiles successfully. Supply component_id/version/"
    "root_digest/staged_dir exactly as returned by specialist_install_inspect(mode='upgrade', "
    "target_slug=<slug>)." + _ORDINARY_CHANGE_TOOL_NOTE,
    {"type": "object", "properties": {
        "slug": {"type": "string"}, "component_id": {"type": "string"},
        "version": {"type": "string"}, "root_digest": {"type": "string"},
        "staged_dir": {"type": "string"}, "receipt_id": {"type": "string"},
        "config": {"type": "object", "additionalProperties": {"type": "string"}},
        "secret_names_provided": {"type": "array", "items": {"type": "string"}},
        "acknowledged_conversations": _ACKNOWLEDGED_SCHEMA},
     "required": ["slug", "component_id", "version", "root_digest", "staged_dir", "receipt_id"]},
)
@_off_the_ceiling
async def specialist_upgrade(args: dict) -> dict:
    import agent as agent_mod
    import approval_records
    from specialist_install import (
        SpecialistInstallError, upgrade_specialist, validate_resume_inputs,
    )
    from specialist_install_consent import SpecialistInstallAckStore
    import specialist_bundle_journal
    import specialist_install as specialist_install_mod
    import specialist_receipt

    # #929: the same shared acceptance predicate the install handler and the
    # status disclosure use. Whole-branch M's staged_dir_invalid mapping and
    # the fresh root re-validation ("upgrade must not trust a caller-supplied
    # digest either") both live inside it now. `expect_slug` is NOT passed:
    # this handler has never compared the staged component's slug to its
    # argument, and the extraction preserves that.
    staged_dir = Path(args["staged_dir"])
    checked = validate_resume_inputs(
        staged_dir=staged_dir, receipt_id=args.get("receipt_id"),
        root_digest=args["root_digest"])
    if not checked.ok:
        detail = (checked.detail if checked.kind != "receipt_required"
                  else "specialist_upgrade requires a valid receipt_id "
                       "from specialist_install_inspect(mode='upgrade')")
        return _result({"ok": False, "kind": checked.kind, "detail": detail})
    receipt = checked.receipt
    inspection = _inspection_from_checked(checked, staged_dir)
    approval = _run_approval()
    refusal, approved, identity = _install_fence("specialist_upgrade", inspection, approval)
    if refusal is not None:
        return _result(refusal)
    # #1095: warn first, before the transaction — the receipt, the staging tree
    # and the consent are untouched by a refused call (validation above only
    # reads them).
    refusal, ack, seen = _ordinary_change_gate("specialist_upgrade", args["slug"], args)
    if refusal is not None:
        return _result(refusal)

    async def _txn() -> dict:
        # #346: the receipt was loaded BEFORE this lock; a concurrent bundle
        # holding the same receipt may have completed (and pruned it) while
        # we waited. Re-check under the lock — proceeding would treat the
        # tuple commit as a no-op yet still rotate the owned-plugins sidecar,
        # desyncing active.prior from owned-plugins.prior for rollback.
        if specialist_receipt.load(receipt.receipt_id) is None:
            return {"ok": False, "kind": "receipt_required",
                            "detail": "the receipt was consumed by a concurrent "
                                      "bundle while waiting for the mutation lock "
                                      "— re-run specialist_install_inspect"}
        # One tap, one attempt (see specialist_install_commit).
        refused = approval_records.attempt(approval, kind="specialist_upgrade",
                                           consent_identity=identity)
        if refused is not None:
            return refused
        try:
            instance, txn = await asyncio.to_thread(
                upgrade_specialist, slug=args["slug"], inspection=inspection, receipt=receipt,
                config=args.get("config") or {},
                secret_names_provided=frozenset(args.get("secret_names_provided") or []),
                acks=SpecialistInstallAckStore(), approved_values=approved,
            )
        except SpecialistInstallError as exc:
            if exc.kind == "upgrade_kept_new_version":
                # #975: the library kept the new version (it failed after
                # activation); the receipt and staging are kept for the re-run.
                # #1095 (ruling-1095-5/-6): this arm returns BEFORE the
                # sequencer, so Casa has loaded none of it — the result says
                # so, never the shared envelope's "active" sentence (which the
                # sequencer-ran arm in _bundle_seq_failure keeps unless the
                # specialist's own reload failed without leaving the new
                # version's root on the live agent, #1146).
                # #1296: the in-use clause by the live agent, read here with no
                # await before the result is returned. #1298: a reload that
                # landed while the library ran may have loaded the kept version
                # anyway; the live root equal to the committed one says so.
                dropped = list(getattr(exc, "dropped_owned_names", ()) or ())
                live = _kept_live_match(
                    getattr(agent_mod, "active_runtime", None), args["slug"],
                    getattr(exc, "replaced_root", None),
                    committed_root=getattr(exc, "committed_root", None))
                details = getattr(exc, "details_by_live", None) or {}
                return {"ok": False, "kind": exc.kind,
                        "detail": details.get(live, exc.detail),
                        "kept_new_version": True,
                        "outcome": _KEPT_NOT_ACTIVE_OUTCOMES[
                            (bool(getattr(exc, "restart_first", False)), live)],
                        **(_plugin_data_disclosure(_PLUGIN_DATA_NOTE_COMMITTED, dropped)
                           if dropped else {})}
            return {"ok": False, "kind": exc.kind, "detail": exc.detail}
        try:
            seq = await _bundle_reload_and_verify(
                txn.slug, removed_artifact_ids=list(txn.removed_artifact_ids),
                targets_removed=[])
        except Exception:
            await _bundle_compensate(txn)
            raise
        if not seq.get("ok", True):
            return await _bundle_seq_failure(txn, seq, slug=txn.slug)
        await asyncio.to_thread(specialist_bundle_journal.complete, txn.journal_path)
        # #331: a pending-configuration upgrade outcome retains the receipt
        # for the follow-up configure re-commit; #306/Sol r2-1: the staging
        # tree is likewise consumed only now, after sequencer success (same
        # rationale as specialist_install_commit above).
        if instance.state == "active":
            _prune_bundle_receipt(receipt.receipt_id)
            specialist_install_mod.reclaim_staging_tree(staged_dir)
        return {"ok": True, "slug": instance.slug, "state": instance.state,
                "reloaded": seq["reloaded"], "verify": seq["verify"],
                **_activation_error_disclosure(instance),
                # #929: parity with specialist_install_commit — an upgrade that
                # lands pending-configuration keeps its receipt and staging
                # tree, and names the five inputs the follow-up re-commit takes.
                **(_pending_resume_inputs(inspection, receipt,
                                          tool_name="specialist_upgrade")
                   if instance.state == "pending-configuration" else {}),
                # #676: an upgrade whose new owned generation omits an old
                # plugin removed that plugin's registry entry. Same
                # persisting removal, same disclosure.
                **_swap_removal_disclosure(txn)}

    persona_before = await asyncio.to_thread(_bound_persona, str(args["slug"]))
    out = _after_ordinary_change(
        await _run_bundle_transaction(_txn), args["slug"], ack, seen)
    if isinstance(out, dict) and out.get("ok") is True:
        await _after_own_chat_change(out, str(args["slug"]), "on_upgrade_committed",
                                     persona_before)
    return _result(out)


@tool(
    "specialist_rollback",
    "Restore an installed specialist's retained prior tuple — the state before its most recent "
    "upgrade OR persona override (a first persona override rotates the component-default "
    "binding into that prior, so this is also the way back to the bundled persona). The "
    "rollback exchanges the active tuple with the retained prior, owned plugin set included; "
    "fails with no_prior_tuple if nothing was retained." + _ORDINARY_CHANGE_TOOL_NOTE,
    {"type": "object", "properties": {
        "slug": {"type": "string"},
        "acknowledged_conversations": _ACKNOWLEDGED_SCHEMA}, "required": ["slug"]},
)
async def specialist_rollback(args: dict) -> dict:
    from specialist_install import SpecialistInstallError, rollback_specialist
    from specialist_install_consent import SpecialistInstallAckStore
    import specialist_bundle_journal

    slug = args["slug"]
    # #1095: warn first, before the transaction.
    refusal, ack, seen = _ordinary_change_gate("specialist_rollback", slug, args)
    if refusal is not None:
        return _result(refusal)

    async def _txn() -> dict:
        try:
            instance, txn = await asyncio.to_thread(
                rollback_specialist, slug=slug, bundle=True,
                acks=SpecialistInstallAckStore())
        except SpecialistInstallError as exc:
            return {"ok": False, "kind": exc.kind, "detail": exc.detail}
        try:
            seq = await _bundle_reload_and_verify(
                txn.slug, removed_artifact_ids=list(txn.removed_artifact_ids),
                targets_removed=[])
        except Exception:
            await _bundle_compensate(txn)
            raise
        if not seq.get("ok", True):
            return await _bundle_seq_failure(txn, seq, slug=txn.slug)
        await asyncio.to_thread(specialist_bundle_journal.complete, txn.journal_path)
        return {"ok": True, "slug": instance.slug, "state": instance.state,
                "reloaded": seq["reloaded"], "verify": seq["verify"],
                # #676: a rollback republishes the RETAINED prior owned
                # set, so a plugin the current generation added and the
                # prior one never had is dropped by the swap.
                **_swap_removal_disclosure(txn)}

    persona_before = await asyncio.to_thread(_bound_persona, slug)
    out = _after_ordinary_change(
        await _run_bundle_transaction(_txn), slug, ack, seen)
    if isinstance(out, dict) and out.get("ok") is True:
        await _after_own_chat_change(out, slug, "on_rollback_committed", persona_before)
    return _result(out)


@tool(
    "specialist_uninstall",
    "Remove an installed specialist entirely (its binding, config, and legacy operational files). "
    "Does not affect a hand-authored (non-installed) specialist of the same name. When a bundled "
    "plugin declares an eraser the operator is asked first, in the DM, with the options Casa "
    "offers for this uninstall: call without erase_data and wait for Casa to continue with "
    "their choice. After the uninstall, Casa closes the specialist's conversations still open "
    "and lists each in closed_conversations; any it could not close are also listed in "
    "conversations_still_open." + _UNINSTALL_TOOL_NOTE,
    {"type": "object", "properties": {
        "slug": {"type": "string"}, "erase_data": {"type": "boolean"},
        "acknowledged_conversations": _ACKNOWLEDGED_SCHEMA},
     "required": ["slug"]},
)
async def specialist_uninstall(args: dict) -> dict:
    from specialist_install import SpecialistInstallError, uninstall_specialist
    from specialist_install_consent import SpecialistInstallAckStore
    import specialist_bundle_journal

    slug = args["slug"]
    targets_removed = [f"specialist:{slug}"]
    reports: list = []
    cleared: list = []
    not_cleared: list = []
    ack, bad = _acknowledged(args)
    if bad is not None:
        return _result(bad)

    def _confirm(withdrew: bool) -> "dict | None":
        # #1095: run by the erase gate, inside this transaction (which owns
        # the mutation lock), at the point where the base call would open or
        # close the erase question — so a refused call does exactly that
        # question step and nothing else, linearized with every other
        # uninstall of this slug. Never on an erase_data=true call.
        recs = _open_specialist_engagements(slug)
        if all(r.id in (ack or ()) for r in recs):
            return None
        return _open_conversations_pending("specialist_uninstall", slug, recs,
                                           removal=True, withdrew=withdrew)

    async def _txn() -> dict:
        import plugin_erasure
        held: list = []
        try:
            return await _txn_body(held)
        finally:
            # #1070: the transaction's own task — which a cancelled handler
            # abandons but never cancels — settles the fence it held.
            for token in held:
                plugin_erasure.FENCE.settle(token)

    async def _txn_body(held: list) -> dict:
        # #1046: the erase step runs inside the transaction, which owns the
        # mutation lock, on the bundle's erasing plugins as the registry holds
        # them under it — decision and uninstall see one state.
        gate, taken = await _erase_gate(
            tool="specialist_uninstall", arg="slug", name=slug,
            subject=f"specialist:{slug}", what=f"the specialist {slug}",
            specs=_erase_specs_for(_owned_entries_now(slug)),
            erase=args.get("erase_data"), held=held, confirm=_confirm,
            call_extra=(f", acknowledged_conversations={ack!r}" if ack else ""))
        if gate is not None:
            return gate
        reports.extend(taken)
        # #1067: after an Erase everything, the erased plugins' names are
        # computed before the uninstall and cleared once it committed, both
        # inside this transaction (which owns the mutation lock). An owned
        # plugin without an eraser keeps its data, and so its references.
        clear = (_env_names_to_clear({r[0] for r in taken})
                 if taken and _erased_everything(taken) else [])
        # Whole-branch M: map a typed refusal (invalid_slug / bundle_required)
        # to a structured ok:false envelope, never a raw exception out of the
        # tool — the same guard the other four bundle tools already have.
        try:
            txn = await asyncio.to_thread(
                uninstall_specialist, slug=slug, bundle=True,
                acks=SpecialistInstallAckStore())
        except SpecialistInstallError as exc:
            return {"ok": False, "kind": exc.kind, "detail": exc.detail}
        try:
            seq = await _bundle_reload_and_verify(
                txn.slug, removed_artifact_ids=list(txn.removed_artifact_ids),
                targets_removed=targets_removed)
        except Exception:
            await _bundle_compensate(txn)
            raise
        if not seq.get("ok", True):
            return await _bundle_seq_failure(txn, seq, slug=slug)
        await asyncio.to_thread(specialist_bundle_journal.complete, txn.journal_path)
        # #1095: the uninstall committed and the reload removed the
        # specialist, so its open conversations are closed now — read here,
        # never copied from the warning, so one opened since the operator
        # confirmed is closed too and named. In this shielded child, so a
        # cancelled handler still closes them (its result is then lost).
        closed = await close_specialist_engagements(
            slug, reason=f"Closed: the specialist {slug} was uninstalled.",
            acknowledged=ack)
        done, failed = await _clear_env_references(clear)
        cleared.extend(done)
        not_cleared.extend(failed)
        # #676: the uninstall CASCADED these owned plugins out of the registry —
        # the same persisting committed removal, reached by another door, so the
        # same disclosure is owed. Zero owned plugins removes nothing and discloses
        # nothing. One gate serves every successful bundle now (Terra handback
        # review, attempt 2): an uninstall's swap publishes an EMPTY owned set, so
        # `removed_owned_names` is exactly the entries this slug owned — the same
        # answer the hand-rolled `before_entries` read gave here, now derived the
        # one way an upgrade and a rollback can share.
        payload = {"ok": True, "slug": slug, "reloaded": seq["reloaded"],
                   "verify": seq["verify"]}
        if closed:
            payload["closed_conversations"] = closed
            still = [r["engagement_id"] for r in closed if r["outcome"] == "close_failed"]
            if still:
                payload["conversations_still_open"] = still
        payload.update(_swap_removal_disclosure(txn))
        return payload

    own_chat = _own_chat_disclosure(slug)
    outcome = await _run_bundle_transaction(_txn)
    if isinstance(outcome, dict) and outcome.get("ok") is True:
        # the agent's own bot, if it has one, goes dormant; the operator may
        # delete it in BotFather, which the result names so it can be offered
        await _own_chat_hook("on_uninstall_committed", slug)
        if own_chat is not None:
            outcome["own_chat"] = own_chat
    if reports and isinstance(outcome, dict) and outcome.get("ok") is True:
        # The reload takes the plugin guard itself, so it runs after the
        # transaction released the lock.
        if cleared and not await _reload_plugin_env_after_clear():
            outcome["env_reload_ok"] = False
        _apply_erasure_to_disclosure(outcome, reports, cleared, not_cleared)
    return _result(outcome)


@tool(
    "persona_install_inspect",
    "Fetch a persona-only repository for inspection (non-persistent staging). ref may be a tag, a "
    "sha, a branch, or the literal 'latest' — the newest published release tag, reported back as "
    "resolved_ref, never a branch; a repository with no published release refuses with "
    "no_release_found. Returns the persona "
    "id/version/checksum/display name, or a structured error. On success this ALSO asks the "
    "operator to approve the install on Casa's card (approval 'requested': stop; Casa continues "
    "you with persona_install_commit after Approve), or reports approval 'already_approved' "
    "(stop; Casa continues you once this run ends) or 'in_progress' (this run is the one Casa "
    "started for the approval: make the call). If the card cannot be posted, this returns "
    "ok:false (consent_channel_unavailable / consent_prompt_refused / consent_prompt_failed / "
    "consent_delivery_failed / consent_prompt_inactive / consent_post_unsettled).",
    {"type": "object", "properties": {
        "repo": {"type": "string"}, "ref": {"type": "string"}, "subdir": {"type": "string"},
        "expected_revision": {"type": "string"}}, "required": ["repo", "ref"]},
)
@_off_the_ceiling
async def persona_install_inspect(args: dict) -> dict:
    import approval_records
    import configurator_continue
    from persona_install import inspect_persona_repo, persona_install_consent_identity
    from persona_install_consent import prompt_persona_install_consent
    from specialist_install import SpecialistInstallError

    try:
        result = await asyncio.to_thread(
            inspect_persona_repo, args["repo"], args["ref"], subdir=args.get("subdir", ""),
            expected_revision=args.get("expected_revision"))
    except SpecialistInstallError as exc:
        return _result({"ok": False, "kind": exc.kind, "detail": exc.detail})

    ok_payload = {"ok": True, "persona_id": result.persona_id, "version": result.version,
                  "checksum": result.checksum, "display_name": result.display_name,
                  "staged_dir": str(result.staged_dir),
                  # INV-PERS-020: the ref actually fetched — the release tag
                  # when the caller passed ``latest``.
                  "resolved_ref": result.resolved_ref}

    # The approval this install asks for: the persona as inspected, with no
    # settings. Casa's card asks the operator; after Approve Casa carries the
    # configurator on with persona_install_commit.
    values: dict = {}
    identity = persona_install_consent_identity(
        persona_id=result.persona_id, version=result.version, checksum=result.checksum,
        values_digest=approval_records.values_digest(values))
    origin = _snapshot_origin() or {}
    # #1565: only the operator's own server-stamped words; any other root (an
    # event wake, a narration) records none, so its run is never told them as the
    # operator's
    asked = (operator_words(origin) or "")[:approval_records.ASKED_MAX]
    carrying = origin.get(approval_records.APPROVAL_KEY)
    carrying_rid = str(carrying.get("rid") or "") if isinstance(carrying, dict) else ""
    async with _plugin_tools_guard():
        record, outcome, superseded = approval_records.request(
            "persona_install", result.persona_id, identity, values,
            asked=asked, carrying_rid=carrying_rid)
    continuation = configurator_continue.continuation_text(
        "persona_install", persona_id=result.persona_id, version=result.version,
        checksum=result.checksum, staged_dir=str(result.staged_dir))
    if outcome == "in_progress":
        return _result({**ok_payload, "approval": "in_progress",
                        "next": ("this run is the one Casa started for this approval; "
                                 "call persona_install_commit")})
    if outcome == "already_approved":
        configurator_continue.start(
            continuation, key=record.rid, approval=record,
            on_outcome=_approval_notice(f"install of persona {result.persona_id}"))
        return _result({**ok_payload, "approval": "already_approved",
                        "next": "stop; Casa continues you once this run ends"})
    await _supersede_card(superseded)

    requester_name = _requester_name(origin)

    def _post(**kw):
        return prompt_persona_install_consent(inspection=result,
                                              requester_name=requester_name, **kw)
    posted = await _post_approval_card(
        record, _post, what=f"persona {result.persona_id}", continuation=continuation)
    if posted.get("ok") is not True:
        return _result(posted)
    return _result({**ok_payload, **posted})


@tool(
    "persona_install_commit",
    "Persist an INSPECTED persona to /config/personas — only in the run Casa started after the "
    "operator approved it on the card persona_install_inspect asked for; one approval is one "
    "attempt.",
    {"type": "object", "properties": {
        "persona_id": {"type": "string"}, "version": {"type": "string"},
        "checksum": {"type": "string"}, "staged_dir": {"type": "string"}},
     "required": ["persona_id", "version", "checksum", "staged_dir"]},
)
@_off_the_ceiling
async def persona_install_commit(args: dict) -> dict:
    import approval_records
    from persona_install import (
        PersonaInspectionResult, PersonaInstallAckStore, commit_persona_install,
        persona_install_consent_identity,
    )
    from persona_pack import PersonaPackError, load_persona_pack
    from specialist_install import SpecialistInstallError

    staged_dir = Path(args["staged_dir"])
    # Round-2 fix (finding #3): reload the staged bytes and cross-check the
    # RECOMPUTED persona_id/version/checksum against the caller-supplied
    # args, rather than trusting them directly — mirrors
    # specialist_install_commit's existing (correct) pattern.
    try:
        pack = load_persona_pack(staged_dir / "pack", staged_dir / "manifest.json")
    except (PersonaPackError, OSError) as exc:
        return _result({"ok": False, "kind": "staged_dir_invalid", "detail": str(exc)})
    if (pack.persona_id != args["persona_id"] or pack.version != args["version"]
            or pack.checksum != args["checksum"]):
        return _result({"ok": False, "kind": "checksum_changed",
                         "detail": "staged persona no longer matches the approved inspection"})
    inspection = PersonaInspectionResult(
        persona_id=pack.persona_id, version=pack.version, checksum=pack.checksum,
        display_name=pack.identity.get("display_name", pack.persona_id), staged_dir=staged_dir)
    approval = _run_approval()
    approved = approval_records.approved_values(approval)
    identity = (persona_install_consent_identity(
        persona_id=pack.persona_id, version=pack.version, checksum=pack.checksum,
        values_digest=approval_records.values_digest(approved))
        if approved is not None else "")
    refusal = approval_records.fence(approval, kind="persona_install", consent_identity=identity)
    if refusal is not None:
        return _result(refusal)

    async def _txn() -> dict:
        # Under the plugin-tools lock from the attempt through the write, so a
        # newer request cannot replace the approval in between.
        refused = approval_records.attempt(approval, kind="persona_install",
                                           consent_identity=identity)
        if refused is not None:
            return refused
        try:
            done = await asyncio.to_thread(
                commit_persona_install, inspection=inspection, acks=PersonaInstallAckStore(),
                approved_values=approved)
        except SpecialistInstallError as exc:
            return {"ok": False, "kind": exc.kind, "detail": exc.detail}
        return {"ok": True, "persona_id": done.persona_id, "version": done.version,
                "checksum": done.checksum}

    return _result(await _run_bundle_transaction(_txn))


# #931: a resident's persona identity is an input of its binding digest
# (personality_binding.compute_binding_digest), and that digest is a resume
# precondition — agent._resume_decision refuses a stored session whose digest
# differs from the freshly loaded resident's, returning
# ("new", "binding_mismatch") after the role check and BEFORE the freshness
# window, so the refusal is channel-agnostic. What a conversation said reaches
# memory through live capture as it happens, on the channels capture admits —
# voice is not one (channel_policy.writes_to_bank).
#
# Staging is the LAST moment the operator can be told, because the promotion
# happens at a restart they are about to order. One constant, shared by every
# staging entry point's description AND its result, so the three cannot drift
# apart — and so the telling is data the model must relay, not doctrine it may
# have skipped. INV-PERS-018, pinned by
# tests/test_resident_persona_restart_notice.py.
#
# NOTHING HERE PREDICTS THE OUTCOME, AND NOTHING INSTRUCTS ANYONE ELSE TO.
# Three diff-review rounds found the same mechanism — something deciding
# whether this staging will move the digest — wrong in every direction it was
# tried:
#   r1 (Terra, S2) an unconditional notice fired on a stage whose candidate
#     equals the active binding, which reconcile discards rather than promotes:
#     a confident FALSE warning of conversation loss.
#   r2 (Astra, S2) the predicate added for r1 stayed SILENT where the digest
#     does move. `reset` resolves its pack through persona_pack_roots(), which
#     searches the INSTALLED root first, while boot loads the image-default
#     pack from the image root ONLY (agent_loader's `_load_default`). An
#     installed pack shadowing an image-default ref makes the two disagree, and
#     a suppressed TRUE warning is the original bug restored. Filed as #945.
#   r3 (Astra AND Terra, S2) the predicate was gone from Python but survived as
#     an INSTRUCTION: this sentence told the model to say "which of the two
#     applies", and the recipe told it to infer that from `prior_persona` or
#     from what the operator asked for. In the #945 state both of those read
#     identical while boot changes the digest — the same false reassurance, one
#     carrier further out. Terra also measured that `compute_binding_digest`
#     moves for non-persona inputs (a role checksum among them), so an
#     apparently unchanged persona never implied continuity in the first place.
#   r4 (Astra, S2) the same overclaiming, now about the CONSOLATION rather
#     than the loss: the sentence said Telegram history "stays recallable",
#     which was false on a default install of the time (long-term memory was
#     off by default). Qualified with "only if long-term memory is configured".
#   r5 (Terra, S2) that qualification was STILL a guarantee: the save path of
#     the time could discard, skip or give up. "Written out" promises what the
#     code does not.
#
#   r8 (Astra, S2) the CENTRAL claim was itself too strong. "Every
#     conversation starts fresh" is false: `_resume_decision` compares the
#     promoted digest against EACH STORED SESSION's own digest, not against
#     the identity being left behind, so a conversation last held under the
#     persona now being promoted RESUMES. Astra reproduced A -> B -> A through
#     three real reconciliations: 2 resumed, 2 fresh. The sentence now says
#     "last held under a different persona identity", and says that in the
#     ordinary case that is all of them.
#
# The conclusion the rounds force: this tool CANNOT know what survives — not
# whether the identity moves (boot decides, after this call returns), and not
# whether anything is recoverable afterwards (configuration, the wipe fence and
# the snapshot's own integrity decide). Two rounds on the consolation clause is
# the signal to CUT it, not to qualify it a third time. So the sentence states
# the LOSS, names who decides, and mentions memory not at all: what it costs is
# the operator's decision input, and a consolation nobody can guarantee was
# only ever a way of softening it. INV-PERS-018, pinned by
# tests/test_resident_persona_restart_notice.py.
RESIDENT_CONVERSATION_RESET_NOTICE = (
    "On the restart that promotes this binding, every conversation of this "
    "resident that was last held under a different persona identity starts "
    "fresh on every channel — the persona is part of the resident's session "
    "identity, and each stored conversation is judged against the identity "
    "that is actually promoted, not against the one being left behind. In the "
    "ordinary case that is every conversation the resident is holding now. "
    "Voice history is not carried at all, and Casa promises nothing about "
    "recovering what any of those conversations held. Boot decides what is "
    "promoted, not this tool, and staging the persona the resident already "
    "appears to have does not guarantee continuity. Relay this to the "
    "operator verbatim before they restart; do not predict which way it will "
    "go."
)


@tool(
    "persona_apply",
    "Apply an installed persona as an override binding to a resident slot (assistant/butler/"
    "concierge) or an installed specialist slug. The persona is compiled for the target's "
    "surfaces first: one that exceeds an admission ceiling is refused with nothing written "
    "(ok:false, kind:incompatible). For a RESIDENT the accepted binding is STAGED, not "
    "activated — it takes effect on that resident's next restart. For a specialist it is "
    "committed and loaded now — this tool reloads the specialist itself (its reply carries "
    "the reload) — so new conversations with it use it from then on; a conversation already "
    "open with it keeps its personality." + _ORDINARY_CHANGE_TOOL_NOTE
    + " FOR A RESIDENT, say this when you report the staging: "
    + RESIDENT_CONVERSATION_RESET_NOTICE,
    {"type": "object", "properties": {
        "target_role_id": {"type": "string"}, "persona_id": {"type": "string"},
        "persona_version": {"type": "string"},
        "acknowledged_conversations": _ACKNOWLEDGED_SCHEMA},
     "required": ["target_role_id", "persona_id", "persona_version"]},
)
async def persona_apply(args: dict) -> dict:
    from persona_install import apply_persona_override, validate_persona_path_segments
    from persona_pack import PersonaPackError, load_persona_pack
    from role_slot import _ha_model_options, materialize_role
    from role_artifact import load_role_artifact
    from specialist_install import SpecialistInstallError, validate_specialist_slug

    kind, _, slot = args["target_role_id"].partition(":")
    # F1: persona_id/persona_version index `/config/personas/<id>/<version>/`
    # and `slot` indexes both the image-roles tree and the InstanceDir tree —
    # validate every caller-supplied path segment before any join.
    try:
        validate_persona_path_segments(args["persona_id"], args["persona_version"])
        validate_specialist_slug(slot)
    except SpecialistInstallError as exc:
        return _result({"ok": False, "kind": exc.kind, "detail": exc.detail})
    # #323: resolve the installed-personas root through the same env-aware
    # seam the resident loader uses ($CASA_CONFIG_DIR/personas) — hard-coding
    # /config here made an installed pack under a custom config root report
    # persona_unavailable.
    from persona_install import installed_personas_root
    personas_root = (installed_personas_root()
                     / args["persona_id"] / args["persona_version"])
    try:
        persona = load_persona_pack(personas_root / "pack", personas_root / "manifest.json")
    except (PersonaPackError, OSError) as exc:
        return _result({"ok": False, "kind": "persona_unavailable", "detail": str(exc)})
    # INV-PERS-013 (#737): the pack found at the requested directory must
    # DECLARE the identity the caller named — the rule `_resolve_local_persona`
    # (#323) and the resident loader's `_declares` (#670) already enforce. A
    # pack parked under the wrong directory was otherwise bound under its own
    # declared id@version while this tool reported success for the requested
    # one, and the response carries no persona identity that would reveal it.
    if persona.persona_id != args["persona_id"] or persona.version != args["persona_version"]:
        return _result({"ok": False, "kind": "persona_unavailable",
                        "detail": (f"persona pack under {personas_root} declares "
                                   f"{persona.persona_id}@{persona.version}, not the "
                                   f"requested {args['persona_id']}@{args['persona_version']}; "
                                   "nothing was staged")})

    async def _materialize_and_apply(role_dir: Path, instance_dir_root: Path, **library_kwargs):
        # #355: resolve ha_option models exactly as the agent loader does —
        # options={} would bind the override to the role's DEFAULT checksum,
        # and the loader would then reject the persisted binding.
        role = materialize_role(
            source=load_role_artifact(role_dir), options=_ha_model_options())
        # #607: the SAME compile proof boot reconciliation runs
        # (agent_loader.make_candidate_compile_validator), so a persona that
        # blows an admission ceiling is refused here — with nothing written —
        # instead of being committed active and killing every subsequent boot.
        # Built on this thread and handed in; the factory reads the image-owned
        # platform frame and safety kernel eagerly, so a broken image raises
        # OSError here rather than inside the materialize lock.
        import agent_loader

        try:
            validator = agent_loader.make_candidate_compile_validator(role)
        except OSError as exc:
            return {"ok": False, "kind": "image_incomplete", "detail": str(exc)}
        try:
            staged = await _to_thread_settled(
                apply_persona_override, target_role_id=args["target_role_id"],
                persona=persona, role=role, instance_dir_root=instance_dir_root,
                candidate_validator=validator, **library_kwargs)
        except ValueError as exc:
            return {"ok": False, "kind": "incompatible", "detail": str(exc)}
        except SpecialistInstallError as exc:
            # Fix-round-1 (finding IMPORTANT): installed_component_role_dirs()
            # legitimately resolves a pending-configuration specialist
            # (desired-only, active=None — a real state per
            # specialist_registry.py), so apply_persona_override's specialist
            # branch can reach here and raise
            # SpecialistInstallError("no_active_tuple", ...) for a target this
            # tool otherwise accepted. Without this clause that escaped as an
            # unstructured MCP error instead of the tool's {ok, kind} contract.
            return {"ok": False, "kind": exc.kind, "detail": exc.detail}
        # #607: for a resident this is now the STAGED tuple — `restart_required`
        # describes something that has not happened yet, which is what the
        # tool description and recipes/persona/apply.md have always claimed. A
        # specialist is committed active and reports restart_required: false,
        # and this tool reloads it below (its tuple is never committed).
        # #931: the staging result carries what the promoting restart COSTS,
        # not only that one is required. `restart_required` told the operator
        # when the binding takes effect and never what taking effect does to
        # the conversations they are already holding. Additive, and gated on
        # exactly the condition `restart_required` is: a specialist activates
        # on a reload rather than a restart, so the notice would be untrue
        # of it — None is "no statement", not a silent omission.
        return {"ok": True, "target_role_id": args["target_role_id"],
                "binding_digest": staged.binding.binding_digest,
                "restart_required": kind == "resident",
                "conversation_notice": (
                    RESIDENT_CONVERSATION_RESET_NOTICE if kind == "resident"
                    else None)}

    if kind == "resident":
        # Controller resolution #2 (task-n1d-brief deviation): the brief
        # hard-coded Path("/opt/casa/defaults/roles/resident") — use the
        # canonical image-roles seam (agent_loader.DEFAULT_ROLES_DIR)
        # instead, identical in production but correct under test/dev
        # layouts where the image tree isn't rooted at /opt/casa.
        import agent_loader

        role_dir = Path(agent_loader.DEFAULT_ROLES_DIR) / "resident" / slot
        # #323: resolve through the same seam boot reads (CASA_BINDINGS_DIR)
        # — hard-coding /config/bindings made this tool report success +
        # restart_required while the restarted resident read a different
        # directory and kept the prior persona.
        instance_dir_root = agent_loader._resident_bindings_root(None) / f"resident-{slot}"
        return _result(await _materialize_and_apply(role_dir, instance_dir_root))
    if kind != "specialist":
        return _result({"ok": False, "kind": "invalid_target", "target_role_id": args["target_role_id"]})

    # #810 (INV-SPEC-011): a specialist override is a specialist-generation
    # transaction (it rotates the tuple AND its owned-plugins sidecar into the
    # retained prior), so this arm holds the plugin-tools mutation lock across
    # the WHOLE transaction — from the installed-index read that chooses the
    # role, through the role materialization, to the library call — exactly
    # as the four bundle handlers do, so no bundle handler's swap or commit can
    # interleave with the discovery this apply is built on. The raw lock, not
    # `_plugin_tools_guard`: this handler never re-enters. The library call
    # is the JOURNALED arm (`bundle=True`): the transaction begins, commits
    # and completes inside the worker thread under the lifecycle lock, so a
    # cancelled handler task can neither release it early nor leave its
    # journal open — this handler has no sequencer phase for a journal to
    # outlive.
    async with _PLUGIN_TOOLS_LOCK:
        from specialist_registry import InstalledSpecialistIndex

        index = InstalledSpecialistIndex()
        index.load()
        role_dirs = index.installed_component_role_dirs()
        if slot not in role_dirs:
            return _result({"ok": False, "kind": "not_installed", "slug": slot})
        # #1095: warn first — the apply below is the commit. Residents never
        # reach this (their binding is staged, INV-PERS-018's notice).
        refusal, ack, seen = _ordinary_change_gate("persona_apply", slot, args)
        if refusal is not None:
            return _result(refusal)
        out = _after_ordinary_change(await _materialize_and_apply(
            role_dirs[slot] / "role", Path("/config/specialists") / slot, bundle=True),
            slot, ack, seen)
    if isinstance(out, dict) and out.get("ok") is True:
        out.update(await _reload_after_untracked_write("agent", slot))
        # the agent's own bot takes on the new persona (its name, its avatar)
        await _own_chat_hook("on_persona_applied", slot)
    return _result(out)


@tool(
    "offer_own_chat",
    "Give a specialist that can have its own Telegram chat with the person a chat of "
    "its own, or a new one. You choose the bot's username (its handle). By default it "
    "is Casa, then the specialist's role, then a few digits, then Bot (for example "
    "CasaFinance4821Bot): name the role, not the persona, and the digits make it very "
    "likely unused. Follow the pattern of the person's existing specialist handles "
    "when they have one; the person's own wishes always win. Before you propose "
    "handles to the person, check them with mode \"check\" and candidates (it also "
    "tells you the role, the persona's name, your own bot's handle and the other "
    "specialists' handles, and the handles it had before); propose only ones it "
    "reports probably_free, or used_before: a bot this specialist was moved away "
    "from, which it can move back to. mode "
    "\"offer\" (the default) with username posts the card that creates the chat, or, "
    "once created, the one that opens it; when the chat is already open it returns "
    "already_open with its link. When the person wants a specialist's handle "
    "changed, use mode \"replace\" with the new username: it is refused while "
    "something is still open in the specialist's current chat, so tell the person "
    "what is open and that the change can happen once it is done. Once the person taps "
    "Create, the specialist says hi in its new chat within about half a minute; Casa "
    "restarts behind it, which the person need not hear about. If the person changes "
    "their mind before tapping Create, use mode \"drop\". Asking again with mode "
    "\"replace\" after a move was held back finishes it. Offering a used_before handle "
    "(with mode \"replace\", or \"offer\" when the specialist has no bot) moves the "
    "specialist back to that bot within about half a minute, with nothing to tap; if "
    "Telegram no longer has that bot, the result says so. To change the name shown at "
    "the top of the specialist's chat (not its handle), use mode \"rename\" with name: "
    "it applies at once, and to any later bot of that specialist. When a specialist was just "
    "installed or upgraded and can now have its own chat, offer it, choosing the "
    "handle yourself; the person need not discuss it. For a specialist that cannot "
    "have its own chat, it returns an error saying so.",
    {"type": "object",
     "properties": {
         "agent": {"type": "string"},
         "mode": {"type": "string",
                  "enum": ["check", "offer", "replace", "drop", "rename"],
                  "default": "offer"},
         "username": {"type": "string"},
         "name": {"type": "string"},
         "candidates": {"type": "array", "items": {"type": "string"}},
     },
     "required": ["agent"]},
)
async def offer_own_chat(args: dict) -> dict:
    """The resident's tool for a specialist's own chat: it checks handles,
    offers the chat, replaces its bot (or moves it back to an earlier one),
    drops a pending change or renames the bot. The body and the results are
    the own-chat lifecycle's."""
    import own_voice
    agent = args.get("agent")
    role, refusal = _handoff_target(agent if isinstance(agent, str) else "")
    if refusal is not None:
        return refusal
    mode = args.get("mode") or "offer"
    username = args.get("username")
    candidates = args.get("candidates")
    name = args.get("name")
    try:
        return _result(await own_voice.offer_own_chat(
            role, mode=mode if isinstance(mode, str) else "",
            username=username if isinstance(username, str) else None,
            candidates=[c for c in candidates if isinstance(c, str)]
            if isinstance(candidates, list) else None,
            name=name if isinstance(name, str) else None))
    except Exception:  # noqa: BLE001 — a result, never a raise
        logger.exception("offer_own_chat for %s failed", role)
        return _result({"status": "error", "kind": "offer_failed",
                        "message": "That did not go through; try again shortly."})


# ---------------------------------------------------------------------------
# An agent's own chat across specialist changes. Each hook runs after the
# bundle transaction has committed and released its lock; a hook's failure
# is logged and never turns a committed change into a failed one (the next
# boot reconciles the chat against the committed state).
# ---------------------------------------------------------------------------



async def _own_chat_hook(name: str, role: str) -> bool:
    """Run the own-chat lifecycle hook *name* for *role*; ``False`` when it
    failed (logged)."""
    import own_voice
    try:
        await getattr(own_voice, name)(role)
    except Exception:  # noqa: BLE001 — the committed change stands
        logger.warning("own chat: %s for %s failed", name, role, exc_info=True)
        return False
    return True


def _bound_persona(slug: str) -> "str | None":
    """The persona *slug*'s active tuple binds, as one comparable string, or
    ``None`` (not installed, unreadable). Off the event loop: it reads the
    instance files."""
    try:
        from specialist_registry import InstalledSpecialistIndex
        index = InstalledSpecialistIndex()
        index.load()
        inst = index.get_instance(slug)
        binding = inst.active.binding if inst is not None and inst.active is not None else None
    except Exception:  # noqa: BLE001 — unknown is "no persona"
        return None
    if binding is None:
        return None
    return f"{binding.persona_id}@{binding.persona_version}#{binding.persona_checksum}"


def _own_chat_disclosure(role: str) -> "dict | None":
    """The bot *role* speaks through and the BotFather link that deletes
    it (``{"bot": "@name", "delete_link": ...}``), as the own-chat records
    hold them, or ``None`` when it has no bot."""
    import own_voice
    try:
        return own_voice.own_chat_disclosure(role)
    except Exception:  # noqa: BLE001 — no bot known
        logger.warning("own chat of %s could not be read for the uninstall result", role,
                       exc_info=True)
        return None


async def _after_own_chat_change(out: dict, slug: str, hook: str,
                                 persona_before: "str | None") -> None:
    """After a committed install, upgrade or rollback of *slug*: the chat's
    lifecycle *hook* (dormancy for a chat no longer declared, back for one
    declared again), the bot's self-setup again when the bound persona
    changed, and ``own_chat: "offer_due"`` in the result when *slug* declares
    a chat it has no bot for yet. Casa offers nothing itself: the configurator
    says so in its answer, and the resident makes the offer."""
    import own_voice
    ran = await _own_chat_hook(hook, slug)
    if await asyncio.to_thread(_bound_persona, slug) != persona_before:
        await _own_chat_hook("on_persona_applied", slug)
    if not ran:
        return
    try:
        if own_voice.declared(slug) and own_voice.own_chat_state(slug) == "no_bot":
            out["own_chat"] = "offer_due"
    except Exception:  # noqa: BLE001 — the result just does not say so
        logger.warning("own chat state for %s unreadable", slug, exc_info=True)


async def _reload_after_untracked_write(scope: str, role: "str | None") -> dict:
    """The reload for a change the config repository cannot see. A specialist's
    instance tuple lives under ``specialists/``, which is never tracked, so no
    ``config_git_commit`` reloads it (INV-TOOL-014): the tool that wrote it
    reloads it here, the way the bundle handlers reload theirs. Called with no
    plugin lock held — the reload's health refresh takes the plugin guard.
    Returns ``{"reload": <envelope>}``, plus a ``warning`` when the reload did
    not fully succeed."""
    import agent as agent_mod
    runtime = getattr(agent_mod, "active_runtime", None)
    if runtime is None:
        envelope = {"status": "error", "kind": "not_initialized",
                    "message": "CasaRuntime not bound - boot ordering bug"}
    else:
        envelope = await _run_reload(scope, runtime=runtime, role=role)
    out: dict = {"reload": envelope}
    if envelope.get("status") != "ok" or envelope.get("failures"):
        out["warning"] = (
            "The change is saved, but its reload did not fully succeed, so it is "
            "not live yet. Read the reload's kind, message and failures and tell "
            "the operator plainly; retry with casa_reload and the scope shown.")
    return out


def _find_entry(data, name: str) -> dict | None:
    return next((e for e in data.raw.get("plugins", [])
                 if isinstance(e, dict) and e.get("name") == name), None)


def _profile_tools_denied_by_config(name: str, target: str, profile, *,
                                    entry: "dict | None" = None,
                                    store_root=None) -> list:
    """S8 (round-7 fold): the bare tools of *profile* that the TARGET's own
    runtime.yaml ``tools.disallowed`` keeps denying. Operator config can only
    remove capability and a plugin manifest never widens it, so the deny
    wins; this names the overlap for the confirmation. Best effort, never
    raises, ``[]`` when the target's config is not reachable here."""
    import plugin_requirements
    if not profile:
        return []
    try:
        tier, _, role = target.partition(":")
        cfg = None
        if tier == "specialist" and _specialist_registry is not None:
            cfg = _specialist_registry.get(role)
        elif tier == "resident":
            import agent as agent_mod
            agents = getattr(getattr(agent_mod, "active_runtime", None), "agents", None) or {}
            cfg = getattr(agents.get(role), "config", None)
        denied = set(getattr(getattr(cfg, "tools", None), "disallowed", None) or [])
        if not denied:
            return []
        if entry is None:
            data = plugin_registry.load_registry()
            raw = data.raw if isinstance(data.raw, dict) else {}
            entry = plugin_requirements.entry_named(raw, name)
        if entry is None:
            return []
        if store_root is None:
            store_root = plugin_registry.snapshot_store_root()
        from plugin_store import expand_tool_names
        bare = plugin_requirements.profile_bare_tools(
            plugin_requirements.manifest_at(store_root, name, entry.get("artifact_id")),
            profile) or []
        servers = plugin_requirements._servers_at(store_root, name, entry.get("artifact_id"))
        rname = entry.get("manifest_name") or name
        return [b for b in bare
                if any(fq in denied for fq in expand_tool_names(rname, servers, b))]
    except Exception:  # noqa: BLE001 — a disclosure must never fail the mutation
        logger.debug("profile_tools_denied_by_config unavailable", exc_info=True)
        return []


def _requirement_candidates_for(name: str, targets: list, *,
                                manifest: "dict | None" = None) -> list:
    """S8: ``requirement_candidates`` for *name*'s requirements on *targets*,
    from the given manifest (the one a mutation just published) or the
    plugin's live artifact. Never raises into a mutation envelope."""
    import plugin_requirements
    try:
        data = plugin_registry.load_registry()
        raw = data.raw if isinstance(data.raw, dict) else {}
        store_root = plugin_registry.snapshot_store_root()
        if manifest is not None:
            from plugin_store import manifest_requires
            requires = manifest_requires(manifest)
        else:
            entry = plugin_requirements.entry_named(raw, name)
            requires = (plugin_requirements.requires_of(store_root, entry)
                        if entry is not None else [])
        return plugin_requirements.requirement_candidates(
            raw, store_root, requires, targets)
    except Exception:  # noqa: BLE001 — a recommendation must never fail the mutation
        logger.warning("requirement candidates unavailable for %s", name,
                       exc_info=True)
        return []


def _dependents_for(name: str, targets: list) -> list:
    """S8: ``dependents`` of *name* on *targets* (see plugin_requirements).
    Never raises into a mutation envelope."""
    import plugin_requirements
    try:
        data = plugin_registry.load_registry()
        return plugin_requirements.dependents(
            data.raw if isinstance(data.raw, dict) else {},
            plugin_registry.snapshot_store_root(), name, targets)
    except Exception:  # noqa: BLE001
        logger.warning("dependents unavailable for %s", name, exc_info=True)
        return []


def _plugin_assign_local(*, name: str, target: str,
                         profile: "str | None" = None) -> "tuple[dict | None, dict]":
    """plugin_assign's guards, all LOCAL and read-only: ``(refusal, state)``.
    One source, called by the core and — #1145 — by the handler first, which
    warns only about a NEW assignment (``was_assigned`` False)."""
    if not plugin_registry.TARGET_RE.match(target or ""):
        return {"ok": False, "kind": "invalid_target", "target": target}, {}
    # S8: ``profile`` names a manifest-declared tool subset for a NEW
    # assignment; omitted or ``full`` means no profile (today's behaviour).
    # It is judged only on the new-assignment branch below: an existing
    # assignment is the no-op it always was, whatever ``profile`` says.
    if profile == plugin_registry.FULL_PROFILE:
        profile = None
    data = plugin_registry.load_registry()
    if not data.valid:
        return {"ok": False, "kind": "registry_invalid"}, {}
    entry = _find_entry(data, name)
    if entry is None:
        return {"ok": False, "kind": "not_registered", "name": name}, {}
    owner = plugin_registry.entry_owner(entry)
    if owner is not None:
        return {"ok": False, "kind": "owned_by_specialist", "owner": owner,
                "detail": (f"{name!r} is managed by {owner}'s bundle — use "
                          "specialist_upgrade / specialist_uninstall")}, {}
    # #923: EVERY executor assignment is refused, bundled entries included —
    # `plugin_assign(name="superpowers", target="executor:configurator")` is the
    # operator composing a worker's plugin set through a tool, which is what the
    # ruling forbids. Nothing Casa ships loses its worker: `seed_defaults`
    # writes the shipped targets straight into the raw document and never calls
    # this handler. The consequence is stated in the docs: an operator who
    # unassigns a shipped default from its worker cannot restore it here.
    if target.startswith("executor:"):
        return {"ok": False, "kind": "executor_target_not_allowed",
                "target": target,
                "detail": ("for now a plugin cannot be assigned to a worker — "
                           "workers use only the plugins Casa ships")}, {}
    targets = entry.setdefault("targets", [])
    was_assigned = target in targets
    import plugin_requirements
    store_root = plugin_registry.snapshot_store_root()
    if was_assigned:
        # S8 (widest access wins): an existing assignment is never changed —
        # whatever ``profile`` says, this is the no-op it always was, and the
        # envelope reports the access the target already holds.
        held = plugin_requirements.held_profile(entry, target)
    else:
        held = profile
        if held is not None and (
                not isinstance(held, str)
                or not plugin_registry.PROFILE_NAME_RE.fullmatch(held)):
            return {"ok": False, "kind": "invalid_profile", "profile": held}, {}
        if held is not None:
            # The profile must exist on the plugin's LIVE artifact before
            # anything is written — a name the manifest does not declare
            # would deny the whole namespace at the next build.
            declared = plugin_requirements.profile_bare_tools(
                plugin_requirements.manifest_at(
                    store_root, name, entry.get("artifact_id")), held)
            if declared is None:
                return {"ok": False, "kind": "profile_missing_in_plugin",
                        "name": name, "profile": held}, {}
    return None, {"data": data, "entry": entry, "targets": targets,
                  "was_assigned": was_assigned, "held": held,
                  "store_root": store_root}


def _plugin_assign_sync(*, name: str, target: str,
                        profile: "str | None" = None) -> dict:
    refusal, state = _plugin_assign_local(name=name, target=target, profile=profile)
    if refusal is not None:
        return refusal
    data, entry, targets = state["data"], state["entry"], state["targets"]
    was_assigned, held = state["was_assigned"], state["held"]
    store_root = state["store_root"]
    import plugin_requirements
    if not was_assigned:
        targets.append(target)
        if held is not None:
            profiles = entry.get("profiles")
            if not isinstance(profiles, dict):
                profiles = entry["profiles"] = {}
            profiles[target] = held
        plugin_registry.save_registry(data)
    profile_tools = (plugin_requirements.profile_bare_tools(
        plugin_requirements.manifest_at(store_root, name, entry.get("artifact_id")),
        held) if held is not None else None)
    return {"ok": True, "name": name, "target": target,
            "targets": list(targets), "was_assigned": was_assigned,
            "profile": held if held is not None else plugin_registry.FULL_PROFILE,
            "profile_tools": profile_tools}


def _plugin_unassign_local(*, name: str, target: str) -> "tuple[dict | None, dict]":
    """plugin_unassign's guards, all LOCAL and read-only: ``(refusal, state)``.
    One source, called by the core and — #1145 — by the handler first, which
    warns only when the target is assigned (``was_assigned`` True)."""
    data = plugin_registry.load_registry()
    if not data.valid:
        return {"ok": False, "kind": "registry_invalid"}, {}
    entry = _find_entry(data, name)
    if entry is None:
        return {"ok": False, "kind": "not_registered", "name": name}, {}
    owner = plugin_registry.entry_owner(entry)
    if owner is not None:
        return {"ok": False, "kind": "owned_by_specialist", "owner": owner,
                "detail": (f"{name!r} is managed by {owner}'s bundle — use "
                          "specialist_upgrade / specialist_uninstall")}, {}
    targets = entry.get("targets") or []
    return None, {"data": data, "entry": entry, "targets": targets,
                  "was_assigned": target in targets}


def _plugin_unassign_sync(*, name: str, target: str) -> dict:
    refusal, state = _plugin_unassign_local(name=name, target=target)
    if refusal is not None:
        return refusal
    data, entry, targets = state["data"], state["entry"], state["targets"]
    was_assigned = state["was_assigned"]
    if was_assigned:
        entry["targets"] = [t for t in targets if t != target]
        # S8: the sibling ``profiles`` map keeps ``keys ⊆ targets`` in the
        # SAME save — a dangling key would invalidate the whole entry.
        profiles = entry.get("profiles")
        if isinstance(profiles, dict):
            profiles.pop(target, None)
            if not profiles:
                del entry["profiles"]
        plugin_registry.save_registry(data)
    return {"ok": True, "name": name, "target": target,
            "was_assigned": was_assigned, "targets": entry.get("targets") or []}


def _plugin_remove_sync(*, name: str) -> dict:
    data = plugin_registry.load_registry()
    if not data.valid:
        return {"ok": False, "kind": "registry_invalid"}
    entry = _find_entry(data, name)
    if entry is None:
        return {"ok": False, "kind": "not_registered", "name": name}
    owner = plugin_registry.entry_owner(entry)
    if owner is not None:
        return {"ok": False, "kind": "owned_by_specialist", "owner": owner,
                "detail": (f"{name!r} is managed by {owner}'s bundle — use "
                          "specialist_upgrade / specialist_uninstall")}
    targets = list(entry.get("targets") or [])
    # A:§3.3 (r1-B8): capture the artifact_id + targets BEFORE the mutation —
    # the caller invalidates grants/challenges by artifact AND by each
    # former target's role only after this commits.
    artifact_id = entry.get("artifact_id")
    # S8: what this plugin's own requirements leave assigned on its former
    # targets — computed BEFORE the entry goes, mentioned, never unassigned.
    import plugin_requirements
    leftover = plugin_requirements.leftover_requirements(
        data.raw, plugin_registry.snapshot_store_root(), entry)
    data.raw["plugins"] = [
        e for e in data.raw.get("plugins", [])
        if not (isinstance(e, dict) and e.get("name") == name)]
    # §3.1 no-resurrection: seeded_defaults is INTENTIONALLY left untouched, so
    # a removed default stays removed across upgrades. Artifact left for GC.
    plugin_registry.save_registry(data)
    # Sol round-3 M: drop the plugin's system-requirement manifest row too, so a
    # removed plugin leaves no stale reconciliation burden. Sol round-4: this is
    # NON-FATAL — a raise here (unwritable manifest) must not bypass the mandatory
    # reload/verify tail that runs after the sync core returns ok.
    # #354 (Sol P1-4): retire the removed plugin's published tools/bin links
    # too (ownership-checked; best-effort) — capture the rows before the prune.
    removed_bins = {p.get("verify_bin") for p in read_sysreq_manifest()["plugins"]
                    if p.get("name") == name and p.get("verify_bin")}
    _safe_remove_manifest(name)
    for stale in removed_bins:
        retire_stale_bin(stale, name, Path("/config/tools"))
    # #676: the disclosure rides the sync core's payload, added AFTER
    # save_registry — the survival fact is true the moment the registry commits,
    # so a committed-but-not-ready outcome (INV-TOOL-004) must carry it too, and
    # `core.update(seq)` in the async wrapper cannot drop it.
    return {"ok": True, "name": name, "targets": targets,
            "artifact_retained": True, "artifact_id": artifact_id,
            "leftover_requirements": leftover,
            **_plugin_data_disclosure(_PLUGIN_DATA_NOTE_COMMITTED)}


def _tool_plugin_list() -> dict:
    data = plugin_registry.load_registry()
    seeded = set(data.raw.get("seeded_defaults") or [])
    plugins = []
    for e in data.raw.get("plugins", []):
        if not isinstance(e, dict):
            continue
        name, aid = e.get("name"), e.get("artifact_id")
        store_dir = plugin_registry.STORE_ROOT / str(name) / str(aid)
        plugins.append({
            "name": name, "version": e.get("version"),
            "revision": (e.get("source") or {}).get("revision"),
            "targets": e.get("targets") or [], "artifact_id": aid,
            # #923: `targets` keeps its present meaning — the operator's STORED
            # assignment, read from the registry document — because the shipped
            # configurator doctrine already reads that field, and redefining a
            # field a reader cannot see change is a silent misread. The
            # distinction is ADDED beside it: what Casa actually serves, and
            # what it stores but ignores. `targets` alone would imply an
            # assignment that is not served; effective alone would imply the
            # operator's configuration was deleted, which it never is.
            "effective_targets": plugin_registry.effective_targets(e),
            "ignored_targets": list(
                plugin_registry.ignored_executor_targets(e)),
            # S8: the access profile each target holds (absent = full).
            "profiles": dict(e.get("profiles") or {})
            if isinstance(e.get("profiles"), dict) else {},
            "artifact_present": store_dir.is_dir(),
            "seeded_default": name in seeded,
            # The specialist whose bundle this plugin comes with (it moves with
            # that specialist's upgrades), or None for a plugin of its own.
            "owner": plugin_registry.entry_owner(e),
        })
    return {
        "registry_valid": data.valid, "plugins": plugins,
        "issues": [{"name": i.name, "reason_code": i.reason_code}
                   for i in data.entry_issues],
    }


@tool(
    "plugin_assign",
    "Assign a registered plugin to a target (resident: or specialist:). For "
    "now a plugin cannot be assigned to a worker (executor:) — workers use "
    "only the plugins Casa ships with them. An optional `profile` names one "
    "of the plugin's declared access profiles for a NEW assignment (omit it "
    "for full access); an existing assignment is never changed and the "
    "result reports the access the target already holds. A profile applies "
    "to the target's sessions built from now on."
    + _plugin_change_tool_note("added"),
    {"type": "object",
     "properties": {
         "name": {"type": "string"},
         "target": {"type": "string"},
         "profile": {"type": "string"},
         "acknowledged_conversations": _ACKNOWLEDGED_SCHEMA},
     "required": ["name", "target"]},
)
async def plugin_assign(args: dict) -> dict:
    def reached() -> list:
        refusal, state = _plugin_assign_local(
            name=args["name"], target=args["target"], profile=args.get("profile"))
        if refusal is not None or state["was_assigned"]:
            return []                     # the core refuses, or changes nothing
        return _specialist_slugs([args["target"]])

    async with _PLUGIN_TOOLS_LOCK:
        # #1145: warn first when a NEW assignment reaches a specialist with
        # open conversations; a refused or no-op call is the core's as before.
        core, ack, seen = await _plugin_gate_phase(
            "plugin_assign", "added", args, reached)
        if core is None:
            core = await _to_thread_settled(
                _plugin_assign_sync, name=args["name"], target=args["target"],
                profile=args.get("profile"))
        elif core.get("kind") == OPEN_CONVERSATIONS_PENDING:
            return _result(core)
        if core.get("ok") is not True:
            # Spec §E: the pinned payload shape holds on EVERY path.
            core.setdefault("kind", "unknown")
            core.setdefault("activation_committed", False)
            core.setdefault("runtime_ready", False)
            core.setdefault("verify", {})
            return _result(_after_plugin_change(
                core, "added", args["name"], [], ack, seen))
        seq = await _reload_and_verify_targets(
            core["name"], [core["target"]], expect="present")
        core.update(seq)
        core["requirement_candidates"] = await _to_thread_settled(
            _requirement_candidates_for, core["name"], [core["target"]])
        core["profile_tools_denied_by_config"] = await _to_thread_settled(
            _profile_tools_denied_by_config, core["name"], core["target"],
            None if core.get("profile") == plugin_registry.FULL_PROFILE
            else core.get("profile"))
        return _result(_after_plugin_change(
            core, "added", core["name"],
            [] if core.get("was_assigned") else _specialist_slugs([core["target"]]),
            ack, seen))


@tool(
    "plugin_unassign",
    "Remove a plugin's assignment to one target (the plugin stays registered)."
    + _plugin_change_tool_note("removed"),
    # #1145: an explicit JSON Schema, so acknowledged_conversations is optional
    # (the shorthand {key: type} form marks every key required).
    {"type": "object",
     "properties": {
         "name": {"type": "string"},
         "target": {"type": "string"},
         "acknowledged_conversations": _ACKNOWLEDGED_SCHEMA},
     "required": ["name", "target"]},
)
async def plugin_unassign(args: dict) -> dict:
    def reached() -> list:
        refusal, state = _plugin_unassign_local(
            name=args["name"], target=args["target"])
        if refusal is not None or not state["was_assigned"]:
            return []                     # the core refuses, or changes nothing
        return _specialist_slugs([args["target"]])

    async with _PLUGIN_TOOLS_LOCK:
        # #1145: warn first when the unassigned target is a specialist with
        # open conversations; a refused or no-op call is the core's as before.
        core, ack, seen = await _plugin_gate_phase(
            "plugin_unassign", "removed", args, reached)
        if core is None:
            core = await _to_thread_settled(
                _plugin_unassign_sync, name=args["name"], target=args["target"])
        elif core.get("kind") == OPEN_CONVERSATIONS_PENDING:
            return _result(core)
        if core.get("ok") is not True:
            # Spec §E: the pinned payload shape holds on EVERY path.
            core.setdefault("kind", "unknown")
            core.setdefault("activation_committed", False)
            core.setdefault("runtime_ready", False)
            core.setdefault("verify", {})
            return _result(_after_plugin_change(
                core, "removed", args["name"], [], ack, seen))
        # A:§3.3 (r2-B5): invalidate by the (normalized) unassigned role only
        # — the plugin/artifact stays valid for its OTHER targets. A NO-OP
        # unassign (plugin was never assigned to this target) invalidates
        # NOTHING.
        if core.get("was_assigned"):
            _invalidate_lifecycle(roles=[core["target"]])
        seq = await _reload_and_verify_targets(
            core["name"], [core["target"]], expect="absent")
        core.update(seq)
        # S8: the plugins whose requirements named this one on this target —
        # the consequence is stated once; nothing blocks.
        core["dependents"] = await _to_thread_settled(
            _dependents_for, core["name"], [core["target"]])
        return _result(_after_plugin_change(
            core, "removed", core["name"],
            _specialist_slugs([core["target"]]) if core.get("was_assigned") else [],
            ack, seen))


# --- #1046: erasing a plugin's data at uninstall -----------------------------------
#
# A plugin may declare the eraser (`casa.eraseTool`) that erases everything it
# holds and revokes what it can at its providers. Uninstalling it asks ONE
# Casa-owned question (plugin_erase_consent): Keep data / Erase everything / Cancel.
# An Erase tap is the authorization; the eraser then runs in the background
# (plugin_erasure) and its outcome continues the configurator engagement. The
# plugin is removed only by a finishing call that finds a complete erasure for
# the artifact the registry still resolves — Casa sequences the plugin's
# eraser and never erases anything itself.
#
# #1067: a plugin may also declare a data-only eraser (`casa.eraseDataOnlyTool`)
# that keeps what a reinstall needs to carry on without signing in again. The
# question then offers "Erase data, keep sign-ins" beside "Erase everything",
# each only when every erasing plugin in the uninstall declares that kind. The
# tap records the kind, so `erase_data=true` never lets the model choose it.
# After a removal that followed an Erase everything, Casa clears the plugin's
# plugin-env.conf references and reloads the plugin environment.

_ERASED_EVERYTHING_NOTE = (
    "The plugin's own eraser reported everything it held erased, sign-ins "
    "included, before the removal (erase_report — relay it to the operator "
    "verbatim), and Casa cleared its plugin-env.conf references "
    "(env_references_cleared). Home Assistant backups taken before now still "
    "contain the data; Casa did not delete them.")
_ERASED_EVERYTHING_UNCLEARED_NOTE = (
    "The plugin's own eraser reported everything it held erased, sign-ins "
    "included, before the removal (erase_report — relay it to the operator "
    "verbatim), but Casa could NOT clear its plugin-env.conf references "
    "(env_references_not_cleared): remove each with "
    "remove_plugin_env_reference, then run casa_reload(scope=\"plugin_env\"). "
    "Home Assistant backups taken before now still contain the data; Casa did "
    "not delete them.")
_ERASED_DATA_ONLY_NOTE = (
    "The plugin's own eraser reported its data erased before the removal, "
    "keeping its sign-ins so a reinstall carries on without signing in again "
    "(erase_report — relay it to the operator verbatim). Casa kept its "
    "plugin-env.conf references for the same reason. Home Assistant backups "
    "taken before now still contain the data; Casa did not delete them.")
# #1073: appended to whichever note applies when an eraser that reported a
# complete erasure named vault items it left because it cannot prove it
# created them.
_UNRECORDED_VAULT_ITEMS_NOTE = (
    " The eraser left in place the vault items listed in "
    "unrecorded_vault_items: nothing records the plugin creating them, so they "
    "cannot be told apart from items the operator made. Tell the operator they "
    "were left for deletion by hand (found=true: in the vault; found=null: the "
    "vault could not be checked) — the plugin is not gone without a trace while "
    "they remain.")


def _erase_specs_for(entries: "list[dict]") -> "list":
    """An :class:`plugin_erasure.EraseSpec` for every registry entry in
    *entries* whose resolved artifact declares a valid ``casa.eraseTool`` or
    ``casa.eraseDataOnlyTool`` (#1067) — each eraser with empty tool names
    when it has no MCP server to call it on, so its erasure can never
    complete; entries without one are skipped (their removal is today's)."""
    import plugin_erasure
    from plugin_grants import plugin_tool_names, protected_map
    from plugin_store import (StoreError, manifest_erase_data_only_tool,
                              manifest_erase_tool)
    resolution = plugin_registry.resolve_all()
    by_name = {rp.name: rp for rp in getattr(resolution, "plugins", None) or []}
    protected = protected_map(resolution)

    def _declared(fn, rp) -> str:
        try:
            return fn(rp.manifest) or ""
        except StoreError:
            return ""

    def _eraser(rp, tool: str):
        # A declared eraser with no MCP server to run it on still counts: the
        # uninstall asks, and its erasure can never complete (not dispatched)
        # — never silently an ordinary removal.
        if not tool:
            return (), False, None
        names = plugin_tool_names(rp, tool)
        summary = next((protected[n]["summary"] for n in names if n in protected),
                       None)
        return names, any(n in protected for n in names), summary

    specs = []
    for entry in entries:
        rp = by_name.get(entry.get("name"))
        if rp is None or rp.artifact_id != entry.get("artifact_id"):
            continue
        tool = _declared(manifest_erase_tool, rp)
        data_tool = _declared(manifest_erase_data_only_tool, rp)
        if not tool and not data_tool:
            continue
        names, prot, summary = _eraser(rp, tool)
        d_names, d_prot, d_summary = _eraser(rp, data_tool)
        specs.append(plugin_erasure.EraseSpec(
            name=rp.name, artifact_id=rp.artifact_id,
            targets=tuple(entry.get("targets") or ()), tool_names=names,
            protected=prot, tool=tool, summary=summary, data_tool=data_tool,
            data_tool_names=d_names, data_protected=d_prot,
            data_summary=d_summary))
    return specs


def _offered_erase_choices(specs: list) -> "tuple[int, ...]":
    """The question's buttons for *specs* (#1067): an erase option only when
    EVERY erasing plugin declares that kind of eraser — a plugin lacking it
    could honour neither "keep sign-ins" nor "everything"."""
    import plugin_erase_consent as pec
    import plugin_erasure
    kinds = {pec.ERASE_DATA_ONLY: plugin_erasure.DATA_ONLY,
             pec.ERASE: plugin_erasure.EVERYTHING}
    return tuple(c for c in pec.DEFAULT_ORDER
                 if c not in kinds or all(s.offers(kinds[c]) for s in specs))


def _plugin_env_names(rp) -> "set[str]":
    """Every plugin-env.conf name a resolved plugin uses (#1067): the names its
    ``.mcp.json`` references, in either form, and its ``casa.setupProvides``
    — a setup-provided value need not appear in the launch configuration."""
    from pathlib import Path as _Path
    import plugin_env_extractor
    from plugin_store import StoreError, manifest_setup_provides
    names: "set[str]" = set()
    mcp_json = _Path(rp.path) / ".mcp.json"
    if mcp_json.is_file():
        try:
            names |= plugin_env_extractor.extract_referenced_env_vars(mcp_json)
        except Exception:  # noqa: BLE001 — a malformed tree uses no names
            logger.debug("env-name extraction failed (%s)", mcp_json,
                         exc_info=True)
    try:
        names |= set(manifest_setup_provides(rp.manifest or {}))
    except StoreError:
        pass
    return names


def _env_names_to_clear(erased: "set[str]") -> "list[str]":
    """Under the mutation lock, before the removal: the plugin-env.conf names
    the plugins in *erased* use and no other resolved plugin does. A name a
    remaining plugin also uses is never cleared."""
    mine: "set[str]" = set()
    others: "set[str]" = set()
    for rp in getattr(plugin_registry.resolve_all(), "plugins", None) or []:
        (mine if rp.name in erased else others).update(_plugin_env_names(rp))
    return sorted(mine - others)


async def _clear_env_references(names: "list[str]") -> "tuple[list[str], list[str]]":
    """Under the mutation lock, after a removal that followed an Erase
    everything: delete *names*' plugin-env.conf lines. Returns ``(cleared,
    failed)`` — the names that had a line and lost it, and, when the rewrite
    failed, every name it was asked to clear. Never raises: the removal has
    already committed, so a failure is reported, never hidden."""
    if not names:
        return [], []
    import plugin_env_conf
    try:
        # Diff r1 (Astra S2): a thread cannot be cancelled, so a cancelled
        # caller must not release the mutation lock while the rewrite still
        # runs — a plugin installed meanwhile could lose a line it just wired
        # to a name computed before it existed. Settle the rewrite first.
        return await _settle_through_cancellation(asyncio.to_thread(
            plugin_env_conf.remove_entries, names)), []
    except Exception:  # noqa: BLE001 — the removal already committed
        logger.exception("clearing plugin-env.conf references failed")
        return [], list(names)


async def _reload_plugin_env_after_clear() -> bool:
    """After the mutation lock is released: the ``plugin_env`` reload that
    drops the cleared names from the effective environment, entered the way
    ``casa_reload`` enters it (the handler takes the plugin guard, so it
    cannot run under the raw lock the removal held). True when it ran ok."""
    try:
        import agent as agent_mod
        from reload import dispatch
        runtime = getattr(agent_mod, "active_runtime", None)
        if runtime is None:
            return False
        async with _plugin_tools_reload_guard("plugin_env"):
            result = await dispatch("plugin_env", runtime=runtime, role=None,
                                    include_env=False)
        await _regenerate_plugin_health_after_reload("plugin_env", result)
        return isinstance(result, dict) and result.get("status") == "ok"
    except Exception:  # noqa: BLE001 — the removal already committed
        logger.exception("plugin_env reload after clearing references failed")
        return False


def _owned_entries_now(slug: str) -> "list[dict]":
    data = plugin_registry.load_registry()
    return plugin_registry.owned_entries_for(slug, data) if data.valid else []


def _configurator_deliverer(question: str, asked: str):
    """Casa continuing the configurator for the erase question *question*: a
    delegation Casa starts on the configurator's lane, keyed by the question,
    so the eraser's result waits behind a choice run still going. *asked* is
    the operator's own words that led to the uninstall. The returned function
    starts it with *text* and returns whether it was taken; if the run cannot
    be started, the operator is told *fallback* directly."""
    import configurator_continue

    def _deliver(text: str, fallback: str) -> bool:
        async def _told(outcome: str) -> None:
            if outcome in ("busy", "failed"):
                import casa_core
                await casa_core.operator_notify(_channel_manager, fallback)
        outcome = configurator_continue.start(
            text, key=f"erase:{question}", asked=asked, on_outcome=_told)
        return outcome in ("started", "queued")
    return _deliver


def _erase_call(tool: str, arg: str, name: str, erase: bool, extra: str = "") -> str:
    # #1095: *extra* carries the specialist uninstall's acknowledged
    # conversations into every call a continuation tells the model to make, so
    # a confirmed uninstall is not warned again on its Keep or finishing call.
    return f"{tool}({arg}={name!r}, erase_data={'true' if erase else 'false'}{extra})"


def _choice_continuation(tool: str, arg: str, name: str, choice: int,
                         extra: str = "") -> str:
    import plugin_erase_consent as pec
    if choice == pec.KEEP:
        return (f"The operator chose Keep data. Call {_erase_call(tool, arg, name, False, extra)} "
                "now to uninstall and keep the data, then finish the recipe.")
    if choice in (pec.ERASE, pec.ERASE_DATA_ONLY):
        label = pec.LABELS[choice]
        return (f"The operator chose {label}. Call {_erase_call(tool, arg, name, True, extra)} "
                "now: it starts the plugin's eraser and removes nothing yet. Then "
                "end your turn with one short line saying the data is being "
                "erased; Casa continues you with the eraser's result.")
    return (f"The operator cancelled uninstalling {name!r}. Nothing was removed; "
            "tell them so and finish.")


async def _deliver_erasure_outcome(tool: str, name: str, outcomes: list,
                                   deliver, extra: str = "") -> None:
    """Continue the configurator with what the eraser(s) said; if it cannot
    be started, tell the operator directly. Never raises."""
    arg = "slug" if tool == "specialist_uninstall" else "name"
    try:
        if outcomes and all(o.verdict == "complete" for o in outcomes):
            reports = "; ".join(f"{o.name}: {o.report}" for o in outcomes)
            model_text = (
                f"The eraser reported the erasure complete. Its report, to relay "
                f"to the operator verbatim: {reports}. Now call "
                f"{_erase_call(tool, arg, name, True, extra)} to finish the uninstall.")
            # Casa's own words only: this DM is a Casa notice, and the
            # plugin's report is plugin-authored text.
            operator_text = (
                f"Erasing {name}'s data finished, but the configurator could not "
                "be resumed to finish the uninstall — ask it to uninstall "
                f"{name} again; the plugin's report is relayed there.")
        else:
            last = outcomes[-1] if outcomes else None
            plugin = last.name if last else name
            report = last.report if last else ""
            verdict = last.verdict if last else "not_dispatched"
            model_text = (
                f"Erasing {plugin!r}'s data did not complete ({verdict}). Nothing "
                f"was removed. The plugin's report, verbatim for the operator: "
                f"{report} — Relay it verbatim, then ask the operator whether to "
                "try again later (run the uninstall again, which asks again) or "
                "uninstall anyway keeping whatever is left "
                f"({_erase_call(tool, arg, name, False, extra)}).")
            operator_text = (
                f"Erasing {plugin}'s data did not complete, so nothing was "
                "removed, and the configurator could not be resumed to relay "
                "the plugin's report — run the uninstall again to see it.")
        if deliver(model_text, operator_text):
            return
        import casa_core
        await casa_core.operator_notify(_channel_manager, operator_text)
    except Exception:  # noqa: BLE001 — a background task: log, never raise
        logger.exception("erasure outcome delivery failed (%s %s)", tool, name)


_ERASE_TASKS: "set[asyncio.Task]" = set()


def _take_complete_erasures(specs: list, question: str) -> "list | None":
    """Under the mutation lock, immediately before a removal: ``(plugin,
    report, kind, unrecorded vault items)`` of a complete erasure for EVERY erasing plugin at the
    artifact the registry resolves now, run for the still-open *question*,
    consumed — or ``None`` (nothing consumed) when any is missing, or when no
    erasing plugin remains."""
    import plugin_erasure
    rows = [(f"plugin:{s.name}", s.artifact_id, question) for s in specs]
    if not rows or not all(plugin_erasure.RECORDS.has_complete(*r) for r in rows):
        return None
    kinds = [plugin_erasure.RECORDS.complete_kind(*r) for r in rows]
    taken = [plugin_erasure.RECORDS.take_complete(*r) for r in rows]
    return [(s.name, report, k, unrecorded)
            for s, (report, unrecorded), k in zip(specs, taken, kinds)]


_ERASE_UNAVAILABLE = {
    "ok": False, "kind": "erase_unavailable",
    "detail": ("erase_data=true, but no plugin being uninstalled declares an "
               "eraser now (it may have been updated since the question), so "
               "nothing can be erased and nothing was removed. Uninstall "
               "without erase_data; the operator is asked again if an eraser "
               "is declared.")}


async def _erase_gate(*, tool: str, arg: str, name: str, subject: str,
                      what: str, specs: list, erase,
                      held: "list | None" = None,
                      confirm: "Callable[[bool], dict | None] | None" = None,
                      call_extra: str = "",
                      ) -> "tuple[dict | None, list]":
    """The erase step in front of an uninstall. The caller holds the mutation
    lock the removal commits under, and computed *specs* from the registry
    under it, so what this decides is what the removal removes: no update can
    land between the two. Returns ``(payload, reports)``: a payload is the
    refusal / pending result to return with nothing removed; ``None`` means
    proceed, with *reports* ``[(plugin, report, kind, unrecorded)]`` of the
    complete erasures it consumed (empty when nothing was erased).

    #1095: *confirm* (a specialist uninstall's open-conversation check,
    synchronous) runs where the call would otherwise go on to remove or ask:
    after the close of the Keep and no-eraser branches, and in place of opening
    the question — which it voids instead — on the asking branch. A refusal
    therefore has exactly the base call's effect on erase state: never on an
    erase_data=true call, and never where the operator's DM is unavailable
    (that refusal comes first, unchanged)."""
    import plugin_erase_consent as pec
    import plugin_erasure
    questions = plugin_erasure.QUESTIONS
    if erase is False:
        # Keep data: today's removal. It answers — and so closes — whatever
        # question is open, voiding every tap, run and record of it.
        had = questions.current(subject) is not None
        questions.close(subject)
        refusal = confirm(had) if confirm is not None else None
        return refusal, []
    if not specs:
        if erase is True:
            # #1070: nothing can finish this question's erasure now.
            plugin_erasure.FENCE.lift_completed(subject)
            return dict(_ERASE_UNAVAILABLE), []
        # An ordinary removal answers — and so closes — whatever question is
        # open (#1070 diff r1: a complete erasure's fence must not survive
        # into a reinstall).
        had = questions.current(subject) is not None
        questions.close(subject)
        refusal = confirm(had) if confirm is not None else None
        return refusal, []
    if erase is True:
        open_q = questions.current(subject)
        taken = _take_complete_erasures(specs, open_q) if open_q else None
        if taken is not None:
            # #1070: the fence survives the close — the finishing call
            # settles it, by this token, once the removal settled.
            token = plugin_erasure.FENCE.hold(
                [s.name for s in specs], subject, open_q)
            if held is not None:
                held.append(token)
            questions.close(subject, open_q)
            return None, taken
    # Casa's own question, through Casa's bot in the operator's direct chat.
    channel, address, problem = _casa_card_target()
    if problem is not None:
        return {"ok": False, "kind": "consent_channel_unavailable",
                "detail": ("the operator's Telegram DM is not available, so the "
                           "erase-data question cannot be asked; nothing was "
                           "removed. Fix the Telegram channel, or uninstall with "
                           "erase_data=false to keep the data.")}, []
    chat_id = operator_id = address.chat_id
    artifacts = tuple(s.artifact_id for s in specs)
    # the operator's own words in this run: what a continuation's row keeps
    origin = _snapshot_origin() or {}
    asked = str(origin.get("user_text") or "")
    if erase is None:
        if confirm is not None:
            had = questions.current(subject) is not None
            refusal = confirm(had)
            if refusal is not None:
                # #1095: base would replace the question here — voiding every
                # tap, run and record of the old one; the refusal voids it the
                # same way and asks nothing (no new question, no DM).
                questions.close(subject)
                return refusal, []
        # A fresh question replaces any open one: every tap, run and record of
        # an earlier question — or of an earlier installation of the same
        # artifact — is void, so only the answer to THIS question decides.
        question = questions.open(subject)
        key = pec.EraseChoiceKey(operator_id=operator_id, bot_id=address.bot_id,
                                 chat_id=chat_id, subject=subject, artifacts=artifacts,
                                 question=question)
        choices = _offered_erase_choices(specs)
        erasers = []
        for s in specs:
            if pec.ERASE_DATA_ONLY in choices:
                erasers.append((s.name, s.data_tool, s.data_summary))
            if pec.ERASE in choices:
                erasers.append((s.name, s.tool, s.summary))
        text = pec.render_erase_choice(what, erasers, choices)
        card_request = pec.erase_card_request(what, erasers, choices,
                                              requester_name=_requester_name(origin))
        deliver = _configurator_deliverer(question, asked)

        async def _continue(choice: int) -> bool:
            if choice in (pec.CANCEL, pec.KEEP):
                # Keep and Cancel answer the question now: an erase already
                # approved and queued must not run behind them.
                questions.close(subject, question)
            return deliver(
                _choice_continuation(tool, arg, name, choice, call_extra),
                f"The configurator could not be resumed with your choice about {name}'s "
                f"data; nothing was removed — ask it to uninstall {name} again.")
        try:
            handle = pec.prompt_erase_choice(
                coordinator=CHALLENGES, channel=channel, key=key, text=text,
                continue_cb=_continue,
                choices=choices, card_request=card_request)
        except Exception as exc:  # noqa: BLE001 — structured, never a raise
            logger.exception("erase-choice prompt failed to post")
            return {"ok": False, "kind": "consent_prompt_failed",
                    "detail": f"{type(exc).__name__}: {exc}"}, []
        failure = await _settle_install_consent_post(handle)
        if failure is not None:
            return failure, []
        return {"ok": False, "kind": "erase_choice_pending",
                "detail": ("Nothing was removed yet. The operator was asked in "
                           "their DM whether to also erase the data "
                           f"({', '.join(s.name for s in specs)}); Casa continues "
                           "you with their choice. End your turn now with one "
                           "short line saying what you asked.")}, []
    question = questions.current(subject)
    key = pec.EraseChoiceKey(operator_id=operator_id, bot_id=address.bot_id,
                             chat_id=chat_id, subject=subject, artifacts=artifacts,
                             question=question or "")
    choice = pec.CHOICES.consume_erase(key) if question is not None else None
    if choice is None:
        # #1070: a complete erasure this call could not consume (the plugin
        # changed under it) can no longer finish; an episode still running
        # keeps its fence.
        plugin_erasure.FENCE.lift_completed(subject)
        return {"ok": False, "kind": "erase_not_confirmed",
                "detail": ("erase_data=true needs the operator's Erase tap on the "
                           "question Casa posts, for this exact version, and runs "
                           "once per tap — call again without erase_data to ask "
                           "them. Nothing was removed.")}, []
    # #1067: the kind is the one the operator tapped, never the model's.
    kind = (plugin_erasure.DATA_ONLY if choice == pec.ERASE_DATA_ONLY
            else plugin_erasure.EVERYTHING)
    if not all(s.offers(kind) for s in specs):
        plugin_erasure.FENCE.lift_completed(subject)
        return dict(_ERASE_UNAVAILABLE), []
    projected = [s.for_kind(kind) for s in specs]
    # #1070: from the dispatch until the uninstall removes them, or the
    # erasure ends not complete, the erasing plugins' tools are refused on
    # every turn but the episode's own eraser call.
    fenced = [s.name for s in specs]
    fence = plugin_erasure.FENCE
    fence.raise_(fenced, subject, question)
    deliver = _configurator_deliverer(question, asked)

    async def _run() -> None:
        complete = False
        try:
            outcomes = await plugin_erasure.run_erase_episode(
                projected, (chat_id, operator_id), question, subject, kind=kind)
            complete = bool(outcomes) and all(
                o.verdict == "complete" for o in outcomes)
            if complete:
                fence.completed(fenced, question)
        finally:
            if not complete:
                fence.lift(fenced, question)
        await _deliver_erasure_outcome(tool, name, outcomes, deliver, call_extra)
    task = asyncio.get_running_loop().create_task(_run())
    _ERASE_TASKS.add(task)
    task.add_done_callback(_ERASE_TASKS.discard)
    return {"ok": False, "kind": "erasure_running",
            "detail": ("The plugin's eraser is running; nothing is removed yet. "
                       "Casa brings you its result in a new message — wait "
                       "for it, then follow what it says.")}, []


def _apply_erasure_to_disclosure(payload: dict, reports: list,
                                 cleared: "list[str] | None" = None,
                                 not_cleared: "list[str] | None" = None) -> None:
    """A removal that followed a complete erasure: the erased plugins are no
    longer 'data may remain'; their reports are relayed. *reports* are
    ``(plugin, report, kind, unrecorded vault items)``; after an Erase
    everything, *cleared* names the plugin-env.conf references Casa cleared
    (#1067) and *not_cleared* those a failed rewrite left behind. Vault items
    an eraser left because it cannot prove it created them (#1073) are listed
    and qualify the note, whichever kind ran."""
    if not reports:
        return
    erased = {r[0] for r in reports}
    everything = _erased_everything(reports)
    payload["erasure"] = "complete"
    payload["erasure_kind"] = "everything" if everything else "data_only"
    if everything:
        payload["env_references_cleared"] = list(cleared or [])
        note = _ERASED_EVERYTHING_NOTE
        if not_cleared:
            payload["env_references_not_cleared"] = list(not_cleared)
            note = _ERASED_EVERYTHING_UNCLEARED_NOTE
    else:
        note = _ERASED_DATA_ONLY_NOTE
    unrecorded = [{"plugin": r[0], "title": title, "found": found}
                  for r in reports if len(r) > 3 for title, found in r[3]]
    if unrecorded:
        payload["unrecorded_vault_items"] = unrecorded
        note += _UNRECORDED_VAULT_ITEMS_NOTE
    if len(reports) == 1 and "slug" not in payload:
        payload["erase_report"] = reports[0][1]
    else:
        payload["erase_reports"] = [{"name": r[0], "report": r[1]} for r in reports]
    remaining = [n for n in payload.get("plugin_data_plugins") or [] if n not in erased]
    if "plugin_data_plugins" in payload and remaining:
        payload["plugin_data_plugins"] = remaining
        payload["erased_note"] = note
        return
    for k in ("plugin_data_may_remain", "provider_revocation_performed",
              "plugin_data_plugins"):
        payload.pop(k, None)
    payload["plugin_data_note"] = note


def _erased_everything(reports: list) -> bool:
    """Whether the erasures in *reports* — ``(plugin, report, kind, …)``, all of
    one question and so of one kind — ran the plugins' casa.eraseTool."""
    import plugin_erasure
    return bool(reports) and all(
        (r[2] if len(r) > 2 else plugin_erasure.EVERYTHING)
        == plugin_erasure.EVERYTHING for r in reports)


@tool(
    "plugin_remove",
    "Remove a plugin from the registry entirely (artifact retained for GC). Does NOT delete the "
    "plugin's CLI-managed persistent data directory (CLAUDE_PLUGIN_DATA), which may hold stored "
    "authorizations such as OAuth tokens: that data survives and a reinstall re-attaches to it. "
    "Performs no provider-side revocation. A plugin that declares an eraser asks the operator "
    "first, in the DM, with the options Casa offers for this uninstall: call without "
    "erase_data and wait for Casa to continue with their choice."
    + _plugin_change_tool_note("removed", removal=True),
    {"type": "object", "properties": {
        "name": {"type": "string"},
        "erase_data": {"type": "boolean"},
        "acknowledged_conversations": _ACKNOWLEDGED_SCHEMA},
     "required": ["name"]},
)
async def plugin_remove(args: dict) -> dict:
    import plugin_erasure
    held: list = []
    try:
        return await _plugin_remove_erasing(args, held)
    finally:
        # #1070: the fence THIS call held settles once the removal did,
        # whatever it returned — cancelled included: the commit is drained
        # through cancellation, and a settled fence stays up while the
        # committed registry lacks the plugin.
        for token in held:
            plugin_erasure.FENCE.settle(token)


async def _plugin_remove_erasing(args: dict, held: list) -> dict:
    ack, bad = _acknowledged(args)
    if bad is not None:
        return _result({**bad, "activation_committed": False,
                        "runtime_ready": False, "verify": {}})
    # #1145: what the removal's result names afterwards; `confirm` below
    # replaces it, and an erase_data=true call (which never confirms) keeps it.
    after = {"seen": set(ack or ())}
    name = args["name"]

    def _named(result: dict) -> dict:
        payload = json.loads(result["content"][0]["text"])
        committed = (payload.get("ok") is True
                     or payload.get("activation_committed") is True)
        keys = set(payload)
        _after_plugin_change(
            payload, "removed", name,
            _specialist_slugs(payload.get("targets")) if committed else [],
            ack, after["seen"])
        return result if set(payload) == keys else _result(payload)

    async with _PLUGIN_TOOLS_LOCK:
        # #1046: the erase step runs under the same lock as the removal, on the
        # registry as it stands under it — the decision and the removal see
        # one state, so no update can slip between them.
        data = plugin_registry.load_registry()
        entry = _find_entry(data, name) if data.valid else None
        reports: list = []
        if entry is not None and plugin_registry.entry_owner(entry) is None:
            slugs = _specialist_slugs(entry.get("targets"))

            def _confirm(withdrew: bool) -> "dict | None":
                # #1145: run by the erase gate where the call would go on to
                # remove or ask — never on an erase_data=true call — under
                # this lock, on the event loop.
                refusal, after["seen"] = _plugin_change_gate(
                    "plugin_remove", "removed", name, slugs, ack,
                    removal=True, withdrew=withdrew)
                return refusal

            gate, reports = await _erase_gate(
                tool="plugin_remove", arg="name", name=name,
                subject=f"plugin:{name}", what=f"the plugin {name}",
                specs=_erase_specs_for([entry]), erase=args.get("erase_data"),
                held=held, confirm=_confirm,
                call_extra=(f", acknowledged_conversations={ack!r}" if ack else ""))
            if gate is not None:
                return _result(gate)
        # #1067: after an Erase everything, the names to clear are computed
        # before the removal (the resolved snapshot still holds the plugin)
        # and cleared once it committed, both under this lock.
        clear = (_env_names_to_clear({name})
                 if reports and _erased_everything(reports) else [])
        result = await _plugin_remove_unit(args)
        if not reports:
            return _named(result)
        payload = json.loads(result["content"][0]["text"])
        if payload.get("ok") is not True:
            return _named(result)
        cleared, not_cleared = await _clear_env_references(clear)
    # The reload takes the plugin guard itself, so it runs after the lock.
    if cleared and not await _reload_plugin_env_after_clear():
        payload["env_reload_ok"] = False
    _apply_erasure_to_disclosure(payload, reports, cleared, not_cleared)
    return _named(_result(payload))


async def _plugin_remove_unit(args: dict) -> dict:
    """The removal proper; the caller holds ``_PLUGIN_TOOLS_LOCK``."""
    # #928 (design + seam round, astra + terra): the registry write runs in
    # a THREAD, and a thread cannot be cancelled — a cancellation on that
    # await cannot stop the removal committing, it only stops US from
    # continuing. So the commit and EVERYTHING IT OWES DURABLY are one
    # child unit, drained through repeated cancellation; only the runtime
    # reload below is abandonable.
    #
    # Two review rounds found the same shape here — a cancellation
    # stranding one piece of a committed removal's teardown (the setup
    # retire, then the callback revocation) — because the settlement used
    # to be SPLIT ACROSS the reload. Naming the unit is what stops a third.
    core = await _settle_through_cancellation(
        _remove_and_settle(args["name"]))
    if core.get("ok") is not True:
        # Spec §E: the pinned payload shape holds on EVERY path.
        core.setdefault("kind", "unknown")
        core.setdefault("activation_committed", False)
        core.setdefault("runtime_ready", False)
        core.setdefault("verify", {})
        return _result(core)
    seq = await _reload_and_verify_targets(
        core["name"], core["targets"], expect="absent")
    core.update(seq)
    # S8: the plugins whose requirements named this one on its former
    # targets — stated once; the entry is already gone, nothing blocks.
    core["dependents"] = await _to_thread_settled(
        _dependents_for, core["name"], list(core.get("targets") or []))
    return _result(core)


async def _remove_and_settle(name: str) -> dict:
    """The registry removal and every durable consequence it owes, as ONE unit
    (#928). Runs as a CHILD of `plugin_remove` under `_settle_through_
    cancellation`, so a cancelled caller cannot strand any of it.

    Nothing here acquires `_PLUGIN_TOOLS_LOCK` or enters `_plugin_tools_guard`,
    and nothing here dispatches a reload: the parent holds the RAW lock (so no
    task-identity owner is recorded) and a child that re-entered a guard keyed
    on task identity would deadlock against its own parent — the helper's own
    docstring. Every step is BOUNDED: two synchronous calls and two `to_thread`
    disk operations. The reload is deliberately NOT here. Shielding it was
    measured (astra, refutation round): with a reload held, it leaves the
    removal task unfinished, the plugin lock held, a reload reader and an agent
    reload lock held, and STILL performs no teardown — a stranded revocation
    traded for a stranded shutdown.

    A refused core settles NOTHING: nothing committed, so nothing is owed.

    Ordering is the mechanism, and the callback teardown now runs BEFORE the
    reload rather than after it. That is safe because the plugin is already
    absent from the registry when the core returns and the callback reconcile
    computes from registry resolution — for an absent plugin, earlier
    revocation can create no route and no pending consent (measured both
    orderings: zero routes, pending consents, valid ack identities and
    republications either way). It is NOT an equivalence: the later prune may
    now remove zero records, the ready marker may already be gone with the
    purged dir, and the spool's abort notice lands earlier. And settlement
    UNROUTES NOTHING — the old runtime overlay stands until the reload."""
    core = await asyncio.to_thread(_plugin_remove_sync, name=name)
    if core.get("ok") is not True:
        return core
    # #494: retire the plugin's setup obligation + round durably. Without
    # this, an approval racing this removal could re-arm a `pending`
    # obligation nothing can ever seal or release (a `pending` row never
    # decays out of health). Called SYNCHRONOUSLY on the event loop —
    # never via to_thread — so it serializes with the loop-confined
    # episode-store writers (consent commit steps, the decision feed); a
    # threaded retire could interleave a feed's load/save and let the
    # feed's stale `pending` snapshot overwrite the retirement (Sol
    # diff-gate r1). FIRST, and before this coroutine's first await: the
    # registry write has already committed.
    try:
        import plugin_setup_episodes
        plugin_setup_episodes.retire_for_removed(core["name"])
    except Exception:  # noqa: BLE001 — teardown must never fail removal
        logger.warning("plugin_remove: setup-obligation retire failed "
                       "(%s)", core["name"], exc_info=True)
    # A:§3.3 (r1-B8): the plugin is gone entirely — invalidate by its
    # (retained-for-GC) artifact AND by every former target's role.
    _invalidate_lifecycle(artifact_id=core.get("artifact_id"),
                          roles=core.get("targets"))
    # The plugin is gone entirely — make its callback removal DURABLE: drop
    # the persisted operator consents (unlike a plugin_update, a REMOVAL DOES
    # revoke — the declaration is gone, not merely re-digested) and purge the
    # spool dir so a later reinstall starts clean instead of inheriting stale
    # results/claims. A reinstall that inherited a surviving ack was measured
    # to make the re-armed setup round empty and authoritative, releasing a
    # setup dispatch on a consent this removal was supposed to revoke.
    await _remove_plugin_callbacks(name)
    return core


@tool(
    "trigger_ack_revoke",
    "Revoke the operator's webhook-trigger consent for a plugin, unroute "
    "its /webhook/plg-<plugin>--… endpoints IMMEDIATELY (they 404 before "
    "this returns), and retire its per-trigger secrets. A later re-approval "
    "mints fresh secrets, so the plugin's setup tool must re-provision the "
    "external service; the consent DM re-prompts on the next plugin "
    "mutation or casa_reload_triggers.",
    {"type": "object", "properties": {"name": {"type": "string"}},
     "required": ["name"]},
)
async def trigger_ack_revoke(args: dict) -> dict:
    """Release B operator off-switch. Runs under _PLUGIN_TOOLS_LOCK like the
    lifecycle mutations; the reconcile inside is the SYNCHRONOUS unroute —
    the overlay swap completes before the tool returns, so an inbound
    request can only race the 404, never a half-revoked route."""
    async with _PLUGIN_TOOLS_LOCK:
        import agent as agent_mod
        import trigger_acks
        import trigger_reconcile
        name = args["name"]
        # Terra shipB-r1 P1-1: kill any PENDING consent keyboard for this
        # plugin FIRST (synchronous broker cancel — later taps read
        # "expired"), so a stale Approve can never re-ack past the revoke.
        CHALLENGES.cancel_matching(plugin=name)
        removed = await _to_thread_settled(
            trigger_acks.ACKS.revoke_plugin, name)
        runtime = getattr(agent_mod, "active_runtime", None)
        registry = getattr(runtime, "trigger_registry", None)
        # Effective names are injective (plg-<plugin>--…), so a prefix
        # match is exact — used for BOTH the overlay sweep and the
        # filesystem secret retirement below.
        prefix = f"plg-{name}--"
        if registry is not None:
            # Fail-closed DIRECT sweep first — the immediate-404 guarantee
            # must not depend on resolver health. Match on the entry's OWN
            # plugin attribution (Sol shipB-r2 P1-3), with the name prefix
            # as belt-and-braces for any entry lacking it.
            #
            # #606: under the routing sentinel there is nothing to sweep —
            # plugin ingress is ALREADY closed — and sweeping anyway would be
            # actively wrong twice over: `.items()` on the sentinel raises, and
            # deriving `{}` from it would publish an authoritative "nothing
            # should route" built from state this process never had.
            import trigger_registry as _treg
            snapshot = registry.plugin_overlay_snapshot()
            if snapshot is not _treg.ROUTING_UNAVAILABLE:
                registry.replace_plugin_overlay({
                    eff: entry
                    for eff, entry in snapshot.items()
                    if entry.get("plugin", "") != name
                    and not eff.startswith(prefix)})
        try:
            await trigger_reconcile.reconcile_from_runtime(
                runtime, prompt=False)
        except Exception:  # noqa: BLE001 — sweep above already unrouted
            logger.warning("post-revoke trigger reconcile failed",
                           exc_info=True)
        # Pair the callback reconcile with matching prompt=False
        # so the callback overlay stays consistent after a trigger revoke (a
        # resident's grants can shift callback assignment too). Non-fatal.
        try:
            import callback_reconcile
            await callback_reconcile.reconcile_from_runtime(
                runtime, prompt=False)
        except Exception:  # noqa: BLE001
            logger.warning("post-revoke callback reconcile failed",
                           exc_info=True)
        # Sol shipB-r1 P1-4: retire the plugin's per-trigger secrets from
        # the FILESYSTEM inventory (prefix glob — never from the ack
        # records this tool just deleted). Keeping them would let the NEXT
        # artifact inherit the old credential: a later plugin_update's
        # revoke_artifact would find no records and retire nothing.
        import webhook_auth
        retired = await _to_thread_settled(
            webhook_auth.retire_secrets_with_prefix, prefix,
            secrets_dir=trigger_reconcile.SECRETS_DIR)
        # Sol shipB-r2 P1-1: cancel AGAIN after our reconcile. Prompts fire
        # under _RECONCILE_LOCK, and our reconcile above serialized behind
        # any in-flight one — so a keyboard posted by that in-flight
        # reconcile is REGISTERED by now and this cancel provably kills it.
        # (Reconciles triggered by LATER mutations legitimately re-prompt —
        # that is the documented re-consent path, not a survival.)
        CHALLENGES.cancel_matching(plugin=name)
        # Refresh health (trigger_pending_ack reappears via the recomputable
        # input) WITHOUT the operator DM — they just did this deliberately.
        await _regenerate_plugin_health_held()
        return _result({
            "ok": True, "name": name, "revoked": len(removed),
            "unrouted": sorted(r.get("effective") or "" for r in removed),
            "secrets_retired": retired,
        })


async def _remove_plugin_callbacks(name: str) -> None:
    """Durable callback teardown for a removed plugin: revoke its
    persisted callback consents and delete its spool dir. Best-effort — a
    failure here never fails the removal (the overlay was already swept by
    absence in the sequencer's callback reconcile)."""
    try:
        import callback_acks
        removed = await asyncio.to_thread(
            callback_acks.ACKS.revoke_plugin, name)
        if removed:
            logger.info("plugin_remove: revoked %d callback ack(s) for %s",
                        len(removed), name)
    except Exception:  # noqa: BLE001
        logger.warning("plugin_remove: callback ack revoke failed (%s)",
                       name, exc_info=True)
    try:
        import callback_spool
        spool = callback_spool.get_spool()
        if spool is not None:
            # The spool owns the abort record: it counts the plugin's
            # unsettled flows and writes a durable `.removals` record BEFORE
            # the purge, returning False when that record would not go
            # durable (the purge is then skipped and the orphan GC converges
            # on the dir later). Best-effort here by design — the plugin is
            # already unrouted, so a deferred purge changes nothing the
            # operator can see.
            await asyncio.to_thread(spool.remove_plugin, name)
    except Exception:  # noqa: BLE001
        logger.warning("plugin_remove: callback spool purge failed (%s)",
                       name, exc_info=True)


@tool(
    "callback_ack_revoke",
    "Revoke the operator's authorization-callback consent for ONE callback of "
    "a plugin (by its effective name), and unroute its /callback/<effective> "
    "endpoint IMMEDIATELY (it stops accepting deposits before this returns). "
    "A later re-approval re-consents; the consent DM re-prompts on the next "
    "plugin mutation or reload.",
    {"type": "object",
     "properties": {"plugin": {"type": "string"}, "name": {"type": "string"}},
     "required": ["plugin", "name"]},
)
async def callback_ack_revoke(args: dict) -> dict:
    """Operator off-switch — the callback sibling of
    trigger_ack_revoke. Runs under _PLUGIN_TOOLS_LOCK; the reconcile inside is
    the SYNCHRONOUS unroute (a compute failure fail-closes the WHOLE callback
    overlay, so the revoked route is 404 either way — no separate direct sweep
    is needed, unlike triggers)."""
    async with _PLUGIN_TOOLS_LOCK:
        import agent as agent_mod
        import callback_acks
        import callback_reconcile
        plugin = args["plugin"]
        name = args["name"]
        # Kill any pending consent keyboard for this plugin FIRST so a stale
        # Approve cannot re-ack past the revoke (mirrors trigger_ack_revoke).
        CHALLENGES.cancel_matching(plugin=plugin)
        removed = await _to_thread_settled(
            callback_acks.ACKS.revoke_effective, plugin, name)
        runtime = getattr(agent_mod, "active_runtime", None)
        # I3: run BOTH reconcilers so a shared round never keeps an open member.
        try:
            import trigger_reconcile
            await trigger_reconcile.reconcile_from_runtime(runtime, prompt=False)
        except Exception:  # noqa: BLE001
            logger.warning("post-callback-revoke trigger reconcile failed",
                           exc_info=True)
        try:
            await callback_reconcile.reconcile_from_runtime(
                runtime, prompt=False)
        except Exception:  # noqa: BLE001
            logger.warning("post-callback-revoke callback reconcile failed",
                           exc_info=True)
        # Cancel again AFTER our reconcile — a keyboard posted by an in-flight
        # reconcile is registered by now (prompts fire under _RECONCILE_LOCK,
        # which our reconcile serialized behind).
        CHALLENGES.cancel_matching(plugin=plugin)
        await _regenerate_plugin_health_held()
        return _result({
            "ok": True, "plugin": plugin, "name": name,
            "revoked": len(removed),
        })


@tool(
    "event_ack_revoke",
    "Revoke the operator's event-subscription consent for a plugin — for "
    "ONE subscription when both emitter and event are given, otherwise "
    "every subscription the plugin holds — and unroute it IMMEDIATELY (no "
    "further delivery nudges are admitted once this returns). A later "
    "re-approval re-consents; the consent DM re-prompts on the next plugin "
    "mutation or reload.",
    {"type": "object",
     "properties": {"subscriber": {"type": "string"},
                    "emitter": {"type": "string"}, "event": {"type": "string"}},
     "required": ["subscriber"]},
)
async def event_ack_revoke(args: dict) -> dict:
    """Operator off-switch — the event sibling of callback_ack_revoke /
    trigger_ack_revoke. ``event_reconcile.revoke_and_unroute`` itself does
    the unroute-BEFORE-ack-delete transaction under the shared dispatch-
    admission lock (event_reconcile Sol-r4 #2 / Sol-r5 #2) — this tool only
    orders the consent-keyboard cancel and the follow-up reconcile around
    it, mirroring its siblings."""
    async with _PLUGIN_TOOLS_LOCK:
        import agent as agent_mod
        import event_reconcile
        subscriber = args["subscriber"]
        emitter = (args.get("emitter") or "").strip()
        event = (args.get("event") or "").strip()
        # Kill any pending consent keyboard for this subscriber FIRST so a
        # stale Approve cannot re-ack past the revoke (mirrors the siblings).
        CHALLENGES.cancel_matching(plugin=subscriber)
        removed = await event_reconcile.revoke_and_unroute(
            subscriber, emitter, event)
        runtime = getattr(agent_mod, "active_runtime", None)
        try:
            await event_reconcile.reconcile_plugin_events(
                runtime, prompt=False)
        except Exception:  # noqa: BLE001 — revoke_and_unroute already
            # unrouted synchronously; a reconcile failure never reopens it.
            logger.warning("post-event-revoke reconcile failed",
                           exc_info=True)
        # Cancel again AFTER our reconcile — a keyboard posted by an
        # in-flight reconcile is registered by now (prompts fire under
        # event_reconcile's own lock, which our reconcile serialized behind).
        CHALLENGES.cancel_matching(plugin=subscriber)
        await _regenerate_plugin_health_held()
        # Minor-9 (review round 1): a malformed record's emitter/event must
        # never crash the sort with a None-vs-str comparison — map to "" so
        # a defensive-only anomaly is merely mis-sorted, never a raise.
        pairs = sorted({(r.get("emitter") or "", r.get("event") or "")
                        for r in removed})
        return _result({
            "ok": True, "subscriber": subscriber,
            "revoked": len(removed),
            "pairs": pairs,
        })


# #1164: how consent_reprompt names a keyboard it does not re-issue. Kind
# literals come from the code that registers each challenge, never from a
# model; an authorization request is named by its existence alone.
_OPEN_KEYBOARD_LABELS = {
    "specialist_install_consent": (
        "a specialist install/upgrade consent — re-asked by "
        "specialist_install_request_approval, after specialist_install_inspect"),
    "persona_install_consent": (
        "a persona install consent — re-asked by persona_install_inspect"),
    "plugin_erase_choice": (
        "an uninstall erase-data question — re-asked by plugin_remove / "
        "specialist_uninstall called without erase_data"),
    "authz": "an authorization request — the operator answers it there",
    "trigger_consent": "a plugin trigger consent",
    "callback_consent": "a plugin callback consent",
    "event_consent": "a plugin event consent",
}


def _open_keyboard_label(kind: str) -> str:
    return _OPEN_KEYBOARD_LABELS.get(kind, f"a {kind} keyboard")


@tool(
    "consent_reprompt",
    "Re-issue the operator's pending plugin-consent DM keyboards (trigger, "
    "callback, and event consents) whose original prompt expired or was "
    "missed. This is the ONLY way to re-surface a consent on demand — a "
    "question asked any other way (ask_user, an engagement ask) commits "
    "nothing however it is answered. Posts nothing for consents the "
    "operator explicitly DENIED (those re-prompt only on the next plugin "
    "mutation or casa_reload) and reports each pending consent's outcome. "
    "Any other keyboard still open in the operator DM (a specialist or "
    "persona install consent, an erase-data question, an authorization "
    "request) is named by kind, never re-issued. A specialist or persona "
    "install/upgrade consent that already expired is not re-issued by any "
    "tool: it comes back only by running that install or upgrade again (the "
    "configurator requests approval again, and Casa posts a fresh card).",
    {"type": "object", "properties": {}},
)
async def consent_reprompt(args: dict) -> dict:
    """#494 — the committing recovery for a missed/expired consent DM.

    Prompt-only by design (design rounds 2–4): each kind's
    ``reprompt_pending`` runs under its own reconcile lock but performs NO
    reconcile — no overlay swap, no setup-round sealing or re-arming — so an
    on-demand call can never reopen a setup-round member its own denial
    filter refuses to prompt. Delivery is reported from the keyboards'
    actual settled post outcomes (``ChallengeHandle.settled_post``), never
    inferred from pending rows."""
    async with _PLUGIN_TOOLS_LOCK:
        import agent as agent_mod
        runtime = getattr(agent_mod, "active_runtime", None)
        if runtime is None:
            return _result({"ok": False, "kind": "runtime_unavailable",
                            "message": "runtime not ready"})
        channel_manager = getattr(runtime, "channel_manager", None)
        transport = telegram_transport(channel_manager) if channel_manager else None
        op = (transport.operator_user_id()
              if transport is not None else None)
        if op is None:
            return _result({
                "ok": False, "kind": "dm_unreachable",
                "message": ("no operator DM is reachable — a consent "
                            "keyboard cannot be posted"),
            })
        report: list = []
        for mod_name in ("trigger_reconcile", "callback_reconcile",
                         "event_reconcile"):
            try:
                mod = importlib.import_module(mod_name)
                await mod.reprompt_pending(runtime, report=report)
            except Exception:  # noqa: BLE001 — one kind's failure must not
                # silence the other kinds' rows
                logger.exception("consent reprompt failed (%s)", mod_name)
                report.append({"kind": mod_name.partition("_")[0],
                               "plugin": "", "name": "", "status": "error"})
    # Locks released — classify DELIVERY from the settled post outcome
    # (design r1, Sol+Terra: pending rows are intent, not delivery; a
    # background post can fail and unregister). Settled outcome FIRST,
    # `created` second (design r3, Sol: a deduped handle's shared driver can
    # still fail).
    rows: list[dict] = []
    posted = 0
    attempted = 0
    reachable = 0
    for r in report:
        handle = r.pop("handle", None)
        if handle is None:
            rows.append(r)
            continue
        attempted += 1
        if getattr(handle, "refused", None):
            r["status"] = "refused"
        else:
            settled = await handle.settled_post()
            if settled == "posted":
                reachable += 1
                if handle.created:
                    posted += 1
                    r["status"] = "posted"
                else:
                    r["status"] = "already_pending"
            else:
                r["status"] = settled  # delivery_failed | inactive
        rows.append(r)
    # #582 batch (Sol design r1): this regeneration runs OUTSIDE the lock the
    # body above held (the settled-post awaits are deliberately not under it),
    # so it takes the guard — the report lock orders the write, not the
    # computation preceding it, and an unguarded pass can write a pre-mutation
    # result last and delete the row a concurrent mutation just added.
    await _regenerate_plugin_health_guarded()
    # Sol/Terra diff-gate r1: a kind that FAILED (compute raised, a prompt
    # registration raised, or the whole reprompt_pending call raised) must
    # never read as "nothing pending" — its pending state could not be
    # evaluated, so the result is a typed failure even when other kinds'
    # keyboards posted (their rows still say so).
    if any(r.get("status") == "error" for r in rows):
        return _result({
            "ok": False, "kind": "reprompt_failed", "rows": rows,
            "message": ("a consent kind could not be re-evaluated — its "
                        "pending consents (if any) were NOT re-issued; see "
                        "rows, check logs, and retry"),
        })
    if attempted and reachable == 0:
        return _result({
            "ok": False, "kind": "delivery_failed", "rows": rows,
            "message": ("consent keyboards were needed but none could be "
                        "delivered to the operator DM — retry once the "
                        "channel recovers"),
        })
    denied = [r for r in rows if r.get("status") == "denied"]
    # #1164: every other keyboard still open in the operator DM, by kind —
    # read, never re-issued. A plugin kind is left out only when a row above
    # already says its keyboard is on screen. The coordinator is resolved at
    # call time, as the reconcilers resolve it.
    import authz_grants
    shown = {f"{r.get('kind')}_consent" for r in rows
             if r.get("status") in ("posted", "already_pending")}
    # Casa's consent cards sit in the operator's private chat, whose id is the
    # operator's own user id.
    live = authz_grants.CHALLENGES.live_kinds(chat_id=op, operator_id=op)
    others = [k for k in live if k not in shown]
    message = (
        f"{posted} consent keyboard(s) (re)posted to the operator DM"
        if posted else
        ("a consent keyboard is already open in the operator DM"
         if any(r.get("status") == "already_pending" for r in rows)
         else ("no plugin consent needed re-issuing" if others
               else "no consent is pending — nothing to re-issue")))
    if denied:
        message += (
            f"; {len(denied)} consent(s) were DENIED by the operator and "
            "were NOT re-issued — a new plugin mutation or casa_reload "
            "re-asks")
    if others:
        message += (
            "; still open or being posted in the operator DM, and NOT "
            "re-issued by consent_reprompt: "
            + "; ".join(_open_keyboard_label(k) for k in others))
    return _result({"ok": True, "reprompted": posted, "rows": rows,
                    "message": message})


@tool(
    "plugin_list",
    "List every registered plugin with its version, revision, targets, "
    "artifact presence, seeded-default status and owner. `owner` names the "
    "specialist whose bundle a plugin comes with: it moves with that "
    "specialist's upgrades, not on its own. `targets` is what the "
    "registry stores; `effective_targets` is what Casa actually serves and "
    "`ignored_targets` what it stores but does not — for now a worker "
    "(executor:) assignment on an operator-installed plugin is ignored.",
    {},
)
async def plugin_list(args: dict) -> dict:
    return _result(await asyncio.to_thread(_tool_plugin_list))


# #555: how many setup obligations the history names. The episode store has no
# TTL and no size cap — one row survives per plugin ever installed, including
# removed ones — so an unbounded return would grow a resident's context without
# limit for no added answer.
_STATUS_HISTORY_LIMIT = 20


def _episode_sentence(row: dict) -> str:
    """One operator-facing line for a setup obligation.

    Prose, not a row: the assistant paraphrases raw JSON badly, which is the
    whole of #550 — handing it a dict inside the feature meant to relieve that
    would reproduce the problem."""
    plugin = row.get("plugin") or "a plugin"
    status = str(row.get("status") or "unknown")
    said = {
        "pending": "setup has not run yet",
        "dispatched": "setup is running",
        "failed": "setup failed",
        "stale": "setup started and stopped partway",
        "refused": "setup was not allowed to run",
    }.get(status, f"setup is {status}")
    # #1183: `dispatched` is the store's one consumed status, so an unmarked
    # row is either in flight or was consumed before the mark existed; the
    # mark the outcome report writes says which arm consumed it. A courier
    # row evidenced a delegation, never the specialist's run of the tool.
    if status == "dispatched":
        outcome = row.get("dispatch_outcome")
        if outcome == "tool_ran":
            said = "setup ran (the dispatched setup turn ran the setup tool)"
        elif outcome == "delegated":
            target = row.get("courier_target")
            if isinstance(target, str) and target:
                said = (f"setup was handed to '{target}' "
                        "(the delegation completed)")
    # #1003: a row settled by turn evidence records a successful run of the
    # setup tool by the assistant's own hand, whatever status a later removal
    # left on it. Said independently of `status`; no claim about the
    # external side, which Casa cannot see.
    if row.get("settled_by") == "turn_evidence":
        # #1052: a specialist's own run settles too; name who ran it. A row
        # settled before `settled_role` existed was the assistant's.
        who = row.get("settled_role")
        if not isinstance(who, str) or not who or who == "assistant":
            said = "setup ran (the assistant ran the setup tool itself)"
        else:
            said = f"setup ran ('{who}' ran the setup tool itself)"
    parts = [f"{plugin}: {said}"]
    attempts = _status_int(row.get("attempts"))
    retries = _status_int(row.get("execution_retries"))
    if attempts:
        parts.append(f"{attempts} attempt{'s' if attempts != 1 else ''}")
    if retries:
        parts.append(f"{retries} execution retr{'ies' if retries != 1 else 'y'}")
    line = " — ".join(parts)
    # The last_error is the ONE fact that answers a historical failure; the
    # health report describes standing state only and cannot carry it.
    last_error = row.get("last_error")
    if isinstance(last_error, str) and last_error.strip():
        line += f" — last error: {last_error.strip()}"
    # #928: two more facts the row carries about a removal. A row stamped by
    # `retire_for_removed` describes an installation that is gone; a row
    # re-armed after a same-download reinstall carries what the earlier
    # installation failed on, which is the answer to "why did it fail last
    # time" once the current row says only "has not run yet". A malformed
    # snapshot renders as nothing rather than as a claim.
    import plugin_setup_episodes
    if plugin_setup_episodes._removal_mark(row) is not None:
        line += " — the plugin was removed since"
    previous = row.get("previous_failure")
    if isinstance(previous, dict):
        error = previous.get("last_error")
        if isinstance(error, str) and error.strip():
            line += (" — before the plugin was removed and reinstalled, "
                     f"setup failed: {error.strip()}")
    return line


def _status_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _status_sort_key(row: dict) -> float:
    """`updated_ts` as a number, tolerating anything a hand-edited store holds.
    A malformed stamp sorts oldest rather than raising mid-render — ANY failure
    (an int too large for a float raises OverflowError) and any non-finite
    value, the same rule `plugin_setup_episodes._stamp` applies."""
    import plugin_setup_episodes
    value = row.get("updated_ts")
    stamp = plugin_setup_episodes._finite(value) if value else 0.0
    return 0.0 if stamp is None else stamp


def _tool_plugin_status() -> dict:
    """#555: the assistant's read-only answer to "what is wrong with plugin X,
    and what went wrong when it was installed?".

    Two stores, because they answer different questions: the health report holds
    STANDING failures, while the episode row's `last_error` and retry counts are
    the only record of a HISTORICAL one. Neither is reachable any other way —
    `Read` is inert for residents and /data is deliberately never opened
    broadly, since it holds secrets.

    Each half degrades independently: a corrupt report must not cost the
    operator the episode history that would have explained the failure.

    #677: damage is DISCLOSED, never presented as health. `load_report` returns
    None for a report that is absent, unreadable, unparseable, or valid JSON
    that is not an object — and `or {}` collapsed all four into `standing: []`,
    byte-identical to a healthy report with no problems. So the agent asserted
    that nothing was wrong on the strength of a file it could not read. The
    absent case really is ordinary (a box that has not regenerated health yet),
    so only that one is silent; every other case adds a conditional
    `standing_unavailable`. The keys are conditional so the healthy answer stays
    exactly `{"ok": True, "standing": [], "history": []}` — a genuinely empty
    report still reads as health.

    `history_unavailable` (#747, INV-PLUG-015) covers an episode store that
    exists but could not be read as a valid store — unreadable bytes, a
    malformed or wrong-schema document, or episode rows that were only partly
    readable — and, once a writer has replaced such a store with a valid empty
    one, the fact of that reset for as long as a failed setup would stay in
    health. `plugin_setup_episodes.read_episodes()` reports the damage beside
    the rows it did read, from ONE read; a read that RAISES is still caught
    below. An absent store is ordinary and stays silent."""
    standing: list = []
    history: list = []
    unavailable: dict = {}
    try:
        import plugin_health
        report = plugin_health.load_report(Path(_PLUGIN_HEALTH_PATH))
        if report is None:
            # Absent is ordinary; anything else is damage. A probe that itself
            # fails is damage too — "cannot tell" must not read as "healthy".
            try:
                present = Path(_PLUGIN_HEALTH_PATH).exists()
            except Exception:  # noqa: BLE001
                present = True
            if present:
                unavailable["standing_unavailable"] = (
                    "the plugin health report exists but could not be read, so "
                    "the standing problems below are not the full set")
            report = {}
        standing = [plugin_health.describe_issue(d)
                    for d in (list(report.get("issues") or [])
                              + list(report.get("warnings") or []))]
    except Exception:  # noqa: BLE001 — a read tool must never raise at a resident
        logger.exception("plugin_status: health report read failed")
        unavailable["standing_unavailable"] = (
            "the plugin health report could not be read, so the standing "
            "problems below are not the full set")
    try:
        import plugin_setup_episodes
        read = plugin_setup_episodes.read_episodes()
        rows = [r for r in read.rows if isinstance(r, dict)]
        rows.sort(key=_status_sort_key, reverse=True)
        history = [_episode_sentence(r) for r in rows[:_STATUS_HISTORY_LIMIT]]
        sentence = plugin_setup_episodes.damage_sentence(read)
        if sentence:
            unavailable["history_unavailable"] = (
                f"{sentence}, so the entries below are not the full record")
    except Exception:  # noqa: BLE001
        logger.exception("plugin_status: episode store read failed")
        unavailable["history_unavailable"] = (
            "the plugin setup history could not be read, so the entries below "
            "are not the full record")
    # Best-effort and never raising: while a plugin routing overlay is the
    # unavailable sentinel, no reconcile has published an authoritative set, so
    # trigger/callback/pending-approval rows in the report may be stale.
    try:
        import agent as agent_mod
        registry = getattr(getattr(agent_mod, "active_runtime", None),
                           "trigger_registry", None)
        if registry is not None and (registry.plugin_overlay_unavailable()
                                     or registry.callback_overlay_unavailable()):
            unavailable["routing_unavailable"] = (
                "plugin webhook/callback routing is not currently established, "
                "so trigger, callback and pending-approval entries may be stale")
    except Exception:  # noqa: BLE001
        logger.debug("plugin_status: routing availability probe skipped",
                     exc_info=True)
    return {"ok": True, "standing": standing, "history": history,
            **unavailable}


@tool(
    "plugin_status",
    "Answer an operator asking why a plugin is not working from `standing` "
    "first: it reports what is currently wrong with each installed plugin — "
    "what is blocking it right now, such as a value it is still waiting for. "
    "`history` comes after, and says what happened during each plugin's "
    "automatic setup, including the error a failed setup last reported. "
    "Read-only: it changes nothing, and it answers the question without "
    "engaging the configurator.",
    {"type": "object", "properties": {}},
)
async def plugin_status(args: dict) -> dict:
    return _result(await asyncio.to_thread(_tool_plugin_status))


# ---------------------------------------------------------------------------
# Plan 4b §7.4–7.6: uninstall + verify_plugin_state + vault helper tools
# ---------------------------------------------------------------------------


def _tool_verify_plugin_state(
    *,
    plugin_name: str,
    _tools_bin: Path | None = None,
    _registry_path=None,
    _store_root=None,
) -> dict:
    """Tier-aware verification (§3.9): does the RUNNING state agree with the
    registry's DESIRED state? Constructed residents/specialists must have the
    desired artifact bound (else reload_required); dormant targets report
    configured readiness only (an `executor:` target is always dormant: no
    executor loads plugins); running engagements on a previous artifact are informational.
    Verification can never report active agreement while a running consumer
    executes different code (FR3).

    FR3 readiness rule (spec 2026-07-13 §D1 — verbatim):

    Readiness describes the artifact a target can execute on its next new
    turn, not whether it is busy at verification time. A current dispatchable
    resident or specialist Agent is ready only when it is unresolved and will
    resolve the current registry snapshot before use, or its coherently
    recorded binding equals the desired artifact. A stale binding remains
    `reload_required` while idle because that Agent can reuse it on its next
    bus or trigger turn. Artifacts retained only by already-started, pinned,
    or draining engagements are informational, provided no new turn can enter
    them.
    """
    import agent as agent_mod
    import cc_tool_pattern
    from plugin_env_conf import read_entries
    from plugin_registry import ResolvedPlugin, STORE_ROOT, load_registry
    from system_requirements.manifest import read_manifest

    reg = (load_registry(_registry_path) if _registry_path is not None
           else load_registry())
    if not reg.valid:
        return {"ready": False, "reasons": ["registry_invalid"],
                "targets": []}
    # Sol #8: verify MUST agree with the resolver. An entry that resolve_for()
    # drops (duplicate_name) or skips (entry_invalid) is unavailable to every
    # agent/executor — verify must not green it off the raw list. Select from
    # the VALIDATED entries and surface the resolver's own rejection reason.
    # Sol round-3 M-shadow: PREFER a validated resolved entry. A rejection issue
    # (entry_invalid / duplicate_name) only blocks when NO validated entry of
    # this name survived — otherwise an unrelated same-name invalid entry would
    # shadow the valid one the resolver actually serves.
    entry = next((e for e in reg.entries
                  if isinstance(e, dict) and e.get("name") == plugin_name), None)
    if entry is None:
        bad = next((i for i in reg.entry_issues if i.name == plugin_name), None)
        if bad is not None:
            return {"ready": False, "reasons": [bad.reason_code], "targets": []}
        return {"ready": False, "reasons": ["not_registered"], "targets": []}

    store_root = _store_root if _store_root is not None else STORE_ROOT
    artifact_id = entry.get("artifact_id")
    src = entry.get("source") or {}
    revision = src.get("revision", "")
    path = Path(store_root) / plugin_name / str(artifact_id)
    reasons: list[str] = []
    provenance_warning = revision.startswith("legacy-content:")
    present = path.is_dir()
    checksum_valid = False
    if not present:
        reasons.append("artifact_missing")
    else:
        verdict = plugin_store.artifact_verdict(
            path, name=plugin_name, repo=src.get("repo", ""),
            revision=revision, subdir=src.get("subdir", ""),
            artifact_id=str(artifact_id),
            manifest_name=entry.get("manifest_name"))
        if verdict is None:
            checksum_valid = True
        else:
            reasons.append(verdict)          # artifact_invalid | corrupt_artifact

    manifest: dict = {}
    try:
        manifest = json.loads((path / ".claude-plugin" / "plugin.json")
                              .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    rp = ResolvedPlugin(name=plugin_name, artifact_id=str(artifact_id),
                        path=str(path), version=str(entry.get("version", "")),
                        manifest=manifest,
                        manifest_name=entry.get("manifest_name") or plugin_name)
    # Sol #16: a PRESENT-but-malformed .mcp.json silently degrades grants/secrets
    # to [] (indistinguishable from skill-only), so a broken MCP server would
    # otherwise verify ready. Treat it as a blocking reason.
    if checksum_valid and mcp_json_malformed(rp):
        reasons.append("mcp_invalid")
    # P1 (2026-07-18 self-containment plan): static launch-reference
    # resolvability — the gmail-v0.2.0 class (command points at a gitignored
    # venv absent from the artifact). Detects resolvable command/artifact-file
    # references only, NOT "the server can spawn"; unjudgeable shapes are
    # "unchecked" and never block. Appended AFTER artifact/mcp_invalid so the
    # single-reason health rollup (reasons[0]) keeps integrity reasons first.
    command_status = (plugin_store.mcp_command_verdicts(
        path / ".mcp.json", path) if checksum_valid else [])
    if any(c["status"] == "missing" for c in command_status):
        reasons.append("mcp_command_missing")
    # G6 corrected (2026-07-19): a server env self-declaring a CLI-reserved
    # var (CLAUDE_PLUGIN_DATA/CLAUDE_PLUGIN_ROOT) shadows the CLI's native
    # per-plugin value with a literal — blocking, same tier as mcp_invalid.
    reserved_env = (plugin_store.reserved_env_violations(path / ".mcp.json")
                    if checksum_valid else [])
    if reserved_env:
        reasons.append("mcp_reserved_env")
    granted = grants_for_resolved(rp) if checksum_valid else []
    # A:§3.7 (B7): eyeball-checkable declared protected tools. A malformed
    # field already surfaced as a blocking "protected_tools_invalid" reason
    # above (via artifact_verdict); this is purely informational disclosure
    # of the declared list, tolerant of that same malformed case.
    # v0.78.0 W1: manifest_protected_tools now returns a NORMALIZED
    # [{"name", "summary"}] list. `protected_tools` KEEPS its existing
    # machine-readable names-only contract; the new `protected_tool_summaries`
    # field (below) surfaces the declared templates for entries that have one,
    # so the trusted template text is eyeball-checkable too.
    try:
        protected_entries = (plugin_store.manifest_protected_tools(manifest)
                             if checksum_valid else [])
    except plugin_store.StoreError:
        protected_entries = []
    protected_tools = [e["name"] for e in protected_entries]
    protected_tool_summaries = {
        e["name"]: e["summary"] for e in protected_entries if e.get("summary")
    }
    # v0.112.0: a declared casa.setupTool with NO invocation path (targets
    # exclusively executor:* — executors have no channel and no post-consent
    # dispatch) is a configuration error, not a silent no-op (Sol design
    # round). Blocking, same tier as the other configured-state reasons.
    try:
        _setup_decl = (plugin_store.manifest_setup_tool(manifest)
                       if checksum_valid else None)
    except plugin_store.StoreError:
        _setup_decl = None  # malformed already surfaced via artifact_verdict
    _entry_targets = list(entry.get("targets") or [])
    if _setup_decl and not any(
            str(t).startswith(("resident:", "specialist:"))
            for t in _entry_targets):
        # Executor-only AND empty target lists alike: a declared automatic
        # setup hook with no invocation path is a configuration error, not a
        # silent no-op (impl r2 closed the empty-list gap).
        reasons.append("setup_tool_unsupported_target")
    # v0.112.0 (impl r1): the post-consent dispatch names the EXACT
    # namespaced tool `<server-grant>__<setupTool>` — that binding is only
    # unambiguous with exactly ONE MCP server grant. Zero (skill-only) or
    # several servers make the declaration unexecutable/ambiguous: blocking.
    if _setup_decl and len(granted) != 1:
        reasons.append("setup_tool_ambiguous_server")
    # #451 r4 (Sol): a setup tool that is ALSO declared protected can never run
    # through the automatic runner. Casa dispatches its synthetic turn with the
    # reserved provenance marker `synthetic="plugin_setup"`, which the
    # provenance classifier maps to `other`, and the protected-tool hook denies
    # an `other` origin outright as an unsupported origin — before any grant
    # lookup or operator prompt. Bus acceptance has already marked the
    # obligation dispatched by then, so nothing retries and nothing surfaces.
    # Since v0.161.0 Casa is the ONLY runner, the combination has no invocation
    # path at all: blocking, exactly like an executor-only target.
    if _setup_decl and _setup_decl in set(protected_tools):
        reasons.append("setup_tool_protected")

    # Tools (system-requirements — verify_bin presence). Sol #11: check BOTH the
    # INSTALLED manifest rows AND every requirement the ARTIFACT declares, so a
    # plugin declaring a sysreq with no installed row can't pass on all([]).
    data = read_manifest()
    tool_entries = [p for p in data["plugins"] if p["name"] == plugin_name]
    tools_bin = _tools_bin if _tools_bin is not None else Path("/config/tools/bin")
    tools_status = []
    for t in tool_entries:
        vb = t.get("verify_bin", "")
        ready_bin = (tools_bin / vb).is_file()   # follows symlinks (M23)
        tools_status.append({
            # Sol CI-review: a hand-corrupted manifest row may lack winning_strategy
            # (read_manifest keeps any dict with a name) — access defensively.
            "requirement": t.get("winning_strategy", "unknown"), "verify_bin": vb,
            "status": "ready" if ready_bin else "missing",
            **({} if ready_bin else {"reason": f"{vb} not in tools/bin"})})
    # Declared-but-not-installed requirements (Sol #11): every requirement the
    # artifact's manifest declares must have a corresponding installed entry.
    installed_bins = {t.get("verify_bin", "") for t in tool_entries}
    declared_reqs: list = []
    if checksum_valid:
        try:
            declared_reqs = plugin_store.manifest_sysreqs(manifest)
        except plugin_store.StoreError as exc:
            # #354: a malformed declaration must be VISIBLE on the verify
            # surface (pre-fix it silently read as "no requirements"), and
            # must not crash verification for the whole plugin.
            tools_status.append({
                "requirement": "declared", "verify_bin": "",
                "status": "missing",
                "reason": f"casa.systemRequirements malformed: {exc}"})
    for req in declared_reqs:
        vb = req.get("verify_bin", "")
        if vb and vb not in installed_bins:
            tools_status.append({
                "requirement": req.get("type", "declared"), "verify_bin": vb,
                "status": "missing",
                "reason": f"declared requirement {vb!r} has no installed entry"})

    # Secrets from the ARTIFACT's .mcp.json (no version-dir guessing).
    required = set(required_env_vars_for_resolved(rp)) if checksum_valid else set()
    env_conf = read_entries()
    secrets_status = []
    for var in sorted(required):
        conf_val = env_conf.get(var)
        # Sol round-4: a secret counts as resolved only if it is present in the
        # EFFECTIVE environment (boot sourced plugin-env.conf into os.environ,
        # resolving op:// refs). A config entry whose op:// resolution FAILED at
        # boot leaves os.environ unset — configuration presence alone must not
        # mark it resolved, or a broken MCP plugin passes the launch gate.
        effective = os.environ.get(var)
        # Sol r4-10 (rotation stale-green): a PLAIN configured value that
        # differs from the effective environment means the reload hasn't run
        # yet — the old value would be inherited by the next MCP spawn.
        # (op:// refs can't be compared: conf holds the ref, environ the
        # resolved secret.)
        if (effective and conf_val is not None
                and not conf_val.startswith("op://")
                and effective != conf_val):
            secrets_status.append({
                "var": var, "source": "plain", "status": "unresolved",
                "reason": ("configured value not yet applied to the "
                           "effective environment (reload pending)")})
        elif effective and not effective.startswith("op://"):
            source = "op" if (conf_val or "").startswith("op://") else "plain"
            secrets_status.append({"var": var, "source": source,
                                   "status": "resolved"})
        elif conf_val is not None:
            secrets_status.append({
                "var": var,
                "source": "op" if conf_val.startswith("op://") else "plain",
                "status": "unresolved",
                "reason": "configured but not resolved in the effective environment"})
        else:
            secrets_status.append({
                "var": var, "source": "missing", "status": "unresolved",
                "reason": ("supplied by the app option "
                           f"{_CASA_OWNED_ENV[var]!r}, which is unset"
                           if var in _CASA_OWNED_ENV
                           else "not in plugin-env.conf")})

    # #429: reclassify the DECLARED class. A `casa.setupProvides` var no
    # longer withholds the plugin (that is what deadlocked a setup tool whose
    # job is to create its own credentials), so it must not read as a plain
    # unresolved secret here either — but it is still NOT ready: the setup
    # tool has not landed the value yet, and a plugin sitting on empty
    # credentials forever must stay loud rather than quietly pass as
    # configured. That readiness signal is the entire reason the declaration
    # exists; a genuinely optional variable needs none, because `${VAR:-}` in
    # .mcp.json already expands to empty without withholding (#431).
    # Read STRICTLY: a malformed declaration leaves every row as it was (the
    # plugin is still withheld — plugin_grants fails closed too).
    setup_provided: set = set()
    if checksum_valid:
        try:
            setup_provided.update(plugin_store.manifest_setup_provides(manifest))
        except plugin_store.StoreError as exc:
            reasons.append(exc.reason_code)
            secrets_status.append({
                "var": "", "source": "manifest", "status": "unresolved",
                "reason": f"declaration malformed: {exc}"})
    unprovisioned: list[str] = []
    for row in secrets_status:
        if row["status"] == "unresolved" and row["var"] in setup_provided:
            row["status"] = "unprovisioned"
            row["reason"] = _SETUP_PROVIDED_REASON
            unprovisioned.append(row["var"])
    # Sol r1: the rows above are built from `.mcp.json` ${VAR} references
    # only, so a setupProvides name the plugin reads from the INHERITED
    # environment instead of naming in its launch config would produce no
    # row at all — and the plugin would grade ready with its declared
    # credential absent, which is precisely the state this reason exists to
    # make loud. Grade every declared name, referenced or not.
    for var in sorted(setup_provided - required):
        effective = os.environ.get(var)
        if effective and not effective.startswith("op://"):
            continue
        secrets_status.append({
            "var": var, "source": "setup", "status": "unprovisioned",
            "reason": _SETUP_PROVIDED_REASON})
        unprovisioned.append(var)
    if unprovisioned:
        reasons.append("setup_env_unprovisioned")
    # #533: a plainly-unresolved required secret gets a NAMED reason —
    # pre-fix nothing was appended here, so the target rows (and therefore
    # the health report and the operator DM) degraded to the generic
    # "not_ready" while the variable names sat unread in secrets_status.
    if any(s["status"] == "unresolved" and s.get("var")
           for s in secrets_status):
        reasons.append("env_unresolved")

    tools_ready = all(t["status"] == "ready" for t in tools_status)
    # "unprovisioned" deliberately does NOT count as ready.
    secrets_ready = all(s["status"] == "resolved" for s in secrets_status)
    configured_ready = (not reasons) and tools_ready and secrets_ready

    runtime = getattr(agent_mod, "active_runtime", None)
    agents = getattr(runtime, "agents", {}) or {}

    target_rows = []
    stale_targets = []
    for target in entry.get("targets", []):
        tier, _, role = target.partition(":")
        _held = ((entry.get("profiles") or {}).get(target)
                 if isinstance(entry.get("profiles"), dict) else None)
        row = {"target": target, "ready": configured_ready, "reasons": [],
               # S8: the access profile this target holds (None = full) and
               # the profile tools the target's own config still denies —
               # disclosure only; the row grades what will be loaded.
               "profile": _held,
               "profile_tools_denied_by_config": _profile_tools_denied_by_config(
                   plugin_name, target, _held, entry=entry, store_root=store_root)}
        if not configured_ready:
            row["reasons"] = list(reasons) or ["not_ready"]
        if tier == "specialist" and (
                spec_reg := getattr(runtime, "specialist_registry", None)
        ) is not None and getattr(spec_reg, "is_disabled",
                                  lambda r: False)(role) is True:
            # (`is True`: a MagicMock registry in unit stand-ins returns a
            # truthy Mock — never let that read as "disabled".)
            # v0.74.1 (live finding 2026-07-13): the specialist-tier analogue
            # of the v0.71.1 disabled-executor rule. A DISABLED specialist is
            # dormant-by-config: reload tears it down (never registered), so
            # no new turn can enter it (FR3: informational, not blocking) —
            # its BINDING is never graded. Grading the tier-missed empty
            # binding produced the §1.4 registry-wide "Plugin degraded"
            # amplification. Sol v0.74.1-B2: configured readiness (secrets/
            # sysreqs/artifact/mcp) is PRESERVED from the row init — being
            # disabled must not mask a broken configuration. Re-checked for
            # real the moment the operator enables the specialist.
            row["state"] = "disabled"
        elif tier in ("resident", "specialist"):
            # D2 (v0.74.0): read the ONE frozen snapshot — never the legacy
            # attribute pair — and guard against racing a reload swap: the
            # object we grade must still BE runtime.agents[role] after the
            # snapshot read (verify runs on a worker thread). Bounded
            # re-read; if the mapping won't stabilize, fail EXPLICITLY
            # rather than grade a replaced object.
            agent = None
            snap = None
            stable = False
            for _ in range(5):
                agent = agents.get(role)
                snap = (getattr(agent, "plugin_binding_snapshot", None)
                        if agent is not None else None)
                if agent is agents.get(role):
                    stable = True
                    break
            if not stable:
                row["ready"] = False
                row["reasons"] = ["verify_unstable"]
                row["state"] = "unstable"
            elif agent is not None and snap is not None:
                row["state"] = "active"
                active_aid = snap.binding.get(plugin_name)
                row["active_artifact_id"] = active_aid
                # D2 disclosure: which resolver generation this binding was
                # computed against. Grading stays FR3 — binding equality is
                # authoritative (an unrelated mutation bumps the generation
                # for agents it never reconstructs; the MUTATION path is
                # where a generation mismatch hard-fails, spec D2 —
                # provisional pending the B1 spec decision, Sol r3/r4).
                row["resolution_generation"] = snap.generation
                row["generation_stale"] = (
                    snap.generation != plugin_registry.snapshot_generation())
                if active_aid != artifact_id:
                    # FR3: a stale binding is BLOCKING even while idle — the
                    # persistent Agent reuses it on its next bus/trigger turn.
                    row["ready"] = False
                    row["reasons"] = ["reload_required"]
                    stale_targets.append(target)
            else:
                row["state"] = "dormant"
        elif tier == "executor":
            # No executor runs plugins any more (the
            # configurator is a delegated run with none), so such a target is
            # never loaded — dormant, with nothing to authorize.
            row["state"] = "dormant"
        target_rows.append(row)

    # Running executor engagements on a PREVIOUS artifact — informational.
    sessions = []
    if _engagement_registry is not None:
        try:
            running = _engagement_registry.active_and_idle()
        except Exception:  # noqa: BLE001
            running = []
        for rec in running:
            for pa in getattr(rec, "plugin_artifacts", ()) or ():
                if (pa.get("name") == plugin_name
                        and pa.get("artifact_id") != artifact_id):
                    sessions.append({"engagement_id": rec.id,
                                     "artifact_id": pa.get("artifact_id")})

    # Draining resident/specialist turns still on the PREVIOUS artifact —
    # informational (Sol #4): after a reload swaps an agent, its in-flight turn
    # keeps executing the old artifact until aclose drains it (≤ pool drain
    # timeout). verify discloses it rather than silently implying it's gone.
    for d in getattr(runtime, "draining", None) or []:
        aid = (d.get("binding") or {}).get(plugin_name)
        if aid is not None and aid != artifact_id:
            sessions.append({"draining_role": d.get("role"),
                             "artifact_id": aid})

    top_ready = (configured_ready
                 and all(r["ready"] for r in target_rows))
    return {
        "ready": top_ready,
        "reasons": reasons,
        # #923: `desired` here is what verification GRADES against, and
        # `entry` is the effective read view — so `targets` is the set that
        # will actually be served, which is the only set a readiness row can
        # honestly describe (grading a target the resolver will never serve is
        # the verify/resolver disagreement Sol #8 forbids). What the operator
        # stored and Casa ignores is disclosed beside it rather than dropped.
        "desired": {"artifact_id": artifact_id,
                    "version": entry.get("version"),
                    "revision": revision, "targets": entry.get("targets", []),
                    "ignored_targets": plugin_registry.ignored_targets_for(
                        reg, plugin_name)},
        "artifact": {"present": present, "checksum_valid": checksum_valid,
                     "provenance_warning": provenance_warning},
        "tools": tools_status,
        "secrets": secrets_status,
        "mcp_commands": command_status,
        "reserved_env": reserved_env,
        "granted_tools": granted,
        "protected_tools": sorted(protected_tools),
        "protected_tool_summaries": protected_tool_summaries,
        "targets": target_rows,
        "stale_targets": stale_targets,
        "sessions_on_previous_artifact": sessions,
    }


def _tool_verify_plugin_secrets(*, plugin_name: str) -> dict:
    """Back-compat shim (one release only)."""
    state = _tool_verify_plugin_state(plugin_name=plugin_name)
    return {"secrets": state["secrets"]}


def _tool_set_plugin_env_reference(
    *,
    plugin: str,
    var_name: str,
    op_ref_or_value: str,
) -> dict:
    from plugin_env_conf import set_entry as _set_env_entry_local
    _set_env_entry_local(var_name, op_ref_or_value)
    return {"ok": True}


def _default_vault() -> str:
    """#535: the operator's configured default vault (``svc-casa/run`` exports
    the ``onepassword_default_vault`` option, "" when unset). The recipe
    doctrine promises the vault-facing tools fall back to it — without the
    fallback the configurator guessed vault names against the account."""
    return os.environ.get("ONEPASSWORD_DEFAULT_VAULT", "")


def _op_item_list(query: str, vault: str) -> dict:
    """The parsed ``op item list`` rows matching ``query`` (case-folded
    substring of the title), or a classified error dict. Rows carry titles;
    they never leave the two callers below unprojected."""
    cmd = ["op", "item", "list", "--format", "json"]
    if vault:
        cmd += ["--vault", vault]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return _op_timeout()
    if r.returncode != 0:
        return _op_failed(r.returncode)
    try:
        items = json.loads(r.stdout)
        if not isinstance(items, list):
            raise ValueError("op item list did not return a list")
    except ValueError:
        return _op_unreadable()
    if query:
        items = [i for i in items if isinstance(i, dict)
                 and query.lower() in str(i.get("title", "")).lower()]
    return {"rows": items}


# #1021: nothing writes a setup-provided value on its own. The plugin's setup
# tool creates or finds it and REPORTS the reference to wire; the configurator
# wires it. The old text ("the plugin's setup tool has not provisioned it yet")
# told a configurator the value would fill itself in, so it declared the job
# not its own while the setup report handed the job to it.
_SETUP_PROVIDED_REASON = (
    "declared in casa.setupProvides — not wired yet: the plugin's setup tool "
    "reports the value to wire, and the configurator wires it with "
    "set_plugin_env_reference and casa_reload(scope='plugin_env')")


def _tool_list_vault_items(*, query: str = "", vault: str = "") -> dict:
    vault = vault or _default_vault()
    got = _op_item_list(query, vault)
    if "error" in got:
        return got
    rows = []
    for i in got["rows"]:
        iid = i.get("id")
        cat = i.get("category")
        # #1019: the key is `matched_query`, never `name` — the value is the
        # search term the title contained, and a reader of `name` takes it for
        # the item's name (two different items read as two items sharing one).
        rows.append({"matched_query": _matched_term(i.get("title"), [query]),
                     "id": iid if isinstance(iid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", iid) else None,
                     "category": cat if isinstance(cat, str) and re.fullmatch(r"[A-Z_]{1,40}", cat) else None})
    return {"items": rows}


def _op_item_get(item: str, vault: str) -> dict:
    """The parsed ``op item get`` document, or a classified error dict. The
    document holds VALUES; it never leaves the two callers below unprojected."""
    cmd = ["op", "item", "get", item, "--format", "json"]
    if vault:
        cmd += ["--vault", vault]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return _op_timeout()
    if r.returncode != 0:
        return _op_failed(r.returncode)
    try:
        doc = json.loads(r.stdout)
        if not isinstance(doc, dict):
            raise ValueError("op item get did not return an object")
    except ValueError:
        return _op_unreadable()
    return {"doc": doc}


def _tool_get_item_fields(*, item: str, vault: str = "") -> dict:
    vault = vault or _default_vault()
    got = _op_item_get(item, vault)
    if "error" in got:
        return got
    return {"fields": _project_item_fields(got["doc"])}


def _op_failed(exit_code: int) -> dict:
    """A classified ``op`` failure. NEVER the subprocess's stderr or stdout:
    whatever ``op`` prints on failure lands in the configurator's transcript
    and from there in Telegram, and truncation is not redaction."""
    return {"error": "op_failed", "exit_code": int(exit_code)}


def _op_timeout() -> dict:
    """``op`` did not return within its budget (fixed string, no output)."""
    return {"error": "op_timeout"}


def _op_unreadable() -> dict:
    """``op`` exited 0 but its stdout was not the JSON document expected — a
    fixed string; the unparseable bytes are never forwarded."""
    return {"error": "op_unreadable"}


# Casa never repeats an operator-typed vault string. Three review rounds
# tried to decide which metadata strings were safe to echo by comparing them
# against where secrets live (field values; then every string in the
# document; then also by shape) and each round found a string that got
# through: a value duplicated into a label, a value split across labels, a
# plain-shaped secret typed as the label itself, and — the other way — an
# ordinary label withheld because the item's notes mentioned it. The mechanism
# is cut. What is returned about an item is: its op-generated id, and for each
# field its op-generated id, its type, and a ROLE from the closed set below,
# derived in-process from the label (which is never returned). In place of a
# title, the query term the item matched. A wiring reference is
# ``op://<vault>/<item id>/<field id>``, which the resolver accepts.
_FIELD_ROLES = (
    # (regex over the case-folded, whitespace-normalised label, role)
    (re.compile(r"^(oauth[ -]?)?client[ _-]?id$"), "client_id"),
    (re.compile(r"^(oauth[ -]?)?client[ _-]?secret$"), "client_secret"),
    (re.compile(r"^refresh[ _-]?token$"), "refresh_token"),
    (re.compile(r"^access[ _-]?token$"), "access_token"),
    (re.compile(r"^(api[ _-]?)?(token|bearer( token)?)$"), "token"),
    (re.compile(r"^(api[ _-]?)?(key|api key|apikey|secret[ _-]?key)$"), "api_key"),
    (re.compile(r"^(client|app|application|consumer)[ _-]?(key|secret)$"), "api_key"),
    (re.compile(r"^(secret|credential|credentials)$"), "credential"),
    (re.compile(r"^(user ?name|user|login|account[ _-]?name)$"), "username"),
    (re.compile(r"^(pass ?word|pass|pin|passphrase)$"), "password"),
    (re.compile(r"^(e[ -]?mail|e[ -]?mail address|mail|user[ _-]?email)$"), "email"),
    (re.compile(r"^(host ?name|host|server|domain)$"), "hostname"),
    (re.compile(r"^(url|uri|endpoint|base[ _-]?url|website|address)$"), "url"),
    (re.compile(r"^(account|account[ _-]?id|tenant|tenant[ _-]?id|org|organi[sz]ation|project|project[ _-]?id)$"), "account"),
    (re.compile(r"^(region|zone|environment|env)$"), "region"),
    # 1Password's own templates (API Credential, Login, Database, Server) and
    # the plugins Casa ships today (finance's BANKFEED_OP_VAULT → "Vault").
    (re.compile(r"^(vault|vault[ _-]?name|database|db|schema|port|type|connection[ _-]?options|"
                r"phone|phone number|number|chat[ _-]?id|bot[ _-]?token|webhook[ _-]?secret|"
                r"voice[ _-]?id|model|one[ _-]?time[ _-]?password|otp|notes?)$"), "other_known"),
)
_FIELD_TYPES = frozenset({"STRING", "CONCEALED", "EMAIL", "URL", "OTP", "DATE",
                          "MONTH_YEAR", "MENU", "PHONE", "ADDRESS", "REFERENCE",
                          "FILE", "SSHKEY", "CREDIT_CARD_NUMBER", "CREDIT_CARD_TYPE",
                          "GENDER", "UNKNOWN"})


def _field_role(label: object) -> str | None:
    """The fixed role a field label denotes, or None. The LABEL never leaves
    this function; a role is one of a closed set of Casa-chosen tokens, so
    nothing operator-typed reaches the result and nothing is compared against
    the document. An unrecognised label is a field the configurator asks the
    operator about, by the field's id."""
    t = label if isinstance(label, str) else ""
    t = re.sub(r"\s+", " ", t.strip().lower())
    if not t:
        return None
    for rx, role in _FIELD_ROLES:
        if rx.match(t):
            return role
    return None


def _matched_term(title: object, queries: list[str]) -> str:
    """The query term an item's title matched, never the title. Case-folded
    substring match, the same rule the listing filters by; the first matching
    query wins; an item that matched none reads ``(unnamed)``."""
    t = title.lower() if isinstance(title, str) else ""
    for q in queries:
        if q and q.lower() in t:
            return q.lower()
    return "(unnamed)"


def _project_item_fields(item: dict) -> list[dict]:
    """The ONE projection of an ``op item get`` document: per field its
    op-generated ``id``, its ``role`` (a closed set derived from the label,
    which is never returned) and its ``type`` (validated against op's own
    enum, else None). Never a label, a section, a value, a reference or a
    note. Shared by ``get_item_fields`` and ``plugin_add``'s
    ``secret_candidates`` so there is a single parser to audit for disclosure."""
    out: list[dict] = []
    for f in (item or {}).get("fields", []) or []:
        if not isinstance(f, dict):
            continue
        fid = f.get("id")
        ftype = f.get("type")
        out.append({
            "id": fid if isinstance(fid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fid) else None,
            "role": _field_role(f.get("label")),
            "type": ftype if isinstance(ftype, str) and ftype in _FIELD_TYPES else None,
        })
    return out


@tool(
    "verify_plugin_state",
    "Check tool readiness, secret resolution, and MCP cache status for a "
    "plugin. Readiness rows cover the assignments Casa serves; an ignored "
    "worker (executor:) assignment is listed under desired.ignored_targets "
    "and is not graded, because it is never loaded.",
    {"plugin_name": str},
)
async def verify_plugin_state(args: dict) -> dict:
    return _result(_tool_verify_plugin_state(plugin_name=args["plugin_name"]))


@tool(
    "verify_plugin_secrets",
    "Back-compat shim: check secret resolution for a plugin (use verify_plugin_state instead).",
    {"plugin_name": str},
)
async def verify_plugin_secrets(args: dict) -> dict:
    return _result(_tool_verify_plugin_secrets(plugin_name=args["plugin_name"]))


@tool(
    "set_plugin_env_reference",
    "Upsert a VAR=value or VAR=op://... line in plugin-env.conf.",
    {"plugin": str, "var_name": str, "op_ref_or_value": str},
)
async def set_plugin_env_reference(args: dict) -> dict:
    _record_plugin_installer([args.get("plugin")])   # #1053
    return _result(_tool_set_plugin_env_reference(
        plugin=args["plugin"],
        var_name=args["var_name"],
        op_ref_or_value=args["op_ref_or_value"],
    ))


@tool(
    "remove_plugin_env_reference",
    "Remove a VAR line from plugin-env.conf (idempotent; follow with "
    "casa_reload(scope='plugin_env') to drop it from the effective env).",
    {"plugin": str, "var_name": str},
)
async def remove_plugin_env_reference(args: dict) -> dict:
    # v0.111.0 (#236): removal counterpart of set_plugin_env_reference —
    # a plugin update that makes a var optional could not clean up its
    # now-orphaned entry (path_scope correctly blocks direct file edits).
    from plugin_env_conf import remove_entry as _remove_env_entry
    try:
        removed = _remove_env_entry(args["var_name"])
    except Exception as exc:  # noqa: BLE001 — invalid name / IO
        return _result({"ok": False, "error": str(exc)})
    return _result({"ok": True, "removed": removed})


@tool(
    "list_vault_items",
    "List 1Password vault items whose title contains the query: each as "
    "matched_query (the search term its title contained — not the item's "
    "name; the title is never returned), its op item id and its category. "
    "An omitted vault falls back to the configured "
    "onepassword_default_vault.",
    # An explicit JSON Schema: the shorthand {key: type} form marks EVERY key
    # required, so the omitted-vault call the description promises was rejected
    # by the MCP input validator before the handler's #535 fallback could run
    # (measured on the production box, 2026-09-15). `query` stays required — the recipe's
    # "never enumerate the whole vault" is enforced here, not in prose.
    {"type": "object",
     "properties": {"query": {"type": "string"},
                    "vault": {"type": "string"}},
     "required": ["query"]},
)
async def list_vault_items(args: dict) -> dict:
    return _result(await asyncio.to_thread(
        _tool_list_vault_items,
        query=args.get("query", ""),
        vault=args.get("vault", ""),
    ))


@tool(
    "get_item_fields",
    "Get a 1Password item's fields as op field ids, roles (client_id, "
    "client_secret, api_key, token, email, username, password, hostname, url, "
    "account, ...) and types. Never returns labels, sections or values; wire "
    "with op://<vault>/<item id>/<field id>. An omitted vault falls back to "
    "the configured onepassword_default_vault.",
    # Explicit schema for the same reason as list_vault_items: `vault` optional.
    {"type": "object",
     "properties": {"item": {"type": "string"},
                    "vault": {"type": "string"}},
     "required": ["item"]},
)
async def get_item_fields(args: dict) -> dict:
    return _result(await asyncio.to_thread(
        _tool_get_item_fields,
        item=args["item"],
        vault=args.get("vault", ""),
    ))


# #1047: the vault drop-off — the one vault WRITE an agent holds. It writes
# only the Casa-titled item of a drop-off the named plugin declares
# (vault_drop_off.item_title), and returns, logs and raises nothing that
# carries the value.
_DROP_OFF_STORED_NOTE = (
    "Stored. Tell the agent that runs this plugin only that it is waiting, "
    "and ask it to run its step now — never the value, in no brief, reply "
    "or summary.")


def _drop_off_target(plugin: str) -> "tuple[str | None, list[str], dict]":
    """``(runtime name, declared drop-offs, error)`` for *plugin*, matched
    against the current resolution by registry name or runtime name — the
    name the plugin itself knows is its manifest name, which is what its
    setup text tells an agent."""
    from plugin_store import StoreError, manifest_drop_offs
    resolution = plugin_registry.resolve_all()
    hits = [rp for rp in getattr(resolution, "plugins", None) or []
            if plugin in (rp.name, plugin_registry.runtime_name(rp))]
    if len(hits) != 1:
        return None, [], {"error": "unknown_plugin" if not hits
                          else "ambiguous_plugin"}
    rp = hits[0]
    try:
        declared = manifest_drop_offs(rp.manifest)
    except StoreError:
        declared = []
    return plugin_registry.runtime_name(rp), declared, {}


def _tool_vault_drop_off(*, plugin: str, drop_off: str, value: object) -> dict:
    import vault_drop_off
    runtime, declared, err = _drop_off_target(plugin)
    if err:
        return err
    if drop_off not in declared:
        return {"error": "unknown_drop_off", "declared": declared}
    if not vault_drop_off.valid_value(value):
        return {"error": "invalid_value"}
    out = vault_drop_off.store(runtime, drop_off, value)  # type: ignore[arg-type]
    if out.get("status") == "ok":
        logger.info("vault drop-off stored: plugin=%s drop_off=%s",
                    runtime, drop_off)
        return {"status": "ok", "plugin": runtime, "drop_off": drop_off,
                "note": _DROP_OFF_STORED_NOTE}
    logger.warning("vault drop-off failed: plugin=%s drop_off=%s kind=%s",
                   runtime, drop_off, out.get("error"))
    return out


@tool(
    "vault_drop_off",
    "Store a sign-in link or one-time code in the vault drop-off a plugin "
    "declares, so the plugin redeems it itself and the value never travels "
    "in a delegation brief. Use it for a link you read from a mailbox at the "
    "operator's request or one the operator pasted to you. Writes only that "
    "drop-off; returns no value. Afterwards tell the plugin's agent only that "
    "it is waiting.",
    {"type": "object",
     "properties": {"plugin": {"type": "string"},
                    "drop_off": {"type": "string"},
                    "value": {"type": "string"}},
     "required": ["plugin", "drop_off", "value"]},
)
async def vault_drop_off(args: dict) -> dict:
    return _result(await asyncio.to_thread(
        _tool_vault_drop_off,
        plugin=str(args.get("plugin") or ""),
        drop_off=str(args.get("drop_off") or ""),
        value=args.get("value"),
    ))


# --- Personality Phase A, Task 8: resident persona swap / reset -------------
#
# Both tools STAGE a desired instance tuple (InstanceDir.stage_desired) rather
# than committing directly — the actual `active := desired` commit happens at
# the NEXT BOOT's reconcile_resident_binding (agent_loader), which re-validates
# against the then-current role checksum before committing. An in-process
# reload of an already-live resident loads with binding_commit=False (#672,
# INV-CFG-003): it validates the staged candidate and refuses the hot-swap
# without touching the InstanceDir, so the commit paths are boot and a
# resident's very first load only — a personality-identity change is
# restart-to-swap on disk and in memory alike, and the persona reference scan
# keeps pinning what a resident serves until the restart. `active_runtime` is
# the established module-level accessor, set by casa_core as `agent.active_runtime = runtime`.

def _persona_roots() -> tuple[Path, ...]:
    """#323: resolve the approved persona roots through the SAME seams the
    resident loader uses (agent_loader.reconcile: ``$CASA_CONFIG_DIR/personas``
    for operator-installed packs, the module-relative image-defaults tree for
    shipped ones) — the tool that resolves/stages a persona and the loader
    that activates it must agree on the directories."""
    # #543: one authority — persona_install.persona_pack_roots — so the
    # removal/reference side and the resolve side can never disagree about
    # where a persona may live.
    from persona_install import persona_pack_roots

    return persona_pack_roots()


def _resolve_local_persona(ref: str, *, roots: "tuple[Path, ...] | None" = None):
    """Load an ALREADY LOCALLY PRESENT persona pack by exact ref (this plan does
    not fetch a bare persona from a remote repo). Persona bytes are installed
    under <config>/personas/<ns>/<slug>/<version>/ (or the image defaults) by the
    same out-of-band means as any other locally-staged content; this only loads
    and validates what is already there.

    #945: `roots` lets a caller name WHICH approved roots its ref may come from;
    None keeps the approved installed-first search every caller-supplied ref
    uses. The ref validation and the declaration check below are the part no
    caller may vary, which is why this takes a root tuple instead of each caller
    growing its own resolver — `resident_persona_reset` passes the image root
    alone, and must still refuse a traversal-bearing or mis-parked pack for the
    same reasons and with the same message shape as every other caller.

    #323: the ref's two path segments are validated with the SAME F1 patterns
    every other persona path join uses (a traversal-bearing ref like
    ``../../../tmp/x@1.0.0`` previously escaped the approved roots), and the
    loaded pack must DECLARE the identity the ref names — a pack parked at the
    wrong directory never resolves."""
    from persona_install import validate_persona_path_segments
    from persona_pack import load_persona_pack
    from specialist_install import SpecialistInstallError

    persona_id, _, version = ref.partition("@")
    try:
        validate_persona_path_segments(persona_id, version)
    except SpecialistInstallError as exc:
        # Callers uniformly handle ValueError as incompatible_or_missing.
        raise ValueError(f"invalid persona ref {ref!r}: {exc.detail}") from exc
    for root in (_persona_roots() if roots is None else roots):
        pack_dir = root / persona_id / version / "pack"
        manifest_path = root / persona_id / version / "manifest.json"
        if pack_dir.is_dir() and manifest_path.is_file():
            pack = load_persona_pack(pack_dir, manifest_path)
            if pack.persona_id != persona_id or pack.version != version:
                raise ValueError(
                    f"persona pack under {root} declares "
                    f"{pack.persona_id}@{pack.version}, not the requested {ref!r}")
            return pack
    raise ValueError(f"persona {ref!r} is not present under any configured persona root")


async def _stage_and_report(role_id: str, slot: str, binding) -> dict:
    from personality_binding import InstanceDir, make_instance_tuple
    from specialist_install import SpecialistInstallError
    import specialist_materialize

    # Resolve the bindings root through the SAME seam agent_loader's boot-time
    # reconcile uses (agent_loader._resident_bindings_root — honors an explicit
    # arg, then CASA_BINDINGS_DIR, then /config/bindings). This tool and the
    # next boot's reconcile_resident_binding() MUST agree on the on-disk
    # directory for a given slot, or a staged swap/reset is silently never
    # picked up (see tests/test_reconcile_resident_binding.py's round-trip
    # regression test).
    import agent_loader

    instance_dir = InstanceDir(agent_loader._resident_bindings_root(None) / f"resident-{slot}")
    root = binding.override_source or binding.image_default_root or ""
    tuple_ = make_instance_tuple(root=root, binding=binding, config_snapshot={})

    # Round-5 fix (F2b): the resident InstanceDir write shares the
    # personality-instance mutation lock (specialist_materialize.MATERIALIZE_LOCK)
    # with every specialist writer and with apply_persona_override's resident
    # branch. This tool handler runs on the EVENT LOOP, so acquiring the
    # threading.Lock synchronously here would block asyncio; offload the minimal
    # InstanceDir-write section (active re-read + stage_desired, both in-lock) to
    # a worker thread via asyncio.to_thread — the same off-loop pattern reload's
    # _specialist_roles_dir uses. Validation/reporting stay loop-side.
    def _locked_stage():
        from persona_install import require_persona_present

        with specialist_materialize.MATERIALIZE_LOCK:
            active = instance_dir.active()
            # #543: the persona was resolved BEFORE this lock was taken, so a
            # persona_remove could have deleted it in between; staging a
            # binding that pins absent bytes is boot-fatal for a resident
            # (INV-PERS-003). Re-prove it in-lock, where no removal can
            # interleave. (An image-default reset is a no-op here — image
            # packs are never removable.)
            require_persona_present(binding)
            instance_dir.stage_desired(tuple_)
            return active

    try:
        active = await asyncio.to_thread(_locked_stage)
    except SpecialistInstallError as exc:
        # #543: the in-lock persona re-verify refused (the pack was removed or
        # its bytes changed while this swap was in flight). Structured refusal,
        # never a raw exception out of the tool — and nothing was staged.
        return {"ok": False, "kind": exc.kind, "detail": exc.detail}
    # #931: `activation: "restart_required"` names WHEN, never what it costs.
    # Both callers (resident_persona_swap, resident_persona_reset) are
    # residents-only by their own enum, so the notice is unconditionally true
    # here. Additive — `tests/test_reconcile_resident_binding.py` round-trips
    # the staged tuple, not this envelope's key set.
    return {
        "ok": True, "role": role_id, "persona": f"{binding.persona_id}@{binding.persona_version}",
        "activation": "restart_required",
        "prior_persona": (f"{active.binding.persona_id}@{active.binding.persona_version}" if active else None),
        "conversation_notice": RESIDENT_CONVERSATION_RESET_NOTICE,
    }


def _resolve_resident_role(role_id: str):
    """Resolve a fixed resident slot's live RoleSlot from the Task 8
    ``active_runtime.role_slots`` registry, or a structured-error result."""
    from role_slot import FIXED_RESIDENT_SLOTS

    slot = role_id.split(":", 1)[1] if ":" in role_id else ""
    if slot not in FIXED_RESIDENT_SLOTS:
        return None, slot, _result({"ok": False, "kind": "invalid_role"})
    import agent as agent_mod
    runtime = getattr(agent_mod, "active_runtime", None)
    if runtime is None:
        return None, slot, _result({"ok": False, "kind": "runtime_unavailable"})
    role = runtime.role_slots.get(role_id)
    if role is None:
        return None, slot, _result({"ok": False, "kind": "role_not_loaded"})
    return role, slot, None


@tool(
    "resident_persona_swap",
    "Stage an OVERRIDE persona binding for a fixed resident slot (assistant/"
    "butler/concierge) from an already locally-present persona pack. Validates "
    "role/persona compatibility BEFORE staging anything. Takes effect only after "
    "casa_restart_supervised (personality-identity changes are restart-to-swap, "
    "never hot-reloaded). Say this when you report the staging: "
    + RESIDENT_CONVERSATION_RESET_NOTICE,
    {"type": "object", "properties": {
        "role": {"enum": ["resident:assistant", "resident:butler", "resident:concierge"]},
        "persona_ref": {"type": "string"},
    }, "required": ["role", "persona_ref"]},
)
async def resident_persona_swap(args: dict) -> dict:
    from personality_binding import check_persona_requirements, materialize_override_binding

    role_id = args["role"]
    role, slot, err = _resolve_resident_role(role_id)
    if err is not None:
        return err
    try:
        persona = _resolve_local_persona(args["persona_ref"])
        check_persona_requirements(role.normalized, persona)  # rejects BEFORE any staging
    except ValueError as exc:
        return _result({"ok": False, "kind": "incompatible_or_missing_persona", "detail": str(exc)})
    binding = materialize_override_binding(
        role=role, persona=persona, override_source=f"operator:{args['persona_ref']}",
    )
    return _result(await _stage_and_report(role_id, slot, binding))


@tool(
    "resident_persona_reset",
    "Stage a reset for a fixed resident slot's binding back to the CURRENT "
    "in-image default persona, undoing any override (spec §2.3/§4.4's "
    "always-available reset). Takes effect only after casa_restart_supervised. "
    "Say this when you report the staging: "
    + RESIDENT_CONVERSATION_RESET_NOTICE,
    {"type": "object", "properties": {
        "role": {"enum": ["resident:assistant", "resident:butler", "resident:concierge"]},
    }, "required": ["role"]},
)
async def resident_persona_reset(args: dict) -> dict:
    from persona_install import image_personas_root
    from personality_binding import (
        IMAGE_DEFAULT_PERSONA_BY_SLOT,
        check_persona_requirements,
        materialize_image_default_binding,
    )

    role_id = args["role"]
    role, slot, err = _resolve_resident_role(role_id)
    if err is not None:
        return err
    default_ref = IMAGE_DEFAULT_PERSONA_BY_SLOT[slot]
    # Run the SAME resolve-then-validate-before-staging sequence as
    # resident_persona_swap — a missing/uninstalled default persona blob or a
    # compatibility failure is returned as a structured error, never an
    # unhandled ValueError out of the handler, and nothing is staged before
    # validation passes.
    #
    # #945: from the IMAGE root alone, which is the only root that decides what
    # this reset actually delivers. `reconcile_resident_binding`'s non-override
    # arm re-materializes the candidate from `agent_loader._load_default` — image
    # root only — and ignores the staged tuple's persona bytes, so an
    # installed-first resolution here validated and staged an identity boot would
    # never promote; and because the resolver loads the FIRST root that answers
    # for the ref and never falls through on a load failure, an unreadable pack
    # parked at this ref under `$CASA_CONFIG_DIR/personas` turned the
    # always-available reset (spec §4.4) into a refusal while the image default
    # was intact — a recovery action refused by the very thing being recovered
    # from. The swap's caller-supplied ref keeps its installed-first search; only
    # this in-image constant is pinned to the image root.
    try:
        persona = _resolve_local_persona(default_ref, roots=(image_personas_root(),))
        check_persona_requirements(role.normalized, persona)  # rejects BEFORE any staging
    except ValueError as exc:
        return _result({"ok": False, "kind": "incompatible_or_missing_persona", "detail": str(exc)})
    binding = materialize_image_default_binding(role=role, persona=persona, image_default_root=default_ref)
    return _result(await _stage_and_report(role_id, slot, binding))


# ---------------------------------------------------------------------------
# #543 — persona disposal tools (list / remove / prune / ack revoke)
#
# All three mutating tools run under _PLUGIN_TOOLS_LOCK, like every other
# lifecycle mutation: it is what keeps a bundle transaction from being
# mid-flight (its journal captures tuples that a compensation can restore)
# while a persona is being removed. The removal core then takes
# MATERIALIZE_LOCK in the worker thread — the same order every lifecycle path
# already uses.
# ---------------------------------------------------------------------------


@tool(
    "persona_list",
    "List the personas installed under /config/personas — id, version, checksum, display name, "
    "which agents' bindings still refer to each one, and whether it can be removed. Also reports "
    "the image-shipped persona packs, which are part of the image and are never removable. "
    "Read-only.",
    {"type": "object", "properties": {}},
)
async def persona_list(args: dict) -> dict:
    from persona_install import (
        image_personas_root, list_installed_personas, persona_references,
    )
    from personality_binding import IMAGE_DEFAULT_PERSONA_BY_SLOT
    from specialist_install import SpecialistInstallError

    def _sync() -> dict:
        # Sol/Terra diff r1: the WHOLE body, not just the reference scan — an
        # OSError from any directory walk (bindings, specialists, personas)
        # must come back as this tool's {ok, kind} envelope, because these
        # tools are the operator's only sanctioned surface onto that tree.
        try:
            references = persona_references()
            installed = list_installed_personas(references=references)
            # #945: the named accessor, not an index into the approved-roots
            # tuple — this probe has always wanted THIS root, and said so only
            # by knowing that `persona_pack_roots()` happens to put it second.
            image_root = image_personas_root()
            image_defaults = []
            for slot, ref in sorted(IMAGE_DEFAULT_PERSONA_BY_SLOT.items()):
                persona_id, _, version = ref.partition("@")
                image_defaults.append({
                    "slot": slot, "ref": ref,
                    "present": (image_root / persona_id / version / "manifest.json").is_file(),
                })
        except SpecialistInstallError as exc:
            return {"ok": False, "kind": exc.kind, "detail": exc.detail}
        except OSError as exc:
            return {"ok": False, "kind": "scan_failed", "detail": str(exc)}
        return {"ok": True, "installed": installed, "image_defaults": image_defaults}

    return _result(await asyncio.to_thread(_sync))


@tool(
    "persona_remove",
    "Remove an INSTALLED persona version from /config/personas and revoke its install approval. "
    "REFUSES (kind: persona_pinned) while any resident or specialist binding — active, staged, or "
    "retained for rollback — still refers to it: reset or re-apply those agents first (and restart, "
    "for a resident). Image-shipped personas are never removable.",
    {"type": "object", "properties": {
        "persona_id": {"type": "string"}, "version": {"type": "string"}},
     "required": ["persona_id", "version"]},
)
async def persona_remove(args: dict) -> dict:
    from persona_install import remove_installed_persona
    from specialist_install import SpecialistInstallError

    async with _PLUGIN_TOOLS_LOCK:
        # Kill any pending install keyboard for this persona FIRST, on the LOOP
        # — ChallengeCoordinator is loop-confined and single-owner. This is
        # best-effort (an already-answered challenge cannot be cancelled); the
        # authoritative guard is the ack ledger's revocation generation, which
        # the removal advances before it deletes anything.
        CHALLENGES.cancel_matching(persona=args["persona_id"])
        try:
            result = await _to_thread_settled(
                remove_installed_persona,
                persona_id=args["persona_id"], version=args["version"])
        except SpecialistInstallError as exc:
            return _result({"ok": False, "kind": exc.kind, "detail": exc.detail})
        except OSError as exc:
            # A directory that cannot be walked or a rename that fails at the
            # filesystem level: structured refusal, never a raw exception.
            return _result({"ok": False, "kind": "scan_failed", "detail": str(exc)})
    return _result(result)


@tool(
    "persona_prune",
    "Remove every installed persona version that no binding refers to any more (the persona "
    "garbage collection sweep). Each removal also revokes that version's install approval. "
    "Reports what was removed and what was kept, with the reason. Run persona_list first so the "
    "operator sees what will go.",
    {"type": "object", "properties": {}},
)
async def persona_prune(args: dict) -> dict:
    from persona_install import prune_installed_personas
    from specialist_install import SpecialistInstallError

    async with _PLUGIN_TOOLS_LOCK:
        try:
            result = await _to_thread_settled(prune_installed_personas)
        except SpecialistInstallError as exc:
            return _result({"ok": False, "kind": exc.kind, "detail": exc.detail})
        except OSError as exc:
            return _result({"ok": False, "kind": "scan_failed", "detail": str(exc)})
    return _result({"ok": True, **result})


@tool(
    "persona_ack_revoke",
    "Revoke the operator's install approval for a persona — all versions, or one version — WITHOUT "
    "touching any installed bytes. A later install of the same persona re-prompts for consent. "
    "Sibling of trigger_ack_revoke / callback_ack_revoke / event_ack_revoke.",
    {"type": "object", "properties": {
        "persona_id": {"type": "string"}, "version": {"type": "string"}},
     "required": ["persona_id"]},
)
async def persona_ack_revoke(args: dict) -> dict:
    from persona_install import PersonaInstallAckStore, PersonaLedgerInvalid
    import specialist_materialize

    persona_id = args["persona_id"]
    version = args.get("version") or None

    def _sync() -> dict:
        # Under MATERIALIZE_LOCK for the same reason commit_persona_install
        # publishes under it: the commit path re-reads the ack inside that lock,
        # so revoking under it means a revoke can never land between that read
        # and the publication it authorizes.
        with specialist_materialize.MATERIALIZE_LOCK:
            try:
                removed = PersonaInstallAckStore().revoke(
                    persona_id=persona_id, version=version)
            except (OSError, PersonaLedgerInvalid) as exc:
                return {"ok": False, "kind": "ack_revoke_failed", "detail": str(exc)}
        return {"ok": True, "persona_id": persona_id, "version": version,
                "revoked_acks": len(removed)}

    async with _PLUGIN_TOOLS_LOCK:
        CHALLENGES.cancel_matching(persona=persona_id)
        return _result(await _to_thread_settled(_sync))


# Module-level tool registry — iterated by create_casa_tools() for the SDK
# path. Adding a tool here exposes it automatically.
CASA_TOOLS: tuple = (
    send_message,
    stop_work,                     # the resident's direct stop of a specialist's own work
    offer_own_chat,                # the resident offers a specialist its own chat
    send_media,
    ask_user,
    delegate_to_agent,
    hand_off,                      # browser executor §5.4 — specialists only,
                                   # granted in code (never by a bundle)
    start_job,
    voice_job_status,
    cancel_voice_job,
    continue_voice_job,
    search_memory,                 # memory: BM25 over the saved conversation,
    browse_memory_day,             # a day in order, an entry by id, and the
    open_memory,                   # correction mark (each read filtered per
    correct_memory,                # object at the reader's clearance)
    wipe_memory,                   # #411 — operator-consented long-term wipe;
                                   # NEVER add to SPECIALIST_CASA_TOOL_ALLOWLIST
    get_schedule,
    list_inbound_files,            # #1036 — files the operator sent in Telegram
    share_inbound_file,            # #486 — one of those files, to a plugin
    set_reminder,                  # #396 — durable reminders
    cancel_reminder,               # #396
    ack_event,                     # #419 — plugin-events delivery receipt
    emit_completion,
    report_job_progress,
    ask_operator,                  # a specialist's job asks the operator
    cancel_engagement,
    query_engager,
    config_git_commit,
    casa_reload,
    casa_restart_supervised,            # NEW — Task D.2
    casa_reload_triggers,
    config_trigger_upsert,          # #403 — the ONE writer path for
    config_trigger_delete,          #        agents/<role>/triggers.yaml
    # Unified plugin architecture (§3.13) — registry tools.
    plugin_add,
    plugin_update,
    plugin_assign,
    plugin_unassign,
    plugin_remove,
    trigger_ack_revoke,            # Release B — plugin-trigger consent off-switch
    callback_ack_revoke,           # plugin-callback consent off-switch
    event_ack_revoke,              # #419 — plugin-event consent off-switch
    consent_reprompt,              # #494 — on-demand consent-DM re-issue
    plugin_list,
    plugin_status,                 # #555 — read-only plugin health + setup
                                   # history for the RESIDENT; the mutation
                                   # tools above stay configurator-only
    verify_plugin_state,
    verify_plugin_secrets,
    set_plugin_env_reference,
    remove_plugin_env_reference,   # v0.111.0 (#236) — removal counterpart
    list_vault_items,
    get_item_fields,
    vault_drop_off,
    # Personality Phase A, Task 8 — configurator-only resident persona control.
    resident_persona_swap,
    resident_persona_reset,
    # Personality Phase A, Plan 2 Task N1b — configurator-only specialist
    # install-from-repo tools.
    specialist_install_inspect,
    specialist_install_request_approval,   # the approval card for an install or upgrade
    specialist_install_commit,
    # Personality Phase A, Plan 2 Task N1c — configurator-only specialist
    # upgrade/rollback/uninstall tools.
    specialist_upgrade,
    specialist_rollback,
    specialist_uninstall,
    # Personality Phase A, Plan 2 Task N1d — configurator-only bare-persona
    # repo install/apply tools.
    persona_install_inspect,
    persona_install_commit,
    persona_apply,
    persona_list,                  # #543 — the disposal side of the lifecycle
    persona_remove,
    persona_prune,
    persona_ack_revoke,
)


# #369 (Terra diff-gate r1): the context-rebuild fence is enforced CENTRALLY,
# by wrapping every Casa tool handler at registry definition — not by
# per-tool calls a new (or overlooked) tool can silently omit.
# create_casa_tools iterates CASA_TOOLS, so one wrap covers every tool. Only
# the terminal-binding completion tool is exempt — its output is in-flight-turn
# residual and its idempotency check needs the record.
_REBUILD_FENCE_EXEMPT = frozenset({"emit_completion"})

# #541 layer 3 — the dispatch-time specialist ceiling. Applied in the SAME
# central wrapper as the #369 fence, so it covers every casa tool by
# construction — including engagement records
# PINNED before the install/load ceilings existed (a pre-fix bundle's
# self-granted spawn tool stays in record.tools_allowed and would otherwise
# still be honored; design r2, Sol S2a + Terra S1). Union with the
# launch-mandatory grants so the two universal tools can never fall out of
# the ceiling if the allowlist and MANDATORY_BRIDGE_CASA_GRANTS drift.
from specialist_component import SPECIALIST_CASA_TOOL_ALLOWLIST

_SPECIALIST_DISPATCH_CEILING: frozenset[str] = frozenset(
    SPECIALIST_CASA_TOOL_ALLOWLIST
    | {"report_job_progress", "ask_operator"}
    | {g.rsplit("__", 1)[-1] for g in MANDATORY_BRIDGE_CASA_GRANTS}
)


def _specialist_ceiling_refusal(tool_name: str) -> "dict | None":
    """Refuse a casa tool outside the ceiling when the bound engagement is a
    SPECIALIST. Executor records are not ceilinged (their definitions are
    image-owned, not third-party); unbound callers (resident/operator
    in-process turns, ephemeral delegations) are governed by their own
    loaded config, which layers 1-2 already clamp."""
    eng = engagement_var.get(None)
    if eng is None or getattr(eng, "kind", "") != "specialist":
        return None
    if tool_name in _SPECIALIST_DISPATCH_CEILING:
        return None
    return {
        "status": "error", "kind": "specialist_tool_ceiling",
        "message": (
            f"{tool_name} is not grantable to a specialist engagement "
            "(consumer-safe ceiling, #541)."
        ),
    }


def _fence_wrapped(tool_obj) -> None:
    inner = tool_obj.handler
    _name = tool_obj.name

    async def fenced(args: dict) -> dict:
        refusal = _engagement_rebuild_fence()
        if refusal is not None:
            return _result(refusal)
        ceiling = _specialist_ceiling_refusal(_name)
        if ceiling is not None:
            return _result(ceiling)
        return await inner(args)

    fenced.__name__ = getattr(inner, "__name__", tool_obj.name)
    tool_obj.handler = fenced


for _tool_obj in CASA_TOOLS:
    if _tool_obj.name not in _REBUILD_FENCE_EXEMPT:
        _fence_wrapped(_tool_obj)
del _tool_obj


# #1075: the tools that send model text to the operator. Every CALL leaves a
# SendAttempt on the scope it commits under, because a send refused before
# admission opens no OperatorSend at all — and a buffered turn that closes in
# `<silent/>` may drop its earlier text only when every send it tried went out.
_ACCOUNTED_SENDS = frozenset({"send_message", "send_media", "ask_user"})


def _account_send_attempts(tool_obj) -> None:
    """Wrap a send tool's REGISTERED handler, outermost (outside the fence
    above, so every refusal the entry point returns is seen): open an attempt
    at entry, on the scope the handler's entry snapshot resolves to (the same
    resolution the body uses — an engagement's, else the turn's), and resolve
    it from the tool's own error flag. Nothing here changes a result, and
    accounting never fails a send."""
    inner = tool_obj.handler
    _name = tool_obj.name

    async def accounted(args: dict) -> dict:
        attempt = None
        try:
            scope = _current_scope(_snapshot_origin())
            if scope is not None:
                attempt = scope.open_attempt(_name)
        except Exception:  # noqa: BLE001 — accounting never breaks a send
            logger.warning("send accounting failed for %s", _name,
                           exc_info=True)
        try:
            result = await inner(args)
        except BaseException:
            if attempt is not None:
                attempt.state = "failed"
            raise
        if attempt is not None:
            attempt.state = ("failed" if isinstance(result, dict)
                             and result.get("is_error") else "ok")
        return result

    accounted.__name__ = getattr(inner, "__name__", _name)
    tool_obj.handler = accounted


for _tool_obj in CASA_TOOLS:
    if _tool_obj.name in _ACCOUNTED_SENDS:
        _account_send_attempts(_tool_obj)
del _tool_obj


def select_casa_tools(
    allowed_tools: frozenset[str] | None = None,
) -> tuple[SdkMcpTool, ...]:
    """Return the Casa tools granted to one resolved MCP server."""
    selected = list(CASA_TOOLS)
    if allowed_tools is not None and "mcp__casa-framework" not in allowed_tools:
        selected = [
            candidate for candidate in CASA_TOOLS
            if f"mcp__casa-framework__{candidate.name}" in allowed_tools
        ]
    return tuple(selected)


def create_casa_tools(
    allowed_tools: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Create and return the casa-framework MCP server config."""
    selected = select_casa_tools(allowed_tools)
    server = create_sdk_mcp_server(
        name="casa-framework",
        tools=list(selected),
    )
    # S-1 (block-S live finding 2026-07-15): opt the framework server out of
    # Claude Code's ToolSearch deferral. Since CLI ~2.1.69 ALL MCP tool
    # schemas are deferred by default (allow-listing does not exempt them);
    # cold voice sessions burned 1-4 ToolSearch model round-trips (4.7→27.5s,
    # escalating) resolving delegate_to_agent before they could call it —
    # eating the 27s voice budget. `alwaysLoad` (CLI ≥2.1.121, incl. the
    # 2.1.220 pin) eager-loads every tool of THIS server at start; the
    # SDK transport forwards all non-`instance` keys of an sdk-type server
    # config to --mcp-config verbatim. Plugin/HA/n8n servers deliberately
    # keep deferral — this is the small always-needed core surface only.
    server["alwaysLoad"] = True
    return server
