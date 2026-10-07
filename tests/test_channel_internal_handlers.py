# tests/test_channel_internal_handlers.py
"""Unit tests for channels.channel_handlers (v0.37.0 Phase 1).

Covers ``POST /internal/channel/send_to_topic``: the casa-main side of the
bridge that ``casa_engagement_channel.py`` (the stdio MCP server inside each
``claude_code`` engagement) POSTs into over ``/run/casa/internal.sock``.

Phase 1 surface is intentionally one path; later phases extend the dict
returned by ``_make_channel_handlers`` (see spec §A.3).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeChannel:
    """Capture-fake for ``TelegramChannel.send_to_topic``.

    Returns incrementing ``message_id`` values starting at 7000 so tests can
    assert the handler propagates the value back over the wire.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response_route_calls = 0
        self._next_msg_id = 7000

    async def send_to_topic(
        self, thread_id: int, text: str, **kwargs: Any,
    ) -> int:
        msg_id = self._next_msg_id
        self._next_msg_id += 1
        self.calls.append(
            {"topic_id": thread_id, "text": text, "kwargs": kwargs},
        )
        return msg_id

    async def send_response_to_topic(
        self, thread_id: int, text: str, **kwargs: Any,
    ) -> int:
        # v0.70.0: CC reply handler routes here; rich rendering lives in the real
        # TelegramChannel. Record separately so tests can prove the route.
        self.response_route_calls += 1
        return await self.send_to_topic(thread_id, text, **kwargs)


class _FakeRecord:
    def __init__(
        self, eng_id: str, *, topic_id: int | None, status: str = "active",
    ) -> None:
        self.id = eng_id
        self.topic_id = topic_id
        self.status = status
        # #335: per-engagement secret; bodies must present it as
        # ``engagement_token`` to act with this engagement's authority.
        self.auth_token = f"tok-{eng_id}"


class _FakeRegistry:
    """Minimal stand-in for ``EngagementRegistry``.

    Pre-seeds one record (``eng-1`` → topic 42). Tests that need other
    shapes (missing record / record with no topic_id) override via
    ``set_record``. Carries ``advance_interaction_state`` (W2/Sol B9, Task
    7) so send_to_topic's first_contact seam has something to call; most
    tests never assert on ``self.advances``.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, _FakeRecord] = {
            "eng-1": _FakeRecord("eng-1", topic_id=42),
        }
        self.advances: list[tuple[str, str]] = []

    def set_record(self, eng_id: str, rec: _FakeRecord | None) -> None:
        if rec is None:
            self._by_id.pop(eng_id, None)
        else:
            self._by_id[eng_id] = rec

    def get(self, eng_id: str) -> _FakeRecord | None:
        return self._by_id.get(eng_id)

    async def advance_interaction_state(self, eng_id: str, event: str) -> None:
        self.advances.append((eng_id, event))


# ---------------------------------------------------------------------------
# App factory fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def app_factory():
    from channels.channel_handlers import _make_channel_handlers

    def make(channel=None, registry=None):
        ch = channel or _FakeChannel()
        reg = registry or _FakeRegistry()
        handlers = _make_channel_handlers(
            telegram_channel=ch, engagement_registry=reg,
        )
        app = web.Application()
        for path, h in handlers.items():
            app.router.add_post(path, h)
        return app, ch, reg

    return make


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_send_to_topic_routes_by_engagement_id(app_factory) -> None:
    """Handler resolves engagement_id → topic_id via the registry and
    forwards the text. Response carries the channel's returned message_id."""
    app, ch, _reg = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/send_to_topic",
            json={"engagement_id": "eng-1", "text": "hello operator",
                  "engagement_token": "tok-eng-1"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"ok": True, "message_id": 7000}

    assert len(ch.calls) == 1
    assert ch.calls[0]["topic_id"] == 42
    assert ch.calls[0]["text"] == "hello operator"
    # v0.70.0: the CC reply handler must use the response-provenant method.
    assert ch.response_route_calls == 1


async def test_send_to_topic_unknown_engagement_returns_error(
    app_factory,
) -> None:
    """Missing engagement record short-circuits with ``unknown_engagement``
    and never touches the telegram channel."""
    reg = _FakeRegistry()
    reg.set_record("eng-1", None)  # so registry.get("missing") returns None
    app, ch, _reg = app_factory(registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/send_to_topic",
            json={"engagement_id": "missing", "text": "hi"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"ok": False, "error": "unknown_engagement"}

    assert ch.calls == []


async def test_send_to_topic_missing_topic_id_returns_error(
    app_factory,
) -> None:
    """Record exists but has no bound topic_id → ``no_topic_bound``,
    no telegram call."""
    reg = _FakeRegistry()
    reg.set_record("eng-1", _FakeRecord("eng-1", topic_id=None))
    app, ch, _reg = app_factory(registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/send_to_topic",
            json={"engagement_id": "eng-1", "text": "hi",
                  "engagement_token": "tok-eng-1"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"ok": False, "error": "no_topic_bound"}

    assert ch.calls == []


async def test_send_to_topic_advances_interaction_state_first_contact(
    app_factory,
) -> None:
    """W2/Sol B9 (Task 7): a successful reply-through-send_to_topic is the
    agent's outbound act — fires advance_interaction_state(eng, "first_contact")."""
    app, ch, reg = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/send_to_topic",
            json={"engagement_id": "eng-1", "text": "hello operator",
                  "engagement_token": "tok-eng-1"},
        )
        assert resp.status == 200
    assert reg.advances == [("eng-1", "first_contact")]


class _FakeIntentDriver:
    """Minimal claude_code-driver fake for the DEFERRED reply path (§2 C1):
    registration succeeds, arm is a no-op, and awaiting the intent invokes
    the installed poster inline (as the relay would at the tool_use block).
    Carries the #332 turn-reply-target seams."""

    def __init__(self, reply_target: int | None = 555) -> None:
        self.posters: dict[str, Any] = {}
        self.reply_target = reply_target
        self.restored: list[int | None] = []

    def register_send_intent(self, **kwargs: Any):
        return (object(), True)

    def set_send_intent_poster(self, eng_id: str, rid: str, poster: Any) -> None:
        self.posters[rid] = poster

    def arm_send_intent(self, eng_id: str, rid: str) -> None:
        pass

    async def await_send_intent(self, eng_id: str, rid: str) -> dict | None:
        mid = await self.posters[rid]()
        return {"ok": mid is not None, "message_id": mid}

    def consume_turn_reply_to(self, eng_id: str) -> int | None:
        target, self.reply_target = self.reply_target, None
        return target

    def restore_turn_reply_to(self, eng_id: str, mid: int | None) -> None:
        self.restored.append(mid)
        if self.reply_target is None:
            self.reply_target = mid


async def test_deferred_reply_first_output_threads_to_inbound(
    app_factory, monkeypatch,
) -> None:
    """#332: a deferred reply that is the turn's first output must consume
    the sequencer's one-shot turn reply target and thread to the operator's
    inbound message."""
    from channels import channel_handlers as ch_mod

    driver = _FakeIntentDriver(reply_target=555)
    monkeypatch.setattr(ch_mod, "_resolve_active_driver", lambda: driver)
    app, ch, _reg = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/send_to_topic",
            json={"engagement_id": "eng-1", "text": "hello operator",
                  "engagement_token": "tok-eng-1",
                  "request_id": "r1", "projection_hash": "h1"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True

    assert len(ch.calls) == 1
    assert ch.calls[0]["kwargs"].get("reply_to_message_id") == 555
    assert driver.reply_target is None  # consumed by the successful post


async def test_deferred_reply_cancelled_send_restores_reply_target(
    app_factory, monkeypatch,
) -> None:
    """Terra r1 (#332): task cancellation bypasses ``except Exception`` — a
    cancelled poster must still restore the consumed one-shot target when no
    message id was recorded."""
    from channels import channel_handlers as ch_mod

    driver = _FakeIntentDriver(reply_target=555)

    # First run the handler with a NON-invoking await seam so the poster
    # closure is captured without being executed.
    async def _no_invoke(eng_id, rid):
        return {"ok": True, "message_id": 1}

    driver.await_send_intent = _no_invoke  # type: ignore[method-assign]

    class _CancellingChannel(_FakeChannel):
        async def send_response_to_topic(self, thread_id, text, **kwargs):
            raise asyncio.CancelledError()

    monkeypatch.setattr(ch_mod, "_resolve_active_driver", lambda: driver)
    app, _ch, _reg = app_factory(channel=_CancellingChannel())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/send_to_topic",
            json={"engagement_id": "eng-1", "text": "hello",
                  "engagement_token": "tok-eng-1",
                  "request_id": "r1", "projection_hash": "h1"},
        )
        assert resp.status == 200

    poster = driver.posters["r1"]
    driver.reply_target = 999
    with pytest.raises(asyncio.CancelledError):
        await poster()
    assert driver.restored == [999]
    assert driver.reply_target == 999  # re-armed


async def test_deferred_reply_failed_send_restores_reply_target(
    app_factory, monkeypatch,
) -> None:
    """#332 failure arm: a consumed-but-unsent reply target is restored so
    the turn's first SUCCESSFUL output still threads."""
    from channels import channel_handlers as ch_mod

    class _FailingChannel(_FakeChannel):
        async def send_response_to_topic(self, thread_id, text, **kwargs):
            raise RuntimeError("wire down")

    driver = _FakeIntentDriver(reply_target=555)
    monkeypatch.setattr(ch_mod, "_resolve_active_driver", lambda: driver)
    app, _ch, _reg = app_factory(channel=_FailingChannel())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/send_to_topic",
            json={"engagement_id": "eng-1", "text": "hello operator",
                  "engagement_token": "tok-eng-1",
                  "request_id": "r1", "projection_hash": "h1"},
        )
        body = await resp.json()
        assert body["ok"] is False

    assert driver.restored == [555]
    assert driver.reply_target == 555  # re-armed for the next output


# ---------------------------------------------------------------------------
# Phase 2 — /internal/channel/post_inline_keyboard (Task 19)
# ---------------------------------------------------------------------------


async def test_post_inline_keyboard_routes_to_topic(app_factory) -> None:
    """Handler resolves engagement_id, builds an InlineKeyboardMarkup with the
    operator buttons, and forwards reply_markup + parse_mode to the channel."""
    app, ch, _reg = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/post_inline_keyboard",
            json={
                "engagement_id": "eng-1",
                "engagement_token": "tok-eng-1",
                "text": "approve?",
                "buttons": [[
                    {"text": "✅ Allow", "callback_data": "perm:allow:rid"},
                    {"text": "❌ Deny", "callback_data": "perm:deny:rid"},
                ]],
                "parse_mode": "MarkdownV2",
            },
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True

    assert len(ch.calls) == 1
    call = ch.calls[0]
    assert call["topic_id"] == 42
    assert call["text"] == "approve?"
    reply_markup = call["kwargs"].get("reply_markup")
    assert reply_markup is not None
    # _FakeInlineKeyboardMarkup exposes .inline_keyboard.
    rows = reply_markup.inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 2
    assert rows[0][0].text == "✅ Allow"
    assert rows[0][0].callback_data == "perm:allow:rid"
    assert rows[0][1].text == "❌ Deny"
    assert rows[0][1].callback_data == "perm:deny:rid"
    assert call["kwargs"].get("parse_mode") == "MarkdownV2"


async def test_post_inline_keyboard_supports_url_buttons(app_factory) -> None:
    """U6: buttons with ``url=`` (no callback_data) round-trip through to TG."""
    app, ch, _reg = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/post_inline_keyboard",
            json={
                "engagement_id": "eng-1",
                "engagement_token": "tok-eng-1",
                "text": "open remote",
                "buttons": [[
                    {"text": "🌐 Open Remote Control",
                     "url": "https://rc.example/abc"},
                ]],
            },
        )
        assert resp.status == 200
    btn = ch.calls[0]["kwargs"]["reply_markup"].inline_keyboard[0][0]
    assert btn.url == "https://rc.example/abc"
    assert btn.callback_data is None


async def test_post_inline_keyboard_plain_body_shows_no_escape_backslash(
    app_factory,
) -> None:
    """#1330: a body with no formatting span posts the text the rich path
    would show — escapes consumed — not the authored ``\\-``."""
    app, ch, _reg = app_factory()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/post_inline_keyboard",
            json={
                "engagement_id": "eng-1",
                "engagement_token": "tok-eng-1",
                "text": "chose CUWVSRB8\\-0007",
                "buttons": [[{"text": "Ok", "callback_data": "ok"}]],
            },
        )
        assert resp.status == 200
    assert ch.calls[0]["text"] == "chose CUWVSRB8-0007"
    assert "entities" not in ch.calls[0]["kwargs"]


async def test_post_inline_keyboard_unknown_engagement_returns_error(
    app_factory,
) -> None:
    reg = _FakeRegistry()
    reg.set_record("eng-1", None)
    app, ch, _reg = app_factory(registry=reg)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/post_inline_keyboard",
            json={"engagement_id": "missing", "text": "x",
                  "buttons": [[{"text": "a", "callback_data": "b"}]]},
        )
        body = await resp.json()
        assert body == {"ok": False, "error": "unknown_engagement"}
    assert ch.calls == []


async def test_post_inline_keyboard_send_failure_returns_error(
    app_factory,
) -> None:
    class _ExplodingChannel(_FakeChannel):
        async def send_to_topic(self, thread_id, text, **kwargs):
            raise RuntimeError("telegram down")

    app, ch, _reg = app_factory(channel=_ExplodingChannel())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/post_inline_keyboard",
            json={"engagement_id": "eng-1", "text": "x",
                  "engagement_token": "tok-eng-1",
                  "buttons": [[{"text": "a", "callback_data": "b"}]]},
        )
        body = await resp.json()
        assert body == {"ok": False, "error": "send_failed"}


# ---------------------------------------------------------------------------
# v0.75.0 (W5/Sol B3,B4) — permission verdict → verdict_broker.BROKER.deliver
# ---------------------------------------------------------------------------


@pytest.fixture
def channel_full_app_factory():
    """Build an aiohttp app with BOTH POST handlers from _make_channel_handlers
    AND the (now empty — v0.75.0 removed permission_pending) GET handlers
    from _make_channel_get_handlers.
    """
    from channels.channel_handlers import (
        _make_channel_handlers,
        _make_channel_get_handlers,
    )

    def make(channel=None, registry=None):
        ch = channel or _FakeChannel()
        reg = registry or _FakeRegistry()
        post_handlers = _make_channel_handlers(
            telegram_channel=ch, engagement_registry=reg,
        )
        get_handlers = _make_channel_get_handlers(engagement_registry=reg)
        app = web.Application()
        for path, h in post_handlers.items():
            app.router.add_post(path, h)
        for path, h in get_handlers.items():
            app.router.add_get(path, h)
        return app, ch, reg

    yield make


@pytest.fixture
def _fresh_broker(monkeypatch):
    """Isolate broker-touching tests on their own VerdictBroker — the
    handler resolves ``from verdict_broker import BROKER`` per-request, so
    redirecting the module attribute here is picked up transparently."""
    import verdict_broker
    from verdict_broker import VerdictBroker

    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


async def test_get_channel_handlers_no_longer_registers_permission_pending() -> None:
    """v0.75.0: the queue+long-poll indirection is retired — verdicts flow
    through the in-process Telegram callback claim/commit (the POST route itself was removed in #469)."""
    from channels.channel_handlers import _make_channel_get_handlers

    handlers = _make_channel_get_handlers(engagement_registry=_FakeRegistry())
    assert "/internal/channel/permission_pending" not in handlers
    assert handlers == {}


async def test_permission_verdict_route_gone() -> None:
    """#469 pinning: permission verdicts have NO internal writer. The route
    authenticated only the engagement's own token, so an executor holding its
    workspace credential could POST an allow for its own pending gated tool
    call — a silent self-approval. The only remaining verdict writer is the
    in-process Telegram callback (operator-tap-authenticated claim/commit)."""
    from channels import channel_handlers as ch
    from channels.channel_handlers import _make_channel_handlers

    handlers = _make_channel_handlers(
        telegram_channel=_FakeChannel(), engagement_registry=_FakeRegistry(),
    )
    assert "/internal/channel/permission_verdict" not in handlers
    assert not hasattr(ch, "_make_permission_verdict")


async def test_permission_verdict_post_is_404(
    channel_full_app_factory, _fresh_broker,
) -> None:
    """#469 red case: the exact self-approval POST an executor engagement
    could forge (own engagement id + own token + own tool_use_id-derived
    request_id) must not resolve the pending permission request."""
    reg = _FakeRegistry()
    reg.set_record("eng-1", _FakeRecord("eng-1", topic_id=42, status="active"))
    app, _ch, _reg = channel_full_app_factory(registry=reg)

    req, created = _fresh_broker.register(
        namespace="permission", scope="eng-1", request_id="rid-001",
        timeout_s=5.0, meta={"operator_id": 999},
    )
    assert created is True

    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/permission_verdict",
            json={"engagement_id": "eng-1", "request_id": "rid-001",
                  "engagement_token": "tok-eng-1",
                  "verdict": "allow", "operator_id": 999},
        )
        assert resp.status == 404
    # Never delivered: the request is still live/unresolved.
    assert _fresh_broker.pending(namespace="permission", scope="eng-1") == [
        "rid-001",
    ]


# ---------------------------------------------------------------------------
# Phase 2 — /internal/channel/update_state (Task 23)
# ---------------------------------------------------------------------------


class _StateTrackingChannel(_FakeChannel):
    """Captures update_topic_state calls so tests can assert on them."""

    def __init__(self) -> None:
        super().__init__()
        self.state_transitions: list[dict[str, Any]] = []

    async def update_topic_state(self, *, engagement_id: str, new_state: str):
        self.state_transitions.append(
            {"engagement_id": engagement_id, "new_state": new_state},
        )


async def test_update_state_calls_telegram_helper(app_factory) -> None:
    """Task 23: /internal/channel/update_state forwards to
    TelegramChannel.update_topic_state for the per-engagement title edit."""
    ch = _StateTrackingChannel()
    app, ch, _reg = app_factory(channel=ch)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/update_state",
            json={"engagement_id": "eng-1", "new_state": "awaiting",
                  "engagement_token": "tok-eng-1"},
        )
        assert (await resp.json()) == {"ok": True}

    assert ch.state_transitions == [
        {"engagement_id": "eng-1", "new_state": "awaiting"},
    ]


async def test_update_state_unknown_state_is_dropped_gracefully(
    app_factory,
) -> None:
    """Unknown state shouldn't raise — channel helper logs + drops."""
    class _NoopChannel(_FakeChannel):
        async def update_topic_state(self, *, engagement_id, new_state):
            return

    app, _ch, _reg = app_factory(channel=_NoopChannel())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/update_state",
            json={"engagement_id": "eng-1", "new_state": "made-up",
                  "engagement_token": "tok-eng-1"},
        )
        # The handler is forgiving — the channel decides what's a valid state.
        assert (await resp.json()) == {"ok": True}


async def test_update_state_bad_json_returns_error(app_factory) -> None:
    app, _ch, _reg = app_factory(channel=_StateTrackingChannel())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/update_state",
            data="not json", headers={"Content-Type": "application/json"},
        )
        assert (await resp.json()) == {"ok": False, "error": "bad_json"}


async def test_update_state_channel_failure_returns_error(app_factory) -> None:
    class _ExplodingChannel(_FakeChannel):
        async def update_topic_state(self, *, engagement_id, new_state):
            raise RuntimeError("edit_forum_topic timeout")

    app, _ch, _reg = app_factory(channel=_ExplodingChannel())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/update_state",
            json={"engagement_id": "eng-1", "new_state": "awaiting",
                  "engagement_token": "tok-eng-1"},
        )
        assert (await resp.json()) == {"ok": False, "error": "update_failed"}
