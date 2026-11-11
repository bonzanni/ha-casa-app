"""The bot in memory keys and chat-note keys, and the routes FRESH turns are born with.

Telegram numbers messages per bot, and two bots' private chats with the
operator share the chat id. So:

* a memory key of an operator message names the bot that received it, or
  bot B's message N collapses into bot A's message N and is lost
  (``events.source_key`` is unique);
* Casa's notes for a chat are keyed by the bot, or one bot's agent hears what
  happened in the other's conversation;
* a turn with no parent (a fired trigger, a swept reminder, a voice
  utterance) is stamped with ``route_for(role)`` when it is created, so every
  later delivery of that turn goes through the bot decided then, and with one
  bot it goes exactly where it goes today.

A route value (``CASA``) can never become part of a key.
"""

from __future__ import annotations

import json
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from aiohttp import WSMsgType, web

import tools as tools_mod
from channels.tg_address import CASA, AddressError, RouteMissing, TgAddress
from memory import capture, occurrence
from two_endpoints import (
    CASA_BOT, FINANCE_BOT, OPERATOR, FakeEndpoint, build_two_endpoints,
)
from telegram_channel_double import MemoryBotIdStore, make_transport

# fixtures shared with the memory suites
from test_memory_capture import (  # noqa: F401 — pytest fixtures
    _engagement_client, _rows_when, captured_events, mem,
)
from test_voice_context_sanitize import voice_app  # noqa: F401 — pytest fixture

pytestmark = pytest.mark.asyncio

CHAT = 4242


def _use_transport(monkeypatch, manager) -> None:
    monkeypatch.setattr(tools_mod, "_channel_manager", manager)


def _only_casa(*, known: "int | None" = CASA_BOT, ready: bool = True):
    """A transport with Casa's endpoint alone (role ``assistant``). With
    *known* None and not ready, Casa's bot has never completed ``getMe``."""
    casa = FakeEndpoint("assistant", bot_id=known, is_casa=True, ready=ready)
    manager = make_transport(casa, casa=casa, casa_bot_ids=MemoryBotIdStore(known))
    return manager, manager.get("telegram"), casa


# --- memory keys -------------------------------------------------------------

async def test_message_key_names_the_receiving_bot():
    key = occurrence.message_key("telegram", CASA_BOT, CHAT, 55)
    assert key == occurrence.message_key("telegram", CASA_BOT, str(CHAT), "55")
    assert key != occurrence.message_key("telegram", FINANCE_BOT, CHAT, 55)
    assert key.startswith("provider:")
    assert (occurrence.engagement_key("telegram", CASA_BOT, "e1", 55)
            != occurrence.engagement_key("telegram", FINANCE_BOT, "e1", 55))
    assert (occurrence.engagement_key("telegram", CASA_BOT, "e1", 55)
            == occurrence.message_key("telegram", CASA_BOT, "engagement:e1", 55))


@pytest.mark.parametrize("bot", [CASA, None, True, 0, -1, "111", 111.0])
async def test_a_route_value_never_becomes_a_memory_key(bot):
    with pytest.raises(AddressError):
        occurrence.message_key("telegram", bot, CHAT, 55)
    with pytest.raises(AddressError):
        occurrence.engagement_key("telegram", bot, "e1", 55)


def _operator_item(key: str, text: str) -> capture.Event:
    return capture.event(role="assistant", channel="telegram",
                         speaker_kind="operator", origin_kind="operator",
                         text=text, eligibility="write_trusted_turn",
                         origin_tier="private", source_key=key)


async def test_equal_chat_and_message_on_two_bots_are_two_events(mem):
    """The same (chat, message id) on bots 111 and 222 is two messages;
    the same message delivered twice on one bot stays one."""
    for bot, text in ((CASA_BOT, "to Ellen"), (FINANCE_BOT, "to Finance"),
                      (CASA_BOT, "to Ellen")):
        capture.append(mem, _operator_item(
            occurrence.message_key("telegram", bot, CHAT, 7), text))
    rows = await captured_events(mem)
    assert sorted(r["text"] for r in rows) == ["to Ellen", "to Finance"]


async def test_a_job_reply_is_keyed_by_the_bot_that_received_it(mem):
    """Through a job reply: the endpoint hands ``submit_operator`` the
    address it received the message at; the engagement's own route (here
    ``CASA``, a turn born before Casa's bot was verified) is not used."""
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRecord
    from turn_scripted_session import attach_session

    drv = InCasaDriver()
    rec = EngagementRecord(
        id="f" * 32, kind="specialist", role_or_type="finance",
        driver="in_casa", status="active", started_at=0.0,
        last_user_turn_ts=0.0, completed_at=None,
        sdk_session_id=None, task="t",
        origin={"channel": "telegram", "_origin_route": "telegram",
                "_origin_clearance": "private", "_tg_bot": CASA})
    attach_session(drv, rec, _engagement_client())
    try:
        drv.submit_operator(rec, "on Ellen's bot",
                            tg_message=TgAddress(CASA_BOT, CHAT, 55))
        drv.submit_operator(rec, "on Finance's bot",
                            tg_message=TgAddress(FINANCE_BOT, CHAT, 55))
        rows = await _rows_when(mem, 6)
    finally:
        await drv.cancel(rec)
    operator = {r["text"]: r["source_key"] for r in rows
                if r["speaker_kind"] == "operator"}
    assert operator == {
        "on Ellen's bot": occurrence.engagement_key(
            "telegram", CASA_BOT, rec.id, "55"),
        "on Finance's bot": occurrence.engagement_key(
            "telegram", FINANCE_BOT, rec.id, "55"),
    }


class _Endpoint:
    """What the job-reply capture reads off the receiving endpoint."""

    name = "telegram"
    chat_id = OPERATOR

    def __init__(self, bot_id: int) -> None:
        self.bot_id = bot_id

    def _user_id_is_operator(self, user_id) -> bool:
        return user_id == OPERATOR


@pytest.fixture
def no_memory_specialist(monkeypatch):
    """``finance`` keeps no memory, so the replies to his job are captured by
    the operator-words seam."""
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "assistant": SimpleNamespace(kind="resident"),
        "finance": SimpleNamespace(kind="specialist",
                                   memory=SimpleNamespace(token_budget=0)),
    })


async def test_job_replies_on_two_bots_are_two_events(
        mem, no_memory_specialist):
    from delegated_memory import capture_job_reply

    job = SimpleNamespace(kind="specialist", id="a" * 32, role_or_type="finance")
    for bot in (CASA_BOT, FINANCE_BOT):
        capture_job_reply(channel=_Endpoint(bot), engagement=job, user_id=OPERATOR,
                          user_name="Op", message_id=9, resident="assistant",
                          text=f"job on {bot}")
    rows = await captured_events(mem)
    keys = {r["text"]: r["source_key"] for r in rows}
    assert keys == {
        f"job on {b}": occurrence.engagement_key("telegram", b, job.id, "9")
        for b in (CASA_BOT, FINANCE_BOT)
    }


# --- the chat notes' keys -----------------------------------------------------

async def test_chat_notes_are_per_bot():
    import chat_notes
    chat_notes.reset()
    chat_notes.record(CASA_BOT, OPERATOR, "📊 Finance answered your reply (1 page).")
    assert chat_notes.drain(FINANCE_BOT, OPERATOR) == []
    assert chat_notes.drain(CASA_BOT, OPERATOR) == [
        "📊 Finance answered your reply (1 page)."]
    chat_notes.record(CASA, OPERATOR, "never recorded")
    assert chat_notes.drain(CASA_BOT, OPERATOR) == []
    chat_notes.reset()


# --- FRESH routes: triggers --------------------------------------------------

def _fire_trigger(role: str, channel: str = "telegram"):
    from config import TriggerSpec
    from trigger_registry import TriggerRegistry

    sched, bus = MagicMock(), MagicMock()
    bus.send = AsyncMock()
    reg = TriggerRegistry(scheduler=sched, app=web.Application(), bus=bus)
    reg.register_agent(role, [TriggerSpec(name="hb", type="interval", minutes=60,
                                          channel=channel, prompt="tick")],
                       channels=[channel])
    return sched.add_job.call_args.args[0], bus


@pytest.mark.parametrize("role, expected", [
    ("finance", FINANCE_BOT),       # the role's own bot
    ("butler", CASA_BOT),           # a role with no bot of its own: Casa's
    ("assistant", CASA_BOT),
])
async def test_a_fired_trigger_is_born_with_route_for_its_role(monkeypatch, role, expected):
    _use_transport(monkeypatch, build_two_endpoints().manager)
    fire, bus = _fire_trigger(role)
    await fire()
    assert bus.send.call_args.args[0].context["_tg_bot"] == expected


async def test_a_trigger_born_before_casas_bot_is_verified_carries_casa(monkeypatch):
    manager, transport, casa = _only_casa(known=None, ready=False)
    _use_transport(monkeypatch, manager)
    fire, bus = _fire_trigger("assistant")
    await fire()
    ctx = bus.send.call_args.args[0].context
    assert ctx["_tg_bot"] == CASA
    assert transport.endpoint_for_route(ctx, site="t") is casa


async def test_a_trigger_stamps_a_route_whatever_its_channel(monkeypatch):
    """Its tools may still send to Telegram (``send_message``)."""
    _use_transport(monkeypatch, build_two_endpoints().manager)
    fire, bus = _fire_trigger("finance", channel="voice")
    await fire()
    assert bus.send.call_args.args[0].context["_tg_bot"] == FINANCE_BOT


async def test_no_telegram_no_route(monkeypatch):
    _use_transport(monkeypatch, None)
    fire, bus = _fire_trigger("assistant")
    await fire()
    assert "_tg_bot" not in bus.send.call_args.args[0].context


async def test_the_route_is_decided_at_birth_not_at_delivery(monkeypatch):
    """A trigger fired for ``finance`` while it has no bot is stamped
    with Casa's bot; binding ``finance`` to its own bot afterwards does not
    move that turn's delivery."""
    manager, transport, casa = _only_casa()
    _use_transport(monkeypatch, manager)
    fire, bus = _fire_trigger("finance")
    await fire()
    ctx = bus.send.call_args.args[0].context
    assert ctx["_tg_bot"] == CASA_BOT
    finance = FakeEndpoint("finance", bot_id=FINANCE_BOT)
    transport.add_endpoint(finance, casa=False)
    assert transport.route_for("finance") == FINANCE_BOT
    await transport.send("the answer", ctx)
    assert [c[0] for c in casa.calls] == ["send"] and finance.calls == []


# --- FRESH routes: the reminder sweep ----------------------------------------

def _sweep_runtime(tmp_path, role: str):
    from trigger_registry import TriggerRegistry

    agents_dir = tmp_path / "agents"
    (agents_dir / role).mkdir(parents=True)
    with open(agents_dir / role / "triggers.yaml", "w", encoding="utf-8") as fh:
        yaml.safe_dump({"schema_version": 1, "triggers": [{
            "name": "reminder-old11111", "type": "date",
            "at": "2026-08-03T08:00:00+02:00", "one_shot": True,
            "channel": "telegram", "managed_by": "agent",
            "prompt": 'Send this exact message via telegram: "Bins."'}]},
            fh, sort_keys=False)
    bus = MagicMock()
    bus.send = AsyncMock()
    registry = TriggerRegistry(scheduler=MagicMock(), app=web.Application(), bus=bus)
    return types.SimpleNamespace(agents_dir=str(agents_dir), bus=bus,
                                 trigger_registry=registry, role_configs={role: object()})


@pytest.mark.parametrize("role, expected", [("finance", FINANCE_BOT),
                                            ("assistant", CASA_BOT)])
async def test_a_swept_reminder_is_born_with_route_for_its_role(
        monkeypatch, tmp_path, role, expected):
    from datetime import datetime, timedelta, timezone
    import reminders

    _use_transport(monkeypatch, build_two_endpoints().manager)
    runtime = _sweep_runtime(tmp_path, role)
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    assert await reminders.sweep_reminders(runtime, now) == 1
    assert runtime.bus.send.call_args.args[0].context["_tg_bot"] == expected


# --- FRESH routes: voice -----------------------------------------------------

async def _sse_turn(client, context: dict) -> None:
    resp = await client.post("/api/converse", json={
        "prompt": "hi", "agent_role": "butler", "context": context})
    await resp.read()


async def _ws_turn(client, context: dict) -> None:
    async with client.ws_connect("/api/converse/ws") as ws:
        await ws.send_json({"type": "utterance", "utterance_id": "u1", "text": "hi",
                            "agent_role": "butler", "scope_id": "s",
                            "context": context})
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            if json.loads(msg.data)["type"] in ("done", "error"):
                break


@pytest.mark.parametrize("turn", [_sse_turn, _ws_turn], ids=["sse", "ws"])
async def test_a_voice_turn_is_born_with_route_for_its_role_server_side(
        monkeypatch, voice_app, turn):  # noqa: F811
    """Both voice ingresses stamp ``route_for(agent_role)`` themselves; a
    ``_tg_bot`` in the caller's context never survives."""
    client, agent, _channel = voice_app
    te = build_two_endpoints()
    _use_transport(monkeypatch, te.manager)
    await turn(client, {"device_id": "kitchen", "_tg_bot": FINANCE_BOT})
    ctx = agent.captured[-1]
    assert ctx["_tg_bot"] == CASA_BOT            # butler has no bot: Casa's
    assert ctx["device_id"] == "kitchen"


@pytest.mark.parametrize("turn", [_sse_turn, _ws_turn], ids=["sse", "ws"])
async def test_with_one_bot_a_voice_turn_delivers_to_telegram_once(
        monkeypatch, voice_app, turn):  # noqa: F811
    """With one bot, what the voice turn's tools send
    to Telegram resolves and is delivered exactly once, never ``RouteMissing``.
    Before Casa's bot is verified the route is ``CASA``, which resolves to
    the same endpoint."""
    client, agent, _channel = voice_app
    for known, ready, route in ((CASA_BOT, True, CASA_BOT), (None, False, CASA)):
        manager, transport, casa = _only_casa(known=known, ready=ready)
        _use_transport(monkeypatch, manager)
        await turn(client, {})
        ctx = agent.captured[-1]
        assert ctx["_tg_bot"] == route
        await transport.send("from a voice turn", ctx)
        assert [c[0] for c in casa.calls] == ["send"]


async def test_a_voice_turn_without_telegram_has_no_route(monkeypatch, voice_app):  # noqa: F811
    client, agent, _channel = voice_app
    _use_transport(monkeypatch, None)
    await _sse_turn(client, {})
    assert "_tg_bot" not in agent.captured[-1]


async def test_an_unstamped_context_is_refused_not_guessed():
    """The other half of the contract: a context with no route reaches no bot."""
    manager, transport, casa = _only_casa()
    with pytest.raises(RouteMissing):
        await transport.send("x", {"chat_id": "s"})
    assert casa.calls == []
