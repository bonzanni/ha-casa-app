"""Tests for the immutable speaker-provenance contract.

Covers the provenance mapping round trip and its validation, the frozen
`SpeakerProvenance` snapshot type, and the
`UserProvenance.from_origin` factory contract: an anonymous voice speaker
never acquires a name, and an authenticated transport
identity is the ONLY name source (no caller-supplied display name can be
injected through the factory).
"""

from __future__ import annotations

import inspect
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import jsonschema
import pytest

from speaker_provenance import (
    UserProvenance,
    _FIELD_LIMITS,
    provenance_from_mapping,
    provenance_mapping,
    validate_speaker_provenance,
)
from personality_types import (
    AuthenticatedUser,
    SpeakerProvenance,
    TrustedOrigin,
)

_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "casa" / "rootfs" / "opt" / "casa" / "defaults" / "schema"
    / "speaker-provenance.v1.json"
)


def _raw_mapping(**overrides: object) -> dict[str, object]:
    """A schema-shaped v1 payload dict with every field defaulted to None,
    overridden by kwargs. Used to build malformed mappings directly
    (bypassing `provenance_mapping`, which validates)."""
    base: dict[str, object] = {
        "speaker_kind": "user",
        "role_id": None,
        "persona_id": None,
        "persona_version": None,
        "display_name": None,
        "binding_digest": None,
        "user_peer": None,
        "user_id": None,
    }
    base.update(overrides)
    return base


def resident() -> SpeakerProvenance:
    return SpeakerProvenance(
        speaker_kind="resident",
        role_id="resident:butler",
        persona_id="casa.personas/tina",
        persona_version="1.0.0",
        display_name="Tina",
        binding_digest="sha256:" + "5" * 64,
        user_peer=None,
        user_id=None,
    )


def specialist() -> SpeakerProvenance:
    return SpeakerProvenance(
        speaker_kind="specialist",
        role_id="specialist:gary",
        persona_id="casa.personas/gary",
        persona_version="2.3.1",
        display_name="Gary",
        binding_digest="sha256:" + "a" * 64,
        user_peer=None,
        user_id=None,
    )


def executor() -> SpeakerProvenance:
    return SpeakerProvenance(
        speaker_kind="executor",
        role_id="executor:home",
    )


def user() -> SpeakerProvenance:
    return SpeakerProvenance(speaker_kind="user", user_peer="telegram_123")


def system() -> SpeakerProvenance:
    return SpeakerProvenance(speaker_kind="system")


# ---------------------------------------------------------------------------
# Frozen snapshot
# ---------------------------------------------------------------------------


def test_snapshot_is_frozen() -> None:
    value = resident()
    with pytest.raises(FrozenInstanceError):
        value.display_name = "Changed"


# ---------------------------------------------------------------------------
# All speaker kinds round-trip through the mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [resident(), specialist(), executor(), user(), system()],
    ids=["resident", "specialist", "executor", "user", "system"],
)
def test_all_speaker_kinds_round_trip(value: SpeakerProvenance) -> None:
    assert provenance_from_mapping(provenance_mapping(value)) == value


# ---------------------------------------------------------------------------
# Maximum-length and worst-case 4-byte Unicode round trips
# ---------------------------------------------------------------------------

_POO = "\U0001f4a9"  # U+1F4A9, worst-case 4-byte UTF-8 scalar


def _fill(scalar_limit: int, byte_limit: int, unicode_char: str | None = None) -> str:
    """Build the longest string that still satisfies both a scalar-count and
    a UTF-8-byte-count ceiling for the given char set."""
    if unicode_char is None:
        return "a" * scalar_limit
    char_bytes = len(unicode_char.encode("utf-8"))
    max_by_scalar = scalar_limit
    max_by_bytes = byte_limit // char_bytes
    count = min(max_by_scalar, max_by_bytes)
    return unicode_char * count


def test_max_length_user_round_trip() -> None:
    scalar, byte = _FIELD_LIMITS["user_peer"]
    scalar_id, byte_id = _FIELD_LIMITS["user_id"]
    value = SpeakerProvenance(
        speaker_kind="user",
        user_peer=_fill(scalar, byte),
        user_id=_fill(scalar_id, byte_id),
    )
    validate_speaker_provenance(value)
    assert provenance_from_mapping(provenance_mapping(value)) == value


def test_max_length_agent_round_trip() -> None:
    scalar_persona, byte_persona = _FIELD_LIMITS["persona_id"]
    scalar_ver, byte_ver = _FIELD_LIMITS["persona_version"]
    scalar_name, byte_name = _FIELD_LIMITS["display_name"]
    # role_id/persona_id/persona_version must still match their regexes, so
    # build max-length values that are also structurally valid rather than
    # pure filler. role_id's real ceiling is now the 32-byte-slug bound in
    # _ROLE_RE (``{0,31}`` after the mandatory first char), which is far
    # tighter than the 128-byte _FIELD_LIMITS scalar limit — so the
    # max-length role_id below is bounded by the regex, not the field limit.
    role_id = "resident:" + "b" * 32
    persona_id = "a/" + "b" * (scalar_persona - 2)
    persona_version = "0.0." + "9" * (scalar_ver - 4)
    display_name = _fill(scalar_name, byte_name)
    value = SpeakerProvenance(
        speaker_kind="resident",
        role_id=role_id,
        persona_id=persona_id,
        persona_version=persona_version,
        display_name=display_name,
        binding_digest="sha256:" + "5" * 64,
    )
    validate_speaker_provenance(value)
    assert provenance_from_mapping(provenance_mapping(value)) == value


def test_worst_case_four_byte_unicode_user_round_trip() -> None:
    scalar, byte = _FIELD_LIMITS["user_peer"]
    value = SpeakerProvenance(
        speaker_kind="user",
        user_peer=_fill(scalar, byte, _POO),
    )
    validate_speaker_provenance(value)
    assert provenance_from_mapping(provenance_mapping(value)) == value


def test_worst_case_four_byte_unicode_agent_display_name_round_trip() -> None:
    scalar_name, byte_name = _FIELD_LIMITS["display_name"]
    display_name = _fill(scalar_name, byte_name, _POO)
    value = SpeakerProvenance(
        speaker_kind="resident",
        role_id="resident:butler",
        persona_id="casa.personas/tina",
        persona_version="1.0.0",
        display_name=display_name,
        binding_digest="sha256:" + "5" * 64,
    )
    validate_speaker_provenance(value)
    assert provenance_from_mapping(provenance_mapping(value)) == value


# ---------------------------------------------------------------------------
# One-byte-over-limit field failures for every _FIELD_LIMITS field
# ---------------------------------------------------------------------------


def test_role_id_one_byte_over_scalar_limit_fails() -> None:
    scalar, _ = _FIELD_LIMITS["role_id"]
    role_id = "resident:" + "b" * (scalar - len("resident:") + 1)
    value = SpeakerProvenance(
        speaker_kind="resident",
        role_id=role_id,
        persona_id="casa.personas/tina",
        persona_version="1.0.0",
        binding_digest="sha256:" + "5" * 64,
    )
    with pytest.raises(ValueError):
        validate_speaker_provenance(value)


def test_role_id_slug_at_32_bytes_accepted() -> None:
    """_ROLE_RE bounds the slug portion (after ``kind:``) to 32 bytes
    (``[a-z0-9][a-z0-9-]{0,31}``) — the same bound `specialist_component
    ._SLUG_RE` and the role/binding/speaker-provenance v1 schemas enforce.
    A 32-byte slug is the boundary and must still validate."""
    role_id = "specialist:" + "b" * 32
    value = SpeakerProvenance(
        speaker_kind="specialist",
        role_id=role_id,
        persona_id="casa.personas/gary",
        persona_version="1.0.0",
        binding_digest="sha256:" + "5" * 64,
    )
    validate_speaker_provenance(value)


def test_role_id_slug_over_32_bytes_rejected() -> None:
    """One byte past the _ROLE_RE slug bound must be rejected even though
    it is well within the 128-byte role_id _FIELD_LIMITS scalar limit."""
    role_id = "specialist:" + "b" * 33
    value = SpeakerProvenance(
        speaker_kind="specialist",
        role_id=role_id,
        persona_id="casa.personas/gary",
        persona_version="1.0.0",
        binding_digest="sha256:" + "5" * 64,
    )
    with pytest.raises(ValueError):
        validate_speaker_provenance(value)


def test_persona_id_one_byte_over_scalar_limit_fails() -> None:
    scalar, _ = _FIELD_LIMITS["persona_id"]
    persona_id = "a/" + "b" * (scalar - 2 + 1)
    value = SpeakerProvenance(
        speaker_kind="resident",
        role_id="resident:butler",
        persona_id=persona_id,
        persona_version="1.0.0",
        binding_digest="sha256:" + "5" * 64,
    )
    with pytest.raises(ValueError):
        validate_speaker_provenance(value)


def test_persona_version_one_byte_over_scalar_limit_fails() -> None:
    scalar, _ = _FIELD_LIMITS["persona_version"]
    persona_version = "0.0." + "9" * (scalar - 4 + 1)
    value = SpeakerProvenance(
        speaker_kind="resident",
        role_id="resident:butler",
        persona_id="casa.personas/tina",
        persona_version=persona_version,
        binding_digest="sha256:" + "5" * 64,
    )
    with pytest.raises(ValueError):
        validate_speaker_provenance(value)


def test_display_name_one_byte_over_scalar_limit_fails() -> None:
    scalar, _ = _FIELD_LIMITS["display_name"]
    value = SpeakerProvenance(
        speaker_kind="resident",
        role_id="resident:butler",
        persona_id="casa.personas/tina",
        persona_version="1.0.0",
        display_name="a" * (scalar + 1),
        binding_digest="sha256:" + "5" * 64,
    )
    with pytest.raises(ValueError):
        validate_speaker_provenance(value)


# display_name has no distinguishable "byte-limit-only" failure: its
# byte/scalar ratio is exactly 320/80 = 4, the maximum possible UTF-8 bytes
# per scalar. Any string within the 80-scalar cap therefore has at most
# 80*4=320 bytes, so it is structurally impossible to exceed the byte
# ceiling while staying under the scalar ceiling for this field — the two
# checks are coincident at the boundary. The scalar test above (which uses
# 4-byte filler in the worst-case-Unicode round trip, and ASCII filler here)
# already exercises this field's one-over failure fully.


def test_user_peer_one_byte_over_scalar_limit_fails() -> None:
    scalar, _ = _FIELD_LIMITS["user_peer"]
    value = SpeakerProvenance(speaker_kind="user", user_peer="a" * (scalar + 1))
    with pytest.raises(ValueError):
        validate_speaker_provenance(value)


def test_user_id_one_byte_over_scalar_limit_fails() -> None:
    scalar, _ = _FIELD_LIMITS["user_id"]
    value = SpeakerProvenance(
        speaker_kind="user",
        user_peer="telegram_123",
        user_id="a" * (scalar + 1),
    )
    with pytest.raises(ValueError):
        validate_speaker_provenance(value)


def test_user_peer_one_byte_over_byte_limit_but_under_scalar_limit_fails() -> None:
    """user_peer's ratio (512/256 = 2 bytes/scalar) is looser than its max
    4-byte-per-char width, so a byte-limit-only violation — one byte over
    512 while the scalar count stays under 256 — IS constructible using a
    4-byte filler char, unlike display_name above."""
    scalar_limit, byte_limit = _FIELD_LIMITS["user_peer"]
    count = (byte_limit // 4) + 1  # 4-byte char => bytes = count * 4
    assert count <= scalar_limit, "must stay under the scalar cap"
    user_peer = _POO * count
    assert len(user_peer) < scalar_limit
    assert len(user_peer.encode("utf-8")) > byte_limit
    value = SpeakerProvenance(speaker_kind="user", user_peer=user_peer)
    with pytest.raises(ValueError):
        validate_speaker_provenance(value)


def test_user_id_one_byte_over_byte_limit_but_under_scalar_limit_fails() -> None:
    scalar_limit, byte_limit = _FIELD_LIMITS["user_id"]
    count = (byte_limit // 4) + 1
    assert count <= scalar_limit, "must stay under the scalar cap"
    user_id = _POO * count
    assert len(user_id) < scalar_limit
    assert len(user_id.encode("utf-8")) > byte_limit
    value = SpeakerProvenance(speaker_kind="user", user_peer="telegram_123", user_id=user_id)
    with pytest.raises(ValueError):
        validate_speaker_provenance(value)


# ---------------------------------------------------------------------------
# UserProvenance.from_origin factory contract
# ---------------------------------------------------------------------------


def test_anonymous_voice_never_acquires_a_name() -> None:
    value = UserProvenance.from_origin(
        surface="voice",
        server_origin=TrustedOrigin(
            route="voice", is_authenticated=False, clearance="friends"
        ),
        authenticated_user=None,
        user_peer="voice_speaker",
    )
    assert value.user_peer == "voice_speaker"
    assert value.user_id is None
    assert value.display_name is None


def test_authenticated_transport_identity_is_the_only_name_source() -> None:
    value = UserProvenance.from_origin(
        surface="telegram",
        server_origin=TrustedOrigin(
            route="telegram", is_authenticated=True, clearance="private"
        ),
        authenticated_user=AuthenticatedUser(
            stable_id="3899230", configured_display_name="Nicola"
        ),
        user_peer="telegram_3899230",
    )
    assert value.user_id == "3899230"
    assert value.display_name == "Nicola"


def test_from_origin_signature_has_no_claimed_name_parameter() -> None:
    """The factory must not accept a display_name / user_name argument —
    an authenticated transport identity is the only name source."""
    params = set(inspect.signature(UserProvenance.from_origin).parameters)
    for forbidden in ("display_name", "user_name", "user_display_name", "name"):
        assert forbidden not in params


def test_claimed_display_name_cannot_be_injected_via_kwargs() -> None:
    """A caller cannot smuggle a claimed name in through an unexpected
    keyword argument — the factory must reject it outright rather than
    silently accepting and threading it through."""
    with pytest.raises(TypeError):
        UserProvenance.from_origin(
            surface="telegram",
            server_origin=TrustedOrigin(
                route="telegram", is_authenticated=True, clearance="private"
            ),
            authenticated_user=AuthenticatedUser(
                stable_id="3899230", configured_display_name="Nicola"
            ),
            user_peer="telegram_3899230",
            user_display_name="Attacker-Controlled Name",
        )


def test_claimed_name_in_external_mapping_is_not_threaded_through() -> None:
    """Simulates an inbound payload where an attacker sets a claimed name
    field (as if from message text or a webhook body). Only fields the
    factory's real signature accepts may be passed; the claimed-name key
    must be dropped, not silently accepted, and the resulting provenance
    must reflect ONLY the authenticated transport identity."""
    inbound_payload = {
        "surface": "telegram",
        "server_origin": TrustedOrigin(
            route="telegram", is_authenticated=True, clearance="private"
        ),
        "authenticated_user": AuthenticatedUser(
            stable_id="3899230", configured_display_name="Nicola"
        ),
        "user_peer": "telegram_3899230",
        "user_display_name": "Attacker-Controlled Name",
        "user_name": "Also Attacker-Controlled",
    }
    accepted_params = set(inspect.signature(UserProvenance.from_origin).parameters)
    safe_kwargs = {k: v for k, v in inbound_payload.items() if k in accepted_params}
    assert "user_display_name" not in safe_kwargs
    assert "user_name" not in safe_kwargs
    value = UserProvenance.from_origin(**safe_kwargs)
    assert value.display_name == "Nicola"
    assert value.user_id == "3899230"


def test_unauthenticated_non_voice_surface_gets_no_identity() -> None:
    value = UserProvenance.from_origin(
        surface="telegram",
        server_origin=TrustedOrigin(
            route="telegram", is_authenticated=False, clearance="public"
        ),
        authenticated_user=AuthenticatedUser(
            stable_id="3899230", configured_display_name="Nicola"
        ),
        user_peer="telegram_anon",
    )
    # server_origin says NOT authenticated, so even a present
    # authenticated_user object must not be trusted.
    assert value.user_id is None
    assert value.display_name is None


def test_from_origin_route_mismatch_rejected() -> None:
    with pytest.raises(ValueError):
        UserProvenance.from_origin(
            surface="telegram",
            server_origin=TrustedOrigin(
                route="voice", is_authenticated=True, clearance="private"
            ),
            authenticated_user=None,
            user_peer="telegram_123",
        )


# ---------------------------------------------------------------------------
# CRITICAL: type-confused fields in an otherwise schema-shaped mapping
# must be rejected as malformed, never raise TypeError/AttributeError out
# of validate_speaker_provenance.
# ---------------------------------------------------------------------------


def test_type_confused_display_name_on_user_is_malformed_not_a_crash() -> None:
    raw = _raw_mapping(speaker_kind="user", user_peer="telegram_123", display_name=123)
    with pytest.raises(ValueError):
        provenance_from_mapping(raw)


def test_type_confused_role_id_on_resident_is_malformed_not_a_crash() -> None:
    raw = _raw_mapping(
        speaker_kind="resident",
        role_id=123,
        persona_id="casa.personas/tina",
        persona_version="1.0.0",
        binding_digest="sha256:" + "5" * 64,
    )
    with pytest.raises(ValueError):
        provenance_from_mapping(raw)


# ---------------------------------------------------------------------------
# JSON Schema drift guard: the schema and the Python validator must agree.
# ---------------------------------------------------------------------------


def test_schema_accepts_a_valid_provenance_mapping() -> None:
    schema = json.loads(_SCHEMA_PATH.read_text())
    jsonschema.validate(instance=provenance_mapping(resident()), schema=schema)


def test_schema_and_python_validator_agree_on_invalid_mappings() -> None:
    schema = json.loads(_SCHEMA_PATH.read_text())

    # A resident missing binding_digest: both the schema and the Python
    # validator must reject it.
    resident_missing_digest = provenance_mapping(resident())
    resident_missing_digest["binding_digest"] = None
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=resident_missing_digest, schema=schema)
    with pytest.raises(ValueError):
        validate_speaker_provenance(SpeakerProvenance(**resident_missing_digest))

    # A user carrying a non-null role_id: both must reject it.
    user_with_role_id = provenance_mapping(user())
    user_with_role_id["role_id"] = "resident:butler"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=user_with_role_id, schema=schema)
    with pytest.raises(ValueError):
        validate_speaker_provenance(SpeakerProvenance(**user_with_role_id))


def test_schema_accepts_automation_provenance() -> None:
    """#349: `UserProvenance.from_origin` emits speaker_kind="automation" for
    /invoke and webhook turns; the SHIPPED schema must accept what the runtime
    validator accepts."""
    schema = json.loads(_SCHEMA_PATH.read_text())
    automation = SpeakerProvenance(
        speaker_kind="automation", user_peer="webhook:github",
    )
    jsonschema.validate(instance=provenance_mapping(automation), schema=schema)


def test_schema_and_python_validator_agree_on_invalid_automation() -> None:
    """Automation identifies no person and no agent role: user_id,
    display_name and the agent fields must be null in BOTH validators."""
    schema = json.loads(_SCHEMA_PATH.read_text())
    base = _raw_mapping(speaker_kind="automation", user_peer="webhook:github")

    for field, value in (
        ("user_id", "tg:12345"),
        ("display_name", "Nicola"),
        ("role_id", "resident:butler"),
    ):
        bad = dict(base)
        bad[field] = value
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad, schema=schema)
        with pytest.raises(ValueError):
            validate_speaker_provenance(SpeakerProvenance(**bad))
