"""Two real Telegram endpoints behind one transport: nothing one bot receives,
posts or keys reaches the other.

Telegram numbers updates and messages per bot, and a private chat's id is the
user's id, so two bots' chats with the operator share the chat id and both
count message ids up from 1. Production runs one bot, so this file is where
the boundary between two is exercised: bot 111 (Casa's endpoint, the
``assistant``) and bot 222 (``finance``), both real ``TelegramChannel``
instances over fake bots, both serving operator 100, fed the same update ids,
chat, message ids and album ids.

Each test asserts what a bot sent, what a turn received, or which record a
reply reached; never a key. The registry, post map and desk stores are small
stand-ins keyed by the full address, as their own modules key them; the
endpoint's part — which address it builds and which bot it names — is what
is pinned here.

It also pins one endpoint's identity lifecycle: before its first ``getMe`` it
has no bot id and behaves as an unready channel always has; the id ``getMe``
verifies is reported to the transport before the endpoint is ready.
"""

from __future__ import annotations

import asyncio
import itertools
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

import verdict_broker
from channels import DeliveryOutcome
from channels.tg_address import (
    CASA,
    DuplicateEndpoint,
    EndpointNotReady,
    TgAddress,
)
from two_endpoints import (  # noqa: F401 — the harness
    CASA_BOT,
    FINANCE_BOT,
    OPERATOR,
    build_two_endpoints,
    calls,
    feed_update,
)
from output_boundary_testing import admitted
from verdict_broker import VerdictBroker

pytestmark = pytest.mark.asyncio

ROLE_OF = {CASA_BOT: "assistant", FINANCE_BOT: "finance"}


# ---------------------------------------------------------------------------
# Fakes: a bot per endpoint, a bus, and the stores keyed by full address
# ---------------------------------------------------------------------------


def _fake_bot(bot_id: int) -> MagicMock:
    """A Bot API stand-in whose every call is recorded on the mock, so
    ``calls(ep)`` lists what went out through this bot."""
    bot = MagicMock(name=f"bot{bot_id}")
    bot.id = bot_id
    ids = itertools.count(1000)
    bot.send_message = AsyncMock(side_effect=lambda **kw: NS(message_id=next(ids)))
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_reply_markup = AsyncMock(return_value=True)
    bot.send_chat_action = AsyncMock(return_value=True)
    bot.delete_message = AsyncMock(return_value=True)
    return bot


class _Bus:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, msg) -> None:
        self.sent.append(msg)

    async def send_checked(self, msg) -> str:
        self.sent.append(msg)
        return "accepted"


class _PostMap:
    """``POST_MAP`` keyed by the message's full address, as the result
    broker keys it."""

    def __init__(self) -> None:
        self.entries: dict = {}

    def record(self, address: TgAddress, record) -> None:
        self.entries[address] = record

    def get(self, address: TgAddress):
        return self.entries.get(address)


class _Registry:
    """The engagement registry's post indexes, keyed by full address."""

    def __init__(self) -> None:
        self.records: dict = {}
        self.live: dict = {}
        self.ended: dict = {}
        self.filed: list = []

    def get(self, eid):
        return self.records.get(eid)

    def job_post_owner(self, address: TgAddress):
        return self.live.get(address)

    def ended_job_post_owner(self, address: TgAddress):
        return self.ended.get(address)

    def record_job_post(self, eid, address: TgAddress, *, keyboard=None) -> bool:
        self.filed.append((eid, address, keyboard))
        self.live[address] = self.records[eid]
        return True

    def counts_as_live(self, eid) -> bool:
        return self.records.get(eid) is not None


class _Driver:
    def __init__(self) -> None:
        self.submitted: list = []

    def submit_operator(self, rec, text, *, tg_message=None, notice=None):
        self.submitted.append((rec.id, text, tg_message, notice))


def _endpoint(bot_id: int, bus: _Bus, *, known: "int | None" = None):
    from channels.telegram import TelegramChannel
    bot = _fake_bot(bot_id)
    ep = TelegramChannel(
        bot_token=f"{bot_id}:token", chat_id=str(OPERATOR),
        default_agent=ROLE_OF.get(bot_id, "assistant"), bus=bus, bot=bot,
        is_casa=bot_id == CASA_BOT, known_bot_id=bot_id if known is None else known,
    )
    ep._app = MagicMock(bot=bot)
    return ep


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Fresh broker, post map and desk-store stand-ins; the per-chat lanes
    and memory side effects recorded rather than run."""
    import delegated_memory
    import result_broker
    import scheduled_asks
    monkeypatch.setattr(verdict_broker, "BROKER", VerdictBroker())
    monkeypatch.setattr(result_broker, "POST_MAP", _PostMap())
    lanes: list = []
    monkeypatch.setattr(scheduled_asks, "cancel_non_scheduled_for_chat",
                        lambda bot, chat, reason: lanes.append((bot, chat)) or 0)
    monkeypatch.setattr(delegated_memory, "capture_post_line", lambda **kw: None)
    monkeypatch.setattr(delegated_memory, "capture_job_reply", lambda **kw: None)
    return lanes


@pytest.fixture
def pair():
    """Real endpoints for bots 111 and 222 over one transport and one bus."""
    bus = _Bus()
    casa, finance = _endpoint(CASA_BOT, bus), _endpoint(FINANCE_BOT, bus)
    h = build_two_endpoints(casa=casa, finance=finance)
    h.bus = bus
    return h


def _user(uid: int = OPERATOR):
    return NS(id=uid, first_name="Op", is_bot=False)


def _text_update(text: str, *, message_id: int = 5, reply_to=None):
    user = _user()
    msg = NS(message_id=message_id, text=text, chat=NS(id=OPERATOR),
             from_user=user, reply_to_message=reply_to, caption=None,
             sender_chat=None, date=None)
    return NS(message=msg, effective_chat=NS(id=OPERATOR), effective_user=user)


def _quoted(message_id: int, bot_id: int, text: str = "posted"):
    return NS(message_id=message_id, text=text, caption=None, date=None,
              from_user=NS(id=bot_id, is_bot=True), sender_chat=None)


def _turns(bus: _Bus) -> list:
    return [m for m in bus.sent if getattr(m, "context", None) is not None]


# ---------------------------------------------------------------------------
# updates are numbered per bot
# ---------------------------------------------------------------------------


async def test_equal_update_ids_on_two_bots_are_both_taken_and_a_replay_is_not(
        pair, monkeypatch):
    import channels.telegram as tg
    monkeypatch.setattr(tg, "Update", NS(de_json=lambda payload, bot: NS(
        update_id=payload["update_id"])))
    for ep in (pair.casa, pair.finance):
        ep._app.update_queue = asyncio.Queue()

    assert await feed_update(pair.casa, 71, 5, "hi") == "accepted"
    assert await feed_update(pair.finance, 71, 5, "hi") == "accepted"
    assert await feed_update(pair.casa, 71, 5, "hi") == "duplicate"
    assert pair.casa._app.update_queue.qsize() == 1
    assert pair.finance._app.update_queue.qsize() == 1


# ---------------------------------------------------------------------------
# every turn an endpoint builds carries the bot it arrived on
# ---------------------------------------------------------------------------


async def _dm(ep, h):
    await ep._handle(_text_update("hello"), None)
    return _turns(h.bus)[-1].context


async def _caption(ep, h):
    update = _text_update("file caption")
    await ep._caption_turn(update, update.message, update.effective_user,
                           "file caption", [("a.pdf", "PDF", "1 KB", "/x/a.pdf")],
                           album=False)
    return _turns(h.bus)[-1].context


async def _button(ep, h):
    assert await ep._dispatch_button_continuation(
        chat_id=OPERATOR, user_id=OPERATOR, target_role=ep.default_agent,
        request_id="rid", text="[answer]")
    return _turns(h.bus)[-1].context


async def _scheduled(ep, h):
    assert await ep._dispatch_scheduled_continuation(
        session_scope="scheduled:x", target_role=ep.default_agent,
        request_id="rid", text="[answer]")
    return _turns(h.bus)[-1].context


async def _desk(ep, h):
    """The desk turn is built by the desk from the endpoint's bot, handed
    over at the spawn."""
    import specialist_desk
    seen: dict = {}

    class _Desk:
        faulted = False

        def reserve(self):
            return NS(release=lambda *a: None)

    class _Desks:
        def get_or_create(self, bot, chat, role):
            seen["desk_key"] = (bot, chat, role)
            return _Desk()

    async def handle_reply(**kw):
        seen.update(kw)

    h.monkeypatch.setattr(specialist_desk, "DESKS", _Desks())
    h.monkeypatch.setattr(specialist_desk, "desk_target_ok", lambda *a: True)
    h.monkeypatch.setattr(specialist_desk, "handle_reply", handle_reply)
    assert await ep._dispatch_desk_continuation(
        chat_id=OPERATOR, user_id=OPERATOR, desk_role="finance",
        request_id="rid", text="[approved]")
    await asyncio.gather(*ep._turn_tasks)
    assert seen["desk_key"] == (ep.bot_id, OPERATOR, "finance")
    return {"_tg_bot": seen["bot_id"]}


async def _job_reply(ep, h):
    """A reply into a job: the job's memory key and its notice name the bot
    that received the message."""
    driver = _Driver()
    reg = _Registry()
    rec = NS(id="e" * 32, origin={"user_id": OPERATOR, "chat_id": OPERATOR},
             status="active", role_or_type="finance")
    reg.records[rec.id] = rec
    ep._engagement_registry, ep._engagement_driver = reg, driver
    assert await ep.deliver_operator_reply(rec, "go", chat_id=OPERATOR, message_id=9)
    (_eid, _text, tg_message, notice), = driver.submitted
    assert tg_message == TgAddress(ep.bot_id, OPERATOR, 9)
    assert notice == {"address": [ep.bot_id, OPERATOR, 9], "answer": False}
    return {"_tg_bot": tg_message.bot_id}


_BUILDERS = {"dm": _dm, "caption": _caption, "button": _button,
             "scheduled_ask": _scheduled, "desk": _desk, "job_reply": _job_reply}


@pytest.mark.parametrize("builder", sorted(_BUILDERS))
@pytest.mark.parametrize("bot", [CASA_BOT, FINANCE_BOT])
async def test_every_turn_an_endpoint_builds_carries_the_bot_it_arrived_on(
        pair, monkeypatch, builder, bot):
    pair.monkeypatch = monkeypatch
    ep = pair.ep[bot]
    context = await _BUILDERS[builder](ep, pair)
    assert context["_tg_bot"] == bot


async def test_a_typed_message_ends_only_its_own_bots_pending_question(
        pair, _isolated):
    await pair.finance._handle(_text_update("an answer"), None)
    assert _isolated == [(FINANCE_BOT, OPERATOR)]


# ---------------------------------------------------------------------------
# a reply leaves through the bot its turn arrived on
# ---------------------------------------------------------------------------


async def test_a_turn_on_222_is_answered_through_222_only(pair):
    context = await _dm(pair.finance, pair)
    before = len(calls(pair.casa))
    on_token = pair.transport.create_on_token(context)
    await on_token("partial")
    out = await pair.transport.send_response(admitted("the answer"), context)
    await pair.transport.turn_finished(context)
    assert out is DeliveryOutcome.DELIVERED
    assert len(calls(pair.casa)) == before == 0
    sent = [kw for name, kw in calls(pair.finance) if name == "send_message"]
    assert [kw["text"] for kw in sent] == ["partial", "the answer"]


# ---------------------------------------------------------------------------
# equal message ids on two bots are two different messages
# ---------------------------------------------------------------------------


def _job(eid: str = "j" * 32, *, status: str = "active"):
    # the job was born delivering to the operator's chat on bot 111
    return NS(id=eid, origin={"user_id": OPERATOR, "chat_id": OPERATOR,
                              "job": {"title": "Accounting check"}},
              status=status, role_or_type="finance", completed_at=None,
              delivery=TgAddress(CASA_BOT, OPERATOR), ui_posts=[], questions={})


def _indexed(monkeypatch, reg: _Registry) -> None:
    """The task-post index answers from *reg*'s job posts, as the real index
    answers from the engagement registry."""
    from task_posts_double import install
    install(monkeypatch).regs.append(reg)


async def test_a_reply_on_222_to_message_5_never_reaches_111s_live_job(pair, monkeypatch):
    reg, driver = _Registry(), _Driver()
    _indexed(monkeypatch, reg)
    rec = _job()
    reg.records[rec.id] = rec
    for ep in (pair.casa, pair.finance):
        ep._engagement_registry, ep._engagement_driver = reg, driver
    # The job's question landed on bot 111 as message 1000 there.
    mid = await pair.casa.post_job_question(
        rec, "q1", admitted("Which account?"), ["A"], ["A"])
    assert reg.filed == [(rec.id, TgAddress(CASA_BOT, OPERATOR, mid), "question")]

    await pair.finance._handle(_text_update(
        "B, please", message_id=40, reply_to=_quoted(mid, FINANCE_BOT)), None)
    assert driver.submitted == []
    assert [m.target for m in _turns(pair.bus)] == ["finance"]

    await pair.casa._handle(_text_update(
        "A, please", message_id=41, reply_to=_quoted(mid, CASA_BOT)), None)
    assert [(eid, msg.bot_id) for eid, _t, msg, _n in driver.submitted] == [
        (rec.id, CASA_BOT)]


async def test_a_reply_on_222_to_message_5_is_not_named_as_111s_ended_job(pair, monkeypatch):
    reg = _Registry()
    _indexed(monkeypatch, reg)
    rec = _job(status="completed")
    reg.records[rec.id] = rec
    reg.ended[TgAddress(CASA_BOT, OPERATOR, 5)] = rec
    for ep in (pair.casa, pair.finance):
        ep._engagement_registry = reg

    on_222 = await _dm_reply(pair.finance, pair, 5, FINANCE_BOT)
    on_111 = await _dm_reply(pair.casa, pair, 5, CASA_BOT)
    assert "background job" not in on_222
    assert "background job «Accounting check»" in on_111


async def _dm_reply(ep, h, mid: int, poster: int) -> str:
    await ep._handle(_text_update("about that", message_id=50,
                                  reply_to=_quoted(mid, poster)), None)
    return _turns(h.bus)[-1].context["_reply_note"]


async def test_a_post_filed_on_111_is_not_222s_message_with_the_same_id(pair):
    import result_broker
    # a specialist neither endpoint's chat belongs to: either bot would name it
    post = result_broker.PostRecord(
        role="tax", operator_id=OPERATOR, plugin="p", slot="s",
        tool_use_id="t", owner="", posted_at=0.0)
    pair.casa._record_post(OPERATOR, NS(message_id=5), post)
    assert result_broker.POST_MAP.entries == {
        TgAddress(CASA_BOT, OPERATOR, 5): post}

    on_222 = await _dm_reply(pair.finance, pair, 5, FINANCE_BOT)
    on_111 = await _dm_reply(pair.casa, pair, 5, CASA_BOT)
    assert "an earlier message from" not in on_222
    assert "an earlier message from" in on_111
    assert pair.finance._file_address(
        OPERATOR, _user(), NS(reply_to_message=_quoted(5, FINANCE_BOT))) is None
    assert pair.casa._file_address(
        OPERATOR, _user(), NS(reply_to_message=_quoted(5, CASA_BOT))) == (
        "reply", post, "tax")


# ---------------------------------------------------------------------------
# files — albums and armings are per bot
# ---------------------------------------------------------------------------


def _file_msg(mid: int, album: str):
    fields = dict(document=NS(file_id=f"f{mid}", file_name=f"{mid}.pdf",
                              file_size=10), photo=None, caption=None)
    kinds = ("sticker", "video", "video_note", "voice", "audio", "animation",
             "contact", "location", "venue", "poll", "dice", "game", "story",
             "web_app_data", "invoice", "pinned_message", "new_chat_title",
             "new_chat_photo", "delete_chat_photo",
             "message_auto_delete_timer_changed", "new_chat_members",
             "left_chat_member", "write_access_allowed")
    fields.update({k: None for k in kinds})
    return NS(message_id=mid, chat=NS(id=OPERATOR), from_user=_user(),
              media_group_id=album, reply_to_message=None,
              message_thread_id=None, **fields)


async def test_equal_album_ids_on_two_bots_make_two_acknowledgements(pair, monkeypatch):
    import agent_inbox
    monkeypatch.setattr(agent_inbox, "get_inbox", lambda role: None)
    await pair.casa._on_non_text_message(NS(message=_file_msg(5, "album-1")))
    await pair.finance._on_non_text_message(NS(message=_file_msg(5, "album-1")))
    for ep in (pair.casa, pair.finance):
        assert [name for name, _ in calls(ep)] == ["send_message"]


async def test_an_arming_on_111_does_not_take_a_file_sent_to_222(pair):
    import time
    pair.casa._armings[OPERATOR] = {
        "chat_id": OPERATOR, "operator_id": OPERATOR, "role": "finance",
        "artifact_id": "a", "expires_at": time.monotonic() + 60}
    msg = NS(reply_to_message=None)
    assert pair.finance._file_address(OPERATOR, _user(), msg) is None
    assert pair.casa._file_address(OPERATOR, _user(), msg) == ("armed", None, "finance")


# ---------------------------------------------------------------------------
# a tap resolves only on the endpoint that posted the keyboard
# ---------------------------------------------------------------------------


def _tap(data: str, mid: int = 77):
    cq = NS(data=data, message=NS(chat=NS(id=OPERATOR), message_id=mid),
            from_user=_user(), answer=AsyncMock())
    return NS(callback_query=cq)


def _seed(kind: str, ns: str, scope_bot: int, meta_bot: int, rid: str):
    broker = verdict_broker.BROKER
    scope = f"{kind}:{scope_bot}:{OPERATOR}"
    req, _ = broker.register(namespace=ns, scope=scope, request_id=rid,
                             timeout_s=30.0)
    req.meta.update({"options": ["Yes", "No"], "chat_id": OPERATOR,
                     "bot_id": meta_bot, "operator_id": OPERATOR,
                     "_scope": scope, "message_id": 77,
                     "deadline": asyncio.get_running_loop().time() + 30})
    return broker, scope


@pytest.mark.parametrize("kind", ["dm", "authz", "ask"])
async def test_a_question_naming_bot_111_is_refused_on_222(pair, kind):
    broker, scope = _seed(kind, "resident_ask", FINANCE_BOT, CASA_BOT, "rid-1")
    update = _tap("v1|resident_ask|rid-1|0")
    await pair.finance._on_inline_callback(update, None)
    update.callback_query.answer.assert_awaited_once_with("expired")
    assert broker.pending(namespace="resident_ask", scope=scope) == ["rid-1"]

    broker, scope = _seed(kind, "resident_ask", FINANCE_BOT, FINANCE_BOT, "rid-2")
    update = _tap("v1|resident_ask|rid-2|0")
    await pair.finance._on_inline_callback(update, None)
    update.callback_query.answer.assert_awaited_once_with("✔")


async def test_a_proposal_naming_bot_111_is_refused_on_222(pair):
    broker, scope = _seed("proposal", "proposal", FINANCE_BOT, CASA_BOT, "rid-p")
    update = _tap("v1|proposal|rid-p|0")
    await pair.finance._on_inline_callback(update, None)
    update.callback_query.answer.assert_awaited_once_with("expired")
    assert broker.pending(namespace="proposal", scope=scope) == ["rid-p"]

    broker, scope = _seed("proposal", "proposal", FINANCE_BOT, FINANCE_BOT, "rid-q")
    update = _tap("v1|proposal|rid-q|0")
    await pair.finance._on_inline_callback(update, None)
    update.callback_query.answer.assert_awaited_once_with("✔")


async def test_a_tap_on_111_looks_only_in_111s_lanes(pair):
    broker, _scope = _seed("dm", "resident_ask", FINANCE_BOT, FINANCE_BOT, "rid-3")
    update = _tap("v1|resident_ask|rid-3|0")
    await pair.casa._on_inline_callback(update, None)
    update.callback_query.answer.assert_awaited_once_with("expired")


# ---------------------------------------------------------------------------
# Keyboards are edited only through the bot that posted them
# ---------------------------------------------------------------------------


async def test_an_ended_jobs_keyboards_are_retired_through_their_own_bots(pair):
    from engagement_registry import JOB_POST_QUESTION, JOB_POST_START
    rec = _job(status="completed")
    posts = [(TgAddress(CASA_BOT, OPERATOR, 7), JOB_POST_START),
             [FINANCE_BOT, OPERATOR, 7, JOB_POST_QUESTION]]
    await pair.casa._retire_job_posts(rec, posts)
    await pair.finance._retire_job_posts(rec, posts)
    assert [n for n, _ in calls(pair.casa)] == ["edit_message_text"]
    assert [n for n, _ in calls(pair.finance)] == ["edit_message_reply_markup"]
    (_, kw), = calls(pair.finance)
    assert (kw["chat_id"], kw["message_id"]) == (OPERATOR, 7)


async def test_a_lost_message_notice_is_never_sent_through_another_bot(pair):
    from channels.telegram import _notice_dict, _notice_target_from, _NoticeTarget
    rec = _job()
    entry = {"notice": _notice_dict(_NoticeTarget(
        address=TgAddress(CASA_BOT, OPERATOR, 9)))}
    assert _notice_target_from(entry["notice"]).address == TgAddress(
        CASA_BOT, OPERATOR, 9)
    await pair.finance.tell_journal_lost(rec, entry)
    assert calls(pair.finance) == []
    await pair.casa.tell_journal_lost(rec, entry)
    (name, kw), = calls(pair.casa)
    assert name == "send_message" and kw["reply_parameters"].message_id == 9


# ---------------------------------------------------------------------------
# One endpoint's identity: unknown until getMe, reported before it is ready
# ---------------------------------------------------------------------------


def _fake_application(bot_id: int):
    app = MagicMock()
    for name in ("initialize", "start", "stop", "shutdown"):
        setattr(app, name, AsyncMock())
    app.bot = _fake_bot(bot_id)
    app.bot.set_webhook = AsyncMock()
    app.bot.delete_webhook = AsyncMock()
    app.updater = MagicMock(start_polling=AsyncMock(), stop=AsyncMock())
    return app


@pytest.fixture
def built(monkeypatch):
    from telegram.ext import Application
    apps: list = []

    def builder():
        chain = MagicMock()
        chain.token = MagicMock(return_value=chain)

        def build():
            apps.append(_fake_application(333))
            return apps[-1]
        chain.build = build
        return chain
    monkeypatch.setattr(Application, "builder", builder)
    return apps


def _unknown_endpoint(**kw):
    from channels.telegram import TelegramChannel
    return TelegramChannel(bot_token="333:t", chat_id=str(OPERATOR),
                           default_agent="assistant", **kw)


async def test_an_endpoint_with_no_id_yet_behaves_as_an_unready_channel():
    ep = _unknown_endpoint()
    assert ep.has_bot_id is False
    with pytest.raises(EndpointNotReady):
        ep.bot_id
    ctx = {"chat_id": OPERATOR, "cid": "c1", "_tg_bot": CASA}
    assert await ep.send("hi", ctx) is DeliveryOutcome.NOT_DELIVERED
    with pytest.raises(RuntimeError, match="not started"):
        await ep.send_media(b"x", "document", "a.pdf", ctx)
    on_token = ep.create_on_token(ctx)
    assert callable(on_token)
    await on_token("partial")       # raises nothing


async def test_getme_reports_the_id_before_the_endpoint_is_ready(built):
    from telegram_channel_double import make_transport
    from channels.telegram_transport import telegram_transport
    ep = _unknown_endpoint(
        webhook_url="https://casa.example",
        webhook_secret_for=lambda bot: f"secret-{bot}")
    transport = telegram_transport(make_transport(ep))
    assert transport.casa_route() == CASA
    observe = ep.on_identity
    seen: list = []

    def spy(bot_id):
        seen.append((bot_id, ep.is_ready))
        observe(bot_id)
    ep.on_identity = spy

    await ep._rebuild()

    assert seen == [(333, False)]
    assert ep.is_ready and ep.bot_id == 333
    assert transport.casa_route() == 333 and transport.endpoint(333) is ep
    built[0].bot.set_webhook.assert_awaited_once_with(
        url="https://casa.example/telegram/update/333", secret_token="secret-333",
        allowed_updates=list(ep.allowed_updates))


async def test_a_bot_another_endpoint_serves_stops_this_one_for_good(built):
    ep = _unknown_endpoint()

    def refuse(bot_id):
        raise DuplicateEndpoint(bot_id)
    ep.on_identity = refuse

    await ep._rebuild()
    assert not ep.is_ready and ep.has_bot_id is False
    assert built[0].shutdown.await_count == 1
    await ep._rebuild()             # a reconnect never revives it
    assert len(built) == 1


async def test_getme_without_an_id_fails_the_bring_up(monkeypatch):
    from telegram.ext import Application
    app = _fake_application(333)
    app.bot.id = None
    chain = MagicMock()
    chain.token = MagicMock(return_value=chain)
    chain.build = MagicMock(return_value=app)
    monkeypatch.setattr(Application, "builder", lambda: chain)
    ep = _unknown_endpoint()
    with pytest.raises(RuntimeError, match="no bot id"):
        await ep._rebuild()
    assert not ep.is_ready and ep.has_bot_id is False
