"""Stop pressed on a reply's draft.

Telegram reports the press as an update, ``stopped_message_generation``,
naming the chat and the draft. The channel ends that draft's stream and asks
the SDK client pool to interrupt the turn that was writing it — and nothing
else: a press for a draft this channel is not writing, or one that names
another chat than the draft's, is ignored.

python-telegram-bot 22.8 has no field for this update: the payload is kept in
``Update.api_kwargs``, and only a handler that takes every update sees it. So
all of this runs against the real library, in a fresh interpreter (this
process imports a stub of it): each scenario starts an endpoint through
``TelegramChannel._rebuild`` over a canned Bot API, feeds it updates the way
a webhook does, and waits for every handler to finish. That is also what
shows the handlers of ordinary updates still run, on Casa's bot and on an
agent's own bot, whose updates pass its intake queue first.

The log line of a press needs none of that: it is read in this process, from
the handler called directly on a channel over a fake bot.
"""
from __future__ import annotations

import json
import logging
import pathlib
import subprocess
import sys
import types

import pytest

from channels.telegram import ALLOWED_UPDATES_AGENT, ALLOWED_UPDATES_CASA
from telegram_channel_double import _mk_channel_with_fake_bot

CODE = pathlib.Path(__file__).resolve().parents[1] / "casa" / "rootfs" / "opt" / "casa"

_SCENARIOS = r'''
import asyncio, json, logging, sys
sys.path.insert(0, sys.argv[1])
try:
    import httpx
    from telegram import Update
except ImportError:
    print(json.dumps({"skip": True})); raise SystemExit(0)

import channels.telegram as tg
import own_voice
import sdk_client_pool
from channels.telegram import TelegramChannel
from channels.telegram_draft import DraftStream

CHAT, CASA_BOT, AGENT_BOT = 42, 111, 222
update_ids = iter(range(1, 10_000))
interrupts = []          # the cids the pool was asked to interrupt
offered = []             # the updates an agent's bot offered its chat's lifecycle
called = []              # every Bot API method the endpoint called
_TEXT_CALLS = {"sendMessage", "sendMessageDraft", "editMessageText"}


def bot_api(request):
    name = request.url.path.rsplit("/", 1)[-1]
    called.append(name)
    result = True
    if name == "getMe":
        bot = int(request.url.path.split("/bot", 1)[1].split(":", 1)[0])
        result = {"id": bot, "is_bot": True, "first_name": "t", "username": "t_bot"}
    return httpx.Response(200, json={"ok": True, "result": result})


_request = tg._SurrogateSafeRequest
tg._SurrogateSafeRequest = lambda: _request(
    httpx_kwargs={"transport": httpx.MockTransport(bot_api)})


async def interrupt_turns(cid):
    interrupts.append(cid)
    return 1


async def intake(bot_id, update_json):
    offered.append(sorted(update_json))
    return "deliver"


sdk_client_pool.interrupt_turns = interrupt_turns
own_voice.intake = intake


def stop_update(draft_id, chat=CHAT):
    return {"update_id": next(update_ids), "stopped_message_generation": {
        "chat": {"id": chat, "type": "private"}, "draft_id": draft_id}}


def message_update():
    person = {"id": CHAT, "is_bot": False, "first_name": "Op"}
    return {"update_id": next(update_ids), "message": {
        "message_id": 5, "date": 1794339400, "text": "hello", "from": person,
        "chat": {"id": CHAT, "type": "private", "first_name": "Op"}}}


async def started(bot):
    """An endpoint brought up as in production, with the text handler
    replaced by one that records it ran."""
    del interrupts[:], offered[:]
    casa = bot == "casa"
    ch = TelegramChannel(
        bot_token=f"{CASA_BOT if casa else AGENT_BOT}:AA", chat_id=str(CHAT),
        default_agent="assistant" if casa else "finance", is_casa=casa,
        known_bot_id=None if casa else AGENT_BOT,
        webhook_url="https://casa.example")
    handled = []

    async def on_text(update, context=None):
        handled.append("text")

    ch.handle_update = on_text
    await ch._rebuild()
    return ch, handled


async def open_draft(ch):
    """Start a turn's draft on *ch*. Returns the id its draft calls carry
    and its stream."""
    sends, carried = [], []

    def new_stream(**kw):
        sends.append(kw["send"])
        return DraftStream(**kw)

    async def send_draft(target_chat, draft_id, text):
        carried.append(draft_id)

    ch._new_draft_stream = new_stream
    ch._send_draft = send_draft
    ch.create_on_token({"chat_id": str(CHAT), "cid": "c1"})
    await sends[0]("text")
    [(_chat, stream)] = ch._streams.values()
    return carried[0], stream


async def deliver(ch, *payloads):
    """Feed *payloads* as a webhook does, then wait for every handler."""
    for payload in payloads:
        assert await ch.process_webhook_update(payload) == "accepted"
    await ch._app.update_queue.join()
    await ch.stop()


async def stop(bot, draft=str, chat=CHAT, offset=0):
    ch, handled = await started(bot)
    draft_id, stream = await open_draft(ch)
    del called[:]
    await deliver(ch, stop_update(draft(draft_id + offset), chat))
    return {"carried": type(draft_id).__name__, "ended": stream.ended,
            "interrupts": list(interrupts), "streams": len(ch._streams),
            "handled": handled, "offered": list(offered),
            "sent": [name for name in called if name in _TEXT_CALLS]}


async def message(bot):
    ch, handled = await started(bot)
    _draft_id, stream = await open_draft(ch)
    await deliver(ch, message_update())
    return {"ended": stream.ended, "interrupts": list(interrupts),
            "handled": handled, "offered": list(offered)}


async def failing_interrupt():
    ch, _handled = await started("casa")
    draft_id, stream = await open_draft(ch)
    logged = []
    handler = logging.Handler()
    handler.emit = lambda record: logged.append(record.levelname)
    logging.getLogger("channels.telegram").addHandler(handler)

    async def failing(cid):
        raise RuntimeError("the pool is gone")

    sdk_client_pool.interrupt_turns = failing
    try:
        await ch._on_stop_update(
            Update.de_json(stop_update(str(draft_id)), None), None)
        raised = None
    except Exception as exc:
        raised = repr(exc)
    finally:
        sdk_client_pool.interrupt_turns = interrupt_turns
        logging.getLogger("channels.telegram").removeHandler(handler)
        await ch.stop()
    return {"raised": raised, "ended": stream.ended, "logged": logged}


SCENARIOS = {
    "stop:casa": lambda: stop("casa"),
    "stop:agent": lambda: stop("agent"),
    "stop:int-draft-id": lambda: stop("casa", draft=int),
    "stop:unknown-draft": lambda: stop("casa", offset=1),
    "stop:another-chat": lambda: stop("casa", chat=7),
    "message:casa": lambda: message("casa"),
    "message:agent": lambda: message("agent"),
    "failing-interrupt": failing_interrupt,
}


async def main():
    parsed = Update.de_json(stop_update("7"), None)
    out = {"api_kwargs": dict(parsed.api_kwargs)}
    for name, scenario in SCENARIOS.items():
        try:
            out[name] = await asyncio.wait_for(scenario(), timeout=20)
        except Exception as exc:
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(out))


asyncio.run(main())
'''


@pytest.fixture(scope="module")
def ran() -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _SCENARIOS, str(CODE)],
        capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    if out.get("skip"):
        pytest.skip("python-telegram-bot not installed")
    return out


def _scenario(ran: dict, name: str) -> dict:
    # The library keeps the update's payload where the channel reads it. Once
    # it gains a field for this update, the payload moves there.
    assert ran["api_kwargs"] == {"stopped_message_generation": {
        "chat": {"id": 42, "type": "private"}, "draft_id": "7"}}
    got = ran[name]
    assert "error" not in got, got["error"]
    return got


@pytest.mark.parametrize("bot", ["casa", "agent"])
def test_stop_ends_the_stream_and_interrupts_by_cid(ran, bot):
    got = _scenario(ran, f"stop:{bot}")
    assert got["ended"] is True
    assert got["interrupts"] == ["c1"]          # once, with the stream's cid
    assert got["streams"] == 0
    assert got["handled"] == []                 # it is nobody's message
    assert got["sent"] == []                    # nothing is sent to the chat
    # an agent's bot offers the update to its chat's lifecycle like any other
    assert got["offered"] == ([] if bot == "casa" else [
        ["stopped_message_generation", "update_id"]])


def test_stop_matches_a_string_draft_id_against_an_int(ran):
    # The draft calls carry the id as a number; the update names it as text.
    got = _scenario(ran, "stop:casa")
    assert got["carried"] == "int"
    assert got["ended"] is True and got["interrupts"] == ["c1"]
    # ... and as a number, which is what the API reference says it is.
    got = _scenario(ran, "stop:int-draft-id")
    assert got["ended"] is True and got["interrupts"] == ["c1"]


def test_stop_for_an_unknown_draft_is_ignored(ran):
    got = _scenario(ran, "stop:unknown-draft")
    assert got["ended"] is False
    assert got["interrupts"] == []
    assert got["streams"] == 1


def test_stop_from_another_chat_is_ignored(ran):
    got = _scenario(ran, "stop:another-chat")
    assert got["ended"] is False
    assert got["interrupts"] == []
    assert got["streams"] == 1


@pytest.mark.parametrize("bot", ["casa", "agent"])
def test_other_updates_do_not_reach_the_stop_path(ran, bot):
    got = _scenario(ran, f"message:{bot}")
    assert got["handled"] == ["text"]           # the message handler still ran
    assert got["ended"] is False
    assert got["interrupts"] == []
    assert got["offered"] == ([] if bot == "casa" else [["message", "update_id"]])


def test_stop_update_is_listed_for_both_bots():
    assert "stopped_message_generation" in ALLOWED_UPDATES_CASA
    assert "stopped_message_generation" in ALLOWED_UPDATES_AGENT


def test_a_failing_interrupt_does_not_raise(ran):
    got = _scenario(ran, "failing-interrupt")
    assert got["raised"] is None
    assert got["ended"] is True
    assert got["logged"] == ["ERROR"]


@pytest.mark.parametrize("reached", [1, 0])
async def test_a_press_is_logged_with_its_cid_and_the_turns_reached(
    monkeypatch, caplog, reached,
):
    # A press that reached no turn is logged too: the count is what tells
    # the two apart.
    ch, bot = _mk_channel_with_fake_bot()
    ch.create_on_token({"chat_id": "42", "cid": "c1"})
    [draft_id] = ch._streams

    async def interrupt_turns(cid):
        return reached

    monkeypatch.setattr("sdk_client_pool.interrupt_turns", interrupt_turns)
    press = types.SimpleNamespace(api_kwargs={"stopped_message_generation": {
        "chat": {"id": 42, "type": "private"}, "draft_id": draft_id}})

    with caplog.at_level(logging.INFO, logger="channels.telegram"):
        await ch._on_stop_update(press)

    assert ch._streams == {}
    assert [(r.levelname, r.getMessage()) for r in caplog.records
            if r.name == "channels.telegram"] == [
        ("INFO", f"Stop pressed on a draft (cid=c1): turns interrupted={reached}")]
    assert bot.send_message.await_count == 0
