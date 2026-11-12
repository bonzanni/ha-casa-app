"""Own-chat routing on every endpoint — the resident's bot and an agent's own
bot alike — and the intake of an agent's own bot.

Task first: a reply or a file to a post of a live task (a job or a
delegation) goes into that task, ahead of everything else — whichever of the
task's posts it answers (an intermediate plugin post, its second page), and
while the task is still pending. A reply to an ended task's post is this
chat's agent's turn with the task's end in its note. Everything else is this
chat's agent's conversation, whoever posted the message it replies to.

A file for a live task is stored in the task's executor's own inbox, granted
to that task, and handed to it as a Casa line naming its path; 📎 records its
destination when tapped, and an album follows its first file.

An agent's own bot offers every update to its chat's lifecycle before it is
queued, hands a message its agent's queue cannot take back to it, and
releases a held update on the normal path.
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import agent_inbox
import chat_notes
import own_voice
import result_broker as rb
import task_input
from bus import BusMessage, MessageBus
from channels.telegram import TelegramChannel, _IntakeQueue
from channels.tg_address import TgAddress
from task_model import Requester
from task_posts import TaskRef

pytestmark = pytest.mark.asyncio

OPERATOR = 100
CASA_BOT = 111
OWN_BOT = 222
ENDPOINTS = ["casa", "own"]


class _Bot:
    def __init__(self):
        self.sent, self.edited = [], []

    async def send_message(self, **kw):
        self.sent.append(kw)
        return types.SimpleNamespace(message_id=900 + len(self.sent))

    async def edit_message_text(self, **kw):
        self.edited.append(kw)
        return True


async def _noop(_msg):
    return None


def _endpoint(kind: str, *, register: bool = True):
    casa = kind == "casa"
    role = "assistant" if casa else "finance"
    bus = MessageBus()
    if register:
        bus.register(role, _noop)
    ch = TelegramChannel(bot_token="T", chat_id=str(OPERATOR), default_agent=role, bus=bus,
                         is_casa=casa, known_bot_id=CASA_BOT if casa else OWN_BOT)
    ch._start_typing = lambda *a, **k: None
    ch._stop_typing = lambda *a, **k: None
    ch._app = types.SimpleNamespace(bot=_Bot())
    ch._session_registry = None
    return ch, bus


def _turns(bus, role):
    q = bus.queues.get(role)
    out = []
    while q is not None and not q.empty():
        out.append(q.get_nowait()[2])
    return out


def _user(uid=OPERATOR):
    return types.SimpleNamespace(id=uid, first_name="Op", is_bot=False)


def _quoted(mid, text="📊 Finance\nPair 17?"):
    return types.SimpleNamespace(message_id=mid, text=text, caption=None, date=None,
                                 from_user=None, sender_chat=None)


def _text(text, *, reply_to=None, quoted_text="📊 Finance\nPair 17?", uid=OPERATOR, mid=70):
    user = _user(uid)
    message = types.SimpleNamespace(
        text=text, message_id=mid, from_user=user, sender_chat=None,
        chat=types.SimpleNamespace(id=OPERATOR),
        reply_to_message=_quoted(reply_to, quoted_text) if reply_to is not None else None)
    return types.SimpleNamespace(
        update_id=5000 + mid, message=message, callback_query=None,
        effective_chat=types.SimpleNamespace(id=str(OPERATOR)), effective_user=user,
        to_dict=lambda: {"update_id": 5000 + mid, "message": {"text": text}})


def _file(name="statement.pdf", *, reply_to=None, album=None, caption=None, mid=80):
    doc = types.SimpleNamespace(file_id=f"fid-{mid}", file_name=name, file_size=1234,
                                mime_type="application/pdf")
    return types.SimpleNamespace(
        message_id=mid, chat=types.SimpleNamespace(id=OPERATOR), from_user=_user(),
        document=doc, photo=None, caption=caption, media_group_id=album,
        reply_to_message=_quoted(reply_to) if reply_to is not None else None,
        message_thread_id=None, sender_chat=None,
        **{k: None for k in ("sticker", "video", "video_note", "voice", "audio", "animation",
                             "contact", "location", "venue", "poll", "dice", "game", "story",
                             "web_app_data", "invoice", "pinned_message", "new_chat_title",
                             "new_chat_photo", "delete_chat_photo",
                             "message_auto_delete_timer_changed", "new_chat_members",
                             "left_chat_member", "write_access_allowed")})


class _Inbox:
    def __init__(self, root: Path, role: str):
        self.ready_dir = str(root / role / "ready")
        self.received = []

    async def receive(self, fetch, *, ext, display_name, declared_size):
        self.received.append(display_name)
        return agent_inbox.Receipt(agent_inbox.Outcome.STORED, f"{len(self.received)}-{display_name}",
                                   1234)


@pytest.fixture
def world(monkeypatch, tmp_path):
    import task_posts_double
    import tools
    index = task_posts_double.install(monkeypatch)
    monkeypatch.setattr(tools, "_display_name_for_role",
                        lambda role: {"finance": "Finance", "assistant": "Ellen"}.get(role, role))
    chat_notes.reset()
    inputs = types.SimpleNamespace(calls=[], said=[], answer="written")

    def submit(task_id, text, *, said=None):
        inputs.calls.append((task_id, text))
        inputs.said.append(said)
        return inputs.answer
    monkeypatch.setattr(task_input, "submit", submit)
    inboxes: dict = {}
    monkeypatch.setattr(agent_inbox, "get_inbox",
                        lambda role: inboxes.setdefault(role, _Inbox(tmp_path, role)))
    grants = []
    monkeypatch.setattr(agent_inbox, "grant_task_file",
                        lambda task_id, path: grants.append((task_id, path)))
    # the own chat's lifecycle, as far as a turn asks it: whether the chat has
    # opened yet (a flag only ``mark_open`` flips), and every mark
    opening = types.SimpleNamespace(unopened=False, moved=False, asked=[], marks=[])

    def chat_opening(bot_id):
        opening.asked.append(bot_id)
        return opening.unopened

    def chat_moved(bot_id):
        if isinstance(opening.moved, Exception):
            raise opening.moved
        return opening.moved

    async def mark_open(bot_id):
        opening.marks.append(bot_id)
        opening.unopened = False
    monkeypatch.setattr(own_voice, "chat_opening", chat_opening, raising=False)
    monkeypatch.setattr(own_voice, "mark_open", mark_open, raising=False)
    monkeypatch.setattr(own_voice, "chat_moved", chat_moved, raising=False)
    yield types.SimpleNamespace(index=index, inputs=inputs, inboxes=inboxes, grants=grants,
                                opening=opening)
    chat_notes.reset()


def _land(ch, mid, owner, role):
    """A plugin's post of the task *owner* landing on this bot, filed as it lands."""
    ch._record_post(OPERATOR, types.SimpleNamespace(message_id=mid),
                    rb.PostRecord(role=role, operator_id=OPERATOR, plugin="probe",
                                  slot="operator_proposal", tool_use_id="tu", owner=owner,
                                  posted_at=1.0, kind="operator_proposal"))


async def _handle(ch, update):
    await ch._handle(update, None)
    for t in list(ch._turn_tasks):
        await t


# ---------------------------------------------------------------------------
# text: a delegation's posts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ENDPOINTS)
async def test_a_reply_to_a_delegations_intermediate_plugin_post_goes_into_it(world, kind):
    ch, bus = _endpoint(kind)
    world.index.delegation("d-1", ch.default_agent)
    _land(ch, 640, "d-1", ch.default_agent)
    await _handle(ch, _text("the second one", reply_to=640))
    assert world.inputs.calls == [("d-1", "[replying to «📊 Finance»]\nthe second one")]
    assert world.inputs.said == ["the second one"]          # the operator's words alone
    assert _turns(bus, ch.default_agent) == []


@pytest.mark.parametrize("kind", ENDPOINTS)
async def test_a_reply_to_a_delegations_second_page_goes_into_it(world, kind):
    ch, bus = _endpoint(kind)
    world.index.delegation("d-1", ch.default_agent)
    _land(ch, 650, "d-1", ch.default_agent)
    _land(ch, 651, "d-1", ch.default_agent)
    await _handle(ch, _text("why that total?", reply_to=651, quoted_text="…page two"))
    assert world.inputs.calls == [("d-1", "[replying to «…page two»]\nwhy that total?")]
    assert _turns(bus, ch.default_agent) == []


@pytest.mark.parametrize("kind", ENDPOINTS)
async def test_a_reply_while_the_delegation_is_pending_is_queued_into_it(world, kind):
    ch, bus = _endpoint(kind)
    world.inputs.answer = "queued"
    world.index.delegation("d-1", ch.default_agent)
    _land(ch, 640, "d-1", ch.default_agent)
    await _handle(ch, _text("wait for me", reply_to=640))
    assert [c[0] for c in world.inputs.calls] == ["d-1"]
    assert _turns(bus, ch.default_agent) == []          # pending is never ended


@pytest.mark.parametrize("kind", ENDPOINTS)
async def test_a_reply_to_an_ended_delegations_post_is_this_chats_agents_turn(world, kind):
    ch, bus = _endpoint(kind)
    world.index.delegation("d-1", ch.default_agent)
    _land(ch, 640, "d-1", ch.default_agent)
    world.index.end("d-1")
    await _handle(ch, _text("and now?", reply_to=640))
    assert world.inputs.calls == []
    [turn] = _turns(bus, ch.default_agent)
    assert turn.content == "and now?"
    assert "a message from your earlier task, which has ended" in turn.context["_reply_note"]


@pytest.mark.parametrize("kind", ENDPOINTS)
async def test_a_reply_the_delegations_input_refuses_as_ended_reaches_this_chats_agent(world, kind):
    ch, bus = _endpoint(kind)
    world.inputs.answer = "ended"
    world.index.delegation("d-1", ch.default_agent)
    _land(ch, 640, "d-1", ch.default_agent)
    await _handle(ch, _text("too late?", reply_to=640))
    [turn] = _turns(bus, ch.default_agent)
    assert "which has ended" in turn.context["_reply_note"]


@pytest.mark.parametrize("kind", ENDPOINTS)
async def test_a_plain_message_is_this_chats_agents_turn(world, kind):
    ch, bus = _endpoint(kind)
    await _handle(ch, _text("hello"))
    [turn] = _turns(bus, ch.default_agent)
    assert turn.target == ch.default_agent and turn.context["_tg_bot"] == ch.bot_id


async def test_a_reply_from_someone_else_never_reaches_the_task(world):
    ch, _bus = _endpoint("own")
    world.index.delegation("d-1", "finance")
    _land(ch, 640, "d-1", "finance")
    await _handle(ch, _text("let me in", reply_to=640, uid=555))
    assert world.inputs.calls == []


# ---------------------------------------------------------------------------
# text: a job's posts, and a post of no task
# ---------------------------------------------------------------------------

def _job(ch, world, executor, mid):
    rec = types.SimpleNamespace(id="e-1", role_or_type=executor, status="running",
                                origin={"user_id": OPERATOR, "chat_id": str(OPERATOR)})
    ch._engagement_registry = types.SimpleNamespace(get=lambda i: rec if i == "e-1" else None)
    world.index.live[TgAddress(ch.bot_id, OPERATOR, mid)] = TaskRef(
        "job", "e-1", executor, Requester("assistant", {}))
    return rec


@pytest.mark.parametrize("kind", ENDPOINTS)
async def test_a_reply_to_a_live_jobs_post_goes_into_the_job(world, kind):
    ch, bus = _endpoint(kind)
    rec = _job(ch, world, ch.default_agent, 501)
    ch.deliver_operator_reply = AsyncMock(return_value=True)
    await _handle(ch, _text("use the second account", reply_to=501))
    ch.deliver_operator_reply.assert_awaited_once()
    args, kw = ch.deliver_operator_reply.await_args
    assert args == (rec, "use the second account") and kw["message_id"] == 70
    assert _turns(bus, ch.default_agent) == []


@pytest.mark.parametrize("kind", ENDPOINTS)
async def test_a_reply_to_a_post_of_no_task_is_this_chats_agents_turn(world, kind):
    ch, bus = _endpoint(kind)
    ch._record_post(OPERATOR, types.SimpleNamespace(message_id=11),
                    rb.PostRecord(role="travel", operator_id=OPERATOR, plugin="p",
                                  slot="s", tool_use_id="t", owner="", posted_at=1.0))
    await _handle(ch, _text("more please", reply_to=11))
    [turn] = _turns(bus, ch.default_agent)
    assert turn.content == "more please" and "_reply_note" in turn.context
    assert _turns(bus, "travel") == [] and world.inputs.calls == []


# ---------------------------------------------------------------------------
# files to a live task (the executor's own inbox, granted to the task)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ENDPOINTS)
async def test_a_file_replying_to_a_live_delegations_post_goes_into_it(world, kind):
    ch, bus = _endpoint(kind)
    world.index.delegation("d-1", ch.default_agent)
    _land(ch, 640, "d-1", ch.default_agent)
    await ch._on_non_text_message(types.SimpleNamespace(message=_file(reply_to=640)))
    inbox = world.inboxes[ch.default_agent]
    path = Path(inbox.ready_dir) / "1-statement.pdf"
    assert world.grants == [("d-1", path)]
    assert world.inputs.calls == [
        ("d-1", f"[casa file] statement.pdf at {path} — sent as a reply to your post")]
    assert world.inputs.said == ["(the file statement.pdf)"]
    assert _turns(bus, ch.default_agent) == [] and ch._app.bot.sent == []


async def test_a_captioned_file_into_a_live_delegation_records_the_caption_as_said(world):
    ch, _bus = _endpoint("own")
    world.index.delegation("d-1", ch.default_agent)
    _land(ch, 640, "d-1", ch.default_agent)
    await ch._on_non_text_message(types.SimpleNamespace(
        message=_file(reply_to=640, caption="only Lidl")))
    assert world.inputs.said == ["only Lidl (with the file statement.pdf)"]


@pytest.mark.parametrize("kind", ENDPOINTS)
async def test_a_file_replying_to_a_live_jobs_post_reaches_the_job_from_its_executors_inbox(
        world, kind):
    """In the resident's chat the job may be another agent's (one started before
    that agent had its own chat): the file is still its executor's."""
    ch, _bus = _endpoint(kind)
    rec = _job(ch, world, "finance", 501)
    ch.deliver_operator_reply = AsyncMock(return_value=True)
    await ch._on_non_text_message(types.SimpleNamespace(
        message=_file(reply_to=501, caption="the March one")))
    assert list(world.inboxes) == ["finance"]               # never the resident's inbox
    path = Path(world.inboxes["finance"].ready_dir) / "1-statement.pdf"
    assert world.grants == [("e-1", path)]
    args, kw = ch.deliver_operator_reply.await_args
    assert args == (rec, f"[casa file] statement.pdf at {path} — sent as a reply to your post"
                         "\nThe operator wrote: the March one")
    assert kw["message_id"] == 80


async def test_a_file_replying_to_an_ended_tasks_post_is_this_chats_agents(world):
    ch, _bus = _endpoint("own")
    world.index.delegation("d-1", "finance")
    _land(ch, 640, "d-1", "finance")
    world.index.end("d-1")
    await ch._on_non_text_message(types.SimpleNamespace(message=_file(reply_to=640)))
    assert world.grants == [] and world.inputs.calls == []
    assert world.inboxes["finance"].received == ["statement.pdf"]       # the default path
    assert ch._app.bot.sent and ch._app.bot.sent[0]["text"].startswith("Got statement.pdf")


def _behind_transport(monkeypatch, ch):
    """*ch* as an endpoint of the transport, beside Casa's own, the way
    casa_core registers them."""
    import tools
    from telegram_channel_double import make_transport
    casa = ch if ch.is_casa else _endpoint("casa")[0]
    others = () if ch is casa else (ch,)
    monkeypatch.setattr(tools, "_channel_manager", make_transport(casa, *others),
                        raising=False)


async def test_a_file_whose_task_ended_meanwhile_is_kept_and_told_in_the_agents_voice(
        world, monkeypatch):
    ch, _bus = _endpoint("own")
    _behind_transport(monkeypatch, ch)
    world.inputs.answer = "ended"
    world.index.delegation("d-1", "finance")
    _land(ch, 640, "d-1", "finance")
    await ch._on_non_text_message(types.SimpleNamespace(message=_file(reply_to=640)))
    assert [m["text"] for m in ch._app.bot.sent] == [
        "I've kept statement.pdf, but that task had already finished. "
        "Tell me what you'd like done with it."]


async def test_a_file_whose_task_ended_meanwhile_in_the_residents_chat_names_the_inbox(
        world, monkeypatch):
    ch, _bus = _endpoint("casa")
    _behind_transport(monkeypatch, ch)
    world.inputs.answer = "ended"
    world.index.delegation("d-1", "finance")
    _land(ch, 640, "d-1", "finance")
    await ch._on_non_text_message(types.SimpleNamespace(message=_file(reply_to=640)))
    assert [m["text"] for m in ch._app.bot.sent] == [
        f"statement.pdf is in {rb.post_label('finance')}'s inbox; the task had already ended."]


def _ended_job(status="completed"):
    return types.SimpleNamespace(
        id="c" * 32, status=status, role_or_type="finance", task="t",
        started_at=1000.0, completed_at=1060.0,
        origin={"chat_id": str(OPERATOR), "job": {"title": "Accounting check"}})


@pytest.mark.parametrize("cost,suffix", [(0.12, " · 1 min · $0.12"), (None, " · 1 min")])
@pytest.mark.parametrize("status,line", [("completed", "✅ Finished «Accounting check»"),
                                         ("cancelled", "🛑 Stopped «Accounting check»"),
                                         ("failed", "❌ «Accounting check» failed")])
async def test_a_jobs_end_line_in_its_agents_own_chat_does_not_name_the_agent(
        world, monkeypatch, status, line, cost, suffix):
    from engagement_registry import JOB_POST_START
    ch, _bus = _endpoint("own")
    _behind_transport(monkeypatch, ch)
    rec = _ended_job(status)
    if cost is not None:
        rec.origin["job"]["cost_usd"] = cost
    await ch._retire_job_post(rec, (TgAddress(OWN_BOT, OPERATOR, 501), JOB_POST_START))
    assert ch._app.bot.edited == []
    assert [m["text"] for m in ch._app.bot.sent] == [line + suffix]


async def test_a_jobs_end_line_in_the_residents_chat_names_the_agent(world, monkeypatch):
    from engagement_registry import JOB_POST_START
    ch, _bus = _endpoint("casa")
    _behind_transport(monkeypatch, ch)
    await ch._retire_job_post(_ended_job(),
                              (TgAddress(CASA_BOT, OPERATOR, 501), JOB_POST_START))
    assert ch._app.bot.edited == []
    assert [m["text"] for m in ch._app.bot.sent] == [
        "✅ Finance finished «Accounting check» · 1 min"]


# ---------------------------------------------------------------------------
# 📎 and albums
# ---------------------------------------------------------------------------

def _arm(ch, dest, role="finance"):
    import time
    ch._armings[OPERATOR] = {"chat_id": OPERATOR, "operator_id": OPERATOR, "role": role,
                             "artifact_id": "", "expires_at": time.monotonic() + 60,
                             "dest": dest}


@pytest.mark.parametrize("kind", ENDPOINTS)
async def test_an_arming_on_a_live_tasks_card_sends_the_next_file_into_the_task(world, kind):
    ch, bus = _endpoint(kind)
    ref = world.index.delegation("d-1", ch.default_agent)
    _arm(ch, ("task", ref), role=ch.default_agent)
    await ch._on_non_text_message(types.SimpleNamespace(message=_file()))
    path = Path(world.inboxes[ch.default_agent].ready_dir) / "1-statement.pdf"
    assert world.inputs.calls == [
        ("d-1", f"[casa file] statement.pdf at {path} — sent as a file for your post")]
    assert OPERATOR not in ch._armings


async def test_an_arming_for_the_agents_conversation_in_its_own_chat_is_the_default_path(world):
    ch, _bus = _endpoint("own")
    _arm(ch, ("conversation", "finance"))
    assert ch._file_address(OPERATOR, _user(), _file()) is None
    assert OPERATOR not in ch._armings


async def test_an_arming_for_a_specialists_card_in_the_residents_chat_is_the_default_path(world):
    ch, _bus = _endpoint("casa")
    _arm(ch, ("conversation", "finance"))
    assert ch._file_address(OPERATOR, _user(), _file()) is None
    assert OPERATOR not in ch._armings


@pytest.mark.parametrize("kind", ENDPOINTS)
async def test_an_album_follows_the_destination_its_first_file_resolved(world, kind):
    ch, _bus = _endpoint(kind)
    world.index.delegation("d-1", ch.default_agent)
    _land(ch, 640, "d-1", ch.default_agent)
    files = [_file(f"p{i}.pdf", album="g1", mid=80 + i, reply_to=640 if i == 0 else None)
             for i in range(3)]
    for f in files:
        await ch._on_non_text_message(types.SimpleNamespace(message=f))
    assert [c[0] for c in world.inputs.calls] == ["d-1", "d-1", "d-1"]
    assert world.inboxes[ch.default_agent].received == ["p0.pdf", "p1.pdf", "p2.pdf"]
    # another album keeps its own destination
    await ch._on_non_text_message(types.SimpleNamespace(message=_file("q.pdf", album="g2", mid=90)))
    assert len(world.inputs.calls) == 3


async def test_an_albums_arming_is_spent_by_its_first_file_and_followed_by_the_rest(world):
    ch, _bus = _endpoint("own")
    ref = world.index.delegation("d-1", "finance")
    _arm(ch, ("task", ref))
    for i in range(3):
        await ch._on_non_text_message(types.SimpleNamespace(
            message=_file(f"p{i}.pdf", album="g1", mid=80 + i)))
    assert [c[0] for c in world.inputs.calls] == ["d-1", "d-1", "d-1"]


# ---------------------------------------------------------------------------
# an agent's own bot: intake, hand-back, release
# ---------------------------------------------------------------------------

@pytest.fixture
def intake(monkeypatch):
    seen = types.SimpleNamespace(calls=[], answer="deliver")

    async def fake(bot_id, update_json):
        seen.calls.append((bot_id, update_json))
        if isinstance(seen.answer, Exception):
            raise seen.answer
        return seen.answer
    monkeypatch.setattr(own_voice, "intake", fake)
    return seen


@pytest.mark.parametrize("answer,queued", [("deliver", 1), ("held", 0), ("dormant_reply", 0)])
async def test_every_update_passes_the_lifecycle_before_it_is_queued(world, intake, answer, queued):
    ch, _bus = _endpoint("own")
    intake.answer = answer
    q = _IntakeQueue(ch)
    update = _text("hi")
    await q.put(update)
    assert intake.calls == [(OWN_BOT, update.to_dict())]
    assert q.qsize() == queued


async def test_a_dormant_update_gets_one_line_naming_where_to_talk(world, intake, monkeypatch):
    import tools
    from telegram_channel_double import make_transport
    import channels.telegram_transport as tt
    ch, _bus = _endpoint("own")
    casa, _ = _endpoint("casa")
    manager = make_transport(casa, ch)
    monkeypatch.setattr(tools, "_channel_manager", manager, raising=False)
    monkeypatch.setattr(tools, "_agent_role_map", {"finance": object()})
    intake.answer = "dormant_reply"
    await _IntakeQueue(ch).put(_text("hi"))
    assert [m["text"] for m in ch._app.bot.sent] == [
        "Finance no longer has a separate chat — talk to Ellen."]
    monkeypatch.setattr(tools, "_agent_role_map", {})
    await _IntakeQueue(ch).put(_text("hi again", mid=71))
    assert ch._app.bot.sent[-1]["text"] == "Finance isn't installed right now."
    assert tt.telegram_transport(manager) is not None


async def test_a_failing_lifecycle_delivers_nothing(world, intake):
    ch, _bus = _endpoint("own")
    intake.answer = RuntimeError("journal unreadable")
    q = _IntakeQueue(ch)
    await q.put(_text("hi"))
    assert q.qsize() == 0


async def test_the_librarys_stop_signal_passes_the_intake(world, intake):
    ch, _bus = _endpoint("own")
    q = _IntakeQueue(ch)
    stop = object()
    await q.put(stop)
    assert intake.calls == [] and q.get_nowait() is stop


async def test_a_message_its_agents_queue_cannot_take_goes_back_to_the_lifecycle(world, intake):
    ch, bus = _endpoint("own", register=False)
    intake.answer = "held"
    update = _text("are you there?")
    await _handle(ch, update)
    assert intake.calls == [(OWN_BOT, update.to_dict())]            # handed back once
    assert "finance" not in bus.queues


async def test_a_message_handed_back_and_delivered_again_reaches_the_agent(
        world, intake, monkeypatch):
    ch, bus = _endpoint("own", register=False)

    async def registered(bot_id, update_json):
        bus.register("finance", _noop)                  # the agent came back meanwhile
        return "deliver"
    monkeypatch.setattr(own_voice, "intake", registered)
    await _handle(ch, _text("are you there?"))
    [turn] = _turns(bus, "finance")
    assert turn.content == "are you there?"


async def test_the_residents_bot_keeps_its_plain_send(world, intake):
    ch, bus = _endpoint("casa", register=False)
    await _handle(ch, _text("hello"))
    assert intake.calls == []                           # no lifecycle on the resident's bot


async def test_a_released_update_is_delivered_on_the_normal_path(world, intake, monkeypatch):
    import channels.telegram as tg_mod
    ch, bus = _endpoint("own")
    update = _text("held while binding")
    monkeypatch.setattr(tg_mod.Update, "de_json", lambda data, bot: update, raising=False)
    assert await ch.release_update(update.to_dict()) == "accepted"
    [turn] = _turns(bus, "finance")
    assert turn.content == "held while binding"
    assert intake.calls == []                           # the intake already let it go


async def test_a_released_update_whose_agent_has_no_queue_stays_held(world, intake, monkeypatch):
    import channels.telegram as tg_mod
    ch, _bus = _endpoint("own", register=False)
    update = _text("held while binding")
    monkeypatch.setattr(tg_mod.Update, "de_json", lambda data, bot: update, raising=False)
    assert await ch.release_update(update.to_dict()) == "no_target"
    assert intake.calls == []                           # the release keeps it, not a new hold


# ---------------------------------------------------------------------------
# the operator's first turn in an agent's own chat opens it
# ---------------------------------------------------------------------------

async def _first_turn(ch, how):
    if how == "caption":
        await ch._on_non_text_message(types.SimpleNamespace(
            message=_file("a.pdf", caption="what is this?")))
    else:
        await _handle(ch, _text(how))


@pytest.mark.parametrize("how", ["hi", "/start", "caption"])
async def test_the_first_turn_of_an_unopened_chat_carries_the_opening(world, how):
    ch, bus = _endpoint("own")
    world.opening.unopened = True
    await _first_turn(ch, how)
    [turn] = _turns(bus, "finance")
    assert turn.context["_chat_opening"] is True
    assert world.opening.marks == [OWN_BOT]


async def test_an_open_chat_carries_no_opening(world):
    ch, bus = _endpoint("own")
    await _handle(ch, _text("hi"))
    [turn] = _turns(bus, "finance")
    assert "_chat_opening" not in turn.context
    assert world.opening.asked == [OWN_BOT] and world.opening.marks == []


async def test_a_turn_its_queue_refuses_opens_nothing(world, intake):
    ch, bus = _endpoint("own", register=False)
    world.opening.unopened = True
    intake.answer = "held"
    await _handle(ch, _text("hi"))
    assert "finance" not in bus.queues
    assert world.opening.marks == [] and world.opening.unopened is True


async def test_a_turn_handed_back_and_taken_on_retry_opens_the_chat(world, monkeypatch):
    ch, bus = _endpoint("own", register=False)
    world.opening.unopened = True

    async def registered(bot_id, update_json):
        bus.register("finance", _noop)                  # the agent came back meanwhile
        return "deliver"
    monkeypatch.setattr(own_voice, "intake", registered)
    await _handle(ch, _text("hi"))
    await _handle(ch, _text("still there?", mid=71))
    first, second = _turns(bus, "finance")
    assert first.context["_chat_opening"] is True
    assert "_chat_opening" not in second.context
    assert world.opening.marks == [OWN_BOT]


async def test_the_first_turn_of_a_moved_chat_carries_the_moved_opening(world):
    ch, bus = _endpoint("own")
    world.opening.unopened = True
    world.opening.moved = True
    await _handle(ch, _text("hi"))
    [turn] = _turns(bus, "finance")
    assert turn.context["_chat_opening"] == "moved"
    assert world.opening.marks == [OWN_BOT]


async def test_a_moved_opening_marks_the_chat_open_once_taken(world, monkeypatch):
    ch, bus = _endpoint("own", register=False)
    world.opening.unopened = True
    world.opening.moved = True
    await _handle(ch, _text("hi"))                      # no queue yet: nothing opens
    assert world.opening.marks == []

    async def registered(bot_id, update_json):
        bus.register("finance", _noop)                  # the agent came back meanwhile
        return "deliver"
    monkeypatch.setattr(own_voice, "intake", registered)
    await _handle(ch, _text("hi", mid=70))
    await _handle(ch, _text("still there?", mid=71))
    first, second = _turns(bus, "finance")
    assert first.context["_chat_opening"] == "moved"
    assert "_chat_opening" not in second.context
    assert world.opening.marks == [OWN_BOT]


async def test_a_moved_read_that_fails_opens_the_chat_as_a_first_one(world, caplog):
    ch, bus = _endpoint("own")
    world.opening.unopened = True
    world.opening.moved = RuntimeError("no lifecycle")
    await _handle(ch, _text("hi"))
    [turn] = _turns(bus, "finance")
    assert turn.context["_chat_opening"] is True
    assert world.opening.marks == [OWN_BOT]
    assert any("moved" in r.getMessage() for r in caplog.records)


async def test_the_residents_bot_never_asks(world):
    ch, bus = _endpoint("casa")
    world.opening.unopened = True
    await _handle(ch, _text("/start"))
    [turn] = _turns(bus, "assistant")
    assert "_chat_opening" not in turn.context
    assert world.opening.asked == [] and world.opening.marks == []


async def test_someone_elses_message_never_asks(world):
    ch, _bus = _endpoint("own")
    world.opening.unopened = True
    await _handle(ch, _text("hi", uid=555))
    assert world.opening.asked == [] and world.opening.marks == []


@pytest.mark.parametrize("kind,text", [("own", "/start"), ("casa", "/start"),
                                       ("own", "hello")])
async def test_the_channel_leaves_no_opening_note_itself(world, kind, text):
    ch, _bus = _endpoint(kind)
    world.opening.unopened = kind == "own"
    await _handle(ch, _text(text))
    assert chat_notes.drain(ch.bot_id, OPERATOR) == []


async def test_the_opening_mark_is_casas_alone():
    import provenance
    assert "_chat_opening" in provenance.RESERVED_CONTEXT_KEYS
    assert "_chat_opening" not in provenance.sanitize_external_context({"_chat_opening": True})


async def test_a_released_captionless_document_opens_nothing_and_the_next_text_greets(
        world, monkeypatch):
    import channels.telegram as tg_mod
    ch, bus = _endpoint("own")
    world.opening.unopened = True
    doc = types.SimpleNamespace(message=_file("a.pdf"), callback_query=None)
    doc.message.text = None
    monkeypatch.setattr(tg_mod.Update, "de_json", lambda data, bot: doc, raising=False)
    assert await ch.release_update({"update_id": 1}) == "accepted"
    assert world.inboxes["finance"].received == ["a.pdf"]          # stored
    assert len(ch._app.bot.sent) == 1                               # and acknowledged
    assert _turns(bus, "finance") == []
    assert world.opening.marks == []
    await _handle(ch, _text("hi"))
    [turn] = _turns(bus, "finance")
    assert turn.context["_chat_opening"] is True
    assert world.opening.marks == [OWN_BOT]


# ---------------------------------------------------------------------------
# the agent says hello by itself once its bot is bound
# ---------------------------------------------------------------------------

async def test_the_greeting_is_casas_turn_carrying_the_opening(world):
    ch, bus = _endpoint("own")
    world.opening.unopened = True
    assert await ch.speak_first() is True
    [turn] = _turns(bus, "finance")
    assert turn.content == "/start"
    assert turn.trusted_user_origin is None
    ctx = turn.context
    assert ctx["synthetic"] == "chat_opening" and ctx["_chat_opening"] is True
    assert ctx["_tg_bot"] == OWN_BOT and ctx["chat_id"] == str(OPERATOR)
    assert "_operator_turn" not in ctx and "_operator_words" not in ctx
    assert world.opening.marks == []            # taken by the queue opens nothing yet


async def test_the_greeting_opens_the_chat_once_delivered(world):
    ch, bus = _endpoint("own")
    world.opening.unopened = True
    await ch.speak_first()
    [turn] = _turns(bus, "finance")
    await turn.on_delivery()                    # the agent's reply reached Telegram
    assert world.opening.marks == [OWN_BOT]


async def test_a_moved_chats_greeting_carries_the_moved_opening(world):
    ch, bus = _endpoint("own")
    world.opening.unopened = True
    world.opening.moved = True
    await ch.speak_first()
    [turn] = _turns(bus, "finance")
    assert turn.context["_chat_opening"] == "moved"


@pytest.mark.parametrize("kind", ENDPOINTS)
async def test_an_open_chat_or_the_residents_bot_gets_no_greeting(world, kind):
    ch, bus = _endpoint(kind)
    world.opening.unopened = kind == "casa"
    assert await ch.speak_first() is False
    assert _turns(bus, ch.default_agent) == []


async def test_a_greeting_its_agents_queue_cannot_take_opens_nothing(world):
    ch, bus = _endpoint("own", register=False)
    world.opening.unopened = True
    assert await ch.speak_first() is False
    assert world.opening.marks == [] and world.opening.unopened is True


async def test_an_operator_message_taken_first_leaves_no_greeting(world):
    """The operator's first message holds the chat's serial lock when the
    greeting starts: it opens the chat, and the greeting, reading the chat
    again under the lock, adds no second opening."""
    ch, bus = _endpoint("own")
    world.opening.unopened = True
    lock = ch._chat_serial_locks.setdefault(str(OPERATOR), asyncio.Lock())
    await lock.acquire()
    first = asyncio.ensure_future(ch._handle(_text("/start"), None))
    greeting = asyncio.ensure_future(ch.speak_first())
    await asyncio.sleep(0)
    lock.release()
    await first
    assert await greeting is False
    [turn] = _turns(bus, "finance")
    assert turn.context["_chat_opening"] is True and "synthetic" not in turn.context
    assert world.opening.marks == [OWN_BOT]


async def test_a_start_while_the_greeting_is_on_its_way_adds_no_turn(world):
    """The operator presses Start while the agent's greeting is queued but
    not yet delivered: the greeting answers it, so the Start adds no second
    opening turn; once the greeting landed, a Start is an ordinary turn."""
    ch, bus = _endpoint("own")
    world.opening.unopened = True
    assert await ch.speak_first() is True
    [greeting] = _turns(bus, "finance")
    await _handle(ch, _text("/start"))
    assert _turns(bus, "finance") == []
    await _handle(ch, _text("hi"))                  # anything else goes through
    [turn] = _turns(bus, "finance")
    assert turn.content == "hi"
    await greeting.on_delivery()
    await _handle(ch, _text("/start"))
    [later] = _turns(bus, "finance")
    assert later.content == "/start"


async def test_a_refused_greeting_lets_the_next_start_open_the_chat(world):
    from output_boundary import casa_text
    from telegram.error import Forbidden
    ch, bus = _endpoint("own")
    world.opening.unopened = True
    await ch.speak_first()
    _turns(bus, "finance")

    async def refuse(**_kw):
        raise Forbidden("Forbidden: bot can't initiate conversation with a user")
    ch._app.bot.send_message = refuse
    await ch.send_response(casa_text("Hi, I'm Alex."),
                           {"chat_id": str(OPERATOR), "synthetic": "chat_opening"})
    await _handle(ch, _text("/start"))
    [turn] = _turns(bus, "finance")
    assert turn.content == "/start" and turn.context["_chat_opening"] is True


async def test_a_greeting_that_never_ends_replays_a_held_start_when_its_time_runs_out(
        world, monkeypatch):
    """The greeting neither lands nor is refused (a failed turn): when its
    window runs out, the operator's held Start becomes his own turn."""
    import channels.telegram as tg
    monkeypatch.setattr(tg, "GREETING_IN_FLIGHT_S", 0.01)
    ch, bus = _endpoint("own")
    world.opening.unopened = True
    await ch.speak_first()
    _turns(bus, "finance")
    await _handle(ch, _text("/start"))
    assert _turns(bus, "finance") == []
    for _ in range(100):
        if getattr(ch, "_start_replay", None) is not None:
            break
        await asyncio.sleep(0.01)
    await ch._start_replay
    [turn] = _turns(bus, "finance")
    assert turn.context["_chat_opening"] is True and ch._greeting_timer is None


async def test_a_silent_greeting_lets_the_next_start_open_the_chat(world):
    ch, bus = _endpoint("own")
    world.opening.unopened = True
    await ch.speak_first()
    _turns(bus, "finance")
    await ch.turn_finished({"chat_id": str(OPERATOR), "synthetic": "chat_opening"})
    await _handle(ch, _text("/start"))
    [turn] = _turns(bus, "finance")
    assert turn.context["_chat_opening"] is True


@pytest.mark.parametrize("end", ["silent", "refused"])
async def test_a_start_held_for_a_greeting_that_never_lands_is_replayed(world, end):
    """The operator's Start was held for the greeting, and the greeting then
    ends without reaching him: his Start becomes his own opening turn."""
    from output_boundary import casa_text
    from telegram.error import Forbidden
    ch, bus = _endpoint("own")
    world.opening.unopened = True
    await ch.speak_first()
    _turns(bus, "finance")
    await _handle(ch, _text("/start"))
    assert _turns(bus, "finance") == []
    if end == "silent":
        await ch.turn_finished({"chat_id": str(OPERATOR), "synthetic": "chat_opening"})
    else:
        async def refuse(**_kw):
            raise Forbidden("Forbidden: bot can't initiate conversation with a user")
        ch._app.bot.send_message = refuse
        await ch.send_response(casa_text("Hi, I'm Alex."),
                               {"chat_id": str(OPERATOR), "synthetic": "chat_opening"})
    await ch._start_replay
    [turn] = _turns(bus, "finance")
    assert turn.content == "/start" and turn.context["_chat_opening"] is True


async def test_a_start_held_for_a_delivered_greeting_is_answered_by_it(world):
    ch, bus = _endpoint("own")
    world.opening.unopened = True
    await ch.speak_first()
    [greeting] = _turns(bus, "finance")
    await _handle(ch, _text("/start"))
    await greeting.on_delivery()
    assert ch._held_start is None and _turns(bus, "finance") == []


@pytest.mark.parametrize("text", ["/start@HeyAlexTestBot please check my balance",
                                  "/start@SomeOtherBot", "/start please"])
async def test_anything_but_a_bare_start_during_the_greeting_is_an_ordinary_turn(
        world, text):
    ch, bus = _endpoint("own")
    world.opening.unopened = True
    await ch.speak_first()
    _turns(bus, "finance")
    await _handle(ch, _text(text))
    [turn] = _turns(bus, "finance")
    assert turn.content == text


async def test_the_greeting_has_no_operator_grant_identity(world):
    """Casa starts the greeting: classified as no direct message, so it
    carries none of the operator's grants."""
    import agent as agent_mod
    import provenance
    ch, bus = _endpoint("own")
    world.opening.unopened = True
    await ch.speak_first()
    [turn] = _turns(bus, "finance")
    origin = {**turn.context, "message_type": "channel_in", "channel": "telegram",
              "source": "telegram", "role": "finance", "execution_role": "finance"}
    token = agent_mod.origin_var.set(origin)
    try:
        assert provenance.turn_provenance().transport == "other"
    finally:
        agent_mod.origin_var.reset(token)


async def test_the_greeting_thinks_privately_and_sends_its_reply_whole():
    from bus import MessageType
    from output_boundary import TurnScope
    msg = BusMessage(type=MessageType.CHANNEL_IN, source="telegram",
                     target="finance", content="/start", channel="telegram",
                     context={"synthetic": "chat_opening", "cid": "c"})
    assert TurnScope.mint(msg, types.SimpleNamespace(role="finance")).streaming_allowed is False


@pytest.mark.parametrize("error", ["forbidden", "chat_not_found"])
async def test_a_greeting_telegram_refuses_is_one_info_line(world, caplog, error):
    import logging

    from channels import DeliveryOutcome
    from output_boundary import casa_text
    from telegram.error import BadRequest, Forbidden
    ch, _bus = _endpoint("own")
    exc = (Forbidden("Forbidden: bot can't initiate conversation with a user")
           if error == "forbidden" else BadRequest("Chat not found"))

    async def refuse(**_kw):
        raise exc
    ch._app.bot.send_message = refuse
    caplog.set_level(logging.INFO)
    out = await ch.send_response(casa_text("Hi, I'm Alex."),
                                 {"chat_id": str(OPERATOR), "synthetic": "chat_opening"})
    assert out is DeliveryOutcome.NOT_DELIVERED
    [line] = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert line.levelno == logging.INFO and "greeting" in line.getMessage()
    with pytest.raises(type(exc)):              # any other reply is not excused
        await ch.send_response(casa_text("Hi."), {"chat_id": str(OPERATOR)})


# ---------------------------------------------------------------------------
# lines in an agent's own chat carry no 📊 head
# ---------------------------------------------------------------------------

async def test_an_agents_own_chat_names_it_plainly(world, monkeypatch):
    import agent_lane
    monkeypatch.setattr(rb, "post_label", lambda role: "📊 Finance")
    own, _ = _endpoint("own")
    casa, _ = _endpoint("casa")
    assert own._agent_label("finance") == "Finance"
    assert casa._agent_label("finance") == "📊 Finance"
    agent_lane.FAULTS.mark("finance", "r-1", "probe")
    own._stop_tap = AsyncMock(return_value=False)
    meta = {"chat_id": OPERATOR, "role": "finance", "operator_id": OPERATOR, "options": ["Yes"],
            "message_id": 640, "text": "Pair 17?"}
    await own._dispatch_proposal_tap(meta=meta, idx=0, request_id="r" * 32)
    assert own._app.bot.sent[0]["text"] == "Finance is out of service until Casa restarts."


# ---------------------------------------------------------------------------
# Casa cards that link out
# ---------------------------------------------------------------------------

async def test_a_casa_card_may_carry_link_buttons(world):
    from output_boundary import casa_text
    ch, _ = _endpoint("casa")
    mid = await ch.post_dm_keyboard(chat_id=OPERATOR, request_id="q" * 32,
                                    text=casa_text("Finance can get a separate chat with you."),
                                    options=["Create Finance's chat", "Not now"],
                                    urls=["https://t.me/newbot/EllenBot/FinanceBot?name=Finance"])
    assert mid is not None
    rows = ch._app.bot.sent[0]["reply_markup"].inline_keyboard
    assert rows[0][0].url == "https://t.me/newbot/EllenBot/FinanceBot?name=Finance"
    assert rows[0][0].callback_data is None
    assert rows[1][0].callback_data == f"v1|resident_ask|{'q' * 32}|1" and rows[1][0].url is None
    assert await ch.edit_dm_message(OPERATOR, mid, "Finance's chat is ready.",
                                    buttons=[("Open Finance's chat", "")],
                                    urls=["https://t.me/FinanceBot"])
    [[button]] = ch._app.bot.edited[0]["reply_markup"].inline_keyboard
    assert button.url == "https://t.me/FinanceBot" and button.callback_data is None


@pytest.mark.parametrize("kind", ENDPOINTS)
async def test_new_resets_this_chats_agents_own_conversation(world, kind, monkeypatch):
    """``/new`` on a bot resets the conversation of that bot's agent — on an
    agent's own bot its own conversation, never the resident's."""
    import session_registry
    from session_registry import build_scoped_session_key
    reset = AsyncMock()
    monkeypatch.setattr(session_registry, "reset_channel", reset)
    ch, bus = _endpoint(kind)
    ch._session_registry = object()
    await _handle(ch, _text("/new"))
    reset.assert_awaited_once()
    assert reset.await_args.args[0] == build_scoped_session_key(
        "telegram", ch.default_agent, str(OPERATOR))
    assert reset.await_args.args[1] is ch._session_registry
    assert _turns(bus, ch.default_agent) == []
