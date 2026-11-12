"""v0.81.0 (W-R3, Sol r1-5) — readable ask buttons; v0.84.0 (round 4, spec D2)
— the button-label half moved to whole-set floor/verbatim semantics.

The original bug: an ask keyboard used each FULL option string as the button
label, which Telegram truncates to a few words → most choices were unpickable.
The fix (still current): ``render_ask_body`` renders the full options VERBATIM
+ numbered in the MESSAGE body, and the buttons carry a resolved caption whose
``callback_data`` identity stays the option INDEX.

Button labels are MODEL-generated: the agent supplies an optional ``short`` per
option, resolved with WHOLE-SET semantics by ``resolve_button_labels`` (see
``tests/test_ask_button_labels.py`` for its exhaustive coverage) — no agent
shorts anywhere always floors to the numbered placeholder.

These tests assert the floored/verbatim captions line up with the body's
numbered lines, and that the SEPARATE authz-challenge renderer is untouched.
"""

from __future__ import annotations

from channels.telegram import render_ask_body


# ---------------------------------------------------------------------------
# short_option_labels / resolve_button_labels — resolved button labels
# ---------------------------------------------------------------------------


class TestShortOptionLabel:
    """v0.84.0 (round 4, D2): the per-option elision ladder is gone; these pin
    ``short_option_labels``'s whole-set floor/verbatim behaviour for the
    exact shapes this file used to probe with the deleted
    ``_short_option_label``. See ``tests/test_ask_button_labels.py`` for the
    resolver's own exhaustive coverage."""

    def test_short_option_verbatim_when_short_given(self) -> None:
        from channels.telegram import short_option_labels

        assert short_option_labels(["Personal Gmail"], ["Gmail"]) == [
            "1 · Gmail"]

    def test_no_short_floors_to_numbered_placeholder(self) -> None:
        # v0.84.0 (D2): no agent-supplied short → the WHOLE set floors; it
        # never elides the full option text into a garbled summary.
        from channels.telegram import short_option_labels

        label = short_option_labels(
            ["Configure the enterprise SSO integration"])[0]
        assert label == "Option 1"

    def test_number_matches_body_line(self) -> None:
        # The floor's number is the 1-based option position, matching the
        # numbered line in render_ask_body so the operator can
        # cross-reference even without an agent-supplied short.
        from channels.telegram import short_option_labels

        options = ["Personal Gmail, the one you use every day",
                   "Work Outlook, through the company tenant"]
        body = render_ask_body(1, "Which?", options)
        labels = short_option_labels(options)
        for i, opt in enumerate(options):
            assert labels[i] == f"Option {i + 1}"
            assert f"{i + 1}. {opt}" in body

    def test_caption_within_64_char_cap_when_short_given(self) -> None:
        from channels.telegram import _ASK_BUTTON_CAPTION_CAP, short_option_labels

        label = short_option_labels(["Full option text"], ["SSO"])[0]
        assert label == "1 · SSO"
        assert len(label) <= _ASK_BUTTON_CAPTION_CAP


# ---------------------------------------------------------------------------
# The SEPARATE authz-challenge renderer is UNTOUCHED
# ---------------------------------------------------------------------------


def test_authz_challenge_renderer_untouched() -> None:
    """An authz challenge posts an approval card (``approval_card``) via
    post_dm_keyboard, measured for size by its own
    ``render_challenge_message`` — a SEPARATE path that must not share the ask
    body/label code changed here."""
    import approval_card
    import authz_grants

    # The authz challenge's measure and its card renderer exist and are
    # independent of the ask body.
    assert hasattr(authz_grants, "render_challenge_message")
    assert authz_grants.render_challenge_message.__module__ == "authz_grants"
    assert approval_card.render_card.__module__ == "approval_card"
    # Neither imports or depends on the ask-body helpers.
    for module in (authz_grants, approval_card):
        assert not hasattr(module, "render_ask_body")
        assert not hasattr(module, "_short_option_label")
        assert not hasattr(module, "short_option_labels")
