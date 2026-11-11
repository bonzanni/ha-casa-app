"""L4 (v0.50.0): the Telegram webhook secret-token header must be compared
with ``hmac.compare_digest`` (constant-time), not ``!=`` (timing oracle).

Each bot has its own route, ``/telegram/update/{bot_id}``, and its own
secret derived from the configured one; ``tests/test_bot_webhook_routes.py``
pins the separation between bots.

Mirrors the aiohttp TestClient/TestServer pattern in
``tests/test_webhook_handler.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


BOT = 111
URL = f"/telegram/update/{BOT}"


def _token(root: str = "s3cret") -> str:
    """The secret Telegram sends on bot 111's route."""
    from channels.telegram_transport import webhook_secret_for
    return webhook_secret_for(root, BOT)


async def _client(secret: str, channel):
    from casa_core import _make_telegram_update_handler
    handler = _make_telegram_update_handler(
        get_endpoint=lambda bot_id: channel if bot_id == BOT else None,
        root_secret=secret,
    )
    app = web.Application()
    app.router.add_post("/telegram/update/{bot_id}", handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _channel(outcome: str = "accepted"):
    ch = MagicMock()
    ch.process_webhook_update = AsyncMock(return_value=outcome)
    return ch


async def test_wrong_token_403_and_not_processed():
    ch = _channel()
    client = await _client("s3cret", ch)
    resp = await client.post(
        URL, json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert resp.status == 403
    ch.process_webhook_update.assert_not_awaited()
    await client.close()


async def test_the_configured_secret_itself_is_not_the_route_secret():
    """The root secret signs nothing on its own: each route expects the
    secret derived for its bot."""
    ch = _channel()
    client = await _client("s3cret", ch)
    resp = await client.post(
        URL, json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
    )
    assert resp.status == 403
    ch.process_webhook_update.assert_not_awaited()
    await client.close()


async def test_correct_token_200():
    ch = _channel()
    client = await _client("s3cret", ch)
    resp = await client.post(
        URL, json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": _token()},
    )
    assert resp.status == 200
    ch.process_webhook_update.assert_awaited_once()
    await client.close()


async def test_outcome_header_reflects_channel_outcome():
    # #428: same 200 + empty body either way (Telegram's contract), but the
    # X-Casa-Update header tells a programmatic caller queued from deduped.
    for outcome in ("accepted", "duplicate", "ignored"):
        ch = _channel(outcome)
        client = await _client("s3cret", ch)
        resp = await client.post(
            URL, json={"update_id": 7},
            headers={"X-Telegram-Bot-Api-Secret-Token": _token()},
        )
        assert resp.status == 200
        assert resp.headers["X-Casa-Update"] == outcome
        assert await resp.read() == b""
        await client.close()


async def test_non_ascii_token_is_403_not_500():
    # Regression for the compare_digest str/str TypeError pitfall: a
    # non-ASCII header must produce 403, not an unhandled 500.
    ch = _channel()
    client = await _client("s3cret", ch)
    resp = await client.post(
        URL, json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "sécret"},
    )
    assert resp.status == 403
    await client.close()


async def test_no_secret_configured_rejects():
    """Pins INV-TG-001 (with the sibling tests here). Red case demonstrated: neutering the no-secret rejection in _make_telegram_update_handler fails this test."""
    # #193: with no webhook secret the route is fail-CLOSED — an unsigned
    # (potentially forged) update must NOT reach the assistant. 403 signals the
    # route is disabled, not merely mis-signed (mirrors /invoke). In polling
    # mode this route is registered but unused, so rejecting is harmless.
    ch = _channel()
    client = await _client("", ch)
    resp = await client.post(URL, json={"update_id": 1})
    assert resp.status == 403
    ch.process_webhook_update.assert_not_awaited()
    await client.close()


async def test_constant_time_comparison_used():
    # Source-level guard: the handler must not use plain != on the token.
    import inspect
    import casa_core
    src = inspect.getsource(casa_core._make_telegram_update_handler)
    assert "compare_digest" in src and "token != " not in src
