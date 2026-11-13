"""Tappable times: a Markdown link whose target is ``time:<ISO 8601>`` becomes
a Telegram ``date_time`` entity over its label. Casa parses only that target;
it never looks for times in free text. A target it cannot read leaves the
label as plain text, and the rest of the line keeps its formatting."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from telegram import MessageEntity

import agent
import background_jobs
from channels.tg_richtext import (
    TIME_LINK_GUIDE,
    plain_with_link_targets,
    render,
    render_paged,
    time_instant,
)
from tools import ANSWER_SHOWN_LINE

CASA = Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"
MON_0900_ROME = 1794816000          # 2026-11-16T08:00:00Z
MON_MIDNIGHT_ROME = 1794783600      # 2026-11-15T23:00:00Z


@pytest.fixture(autouse=True)
def _rome(monkeypatch):
    """Casa's timezone is Rome here; ``resolve_tz`` caches its answer, so the cache is
    cleared around each test and a zone an earlier test cached never leaks in or out."""
    import timekeeping
    monkeypatch.setenv("CASA_TZ", "Europe/Rome")
    timekeeping.resolve_tz.cache_clear()
    yield
    timekeeping.resolve_tz.cache_clear()


def _times(ents):
    return [
        (e.offset, e.length, int(e.unix_time.timestamp()))
        for e in ents or [] if e.type == MessageEntity.DATE_TIME
    ]


def test_a_time_link_becomes_a_date_time_entity_over_its_label():
    display, ents = render(
        "Call [Monday 16 November at 09:00](time:2026-11-16T09:00+01:00).")
    assert display == "Call Monday 16 November at 09:00."
    (e,) = ents
    assert (e.type, e.offset, e.length, int(e.unix_time.timestamp())) == (
        "date_time", 5, 27, MON_0900_ROME)
    # No format: the reader sees the model's own words.
    assert e.date_time_format is None


_REAL_PTB_SCRIPT = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {casa!r})
    from telegram import MessageEntity  # REAL ptb
    from channels.tg_richtext import render

    display, ents = render("🧾 [Mon](time:2026-11-16T09:00+01:00)")
    (e,) = ents
    assert e.type == MessageEntity.DATE_TIME, e.type
    assert (e.offset, e.length) == (3, 3), (e.offset, e.length)
    d = e.to_dict()
    assert d["unix_time"] == {ts}, d
    assert "date_time_format" not in d, d
    print("OK")
    """
).format(casa=str(CASA), ts=MON_0900_ROME)


def test_the_real_ptb_serialises_the_entity_with_its_unix_time_and_no_format():
    import os
    proc = subprocess.run(
        [sys.executable, "-c", _REAL_PTB_SCRIPT], capture_output=True,
        text=True, timeout=60, env={**os.environ, "CASA_TZ": "Europe/Rome"})
    assert proc.returncode == 0 and proc.stdout.strip() == "OK", proc.stderr


def test_a_malformed_time_link_keeps_its_label_as_plain_text():
    display, ents = render("**Due** [soon](time:next week)")
    assert display == "Due soon" and [e.type for e in ents] == ["bold"]


@pytest.mark.parametrize("target", ["time:garbage", "time:", "time:2026-13-40"])
def test_garbage_targets_keep_the_label_and_the_rest_of_the_line(target):
    display, ents = render(f"[Mon]({target}) and `code`")
    assert display == "Mon and code"
    assert [e.type for e in ents] == ["code"]


def test_a_time_without_offset_is_read_in_casas_timezone():
    assert int(render("[9](time:2026-11-16T09:00)")[1][0].unix_time.timestamp()) \
        == MON_0900_ROME


def test_a_z_suffix_and_a_date_only_target_parse():
    assert time_instant("time:2026-11-16T08:00Z") == MON_0900_ROME
    assert time_instant("time:2026-11-16") == MON_MIDNIGHT_ROME
    assert _times(render("[16 November](time:2026-11-16)")[1]) == [
        (0, 11, MON_MIDNIGHT_ROME)]
    assert time_instant("time:next week") is None
    assert time_instant("time:0001-01-01T00:00+14:00") is None   # before 1970


def test_a_spaced_iso_target_is_read_like_a_t_separated_one():
    assert _times(render("[Mon](time:2026-11-16 09:00+01:00)")[1]) == [
        (0, 3, MON_0900_ROME)]


def test_a_time_link_inside_bold_splits_the_bold_around_it():
    display, ents = render("**Due [Mon](time:2026-11-16T09:00+01:00) now**")
    assert display == "Due Mon now"
    got = sorted((e.type, e.offset, e.length) for e in ents)
    assert got == [("bold", 0, 4), ("bold", 7, 4), ("date_time", 4, 3)]


def test_a_time_link_in_a_heading_is_not_inside_its_bold():
    display, ents = render("## Next [Mon](time:2026-11-16)")
    assert display == "Next Mon"
    got = sorted((e.type, e.offset, e.length) for e in ents)
    assert got == [("bold", 0, 5), ("date_time", 5, 3)]


def test_a_time_link_in_a_table_renders_the_table_as_a_stanza():
    src = "| Item | When |\n|---|---|\n| Call | [Mon](time:2026-11-16T09:00+01:00) |"
    display, ents = render(src)
    assert display == "Item: Call\nWhen: Mon"
    assert _times(ents) == [(display.index("Mon"), 3, MON_0900_ROME)]
    assert all(e.type != MessageEntity.PRE for e in ents)


def test_a_page_cut_through_a_time_link_keeps_an_entity_on_each_page():
    src = "x" * 4090 + "[" + "M" * 20 + "](time:2026-11-16T09:00+01:00)"
    pages = render_paged(src)
    assert len(pages) == 2
    (first, e1), (second, e2) = pages
    assert first.endswith("M" * 6) and second == "M" * 14
    assert _times(e1) == [(4090, 6, MON_0900_ROME)]
    assert _times(e2) == [(0, 14, MON_0900_ROME)]


def test_other_schemes_still_leave_the_line_literal():
    assert render("[x](mailto:a@b.c) **b**")[1] is None


def test_an_empty_time_label_leaves_the_line_literal():
    src = "[](time:2026-11-16) **b**"
    assert render(src) == (src, None)


def test_the_plain_fallback_shows_the_label_only():
    assert plain_with_link_targets(*render("[Mon](time:2026-11-16)")) == "Mon"


def test_the_guide_is_the_fixed_copy():
    assert TIME_LINK_GUIDE == (
        "time_links: in the messages the person reads in this chat (answers, questions, "
        "progress notes, summaries) write every date or time as a Markdown link whose target "
        "is time: and the moment in ISO 8601 with its UTC offset, keeping your own words as "
        "the label, for example [Monday 16 November at 09:00](time:2026-11-16T09:00+01:00) or "
        "[16 November](time:2026-11-16). Telegram makes it tappable. Everywhere else (files, "
        "emails, tool fields, records) write dates plainly.")


def test_every_writer_of_telegram_text_is_taught_time_links():
    assert TIME_LINK_GUIDE in agent._channel_context_block("telegram")
    assert TIME_LINK_GUIDE not in agent._channel_context_block("voice")
    assert TIME_LINK_GUIDE in ANSWER_SHOWN_LINE
    assert TIME_LINK_GUIDE in background_jobs._job_rules()
    assert TIME_LINK_GUIDE in background_jobs._job_rules(can_ask=False)


@pytest.mark.parametrize("src", [
    "see ](time: 2 * 3 * 4) ok",
    "x ](time: ** y ** z) w",
])
def test_asterisks_near_a_time_target_stay_literal(src):
    assert render(src) == (src, None)


def test_asterisks_after_a_real_link_and_a_time_target_stay_literal():
    display, ents = render("[a](https://e.com/x) ](time: * b *)")
    assert display == "a ](time: * b *)"
    assert [e.type for e in ents] == ["text_link"]


def test_a_time_link_with_a_title_still_becomes_an_entity():
    display, ents = render('[Mon](time:2026-11-16T09:00+01:00 "title")')
    assert display == "Mon"
    assert _times(ents) == [(0, 3, MON_0900_ROME)]
