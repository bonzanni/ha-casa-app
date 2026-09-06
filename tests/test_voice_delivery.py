"""Durable proactive voice delivery coordination."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace

import pytest

import voice_phrases
from unittest.mock import AsyncMock

from bus import BusMessage, MessageBus, MessageType
from channels.voice.channel import VoiceChannel, VoiceHandoffCoordinator
from channels.voice.delivery import VoiceDeliveryCoordinator
from channels.voice.routes import VoiceRouteRegistry, VoiceWsConnection
from job_registry import (
    DeliveryState,
    ExecutionState,
    HandoffState,
    JobRegistry,
    VoiceJob,
)
from personality_types import SpeakerProvenance


pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

SYSTEM_SPEAKER = SpeakerProvenance(speaker_kind="system")


def _result(**changes) -> str:
    return json.dumps({
        "status": "answered",
        "spoken_summary": "The policy-approved answer.",
        "answer": "PRIVATE_FULL_RESULT_CANARY",
        "clarification": "",
        "citations": ["PRIVATE_CITATION_CANARY"],
        "assumptions": [],
        "provenance": {},
        "sensitivity": "household",
        "delivery_ttl_s": 900,
        **changes,
    })


def _ready_job(job_id: str, *, sequence: int, device: str, route="entry-1", **changes):
    base = VoiceJob(
        id=job_id,
        parent_job_id=None,
        creating_speaker=SYSTEM_SPEAKER, executing_speaker=SYSTEM_SPEAKER,
        creating_role="concierge",
        specialist_role="judge",
        specialist_display_name="Judge",
        creator_peer="voice",
        creator_user_id=None,
        scope_id="scope-1",
        origin_route_id=route,
        origin_device_id=device,
        task="PRIVATE_TASK_CANARY",
        context="PRIVATE_CONTEXT_CANARY",
        created_at=100.0 + sequence,
        started_at=101.0 + sequence,
        terminal_at=102.0 + sequence,
        expires_at=1000.0,
        execution_state=ExecutionState.SUCCEEDED,
        delivery_state=DeliveryState.READY,
        result=_result(),
        failure=None,
        awaiting_input=False,
        continuable_until=None,
        delivery_sequence=sequence,
        delivery_attempt_id=None,
        lease_until=None,
        cancel_pending=False,
        # Every case here is a deliverable job; the legacy no-modality row has
        # its own test, since it must be offered to nobody.
        delivery_modality="audio",
    )
    return replace(base, **changes)


class _Route:
    def __init__(self, route_id="entry-1", role="concierge") -> None:
        self.route_id = route_id
        self.role = role
        self.capabilities = frozenset({
            "background_jobs", "endpoint_delivery",
        })
        self.sent: list[dict] = []

    async def send_json(self, frame: dict, **_kw) -> None:
        self.sent.append(frame)


class _Routes:
    def __init__(self, *routes: _Route) -> None:
        self.connected = {route.route_id: route for route in routes}

    def get_connected(self, route_id: str):
        return self.connected.get(route_id)


class _SerialRawSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.active_sends = 0
        self.max_concurrent_send = 0

    async def send_json(self, frame: dict) -> None:
        self.active_sends += 1
        self.max_concurrent_send = max(
            self.max_concurrent_send, self.active_sends,
        )
        await asyncio.sleep(0)
        self.sent.append(frame)
        self.active_sends -= 1


class _VoiceCfg:
    class tts:
        tag_dialect = "square_brackets"

    channels = ["ha_voice"]
    voice_errors: dict = {}


@pytest.fixture
async def delivery(tmp_path):
    now = [100.0]
    registry = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: now[0],
    )
    await registry.load()
    route = _Route()
    routes = _Routes(route)
    coordinator = VoiceDeliveryCoordinator(
        registry, routes, lease_s=15, renew_s=5,
    )
    try:
        yield registry, routes, route, coordinator, now
    finally:
        await coordinator.stop()
        await registry.close()


def _offered(route: _Route) -> list[dict]:
    return [frame for frame in route.sent if frame["type"] == "job_ready"]


def _frame(frame_type: str, offer: dict, **changes) -> dict:
    return {
        "type": frame_type,
        "protocol": 2,
        "job_id": offer["job_id"],
        "delivery_attempt_id": offer["delivery_attempt_id"],
        **changes,
    }


async def test_handoff_reoffer_is_route_scoped_and_receipt_stops_replay(
    tmp_path,
):
    registry = JobRegistry(tmp_path / "jobs.json")
    await registry.load()
    await registry.create(_ready_job(
        "job-1", sequence=1, device="kitchen",
        execution_state=ExecutionState.RUNNING,
        delivery_state=DeliveryState.NONE,
        terminal_at=None,
        result=None,
    ))
    await registry.mark_handoff_pending("job-1", "handoff-1")
    coordinator = VoiceHandoffCoordinator(registry)
    route = _Route()

    await coordinator.route_connected(route)
    assert route.sent == [{
        "type": "voice_handoff", "protocol": 2,
        "job_id": "job-1", "handoff_id": "handoff-1",
        "specialist_display_name": "Judge",
    }]

    await coordinator.route_connected(route)
    assert route.sent[-1] == route.sent[0]

    await coordinator.handle(route, {
        "type": "handoff_received", "protocol": 2,
        "job_id": "job-1", "handoff_id": "handoff-1",
        "task": "CLIENT_TEXT_MUST_NOT_BE_USED",
    })
    assert registry.get("job-1").handoff_state is HandoffState.RECEIVED

    await coordinator.route_connected(route)
    assert len(route.sent) == 2


async def test_shared_writer_interleaves_utterances_and_delivery_protocol(
    tmp_path,
):
    now = [100.0]
    jobs = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: now[0],
    )
    await jobs.load()
    raw = _SerialRawSocket()
    connection = VoiceWsConnection(raw)
    configs = {"concierge": _VoiceCfg(), "butler": _VoiceCfg()}
    routes = VoiceRouteRegistry(
        secret_present=True,
        agent_configs=configs,
    )
    bound = await routes.register(connection, {
        "type": "voice_route_register",
        "protocol": 3,
        "route_id": "entry-1",
        "agent_role": "concierge",
        "capabilities": [
            "background_jobs", "endpoint_delivery", "voice_handoff",
        ],
    })
    assert bound is not None
    coordinator = VoiceDeliveryCoordinator(jobs, routes)

    bus = MessageBus()

    async def stream(msg: BusMessage) -> BusMessage:
        on_token = msg.context["_on_token"]
        await on_token(f"{msg.target} first sentence.")
        await asyncio.sleep(0)
        await on_token(f"{msg.target} first sentence. Second sentence.")
        return BusMessage(
            type=MessageType.RESPONSE,
            source=msg.target,
            target=msg.source,
            content="done",
            reply_to=msg.id,
            channel=msg.channel,
            context=msg.context,
        )

    loops = []
    for role in configs:
        bus.register(role, stream)
        loops.append(asyncio.create_task(bus.run_agent_loop(role)))
    channel = VoiceChannel(
        bus=bus,
        default_agent="concierge",
        webhook_secret="secret",
        sse_path="/sse",
        ws_path="/ws",
        agent_configs=configs,
        memory=AsyncMock(),
        idle_timeout=300,
        route_registry=routes,
        delivery_coordinator=coordinator,
    )
    await jobs.create(_ready_job("job-1", sequence=1, device="kitchen"))

    async def delivery_frames() -> None:
        await coordinator.route_connected(bound)
        offer = next(
            frame for frame in raw.sent if frame["type"] == "job_ready"
        )
        await coordinator.handle(connection, _frame("job_claimed", offer))
        now[0] = 104.0
        await coordinator.handle(
            connection, _frame("job_claim_renew", offer),
        )
        await coordinator.handle(
            connection, _frame("job_delivery_start", offer),
        )
        await jobs.request_cancel("job-1", actor={
            "creator_peer": "voice",
            "creator_user_id": None,
            "scope_id": "scope-1",
        })
        await coordinator.sweep_once()

    deadline = asyncio.get_running_loop().time() + 10
    try:
        await asyncio.gather(
            channel._run_ws_utterance(connection, {
                "text": "one",
                "agent_role": "concierge",
                "scope_id": "scope-1",
            }, "u1", deadline),
            channel._run_ws_utterance(connection, {
                "text": "two",
                "agent_role": "butler",
                "scope_id": "scope-2",
            }, "u2", deadline),
            delivery_frames(),
        )
    finally:
        for loop in loops:
            loop.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        await coordinator.stop()
        await jobs.close()

    frame_types = [frame["type"] for frame in raw.sent]
    assert {frame["utterance_id"] for frame in raw.sent
            if frame["type"] == "done"} == {"u1", "u2"}
    assert {
        "job_ready", "job_delivery_authorized", "job_revoke",
    } <= set(frame_types)
    assert all(isinstance(frame, dict) for frame in raw.sent)
    assert raw.max_concurrent_send == 1


async def test_only_device_queue_head_is_offered(delivery):
    """Pins INV-JOB-005. Red case demonstrated: removing the per-device occupancy check from the offer pass fails this test."""
    registry, _, route, coordinator, _ = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await registry.create(_ready_job("job-2", sequence=2, device="kitchen"))

    await coordinator.route_connected(route)
    assert [frame["job_id"] for frame in _offered(route)] == ["job-1"]

    offer = _offered(route)[0]
    await coordinator.handle(route, _frame("job_claimed", offer))
    await coordinator.handle(route, _frame("job_delivery_start", offer))
    await coordinator.handle(route, _frame("job_playback_started", offer))
    await coordinator.handle(route, _frame("job_delivered", offer))

    assert [frame["job_id"] for frame in _offered(route)] == [
        "job-1", "job-2",
    ]


async def test_delivered_lru_reoffer_can_close_claim_without_fake_playback(
    delivery,
):
    registry, _, route, coordinator, _ = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await coordinator.route_connected(route)
    offer = _offered(route)[0]

    await coordinator.handle(route, _frame("job_claimed", offer))
    await coordinator.handle(route, _frame("job_delivered", offer))

    current = registry.get("job-1")
    assert current.delivery_state is DeliveryState.DELIVERED
    assert current.delivery_attempt_id is None
    assert not any(
        frame["type"] in {"job_delivery_authorized", "job_playback_started"}
        for frame in route.sent
    )


async def test_different_devices_progress_independently(delivery):
    registry, _, route, coordinator, _ = delivery
    await registry.create(_ready_job("job-k", sequence=1, device="kitchen"))
    await registry.create(_ready_job("job-o", sequence=2, device="office"))

    await coordinator.route_connected(route)

    assert {frame["job_id"] for frame in _offered(route)} == {
        "job-k", "job-o",
    }


async def test_device_fifo_is_stable_across_route_ids(delivery):
    registry, routes, route, coordinator, _ = delivery
    other_route = _Route(route_id="entry-2")
    routes.connected["entry-2"] = other_route
    await registry.create(_ready_job(
        "job-1", sequence=1, device="shared-device", route="entry-1",
    ))
    await registry.create(_ready_job(
        "job-2", sequence=2, device="shared-device", route="entry-2",
    ))

    await coordinator.route_connected(route)
    await coordinator.route_connected(other_route)

    assert [frame["job_id"] for frame in _offered(route)] == ["job-1"]
    assert _offered(other_route) == []


async def test_offer_contains_only_policy_approved_spoken_text(delivery):
    registry, _, route, coordinator, _ = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))

    await coordinator.route_connected(route)

    offer = _offered(route)[0]
    # v0.120.0 (#233): the answer lands on a speaker up to a minute after the
    # question, so the specialist's OWN text is attributed. Disclosure
    # fallbacks and clarifications stay verbatim (see the private cases).
    assert offer["spoken_text"] == voice_phrases.announcement(
        "Judge", "The policy-approved answer.", voice_phrases.seed_for("job-1"))
    assert offer == {
        "type": "job_ready",
        "protocol": 2,
        "job_id": "job-1",
        "delivery_attempt_id": offer["delivery_attempt_id"],
        "route_id": "entry-1",
        "origin_device_id": "kitchen",
        "spoken_text": voice_phrases.announcement(
            "Judge", "The policy-approved answer.",
            voice_phrases.seed_for("job-1")),
        "ready_at": 103.0,
        "expires_at": 1000.0,
        "delivery_sequence": 1,
        # The client dispatches announce-vs-notify on this (#233/#224).
        "delivery_modality": "audio",
    }
    serialized = json.dumps(offer)
    assert "PRIVATE_FULL_RESULT_CANARY" not in serialized
    assert "PRIVATE_CITATION_CANARY" not in serialized
    assert "PRIVATE_TASK_CANARY" not in serialized


async def test_private_disclosure_is_re_evaluated_before_authorization(
    delivery, monkeypatch,
):
    registry, _, route, coordinator, _ = delivery
    from channels.voice import delivery as delivery_module
    real_spoken_text_for = delivery_module.spoken_text_for
    evaluations = []

    def recording_spoken_text_for(*args, **kwargs):
        evaluations.append((args, kwargs))
        return real_spoken_text_for(*args, **kwargs)

    monkeypatch.setattr(
        delivery_module, "spoken_text_for", recording_spoken_text_for,
    )
    await registry.create(_ready_job(
        "job-1",
        sequence=1,
        device="kitchen",
        result=_result(
            spoken_summary="PRIVATE_SPOKEN_CANARY",
            sensitivity="private",
        ),
    ))
    await coordinator.route_connected(route)
    offer = _offered(route)[0]
    assert offer["spoken_text"] == "Your result is ready; ask me for the details."

    await coordinator.handle(route, _frame("job_claimed", offer))
    await coordinator.handle(route, _frame("job_delivery_start", offer))

    authorized = [
        frame for frame in route.sent
        if frame["type"] == "job_delivery_authorized"
    ]
    assert authorized[-1] == {
        "type": "job_delivery_authorized",
        "protocol": 2,
        "job_id": "job-1",
        "delivery_attempt_id": offer["delivery_attempt_id"],
    }
    assert len(evaluations) == 2
    assert "PRIVATE_SPOKEN_CANARY" not in json.dumps(route.sent)


async def test_recently_disconnected_route_accepts_then_waits_and_reoffers(
    tmp_path,
):
    now = [100.0]
    routes = VoiceRouteRegistry(
        secret_present=True,
        agent_configs={"concierge": _VoiceCfg()},
        freshness_s=60,
        clock=lambda: now[0],
    )
    first_raw = _SerialRawSocket()
    first = VoiceWsConnection(first_raw)
    await routes.register(first, {
        "type": "voice_route_register",
        "protocol": 3,
        "route_id": "entry-1",
        "agent_role": "concierge",
        "capabilities": [
            "background_jobs", "endpoint_delivery", "voice_handoff",
        ],
    })
    await routes.disconnect(first)
    now[0] = 159.0
    assert routes.is_recently_capable("entry-1") is True

    registry = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: now[0],
    )
    await registry.load()
    await registry.create(_ready_job(
        "job-1",
        sequence=0,
        device="kitchen",
        created_at=now[0],
        started_at=now[0],
        terminal_at=None,
        expires_at=None,
        execution_state=ExecutionState.RUNNING,
        delivery_state=DeliveryState.NONE,
        result=None,
        delivery_sequence=0,
    ))
    now[0] = 161.0
    await registry.finish_voice_result(
        "job-1", _result(), awaiting_input=False, delivery_ttl_s=900,
    )
    coordinator = VoiceDeliveryCoordinator(registry, routes, clock=lambda: now[0])
    await coordinator.sweep_once()
    assert registry.get("job-1").delivery_state is DeliveryState.READY
    assert [frame for frame in first_raw.sent if frame.get("type") == "job_ready"] == []

    now[0] = 162.0
    second_raw = _SerialRawSocket()
    second = VoiceWsConnection(second_raw)
    bound = await routes.register(second, {
        "type": "voice_route_register",
        "protocol": 3,
        "route_id": "entry-1",
        "agent_role": "concierge",
        "capabilities": [
            "background_jobs", "endpoint_delivery", "voice_handoff",
        ],
    })
    await coordinator.route_connected(bound)
    assert [
        frame["job_id"] for frame in second_raw.sent
        if frame.get("type") == "job_ready"
    ] == ["job-1"]


async def test_result_ttl_expiring_across_restart_never_emits_ready_frame(
    tmp_path,
):
    now = [100.0]
    first = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: now[0],
    )
    await first.load()
    await first.create(_ready_job(
        "job-expiring",
        sequence=1,
        device="kitchen",
        expires_at=160.0,
    ))

    now[0] = 161.0
    restarted = JobRegistry(
        tmp_path / "jobs.json",
        clock=lambda: now[0],
    )
    await restarted.load()
    await restarted.expire_due()
    route = _Route()
    routes = _Routes(route)
    coordinator = VoiceDeliveryCoordinator(restarted, routes, clock=lambda: now[0])
    await coordinator.route_connected(route)

    assert restarted.get("job-expiring").delivery_state is DeliveryState.EXPIRED
    assert _offered(route) == []


@pytest.mark.parametrize(
    ("sensitivity", "expected"),
    [
        # household: the specialist's own text -> attributed (v0.120.0 #233).
        ("household", None),   # attributed; computed from the seed below
        (
            # private: a disclosure fallback is CASA speaking -> verbatim, and
            # crucially unchanged by the attribution work.
            "private",
            "Your result is ready; I can't read private details on this voice route.",
        ),
    ],
)
async def test_prompted_detail_child_applies_current_route_clearance(
    delivery, sensitivity, expected,
):
    registry, _, route, coordinator, _ = delivery
    await registry.create(_ready_job(
        "job-detail",
        sequence=1,
        device="kitchen",
        prompted_delivery=True,
        result=_result(
            spoken_summary=(
                "HOUSEHOLD_PROMPTED_DETAIL_CANARY"
                if sensitivity == "household"
                else "PRIVATE_PROMPTED_DETAIL_CANARY"
            ),
            sensitivity=sensitivity,
        ),
    ))

    await coordinator.route_connected(route)

    if expected is None:
        # The specialist's OWN answer is attributed (#233) — wording varies by
        # seed, so derive it rather than pinning a variant.
        expected = voice_phrases.announcement(
            "Judge", "HOUSEHOLD_PROMPTED_DETAIL_CANARY",
            voice_phrases.seed_for(_offered(route)[0]["job_id"]))
    assert _offered(route)[0]["spoken_text"] == expected
    assert "PRIVATE_PROMPTED_DETAIL_CANARY" not in json.dumps(route.sent)


async def test_claim_persists_attempt_before_authorization(delivery):
    registry, _, route, coordinator, _ = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await coordinator.route_connected(route)
    offer = _offered(route)[0]

    await coordinator.handle(route, _frame("job_claimed", offer))

    job = registry.get("job-1")
    assert job.delivery_state is DeliveryState.CLAIMED
    assert job.delivery_attempt_id == offer["delivery_attempt_id"]
    assert job.lease_until == 115.0
    assert not any(
        frame["type"] == "job_delivery_authorized" for frame in route.sent
    )


async def test_stale_claim_is_denied_without_mutation(delivery):
    registry, _, route, coordinator, _ = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await coordinator.route_connected(route)
    offer = _offered(route)[0]
    before = registry.get("job-1")

    await coordinator.handle(route, _frame(
        "job_claimed", offer, delivery_attempt_id="stale-attempt",
    ))

    assert registry.get("job-1") == before
    assert route.sent[-1]["type"] == "job_revoke"


async def test_wrong_connection_for_stable_route_is_denied_without_mutation(delivery):
    registry, routes, route, coordinator, _ = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await coordinator.route_connected(route)
    offer = _offered(route)[0]
    stale_connection = _Route()
    assert routes.get_connected("entry-1") is route
    before = registry.get("job-1")

    await coordinator.handle(stale_connection, _frame("job_claimed", offer))

    assert registry.get("job-1") == before
    assert stale_connection.sent[-1]["type"] == "job_revoke"


async def test_reconnect_discards_offer_and_generates_new_attempt(delivery):
    registry, routes, route, coordinator, _ = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await coordinator.route_connected(route)
    old_offer = _offered(route)[0]

    new_route = _Route()
    routes.connected["entry-1"] = new_route
    await coordinator.route_connected(new_route)
    new_offer = _offered(new_route)[0]

    assert new_offer["job_id"] == old_offer["job_id"]
    assert (
        new_offer["delivery_attempt_id"]
        != old_offer["delivery_attempt_id"]
    )
    await coordinator.handle(new_route, _frame("job_claimed", old_offer))
    assert registry.get("job-1").delivery_state is DeliveryState.READY
    assert new_route.sent[-1]["type"] == "job_revoke"


async def test_claimed_attempt_rebinds_to_authenticated_reconnect_for_revoke(
    delivery,
):
    registry, routes, route, coordinator, _ = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await coordinator.route_connected(route)
    offer = _offered(route)[0]
    await coordinator.handle(route, _frame("job_claimed", offer))

    new_route = _Route()
    routes.connected["entry-1"] = new_route
    await coordinator.route_connected(new_route)
    await registry.request_cancel("job-1", actor={
        "creator_peer": "voice", "creator_user_id": None,
        "scope_id": "scope-1",
    })
    await coordinator.sweep_once()

    assert new_route.sent[-1]["type"] == "job_revoke"
    assert new_route.sent[-1]["delivery_attempt_id"] == (
        offer["delivery_attempt_id"]
    )


async def test_missed_authorization_can_be_retried_after_reconnect(delivery):
    """#329: `job_delivery_start` durably transitions to AUTHORIZED before
    `job_delivery_authorized` is sent. A client that lost the socket in
    that window could never retry — authorize() rejected the now-AUTHORIZED
    row, the coordinator revoked, and the result sat until lease expiry."""
    registry, routes, route, coordinator, _ = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await coordinator.route_connected(route)
    offer = _offered(route)[0]
    await coordinator.handle(route, _frame("job_claimed", offer))
    await coordinator.handle(route, _frame("job_delivery_start", offer))
    assert registry.get("job-1").delivery_state is DeliveryState.AUTHORIZED

    # The authorization frame died with the socket; the client reconnects
    # and retries the SAME attempt.
    new_route = _Route()
    routes.connected["entry-1"] = new_route
    await coordinator.route_connected(new_route)
    await coordinator.handle(new_route, _frame("job_delivery_start", offer))

    authorized = [
        frame for frame in new_route.sent
        if frame["type"] == "job_delivery_authorized"
    ]
    assert authorized
    assert authorized[-1]["delivery_attempt_id"] == (
        offer["delivery_attempt_id"]
    )
    assert not any(
        frame["type"] == "job_revoke" for frame in new_route.sent
    )

    # The retried delivery then completes normally.
    await coordinator.handle(new_route, _frame("job_playback_started", offer))
    await coordinator.handle(new_route, _frame("job_delivered", offer))
    assert registry.get("job-1").delivery_state is DeliveryState.DELIVERED


async def test_delivery_start_retry_with_pending_cancel_is_revoked(delivery):
    """#329 guard: an idempotent re-authorization must not bypass a pending
    cancellation — mirror the renew branch's revoke."""
    registry, _, route, coordinator, _ = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await coordinator.route_connected(route)
    offer = _offered(route)[0]
    await coordinator.handle(route, _frame("job_claimed", offer))
    await coordinator.handle(route, _frame("job_delivery_start", offer))
    await registry.request_cancel("job-1", actor={
        "creator_peer": "voice", "creator_user_id": None,
        "scope_id": "scope-1",
    })
    sent_before = len(route.sent)

    await coordinator.handle(route, _frame("job_delivery_start", offer))

    assert route.sent[-1]["type"] == "job_revoke"
    assert not any(
        frame["type"] == "job_delivery_authorized"
        for frame in route.sent[sent_before:]
    )


async def test_unacked_revoked_attempt_is_expired_not_retained(delivery):
    """#329: a reconciliation-revoked attempt was removed only by the
    client's `job_revoked` ack — a client that disconnected before acking
    pinned the attempt (and its socket object) for the process lifetime."""
    registry, routes, route, old_coordinator, now = delivery
    coordinator = VoiceDeliveryCoordinator(
        registry, routes, lease_s=15, renew_s=5, clock=lambda: now[0],
    )
    await registry.create(_ready_job(
        "job-1", sequence=1, device="kitchen", expires_at=105.0,
    ))
    await coordinator.route_connected(route)
    offer = _offered(route)[0]
    await coordinator.handle(route, _frame("job_claimed", offer))

    # TTL expiry makes the durable row terminal; reconciliation revokes.
    now[0] = 106.0
    await coordinator.sweep_once()
    assert any(frame["type"] == "job_revoke" for frame in route.sent)
    assert "job-1" in coordinator._attempts

    # No ack ever arrives; the attempt must still be reclaimed.
    now[0] = 130.0
    await coordinator.sweep_once()
    assert "job-1" not in coordinator._attempts
    await coordinator.stop()
    await old_coordinator.stop()


async def test_stale_disconnect_notification_spares_the_new_bindings_offer(
    delivery,
):
    """#304 (review round 2): a late route-disconnected notification for a
    DISPLACED binding must remove only attempts offered to that binding —
    matching by route id alone tore down the offer a newer socket had just
    received for the reused id."""
    registry, routes, route, coordinator, _ = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await coordinator.route_connected(route)
    displaced = route

    # Socket C takes over the same route id and gets its own fresh offer.
    new_route = _Route()
    routes.connected["entry-1"] = new_route
    await coordinator.route_connected(new_route)
    offer = _offered(new_route)[-1]

    # The displaced binding's disconnect notification arrives late.
    await coordinator.route_disconnected(displaced)

    await coordinator.handle(new_route, _frame("job_claimed", offer))
    assert registry.get("job-1").delivery_state is DeliveryState.CLAIMED


async def test_lease_renewal_and_lapse_reoffer_with_new_attempt(delivery):
    registry, _, route, coordinator, now = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await coordinator.route_connected(route)
    first = _offered(route)[0]
    await coordinator.handle(route, _frame("job_claimed", first))

    now[0] = 104.0
    await coordinator.handle(route, _frame("job_claim_renew", first))
    assert registry.get("job-1").lease_until == 119.0
    now[0] = 120.0
    await coordinator.sweep_once()

    assert registry.get("job-1").delivery_state is DeliveryState.READY
    assert len(_offered(route)) == 2
    assert (
        _offered(route)[1]["delivery_attempt_id"]
        != first["delivery_attempt_id"]
    )


async def test_late_renew_cannot_revive_an_expired_lease(delivery):
    registry, _, route, coordinator, now = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await coordinator.route_connected(route)
    first = _offered(route)[0]
    await coordinator.handle(route, _frame("job_claimed", first))
    now[0] = 116.0

    await coordinator.handle(route, _frame("job_claim_renew", first))

    job = registry.get("job-1")
    assert job.delivery_state is DeliveryState.READY
    assert job.delivery_attempt_id is None
    assert route.sent[-1]["type"] == "job_revoke"


async def test_ttl_expiry_revokes_offer_and_never_offers_followup_early(delivery):
    registry, _, route, coordinator, now = delivery
    await registry.create(_ready_job(
        "job-1", sequence=1, device="kitchen", expires_at=105.0,
    ))
    await registry.create(_ready_job("job-2", sequence=2, device="kitchen"))
    await coordinator.route_connected(route)
    assert [frame["job_id"] for frame in _offered(route)] == ["job-1"]

    now[0] = 106.0
    await coordinator.sweep_once()

    assert registry.get("job-1").delivery_state is DeliveryState.EXPIRED
    assert route.sent[-2]["type"] == "job_revoke"
    assert route.sent[-1]["type"] == "job_ready"
    assert route.sent[-1]["job_id"] == "job-2"


async def test_route_reconnect_never_offers_a_result_past_ttl(delivery):
    registry, _, route, coordinator, _ = delivery
    await registry.create(_ready_job(
        "job-1", sequence=1, device="kitchen", expires_at=99.0,
    ))

    await coordinator.route_connected(route)

    assert _offered(route) == []
    assert registry.get("job-1").delivery_state is DeliveryState.EXPIRED


async def test_ready_cancellation_revokes_offer_and_releases_fifo(delivery):
    registry, _, route, coordinator, _ = delivery
    first = _ready_job("job-1", sequence=1, device="kitchen")
    await registry.create(first)
    await registry.create(_ready_job("job-2", sequence=2, device="kitchen"))
    await coordinator.route_connected(route)
    await registry.request_cancel("job-1", actor={
        "creator_peer": "voice", "creator_user_id": None,
        "scope_id": "scope-1",
    })

    await coordinator.sweep_once()

    assert registry.get("job-1").delivery_state is DeliveryState.CANCELLED
    assert [frame["type"] for frame in route.sent[-2:]] == [
        "job_revoke", "job_ready",
    ]


async def test_claimed_cancellation_revokes_and_revoked_is_idempotent(delivery):
    registry, _, route, coordinator, _ = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await coordinator.route_connected(route)
    offer = _offered(route)[0]
    await coordinator.handle(route, _frame("job_claimed", offer))
    result = await registry.request_cancel("job-1", actor={
        "creator_peer": "voice", "creator_user_id": None,
        "scope_id": "scope-1",
    })
    assert result.status == "cancelled"

    await coordinator.sweep_once()
    before = registry.get("job-1")
    sent_before_ack = list(route.sent)
    await coordinator.handle(route, _frame("job_revoked", offer))
    await coordinator.handle(route, _frame("job_revoked", offer))

    assert route.sent == sent_before_ack
    assert registry.get("job-1") == before


async def test_restart_reconstructs_live_attempts_and_blocks_pending_cancel_renewal(
    delivery,
):
    registry, routes, route, old_coordinator, now = delivery
    attempt_id = "durable-attempt"
    await registry.create(_ready_job(
        "claimed-job",
        sequence=1,
        device="kitchen",
        delivery_state=DeliveryState.CLAIMED,
        delivery_attempt_id=attempt_id,
        lease_until=115.0,
    ))
    await registry.create(_ready_job(
        "authorized-job",
        sequence=2,
        device="office",
        delivery_state=DeliveryState.AUTHORIZED,
        delivery_attempt_id="authorized-attempt",
        lease_until=115.0,
        cancel_pending=True,
    ))
    await registry.create(_ready_job(
        "playing-job",
        sequence=3,
        device="bedroom",
        delivery_state=DeliveryState.PLAYING,
        delivery_attempt_id="playing-attempt",
        lease_until=115.0,
    ))
    coordinator = VoiceDeliveryCoordinator(
        registry, routes, lease_s=15, renew_s=5,
    )

    await coordinator.route_connected(route)

    assert {
        (frame["job_id"], frame["delivery_attempt_id"])
        for frame in route.sent if frame["type"] == "job_revoke"
    } == {("authorized-job", "authorized-attempt")}

    now[0] = 104.0
    await coordinator.handle(route, {
        "type": "job_claim_renew",
        "protocol": 2,
        "job_id": "claimed-job",
        "delivery_attempt_id": attempt_id,
    })
    await coordinator.handle(route, {
        "type": "job_claim_renew",
        "protocol": 2,
        "job_id": "playing-job",
        "delivery_attempt_id": "playing-attempt",
    })
    pending_before = registry.get("authorized-job")
    await coordinator.handle(route, {
        "type": "job_claim_renew",
        "protocol": 2,
        "job_id": "authorized-job",
        "delivery_attempt_id": "authorized-attempt",
    })

    assert registry.get("claimed-job").lease_until == 119.0
    assert registry.get("playing-job").lease_until == 119.0
    assert registry.get("authorized-job").lease_until == (
        pending_before.lease_until
    )
    assert route.sent[-1]["type"] == "job_revoke"
    await coordinator.stop()
    await old_coordinator.stop()


@pytest.mark.parametrize("reason", [
    "satellite_not_found",
    "satellite_ambiguous",
])
async def test_satellite_mapping_nack_parks_until_backoff(reason, delivery):
    registry, routes, route, old_coordinator, now = delivery
    coordinator = VoiceDeliveryCoordinator(
        registry,
        routes,
        lease_s=15,
        renew_s=5,
        park_s=30,
        clock=lambda: now[0],
    )
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await coordinator.route_connected(route)
    offer = _offered(route)[0]
    await coordinator.handle(route, _frame("job_claimed", offer))

    await coordinator.handle(route, _frame("job_nack", offer, reason=reason))
    await coordinator.sweep_once()
    now[0] = 129.999
    await coordinator.sweep_once()

    assert registry.get("job-1").delivery_state is DeliveryState.READY
    assert len(_offered(route)) == 1

    now[0] = 130.0
    await coordinator.sweep_once()
    assert len(_offered(route)) == 2
    assert _offered(route)[1]["delivery_attempt_id"] != (
        offer["delivery_attempt_id"]
    )
    await coordinator.stop()
    await old_coordinator.stop()


async def test_satellite_mapping_park_retries_on_route_reconnect(delivery):
    registry, routes, route, old_coordinator, now = delivery
    coordinator = VoiceDeliveryCoordinator(
        registry,
        routes,
        lease_s=15,
        renew_s=5,
        park_s=30,
        clock=lambda: now[0],
    )
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await coordinator.route_connected(route)
    offer = _offered(route)[0]
    await coordinator.handle(route, _frame("job_claimed", offer))
    await coordinator.handle(route, _frame(
        "job_nack", offer, reason="satellite_not_found",
    ))

    replacement = _Route()
    routes.connected[replacement.route_id] = replacement
    await coordinator.route_connected(replacement)

    assert len(_offered(route)) == 1
    assert [frame["job_id"] for frame in _offered(replacement)] == ["job-1"]
    await coordinator.stop()
    await old_coordinator.stop()


async def test_satellite_mapping_park_still_expires_at_ttl(delivery):
    registry, routes, route, old_coordinator, now = delivery
    coordinator = VoiceDeliveryCoordinator(
        registry,
        routes,
        lease_s=15,
        renew_s=5,
        park_s=30,
        clock=lambda: now[0],
    )
    await registry.create(_ready_job(
        "job-1", sequence=1, device="kitchen", expires_at=105.0,
    ))
    await coordinator.route_connected(route)
    offer = _offered(route)[0]
    await coordinator.handle(route, _frame("job_claimed", offer))
    await coordinator.handle(route, _frame(
        "job_nack", offer, reason="satellite_ambiguous",
    ))

    now[0] = 106.0
    await coordinator.sweep_once()

    assert registry.get("job-1").delivery_state is DeliveryState.EXPIRED
    assert len(_offered(route)) == 1
    await coordinator.stop()
    await old_coordinator.stop()


async def test_sweep_task_logs_transient_failure_and_keeps_running(
    delivery, monkeypatch, caplog,
):
    _, _, _, coordinator, _ = delivery
    calls = 0
    recovered = asyncio.Event()

    async def flaky_sweep():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("PRIVATE_TRANSIENT_CANARY")
        recovered.set()

    monkeypatch.setattr(coordinator, "sweep_once", flaky_sweep)
    with caplog.at_level(logging.ERROR):
        await coordinator.start()
        await asyncio.wait_for(recovered.wait(), timeout=2.0)

    assert coordinator._task is not None
    assert not coordinator._task.done()
    assert "voice delivery sweep failed" in caplog.text
    assert "PRIVATE_TRANSIENT_CANARY" not in caplog.text


async def test_authorized_cancel_nack_becomes_cancelled_not_ready(delivery):
    registry, _, route, coordinator, _ = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await coordinator.route_connected(route)
    offer = _offered(route)[0]
    await coordinator.handle(route, _frame("job_claimed", offer))
    await coordinator.handle(route, _frame("job_delivery_start", offer))
    result = await registry.request_cancel("job-1", actor={
        "creator_peer": "voice", "creator_user_id": None,
        "scope_id": "scope-1",
    })
    assert result.status == "stopping"
    await coordinator.sweep_once()

    await coordinator.handle(route, _frame(
        "job_nack", offer, reason="preempted_before_playback",
    ))

    job = registry.get("job-1")
    assert job.delivery_state is DeliveryState.CANCELLED
    assert job.cancel_pending is False


async def test_authorized_cancel_survives_integration_death(delivery):
    registry, routes, route, coordinator, now = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await coordinator.route_connected(route)
    offer = _offered(route)[0]
    await coordinator.handle(route, _frame("job_claimed", offer))
    await coordinator.handle(route, _frame("job_delivery_start", offer))
    await registry.request_cancel("job-1", actor={
        "creator_peer": "voice", "creator_user_id": None,
        "scope_id": "scope-1",
    })
    routes.connected.clear()
    now[0] = 116.0

    await coordinator.sweep_once()

    assert registry.get("job-1").delivery_state is DeliveryState.CANCELLED


async def test_playing_cancellation_is_too_late(delivery):
    registry, _, route, coordinator, _ = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    await coordinator.route_connected(route)
    offer = _offered(route)[0]
    await coordinator.handle(route, _frame("job_claimed", offer))
    await coordinator.handle(route, _frame("job_delivery_start", offer))
    await coordinator.handle(route, _frame("job_playback_started", offer))
    before = registry.get("job-1")

    result = await registry.request_cancel("job-1", actor={
        "creator_peer": "voice", "creator_user_id": None,
        "scope_id": "scope-1",
    })

    assert result.status == "too_late"
    assert registry.get("job-1") == before


async def test_unknown_and_old_protocol_frames_are_ignored(delivery):
    registry, _, route, coordinator, _ = delivery
    await registry.create(_ready_job("job-1", sequence=1, device="kitchen"))
    before = registry.get("job-1")

    await coordinator.handle(route, {"type": "future_frame", "protocol": 2})
    await coordinator.handle(route, {"type": "job_claimed", "protocol": 0})
    await coordinator.handle(route, {"type": "job_claimed", "protocol": True})
    await coordinator.handle(route, {"type": "job_claimed", "protocol": 2.0})

    assert registry.get("job-1") == before
    assert route.sent == []


@pytest.mark.asyncio
async def test_a_job_with_no_recorded_endpoint_is_never_offered(delivery):
    """Fail closed, and stop — do not churn.

    A row from before endpoint delivery, or one whose endpoint offered nothing,
    records no promise. The integration refuses such a frame, so offering it on
    every sweep would re-send it once a second until expiry. It must be offered
    to nobody — and it must not hold the device's queue hostage either: the
    next answer for that device gets its turn once the undeliverable one
    expires.
    """
    registry, _, route, coordinator, now = delivery
    await registry.create(_ready_job(
        "legacy-1", sequence=1, device="kitchen",
        delivery_modality=None, expires_at=200.0))
    await registry.create(_ready_job(
        "job-2", sequence=2, device="kitchen", expires_at=1000.0))

    await coordinator.route_connected(route)
    assert _offered(route) == [], "the undeliverable head must not be offered"

    # ...and it must not starve what is behind it.
    now[0] = 201.0
    await registry.expire_due()
    await coordinator.sweep_once()

    assert [frame["job_id"] for frame in _offered(route)] == ["job-2"]


@pytest.mark.asyncio
async def test_an_unknown_modality_is_treated_as_no_endpoint(delivery):
    """Pins INV-VOICE-006. Red case demonstrated: narrowing the modality check to `is None` offers the unknown modality and fails this test."""
    registry, _, route, coordinator, _ = delivery
    await registry.create(_ready_job(
        "weird-1", sequence=1, device="kitchen", delivery_modality="telepathy"))

    await coordinator.route_connected(route)

    assert _offered(route) == []


@pytest.mark.asyncio
async def test_a_notification_that_never_left_is_reoffered_not_lost(delivery):
    """The recovery half of a failed push (#233/#224).

    The integration reaches `job_playback_started` and only then does the
    notification fail. It deliberately does NOT acknowledge delivery, so the
    answer must not be retired: the lease lapses, the job returns to READY, and
    it is offered again under a fresh attempt. Without this the operator's
    answer would sit in PLAYING until it expired — the same silent loss, one
    step further along.
    """
    registry, _, route, coordinator, now = delivery
    await registry.create(_ready_job(
        "job-t", sequence=1, device="phone", delivery_modality="text"))
    await coordinator.route_connected(route)
    first = _offered(route)[0]
    assert first["delivery_modality"] == "text"

    await coordinator.handle(route, _frame("job_claimed", first))
    await coordinator.handle(route, _frame("job_delivery_start", first))
    await coordinator.handle(route, _frame("job_playback_started", first))
    assert registry.get("job-t").delivery_state is DeliveryState.PLAYING

    # The push fails. No `job_delivered` follows; the lease simply lapses.
    now[0] = 200.0
    await coordinator.sweep_once()

    assert registry.get("job-t").delivery_state is DeliveryState.READY
    retry = _offered(route)[-1]
    assert retry["delivery_attempt_id"] != first["delivery_attempt_id"]
    assert retry["delivery_modality"] == "text"

    # The retry can now complete normally.
    await coordinator.handle(route, _frame("job_claimed", retry))
    await coordinator.handle(route, _frame("job_delivery_start", retry))
    await coordinator.handle(route, _frame("job_playback_started", retry))
    await coordinator.handle(route, _frame("job_delivered", retry))

    assert registry.get("job-t").delivery_state is DeliveryState.DELIVERED


async def test_deleted_row_mid_attempt_is_revoked_and_never_offered(delivery):
    """#862: retention deletes a row a local attempt still references.

    The deletion must be handled exactly as the EXPIRED flip was — one
    `job_revoke` for that id, the local attempt reclaimed, no further offer —
    so a record leaving the file at its deadline cannot strand a live delivery.

    The coordinator is built with the fixture's clock: the shared fixture's own
    coordinator reads `time.monotonic`, so advancing `now` would not reach its
    reclamation window (astra, red-case acceptance round 1).
    """
    registry, routes, route, old_coordinator, now = delivery
    coordinator = VoiceDeliveryCoordinator(
        registry, routes, lease_s=15, renew_s=5, clock=lambda: now[0],
    )
    await registry.create(_ready_job(
        "job-1", sequence=1, device="kitchen", expires_at=105.0,
    ))
    await coordinator.route_connected(route)
    offer = _offered(route)[0]
    await coordinator.handle(route, _frame("job_claimed", offer))

    now[0] = 106.0
    await coordinator.sweep_once()

    assert len(registry.all()) == 0
    assert registry.get("job-1") is None
    assert len([
        frame for frame in route.sent
        if frame["type"] == "job_revoke" and frame["job_id"] == "job-1"
    ]) == 1
    assert len([
        frame for frame in _offered(route) if frame["job_id"] == "job-1"
    ]) == 1

    now[0] = 130.0
    await coordinator.sweep_once()
    assert "job-1" not in coordinator._attempts
    assert len([
        frame for frame in _offered(route) if frame["job_id"] == "job-1"
    ]) == 1
    await coordinator.stop()
    await old_coordinator.stop()
