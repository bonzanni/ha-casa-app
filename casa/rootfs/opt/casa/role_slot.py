from __future__ import annotations

import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from canonical_bytes import canonical_json_bytes, canonical_text, checksum_bytes, checksum_json, to_plain_json
from config import resolve_model
from role_artifact import RoleArtifactSource

FIXED_RESIDENT_SLOTS: tuple[str, ...] = ("assistant", "butler", "concierge")
_KINDS = ("resident", "specialist", "executor")
# #1353: a fixed model is a shortname, or one pinned full id: the configurator
# and the plugin-developer stay on Sonnet 5 while ``sonnet`` means Sonnet 5.5.
_MODELS = ("opus", "sonnet", "haiku", "claude-sonnet-5")


def compute_effective_config_digest(config: Mapping[str, object]) -> str:
    """Canonical digest of an agent's resolved NON-SECRET configuration (spec §2.3).
    Callers must strip every secret value before calling this — secrets never enter
    any digest. Defined HERE (not in Task 7's personality_binding.py — see this
    task's Interfaces note) because THIS task's own executor wiring (Step 8) needs
    it, and personality_binding.py does not exist until Task 7 runs. Residents and
    executors in this plan have no per-instance configuration, so every
    resident/executor identity uses EMPTY_CONFIG_DIGEST; specialists get a real one
    once Plan 2's N1 exists. Task 7's personality_binding.py imports this function
    (and EMPTY_CONFIG_DIGEST below) rather than redefining them."""
    return checksum_json(dict(config))


EMPTY_CONFIG_DIGEST = compute_effective_config_digest({})


class RoleValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    source: str
    effective: str
    sdk_model: str
    option: str | None


@dataclass(frozen=True, slots=True)
class RoleSlot:
    role_id: str
    kind: str
    slot: str
    mission: str
    resolved_model: ResolvedModel
    normalized: Mapping[str, object]
    doctrine: str
    checksum: str


@dataclass(frozen=True, slots=True)
class ExecutorIdentity:
    """Spec §2.3: an executor's identity is role-only — no persona, no binding."""
    stable_agent_id: str
    role_checksum: str
    effective_config_digest: str


def _ha_model_options(env: Mapping[str, str] = os.environ) -> Mapping[str, str]:
    def value(name: str, default: str) -> str:
        raw = env.get(name)
        return raw.strip() if isinstance(raw, str) and raw.strip() else default

    return MappingProxyType({
        "primary_agent_model": value("PRIMARY_AGENT_MODEL", "opus"),
        "voice_agent_model": value("VOICE_AGENT_MODEL", "haiku"),
    })


def resolve_role_model(
    model: Mapping[str, object], options: Mapping[str, str],
) -> ResolvedModel:
    source = model.get("source")
    if source == "fixed":
        if set(model) != {"source", "value"}:
            raise RoleValidationError("fixed model accepts only source and value")
        effective = str(model["value"])
        if effective not in _MODELS:
            raise RoleValidationError("fixed model value is not a known model")
        option = None
    elif source == "ha_option":
        if set(model) != {"source", "option", "default", "allowed"}:
            raise RoleValidationError("ha_option model shape is invalid")
        option = str(model["option"])
        effective = options.get(option) or str(model["default"])
        if effective not in model["allowed"]:
            raise RoleValidationError("resolved model is not in the role's allowed list")
    else:
        raise RoleValidationError("model source must be fixed or ha_option")
    return ResolvedModel(
        source=str(source), effective=effective,
        sdk_model=resolve_model(effective), option=option,
    )


def validate_role_shape(role: Mapping[str, object]) -> None:
    kind = role.get("kind")
    slot = role.get("slot")
    role_id = role.get("id")
    if kind not in _KINDS:
        raise RoleValidationError("kind is required and must be resident/specialist/executor")
    if not isinstance(slot, str) or not slot:
        raise RoleValidationError("slot is required")
    if role_id != f"{kind}:{slot}":
        raise RoleValidationError(f"role id {role_id!r} must equal exactly '{kind}:{slot}'")
    if kind == "resident" and slot not in FIXED_RESIDENT_SLOTS:
        raise RoleValidationError(
            f"resident slot {slot!r} is not one of the fixed resident slots "
            f"{FIXED_RESIDENT_SLOTS}"
        )
    channels = role.get("channels", [])
    session = role.get("session", {})
    persona_policy = (role.get("persona") or {}).get("policy")
    if kind == "resident" and not channels:
        raise RoleValidationError("resident requires an image-declared channel")
    if kind == "specialist":
        if channels or session.get("strategy") != "ephemeral":
            raise RoleValidationError("specialist must be channel-free and ephemeral")
        if role.get("triggers") or role.get("executors"):
            raise RoleValidationError("specialist cannot own triggers or executors")
    if kind == "executor" and persona_policy != "forbidden":
        raise RoleValidationError("executor persona policy must be forbidden")
    if kind != "executor" and persona_policy not in {"required", "optional-but-bound"}:
        raise RoleValidationError("persona-bearing role requires a binding-capable policy")


def normalize_role_for_checksum(
    role: Mapping[str, object], resolved: ResolvedModel,
) -> dict[str, object]:
    """The ONE canonical role representation hashed into the role checksum (spec §2.3:
    'canonical role.yaml after resolving model.source to a concrete model value').

    CORRECTED (defect #2 from the prior draft's review): every key of ``role`` survives
    byte-for-byte, INCLUDING the original structured ``model`` block (``value`` for
    ``fixed``; ``option``/``default``/``allowed`` for ``ha_option``) — so a change to the
    role's model POLICY (e.g. narrowing ``allowed``) always moves the checksum even when
    the currently resolved model doesn't move. The resolution result is carried as an
    ADDITIONAL sibling key (``model_resolved``), never a replacement of ``model`` — so the
    concrete model actually running is ALSO checksum-significant (an HA-option flip with
    no role.yaml edit still forces a new checksum and thus a new session epoch)."""
    # NOTE (foundation-hardening reconciliation): ``role`` here is
    # ``RoleArtifactSource.role``, which the hardened loader returns as
    # ``deep_freeze(raw)`` — every nested field is a ``MappingProxyType``/``tuple``.
    # ``dict(role)`` would be only a SHALLOW copy (nested stays frozen). Use
    # ``to_plain_json`` (canonical_bytes) to deep-unfreeze to plain dict/list, so
    # ``normalized`` is provably plain for both the checksum and any downstream
    # consumer. (``canonical_json_bytes`` also tolerates frozen input now, but
    # keeping ``normalized`` plain is clearer and avoids re-freezing surprises.)
    normalized = to_plain_json(role)  # -> plain dict, recursively unfrozen
    normalized["model_resolved"] = {"effective": resolved.effective, "sdk_model": resolved.sdk_model}
    return normalized


def compute_role_checksum(*, normalized_role: Mapping[str, object], doctrine: str) -> str:
    """Normative role checksum (spec §2.3): sha256 over canonical_json_bytes(role.yaml)
    length-prefixed and concatenated with canonical_text(doctrine.md) bytes."""
    role_bytes = canonical_json_bytes(normalized_role)
    doctrine_bytes = canonical_text(doctrine).encode("utf-8")
    framed = len(role_bytes).to_bytes(8, "big") + role_bytes + doctrine_bytes
    return checksum_bytes(framed)


def materialize_role(*, source: RoleArtifactSource, options: Mapping[str, str]) -> RoleSlot:
    role = source.role
    doctrine = source.doctrine
    validate_role_shape(role)
    resolved = resolve_role_model(role["model"], options)
    normalized = normalize_role_for_checksum(role, resolved)
    checksum = compute_role_checksum(normalized_role=normalized, doctrine=doctrine)
    return RoleSlot(
        role_id=str(role["id"]), kind=str(role["kind"]), slot=str(role["slot"]),
        mission=str(role["mission"]), resolved_model=resolved,
        normalized=MappingProxyType(normalized), doctrine=doctrine, checksum=checksum,
    )


def compute_executor_identity(
    *, role: RoleSlot, effective_config_digest: str,
) -> ExecutorIdentity:
    """Spec §2.3's role-only identity triple. Plan 1 executors carry no
    operator-configurable non-secret settings, so callers pass this module's own
    ``EMPTY_CONFIG_DIGEST`` (defined above) today — the seam is real (a future
    executor config feature substitutes a real digest here without any interface
    change), not a stub that must be revisited."""
    return ExecutorIdentity(
        stable_agent_id=role.role_id, role_checksum=role.checksum,
        effective_config_digest=effective_config_digest,
    )
