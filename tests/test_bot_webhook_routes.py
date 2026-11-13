"""Boot and ingress with more than one Telegram bot.

* Each bot has its own update route, ``/telegram/update/{bot_id}``, with its
  own secret derived from the one configured secret: an update signed for
  one bot never enters another bot's route, and equal update ids on two
  routes are two updates.
* A turn with no parent is born with its Telegram route: /invoke for the
  invoked role, a plugin-setup turn on Casa's own bot.
* The boot replay of a delegation's notice carries the route stored on its
  row, not one derived again from the role.
* Casa's bot id is persisted when its endpoint verifies it, read back at the
  next boot before the bot reconnects, and not trusted for another token.
* A failed memory wipe idles casa-main before any channel exists.

The endpoints are the two-endpoint harness's fakes (``tests/two_endpoints.py``).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from channels.tg_address import CASA, RouteMissing
from two_endpoints import (CASA_BOT, CASA_TOKEN, FINANCE_BOT, OPERATOR,
                           FakeEndpoint, build_two_endpoints, calls,
                           update_payload)

pytestmark = [pytest.mark.unit]

ROOT = "root-webhook-secret"
HEADER = "X-Telegram-Bot-Api-Secret-Token"


def _secret(bot_id: int, root: str = ROOT) -> str:
    from channels.telegram_transport import webhook_secret_for
    return webhook_secret_for(root, bot_id)


def _updates(ep) -> list:
    return [c for c in ep.calls if c[0] == "update"]


async def _routes(transport, root: str = ROOT) -> TestClient:
    from casa_core import _make_telegram_update_handler, _telegram_update_endpoints
    handler = _make_telegram_update_handler(
        get_endpoint=_telegram_update_endpoints(transport), root_secret=root)
    app = web.Application()
    app.router.add_post("/telegram/update/{bot_id}", handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


# ---------------------------------------------------------------------------
# One update route per bot
# ---------------------------------------------------------------------------

async def test_another_bots_secret_is_refused_on_this_route():
    two = build_two_endpoints()
    client = await _routes(two.transport)
    try:
        resp = await client.post(
            f"/telegram/update/{CASA_BOT}", json=update_payload(1, 1, "hi"),
            headers={HEADER: _secret(FINANCE_BOT)})
        assert resp.status == 403
    finally:
        await client.close()
    assert _updates(two.casa) == [] and _updates(two.finance) == []


async def test_each_route_takes_its_own_bots_secret_to_that_bot_only():
    two = build_two_endpoints()
    client = await _routes(two.transport)
    try:
        resp = await client.post(
            f"/telegram/update/{FINANCE_BOT}", json=update_payload(1, 1, "hi"),
            headers={HEADER: _secret(FINANCE_BOT)})
        assert resp.status == 200
        assert resp.headers["X-Casa-Update"] == "accepted"
    finally:
        await client.close()
    assert len(_updates(two.finance)) == 1
    assert _updates(two.casa) == []


@pytest.mark.parametrize("segment", ["333", "abc", "0", "-111", "１１１"])
async def test_a_bot_with_no_endpoint_is_404(segment):
    two = build_two_endpoints()
    client = await _routes(two.transport)
    try:
        resp = await client.post(
            f"/telegram/update/{segment}", json=update_payload(1, 1, "hi"),
            headers={HEADER: _secret(CASA_BOT)})
        assert resp.status == 404
    finally:
        await client.close()
    assert _updates(two.casa) == [] and _updates(two.finance) == []


async def test_no_configured_secret_closes_every_route():
    two = build_two_endpoints()
    client = await _routes(two.transport, root="")
    try:
        for bot in (CASA_BOT, FINANCE_BOT):
            resp = await client.post(
                f"/telegram/update/{bot}", json=update_payload(1, 1, "hi"))
            assert resp.status == 403
            resp = await client.post(
                f"/telegram/update/{bot}", json=update_payload(1, 1, "hi"),
                headers={HEADER: ""})
            assert resp.status == 403
    finally:
        await client.close()
    assert _updates(two.casa) == [] and _updates(two.finance) == []


async def test_equal_update_ids_on_two_routes_are_two_updates():
    """Telegram numbers updates per bot, so update 71 on bot 111 and update
    71 on bot 222 are different updates: each reaches its own bot once."""
    two = build_two_endpoints()
    client = await _routes(two.transport)
    try:
        outcomes = []
        for bot in (CASA_BOT, FINANCE_BOT):
            resp = await client.post(
                f"/telegram/update/{bot}", json=update_payload(71, 5, "same"),
                headers={HEADER: _secret(bot)})
            assert resp.status == 200
            outcomes.append(resp.headers["X-Casa-Update"])
    finally:
        await client.close()
    assert outcomes == ["accepted", "accepted"]
    assert len(_updates(two.casa)) == 1 and len(_updates(two.finance)) == 1


def test_the_update_route_is_registered_per_bot():
    import inspect

    import casa_core
    src = inspect.getsource(casa_core.main)
    assert 'add_post("/telegram/update/{bot_id}"' in src
    assert 'add_post("/telegram/update",' not in src


# ---------------------------------------------------------------------------
# /invoke: born with the invoked role's route
# ---------------------------------------------------------------------------

_INVOKE_SECRET = "invoke-secret"


class _StubBus:
    def __init__(self):
        self.last_msg = None

    async def request(self, msg, timeout=300):
        self.last_msg = msg
        return SimpleNamespace(content="ok")


async def _invoke(route_for, role: str, context: dict | None = None):
    from casa_core import _make_invoke_handler
    from casa_core_middleware import cid_middleware
    from rate_limit import RateLimiter

    bus = _StubBus()
    cfg = SimpleNamespace(channels=["telegram", "webhook"])
    handler = _make_invoke_handler(
        webhook_rate_limiter=RateLimiter(capacity=0, window_s=60.0),
        webhook_secret=_INVOKE_SECRET, bus=bus, assistant_role="assistant",
        role_configs={"assistant": cfg, "finance": cfg}, route_for=route_for)
    app = web.Application(middlewares=[cid_middleware])
    app.router.add_post("/invoke/{agent}", handler)
    body = json.dumps({"prompt": "hi", "context": context or {}}).encode()
    sig = hmac.new(_INVOKE_SECRET.encode(), body, hashlib.sha256).hexdigest()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/invoke/{role}", data=body,
            headers={"Content-Type": "application/json",
                     "X-Webhook-Signature": sig})
        assert resp.status == 200, await resp.text()
    return bus.last_msg


@pytest.mark.parametrize("role,bot", [("assistant", CASA_BOT),
                                      ("finance", FINANCE_BOT)])
async def test_an_invoked_turn_is_born_with_its_roles_route(role, bot):
    """What an invoked turn sends to Telegram (``send_message`` passes the
    route of its origin) goes through the invoked role's bot, and nowhere
    else. Without the stamp the send raises ``RouteMissing``."""
    from provenance import route_context

    two = build_two_endpoints()
    msg = await _invoke(two.transport.route_for, role)
    assert msg.context["_tg_bot"] == bot
    await two.transport.send("hello", route_context(msg.context))
    other = FINANCE_BOT if bot == CASA_BOT else CASA_BOT
    assert [c[0] for c in calls(two.ep[bot])] == ["send"]
    assert calls(two.ep[other]) == []


async def test_a_caller_cannot_choose_an_invoked_turns_route():
    two = build_two_endpoints()
    msg = await _invoke(two.transport.route_for, "assistant",
                        context={"_tg_bot": FINANCE_BOT})
    assert msg.context["_tg_bot"] == CASA_BOT


async def test_an_unstamped_turn_has_no_route_to_deliver_through():
    """The other half of the mutation check above: a turn built without the
    stamp has no route, and delivery refuses rather than guess a bot."""
    from casa_core import build_invoke_message
    from provenance import route_context

    two = build_two_endpoints()
    msg = build_invoke_message("assistant", "hi", {})
    assert "_tg_bot" not in msg.context
    with pytest.raises(RouteMissing):
        await two.transport.send("hello", route_context(msg.context))
    assert calls(two.casa) == [] and calls(two.finance) == []


# ---------------------------------------------------------------------------
# Plugin setup: Casa's conversation, on Casa's bot
# ---------------------------------------------------------------------------

async def test_a_setup_turn_stays_on_casas_bot_after_its_role_is_bound_elsewhere():
    """A setup turn for ``finance`` is born on Casa's bot (111). When
    ``finance`` gets a bot of its own (222) afterwards, every reply and post
    of that turn still goes through 111, and nothing reaches 222."""
    from casa_core import _setup_turn_context
    from telegram_channel_double import make_transport

    casa = FakeEndpoint("assistant", bot_id=CASA_BOT, is_casa=True)
    manager = make_transport(casa)
    transport = manager.get("telegram")
    ctx = _setup_turn_context(transport, {"_plugin_setup": "lantern"})
    assert ctx["_tg_bot"] == CASA_BOT
    assert (ctx["chat_id"], ctx["user_id"]) == (OPERATOR, OPERATOR)
    assert ctx["_plugin_setup"] == "lantern"

    finance = FakeEndpoint("finance", bot_id=FINANCE_BOT)
    transport.add_endpoint(finance, casa=False)
    assert transport.route_for("finance") == FINANCE_BOT

    await transport.send("a", ctx)
    await transport.send_response("b", ctx)
    transport.create_on_token(ctx)
    await transport.send_media(b"x", "document", "f.txt", ctx)
    await transport.turn_finished(ctx)
    assert [c[0] for c in calls(casa)] == [
        "send", "send_response", "create_on_token", "send_media",
        "turn_finished"]
    assert calls(finance) == []


def test_a_setup_turn_for_a_role_with_its_own_bot_is_still_born_on_casas():
    from casa_core import _setup_turn_context

    two = build_two_endpoints()
    assert two.transport.route_for("finance") == FINANCE_BOT
    ctx = _setup_turn_context(two.transport, {})
    assert ctx["_tg_bot"] == CASA_BOT


def test_a_setup_turn_before_casas_id_is_known_is_born_on_casas_endpoint():
    from casa_core import _setup_turn_context
    from telegram_channel_double import make_transport

    casa = FakeEndpoint("assistant", bot_id=None, is_casa=True, ready=False)
    transport = make_transport(casa).get("telegram")
    ctx = _setup_turn_context(transport, {})
    assert ctx["_tg_bot"] == CASA
    assert transport.endpoint_for_route(ctx, site="test") is casa


def test_no_operator_chat_dispatches_no_setup_turn():
    from casa_core import _setup_turn_context
    from telegram_channel_double import make_transport

    casa = FakeEndpoint("assistant", bot_id=CASA_BOT, is_casa=True,
                        operator=-100123)
    assert _setup_turn_context(make_transport(casa).get("telegram"), {}) is None
    assert _setup_turn_context(None, {}) is None


# ---------------------------------------------------------------------------
# The boot replay keeps the route stored on the row
# ---------------------------------------------------------------------------

def _owed_row(route: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id="d" * 32, creator_peer="telegram", orphan_notification_pending=True,
        terminal_notification_pending=False, failure=None,
        creating_role="finance", specialist_role="reporter",
        result_available=True, result="the answer",
        closing_message="kind regards", terminal_at=10.0,
        origin_route_id="cid-1", scope_id=str(OPERATOR), task="report",
        scheduled_delivery=None, output_note="", route=route)


def _origin_from_row(job) -> dict:
    """A stand-in for ``specialist_registry.origin_from_job``: the row's
    origin with the route stored on it."""
    return {"role": job.creating_role, "channel": job.creator_peer,
            "chat_id": job.scope_id, "cid": job.origin_route_id or "-",
            "user_text": job.task, **job.route}


async def test_the_boot_replay_carries_the_route_stored_on_the_row(monkeypatch):
    """The delegation was born on Casa's bot (111). Its role is bound to
    another bot (222) now; the replayed notice still carries 111, on its
    origin and its delivery context, and the closing message survives."""
    import casa_core
    import specialist_registry

    monkeypatch.setattr(specialist_registry, "origin_from_job",
                        _origin_from_row, raising=False)
    two = build_two_endpoints()
    assert two.transport.route_for("finance") == FINANCE_BOT

    class _Bus:
        queues = {"finance": object(), "assistant": object()}

        def __init__(self):
            self.sent = []

        async def notify(self, message):
            self.sent.append(message)

    bus = _Bus()
    await casa_core._notify_recovered_delegations(
        [_owed_row({"_tg_bot": CASA_BOT})], SimpleNamespace(), bus,
        assistant_role="assistant")
    (msg,) = bus.sent
    assert msg.context["_tg_bot"] == CASA_BOT
    assert msg.content.origin["_tg_bot"] == CASA_BOT
    assert msg.content.closing_message == "kind regards"
    assert two.transport.endpoint_for_route(msg.context, site="t") is two.casa


async def test_a_row_born_before_casas_id_was_known_replays_to_casas_endpoint(
        monkeypatch):
    import casa_core
    import specialist_registry

    monkeypatch.setattr(specialist_registry, "origin_from_job",
                        _origin_from_row, raising=False)
    two = build_two_endpoints()

    class _Bus:
        queues = {"finance": object()}
        sent: list = []

        async def notify(self, message):
            self.sent.append(message)

    bus = _Bus()
    await casa_core._notify_recovered_delegations(
        [_owed_row({"_tg_bot": CASA})], SimpleNamespace(), bus,
        assistant_role="assistant")
    (msg,) = bus.sent
    assert msg.context["_tg_bot"] == CASA
    assert two.transport.endpoint_for_route(msg.context, site="t") is two.casa


# ---------------------------------------------------------------------------
# Casa's bot id across boots
# ---------------------------------------------------------------------------

def _boot(path, token: str = CASA_TOKEN):
    """Build Casa's transport the way ``main`` does, with a fake endpoint
    standing in for ``TelegramChannel``. Returns (transport, endpoint, the
    keyword arguments the endpoint was built with)."""
    from casa_core import _build_telegram_transport

    built: list[dict] = []

    def make_endpoint(**kw):
        built.append(kw)
        return FakeEndpoint(kw["default_agent"], bot_id=kw["known_bot_id"],
                            is_casa=kw["is_casa"], ready=False)

    transport, ep = _build_telegram_transport(
        token=token, store_path=path, make_endpoint=make_endpoint,
        default_agent="assistant", chat_id=str(OPERATOR))
    return transport, ep, built[0]


async def _invoked_route(transport) -> object:
    msg = await _invoke(transport.route_for, "assistant")
    return msg.context["_tg_bot"]


async def test_casas_bot_id_is_persisted_and_read_back_before_the_next_start(
        tmp_path):
    from channels.telegram_transport import token_digest

    path = tmp_path / "telegram-casa-bot.json"
    # First boot: nothing stored. A turn born now is routed to Casa's
    # endpoint, whatever id it verifies.
    transport, ep, built = _boot(path)
    assert built["known_bot_id"] is None and built["is_casa"] is True
    assert transport.casa_route() == CASA
    assert await _invoked_route(transport) == CASA
    assert not path.exists()
    # Its getMe succeeds: the id is mapped and persisted, through the
    # endpoint's identity callback.
    assert ep.initialize(CASA_BOT) is True
    assert json.loads(path.read_text()) == {
        "bot_id": CASA_BOT, "token_digest": token_digest(CASA_TOKEN)}
    assert transport.casa_route() == CASA_BOT
    # Second boot, same token: the id is known before the endpoint starts,
    # and the first /invoke carries it.
    transport2, ep2, built2 = _boot(path)
    assert built2["known_bot_id"] == CASA_BOT
    assert not ep2.is_ready
    assert await _invoked_route(transport2) == CASA_BOT
    assert transport2.endpoint(CASA_BOT) is ep2


async def test_a_replaced_token_is_routed_to_casas_endpoint_until_verified(
        tmp_path):
    from channels.telegram_transport import token_digest

    path = tmp_path / "telegram-casa-bot.json"
    _, ep, _ = _boot(path)
    ep.initialize(CASA_BOT)
    # The token was replaced: it may belong to another bot, so the stored
    # id is not trusted.
    transport, ep2, built = _boot(path, token="999:another-token")
    assert built["known_bot_id"] is None
    assert await _invoked_route(transport) == CASA
    ep2.initialize(999)
    assert await _invoked_route(transport) == 999
    assert json.loads(path.read_text()) == {
        "bot_id": 999, "token_digest": token_digest("999:another-token")}


def test_casas_bot_id_file_is_declared_private():
    import private_state
    (entry,) = [e for e in private_state.FILES
                if e.path == "/data/telegram-casa-bot.json"]
    assert (entry.mode, entry.kind) == (0o600, private_state.PRIVATE)


def test_main_reads_casas_bot_id_before_the_http_server_starts():
    """``main`` builds the transport (which reads the stored id) and
    registers it before the HTTP server starts, so no request is served
    before the route of a newly born turn can be decided."""
    import inspect

    import casa_core
    src = inspect.getsource(casa_core.main)
    built = src.index("_build_telegram_transport(")
    registered = src.index("channel_manager.register(tg_transport)")
    serving = src.index("await site.start()")
    assert built < registered < serving
    assert src.count('Path("/data/telegram-casa-bot.json")') == 1


# ---------------------------------------------------------------------------
# A failed memory wipe idles before any channel exists
# ---------------------------------------------------------------------------

_SPY = """
import os as _os, channels as _ch
import channels.telegram_transport as _tt
_os.environ["TELEGRAM_BOT_TOKEN"] = "111:spy-token"
_os.environ["TELEGRAM_CHAT_ID"] = "100"
def _spy(cls, label):
    init = cls.__init__
    def wrapped(self, *a, **k):
        with open({spy!r}, "a") as fh:
            fh.write(label + "\\n")
        init(self, *a, **k)
    cls.__init__ = wrapped
_spy(_ch.ChannelManager, "ChannelManager")
_spy(_tt.TelegramTransport, "TelegramTransport")
"""


def test_a_failed_wipe_boot_sends_its_line_once_and_builds_no_channel(tmp_path):
    """With a recorded wipe attempt casa-main sends the operator one line
    through the raw sender and idles; it constructs no channel manager and
    no Telegram transport, even with Telegram configured."""
    from test_memory_pending_intent import _attempted, run_main

    spy = tmp_path / "constructed.txt"
    result, lines = run_main(tmp_path, _attempted(), cid="c2",
                             prelude=_SPY.format(spy=str(spy)))
    assert result["sends"] == 1 and len(lines) == 1
    assert not spy.exists(), spy.read_text()
