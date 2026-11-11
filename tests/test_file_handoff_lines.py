"""S6 §2.6 — one bounded composer (``chat_notes.bounded_line``) for Casa's labelled lines
in the chat: the literal text survives, every label and field keeps a readable head, and
the line fits the note cap."""
from __future__ import annotations

import chat_notes as cn
import result_broker as rb

LABEL_MAX = "📊 " + "L" * 62                 # a 64-character display name after the glyph
NAME_200 = "n" * 196 + ".pdf"
TITLE_60 = "t" * 60


def test_the_literal_text_survives_at_maximum_inputs_and_the_line_fits_the_echo_cap():
    line = cn.bounded_line("{label} could not take {name}: inbox full (50 files / 200 MB).",
                           label=LABEL_MAX, fields={"name": NAME_200})
    assert len(line) <= rb.ECHO_LINE_MAX
    assert line.endswith(": inbox full (50 files / 200 MB).")       # the outcome clause intact
    assert " could not take " in line                               # the connective intact
    assert cn.clip(line, rb.ECHO_LINE_MAX) == line                  # kept whole


def test_label_and_fields_each_keep_at_least_eight_characters_with_an_ellipsis():
    line = cn.bounded_line("{label} received your file {name}; the buttons could not be cleared.",
                           label=LABEL_MAX, fields={"name": NAME_200})
    head, _, _tail = line.partition(" received your file ")
    name = line.split(" received your file ", 1)[1].split(";", 1)[0]
    assert len(head) >= 8 and head.endswith(cn.CLIP)
    assert len(name) >= 8 and name.endswith(cn.CLIP)
    assert len(line) <= rb.ECHO_LINE_MAX


def test_short_inputs_are_not_clipped_at_all():
    line = cn.bounded_line("{label} ☑ {button} — the buttons could not be cleared.",
                           label="📊 Finance", fields={"button": "📎 Add a document"})
    assert line == "📊 Finance ☑ 📎 Add a document — the buttons could not be cleared."


def test_a_line_with_a_title_field_keeps_its_reason_at_maximum_inputs():
    line = cn.bounded_line('{label}: "{title}" did not start — another job of this plugin held the claim at this occurrence.',
                           label=LABEL_MAX, fields={"title": TITLE_60})
    assert len(line) <= rb.ECHO_LINE_MAX
    assert line.endswith(" did not start — another job of this plugin held the claim at this occurrence.")
