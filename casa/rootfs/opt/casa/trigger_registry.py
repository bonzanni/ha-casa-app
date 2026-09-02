"""Per-agent trigger registry.

Residents declare their own interval / cron / webhook triggers in
``<agent>/triggers.yaml``. :class:`TriggerRegistry` wires each one
to the shared APScheduler instance or the HTTP app at boot time.

Replaces the single global heartbeat block in ``casa_core.main`` that
only understood assistant-level scheduling.
"""

from __future__ import annotations

import logging
import inspect
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable

from aiohttp import web

from bus import BusMessage, MessageBus, MessageType
from config import TriggerSpec
from log_cid import new_cid
from provenance import scheduled_delivery_markers
import scheduled_asks

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)


class _RoutingUnavailable:
    """The type of :data:`ROUTING_UNAVAILABLE` — a unique sentinel, never
    constructed a second time, never equal to anything but itself. Modelled on
    :class:`event_spool._RoutingUnavailable`, which is the same idea for the
    event routing map."""

    __slots__ = ()

    def __repr__(self) -> str:                          # pragma: no cover
        return "ROUTING_UNAVAILABLE"


#: Published into a PLUGIN overlay when no authoritative routing computation
#: stands behind it — the reconcile raised, or it ran against a registry it
#: could not read, or it has simply never run yet (#606). Distinguished from an
#: authoritative EMPTY ``{}``, which is a real compute result that says "nothing
#: should route": both close plugin ingress, but only one of them is a claim.
#: Reading them as the same thing is what let a failed reconcile regenerate
#: health and report nothing wrong while ingress was shut.
ROUTING_UNAVAILABLE = _RoutingUnavailable()


class TriggerError(Exception):
    """Raised on any trigger-wiring conflict or invalid shape."""


# #343: cron numbers weekdays 0/7=Sunday..6=Saturday; APScheduler 3.x
# numbers them 0=Monday..6=Sunday (4.x adopts the cron convention — this
# translation emits day NAMES, which mean the same days in both).
_CRON_DOW_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}
_DOW_TOKENS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]  # cron order


def _cron_dow_token(tok: str) -> int:
    """One cron DOW token → RAW cron weekday number (0..7; 0 and 7 are
    both Sunday). Kept raw — NOT collapsed mod 7 — so range endpoints
    keep their ordering: collapsing ``7`` early turned ``0-7`` (every
    day) into ``0-0`` (Sundays only). Callers normalize per expanded day.
    """
    if tok.isdigit():
        n = int(tok)
        if n > 7:
            raise TriggerError(f"cron day_of_week out of range: {tok!r}")
        return n
    if tok in _CRON_DOW_NAMES:
        return _CRON_DOW_NAMES[tok]
    raise TriggerError(f"invalid cron day_of_week token: {tok!r}")


def _translate_cron_dow(field: str) -> str:
    """Translate a standard-cron day-of-week field into APScheduler day
    NAMES (#343). Passing the numeric field through verbatim silently
    misschedules: APScheduler 3.x reads 0 as Monday, so ``0 9 * * 0``
    fired Monday instead of Sunday. Handles numbers, names, lists,
    ranges (wrap-around expanded — APScheduler ranges cannot wrap) and
    ``/step``; raises :class:`TriggerError` on anything malformed."""
    field = field.strip().lower()
    if field == "*":
        return "*"
    out: list[int] = []
    for part in field.split(","):
        part = part.strip()
        expr, sep, step_s = part.partition("/")
        if sep and (not step_s.isdigit() or int(step_s) < 1):
            raise TriggerError(
                f"invalid cron day_of_week step in {part!r}")
        step = int(step_s) if sep else 1
        if expr == "*":
            lo, hi = 0, 6
        elif "-" in expr:
            lo_s, _, hi_s = expr.partition("-")
            if not lo_s or not hi_s:
                raise TriggerError(
                    f"invalid cron day_of_week range: {expr!r}")
            # Raw 0..7 endpoints: 0-7 and 1-7 stay ascending full ranges
            # (7=Sunday alias) instead of collapsing/wrapping.
            lo, hi = _cron_dow_token(lo_s), _cron_dow_token(hi_s)
        else:
            lo = hi = _cron_dow_token(expr)
            if sep:
                # "n/step" = n through the field MAX, stepped (the
                # n-max/step convention). Terra r3-1: the max is RAW 7 —
                # capping at 6 turned "7/2" (Sunday only) into a
                # wraparound over the whole week.
                hi = 7
        if lo <= hi:
            days = list(range(lo, hi + 1))
        else:                     # cron wrap range, e.g. 6-0 = Sat,Sun
            days = list(range(lo, 7)) + list(range(0, hi + 1))
        for d_raw in days[::step]:
            d = d_raw % 7         # normalize the Sunday alias per day
            if d not in out:
                out.append(d)
    return ",".join(_DOW_TOKENS[d] for d in out)


@dataclass
class TriggerSummary:
    name: str
    type: str            # "interval" | "cron" | "date"
    schedule_desc: str   # "every 30m", the raw 5-field cron, or "once at <ts>"
    next_fire: datetime  # tz-aware


class TriggerRegistry:
    def __init__(
        self,
        *,
        scheduler: "AsyncIOScheduler",
        app: web.Application,
        bus: MessageBus,
        on_one_shot_fired: "Callable[[str, str], object] | None" = None,
    ) -> None:
        self._scheduler = scheduler
        self._app = app
        self._bus = bus
        # #396: invoked as (role, trigger_name) after an AGENT-OWNED one_shot
        # trigger has been dispatched, so its entry can be removed. INJECTED
        # rather than done here — the registry must not learn to write YAML.
        # Default None keeps every existing call site working.
        self._on_one_shot_fired = on_one_shot_fired
        self._seen_job_ids: set[str] = set()
        # #396: job ids whose _fire is RUNNING RIGHT NOW. APScheduler submits
        # a date job and then removes it from the store, so for the whole
        # duration of the dispatch the scheduler no longer reports the job —
        # and a sweep landing in that window would deliver the same reminder a
        # second time. This set is what keeps ownership exclusive across the
        # await inside _fire.
        self._in_flight: set[str] = set()
        self._specs_by_job_id: dict[str, TriggerSpec] = {}
        # N-1 + N-2 (v0.36.0): per-boot allowlist of webhook trigger names
        # → role. The wildcard /webhook/{name} handler in casa_core consults
        # this to 404 unknown names and dispatch knowns to the registered
        # role. reregister_for evicts removed names by role.
        #
        # #620 (seam S1): ONE immutable record per name — ``{"role",
        # "clearance", "auth"}`` — published by a single dict assignment, so a
        # reader that takes ``webhook_route(name)`` sees one route whole. The
        # former three per-field maps (target / clearance / auth policy) let a
        # consumer read a target, then a clearance from a re-registration that
        # landed in between, and stamp one message from two routes. A consumer
        # that needs more than one field of a route reads ``webhook_route``,
        # never two getters.
        self._webhook_routes: dict[str, dict] = {}
        self._webhook_names_by_role: dict[str, list[str]] = {}
        # #620 (seam S14/S17): while a ``reregister_for`` call is in progress,
        # its webhook records are STAGED here and published only after every
        # entry validated, the retirement hook succeeded and every job
        # installed. ``None`` outside such a call (a direct ``register_agent``
        # — boot, the reminder sites — publishes immediately, as before).
        self._staging: dict[str, dict] | None = None
        # Release B: plugin-declared webhook triggers form a SEPARATE overlay
        # layer keyed by effective name (always ``plg-<plugin>--<declared>``).
        # Resident trigger names can never start with ``plg-`` (schema
        # reservation), so the two namespaces are DISJOINT — the overlay needs
        # no owner map and cannot collide with or be evicted by resident
        # (re)registration. The whole overlay is replaced atomically (one dict
        # rebind) by the reconciler, so a request read sees the old-complete or
        # new-complete overlay, never a partial one. Each value:
        # ``{"role": str, "clearance": str, "auth": dict}``.
        # #606: starts UNAVAILABLE, not empty — before the first successful
        # reconcile nothing authoritative has been computed, and ``{}`` would
        # claim otherwise.
        self._plugin_overlay = ROUTING_UNAVAILABLE
        # #803: ONE publication generation for BOTH overlays, advanced by
        # every ``replace_*_overlay`` call after its rebind — a map, the
        # sentinel, the sentinel again. It answers the question the two
        # identity predicates cannot: "has ANY publication landed since I
        # last looked?" The setup-dispatch worker captures it before its
        # blocking route recomputation and compares it, yield-free, before
        # the send; an ordinary live-to-live publication (the revoke sweep
        # filtering a plugin's routes out) is invisible to the predicates
        # and visible here.
        self._routing_generation = 0
        # Plugin-declared authorization callbacks are a SECOND,
        # independent overlay keyed by effective name (also ``plg-<plugin>--
        # <declared>``, in its OWN namespace — the callback endpoint is
        # ``GET /callback/{name}``, never ``/webhook/{name}``, so a name may
        # legitimately exist in one overlay and not the other). Same atomic
        # whole-swap discipline: the callback reconciler rebuilds the complete
        # desired map and rebinds it in one operation, so a removed / revoked /
        # unconsented plugin's callback ingress is swept by absence (the
        # handler 404s) and readers never see a partial overlay. Each value:
        # ``{"plugin": str, "declared": str, "path": str}`` (``path`` is the
        # RESOLVED artifact root the discovery index is keyed by).
        self._callback_overlay = ROUTING_UNAVAILABLE      # #606, as above

    def register_agent(
        self,
        role: str,
        triggers: list[TriggerSpec],
        channels: list[str],
    ) -> None:
        """Wire every trigger for *role*. Raises :class:`TriggerError`
        on validation failure. Idempotent failure — a partial register
        leaves prior triggers in place but stops at the offending entry."""

        names_seen: set[str] = set()
        for trig in triggers:
            if trig.name in names_seen:
                raise TriggerError(
                    f"agent {role!r}: duplicate trigger name {trig.name!r}"
                )
            names_seen.add(trig.name)

            if trig.type in ("interval", "cron", "date"):
                if trig.channel not in channels:
                    raise TriggerError(
                        f"agent {role!r} trigger {trig.name!r}: channel "
                        f"{trig.channel!r} not registered on this agent "
                        f"(channels={channels})"
                    )
                self._register_scheduled(role, trig)
            elif trig.type == "webhook":
                # Release A: webhook triggers are served EXCLUSIVELY by the
                # authenticated wildcard /webhook/{name} handler — the old
                # per-path route is gone, so ``path`` is neither served nor a
                # collision axis (v2 triggers carry path=""; a path check would
                # false-collide). Uniqueness is by trigger NAME only.
                self._check_webhook_collision(role, trig.name)
                self._register_webhook(role, trig)
            else:
                raise TriggerError(
                    f"agent {role!r} trigger {trig.name!r}: unknown "
                    f"type {trig.type!r}"
                )

    def _register_scheduled(self, role: str, trig: TriggerSpec) -> None:
        job_id = f"{role}:{trig.name}"
        if job_id in self._seen_job_ids:
            raise TriggerError(
                f"duplicate scheduler job id {job_id!r}"
            )

        async def _fire() -> None:
            logger.info(
                "Trigger firing: agent=%s name=%s type=%s",
                role, trig.name, trig.type,
            )
            self._in_flight.add(job_id)
            try:
                await _dispatch()
            finally:
                self._in_flight.discard(job_id)

        async def _dispatch() -> None:
            msg = BusMessage(
                type=MessageType.SCHEDULED,
                source="scheduler",
                target=role,
                content=trig.prompt,
                channel=trig.channel,
                context={
                    "chat_id": f"{trig.type}-{trig.name}",
                    "trigger": trig.name,
                    "cid": new_cid(),
                    # #485: admits send_media for a telegram-channel trigger.
                    # The chat_id above stays the SESSION LABEL — it keys the
                    # SDK session and the voice-job scope, and the delivery
                    # identity is resolved separately at the point of use.
                    # #573: the epoch says which trigger SET this fired under,
                    # so a turn still running when its trigger is deleted or
                    # rewritten can no longer raise a question for it.
                    **scheduled_delivery_markers(
                        trig.channel,
                        scheduled_asks.epoch_for(
                            role, f"{trig.type}-{trig.name}")),
                },
            )
            await self._bus.send(msg)

            if trig.one_shot:
                # #396 / INV-TRIG-009. Ordering is load-bearing: the bus send
                # has ALREADY happened, so everything below is cleanup, not
                # part of delivery. A failure here leaves the yaml entry in
                # place and the sweep redelivers — at-least-once by choice
                # (spec §8), because a duplicate nudge is a far better failure
                # than a missed reminder.
                #
                # _drop_job is UNCONDITIONAL: the job must go whoever owns the
                # entry, both because a one-shot that keeps its job could fire
                # again and because the id must be freed for re-registration.
                self._drop_job(job_id)
                # The ENTRY removal is gated on ownership (#398 release 2).
                # An operator may author their own dated one-shot, and deleting
                # a line out of their triggers.yaml because it fired is not
                # this registry's business. Consequence, accepted and stated in
                # INV-TRIG-009: such an entry lingers inert — never
                # re-registered, because a past-dated trigger is not registered
                # at boot, and never swept, because it carries no managed_by.
                if trig.managed_by != "agent":
                    logger.info(
                        "one-shot %s for %s is operator-owned; leaving its "
                        "entry in place", trig.name, role,
                    )
                elif self._on_one_shot_fired is not None:
                    try:
                        # May be sync (tests inject plain callables) or a
                        # coroutine function: the shipped cleanup writes
                        # triggers.yaml under trigger_write_lock.PASS_LOCK on a
                        # worker thread (#458), so it returns an awaitable that
                        # must be awaited here rather than dropped.
                        _res = self._on_one_shot_fired(role, trig.name)
                        if inspect.isawaitable(_res):
                            await _res
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "one-shot cleanup failed for %s:%s; the sweep "
                            "will redeliver", role, trig.name, exc_info=True,
                        )

        if trig.type == "date":
            # #396: a genuine point in time. Cron has no year field, so a
            # dated one-shot written as cron is an ANNUAL trigger in disguise
            # — this branch is what removes that trap.
            import reminders
            when = reminders.parse_at(trig.at)   # raises on naive/absent
            if when <= datetime.now(when.tzinfo):
                # Already overdue: the sweep owns it. Registering here would
                # either fire instantly at boot or vanish as a misfire, and
                # both destroy the "still present, still owed" evidence.
                # Deliberately returns BEFORE claiming job_id, so a later
                # re-registration of the same name is not blocked.
                logger.info(
                    "Reminder %s for agent %s is already past (%s); leaving "
                    "it to the sweep", trig.name, role, trig.at,
                )
                return
            self._scheduler.add_job(
                _fire, trigger="date", run_date=when, id=job_id,
            )
        elif trig.type == "interval":
            self._scheduler.add_job(
                _fire, trigger="interval", minutes=trig.minutes, id=job_id,
            )
        else:  # cron
            # Parse the 5-field cron string. APScheduler uses kwargs.
            fields = trig.schedule.split()
            if len(fields) != 5:
                raise TriggerError(
                    f"agent {role!r} trigger {trig.name!r}: cron schedule "
                    f"must be a 5-field string; got {trig.schedule!r}"
                )
            minute, hour, day, month, day_of_week = fields
            # #396: a recurring reminder carries its first occurrence in
            # ``at``. Passing it as start_date stops the series firing BEFORE
            # the date the user asked for — "every Thursday from the 20th"
            # set on the 3rd would otherwise fire on the 6th and 13th.
            # Recurrence is still driven by the cron fields, evaluated in the
            # scheduler's timezone, so this does not affect DST correctness.
            extra: dict = {}
            if trig.at:
                import reminders
                extra["start_date"] = reminders.parse_at(trig.at)
            self._scheduler.add_job(
                _fire, trigger="cron",
                minute=minute, hour=hour, day=day, month=month,
                # #343: cron 0/7=Sunday vs APScheduler 3.x 0=Monday —
                # translate to day names (identical meaning in both).
                day_of_week=_translate_cron_dow(day_of_week), id=job_id,
                **extra,
            )
        self._seen_job_ids.add(job_id)
        self._specs_by_job_id[job_id] = trig

    def _drop_job(self, job_id: str) -> bool:
        """Remove a scheduled job and forget its bookkeeping.

        Returns True if the scheduler actually had it. Freeing ``job_id`` from
        ``_seen_job_ids`` matters: without it the duplicate-id guard would
        refuse to re-register the same trigger name after a one-shot fired or
        a reminder was cancelled.
        """
        removed = True
        try:
            self._scheduler.remove_job(job_id)
        except Exception:  # noqa: BLE001 - already gone is success here
            removed = False
        self._seen_job_ids.discard(job_id)
        self._specs_by_job_id.pop(job_id, None)
        return removed

    def has_job(self, role: str, name: str) -> bool:
        """True if a live scheduled job exists for this role and name (#396).

        The reminder sweep consults this to keep ownership exclusive: if the
        scheduler still holds the job it WILL deliver it, so the sweep must
        not. Without this the two race for a reminder whose time has just
        passed and the user gets it twice.

        The SCHEDULER is the authority, not ``_seen_job_ids``. APScheduler
        drops a date job that overran its misfire grace period WITHOUT ever
        calling the job function, which leaves our bookkeeping claiming a live
        job that no longer exists — and the sweep would then skip that
        reminder forever, so it would never be delivered at all.
        """
        job_id = f"{role}:{name}"
        # A dispatch already in progress owns this reminder, even though the
        # scheduler has already dropped the job.
        if job_id in self._in_flight:
            return True
        if job_id not in self._seen_job_ids:
            return False
        try:
            return self._scheduler.get_job(job_id) is not None
        except Exception:  # noqa: BLE001 - no scheduler view; trust bookkeeping
            return True

    def remove_job_for(self, role: str, name: str) -> bool:
        """Drop a live scheduled job by role and trigger name (#396).

        Used by ``cancel_reminder`` so a cancellation takes effect now rather
        than at the next boot.
        """
        return self._drop_job(f"{role}:{name}")

    def agent_owned_job_names(self, role: str) -> list[str]:
        """Names of this role's jobs the AGENT owns (``managed_by: agent``).

        The sweep uses this to reconcile in both directions: an agent-owned job
        with no entry left in ``triggers.yaml`` must go, or a cancellation that
        raced a reload — which re-registers from a snapshot taken before the
        cancellation — would leave the reminder firing forever despite the
        tool having reported success.

        Selection is by the spec's recorded ownership, never by its name. The
        operator's triggers share the same file, and the schema permits them a
        `reminder-`-prefixed dated one-shot of their own; matching on the
        prefix would drop it.
        """
        head = f"{role}:"
        return [job_id[len(head):]
                for job_id, spec in self._specs_by_job_id.items()
                if job_id.startswith(head) and spec.managed_by == "agent"]

    def _register_webhook(self, role: str, trig: TriggerSpec) -> None:
        # Release A: webhook triggers are served ONLY by the authenticated
        # wildcard /webhook/{name} handler in casa_core (per-trigger auth +
        # body cap + origin stamping + fresh-uuid one-shot). The old
        # per-path ``router.add_post(trig.path, …)`` route — which did NO
        # auth, NO body cap, and pinned chat_id=trig.name — is REMOVED (it
        # was an unauthenticated bypass; a v2 trigger's empty path even
        # registered an open ``POST /``). This method now only maintains the
        # name→role/clearance/auth allowlist the wildcard handler consults.
        record = {
            "role": role,
            "clearance": getattr(trig, "clearance", "public") or "public",
            "auth": getattr(trig, "auth", None) or {
                "mode": "hmac_body", "header": "X-Webhook-Signature",
                "tolerance_secs": 300, "secret_owner": "casa",
            },
        }
        self._webhook_names_by_role.setdefault(role, []).append(trig.name)
        if self._staging is not None:
            self._staging[trig.name] = record
        else:
            self._webhook_routes[trig.name] = record

    def _check_webhook_collision(self, role: str, name: str) -> None:
        owner = (self._webhook_routes.get(name) or {}).get("role")
        if owner is not None and owner != role:
            raise TriggerError(
                f"agent {role!r} trigger {name!r}: webhook "
                f"trigger name already registered by role {owner!r}"
            )

    def _validate_agent(
        self, role: str, triggers: list[TriggerSpec], channels: list[str],
    ) -> None:
        """Every check :meth:`register_agent` performs, with NO side effect
        (#620, seam S33). ``reregister_for`` runs this before the retirement
        hook and before any job is installed, so a later entry's fault can
        neither follow a retirement nor find an earlier job already executable.
        Raises :class:`TriggerError` for every failure, including the ones
        APScheduler's own trigger construction would raise at ``add_job``."""
        from apscheduler.triggers.cron import CronTrigger
        import reminders

        names_seen: set[str] = set()
        for trig in triggers:
            if trig.name in names_seen:
                raise TriggerError(
                    f"agent {role!r}: duplicate trigger name {trig.name!r}"
                )
            names_seen.add(trig.name)
            if trig.type in ("interval", "cron", "date"):
                if trig.channel not in channels:
                    raise TriggerError(
                        f"agent {role!r} trigger {trig.name!r}: channel "
                        f"{trig.channel!r} not registered on this agent "
                        f"(channels={channels})"
                    )
                job_id = f"{role}:{trig.name}"
                if job_id in self._seen_job_ids:
                    raise TriggerError(f"duplicate scheduler job id {job_id!r}")
                try:
                    if trig.type == "date":
                        reminders.parse_at(trig.at)
                    elif trig.type == "cron":
                        fields = trig.schedule.split()
                        if len(fields) != 5:
                            raise TriggerError(
                                f"agent {role!r} trigger {trig.name!r}: cron "
                                f"schedule must be a 5-field string; got "
                                f"{trig.schedule!r}"
                            )
                        minute, hour, day, month, day_of_week = fields
                        extra: dict = {}
                        if trig.at:
                            extra["start_date"] = reminders.parse_at(trig.at)
                        CronTrigger(minute=minute, hour=hour, day=day, month=month,
                                    day_of_week=_translate_cron_dow(day_of_week),
                                    **extra)
                except TriggerError:
                    raise
                except Exception as exc:  # noqa: BLE001 — surfaced as the one error type
                    raise TriggerError(
                        f"agent {role!r} trigger {trig.name!r}: {exc}"
                    ) from exc
            elif trig.type == "webhook":
                self._check_webhook_collision(role, trig.name)
            else:
                raise TriggerError(
                    f"agent {role!r} trigger {trig.name!r}: unknown "
                    f"trigger type {trig.type!r}"
                )

    def get_webhook_target(self, name: str) -> str | None:
        """Return the role registered for a webhook trigger ``name``,
        or ``None`` if no such trigger is currently registered. Consulted
        by the wildcard ``/webhook/{name}`` handler in casa_core to 404
        unknown names and dispatch knowns to the right role. Resident
        triggers win; plugin-overlay triggers (``plg-…``) are the fallback
        (the namespaces are disjoint, so at most one ever matches).
        """
        record = self._webhook_routes.get(name)
        if record is not None:
            return record["role"]
        if self._plugin_overlay is ROUTING_UNAVAILABLE:
            return None                                  # #606: closed ingress
        entry = self._plugin_overlay.get(name)
        return entry["role"] if entry is not None else None

    def webhook_route(self, name: str) -> dict | None:
        """ONE atomic snapshot of the route for ``name`` — ``{"role",
        "clearance", "auth", "resident"}`` — or ``None`` when nothing routes
        it (#620, seam S1). Resident records win; the plugin overlay is the
        fallback, ``resident: False``, exactly as ``get_webhook_target``
        resolves. The wildcard handler and the secret report read THIS and
        nothing else per request or row, so a re-registration between two
        reads can never stamp one message, or one row, from two routes.
        """
        record = self._webhook_routes.get(name)
        if record is not None:
            return {**record, "resident": True}
        if self._plugin_overlay is ROUTING_UNAVAILABLE:
            return None                                  # #606: closed ingress
        entry = self._plugin_overlay.get(name)
        if entry is None:
            return None
        return {"role": entry["role"], "clearance": entry["clearance"],
                "auth": entry["auth"], "resident": False}

    def webhook_names_for(self, role: str) -> list[str]:
        """The webhook names this role currently ROUTES, from the same map
        ``get_webhook_target`` consults first (#609).

        Deliberately not ``_webhook_names_by_role``: that is teardown
        bookkeeping which appends without de-duplicating, so a re-registration
        leaves the name twice. ``_webhook_routes`` is the routing authority
        and ``_unwind_role`` clears it, so a name here is a name a request
        would actually reach. Plugin-overlay names are excluded by
        construction — they live in ``_plugin_overlay`` and their namespace
        (``plg-…``) is disjoint from any resident name the schema admits.
        """
        return sorted(n for n, r in self._webhook_routes.items() if r["role"] == role)

    def get_clearance(self, name: str) -> str:
        """Return the declared memory read-clearance for webhook trigger
        ``name`` (default ``"public"``). Stamped onto the dispatched turn's
        ``_origin_clearance`` so the recall gate reads at the declared tier.
        """
        record = self._webhook_routes.get(name)
        if record is not None:
            return record["clearance"]
        if self._plugin_overlay is ROUTING_UNAVAILABLE:
            return "public"                              # #606: closed ingress
        entry = self._plugin_overlay.get(name)
        return entry["clearance"] if entry is not None else "public"

    def get_auth_policy(self, name: str) -> dict | None:
        """Return the per-trigger auth policy for webhook ``name`` (mode/header/
        tolerance_secs/secret_owner), or ``None`` if the name is unregistered.
        The wildcard handler verifies the request with this policy."""
        record = self._webhook_routes.get(name)
        if record is not None:
            return record["auth"]
        if self._plugin_overlay is ROUTING_UNAVAILABLE:
            return None                                  # #606: closed ingress
        entry = self._plugin_overlay.get(name)
        return entry["auth"] if entry is not None else None

    def replace_plugin_overlay(self, overlay: dict[str, dict]) -> None:
        """Atomically replace the ENTIRE plugin-trigger overlay (Release B).

        ``overlay`` maps effective name → ``{"role", "clearance", "auth"}``.
        The reconciler builds the complete desired overlay (every resolved,
        assigned, valid, acked plugin trigger) and swaps it here in one dict
        rebind — so a removed/unresolved/revoked plugin's ingress is swept
        (absent from the new map → 404) and readers never see a partial set.

        #606: accepts :data:`ROUTING_UNAVAILABLE` too, and stores the SINGLETON
        — never ``dict(sentinel)``, which would silently downgrade "no
        authoritative computation stands behind this" to the authoritative
        claim "nothing should route".
        """
        if overlay is ROUTING_UNAVAILABLE:
            self._plugin_overlay = ROUTING_UNAVAILABLE
        else:
            self._plugin_overlay = dict(overlay)
        # #803: bump AFTER the one rebind, in the same synchronous step, so a
        # loop-side reader never sees a new generation with an old overlay.
        self._routing_generation += 1

    def replace_callback_overlay(self, overlay: dict[str, dict]) -> None:
        """Atomically replace the ENTIRE authorization-callback overlay —
        the exact counterpart of :meth:`replace_plugin_overlay`.

        ``overlay`` maps effective name → ``{"plugin", "declared", "path"}``.
        The callback reconciler is its ONE writer: it derives the complete
        desired map (every resolved, assigned, validly-declared, acked plugin
        callback) and swaps it in a single dict rebind, so an unrouted
        plugin's callback endpoint is swept by absence and a concurrent
        request read sees the old-complete or new-complete overlay.

        #606: accepts :data:`ROUTING_UNAVAILABLE`, stored as the singleton —
        see :meth:`replace_plugin_overlay`.
        """
        if overlay is ROUTING_UNAVAILABLE:
            self._callback_overlay = ROUTING_UNAVAILABLE
        else:
            self._callback_overlay = dict(overlay)
        self._routing_generation += 1                 # #803: see the trigger twin

    def get_callback(self, name: str) -> dict | None:
        """The overlay entry for callback ``name``, or ``None`` when no such
        callback is currently consented + routed. The ``GET /callback/{name}``
        handler consults this to 404 unknown names and to learn which plugin's
        spool a deposit belongs to. There is no resident-callback layer: unlike
        webhooks, callbacks exist only as plugin declarations."""
        if self._callback_overlay is ROUTING_UNAVAILABLE:
            return None                                  # #606: closed ingress
        return self._callback_overlay.get(name)

    def callback_overlay_names(self) -> list[str]:
        """Effective names currently live in the callback overlay. Empty under
        :data:`ROUTING_UNAVAILABLE` — nothing routes."""
        if self._callback_overlay is ROUTING_UNAVAILABLE:
            return []
        return list(self._callback_overlay)

    def plugin_overlay_names(self) -> list[str]:
        """Effective names currently live in the plugin overlay. Empty under
        :data:`ROUTING_UNAVAILABLE` — nothing routes."""
        if self._plugin_overlay is ROUTING_UNAVAILABLE:
            return []
        return list(self._plugin_overlay)

    def plugin_overlay_snapshot(self) -> dict[str, dict]:
        """A shallow copy of the current overlay — for callers that must
        derive a swept replacement WITHOUT a resolver pass (the revoke
        tool's fail-closed direct sweep).

        #606: under :data:`ROUTING_UNAVAILABLE` this returns the SINGLETON, not
        a copy and not ``{}``. A caller deriving a swept replacement from it
        would otherwise publish an authoritative empty map built from state it
        never had — see the revoke sweep, which checks identity and skips."""
        if self._plugin_overlay is ROUTING_UNAVAILABLE:
            return ROUTING_UNAVAILABLE
        return dict(self._plugin_overlay)

    def plugin_overlay_unavailable(self) -> bool:
        """True while no authoritative plugin-trigger routing computation
        stands behind the overlay (#606)."""
        return self._plugin_overlay is ROUTING_UNAVAILABLE

    def callback_overlay_unavailable(self) -> bool:
        """True while no authoritative plugin-callback routing computation
        stands behind the overlay (#606)."""
        return self._callback_overlay is ROUTING_UNAVAILABLE

    def routing_generation(self) -> int:
        """#803: the number of overlay publications so far, of EITHER kind
        and including a re-published sentinel. Two equal readings mean no
        publication landed between them; the setup-dispatch worker relies on
        exactly that across its blocking route recomputation."""
        return self._routing_generation

    def _unwind_role(self, role: str) -> list[str]:
        """Drop every scheduler job and webhook-allowlist entry owned by
        *role*. Idempotent. Returns the job ids that could NOT be removed
        (Terra r1-2): a failed ``remove_job`` used to be swallowed while the
        id was dropped from tracking anyway — a zombie job kept firing while
        the registry reported no triggers, and a same-id replacement would
        collide. A stuck job stays tracked so the registry never lies; the
        caller decides whether stuck jobs are fatal."""
        from apscheduler.jobstores.base import JobLookupError

        stuck: list[str] = []
        prefix = f"{role}:"
        to_drop = [
            jid for jid in list(self._seen_job_ids) if jid.startswith(prefix)
        ]
        for jid in to_drop:
            try:
                self._scheduler.remove_job(jid)
            except JobLookupError:
                pass  # already absent — dropping the tracking is correct
            except Exception as exc:  # noqa: BLE001
                logger.warning("remove_job %s failed: %s", jid, exc)
                stuck.append(jid)
                continue
            self._seen_job_ids.discard(jid)
            self._specs_by_job_id.pop(jid, None)

        # N-2 (v0.36.0): evict this role's webhook names from the
        # allowlist so a removed webhook trigger naturally 404s on the
        # wildcard handler post-reload.
        for name in self._webhook_names_by_role.get(role, []):
            # Only evict if THIS role still owns the name (register_agent
            # now rejects cross-role webhook name collisions, so this is
            # belt-and-braces).
            if (self._webhook_routes.get(name) or {}).get("role") == role:
                self._webhook_routes.pop(name, None)
        self._webhook_names_by_role[role] = []
        return stuck

    def reregister_for(
        self,
        role: str,
        triggers: list[TriggerSpec],
        channels: list[str],
        *,
        before_install=None,
    ) -> None:
        """Remove this role's existing APScheduler jobs and webhook paths,
        then re-wire from the supplied specs — as ONE operation (#620):
        unwind → validate every entry (no side effect) → ``before_install``
        (the secret-retirement hook, handed the sorted webhook names about to
        route; any exception aborts) → install (jobs live, webhook records
        STAGED) → publish the staged records. Nothing is published, and no job
        exists, before every earlier step succeeded; a failure at any step
        runs the existing unwind arm and leaves the role with NO triggers.

        Fail-closed: if re-registration raises, the agent is left with NO
        triggers. #307: register_agent stops at the offending entry, so the
        replacement triggers installed before it must be unwound too —
        without the second unwind, a `[valid, malformed]` list left the
        valid trigger firing while the reload reported failure. Terra r1-2:
        if the scheduler refuses to REMOVE an existing job, the old set is
        still (partially) live — that raises here before any replacement is
        installed, and the error says the old triggers remain. The caller
        should surface the error either way.
        """
        stuck = self._unwind_role(role)
        if stuck:
            # Sol r2-2a: precise state disclosure — the unwind already
            # evicted the role's webhook allowlist entries (pure dict ops
            # that cannot fail), so only the STUCK JOBS remain live.
            raise TriggerError(
                f"agent {role!r}: could not remove existing scheduler "
                f"job(s) {stuck} — those jobs remain live; every other "
                f"trigger for the role (webhooks included) is now "
                f"unregistered. Refusing re-registration to avoid "
                f"zombie/duplicate jobs"
            )
        try:
            self._validate_agent(role, triggers, channels)
            if before_install is not None:
                before_install(sorted(
                    t.name for t in triggers if t.type == "webhook"))
            self._staging = {}
            try:
                self.register_agent(role, triggers, channels)
                staged = self._staging
            finally:
                self._staging = None
            for name, record in staged.items():
                self._webhook_routes[name] = record
        except Exception as exc:
            leftover = self._unwind_role(role)
            if leftover:
                logger.error(
                    "unwind after failed re-registration left job(s) live "
                    "for role=%s: %s", role, leftover,
                )
                # Terra r2-2 / Sol r2-2b: the raised error must DISCLOSE the
                # leftover live jobs — callers relay this message as the
                # role's resulting trigger state.
                raise TriggerError(
                    f"{exc} — additionally, the post-failure unwind could "
                    f"not remove job(s) {leftover}; those replacement "
                    f"job(s) remain live while every other trigger for the "
                    f"role is unregistered"
                ) from exc
            raise

    def list_jobs_for(
        self, role: str, within_hours: int,
    ) -> list[TriggerSummary]:
        """Return summaries of this agent's scheduled jobs firing in the window.

        Sorted by next fire time ascending. Does not include webhook
        triggers (they have no schedule).
        """
        within_hours = max(1, min(720, int(within_hours)))
        now = datetime.now(self._scheduler.timezone)
        cutoff = now + timedelta(hours=within_hours)

        out: list[TriggerSummary] = []
        prefix = f"{role}:"
        for job in self._scheduler.get_jobs():
            if not job.id.startswith(prefix):
                continue
            next_fire = job.next_run_time
            if next_fire is None or next_fire > cutoff:
                continue
            trig = self._specs_by_job_id.get(job.id)
            if trig is None:
                continue
            if trig.type == "interval":
                schedule_desc = f"every {trig.minutes}m"
            elif trig.type == "cron":
                schedule_desc = trig.schedule
            elif trig.type == "date":
                # #396: MUST be listed. cancel_reminder takes the name that
                # get_schedule reports, so a one-shot reminder omitted here is
                # a reminder the user can neither see nor cancel.
                schedule_desc = f"once at {trig.at}"
            else:
                continue
            out.append(TriggerSummary(
                name=trig.name,
                type=trig.type,
                schedule_desc=schedule_desc,
                next_fire=next_fire,
            ))

        out.sort(key=lambda s: s.next_fire)
        return out
