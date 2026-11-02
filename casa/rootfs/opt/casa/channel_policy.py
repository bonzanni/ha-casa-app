# casa/rootfs/opt/casa/channel_policy.py
"""Per-channel WRITE-TRUST policy (design §2.4, revised 2026-06-04).

Distinct from read-clearance (sensitivity.clearance_for_channel): write-trust is
whether a channel's speaker is authenticated enough to PERSIST facts into the
trusted ``casa`` bank. Voice has no speaker recognition yet, so it is recall-only
(it must not be able to poison the store with a guest's words / a friend's joke).

Future seam: when voice speaker-recognition exists, a recognised speaker flips
voice to writable at that speaker's clearance — change this one predicate."""
from __future__ import annotations

# Channels whose speaker is authenticated and may write the trusted bank.
#
# SECURITY INVARIANT (Release A / spec A4): "webhook" is deliberately absent, so
# BOTH /invoke and webhook_trigger turns are recall-only for writes and
# third-party webhook content is never captured into memory. This is why the
# webhook-origin store
# machinery (sticky contamination bit, atomic claim snapshot) is NOT needed.
# Adding "webhook" here (granting invoke/webhook write-trust) REQUIRES first
# adding an origin-aware store-deny keyed on _origin_route=="webhook_trigger" —
# see the Task 8+9 restricted-runtime design (Sol+Terra r5).
_WRITABLE_CHANNELS: frozenset[str] = frozenset({"telegram"})


def writes_to_bank(channel: str) -> bool:
    """True iff facts said on ``channel`` may be persisted to the shared ``casa``
    bank. Unknown channels default to False (leak-safe: do not persist what we
    cannot vouch for)."""
    return channel in _WRITABLE_CHANNELS
