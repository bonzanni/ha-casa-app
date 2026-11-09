"""A specialist that keeps no memory posted to the operator's chat: memory
keeps one body-free Casa line that it did (INV-MEM-040).

Fido writes no memory (``memory.token_budget`` 0, INV-MEM-032): his answer,
Casa's notice carrying it and the echo lines in the resident's Casa notes are
all kept out of the store. Without a line of its own, the writer saw a request
handed to him and then nothing, and kept the errand open. So when a message
Casa sends on such a specialist's behalf lands in the operator's chat, one
event is captured: "<name> posted to your chat.", Casa's words, with the time;
"<name> posted his answer to your chat." when the path that sent it is the one
that carries his answer (a relayed answer, a desk reply), never by its text.
Never the post's body, caption, file name or buttons. Every page of one post,
and a re-landing of it, is one event.

The sends are the channel's real ones over a fake bot, against a real store.
"""
from __future__ import annotations

import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import result_broker as rb
import tools
from channels import DeliveryOutcome
from test_memory_capture import captured_events
from test_memory_capture import mem  # noqa: F401 — fixture
from test_no_retain_specialist import _cfg

pytestmark = [pytest.mark.asyncio]

OPERATOR_CHAT = 42
CANARY = secrets.token_hex(8)


@pytest.fixture
def roles(monkeypatch):
    m = {"browser": _cfg("browser", "specialist", 0),
         "finance": _cfg("finance", "specialist", 4000),
         "assistant": _cfg("assistant", "resident", 5000)}
    m["browser"].character.name = "Fido"
    monkeypatch.setattr(tools, "_agent_role_map", m, raising=False)
    return m


def _channel(chat_id: str = str(OPERATOR_CHAT)):
    from channels.telegram import TelegramChannel
    ch = TelegramChannel(bot=MagicMock(), chat_id=chat_id,
                         default_agent="assistant")
    ch._app = MagicMock()
    ids = iter(range(100, 200))

    async def _send(**kw):
        return SimpleNamespace(message_id=next(ids))
    ch._app.bot.send_message = AsyncMock(side_effect=_send)
    return ch


def _post(role: str = "browser", **over) -> rb.PostRecord:
    base = dict(role=role, operator_id=OPERATOR_CHAT, plugin="browser",
                slot="answer", tool_use_id="call-1", owner="d-1",
                posted_at=1000.0, kind=rb.OPERATOR_MESSAGE)
    base.update(over)
    return rb.PostRecord(**base)


def _message(body: str) -> str:
    return f"📊 Fido\n{body}"


async def test_a_no_memory_specialist_s_post_leaves_one_casa_line(mem, roles):
    ch = _channel()
    out = await ch.deliver_operator_message(
        OPERATOR_CHAT, _message(f"The pool opens at 7 {CANARY}"), post=_post())
    assert out is DeliveryOutcome.DELIVERED
    rows = await captured_events(mem)
    assert [(e["speaker_kind"], e["origin_kind"], e["role"], e["text"])
            for e in rows] == [
        ("casa", "system", "assistant", "Fido posted to your chat.")]
    [row] = rows
    assert row["channel"] == "telegram" and row["origin_tier"] == "private"
    assert row["speaker_id"] == "browser"
    assert row["source_key"].startswith("post:")
    assert not any(CANARY in str(v) for v in row.values())


async def test_every_page_of_one_post_and_a_re_landing_are_one_event(
        mem, roles):
    ch = _channel()
    post = _post()
    for page in ("one", "two", "three"):
        await ch.deliver_operator_page(OPERATOR_CHAT, _message(page),
                                       post=post)
    # The same post landing again (a re-delivery of it) adds nothing.
    await ch.deliver_operator_message(OPERATOR_CHAT, _message("again"),
                                      post=_post())
    # A second post is a second line.
    await ch.deliver_operator_message(OPERATOR_CHAT, _message("next"),
                                      post=_post(tool_use_id="call-2"))
    rows = await captured_events(mem)
    assert [e["text"] for e in rows] == ["Fido posted to your chat."] * 2


async def test_a_desk_reply_and_a_relayed_answer_leave_a_line_too(
        mem, roles):
    """Both are the message the specialist's turn closed with, his answer
    (v0.347.6): the line says so. Which path sent the post decides, never its
    text."""
    ch = _channel()
    for post in (_post(plugin="", slot="desk", tool_use_id="t-1",
                       owner="t-1", kind=rb.DESK_REPLY),
                 _post(plugin="", slot="relay", tool_use_id="d-9",
                       owner="relay:d-9", kind=rb.RELAYED_ANSWER)):
        from output_boundary import casa_text
        out = await ch.send_response(casa_text(_message(f"hi {CANARY}")),
                                     {"chat_id": OPERATOR_CHAT, "_post": post})
        assert out is DeliveryOutcome.DELIVERED
    rows = await captured_events(mem)
    assert [e["text"] for e in rows] == ["Fido posted his answer to your chat."] * 2
    assert not any(CANARY in e["text"] for e in rows)


async def test_a_relayed_answer_through_relay_answer_is_his_answer_once(
        mem, roles, monkeypatch):
    """The real ``relay_answer`` over the real channel: an answer of two pages
    is one event, worded as his answer, keyed by the post's identity alone."""
    from collections import OrderedDict

    import agent as agent_mod
    from memory import occurrence
    from output_boundary_testing import with_scope
    monkeypatch.setattr(tools, "_RELAYABLE", OrderedDict())
    monkeypatch.setattr(rb, "POST_MAP", rb.PostMap())
    ch = _channel()

    class _Manager:
        def get(self, name):
            return ch if name == "telegram" else None
    monkeypatch.setattr(tools, "_channel_manager", _Manager(), raising=False)
    origin = {"role": "assistant", "channel": "telegram", "chat_id": OPERATOR_CHAT,
              "user_id": OPERATOR_CHAT, "cid": "c1"}
    did = "d-relay-1"
    answer = f"The pool opens at 7 {CANARY}. " + "More detail follows. " * 400
    assert tools._hold_answer(did, "browser", origin, answer)
    tok = agent_mod.origin_var.set(with_scope(origin))
    try:
        out = await tools.relay_answer.handler({"delegation_id": did})
    finally:
        agent_mod.origin_var.reset(tok)
    assert not out.get("is_error")
    assert ch._app.bot.send_message.await_count >= 2          # two pages landed
    rows = await captured_events(mem)
    assert [e["text"] for e in rows] == ["Fido posted his answer to your chat."]
    [rec] = rb.POST_MAP.owned(f"relay:{did}")[:1]
    assert rows[0]["source_key"] == occurrence.post_key(
        "telegram", OPERATOR_CHAT, rec.owner, rec.tool_use_id, rec.slot)
    assert not any(CANARY in str(v) for v in rows[0].values())


@pytest.mark.parametrize("over", [
    dict(slot="beat", kind=rb.OPERATOR_MESSAGE),          # a progress note
    dict(slot="sources", kind=rb.OPERATOR_MESSAGE),       # Fido's answer with sources
    dict(slot="notice", kind=rb.OPERATOR_MESSAGE),
    dict(slot="file", kind=rb.OPERATOR_FILE),
    dict(slot="live", kind=rb.OPERATOR_LINK),
    dict(slot="card", kind=rb.OPERATOR_PROPOSAL),
    dict(plugin="browser", slot="receipt", owner="run-1", tool_use_id="run-1",
         kind=rb.RECEIPT),
], ids=["beat", "sources", "notice", "file", "link", "card", "receipt"])
async def test_every_other_post_keeps_the_neutral_line(mem, roles, over):
    """A plugin's message, file, link or card and a stored call's receipt are
    sent the same way mid-run and at the end: nothing mechanical says which is
    the answer, so the line stays neutral — whatever the post says (the body
    below calls itself the final answer, and changes nothing)."""
    from output_boundary import casa_text
    ch = _channel()
    post = _post(**over)
    body = _message(f"Here is my final answer {CANARY}")
    if over["kind"] == rb.RECEIPT:
        await ch.send_response(casa_text(body),
                               {"chat_id": OPERATOR_CHAT, "_post": post})
    else:
        await ch.deliver_operator_message(OPERATOR_CHAT, body, post=post)
    rows = await captured_events(mem)
    assert [e["text"] for e in rows] == ["Fido posted to your chat."]


async def test_the_key_and_one_row_per_post_do_not_depend_on_the_wording(
        mem, roles):
    """The key is the post's identity (owner, tool call, slot), as before: an
    answer's pages and a re-landing are one event, and the same identity under
    another kind adds nothing."""
    from memory import occurrence
    from output_boundary import casa_text
    ch = _channel()
    post = _post(plugin="", slot="desk", tool_use_id="t-7", owner="t-7",
                 kind=rb.DESK_REPLY)
    for _ in range(2):
        await ch.send_response(casa_text(_message("hello")),
                               {"chat_id": OPERATOR_CHAT, "_post": post})
    rows = await captured_events(mem)
    assert [e["text"] for e in rows] == ["Fido posted his answer to your chat."]
    key = occurrence.post_key("telegram", OPERATOR_CHAT, "t-7", "t-7", "desk")
    assert rows[0]["source_key"] == key
    # the same identity under another kind is the same key: still one row
    await ch.deliver_operator_message(
        OPERATOR_CHAT, _message("same identity"),
        post=_post(plugin="", slot="desk", tool_use_id="t-7", owner="t-7"))
    rows = await captured_events(mem)
    assert [e["source_key"] for e in rows] == [key]


async def test_nothing_for_a_specialist_that_keeps_memory_or_another_chat(
        mem, roles):
    ch = _channel()
    await ch.deliver_operator_message(
        OPERATOR_CHAT, "📊 Finance\nsaved", post=_post(role="finance"))
    await ch.deliver_operator_message(7, _message("elsewhere"),
                                      post=_post(tool_use_id="call-3"))
    await ch.deliver_operator_message(OPERATOR_CHAT, "a plain reply")
    unconfigured = _channel(chat_id="")
    await unconfigured.deliver_operator_message(
        OPERATOR_CHAT, _message("open mode"), post=_post(tool_use_id="c-4"))
    assert await captured_events(mem) == []


async def test_a_role_no_longer_loaded_counts_as_keeping_no_memory(
        mem, roles):
    ch = _channel()
    await ch.deliver_operator_message(
        OPERATOR_CHAT, "📊 gone\nhi", post=_post(role="gone"))
    rows = await captured_events(mem)
    assert [e["text"] for e in rows] == ["gone posted to your chat."]


async def test_a_capture_that_fails_never_fails_the_send(
        mem, roles, monkeypatch):
    from memory import capture

    def boom(**kw):
        raise RuntimeError("no")

    monkeypatch.setattr(capture, "event", boom)
    ch = _channel()
    out = await ch.deliver_operator_message(
        OPERATOR_CHAT, _message("still sent"), post=_post())
    assert out is DeliveryOutcome.DELIVERED
    assert await captured_events(mem) == []


async def test_a_link_posted_as_a_button_leaves_a_line(mem, roles):
    ch = _channel()
    url = f"https://live.example/{CANARY}"
    post = _post(slot="live_view", kind=rb.OPERATOR_LINK)
    out = await ch.deliver_operator_link(
        OPERATOR_CHAT, "📊 Fido\nlive view", [], f"📊 Fido\n{url}", post=post,
        button=("📊 Fido\nlive view", "Open (live.example)", url))
    assert out is DeliveryOutcome.DELIVERED
    rows = await captured_events(mem)
    assert [e["text"] for e in rows] == ["Fido posted to your chat."]
    assert not any(CANARY in str(v) for e in rows for v in e.values())


# --- the desk's own reply, through the real handle_reply ----------------------
# (the real bounded runner and ``_run_delegated_agent`` under a scripted client,
# ``test_pending_approval_desk``'s harness)

from test_desk_turn import _reply, env  # noqa: E402,F401 — fixture
from test_pending_approval_desk import (  # noqa: E402,F401 — fixtures
    _Scripted, _ask, _call, _result, _text, desk,
)


def _desk_post(env) -> rb.PostRecord:
    [(_message, context)] = env.channel.replies
    return context["_post"]


async def test_a_desk_reply_that_closes_the_turn_is_his_answer(desk):
    from delegated_memory import post_line
    _Scripted.load(_text("The March total is 42."))
    await _reply(desk, text="what is the March total?")
    post = _desk_post(desk)
    assert post.kind == rb.DESK_REPLY
    assert post_line("Fido", post.kind) == "Fido posted his answer to your chat."


async def test_a_desk_reply_before_a_pending_approval_is_not_his_answer(desk):
    """Round 1, Astra S1: a desk turn that ends with an approval pending posts
    the words written before that call (#1252) — work still waiting on the
    operator, not the answer: the neutral line."""
    import asyncio

    from delegated_memory import post_line
    _Scripted.load(_text("I will check that after you approve."), _call("deny-1"),
                   _ask(desk, "deny-1"), _result("deny-1"), _text("after the call"))
    task = asyncio.create_task(_reply(desk, text="what is the March total?"))
    await asyncio.wait_for(desk.hook_returned.wait(), 10)
    desk.release_result.set()
    await asyncio.wait_for(task, 10)
    post = _desk_post(desk)
    assert post.kind not in rb.ANSWER_KINDS
    assert post_line("Fido", post.kind) == "Fido posted to your chat."


# --- the operator's swipe-reply on such a desk (INV-MEM-043) -----------------
# The real handle_reply over the real channel and a fake bot, against a real
# store; the desk's runner answers with a canary, the quoted post carries one.

QUOTED = secrets.token_hex(8)


@pytest.fixture
def fido_desk(env, mem, monkeypatch):
    from test_desk_turn import OPERATOR as DESK_OPERATOR
    assert DESK_OPERATOR == OPERATOR_CHAT
    finance = tools._agent_role_map["finance"]
    finance.memory = SimpleNamespace(token_budget=0)

    async def respond(call):
        return tools.DelegatedOutput(text=f"Saturday 9-17 {CANARY}")
    env.respond = respond
    env.channel = _channel()
    return env


def _desk_kw(**over):
    kw = dict(quoted_text=f"📊 Finance\nweekday hours {QUOTED}", message_id=70)
    kw.update(over)
    return kw


async def test_a_swipe_reply_on_a_no_memory_specialist_s_desk_is_his_message(
        fido_desk, mem):
    from memory import occurrence
    await _reply(fido_desk, text="Saturday please, and the link", **_desk_kw())
    [call] = fido_desk.calls
    assert call.task == "Saturday please, and the link"   # the desk turn ran
    rows = await captured_events(mem)
    assert [(e["speaker_kind"], e["origin_kind"], e["role"], e["text"])
            for e in rows] == [
        ("operator", "operator", "assistant", "Saturday please, and the link"),
        ("casa", "system", "assistant", "Finance posted his answer to your chat.")]
    mine = rows[0]
    assert mine["eligibility"] == "desk_reply" and mine["origin_tier"] is None
    assert mine["speaker_id"] == str(OPERATOR_CHAT)
    assert mine["speaker_name"] == "Nicola"
    assert mine["source_key"] == occurrence.provider_key(
        "telegram", str(OPERATOR_CHAT), "70")
    # never the post he quoted, never the specialist's reply
    assert not any(tok in str(v) for e in rows for v in e.values()
                   for tok in (CANARY, QUOTED))


async def test_no_desk_reply_event_for_a_memory_specialist_a_composed_turn_or_another_chat(
        fido_desk, mem):
    # a specialist that keeps memory: its own exchange capture applies, not this
    tools._agent_role_map["finance"].memory = SimpleNamespace(token_budget=4000)
    await _reply(fido_desk, text="one", **_desk_kw(message_id=71))
    tools._agent_role_map["finance"].memory = SimpleNamespace(token_budget=0)
    # Casa composed the task: an approval's continuation, a file's hand-off
    await _reply(fido_desk, text="two", continuation=True, **_desk_kw(message_id=None))
    await _reply(fido_desk, text="three", file_name="a.pdf", file_path="/nowhere/a.pdf",
                 **_desk_kw(message_id=72))
    # a channel whose operator chat is another, or none at all
    for chat in ("7", ""):
        fido_desk.channel = _channel(chat_id=chat)
        await _reply(fido_desk, text="four", **_desk_kw(message_id=73))
    rows = await captured_events(mem)
    assert not [e for e in rows if e["speaker_kind"] == "operator"]


async def test_a_desk_reply_capture_that_fails_never_fails_the_desk_turn(
        fido_desk, mem, monkeypatch):
    from memory import capture

    def boom(**kw):
        raise RuntimeError("no")

    monkeypatch.setattr(capture, "event", boom)
    await _reply(fido_desk, text="Saturday please", **_desk_kw())
    assert len(fido_desk.calls) == 1
    assert fido_desk.channel._app.bot.send_message.await_count == 1
    assert await captured_events(mem) == []


# --- the operator's messages in such a specialist's topic (INV-MEM-044) -------
# The real handle_update over a real channel, wired to a real in_casa driver
# whose client answers with a canary, against a real store.

SUPERGROUP = -1001
TOPIC = 555


def _topic_update(text: str, *, user_id: int = OPERATOR_CHAT, message_id=900,
                  thread_id: int = TOPIC):
    u = MagicMock()
    u.message.chat.id = SUPERGROUP
    u.message.text = text
    u.message.message_thread_id = thread_id
    u.message.from_user = SimpleNamespace(id=user_id, first_name="Nicola",
                                          is_bot=False)
    u.message.message_id = message_id
    return u


class _TopicStream:
    def __init__(self):
        self.finals: list[str] = []

    async def emit(self, text):
        pass

    async def finalize(self, text):
        self.finals.append(text)
        return DeliveryOutcome.DELIVERED


@pytest.fixture
async def topic(tmp_path, roles, mem):
    """A topic of Fido's engagement (and one of finance's, and one of an
    executor's) on a real channel and a real in_casa driver."""
    import asyncio

    from channels.telegram import TelegramChannel
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRegistry
    from test_in_casa_inbound_admission import _mk_assistant, _ScriptedClient

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    origin = {"role": "assistant", "channel": "telegram",
              "user_id": OPERATOR_CHAT}
    recs = {
        "browser": await reg.create("specialist", "browser", "in_casa", "t",
                                    dict(origin), topic_id=TOPIC),
        "finance": await reg.create("specialist", "finance", "in_casa", "t",
                                    dict(origin), topic_id=TOPIC + 1),
        "executor": await reg.create("executor", "configurator", "in_casa",
                                     "t", dict(origin), topic_id=TOPIC + 2),
    }
    stream = _TopicStream()
    drv = InCasaDriver(topic_stream_factory=lambda tid: stream)
    clients = {}
    for name, rec in recs.items():
        client = _ScriptedClient(scripts=[
            [_mk_assistant(f"Saturday 9-17 {CANARY}")] for _ in range(4)])
        clients[name] = client
        drv._clients[rec.id] = client
        drv._ctx_stack[rec.id] = client
        drv._locks[rec.id] = asyncio.Lock()

    def channel(chat_id=str(OPERATOR_CHAT)):
        ch = TelegramChannel(bot=MagicMock(), chat_id=chat_id,
                             engagement_supergroup_id=SUPERGROUP,
                             default_agent="assistant")
        ch._engagement_registry = reg
        ch._engagement_driver = drv
        ch._driver_advance_high_water = AsyncMock()
        ch._post_engagement_notice = AsyncMock()
        ch._driver_admit_inbound = lambda r, t: drv.admit_inbound(r.id, t)
        ch._driver_discharge_inbound = (
            lambda r, tok: drv.discharge_inbound(r.id, tok))
        ch._driver_inbound_held = (
            lambda r, tok: drv.inbound_token_held(r.id, tok))

        async def _send_user_turn(r, t, *, tg_message_id=None,
                                  inbound_token=None, batch=None):
            await drv.send_user_turn(r, t, tg_message_id=tg_message_id,
                                     inbound_token=inbound_token)
        ch._driver_send_user_turn = _send_user_turn
        return ch

    return SimpleNamespace(recs=recs, clients=clients, stream=stream,
                           channel=channel)


async def _say(ch, update):
    from test_in_casa_inbound_admission import _drain
    await ch.handle_update(update)
    await _drain(ch)


async def test_the_operator_s_message_in_a_no_memory_specialist_s_topic_is_his_message(
        topic, mem):
    from memory import occurrence
    ch = topic.channel()
    await _say(ch, _topic_update("Saturday please, and the link"))
    # Fido's turn ran and streamed his reply, canary and all
    assert topic.clients["browser"].query_prompts == [
        "Saturday please, and the link"]
    assert any(CANARY in f for f in topic.stream.finals)
    rows = await captured_events(mem)
    assert [(e["speaker_kind"], e["origin_kind"], e["role"], e["text"])
            for e in rows] == [
        ("operator", "operator", "assistant", "Saturday please, and the link")]
    [mine] = rows
    assert mine["eligibility"] == "topic_message" and mine["origin_tier"] is None
    assert mine["channel"] == "telegram"
    assert mine["speaker_id"] == str(OPERATOR_CHAT)
    assert mine["speaker_name"] == "Nicola"
    assert mine["provider_message_id"] == "900"
    assert mine["source_key"] == occurrence.engagement_key(
        "telegram", topic.recs["browser"].id, "900")
    # never the specialist's reply
    assert not any(CANARY in str(v) for e in rows for v in e.values())


async def test_one_topic_message_is_one_event_whichever_seam_captures_it(
        topic, mem):
    """The same Telegram message delivered twice is one event; a specialist
    that keeps memory captures it in its own turn under the same key, and
    this seam adds nothing beside it."""
    from memory import occurrence
    ch = topic.channel()
    await _say(ch, _topic_update("Saturday please"))
    await _say(ch, _topic_update("Saturday please"))
    await _say(ch, _topic_update("book it", message_id=901,
                                 thread_id=TOPIC + 1))
    assert topic.clients["browser"].query_prompts == ["Saturday please"] * 2
    rows = await captured_events(mem)
    ops = [e for e in rows if e["speaker_kind"] == "operator"]
    assert [(e["eligibility"], e["role"], e["text"]) for e in ops] == [
        ("topic_message", "assistant", "Saturday please"),
        ("engagement_turn", "finance", "book it")]
    assert ops[1]["source_key"] == occurrence.engagement_key(
        "telegram", topic.recs["finance"].id, "901")


async def test_no_topic_event_for_another_sender_a_command_an_executor_or_no_operator(
        topic, mem):
    ch = topic.channel()
    # a command Casa consumes itself
    await _say(ch, _topic_update("/help", message_id=902))
    # an executor's topic: not a specialist's engagement
    await _say(ch, _topic_update("adjust it", message_id=903,
                                 thread_id=TOPIC + 2))
    assert topic.clients["executor"].query_prompts == ["adjust it"]
    # another member of the supergroup
    await _say(ch, _topic_update("me too", user_id=7, message_id=904))
    # a channel with no configured operator
    await _say(topic.channel(chat_id=""),
               _topic_update("and me", message_id=905))
    # each reached Fido as a turn; only the command did not
    assert topic.clients["browser"].query_prompts == ["me too", "and me"]
    rows = await captured_events(mem)
    assert rows == []


async def test_a_topic_capture_that_fails_never_fails_the_turn(
        topic, mem, monkeypatch):
    from memory import capture

    def boom(**kw):
        raise RuntimeError("no")

    monkeypatch.setattr(capture, "event", boom)
    ch = topic.channel()
    await _say(ch, _topic_update("Saturday please"))
    assert topic.clients["browser"].query_prompts == ["Saturday please"]
    assert any(CANARY in f for f in topic.stream.finals)
    assert await captured_events(mem) == []


async def test_the_operator_s_reply_to_a_no_memory_specialist_s_job_post_is_their_message(
        topic, mem, monkeypatch):
    """A reply in their direct chat to a live job's post reaches that job as a
    turn; their typed words — not the note Casa puts before them for the job —
    are captured once as their message, keyed by that DM message under the
    engagement, and never the specialist's reply."""
    import background_jobs as jobs
    from memory import occurrence
    monkeypatch.setattr(jobs, "job_after_turn", AsyncMock())
    rec = topic.recs["browser"]
    rec.origin["job"] = jobs.initial_job_state(jobs.JobDecl(
        "sample:work", "sample", "work", "sample:work", "Book slots", None,
        None, 10), started_by="operator")
    rec.origin["chat_id"] = str(OPERATOR_CHAT)
    ch = topic.channel()
    ch._engagement_registry.record_job_post(rec.id, 600)
    quoted = SimpleNamespace(message_id=600, text="📊 Fido: which day?",
                             caption=None)
    update = SimpleNamespace(
        message=SimpleNamespace(text="Saturday please", message_id=910,
                                reply_to_message=quoted),
        effective_user=SimpleNamespace(id=OPERATOR_CHAT, first_name="Nicola"))
    from test_in_casa_inbound_admission import _drain
    await ch._handle_serialized(update, str(OPERATOR_CHAT))
    await _drain(ch)
    assert topic.clients["browser"].query_prompts == [
        "[replying to «📊 Fido: which day?»]\nSaturday please"]
    rows = await captured_events(mem)
    assert [(e["speaker_kind"], e["origin_kind"], e["role"], e["text"])
            for e in rows] == [
        ("operator", "operator", "assistant", "Saturday please")]
    [mine] = rows
    assert mine["source_key"] == occurrence.engagement_key(
        "telegram", rec.id, "910")
    assert not any(CANARY in str(v) for e in rows for v in e.values())
