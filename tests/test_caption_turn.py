"""#1378: a caption on a file sent to the default agent is the operator's
instruction for that file. The file is stored as before; then ONE ordinary turn
of the default agent runs, admitted exactly as a text message is — the caption
verbatim as the operator's words, the file's facts as Casa's note on a reserved
key, the file armed as the turn's own. Casa decides only whether there is a
caption; what it asks is the model's to judge (operator ruling, 2026-10-09).
A file with no caption keeps the stored-and-acknowledged path.
"""
from __future__ import annotations

import asyncio
import os
import types
from types import SimpleNamespace as NS

import pytest

import agent_inbox as ai
from provenance import sanitize_external_context
from test_desk_echo_prompt import _agent, _turn
from test_desk_route import OPERATOR, _channel, _FakeBot  # noqa: F401
from test_inbound_files import PDF, _doc, _msg
from test_telegram_new_reset import _drain_bus

pytestmark = pytest.mark.asyncio

CAPTION = "  add it to the invoices  "


class _Sched:
    def add_job(self, *a, **k):
        pass


@pytest.fixture(autouse=True)
def _clean_inbox():
    ai._reset_for_tests()
    yield
    ai._reset_for_tests()


@pytest.fixture
async def wired(tmp_path, monkeypatch):
    await ai.wire(_Sched(), str(tmp_path / "agent-inbox"), role="assistant")

    async def fetch(bot, file_id, cap):
        return PDF
    monkeypatch.setattr(ai, "fetch_telegram_file", fetch)
    return ai.get_inbox("assistant")


def _file_msg(caption=None, *, media_group_id=None, document=None, **over):
    m = _msg(chat_id=OPERATOR, user_id=OPERATOR,
             document=document or _doc("invoice-0912.pdf"), **over)
    m.caption = caption
    m.media_group_id = media_group_id
    return m


class _Bot(_FakeBot):
    def __init__(self):
        super().__init__()
        self.texts: list[str] = []

    async def send_message(self, **kw):
        self.texts.append(kw.get("text"))
        return NS(message_id=len(self.texts))


def _ch():
    ch, bus = _channel()
    ch._app = types.SimpleNamespace(bot=_Bot())
    return ch, bus


async def test_a_captioned_file_is_stored_and_runs_one_turn_with_the_caption_verbatim(wired):
    ch, bus = _ch()
    await ch._on_non_text_message(NS(message=_file_msg(CAPTION)))
    [stored] = wired.list_files()
    assert stored.display_name == "invoice-0912.pdf"
    [turn] = await _drain_bus(bus)
    assert turn.target == "assistant" and turn.channel == "telegram"
    assert turn.content == CAPTION                     # the operator's words, untouched
    assert ch._app.bot.texts == []                     # the turn's reply replaces the ack
    rf = turn.context["_received_file"]
    path = os.path.join(wired.ready_dir, stored.name)
    assert rf["path"] == path and rf["name"] == "invoice-0912.pdf"
    for fact in ("invoice-0912.pdf", "PDF", path, "caption"):
        assert fact in rf["note"], fact
    # admitted as the operator's own text turn is
    assert turn.context["_operator_turn"] is True
    assert turn.context["chat_id"] == str(OPERATOR)
    assert turn.trusted_user_origin is not None


@pytest.mark.parametrize("caption", [None, "", "   \n"])
async def test_a_file_without_a_caption_is_acknowledged_and_runs_no_turn(wired, caption):
    ch, bus = _ch()
    await ch._on_non_text_message(NS(message=_file_msg(caption)))
    assert await _drain_bus(bus) == []
    [ack] = ch._app.bot.texts
    assert ack.startswith("Got invoice-0912.pdf") and "Ask me any time" in ack
    assert len(wired.list_files()) == 1


async def test_a_refused_upload_with_a_caption_runs_no_turn(wired):
    ch, bus = _ch()
    big = _doc("huge.pdf", size=ai.CAP_BYTES + 1)
    await ch._on_non_text_message(NS(message=_file_msg(CAPTION, document=big)))
    assert await _drain_bus(bus) == []
    [refusal] = ch._app.bot.texts
    assert "more than" in refusal
    assert wired.list_files() == []


async def test_a_rate_limited_caption_falls_back_to_the_acknowledgement(wired):
    ch, bus = _ch()
    ch._rate_limiter = NS(enabled=True,
                          check=lambda key: NS(allowed=False, should_notify=False))
    await ch._on_non_text_message(NS(message=_file_msg(CAPTION)))
    assert await _drain_bus(bus) == []
    [ack] = ch._app.bot.texts
    assert ack.startswith("Got invoice-0912.pdf")       # stored, and said so


async def test_a_caption_turn_no_queue_took_falls_back_to_the_acknowledgement(wired):
    """Round 1 (Terra): the bus drops a message for an unregistered target in
    silence; the caption path must then acknowledge, not go quiet."""
    ch, bus = _ch()
    bus.queues.pop("assistant")
    await ch._on_non_text_message(NS(message=_file_msg(CAPTION)))
    [ack] = ch._app.bot.texts
    assert ack.startswith("Got invoice-0912.pdf")
    assert len(wired.list_files()) == 1


async def test_the_caption_turn_waits_for_the_chats_serial_lock(wired):
    """The same admission as a text message: a turn already holding the chat's
    lock (a /new reset, say) finishes before the caption's turn is queued."""
    ch, bus = _ch()
    lock = ch._chat_serial_locks.setdefault(str(OPERATOR), asyncio.Lock())
    await lock.acquire()
    task = asyncio.create_task(ch._on_non_text_message(NS(message=_file_msg(CAPTION))))
    for _ in range(500):                  # until it finishes or queues on the lock
        if task.done() or getattr(lock, "_waiters", None):
            break
        await asyncio.sleep(0.01)
    assert wired.list_files()             # stored before it waits
    assert not task.done()
    assert await _drain_bus(bus) == []
    lock.release()
    await task
    assert [m.content for m in await _drain_bus(bus)] == [CAPTION]


async def test_an_album_turn_names_its_own_file_and_the_uncaptioned_ones_are_acknowledged(wired):
    ch, bus = _ch()
    await ch._on_non_text_message(NS(message=_file_msg(
        "file these", media_group_id="g1", document=_doc("a.pdf"))))
    await ch._on_non_text_message(NS(message=_file_msg(
        None, media_group_id="g1", document=_doc("b.pdf"))))
    [turn] = await _drain_bus(bus)
    assert turn.context["_received_file"]["name"] == "a.pdf"
    assert "album" in turn.context["_received_file"]["note"]
    assert [t[:9] for t in ch._app.bot.texts] == ["Got b.pdf"]


async def test_no_external_context_can_set_the_received_file():
    assert "_received_file" not in sanitize_external_context(
        {"_received_file": {"path": "/x", "name": "x", "note": "n"}, "chat_id": "1"})


async def test_the_turn_carries_casas_note_and_owes_its_own_file(tmp_path, monkeypatch):
    """The agent side: the note rides in the Casa notes block (stripped on
    readback), the operator's words stay raw, and the turn's scope is armed
    over that one file, as a file desk turn's is."""
    import result_broker as rb
    import specialist_desk as sd
    from output_boundary import TurnScope
    from timekeeping import split_time_envelope
    monkeypatch.setattr(sd, "DESK_ECHO", rb.PostLedger(max_events=64))
    agent = _agent(tmp_path)
    rf = {"path": "/data/agent-inbox/assistant/ready/1-ab.pdf", "name": "invoice.pdf",
          "note": "The person sent a file with this message: invoice.pdf (PDF, 1 KB)."}
    query = await _turn(agent, text=CAPTION, _received_file=rf)
    assert "<casa_notes>\n" + rf["note"] + "\n</casa_notes>\n\n" + CAPTION in query
    assert split_time_envelope(query)[1] == CAPTION
    msg = NS(id="m", channel="telegram", type=NS(value="channel_in"),
             context={"cid": "c", "_received_file": rf})
    scope = TurnScope.mint(msg, NS(role="assistant", character=NS(name="Ellen")))
    [ob] = [o for o in scope.obligations if type(o).__name__ == "ReadBeforeDescribe"]
    assert ob.source == "own" and [n for _, n in ob.files] == ["invoice.pdf"]
    plain = TurnScope.mint(NS(id="m", channel="telegram", type=NS(value="channel_in"),
                              context={"cid": "c"}),
                           NS(role="assistant", character=NS(name="Ellen")))
    assert not [o for o in plain.obligations if type(o).__name__ == "ReadBeforeDescribe"]
