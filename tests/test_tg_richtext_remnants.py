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
# Fences
# --------------------------------------------------------------------------

def test_pipes_inside_fence_untouched():
    src = "```\n| a | b |\n|---|---|\n```"
    display, spans = parse_markdown(src)
    assert display == "| a | b |\n|---|---|"
    assert spans == [(0, len(display), "pre")]


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
