"""The plugin runtime's identities are per bot.

One plugin can serve agents on two bots (a registry entry may target
``resident:assistant`` and ``specialist:finance`` together), and everything it
supplies — request keys, run refs, delivery keys, spec digests — can be equal
on both, as can the operator. So every identity the runtime forms from those
values names the bot too: an admission, a Stop, a remembered delivery, a queued
continuation and the operator's words for an action check each stay with the
bot they were made on.

Bots 111 (Casa's own endpoint, ``assistant``) and 222 (``finance``) serve
operator 100, through the two-endpoint harness. The per-bot stores round-trip
their bot, and refuse a stored record without one. Before Casa's bot has an id
(a first boot or a replaced token) a binding whose route is ``CASA`` forms no
identity: nothing is stored, posted or queued, and once the id is verified the
same requests key on it.
"""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

import action_judgement
import delivery_keys
import operator_calls
import plugin_bindings
import plugin_continuations as pc
import plugin_stop
import result_broker as rb
import run_admission
import tools as tools_mod
import verdict_broker as vb
from authz_grants import GrantIdentity
from channels import DeliveryOutcome
from channels.tg_address import CASA, TgAddress
from own_voice_fixtures import open_own_chats, with_delivery
from plugin_grants import PluginContract, ResultContractMap, ToolContract
from telegram_channel_double import MemoryBotIdStore
from two_endpoints import (CASA_BOT, FINANCE_BOT, OPERATOR, FakeEndpoint,
                           build_two_endpoints)

ARTIFACT = "1" * 64
PLUGIN = "browser"
REPORT = "mcp__plugin_browser_api__report"
SLOT = "report"
BODY = "Your invoice is ready."
DIGEST = "sha256:aa"
RUN = {"spec_digest": DIGEST, "mode": "account", "task_kind": "invoice", "task_text": "t",
       "sites": [{"site_id": "runpod", "access": "account", "name": "RunPod"}]}


class _Endpoint(FakeEndpoint):
    """A fake endpoint with the posting methods the plugin runtime uses."""

    async def deliver_operator_message(self, chat_id, text, *, post=None, silent=False):
        self._record("deliver_operator_message", chat_id, text)
        return DeliveryOutcome.DELIVERED if self.is_ready else DeliveryOutcome.NOT_DELIVERED

    async def deliver_operator_question(self, chat_id, text, labels, rid, *, post=None):
        self._record("deliver_operator_question", chat_id, text, rid)
        return self._mid()

    async def deliver_run_card(self, chat_id, text, *, starting=None, post=None, silent=False):
        self._record("deliver_run_card", chat_id, text)
        return self._mid()

    async def edit_dm_message(self, chat_id, message_id, text, buttons=None):
        self._record("edit_dm_message", chat_id, message_id, text)
        return True

    async def send_reply_line(self, chat_id, text, reply_to, *, post=None, silent=False):
        self._record("send_reply_line", chat_id, text, reply_to)
        return True


def _named(ep, name):
    return [args for n, args in ep.calls if n == name]


@pytest.fixture
def stores(monkeypatch, tmp_path):
    monkeypatch.setattr(operator_calls, "CALLS", operator_calls.OperatorCalls())
    monkeypatch.setattr(plugin_bindings, "BINDINGS",
                        plugin_bindings.BindingStore(str(tmp_path / "bindings.json")))
    monkeypatch.setattr(run_admission, "CONFIRMATIONS",
                        run_admission.Confirmations(str(tmp_path / "admissions.json")))
    monkeypatch.setattr(action_judgement, "WORDS", action_judgement.OperatorWords())
    monkeypatch.setattr(delivery_keys, "KEYS",
                        delivery_keys.DeliveryKeys(str(tmp_path / "delivery_keys.json")))
    monkeypatch.setattr(pc, "CONTINUATIONS", pc.Continuations())
    monkeypatch.setattr(vb, "BROKER", vb.VerdictBroker())
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "assistant": SimpleNamespace(character=SimpleNamespace(name="Ellen"), kind="resident"),
        "browser": SimpleNamespace(character=SimpleNamespace(name="Fido"), kind="specialist"),
    }, raising=False)
    # Fido's own chat is open: these tests are about which bot a post keys
    # on, not about whether Fido may post at all
    open_own_chats(monkeypatch, "browser")
    held = asyncio.Event()

    async def _held_turn(pending, *, on_start):
        await held.wait()             # a queued continuation stays queued for the test
    monkeypatch.setattr(pc, "run_continuation", _held_turn)
    return tmp_path


@pytest.fixture
def two(stores, monkeypatch):
    te = build_two_endpoints(_Endpoint("assistant", bot_id=CASA_BOT, is_casa=True),
                             _Endpoint("finance", bot_id=FINANCE_BOT))
    monkeypatch.setattr(tools_mod, "_channel_manager", te.manager, raising=False)
    yield te
    for ep in te.ep.values():
        ep.calls.clear()


@pytest.fixture
def window(stores, monkeypatch):
    """Casa's endpoint before its first ``getMe`` for a new token: no id at all."""
    casa = _Endpoint("assistant", bot_id=None, is_casa=True, ready=False)
    te = build_two_endpoints(casa, _Endpoint("finance", bot_id=FINANCE_BOT),
                             store=MemoryBotIdStore(None))
    monkeypatch.setattr(tools_mod, "_channel_manager", te.manager, raising=False)
    assert te.transport.casa_route() == CASA
    return te


def _identity(bot):
    """Fido's call on *bot*: its posts go to the operator's chat on that bot."""
    return with_delivery(GrantIdentity(
        operator_id=OPERATOR, chat_id=OPERATOR, enforcement_role="browser",
        artifact_id=ARTIFACT, engagement_id="", bot_id=bot))


def _call(route, *, client, words=None, bot=None):
    """A ``callsCasa`` call on a turn whose route is *route*."""
    origin = {"role": "assistant", "execution_role": "browser", "channel": "telegram",
              "chat_id": OPERATOR, "user_id": OPERATOR, "cid": "turn-" + client,
              "message_type": "channel_in", "source": "telegram", "_tg_bot": route,
              "_delegation_id": "d-" + client, "delegation_depth": 1,
              "_handoff_chain": ("assistant", "browser")}
    if words is None:
        origin["synthetic"] = "continuation"
    else:
        origin.update(_operator_words=words, _operator_turn=True)
    return operator_calls.CALLS.open(
        client_id=client, tool_use_id="tu-" + client, tool_name="x", role="browser",
        plugin=PLUGIN, identity=_identity(bot if bot is not None else route), origin=origin)


def _reading(reply):
    async def _ask(words):
        return reply
    return _ask


async def _continue(client, binding_ref, run_ref="run-x"):
    handler = operator_calls.build_continue_handler()
    req = make_mocked_request("POST", "/internal/broker/continue")

    async def _json():
        return {"client": client, "binding_ref": binding_ref, "run_ref": run_ref,
                "reason": "next_run"}
    req.json = _json
    return json.loads((await handler(req)).body)


# -- run admission ----------------------------------------------------------------

async def test_an_equal_request_key_on_two_bots_is_two_requests(two, monkeypatch):
    monkeypatch.setattr(run_admission, "_ask_model", _reading('{"runs": []}'))
    a = await run_admission.request_admission(
        _call(CASA_BOT, client="c1", words="yes"), [RUN], "k1")
    b = await run_admission.request_admission(
        _call(FINANCE_BOT, client="c2", words="yes"), [RUN], "k1")
    assert a["status"] == b["status"] == "confirmation_pending"
    assert a["confirmation_id"] != b["confirmation_id"]
    for bot, answer in ((CASA_BOT, a), (FINANCE_BOT, b)):
        c = run_admission.CONFIRMATIONS.by_key(plugin=PLUGIN, bot_id=bot, operator_id=OPERATOR,
                                               request_key="k1")
        assert c.cid == answer["confirmation_id"] and c.bot_id == bot
        # each card went out through its own bot, once
        assert len(_named(two.ep[bot], "deliver_operator_question")) == 1
    # a replay on one bot is that bot's request again, never the other's
    again = await run_admission.request_admission(
        _call(FINANCE_BOT, client="c3", words="yes"), [RUN], "k1")
    assert again == b
    assert len(_named(two.finance, "deliver_operator_question")) == 1


def _admitted(bot, client, key="k1"):
    call = _call(bot, client=client)
    binding = operator_calls.binding_for(call)
    c = run_admission.Confirmation(
        cid=f"conf-{bot}", plugin=PLUGIN, bot_id=bot, operator_id=OPERATOR,
        binding_ref=binding.ref, request_key=key, status="admitted", admissions=[],
        created_at=0.0, card=[bot, OPERATOR, 700 + bot % 100], card_text="Browsing now:")
    run_admission.CONFIRMATIONS.add(c)
    return c, binding


async def test_a_stop_on_one_bot_leaves_the_other_bots_request(two, monkeypatch):
    c111, b111 = _admitted(CASA_BOT, "c1")
    c222, b222 = _admitted(FINANCE_BOT, "c2")
    for b in (b111, b222):
        assert pc.CONTINUATIONS.request(b, "run-x", "sweep") == "queued"

    async def _plugins():
        return [("browser", None, SimpleNamespace(artifact_id=ARTIFACT), PLUGIN, "stop_runs")]

    async def _stopped(rp, tool, args):
        return {"outcome": "stopped", "refs": ["run-x"]}
    monkeypatch.setattr(plugin_stop, "stop_plugins", _plugins)
    monkeypatch.setattr(plugin_stop, "call_stop", _stopped)

    async def _tell(outcome):
        await plugin_stop.settle_run_card(
            plugin_stop.confirmation_for(PLUGIN, FINANCE_BOT, OPERATOR, "request:k1"),
            plugin_stop.MARKS[outcome["outcome"]])

    out = await plugin_stop.stop("browser", PLUGIN, "request:k1", bot_id=FINANCE_BOT,
                                 operator_id=OPERATOR, tell=_tell)
    assert out["outcome"] == "stopped"
    # 222's card was marked on 222; nothing at all went out on 111
    assert [a[1] for a in _named(two.finance, "edit_dm_message")] == [722]
    assert _named(two.casa, "edit_dm_message") == []
    # 222's queued continuation is gone; 111's is still queued
    assert pc.CONTINUATIONS.request(b222, "run-x", "sweep") == "queued"
    assert pc.CONTINUATIONS.request(b111, "run-x", "sweep") == "already_queued"
    assert plugin_stop.confirmation_for(PLUGIN, CASA_BOT, OPERATOR, "request:k1") is not None
    await pc.CONTINUATIONS.drain()


async def test_a_stop_in_flight_on_one_bot_does_not_hold_the_other(two, monkeypatch):
    _admitted(CASA_BOT, "c1")
    _admitted(FINANCE_BOT, "c2")
    gate = asyncio.Event()

    async def _plugins():
        return [("browser", None, SimpleNamespace(artifact_id=ARTIFACT), PLUGIN, "stop_runs")]

    async def _slow(rp, tool, args):
        await gate.wait()
        return {"outcome": "ended"}
    monkeypatch.setattr(plugin_stop, "stop_plugins", _plugins)
    monkeypatch.setattr(plugin_stop, "call_stop", _slow)
    first = asyncio.create_task(plugin_stop.stop("browser", PLUGIN, "request:k1",
                                                 bot_id=FINANCE_BOT, operator_id=OPERATOR))
    await asyncio.sleep(0)
    second = asyncio.create_task(plugin_stop.stop("browser", PLUGIN, "request:k1",
                                                  bot_id=CASA_BOT, operator_id=OPERATOR))
    again = await plugin_stop.stop("browser", PLUGIN, "request:k1", bot_id=FINANCE_BOT,
                                   operator_id=OPERATOR)
    assert again == {"outcome": "in_flight"}
    gate.set()
    assert (await first)["outcome"] == "ended" and (await second)["outcome"] == "ended"


# -- delivery keys ----------------------------------------------------------------

def _map():
    tools = {REPORT: ToolContract(ARTIFACT, PLUGIN, "capability", (SLOT,), {},
                                  {SLOT: "operator_message"})}
    return ResultContractMap(tools=tools, plugins={PLUGIN: PluginContract(ARTIFACT, True,
                                                                          frozenset())})


async def _deliver(bot, call_id, key="k"):
    store = rb.ReferenceStore(now=lambda: 1000.0)
    hook = rb.make_result_hook(_map(), client_id="c1", store=store)
    store.open_call(client_id="c1", artifact_id=ARTIFACT, tool_name=REPORT,
                    tool_use_id=call_id, identity=_identity(bot), provides=(SLOT,),
                    delivers={SLOT: "operator_message"})
    ref, err = store.deposit(client_id="c1", slot=SLOT, value=BODY, key=key)
    assert err is None
    out = await hook({"hook_event_name": "PostToolUse", "tool_name": REPORT,
                      "tool_input": {}, "tool_response": json.dumps({SLOT: ref})}, call_id, {})
    return json.loads(out["hookSpecificOutput"]["updatedToolOutput"])


async def test_an_equal_delivery_key_on_two_bots_delivers_on_both(two):
    a = await _deliver(CASA_BOT, "call-1")
    b = await _deliver(FINANCE_BOT, "call-2")
    for receipt in (a, b):
        assert receipt["casa_delivery"]["status"] == "delivered"
        assert "repeat" not in receipt["casa_delivery"]
    assert len(_named(two.casa, "deliver_operator_message")) == 1
    assert len(_named(two.finance, "deliver_operator_message")) == 1
    # the key is still remembered per bot: on 111 again it is a repeat
    again = await _deliver(CASA_BOT, "call-3")
    assert again["casa_delivery"]["repeat"] is True
    assert len(_named(two.casa, "deliver_operator_message")) == 1


# -- the continuation queue -------------------------------------------------------

async def test_an_equal_run_ref_on_two_bots_is_queued_twice_and_dropped_apart(two):
    b111 = operator_calls.binding_for(_call(CASA_BOT, client="c1"))
    b222 = operator_calls.binding_for(_call(FINANCE_BOT, client="c2"))
    assert pc.CONTINUATIONS.request(b111, "run-x", "sweep") == "queued"
    assert pc.CONTINUATIONS.request(b222, "run-x", "sweep") == "queued"
    assert pc.CONTINUATIONS.pending_count() == 2
    assert pc.CONTINUATIONS.drop(PLUGIN, FINANCE_BOT, OPERATOR, ["run-x"]) == 1
    assert pc.CONTINUATIONS.request(b111, "run-x", "sweep") == "already_queued"
    assert pc.CONTINUATIONS.request(b222, "run-x", "sweep") == "queued"
    await pc.CONTINUATIONS.drain()


# -- the operator's words ---------------------------------------------------------

async def test_words_recorded_on_one_bot_are_not_read_for_the_other(two, monkeypatch):
    monkeypatch.setattr(run_admission, "_ask_model", _reading('{"runs": [{"site": "RunPod"}]}'))
    out = await run_admission.request_admission(
        _call(CASA_BOT, client="c1", words="get my RunPod invoice"), [RUN], "k1")
    assert out["status"] == "admitted"
    bodies: list = []

    async def _ask(rulebook, body, picture=None):
        bodies.append(json.loads(body)["context"]["operator_words"])
        return json.dumps({"verdict": "go", "category": "reading", "reason": "r",
                           "look_first": "", "question": None})
    monkeypatch.setattr(action_judgement, "_ask_check", _ask)
    packet = json.dumps({"action": {"kind": "click"}})
    await action_judgement.judge(_call(FINANCE_BOT, client="c2"), "action", packet, DIGEST)
    await action_judgement.judge(_call(CASA_BOT, client="c3"), "action", packet, DIGEST)
    assert bodies == [None, "get my RunPod invoice"]


# -- the stores keep the bot across a restart -------------------------------------

def test_a_binding_keeps_its_route_including_casa(stores):
    path = str(stores / "bindings.json")
    store = plugin_bindings.BindingStore(path)
    kept = {}
    for route in (FINANCE_BOT, CASA):
        b = store.for_turn(plugin=PLUGIN, turn_key=f"t-{route}", identity=_identity(CASA_BOT),
                           chain=("assistant", "browser"),
                           origin={"chat_id": OPERATOR, "_tg_bot": route})
        kept[b.ref] = route
    reloaded = plugin_bindings.BindingStore(path)
    for ref, route in kept.items():
        assert reloaded.peek(ref).origin["_tg_bot"] == route


def test_a_binding_minted_without_a_route_keeps_the_identitys_bot(stores):
    store = plugin_bindings.BindingStore(str(stores / "bindings.json"))
    b = store.for_turn(plugin=PLUGIN, turn_key="t", identity=_identity(FINANCE_BOT),
                       chain=("assistant", "browser"), origin={"chat_id": OPERATOR})
    assert b.origin["_tg_bot"] == FINANCE_BOT


def test_a_stored_binding_without_a_route_is_refused(stores, caplog):
    path = stores / "bindings.json"
    entry = {"ref": "casa-bind-" + "a" * 32, "plugin": PLUGIN, "operator_id": OPERATOR,
             "chat_id": OPERATOR, "requester": "assistant", "requester_kind": "operator_resident",
             "chain": ["assistant", "browser"], "origin": {"chat_id": OPERATOR},
             "turn_key": "t", "created_at": 1e12, "last_used": 1e12, "delivery": None}
    good = {**entry, "ref": "casa-bind-" + "b" * 32, "origin": {"_tg_bot": CASA_BOT}}
    path.write_text(json.dumps([entry, good]))
    with caplog.at_level(logging.ERROR, logger="plugin_bindings"):
        store = plugin_bindings.BindingStore(str(path))
        assert store.peek(entry["ref"]) is None
    assert store.peek(good["ref"]) is not None
    assert any("without a Telegram route" in r.message for r in caplog.records)


def test_a_confirmation_keeps_its_bot_and_card_and_one_without_a_bot_is_refused(stores, caplog):
    path = str(stores / "admissions.json")
    store = run_admission.Confirmations(path)
    store.add(run_admission.Confirmation(
        cid="conf-1", plugin=PLUGIN, bot_id=FINANCE_BOT, operator_id=OPERATOR,
        binding_ref="b", request_key="k", status="admitted", admissions=[], created_at=1e12,
        card=[FINANCE_BOT, OPERATOR, 7], card_text="t"))
    reloaded = run_admission.Confirmations(path)
    c = reloaded.by_key(plugin=PLUGIN, bot_id=FINANCE_BOT, operator_id=OPERATOR,
                        request_key="k")
    assert c.bot_id == FINANCE_BOT and c.card_address() == TgAddress(FINANCE_BOT, OPERATOR, 7)
    assert reloaded.by_key(plugin=PLUGIN, bot_id=CASA_BOT, operator_id=OPERATOR,
                           request_key="k") is None
    raw = json.loads((stores / "admissions.json").read_text())
    old = {k: v for k, v in raw[0].items() if k not in ("bot_id", "card")}
    old.update(cid="conf-old", card_message_id=7)
    torn = {**raw[0], "cid": "conf-torn", "card": [OPERATOR, 7]}
    (stores / "admissions.json").write_text(json.dumps([raw[0], old, torn]))
    with caplog.at_level(logging.ERROR, logger="run_admission"):
        reloaded = run_admission.Confirmations(path)
        assert reloaded.peek("conf-1") is not None
        assert reloaded.peek("conf-old") is None and reloaded.peek("conf-torn") is None
    assert any("refused" in r.message for r in caplog.records)


def test_delivery_keys_keep_their_bot_and_an_entry_without_one_is_refused(stores, caplog):
    path = str(stores / "keys.json")
    keys = delivery_keys.DeliveryKeys(path)
    keys.record(PLUGIN, FINANCE_BOT, OPERATOR, "k", kind="operator_message",
                detail={"pages": 1})
    keys.record(PLUGIN, CASA_BOT, OPERATOR, "p", kind="operator_proposal",
                detail={"proposal_id": "rid", "buttons": 2},
                proposal=(f"proposal:{CASA_BOT}:{OPERATOR}", "rid"))
    reloaded = delivery_keys.DeliveryKeys()
    reloaded.load(path)
    assert reloaded.lookup(PLUGIN, FINANCE_BOT, OPERATOR, "k") is not None
    assert reloaded.lookup(PLUGIN, CASA_BOT, OPERATOR, "k") is None
    assert reloaded.lookup(PLUGIN, CASA_BOT, OPERATOR, "p")["proposal"] == [
        f"proposal:{CASA_BOT}:{OPERATOR}", "rid"]
    raw = json.loads((stores / "keys.json").read_text())
    entry = raw[json.dumps([PLUGIN, FINANCE_BOT, OPERATOR, "k"])]
    old_proposal = dict(raw[json.dumps([PLUGIN, CASA_BOT, OPERATOR, "p"])],
                        proposal=[f"proposal:{OPERATOR}", "rid"])
    (stores / "keys.json").write_text(json.dumps({
        json.dumps([PLUGIN, OPERATOR, "k"]): entry,                    # written without a bot
        json.dumps([PLUGIN, CASA_BOT, OPERATOR, "p"]): old_proposal,  # an old proposal scope
        json.dumps([PLUGIN, FINANCE_BOT, OPERATOR, "k"]): entry}))
    with caplog.at_level(logging.WARNING, logger="delivery_keys"):
        reloaded.load(path)
    assert reloaded.lookup(PLUGIN, FINANCE_BOT, OPERATOR, "k") is not None
    assert reloaded.lookup(PLUGIN, CASA_BOT, OPERATOR, "p") is None
    assert len(reloaded._entries) == 1
    assert any(r.levelno == logging.ERROR and "without a bot" in r.message
               for r in caplog.records)


# -- before Casa's bot has an id ----------------------------------------------------------

async def test_before_casas_bot_has_an_id_nothing_is_keyed_stored_or_posted(window, monkeypatch):
    monkeypatch.setattr(run_admission, "_ask_model", _reading('{"runs": []}'))
    casa = window.casa
    call = _call(CASA, client="c1", words="yes", bot=CASA_BOT)
    # an admission stores no confirmation and posts no card
    out = await run_admission.request_admission(call, [RUN], "k1")
    assert out["status"] == "refused" and out["reason"] == "not_delivered"
    assert run_admission.CONFIRMATIONS.by_key(plugin=PLUGIN, bot_id=CASA_BOT,
                                              operator_id=OPERATOR, request_key="k1") is None
    assert run_admission.CONFIRMATIONS._items == {}
    # a capability delivery writes no key and is withheld as not delivered
    receipt = await _deliver(CASA_BOT, "call-1")
    assert receipt.get("casa_result_withheld") is True
    assert delivery_keys.KEYS._entries == {}
    # a continuation request is refused and queues nothing
    binding = operator_calls.binding_for(call)
    assert binding.origin["_tg_bot"] == CASA
    assert await _continue("c1", binding.ref) == {"error": "channel_unavailable"}
    assert pc.CONTINUATIONS.pending_count() == 0
    assert casa.calls == [] and window.finance.calls == []

    # Casa's bot verifies its id: the same three now key on it
    assert casa.initialize(CASA_BOT)
    out = await run_admission.request_admission(call, [RUN], "k1")
    assert out["status"] == "confirmation_pending"
    c = run_admission.CONFIRMATIONS.peek(out["confirmation_id"])
    assert c.bot_id == CASA_BOT
    assert len(_named(casa, "deliver_operator_question")) == 1
    receipt = await _deliver(CASA_BOT, "call-2")
    assert receipt["casa_delivery"]["status"] == "delivered"
    assert list(delivery_keys.KEYS._entries) == [json.dumps([PLUGIN, CASA_BOT, OPERATOR, "k"])]
    assert await _continue("c1", binding.ref) == {"status": "queued"}
    assert pc.CONTINUATIONS.pending_count() == 1
    assert window.finance.calls == []
    await pc.CONTINUATIONS.drain()
