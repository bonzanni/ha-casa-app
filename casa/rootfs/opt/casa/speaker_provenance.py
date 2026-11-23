from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict
from typing import Literal

from personality_types import (
    AuthenticatedUser,
    SpeakerProvenance,
    TrustedOrigin,
)

_ROLE_RE = re.compile(r"^(resident|specialist|executor):[a-z0-9][a-z0-9-]{0,31}$")
_PERSONA_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?/[a-z0-9][a-z0-9-]*$"
)
_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
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
