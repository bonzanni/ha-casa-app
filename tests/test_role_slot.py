import pytest

from canonical_bytes import canonical_json_bytes, canonical_text, checksum_bytes

from role_slot import (
    FIXED_RESIDENT_SLOTS,
    ResolvedModel,
    RoleValidationError,
    compute_role_checksum,
    materialize_role,
    normalize_role_for_checksum,
    resolve_role_model,
    validate_role_shape,
)


def _role(**overrides) -> dict:
    base = {
        "api_version": "casa.role/v1",
        "id": "resident:butler",
        "kind": "resident",
        "slot": "butler",
        "mission": "Control and report on the household through Home Assistant.",
        "enabled": True,
        "model": {"source": "ha_option", "option": "voice_agent_model",
                   "default": "haiku", "allowed": ["opus", "sonnet", "haiku"]},
        "tools": {"allowed": [], "disallowed": [], "permission_mode": "acceptEdits",
                   "max_turns": 10, "skills": "none", "voice_guard": "none"},
        "mcp_servers": [],
        "channels": ["ha_voice"],
        "memory": {"token_budget": 800, "read_strategy": "cached"},
        "session": {"strategy": "pooled", "idle_timeout_seconds": 300},
        "disclosure": {"policy": "standard", "overrides": {}},
        "delegates": [], "executors": [], "triggers": [],
        "hooks": {"pre_tool_use": []},
        "tts": {"tag_dialect": "square_brackets", "error_phrases": {}},
        "response": {
            "text": {"register": "conversational"},
            "voice": {"register": "spoken"},
            "restricted_webhook": {"register": "plain"},
        },
        "persona": {"policy": "required", "compatibility": ["casa/tina@>=0.1.0 <1.0.0"]},
        "requires": {"plugins": [], "tools": []},
        "doctrine_file": "doctrine.md",
    }
    base.update(overrides)
    return base


def test_kind_is_required_and_id_prefix_must_match() -> None:
    with pytest.raises(RoleValidationError, match="id"):
        validate_role_shape(_role(kind="specialist"))


def test_id_slot_suffix_must_match_slot_field_exactly() -> None:
    with pytest.raises(RoleValidationError, match="id"):
        validate_role_shape(_role(id="resident:butler-x"))


def test_resident_slot_must_be_one_of_the_three_fixed_slots() -> None:
    assert FIXED_RESIDENT_SLOTS == ("assistant", "butler", "concierge")
    with pytest.raises(RoleValidationError, match="fixed resident"):
        validate_role_shape(_role(id="resident:steward", slot="steward"))


def test_executor_persona_policy_must_be_forbidden() -> None:
    executor = _role(
        id="executor:configurator", kind="executor", slot="configurator",
        channels=[], session={"strategy": "ephemeral", "idle_timeout_seconds": 0},
        persona={"policy": "forbidden"}, delegates=[], executors=[], triggers=[],
    )
    validate_role_shape(executor)  # does not raise
    with pytest.raises(RoleValidationError, match="persona"):
        validate_role_shape({**executor, "persona": {"policy": "required",
                                                        "compatibility": ["casa/x@0.1.0"]}})


def test_specialist_with_channels_or_non_ephemeral_session_fails() -> None:
    with pytest.raises(RoleValidationError):
        validate_role_shape(_role(
            id="specialist:finance", kind="specialist", slot="finance",
            channels=["telegram"], session={"strategy": "ephemeral", "idle_timeout_seconds": 0},
        ))
    with pytest.raises(RoleValidationError):
        validate_role_shape(_role(
            id="specialist:finance", kind="specialist", slot="finance",
            channels=[], session={"strategy": "persistent", "idle_timeout_seconds": 0},
        ))


def test_ha_option_model_is_resolved_before_materialization() -> None:
    value = resolve_role_model(
        {"source": "ha_option", "option": "voice_agent_model",
         "default": "haiku", "allowed": ["opus", "sonnet", "haiku"]},
        {"voice_agent_model": "sonnet"},
    )
    assert value.effective == "sonnet"
    assert value.option == "voice_agent_model"


def test_fixed_model_rejects_out_of_enum_value() -> None:
    with pytest.raises(RoleValidationError):
        resolve_role_model({"source": "fixed", "value": "gpt-4"}, {})


# --- The corrected checksum: defect #2 regression coverage ------------------


def test_normalized_role_retains_every_original_field_including_model_policy() -> None:
    role = _role()
    resolved = resolve_role_model(role["model"], {"voice_agent_model": "haiku"})
    normalized = normalize_role_for_checksum(role, resolved)
    for key, value in role.items():
        if key == "model":
            assert normalized["model"] == role["model"]  # verbatim, untouched
        else:
            assert normalized[key] == value
    assert normalized["model_resolved"] == {"effective": "haiku", "sdk_model": resolved.sdk_model}


def test_checksum_moves_when_model_allowed_list_narrows_even_if_effective_is_unchanged() -> None:
    role = _role()
    resolved = resolve_role_model(role["model"], {"voice_agent_model": "haiku"})
    baseline = compute_role_checksum(
        normalized_role=normalize_role_for_checksum(role, resolved), doctrine="Doctrine.\n",
    )
    narrowed_role = _role(model={**role["model"], "allowed": ["opus", "haiku"]})
    narrowed_resolved = resolve_role_model(
        narrowed_role["model"], {"voice_agent_model": "haiku"},
    )
    narrowed = compute_role_checksum(
        normalized_role=normalize_role_for_checksum(narrowed_role, narrowed_resolved),
        doctrine="Doctrine.\n",
    )
    assert narrowed_resolved.effective == resolved.effective == "haiku"  # same running model
    assert narrowed != baseline  # policy change is still checksum-significant


def test_checksum_moves_when_resolution_changes_even_if_role_yaml_is_unchanged() -> None:
    role = _role()
    haiku = compute_role_checksum(
        normalized_role=normalize_role_for_checksum(
            role, resolve_role_model(role["model"], {"voice_agent_model": "haiku"}),
        ),
        doctrine="Doctrine.\n",
    )
    sonnet = compute_role_checksum(
        normalized_role=normalize_role_for_checksum(
            role, resolve_role_model(role["model"], {"voice_agent_model": "sonnet"}),
        ),
        doctrine="Doctrine.\n",
    )
    assert haiku != sonnet  # an HA-option flip is checksum-significant too


def test_checksum_is_a_valid_sha256_digest_string() -> None:
    role = _role()
    resolved = resolve_role_model(role["model"], {"voice_agent_model": "haiku"})
    value = compute_role_checksum(
        normalized_role=normalize_role_for_checksum(role, resolved), doctrine="Doctrine.\n",
    )
    assert value.startswith("sha256:") and len(value) == 71


def test_checksum_matches_manual_length_prefixed_framing() -> None:
    role = _role()
    resolved = resolve_role_model(role["model"], {"voice_agent_model": "haiku"})
    normalized = normalize_role_for_checksum(role, resolved)
    role_bytes = canonical_json_bytes(normalized)
    doctrine_bytes = canonical_text("Doctrine.\n").encode("utf-8")
    expected = checksum_bytes(len(role_bytes).to_bytes(8, "big") + role_bytes + doctrine_bytes)
    assert compute_role_checksum(normalized_role=normalized, doctrine="Doctrine.\n") == expected


# --- Foundation-hardening regression: materialize_role over a REAL,
# deep-frozen RoleArtifactSource (loaded through load_role_artifact, which
# deep-freezes role.yaml's nested content into MappingProxyType/tuple) must
# succeed end-to-end with no CanonicalizationError. Step-1's hand-made dicts
# above are always plain dict/list and never exercise this path — this is
# the exact regression the foundation-hardening reconciliation fixed
# (normalize_role_for_checksum using to_plain_json instead of dict(role)).
# ----------------------------------------------------------------------------


def test_materialize_role_succeeds_on_a_real_deep_frozen_role_artifact() -> None:
    from pathlib import Path

    from role_artifact import load_role_artifact

    real_roles_dir = (
        Path(__file__).resolve().parent.parent
        / "casa/rootfs/opt/casa/defaults/roles"
    )
    source = load_role_artifact(real_roles_dir / "resident" / "butler")

    # Confirm the fixture actually IS deep-frozen (guards against a future
    # loader change silently making this test meaningless).
    from types import MappingProxyType
    assert isinstance(source.role, MappingProxyType)
    assert isinstance(source.role["model"], MappingProxyType)
    assert isinstance(source.role["channels"], tuple)

    role_slot = materialize_role(
        source=source, options={"voice_agent_model": "haiku"},
    )

    assert role_slot.role_id == "resident:butler"
    assert role_slot.kind == "resident"
    assert role_slot.checksum.startswith("sha256:")
    # The normalized representation must be plain (JSON-native), not frozen.
    assert isinstance(role_slot.normalized["model"], dict)
    assert isinstance(role_slot.normalized["channels"], list)
