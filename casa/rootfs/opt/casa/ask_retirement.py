"""What a retired `resident_ask` keyboard is edited to say (#933).

Every question Casa puts on the operator's screen as a DM keyboard is
registered in the broker's ``resident_ask`` namespace, and every one of them is
eventually retired. The broker tells each finish hook exactly how: a TTL that
ran out arrives as ``{"outcome": "no_answer"}`` and everything else arrives as
``{"outcome": "cancelled", "reason": <the caller's reason>}`` — a distinct
reason per cause, ~16 of them, delivered verbatim.

Before this module every hook branched on the outcome KIND alone and composed
one fixed string, so an operator who had just cancelled a reminder, removed a
persona, typed a reply or run ``/new`` was told the question had expired.
``tools.wipe_memory`` collapsed the other way and told an operator who simply
never came back that they had cancelled. Both are the same bug: the cause was
in the hook's hands and thrown away at the last step.

So the reason is rendered here, once, for the whole family. Two shapes, because
the family composes in two places:

* ``retired_body`` — the ``dm:<chat>`` lane, where the keyboard's own text is
  kept and a parenthetical is appended to it.
* ``retirement_headline`` — the ``authz:<chat>`` lane, where the keyboard is
  replaced by a one-line verdict.

**An unrecognised reason renders a truthful generic cancellation, never an
expiry.** That is the property the whole module exists for: the reason
vocabulary is open — a new revocation path invents a new string without
touching this file — so the fallback is the case that actually ships, not the
unreachable one. Saying "cancelled" about a cancellation whose cause we cannot
name is imprecise; saying "expired" about it is false.

Nothing here decides WHETHER to edit, or WHEN. The scheduled path still decides
its text before the ``settling`` write that persists it (INV-JOB-013), the
shutdown cancel still edits nothing on that path, and a settled keyboard still
drops its buttons (INV-TG-007). This module only answers what the text says.
"""
from __future__ import annotations

# (icon, headline label) — an expiry and a cancellation are different events
# and read differently at a glance in a chat list.
_EXPIRED = ("⌛", "Expired")
_WITHDRAWN = ("🚫", "Withdrawn")
_CANCELLED = ("🚫", "Cancelled")

# The default clause for a TTL that ran out. Sites that ask for something more
# precise ("was not approved in time") pass their own.
DEFAULT_EXPIRED_CLAUSE = "has expired"

# reason -> (headline kind, clause). Every key is a reason some production path
# passes to `BROKER.cancel*` for this namespace; the two lanes are listed apart
# because a reason only ever reaches the lane that raises it.
_CAUSES: dict[str, tuple[tuple[str, str], str]] = {
    # dm:<chat> — the scheduled question and the human `ask_user`.
    "trigger_cancelled": (_CANCELLED, "was cancelled along with its reminder"),
    "trigger_removed": (_CANCELLED, "was cancelled when its reminder was removed"),
    "trigger_reloaded": (_CANCELLED, "was cancelled when its trigger was reloaded"),
    "trigger_changed": (_CANCELLED, "was cancelled when its trigger changed"),
    "role_evicted": (_CANCELLED, "was cancelled when its role was removed"),
    "superseded": (_CANCELLED, "was replaced by a newer question"),
    "operator_challenge": (
        _CANCELLED, "was set aside for an approval request in this chat"),
    "typed_answer": (_CANCELLED, "was closed when you replied by text"),
    "operator_changed": (
        _CANCELLED,
        "was cancelled because the operator changed while Casa was down"),
    "operator_busy": (
        _CANCELLED, "was cancelled because another question held the chat"),
    "delivery_unconfirmed": (
        _CANCELLED, "was cancelled because its delivery was never confirmed"),
    "invalid_option": (_CANCELLED, "was cancelled after an unusable answer"),
    # authz:<chat> — the challenge and consent family. `challenge_cancelled` is
    # ONE reason for many causes (a persona removed, an ack revoked, a plugin
    # role reloaded); it says the request was taken back, and deliberately not
    # by what, because `cancel_matching` does not carry that and widening it to
    # say so would be a different change.
    "challenge_cancelled": (_WITHDRAWN, "was withdrawn before it was answered"),
    # Both lanes.
    "new_session": (_CANCELLED, "was cancelled by /new"),
    "casa_shutdown": (_CANCELLED, "was cancelled when Casa shut down"),
}

_GENERIC = (_CANCELLED, "was cancelled")


def _resolve(
    kind: str | None, reason: str | None, expired_clause: str,
) -> tuple[tuple[str, str], str]:
    if kind == "no_answer":
        return _EXPIRED, expired_clause
    # Anything that is not a TTL is a cancellation, including a kind this
    # module has never heard of: an unknown KIND is no more an expiry than an
    # unknown reason is.
    return _CAUSES.get(reason or "", _GENERIC)


def retirement_clause(
    kind: str | None, reason: str | None, *,
    expired_clause: str = DEFAULT_EXPIRED_CLAUSE,
) -> str:
    """The truthful cause as a verb clause: "has expired", "was cancelled by
    /new", "was replaced by a newer question", … Unknown reason ⇒ "was
    cancelled"."""
    return _resolve(kind, reason, expired_clause)[1]


def is_expiry(kind: str | None, reason: str | None) -> bool:
    """Did this question run out of time, as opposed to being retired?"""
    return _resolve(kind, reason, DEFAULT_EXPIRED_CLAUSE)[0] is _EXPIRED


def retired_body(
    body: str, kind: str | None, reason: str | None, *,
    expired_clause: str = DEFAULT_EXPIRED_CLAUSE,
    consequence: str | None = None,
) -> str:
    """The `dm:` lane's edit: the question as posted, plus why it went away."""
    clause = retirement_clause(kind, reason, expired_clause=expired_clause)
    tail = f"{clause} — {consequence}" if consequence else clause
    return f"{body}\n\n(this question {tail})"


def retirement_headline(
    subject: str, kind: str | None, reason: str | None, *,
    expired_clause: str = "was not answered",
    consequence: str | None = None,
) -> str:
    """The `authz:` lane's edit: one line that replaces the keyboard.

    *subject* names what was asked ("persona install consent for 'judge'"),
    *consequence* what follows from nobody answering ("nothing was installed").
    """
    (icon, label), clause = _resolve(kind, reason, expired_clause)
    text = f"{icon} {label} — {subject} {clause}"
    return f"{text}; {consequence}" if consequence else text
