"""Phone numbers as people write them, reduced to E.164 for matching."""

from __future__ import annotations

import phonenumbers


def to_e164(raw: str, default_region: str) -> str | None:
    """Return ``raw`` as E.164, or None when it cannot be read as a number.

    A number without ``+`` or ``00`` needs ``default_region`` (an ISO country
    code); with an empty region such numbers are skipped.
    """
    if not raw or not raw.strip():
        return None
    try:
        parsed = phonenumbers.parse(raw, default_region or None)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def country_code(e164: str) -> int | None:
    """The calling code of an E.164 number, or None when it does not parse."""
    try:
        return phonenumbers.parse(e164, None).country_code
    except phonenumbers.NumberParseException:
        return None
