from dataclasses import dataclass
from typing import Literal


SpeakerKind = Literal[
    "user", "resident", "specialist", "executor", "system", "automation",
]
SensitivityTier = Literal["public", "friends", "family", "private"]


@dataclass(frozen=True, slots=True)
class SpeakerProvenance:
    speaker_kind: SpeakerKind
    role_id: str | None = None
    persona_id: str | None = None
    persona_version: str | None = None
    display_name: str | None = None
    binding_digest: str | None = None
    user_peer: str | None = None
    user_id: str | None = None


@dataclass(frozen=True, slots=True)
class TrustedOrigin:
    """Server-created route/authentication result; never decoded from payload."""
    route: Literal["telegram", "voice", "invoke", "webhook"]
    is_authenticated: bool
    clearance: SensitivityTier


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """Identity asserted by the authenticated transport, not by message text."""
    stable_id: str
    configured_display_name: str | None


@dataclass(frozen=True, slots=True)
class TrustedUserOriginInput:
    """Personality Task 9: the server-created, per-turn ingress identity a
    channel stamps onto its ``BusMessage`` AFTER external-context sanitization.
    It is the ONLY source ``Agent._process`` reads to build the persisted
    ``user_provenance`` — never free-text ``origin``/``context``. A turn that
    carries no ``TrustedUserOriginInput`` (scheduled heartbeat, webhook
    trigger, delegation-completion synthesis, internal/test turn) has no human
    author and is recorded with the honest unattributed ``system`` identity."""
    surface: Literal["telegram", "voice", "invoke", "webhook"]
    server_origin: TrustedOrigin
    authenticated_user: AuthenticatedUser | None
    user_peer: str
