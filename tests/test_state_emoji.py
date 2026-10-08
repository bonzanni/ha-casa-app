"""Lookup-table coverage for state_emoji per spec §6.3, §6.9."""

from __future__ import annotations


def test_state_emoji_table_covers_locked_states():
    from channels.state_emoji import STATE_EMOJI
    assert STATE_EMOJI["active"] == "🟢"
    assert STATE_EMOJI["awaiting"] == "🟡"
    assert STATE_EMOJI["completed"] == "✅"
    assert STATE_EMOJI["failed"] == "❌"
    assert STATE_EMOJI["cancelled"] == "⏹"


def test_role_emoji_table_covers_known_executors():
    from channels.state_emoji import ROLE_EMOJI
    assert ROLE_EMOJI["configurator"] == "⚙️"
    assert ROLE_EMOJI["plugin-developer"] == "🛠"
    # casa-builder and automation-builder removed — never shipped as
    # actual executors; entries dropped 2026-05-13 as part of v0.37.1
    # D-1 cleanup. ROLE_EMOJI is retained for any future caller that
    # wants the visual role glyph in non-topic contexts (logs, etc.).
    assert "casa-builder" not in ROLE_EMOJI
    assert "automation-builder" not in ROLE_EMOJI


def test_role_emoji_unknown_falls_back_to_robot():
    from channels.state_emoji import role_emoji
    assert role_emoji("totally-unknown-type") == "🤖"
    # Previously-known aspirational roles also fall through to default
    # after the v0.37.1 ROLE_EMOJI cleanup.
    assert role_emoji("casa-builder") == "🤖"
    assert role_emoji("automation-builder") == "🤖"


def test_progress_glyphs_match_spec():
    from channels.state_emoji import PROGRESS_GLYPH
    assert PROGRESS_GLYPH["pending"] == "☐"
    assert PROGRESS_GLYPH["completed"] == "☑"
    assert PROGRESS_GLYPH["blocked"] == "🚫"
    assert PROGRESS_GLYPH["skipped"] == "⏭"
    assert PROGRESS_GLYPH["in_progress"] == "⏳"


def test_compose_topic_title_format():
    from channels.state_emoji import compose_topic_title
    # v0.37.1 D-1: title no longer carries the role emoji — bubble
    # does (via channels.topic_icons). Format is "<state> <task>".
    title = compose_topic_title(
        state="active", short_task="add Skill probe-foo",
    )
    assert title == "🟢 add Skill probe-foo"


def test_concision_strips_filler_drops_articles():
    from channels.state_emoji import concise_task
    assert concise_task(
        "Please add a Skill for the casa-probe-foo plugin"
    ).startswith("add Skill")
    assert concise_task(
        "Can you wire up the morning briefing trigger?"
    ).startswith("wire morning") or concise_task(
        "Can you wire up the morning briefing trigger?"
    ).startswith("wire up morning")


def test_concise_task_respects_byte_budget():
    """U3_TASK_BYTE_BUDGET = 26 (v0.37.1: bumped from 22 after
    dropping the role-emoji prefix from the title)."""
    from channels.state_emoji import concise_task, U3_TASK_BYTE_BUDGET
    assert U3_TASK_BYTE_BUDGET == 26
    long = "make the Telegram bot post a snazzy progress panel"
    out = concise_task(long)
    assert len(out.encode("utf-8")) <= U3_TASK_BYTE_BUDGET


def test_compose_topic_title_unknown_state_falls_back_to_active():
    from channels.state_emoji import compose_topic_title
    title = compose_topic_title(state="banana", short_task="x")
    assert title.startswith("🟢")
    assert title == "🟢 x"


# ---------------------------------------------------------------------------
# W-R6 (v0.81.0): normalize_topic_title — engager-supplied short topic title.
# ---------------------------------------------------------------------------


def test_normalize_topic_title_passes_short_clean_title():
    from channels.state_emoji import normalize_topic_title
    assert normalize_topic_title("Gmail plugin") == "Gmail plugin"


def test_normalize_topic_title_caps_to_three_words_at_word_boundary():
    from channels.state_emoji import normalize_topic_title
    assert normalize_topic_title("one two three four five") == "one two three"


def test_normalize_topic_title_caps_to_char_budget_at_word_boundary():
    from channels.state_emoji import normalize_topic_title, TOPIC_TITLE_CHAR_CAP
    # Three long words exceed the char cap → dropped at a WORD boundary.
    out = normalize_topic_title("configuration deployment orchestration")
    assert len(out) <= TOPIC_TITLE_CHAR_CAP
    # Never a mid-word cut: the result is a whitespace-joined prefix.
    assert out in "configuration deployment orchestration"
    assert not out.endswith(" ")


def test_normalize_topic_title_ellipsizes_lone_long_word():
    from channels.state_emoji import normalize_topic_title, TOPIC_TITLE_CHAR_CAP
    # #357: a single word longer than the cap has no word boundary to cut
    # at — ellipsize instead of returning a raw character slice that
    # masquerades as the whole word.
    out = normalize_topic_title("restart-backup-after-migration")
    assert len(out) <= TOPIC_TITLE_CHAR_CAP
    assert out.endswith("…")
    assert "restart-backup-after-migration".startswith(out[:-1])


def test_normalize_topic_title_ellipsizes_when_first_word_exceeds_cap():
    from channels.state_emoji import normalize_topic_title, TOPIC_TITLE_CHAR_CAP
    out = normalize_topic_title("supercalifragilisticexpialidocious now")
    assert len(out) <= TOPIC_TITLE_CHAR_CAP
    assert out.endswith("…")


def test_normalize_topic_title_unsafe_text_falls_back_to_empty():
    from channels.state_emoji import normalize_topic_title
    # A newline (C0 control) is UNSAFE-TEXT → rejected → "" (caller derives).
    assert normalize_topic_title("Gmail\nplugin") == ""
    # A bidi override is also UNSAFE.
    assert normalize_topic_title("safe‮title") == ""


def test_normalize_topic_title_blank_and_non_str_fall_back_to_empty():
    from channels.state_emoji import normalize_topic_title
    assert normalize_topic_title("") == ""
    assert normalize_topic_title("   ") == ""
    assert normalize_topic_title(None) == ""
    assert normalize_topic_title(123) == ""


# #1368: a long middle word crossing the cap must not leave the first word
# alone — "Remove water-plants-test trigger" was shown as just "Remove" in
# the topic name and in the hand-off line of the person's chat.
def test_normalize_topic_title_keeps_the_object_when_a_long_word_crosses_the_cap():
    from channels.state_emoji import normalize_topic_title, TOPIC_TITLE_CHAR_CAP
    out = normalize_topic_title("Remove water-plants-test trigger")
    assert out == "Remove water-plants-tes…"
    assert len(out) <= TOPIC_TITLE_CHAR_CAP


def test_normalize_topic_title_still_cuts_at_a_word_when_that_keeps_most():
    from channels.state_emoji import normalize_topic_title
    assert normalize_topic_title("Add water-plants-test trigger") == \
        "Add water-plants-test"


def test_concise_task_keeps_the_object_when_a_long_word_crosses_the_budget():
    from channels.state_emoji import concise_task, U3_TASK_BYTE_BUDGET
    out = concise_task("Remove water-plants-test trigger")
    assert out == "Remove water-plants-tes…"
    assert len(out.encode("utf-8")) <= U3_TASK_BYTE_BUDGET
