"""Preparing agent Markdown for Telegram rich messages (channels/tg_rich.py)."""

from datetime import datetime

import pytest

from channels.tg_rich import (
    RICH_BUDGET_BYTES,
    RICH_MAX_BLOCKS,
    flatten,
    is_size_refusal,
    plain_blocks,
    prepare,
    split,
)


def test_underscores_become_entities_outside_code():
    assert prepare("bare mcp__casa__x and `mcp__casa__x`") == "bare mcp&#95;&#95;casa&#95;&#95;x and `mcp__casa__x`"


def test_escaped_underscore_loses_its_backslash():
    assert prepare(r"sensor.living\_room") == "sensor.living&#95;room"
    assert prepare(r"a\\_b") == r"a\\&#95;b"          # even parity: the backslash is literal


def test_underscores_in_html_blocks():
    assert prepare("<aside>mcp__x</aside>") == "<aside>mcp&#95;&#95;x</aside>"
    assert prepare(r"<aside>living\_room</aside>") == "<aside>living&#95;room</aside>"


def test_tool_name_in_details_summary_has_no_backslash():
    out = prepare("<details><summary>mcp__casa__x</summary>\n\nbody\\_text\n\n</details>")
    assert out == "<details><summary>mcp&#95;&#95;casa&#95;&#95;x</summary>\n\nbody&#95;text\n\n</details>"


def test_link_destinations_and_bare_urls_untouched():
    assert prepare("[a_b](https://x.io/a_b__c) https://x.io/p_q") == "[a&#95;b](https://x.io/a_b__c) https://x.io/p_q"


def test_bare_url_keeps_balanced_parentheses_and_stops_at_a_closing_one():
    assert prepare("see https://x.io/A_(b_c) now") == "see https://x.io/A_(b_c) now"
    assert prepare("(at https://x.io/a_b) and a_b") == "(at https://x.io/a_b) and a&#95;b"


def test_fenced_and_inline_code_byte_for_byte():
    src = ("```jinja\n{{ value < 5 }} mcp__x\n```\nand `a<b_c`\n\n"
           '```html\n<tg-button type="callback_data" data="proposal:abc">Approve</tg-button>\n'
           "| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n```")
    assert prepare(src) == src


def test_unclosed_fence_keeps_its_remainder_byte_for_byte():
    src = "a_b\n```py\nx_y < 3\n"
    assert prepare(src) == "a&#95;b\n```py\nx_y < 3\n"


def test_stray_less_than_escaped_supported_tags_kept():
    assert prepare("3 <5 and <not a tag> and <b>bold</b>") == "3 &lt;5 and &lt;not a tag> and <b>bold</b>"
    assert prepare(r"threshold \<5 W") == "threshold &lt;5 W"
    assert prepare("<details open><summary>S</summary>\n\nx\n\n</details>").startswith("<details open><summary>")


def test_tag_attributes_copied_verbatim():
    src = '<a href="https://x.io/a_b">link_text</a> <hr/> <br />'
    assert prepare(src) == '<a href="https://x.io/a_b">link&#95;text</a> <hr/> <br />'


def test_time_link_rewritten_without_format():
    unix = int(datetime.fromisoformat("2026-11-20T09:00+01:00").timestamp())
    out = prepare("[Friday 9:00](time:2026-11-20T09:00+01:00)")
    assert out == f'<tg-time unix="{unix}">Friday 9:00</tg-time>'


def test_unparseable_time_keeps_label():
    assert prepare("[soon](time:not-a-date)") == "soon"


def test_time_label_is_html_escaped():
    assert "a&amp;b" in prepare("[a&b](time:2026-11-20)")


def test_model_callback_button_becomes_text():
    out = prepare('<tg-button-row><tg-button type="callback_data" data="proposal:abc">Approve</tg-button></tg-button-row>')
    assert "callback_data" not in out and "Approve" in out


def test_callback_button_the_rewrite_misses_never_stays_a_tag():
    # Inline code inside the label keeps the full-button rewrite from matching;
    # the opening tag is still sent as text, so no button can come of it.
    out = prepare('<tg-button type="callback_data" data="proposal:abc">`Approve`</tg-button>')
    assert out.startswith("&lt;tg-button")


def test_url_and_copy_buttons_kept():
    src = '<tg-button-row><tg-button type="copy_text" text="IT60">Copy</tg-button></tg-button-row>'
    assert prepare(src) == src


def test_table_header_widened_to_widest_row():
    out = prepare("| Invoice | Amount |\n| --- | --- |\n| INV-7 | EUR 10 | EUR 2 tax |")
    assert out.splitlines()[0].count("|") == 4 and out.splitlines()[1].count("---") == 3


def test_table_without_extra_cells_unchanged():
    src = "| a | b |\n| --- | --- |\n| 1 | 2 \\| 3 |"
    assert prepare(src) == src


def test_italian_reply_over_budget_splits_without_loss():
    paras = ["Fattura pagata, importo €1.200,50 è stata saldata — grazie. " * 2 + f"#{i}" for i in range(300)]
    src = "\n\n".join(paras)
    assert len(src.encode("utf-8")) > RICH_BUDGET_BYTES
    parts = split(src)
    assert len(parts) >= 2
    assert all(len(prepare(p).encode()) <= RICH_BUDGET_BYTES for p in parts)
    assert "\n\n".join(parts) == src


def test_short_text_is_one_part():
    assert split("hello") == ["hello"]


def test_split_measures_prepared_bytes():
    # Every underscore grows to five bytes once prepared.
    src = "\n\n".join(["a_b_c_d_e"] * 40)
    parts = split(src, budget=200)
    assert len(parts) >= 2
    assert all(len(prepare(p).encode()) <= 200 for p in parts)
    assert "\n\n".join(parts) == src


def test_split_inside_fence_reopens_with_language():
    src = "```xml\n" + ("a" * 100 + "\n") * 400 + "<script>invoice 42</script>\n```"
    parts = split(src)
    assert len(parts) >= 2
    assert all(p.lstrip().startswith("```xml") for p in parts[1:])
    assert all(p.rstrip().endswith("```") for p in parts)
    assert all(len(prepare(p).encode()) <= RICH_BUDGET_BYTES for p in parts)


def test_split_hard_cuts_one_long_line():
    src = "é" * 20_000
    parts = split(src)
    assert len(parts) >= 2
    assert all(len(prepare(p).encode()) <= RICH_BUDGET_BYTES for p in parts)
    assert "".join(parts) == src


def test_plain_blocks_parse_nothing_and_respect_limits():
    src = "\n\n".join(["**x** <b>y</b>"] * 700)
    blocks = plain_blocks(src)
    assert len(blocks) <= RICH_MAX_BLOCKS
    assert all(b["type"] == "paragraph" and isinstance(b["text"], str) for b in blocks)
    assert "\n\n".join(b["text"] for b in blocks) == src


def test_plain_blocks_clip_to_budget_with_ellipsis():
    src = "\n\n".join(["è" * 50] * 10)
    blocks = plain_blocks(src, budget=300)
    joined = "\n\n".join(b["text"] for b in blocks)
    assert len(joined.encode("utf-8")) <= 300
    assert joined.endswith("…")
    assert not plain_blocks("short", budget=300)[-1]["text"].endswith("…")


def test_flatten_walks_every_block_type():
    rm = {"blocks": [{"type": "heading", "text": "Invoice 913", "size": 1},
                     {"type": "paragraph", "text": ["due ", {"type": "bold", "text": "Friday"}]},
                     {"type": "table", "cells": [[{"text": "a"}, {"text": "b"}]]},
                     {"type": "details", "summary": "More", "blocks": [{"type": "paragraph", "text": "inside"}]},
                     {"type": "buttons", "buttons": [{"text": "Copy IBAN", "copy_text": {"text": "IT60"}}]},
                     {"type": "paragraph", "text": ["Use ", {"type": "button", "button": {"text": "Open file", "url": "https://x.io"}}, " now"]}]}
    out = flatten(rm)
    for word in ("Invoice 913", "due Friday", "a | b", "More", "inside", "Copy IBAN", "Use Open file now"):
        assert word in out
    assert "IT60" not in out and "https://x.io" not in out


def test_flatten_lists_quotes_and_captions():
    rm = {"blocks": [
        {"type": "list", "items": [{"label": "1.", "blocks": [{"type": "paragraph", "text": "first"}]},
                                   {"label": "2.", "blocks": [{"type": "paragraph", "text": "second"}]}]},
        {"type": "blockquote", "blocks": [{"type": "paragraph", "text": "quoted"}], "credit": "Ellen"},
        {"type": "collage", "blocks": [], "caption": {"text": "Photos", "credit": "me"}},
        {"type": "paragraph", "text": {"type": "date_time", "text": "Friday", "unix_time": 1795161600}},
    ]}
    out = flatten(rm)
    for word in ("first", "second", "quoted", "Ellen", "Photos", "Friday"):
        assert word in out
    assert "1795161600" not in out


def test_size_refusal_matches_ptb_shaped_text():
    assert is_size_refusal(Exception("Rich_message_blocks_too_many"))
    assert is_size_refusal(Exception("Bad Request: RICH_MESSAGE_TEXT_TOO_LONG"))
    assert is_size_refusal(Exception("Rich_message_too_large"))
    assert not is_size_refusal(Exception("Rich_message_photo_url_invalid"))


def test_lone_surrogate_is_replaced_before_measuring():
    split("x\ud800")
    assert "\udc00" not in prepare("\udc00 hello")
    plain_blocks("a\ud800")
    assert "\ud800" not in flatten({"blocks": [{"type": "paragraph", "text": "a\ud800"}]})


def test_split_inside_fence_keeps_every_code_line():
    code_lines = []
    for i in range(40):
        code_lines += [f"line_{i} = " + "x" * 50, ""]
    code_lines.pop()
    src = "```py\n" + "\n".join(code_lines) + "\n```"
    parts = split(src, budget=500)
    assert len(parts) >= 2
    interiors = []
    for part in parts:
        lines = part.split("\n")
        assert lines[0] == "```py" and lines[-1] == "```"
        interiors += lines[1:-1]
    assert interiors == code_lines


def test_supported_tag_name_followed_by_prose_is_text():
    assert prepare("it takes <a lot of time> really, done") == "it takes &lt;a lot of time> really, done"
    assert prepare("pick <b or c> later") == "pick &lt;b or c> later"
    assert prepare("<b x>y</b>") == "&lt;b x>y</b>"
    assert prepare("<details open=x>") == "&lt;details open=x>"


def test_real_tags_with_attributes_stay_tags():
    for tag in ("<details open>", "<blockquote expandable>", "<table bordered striped compact>",
                '<input type="checkbox" checked>', '<tg-button type="copy_text" text="x">',
                "<tg-spoiler>", "<br/>", "<br />", "</details >", "<B>"):
        assert prepare(tag) == tag


def test_autolink_passes_through_verbatim():
    assert prepare("see <https://x.io/a_b> and a_b") == "see <https://x.io/a_b> and a&#95;b"
    assert prepare("<http://x.io/p_q>") == "<http://x.io/p_q>"
    assert prepare("<https://x.io/a b>") == "&lt;https://x.io/a b>"


def test_bare_url_stops_at_a_pipe():
    assert prepare("|https://x.io/a|mcp__x|") == "|https://x.io/a|mcp&#95;&#95;x|"


def test_format_guide_injected_where_time_guide_was():
    import agent
    import background_jobs
    from channels.tg_rich import FORMAT_GUIDE
    from tools import ANSWER_SHOWN_LINE
    assert FORMAT_GUIDE in agent._channel_context_block("telegram")
    assert FORMAT_GUIDE not in agent._channel_context_block("voice")
    assert FORMAT_GUIDE in ANSWER_SHOWN_LINE
    assert FORMAT_GUIDE in background_jobs._job_rules()
    assert FORMAT_GUIDE in background_jobs._job_rules(can_ask=False)


def test_format_guide_keeps_the_time_link_rule():
    from channels.tg_rich import FORMAT_GUIDE
    assert "[Monday 16 November at 09:00](time:2026-11-16T09:00+01:00)" in FORMAT_GUIDE
    assert "[16 November](time:2026-11-16)" in FORMAT_GUIDE


def test_format_guide_names_vocabulary():
    from channels.tg_rich import FORMAT_GUIDE
    assert FORMAT_GUIDE.startswith("telegram_formatting:")
    for word in ("<details>", "==", "||", "<tg-button-row>", "copy_text", "table"):
        assert word in FORMAT_GUIDE, word
    assert "no media, maps or callback buttons" in FORMAT_GUIDE.lower()


def test_format_guide_gives_the_forms_that_render():
    from channels.tg_rich import FORMAT_GUIDE
    assert ("a fold is <details><summary>title</summary>, a blank line, the Markdown body, "
            "a blank line, </details>") in FORMAT_GUIDE
    assert ("an expandable quote <blockquote expandable>...</blockquote> and a pull quote "
            "<aside>...<cite>who</cite></aside> take plain text or <b>/<i>, not Markdown") in FORMAT_GUIDE


# ---------------------------------------------------------------------------
# Checked against a real GitHub-flavoured Markdown parser
# ---------------------------------------------------------------------------


def _gfm():
    """CommonMark with GFM tables: the reference for what a text parses to."""
    from markdown_it import MarkdownIt
    return MarkdownIt("commonmark").enable("table")


def _code_texts(markdown: str) -> list[str]:
    return [t.content for t in _gfm().parse(markdown) if t.type in ("fence", "code_block")]


def test_quoted_table_header_widened_behind_its_quote():
    src = "> | Voce | Mese |\n> | --- | --- |\n> | Luce | Novembre | €120,50 |"
    out = prepare(src)
    assert out.splitlines()[:2] == ["> | Voce | Mese | |", "> | --- | --- | --- |"]
    assert "€120,50" not in _gfm().render(src)          # what Telegram would drop
    assert "€120,50" in _gfm().render(out)


def test_nested_quote_table_widened_and_a_shallower_line_ends_it():
    src = "> > | a | b |\n> > |---|---|\n> > | 1 | 2 | 3 |\n> | x | y | z | w |"
    out = prepare(src)
    assert out.splitlines()[0] == "> > | a | b | |"
    assert out.splitlines()[1] == "> > |---|---| --- |"
    assert "<td>3</td>" in _gfm().render(out)


def test_task_start_quote_keeps_a_wide_rows_amount():
    from channels.telegram import TelegramChannel
    request = "Paga queste:\n\n| Voce | Mese |\n| --- | --- |\n| Luce | Novembre | €120,50 |"
    out = prepare(TelegramChannel._task_start_markdown("Sam", request))
    assert "€120,50" in _gfm().render(out)


def test_tilde_fence_is_code_byte_for_byte():
    src = "~~~yaml\nentity_id: light.sala_cucina\n<b>x</b> a < b\n~~~\nafter_x"
    assert prepare(src) == "~~~yaml\nentity_id: light.sala_cucina\n<b>x</b> a < b\n~~~\nafter&#95;x"
    assert _code_texts(prepare(src)) == _code_texts(src)


def test_fence_closes_only_on_its_own_char_and_length():
    src = "````md\n```\na_b\n```\n````\n\n~~~\nc_d\n```\ne_f\n~~~\nlast_x"
    out = prepare(src)
    assert out == src.replace("last_x", "last&#95;x")
    assert _code_texts(out) == _code_texts(src)


def test_backtick_fence_with_an_info_string_with_spaces_is_code():
    src = '```python title="cfg"\nentity_id = 1\n```'
    assert prepare(src) == src


def test_indented_code_block_is_code_byte_for_byte():
    src = "Config:\n\n    entity_id: light.sala_cucina\n\n    x < 3\n\nafter_x"
    out = prepare(src)
    assert out == src.replace("after_x", "after&#95;x")
    assert _code_texts(out) == _code_texts(src) == ["entity_id: light.sala_cucina\n\nx < 3\n"]


def test_tab_indented_code_block_is_code():
    src = "\tentity_id: light.sala_cucina"
    assert prepare(src) == src
    assert _code_texts(src) == ["entity_id: light.sala_cucina\n"]


def test_indented_lines_that_are_not_code_are_encoded():
    # A paragraph continuation, and a list item's own paragraph, are not code.
    for src in ("text_a\n    more_b", "- item_a\n\n    more_b", "1. item_a\n\n    more_b"):
        assert _code_texts(src) == [], src
        assert "_" not in prepare(src), src


def test_indented_code_inside_a_list_item_is_code():
    src = "- item_a\n\n      entity_id: x\n\nafter_b"
    assert _code_texts(src) == ["entity_id: x\n"]
    out = prepare(src)
    assert "      entity_id: x" in out and "item&#95;a" in out and "after&#95;b" in out


def test_split_inside_a_tilde_fence_closes_and_reopens_it():
    src = "~~~yaml\n" + "\n".join(f"entity_{i}: x" * 5 for i in range(200)) + "\n~~~"
    parts = split(src, budget=2_000)
    assert len(parts) >= 2
    for part in parts:
        lines = part.split("\n")
        assert lines[0] == "~~~yaml" and lines[-1] == "~~~", (lines[0], lines[-1])
        assert prepare(part) == part


def test_split_inside_a_long_backtick_fence_closes_with_the_same_length():
    src = "````md\n```\n" + "\n".join("a_b " * 20 for _ in range(200)) + "\n```\n````"
    parts = split(src, budget=2_000)
    assert len(parts) >= 2
    assert all(p.split("\n")[0] == "````md" and p.split("\n")[-1] == "````" for p in parts)


# ---------------------------------------------------------------------------
# Buttons: only URL and copy-text buttons stay buttons
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [
    "callback&#95;data", "&#99;allback_data", "callback&#x5f;data", "callback&#X5F;data",
    "callback&#95data", "CALLBACK_DATA", "web_app", "login_url", "switch_inline_query",
    "disabled", ""])
def test_a_button_that_is_not_url_or_copy_text_becomes_its_label(kind):
    out = prepare(f'<tg-button-row><tg-button type="{kind}" data="v1|proposal|{"f" * 32}|0">'
                  "Pay</tg-button></tg-button-row>")
    assert "<tg-button " not in out and "<tg-button>" not in out
    assert out == "<tg-button-row>Pay</tg-button-row>"


def test_a_button_with_no_type_becomes_its_label():
    assert prepare('<tg-button data="x">Pay</tg-button>') == "Pay"


@pytest.mark.parametrize("kind", ["callback&#95;data", "&#99;allback_data", "web_app"])
def test_a_button_the_rewrite_misses_is_text_whatever_its_type(kind):
    out = prepare(f'<tg-button type="{kind}" data="x">`Pay`</tg-button>')
    assert out.startswith("&lt;tg-button")


def test_url_buttons_kept_and_entity_spelled_types_read_as_written():
    src = '<tg-button type="url" url="https://x.io/a_b">Open</tg-button>'
    assert prepare(src) == src
    src = '<tg-button type="&#117;rl" url="https://x.io">Open</tg-button>'
    assert prepare(src) == src


def test_a_second_type_attribute_cannot_smuggle_a_callback():
    out = prepare('<tg-button type="url" type="callback_data" data="x">Pay</tg-button>')
    assert out == "Pay"


# ---------------------------------------------------------------------------
# Reading back mathematical expressions
# ---------------------------------------------------------------------------


def test_flatten_reads_block_and_inline_math():
    rm = {"blocks": [
        {"type": "mathematical_expression", "expression": "E = mc^2"},
        {"type": "paragraph", "text": ["so ", {"type": "mathematical_expression",
                                               "expression": "x^2 + y^2"}, " holds"]},
    ]}
    out = flatten(rm)
    assert "E = mc^2" in out
    assert "so x^2 + y^2 holds" in out


def test_a_nested_list_item_after_a_blank_line_is_not_code():
    src = "- one_a\n\n    - two_b\n\n        - three_c\n\n            four_d"
    assert _code_texts(src) == []
    assert "_" not in prepare(src)


# ---------------------------------------------------------------------------
# Code is wherever a CommonMark parser finds it
# ---------------------------------------------------------------------------


def _inline_codes(markdown: str) -> list[str]:
    def walk(tokens):
        for t in tokens:
            if t.type == "code_inline":
                yield t.content
            yield from walk(t.children or [])
    return list(walk(_gfm().parse(markdown)))


@pytest.mark.parametrize("src", [
    "> ```yaml\n> entity_id: light.sala_cucina\n> x < 5\n> ```",
    "> ~~~yaml\n> entity_id: light.sala_cucina\n> x < 5\n> ~~~",
    "> Config:\n>\n>     entity_id: light.sala_cucina\n>     x < 5",
    "> - step:\n>\n>   ```\n>   entity_id: light.sala_cucina\n>   x < 5\n>   ```",
])
def test_code_inside_a_quote_is_untouched(src):
    src += "\n\nafter_x"
    out = prepare(src)
    assert out == src.replace("after_x", "after&#95;x")
    assert _code_texts(out) == _code_texts(src) == ["entity_id: light.sala_cucina\nx < 5\n"]


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_task_start_quote_keeps_a_fenced_brief_as_written(fence):
    from channels.telegram import TelegramChannel
    brief = (f"Applica questa config:\n\n{fence}yaml\nentity_id: light.sala_cucina\n"
             f"threshold: <5\n\nmode: on_off\n{fence}\n\nGrazie_mille")
    out = prepare(TelegramChannel._task_start_markdown("Sam", brief))
    assert _code_texts(out) == ["entity_id: light.sala_cucina\nthreshold: <5\n\nmode: on_off\n"]
    assert "Grazie&#95;mille" in out


def test_task_start_quote_keeps_a_table_written_inside_code():
    from channels.telegram import TelegramChannel
    brief = "Formato:\n\n```md\n| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n```"
    out = prepare(TelegramChannel._task_start_markdown("Sam", brief))
    assert _code_texts(out) == ["| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n"]


def test_a_pipe_inside_backticks_still_splits_a_table_cell():
    # GitHub-flavoured Markdown splits a table row on every unescaped pipe,
    # backticks or not, so this row has three cells: "`grep <pattern> file.txt"
    # (an unclosed backtick, so ordinary text holding the tag-like <pattern>),
    # "sort`" and "completato". As text, <pattern> must be sent as &lt;pattern>,
    # or Telegram drops it as an unknown tag.
    src = "| comando | stato |\n| --- | --- |\n| `grep <pattern> file.txt | sort` | completato |"
    assert _inline_codes(src) == []
    out = prepare(src)
    assert "`grep &lt;pattern> file.txt | sort` | completato |" in out
    assert "&lt;pattern&gt;" in _gfm().render(out)
    assert "<td>completato</td>" in _gfm().render(out)       # the widened header keeps it


def test_an_escaped_pipe_keeps_a_code_span_in_its_cell():
    src = "| a | b |\n| --- | --- |\n| `x \\| y_z` | y_z |"
    assert _inline_codes(src) == ["x | y_z"]
    assert prepare(src) == "| a | b |\n| --- | --- |\n| `x \\| y_z` | y&#95;z |"


def test_only_the_cell_outside_code_is_encoded():
    src = "| a | b |\n| --- | --- |\n| `a_b` | a_b |"
    assert prepare(src) == "| a | b |\n| --- | --- |\n| `a_b` | a&#95;b |"


def test_a_fence_inside_a_list_item_is_code():
    src = "- step_one\n\n  ```py\n  a_b < 3\n  ```\n- step_two"
    out = prepare(src)
    assert out == src.replace("step_", "step&#95;")


def test_inline_code_in_a_nested_quote():
    assert prepare("> > use `entity_id` < now_x") == "> > use `entity_id` &lt; now&#95;x"


def test_backtick_runs_as_commonmark_reads_them():
    assert prepare("a `b_c") == "a `b&#95;c"                               # unclosed: text
    assert prepare("``a ` b_c`` d_e") == "``a ` b_c`` d&#95;e"               # a span holding `
    assert prepare("`a_b` and ``c_d` e_f") == "`a_b` and ``c&#95;d` e&#95;f"  # run lengths differ


def test_a_code_span_does_not_run_across_a_quoted_blank_line():
    # The blank quote line ends the paragraph, so neither backtick closes.
    src = "> `a_b\n>\n> c_d`"
    assert _inline_codes(src) == []
    assert prepare(src) == "> `a&#95;b\n>\n> c&#95;d`"


def test_code_in_a_heading_and_a_link_label():
    assert prepare("## Set `entity_id` now_x ##") == "## Set `entity_id` now&#95;x ##"
    assert prepare("[`a_b` c_d](https://x.io/e_f)") == "[`a_b` c&#95;d](https://x.io/e_f)"


# How Telegram reads an HTML block's text (measured on live messages): a code
# span inside <summary> is code, and an entity there shows literally; the body
# of a <details> is Markdown even with no blank line around it; anywhere else
# (<aside>, <blockquote expandable>) backticks are literal and an unknown tag
# inside them is dropped, so that text is encoded like any other.


def test_a_code_span_in_a_summary_is_untouched():
    src = "<details><summary>`entity_id` x < 5 raw a_b</summary>\n\nbody_x\n\n</details>"
    assert prepare(src) == ("<details><summary>`entity_id` x &lt; 5 raw a&#95;b</summary>"
                            "\n\nbody&#95;x\n\n</details>")
    assert prepare("<details><summary>`x < 5` and `a_b`</summary>") == \
        "<details><summary>`x < 5` and `a_b`</summary>"


def test_a_code_span_in_a_quoted_summary_is_untouched():
    assert prepare("> <details><summary>`a_b` c_d</summary>") == \
        "> <details><summary>`a_b` c&#95;d</summary>"


@pytest.mark.parametrize("src, out", [
    ("<aside>Usa `grep <pattern> file.txt`</aside>",
     "<aside>Usa `grep &lt;pattern> file.txt`</aside>"),
    ("<blockquote expandable>`a_b` raw c_d</blockquote>",
     "<blockquote expandable>`a&#95;b` raw c&#95;d</blockquote>"),
    ("  <aside>\n  `mcp__x` y_z\n  </aside>",
     "  <aside>\n  `mcp&#95;&#95;x` y&#95;z\n  </aside>"),
])
def test_backticks_in_an_aside_or_expandable_quote_are_text(src, out):
    assert prepare(src) == out


@pytest.mark.parametrize("fence", ["~~~", "```"])
def test_a_fence_in_a_details_body_is_code(fence):
    src = f"<details><summary>t_x</summary>\n{fence}\na_b x<y\n{fence}\n</details>"
    assert prepare(src) == src.replace("t_x", "t&#95;x")


def test_a_details_body_is_markdown_without_blank_lines():
    src = ("<details>\n<summary>Log</summary>\nsee `a_b` and c_d\n\n    e_f < g\n\n"
           "h_i\n</details>\nafter_x")
    assert prepare(src) == ("<details>\n<summary>Log</summary>\nsee `a_b` and c&#95;d\n\n"
                            "    e_f < g\n\nh&#95;i\n</details>\nafter&#95;x")


def test_two_folds_with_no_blank_line_between_keep_their_code():
    src = ("<details><summary>A</summary>\n`a_b`\n</details>\n"
           "<details><summary>B</summary>\n`c_d` e_f\n</details>")
    assert prepare(src) == src.replace("e_f", "e&#95;f")


def test_a_fence_with_a_blank_line_in_a_fold_stays_whole():
    # Telegram reads <details> as a container: this is one yaml block in the
    # fold, then a paragraph after it (measured on a live message).
    src = ("<details><summary>Config</summary>\n```yaml\na_b: 1\n\nc_d: 2\n```\n</details>"
           "\n\nafter_x here")
    assert prepare(src) == src.replace("after_x", "after&#95;x")


def test_three_folds_in_a_row_read_each_body_as_markdown():
    fold = "<details open><summary>`s_{0}` t_{0}</summary>\n~~~\nk_{0} < 1\n\nv_{0}\n~~~\nw_{0}\n</details>"
    src = "\n".join(fold.format(i) for i in range(3))
    out = prepare(src)
    for i in range(3):
        assert f"<details open><summary>`s_{i}` t&#95;{i}</summary>" in out
        assert f"~~~\nk_{i} < 1\n\nv_{i}\n~~~\nw&#95;{i}\n</details>" in out


def test_a_fold_inside_a_quote_reads_its_body_as_markdown():
    src = "> <details><summary>Log</summary>\n> ```\n> a_b\n>\n> c_d\n> ```\n> </details>\n> e_f"
    assert prepare(src) == src.replace("e_f", "e&#95;f")


def test_a_fold_tag_line_with_a_crlf_is_still_a_tag_line():
    src = "<details>\r\n<summary>S</summary>\r\n~~~\r\na_b\r\n\r\nc_d\r\n~~~\r\n</details>\r\n"
    assert prepare(src) == src.replace("\r\n", "\n")      # line breaks go out as "\n"


def test_a_summary_on_lines_of_its_own_is_a_fold():
    src = ("<details>\n<summary>\nConfigurazione cucina_x\n</summary>\n```yaml\n"
           "entity_id: light.cucina\n```\n</details>")
    assert prepare(src) == src.replace("cucina_x", "cucina&#95;x")


def test_a_fold_opened_behind_a_list_marker_keeps_its_body_code():
    src = ("- <details><summary>Configurazione</summary>\n  ```yaml\n  entity_id: light.cucina\n"
           "  x < 5\n  ```\n  </details>\n- next_x")
    assert prepare(src) == src.replace("next_x", "next&#95;x")


@pytest.mark.parametrize("opener", ['<details class="x">', '<details open class="x">',
                                    '<details class="x"><summary>T</summary>'])
def test_a_fold_opener_with_attributes_is_a_fold(opener):
    src = f"{opener}\n~~~\na_b < c\n~~~\n</details>\nd_e"
    assert prepare(src) == src.replace("d_e", "d&#95;e")


def test_a_fold_shown_as_indented_code_stays_code():
    src = ("Example:\n\n    <details>\n    <summary>Show a_b < c</summary>\n    body_x\n"
           "    </details>\n\nend_y")
    assert _code_texts(src) == ["<details>\n<summary>Show a_b < c</summary>\nbody_x\n</details>\n"]
    assert prepare(src) == src.replace("end_y", "end&#95;y")


def test_a_code_span_in_a_summary_is_not_cut():
    unit = "`grep -r entity_id /config`"
    src = "<details><summary>" + "x" * (1_990 - len(unit) // 2) + unit + "y" * 500 + "</summary>"
    parts = split(src, 2_000)
    assert len(parts) >= 2 and "".join(parts) == src
    assert _one_part_holds(unit, parts)


def test_a_cut_inside_a_quoted_fence_closes_it_inside_the_quote():
    lines = [f"entity_{i}: light.sala_{i}" for i in range(300)]
    src = "> ```yaml\n" + "\n".join("> " + line for line in lines) + "\n> ```"
    parts = split(src, 2_000)
    assert len(parts) >= 2
    interiors = []
    for part in parts:
        fences = _code_texts(part)
        assert len(fences) == 1, fences          # no stray empty code block
        assert prepare(part) == part
        interiors += fences[0].splitlines()
    assert interiors == lines


def test_a_backtick_inside_a_tag_attribute_is_not_a_code_span():
    assert prepare('<details><summary title="`a_b">c_d `e_f`</summary>') == \
        '<details><summary title="`a_b">c&#95;d `e_f`</summary>'


def test_a_crlf_text_goes_out_with_plain_line_breaks():
    # Telegram reads "\r\n" as "\n"; read the same way here, a single space
    # before "\r\n" is no hard line break.
    src = "a_b `c_d`\r\n> `e_f` g_h\r\nnext \r\nline\rend"
    assert prepare(src) == "a&#95;b `c_d`\n> `e_f` g&#95;h\\\nnext \\\nline\\\nend"


# ---------------------------------------------------------------------------
# A cut never falls inside a link, a tag or a code span
# ---------------------------------------------------------------------------

_INVOICE = "[invoice](https://example.test/dossier?invoice=IT_2026_001)"


def _one_part_holds(unit: str, parts: list[str]) -> bool:
    return [unit in p for p in parts].count(True) == 1


def test_a_long_paragraph_is_not_cut_inside_its_closing_link():
    words = ("parola " * 6000)[:32_030 - len(_INVOICE) - 1] + " "
    src = words + _INVOICE
    assert len(src.encode("utf-8")) == 32_030
    parts = split(src, 32_000)
    assert len(parts) == 2
    for part in parts:
        prepared = prepare(part)
        assert _INVOICE in prepared or not any(
            piece in prepared for piece in ("[invoice", "](https", "IT_2026_001)"))
    assert "".join(parts).count(_INVOICE) == 1
    assert "".join(parts) == src


@pytest.mark.parametrize("unit", [
    _INVOICE,
    "<https://example.test/dossier/IT_2026_001>",
    "[venerdì 20 novembre](time:2026-11-20T09:00+01:00)",
    "`grep -r entity_id /config/automations.yaml`",
    '<a href="https://example.test/dossier?invoice=IT_2026_001">',
])
def test_a_text_with_no_spaces_is_not_cut_inside_a_unit(unit):
    src = "x" * (2_000 - len(unit) // 2) + unit + "y" * 500
    parts = split(src, 2_000)
    assert len(parts) >= 2
    assert all(len(prepare(p).encode("utf-8")) <= 2_000 for p in parts)
    assert "".join(parts) == src
    assert _one_part_holds(unit, parts)


def test_a_space_inside_a_link_label_is_not_a_cut():
    src = "parola " * 250 + "[la fattura di novembre del condominio](https://example.test/f_1)"
    parts = split(src, 1_790)
    assert "".join(parts) == src
    assert _one_part_holds("[la fattura di novembre del condominio](https://example.test/f_1)", parts)


def test_a_paragraph_with_no_spaces_still_splits_and_ends():
    src = "a_b" * 5_000
    parts = split(src, 2_000)
    assert len(parts) >= 2
    assert all(len(prepare(p).encode("utf-8")) <= 2_000 for p in parts)
    assert "".join(parts) == src


def test_a_link_bigger_than_the_budget_is_still_cut():
    src = "[x](https://example.test/" + "a" * 5_000 + ")"
    parts = split(src, 1_000)
    assert len(parts) >= 5
    assert all(len(prepare(p).encode("utf-8")) <= 1_000 for p in parts)
    assert "".join(parts) == src


# ---------------------------------------------------------------------------
# A single line break stays a line break
# ---------------------------------------------------------------------------
# Telegram joins the lines of a paragraph into one, as CommonMark reads a
# soft line break; a backslash before the line break makes it a hard one.


def test_lines_written_one_under_another_stay_on_their_own_lines():
    src = "**Call from Marco** (2 min)\n**Reason:** viewing\n**Outcome:** Message left"
    out = prepare(src)
    assert out == ("**Call from Marco** (2 min)\\\n**Reason:** viewing\\\n"
                   "**Outcome:** Message left")
    assert _gfm().render(out).count("<br />") == 2


@pytest.mark.parametrize("src, out", [
    ("> q one\n> q two_x", "> q one\\\n> q two&#95;x"),
    ("> > deep one\n> > deep two", "> > deep one\\\n> > deep two"),
    ("- item_a\ncontinued", "- item&#95;a\\\ncontinued"),
    ("1. first\n   second", "1. first\\\n   second"),
    ("<details><summary>T</summary>\n\nline one\nline two\n\n</details>",
     "<details><summary>T</summary>\n\nline one\\\nline two\n\n</details>"),
    ("<details><summary>T</summary>\nline one\nline two\n</details>",
     "<details><summary>T</summary>\nline one\\\nline two\n</details>"),
    ("a\r\nb", "a\\\nb"),
    ("a \nb", "a \\\nb"),
    ("a\\\\\nb", "a\\\\\\\nb"),
    ("`a\nb` c\nd", "`a\nb` c\\\nd"),
    # none after a bare "1.": a backslash would make it a list item
    ("text\n1.\nmore", "text\\\n1.\nmore"),
])
def test_a_soft_line_break_becomes_a_hard_one(src, out):
    assert prepare(src) == out


@pytest.mark.parametrize("src", [
    "```\na_b\nc\nd\n```",
    "    a\n    b",
    "| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |",
    "two spaces  \nnext",
    "backslash\\\nnext",
    "x<br>\nnext",
    "x<br />\nnext",
    "first paragraph\n\nsecond paragraph",
    "Approve the payment?\n\n✓ Approved",
    "<aside>one\ntwo</aside>",
    "# Heading\nbody",
])
def test_line_breaks_that_need_nothing_are_left_alone(src):
    assert prepare(src) == src


def test_the_plain_fallback_and_reading_back_keep_lines_as_written():
    assert plain_blocks("a_b\nc") == [{"type": "paragraph", "text": "a_b\nc"}]
    assert flatten({"blocks": [{"type": "paragraph", "text": "a\nb"}]}) == "a\nb"


def test_a_time_link_ending_a_line_keeps_the_break_after_it():
    unix = int(datetime.fromisoformat("2026-11-16T09:00+01:00").timestamp())
    out = prepare("[Monday](time:2026-11-16T09:00+01:00)\nnext")
    assert out == f'<tg-time unix="{unix}">Monday</tg-time>\\\nnext'


def test_split_counts_the_added_backslashes():
    src = "\n".join(f"riga {i}" for i in range(3_000))
    parts = split(src, 2_000)
    assert all(len(prepare(p).encode("utf-8")) <= 2_000 for p in parts)
    assert "\n".join(parts) == src
