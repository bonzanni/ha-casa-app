"""An in-process executor's topic posts only a turn's CLOSING text: what the turn
wrote after its last main-loop tool call. Text written before or between calls
("Now let me read the upgrade recipe.") is not posted; the step log already
shows the steps. Decided by block position, never by the words. Nothing
streams while the turn runs, so no interim line is ever on screen. A
specialist's conversation streams every text block exactly as before."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import (
    AssistantMessage, ResultMessage, TextBlock,
    ToolResultBlock, ToolUseBlock, UserMessage,
)

from channels import DeliveryOutcome
from drivers.in_casa_driver import LAUNCH_NO_VISIBLE_OUTPUT, InCasaDriver
from engagement_registry import EngagementRecord

pytestmark = [pytest.mark.unit]

NARRATION = "Now let me read the specialist upgrade recipe."
QUESTION = "Fido 0.1.26 is available. Shall I upgrade him now?"


class _Stream:
    """Records every call the driver makes on the turn's stream, in order."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def emit(self, text):
        self.calls.append(("emit", text))

    async def finalize(self, text):
        self.calls.append(("finalize", text))
        return DeliveryOutcome.DELIVERED


def _result():
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="s1", total_cost_usd=0.0, usage={},
        result="", stop_reason="end_turn")


def _say(*blocks, parent=None):
    return AssistantMessage(content=list(blocks), model="m",
                            parent_tool_use_id=parent)


def _call(tid, name="mcp__casa-framework__specialist_upgrade", parent=None):
    return _say(ToolUseBlock(id=tid, name=name, input={}), parent=parent)


def _done(tid):
    return UserMessage(content=[ToolResultBlock(tool_use_id=tid, content="ok")])


class _Client:
    def __init__(self, messages, *, raise_after=False):
        self._messages = messages
        self._raise_after = raise_after

    async def query(self, prompt):
        pass

    async def receive_response(self):
        for m in self._messages:
            yield m
        if self._raise_after:
            raise RuntimeError("stream broke")


def _rec(kind="executor"):
    return EngagementRecord(
        id="e1", kind=kind, role_or_type="configurator", driver="in_casa",
        status="active", topic_id=99, started_at=0.0, last_user_turn_ts=0.0,
        completed_at=None, sdk_session_id=None,
        origin={}, task="t")


def _driver(messages, stream, **client_kw):
    drv = InCasaDriver(topic_stream_factory=lambda tid: stream)
    drv._clients["e1"] = _Client(messages, **client_kw)
    drv._locks["e1"] = asyncio.Lock()
    return drv


async def _turn(messages, *, kind="executor", **client_kw):
    stream = _Stream()
    drv = _driver(messages, stream, **client_kw)
    await drv._deliver_turn(_rec(kind), "go")
    return drv, stream


async def test_a_narration_then_a_call_then_a_question_posts_only_the_question():
    """(a) The casa-test topic: the narration line and the question arrived as
    one message. Only the question is posted, once, and nothing streams."""
    _, stream = await _turn([
        _say(TextBlock(NARRATION)), _call("t1"), _done("t1"),
        _say(TextBlock(QUESTION)), _result()])
    assert stream.calls == [("emit", QUESTION), ("finalize", QUESTION)]


async def test_a_question_written_mid_turn_is_replaced_by_the_closing_text():
    """(b) Position decides, not the words: a question written before the
    turn's last call is interim text too. The prompt tells the model to end
    its turn with any question it has."""
    _, stream = await _turn([
        _say(TextBlock("Which branch should I use?")), _call("t1"), _done("t1"),
        _say(TextBlock("State is active. Proceeding to commit and reload.")),
        _call("t2", "mcp__casa-framework__config_git_commit"), _done("t2"),
        _say(TextBlock("Fido is upgraded and running.")), _result()])
    assert stream.calls == [("emit", "Fido is upgraded and running."), ("finalize", "Fido is upgraded and running.")]


async def test_text_before_a_call_in_the_same_message_is_interim():
    """(c) A message carrying text then a call: the text is before the call.
    Text after the call in the same message is the closing text."""
    _, stream = await _turn([
        _say(TextBlock(NARRATION),
             ToolUseBlock(id="t1", name="Read", input={})),
        _done("t1"),
        _say(ToolUseBlock(id="t2", name="Read", input={}),
             TextBlock(QUESTION)),
        _result()])
    assert stream.calls == [("emit", QUESTION), ("finalize", QUESTION)]


async def test_a_message_that_ends_in_a_call_leaves_no_closing_text():
    _, stream = await _turn([
        _say(TextBlock(QUESTION)),
        _say(TextBlock(NARRATION), ToolUseBlock(id="t1", name="Read", input={})),
        _done("t1"), _result()])
    assert stream.calls == []


async def test_several_text_messages_after_the_last_call_are_all_posted():
    _, stream = await _turn([
        _say(TextBlock(NARRATION)), _call("t1"), _done("t1"),
        _say(TextBlock("I found two versions.")),
        _say(TextBlock(QUESTION)), _result()])
    assert stream.calls == [("emit", f"I found two versions.\n\n{QUESTION}"), ("finalize", f"I found two versions.\n\n{QUESTION}")]


async def test_a_turn_with_no_calls_posts_its_text_as_before():
    _, stream = await _turn([
        _say(TextBlock("Hello.")), _say(TextBlock(QUESTION)), _result()])
    assert stream.calls == [("emit", f"Hello.\n\n{QUESTION}"), ("finalize", f"Hello.\n\n{QUESTION}")]


async def test_a_sub_agents_words_are_never_the_closing_text():
    """A sub-agent writes inside a main-loop call that has not returned."""
    _, stream = await _turn([
        _say(TextBlock(NARRATION)), _call("t1", "Task"),
        _call("t9", "Read", parent="t1"),
        _say(TextBlock("sub-agent finding"), parent="t1"),
        _done("t1"), _result()])
    assert stream.calls == []


async def test_a_turn_with_no_text_after_its_last_call_posts_no_body():
    """Nothing beyond the step log — and the launch is not mute: it wrote
    words, which the closing rule withheld, so no launch death is recorded."""
    drv, stream = await _turn([
        _say(TextBlock(NARRATION)), _call("t1"), _done("t1"), _result()])
    assert stream.calls == []
    assert drv.launch_turn_incomplete("e1") == ""


async def test_a_launch_that_wrote_nothing_at_all_is_still_mute():
    drv, stream = await _turn([_call("t1"), _done("t1"), _result()])
    assert stream.calls == []
    assert drv.launch_turn_incomplete("e1") == LAUNCH_NO_VISIBLE_OUTPUT


async def test_a_completion_turn_posts_nothing_of_its_own():
    """(d) A self-emitted completion: the completion's own summary is posted by
    the finalize tail (unchanged); the turn's text before the call is interim,
    and the closed client ends the stream with no result frame."""
    _, stream = await _turn([
        _say(TextBlock("Committed and reloaded. Finalizing with completion "
                       "summary.")),
        _call("t1", "mcp__casa-framework__emit_completion")])
    assert stream.calls == []


async def test_a_question_written_before_the_stream_broke_is_still_posted():
    """(e) A turn that raises skips the normal finalize. Streamed, its words
    were already on screen; the closing text is posted on the way out and the
    turn's failure still propagates."""
    stream = _Stream()
    drv = _driver([_say(TextBlock(NARRATION)), _call("t1"), _done("t1"),
                   _say(TextBlock(QUESTION))], stream, raise_after=True)
    with pytest.raises(RuntimeError, match="stream broke"):
        await drv._deliver_turn(_rec(), "go")
    assert stream.calls == [("emit", QUESTION), ("finalize", QUESTION)]


async def test_a_failing_post_after_a_broken_stream_keeps_the_turns_own_error():
    class _Failing(_Stream):
        async def finalize(self, text):
            raise ValueError("post failed")

    drv = _driver([_say(TextBlock(QUESTION))], _Failing(), raise_after=True)
    with pytest.raises(RuntimeError, match="stream broke"):
        await drv._deliver_turn(_rec(), "go")


async def test_the_question_reaches_the_topic_when_every_telegram_edit_fails():
    """(e) The REAL topic stream: the closing text is sent raw first, then
    rendered in place by an edit. Streamed, it was a send of the narration and
    an edit to the question — a failed edit left the narration on screen; now
    a failed edit leaves the raw question."""
    from telegram.error import TimedOut
    from test_telegram_topic_stream import _mk_channel_with_fake_bot

    ch, bot = _mk_channel_with_fake_bot()
    bot.edit_message_text = AsyncMock(side_effect=TimedOut("edit timed out"))
    drv = InCasaDriver(topic_stream_factory=ch.create_topic_stream)
    drv._clients["e1"] = _Client([
        _say(TextBlock(NARRATION)), _call("t1"), _done("t1"),
        _say(TextBlock(QUESTION)), _result()])
    drv._locks["e1"] = asyncio.Lock()
    await drv._deliver_turn(_rec(), "go")
    sent = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert sent == [QUESTION]
    assert bot.edit_message_text.await_count == 1
    assert drv.launch_turn_incomplete("e1") == ""


async def test_a_rejected_render_and_a_failed_fallback_leave_the_raw_question():
    """(e) Terra r3: the rich render is refused (an entity BadRequest) and its
    plain fallback times out. The raw closing text already landed first, as a
    streamed block's first emit does, so the question stays on screen."""
    from telegram.error import BadRequest, TimedOut
    from test_telegram_topic_stream import _mk_channel_with_fake_bot

    ch, bot = _mk_channel_with_fake_bot()
    bot.edit_message_text = AsyncMock(
        side_effect=[BadRequest("can't parse entities"), TimedOut("t")])
    drv = InCasaDriver(topic_stream_factory=ch.create_topic_stream)
    drv._clients["e1"] = _Client([
        _say(TextBlock(NARRATION)), _call("t1"), _done("t1"),
        _say(TextBlock("**Shall I** upgrade him now?")), _result()])
    drv._locks["e1"] = asyncio.Lock()
    await drv._deliver_turn(_rec(), "go")
    sent = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert sent == ["**Shall I** upgrade him now?"]
    assert NARRATION not in " ".join(sent)
    assert bot.edit_message_text.await_count == 2


async def test_a_specialists_conversation_still_streams_every_text_block():
    """Out of scope on purpose: a specialist's topic has no step log, and the
    operator chats with it turn by turn."""
    _, stream = await _turn([
        _say(TextBlock(NARRATION)), _call("t1"), _done("t1"),
        _say(TextBlock(QUESTION)), _result()], kind="specialist")
    assert stream.calls == [
        ("emit", NARRATION),
        ("emit", f"{NARRATION}\n\n{QUESTION}"),
        ("finalize", f"{NARRATION}\n\n{QUESTION}"),
    ]


async def test_a_step_log_still_records_the_steps_of_a_closing_only_turn():
    """The step log is what covers the wait while nothing streams."""
    lines: list = []

    class _Line:
        def __init__(self):
            self.steps, self.closed = [], []

        def step(self, activity):
            self.steps.append(activity)

        def finish(self, *, ok):
            self.closed.append(ok)

    def factory(topic_id):
        lines.append(_Line())
        return lines[-1]

    stream = _Stream()
    drv = InCasaDriver(topic_stream_factory=lambda tid: stream,
                       turn_progress_factory=factory)
    drv._clients["e1"] = _Client([
        _say(TextBlock(NARRATION)), _call("t1", "Read"), _done("t1"),
        _say(TextBlock(QUESTION)), _result()])
    drv._locks["e1"] = asyncio.Lock()
    await drv._deliver_turn(_rec(), "go")
    assert lines[0].steps == ["reading files"] and lines[0].closed == [True]
    assert stream.calls == [("emit", QUESTION), ("finalize", QUESTION)]


def test_the_rule_reads_block_positions_only():
    """No word of the text is read: the same words post or not by position."""
    from drivers.in_casa_driver import _fold_closing_text
    tail: list[str] = []
    _fold_closing_text(tail, _say(TextBlock(QUESTION)))
    assert tail == [QUESTION]
    _fold_closing_text(tail, MagicMock(content=[ToolUseBlock(id="x", name="n",
                                                              input={})]))
    assert tail == []
    _fold_closing_text(tail, _say(TextBlock(NARRATION)))
    assert tail == [NARRATION]


class _Hanging(_Client):
    """Yields its messages, then waits forever — a turn still running."""

    async def receive_response(self):
        for m in self._messages:
            yield m
        await asyncio.Event().wait()


async def test_a_question_written_before_the_turn_was_cancelled_is_still_posted():
    """(e) Terra r1: a cancellation skips the normal finalize too. Streamed, the
    question was on screen the moment it was written."""
    stream = _Stream()
    drv = InCasaDriver(topic_stream_factory=lambda tid: stream)
    drv._clients["e1"] = _Hanging([_say(TextBlock(NARRATION)), _call("t1"),
                                   _done("t1"), _say(TextBlock(QUESTION))])
    drv._locks["e1"] = asyncio.Lock()
    task = asyncio.ensure_future(drv._deliver_turn(_rec(), "go"))
    for _ in range(50):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.calls == [("emit", QUESTION), ("finalize", QUESTION)]


async def test_a_hung_post_never_holds_a_cancelled_turn_open(monkeypatch):
    monkeypatch.setattr("drivers.in_casa_driver._CLOSING_POST_TIMEOUT_S", 0.05)

    class _Stuck(_Stream):
        async def finalize(self, text):
            await asyncio.Event().wait()

    drv = InCasaDriver(topic_stream_factory=lambda tid: _Stuck())
    drv._clients["e1"] = _Hanging([_say(TextBlock(QUESTION))])
    drv._locks["e1"] = asyncio.Lock()
    task = asyncio.ensure_future(drv._deliver_turn(_rec(), "go"))
    for _ in range(50):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)


def _long_closing_text():
    from channels.telegram import render_paged
    text = "\n\n".join(["a" * 3000, "b" * 3000, QUESTION])
    assert len(render_paged(text)) >= 2
    return text


async def _deliver_long(bot_side_effect):
    from test_telegram_topic_stream import _mk_channel_with_fake_bot

    ch, bot = _mk_channel_with_fake_bot()
    bot.send_message = AsyncMock(side_effect=bot_side_effect)
    drv = InCasaDriver(topic_stream_factory=ch.create_topic_stream)
    drv._clients["e1"] = _Client([
        _say(TextBlock(NARRATION)), _call("t1"), _done("t1"),
        _say(TextBlock(_long_closing_text())), _result()])
    drv._locks["e1"] = asyncio.Lock()
    await drv._deliver_turn(_rec(), "go")
    return drv, bot


async def test_a_failed_first_page_never_drops_the_question_on_a_later_page():
    """(e) Astra r1: the closing text is never streamed, so a long one is sent
    page by page with no prior message. A first page that fails must not stop
    the page carrying the question."""
    from telegram.error import TimedOut
    ok = MagicMock(message_id=7)
    drv, bot = await _deliver_long([TimedOut("t"), ok, ok, ok])
    texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert len(texts) >= 2
    assert QUESTION in texts[-1]
    assert sum(QUESTION in t for t in texts) == 1
    assert bot.edit_message_text.await_count == 0
    assert drv.launch_turn_incomplete("e1") == ""


async def test_a_long_closing_text_with_every_page_refused_is_not_called_delivered():
    from telegram.error import Forbidden
    from test_telegram_topic_stream import _mk_channel_with_fake_bot

    ch, bot = _mk_channel_with_fake_bot()
    bot.send_message = AsyncMock(side_effect=Forbidden("blocked"))
    outcome = await ch.create_topic_stream(99).finalize(_long_closing_text())
    assert bot.send_message.await_count >= 2
    # Multi-page and nothing landed: UNKNOWN, never DELIVERED (#665 residual).
    assert outcome is DeliveryOutcome.UNKNOWN
    bot.send_message = AsyncMock(
        side_effect=[Forbidden("blocked")] + [MagicMock(message_id=7)] * 5)
    assert (await ch.create_topic_stream(99).finalize(_long_closing_text())
            is DeliveryOutcome.DELIVERED)


async def test_a_long_closing_text_that_lands_is_one_message_per_page():
    from channels.telegram import render_paged
    ok = MagicMock(message_id=7)
    _, bot = await _deliver_long(lambda **kw: ok)
    texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
    assert len(texts) == len(render_paged(_long_closing_text()))
    assert QUESTION in texts[-1]


class _Gated(_Stream):
    """A finalize that blocks until released — a send still in flight."""

    def __init__(self, *, forever=False):
        super().__init__()
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.landed: list[str] = []
        self._forever = forever

    async def finalize(self, text):
        self.calls.append(("finalize", text))
        self.started.set()
        if self._forever:
            await asyncio.Event().wait()
        await self.release.wait()
        self.landed.append(text)
        return DeliveryOutcome.DELIVERED


async def test_a_cancellation_during_the_closing_post_lets_that_post_land_once():
    """(e) Astra r2: cancelled while the closing post is in flight — a stop
    reaching the turn at its very end. The post is not cut and not restarted:
    it lands once, then the cancellation propagates."""
    stream = _Gated()
    drv = InCasaDriver(topic_stream_factory=lambda tid: stream)
    drv._clients["e1"] = _Client([_say(TextBlock(QUESTION)), _result()])
    drv._locks["e1"] = asyncio.Lock()
    task = asyncio.ensure_future(drv._deliver_turn(_rec(), "go"))
    await asyncio.wait_for(stream.started.wait(), 2)
    task.cancel()
    for _ in range(20):
        await asyncio.sleep(0)
    assert not task.done()
    stream.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert stream.calls == [("emit", QUESTION), ("finalize", QUESTION)]
    assert stream.landed == [QUESTION]


async def test_a_hung_closing_post_holds_a_cancellation_only_within_the_bound(
        monkeypatch):
    monkeypatch.setattr("drivers.in_casa_driver._CLOSING_POST_TIMEOUT_S", 0.05)
    stream = _Gated(forever=True)
    drv = InCasaDriver(topic_stream_factory=lambda tid: stream)
    drv._clients["e1"] = _Client([_say(TextBlock(QUESTION)), _result()])
    drv._locks["e1"] = asyncio.Lock()
    task = asyncio.ensure_future(drv._deliver_turn(_rec(), "go"))
    await asyncio.wait_for(stream.started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert stream.calls == [("emit", QUESTION), ("finalize", QUESTION)]
