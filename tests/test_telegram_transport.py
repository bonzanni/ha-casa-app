"""The Telegram transport: endpoints by bot id, the three resolution rules,
routes born before Casa's bot knows its id, and the persisted Casa bot id.

What these pin: a delivery goes through exactly the endpoint its route or
address names, never through a guessed one; an unready endpoint behaves as it
does alone (the transport adds no outcome of its own); and Casa's route is its
integer id whenever that id is known, else ``CASA``."""

from __future__ import annotations

import asyncio
import json
import os
import re
from types import SimpleNamespace

import pytest

from channels import DeliveryOutcome
from channels.tg_address import (
    CASA,
    AddressError,
    DuplicateEndpoint,
    EndpointNotReady,
    EndpointUnavailable,
    RouteMissing,
    TgAddress,
)
from channels.telegram_transport import (
    CasaBotIdStore,
    TelegramTransport,
    telegram_transport,
    token_digest,
    webhook_secret_for,
)
from telegram_channel_double import MemoryBotIdStore, make_transport
from two_endpoints import (  # noqa: F401 — the fixture is used by name
    CASA_TOKEN,
    FakeEndpoint,
    calls,
    feed_update,
    two_endpoints,
)

import channels.telegram_transport as transport_mod


def _transport(*eps, casa=None, store=None):
    t = TelegramTransport(casa_bot_ids=store if store is not None else MemoryBotIdStore())
    casa = eps[0] if casa is None else casa
    t.add_endpoint(casa, casa=True)
    for ep in eps:
        if ep is not casa:
            t.add_endpoint(ep, casa=False)
    return t


# Every route-bound method, called with *ctx* as its delivery context.
ROUTE_BOUND = {
    "send": lambda t, ctx: t.send("hi", ctx),
    "send_media": lambda t, ctx: t.send_media(b"x", "document", "f.txt", ctx),
    "send_response": lambda t, ctx: t.send_response("hi", ctx),
    "finalize_response_stream":
        lambda t, ctx: t.finalize_response_stream("hi", ctx, None),
    "turn_finished": lambda t, ctx: t.turn_finished(ctx),
    "create_on_token": lambda t, ctx: t.create_on_token(ctx),
    "finalize_stream": lambda t, ctx: t.finalize_stream("hi", ctx, None),
}


async def _call(method, t, ctx):
    out = ROUTE_BOUND[method](t, ctx)
    if asyncio.iscoroutine(out):
        out = await out
    return out


# ---------------------------------------------------------------------------
# Rule A: endpoint by bot id or address
# ---------------------------------------------------------------------------


def test_endpoint_by_id_and_by_address(two_endpoints):
    h = two_endpoints
    assert h.transport.endpoint(222) is h.finance
    assert h.transport.endpoint(111) is h.casa
    assert h.transport.endpoint(TgAddress(222, 100, 5)) is h.finance
    with pytest.raises(EndpointUnavailable) as exc:
        h.transport.endpoint(333)
    assert exc.value.bot_id == 333
    with pytest.raises(EndpointUnavailable):
        h.transport.endpoint(TgAddress(333, 100, 5))


@pytest.mark.parametrize("bot", [CASA, None, True, "222"])
def test_endpoint_refuses_a_value_that_is_not_a_bot_id(two_endpoints, bot):
    with pytest.raises(AddressError):
        two_endpoints.transport.endpoint(bot)


async def test_a_known_endpoint_that_is_not_ready_is_still_the_one_resolved():
    casa = FakeEndpoint("assistant", bot_id=111, is_casa=True)
    finance = FakeEndpoint("finance", bot_id=222, ready=False)
    t = _transport(casa, finance)
    assert t.endpoint(222) is finance
    out = await t.send("hi", {"_tg_bot": 222})
    assert out is DeliveryOutcome.NOT_DELIVERED
    assert [c[0] for c in calls(finance)] == ["send"]
    assert calls(casa) == []


# ---------------------------------------------------------------------------
# Rule B: route-bound delivery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", sorted(ROUTE_BOUND))
@pytest.mark.parametrize("ctx", [{}, {"cid": "x"}, {"chat_id": 100}])
async def test_route_bound_methods_raise_route_missing_without_a_route(
        two_endpoints, method, ctx):
    h = two_endpoints
    with pytest.raises(RouteMissing) as exc:
        await _call(method, h.transport, dict(ctx))
    assert exc.value.site == method
    assert calls(h.casa) == [] and calls(h.finance) == []


@pytest.mark.parametrize("method", sorted(ROUTE_BOUND))
@pytest.mark.parametrize("route", [None, "x", True, 0, "111"])
async def test_route_bound_methods_refuse_a_malformed_route(
        two_endpoints, method, route):
    h = two_endpoints
    with pytest.raises(AddressError):
        await _call(method, h.transport, {"_tg_bot": route})
    assert calls(h.casa) == [] and calls(h.finance) == []


def test_route_resolution(two_endpoints):
    t = two_endpoints.transport
    assert t.endpoint_for_route({"_tg_bot": CASA}, site="t") is two_endpoints.casa
    assert t.endpoint_for_route({"_tg_bot": 222}, site="t") is two_endpoints.finance
    assert t.endpoint_for_route({"_tg_bot": 111}, site="t") is two_endpoints.casa
    with pytest.raises(EndpointUnavailable):
        t.endpoint_for_route({"_tg_bot": 333}, site="t")
    with pytest.raises(RouteMissing):
        t.endpoint_for_route(None, site="t")


@pytest.mark.parametrize("method", sorted(ROUTE_BOUND))
async def test_each_route_bound_method_reaches_only_its_route(two_endpoints, method):
    h = two_endpoints
    await _call(method, h.transport, {"_tg_bot": 222})
    assert [c[0] for c in calls(h.finance)] == [method]
    assert calls(h.casa) == []


async def test_send_response_on_222_calls_only_222(two_endpoints):
    h = two_endpoints
    ctx = {"_tg_bot": 222, "chat_id": 100, "cid": "c1"}
    assert await h.transport.send_response("hello", ctx) is DeliveryOutcome.DELIVERED
    assert calls(h.finance) == [("send_response", ("hello", ctx))]
    assert calls(h.casa) == []


# ---------------------------------------------------------------------------
# Pass-through: an unready endpoint behaves as it does alone
# ---------------------------------------------------------------------------


class _SpyEndpoint(FakeEndpoint):
    """Returns sentinels so a test can tell the endpoint's own result from
    anything the transport could have made up."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sentinel = {name: object() for name in (
            "send", "send_response", "finalize_response_stream",
            "finalize_stream", "turn_finished")}

    async def send(self, message, context):
        await super().send(message, context)
        return self.sentinel["send"]

    async def send_response(self, message, context):
        await super().send_response(message, context)
        return self.sentinel["send_response"]

    async def finalize_response_stream(self, full_text, context, on_token):
        await super().finalize_response_stream(full_text, context, on_token)
        return self.sentinel["finalize_response_stream"]

    async def finalize_stream(self, full_text, context, on_token):
        await super().finalize_stream(full_text, context, on_token)
        return self.sentinel["finalize_stream"]

    async def turn_finished(self, context):
        await super().turn_finished(context)
        return self.sentinel["turn_finished"]


@pytest.mark.parametrize("route", [222, CASA])
async def test_unready_endpoint_results_pass_through_unchanged(route):
    casa = FakeEndpoint("assistant", bot_id=None, is_casa=True, ready=False)
    finance = FakeEndpoint("finance", bot_id=222, ready=False)
    target = finance if route == 222 else casa
    t = _transport(casa, finance)
    ctx = {"_tg_bot": route}

    # send: the endpoint's own NOT_DELIVERED, nothing more
    assert await t.send("hi", ctx) is DeliveryOutcome.NOT_DELIVERED

    # send_media: the endpoint's very exception object
    with pytest.raises(RuntimeError) as exc:
        await t.send_media(b"x", "document", "f.txt", ctx)
    assert exc.value is target.media_unavailable

    # create_on_token: the endpoint's callable, the same object
    on_token = t.create_on_token(ctx)
    assert on_token is target.last_on_token
    await on_token("partial")

    # teardown is harmless
    assert await t.turn_finished(ctx) is None
    assert {c[0] for c in calls(target)} == {
        "send", "send_media", "create_on_token", "on_token", "turn_finished"}
    other = casa if target is finance else finance
    assert calls(other) == []


@pytest.mark.parametrize("method", [
    "send", "send_response", "finalize_response_stream", "finalize_stream",
    "turn_finished"])
async def test_unready_endpoint_return_value_is_the_transports(method):
    casa = FakeEndpoint("assistant", bot_id=111, is_casa=True)
    spy = _SpyEndpoint("finance", bot_id=222, ready=False)
    t = _transport(casa, spy)
    assert await _call(method, t, {"_tg_bot": 222}) is spy.sentinel[method]


# ---------------------------------------------------------------------------
# Routes for turns born with no parent
# ---------------------------------------------------------------------------


def test_route_for(two_endpoints):
    t = two_endpoints.transport
    assert t.route_for("finance") == 222
    assert t.route_for("butler") == 111       # no endpoint of its own: Casa's
    assert t.route_for("assistant") == 111
    assert t.casa_route() == 111


def test_routes_use_the_persisted_casa_id_before_the_endpoint_is_ready():
    casa = FakeEndpoint("assistant", bot_id=111, is_casa=True, ready=False)
    t = _transport(casa, store=MemoryBotIdStore(111))
    assert not casa.is_ready
    assert t.casa_route() == 111
    assert t.route_for("butler") == 111
    assert t.endpoint(111) is casa


# ---------------------------------------------------------------------------
# Identity observation
# ---------------------------------------------------------------------------


def test_identity_observation(tmp_path, monkeypatch):
    writes = []
    real_write = transport_mod.atomic_write_json

    def counting_write(path, data, **kw):
        writes.append((path, data, kw))
        return real_write(path, data, **kw)

    monkeypatch.setattr(transport_mod, "atomic_write_json", counting_write)
    path = tmp_path / "telegram-casa-bot.json"
    store = CasaBotIdStore(path, CASA_TOKEN)
    casa = FakeEndpoint("assistant", bot_id=None, is_casa=True, ready=False)
    finance = FakeEndpoint("finance", bot_id=222)
    t = _transport(casa, finance, store=store)

    # 1. no id yet: the route is CASA, for Casa's role and for a role
    #    without an endpoint alike
    assert t.casa_route() == CASA
    assert t.route_for("butler") == CASA
    assert t.route_for("assistant") == CASA
    assert t.route_for("finance") == 222

    # 2. getMe verifies 111: mapped and persisted before the endpoint is ready
    assert casa.initialize(111) is True
    assert casa.identity_seen_ready == [False]
    assert t.endpoint(111) is casa
    assert t.casa_route() == 111
    assert t.route_for("butler") == 111
    assert json.loads(path.read_text()) == {
        "bot_id": 111, "token_digest": token_digest(CASA_TOKEN)}
    assert len(writes) == 1

    # 3. the same id again writes nothing
    t.observe_endpoint_identity(casa, 111)
    casa.initialize(111)
    assert len(writes) == 1

    # 4. another endpoint whose getMe says 111 is refused; nothing changes
    with pytest.raises(DuplicateEndpoint) as exc:
        t.observe_endpoint_identity(finance, 111)
    assert exc.value.bot_id == 111
    assert t.endpoint(111) is casa
    assert t.endpoint(222) is finance
    assert len(writes) == 1


def test_a_duplicate_identity_leaves_the_endpoint_stopped_and_unready():
    casa = FakeEndpoint("assistant", bot_id=111, is_casa=True)
    finance = FakeEndpoint("finance", bot_id=222, ready=False)
    t = _transport(casa, finance)
    assert finance.initialize(111) is False
    assert finance.stopped and not finance.is_ready
    assert t.endpoint(111) is casa and t.endpoint(222) is finance


def test_an_endpoint_whose_bot_changed_is_remapped():
    casa = FakeEndpoint("assistant", bot_id=111, is_casa=True)
    finance = FakeEndpoint("finance", bot_id=222, ready=False)
    t = _transport(casa, finance)
    finance.initialize(333)
    assert t.endpoint(333) is finance
    with pytest.raises(EndpointUnavailable):
        t.endpoint(222)


def test_only_casa_identity_is_persisted():
    store = MemoryBotIdStore(111)
    casa = FakeEndpoint("assistant", bot_id=111, is_casa=True)
    finance = FakeEndpoint("finance", bot_id=222, ready=False)
    t = _transport(casa, finance, store=store)
    finance.initialize(222)
    assert store.writes == []
    assert t.casa_route() == 111


def test_add_endpoint_installs_on_identity():
    casa = FakeEndpoint("assistant", bot_id=None, is_casa=True, ready=False)
    t = _transport(casa)
    casa.on_identity(111)
    assert t.endpoint(111) is casa


# ---------------------------------------------------------------------------
# Registration (one endpoint per role, one per bot)
# ---------------------------------------------------------------------------


def test_add_endpoint_refuses_a_non_casa_endpoint_without_an_id():
    t = _transport(FakeEndpoint("assistant", bot_id=111, is_casa=True))
    with pytest.raises(ValueError):
        t.add_endpoint(FakeEndpoint("finance", bot_id=None, ready=False), casa=False)
    assert t.route_for("finance") == 111


def test_one_endpoint_per_role_and_per_bot(two_endpoints):
    t = two_endpoints.transport
    with pytest.raises(ValueError):
        t.add_endpoint(FakeEndpoint("finance", bot_id=333), casa=False)
    with pytest.raises(DuplicateEndpoint):
        t.add_endpoint(FakeEndpoint("butler", bot_id=111), casa=False)
    with pytest.raises(ValueError):
        t.add_endpoint(FakeEndpoint("butler", bot_id=444, is_casa=True), casa=True)
    assert t.route_for("finance") == 222
    assert t.route_for("butler") == 111
    with pytest.raises(EndpointUnavailable):
        t.endpoint(333)


# ---------------------------------------------------------------------------
# Rule C and the operator
# ---------------------------------------------------------------------------


def test_casa_endpoint_and_operator(two_endpoints):
    t = two_endpoints.transport
    assert t.casa_endpoint() is two_endpoints.casa
    assert t.operator_user_id() == 100
    assert t.casa_operator_address() == TgAddress(111, 100)


def test_casa_operator_address_before_any_id_is_not_ready():
    casa = FakeEndpoint("assistant", bot_id=None, is_casa=True, ready=False)
    t = _transport(casa)
    assert t.casa_endpoint() is casa
    assert t.operator_user_id() == 100
    with pytest.raises(EndpointNotReady):
        t.casa_operator_address()


@pytest.mark.parametrize("configured", ["", "-1001", "abc", None])
def test_no_operator_without_a_private_chat(configured):
    casa = FakeEndpoint("assistant", bot_id=111, is_casa=True)
    casa.chat_id = configured
    t = _transport(casa)
    assert t.operator_user_id() is None
    with pytest.raises(AddressError):
        t.casa_operator_address()


# ---------------------------------------------------------------------------
# Job records
# ---------------------------------------------------------------------------


class _Registry:
    def __init__(self, *records):
        self.records = list(records)

    def take_ui_posts(self, eid):
        rec = next(r for r in self.records if r.id == eid)
        posts, rec.ui_posts = rec.ui_posts, []
        return posts

    def records_with_ui_posts(self):
        return [r for r in self.records if r.ui_posts]


def _job(eid="e1", route=111, posts=()):
    origin = {"chat_id": 100} if route is None else {"chat_id": 100, "_tg_bot": route}
    return SimpleNamespace(id=eid, origin=origin, ui_posts=list(posts))


@pytest.mark.parametrize("method,args", [
    ("post_job_start_line", ()),
    ("post_job_parked", ("parked",)),
    ("post_job_question", ("q1", "body", ["a"], ["a"])),
])
async def test_a_new_job_post_goes_through_the_jobs_route(two_endpoints, method, args):
    h = two_endpoints
    rec = _job(route=222)
    mid = await getattr(h.transport, method)(rec, *args)
    assert isinstance(mid, int)
    assert [c[0] for c in calls(h.finance)] == [method]
    assert calls(h.casa) == []
    with pytest.raises(RouteMissing):
        await getattr(h.transport, method)(_job(route=None), *args)


async def test_a_job_route_of_casa_posts_through_casa(two_endpoints):
    h = two_endpoints
    await h.transport.post_job_start_line(_job(route=CASA))
    assert [c[0] for c in calls(h.casa)] == ["post_job_start_line"]
    assert calls(h.finance) == []


async def test_a_start_line_edit_goes_through_the_bot_that_posted_it(two_endpoints):
    h = two_endpoints
    # the job's route says 111, but its start line was posted by 222
    rec = _job(route=111, posts=[(TgAddress(222, 100, 7), "start"),
                                 (TgAddress(111, 100, 8), "question")])
    await h.transport.refresh_job_start_line(rec)
    assert [c[0] for c in calls(h.finance)] == ["refresh_job_start_line"]
    assert calls(h.casa) == []
    # the stored row form names the same post
    rec2 = _job(route=111, posts=[[222, 100, 7, "start"]])
    await h.transport.refresh_job_start_line(rec2)
    assert [c[0] for c in calls(h.finance)] == ["refresh_job_start_line"] * 2
    # no start line filed: nothing to edit
    await h.transport.refresh_job_start_line(_job(posts=[(TgAddress(111, 100, 8), "question")]))
    assert calls(h.casa) == []


async def test_retire_job_keyboards_is_synchronous_and_per_post_endpoint(two_endpoints):
    h = two_endpoints
    p1, p2, p3 = ((TgAddress(111, 100, 5), "start"),
                  (TgAddress(222, 100, 5), "question"),
                  (TgAddress(111, 100, 6), "parked"))
    rec = _job(posts=[p1, p2, p3])
    h.casa._engagement_registry = _Registry(rec)

    result = h.transport.retire_job_keyboards(rec)
    assert result is None                       # a plain call, nothing to await
    assert rec.ui_posts == []                   # taken before it returned
    assert calls(h.casa) == [] and calls(h.finance) == []   # edits are scheduled
    await asyncio.gather(*list(h.transport._job_ui_tasks))
    assert calls(h.casa) == [("retire_job_posts", (rec, [p1, p3]))]
    assert calls(h.finance) == [("retire_job_posts", (rec, [p2]))]

    # taken once: a second call retires nothing
    h.transport.retire_job_keyboards(rec)
    assert not h.transport._job_ui_tasks


async def test_retire_job_keyboards_never_substitutes_an_endpoint(two_endpoints):
    h = two_endpoints
    gone, here = (TgAddress(333, 100, 5), "question"), (TgAddress(222, 100, 6), "start")
    rec = _job(posts=[gone, here])
    h.casa._engagement_registry = _Registry(rec)
    with pytest.raises(EndpointUnavailable) as exc:
        h.transport.retire_job_keyboards(rec)
    assert exc.value.bot_id == 333
    await asyncio.gather(*list(h.transport._job_ui_tasks))
    assert calls(h.finance) == [("retire_job_posts", (rec, [here]))]
    assert calls(h.casa) == []


async def test_retire_ended_job_keyboards_at_boot(two_endpoints):
    h = two_endpoints
    a = _job("a", posts=[(TgAddress(111, 100, 1), "start")])
    b = _job("b", posts=[(TgAddress(333, 100, 2), "start")])
    c = _job("c", posts=[(TgAddress(222, 100, 3), "question")])
    h.casa._engagement_registry = _Registry(a, b, c)
    assert h.transport.retire_ended_job_keyboards() == 3
    await asyncio.gather(*list(h.transport._job_ui_tasks))
    assert [c_[0] for c_ in calls(h.casa)] == ["retire_job_posts"]
    assert [c_[0] for c_ in calls(h.finance)] == ["retire_job_posts"]


def test_retire_without_a_registry_does_nothing(two_endpoints):
    assert two_endpoints.transport.retire_job_keyboards(_job()) is None
    assert two_endpoints.transport.retire_ended_job_keyboards() == 0


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_lifecycle_reaches_every_endpoint(two_endpoints):
    h = two_endpoints
    t = h.transport
    await t.start()
    assert await t.pause_updates_for_restart() is True
    await t.resume_updates()
    await t.stop()
    for ep in (h.casa, h.finance):
        assert [c[0] for c in calls(ep)] == [
            "start", "pause_updates_for_restart", "resume_updates", "stop"]


async def test_another_bot_failing_to_start_never_blocks_casa():
    casa = FakeEndpoint("assistant", bot_id=111, is_casa=True)
    finance = FakeEndpoint("finance", bot_id=222)

    async def boom():
        raise RuntimeError("bad token")

    finance.start = boom
    t = _transport(finance, casa, casa=casa)
    await t.start()
    assert [c[0] for c in calls(casa)] == ["start"]


async def test_pause_reports_whether_any_endpoint_paused(two_endpoints):
    h = two_endpoints

    async def not_paused():
        return False

    h.casa.pause_updates_for_restart = not_paused
    assert await h.transport.pause_updates_for_restart() is True
    h.finance.pause_updates_for_restart = not_paused
    assert await h.transport.pause_updates_for_restart() is False


def test_ready_event_is_casas(two_endpoints):
    assert two_endpoints.transport.ready_event is two_endpoints.casa.ready_event


def test_registered_as_the_one_telegram_channel(two_endpoints, monkeypatch):
    h = two_endpoints
    assert h.manager.get("telegram") is h.transport
    assert h.transport.name == "telegram"
    assert telegram_transport(h.manager) is h.transport
    import tools
    monkeypatch.setattr(tools, "_channel_manager", h.manager)
    assert telegram_transport() is h.transport
    monkeypatch.setattr(tools, "_channel_manager", None)
    assert telegram_transport() is None


def test_make_transport_registers_casa_first():
    casa = FakeEndpoint("assistant", bot_id=111, is_casa=True)
    finance = FakeEndpoint("finance", bot_id=222)
    manager = make_transport(finance, casa, casa=casa)
    t = telegram_transport(manager)
    assert t.casa_endpoint() is casa
    assert t.route_for("finance") == 222


# ---------------------------------------------------------------------------
# The harness itself
# ---------------------------------------------------------------------------


async def test_equal_update_ids_on_two_endpoints_are_both_taken(two_endpoints):
    h = two_endpoints
    assert await feed_update(h.casa, 71, 5, "hi") == "accepted"
    assert await feed_update(h.finance, 71, 5, "hi") == "accepted"
    assert await feed_update(h.casa, 71, 5, "hi") == "duplicate"


# ---------------------------------------------------------------------------
# The persisted Casa bot id
# ---------------------------------------------------------------------------


def test_casa_bot_id_store_round_trip(tmp_path):
    path = tmp_path / "telegram-casa-bot.json"
    assert CasaBotIdStore(path, CASA_TOKEN).known_id() is None
    CasaBotIdStore(path, CASA_TOKEN).observe(111)
    assert CasaBotIdStore(path, CASA_TOKEN).known_id() == 111
    assert os.stat(path).st_mode & 0o777 == 0o600
    # a replaced token does not trust the stored id
    assert CasaBotIdStore(path, "111:another-token").known_id() is None


def test_casa_bot_id_store_writes_only_on_change(tmp_path, monkeypatch):
    writes = []
    real_write = transport_mod.atomic_write_json
    monkeypatch.setattr(transport_mod, "atomic_write_json",
                        lambda p, d, **kw: (writes.append(d), real_write(p, d, **kw)))
    path = tmp_path / "telegram-casa-bot.json"
    store = CasaBotIdStore(path, CASA_TOKEN)
    store.observe(111)
    store.observe(111)
    assert len(writes) == 1
    store.observe(112)
    assert len(writes) == 2
    # a fresh store over the same file and token: nothing to write
    CasaBotIdStore(path, CASA_TOKEN).observe(112)
    assert len(writes) == 2
    # the same bot under a new token: rewritten with the new digest
    rotated = CasaBotIdStore(path, "112:new-token")
    assert rotated.known_id() is None
    rotated.observe(112)
    assert len(writes) == 3
    assert rotated.known_id() == 112
    assert CasaBotIdStore(path, "112:new-token").known_id() == 112


@pytest.mark.parametrize("content", [
    "not json", "[]", '{"bot_id": "111", "token_digest": "x"}',
    '{"bot_id": 111}', '{"bot_id": true, "token_digest": "x"}',
    '{"bot_id": "casa", "token_digest": "x"}'])
def test_casa_bot_id_store_ignores_a_malformed_file(tmp_path, content):
    path = tmp_path / "telegram-casa-bot.json"
    path.write_text(content)
    assert CasaBotIdStore(path, CASA_TOKEN).known_id() is None


def test_casa_bot_id_store_write_failure_is_logged_not_raised(tmp_path, caplog):
    store = CasaBotIdStore(tmp_path / "missing-dir" / "f.json", CASA_TOKEN)
    store.observe(111)
    assert store.known_id() is None
    assert "not persisted" in caplog.text


def test_token_digest():
    d = token_digest(CASA_TOKEN)
    assert re.fullmatch(r"[0-9a-f]{16}", d)
    assert d == token_digest(CASA_TOKEN)
    assert d != token_digest("111:other")


def test_webhook_secret_for():
    s111 = webhook_secret_for("root", 111)
    assert s111 == webhook_secret_for("root", 111)
    assert re.fullmatch(r"[0-9a-f]{64}", s111)
    assert s111 != webhook_secret_for("root", 222)
    assert s111 != webhook_secret_for("other-root", 111)
    assert webhook_secret_for("", 111) == ""
