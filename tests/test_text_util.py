"""Tests for casa.text_util: truncate_for_topic, sanitize_segment, and the
pinned UNSAFE-TEXT predicate (is_unsafe_text) added in v0.78.0 W1.

Telegram's createForumTopic name parameter is limited to ~128 bytes
(per Bot API). The helper must respect this BYTE budget (not char),
break on word boundaries when possible, and signal truncation with
a Unicode ellipsis '…' (3 UTF-8 bytes).
"""

from __future__ import annotations

import pytest

from text_util import is_unsafe_text, sanitize_segment, truncate_for_topic


def test_truncate_short_text_passes_through():
    out = truncate_for_topic("hello", byte_budget=128)
    assert out == "hello"


def test_truncate_word_boundary_with_ellipsis():
    text = "Add a one-line personality trait to Ellen's agent config"
    # Budget chosen so we cut mid-sentence on a word boundary.
    out = truncate_for_topic(text, byte_budget=30)
    assert out.endswith("…")
    # Must not slice mid-word: trailing char before '…' is whitespace
    # ABSENT (we strip), and the chars that ARE there form a complete
    # prefix of the original up to a word boundary.
    body = out[:-1]  # drop the '…'
    assert text.startswith(body)
    # The break point must coincide with a space in the original.
    assert text[len(body)] == " " or len(body) == len(text)
    # And total bytes must fit budget.
    assert len(out.encode("utf-8")) <= 30


def test_truncate_strips_trailing_punctuation():
    """Word-boundary truncation must not leave dangling ', ' or '. '
    before the ellipsis."""
    text = "Hello, world. Sample task that goes on and on and on"
    # Budget chosen so the natural word break would leave ", " or
    # ". " just before the '…'. The helper must strip the punctuation.
    out = truncate_for_topic(text, byte_budget=20)
    assert out.endswith("…")
    # Char immediately before '…' must NOT be ',', ';', ':', '.', or ' '.
    pre = out[:-1].rstrip()
    assert pre and pre[-1] not in ",;:."


def test_truncate_byte_budget_emoji():
    """Multi-byte characters count by UTF-8 byte length, not Python
    char length."""
    # Each '🎉' is 4 UTF-8 bytes. With a 12-byte budget we can fit
    # at most 2 emoji (8 bytes) + '…' (3 bytes) = 11 bytes.
    text = "🎉🎉🎉🎉 task"
    out = truncate_for_topic(text, byte_budget=12)
    assert len(out.encode("utf-8")) <= 12


def test_truncate_pathological_short_budget():
    # No room for the 3-byte '…' → empty string.
    assert truncate_for_topic("hello world", byte_budget=2) == ""
    assert truncate_for_topic("hello world", byte_budget=0) == ""
    # Exactly 3 bytes fits only the ellipsis when truncation is needed.
    out = truncate_for_topic("hello world", byte_budget=3)
    assert out == "…"


def test_truncate_single_long_word():
    """A single word longer than budget: hard byte-cut + '…'."""
    text = "supercalifragilisticexpialidocious"  # 34 ASCII bytes
    out = truncate_for_topic(text, byte_budget=10)
    assert out.endswith("…")
    assert len(out.encode("utf-8")) <= 10
    # First chars must be a prefix of the original word.
    assert text.startswith(out[:-1])


# --- sanitize_segment (moved here from plugin_grants in v0.78.0 W1;
# re-exported by plugin_grants for its existing callers/tests) -------------


def test_sanitize_segment_keeps_hyphens_and_underscores():
    assert sanitize_segment("lesina-invoice") == "lesina-invoice"
    assert sanitize_segment("a_b-c9") == "a_b-c9"


def test_sanitize_segment_replaces_other_chars():
    assert sanitize_segment("my plugin.v2") == "my_plugin_v2"


# --- is_unsafe_text: pinned UNSAFE-TEXT predicate (v0.78.0 W1/W2 design) ---


def test_is_unsafe_text_safe_ascii_and_unicode_pass():
    assert is_unsafe_text("hello world") is False
    assert is_unsafe_text("Delete the draft for {period} — café") is False
    assert is_unsafe_text("") is False


@pytest.mark.parametrize("codepoint", [
    0x0000,   # C0: NUL
    0x0009,   # C0: TAB
    0x000A,   # C0: newline
    0x000D,   # C0: CR
    0x001F,   # C0: last C0 codepoint
])
def test_is_unsafe_text_c0_group(codepoint):
    assert is_unsafe_text(f"pre{chr(codepoint)}post") is True


@pytest.mark.parametrize("codepoint", [0x007F, 0x0080, 0x009F])
def test_is_unsafe_text_c1_group(codepoint):
    assert is_unsafe_text(f"pre{chr(codepoint)}post") is True


def test_is_unsafe_text_line_paragraph_separators():
    assert is_unsafe_text(f"pre{chr(0x2028)}post") is True  # U+2028
    assert is_unsafe_text(f"pre{chr(0x2029)}post") is True  # U+2029


def test_is_unsafe_text_arabic_letter_mark():
    assert is_unsafe_text(f"pre{chr(0x061C)}post") is True  # U+061C


@pytest.mark.parametrize("codepoint", [0x200E, 0x200F])
def test_is_unsafe_text_lrm_rlm_group(codepoint):
    assert is_unsafe_text(f"pre{chr(codepoint)}post") is True


@pytest.mark.parametrize("codepoint", [0x202A, 0x202B, 0x202C, 0x202D, 0x202E])
def test_is_unsafe_text_bidi_embedding_override_group(codepoint):
    assert is_unsafe_text(f"pre{chr(codepoint)}post") is True


@pytest.mark.parametrize("codepoint", [0x2066, 0x2067, 0x2068, 0x2069])
def test_is_unsafe_text_bidi_isolate_group(codepoint):
    assert is_unsafe_text(f"pre{chr(codepoint)}post") is True


def test_is_unsafe_text_boundary_just_outside_ranges_is_safe():
    """Codepoints immediately adjacent to a pinned range must NOT trigger —
    guards against an off-by-one in the range table."""
    assert is_unsafe_text(chr(0x0020)) is False   # just after C0 (space)
    assert is_unsafe_text(chr(0x007E)) is False   # just before C1 (~)
    assert is_unsafe_text(chr(0x00A0)) is False   # just after C1
    assert is_unsafe_text(chr(0x2027)) is False   # just before U+2028
    assert is_unsafe_text(chr(0x202F)) is False   # just after bidi embed group
    assert is_unsafe_text(chr(0x2065)) is False   # just before bidi isolates
    assert is_unsafe_text(chr(0x206A)) is False   # just after bidi isolates


# ---------------------------------------------------------------------------
# utf16_len / utf16_prefix_end (#305/#328 — Telegram counts UTF-16 units, not
# code points; the shared helper is the single home for that measurement so
# the splitter, the plain streaming checks, and the authz challenge size gate
# can never drift apart again).
# ---------------------------------------------------------------------------

from text_util import utf16_len, utf16_prefix_end  # noqa: E402


def test_utf16_len_ascii_matches_len():
    assert utf16_len("hello") == 5
    assert utf16_len("") == 0


def test_utf16_len_bmp_counts_one_per_char():
    # CJK + accents live in the BMP: 1 UTF-16 unit each, same as len().
    assert utf16_len("中文ü") == 3


def test_utf16_len_astral_counts_two():
    # Astral codepoints (most emoji) are surrogate PAIRS: 2 units each.
    assert utf16_len("\U0001F389") == 2
    assert utf16_len("a\U0001F389b") == 4


def test_utf16_len_never_raises_on_lone_surrogate():
    # A surrogateescape'd str must be measured, not crash the send path.
    assert utf16_len("a\ud800b") == 3


def test_utf16_prefix_end_ascii():
    assert utf16_prefix_end("abcdef", 0, 3) == 3
    assert utf16_prefix_end("abc", 0, 10) == 3


def test_utf16_prefix_end_never_splits_inside_astral_pair():
    # budget 2 after "a" (1 unit): the astral char needs 2 more units — it
    # does not fit, so the boundary lands BEFORE it, never between surrogates.
    assert utf16_prefix_end("a\U0001F389b", 0, 2) == 1
    assert utf16_prefix_end("a\U0001F389b", 0, 3) == 2


def test_utf16_prefix_end_honours_start():
    assert utf16_prefix_end("abcdef", 2, 2) == 4


def test_truncate_cuts_inside_a_word_when_the_boundary_keeps_too_little():
    """#1368: a break at the only space before the budget would keep one short
    word ("Remove…"); a cut inside the long word keeps the object visible."""
    out = truncate_for_topic("Remove water-plants-test trigger", byte_budget=26)
    assert out == "Remove water-plants-tes…"
