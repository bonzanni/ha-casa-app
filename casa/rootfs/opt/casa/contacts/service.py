"""The running contacts service: the store Casa opened at boot, the iCloud
login, one shared refresh, and the daily job.

The iCloud account (the Apple ID and its app-specific password) is the login
:data:`LOGIN` of :mod:`logins`: the vault item chosen for it, or else the one
under its default title. Both values are read from the one item, in one go.
Without them a refresh does nothing, and where a login never worked (no
successful sync on record, no item chosen) it stays silent. Where one did
work, a refresh that finds none is a failed refresh: the login was lost.

A refresh never raises: an unavailable store, a lost login or an unexpected
error is a failed :class:`contacts.sync.Result`. Concurrent callers share the
refresh already running. The daily job tells the operator about a failure
once per failure streak: the store records the streak of a sync that ran, and
a failure before a sync could run (an unavailable store, a lost login, an
unexpected error) keeps its streak here in memory, since the store may be
what is broken. A lost login's line says what is wrong with it
(:meth:`logins.Check.sentence`). Delivery is tracked apart from the streak: a
notice that did not reach the operator stays pending and is sent again by the
next daily run, until it is delivered or a refresh succeeds.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable

from contacts import notes, sync
from contacts import store as cstore
from contacts.carddav import ICloudSource
from contacts.store import ContactsStore
import logins
from output_boundary import casa_text

logger = logging.getLogger(__name__)

#: The login of :mod:`logins` that holds the iCloud account.
LOGIN = "icloud_contacts"
SOURCE = ICloudSource.name

FAILED_LINE = "Contacts could not be refreshed from iCloud: {error}."
SERVICE_FAILED_LINE = "Contacts could not be refreshed: {error}."
KEPT_LINE = "The last good copy is still in use."
NONE_STORED_LINE = "No contacts are stored yet."
UNAVAILABLE_ERROR = "the contacts store is unavailable"
UNEXPECTED_ERROR = "the refresh stopped on an unexpected error"
#: A login that worked before can no longer be read.
LOGIN_ERROR = "the iCloud login could not be read"
_SERVICE_ERRORS = (UNAVAILABLE_ERROR, UNEXPECTED_ERROR, LOGIN_ERROR)


def _read_login() -> dict[str, str] | None:
    """The Apple ID (``username``) and its password (``password``), both
    from the one vault item of :data:`LOGIN`; None unless both are there.
    Blocking."""
    return logins.read_all(LOGIN)


_store: ContactsStore | None = None
_notify: Callable[[str], Awaitable[bool]] | None = None
_model: notes.Model | None = None
_read: Callable[[], dict[str, str] | None] | None = None
#: What is wrong with a login that worked before, as the last refresh found
#: it; None while the login can be read.
_login_check: logins.Check | None = None
_running: asyncio.Future | None = None
#: True from a failure before a sync could run until the next successful
#: sync: the operator has been told about this streak.
_service_failing = False
#: The failure notice not yet delivered to the operator, if any.
_pending: str | None = None


def configure(*, store: ContactsStore,
              notify: Callable[[str], Awaitable[bool]] | None,
              model: notes.Model | None,
              read_login: Callable[[], dict[str, str] | None] = _read_login,
              ) -> None:
    global _store, _notify, _model, _read, _running, _service_failing
    global _pending, _login_check
    _store, _notify, _model, _read = store, notify, model, read_login
    _running = None
    _service_failing = False
    _pending = None
    _login_check = None


def active_store() -> ContactsStore | None:
    """The configured store while it is available; otherwise None."""
    store = _store
    return store if store is not None and store.available else None


def default_region() -> str:
    """The country assumed for numbers written without ``+``; ``""`` when
    the option is not set (such numbers are skipped)."""
    return os.environ.get("CONTACTS_DEFAULT_REGION", "").strip().upper()


def mirror_empty() -> bool:
    """True when the store is available and holds nobody from iCloud."""
    store = active_store()
    if store is None:
        return False
    with store.transaction() as conn:
        return cstore.count(conn, SOURCE) == 0


def login_problem() -> logins.Check | None:
    """What the last refresh found wrong with a login that worked before;
    None while the login can be read or never worked."""
    return _login_check


def _was_set_up() -> bool:
    """True when the login worked before or was meant to: the source has a
    successful sync on record, or an item was chosen for it. Blocking."""
    if logins.chosen(LOGIN) is not None:
        return True
    store = active_store()
    if store is None:
        return False
    with store.transaction() as conn:
        return cstore.sync_state(conn, SOURCE)["last_ok"] is not None


def _failed(error: str) -> sync.Result:
    """A failure before a sync could run; the operator is to be told only
    about the first of a streak."""
    global _service_failing
    tell = not _service_failing
    _service_failing = True
    return sync.Result(ok=False, added=0, removed=0, kept=0,
                       skipped_numbers=0, note_changed=(), error=error,
                       tell_operator=tell)


def _copy_line() -> str:
    """What the operator still has: the last good copy, or no contacts at
    all; ``""`` when the store cannot say. Blocking."""
    store = active_store()
    if store is None:
        return ""
    try:
        with store.transaction() as conn:
            held = cstore.count(conn, SOURCE)
    except Exception:  # noqa: BLE001 — the line is still sent without it
        logger.warning("contacts: the stored copy could not be counted",
                       exc_info=True)
        return ""
    return KEPT_LINE if held else NONE_STORED_LINE


async def _refresh(reason: str) -> sync.Result | None:
    global _service_failing, _pending, _login_check
    try:
        read = _read
        if read is None:
            return None
        login = await asyncio.to_thread(read)
        if login is None:
            if not await asyncio.to_thread(_was_set_up):
                logger.info("contacts refresh (%s): the iCloud login is not "
                            "set up", reason)
                return None
            # It worked before: say what is wrong with it now.
            found = await asyncio.to_thread(logins.check, LOGIN)
            _login_check = found
            logger.warning("contacts refresh (%s) failed: %s. %s", reason,
                           LOGIN_ERROR, found.sentence())
            return _failed(LOGIN_ERROR)
        _login_check = None
        store = active_store()
        if store is None:
            logger.warning("contacts refresh (%s) skipped: %s", reason,
                           UNAVAILABLE_ERROR)
            return _failed(UNAVAILABLE_ERROR)
        result = await sync.run(
            store, ICloudSource(login["username"], login["password"]),
            default_region=default_region(), model=_model)
    except Exception:  # noqa: BLE001 — a refresh never raises
        logger.warning("contacts refresh (%s) failed", reason, exc_info=True)
        return _failed(UNEXPECTED_ERROR)
    if result.ok:
        # The problem is over: nothing is left to report.
        _service_failing = False
        _pending = None
    return result


async def refresh(*, reason: str) -> sync.Result | None:
    """Refresh the iCloud mirror, or join the refresh already running.
    None when contacts are not set up; a failed result with
    :data:`LOGIN_ERROR` when a login that worked before cannot be read
    (:func:`login_problem` then says what is wrong with it)."""
    global _running
    if _running is None or _running.done():
        _running = asyncio.ensure_future(_refresh(reason))
    return await asyncio.shield(_running)


async def run_daily() -> None:
    """The scheduled refresh; tells the operator once per failure streak,
    and sends again a notice that did not reach the operator. Never
    raises."""
    global _pending
    try:
        result = await refresh(reason="scheduled")
        if result is not None and result.tell_operator:
            line = (SERVICE_FAILED_LINE if result.error in _SERVICE_ERRORS
                    else FAILED_LINE).format(error=result.error)
            found = _login_check
            if result.error == LOGIN_ERROR and found is not None \
                    and found.sentence():
                line = f"{line} {found.sentence()}"
            copy = await asyncio.to_thread(_copy_line)
            if copy:
                line = f"{line} {copy}"
            if _notify is None:
                logger.warning("no operator channel; not sent: %s", line)
            else:
                _pending = line
        line = _pending
        if line is None or _notify is None:
            return
        if await _notify(casa_text(line)):
            if _pending == line:
                _pending = None
        else:
            logger.warning("contacts refresh line not delivered; it is sent "
                           "again by the next daily run")
    except Exception:  # noqa: BLE001 — a scheduled job never raises
        logger.warning("contacts daily refresh failed", exc_info=True)
