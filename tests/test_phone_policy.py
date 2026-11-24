"""The caller class and the transfer level a call gets."""
from __future__ import annotations

import pytest

from contacts.lookup import CallerInfo
from phone import policy
from phone.settings import Effective

GRANTS = {("priority", "transfer"): "open", ("known", "transfer"): "urgent",
          ("unknown", "transfer"): "emergency"}
EFFECTIVE = Effective(models={}, voices={}, grants=GRANTS)
NUMBER = "+31612345678"


@pytest.mark.parametrize("known, priority, expected", [
    (True, True, "priority"),
    (True, False, "known"),
    (False, False, "unknown"),
    # A priority mark without a known person cannot happen in a lookup; the
    # class follows what is known.
    (False, True, "unknown"),
])
def test_caller_class(known, priority, expected):
    info = CallerInfo(known, priority, "Ann Example" if known else "",
                      "Ann" if known else "", "en")
    assert policy.caller_class(info) == expected


@pytest.mark.parametrize("caller_class", ["priority", "known", "unknown"])
def test_transfer_level_is_the_grant(caller_class):
    assert policy.transfer_level(
        EFFECTIVE, caller_class, returned=False,
        transfer_number=NUMBER) == GRANTS[(caller_class, "transfer")]


def test_a_returned_call_is_never_put_through():
    assert policy.transfer_level(EFFECTIVE, "priority", returned=True,
                                 transfer_number=NUMBER) == "off"


def test_no_transfer_number_means_nobody_is_put_through():
    assert policy.transfer_level(EFFECTIVE, "priority", returned=False,
                                 transfer_number="") == "off"


def test_a_missing_grant_is_off():
    empty = Effective(models={}, voices={}, grants={})
    assert policy.transfer_level(empty, "known", returned=False,
                                 transfer_number=NUMBER) == "off"
