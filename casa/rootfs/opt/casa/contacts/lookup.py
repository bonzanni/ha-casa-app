"""The caller lookup: a number in, five fields out.

Deliberately narrow: whoever calls this learns whether the number is a known
person, whether that person is a priority, their name and first name, and the
language to answer in, and nothing else.

Blocking (one read transaction); async callers use ``asyncio.to_thread``.
Never raises.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from contacts import numbers as phone_numbers
from contacts import store as cstore
from contacts.store import ContactsStore, ContactsUnavailable

logger = logging.getLogger(__name__)

DEFAULT_LANGUAGE = "en"
LANGUAGE_BY_COUNTRY_CODE = {31: "nl", 39: "it"}


@dataclass(frozen=True)
class CallerInfo:
    known: bool
    priority: bool
    name: str
    first_name: str
    language: str


def _language_of(e164: str | None) -> str:
    if e164 is None:
        return DEFAULT_LANGUAGE
    code = phone_numbers.country_code(e164)
    return LANGUAGE_BY_COUNTRY_CODE.get(code, DEFAULT_LANGUAGE)


def lookup(store: ContactsStore, raw_number: str, *,
           default_region: str) -> CallerInfo:
    """Who is calling from *raw_number*. Never raises: whatever the input or
    the state of the store, the worst answer is an unknown caller."""
    try:
        e164 = phone_numbers.to_e164(raw_number, default_region)
    except Exception:  # noqa: BLE001 — a lookup never fails the call
        e164 = None
    fallback = _language_of(e164)
    unknown = CallerInfo(False, False, "", "", fallback)
    if e164 is None:
        return unknown
    try:
        with store.transaction() as conn:
            people = cstore.by_number(conn, e164)
            if not people:
                return unknown
            person = people[0]
            marks = cstore.marks_for(conn, person.source, person.uid)
    except (ContactsUnavailable, sqlite3.Error):
        return unknown
    except Exception:  # noqa: BLE001 — a lookup never fails the call
        logger.exception("contacts lookup failed")
        return unknown
    return CallerInfo(True, marks.priority, person.name, person.first_name,
                      marks.language or fallback)
