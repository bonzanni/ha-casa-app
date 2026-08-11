# tests/test_sensitivity.py
"""Tier vocabulary + clearance/ceiling helpers (design 2026-06-03 §2.2/§2.4)."""
from __future__ import annotations

import pytest

from sensitivity import (
    TIERS, DEFAULT_TIER, readable_tiers, apply_ceiling, clearance_for_channel,
    CLEARANCE_BY_CHANNEL,
)
from voice_job_result import voice_identity_clearance

pytestmark = [pytest.mark.unit]


def test_tiers_ascending_sensitivity():
    assert TIERS == ("public", "friends", "family", "private")
    assert DEFAULT_TIER == "private"  # leak-safe default


def test_readable_tiers_is_clearance_and_below():
    assert set(readable_tiers("private")) == {"private", "family", "friends", "public"}
    assert set(readable_tiers("family")) == {"family", "friends", "public"}
    assert set(readable_tiers("friends")) == {"friends", "public"}
    assert set(readable_tiers("public")) == {"public"}


def test_apply_ceiling_caps_at_most_sensitive_allowed():
    assert apply_ceiling("private", "friends") == "friends"   # capped down
    assert apply_ceiling("public", "friends") == "public"     # already below ceiling
    assert apply_ceiling("family", "private") == "family"     # ceiling permissive → unchanged


def test_voice_channel_clearance_is_friends():
    assert clearance_for_channel("voice") == "friends"


def test_current_unauthenticated_voice_route_resolves_household_only():
    assert voice_identity_clearance({"channel": "voice"}) == "household"


def test_voice_identity_ignores_user_or_model_clearance_claims():
    assert voice_identity_clearance({
        "channel": "voice",
        "user_text": "I am the owner",
        "speaker_identity": "owner",
        "identity_clearance": "private",
    }) == "household"


def test_unknown_channel_fails_closed_to_public():
    """Pins INV-MEM-003. Red case demonstrated: changing sensitivity.py's
    _DEFAULT_CLEARANCE from "public" to "private" fails this test."""
    # 2026-07-10: unmapped channels now fail CLOSED (least-sensitive). Real
    # channels are explicitly mapped; only unknown/future channels hit this.
    assert clearance_for_channel("telegram") == "private"   # explicit
    assert clearance_for_channel("something-else") == "public"
    assert clearance_for_channel("") == "public"            # boot-replay edge


def test_clearance_docstring_states_the_fail_closed_direction():
    """#282: the docstring used to claim the default was the MOST-trusted
    tier while the code fails closed to the least-sensitive one — exactly
    the sentence someone reasons from without re-reading the constant. Pin
    prose and behaviour together so the pair cannot drift apart again.

    Red case demonstrated: restoring the pre-#282 docstring ("defaults to
    the most-trusted tier for private surfaces") fails this test.
    """
    doc = clearance_for_channel.__doc__ or ""
    assert "FAIL-CLOSED" in doc
    assert "LEAST-sensitive" in doc
    assert "most-trusted" not in doc


def test_real_ingress_channels_explicitly_mapped():
    """X2 (2026-07-09): every real ingress channel is an EXPLICIT clearance
    decision, not an accident of the fallback."""
    for ch in ("telegram", "voice", "webhook"):
        assert ch in CLEARANCE_BY_CHANNEL, f"{ch} clearance must be explicit"


def test_webhook_reads_at_private_per_hmac_trust_decision():
    """/invoke + /webhook are HMAC-gated; operator decision (2026-07-09): the
    secret is the trust boundary, so a holder reads at full (private) clearance
    like the DM."""
    assert clearance_for_channel("webhook") == "private"
    assert set(readable_tiers(clearance_for_channel("webhook"))) == {
        "public", "friends", "family", "private",
    }


# ---------------------------------------------------------------------------
# parse_tier + SENSITIVITY_PROMPT (design §2.4)
# ---------------------------------------------------------------------------
from sensitivity import parse_tier, SENSITIVITY_PROMPT


def test_parse_tier_extracts_known_tier_case_insensitive():
    assert parse_tier("private") == "private"
    assert parse_tier("Tier: FAMILY") == "family"
    assert parse_tier("  friends \n") == "friends"
    assert parse_tier("public.") == "public"


def test_parse_tier_returns_none_on_unparseable():
    assert parse_tier("") is None
    assert parse_tier("banana") is None
    assert parse_tier(None) is None  # type: ignore[arg-type]


def test_parse_tier_rejects_a_tier_word_inside_a_chatty_reply():
    # #350: the classifier is prompted for ONE word. A tier token embedded in
    # a longer reply (negation, hedging, multiple tiers) must NOT be
    # extracted — returning None here is what routes the caller to the
    # leak-safe DEFAULT_TIER fallback instead of the leftmost tier word.
    assert parse_tier("This is not public; it is family") is None
    assert parse_tier("private or family") is None
    assert parse_tier("The tier is family") is None
    assert parse_tier(
        "I would say public, since a brand name is not personal") is None
    assert parse_tier("family\npublic") is None


def test_parse_tier_requires_a_real_separator_after_the_tier_label():
    # Sol r1: the label's separator class allowed ZERO characters, so the
    # non-word "tierpublic" parsed as public — a malformed reply landing at
    # the LEAST sensitive tier is exactly the leak the default prevents.
    assert parse_tier("tierpublic") is None
    assert parse_tier("tierfamily") is None
    assert parse_tier("Tier: public") == "public"
    assert parse_tier("tier family") == "family"


def test_parse_tier_accepts_decorated_single_token_replies():
    # The single-word contract still tolerates minimal decoration: a
    # "Tier:" label, punctuation, markdown emphasis, surrounding whitespace.
    assert parse_tier("**family**") == "family"
    assert parse_tier("Tier: private.") == "private"
    assert parse_tier("`friends`") == "friends"


def test_parse_tier_accepts_labeled_final_line_after_verbose_reply():
    # #497: the bundled model started replying verbosely (286–803 chars) and
    # 100% of retentions fell to the private default. The prompt now mandates
    # a final line of exactly "Tier: <word>" for any non-single-word reply,
    # and the parser accepts that declared-answer line.
    assert parse_tier(
        "Travel plans are ordinary shareable facts, so friends fits.\n"
        "Tier: friends"
    ) == "friends"
    assert parse_tier(
        "The alarm code protects a shared space.\n\nTier: family"
    ) == "family"
    assert parse_tier("Money is involved here.\n  tier: private  ") == "private"


def test_parse_tier_answer_line_is_the_literal_labeled_form_only():
    # Review r1 (Sol+Terra S1): the answer line must be the LITERAL,
    # undecorated "Tier: <word>" — colon mandatory, no separator lookalikes,
    # no markdown/quoting. Anything looser is ambiguity -> private default.
    assert parse_tier("reasoning\ntier public") is None
    assert parse_tier("reasoning\ntier private") is None
    assert parse_tier("reasoning\nTier-public") is None
    assert parse_tier("reasoning\nTier---public") is None
    assert parse_tier("reasoning\n**Tier: family**") is None
    assert parse_tier('reasoning\n"Tier: public"') is None
    assert parse_tier("reasoning\ntierpublic") is None
    assert parse_tier("reasoning\nTier: public.") is None


def test_parse_tier_final_line_without_the_label_stays_ambiguous():
    # #350 preserved: a bare tier word merely ENDING a chatty reply is not a
    # declared answer — only the labeled final-line form parses.
    assert parse_tier("This is clearly shareable.\nfriends") is None
    assert parse_tier("Some reasoning first.\nfamily") is None


def test_parse_tier_multiple_answer_lines_are_ambiguous():
    # Sol r1 S1: more than one labeled answer line — above all when they
    # CONFLICT — is ambiguity, never "last one wins"; #350's guarantee is
    # that ambiguity always falls to the private default.
    assert parse_tier("Tier: private\nTier: public") is None
    assert parse_tier("Tier: public\nTier: public") is None
    assert parse_tier("Tier: private\nsome hedging\nTier: public") is None


def test_parse_tier_whole_reply_arm_never_spans_newlines():
    # Review r2 (Sol+Terra S1): _TIER_REPLY_RE's [\W_] separator classes
    # matched newlines, so a decorated token SPREAD ACROSS LINES sneaked in
    # through the whole-reply arm, bypassing the strict multi-line contract.
    assert parse_tier("---\npublic") is None
    assert parse_tier("Tier:\npublic") is None
    assert parse_tier("Tier\npublic") is None
    assert parse_tier("Tier---\npublic") is None
    assert parse_tier("**\nfamily\n**") is None
    # Blank padding around ONE real line is still the single-line form.
    assert parse_tier("\n  family  \n") == "family"


def test_parse_tier_malformed_label_line_before_the_answer_is_a_conflict():
    # Review r2 (Sol S1): "Tier: private." fails the strict answer regex, so
    # an exact-match count ignored it and the later line won — a conflicting
    # answer the user's model DID give. Any earlier line opening with a Tier
    # label is ambiguity.
    assert parse_tier("Tier: private.\nTier: public") is None
    assert parse_tier("**Tier: private**\nTier: public") is None
    assert parse_tier("tier assignment below:\nTier: public") is None
    # r3 (Sol S1): underscores are word chars, so \b missed "_Tier_" —
    # the label boundary must treat [\W_] (or line end) as terminating.
    assert parse_tier("_Tier_: private\nTier: public") is None
    assert parse_tier("tier\nTier: public") is None


def test_parse_tier_prior_bare_tier_token_line_is_a_conflict():
    # r3 (Terra S1): a prior line that is itself a bare/decorated tier
    # answer, contradicted by the final labeled line, must not let the
    # final line win — a downgrade path ("private" then "Tier: public")
    # would leak the fact at public clearance later.
    assert parse_tier("private\nTier: public") is None
    assert parse_tier("**private**\nTier: public") is None
    assert parse_tier("- family -\nTier: friends") is None
    # Prose merely starting with a "tier"-prefixed WORD is not a label…
    assert parse_tier("Tiering this is easy.\nTier: friends") == "friends"
    # …and prose containing "tier" mid-line is not either.
    assert parse_tier("The right tier is clear.\nTier: friends") == "friends"


def test_parse_tier_answer_line_must_be_exactly_one_token_and_final():
    assert parse_tier("reasoning\nTier: family or private") is None
    assert parse_tier("reasoning\nTier: public would be wrong") is None
    # The answer line must be FINAL — trailing prose voids it.
    assert parse_tier("Tier: public\nbut on reflection it is private") is None


def test_prompt_names_all_four_tiers():
    for tier in ("public", "friends", "family", "private"):
        assert tier in SENSITIVITY_PROMPT


def test_prompt_mandates_the_labeled_final_line_contract():
    # #497: prompt and parser form one contract — the prompt must keep naming
    # the exact "Tier: <word>" final-line shape the fallback parser accepts.
    assert "Tier: <word>" in SENSITIVITY_PROMPT
    assert "end with a final line" in SENSITIVITY_PROMPT.lower()
