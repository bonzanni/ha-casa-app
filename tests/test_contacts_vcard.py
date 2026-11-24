from pathlib import Path

from contacts import vcard

FIX = Path(__file__).parent / "fixtures" / "contacts"


def test_person_card():
    (c,) = vcard.parse_cards((FIX / "apple_person.vcf").read_text())
    assert (c.name, c.first_name, c.org) == ("Marco Rossi", "Marco", "Acme")
    assert c.tels == (("+39 333 1234567", "CELL"), ("02 1234 5678", "WORK"))
    assert c.note == "Old friend, speaks Italian.\nMet in Milan, 2010"
    assert c.related == ("Anna Rossi",)
    assert c.is_group is False


def test_group_card():
    (g,) = vcard.parse_cards((FIX / "apple_group.vcf").read_text())
    assert g.is_group and g.name == "Priority"
    assert g.members == ("AAAA-1111", "BBBB-2222")


def test_folded_lines_and_odd_blocks_are_kept():
    text = (FIX / "folded.vcf").read_text()
    cards = vcard.parse_cards(text)
    assert [c.uid for c in cards] == ["U1", "U2", "", "U4"]
    assert cards[0].note.endswith("continued on the next line")
    assert cards[3].name == "Acme Plumbing"
    assert vcard.well_formed(text) and len(cards) == 4


ONE = "BEGIN:VCARD\r\nUID:U1\r\nFN:Zoe Example\r\nEND:VCARD\r\n"


def test_well_formed_single_and_multi():
    assert vcard.well_formed(ONE)
    assert vcard.well_formed(ONE + ONE.replace("U1", "U2"))
    assert vcard.well_formed((FIX / "apple_person.vcf").read_text())


def test_well_formed_refuses_unterminated_nested_stray_end_and_empty():
    assert not vcard.well_formed("BEGIN:VCARD\r\nUID:U1\r\nFN:Zoe Example\r\n")
    assert not vcard.well_formed(
        "BEGIN:VCARD\r\nUID:U1\r\n" + ONE)
    assert not vcard.well_formed("UID:U1\r\nEND:VCARD\r\n")
    assert not vcard.well_formed(ONE + "END:VCARD\r\n")
    assert not vcard.well_formed("")
    assert not vcard.well_formed("garbage")
