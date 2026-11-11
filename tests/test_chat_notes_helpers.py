"""The composer a notice and its chat note share (``chat_notes.clip`` and
``chat_notes.bounded_line``), and the lane predicate ``agent_lane.has_lane``."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import agent_lane
import chat_notes
import result_broker as rb

LABEL_MAX = "📊 " + "L" * 62                 # a 64-character display name after the glyph
NAME_200 = "n" * 196 + ".pdf"
TITLE_60 = "t" * 60


# -- clip ------------------------------------------------------------------------------

def test_clip_keeps_text_within_the_limit_untouched():
    assert chat_notes.clip("short", 10) == "short"
    assert chat_notes.clip("exactly10!", 10) == "exactly10!"
    assert chat_notes.clip("", 5) == "" and chat_notes.clip(None, 5) == ""   # type: ignore[arg-type]


def test_clip_cuts_with_a_visible_marker_within_the_limit():
    out = chat_notes.clip("x" * 50, 20)
    assert len(out) == 20 and out.endswith(chat_notes.CLIP) and out.startswith("x" * 17)
    assert chat_notes.clip("abcdef", 2) == chat_notes.CLIP     # no room left but the marker


# -- bounded_line ------------------------------------------------------------------------

def test_the_literal_text_survives_at_maximum_inputs_and_the_line_fits_the_echo_cap():
    line = chat_notes.bounded_line("{label} could not take {name}: inbox full (50 files / 200 MB).",
                                   label=LABEL_MAX, fields={"name": NAME_200})
    assert len(line) <= rb.ECHO_LINE_MAX
    assert line.endswith(": inbox full (50 files / 200 MB).")       # the outcome clause intact
    assert " could not take " in line                               # the connective intact
    assert chat_notes.clip(line, rb.ECHO_LINE_MAX) == line          # a note keeps it whole


def test_label_and_fields_each_keep_at_least_eight_characters_with_an_ellipsis():
    line = chat_notes.bounded_line(
        "{label} received your file {name}; its lane was full when the place was requested.",
        label=LABEL_MAX, fields={"name": NAME_200})
    head = line.partition(" received your file ")[0]
    name = line.split(" received your file ", 1)[1].split(";", 1)[0]
    assert len(head) >= chat_notes.FIELD_MIN_CHARS and head.endswith(chat_notes.CLIP)
    assert len(name) >= chat_notes.FIELD_MIN_CHARS and name.endswith(chat_notes.CLIP)
    assert len(line) <= rb.ECHO_LINE_MAX


def test_short_inputs_are_not_clipped_at_all():
    line = chat_notes.bounded_line(
        "{label} stored {name} in its inbox but was not delegable at the post-download check.",
        label="📊 Finance", fields={"name": "invoice.pdf"})
    assert line == ("📊 Finance stored invoice.pdf in its inbox but was not delegable at the "
                    "post-download check.")


def test_a_line_with_a_title_field_keeps_its_reason_at_maximum_inputs():
    line = chat_notes.bounded_line(
        '{label}: "{title}" did not start — another job of this plugin held the claim at this occurrence.',
        label=LABEL_MAX, fields={"title": TITLE_60})
    assert len(line) <= rb.ECHO_LINE_MAX
    assert line.endswith(" did not start — another job of this plugin held the claim at this occurrence.")


def test_a_wider_limit_is_honoured_and_a_line_fits_one_note():
    line = chat_notes.bounded_line("{label} said {words}", label=LABEL_MAX,
                                   fields={"words": "w" * 1000}, limit=chat_notes.LINE_MAX)
    assert len(line) == chat_notes.LINE_MAX and line.endswith(chat_notes.CLIP)
    chat_notes.reset()
    chat_notes.record(111, 701, line)
    assert chat_notes.drain(111, 701) == [line]                # recorded whole
    chat_notes.reset()


def test_a_missing_field_value_is_empty_not_none():
    assert chat_notes.bounded_line("{label}: {x}", label="L", fields={"x": None}) == "L: "


# -- has_lane ------------------------------------------------------------------------------

@pytest.mark.parametrize("kind, expected", [("specialist", True), ("executor", True),
                                            ("resident", False), ("", False)])
def test_a_specialist_or_the_configurator_has_a_lane_a_resident_none(kind, expected):
    assert agent_lane.has_lane(SimpleNamespace(kind=kind)) is expected
    assert agent_lane.has_lane(object()) is False


def test_a_task_answer_is_an_answer_kind():
    assert rb.TASK_ANSWER == "task_answer"
    assert rb.ANSWER_KINDS == frozenset({rb.TASK_ANSWER})
