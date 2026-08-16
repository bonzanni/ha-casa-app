"""Spec §5.2 — TagDialectAdapter."""

import re

import pytest

from channels.voice.tts_adapter import TagDialectAdapter, has_speech


class TestSquareBrackets:
    def test_identity(self):
        a = TagDialectAdapter("square_brackets")
        assert a.render("[confident] Done.") == "[confident] Done."


class TestParens:
    def test_rewrites_leading_bracket(self):
        a = TagDialectAdapter("parens")
        assert a.render("[confident] Done.") == "(confident) Done."

    def test_rewrites_multiple_tags(self):
        a = TagDialectAdapter("parens")
        assert a.render("[warm] [softly] hello") == "(warm) (softly) hello"

    def test_leaves_prose_square_brackets_untouched_if_no_canonical_tag(self):
        """Spec §5.2: adapter operates on canonical input. If the block has
        no leading tag, arbitrary square-bracket text in prose is still
        rewritten (the adapter is a simple substitution). This test pins
        current behaviour so any future 'leading-only' refinement is
        explicit.
        """
        a = TagDialectAdapter("parens")
        # Every [X] pair is rewritten — canonical convention expects tags
        # to appear as the only bracket form in butler output.
        assert a.render("See also [ref].") == "See also (ref)."


class TestNone:
    def test_strips_leading_tag(self):
        a = TagDialectAdapter("none")
        assert a.render("[confident] Done.") == "Done."

    def test_keeps_lowercase_parenthetical_prose(self):
        """#357 (review round 2): a lowercase heuristic for "tag-like"
        parens still deleted substantive speech — "(do not take it)" is
        indistinguishable from a tag by shape. Canonical tags are ONLY
        square-bracketed, so under `none` a parenthetical is always prose.
        """
        a = TagDialectAdapter("none")
        block = "(do not take it) Call emergency services."
        assert a.render(block) == block

    def test_keeps_leading_parens_even_when_tag_shaped(self):
        # The cost of the fail-safe boundary: a model that emits a
        # non-canonical parens tag under `none` is spoken, not stripped.
        a = TagDialectAdapter("none")
        assert a.render("(confident) Done.") == "(confident) Done."

    def test_strips_multiple_leading_tags(self):
        a = TagDialectAdapter("none")
        assert a.render("[warm] [softly] hello") == "hello"

    def test_empty_block_empty_result(self):
        a = TagDialectAdapter("none")
        assert a.render("") == ""

    def test_keeps_substantive_leading_parenthetical(self):
        """#357: only canonical tag atoms are stripped. A leading
        parenthetical carrying real prose (spaces plus sentence
        punctuation — nothing a prosody tag ever contains) is content,
        not markup, and deleting it can drop safety-relevant speech.
        """
        a = TagDialectAdapter("none")
        block = "(Important: the oven is still on.) Turn it off."
        assert a.render(block) == block

    def test_keeps_capitalized_leading_parenthetical(self):
        # Canonical tags are lowercase ([confident], [flat]); a
        # capitalized parenthetical is prose.
        a = TagDialectAdapter("none")
        assert a.render("(Important) Turn it off.") == (
            "(Important) Turn it off."
        )

    def test_strips_tag_then_keeps_prose_parenthetical(self):
        a = TagDialectAdapter("none")
        assert a.render("[flat] (Warning: gas leak.) Leave now.") == (
            "(Warning: gas leak.) Leave now."
        )


class TestValidation:
    def test_unknown_dialect_rejected(self):
        with pytest.raises(ValueError, match="tag_dialect"):
            TagDialectAdapter("ssml")


class TestHasSpeech:
    """#594: does this line say anything ALOUD? A configured line can be
    schema-valid and still be pure delivery instruction, and the voice
    retraction must treat one as neither a reason nor a retraction."""

    @pytest.mark.parametrize("line", [
        "",
        " ",
        "[flat]",
        "  [flat]  ",
        "[flat][warm]",
        # Reproduced by both reviewers: the leading-tag pattern stops at the
        # FIRST `]`, so a malformed nested tag leaves `]` behind — non-empty
        # residue, but nothing a listener hears. `none` renders it to "]",
        # and the first guard shipped read that residue as a reason.
        "[flat [warm]]",
        "[outer[inner]]",
    ])
    def test_lines_that_say_nothing_aloud(self, line):
        assert has_speech(line) is False, line

    @pytest.mark.parametrize("line", [
        "reason",
        "[flat] reason",
        "reason [flat]",
        # A parenthetical is NOT a tag: the adapter deliberately preserves it
        # because it can be safety-relevant speech. It must stay speakable.
        "[flat] (Warning: gas leak.) Leave now.",
        "[a](b)",
        "7",
    ])
    def test_lines_that_do_say_something(self, line):
        assert has_speech(line) is True, line

    def test_the_answer_does_not_depend_on_the_dialect(self):
        """`has_speech` reads the CANONICAL line precisely so one persona's
        dialect cannot make a line speakable that another's does not. Note the
        rendered strings differ wildly — a tag-preserving dialect keeps the
        tag NAMES, which are word-bearing but are delivery instructions the
        engine consumes rather than speaks. That is why judging the rendered
        text is the wrong test, and why this one is not written that way."""
        line = "[flat [warm]]"
        rendered = {
            d: TagDialectAdapter(d).render(line)
            for d in ("square_brackets", "parens", "none")
        }
        assert len(set(rendered.values())) > 1, rendered
        assert has_speech(line) is False
