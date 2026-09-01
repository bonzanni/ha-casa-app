"""N-2 (v0.36.0). The wildcard ``/webhook/{name}`` handler must consult
the trigger registry's per-boot allowlist and 404 unknown names. Known
names dispatch to the role registered with the trigger, not the
hardcoded assistant_role.
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


def _make_bus():
    bus = MagicMock()
    bus.send = AsyncMock()
    return bus


def _make_registry(
    targets: dict[str, str],
    clearances: dict[str, str] | None = None,
    policies: dict[str, dict] | None = None,
):
    """Stand-in TriggerRegistry exposing get_webhook_target/get_clearance/
    get_auth_policy. Default policy is hmac_body (global-secret HMAC)."""
    clearances = clearances or {}
    policies = policies or {}
    default_policy = {
        "mode": "hmac_body", "header": "X-Webhook-Signature",
        "tolerance_secs": 300, "secret_owner": "casa",
    }
    reg = MagicMock()
    reg.get_webhook_target = lambda name: targets.get(name)
    reg.webhook_route = lambda name: (
        None if name not in targets else {
            "role": targets[name], "clearance": reg.get_clearance(name),
            "auth": reg.get_auth_policy(name), "resident": True})
    reg.get_clearance = lambda name: clearances.get(name, "public")
    reg.get_auth_policy = lambda name: (
        policies.get(name, default_policy) if name in targets else None
    )
    return reg


def _hmac(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def _build_app(
    *,
    secret: str = "",
    targets: dict[str, str] | None = None,
    clearances: dict[str, str] | None = None,
    policies: dict[str, dict] | None = None,
    secrets_dir=None,
    default_role: str = "assistant",
    bus=None,
):
    from casa_core import _make_webhook_handler
    from rate_limit import RateLimiter

    targets = targets or {}
    bus = bus or _make_bus()
    limiter = RateLimiter(capacity=0, window_s=60.0)  # 0 = disabled

    handler = _make_webhook_handler(
        webhook_rate_limiter=limiter,
        webhook_secret=secret,
        trigger_registry=_make_registry(targets, clearances, policies),
        default_role=default_role,
        bus=bus,
        secrets_dir=secrets_dir or "/data/webhook_secrets",
    )

    app = web.Application()
    app.router.add_post("/webhook/{name}", handler)
    # The handler's cid lookup uses ``request.get("cid") or new_cid()``,
    # so missing cid is fine — production wires log_cid middleware.
    return app, bus


class TestWebhookAllowlist:
    async def test_unknown_name_returns_404(self):
        """N-2: POST /webhook/<unknown> must 404, not dispatch."""
        app, bus = await _build_app(targets={"known": "assistant"})
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/webhook/never-registered", json={})
            assert r.status == 404
            payload = await r.json()
            assert payload == {"error": "unknown webhook"}
            bus.send.assert_not_called()

    async def test_known_name_dispatches_and_returns_200(self):
        secret = "s3cret"; body = b'{"x": 1}'
        app, bus = await _build_app(secret=secret, targets={"probe": "assistant"})
        async with TestClient(TestServer(app)) as client:
            r = await client.post(
                "/webhook/probe", data=body,
                headers={"X-Webhook-Signature": _hmac(secret, body)},
            )
            assert r.status == 200
            payload = await r.json()
            assert payload == {"status": "accepted"}
            bus.send.assert_awaited_once()
            msg = bus.send.call_args.args[0]
            assert msg.target == "assistant"
            assert msg.context["webhook_name"] == "probe"

    async def test_known_name_dispatches_to_registered_role(self):
        """A webhook trigger registered for role=butler dispatches there,
        not to the hardcoded default."""
        secret = "s3cret"; body = b"{}"
        app, bus = await _build_app(
            secret=secret, targets={"b1": "butler"}, default_role="assistant",
        )
        async with TestClient(TestServer(app)) as client:
            r = await client.post(
                "/webhook/b1", data=body,
                headers={"X-Webhook-Signature": _hmac(secret, body)},
            )
            assert r.status == 200
            msg = bus.send.call_args.args[0]
            assert msg.target == "butler"

    async def test_unknown_name_404_before_auth(self):
        """Spec r3: name lookup precedes per-trigger auth (the auth policy is
        selected BY name), so an unknown name 404s regardless of signature —
        names are non-secret and rate limiting bounds enumeration."""
        app, bus = await _build_app(
            secret="topsecret", targets={"known": "assistant"},
        )
        async with TestClient(TestServer(app)) as client:
            r = await client.post(
                "/webhook/never-registered",
                data=b"{}",
                headers={"X-Webhook-Signature": "wrong"},
            )
            assert r.status == 404
            bus.send.assert_not_called()

    async def test_payload_context_never_enters_bus_message_context(self):
        """r2-B6 (A:§1): the wildcard handler builds a Casa-OWNED context
        (webhook_name/cid only) and embeds the payload in message CONTENT —
        it must NOT start propagating a caller-supplied payload["context"]
        dict into BusMessage.context (that would let an external webhook
        caller spoof provenance-bearing keys like execution_role)."""
        secret = "s3cret"
        body = json.dumps({
            "x": 1,
            "context": {
                "execution_role": "butler", "message_type": "channel_in",
                "source": "telegram", "synthetic": "button",
                "smuggled": "should-not-appear",
            },
        }).encode()
        app, bus = await _build_app(secret=secret, targets={"probe": "assistant"})
        async with TestClient(TestServer(app)) as client:
            r = await client.post(
                "/webhook/probe", data=body,
                headers={"X-Webhook-Signature": _hmac(secret, body)},
            )
            assert r.status == 200
            msg = bus.send.call_args.args[0]
            # Precise contract: only Casa-OWNED keys are present. Release A
            # adds the server-set origin markers + one-shot chat_id; caller
            # keys are still stripped.
            assert set(msg.context.keys()) <= {
                "webhook_name", "cid",
                "_origin_route", "_origin_clearance", "chat_id",
            }
            assert "execution_role" not in msg.context
            assert "smuggled" not in msg.context
            assert "synthetic" not in msg.context
            # The caller cannot forge the origin route: it is server-stamped.
            assert msg.context["_origin_route"] == "webhook_trigger"

    async def test_valid_hmac_unknown_name_returns_404(self):
        """Valid HMAC but unknown name still 404s (defense-in-depth):
        operator removed a webhook trigger, secret unchanged, replays must
        fail with the right status."""
        secret = "topsecret"
        body = b"{}"
        sig = _hmac(secret, body)
        app, bus = await _build_app(
            secret=secret, targets={"known": "assistant"},
        )
        async with TestClient(TestServer(app)) as client:
            r = await client.post(
                "/webhook/never-registered",
                data=body,
                headers={"X-Webhook-Signature": sig},
            )
            assert r.status == 404
            bus.send.assert_not_called()


class TestWebhookOriginStamping:
    """Release A / Layer 3: the wildcard dispatch stamps the unspoofable
    webhook_trigger origin markers + a fresh one-shot chat_id."""

    _SECRET = "s3cret"

    async def _post_signed(self, client, name, body=b"{}"):
        return await client.post(
            f"/webhook/{name}", data=body,
            headers={"X-Webhook-Signature": _hmac(self._SECRET, body)},
        )

    async def test_dispatch_stamps_webhook_trigger_markers(self):
        app, bus = await _build_app(
            secret=self._SECRET, targets={"vm": "assistant"},
            clearances={"vm": "family"})
        async with TestClient(TestServer(app)) as client:
            r = await self._post_signed(client, "vm", b'{"x": 1}')
            assert r.status == 200
        msg = bus.send.call_args.args[0]
        assert msg.context["_origin_route"] == "webhook_trigger"
        assert msg.context["_origin_clearance"] == "family"
        # fresh uuid chat_id (one-shot isolation)
        chat_id = msg.context["chat_id"]
        assert isinstance(chat_id, str) and len(chat_id) == 36

    async def test_clearance_defaults_public(self):
        app, bus = await _build_app(secret=self._SECRET, targets={"vm": "assistant"})
        async with TestClient(TestServer(app)) as client:
            await self._post_signed(client, "vm")
        msg = bus.send.call_args.args[0]
        assert msg.context["_origin_clearance"] == "public"

    async def test_two_dispatches_get_distinct_chat_ids(self):
        app, bus = await _build_app(secret=self._SECRET, targets={"vm": "assistant"})
        async with TestClient(TestServer(app)) as client:
            await self._post_signed(client, "vm")
            await self._post_signed(client, "vm")
        ids = {c.args[0].context["chat_id"] for c in bus.send.call_args_list}
        assert len(ids) == 2


class TestPerTriggerAuthModes:
    """Release A: the handler verifies with the trigger's declared auth policy."""

    async def test_static_header_mode_end_to_end(self, tmp_path):
        # provider-owned opaque secret pre-placed so the test knows its value
        (tmp_path / "vm").write_bytes(b"opaque-provider-key-123")
        (tmp_path / "vm").chmod(0o600)
        app, bus = await _build_app(
            targets={"vm": "assistant"},
            policies={"vm": {"mode": "static_header", "header": "X-API-Key",
                             "tolerance_secs": 300, "secret_owner": "provider"}},
            secrets_dir=str(tmp_path),
        )
        async with TestClient(TestServer(app)) as client:
            ok = await client.post("/webhook/vm", data=b"{}",
                                   headers={"X-API-Key": "opaque-provider-key-123"})
            assert ok.status == 200
            bad = await client.post("/webhook/vm", data=b"{}",
                                    headers={"X-API-Key": "wrong"})
            assert bad.status == 401
            missing = await client.post("/webhook/vm", data=b"{}")
            assert missing.status == 401

    async def test_hmac_body_fail_closed_when_no_global_secret(self, tmp_path):
        # hmac_body trigger but NO global secret → empty secret → 401 (never open)
        app, bus = await _build_app(
            secret="", targets={"vm": "assistant"}, secrets_dir=str(tmp_path))
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/webhook/vm", data=b"{}",
                                  headers={"X-Webhook-Signature": "anything"})
            assert r.status == 401
            bus.send.assert_not_called()

    async def test_secret_read_failure_returns_401_not_500(
        self, tmp_path, monkeypatch,
    ):
        """Sol shipB-r1 P1-6: a filesystem failure while reading the secret
        (unwritable / full / unreadable secrets dir) must degrade to an empty
        secret — 401 — never a 500 through the auth path.

        #609: this test used to patch ``ensure_secret``. When the request path
        stopped minting, that patch no longer fired and the test kept passing
        while proving NOTHING — it stayed green with the try/except deleted.
        The call-count assertion is what makes it a test again: a patch that
        does not fire is the failure mode, not the target's name.
        """
        import webhook_auth

        calls = []

        def _boom(name, **kw):
            calls.append(name)
            raise OSError("read-only filesystem")

        # #620: a casa-owned resident route reads through the CERTIFIED reader;
        # patching `read_secret` would leave a green test that exercised nothing.
        monkeypatch.setattr(webhook_auth, "read_certified_secret", _boom)
        app, bus = await _build_app(
            targets={"vm": "assistant"},
            policies={"vm": {"mode": "static_header", "header": "X-API-Key",
                             "tolerance_secs": 300, "secret_owner": "casa"}},
            secrets_dir=str(tmp_path),
        )
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/webhook/vm", data=b"{}",
                                  headers={"X-API-Key": "whatever"})
            assert r.status == 401
            bus.send.assert_not_called()
        assert calls == ["vm"], "the patch never fired — the test proves nothing"

    async def test_the_request_path_never_creates_a_secret(self, tmp_path):
        """#609: minting moved to registration, so verification is READ-ONLY.
        Both mechanisms live at once would let a route be served from whatever
        file happened to survive; an unprovisioned route must be a 401, not a
        mint."""
        app, bus = await _build_app(
            targets={"vm": "assistant"},
            policies={"vm": {"mode": "static_header", "header": "X-API-Key",
                             "tolerance_secs": 300, "secret_owner": "casa"}},
            secrets_dir=str(tmp_path),
        )
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/webhook/vm", data=b"{}",
                                  headers={"X-API-Key": "whatever"})
            assert r.status == 401
            bus.send.assert_not_called()
        assert sorted(p.name for p in tmp_path.iterdir()) == [], (
            "the request path created a file")

    async def test_oversize_body_rejected_413(self, tmp_path):
        app, bus = await _build_app(
            secret="s", targets={"vm": "assistant"}, secrets_dir=str(tmp_path))
        big = b"x" * (64 * 1024 + 1)
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/webhook/vm", data=big,
                                  headers={"X-Webhook-Signature": "x"})
            assert r.status == 413
            bus.send.assert_not_called()


class TestWebhookPayloadParsing:
    """Release A: the handler parses the already-read body as JSON (the
    streaming body-cap consumed request.content, so request.json() re-reads
    empty — Terra ship-review P2)."""

    _SECRET = "s3cret"

    async def test_json_payload_is_parsed_not_raw_text(self):
        app, bus = await _build_app(secret=self._SECRET, targets={"vm": "assistant"})
        body = json.dumps({"caller_name": "Alice", "urgency": "high"}).encode()
        async with TestClient(TestServer(app)) as client:
            r = await client.post(
                "/webhook/vm", data=body,
                headers={"X-Webhook-Signature": _hmac(self._SECRET, body)},
            )
            assert r.status == 200
        content = bus.send.call_args.args[0].content
        # The parsed dict (not the raw JSON string) is embedded in the prompt.
        assert "'caller_name': 'Alice'" in content or '"caller_name": "Alice"' in content
        assert "Alice" in content and "high" in content
