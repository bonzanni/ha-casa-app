"""v0.83.0 (A4 · F-BTN) / v0.84.0 (round 4, spec D2) — button labels.

Two halves:

  1. PURE-FUNCTION tests of ``short_option_labels`` (the ``labels``/``shorts``
     convenience wrapper over ``resolve_button_labels`` — see
     ``tests/test_ask_button_labels.py`` for the resolver's own exhaustive
     whole-set/floor/telemetry coverage). v0.83.0's per-option ELISION LADDER
     (``_elide_one``/``_resolve_label_collisions``/``_short_option_label``) is
     GONE (D2): labels are now MODEL-generated with WHOLE-SET floor/verbatim
     semantics — either every option has a usable short and the whole set
     renders ``n · <short>`` verbatim, or the whole set floors to
     ``Option 1``, ``Option 2``, … . These tests pin that behaviour for
     ``short_option_labels`` specifically (name/signature PINNED — resident
     ``ask_user`` depends on it via ``post_dm_keyboard(short_labels=True)``).

  2. PURE-FUNCTION tests of ``render_ask_body`` / ``_canonical_question`` —
     the single canonical ask body and its verbatim question.
"""

from __future__ import annotations

from channels.telegram import (
    _ASK_BUTTON_CAPTION_CAP,
    _canonical_question,
    render_ask_body,
    short_option_labels,
)


# ===========================================================================
# Pure-function: short_option_labels — whole-set floor/verbatim
# ===========================================================================


class TestAllShortsUsable:
    def test_verbatim_captions_when_every_short_usable(self) -> None:
        labels = ["Personal Gmail", "Work Outlook", "Yahoo Mail"]
        shorts = ["Gmail", "Outlook", "Yahoo"]
        assert short_option_labels(labels, shorts) == [
            "1 · Gmail", "2 · Outlook", "3 · Yahoo"]

    def test_single_option_with_short(self) -> None:
        assert short_option_labels(["Personal Gmail"], ["Gmail"]) == [
            "1 · Gmail"]


class TestWholeSetFloors:
    # #1386: an option without a usable short offers its own label; the
    # whole set floors only when the words do not fit (or collide / are blank).
    _WIDE = ["Personal Gmail, the one you use every day",
             "Work Outlook, through the company tenant"]

    def test_no_shorts_short_labels_render_own_words(self) -> None:
        labels = ["Personal Gmail", "Work Outlook"]
        assert short_option_labels(labels) == ["Personal Gmail", "Work Outlook"]

    def test_no_shorts_wide_labels_floor_whole_set(self) -> None:
        assert short_option_labels(self._WIDE) == ["Option 1", "Option 2"]

    def test_partial_shorts_borrow_own_label_numbered(self) -> None:
        labels = ["Personal Gmail", "Work Outlook"]
        shorts = ["Gmail", None]
        assert short_option_labels(labels, shorts) == [
            "1 · Gmail", "2 · Work Outlook"]

    def test_partial_shorts_wide_label_floors_whole_set(self) -> None:
        assert short_option_labels(self._WIDE, ["Gmail", None]) == [
            "Option 1", "Option 2"]

    def test_blank_short_floors_whole_set(self) -> None:
        assert short_option_labels(self._WIDE, ["Gmail", "   "]) == [
            "Option 1", "Option 2"]

    def test_duplicate_shorts_floors_whole_set(self) -> None:
        assert short_option_labels(self._WIDE, ["Same", "Same"]) == [
            "Option 1", "Option 2"]

    def test_duplicate_shorts_fall_back_to_fitting_labels(self) -> None:
        labels = ["Personal Gmail", "Work Outlook"]
        assert short_option_labels(labels, ["Same", "Same"]) == labels

    def test_non_string_short_uses_own_label(self) -> None:
        labels = ["Personal Gmail", "Work Outlook"]
        assert short_option_labels(labels, ["Gmail", 7]) == [
            "1 · Gmail", "2 · Work Outlook"]

    def test_shorts_shorter_than_labels_borrow_own_labels(self) -> None:
        # ``shorts`` has fewer entries than ``labels`` — the missing tail
        # entries show their own labels.
        labels = ["Personal Gmail", "Work Outlook", "Yahoo Mail"]
        assert short_option_labels(labels, ["Gmail"]) == [
            "1 · Gmail", "2 · Work Outlook", "3 · Yahoo Mail"]


class TestCaptionCapBoundary:
    def test_exact_64_char_decorated_caption_passes_verbatim(self) -> None:
        short = "s" * 60  # "1 · " (4) + 60 == 64
        label = short_option_labels(["full label text"], [short])[0]
        assert label == f"1 · {short}"
        assert len(label) == _ASK_BUTTON_CAPTION_CAP == 64

    def test_65_char_decorated_caption_floors(self) -> None:
        short = "s" * 61  # 65 > 64
        assert short_option_labels(
            ["full label text, far too wide for one button"], [short]) == [
                "Option 1"]

    def test_sol_live_case_no_shorts_never_elides(self) -> None:
        # The exact live F-BTN regression — no agent shorts supplied, so the
        # whole set floors; it never garbles into "1 · A —…MCP…MCPB".
        labels = [
            "Option A — Python MCP server, MCPB packaged",
            "Option B — Python MCP server via venv",
        ]
        result = short_option_labels(labels)
        assert result == ["Option 1", "Option 2"]
        for lab in result:
            assert "MCP" not in lab
            assert "…" not in lab


# ===========================================================================
# Pure-function: render_ask_body — the single canonical ask body (moved from
# tests/test_engagement_ask_readable_buttons.py and
# tests/test_option_normalization.py with the renderer)
# ===========================================================================


class TestRenderAskBody:
    def test_numbered_verbatim_options_below_question(self) -> None:
        body = render_ask_body(
            3, "Which account?", ["Personal Gmail", "Work Outlook"])
        assert body == (
            "Q3: Which account?\n\n1. Personal Gmail\n2. Work Outlook")

    def test_every_full_option_appears_verbatim(self) -> None:
        options = [
            "Set up a brand new dedicated work account",
            "Reuse the existing personal address for now",
            "Ask the finance team which alias to use",
        ]
        body = render_ask_body(2, "How should I proceed?", options)
        # Header carries the canonical durable number.
        assert body.startswith("Q2: How should I proceed?\n\n")
        # EVERY option is present verbatim, 1-based numbered, in order.
        for i, opt in enumerate(options):
            assert f"{i + 1}. {opt}" in body
        assert body.splitlines()[2:] == [
            "1. " + options[0], "2. " + options[1], "3. " + options[2]]

    def test_prepends_qnumber_verbatim(self) -> None:
        # v0.85.0 (round 4, D4): the agent-authored "Q7:" prefix is preserved
        # VERBATIM; Casa only PREPENDS the allocated durable number (parity
        # with _canonical_question).
        body = render_ask_body(1, "Q7: Which DB?", ["A", "B"])
        assert body == "Q1: Q7: Which DB?\n\n1. A\n2. B"

    def test_anchor_no_option_list(self) -> None:
        # Free-text anchor (options == []) renders the numbered question ALONE.
        assert render_ask_body(4, "What's the DB name?", []) == (
            "Q4: What's the DB name?")

    def test_no_number_uses_bare_question(self) -> None:
        assert render_ask_body(None, "Proceed?", ["A", "B"]) == (
            "Proceed?\n\n1. A\n2. B")

    def test_own_word_buttons_leave_out_the_list(self) -> None:
        # #1392: buttons that read "Delete" / "Keep" already carry every
        # option in full — the body is the question alone.
        assert render_ask_body(
            None, "Delete both?", ["Delete", "Keep"], ["Delete", "Keep"],
        ) == "Delete both?"
        assert render_ask_body(
            2, "Tea or coffee?", ["Tea", "Coffee"], ["Tea", "Coffee"],
        ) == "Q2: Tea or coffee?"

    def test_list_stays_when_a_button_does_not_show_the_words(self) -> None:
        # #1392: the "Option n" floor and "n · <short>" captions both need the
        # numbered full options under the question.
        options = ["Delete", "Keep"]
        listed = "Delete both?\n\n1. Delete\n2. Keep"
        for captions in (["Option 1", "Option 2"], ["1 · Del", "2 · Keep"],
                         None):
            assert render_ask_body(
                None, "Delete both?", options, captions) == listed

    def test_resolved_captions_decide_the_list(self) -> None:
        # The SAME resolver the keyboards use decides the body: words that fit
        # drop the list; one option over the button fit floors every button
        # and brings the list back.
        from channels.telegram import _ASK_BUTTON_WORDS_FIT, short_option_labels
        short = ["Delete", "Keep"]
        assert render_ask_body(
            None, "Q?", short, short_option_labels(short)) == "Q?"
        long_ = ["Delete", "K" * (_ASK_BUTTON_WORDS_FIT + 1)]
        assert render_ask_body(
            None, "Q?", long_, short_option_labels(long_)
        ) == f"Q?\n\n1. Delete\n2. {long_[1]}"

    def test_body_stays_well_under_telegram_limit_at_caps(self) -> None:
        # Worst case at _validate_ask_args caps (question 1024, 8 options × 48)
        # is far below Telegram's 4096 — no truncation path needed (Sol r1-5).
        body = render_ask_body(999, "q" * 1024, ["o" * 48 for _ in range(8)])
        assert len(body) < 4096


class TestCanonicalQuestionVerbatim:
    def test_agent_q_prefix_preserved_verbatim(self):
        # Task B1 brief: "Q7: which flavor?" is preserved verbatim and
        # rendered "Q<n>: Q7: which flavor?" — Casa prepends its own prefix,
        # the agent's text is untouched.
        assert _canonical_question("Q7: which flavor?", 3) == "Q3: Q7: which flavor?"

    def test_lowercase_q_prefix_preserved_verbatim(self):
        assert _canonical_question("q2: another one?", 5) == "Q5: q2: another one?"

    def test_plain_question_unaffected(self):
        assert _canonical_question("What database name?", 1) == "Q1: What database name?"

    def test_render_ask_body_uses_verbatim_question(self):
        rendered = render_ask_body(3, "Q7: which flavor?", ["A", "B"])
        assert rendered.startswith("Q3: Q7: which flavor?")
