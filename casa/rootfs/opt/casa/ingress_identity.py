# casa/rootfs/opt/casa/ingress_identity.py
"""Declarative ingress identity policy (#203 + #204).

Every EXTERNAL ingress — the transports a request can arrive on from outside
Casa — resolves its per-turn identity here, and nowhere else. The table below
is the single place that answers "who authored a turn that arrived this way?",
so adding an authenticated transport without answering that question is a
compile-of-the-mind impossibility rather than a silent degradation.

**#204 — attribution.** ``/invoke`` and ``/webhook/{name}`` used to stamp
nothing and fall back to the honest-but-blind ``system`` identity. They now
stamp distinct peers:

* ``/invoke`` → ``invoke_caller``
* ``/webhook/{name}`` → ``webhook:{name}`` (per-trigger)

They deliberately do NOT share a peer. Both are gated by an HMAC secret, and the
2026-07-10 operator decision that "the secret IS the trust boundary" governs
*authorization* — ``channel_trust("webhook") == "authenticated"`` is unchanged.
Authorship is a different axis: a shared bearer secret proves possession, never
that a particular human wrote the text. Neither peer is ever a human's. Naming
``/invoke`` inside the ``telegram:`` namespace would attribute caller-supplied
text to a human (memory keys a speaker's words on ``user_peer``).

**The operator has no peer literal.** Every accepted Telegram sender — the
configured operator included — resolves to ``telegram:<sender id>``. What
distinguishes the operator is CLEARANCE, not the peer string: an operator sender
keeps the table's private clearance, any other accepted sender is floored to
public. The route used to name the operator with a hardcoded constant, which put
one person's name in a shipped default and made "who is the operator?" a fact
about this module rather than about ``telegram_chat_id``. Two consequences worth
knowing: the peer is now derived from operator-controlled configuration, so the
boot check can no longer compare it against a literal (it checks the NAMESPACE
instead — see :func:`validate_ingress_identity_table`).

Both automation routes yield ``speaker_kind="automation"`` (see
``speaker_provenance.UserProvenance.from_origin``) — honest about being
externally originated without claiming either a human author or Casa's own
internal authority.

**#203 — failing loudly.** The invariant enforced is "an authenticated ingress
stamps a ``user_peer``", NOT "an authenticated ingress names a human": voice is
``is_authenticated=True`` with ``authenticated_user=None`` by design (an
anonymous but trusted household speaker), so the latter would fail on voice at
boot. Enforcement is two-layer:

* per request — :func:`ingress_identity` RAISES on an unknown route or an
  unresolvable peer, so a handler cannot fall through to ``system``;
* at boot — :func:`validate_ingress_identity_table` fails startup on a
  deterministic table defect (a dropped route, an empty fixed peer, an
  unrecognized strategy). It is pure and directly unit-tested; the caller in
  ``main()`` is one unconditional line.

The assertion deliberately lives at the ingress boundary and NOT in
``Agent._process``. A "channel telegram ⇒ must carry a trusted origin" check
there would break Casa-composed internal turns that legitimately have no human
author — notably the post-consent plugin-setup dispatch (``_setup_dispatch`` in
casa_core, v0.112.0), which sends a ``CHANNEL_IN`` on ``channel="telegram"``
carrying text Casa wrote itself. Those correctly stay ``system``.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Literal

from personality_types import (
    AuthenticatedUser,
    TrustedOrigin,
    TrustedUserOriginInput,
)

# The external transports a turn can arrive on. Finer-grained than the
# ``channel`` string: voice has two transports, and /invoke and /webhook share
# ``channel="webhook"`` while being different origins with different trust
# semantics (operator-signed call vs. untrusted third-party content).
IngressRoute = Literal[
    "telegram", "voice_sse", "voice_ws", "invoke", "webhook_trigger",
]

PeerStrategy = Literal["fixed", "telegram_sender", "webhook_name"]

_CLEARANCES = frozenset({"public", "friends", "family", "private"})

# Mirrors speaker_provenance._FIELD_LIMITS["user_peer"]. A registered trigger
# name is Casa-controlled (an unknown name 404s before dispatch), so this is a
# belt-and-braces bound rather than an untrusted-input gate — but it must RAISE
# rather than truncate: a silently shortened peer is a silently wrong identity.
_PEER_MAX_SCALARS = 256
_PEER_MAX_BYTES = 512

_WEBHOOK_PEER_PREFIX = "webhook:"

# #336: peer namespace for a Telegram sender. With ``telegram_chat_id`` empty
# ("accept all chats"), any Telegram user can reach the telegram route, so
# identity must be per-SENDER — a fixed route-wide peer recorded every
# stranger's turns under the operator's identity and ran them at the operator's
# recall clearance. EVERY sender is named here, the operator included: the
# operator is distinguished by clearance, not by a peer of its own.
_TELEGRAM_PEER_PREFIX = "telegram:"

# Read-clearance for a non-operator Telegram sender. Fail-closed: accept-all
# mode is reachable by anyone who finds the bot, so an unrecognized sender
# reads at the least-sensitive tier only. Exported (with the operator's own
# clearance) because a turn can also begin at a BUTTON TAP, which does not
# pass through :func:`ingress_identity` — the channel stamps the same
# per-sender clearance there, and both entry points must read it from here
# rather than each spelling the tier out.
TELEGRAM_NON_OPERATOR_CLEARANCE = "public"
TELEGRAM_OPERATOR_CLEARANCE = "private"
_TELEGRAM_NON_OPERATOR_CLEARANCE = TELEGRAM_NON_OPERATOR_CLEARANCE


class IngressIdentityError(RuntimeError):
    """An ingress could not be given a trusted identity. Always fatal to the
    turn (or, at boot, to startup) — never downgraded to ``system``."""


@dataclass(frozen=True, slots=True)
class IngressIdentityPolicy:
    """How one ingress route names its author."""
    surface: Literal["telegram", "voice", "invoke", "webhook"]
    authenticated: bool
    clearance: str
    peer_strategy: PeerStrategy
    peer: str | None


_INGRESS_IDENTITY: dict[str, IngressIdentityPolicy] = {
    "telegram": IngressIdentityPolicy(
        # Peer is derived per sender (``telegram:<id>``), so there is no fixed
        # value here — the operator is identified by clearance, not by a name
        # this table knows. ``clearance`` is the OPERATOR's; a non-operator
        # sender is floored in ``ingress_identity``.
        surface="telegram", authenticated=True, clearance="private",
        peer_strategy="telegram_sender", peer=None,
    ),
    "voice_sse": IngressIdentityPolicy(
        surface="voice", authenticated=True, clearance="friends",
        peer_strategy="fixed", peer="voice_speaker",
    ),
    "voice_ws": IngressIdentityPolicy(
        surface="voice", authenticated=True, clearance="friends",
        peer_strategy="fixed", peer="voice_speaker",
    ),
    "invoke": IngressIdentityPolicy(
        surface="invoke", authenticated=True, clearance="private",
        peer_strategy="fixed", peer="invoke_caller",
    ),
    "webhook_trigger": IngressIdentityPolicy(
        # Clearance is per-trigger (trigger_registry.get_clearance); the table
        # carries the public FLOOR that applies when a trigger declares none.
        surface="webhook", authenticated=True, clearance="public",
        peer_strategy="webhook_name", peer=None,
    ),
}

# The contract each shipped route must satisfy, written INDEPENDENTLY of the
# table above so the boot check compares two statements rather than one against
# itself. Coverage alone is not enough: review round 1 (Terra and Sol,
# independently) showed that a table checked only for internal coherence still
# accepts `webhook_trigger -> fixed peer "<a human's peer>"` (every third-party
# trigger recorded as that person) and `invoke -> surface "telegram"` (a machine
# promoted to a person, because from_origin derives the speaker kind from the
# surface). Both are deterministic programming defects, which is exactly what a
# boot check is for.
_ROUTE_CONTRACT: dict[str, tuple[str, bool, str, str | None, str]] = {
    # route: (surface, authenticated, peer_strategy, expected fixed peer,
    #         expected speaker kind)
    #
    # The speaker kind is the OUTCOME the route must produce once its identity
    # has been through ``UserProvenance.from_origin`` — "is this turn recorded
    # as a person or as a machine?". It is declared here rather than derived
    # because deriving it would re-implement the classifier and agree with the
    # bug (see the boot check).
    #
    # Peer is derived per sender, so there is no fixed value to pin — the
    # namespace checks below carry what the literal used to.
    "telegram":        ("telegram", True, "telegram_sender", None, "user"),
    # Voice is deliberately a PERSON: an anonymous but trusted household
    # speaker, not an automation.
    "voice_sse":       ("voice",    True, "fixed", "voice_speaker", "user"),
    "voice_ws":        ("voice",    True, "fixed", "voice_speaker", "user"),
    "invoke":          ("invoke",   True, "fixed", "invoke_caller", "automation"),
    # Peer is derived per trigger, so there is no fixed value to pin.
    "webhook_trigger": ("webhook",  True, "webhook_name", None, "automation"),
}

# The peer namespaces that name a HUMAN sender. This replaces the former
# `_OPERATOR_PEERS` literal set: with the operator resolving to `telegram:<id>`
# there is no constant to compare against, so the property is carried by the
# namespace instead — "no route other than a human's may resolve a peer that
# lives in here". That is strictly WIDER than the old check, which only
# constrained the two automation routes: voice (`voice_speaker`, an anonymous
# household speaker) is now covered too and may not be recorded as a Telegram
# person either.
_HUMAN_PEER_PREFIXES = frozenset({_TELEGRAM_PEER_PREFIX})

# The peer strategy that names a human. Its resolved peer must land INSIDE the
# human namespace; every other strategy's must land outside it.
_HUMAN_PEER_STRATEGY = "telegram_sender"

# The closed set of surfaces this table may declare. The ``Literal`` annotation
# on the field is a type hint, not a runtime gate, so a coordinated edit could
# otherwise introduce a surface no module has been taught — and an unrecognised
# surface is classified as a PERSON by default in
# ``speaker_provenance.from_origin``. The boot check's speaker-kind comparison
# would catch that too; this set exists so the failure names the real cause
# instead of surfacing as a downstream classification mismatch.
_APPROVED_SURFACES = frozenset({"telegram", "voice", "invoke", "webhook"})


def _check_peer(route: str, peer: str) -> str:
    if not peer:
        raise IngressIdentityError(
            f"ingress route {route!r} resolved an empty peer")
    if len(peer) > _PEER_MAX_SCALARS or len(peer.encode("utf-8")) > _PEER_MAX_BYTES:
        raise IngressIdentityError(
            f"ingress route {route!r} resolved a peer exceeding the "
            f"provenance length limit")
    return peer


def ingress_identity(
    route: str,
    *,
    webhook_name: str | None = None,
    clearance: str | None = None,
    sender_id: str | None = None,
    sender_display_name: str | None = None,
    sender_is_operator: bool = False,
) -> TrustedUserOriginInput:
    """Build the server-created trusted ingress identity for *route*.

    Raises :class:`IngressIdentityError` rather than returning anything a
    caller could mistake for "no identity" — that fallback is what #203 exists
    to make impossible. ``clearance`` overrides the table default (used by
    ``/webhook/{name}`` to carry the trigger's declared read-clearance);
    ``sender_id``/``sender_display_name`` supply Telegram's authenticated user.

    ``sender_is_operator`` (#336) is the CHANNEL's server-side determination
    that this Telegram sender is the configured operator (sender id matches
    ``telegram_chat_id``). Every accepted sender gets its own
    ``telegram:<id>`` peer; only an operator sender additionally keeps the
    table's private clearance, any other sender is floored to public, and a
    sender-less telegram turn (anonymous group/channel post) fails loudly
    rather than borrowing someone else's identity.
    """
    policy = _INGRESS_IDENTITY.get(route)
    if policy is None:
        raise IngressIdentityError(f"unknown ingress route {route!r}")

    effective_clearance = policy.clearance if clearance is None else clearance
    if effective_clearance not in _CLEARANCES:
        raise IngressIdentityError(
            f"ingress route {route!r} got an unknown clearance "
            f"{effective_clearance!r}")

    authenticated_user: AuthenticatedUser | None = None
    if policy.peer_strategy == "webhook_name":
        if not webhook_name:
            raise IngressIdentityError(
                f"ingress route {route!r} requires a webhook_name")
        # The peer composed here is NFC-normalized downstream by
        # ``UserProvenance.from_origin``. If two registered trigger names
        # differed only in canonical form they would silently fold onto ONE
        # peer — and so onto one ``automation_document_id``, merging two
        # sources' memories. The shipped trigger schema is ASCII-only, which
        # makes that unreachable today; rejecting here means it stays
        # unreachable if the schema ever widens (Terra, review r1).
        if unicodedata.normalize("NFC", webhook_name) != webhook_name:
            raise IngressIdentityError(
                f"webhook_name {webhook_name!r} is not in canonical (NFC) "
                f"form; two spellings of one name would share an identity")
        peer = _check_peer(route, _WEBHOOK_PEER_PREFIX + webhook_name)
    elif policy.peer_strategy == "telegram_sender":
        # #336: identity is per-SENDER. No sender ⇒ no author ⇒ fail the
        # turn (#203) — the pre-#336 fallback silently attributed anonymous
        # group/channel posts to the operator.
        if not sender_id:
            raise IngressIdentityError(
                f"ingress route {route!r} requires an authenticated sender id")
        authenticated_user = AuthenticatedUser(
            stable_id=sender_id, configured_display_name=sender_display_name,
        )
        # Every sender, operator included, is named by its own id — the
        # operator has no peer literal (see the module docstring). Only the
        # CLEARANCE differs.
        peer = _check_peer(route, _TELEGRAM_PEER_PREFIX + sender_id)
        if not sender_is_operator:
            # Forced, not defaulted: the ``clearance`` override parameter
            # exists for webhook triggers and must never lift a non-operator
            # Telegram sender above the fail-closed floor.
            effective_clearance = _TELEGRAM_NON_OPERATOR_CLEARANCE
    elif policy.peer_strategy == "fixed":
        peer = _check_peer(route, policy.peer or "")
    else:
        raise IngressIdentityError(
            f"ingress route {route!r} declares an unrecognized peer "
            f"strategy {policy.peer_strategy!r}")

    return TrustedUserOriginInput(
        surface=policy.surface,
        server_origin=TrustedOrigin(
            route=policy.surface,
            is_authenticated=policy.authenticated,
            clearance=effective_clearance,
        ),
        authenticated_user=authenticated_user,
        user_peer=peer,
    )


def _classify(policy: IngressIdentityPolicy, peer: str) -> str:
    """The speaker kind a route's identity actually resolves to, obtained from
    the real classifier rather than restated here.

    Imported lazily: ``speaker_provenance`` is a peer module in the import
    graph and only the boot check needs it, so a module-level import would add
    an edge for one caller. The probe supplies the minimum
    ``from_origin`` requires — an automation surface must carry no
    authenticated user, and the anonymous-voice branch is keyed on exactly that
    being absent, so passing None models both honestly.
    """
    from speaker_provenance import UserProvenance

    return UserProvenance.from_origin(
        surface=policy.surface,
        server_origin=TrustedOrigin(
            route=policy.surface, is_authenticated=policy.authenticated,
            clearance=policy.clearance,
        ),
        authenticated_user=None,
        user_peer=peer,
    ).speaker_kind


def validate_ingress_identity_table() -> None:
    """Boot check (#203): fail startup on a deterministic table defect.

    Deliberately NARROW — it proves the table is internally coherent and covers
    every shipped route, nothing more. A boot crash takes Telegram, voice and
    the HA voice pipeline down together, so anything that depends on runtime
    data belongs in :func:`ingress_identity`'s per-request path instead.
    """
    missing = set(_ROUTE_CONTRACT) - set(_INGRESS_IDENTITY)
    if missing:
        raise IngressIdentityError(
            "ingress identity table is missing route(s): "
            + ", ".join(sorted(missing))
        )
    # EXACT equality, not just coverage: a route present in the table but
    # absent from the contract would otherwise skip semantic validation
    # entirely, which is the single-edit bypass the contract exists to close
    # (Sol, re-review r2).
    uncontracted = set(_INGRESS_IDENTITY) - set(_ROUTE_CONTRACT)
    if uncontracted:
        raise IngressIdentityError(
            "ingress route(s) declared with no identity contract: "
            + ", ".join(sorted(uncontracted))
        )
    # The dynamic webhook namespace must be incapable of producing a human's
    # peer. Probing one composed value would prove nothing about the namespace:
    # with an empty prefix a trigger NAMED after a sender's id would resolve to
    # that person's peer itself (Sol, re-review r2).
    if not _WEBHOOK_PEER_PREFIX:
        raise IngressIdentityError(
            "the webhook peer namespace prefix must be non-empty, or a "
            "trigger name could impersonate another peer")
    # #336: same namespace discipline for per-sender Telegram peers — with an
    # empty prefix, a Telegram sender id could collide with (or, degenerately,
    # BE) another peer's name.
    if not _TELEGRAM_PEER_PREFIX:
        raise IngressIdentityError(
            "the telegram peer namespace prefix must be non-empty, or a "
            "sender id could impersonate another peer")
    # The human and webhook namespaces must be DISJOINT, checked as prefixes
    # rather than by probing a composed value: if either prefix began with the
    # other, some trigger name would compose into the human namespace (or a
    # sender id into the webhook one) while a single-value probe still passed.
    # This is what carries the property the removed `_OPERATOR_PEERS` literal
    # used to state, now that the operator's peer is derived from its sender id.
    for human_prefix in _HUMAN_PEER_PREFIXES:
        if human_prefix.startswith(_WEBHOOK_PEER_PREFIX) or (
                _WEBHOOK_PEER_PREFIX.startswith(human_prefix)):
            raise IngressIdentityError(
                f"the human peer namespace {human_prefix!r} and the webhook "
                f"namespace {_WEBHOOK_PEER_PREFIX!r} are not disjoint; a "
                f"caller-derived name could claim a person's identity")

    for route, policy in _INGRESS_IDENTITY.items():
        if policy.surface not in _APPROVED_SURFACES:
            raise IngressIdentityError(
                f"ingress route {route!r} declares surface {policy.surface!r}, "
                f"which is not one of {sorted(_APPROVED_SURFACES)}; an "
                f"unrecognised surface is classified as a person by default")
        if policy.clearance not in _CLEARANCES:
            raise IngressIdentityError(
                f"ingress route {route!r} declares an unknown clearance "
                f"{policy.clearance!r}")
        if policy.peer_strategy not in ("fixed", "telegram_sender", "webhook_name"):
            raise IngressIdentityError(
                f"ingress route {route!r} declares an unrecognized peer "
                f"strategy {policy.peer_strategy!r}")

        (surface, authenticated, peer_strategy, expected_peer,
         expected_kind) = _ROUTE_CONTRACT[route]
        if expected_peer is not None and policy.peer != expected_peer:
            raise IngressIdentityError(
                f"ingress route {route!r} must resolve peer "
                f"{expected_peer!r}, not {policy.peer!r}")
        if policy.surface != surface:
            raise IngressIdentityError(
                f"ingress route {route!r} must stamp surface {surface!r}, "
                f"not {policy.surface!r} — the surface decides whether a "
                f"turn is recorded as a person or an automation")
        if policy.authenticated != authenticated:
            raise IngressIdentityError(
                f"ingress route {route!r} must declare "
                f"authenticated={authenticated}")
        if policy.peer_strategy != peer_strategy:
            raise IngressIdentityError(
                f"ingress route {route!r} must use the {peer_strategy!r} "
                f"peer strategy, not {policy.peer_strategy!r}")

        # Prove the strategy actually resolves to a usable peer. A dynamic
        # peer is probed with a stand-in name.
        if policy.peer_strategy == "webhook_name":
            peer = _WEBHOOK_PEER_PREFIX + "probe"
        elif policy.peer_strategy == _HUMAN_PEER_STRATEGY:
            peer = _TELEGRAM_PEER_PREFIX + "probe"
        else:
            peer = policy.peer or ""
        _check_peer(route, peer)
        # Both directions, because each catches a different defect. A human
        # route landing OUTSIDE the human namespace means senders are being
        # named by something other than their own id — the regression this
        # change exists to prevent coming back. A non-human route landing
        # INSIDE it means a machine or an anonymous speaker is being recorded
        # as a person, which is what the old operator-peer guard caught.
        in_human_namespace = any(
            peer.startswith(prefix) for prefix in _HUMAN_PEER_PREFIXES)
        if policy.peer_strategy == _HUMAN_PEER_STRATEGY and not in_human_namespace:
            raise IngressIdentityError(
                f"ingress route {route!r} names a human sender but resolves "
                f"peer {peer!r} outside the human namespace "
                f"{sorted(_HUMAN_PEER_PREFIXES)}")
        if policy.peer_strategy != _HUMAN_PEER_STRATEGY and in_human_namespace:
            raise IngressIdentityError(
                f"ingress route {route!r} resolves peer {peer!r} inside the "
                f"human peer namespace; a machine caller or an anonymous "
                f"speaker must never be recorded as a person")

        # The namespace checks above bound what a peer may be NAMED. They say
        # nothing about whether the turn is recorded as a person, because
        # ``UserProvenance.from_origin`` classifies on the SURFACE, not the
        # peer — so a machine route wearing a person's surface passes all of
        # them and is still persisted as a user.
        #
        # Two rounds of review found two different arrangements of exactly that
        # shape (`invoke` wearing "telegram"; then `invoke` wearing an unknown
        # surface, and `invoke` wearing "voice"), which is the signal that
        # enumerating arrangements is the wrong instrument. So this does not
        # reason about surfaces at all: it runs the route's identity through the
        # REAL classifier and compares the resulting speaker kind with the one
        # the contract declares. Any arrangement that misclassifies a route now
        # fails here, including ones nobody has thought of — and re-implementing
        # the classification locally would have reproduced the bug instead of
        # catching it.
        provenance_kind = _classify(policy, peer)
        if provenance_kind != expected_kind:
            raise IngressIdentityError(
                f"ingress route {route!r} is classified as "
                f"{provenance_kind!r} but its contract declares "
                f"{expected_kind!r}; the surface decides whether a turn is "
                f"recorded as a person, so this route would persist under the "
                f"wrong speaker kind")

        # Kept alongside the outcome check because it protects a different
        # property: that senders are named INDIVIDUALLY. A telegram route
        # demoted to a fixed peer still classifies as "user" — correctly — while
        # recording every sender under one shared identity, which is the
        # regression the operator-peer collapse exists to prevent.
        if (policy.surface == "telegram") != (
                policy.peer_strategy == _HUMAN_PEER_STRATEGY):
            raise IngressIdentityError(
                f"ingress route {route!r} declares surface "
                f"{policy.surface!r} with peer strategy "
                f"{policy.peer_strategy!r}; the telegram surface and the "
                f"{_HUMAN_PEER_STRATEGY!r} strategy must imply each other, or "
                f"senders stop being named individually")
