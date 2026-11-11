"""Casa's notes for the agent that owns a chat: kept per (bot, chat), drained
once, oldest first, bounded."""
from __future__ import annotations

import pytest

import chat_notes


@pytest.fixture(autouse=True)
def fresh():
    chat_notes.reset()
    yield
    chat_notes.reset()


def test_notes_are_kept_per_bot_and_chat_and_drained_once():
    chat_notes.record(222, 100, "Ellen asked you «the march total»; you answered (412 chars)")
    chat_notes.record(222, 100, "second")
    chat_notes.record(111, 100, "on another bot")         # the same chat id on another bot
    chat_notes.record(222, 7, "another chat")
    assert chat_notes.drain(222, 100) == [
        "Ellen asked you «the march total»; you answered (412 chars)", "second"]
    assert chat_notes.drain(222, 100) == []                 # drained once
    assert chat_notes.drain(111, 100) == ["on another bot"]
    assert chat_notes.drain(222, 7) == ["another chat"]


@pytest.mark.parametrize("bot, chat", [(0, 100), (222, 0), (True, 100), ("222", 100), (222, None)])
def test_ids_that_are_not_positive_ints_keep_nothing(bot, chat):
    chat_notes.record(bot, chat, "line")
    assert chat_notes.drain(bot, chat) == []


def test_an_empty_line_is_not_a_note():
    chat_notes.record(222, 100, "   ")
    assert chat_notes.drain(222, 100) == []


def test_a_long_line_is_clipped_with_a_marker():
    chat_notes.record(222, 100, "x" * 1000)
    (line,) = chat_notes.drain(222, 100)
    assert len(line) == chat_notes.LINE_MAX and line.endswith(chat_notes.CLIP)


def test_a_drain_is_bounded_and_counts_the_rest():
    for i in range(chat_notes.MAX_LINES + 3):
        chat_notes.record(222, 100, f"n{i}")
    lines = chat_notes.drain(222, 100)
    assert lines[:chat_notes.MAX_LINES] == [f"n{i}" for i in range(chat_notes.MAX_LINES)]
    assert lines[-1] == "…and 3 more."


def test_a_chat_keeps_its_newest_notes():
    for i in range(chat_notes.MAX_EVENTS + 5):
        chat_notes.record(222, 100, f"n{i}")
    lines = chat_notes.drain(222, 100)
    assert lines[0] == "n5"                                 # the five oldest dropped
    assert lines[-1] == f"…and {chat_notes.MAX_EVENTS - chat_notes.MAX_LINES} more."
