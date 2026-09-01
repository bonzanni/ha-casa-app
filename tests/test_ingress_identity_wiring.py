"""Every external ingress actually stamps its identity through the table.

The table (test_ingress_identity.py) is only worth having if the handlers use
it. These are the end-to-end pins: a dispatched turn must carry the trusted
origin, and a Casa-composed internal turn must still carry none.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.asyncio


def _hmac(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _make_bus():
    bus = MagicMock()
    bus.send = AsyncMock()
    return bus


def _make_registry(targets, clearances=None):
    clearances = clearances or {}
    reg = MagicMock()
    reg.get_webhook_target = lambda name: targets.get(name)
    reg.webhook_route = lambda name: (
        None if name not in targets else {
            "role": targets[name], "clearance": reg.get_clearance(name),
            "auth": reg.get_auth_policy(name), "resident": True})
    reg.get_clearance = lambda name: clearances.get(name, "public")
    reg.get_auth_policy = lambda name: (
        {"mode": "hmac_body", "header": "X-Webhook-Signature",
         "tolerance_secs": 300, "secret_owner": "casa"}
        if name in targets else None
    )
    return reg


async def _webhook_app(*, secret, targets, clearances=None, bus=None):
    from casa_core import _make_webhook_handler
    from rate_limit import RateLimiter

    bus = bus or _make_bus()
    handler = _make_webhook_handler(
        webhook_rate_limiter=RateLimiter(capacity=0, window_s=60.0),
        webhook_secret=secret,
        trigger_registry=_make_registry(targets, clearances),
        default_role="assistant",
        bus=bus,
        secrets_dir="/data/webhook_secrets",
    )
    app = web.Application()
    app.router.add_post("/webhook/{name}", handler)
    return app, bus


class TestWebhookStamps:
    async def test_a_dispatched_trigger_carries_its_per_trigger_identity(self):
        secret = "s3cret"
        body = b'{"x": 1}'
        app, bus = await _webhook_app(secret=secret, targets={"probe": "assistant"})
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/webhook/probe", data=body,
                headers={"X-Webhook-Signature": _hmac(secret, body)},
            )
            assert response.status == 200

        origin = bus.send.call_args.args[0].trusted_user_origin
        assert origin is not None, "#204: a webhook turn must not be unattributed"
        assert origin.user_peer == "webhook:probe"
        assert origin.surface == "webhook"
        assert origin.authenticated_user is None

    async def test_the_stamped_turn_is_an_automation_not_a_user(self):
        from speaker_provenance import UserProvenance

        secret = "s3cret"
        body = b"{}"
        app, bus = await _webhook_app(secret=secret, targets={"probe": "assistant"})
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/webhook/probe", data=body,
                headers={"X-Webhook-Signature": _hmac(secret, body)},
            )

        origin = bus.send.call_args.args[0].trusted_user_origin
        provenance = UserProvenance.from_origin(
            surface=origin.surface, server_origin=origin.server_origin,
            authenticated_user=origin.authenticated_user,
            user_peer=origin.user_peer,
        )
        assert provenance.speaker_kind == "automation"

    async def test_a_third_party_trigger_is_never_recorded_as_a_person(self):
        # Trap 2: the retired user_peer_for_channel default would have given
        # this the operator's peer — permanently recording third-party content
        # as the operator's own words.
        secret = "s3cret"
        body = b"{}"
        app, bus = await _webhook_app(
            secret=secret, targets={"plg-acme--inbound": "assistant"})
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/webhook/plg-acme--inbound", data=body,
                headers={"X-Webhook-Signature": _hmac(secret, body)},
            )

        from ingress_identity import _TELEGRAM_PEER_PREFIX

        origin = bus.send.call_args.args[0].trusted_user_origin
        assert origin.user_peer == "webhook:plg-acme--inbound"
        assert not origin.user_peer.startswith(_TELEGRAM_PEER_PREFIX)

    async def test_the_declared_trigger_clearance_rides_on_the_origin(self):
        secret = "s3cret"
        body = b"{}"
        app, bus = await _webhook_app(
            secret=secret, targets={"probe": "assistant"},
            clearances={"probe": "friends"},
        )
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/webhook/probe", data=body,
                headers={"X-Webhook-Signature": _hmac(secret, body)},
            )

        origin = bus.send.call_args.args[0].trusted_user_origin
        assert origin.server_origin.clearance == "friends"

    async def test_an_unstampable_trigger_is_rejected_not_dispatched(self):
        # #203: an ingress that cannot be given an identity must fail the turn
        # rather than dispatch it under the unattributed ``system``.
        import ingress_identity as ii

        secret = "s3cret"
        body = b"{}"
        app, bus = await _webhook_app(secret=secret, targets={"probe": "assistant"})

        def boom(*_args, **_kwargs):
            raise ii.IngressIdentityError("no identity for you")

        async with TestClient(TestServer(app)) as client:
            import casa_core

            original = casa_core.ingress_identity
            casa_core.ingress_identity = boom
            try:
                response = await client.post(
                    "/webhook/probe", data=body,
                    headers={"X-Webhook-Signature": _hmac(secret, body)},
                )
            finally:
                casa_core.ingress_identity = original

        assert response.status == 500
        bus.send.assert_not_called()


class TestInvokeStamps:
    async def test_invoke_carries_the_invoke_caller_identity(self):
        from casa_core import build_invoke_message

        msg = build_invoke_message("assistant", "do the thing", {})
        origin = msg.trusted_user_origin
        assert origin is not None, "#204: an invoke turn must not be unattributed"
        assert origin.user_peer == "invoke_caller"
        assert origin.surface == "invoke"

    async def test_invoke_and_webhook_never_share_a_peer_end_to_end(self):
        from casa_core import build_invoke_message

        secret = "s3cret"
        body = b"{}"
        app, bus = await _webhook_app(secret=secret, targets={"probe": "assistant"})
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/webhook/probe", data=body,
                headers={"X-Webhook-Signature": _hmac(secret, body)},
            )

        webhook_peer = bus.send.call_args.args[0].trusted_user_origin.user_peer
        invoke_peer = build_invoke_message(
            "assistant", "hi", {}).trusted_user_origin.user_peer
        assert webhook_peer != invoke_peer

    async def test_a_caller_cannot_forge_the_identity_through_the_payload(self):
        # The origin is server-created; sanitize_external_context already
        # strips reserved context keys, and the typed field is never decoded
        # from the body.
        from casa_core import build_invoke_message

        # The forged value is a REAL human peer (the shape a sender resolves
        # to), so this stays a meaningful spoof attempt rather than a string
        # nothing would honour anyway.
        msg = build_invoke_message("assistant", "hi", {
            "context": {
                "trusted_user_origin": {"user_peer": "telegram:7"},
                "user_peer": "telegram:7",
            },
        })
        assert msg.trusted_user_origin.user_peer == "invoke_caller"


class TestInternalTurnsStayUnattributed:
    async def test_a_casa_composed_turn_carries_no_trusted_origin(self):
        # The post-consent plugin-setup dispatch (v0.112.0) sends a CHANNEL_IN
        # on channel="telegram" carrying text CASA wrote. It has no human
        # author, so it must keep the honest ``system`` identity — which is why
        # #203's assertion lives at the ingress boundary and not in
        # Agent._process.
        from bus import BusMessage, MessageType

        msg = BusMessage(
            type=MessageType.CHANNEL_IN, source="telegram", target="assistant",
            content="Casa-composed setup prompt", channel="telegram",
            context={"chat_id": "1", "synthetic": "plugin_setup"},
        )
        assert msg.trusted_user_origin is None


class TestRetiredFallback:
    async def test_user_peer_for_channel_is_gone(self):
        # Trap 2 removed at the root: the helper defaulted to the operator's
        # peer for any channel not in its map, so every future ingress
        # inherited the operator's identity by omission.
        import channel_trust

        assert not hasattr(channel_trust, "user_peer_for_channel")
        assert not hasattr(channel_trust, "_USER_PEER_BY_CHANNEL")
