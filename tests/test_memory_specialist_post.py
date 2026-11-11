"""A specialist that keeps no memory posted to the operator's chat: memory
keeps one body-free Casa line that it did (INV-MEM-040).

Fido writes no memory (``memory.token_budget`` 0, INV-MEM-032): his answer,
Casa's notice carrying it and the echo lines in the resident's Casa notes are
all kept out of the store. Without a line of its own, the writer saw a request
handed to him and then nothing, and kept the errand open. So when a message
Casa sends on such a specialist's behalf lands in the operator's chat, one
event is captured: "<name> posted to your chat.", Casa's words, with the time;
"<name> posted his answer to your chat." when the path that sent it is the one
that carries his answer (a task's answer), never by its text.
Never the post's body, caption, file name or buttons. Every page of one post,
and a re-landing of it, is one event.

The sends are the channel's real ones over a fake bot, against a real store.
"""
from __future__ import annotations

import asyncio
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import result_broker as rb
import tools
from channels import DeliveryOutcome
from channels.tg_address import TgAddress
from test_memory_capture import captured_events
from test_memory_capture import mem  # noqa: F401 — fixture
from test_no_retain_specialist import _cfg
from two_endpoints import CASA_BOT as BOT

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
    ch = TelegramChannel(bot=MagicMock(), chat_id=chat_id, known_bot_id=BOT,
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
    post = _post(plugin="", slot="task-answer", tool_use_id="t-7", owner="t-7",
                 kind=rb.TASK_ANSWER)
    for _ in range(2):
        await ch.send_response(casa_text(_message("hello")),
                               {"chat_id": OPERATOR_CHAT, "_post": post})
    rows = await captured_events(mem)
    assert [e["text"] for e in rows] == ["Fido posted his answer to your chat."]
    key = occurrence.post_key("telegram", OPERATOR_CHAT, "t-7", "t-7", "task-answer")
    assert rows[0]["source_key"] == key
    # the same identity under another kind is the same key: still one row
    await ch.deliver_operator_message(
        OPERATOR_CHAT, _message("same identity"),
        post=_post(plugin="", slot="task-answer", tool_use_id="t-7", owner="t-7"))
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


# --- the operator's messages to such a specialist's job (INV-MEM-044) ---------
# The real channel, wired to a real in_casa driver whose client answers with
# a canary, against a real store.

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


def _done():
    from claude_agent_sdk import ResultMessage
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=False, num_turns=1, session_id="sid")


@pytest.fixture
async def topic(tmp_path, roles, mem, monkeypatch):
    """Fido's job (and one of finance's, and one of an executor's) on a real
    channel and a real in_casa driver, each with a live scripted session."""
    from channels.telegram import TelegramChannel
    from drivers.in_casa_driver import InCasaDriver
    from engagement_registry import EngagementRegistry
    from test_in_casa_inbound_admission import _mk_assistant, _ScriptedClient
    try:
        from tests.turn_scripted_session import attach_session
    except ImportError:  # pragma: no cover — direct-path collection
        from turn_scripted_session import attach_session

    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    # the running Casa's registry: the task-post index finds a job's posts there
    import tools
    monkeypatch.setattr(tools, "_engagement_registry", reg)
    origin = {"role": "assistant", "channel": "telegram",
              "user_id": OPERATOR_CHAT, "_tg_bot": BOT}
    recs = {
        "browser": await reg.create("specialist", "browser", "in_casa", "t",
                                    dict(origin)),
        "finance": await reg.create("specialist", "finance", "in_casa", "t",
                                    dict(origin)),
        "executor": await reg.create("executor", "configurator", "in_casa",
                                     "t", dict(origin)),
    }
    drv = InCasaDriver(record_lookup=reg.get,
                       begin_turn_delivery=reg.begin_turn_delivery)
    clients = {}
    for name, rec in recs.items():
        client = _ScriptedClient(scripts=[
            [_mk_assistant(f"Saturday 9-17 {CANARY}"), _done()]
            for _ in range(4)])
        clients[name] = client
        attach_session(drv, rec, client)

    def channel(chat_id=str(OPERATOR_CHAT)):
        ch = TelegramChannel(bot=MagicMock(), chat_id=chat_id, known_bot_id=BOT,
                             engagement_supergroup_id=SUPERGROUP,
                             default_agent="assistant")
        ch._engagement_registry = reg
        ch._engagement_driver = drv
        ch._driver_advance_high_water = AsyncMock()
        return ch

    yield SimpleNamespace(recs=recs, clients=clients, channel=channel, drv=drv)
    for rec in recs.values():
        await drv.cancel(rec)


async def _settled(drv, recs) -> None:
    """Every accepted message was written, taken up and its turn ended."""
    try:
        from tests.test_job_session_driver import until
    except ImportError:  # pragma: no cover — direct-path collection
        from test_job_session_driver import until
    await until(lambda: all(drv.inbound_unread_depth(r.id) == 0
                            and not drv.turn_open(r.id) for r in recs))


async def _say(ch, update):
    await ch.handle_update(update)
    await _settled(ch._engagement_driver,
                   list(ch._engagement_registry._records.values()))


async def test_a_message_in_the_log_group_reaches_no_job_and_no_memory(
        topic, mem):
    """The log group is read-only: whatever is written there — by the
    operator or another member, a command, in a job's old topic or in
    General — reaches no job and no memory. Each person gets one pointer to
    the direct chat."""
    ch = topic.channel()
    ch.bot.send_message = AsyncMock()
    await _say(ch, _topic_update("Saturday please, and the link"))
    await _say(ch, _topic_update("/help", message_id=902))
    await _say(ch, _topic_update("adjust it", message_id=903,
                                 thread_id=TOPIC + 2))
    await _say(ch, _topic_update("me too", user_id=7, message_id=904))
    assert all(c.query_prompts == [] for c in topic.clients.values())
    assert await captured_events(mem) == []
    sends = ch.bot.send_message.await_args_list
    assert [c.kwargs["text"] for c in sends] == [
        "This group is a read-only log. Talk to me in our direct chat."] * 2


async def test_the_operator_s_reply_to_a_no_memory_specialist_s_job_post_is_their_message(
        topic, mem, monkeypatch):
    """A reply in their direct chat to a live job's post reaches that job as a
    turn; their typed words — not the note Casa puts before them for the job —
    are captured once as their message, keyed by that DM message under the
    engagement, and never the specialist's reply."""
    import background_jobs as jobs
    from memory import occurrence
    rec = topic.recs["browser"]
    rec.origin["job"] = jobs.initial_job_state(jobs.JobDecl(
        "sample:work", "sample", "work", "sample:work", "Book slots", None), started_by="operator")
    rec.origin["chat_id"] = str(OPERATOR_CHAT)
    ch = topic.channel()
    ch._engagement_registry.record_job_post(rec.id, TgAddress(BOT, OPERATOR_CHAT, 600))
    quoted = SimpleNamespace(message_id=600, text="📊 Fido: which day?",
                             caption=None)
    update = SimpleNamespace(
        message=SimpleNamespace(text="Saturday please", message_id=910,
                                reply_to_message=quoted),
        effective_user=SimpleNamespace(id=OPERATOR_CHAT, first_name="Nicola"))
    await ch._handle_serialized(update, str(OPERATOR_CHAT))
    await _settled(topic.drv, topic.recs.values())
    assert topic.clients["browser"].query_prompts == [
        "[replying to «📊 Fido: which day?»]\nSaturday please"]
    rows = await captured_events(mem)
    assert [(e["speaker_kind"], e["origin_kind"], e["role"], e["text"])
            for e in rows] == [
        ("operator", "operator", "assistant", "Saturday please")]
    [mine] = rows
    assert mine["source_key"] == occurrence.engagement_key(
        "telegram", BOT, rec.id, "910")
    assert not any(CANARY in str(v) for e in rows for v in e.values())
