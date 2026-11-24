"""The running phone line: what Casa's boot configures, the call webhook's
two callbacks, the call slots, the record of an ended call and its hand-off
to the assistant (:mod:`phone.record`), the stop and the nightly purge.

The API key and the webhook signing secret are the values
:data:`KEY_FIELD` and :data:`SECRET_FIELD` of the login :data:`LOGIN` of
:mod:`logins`: the vault item chosen for it, or else the one under its
default title. They are read through the blocking ``op`` front in a worker
thread, never on the event loop. A value read is kept in memory for
:data:`CACHE_S`, so a call does not wait on the vault; a missing value is
never kept, and concurrent readers of one value share one read. What is kept
is kept per vault item, and a choice of item (a new one, or the same one
taken again) drops all of it, so after a choice a key and a secret never
come from two different items. Each value is kept on its own, so under the
default title an item replaced in the vault can serve the two from different
items until :data:`CACHE_S` runs out. A read already under way when the choice is made is thrown
away when it ends and the value is read once more.

When a call has ended, Casa's own first line about it
(``phone.controller.headline``: who, the number, when, how it ended) is sent
to the operator before the record is written, and the record is handed to
the assistant only once that line was delivered. A line that was not is
tried again after each of :data:`HEADLINE_RETRY_S`, in the same task and
never once the stop has begun; one that still was not leaves the call
undelivered, for :func:`redeliver`.

At most :data:`MAX_CALLS` calls run at once. The webhook's callback
(:func:`on_incoming`) is synchronous: it reserves a slot and starts one task,
which reads the key and runs the call, or, when both slots are taken, only
answers busy. Without a key nothing at all is sent to the provider (the
caller falls to the carrier's fallback); without a signing secret the
webhook answers 503. Either way the operator is told what is wrong with the
login (:meth:`logins.Check.sentence`), at most once an hour.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Callable

import aiohttp
from aiohttp import web

import logins
from contacts import service as contacts_service
from contacts.store import ContactsStore
from live_voice.api import ApiError, LiveApi
from live_voice.session import Timing
from output_boundary import casa_text
from phone import controller
from phone import record as precord
from phone import settings
from phone import store as pstore
from phone.abilities import Notify
from phone.store import PhoneStore
from phone.webhook import PHONE_WEBHOOK_PATH, Incoming, make_handler
from rate_limit import RateLimiter

if TYPE_CHECKING:
    from bus import MessageBus

logger = logging.getLogger(__name__)

#: The login of :mod:`logins` that holds the key and the signing secret.
LOGIN = "openai_voice"
KEY_FIELD = "credential"
SECRET_FIELD = "webhook_secret"
#: Seconds a value read from the vault is kept in memory.
CACHE_S = 600.0
MAX_CALLS = 2
#: The shortest time between two lines to the operator about a login that
#: cannot be read.
MISSING_EVERY_S = 3600.0
#: How long the stop waits for the hung-up calls to end, then for the tasks
#: it cancels after that.
STOP_WAIT_S = 5.0
#: Call webhook requests a minute.
RATE_PER_MIN = 30
#: How long after its end an undelivered call is still handed on at a boot.
REDELIVER_WITHIN_S = 24 * 3600.0

#: How the operator's line opens, and what it says when the check itself
#: finds nothing wrong (the read failed for a moment).
NO_KEY_LINE = "A phone call came in but could not be answered."
NO_KEY_UNKNOWN = "The API key could not be read from the vault."
NO_SECRET_LINE = "The phone line cannot check incoming calls."
NO_SECRET_UNKNOWN = ("The webhook signing secret could not be read from the "
                     "vault.")
#: What follows the first line about a call whose record could not be handed
#: to the assistant.
NOT_DELIVERED = "The summary could not be delivered."
#: Seconds to wait before each further try of a first line that was not
#: delivered.
HEADLINE_RETRY_S: tuple[float, ...] = (20.0, 120.0)


def _read_login(item: str, field: str) -> str | None:
    """The value *field* of the login in the vault item *item*. Blocking."""
    return logins.read_from(LOGIN, item, field)


def _check_login() -> logins.Check:
    """What is wrong with the login, if anything. Blocking."""
    return logins.check(LOGIN)


_store: PhoneStore | None = None
_contacts: Callable[[], ContactsStore | None] = lambda: None
_notify: Notify | None = None
_read_secret: Callable[[str, str], str | None] | None = None
_check: Callable[[], logins.Check] = _check_login
_http: aiohttp.ClientSession | None = None
_timing = Timing()
_clock: Callable[[], float] = time.monotonic
#: Wall-clock time, for the day a first line's time is told against.
_now: Callable[[], float] = time.time
_base_url = ""
#: (vault item: the chosen id or the default title, field)
#: -> (value, monotonic expiry)
_cache: dict[tuple[str, str], tuple[str, float]] = {}
#: (vault item, field) -> the read in flight
_reading: dict[tuple[str, str], asyncio.Future] = {}
#: Counts the choices of a vault item for the login. A read belongs to the
#: choice it began under, and its value is used only while that choice is
#: still the one in force.
_generation = 0
#: session id -> its client once the call has one; a key here holds a slot.
_running: dict[str, LiveApi | None] = {}
_tasks: set[asyncio.Task] = set()
#: The admission tasks among :data:`_tasks` (one per incoming call).
_call_tasks: set[asyncio.Task] = set()
#: True from :func:`begin_stop` (or :func:`stop`): no call is admitted or
#: accepted, and no record is handed on, any more.
_stopping = False
#: Set with :data:`_stopping`, so a wait between two tries ends at once.
_stop_signal = asyncio.Event()
#: The ids of the call rows this process wrote that have no outcome yet: a
#: row with no outcome that is not among them was cut off by an earlier Casa.
_ours: set[str] = set()
#: Running calls whose accept has been sent.
_accepted: set[str] = set()
#: Running calls the stop hung up (marked as the hang-up goes, unmarked when
#: the provider refuses it): they are recorded as ``interrupted``.
_cut: set[str] = set()
_missing_told_at: float | None = None
_bus: MessageBus | None = None
_route_for: Callable[[str], Any] | None = None
#: Calls whose record is being written or handed on right now.
_finishing: set[str] = set()


def configure(*, store: PhoneStore | None,
              contacts: Callable[[], ContactsStore | None],
              notify: Notify | None,
              read_secret: Callable[[str, str], str | None] = _read_login,
              check: Callable[[], logins.Check] = _check_login,
              http: aiohttp.ClientSession,
              timing: Timing = Timing(),
              clock: Callable[[], float] = time.monotonic,
              base_url: str = "",
              bus: MessageBus | None = None,
              route_for: Callable[[str], Any] | None = None) -> None:
    """Set up the service. ``http`` is the one client session of the phone
    line; :func:`stop` closes it. ``bus`` and ``route_for`` (the Telegram
    transport's, when Telegram is configured) carry an ended call's record to
    the assistant; without a bus every ended call gets only its first line
    and :data:`NOT_DELIVERED`. ``read_secret`` reads one value of the login
    from a vault item and ``check`` says what is wrong with the login; both
    are blocking.
    ``base_url`` overrides the API's address (tests)."""
    global _store, _contacts, _notify, _read_secret, _check, _http, _timing
    global _clock, _base_url, _missing_told_at, _bus, _route_for
    global _stopping, _stop_signal
    _store, _contacts, _notify, _read_secret = store, contacts, notify, \
        read_secret
    _check = check
    logins.on_change(LOGIN, _login_changed)
    _http, _timing, _clock, _base_url = http, timing, clock, base_url
    _bus, _route_for = bus, route_for
    _cache.clear()
    _reading.clear()
    _missing_told_at = None
    _stopping = False
    _stop_signal = asyncio.Event()
    _ours.clear()
    _accepted.clear()
    _cut.clear()


def enabled() -> bool:
    return settings.options().enabled


def active_store() -> PhoneStore | None:
    """The configured store while it is available; otherwise None."""
    store = _store
    return store if store is not None and store.available else None


# --- the vault ---------------------------------------------------------------

def _login_changed() -> None:
    """A vault item was chosen for the login (another one, or the same one
    again, whose values may be new), or the choice was dropped: nothing read
    so far is used again, a read in flight is no longer joined, and what
    such a read finds is thrown away when it ends."""
    global _generation
    _generation += 1
    _cache.clear()
    _reading.clear()


async def _fetch(item: str, field: str,
                 generation: int) -> tuple[str | None, bool]:
    """Read *field* from *item* for a reader that began under choice number
    *generation*: ``(value, True)``, or ``(None, False)`` when the login was
    chosen again while the vault was read. Such a value is neither kept nor
    handed on: it may be the one the new choice replaced."""
    read = _read_secret
    if read is None:
        return None, True
    try:
        value = await asyncio.to_thread(read, item, field)
    except Exception:  # noqa: BLE001 — an unreadable value is a missing one
        logger.warning("phone: %s of the vault item %r not read", field,
                       item, exc_info=True)
        value = None
    if generation != _generation:
        return None, False
    if value:
        _cache[(item, field)] = (value, _clock() + CACHE_S)
    return value or None, True


async def _field(field: str) -> str | None:
    # The read, and one more when the login was chosen again meanwhile. A
    # second read that is overtaken as well gives nothing: never a value
    # read under a choice that no longer holds, and never a loop.
    for _attempt in range(2):
        slot = (logins.selection(LOGIN), field)
        hit = _cache.get(slot)
        if hit is not None and _clock() < hit[1]:
            return hit[0]
        reading = _reading.get(slot)
        if reading is None or reading.done():
            reading = asyncio.ensure_future(_fetch(*slot, _generation))
            _reading[slot] = reading
        # A reader that gives up does not cancel the read the others share.
        value, current = await asyncio.shield(reading)
        if current:
            return value
    return None


async def key() -> str | None:
    """The API key, or None when the vault has none."""
    return await _field(KEY_FIELD)


async def secret() -> str | None:
    """The webhook signing secret, or None when the vault has none."""
    return await _field(SECRET_FIELD)


# --- lines to the operator --------------------------------------------------

async def _send(text: str) -> bool:
    """One line to the operator; True when it was delivered."""
    notify = _notify
    if notify is None:
        logger.warning("phone: no operator channel; a line was not sent")
        return False
    try:
        if await notify(casa_text(text)):
            return True
        logger.warning("phone: a line to the operator was not delivered")
    except Exception:  # noqa: BLE001 — a lost line is logged, never raised
        logger.warning("phone: a line to the operator was not sent",
                       exc_info=True)
    return False


async def _login_sentence() -> str:
    """What is wrong with the login, for the operator; ``""`` when the
    check finds nothing wrong or cannot be made."""
    try:
        return (await asyncio.to_thread(_check)).sentence()
    except Exception:  # noqa: BLE001 — the line is still sent without it
        logger.warning("phone: the login could not be checked", exc_info=True)
        return ""


async def _tell_missing(opening: str, unknown: str) -> None:
    """Tell the operator that a value of the login could not be read:
    *opening*, then what is wrong with the login (*unknown* when the check
    does not say). At most one such line every :data:`MISSING_EVERY_S`."""
    global _missing_told_at
    now = _clock()
    before = _missing_told_at
    if before is not None and now - before < MISSING_EVERY_S:
        return
    # Taken before the check and the send, so a call arriving meanwhile
    # sends no second line; given back when the line did not go, so the
    # next call tries.
    _missing_told_at = now
    line = f"{opening} {await _login_sentence() or unknown}"
    logger.warning("phone: %s", line)
    if not await _send(line) and _missing_told_at == now:
        _missing_told_at = before


async def _webhook_secret() -> str | None:
    """The signing secret, for the webhook. With none the webhook answers
    its own 503, and the operator is told what is wrong with the login."""
    value = await secret()
    if not value and not _stopping:
        _start(_tell_missing(NO_SECRET_LINE, NO_SECRET_UNKNOWN),
               "phone missing secret")
    return value


async def _written(transcript: str | None) -> dict:
    """The model-written part of a call's record; without a key, the failed
    shape (Casa still hands on what it knows)."""
    api_key = await key()
    if not api_key or _http is None or _http.closed:
        logger.warning("phone: no API key; a call record has no summary")
        return precord.failed()
    found = await asyncio.to_thread(settings.effective, _store)
    api = LiveApi(key=api_key, host=settings.options().api_host, http=_http,
                  base_url=_base_url)
    return await precord.write(api, model=found.models["record_model"],
                               transcript=transcript or "")


async def _deliver(store: PhoneStore, call_id: str, rec: dict) -> bool:
    """Give *rec* to the assistant; True when the bus took it. The call is
    then marked delivered (a failed mark is only logged: the record has gone
    and the next boot may hand it on once more)."""
    bus = _bus
    if bus is None:
        logger.warning("phone: no bus; call %s was not handed on", call_id)
        return False
    outcome = await precord.hand_off(bus, rec, route_for=_route_for)
    if outcome != "accepted":
        logger.warning("phone: call %s was not handed on (%s)", call_id,
                       outcome)
        return False

    def mark() -> None:
        with store.transaction(write=True) as conn:
            pstore.mark_delivered(conn, call_id)
    try:
        await asyncio.to_thread(mark)
    except Exception:  # noqa: BLE001 — the record already reached the bus
        logger.warning("phone: call %s was handed on but not marked "
                       "delivered", call_id, exc_info=True)
    return True


async def _stopped_within(seconds: float) -> bool:
    """Wait *seconds*, or less when the stop begins; True once it has."""
    try:
        await asyncio.wait_for(_stop_signal.wait(), seconds)
    except asyncio.TimeoutError:
        pass
    return _stopping


async def _headline(store: PhoneStore, row: dict) -> bool:
    """Send the call's first line to the operator, trying again after each
    of :data:`HEADLINE_RETRY_S` while it is not delivered; True when it was.
    Nothing is sent once the stop has begun. The call is then marked (a
    failed mark is only logged: the line has gone, and the next boot may
    send it once more)."""
    for wait in (0.0, *HEADLINE_RETRY_S):
        if wait and await _stopped_within(wait):
            return False
        if _stopping:
            return False
        # Built at each try: a try after midnight dates the time.
        if await _send(controller.headline(row, now=_now())):
            break
        logger.info("phone: the first line of call %s was not delivered",
                    row["id"])
    else:
        return False

    def mark() -> None:
        with store.transaction(write=True) as conn:
            pstore.mark_headline_sent(conn, row["id"])
    try:
        await asyncio.to_thread(mark)
    except Exception:  # noqa: BLE001 — the line already reached the operator
        logger.warning("phone: the first line of call %s was sent but not "
                       "marked", row["id"], exc_info=True)
    return True


async def _finish(call_id: str, *, again: bool) -> None:
    store = _store
    if store is None or not store.available:
        return
    if _stopping:
        # The bus may no longer be read: the row stays undelivered and the
        # next boot hands it on.
        logger.info("phone: call %s is handed on by the next boot", call_id)
        return

    def read() -> tuple[dict | None, str | None]:
        with store.transaction() as conn:
            return (pstore.get_call(conn, call_id),
                    pstore.transcript(conn, call_id))
    row, transcript = await asyncio.to_thread(read)
    if row is None:
        logger.warning("phone: no call %s to report", call_id)
        return
    if row["delivered"]:
        return
    # The first line goes before the record is written, so a record that
    # fails is still announced; the assistant, told the operator has seen
    # it, is given the record only once it was delivered.
    told_now = not row.get("headline_sent") and await _headline(store, row)
    told = told_now or bool(row.get("headline_sent"))
    if not told and _stopping:
        logger.info("phone: call %s is handed on by the next boot", call_id)
        return
    # At a boot, a record written before the stop is handed on as it is.
    stored = again and isinstance(row.get("record"), dict)
    try:
        if stored:
            written = row["record"]
        else:
            written = await _written(transcript)

            def keep() -> None:
                with store.transaction(write=True) as conn:
                    pstore.set_record(conn, call_id, written)
            await asyncio.to_thread(keep)
        if not told:
            logger.warning("phone: call %s is not handed on: its first line "
                           "did not reach the operator", call_id)
            return
        if _stopping:
            logger.info("phone: call %s is handed on by the next boot",
                        call_id)
            return
        # Nothing is awaited between the check above and the hand-off.
        if await _deliver(store, call_id, precord.present(row, written)):
            return
    except Exception:  # noqa: BLE001 — the note below
        logger.warning("phone: call %s was not handed on", call_id,
                       exc_info=True)
    # The operator already had the note about a stored record when it failed
    # to go on after its first line.
    if told and (told_now or not stored):
        await _send(NOT_DELIVERED)


async def _once(call_id: str, *, again: bool) -> None:
    if call_id in _finishing:
        return
    _finishing.add(call_id)
    try:
        await _finish(call_id, again=again)
    except Exception:  # noqa: BLE001 — never raises; the row stays undelivered
        logger.warning("phone: call %s was not reported", call_id,
                       exc_info=True)
    finally:
        _finishing.discard(call_id)


async def finish(call_id: str) -> None:
    """Report an ended call: its first line goes to the operator, the record
    is written from the transcript and kept on the row, and, once that line
    was delivered, given to the assistant and the call marked delivered.
    When it cannot be given (no bus, no assistant, any failure) the operator
    gets :data:`NOT_DELIVERED` after the first line, and the row stays
    undelivered; when the first line was not delivered, nothing is given
    and the row stays undelivered. Never raises."""
    await _once(call_id, again=False)


async def _finish_later(call_id: str) -> None:
    """The call's own hand-on: the record is written in a task of its own,
    so the ended call's slot is free while the model writes it. A record the
    stop cuts short is handed on by the next boot (:func:`redeliver`)."""
    _start(finish(call_id), f"phone record {call_id}")


async def redeliver() -> None:
    """At a boot: hand on every undelivered call that ended within
    :data:`REDELIVER_WITHIN_S`. A call whose first line has not reached the
    operator gets it first. A call with a stored record is handed on as it
    is; one whose record was never written (Casa stopped first) is finished
    now; one that never ended (Casa was killed during it) is ended now as
    ``interrupted`` and then finished. Never raises."""
    try:
        store = _store
        if store is None or not store.available:
            return

        def read() -> list[dict]:
            with store.transaction(write=True) as conn:
                _close_interrupted(conn, ours)
                return pstore.undelivered(conn)
        ours = frozenset(_ours)
        cutoff = time.time() - REDELIVER_WITHIN_S
        owed = [r["id"] for r in await asyncio.to_thread(read)
                if isinstance(r.get("ended_at"), (int, float))
                and r["ended_at"] >= cutoff]
        for call_id in owed:
            await _once(call_id, again=True)
        if owed:
            logger.info("phone: retried %d undelivered call record(s)",
                        len(owed))
    except Exception:  # noqa: BLE001 — a boot job never raises
        logger.warning("phone: undelivered call records were not handed on",
                       exc_info=True)


def _close_interrupted(conn, ours: frozenset[str]) -> None:
    """End, as ``interrupted`` and now, every call with no outcome that this
    process did not write: an earlier Casa was cut off during it."""
    now = time.time()
    for row in pstore.undelivered(conn):
        if row.get("outcome") is None and row["id"] not in ours:
            pstore.finish_call(conn, row["id"], ended_at=now,
                               outcome="interrupted",
                               actions=row.get("actions") or [],
                               record=row.get("record"))
            logger.info("phone: call %s was cut off by the last stop",
                        row["session_id"])


# --- calls -------------------------------------------------------------------

def _start(coro, name: str) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def on_incoming(incoming: Incoming) -> None:
    """The webhook's callback for a new call: reserve a slot and start the
    task that answers it (or answers busy when no slot is free)."""
    if _stopping or _http is None or _http.closed:
        # Unanswered, the call falls to the carrier's fallback.
        logger.warning("phone: a call came in while the line is not set up "
                       "or is stopping")
        return
    sid = incoming.session_id
    free = len(_running) < MAX_CALLS and sid not in _running
    if free:
        _running[sid] = None
    task = _start(_admit(incoming, free), f"phone call {sid}")
    _call_tasks.add(task)
    task.add_done_callback(_call_tasks.discard)


async def _admit(incoming: Incoming, reserved: bool) -> None:
    sid = incoming.session_id
    try:
        api_key = await key()
        if _stopping:
            # Casa began to stop while the key was read: nothing is sent,
            # and the carrier's fallback takes the call.
            logger.info("phone: call %s not answered: Casa is stopping", sid)
            return
        if not api_key or _http is None:
            await _tell_missing(NO_KEY_LINE, NO_KEY_UNKNOWN)
            return
        api = LiveApi(key=api_key, host=settings.options().api_host,
                      http=_http, base_url=_base_url)
        if not reserved:
            logger.info("phone: call %s answered busy (%d calls running)",
                        sid, len(_running))
            try:
                await api.reject(sid, 486)
            except ApiError as exc:
                logger.warning("phone: busy answer for %s failed: %s "
                               "(status %s)", sid, exc.code, exc.status)
            return
        _running[sid] = api
        await controller.handle_call(incoming, controller.Deps(
            api=api, store=_store, contacts=_contacts,
            options=settings.options(),
            default_region=contacts_service.default_region(),
            notify=_notify, finish=_finish_later, timing=_timing,
            stopping=lambda: _stopping, cut_by_stop=lambda: sid in _cut,
            on_row=_ours.add, forget_row=_ours.discard,
            on_accept=lambda: _accepted.add(sid)))
    except Exception:  # noqa: BLE001 — a task never fails unobserved
        logger.exception("phone: call %s could not be admitted", sid)
    finally:
        if reserved:
            _running.pop(sid, None)
            _accepted.discard(sid)
            _cut.discard(sid)


async def _hangup(sid: str, api: LiveApi) -> None:
    """The stop's hang-up of a running call. The call is marked cut by the
    stop before the request goes, so a session close seen before the answer
    still finds it marked; a definite refusal (an HTTP status) takes the mark
    back, unless an earlier hang-up of the stop had set it, so a caller who
    then hangs up keeps that outcome. No answer leaves the mark: the
    provider may have acted."""
    marked = sid in _cut
    _cut.add(sid)
    try:
        await api.hangup(sid)
    except ApiError as exc:
        if exc.status != 0 and not marked:
            _cut.discard(sid)
        logger.warning("phone: hang-up of %s at the stop failed: %s "
                       "(status %s)", sid, exc.code, exc.status)
    except Exception:  # noqa: BLE001 — the stop goes on
        logger.warning("phone: hang-up of %s at the stop failed", sid,
                       exc_info=True)


def begin_stop() -> None:
    """From now on no call is admitted or accepted and no record is handed
    on (a record not handed on stays for the next boot). Synchronous, so the
    stop can declare it before anything it closes."""
    global _stopping
    _stopping = True
    _stop_signal.set()


async def stop() -> None:
    """Stop the line: admit no new call, hang up every running call and wait
    up to :data:`STOP_WAIT_S` for those calls to end, so each records its
    outcome and transcript; then cancel what is left (calls that did not end,
    records still being written, which the next boot's :func:`redeliver`
    finishes) and wait up to :data:`STOP_WAIT_S` again; then close the line's
    client session. Does nothing when the line was never set up."""
    global _stopping
    _stopping = True
    _stop_signal.set()
    hangups = [asyncio.ensure_future(_hangup(sid, api))
               for sid, api in list(_running.items()) if api is not None]
    calls = [task for task in _call_tasks if not task.done()]
    if calls or hangups:
        # The hang-up makes the service close the session, which ends the
        # call's run and lets the controller record it.
        _, late = await asyncio.wait([*calls, *hangups], timeout=STOP_WAIT_S)
        if late:
            logger.warning("phone: %d call(s) did not end at the stop",
                           len([t for t in late if t in calls]))
        for hangup in hangups:
            hangup.cancel()
    # A call still running here may have had its accept land after the
    # hang-up above: it is hung up again, so no accepted session is left
    # with nobody following it once its task is cancelled.
    again = [_hangup(sid, api) for sid in list(_accepted)
             if (api := _running.get(sid)) is not None]
    if again:
        try:
            await asyncio.wait_for(asyncio.gather(*again), STOP_WAIT_S)
        except asyncio.TimeoutError:
            logger.warning("phone: hang-ups of unfinished calls at the stop "
                           "did not finish")
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        _, late = await asyncio.wait(tasks, timeout=STOP_WAIT_S)
        if late:
            logger.warning("phone: %d call task(s) did not end at the stop",
                           len(late))
    http = _http
    if http is not None and not http.closed:
        await http.close()


async def purge() -> None:
    """Delete the transcripts whose time is up. Never raises."""
    try:
        store = _store
        if store is None or not store.available:
            return

        def run() -> int:
            with store.transaction(write=True) as conn:
                return pstore.purge_transcripts(conn, time.time())
        gone = await asyncio.to_thread(run)
        if gone:
            logger.info("phone: %d expired transcript(s) deleted", gone)
    except Exception:  # noqa: BLE001 — a scheduled job never raises
        logger.warning("phone: the transcript purge failed", exc_info=True)


# --- boot ----------------------------------------------------------------------

def add_routes(app: web.Application, *,
               default_region: Callable[[], str]) -> bool:
    """Register the call webhook, with its own rate limit, when the line is
    on; True when it was registered."""
    if not enabled():
        return False
    app.router.add_post(PHONE_WEBHOOK_PATH, make_handler(
        secret=_webhook_secret, on_incoming=on_incoming,
        limiter=RateLimiter(capacity=RATE_PER_MIN, window_s=60.0),
        default_region=default_region))
    return True


async def _boot_check(public_url: str) -> None:
    try:
        # Both are read, so the first call does not wait on the vault.
        found = (await key(), await secret())
        wrong = "" if all(found) else (
            await _login_sentence()
            or "the login could not be read from the vault.")
    except Exception:  # noqa: BLE001 — a boot check never raises
        logger.warning("phone: the vault could not be checked", exc_info=True)
        return
    if wrong:
        logger.warning("phone: enabled, but calls are not answered: %s",
                       wrong)
    elif not public_url:
        logger.warning("phone: enabled, but public_url is not set; the "
                       "provider cannot reach %s", PHONE_WEBHOOK_PATH)
    else:
        logger.info("phone: enabled, webhook at %s%s", public_url,
                    PHONE_WEBHOOK_PATH)


def start_boot_check(public_url: str) -> None:
    """Check the login in the background and log the line's state; the
    boot does not wait for it."""
    line = settings.options().line_number
    if line:
        logger.info("phone: the line's own number ends %s",
                    controller.number_tail(line))
    else:
        logger.info("phone: phone_line_number is not set; a call put "
                    "through that comes back is not recognised")
    _start(_boot_check(public_url), "phone boot check")
