"""Regression tests for casa_core helpers.

Covers two runtime bugs the Phase 2.1 review surfaced that still live
on build_invoke_message (the heartbeat helpers were removed in the
Phase 4.x agent-definition cut; scheduled-message shape is now covered
by tests/test_trigger_registry.py).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from broker_helpers import deliver

from bus import MessageType
from session_registry import build_session_key


# ---------------------------------------------------------------------------
# Invoke: each call gets its own session key
# ---------------------------------------------------------------------------


def test_build_invoke_message_caller_supplied_chat_id_wins():
    from casa_core import build_invoke_message

    msg = build_invoke_message(
        agent_role="assistant",
        prompt="hi",
        payload={"context": {"chat_id": "user-A"}},
    )
    assert msg.context["chat_id"] == "user-A"
    assert build_session_key(msg.channel, msg.context["chat_id"]) == "webhook-user-A"


def test_build_invoke_message_generates_chat_id_when_missing():
    from casa_core import build_invoke_message

    a = build_invoke_message(agent_role="assistant", prompt="hi", payload={})
    b = build_invoke_message(agent_role="assistant", prompt="hi", payload={})
    # Two back-to-back calls without chat_id must not collide on
    # `webhook:default` — each invocation is its own session.
    assert a.context["chat_id"] != b.context["chat_id"]
    key_a = build_session_key(a.channel, a.context["chat_id"])
    key_b = build_session_key(b.channel, b.context["chat_id"])
    assert key_a != key_b
    assert key_a != "webhook-default"


def test_build_invoke_message_target_is_agent_role():
    from casa_core import build_invoke_message

    msg = build_invoke_message(agent_role="butler", prompt="hi", payload={})
    assert msg.target == "butler"
    assert msg.channel == "webhook"
    assert msg.type == MessageType.REQUEST


# ---------------------------------------------------------------------------
# Correlation id — builders attach fresh cid per message (spec 5.2 §7.2)
# ---------------------------------------------------------------------------

import re as _re


def test_build_invoke_message_attaches_cid():
    from casa_core import build_invoke_message

    msg = build_invoke_message(
        agent_role="butler", prompt="hi",
        payload={"context": {"chat_id": "user-A"}},
    )
    cid = msg.context.get("cid")
    assert isinstance(cid, str)
    assert _re.fullmatch(r"[0-9a-f]{8}", cid), cid
    # Payload-supplied fields continue to round-trip.
    assert msg.context["chat_id"] == "user-A"


def test_build_invoke_message_cid_is_unique_per_call():
    from casa_core import build_invoke_message

    a = build_invoke_message(agent_role="assistant", prompt="hi", payload={})
    b = build_invoke_message(agent_role="assistant", prompt="hi", payload={})
    assert a.context["cid"] != b.context["cid"]


# ---------------------------------------------------------------------------
# Webhook/invoke rate limiting — global bucket (spec 5.2 §8)
# ---------------------------------------------------------------------------

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rate_limit import RateLimiter, rate_limit_response


@pytest.mark.asyncio
class TestWebhookRateLimit:
    """The webhook handler exports a thin helper `rate_limit_response`
    that casa_core.main() wraps around `/webhook/{name}` and
    `/invoke/{agent}`. These tests pin the helper's HTTP contract
    directly against a minimal aiohttp app — avoids having to
    instantiate the full main() state machine for a 429-path check.
    """

    async def _build_app(self, capacity: int) -> web.Application:
        limiter = RateLimiter(capacity=capacity, window_s=60.0)

        async def webhook_handler(request: web.Request) -> web.Response:
            resp = rate_limit_response(limiter, "global")
            if resp is not None:
                return resp
            return web.json_response({"status": "accepted"})

        async def invoke_handler(request: web.Request) -> web.Response:
            resp = rate_limit_response(limiter, "global")
            if resp is not None:
                return resp
            return web.json_response({"response": "ok"})

        app = web.Application()
        app.router.add_post("/webhook/{name}", webhook_handler)
        app.router.add_post("/invoke/{agent}", invoke_handler)
        return app

    async def test_burst_admits_up_to_capacity_then_429s(self):
        app = await self._build_app(capacity=3)
        async with TestClient(TestServer(app)) as client:
            for _ in range(3):
                r = await client.post("/webhook/any", json={})
                assert r.status == 200
            r = await client.post("/webhook/any", json={})
            assert r.status == 429
            assert "Retry-After" in r.headers
            retry_after = int(r.headers["Retry-After"])
            assert 1 <= retry_after <= 61

    async def test_global_bucket_shared_across_webhook_and_invoke(self):
        """All webhook/* and invoke/* calls share the ONE global bucket
        (spec §8.2: 'all names and agents share one bucket').
        """
        app = await self._build_app(capacity=2)
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/webhook/ha-alert", json={})
            assert r.status == 200
            r = await client.post("/invoke/assistant", json={"prompt": "x"})
            assert r.status == 200
            # Bucket exhausted — any further call from either path is 429.
            r = await client.post("/webhook/other", json={})
            assert r.status == 429
            r = await client.post("/invoke/butler", json={"prompt": "x"})
            assert r.status == 429

    async def test_capacity_zero_disables(self):
        app = await self._build_app(capacity=0)
        async with TestClient(TestServer(app)) as client:
            for _ in range(200):
                r = await client.post("/webhook/any", json={})
                assert r.status == 200
                r = await client.post("/invoke/butler", json={"prompt": "x"})
                assert r.status == 200

    async def test_rejected_body_is_json_with_error_field(self):
        app = await self._build_app(capacity=1)
        async with TestClient(TestServer(app)) as client:
            await client.post("/webhook/any", json={})  # consume
            r = await client.post("/webhook/any", json={})
            assert r.status == 429
            payload = await r.json()
            assert payload == {"error": "rate_limited"}


# ---------------------------------------------------------------------------
# v0.75.0 (W5/Sol B3,B4, r4-B1/B3): graceful-shutdown broker drain barrier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBrokerShutdownOrdering:
    """Every live verdict_broker request must be resolved (cancelled) and
    its keyboard-edit finish-hook flushed BEFORE channel_manager.stop_all()
    tears the channels down — a finish hook firing after the channel is
    stopped can't edit anything."""

    async def test_broker_drained_before_channel_manager_stops(self, monkeypatch):
        import casa_core
        import verdict_broker
        from casa_core import _drain_broker_before_channel_shutdown

        order: list[str] = []

        class _FakeBroker:
            def cancel_all(self, *, reason):
                order.append(f"cancel_all:{reason}")

            async def drain_hooks(self):
                order.append("drain_hooks")

        class _FakeChallenges:
            async def drain(self):
                order.append("challenges_drain")

        monkeypatch.setattr(verdict_broker, "BROKER", _FakeBroker())
        # v0.76.0 (A:§3.4 r5-B2): CHALLENGES.drain() joins the pinned ladder
        # AFTER cancel_all (so a draining setup driver can't post) and BEFORE
        # drain_hooks — the ordering test asserts all FOUR steps.
        monkeypatch.setattr(casa_core, "CHALLENGES", _FakeChallenges())

        channel_manager = MagicMock()

        async def _stop_all():
            order.append("stop_all")

        channel_manager.stop_all = AsyncMock(side_effect=_stop_all)

        await _drain_broker_before_channel_shutdown(channel_manager)

        assert order == [
            "cancel_all:casa_shutdown", "challenges_drain",
            "drain_hooks", "stop_all",
        ]
        channel_manager.stop_all.assert_awaited_once()

    async def test_in_flight_finish_hook_dispatch_completes_before_stop(
        self, monkeypatch,
    ):
        """v0.76.0 (r1-B2): a REAL committed resident_ask whose finish hook is
        still running its post-commit continuation (edit -> dispatch) must be
        DRAINED by BROKER.drain_hooks before channel_manager.stop_all() — a
        dispatch/edit firing after the channel stops can't be delivered."""
        import verdict_broker
        from verdict_broker import VerdictBroker
        from casa_core import _drain_broker_before_channel_shutdown

        broker = VerdictBroker()
        monkeypatch.setattr(verdict_broker, "BROKER", broker)

        order: list[str] = []
        req, _ = broker.register(
            namespace="resident_ask", scope="dm:500", request_id="rid-drain",
            timeout_s=30.0, meta={"operator_id": 999},
        )

        async def _finish(outcome):
            order.append("edit")
            await asyncio.sleep(0)      # simulate the async edit_dm_message
            order.append("dispatch")    # simulate the dispatch continuation

        broker.set_finish_hook(req, _finish)
        # Commit synchronously -> schedules (does NOT await) the finish hook.
        assert deliver(broker, 
            namespace="resident_ask", scope="dm:500", request_id="rid-drain",
            option_index=0, actor_id=999,
        ) == "delivered"

        channel_manager = MagicMock()

        async def _stop_all():
            order.append("stop_all")

        channel_manager.stop_all = AsyncMock(side_effect=_stop_all)

        await _drain_broker_before_channel_shutdown(channel_manager)

        # The in-flight hook's edit+dispatch both completed before stop_all.
        assert order == ["edit", "dispatch", "stop_all"]

    async def test_real_ask_user_pending_keyboard_edited_before_stop(
        self, monkeypatch,
    ):
        """v0.76.0 (W5b, r1-B9): extends the shutdown-ordering barrier to a
        REAL pending `ask_user` request (not a fake/synthetic finish hook) —
        BROKER.cancel_all + drain_hooks must edit the pending ask's keyboard
        before channel_manager.stop_all() tears the channel down. #933: the
        edit says the shutdown retired it — "expired" was never true here, the
        cancel_all reason has always been `casa_shutdown`."""
        import agent as agent_mod
        import tools
        import verdict_broker
        from verdict_broker import VerdictBroker
        from casa_core import _drain_broker_before_channel_shutdown

        broker = VerdictBroker()
        monkeypatch.setattr(verdict_broker, "BROKER", broker)

        edits: list[tuple] = []
        order: list[str] = []

        class _FakeAskChannel:
            async def post_dm_keyboard(
                self, *, chat_id, request_id, text, options, short_labels=False,
            ):
                return 77

            async def edit_dm_message(self, chat_id, message_id, text):
                edits.append((chat_id, message_id, text))
                order.append("edit")
                return True

            async def _dispatch_button_continuation(self, **kw):
                return True

        channel = _FakeAskChannel()
        cm = MagicMock()
        cm.get = MagicMock(return_value=channel)
        tools.init_tools(
            channel_manager=cm, bus=MagicMock(), specialist_registry=MagicMock(),
            mcp_registry=MagicMock(),
        )
        tok = agent_mod.origin_var.set({
            "role": "assistant", "channel": "telegram", "chat_id": "500",
            "user_id": 999, "message_type": "channel_in", "source": "telegram",
            "execution_role": "assistant",
        })
        try:
            result = await tools.ask_user.handler(
                {"question": "Proceed?", "options": ["Yes", "No"]},
            )
        finally:
            agent_mod.origin_var.reset(tok)
        import json
        payload = json.loads(result["content"][0]["text"])
        assert payload["status"] == "awaiting_user"

        channel_manager = MagicMock()

        async def _stop_all():
            order.append("stop_all")

        channel_manager.stop_all = AsyncMock(side_effect=_stop_all)

        await _drain_broker_before_channel_shutdown(channel_manager)

        assert edits, "the pending ask's keyboard must have been edited"
        assert edits[0][2].endswith(
            "(this question was cancelled when Casa shut down)")
        # The edit landed BEFORE stop_all — drain_hooks() is awaited first.
        assert order == ["edit", "stop_all"]


# ---------------------------------------------------------------------------
# _env_int_or clamps (final-review MINOR 1: mirror the add-on schema rails)
# ---------------------------------------------------------------------------


class TestEnvIntClamp:
    def test_max_value_clamps_above(self):
        from casa_core import _env_int_or
        assert _env_int_or(
            "X", 2, min_value=1, max_value=20, env={"X": "999"}) == 20

    def test_min_value_clamps_below(self):
        from casa_core import _env_int_or
        assert _env_int_or(
            "X", 2, min_value=1, max_value=20, env={"X": "0"}) == 1

    def test_in_range_passes_through(self):
        from casa_core import _env_int_or
        assert _env_int_or(
            "X", 2, min_value=1, max_value=20, env={"X": "5"}) == 5

    def test_absent_uses_default(self):
        from casa_core import _env_int_or
        assert _env_int_or("X", 2, min_value=1, max_value=20, env={}) == 2
