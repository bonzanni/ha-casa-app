"""S6 §2.6 — one bounded composer for every line the slice introduces or changes, and
`file_outcome` over the real inbox outcome enum (INV-FILE-001's wording)."""
from __future__ import annotations

import pytest

import agent_inbox
import result_broker as rb
import specialist_desk as sd

LABEL_MAX = "📊 " + "L" * 62                 # a 64-character display name after the glyph
NAME_200 = "n" * 196 + ".pdf"
TITLE_60 = "t" * 60


def test_the_literal_text_survives_at_maximum_inputs_and_the_line_fits_the_echo_cap():
    line = sd.bounded_line("{label} could not take {name}: inbox full (50 files / 200 MB).",
                           label=LABEL_MAX, fields={"name": NAME_200})
    assert len(line) <= rb.ECHO_LINE_MAX
    assert line.endswith(": inbox full (50 files / 200 MB).")       # the outcome clause intact
    assert " could not take " in line                               # the connective intact
    assert sd.clip(line, rb.ECHO_LINE_MAX) == line                  # record_echo would keep it whole


def test_label_and_fields_each_keep_at_least_eight_characters_with_an_ellipsis():
    line = sd.bounded_line("{label} received your file {name}; its desk was full when the place was requested.",
                           label=LABEL_MAX, fields={"name": NAME_200})
    head, _, _tail = line.partition(" received your file ")
    name = line.split(" received your file ", 1)[1].split(";", 1)[0]
    assert len(head) >= 8 and head.endswith(sd.CLIP)
    assert len(name) >= 8 and name.endswith(sd.CLIP)
    assert len(line) <= rb.ECHO_LINE_MAX


def test_short_inputs_are_not_clipped_at_all():
    line = sd.bounded_line("{label} stored {name} in its inbox but was not delegable at the post-download check.",
                           label="📊 Finance", fields={"name": "invoice.pdf"})
    assert line == "📊 Finance stored invoice.pdf in its inbox but was not delegable at the post-download check."


def test_a_line_with_a_title_field_keeps_its_reason_at_maximum_inputs():
    line = sd.bounded_line('{label}: "{title}" did not start — another job of this plugin held the claim at this occurrence.',
                           label=LABEL_MAX, fields={"title": TITLE_60})
    assert len(line) <= rb.ECHO_LINE_MAX
    assert line.endswith(" did not start — another job of this plugin held the claim at this occurrence.")


@pytest.mark.parametrize("outcome, expected_tail", [
    (agent_inbox.Outcome.TOO_LARGE, ": over 8 MB."),
    (agent_inbox.Outcome.MISMATCH, ": not a PDF."),
    (agent_inbox.Outcome.FULL, ": inbox full (50 files / 200 MB)."),
    (agent_inbox.Outcome.DOWNLOAD_FAILED, ": Telegram did not hand it over."),
    (agent_inbox.Outcome.LOCAL_PATH, ": could not be saved; nothing kept."),
    (agent_inbox.Outcome.STORAGE_FAILED, ": could not be saved; nothing kept."),
])
def test_file_outcome_covers_the_real_enum_as_past_events(outcome, expected_tail):
    line = sd.file_outcome("📊 Finance", "invoice.pdf", outcome, kind="PDF")
    assert line.startswith("📊 Finance could not take invoice.pdf")
    assert line.endswith(expected_tail)


def test_an_uncertain_save_claims_neither_kept_nor_lost():
    line = sd.file_outcome("📊 Finance", "invoice.pdf", agent_inbox.Outcome.UNCERTAIN, kind="PDF")
    assert line == "📊 Finance could not confirm that invoice.pdf was saved — it may or may not be in its inbox."
    assert "could not take" not in line and "nothing kept" not in line


def test_a_stored_receipt_is_not_a_refusal_line():
    assert sd.file_outcome("📊 Finance", "invoice.pdf", agent_inbox.Outcome.STORED, kind="PDF") is None


def test_every_file_outcome_fits_at_maximum_inputs_with_its_clause_intact():
    for outcome in agent_inbox.Outcome:
        line = sd.file_outcome(LABEL_MAX, NAME_200, outcome, kind="PDF")
        if line is None:
            continue
        assert len(line) <= rb.ECHO_LINE_MAX
        assert line.endswith((".", "inbox.")), line
        assert ("could not take " in line) or ("could not confirm" in line)
