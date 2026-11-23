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
    assert vcard.count_blocks(text) == len(cards) == 4
