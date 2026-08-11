"""v0.94.0 remnant fixes — prod-evidence regression suite (2026-07-19).

Three root causes replayed from live Ellen engagement/DM transcripts:
RC1 emphasis adjacent to inline code never matched (segment-edge flanking);
RC2 ATX headings leaked literal ``##``;
RC3 >4096 texts bypassed render() and shipped raw markdown chunks.

Bot API nesting rule (verified 2026-07-19): bold/italic must NOT contain or
intersect code/pre entities — emphasis spans are SPLIT AROUND code atoms.
Converged with Terra + Sol (Codex) before implementation.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from channels.tg_richtext import parse_markdown, render


# --------------------------------------------------------------------------
# RC1 — emphasis adjacent to / wrapping inline code (prod cases verbatim)
# --------------------------------------------------------------------------

def test_bold_wrapping_code_consumes_markers():
    # prod: "1. **`definition.yaml`** — plugin-developer…"
    display, spans = parse_markdown("**`definition.yaml`**")
    assert display == "definition.yaml"
    assert (0, 15, "code") in spans
    assert not any(k == "bold" for _, _, k in spans)  # cannot nest around code


def test_bold_containing_code_splits_around_code_atom():
    # prod: "**ADC token (your `gcloud auth` session):** Yes…"
    display, spans = parse_markdown("**ADC token (`gcloud`):** yes")
    assert display == "ADC token (gcloud): yes"
    assert (11, 17, "code") in spans
    assert (0, 11, "bold") in spans and (17, 19, "bold") in spans


def test_bold_ending_with_code_then_colon():
    # prod: "**Step 2 — Rewrite `server/auth.py`**: text"
    display, spans = parse_markdown("**Step 2 — `auth.py`**: text")
    assert display == "Step 2 — auth.py: text"
    assert (9, 16, "code") in spans
    assert (0, 9, "bold") in spans
    assert "*" not in display


def test_interleaved_bold_and_code_bold_in_one_message():
    # the reported symptom: working bold and leaked ** in the SAME message
    display, spans = parse_markdown("plain **bold** and **`code`** mix")
    assert display == "plain bold and code mix"
    assert (6, 10, "bold") in spans
    assert (15, 19, "code") in spans
    assert "*" not in display


def test_bold_code_infix_and_prefix_and_suffix():
    # Terra: `**a`code`b**`, `**`code` suffix**`, `**prefix `code`**`
    d1, s1 = parse_markdown("**a`c`b**")
    assert d1 == "acb"
    assert (1, 2, "code") in s1
    assert (0, 1, "bold") in s1 and (2, 3, "bold") in s1

    d2, s2 = parse_markdown("**`c` suffix**")
    assert d2 == "c suffix"
    assert (0, 1, "code") in s2 and (1, 8, "bold") in s2

    d3, s3 = parse_markdown("**prefix `c`**")
    assert d3 == "prefix c"
    assert (7, 8, "code") in s3 and (0, 7, "bold") in s3


def test_code_content_is_opaque_to_emphasis():
    # Terra: `**a `**literal**` b**` — the inner ** are code content, and the
    # outer bold pairs across the atom
    display, spans = parse_markdown("**a `**literal**` b**")
    assert display == "a **literal** b"
    assert (2, 13, "code") in spans
    assert (0, 2, "bold") in spans and (13, 15, "bold") in spans
    assert not any(s <= 2 < e and k == "bold" for s, e, k in spans if (s, e) != (0, 2))


def test_nested_italic_bold_code():
    # Terra: `*a **b `c` d** e*`
    display, spans = parse_markdown("*a **b `c` d** e*")
    assert display == "a b c d e"
    assert (4, 5, "code") in spans
    # italic split around the code atom; bold split around the code atom
    assert (0, 4, "italic") in spans and (5, 9, "italic") in spans
    assert (2, 4, "bold") in spans and (5, 7, "bold") in spans


def test_unmatched_emphasis_keeps_code_entity():
    # Terra: unmatched outer emphasis must not discard a valid code span
    display, spans = parse_markdown("**oops `code`")
    assert display == "**oops code"
    assert (7, 11, "code") in spans
    assert not any(k in ("bold", "italic") for _, _, k in spans)


def test_odd_backtick_no_longer_poisons_line():
    # v3 flip (CommonMark): an unmatched backtick is literal by itself; the
    # rest of the line still renders (was: whole line inline-literal).
    display, spans = parse_markdown("a ` b **bold**")
    assert display == "a ` b bold"
    assert spans == [(6, 10, "bold")]


def test_odd_backtick_heading_line_now_renders():
    # v3 flip: the heading renders; the unmatched backtick stays literal.
    display, spans = parse_markdown("## a ` b")
    assert display == "a ` b"
    assert spans == [(0, 5, "bold")]


def test_emphasis_is_per_line_scoped():
    # D1 (Sol+Terra ACK): documented compatibility change — emphasis never
    # pairs across a newline, and a stray delimiter only poisons ITS line
    src = "**cross\nline**"
    assert parse_markdown(src) == (src, [])

    display, spans = parse_markdown("*unclosed\n**valid**")
    assert display == "*unclosed\nvalid"
    assert (10, 15, "bold") in spans


def test_code_pair_rules():
    # v3: a lone backtick stays literal; pairing never crosses a newline;
    # double-backtick code renders (CommonMark flip).
    src = "a ` b"
    assert parse_markdown(src) == (src, [])
    display, spans = parse_markdown("before `bad\nclose` after `good`")
    assert display.startswith("before `bad\n")  # line 1 untouched
    assert parse_markdown("``x``") == ("x", [(0, 1, "code")])


# --------------------------------------------------------------------------
# RC2 — ATX headings render as bold lines
# --------------------------------------------------------------------------

def test_h2_heading_becomes_bold_line():
    display, spans = parse_markdown("## Authentication Migration\nbody")
    assert display == "Authentication Migration\nbody"
    assert (0, 24, "bold") in spans


def test_h1_through_h6():
    display, spans = parse_markdown("# A\n###### B")
    assert display == "A\nB"
    assert (0, 1, "bold") in spans and (2, 3, "bold") in spans


def test_heading_with_inline_code():
    display, spans = parse_markdown("## Check `auth.py` now")
    assert display == "Check auth.py now"
    assert (6, 13, "code") in spans
    # bold split around the code atom
    assert (0, 6, "bold") in spans and (13, 17, "bold") in spans


def test_heading_with_inner_bold_normalizes():
    # `## **Title**` — inner markers consumed, ONE bold span, no overlap
    display, spans = parse_markdown("## **Title**")
    assert display == "Title"
    bolds = [(s, e) for s, e, k in spans if k == "bold"]
    assert bolds == [(0, 5)]


def test_heading_trailing_hashes():
    # trailing run stripped only when whitespace-separated and at EOL
    display, spans = parse_markdown("## Title ##")
    assert display == "Title"
    assert (0, 5, "bold") in spans


def test_heading_trailing_hash_not_stripped_when_attached():
    display, spans = parse_markdown("## C#")
    assert display == "C#"
    assert (0, 2, "bold") in spans


def test_hash_mid_line_stays_literal():
    src = "issue #42 and C# code"
    assert parse_markdown(src) == (src, [])


def test_hash_inside_fence_stays_literal():
    display, spans = parse_markdown("```\n# comment\n```")
    assert display == "# comment"
    assert spans == [(0, 9, "pre")]


def test_bare_hashes_no_text_stay_literal():
    src = "##\n#"
    assert parse_markdown(src) == (src, [])


def test_seven_hashes_stay_literal():
    src = "####### too deep"
    assert parse_markdown(src) == (src, [])


# --------------------------------------------------------------------------
# Tables (T-a, Sol+Terra converged): confident plain tables render as PRE
# --------------------------------------------------------------------------

def test_plain_table_renders_as_pre():
    # v3 (#506): reflowed — separator row dropped, cells padded per column.
    src = "before\n| # | Item |\n|---|---|\n| 1 | first |\n| 2 | second |\nafter"
    display, spans = parse_markdown(src)
    table = "| # | Item   |\n| 1 | first  |\n| 2 | second |"
    assert table in display
    s = display.find(table)
    assert (s, s + len(table), "pre") in spans


def test_table_with_markers_in_cells_reflows_to_pre():
    # v3 (#506 mode-A fix): markers no longer reject the table — cells are
    # parsed and re-emitted marker-stripped inside the padded PRE box.
    src = "| # | Item |\n|---|---|\n| 1 | **bold** thing |"
    display, spans = parse_markdown(src)
    assert any(k == "pre" for _, _, k in spans)
    assert "**" not in display                       # markers stripped
    assert "bold thing" in display


def test_pipe_lines_without_separator_stay_literal():
    src = "| a | b |\n| c | d |"
    assert parse_markdown(src) == (src, [])


def test_table_with_inconsistent_columns_stays_literal():
    src = "| a | b |\n|---|---|\n| only one |"
    assert parse_markdown(src) == (src, [])


def test_table_alignment_colons_accepted():
    src = "| l | r |\n|:---|---:|\n| x | y |"
    display, spans = parse_markdown(src)
    assert spans and spans[0][2] == "pre"


def test_pipes_inside_fence_untouched():
    src = "```\n| a | b |\n|---|---|\n```"
    display, spans = parse_markdown(src)
    assert display == "| a | b |\n|---|---|"
    assert spans == [(0, len(display), "pre")]


# --------------------------------------------------------------------------
# RC3 — delivery planner: split display, rebase spans, cap entities
# --------------------------------------------------------------------------

def _get_render_paged():
    from channels.tg_richtext import render_paged
    return render_paged


def test_render_paged_single_page_matches_render():
    render_paged = _get_render_paged()
    pages = render_paged("say **hi** now")
    assert len(pages) == 1
    display, entities = pages[0]
    assert display == "say hi now"
    assert entities is not None and len(entities) == 1


def test_render_paged_long_text_all_pages_rendered():
    render_paged = _get_render_paged()
    src = "**Heading item** with `code.py` and prose line\n" * 200  # ≫4096
    pages = render_paged(src)
    assert len(pages) >= 2
    for display, entities in pages:
        assert len(display) <= 4096
        assert "**" not in display
        assert entities


def test_render_paged_span_crossing_boundary_is_split():
    render_paged = _get_render_paged()
    # one giant bold span longer than a page → bold on BOTH pages
    src = "**" + ("word " * 1200).strip() + "**"  # ~6000 display chars
    pages = render_paged(src)
    assert len(pages) >= 2
    for display, entities in pages:
        assert entities, "every page keeps its share of the split span"
        assert any(e.type == "bold" for e in entities)


def test_render_paged_long_pre_splits_as_pre_per_page():
    render_paged = _get_render_paged()
    src = "```\n" + ("code line\n" * 800) + "```"
    pages = render_paged(src)
    assert len(pages) >= 2
    for display, entities in pages:
        assert entities and all(e.type == "pre" for e in entities)


def test_render_paged_entity_cap_paginates():
    render_paged = _get_render_paged()
    # >100 entities worth of markup must yield pages each ≤100 entities,
    # never a plain fallback
    src = " ".join("**x**" for _ in range(150))
    pages = render_paged(src)
    assert len(pages) >= 2
    for display, entities in pages:
        assert entities and len(entities) <= 100
        assert "**" not in display


def test_render_paged_plain_text_single_page_none_entities():
    render_paged = _get_render_paged()
    pages = render_paged("no markup at all")
    assert pages == [("no markup at all", None)]


def test_render_paged_utf16_boundary_with_astral():
    render_paged = _get_render_paged()
    # astral-heavy text near the boundary: offsets must stay valid per page
    src = ("🧾" * 10 + " **bold** and `code`\n") * 180
    pages = render_paged(src)
    for display, entities in pages:
        assert entities
        # UTF-16 length within Telegram limit
        assert len(display.encode("utf-16-le")) // 2 <= 4096


def test_render_paged_concat_covers_whole_display():
    # Sol: no content lost or duplicated across pages (page-leading newlines
    # at cut points are the only permitted difference)
    render_paged = _get_render_paged()
    from channels.tg_richtext import parse_markdown as _pm
    src = "**Heading item** with `code.py` and prose line\n" * 200
    whole_display, _ = _pm(src)
    joined = "\n".join(display for display, _ in render_paged(src))
    assert joined.replace("\n", "") == whole_display.replace("\n", "")


def test_render_paged_never_drops_content_at_newline_runs():
    # Terra impl-review MAJOR: an unbounded newline skip at a page cut
    # silently discarded content — bounded to the one paragraph separator
    render_paged = _get_render_paged()
    src = "**x**" + "\n" * 5000 + "**tail**"
    pages = render_paged(src)
    joined = "".join(d for d, _ in pages)
    assert joined.count("\n") >= 4990  # at most 2 swallowed per cut
    assert joined.startswith("x") and joined.endswith("tail")


def test_render_paged_pre_blank_lines_survive_page_cut():
    render_paged = _get_render_paged()
    body = ("chunk\n\n\n" * 600).rstrip()  # blank lines inside one huge fence
    src = "```\n" + body + "\n```"
    pages = render_paged(src)
    joined = "".join(d for d, _ in pages)
    assert joined == body  # byte-identical PRE content across pages


def test_render_paged_unclosed_fence_stays_literal_across_pages():
    render_paged = _get_render_paged()
    src = "```\n" + ("unclosed fence line\n" * 400)  # ≫4096, never closed
    pages = render_paged(src)
    joined = "\n".join(d for d, _ in pages)
    assert "```" in joined  # opener retained byte-for-byte (fail-literal)


# --------------------------------------------------------------------------
# RC3 — telegram.py delivery paths use the planner
# --------------------------------------------------------------------------

def _mk_channel(supergroup_id: int = -1001):
    from channels.telegram import TelegramChannel

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(return_value=MagicMock(message_id=777))
    fake_bot.edit_message_text = AsyncMock()
    fake_app = MagicMock()
    fake_app.bot = fake_bot
    ch = TelegramChannel(
        bot_token="x:y", chat_id=100, default_agent="assistant",
        engagement_supergroup_id=supergroup_id,
    )
    ch._app = fake_app
    return ch, fake_bot


_LONG_MD = "**Heading item** with `code.py` and prose line\n" * 200  # ≫4096


@pytest.mark.asyncio
async def test_send_response_overflow_renders_each_chunk():
    ch, bot = _mk_channel()
    await ch.send_response(_LONG_MD, {"chat_id": "100"})
    assert bot.send_message.await_count >= 2
    for call in bot.send_message.await_args_list:
        kw = call.kwargs
        assert len(kw["text"]) <= 4096
        assert "**" not in kw["text"]          # markers consumed
        assert kw.get("entities")              # rendered, not plain


@pytest.mark.asyncio
async def test_topic_finalize_overflow_renders_each_chunk():
    ch, bot = _mk_channel()
    handle = ch.create_topic_stream(topic_id=42)
    await handle.finalize(_LONG_MD)
    assert bot.send_message.await_count >= 2
    for call in bot.send_message.await_args_list:
        kw = call.kwargs
        assert len(kw["text"]) <= 4096
        assert "**" not in kw["text"]
        assert kw.get("entities")


@pytest.mark.asyncio
async def test_topic_finalize_overflow_after_stream_renders_chunks():
    ch, bot = _mk_channel()
    handle = ch.create_topic_stream(topic_id=42)
    await handle.emit("partial")
    bot.send_message.reset_mock()
    await handle.finalize(_LONG_MD)
    # first chunk edits the streamed message, rest are fresh sends — all rendered
    kw_edit = bot.edit_message_text.await_args.kwargs
    assert "**" not in kw_edit["text"] and kw_edit.get("entities")
    for call in bot.send_message.await_args_list:
        kw = call.kwargs
        assert "**" not in kw["text"] and kw.get("entities")


@pytest.mark.asyncio
async def test_send_response_to_topic_overflow_paginates_rendered():
    # D3 (Sol+Terra ACK): pagination lives in send_response_to_topic;
    # send_to_topic_rich stays the sequencer's ONE-message primitive
    ch, bot = _mk_channel()
    mid = await ch.send_response_to_topic(42, _LONG_MD)
    assert bot.send_message.await_count >= 2
    assert mid == 777  # last page's message_id
    for call in bot.send_message.await_args_list:
        kw = call.kwargs
        assert kw["message_thread_id"] == 42
        assert "**" not in kw["text"] and kw.get("entities")


@pytest.mark.asyncio
async def test_send_to_topic_rich_stays_single_send_when_fits():
    ch, bot = _mk_channel()
    await ch.send_to_topic_rich(42, "say **hi**")
    bot.send_message.assert_awaited_once()
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "say hi" and kw.get("entities")


@pytest.mark.asyncio
async def test_plain_paths_stay_plain():
    # error/tool/notice paths keep byte-identical literal markdown
    ch, bot = _mk_channel()
    await ch.send_to_topic(7, "**not bold** `raw`")
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "**not bold** `raw`"
    assert "entities" not in kw

    bot.send_message.reset_mock()
    await ch.send("**stars stay** `literal`", {"chat_id": "42"})
    kw = bot.send_message.await_args.kwargs
    assert kw["text"] == "**stars stay** `literal`"
    assert "entities" not in kw


# --------------------------------------------------------------------------
# render() end-to-end on a verbatim prod paragraph
# --------------------------------------------------------------------------

def test_prod_paragraph_no_remnants():
    src = (
        "## Authentication Migration Assessment\n\n"
        "**server/auth.py** — every reference:\n"
        "- **`GMAIL_IMPERSONATION_SA`** and **`GMAIL_SUBJECT_EMAIL`** as env "
        "vars (both validated).\n"
        "**Step 6 — Smoke test** (run after IAM binding is live)\n"
    )
    display, entities = render(src)
    assert entities is not None
    assert "**" not in display
    assert "##" not in display
