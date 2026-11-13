"""An agent's surface lives at its task's delivery, in its own open chat, and
nowhere else; a continuation's result goes to its requester; a plugin's
callback follows the operation its sign-in link belongs to.

Driven through the real seams: the plugin admission hook and the result hook
(``result_broker``) under a real grant identity derived from the turn's
origin, run admission's card (``run_admission``), the Stop line
(``plugin_stop``), a job's parking line (``background_jobs``), a continuation
(``plugin_continuations``) rebuilt from a binding that survives a reload, and
the callback ingress (``callback_episodes``) over a real spool. The two bots
are the two-endpoint harness: Casa's (111, the resident's chat) and Alex's
own (222). The own-chat state is ``own_voice.require_open_chat``'s, replaced
here by a table with its documented answers."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import agent as agent_mod
import agent_lane
import background_jobs
import callback_attempts
import callback_episodes as ce
import callback_owner
import callback_spool
import chat_notes
import operator_calls
import own_voice
import plugin_bindings
import plugin_continuations as pc
import plugin_stop
import result_broker as rb
import run_admission
import tools as tools_mod
from authz_grants import GrantIdentity
from channels import DeliveryOutcome
from channels.tg_address import TgAddress
from handoff import child_chain
from plugin_grants import PluginContract, ResultContractMap, ToolContract
from provenance import task_delivery_marker
from tools import DelegatedOutput
from two_endpoints import CASA_BOT, FINANCE_BOT, OPERATOR, FakeEndpoint, build_two_endpoints

ARTIFACT = "5" * 64
PLUGIN = "demo"
LINK = "mcp__plugin_demo_api__sign_in"            # capability, delivers a link
NOTE = "mcp__plugin_demo_api__note"               # capability, delivers a message
CARD = "mcp__plugin_demo_api__card"               # capability, delivers a proposal
RUNS = "mcp__plugin_demo_api__start_runs"         # asks Casa (run card, Stop)
READ = "mcp__plugin_demo_api__read"               # safe: posts nothing
URL = "https://bank.example.com/approve?s=1"
ALEX_CHAT = TgAddress(FINANCE_BOT, OPERATOR)
ELLEN_CHAT = TgAddress(CASA_BOT, OPERATOR)
STATES = ("not_declared", "no_bot", "not_open", "open", "dormant")


class Endpoint(FakeEndpoint):
    """The harness endpoint, with the operator-surface methods Casa posts through."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # (method, options) of each post that can be sent without a notification
        self.options: list[tuple[str, dict]] = []

    async def deliver_operator_link(self, chat_id, text, entities, plain, *, post=None,
                                    button=None):
        self._record("deliver_operator_link", chat_id, text)
        return DeliveryOutcome.DELIVERED

    async def deliver_operator_message(self, chat_id, text, *, post=None, **options):
        self._record("deliver_operator_message", chat_id, text)
        self.options.append(("deliver_operator_message", options))
        return DeliveryOutcome.DELIVERED

    async def deliver_operator_page(self, chat_id, text, *, post=None):
        self._record("deliver_operator_page", chat_id, text)
        return DeliveryOutcome.DELIVERED

    async def deliver_operator_proposal(self, chat_id, text, labels, rid, *, post=None,
                                        photo=None, urls=None):
        self._record("deliver_operator_proposal", chat_id, text)
        return self._mid()

    async def deliver_operator_question(self, chat_id, text, labels, rid, *, post=None):
        self._record("deliver_operator_question", chat_id, text)
        return self._mid()

    async def deliver_run_card(self, chat_id, text, *, post=None, **options):
        self._record("deliver_run_card", chat_id, text)
        self.options.append(("deliver_run_card", options))
        return self._mid()

    async def send_reply_line(self, chat_id, text, reply_to, *, post=None, silent=False):
        self._record("send_reply_line", chat_id, text)

    async def edit_dm_message(self, chat_id, message_id, text, buttons=None):
        self._record("edit_dm_message", chat_id, text)
        return True


def sends(ep) -> list[tuple]:
    return [c for c in ep.calls if c[0] != "update"]


def _map() -> ResultContractMap:
    return ResultContractMap(
        tools={
            LINK: ToolContract(ARTIFACT, PLUGIN, "capability", ("link",), {},
                               {"link": "operator_link"}, servers=("api",)),
            NOTE: ToolContract(ARTIFACT, PLUGIN, "capability", ("note",), {},
                               {"note": "operator_message"}, servers=("api",)),
            CARD: ToolContract(ARTIFACT, PLUGIN, "capability", ("card",), {},
                               {"card": "operator_proposal"}, servers=("api",)),
            RUNS: ToolContract(ARTIFACT, PLUGIN, "safe", (), {}, calls_casa=True),
            READ: ToolContract(ARTIFACT, PLUGIN, "safe", (), {}),
        },
        plugins={PLUGIN: PluginContract(ARTIFACT, True, frozenset(), name=PLUGIN)})


DEPOSITS = {LINK: ("link", URL), NOTE: ("note", "Groceries in October: EUR 420."),
            CARD: ("card", json.dumps({"text": "Keep this?",
                                       "buttons": [{"label": "Close", "close": True}]}))}


def _base(role: str, execution: str, bot: int, **extra) -> dict:
    return {"role": role, "execution_role": execution, "channel": "telegram",
            "source": "telegram", "message_type": "channel_in", "chat_id": OPERATOR,
            "user_id": OPERATOR, "cid": "c-1", "_origin_route": "telegram",
            "_origin_clearance": "private", "_tg_bot": bot, **extra}


def ellen_delegates(executor: str, delivery: "TgAddress | None") -> dict:
    """A task Ellen handed to *executor*: her route, the task's birth-time delivery."""
    return _base("assistant", executor, CASA_BOT, _delegation_id="d-1",
                 delegation_depth=1, _handoff_chain=("assistant", executor),
                 **task_delivery_marker(delivery))


def colleague_asks(executor: str, delivery: "TgAddress | None", **extra) -> dict:
    """A task Ellen handed to Tina, who asked *executor* for help with it."""
    return _base("assistant", executor, CASA_BOT, _delegation_id="d-2",
                 delegation_depth=2, _handoff_chain=("assistant", "butler", executor),
                 **task_delivery_marker(delivery), **extra)


def own_turn(role: str, bot: int) -> dict:
    """*role*'s own conversation turn, in its own chat on bot *bot*."""
    return _base(role, role, bot, _operator_turn=True)


class _Origin:
    def __init__(self, origin):
        self.origin = origin

    def __enter__(self):
        self._o = agent_mod.origin_var.set(self.origin)
        self._e = tools_mod.engagement_var.set(None)

    def __exit__(self, *exc):
        agent_mod.origin_var.reset(self._o)
        tools_mod.engagement_var.reset(self._e)


@pytest.fixture
def world(monkeypatch, tmp_path):
    import authz_grants
    casa = Endpoint("assistant", bot_id=CASA_BOT, is_casa=True)
    alex = Endpoint("finance", bot_id=FINANCE_BOT)
    two = build_two_endpoints(casa=casa, finance=alex)
    monkeypatch.setattr(tools_mod, "_channel_manager", two.manager, raising=False)
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "assistant": SimpleNamespace(character=SimpleNamespace(name="Ellen"), kind="resident",
                                     role="assistant"),
        "finance": SimpleNamespace(character=SimpleNamespace(name="Alex"), kind="specialist",
                                   role="finance"),
        "butler": SimpleNamespace(character=SimpleNamespace(name="Tina"), kind="specialist",
                                  role="butler"),
    }, raising=False)
    monkeypatch.setattr(authz_grants, "_live_operator_identity",
                        lambda: (OPERATOR, OPERATOR))
    monkeypatch.setattr(operator_calls, "CALLS", operator_calls.OperatorCalls())
    monkeypatch.setattr(plugin_bindings, "BINDINGS",
                        plugin_bindings.BindingStore(str(tmp_path / "bindings.json")))
    monkeypatch.setattr(run_admission, "CONFIRMATIONS",
                        run_admission.Confirmations(str(tmp_path / "admissions.json")))
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    agent_lane.reset()
    chat_notes.reset()
    callback_owner.reset()
    state = {"finance": "open", "butler": "not_declared"}

    def require_open_chat(role, *, label):
        # own_voice's documented answers: the resident's chat always exists;
        # any other agent posts only while its own chat is open
        if role == "assistant":
            return None
        s = state.get(role, "not_declared")
        if s == "open":
            return None
        kind = {"not_declared": "own_chat_missing", "no_bot": "own_chat_missing",
                "not_open": "own_chat_not_open", "dormant": "own_chat_unreachable"}[s]
        return own_voice.Refusal(kind, f"{label}'s chat is {s.replace('_', ' ')}")

    monkeypatch.setattr(own_voice, "require_open_chat", require_open_chat)
    yield SimpleNamespace(casa=casa, alex=alex, state=state, tmp=tmp_path,
                          transport=two.transport)
    agent_lane.reset()
    chat_notes.reset()
    callback_owner.reset()


def _pre(tool):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}}


def _post(tool, response):
    return {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": {},
            "tool_response": response}


async def call_tool(role: str, origin: dict, tool: str, *, client="c1", tu="tu-1"):
    """One plugin call of *role* on a turn of *origin*, through the real
    admission and result hooks: ``("denied", reason)``, ``("delivered", receipt)``
    or ``("withheld", body)``."""
    store = rb.ReferenceStore()
    admit = rb.make_plugin_admission_hook(role, _map(), client_id=client, store=store)
    result = rb.make_result_hook(_map(), client_id=client, store=store)
    with _Origin(origin):
        verdict = await admit(_pre(tool), tu, {})
        if verdict:
            return "denied", verdict["hookSpecificOutput"]["permissionDecisionReason"]
        if tool not in DEPOSITS:
            return "admitted", None
        slot, value = DEPOSITS[tool]
        ref, err = store.deposit(client_id=client, slot=slot, value=value)
        assert err is None, err
        out = await result(_post(tool, json.dumps({slot: ref})), tu, {})
    body = json.loads(out["hookSpecificOutput"]["updatedToolOutput"])
    if body.get("casa_result_withheld"):
        return "withheld", body
    return "delivered", body


# -- the capability rule, across the own-chat states ----------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [LINK, NOTE, CARD])
@pytest.mark.parametrize("state", STATES)
async def test_a_delegated_agents_posts_land_only_in_its_own_open_chat(world, tool, state):
    """Ellen delegated to Alex. Only an open own chat admits his post, and it lands at the
    task's delivery, Alex's chat — with zero sends through Ellen's bot in every state."""
    world.state["finance"] = state
    outcome, detail = await call_tool("finance", ellen_delegates("finance", ALEX_CHAT), tool)
    assert sends(world.casa) == []
    if state == "open":
        assert outcome == "delivered"
        assert [c[1][0] for c in sends(world.alex)] == [OPERATOR]
        assert detail["casa_delivery"]["status"] == "delivered"
    else:
        assert outcome == "denied"
        assert detail == f"not executed: Alex's chat is {state.replace('_', ' ')}"
        assert sends(world.alex) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [LINK, NOTE, CARD, RUNS])
async def test_an_agent_behind_ellen_has_no_surface_at_all(world, tool):
    """Tina never declared her own chat: every surface of hers refuses, typed, and nothing
    reaches either bot — Ellen's chat is never the fall-back."""
    outcome, why = await call_tool("butler", ellen_delegates("butler", None), tool)
    assert (outcome, why) == ("denied", "not executed: Tina's chat is not declared")
    assert sends(world.casa) == [] and sends(world.alex) == []
    assert operator_calls.CALLS.find("c1") == (None, "no_call_in_flight")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [LINK, NOTE, CARD, RUNS])
async def test_a_task_born_before_the_chat_opened_posts_nothing_even_once_it_is_open(world,
                                                                                    tool):
    """The task's delivery is decided at birth: born while Alex's chat was closed it is
    None, and stays None although the chat is open now."""
    outcome, why = await call_tool("finance", ellen_delegates("finance", None), tool)
    assert outcome == "denied"
    assert why == ("not executed: Alex's chat was not open when this task started, so "
                   "nothing from this task can be shown to the operator. Put what matters "
                   "in your answer instead; do not retry.")
    assert sends(world.casa) == [] and sends(world.alex) == []


@pytest.mark.asyncio
async def test_a_safe_tool_is_not_a_surface(world):
    world.state["finance"] = "no_bot"
    assert await call_tool("finance", ellen_delegates("finance", None), READ) == (
        "admitted", None)


@pytest.mark.asyncio
async def test_ellens_own_plugin_posts_in_her_chat_under_her_label(world):
    outcome, _ = await call_tool("assistant", own_turn("assistant", CASA_BOT), LINK)
    assert outcome == "delivered"
    assert sends(world.alex) == []
    [(name, (chat, text))] = sends(world.casa)
    assert (name, chat) == ("deliver_operator_link", OPERATOR)
    assert text.startswith("\U0001F4CA Ellen\n")


@pytest.mark.asyncio
async def test_in_an_agents_own_chat_its_posts_carry_no_label(world):
    """Alex's own conversation turn: its delivery is its own chat, and the post there is
    not headed with his name — the chat is his."""
    outcome, _ = await call_tool("finance", own_turn("finance", FINANCE_BOT), NOTE)
    assert outcome == "delivered"
    assert sends(world.casa) == []
    [(name, (chat, text))] = sends(world.alex)
    assert (name, chat, text) == ("deliver_operator_message", OPERATOR,
                                  "Groceries in October: EUR 420.")


@pytest.mark.asyncio
async def test_a_card_records_where_it_was_delivered_and_who_executes_it(world):
    import verdict_broker
    outcome, detail = await call_tool("finance", ellen_delegates("finance", ALEX_CHAT), CARD)
    assert outcome == "delivered"
    rid = detail["casa_delivery"]["proposal_id"]
    meta = verdict_broker.BROKER.get_meta(
        namespace="proposal", scope=f"proposal:{FINANCE_BOT}:{OPERATOR}", request_id=rid)
    assert meta["delivery"] == ALEX_CHAT and meta["role"] == "finance"
    assert (meta["bot_id"], meta["chat_id"]) == (FINANCE_BOT, OPERATOR)


@pytest.mark.asyncio
async def test_the_delivery_backstop_withholds_when_the_chat_closed_after_admission(world):
    """Admitted while the chat was open; by the time the result is delivered it is not:
    nothing is posted anywhere and the call is told why."""
    store = rb.ReferenceStore()
    admit = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", store=store)
    result = rb.make_result_hook(_map(), client_id="c1", store=store)
    with _Origin(ellen_delegates("finance", ALEX_CHAT)):
        assert await admit(_pre(LINK), "tu-1", {}) == {}
        ref, _ = store.deposit(client_id="c1", slot="link", value=URL)
        world.state["finance"] = "dormant"
        out = await result(_post(LINK, json.dumps({"link": ref})), "tu-1", {})
    body = json.loads(out["hookSpecificOutput"]["updatedToolOutput"])
    assert body["casa_result_withheld"] is True
    assert body["reason"] == "Alex's chat is dormant"
    assert sends(world.casa) == [] and sends(world.alex) == []


@pytest.mark.asyncio
async def test_a_faulted_agent_starts_no_plugin_operation(world):
    agent_lane.FAULTS.mark("finance", "run-1", PLUGIN, reason="unconfirmed")
    outcome, why = await call_tool("finance", own_turn("finance", FINANCE_BOT), READ)
    assert outcome == "denied" and "Alex is out of service until Casa restarts." in why
    # another agent is unaffected
    assert (await call_tool("assistant", own_turn("assistant", CASA_BOT), READ))[0] == "admitted"


# -- run card and Stop -------------------------------------------------------------------------

RUN = {"spec_digest": "sha256:aa", "mode": "account", "task_kind": "invoice", "task_text": "t",
       "sites": [{"site_id": "runpod", "access": "account", "name": "RunPod"}]}


@pytest.mark.asyncio
async def test_the_run_card_and_its_stop_line_follow_the_tasks_delivery(world, monkeypatch):
    monkeypatch.setattr(pc, "CONTINUATIONS", SimpleNamespace(request=lambda *a: "queued"))
    outcome, _ = await call_tool("finance", ellen_delegates("finance", ALEX_CHAT), RUNS)
    assert outcome == "admitted"
    call, err = operator_calls.CALLS.find("c1")
    assert err is None
    out = await run_admission.request_admission(call, [RUN], "k1")
    assert out["status"] == "confirmation_pending"
    c = run_admission.CONFIRMATIONS.peek(out["confirmation_id"])
    assert c.bot_id == FINANCE_BOT              # a Stop on Alex's bot finds it
    [(name, (chat, text))] = sends(world.alex)
    assert (name, chat) == ("deliver_operator_question", OPERATOR)
    assert not text.startswith("\U0001F4CA")    # his own chat: no label
    await plugin_stop.tell_line(c.bot_id, OPERATOR, "finance", "stopped", reply_to=None)
    assert sends(world.alex)[-1] == ("send_reply_line", (OPERATOR, "Stopped."))
    assert sends(world.casa) == []


@pytest.mark.asyncio
async def test_a_plugin_question_records_its_agent_and_where_it_was_delivered(world,
                                                                             monkeypatch):
    """The callback ingress reads a card's executing agent from its record: a run
    admission's question records Alex and his chat, like every card of his."""
    import verdict_broker
    monkeypatch.setattr(pc, "CONTINUATIONS", SimpleNamespace(request=lambda *a: "queued"))
    assert (await call_tool("finance", ellen_delegates("finance", ALEX_CHAT), RUNS))[0] == \
        "admitted"
    call, _ = operator_calls.CALLS.find("c1")
    await run_admission.request_admission(call, [RUN], "k1")
    scope = f"ask:{FINANCE_BOT}:{OPERATOR}"
    [rid] = verdict_broker.BROKER.pending(namespace="resident_ask", scope=scope)
    meta = verdict_broker.BROKER.get_meta(namespace="resident_ask", scope=scope,
                                          request_id=rid)
    assert meta["kind"] == "casa_question"
    assert (meta["role"], meta["delivery"]) == ("finance", ALEX_CHAT)


@pytest.mark.asyncio
async def test_run_admission_with_no_delivery_posts_nothing(world):
    ident = GrantIdentity(operator_id=OPERATOR, bot_id=CASA_BOT, chat_id=OPERATOR,
                          enforcement_role="finance", artifact_id=ARTIFACT, engagement_id="",
                          delivery=None)
    call = operator_calls.CALLS.open(client_id="c1", tool_use_id="tu", tool_name=RUNS,
                                     role="finance", plugin=PLUGIN, identity=ident,
                                     origin=ellen_delegates("finance", None))
    out = await run_admission.request_admission(call, [RUN], "k1")
    assert (out["status"], out["reason"]) == ("refused", "not_delivered")
    assert sends(world.casa) == [] and sends(world.alex) == []


# -- a helper's posts under a task the operator started do not notify --------------------------

UNDER_HIS_TASK = {"_operator_task": True}
TURNS = [
    pytest.param(lambda: colleague_asks("finance", ALEX_CHAT, **UNDER_HIS_TASK), True,
                 id="a-colleague-asked-under-the-operators-task"),
    pytest.param(lambda: colleague_asks("finance", ALEX_CHAT), False,
                 id="a-colleague-asked-outside-it"),
    pytest.param(lambda: colleague_asks("finance", ALEX_CHAT, _operator_task="yes"), False,
                 id="a-mark-that-is-not-true"),
    pytest.param(lambda: ellen_delegates("finance", ALEX_CHAT) | UNDER_HIS_TASK, False,
                 id="the-resident-asked-under-the-operators-task"),
    pytest.param(lambda: own_turn("finance", FINANCE_BOT), False, id="its-own-chat"),
]


@pytest.fixture
def message_posts(monkeypatch):
    """What the result broker hands its message poster: ``(to, post, options)``."""
    seen: list[tuple] = []
    real = rb._post_operator_message

    async def _spy(to, text, post=None, **options):
        seen.append((to, post, options))
        return await real(to, text, post, **options)

    monkeypatch.setattr(rb, "_post_operator_message", _spy)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("turn,silent", TURNS)
async def test_a_plugin_message_notifies_unless_a_colleague_asked_under_the_operators_task(
        world, message_posts, turn, silent):
    """The message is delivered and filed the same either way; only the
    notification differs."""
    outcome, detail = await call_tool("finance", turn(), NOTE)
    assert outcome == "delivered" and detail["casa_delivery"]["status"] == "delivered"
    [(to, post, options)] = message_posts
    assert options == {"silent": silent}
    assert to == ALEX_CHAT
    assert (post.role, post.operator_id, post.plugin, post.slot, post.tool_use_id,
            post.kind) == ("finance", OPERATOR, PLUGIN, "note", "tu-1", "operator_message")
    assert world.alex.options == [("deliver_operator_message", {"silent": silent})]
    [(name, (chat, _text))] = sends(world.alex)
    assert (name, chat) == ("deliver_operator_message", OPERATOR)
    assert sends(world.casa) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,method", [(LINK, "deliver_operator_link"),
                                         (CARD, "deliver_operator_proposal")])
async def test_a_helpers_link_and_question_notify_as_before(world, message_posts, tool, method):
    """A sign-in link and a card with buttons need the operator: they are sent
    exactly as on any other turn."""
    outcome, _ = await call_tool(
        "finance", colleague_asks("finance", ALEX_CHAT, **UNDER_HIS_TASK), tool)
    assert outcome == "delivered"
    [(name, (chat, _text))] = sends(world.alex)
    assert (name, chat) == (method, OPERATOR)
    assert world.alex.options == [] and message_posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("turn,silent", TURNS[:2])
async def test_a_helpers_run_status_card_does_not_notify_under_the_operators_task(
        world, monkeypatch, turn, silent):
    """The call is recorded by the real admission hook, with the facts of its
    turn; the status card of the runs it starts reads them."""
    monkeypatch.setattr(pc, "CONTINUATIONS", SimpleNamespace(request=lambda *a: "queued"))
    assert (await call_tool("finance", turn(), RUNS))[0] == "admitted"
    call, err = operator_calls.CALLS.find("c1")
    assert err is None
    out = await run_admission.request_admission(call, [RUN], "k1")
    for _ in range(200):
        if not run_admission._CARD_TASKS:
            break
        await asyncio.sleep(0)
    if silent:
        assert out["status"] == "admitted"
        [(name, (chat, _text))] = sends(world.alex)
        assert (name, chat) == ("deliver_run_card", OPERATOR)
        assert world.alex.options == [("deliver_run_card", {"silent": True})]
    else:
        # outside his task a colleague's request is still put to the operator
        assert out["status"] == "confirmation_pending"
        [(name, _args)] = sends(world.alex)
        assert name == "deliver_operator_question" and world.alex.options == []
    assert sends(world.casa) == []


# -- job lines ---------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("delivery,where", [(ALEX_CHAT, "alex"), (None, None)])
async def test_a_jobs_parking_line_goes_to_its_delivery_or_nowhere(world, delivery, where):
    rec = SimpleNamespace(id="e" * 32, origin={"_tg_bot": CASA_BOT, "chat_id": OPERATOR},
                          delivery=delivery)
    runner = background_jobs.JobRunner(registry=None, channel=world.transport)
    await runner._post_parked(rec, "«Q3» stopped without finishing.")
    posted = {"alex": sends(world.alex), "casa": sends(world.casa)}
    assert posted["casa"] == []
    if where is None:
        assert posted["alex"] == []
    else:
        assert [c[0] for c in posted["alex"]] == ["post_job_parked"]


# -- continuations: delivery kept, result to the requester -------------------------------------

def _mint(role: str, origin: dict, chain: tuple):
    """The binding a call of *role* on a turn of *origin* mints (C1)."""
    from authz_grants import resolve_grant_identity
    with _Origin({**origin, "execution_role": role}):
        identity, why = resolve_grant_identity(role, artifact_id=ARTIFACT)
    assert why is None
    return plugin_bindings.BINDINGS.for_turn(plugin=PLUGIN, turn_key="t-" + role,
                                             identity=identity, chain=chain, origin=origin)


@pytest.fixture
def runs(world, monkeypatch):
    """A continuation's turn runs Alex's plugin call through the real hooks, under the
    origin a delegated run of the continuation's parent origin gets; the completion's
    announcement is captured."""
    monkeypatch.setattr(tools_mod, "_specialist_limiter", None, raising=False)
    monkeypatch.setattr(pc, "RETRY_EVERY_S", 0.01)
    seen = SimpleNamespace(calls=[], records=[], announced=[])

    async def bounded(cfg, task, _ctx):
        parent = agent_mod.origin_var.get()
        child = {**parent, "execution_role": cfg.role,
                 "_handoff_chain": child_chain(parent, cfg.role)}
        seen.calls.append(await call_tool(cfg.role, child, LINK, client="c-cont"))
        return DelegatedOutput(text="The invoice is in your downloads.")

    class _Reg:
        async def register_delegation(self, record):
            seen.records.append(record)

    def attach(task, record, **_kw):
        seen.announced.append((task, record))

    monkeypatch.setattr(tools_mod, "_run_delegated_agent_bounded", bounded)
    monkeypatch.setattr(tools_mod, "_specialist_registry", _Reg(), raising=False)
    monkeypatch.setattr(tools_mod, "_attach_completion_callback", attach)
    return seen


async def _continue(binding, run_ref="r-1"):
    pending = pc.Pending(binding, run_ref, "sweep", 0.0, bot_id=FINANCE_BOT)
    await pc.run_continuation(pending, on_start=lambda: None)


async def _completed(seen):
    [(task, record)] = seen.announced
    return await task, record


@pytest.mark.asyncio
async def test_a_continuation_is_a_task_that_survives_a_restart(world, runs, monkeypatch):
    """The continuation's durable record is a task like any delegation: run
    by the chain's last agent, its result to the requester, its posts at the
    binding's delivery — and a restart reads it back whole, so its result is
    still owed after one."""
    from job_registry import JobRegistry
    from specialist_registry import SpecialistRegistry
    path = str(world.tmp / "jobs.json")
    jobs = JobRegistry(path)
    await jobs.load()
    monkeypatch.setattr(tools_mod, "_specialist_registry",
                        SpecialistRegistry(str(world.tmp / "specialists"), job_registry=jobs),
                        raising=False)
    b = _mint("finance", ellen_delegates("finance", ALEX_CHAT), ("assistant", "finance"))
    await _continue(b)
    [(_task, record)] = runs.announced
    restarted = JobRegistry(path)
    await restarted.load()
    job = restarted.get(record.id)
    assert job is not None, "the continuation's row was refused at reload"
    assert (job.executor, job.delivery) == ("finance", ALEX_CHAT)
    assert job.requester.role == "assistant" and job.requester.route == {"_tg_bot": CASA_BOT}


@pytest.mark.asyncio
async def test_a_null_delivery_tasks_continuation_posts_nothing_live_and_after_a_restart(
        world, runs, tmp_path, monkeypatch):
    """Born while Alex's chat was closed: its binding keeps the null delivery, so each
    continuation — live, and again after the bindings are reloaded from disk — is refused
    every surface, while its result reaches Ellen exactly once per continuation."""
    world.state["finance"] = "not_open"
    b = _mint("finance", ellen_delegates("finance", None), ("assistant", "finance"))
    assert b.delivery is None
    world.state["finance"] = "open"                  # the chat opens mid-run
    await _continue(b)
    out, record = await _completed(runs)
    assert out.text == "The invoice is in your downloads."
    assert record.origin["role"] == "assistant" and record.origin["_tg_bot"] == CASA_BOT
    # restart: a fresh store reads the same file
    monkeypatch.setattr(plugin_bindings, "BINDINGS",
                        plugin_bindings.BindingStore(str(world.tmp / "bindings.json")))
    reloaded = plugin_bindings.BINDINGS.peek(b.ref)
    assert reloaded is not None and reloaded.delivery is None
    runs.announced.clear()
    await _continue(reloaded)
    await _completed(runs)
    assert [c[0] for c in runs.calls] == ["denied", "denied"]
    assert all("was not open when this task started" in c[1] for c in runs.calls)
    assert sends(world.casa) == [] and sends(world.alex) == []
    assert len(runs.records) == 2                    # one durable announcement each
    assert chat_notes.drain(FINANCE_BOT, OPERATOR) == []


@pytest.mark.asyncio
async def test_ellens_tasks_continuation_delivers_to_alexs_chat_and_completes_to_ellen(
        world, runs):
    b = _mint("finance", ellen_delegates("finance", ALEX_CHAT), ("assistant", "finance"))
    assert plugin_bindings.delivery_of(b) == ALEX_CHAT
    await _continue(b)
    _out, record = await _completed(runs)
    assert [c[0] for c in runs.calls] == ["delivered"]
    assert [c[0] for c in sends(world.alex)] == ["deliver_operator_link"]
    assert sends(world.casa) == []
    # the result to the requester, Ellen, on her route; Alex's conversation gets the note
    assert (record.origin["role"], record.origin["_tg_bot"]) == ("assistant", CASA_BOT)
    assert chat_notes.drain(FINANCE_BOT, OPERATOR) == [
        "Ellen asked you: Continue browsing run r-1. — you answered"]


@pytest.mark.asyncio
async def test_alexs_own_continuation_completes_to_his_conversation_with_no_note(world, runs):
    b = _mint("finance", own_turn("finance", FINANCE_BOT), ("finance",))
    await _continue(b)
    _out, record = await _completed(runs)
    assert [c[0] for c in runs.calls] == ["delivered"]
    assert [c[0] for c in sends(world.alex)] == ["deliver_operator_link"]
    assert sends(world.casa) == []
    assert (record.origin["role"], record.origin["_tg_bot"]) == ("finance", FINANCE_BOT)
    assert chat_notes.drain(FINANCE_BOT, OPERATOR) == []


def test_a_stored_binding_without_a_delivery_is_refused_at_load(world):
    path = world.tmp / "old.json"
    entry = {"ref": "casa-bind-" + "0" * 32, "plugin": PLUGIN, "operator_id": OPERATOR,
             "chat_id": OPERATOR, "requester": "assistant", "requester_kind": "x",
             "chain": ["assistant", "finance"], "origin": {"_tg_bot": CASA_BOT},
             "turn_key": "t", "created_at": 1.0, "last_used": 9e18}
    path.write_text(json.dumps([entry, {**entry, "ref": "casa-bind-" + "1" * 32,
                                        "delivery": [FINANCE_BOT, OPERATOR, None]},
                                {**entry, "ref": "casa-bind-" + "2" * 32,
                                 "delivery": ["x", OPERATOR, None]}]))
    store = plugin_bindings.BindingStore(str(path))
    assert store.peek("casa-bind-" + "0" * 32) is None
    assert store.peek("casa-bind-" + "2" * 32) is None
    kept = store.peek("casa-bind-" + "1" * 32)
    assert plugin_bindings.delivery_of(kept) == ALEX_CHAT
    assert plugin_bindings.binding_target(kept)[1] == FINANCE_BOT


# -- L-18: a shared plugin's callback goes where its operation lives ---------------------------

@pytest.fixture
def ingress(world, tmp_path, monkeypatch):
    """The real callback ingress over a real spool, with a plugin shared by Ellen and Alex."""
    spool = callback_spool.CallbackSpool(tmp_path / "callbacks")
    spool.ensure_plugin_dirs(PLUGIN)
    dispatched: list = []
    clock = {"t": 1_000_000.0}

    async def dispatch(role, text, context):
        dispatched.append((role, context))
        return True

    async def notify(_text):
        return None

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(ce, "_worker_task", None)
    monkeypatch.setattr(ce, "_kick", None)
    monkeypatch.setattr(ce, "_now", lambda: clock["t"])
    ce._pending_hints.clear()
    ce.configure(dispatch=dispatch,
                 resolve_registry_entry=lambda plugin: {
                     "targets": ["resident:assistant", "specialist:finance"]},
                 get_spool=lambda: spool, notify_operator=notify, sleep=no_sleep)

    def seed():
        h = "a" * 64
        path = spool.root / PLUGIN / callback_spool.RESULTS_DIR / f"{h}.json"
        path.write_text(json.dumps({"v": 1, "plugin": PLUGIN}), encoding="utf-8")
        import os
        os.utime(path, (clock["t"], clock["t"]))
        rec = callback_attempts.new_attempt(state_hash=h, minted_ts=clock["t"],
                                            status="result_ready", now=clock["t"])
        assert spool.write_attempt(PLUGIN, h, rec)

    return SimpleNamespace(seed=seed, dispatched=dispatched)


@pytest.mark.asyncio
async def test_a_callback_for_alexs_sign_in_goes_to_alex_in_his_chat(world, ingress):
    assert (await call_tool("finance", ellen_delegates("finance", ALEX_CHAT), LINK))[0] == \
        "delivered"
    ingress.seed()
    await ce._worker_pass()
    [(role, context)] = ingress.dispatched
    assert role == "finance"
    assert context == {"synthetic": "callback_nudge", "_tg_bot": FINANCE_BOT}


@pytest.mark.asyncio
async def test_alexs_callback_turn_is_born_on_alexs_bot_through_casas_dispatcher(
        world, ingress, monkeypatch):
    """Through the dispatcher Casa really wires (``main``'s setup dispatch):
    the nudge answering Alex's sign-in is a turn of Alex's, born on Alex's
    bot in the operator's chat — never on Casa's bot. A nudge with no owner
    stays Casa's."""
    from test_bot_boundary_invariants import _c_main_closure
    sent: list = []

    class _Bus:
        async def send_checked(self, msg):
            sent.append(msg)
            return "accepted"

    dispatch = _c_main_closure("_setup_dispatch", "    async def _setup_notify(",
                               channel_manager=tools_mod._channel_manager, bus=_Bus())
    monkeypatch.setattr(ce, "_dispatch", dispatch)
    assert (await call_tool("finance", ellen_delegates("finance", ALEX_CHAT), LINK))[0] == \
        "delivered"
    ingress.seed()
    await ce._worker_pass()
    (turn,) = sent
    assert turn.target == "finance"
    assert turn.context["_tg_bot"] == FINANCE_BOT
    assert (turn.context["chat_id"], turn.context["synthetic"]) == (OPERATOR, "callback_nudge")
    assert world.transport.endpoint_for_route(turn.context, site="test") is world.alex

    # with no sign-in link the nudge keeps today's order, on Casa's bot
    callback_owner.reset()
    sent.clear()
    import casa_core
    ctx = casa_core._setup_turn_context(world.transport, {"synthetic": "callback_nudge"})
    assert ctx["_tg_bot"] == CASA_BOT
    # a setup turn never takes a bot from its caller
    ctx = casa_core._setup_turn_context(world.transport,
                                        {"_plugin_setup": "x", "_tg_bot": FINANCE_BOT})
    assert ctx["_tg_bot"] == CASA_BOT


@pytest.mark.asyncio
async def test_a_callback_for_ellens_sign_in_goes_where_it_went_before(world, ingress):
    assert (await call_tool("assistant", own_turn("assistant", CASA_BOT), LINK))[0] == \
        "delivered"
    ingress.seed()
    await ce._worker_pass()
    [(role, context)] = ingress.dispatched
    assert role == "assistant" and context["synthetic"] == "callback_nudge"


@pytest.mark.asyncio
async def test_a_callback_with_no_sign_in_link_keeps_todays_order(world, ingress):
    ingress.seed()
    await ce._worker_pass()
    [(role, context)] = ingress.dispatched
    assert (role, context) == ("assistant", {"synthetic": "callback_nudge"})


def test_the_latest_unexpired_link_decides():
    clock = {"t": 0.0}
    callback_owner.reset(now=lambda: clock["t"])
    callback_owner.note_link(PLUGIN, "assistant", ELLEN_CHAT, ttl_s=60)
    callback_owner.note_link(PLUGIN, "finance", ALEX_CHAT, ttl_s=60)
    assert callback_owner.operation_for(PLUGIN) == ("finance", ALEX_CHAT)
    clock["t"] = 61.0
    assert callback_owner.operation_for(PLUGIN) is None
    callback_owner.reset()
