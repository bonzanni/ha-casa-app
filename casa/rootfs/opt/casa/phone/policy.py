"""Which class a caller is in, and how far that class may be put through.

Mechanical facts only: the lookup's two flags pick the class, and the grant
for that class is read from the effective settings.
"""
from __future__ import annotations

from typing import Literal

from contacts.lookup import CallerInfo
from phone.settings import Effective

CallerClass = Literal["priority", "known", "unknown"]


def caller_class(info: CallerInfo) -> CallerClass:
    if not info.known:
        return "unknown"
    return "priority" if info.priority else "known"


def transfer_level(effective: Effective, caller_class: str, *, returned: bool,
                   transfer_number: str) -> str:
    """The transfer grant for this call: ``off`` for a call that comes back
    from a transfer (it is never put through a second time) and when there is
    no number to put anyone through to."""
    if returned or not transfer_number:
        return "off"
    return effective.grants.get((caller_class, "transfer"), "off")
