"""The tools deliver through the bot of the turn's route, decided once.

With more than one Telegram bot, a tool that sends to the operator must not
pick a bot: it uses the route the turn was born with (``_tg_bot``), carried in
the origin. These tests drive the real tool handlers over the real transport
with two bots — 111, Casa's own (role ``assistant``), and 222 (role
``finance``) — and count what each bot was asked to send:

* ``send_message`` from a finance turn goes through 222 only;
* a turn born with no parent (voice, ``/invoke``, a webhook) on the one bot
  production runs delivers once and never fails for want of a route;
* a scheduled job's route is decided when it is launched and kept: a later
  binding of its role to another bot does not move its question or its posts;
* both completion envelopes carry the route back to the turn that answers;
* before Casa's bot has any id (a first boot, a replaced token) a turn's
  route is ``CASA``: a file sent then reports the channel unavailable and
  stays owed, and a scheduled question is refused before anything is
  registered or stored, exactly as when its post fails.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import agent
import scheduled_asks
import tools
import verdict_broker
from channels.tg_address import CASA, EndpointNotReady
from output_boundary_testing import with_scope
from telegram_channel_double import MemoryBotIdStore, make_transport
from two_endpoints import (
    CASA_BOT, FINANCE_BOT, OPERATOR, FakeEndpoint, build_two_endpoints, calls,
)
from verdict_broker import VerdictBroker

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _init(manager) -> None:
    tools.init_tools(channel_manager=manager, bus=MagicMock(),
                     specialist_registry=MagicMock(), mcp_registry=MagicMock())


async def _call(tool, origin: dict, args: dict):
    token = agent.origin_var.set(origin)
    try:
        return await tool.handler(args)
    finally:
        agent.origin_var.reset(token)


def _payload(res: dict) -> dict:
    return json.loads(res["content"][0]["text"])


def _sent(env, method: str = "send") -> dict:
    """How many times each bot was asked to run *method*."""
    return {bot: sum(1 for name, _ in calls(ep) if name == method)
            for bot, ep in env.ep.items()}


def _dm_origin(role: str, bot, **over) -> dict:
    """A DM turn as the receiving bot stamps it."""
    origin = {"role": role, "execution_role": role, "channel": "telegram",
              "chat_id": OPERATOR, "user_id": OPERATOR, "cid": "c1",
              "message_type": "channel_in", "source": "telegram",
              "_tg_bot": bot}
    origin.update(over)
    return with_scope(origin)


@pytest.fixture(autouse=True)
def _restore_tools():
    saved = (tools._channel_manager, tools._bus, tools._specialist_registry,
             tools._engagement_registry)
    yield
    (tools._channel_manager, tools._bus, tools._specialist_registry,
     tools._engagement_registry) = saved


# ---------------------------------------------------------------------------
# send_message: the turn's bot, never another
# ---------------------------------------------------------------------------


async def test_send_message_goes_through_the_bot_of_the_turns_route():
    env = build_two_endpoints()
    _init(env.manager)

    res = await _call(tools.send_message, _dm_origin("finance", FINANCE_BOT),
                      {"message": "Your October total is ready.",
                       "channel": "telegram"})

    assert not res.get("is_error"), res
    assert _sent(env) == {CASA_BOT: 0, FINANCE_BOT: 1}
    # the delivery context is the route and nothing the origin could steer
    (_, (_, ctx)), = [c for c in calls(env.finance) if c[0] == "send"]
    assert ctx == {"_tg_bot": FINANCE_BOT}


def _one_bot():
    casa = FakeEndpoint("assistant", bot_id=CASA_BOT, is_casa=True)
    manager = make_transport(casa, casa_bot_ids=MemoryBotIdStore(CASA_BOT))
    return SimpleNamespace(manager=manager, transport=manager.get("telegram"),
                           casa=casa, ep={CASA_BOT: casa})


_FRESH_ORIGINS = {
    "voice": {"role": "butler", "execution_role": "butler", "channel": "voice",
              "chat_id": "kitchen", "cid": "c2", "message_type": "request",
              "source": "voice"},
    "invoke": {"role": "assistant", "execution_role": "assistant",
               "channel": "webhook", "chat_id": "3f0c", "cid": "c3",
               "message_type": "request", "source": "webhook",
               "_origin_route": "invoke"},
    "webhook": {"role": "assistant", "execution_role": "assistant",
                "channel": "webhook", "chat_id": "hook-1", "cid": "c4",
                "message_type": "scheduled", "source": "webhook",
                "_origin_route": "webhook_trigger",
                "_origin_clearance": "public"},
}


@pytest.mark.parametrize("ingress", sorted(_FRESH_ORIGINS))
async def test_a_turn_born_with_no_parent_delivers_once_on_the_one_bot(ingress):
    one = _one_bot()
    _init(one.manager)
    origin = dict(_FRESH_ORIGINS[ingress])
    # the route its ingress stamps at the turn's creation
    origin["_tg_bot"] = one.transport.route_for(origin["role"])

    res = await _call(tools.send_message, with_scope(origin),
                      {"message": "The garage door is open.",
                       "channel": "telegram"})

    assert not res.get("is_error"), res
    assert _sent(one) == {CASA_BOT: 1}


# ---------------------------------------------------------------------------
# A scheduled job: its route is born at launch and persists with it
# ---------------------------------------------------------------------------


class _JobRegistry:
    """The engagement registry a job's question reads, for one record."""

    def __init__(self, rec):
        self.rec = rec

    def get(self, eid):
        return self.rec if eid == self.rec.id else None

    def counts_as_live(self, eid):
        return eid == self.rec.id

    def new_job_question(self, eid, question, options):
        return "q1"

    async def promote_from_quiet(self, eid):
        return True


async def test_a_scheduled_jobs_route_is_born_at_launch_and_kept(monkeypatch):
    import authz_grants
    import background_jobs

    # "butler" has no bot of its own: its job is born on Casa's.
    env = build_two_endpoints()
    _init(env.manager)
    host = SimpleNamespace(role="butler", kind="specialist",
                           decl=SimpleNamespace(title="Weekly ledger"))
    monkeypatch.setattr(background_jobs, "find_job_host", lambda *a: host)
    monkeypatch.setattr(authz_grants, "_live_operator_identity",
                        lambda: (OPERATOR, OPERATOR))
    persisted: list[dict] = []

    async def start_on_host(_host, task, context, origin, **_kw):
        persisted.append(json.loads(json.dumps(origin)))   # as stored
        return tools._result({"status": "pending", "engagement_id": "e1"})

    monkeypatch.setattr(tools, "_start_job_on_host", start_on_host)
    trig = SimpleNamespace(name="weekly", type="cron", job="ledger:weekly",
                           task="Check the ledger.", context="",
                           channel="telegram")

    await tools.start_scheduled_job("butler", trig)

    assert len(persisted) == 1
    assert persisted[0]["_tg_bot"] == CASA_BOT

    # Restart with "butler" now bound to bot 222: the job keeps 111.
    casa = FakeEndpoint("assistant", bot_id=CASA_BOT, is_casa=True)
    butler = FakeEndpoint("butler", bot_id=FINANCE_BOT)
    manager = make_transport(casa, butler, casa=casa,
                             casa_bot_ids=MemoryBotIdStore(CASA_BOT))
    _init(manager)
    after = SimpleNamespace(ep={CASA_BOT: casa, FINANCE_BOT: butler})
    origin = {**persisted[0], "job": {"started_by": "scheduled"}}
    from channels.tg_address import TgAddress
    rec = SimpleNamespace(id="e1", kind="specialist", role_or_type="butler",
                          origin=origin, executor="butler",
                          delivery=TgAddress(CASA_BOT, OPERATOR))
    monkeypatch.setattr(tools, "_engagement_registry", _JobRegistry(rec))

    token = tools.engagement_var.set(rec)
    try:
        res = await _call(tools.ask_operator, with_scope(dict(origin)),
                          {"question": "Split the October total by week?",
                           "options": ["Yes", "No"]})
    finally:
        tools.engagement_var.reset(token)
    await manager.get("telegram").post_job_parked(rec, "Waiting for you.")

    assert _payload(res)["status"] == "asked", _payload(res)
    assert _sent(after, "post_job_question") == {CASA_BOT: 1, FINANCE_BOT: 0}
    assert _sent(after, "post_job_parked") == {CASA_BOT: 1, FINANCE_BOT: 0}


async def test_a_question_with_no_bot_for_its_route_is_not_asked(monkeypatch):
    """A job whose delivery names a bot Casa no longer runs: the question is
    reported not posted, as when a post fails, and nothing raises."""
    env = build_two_endpoints()
    _init(env.manager)
    origin = {"role": "finance", "channel": "telegram", "chat_id": OPERATOR,
              "_tg_bot": 333, "job": {"started_by": "operator"}}
    from channels.tg_address import TgAddress
    rec = SimpleNamespace(id="e2", kind="specialist", role_or_type="finance",
                          origin=origin, executor="finance",
                          delivery=TgAddress(333, OPERATOR))
    monkeypatch.setattr(tools, "_engagement_registry", _JobRegistry(rec))
    token = tools.engagement_var.set(rec)
    try:
        res = await _call(tools.ask_operator, with_scope(dict(origin)),
                          {"question": "Keep the duplicates?"})
    finally:
        tools.engagement_var.reset(token)

    assert _payload(res) == {
        "status": "error", "kind": "delivery_failed",
        "message": "the question was not posted; you may ask again"}
    assert _sent(env, "post_job_question") == {CASA_BOT: 0, FINANCE_BOT: 0}


# ---------------------------------------------------------------------------
# Completion envelopes carry the route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", [FINANCE_BOT, CASA])
async def test_the_engagement_outcome_envelope_carries_the_route(route):
    from specialist_registry import DelegationComplete

    bus = SimpleNamespace(notify=AsyncMock())
    origin = {"role": "assistant", "channel": "telegram", "chat_id": OPERATOR,
              "cid": "c5", "_tg_bot": route}

    await tools.send_engagement_outcome(
        bus, complete=DelegationComplete(delegation_id="e3", agent="finance",
                                         status="ok"),
        origin=origin, ack=None)

    msg = bus.notify.await_args.args[0]
    assert msg.context["_tg_bot"] == route
    assert msg.context["engagement_id"] == "e3"


@pytest.mark.parametrize("route", [FINANCE_BOT, CASA])
async def test_the_delegation_completion_envelope_carries_the_route(
        monkeypatch, route):
    from specialist_registry import DelegationComplete, DelegationRecord

    bus = SimpleNamespace(notify=AsyncMock())
    monkeypatch.setattr(tools, "_bus", bus)
    monkeypatch.setattr(tools, "_specialist_registry", SimpleNamespace(
        job_registry=SimpleNamespace(ack_terminal_notification=AsyncMock())))
    record = DelegationRecord(id="d1", agent="finance", started_at=0.0, origin={
        "role": "assistant", "channel": "telegram", "chat_id": OPERATOR,
        "cid": "c6", "_tg_bot": route})

    async def settled():
        return None

    await tools._settle_then_announce(
        record, DelegationComplete(delegation_id="d1", agent="finance",
                                   status="ok", text="Done."), settled())

    msg = bus.notify.await_args.args[0]
    assert msg.context["_tg_bot"] == route
    assert msg.context["delegation_id"] == "d1"


# ---------------------------------------------------------------------------
# Before Casa's bot has any id: the route is CASA
# ---------------------------------------------------------------------------


@pytest.fixture
def outbox(tmp_path):
    import plugin_outbox

    ob = plugin_outbox.init_outbox(str(tmp_path / "plugin-outbox"))
    path = os.path.join(ob._root_realpath, "invoice.pdf")
    with open(path, "wb") as fh:
        fh.write(b"%PDF-1.4 invoice")
    try:
        yield path
    finally:
        ob.close()
        plugin_outbox._OUTBOX = None


async def test_a_file_sent_before_casas_bot_has_an_id_is_unavailable_and_owed(
        outbox):
    from channels.telegram import TelegramChannel

    # Casa's real endpoint, never connected and with no id persisted.
    casa = TelegramChannel(bot_token="1:x", chat_id=OPERATOR,
                           default_agent="assistant", is_casa=True)
    manager = make_transport(casa, casa_bot_ids=MemoryBotIdStore())
    transport = manager.get("telegram")
    _init(manager)
    with pytest.raises(EndpointNotReady):
        casa.bot_id
    origin = _dm_origin("assistant", transport.casa_route())
    assert origin["_tg_bot"] == CASA

    res = await _call(tools.send_media, origin,
                      {"path": outbox, "kind": "document"})

    payload = _payload(res)
    assert payload["status"] == "error"
    assert payload["kind_error"] == "channel_unavailable"
    sends = origin["turn_scope"].operator_sends
    assert len(sends) == 1
    assert sends[0].delivered is False


class _AskEndpoint(FakeEndpoint):
    """Casa's endpoint, able to post a question's keyboard."""

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options,
                               short_labels=False, shorts=None):
        self._record("post_dm_keyboard", chat_id, request_id)
        return 77

    async def edit_dm_message(self, chat_id, message_id, text):
        self._record("edit_dm_message", chat_id, message_id)
        return True


class _RecordingStore:
    def __init__(self):
        self.records: dict = {}
        self.writes: list = []

    def get(self, rid):
        return self.records.get(rid)

    def all(self):
        return list(self.records.values())

    async def put(self, record):
        self.writes.append(("put", record["rid"]))
        self.records[record["rid"]] = dict(record)

    async def set_state(self, rid, state, **fields):
        self.writes.append(("set_state", rid))
        self.records.get(rid, {}).update(state=state, **fields)
        return True

    async def drop(self, rid):
        self.writes.append(("drop", rid))
        self.records.pop(rid, None)


async def test_a_scheduled_question_before_casas_bot_has_an_id_touches_nothing(
        monkeypatch):
    broker = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    registered: list = []
    real_register = broker.register

    def register(**kw):
        registered.append(kw["scope"])
        return real_register(**kw)

    monkeypatch.setattr(broker, "register", register)
    store = _RecordingStore()
    monkeypatch.setattr(scheduled_asks, "STORE", store)
    monkeypatch.setattr(scheduled_asks, "_ROLE_EPOCHS", {})
    monkeypatch.setattr(scheduled_asks, "_TRIGGER_EPOCHS", {})
    scopes: list = []
    real_scope = tools.tg_scope

    def counting_scope(kind, bot, chat):
        scopes.append((kind, bot, chat))
        return real_scope(kind, bot, chat)

    monkeypatch.setattr(tools, "tg_scope", counting_scope)

    casa = _AskEndpoint("assistant", bot_id=None, is_casa=True, ready=False)
    manager = make_transport(casa, casa_bot_ids=MemoryBotIdStore())
    transport = manager.get("telegram")
    _init(manager)
    label = "cron-invoices"

    def scheduled_origin():
        return with_scope({
            "role": "assistant", "execution_role": "assistant",
            "channel": "telegram", "chat_id": label, "user_id": None,
            "message_type": "scheduled", "source": "scheduler",
            "_scheduled_delivery": True,
            "_scheduled_epoch": scheduled_asks.epoch_for("assistant", label),
            "_tg_bot": transport.casa_route()})

    args = {"question": "Send the invoices now?", "options": ["Yes", "No"]}
    origin = scheduled_origin()
    assert origin["_tg_bot"] == CASA

    payload = _payload(await _call(tools.ask_user, origin, args))

    assert payload == {"status": "error", "kind": "delivery_failed",
                       "message": "could not deliver the question to the operator"}
    assert registered == []
    assert store.writes == []
    assert scopes == []
    assert calls(casa) == []

    # Once Casa's bot has verified its id, the same question keys on it.
    assert casa.initialize(CASA_BOT)
    payload = _payload(await _call(tools.ask_user, scheduled_origin(), args))

    assert payload["status"] == "awaiting_user", payload
    assert registered == [f"dm:{CASA_BOT}:{OPERATOR}"]
    (record,) = store.records.values()
    assert record["bot_id"] == CASA_BOT
    assert record["scope"] == f"dm:{CASA_BOT}:{OPERATOR}"
