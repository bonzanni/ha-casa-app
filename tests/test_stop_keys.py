"""The Stop key at the keyboard (``stop_keys``): one red reply-keyboard key per piece of
stoppable work live in the operator's chat with a bot. Every test drives the real channel
against a recording bot and asserts what was sent: the method, the text, the reply
keyboard's keys and the counts."""
from __future__ import annotations

import asyncio
import time
import types
from unittest.mock import AsyncMock

import pytest

import chat_notes
import plugin_bindings
import plugin_continuations as pc
import plugin_stop
import run_admission
import stop_keys
import tools as tools_mod
from authz_grants import GrantIdentity
from bus import MessageBus
from channels.tg_address import TgAddress
from channels.telegram import TelegramChannel
from config import AgentConfig, CharacterConfig, MemoryConfig
from engagement_registry import JOB_POST_START, EngagementRegistry
from telegram_channel_double import make_transport
from two_endpoints import CASA_BOT

try:
    from tests.role_artifact_stub import STUB_ROLE_ARTIFACT
except ImportError:
    from role_artifact_stub import STUB_ROLE_ARTIFACT

OPERATOR = 42
BOT = CASA_BOT
ARTIFACT = "1" * 64
START_MID = 501
CARD_MID = 777
CARD_TEXT = "📊 Fido\nOn it for Ellen:\n• RunPod — invoice"
ORIGIN = {"role": "assistant", "channel": "telegram", "chat_id": OPERATOR,
          "user_id": OPERATOR, "user_name": "N", "_origin_route": "telegram",
          "_origin_clearance": "private", "user_text": "get my invoice", "_operator_turn": True,
          "_tg_bot": BOT}


def _cfg(role, name, kind="specialist"):
    return AgentConfig(role_artifact=STUB_ROLE_ARTIFACT, role=role, model="x",
                       character=CharacterConfig(name=name), system_prompt="x",
                       memory=MemoryConfig(token_budget=0), kind=kind)


class _Bot:
    """The Bot API calls the keys make, recorded."""

    def __init__(self):
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.deleted: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=900 + len(self.sent))

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)
        return True

    async def edit_message_reply_markup(self, **kwargs):
        return True

    async def delete_message(self, **kwargs):
        self.deleted.append(kwargs)
        return True


def keys(markup) -> list:
    """A reply keyboard's keys as (text, style), row by row; ``"remove"`` for its removal."""
    if getattr(markup, "remove_keyboard", False):
        return "remove"
    assert markup.is_persistent is True and markup.resize_keyboard is True
    return [[(k.text, k.style) for k in row] for row in markup.keyboard]


@pytest.fixture
async def rig(monkeypatch, tmp_path):
    import authz_grants
    stop_keys.reset()
    chat_notes.reset()
    monkeypatch.setattr(authz_grants, "_live_operator_identity", lambda: (OPERATOR, OPERATOR))
    monkeypatch.setattr(tools_mod, "_agent_role_map", {
        "assistant": _cfg("assistant", "Ellen", "resident"), "browser": _cfg("browser", "Fido"),
        "finance": _cfg("finance", "Alex")}, raising=False)
    monkeypatch.setattr(plugin_bindings, "BINDINGS",
                        plugin_bindings.BindingStore(str(tmp_path / "b.json")))
    monkeypatch.setattr(run_admission, "CONFIRMATIONS",
                        run_admission.Confirmations(str(tmp_path / "admissions.json")))
    monkeypatch.setattr(pc, "CONTINUATIONS", pc.Continuations())
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"))
    monkeypatch.setattr(tools_mod, "_engagement_registry", reg, raising=False)
    bus = MessageBus()

    async def _noop(*a, **k):
        return None
    bus.register("assistant", _noop)
    ch = TelegramChannel(bot_token="T", chat_id=str(OPERATOR), default_agent="assistant",
                         bus=bus, is_casa=True, known_bot_id=BOT)
    ch._start_typing = lambda *a, **k: None
    ch._app = types.SimpleNamespace(bot=_Bot())
    ch._session_registry = None
    ch._engagement_registry = reg
    ch._dispatch_dm_turn = AsyncMock(return_value=True)
    cancels: list = []

    async def _finalize_cancel(rec, reason="user"):
        cancels.append((rec.id, reason))
        return True
    ch._finalize_cancel = _finalize_cancel
    monkeypatch.setattr(tools_mod, "_channel_manager", make_transport(ch), raising=False)
    stops: list = []

    async def _call_stop(rp, tool, args):
        stops.append(args)
        return {"outcome": "stopped", "refs": []}

    async def _stop_plugins():
        return [("browser", None, types.SimpleNamespace(artifact_id=ARTIFACT), "browser",
                 "stop_runs")]
    monkeypatch.setattr(plugin_stop, "call_stop", _call_stop)
    monkeypatch.setattr(plugin_stop, "stop_plugins", _stop_plugins)

    async def job(title="Accounting check", *, filed=True, mid=START_MID):
        rec = await reg.create(
            "specialist", "finance", "in_casa", "t",
            {**ORIGIN, "job": {"title": title, "started_by": "operator"}},
            executor="finance", delivery=TgAddress(BOT, OPERATOR))
        if filed:
            assert reg.record_job_post(rec.id, TgAddress(BOT, OPERATOR, mid),
                                       keyboard=JOB_POST_START)
        return rec

    def run(cid="conf-" + "c" * 32, *, key="k1", card_mid=CARD_MID, text=CARD_TEXT):
        ident = GrantIdentity(operator_id=OPERATOR, chat_id=OPERATOR, enforcement_role="browser",
                              artifact_id=ARTIFACT, engagement_id="", bot_id=BOT,
                              delivery=TgAddress(BOT, OPERATOR))
        b = plugin_bindings.BINDINGS.for_turn(plugin="browser", turn_key="d-" + cid,
                                              identity=ident, chain=("assistant", "browser"),
                                              origin=ORIGIN)
        c = run_admission.Confirmation(
            cid=cid, plugin="browser", bot_id=BOT, operator_id=OPERATOR, binding_ref=b.ref,
            request_key=key, status="admitted", admissions=[], created_at=time.time(),
            card=[BOT, OPERATOR, card_mid] if card_mid else None, card_text=text)
        run_admission.CONFIRMATIONS.add(c)
        return c

    yield types.SimpleNamespace(ch=ch, bot=ch._app.bot, reg=reg, job=job, run=run,
                                cancels=cancels, stops=stops)
    stop_keys.reset()


def _message(text, *, user=OPERATOR, mid=990):
    msg = types.SimpleNamespace(text=text, message_id=mid, reply_to_message=None,
                                chat=types.SimpleNamespace(id=OPERATOR))
    return types.SimpleNamespace(message=msg, effective_user=types.SimpleNamespace(id=user),
                                 effective_chat=types.SimpleNamespace(id=OPERATOR))


async def _press(rig, text, **kw):
    await rig.ch._handle_serialized(_message(text, **kw), str(OPERATOR))
    for _ in range(100):
        busy = [t for t in rig.ch._turn_tasks if not t.done()]
        if not busy:
            return
        await asyncio.wait(busy, timeout=0.1)


# -- the keyboard on each start message ----------------------------------------------------

async def test_a_job_start_line_carries_no_button_and_raises_its_key(rig):
    rec = await rig.job(filed=False)
    mid = await rig.ch.post_job_start_line(rec)
    (sent,) = rig.bot.sent
    assert sent["disable_notification"] is True
    assert sent["text"] == "📊 Alex started «Accounting check»."
    assert keys(sent["reply_markup"]) == [[("⏹ Stop «Accounting check»", "danger")]]
    assert rec.ui_posts == [(TgAddress(BOT, OPERATOR, mid), JOB_POST_START)]


async def test_a_run_status_card_raises_its_key_beside_a_live_jobs(rig):
    await rig.job()
    c = rig.run(card_mid=None, text="")
    mid = await rig.ch.deliver_run_card(OPERATOR, CARD_TEXT,
                                        starting=stop_keys.run_item(c, CARD_TEXT))
    assert mid == 901
    (sent,) = rig.bot.sent
    assert keys(sent["reply_markup"]) == [[("⏹ Stop «Accounting check»", "danger")],
                                          [("⏹ Stop «RunPod»", "danger")]]


async def test_the_started_line_after_his_start_tap_raises_the_runs_key(rig):
    rig.run()
    assert await plugin_stop.post_line(BOT, OPERATOR, "browser", "Started.",
                                       reply_to=CARD_MID)
    (sent,) = rig.bot.sent
    assert (sent["text"], sent["reply_to_message_id"]) == ("📊 Fido\nStarted.", CARD_MID)
    assert sent["disable_notification"] is True
    assert keys(sent["reply_markup"]) == [[("⏹ Stop «RunPod»", "danger")]]


def _at(rig, rec, clock: str):
    """Set the job's start to today's *clock* (HH:MM:SS) in Casa's zone."""
    import datetime
    import timekeeping
    h, m, sec = (int(x) for x in clock.split(":"))
    when = datetime.datetime.now(timekeeping.resolve_tz()).replace(
        hour=h, minute=m, second=sec, microsecond=0)
    rig.reg.get(rec.id).started_at = when.timestamp()


async def test_items_with_one_title_are_told_apart_by_when_they_started(rig):
    first = await rig.job()
    second = await rig.job(mid=502)
    third = await rig.job(mid=503)
    _at(rig, first, "19:42:05")
    _at(rig, second, "19:50:10")
    _at(rig, third, "19:50:40")
    assert keys(stop_keys.keyboard(BOT, OPERATOR)) == [
        [("⏹ Stop «Accounting check» · 19:42", "danger")],
        [("⏹ Stop «Accounting check» · 19:50:10", "danger")],
        [("⏹ Stop «Accounting check» · 19:50:40", "danger")]]
    # alone again, a title is the whole label
    await rig.reg.try_transition_terminal(first.id, "completed", strict=True)
    await rig.reg.try_transition_terminal(third.id, "completed", strict=True)
    assert keys(stop_keys.keyboard(BOT, OPERATOR)) == [
        [("⏹ Stop «Accounting check»", "danger")]]


async def test_after_a_restart_a_shown_key_still_stops_its_own_item(rig):
    """Labels come from the records alone: with one same-titled item ended and another
    started since, the key a chat was last sent names the same item after a restart."""
    a = await rig.job()
    b = await rig.job(mid=502)
    _at(rig, a, "19:42:00")
    _at(rig, b, "19:50:00")
    await rig.reg.try_transition_terminal(a.id, "completed", strict=True)
    c = await rig.job(mid=503)
    _at(rig, c, "19:58:00")
    shown = keys(stop_keys.keyboard(BOT, OPERATOR))      # what his phone shows
    assert shown == [[("⏹ Stop «Accounting check» · 19:50", "danger")],
                     [("⏹ Stop «Accounting check» · 19:58", "danger")]]
    stop_keys.reset()                                    # a restart
    await _press(rig, "⏹ Stop «Accounting check» · 19:50")
    assert rig.cancels == [(b.id, "the operator")]


async def test_a_press_made_as_a_reply_to_a_live_jobs_post_is_still_the_key(rig):
    rec = await rig.job()
    update = _message("⏹ Stop «Accounting check»")
    update.message.reply_to_message = types.SimpleNamespace(
        message_id=START_MID, chat=types.SimpleNamespace(id=OPERATOR),
        from_user=types.SimpleNamespace(id=BOT, is_bot=True))
    rig.ch._quoted_task = lambda *a, **k: (object(), None)     # a reply into a live task
    rig.ch._route_task_reply = AsyncMock(return_value="taken")
    await rig.ch._handle_serialized(update, str(OPERATOR))
    for _ in range(100):
        busy = [t for t in rig.ch._turn_tasks if not t.done()]
        if not busy:
            break
        await asyncio.wait(busy, timeout=0.1)
    assert rig.cancels == [(rec.id, "the operator")]
    rig.ch._route_task_reply.assert_not_awaited()
    rig.ch._dispatch_dm_turn.assert_not_awaited()


async def test_a_job_on_another_bot_or_without_a_start_line_has_no_key(rig):
    await rig.job(filed=False)
    other = await rig.job("Elsewhere", filed=False)
    rig.reg.record_job_post(other.id, TgAddress(222, OPERATOR, 7), keyboard=JOB_POST_START)
    assert keys(stop_keys.keyboard(BOT, OPERATOR)) == "remove"


async def test_before_the_bot_has_an_id_nothing_that_carries_the_keys_is_posted(rig):
    rec = await rig.job(filed=False)
    rig.ch._known_bot_id = None
    assert not rig.ch.has_bot_id
    assert await rig.ch.post_job_start_line(rec) is None
    assert await rig.ch.deliver_run_card(OPERATOR, CARD_TEXT) is None
    assert await rig.ch.send_reply_line(OPERATOR, "Stopped.", CARD_MID) is False
    assert await rig.ch._send_key_line(OPERATOR, "Finished «x».") is False
    assert rig.bot.sent == []


# -- a press -----------------------------------------------------------------------------

async def test_a_press_cancels_the_job_through_its_funnel_and_reaches_no_agent(rig):
    rec = await rig.job()
    await _press(rig, "⏹ Stop «Accounting check»")
    assert rig.cancels == [(rec.id, "the operator")]
    assert rig.ch._job_cancels == set()
    rig.ch._dispatch_dm_turn.assert_not_awaited()
    assert rig.bot.deleted == [{"chat_id": OPERATOR, "message_id": 990}]
    assert rig.bot.sent == []           # the job's end line is its terminal observer's


async def test_a_press_while_its_cancel_runs_cancels_once(rig):
    rec = await rig.job()
    rig.ch._job_cancels.add(rec.id)
    await _press(rig, "⏹ Stop «Accounting check»")
    assert rig.cancels == []
    assert len(rig.bot.deleted) == 1
    rig.ch._dispatch_dm_turn.assert_not_awaited()


async def test_a_press_stops_the_run_through_plugin_stop_and_takes_its_key(rig):
    await rig.job()
    c = rig.run()
    await _press(rig, "⏹ Stop «RunPod»")
    assert rig.stops == [{"ref": "request:k1"}]
    rig.ch._dispatch_dm_turn.assert_not_awaited()
    assert len(rig.bot.deleted) == 1
    (edit,) = rig.bot.edited
    assert (edit["message_id"], edit["text"]) == (CARD_MID, f"{CARD_TEXT}\n☑ Stopped")
    (line,) = rig.bot.sent
    assert (line["text"], line["reply_to_message_id"]) == ("📊 Fido\nStopped.", CARD_MID)
    # the line carries what is still live: the job's key, not the run's
    assert keys(line["reply_markup"]) == [[("⏹ Stop «Accounting check»", "danger")]]
    assert run_admission.CONFIRMATIONS.peek(c.cid).card is None


@pytest.mark.parametrize("case", ["another_sender", "another_text", "a_prefix"])
async def test_a_message_that_is_not_his_key_takes_its_ordinary_path(rig, case):
    await rig.job()
    text = {"another_sender": "⏹ Stop «Accounting check»",
            "another_text": "please stop the accounting check",
            "a_prefix": "⏹ Stop «Accounting"}[case]
    await _press(rig, text, user=7 if case == "another_sender" else OPERATOR)
    assert rig.cancels == [] and rig.stops == [] and rig.bot.deleted == []
    rig.ch._dispatch_dm_turn.assert_awaited_once()


async def test_a_key_whose_work_is_gone_is_answered_with_the_keys_still_live(rig):
    first = await rig.job()
    await rig.job("Bank sync", mid=502)
    stop_keys.keyboard(BOT, OPERATOR)                    # both keys shown
    await rig.reg.try_transition_terminal(first.id, "completed", strict=True)
    rig.reg.take_ui_posts(first.id)
    await _press(rig, "⏹ Stop «Accounting check»")
    assert rig.cancels == [] and len(rig.bot.deleted) == 1
    rig.ch._dispatch_dm_turn.assert_not_awaited()
    (line,) = rig.bot.sent
    assert line["text"] == "It had already ended."
    assert keys(line["reply_markup"]) == [[("⏹ Stop «Bank sync»", "danger")]]


async def test_after_a_restart_a_stale_key_is_an_ordinary_message(rig):
    first = await rig.job()
    stop_keys.keyboard(BOT, OPERATOR)
    await rig.reg.try_transition_terminal(first.id, "completed", strict=True)
    stop_keys.reset()                                    # a restart empties the memory
    await _press(rig, "⏹ Stop «Accounting check»")
    assert rig.bot.sent == [] and rig.bot.deleted == []
    rig.ch._dispatch_dm_turn.assert_awaited_once()


# -- the end lines -----------------------------------------------------------------------

@pytest.mark.parametrize("status,line", [
    ("completed", "Finished «Accounting check»."),
    ("cancelled", "Stopped «Accounting check»."),
    ("error", "«Accounting check» failed."),
])
async def test_a_jobs_end_line_carries_the_keys_still_live(rig, status, line):
    rec = await rig.job()
    rig.run()
    await rig.reg.try_transition_terminal(rec.id, status, strict=True)
    posts = rig.reg.take_ui_posts(rec.id)
    await rig.ch._retire_job_posts(rec, posts)
    (edit,) = rig.bot.edited
    assert edit["message_id"] == START_MID
    (sent,) = rig.bot.sent
    assert sent["text"] == line and sent["disable_notification"] is True
    assert sent["reply_parameters"].message_id == START_MID
    assert keys(sent["reply_markup"]) == [[("⏹ Stop «RunPod»", "danger")]]


async def test_the_last_end_line_removes_the_keyboard(rig):
    rec = await rig.job()
    await rig.reg.try_transition_terminal(rec.id, "completed", strict=True)
    await rig.ch._retire_job_posts(rec, rig.reg.take_ui_posts(rec.id))
    (sent,) = rig.bot.sent
    assert keys(sent["reply_markup"]) == "remove"


async def test_stop_work_posts_a_silent_stopped_line_that_takes_the_key(rig):
    rig.run()
    import agent as agent_mod
    token = agent_mod.origin_var.set(dict(ORIGIN))
    try:
        out = await tools_mod.stop_work.handler({"ref": "request:k1"})
    finally:
        agent_mod.origin_var.reset(token)
    assert "Stopped" in out["content"][0]["text"]
    (line,) = rig.bot.sent
    assert (line["text"], line["reply_to_message_id"]) == ("📊 Fido\nStopped.", CARD_MID)
    assert line["disable_notification"] is True and keys(line["reply_markup"]) == "remove"


def test_a_runs_title_is_the_sites_its_card_names():
    assert stop_keys.run_title(CARD_TEXT) == "RunPod"
    assert stop_keys.run_title(
        "📊 Fido\nOn it (2 runs):\n• RunPod — a\n• Hetzner, RunPod — b\n• RunPod — c"
    ) == "RunPod, Hetzner"
    assert stop_keys.run_title("📊 Fido\nOK to start?\n• the open web — find flights\n\n"
                               "☑ Start") == "the open web"
    assert stop_keys.run_title("") == "browsing"
