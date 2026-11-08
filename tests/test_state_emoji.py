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
    assert "plugin-developer" not in ROLE_EMOJI      # no longer shipped
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


def test_normalize_topic_title_has_no_word_cap():
    from channels.state_emoji import normalize_topic_title
    # v0.347.12: the 3-word cap is gone; a short many-word title stays whole.
    assert normalize_topic_title("one two three four five") == \
        "one two three four five"


# The two production titles the old 3-word cap cut silently.
def test_normalize_topic_title_keeps_install_fido_browser_specialist_whole():
    from channels.state_emoji import normalize_topic_title
    assert normalize_topic_title("Install Fido (browser specialist)") == \
        "Install Fido (browser specialist)"


def test_normalize_topic_title_keeps_upgrade_alex_to_whole():
    from channels.state_emoji import normalize_topic_title
    assert normalize_topic_title("Upgrade Alex to version 0.27.0") == \
        "Upgrade Alex to version 0.27.0"


def test_normalize_topic_title_caps_at_forty_chars():
    from channels.state_emoji import TOPIC_TITLE_CHAR_CAP
    assert TOPIC_TITLE_CHAR_CAP == 40


def test_normalize_topic_title_keeps_a_title_of_exactly_the_cap():
    from channels.state_emoji import normalize_topic_title, TOPIC_TITLE_CHAR_CAP
    title = "Wire the Gmail plugin secrets from vault"
    assert len(title) == TOPIC_TITLE_CHAR_CAP
    assert normalize_topic_title(title) == title


def test_normalize_topic_title_cuts_between_words_and_marks_the_cut():
    from channels.state_emoji import normalize_topic_title, TOPIC_TITLE_CHAR_CAP
    out = normalize_topic_title(
        "Install the Fido browser specialist and wire its vault secrets")
    assert out == "Install the Fido browser specialist and…"
    assert len(out) <= TOPIC_TITLE_CHAR_CAP


def test_normalize_topic_title_a_shortened_title_always_shows_the_ellipsis():
    from channels.state_emoji import normalize_topic_title, TOPIC_TITLE_CHAR_CAP
    for raw in (
        "Install the Fido browser specialist and wire its vault secrets",
        "configuration deployment orchestration reconciliation pipeline",
        "Remove the water-plants-test-automation-trigger-for-the-balcony",
        "a " * 30,
    ):
        out = normalize_topic_title(raw)
        if out != raw.strip():
            assert out.endswith("…"), (raw, out)
        assert len(out) <= TOPIC_TITLE_CHAR_CAP, (raw, out)
        # Cut only between words: the kept words are a prefix of the title's.
        kept = out[:-1].split() if out.endswith("…") else out.split()
        assert kept == raw.split()[:len(kept)], (raw, out)


def test_normalize_topic_title_ellipsizes_lone_long_word():
    from channels.state_emoji import normalize_topic_title, TOPIC_TITLE_CHAR_CAP
    # #357: a single word longer than the cap has no word boundary to cut
    # at — ellipsize instead of returning a raw character slice that
    # masquerades as the whole word.
    word = "restart-backup-after-migration-and-verify-it"
    out = normalize_topic_title(word)
    assert out == "restart-backup-after-migration-and-veri…"
    assert len(out) <= TOPIC_TITLE_CHAR_CAP
    assert word.startswith(out[:-1])


def test_normalize_topic_title_ellipsizes_when_first_word_exceeds_cap():
    from channels.state_emoji import normalize_topic_title, TOPIC_TITLE_CHAR_CAP
    out = normalize_topic_title(
        "supercalifragilisticexpialidocious-and-more now")
    assert out == "supercalifragilisticexpialidocious-and-…"
    assert len(out) <= TOPIC_TITLE_CHAR_CAP


# r1 (Terra): 40 emoji fit the char cap but not Telegram's 128-byte topic
# name once the state emoji is in front.
def test_normalize_topic_title_fits_the_topic_name_byte_limit():
    from channels.state_emoji import (
        STATE_EMOJI, compose_topic_title, normalize_topic_title,
    )
    from text_util import TELEGRAM_TOPIC_NAME_BYTES
    for raw in ("😀" * 40, "😀😀 " * 14, "Install 😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀😀"):
        out = normalize_topic_title(raw)
        assert out.endswith("…"), (raw, out)
        for state in STATE_EMOJI:
            name = compose_topic_title(state=state, short_task=out)
            assert len(name.encode("utf-8")) <= TELEGRAM_TOPIC_NAME_BYTES, name


def test_normalize_topic_title_a_cut_keeps_the_separators_it_keeps():
    from channels.state_emoji import normalize_topic_title
    raw = "alpha\u00a0beta  gamma delta epsilon zeta eta theta"
    assert normalize_topic_title(raw) == "alpha\u00a0beta  gamma delta epsilon zeta…"


# r2 (Astra): a JSON "\ud800" in the tool call decodes to a lone surrogate;
# measuring its bytes raised UnicodeEncodeError and broke the launch.
def test_normalize_topic_title_replaces_a_lone_surrogate():
    from channels.state_emoji import normalize_topic_title
    import json
    raw = json.loads('"Repair \\ud800 lamp"')
    assert normalize_topic_title(raw) == "Repair \ufffd lamp"
    long = json.loads('"\\ud800' + 'x' * 50 + '"')
    out = normalize_topic_title(long)
    assert out.startswith("\ufffd") and out.endswith("…")
    out.encode("utf-8")


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


# #1368: "Remove water-plants-test trigger" was shown as just "Remove". With
# the 40-char cap it fits whole, so the object is kept.
def test_normalize_topic_title_keeps_the_object_when_a_long_word_crosses_the_cap():
    from channels.state_emoji import normalize_topic_title
    assert normalize_topic_title("Remove water-plants-test trigger") == \
        "Remove water-plants-test trigger"


def test_concise_task_keeps_the_object_when_a_long_word_crosses_the_budget():
    from channels.state_emoji import concise_task, U3_TASK_BYTE_BUDGET
    out = concise_task("Remove water-plants-test trigger")
    assert out == "Remove water-plants-tes…"
    assert len(out.encode("utf-8")) <= U3_TASK_BYTE_BUDGET
