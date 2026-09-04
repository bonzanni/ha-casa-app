"""Unit tests for the fail-literal Markdown→Telegram-entity parser.

Parser offsets were verified line-by-line with Sol (Codex) before implementation;
these assertions are the executable oracle.
"""
from channels.tg_richtext import parse_markdown


# --------------------------------------------------------------------------
# Task 1 — fenced blocks + inline code
# --------------------------------------------------------------------------

def test_plain_text_no_spans():
    assert parse_markdown("hello world") == ("hello world", [])


def test_fenced_block_becomes_pre_span():
    display, spans = parse_markdown("before\n```\nline1\nline2\n```\nafter")
    assert display == "before\nline1\nline2\nafter"
    assert (7, 18, "pre") in spans  # 'line1\nline2' at display offset 7


def test_fenced_block_with_language_token():
    assert parse_markdown("```python\nx = 1\n```") == ("x = 1", [(0, 5, "pre")])


def test_unclosed_fence_is_literal():
    src = "```\nnot closed"
    assert parse_markdown(src) == (src, [])


def test_inline_code_span():
    assert parse_markdown("see `config.yaml` now") == (
        "see config.yaml now", [(4, 15, "code")],
    )


def test_unclosed_inline_code_is_literal():
    assert parse_markdown("a ` b") == ("a ` b", [])


def test_inline_code_does_not_cross_newline():
    src = "a `b\nc` d"
    assert parse_markdown(src) == (src, [])


def test_no_emphasis_inside_pre_or_code():
    assert parse_markdown("```\n**x**\n```") == ("**x**", [(0, 5, "pre")])
    assert parse_markdown("`**x**`") == ("**x**", [(0, 5, "code")])


# --- Sol re-review #2: inline-code must be fully fail-literal ---

def test_crossline_backticks_cannot_be_reused():
    # v3: backticks still never pair across a newline; each line is scoped
    # independently (line 1 unmatched → literal; line 2 pairs internally).
    display, spans = parse_markdown("before `bad\nclose` after `good`")
    assert display.startswith("before `bad\n")
    assert all(s >= len("before `bad\n") for s, _, _ in spans)


def test_multi_backtick_code_renders():
    # v3 flip (CommonMark): a double-backtick run is a valid code delimiter.
    assert parse_markdown("``x``") == ("x", [(0, 1, "code")])


def test_two_inline_code_spans_offsets_in_display():
    assert parse_markdown("`a` and `b`") == (
        "a and b", [(0, 1, "code"), (6, 7, "code")],
    )


# --------------------------------------------------------------------------
# Task 2 — bold + italic (asterisks only, nesting)
# --------------------------------------------------------------------------

def test_bold_asterisks():
    assert parse_markdown("say **hi** now") == ("say hi now", [(4, 6, "bold")])


def test_italic_asterisks():
    assert parse_markdown("say *hi* now") == ("say hi now", [(4, 6, "italic")])


def test_underscores_never_emphasis():
    src = "call mcp__plugin__tool and snake_case_name"
    assert parse_markdown(src) == (src, [])


def test_unbalanced_asterisk_is_literal():
    assert parse_markdown("2 * 3 = 6 and *oops") == ("2 * 3 = 6 and *oops", [])


def test_bold_not_opened_by_space_flank():
    assert parse_markdown("a ** b ** c") == ("a ** b ** c", [])


def test_triple_asterisk_bold_italic():
    # v3 flip (CommonMark): ***x*** is nested strong+em.
    display, spans = parse_markdown("***x***")
    assert display == "x"
    assert set(spans) == {(0, 1, "bold"), (0, 1, "italic")}


def test_nested_bold_then_italic():
    display, spans = parse_markdown("**bold *italic* bold**")
    assert display == "bold italic bold"
    assert (0, 16, "bold") in spans and (5, 11, "italic") in spans


def test_nested_italic_then_bold():
    display, spans = parse_markdown("*italic **bold** italic*")
    assert display == "italic bold italic"
    assert (0, 18, "italic") in spans and (7, 11, "bold") in spans


def test_bold_and_inline_code_coexist():
    display, spans = parse_markdown("**Done** — see `x.py`")
    assert display == "Done — see x.py"
    assert sorted(k for _, _, k in spans) == ["bold", "code"]


# --------------------------------------------------------------------------
# Task 3 — delivery planner (render): validated UTF-16 MessageEntity list
# --------------------------------------------------------------------------

from telegram import MessageEntity  # noqa: E402
from channels.tg_richtext import render, MAX_ENTITIES  # noqa: E402


def test_render_plain_returns_none():
    assert render("nothing to format") == ("nothing to format", None)


def test_render_bold_entities():
    display, ents = render("**hi**")
    assert display == "hi" and len(ents) == 1
    assert ents[0].type == MessageEntity.BOLD
    assert ents[0].offset == 0 and ents[0].length == 2


def test_render_pre_entity():
    display, ents = render("```\ntbl\n```")
    assert display == "tbl" and ents[0].type == MessageEntity.PRE


def test_render_utf16_astral_offset():
    display, ents = render("🧾 **hi**")
    assert display == "🧾 hi"
    assert ents[0].offset == 3 and ents[0].length == 2  # 2 (emoji) + 1 (space) in UTF-16


def test_render_over_entity_limit_none():
    assert render(" ".join(["**x**"] * (MAX_ENTITIES + 5)))[1] is None


def test_render_over_length_none():
    assert render("**" + "a" * 5000 + "**")[1] is None


def test_render_unpaired_surrogate_degrades_to_none():
    # Sol code-review: a lone surrogate breaks UTF-16 conversion; render() must
    # degrade to plain (never raise).
    display, ents = render("\ud800 **x**")
    assert ents is None


def test_render_paged_conversion_failure_keeps_well_formed_link_target():
    """#834 red case, specified by sol: a paginated page whose entity
    conversion FAILS must still carry its link destination.

    The trigger is a conversion failure on a WELL-FORMED span — the lone
    surrogate precedes the link, so PTB's UTF-16 adjustment raises while the
    span itself is exactly what `parse_markdown` produces. Asserted here
    rather than assumed, so no change to how spans are BUILT can satisfy this
    case in place of the fix.
    """
    import pytest
    from telegram import MessageEntity

    from channels.tg_richtext import parse_markdown, render_paged

    label = "DESTINATION"
    url = "https://example.test/target"
    tail = "x" * 4096
    authored = f"\ud800[{label}]({url})\n\n{tail}"

    display, spans = parse_markdown(authored)
    assert (display, spans) == (
        f"\ud800{label}\n\n{tail}",
        [(1, 1 + len(label), f"link:{url}")],
    )

    entity = MessageEntity(
        type=MessageEntity.TEXT_LINK,
        offset=1,
        length=len(label),
        url=url,
    )
    with pytest.raises(UnicodeEncodeError):
        MessageEntity.adjust_message_entities_to_utf_16(
            f"\ud800{label}", [entity]
        )

    pages = render_paged(authored)
    assert (
        len(pages),
        sum(text.count(url) for text, _ in pages),
        pages,
    ) == (
        2,
        1,
        [
            (f"\ud800{label} ({url})", None),
            (tail, None),
        ],
    )


def test_pin_inv_tg_005_render_paged_enforces_length_and_entity_budgets():
    """Pins INV-TG-005: a rich response is paginated to Telegram's UTF-16
    message-length and entity budgets.

    Red case demonstrated: short-circuiting _paginate's initial budget check
    to always return a single page fails both over-budget cases here.
    """
    from channels.tg_richtext import (
        MAX_ENTITIES,
        MAX_LEN,
        parse_markdown,
        render_paged,
    )

    long_source = "```\n" + ("x" * 9000) + "\n```"
    long_display, _ = parse_markdown(long_source)
    long_pages = render_paged(long_source)
    assert len(long_pages) > 1
    assert "".join(text for text, _ in long_pages) == long_display
    assert all(
        len(text.encode("utf-16-le")) // 2 <= MAX_LEN for text, _ in long_pages
    )

    entity_source = " ".join(["**x**"] * (MAX_ENTITIES + 1))
    entity_pages = render_paged(entity_source)
    assert len(entity_pages) > 1
    assert all(
        len(entities or ()) <= MAX_ENTITIES for _, entities in entity_pages
    )
