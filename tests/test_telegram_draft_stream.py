"""The draft stream: the token callback only stores the latest text, and one
task per turn sends it on its own clock — changed text once per interval,
the same text again every keepalive while the turn is not idle.

The clock and the sleep are constructor parameters. The fake sleep advances
the fake clock and yields to the event loop on the real ``asyncio.sleep``
captured at import, so the stream's loop can never spin without yielding;
every test drives it with a bounded number of yields.

The second half is the channel's side: the token callback it hands a turn,
and the one draft call it makes for the stream.
"""
from __future__ import annotations

import asyncio
import functools
import logging
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from telegram import MessageEntity
from telegram.error import RetryAfter

from channels.telegram_draft import DraftStream, DraftTooLong
from output_boundary_testing import admitted
from telegram_channel_double import _mk_channel_with_fake_bot

_real_sleep = asyncio.sleep
_MAX_YIELDS = 200_000


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds
        await _real_sleep(0)


class Recorder:
    """A ``send`` that records (time, text) and raises or returns as scripted."""

    def __init__(self, ft: FakeTime, script=None) -> None:
        self.ft = ft
        self.calls: list[tuple[float, object]] = []
        self.script = script or (lambda text, n: None)

    async def __call__(self, text):
        self.calls.append((self.ft.t, text))
        return self.script(text, len(self.calls))

    @property
    def texts(self) -> list[object]:
        return [text for _, text in self.calls]


def make(ft: FakeTime, send, **kw) -> DraftStream:
    return DraftStream(send=send, cid="c1", clock=ft.clock, sleep=ft.sleep, **kw)


async def run_until(ft: FakeTime, t: float) -> None:
    for _ in range(_MAX_YIELDS):
        if ft.t >= t:
            return
        await _real_sleep(0)
    raise AssertionError(f"clock stuck at {ft.t}, wanted {t}")


def stream_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks()
            if t.get_name() == "telegram-draft-stream" and not t.done()]


async def finish(stream: DraftStream) -> None:
    stream.end()
    for _ in range(5):
        await _real_sleep(0)
    assert stream_tasks() == []


async def test_on_token_never_calls_send_itself():
    ft = FakeTime()
    send = Recorder(ft)
    stream = make(ft, send)
    await stream.on_token("a")
    assert send.calls == []
    await finish(stream)


async def test_changed_text_goes_out_once_per_interval():
    ft = FakeTime()
    send = Recorder(ft)
    stream = make(ft, send)
    await stream.on_token("a")
    await stream.on_token("ab")
    await stream.on_token("abc")
    await run_until(ft, 1.0)
    assert send.texts == ["abc"]
    assert send.calls[0][0] == pytest.approx(0.6)
    await finish(stream)


async def test_unchanged_text_is_resent_every_keepalive():
    ft = FakeTime()
    send = Recorder(ft)
    stream = make(ft, send)
    await stream.on_token("hello")
    await run_until(ft, 25.0)
    assert send.texts == ["hello", "hello", "hello"]
    times = [t for t, _ in send.calls]
    assert times[0] == pytest.approx(0.6)
    # rounds fall on the 0.6 s grid: the first at or after 0.6 + 10 is 10.8,
    # the next at or after 10.8 + 10 is 21.0
    assert times[1] == pytest.approx(10.8, abs=0.01)
    assert times[2] == pytest.approx(21.0, abs=0.01)
    await finish(stream)


async def test_resend_stops_after_the_idle_limit():
    ft = FakeTime()
    send = Recorder(ft)
    stream = make(ft, send)
    await stream.on_token("a")
    await run_until(ft, 700.0)
    assert send.calls, "the text should have gone out"
    assert all(t < 600.0 for t, _ in send.calls)
    before = len(send.calls)
    await stream.on_token("ab")
    await run_until(ft, 702.0)
    assert send.texts[before:] == ["ab"]
    await finish(stream)


async def test_too_long_text_keeps_the_last_that_fitted():
    ft = FakeTime()

    def script(text, n):
        if text == "abcd":
            raise DraftTooLong()
        return None

    send = Recorder(ft, script)
    stream = make(ft, send)
    await stream.on_token("abc")
    await run_until(ft, 1.0)
    await stream.on_token("abcd")
    await run_until(ft, 2.0)
    await stream.on_token("abcde")
    await run_until(ft, 25.0)
    assert send.texts[:2] == ["abc", "abcd"]
    later = send.texts[2:]
    assert later and all(text == "abc" for text in later)
    await finish(stream)


async def test_a_failing_send_is_not_retried_and_warns_once(caplog):
    ft = FakeTime()

    def script(text, n):
        raise RuntimeError("network down")

    send = Recorder(ft, script)
    stream = make(ft, send)
    with caplog.at_level(logging.WARNING, logger="channels.telegram_draft"):
        await stream.on_token("a")
        await run_until(ft, 5.0)
        # "a" failed once at 0.6 and is not tried again within the interval rounds
        assert send.calls == [(pytest.approx(0.6, abs=0.01), "a")]
        # new text is attempted at the next round
        await stream.on_token("ab")
        await run_until(ft, 5.5)
        assert send.texts == ["a", "ab"]
        assert send.calls[1][0] == pytest.approx(5.4, abs=0.01)
        # with no new token, the next attempt comes at the keepalive
        await run_until(ft, 16.0)
        assert send.texts == ["a", "ab", "ab"]
        # first round on the 0.6 s grid at or after 5.4 + 10
        assert send.calls[2][0] == pytest.approx(15.6, abs=0.01)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    await finish(stream)


async def test_flood_wait_is_slept():
    ft = FakeTime()
    send = Recorder(ft, lambda text, n: 3.0 if n == 1 else None)
    stream = make(ft, send)
    await stream.on_token("a")
    await run_until(ft, 5.0)
    assert 3.0 in ft.slept
    await finish(stream)


async def test_a_started_stream_shows_an_empty_draft_until_the_first_text():
    ft = FakeTime()
    send = Recorder(ft)
    stream = make(ft, send)
    stream.start()
    stream.start()                       # a second start is nothing
    assert len(stream_tasks()) == 1
    await run_until(ft, 12.0)
    # the empty draft at the first round, and again at the keepalive
    assert send.texts == ["", ""]
    assert send.calls[0][0] == pytest.approx(0.6)
    await stream.on_token("hello")
    await run_until(ft, 13.0)
    assert send.texts == ["", "", "hello"]
    await finish(stream)


async def test_an_empty_draft_is_kept_up_to_the_idle_limit_from_the_start():
    ft = FakeTime()
    send = Recorder(ft)
    stream = make(ft, send, idle_limit=25.0)
    stream.start()
    await run_until(ft, 60.0)
    assert send.texts == ["", "", ""]
    assert send.calls[-1][0] < 25.0
    await finish(stream)


async def test_start_after_end_starts_nothing():
    ft = FakeTime()
    send = Recorder(ft)
    stream = make(ft, send)
    stream.end()
    stream.start()
    assert stream_tasks() == []
    assert send.calls == []


async def test_end_stops_everything():
    ft = FakeTime()
    send = Recorder(ft)
    ended: list[int] = []
    stream = make(ft, send, on_end=lambda: ended.append(1))
    await stream.on_token("a")
    await run_until(ft, 1.0)
    assert send.texts == ["a"]
    await finish(stream)
    assert stream.ended
    assert ended == [1]

    ft.t += 100.0
    for _ in range(20):
        await _real_sleep(0)
    assert send.texts == ["a"]

    stream.end()
    assert ended == [1]
    await stream.on_token("ab")
    for _ in range(20):
        await _real_sleep(0)
    assert stream_tasks() == []
    assert send.texts == ["a"]

    early_ended: list[int] = []
    early = make(ft, send, on_end=lambda: early_ended.append(1))
    early.end()
    await early.on_token("x")
    assert early_ended == [1]
    assert stream_tasks() == []


# ---------------------------------------------------------------------------
# The channel: a turn's token callback, and the draft call
# ---------------------------------------------------------------------------

_CTX = {"chat_id": "42", "cid": "c1"}


def channel(ft: FakeTime):
    """A channel over a fake bot whose draft streams run on the fake clock."""
    ch, bot = _mk_channel_with_fake_bot()
    ch._new_draft_stream = functools.partial(
        DraftStream, clock=ft.clock, sleep=ft.sleep)
    return ch, bot


async def finish_turn(on_token) -> None:
    on_token.end()
    for _ in range(5):
        await _real_sleep(0)
    assert stream_tasks() == []


async def test_draft_call_shape():
    ft = FakeTime()
    ch, bot = channel(ft)
    on_token = ch.create_on_token(dict(_CTX))
    await on_token(admitted("**hi**"))
    await run_until(ft, 1.0)                    # one round of the stream

    assert bot.send_message_draft.await_count == 1
    call = bot.send_message_draft.await_args
    assert call.args == ()
    assert set(call.kwargs) == {
        "chat_id", "draft_id", "text", "entities", "api_kwargs"}
    assert call.kwargs["chat_id"] == 42
    draft_id = call.kwargs["draft_id"]
    assert type(draft_id) is int and 0 < draft_id < 2**31
    assert call.kwargs["text"] == "hi"
    assert call.kwargs["entities"] == [MessageEntity(MessageEntity.BOLD, 0, 2)]
    assert call.kwargs["api_kwargs"] == {"can_stop": True, "keep_on_stop": True}
    # the stream is kept under its draft id, with the chat it writes to
    assert list(ch._streams) == [str(draft_id)]
    chat, stream = ch._streams[str(draft_id)]
    assert chat == "42" and stream.cid == "c1"
    assert bot.send_message.await_count == 0
    assert bot.edit_message_text.await_count == 0
    await finish_turn(on_token)


async def test_the_draft_is_there_empty_before_the_first_token():
    ft = FakeTime()
    ch, bot = channel(ft)
    bot.send_chat_action = AsyncMock()
    ch._start_typing("42", "c1")
    on_token = ch.create_on_token(dict(_CTX))
    await run_until(ft, 1.0)

    # no token yet: an empty draft with Stop, which Stop removes
    assert bot.send_message_draft.await_count == 1
    kwargs = bot.send_message_draft.await_args.kwargs
    assert kwargs["chat_id"] == 42
    assert kwargs["text"] == ""
    assert kwargs["entities"] is None
    assert kwargs["api_kwargs"] == {"can_stop": True, "keep_on_stop": False}
    assert list(ch._streams) == [str(kwargs["draft_id"])]
    # "typing…" stays until the first word
    assert set(ch._typing_leases["42"]) == {"c1"}

    await on_token(admitted("hi"))
    await run_until(ft, 2.0)
    # the same draft takes the text, and from then on stays after Stop
    assert bot.send_message_draft.await_count == 2
    later = bot.send_message_draft.await_args.kwargs
    assert later["draft_id"] == kwargs["draft_id"]
    assert later["text"] == "hi"
    assert later["api_kwargs"] == {"can_stop": True, "keep_on_stop": True}
    ch._stop_typing("42")
    await finish_turn(on_token)


async def test_plain_text_draft_has_no_entities():
    ft = FakeTime()
    ch, bot = channel(ft)
    on_token = ch.create_on_token(dict(_CTX))
    # no markup, one escape: sent as the text reads, without entities
    await on_token(admitted(r"2 \* 3 = 6"))
    await run_until(ft, 1.0)

    assert bot.send_message_draft.await_count == 1
    kwargs = bot.send_message_draft.await_args.kwargs
    assert kwargs["text"] == "2 * 3 = 6"
    assert kwargs["entities"] is None
    await finish_turn(on_token)


async def test_unadmitted_text_makes_zero_bot_calls(caplog):
    ft = FakeTime()
    ch, bot = channel(ft)
    on_token = ch.create_on_token(dict(_CTX))
    with caplog.at_level(logging.ERROR):
        await on_token("hi")                    # a bare string, never admitted
        await run_until(ft, 25.0)
        # the draft send itself: its own failure value, None
        assert await ch._send_draft("42", 7, "hi") is None

    assert bot.send_message_draft.await_count == 0
    assert any("unadmitted model text refused: send_draft" in r.message
               for r in caplog.records)
    await finish_turn(on_token)


async def test_first_token_releases_typing():
    ft = FakeTime()
    ch, bot = channel(ft)
    bot.send_chat_action = AsyncMock()
    ch._start_typing("42", "c1")
    ch._start_typing("42", "another-turn")
    on_token = ch.create_on_token(dict(_CTX))
    assert set(ch._typing_leases["42"]) == {"c1", "another-turn"}

    await on_token(admitted("hi"))

    # this turn's lease is gone before the stream has run a single round
    assert set(ch._typing_leases["42"]) == {"another-turn"}
    assert ft.slept == []
    assert bot.send_message_draft.await_count == 0
    ch._stop_typing("42")
    await finish_turn(on_token)


async def test_long_text_raises_draft_too_long_before_any_call():
    ft = FakeTime()
    ch, bot = channel(ft)
    with pytest.raises(DraftTooLong):
        # 2049 characters, 4098 UTF-16 units
        await ch._send_draft("42", 7, admitted("\U0001F389" * 2049))
    assert bot.send_message_draft.await_count == 0

    # 4096 units is still one message
    assert await ch._send_draft("42", 7, admitted("\U0001F389" * 2048)) is None
    assert bot.send_message_draft.await_count == 1


@pytest.mark.parametrize("asked", [3, timedelta(seconds=3)],
                         ids=["seconds", "timedelta"])
async def test_retry_after_becomes_a_wait(asked):
    ft = FakeTime()
    ch, bot = channel(ft)
    bot.send_message_draft.side_effect = RetryAfter(asked)

    wait = await ch._send_draft("42", 7, admitted("hi"))

    assert type(wait) is float and wait == 3.0
    assert bot.send_message_draft.await_count == 1


async def test_summoned_turn_gets_no_stream():
    ft = FakeTime()
    ch, _bot = channel(ft)
    ch._guest_replies["q1"] = object()

    assert ch.create_on_token({**_CTX, "_guest_query": "q1"}) is None
    assert ch._streams == {}


async def test_a_group_chat_gets_no_stream():
    # Drafts exist in private chats only; a group's or supergroup's id is
    # negative. Such a turn delivers its reply whole.
    ft = FakeTime()
    ch, _bot = channel(ft)

    assert ch.create_on_token({"chat_id": "-1001234", "cid": "c1"}) is None
    assert ch._streams == {}

    on_token = ch.create_on_token(dict(_CTX))
    assert callable(on_token)
    await finish_turn(on_token)


async def test_end_removes_the_stream():
    ft = FakeTime()
    ch, bot = channel(ft)
    on_token = ch.create_on_token(dict(_CTX))
    await on_token(admitted("hi"))
    assert len(ch._streams) == 1

    await finish_turn(on_token)

    assert ch._streams == {}
    ft.t += 100.0
    for _ in range(20):
        await _real_sleep(0)
    assert bot.send_message_draft.await_count == 0


async def test_only_the_empty_string_is_taken_bare(caplog):
    ft = FakeTime()
    ch, bot = channel(ft)
    with caplog.at_level(logging.ERROR):
        assert await ch._send_draft("42", 7, " ") is None
    assert bot.send_message_draft.await_count == 0
    assert await ch._send_draft("42", 7, "") is None
    assert bot.send_message_draft.await_count == 1
