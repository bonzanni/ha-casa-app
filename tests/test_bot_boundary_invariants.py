"""The bot boundary's invariants, end to end.

Telegram numbers updates and messages per bot, and a private chat's id is the
user's id, so once Casa runs more than one bot a chat and a message id no
longer name a message. Production runs one bot; these tests run two behind
one transport, bot 111 (Casa's own endpoint, role ``assistant``) and bot 222
(role ``finance``), both serving operator 100, and drive the real code paths
that cross the boundary:

* a record written on one bot keeps acting through that bot after a restart,
  even when its role is bound to another bot by then, and a route is decided
  once, where its turn is born;
* a stored address never resolves to another bot: a bot with no endpoint is
  a typed failure, and a route-bound call without a route is refused;
* Casa's own cards and notices go through Casa's bot whatever bot the turn
  came from;
* a turn born before Casa's bot has connected delivers through it once it
  does, whether or not its id was known;
* every derivation of an origin keeps the route it was built from;
* before Casa's bot has an id, a card that must name it first posts nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import itertools
import json
import logging
import os
import stat
import textwrap
import time
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

import agent as agent_mod
import authz_grants
import scheduled_asks
import tools
import verdict_broker
from bus import BusMessage, MessageBus, MessageType
from channels import ChannelManager, DeliveryOutcome
from channels.tg_address import (
    CASA,
    AddressError,
    DuplicateEndpoint,
    EndpointUnavailable,
    RouteMissing,
    TgAddress,
)
from output_boundary_testing import admitted, with_scope
from telegram_channel_double import MemoryBotIdStore, make_transport
from two_endpoints import (
    CASA_BOT,
    FINANCE_BOT,
    OPERATOR,
    FakeEndpoint,
    build_two_endpoints,
    calls,
)
from verdict_broker import VerdictBroker


# ---------------------------------------------------------------------------
# Restart keeps the stored bot; a route is decided once
#
# A record written while bot 111 carried the conversation names 111. After a
# restart the role it serves (``finance``) may be bound to another bot (222):
# every reply, edit, continuation and notice that acts on the record still
# goes through 111, and 222 is never asked to do anything. Each case writes
# the record through its real store, reloads it from disk, then acts on it
# through the real code path that does so after a restart.
# ---------------------------------------------------------------------------

_A_ROLE_OF = {CASA_BOT: "assistant", FINANCE_BOT: "finance"}


def _a_fake_bot(bot_id: int) -> MagicMock:
    """A Bot API stand-in whose every call is recorded on the mock, so
    ``calls(ep)`` lists what went out through this bot."""
    bot = MagicMock(name=f"bot{bot_id}")
    bot.id = bot_id
    ids = itertools.count(1000)
    bot.send_message = AsyncMock(side_effect=lambda **kw: NS(message_id=next(ids)))
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_reply_markup = AsyncMock(return_value=True)
    bot.send_chat_action = AsyncMock(return_value=True)
    bot.delete_message = AsyncMock(return_value=True)
    return bot


class _A_Bus:
    def __init__(self) -> None:
        self.sent: list = []
        self.queues = {"assistant": object(), "finance": object()}

    async def send(self, msg) -> None:
        self.sent.append(msg)

    async def send_checked(self, msg) -> str:
        self.sent.append(msg)
        return "accepted"

    async def notify(self, msg) -> None:
        self.sent.append(msg)


class _A_Driver:
    def __init__(self) -> None:
        self.submitted: list = []

    def submit_operator(self, rec, text, *, tg_message=None, notice=None):
        self.submitted.append((rec.id, text, tg_message, notice))


def _a_endpoint(bot_id: int, bus: _A_Bus, *, role: "str | None" = None):
    """A real ``TelegramChannel`` for *bot_id* over a fake bot, serving
    operator 100."""
    from channels.telegram import TelegramChannel
    bot = _a_fake_bot(bot_id)
    ep = TelegramChannel(
        bot_token=f"{bot_id}:token", chat_id=str(OPERATOR),
        default_agent=role or _A_ROLE_OF.get(bot_id, "assistant"), bus=bus,
        bot=bot, is_casa=bot_id == CASA_BOT, known_bot_id=bot_id,
    )
    ep._app = MagicMock(bot=bot)
    return ep


def _a_user():
    return NS(id=OPERATOR, first_name="Op", is_bot=False)


def _a_text_update(text: str, *, message_id: int, reply_to=None):
    user = _a_user()
    msg = NS(message_id=message_id, text=text, chat=NS(id=OPERATOR),
             from_user=user, reply_to_message=reply_to, caption=None,
             sender_chat=None, date=None)
    return NS(message=msg, effective_chat=NS(id=OPERATOR), effective_user=user)


def _a_quoted(message_id: int, bot_id: int, text: str = "posted"):
    return NS(message_id=message_id, text=text, caption=None, date=None,
              from_user=NS(id=bot_id, is_bot=True), sender_chat=None)


def _a_turns(bus: _A_Bus) -> list:
    return [m for m in bus.sent if getattr(m, "context", None) is not None]


def _a_job_state() -> dict:
    import background_jobs as jobs
    decl = jobs.JobDecl("ledger:work", "ledger", "work", "ledger:work",
                        "Check the ledger", None)
    return jobs.initial_job_state(decl, started_by="operator")


async def _a_registry(path):
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(path))
    await reg.load()
    return reg


async def _a_flush(reg) -> None:
    while reg._deferred_persists:
        await asyncio.gather(*list(reg._deferred_persists),
                             return_exceptions=True)


@pytest.fixture
def a_isolated(monkeypatch):
    """A fresh broker and post map; the per-chat lane cancel and the memory
    captures of an operator message recorded rather than run."""
    import delegated_memory
    import result_broker
    import scheduled_asks
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    monkeypatch.setattr(result_broker, "POST_MAP", result_broker.PostMap())
    monkeypatch.setattr(scheduled_asks, "cancel_non_scheduled_for_chat",
                        lambda bot, chat, reason: 0)
    monkeypatch.setattr(delegated_memory, "capture_post_line", lambda **kw: None)
    monkeypatch.setattr(delegated_memory, "capture_job_reply", lambda **kw: None)


@pytest.fixture
def a_pair(a_isolated, monkeypatch):
    """Real endpoints for bots 111 (Casa's, ``assistant``) and 222
    (``finance``) behind one transport, which ``tools`` uses."""
    import tools as tools_mod
    bus = _A_Bus()
    casa, finance = _a_endpoint(CASA_BOT, bus), _a_endpoint(FINANCE_BOT, bus)
    h = build_two_endpoints(casa=casa, finance=finance)
    h.bus = bus
    monkeypatch.setattr(tools_mod, "_channel_manager", h.manager, raising=False)
    return h


# -- a live job ---------------------------------------------------------------


async def test_after_a_restart_a_jobs_posts_and_replies_stay_on_the_bot_it_ran_on(
        a_pair, tmp_path):
    """A finance job ran on bot 111 and posted its question there as message
    5. After a restart finance has its own bot, 222. The job's posts are
    re-indexed from the stored record: a reply on 111 to message 5 reaches
    the job, the same message id quoted on 222 is an ordinary finance turn,
    and the job's next question goes out through 111."""
    from engagement_registry import JOB_POST_QUESTION
    path = tmp_path / "engagements.json"
    reg = await _a_registry(path)
    rec = await reg.create(
        "specialist", "finance", "in_casa", "check the ledger",
        {"role": "finance", "channel": "telegram", "chat_id": str(OPERATOR),
         "user_id": OPERATOR, "job": _a_job_state(), "_tg_bot": CASA_BOT})
    assert reg.record_job_post(rec.id, TgAddress(CASA_BOT, OPERATOR, 5),
                               keyboard=JOB_POST_QUESTION)
    await _a_flush(reg)

    again = await _a_registry(path)
    driver = _A_Driver()
    for ep in (a_pair.casa, a_pair.finance):
        ep._engagement_registry, ep._engagement_driver = again, driver
    loaded = again.get(rec.id)
    assert loaded.origin["_tg_bot"] == CASA_BOT

    await a_pair.finance._handle(_a_text_update(
        "B, please", message_id=40, reply_to=_a_quoted(5, FINANCE_BOT)), None)
    assert driver.submitted == []
    assert [m.target for m in _a_turns(a_pair.bus)] == ["finance"]
    assert _a_turns(a_pair.bus)[0].context["_tg_bot"] == FINANCE_BOT

    await a_pair.casa._handle(_a_text_update(
        "A, please", message_id=41, reply_to=_a_quoted(5, CASA_BOT)), None)
    assert [(eid, msg) for eid, _t, msg, _n in driver.submitted] == [
        (rec.id, TgAddress(CASA_BOT, OPERATOR, 41))]

    before = len(calls(a_pair.finance))
    mid = await a_pair.transport.post_job_question(
        loaded, "q2", admitted("Which account?"), ["A", "B"], ["A", "B"])
    assert mid is not None
    assert len(calls(a_pair.finance)) == before == 0
    assert TgAddress(CASA_BOT, OPERATOR, mid) in again.get(rec.id).dm_posts


# -- a delegation completion through the boot replay --------------------------


def _a_voice_job(**changes):
    from job_registry import DeliveryState, ExecutionState, VoiceJob
    from personality_types import SpeakerProvenance
    system = SpeakerProvenance(speaker_kind="system")
    base = VoiceJob(
        id="d" * 32, parent_job_id=None,
        creating_speaker=system, executing_speaker=system,
        creating_role="finance", specialist_role="reporter",
        specialist_display_name="Reporter",
        creator_peer="telegram", creator_user_id=str(OPERATOR),
        scope_id=str(OPERATOR), origin_route_id="cid-1", origin_device_id=None,
        task="the quarter's report", context="",
        created_at=100.0, started_at=101.0, terminal_at=None,
        expires_at=None, execution_state=ExecutionState.RUNNING,
        delivery_state=DeliveryState.NONE,
        result=None, failure=None, awaiting_input=False,
        continuable_until=None, delivery_sequence=0,
        delivery_attempt_id=None, lease_until=None, cancel_pending=False,
        route={"_tg_bot": CASA_BOT},
    )
    return replace(base, **changes)


async def test_a_delegation_replayed_at_boot_is_answered_through_the_bot_it_was_asked_on(
        tmp_path):
    """A finance turn on bot 111 delegated; Casa restarted while the
    delegation ran. Finance is bound to bot 222 now. The real boot replay
    rebuilds the notice from the stored row, the resident's answering turn
    is synthesized from it, and that answer goes out through 111 only."""
    import agent
    import casa_core
    from job_registry import JobRegistry

    jobs = JobRegistry(tmp_path / "jobs.json", clock=lambda: 200.0)
    await jobs.load()
    await jobs.create(_a_voice_job())
    jobs = JobRegistry(tmp_path / "jobs.json", clock=lambda: 200.0)
    await jobs.load()
    recovered = await jobs.recover_after_restart()

    two = build_two_endpoints()
    assert two.transport.route_for("finance") == FINANCE_BOT
    bus = _A_Bus()
    await casa_core._notify_recovered_delegations(
        recovered, jobs, bus, assistant_role="assistant")
    (notice,) = bus.sent
    assert notice.target == "finance"

    turn = agent.Agent._synthesize_delegation_turn(NS(), notice)
    assert turn.context["_tg_bot"] == CASA_BOT
    await two.transport.send_response(admitted("The report is ready."),
                                      turn.context)
    assert [n for n, _ in calls(two.casa)] == ["send_response"]
    assert calls(two.finance) == []


# -- a plugin continuation from a stored binding ------------------------------


async def test_a_continuation_after_a_restart_runs_on_the_bot_its_binding_names(
        tmp_path, monkeypatch):
    """A plugin call made in a finance turn on bot 111 minted a binding.
    After a restart, with finance on bot 222, the binding is read back from
    disk: the continuation turn it rebuilds is routed to 111, the identity
    it keys by is 111's, and its reply goes out through 111 only."""
    import plugin_bindings
    import plugin_continuations as pc
    import tools as tools_mod
    from authz_grants import GrantIdentity

    two = build_two_endpoints()
    monkeypatch.setattr(tools_mod, "_channel_manager", two.manager, raising=False)
    path = str(tmp_path / "bindings.json")
    identity = GrantIdentity(operator_id=OPERATOR, chat_id=OPERATOR,
                             enforcement_role="browser", artifact_id="1" * 64,
                             engagement_id="", bot_id=CASA_BOT)
    minted = plugin_bindings.BindingStore(path).for_turn(
        plugin="browser", turn_key="turn-1", identity=identity,
        chain=("finance", "browser"),
        origin={"role": "finance", "channel": "telegram", "chat_id": OPERATOR,
                "user_id": OPERATOR, "_origin_route": "telegram",
                "_origin_clearance": "private", "_tg_bot": CASA_BOT})

    stored = plugin_bindings.BindingStore(path).peek(minted.ref)
    assert stored is not None
    origin = pc.continuation_origin(stored)
    assert origin["_tg_bot"] == CASA_BOT
    ep, bot = plugin_bindings.binding_target(stored)
    assert ep is two.casa and bot == CASA_BOT
    await two.transport.send_response(admitted("Downloaded."), origin)
    assert [n for n, _ in calls(two.casa)] == ["send_response"]
    assert calls(two.finance) == []


# -- a scheduled question at boot ----------------------------------------------


class _A_AskEndpoint(FakeEndpoint):
    """A fake endpoint that edits keyboards and carries a scheduled
    question's continuation, recording each on its own bot."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.edits: list[tuple] = []
        self.continuations: list[dict] = []

    async def edit_dm_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))
        return True

    async def _dispatch_scheduled_continuation(self, **kw):
        self.continuations.append(kw)
        return True


@pytest.fixture
def a_asks(monkeypatch, tmp_path):
    import scheduled_asks
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    monkeypatch.setattr(scheduled_asks, "_ROLE_EPOCHS", {})
    monkeypatch.setattr(scheduled_asks, "_TRIGGER_EPOCHS", {})
    monkeypatch.setattr(scheduled_asks, "_BOOT_REVOCATIONS", [])
    monkeypatch.setattr(scheduled_asks, "_BOOT_RECONCILED", False)
    path = str(tmp_path / "scheduled_asks.json")
    monkeypatch.setattr(scheduled_asks, "STORE",
                        scheduled_asks.ScheduledAskStore(path))
    casa = _A_AskEndpoint("assistant", bot_id=CASA_BOT, is_casa=True)
    finance = _A_AskEndpoint("finance", bot_id=FINANCE_BOT)
    two = build_two_endpoints(casa=casa, finance=finance)
    two.asks_path = path
    return two


def _a_ask_record(**over) -> dict:
    from channels.tg_address import scope
    import scheduled_asks
    rec = {"rid": "rid-f", "state": scheduled_asks.STATE_LIVE,
           "role": "finance", "session_scope": "cron-ledger",
           "scope": scope("dm", CASA_BOT, OPERATOR), "bot_id": CASA_BOT,
           "chat_id": OPERATOR, "operator_id": OPERATOR, "message_id": 77,
           "options": ["Yes", "No"], "body": "Pay it?", "epoch": None,
           "created_at": 0.0, "expires_at": 1_000.0}
    rec.update(over)
    return rec


async def _a_put_and_reload(two, rec) -> None:
    import scheduled_asks
    await scheduled_asks.STORE.put(rec)
    scheduled_asks.STORE = scheduled_asks.ScheduledAskStore(two.asks_path)


async def test_a_scheduled_question_restored_at_boot_stays_in_the_lane_of_the_bot_that_asked(
        a_asks):
    """A finance question was posted through bot 111 before the restart;
    finance is on bot 222 now. Boot restores it in 111's lane, where a tap
    on 111's keyboard finds it, and 222's lane stays empty."""
    import scheduled_asks
    import verdict_broker
    from channels.tg_address import scope
    await _a_put_and_reload(a_asks, _a_ask_record())
    counts = await scheduled_asks.reconcile_at_boot(a_asks.transport, now=0.0)
    assert counts["restored"] == 1
    broker = verdict_broker.BROKER
    assert broker.pending(namespace="resident_ask",
                          scope=scope("dm", CASA_BOT, OPERATOR)) == ["rid-f"]
    assert broker.pending(namespace="resident_ask",
                          scope=scope("dm", FINANCE_BOT, OPERATOR)) == []
    meta = broker.get_meta(namespace="resident_ask",
                           scope=scope("dm", CASA_BOT, OPERATOR), request_id="rid-f")
    assert meta["bot_id"] == CASA_BOT

    # Cancelling 222's lane leaves it; cancelling 111's settles it, and its
    # keyboard is edited and its outcome carried through 111.
    assert scheduled_asks.cancel_for_chat(FINANCE_BOT, OPERATOR, "x") == 0
    assert scheduled_asks.cancel_for_chat(CASA_BOT, OPERATOR, "operator_new") == 1
    for _ in range(200):
        if a_asks.casa.continuations:
            break
        await asyncio.sleep(0.01)
    assert [e[:2] for e in a_asks.casa.edits] == [(OPERATOR, 77)]
    assert [c["target_role"] for c in a_asks.casa.continuations] == ["finance"]
    assert a_asks.finance.edits == [] and a_asks.finance.continuations == []


async def test_a_scheduled_question_that_expired_over_a_restart_is_settled_through_its_bot(
        a_asks):
    """The same question expired while Casa was down: its keyboard is edited
    and its answer carried through 111, the bot that posted it."""
    import scheduled_asks
    await _a_put_and_reload(a_asks, _a_ask_record(expires_at=1.0))
    counts = await scheduled_asks.reconcile_at_boot(a_asks.transport, now=5.0)
    assert counts["expired"] == 1
    assert [e[:2] for e in a_asks.casa.edits] == [(OPERATOR, 77)]
    assert [c["target_role"] for c in a_asks.casa.continuations] == ["finance"]
    assert a_asks.finance.edits == [] and a_asks.finance.continuations == []


# -- a run card's Stop ---------------------------------------------------------


async def test_a_stop_after_a_restart_is_taken_and_told_on_the_bot_that_posted_the_card(
        a_pair, tmp_path, monkeypatch):
    """A browsing request admitted in a finance turn on bot 111 posted its
    run card there. After a restart, with finance on bot 222, the admission
    and the binding are read back from disk. The card's Stop, tapped on 111,
    stops the request, marks the card and tells the line through 111; the
    same Stop arriving on 222 is refused, and 222 sends nothing."""
    import plugin_bindings
    import plugin_continuations as pc
    import plugin_stop
    import run_admission
    import specialist_desk
    from authz_grants import GrantIdentity

    bindings_path = str(tmp_path / "bindings.json")
    admissions_path = str(tmp_path / "admissions.json")
    identity = GrantIdentity(operator_id=OPERATOR, chat_id=OPERATOR,
                             enforcement_role="browser", artifact_id="1" * 64,
                             engagement_id="", bot_id=CASA_BOT)
    binding = plugin_bindings.BindingStore(bindings_path).for_turn(
        plugin="browser", turn_key="turn-1", identity=identity,
        chain=("finance", "browser"),
        origin={"role": "finance", "channel": "telegram", "chat_id": OPERATOR,
                "user_id": OPERATOR, "_tg_bot": CASA_BOT})
    run_admission.Confirmations(admissions_path).add(run_admission.Confirmation(
        cid="conf-1", plugin="browser", bot_id=CASA_BOT, operator_id=OPERATOR,
        binding_ref=binding.ref, request_key="k1", status="admitted",
        admissions=[], created_at=1e12, card=[CASA_BOT, OPERATOR, 7],
        card_text="Browsing now:"))

    # the restart: both stores read back from disk
    monkeypatch.setattr(plugin_bindings, "BINDINGS",
                        plugin_bindings.BindingStore(bindings_path))
    monkeypatch.setattr(run_admission, "CONFIRMATIONS",
                        run_admission.Confirmations(admissions_path))
    monkeypatch.setattr(specialist_desk, "record_echo", lambda *a, **kw: None)
    # the run's continuation, queued and held: the Stop must drop it
    monkeypatch.setattr(pc, "CONTINUATIONS", pc.Continuations())
    held = asyncio.Event()

    async def _held_turn(pending, *, on_start):
        await held.wait()
    monkeypatch.setattr(pc, "run_continuation", _held_turn)
    stored = plugin_bindings.BINDINGS.peek(binding.ref)
    assert pc.CONTINUATIONS.request(stored, "run-x", "sweep") == "queued"
    stops: list = []

    async def _plugins():
        return [("browser", None, NS(artifact_id="1" * 64), "browser", "stop_runs")]

    async def _stopped(rp, tool, args):
        stops.append(args)
        return {"outcome": "stopped", "refs": ["run-x"]}
    monkeypatch.setattr(plugin_stop, "stop_plugins", _plugins)
    monkeypatch.setattr(plugin_stop, "call_stop", _stopped)

    def _tap(mid=7):
        return NS(data="v1|run_stop|conf-1|0", from_user=_a_user(),
                  message=NS(chat=NS(id=OPERATOR), message_id=mid),
                  answer=AsyncMock())

    on_222 = _tap()
    await a_pair.finance._on_run_stop_callback(on_222, "conf-1")
    await asyncio.gather(*a_pair.finance._turn_tasks)
    on_222.answer.assert_awaited_once_with("not for you")
    assert stops == []

    on_111 = _tap()
    await a_pair.casa._on_run_stop_callback(on_111, "conf-1")
    await asyncio.gather(*a_pair.casa._turn_tasks)
    on_111.answer.assert_awaited_once_with("stopping…")
    assert stops == [{"ref": "request:k1"}]
    sent = [n for n, _ in calls(a_pair.casa)]
    assert sent.count("edit_message_text") == 1 and sent.count("send_message") == 1
    edit = next(kw for n, kw in calls(a_pair.casa) if n == "edit_message_text")
    assert (edit["chat_id"], edit["message_id"]) == (OPERATOR, 7)
    assert calls(a_pair.finance) == []
    # the request's queued continuation went with it
    assert pc.CONTINUATIONS.pending_count() == 0
    await pc.CONTINUATIONS.drain()


# -- a route is decided once: birth, rebind, restart --------------------------


async def test_a_scheduled_job_born_on_casas_bot_keeps_it_after_its_role_gets_a_bot_and_casa_restarts(
        tmp_path, monkeypatch):
    """A scheduled job is launched for finance while finance has no bot of
    its own: it is born on Casa's bot, 111, and its record keeps that route.
    Finance is then given bot 222 and Casa restarts: the reloaded job's
    question and its start line still go out through 111."""
    import authz_grants
    import background_jobs
    import tools as tools_mod

    saved = (tools_mod._channel_manager, tools_mod._bus,
             tools_mod._specialist_registry, tools_mod._engagement_registry)
    try:
        casa = FakeEndpoint("assistant", bot_id=CASA_BOT, is_casa=True)
        alone = make_transport(casa, casa_bot_ids=MemoryBotIdStore(CASA_BOT))
        tools_mod.init_tools(channel_manager=alone, bus=MagicMock(),
                             specialist_registry=MagicMock(),
                             mcp_registry=MagicMock())
        assert alone.get("telegram").route_for("finance") == CASA_BOT

        path = tmp_path / "engagements.json"
        reg = await _a_registry(path)
        host = NS(role="finance", kind="specialist",
                  decl=NS(title="Weekly ledger"))
        monkeypatch.setattr(background_jobs, "find_job_host", lambda *a: host)
        monkeypatch.setattr(authz_grants, "_live_operator_identity",
                            lambda: (OPERATOR, OPERATOR))
        created: list = []

        async def start_on_host(_host, task, context, origin, **_kw):
            created.append(await reg.create("specialist", "finance", "in_casa",
                                            task, {**origin, "job": _a_job_state()}))
            return tools_mod._result({"status": "pending",
                                      "engagement_id": created[-1].id})

        monkeypatch.setattr(tools_mod, "_start_job_on_host", start_on_host)
        await tools_mod.start_scheduled_job("finance", NS(
            name="weekly", type="cron", job="ledger:weekly",
            task="Check the ledger.", context="", channel="telegram"))
        (rec,) = created
        assert rec.origin["_tg_bot"] == CASA_BOT
        await _a_flush(reg)

        # finance gets its own bot; Casa restarts
        casa = FakeEndpoint("assistant", bot_id=CASA_BOT, is_casa=True)
        finance = FakeEndpoint("finance", bot_id=FINANCE_BOT)
        manager = make_transport(casa, finance, casa=casa,
                                 casa_bot_ids=MemoryBotIdStore(CASA_BOT))
        transport = manager.get("telegram")
        assert transport.route_for("finance") == FINANCE_BOT
        again = await _a_registry(path)
        loaded = again.get(rec.id)
        assert loaded.origin["_tg_bot"] == CASA_BOT

        await transport.post_job_question(
            loaded, "q1", admitted("Split by week?"), ["Yes", "No"], ["Yes", "No"])
        await transport.post_job_start_line(loaded)
        assert [n for n, _ in calls(casa)] == ["post_job_question",
                                               "post_job_start_line"]
        assert calls(finance) == []
    finally:
        (tools_mod._channel_manager, tools_mod._bus,
         tools_mod._specialist_registry, tools_mod._engagement_registry) = saved


# -- the conversation is the agent's, not the bot's ---------------------------


async def test_an_agents_conversation_survives_a_change_of_its_bot_and_is_its_own(
        a_isolated, monkeypatch):
    """The session a turn resumes is keyed by the agent and the chat, never
    the bot: finance's DM through bot 222 and, after its token is replaced by
    a new bot (333), through bot 333 resume one conversation, and ``/new``
    on either resets that one; the assistant on the same chat has its own."""
    import session_registry
    from session_registry import build_scoped_session_key

    resets: list[str] = []

    async def _reset(key, registry):
        resets.append(key)
    monkeypatch.setattr(session_registry, "reset_channel", _reset)

    def _key(msg) -> str:
        # the key the agent resumes a turn under (``Agent._process``)
        return build_scoped_session_key(msg.channel, msg.target,
                                        msg.context.get("chat_id"))

    keys = {}
    for bot, role in ((FINANCE_BOT, "finance"), (333, "finance"),
                      (CASA_BOT, "assistant")):
        bus = _A_Bus()
        ep = _a_endpoint(bot, bus, role=role)
        ep._session_registry = object()
        await ep._handle(_a_text_update("hello", message_id=5), None)
        (turn,) = _a_turns(bus)
        assert turn.context["_tg_bot"] == bot
        keys[bot] = _key(turn)
        await ep._handle(_a_text_update("/new", message_id=6), None)
    assert keys[FINANCE_BOT] == keys[333] != keys[CASA_BOT]
    assert resets == [keys[FINANCE_BOT], keys[333], keys[CASA_BOT]]


# ---------------------------------------------------------------------------
# A stored address never resolves to another bot
#
# Every message Casa keeps is addressed (bot, chat, message), and only the bot
# that holds a message can edit, delete or reply to it. So when the bot a
# record names has no endpoint any more (it was removed from the
# configuration), each address-bound caller fails in its own typed way and
# never falls back to Casa's bot; and when that bot's endpoint exists but has
# not connected yet, the caller acts on that endpoint as an unready endpoint
# behaves, and still never on Casa's.
# ---------------------------------------------------------------------------

_B_ROLE_OF = {CASA_BOT: "assistant", FINANCE_BOT: "finance"}


# ---------------------------------------------------------------------------
# Real endpoints over fake bots
# ---------------------------------------------------------------------------


def _b_fake_bot(bot_id: int) -> MagicMock:
    """A Bot API stand-in recording every call, so ``calls(ep)`` lists what
    went out through this bot."""
    bot = MagicMock(name=f"bot{bot_id}")
    bot.id = bot_id
    ids = itertools.count(1000)
    bot.send_message = AsyncMock(side_effect=lambda **kw: NS(message_id=next(ids)))
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_caption = AsyncMock(return_value=True)
    bot.edit_message_reply_markup = AsyncMock(return_value=True)
    bot.delete_message = AsyncMock(return_value=True)
    bot.send_chat_action = AsyncMock(return_value=True)
    bot.send_document = AsyncMock(return_value=NS(message_id=999))
    return bot


def _b_endpoint(bot_id: int, *, ready: bool = True, role: "str | None" = None):
    """A real ``TelegramChannel`` for *bot_id*. A ready one has its
    application (a fake bot behind it); an unready one knows its id (it was
    persisted, or configured) but has not connected: no application, no
    bot."""
    from channels.telegram import TelegramChannel
    bot = _b_fake_bot(bot_id) if ready else None
    ep = TelegramChannel(
        bot_token=f"{bot_id}:token", chat_id=str(OPERATOR),
        default_agent=role or _B_ROLE_OF.get(bot_id, "assistant"), bot=bot,
        is_casa=bot_id == CASA_BOT, known_bot_id=bot_id)
    if ready:
        ep._app = MagicMock(bot=bot)
    return ep


def _b_transport(*eps, casa):
    from channels import ChannelManager
    from channels.telegram_transport import TelegramTransport
    transport = TelegramTransport(casa_bot_ids=MemoryBotIdStore(CASA_BOT))
    transport.add_endpoint(casa, casa=True)
    for ep in eps:
        if ep is not casa:
            transport.add_endpoint(ep, casa=False)
    manager = ChannelManager()
    manager.register(transport)
    return transport, manager


def _b_spy(monkeypatch, ep, name: str) -> list:
    """Record each call of *ep*'s method *name*, then run the method."""
    seen: list = []
    original = getattr(ep, name)

    async def spy(*args, **kwargs):
        seen.append((args, kwargs))
        return await original(*args, **kwargs)
    monkeypatch.setattr(ep, name, spy)
    return seen


@pytest.fixture
def b_world(monkeypatch, tmp_path):
    """Fresh stores, and a builder for the two worlds: ``"retired"`` (only
    Casa's bot 111 has an endpoint) and ``"unready"`` (bot 222's endpoint is
    registered but has not connected). Returns ``build(mode)``."""
    import background_jobs
    import plugin_bindings
    import run_admission
    import scheduled_asks
    import tools as tools_mod
    import verdict_broker
    from verdict_broker import VerdictBroker

    monkeypatch.setattr(verdict_broker, "BROKER", VerdictBroker())
    monkeypatch.setattr(scheduled_asks, "_BOOT_REVOCATIONS", [])
    monkeypatch.setattr(scheduled_asks, "_BOOT_RECONCILED", False)
    monkeypatch.setattr(scheduled_asks, "STORE", scheduled_asks.ScheduledAskStore(
        str(tmp_path / "scheduled_asks.json")))
    monkeypatch.setattr(plugin_bindings, "BINDINGS",
                        plugin_bindings.BindingStore(str(tmp_path / "bindings.json")))
    monkeypatch.setattr(run_admission, "CONFIRMATIONS",
                        run_admission.Confirmations(str(tmp_path / "admissions.json")))
    monkeypatch.setattr(background_jobs, "JOB_PAD_ROOT", tmp_path / "pads")

    def build(mode: str):
        casa = _b_endpoint(CASA_BOT)
        eps = [casa]
        finance = None
        if mode == "unready":
            finance = _b_endpoint(FINANCE_BOT, ready=False)
            eps.append(finance)
        transport, manager = _b_transport(*eps, casa=casa)
        monkeypatch.setattr(tools_mod, "_channel_manager", manager, raising=False)
        return NS(transport=transport, manager=manager, casa=casa,
                     finance=finance, tmp=tmp_path, monkeypatch=monkeypatch)
    return build


# ---------------------------------------------------------------------------
# The seven address-bound callers, each acting on a record addressed to 222
# ---------------------------------------------------------------------------


def _b_job(eid: str = "j" * 32, *, route=FINANCE_BOT):
    origin = {"role": "assistant", "channel": "telegram", "chat_id": OPERATOR,
              "user_id": OPERATOR, "job": {"title": "Accounting check"}}
    if route is not None:
        origin["_tg_bot"] = route
    return NS(id=eid, origin=origin, status="active", role_or_type="finance",
                 completed_at=None, ui_posts=[], questions={})


async def _b_job_question(h, mode):
    """A job whose route is bot 222 asks a question."""
    rec = _b_job()
    if mode == "retired":
        with pytest.raises(EndpointUnavailable) as err:
            await h.transport.post_job_question(
                rec, "q1", admitted("Which account?"), ["A"], ["A"])
        assert err.value.bot_id == FINANCE_BOT
        return
    seen = _b_spy(h.monkeypatch, h.finance, "post_job_question")
    assert await h.transport.post_job_question(
        rec, "q1", admitted("Which account?"), ["A"], ["A"]) is None
    assert len(seen) == 1


async def _b_job_keyboards(h, mode):
    """An ended job whose question keyboard was posted on bot 222 has its
    keyboards retired."""
    import asyncio

    from engagement_registry import JOB_POST_QUESTION, EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(h.tmp / "engagements.json"))
    await reg.load()
    h.casa._engagement_registry = reg
    origin = dict(_b_job().origin)
    rec = await reg.create("specialist", "finance", "in_casa", "a", origin)
    address = TgAddress(FINANCE_BOT, OPERATOR, 5)
    assert reg.record_job_post(rec.id, address, keyboard=JOB_POST_QUESTION)
    assert await reg.try_transition_terminal(rec.id, "completed", strict=True)
    rec = reg.get(rec.id)
    seen = (_b_spy(h.monkeypatch, h.finance, "_retire_job_posts")
            if mode == "unready" else [])
    if mode == "retired":
        with pytest.raises(EndpointUnavailable) as err:
            h.transport.retire_job_keyboards(rec)
        assert err.value.bot_id == FINANCE_BOT
    else:
        h.transport.retire_job_keyboards(rec)
    await asyncio.gather(*list(h.transport._job_ui_tasks))
    # each post is retired once, by its owner: it is off the record either way
    assert reg.take_ui_posts(rec.id) == []
    if mode == "unready":
        (args, _kw), = seen
        assert args[1] == [(address, JOB_POST_QUESTION)]


class _B_Journal:
    """The job driver's journal settle: hands each lost message to the
    teller, keeping what the teller raised."""

    def __init__(self, entries: list) -> None:
        self.entries = entries
        self.raised: list = []

    async def settle_journal_terminal(self, engagement, tell) -> int:
        for entry in self.entries:
            try:
                await tell(engagement, entry)
            except Exception as exc:  # noqa: BLE001 — recorded for the test
                self.raised.append(exc)
        return len(self.entries)


async def _b_journal_notice(h, mode):
    """A job ends with a message the operator sent it on bot 222 unread: the
    lost-message notice is told in reply to it, through 222 only."""
    import tools as tools_mod
    entry = {"uuid": "u1", "text": "and the receipts?",
             "notice": {"address": [FINANCE_BOT, OPERATOR, 9], "answer": False}}
    driver = _B_Journal([entry])
    seen = (_b_spy(h.monkeypatch, h.finance, "tell_journal_lost")
            if mode == "unready" else [])
    await tools_mod._settle_job_journal(_b_job(), driver)
    if mode == "retired":
        (exc,) = driver.raised
        assert isinstance(exc, EndpointUnavailable) and exc.bot_id == FINANCE_BOT
    else:
        assert driver.raised == []
        assert len(seen) == 1


def _b_ask_record(**over) -> dict:
    from channels.tg_address import scope
    import scheduled_asks
    rec = {"rid": "rid-222", "state": scheduled_asks.STATE_LIVE,
           "role": "finance", "session_scope": "cron-x",
           "scope": scope("dm", FINANCE_BOT, OPERATOR), "bot_id": FINANCE_BOT,
           "chat_id": OPERATOR, "operator_id": OPERATOR, "message_id": 77,
           "options": ["Yes", "No"], "body": "Pay it?", "epoch": None,
           "created_at": 0.0, "expires_at": 1.0}
    rec.update(over)
    return rec


async def _b_scheduled_ask(h, mode):
    """At boot, a question asked on bot 222 that settled before the crash
    (its keyboard edit replayed) and one that expired while Casa was down
    (settled: edited and its session told)."""
    import scheduled_asks
    await scheduled_asks.STORE.put(_b_ask_record(
        rid="settled", state=scheduled_asks.STATE_SETTLING,
        terminal_edit="Pay it?\n\nAnswered: Yes", message_id=76))
    await scheduled_asks.STORE.put(_b_ask_record(rid="expired"))
    casa_turns = _b_spy(h.monkeypatch, h.casa, "_dispatch_scheduled_continuation")
    if mode == "unready":
        edits = _b_spy(h.monkeypatch, h.finance, "edit_dm_message")
        turns = _b_spy(h.monkeypatch, h.finance, "_dispatch_scheduled_continuation")
    counts = await scheduled_asks.reconcile_at_boot(h.transport, now=5.0)
    assert scheduled_asks.STORE.all() == []
    assert casa_turns == []
    if mode == "retired":
        assert counts["no_endpoint"] == 2
        assert counts["settled_before_crash"] == counts["expired"] == 0
    else:
        assert counts["settled_before_crash"] == counts["expired"] == 1
        assert sorted(args[:2] for args, _kw in edits) == [
            (OPERATOR, 76), (OPERATOR, 77)]
        assert [kw["request_id"] for _a, kw in turns] == ["expired"]


async def _b_run_card(h, mode):
    """A Stop marks the run card the plugin's request was admitted with,
    posted on bot 222."""
    import plugin_bindings
    import plugin_stop
    import run_admission
    from authz_grants import GrantIdentity
    ident = GrantIdentity(operator_id=OPERATOR, chat_id=OPERATOR,
                          enforcement_role="browser", artifact_id="artifact-1",
                          engagement_id="", bot_id=FINANCE_BOT)
    binding = plugin_bindings.BINDINGS.for_turn(
        plugin="browser", turn_key="d-run", identity=ident,
        chain=("finance", "browser"),
        origin={"role": "finance", "channel": "telegram", "chat_id": OPERATOR,
                "user_id": OPERATOR, "_tg_bot": FINANCE_BOT})
    c = run_admission.Confirmation(
        cid="conf-222", plugin="browser", bot_id=FINANCE_BOT, operator_id=OPERATOR,
        binding_ref=binding.ref, request_key="k1", status="admitted", admissions=[],
        created_at=time.time(), card=[FINANCE_BOT, OPERATOR, 700],
        card_text="Browsing now:")
    run_admission.CONFIRMATIONS.add(c)
    if mode == "retired":
        with pytest.raises(EndpointUnavailable) as err:
            await plugin_stop.settle_run_card(c, plugin_stop.MARKS["stopped"])
        assert err.value.bot_id == FINANCE_BOT
        return
    edits = _b_spy(h.monkeypatch, h.finance, "edit_dm_message")
    assert await plugin_stop.settle_run_card(c, plugin_stop.MARKS["stopped"]) is False
    assert [args[:2] for args, _kw in edits] == [(OPERATOR, 700)]


async def _b_step_line(h, mode, caplog=None):
    """At boot, the step line of a configurator run a stop cut off, posted on
    bot 222, is deleted through the bot that posted it, as Casa's boot does;
    the operator is told about the run either way."""
    import casa_core
    import configurator_run
    # the boot's own delete: the step line's endpoint, never another
    assert "tg_transport.endpoint(addr).delete_dm_message(" in inspect.getsource(
        casa_core)

    async def delete(addr):
        return await h.transport.endpoint(addr).delete_dm_message(
            addr.chat_id, addr.message_id)

    told: list = []

    async def notify(text):
        told.append(text)

    deletes = (_b_spy(h.monkeypatch, h.finance, "delete_dm_message")
               if mode == "unready" else [])
    with caplog.at_level(logging.INFO, logger="configurator_run"):
        await configurator_run.announce_interrupted(
            {"step_line": [FINANCE_BOT, OPERATOR, 9], "task": "set up finance",
             "run_key": ""},
            recovered_ids=set(), notify=notify, delete=delete)
    assert len(told) == 1
    if mode == "retired":
        assert "not deleted: EndpointUnavailable" in caplog.text
    else:
        assert [args for args, _kw in deletes] == [(OPERATOR, 9)]


async def _b_proposal_edit(h, mode):
    """A tapped proposal on bot 222 is edited in place to its next card."""
    import result_broker
    at = TgAddress(FINANCE_BOT, OPERATOR, 5)
    if mode == "retired":
        with pytest.raises(EndpointUnavailable) as err:
            await result_broker._edit_operator_proposal(at, "Next?", ["A"], "rid-1")
        assert err.value.bot_id == FINANCE_BOT
        return
    seen = _b_spy(h.monkeypatch, h.finance, "replace_operator_proposal")
    assert await result_broker._edit_operator_proposal(at, "Next?", ["A"], "rid-1") is None
    assert [args[:2] for args, _kw in seen] == [(OPERATOR, 5)]


async def _b_capability_delivery(h, mode):
    """A plugin's message for the operator, for a call whose grant identity
    is on bot 222."""
    import result_broker
    to = TgAddress(FINANCE_BOT, OPERATOR)
    if mode == "retired":
        with pytest.raises(EndpointUnavailable) as err:
            await result_broker._post_operator_message(to, "Finance\nYour invoice")
        assert err.value.bot_id == FINANCE_BOT
        return
    seen = _b_spy(h.monkeypatch, h.finance, "deliver_operator_message")
    assert await result_broker._post_operator_message(
        to, "Finance\nYour invoice") is DeliveryOutcome.NOT_DELIVERED
    assert [args[:1] for args, _kw in seen] == [(OPERATOR,)]


_B_CALLERS = {
    "job_question": _b_job_question,
    "job_keyboards": _b_job_keyboards,
    "journal_notice": _b_journal_notice,
    "scheduled_ask_boot": _b_scheduled_ask,
    "run_card": _b_run_card,
    "step_line_delete": _b_step_line,
    "proposal_edit": _b_proposal_edit,
    "capability_delivery": _b_capability_delivery,
}


@pytest.mark.parametrize("caller", sorted(_B_CALLERS))
async def test_a_record_on_a_retired_bot_fails_typed_and_nothing_reaches_casas_bot(
        b_world, caplog, caller):
    h = b_world("retired")
    run = _B_CALLERS[caller]
    if caller == "step_line_delete":
        await run(h, "retired", caplog)
    else:
        await run(h, "retired")
    assert calls(h.casa) == []


@pytest.mark.parametrize("caller", sorted(_B_CALLERS))
async def test_a_record_on_a_bot_not_yet_connected_waits_on_that_bot_never_casas(
        b_world, caplog, caller):
    h = b_world("unready")
    run = _B_CALLERS[caller]
    if caller == "step_line_delete":
        await run(h, "unready", caplog)
    else:
        await run(h, "unready")
    assert calls(h.casa) == []
    assert calls(h.finance) == []


# ---------------------------------------------------------------------------
# One endpoint per bot and one per role
# ---------------------------------------------------------------------------


def test_two_endpoints_for_one_bot_are_refused():
    from channels.telegram_transport import TelegramTransport
    casa = _b_endpoint(CASA_BOT)
    twin = _b_endpoint(CASA_BOT, role="finance")
    transport = TelegramTransport(casa_bot_ids=MemoryBotIdStore(CASA_BOT))
    transport.add_endpoint(casa, casa=True)
    with pytest.raises(DuplicateEndpoint) as err:
        transport.add_endpoint(twin, casa=False)
    assert err.value.bot_id == CASA_BOT
    assert transport.endpoint(CASA_BOT) is casa
    assert transport.route_for("finance") == CASA_BOT       # finance has no bot of its own


def test_two_endpoints_for_one_role_are_refused():
    from channels.telegram_transport import TelegramTransport
    casa = _b_endpoint(CASA_BOT)
    finance = _b_endpoint(FINANCE_BOT)
    other = _b_endpoint(333, role="finance")
    transport = TelegramTransport(casa_bot_ids=MemoryBotIdStore(CASA_BOT))
    transport.add_endpoint(casa, casa=True)
    transport.add_endpoint(finance, casa=False)
    with pytest.raises(ValueError, match="finance"):
        transport.add_endpoint(other, casa=False)
    assert transport.route_for("finance") == FINANCE_BOT
    with pytest.raises(EndpointUnavailable):
        transport.endpoint(333)


def _b_application(bot_id: int):
    app = MagicMock()
    for name in ("initialize", "start", "stop", "shutdown"):
        setattr(app, name, AsyncMock())
    app.bot = _b_fake_bot(bot_id)
    app.bot.set_webhook = AsyncMock()
    app.bot.delete_webhook = AsyncMock()
    app.updater = MagicMock(start_polling=AsyncMock(), stop=AsyncMock())
    return app


async def test_a_token_that_turns_out_to_be_another_endpoints_bot_stays_stopped(
        monkeypatch):
    """Bot 222's endpoint is configured with a token whose ``getMe`` answers
    111, a bot Casa's endpoint already serves: that endpoint is stopped and
    stays unready, every bot id still names its own endpoint, and Casa's
    bot keeps answering its turns."""
    from channels.telegram import TelegramChannel
    from telegram.ext import Application
    apps: list = []

    def builder():
        chain = MagicMock()
        chain.token = MagicMock(return_value=chain)

        def build():
            apps.append(_b_application(CASA_BOT))
            return apps[-1]
        chain.build = build
        return chain
    monkeypatch.setattr(Application, "builder", builder)

    casa = _b_endpoint(CASA_BOT)
    finance = TelegramChannel(bot_token="222:token", chat_id=str(OPERATOR),
                              default_agent="finance", is_casa=False,
                              known_bot_id=FINANCE_BOT)
    transport, _manager = _b_transport(casa, finance, casa=casa)

    await finance._rebuild()

    assert not finance.is_ready
    assert apps[0].shutdown.await_count == 1
    assert transport.endpoint(CASA_BOT) is casa
    assert transport.endpoint(FINANCE_BOT) is finance
    out = await transport.send_response(admitted("still here"), {
        "_tg_bot": CASA_BOT, "chat_id": OPERATOR, "cid": "c1"})
    assert out is DeliveryOutcome.DELIVERED
    assert [kw["text"] for name, kw in calls(casa) if name == "send_message"] == [
        "still here"]
    assert apps[0].bot.mock_calls == []
    await finance._rebuild()            # a reconnect never revives it
    assert len(apps) == 1 and not finance.is_ready


# ---------------------------------------------------------------------------
# Route-bound delivery never guesses a bot
# ---------------------------------------------------------------------------


def _b_route_bound(transport, ctx):
    """Each route-bound method of the transport, called with *ctx* as its
    delivery context (a job method: as the job's origin)."""
    rec = _b_job(route=None)
    rec.origin = {**{k: v for k, v in rec.origin.items() if k != "chat_id"}, **ctx}
    return {
        "send": lambda: transport.send("hi", ctx),
        "send_media": lambda: transport.send_media(b"x", "document", "a.pdf", ctx),
        "send_response": lambda: transport.send_response(admitted("hi"), ctx),
        "finalize_response_stream": lambda: transport.finalize_response_stream(
            "hi", ctx, None),
        "turn_finished": lambda: transport.turn_finished(ctx),
        "create_on_token": lambda: transport.create_on_token(ctx),
        "finalize_stream": lambda: transport.finalize_stream("hi", ctx, None),
        "post_job_start_line": lambda: transport.post_job_start_line(rec),
        "post_job_parked": lambda: transport.post_job_parked(rec, "parked"),
        "post_job_question": lambda: transport.post_job_question(
            rec, "q1", admitted("Which?"), ["A"], ["A"]),
    }


_B_METHODS = sorted(_b_route_bound(None, {}))


async def _b_call(fn):
    result = fn()
    if inspect.isawaitable(result):
        return await result
    return result


@pytest.mark.parametrize("ctx", [{}, {"cid": "x"}, {"chat_id": OPERATOR}],
                         ids=["empty", "cid_only", "chat_only"])
@pytest.mark.parametrize("method", _B_METHODS)
async def test_a_context_without_a_route_is_refused_by_every_route_bound_method(
        method, ctx):
    casa, finance = _b_endpoint(CASA_BOT), _b_endpoint(FINANCE_BOT)
    transport, _ = _b_transport(casa, finance, casa=casa)
    with pytest.raises(RouteMissing):
        await _b_call(_b_route_bound(transport, ctx)[method])
    assert calls(casa) == [] and calls(finance) == []


@pytest.mark.parametrize("route", [None, "x", True, 0, "111"])
@pytest.mark.parametrize("method", _B_METHODS)
async def test_a_malformed_route_is_refused_by_every_route_bound_method(method, route):
    casa, finance = _b_endpoint(CASA_BOT), _b_endpoint(FINANCE_BOT)
    transport, _ = _b_transport(casa, finance, casa=casa)
    ctx = {"_tg_bot": route, "chat_id": OPERATOR, "cid": "x"}
    with pytest.raises(AddressError):
        await _b_call(_b_route_bound(transport, ctx)[method])
    assert calls(casa) == [] and calls(finance) == []


# ---------------------------------------------------------------------------
# Casa's own cards and notices go through Casa's bot
#
# Every case runs inside a finance turn that arrived on bot 222 (its origin
# carries ``_tg_bot: 222``), so a site that resolved its endpoint from the
# turn's route or from the role would post on 222. Each of Casa's consent
# cards, install, persona and erase questions, the wipe card, the restart
# card, plugin-health, config-sync and placeholder notices, the limit line,
# setup dispatch, ``operator_notify`` and the scheduled job's "did not start"
# line must make Bot API calls on 111 only, in the operator's chat there. The
# pre-boot maintenance sender runs before any endpoint exists: it posts with
# the configured bot token, which is Casa's.
# ---------------------------------------------------------------------------

_C_ROLE = {CASA_BOT: "assistant", FINANCE_BOT: "finance"}


def _c_fake_bot(bot_id: int) -> MagicMock:
    """A Bot API stand-in; every call is recorded on the mock, so
    ``calls(ep)`` lists what went out through this bot."""
    bot = MagicMock(name=f"bot{bot_id}")
    bot.id = bot_id
    ids = itertools.count(1000)
    bot.send_message = AsyncMock(
        side_effect=lambda **kw: NS(message_id=next(ids)))
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_reply_markup = AsyncMock(return_value=True)
    bot.send_chat_action = AsyncMock(return_value=True)
    bot.delete_message = AsyncMock(return_value=True)
    return bot


class _CBus:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, msg) -> None:
        self.sent.append(msg)

    async def send_checked(self, msg) -> str:
        self.sent.append(msg)
        return "accepted"

    async def notify(self, msg) -> None:
        self.sent.append(msg)


def _c_endpoint(bot_id: int, bus: _CBus):
    from channels.telegram import TelegramChannel
    bot = _c_fake_bot(bot_id)
    ep = TelegramChannel(
        bot_token=f"{bot_id}:token", chat_id=str(OPERATOR),
        default_agent=_C_ROLE[bot_id], bus=bus, bot=bot,
        is_casa=bot_id == CASA_BOT, known_bot_id=bot_id)
    ep._app = MagicMock(bot=bot)
    return ep


class _CChallenges:
    """The real challenge coordinator, keeping each handle so a test can
    wait for the card to be posted."""

    def __init__(self) -> None:
        import authz_grants
        self._inner = authz_grants.ChallengeCoordinator()
        self.handles: list = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def register_challenge(self, key, **kw):
        handle = self._inner.register_challenge(key, **kw)
        self.handles.append(handle)
        return handle

    async def posted(self) -> list[str]:
        return [await h.settled_post() for h in self.handles]


#: The turn every case runs in: the operator wrote to finance on bot 222.
_C_TURN_ON_222 = {
    "role": "finance", "execution_role": "finance", "channel": "telegram",
    "chat_id": str(OPERATOR), "user_id": OPERATOR, "cid": "turn-222",
    "message_type": "channel_in", "source": "telegram", "_tg_bot": FINANCE_BOT,
}


@pytest.fixture
def c_pair(monkeypatch):
    import agent as agent_mod
    import authz_grants
    import tools as tools_mod
    import verdict_broker
    from telegram_channel_double import make_transport
    from verdict_broker import VerdictBroker

    bus = _CBus()
    casa, finance = _c_endpoint(CASA_BOT, bus), _c_endpoint(FINANCE_BOT, bus)
    manager = make_transport(casa, finance, casa=casa)
    transport = manager.get("telegram")
    assert transport.route_for("finance") == FINANCE_BOT
    monkeypatch.setattr(tools_mod, "_channel_manager", manager, raising=False)
    monkeypatch.setattr(verdict_broker, "BROKER", VerdictBroker())
    challenges = _CChallenges()
    monkeypatch.setattr(authz_grants, "CHALLENGES", challenges)
    monkeypatch.setattr(tools_mod, "CHALLENGES", challenges, raising=False)
    monkeypatch.setattr(agent_mod, "active_session_registry", MagicMock(),
                        raising=False)
    token = agent_mod.origin_var.set(dict(_C_TURN_ON_222))
    yield NS(casa=casa, finance=finance, manager=manager,
                transport=transport, bus=bus, challenges=challenges,
                monkeypatch=monkeypatch)
    agent_mod.origin_var.reset(token)


def _c_main_closure(name: str, end: str, **names):
    """A closure ``casa_core.main`` defines (the bus target for Casa's
    notices, the setup dispatcher), compiled from main's own source over
    casa_core's globals plus *names*, so the code exercised is the code that
    runs."""
    import casa_core
    src = inspect.getsource(casa_core.main)
    start = src.index(f"    async def {name}(")
    stop = src.index(end, start)
    ns = dict(vars(casa_core))
    ns.update(names)
    exec(compile(textwrap.dedent(src[start:stop]), casa_core.__file__, "exec"), ns)
    return ns[name]


# ---------------------------------------------------------------------------
# The sites, each driven through its own production function
# ---------------------------------------------------------------------------


async def _c_trigger_card(h):
    import trigger_reconcile
    trigger_reconcile._fire_consent_prompts(
        [{"plugin": "lantern", "artifact_id": "a" * 64, "effective": "lantern-hook",
          "target": "resident:finance",
          "auth": {"mode": "hmac", "header": "X-Sig"}}],
        trigger_registry=MagicMock(), role_configs={},
        channel_manager=h.manager, acks=MagicMock(), secrets_dir=None,
        resolver=None, global_secret_ok=None, nonce_by_identity={})
    assert await h.challenges.posted() == ["posted"]


async def _c_event_card(h):
    import event_reconcile
    event_reconcile._fire_consent_prompts(
        [_c_event_row()], role_configs={}, channel_manager=h.manager,
        acks=MagicMock(), resolver=None, entries=None)
    assert await h.challenges.posted() == ["posted"]


def _c_event_row() -> dict:
    return {"subscriber": "ledger", "artifact_id": "b" * 64, "emitter": "bank",
            "event": "payment", "digest": "d" * 16,
            "targets": ["resident:finance"], "identity": "event-identity"}


def _c_callback_row() -> dict:
    return {"plugin": "lantern", "artifact_id": "c" * 64, "declared": "oauth",
            "effective": "lantern-oauth", "declaration_digest": "e" * 16,
            "identity": "callback-identity"}


async def _c_callback_card(h):
    import callback_reconcile
    callback_reconcile._fire_consent_prompts(
        [_c_callback_row()], trigger_registry=MagicMock(), role_configs={},
        channel_manager=h.manager, acks=MagicMock(), spool=MagicMock(),
        resolver=None, entries=None, nonce_by_identity={})
    assert await h.challenges.posted() == ["posted"]


def _c_no_denials(h):
    import consent_denials
    import plugin_setup_episodes
    h.monkeypatch.setattr(consent_denials, "denied", lambda key: False)
    h.monkeypatch.setattr(plugin_setup_episodes, "open_member_nonce",
                          lambda plugin, identity: "")


def _c_acks() -> MagicMock:
    acks = MagicMock()
    acks.get = MagicMock(return_value=None)
    return acks


async def _c_trigger_reprompt(h):
    import trigger_reconcile
    _c_no_denials(h)
    row = {"plugin": "lantern", "artifact_id": "a" * 64, "effective": "lantern-hook",
           "target": "resident:finance", "auth": {"mode": "hmac", "header": "X-Sig"}}
    h.monkeypatch.setattr(trigger_reconcile, "compute_desired",
                          lambda **kw: NS(pending=[row]))
    report: list = []
    await trigger_reconcile.reprompt_pending(
        NS(channel_manager=h.manager, role_configs={}), report=report,
        acks=_c_acks())
    assert [r.get("status") for r in report] == [None]
    assert await h.challenges.posted() == ["posted"]


async def _c_event_reprompt(h):
    import event_reconcile
    _c_no_denials(h)
    h.monkeypatch.setattr(event_reconcile, "compute_desired",
                          lambda **kw: NS(consent_needed=[_c_event_row()]))
    report: list = []
    await event_reconcile.reprompt_pending(
        NS(channel_manager=h.manager, role_configs={}), report=report,
        acks=_c_acks())
    assert [r.get("status") for r in report] == [None]
    assert await h.challenges.posted() == ["posted"]


async def _c_callback_reprompt(h):
    import callback_reconcile
    _c_no_denials(h)
    h.monkeypatch.setattr(callback_reconcile, "compute_desired",
                          lambda **kw: NS(pending=[_c_callback_row()]))
    report: list = []
    await callback_reconcile.reprompt_pending(
        NS(channel_manager=h.manager, role_configs={}), report=report,
        acks=_c_acks())
    assert [r.get("status") for r in report] == [None]
    assert await h.challenges.posted() == ["posted"]


def _c_payload(result) -> dict:
    return json.loads(result["content"][0]["text"])


async def _c_specialist_install_card(h, tmp_path):
    import specialist_install
    import specialist_install_consent as sic
    import tools as tools_mod
    staged = NS(
        component_id="ledger-specialist", version="1.0.0", slug="ledger",
        component_checksum="f" * 64, root_digest="0" * 64, mission="Books.",
        default_persona_ref="persona:ledger@1", required_config_names=(),
        required_secret_names=(), dependencies=(), staged_dir=tmp_path,
        resolved_ref="v1.0.0", receipt_id="r-1", receipt_digest="1" * 64,
        plugin_resolutions=())
    h.monkeypatch.setattr(specialist_install, "inspect_specialist_repo",
                          lambda *a, **kw: staged)
    h.monkeypatch.setattr(sic.SpecialistInstallAckStore, "is_acked",
                          lambda self, identity: False)
    payload = _c_payload(await tools_mod.specialist_install_inspect.handler(
        {"repo": "https://example.invalid/ledger.git", "ref": "v1.0.0"}))
    assert payload.get("consent") == "keyboard_posted", payload


async def _c_persona_install_card(h, tmp_path):
    import persona_install
    import tools as tools_mod
    staged = NS(persona_id="ledger", version="1.0.0", checksum="9" * 64,
                   display_name="Ledger", staged_dir=tmp_path,
                   resolved_ref="v1.0.0")
    h.monkeypatch.setattr(persona_install, "inspect_persona_repo",
                          lambda *a, **kw: staged)
    h.monkeypatch.setattr(persona_install.PersonaInstallAckStore, "is_acked",
                          lambda self, identity: False)
    payload = _c_payload(await tools_mod.persona_install_inspect.handler(
        {"repo": "https://example.invalid/persona.git", "ref": "v1.0.0"}))
    assert payload.get("consent") == "keyboard_posted", payload


async def _c_erase_card(h, tmp_path):
    import plugin_erase_consent as pec
    import plugin_erasure as pe
    import tools as tools_mod
    h.monkeypatch.setattr(pec, "CHOICES", pec.ChoiceGrants())
    h.monkeypatch.setattr(pe, "QUESTIONS", pe.QuestionIds())
    spec = pe.EraseSpec(name="probe", artifact_id="e" * 64,
                        targets=("specialist:finance",),
                        tool_names=("mcp__plugin_probe_srv__erase_all",),
                        protected=False, tool="erase_all", summary=None)
    payload, reports = await tools_mod._erase_gate(
        tool="plugin_remove", arg="name", name="probe", subject="plugin:probe",
        what="the plugin probe", specs=[spec], erase=None)
    assert payload["kind"] == "erase_choice_pending", payload
    assert reports == []


async def _c_wipe_card(h, tmp_path):
    import tools as tools_mod
    from memory import maintenance
    h.monkeypatch.setattr(maintenance, "INTENT_PATH",
                          str(tmp_path / "memory" / "maintenance.json"))
    payload = _c_payload(await tools_mod.wipe_memory.handler({}))
    assert payload["status"] == "awaiting_user", payload


async def _c_restart_card(h, tmp_path):
    import configurator_run
    configurator_run.note_restart("run-1", "add the ledger specialist")
    assert await configurator_run.offer_restart("run-1") is True


async def _c_plugin_health(h, tmp_path):
    import casa_core
    import plugin_health
    import tools as tools_mod
    report = {"generation": 1, "issues": [
        {"fingerprint": "fp-1", "name": "lantern", "reason_code": "crashed"}],
        "warnings": []}
    marked: list = []
    h.monkeypatch.setattr(plugin_health, "load_report", lambda path: report)
    h.monkeypatch.setattr(plugin_health, "new_fingerprints", lambda r: ["fp-1"])
    h.monkeypatch.setattr(plugin_health, "render_line",
                          lambda entries, limit: "lantern stopped working.")
    h.monkeypatch.setattr(plugin_health, "mark_notified",
                          lambda fps, path, generation: marked.append(fps))
    h.monkeypatch.setattr(tools_mod, "plugins_under_live_install", lambda: set())
    await casa_core.notify_plugin_health(h.manager, path=str(tmp_path / "h.json"))
    assert marked == [["fp-1"]]


async def _c_config_sync(h, tmp_path):
    from bus import BusMessage, MessageType
    outbound = _c_main_closure("_telegram_outbound", 'bus.register("telegram"',
                               channel_manager=h.manager)
    await outbound(BusMessage(
        type=MessageType.NOTIFICATION, source="config_sync", target="telegram",
        content="Configuration synced.", channel="telegram",
        context=dict(_C_TURN_ON_222)))


async def _c_limit_line(h, tmp_path):
    import agent as agent_mod
    from bus import BusMessage, MessageType
    stub = NS(_channel_manager=h.manager, config=NS(role="finance"))
    msg = BusMessage(type=MessageType.REQUEST, source="voice", target="finance",
                     content="reconcile", channel="voice",
                     context={"chat_id": "kitchen", "cid": "v-1"})
    await agent_mod.Agent._send_limit_line(stub, msg, None, "operator", True)


async def _c_setup_dispatch(h, tmp_path):
    dispatch = _c_main_closure("_setup_dispatch", "    async def _setup_notify(",
                               channel_manager=h.manager, bus=h.bus)
    assert await dispatch("finance", "Run lantern's setup.",
                          {"_plugin_setup": "lantern"}) is True
    (turn,) = h.bus.sent
    assert turn.context["_tg_bot"] == CASA_BOT
    from output_boundary_testing import admitted
    await h.transport.send_response(admitted("Lantern is set up."), turn.context)


async def _c_operator_notify(h, tmp_path):
    import casa_core
    await casa_core.operator_notify(h.manager, "An episode gave up.")


async def _c_placeholder_rewrites(h, tmp_path):
    import casa_core
    import reminders
    cleared: list = []
    h.monkeypatch.setattr(casa_core, "_placeholder_notified", set())
    h.monkeypatch.setattr(reminders, "peek_placeholder_notices",
                          lambda: ["/config/triggers.yaml"])
    h.monkeypatch.setattr(reminders, "clear_placeholder_notice", cleared.append)
    await casa_core.notify_placeholder_rewrites(h.manager)
    assert cleared == ["/config/triggers.yaml"]


async def _c_job_did_not_start(h, tmp_path):
    import background_jobs
    import tools as tools_mod
    h.monkeypatch.setattr(background_jobs, "find_job_host",
                          lambda job, role, delegates: None)
    trig = NS(name="month-end", job="close-books", task="", context="",
                 channel="telegram", type="cron")
    await tools_mod.start_scheduled_job("finance", trig)


_C_SITES = {
    "trigger consent card": _c_trigger_card,
    "event consent card": _c_event_card,
    "callback consent card": _c_callback_card,
    "trigger consent asked again": _c_trigger_reprompt,
    "event consent asked again": _c_event_reprompt,
    "callback consent asked again": _c_callback_reprompt,
    "specialist install card": _c_specialist_install_card,
    "persona install card": _c_persona_install_card,
    "erase-data question": _c_erase_card,
    "memory wipe card": _c_wipe_card,
    "restart card": _c_restart_card,
    "plugin health notice": _c_plugin_health,
    "config sync notice": _c_config_sync,
    "turn limit line": _c_limit_line,
    "setup dispatch turn": _c_setup_dispatch,
    "operator notice": _c_operator_notify,
    "placeholder rewrite notice": _c_placeholder_rewrites,
    "scheduled job did not start": _c_job_did_not_start,
}

_C_NO_TURN_ARG = {_c_trigger_card, _c_event_card, _c_callback_card,
                  _c_trigger_reprompt, _c_event_reprompt, _c_callback_reprompt}


@pytest.mark.parametrize("site", sorted(_C_SITES))
async def test_casas_own_cards_and_notices_post_only_on_casas_bot(
        c_pair, tmp_path, site):
    """The site runs inside a finance turn that arrived on bot 222; what it
    posts goes to the operator's chat on Casa's bot 111, and bot 222 makes
    no Bot API call at all."""
    run = _C_SITES[site]
    if run in _C_NO_TURN_ARG:
        await run(c_pair)
    else:
        await run(c_pair, tmp_path)
    await asyncio.sleep(0)
    on_111 = [(name, kw) for name, kw in calls(c_pair.casa)
              if name in ("send_message", "send_document")]
    assert on_111, f"{site}: nothing was posted on Casa's bot"
    assert {kw.get("chat_id") for _n, kw in on_111} == {OPERATOR}
    assert calls(c_pair.finance) == []


# ---------------------------------------------------------------------------
# Before any endpoint exists: the maintenance sender
# ---------------------------------------------------------------------------


class _CResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _CSession:
    posts: list = []

    def __init__(self, *a, **kw) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json=None):
        _CSession.posts.append((url, json))
        return _CResponse()


async def test_the_maintenance_line_before_boot_is_sent_with_casas_own_token(
        monkeypatch):
    """Memory maintenance runs before Casa builds any Telegram endpoint, so
    its operator line is one raw ``sendMessage`` with the configured token:
    Casa's bot, the operator's chat, nothing else."""
    import aiohttp
    from memory import maintenance
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", f"{CASA_BOT}:token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(OPERATOR))
    monkeypatch.delenv("TELEGRAM_BOT_API_BASE", raising=False)
    monkeypatch.setattr(_CSession, "posts", [])
    monkeypatch.setattr(aiohttp, "ClientSession", _CSession)
    assert await maintenance.send_operator_line("Memory is being rebuilt.") is True
    assert _CSession.posts == [(
        f"https://api.telegram.org/bot{CASA_BOT}:token/sendMessage",
        {"chat_id": str(OPERATOR), "text": "Memory is being rebuilt."})]


# ---------------------------------------------------------------------------
# Turns born before Casa's bot is ready
#
# Casa's endpoint is a real TelegramChannel, registered in the transport the
# way ``main`` builds it, over a fake Bot API, and not yet initialized. Six
# ingresses create a turn each: /invoke, a default webhook, voice over SSE,
# voice over WebSocket, a trigger and a plugin-setup dispatch. Three boots:
#
# * a normal reboot — the bot id Casa verified last time is stored for the
#   configured token, so every turn is born with the integer route;
# * a token rotation — the stored id belongs to the previous token and is not
#   trusted, so every turn is born with the route CASA (Casa's endpoint,
#   whatever id it verifies);
# * a fresh install — nothing is stored; as for a rotation.
#
# Until the bot connects, every method behaves as an unready channel always
# has: nothing raises for want of a route. Once its getMe returns the id,
# every turn, and every record persisted from one, delivers once through it.
# ---------------------------------------------------------------------------

_D_TOKEN = "111:the-current-token"
_D_OLD_TOKEN = "111:the-previous-token"
_D_WEBHOOK_SECRET = "d-webhook-secret"
_D_VOICE_ROLE = "butler"
_D_CASES = ("reboot", "rotation", "fresh_install")
_D_UNKNOWN_ID = ("rotation", "fresh_install")


def _d_fake_application(bot_id: int) -> MagicMock:
    """A PTB Application stand-in whose ``getMe`` (``initialize``) verifies
    *bot_id*; every Bot API call is recorded on ``app.bot``."""
    ids = itertools.count(5000)
    bot = MagicMock(name=f"bot{bot_id}")
    bot.id = bot_id
    bot.send_message = AsyncMock(side_effect=lambda **kw: NS(message_id=next(ids)))
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_reply_markup = AsyncMock(return_value=True)
    bot.send_chat_action = AsyncMock(return_value=True)
    bot.send_document = AsyncMock(return_value=NS(message_id=next(ids)))
    bot.set_webhook = AsyncMock()
    bot.delete_webhook = AsyncMock()
    app = MagicMock()
    for name in ("initialize", "start", "stop", "shutdown"):
        setattr(app, name, AsyncMock())
    app.bot = bot
    app.updater = MagicMock(start_polling=AsyncMock(), stop=AsyncMock())
    return app


def _d_write_store(path, token: str) -> None:
    from channels.telegram_transport import token_digest
    path.write_text(json.dumps({"bot_id": CASA_BOT,
                                "token_digest": token_digest(token)}))


def _d_store(path) -> "dict | None":
    return json.loads(path.read_text()) if path.exists() else None


@pytest.fixture
def d_boot(monkeypatch, tmp_path):
    """``boot(case)``: Casa's real endpoint and transport as ``main`` builds
    them for that boot, not yet connected, installed as the tools' channel
    manager. ``env.connect()`` runs the endpoint's bring-up, whose ``getMe``
    returns 111; ``env.sent()`` lists the texts bot 111 sent."""
    from telegram.ext import Application

    from casa_core import _build_telegram_transport
    from channels.telegram import TelegramChannel

    apps: list = []

    def builder():
        chain = MagicMock()
        chain.token = MagicMock(return_value=chain)

        def build():
            apps.append(_d_fake_application(CASA_BOT))
            return apps[-1]

        chain.build = build
        return chain

    monkeypatch.setattr(Application, "builder", builder)
    saved = (tools._channel_manager, tools._bus, tools._specialist_registry,
             tools._engagement_registry)
    broker = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    monkeypatch.setattr(authz_grants, "GRANTS", authz_grants.GrantStore())
    monkeypatch.setattr(authz_grants, "CHALLENGES",
                        authz_grants.ChallengeCoordinator())
    monkeypatch.setattr(scheduled_asks, "STORE", scheduled_asks.ScheduledAskStore(
        str(tmp_path / "scheduled_asks.json")))
    monkeypatch.setattr(scheduled_asks, "_ROLE_EPOCHS", {})
    monkeypatch.setattr(scheduled_asks, "_TRIGGER_EPOCHS", {})

    def boot(case: str):
        path = tmp_path / "telegram-casa-bot.json"
        if case == "reboot":
            _d_write_store(path, _D_TOKEN)
        elif case == "rotation":
            _d_write_store(path, _D_OLD_TOKEN)
        transport, ep = _build_telegram_transport(
            token=_D_TOKEN, store_path=path, make_endpoint=TelegramChannel,
            default_agent="assistant", chat_id=str(OPERATOR))
        manager = ChannelManager()
        manager.register(transport)
        tools.init_tools(channel_manager=manager, bus=MagicMock(),
                         specialist_registry=MagicMock(),
                         mcp_registry=MagicMock())

        async def connect():
            await ep._rebuild()
            assert ep.is_ready

        def sent() -> list:
            if not apps:
                return []
            return [c.kwargs.get("text")
                    for c in apps[-1].bot.send_message.await_args_list]

        return NS(case=case, path=path, transport=transport, ep=ep,
                  manager=manager, connect=connect, sent=sent, tmp=tmp_path,
                  broker=broker, monkeypatch=monkeypatch)

    yield boot
    (tools._channel_manager, tools._bus, tools._specialist_registry,
     tools._engagement_registry) = saved


# -- the six ingresses, each through its real birth --------------------------


class _DStubBus:
    def __init__(self):
        self.sent: list = []

    async def send(self, msg):
        self.sent.append(msg)

    async def request(self, msg, timeout=300):
        self.sent.append(msg)
        return NS(content="ok")

    async def send_checked(self, msg):
        self.sent.append(msg)
        return "accepted"


async def _d_post(handler, path_pattern: str, path: str, body: bytes,
                  headers: dict) -> None:
    from casa_core_middleware import cid_middleware

    app = web.Application(middlewares=[cid_middleware])
    app.router.add_post(path_pattern, handler)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(path, data=body, headers=headers)
        assert resp.status == 200, await resp.text()


def _d_signed(body: bytes) -> dict:
    sig = hmac.new(_D_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-Webhook-Signature": sig}


async def _d_born_invoke(env) -> BusMessage:
    from casa_core import _make_invoke_handler
    from rate_limit import RateLimiter

    bus = _DStubBus()
    cfg = NS(channels=["telegram", "webhook"])
    handler = _make_invoke_handler(
        webhook_rate_limiter=RateLimiter(capacity=0, window_s=60.0),
        webhook_secret=_D_WEBHOOK_SECRET, bus=bus, assistant_role="assistant",
        role_configs={"assistant": cfg}, route_for=env.transport.route_for)
    body = json.dumps({"prompt": "Is the garage door open?",
                       "context": {"_tg_bot": 999}}).encode()
    await _d_post(handler, "/invoke/{agent}", "/invoke/assistant", body,
                  _d_signed(body))
    (msg,) = bus.sent
    return msg


async def _d_born_webhook(env) -> BusMessage:
    from casa_core import _make_webhook_handler
    from rate_limit import RateLimiter

    bus = _DStubBus()
    route = {"role": "assistant", "auth": {"mode": "hmac_body"},
             "clearance": "public"}
    handler = _make_webhook_handler(
        webhook_rate_limiter=RateLimiter(capacity=0, window_s=60.0),
        webhook_secret=_D_WEBHOOK_SECRET,
        trigger_registry=NS(webhook_route=lambda name: dict(route)),
        default_role="assistant", bus=bus,
        secrets_dir=env.tmp / "webhook_secrets",
        route_for=env.transport.route_for)
    body = json.dumps({"door": "open"}).encode()
    await _d_post(handler, "/webhook/{name}", "/webhook/garage", body,
                  _d_signed(body))
    (msg,) = bus.sent
    return msg


class _DVoiceAgent:
    """Records each turn the voice channel dispatches and answers it."""

    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus
        self.messages: list = []

    async def handle_message(self, msg: BusMessage) -> BusMessage:
        self.messages.append(msg)
        return BusMessage(type=MessageType.RESPONSE, source=_D_VOICE_ROLE,
                          target=msg.source, content="ok", reply_to=msg.id,
                          channel=msg.channel, context=msg.context)


async def _d_voice_turn(env, transport: str) -> BusMessage:
    import asyncio

    from casa_core_middleware import cid_middleware
    from channels.voice.channel import VoiceChannel
    from test_voice_context_sanitize import _FakeAgentConfig
    from voice_auth_helpers import VOICE_TEST_SECRET, SigningVoiceClient

    bus = MessageBus()
    voice_agent = _DVoiceAgent(bus)
    bus.register(_D_VOICE_ROLE, voice_agent.handle_message)
    loop_task = asyncio.create_task(bus.run_agent_loop(_D_VOICE_ROLE))
    channel = VoiceChannel(
        bus=bus, default_agent=_D_VOICE_ROLE, webhook_secret=VOICE_TEST_SECRET,
        sse_path="/api/converse", ws_path="/api/converse/ws",
        agent_configs={_D_VOICE_ROLE: _FakeAgentConfig()}, idle_timeout=300)
    app = web.Application(middlewares=[cid_middleware])
    channel.register_routes(app)
    try:
        async with TestClient(TestServer(app)) as raw:
            client = SigningVoiceClient(raw)
            context = {"device_id": "kitchen", "_tg_bot": 999}
            if transport == "sse":
                resp = await client.post("/api/converse", json={
                    "prompt": "hi", "agent_role": _D_VOICE_ROLE,
                    "context": context})
                await resp.read()
            else:
                async with client.ws_connect("/api/converse/ws") as ws:
                    await ws.send_json({
                        "type": "utterance", "utterance_id": "u1", "text": "hi",
                        "agent_role": _D_VOICE_ROLE, "scope_id": "s",
                        "context": context})
                    async for frame in ws:
                        if frame.type != WSMsgType.TEXT:
                            break
                        if json.loads(frame.data)["type"] in ("done", "error"):
                            break
    finally:
        loop_task.cancel()
    (msg,) = voice_agent.messages
    return msg


async def _d_born_voice_sse(env) -> BusMessage:
    return await _d_voice_turn(env, "sse")


async def _d_born_voice_ws(env) -> BusMessage:
    return await _d_voice_turn(env, "ws")


async def _d_born_trigger(env) -> BusMessage:
    from config import TriggerSpec
    from trigger_registry import TriggerRegistry

    sched, bus = MagicMock(), _DStubBus()
    reg = TriggerRegistry(scheduler=sched, app=web.Application(), bus=bus)
    reg.register_agent("assistant", [TriggerSpec(
        name="invoices", type="interval", minutes=60, channel="telegram",
        prompt="Ask whether to send the invoices.")], channels=["telegram"])
    await sched.add_job.call_args.args[0]()
    (msg,) = bus.sent
    return msg


async def _d_born_setup(env) -> BusMessage:
    """What ``main``'s setup dispatch hands the bus: the turn context of a
    plugin-setup turn for the assistant."""
    from casa_core import _setup_turn_context

    context = _setup_turn_context(env.transport, {
        "synthetic": "plugin_setup", "plugin_setup_target": "assistant"})
    assert context is not None
    return BusMessage(type=MessageType.CHANNEL_IN, source="telegram",
                      target="assistant", content="Set up the lantern plugin.",
                      channel="telegram", context=context)


_D_INGRESSES = {
    "invoke": _d_born_invoke,
    "webhook": _d_born_webhook,
    "voice_sse": _d_born_voice_sse,
    "voice_ws": _d_born_voice_ws,
    "trigger": _d_born_trigger,
    "setup": _d_born_setup,
}


def _d_origin(msg: BusMessage) -> dict:
    """The turn's origin as the agent binds it for its tools."""
    return with_scope({**msg.context, "role": msg.target,
                       "execution_role": msg.target, "channel": msg.channel,
                       "message_type": msg.type.value, "source": msg.source})


async def _d_call(tool, origin: dict, args: dict) -> dict:
    token = agent_mod.origin_var.set(origin)
    try:
        return await tool.handler(args)
    finally:
        agent_mod.origin_var.reset(token)


def _d_text(res: dict) -> str:
    return res["content"][0]["text"]


async def _d_send_message(origin: dict, text: str) -> dict:
    return await _d_call(tools.send_message, origin,
                         {"message": text, "channel": "telegram"})


# -- births and deliveries ----------------------------------------------------


@pytest.mark.parametrize("ingress", sorted(_D_INGRESSES))
async def test_after_a_normal_reboot_a_turn_born_before_the_bot_connects_carries_its_id(
        d_boot, ingress):
    env = d_boot("reboot")
    assert not env.ep.is_ready
    msg = await _D_INGRESSES[ingress](env)
    assert msg.context["_tg_bot"] == CASA_BOT

    await env.connect()
    res = await _d_send_message(_d_origin(msg), "The garage door is open.")
    assert not res.get("is_error"), res
    assert env.sent() == ["The garage door is open."]
    # the stored id was already right: nothing rewritten
    from channels.telegram_transport import token_digest
    assert _d_store(env.path) == {"bot_id": CASA_BOT,
                                  "token_digest": token_digest(_D_TOKEN)}


@pytest.mark.parametrize("ingress", sorted(_D_INGRESSES))
@pytest.mark.parametrize("case", _D_UNKNOWN_ID)
async def test_a_turn_born_before_casas_bot_has_an_id_delivers_through_it_once_it_connects(
        d_boot, case, ingress):
    env = d_boot(case)
    assert not env.ep.has_bot_id
    msg = await _D_INGRESSES[ingress](env)
    assert msg.context["_tg_bot"] == CASA
    origin = _d_origin(msg)

    await env.connect()
    res = await _d_send_message(origin, "Your invoices went out.")
    assert not res.get("is_error"), res
    assert env.sent() == ["Your invoices went out."]


@pytest.mark.parametrize("case", _D_UNKNOWN_ID)
async def test_once_casas_bot_connects_its_id_is_stored_for_this_token_and_new_turns_carry_it(
        d_boot, case):
    from channels.telegram_transport import token_digest

    env = d_boot(case)
    assert env.transport.casa_route() == CASA
    await env.connect()

    assert _d_store(env.path) == {"bot_id": CASA_BOT,
                                  "token_digest": token_digest(_D_TOKEN)}
    assert stat.S_IMODE(os.stat(env.path).st_mode) == 0o600
    msg = await _d_born_invoke(env)
    assert msg.context["_tg_bot"] == CASA_BOT


# -- records persisted from a turn born with CASA -----------------------------


async def _d_launch_scheduled_job(env, registry) -> str:
    """A job trigger fires before the bot connects: the job's engagement is
    created, with the origin the launcher builds, in a real registry."""
    import background_jobs

    host = NS(role="assistant", kind="specialist",
              decl=NS(title="Weekly ledger"))
    env.monkeypatch.setattr(background_jobs, "find_job_host", lambda *a: host)
    created: list = []

    async def start_on_host(_host, task, context, origin, **_kw):
        rec = await registry.create(kind="specialist", role_or_type="assistant",
                                    driver="in_casa", task=task, origin=origin)
        created.append(rec.id)
        return tools._result({"status": "pending", "engagement_id": rec.id})

    env.monkeypatch.setattr(tools, "_start_job_on_host", start_on_host)
    trig = NS(name="weekly", type="cron", job="ledger:weekly",
              task="Check the ledger.", context="", channel="telegram")
    await tools.start_scheduled_job("assistant", trig)
    (eid,) = created
    return eid


async def _d_delegate(env, origin: dict) -> None:
    from job_registry import JobRegistry
    from specialist_registry import DelegationRecord, SpecialistRegistry

    reg = SpecialistRegistry(str(env.tmp / "specialists"),
                             job_registry=JobRegistry(env.tmp / "jobs.json"))
    await reg.register_delegation(DelegationRecord(
        id="d" * 32, agent="finance", started_at=1.0,
        origin={**origin, "user_text": "What did October cost?"}))


@pytest.mark.parametrize("case", _D_UNKNOWN_ID)
async def test_records_persisted_before_casas_bot_has_an_id_deliver_through_it_after_a_restart(
        d_boot, case):
    """A scheduled job and a delegation born from turns with the route CASA
    are persisted with it, reloaded through the strict loaders, and deliver
    through Casa's bot once it has connected: the job's start line, and the
    delegation's notice through the real boot replay."""
    import casa_core
    from engagement_registry import EngagementRegistry
    from job_registry import JobRegistry

    env = d_boot(case)
    tombstone = str(env.tmp / "engagements.json")
    eid = await _d_launch_scheduled_job(
        env, EngagementRegistry(tombstone_path=tombstone))
    trigger_turn = await _d_born_trigger(env)
    await _d_delegate(env, _d_origin(trigger_turn))

    # The restart: both stores reloaded through their strict loaders.
    engagements = EngagementRegistry(tombstone_path=tombstone)
    await engagements.load()
    rec = engagements.get(eid)
    assert rec is not None and rec.origin["_tg_bot"] == CASA
    jobs = JobRegistry(env.tmp / "jobs.json")
    await jobs.load()
    recovered = await jobs.recover_after_restart()
    assert [j.route for j in recovered] == [{"_tg_bot": CASA}]

    bus = NS(queues={"assistant": object()}, sent=[])

    async def notify(message):
        bus.sent.append(message)

    bus.notify = notify
    await casa_core._notify_recovered_delegations(
        recovered, jobs, bus, assistant_role="assistant")
    (notice,) = bus.sent
    assert notice.context["_tg_bot"] == CASA
    assert notice.content.origin["_tg_bot"] == CASA

    # Before the bot connects nothing is posted, and nothing raises.
    assert await env.transport.post_job_start_line(rec) is None
    assert env.sent() == []

    await env.connect()
    assert await env.transport.post_job_start_line(rec) is not None
    out = await env.transport.send_response(
        admitted("Finance answered: October cost 412 euros."), notice.context)
    assert out is DeliveryOutcome.DELIVERED
    texts = env.sent()
    assert len(texts) == 2
    assert texts[0].startswith("📊 ") and "Check the ledger." in texts[0]
    assert "412 euros" in texts[1]


# -- the unknown-id window -----------------------------------------------------


@pytest.fixture
def d_outbox(tmp_path):
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


@pytest.mark.parametrize("ingress", sorted(_D_INGRESSES))
@pytest.mark.parametrize("case", _D_UNKNOWN_ID)
async def test_before_casas_bot_has_an_id_a_send_reports_not_delivered_as_it_always_has(
        d_boot, case, ingress):
    env = d_boot(case)
    msg = await _D_INGRESSES[ingress](env)
    res = await _d_send_message(_d_origin(msg), "The garage door is open.")
    assert res.get("is_error") is True
    assert "delivered nothing" in _d_text(res)
    assert env.sent() == []


@pytest.mark.parametrize("case", _D_UNKNOWN_ID)
async def test_before_casas_bot_has_an_id_a_file_is_unavailable_and_stays_owed(
        d_boot, d_outbox, case):
    env = d_boot(case)
    origin = _d_origin(await _d_born_setup(env))
    assert origin["_tg_bot"] == CASA

    res = await _d_call(tools.send_media, origin,
                        {"path": d_outbox, "kind": "document"})

    payload = json.loads(_d_text(res))
    assert payload["status"] == "error"
    assert payload["kind_error"] == "channel_unavailable"
    # the endpoint's own "not started", not a missing or malformed route
    assert payload["message"] == "channel not started"
    sends = origin["turn_scope"].operator_sends
    assert len(sends) == 1 and sends[0].delivered is False


@pytest.mark.parametrize("ingress", sorted(_D_INGRESSES))
@pytest.mark.parametrize("case", _D_UNKNOWN_ID)
async def test_before_casas_bot_has_an_id_a_streamed_reply_is_harmless(
        d_boot, case, ingress):
    env = d_boot(case)
    msg = await _D_INGRESSES[ingress](env)
    on_token = env.transport.create_on_token(msg.context)
    assert callable(on_token)
    await on_token("partial answer")
    out = await env.transport.finalize_stream("the answer", msg.context, on_token)
    assert out is DeliveryOutcome.NOT_DELIVERED
    await env.transport.turn_finished(msg.context)
    assert env.sent() == []


@pytest.mark.parametrize("case", _D_UNKNOWN_ID)
async def test_before_casas_bot_has_an_id_a_protected_call_is_denied_and_nothing_is_keyed(
        d_boot, case):
    env = d_boot(case)
    origin = _d_origin(await _d_born_setup(env))
    tool = "mcp__plugin_lantern_lantern__reset"
    hook = authz_grants.make_resident_authz_hook(
        "assistant", {tool: {"artifact_id": "artifact-1", "summary": None}},
        lambda: authz_grants.AuthzDeps(channel=env.transport,
                                       grants=authz_grants.GRANTS,
                                       challenges=authz_grants.CHALLENGES))
    token = agent_mod.origin_var.set(origin)
    try:
        assert authz_grants.resolve_grant_identity("assistant", "artifact-1") == (
            None, "channel_unavailable")
        out = await hook({"tool_name": tool, "tool_input": {}}, "c-1", {})
    finally:
        agent_mod.origin_var.reset(token)
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == \
        authz_grants._DENY_DELIVERY_FAILED
    assert authz_grants.GRANTS._grants == {}
    assert authz_grants.CHALLENGES._entries == {}
    assert env.broker._live == {}
    assert env.sent() == []


@pytest.mark.parametrize("case", _D_UNKNOWN_ID)
async def test_before_casas_bot_has_an_id_a_scheduled_question_is_not_posted_and_nothing_is_stored(
        d_boot, case):
    env = d_boot(case)
    origin = _d_origin(await _d_born_trigger(env))
    assert origin["_tg_bot"] == CASA

    res = await _d_call(tools.ask_user, origin,
                        {"question": "Send the invoices now?",
                         "options": ["Yes", "No"]})

    assert json.loads(_d_text(res)) == {
        "status": "error", "kind": "delivery_failed",
        "message": "could not deliver the question to the operator"}
    assert env.broker._live == {}
    assert scheduled_asks.STORE.all() == []
    assert not (env.tmp / "scheduled_asks.json").exists()
    assert env.sent() == []


# ---------------------------------------------------------------------------
# Every derivation keeps the route of the origin it is built from
#
# A turn's route is decided where the turn is born; every origin, context or
# record later built from it carries that route unchanged. Each case below
# drives one real builder from an origin routed to Casa's bot (111) while the
# role it serves, ``finance``, is bound to bot 222, and reads the route off
# what the builder produced. A builder that dropped the route, or re-derived
# one from the role, yields 222 or nothing.
# ---------------------------------------------------------------------------


def _e_origin(**extra) -> dict:
    """A Telegram turn of ``finance`` that was routed to Casa's bot."""
    return {"role": "finance", "execution_role": "finance",
            "channel": "telegram", "chat_id": str(OPERATOR),
            "user_id": OPERATOR, "cid": "e-1", "user_text": "reconcile it",
            "_tg_bot": CASA_BOT, **extra}


async def _e_turn_reply(env, tmp_path, monkeypatch):
    """A turn's origin, which its tools read, copies the route from the
    turn's context; and its reply leaves through that route."""
    import agent as agent_mod
    from bus import BusMessage, MessageType
    from test_agent_process import _make_agent, _mk_assistant, patch_retry_sleep
    from test_pin_1121_turn_limit import _Client, _reset

    origins: list = []

    class _Seeing(_Client):
        async def query(self, text):
            origins.append(dict(agent_mod.origin_var.get(None) or {}))
            return await super().query(text)

    agent = _make_agent(tmp_path, role="finance")
    agent._channel_manager.register(env.transport)
    msg = BusMessage(
        type=MessageType.REQUEST, source="telegram", target="finance",
        content="how is the ledger?", channel="telegram",
        context={"chat_id": str(OPERATOR), "user_id": OPERATOR, "cid": "e-2",
                 "_tg_bot": CASA_BOT})
    _reset([_mk_assistant("The ledger is fine.")])
    with patch("sdk_client_pool._default_make_client", _Seeing), patch_retry_sleep():
        try:
            await agent.handle_message(msg)
        finally:
            await agent.aclose()
    replies = [bot for bot, ep in env.ep.items() for name, _ in calls(ep)
               if name in ("send", "send_response", "finalize_response_stream",
                           "finalize_stream")]
    assert len(origins) == 1 and len(replies) == 1
    return [o.get("_tg_bot") for o in origins] + replies


def _e_completion_synthesis(env, tmp_path, monkeypatch):
    from bus import BusMessage, MessageType
    from specialist_registry import DelegationComplete
    from test_agent_process import _make_agent

    agent = _make_agent(tmp_path, role="finance")
    msg = BusMessage(
        type=MessageType.NOTIFICATION, source="assistant", target="finance",
        content=DelegationComplete(delegation_id="d-1", agent="assistant",
                                   status="ok", text="Done.", origin=_e_origin()),
        channel="telegram",
        context={"chat_id": str(OPERATOR), "cid": "e-1", "delegation_id": "d-1",
                 "_tg_bot": FINANCE_BOT})
    return [agent._synthesize_delegation_turn(msg).context.get("_tg_bot")]


async def _e_engagement_persist(env, tmp_path, monkeypatch):
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    await reg.load()
    rec = await reg.create(kind="specialist", role_or_type="finance",
                           driver="in_casa", task="t", origin=_e_origin())
    again = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    await again.load()
    return [rec.origin.get("_tg_bot"), again.get(rec.id).origin.get("_tg_bot")]


async def _e_delegation_row(env, tmp_path, monkeypatch):
    from job_registry import JobRegistry
    from specialist_registry import (
        DelegationRecord, SpecialistRegistry, origin_from_job,
    )
    reg = SpecialistRegistry(str(tmp_path / "specialists"),
                             job_registry=JobRegistry(tmp_path / "jobs.json"))
    await reg.register_delegation(DelegationRecord(
        id="d-2", agent="finance", started_at=1.0,
        origin={**_e_origin(), "role": "assistant"}))
    again = JobRegistry(tmp_path / "jobs.json")
    await again.load()
    return [origin_from_job(again.get("d-2")).get("_tg_bot")]


async def _e_boot_replay(env, tmp_path, monkeypatch):
    """The owed completion of a delegation, replayed at boot from its row."""
    import casa_core
    from job_registry import DeliveryState, ExecutionState
    from test_job_addresses import _voice_job

    row = _voice_job(creating_role="finance", route={"_tg_bot": CASA_BOT},
                     execution_state=ExecutionState.SUCCEEDED,
                     delivery_state=DeliveryState.READY, terminal_at=10.0,
                     result="the answer", orphan_notification_pending=True)
    sent: list = []
    bus = NS(queues={"finance": object(), "assistant": object()},
                notify=AsyncMock(side_effect=lambda m: sent.append(m)))
    await casa_core._notify_recovered_delegations(
        [row], NS(), bus, assistant_role="assistant")
    (msg,) = sent
    return [msg.context.get("_tg_bot"), msg.content.origin.get("_tg_bot")]


def _e_identity(bot_id: int = FINANCE_BOT):
    from authz_grants import GrantIdentity
    return GrantIdentity(operator_id=OPERATOR, chat_id=OPERATOR,
                         enforcement_role="browser", artifact_id="1" * 64,
                         engagement_id="", bot_id=bot_id)


def _e_binding(tmp_path, monkeypatch):
    """A binding minted in the turn; its call's grant identity was resolved
    on bot 222, the turn's route is 111."""
    import plugin_bindings
    store = plugin_bindings.BindingStore(str(tmp_path / "b.json"))
    monkeypatch.setattr(plugin_bindings, "BINDINGS", store)
    store.for_turn(plugin="browser", turn_key="t-1", identity=_e_identity(),
                   chain=("finance", "browser"), origin=_e_origin())
    again = plugin_bindings.BindingStore(str(tmp_path / "b.json"))
    (b,) = [again.peek(entry["ref"]) for entry in json.loads(
        (tmp_path / "b.json").read_text())]
    return b


def _e_binding_route(env, tmp_path, monkeypatch):
    return [_e_binding(tmp_path, monkeypatch).origin.get("_tg_bot")]


def _e_continuation_origin(env, tmp_path, monkeypatch):
    import plugin_continuations as pc
    return [pc.continuation_origin(_e_binding(tmp_path, monkeypatch)).get("_tg_bot")]


async def _e_continuation_record(env, tmp_path, monkeypatch):
    """The delegation record a continuation registers names the binding's
    route."""
    import authz_grants
    import plugin_continuations as pc
    import specialist_desk
    import tools
    from config import AgentConfig, CharacterConfig, MemoryConfig
    from role_artifact_stub import STUB_ROLE_ARTIFACT
    from tools import DelegatedOutput

    def cfg(role, name, kind="specialist"):
        return AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role=role, model="x",
                           character=CharacterConfig(name=name), system_prompt="x",
                           memory=MemoryConfig(token_budget=0), kind=kind)

    monkeypatch.setattr(tools, "_agent_role_map", {
        "finance": cfg("finance", "Alex", "resident"),
        "browser": cfg("browser", "Fido")}, raising=False)
    monkeypatch.setattr(authz_grants, "_live_operator_identity",
                        lambda: (OPERATOR, OPERATOR))
    monkeypatch.setattr(specialist_desk, "DESKS", specialist_desk.DeskRegistry())
    monkeypatch.setattr(tools, "_specialist_limiter", None, raising=False)
    records: list = []
    monkeypatch.setattr(tools, "_specialist_registry", NS(
        register_delegation=AsyncMock(side_effect=lambda r: records.append(r))),
        raising=False)
    tasks: list = []
    monkeypatch.setattr(tools, "_attach_completion_callback",
                        lambda task, record, **kw: tasks.append(task))

    async def bounded(cfg, task_text, context_text, resolution=None,
                      output_format=None):
        return DelegatedOutput(text="done")

    monkeypatch.setattr(tools, "_run_delegated_agent_bounded", bounded)
    b = _e_binding(tmp_path, monkeypatch)
    await pc.run_continuation(pc.Pending(b, "r-1", "sweep", 0.0, bot_id=CASA_BOT),
                              on_start=lambda: None)
    for task in tasks:
        await task
    return [r.origin.get("_tg_bot") for r in records]


def _e_scheduled_ask_meta(env, tmp_path, monkeypatch):
    """A scheduled question's broker meta, live or restored, names the bot
    its record was posted through."""
    import scheduled_asks
    rec = {"options": ["Yes", "No"], "bot_id": CASA_BOT, "chat_id": OPERATOR,
           "operator_id": OPERATOR, "role": "finance",
           "session_scope": "interval:check", "scope": f"dm:{CASA_BOT}:{OPERATOR}",
           "epoch": 1, "message_id": 5}
    return [scheduled_asks.broker_meta(rec)["bot_id"]]


def _e_launch_note(env, tmp_path, monkeypatch):
    import tools
    from output_boundary_testing import with_scope
    return [tools._launch_note(with_scope(_e_origin()), "a brief")[0].get("_tg_bot")]


async def _e_delegation_envelope(env, tmp_path, monkeypatch):
    import tools
    from specialist_registry import DelegationComplete, DelegationRecord
    bus = NS(notify=AsyncMock())
    monkeypatch.setattr(tools, "_bus", bus)
    monkeypatch.setattr(tools, "_specialist_registry", NS(
        job_registry=NS(ack_terminal_notification=AsyncMock())))
    record = DelegationRecord(id="d-3", agent="tax", started_at=0.0,
                              origin=_e_origin())

    async def settled():
        return None

    await tools._settle_then_announce(
        record, DelegationComplete(delegation_id="d-3", agent="tax",
                                   status="ok", text="Done."), settled())
    return [bus.notify.await_args.args[0].context.get("_tg_bot")]


async def _e_engagement_envelope(env, tmp_path, monkeypatch):
    import tools
    from specialist_registry import DelegationComplete
    bus = NS(notify=AsyncMock())
    await tools.send_engagement_outcome(
        bus, complete=DelegationComplete(delegation_id="e-3", agent="tax",
                                         status="ok"),
        origin=_e_origin(), ack=None)
    return [bus.notify.await_args.args[0].context.get("_tg_bot")]


async def _e_delegated_run(env, tmp_path, monkeypatch):
    """The origin a delegated run executes under, built from its parent."""
    import agent as agent_mod
    import tools
    from test_handoff import _CapturingClient, _cfg

    _CapturingClient.prompts, _CapturingClient.origins = [], []
    roles = {"finance": _cfg("finance", "Alex", kind="resident"),
             "tax": _cfg("tax", "Tess")}
    monkeypatch.setattr(tools, "_agent_role_map", roles, raising=False)
    monkeypatch.setattr(tools, "ClaudeSDKClient", _CapturingClient)
    token = agent_mod.origin_var.set(_e_origin(delegation_depth=0))
    try:
        await tools._run_delegated_agent(roles["tax"], "file the return", "")
    finally:
        agent_mod.origin_var.reset(token)
    return [o.get("_tg_bot") for o in _CapturingClient.origins]


async def _e_handoff_child(env, tmp_path, monkeypatch):
    import agent as agent_mod
    import specialist_limits
    import tools
    from test_handoff import _cfg
    from tools import DelegatedOutput

    roles = {"finance": _cfg("finance", "Alex", kind="resident"),
             "tax": _cfg("tax", "Tess"), "browser": _cfg("browser", "Fido")}
    monkeypatch.setattr(tools, "_agent_role_map", roles, raising=False)
    monkeypatch.setattr(tools, "_specialist_limiter",
                        specialist_limits.SpecialistLimiter(max_global=1),
                        raising=False)
    seen: list = []

    async def bounded(cfg, task_text, context_text, resolution=None,
                      output_format=None):
        seen.append((agent_mod.origin_var.get(None) or {}).get("_tg_bot"))
        return DelegatedOutput(text="saved")

    monkeypatch.setattr(tools, "_run_delegated_agent_bounded", bounded)
    origin = _e_origin(role="finance", execution_role="tax", delegation_depth=1,
                       _handoff_chain=("finance", "tax"), _operator_turn=True,
                       _operator_words="collect them")
    token = agent_mod.origin_var.set(origin)
    try:
        await tools.hand_off.handler({"agent": "browser", "task": "download",
                                      "expected_result": "two PDFs"})
    finally:
        agent_mod.origin_var.reset(token)
    return seen


async def _e_delegated_child(env, tmp_path, monkeypatch):
    """A resident's delegation: the child run's origin and the delegation's
    durable row both keep the delegating turn's route."""
    import agent as agent_mod
    import tools
    from test_delegate_to_agent import _seeded_delegation_harness
    from tools import DelegatedOutput

    reg, _bus = _seeded_delegation_harness(tmp_path, monkeypatch)
    monkeypatch.setattr(tools, "_channel_manager", env.manager)
    seen: list = []

    async def bounded(cfg, task_text, context_text, resolution=None,
                      output_format=None):
        seen.append((agent_mod.origin_var.get(None) or {}).get("_tg_bot"))
        return DelegatedOutput(text="done")

    monkeypatch.setattr(tools, "_run_delegated_agent_bounded", bounded)
    token = agent_mod.origin_var.set(_e_origin(role="assistant",
                                               execution_role="assistant"))
    try:
        await tools.delegate_to_agent.handler(
            {"agent": "finance", "task": "x", "context": "", "mode": "sync"})
    finally:
        agent_mod.origin_var.reset(token)
    rows = [job.route.get("_tg_bot") for job in reg.job_registry.all()]
    assert len(seen) == 1 and len(rows) == 1
    return seen + rows


async def _e_voice_job_run(env, tmp_path, monkeypatch):
    """A voice turn's asynchronous job runs under a copy of the turn's
    origin, route included."""
    import tools
    from job_registry import JobRegistry
    from test_handoff import _cfg

    registry = JobRegistry(tmp_path / "jobs.json")
    monkeypatch.setattr(tools, "_specialist_registry",
                        NS(job_registry=registry), raising=False)
    captured: list = []

    class _Task:
        def done(self):
            return False

        def add_done_callback(self, cb):
            pass

        def cancel(self):
            pass

    def lifecycle(*, start_gate, lifecycle_kwargs):
        captured.append(lifecycle_kwargs["origin"])
        return _Task()

    monkeypatch.setattr(tools, "_create_voice_lifecycle_task", lifecycle)
    monkeypatch.setattr(registry, "bind_task", AsyncMock())
    origin = _e_origin(channel="voice", chat_id="kitchen", cid="v-1",
                       voice_route_id="v-1", origin_device_id="kitchen-1")
    await tools._start_voice_async_job(
        cfg=_cfg("tax", "Tess"), specialist_role="tax", task_text="t",
        context_text="", origin=origin, resolution=None, permit=None,
        handoff=tools._PermitHandoff())
    return [o.get("_tg_bot") for o in captured]


#: The derived builders of the classified site list, by case. The site list
#: in ``test_bot_boundary_sites.py`` names one of these for every builder it
#: classifies as derived.
DERIVED_CASES = {
    "turn_reply": _e_turn_reply,
    "completion_synthesis": _e_completion_synthesis,
    "engagement_persist": _e_engagement_persist,
    "delegation_row": _e_delegation_row,
    "boot_replay": _e_boot_replay,
    "binding": _e_binding_route,
    "continuation_origin": _e_continuation_origin,
    "continuation_record": _e_continuation_record,
    "scheduled_ask_meta": _e_scheduled_ask_meta,
    "launch_note": _e_launch_note,
    "delegation_envelope": _e_delegation_envelope,
    "engagement_envelope": _e_engagement_envelope,
    "delegated_run": _e_delegated_run,
    "handoff_child": _e_handoff_child,
    "delegated_child": _e_delegated_child,
    "voice_job_run": _e_voice_job_run,
}


@pytest.fixture
def e_env(monkeypatch):
    """Bots 111 (Casa) and 222 (``finance``) registered as Casa's channel
    manager, so a builder that asked the transport for the role's route
    would get 222."""
    import tools
    env = build_two_endpoints()
    assert env.transport.route_for("finance") == FINANCE_BOT
    monkeypatch.setattr(tools, "_channel_manager", env.manager, raising=False)
    return env


@pytest.mark.parametrize("case", sorted(DERIVED_CASES))
async def test_every_derivation_keeps_the_route_it_was_built_from(
        e_env, tmp_path, monkeypatch, case):
    out = DERIVED_CASES[case](e_env, tmp_path, monkeypatch)
    if hasattr(out, "__await__"):
        out = await out
    assert out, f"{case}: the builder produced nothing to read a route from"
    assert all(route == CASA_BOT for route in out), out


# ---------------------------------------------------------------------------
# Before Casa's bot has an id, a card that names its bot first posts nothing
#
# Some of Casa's cards and questions form an identity that names the bot before
# the message exists: the broker lane a tap is checked against, a consent or
# erase key. Until Casa's bot has completed its first getMe (a fresh install,
# a replaced token) that bot has no id, so each such writer gives the outcome
# it gives today when its post fails, and registers, stores and posts nothing.
# Casa's endpoint here has no id at all; bot 222 is ready, and is never used.
# ---------------------------------------------------------------------------


class _FBroker(VerdictBroker):
    """The real broker, remembering every scope a request was registered
    under, so a registration later undone still shows."""

    def __init__(self) -> None:
        super().__init__()
        self.registered: list[str] = []

    def register(self, *args, **kwargs):
        self.registered.append(kwargs.get("scope"))
        return super().register(*args, **kwargs)


@pytest.fixture
def f_window(monkeypatch):
    import agent as agent_mod
    import authz_grants
    import tools as tools_mod
    import verdict_broker
    from channels.telegram import TelegramChannel
    from telegram_channel_double import make_transport

    bus = _CBus()
    casa = TelegramChannel(
        bot_token="111:new-token", chat_id=str(OPERATOR),
        default_agent="assistant", bus=bus, is_casa=True, known_bot_id=None)
    finance = _c_endpoint(FINANCE_BOT, bus)
    manager = make_transport(casa, finance, casa=casa,
                             casa_bot_ids=MemoryBotIdStore())
    assert not casa.has_bot_id
    monkeypatch.setattr(tools_mod, "_channel_manager", manager, raising=False)
    broker = _FBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", broker)
    challenges = _CChallenges()
    monkeypatch.setattr(authz_grants, "CHALLENGES", challenges)
    monkeypatch.setattr(tools_mod, "CHALLENGES", challenges, raising=False)
    monkeypatch.setattr(agent_mod, "active_session_registry", MagicMock(),
                        raising=False)
    token = agent_mod.origin_var.set(dict(_C_TURN_ON_222))
    yield NS(casa=casa, finance=finance, manager=manager, broker=broker,
             challenges=challenges, monkeypatch=monkeypatch)
    agent_mod.origin_var.reset(token)


async def _f_specialist_install(h, tmp_path):
    import specialist_install
    import specialist_install_consent as sic
    import tools as tools_mod
    staged = NS(
        component_id="ledger-specialist", version="1.0.0", slug="ledger",
        component_checksum="f" * 64, root_digest="0" * 64, mission="Books.",
        default_persona_ref="persona:ledger@1", required_config_names=(),
        required_secret_names=(), dependencies=(), staged_dir=tmp_path,
        resolved_ref="v1.0.0", receipt_id="r-1", receipt_digest="1" * 64,
        plugin_resolutions=())
    h.monkeypatch.setattr(specialist_install, "inspect_specialist_repo",
                          lambda *a, **kw: staged)
    h.monkeypatch.setattr(sic.SpecialistInstallAckStore, "is_acked",
                          lambda self, identity: False)
    payload = _c_payload(await tools_mod.specialist_install_inspect.handler(
        {"repo": "https://example.invalid/ledger.git", "ref": "v1.0.0"}))
    return payload.get("kind")


async def _f_persona_install(h, tmp_path):
    import persona_install
    import tools as tools_mod
    staged = NS(persona_id="ledger", version="1.0.0", checksum="9" * 64,
                display_name="Ledger", staged_dir=tmp_path,
                resolved_ref="v1.0.0")
    h.monkeypatch.setattr(persona_install, "inspect_persona_repo",
                          lambda *a, **kw: staged)
    h.monkeypatch.setattr(persona_install.PersonaInstallAckStore, "is_acked",
                          lambda self, identity: False)
    payload = _c_payload(await tools_mod.persona_install_inspect.handler(
        {"repo": "https://example.invalid/persona.git", "ref": "v1.0.0"}))
    return payload.get("kind")


async def _f_erase_question(h, tmp_path):
    import plugin_erase_consent as pec
    import plugin_erasure as pe
    import tools as tools_mod
    choices = pec.ChoiceGrants()
    h.monkeypatch.setattr(pec, "CHOICES", choices)
    h.monkeypatch.setattr(pe, "QUESTIONS", pe.QuestionIds())
    spec = pe.EraseSpec(name="probe", artifact_id="e" * 64,
                        targets=("specialist:finance",),
                        tool_names=("mcp__plugin_probe_srv__erase_all",),
                        protected=False, tool="erase_all", summary=None)
    payload, reports = await tools_mod._erase_gate(
        tool="plugin_remove", arg="name", name="probe", subject="plugin:probe",
        what="the plugin probe", specs=[spec], erase=None)
    assert reports == [] and choices._rows == {}
    return payload["kind"]


async def _f_wipe_card(h, tmp_path):
    import tools as tools_mod
    from memory import maintenance
    intent = tmp_path / "memory" / "maintenance.json"
    h.monkeypatch.setattr(maintenance, "INTENT_PATH", str(intent))
    payload = _c_payload(await tools_mod.wipe_memory.handler({}))
    assert not intent.exists()
    return payload["kind"]


async def _f_restart_card(h, tmp_path):
    import configurator_run
    configurator_run.note_restart("run-1", "add the ledger specialist")
    return await configurator_run.offer_restart("run-1")


async def _f_operator_question(h, tmp_path):
    """Run admission's confirmation card, asked through Casa's endpoint."""
    import operator_calls
    from authz_grants import GrantIdentity
    identity = GrantIdentity(operator_id=OPERATOR, chat_id=OPERATOR,
                             enforcement_role="browser", artifact_id="1" * 64,
                             engagement_id="", bot_id=CASA_BOT)
    return await operator_calls.post_question(
        endpoint=h.casa, identity=identity, role="browser",
        text="Run the order?", options=["Yes", "No"], timeout_s=30.0,
        on_settle=AsyncMock())


async def _f_trigger_consent(h, tmp_path):
    import trigger_reconcile
    trigger_reconcile._fire_consent_prompts(
        [{"plugin": "lantern", "artifact_id": "a" * 64, "effective": "lantern-hook",
          "target": "resident:finance",
          "auth": {"mode": "hmac", "header": "X-Sig"}}],
        trigger_registry=MagicMock(), role_configs={},
        channel_manager=h.manager, acks=MagicMock(), secrets_dir=None,
        resolver=None, global_secret_ok=None, nonce_by_identity={})
    return len(h.challenges.handles)


async def _f_event_consent(h, tmp_path):
    import event_reconcile
    event_reconcile._fire_consent_prompts(
        [_c_event_row()], role_configs={}, channel_manager=h.manager,
        acks=MagicMock(), resolver=None, entries=None)
    return len(h.challenges.handles)


async def _f_callback_consent(h, tmp_path):
    import callback_reconcile
    callback_reconcile._fire_consent_prompts(
        [_c_callback_row()], trigger_registry=MagicMock(), role_configs={},
        channel_manager=h.manager, acks=MagicMock(), spool=MagicMock(),
        resolver=None, entries=None, nonce_by_identity={})
    return len(h.challenges.handles)


_F_SITES = {
    # site: (driver, the outcome it gives when its post cannot be made)
    "trigger consent card": (_f_trigger_consent, 0),
    "event consent card": (_f_event_consent, 0),
    "callback consent card": (_f_callback_consent, 0),
    "specialist install card": (_f_specialist_install, "consent_channel_unavailable"),
    "persona install card": (_f_persona_install, "consent_channel_unavailable"),
    "erase-data question": (_f_erase_question, "consent_channel_unavailable"),
    "memory wipe card": (_f_wipe_card, "delivery_failed"),
    "restart card": (_f_restart_card, False),
    "operator question": (_f_operator_question, False),
}


@pytest.mark.parametrize("site", sorted(_F_SITES))
async def test_before_casas_bot_has_an_id_a_card_that_names_it_is_not_posted(
        f_window, tmp_path, site):
    run, expected = _F_SITES[site]
    assert await run(f_window, tmp_path) == expected
    await asyncio.sleep(0)
    assert f_window.broker.registered == [] and f_window.broker._live == {}
    assert f_window.challenges._inner._entries == {}
    assert calls(f_window.casa) == [] and calls(f_window.finance) == []
