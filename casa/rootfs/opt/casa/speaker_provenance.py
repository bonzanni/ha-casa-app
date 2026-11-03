from __future__ import annotations

import base64
import json
import re
import unicodedata
from dataclasses import asdict
from typing import Iterable, Literal

from canonical_bytes import canonical_json_bytes
from personality_types import (
    AuthenticatedUser,
    SpeakerProvenance,
    TrustedOrigin,
)

RESERVED_SOURCE_NAMESPACE = "casa-source-"
RESERVED_SOURCE_PREFIX = "casa-source-v1."
_ROLE_RE = re.compile(r"^(resident|specialist|executor):[a-z0-9][a-z0-9-]{0,31}$")
_PERSONA_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?/[a-z0-9][a-z0-9-]*$"
)
_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
MAX_CANONICAL_PROVENANCE_BYTES = 2048
MAX_ENCODED_PROVENANCE_TAG_BYTES = 2746
# Provenance JSON is a flat mapping of scalar fields — 32 levels of
# array/object nesting is already generous. Enforced as an explicit
# byte-level scan BEFORE json.loads so a hostile-but-cheap payload (e.g.
# ~1000 nested arrays) is rejected as malformed rather than relying on the
# interpreter's own recursion limit, which raises RecursionError — not a
# ValueError, so it would otherwise escape this module's error contract.
MAX_PROVENANCE_JSON_DEPTH = 32
_FIELD_LIMITS = {
    "role_id": (128, 128),
    "persona_id": (192, 192),
    "persona_version": (64, 64),
    "display_name": (80, 320),
    "user_peer": (256, 512),
    "user_id": (256, 512),
}


# Ingress surfaces reached with a shared bearer secret rather than by a
# person: their turns are authored by a machine (#204).
_AUTOMATION_SURFACES = frozenset({"invoke", "webhook"})


class UserProvenance:
    """Trusted factory namespace; not a fifth runtime identity object."""

    @staticmethod
    def from_origin(
        *,
        surface: Literal["telegram", "voice", "invoke", "webhook"],
        server_origin: TrustedOrigin,
        authenticated_user: AuthenticatedUser | None,
        user_peer: str,
    ) -> SpeakerProvenance:
        if server_origin.route != surface:
            raise ValueError("trusted origin route does not match surface")
        if surface in _AUTOMATION_SURFACES:
            # #204: /invoke and /webhook are reached with a shared bearer
            # secret. That authorizes the request; it never establishes who
            # authored the text, so the turn is an ``automation`` — honest
            # about being externally originated without claiming either a human
            # author or Casa's own internal authority.
            if authenticated_user is not None:
                raise ValueError(
                    "an automation surface cannot assert an authenticated user")
            value = SpeakerProvenance(
                speaker_kind="automation",
                user_peer=unicodedata.normalize("NFC", user_peer),
            )
            validate_speaker_provenance(value)
            return value
        if surface == "voice" and authenticated_user is None:
            if user_peer != "voice_speaker":
                raise ValueError("anonymous voice must use voice_speaker")
            value = SpeakerProvenance(
                speaker_kind="user", user_peer="voice_speaker",
                user_id=None, display_name=None,
            )
            validate_speaker_provenance(value)
            return value
        trusted_user = (
            authenticated_user
            if authenticated_user is not None and server_origin.is_authenticated
            else None
        )
        value = SpeakerProvenance(
            speaker_kind="user",
            display_name=(
                unicodedata.normalize("NFC", trusted_user.configured_display_name)
                if trusted_user and trusted_user.configured_display_name else None
            ),
            user_peer=unicodedata.normalize("NFC", user_peer),
            user_id=(
                unicodedata.normalize("NFC", trusted_user.stable_id)
                if trusted_user else None
            ),
        )
        validate_speaker_provenance(value)
        return value


def _validate_string_bound(name: str, value: str | None) -> None:
    if value is None:
        return
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value:
        raise ValueError(f"{name} must already be NFC")
    scalar_limit, byte_limit = _FIELD_LIMITS[name]
    if len(value) > scalar_limit or len(value.encode("utf-8")) > byte_limit:
        raise ValueError(f"{name} exceeds provenance length limit")


def validate_speaker_provenance(value: SpeakerProvenance) -> None:
    for field_name in ("speaker_kind", "role_id", "persona_id", "persona_version",
                       "display_name", "binding_digest", "user_peer", "user_id"):
        field_value = getattr(value, field_name)
        if field_value is not None and not isinstance(field_value, str):
            raise ValueError(f"{field_name} must be a string or null")
    kind = value.speaker_kind
    if kind in {"resident", "specialist"}:
        if not value.role_id or not value.role_id.startswith(kind + ":"):
            raise ValueError("agent role_id must match speaker_kind")
        if not value.persona_id or not _PERSONA_RE.fullmatch(value.persona_id):
            raise ValueError("resident/specialist persona_id is required")
        if not value.persona_version or not _SEMVER_RE.fullmatch(value.persona_version):
            raise ValueError("resident/specialist persona_version is required")
        if not value.binding_digest or not _CHECKSUM_RE.fullmatch(value.binding_digest):
            raise ValueError("resident/specialist binding_digest is required")
        if value.user_peer is not None or value.user_id is not None:
            raise ValueError("agent provenance cannot contain user identity")
    elif kind == "executor":
        if not value.role_id or not value.role_id.startswith("executor:"):
            raise ValueError("executor role_id is required")
        if any((value.persona_id, value.persona_version,
                value.binding_digest, value.user_peer, value.user_id)):
            raise ValueError("executor persona and user fields must be null")
    elif kind == "user":
        if not value.user_peer:
            raise ValueError("user_peer is required for user provenance")
        if any((value.role_id, value.persona_id,
                value.persona_version, value.binding_digest)):
            raise ValueError("user agent fields must be null")
    elif kind == "automation":
        # A machine author reached through a shared bearer secret (#204). It
        # gets a peer so the originating trigger stays attributable, but never
        # a user_id or display_name — those are what promote a provenance to a
        # NAMED person, and a shared secret
        # identifies no human. Agent fields stay null: it is not one of Casa's
        # own roles either.
        if not value.user_peer:
            raise ValueError("user_peer is required for automation provenance")
        if any((value.role_id, value.persona_id, value.persona_version,
                value.binding_digest, value.display_name, value.user_id)):
            raise ValueError(
                "automation identity cannot name a person or an agent role")
    elif kind == "system":
        if any((value.role_id, value.persona_id, value.persona_version,
                value.binding_digest, value.user_peer, value.user_id)):
            raise ValueError("system identity fields must be null")
    else:
        raise ValueError("invalid speaker_kind")
    if value.role_id is not None and not _ROLE_RE.fullmatch(value.role_id):
        raise ValueError("invalid role_id")
    for field_name in _FIELD_LIMITS:
        _validate_string_bound(field_name, getattr(value, field_name))


def provenance_mapping(value: SpeakerProvenance) -> dict[str, object]:
    validate_speaker_provenance(value)
    return asdict(value)


def provenance_from_mapping(raw: object) -> SpeakerProvenance:
    if not isinstance(raw, dict):
        raise ValueError("provenance must be an object")
    expected = {
        "speaker_kind", "role_id", "persona_id", "persona_version",
        "display_name", "binding_digest", "user_peer", "user_id",
    }
    if set(raw) != expected:
        raise ValueError("provenance fields must match the v1 schema exactly")
    value = SpeakerProvenance(**raw)
    validate_speaker_provenance(value)
    return value


def encode_provenance_tag(value: SpeakerProvenance) -> str:
    wire = canonical_json_bytes(provenance_mapping(value))
    if len(wire) > MAX_CANONICAL_PROVENANCE_BYTES:
        raise ValueError("canonical provenance payload exceeds 2048 bytes")
    payload = base64.urlsafe_b64encode(wire).decode("ascii").rstrip("=")
    tag = RESERVED_SOURCE_PREFIX + payload
    if len(tag.encode("ascii")) > MAX_ENCODED_PROVENANCE_TAG_BYTES:
        raise ValueError("encoded provenance tag exceeds 2746 bytes")
    return tag


def _reject_excessive_json_nesting(wire: bytes, limit: int) -> None:
    """Reject *wire* if its bracket/brace nesting depth exceeds *limit*,
    scanning raw bytes so this never itself recurses. String contents are
    skipped (tracking quote/escape state) so a bracket character inside a
    JSON string value is not mistaken for structural nesting."""
    depth = 0
    in_string = False
    escaped = False
    for byte in wire:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # double quote
                in_string = False
            continue
        if byte == 0x22:  # double quote
            in_string = True
        elif byte in (0x7B, 0x5B):  # '{' or '['
            depth += 1
            if depth > limit:
                raise ValueError("invalid provenance payload")
        elif byte in (0x7D, 0x5D):  # '}' or ']'
            depth -= 1


def decode_provenance_tag(tag: str) -> SpeakerProvenance:
    if not isinstance(tag, str) or not tag.isascii():
        raise ValueError("provenance tag must be ASCII")
    if len(tag.encode("ascii")) > MAX_ENCODED_PROVENANCE_TAG_BYTES:
        raise ValueError("encoded provenance tag exceeds 2746 bytes")
    if not tag.startswith(RESERVED_SOURCE_PREFIX):
        raise ValueError("unsupported provenance tag")
    payload = tag[len(RESERVED_SOURCE_PREFIX):]
    if not payload or "=" in payload:
        raise ValueError("provenance tag must use unpadded base64url")
    if (len(payload) * 3) // 4 > MAX_CANONICAL_PROVENANCE_BYTES:
        raise ValueError("encoded payload would exceed 2048 bytes")
    try:
        wire = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        if len(wire) > MAX_CANONICAL_PROVENANCE_BYTES:
            raise ValueError("canonical provenance payload exceeds 2048 bytes")
        # FIX 2: base64.urlsafe_b64decode (validate=False) silently drops
        # any character outside the base64url alphabet, so a payload with
        # junk spliced in can decode to the SAME bytes as the canonical
        # spelling encode_provenance_tag would have produced. Require the
        # payload to be that one canonical spelling.
        if base64.urlsafe_b64encode(wire).decode("ascii").rstrip("=") != payload:
            raise ValueError("invalid provenance payload")
        # FIX 3 (foundation review r2): _reject_excessive_json_nesting below
        # scans `wire` byte-by-byte assuming UTF-8 (one structural byte per
        # character), but json.loads(bytes) auto-detects UTF-16/UTF-32 per
        # RFC 4627. A UTF-16-encoded wire's 2-byte-per-character encoding
        # can desync the scanner's in_string tracking (e.g. an
        # escaped-quote sequence), letting real nested-array bytes it
        # should have counted slip past uncounted. Require wire be valid
        # UTF-8 BEFORE the depth scan runs — the canonical RFC 8785 re-check
        # further below already requires UTF-8 canonical JSON, so a
        # non-UTF-8 wire is always invalid anyway; this just moves that
        # guarantee ahead of the depth scan (and ahead of json.loads).
        wire.decode("utf-8")
        # FIX 1: bound nesting depth before trusting the parser — a cheap
        # hostile payload (deeply nested arrays) can otherwise blow the
        # interpreter's recursion limit inside json.loads.
        _reject_excessive_json_nesting(wire, MAX_PROVENANCE_JSON_DEPTH)
        raw = json.loads(wire)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ValueError("invalid provenance payload") from exc
    if wire != canonical_json_bytes(raw):
        raise ValueError("provenance payload is not canonical RFC 8785 JSON")
    return provenance_from_mapping(raw)


def decode_provenance_from_tags(
    tags: Iterable[str],
) -> tuple[SpeakerProvenance | None, str]:
    reserved = [
        tag for tag in tags
        if isinstance(tag, str) and tag.startswith(RESERVED_SOURCE_NAMESPACE)
    ]
    if not reserved:
        return None, "missing"
    if len(reserved) != 1:
        return None, "multiple"
    try:
        return decode_provenance_tag(reserved[0]), "ok"
    except ValueError:
        return None, "malformed"
