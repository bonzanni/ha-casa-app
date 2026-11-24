"""Durable FIFO coordination for proactive Home Assistant voice delivery."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

import voice_phrases
from channels.voice.routes import _identifier
from job_registry import DeliveryState, JobTransitionError, VoiceJob
from voice_job_result import (
    VoiceJobResultError,
    parse_voice_job_result,
    spoken_text_for,
    voice_identity_clearance,
)


logger = logging.getLogger(__name__)

# Bumped to 2 for endpoint delivery (#233/#224): `job_ready` now carries the
# modality the endpoint promised, and an integration that cannot read it must
# not act on the frame at all — a v1 peer would announce a `text` delivery on a
# satellite the job was never promised to.
_PROTOCOL = 2
_INBOUND_TYPES = frozenset({
    "job_claimed",
    "job_claim_renew",
    "job_delivery_start",
    "job_playback_started",
    "job_delivered",
    "job_nack",
    "job_revoked",
})
_LIVE_STATES = frozenset({
    DeliveryState.READY,
    DeliveryState.CLAIMED,
    DeliveryState.AUTHORIZED,
    DeliveryState.PLAYING,
})
_BACKGROUND_CAPABILITIES = frozenset({
    "background_jobs", "endpoint_delivery",
})
# Delivery dispatches on this; anything else has no honourable destination.
_DELIVERABLE_MODALITIES = frozenset({"audio", "text"})
_PARK_REASONS = frozenset({
    # A nack that means "this endpoint cannot take it" must PARK. Without the
    # entry the nack returns the job to READY and _offer_heads_locked() reoffers
    # immediately, churning job_ready<->job_nack every sweep instead of once
    # (#233/#224 review). With no fallback chain in v1, parking until expiry is
    # the correct end state.
    "satellite_not_found", "satellite_ambiguous", "no_text_endpoint",
})


def _is_current_protocol(value: Any) -> bool:
    return type(value) is int and value == _PROTOCOL


@dataclass
class _Attempt:
    job_id: str
    route_id: str
    device_id: str
    attempt_id: str
    endpoint: Any
    offered: bool = True
    revoke_sent: bool = False
    # #329: set when reconciliation decides the attempt is no longer
    # deliverable — an unacked revoke must eventually free the entry.
    revoke_deadline: float | None = None


class VoiceDeliveryCoordinator:
    """One process-local writer/sweeper over durable delivery CAS state."""

    def __init__(
        self,
        job_registry,
        route_registry,
        *,
        lease_s: float = 15,
        renew_s: float = 5,
        park_s: float = 30,
        clock=time.monotonic,
    ) -> None:
        if (
            lease_s <= 0
            or renew_s <= 0
            or renew_s >= lease_s
            or park_s <= 0
        ):
            raise ValueError("delivery lease/renew windows are invalid")
        self._jobs = job_registry
        self._routes = route_registry
        self._lease_s = float(lease_s)
        self._renew_s = float(renew_s)
        self._park_s = float(park_s)
        self._clock = clock
        # The registry is the durable lease authority.
        self._jobs.LEASE_SECONDS = self._lease_s
        self._attempts: dict[str, _Attempt] = {}
        self._parked_until: dict[str, float] = {}
        # Jobs already reported as un-offerable, so the 1s sweep logs the
        # condition once rather than every tick (#233/#224).
        self._unoffered_logged: set[str] = set()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — keep delivery supervised
                # The exception type is useful operational metadata; its
                # message may contain persisted result data and is omitted.
                logger.error(
                    "voice delivery sweep failed error=%s",
                    type(exc).__name__,
                )
            await asyncio.sleep(1)

    async def route_connected(self, endpoint: Any) -> None:
        """Discard unclaimed offers on reconnect and replay durable heads."""
        async with self._lock:
            bound = self._current_bound(endpoint)
            if bound is None:
                return
            for job_id in list(self._parked_until):
                job = self._jobs.get(job_id)
                if job is not None and job.origin_route_id == bound.route_id:
                    self._parked_until.pop(job_id, None)
            for job_id, attempt in list(self._attempts.items()):
                if attempt.route_id != bound.route_id:
                    continue
                if attempt.offered:
                    self._attempts.pop(job_id, None)
                else:
                    # A durable claimed attempt survives reconnect. Rebind its
                    # writer only to the newly authenticated route instance.
                    attempt.endpoint = bound
                    attempt.revoke_sent = False
                    attempt.revoke_deadline = None
            await self._recover_due_locked()
            await self._offer_heads_locked()

    async def route_disconnected(self, endpoint: Any) -> None:
        async with self._lock:
            route_id = _identifier(getattr(endpoint, "route_id", None))
            if route_id is None:
                route_id = _identifier(
                    getattr(endpoint, "voice_route_id", None),
                )
            if route_id is None:
                return
            for job_id, attempt in list(self._attempts.items()):
                if (
                    attempt.route_id == route_id
                    and attempt.offered
                    # #304 (review round 2): a LATE disconnect notification
                    # for a displaced binding must not tear down the offer a
                    # newer socket already received for the reused route id.
                    and attempt.endpoint is endpoint
                ):
                    self._attempts.pop(job_id, None)

    async def sweep_once(self) -> None:
        async with self._lock:
            await self._recover_due_locked()
            await self._offer_heads_locked()

    async def handle(self, endpoint: Any, frame: Mapping[str, Any]) -> None:
        frame_type = frame.get("type")
        if (
            frame_type not in _INBOUND_TYPES
            or not _is_current_protocol(frame.get("protocol"))
        ):
            return
        async with self._lock:
            # A frame cannot revive a result or lease whose durable deadline
            # passed between one-second sweep ticks.
            await self._recover_due_locked()
            await self._offer_heads_locked()
            bound = self._current_bound(endpoint)
            job_id = _identifier(frame.get("job_id"))
            attempt_id = _identifier(frame.get("delivery_attempt_id"))
            if bound is None or job_id is None or attempt_id is None:
                await self._deny(endpoint, job_id, attempt_id, "invalid_frame")
                return

            # Revocation acknowledgements refer to the process-local attempt
            # Casa just revoked.  The durable transition may already have
            # cleared its attempt ID (CANCELLED/EXPIRED/READY), so handle the
            # ack before normal durable-state matching.  Duplicate/stale acks
            # are silent to avoid a revoke/ack feedback loop.
            if frame_type == "job_revoked":
                await self._handle_revoked_ack_locked(
                    bound, job_id, attempt_id,
                )
                return

            job = self._jobs.get(job_id)
            if not self._matches_job(bound, job, attempt_id=attempt_id):
                await self._deny(endpoint, job_id, attempt_id, "stale_attempt")
                return

            try:
                if frame_type == "job_claimed":
                    attempt = self._attempts.get(job_id)
                    if (
                        attempt is None
                        or not attempt.offered
                        or attempt.attempt_id != attempt_id
                        or attempt.endpoint is not bound
                    ):
                        await self._deny(
                            endpoint, job_id, attempt_id, "stale_attempt",
                        )
                        return
                    await self._jobs.claim(job_id, attempt_id)
                    attempt.offered = False
                    return

                if frame_type == "job_claim_renew":
                    attempt = self._attempts.get(job_id)
                    if (
                        job.delivery_state is DeliveryState.AUTHORIZED
                        and job.cancel_pending
                        and attempt is not None
                    ):
                        await self._send_revoke(attempt, "cancelled")
                        return
                    await self._jobs.renew(job_id, attempt_id)
                    return

                if frame_type == "job_delivery_start":
                    # Re-run disclosure at the final Casa authorization seam.
                    # The already-offered text is not echoed back across the
                    # wire; authorization is correlated only by the durable
                    # job/attempt pair.
                    self._spoken_text(job)
                    if job.delivery_state is DeliveryState.AUTHORIZED:
                        # #329: the durable transition precedes the
                        # authorization send, so a client that lost the
                        # socket in that window retries the SAME attempt —
                        # re-ack idempotently rather than revoking. Never
                        # past a pending cancel (mirror the renew branch).
                        if job.cancel_pending:
                            attempt = self._attempts.get(job_id)
                            if attempt is not None:
                                await self._send_revoke(attempt, "cancelled")
                            return
                    else:
                        await self._jobs.authorize(job_id, attempt_id)
                    await bound.send_json({
                        "type": "job_delivery_authorized",
                        "protocol": _PROTOCOL,
                        "job_id": job_id,
                        "delivery_attempt_id": attempt_id,
                    })
                    return

                if frame_type == "job_playback_started":
                    await self._jobs.mark_playing(job_id, attempt_id)
                    return

                if frame_type == "job_delivered":
                    await self._jobs.mark_delivered(job_id, attempt_id)
                    self._attempts.pop(job_id, None)
                    await self._offer_heads_locked()
                    return

                if frame_type == "job_nack":
                    reason = frame.get("reason")
                    if not isinstance(reason, str):
                        reason = "integration_nack"
                    await self._jobs.nack(job_id, attempt_id, reason)
                    self._attempts.pop(job_id, None)
                    if reason in _PARK_REASONS:
                        self._parked_until[job_id] = (
                            self._clock() + self._park_s
                        )
                    await self._offer_heads_locked()
                    return
            except JobTransitionError:
                await self._deny(endpoint, job_id, attempt_id, "stale_attempt")

    async def _recover_due_locked(self) -> None:
        await self._jobs.expire_due()
        await self._jobs.expire_leases()
        self._release_due_parks_locked()
        self._restore_durable_attempts_locked()
        await self._reconcile_attempts_locked()
        self._expire_dead_attempts_locked()

    def _expire_dead_attempts_locked(self) -> None:
        # #329: an attempt whose revoke was never acknowledged (client gone
        # before `job_revoked`) must not pin its entry — and its endpoint
        # socket object — for the process lifetime. Durable-live rows are
        # exempt: restore recreates them, so pruning would only churn.
        now = self._clock()
        for job_id, attempt in list(self._attempts.items()):
            if (
                attempt.revoke_deadline is not None
                and attempt.revoke_deadline <= now
            ):
                self._attempts.pop(job_id, None)

    def _release_due_parks_locked(self) -> None:
        now = self._clock()
        for job_id, deadline in list(self._parked_until.items()):
            job = self._jobs.get(job_id)
            if (
                deadline <= now
                or job is None
                or job.delivery_state is not DeliveryState.READY
            ):
                self._parked_until.pop(job_id, None)

    def _restore_durable_attempts_locked(self) -> None:
        for job in self._jobs.all():
            if (
                job.delivery_state not in {
                    DeliveryState.CLAIMED,
                    DeliveryState.AUTHORIZED,
                    DeliveryState.PLAYING,
                }
                or not job.origin_route_id
                or not job.origin_device_id
                or not job.delivery_attempt_id
                or job.lease_until is None
            ):
                continue
            existing = self._attempts.get(job.id)
            if (
                existing is not None
                and not existing.offered
                and existing.attempt_id == job.delivery_attempt_id
                and existing.route_id == job.origin_route_id
                and existing.device_id == job.origin_device_id
            ):
                continue
            route = self._routes.get_connected(job.origin_route_id)
            endpoint = (
                route if route is not None and self._current_bound(route)
                else None
            )
            self._attempts[job.id] = _Attempt(
                job_id=job.id,
                route_id=job.origin_route_id,
                device_id=job.origin_device_id,
                attempt_id=job.delivery_attempt_id,
                endpoint=endpoint,
                offered=False,
            )

    async def _handle_revoked_ack_locked(
        self,
        bound: Any,
        job_id: str,
        attempt_id: str,
    ) -> None:
        attempt = self._attempts.get(job_id)
        if (
            attempt is None
            or attempt.attempt_id != attempt_id
            or attempt.route_id != bound.route_id
            or attempt.endpoint is not bound
            or not attempt.revoke_sent
        ):
            return
        job = self._jobs.get(job_id)
        if (
            job is not None
            and job.delivery_state is DeliveryState.AUTHORIZED
            and job.cancel_pending
            and job.delivery_attempt_id == attempt_id
        ):
            try:
                await self._jobs.nack(
                    job_id, attempt_id, "preempted_before_playback",
                )
            except JobTransitionError:
                return
        self._attempts.pop(job_id, None)
        await self._offer_heads_locked()

    def _current_bound(self, endpoint: Any) -> Any | None:
        route_id = _identifier(getattr(endpoint, "route_id", None))
        if route_id is None:
            route_id = _identifier(getattr(endpoint, "voice_route_id", None))
        if route_id is None:
            return None
        current = self._routes.get_connected(route_id)
        if current is endpoint:
            bound = current
        elif getattr(current, "connection", None) is endpoint:
            bound = current
        else:
            return None
        capabilities = getattr(bound, "capabilities", None)
        if capabilities is None:
            capabilities = getattr(bound, "voice_route_capabilities", ())
        if not isinstance(capabilities, (set, frozenset, list, tuple)):
            return None
        if not _BACKGROUND_CAPABILITIES <= frozenset(capabilities):
            return None
        return bound

    def _matches_job(
        self,
        bound: Any,
        job: VoiceJob | None,
        *,
        attempt_id: str,
    ) -> bool:
        if job is None or job.origin_route_id != bound.route_id:
            return False
        if not job.origin_device_id:
            return False
        attempt = self._attempts.get(job.id)
        if attempt is not None and (
            attempt.route_id != job.origin_route_id
            or attempt.device_id != job.origin_device_id
        ):
            return False
        if job.delivery_state is DeliveryState.READY:
            return bool(
                attempt is not None
                and attempt.offered
                and attempt.endpoint is bound
                and attempt.attempt_id == attempt_id
            )
        return bool(
            attempt is not None
            and not attempt.offered
            and attempt.endpoint is bound
            and attempt.attempt_id == attempt_id
            and job.delivery_state in _LIVE_STATES
            and job.delivery_attempt_id == attempt_id
        )

    async def _reconcile_attempts_locked(self) -> None:
        for job_id, attempt in list(self._attempts.items()):
            job = self._jobs.get(job_id)
            if job is not None and (
                (attempt.offered and job.delivery_state is DeliveryState.READY)
                or (
                    not attempt.offered
                    and job.delivery_state in {
                        DeliveryState.CLAIMED,
                        DeliveryState.AUTHORIZED,
                        DeliveryState.PLAYING,
                    }
                    and job.delivery_attempt_id == attempt.attempt_id
                )
            ):
                if job.cancel_pending:
                    if not attempt.revoke_sent:
                        attempt.revoke_sent = await self._send_revoke(
                            attempt, "cancelled",
                        )
                continue
            if attempt.revoke_deadline is None:
                # #329: start the reclamation window whether or not the
                # revoke can currently be sent — a route that never
                # reconnects must not retain the attempt forever.
                attempt.revoke_deadline = self._clock() + self._lease_s
            if not attempt.revoke_sent:
                attempt.revoke_sent = await self._send_revoke(
                    attempt, "no_longer_deliverable",
                )

    async def _offer_heads_locked(self) -> None:
        occupied: set[str] = set()
        for job in self._jobs.all():
            if (
                job.delivery_state not in _LIVE_STATES
                or not job.origin_route_id
                or not job.origin_device_id
            ):
                continue
            key = job.origin_device_id
            if key in occupied:
                continue
            occupied.add(key)
            if job.delivery_state is not DeliveryState.READY:
                continue
            if job.id in self._parked_until:
                continue
            if job.delivery_modality not in _DELIVERABLE_MODALITIES:
                # No recorded promise — a row from before endpoint delivery, or
                # one whose endpoint offered nothing. The integration would
                # refuse the frame anyway, so offering it every second would
                # just churn until expiry. Say so once and let it expire.
                if job.id not in self._unoffered_logged:
                    self._unoffered_logged.add(job.id)
                    logger.warning(
                        "voice delivery WAITING reason=no_delivery_modality "
                        "job=%s — this answer records no endpoint it can be "
                        "delivered to, so it will expire unsent.", job.id[:8],
                    )
                continue
            existing = self._attempts.get(job.id)
            if existing is not None and existing.offered:
                continue
            route = self._routes.get_connected(job.origin_route_id)
            if route is None or self._current_bound(route) is None:
                # #233/#224: a READY answer with no live route was skipped
                # SILENTLY on every 1s tick, so a result that never reached the
                # household looked identical to no result at all. Log it once
                # per job (the sweep is per-second — this must not spam).
                if job.id not in self._unoffered_logged:
                    self._unoffered_logged.add(job.id)
                    logger.warning(
                        "voice delivery WAITING reason=route_not_connected "
                        "job=%s — the answer is ready but its origin route is "
                        "not currently connected; it will be offered on "
                        "reconnect, or expire.", job.id[:8],
                    )
                continue
            self._unoffered_logged.discard(job.id)
            attempt = _Attempt(
                job_id=job.id,
                route_id=job.origin_route_id,
                device_id=job.origin_device_id,
                attempt_id=str(uuid.uuid4()),
                endpoint=route,
            )
            spoken_text = self._spoken_text(job)
            self._attempts[job.id] = attempt
            sent = await self._safe_send(route, {
                "type": "job_ready",
                "protocol": _PROTOCOL,
                "job_id": job.id,
                "delivery_attempt_id": attempt.attempt_id,
                "route_id": job.origin_route_id,
                "origin_device_id": job.origin_device_id,
                # The client dispatches on this: announce vs notify.
                "delivery_modality": job.delivery_modality,
                "spoken_text": spoken_text,
                "ready_at": job.terminal_at,
                "expires_at": job.expires_at,
                "delivery_sequence": job.delivery_sequence,
            })
            if not sent:
                self._attempts.pop(job.id, None)

    def _spoken_text(self, job: VoiceJob) -> str:
        # #233: the answer arrives up to a minute after the question, out of
        # nowhere, on a speaker. Attribute it so the listener can place it —
        # channel-owned fixed framing around the specialist's own words. (A
        # richer, varied phrasing is a deliberate follow-up; fixed text first.)
        speaker = job.specialist_display_name or "The specialist"
        if job.result is not None:
            try:
                payload = json.loads(job.result)
                result = parse_voice_job_result(payload)
                spoken = spoken_text_for(
                    result,
                    prompted=job.prompted_delivery,
                    identity_clearance=voice_identity_clearance({
                        "channel": "voice",
                    }),
                )
                # Attribute ONLY the specialist's own answer. A disclosure
                # fallback ("Your result is ready; I can't read private
                # details…") or a clarification prompt is CASA speaking, and
                # prefixing those would both misattribute them and mangle
                # policy-approved wording. Comparing against spoken_summary is
                # the exact test for "this is the specialist's text".
                if spoken and spoken == result.spoken_summary:
                    return voice_phrases.announcement(
                        speaker, spoken, voice_phrases.seed_for(job.id))
                return spoken
            except (json.JSONDecodeError, TypeError, VoiceJobResultError):
                pass
        return (f"I couldn't get an answer from {speaker} — "
                "you can ask me again.")

    async def _send_revoke(self, attempt: _Attempt, reason: str) -> bool:
        current = self._routes.get_connected(attempt.route_id)
        if current is None or current is not attempt.endpoint:
            return False
        return await self._safe_send(current, {
            "type": "job_revoke",
            "protocol": _PROTOCOL,
            "job_id": attempt.job_id,
            "delivery_attempt_id": attempt.attempt_id,
            "reason": reason,
        })

    async def _deny(
        self,
        endpoint: Any,
        job_id: str | None,
        attempt_id: str | None,
        reason: str,
    ) -> None:
        await self._safe_send(endpoint, {
            "type": "job_revoke",
            "protocol": _PROTOCOL,
            "job_id": job_id,
            "delivery_attempt_id": attempt_id,
            "reason": reason,
        })

    @staticmethod
    async def _safe_send(endpoint: Any, frame: dict[str, Any]) -> bool:
        try:
            await endpoint.send_json(frame)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never log result/spoken payloads
            logger.warning(
                "voice delivery frame send failed type=%s job=%s",
                frame.get("type"), str(frame.get("job_id") or "-")[:8],
            )
            return False


__all__ = ["VoiceDeliveryCoordinator"]
