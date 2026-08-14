# tests/test_engagement_ask.py
"""Unit tests for the engagement_ask surface (v0.75.0 W5, Task 3).

Covers three layers:
  - ``POST /internal/channel/ask`` + ``ask_cancel`` (``channels.channel_handlers``),
    exercised via the ``TestClient(TestServer(app))`` harness (mirrors
    ``tests/test_internal_handlers.py`` / ``tests/test_channel_internal_handlers.py``).
  - ``TelegramChannel.post_options_keyboard`` / ``edit_topic_message`` /
    ``delete_topic_message`` (``channels.telegram``), exercised directly via
    ``TelegramChannel.__new__`` (mirrors ``tests/test_telegram_post_perm_keyboard.py``).

Two tests (cancellation nuance: mid-post disconnect, mid-await disconnect)
invoke the ``/internal/channel/ask`` handler function DIRECTLY (via a minimal
``_FakeRequest`` stand-in) instead of going through a real TestClient/TestServer
HTTP round trip. A genuine client-side `.cancel()` racing an in-flight aiohttp
TCP connection is timing-dependent on the transport actually tearing down and
the server noticing — direct handler-task cancellation gives the same
broker-level guarantee (a shielded ``await_result``/``ensure_posted`` seeing
``asyncio.CancelledError`` at its own await point) deterministically, matching
the style already used for the analogous permission-relay hook cancellation
tests in ``tests/test_hooks_engagement_permission_relay.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from broker_helpers import deliver, wait_until
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import verdict_broker
from verdict_broker import VerdictBroker

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRecord:
    def __init__(
        self, eng_id: str, *, topic_id: int | None, status: str = "active",
        origin: dict | None = None,
    ) -> None:
        self.id = eng_id
        self.topic_id = topic_id
        self.status = status
        self.origin = origin if origin is not None else {"user_id": 999}
        # #335: per-engagement secret; bodies must present it as
        # ``engagement_token`` to act with this engagement's authority.
        self.auth_token = f"tok-{eng_id}"


class _FakeRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, _FakeRecord] = {
            "eng-1": _FakeRecord("eng-1", topic_id=42),
        }
        # W2/Sol B9 (Task 7): the `ask` handler's first_contact seam calls
        # this — track for the dedicated assert below.
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


class _AskFakeChannel:
    """Capture-fake for the two TelegramChannel methods the ask handler
    calls. Records posted keyboards and finish-hook edits, categorized by
    whether the edited text reads as an "answered" or "expired"/other
    resolution (matching ``channel_handlers._ask_keyboard_finish``'s text
    shape: "... Answered: <label>" vs "... Expired")."""

    def __init__(self) -> None:
        self.options_keyboards: list[dict[str, Any]] = []
        self.edited_answered: list[tuple] = []
        self.edited_expired: list[tuple] = []
        self.edited_clear_keyboard: list[bool] = []
        self.sent_texts: list[tuple] = []
        self._next_id = 9000

    async def post_options_keyboard(
        self, *, engagement_id, request_id, question, options,
    ) -> int | None:
        self.options_keyboards.append({
            "engagement_id": engagement_id, "request_id": request_id,
            "question": question, "options": list(options),
        })
        mid = self._next_id
        self._next_id += 1
        return mid

    async def send_response_to_topic(self, topic_id, text) -> int:
        self.sent_texts.append((topic_id, text))
        mid = self._next_id
        self._next_id += 1
        return mid

    async def edit_topic_message(
        self, topic_id, message_id, text, *, clear_keyboard=False,
    ) -> bool:
        # v0.79.0 §4 settle copy: answered ⇒ "✅ <label>", else expired/other.
        self.edited_clear_keyboard.append(clear_keyboard)
        if "✅" in text:
            self.edited_answered.append((topic_id, message_id, text))
        else:
            self.edited_expired.append((topic_id, message_id, text))
        return True

    # R2c: the single-select finish-hook settle routes through the rich edit
    # primitive; the fake delegates (no wire render).
    async def edit_topic_message_rich(
        self, topic_id, message_id, text, *, clear_keyboard=False,
    ) -> bool:
        return await self.edit_topic_message(
            topic_id, message_id, text, clear_keyboard=clear_keyboard)


class _FakeRequest:
    """Minimal aiohttp.web.Request stand-in — the handler only calls
    ``await request.json()``."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


def _body(resp: web.Response) -> dict:
    return json.loads(resp.text)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _fresh_broker(monkeypatch):
    fresh = VerdictBroker()
    # #562: the tests below must wait for a CONDITION -- "the handler has
    # reached ``BROKER.register``" -- not for a fixed wall-clock sleep, which
    # is no barrier at all under the parallel gate (12 workers on a loaded
    # box). ``register_counts[request_id]`` is the observable for that
    # condition, and it is the only one that can see a SECOND arrival for the
    # same request_id: a keyboard is posted once per request_id, and the
    # broker's own ``pending()`` is a set of live keys, so neither counts.
    counts: dict[str, int] = {}
    real_register = fresh.register

    def _counting_register(**kw):
        out = real_register(**kw)
        counts[kw["request_id"]] = counts.get(kw["request_id"], 0) + 1
        return out

    fresh.register = _counting_register  # type: ignore[method-assign]
    fresh.register_counts = counts  # type: ignore[attr-defined]
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


@pytest.fixture
def app_with_ask(_fresh_broker):
    from channels.channel_handlers import _make_channel_handlers

    reg = _FakeRegistry()
    ch = _AskFakeChannel()
    handlers = _make_channel_handlers(telegram_channel=ch, engagement_registry=reg)
    app = web.Application()
    for path, h in handlers.items():
        app.router.add_post(path, h)
    return app, ch, reg, _fresh_broker


@pytest.fixture
def ask_handler_direct(_fresh_broker):
    from channels.channel_handlers import _make_channel_handlers

    reg = _FakeRegistry()
    ch = _AskFakeChannel()
    handlers = _make_channel_handlers(telegram_channel=ch, engagement_registry=reg)
    return handlers["/internal/channel/ask"], ch, reg, _fresh_broker


def _payload(**overrides) -> dict:
    base = {
        "engagement_id": "eng-1", "request_id": "rid-default",
        "engagement_token": "tok-eng-1",
        "question": "Proceed?", "options": ["A", "B"], "timeout_s": 60,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Readiness barriers (#562)
# ---------------------------------------------------------------------------
#
# Every `deliver()` below needs the ask to have reached a specific state
# first; a fixed ``asyncio.sleep(0.05)`` only *usually* gets there. These wait
# on the state itself (bounded by ``wait_until``'s wall-clock timeout, so a
# condition that never fires still FAILS rather than hanging).


async def _await_registered(broker, request_id: str, n: int = 1) -> None:
    """Wait until *n* handlers have reached ``BROKER.register`` for
    *request_id* (n=2 for a concurrent pair / a reattaching retry)."""
    await wait_until(lambda: broker.register_counts.get(request_id, 0) >= n)


async def _await_posted(broker, ch, request_id: str, n_reg: int = 1) -> None:
    """Wait until the ask is registered AND its keyboard has been posted.

    The keyboard post is what wires the finish hook and the ``message_id``
    (``verdict_broker._run_setup``), and ``ensure_posted`` declines to post at
    all once the future is already resolved — so delivering before this point
    can leave a test with zero keyboards and no settle edit."""
    await wait_until(
        lambda: (
            broker.register_counts.get(request_id, 0) >= n_reg
            and len(ch.options_keyboards) >= 1
        )
    )


# ---------------------------------------------------------------------------
# /internal/channel/ask — HTTP-level tests (TestClient/TestServer)
# ---------------------------------------------------------------------------


async def test_ask_answered_edits_answered(app_with_ask) -> None:
    app, ch, _reg, broker = app_with_ask
    async with TestClient(TestServer(app)) as client:
        task = asyncio.ensure_future(client.post(
            "/internal/channel/ask", json=_payload(request_id="rid-1"),
        ))
        await _await_posted(broker, ch, "rid-1")
        assert deliver(broker, 
            namespace="engagement_ask", scope="eng-1", request_id="rid-1",
            option_index=1, actor_id=999,
        ) == "delivered"
        resp = await asyncio.wait_for(task, timeout=1.0)
        body = await resp.json()
        await broker.drain_hooks()

    assert body == {
        "ok": True, "outcome": "answered", "option": "B", "option_index": 1,
    }
    assert len(ch.options_keyboards) == 1
    assert len(ch.edited_answered) == 1
    # v0.84.0 (round 4, D1 bullet 3): settle copy is the BOUNDED positional
    # form (never the chosen full label) — option_index=1 is position 2.
    assert "Option 2" in ch.edited_answered[0][2]
    assert ch.edited_expired == []


async def test_ask_calls_advance_interaction_state_first_contact(
    app_with_ask,
) -> None:
    """W2/Sol B9 (Task 7): asking is an outbound agent action too — the
    `ask` handler fires advance_interaction_state(eng, "first_contact")
    once the engagement/status checks pass (before registering the
    broker request)."""
    app, ch, reg, broker = app_with_ask
    async with TestClient(TestServer(app)) as client:
        task = asyncio.ensure_future(client.post(
            "/internal/channel/ask", json=_payload(request_id="rid-fc"),
        ))
        await _await_posted(broker, ch, "rid-fc")
        assert deliver(broker, 
            namespace="engagement_ask", scope="eng-1", request_id="rid-fc",
            option_index=0, actor_id=999,
        ) == "delivered"
        await asyncio.wait_for(task, timeout=1.0)

    assert reg.advances == [("eng-1", "first_contact")]


async def test_ask_timeout_no_answer_edits_expired(
    app_with_ask, monkeypatch,
) -> None:
    import channels.channel_handlers as ch_mod
    # Shrink the clamp floor so a real (but tiny) broker timeout fires fast
    # without waiting out the real 30s minimum.
    monkeypatch.setattr(ch_mod, "_ASK_MIN_TIMEOUT_S", 0.05)

    app, ch, _reg, broker = app_with_ask
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/ask",
            json=_payload(request_id="rid-2", timeout_s=0.05),
        )
        body = await resp.json()
        await broker.drain_hooks()

    assert body == {"ok": True, "outcome": "no_answer"}
    assert len(ch.edited_expired) == 1
    assert ch.edited_answered == []


async def test_ask_only_creator_posts_one_keyboard(app_with_ask) -> None:
    """B2: two concurrent POSTs for the same request_id must post exactly
    ONE keyboard, and both responses carry the same answer."""
    app, ch, _reg, broker = app_with_ask
    async with TestClient(TestServer(app)) as client:
        t1 = asyncio.ensure_future(client.post(
            "/internal/channel/ask", json=_payload(request_id="rid-x"),
        ))
        t2 = asyncio.ensure_future(client.post(
            "/internal/channel/ask", json=_payload(request_id="rid-x"),
        ))
        # #562: ``n_reg=2`` is the test's PREMISE, not the flake fix — without
        # it a slow second POST could still be pre-``register`` when the
        # verdict lands, and the "two concurrent POSTs" case would silently
        # degrade to a plain sequential reattach (which the 60s retired-key
        # tombstone answers identically, so nothing would fail). The flake
        # itself is the registered+posted clause of ``_await_posted``.
        await _await_posted(broker, ch, "rid-x", n_reg=2)
        assert deliver(broker, 
            namespace="engagement_ask", scope="eng-1", request_id="rid-x",
            option_index=0, actor_id=999,
        ) == "delivered"
        r1, r2 = await asyncio.gather(t1, t2)
        b1, b2 = await r1.json(), await r2.json()

    assert b1 == b2 == {
        "ok": True, "outcome": "answered", "option": "A", "option_index": 0,
    }
    assert len(ch.options_keyboards) == 1


async def test_ask_validation_clamps_and_rejects(
    app_with_ask,
) -> None:
    app, ch, _reg, broker = app_with_ask
    async with TestClient(TestServer(app)) as client:
        # 1 option
        resp = await client.post("/internal/channel/ask", json=_payload(
            request_id="v1", options=["A"]))
        assert (await resp.json()) == {"ok": False, "error": "invalid_args"}
        # 9 options
        resp = await client.post("/internal/channel/ask", json=_payload(
            request_id="v2", options=[f"o{i}" for i in range(9)]))
        assert (await resp.json()) == {"ok": False, "error": "invalid_args"}
        # duplicate options
        resp = await client.post("/internal/channel/ask", json=_payload(
            request_id="v3", options=["A", "A"]))
        assert (await resp.json()) == {"ok": False, "error": "invalid_args"}
        # D1 (round 4, spec §D1): the invented 1024-char question cap and the
        # 48-char label cap are REMOVED — a long question / long label is now
        # ACCEPTED, not rejected. See test_ask_accepts_long_question_and_label
        # below for the positive (posts-a-keyboard) coverage.

    assert ch.options_keyboards == []  # none of the invalid ones ever posted

    # Clamps: timeout_s=20 -> 30 (floor); timeout_s=600 -> 570 (ceiling).
    # A blocking post lets us peek the *registered* TTL before letting the
    # request resolve (never actually wait out a real 30s/570s timer).
    release = asyncio.Event()

    async def blocking_post(**kw):
        await release.wait()
        return 1

    ch.post_options_keyboard = blocking_post

    async with TestClient(TestServer(app)) as client:
        t_lo = asyncio.ensure_future(client.post(
            "/internal/channel/ask",
            json=_payload(request_id="clamp-lo", timeout_s=20),
        ))
        await _await_registered(broker, "clamp-lo")
        req_lo = broker._live[("engagement_ask", "eng-1", "clamp-lo")]
        assert req_lo.timeout_s == 30.0
        broker.cancel(namespace="engagement_ask", scope="eng-1",
                      request_id="clamp-lo", reason="test_cleanup")
        release.set()
        resp_lo = await asyncio.wait_for(t_lo, timeout=1.0)
        assert (await resp_lo.json()) == {"ok": False, "error": "cancelled"}
        release.clear()

        t_hi = asyncio.ensure_future(client.post(
            "/internal/channel/ask",
            json=_payload(request_id="clamp-hi", timeout_s=600),
        ))
        await _await_registered(broker, "clamp-hi")
        req_hi = broker._live[("engagement_ask", "eng-1", "clamp-hi")]
        assert req_hi.timeout_s == 570.0
        broker.cancel(namespace="engagement_ask", scope="eng-1",
                      request_id="clamp-hi", reason="test_cleanup")
        release.set()
        resp_hi = await asyncio.wait_for(t_hi, timeout=1.0)
        assert (await resp_hi.json()) == {"ok": False, "error": "cancelled"}


async def test_ask_accepts_long_question_and_label(app_with_ask) -> None:
    """D1 (round 4, spec §D1 bullets 1-2): the invented ``_ASK_MAX_QUESTION_LEN``
    (1024) and ``_ASK_MAX_LABEL_LEN`` (48) caps are gone — a long question and
    a long option label are ACCEPTED end-to-end (keyboard posts, tap answers)
    rather than refused ``invalid_args``."""
    app, ch, _reg, broker = app_with_ask
    long_question = "x" * 1025
    long_label = "y" * 49
    async with TestClient(TestServer(app)) as client:
        task = asyncio.ensure_future(client.post(
            "/internal/channel/ask",
            json=_payload(
                request_id="rid-long", question=long_question,
                options=[long_label, "B"]),
        ))
        await _await_posted(broker, ch, "rid-long")
        assert deliver(broker, 
            namespace="engagement_ask", scope="eng-1", request_id="rid-long",
            option_index=0, actor_id=999,
        ) == "delivered"
        resp = await asyncio.wait_for(task, timeout=1.0)
        body = await resp.json()
        await broker.drain_hooks()

    assert body == {
        "ok": True, "outcome": "answered", "option": long_label,
        "option_index": 0,
    }
    assert len(ch.options_keyboards) == 1
    assert long_question in ch.options_keyboards[0]["question"]
    assert long_label in ch.options_keyboards[0]["question"]


async def test_ask_keyboard_post_failure_unregisters(app_with_ask) -> None:
    app, ch, _reg, broker = app_with_ask

    async def raising_post(**kw):
        raise RuntimeError("network down")

    ch.post_options_keyboard = raising_post
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/ask", json=_payload(request_id="rid-3"),
        )
        body = await resp.json()

    assert body == {"ok": False, "error": "delivery_failed"}
    assert broker.pending(namespace="engagement_ask", scope="eng-1") == []


async def test_ask_keyboard_post_returns_none_is_delivery_failed(
    app_with_ask,
) -> None:
    """r10-B3: post_options_keyboard returning None (unresolvable
    engagement/topic) is a delivery FAILURE too -- unregister, no finish
    hook ever installed."""
    app, ch, _reg, broker = app_with_ask

    async def none_post(**kw):
        return None

    ch.post_options_keyboard = none_post
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/ask", json=_payload(request_id="rid-4"),
        )
        body = await resp.json()

    assert body == {"ok": False, "error": "delivery_failed"}
    assert broker.pending(namespace="engagement_ask", scope="eng-1") == []
    assert ch.edited_answered == [] and ch.edited_expired == []


async def test_ask_delivery_failed_does_not_advance_first_contact(
    app_with_ask,
) -> None:
    """B5 (Sol r1): first_contact must advance only AFTER the keyboard is
    actually posted. A raising keyboard post → delivery_failed → the
    interaction_state must NOT flip to awaiting_operator (otherwise the
    engagement awaits a question the operator never received)."""
    app, ch, reg, broker = app_with_ask

    async def raising_post(**kw):
        raise RuntimeError("network down")

    ch.post_options_keyboard = raising_post
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/ask", json=_payload(request_id="rid-nf"),
        )
        body = await resp.json()

    assert body == {"ok": False, "error": "delivery_failed"}
    assert reg.advances == []  # never advanced past first_contact_required


async def test_ask_delivery_failed_none_post_does_not_advance_first_contact(
    app_with_ask,
) -> None:
    """B5 (Sol r1): a keyboard post that returns None (unresolvable topic) is
    also a delivery failure — no first_contact advance."""
    app, ch, reg, broker = app_with_ask

    async def none_post(**kw):
        return None

    ch.post_options_keyboard = none_post
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/ask", json=_payload(request_id="rid-nf2"),
        )
        body = await resp.json()

    assert body == {"ok": False, "error": "delivery_failed"}
    assert reg.advances == []


async def test_ask_terminal_engagement_rejected(app_with_ask) -> None:
    app, ch, reg, _broker = app_with_ask
    reg.set_record("eng-1", _FakeRecord("eng-1", topic_id=42, status="completed"))
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/ask", json=_payload(request_id="rid-5"),
        )
        body = await resp.json()

    assert body == {"ok": False, "error": "engagement_terminal"}
    assert ch.options_keyboards == []


async def test_ask_cancel_route_is_explicit_cancellation(app_with_ask) -> None:
    app, ch, _reg, broker = app_with_ask
    async with TestClient(TestServer(app)) as client:
        t1 = asyncio.ensure_future(client.post(
            "/internal/channel/ask", json=_payload(request_id="rid-ec"),
        ))
        await _await_posted(broker, ch, "rid-ec")
        resp_cancel = await client.post(
            "/internal/channel/ask_cancel",
            json={"engagement_id": "eng-1", "request_id": "rid-ec",
                  "engagement_token": "tok-eng-1"},
        )
        assert (await resp_cancel.json()) == {"ok": True}

        resp1 = await asyncio.wait_for(t1, timeout=1.0)
        body1 = await resp1.json()

    assert body1 == {"ok": False, "error": "cancelled"}
    assert broker.pending(namespace="engagement_ask", scope="eng-1") == []


# ---------------------------------------------------------------------------
# Direct-invocation tests — cancellation nuance (see module docstring).
# ---------------------------------------------------------------------------


async def test_ask_cancelled_during_post_completes_setup_one_keyboard(
    ask_handler_direct,
) -> None:
    """r8-B3 (supersedes r7 unregister-on-cancel): cancelling the awaiting
    handler task WHILE the keyboard post is still in flight must not
    duplicate the post -- the shielded broker setup task completes in the
    background; a same-id retry reattaches (created=False) and awaits the
    SAME setup -- exactly one keyboard send recorded overall."""
    handler, ch, _reg, broker = ask_handler_direct

    post_started = asyncio.Event()
    release_post = asyncio.Event()

    async def slow_post(**kw):
        ch.options_keyboards.append(dict(kw))
        post_started.set()
        await release_post.wait()
        return 777

    ch.post_options_keyboard = slow_post

    payload = _payload(request_id="rid-6")

    task = asyncio.create_task(handler(_FakeRequest(payload)))
    await asyncio.wait_for(post_started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The shielded setup task is still in flight (blocked on release_post).
    release_post.set()
    await broker.drain_hooks()

    assert len(ch.options_keyboards) == 1

    # A same-id retry reattaches (created=False) -- awaits the SAME setup,
    # no second post.
    task2 = asyncio.create_task(handler(_FakeRequest(payload)))
    await _await_registered(broker, "rid-6", 2)
    assert deliver(broker, 
        namespace="engagement_ask", scope="eng-1", request_id="rid-6",
        option_index=0, actor_id=999,
    ) == "delivered"
    resp2 = await asyncio.wait_for(task2, timeout=1.0)
    body2 = _body(resp2)

    assert body2 == {
        "ok": True, "outcome": "answered", "option": "A", "option_index": 0,
    }
    assert len(ch.options_keyboards) == 1  # still exactly one post overall


async def test_ask_disconnect_leaves_pending_for_reattach(
    ask_handler_direct,
) -> None:
    """r2-B2: transport disconnect != cancel. Cancelling the awaiting
    handler task (simulating a dropped HTTP connection) while it's waiting
    on the operator's answer must NOT resolve the broker request -- it
    stays pending for a same-id reattach."""
    handler, ch, _reg, broker = ask_handler_direct

    payload = _payload(request_id="rid-7")

    task = asyncio.create_task(handler(_FakeRequest(payload)))
    await _await_posted(broker, ch, "rid-7")  # registered + posted
    assert broker.pending(namespace="engagement_ask", scope="eng-1") == [
        "rid-7",
    ]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # STILL pending -- the disconnect never called BROKER.cancel.
    assert broker.pending(namespace="engagement_ask", scope="eng-1") == [
        "rid-7",
    ]

    task2 = asyncio.create_task(handler(_FakeRequest(payload)))
    await _await_registered(broker, "rid-7", 2)
    assert deliver(broker, 
        namespace="engagement_ask", scope="eng-1", request_id="rid-7",
        option_index=0, actor_id=999,
    ) == "delivered"
    resp2 = await asyncio.wait_for(task2, timeout=1.0)
    body2 = _body(resp2)

    assert body2 == {
        "ok": True, "outcome": "answered", "option": "A", "option_index": 0,
    }
    assert len(ch.options_keyboards) == 1


async def test_ask_broker_finish_hook_edits_answered_not_handler_not_callback(
    ask_handler_direct,
) -> None:
    """r3-B3/r5-B7: the BROKER finish-hook owns the keyboard/text edit --
    fires once on outcome even when the awaiting ask HANDLER was cancelled
    (disconnect) before the answer arrived, proving the edit is not
    handler-code (nothing "handler-side" is alive to have done it)."""
    handler, ch, _reg, broker = ask_handler_direct

    payload = _payload(request_id="rid-fh")
    task = asyncio.create_task(handler(_FakeRequest(payload)))
    await _await_posted(broker, ch, "rid-fh")  # now awaiting the tap

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert deliver(broker, 
        namespace="engagement_ask", scope="eng-1", request_id="rid-fh",
        option_index=0, actor_id=999,
    ) == "delivered"
    await broker.drain_hooks()

    assert len(ch.edited_answered) == 1
    assert ch.edited_expired == []


# ---------------------------------------------------------------------------
# /internal/channel/ask_cancel — validation
# ---------------------------------------------------------------------------


async def test_ask_cancel_missing_args_returns_invalid_args(app_with_ask) -> None:
    app, _ch, _reg, _broker = app_with_ask
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/internal/channel/ask_cancel", json={})
        assert (await resp.json()) == {"ok": False, "error": "invalid_args"}


async def test_ask_cancel_no_live_request_is_still_ok(app_with_ask) -> None:
    app, _ch, _reg, _broker = app_with_ask
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/internal/channel/ask_cancel",
            json={"engagement_id": "eng-1", "request_id": "no-such-rid",
                  "engagement_token": "tok-eng-1"},
        )
        assert (await resp.json()) == {"ok": True}


# ---------------------------------------------------------------------------
# TelegramChannel.post_options_keyboard
# ---------------------------------------------------------------------------


class TestPostOptionsKeyboard:
    async def test_composes_one_button_per_option(self) -> None:
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch._engagement_registry = MagicMock()
        ch._engagement_registry.get = MagicMock(
            return_value=MagicMock(topic_id=42),
        )
        ch.send_to_topic = AsyncMock(return_value=101)

        msg_id = await ch.post_options_keyboard(
            engagement_id="x" * 32, request_id="rid_ask",
            question="Proceed?", options=["Yes", "No", "Maybe"],
        )
        assert msg_id == 101
        ch.send_to_topic.assert_awaited_once()
        args, kwargs = ch.send_to_topic.call_args
        assert args[0] == 42
        assert args[1] == "Proceed?"
        assert "parse_mode" not in kwargs

        kbd = kwargs["reply_markup"]
        rows = kbd.inline_keyboard
        assert len(rows) == 3
        # v0.84.0 (round 4, D2): no agent-supplied ``short`` per option → the
        # WHOLE set floors to the numbered placeholder (the FULL option is
        # carried verbatim in the message body, not the button).
        assert [r[0].text for r in rows] == ["Option 1", "Option 2", "Option 3"]
        assert [r[0].callback_data for r in rows] == [
            "v1|engagement_ask|rid_ask|0",
            "v1|engagement_ask|rid_ask|1",
            "v1|engagement_ask|rid_ask|2",
        ]

    async def test_callback_data_fits_64_bytes_for_max_options(self) -> None:
        """A full-length uuid4().hex request_id (32 chars) with 8 options
        must stay within Telegram's 64-byte callback_data cap."""
        from channels import telegram as tg_mod
        import uuid

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch._engagement_registry = MagicMock()
        ch._engagement_registry.get = MagicMock(
            return_value=MagicMock(topic_id=42),
        )
        ch.send_to_topic = AsyncMock(return_value=1)

        rid = uuid.uuid4().hex
        options = [f"option-{i}" for i in range(8)]
        await ch.post_options_keyboard(
            engagement_id="x" * 32, request_id=rid,
            question="Q?", options=options,
        )
        kbd = ch.send_to_topic.call_args.kwargs["reply_markup"]
        for row in kbd.inline_keyboard:
            assert len(row[0].callback_data.encode("utf-8")) <= 64

    async def test_unknown_engagement_returns_none(self) -> None:
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch._engagement_registry = MagicMock()
        ch._engagement_registry.get = MagicMock(return_value=None)
        ch.send_to_topic = AsyncMock()

        msg_id = await ch.post_options_keyboard(
            engagement_id="z" * 32, request_id="r",
            question="Q?", options=["A", "B"],
        )
        assert msg_id is None
        ch.send_to_topic.assert_not_called()

    async def test_no_topic_id_returns_none(self) -> None:
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch._engagement_registry = MagicMock()
        ch._engagement_registry.get = MagicMock(
            return_value=MagicMock(topic_id=None),
        )
        ch.send_to_topic = AsyncMock()

        msg_id = await ch.post_options_keyboard(
            engagement_id="y" * 32, request_id="r",
            question="Q?", options=["A", "B"],
        )
        assert msg_id is None
        ch.send_to_topic.assert_not_called()


# ---------------------------------------------------------------------------
# TelegramChannel.edit_topic_message / delete_topic_message
# ---------------------------------------------------------------------------


class TestEditTopicMessage:
    async def test_edits_text_plain(self) -> None:
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch.engagement_supergroup_id = 555
        ch._bot = MagicMock()
        ch._bot.edit_message_text = AsyncMock()

        ok = await ch.edit_topic_message(42, 101, "Proceed?\n\nAnswered: Yes")
        assert ok is True
        ch._bot.edit_message_text.assert_awaited_once_with(
            chat_id=555, message_id=101, text="Proceed?\n\nAnswered: Yes",
        )

    async def test_not_modified_is_tolerated_as_success(self) -> None:
        from channels import telegram as tg_mod
        from telegram.error import BadRequest

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch.engagement_supergroup_id = 555
        bot = MagicMock()
        bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("Message is not modified"),
        )
        ch._bot = bot

        ok = await ch.edit_topic_message(42, 101, "same text")
        assert ok is True

    async def test_other_badrequest_returns_false(self) -> None:
        from channels import telegram as tg_mod
        from telegram.error import BadRequest

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch.engagement_supergroup_id = 555
        bot = MagicMock()
        bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("message to edit not found"),
        )
        ch._bot = bot

        ok = await ch.edit_topic_message(42, 101, "text")
        assert ok is False

    async def test_no_supergroup_configured_returns_false(self) -> None:
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch.engagement_supergroup_id = None
        ch._bot = MagicMock()
        ch._bot.edit_message_text = AsyncMock()

        ok = await ch.edit_topic_message(42, 101, "text")
        assert ok is False
        ch._bot.edit_message_text.assert_not_called()


class TestDeleteTopicMessage:
    async def test_deletes_message(self) -> None:
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch.engagement_supergroup_id = 555
        bot = MagicMock()
        bot.delete_message = AsyncMock(return_value=True)
        ch._bot = bot

        ok = await ch.delete_topic_message(42, 101)
        assert ok is True
        bot.delete_message.assert_awaited_once_with(chat_id=555, message_id=101)

    async def test_failure_returns_false(self) -> None:
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch.engagement_supergroup_id = 555
        bot = MagicMock()
        bot.delete_message = AsyncMock(side_effect=RuntimeError("gone"))
        ch._bot = bot

        ok = await ch.delete_topic_message(42, 101)
        assert ok is False

    async def test_no_supergroup_configured_returns_false(self) -> None:
        from channels import telegram as tg_mod

        ch = tg_mod.TelegramChannel.__new__(tg_mod.TelegramChannel)
        ch.engagement_supergroup_id = None
        ch._bot = MagicMock()
        ch._bot.delete_message = AsyncMock()

        ok = await ch.delete_topic_message(42, 101)
        assert ok is False
        ch._bot.delete_message.assert_not_called()
