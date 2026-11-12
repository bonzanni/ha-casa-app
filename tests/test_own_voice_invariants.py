"""The own-chat invariants, end to end over the real modules.

Two bots behind one transport, both real ``TelegramChannel`` endpoints on a
recording bot: Casa's (Ellen, ``assistant``) and Alex's own (``finance``),
whose chat the real own-chat lifecycle has offered, bound and opened. Fido
(``browser``) declares a chat of his own but has no bot yet, so he is behind
Ellen; Tina (``butler``) declares none. Delegations run through the real
``delegate_to_agent`` on the specialist SDK-client double; jobs through the
real launcher, engagement registry and in-Casa driver; a task's posts are
filed in the real ``task_posts`` and its replies go through the real
``task_input``; plugin calls through the real admission and result hooks.

What is pinned, in the operator's terms:

* Alex's conversation hears the operator's messages in his chat and the
  results of work it started; Ellen's tasks reach it only as a note, once.
* A task Ellen hands Alex shows in Alex's chat (who asked, the whole answer);
  Ellen gets her result. A reply to a live task's post reaches that task; to
  an ended one, Alex's conversation with the task's end in its note.
* Everything of Alex's posts in Alex's chat or refuses with a typed line;
  nothing of his ever posts in Ellen's chat, and nothing falls back there.
* Ellen's own chat, and Casa's own cards, are as they always were.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent as agent_mod
import agent_inbox
import agent_lane
import callback_owner
import chat_notes
import operator_calls
import own_voice
import own_voice_lifecycle as ovl
import plugin_bindings
import plugin_outbox
import result_broker as rb
import run_admission
import task_input
import task_posts
import tools
from bus import MessageBus
from channels.telegram import TelegramChannel
from channels.tg_address import TgAddress
from config import CharacterConfig
from engagement_registry import EngagementRegistry
from mcp_registry import McpServerRegistry
from own_voice_world import World, created, make_lifecycle, specialist
from plugin_grants import PluginContract, ResultContractMap, ToolContract
from provenance import task_delivery_marker
from specialist_limits import SpecialistLimiter
from specialist_registry import SpecialistRegistry
from telegram_channel_double import MemoryBotIdStore, make_transport

try:
    from tests.test_delegate_to_agent import _caller_cfg, _FakeSpecialistClient
    from tests.test_delegate_to_agent_interactive import _make_alex_cfg
except ImportError:
    from test_delegate_to_agent import _caller_cfg, _FakeSpecialistClient
    from test_delegate_to_agent_interactive import _make_alex_cfg

OPERATOR = 100
CASA_BOT = 111
ALEX_BOT = 222
ALEX_TOKEN = "222:alex-token-aaaaaaaaaaaaaaaaaaaa"
ALEX_CHAT = TgAddress(ALEX_BOT, OPERATOR)
ELLEN_CHAT = TgAddress(CASA_BOT, OPERATOR)
ANSWER = "Groceries in October: €420, mostly at the market."
BRIEF = "How much on groceries in October?"


# ---------------------------------------------------------------------------
# The bots: every Bot API call recorded, every send answered with a new id
# ---------------------------------------------------------------------------

class RecordingBot:
    """A Telegram bot that records each Bot API call by name."""

    def __init__(self, bot_id: int) -> None:
        self.id = bot_id
        self.calls: list[tuple[str, dict]] = []
        self._ids = itertools.count(500)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        async def call(*args, **kwargs):
            if args:
                kwargs = {**kwargs, "_args": args}
            self.calls.append((name, kwargs))
            if name.startswith(("send_", "copy_")):
                kwargs["_mid"] = mid = next(self._ids)
                return SimpleNamespace(message_id=mid, chat=SimpleNamespace(
                    id=kwargs.get("chat_id")), photo=None, document=None)
            return True
        return call

    def sends(self) -> list[tuple[str, dict]]:
        """What reached the chat: sends and edits, never a typing action."""
        return [(n, k) for n, k in self.calls
                if n.startswith(("send_", "edit_", "copy_")) and n != "send_chat_action"]

    def texts(self) -> list[str]:
        return [k.get("text") or k.get("caption") or "" for _n, k in self.sends()]


def _endpoint(role: str, bot_id: int, bus: MessageBus, *, casa: bool) -> TelegramChannel:
    bot = RecordingBot(bot_id)
    ch = TelegramChannel(chat_id=str(OPERATOR), bot=bot, bus=bus, default_agent=role,
                         is_casa=casa, known_bot_id=bot_id)
    ch._app = SimpleNamespace(bot=bot)
    ch._start_typing = lambda *a, **k: None
    ch._stop_typing = lambda *a, **k: None
    ch.recorder = bot
    return ch


async def _noop(_msg):
    return None


def _queued(bus: MessageBus, role: str) -> list:
    """The messages waiting on *role*'s bus queue (nobody consumes them here)."""
    q = bus.queues.get(role)
    out = []
    while q is not None and not q.empty():
        out.append(q.get_nowait()[2])
    return out


# ---------------------------------------------------------------------------
# The SDK client a delegated run builds
# ---------------------------------------------------------------------------

class Client(_FakeSpecialistClient):
    """The specialist's session: records every message written into it — the
    brief, then the operator's replies — and can run surface calls mid-run
    (``on_query``) or hold the run until released (``hold``)."""
    built: "Client | None" = None
    on_query = None
    hold: "asyncio.Event | None" = None

    def __init__(self, options):
        super().__init__(options)
        self.writes: list[str] = []
        type(self).built = self

    async def query(self, prompt):
        if isinstance(prompt, str):
            await super().query(prompt)
            self.writes.append(prompt)
            if type(self).on_query is not None:
                await type(self).on_query(self)
            if type(self).hold is not None:
                await type(self).hold.wait()
            return
        async for payload in prompt:                  # a reply written by task_input
            self.writes.append(payload["message"]["content"])


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------

def _cfg(role: str, name: str, *, kind: str = "specialist"):
    cfg = _make_alex_cfg()
    cfg.role = role
    cfg.character = CharacterConfig(name=name, archetype=role, card="", prompt=f"You are {name}.")
    cfg.kind = kind
    cfg.system_prompt = f"You are {name}."
    return cfg


@pytest.fixture
async def world(tmp_path, monkeypatch):
    # -- the own-chat lifecycle: Alex's chat offered, bound and opened -------------
    w = World()
    w.manager_first_name, w.manager_username = "EllenTest", "HeyEllenTestBot"
    w.configs = {"finance": specialist("Alex"), "browser": specialist("Fido")}
    w.queues = {"finance"}
    w.tokens = {ALEX_BOT: ALEX_TOKEN}
    lc, host = make_lifecycle(w, tmp_path / "data")
    monkeypatch.setattr(host, "operator_id", lambda: OPERATOR)
    ovl.install(lc)
    await lc.post_offer("finance", "HeyAlexTestBot")
    await lc.on_created(created(ALEX_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    await lc.intake(ALEX_BOT, {"update_id": 1, "message": {
        "message_id": 1, "text": "/start", "chat": {"id": OPERATOR, "type": "private"},
        "from": {"id": OPERATOR, "is_bot": False}}})

    # -- the two endpoints on one transport ---------------------------------------
    bus = MessageBus()
    bus.register("assistant", _noop)
    bus.register("finance", _noop)
    casa = _endpoint("assistant", CASA_BOT, bus, casa=True)
    alex = _endpoint("finance", ALEX_BOT, bus, casa=False)
    filed_on_casa: list = []
    real_record = casa._record_post

    def _record_post(chat_id, sent_msg, post):
        filed_on_casa.append(post)
        return real_record(chat_id, sent_msg, post)
    casa._record_post = _record_post
    manager = make_transport(casa, casa=casa, casa_bot_ids=MemoryBotIdStore(CASA_BOT))
    transport = manager.get("telegram")
    transport.add_agent_endpoint(alex, ALEX_BOT, "finance")
    monkeypatch.setattr(host, "endpoint", lambda bot_id: {ALEX_BOT: alex}.get(bot_id))

    # -- Casa's registries and agents ----------------------------------------------
    registry = EngagementRegistry(tombstone_path=str(tmp_path / "eng.json"))
    casa._engagement_registry = alex._engagement_registry = registry
    specialists = SpecialistRegistry(str(tmp_path / "specialists"),
                                     tombstone_path=str(tmp_path / "jobs.json"))
    ellen = _caller_cfg(delegates=("finance", "browser", "butler"))
    ellen.kind = "resident"
    ellen.character = CharacterConfig(name="Ellen")
    roles = {"assistant": ellen, "finance": _cfg("finance", "Alex"),
             "browser": _cfg("browser", "Fido"), "butler": _cfg("butler", "Tina")}
    for role in ("finance", "browser", "butler"):
        roles[role].cwd = str(tmp_path / "home" / role)
        specialists._configs[role] = roles[role]
    tools.init_tools(manager, bus, specialists, McpServerRegistry(),
                     engagement_registry=registry,
                     specialist_limiter=SpecialistLimiter(max_global=4),
                     agent_role_map=roles)

    # -- the module state every task path uses, fresh --------------------------------
    for module in (agent_lane, chat_notes, task_posts, task_input, callback_owner):
        module.reset()
    tools._TASK_INPUTS.clear()
    import verdict_broker
    monkeypatch.setattr(verdict_broker, "BROKER", verdict_broker.VerdictBroker())
    monkeypatch.setattr(operator_calls, "CALLS", operator_calls.OperatorCalls())
    monkeypatch.setattr(plugin_bindings, "BINDINGS",
                        plugin_bindings.BindingStore(str(tmp_path / "bindings.json")))
    monkeypatch.setattr(run_admission, "CONFIRMATIONS",
                        run_admission.Confirmations(str(tmp_path / "admissions.json")))
    agent_inbox._reset_for_tests()
    for role in ("assistant", "finance"):
        agent_inbox._inboxes[role] = agent_inbox.open_inbox(role, str(tmp_path / "inbox" / role))
    monkeypatch.setattr(tools, "ClaudeSDKClient", Client)
    for name in ("built", "on_query", "hold"):
        monkeypatch.setattr(Client, name, None)
    _FakeSpecialistClient.reset(response=ANSWER)

    yield SimpleNamespace(lc=lc, host=host, w=w, bus=bus, casa=casa, alex=alex,
                          transport=transport, registry=registry, specialists=specialists,
                          roles=roles, tmp=tmp_path)

    # nothing any scenario sent through Ellen's bot was filed as another agent's
    assert [p.role for p in filed_on_casa if p is not None and p.role != "assistant"] == []
    for t in list(tools._delegation_settle_tasks):
        t.cancel()
    for module in (agent_lane, chat_notes, task_posts, task_input, callback_owner):
        module.reset()
    tools._TASK_INPUTS.clear()
    agent_inbox._reset_for_tests()
    ovl.install(None)


def _ellen_turn(**extra) -> dict:
    """Ellen's own turn: the operator wrote to her in her chat."""
    return {"role": "assistant", "execution_role": "assistant", "channel": "telegram",
            "source": "telegram", "message_type": "channel_in", "chat_id": OPERATOR,
            "user_id": OPERATOR, "cid": "c-ellen", "_tg_bot": CASA_BOT,
            "_origin_route": "telegram", "_origin_clearance": "private",
            "_operator_turn": True, "user_text": "groceries in october?", **extra}


def _alex_turn(**extra) -> dict:
    """A turn of Alex's own conversation: the operator wrote in Alex's chat."""
    return {"role": "finance", "execution_role": "finance", "channel": "telegram",
            "source": "telegram", "message_type": "channel_in", "chat_id": OPERATOR,
            "user_id": OPERATOR, "cid": "c-alex", "_tg_bot": ALEX_BOT,
            "_origin_route": "telegram", "_origin_clearance": "private",
            "_operator_turn": True, "user_text": "and september?", **extra}


async def _delegate(agent="finance", task=BRIEF, mode="sync", origin=None) -> dict:
    token = agent_mod.origin_var.set(origin or _ellen_turn())
    try:
        res = await tools.delegate_to_agent.handler(
            {"agent": agent, "task": task, "context": "", "mode": mode})
    finally:
        agent_mod.origin_var.reset(token)
    return json.loads(res["content"][0]["text"])


async def _settled():
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.gather(*list(tools._delegation_settle_tasks), return_exceptions=True)
    for _ in range(5):
        await asyncio.sleep(0)


def _ellen_sends(world) -> list:
    return world.casa.recorder.sends()


def _ellen_texts(world) -> list[str]:
    return world.casa.recorder.texts()


# ---------------------------------------------------------------------------
# A plugin shared by the agents: its calls through the real admission and
# result hooks
# ---------------------------------------------------------------------------

ARTIFACT = "5" * 64
PLUGIN = "demo"
LINK = "mcp__plugin_demo_api__sign_in"            # capability: delivers a link
NOTE = "mcp__plugin_demo_api__note"               # capability: delivers a message
CARD = "mcp__plugin_demo_api__card"               # capability: delivers a proposal
READ = "mcp__plugin_demo_api__read"               # safe: posts nothing
SURFACES = (LINK, NOTE, CARD)
URL = "https://bank.example.com/approve?s=1"
DEPOSITS = {LINK: ("link", URL), NOTE: ("note", "Groceries in October: EUR 420."),
            CARD: ("card", json.dumps({"text": "Keep this?",
                                       "buttons": [{"label": "Close", "close": True}]}))}


FILE = "mcp__plugin_demo_api__export"           # capability: delivers a file


def _map() -> ResultContractMap:
    return ResultContractMap(
        tools={
            FILE: ToolContract(ARTIFACT, PLUGIN, "capability", ("export",), {},
                               {"export": "operator_file"}, servers=("api",)),
            LINK: ToolContract(ARTIFACT, PLUGIN, "capability", ("link",), {},
                               {"link": "operator_link"}, servers=("api",)),
            NOTE: ToolContract(ARTIFACT, PLUGIN, "capability", ("note",), {},
                               {"note": "operator_message"}, servers=("api",)),
            CARD: ToolContract(ARTIFACT, PLUGIN, "capability", ("card",), {},
                               {"card": "operator_proposal"}, servers=("api",)),
            READ: ToolContract(ARTIFACT, PLUGIN, "safe", (), {}),
        },
        plugins={PLUGIN: PluginContract(ARTIFACT, True, frozenset(), name=PLUGIN)})


class _Origin:
    def __init__(self, origin):
        self.origin = origin

    def __enter__(self):
        self._o = agent_mod.origin_var.set(self.origin)
        self._e = tools.engagement_var.set(None)

    def __exit__(self, *exc):
        agent_mod.origin_var.reset(self._o)
        tools.engagement_var.reset(self._e)


async def call_tool(role: str, tool: str, origin: "dict | None" = None, *, client="c1",
                    tu="tu-1", deposit: "tuple | None" = None) -> tuple:
    """One plugin call of *role* through the real admission and result hooks, on
    the turn of *origin* (default: the turn running now): ``("denied", why)``,
    ``("admitted", None)``, ``("delivered", body)`` or ``("withheld", body)``.
    *deposit*: ``(slot, value, deposit keywords)`` instead of :data:`DEPOSITS`'."""
    store = rb.ReferenceStore()
    admit = rb.make_plugin_admission_hook(role, _map(), client_id=client, store=store)
    result = rb.make_result_hook(_map(), client_id=client, store=store)
    with _Origin(origin if origin is not None else agent_mod.origin_var.get()):
        verdict = await admit({"hook_event_name": "PreToolUse", "tool_name": tool,
                               "tool_input": {}}, tu, {})
        if verdict:
            return "denied", verdict["hookSpecificOutput"]["permissionDecisionReason"]
        if tool not in DEPOSITS and deposit is None:
            return "admitted", None
        slot, value, extra = deposit if deposit is not None else (*DEPOSITS[tool], {})
        ref, err = store.deposit(client_id=client, slot=slot, value=value, **extra)
        assert err is None, err
        out = await result({"hook_event_name": "PostToolUse", "tool_name": tool,
                            "tool_input": {}, "tool_response": json.dumps({slot: ref})},
                           tu, {})
    body = json.loads(out["hookSpecificOutput"]["updatedToolOutput"])
    return ("withheld" if body.get("casa_result_withheld") else "delivered"), body


async def _send_media(name: str = "report.pdf") -> dict:
    """``send_media`` of a file in the plugin outbox, on the turn running now."""
    outbox = plugin_outbox._OUTBOX
    path = os.path.join(outbox._root_realpath, name)
    with open(path, "wb") as fh:
        fh.write(b"%PDF-1.7\n" + b"x" * 64)
    res = await tools.send_media.handler({"path": path, "kind": "document"})
    return json.loads(res["content"][0]["text"])


@pytest.fixture
def outbox(tmp_path):
    box = plugin_outbox.init_outbox(str(tmp_path / "outbox"))
    yield box
    box.close()
    plugin_outbox._OUTBOX = None


# ---------------------------------------------------------------------------
# The operator's messages, as an endpoint's handler receives them
# ---------------------------------------------------------------------------

_mids = itertools.count(70)


def _user():
    return SimpleNamespace(id=OPERATOR, first_name="Op", is_bot=False)


def _quoted(mid, text):
    return SimpleNamespace(message_id=mid, text=text, caption=None, date=None,
                           from_user=None, sender_chat=None)


def _text(text, *, reply_to=None, quoted_text="…"):
    mid = next(_mids)
    message = SimpleNamespace(
        text=text, message_id=mid, from_user=_user(), sender_chat=None,
        chat=SimpleNamespace(id=OPERATOR),
        reply_to_message=_quoted(reply_to, quoted_text) if reply_to is not None else None)
    return SimpleNamespace(
        update_id=5000 + mid, message=message, callback_query=None,
        effective_chat=SimpleNamespace(id=str(OPERATOR)), effective_user=_user(),
        to_dict=lambda: {"update_id": 5000 + mid, "message": {"text": text}})


def _file(name="statement.pdf", *, reply_to=None):
    mid = next(_mids)
    doc = SimpleNamespace(file_id=f"fid-{mid}", file_name=name, file_size=1234,
                          mime_type="application/pdf")
    msg = SimpleNamespace(
        message_id=mid, chat=SimpleNamespace(id=OPERATOR), from_user=_user(),
        document=doc, photo=None, caption=None, media_group_id=None,
        reply_to_message=_quoted(reply_to, "…") if reply_to is not None else None,
        message_thread_id=None, sender_chat=None,
        **{k: None for k in ("sticker", "video", "video_note", "voice", "audio", "animation",
                             "contact", "location", "venue", "poll", "dice", "game", "story",
                             "web_app_data", "invoice", "pinned_message", "new_chat_title",
                             "new_chat_photo", "delete_chat_photo",
                             "message_auto_delete_timer_changed", "new_chat_members",
                             "left_chat_member", "write_access_allowed")})
    return SimpleNamespace(update_id=6000 + mid, message=msg, callback_query=None,
                           effective_chat=SimpleNamespace(id=str(OPERATOR)),
                           effective_user=_user(),
                           to_dict=lambda: {"update_id": 6000 + mid})


async def _handle(ch, upd) -> None:
    """The endpoint's message handler, and every turn task it spawned; then
    the writers of the tasks' inputs have their turn."""
    if getattr(upd.message, "document", None) is not None:
        await ch._on_non_text_message(upd, None)
    else:
        await ch._handle(upd, None)
    for t in list(ch._turn_tasks):
        await t
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.fixture
def downloads(monkeypatch):
    """Telegram's file download: the bytes of a PDF, whatever the file id."""
    async def fetch(bot, file_id, cap, **_kw):
        return b"%PDF-1.4\n" + file_id.encode()
    monkeypatch.setattr(agent_inbox, "fetch_telegram_file", fetch)


def _cq(data: str, message_id: int):
    cq = SimpleNamespace(
        id="cq1", data=data,
        message=SimpleNamespace(message_id=message_id, chat=SimpleNamespace(id=OPERATOR),
                                message_thread_id=None),
        answer=AsyncMock(return_value=None), from_user=SimpleNamespace(id=OPERATOR))
    return SimpleNamespace(callback_query=cq)


async def _tap(ch, data: str, message_id: int) -> list[str]:
    upd = _cq(data, message_id)
    await ch._on_inline_callback(upd, context=None)
    for _ in range(5):
        await asyncio.sleep(0)
    for t in list(ch._turn_tasks):
        await t
    return [c.args[0] for c in upd.callback_query.answer.await_args_list]


# ---------------------------------------------------------------------------
# A delegation that stays live until released
# ---------------------------------------------------------------------------

class Held(Client):
    """A run whose session stays live (its client attached, its reply not yet
    given) until ``release`` is set."""
    release: "asyncio.Event | None" = None

    async def receive_response(self):
        await type(self).release.wait()
        async for item in super().receive_response():
            yield item


async def _live_delegation(world, monkeypatch, agent="finance"):
    """Ellen's sync delegation to *agent*, running and attached: returns the
    pending call and the task's id."""
    monkeypatch.setattr(tools, "ClaudeSDKClient", Held)
    monkeypatch.setattr(Held, "release", asyncio.Event())
    call = asyncio.ensure_future(_delegate(agent))
    for _ in range(500):
        await asyncio.sleep(0.002)
        if Held.built is not None and tools._TASK_INPUTS:
            (task_id,) = list(tools._TASK_INPUTS)
            if task_input.state(task_id) == "live":
                return call, task_id
        if call.done():
            break
    raise AssertionError(f"the delegation never went live: {call}")


# ---------------------------------------------------------------------------
# The world is what it says
# ---------------------------------------------------------------------------

async def test_the_world_alex_open_fido_behind_ellen_tina_without_a_chat(world):
    assert own_voice.own_chat_state("finance") == "open"
    assert own_voice.own_chat_state("browser") == "no_bot"
    assert own_voice.own_chat_state("butler") == "not_declared"
    assert own_voice.endpoint_for("finance") is world.alex
    assert world.transport.route_for("finance") == ALEX_BOT
    assert world.transport.route_for("browser") == CASA_BOT


# ---------------------------------------------------------------------------
# Alex's conversation: the operator's messages there, results of work it
# started; Ellen's tasks never
# ---------------------------------------------------------------------------

async def test_the_operators_message_in_alexs_chat_is_a_turn_of_alexs_conversation(world):
    await _handle(world.alex, _text("and September?"))
    [turn] = _queued(world.bus, "finance")
    assert turn.content == "and September?"
    assert turn.context["_tg_bot"] == ALEX_BOT
    assert _queued(world.bus, "assistant") == []


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_ellens_tasks_never_enter_alexs_conversation_only_a_note(world, mode):
    payload = await _delegate(mode=mode)
    await asyncio.sleep(0.05)
    await _settled()
    assert payload["status"] == ("ok" if mode == "sync" else "pending")
    assert _queued(world.bus, "finance") == []
    told = _queued(world.bus, "assistant")
    if mode == "async":
        # the result reaches its requester, Ellen, once
        [done] = told
        assert done.content.delegation_id == payload["delegation_id"]
        assert ANSWER in done.content.text
    else:
        assert told == []                      # the sync result was the tool's answer
    [note] = chat_notes.drain(ALEX_BOT, OPERATOR)
    assert note == f"Ellen asked you: {BRIEF} — you answered"


# ---------------------------------------------------------------------------
# A task in Alex's chat: who asked, his closing message; Ellen gets her result
# ---------------------------------------------------------------------------

async def test_a_task_ellen_hands_alex_shows_in_his_chat_and_ellen_gets_her_result(world):
    payload = await _delegate()
    await _settled()
    assert payload["status"] == "ok" and payload["text"].startswith(ANSWER)
    texts = world.alex.recorder.texts()
    assert texts[0] == f"Ellen asked me: {BRIEF}"
    assert ANSWER in "".join(texts[1:])
    assert _ellen_texts(world) == []          # Ellen's own record only
    # every message of the task is filed under it: a reply to it finds the task
    owners = [task_posts.ended_owner(TgAddress(ALEX_BOT, OPERATOR, 500 + i))
              for i in range(len(texts))]
    assert {o.task_id for o in owners if o is not None} == {payload["delegation_id"]}
    assert None not in owners


# ---------------------------------------------------------------------------
# The conversation's next turn carries the task's note, once
# ---------------------------------------------------------------------------

@pytest.fixture
def conversation(world, tmp_path, monkeypatch):
    """Alex's conversation: the real ``Agent`` for his role on the session
    machinery, its SDK client the conversation double."""
    from unittest.mock import patch

    from agent import Agent
    from agent_registry import AgentRegistry
    from session_registry import SessionRegistry
    import scratchpad
    try:
        from tests.test_own_voice_conversation import _Client as Conv, _resident, _specialist
    except ImportError:
        from test_own_voice_conversation import _Client as Conv, _resident, _specialist
    from agent_log_double import RecordingLogs
    monkeypatch.setattr(scratchpad, "PADS", scratchpad.PadStore())
    monkeypatch.setattr(tools, "_agent_logs", RecordingLogs())
    Conv.reset()
    known = AgentRegistry.build(residents={"assistant": _resident()},
                                specialists={"finance": _specialist()})
    monkeypatch.setattr(tools, "_agent_registry", known, raising=False)
    cfg = _specialist()
    cfg.cwd = str(tmp_path / "home" / "finance-conversation")
    alex = Agent(config=cfg, session_registry=SessionRegistry(str(tmp_path / "sessions.json")),
                 mcp_registry=McpServerRegistry(), channel_manager=tools._channel_manager,
                 agent_registry=known)
    with patch("sdk_client_pool._default_make_client", Conv):
        yield SimpleNamespace(agent=alex, client=Conv)


def _dm_to_alex(text: str, mid: str):
    from bus import BusMessage, MessageType
    try:
        from tests.test_agent_process import _person_typed
    except ImportError:
        from test_agent_process import _person_typed
    msg = BusMessage(type=MessageType.CHANNEL_IN, source="telegram", target="finance",
                     content=text, channel="telegram",
                     context={"chat_id": OPERATOR, "user_id": OPERATOR, "message_id": mid,
                              "_origin_route": "telegram", "_origin_clearance": "private",
                              "_operator_turn": True, "_tg_bot": ALEX_BOT})
    msg.trusted_user_origin = _person_typed()
    return msg


async def test_alexs_next_turn_carries_the_tasks_note_once(world, conversation):
    await _delegate()
    await _settled()
    await conversation.agent.handle_message(_dm_to_alex("and September?", "8"))
    await conversation.agent.handle_message(_dm_to_alex("and August?", "9"))
    first, second = conversation.client.prompts
    assert f"(Casa) Ellen asked you: {BRIEF} — you answered" in first
    assert "Ellen asked you" not in second
    assert _ellen_texts(world) == []          # his answers stay in his chat


# ---------------------------------------------------------------------------
# A reply to a task's post: to the live task; to an ended one, the
# conversation with the task's end in its note — text and files alike
# ---------------------------------------------------------------------------

async def test_a_reply_to_a_live_delegations_post_reaches_it_and_after_its_end_alex(
        world, monkeypatch):
    call, did = await _live_delegation(world, monkeypatch)
    start_line = 500                                       # the task's first post
    assert task_posts.owner(TgAddress(ALEX_BOT, OPERATOR, start_line)).task_id == did
    await _handle(world.alex, _text("only the market, please", reply_to=start_line,
                                    quoted_text=f"Ellen asked me: {BRIEF}"))
    assert Held.built.writes[-1].endswith("only the market, please")
    assert _queued(world.bus, "finance") == []
    Held.release.set()
    assert (await call)["status"] == "ok"
    await _settled()
    # ended: the conversation takes it, told the task has ended
    await _handle(world.alex, _text("and now?", reply_to=start_line))
    [turn] = _queued(world.bus, "finance")
    assert turn.content == "and now?"
    assert "which has ended" in turn.context["_reply_note"]
    assert Held.built.writes[-1].endswith("only the market, please")
    assert _ellen_texts(world) == []


async def test_what_the_operator_wrote_into_a_live_task_reaches_its_requester(
        world, monkeypatch):
    call, did = await _live_delegation(world, monkeypatch)
    await _handle(world.alex, _text("only Lidl", reply_to=500,
                                    quoted_text=f"Ellen asked me: {BRIEF}"))
    assert Held.built.writes[-1].endswith("only Lidl")      # the task read it
    Held.release.set()
    payload = await call
    await _settled()
    assert payload["status"] == "ok"
    # Ellen's result names the words once, as the operator wrote them
    assert payload["text"].endswith(
        "(Casa: the person also wrote to Alex directly while it worked on this: «only Lidl». "
        "Alex got those words in the task itself and worked with them, so they are not yours "
        "to pass on or to start again as a new task.)")
    assert payload["text"].count("only Lidl") == 1
    assert task_input.take_said(did) == []                   # taken once
    assert _ellen_texts(world) == []


async def _reads(options, path) -> bool:
    """The build's own path check, as its ``Read`` meets it."""
    [scope] = [m for m in options.hooks["PreToolUse"] if m.matcher == "Read|Write|Edit"]
    out = await scope.hooks[0]({"tool_name": "Read", "tool_input": {"file_path": str(path)}},
                               None, {})
    return out == {}


async def test_a_file_sent_to_a_live_delegations_post_reaches_it_and_its_build_reads_it(
        world, monkeypatch, downloads):
    call, did = await _live_delegation(world, monkeypatch)
    await _handle(world.alex, _file("receipt.pdf", reply_to=500))
    line = Held.built.writes[-1]
    assert line.startswith("[casa file] receipt.pdf at ")
    path = line.split(" at ", 1)[1].split(" — ", 1)[0]
    assert os.path.dirname(path) == agent_inbox.get_inbox("finance").ready_dir
    assert agent_inbox.task_file_granted(did, path)
    assert await _reads(Held.built.captured_options, path)
    assert _queued(world.bus, "finance") == []
    Held.release.set()
    await call
    await _settled()


# ---------------------------------------------------------------------------
# Every surface of Alex's: in his chat, or refused with a typed line; never
# in Ellen's chat, never a fall-back there
# ---------------------------------------------------------------------------

async def _surfaces_inside(world, agent: str, outbox) -> list:
    """Ellen delegates to *agent*; inside the run, every surface it has is
    called on the run's own turn: the plugin's three deliveries and a file."""
    seen: list = []

    async def mid_run(_client):
        for tool in SURFACES:
            seen.append((tool, *await call_tool(agent, tool)))
        seen.append(("send_media", await _send_media()))

    Client.on_query = mid_run
    payload = await _delegate(agent)
    await _settled()
    assert payload["status"] == "ok", payload
    return seen


async def test_every_surface_of_alexs_task_posts_in_his_chat_and_none_in_ellens(
        world, outbox):
    seen = await _surfaces_inside(world, "finance", outbox)
    assert [(tool, outcome) for tool, outcome, *_ in seen[:3]] == [
        (LINK, "delivered"), (NOTE, "delivered"), (CARD, "delivered")]
    assert seen[3][1]["status"] == "ok", seen[3]
    names = [n for n, _k in world.alex.recorder.sends()]
    assert "send_document" in names and len(names) >= 6       # start line, 3, file, answer
    assert {k.get("chat_id", OPERATOR) for _n, k in world.alex.recorder.sends()} == {OPERATOR}
    assert _ellen_texts(world) == []


async def test_every_surface_of_an_agent_behind_ellen_refuses_typed_and_posts_nothing(
        world, outbox):
    """Fido declares a chat but has no bot yet; Tina declares none. Each of
    their surfaces refuses with the line it is told, and neither bot posts
    any of it, and nothing posts in Ellen's chat."""
    for agent, name in (("browser", "Fido"), ("butler", "Tina")):
        seen = await _surfaces_inside(world, agent, outbox)
        for tool, outcome, why in seen[:3]:
            assert outcome == "denied", (agent, tool)
            assert why.startswith("not executed: ") and name in why, why
        media = seen[3][1]
        assert media["status"] == "error" and media["kind_error"].startswith("own_chat_")
    assert world.alex.recorder.sends() == []
    assert _ellen_texts(world) == []


async def test_a_surface_of_alexs_task_refuses_once_his_chat_has_closed(world, outbox):
    """Born while Alex's chat was open, the task's delivery is his chat; the
    chat closes while it runs (his manifest no longer declares it). Every
    surface then refuses with a typed line, and nothing posts anywhere — not
    in his chat, not in Ellen's."""
    seen: list = []

    async def mid_run(_client):
        del world.w.configs["finance"]
        await world.lc.reconcile_role("finance")
        assert own_voice.own_chat_state("finance") == "dormant"
        before = len(world.alex.recorder.sends())
        for tool in SURFACES:
            seen.append(await call_tool("finance", tool))
        seen.append(await _send_media())
        seen.append(len(world.alex.recorder.sends()) - before)

    Client.on_query = mid_run
    payload = await _delegate()
    await _settled()
    assert payload["status"] == "ok"
    for outcome, why in seen[:3]:
        assert outcome == "denied" and why == "not executed: Alex's chat is closed right now."
    assert seen[3]["status"] == "error" and seen[3]["kind_error"] == "own_chat_missing"
    assert seen[4] == 0
    assert _ellen_texts(world) == []


async def test_a_turn_running_another_agent_in_ellens_chat_never_posts_there(world):
    """A turn that is no task and runs Alex in Ellen's chat — a hand-off made
    without a task's delivery — has nowhere to post: each surface refuses,
    nothing is posted in either chat. The resident's and the agent's own
    turns keep their own chats."""
    from authz_grants import resolve_grant_identity
    in_ellens_chat = _ellen_turn(execution_role="finance", delegation_depth=1,
                                 _handoff_chain=("assistant", "finance"))
    with _Origin(in_ellens_chat):
        identity, why = resolve_grant_identity("finance", artifact_id=ARTIFACT)
    assert why is None and identity.delivery is None
    for tool in SURFACES:
        outcome, why = await call_tool("finance", tool, in_ellens_chat)
        assert outcome == "denied" and "was not open when this task started" in why
    assert world.alex.recorder.sends() == [] and _ellen_texts(world) == []
    with _Origin(_ellen_turn()):
        assert resolve_grant_identity("assistant", ARTIFACT)[0].delivery == ELLEN_CHAT
    with _Origin(_alex_turn()):
        assert resolve_grant_identity("finance", ARTIFACT)[0].delivery == ALEX_CHAT


async def test_a_job_of_an_agent_without_an_open_chat_is_refused_before_it_starts(world):
    import background_jobs as jobs
    decl = jobs.JobDecl("web:fetch", "web", "fetch", "web:fetch", "Fetch a page", None)
    host = jobs.JobHost("specialist", "browser", decl, SimpleNamespace(name="web"))
    res = json.loads((await tools._start_job_on_host(
        host, "fetch", "", _ellen_turn()))["content"][0]["text"])
    assert res["status"] == "error" and res["kind"] == "own_chat_missing"
    assert "Fido" in res["message"]
    assert world.registry.active_and_idle() == []
    assert _ellen_texts(world) == [] and world.alex.recorder.sends() == []


# ---------------------------------------------------------------------------
# Ellen's chat is unchanged for everything that is hers
# ---------------------------------------------------------------------------

async def test_ellens_own_chat_is_as_it_was(world, outbox):
    # her own message is her own turn
    await _handle(world.casa, _text("hello Ellen"))
    [turn] = _queued(world.bus, "assistant")
    assert turn.content == "hello Ellen" and turn.context["_tg_bot"] == CASA_BOT
    # her own plugin delivery posts in her chat, under her label
    outcome, _ = await call_tool("assistant", LINK, _ellen_turn())
    assert outcome == "delivered"
    # her own file goes to her chat, through her bot
    try:
        from output_boundary_testing import with_scope
    except ImportError:
        from tests.output_boundary_testing import with_scope
    with _Origin(with_scope(_ellen_turn())):
        assert (await _send_media("mine.pdf"))["status"] == "ok"
    # a task for Tina, behind her, posts nothing of Tina's and is answered to Ellen
    payload = await _delegate("butler")
    await _settled()
    assert payload["status"] == "ok" and payload["text"].startswith(ANSWER)
    sends = world.casa.recorder.sends()
    assert sends[0][1]["text"].startswith("\U0001F4CA Ellen\n")
    assert [n for n, _k in sends] == ["send_message", "send_document"]
    assert world.alex.recorder.sends() == []
    assert chat_notes.drain(CASA_BOT, OPERATOR) == []


# ---------------------------------------------------------------------------
# Casa's own cards stay in Ellen's chat
# ---------------------------------------------------------------------------

def _real_cards(world, monkeypatch, transport=None):
    """The lifecycle's cards through the production host's card methods, on
    the real endpoints."""
    real = ovl.TelegramHost()
    monkeypatch.setattr(real, "_transport", lambda: transport or world.transport)
    for name in ("post_card", "post_ask", "edit_card"):
        monkeypatch.setattr(world.host, name, getattr(real, name))
    return real


async def test_casas_own_chat_cards_go_to_ellens_chat_whatever_agent_they_are_about(
        world, monkeypatch):
    import verdict_broker
    _real_cards(world, monkeypatch)
    with _Origin(_ellen_turn()):
        res = json.loads((await tools.offer_own_chat.handler(
            {"agent": "browser", "username": "HeyFidoTestBot"}))["content"][0]["text"])
    assert res["status"] == "offered", res
    # a new bot that matches no suggestion: Casa asks which agent it is for
    FIDO_BOT = 333
    world.w.tokens[FIDO_BOT] = "333:fido-token-bbbbbbbbbbbbbbbbbbbb"
    await world.lc.on_created(created(FIDO_BOT, "Somebody", "SomebodyHomeBot"), OPERATOR)
    sends = world.casa.recorder.sends()
    assert len(sends) == 2
    assert "Fido" in sends[0][1]["text"]
    assert "Which agent is it for?" in sends[1][1]["text"]
    assert {k["chat_id"] for _n, k in sends} == {OPERATOR}
    assert verdict_broker.BROKER.pending(namespace="resident_ask",
                                         scope=f"ask:{CASA_BOT}:{OPERATOR}") != []
    assert world.alex.recorder.sends() == []


async def test_casas_ask_card_before_casas_bot_has_an_id_registers_and_posts_nothing(
        world, monkeypatch):
    """Casa's bot has never completed getMe and no id is known for it: the
    question about a new bot is neither registered nor posted; the lifecycle
    asks again later."""
    import verdict_broker
    from channels.tg_address import EndpointNotReady
    bot = RecordingBot(0)
    casa = TelegramChannel(chat_id=str(OPERATOR), bot=bot, bus=MessageBus(),
                           default_agent="assistant", is_casa=True, known_bot_id=None)
    casa._app = SimpleNamespace(bot=bot)
    transport = make_transport(casa, casa=casa,
                               casa_bot_ids=MemoryBotIdStore(None)).get("telegram")
    host = _real_cards(world, monkeypatch, transport)

    async def on_answer(_idx):
        raise AssertionError("never asked")

    with pytest.raises(EndpointNotReady):
        await host.post_ask("a" * 32, "Which agent is it for?", ["Alex"], on_answer)
    assert verdict_broker.BROKER._live == {}
    assert bot.calls == []


# ---------------------------------------------------------------------------
# A card Alex left in Ellen's chat before his own chat existed refuses its tap
# ---------------------------------------------------------------------------

MOVED_CARD_LINE = "This card belongs to Alex's chat now — open it there."


@pytest.mark.parametrize("namespace", ["proposal", "resident_ask"])
async def test_a_card_alex_left_in_ellens_chat_refuses_its_tap_with_one_line(
        world, namespace):
    import verdict_broker
    rid = "f" * 32
    scope = (f"proposal:{CASA_BOT}:{OPERATOR}" if namespace == "proposal"
             else f"dm:{CASA_BOT}:{OPERATOR}")
    meta = {"options": ["Yes", "No"], "chat_id": OPERATOR, "bot_id": CASA_BOT,
            "operator_id": OPERATOR, "role": "finance", "message_id": 501, "_scope": scope,
            "deadline": 10_000.0, "plugin_seg": "probe", "label": "📊 Alex",
            "text": "📊 Alex\nPair 17?", "artifact_id": ARTIFACT, "revision": "",
            "owner": "d-1", "calls": [{"server": "api", "wire_name": "apply",
                                        "runtime_name": "mcp__plugin_probe_api__apply",
                                        "proposal": False, "arguments": {}, "canonical": "{}"}]
            * 2}
    verdict_broker.BROKER.register(namespace=namespace, scope=scope, request_id=rid,
                                   timeout_s=600, detached=True, meta=meta)
    answers = await _tap(world.casa, f"v1|{namespace}|{rid}|0", 501)
    assert answers == [MOVED_CARD_LINE]
    assert verdict_broker.BROKER.pending(namespace=namespace, scope=scope) == [rid]
    assert world.casa.recorder.sends() == [] and world.alex.recorder.sends() == []
    assert _queued(world.bus, "assistant") == [] and _queued(world.bus, "finance") == []


# ---------------------------------------------------------------------------
# A protected call in Alex's own chat is approved in Alex's chat and continues
# there
# ---------------------------------------------------------------------------

PAY = "mcp__plugin_demo_api__pay"


async def test_a_protected_call_in_alexs_chat_is_asked_and_continued_there(world):
    import verdict_broker
    from authz_grants import AuthzDeps, ChallengeCoordinator, GrantStore, \
        make_resident_authz_hook
    grants = GrantStore()
    hook = make_resident_authz_hook(
        "finance", {PAY: {"artifact_id": ARTIFACT, "summary": "pay the invoice"}},
        lambda: AuthzDeps(channel=world.transport, grants=grants,
                          challenges=ChallengeCoordinator()))
    call = {"tool_name": PAY, "tool_input": {"amount": 42}}
    with _Origin(_alex_turn()):
        out = await hook(call, "tu-1", {})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    for _ in range(10):
        await asyncio.sleep(0)
    [(name, card)] = world.alex.recorder.sends()
    assert name == "send_message" and card["chat_id"] == OPERATOR
    [rid] = verdict_broker.BROKER.pending(namespace="resident_ask",
                                          scope=f"authz:{ALEX_BOT}:{OPERATOR}")
    # the operator approves, on Alex's bot
    await _tap(world.alex, f"v1|resident_ask|{rid}|0", 500)
    for _ in range(20):
        await asyncio.sleep(0)
    [turn] = _queued(world.bus, "finance")
    assert turn.context["_tg_bot"] == ALEX_BOT and turn.context["synthetic"] == "button"
    with _Origin(_alex_turn()):
        assert await hook(call, "tu-2", {}) == {}            # the approval is consumed
    assert _ellen_texts(world) == [] and _queued(world.bus, "assistant") == []


# ---------------------------------------------------------------------------
# One lane per agent: taps, continuations and Ellen's tasks share it; a fault
# stops Alex's plugins until restart; the conversation never waits on a task
# ---------------------------------------------------------------------------

async def test_alexs_work_shares_one_lane_and_his_conversation_never_waits_on_it(
        world, monkeypatch, conversation):
    import plugin_continuations as pc
    call, _did = await _live_delegation(world, monkeypatch)
    lane = agent_lane.LANES.get("finance")
    assert lane.lock.locked()                                  # Ellen's task holds it
    # a continuation of Alex's run waits for it
    monkeypatch.setattr(pc, "RETRY_EVERY_S", 0.001)
    entered = []

    async def continuation():
        import time
        async with pc._lane_turn(OPERATOR, "finance", deadline=time.monotonic() + 5):
            entered.append(True)

    waiting = asyncio.ensure_future(continuation())
    await asyncio.sleep(0.02)
    assert entered == []
    # meanwhile the operator talks to Alex: his conversation answers at once
    await asyncio.wait_for(
        conversation.agent.handle_message(_dm_to_alex("are you there?", "7")), 5)
    assert conversation.client.prompts and entered == []
    Held.release.set()
    await call
    await asyncio.wait_for(waiting, 5)
    assert entered == [True]


async def test_a_fault_stops_alexs_plugins_until_restart_and_no_one_elses(world):
    agent_lane.FAULTS.mark("finance", "run-1", PLUGIN, reason="unconfirmed")
    outcome, why = await call_tool("finance", READ, _alex_turn())
    assert outcome == "denied" and "Alex is out of service until Casa restarts." in why
    payload = await _delegate()
    assert (payload["status"], payload["kind"]) == ("error", "agent_faulted")
    assert (await call_tool("assistant", READ, _ellen_turn()))[0] == "admitted"
    agent_lane.reset()                                         # Casa restarts
    assert (await call_tool("finance", READ, _alex_turn()))[0] == "admitted"
    assert world.alex.recorder.sends() == [] and _ellen_texts(world) == []


# ---------------------------------------------------------------------------
# A shared plugin's callback goes to the agent whose sign-in it answers
# ---------------------------------------------------------------------------

@pytest.fixture
def ingress(world, tmp_path, monkeypatch):
    """The real callback ingress over a real spool, for a plugin both Ellen and
    Alex use."""
    import callback_attempts
    import callback_episodes as ce
    import callback_spool
    spool = callback_spool.CallbackSpool(tmp_path / "callbacks")
    spool.ensure_plugin_dirs(PLUGIN)
    dispatched: list = []
    clock = {"t": 1_000_000.0}

    async def dispatch(role, text, context):
        dispatched.append((role, context))
        return True

    async def nothing(*_a):
        return None

    monkeypatch.setattr(ce, "_worker_task", None)
    monkeypatch.setattr(ce, "_kick", None)
    monkeypatch.setattr(ce, "_now", lambda: clock["t"])
    ce._pending_hints.clear()
    ce.configure(dispatch=dispatch,
                 resolve_registry_entry=lambda plugin: {
                     "targets": ["resident:assistant", "specialist:finance"]},
                 get_spool=lambda: spool, notify_operator=nothing, sleep=nothing)

    async def callback() -> list:
        h = os.urandom(32).hex()
        path = spool.root / PLUGIN / callback_spool.RESULTS_DIR / f"{h}.json"
        path.write_text(json.dumps({"v": 1, "plugin": PLUGIN}), encoding="utf-8")
        os.utime(path, (clock["t"], clock["t"]))
        rec = callback_attempts.new_attempt(state_hash=h, minted_ts=clock["t"],
                                            status="result_ready", now=clock["t"])
        assert spool.write_attempt(PLUGIN, h, rec)
        dispatched.clear()
        await ce._worker_pass()
        return list(dispatched)

    return callback


async def test_a_shared_plugins_callback_follows_the_sign_in_it_answers(world, ingress):
    async def sign_in(_client):
        assert (await call_tool("finance", LINK))[0] == "delivered"

    Client.on_query = sign_in
    await _delegate()
    await _settled()
    assert await ingress() == [("finance", {"synthetic": "callback_nudge",
                                            "_tg_bot": ALEX_BOT})]
    # Ellen signs in to the same plugin later: hers is the operation now
    assert (await call_tool("assistant", LINK, _ellen_turn()))[0] == "delivered"
    [(role, context)] = await ingress()
    assert role == "assistant" and context.get("_tg_bot") in (None, CASA_BOT)


# ---------------------------------------------------------------------------
# Jobs: the real launcher, engagement registry and in-Casa driver
# ---------------------------------------------------------------------------

JOB = "ledger:classify"


@pytest.fixture
async def jobs_rt(world, monkeypatch):
    import background_jobs as jobs
    import plugin_registry
    from drivers.in_casa_driver import InCasaDriver
    try:
        from tests.test_start_job import Client as JobClient, drain_launch_turns
    except ImportError:
        from test_start_job import Client as JobClient, drain_launch_turns
    loop = asyncio.get_running_loop()
    timer = None

    def tick():                      # a periodic wakeup, as the launch rig keeps one
        nonlocal timer
        timer = loop.call_later(.005, tick)
    tick()
    driver = InCasaDriver(persist_session_id=world.registry.persist_session_id,
                          record_lookup=world.registry.get)
    monkeypatch.setattr(agent_mod, "active_engagement_driver", driver, raising=False)
    for ep in (world.casa, world.alex):          # wired as Casa wires every endpoint
        monkeypatch.setattr(ep, "_engagement_driver", driver)
    monkeypatch.setattr(plugin_registry, "_snapshot", None)
    monkeypatch.setattr("drivers.in_casa_driver.ClaudeSDKClient", JobClient)
    monkeypatch.setattr(JobClient, "instances", [])
    decl = jobs.JobDecl(JOB, "ledger", "classify", JOB, "Classify entries", None)
    host = jobs.JobHost("specialist", "finance", decl, SimpleNamespace(name="ledger"))
    monkeypatch.setattr(jobs, "find_job_host",
                        lambda name, caller, roles: host if name == JOB else None)
    monkeypatch.setattr(jobs, "startable_jobs", lambda caller, roles: [host])
    monkeypatch.setattr(jobs, "own_job_hosts",
                        lambda role: [host] if role == "finance" else [])
    yield SimpleNamespace(driver=driver, client=JobClient, drain=drain_launch_turns)
    await drain_launch_turns()
    await tools.drain_launch_death_reports()
    for rec in world.registry.active_and_idle():
        await driver.cancel(rec)
    timer.cancel()


async def _start_job(origin: dict, rt) -> "object":
    token = agent_mod.origin_var.set(dict(origin))
    try:
        reply = json.loads((await tools.start_job.handler(
            {"job": JOB, "task": "Classify the ledger", "context": ""}))["content"][0]["text"])
    finally:
        agent_mod.origin_var.reset(token)
    assert reply["status"] == "pending", reply
    await rt.drain()
    [rec] = [r for r in tools._engagement_registry.active_and_idle()
             if r.id == reply.get("engagement_id", r.id)][-1:]
    return rec


async def _finish_job(world, rec, rt, text="12 rows classified.") -> None:
    await tools._finalize_engagement(rec, outcome="completed", text=text, next_steps=[],
                                     driver=rt.driver)
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.parametrize("starter", ["ellen", "alex"])
async def test_a_jobs_posts_are_in_alexs_chat_its_result_to_who_started_it_the_note_iff_ellen(
        world, jobs_rt, starter):
    origin = _ellen_turn() if starter == "ellen" else _alex_turn()
    rec = await _start_job(origin, jobs_rt)
    assert rec.delivery == ALEX_CHAT
    assert rec.requester.role == ("assistant" if starter == "ellen" else "finance")
    start_line = world.alex.recorder.sends()[0]
    assert start_line[1]["chat_id"] == OPERATOR
    await _finish_job(world, rec, jobs_rt)
    told = {role: _queued(world.bus, role) for role in ("assistant", "finance")}
    requester = "assistant" if starter == "ellen" else "finance"
    [done] = told[requester]
    assert "12 rows classified." in done.content.text
    assert told["finance" if starter == "ellen" else "assistant"] == []
    notes = chat_notes.drain(ALEX_BOT, OPERATOR)
    assert len(notes) == (1 if starter == "ellen" else 0)
    assert all(n.startswith("Ellen started your job: ") and n.endswith(" — it finished")
               for n in notes)
    assert _ellen_texts(world) == []


async def test_a_reply_and_a_file_to_a_live_jobs_post_reach_the_job_and_after_its_end_alex(
        world, jobs_rt, downloads):
    rec = await _start_job(_ellen_turn(), jobs_rt)
    client = jobs_rt.client.instances[-1]
    start_line = 500
    assert task_posts.owner(TgAddress(ALEX_BOT, OPERATOR, start_line)).task_id == rec.id
    await _handle(world.alex, _text("skip the rent rows", reply_to=start_line,
                                    quoted_text="Classify entries"))
    await jobs_rt.drain()
    assert any("skip the rent rows" in p for p in client.prompts[1:]), client.prompts
    await _handle(world.alex, _file("rows.pdf", reply_to=start_line))
    await jobs_rt.drain()
    [line] = [p for p in client.prompts if "[casa file] rows.pdf at " in p]
    path = line.split(" at ", 1)[1].split(" — ", 1)[0]
    assert agent_inbox.task_file_granted(rec.id, path)
    assert await _reads(client.options, path)
    assert _queued(world.bus, "finance") == []
    await _finish_job(world, rec, jobs_rt)
    _queued(world.bus, "assistant")
    await _handle(world.alex, _text("did it finish?", reply_to=start_line))
    [turn] = _queued(world.bus, "finance")
    assert turn.content == "did it finish?" and "ended" in turn.context["_reply_note"]
    assert _ellen_texts(world) == []


# ---------------------------------------------------------------------------
# Continuations: a plugin's run continued later, by the real continuation
# runner
# ---------------------------------------------------------------------------

async def _binding_from(world, agent: str, origin: "dict | None" = None):
    """The binding a plugin call of *agent* mints: inside Ellen's real task for
    it (*origin* None), or on the turn *origin*."""
    from authz_grants import resolve_grant_identity
    minted = []

    def mint(turn: dict):
        with _Origin(turn):
            identity, why = resolve_grant_identity(agent, artifact_id=ARTIFACT)
        assert why is None, why
        chain = tuple(turn.get("_handoff_chain") or (agent,))
        minted.append(plugin_bindings.BINDINGS.for_turn(
            plugin=PLUGIN, turn_key="t-" + agent, identity=identity, chain=chain,
            origin=turn))

    if origin is not None:
        mint(origin)
    else:
        async def inside(_client):
            mint(dict(agent_mod.origin_var.get()))
        Client.on_query = inside
        await _delegate(agent)
        await _settled()
        Client.on_query = None
    [binding] = minted
    return binding


async def _continue(binding) -> list:
    """One continuation of *binding*'s run: its turn calls the plugin's link
    capability. Returns what that call met."""
    import plugin_continuations as pc
    from plugin_bindings import binding_target
    met = []

    async def inside(_client):
        met.append(await call_tool(binding.chain[-1], LINK, client="c-cont"))

    Client.on_query = inside
    _FakeSpecialistClient.reset(response="The invoice is in your downloads.")
    try:
        bot_id = binding_target(binding)[1]
    except Exception:  # noqa: BLE001 — a binding with no delivery names Casa's bot
        bot_id = CASA_BOT
    await pc.run_continuation(pc.Pending(binding, "r-1", "sweep", 0.0, bot_id=bot_id),
                              on_start=lambda: None)
    await asyncio.sleep(0.05)
    await _settled()
    Client.on_query = None
    return met


@pytest.mark.parametrize("starter", ["ellen", "alex"])
async def test_a_continuations_posts_are_in_alexs_chat_its_result_to_its_requester(
        world, starter):
    origin = None if starter == "ellen" else _alex_turn()
    binding = await _binding_from(world, "finance", origin)
    assert plugin_bindings.delivery_of(binding) == ALEX_CHAT
    _queued(world.bus, "assistant")
    chat_notes.reset()
    before = len(world.alex.recorder.sends())
    met = await _continue(binding)
    assert [m[0] for m in met] == ["delivered"]
    assert len(world.alex.recorder.sends()) > before
    requester = "assistant" if starter == "ellen" else "finance"
    [done] = _queued(world.bus, requester)
    assert "The invoice is in your downloads." in done.content.text
    assert _queued(world.bus, "finance" if starter == "ellen" else "assistant") == []
    assert len(chat_notes.drain(ALEX_BOT, OPERATOR)) == (1 if starter == "ellen" else 0)
    assert _ellen_texts(world) == []


# ---------------------------------------------------------------------------
# A task born without an open chat posts nothing, all its life; its result
# reaches its requester
# ---------------------------------------------------------------------------

async def test_a_delegation_born_without_an_open_chat_posts_nothing_ever(world, outbox):
    """Fido has no bot yet. Ellen's task for him posts no start line, no
    answer, no surface; its result is hers."""
    seen = []

    async def mid_run(_client):
        seen.append(await call_tool("browser", LINK))
        seen.append(await _send_media())

    Client.on_query = mid_run
    payload = await _delegate("browser")
    await _settled()
    assert payload["status"] == "ok" and payload["text"].startswith(ANSWER)
    assert seen[0][0] == "denied" and seen[1]["status"] == "error"
    assert world.alex.recorder.sends() == []
    assert _ellen_texts(world) == []
    assert tools._TASK_INPUTS == {} and task_posts.delivery_of(payload["delegation_id"]) is None
    assert chat_notes.drain(CASA_BOT, OPERATOR) == []


async def test_a_task_born_before_alexs_chat_opened_posts_nothing_even_once_it_is_open(
        world, monkeypatch):
    """Alex's chat is closed when Ellen hands him a task; it opens while the
    task runs and a continuation of its plugin's run comes later. None of it
    posts, anywhere; each result reaches Ellen."""
    with monkeypatch.context() as closed:
        closed.setattr(world.lc, "state", lambda role: "not_open"
                       if role == "finance" else ovl.Lifecycle.state(world.lc, role))
        binding = await _binding_from(world, "finance")
    assert plugin_bindings.delivery_of(binding) is None
    assert own_voice.own_chat_state("finance") == "open"
    _queued(world.bus, "assistant")
    met = await _continue(binding)
    assert met[0][0] == "denied" and "was not open when this task started" in met[0][1]
    [done] = _queued(world.bus, "assistant")
    assert "The invoice is in your downloads." in done.content.text
    assert world.alex.recorder.sends() == []
    assert _ellen_texts(world) == []
    assert chat_notes.drain(ALEX_BOT, OPERATOR) == []


async def test_a_scheduled_job_of_an_agent_without_an_open_chat_posts_nothing(
        world, jobs_rt, monkeypatch):
    import background_jobs as jobs
    decl = jobs.JobDecl("web:fetch", "web", "fetch", "web:fetch", "Fetch a page", None)
    host = jobs.JobHost("specialist", "browser", decl, SimpleNamespace(name="web"))
    monkeypatch.setattr(jobs, "find_job_host", lambda name, caller, roles: host)
    trig = SimpleNamespace(job="web:fetch", name="nightly", channel="telegram", type="cron",
                           task="fetch", context="")
    await tools.start_scheduled_job("assistant", trig)
    await jobs_rt.drain()
    assert world.registry.active_and_idle() == []
    assert world.alex.recorder.sends() == [] and _ellen_texts(world) == []


# ---------------------------------------------------------------------------
# Structural pins: the closed lists of every agent-surface post and of every
# post record a send files
# ---------------------------------------------------------------------------

import ast  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402

CODE = pathlib.Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"
_SURFACE_METHOD = re.compile(r"^(deliver_operator_(?!reply$)\w+|post_job_\w+|deliver_run_card"
                             r"|post_question)$")
# methods whose name is also a common word in strings (a site name): calls only
_SURFACE_CALL = re.compile(r"^(send_media|send_reply_line)$")

# Admission sites: A — the plugin admission hook (result_broker), whose
# capability rule admits a call that posts only at its call's delivery;
# B — send_media, at the task's delivery; C — a job's launch on its host
# (tools._start_job_on_host), whose record keeps the job's delivery.
A, B, C = "A", "B", "C"
CASA = "casa"     # Casa's own card, on Casa's endpoint: no agent's surface

#: ``(file, function, method)`` → the site every post it makes has passed.
SURFACE_POSTS = {
    ("result_broker.py", "_post_operator_link", "deliver_operator_link"): A,
    ("result_broker.py", "_post_operator_message", "deliver_operator_message"): A,
    ("result_broker.py", "_post_operator_page", "deliver_operator_page"): A,
    ("result_broker.py", "_post_operator_proposal", "deliver_operator_proposal"): A,
    ("result_broker.py", "_post_operator_file", "deliver_operator_file"): A,
    # a plugin call that asks Casa (admitted at A): its question and run card
    ("operator_calls.py", "post_question._post", "deliver_operator_question"): A,
    ("run_admission.py", "_decide", "post_question"): A,
    ("run_admission.py", "_spawn_status_card._post", "deliver_run_card"): A,
    # a job's lines and question, at the delivery its launch recorded
    ("channels/telegram_transport.py", "TelegramTransport.post_job_question",
     "post_job_question"): C,
    ("channels/telegram_transport.py", "TelegramTransport.post_job_start_line",
     "post_job_start_line"): C,
    ("channels/telegram_transport.py", "TelegramTransport.post_job_parked",
     "post_job_parked"): C,
    ("tools.py", "ask_operator", "post_job_question"): C,
    ("tools.py", "_post_job_start_line", "post_job_start_line"): C,
    ("background_jobs.py", "JobRunner._post_parked", "post_job_parked"): C,
    # a file the agent sends, at the task's delivery
    ("tools.py", "_classify_send", "send_media"): B,
    ("channels/telegram_transport.py", "TelegramTransport.send_media", "send_media"): B,
    # the Stop's lines, a reply to the card they answer (a card admitted at A)
    ("plugin_stop.py", "post_line", "send_reply_line"): A,
    # the configurator's Restart now button: Casa's own, on Casa's endpoint
    ("configurator_run.py", "offer_restart", "post_question"): CASA,
}


def _owners(tree):
    out = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                out.append((child.lineno, child.end_lineno, name))
                walk(child, name)
            else:
                walk(child, prefix)
    walk(tree, "")
    return out


def _owner(owners, line):
    best = None
    for first, last, name in owners:
        if first <= line <= last and (best is None or first >= best[0]):
            best = (first, name)
    return best[1] if best else "<module>"


def _surface_posts() -> set:
    """Every call of a surface-posting method by name, and every method named
    by a string (a ``getattr`` of it), outside the endpoint that implements
    them."""
    found = set()
    for path in sorted(CODE.rglob("*.py")):
        rel = path.relative_to(CODE).as_posix()
        if rel == "channels/telegram.py":
            continue                                   # the endpoint's own methods
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owners = _owners(tree)
        for node in ast.walk(tree):
            name, call = None, False
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                call = True
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                name = node.value
            if name and (_SURFACE_METHOD.match(name)
                         or (call and _SURFACE_CALL.match(name))):
                found.add((rel, _owner(owners, node.lineno), name))
    return found


def test_every_agent_surface_post_passes_an_admission_site():
    found = _surface_posts()
    assert found == set(SURFACE_POSTS), {
        "unrecorded": sorted(found - set(SURFACE_POSTS)),
        "gone": sorted(set(SURFACE_POSTS) - found)}
    assert set(SURFACE_POSTS.values()) <= {A, B, C, CASA}


#: Every post record a send files (``PostRecord(...)``): whose role it names
#: and why that is the agent of the chat it lands in.
POST_RECORDS = {
    ("result_broker.py", "_deliver_and_replace"):
        "the call's executing agent; posted at the call's delivery, which the "
        "capability rule admitted only in that agent's own open chat (or the "
        "resident's own turn in its own chat)",
    ("stored_call_settler.py", "_post_next_card"):
        "the card's executing agent; posted at the card's own delivery",
    ("stored_call_settler.py", "handle_tap"):
        "the card's executing agent; posted in the chat the tap arrived in, "
        "which owns the card (a card of another agent's chat refuses at the "
        "callback's ingress)",
    ("tools.py", "_post_task_text"):
        "the task's executor; posted at the task's delivery, its own chat",
    ("tools.py", "_media_post"):
        "the agent sending the file; posted at the task's delivery, or in its "
        "own turn's chat when that chat is the agent's own (send_media's rule)",
    ("run_admission.py", "_card_post"):
        "the call's executing agent; its question and status card are posted at "
        "the call's delivery, as every capability post of the call",
    ("plugin_stop.py", "post_line"):
        "no agent: Casa's own Stop line, in the chat of the card it answers; the "
        "record only files the line under that card's task",
}


def _post_records() -> set:
    found = set()
    for path in sorted(CODE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owners = _owners(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name == "PostRecord":
                    found.add((path.relative_to(CODE).as_posix(),
                               _owner(owners, node.lineno)))
    return found


def test_every_post_record_names_the_agent_of_the_chat_it_lands_in():
    found = _post_records()
    assert found == set(POST_RECORDS), {
        "unrecorded": sorted(found - set(POST_RECORDS)),
        "gone": sorted(set(POST_RECORDS) - found)}


async def test_a_file_sent_after_tapping_a_live_tasks_paperclip_reaches_that_task(
        world, monkeypatch, downloads):
    """Alex's task posts a card with a 📎 button in his chat; the operator taps
    it and sends a file (not as a reply): the file goes to the task, which
    may read it."""
    import verdict_broker
    card = json.dumps({"text": "Which statement?",
                       "buttons": [{"label": "📎 Send it", "arm_file": True}]})

    async def post_card(_client):
        store = rb.ReferenceStore()
        admit = rb.make_plugin_admission_hook("finance", _map(), client_id="c1", store=store)
        result = rb.make_result_hook(_map(), client_id="c1", store=store)
        assert await admit({"hook_event_name": "PreToolUse", "tool_name": CARD,
                            "tool_input": {}}, "tu-c", {}) == {}
        ref, _ = store.deposit(client_id="c1", slot="card", value=card)
        await result({"hook_event_name": "PostToolUse", "tool_name": CARD, "tool_input": {},
                      "tool_response": json.dumps({"card": ref})}, "tu-c", {})

    Client.on_query = post_card
    call, did = await _live_delegation(world, monkeypatch)
    [rid] = verdict_broker.BROKER.pending(namespace="proposal",
                                          scope=f"proposal:{ALEX_BOT}:{OPERATOR}")
    card_mid = verdict_broker.BROKER.get_meta(
        namespace="proposal", scope=f"proposal:{ALEX_BOT}:{OPERATOR}",
        request_id=rid)["message_id"]
    assert task_posts.owner(TgAddress(ALEX_BOT, OPERATOR, card_mid)).task_id == did
    await _tap(world.alex, f"v1|proposal|{rid}|0", card_mid)
    await _handle(world.alex, _file("statement.pdf"))
    line = Held.built.writes[-1]
    assert line.startswith("[casa file] statement.pdf at ") and "a file for your post" in line
    path = line.split(" at ", 1)[1].split(" — ", 1)[0]
    assert await _reads(Held.built.captured_options, path)
    assert _queued(world.bus, "finance") == [] and _ellen_texts(world) == []
    Held.release.set()
    await call
    await _settled()


# ---------------------------------------------------------------------------
# A continuation in Alex's chat is a task there: a reply to its post reaches
# it, even before its session is up
# ---------------------------------------------------------------------------

def _mids_since(world, before: int) -> list[int]:
    return [k["_mid"] for _n, k in world.alex.recorder.sends()[before:] if "_mid" in k]


async def _live_continuation(world, monkeypatch, inside) -> tuple:
    """A continuation of the run Ellen's task for Alex started, held live;
    *inside* runs within its turn. Returns the running continuation and its
    task id."""
    import plugin_continuations as pc
    binding = await _binding_from(world, "finance")
    monkeypatch.setattr(tools, "ClaudeSDKClient", Held)
    monkeypatch.setattr(Held, "release", asyncio.Event())
    monkeypatch.setattr(Held, "built", None)
    Client.on_query = inside
    run = asyncio.ensure_future(pc.run_continuation(
        pc.Pending(binding, "r-1", "sweep", 0.0, bot_id=ALEX_BOT), on_start=lambda: None))
    for _ in range(500):
        await asyncio.sleep(0.002)
        live = [t for t in tools._TASK_INPUTS if task_input.state(t) == "live"]
        if live and Held.built is not None:
            Client.on_query = None
            return run, live[0]
        if run.done():
            break
    Client.on_query = None
    raise AssertionError(f"the continuation never went live: {run}")


async def _end_continuation(run) -> None:
    Held.release.set()
    await run
    await asyncio.sleep(0.05)
    await _settled()


async def test_a_reply_to_a_continuations_post_reaches_it_even_before_its_session_is_up(
        world, monkeypatch):
    """The continuation's post lands while its session is still starting; the
    operator's reply to it is queued for the continuation and written into its
    session once it is up, and a later reply goes straight in — never to
    Alex's conversation."""
    posted: list = []

    async def inside(_client):
        before = len(world.alex.recorder.sends())
        await call_tool("finance", LINK, client="c-cont", tu="t9")
        posted.extend(_mids_since(world, before))
        await _handle(world.alex, _text("use the March one", reply_to=posted[0]))

    run, cid = await _live_continuation(world, monkeypatch, inside)
    await asyncio.sleep(0.01)
    assert Held.built.writes[-1].endswith("use the March one")
    await _handle(world.alex, _text("and April", reply_to=posted[0]))
    assert Held.built.writes[-1].endswith("and April")
    assert _queued(world.bus, "finance") == []
    await _end_continuation(run)
    assert task_input.state(cid) == "ended"


# ---------------------------------------------------------------------------
# Every post a task makes is filed under that task, whatever path it takes;
# a reply to any of them reaches the task
# ---------------------------------------------------------------------------

#: Every kind of post a task makes in its executor's own chat, and the
#: endpoint method it lands through. Closed: every agent-surface method of
#: ``SURFACE_POSTS`` (outside Casa's own cards) must be driven by a kind here,
#: so a new way for a task to post is driven, and found filed, or this fails.
TASK_POST_KINDS = {
    "delegation start line": "send_response",
    "delegation answer": "send_response",
    "plugin link": "deliver_operator_link",
    "plugin message": "deliver_operator_message",
    "plugin page": "deliver_operator_page",
    "plugin proposal": "deliver_operator_proposal",
    "plugin file": "deliver_operator_file",
    "send_media": "send_media",
    "run admission question": "deliver_operator_question",
    "run status card": "deliver_run_card",
    "Stop line": "send_reply_line",
    "job start line": "post_job_start_line",
    "job question": "post_job_question",
    "job parked line": "post_job_parked",
    "job send_media": "send_media",
    "continuation plugin post": "deliver_operator_link",
    "continuation send_media": "send_media",
}

RUN = {"spec_digest": "sha256:aa", "mode": "account", "task_kind": "download invoice",
       "task_text": "t", "sites": [{"site_id": "runpod", "access": "account",
                                    "name": "RunPod"}]}


async def _landed(world, landed: dict, kind: str, step) -> object:
    """Run *step* (an awaitable) and file under *kind* the messages it landed
    in Alex's chat."""
    before = len(world.alex.recorder.sends())
    out = await step
    landed.setdefault(kind, []).extend(_mids_since(world, before))
    return out


def _operator_call(role: str, *, words: "str | None" = None, tu: str):
    """The operator call a plugin of *role* makes on the turn running now."""
    from authz_grants import resolve_grant_identity
    origin = dict(agent_mod.origin_var.get())
    identity, why = resolve_grant_identity(role, artifact_id=ARTIFACT)
    assert why is None, why
    if words is not None:
        origin["_operator_words"] = words
    return operator_calls.CALLS.open(client_id="c-run", tool_use_id=tu, tool_name=LINK,
                                     role=role, plugin=PLUGIN, identity=identity,
                                     origin=origin)


async def _task_surfaces(world, monkeypatch, outbox) -> tuple:
    """Ellen's task for Alex, held live; inside its run every surface a task
    has posts once. Returns the pending call, the task id and what landed,
    by kind."""
    landed: dict = {}
    path = os.path.join(outbox._root_realpath, "rows.txt")
    with open(path, "w") as fh:
        fh.write("12 rows\n")
    named: list = []

    async def sites_named(_words):
        return named

    monkeypatch.setattr(run_admission, "sites_named", sites_named)
    page_card = ("card", json.dumps({"text": "Keep this?", "pages": ["The October rows."],
                                     "buttons": [{"label": "Close", "close": True}]}), {})

    async def inside(_client):
        start = _mids_since(world, 0)
        landed["delegation start line"] = start
        await _landed(world, landed, "plugin link", call_tool("finance", LINK, tu="t1"))
        await _landed(world, landed, "plugin message", call_tool("finance", NOTE, tu="t2"))
        before = len(world.alex.recorder.sends())
        await call_tool("finance", CARD, tu="t3", deposit=page_card)
        page, card = _mids_since(world, before)
        landed["plugin page"], landed["plugin proposal"] = [page], [card]
        await _landed(world, landed, "plugin file", call_tool(
            "finance", FILE, tu="t4", deposit=("export", path, {"kind": "text"})))
        await _landed(world, landed, "send_media", _send_media())
        await _landed(world, landed, "run admission question", run_admission.request_admission(
            _operator_call("finance", tu="t5"), [dict(RUN)], "k-question"))
        named.append("runpod")
        before = len(world.alex.recorder.sends())
        await run_admission.request_admission(
            _operator_call("finance", words="get my RunPod invoice", tu="t6"),
            [dict(RUN)], "k-card")
        await asyncio.gather(*list(run_admission._CARD_TASKS))
        landed["run status card"] = _mids_since(world, before)

    Client.on_query = inside
    call, did = await _live_delegation(world, monkeypatch)
    Client.on_query = None
    [card] = landed["run status card"]
    import plugin_stop
    await _landed(world, landed, "Stop line", plugin_stop.tell_line(
        ALEX_BOT, OPERATOR, "finance", "stopped", reply_to=card))
    return call, did, landed


def _owner_of(mid: int):
    ref = task_posts.owner(TgAddress(ALEX_BOT, OPERATOR, mid))
    return ref.task_id if ref is not None else None


async def _job_surfaces(world, rt, outbox) -> tuple:
    """A job Ellen starts for Alex: its start line, a question, a parking line
    and a file, each landing in Alex's chat. Returns the job and what landed."""
    from output_boundary_testing import admitted
    landed: dict = {}
    rec = await _landed(world, landed, "job start line", _start_job(_ellen_turn(), rt))
    await _landed(world, landed, "job question", world.transport.post_job_question(
        rec, "q1", admitted("📊 Alex\nWhich account?"), ["A", "B"], ["A", "B"]))
    await _landed(world, landed, "job parked line", world.transport.post_job_parked(
        rec, "Waiting for you."))
    token = tools.engagement_var.set(rec)
    origin_token = agent_mod.origin_var.set(dict(rec.origin))
    try:
        media = await _landed(world, landed, "job send_media", _send_media("job.pdf"))
    finally:
        agent_mod.origin_var.reset(origin_token)
        tools.engagement_var.reset(token)
    assert media["status"] == "ok", media
    return rec, landed


async def test_every_post_a_task_makes_is_filed_under_it(world, monkeypatch, outbox, jobs_rt):
    """Each kind of post a task makes — a delegation's, a job's, a
    continuation's — is driven through the real endpoint, and every message
    it landed is filed under its task: a reply to it reaches the task."""
    surface_methods = {m for (_f, _fn, m), site in SURFACE_POSTS.items() if site != CASA}
    driven = set(TASK_POST_KINDS.values())
    assert surface_methods - {"post_question"} <= driven, surface_methods - driven

    found: dict = {}
    # a delegation
    call, did, landed = await _task_surfaces(world, monkeypatch, outbox)
    for kind, mids in landed.items():
        found[kind] = (did, mids, [_owner_of(m) for m in mids])
    Held.release.set()
    assert (await call)["status"] == "ok"
    await _settled()
    answer = [a for a in world.specialists.job_registry.task_posts(did)
              if a.message_id not in {m for _t, ms, _o in found.values() for m in ms}]
    found["delegation answer"] = (did, [a.message_id for a in answer], [
        ref.task_id if (ref := task_posts.ended_owner(a)) is not None else None
        for a in answer])
    # a job
    rec, landed = await _job_surfaces(world, jobs_rt, outbox)
    for kind, mids in landed.items():
        found[kind] = (rec.id, mids, [_owner_of(m) for m in mids])
    # a continuation
    cont: dict = {}

    async def inside(_client):
        await _landed(world, cont, "continuation plugin post",
                      call_tool("finance", LINK, client="c-cont", tu="t9"))
        await _landed(world, cont, "continuation send_media", _send_media("cont.pdf"))

    run, cid = await _live_continuation(world, monkeypatch, inside)
    for kind, mids in cont.items():
        found[kind] = (cid, mids, [_owner_of(m) for m in mids])
    await _end_continuation(run)

    assert set(found) == set(TASK_POST_KINDS), set(TASK_POST_KINDS) ^ set(found)
    unfiled = {kind: (task, mids, owners) for kind, (task, mids, owners) in found.items()
               if not mids or owners != [task] * len(mids)}
    assert unfiled == {}
    assert _ellen_texts(world) == []


@pytest.mark.parametrize("kind", ["send_media", "run admission question",
                                  "run status card", "Stop line"])
async def test_a_reply_to_a_tasks_file_question_card_or_stop_line_reaches_the_task(
        world, monkeypatch, outbox, kind):
    call, did, landed = await _task_surfaces(world, monkeypatch, outbox)
    [mid] = landed[kind]
    await _handle(world.alex, _text("the March one, please", reply_to=mid))
    assert Held.built.writes[-1].endswith("the March one, please")
    assert _queued(world.bus, "finance") == []
    Held.release.set()
    await call
    await _settled()
