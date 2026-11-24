"""The running contacts service: the store Casa opened at boot, the iCloud
secret, one shared refresh, and the daily job.

The iCloud account is the vault item :data:`ITEM_TITLE` (fields ``username``
and ``password``) in the default vault, read through the ``op`` front, which
serves both secret backends. Without both fields contacts are not set up and a
refresh does nothing.

A refresh never raises: an unavailable store or an unexpected error is a
failed :class:`contacts.sync.Result`. Concurrent callers share the refresh
already running. The daily job tells the operator about a failure once per
failure streak: the store records the streak of a sync that ran, and a
failure before a sync could run (an unavailable store, an unexpected error)
keeps its streak here in memory, since the store may be what is broken.
Delivery is tracked apart from the streak: a notice that did not reach the
operator stays pending and is sent again by the next daily run, until it is
delivered or a refresh succeeds.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from typing import Awaitable, Callable

from contacts import notes, sync
from contacts import store as cstore
from contacts.carddav import ICloudSource
from contacts.store import ContactsStore
from output_boundary import casa_text

logger = logging.getLogger(__name__)

ITEM_TITLE = "iCloud Contacts"
SOURCE = ICloudSource.name
OP_TIMEOUT_S = 10

FAILED_LINE = "Contacts could not be refreshed from iCloud: {error}."
SERVICE_FAILED_LINE = "Contacts could not be refreshed: {error}."
KEPT_LINE = "The last good copy is still in use."
NONE_STORED_LINE = "No contacts are stored yet."
UNAVAILABLE_ERROR = "the contacts store is unavailable"
UNEXPECTED_ERROR = "the refresh stopped on an unexpected error"
_SERVICE_ERRORS = (UNAVAILABLE_ERROR, UNEXPECTED_ERROR)

_store: ContactsStore | None = None
_notify: Callable[[str], Awaitable[bool]] | None = None
_model: notes.Model | None = None
_read_secret: Callable[[str], str | None] | None = None
_running: asyncio.Future | None = None
#: True from a failure before a sync could run until the next successful
#: sync: the operator has been told about this streak.
_service_failing = False
#: The failure notice not yet delivered to the operator, if any.
_pending: str | None = None


def configure(*, store: ContactsStore,
              notify: Callable[[str], Awaitable[bool]] | None,
              model: notes.Model | None,
              read_secret: Callable[[str], str | None]) -> None:
    global _store, _notify, _model, _read_secret, _running, _service_failing
    global _pending
    _store, _notify, _model, _read_secret = store, notify, model, read_secret
    _running = None
    _service_failing = False
    _pending = None


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


def read_icloud_secret(field: str) -> str | None:
    """One field of :data:`ITEM_TITLE` in the default vault, or None when it
    cannot be read. Blocking; ``op`` output is never logged or returned
    except the value itself."""
    vault = os.environ.get("ONEPASSWORD_DEFAULT_VAULT", "")
    if not vault:
        return None
    try:
        result = subprocess.run(
            ["op", "item", "get", ITEM_TITLE, "--vault", vault,
             "--fields", f"label={field}", "--reveal"],
            capture_output=True, text=True, timeout=OP_TIMEOUT_S,
            stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("contacts: %s of %r not read (%s)", field, ITEM_TITLE,
                    type(exc).__name__)
        return None
    if result.returncode != 0:
        logger.info("contacts: %s of %r not read (op exited %d)", field,
                    ITEM_TITLE, result.returncode)
        return None
    value = (result.stdout or "").strip()
    return value or None


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
    global _service_failing, _pending
    try:
        read = _read_secret
        if read is None:
            return None
        username = await asyncio.to_thread(read, "username")
        password = (await asyncio.to_thread(read, "password")
                    if username else None)
        if not username or not password:
            logger.info("contacts refresh (%s): %r is not set up", reason,
                        ITEM_TITLE)
            return None
        store = active_store()
        if store is None:
            logger.warning("contacts refresh (%s) skipped: %s", reason,
                           UNAVAILABLE_ERROR)
            return _failed(UNAVAILABLE_ERROR)
        result = await sync.run(store, ICloudSource(username, password),
                                default_region=default_region(),
                                model=_model)
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
    None when contacts are not set up."""
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
