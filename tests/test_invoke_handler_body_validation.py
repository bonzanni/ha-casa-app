"""L3: POST /invoke/{agent} must return 400 (not 500) for non-object JSON
bodies and must normalize an explicit "context": null instead of crashing.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.unit

# Release A: /invoke is fail-closed — it requires a non-empty secret + a valid
# signature, so these body-validation tests (which exercise POST-auth logic)
# authenticate first.
_SECRET = "invoke-secret"


def _sign(body: bytes) -> str:
    return hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()


async def _post_signed(client, path, obj):
    body = json.dumps(obj).encode()
    return await client.post(
        path, data=body,
        headers={"Content-Type": "application/json",
                 "X-Webhook-Signature": _sign(body)},
    )


class _StubResult:
    content = "ok"


class _StubBus:
    def __init__(self):
        self.last_msg = None

    async def request(self, msg, timeout=300):
        self.last_msg = msg
        return _StubResult()


class _StubCfg:
    def __init__(self, channels):
        self.channels = channels


def _make_app(bus):
    from casa_core import _make_invoke_handler
    from casa_core_middleware import cid_middleware
    from rate_limit import RateLimiter

    # A:§3—these body-validation tests all target the "assistant" role,
    # which (per the real defaults/agents/assistant/runtime.yaml) declares
    # webhook — so it stays invoke-reachable under the spec A3 gate.
    handler = _make_invoke_handler(
        webhook_rate_limiter=RateLimiter(capacity=0, window_s=60.0),  # 0 = disabled
        webhook_secret=_SECRET,  # fail-closed: /invoke requires a secret
        bus=bus,
        assistant_role="assistant",
        role_configs={"assistant": _StubCfg(["telegram", "webhook"])},
    )
    app = web.Application(middlewares=[cid_middleware])  # provides request["cid"], as prod does
    app.router.add_post("/invoke/{agent}", handler)
    return app


@pytest.mark.asyncio
async def test_non_dict_json_bodies_return_400_not_500():
    app = _make_app(_StubBus())
    async with TestClient(TestServer(app)) as client:
        for body in ("[1]", '"hi"', "42", "null"):
            raw = body.encode()
            r = await client.post(
                "/invoke/assistant", data=raw,
                headers={"Content-Type": "application/json",
                         "X-Webhook-Signature": _sign(raw)},
            )
            assert r.status == 400, f"body={body} gave {r.status}"
            assert (await r.json()) == {"error": "invalid JSON body"}


@pytest.mark.asyncio
async def test_context_null_is_normalized_and_invocation_succeeds():
    bus = _StubBus()
    app = _make_app(bus)
    async with TestClient(TestServer(app)) as client:
        r = await _post_signed(
            client, "/invoke/assistant",
            {"prompt": "status", "context": None},
        )
        assert r.status == 200
        assert (await r.json()) == {"response": "ok"}
        assert bus.last_msg.context.get("cid")  # cid was injected despite null context


@pytest.mark.asyncio
async def test_context_non_dict_string_is_normalized():
    """Bonus variant: a truthy non-dict context (e.g. a string) must also
    be normalized rather than raising at item-assignment."""
    bus = _StubBus()
    app = _make_app(bus)
    async with TestClient(TestServer(app)) as client:
        r = await _post_signed(
            client, "/invoke/assistant",
            {"prompt": "status", "context": "abc"},
        )
        assert r.status == 200
        assert bus.last_msg.context.get("cid")


@pytest.mark.asyncio
async def test_missing_prompt_still_returns_400():
    """Existing contract must survive the refactor."""
    app = _make_app(_StubBus())
    async with TestClient(TestServer(app)) as client:
        r = await _post_signed(client, "/invoke/assistant", {})
        assert r.status == 400
        assert (await r.json()) == {"error": "missing 'prompt' field"}


@pytest.mark.asyncio
async def test_caller_supplied_reserved_keys_are_stripped():
    """A:§3.5 sanitize-and-preserve: an external /invoke caller cannot spoof
    provenance-bearing keys (execution_role/message_type/source/synthetic/
    button_answer) via the context dict — they must never reach the
    dispatched BusMessage. Ordinary caller-supplied keys are preserved."""
    bus = _StubBus()
    app = _make_app(bus)
    async with TestClient(TestServer(app)) as client:
        r = await _post_signed(
            client, "/invoke/assistant",
            {
                "prompt": "status",
                "context": {
                    "chat_id": "caller-1",
                    "device": "kitchen-panel",
                    "synthetic": "button",
                    "button_answer": "yes",
                    "execution_role": "butler",
                    "message_type": "channel_in",
                    "source": "telegram",
                },
            },
        )
        assert r.status == 200
        ctx = bus.last_msg.context
        assert ctx["chat_id"] == "caller-1"     # preserved
        assert ctx["device"] == "kitchen-panel"  # preserved
        for key in (
            "synthetic", "button_answer", "execution_role",
            "message_type", "source",
        ):
            assert key not in ctx, f"reserved key {key!r} leaked into BusMessage.context"


@pytest.mark.asyncio
async def test_caller_supplied_cid_is_honored():
    """#324: a caller-supplied context.cid must reach the BusMessage (the
    documented "caller cid wins" contract) instead of being overwritten by
    the middleware request cid."""
    bus = _StubBus()
    app = _make_app(bus)
    async with TestClient(TestServer(app)) as client:
        r = await _post_signed(
            client, "/invoke/assistant",
            {"prompt": "status", "context": {"cid": "workflow-42"}},
        )
        assert r.status == 200
        assert bus.last_msg.context["cid"] == "workflow-42"


@pytest.mark.asyncio
async def test_blank_or_non_string_cid_falls_back_to_request_cid():
    """A blank or non-string caller cid is replaced (never dispatched)."""
    bus = _StubBus()
    app = _make_app(bus)
    async with TestClient(TestServer(app)) as client:
        for bad in ("", "   ", 42, None):
            r = await _post_signed(
                client, "/invoke/assistant",
                {"prompt": "status", "context": {"cid": bad}},
            )
            assert r.status == 200
            cid = bus.last_msg.context.get("cid")
            assert isinstance(cid, str) and cid.strip(), f"cid={cid!r} for input {bad!r}"
            assert cid != bad


@pytest.mark.asyncio
async def test_invoke_fail_closed_403_when_no_secret():
    """Release A (spec A1): /invoke with webhook auth disabled (empty secret)
    returns 403 — the route is off, never an open arbitrary-prompt endpoint."""
    from casa_core import _make_invoke_handler
    from casa_core_middleware import cid_middleware
    from rate_limit import RateLimiter
    bus = _StubBus()
    handler = _make_invoke_handler(
        webhook_rate_limiter=RateLimiter(capacity=0, window_s=60.0),
        webhook_secret="",  # auth disabled
        bus=bus, assistant_role="assistant",
        role_configs={"assistant": _StubCfg(["telegram", "webhook"])},
    )
    app = web.Application(middlewares=[cid_middleware])
    app.router.add_post("/invoke/{agent}", handler)
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/invoke/assistant", json={"prompt": "hi"})
        assert r.status == 403
        assert bus.last_msg is None
