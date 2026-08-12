"""Delivery worker for plugin-declared events — timed redelivery until ack.

An event wake carries no data — the emission is a pure "something happened"
signal, and the subscriber's own durable state is the real queue — so a
lost, suppressed, or duplicate wake costs promptness, never correctness
(the level-triggered discipline ``event_spool.py`` documents). This module
is casa's delivery half: when the spool's fold mints or refreshes a
subscriber's delivery record, this worker dispatches a fixed
**casa-authored, headless** turn asking the subscriber to process the event
and call ``ack_event`` when done, redelivering on a schedule until the ack
lands or the record's nudge budget (``event_attempts.MAX_NUDGES``) is spent.

Structurally the sibling of :mod:`callback_episodes` (read that module
whole before touching this one — its idioms, including the worker/wake-
timeout loop shape at ``callback_episodes.py:218/:325``, are mirrored here)
with two differences the event protocol requires:

* **No private per-flow ledger split (result-then-outcome).** An event
  delivery has one anchor (``minted_ts``) and one ladder
  (``event_attempts.PHASE_OFFSETS``) — there is no separate "result
  published" moment to switch schedules on.
* **A revalidating pre-send gate (spec decisions 27/29/30/36).** Unlike a
  callback (which grants no turn or memory access and so never re-checks
  consent at dispatch time), an event wake reaches into a subscriber
  role — so immediately before every dispatch attempt this worker
  recomputes the FULL consent identity from the LIVE resolved manifest
  (declaration digest, artifact_id, sorted targets) and requires it to
  match the routed snapshot AND still be present in the live ack store.
  The gate check and the dispatch call run under ONE asyncio
  **dispatch-admission lock** (:data:`DISPATCH_LOCK`, exported and shared
  with :func:`event_reconcile.revoke_and_unroute`) so a revocation can
  never race a dispatch already past its gate check.

**The ``resolve_registry_entry`` contract.** Unlike callback's
(``{"targets": [...]}``), this worker needs enough to recompute the full
consent identity, so the wiring closure must return
``{"targets": [...], "artifact_id": <str>, "manifest": <dict>} | None`` for
a resolvable subscriber — the live registry entry's targets, the live
resolved artifact id, and the live resolved plugin.json. ``None`` means
"cannot resolve yet" (transient — the caller leaves the schedule alone).

**Routing sentinel discipline (decision 26).** ``get_routed()`` returns
either the reconciler's published ``dict[(emitter, event),
dict[subscriber, snapshot]]`` or :data:`event_spool.ROUTING_UNAVAILABLE`.
Under the sentinel this worker does NO destructive work at all: the spool's
own ``sweep``/``fold_pass`` already degrade to a strict no-op (part-TTL
housekeeping only) under the sentinel, and this module additionally skips
the fold/due-scan/dispatch stages entirely that pass — queued emissions and
pending delivery records SURVIVE a transient reconcile-compute failure
untouched, and dispatch resumes in full the moment routing is available
again. ``get_registry_valid() is False`` degrades the SAME way even when
``routed`` itself still looks like a real mapping (Critical-2c): the
spool's sweep gates its destructive DROP/terminalize-as-removed logic on
``registry_valid`` too, because that logic keys on ``installed``, which is
only trustworthy while the registry snapshot is.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

import event_attempts
import event_reconcile
import event_spool
import plugin_dispatch

logger = logging.getLogger(__name__)

_MAX_DISPATCH_ATTEMPTS = 3
_RETRY_BACKOFF_S = (1.0, 5.0)
#: Floor on the timed wake — bounds how hot the worker can spin when a due
#: entry cannot be cleared, without making the schedule's own resolution
#: coarser than the ladder's shortest gap.
_MIN_WAKE_S = 5.0

# Serializes the pre-send gate check WITH the dispatch call, and is shared
# with event_reconcile.revoke_and_unroute's unroute-then-ack-delete
# transaction (decision 29/36): once a revoke completes, no dispatch for
# that pair can be admitted, because both paths hold this SAME lock across
# their whole read-then-act critical section. Exported (no leading
# underscore) — event_reconcile imports it directly (revoke_and_unroute);
# tools.py's event_ack_revoke reaches it only indirectly, by calling
# revoke_and_unroute, and never imports this module itself (Minor-8).
DISPATCH_LOCK = asyncio.Lock()

# Wired by casa_core at boot. All optional — absent seams degrade to
# logging, exactly like callback_episodes / plugin_setup_episodes.
_dispatch: "Callable[[str, str, dict], Awaitable[bool]] | None" = None
_resolve_registry_entry: "Callable[[str], Any] | None" = None
_get_routed: "Callable[[], Any] | None" = None
_get_installed: "Callable[[], set] | None" = None
_get_registry_valid: "Callable[[], bool] | None" = None
#: Every installed plugin that currently declares ``casa.emits`` (Critical-1)
#: — the worker's own provisioning worklist, wired from casa_core exactly
#: like ``_get_installed``. Absent (``None``, the un-configured default)
#: degrades to "provision nothing this pass", never an exception.
_get_emitters: "Callable[[], set] | None" = None
_get_acks: "Callable[[], Any] | None" = None
_get_spool: "Callable[[], Any] | None" = None
_notify_operator: "Callable[[str], Awaitable[None]] | None" = None
_sleep: "Callable[[float], Awaitable[None]]" = asyncio.sleep
#: Clock seam — injected in tests so the schedule is driven deterministically
#: without touching the shared ``asyncio.sleep`` (the memory-cage rule).
_now: "Callable[[], float]" = time.time

_worker_task: "asyncio.Task | None" = None
_kick: "asyncio.Event | None" = None

#: The nearest due ``next_nudge_ts`` seen by the last pass — the timed
#: wake's input. ``None`` means nothing is scheduled. Non-durable: the
#: durable schedule is in the ledger, and every pass recomputes this.
_next_due: "float | None" = None

# In-memory, non-durable request-path hints: kick() appends here (O(1)) and
# the worker drains it on the next pass. Correctness never depends on it —
# the delivery ledger is the backstop, so a hint lost to a crash converges
# on the next pass regardless.
_pending_hints: "set[tuple[str, str]]" = set()

# #532 (design r1-r3): sent-but-mark-failed keys. A notice whose SEND was
# confirmed but whose durable ``noted`` mark then failed must never be sent
# again — later scans retry ONLY the mark. A key clears ONLY on a confirmed
# successful mark (the enumeration APIs fail closed to empty, so "proven
# absence" is unprovable — a stale key for a vanished record lingers
# harmlessly: exhaustion keys carry ``gen`` and removal keys are uuid
# filenames, so no NEW record is ever blocked). In-memory on purpose: lost
# to a crash, costing at most one duplicate DM per crash (the contract).
_exhaustion_sent_unmarked: "set[tuple[str, str, str, int]]" = set()
_removal_sent_unmarked: "set[str]" = set()


def configure(*, dispatch, resolve_registry_entry, get_routed, get_installed,
              get_registry_valid, get_acks, get_spool, get_emitters=None,
              notify_operator=None, sleep=asyncio.sleep) -> None:
    """casa_core boot wiring. Idempotent. Every argument is a LIVE
    callable, re-invoked fresh on every pass/gate check — never a snapshot
    captured once (decisions 26/27/30). ``get_emitters`` is optional
    (defaults to ``None``, which provisions nothing) so existing callers/
    tests that predate Critical-1's provisioning fix keep working
    unchanged."""
    global _dispatch, _resolve_registry_entry, _get_routed, _get_installed
    global _get_registry_valid, _get_emitters, _get_acks, _get_spool
    global _notify_operator, _sleep, _kick, _next_due
    _dispatch = dispatch
    _resolve_registry_entry = resolve_registry_entry
    _get_routed = get_routed
    _get_installed = get_installed
    _get_registry_valid = get_registry_valid
    _get_emitters = get_emitters
    _get_acks = get_acks
    _get_spool = get_spool
    _notify_operator = notify_operator
    _sleep = sleep
    _next_due = None
    # #532: the sent-keys are per-wiring state (a reconfigure means a new
    # notify seam — a fresh boot's empty sets cost at most the documented
    # one-duplicate-per-crash, never a lost notice).
    _exhaustion_sent_unmarked.clear()
    _removal_sent_unmarked.clear()
    if _kick is None:
        _kick = asyncio.Event()


# ---------------------------------------------------------------------------
# kick — O(1), non-durable, no file I/O on the request path
# ---------------------------------------------------------------------------


def kick(emitter: str, event: str) -> None:
    """Signal the worker that one ``(emitter, event)`` pair may have new
    work (a fold just ran, a consent just landed). No spool I/O — the fold
    that preceded this already wrote whatever is durable; the hint only
    saves the worker a wait."""
    _pending_hints.add((emitter, event))
    if _kick is not None:
        _kick.set()


def kick_all() -> None:
    """Signal the worker broadly (the reconciler's own kick after every
    publish, success or fail-closed) — every pass scans everything
    regardless, so there is no hint to record here."""
    if _kick is not None:
        _kick.set()


# ---------------------------------------------------------------------------
# wake instruction (spec §5, exact wording)
# ---------------------------------------------------------------------------


def _wake_instruction(emitter: str, event: str, subscriber: str,
                      token: str, *, delegate_ack: bool = False) -> str:
    """The wake instruction. ``delegate_ack=True`` (Important-1) is for a
    SPECIALIST-only target (``plugin_dispatch.compose`` always resolves
    that to ``role="assistant"`` relaying a delegation instruction — the
    specialist itself never receives ``role`` here): a specialist is
    operator-installed content this repo does not ship or control, so it
    is never guaranteed ``casa-framework`` tool access. The specialist's
    own delegated task therefore does NOT ask it to call ``ack_event`` —
    :func:`_run_nudge` appends a SEPARATE, assistant-directed instruction
    (outside the text delegated verbatim to the specialist) telling the
    delegating agent to perform the ack itself once the specialist's turn
    completes.

    Internal minor #1: the ``delegate_ack=True`` text is wrapped in double
    quotes — ``plugin_dispatch.compose`` relays it VERBATIM inside
    ``"with the instruction: {base}"``, and :func:`_run_nudge` appends the
    assistant-directed ack postscript immediately after compose's own
    trailing "Do not substitute another agent." sentence. Without an
    explicit lexical fence around the delegated text, that postscript
    reads as one more sentence of the SAME instruction handed to the
    specialist rather than a directive aimed at the delegating agent."""
    base = (
        f"Plugin '{emitter}' emitted the event '{event}'. This is a "
        f"headless wake for '{subscriber}': process it through your tools "
        "now; if you need operator input, record it durably through your "
        "tools and end the turn — do not ask.")
    if delegate_ack:
        return (f'"{base} Do NOT call ack_event yourself — the agent '
                'delegating this task to you handles the ack."')
    # #534: the reminder-delivery convention (#511) — imperative, so
    # delivery is never left to the model's judgement; worst case on a
    # disobedient model is one stray message (fail-noisy), never a lost
    # result (anything operator-relevant goes through tools, per above).
    return (f"{base} When done, call "
            f"ack_event(emitter='{emitter}', event='{event}', token='{token}'). "
            "After the ack, output the sentinel `<silent/>` and nothing "
            "else — this is a background wake; any other closing text "
            "lands in the operator's chat.")


def _delegated_ack_postscript(emitter: str, event: str, token: str) -> str:
    """Appended AFTER ``plugin_dispatch.compose`` wraps the delegation
    instruction (never inside the text delegated verbatim to the
    specialist, which must never be asked to call a tool it may not have)
    — directs the DELEGATING agent (always 'assistant' here) to perform
    the ack itself once the specialist's turn is done.

    Internal minor #1: opens with an explicit "— end of delegated
    instruction." marker — the second half of the lexical fence around
    the quoted text :func:`_wake_instruction` returns for
    ``delegate_ack=True`` — so this postscript cannot be misread as part
    of what gets relayed to the specialist, regardless of how the
    surrounding sentences parse."""
    return (
        " — end of delegated instruction. Separately, after the "
        "specialist's turn completes, YOU (the delegating agent, not the "
        f"specialist) must call ack_event(emitter='{emitter}', "
        f"event='{event}', token='{token}') yourself — the specialist is "
        "not guaranteed casa-framework tool access, so the ack is your "
        "responsibility, not theirs. After the ack, output the sentinel "
        "`<silent/>` and nothing else — this is a background wake; any "
        "other closing text lands in the operator's chat.")


def _is_specialist_only_target(entry: dict) -> bool:
    """True when ``entry["targets"]`` names only ``specialist:*`` entries
    (no ``resident:*`` at all) — the exact shape under which
    ``plugin_dispatch.compose`` takes its delegation branch (always
    ``role="assistant"``, never the specialist directly; a resident
    target, if present, always wins first)."""
    targets = entry.get("targets") or []
    has_resident = any(isinstance(t, str) and t.startswith("resident:")
                       for t in targets)
    has_specialist = any(isinstance(t, str) and t.startswith("specialist:")
                         for t in targets)
    return has_specialist and not has_resident


def _wake_context(emitter: str, event: str) -> dict:
    return {"synthetic": "event_wake", "emitter": emitter, "event": event}


def _removal_text(rec: dict) -> str:
    plugin = rec.get("plugin")
    count = len(rec.get("entries") or [])
    return (f"Plugin {plugin} was removed (or had its leftover event spool "
            f"cleaned up) while {count} event delivery record(s) were still "
            "unsettled. Those deliveries were aborted and cannot be "
            "completed; re-consent after reinstalling if you still need "
            "them.")


# Target selection is the shared ``plugin_dispatch.compose`` (extracted so
# this and callback_episodes/plugin_setup_episodes's own copies can never
# drift apart in target ORDER).
_compose = plugin_dispatch.compose

# The spool-shape narrowing lives in ONE place (Minor-6, review round 1):
# ``event_reconcile.to_spool_shape`` — this used to carry its own private
# byte-identical copy, which is exactly the kind of divergence risk the
# module docstring warns about elsewhere in this codebase.
_to_spool_shape = event_reconcile.to_spool_shape


# ---------------------------------------------------------------------------
# selection — the nudgeable snapshot (blocking; runs off the loop)
# ---------------------------------------------------------------------------


def _is_nudgeable(rec: dict, now: float) -> bool:
    """Due (``next_nudge_ts`` set and reached), budget left, not a settled
    terminal record. A terminal record always carries ``next_nudge_ts ==
    None`` (``event_attempts.terminalize``), so the first gate alone
    already excludes it; the explicit status check is defensive."""
    nxt = rec.get("next_nudge_ts")
    if nxt is None or nxt > now:
        return False
    if rec.get("nudges", 0) >= event_attempts.MAX_NUDGES:
        return False
    if rec.get("status") == "done":
        return False
    return True


def _select_nudgeable(spool: Any, now: float) -> "list[tuple[str, str, str, dict]]":
    """Every due ``(emitter, event, subscriber, record)`` across every
    emitter, in a DETERMINISTIC (emitter, event, subscriber) order.
    Blocking (directory scans and stats) — always called through
    :func:`asyncio.to_thread`."""
    out: list = []
    for emitter in spool.emitters():
        for evt in spool.events(emitter):
            for subscriber, rec in sorted(spool.list_deliveries(
                    emitter, evt).items()):
                if _is_nudgeable(rec, now):
                    out.append((emitter, evt, subscriber, rec))
    return out


def _scan_next_due(spool: Any) -> "float | None":
    """The nearest ``next_nudge_ts`` that can still fire, or ``None`` —
    recomputed from the ledger AFTER the pass so a just-dispatched entry
    contributes its NEW slot. Blocking; via :func:`asyncio.to_thread`."""
    best: "float | None" = None
    for emitter in spool.emitters():
        for evt in spool.events(emitter):
            for _sub, rec in spool.list_deliveries(emitter, evt).items():
                nxt = rec.get("next_nudge_ts")
                if nxt is None:
                    continue
                if rec.get("nudges", 0) >= event_attempts.MAX_NUDGES:
                    continue
                if rec.get("status") == "done":
                    continue
                if best is None or nxt < best:
                    best = nxt
    return best


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def start_worker() -> None:
    """Boot seam: start the supervised delivery worker. The initial kick
    makes it run one pass immediately."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.get_running_loop().create_task(
        _worker(), name="event-episodes")
    if _kick is not None:
        _kick.set()


async def _provision_emitters(spool: Any) -> None:
    """Create ``<emitter>/{emissions,state,delivery}`` and publish
    ``ready.json`` for every installed plugin that currently declares
    ``casa.emits`` (Critical-1 fix): ``ensure_emitter_dirs``/``write_ready``
    had zero production callers before this, so a freshly-installed
    emitter's first ``emit()`` call raised ``FileNotFoundError``. This is
    non-destructive housekeeping (creates/repairs only — never deletes or
    unroutes anything), so unlike fold/dispatch it is allowed to run even
    under the :data:`event_spool.ROUTING_UNAVAILABLE` sentinel (decision 26
    only bounds DESTRUCTIVE action). Skips an emitter already carrying a
    valid ready marker, so a steady state costs one cheap read per pass, not
    a full mkdir+fsync-backed republish. One bad emitter's enumeration or
    provisioning failure never stops another's."""
    if _get_emitters is None:
        return
    try:
        emitters = set(_get_emitters() or ())
    except Exception:  # noqa: BLE001 — a broken enumeration must not abort
        # the pass it was only meant to prime
        logger.exception("event-episodes emitter enumeration failed")
        return
    for emitter in sorted(emitters):
        try:
            marker = await asyncio.to_thread(spool.read_marker, emitter)
            if marker.state is event_spool.MarkerState.PRESENT:
                continue
            await asyncio.to_thread(spool.ensure_emitter_dirs, emitter)
            await asyncio.to_thread(spool.write_ready, emitter, {"v": 1})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad emitter must never stop
            # provisioning the rest
            logger.exception(
                "event-episodes emitter provisioning failed (emitter=%s)",
                emitter)


async def recovery(*, boot: bool = True) -> None:
    """Boot/periodic recovery pass: reconcile the fold + delivery ledger
    against the artifacts via the SAME configured closures the worker pass
    uses (no extra params). Never dispatches — that is the worker's job —
    so casa_core can run it before the worker starts. Never raises.

    ``boot=True`` (the default, for the boot seam) is the trustworthy full
    reconstruction pass; a periodic caller passes ``False`` (mirrors
    ``EventSpool.recovery_pass``'s own ``boot`` parameter, which gates the
    orphan-dir GC)."""
    spool = _get_spool() if _get_spool is not None else None
    if spool is not None:
        await _provision_emitters(spool)
        routed = (_get_routed() if _get_routed is not None
                 else event_spool.ROUTING_UNAVAILABLE)
        installed = set(_get_installed() or ()) if _get_installed is not None \
            else set()
        registry_valid = bool(_get_registry_valid()) \
            if _get_registry_valid is not None else False
        try:
            await asyncio.to_thread(
                spool.recovery_pass, _to_spool_shape(routed), installed,
                registry_valid, _now(), boot=boot)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — recovery must never brick boot
            logger.exception("event-episodes recovery pass failed")
    if _kick is not None:
        _kick.set()


async def _worker_pass() -> None:
    """One delivery pass: sweep -> notice scans -> fold -> due-scan ->
    dispatch -> notice scans again -> recompute the wake. Under
    :data:`event_spool.ROUTING_UNAVAILABLE` this does part-TTL housekeeping
    AND the notice scans ONLY (design R3-1: the removal and
    unnoted-exhaustion scans are notify/mark-only — they read settled
    records and flip ``noted``, no fold/dispatch/destructive action, so
    decision 26's sentinel, which bounds DESTRUCTIVE and forward-moving
    work, deliberately does not gate them: an owed operator notice never
    waits on routing health) and skips fold/due-scan/dispatch entirely —
    no destructive action of any kind runs against an unauthoritative
    routed view."""
    global _next_due
    _pending_hints.clear()
    spool = _get_spool() if _get_spool is not None else None
    if spool is None:
        _next_due = None
        return

    await _provision_emitters(spool)

    routed = _get_routed() if _get_routed is not None \
        else event_spool.ROUTING_UNAVAILABLE
    spool_routed = _to_spool_shape(routed)
    installed = set(_get_installed() or ()) if _get_installed is not None \
        else set()
    registry_valid = bool(_get_registry_valid()) \
        if _get_registry_valid is not None else False
    now = _now()

    try:
        await asyncio.to_thread(spool.sweep, spool_routed, installed,
                                registry_valid, now)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — one bad emitter must not stop the pass
        logger.exception("event-spool sweep failed")

    # Notice scans run BEFORE the sentinel gate (R3-1) so a boot pass that
    # finds routing unavailable still delivers the notices already owed.
    await _process_removal_records(spool)
    await _process_unnoted_exhaustions(spool)

    if routed is event_spool.ROUTING_UNAVAILABLE or not registry_valid:
        # No fold, no due-scan, no dispatch — the sentinel (or an invalid
        # registry snapshot, Critical-2(c): `installed` is untrustworthy
        # under it, exactly like the spool's own sweep gate above)
        # authorizes nothing destructive OR forward-moving (decision 26).
        _next_due = None
        return

    try:
        await asyncio.to_thread(spool.fold_pass, spool_routed, now)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("event-spool fold failed")

    try:
        due = await asyncio.to_thread(_select_nudgeable, spool, _now())
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("event-episodes snapshot failed")
        due = []

    for emitter, evt, subscriber, rec in due:
        try:
            await _run_nudge(emitter, evt, subscriber, rec)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — isolate per record
            logger.exception(
                "event nudge failed unexpectedly (emitter=%s event=%s)",
                emitter, evt)

    # Second exhaustion scan: an exhaustion minted by THIS pass's dispatch
    # loop is noticed this pass when the channel is up (design R3-1).
    await _process_unnoted_exhaustions(spool)

    try:
        _next_due = await asyncio.to_thread(_scan_next_due, spool)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("event-episodes schedule scan failed")
        _next_due = None


def _wake_timeout() -> "float | None":
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
            logger.exception("event-episodes worker pass failed")
            await _sleep(5.0)
            if _kick is not None:
                _kick.set()   # self re-kick: never strand a due record


# ---------------------------------------------------------------------------
# pre-send gate + one nudge
# ---------------------------------------------------------------------------


def _resolve(subscriber: str) -> "dict | None":
    """Resolve the subscriber's live registry entry. ``None`` (resolver
    absent, raised, or a non-dict) means "cannot resolve yet" — the caller
    leaves the schedule untouched and retries; never a confirmed removal."""
    if _resolve_registry_entry is None:
        return None
    try:
        entry = _resolve_registry_entry(subscriber)
    except Exception:  # noqa: BLE001
        logger.exception(
            "event-episodes registry resolve failed (subscriber=%s)",
            subscriber)
        return None
    return entry if isinstance(entry, dict) else None


def _gate_ok(emitter: str, event: str, subscriber: str, rec: dict,
            entry: dict, emitter_entry: "dict | None") -> bool:
    """The pre-send identity gate (decisions 27/29/30/36): the pair must
    still be in the LIVE authoritative routed map, AND the consent
    identity recomputed from the LIVE resolved manifest's declaration
    digest + live artifact_id + live sorted targets must equal the routed
    snapshot's, AND that identity must still be present in the live ack
    store, AND (SOL-P2b) the EMITTER side of the pair must still resolve
    AND still declare this exact event — the routed snapshot can survive
    stale for a whole pass after the emitter is uninstalled or upgraded
    to drop the declaration, since ``routed`` itself only refreshes on
    the NEXT reconcile.

    *entry* and *emitter_entry* are the subscriber's and the emitter's
    LIVE resolved registry entries — resolved by the caller ONCE per
    dispatch attempt, BEFORE :data:`DISPATCH_LOCK` is acquired (internal
    minor #3: ``resolve_registry_entry`` reads a plugin.json off disk,
    synchronously, and must never block another task's dispatch/revoke
    while holding the lock, up to :data:`_MAX_DISPATCH_ATTEMPTS` times a
    nudge). This function itself performs no resolve I/O — only the
    cheap live map/ack-store checks below — and MUST be called with the
    lock already held, so a revoke can never land between the resolve
    and the checks it grounds."""
    import plugin_store
    from plugin_events import ack_identity

    routed = _get_routed() if _get_routed is not None \
        else event_spool.ROUTING_UNAVAILABLE
    if routed is event_spool.ROUTING_UNAVAILABLE:
        return False
    snapshot = (routed.get((emitter, event)) or {}).get(subscriber)
    if snapshot is None:
        return False

    manifest = entry.get("manifest")
    if not isinstance(manifest, dict):
        return False
    try:
        subs = plugin_store.manifest_subscribes(manifest, subscriber)
    except Exception:  # noqa: BLE001 — a live declaration that no longer
        # parses can never pass the gate.
        return False
    match = next((s for s in subs if s.get("plugin") == emitter
                 and s.get("event") == event), None)
    if match is None:
        return False
    digest = match.get("digest")
    artifact_id = entry.get("artifact_id")
    if not isinstance(digest, str) or not isinstance(artifact_id, str) \
            or not artifact_id:
        return False
    targets = event_reconcile.normalize_targets(entry.get("targets"))
    live_identity = ack_identity(subscriber, artifact_id, emitter, event,
                                 digest, targets)
    if live_identity != snapshot.get("ack_identity"):
        return False

    # SOL-P2b: the EMITTER side of the pair, resolved the SAME way the
    # subscriber side is — an unresolvable emitter (uninstalled) or one
    # whose live manifest no longer declares this event (a routine
    # upgrade that dropped it) must refuse the send exactly like a stale
    # subscriber-side identity does, never dispatch against a declaration
    # that is no longer live.
    if emitter_entry is None:
        return False
    emitter_manifest = emitter_entry.get("manifest")
    if not isinstance(emitter_manifest, dict):
        return False
    try:
        emits = plugin_store.manifest_emits(emitter_manifest, emitter)
    except Exception:  # noqa: BLE001 — a live EMITTER declaration that no
        # longer parses can never pass the gate either.
        return False
    if event not in {e.get("declared") for e in emits}:
        return False

    acks = _get_acks() if _get_acks is not None else None
    if acks is None:
        return False
    if acks.get(live_identity) is None:
        return False
    return True


async def _dispatch_admitted(role: str, instruction: str, context: dict,
                             emitter: str, event: str, subscriber: str,
                             rec: dict) -> "bool | None":
    """One admission attempt: resolve the subscriber AND the emitter's
    LIVE registry entries FIRST — outside :data:`DISPATCH_LOCK` (internal
    minor #3) — then run the gate check AND the dispatch call BOTH under
    the lock, so a revoke can never complete between them. Re-resolving
    both once per ATTEMPT (not once per whole nudge) preserves the
    gate's per-attempt live-read semantics while keeping the blocking
    resolve I/O off the lock. Returns ``True`` (bus accepted), ``False``
    (bus rejected — retry), or ``None`` (gate failed, including an
    unresolvable subscriber this attempt — stop retrying, defer with no
    budget spent)."""
    sub_entry = _resolve(subscriber)
    emitter_entry = _resolve(emitter)
    async with DISPATCH_LOCK:
        if sub_entry is None or not _gate_ok(
                emitter, event, subscriber, rec, sub_entry, emitter_entry):
            return None
        if _dispatch is None:
            return False
        try:
            return bool(await _dispatch(role, instruction, context))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception(
                "event nudge dispatch raised (emitter=%s event=%s)",
                emitter, event)
            return False


def _kick_reconcile() -> None:
    """Lazily-imported (avoids an event_episodes<->event_reconcile import
    cycle): schedule a reconcile pass so a pre-send mismatch (a stale
    snapshot, a revoked ack) resolves promptly instead of waiting for the
    next lifecycle mutation."""
    try:
        import event_reconcile
        event_reconcile.kick()
    except Exception:  # noqa: BLE001 — a background kick must never raise
        logger.exception("event reconcile kick (pre-send gate) failed")


async def _run_nudge(emitter: str, event: str, subscriber: str,
                     rec: dict) -> None:
    """Dispatch one due record's nudge and record what the bus (or the
    gate) said. Unresolvable registry ⇒ leave the schedule alone
    (transient). No target ⇒ defer and note once per streak. A gate
    failure on the FIRST attempt stops retrying immediately (there is
    nothing to retry against — the pair or the consent is gone) and kicks
    a reconcile. Bus accept ⇒ spend budget; all-rejected ⇒ defer, no
    budget spent."""
    entry = _resolve(subscriber)
    if entry is None:
        return
    delegate_ack = _is_specialist_only_target(entry)
    instruction = _wake_instruction(emitter, event, subscriber,
                                    rec["ack_token"], delegate_ack=delegate_ack)
    role, composed = _compose(entry, instruction)
    if delegate_ack and role is not None:
        # Important-1: the text just delegated to the specialist never asks
        # IT to call ack_event (see _wake_instruction's delegate_ack
        # branch) — this postscript is directed at the DELEGATING agent
        # (role is always "assistant" for a specialist-only target) and
        # sits OUTSIDE what plugin_dispatch.compose wrapped as the
        # specialist's own verbatim task text.
        composed += _delegated_ack_postscript(emitter, event, rec["ack_token"])
    context = _wake_context(emitter, event)
    if role is None:
        await _defer(emitter, event, subscriber, rec)
        if not rec.get("deferrals"):
            await _note(
                f"Subscriber '{subscriber}': an event delivery nudge for "
                f"'{event}' from '{emitter}' could not run ({composed}). "
                "Ask the agent to process it manually and call ack_event.")
        return

    tries = 0
    gate_failed = False
    accepted = False
    while tries < _MAX_DISPATCH_ATTEMPTS:
        tries += 1
        result = await _dispatch_admitted(
            role, composed, context, emitter, event, subscriber, rec)
        if result is None:
            gate_failed = True
            break
        if result:
            accepted = True
            break
        if tries < _MAX_DISPATCH_ATTEMPTS:
            await _sleep(_RETRY_BACKOFF_S[
                min(tries - 1, len(_RETRY_BACKOFF_S) - 1)])

    if gate_failed:
        await _defer(emitter, event, subscriber, rec)
        _kick_reconcile()
        return
    if accepted:
        await _accept(emitter, event, subscriber, rec)
    else:
        await _defer(emitter, event, subscriber, rec)


async def _accept(emitter: str, event: str, subscriber: str,
                  rec: dict) -> None:
    """Record a bus-ACCEPTED dispatch: one budget unit spent and the next
    slot computed off the ladder, OR — the last unit — a single ATOMIC
    ``done/exhausted/noted=false`` transition (#532: terminalize UN-noted;
    the worker pass's unnoted-exhaustion scan owns the notice,
    notify-after-mark at-least-once — pre-fix this wrote ``noted=true``
    and sent through the failure-swallowing ``_note``, so a notice
    dropped in the boot bring-up window was lost forever). A False
    return (a concurrent ack raced this write) is skipped silently — the
    record stays acked, exactly as it should."""
    spool = _get_spool() if _get_spool is not None else None
    if spool is None:
        return
    now = _now()
    nxt = event_attempts.next_nudge_after_accept(rec, now=now)
    nudges = rec.get("nudges", 0) + 1

    if nxt is None:
        def _exhaust(r: dict) -> dict:
            done = event_attempts.terminalize(r, "exhausted", now=now)
            done["nudges"] = nudges
            done["last_nudge_ts"] = now
            done["deferrals"] = 0
            done["noted"] = False
            return done

        await asyncio.to_thread(
            spool.update_delivery_nudge, emitter, event, subscriber,
            rec["gen"], _exhaust)
        return

    def _advance(r: dict) -> dict:
        r["nudges"] = nudges
        r["last_nudge_ts"] = now
        r["next_nudge_ts"] = nxt
        r["deferrals"] = 0
        return r

    await asyncio.to_thread(
        spool.update_delivery_nudge, emitter, event, subscriber, rec["gen"],
        _advance)


async def _defer(emitter: str, event: str, subscriber: str,
                 rec: dict) -> None:
    """A pass with no bus accept (or a failed gate): push
    ``next_nudge_ts`` forward on the escalating capped deferral, spending
    NO budget — an unavailable bus (or a stale gate) must not consume the
    subscriber's redelivery allowance."""
    spool = _get_spool() if _get_spool is not None else None
    if spool is None:
        return
    now = _now()
    nxt = event_attempts.next_nudge_after_reject(rec, now=now)
    deferrals = rec.get("deferrals", 0) + 1

    def _mutator(r: dict) -> dict:
        r["next_nudge_ts"] = nxt
        r["deferrals"] = deferrals
        return r

    await asyncio.to_thread(
        spool.update_delivery_nudge, emitter, event, subscriber, rec["gen"],
        _mutator)


# ---------------------------------------------------------------------------
# notice scans — NOTIFY-then-mark (at-least-once, duplicates bounded by the
# sent-but-mark-failed keys; see the module-state comment on those sets)
# ---------------------------------------------------------------------------


async def _process_removal_records(spool: Any) -> None:
    """Turn every un-noted removal record into one operator note. NOT
    routed through the failure-swallowing :func:`_note` seam — delivery
    must be OBSERVED; only a confirmed send marks the record. A record
    whose send succeeded but whose mark failed is keyed in
    :data:`_removal_sent_unmarked`: later passes retry ONLY the mark
    (design r2: without the key, every 5-minute recovery kick would
    re-send the same DM for as long as the mark keeps failing)."""
    try:
        records = await asyncio.to_thread(spool.list_removal_records)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("event removal records unreadable")
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
                logger.exception("event removal note failed")
                continue
        try:
            marked = await asyncio.to_thread(spool.mark_removal_noted,
                                             filename, now=_now())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — sent; only the mark is retried
            logger.exception("event removal record mark failed")
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
        logger.exception("event removal record prune failed")


def _exhaustion_text(emitter: str, event: str, subscriber: str) -> str:
    return (f"Subscriber '{subscriber}': the event delivery nudge for "
            f"'{event}' from '{emitter}' went unanswered after "
            f"{event_attempts.MAX_NUDGES} attempts. Ask the agent to "
            "check its pending event deliveries.")


def _select_unnoted_exhaustions(spool: Any) -> "list[tuple[str, str, str, int]]":
    """Every ``done/exhausted/noted=False`` delivery record, deterministic
    order. Blocking (directory scans) — called via ``asyncio.to_thread``."""
    out: list = []
    for emitter in spool.emitters():
        for evt in spool.events(emitter):
            for subscriber, rec in sorted(spool.list_deliveries(
                    emitter, evt).items()):
                if (rec.get("status") == "done"
                        and rec.get("outcome") == "exhausted"
                        and rec.get("noted") is False):
                    out.append((emitter, evt, subscriber, rec["gen"]))
    return out


async def _process_unnoted_exhaustions(spool: Any) -> None:
    """#532: deliver the budget-exhaustion notice for every terminalized
    record still owing one, notify-after-mark: ``_accept`` terminalizes
    ``noted=False``; only a CONFIRMED send flips ``noted`` (via
    ``mark_delivery_noted``, the done-record carve-out), retried every
    pass until it lands. A send-succeeded-mark-failed record is keyed in
    :data:`_exhaustion_sent_unmarked` and only the mark is retried."""
    try:
        owed = await asyncio.to_thread(_select_unnoted_exhaustions, spool)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("event exhaustion scan failed")
        owed = []
    for emitter, evt, subscriber, gen in owed:
        key = (emitter, evt, subscriber, gen)
        if key not in _exhaustion_sent_unmarked:
            if _notify_operator is None:
                continue                  # undeliverable: stays owed
            try:
                await _notify_operator(_exhaustion_text(emitter, evt,
                                                        subscriber))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — un-noted, retried next pass
                logger.exception("event exhaustion note failed")
                continue
        try:
            marked = await asyncio.to_thread(
                spool.mark_delivery_noted, emitter, evt, subscriber, gen)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — sent; only the mark is retried
            logger.exception("event exhaustion mark failed")
            marked = False
        if marked:
            _exhaustion_sent_unmarked.discard(key)
        else:
            _exhaustion_sent_unmarked.add(key)


async def _note(text: str) -> None:
    """Best-effort operator note — for the ADVISORY no-target note only
    (its durable backstop is the ``event_no_target`` health row). The
    removal AND exhaustion notes do NOT come through here: they need
    observed delivery (#532)."""
    if _notify_operator is None:
        return
    try:
        await _notify_operator(text)
    except Exception:  # noqa: BLE001
        logger.exception("event-episode operator note failed")
