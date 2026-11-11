"""Tasks of an agent with its own chat (the task model).

A delegation or a job is a task: its launcher decides, once, where its posts go
(its *delivery*) and who receives its result (its *requester*). Here Ellen
(``assistant``, Casa's bot 111) delegates to Alex (``finance``, his own bot
222); both serve operator 100. Pinned:

* a task of Alex's open chat posts its start line and the message Alex closed
  his turn with, whole (never Ellen's bounded copy, never the notes he wrote
  between tool calls; nothing when he closed silent) — in Alex's chat, every
  page under the task; Ellen gets her bounded result, told where the person
  saw it; Alex's conversation gets a note;
* what the person wrote straight into the task reaches Ellen once;
* the task's input destination exists before its first post: a reply sent
  while the run's client does not exist yet is queued for the task and
  written into its session after the brief, never taken for the conversation;
* ``send_media`` of a task goes to the task's delivery, never to the route of
  the turn that started it; a task born with no delivery refuses, even when
  the chat opened while it ran;
* a job starts only where its host's own chat is open; a scheduled job that
  cannot start says nothing in Ellen's chat;
* each job's requester is the agent whose turn started it.

The own-chat lifecycle, the post index and the input lane are separate
modules; their state is modelled here through their public functions.
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import agent as agent_mod
import agent_lane
import chat_notes
import own_voice
import plugin_outbox
import task_input
import task_posts
import tools
from bus import MessageBus
from config import CharacterConfig
from channels.telegram import TelegramChannel
from channels.tg_address import TgAddress
from specialist_registry import SpecialistRegistry
from task_model import Requester

from two_endpoints import CASA_BOT, FINANCE_BOT, OPERATOR, FakeEndpoint, build_two_endpoints

try:
    from tests.test_delegate_to_agent import (
        _caller_cfg, _FakeSpecialistClient, _seed_specialist_dir)
    from tests.test_specialist_registry import _use_synthetic_roles_dir
except ImportError:
    from test_delegate_to_agent import _caller_cfg, _FakeSpecialistClient, _seed_specialist_dir
    from test_specialist_registry import _use_synthetic_roles_dir

pytestmark = pytest.mark.asyncio

ALEX_CHAT = TgAddress(FINANCE_BOT, OPERATOR)
MARKER = "END-OF-ALEX-ANSWER-7f3c"
ECHO = "\n(Casa: echo of a posted card)"


# ---------------------------------------------------------------------------
# The modelled modules
# ---------------------------------------------------------------------------

class Inputs:
    """``task_input`` as specified: pending queues in order, attach flushes
    into the session then writes directly, close ends it."""

    def __init__(self):
        self.state: dict[str, str] = {}
        self.queue: dict[str, list[str]] = {}
        self.client: dict[str, object] = {}

    def open_pending(self, tid):
        self.state.setdefault(tid, "pending")
        self.queue.setdefault(tid, [])

    def attach(self, tid, client):
        if self.state.get(tid) != "pending":
            return
        self.state[tid] = "live"
        self.client[tid] = client
        for text in self.queue.pop(tid, []):
            client.writes.append(text)

    def submit(self, tid, text, *, said=None):
        state = self.state.get(tid)
        if state == "pending":
            self.queue[tid].append(text)
            return "queued"
        if state == "live":
            self.client[tid].writes.append(text)
            return "written"
        return "ended"

    def close(self, tid):
        self.state[tid] = "ended"


class Chats:
    """The lifecycle's chat states, switchable during a run."""

    def __init__(self, ends):
        self.state = {"finance": "open"}
        self.ends = ends

    def own_chat_state(self, role):
        return self.state.get(role, "not_declared")

    def endpoint_for(self, role):
        return self.ends.get(role)

    def require_open_chat(self, role, *, label):
        if role == "assistant" or self.state.get(role) == "open":
            return None
        kind = "own_chat_not_open" if role in self.state else "own_chat_missing"
        return own_voice.Refusal(kind, f"{label}'s chat isn't set up yet — ask Ellen to create it.")


class Client(_FakeSpecialistClient):
    """The specialist's SDK client: records what was written into its session
    (the brief first), and may be held at construction."""
    hold: "asyncio.Event | None" = None
    built = None
    on_query = None

    def __init__(self, options):
        super().__init__(options)
        self.writes: list[str] = []
        type(self).built = self

    async def __aenter__(self):
        if type(self).hold is not None:
            await type(self).hold.wait()
        return self

    async def query(self, text):
        await super().query(text)
        self.writes.append(text)
        if type(self).on_query is not None:
            await type(self).on_query(self)


def _alex_endpoint(bus):
    """Alex's endpoint: a real TelegramChannel over a fake bot, every landed
    message reported with the post record it was filed under."""
    sent = []
    filed = []
    counter = iter(range(500, 10_000))

    async def send_message(**kw):
        sent.append(kw)
        return SimpleNamespace(message_id=next(counter))

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send_message),
                          send_chat_action=AsyncMock(return_value=True),
                          send_document=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    ch = TelegramChannel(chat_id=str(OPERATOR), bot=bot, bus=bus, default_agent="finance",
                         known_bot_id=FINANCE_BOT)
    ch._app = SimpleNamespace(bot=bot)
    real_record = ch._record_post

    def _record_post(chat_id, sent_msg, post):
        filed.append((int(chat_id), getattr(sent_msg, "message_id", None), post))
        return real_record(chat_id, sent_msg, post)

    ch._record_post = _record_post
    ch.sent, ch.filed = sent, filed
    return ch


@pytest.fixture
def world(tmp_path, monkeypatch):
    agent_lane.reset()
    chat_notes.reset()
    bus = MessageBus()
    alex = _alex_endpoint(bus)
    casa = FakeEndpoint("assistant", bot_id=CASA_BOT, is_casa=True)
    te = build_two_endpoints(casa=casa, finance=alex)

    specialists = tmp_path / "ex"
    specialists.mkdir()
    _seed_specialist_dir(specialists, "finance", enabled=True)
    _use_synthetic_roles_dir(monkeypatch, tmp_path, "finance")
    reg = SpecialistRegistry(str(specialists), tombstone_path=str(tmp_path / "del.json"))
    reg.load()
    fin = reg.get("finance")
    fin.kind = "specialist"
    caller = _caller_cfg(delegates=("finance",))
    caller.kind = "resident"
    caller.character = CharacterConfig(name="Ellen")
    notified = []

    class _Bus:
        async def notify(self, msg):
            notified.append(msg)

        def __getattr__(self, name):
            return getattr(bus, name)

    tools.init_tools(te.manager, _Bus(), reg,
                     agent_role_map={"assistant": caller, "finance": fin})

    chats = Chats({"finance": alex})
    monkeypatch.setattr(own_voice, "own_chat_state", chats.own_chat_state)
    monkeypatch.setattr(own_voice, "endpoint_for", chats.endpoint_for)
    monkeypatch.setattr(own_voice, "require_open_chat", chats.require_open_chat)
    inputs = Inputs()
    for name in ("open_pending", "attach", "submit", "close"):
        monkeypatch.setattr(task_input, name, getattr(inputs, name))
    opened = []
    monkeypatch.setattr(task_posts, "open", lambda tid, d: opened.append((tid, d)))
    monkeypatch.setattr(task_posts, "record", lambda owner, addr: None)
    monkeypatch.setattr(tools, "ClaudeSDKClient", Client)
    monkeypatch.setattr(Client, "hold", None)
    monkeypatch.setattr(Client, "on_query", None)
    monkeypatch.setattr(Client, "built", None)
    real_echo = tools._with_post_echo
    monkeypatch.setattr(tools, "_with_post_echo",
                        lambda text, owner, **kw: real_echo(text, owner, **kw) + ECHO)
    yield SimpleNamespace(alex=alex, casa=casa, chats=chats, inputs=inputs, opened=opened,
                          notified=notified, reg=reg, te=te)
    agent_lane.reset()
    chat_notes.reset()


def _ellen_turn():
    return {"role": "assistant", "execution_role": "assistant", "channel": "telegram",
            "chat_id": OPERATOR, "user_id": OPERATOR, "cid": "c-ellen", "_tg_bot": CASA_BOT,
            "user_text": "groceries in october?"}


async def _delegate(mode="sync", task="How much on groceries in October?", origin=None):
    token = agent_mod.origin_var.set(origin or _ellen_turn())
    try:
        res = await tools.delegate_to_agent.handler(
            {"agent": "finance", "task": task, "context": "", "mode": mode})
    finally:
        agent_mod.origin_var.reset(token)
    return json.loads(res["content"][0]["text"])


async def _settled():
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.gather(*list(tools._delegation_settle_tasks))
    for _ in range(5):
        await asyncio.sleep(0)


def _long_answer():
    body = "\n\n".join(f"Paragraph {i}: " + "grocery line " * 40 for i in range(60))
    assert len(body) > 20_000
    return body + "\n\n" + MARKER


def _alex_texts(world):
    return [m["text"] for m in world.alex.sent]


def _casa_sends(world):
    return [c for c in world.casa.calls if c[0] in ("send", "send_response", "send_media")]


# ---------------------------------------------------------------------------
# The answer at the task's delivery; the result to the requester; the note
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_a_task_of_alexs_open_chat_shows_there_whole_and_ellen_gets_her_bounded_result(
        world, mode):
    _FakeSpecialistClient.reset(response=_long_answer())
    payload = await _delegate(mode)
    if mode == "async":
        assert payload["status"] == "pending", payload
        await asyncio.sleep(0.05)
        await _settled()
        (note,) = [m for m in world.notified if getattr(m.content, "delegation_id", "")]
        result = note.content.text
    else:
        assert payload["status"] == "ok", payload
        result = payload["text"]
    did = payload["delegation_id"]

    # Ellen's result: the bounded copy with Casa's echo, never the marker
    assert MARKER not in result and ECHO.strip() in result and len(result) < 21_000
    # Alex's chat: the start line, then the whole answer, marker included,
    # no echo; every landed message filed under the task
    texts = _alex_texts(world)
    assert texts[0].startswith("Ellen asked me: How much on groceries in October?")
    answer = "".join(texts[1:])
    assert MARKER in answer and ECHO.strip() not in answer
    assert len(texts) >= 3                       # paged
    assert [p.owner for _, _, p in world.alex.filed] == [did] * len(texts)
    assert {c for c, _, _ in world.alex.filed} == {OPERATOR}
    assert world.opened == [(did, ALEX_CHAT)]
    assert _casa_sends(world) == []
    # the conversation learns the outcome on its next turn, once
    (line,) = chat_notes.drain(FINANCE_BOT, OPERATOR)
    assert line == "Ellen asked you: How much on groceries in October? — you answered"
    assert chat_notes.drain(FINANCE_BOT, OPERATOR) == []
    # the record carries the task as born
    assert world.inputs.state[did] == "ended"


async def test_a_reply_while_the_task_is_pending_reaches_its_session_after_the_brief(world):
    """The client is held at construction; the operator replies to the start
    line the moment it lands. The reply waits for the task, is written into
    its session after the brief, and never reaches the conversation."""
    _FakeSpecialistClient.reset(response="€420, mostly Albert Heijn.")
    Client.hold = asyncio.Event()
    outcomes = []
    real_send = world.alex._app.bot.send_message.side_effect

    async def send_then_reply(**kw):
        sent = await real_send(**kw)
        if not outcomes:          # the start line just landed
            did = world.opened[0][0]
            outcomes.append(task_input.submit(did, "[replying to «Ellen asked…»] only Lidl"))
        return sent

    world.alex._app.bot.send_message.side_effect = send_then_reply
    task = asyncio.ensure_future(_delegate("async"))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if outcomes:
            break
    assert outcomes == ["queued"]
    Client.hold.set()
    await task
    await asyncio.sleep(0.05)
    await _settled()
    writes = Client.built.writes
    assert len(writes) == 2 and "How much on groceries" in writes[0]
    assert writes[1] == "[replying to «Ellen asked…»] only Lidl"


async def test_a_task_born_without_an_open_chat_posts_nothing_even_after_the_chat_opens(
        world, tmp_path):
    """Born while Alex's chat is not open: no start line, no index, no input,
    no answer. The chat opens mid-run; the task's send_media still refuses
    with the typed text, and its plugin calls carry no delivery. Ellen still
    gets her result."""
    world.chats.state["finance"] = "not_open"
    outbox = plugin_outbox.init_outbox(str(tmp_path / "outbox"))
    seen = {}

    async def mid_run(client):
        world.chats.state["finance"] = "open"
        origin = tools._snapshot_origin()
        from provenance import task_delivery
        seen["task_delivery"] = task_delivery(origin)
        path = os.path.join(outbox._root_realpath, "chart.pdf")
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.7\n" + b"x" * 64)
        res = await tools.send_media.handler({"path": path, "kind": "document"})
        seen["media"] = json.loads(res["content"][0]["text"])

    Client.on_query = mid_run
    _FakeSpecialistClient.reset(response="€420.")
    try:
        payload = await _delegate("sync")
    finally:
        outbox.close()
        plugin_outbox._OUTBOX = None
    assert payload["status"] == "ok" and payload["text"].startswith("€420.")
    assert seen["task_delivery"] == (True, None)          # what every surface copies
    assert seen["media"]["status"] == "error"
    assert seen["media"]["kind_error"] == "own_chat_not_open"
    assert "posts nothing" in seen["media"]["message"]
    assert world.alex.sent == [] and _casa_sends(world) == []
    assert world.opened == [] and world.inputs.state == {}
    assert chat_notes.drain(FINANCE_BOT, OPERATOR) == []


# ---------------------------------------------------------------------------
# Site B: a task's media goes to its delivery
# ---------------------------------------------------------------------------

async def test_delegated_alexs_media_goes_through_alexs_endpoint_only(world, tmp_path):
    outbox = plugin_outbox.init_outbox(str(tmp_path / "outbox"))
    results = []

    async def mid_run(client):
        path = os.path.join(outbox._root_realpath, "report.pdf")
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.7\n" + b"x" * 64)
        res = await tools.send_media.handler({"path": path, "kind": "document"})
        results.append(json.loads(res["content"][0]["text"]))

    Client.on_query = mid_run
    _FakeSpecialistClient.reset(response="Sent you the report.")
    try:
        payload = await _delegate("sync")
    finally:
        outbox.close()
        plugin_outbox._OUTBOX = None
    assert payload["status"] == "ok"
    assert results[0]["status"] == "ok", results
    docs = world.alex._app.bot.send_document.await_args_list
    assert len(docs) == 1 and docs[0].args[0] == OPERATOR
    assert _casa_sends(world) == []


async def test_a_turn_that_is_no_task_keeps_its_own_chat(world, tmp_path):
    """Ellen's own turn: her file goes to her chat, through her bot."""
    outbox = plugin_outbox.init_outbox(str(tmp_path / "outbox"))
    try:
        path = os.path.join(outbox._root_realpath, "mine.pdf")
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.7\n" + b"x" * 64)
        from output_boundary_testing import with_scope
        token = agent_mod.origin_var.set(with_scope(_ellen_turn()))
        try:
            res = json.loads((await tools.send_media.handler(
                {"path": path, "kind": "document"}))["content"][0]["text"])
        finally:
            agent_mod.origin_var.reset(token)
    finally:
        outbox.close()
        plugin_outbox._OUTBOX = None
    assert res["status"] == "ok", res
    assert [c[0] for c in _casa_sends(world)] == ["send_media"]
    assert world.alex._app.bot.send_document.await_count == 0


def _alex_conversation_turn():
    """A turn of Alex's own conversation: the operator wrote in Alex's chat.
    Never a task — no task delivery, no hand-off chain."""
    return {"role": "finance", "execution_role": "finance", "channel": "telegram",
            "chat_id": OPERATOR, "user_id": OPERATOR, "cid": "c-alex", "_tg_bot": FINANCE_BOT,
            "user_text": "and September?"}


async def test_alexs_conversation_turn_sends_media_in_its_own_chat_metered_per_turn(
        world, tmp_path):
    outbox = plugin_outbox.init_outbox(str(tmp_path / "outbox"))
    tools._MEDIA_SEND_DEBITS.clear()
    try:
        path = os.path.join(outbox._root_realpath, "sept.pdf")
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.7\n" + b"x" * 64)
        from output_boundary_testing import with_scope
        token = agent_mod.origin_var.set(with_scope(_alex_conversation_turn()))
        try:
            res = json.loads((await tools.send_media.handler(
                {"path": path, "kind": "document"}))["content"][0]["text"])
        finally:
            agent_mod.origin_var.reset(token)
    finally:
        outbox.close()
        plugin_outbox._OUTBOX = None
    assert res["status"] == "ok", res
    assert world.alex._app.bot.send_document.await_count == 1 and _casa_sends(world) == []
    assert dict(tools._MEDIA_SEND_DEBITS) == {"turn:finance:c-alex": 1}
    tools._MEDIA_SEND_DEBITS.clear()


# ---------------------------------------------------------------------------
# Site C and the scheduled refusal; the job's requester
# ---------------------------------------------------------------------------

def _host(role="finance", kind="specialist"):
    import background_jobs as jobs
    decl = jobs.JobDecl("ledger:classify", "ledger", "classify", "ledger:classify",
                        "Classify entries", None)
    return jobs.JobHost(kind, role, decl, SimpleNamespace(name="ledger"))


async def test_a_job_whose_hosts_chat_is_not_open_is_refused_before_its_claim(world, monkeypatch):
    import background_jobs as jobs
    world.chats.state["finance"] = "not_open"
    claims = []
    monkeypatch.setattr(jobs, "claim_job_start", lambda *a: claims.append(a))
    res = json.loads((await tools._start_job_on_host(
        _host(), "classify", "", _ellen_turn()))["content"][0]["text"])
    assert res["status"] == "error" and res["kind"] == "own_chat_not_open"
    assert "isn't set up yet" in res["message"]
    assert claims == []


async def test_a_scheduled_job_before_its_chat_exists_posts_nothing_in_ellens_chat(
        world, monkeypatch, caplog):
    import authz_grants
    import background_jobs as jobs
    world.chats.state["finance"] = "no_bot"
    monkeypatch.setattr(jobs, "find_job_host", lambda name, caller, roles: _host())
    monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: (OPERATOR, OPERATOR))
    launched = []
    monkeypatch.setattr(tools, "_launch_interactive_engagement",
                        AsyncMock(side_effect=lambda *a, **k: launched.append(a)))
    trig = SimpleNamespace(job="ledger:classify", name="nightly", channel="telegram",
                           type="cron", task="classify", context="")
    with caplog.at_level("INFO", logger="tools"):
        await tools.start_scheduled_job("assistant", trig)
    assert launched == []
    assert world.casa.calls == [] and world.alex.sent == []
    assert any("own_chat_not_open" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("origin,requester", [
    (_ellen_turn(), Requester("assistant", {"_tg_bot": CASA_BOT})),
    (_alex_conversation_turn(), Requester("finance", {"_tg_bot": FINANCE_BOT})),
])
async def test_each_job_completes_to_the_agent_whose_turn_started_it(
        world, monkeypatch, origin, requester):
    """A job Ellen started reports to Ellen and leaves Alex a note; one Alex's
    own conversation started reports to Alex and leaves none. Both post in
    Alex's chat."""
    import background_jobs as jobs
    births = []

    async def launch(*a, task_birth=None, **k):
        births.append(task_birth)
        return tools._result({"status": "pending", "engagement_id": "e1"})

    monkeypatch.setattr(tools, "_launch_interactive_engagement", launch)
    monkeypatch.setattr(jobs, "claim_job_start", lambda *a: None)
    monkeypatch.setattr(jobs, "release_job_start", lambda *a: None)
    await tools._start_job_on_host(_host(), "classify", "", dict(origin))
    (birth,) = births
    assert (birth.executor, birth.requester, birth.delivery) == ("finance", requester, ALEX_CHAT)
    rec = SimpleNamespace(id="e1", task="classify", executor=birth.executor,
                          requester=birth.requester, delivery=birth.delivery,
                          role_or_type="finance")
    tools._note_task(tools._record_birth(rec), "started your job", "it finished")
    notes = chat_notes.drain(FINANCE_BOT, OPERATOR)
    assert len(notes) == (1 if requester.role == "assistant" else 0)


# ---------------------------------------------------------------------------
# The lane and the fault
# ---------------------------------------------------------------------------

async def test_a_delegation_to_an_agent_out_of_service_is_refused_at_once(world):
    agent_lane.FAULTS.mark("finance", "run-1", "ledger", reason="unconfirmed")
    payload = await _delegate("async")
    assert payload["status"] == "error" and payload["kind"] == "agent_faulted"
    assert payload["message"] == "Finance is out of service until Casa restarts."
    assert world.alex.sent == [] and world.opened == []


# ---------------------------------------------------------------------------
# What a task posts as its answer: the message its delegate closed its turn
# with, never the notes it wrote between tool calls
# ---------------------------------------------------------------------------

def _script(monkeypatch, steps):
    """The delegate's turn as several text-bearing messages, a tool call and
    its result between them, as a real delegate's turn runs: each step is
    ``(words, tool)``; ``tool`` None closes the turn."""
    from claude_agent_sdk import (AssistantMessage, TextBlock, ToolResultBlock,
                                  ToolUseBlock, UserMessage)
    real = Client.receive_response

    async def receive_response(self):
        async for msg in real(self):
            if not isinstance(msg, AssistantMessage):
                yield msg
                continue
            for i, (words, tool) in enumerate(steps):
                content = [TextBlock(text=words)]
                if tool is not None:
                    content.append(ToolUseBlock(id=f"tu{i}", name=f"mcp__ledger__{tool}",
                                                input={}))
                yield AssistantMessage(content=content, model="m")
                if tool is not None:
                    yield UserMessage(content=[ToolResultBlock(tool_use_id=f"tu{i}",
                                                               content="ok")])
    monkeypatch.setattr(Client, "receive_response", receive_response)


async def _ellens_result(world, mode, task="How much on groceries in October?"):
    """Ellen's result of one delegation to Alex: the sync payload's text, or
    the late notice's once the async run settled."""
    payload = await _delegate(mode, task=task)
    if mode == "async":
        assert payload["status"] == "pending", payload
        await asyncio.sleep(0.05)
        await _settled()
        (note,) = [m for m in world.notified if getattr(m.content, "delegation_id", "")]
        return payload, note.content.text
    assert payload["status"] == "ok", payload
    return payload, payload["text"]


# Alex runs here under his role's display name
NAME = "Finance"
START = "Ellen asked me: How much on groceries in October?"


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_a_task_closing_silent_after_notes_posts_only_its_start_line(
        world, monkeypatch, mode):
    _FakeSpecialistClient.reset(response="unused")
    _script(monkeypatch, [("Fetch all eight in parallel.", "run_fetch"), ("<silent/>", None)])
    await _ellens_result(world, mode)
    assert _alex_texts(world) == [START]


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_a_task_posts_its_closing_message_not_its_notes(world, monkeypatch, mode):
    _FakeSpecialistClient.reset(response="unused")
    _script(monkeypatch, [("Reading the ledger now.", "run_fetch"),
                          ("€420, mostly Albert Heijn.", None)])
    _, result = await _ellens_result(world, mode)
    assert _alex_texts(world)[0] == START
    assert _alex_texts(world)[1:] == ["€420, mostly Albert Heijn."]
    assert "Reading the ledger" not in "".join(_alex_texts(world))
    # Ellen still reads the whole turn, notes included
    assert "Reading the ledger now." in result


async def test_a_quoted_brief_is_shown_as_written(world):
    _FakeSpecialistClient.reset(response="€420.")
    await _delegate("sync", task='"How much on groceries in October?"')
    assert _alex_texts(world)[0] == 'Ellen asked me: "How much on groceries in October?"'
    (line,) = chat_notes.drain(FINANCE_BOT, OPERATOR)
    assert line == 'Ellen asked you: "How much on groceries in October?" — you answered'


@pytest.mark.parametrize("synthetic", ["plugin_setup", "plugin_erase"])
async def test_a_task_casa_started_shows_only_its_answer(world, synthetic):
    # a plugin's setup or erase turn has no asker to name: only the answer shows
    _FakeSpecialistClient.reset(response="Paired.")
    payload = await _delegate(
        "sync", origin={**_ellen_turn(), "synthetic": synthetic,
                        f"{synthetic}_target": "finance"})
    assert payload["status"] == "ok", payload
    assert _alex_texts(world) == ["Paired."]


# ---------------------------------------------------------------------------
# What the person wrote straight into a running task reaches its requester
# ---------------------------------------------------------------------------

WORDS_LINE = (
    "(Casa: the person also wrote to Finance directly while it worked on this: «only Lidl». "
    "Finance got those words in the task itself and worked with them, so they are not yours "
    "to pass on or to start again as a new task.)")


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_the_requester_learns_what_the_person_wrote_into_the_task(
        world, monkeypatch, mode):
    said: dict[str, list[str]] = {}

    async def reply_mid_run(client):
        said[world.opened[0][0]] = ["only Lidl"]

    Client.on_query = reply_mid_run
    taken = []

    def take_said(tid):
        taken.append(tid)
        return said.pop(tid, [])

    monkeypatch.setattr(task_input, "take_said", take_said)
    _FakeSpecialistClient.reset(response="€395 without the Lidl runs.")
    payload, result = await _ellens_result(world, mode)
    assert result.endswith("\n\n" + WORDS_LINE)
    assert result.count("only Lidl") == 1
    assert taken == [payload["delegation_id"]]


# ---------------------------------------------------------------------------
# Ellen's notes for a task of Alex's own chat
# ---------------------------------------------------------------------------

def _post_card(world):
    """Alex's plugin posts a card in his chat mid-run, filed under the task."""
    import result_broker as rb

    async def post(client):
        rb.POSTS.record(world.opened[0][0], rb.PostEvent(
            tool_use_id="tu-card", plugin="ledger", slot="card", label="📊 Alex",
            pages=1, media_kind=None))

    Client.on_query = post


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_ellens_result_for_an_own_chat_task_with_posts_asks_her_to_close_the_loop(
        world, monkeypatch, mode):
    _post_card(world)
    _FakeSpecialistClient.reset(response="unused")
    _script(monkeypatch, [("Posting the card.", "post_card"), ("<silent/>", None)])
    _, result = await _ellens_result(world, mode)
    assert "📊 Alex posted to the person in Finance's chat (1 page)." in result
    assert tools.OWN_CHAT_ECHO_NOTE.format(name=NAME) in result
    assert tools.OWN_CHAT_SILENT_CLOSE_NOTE.format(name=NAME) in result
    assert tools.POST_ECHO_SILENCE_NOTE not in result
    assert tools.SILENT_CLOSE_NOTE.format(name=NAME) not in result
    assert tools.OWN_CHAT_ANSWER_NOTE.format(name=NAME) not in result


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_ellen_is_told_the_person_already_read_alexs_answer(world, monkeypatch, mode):
    _FakeSpecialistClient.reset(response="unused")
    _script(monkeypatch, [("Checking the rate.", "run_fetch"), ("EUR/USD is 1.08.", None)])
    _, result = await _ellens_result(world, mode)
    assert _alex_texts(world)[1:] == ["EUR/USD is 1.08."]
    assert result.endswith("\n\n" + tools.OWN_CHAT_ANSWER_NOTE.format(name=NAME))
    assert tools.SILENT_CLOSE_NOTE.format(name=NAME) not in result
    assert tools.OWN_CHAT_SILENT_CLOSE_NOTE.format(name=NAME) not in result


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_a_silent_close_is_never_called_an_answer_the_person_read(
        world, monkeypatch, mode):
    _FakeSpecialistClient.reset(response="unused")
    _script(monkeypatch, [("Checking the rate.", "run_fetch"), ("<silent/>", None)])
    _, result = await _ellens_result(world, mode)
    assert tools.OWN_CHAT_ANSWER_NOTE.format(name=NAME) not in result
    assert result.endswith(tools.OWN_CHAT_SILENT_CLOSE_NOTE.format(name=NAME))


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_the_persons_words_follow_the_answer_note(world, monkeypatch, mode):
    said: dict[str, list[str]] = {}

    async def reply_mid_run(client):
        said[world.opened[0][0]] = ["only Lidl"]

    Client.on_query = reply_mid_run
    monkeypatch.setattr(task_input, "take_said", lambda tid: said.pop(tid, []))
    _FakeSpecialistClient.reset(response="€395.")
    _, result = await _ellens_result(world, mode)
    note = tools.OWN_CHAT_ANSWER_NOTE.format(name=NAME)
    assert result.endswith(f"{note}\n\n{WORDS_LINE}")


# A closing answer longer than the result cap that opens with the marker: its
# capped copy is nothing but the marker and blank lines, its whole text is an
# answer.
MARKED_LONG_ANSWER = "<silent/>" + "\n" * 20000 + "EUR/USD is 1.08."


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_a_long_closing_answer_opening_with_the_marker_is_an_answer_not_silence(
        world, monkeypatch, mode):
    _FakeSpecialistClient.reset(response="unused")
    _script(monkeypatch, [("Checking the rate.", "run_fetch"), (MARKED_LONG_ANSWER, None)])
    _, result = await _ellens_result(world, mode)
    assert _alex_texts(world)[1:] == ["EUR/USD is 1.08."]
    assert tools.OWN_CHAT_ANSWER_NOTE.format(name=NAME) in result
    assert tools.OWN_CHAT_SILENT_CLOSE_NOTE.format(name=NAME) not in result
    assert tools.SILENT_CLOSE_NOTE.format(name=NAME) not in result


@pytest.mark.parametrize("closing,posted", [
    ("EUR/USD is 1.08.", "EUR/USD is 1.08."),
    ("<silent/>", None),
    ("EUR/USD is 1.08.\n\n<silent/>", "EUR/USD is 1.08."),
    (MARKED_LONG_ANSWER, "EUR/USD is 1.08."),
    ("<silent/>" + "\n" * 20000 + "<silent/>", None),
    ("<silent/> " * 3000, None),
], ids=["answer", "silence", "answer-then-marker", "marker-then-long-answer",
        "long-silence", "many-markers"])
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_ellens_notes_agree_with_what_alexs_chat_shows(
        world, monkeypatch, mode, closing, posted):
    """Every place that judges the closing message reads the same whole text:
    the answer note exactly when the answer posted, the silent-close note only
    for a closing silence, never both."""
    _FakeSpecialistClient.reset(response="unused")
    _script(monkeypatch, [("Checking the rate.", "run_fetch"), (closing, None)])
    _, result = await _ellens_result(world, mode)
    assert _alex_texts(world)[1:] == ([posted] if posted else [])
    answer_note = tools.OWN_CHAT_ANSWER_NOTE.format(name=NAME) in result
    silent_note = tools.OWN_CHAT_SILENT_CLOSE_NOTE.format(name=NAME) in result
    assert answer_note is (posted is not None)
    assert silent_note is ("<silent/>" in closing and posted is None)
    assert not (answer_note and silent_note)


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_ellen_is_not_told_the_person_read_an_answer_whose_chat_went_away(
        world, monkeypatch, mode):
    """Alex's bot is released while he works: his answer cannot post, so Ellen
    is not told the person already read it."""
    async def release_alexs_bot(client):
        tools._transport().remove_endpoint(world.alex)

    Client.on_query = release_alexs_bot
    _FakeSpecialistClient.reset(response="EUR/USD is 1.08.")
    _, result = await _ellens_result(world, mode)
    assert "EUR/USD is 1.08." not in _alex_texts(world)
    assert "EUR/USD is 1.08." in result
    assert tools.OWN_CHAT_ANSWER_NOTE.format(name=NAME) not in result


async def test_a_task_of_a_chat_that_is_not_open_keeps_ellens_usual_notes(world, monkeypatch):
    world.chats.state["finance"] = "not_open"
    _FakeSpecialistClient.reset(response="unused")
    _script(monkeypatch, [("Checking.", "run_fetch"), ("<silent/>", None)])
    _, result = await _ellens_result(world, "sync")
    assert result.endswith(tools.SILENT_CLOSE_NOTE.format(name=NAME))
    assert "Finance's chat" not in result
    assert tools.OWN_CHAT_ANSWER_NOTE.format(name=NAME) not in result


# ---------------------------------------------------------------------------
# A continuation: no birth of its own, its record's destinations
# ---------------------------------------------------------------------------

async def _continuation(world, monkeypatch, output, *, quiet_if=None, did="cont-1"):
    """A continuation's completion: the real callback over a record whose
    delivery is Alex's chat, with no birth passed. Returns the notices sent."""
    from specialist_registry import DelegationRecord

    class _Registry:
        job_registry = SimpleNamespace(ack_terminal_notification=AsyncMock())

        async def complete_delegation(self, *a, **kw):
            return None

    monkeypatch.setattr(tools, "_specialist_registry", _Registry())
    record = DelegationRecord(id=did, agent="finance", started_at=0.0, origin=_ellen_turn(),
                              executor="finance",
                              requester=Requester("assistant", {"_tg_bot": CASA_BOT}),
                              delivery=ALEX_CHAT)

    async def _run():
        return output

    task = asyncio.ensure_future(_run())
    tools._attach_completion_callback(task, record, quiet_if=quiet_if)
    await task
    await _settled()
    return [m for m in world.notified if getattr(m.content, "delegation_id", "") == did]


async def test_a_continuations_notes_follow_its_record_birth(world, monkeypatch):
    import result_broker as rb
    rb.POSTS.record("cont-1", rb.PostEvent(
        tool_use_id="tu-card", plugin="ledger", slot="card", label="📊 Alex",
        pages=1, media_kind=None))
    out = tools.DelegatedOutput(text="note\n\n<silent/>", run_subtype="success",
                                final_message="<silent/>")
    (notice,) = await _continuation(world, monkeypatch, out)
    text = notice.content.text
    assert tools.OWN_CHAT_ECHO_NOTE.format(name=NAME) in text
    assert tools.OWN_CHAT_SILENT_CLOSE_NOTE.format(name=NAME) in text
    # a continuation's answer is not posted in Alex's chat: never "already read"
    assert world.alex.sent == []
    assert tools.OWN_CHAT_ANSWER_NOTE.format(name=NAME) not in text


async def test_a_continuations_answer_is_never_called_read_in_alexs_chat(world, monkeypatch):
    out = tools.DelegatedOutput(text="EUR/USD is 1.08.", run_subtype="success",
                                final_message="EUR/USD is 1.08.")
    (notice,) = await _continuation(world, monkeypatch, out)
    assert world.alex.sent == []
    assert tools.OWN_CHAT_ANSWER_NOTE.format(name=NAME) not in notice.content.text


async def test_a_quiet_continuation_still_tells_the_requester_the_persons_words(
        world, monkeypatch):
    said = {"cont-1": ["only Lidl"]}
    monkeypatch.setattr(task_input, "take_said", lambda tid: said.pop(tid, []))
    out = tools.DelegatedOutput(text="<silent/>", run_subtype="success",
                                final_message="<silent/>")
    (notice,) = await _continuation(world, monkeypatch, out, quiet_if=lambda o: True)
    assert notice.content.status == "ok"
    assert notice.content.text == WORDS_LINE
    assert said == {}


async def test_a_quiet_continuation_without_words_stays_quiet(world, monkeypatch):
    monkeypatch.setattr(task_input, "take_said", lambda tid: [])
    out = tools.DelegatedOutput(text="<silent/>", run_subtype="success",
                                final_message="<silent/>")
    assert await _continuation(world, monkeypatch, out, quiet_if=lambda o: True) == []


# ---------------------------------------------------------------------------
# The delegate knows when its closing message is posted to the person
# ---------------------------------------------------------------------------

async def test_an_own_chat_task_tells_its_delegate_its_closing_message_is_shown(world):
    _FakeSpecialistClient.reset(response="€420.")
    await _delegate("sync")
    assert "answer_shown:" in Client.built.writes[0]
    assert tools._ANSWER_IN_CHAT == set()


async def test_a_delegation_whose_answer_is_not_posted_is_not_told_so(world):
    world.chats.state["finance"] = "not_open"
    _FakeSpecialistClient.reset(response="€420.")
    await _delegate("sync")
    assert "answer_shown:" not in Client.built.writes[0]


async def test_a_run_under_another_id_inside_the_task_is_not_told_so(world):
    """A hand-off or a nested run of the task runs under its own id: its
    answer returns to its caller, never to the person's chat."""
    nested = {}

    async def nested_run(client):
        if nested:
            return
        nested["outer"] = client.writes[0]
        token = tools._delegation_quota_key.set("handoff-under-the-task")
        try:
            await tools._run_delegated_agent(tools._agent_role_map["finance"], "sub", "")
        finally:
            tools._delegation_quota_key.reset(token)
        nested["inner"] = Client.built.writes[0]

    Client.on_query = nested_run
    _FakeSpecialistClient.reset(response="€420.")
    await _delegate("sync")
    assert "answer_shown:" in nested["outer"]
    assert "answer_shown:" not in nested["inner"]
