"""Tier 2 specialist loader + durable delegation compatibility facade.

Symmetric with :mod:`session_registry` and :mod:`mcp_registry`.
Scans a directory for per-specialist YAML files, validates the Tier 2
shape (no channels, zero token budget, ephemeral session), honours the
new ``enabled: bool`` field, and exposes a
runtime lookup used by the ``delegate_to_agent`` framework tool.

Delegation lifecycle state belongs exclusively to :mod:`job_registry`.
The legacy methods in this module remain as a narrow facade for existing
sync/async delegation call sites while they migrate to the job-native API.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from config import AgentConfig
from job_registry import (
    DeliveryState,
    ExecutionState,
    JobRegistry,
    VoiceJob,
)
from personality_binding import InstanceDir
from personality_types import SpeakerProvenance
# Forward re-export for future Plan 2 / Task 14 callers (module-level-accessor
# precedent this file already follows, e.g. _active_index below).
from specialist_lifecycle import InstanceState, SpecialistInstance, check_slug_uniqueness  # noqa: F401

logger = logging.getLogger(__name__)


def scheduled_delivery_of(origin: "dict[str, Any] | None") -> bool:
    """Is *origin* a turn Casa's own schedule fired (#485)?

    The one place the live origin marker is converted into the durable job's
    boolean. Exact ``True`` only: absence, ``None``, and any truthy stand-in
    are all "no". ``bool()`` would be wrong here — ``bool("false")`` is True,
    and a persisted eligibility is not somewhere to be generous.
    """
    return (origin or {}).get("_scheduled_delivery") is True


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class DelegationRecord:
    """Legacy call-site input translated into a durable ``VoiceJob``."""

    id: str                          # UUID4
    agent: str                       # specialist name (role)
    started_at: float                # time.time()
    origin: dict[str, Any] = field(default_factory=dict)
    # origin carries the channel/chat_id/cid/role/user_text of the
    # delegating resident's turn so the late-completion NOTIFICATION
    # can be delivered back to the right user via the right channel.
    # Task 6 (spec §4.6): the legacy delegate task still owns this Permit via
    # its done callback. It is never copied into the durable job snapshot.
    permit: Any = None


@dataclass
class DelegationComplete:
    """Typed payload published on the bus as NOTIFICATION content when a
    delegation resolves (or fails, or restart-orphans)."""

    delegation_id: str
    agent: str
    status: str                                    # "ok" | "error"
    text: str = ""
    kind: str = ""                                 # error kind or "restart_orphan"
    message: str = ""
    origin: dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0
    # Task 6 (spec §4.6): True when the delegated output was clipped to
    # `_MAX_OUTPUT_CHARS` before this notification was assembled, so the
    # narrating resident can disclose the answer was cut short.
    output_truncated: bool = False
    # #701/#688: False when this is a BOOT REPLAY of a delegation that really
    # did succeed but whose answer text was never retained (Casa does not
    # persist a non-voice specialist's answer). `status` stays truthful — the
    # work succeeded — and the empty `text` must never be narrated as if it
    # were the answer.
    result_available: bool = True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SpecialistRegistry:
    """Load Tier 2 specialists and facade legacy lifecycle calls."""

    def __init__(
        self,
        specialists_dir: str,
        tombstone_path: str | None = None,
        *,
        job_registry: JobRegistry | None = None,
    ) -> None:
        self._dir = specialists_dir
        self._configs: dict[str, AgentConfig] = {}
        self._disabled_names: set[str] = set()
        self._load_failures: list[tuple[str, str]] = []
        if job_registry is None:
            if tombstone_path is None:
                raise TypeError("job_registry or tombstone_path is required")
            # Convenience construction for tests and embedders: derive the
            # jobs.json home from the given path. Production injects the one
            # boot-loaded registry explicitly.
            job_registry = JobRegistry(
                os.path.join(os.path.dirname(tombstone_path), "jobs.json"),
            )
        self._job_registry = job_registry

    # -- Loading / validation -------------------------------------------------

    def load(self, *, roles_dir: str | None = None) -> None:
        """Scan ``self._dir`` for specialist directories and register valid ones.

        O-2b (v0.37.9): per-specialist failures are tracked in
        :attr:`_load_failures` (also retrievable via :meth:`load_failures`)
        so :mod:`reload` can surface them to ``casactl`` callers. One
        malformed specialist does not poison its siblings — see
        :func:`agent_loader.load_all_specialists`.

        Plan 2, Task N1b: ``roles_dir``, when given, overrides the image-only
        default so an installed specialist's role artifact (which cannot
        live under ``defaults/roles/``) is found. Production always passes
        the reconciled overlay root (``casa_core.py``); tests and every
        other pre-existing caller omit it and get the unchanged image-only
        behavior.

        #439: the scan builds into LOCAL containers and publishes them by
        rebinding, so no reader ever observes a partially-filled registry. This
        runs on a worker thread (``asyncio.to_thread``) while the event loop is
        free to serve another role's reload, and ``reload.dispatch`` serializes
        only on a per-``agent:<role>`` key — so a concurrent
        ``tools.sync_agent_role_map`` snapshots ``all_configs()`` right here. The
        old clear-then-refill made that snapshot the *delegation authority*
        missing specialists that are perfectly healthy on disk: ``delegate_to_agent``
        refuses them as unknown and, since v0.157.0, the ``<delegates>`` block
        renders from the same map and silently drops them. Building locally makes
        every snapshot a whole generation.
        """
        from agent_loader import LoadError, load_all_specialists

        try:
            found, failed = load_all_specialists(self._dir, roles_dir=roles_dir)
        except LoadError as exc:
            # Collection-level error (e.g. non-directory under specialists/).
            logger.error("Specialist load failed at collection level: %s", exc)
            found, failed = {}, [("(collection)", str(exc))]

        configs: dict[str, AgentConfig] = {}
        disabled: set[str] = set()
        failures: list[tuple[str, str]] = []

        for name, err in failed:
            logger.error(
                "Specialist %r failed to load: %s; other specialists continue",
                name, err,
            )
            failures.append((name, err))

        for role, cfg in found.items():
            if not self._validate_tier2_shape(cfg, role):
                continue
            if not cfg.enabled:
                logger.info("Specialist %r bundled but disabled", role)
                disabled.add(role)
                continue
            configs[role] = cfg
            logger.info("Specialist %r loaded (model=%s)", role, cfg.model)
            # D-2 (v0.69.7): emit the same Layer-5 capability line residents
            # log in Agent.__init__ — specialists never build an Agent (they
            # run via _build_specialist_options), so without this they had no
            # boot-time capability oracle for post-install verification.
            # #459: this line reports the role.yaml DECLARATION only — the
            # effective set (server-level grants from the owned plugin, env
            # withholding) is composed per delegation and logged there as
            # `agent_capabilities_effective` (tools.py). Fields say
            # `declared_*` so `declared_tool_count=0` cannot be read as "the
            # specialist got no tools".
            try:
                allowed = list(getattr(cfg.tools, "allowed", []) or [])
                logger.info(
                    "agent_capabilities role=%s model=%s enabled=%s "
                    "declared_tool_count=%d declared_tools=%s "
                    "declared_mcp_servers=%s",
                    cfg.role, getattr(cfg, "model", "?"),
                    getattr(cfg, "enabled", "?"),
                    len(allowed), sorted(allowed),
                    sorted(getattr(cfg, "mcp_server_names", []) or []),
                )
            except Exception:  # noqa: BLE001 — an observability line must never break load
                logger.warning("agent_capabilities log failed for specialist role=%s",
                               getattr(cfg, "role", "?"), exc_info=True)

        # PUBLISH — the single point where this scan becomes visible. Residual,
        # stated rather than implied away: these are three separate stores, so a
        # reader that consults two of them across the rebinds can see two
        # different generations. What it can no longer see is HALF of one, which
        # is the defect: every accessor here reads exactly one store, and each
        # store is now published whole.
        self._load_failures = failures
        self._disabled_names = disabled
        self._configs = configs

        logger.info(
            "Specialists: enabled=%s disabled=%s failed=%s",
            sorted(configs.keys()),
            sorted(disabled),
            sorted(n for n, _ in failures),
        )

    def load_failures(self) -> list[tuple[str, str]]:
        """Return per-specialist load failures from the last :meth:`load`.

        Defensive copy — callers cannot mutate registry state. Each entry
        is ``(directory_name, error_message)``. Empty list means the last
        load saw no per-specialist errors.
        """
        return list(self._load_failures)

    def _validate_tier2_shape(
        self, cfg: AgentConfig, role: str,
    ) -> bool:
        if cfg.channels:
            logger.error(
                "Rejecting specialist %r: Tier 2 forbids non-empty 'channels:' "
                "(channels belong to Tier 1 residents in agents/).",
                role,
            )
            return False
        if cfg.session.strategy != "ephemeral":
            logger.error(
                "Rejecting specialist %r: session.strategy must be 'ephemeral' "
                "(got %r).", role, cfg.session.strategy,
            )
            return False
        return True

    def get(self, agent_name: str) -> AgentConfig | None:
        """Return the enabled specialist config, or None."""
        return self._configs.get(agent_name)

    def is_disabled(self, role: str) -> bool:
        """True if ``role`` is bundled but disabled in user config.

        Returns False for unknown roles and for enabled specialists.
        Disabled-but-known specialists are still distinguishable from
        unknown roles (memory is data, enablement is operational).
        """
        return role in self._disabled_names

    def disabled_roles(self) -> list[str]:
        """Return a sorted list of disabled specialist role names.

        Defensive copy — caller cannot mutate registry state.
        """
        return sorted(self._disabled_names)

    def all_configs(self) -> dict[str, "AgentConfig"]:
        """Return a snapshot of enabled specialist configs by role.

        Used at boot to build the merged role→AgentConfig registry that
        ``delegate_to_agent`` resolves against. Returns a defensive copy.
        """
        return dict(self._configs)

    # -- Durable delegation compatibility facade -------------------------

    @property
    def job_registry(self) -> JobRegistry:
        return self._job_registry

    def has_delegation(self, delegation_id: str) -> bool:
        job = self._job_registry.get(delegation_id)
        return bool(job and job.execution_state in {
            ExecutionState.ACCEPTED, ExecutionState.RUNNING,
        })

    async def register_delegation(self, record: DelegationRecord) -> None:
        await self._job_registry.load()
        origin = dict(record.origin)
        # Task 12: creating_speaker is the DELEGATING caller's own identity,
        # carried on origin["speaker_provenance"] by Task 10 Step 7's
        # origin_var wiring; executing_speaker is the target specialist's own
        # binding, read off the config this registry already loaded. `record`
        # carries no AgentConfig of any kind — both values MUST come from one
        # of these two already-accessible places, never a new parameter.
        creating_speaker = origin.get("speaker_provenance")
        if not isinstance(creating_speaker, SpeakerProvenance):
            creating_speaker = SpeakerProvenance(speaker_kind="system")
        specialist_cfg = self._configs.get(record.agent)
        # Task 14 (whole-branch review): reuse the ONE canonical executing-speaker
        # fallback (agent.speaker_provenance_for_role) instead of re-implementing
        # it. For a bound specialist it returns cfg.speaker_provenance; for an
        # unbound one it returns the honest unattributed `system` identity, never
        # "executor:<slug>" (a specialist's kind is never "executor"). Lazy import
        # mirrors tools.py's reuse of the same helper (no circular import).
        import agent as agent_mod
        executing_speaker = (
            agent_mod.speaker_provenance_for_role(specialist_cfg)
            if specialist_cfg is not None
            else SpeakerProvenance(speaker_kind="system")
        )
        await self._job_registry.create(VoiceJob(
            id=record.id,
            parent_job_id=None,
            creating_speaker=creating_speaker,
            executing_speaker=executing_speaker,
            creating_role=str(origin.get("role") or "assistant"),
            specialist_role=record.agent,
            specialist_display_name=record.agent,
            creator_peer=str(origin.get("channel") or ""),
            creator_user_id=self._optional_str(origin.get("user_id")),
            scope_id=str(origin.get("chat_id") or ""),
            scheduled_delivery=scheduled_delivery_of(origin),
            origin_route_id=self._optional_str(origin.get("cid")),
            origin_device_id=self._optional_str(
                origin.get("origin_device_id")),
            task=str(origin.get("user_text") or ""),
            context="",
            created_at=float(record.started_at),
            started_at=float(record.started_at),
            terminal_at=None,
            expires_at=None,
            execution_state=ExecutionState.RUNNING,
            delivery_state=DeliveryState.NONE,
            result=None,
            failure=None,
            awaiting_input=False,
            continuable_until=None,
            delivery_sequence=0,
            delivery_attempt_id=None,
            lease_until=None,
            cancel_pending=False,
        ))

    # Task 6 (spec §4.6): these terminal transitions deliberately do NOT
    # release the concurrency permit. For a LAUNCHED sync/async delegation
    # the task's ``_permit_release_callback`` done-callback is the SOLE
    # authoritative release — it fires only when the task ACTUALLY ends
    # (honouring cancellation). ``cancel_delegation`` in particular is called
    # by the voice teardown after only a bounded wait (tools._voice_deadline_
    # exceeded), while the specialist task may still be unwinding; releasing
    # here would free the slot for a NEW delegation while the original is
    # still executing (idempotence cannot undo a premature release). Pre-
    # launch cancellation is covered by the lexical ``owned`` guard in
    # delegate_to_agent. (Interactive engagements, which have no task done-
    # callback, DO release in EngagementRegistry terminal transitions.)
    async def complete_delegation(
        self, delegation_id: str, *, announce_creator: bool = False,
    ) -> VoiceJob | None:
        """Persist the successful terminal; return the durable row.

        #701: the row is RETURNED because the announcing caller must ask it one
        question before it enqueues anything — whether the creator cancelled
        while the specialist was finishing, in which case the terminal is
        CANCELLED and the creator is deliberately not told it completed.
        ``announce_creator`` arms the durable obligation to announce; only the
        path that actually posts a notification passes it.
        """
        await self._job_registry.load()
        return await self._job_registry.finish_compat(
            delegation_id, "", announce_creator=announce_creator)

    async def fail_delegation(
        self, delegation_id: str, exc: Exception,
    ) -> None:
        await self._job_registry.load()
        await self._job_registry.fail_compat(delegation_id, exc)

    async def cancel_delegation(self, delegation_id: str) -> None:
        await self._job_registry.load()
        await self._job_registry.cancel(delegation_id)

    def orphans_from_disk(self) -> list[DelegationRecord]:
        """Compatibility view of already-loaded orphaned durable jobs.

        This method deliberately performs no file I/O.  Boot migration and
        restart recovery are owned by :class:`JobRegistry`.
        """
        return [
            DelegationRecord(
                id=job.id,
                agent=job.specialist_role,
                started_at=job.started_at or job.created_at,
                origin=self._origin_from_job(job),
            )
            for job in self._job_registry.all()
            if job.execution_state is ExecutionState.ORPHANED
        ]

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        return None if value is None else str(value)

    @staticmethod
    def _origin_from_job(job: VoiceJob) -> dict[str, Any]:
        origin = {
            "role": job.creating_role,
            "channel": job.creator_peer,
            "chat_id": job.scope_id,
            "cid": job.origin_route_id or "",
            "device_id": job.origin_device_id or "",
            "user_id": job.creator_user_id,
            "user_text": job.task,
        }
        # #485: restore scheduled-delivery eligibility from the durable FIELD,
        # never by inferring it from the scope label's shape. Added only when
        # actually stored, so an unmarked job's origin stays byte-identical to
        # what it was before this field existed.
        if job.scheduled_delivery is True:
            origin["_scheduled_delivery"] = True
        return origin


# ---------------------------------------------------------------------------
# Installed-specialist data model (Task 13) — a SEPARATE concern layered onto
# the legacy SpecialistRegistry above (bundled /config/agents/specialists/
# per-agent-directory tier-2 loading + in-flight delegation tracking). The
# NEW tree this introduces, /config/specialists/<slug>/{active,desired}.yaml,
# is a DIFFERENT directory from SpecialistRegistry._dir's legacy tree — do
# not conflate them. This is DATA MODEL ONLY (spec Plan 1): no fetch/
# consent/CAS-persist/compile runtime — that is Plan 2's N1.
# ---------------------------------------------------------------------------


def _discover_image_role_slots(roles_dir: str | None = None) -> frozenset[str]:
    """Spec §2.4: the slug-collision authority is EVERY image role's bare slot,
    across ALL THREE kinds (resident, executor, AND specialist) — never a
    hand-maintained per-kind constant (the bug this replaces: a resident+executor
    -only hard-coded set silently omitted the bundled specialist:finance, so an
    install with slug 'finance' would have collided undetected). Scans
    defaults/roles/<kind>/<slug>/role.yaml for every kind directory PRESENT under
    roles_dir — no kind is special-cased, so a future fourth kind, a renamed
    executor, or a newly-bundled transitional specialist needs no matching edit
    here. Lazy-imports agent_loader.DEFAULT_ROLES_DIR (mirrors this module's
    existing local-import convention for agent_loader, see load() above) to avoid
    a module-level circular import."""
    from agent_loader import DEFAULT_ROLES_DIR

    base = Path(roles_dir or DEFAULT_ROLES_DIR)
    slots: set[str] = set()
    for kind_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        for role_dir in sorted(p for p in kind_dir.iterdir() if p.is_dir()):
            role_yaml = role_dir / "role.yaml"
            if not role_yaml.is_file():
                continue
            data = yaml.safe_load(role_yaml.read_text(encoding="utf-8"))
            slots.add(str(data["slot"]))
    return frozenset(slots)


# Computed once at import — the image's OWN role tree is static content, never
# mutated at runtime (an INSTALLED specialist lives in a separate tree,
# /config/specialists/, layered on top by InstalledSpecialistIndex below).
_IMAGE_ROLE_SLOTS = _discover_image_role_slots()


class InstalledSpecialistIndex:
    """Tracks 0..N INSTALLED specialist components under /config/specialists/<slug>/ —
    a DIFFERENT tree from SpecialistRegistry._dir's legacy bundled
    /config/agents/specialists/<role>/ (finance today). Populated at boot by
    scanning for active.yaml/desired.yaml pairs; Plan 2's N1 is what actually
    WRITES a new one via InstanceDir.stage_desired/commit_desired_to_active."""

    def __init__(self, specialists_dir: str = "/config/specialists") -> None:
        self._dir = Path(specialists_dir)
        self._instances: dict[str, SpecialistInstance] = {}

    def installed_slugs(self) -> frozenset[str]:
        return frozenset(self._instances)

    def all_collision_slugs(self) -> frozenset[str]:
        return _IMAGE_ROLE_SLOTS | self.installed_slugs()

    def get_instance(self, slug: str) -> SpecialistInstance | None:
        return self._instances.get(slug)

    def load(self) -> None:
        """A slug directory with only a desired.yaml (no active.yaml) is a
        brand-new specialist still in pending-configuration with NO running
        active tuple (spec §4.1) — this Plan defines that state; Plan 2's N1
        is what produces one.

        Round-5 fix (F3 race-safety): builds the new instance map in a LOCAL
        dict and swaps `self._instances` to it in a single (GIL-atomic)
        attribute store, rather than `clear()`-ing then repopulating in place.
        `current_specialist_roles_dir` now RE-LOADS the already-published
        process-wide index in-lock (in a worker thread); a concurrent
        admin/inspection reader (`installed_slugs()` -> `frozenset(self.
        _instances)`) on the event loop must never observe a half-cleared or
        mid-repopulation dict ('dict changed size during iteration'). The swap
        makes every reader see either the complete OLD map or the complete NEW
        one, never a transient in-between."""
        instances: dict[str, SpecialistInstance] = {}
        if self._dir.is_dir():
            for entry in sorted(self._dir.iterdir()):
                if not entry.is_dir() or entry.name in {"store", ".staging"}:
                    continue
                slug = entry.name
                instance_dir = InstanceDir(entry)
                # #346 + #372 (D7): active and desired are loaded
                # INDEPENDENTLY. A damaged/tombstoned ACTIVE isolates the slug
                # as state="error" (keeping it in the index reserves the slug —
                # a fresh install cannot silently overwrite a damaged one, and
                # the error is surfaced to admin/inspection paths). A damaged
                # desired alongside a HEALTHY active must not take the running
                # generation out of the fleet: the instance stays active and
                # the desired failure is surfaced as diagnostic state.
                _tuple_errors = (ValueError, OSError, yaml.YAMLError,
                                 jsonschema.ValidationError)
                desired_error: str | None = None
                try:
                    active = instance_dir.active()
                except _tuple_errors as exc:
                    logger.error(
                        "installed specialist %r: unreadable active tuple "
                        "(%s); isolated as state=error", slug, exc,
                    )
                    instances[slug] = SpecialistInstance(
                        slug=slug, stable_agent_id=f"specialist:{slug}",
                        state="error", active=None, desired=None,
                        last_activation_error=str(exc),
                    )
                    continue
                try:
                    desired = instance_dir.desired()
                except _tuple_errors as exc:
                    desired = None
                    desired_error = str(exc)
                    if active is None:
                        # No healthy generation at all: same isolation as a
                        # damaged active.
                        logger.error(
                            "installed specialist %r: unreadable desired tuple "
                            "with no active (%s); isolated as state=error",
                            slug, exc,
                        )
                        instances[slug] = SpecialistInstance(
                            slug=slug, stable_agent_id=f"specialist:{slug}",
                            state="error", active=None, desired=None,
                            last_activation_error=desired_error,
                        )
                        continue
                    logger.warning(
                        "installed specialist %r: unreadable desired tuple "
                        "(%s); the healthy active generation stays loaded",
                        slug, exc,
                    )
                if active is None and desired is None:
                    continue
                # Two states only: the both-None case `continue`d above, so
                # there is no third arm to reach here. `InstanceState` keeps its
                # "error" member — a FAILED upgrade constructs one directly
                # (specialist_install.py), and that is a transient result state,
                # not something this directory scan can reconstruct from disk.
                state: InstanceState = (
                    "active" if active is not None else "pending-configuration"
                )
                instances[slug] = SpecialistInstance(
                    slug=slug, stable_agent_id=f"specialist:{slug}", state=state,
                    active=active, desired=desired,
                    last_activation_error=desired_error,
                )
        self._instances = instances

    def installed_component_role_dirs(self) -> "dict[str, Path]":
        """slug -> the CAS directory HOLDING role/{role.yaml,doctrine.md}
        (i.e. the component root, not the role/ subdir itself —
        specialist_materialize._copy_role_dir appends 'role' itself,
        mirroring reconcile_specialist_roles_overlay's other branch which
        also receives a component root and descends into 'role').

        Plan 2, Task N1b Step 17: an active tuple is authoritative; a
        pending-configuration slug (desired only, no active) still resolves
        so its role artifact is visible for inspection/upgrade paths —
        `_reconcile_specialist_operational_files` (specialist_materialize.py)
        is the SEPARATE gate that keeps a pending-configuration slug
        non-loadable regardless of this overlay entry existing."""
        from specialist_install import parse_component_root

        out: dict[str, Path] = {}
        for slug, instance in self._instances.items():
            tuple_ = instance.active or instance.desired
            if tuple_ is None:
                continue
            try:
                _, _, checksum = parse_component_root(tuple_.root)
            except ValueError:
                continue
            out[slug] = self._dir / "store" / checksum.removeprefix("sha256:")
        return out


# Module-level accessor over the ONE process-wide index casa_core.py constructs at
# boot — mirrors the established module-level pattern (e.g. tools.active_semantic_memory,
# tools.py:3549) other registries in this codebase already use for tool-module access
# without threading a runtime object through every call. Plan 2's N1 and Task 14's
# admin handlers read through this seam.
_active_index: "InstalledSpecialistIndex | None" = None


def set_active_installed_index(index: "InstalledSpecialistIndex") -> None:
    global _active_index
    _active_index = index


def live_installed_specialist_slugs() -> frozenset[str]:
    return _active_index.installed_slugs() if _active_index is not None else frozenset()


def live_collision_slugs() -> frozenset[str]:
    if _active_index is None:
        return _IMAGE_ROLE_SLOTS
    return _active_index.all_collision_slugs()


def get_installed_instance(slug: str) -> "SpecialistInstance | None":
    """Task 14: thin module-level wrapper over the process-wide
    ``_active_index``, so admin/inspection code (``personality_admin_handlers``)
    can look up a specialist's lifecycle state without threading the index
    through every call site — same seam as ``live_installed_specialist_slugs``."""
    return _active_index.get_instance(slug) if _active_index is not None else None
