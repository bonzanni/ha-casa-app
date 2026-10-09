"""#1379: files sent together get ONE acknowledgement, edited as each lands.
"Together" is mechanical: the same Telegram album, or a file event within a short
window in the chat. A refused file is reported in that same message. An album's
caption makes the assistant act on the album's kept files in one turn, and the
acknowledgement then carries only what that turn does not answer.
"""
from __future__ import annotations

import asyncio
import types
from types import SimpleNamespace as NS

import pytest

import agent_inbox as ai
import channels.telegram as tg
from test_caption_turn import _clean_inbox, _file_msg, wired  # noqa: F401
from test_desk_route import OPERATOR, _channel
from test_inbound_files import _doc
from test_telegram_new_reset import _drain_bus

pytestmark = pytest.mark.asyncio

DAYS = ai.RETENTION_S // 86400


class _Bot:
    """Records the chat as the operator would see it: message id -> text."""

    def __init__(self):
        self.chat: dict[int, str] = {}
        self.sends = 0
        self.edits = 0
        self.fail_edit = False

    async def send_message(self, **kw):
        self.sends += 1
        mid = 100 + self.sends
        self.chat[mid] = kw["text"]
        return NS(message_id=mid)

    async def edit_message_text(self, **kw):
        if self.fail_edit:
            raise RuntimeError("message can't be edited")
        self.edits += 1
        assert kw["message_id"] in self.chat
        self.chat[kw["message_id"]] = kw["text"]
        return NS(message_id=kw["message_id"])

    async def delete_message(self, **kw):
        del self.chat[kw["message_id"]]
        return True


def _ch():
    ch, bus = _channel()
    ch._app = types.SimpleNamespace(bot=_Bot())
    ch.ALBUM_SETTLE_S = 0.05
    return ch, bus


def _send(ch, name, caption=None, group=None, size=1234):
    return ch._on_non_text_message(NS(message=_file_msg(
        caption, media_group_id=group, document=_doc(name, size=size))))


def _chat(ch):
    return list(ch._app.bot.chat.values())


async def test_three_files_in_a_row_get_one_message_listing_each(wired):
    ch, bus = _ch()
    await _send(ch, "a.pdf")
    assert _chat(ch) == [f"Got a.pdf — 1 KB. Ask me any time and I'll read it. "
                         f"I'll keep it {DAYS} days."]           # one file: today's line
    await _send(ch, "b.pdf")
    await _send(ch, "c.pdf")
    assert _chat(ch) == [f"Got 3 files: a.pdf (1 KB), b.pdf (1 KB), c.pdf (1 KB). Ask me "
                         f"any time and I'll read them. I'll keep them {DAYS} days."]
    assert ch._app.bot.sends == 1 and ch._app.bot.edits == 2      # listed as each landed
    assert await _drain_bus(bus) == []
    assert len(wired.list_files()) == 3


async def test_an_album_handled_concurrently_still_gets_one_message(wired):
    ch, _ = _ch()
    await asyncio.gather(*(_send(ch, f"{n}.pdf", group="g1") for n in "abc"))
    [text] = _chat(ch)
    assert text.startswith("Got 3 files: ") and all(f"{n}.pdf" in text for n in "abc")
    assert ch._app.bot.sends == 1


async def test_a_refused_file_is_reported_in_the_same_message(wired):
    ch, _ = _ch()
    await _send(ch, "a.pdf", group="g1")
    await _send(ch, "notes.docx", group="g1")
    await _send(ch, "huge.pdf", group="g1", size=ai.CAP_BYTES + 1)
    [text] = _chat(ch)
    first, docx, huge = text.split("\n")
    assert first.startswith("Got a.pdf — 1 KB.")
    assert docx.startswith("notes.docx: I can't open a .docx")
    assert huge.startswith("huge.pdf: That PDF is more than")


async def test_files_apart_in_time_get_their_own_messages(wired, monkeypatch):
    monkeypatch.setattr(tg, "ACK_WINDOW_S", 0.0)
    ch, _ = _ch()
    await _send(ch, "a.pdf")
    await asyncio.sleep(0.01)
    await _send(ch, "b.pdf")
    assert [t[:9] for t in _chat(ch)] == ["Got a.pdf", "Got b.pdf"]


async def test_a_text_message_in_between_starts_a_new_message(wired):
    ch, _ = _ch()

    async def serialized(update, chat_id):
        return None
    ch._handle_serialized = serialized
    await _send(ch, "a.pdf")
    await ch._handle(NS(message=NS(text="thanks"), effective_chat=NS(id=OPERATOR)), None)
    await _send(ch, "b.pdf")
    assert [t[:9] for t in _chat(ch)] == ["Got a.pdf", "Got b.pdf"]


async def test_a_message_that_cannot_be_edited_is_replaced_by_a_full_one(wired):
    ch, _ = _ch()
    await _send(ch, "a.pdf")
    ch._app.bot.fail_edit = True
    await _send(ch, "b.pdf")
    assert _chat(ch)[-1].startswith("Got 2 files: a.pdf (1 KB), b.pdf (1 KB).")


async def test_ten_long_names_fit_one_telegram_message(wired):
    ch, _ = _ch()
    for i in range(tg.ACK_MAX_FILES):
        await _send(ch, f"{i}{'x' * 250}.pdf", group="g1")
    [text] = _chat(ch)
    assert len(text) <= 4096 and text.startswith(f"Got {tg.ACK_MAX_FILES} files")
    await _send(ch, "late.pdf")                            # the next one starts afresh
    assert _chat(ch)[-1].startswith("Got late.pdf")


async def test_an_album_caption_runs_one_turn_over_every_kept_file_and_no_ack(wired):
    ch, bus = _ch()
    await asyncio.gather(_send(ch, "a.pdf", "add these to the invoices", group="g1"),
                         _send(ch, "b.pdf", group="g1"), _send(ch, "c.pdf", group="g1"))
    [turn] = await _drain_bus(bus)
    assert turn.content == "add these to the invoices"
    rf = turn.context["_received_file"]
    assert [f["name"] for f in rf["files"]] == ["a.pdf", "b.pdf", "c.pdf"]
    assert all(f["path"] in rf["note"] for f in rf["files"])
    assert _chat(ch) == []                                # her reply is the answer


async def test_a_caption_written_on_the_last_file_still_covers_the_album(wired):
    """A sibling acknowledged before the captioned file arrived is taken back
    once the turn covers it, so the chat never shows both."""
    ch, bus = _ch()
    await _send(ch, "a.pdf", group="g1")
    assert len(_chat(ch)) == 1
    await _send(ch, "b.pdf", "file both", group="g1")
    [turn] = await _drain_bus(bus)
    assert [f["name"] for f in turn.context["_received_file"]["files"]] == ["a.pdf", "b.pdf"]
    assert _chat(ch) == []


async def test_an_album_caption_leaves_the_refusals_in_the_message(wired):
    ch, bus = _ch()
    await asyncio.gather(_send(ch, "a.pdf", "file these", group="g1"),
                         _send(ch, "notes.docx", group="g1"))
    [turn] = await _drain_bus(bus)
    assert [f["name"] for f in turn.context["_received_file"]["files"]] == ["a.pdf"]
    [text] = _chat(ch)
    assert text.startswith("notes.docx: I can't open a .docx")


async def test_a_refused_captioned_file_still_has_its_album_acted_on(wired):
    ch, bus = _ch()
    await asyncio.gather(_send(ch, "notes.docx", "file these", group="g1"),
                         _send(ch, "a.pdf", group="g1"))
    [turn] = await _drain_bus(bus)
    assert [f["name"] for f in turn.context["_received_file"]["files"]] == ["a.pdf"]
    assert [t[:32] for t in _chat(ch)] == ["notes.docx: I can't open a .docx"]


async def test_an_album_caption_with_no_turn_acknowledges_every_file(wired):
    ch, bus = _ch()
    ch._rate_limiter = NS(enabled=True,
                          check=lambda key: NS(allowed=False, should_notify=False))
    await asyncio.gather(_send(ch, "a.pdf", "file these", group="g1"),
                         _send(ch, "b.pdf", group="g1"))
    assert await _drain_bus(bus) == []
    assert _chat(ch) == [f"Got 2 files: a.pdf (1 KB), b.pdf (1 KB). Ask me any time "
                         f"and I'll read them. I'll keep them {DAYS} days."]


async def test_the_caption_turn_waits_for_a_slow_album_file(wired, monkeypatch):
    """The turn is held until every file of the album has landed, so a slow
    download is in the turn rather than acknowledged beside it."""
    ch, bus = _ch()
    real = ai.fetch_telegram_file
    gate = asyncio.Event()

    async def fetch(bot, file_id, cap):
        if file_id == "slow":
            await gate.wait()
        return await real(bot, file_id, cap)
    monkeypatch.setattr(ai, "fetch_telegram_file", fetch)
    slow = _file_msg(None, media_group_id="g1", document=NS(
        file_id="slow", file_name="b.pdf", file_size=1234))
    first = asyncio.create_task(_send(ch, "a.pdf", "file these", group="g1"))
    second = asyncio.create_task(ch._on_non_text_message(NS(message=slow)))
    await asyncio.sleep(0.3)
    assert await _drain_bus(bus) == [] and not first.done()
    gate.set()
    await asyncio.gather(first, second)
    [turn] = await _drain_bus(bus)
    assert [f["name"] for f in turn.context["_received_file"]["files"]] == ["a.pdf", "b.pdf"]
    assert _chat(ch) == []


async def test_an_album_stays_one_message_however_slowly_it_lands(wired, monkeypatch):
    monkeypatch.setattr(tg, "ACK_WINDOW_S", 0.0)
    ch, _ = _ch()
    await _send(ch, "a.pdf", group="g1")
    await asyncio.sleep(0.01)
    await _send(ch, "b.pdf", group="g1")
    [text] = _chat(ch)
    assert text.startswith("Got 2 files: a.pdf (1 KB), b.pdf (1 KB).")


async def test_a_full_album_after_a_lone_file_is_one_turn_over_all_ten(wired):
    """Round 1 (Astra): an album keeps a message of its own, so a file sent
    just before it cannot push its tenth file out of the caption's turn."""
    ch, bus = _ch()
    await _send(ch, "before.pdf")
    await asyncio.gather(*(_send(ch, f"{i}.pdf", "file these" if i == 0 else None,
                                 group="g1") for i in range(10)))
    [turn] = await _drain_bus(bus)
    assert len(turn.context["_received_file"]["files"]) == 10
    assert [t[:14] for t in _chat(ch)] == ["Got before.pdf"]


async def test_a_caption_arriving_after_the_turn_took_its_files_runs_its_own(wired):
    """Round 1 (Terra): once a claim's turn has taken its files and words, a
    later captioned file of the album is not folded into it and lost."""
    ch, bus = _ch()
    lock = ch._chat_serial_locks.setdefault(str(OPERATOR), asyncio.Lock())
    await lock.acquire()
    first = asyncio.create_task(_send(ch, "a.pdf", "file these", group="g1"))
    for _ in range(200):
        if getattr(lock, "_waiters", None):
            break
        await asyncio.sleep(0.01)
    second = asyncio.create_task(_send(ch, "b.pdf", "and this one", group="g1"))
    await asyncio.sleep(0.2)
    lock.release()
    await asyncio.gather(first, second)
    turns = await _drain_bus(bus)
    assert [t.content for t in turns] == ["file these", "and this one"]
    assert [[f["name"] for f in t.context["_received_file"]["files"]] for t in turns] \
        == [["a.pdf"], ["b.pdf"]]
    assert _chat(ch) == []


async def test_two_albums_arriving_interleaved_keep_their_own_groups(wired):
    """Round 2 (Terra): an album's group is found by its album id, so another
    album's file landing in between cannot split it."""
    ch, bus = _ch()
    await asyncio.gather(_send(ch, "a1.pdf", "file these", group="A"),
                         _send(ch, "b1.pdf", group="B"), _send(ch, "a2.pdf", group="A"),
                         _send(ch, "b2.pdf", group="B"))
    [turn] = await _drain_bus(bus)
    assert [f["name"] for f in turn.context["_received_file"]["files"]] == ["a1.pdf", "a2.pdf"]
    assert [t[:20] for t in _chat(ch)] == ["Got 2 files: b1.pdf "]


async def test_a_lone_refusal_left_beside_a_turn_still_names_its_file(wired):
    """Round 2 (Astra): when a turn takes the album's kept files, the one
    refusal left in the message still says which file it was."""
    ch, bus = _ch()
    await asyncio.gather(_send(ch, "jan.pdf", "file these", group="g1"),
                         _send(ch, "feb.pdf", group="g1", size=ai.CAP_BYTES + 1))
    [turn] = await _drain_bus(bus)
    assert len(_chat(ch)) == 1
    assert _chat(ch)[0].startswith("feb.pdf: That PDF is more than")
