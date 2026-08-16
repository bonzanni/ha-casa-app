"""Casa runtime container — single dataclass holding state previously
held by casa_core.main's closure.

Passed through init_tools(runtime=...) and stashed on the agent module
as `agent.active_runtime` so reload handlers (reload.py) and tool
handlers (tools.py) can reach all the registries without re-plumbing
through every callsite.

Spec: docs/superpowers/specs/2026-05-02-granular-reload-design.md §2.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from agent import Agent
    from agent_registry import AgentRegistry
    from bus import MessageBus
    from channels import ChannelManager
    from config import AgentConfig
    from engagement_registry import EngagementRegistry
    from executor_registry import ExecutorRegistry
    from job_registry import JobRegistry
    from mcp_registry import McpServerRegistry
    from personality_binding import BindingRecord
    from persona_pack import PersonaPack
    from policies import PolicyLibrary
    from prompt_compiler import CompiledPromptBundle
    from role_slot import RoleSlot
    from explanation_store import ExplanationStore
    from semantic_memory import SemanticMemory
    from session_registry import SessionRegistry
    from specialist_registry import SpecialistRegistry
    from trigger_registry import TriggerRegistry
    from channels.voice.delivery import VoiceDeliveryCoordinator
    from channels.voice.routes import VoiceRouteRegistry


@dataclass
class CasaRuntime:
    """Container for all process-global Casa state mutated by reloads.

    Mutability contract:
    - ``agents`` and ``role_configs`` are MUTATED by reload handlers
      (atomic-swap of role keys).
    - Registry attrs (``agent_registry``, ``policy_lib``) are
      REPLACED by reload handlers (rebind).
    - The four personality maps (``role_slots``, ``persona_packs``,
      ``bindings``, ``compiled_prompt_bundles``) are REPLACED (rebound as
      fresh read-only views) by :meth:`refresh_personality_maps`, which
      reload handlers call synchronously after every ``role_configs``
      mutation (GH #356).
    - Channel/bus/driver attrs are read-only after boot.
    - Path attrs (``config_dir``, ``agents_dir``, ``home_root``,
      ``defaults_root``) are read-only after boot.
    """

    # Mutable role-keyed dicts
    agents: dict[str, "Agent"]
    role_configs: dict[str, "AgentConfig"]

    # Registries (replaced by reloads; never mutated in place)
    specialist_registry: "SpecialistRegistry"
    executor_registry: "ExecutorRegistry"
    engagement_registry: "EngagementRegistry"
    agent_registry: "AgentRegistry"
    trigger_registry: "TriggerRegistry"
    mcp_registry: "McpServerRegistry"
    session_registry: "SessionRegistry"

    # Channels + bus + drivers (boot-fixed)
    channel_manager: "ChannelManager"
    bus: "MessageBus"
    engagement_driver: Any  # InCasaDriver — avoid import cycle
    claude_code_driver: Any  # ClaudeCodeDriver — avoid import cycle

    # Policy (boot-fixed)
    policy_lib: "PolicyLibrary"

    # Paths (boot-fixed)
    config_dir: str
    agents_dir: str
    home_root: str | Path
    defaults_root: str | Path

    # Long-term memory (boot-fixed). H9 (v0.49.0): reload._construct_agent
    # passes this into every Agent it builds — omitting it silently
    # downgraded reload-constructed residents to NoOpSemanticMemory.
    # Defaulted, so it MUST stay the LAST field (dataclass rule); test
    # stand-ins that skip it get None → Agent maps None to NoOp.
    semantic_memory: "SemanticMemory | None" = None

    # Durable delegated execution + delivery state. Defaulted for existing
    # narrow test stand-ins; production always injects the boot-loaded owner.
    job_registry: "JobRegistry | None" = None
    voice_route_registry: "VoiceRouteRegistry | None" = None
    voice_delivery_coordinator: "VoiceDeliveryCoordinator | None" = None

    # Personality Phase A, Task 8: read-only persona/binding registries derived
    # from the loaded resident configs at boot (and rebuilt by reloads). Keyed
    # per the interface note: role_slots by role_id, persona_packs by
    # "<persona_id>@<version>", bindings/compiled_prompt_bundles by role_id.
    # Defaulted (empty) so every existing narrow CasaRuntime(...) test
    # constructor keeps compiling unchanged — MUST stay after the fields above
    # (dataclass-ordering rule).
    role_slots: "Mapping[str, RoleSlot]" = field(default_factory=dict)
    persona_packs: "Mapping[str, PersonaPack]" = field(default_factory=dict)
    bindings: "Mapping[str, BindingRecord]" = field(default_factory=dict)
    compiled_prompt_bundles: "Mapping[str, CompiledPromptBundle]" = field(default_factory=dict)

    # Personality Phase A, Task 14: lean per-correlation-id explanation store
    # (inspect/explain telemetry). Constructed once at boot
    # (ExplanationStore(Path("/data/explanations"))) and preserved verbatim
    # across reload.py's mutate-in-place candidate-registry swap (reload.py
    # never reconstructs CasaRuntime). Defaulted (None) so every existing
    # narrow CasaRuntime(...) test constructor keeps compiling unchanged —
    # MUST stay the final field (dataclass-ordering rule).
    explanation_store: "ExplanationStore | None" = None

    # #340: the per-executor HTTP hook-policy map
    # (``{executor_type: {policy_name: (matcher, callback)}}``) built by
    # ``casa_core._build_executor_cc_hook_policies`` and captured by the
    # /hooks/resolve handlers at boot. ``reload.reload_executors`` MUTATES it
    # in place (clear+update) so the captured references see fresh policies —
    # never rebind this attr. Defaulted (None) so narrow test stand-ins keep
    # compiling; a None map means "no boot map to refresh" and reload skips
    # the rebuild. New defaulted fields are APPENDED after this one.
    executor_cc_policies: dict | None = None

    # #609: whether the ONE global webhook secret is usable. `hmac_body`
    # triggers verify against it, and it is blank when the option holds an
    # unresolved op:// reference or generation failed — in which case every
    # request to such a trigger 401s permanently. Decided at boot beside the
    # value itself (the handlers close over a `main()` local, so nothing else
    # can see it) and restart-only, exactly like the closure. Defaults False:
    # a report that cannot establish the secret is usable must not claim it is.
    # Appended, per this file's convention that new defaulted fields go last.
    webhook_global_secret_usable: bool = False

    def refresh_personality_maps(self) -> None:
        """Re-derive the four personality maps from ``role_configs`` and
        REBIND them as fresh read-only views (GH #356).

        Single source of truth for the derivation: boot (casa_core) calls
        this right after constructing the runtime, and every reload path
        calls it synchronously after mutating ``role_configs`` — otherwise
        a hot-added resident is dispatchable while ``casactl persona
        inspect/render/diff`` 404s for its role until restart (and an
        evicted one stays inspectable).

        Pure in-memory derivation — no I/O, no await point — so callers on
        the event loop can invoke it between a ``role_configs`` mutation
        and their next await without opening a stale-map window. Each attr
        is rebound atomically; readers fetch the attrs per request and
        never hold them across an await, so no torn view is observable
        (Sol/Terra design round 1, 2026-08-12).
        """
        from types import MappingProxyType

        role_slots: dict = {}
        persona_packs: dict = {}
        bindings: dict = {}
        compiled_prompt_bundles: dict = {}
        # getattr-with-default: production AgentConfig always carries these
        # fields; the default tolerates the narrow SimpleNamespace stand-ins
        # test harnesses seed role_configs with (same "absent → skip"
        # semantics either way).
        for cfg in self.role_configs.values():
            role_slot = getattr(cfg, "role_slot", None)
            if role_slot is None:
                continue
            role_slots[cfg.role_id] = role_slot
            persona_pack = getattr(cfg, "persona_pack", None)
            if persona_pack is not None:
                persona_packs[
                    f"{persona_pack.persona_id}@{persona_pack.version}"
                ] = persona_pack
            binding = getattr(cfg, "binding", None)
            if binding is not None:
                bindings[cfg.role_id] = binding
            bundle = getattr(cfg, "compiled_prompt_bundle", None)
            if bundle is not None:
                compiled_prompt_bundles[cfg.role_id] = bundle
        self.role_slots = MappingProxyType(role_slots)
        self.persona_packs = MappingProxyType(persona_packs)
        self.bindings = MappingProxyType(bindings)
        self.compiled_prompt_bundles = MappingProxyType(compiled_prompt_bundles)
