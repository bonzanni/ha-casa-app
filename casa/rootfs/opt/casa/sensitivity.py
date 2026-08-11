# casa/rootfs/opt/casa/sensitivity.py
"""Sensitivity-tier access vocabulary for long-term memory
(design 2026-06-03-tiered-memory-access-design §2).

Tiers are an access ladder, ascending sensitivity:
    public  <  friends  <  family  <  private
A context's CLEARANCE is the highest tier it may read; it reads its own tier and all
LESS-sensitive tiers. Facts are tagged with a tier; recall filters to tiers <= clearance.
Retrieval relevance is Hindsight's job (semantic) — these tiers are purely access control.
"""
from __future__ import annotations

import re

# Ascending sensitivity. Index = sensitivity rank (higher = more private).
TIERS: tuple[str, ...] = ("public", "friends", "family", "private")

# Leak-safe default when a turn is unlabeled on a classified channel (design §2.4).
DEFAULT_TIER = "private"

# Channel -> read clearance. Explicit for every real ingress so the grant is a
# DECISION, not an accident of the fallback (X2, 2026-07-09):
#   telegram = private — the operator's authenticated DM, fully trusted.
#   voice    = friends  — people in the home, not the open public; a future
#                         speaker-ID upgrade can lift a recognised speaker higher.
#   webhook  = private — /invoke + /webhook are gated by the HMAC secret;
#                        per operator decision (2026-07-09) the secret IS the
#                        trust boundary, so a holder reads at full clearance
#                        like the DM.
CLEARANCE_BY_CHANNEL: dict[str, str] = {
    "telegram": "private",
    "voice": "friends",
    "webhook": "private",
}
# Fail-CLOSED default (2026-07-10): a genuinely UNMAPPED channel reads at the
# LEAST-sensitive tier only. Every real ingress is explicitly mapped above, so
# this affects only unknown/future channels and the rare orphan-recovery
# NOTIFICATION replayed with channel="" (origin.get("channel","")) — which then
# reads at public clearance (fail-safe; if orphan recovery ever needs more,
# make engagement origins always carry their channel rather than reopening the
# fail-open default). Was "private" (fail-OPEN) — see X2, cross-surface sweep.
_DEFAULT_CLEARANCE = "public"


def _rank(tier: str) -> int:
    return TIERS.index(tier)


def readable_tiers(clearance: str) -> list[str]:
    """Tiers a context at ``clearance`` may read: its own tier + all less sensitive."""
    ceiling = _rank(clearance)
    return [t for t in TIERS if _rank(t) <= ceiling]


def apply_ceiling(tier: str, ceiling: str) -> str:
    """Cap ``tier`` so it is no more sensitive than ``ceiling`` (the channel ceiling)."""
    return tier if _rank(tier) <= _rank(ceiling) else ceiling


def clearance_for_channel(channel: str) -> str:
    """Clearance for a channel. FAIL-CLOSED: an unmapped channel reads at the
    LEAST-sensitive tier (public) — see _DEFAULT_CLEARANCE (X2, 2026-07-10)."""
    return CLEARANCE_BY_CHANNEL.get(channel, _DEFAULT_CLEARANCE)


# Memory read-clearance a webhook trigger may declare (spec A1/A4). Never
# "private" — a webhook-origin turn carries third-party content and must not
# unlock the operator's most sensitive memories.
_WEBHOOK_TRIGGER_TIERS = frozenset({"public", "friends", "family"})


def clearance_for_origin(
    origin_route: str | None,
    origin_clearance: str | None,
    channel: str,
) -> str:
    """Read-clearance keyed off the unspoofable, server-set origin marker
    rather than the channel string (spec A0/A4).

    ``webhook_trigger`` turns read at their declared ``origin_clearance``
    (default-deny: a missing/malformed/``private`` value ⇒ ``public``).
    ``invoke`` turns are operator-signed and read at ``private`` (the
    documented authenticated exception). ``telegram`` turns (#336) read at
    the per-sender clearance the ingress stamped — private only for the
    configured operator; a missing/malformed stamp on a telegram-marked turn
    fails CLOSED to ``public``, never through to the channel's private. Any
    other origin falls through to today's channel-keyed clearance so
    existing surfaces are unchanged.
    """
    if origin_route == "invoke":
        return "private"
    if origin_route == "webhook_trigger":
        if origin_clearance in _WEBHOOK_TRIGGER_TIERS:
            return origin_clearance
        return "public"
    if origin_route == "telegram":
        if origin_clearance in TIERS:
            return origin_clearance
        return "public"
    # No trusted origin marker. Fail closed on the webhook channel (Sol r4):
    # private there is granted ONLY for an explicit server-stamped ``invoke`` —
    # a missing/unknown route must not inherit webhook's historical private
    # clearance. Non-webhook channels keep today's channel-keyed behavior.
    if channel == "webhook":
        return "public"
    return clearance_for_channel(channel)


# ---------------------------------------------------------------------------
# LLM output parsing (design §2.4)
# ---------------------------------------------------------------------------

# #350: the reply must be EXACTLY ONE tier token. The classifier prompt asks
# for a single word; a leftmost-match search would extract "public" out of
# "This is not public; it is family" and tag a family fact public — silently
# defeating the leak-safe default the module contract promises. Decoration
# that a single-word answer still legitimately carries (a "Tier:" label,
# punctuation, markdown emphasis, whitespace) is tolerated; ANY other word in
# the reply makes it ambiguous, and ambiguity must fall to DEFAULT_TIER.
# The optional "Tier" label must be followed by a REAL separator (Sol, review
# r1): with a zero-width one, "tierpublic" parsed as public — a malformed
# reply silently classified at the least-sensitive tier instead of taking the
# private default.
_TIER_REPLY_RE = re.compile(
    r"^[\W_]*(?:tier[\W_]+)?(private|family|friends|public)[\W_]*$",
    re.IGNORECASE,
)

# #497: the bundled CLI/model started replying verbosely (286–803 chars), so
# the whole-reply match above never fired and 100% of retentions defaulted to
# ``private`` — the tier system was effectively disabled. The prompt now
# mandates that a non-single-word reply END with a line of exactly
# ``Tier: <word>``; this pattern accepts that answer line and nothing looser
# (review r1, Sol+Terra): the LITERAL, undecorated form only — the colon is
# mandatory and immediately follows the label, so ``tier public``,
# ``Tier-public``, ``Tier---public``, markdown-wrapped and quoted variants
# all stay ambiguous. A bare tier word that merely ends a chatty reply stays
# ambiguous too, preserving the #350 guarantee that tier words inside prose
# never parse. "Tier: family or private" and "Tier: public would be wrong"
# do not match.
_TIER_ANSWER_LINE_RE = re.compile(
    r"^tier:\s+(private|family|friends|public)$",
    re.IGNORECASE,
)

# Review r2 (Sol S1): a MALFORMED label line before the answer line
# ("Tier: private." then "Tier: public") is a conflict the exact-match count
# missed — the strict answer regex ignores it, silently letting the later
# line win. Any earlier line that so much as OPENS with a Tier label makes
# the reply ambiguous. \b keeps prose starting with words like "Tiering"
# out of scope; prose lines merely CONTAINING "tier" are not labels.
_TIER_LABELISH_RE = re.compile(r"^[\W_]*tier\b", re.IGNORECASE)


def parse_tier(text: str | None) -> str | None:
    """Parse an LLM/agent reply that should name a single tier. Returns the
    lowercased tier only when either (a) the reply is a SINGLE non-empty line
    that is exactly one tier token (modulo an optional "Tier:" label and
    surrounding punctuation / whitespace), or (b) the reply is multi-line,
    its FINAL non-empty line is the literal ``Tier: <word>`` answer line
    (#497 — the declared-answer line the prompt mandates), and no earlier
    line opens with a Tier label (review r2: a malformed or conflicting
    label line is ambiguity). None otherwise — including when tier words
    appear inside a longer sentence (#350), an unlabeled multi-line reply,
    or a decorated token spread across lines (review r2: the whole-reply
    arm's separator classes must never span newlines). The caller applies
    DEFAULT_TIER on None."""
    if not isinstance(text, str):
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    if len(lines) == 1:
        m = _TIER_REPLY_RE.match(lines[0])
        return m.group(1).lower() if m else None
    m = _TIER_ANSWER_LINE_RE.match(lines[-1])
    if not m:
        return None
    if any(_TIER_LABELISH_RE.match(ln) for ln in lines[:-1]):
        return None
    return m.group(1).lower()


# Classification prompt — converged with the maintainer via the eval session (2026-06-03).
# The eval set (tests/fixtures/sensitivity_eval.jsonl) is the regression source of truth;
# keep this prompt and that set in sync. Calibration note: `friends` is the BROAD default —
# do not over-escalate.
SENSITIVITY_PROMPT = """\
You assign a single fact about the user (or their household) to ONE access tier, deciding
WHO the user would let recall it later. Judge by "who would the user naturally share this
with?", NOT by topic.

Tiers, most to least sensitive:
- private  — only the user. Money/income/expenses (even household — treat finances as
             private); medical diagnoses, treatments, medications, mental-health matters;
             personal-account credentials (email, bank); intimate or relationship matters;
             undisclosed or in-progress personal decisions; identity-document-level details.
- family   — NARROW. Secrets/credentials for a SHARED SPACE the household relies on but
             friends should not have: the home alarm/disarm code, the MAIN wifi password.
             These are family — NOT private (they are not personal-account logins) and NOT
             friends (the main network/alarm is not guest-facing). Also genuinely
             family-internal sensitive matters (e.g. a relative's private difficulty).
             NOT general household logistics, and NOT money.
- friends  — the DEFAULT for ordinary, mildly-personal, socially-shareable facts: preferences
             (thermostat at 20°C), travel/whereabouts and holiday plans, kids' names/ages,
             birthdays, allergies and other safety info, everyday household logistics (school
             pickup times, visitors, weekly dinners), the guest wifi, pets. Anyone the user
             talks to in the home is friends-or-closer.
- public   — impersonal, general-knowledge, harmless to anyone: bin-collection day, local
             shop hours, the make/model/brand of a household device (thermostat, tap,
             appliance — a brand name is not personal), the user's professional role/employer,
             the colour the living room is painted.

Rules:
1. Judge "who would the user share this with?" — not the topic. A topic is not a tier
   (e.g. medical: an allergy is safety info → friends/public; a diagnosis/medication/
   mental-health matter → private).
2. Money and finances are private, even when household-scoped — amounts, accounts, AND
   invoicing/billing patterns or habits (how the user bills, what lines they put on invoices).
3. Tier a secret by what it protects: a PERSONAL-ACCOUNT credential (email/bank login) →
   private; a SHARED-SPACE household secret (alarm code, MAIN wifi password) → family; a
   GUEST-facing secret (guest wifi) → friends.
4. PII that could verify identity or enable social engineering (e.g. birthdate) is at
   least friends — never public.
5. Undisclosed / in-progress personal matters lean more private.
6. Do NOT over-escalate: friends is the right home for most personal-but-shareable facts.
   Reserve family for shared-space secrets / family-internal sensitive matters, and private
   for genuinely sensitive things.
7. Only when you are genuinely unsure after applying the above, choose the more private
   tier (a leak is worse than forgetting).

Respond with ONLY the single tier word: private, family, friends, or public.
Do not explain, hedge, or add anything else. If you nonetheless write anything besides the
single word, your reply MUST end with a final line of exactly:
Tier: <word>
where <word> is one of: private, family, friends, public. A reply without that final line
is discarded and the fact is filed at the most restrictive tier.
Fact:
"""
