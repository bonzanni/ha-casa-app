import pytest
from contacts import numbers


@pytest.mark.parametrize("raw,region,want", [
    ("+31 6 1234 5678", "", "+31612345678"),
    ("06 12345678", "NL", "+31612345678"),
    ("0031612345678", "NL", "+31612345678"),
    ("+31 (0)6 1234 5678", "NL", "+31612345678"),
    ("+39 02 1234 5678", "", "+390212345678"),
    ("06 12345678", "", None),          # national format, no region: skipped
    ("not a number", "NL", None),
    ("", "NL", None),
])
def test_to_e164(raw, region, want):
    assert numbers.to_e164(raw, region) == want


def test_country_code():
    assert numbers.country_code("+31612345678") == 31
    assert numbers.country_code("garbage") is None
