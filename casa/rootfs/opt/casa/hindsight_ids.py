"""Canonical Hindsight bank-id builder.

Casa addresses Hindsight memory banks as ``casa-{role}`` (spec §4.1). This
builder fails fast on any id that falls outside a conservative client-side
charset, because the **server does not validate**: probed live against
Hindsight 0.7.1 (2026-06-02), ``casa.finance`` (dot), ``Casa_Butler``
(uppercase/underscore) and a 100-char id were ALL accepted with HTTP 200.
A permissive server is exactly why silent client-side acceptance is the bug
class to avoid (cf. the retired silent-sanitizer that blinded us for
11 days). We therefore impose our own rule and raise rather than coerce.

Allowed: ASCII letters, digits, underscore, hyphen. ``.``/``/``/space/
non-ASCII are rejected (``/`` and space also break the URL path segment).
"""
from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

from speaker_provenance import validate_speaker_provenance

if TYPE_CHECKING:
    from personality_types import SpeakerProvenance

_BANK_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Conservative cap. The server accepted 100 chars with no published limit;
# Casa's own names are short (``casa-assistant`` = 14), so 64 is ample and safe.
_BANK_NAME_MAX = 64


def bank_id(*parts: str) -> str:
    """Build a Hindsight bank id by joining ``parts`` with ``-``.

    Every part must be a non-empty ``str`` matching ``[A-Za-z0-9_-]+``.
    Raises ``ValueError`` on any violation or if the joined id exceeds
    ``_BANK_NAME_MAX``. Silent sanitization is intentionally NOT supported.
    """
    if not parts:
        raise ValueError("bank_id requires at least one part")
    for i, part in enumerate(parts):
        if not isinstance(part, str) or not part:
            raise ValueError(f"part {i} must be a non-empty str, got {part!r}")
        if not _BANK_NAME_RE.fullmatch(part):
            raise ValueError(
                f"part {i}={part!r} contains characters outside [A-Za-z0-9_-]"
            )
    joined = "-".join(parts)
    if len(joined) > _BANK_NAME_MAX:
        raise ValueError(
            f"bank id {joined!r} is {len(joined)} chars; max {_BANK_NAME_MAX}"
        )
    return joined


def content_document_id(speaker: str, text: str) -> str:
    """Content-stable Hindsight ``document_id`` for a retained turn.

    Hindsight upserts by ``document_id``, so the id chosen decides idempotency
    scope. The retired scheme ``f"{sdk_session_id}:{idx}"`` was idempotent only
    *within one SDK session*: the same utterance retained from a later, rotated
    session (a resumed/gap/`/new` conversation gets a fresh sid) landed under a
    new id and duplicated. Live evidence (2026-07-09 bug review, finding F1): one
    repetitive conversation produced ~50 near-identical memories across four sids.

    Keying on ``sha256(speaker \\x00 text)`` instead makes an identical
    ``(speaker, text)`` collapse to ONE document no matter how many sessions
    retain it — the desired behaviour for a personal memory (saying the same
    thing ten times is one fact, not ten). The ``m-`` prefix keeps ids readable
    and namespaced away from any accidental raw-hash collision with other id
    spaces; 24 hex chars (96 bits) is collision-safe for a single bank.
    """
    digest = hashlib.sha256(f"{speaker}\x00{text}".encode("utf-8")).hexdigest()
    return f"m-{digest[:24]}"


def automation_document_id(user_peer: str, text: str) -> str:
    """Content-stable ``document_id`` for a turn authored by an AUTOMATION —
    a machine that reached Casa through a shared bearer secret (``/invoke``, or
    a ``/webhook/{name}`` trigger). Personality Task 10 / #204.

    Keyed on ``(user_peer, text)`` like :func:`content_document_id`, because the
    originating trigger is exactly what distinguishes one automation from
    another: two triggers posting identical text are two facts from two sources,
    not one. Routing these through :func:`agent_document_id` instead would fold
    role/persona (both null on an automation) and DISCARD ``user_peer``
    entirely, collapsing every trigger into a single document.

    The digest is domain-separated by an ``automation`` prefix and the id by an
    ``m-x-`` prefix (x = eXternal), so the same ``(peer, text)`` can never
    collide with the user ``m-`` space or the agent ``m-a-`` space — an
    automation whose peer happens to spell a person's cannot upsert over that
    person's own memories."""
    if not user_peer:
        raise ValueError("automation_document_id requires a user_peer")
    digest = hashlib.sha256(
        f"automation\x00{user_peer}\x00{text}".encode("utf-8")
    ).hexdigest()
    return f"m-x-{digest[:24]}"


def agent_document_id(provenance: "SpeakerProvenance", text: str) -> str:
    """Content-stable ``document_id`` for a memory turn authored by an AGENT
    (resident / specialist / executor / the unattributed ``system`` identity) —
    the agent-side analogue of :func:`content_document_id` (which keys user turns
    on their ``user_peer``). Personality Task 10.

    Idempotency is keyed on the AGENT'S IDENTITY, not its exact persona VERSION:
    the digest folds in ``speaker_kind``/``role_id``/``persona_id`` but NOT
    ``persona_version``/``binding_digest``. So the same fact retained by the butler
    ``casa/tina@0.1.0`` and by the butler ``casa/tina@0.2.0`` upserts to the SAME
    Hindsight document (a persona bump is not a new author), while a DIFFERENT
    persona (``casa/ellen``) saying the same words is a distinct document. This
    mirrors ``content_document_id``'s "same speaker + same text = one document"
    contract at the resolution the personality model cares about (persona
    identity), not the churn-prone binding version.

    A ``user`` provenance is rejected — user turns must use
    :func:`content_document_id` so their id keys on the trusted ``user_peer``,
    never on an agent identity. The ``m-a-`` prefix namespaces agent ids apart
    from the user-turn ``m-`` space so the two can never collide."""
    validate_speaker_provenance(provenance)
    if provenance.speaker_kind == "user":
        raise ValueError("agent_document_id cannot be used for a user provenance")
    if provenance.speaker_kind == "automation":
        raise ValueError(
            "agent_document_id cannot be used for an automation provenance")
    digest = hashlib.sha256(
        ("agent\0" + provenance.speaker_kind + "\0" + (provenance.role_id or "") + "\0"
         + (provenance.persona_id or "") + "\0" + text).encode("utf-8")
    ).hexdigest()
    return f"m-a-{digest[:24]}"
