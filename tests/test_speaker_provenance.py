"""Strict codec tests for the immutable speaker-provenance contract.

Covers the reserved `casa-source-v1.` tag codec (canonical RFC 8785 JSON,
base64url unpadded), the frozen `SpeakerProvenance`/`RetainedTurn` snapshot
types, and the `UserProvenance.from_origin` factory contract: an anonymous
voice speaker never acquires a name, and an authenticated transport
identity is the ONLY name source (no caller-supplied display name can be
injected through the factory).
"""

from __future__ import annotations

import base64
import codecs
import inspect
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import jsonschema
import pytest
import rfc8785

from speaker_provenance import (
    MAX_CANONICAL_PROVENANCE_BYTES,
    MAX_ENCODED_PROVENANCE_TAG_BYTES,
    RESERVED_SOURCE_PREFIX,
    UserProvenance,
    _FIELD_LIMITS,
    decode_provenance_from_tags,
    decode_provenance_tag,
    encode_provenance_tag,
    provenance_mapping,
    validate_speaker_provenance,
)
from canonical_bytes import canonical_json_bytes
from personality_types import (
    AuthenticatedUser,
    RetainedTurn,
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
    overridden by kwargs. Used to build malformed canonical tags directly
    (bypassing `encode_provenance_tag`, which validates)."""
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


def _tag_from_wire(wire: bytes) -> str:
    payload = base64.urlsafe_b64encode(wire).decode("ascii").rstrip("=")
    return RESERVED_SOURCE_PREFIX + payload


def _tag_from_raw_mapping(raw: dict[str, object]) -> str:
    """Build a canonical (RFC 8785), correctly-base64'd tag directly from a
    raw dict via `rfc8785.dumps` — never through `encode_provenance_tag`."""
    return _tag_from_wire(rfc8785.dumps(raw))


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
# Step 1 skeleton (verbatim from the brief)
# ---------------------------------------------------------------------------


def test_reserved_tag_round_trip_is_canonical_and_unpadded() -> None:
    tag = encode_provenance_tag(resident())
    assert tag.startswith(RESERVED_SOURCE_PREFIX)
    assert "=" not in tag
    assert decode_provenance_tag(tag) == resident()


@pytest.mark.parametrize(
    "tags",
    [
        (),
        ("casa-source-v1.bad!",),
        (encode_provenance_tag(resident()), encode_provenance_tag(resident())),
        (encode_provenance_tag(resident()), "casa-source-v2.invalid"),
    ],
)
def test_missing_malformed_duplicate_or_conflicting_tags_are_unknown(tags) -> None:
    provenance, reason = decode_provenance_from_tags(tags)
    assert provenance is None
    assert reason != "ok"


def test_snapshot_is_frozen() -> None:
    value = resident()
    with pytest.raises(FrozenInstanceError):
        value.display_name = "Changed"


# ---------------------------------------------------------------------------
# Additional codec coverage: all speaker kinds round-trip, ASCII, tag shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [resident(), specialist(), executor(), user(), system()],
    ids=["resident", "specialist", "executor", "user", "system"],
)
def test_all_speaker_kinds_round_trip(value: SpeakerProvenance) -> None:
    tag = encode_provenance_tag(value)
    assert tag.isascii()
    assert tag.startswith(RESERVED_SOURCE_PREFIX)
    assert "=" not in tag
    assert decode_provenance_tag(tag) == value


def test_retained_turn_is_frozen_and_holds_provenance() -> None:
    turn = RetainedTurn(text="hello", provenance=resident())
    assert turn.provenance == resident()
    with pytest.raises(FrozenInstanceError):
        turn.text = "changed"


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
    tag = encode_provenance_tag(value)
    assert decode_provenance_tag(tag) == value


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
    tag = encode_provenance_tag(value)
    assert decode_provenance_tag(tag) == value


def test_worst_case_four_byte_unicode_user_round_trip() -> None:
    scalar, byte = _FIELD_LIMITS["user_peer"]
    value = SpeakerProvenance(
        speaker_kind="user",
        user_peer=_fill(scalar, byte, _POO),
    )
    validate_speaker_provenance(value)
    tag = encode_provenance_tag(value)
    assert decode_provenance_tag(tag) == value


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
    tag = encode_provenance_tag(value)
    assert decode_provenance_tag(tag) == value


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
# Exact boundary tests: 2048-byte canonical payload, 2746-byte final tag
# ---------------------------------------------------------------------------


# NOTE on how these tests are constructed: the per-field `_FIELD_LIMITS`
# ceilings cap the maximum canonical JSON payload reachable by ANY value
# that also passes `validate_speaker_provenance` well below 2048 bytes (the
# richest case, a "user" record with both `user_peer` and `user_id` maxed
# out using 2-byte UTF-8 filler, tops out at roughly 1.1 KB). That means
# `encode_provenance_tag`'s own 2048-byte size check can never actually
# fire for a legitimately field-validated `SpeakerProvenance` today — it is
# pure defense-in-depth headroom (protects against a future field addition
# or a validator bug), not a reachable path. The exact 2048/2746 byte
# ceilings ARE independently exercisable, though: `decode_provenance_tag`
# checks encoded/decoded SIZE strictly before it calls
# `provenance_from_mapping` (which is what applies the tighter per-field
# limits). So these tests build a raw, schema-shaped dict directly (not
# going through `SpeakerProvenance`/`validate_speaker_provenance`), base64
# it by hand into a tag, and confirm the SIZE ceiling in
# `decode_provenance_tag` accepts exactly-2048/2746-byte input and rejects
# one byte more — independent of what the (separate, tighter) per-field
# check does afterward.


def _wire_bytes_for_user_peer(user_peer: str) -> bytes:
    return canonical_json_bytes(
        {
            "speaker_kind": "user",
            "role_id": None,
            "persona_id": None,
            "persona_version": None,
            "display_name": None,
            "binding_digest": None,
            "user_peer": user_peer,
            "user_id": None,
        }
    )


def _max_user_peer_for_payload_ceiling(overshoot: int = 0) -> str:
    """The `user_peer` value (ASCII, so 1 char == 1 byte) whose canonical
    JSON payload lands at exactly MAX_CANONICAL_PROVENANCE_BYTES + overshoot.
    Deliberately exceeds the real `_FIELD_LIMITS["user_peer"]` ceiling — this
    builds a raw dict for the SIZE-ceiling tests below, never a validated
    `SpeakerProvenance`."""
    base_len = len(_wire_bytes_for_user_peer("p"))
    pad_needed = MAX_CANONICAL_PROVENANCE_BYTES - base_len + overshoot
    return "p" + "a" * pad_needed


def _tag_from_raw_wire(wire: bytes) -> str:
    payload = base64.urlsafe_b64encode(wire).decode("ascii").rstrip("=")
    return RESERVED_SOURCE_PREFIX + payload


def test_exact_2048_byte_canonical_payload_passes_size_ceiling() -> None:
    """A raw payload of exactly 2048 canonical bytes must clear
    `decode_provenance_tag`'s size ceiling: it is rejected afterwards for
    exceeding the (tighter, independent) `user_peer` field limit — proving
    the rejection is NOT the size ceiling, i.e. the size ceiling itself
    passed a value at its exact boundary."""
    user_peer = _max_user_peer_for_payload_ceiling()
    wire = _wire_bytes_for_user_peer(user_peer)
    assert len(wire) == MAX_CANONICAL_PROVENANCE_BYTES
    tag = _tag_from_raw_wire(wire)
    with pytest.raises(ValueError, match="length limit") as excinfo:
        decode_provenance_tag(tag)
    assert "2048 bytes" not in str(excinfo.value)
    assert "2746 bytes" not in str(excinfo.value)


def test_canonical_payload_one_byte_over_2048_rejected_by_size_ceiling() -> None:
    """One byte past the ceiling must be rejected by a SIZE check (either
    the outer tag-length check or the payload-length pre-check — both fire
    before content validation ever runs), never by field-level content
    validation."""
    user_peer = _max_user_peer_for_payload_ceiling(overshoot=1)
    wire = _wire_bytes_for_user_peer(user_peer)
    assert len(wire) == MAX_CANONICAL_PROVENANCE_BYTES + 1
    tag = _tag_from_raw_wire(wire)
    with pytest.raises(ValueError, match=r"2048 bytes|2746 bytes") as excinfo:
        decode_provenance_tag(tag)
    assert "length limit" not in str(excinfo.value)


def test_exact_2746_byte_final_tag_passes_length_precheck() -> None:
    """The tag produced from an exactly-2048-byte payload must land at
    exactly MAX_ENCODED_PROVENANCE_TAG_BYTES: 15-byte ASCII prefix +
    unpadded base64url of 2048 bytes (ceil(2048*4/3) = 2731 chars) = 2746.
    `decode_provenance_tag` must accept that exact length and reject it only
    for the (independent, tighter) per-field content ceiling."""
    assert len(RESERVED_SOURCE_PREFIX.encode("ascii")) == 15
    user_peer = _max_user_peer_for_payload_ceiling()
    wire = _wire_bytes_for_user_peer(user_peer)
    tag = _tag_from_raw_wire(wire)
    assert len(tag.encode("ascii")) == MAX_ENCODED_PROVENANCE_TAG_BYTES
    with pytest.raises(ValueError, match="length limit") as excinfo:
        decode_provenance_tag(tag)
    assert "2746 bytes" not in str(excinfo.value)


def test_tag_one_byte_over_2746_rejected_before_decoding() -> None:
    """A tag exactly one byte past MAX_ENCODED_PROVENANCE_TAG_BYTES must be
    rejected by the length pre-check, before any base64/JSON decoding is
    attempted (garbage payload content that would blow up a real decoder,
    proving decode never got that far)."""
    overlong_payload = "a" * (MAX_ENCODED_PROVENANCE_TAG_BYTES - len(RESERVED_SOURCE_PREFIX) + 1)
    tag = RESERVED_SOURCE_PREFIX + overlong_payload
    assert len(tag.encode("ascii")) == MAX_ENCODED_PROVENANCE_TAG_BYTES + 1
    with pytest.raises(ValueError, match="2746 bytes"):
        decode_provenance_tag(tag)


# ---------------------------------------------------------------------------
# Oversized encoded-INPUT rejection before base64/JSON decoding
# ---------------------------------------------------------------------------


def test_oversized_encoded_tag_input_rejected_before_decoding() -> None:
    # Construct a tag string far larger than MAX_ENCODED_PROVENANCE_TAG_BYTES
    # whose payload segment is deliberately NOT valid base64url and NOT valid
    # JSON once decoded — if the length pre-check didn't run first, this
    # would raise from inside base64/json decoding instead of the length
    # guard, and the exercise below verifies decode never gets that far by
    # using a payload guaranteed to blow up if fed to a real decoder.
    huge_payload = "%" * (MAX_ENCODED_PROVENANCE_TAG_BYTES * 3)
    tag = RESERVED_SOURCE_PREFIX + huge_payload
    with pytest.raises(ValueError, match="2746 bytes"):
        decode_provenance_tag(tag)


def test_oversized_but_validly_shaped_encoded_input_rejected_before_decoding() -> None:
    # Even a well-formed unpadded-base64url alphabet payload that merely
    # exceeds the tag byte ceiling must be rejected by the length pre-check.
    overlong_payload = "a" * (MAX_ENCODED_PROVENANCE_TAG_BYTES * 2)
    tag = RESERVED_SOURCE_PREFIX + overlong_payload
    with pytest.raises(ValueError, match="2746 bytes"):
        decode_provenance_tag(tag)


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
# CRITICAL: type-confused fields in an otherwise canonical, correctly-
# base64'd tag must be rejected as malformed, never raise TypeError/
# AttributeError out of validate_speaker_provenance.
# ---------------------------------------------------------------------------


def test_type_confused_display_name_on_user_is_malformed_not_a_crash() -> None:
    tag = _tag_from_raw_mapping(
        _raw_mapping(speaker_kind="user", user_peer="telegram_123", display_name=123)
    )
    provenance, reason = decode_provenance_from_tags((tag,))
    assert provenance is None
    assert reason != "ok"
    with pytest.raises(ValueError):
        decode_provenance_tag(tag)


def test_type_confused_role_id_on_resident_is_malformed_not_a_crash() -> None:
    tag = _tag_from_raw_mapping(
        _raw_mapping(
            speaker_kind="resident",
            role_id=123,
            persona_id="casa.personas/tina",
            persona_version="1.0.0",
            binding_digest="sha256:" + "5" * 64,
        )
    )
    provenance, reason = decode_provenance_from_tags((tag,))
    assert provenance is None
    assert reason != "ok"
    with pytest.raises(ValueError):
        decode_provenance_tag(tag)


# ---------------------------------------------------------------------------
# decode_provenance_tag rejection categories (brief-mandated)
# ---------------------------------------------------------------------------


def test_non_ascii_tag_rejected() -> None:
    tag = RESERVED_SOURCE_PREFIX + "é"
    with pytest.raises(ValueError):
        decode_provenance_tag(tag)


def test_padded_base64_tag_rejected() -> None:
    valid_tag = encode_provenance_tag(user())
    padded_tag = valid_tag + "="
    with pytest.raises(ValueError, match="unpadded base64url"):
        decode_provenance_tag(padded_tag)


def test_valid_but_non_canonical_json_payload_rejected() -> None:
    """Valid base64 of valid JSON that decodes to a schema-shaped dict, but
    whose wire bytes are NOT RFC 8785 canonical (here: standard json.dumps
    key order + whitespace, rather than rfc8785's sorted/compact form)."""
    raw = _raw_mapping(speaker_kind="user", user_peer="telegram_123")
    non_canonical_wire = json.dumps(raw).encode("utf-8")
    assert non_canonical_wire != canonical_json_bytes(raw)
    tag = _tag_from_wire(non_canonical_wire)
    with pytest.raises(ValueError, match="not canonical RFC 8785 JSON"):
        decode_provenance_tag(tag)


def test_sole_unsupported_version_tag_rejected() -> None:
    """A single reserved-namespace tag with an unsupported version prefix
    must be rejected by both entry points. (Not paired with a v1 tag here —
    that would short-circuit to the "multiple" path without ever calling
    the decoder.)"""
    tag = "casa-source-v2.x"
    provenance, reason = decode_provenance_from_tags((tag,))
    assert provenance is None
    assert reason != "ok"
    with pytest.raises(ValueError, match="unsupported provenance tag"):
        decode_provenance_tag(tag)


# ---------------------------------------------------------------------------
# FIX 1 (foundation review, P0): a deeply-nested-array payload must never
# escape decode_provenance_tag/decode_provenance_from_tags as a raw
# RecursionError — it must fail closed as the same malformed-provenance
# ValueError, both via an explicit depth bound checked BEFORE json.loads
# and (defense in depth) a broadened except clause.
# ---------------------------------------------------------------------------


def _nested_array_wire(depth: int) -> bytes:
    return b"[" * depth + b"0" + b"]" * depth


def test_hostile_deeply_nested_payload_is_malformed_not_a_crash() -> None:
    wire = _nested_array_wire(1000)
    assert len(wire) < MAX_CANONICAL_PROVENANCE_BYTES
    tag = _tag_from_wire(wire)

    provenance, reason = decode_provenance_from_tags((tag,))
    assert provenance is None
    assert reason == "malformed"

    with pytest.raises(ValueError) as exc_info:
        decode_provenance_tag(tag)
    assert not isinstance(exc_info.value, RecursionError)


def test_nesting_depth_at_limit_is_not_rejected_by_depth_guard() -> None:
    """Depth exactly at the bound must clear the depth guard — json.loads
    then runs and the payload is rejected for a DIFFERENT reason (it's an
    array, not a provenance object), proving the depth check itself did
    not trip."""
    from speaker_provenance import MAX_PROVENANCE_JSON_DEPTH

    tag = _tag_from_wire(_nested_array_wire(MAX_PROVENANCE_JSON_DEPTH))
    with pytest.raises(ValueError, match="provenance must be an object"):
        decode_provenance_tag(tag)


def test_nesting_depth_over_limit_is_rejected_by_depth_guard() -> None:
    from speaker_provenance import MAX_PROVENANCE_JSON_DEPTH

    tag = _tag_from_wire(_nested_array_wire(MAX_PROVENANCE_JSON_DEPTH + 1))
    with pytest.raises(ValueError, match="invalid provenance payload"):
        decode_provenance_tag(tag)


# ---------------------------------------------------------------------------
# R2 (foundation review r2): _reject_excessive_json_nesting byte-scans
# `wire` assuming UTF-8 (one structural byte per character), but
# json.loads(bytes) auto-detects UTF-16/UTF-32 per RFC 4627. A UTF-16LE
# wire encodes every ASCII character as two bytes (low byte + 0x00 high
# byte). Terra's reproducer: a JSON string containing an escaped quote
# (`\"`), immediately followed by real deep array nesting. In UTF-16LE,
# the escaped-quote sequence's own 0x00 high bytes desync the scanner's
# in_string tracking (it mistakes the escaped quote's low byte for an
# unescaped string terminator, and the real closing quote's low byte for
# a NEW string open) — so the scanner ends up believing the following
# ~480 levels of genuine array nesting sit inside a string, and never
# counts them at all. Fixed by requiring `wire` be valid UTF-8 BEFORE the
# depth scan runs.
# ---------------------------------------------------------------------------


def _utf16le_bom_wire_with_desyncing_escaped_quote(nesting_depth: int) -> bytes:
    """A UTF-16LE-BOM-encoded JSON wire: a top-level array whose first
    element is the (JSON-escaped) string `"` and whose second element is
    *nesting_depth* levels of real nested arrays. Byte-scanning this
    assuming UTF-8 (the pre-fix behavior) desyncs on the escaped-quote
    bytes and never counts the following real nesting at all."""
    quoted_quote = json.dumps('"')  # -> the 4-character text: "\""
    logical_text = "[" + quoted_quote + "," + "[" * nesting_depth + "0" + "]" * nesting_depth + "]"
    json.loads(logical_text)  # sanity: this really is valid, deeply nested JSON
    return codecs.BOM_UTF16_LE + logical_text.encode("utf-16-le")


def test_old_byte_level_depth_scan_would_have_missed_the_utf16_nesting() -> None:
    """Characterizes the vulnerability itself (independent of the fix):
    replaying the pre-fix byte-scan algorithm against this wire proves it
    never sees depth exceed 1, even though the wire's real (UTF-16-
    decoded) nesting is far beyond MAX_PROVENANCE_JSON_DEPTH."""
    from speaker_provenance import MAX_PROVENANCE_JSON_DEPTH

    wire = _utf16le_bom_wire_with_desyncing_escaped_quote(480)

    depth = 0
    max_seen = 0
    in_string = False
    escaped = False
    for byte in wire:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            max_seen = max(max_seen, depth)
        elif byte in (0x7D, 0x5D):
            depth -= 1
    assert max_seen < MAX_PROVENANCE_JSON_DEPTH


def test_utf16_wire_is_rejected_before_reaching_json_loads(monkeypatch) -> None:
    wire = _utf16le_bom_wire_with_desyncing_escaped_quote(480)
    tag = _tag_from_wire(wire)

    called = False
    real_loads = json.loads

    def _spy_loads(*args, **kwargs):
        nonlocal called
        called = True
        return real_loads(*args, **kwargs)

    monkeypatch.setattr("speaker_provenance.json.loads", _spy_loads)

    provenance, reason = decode_provenance_from_tags((tag,))
    assert provenance is None
    assert reason == "malformed"
    with pytest.raises(ValueError, match="invalid provenance payload"):
        decode_provenance_tag(tag)
    assert not called, (
        "UTF-16 wire reached json.loads instead of being rejected by the "
        "UTF-8 pre-check ahead of the depth scan"
    )


def test_non_utf8_wire_without_deep_nesting_is_still_rejected() -> None:
    """A plain non-UTF-8 wire (no nesting-desync trick at all) must also
    be rejected — the UTF-8 requirement is unconditional, not just a
    patch for the specific desync reproducer above."""
    wire = codecs.BOM_UTF16_LE + json.dumps({"speaker_kind": "user"}).encode(
        "utf-16-le"
    )
    tag = _tag_from_wire(wire)
    provenance, reason = decode_provenance_from_tags((tag,))
    assert provenance is None
    assert reason == "malformed"


# ---------------------------------------------------------------------------
# FIX 2 (foundation review, P1): base64url decoding must be canonical —
# non-alphabet characters spliced into an otherwise-valid payload (silently
# ignored by base64.urlsafe_b64decode, so they decode to the SAME bytes)
# must be rejected rather than accepted as an alternate spelling of a valid
# tag.
# ---------------------------------------------------------------------------


def test_non_canonical_base64_spelling_is_rejected() -> None:
    valid_tag = encode_provenance_tag(user())
    payload = valid_tag[len(RESERVED_SOURCE_PREFIX):]
    assert len(payload) % 4 != 1  # inserting 4 chars preserves this
    mid = len(payload) // 2
    spliced_payload = payload[:mid] + "!!!!" + payload[mid:]
    spliced_tag = RESERVED_SOURCE_PREFIX + spliced_payload

    # Sanity: the splice really does decode to the same bytes (confirms the
    # vulnerability this test guards against, independent of our fix).
    padding = "=" * (-len(spliced_payload) % 4)
    assert base64.urlsafe_b64decode(spliced_payload + padding) == base64.urlsafe_b64decode(
        payload + "=" * (-len(payload) % 4)
    )

    provenance, reason = decode_provenance_from_tags((spliced_tag,))
    assert provenance is None
    assert reason == "malformed"
    with pytest.raises(ValueError):
        decode_provenance_tag(spliced_tag)


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
