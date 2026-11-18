"""tg_richtext v3 — hybrid renderer: markdown-it inline + labelled links (#404).

Block segmentation (fences, headings) stays casa's line-based
scanner; inline semantics (code, emphasis, escapes, links) run through
markdown-it-py's parseInline PER LINE. Design converged with Sol + Terra
over three rounds (2026-08-12); the round-3 red cases are pinned here.
"""
from __future__ import annotations

from collections import Counter

from telegram import MessageEntity

from channels.tg_richtext import parse_markdown, render


# --------------------------------------------------------------------------
# Labelled links (#404)
# --------------------------------------------------------------------------

def test_basic_link_becomes_text_link_span():
    display, spans = parse_markdown("[Connect Gmail](https://a.test/auth?x=1)")
    assert display == "Connect Gmail"
    assert spans == [(0, 13, "link:https://a.test/auth?x=1")]


def test_link_render_emits_text_link_entity_with_url():
    display, ents = render("tap [here](https://a.test/p) now")
    assert display == "tap here now"
    assert len(ents) == 1
    assert ents[0].type == MessageEntity.TEXT_LINK
    assert ents[0].url == "https://a.test/p"
    assert ents[0].offset == 4 and ents[0].length == 4


def test_uppercase_scheme_allowed():
    display, spans = parse_markdown("[l](HTTPS://A.TEST/X)")
    assert display == "l"
    assert spans == [(0, 1, "link:HTTPS://A.TEST/X")]


def test_parenthesized_url_kept_whole():
    # round-1 Sol red case: first-')' truncation misroutes the operator.
    display, spans = parse_markdown("[Foo](https://e.test/a_(b))")
    assert display == "Foo"
    assert spans == [(0, 3, "link:https://e.test/a_(b)")]


def test_non_http_scheme_line_stays_fully_literal():
    src = "see [t](tg://user?id=1) now"
    assert parse_markdown(src) == (src, [])


def test_disallowed_scheme_with_title_stays_byte_literal():
    # round-3: reconstruction dropped the title; whole-line literal keeps it.
    src = '[x](mailto:a "operator note")'
    assert parse_markdown(src) == (src, [])


def test_empty_label_line_stays_literal():
    # round-3: an empty rendered label must not silently drop its URL.
    src = "[](https://example.test/path)"
    assert parse_markdown(src) == (src, [])


def test_image_syntax_line_stays_literal():
    src = "![alt](https://a.test/i.png)"
    assert parse_markdown(src) == (src, [])


def test_escaped_bang_renders_bang_then_link():
    # round-3 Sol: \! is an escaped bang, NOT image syntax.
    display, spans = parse_markdown("\\![x](https://a.test)")
    assert display == "!x"
    assert spans == [(1, 2, "link:https://a.test")]


def test_label_inner_markers_are_suppressed_to_plain_text():
    display, spans = parse_markdown("[has `code` in](https://a.test)")
    assert display == "has code in"
    assert spans == [(0, 11, "link:https://a.test")]


def test_bold_splits_around_link_atom():
    # No entity ever nests another: emphasis splits around links like code.
    display, spans = parse_markdown("**bold [lbl](https://a.test) tail**")
    assert display == "bold lbl tail"
    assert (5, 8, "link:https://a.test") in spans
    assert (0, 5, "bold") in spans and (8, 13, "bold") in spans
    assert not any(s <= 5 and e >= 8 for s, e, k in spans if k == "bold")


def test_heading_bold_splits_around_link():
    display, spans = parse_markdown("## See [docs](https://a.test)")
    assert display == "See docs"
    assert (4, 8, "link:https://a.test") in spans
    assert (0, 4, "bold") in spans


def test_link_only_on_its_own_line_among_prose():
    display, spans = parse_markdown("before\n[l](https://a.test)\nafter")
    assert display == "before\nl\nafter"
    assert spans == [(7, 8, "link:https://a.test")]


# --------------------------------------------------------------------------
# Underscore doctrine survives the CommonMark engine
# --------------------------------------------------------------------------

def test_underscores_still_never_emphasis():
    src = "call mcp__plugin__tool and snake_case_name"
    assert parse_markdown(src) == (src, [])


def test_underscore_emphasis_markers_stay_literal():
    src = "_italic_ and __bold__"
    assert parse_markdown(src) == (src, [])


def test_underscore_inside_bold_kept_literal():
    display, spans = parse_markdown("**a _b_ c**")
    assert display == "a _b_ c"
    assert spans == [(0, 7, "bold")]


# --------------------------------------------------------------------------
# Deliberate doctrine flips (CommonMark inline semantics)
# --------------------------------------------------------------------------

def test_flip_double_backtick_code_now_renders():
    assert parse_markdown("``x``") == ("x", [(0, 1, "code")])


def test_flip_triple_asterisk_now_bold_italic():
    display, spans = parse_markdown("***x***")
    assert display == "x"
    assert set(spans) == {(0, 1, "bold"), (0, 1, "italic")}


def test_flip_escapes_consumed():
    assert parse_markdown("\\*not bold\\*") == ("*not bold*", [])


def test_flip_intraword_bold_now_renders():
    assert parse_markdown("a**b**c") == ("abc", [(1, 2, "bold")])


def test_flip_unmatched_backtick_no_longer_poisons_line():
    display, spans = parse_markdown("`a` and ` rest")
    assert display == "a and ` rest"
    assert spans == [(0, 1, "code")]


def test_crossline_backticks_still_literal_per_line():
    src = "a `b\nc` d"
    assert parse_markdown(src) == (src, [])


# --------------------------------------------------------------------------
# Fences: unclosed remainder is a literal block (round-3 strengthening)
# --------------------------------------------------------------------------

def test_unclosed_fence_remainder_fully_literal():
    src = "```\n**secret**"
    assert parse_markdown(src) == (src, [])


def test_unclosed_fence_table_lines_stay_literal():
    src = "```\n| a | b |\n| c | d |\n| e | f |"
    assert parse_markdown(src) == (src, [])


# --------------------------------------------------------------------------
# Robustness: the parser never raises
# --------------------------------------------------------------------------

def test_never_raises_on_nasty_inputs():
    nasty = [
        "",
        "\n\n\n",
        "|\n|\n|",
        "[" * 200 + "(" * 200,
        "```\n| a | b |\n```",
        "| " + "\\|" * 30 + " |\n|---|---|\n| x |",
        "\ud800 [l](https://a.test)",
        "*" * 300,
        "`" * 301,
        "[a](https://a.test)" * 150,
    ]
    for src in nasty:
        display, spans = parse_markdown(src)
        assert isinstance(display, str)
        text, _ = render(src)
        assert isinstance(text, str)


# --------------------------------------------------------------------------
# Diff-review round 1 red cases (Sol + Terra, reproduced)
# --------------------------------------------------------------------------

def test_nested_link_tokens_make_line_literal():
    # Sol S1: adversarial pairing can emit a link inside a link label —
    # Telegram rejects nested TEXT_LINKs, so the line must stay literal.
    src = "[`[x](https://inner.test)`x`](https://outer.test)"
    assert parse_markdown(src) == (src, [])


def test_even_backslash_run_is_unescaped_image():
    # Sol/Terra S2: "\\\\![" is a literal backslash + image marker — literal.
    src = "\\\\![x](https://e.test)"
    assert parse_markdown(src) == (src, [])
