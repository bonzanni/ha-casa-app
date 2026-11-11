"""What is still waiting on an agent's bot (``own_voice.open_on_bot``).

An agent gets a new bot only when nothing Casa holds is still waiting on its
current one. The predicate reads Casa's own stores, through the module
globals the live code uses, and names each kind it finds once. Here Alex
(``finance``) speaks from bot 222; bot 333 is another agent's and bot 111 is
Casa's. Every store is a real instance over ``tmp_path``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

import own_voice
import own_voice_lifecycle
import plugin_bindings
import plugin_continuations
import run_admission
import scheduled_asks
import tools
import verdict_broker
from authz_grants import GrantIdentity
from bus import BusMessage, MessageBus, MessageType
from channels.tg_address import TgAddress, scope as tg_scope
from engagement_registry import JOB_POST_QUESTION, EngagementRegistry
from job_registry import DeliveryState, ExecutionState, JobRegistry, VoiceJob
from own_voice_lifecycle import HeldJournal
from personality_types import SpeakerProvenance
from specialist_registry import SpecialistRegistry
from task_model import Requester

ROLE = "finance"
BOT = 222
OTHER = 333
CASA = 111
OPERATOR = 100
NOW = 200.0

DELEGATED = "a delegated task"
JOB = "a background job"
JOB_CARD = "a job's card"
SCHEDULED = "a scheduled question"
RUN_CARD = "a run card with Stop"
HELD = "a message held for it"
QUESTION = "a question or card awaiting an answer"
CONTINUATION = "a continuation"
ANSWERING = "a message it is answering now"
UNCHECKED = "something Casa could not check"


@pytest.fixture
async def world(tmp_path, monkeypatch):
    """Casa's stores, empty, installed where the live code reads them."""
    jobs = JobRegistry(tmp_path / "jobs.json", clock=lambda: NOW)
    await jobs.load()
    specialists = SpecialistRegistry(str(tmp_path / "specialists"), job_registry=jobs)
    engagements = EngagementRegistry(tombstone_path=str(tmp_path / "engagements.json"))
    await engagements.load()
    bus = MessageBus()
    monkeypatch.setattr(tools, "_specialist_registry", specialists, raising=False)
    monkeypatch.setattr(tools, "_engagement_registry", engagements, raising=False)
    monkeypatch.setattr(tools, "_bus", bus, raising=False)
    monkeypatch.setattr(scheduled_asks, "STORE",
                        scheduled_asks.ScheduledAskStore(str(tmp_path / "asks.json")))
    monkeypatch.setattr(run_admission, "CONFIRMATIONS",
                        run_admission.Confirmations(str(tmp_path / "admissions.json")))
    monkeypatch.setattr(plugin_bindings, "BINDINGS",
                        plugin_bindings.BindingStore(str(tmp_path / "bindings.json")))
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    monkeypatch.setattr(plugin_continuations, "CONTINUATIONS",
                        plugin_continuations.Continuations())
    journal = HeldJournal(tmp_path / "held")
    own_voice_lifecycle.install(SimpleNamespace(journal=journal))
    w = SimpleNamespace(jobs=jobs, engagements=engagements, bus=bus, journal=journal,
                        tmp=tmp_path)
    yield w
    own_voice_lifecycle.install(None)
    verdict_broker.BROKER.cancel_all(reason="test over")


# -- the items, one helper per kind ------------------------------------------

def _voice_job(job_id: str, bot: int, **changes) -> VoiceJob:
    system = SpeakerProvenance(speaker_kind="system")
    base = VoiceJob(
        id=job_id, parent_job_id=None,
        creating_speaker=system, executing_speaker=system,
        creating_role="assistant", specialist_role=ROLE,
        specialist_display_name="Alex",
        creator_peer="telegram", creator_user_id=str(OPERATOR),
        scope_id=str(OPERATOR), origin_route_id="cid-1", origin_device_id=None,
        task="the quarter's numbers", context="",
        created_at=100.0, started_at=101.0, terminal_at=None,
        expires_at=None, execution_state=ExecutionState.RUNNING,
        delivery_state=DeliveryState.NONE,
        result=None, failure=None, awaiting_input=False,
        continuable_until=None, delivery_sequence=0,
        delivery_attempt_id=None, lease_until=None, cancel_pending=False,
        route={"_tg_bot": bot},
        executor=ROLE,
        requester=Requester("assistant", {"_tg_bot": bot}),
        delivery=None,
    )
    return replace(base, **changes)


async def _delegation(w, bot: int, job_id: str = "d" * 32, **changes) -> None:
    await w.jobs.create(_voice_job(job_id, bot, **changes))


def _job_state() -> dict:
    import background_jobs
    decl = background_jobs.JobDecl("ledger:work", "ledger", "work", "ledger:work",
                                   "Check the ledger", None)
    return background_jobs.initial_job_state(decl, started_by="operator")


async def _engagement(w, bot: int):
    return await w.engagements.create(
        "specialist", ROLE, "in_casa", "check the ledger",
        {"role": ROLE, "channel": "telegram", "chat_id": str(OPERATOR),
         "user_id": OPERATOR, "job": _job_state(), "_tg_bot": bot},
        executor=ROLE, requester=Requester(ROLE, {"_tg_bot": bot}),
        delivery=TgAddress(bot, OPERATOR))


async def _job_card(w, bot: int) -> None:
    """A job that ended with its keyboard post on *bot* not yet retired."""
    rec = await _engagement(w, bot)
    assert w.engagements.record_job_post(rec.id, TgAddress(bot, OPERATOR, 5),
                                         keyboard=JOB_POST_QUESTION)
    await w.engagements.mark_completed(rec.id, time.time())


async def _scheduled_ask(bot: int, rid: str = "ask-1") -> None:
    await scheduled_asks.STORE.put({
        "rid": rid, "state": scheduled_asks.STATE_LIVE, "role": ROLE,
        "bot_id": bot, "chat_id": OPERATOR, "operator_id": OPERATOR,
        "message_id": 9, "options": ["Yes", "No"], "body": "Pay it?"})


def _run_card(bot: int, cid: str = "c-1", *, card: bool = True,
              status: str = "admitted") -> str:
    """A browsing request asked on *bot*, admitted, with its Stop card there."""
    ident = GrantIdentity(operator_id=OPERATOR, chat_id=OPERATOR, enforcement_role="browser",
                          artifact_id="1" * 64, engagement_id="", bot_id=bot)
    binding = plugin_bindings.BINDINGS.for_turn(
        plugin="browser", turn_key=f"turn-{cid}", identity=ident,
        chain=(ROLE, "browser"),
        origin={"role": ROLE, "chat_id": OPERATOR, "user_id": OPERATOR, "_tg_bot": bot})
    run_admission.CONFIRMATIONS.add(run_admission.Confirmation(
        cid=cid, plugin="browser", bot_id=bot, operator_id=OPERATOR,
        binding_ref=binding.ref, request_key=f"key-{cid}", status=status,
        admissions=[], created_at=time.time(),
        card=[bot, OPERATOR, 7] if card else None, card_text="Fido is browsing."))
    return binding.ref


def _held(w, bot: int) -> None:
    w.journal.append(bot, {"update_id": 1, "message": {"text": "hi"}})


def _broker_ask(bot: int, rid: str = "q-1", *, by_meta: bool = False) -> None:
    scope = (f"elsewhere-{rid}" if by_meta else tg_scope("dm", bot, OPERATOR))
    req, _ = verdict_broker.BROKER.register(
        namespace="resident_ask", scope=scope, request_id=rid, timeout_s=60.0,
        detached=True, meta={"bot_id": bot} if by_meta else {})
    assert req is not None


def _continuation(bot: int, run_ref: str = "run-1") -> None:
    q = plugin_continuations.CONTINUATIONS
    q._pending[("browser", bot, OPERATOR, run_ref)] = plugin_continuations.Pending(
        binding=None, run_ref=run_ref, reason="sweep", queued_at=0.0, bot_id=bot)


class _Answering:
    """Alex's conversation on the bus, with one turn being answered."""

    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __aenter__(self):
        async def handler(msg):
            self.entered.set()
            await self.release.wait()

        self.bus.register(ROLE, handler)
        self.loop = self.bus.start_agent_loop(ROLE)
        await self.bus.send(BusMessage(type=MessageType.NOTIFICATION, source="telegram",
                                       target=ROLE, content="hi", priority=1))
        await asyncio.wait_for(self.entered.wait(), timeout=5)
        return self

    async def __aexit__(self, *exc):
        self.release.set()
        self.bus.unregister(ROLE)
        await asyncio.gather(self.loop, return_exceptions=True)


# -- the predicate -----------------------------------------------------------

async def test_nothing_open_is_an_empty_list(world):
    assert own_voice.open_on_bot(ROLE, BOT) == []
    assert own_voice.open_on_bot(ROLE, BOT, durable_only=True) == []


async def test_a_running_delegation_on_the_bot_is_a_delegated_task(world):
    await _delegation(world, BOT)
    assert own_voice.open_on_bot(ROLE, BOT) == [DELEGATED]
    assert own_voice.open_on_bot(ROLE, BOT, durable_only=True) == [DELEGATED]


async def test_a_delegation_delivering_to_the_bot_counts_wherever_it_was_asked(world):
    await _delegation(world, CASA, execution_state=ExecutionState.ACCEPTED,
                      delivery=TgAddress(BOT, OPERATOR))
    assert own_voice.open_on_bot(ROLE, BOT) == [DELEGATED]


async def test_a_delegation_waiting_for_input_counts_until_it_can_no_longer_continue(world):
    await _delegation(world, BOT, "a" * 32, execution_state=ExecutionState.SUCCEEDED,
                      terminal_at=150.0, awaiting_input=True, continuable_until=NOW + 60)
    assert own_voice.open_on_bot(ROLE, BOT) == [DELEGATED]


async def test_an_ended_delegation_counts_only_while_it_owes_an_announcement(world):
    await _delegation(world, BOT, "a" * 32, execution_state=ExecutionState.SUCCEEDED,
                      terminal_at=150.0, awaiting_input=True, continuable_until=NOW - 1)
    await _delegation(world, BOT, "b" * 32, execution_state=ExecutionState.FAILED,
                      terminal_at=150.0)
    assert own_voice.open_on_bot(ROLE, BOT) == []
    await _delegation(world, BOT, "c" * 32, execution_state=ExecutionState.SUCCEEDED,
                      terminal_at=150.0, terminal_notification_pending=True)
    assert own_voice.open_on_bot(ROLE, BOT) == [DELEGATED]


async def test_a_live_job_on_the_bot_is_a_background_job(world):
    await _engagement(world, BOT)
    assert own_voice.open_on_bot(ROLE, BOT) == [JOB]
    assert own_voice.open_on_bot(ROLE, BOT, durable_only=True) == [JOB]


async def test_an_ended_job_whose_card_is_still_on_the_bot_is_a_jobs_card(world):
    await _job_card(world, BOT)
    assert own_voice.open_on_bot(ROLE, BOT) == [JOB_CARD]
    assert own_voice.open_on_bot(ROLE, BOT, durable_only=True) == [JOB_CARD]


async def test_a_stored_scheduled_question_on_the_bot_counts(world):
    await _scheduled_ask(BOT)
    assert own_voice.open_on_bot(ROLE, BOT, durable_only=True) == [SCHEDULED]


async def test_a_run_card_counts_while_its_stop_still_works(world):
    _run_card(BOT)
    assert own_voice.open_on_bot(ROLE, BOT, durable_only=True) == [RUN_CARD]
    # the run ended on its own: its card was marked and forgotten
    run_admission.CONFIRMATIONS.clear_card("c-1")
    assert own_voice.open_on_bot(ROLE, BOT) == []
    # a card whose binding is gone: its Stop answers nothing any more
    ref = _run_card(BOT, "c-2")
    assert own_voice.open_on_bot(ROLE, BOT) == [RUN_CARD]
    plugin_bindings.BINDINGS._items.pop(ref)
    assert own_voice.open_on_bot(ROLE, BOT) == []
    # a request that was never admitted carries no Stop
    _run_card(BOT, "c-3", status="declined")
    assert own_voice.open_on_bot(ROLE, BOT) == []


async def test_a_message_held_for_the_bot_counts(world):
    _held(world, BOT)
    assert own_voice.open_on_bot(ROLE, BOT, durable_only=True) == [HELD]


async def test_a_live_question_on_the_bot_counts(world):
    _broker_ask(BOT)
    assert own_voice.open_on_bot(ROLE, BOT) == [QUESTION]


async def test_a_queued_continuation_on_the_bot_counts(world):
    _continuation(BOT)
    assert own_voice.open_on_bot(ROLE, BOT) == [CONTINUATION]


async def test_a_turn_the_agent_is_answering_counts(world):
    async with _Answering(world.bus):
        assert own_voice.open_on_bot(ROLE, BOT) == [ANSWERING]
    assert own_voice.open_on_bot(ROLE, BOT) == []


async def test_each_kind_on_the_bot_is_named_once(world):
    await _delegation(world, BOT, "a" * 32)
    await _delegation(world, BOT, "b" * 32)
    await _engagement(world, BOT)
    await _engagement(world, BOT)
    await _job_card(world, BOT)
    await _scheduled_ask(BOT, "ask-1")
    await _scheduled_ask(BOT, "ask-2")
    _run_card(BOT, "c-1")
    _run_card(BOT, "c-2")
    _held(world, BOT)
    _broker_ask(BOT, "q-1")
    _broker_ask(BOT, "q-2", by_meta=True)
    _continuation(BOT, "run-1")
    _continuation(BOT, "run-2")
    async with _Answering(world.bus):
        assert own_voice.open_on_bot(ROLE, BOT) == [
            DELEGATED, JOB, JOB_CARD, SCHEDULED, RUN_CARD, HELD,
            QUESTION, CONTINUATION, ANSWERING]


async def test_items_on_another_bot_do_not_count(world):
    await _delegation(world, OTHER, delivery=TgAddress(OTHER, OPERATOR))
    await _engagement(world, OTHER)
    await _job_card(world, OTHER)
    await _scheduled_ask(OTHER)
    _run_card(OTHER)
    _held(world, OTHER)
    _broker_ask(OTHER, "q-1")
    _broker_ask(OTHER, "q-2", by_meta=True)
    _continuation(OTHER)
    assert own_voice.open_on_bot(ROLE, BOT) == []
    assert len(own_voice.open_on_bot(ROLE, OTHER)) == 8


async def test_durable_only_ignores_memory_items(world):
    _broker_ask(BOT)
    _continuation(BOT)
    async with _Answering(world.bus):
        assert own_voice.open_on_bot(ROLE, BOT) == [QUESTION, CONTINUATION, ANSWERING]
        assert own_voice.open_on_bot(ROLE, BOT, durable_only=True) == []


async def test_a_missing_store_means_nothing_of_that_kind(world, monkeypatch):
    for name in ("_specialist_registry", "_engagement_registry", "_bus"):
        monkeypatch.setattr(tools, name, None, raising=False)
    monkeypatch.setattr(scheduled_asks, "STORE", None)
    assert own_voice.open_on_bot(ROLE, BOT) == []


async def test_a_store_that_fails_to_read_holds_the_change(world, caplog):
    # the held journal's file for the bot cannot be read
    world.journal.path(BOT).mkdir(parents=True)
    await _engagement(world, BOT)
    with caplog.at_level(logging.WARNING, logger="own_voice"):
        assert own_voice.open_on_bot(ROLE, BOT, durable_only=True) == [JOB, UNCHECKED]
    assert any("could not check" in r.getMessage() for r in caplog.records)


# -- the read-only counts the predicate uses ---------------------------------

async def test_the_broker_counts_live_requests_by_bot(world):
    broker = verdict_broker.BROKER
    assert broker.live_on_bot(BOT) == 0
    _broker_ask(BOT, "q-1")                       # by the bot its scope names
    _broker_ask(BOT, "q-2", by_meta=True)         # by its meta's bot
    _broker_ask(OTHER, "q-3")
    req, _ = broker.register(namespace="proposal", scope=tg_scope("proposal", BOT, OPERATOR),
                             request_id="p-1", timeout_s=60.0, detached=True)
    assert broker.live_on_bot(BOT) == 3
    assert broker.live_on_bot(OTHER) == 1
    broker.cancel(namespace="proposal", scope=req.scope, request_id="p-1", reason="done")
    assert broker.live_on_bot(BOT) == 2


async def test_continuations_count_pending_by_bot(world):
    q = plugin_continuations.CONTINUATIONS
    assert q.pending_on_bot(BOT) == 0
    _continuation(BOT, "run-1")
    _continuation(BOT, "run-2")
    _continuation(OTHER, "run-3")
    assert q.pending_on_bot(BOT) == 2
    assert q.pending_on_bot(OTHER) == 1
