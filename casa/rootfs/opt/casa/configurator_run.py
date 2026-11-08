"""The configurator's run lease and its Restart now button.

The configurator is a delegated run in the operator's DM, not an engagement.
Every configurator run, from every origin (Ellen's delegation, a swipe-reply
on its message, an Approve continuation), takes this one process-wide lease
before anything of the run starts, and holds it until the run's session has
ended. A later request waits up to ``LEASE_WAIT_S``, then is refused with the
busy text and nothing of it runs. A new run also waits, within the same
bound, for plugin erasures still running (``tools._ERASE_TASKS``) and for
specialist-bundle transactions that outlived a cancelled run
(``tools._BUNDLE_MUTATION_TASKS``): both still write what the run would read.

The lease is persisted at ``LEASE_PATH`` while held. A run that ends while Casa
is stopping keeps the file, however it ended, so the next boot finds it, removes it, and tells the
operator once which task was interrupted (``claim_interrupted`` and
``announce_interrupted``); nothing is replayed.

A restart the configurator needs is never made inline: ``note_restart``
records it for the run, and the run's answer is followed by a Casa message with
one "Restart now" button (``offer_restart``). The button is Casa's own keyboard
(``operator_calls.post_question``): spent on its first tap, edited to say so
when it expires or Casa restarts first.

While a run holds the lease, the plugins it installs are held back from the
plugin-health DM (``installing``); the release schedules the deferred pass.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import time
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import specialist_desk

logger = logging.getLogger(__name__)

LEASE_PATH = "/data/configurator-lease.json"
LEASE_WAIT_S = 60.0
TASK_CHARS = 200
UNKNOWN_TASK = "a configuration task"
RESTART_BUTTON = "Restart now"
RESTART_TEXT = "Restart Casa to finish this change."
RESTART_BUTTON_TTL_S = 24 * 3600.0
RESTART_FAILED = "Casa could not ask Home Assistant for the restart ({status}). Try again later."


class ConfiguratorBusy(specialist_desk.DeskBusy):
    """Another configurator run held the lease past the wait bound."""


def busy_text(task: str) -> str:
    return f"the configurator is busy with «{task}»; try again shortly"


def interrupted_text(task: str) -> str:
    return (f"Casa restarted while the Configurator was working on «{task}». "
            "Nothing was replayed; ask again if it is still needed.")


def _clip(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= TASK_CHARS else text[:TASK_CHARS - 1] + "…"


@dataclasses.dataclass
class Lease:
    record: dict
    installing: set = dataclasses.field(default_factory=set)
    released: bool = False
    watched: bool = False          # the run's inner task releases it

    def release(self, *, keep_file: bool = False) -> None:
        _release(self, keep_file=keep_file)


_lock: "asyncio.Lock | None" = None
_held: "Lease | None" = None
# Strong refs for the notify passes a release schedules.
_NOTIFIES: set = set()


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def holder() -> "dict | None":
    return dict(_held.record) if _held is not None else None


def _leftovers() -> "tuple[list, list]":
    """Plugin erasures and surviving bundle transactions still running."""
    import tools
    return ([t for t in list(tools._ERASE_TASKS) if not t.done()],
            [t for t in list(tools._BUNDLE_MUTATION_TASKS) if not t.done()])


async def _leftovers_done() -> None:
    while True:
        erasing, bundles = _leftovers()
        if not erasing and not bundles:
            return
        await asyncio.wait(erasing + bundles)


async def acquire(*, run_key: str, via: str, task: str) -> Lease:
    """Take the lease, waiting up to ``LEASE_WAIT_S`` for the holder, any
    running plugin erasure and any surviving bundle transaction; past the
    bound raise :class:`ConfiguratorBusy`."""
    global _held
    lock = _get_lock()
    deadline = time.monotonic() + LEASE_WAIT_S

    def _busy() -> ConfiguratorBusy:
        holding = _held.record.get("task") if _held is not None else None
        if not holding:
            holding = ("removing a plugin's data" if _leftovers()[0]
                       else "finishing a specialist change")
        return ConfiguratorBusy(busy_text(holding))

    try:
        await asyncio.wait_for(lock.acquire(), max(deadline - time.monotonic(), 0.0))
    except asyncio.TimeoutError:
        raise _busy() from None
    try:
        await asyncio.wait_for(_leftovers_done(), max(deadline - time.monotonic(), 0.0))
    except asyncio.TimeoutError:
        lock.release()
        raise _busy() from None
    except BaseException:
        lock.release()
        raise
    record = {"run_key": str(run_key or ""), "via": via, "task": _clip(task),
              "started_at": time.time()}
    lease = Lease(record=record)
    _held = lease
    try:
        _write(record)
    except OSError:
        # the boot notice is the only reader; a run is never refused for it
        logger.warning("configurator lease file not written", exc_info=True)
    return lease


def _write(record: dict) -> None:
    from atomic_io import atomic_write_json
    atomic_write_json(LEASE_PATH, record, mode=0o600)


def _release(lease: Lease, *, keep_file: bool) -> None:
    global _held
    if lease.released:
        return
    lease.released = True
    if _held is lease:
        _held = None
    if not keep_file:
        try:
            os.unlink(LEASE_PATH)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("configurator lease file not removed", exc_info=True)
    lock = _get_lock()
    if lock.locked():
        lock.release()
    if lease.installing:
        import tools
        task = asyncio.ensure_future(tools._notify_plugin_health_if_possible())
        _NOTIFIES.add(task)
        task.add_done_callback(_NOTIFIES.discard)


def release_when_done(lease: Lease, inner: asyncio.Task) -> None:
    """Release *lease* when the run's inner task has really ended. A run that
    ends while Casa is stopping keeps the file for the next boot's notice,
    however it ended: a runner may turn the stop's cancellation into a normal
    return, and that run was interrupted all the same."""
    lease.watched = True

    def _done(_task: asyncio.Task) -> None:
        import agent as agent_mod
        _release(lease, keep_file=agent_mod.process_stopping())
    inner.add_done_callback(_done)


# -- D4: plugins installed under the lease -----------------------------------

def record_installing(names) -> None:
    if _held is None:
        return
    for name in names:
        if isinstance(name, str) and name:
            _held.installing.add(name)


def installing() -> set:
    return set(_held.installing) if _held is not None else set()


# -- boot: an interrupted run --------------------------------------------------

def claim_interrupted() -> "dict | None":
    """The lease record a previous process left behind, removed so it is
    announced once and never blocks a run. An unreadable file counts as held."""
    try:
        with open(LEASE_PATH, encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None
    except OSError:
        raw = ""
    try:
        record = json.loads(raw)
        if not isinstance(record, dict):
            raise ValueError
    except ValueError:
        record = {}
    try:
        os.unlink(LEASE_PATH)
    except OSError:
        logger.warning("configurator lease file not removed at boot", exc_info=True)
    record.setdefault("run_key", "")
    if not record.get("task"):
        record["task"] = UNKNOWN_TASK
    return record


async def announce_interrupted(record: "dict | None", *, recovered_ids,
                               notify: Callable[[str], Awaitable[Any]]) -> None:
    """Tell the operator once, unless the job registry already announces the
    same run (a delegation Ellen started has its own restart notice)."""
    if not record:
        return
    if record.get("run_key") and record["run_key"] in set(recovered_ids or ()):
        return
    try:
        await notify(interrupted_text(record.get("task") or UNKNOWN_TASK))
    except Exception as exc:  # noqa: BLE001 — logged, not retried
        logger.warning("interrupted configurator run not announced: %s", type(exc).__name__)


# -- the Restart now button ----------------------------------------------------

_RESTARTS: dict[str, str] = {}


def note_restart(run_key: str, task: str = "") -> None:
    """The run *run_key* needs a Casa restart; its answer gets the button."""
    if run_key:
        _RESTARTS[str(run_key)] = _clip(task)


def _operator_identity() -> Any:
    import trigger_consent
    from result_broker import _telegram_channel
    channel = _telegram_channel()
    op = trigger_consent.operator_identity(channel) if channel is not None else None
    if op is None:
        return None
    return SimpleNamespace(chat_id=op[0], operator_id=op[1])


async def _restart_on_tap(outcome: dict) -> None:
    if outcome.get("outcome") != "answered":
        return
    import supervisor_restart
    result = await supervisor_restart.request_app_restart()
    status = result.get("supervisor_status") if isinstance(result, dict) else None
    if status == 200:
        return
    logger.warning("restart button: Supervisor answered %r", status)
    from result_broker import _telegram_channel
    channel = _telegram_channel()
    notify = getattr(channel, "notify_operator", None)
    if notify is not None:
        try:
            await notify(RESTART_FAILED.format(status=status or "no answer"))
        except Exception:  # noqa: BLE001
            logger.warning("restart failure line not sent", exc_info=True)


async def offer_restart(run_key: str) -> bool:
    """Post the Restart now button once for *run_key*, if its run asked for a
    restart. Returns whether a button was posted."""
    if not run_key or str(run_key) not in _RESTARTS:
        return False
    _RESTARTS.pop(str(run_key), None)
    import operator_calls
    try:
        return await operator_calls.post_question(
            identity=_operator_identity(), role="configurator", text=RESTART_TEXT,
            options=[RESTART_BUTTON], timeout_s=RESTART_BUTTON_TTL_S,
            on_settle=_restart_on_tap)
    except Exception:  # noqa: BLE001 — the answer has gone out; log only
        logger.warning("restart button not posted", exc_info=True)
        return False


def _reset_for_tests() -> None:
    global _held, _lock
    _held = None
    _lock = None
    _RESTARTS.clear()
