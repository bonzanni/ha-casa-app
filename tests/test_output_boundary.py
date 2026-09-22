"""output_boundary (#1038): the one place a per-turn output property is applied.

Every test here runs the real module against literal expectations. The wording of
the disclosure is part of the contract the operator sees, so it is asserted verbatim.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import output_boundary as ob
from output_boundary import IntentKind as K

pytestmark = [pytest.mark.unit]

INVOICE = ("/data/agent-inbox/assistant/ready/1758500000000-a1b2.pdf", "invoice.pdf")
STATEMENT = ("/data/agent-inbox/assistant/ready/1758500001000-c3d4.pdf", "statement.pdf")

ANSWERED = "Casa: Ellen answered without opening “invoice.pdf” in this turn."
WROTE = "Casa: Ellen wrote this without opening “invoice.pdf”."


def _scope(**over) -> ob.TurnScope:
    fields = dict(id="turn-1", cid="c1", role="assistant", display_name="Ellen",
                  channel="telegram", message_type="channel_in", markers={})
    fields.update(over)
    return ob.TurnScope(**fields)


# ---------------------------------------------------------------------------
# Admission with nothing owed
# ---------------------------------------------------------------------------

def test_a_scope_with_no_obligation_admits_text_unchanged():
    s = _scope()
    a = s.admit(K.FINAL_REPLY, "It's €412, due Friday.")
    assert a.text == "It's €412, due Friday."
    assert a.annotations == ()
    assert a.scope_id == "turn-1"
    assert a.source == "model"


def test_casa_text_is_admitted_without_a_scope_and_without_annotation():
    a = ob.casa_text("Rate limited — try again in a minute.")
    assert a.text == "Rate limited — try again in a minute."
    assert a.source == "casa"
    assert a.annotations == ()


# ---------------------------------------------------------------------------
# ReadBeforeDescribe — the #1036 disclosure
# ---------------------------------------------------------------------------

def test_a_listing_without_a_read_prefixes_the_disclosure_on_a_final_reply():
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    a = s.admit(K.FINAL_REPLY, "It's €412, due Friday.")
    assert a.text == ANSWERED + "\n\nIt's €412, due Friday."
    assert a.annotations == (ANSWERED,)


def test_a_successful_read_of_a_listed_file_discharges_the_obligation():
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    s.note_read_ok(INVOICE[0])
    a = s.admit(K.FINAL_REPLY, "It's €412, due Friday.")
    assert a.text == "It's €412, due Friday."
    assert a.annotations == ()


def test_reading_any_one_listed_file_discharges_it_for_all():
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE, STATEMENT)))
    s.note_read_ok(STATEMENT[0])
    assert s.admit(K.FINAL_REPLY, "Both look fine.").annotations == ()


def test_text_committed_before_the_read_keeps_its_line_and_text_after_does_not():
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    early = s.admit(K.DISCRETE, "Reminder: the invoice is €412")
    s.note_read_ok(INVOICE[0])
    late = s.admit(K.FINAL_REPLY, "Confirmed: €412.")
    assert early.text == ANSWERED + "\n\nReminder: the invoice is €412"
    assert late.text == "Confirmed: €412."


def test_a_failed_read_is_not_evidence():
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    s.note_read_failed(INVOICE[0])
    assert s.admit(K.FINAL_REPLY, "It's €412.").annotations == (ANSWERED,)


def test_a_read_of_a_file_that_was_not_listed_is_not_evidence():
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    s.note_read_ok("/data/agent-inbox/assistant/ready/other.pdf")
    assert s.admit(K.FINAL_REPLY, "It's €412.").annotations == (ANSWERED,)


def test_a_read_path_is_normalised_before_it_is_matched():
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    s.note_read_ok("/data/agent-inbox/assistant/ready/../ready//1758500000000-a1b2.pdf")
    assert s.admit(K.FINAL_REPLY, "It's €412.").annotations == ()


def test_empty_or_whitespace_text_is_never_annotated():
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    assert s.admit(K.FINAL_REPLY, "").text == ""
    assert s.admit(K.STREAM_UPDATE, "  \n").text == "  \n"


def test_several_listed_files_none_read_names_the_count_not_the_files():
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE, STATEMENT)))
    a = s.admit(K.FINAL_REPLY, "Both are fine.")
    assert a.text == ("Casa: Ellen answered without opening any of the 2 files you "
                      "sent in this turn.\n\nBoth are fine.")


def test_a_read_attempt_on_an_unlisted_inbox_file_arms_the_obligation():
    s = _scope()
    s.note_read_attempt(INVOICE[0], display_name="invoice.pdf")
    s.note_read_failed(INVOICE[0])
    assert s.admit(K.FINAL_REPLY, "It's €412.").annotations == (ANSWERED,)


def test_a_stream_update_is_annotated_while_undischarged_and_clean_after_a_read():
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    assert s.admit(K.STREAM_UPDATE, "It's").text == ANSWERED + "\n\nIt's"
    s.note_read_ok(INVOICE[0])
    assert s.admit(K.STREAM_UPDATE, "It's €412").text == "It's €412"


# ---------------------------------------------------------------------------
# Stored payloads: the note travels, the text does not change
# ---------------------------------------------------------------------------

def test_a_stored_payload_keeps_its_text_and_carries_a_note():
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    a = s.admit(K.STORED, "Pay the €412 invoice")
    assert a.text == "Pay the €412 invoice"
    assert a.note == WROTE
    assert a.annotations == (WROTE,)


def test_a_stored_payload_after_the_read_carries_no_note():
    s = _scope()
    s.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    s.note_read_ok(INVOICE[0])
    a = s.admit(K.STORED, "Pay the €412 invoice")
    assert a.note == ""
    assert a.annotations == ()


# ---------------------------------------------------------------------------
# InheritedNote: what a later turn owes for a payload it did not author
# ---------------------------------------------------------------------------

def test_an_inherited_note_is_prepended_to_every_emission():
    s = _scope(id="turn-2")
    s.arm(ob.InheritedNote(WROTE))
    assert s.admit(K.DISCRETE, "Pay the €412 invoice").text == (
        WROTE + "\n\nPay the €412 invoice")
    assert s.admit(K.FINAL_REPLY, "Sent.").text == WROTE + "\n\nSent."


def test_an_inherited_note_is_never_discharged_by_a_read():
    s = _scope(id="turn-2")
    s.arm(ob.InheritedNote(WROTE))
    s.note_read_ok(INVOICE[0])
    assert s.admit(K.DISCRETE, "Pay the €412 invoice").annotations == (WROTE,)


def test_inherited_and_todays_lines_both_appear_and_a_stored_note_concatenates():
    s = _scope(id="turn-2")
    s.arm(ob.InheritedNote(WROTE))
    s.arm(ob.ReadBeforeDescribe(files=(STATEMENT,)))
    today = "Casa: Ellen answered without opening “statement.pdf” in this turn."
    a = s.admit(K.DISCRETE, "Both are due.")
    assert a.text == WROTE + "\n\n" + today + "\n\nBoth are due."
    stored = s.admit(K.STORED, "Pay both")
    assert stored.note == (WROTE + "\n\n"
                           "Casa: Ellen wrote this without opening “statement.pdf”.")


# ---------------------------------------------------------------------------
# Minting: from the bus message, for a child, for an engagement
# ---------------------------------------------------------------------------

def _config(role="assistant", name="Ellen"):
    return SimpleNamespace(role=role, character=SimpleNamespace(name=name))


def test_mint_reads_identity_and_reserved_markers_off_the_message():
    from bus import BusMessage, MessageType
    msg = BusMessage(type=MessageType.CHANNEL_IN, source="telegram", target="assistant",
                     content="hi", channel="telegram",
                     context={"chat_id": 1, "cid": "c9", "_origin_route": "telegram",
                              "user_name": "nicola"})
    s = ob.TurnScope.mint(msg, _config())
    assert s.id == msg.id
    assert s.cid == "c9"
    assert s.role == "assistant"
    assert s.display_name == "Ellen"
    assert s.channel == "telegram"
    assert s.markers == {"_origin_route": "telegram"}
    assert s.obligations == []


def test_mint_registers_an_inherited_note_from_the_reserved_marker():
    from bus import BusMessage, MessageType
    msg = BusMessage(type=MessageType.SCHEDULED, source="scheduler", target="assistant",
                     content="Send this exact message…", channel="telegram",
                     context={"chat_id": "date-reminder-1", "cid": "c2",
                              "_inherited_note": WROTE})
    s = ob.TurnScope.mint(msg, _config())
    assert s.admit(K.DISCRETE, "Pay the €412 invoice").annotations == (WROTE,)


def test_a_child_view_carries_the_launch_note_not_the_parents_live_obligation():
    parent = _scope()
    parent.arm(ob.ReadBeforeDescribe(files=(INVOICE,)))
    note = parent.admit(K.STORED, "Check the €412 invoice").note
    child = ob.TurnScope.for_child(parent, note)
    parent.note_read_ok(INVOICE[0])          # the parent reads AFTER launching
    assert parent.admit(K.FINAL_REPLY, "Done.").annotations == ()
    assert child.admit(K.CAPTION, "invoice.pdf").text == WROTE + "\n\ninvoice.pdf"
    assert child.id == parent.id
    assert child.markers == parent.markers


def test_a_child_of_a_parent_that_owed_nothing_owes_nothing():
    parent = _scope()
    child = ob.TurnScope.for_child(parent, "")
    assert child.admit(K.CAPTION, "photo").annotations == ()


def test_an_engagement_scope_carries_only_the_persisted_note():
    eng = SimpleNamespace(id="eng-7", origin={"role": "assistant", "channel": "telegram",
                                              "_inherited_note": WROTE,
                                              "chat_id": 1})
    s = ob.TurnScope.for_engagement(eng, display_name="Ellen")
    assert s.id == "eng-7"
    assert s.admit(K.CAPTION, "the report").text == WROTE + "\n\nthe report"
    s2 = ob.TurnScope.for_engagement(SimpleNamespace(id="eng-8", origin={}), display_name="Ellen")
    assert s2.admit(K.CAPTION, "the report").annotations == ()
