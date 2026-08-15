"""Core Agent class -- orchestrates SDK, memory, sessions, and channels."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Literal, Mapping

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ProcessError,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import plugin_registry
from plugin_grants import (
    grants_for_resolution,
    make_fail_closed_can_use_tool,
    sanitized_env_for_resolution,
)

from bus import BusMessage, MessageBus, MessageType
from channels import ChannelManager, DeliveryOutcome
from claude_runtime import CLAUDE_CLI_PATH
from config import AgentConfig
from specialist_registry import DelegationComplete
from hooks import resolve_hooks
from log_cid import cid_var
import sdk_logging
from mcp_registry import McpServerRegistry
from channel_trust import channel_trust_display
from timekeeping import compose_time_envelope, resolve_tz
from hindsight_ids import bank_id
from sensitivity import clearance_for_origin, readable_tiers
from personality_types import SpeakerProvenance
from recall_renderer import (
    READABLE_SLICE_PROMPT_LINE,
    provenance_view,
    render_recall,
)
from speaker_provenance import UserProvenance, provenance_from_mapping
from session_saver import freshness_window, retain_cold_session, save_session
from semantic_memory import NoOpSemanticMemory, RecallUnavailable, SemanticMemory
from session_registry import (
    SessionRegistry,
    _is_uuid_scope,
    build_scoped_session_key,
)
from sdk_client_pool import (
    ManagedSdkClient,
    PoolUnavailable,
    SdkClientPool,
)
from retry import retry_sdk_call
from tokens import (
    BudgetTracker,
    estimate_tokens,
    extract_usage,
    format_turn_summary,
)
from error_kinds import (  # noqa: F401 — selected names are re-exported
    ErrorKind,
    VoiceToolLoopError,
    _classify_error,
    _USER_MESSAGES,
)
from voice_turn_guard import VoiceTurnGuard

logger = logging.getLogger(__name__)


# #573: one writer per scheduled session.
#
# A SCHEDULED turn bypasses the warm client pool (AR-6) and so takes no
# cross-turn lock, while a delegation completion for that same session is
# re-synthesized as a pooled REQUEST and a scheduled ask's answer arrives as a
# second SCHEDULED turn. Nothing made those three mutually exclusive: two of
# them could resume one SDK session id concurrently and fork it. This gate
# serializes every turn that can touch such a session, keyed by channel_key,
# and is held across the SDK attempt AND the registry publish — the bypass
# path publishes its new sid after the attempt returns, so releasing earlier
# would let the next turn read the predecessor.
#
# Refcounted rather than a plain dict-of-locks: the key space is per-boot
# unbounded, and an entry that nothing holds is deleted rather than retained.
_SESSION_GATES: "dict[str, list]" = {}


@asynccontextmanager
async def session_write_gate(channel_key: str):
    entry = _SESSION_GATES.get(channel_key)
    if entry is None:
        entry = [asyncio.Lock(), 0]
        _SESSION_GATES[channel_key] = entry
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] <= 0 and _SESSION_GATES.get(channel_key) is entry:
            del _SESSION_GATES[channel_key]


def _needs_session_gate(msg: BusMessage) -> bool:
    """Whether *msg* can touch a scheduled session's SDK session.

    Both arms are Casa-owned and unspoofable: the message type is set by the
    dispatcher, and ``_scheduled_delivery`` is stripped from every external
    context. Keying on the message type alone missed the delegation-completion
    REQUEST, which is the pooled half of the race.
    """
    return (
        msg.type == MessageType.SCHEDULED
        or msg.context.get("_scheduled_delivery") is True
    )

# Module-level driver/provider/registry references written by casa_core.main
# so tool handlers can reach them without circular imports.
active_engagement_driver = None   # InCasaDriver | None, set by casa_core.main
active_executor_registry = None   # ExecutorRegistry | None, set by casa_core.main
active_claude_code_driver = None  # ClaudeCodeDriver | None, set by casa_core.main
active_runtime = None             # CasaRuntime | None, set by casa_core.main (Task C.3)
active_semantic_memory = None     # SemanticMemory | None, set by casa_core.main
active_session_registry = None    # SessionRegistry | None, set by casa_core.main (#411)

# Phase 3.1: delegating-turn origin. Set by Agent._process for the
# duration of a turn so the `delegate_to_agent` tool handler can read
# the channel/chat_id/cid/role/user_text of the outer user turn without
# threading them through function arguments. ContextVar semantics —
# asyncio.create_task snapshots the value, so late-completing specialist
# tasks still see the right origin.
origin_var: ContextVar[dict | None] = ContextVar("origin_var", default=None)

# Reserved provenance markers `_process` copies from the bus context onto the
# turn's origin snapshot. All of them are server-set and stripped from any
# external context by `provenance.sanitize_external_context`, so whatever
# reaches here is Casa's own value. A marker NOT named here never reaches
# `origin_var`, and every gate that reads the origin therefore cannot see it —
# which is the fail-closed direction, and also why adding a marker means adding
# it here as well as at its ingress.
#
#   synthetic / button_answer  — synthetic turn replay, inline-button answers
#   _origin_route / _origin_clearance — Release A containment (ingress + tier)
#   _operator_turn             — #283 live-operator marker for the spawn cap
#   _scheduled_delivery        — #485 Casa's own schedule fired this turn
#   _scheduled_epoch           — #573 the trigger-lifecycle epoch it fired under
COPIED_CONTEXT_MARKERS = (
    "synthetic", "button_answer", "_origin_route", "_origin_clearance",
    "_operator_turn", "_scheduled_delivery", "_scheduled_epoch",
)

# Personality Task 14 / GH #199: the per-turn explanation draft. ``_build_options``
# is the ONLY seam that observes the selected prompt projection + the auto-recall
# hits, so it stashes those fields here; ``_process`` reads the draft at
# end-of-turn and writes exactly one ``ExplanationStore.record()``. Same-task only
# — ``_build_options`` is awaited directly inside the turn coroutine (both the
# bypass path and via the pool's ``turn()``), so a mutable draft dict set here is
# visible where the record is written, exactly like ``origin_var``. A warm-reuse
# pool turn SKIPS ``_build_options`` and leaves the draft empty; ``_process`` then
# records only the deterministic projection/identity fields it can derive itself
# (honest partial record — no recall ran, the cached prompt was reused).
_explain_draft_var: ContextVar[dict | None] = ContextVar(
    "casa_explain_draft", default=None,
)


def _projection_name_for_turn(channel: str, origin_route: str | None) -> str:
    """Name the bundle projection actually SERVED for this turn, in lockstep
    with ``prompt_compiler.projection_for`` — the function that selects the
    served projection on ``_build_options``' normal path (``webhook_trigger``
    → restricted_webhook, voice → voice, else text).

    Mg (whole-branch review): this MUST mirror ``projection_for``'s predicate
    (``origin_route == "webhook_trigger"``), NOT a broader
    ``channel == "webhook" and route != "invoke"`` test. ``_build_options``
    handles the untrusted-webhook short-circuit SEPARATELY and records
    "restricted_webhook" directly there (that path never reaches this
    function); every call site that DOES reach here — the warm-reuse pool
    record path and the normal-path stash — needs the projection
    ``projection_for`` chose, and ``projection_for`` deliberately falls back
    to the text surface for a missing/unknown webhook route rather than
    treating it as restricted."""
    if origin_route == "webhook_trigger":
        return "restricted_webhook"
    if channel == "voice":
        return "voice"
    return "text"


def _attribution_label(hit, *, clearance: str | None) -> str:
    """Derive an HONEST, operator-safe attribution label for one recall hit from
    its clearance/surface-gated provenance view. Never emits the hit's raw
    ``application_tags`` (which may carry reserved ``casa-source-`` tags the store
    rejects) and never a person's display name — only speaker_kind + role/persona
    IDS, so the label is safe to expose in the DEFAULT (non-sensitive) explain
    output."""
    view = provenance_view(
        hit.provenance, clearance=clearance or "public", surface="text",
    )
    if view is None:
        return "unattributed"
    if view.speaker_kind == "user":
        return "user"
    parts = [view.speaker_kind]
    if view.role_id:
        parts.append(view.role_id)
    label = ":".join(parts)
    if view.persona_id and view.persona_version:
        label += f"/{view.persona_id}@{view.persona_version}"
    return label


# Type alias for the streaming callback
OnTokenCallback = Callable[[str], Awaitable[None]]


def _live_agent_directory() -> dict[str, str] | None:
    """Live role → persona display name, or ``None`` when tools is not
    initialized (see :func:`tools.agent_display_names`).

    Deferred import: ``tools`` imports ``agent`` for the origin ContextVar, so
    this cannot be a module-level import. It matches the other tools imports
    in this module, which are all function-local for the same reason.
    """
    from tools import agent_display_names
    return agent_display_names()


def _render_delegates_block(delegates, registry, *, live_names=None) -> str:
    """Render the <delegates> system-prompt block.

    Empty string when ``delegates`` is empty so callers can append
    unconditionally without polluting the prompt.

    #436: ``live_names`` — role → display name for every currently
    dispatchable agent — is AUTHORITATIVE for both membership and name when
    supplied. ``registry`` is the construction-time ``AgentRegistry``, and
    that object is immutable: a live Agent keeps the instance it was built
    with (``reload._construct_agent``), deliberately, so that a later
    ``runtime.agent_registry`` rebind cannot reach a running agent. Reloading
    a single role therefore refreshed the delegation role map while leaving
    every OTHER agent advertising its boot-time snapshot — renaming a persona
    left the assistant offering a name the resolver no longer accepted, which
    is #433's `delegation_not_declared` all over again for that one name.
    Deriving the advertised name and the accepted name from one live map at
    the point of USE makes that disagreement unrepresentable.

    ``registry`` remains the fallback for callers with no initialized tools
    module (unit tests; boot before ``init_tools``), which is why
    :func:`tools.agent_display_names` reports an empty map as ``None`` rather
    than as an empty directory — see its docstring for what that conflates.
    """
    if not delegates:
        return ""

    # A delegates.yaml entry may point at a specialist that is now disabled
    # (enabled: false) or removed. Such an agent is NOT callable — the
    # delegate_to_agent tool rejects it with unknown_agent — so do not advertise
    # it. Both sources hold exactly residents + ENABLED specialists; filter on
    # that. (Neither → back-compat: render every declared delegate.)
    def _known(role: str) -> bool:
        if live_names is not None:
            return role in live_names
        return registry is None or registry.is_known(role)

    def _display_name(role: str) -> str:
        if live_names is not None:
            return live_names.get(role, role)
        return registry.role_to_name(role) if registry is not None else role

    visible = [d for d in delegates if _known(d.agent)]
    if not visible:
        return ""
    # #433: the ROLE ID leads. `delegate_to_agent` is keyed on the role, and
    # rendering the persona name first made the name the salient token — the
    # model addressed delegates by it and the ACL refused, reporting a
    # WIRING fault for what was only a naming mismatch. The persona name
    # stays visible (the model still has to map "ask Tina to..." onto a
    # role), but as the parenthetical. This converges with the
    # specialist-side renderer, `agent_loader._render_delegates_section`,
    # which has always emitted role ids only.
    lines = ["<delegates>"]
    for d in visible:
        name = _display_name(d.agent)
        label = f"{d.agent} ({name})" if name != d.agent else d.agent
        lines.append(f"- {label} — {d.purpose}")
        lines.append(f"  Delegate when: {d.when}")
    lines.append("</delegates>")
    return "\n".join(lines)


def _render_executors_block(executors) -> str:
    """Render the <executors> system-prompt block (assistant role only)."""
    if not executors:
        return ""
    lines = ["<executors>"]
    for e in executors:
        lines.append(f"- {e.executor_type} — {e.purpose}")
        lines.append(f"  Engage when: {e.when}")
    lines.append("</executors>")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class SessionEntrySnapshot:
    """An immutable copy of a persisted session entry, decoded once under the
    entry lock so no later mutation of the source dict can bleed into a
    resume/retain decision (Task 9). ``binding_digest``/``speaker_provenance``/
    ``user_provenance`` are absent (``None``) on legacy pre-Task-9 entries."""
    agent: str
    sdk_session_id: str
    last_active: str | None
    scope_class: str | None
    binding_digest: str | None
    speaker_provenance: SpeakerProvenance | None
    user_provenance: SpeakerProvenance | None


@dataclass(frozen=True, slots=True)
class ResumeDecision:
    """The structured outcome of the resume gate (replaces the former
    ``(decision, save_old)`` tuple). ``retain_old``/``old`` drive
    save-before-overwrite of a superseded cold session.

    ``reason="retiring"`` (#290/#411) is never produced by
    :func:`_resume_decision` itself — it is the steer applied on top of it
    (by the pool, or by the bypass path) while a reset/wipe holds a
    retirement claim on the key. ``fence_generation`` (#411) is the retain
    fence generation captured in the same no-await block as the decision;
    a cold retain spawned from this decision passes it to the fence so a
    wipe that completed between decision and retain makes the retain
    discard instead of restoring pre-wipe content."""
    action: Literal["resume", "new"]
    resume_sid: str | None
    retain_old: bool
    old: SessionEntrySnapshot | None
    reason: Literal[
        "missing", "role_mismatch", "binding_mismatch",
        "fresh", "expired", "invalid_entry", "retiring",
    ]
    fence_generation: int | None = None


def _decode_provenance(raw: object) -> SpeakerProvenance | None:
    if raw is None:
        return None
    try:
        return provenance_from_mapping(raw)
    except ValueError:
        return None  # a corrupt stored snapshot is treated as absent, never fabricated


def snapshot_session_entry(entry: dict | None) -> SessionEntrySnapshot | None:
    """Decode a persisted entry into an immutable snapshot, or ``None`` when
    there is nothing resumable (no entry / no sid / no agent)."""
    if not entry or not entry.get("sdk_session_id") or not entry.get("agent"):
        return None
    return SessionEntrySnapshot(
        agent=str(entry["agent"]),
        sdk_session_id=str(entry["sdk_session_id"]),
        last_active=entry.get("last_active"),
        scope_class=entry.get("scope_class"),
        binding_digest=entry.get("binding_digest"),
        speaker_provenance=_decode_provenance(entry.get("speaker_provenance")),
        user_provenance=_decode_provenance(entry.get("user_provenance")),
    )


def agent_home_for_role_id(role_id: str) -> str:
    """Map a canonical ``kind:slot`` role id to its on-disk agent home — the
    transcript cwd ``get_session_messages``/``delete_session`` read.

    Bare ``/config/agent-home/<slot>`` for EVERY kind (resident, specialist,
    executor) — this MUST stay byte-for-byte consistent with the actual
    provisioning in ``agent_home.py``'s ``provision_agent_home`` (``agent_dir
    = home_root / role``, a bare slug — no ``{kind}s/`` nesting) and with
    ``Agent._process``'s own ``agent_home = f"/config/agent-home/{self.config.role}"``
    derivation. Global slug uniqueness (enforced elsewhere) means bare slugs
    never collide across kinds, so the bare form is safe for all of them
    (executors get no provisioned home directory at all today, but any
    session-entry lookup for one must still resolve to the same bare path
    a resident/specialist would use). If a future plan changes the on-disk
    layout (e.g. nesting specialists under ``specialists/<slot>``), BOTH this
    function AND ``agent_home.py``'s provisioning must change together.

    Raises ``ValueError`` on a non-canonical (short, pre-Task-9) role
    string."""
    kind, separator, slot = role_id.partition(":")
    if separator != ":" or kind not in {"resident", "specialist", "executor"} or not slot:
        raise ValueError(f"invalid canonical role id {role_id!r}")
    return f"/config/agent-home/{slot}"


def speaker_provenance_for_role(cfg: AgentConfig) -> SpeakerProvenance:
    """Task 9 (finding 3): the ONE fallback policy for a config's own executing
    identity when ``cfg.speaker_provenance`` is unset — used at every
    ``SessionRegistry.register`` call site so a nullable ``cfg.speaker_provenance``
    never reaches ``register``'s non-null ``validate_speaker_provenance`` gate.

    - A resident (Task 8 always activates this) keeps its real persona identity.
    - ``cfg.kind == "executor"`` (never persona/binding-bearing per the taxonomy)
      gets a stable, honestly-labeled executor identity.
    - Anything else with NO activated binding yet (an unbound Plan-1 specialist,
      or an identity-less unit-test config) gets the explicit unattributed
      ``system`` identity — a VALID provenance (every field null) that is honest
      about "unattributed" rather than fabricating a persona. Resolves itself the
      moment Plan 2 populates ``cfg.speaker_provenance`` for specialists."""
    if cfg.speaker_provenance is not None:
        return cfg.speaker_provenance
    if cfg.kind == "executor" and cfg.role_id:
        return SpeakerProvenance(speaker_kind="executor", role_id=cfg.role_id)
    return SpeakerProvenance(speaker_kind="system")


def _resume_decision(
    channel: str, entry: dict | None, now: datetime, *,
    role_id: str, binding_digest: str,
) -> ResumeDecision:
    """Spec §3.3/§4.2 + personality Task 9: resume iff a stored entry exists,
    matches this config's ``{role_id, binding_digest}`` identity, AND is within
    its channel freshness window. A role or binding mismatch — or a stale but
    present session — signals save-before-overwrite via ``retain_old``/``old``.

    The role gate is checked BEFORE the digest gate so a legacy short-role
    entry (``"butler"``) is reported as ``role_mismatch`` against a canonical
    ``"resident:butler"`` lookup and never resumed. For a specialist/executor
    with no binding populated, both stored and looked-up ``binding_digest`` are
    ``""`` — the digest gate degrades to a no-op and resume falls back to
    role-id-only gating, byte-for-byte matching the prior behavior."""
    old = snapshot_session_entry(entry)
    if old is None:
        return ResumeDecision("new", None, False, None, "missing")
    if old.agent != role_id:
        return ResumeDecision("new", None, True, old, "role_mismatch")
    if (old.binding_digest or "") != (binding_digest or ""):
        return ResumeDecision("new", None, True, old, "binding_mismatch")
    try:
        last = (
            datetime.fromisoformat(old.last_active)
            if isinstance(old.last_active, str) else None
        )
    except ValueError:
        last = None
    if last is not None and last.tzinfo is None:
        # A naive timestamp parses, but comparing it against the aware ``now``
        # would raise — treat it as invalid rather than guessing its zone.
        last = None
    if last is None:
        return ResumeDecision("new", None, True, old, "invalid_entry")
    if (now - last) <= freshness_window(channel):
        return ResumeDecision("resume", old.sdk_session_id, False, old, "fresh")
    return ResumeDecision("new", None, True, old, "expired")


@dataclass(frozen=True)
class PluginBindingSnapshot:
    """§3.9/D2 (v0.74.0): ONE immutable publish of this agent's resolved
    plugin state — replaces the two-assignment (resolution, binding) pair
    that verify_plugin_state could tear between (spec D2, agent.py:1010-1011
    pre-fix). ``binding`` is a read-only MappingProxyType; ``generation`` is
    the resolver-snapshot generation the resolution was computed against
    (returned by resolve_for), so verify and the mutation's post-reload
    check can detect an intervening reload instead of grading stale state."""
    resolution: "plugin_registry.ResolutionResult"
    binding: "Mapping[str, str]"
    generation: int


@dataclass(frozen=True)
class _LoadPlan:
    push_overlay: bool   # GET mental-model overlay precedes the turn
    auto_recall: bool    # auto-run a query-specific recall on the opening utterance


def _plan_load(channel: str, *, is_fresh_session: bool) -> _LoadPlan:
    """Spec §4.3 channel-aware load. Overlay is pushed only at fresh-session-start
    (it rides along on resume). Voice never auto-recalls (the multi-strategy + rerank
    recall must not sit on the first-utterance critical path); voice uses the
    recall_memory pull tool instead. Note: even when push_overlay is True the overlay
    is additionally gated by ``_overlay_allowed(channel)`` at the call site — a channel
    without ``private`` clearance (e.g. voice = friends) never actually receives the
    overlay regardless of this plan."""
    if not is_fresh_session:
        return _LoadPlan(push_overlay=False, auto_recall=False)
    if channel == "voice":
        return _LoadPlan(push_overlay=True, auto_recall=False)
    return _LoadPlan(push_overlay=True, auto_recall=True)


def _retain_fence():
    """The process-wide retain fence (#411). Imported lazily so tests that
    stub agent's collaborators need not know the fence exists."""
    from memory_wipe import FENCE
    return FENCE


def _memory_bank() -> str:
    """The single shared long-term bank (design §2.1). Role no longer partitions
    memory — sensitivity tiers do."""
    return bank_id("casa")


# Auto-recall (pre-turn) bound: prompt construction must never sit on the full
# HTTP client timeout (20s) waiting for an overloaded reranker — past this
# deadline the recall is cancelled and the turn runs cold (v0.99.0).
_AUTO_RECALL_TIMEOUT_S = 5.0
_RECALL_BREAKER_THRESHOLD = 3      # consecutive failures before opening
_RECALL_BREAKER_COOLDOWN_S = 60.0  # open duration before a half-open probe


class _RecallBreaker:
    """Circuit breaker for the automatic pre-turn recall (v0.99.0).

    After ``threshold`` CONSECUTIVE failures the breaker opens and auto-recall
    runs cold (skipped entirely) instead of adding a doomed multi-second call —
    and load — to every turn of an already-overloaded backend. After
    ``cooldown_s`` the next turn's recall is allowed through as the recovery
    probe: success closes the breaker, failure re-opens it for a full cooldown.
    The recall_memory pull tool is intentionally NOT gated — an explicit pull
    can still try (and surface status=unavailable to the model)."""

    def __init__(
        self, *, threshold: int = _RECALL_BREAKER_THRESHOLD,
        cooldown_s: float = _RECALL_BREAKER_COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_started_at: float | None = None  # half-open probe in flight

    @property
    def open(self) -> bool:
        return self._opened_at is not None

    def allow(self) -> bool:
        """True when a recall attempt may proceed (closed, or THE half-open
        probe). In half-open state exactly one probe is admitted at a time —
        concurrent turns arriving after cooldown must not all hit the backend
        at once. Calling this in half-open state RESERVES the probe; the
        reservation is released by record_success/record_failure, and expires
        after a cooldown so a turn that died without recording (cancelled
        mid-probe) cannot wedge the breaker open forever. All calls run on
        the one event loop, so no lock is needed."""
        if self._opened_at is None:
            return True
        now = self._clock()
        if now - self._opened_at < self._cooldown_s:
            return False
        if (self._probe_started_at is not None
                and now - self._probe_started_at < self._cooldown_s):
            return False
        self._probe_started_at = now
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probe_started_at = None

    def record_failure(self) -> None:
        self._probe_started_at = None
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = self._clock()  # re-stamps on a failed half-open probe


def _current_origin_clearance(channel: str) -> str:
    """Read-clearance for the CURRENT turn, keyed off the unspoofable origin
    marker in ``origin_var`` (Release A Layer 2) rather than the channel string,
    so a webhook_trigger turn reads public and only an explicit ``invoke`` reads
    private on the webhook channel. Falls through to channel clearance when no
    origin marker is present (non-webhook surfaces unchanged)."""
    origin = origin_var.get(None) or {}
    return clearance_for_origin(
        origin.get("_origin_route"), origin.get("_origin_clearance"), channel,
    )


def _recall_tier_tags(channel: str) -> list[str]:
    """Tiers a turn may recall = readable_tiers(clearance). The sole read-side
    access gate (design §2.3), origin-keyed (Layer 2)."""
    return readable_tiers(_current_origin_clearance(channel))


def _overlay_allowed(channel: str) -> bool:
    """The bank-level mental-model overlay cannot be tier-filtered, so it is pushed
    ONLY at ``private`` clearance — a context that may already see everything
    (design §2.3). At any lower clearance it would leak across tiers."""
    return _current_origin_clearance(channel) == "private"


# Release A / Layer 1 — the two casa-framework tools an untrusted webhook turn
# may use: public-clearance recall + operator-bound notify. Everything else
# (Bash, filesystem, network, delegation, plugin/config mutation) is denied.
_RESTRICTED_WEBHOOK_TOOLS = (
    "mcp__casa-framework__recall_memory",
    "mcp__casa-framework__send_message",
)
# Defense-in-depth: Agent/Task can bypass allowed_tools (they route to
# sub-agents), so name them in disallowed_tools; Bash is belt-and-braces on top
# of tools=[].
_RESTRICTED_DISALLOWED_TOOLS = ("Bash", "Task", "Agent")


def build_restricted_webhook_options(
    *,
    model: str,
    role: str,
    system_prompt: str,
    max_turns: int,
    agent_home: str,
    resume_sid: str | None,
) -> ClaudeAgentOptions:
    """Build the LOCKED-DOWN options for an UNTRUSTED webhook turn (spec A4 /
    Release A Layer 1 — the primary containment boundary; Sol+Terra design r5).

    Third-party webhook content runs with NO plugins, NO external/managed hooks
    (``settings={"disableAllHooks": true}``; ``setting_sources=[]``), NO built-in
    tools (``tools=[]`` — ``allowed_tools`` alone is only auto-approval and would
    leave Bash reachable), NO skills, strict MCP config (no ambient ``.mcp.json``),
    ``permission_mode="dontAsk"``, and exactly two casa-framework tools. ``Agent``/
    ``Task`` are additionally disallowed (they bypass ``allowed_tools``). This
    closes the Bash→(unauthenticated)Hindsight, Bash→transcript-file, and
    pre-tool-decision-hook bypasses that application-level memory gates cannot.
    """
    from tools import create_casa_tools
    casa_server = create_casa_tools(frozenset(_RESTRICTED_WEBHOOK_TOOLS))
    return ClaudeAgentOptions(
        model=model,
        cli_path=CLAUDE_CLI_PATH,
        system_prompt=system_prompt,
        allowed_tools=list(_RESTRICTED_WEBHOOK_TOOLS),
        disallowed_tools=list(_RESTRICTED_DISALLOWED_TOOLS),
        permission_mode="dontAsk",
        max_turns=max_turns,
        mcp_servers={"casa-framework": casa_server},
        hooks={},
        cwd=agent_home,
        resume=resume_sid,
        setting_sources=[],
        skills=[],
        tools=[],
        strict_mcp_config=True,
        plugins=[],
        settings=json.dumps({"disableAllHooks": True}),
        can_use_tool=make_fail_closed_can_use_tool(role),
        include_partial_messages=False,
    )


class Agent:
    """A Casa agent backed by the Claude Agent SDK."""

    def __init__(
        self,
        config: AgentConfig,
        session_registry: SessionRegistry,
        mcp_registry: McpServerRegistry,
        channel_manager: ChannelManager,
        agent_registry=None,
        semantic_memory: SemanticMemory | None = None,
    ) -> None:
        self.config = config
        self._semantic_memory: SemanticMemory = semantic_memory or NoOpSemanticMemory()
        self._session_registry = session_registry
        self._mcp_registry = mcp_registry
        self._channel_manager = channel_manager
        self._agent_registry = agent_registry
        self._bg_tasks: set[asyncio.Task] = set()  # background cold-session retains
        # Per-(session_id) over-budget streak tracker (spec 5.2 §5.2).
        # Per-instance so assistant (4000) and butler (800) budgets stay
        # isolated even when the same channel serves both roles.
        self._budget_tracker = BudgetTracker()
        # Auto-recall circuit breaker (v0.99.0): per-instance — the backend is
        # shared, but auto-recall fires only on this agent's fresh text turns,
        # so per-instance state converges on backend health within one turn each.
        self._recall_breaker = _RecallBreaker()
        # Unified plugin architecture (§3.3/§3.9): per-instance ONE-shot
        # snapshot of the registry resolution for this agent's tier:role
        # (resolution + {name: artifact_id} binding + resolver generation),
        # published by a SINGLE assignment in _get_plugin_resolution so a
        # concurrent verify can never observe a resolved agent with a
        # stale/empty binding (D2, v0.74.0). resolve_for reads the process
        # snapshot (refreshed by casa_reload BEFORE agent reconstruction),
        # so a fresh Agent (reload._construct_agent) always rebuilds this —
        # the cache can never surface a stale plugin set. The lock guards
        # concurrent turns from racing the first (off-loop) resolve.
        self._plugin_snapshot: PluginBindingSnapshot | None = None
        self._plugin_resolution_lock = asyncio.Lock()
        # Resolve hooks once at construction. HooksConfig.pre_tool_use
        # empty → default policy bundle (block_dangerous_bash + path_scope
        # scoped to cfg.cwd).
        self._resolved_hooks = resolve_hooks(
            config.hooks, default_cwd=config.cwd,
        )

        # Warm SDK-client pool (spec 2026-07-11, AR-1..AR-10). One warm
        # conversation per channel_key, reconciled against the SessionRegistry
        # (the registry stays authoritative — the pool derives the resume
        # decision from a fresh read INSIDE turn() via ``_resume_decision``).
        # ``engagement_var`` is imported LAZILY here to avoid the tools↔agent
        # circular import (tools imports agent only inside functions); resident
        # turns never run inside an engagement binding, so open() asserts it is
        # None. The reset listener gives the pool a chance to close (flush) a
        # key's warm subprocess when the registry is explicitly reset (AR-4).
        from tools import engagement_var as _engagement_var
        self._engagement_var = _engagement_var
        self._pool = SdkClientPool(
            session_registry,
            # A2: bind THIS agent's role into the resume decision — a role
            # mismatch (another resident's entry under the same channel_key,
            # impossible post-A2 collision-safety, but defense-in-depth) never
            # resumes.
            # #411: fence_generation is captured INSIDE the lambda — the pool
            # invokes decide synchronously in its decision block (AR-3), so
            # the capture lands in the same no-await block as the registry
            # read, per the fence's capture-point contract.
            decide=lambda ch, entry, now: dataclasses.replace(
                _resume_decision(
                    ch, entry, now,
                    role_id=self.config.role_id,
                    binding_digest=self.config.binding_digest,
                ),
                fence_generation=_retain_fence().generation(),
            ),
            origin_ctxvar=origin_var, cid_ctxvar=cid_var,
            engagement_ctxvar=_engagement_var,
        )
        self._unsub_reset = session_registry.add_reset_listener(
            self._pool.close_key,
        )

        # Layer-5 capability boot log: one INFO line per Agent construction
        # (boot AND reload — reload._construct_agent builds a fresh Agent), so
        # a capability regression (a tool grant vanishing after a config_sync
        # reconcile, an MCP server going undeclared) is visible in `docker
        # logs` and diffable across deploys. Logs the CONFIGURED surface
        # (config.tools.allowed) — the thing that drifts vs runtime.yaml.
        # Skills are enabled via skills="all", not an allowed_tools entry
        # ((f) v0.69.9), so they don't appear here.
        # Best-effort: an observability line must never break construction.
        try:
            allowed = list(getattr(config.tools, "allowed", []) or [])
            logger.info(
                "agent_capabilities role=%s model=%s enabled=%s tool_count=%d "
                "tools=%s mcp_servers=%s",
                config.role, getattr(config, "model", "?"),
                getattr(config, "enabled", "?"),
                len(allowed), sorted(allowed),
                sorted(getattr(config, "mcp_server_names", []) or []),
            )
        except Exception:  # noqa: BLE001 — never let the boot log break boot
            logger.warning("agent_capabilities log failed for role=%s",
                           getattr(config, "role", "?"), exc_info=True)

    # ------------------------------------------------------------------
    # Public entry point (used as bus handler)
    # ------------------------------------------------------------------

    async def handle_message(self, msg: BusMessage) -> BusMessage | None:
        """Process an inbound message and return a response BusMessage.

        If the channel supports streaming, tokens are delivered
        incrementally via ``on_token``.  The full response is sent or
        finalized after the SDK completes.
        """
        # Phase 3.1: late-completion delegation NOTIFICATION — synthesize
        # a fresh turn so the delegating resident narrates the result
        # back to the user on the origin channel.
        if (
            msg.type == MessageType.NOTIFICATION
            and isinstance(msg.content, DelegationComplete)
        ):
            msg = self._synthesize_delegation_turn(msg)

        # Obtain a streaming callback from the channel (if available).
        # SCHEDULED turns are buffered: the agent thinks privately and
        # only the final text is sent. This prevents Telegram leaking
        # acknowledgement-style first tokens into the chat before the
        # prompt's silence check completes (spec 2026-04-28 §3.2 B.1).
        # #534: event wakes dispatch as CHANNEL_IN, so without the same
        # buffering their narration streams to the operator chat before
        # the sentinel gate below can suppress it. The `synthetic` marker
        # is ingress-unforgeable (INV-EV-006) — external input can never
        # opt itself out of streaming.
        on_token: OnTokenCallback | None = None
        channel = self._channel_manager.get(msg.channel) if msg.channel else None

        if (
            channel is not None
            and hasattr(channel, "create_on_token")
            and msg.type != MessageType.SCHEDULED
            and (msg.context or {}).get("synthetic") != "event_wake"
        ):
            on_token = channel.create_on_token(msg.context)

        error_kind: ErrorKind | None = None
        try:
            text = await self._process(msg, on_token=on_token)
        except Exception as exc:
            error_kind = _classify_error(exc)
            logger.error(
                "Agent '%s' error [%s]: %s",
                self.config.character.name,
                error_kind.value,
                exc,
                exc_info=(error_kind == ErrorKind.UNKNOWN),
            )
            text = _USER_MESSAGES[error_kind]

        # Deliver the response via the channel.
        # For voice (or any channel that supplies emit_error_line), prefer
        # the persona-voice error pipeline on error paths. Otherwise fall
        # through to the existing text-based finalize_stream / send flow.
        if error_kind is not None and channel is not None \
                and hasattr(channel, "emit_error_line"):
            try:
                handled = await channel.emit_error_line(
                    error_kind.value, msg.context, self.config,
                )
            except Exception:
                logger.exception("emit_error_line raised; falling back to text")
                handled = False
            if handled:
                text = ""  # suppress normal text delivery below

        # Silence sentinel suppression — applies to ALL message types.
        #
        # Origin contract (spec 2026-04-28 §3.2 B.2): the agent's tokens
        # for SCHEDULED turns are buffered by Fix B.1, so the prompt can
        # signal "do not send" via the literal sentinel `<silent/>` or
        # by producing only whitespace. Strict, exact-match-after-strip
        # — substring matches are rejected by design (prompt produces
        # `<silent/>` followed by recanting text → send the whole thing).
        #
        # G-3 (v0.33.0, exploration2): the suppression was originally
        # scoped to `msg.type == MessageType.SCHEDULED`. Ellen's outer
        # USER-driven turn after a configurator engagement (cid
        # `dcc3c30b` 2026-05-01) accidentally absorbed the heartbeat
        # trigger's `<silent/>` doctrine via mid-engagement Read of
        # triggers.yaml and then emitted the bare sentinel as her own
        # noop on the user DM, where this gate didn't fire. Lifting the
        # SCHEDULED-only condition makes the behavior consistent: any
        # turn whose entire output strips to `<silent/>` (or to
        # whitespace) is a no-op, regardless of channel-trigger source.
        # The cost of false suppression is zero in practice (no model
        # legitimately emits the literal sentinel string to a user); the
        # cost of operator-visible literal `<silent/>` is real-but-small.
        if text:
            stripped = text.strip()
            # A turn is silence when it strips to nothing, or to nothing but
            # one-or-more `<silent/>` sentinels and surrounding whitespace
            # (so a buffered model that emits the sentinel on its own line, or
            # repeats it, is still suppressed). Residual PROSE after a sentinel
            # is still sent — the G-3 recant contract: a model that emits
            # `<silent/>` and then a real message must not have it swallowed.
            if not stripped or not stripped.replace("<silent/>", "").strip():
                text = ""

        # §3.10 notice: while plugin-health holds a blocking issue affecting
        # this agent's role, prepend a one-line notice to a user-visible reply.
        # Suppression of repeats lives entirely in plugin_health.pending_notice
        # (#551) — see _maybe_prepend_health_notice for why it cannot live here.
        # A channel that never delivers its final text out-of-band (voice:
        # `send()` is a documented no-op; the reply reaches the user only via
        # the token stream, which has already flushed) must not receive the
        # notice — and must not consume its cooldown — so it is not consulted
        # at all; a channel that can actually show it renders it (Sol/Terra
        # diff r1).
        if (
            text and channel is not None
            and getattr(channel, "delivers_final_text", True)
        ):
            text, health_notice = await self._maybe_prepend_health_notice(text)
        else:
            health_notice = None

        if text and channel is not None:
            # Rich-text (v0.70.0) renders only genuine agent responses
            # (error_kind is None). Error/system text stays plain via the
            # original finalize_stream/send paths.
            outcome = None
            try:
                if on_token is not None and hasattr(channel, "finalize_stream"):
                    if error_kind is None and hasattr(
                        channel, "finalize_response_stream",
                    ):
                        outcome = await channel.finalize_response_stream(
                            text, msg.context, on_token,
                        )
                    else:
                        outcome = await channel.finalize_stream(
                            text, msg.context, on_token,
                        )
                elif error_kind is None and hasattr(channel, "send_response"):
                    outcome = await channel.send_response(text, msg.context)
                else:
                    outcome = await channel.send(text, msg.context)
            except BaseException:
                # #349, preserved through #551's rework and NARROWED by #556: a
                # delivery that raised having shown NOTHING releases the
                # cooldown, so the notice is offered again on the very next
                # turn. A delivery whose HEAD landed and whose tail then raised
                # has already displayed the notice — releasing there would nag
                # the operator about something they just read. The stamp is the
                # only carrier of that fact, because a raising call returns no
                # outcome at all.
                if (health_notice is not None
                        and not msg.context.get("_delivery_head_sent")):
                    import plugin_health
                    plugin_health.forget_notice(
                        self.config.role, health_notice)
                raise
            # #556: a NORMAL return is not a reliable positive either — every
            # delivery method returns normally when the PTB app is absent,
            # having made zero Bot API calls. Only a proven negative releases;
            # UNKNOWN (a channel not on the contract, returning None) keeps
            # today's behavior, explicitly rather than by omission.
            if (health_notice is not None
                    and outcome is DeliveryOutcome.NOT_DELIVERED):
                import plugin_health
                plugin_health.forget_notice(self.config.role, health_notice)
        elif channel is not None and hasattr(channel, "turn_finished"):
            # L7 (v0.52.0): a turn that strips to empty / `<silent/>` never
            # calls send()/finalize_stream(), so give the channel a chance to
            # tear down per-turn state (e.g. the Telegram typing indicator).
            # hasattr-guarded so channels without the hook are unaffected;
            # teardown must never break the turn.
            try:
                await channel.turn_finished(msg.context)
            except Exception:  # noqa: BLE001
                logger.exception("channel.turn_finished failed")

        if not text and error_kind is None and msg.type != MessageType.REQUEST:
            return None
        # M4 (v0.53.0): REQUEST turns must ALWAYS return a RESPONSE (possibly
        # empty-content). Voice SSE/WS (channels/voice/channel.py) and /invoke
        # (casa_core.py) block on bus.request(timeout=300); a None return here
        # would leave their pending future unresolved for the full window.
        # Channel delivery of the empty text was already suppressed above
        # (send()/finalize_stream() are skipped, turn_finished() torn down).
        # Note: the delegation-synthesis path rebinds ``msg`` to REQUEST, so an
        # empty delegation turn now returns an empty RESPONSE too — but
        # bus._dispatch keys off the ORIGINAL message (a NOTIFICATION with no
        # pending future), so that RESPONSE is simply ignored.

        return BusMessage(
            type=MessageType.RESPONSE,
            source=self.config.role,
            target=msg.source,
            content=text or "",
            reply_to=msg.id,
            channel=msg.channel,
            context=msg.context,
        )

    def _synthesize_delegation_turn(self, msg: BusMessage) -> BusMessage:
        """Convert a NOTIFICATION+DelegationComplete into a REQUEST turn
        whose content is a synth prompt for the delegating resident."""
        complete = msg.content
        assert isinstance(complete, DelegationComplete)
        short_id = complete.delegation_id[:8]

        origin = complete.origin or {}
        user_text = origin.get("user_text", "")

        if complete.status == "ok":
            body = (
                f"[System notification: your delegation to {complete.agent} "
                f"(id {short_id}) has returned with status=ok]\n\n"
                f"Result text from {complete.agent}:\n{complete.text}\n"
            )
        elif complete.kind == "restart_orphan":
            body = (
                f"[System notification: your delegation to {complete.agent} "
                f"(id {short_id}) was orphaned by a Casa restart]\n\n"
                "I lost track of this delegation during a Casa restart. "
                "Tell the user and offer to retry.\n"
            )
        else:
            body = (
                f"[System notification: your delegation to {complete.agent} "
                f"(id {short_id}) has returned with status=error]\n\n"
                f"Delegation failed ({complete.kind or 'unknown'}): "
                f"{complete.message}\n"
            )
        body += (
            f"\nThe original user question was: {user_text}\n\n"
            "Reply to the user via their original channel. Be concise.\n"
        )

        # Release A Layer 3: the notification's context carries only cid/chat_id,
        # so copy the trusted origin markers from the PERSISTED completion origin
        # into the synthesized turn — otherwise a delegating /invoke would
        # fail-close to public/restricted on resume. These keys are reserved
        # (server-set), so the persisted origin value is the trustworthy one.
        # #485: `_scheduled_delivery` joins them. A scheduled turn that
        # delegates comes back through here, and the completion context carries
        # only cid/chat_id — so without this the resumed resident turn could no
        # longer deliver what its specialist had just produced, which is the
        # motivating case (a specialist builds the invoice PDF, the resident
        # sends it). The completion origin is the persisted, server-built one.
        synth_context = dict(msg.context)
        for _marker_key in (
            "_origin_route", "_origin_clearance", "_scheduled_delivery",
            # #573: the epoch travels with the marker for the same reason the
            # marker travels — a completion resumed into a scheduled session
            # must be judged against the trigger set that ASKED for the work.
            "_scheduled_epoch",
        ):
            if _marker_key in origin:
                synth_context[_marker_key] = origin[_marker_key]

        return BusMessage(
            type=MessageType.REQUEST,
            source=msg.source,
            target=msg.target,
            content=body,
            channel=msg.channel,
            context=synth_context,
        )

    # ------------------------------------------------------------------
    # Internal processing pipeline
    # ------------------------------------------------------------------

    async def _process(
        self,
        msg: BusMessage,
        on_token: OnTokenCallback | None = None,
    ) -> str | None:
        channel_key = build_scoped_session_key(
            msg.channel,
            self.config.role,
            msg.context.get("chat_id"),
        )
        user_text = str(msg.content)

        # The origin snapshot is set into ``origin_var`` for this task (so the
        # delegate_to_agent tool handler can read the outer turn) AND handed to
        # the pool / bypass client as ``origin=`` (the warm client rewrites its
        # read-task-visible holder from it per turn — spec Q7). Same content on
        # both seams.
        origin_snapshot = {
            "role": self.config.role,
            "channel": msg.channel,
            "chat_id": msg.context.get("chat_id", ""),
            # Bug 8 (v0.14.6): propagated through engagement.origin so the
            # Telegram channel can verify the user that issues /cancel or
            # /complete in an engagement topic actually owns the engagement.
            "user_id": msg.context.get("user_id"),
            "cid": cid_var.get(),
            "user_text": user_text,
            # Provenance foundation (A:§1, v0.76.0): message_type/source
            # let turn_provenance() classify transport (dm vs button vs
            # other); execution_role starts equal to `role` here (a direct
            # turn) and is overwritten by _run_delegated_agent's
            # child_origin for delegated turns so the two can be compared.
            "message_type": msg.type.value,
            "source": msg.source,
            "execution_role": self.config.role,
            # Provenance foundation (Task 9/10): the real identity of the agent
            # whose turn this is — a live SpeakerProvenance dataclass (or None
            # for a config not yet bound to a persona: an executor, or a Plan-1
            # specialist before Plan 2's N1 wires its binding). This is what lets
            # a delegated turn attribute its CALLER correctly instead of having
            # no provenance to read at all. Stored as a LIVE object (never a
            # mapping) because origin_var is an in-process ContextVar — exactly
            # like the _voice_handoff_reservation object carried below. It is
            # NEVER persisted: engagement tombstones strip it (see
            # engagement_registry._persistable_origin).
            "speaker_provenance": self.config.speaker_provenance,
        }
        # Reserved provenance markers (synthetic turn replay, button
        # answers) ride on msg.context when a LATER task (ask_user/button
        # broker) sets them; copy through only if actually present so a
        # normal turn's origin stays free of stray None-valued keys.
        # Release A: ``_origin_route``/``_origin_clearance`` are stamped
        # server-side at ingress (build_invoke_message / the /webhook/{name}
        # dispatch) and are RESERVED (stripped from external context), so a
        # copy here carries only the trustworthy server value into
        # ``origin_var`` — where _build_options (restricted-runtime gate),
        # the recall clearance gate, and delegation synthesis read it.
        for _marker_key in COPIED_CONTEXT_MARKERS:
            if _marker_key in msg.context:
                origin_snapshot[_marker_key] = msg.context[_marker_key]
        # A4: voice turn budget + progress sink. Set by the SSE/WS handler
        # on msg.context at ingress (channels/voice/channel.py); propagated
        # into origin ONLY for the voice channel so delegate_to_agent's
        # _prelaunch/sync-wait logic can read them off the trusted origin
        # (which survives into the delegated-turn snapshot) rather than
        # off msg.context directly.
        if msg.channel == "voice":
            if "_voice_deadline" in msg.context:
                origin_snapshot["voice_deadline"] = msg.context["_voice_deadline"]
            if "_progress_sink" in msg.context:
                origin_snapshot["_progress_sink"] = msg.context["_progress_sink"]
            transport = msg.context.get("_voice_transport")
            if transport in {"sse", "ws"}:
                origin_snapshot["voice_transport"] = transport
            route_id = msg.context.get("_voice_route_id")
            if isinstance(route_id, str) and route_id.strip():
                origin_snapshot["voice_route_id"] = route_id.strip()
            capabilities = msg.context.get("_voice_route_capabilities")
            if isinstance(capabilities, (set, frozenset, list, tuple)):
                normalized_capabilities = frozenset(
                    item for item in capabilities
                    if isinstance(item, str) and item
                )
                if normalized_capabilities:
                    origin_snapshot["voice_route_capabilities"] = (
                        normalized_capabilities
                    )
            device_id = msg.context.get("_origin_device_id")
            if isinstance(device_id, str) and device_id.strip():
                origin_snapshot["origin_device_id"] = device_id.strip()
            control_id = msg.context.get("_voice_job_control_id")
            if isinstance(control_id, str) and control_id.strip():
                origin_snapshot["voice_job_control_id"] = control_id.strip()
            # The voice channel installs this private foreground-output
            # reservation after sanitizing external context. Keep only the
            # duck-typed capability object, never a caller-supplied shape.
            offer = msg.context.get("_voice_delivery_offer")
            if isinstance(offer, dict) and offer.get("modality") in (
                    "audio", "text"):
                origin_snapshot["voice_delivery_offer"] = dict(offer)
            reservation = msg.context.get("_voice_handoff_reservation")
            if (callable(getattr(reservation, "reserve", None))
                    and callable(getattr(reservation, "release", None))
                    and callable(getattr(reservation, "commit", None))):
                origin_snapshot["_voice_handoff_reservation"] = reservation
        origin_token = origin_var.set(origin_snapshot)
        # Task 14 / #199: per-turn explanation scaffolding. ``explain_draft`` is
        # populated by ``_build_options`` (projection + recall attributions);
        # ``turn_state`` captures the on_message ``state`` dict of whichever
        # attempt produced the response (its ``tool_names_by_id`` are the turn's
        # tool calls). Both are read once at end-of-turn to record the
        # explanation — a store failure there never touches the user's reply.
        explain_draft: dict[str, Any] = {}
        explain_token = _explain_draft_var.set(explain_draft)
        turn_state: dict[str, Any] = {}
        try:
            # Resolve cwd to the agent-home (Plan 4b §5.1). Residents live at
            # /config/agent-home/<role>/; configured cwd on
            # Config stays as an override for legacy tests.
            agent_home = (
                self.config.cwd
                or f"/config/agent-home/{self.config.role}"
            )

            # Task 9: build this turn's persisted user identity ONCE, from the
            # server-created typed ingress field (never free-text
            # origin/context), so the pooled ``_publish``, the bypass path, and
            # the final ``register()`` all persist the SAME identity. A turn
            # with no trusted ingress (scheduled heartbeat, webhook trigger,
            # delegation-completion synthesis, internal/test turn) has no human
            # author — it is recorded with the honest unattributed ``system``
            # identity, never a fabricated user.
            trusted = msg.trusted_user_origin
            if trusted is not None:
                user_provenance = UserProvenance.from_origin(
                    surface=trusted.surface,
                    server_origin=trusted.server_origin,
                    authenticated_user=trusted.authenticated_user,
                    user_peer=trusted.user_peer,
                )
            else:
                user_provenance = SpeakerProvenance(speaker_kind="system")

            # <current_time> rides on the per-turn query text (NOT the cached
            # system prompt) so the agent still knows the wall-clock time to
            # second precision without busting prompt caching (M27). user_text
            # itself stays raw (it also feeds origin_var + the recall query).
            # Composed via timekeeping so retention's strip_time_envelope
            # (#471) can never drift from what is actually prepended here.
            prompt_text = (
                compose_time_envelope(datetime.now(resolve_tz())) + user_text
            )

            # Eligibility gate (spec §4, AR-6/AR-7): a pooled warm turn iff the
            # pool is enabled, the turn is not a SCHEDULED heartbeat, and it is
            # not a webhook one-shot (random-uuid chat_id). Ineligible turns —
            # and any PoolUnavailable — fall to the per-turn bypass path, which
            # reproduces today's semantics exactly (decision here → one-shot
            # ManagedSdkClient → aclose in finally).
            # A2: computed once and reused both for pool eligibility AND the
            # persisted registry scope_class (a v2 key's hashed remainder is
            # never uuid-shaped, so session_sweeper can no longer re-derive
            # this from the key — it reads scope_class off the entry instead).
            is_webhook_oneshot = (
                msg.channel == "webhook"
                and _is_uuid_scope(str(msg.context.get("chat_id", "")))
            )
            # v0.125.0 (#228): pooling is unconditional. The `sdk_client_pool`
            # kill-switch was the v0.66.0 rollback path; that insurance period
            # is long over and the pool has been the only exercised path since.
            use_pool = (
                msg.type != MessageType.SCHEDULED              # AR-6
                and not is_webhook_oneshot
            )

            # The pool decides the resume sid internally (AR-3), but the
            # ProcessError fallback below needs to know whether the failed
            # attempt was resuming. Both attempt closures record it here.
            # #526: "gen" carries the registration generation captured with
            # the SAME registry snapshot the resume decision read, so the
            # ProcessError recovery below can decline against a concurrent
            # turn that re-registered the SAME sid (which the sid guard alone
            # cannot see).
            last_resume: dict[str, str | int | None] = {"sid": None, "gen": None}

            async def _attempt_pooled_turn():
                session_published = False
                turn_guard = (
                    VoiceTurnGuard.ha_direct()
                    if msg.channel == "voice"
                    and self.config.tools.voice_guard == "ha_direct"
                    else None
                )
                on_message, state = self._make_on_message(
                    on_token, turn_guard,
                )
                turn_state["state"] = state
                # #521: keep EVERY attempt's state — a setup tool can run in
                # an attempt that then fails retryably, and the outcome
                # report must aggregate evidence across attempts.
                turn_state.setdefault("states", []).append(state)

                async def _build(is_fresh, resume_sid):
                    # Recorded HERE too (not just via on_decision below) so a
                    # ProcessError raised at connect — the resume-failure
                    # class — still tells the fallback below which sid was in
                    # play. Harmless double-set: on_decision already recorded
                    # the same resume_sid a moment earlier, under the entry
                    # lock, before this cold-connect branch even runs.
                    last_resume["sid"] = resume_sid
                    return await self._build_options(
                        channel=msg.channel, channel_key=channel_key,
                        is_fresh=is_fresh, resume_sid=resume_sid,
                        user_text=user_text,
                    )

                async def _publish(sid):
                    nonlocal session_published
                    await self._session_registry.register(
                        channel_key=channel_key,
                        agent=self.config.role_id,
                        sdk_session_id=sid,
                        scope_class=(
                            "webhook_oneshot" if is_webhook_oneshot else None
                        ),
                        binding_digest=self.config.binding_digest,
                        speaker_provenance=speaker_provenance_for_role(self.config),
                        user_provenance=user_provenance,
                    )
                    session_published = True

                result = await self._pool.turn(
                    channel_key=channel_key, channel=msg.channel,
                    prompt=prompt_text, origin=origin_snapshot,
                    cid=cid_var.get(), build_options=_build,
                    binding_digest=self.config.binding_digest,
                    on_stale_old=lambda old, fence_gen=None: self._spawn_cold_retain(
                        old, directory=agent_home, channel=msg.channel,
                        fence_generation=fence_gen,
                    ),
                    on_message=on_message,
                    on_success=_publish,
                    # Finding 2 (final-review): fires for EVERY turn (warm
                    # reuse included), unlike _build above which the pool
                    # skips on warm reuse — so a non-retryable failure on a
                    # warm-reuse turn still leaves last_resume["sid"]
                    # populated for the ProcessError fallback below.
                    on_decision=lambda resume_sid, is_fresh, generation: (
                        last_resume.update(sid=resume_sid, gen=generation)
                    ),
                )
                last_resume["sid"] = result.resume_sid
                return (
                    state["text"], result.sid, state["usage"],
                    result.resume_sid, session_published,
                )

            async def _attempt_bypass_turn():
                # Per-turn path (today's semantics): decision here, one-shot
                # ManagedSdkClient reusing the same turn body.
                turn_guard = (
                    VoiceTurnGuard.ha_direct()
                    if msg.channel == "voice"
                    and self.config.tools.voice_guard == "ha_direct"
                    else None
                )
                existing = self._session_registry.get(channel_key)
                # #526: same no-await block as the entry snapshot (see the
                # pooled path's on_decision). #290/#411: the retirement check
                # and the fence-generation capture share that block too — the
                # bypass path is a full resume-decision site and gets the
                # same steer the pool applies, so no path can resume a
                # session a reset/wipe is retiring.
                existing_generation = self._session_registry.generation(
                    channel_key,
                )
                _retiring = getattr(
                    self._session_registry, "retirement_pending",
                    lambda _k: False,
                )(channel_key)
                _fence_gen = _retain_fence().generation()
                decision = _resume_decision(
                    msg.channel, existing, datetime.now(timezone.utc),
                    role_id=self.config.role_id,
                    binding_digest=self.config.binding_digest,
                )
                if _retiring:
                    decision = dataclasses.replace(
                        decision, action="new", resume_sid=None,
                        retain_old=False, old=None, reason="retiring",
                    )
                resume_sid = (
                    decision.resume_sid if decision.action == "resume" else None
                )
                last_resume.update(sid=resume_sid, gen=existing_generation)
                if decision.action == "resume":
                    await self._session_registry.touch(channel_key)
                elif decision.retain_old and decision.old is not None:
                    # next-turn-after-gap: register() below overwrites this
                    # channel's pointer, so retain the OLD immutable snapshot in
                    # the BACKGROUND (claim-free / registry-decoupled — cannot
                    # race register(); per-item classification runs off the hot
                    # path, tier §2.4).
                    self._spawn_cold_retain(
                        decision.old, directory=agent_home, channel=msg.channel,
                        fence_generation=_fence_gen,
                    )
                # else ("new", retain_old=False): no prior entry → nothing to save
                options = await self._build_options(
                    channel=msg.channel, channel_key=channel_key,
                    is_fresh=resume_sid is None, resume_sid=resume_sid,
                    user_text=user_text,
                )
                client = ManagedSdkClient(
                    options, origin_ctxvar=origin_var,
                    cid_ctxvar=cid_var, engagement_ctxvar=self._engagement_var,
                )
                on_message, state = self._make_on_message(
                    on_token, turn_guard,
                )
                turn_state["state"] = state
                turn_state.setdefault("states", []).append(state)
                try:
                    await client.open()
                    async with client.lock:
                        sid = await client.run_turn_locked(
                            prompt_text, origin=origin_snapshot,
                            cid=cid_var.get(), on_message=on_message,
                        )
                finally:
                    await client.aclose()
                return state["text"], sid, state["usage"], resume_sid, False

            # Retry transient faults (spec 5.2 §3). The pooled path may raise
            # PoolUnavailable (pool closing / entry unstable) — fall to the
            # per-turn bypass. ProcessError on a resuming attempt = the stale
            # resume class (spec 5.8): clear + retry fresh (the pool re-derives
            # a FRESH decision from the cleared registry).
            # #537: the recovery lives in a helper wrapping EACH attempt path,
            # not in a sibling except arm — a sibling arm never catches an
            # exception raised inside the PoolUnavailable handler's suite, so
            # the bypass fallback's stale-resume ProcessError used to surface
            # raw AND leave the stale sid registered. Both call sites now
            # recover identically; depth stays bounded (one clear per helper
            # call, two helper calls max).
            async def _attempt_with_stale_recovery(attempt_fn):
                try:
                    return await retry_sdk_call(
                        attempt_fn, on_retry=self._log_retry,
                    )
                except ProcessError as exc:
                    if last_resume["sid"] is None:
                        raise
                    # Phase 4b Bug 5: structured retry telemetry. exc.stderr
                    # is populated by Bug 4's stderr callback; truncate to a
                    # 200-char tail with newlines escaped so one log line
                    # stays scannable.
                    stderr_tail = (
                        (exc.stderr or "")[-200:].replace("\n", "\\n")
                    )
                    logger.info(
                        "sdk_retry_fresh channel_key=%s exit_code=%s "
                        "prior_sid=%s stderr_tail=%s",
                        channel_key, exc.exit_code, last_resume["sid"],
                        stderr_tail,
                    )
                    logger.warning(
                        "SDK resume failed (key=%s sid=%s); clearing and "
                        "retrying fresh", channel_key, last_resume["sid"],
                    )
                    # #349: clear only while the entry still carries the sid
                    # that FAILED — this task released its per-key lock before
                    # this arm runs, so a concurrent same-key turn may have
                    # registered a valid newer session, and the retry below
                    # should resume THAT (the pool/bypass re-derive the
                    # decision from the registry).
                    # #526: the generation guard closes the #349 residual — a
                    # concurrent turn that re-registered the SAME sid moved
                    # the generation past this attempt's snapshot, so the
                    # clear declines and that session's continuity and
                    # save-time consolidation both survive.
                    await self._session_registry.clear_sdk_session(
                        channel_key, expected_sid=last_resume["sid"],
                        expected_generation=last_resume["gen"],
                    )
                    return await retry_sdk_call(
                        attempt_fn, on_retry=self._log_retry,
                    )

            attempt = _attempt_pooled_turn if use_pool else _attempt_bypass_turn
            # #573: a turn that can touch a SCHEDULED session runs under the
            # per-session write gate, and holds it across BOTH the attempt (with
            # its stale-resume recovery and PoolUnavailable fallback) and the
            # registry publish below — the bypass path publishes its new sid
            # after the attempt returns, so a gate released at the end of the
            # attempt would still let the next turn resume the predecessor.
            _gate = (
                session_write_gate(channel_key) if _needs_session_gate(msg)
                else nullcontext()
            )
            async with _gate:
                try:
                    response_text, sdk_session_id, usage, used_resume, \
                        session_published = \
                        await _attempt_with_stale_recovery(attempt)
                except PoolUnavailable:
                    # Reachable from the primary attempt OR from the recovery
                    # retry after a clear (the clear is idempotent and guarded,
                    # so falling through to the bypass — which re-derives its
                    # resume decision from the registry — is correct either way).
                    response_text, sdk_session_id, usage, used_resume, \
                        session_published = \
                        await _attempt_with_stale_recovery(_attempt_bypass_turn)

                # 9. SessionRegistry — record the SDK session id for resume +
                # save. Inside the gate; see the note above.
                if sdk_session_id and not session_published:
                    await self._session_registry.register(
                        channel_key=channel_key,
                        agent=self.config.role_id,
                        sdk_session_id=sdk_session_id,
                        scope_class=(
                            "webhook_oneshot" if is_webhook_oneshot else None),
                        binding_digest=self.config.binding_digest,
                        speaker_provenance=speaker_provenance_for_role(self.config),
                        user_provenance=user_provenance,
                    )

            # Per-turn telemetry (spec 5.2 §5.2). Microsecond cost — string
            # format + one logger.info — and runs after streaming has
            # already flushed via on_token, so it is not on the voice
            # critical path. Task 14: also carries binding provenance (when
            # this role is persona-bound) so a `turn_tokens` line can be tied
            # back to the exact role/binding/dependency set via `casactl
            # explain`/`persona diff` — omitted (None/empty) for roles with
            # no binding, which keeps the line byte-identical to pre-Task-14.
            _binding = self.config.binding
            logger.info(
                format_turn_summary(
                    self.config.role,
                    msg.channel or "-",
                    usage,
                    role_checksum=self.config.role_checksum or None,
                    binding_digest=self.config.binding_digest or None,
                    dependency_digests=(
                        _binding.dependency_digests if _binding is not None else ()
                    ),
                    effective_config_digest=(
                        _binding.effective_config_digest if _binding is not None else None
                    ),
                ),
            )

            if sdk_session_id and sdk_session_id != used_resume:
                logger.info(
                    "SDK session for '%s': %s",
                    self.config.role,
                    sdk_session_id,
                )

            # 8. Persist — the freshness reaper classifies each retained item at
            # its true sensitivity tier off the critical path (tier model §2.4);
            # nothing to compute or record per-turn here.

            # 9. SessionRegistry publish moved up, under the #573 session write
            # gate — see the attempt block above.

            # Task 14 / #199: record this turn's provenance so an operator can
            # `casactl explain <cid>` it. Fail-safe (never raises) and off the
            # streaming path — on_token already flushed the reply above.
            await self._record_turn_explanation(
                channel=msg.channel or "-",
                turn_state=turn_state,
                explain_draft=explain_draft,
            )

            # #349: commit the memory-budget record only for a turn that
            # completed END TO END (Sol r1: an SDK success whose registry
            # persistence then raised is still a failed turn). Failed turns
            # neither advance nor reset the overrun streak; a warm-reuse pool
            # turn skipped _build_options and has nothing stashed.
            _pending_budget = explain_draft.pop("pending_budget_record", None)
            if _pending_budget is not None:
                self._budget_tracker.record(*_pending_budget)

            return response_text or None
        finally:
            # #521: correlate a Casa-dispatched setup turn's outcome with its
            # episode — in the finally so success, a raising turn, AND a
            # cancelled one (role teardown cancels in-flight dispatches) all
            # report. Synchronous + never raises by contract.
            self._report_setup_outcome(msg, turn_state)
            _explain_draft_var.reset(explain_token)
            origin_var.reset(origin_token)

    def _report_setup_outcome(self, msg: BusMessage,
                              turn_state: dict) -> None:
        """#521: feed a Casa-dispatched plugin-setup turn's tool evidence to
        ``plugin_setup_episodes.report_dispatch_outcome`` so an episode is
        never terminally consumed by a session that could not run the setup
        tool. Evidence is aggregated across every SDK attempt of this bus
        turn (``turn_state["states"]``): the union of attempted tool names,
        the subset with at least one observed NON-error result, and the union
        of init-listed tool surfaces (None when no attempt saw an init — a
        warm-reuse session replays none, availability UNKNOWN). Runs from
        ``_process``'s finally: SYNCHRONOUS, fail-safe, never raises."""
        try:
            if msg.context.get("synthetic") != "plugin_setup":
                return
            episode_id = str(msg.context.get("setup_episode") or "")
            if not episode_id:
                return
            used_ok: set[str] = set()
            attempted: set[str] = set()
            available: set[str] | None = None
            for state in turn_state.get("states") or []:
                names = state.get("tool_names_by_id") or {}
                results = state.get("tool_results") or {}
                attempted.update(n for n in names.values() if n)
                used_ok.update(
                    n for i, n in names.items()
                    if n and i in results and results[i] is not True)
                tools = state.get("available_tools")
                if tools is not None:
                    available = (available or set()) | set(tools)
            import plugin_setup_episodes
            plugin_setup_episodes.report_dispatch_outcome(
                episode_id, tools_used_ok=used_ok,
                tools_attempted=attempted, available_tools=available)
        except Exception:  # noqa: BLE001 — the reply is already produced
            logger.exception("setup-episode outcome report failed")

    def _stash_explanation_draft(
        self, *, projection: str, bundle, system_prompt: str,
        facts: str, hits: tuple, clearance: str | None,
    ) -> None:
        """Populate this turn's ``_explain_draft_var`` with the projection +
        prompt + recall attributions that only ``_build_options`` observes.
        Reads/writes the per-turn draft dict in place; a no-op when no draft is
        bound (e.g. ``_build_options`` called directly by a unit test). Never
        raises into the options path."""
        draft = _explain_draft_var.get(None)
        if draft is None:
            return
        try:
            if bundle is not None:
                proj_obj = getattr(bundle, projection)
                draft["static_prompt_digest"] = proj_obj.digest
                draft["static_prompt_estimated_tokens"] = proj_obj.estimated_tokens
            else:
                draft["static_prompt_digest"] = ""
                draft["static_prompt_estimated_tokens"] = 0
            draft["projection"] = projection
            # Distinct tiers touched, insertion-ordered; attributions are the
            # clearance-gated, casa-source-free labels (never raw tags).
            draft["memory_tiers"] = tuple(
                dict.fromkeys(h.sensitivity for h in hits)
            )
            draft["memory_attributions"] = tuple(
                _attribution_label(h, clearance=clearance) for h in hits
            )
            draft["system_prompt"] = system_prompt
            draft["memory_text"] = facts or None
        except Exception:  # noqa: BLE001
            logger.debug("explanation draft stash skipped", exc_info=True)

    async def _record_turn_explanation(
        self, *, channel: str, turn_state: dict, explain_draft: dict,
    ) -> None:
        """Write ONE ExplanationStore record for the turn just completed so an
        operator can ``casactl explain <cid>`` it. Fail-safe by contract: any
        error (no store bound, an unrecordable ``"-"`` cid, a store write fault)
        is swallowed at debug level — the user's reply has already been produced
        and must never be delayed or failed by this telemetry.

        F7 (whole-branch review): ``ExplanationStore.record`` does a synchronous
        file write PLUS a prune that globs+stats up to EXPLANATION_MAX_RECORDS
        entries. Running that on the event loop blocked it every turn. The
        entire record is assembled here on the loop from task-local state
        (contextvars, ``self.config``, the per-turn draft/turn_state locals —
        all immutable by the time it is built), then ONLY the blocking
        ``store.record(record)`` is handed to ``asyncio.to_thread``; a fault in
        the thread re-raises into this same fail-safe try/except and is
        swallowed, exactly as before."""
        runtime = active_runtime
        store = getattr(runtime, "explanation_store", None) if runtime else None
        if store is None:
            return
        cid = cid_var.get()
        try:
            from explanation_store import ExplanationRecord

            route = (origin_var.get(None) or {}).get("_origin_route")
            _bundle = (
                self.config.compiled_prompt_bundle
                if self.config.kind == "resident" else None
            )
            # Prefer what _build_options observed; else derive deterministically
            # (a warm-reuse pool turn skips _build_options — projection selection
            # is still deterministic from channel+route+bundle).
            projection = explain_draft.get("projection")
            static_digest = explain_draft.get("static_prompt_digest")
            static_tokens = explain_draft.get("static_prompt_estimated_tokens")
            if projection is None:
                if _bundle is not None:
                    projection = _projection_name_for_turn(channel, route)
                    proj_obj = getattr(_bundle, projection)
                    static_digest = proj_obj.digest
                    static_tokens = proj_obj.estimated_tokens
                else:
                    projection = "legacy"
                    static_digest = ""
                    static_tokens = 0
            binding = self.config.binding
            state = turn_state.get("state") or {}
            tool_calls = tuple(sorted({
                name
                for name in (state.get("tool_names_by_id") or {}).values()
                if name
            }))
            record = ExplanationRecord(
                correlation_id=cid,
                role_id=self.config.role_id,
                kind=self.config.kind or "",
                resolved_model=self.config.model,
                persona_ref=(
                    f"{binding.persona_id}@{binding.persona_version}"
                    if binding is not None else None
                ),
                role_checksum=self.config.role_checksum or "",
                binding_digest=self.config.binding_digest or None,
                dependency_digests=(
                    binding.dependency_digests if binding is not None else ()
                ),
                effective_config_digest=(
                    binding.effective_config_digest if binding is not None else None
                ),
                # Residents have no lifecycle state (a specialist-instance
                # concept); honestly None rather than fabricated.
                lifecycle_state=None,
                projection=projection,
                static_prompt_digest=static_digest or "",
                static_prompt_estimated_tokens=int(static_tokens or 0),
                memory_tiers=tuple(explain_draft.get("memory_tiers", ())),
                memory_attributions=tuple(
                    explain_draft.get("memory_attributions", ())
                ),
                tool_calls=tool_calls,
                # Denials happen inside PreToolUse hooks and are not surfaced to
                # this seam (see GH #199): recorded empty, an honest gap.
                denials=(),
                system_prompt=explain_draft.get("system_prompt"),
                memory_text=explain_draft.get("memory_text"),
            )
            # F7: the blocking write+prune runs off the event loop. `record`
            # is a fully-built immutable dataclass; nothing task-local is read
            # inside the thread.
            await asyncio.to_thread(store.record, record)
        except Exception:  # noqa: BLE001
            logger.debug(
                "explanation record skipped cid=%s", cid, exc_info=True,
            )

    async def _build_options(
        self, *, channel: str, channel_key: str, is_fresh: bool,
        resume_sid: str | None, user_text: str,
    ) -> ClaudeAgentOptions:
        """Assemble the connect-time ClaudeAgentOptions for one conversation
        (spec §4.3 steps 2b–6). Extracted verbatim from _process so BOTH the
        pooled path (via the pool's build_options callback) and the bypass path
        build identical options. Keys the channel-aware memory load on the
        ``is_fresh`` PARAMETER (the pool decides it under the entry lock — AR-3),
        not a locally-recomputed value. Returns the stderr-wrapped options."""
        # #349: drop any budget stash a FAILED earlier attempt of this same
        # logical turn left behind — cleared at entry (not at the stash site
        # below) because the untrusted-webhook branch returns early and would
        # otherwise carry a stale overrun into the post-success commit.
        _draft = _explain_draft_var.get(None)
        if _draft is not None:
            _draft.pop("pending_budget_record", None)
        agent_home = (
            self.config.cwd
            or f"/config/agent-home/{self.config.role}"
        )

        # Release A / Layer 1 (PRIMARY containment): an UNTRUSTED webhook turn
        # — channel=="webhook" and NOT an explicit server-stamped `invoke`
        # (fail-closed: webhook_trigger OR any missing/unknown route) — builds a
        # locked-down runtime and short-circuits the full options path (no
        # plugins, no external hooks, no Bash/fs/net, two casa-framework tools).
        # webhook_trigger turns are SCHEDULED → bypass the client pool → fresh
        # options per turn, so this branch is pool-key-neutral.
        _route = (origin_var.get(None) or {}).get("_origin_route")
        # Personality Phase A, Task 8: a resident config carries a compiled
        # per-surface prompt bundle; specialists/executors keep None and stay on
        # the legacy self.config.system_prompt path untouched.
        _bundle = (
            self.config.compiled_prompt_bundle
            if self.config.kind == "resident" else None
        )
        if channel == "webhook" and _route != "invoke":
            # An untrusted webhook origin ALWAYS takes the restricted_webhook
            # projection (persona-stripped) for a resident — never route through
            # projection_for here, which would fall back to the text surface for
            # a missing/unknown route and leak the persona to an untrusted origin.
            restricted_prompt = (
                _bundle.restricted_webhook.system_prompt
                if _bundle is not None else self.config.system_prompt
            )
            restricted = build_restricted_webhook_options(
                model=self.config.model,
                role=self.config.role,
                system_prompt=restricted_prompt,
                max_turns=self.config.tools.max_turns,
                agent_home=agent_home,
                resume_sid=resume_sid,
            )
            # #199: an untrusted webhook turn does no recall — record the
            # restricted projection + its persona-stripped prompt, memory empty.
            self._stash_explanation_draft(
                projection="restricted_webhook", bundle=_bundle,
                system_prompt=restricted_prompt, facts="", hits=(), clearance=None,
            )
            return sdk_logging.with_stderr_callback(restricted, engagement_id=None)

        # 2b. Memory context (spec §4.3) — channel-aware load on the
        # SemanticMemory seam: a cheap mental-model overlay at fresh-session
        # start, plus (text only) one recall filtered by channel clearance tier.
        load_plan = _plan_load(channel, is_fresh_session=is_fresh)
        bank = _memory_bank()
        overlay_digest = ""
        facts = ""
        # #199: the recall hits (+ the clearance they were gated at) feed the
        # explanation record's memory attributions; empty unless a recall
        # actually succeeds below.
        _recall_hits: tuple = ()
        _recall_clearance_used: str | None = None
        if load_plan.push_overlay and _overlay_allowed(channel):
            try:
                overlay_digest = await self._semantic_memory.profile(bank)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "profile overlay failed for role=%s", self.config.role,
                    exc_info=True,
                )
        if load_plan.auto_recall and not self._recall_breaker.allow():
            # Breaker open: run cold rather than hammer an unavailable backend
            # on every turn. No memory block; the model is never told an empty
            # recall happened (doctrine: "unavailable" ≠ "no memories").
            logger.info(
                "auto_recall role=%s outcome=skipped reason=breaker_open",
                self.config.role,
            )
        elif load_plan.auto_recall:
            _recall_t0 = time.monotonic()
            _recall_clearance = _current_origin_clearance(channel)
            try:
                # Bounded deadline: never the full HTTP timeout. No synchronous
                # retry on failure — a 504 means the reranker is overloaded and
                # retrying makes it worse (the seam itself only retries
                # connection-level drops, never HTTP errors).
                #
                # Task 11: the awaited method is now the typed, attributed
                # recall_items; the OWN _RecallBreaker/wait_for/record_* logic
                # around it is UNCHANGED (this site is NOT wrapped in the new
                # recall_health breaker). On success the hits are rendered with
                # their recorded attribution before assignment to ``facts``.
                _hits = await asyncio.wait_for(
                    self._semantic_memory.recall_items(
                        bank, user_text, tags=_recall_tier_tags(channel),
                        max_tokens=self.config.memory.token_budget,
                        clearance=_recall_clearance,
                        budget="mid",  # auto_recall is always non-voice (see _plan_load); voice uses the recall_memory pull tool at budget=low
                    ),
                    timeout=_AUTO_RECALL_TIMEOUT_S,
                )
                self._recall_breaker.record_success()
                _recall_hits = _hits
                _recall_clearance_used = _recall_clearance
                facts = render_recall(
                    _hits, current_speaker=speaker_provenance_for_role(self.config),
                    surface="text", clearance=_recall_clearance,
                    token_budget=self.config.memory.token_budget,
                )
            except (RecallUnavailable, asyncio.TimeoutError) as exc:
                self._recall_breaker.record_failure()
                logger.warning(
                    "auto_recall role=%s outcome=unavailable reason=%s "
                    "latency_ms=%d breaker_open=%s",
                    self.config.role,
                    getattr(exc, "reason", "deadline"),
                    int((time.monotonic() - _recall_t0) * 1000),
                    self._recall_breaker.open,
                )
            except Exception as exc:  # noqa: BLE001
                self._recall_breaker.record_failure()
                # Exception TYPE only — repr/traceback could embed the query
                # text, which must never be logged.
                logger.warning(
                    "auto_recall role=%s outcome=unavailable reason=unexpected:%s "
                    "latency_ms=%d breaker_open=%s",
                    self.config.role, type(exc).__name__,
                    int((time.monotonic() - _recall_t0) * 1000),
                    self._recall_breaker.open,
                )
        parts: list[str] = []
        if overlay_digest:
            parts.append(f"<peer_overlay>\n{overlay_digest}\n</peer_overlay>")
        if facts:
            # #581: auto-recall reads at the turn's clearance — per SENDER on
            # telegram — so the injected slice is bounded, and a model that
            # treats it as all of memory answers "no record" for a fact it
            # simply cannot read. One instruction line says so. The empty case
            # injects no block at all, which stays deliberately silent:
            # absence of a block is not a claim of absence.
            parts.append(
                f"<memory_context>\n{READABLE_SLICE_PROMPT_LINE}\n{facts}\n"
                f"</memory_context>"
            )
        memory_blocks = "\n".join(parts)

        if memory_blocks:
            # #349: recorded at end-of-turn, not here — `_build_options` runs
            # once per SDK ATTEMPT (retry_sdk_call), and recording per attempt
            # advanced the consecutive-overrun streak N times for one logical
            # turn. Stash into the per-turn draft; `_process` commits exactly
            # one record just before its successful return. When no draft is
            # bound (a unit test calling this method directly), record now.
            budget_record = (
                f"{channel_key}-{self.config.role}",
                estimate_tokens(memory_blocks),
                self.config.memory.token_budget,
            )
            if _draft is not None:
                _draft["pending_budget_record"] = budget_record
            else:
                self._budget_tracker.record(*budget_record)

        # 3. System prompt = composed-prompt + runtime-injected blocks. For a
        # resident, the base is the immutable compiled projection selected for
        # this surface (text/voice); specialists/executors keep the legacy
        # composed self.config.system_prompt (Plan 1 does not touch their
        # prompt composition).
        if _bundle is not None:
            from prompt_compiler import projection_for
            base_system_prompt = projection_for(
                _bundle, channel=channel, origin_route=_route,
            ).system_prompt
        else:
            base_system_prompt = self.config.system_prompt
        system_parts = [base_system_prompt]
        if memory_blocks:
            system_parts.append("\n" + memory_blocks)
        # #336: the static per-channel display claims "authenticated (operator)"
        # for telegram, which is a lie the model would act on when an
        # accept-all-mode stranger is the sender. Key the honesty off the same
        # per-sender origin clearance the recall gate uses: anything below
        # private on telegram means the sender is NOT the configured operator.
        _trust_display = channel_trust_display(channel)
        if channel == "telegram" and _current_origin_clearance(channel) != "private":
            _trust_display = (
                "authenticated chat, but the sender is NOT the configured "
                "operator — treat as an unknown member of the public; do not "
                "disclose personal, household, or operator information"
            )
        system_parts.append(
            "\n<channel_context>\n"
            f"channel: {channel}\n"
            f"trust: {_trust_display}\n"
            "</channel_context>"
        )
        # <delegates> block — renders cfg.delegates with display names.
        # #436: names come from the LIVE role map, not this agent's
        # construction-time registry, so a per-role reload that renamed a
        # delegate cannot leave this agent advertising a name delegation
        # would refuse.
        delegates_block = _render_delegates_block(
            self.config.delegates, self._agent_registry,
            live_names=_live_agent_directory(),
        )
        if delegates_block:
            system_parts.append("\n" + delegates_block)
        # <executors> block — assistant role only (loader enforces this).
        executors_block = _render_executors_block(self.config.executors)
        if executors_block:
            system_parts.append("\n" + executors_block)
        # NOTE: <current_time> is intentionally NOT part of the system prompt —
        # a per-second timestamp in the cached prefix would invalidate Anthropic
        # prompt caching for the whole conversation every turn (see M27). It
        # rides on the per-turn query text (built in _process).
        system_prompt = "\n".join(system_parts)

        # #199: stash the projection + assembled prompt + recall attributions for
        # this turn's explanation record. A resident's projection name is derived
        # in lockstep with the projection_for selection above; a
        # specialist/executor (no bundle) records the honest "legacy" projection.
        self._stash_explanation_draft(
            projection=(
                _projection_name_for_turn(channel, _route)
                if _bundle is not None else "legacy"
            ),
            bundle=_bundle, system_prompt=system_prompt,
            facts=facts, hits=_recall_hits, clearance=_recall_clearance_used,
        )

        # 4. Hooks — resolved from hooks.yaml at load time by agent_loader.
        #    I-2 (v0.69.8): always inject the agent-home settings.json
        #    self-grant guard — a code-side security invariant that config
        #    cannot remove. Build a fresh dict so the shared _resolved_hooks
        #    isn't mutated across turns.
        from hooks import agent_home_settings_guard_matcher
        hooks = dict(self._resolved_hooks)
        # #403: residents carry the trigger-file guard too. A resident with
        # broad Bash (the shipped assistant has it) writing its OWN
        # triggers.yaml is the same lost-update shape as the configurator's —
        # its reminder tools write that file from Casa's loop, and a shell
        # redirect from the CLI child cannot be serialized against them. Both
        # paths lead to config_trigger_upsert / set_reminder instead.
        from hooks import trigger_file_write_guard_matcher
        hooks["PreToolUse"] = [
            *hooks.get("PreToolUse", []),
            agent_home_settings_guard_matcher(),
            trigger_file_write_guard_matcher(),
        ]

        # Unified plugin architecture: the resolver turns this agent's
        # tier:role assignment into immutable artifact paths. Resolved
        # off-loop + cached per instance (see _get_plugin_resolution).
        resolution = await self._get_plugin_resolution()

        # Authorization grants (A:§3.2): APPEND the fail-closed PreToolUse authz
        # matcher for this role's PROTECTED plugin tools, preserving the
        # settings guard already in hooks["PreToolUse"]. protected_map derives
        # from the SAME resolution the options use — it supplies
        # GrantKey.artifact_id, so a mid-TTL plugin update invalidates grants.
        # Only appended when the role actually has protected tools (an authz
        # matcher with matcher=None routes EVERY tool call, so skip the no-op).
        from authz_grants import (
            AuthzDeps, CHALLENGES, GRANTS, make_resident_authz_hook,
        )
        from plugin_grants import protected_map
        _protected = protected_map(resolution)
        if _protected:
            from claude_agent_sdk import HookMatcher
            _cm = self._channel_manager

            def _authz_deps_factory(_cm=_cm):
                ch = _cm.get("telegram") if _cm is not None else None
                if ch is None:
                    return None  # no DM reachable ⇒ unsupported-origin deny
                # Read the CURRENT loaded character name at call time (W2) — a
                # reload swaps self.config, so the next challenge names the new
                # display name; never a boot-time snapshot.
                return AuthzDeps(
                    channel=ch, grants=GRANTS, challenges=CHALLENGES,
                    display_name=self.config.character.name,
                )

            hooks["PreToolUse"] = [
                *hooks.get("PreToolUse", []),
                HookMatcher(hooks=[make_resident_authz_hook(
                    self.config.role, _protected, _authz_deps_factory)]),
            ]

        # Skills are enabled via the `skills="all"` option below, NOT by
        # putting "Skill" in allowed_tools ((f) v0.69.9: bare "Skill" is
        # deprecated by the SDK; skills="all" auto-allows the Skill tool +
        # keeps our explicit setting_sources=["project"]). Strip any
        # config-supplied "Skill" so a runtime.yaml still listing it (deployed
        # configs, pre-reconcile) never re-introduces the deprecated form.
        allowed_tools = [t for t in self.config.tools.allowed if t != "Skill"]

        # P-5a: installed ⇒ granted, by construction — server-level grants
        # derived from the SAME resolved artifacts the loader consumes.
        for grant in grants_for_resolution(resolution):
            if grant not in allowed_tools:
                allowed_tools.append(grant)

        # Resolve role-aware MCP servers only after every config/plugin grant
        # is known so SDK factories can expose the exact authorized schemas.
        mcp_servers = self._mcp_registry.resolve(
            self.config.mcp_server_names,
            role=self.config.role,
            allowed_tools=allowed_tools,
        )
        skills = (
            "all" if getattr(self.config.tools, "skills", "all") == "all"
            else None
        )

        options = ClaudeAgentOptions(
            model=self.config.model,
            cli_path=CLAUDE_CLI_PATH,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            disallowed_tools=self.config.tools.disallowed,
            permission_mode=self.config.tools.permission_mode or "acceptEdits",
            max_turns=self.config.tools.max_turns,
            mcp_servers=mcp_servers if mcp_servers else {},
            hooks=hooks,
            cwd=agent_home,
            resume=resume_sid,
            setting_sources=["project"],
            skills=skills,
            # Task 5 verified: no runtime-identity string to fix here — this
            # passes only `rp.path`, never `rp.name`/`rp.manifest_name`. The
            # CLI loads `.claude-plugin/plugin.json` itself from that path
            # and namespaces the plugin's own tools off `plugin.json.name`,
            # which for an owned artifact already IS the manifest name (the
            # scoped `<owner>.<manifest_name>` form only ever lives in the
            # REGISTRY, never written into the artifact's own plugin.json).
            plugins=[{"type": "local", "path": rp.path}
                     for rp in resolution.plugins],
            # #429: the other half of the withhold relaxation. A plugin that
            # declares casa.setupProvides is no longer
            # withheld for those vars, but the CLI expands an UNDEFINED
            # ${VAR} to the LITERAL string — so each still-unresolved
            # declared var is pinned to "" here, and the plugin's MCP server
            # sees a genuinely empty value instead of a placeholder
            # credential. Never overrides a wired value (the helper emits
            # only unresolved vars), so this is {} on a fully-wired system.
            env=sanitized_env_for_resolution(resolution),
            # P-5b: in-casa agents have no permission relay — fail closed on
            # ungranted tools instead of hanging on CC's prompt. New closure
            # per build is fine: the pool reuses clients, not options objects.
            can_use_tool=make_fail_closed_can_use_tool(self.config.role),
            # Voice partial-message streaming (2026-07-11 design §2 point 1):
            # SDK partial StreamEvents are opt-in and constant per channel,
            # so this stays pool-key compatible (spec §Q6). Non-voice
            # channels are byte-for-byte unaffected — StreamEvents simply
            # never arrive when this is False.
            include_partial_messages=(channel == "voice"),
        )
        return sdk_logging.with_stderr_callback(options, engagement_id=None)

    def _make_on_message(
        self,
        on_token: OnTokenCallback | None,
        turn_guard: VoiceTurnGuard | None = None,
    ):
        """Build the per-turn ``on_message(sdk_msg)`` handler + its ``state``.

        Reproduces today's per-message body VERBATIM (Phase 4b sdk_logging
        dispatch wrapped in try/except, tool_names_by_id, idx counter,
        started_ms, E-2 streaming concat into state["text"] with cumulative
        on_token, usage extraction into state["usage"]) — MINUS session-id
        capture, which the warm client / pool now owns (spec Q7). A fresh state
        per call resets the streaming accumulator per attempt (spec §3.2).

        Voice partial-message streaming (2026-07-11 design, AR-A/AR-B/AR-E):
        ``state["partial"]`` accumulates the in-flight message's text deltas
        from ``StreamEvent`` messages (only ever produced when
        ``include_partial_messages=True``, i.e. voice turns — see
        ``_build_options``). ``_cum()`` is the single pinned formula (AR-A)
        joining the folded ``state["text"]`` to the in-flight partial with
        the same "\\n\\n" separator the canonical fold uses, so a partial
        emission and the eventual fold never disagree — this is what makes
        message N+1's FIRST partial emission already carry the joiner.
        ``state["last_emitted"]`` dedupes: on_token only fires when the
        computed cumulative actually changed (AR-A/AR-B)."""
        state: dict[str, Any] = {
            "text": "",
            "usage": {},
            "idx": 0,
            "started_ms": time.monotonic() * 1000,
            "tool_names_by_id": {},
            "partial": "",
            "last_emitted": "",
            # #521 setup-outcome evidence: the session's init tool list
            # (None until an init SystemMessage arrives — a warm-reuse turn
            # replays none, which reads as availability UNKNOWN) and each
            # observed tool result's is_error, keyed like tool_names_by_id.
            "available_tools": None,
            "tool_results": {},
        }

        def _cum() -> str:
            return (
                state["text"]
                + ("\n\n" if state["text"] and state["partial"] else "")
                + state["partial"]
            )

        async def on_message(sdk_msg: Any) -> None:
            # Voice partial streaming — handled EARLY so a StreamEvent never
            # falls through to the phase4b dispatch below (no per-token log
            # lines, no idx/tool bookkeeping). AR-E: defensive parsing — the
            # CLI can forward raw `error` events with no `delta` key, or any
            # other shape; a malformed event must never abort the turn.
            if isinstance(sdk_msg, StreamEvent):
                try:
                    ev = getattr(sdk_msg, "event", None) or {}
                    if ev.get("type") == "content_block_delta":
                        d = ev.get("delta") or {}
                        if d.get("type") == "text_delta":
                            t = d.get("text") or ""
                            if t:
                                state["partial"] += t
                                cum = _cum()
                                if (
                                    on_token is not None
                                    and cum != state["last_emitted"]
                                ):
                                    await on_token(cum)
                                    state["last_emitted"] = cum
                except Exception as stream_exc:  # noqa: BLE001
                    logger.warning(
                        "stream_event dispatch failed: %s", stream_exc,
                        exc_info=True,
                    )
                return

            if turn_guard is not None:
                try:
                    turn_guard.observe(sdk_msg)
                except VoiceToolLoopError as exc:
                    logger.info(
                        "voice_tool_loop_stop reason=%s "
                        "live_context_successes=%d validation_failures=%d",
                        str(exc),
                        turn_guard.live_context_successes,
                        turn_guard.validation_failures,
                    )
                    raise

            # Phase 4b dispatch — wrapped so a malformed block cannot abort the
            # turn (logged + continued).
            try:
                if isinstance(sdk_msg, SystemMessage):
                    sdk_logging.log_system_init(sdk_msg)
                    # #521: the init event's `tools` is the session's actual
                    # tool surface — the availability half of the setup-
                    # episode outcome evidence. Only a positively parsed
                    # list counts; anything else leaves UNKNOWN (None).
                    if getattr(sdk_msg, "subtype", None) == "init":
                        tools = (getattr(sdk_msg, "data", {}) or {}).get(
                            "tools")
                        if isinstance(tools, list):
                            state["available_tools"] = [
                                str(t) for t in tools]
                elif isinstance(sdk_msg, AssistantMessage):
                    state["idx"] += 1
                    sdk_logging.log_assistant_message(sdk_msg, idx=state["idx"])
                    for block in getattr(sdk_msg, "content", []) or []:
                        if isinstance(block, ToolUseBlock):
                            state["tool_names_by_id"][
                                getattr(block, "id", "")
                            ] = getattr(block, "name", "?")
                            sdk_logging.log_tool_use(
                                block,
                                idx=state["idx"],
                                started_ms=state["started_ms"],
                            )
                elif isinstance(sdk_msg, UserMessage):
                    for block in getattr(sdk_msg, "content", []) or []:
                        if isinstance(block, ToolResultBlock):
                            name = state["tool_names_by_id"].get(
                                getattr(block, "tool_use_id", ""), "",
                            )
                            sdk_logging.log_tool_result(
                                block, idx=state["idx"],
                                started_ms=state["started_ms"], name=name,
                            )
                            # #521: record the result's error flag so the
                            # setup-outcome report can tell "ran" from
                            # "attempted but every result errored".
                            state["tool_results"][
                                getattr(block, "tool_use_id", "")
                            ] = getattr(block, "is_error", None)
                elif isinstance(sdk_msg, ResultMessage):
                    sdk_logging.log_turn_done(
                        sdk_msg, started_ms=state["started_ms"],
                    )
            except Exception as dispatch_exc:  # noqa: BLE001
                logger.warning(
                    "phase4b dispatch failed: %s", dispatch_exc, exc_info=True,
                )
            # Usage + E-2 streaming concat (session-id capture is the client's).
            if isinstance(sdk_msg, ResultMessage):
                state["usage"] = extract_usage(sdk_msg)
            elif isinstance(sdk_msg, AssistantMessage):
                # E-2: collect TextBlocks of THIS AssistantMessage.
                msg_text = "".join(
                    b.text for b in getattr(sdk_msg, "content", [])
                    if isinstance(b, TextBlock)
                )
                if msg_text:
                    if state["text"]:
                        state["text"] += "\n\n"
                    state["text"] += msg_text
                # The canonical fold supersedes any in-flight partial for
                # this message (AR-A/AR-B): reset before computing the
                # cumulative so a stale partial never bleeds into message
                # N+1's first delta. Runs unconditionally (even when this
                # message carried no text, e.g. tool-use-only) so a
                # tool-only fold never leaves a stale partial dangling —
                # cum() then equals state["text"] unchanged, which already
                # matches last_emitted, so no spurious emit follows.
                state["partial"] = ""
                cum = _cum()
                if on_token is not None and cum != state["last_emitted"]:
                    await on_token(cum)
                    state["last_emitted"] = cum

        return on_message, state

    async def invalidate_tool_surface(self) -> None:
        """Reconnect pooled SDK clients against the current MCP schemas."""
        await self._pool.invalidate_all()

    async def aclose(self) -> None:
        """Release pooled SDK clients + reset hook. Safe to call twice; called
        by reload (old instance) and casa_core shutdown."""
        try:
            self._unsub_reset()
        except Exception:  # noqa: BLE001
            pass
        # Sol r1 (#345): settle detached background cold-retains while the
        # loop is still alive — cancelling here runs retain_cold_session's
        # CancelledError arm (a synchronous spool write) deterministically,
        # instead of relying on the runtime's final task-cancellation sweep.
        for task in list(self._bg_tasks):
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)
        await self._pool.aclose()

    async def _maybe_prepend_health_notice(
        self, text: str,
    ) -> tuple[str, str | None]:
        """§3.10: prepend a one-line plugin-health notice if the report holds a
        blocking issue for this role.

        #551 (design r3, Sol+Terra): consulted on EVERY eligible turn rather
        than once per Agent instance. The old per-instance one-shot flag could
        not coexist with plugin_health's decaying memo — a turn whose delivery
        silently sent nothing (#556) consumed the flag, and no later turn ever
        asked again, so the memo's "it comes back after the cooldown" guarantee
        was unreachable and a blocking issue stayed unread for the life of the
        instance. plugin_health.pending_notice is now the sole suppression
        mechanism: it decays, so nothing here needs to know whether a send
        succeeded.

        Returns ``(text, notice_or_None)``. The caller hands the notice back to
        ``plugin_health.forget_notice`` if delivery RAISES, which keeps #349's
        immediate retry on a transient send failure — the cooldown covers the
        silent non-delivery it cannot detect (#556), not the loud one it can."""
        if not text:
            return text, None
        import plugin_health
        notice = await asyncio.to_thread(
            plugin_health.pending_notice, self.config.role)
        if notice:
            return f"{notice}\n\n{text}", notice
        return text, None

    @property
    def plugin_binding_snapshot(self) -> "PluginBindingSnapshot | None":
        """§3.9 verify's ONE coherent read (D2). None until first resolve."""
        return self._plugin_snapshot

    @property
    def _plugin_resolution(self):
        """Read-only view for legacy readers; publishing happens ONLY via
        the snapshot (single assignment — the torn pair is impossible)."""
        snap = self._plugin_snapshot
        return snap.resolution if snap is not None else None

    @property
    def active_plugin_binding(self) -> dict[str, str]:
        """Read-only {name: artifact_id} view of the snapshot."""
        snap = self._plugin_snapshot
        return dict(snap.binding) if snap is not None else {}

    async def _get_plugin_resolution(self):
        """Resolve this agent's tier:role plugin assignment to immutable
        artifacts, off-loop + cached per instance (§3.3/§3.9).

        resolve_for reads the process snapshot (refreshed from disk by
        casa_reload BEFORE agent reconstruction), so a fresh Agent always
        rebuilds this — no stale plugin set. Cached even when empty: under the
        registry the result is deterministic, and reconstruction is the
        invalidation seam. D2 (v0.74.0): resolution + binding + generation
        publish together as ONE frozen PluginBindingSnapshot — a concurrent
        §3.9 verify can never observe a torn (resolved, stale-binding) state.
        """
        if self._plugin_snapshot is not None:
            return self._plugin_snapshot.resolution
        async with self._plugin_resolution_lock:
            if self._plugin_snapshot is not None:
                return self._plugin_snapshot.resolution
            tier = None
            if self._agent_registry is not None:      # agent.py:199 attr name
                tier = self._agent_registry.tier_for_role(self.config.role)
            if tier is None:
                # v0.74.1 (live finding 2026-07-13): the AgentRegistry knows
                # only residents + ENABLED specialists, so a reload-
                # constructed DISABLED specialist lands here and the
                # 'resident' fallback resolves the WRONG target — an empty,
                # issueless resolution that looks like a healthy dormant
                # agent. The fallback stays (back-compat; a disabled
                # specialist takes no new turns), but it must be LOUD.
                logger.warning(
                    "plugin resolve tier-miss for role=%s: not in the "
                    "AgentRegistry (disabled specialist?) — falling back to "
                    "resident:%s, which likely resolves NO plugins",
                    self.config.role, self.config.role)
            target = f"{tier or 'resident'}:{self.config.role}"
            resolution = await asyncio.to_thread(
                plugin_registry.resolve_for, target,
            )
            # #424: withhold any plugin whose .mcp.json references env vars
            # not resolved in the effective environment — the CLI passes an
            # undefined ${VAR} through as the LITERAL string, so the plugin's
            # MCP server would start "successfully" with placeholder
            # credentials (observed live: an OAuth URL built with
            # client_id=${GMAIL_CLIENT_ID}). Withheld here, BEFORE the
            # snapshot publishes, so grants, protected_map and the recorded
            # binding all reflect what the session actually loads — verify's
            # FR3 binding comparison then correctly reports reload_required
            # once the secrets are wired. Filed as an env_unresolved issue so
            # the degraded-resolution warning below names it.
            from plugin_grants import withhold_env_unresolved
            resolution, withheld = await asyncio.to_thread(
                withhold_env_unresolved, resolution,
                context=f"{target} session")
            for rp, _missing in withheld:
                resolution.issues.append(plugin_registry.PluginIssue(
                    name=rp.name, target=target, stage="env",
                    reason_code="env_unresolved",
                    artifact_id=rp.artifact_id))
            # D2: ONE assignment publishes resolution + binding + generation
            # together — no torn-read window, ever.
            self._plugin_snapshot = PluginBindingSnapshot(
                resolution=resolution,
                binding=MappingProxyType({
                    rp.name: rp.artifact_id for rp in resolution.plugins}),
                generation=resolution.generation,
            )
            if resolution.issues:
                logger.warning(
                    "plugin resolution degraded for %s: %s", target,
                    [(i.name, i.reason_code) for i in resolution.issues])
            return resolution

    def _spawn_cold_retain(
        self, old: SessionEntrySnapshot, *, directory: str, channel: str,
        fence_generation: int | None = None,
    ) -> None:
        """Retain a cold prior session in the background (claim-free; cannot race
        register()). Tracked so it isn't GC'd; failures are swallowed in
        retain_cold_session and never reach the turn.

        Task 9/10: takes the immutable ``SessionEntrySnapshot`` the resume gate
        produced (``decision.old``) and passes it straight through to the reduced
        ``retain_cold_session``, which reads the speaker/user provenance off the
        snapshot itself. A legacy/corrupt snapshot with no usable provenance
        retains nothing (never invents authorship).

        ``fence_generation`` (#411) is the retain-fence generation captured at
        the RESUME DECISION that produced ``old`` — not here, and not inside
        the spawned coroutine: awaits (the pool's client close) sit between
        the decision and this call, and the task body may not run until later
        still; a memory wipe completing in either window must make the retain
        DISCARD rather than restore pre-wipe content. ``None`` (legacy/test
        callers) falls back to capturing now, which is still before the
        coroutine body runs."""
        if fence_generation is None:
            fence_generation = _retain_fence().generation()
        task = asyncio.create_task(
            retain_cold_session(
                old, directory=directory, channel=channel,
                semantic_memory=self._semantic_memory,
                fence_generation=fence_generation,
            ),
            name=f"cold-retain-{old.sdk_session_id}",
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _log_retry(self, attempt: int, exc: Exception, delay_ms: int) -> None:
        """Emit a single WARNING per retry event (spec 5.2 §3.2)."""
        kind = _classify_error(exc)
        logger.warning(
            "SDK retry: role=%s attempt=%d kind=%s delay_ms=%d exc=%r",
            self.config.role, attempt + 1, kind.value, delay_ms, exc,
        )
