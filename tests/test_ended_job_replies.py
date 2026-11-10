"""#1522: a reply in the direct chat to a post of a background job that has
ended — its start line, a question, a plugin post, one that landed after the
end — is the resident's turn, carrying a Casa note that names the job, its
agent, how it ended and when. It never reaches the specialist desk, which
cannot see the job. A file sent as such a reply is the resident's file. A reply
from anyone but the operator who started the job, or in another chat, keeps
today's routes and note.

REAL ``EngagementRegistry`` on a tmp tombstone, REAL ``TelegramChannel`` text
and file handlers, REAL post map and desk route (only its delegability and the
desk turn itself are stubbed), REAL bus queue.
"""
from __future__ import annotations

import asyncio
import os
import types
from datetime import datetime
from types import SimpleNamespace as NS

import pytest

import agent_inbox as ai
import result_broker as rb
import specialist_desk as sd
from test_desk_route import OPERATOR, _channel, _record, _settle, routed  # noqa: F401
from test_inbound_files import PDF, _doc, _msg
from test_telegram_new_reset import _drain_bus
from timekeeping import resolve_tz

pytestmark = pytest.mark.asyncio

ENDED_AT = 1_790_000_000.0          # a fixed epoch; rendered in Casa's zone at run time


def _ended_at() -> str:
    return datetime.fromtimestamp(ENDED_AT, tz=resolve_tz()).strftime("%Y-%m-%d %H:%M")


def _job_state():
    import background_jobs as jobs
    decl = jobs.JobDecl("ledger:q3", "ledger", "q3", "ledger:q3",
                        "Q3 accounting\nall accounts", None)
    return jobs.initial_job_state(decl, started_by="operator")


@pytest.fixture(autouse=True)
def _names(monkeypatch):
    import tools
    monkeypatch.setattr(tools, "_display_name_for_role",
                        lambda role: {"finance": "Finance"}.get(role, role))


async def _ended_job(tmp_path, *, role="finance", chat=OPERATOR, user=OPERATOR,
                     posts=(), outcome="completed", plugin=None):
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    origin = {"role": "assistant", "channel": "telegram", "chat_id": str(chat),
              "user_id": user, "job": _job_state()}
    if plugin is not None:
        origin["plugin_job"] = {"plugin": plugin, "model": "m"}
    rec = await reg.create("plugin" if plugin else "specialist", role, "in_casa",
                           "t", origin)
    for mid, keyboard in posts:
        assert reg.record_job_post(rec.id, mid, keyboard=keyboard) is True
    assert await reg.try_transition_terminal(
        rec.id, outcome, completed_at=ENDED_AT, strict=True)
    return reg, rec


def _text_reply(text="did it tag row 7?", *, quoted_id, user=OPERATOR,
                quoted_text="📊 Finance started «Q3 accounting»."):
    who = types.SimpleNamespace(first_name="Nicola", id=user)
    quoted = types.SimpleNamespace(message_id=quoted_id, text=quoted_text, caption=None,
                                   date=None, from_user=None, sender_chat=None)
    message = types.SimpleNamespace(text=text, message_id=70, reply_to_message=quoted,
                                    from_user=who, sender_chat=None)
    return types.SimpleNamespace(message=message,
                                 effective_chat=types.SimpleNamespace(id=str(OPERATOR)),
                                 effective_user=who)


async def _turns(ch, bus, update):
    await ch._handle(update, None)
    await _settle(ch)
    return await _drain_bus(bus)


def _note_for(status="completed", *, whose="Finance's", via=""):
    return (f"a message Casa posted for {whose} background job «Q3 accounting»{via}, "
            f"which ended ({status}) at {_ended_at()}")


# ---------------------------------------------------------------------------
# text replies
# ---------------------------------------------------------------------------


async def test_a_reply_to_an_ended_jobs_start_line_is_the_residents_turn_with_its_end(
        routed, tmp_path):
    from engagement_registry import JOB_POST_START
    pm, spawned = routed
    reg, rec = await _ended_job(tmp_path, posts=[(501, JOB_POST_START)])
    ch, bus = _channel()
    ch._engagement_registry = reg
    [turn] = await _turns(ch, bus, _text_reply(quoted_id=501))
    assert spawned.await_count == 0
    assert turn.target == "assistant" and turn.content == "did it tag row 7?"
    assert turn.context["_reply_note"] == sd.reply_note(
        _note_for(), None, "📊 Finance started «Q3 accounting».")


async def test_a_reply_to_an_ended_jobs_plugin_post_never_reaches_the_desk(routed, tmp_path):
    """The plugin post sits in the post map as Finance's, which the desk route
    would take: the ended job's own record decides first."""
    pm, spawned = routed
    # the post landed while the job lived: filed on the record and in the map
    reg, rec = await _ended_job(tmp_path, posts=[(640, None)])
    pm.record(OPERATOR, 640, _record(owner=rec.id))
    ch, bus = _channel()
    ch._engagement_registry = reg
    [turn] = await _turns(ch, bus, _text_reply(quoted_id=640, quoted_text="📊 Finance\nrow 7"))
    assert spawned.await_count == 0
    assert turn.context["_reply_note"] == sd.reply_note(_note_for(), None, "📊 Finance\nrow 7")


async def test_a_plugin_post_landing_after_the_end_is_the_ended_jobs(routed, tmp_path):
    """A job can end while one of its plugin's posts is still being sent: the
    post is filed as the ended job's as it lands, so a reply to it is told the
    end and is not the desk's."""
    pm, spawned = routed
    reg, rec = await _ended_job(tmp_path)
    ch, bus = _channel()
    ch._engagement_registry = reg
    ch._record_post(OPERATOR, NS(message_id=650), _record(owner=rec.id))
    assert reg.ended_job_post_owner(OPERATOR, 650) is rec
    [turn] = await _turns(ch, bus, _text_reply(quoted_id=650, quoted_text="📊 Finance\nrow 9"))
    assert spawned.await_count == 0
    assert turn.context["_reply_note"] == sd.reply_note(_note_for(), None, "📊 Finance\nrow 9")


async def test_a_reply_to_an_ended_jobs_question_names_how_it_ended(routed, tmp_path):
    from engagement_registry import JOB_POST_QUESTION
    pm, spawned = routed
    reg, rec = await _ended_job(tmp_path, posts=[(520, JOB_POST_QUESTION)],
                                outcome="cancelled")
    ch, bus = _channel()
    ch._engagement_registry = reg
    [turn] = await _turns(ch, bus, _text_reply("the savings one", quoted_id=520,
                                               quoted_text="Which account?"))
    assert spawned.await_count == 0
    assert turn.context["_reply_note"] == sd.reply_note(
        _note_for("cancelled"), None, "Which account?")


async def test_a_residents_plugin_job_is_named_as_its_own(routed, tmp_path):
    pm, spawned = routed
    reg, rec = await _ended_job(tmp_path, role="assistant", plugin="ledger",
                                posts=[(530, None)], outcome="error")
    ch, bus = _channel()
    ch._engagement_registry = reg
    [turn] = await _turns(ch, bus, _text_reply(quoted_id=530, quoted_text="row 7 tagged"))
    assert turn.context["_reply_note"] == sd.reply_note(
        _note_for("error", whose="your", via=" (run by the plugin ledger)"), None,
        "row 7 tagged")


async def test_a_reply_from_someone_who_did_not_start_the_job_keeps_todays_note(
        routed, tmp_path):
    pm, spawned = routed
    reg, rec = await _ended_job(tmp_path, user=777, posts=[(501, None)])
    ch, bus = _channel()
    ch._engagement_registry = reg
    pm.record(OPERATOR, 501, _record(owner=rec.id))
    # not this operator's job: the post map's desk route, exactly as before
    assert await _turns(ch, bus, _text_reply(quoted_id=501, quoted_text="row 7")) == []
    assert spawned.await_count == 1
    note = ch._reply_note_for(_text_reply(quoted_id=501, quoted_text="row 7"), str(OPERATOR))
    assert note == sd.reply_note(f"a message Casa posted for {sd.label_for('finance')}",
                                 None, "row 7")


async def test_a_reply_to_an_ended_job_of_another_chat_keeps_todays_routes(routed, tmp_path):
    pm, spawned = routed
    reg, rec = await _ended_job(tmp_path, chat=OPERATOR + 1, posts=[(501, None)])
    ch, bus = _channel()
    ch._engagement_registry = reg
    pm.record(OPERATOR, 501, _record(owner=rec.id))
    assert await _turns(ch, bus, _text_reply(quoted_id=501, quoted_text="row 7")) == []
    assert spawned.await_count == 1                      # the desk, as before


async def test_a_non_operators_reply_gets_todays_note(routed, tmp_path):
    """Even on a job whose record names that very sender: only the
    authenticated operator's reply is told the job's end."""
    reg, rec = await _ended_job(tmp_path, user=555, posts=[(501, None)])
    ch, _bus = _channel()
    ch._engagement_registry = reg
    note = ch._reply_note_for(_text_reply(quoted_id=501, user=555), str(OPERATOR))
    assert "background job" not in note
    assert "someone else's message" in note


# ---------------------------------------------------------------------------
# a file sent as a reply
# ---------------------------------------------------------------------------


class _Sched:
    def add_job(self, *a, **k):
        pass


@pytest.fixture
async def inbox(tmp_path, monkeypatch):
    ai._reset_for_tests()
    await ai.wire(_Sched(), str(tmp_path / "agent-inbox"), role="assistant")
    ai._inboxes["finance"] = ai.open_inbox("finance", str(tmp_path / "agent-inbox"))

    async def fetch(bot, file_id, cap):
        return PDF
    monkeypatch.setattr(ai, "fetch_telegram_file", fetch)
    yield ai.get_inbox("assistant")
    ai._reset_for_tests()


class _Bot:
    def __init__(self):
        self.texts: list[str] = []

    async def send_message(self, **kw):
        self.texts.append(kw.get("text"))
        return NS(message_id=len(self.texts) + 9000)


def _file_reply(caption, quoted_id):
    m = _msg(chat_id=OPERATOR, user_id=OPERATOR, document=_doc("invoice-0912.pdf"))
    m.caption = caption
    m.media_group_id = None
    m.reply_to_message = NS(message_id=quoted_id, text="📊 Finance\nrow 7", caption=None,
                            date=None, from_user=None, sender_chat=None)
    return m


async def _file_rig(routed, tmp_path):
    pm, spawned = routed
    reg, rec = await _ended_job(tmp_path)
    ch, bus = _channel()
    ch._app = types.SimpleNamespace(bot=_Bot())
    ch._engagement_registry = reg
    ch._record_post(OPERATOR, NS(message_id=640), _record(owner=rec.id))
    return ch, bus, spawned


async def test_a_captioned_file_replying_to_an_ended_jobs_post_is_the_residents(
        routed, tmp_path, inbox):
    ch, bus, spawned = await _file_rig(routed, tmp_path)
    await ch._on_non_text_message(NS(message=_file_reply("file this one too", 640)))
    await _settle(ch)
    assert spawned.await_count == 0
    assert ai.get_inbox("finance").list_files() == []
    [stored] = inbox.list_files()
    [turn] = await _drain_bus(bus)
    assert turn.target == "assistant" and turn.content == "file this one too"
    assert turn.context["_received_file"]["files"] == [
        {"path": os.path.join(inbox.ready_dir, stored.name), "name": "invoice-0912.pdf"}]
    assert turn.context["_reply_note"] == sd.reply_note(_note_for(), None, "📊 Finance\nrow 7")


async def test_a_file_without_a_caption_replying_to_an_ended_jobs_post_is_stored_and_acknowledged(
        routed, tmp_path, inbox):
    ch, bus, spawned = await _file_rig(routed, tmp_path)
    await ch._on_non_text_message(NS(message=_file_reply(None, 640)))
    await _settle(ch)
    assert spawned.await_count == 0
    assert ai.get_inbox("finance").list_files() == []
    assert len(inbox.list_files()) == 1
    assert await _drain_bus(bus) == []
    [ack] = ch._app.bot.texts
    assert ack.startswith("Got invoice-0912.pdf")


async def test_the_ended_route_never_outranks_a_live_job(routed, tmp_path):
    """While the terminal write is pending the record still counts as live:
    no ended owner, so the live route (which runs first) keeps the reply."""
    import threading
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    origin = {"role": "assistant", "channel": "telegram", "chat_id": str(OPERATOR),
              "user_id": OPERATOR, "job": _job_state()}
    rec = await reg.create("specialist", "finance", "in_casa", "t", origin)
    reg.record_job_post(rec.id, 501)
    while reg._deferred_persists:
        await asyncio.gather(*list(reg._deferred_persists), return_exceptions=True)
    real = reg._write_tombstone
    started, release = threading.Event(), threading.Event()

    def held(snapshot):
        started.set()
        release.wait(5)
        real(snapshot)
    reg._write_tombstone = held
    transition = asyncio.ensure_future(
        reg.try_transition_terminal(rec.id, "completed", strict=True))
    for _ in range(500):
        if started.is_set():
            break
        await asyncio.sleep(0.005)
    ch, _bus = _channel()
    ch._engagement_registry = reg
    assert reg.job_post_owner(501) is rec
    assert ch._ended_job_quoted(str(OPERATOR), types.SimpleNamespace(id=OPERATOR),
                                types.SimpleNamespace(message_id=501)) is None
    release.set()
    assert await transition is True
    reg._write_tombstone = real
    assert ch._ended_job_quoted(str(OPERATOR), types.SimpleNamespace(id=OPERATOR),
                                types.SimpleNamespace(message_id=501)) is rec


async def test_a_pending_terminal_reply_goes_into_the_job_through_the_handler(
        tmp_path, monkeypatch):
    """The whole text handler, not the lookups: a reply arriving while the
    job's terminal write is pending is the job's (the live route runs first),
    and when the write fails and the job is live again it is delivered once —
    no desk turn and no resident turn."""
    from unittest.mock import AsyncMock
    from test_job_dm_attention import CHAT, _drain, _HeldWrite, _reply_update, _rig, _wait
    r = await _rig(tmp_path, monkeypatch)
    r.reg.record_job_post(r.rec.id, 501)
    await _drain(r)
    desk = AsyncMock(return_value=True)
    monkeypatch.setattr(r.ch, "_maybe_route_desk_reply", desk)
    resident = AsyncMock(return_value=True)
    monkeypatch.setattr(r.ch, "_dispatch_dm_turn", resident)
    held = _HeldWrite(r.reg)
    held.fail = True
    transition = asyncio.ensure_future(r.reg.try_transition_terminal(
        r.rec.id, "completed", strict=True))
    await _wait(held.started.is_set, "the terminal write")
    handler = asyncio.ensure_future(r.ch._handle(_reply_update("wait", 501), None))
    await asyncio.sleep(0.01)
    held.release.set()
    with pytest.raises(OSError):
        await transition
    held.fail = False
    await handler
    held.restore()
    await _drain(r)
    assert r.rec.status == "active"
    assert len(r.delivered) == 1 and r.delivered[0].endswith("wait")
    assert desk.await_count == 0
    assert resident.await_count == 0


async def test_ended_jobs_of_two_chats_sharing_a_message_id_stay_apart(routed, tmp_path):
    """Telegram numbers messages per chat: two ended jobs, in two chats, each
    filed a post 501. After a restart, a reply in the first chat names that
    chat's job and reaches the resident — never the desk."""
    from engagement_registry import EngagementRegistry
    pm, spawned = routed
    path = str(tmp_path / "e.json")
    reg = EngagementRegistry(tombstone_path=path)
    recs = {}
    for chat, role in ((OPERATOR, "finance"), (99, "travel")):
        origin = {"role": "assistant", "channel": "telegram", "chat_id": str(chat),
                  "user_id": OPERATOR, "job": _job_state()}
        recs[chat] = await reg.create("specialist", role, "in_casa", "t", origin)
        assert reg.record_job_post(recs[chat].id, 501) is True
        assert await reg.try_transition_terminal(
            recs[chat].id, "completed", completed_at=ENDED_AT, strict=True)
    pm.record(OPERATOR, 501, _record(owner=recs[OPERATOR].id))
    again = EngagementRegistry(tombstone_path=path)
    await again.load()
    assert again.ended_job_post_owner(OPERATOR, 501).id == recs[OPERATOR].id
    assert again.ended_job_post_owner(99, 501).id == recs[99].id
    ch, bus = _channel()
    ch._engagement_registry = again
    [turn] = await _turns(ch, bus, _text_reply(quoted_id=501, quoted_text="row 7"))
    assert spawned.await_count == 0
    assert turn.context["_reply_note"] == sd.reply_note(_note_for(), None, "row 7")
