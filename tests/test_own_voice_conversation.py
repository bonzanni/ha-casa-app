"""An agent with its own chat keeps one conversation there, run like the
resident's.

The conversation is an ``Agent`` for the specialist's role, on the bus like a
resident and on the same session machinery: its session key is
``(telegram, role, chat)``, its turns go through the warm pool and the resume
decision, ``/new`` ends only its own session. Its surface is the specialist's,
the one its tasks get (``tools._build_specialist_options``), with the session
extras a task gets (``specialist_session``): the long-run compaction window,
its pad, its run in the agent's log. What it writes is captured as the
specialist's. Casa's notes for its chat are drained into its next turn, once.

Two bots over one transport (``two_endpoints``): 111 is Casa's (Ellen,
``assistant``), 222 is Alex's (``finance``); the operator is 100.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import agent as agent_mod
import agent_inbox
import chat_notes
import own_voice
import scratchpad
import specialist_session
import tools as tools_mod
from agent import Agent
from agent_logs import LogKey
from bus import BusMessage, MessageType
from config import (
    AgentConfig, CharacterConfig, MemoryConfig, SessionConfig, ToolsConfig,
)
from mcp_registry import McpServerRegistry
from provenance import task_delivery, task_delivery_marker
from session_registry import (
    SessionRegistry, build_scoped_session_key, reset_channel,
)
from specialist_registry import DelegationComplete
from channels.tg_address import TgAddress
from session_reg_helpers import RESIDENT_DIGEST, resident_prov, resident_role_id
from test_agent_process import (
    _mk_assistant, _mk_result, _mk_tool_result, _mk_tool_use, _person_typed,
)
from test_memory_capture import events, mem  # noqa: F401 — fixture
from test_scratchpad import pads  # noqa: F401 — fixture
from test_specialist_session import bindings, logs  # noqa: F401 — fixtures
from two_endpoints import CASA_BOT, FINANCE_BOT, OPERATOR, build_two_endpoints, calls

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

pytestmark = [pytest.mark.asyncio]

LONG = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
ALEX_KEY = build_scoped_session_key("telegram", "finance", OPERATOR)
ELLEN_KEY = build_scoped_session_key("telegram", "assistant", OPERATOR)


class _Client:
    """The SDK client the pool builds: records the options it was built with
    and every prompt it was sent; answers with ``reply`` and ``sid``, after
    ``script`` when one is set."""

    made: list = []
    prompts: list = []
    reply = "noted"
    sid = "sid-alex-1"
    script: list = []

    @classmethod
    def reset(cls) -> None:
        cls.made, cls.prompts, cls.script = [], [], []
        cls.reply, cls.sid = "noted", "sid-alex-1"

    def __init__(self, options):
        type(self).made.append(options)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def query(self, text):
        type(self).prompts.append(text)

    async def receive_response(self):
        for item in type(self).script:
            yield item
        yield _mk_assistant(type(self).reply)
        yield _mk_result(type(self).sid)


def _specialist(role="finance", name="Alex", *, long_runs=False, budget=0):
    return AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT, role=role, model="claude-sonnet-4-6",
        system_prompt=f"You are {name}.", character=CharacterConfig(name=name),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=budget),
        session=SessionConfig(long_runs=long_runs),
        role_id=f"specialist:{role}", kind="specialist")


def _resident(role="assistant"):
    return AgentConfig(
        role_artifact=STUB_ROLE_ARTIFACT, role=role, model="claude-sonnet-4-6",
        system_prompt="You are Ellen.", character=CharacterConfig(name="Ellen"),
        tools=ToolsConfig(allowed=["Read"], permission_mode="acceptEdits"),
        memory=MemoryConfig(token_budget=0), role_id=resident_role_id(role),
        kind="resident", binding_digest=RESIDENT_DIGEST,
        speaker_provenance=resident_prov(role))


def _dm(text, *, role="finance", bot=FINANCE_BOT, mid="7", **context):
    """The operator's direct message, as the endpoint of bot *bot* puts it on
    the bus for its agent *role*."""
    msg = BusMessage(
        type=MessageType.CHANNEL_IN, source="telegram", target=role,
        content=text, channel="telegram",
        context={"chat_id": OPERATOR, "user_id": OPERATOR, "message_id": mid,
                 "_origin_route": "telegram", "_origin_clearance": "private",
                 "_operator_turn": True, "_tg_bot": bot, **context})
    msg.trusted_user_origin = _person_typed()
    return msg


@pytest.fixture
def world(tmp_path, monkeypatch, pads, logs, bindings):  # noqa: F811
    """Alex's conversation and Ellen's, over one session registry and the
    two-bot transport; Alex has an inbox."""
    _Client.reset()
    chat_notes.reset()
    eps = build_two_endpoints()
    monkeypatch.setattr(tools_mod, "_channel_manager", eps.manager, raising=False)
    registry = SessionRegistry(str(tmp_path / "sessions.json"))
    agent_inbox._reset_for_tests()
    agent_inbox._inboxes["finance"] = agent_inbox.open_inbox(
        "finance", str(tmp_path / "inbox"))

    from agent_registry import AgentRegistry
    known = AgentRegistry.build(
        residents={"assistant": _resident()},
        specialists={"finance": _specialist(), "browser": _specialist("browser", "Fido")})
    monkeypatch.setattr(tools_mod, "_agent_registry", known, raising=False)

    def _agent(cfg):
        cfg.cwd = str(tmp_path / "home" / cfg.role)
        return Agent(config=cfg, session_registry=registry,
                     mcp_registry=McpServerRegistry(),
                     channel_manager=eps.manager, agent_registry=known)

    alex = _agent(_specialist())
    ellen = _agent(_resident())
    with patch("sdk_client_pool._default_make_client", _Client):
        yield SimpleNamespace(alex=alex, ellen=ellen, registry=registry,
                              eps=eps, agent=_agent, pads=pads, logs=logs)
    agent_inbox._reset_for_tests()
    chat_notes.reset()


# --- the session machinery ------------------------------------------------------------------


async def test_a_direct_message_is_the_agents_own_turn_and_resumes_its_session(world):
    """A direct message in Alex's chat is a turn of Alex's conversation: its
    session is keyed by Alex's role and chat, the reply leaves through Alex's
    bot, and a second message — on a cold reconnect too — resumes the same
    session. Ellen's session is untouched."""
    await world.alex.handle_message(_dm("how much on groceries?"))

    entry = world.registry.get(ALEX_KEY)
    assert entry["agent"] == "specialist:finance"
    assert entry["sdk_session_id"] == "sid-alex-1"
    assert world.registry.get(ELLEN_KEY) is None
    sent = [c[0] for c in calls(world.eps.finance)]
    assert sent[0] == "create_on_token" and sent[-1] == "finalize_response_stream"
    assert calls(world.eps.casa) == []
    assert _Client.made[0].resume is None

    # The warm client answers the next turn: one client, two prompts.
    await world.alex.handle_message(_dm("and September?", mid="8"))
    assert len(_Client.made) == 1 and len(_Client.prompts) == 2

    # A cold reconnect (a reload builds a new pool) resumes the session.
    await world.alex._pool.close_key(ALEX_KEY)
    await world.alex.handle_message(_dm("and August?", mid="9"))
    assert len(_Client.made) == 2
    assert _Client.made[1].resume == "sid-alex-1"


async def test_new_ends_only_the_agents_own_session_and_its_pad(world):
    """``/new`` in Alex's chat resets Alex's session key alone: Ellen's
    session stays, and Alex's pad goes with his session (another pad stays)."""
    await world.ellen.handle_message(_dm("hello", role="assistant", bot=CASA_BOT))
    _Client.sid = "sid-ellen"   # the next client is Alex's, with its own sid
    await world.alex.handle_message(_dm("hi Alex"))
    assert world.registry.get(ELLEN_KEY) is not None
    assert world.registry.get(ALEX_KEY) is not None
    scratchpad.PADS.seed(ALEX_KEY, ["## brief", "Q3 accounts"])
    scratchpad.PADS.seed("another-session", ["## brief", "keep"])

    await reset_channel(ALEX_KEY, world.registry)

    assert world.registry.get(ALEX_KEY) is None
    assert world.registry.get(ELLEN_KEY) is not None
    assert world.pads.keys() == ["another-session"]


async def test_a_new_session_starts_a_new_pad(world):
    """The pad lives as long as its session: a fresh session's first build
    lets the previous session's pad go; a resumed one keeps it."""
    await world.alex.handle_message(_dm("hi"))
    scratchpad.PADS.seed(ALEX_KEY, ["## brief", "kept while resumed"])
    await world.alex._pool.close_key(ALEX_KEY)
    await world.alex.handle_message(_dm("again", mid="8"))      # resumed
    assert world.pads.keys() == [ALEX_KEY]
    await world.registry.clear_sdk_session(ALEX_KEY, expected_sid="sid-alex-1")
    await world.alex._pool.close_key(ALEX_KEY)
    await world.alex.handle_message(_dm("fresh", mid="9"))      # a new session
    assert world.pads.keys() == []


# --- the specialist's surface ----------------------------------------------------------------


def _surface(options):
    pre = (options.hooks or {}).get("PreToolUse", [])
    return dict(allowed=sorted(options.allowed_tools),
                disallowed=sorted(options.disallowed_tools),
                plugins=options.plugins, cwd=options.cwd, model=options.model,
                permission_mode=options.permission_mode,
                max_turns=options.max_turns, servers=sorted(options.mcp_servers),
                pre_tool_use=len(pre))


async def test_the_conversation_has_the_specialists_surface_not_the_residents(world):
    """Alex's conversation tools are his task tools: the same allowed and
    disallowed tools (the inbox tools and hand-off among them), plugins, cwd
    and PreToolUse guards as the specialist builder gives a delegated run.
    The conversation adds only what a conversation carries: its resume and
    the read-evidence hooks."""
    conversation = await world.alex._build_options(
        channel="telegram", channel_key=ALEX_KEY, is_fresh=True,
        resume_sid="sid-x", user_text="hi")
    task = tools_mod._build_specialist_options(world.alex.config)

    assert _surface(conversation) == _surface(task)
    for grant in (*agent_inbox.INBOX_TOOLS, "mcp__casa-framework__hand_off"):
        assert grant in conversation.allowed_tools
    assert conversation.resume == "sid-x" and task.resume is None
    assert set(conversation.hooks) >= {"PostToolUse", "PostToolUseFailure"}
    assert conversation.system_prompt.startswith("You are Alex.")
    assert "<channel_context>\nchannel: telegram\n" in conversation.system_prompt
    assert "<delegates>" not in conversation.system_prompt


@pytest.mark.parametrize("long_runs", [True, False])
async def test_the_conversation_has_its_tasks_session_extras(world, monkeypatch, long_runs):
    """Fido's direct conversation gets the extras his task gets: the
    compaction window when his runs are long, the pad (eager then) keyed by
    the conversation's session key, and a run in his agent's log with its
    steps and words. A conversation turn is never a task."""
    fido = world.agent(_specialist("browser", "Fido", long_runs=long_runs))
    seen: list = []
    real = specialist_session.session_env_and_pad

    def _spy(cfg, origin, *, run_id):
        seen.append((dict(origin), run_id))
        return real(cfg, origin, run_id=run_id)
    monkeypatch.setattr(specialist_session, "session_env_and_pad", _spy)
    _Client.script = [_mk_tool_use("t1", "Read", {"file_path": "/x"}),
                      _mk_tool_result("t1", is_error=False, text="ok")]

    msg = _dm("find the receipt", role="browser",
              **task_delivery_marker(TgAddress(FINANCE_BOT, OPERATOR)))
    await fido.handle_message(msg)

    key = build_scoped_session_key("telegram", "browser", OPERATOR)
    # keyed by the session key; the conversation's origin carries no task delivery
    (origin, run_id), = seen
    assert run_id == key
    assert task_delivery(origin) == (False, None)
    from drivers.turn_progress import step_log_line
    assert world.logs.texts(LogKey("agent", "browser")) == [
        "▶ started", step_log_line("Read"), "noted", "✓ answered"]

    options = _Client.made[0]
    task = real(fido.config, {}, run_id="S").apply(
        tools_mod._build_specialist_options(fido.config))
    assert (LONG in options.env) is long_runs
    assert options.env.get(LONG) == task.env.get(LONG)
    pad = options.mcp_servers[scratchpad.SERVER]
    assert pad.get("alwaysLoad") == task.mcp_servers[scratchpad.SERVER].get("alwaysLoad")
    assert (pad.get("alwaysLoad") is True) is long_runs
    assert scratchpad.GRANT in options.allowed_tools
    assert scratchpad.PROMPT_LINE in options.system_prompt


async def test_a_failed_turn_ends_its_run_as_aborted(world, monkeypatch):
    """The run in the agent's log ends ``⚠️ aborted`` when the turn fails."""
    class _Broken(_Client):
        async def query(self, text):
            raise ValueError("boom")
    with patch("sdk_client_pool._default_make_client", _Broken):
        await world.alex.handle_message(_dm("hi"))
    assert world.logs.texts(LogKey("agent", "finance")) == ["▶ started", "⚠️ aborted"]


async def test_what_the_agent_writes_is_captured_as_the_specialists(world, mem):  # noqa: F811
    """Captured memory is labelled with the speaker's kind: the operator's
    message as his, Alex's answer as a specialist's — never a resident's."""
    await world.alex._process(_dm("how much on groceries?"))
    rows = [(e["speaker_kind"], e["role"], e["text"]) for e in events(mem)]
    assert rows == [("operator", "finance", "how much on groceries?"),
                    ("specialist", "finance", "noted")]


# --- notes and results -----------------------------------------------------------------------


async def test_casas_notes_for_the_chat_are_drained_once_into_its_agents_turn(world):
    """A note Casa kept for Alex's chat reaches Alex's next turn, marked as
    Casa's, and only that one; a note for Ellen's chat is not Alex's."""
    chat_notes.record(FINANCE_BOT, OPERATOR,
                      "Ellen asked you «groceries in October»; you answered (412 chars)")
    chat_notes.record(CASA_BOT, OPERATOR, "a note for Ellen's chat")

    await world.alex.handle_message(_dm("and September?"))
    await world.alex.handle_message(_dm("and August?", mid="8"))

    first, second = _Client.prompts
    assert "(Casa) Ellen asked you «groceries in October»; you answered (412 chars)" in first
    assert (f"{agent_mod.CHAT_NOTES_FRAME}\n(Casa) Ellen asked you «groceries in October»"
            in first)
    assert agent_mod.CHAT_NOTES_FRAME not in second
    assert "Ellen asked you" not in second
    assert "a note for Ellen's chat" not in first
    assert chat_notes.drain(CASA_BOT, OPERATOR) == ["a note for Ellen's chat"]


async def test_a_turn_that_opens_the_chat_says_so_in_its_notes(world):
    """The turn that opens Alex's chat carries Casa's line that the operator
    just opened it, on a line of its own — not among what Alex did for others
    — and only that turn does."""
    chat_notes.record(FINANCE_BOT, OPERATOR, "Ellen asked you: groceries — you answered")
    await world.alex.handle_message(_dm("hi", _chat_opening=True))
    await world.alex.handle_message(_dm("and September?", mid="8"))

    first, second = _Client.prompts
    assert first.count(agent_mod.CHAT_OPENING_NOTE) == 1
    framed = first[first.index(agent_mod.CHAT_NOTES_FRAME):]
    assert agent_mod.CHAT_OPENING_NOTE not in framed
    assert agent_mod.CHAT_OPENING_NOTE not in second


async def test_a_turn_that_opens_a_moved_chat_says_it_is_new(world):
    """The first turn in Alex's new chat, after a handle change, carries Casa's
    line that the operator opened his new chat — not the first-chat line —
    and only that turn does."""
    await world.alex.handle_message(_dm("hi", _chat_opening="moved"))
    await world.alex.handle_message(_dm("and September?", mid="8"))

    first, second = _Client.prompts
    assert first.count(agent_mod.CHAT_MOVED_NOTE) == 1
    assert agent_mod.CHAT_OPENING_NOTE not in first
    assert agent_mod.CHAT_MOVED_NOTE not in second
    assert agent_mod.CHAT_OPENING_NOTE not in second


async def test_a_first_message_handed_back_and_taken_on_retry_greets_once(
        world, tmp_path, monkeypatch):
    """Alex's chat is bound but his conversation is not on the bus yet: the
    operator's first message is handed back to the chat's lifecycle, which is
    busy; meanwhile the conversation registers, the lifecycle lets the message
    through and the retry is taken. That turn opens the chat; the next one
    carries no second greeting."""
    import asyncio

    import own_voice_lifecycle as ovl
    from own_voice_world import World, created, make_lifecycle, specialist
    from test_own_chat_routing import _endpoint, _handle, _noop, _text, _turns

    voices = World(configs={"finance": specialist("Alex")}, queues=set(),
                   tokens={FINANCE_BOT: f"{FINANCE_BOT}:alex-token-aaaaaaaaaaaaaaaa"})
    lc, host = make_lifecycle(voices, tmp_path / "voices", channel=False)
    monkeypatch.setattr(host, "operator_id", lambda: OPERATOR)
    await lc.post_offer("finance", "HeyAlexTestBot")
    await lc.on_created(created(FINANCE_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    assert lc.state("finance") == "not_open"
    ovl.install(lc)
    try:
        ch, bus = _endpoint("own", register=False)
        asked = asyncio.Event()
        inner = lc.intake

        async def intake(bot_id, update_json):
            asked.set()
            return await inner(bot_id, update_json)
        lc.intake = intake
        busy = lc._lock(FINANCE_BOT)
        await busy.acquire()
        first = asyncio.create_task(_handle(ch, _text("hi")))
        await asyncio.wait_for(asked.wait(), 5)      # handed back, waiting on the lifecycle
        bus.register("finance", _noop)
        voices.queues.add("finance")
        busy.release()
        await asyncio.wait_for(first, 5)
        await _handle(ch, _text("are you there?", mid=71))

        turns = _turns(bus, "finance")
        assert [t.content for t in turns] == ["hi", "are you there?"]
        for turn in turns:
            await world.alex.handle_message(turn)
        assert [p.count(agent_mod.CHAT_OPENING_NOTE) for p in _Client.prompts] == [1, 0]
        assert lc.state("finance") == "open"
    finally:
        ovl.install(None)


@pytest.mark.parametrize("lands", [True, False])
async def test_the_greeting_opens_the_chat_only_once_its_reply_landed(
        world, tmp_path, monkeypatch, lands):
    """Casa's greeting turn reaches Alex as his chat's opening: his prompt
    carries the opening line, he answers whole through his own bot (no
    stream), and the chat is open once that reply landed. A reply Telegram
    did not take leaves the chat unopened, and the operator's first message
    is then the turn that opens it."""
    import own_voice_lifecycle as ovl
    from own_voice_world import World, created, make_lifecycle, specialist
    from test_own_chat_routing import _endpoint, _handle, _text, _turns

    voices = World(configs={"finance": specialist("Alex")}, queues=set(),
                   tokens={FINANCE_BOT: f"{FINANCE_BOT}:alex-token-aaaaaaaaaaaaaaaa"})
    lc, host = make_lifecycle(voices, tmp_path / "voices", channel=False)
    monkeypatch.setattr(host, "operator_id", lambda: OPERATOR)
    await lc.post_offer("finance", "HeyAlexTestBot")
    await lc.on_created(created(FINANCE_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    ovl.install(lc)
    try:
        ch, bus = _endpoint("own")
        assert await ch.speak_first() is True
        [greeting] = _turns(bus, "finance")
        world.eps.finance._ready = lands
        await world.alex.handle_message(greeting)
        assert _Client.prompts[0].count(agent_mod.CHAT_OPENING_NOTE) == 1
        sent = [c[0] for c in calls(world.eps.finance)]
        assert "create_on_token" not in sent and sent[-1] == "send_response"
        assert lc.state("finance") == ("open" if lands else "not_open")
        # In Casa the bot's endpoint that sent the greeting is the one that
        # ends its window (refusal or delivery); here the reply went through
        # this test's endpoint double, so the window is closed by hand.
        ch._end_greeting()
        await _handle(ch, _text("/start"))
        [turn] = _turns(bus, "finance")
        assert turn.context.get("_chat_opening") is (None if lands else True)
    finally:
        ovl.install(None)


async def test_the_agents_surfaces_may_post_while_its_greeting_is_on_its_way(
        world, tmp_path, monkeypatch):
    """Casa's greeting turn tells Alex the operator opened his chat, and the
    chat opens with that greeting: while it is on its way his surfaces may
    post (a tool checked during his hello is not refused as an unopened
    chat). A greeting that reached no one leaves the chat as it was."""
    import own_voice
    import own_voice_lifecycle as ovl
    from own_voice_world import World, created, make_lifecycle, specialist
    from test_own_chat_routing import _endpoint, _turns

    voices = World(configs={"finance": specialist("Alex")}, queues=set(),
                   tokens={FINANCE_BOT: f"{FINANCE_BOT}:alex-token-aaaaaaaaaaaaaaaa"})
    lc, host = make_lifecycle(voices, tmp_path / "voices", channel=False)
    monkeypatch.setattr(host, "operator_id", lambda: OPERATOR)
    await lc.post_offer("finance", "HeyAlexTestBot")
    await lc.on_created(created(FINANCE_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    ovl.install(lc)
    try:
        ch, bus = _endpoint("own")
        refused = own_voice.require_open_chat("finance", label="Alex")
        assert refused is not None and refused.kind == "own_chat_not_open"
        assert await ch.speak_first() is True
        [_greeting] = _turns(bus, "finance")
        assert lc.state("finance") == "not_open"
        assert own_voice.require_open_chat("finance", label="Alex") is None
        ch._end_greeting()                       # it reached no one
        refused = own_voice.require_open_chat("finance", label="Alex")
        assert refused is not None and refused.kind == "own_chat_not_open"
    finally:
        ovl.install(None)


@pytest.mark.parametrize("ending", ["not_started", "stopped"])
async def test_a_greeting_that_can_no_longer_arrive_ends_its_window(
        world, tmp_path, monkeypatch, ending):
    """The greeting's reply cannot be sent (the endpoint is not started), or
    the endpoint stops while it is on its way: the window ends there, and the
    agent's surfaces are refused again as an unopened chat."""
    import own_voice
    import own_voice_lifecycle as ovl
    from own_voice_world import World, created, make_lifecycle, specialist
    from test_own_chat_routing import _endpoint, _turns

    voices = World(configs={"finance": specialist("Alex")}, queues=set(),
                   tokens={FINANCE_BOT: f"{FINANCE_BOT}:alex-token-aaaaaaaaaaaaaaaa"})
    lc, host = make_lifecycle(voices, tmp_path / "voices", channel=False)
    monkeypatch.setattr(host, "operator_id", lambda: OPERATOR)
    await lc.post_offer("finance", "HeyAlexTestBot")
    await lc.on_created(created(FINANCE_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    ovl.install(lc)
    try:
        ch, bus = _endpoint("own")
        assert await ch.speak_first() is True
        [greeting] = _turns(bus, "finance")
        assert own_voice.require_open_chat("finance", label="Alex") is None
        if ending == "not_started":
            ch._app = None
            await ch.send_response("Hi, Alex here.", greeting.context)
        else:
            ch._app = None              # this double has no PTB application to tear down
            await ch.stop()
        refused = own_voice.require_open_chat("finance", label="Alex")
        assert refused is not None and refused.kind == "own_chat_not_open"
        assert ch._greeting_timer is None
    finally:
        ovl.install(None)


async def test_a_silent_greeting_opens_nothing(world, tmp_path, monkeypatch):
    """Alex chooses silence on Casa's greeting turn: nothing reached the
    operator, so the chat stays unopened and his first message opens it."""
    import own_voice_lifecycle as ovl
    from own_voice_world import World, created, make_lifecycle, specialist
    from test_own_chat_routing import _endpoint, _handle, _text, _turns

    voices = World(configs={"finance": specialist("Alex")}, queues=set(),
                   tokens={FINANCE_BOT: f"{FINANCE_BOT}:alex-token-aaaaaaaaaaaaaaaa"})
    lc, host = make_lifecycle(voices, tmp_path / "voices", channel=False)
    monkeypatch.setattr(host, "operator_id", lambda: OPERATOR)
    await lc.post_offer("finance", "HeyAlexTestBot")
    await lc.on_created(created(FINANCE_BOT, "AlexTest", "HeyAlexTestBot"), OPERATOR)
    ovl.install(lc)
    try:
        ch, bus = _endpoint("own")
        assert await ch.speak_first() is True
        [greeting] = _turns(bus, "finance")
        _Client.reply = "<silent/>"
        await world.alex.handle_message(greeting)
        assert lc.state("finance") == "not_open"
        await ch.turn_finished({"chat_id": str(OPERATOR), "synthetic": "chat_opening"})
        await _handle(ch, _text("/start"))
        [turn] = _turns(bus, "finance")
        assert turn.context.get("_chat_opening") is True
    finally:
        ovl.install(None)


async def test_a_job_started_in_the_agents_chat_narrates_its_result_there(world):
    """A job Alex's conversation started has Alex's conversation as its
    requester: its completion is a turn of that conversation, on Alex's
    session, answered through Alex's bot — nothing through Ellen's."""
    origin = {"role": "finance", "channel": "telegram", "chat_id": OPERATOR,
              "user_id": OPERATOR, "user_text": "sync the bank feed",
              "_origin_route": "telegram", "_origin_clearance": "private",
              "_tg_bot": FINANCE_BOT}
    done = DelegationComplete(delegation_id="eng-1", agent="finance", status="ok",
                              text="12 transactions imported", origin=origin)
    note = BusMessage(type=MessageType.NOTIFICATION, source="finance",
                      target="finance", content=done, channel="telegram",
                      context={"chat_id": OPERATOR, "cid": "c1"})

    await world.alex.handle_message(note)

    assert "12 transactions imported" in _Client.prompts[0]
    assert world.registry.get(ALEX_KEY)["sdk_session_id"] == "sid-alex-1"
    assert world.registry.get(ELLEN_KEY) is None
    assert [c[0] for c in calls(world.eps.finance)][-1] == "finalize_response_stream"
    assert calls(world.eps.casa) == []


# --- boot and reload -------------------------------------------------------------------------


async def test_boot_builds_a_conversation_only_for_a_declared_agent_and_releases_nothing(
        monkeypatch, caplog):
    """Boot puts an Agent on the bus for each specialist whose committed
    component declares its own chat, and releases nothing held for it: the
    own chats' reconcile does that, after the queues are live. A reconcile
    that fails is logged, never fatal."""
    import casa_core
    released: list = []
    order: list = []
    monkeypatch.setattr(own_voice, "declared", lambda role: role == "finance")

    async def _released(role):
        released.append(role)
    monkeypatch.setattr(own_voice, "on_queue_registered", _released)

    configs = {"finance": _specialist(), "browser": _specialist("browser", "Fido")}
    built = casa_core._own_conversation_agents(
        configs, construct=lambda cfg: order.append(cfg.role) or cfg.role)
    assert built == {"finance": "finance"} and order == ["finance"]
    assert released == []

    async def _reconcile():
        order.append("reconcile")
    monkeypatch.setattr(own_voice, "boot_reconcile", _reconcile)
    await casa_core._reconcile_own_chats()
    assert order == ["finance", "reconcile"]

    async def _boom():
        raise RuntimeError("no Casa bot")
    monkeypatch.setattr(own_voice, "boot_reconcile", _boom)
    with caplog.at_level(logging.ERROR):
        await casa_core._reconcile_own_chats()
    assert "own chats not reconciled at boot" in caplog.text


async def test_a_conversation_that_cannot_be_built_does_not_stop_the_others(monkeypatch):
    import casa_core
    monkeypatch.setattr(own_voice, "declared", lambda role: True)

    def _construct(cfg):
        if cfg.role == "browser":
            raise RuntimeError("no home")
        return cfg.role
    built = casa_core._own_conversation_agents(
        {"browser": _specialist("browser", "Fido"), "finance": _specialist()},
        construct=_construct)
    assert built == {"finance": "finance"}


def _reload_runtime(tmp_path, roles):
    from runtime import CasaRuntime
    agents_dir = tmp_path / "agents"
    for role in roles:
        (agents_dir / "specialists" / role).mkdir(parents=True)
    rt = CasaRuntime(
        agents={}, role_configs={}, specialist_registry=MagicMock(),
        engagement_registry=MagicMock(), agent_registry=MagicMock(),
        trigger_registry=MagicMock(), mcp_registry=MagicMock(),
        session_registry=MagicMock(), channel_manager=MagicMock(),
        bus=MagicMock(), engagement_driver=MagicMock(), policy_lib=MagicMock(),
        config_dir=str(tmp_path), agents_dir=str(agents_dir),
        home_root=str(tmp_path / "home"), defaults_root="/opt/casa")
    rt.specialist_registry.load = MagicMock()
    rt.specialist_registry.load_failures = MagicMock(return_value=[])
    rt.specialist_registry.disabled_roles = MagicMock(return_value=[])
    rt.specialist_registry.all_configs = MagicMock(
        return_value={r: SimpleNamespace(role=r, character=SimpleNamespace(name=r, card=""),
                                         triggers=[], channels=[])
                      for r in roles})
    return rt


async def test_a_reload_releases_what_a_declared_agents_chat_held(tmp_path, monkeypatch):
    """Every reload that registers a specialist's Agent again tells the own
    chat its queue is back — for a declared agent only, after the Agent is on
    the bus: the agents sweep's backfill and the single-agent reload."""
    import reload as reload_mod
    from reload import dispatch, register_handler, reload_agent
    register_handler("agent", reload_agent)
    released: list = []

    async def _released(role):
        assert role in rt.agents                  # on the bus first
        released.append(role)
    monkeypatch.setattr(own_voice, "declared", lambda role: role == "finance")
    monkeypatch.setattr(own_voice, "on_queue_registered", _released)
    monkeypatch.setattr(reload_mod, "_construct_agent", lambda *a, **kw: MagicMock())
    monkeypatch.setattr("policies.load_policies", lambda *a, **kw: MagicMock())
    monkeypatch.setattr("agent_home.provision_agent_home", lambda **kw: None)
    rt = _reload_runtime(tmp_path, ["finance", "probe"])

    result = await dispatch("agents", runtime=rt)
    assert result["status"] == "ok"
    assert released == ["finance"]

    monkeypatch.setattr("agent_loader.load_agent_from_dir", lambda *a, **kw:
                        rt.specialist_registry.all_configs()["finance"])
    result = await dispatch("agent", runtime=rt, role="finance")
    assert result["status"] == "ok", result
    assert released == ["finance", "finance"]
