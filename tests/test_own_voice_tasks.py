"""Tasks of an agent with its own chat (the task model).

A delegation or a job is a task: its launcher decides, once, where its posts go
(its *delivery*) and who receives its result (its *requester*). Here Ellen
(``assistant``, Casa's bot 111) delegates to Alex (``finance``, his own bot
222); both serve operator 100. Pinned:

* a task of Alex's open chat posts its start line and Alex's whole answer —
  the raw text, never Ellen's bounded copy — in Alex's chat, every page under
  the task; Ellen gets her bounded result; Alex's conversation gets a note;
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

    def submit(self, tid, text):
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
                        lambda text, owner: real_echo(text, owner) + ECHO)
    yield SimpleNamespace(alex=alex, casa=casa, chats=chats, inputs=inputs, opened=opened,
                          notified=notified, reg=reg, te=te)
    agent_lane.reset()
    chat_notes.reset()


def _ellen_turn():
    return {"role": "assistant", "execution_role": "assistant", "channel": "telegram",
            "chat_id": OPERATOR, "user_id": OPERATOR, "cid": "c-ellen", "_tg_bot": CASA_BOT,
            "user_text": "groceries in october?"}


async def _delegate(mode="sync", task="How much on groceries in October?"):
    token = agent_mod.origin_var.set(_ellen_turn())
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
    assert texts[0].startswith("Ellen asked: «How much on groceries in October?»")
    answer = "".join(texts[1:])
    assert MARKER in answer and ECHO.strip() not in answer
    assert len(texts) >= 3                       # paged
    assert [p.owner for _, _, p in world.alex.filed] == [did] * len(texts)
    assert {c for c, _, _ in world.alex.filed} == {OPERATOR}
    assert world.opened == [(did, ALEX_CHAT)]
    assert _casa_sends(world) == []
    # the conversation learns the outcome on its next turn, once
    (line,) = chat_notes.drain(FINANCE_BOT, OPERATOR)
    assert line.startswith("Ellen asked you «How much on groceries in October?»; you answered (")
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
    tools._note_task(tools._record_birth(rec), "started your job", "it completed")
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
