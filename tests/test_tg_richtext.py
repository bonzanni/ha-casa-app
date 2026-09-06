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

    # First, and it is the intended red at the true parent: the destination.
    assert len(pages) == 2
    assert sum(text.count(url) for text, _ in pages) == 1

    # Then transportability. PTB 22.7 sends `data=request_data.json_parameters`
    # — FORM data, which httpx encodes as UTF-8 — so a page still carrying the
    # lone surrogate raises `NetworkError(UnicodeEncodeError)` before any
    # request leaves the process, and the restored destination reaches nobody.
    assert pages == [
        (f"\ufffd{label} ({url})", None),
        (tail, None),
    ]
    assert sum(text.count("\ud800") for text, _ in pages) == 0
    assert sum(text.count("\ufffd") for text, _ in pages) == 1


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


# ---------------------------------------------------------------------------
# #834 mutation checks. These PASS at the pre-fix tree or are vacuous there;
# they are NOT red cases. Each is here to kill one mutation of the new
# span-based reconstruction — the existing `plain_with_link_targets` pins in
# tests/test_telegram_link_fallback_shapes.py cannot, because they exercise
# the ENTITY-based sibling and not this path (sol, seam round).
# ---------------------------------------------------------------------------

_U_A = "https://example.test/a"
_U_B = "https://example.test/bbb"


def test_render_paged_single_page_conversion_failure_is_untouched():
    """Kills removal or inversion of the originally-multi-page fence.

    Every sender branches on `len(pages) == 1` and that arm retries with the
    AUTHORED text, which already carries the address. Reconstructing here
    would at best duplicate it and at worst — when the inline form overflows
    and a destinations page is appended — take the reply off the single-page
    path entirely.

    The surrogate the page keeps is NOT delivered as such: the channel's
    request boundary replaces it where the text leaves for the Bot API
    (INV-TG-009, tests/test_telegram_surrogate_boundary.py). This pin says the
    renderer does not own that replacement, not that the bytes reach the wire.
    """
    from channels.tg_richtext import render_paged

    assert render_paged(f"\ud800[A]({_U_A})") == [("\ud800A", None)]


def test_render_paged_reconstruction_ignores_non_link_spans_and_runs_right_to_left():
    """Kills the non-link filter and the insertion order in one exact output.

    A left-to-right insertion with unchanged Python offsets puts the second
    destination inside the first; dropping the `link:` filter invents a
    destination out of an italic span's kind.
    """
    from channels.tg_richtext import render_paged

    tail = "x" * 4096
    pages = render_paged(f"\ud800*em* [A]({_U_A}) [B]({_U_B})\n\n{tail}")
    assert pages == [
        (f"\ufffdem A ({_U_A}) B ({_U_B})", None),
        (tail, None),
    ]


def test_render_paged_reconstruction_skips_a_url_visible_in_its_own_label():
    """Kills deletion or inversion of the already-visible-destination guard:
    an autolink whose label IS the address must not be printed twice."""
    from channels.tg_richtext import render_paged

    tail = "x" * 4096
    pages = render_paged(f"\ud800[{_U_A}]({_U_A})\n\n{tail}")
    assert pages == [(f"\ufffd{_U_A}", None), (tail, None)]


def test_render_paged_leaves_a_convertible_page_alone_when_a_later_page_fails():
    """The loss is per page and so is the repair: a page whose entities DO
    convert keeps them, with no destination text appended."""
    from channels.tg_richtext import MAX_LEN, render_paged
    from text_util import utf16_len

    pages = render_paged(f"[A]({_U_A})\n\n" + "y" * 4090 + "\n\n\ud800 z")
    assert (
        len(pages),
        [(e.type, e.url) for e in (pages[0][1] or ())],
        pages[0][0].count(_U_A),
        pages[1],
        all(utf16_len(t) <= MAX_LEN for t, _ in pages),
    ) == (2, [(MessageEntity.TEXT_LINK, _U_A)], 0, ("\ud800 z", None), True)


def test_render_paged_over_budget_reconstruction_packs_targets_onto_own_pages():
    """Kills unconditional inline insertion and unbounded target packing.

    Two destinations that fit individually but not together must land on two
    pages, each within the budget, after an UNCHANGED display page — the
    inline form is what overflows, and cutting it would cut through an
    address.
    """
    from channels.tg_richtext import MAX_LEN, render_paged
    from text_util import utf16_len

    long_a = "https://example.test/" + "a" * 2500
    long_b = "https://example.test/" + "b" * 2500
    pad, tail = "y" * 4000, "x" * 4096
    pages = render_paged(
        f"\ud800{pad} [A]({long_a}) [B]({long_b})\n\n{tail}")

    assert (
        len(pages),
        pages[0][0].endswith(" A B"),
        pages[0][0].count(long_a) + pages[0][0].count(long_b),
        pages[1][0],
        pages[2][0],
        pages[3][0],
        [e for _, e in pages],
        all(utf16_len(t) <= MAX_LEN for t, _ in pages),
    ) == (4, True, 0, long_a, long_b, tail,
          [None, None, None, None], True)


def test_render_paged_drops_a_destination_longer_than_one_message(caplog):
    """Kills removal or inversion of the over-length drop, fragmentation of
    that destination, suppression of its warning, and loss of a deliverable
    sibling that shares the page."""
    import logging

    from channels.tg_richtext import MAX_LEN, render_paged
    from text_util import utf16_len

    deliverable = "https://example.test/" + "a" * 2000
    undeliverable = "https://example.test/" + "c" * 5000
    pad, tail = "y" * 4000, "x" * 4096

    with caplog.at_level(logging.WARNING, logger="channels.tg_richtext"):
        pages = render_paged(
            f"\ud800{pad} [A]({deliverable}) [C]({undeliverable})\n\n{tail}")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert (
        len(pages),
        pages[1][0],
        sum(text.count(undeliverable) for text, _ in pages),
        sum(text.count(undeliverable[:200]) for text, _ in pages),
        len(warnings),
        all(utf16_len(t) <= MAX_LEN for t, _ in pages),
    ) == (3, deliverable, 0, 0, 1, True)


def test_render_paged_lists_a_repeated_destination_once_on_the_target_page():
    """Kills removal of the target de-duplication: two links to the SAME
    address that overflow the inline form owe the reader one line, not two —
    the rule `missing_link_targets` already applies on the entity side."""
    from channels.tg_richtext import MAX_LEN, render_paged
    from text_util import utf16_len

    url = "https://example.test/" + "a" * 3000
    pad, tail = "y" * 4000, "x" * 4096
    pages = render_paged(f"\ud800{pad} [A]({url}) [B]({url})\n\n{tail}")

    assert (
        len(pages),
        pages[1][0],
        sum(text.count(url) for text, _ in pages),
        all(utf16_len(t) <= MAX_LEN for t, _ in pages),
    ) == (3, url, 1, True)


def test_plain_pages_with_targets_does_not_guess_an_unusable_offset():
    """Kills removal of the `inline_ok` fence.

    A span whose offsets do not address this page cannot say WHERE its label
    ends, so inserting inline would attach the address at an invented point.
    The whole page takes the overflow form instead, which loses no
    destination. Driven at the helper because `_paginate` rebases offsets and
    cannot produce this shape — the helper's own contract is what is pinned.
    """
    from channels.tg_richtext import _plain_pages_with_targets

    spans = [(0, 5, f"link:{_U_A}"), (99, 120, f"link:{_U_B}")]
    assert _plain_pages_with_targets("label", spans) == [
        ("label", None),
        (f"{_U_A}\n{_U_B}", None),
    ]


def test_render_paged_replaces_each_surrogate_on_failed_page_one_for_one():
    """Kills deletion, first-only replacement, collapsing several surrogates
    into one, and replacement by a multi-code-point escape.

    One code point for one keeps every Python offset and every UTF-16 unit
    count exactly as `_paginate` measured them, which is what lets the
    reconstruction insert at the offsets it was handed.
    """
    from channels.tg_richtext import render_paged

    authored = f"\ud800[A]({_U_A})\ud801\n\n" + "x" * 4096
    pages = render_paged(authored)

    assert pages[0] == (f"�A ({_U_A})�", None)
    assert len(pages[0][0]) == len(f"\ud800A ({_U_A})\ud801")
    assert pages[0][0].count("�") == 2
    assert pages[0][0].count("\ud800") + pages[0][0].count("\ud801") == 0


def test_render_paged_normalizes_a_failed_page_without_link_spans():
    """Kills normalising only when a destination was reconstructed.

    Once conversion has failed the branch already knows the text may be
    unencodable; passing it through merely because there is no url to restore
    would rebuild the same undeliverable page inside the recovery path.
    """
    from channels.tg_richtext import render_paged

    tail = "x" * 4096
    assert render_paged(f"\ud800**B**\n\n{tail}") == [
        ("�B", None), (tail, None)]


def test_render_paged_page_without_spans_remains_byte_identical():
    """A page with no spans never enters the conversion-failure branch, so
    the RENDERER emits its bytes — surrogate included — unchanged. Delivery
    is a different question: the channel's request boundary replaces the
    surrogate where the text leaves for the Bot API (INV-TG-009,
    tests/test_telegram_surrogate_boundary.py), so renderer identity here is
    not, and never was, proof that this page could be sent."""
    from channels.tg_richtext import render_paged

    tail = "x" * 4096
    assert render_paged(f"\ud800plain\n\n{tail}") == [
        ("\ud800plain", None), (tail, None)]
