"""Durable pending-ask records for scheduled ``ask_user`` questions (#573).

A resident's time-based trigger turn can raise a button question in the
operator's DM. That question outlives the turn that asked it by design, so —
unlike an operator's own ``ask_user`` — it cannot live only in the in-memory
:mod:`verdict_broker`: a restart would leave a tappable keyboard on screen
with nothing behind it, and the scheduled session would never learn anything.

This module owns three things:

1. **The durable record** (``/data/scheduled_asks.json``) and its state
   machine. Each transition is persisted BEFORE the action it authorizes:

   ``posting``   written before the keyboard is posted — a keyboard may or may
                 not exist, and no ``message_id`` is known yet.
   ``live``      written once the ``message_id`` is known: the broker request
                 owns a real keyboard.
   ``settling``  written before the FIRST terminal action (the keyboard edit),
                 so a crash can never replay a dispatch.

   The record is deleted once a terminal outcome has been dispatched. Deletion
   is not the acknowledgement — ``settling`` is: the crash window between
   "decided" and "dispatched" resolves toward AT-MOST-ONCE, because a
   duplicated answer makes the resident act twice on one confirmation, while a
   lost one leaves an unanswered question in a session that keeps working.

2. **The single-owner finish hook** for a scheduled ask — the one place that
   edits the keyboard, dispatches the terminal continuation back into the
   scheduled session, and drops the record. Built here rather than in
   ``tools.ask_user`` because the boot reconciler has to rebuild the identical
   hook for a record it restores.

3. **Trigger lifecycle revocation**: a per-role epoch stamped into the firing
   context, plus the cancellation of pending asks when a trigger is removed or
   rewritten. Both are driven from the EVENT LOOP by the callers of
   ``TriggerRegistry.reregister_for`` — never from inside the registry, whose
   ``_unwind_role`` runs in a worker thread where the broker's ``_finish``
   would raise on ``get_running_loop()`` having already popped the request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Callable, Coroutine

from atomic_io import PRIVATE, atomic_write_json

logger = logging.getLogger(__name__)

NAMESPACE = "resident_ask"

STATE_POSTING = "posting"
STATE_LIVE = "live"
STATE_SETTLING = "settling"

# Which states a transition may overwrite. A finish hook that fires before
# ``ask_user`` has recorded the message id must not be undone by that later
# write, and nothing may move a settling record back.
_ALLOWED_FROM: dict[str, tuple[str, ...]] = {
    STATE_LIVE: (STATE_POSTING,),
    STATE_SETTLING: (STATE_POSTING, STATE_LIVE),
}


class ScheduledAskStore:
    """JSON-on-disk records keyed by request id.

    One :class:`asyncio.Lock` serialises every mutate+save (the same
    discipline as :class:`session_registry.SessionRegistry`), and the write
    itself is offloaded to a thread. A corrupt or unreadable file is
    quarantined and the store starts empty: losing pending questions is
    recoverable — every affected session simply never hears back — while a
    boot crash-stop is not.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._data: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if not isinstance(loaded, dict):
                    raise ValueError(f"expected dict, got {type(loaded).__name__}")
                self._data = {
                    k: v for k, v in loaded.items() if isinstance(v, dict)
                }
            except (json.JSONDecodeError, OSError, ValueError):
                logger.error(
                    "scheduled_asks.json is corrupt or unreadable; moving it "
                    "to %s.corrupt and starting empty", path,
                )
                try:
                    os.replace(path, f"{path}.corrupt")
                except OSError:
                    pass
                self._data = {}

    # -- reads (synchronous, in-memory) ------------------------------------
    #
    # Deliberately only `get` and `all`, and `all` has ONE caller: the boot
    # reconciler. There is no by-role / by-chat query, because a live decision
    # must never be answered from here — the store is written after an await
    # and lags the broker, which is the authority for what is live. A query
    # method is the invitation to forget that, so there isn't one.

    def get(self, rid: str) -> dict[str, Any] | None:
        rec = self._data.get(rid)
        return dict(rec) if rec is not None else None

    def all(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._data.values()]

    # -- writes -------------------------------------------------------------

    async def put(self, record: dict[str, Any]) -> None:
        async with self._lock:
            self._data[record["rid"]] = dict(record)
            await self._save_locked()

    async def set_state(self, rid: str, state: str, **fields: Any) -> bool:
        """Compare-and-set the record's state. Returns True iff it moved.

        The guard is what keeps the two writers honest: ``ask_user`` stamping
        ``live`` after the post can race a finish hook that already settled
        the request (an immediate supersede), and a resurrected record would
        be re-registered at the next boot with no broker request behind it.
        """
        async with self._lock:
            rec = self._data.get(rid)
            if rec is None:
                return False
            if rec.get("state") not in _ALLOWED_FROM.get(state, ()):
                return False
            rec["state"] = state
            rec.update(fields)
            await self._save_locked()
            return True

    async def drop(self, rid: str) -> None:
        async with self._lock:
            if self._data.pop(rid, None) is not None:
                await self._save_locked()

    async def _save_locked(self) -> None:
        await asyncio.to_thread(
            lambda: atomic_write_json(self._path, self._data, mode=PRIVATE),
        )


# Process-wide singleton, installed at boot by casa_core. ``None`` until then:
# every consumer treats an uninitialised store as "no scheduled asks", which is
# what a deploy without /data (tests, tooling) should see.
STORE: ScheduledAskStore | None = None


def init_store(path: str) -> ScheduledAskStore:
    global STORE
    STORE = ScheduledAskStore(path)
    return STORE


# ---------------------------------------------------------------------------
# trigger-lifecycle epochs
# ---------------------------------------------------------------------------

# Process-local, never persisted: an epoch is minted and consumed inside one
# process lifetime (it guards an ask created by a turn that is already
# running), and a persisted counter could only re-open the staleness it exists
# to close.
#
# TWO counters, and the pair is the point. A role-wide one, bumped when a
# role's whole trigger set is replaced, and a per-trigger one, bumped when a
# single named trigger goes. A single role-wide counter made `revoke_trigger`
# refuse the in-flight asks of every OTHER trigger of that role — the same
# over-broad selector that had to be fixed at boot, one level along (Terra, S2).
_ROLE_EPOCHS: dict[str, int] = {}
_TRIGGER_EPOCHS: dict[tuple[str, str], int] = {}


def epoch_for(role: str, label: str) -> str:
    """The trigger-lifecycle epoch a firing turn is stamped with.

    ``label`` is the trigger's session label (``f"{type}-{name}"``), which both
    dispatch sites already build for ``chat_id``. Opaque to every consumer:
    they only ever compare it for equality.
    """
    return (f"{_ROLE_EPOCHS.get(role, 0)}:"
            f"{_TRIGGER_EPOCHS.get((role, label), 0)}")


def bump_role_epoch(role: str) -> None:
    """The whole trigger set for *role* was replaced."""
    _ROLE_EPOCHS[role] = _ROLE_EPOCHS.get(role, 0) + 1


def bump_trigger_epochs(role: str, labels: "set[str]") -> None:
    """One named trigger of *role* went; every other trigger stays current."""
    for label in labels:
        key = (role, label)
        _TRIGGER_EPOCHS[key] = _TRIGGER_EPOCHS.get(key, 0) + 1


def epoch_is_current(role: str, label: str, stamped: Any) -> bool:
    """Whether an origin's stamped epoch still names the live trigger set.

    Absence is tolerated (returns True): a delegation-completion turn copies
    ``_scheduled_delivery`` from a persisted origin and carries no epoch. That
    derived path stays covered by :func:`revoke_role`, which cancels pending
    asks outright.

    A terminal continuation is NOT such a path: it carries the epoch of the
    turn that ASKED, replayed verbatim from the durable record, never the
    epoch resolved at finish time. Resolving it late would bless a revoked
    trigger's continuation with the very epoch the revocation just minted —
    the tap commits, the revocation finds nothing live to cancel, and the
    continuation would then be free to raise a fresh question.
    """
    if stamped is None:
        return True
    if not isinstance(stamped, str):
        return False
    return stamped == epoch_for(role, label)


# the single-owner finish hook
# ---------------------------------------------------------------------------

def _expired_body(body: str) -> str:
    return f"{body}\n\n(this question has expired)"


def _terminal_text(rid: str, kind: str, reason: str | None, chosen: str | None) -> str:
    if kind == "answered":
        return f"[answer to {rid}] the operator tapped: {chosen}"
    if kind == "no_answer":
        return f"[no answer to {rid}] the question expired without an answer"
    return (
        f"[no answer to {rid}] the question was cancelled "
        f"({reason or kind or 'unknown'})"
    )


async def _settle(
    channel: Any, rec: dict, *, kind: str, reason: str | None,
    chosen: str | None, edit_text: str | None,
) -> None:
    """Persist ``settling``, edit the keyboard, dispatch, drop.

    The state write comes FIRST — before the edit, not between the edit and
    the dispatch — because an edited keyboard with a record still reading
    ``live`` would be restored at the next boot as an answerable question
    whose buttons are gone, and settled a second time as ``no_answer``.
    """
    rid = rec["rid"]
    store = STORE
    if store is not None and not await store.set_state(rid, STATE_SETTLING):
        return  # another finisher owns it (or it is already gone)
    message_id = rec.get("message_id")
    if edit_text is not None and message_id is not None:
        try:
            await channel.edit_dm_message(rec["chat_id"], message_id, edit_text)
        except Exception:  # noqa: BLE001 — an edit failure must not strand the session
            logger.warning("scheduled ask %s: keyboard edit failed", rid,
                           exc_info=True)
    text = _terminal_text(rid, kind, reason, chosen)
    try:
        ok = await channel._dispatch_scheduled_continuation(
            session_scope=rec["session_scope"], target_role=rec["role"],
            request_id=rid, text=text, epoch=rec.get("epoch"),
        )
    except Exception:  # noqa: BLE001
        ok = False
        logger.warning("scheduled ask %s: continuation dispatch raised", rid,
                       exc_info=True)
    if not ok:
        logger.warning(
            "scheduled ask %s: terminal outcome %r never reached session %s "
            "for role %s", rid, kind, rec.get("session_scope"), rec.get("role"),
        )
    if store is not None:
        await store.drop(rid)


def make_finish_hook(
    channel: Any, rec: dict,
) -> Callable[[dict], Coroutine[Any, Any, None]]:
    """The broker finish hook for one scheduled ask.

    Single owner of everything that happens after a terminal outcome: the
    keyboard edit, the continuation back into the SCHEDULED session, and the
    durable record. Unlike the operator-DM ask (``tools.ask_user``), EVERY
    terminal outcome dispatches — an unanswered scheduled question that told
    nobody leaves its session waiting forever.
    """
    body = rec.get("body") or ""
    options = list(rec.get("options") or [])

    async def _finish(outcome: dict) -> None:
        kind = outcome.get("outcome")
        reason = outcome.get("reason")
        if kind == "cancelled" and reason == "casa_shutdown":
            # Casa is going down with the keyboard still on screen and the
            # record still `live`; the boot reconcile owns it. Editing or
            # dispatching here would settle a question the next boot can
            # legitimately restore.
            return
        chosen = None
        edit_text = _expired_body(body)
        if kind == "answered":
            idx = outcome.get("option_index")
            if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(options):
                chosen = options[idx]
                edit_text = f"{body}\n\nAnswered: {chosen}"
            else:  # pragma: no cover — the broker range-checks before commit
                kind = "cancelled"
                reason = "invalid_option"
        await _settle(channel, rec, kind=kind, reason=reason, chosen=chosen,
                      edit_text=edit_text)

    return _finish


# ---------------------------------------------------------------------------
# revocation — SYNCHRONOUS, broker-authoritative
# ---------------------------------------------------------------------------
#
# Every one of these decides and acts inside ONE no-await block against the
# broker's live map. None of them reads the durable store: the store is
# written after an await, so a store-driven scan can always miss an ask that
# has already won its lane, and three separate S1s in design review were that
# one shape. The store is a recovery log; the broker is the authority.


def _is_scheduled(req: Any) -> bool:
    return req.meta.get("scheduled") is True


# The one thing the broker cannot answer: from process start until
# ``reconcile_at_boot`` runs, records sit on disk and the live map is empty, so
# a revocation in that window cancels nothing. Each revocation therefore leaves
# a process-local MARKER of what it revoked, and the reconciler settles a record
# that matches one instead of restoring it. This is not the deleted store scan
# in another coat: markers are in-memory, written by the same synchronous call
# that decides, and read by exactly one consumer, once — the reconciler clears
# them when it finishes, after which the broker is authoritative again.
#
# The markers carry the SELECTOR each revocation actually used. An earlier
# version keyed this on the role epoch, which `revoke_trigger` bumps role-wide:
# cancelling one reminder then discarded every other question that role was
# waiting on (Sol + Terra, both S2). The epoch keeps its own, separate job —
# refusing an ask from a turn that is still running under the old trigger set.
_BOOT_REVOCATIONS: list[dict] = []
_BOOT_RECONCILED = False

# Belt-and-braces bound. On a deploy with no Telegram channel the reconciler is
# never scheduled, so nothing would ever flip the flag below; such a deploy also
# has no scheduled asks at all, which makes every marker there inert — but an
# unbounded list fed by every reload is still a leak.
_BOOT_REVOCATIONS_MAX = 256


def _note_boot_revocation(**selector: Any) -> None:
    if _BOOT_RECONCILED:
        # The window is over: every surviving record is registered in the
        # broker, so the caller's own broker scan already saw it.
        return
    _BOOT_REVOCATIONS.append(selector)
    while len(_BOOT_REVOCATIONS) > _BOOT_REVOCATIONS_MAX:
        _BOOT_REVOCATIONS.pop(0)


def _revoked_before_reconcile(rec: dict) -> bool:
    """Did a revocation land before this record could be restored?"""
    for sel in _BOOT_REVOCATIONS:
        if "chat_id" in sel:
            if rec.get("chat_id") == sel["chat_id"]:
                return True
            continue
        if sel.get("role") != rec.get("role"):
            continue
        labels = sel.get("labels")
        if labels is None or rec.get("session_scope") in labels:
            return True
    return False


def revoke_role(role: str, reason: str = "trigger_changed") -> int:
    """Every trigger of *role* was removed or rewritten.

    Bumps the role's epoch (so a firing turn still in flight cannot post a
    keyboard for the trigger set that just went away) and cancels its pending
    scheduled asks; each cancellation runs the finish hook, which edits the
    DM, tells the session and drops the record. Called from the EVENT LOOP by
    the callers of ``TriggerRegistry.reregister_for`` — never from inside the
    registry, whose ``_unwind_role`` runs in a worker thread where the
    broker's ``_finish`` would raise on ``get_running_loop()`` having already
    popped the request.
    """
    from verdict_broker import BROKER

    bump_role_epoch(role)
    _note_boot_revocation(role=role, labels=None)
    return len(BROKER.cancel_where(
        namespace=NAMESPACE, reason=reason,
        predicate=lambda r: _is_scheduled(r) and r.meta.get("target_role") == role,
    ))


# The three time-based trigger types. A trigger's session label is
# ``f"{type}-{name}"``, and a caller that knows only the NAME cannot know which
# type it was: a repeating reminder is derived into a `cron` trigger
# (`reminders.derive_recurrence`), a one-off into a `date` one. Matching all
# three is what keeps a cancel-by-name honest — asking callers to rebuild the
# label is how a cancelled repeating reminder kept an answerable question.
_TRIGGER_TYPES = ("date", "cron", "interval")


def revoke_trigger(
    role: str, name: str, reason: str = "trigger_changed",
) -> int:
    """One NAMED trigger of *role* was removed — a cancelled reminder, or a job
    the reminder sweep reconciled away.

    Takes the trigger name, never a session label: the label encodes the
    trigger's TYPE, which a cancellation site does not reliably know.
    """
    from verdict_broker import BROKER

    labels = {f"{t}-{name}" for t in _TRIGGER_TYPES}
    bump_trigger_epochs(role, labels)
    _note_boot_revocation(role=role, labels=labels)
    return len(BROKER.cancel_where(
        namespace=NAMESPACE, reason=reason,
        predicate=lambda r: (
            _is_scheduled(r)
            and r.meta.get("target_role") == role
            and r.meta.get("session_scope") in labels
        ),
    ))


def cancel_for_chat(chat_id: int, reason: str) -> int:
    """Cancel every pending SCHEDULED ask in one DM.

    The operator's attention lane is not one broker scope — a protected-action
    challenge registers under ``authz:<chat>`` while these live under
    ``dm:<chat>`` — so the challenge's admission calls this, in its own
    no-await block, to make the machine-timed question non-actionable before
    the human one is raised. An operator's own ``ask_user`` carries no
    ``scheduled`` marker and is never touched.
    """
    from verdict_broker import BROKER

    scope = f"dm:{chat_id}"
    _note_boot_revocation(chat_id=chat_id)
    return len(BROKER.cancel_where(
        namespace=NAMESPACE, reason=reason,
        predicate=lambda r: _is_scheduled(r) and r.scope == scope,
    ))


def cancel_non_scheduled_for_chat(chat_id: int, reason: str) -> int:
    """Cancel every pending NON-scheduled (human-raised) ask in one DM.

    The exact inverse selection of :func:`cancel_for_chat`, under the same
    discipline: ONE no-await block against the broker's live map, never the
    durable store. ``cancel_where`` is NAMESPACE-WIDE, so the predicate binds
    ``r.scope`` as well as the marker — without the scope clause this would
    retire an interactive ask in a DIFFERENT chat.

    #648: an ordinary DM message is what calls this. The v0.76.0 rule it
    implements ("the text IS the answer") is true of a question the operator
    was asked in this conversation and false of a machine-timed one, whose
    answer routes to the scheduled session that asked it and can never be
    carried by a turn of this one.

    No boot-revocation marker is left, deliberately: markers exist to settle
    DURABLE SCHEDULED records that an empty live map cannot speak for, and
    this call is defined to leave scheduled asks alone.
    """
    from verdict_broker import BROKER

    scope = f"dm:{chat_id}"
    return len(BROKER.cancel_where(
        namespace=NAMESPACE, reason=reason,
        predicate=lambda r: r.scope == scope and not _is_scheduled(r),
    ))


def displace_scheduled_for_chat(chat_id: int, reason: str) -> int:
    """Retire a live SCHEDULED ask in one DM because a human question has just
    TAKEN the lane — and, unlike :func:`cancel_for_chat`, leave no boot marker.

    Same selection, one difference, and the difference is the point (#648).
    A marker exists so a revocation landing in the boot window — live map
    empty, records still on disk — settles the record it meant to revoke.
    This caller has nothing to settle: if the live map is empty, the previous
    process's keyboard is still on screen, unedited, and nothing was told
    anything, so marking the record revoked would assert an event that did not
    happen. ``reconcile_at_boot`` decides it truthfully on its own — the
    delivered human ask holds the lane, so ``require_idle`` refuses and the
    record settles ``operator_busy``; or the lane is free again by then and the
    question is restored, still answerable.
    """
    from verdict_broker import BROKER

    scope = f"dm:{chat_id}"
    return len(BROKER.cancel_where(
        namespace=NAMESPACE, reason=reason,
        predicate=lambda r: _is_scheduled(r) and r.scope == scope,
    ))


def broker_meta(rec: dict) -> dict:
    """The broker ``meta`` for a scheduled ask — the single source for both
    the live path and the boot restore, so a restored request is bound
    exactly as the original was."""
    return {
        "options": list(rec.get("options") or []),
        "chat_id": rec["chat_id"],
        "operator_id": rec["operator_id"],
        "target_role": rec["role"],
        "session_scope": rec["session_scope"],
        "kind": "ask",
        "_scope": rec["scope"],
        "scheduled": True,
        "epoch": rec.get("epoch"),
        **({"message_id": rec["message_id"]}
           if rec.get("message_id") is not None else {}),
    }


# ---------------------------------------------------------------------------
# boot reconciliation
# ---------------------------------------------------------------------------

async def reconcile_at_boot(channel: Any, *, now: float | None = None) -> dict:
    """Reconcile every durable record against a fresh process.

    Runs after the Telegram channel is ready (its edits need a live bot).
    Returns a count per disposition, for the boot log.
    """
    from verdict_broker import BROKER

    store = STORE
    if store is None or channel is None:
        return {}
    now = time.time() if now is None else now
    counts = {"restored": 0, "expired": 0, "unconfirmed": 0,
              "settled_before_crash": 0, "operator_changed": 0}

    operator_id = None
    resolver = getattr(channel, "operator_user_id", None)
    if callable(resolver):
        try:
            from provenance import strict_positive_id
            operator_id = strict_positive_id(resolver())
        except Exception:  # noqa: BLE001
            operator_id = None

    for rec in store.all():
        rid = rec.get("rid")
        state = rec.get("state")
        if not isinstance(rid, str) or not rid:
            continue
        if state == STATE_SETTLING:
            # A terminal outcome was decided before the crash. At-most-once:
            # never replay it.
            counts["settled_before_crash"] += 1
            await store.drop(rid)
            continue
        if state == STATE_POSTING:
            # The keyboard may or may not have reached Telegram, and no
            # message_id was captured, so there is nothing to edit. A dispatch
            # here cannot duplicate: only a `settling` record has ever
            # dispatched.
            counts["unconfirmed"] += 1
            await _settle(channel, rec, kind="cancelled",
                          reason="delivery_unconfirmed", chosen=None,
                          edit_text=None)
            continue
        if state != STATE_LIVE:
            await store.drop(rid)
            continue

        # The one state in which the broker cannot answer for the store: from
        # process start until this reconcile runs, records exist on disk and
        # `_live` is empty, so a revocation in that window cancels nothing —
        # `revoke_role`/`revoke_trigger` scan the broker by design (they must
        # be atomic with registration, which the store cannot be). Without
        # this, a trigger reloaded or a reminder cancelled during the boot
        # window would have its question restored here and left answerable.
        #
        # The marker each revocation leaves is what closes it — carrying the
        # SELECTOR that revocation used, so cancelling one reminder settles
        # that reminder's question and restores the role's others.
        if _revoked_before_reconcile(rec):
            counts["revoked_before_reconcile"] = (
                counts.get("revoked_before_reconcile", 0) + 1)
            await _settle(channel, rec, kind="cancelled",
                          reason="trigger_changed", chosen=None,
                          edit_text=_expired_body(rec.get("body") or ""))
            continue

        # Identity at the point of use (#485 doctrine): the operator may have
        # changed while Casa was down. A keyboard bound to the previous one
        # must not be restored — the broker's actor binding would honour it.
        if (operator_id is None
                or rec.get("operator_id") != operator_id
                or rec.get("chat_id") != operator_id):
            counts["operator_changed"] += 1
            await _settle(channel, rec, kind="cancelled",
                          reason="operator_changed", chosen=None,
                          edit_text=_expired_body(rec.get("body") or ""))
            continue

        remaining = float(rec.get("expires_at") or 0) - now
        if remaining <= 0:
            counts["expired"] += 1
            await _settle(channel, rec, kind="no_answer", reason=None,
                          chosen=None,
                          edit_text=_expired_body(rec.get("body") or ""))
            continue

        # Same lane contract as the live path: a human question raised
        # between channel readiness and this reconcile owns the lane, and the
        # restored machine one must not sit behind it. A refusal is settled,
        # never left `live` with no hook.
        req, _created = BROKER.register(
            namespace=NAMESPACE, scope=rec["scope"], request_id=rid,
            timeout_s=remaining, detached=True,
            require_idle=True, idle_scopes=(f"authz:{rec['chat_id']}",),
            meta=broker_meta(rec),
        )
        if req is None:
            counts["operator_busy"] = counts.get("operator_busy", 0) + 1
            await _settle(channel, rec, kind="cancelled", reason="operator_busy",
                          chosen=None,
                          edit_text=_expired_body(rec.get("body") or ""))
            continue
        BROKER.set_finish_hook(req, make_finish_hook(channel, rec))
        counts["restored"] += 1

    # Past this point every surviving record is registered in the broker, so
    # the broker is authoritative again and the markers describe nothing that
    # is still invisible. Retired here rather than at each read so a revocation
    # landing DURING the pass still settles a record the loop has not reached.
    global _BOOT_RECONCILED
    _BOOT_RECONCILED = True
    _BOOT_REVOCATIONS.clear()
    logger.info("scheduled asks reconciled at boot: %s", counts)
    return counts
