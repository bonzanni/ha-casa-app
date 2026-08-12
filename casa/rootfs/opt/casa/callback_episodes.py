"""Delivery worker for authorization callbacks — redelivery until receipt.

An authorization code dies in 30–600 s; a pull-only pickup with a long TTL is
a live-looking corpse. So casa nudges: when a result lands in the spool
(:mod:`callback_spool`) — or when a flow ends without one — this worker
dispatches a fixed **casa-authored** turn to the plugin's assigned role:

    Authorization result for '<plugin>' is waiting (handle <hash>) —
    collect it now.

    Authorization attempt for '<plugin>' ended without collection
    (handle <hash>) — check the plugin's attempt list.

The turn is internal and **system-attributed** (the ``synthetic`` context
marker, mirroring ``plugin_setup_episodes``/``_setup_dispatch``), so it needs
no ingress-identity row; ``<hash>`` is the non-secret flow handle
(``sha256(state)``, already the artifact filename) so a successor session can
find and collect it. Target selection is the shared
``plugin_dispatch.compose`` — the same target-order decision
``plugin_setup_episodes._compose`` makes: ``resident:assistant`` when
targeted, else the lexicographically-first resident, else the first specialist
via assistant delegation.

**No private ledger (v0.147.0).** The JSON episode store and its consumed-key
tombstones are retired: "the bus accepted a turn" was treated as terminal, and
the tombstone then suppressed every further nudge while the result sat unread
until its TTL killed it. The durable state now lives in the spool's per-flow
**attempt ledger** (``attempts/<hash>.json``), which the spool derives from
the artifacts themselves; this module owns only the delivery half of it:

* **Selection** — an attempt is nudgeable when its ``next_nudge_ts`` is due,
  it has budget left (``callback_attempts.MAX_NUDGES`` bus-ACCEPTED
  dispatches), it is not a settled ``collected``, and — for a
  ``result_ready`` record — the result is still there to collect.
* **Schedule** — a bus accept advances ``next_nudge_ts`` through the
  result-phase offsets anchored on the RESULT INODE'S MTIME (its durable
  publish time, read per pass while the result exists) and then the
  outcome-phase offsets anchored on ``ended_ts``. A pass that exhausts its
  in-pass retries with no accept spends **no** budget and defers on an
  escalating capped delay instead.
* **Timed wake** — the worker waits on the kick event with a timeout derived
  from the nearest due ``next_nudge_ts``. A kick-only worker cannot generate
  scheduled work, and the schedule is durable, so a restart resumes it.
* **Notes** — deliberately asymmetric (spec §10). The budget-exhaustion note
  is **mark-then-notify** (at-most-once: a lost advisory note beats a
  crash-looped duplicate, and the unacked attempt file stays visible anyway);
  a **removal** note is **notify-then-mark** (at-least-once: once the removal
  record prunes, the note was the operator's only surface).

**Request-path discipline.** :func:`kick` is O(1) and touches no file: it
records an in-memory hint and signals the worker. Correctness never depends
on the hint — the attempt ledger is the truth and
:func:`callback_spool.CallbackSpool.attempts_pass` re-derives it every pass —
so a hint lost to a crash converges rather than dropping the flow.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

import callback_attempts
import plugin_dispatch

logger = logging.getLogger(__name__)

_MAX_DISPATCH_ATTEMPTS = 3
_RETRY_BACKOFF_S = (1.0, 5.0)
#: Floor on the timed wake. Bounds how hot the worker can spin when a due
#: entry cannot be cleared, without making the schedule's own resolution
#: coarser than the spec's shortest gap.
_MIN_WAKE_S = 5.0

# Wired by casa_core at boot. All optional — absent seams degrade to
# logging, exactly like plugin_setup_episodes.
_dispatch: Callable[[str, str, dict], Awaitable[bool]] | None = None
_notify_operator: Callable[[str], Awaitable[None]] | None = None
_resolve_registry_entry: Callable[[str], Any] | None = None
_get_spool: Callable[[], Any] | None = None
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
#: Clock seam — injected in tests so the schedule can be driven deterministically
#: without touching the shared ``asyncio.sleep`` (the memory-cage rule).
_now: Callable[[], float] = time.time

_worker_task: asyncio.Task | None = None
_kick: asyncio.Event | None = None

#: The nearest due ``next_nudge_ts`` seen by the last pass — the timed wake's
#: input. ``None`` means nothing is scheduled, so the worker sleeps on the kick
#: alone. Non-durable by construction: the durable schedule is in the ledger,
#: and every pass recomputes this from it.
_next_due: float | None = None

# In-memory, non-durable request-path hint set: kick() appends here (O(1)) and
# the worker drains it. Correctness never depends on it — the attempt ledger is
# the backstop — so a hint lost to a crash converges on the next pass.
_pending_hints: set[tuple[str, str]] = set()

# #532: removal-note filenames whose DM was SENT but whose durable mark then
# failed — later passes retry only the mark (see _process_removal_records).
# Cleared only on a confirmed mark; lost to a crash = one duplicate DM.
_removal_sent_unmarked: "set[str]" = set()


def configure(*, dispatch, resolve_registry_entry, get_spool,
              notify_operator=None, sleep=asyncio.sleep) -> None:
    """casa_core boot wiring. Idempotent. ``get_spool()`` returns the
    process-wide :class:`callback_spool.CallbackSpool` (or ``None`` before boot
    wired it); the worker reads and updates the attempt ledger through it.
    ``resolve_registry_entry(plugin)`` returns an overlay entry
    ``{"targets": [...]}`` (or ``None`` when the plugin cannot be resolved yet
    — the nudge then stays due and retries)."""
    global _dispatch, _notify_operator, _resolve_registry_entry
    global _get_spool, _sleep, _kick, _next_due
    _dispatch = dispatch
    _notify_operator = notify_operator
    _resolve_registry_entry = resolve_registry_entry
    _get_spool = get_spool
    _sleep = sleep
    _next_due = None
    # #532: per-wiring state — a fresh boot's empty set costs at most the
    # documented one-duplicate-per-crash, never a lost notice.
    _removal_sent_unmarked.clear()
    if _kick is None:
        _kick = asyncio.Event()


# ---------------------------------------------------------------------------
# kick — O(1), non-durable, no file I/O on the request path
# ---------------------------------------------------------------------------

def kick(plugin: str, result_hash: str) -> None:
    """Signal the worker that a result landed. Records a non-durable in-memory
    hint and sets the wake event — no spool I/O — so the HTTP handler's
    per-request work stays O(1). The publish that preceded this kick already
    wrote the durable ``result_ready`` attempt; the hint only saves the worker
    a wait."""
    _pending_hints.add((plugin, result_hash))
    if _kick is not None:
        _kick.set()


# ---------------------------------------------------------------------------
# target selection + fixed messages
# ---------------------------------------------------------------------------

def _message(plugin: str, h: str, rec: dict) -> str:
    """The fixed casa-authored nudge for one attempt. A ``result_ready``
    record asks for a collection; a TERMINAL one (expired, evicted, publish
    failure — never ``collected``) tells the consumer to read its attempt list,
    which is where the outcome now lives."""
    if rec.get("status") == "done":
        return (f"Authorization attempt for '{plugin}' ended without "
                f"collection (handle {h}) — check the plugin's attempt list.")
    return (f"Authorization result for '{plugin}' is waiting "
            f"(handle {h}) — collect it now.")


# Target selection is the shared ``plugin_dispatch.compose`` (extracted so
# this and ``plugin_setup_episodes._compose`` can never drift apart in target
# ORDER — see plugin_dispatch.py).
_compose = plugin_dispatch.compose


# ---------------------------------------------------------------------------
# selection — the nudgeable snapshot (blocking; runs off the loop)
# ---------------------------------------------------------------------------

def _is_nudgeable(spool: Any, plugin: str, h: str, rec: dict,
                  now: float) -> bool:
    """Four gates, all of which must hold (spec §8):

    due (``next_nudge_ts`` set and reached), budget left, not a settled
    ``collected``, and — for a ``result_ready`` record — the result still
    present, because nudging for a result that is already gone asks the
    consumer to collect nothing. An ``awaiting_redirect`` record carries no
    schedule at all, so it is excluded by the first gate."""
    nxt = rec.get("next_nudge_ts")
    if nxt is None or nxt > now:
        return False
    if rec.get("nudges", 0) >= callback_attempts.MAX_NUDGES:
        return False
    if rec.get("status") == "done" and rec.get("outcome") == "collected":
        return False
    if rec.get("status") == "result_ready" and not spool.has_result(plugin, h):
        return False
    return True


def _select_nudgeable(spool: Any, now: float) -> list[tuple[str, str, dict]]:
    """Every due attempt across every plugin. Blocking (directory scans and
    stats) — always called through :func:`asyncio.to_thread`."""
    out: list[tuple[str, str, dict]] = []
    for plugin in spool.plugins():
        for h, rec in spool.list_attempts(plugin):
            if _is_nudgeable(spool, plugin, h, rec, now):
                out.append((plugin, h, rec))
    return out


def _scan_next_due(spool: Any) -> float | None:
    """The nearest ``next_nudge_ts`` that can still fire, or ``None`` when
    nothing is scheduled — the timed wake's input, recomputed from the ledger
    AFTER the pass so a just-dispatched entry contributes its NEW slot rather
    than the one it just spent. Blocking; called through
    :func:`asyncio.to_thread`."""
    best: float | None = None
    for plugin in spool.plugins():
        for _h, rec in spool.list_attempts(plugin):
            nxt = rec.get("next_nudge_ts")
            if nxt is None:
                continue
            if rec.get("nudges", 0) >= callback_attempts.MAX_NUDGES:
                continue
            if rec.get("status") == "done" and rec.get("outcome") == "collected":
                continue
            if best is None or nxt < best:
                best = nxt
    return best


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def start_worker() -> None:
    """Boot seam: start the supervised delivery worker. The initial kick makes
    it run one pass immediately, which both drains anything the ledger already
    owes and establishes the timed wake."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.get_running_loop().create_task(
        _worker(), name="callback-episodes")
    if _kick is not None:
        _kick.set()


async def recovery(spool: Any, *, boot: bool = True) -> None:
    """Boot/periodic recovery pass: reconcile the attempt ledger against the
    artifacts (materialize, re-derive, infer receipts, consume acks, apply the
    bounds) and wake the worker. Never dispatches — that is the worker's job —
    so casa_core can run it before the worker starts. Never raises.

    ``boot`` is the pass's in-flight discipline, not a description of when it
    runs: ``True`` (the default, for the boot seam) reconciles every hash,
    which is safe because no handler exists yet; the PERIODIC caller must pass
    ``False`` so hashes a live handler holds between claim and publish are
    skipped — reading a half-built flow would judge it against artifacts that
    are still being written.
    """
    if spool is not None:
        try:
            await asyncio.to_thread(spool.attempts_pass, now=_now(), boot=boot)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — recovery must never brick boot
            logger.exception("callback-attempts recovery pass failed")
    if _kick is not None:
        _kick.set()


async def _worker_pass() -> None:
    """One delivery pass: reconcile the ledger, dispatch every due nudge,
    convert removal records into operator notes, then recompute the wake.
    Each nudge is isolated so one failure never strands the rest."""
    global _next_due
    _pending_hints.clear()
    spool = _get_spool() if _get_spool is not None else None
    if spool is None:
        _next_due = None
        return
    try:
        await asyncio.to_thread(spool.attempts_pass, now=_now(), boot=False)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a bad plugin dir must not stop delivery
        logger.exception("callback-attempts pass failed")

    try:
        due = await asyncio.to_thread(_select_nudgeable, spool, _now())
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("callback-attempts snapshot failed")
        due = []

    for plugin, h, rec in due:
        try:
            await _run_nudge(spool, plugin, h, rec)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — isolate per attempt
            logger.exception("callback nudge failed unexpectedly (plugin=%s)",
                             plugin)

    await _process_removal_records(spool)

    try:
        _next_due = await asyncio.to_thread(_scan_next_due, spool)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("callback-attempts schedule scan failed")
        _next_due = None


def _wake_timeout() -> float | None:
    """The kick-wait timeout: the nearest due slot, floored at
    :data:`_MIN_WAKE_S`. ``None`` when nothing is scheduled — then the worker
    sleeps on the kick alone, which is what wakes it when a result lands."""
    if _next_due is None:
        return None
    return max(_MIN_WAKE_S, _next_due - _now())


async def _worker() -> None:
    while True:
        try:
            assert _kick is not None
            timeout = _wake_timeout()
            if timeout is None:
                await _kick.wait()
            else:
                try:
                    await asyncio.wait_for(_kick.wait(), timeout=timeout)
                except (asyncio.TimeoutError, TimeoutError):
                    pass          # the schedule, not a kick, is what fired
            _kick.clear()
            await _worker_pass()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the worker must survive anything
            logger.exception("callback-episodes worker pass failed")
            await _sleep(5.0)
            if _kick is not None:
                _kick.set()  # self re-kick: never strand a due attempt


# ---------------------------------------------------------------------------
# one nudge
# ---------------------------------------------------------------------------

async def _run_nudge(spool: Any, plugin: str, h: str, rec: dict) -> None:
    """Dispatch one due attempt's nudge and record what the bus said.

    Unresolvable registry ⇒ leave the schedule alone (transient — the same
    entry is due again next pass). No target ⇒ defer like a rejected pass and
    note ONCE per failure streak. Bus accept ⇒ spend a budget unit and
    advance the schedule; all-rejected ⇒ spend none and defer."""
    entry = _resolve(plugin)
    if entry is None:
        return
    role, instruction = _compose(entry, _message(plugin, h, rec))
    if role is None:
        await _defer(spool, plugin, h, rec)
        if not rec.get("deferrals"):
            await _note(
                f"Plugin {plugin}: an authorization delivery nudge could not "
                f"run ({instruction}). Ask the agent to collect handle {h} "
                "manually.")
        return
    ok = await _dispatch_with_retry(role, instruction, plugin, h)
    if ok:
        await _accept(spool, plugin, h, rec)
    else:
        await _defer(spool, plugin, h, rec)


async def _dispatch_with_retry(role: str, instruction: str, plugin: str,
                               h: str) -> bool:
    """The per-PASS attempt budget (never the six-dispatch nudge budget, which
    only bus ACCEPTS consume): a transient bus outage that exhausts this pass
    leaves the attempt due-but-deferred, and a later pass retries afresh."""
    tries = 0
    while tries < _MAX_DISPATCH_ATTEMPTS:
        tries += 1
        if _dispatch is not None:
            try:
                if await _dispatch(role, instruction,
                                   {"synthetic": "callback_nudge"}):
                    return True
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("callback nudge dispatch raised (plugin=%s)",
                                 plugin)
        if tries < _MAX_DISPATCH_ATTEMPTS:
            await _sleep(_RETRY_BACKOFF_S[min(tries - 1,
                                              len(_RETRY_BACKOFF_S) - 1)])
    return False


async def _accept(spool: Any, plugin: str, h: str, rec: dict) -> None:
    """Record a bus-ACCEPTED dispatch: one budget unit spent, the deferral
    streak reset, and the next slot computed — result-phase against the RESULT
    INODE'S MTIME (the durable publish clock), outcome-phase against
    ``ended_ts`` inside :func:`callback_attempts.next_nudge_after_accept`.

    A failed update is deliberately NOT retried here: the accept already
    happened, so the flow re-dispatches on the next pass. That is INV-CB-008's
    at-least-once boundary — one duplicate nudge, idempotent for a consumer
    whose collect against an emptied directory is a no-op."""
    now = _now()
    anchor = None
    if rec.get("status") == "result_ready":
        anchor = await asyncio.to_thread(spool.result_mtime, plugin, h)
    nxt = callback_attempts.next_nudge_after_accept(rec, now=now,
                                                    anchor_ts=anchor)
    nudges = int(rec.get("nudges", 0)) + 1
    ok = await asyncio.to_thread(
        spool.update_attempt_nudge, plugin, h,
        nudges=nudges, last_nudge_ts=now, next_nudge_ts=nxt, deferrals=0)
    if not ok:
        return
    if nudges >= callback_attempts.MAX_NUDGES and not rec.get("noted"):
        await _exhaustion_note(spool, plugin, h)


async def _defer(spool: Any, plugin: str, h: str, rec: dict) -> None:
    """A pass that ended with no bus accept: push ``next_nudge_ts`` forward on
    the escalating capped deferral and count the streak. The six-dispatch
    budget is UNTOUCHED (spec §8) — an unavailable bus must not consume the
    consumer's redelivery allowance — and the anchors never move."""
    await asyncio.to_thread(
        spool.update_attempt_nudge, plugin, h,
        next_nudge_ts=callback_attempts.next_nudge_after_reject(rec,
                                                                now=_now()),
        deferrals=int(rec.get("deferrals", 0)) + 1)


async def _exhaustion_note(spool: Any, plugin: str, h: str) -> None:
    """The budget-exhaustion note: MARK-then-notify (spec §8/§10).

    ``noted`` goes durable first, so a crash in the window loses one advisory
    note rather than crash-looping duplicates onto the operator — acceptable
    precisely because the unacked attempt file itself stays visible to the
    consumer until its ack or its retention bound. A failed mark therefore
    suppresses the note as well; there is nothing to retry against, the entry
    having spent its budget."""
    marked = await asyncio.to_thread(spool.update_attempt_nudge, plugin, h,
                                     noted=True)
    if not marked:
        logger.warning("callback-attempts: exhaustion mark failed; the "
                       "operator note is skipped (plugin=%s)", plugin)
        return
    await _note(f"Plugin {plugin}: the authorization delivery nudge for handle "
                f"{h} went unanswered after "
                f"{callback_attempts.MAX_NUDGES} attempts. The outcome is in "
                "the plugin's attempt list; ask the agent to read it.")


# ---------------------------------------------------------------------------
# removal records — NOTIFY-then-mark (at-least-once)
# ---------------------------------------------------------------------------

def _removal_text(rec: dict) -> str:
    plugin = rec.get("plugin")
    count = rec.get("count")
    why = ("was removed" if rec.get("reason") == "remove"
           else "had its leftover callback spool cleaned up")
    return (f"Plugin {plugin} {why} while {count} authorization "
            "flow(s) were still unsettled. Those authorizations were aborted "
            "and cannot be completed; re-run them after reinstalling if you "
            "still need them.")


async def _process_removal_records(spool: Any) -> None:
    """Turn every un-noted removal record into one operator note.

    Deliberately NOT routed through the failure-swallowing :func:`_note`
    seam (spec §10): delivery must be OBSERVED. A raised exception — or an
    absent/unconfigured notifier, which cannot have delivered anything —
    leaves the record un-noted for a later pass, and only a confirmed send
    marks it. Notify-then-mark is at-least-once on purpose: a crash in the
    window costs one duplicate DM for a rare event, whereas a lost note here
    would be silent once the record prunes. A note that SENT but whose mark
    then failed (False return or exception) is keyed in
    :data:`_removal_sent_unmarked` — later passes retry ONLY the mark
    (#532, Sol diff r1: with un-noted records never age-pruned, an ignored
    mark failure would otherwise resend the same DM every pass, forever,
    without a crash). A key clears only on a confirmed mark; in-memory on
    purpose, so a crash costs the documented one duplicate."""
    try:
        records = await asyncio.to_thread(spool.list_removal_records)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("callback removal records unreadable")
        records = []
    for filename, rec in records:
        if rec.get("noted"):
            _removal_sent_unmarked.discard(filename)
            continue
        if filename not in _removal_sent_unmarked:
            if _notify_operator is None:
                continue                  # undeliverable: leave it un-noted
            try:
                await _notify_operator(_removal_text(rec))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — un-noted, retried next pass
                logger.exception("callback removal note failed")
                continue
        try:
            marked = await asyncio.to_thread(spool.mark_removal_noted,
                                             filename, now=_now())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — sent; only the mark is retried
            logger.exception("callback removal record mark failed")
            marked = False
        if marked:
            _removal_sent_unmarked.discard(filename)
        else:
            _removal_sent_unmarked.add(filename)
    try:
        await asyncio.to_thread(spool.prune_removal_records, now=_now())
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("callback removal record prune failed")


# ---------------------------------------------------------------------------
# seams
# ---------------------------------------------------------------------------

def _resolve(plugin: str) -> dict | None:
    """Resolve the plugin's overlay entry. ``None`` (resolver absent, raised,
    or returned a non-dict) means "cannot resolve yet" — the caller LEAVES the
    schedule untouched and retries; it must never treat this as a confirmed
    removal."""
    if _resolve_registry_entry is None:
        return None
    try:
        entry = _resolve_registry_entry(plugin)
    except Exception:  # noqa: BLE001
        logger.exception("callback-episodes registry resolve failed (plugin=%s)",
                         plugin)
        return None
    return entry if isinstance(entry, dict) else None


async def _note(text: str) -> None:
    """Best-effort operator note — for the ADVISORY notes only (no-target,
    budget exhaustion), whose durable record is the attempt file itself. The
    removal note does NOT come through here: it needs observed delivery."""
    if _notify_operator is None:
        return
    try:
        await _notify_operator(text)
    except Exception:  # noqa: BLE001
        logger.exception("callback-episode operator note failed")
