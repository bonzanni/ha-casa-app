"""A guest summons from the update to the group's last edit, and the one log
line each summons leaves behind.

The operator's summons reaches the specialist's real endpoint as an update;
the endpoint answers the guest query with the placeholder, the ticker edits
it while the agent works, and the agent's text — produced by the real
``Agent.handle_message`` over the real transport — is the final edit. A turn
that ends silent declines instead.

Each summons is logged exactly once, by whoever learns its outcome:
``guest summons: role=… caller=… chat=… (title) outcome=… ms=…``. The
terminal arms log the turn's outcome; the dispatcher logs a summons it could
not start; the intake logs one it does not answer (and, at DEBUG, one it
drops).

Bot 222 is the specialist's (``finance``, shown as "Judge"), the operator is
100, the group -5.
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from guest_groups import THINKING_WORDS
from test_guest_intake import FRIEND, GROUP, OPERATOR, _guest
from test_guest_reply import ASKED_ELSEWHERE, DECLINE, FAIL, WAITING, _Ticks
from test_guest_summons import (  # noqa: F401 — fixtures
    _ask, _endpoint, _turn_that, _turns, broker, names, opening, summoned, world,
)

pytestmark = pytest.mark.asyncio

TITLE = "Test Group"
SUMMONS = "guest summons: role=%s caller=%s chat=%s (%s) outcome=%s ms=%d"
FIELDS = ("role", "caller", "chat", "title", "outcome", "ms")


def _summons_lines(caplog) -> list[tuple[int, dict]]:
    """Every summons line logged, as ``(level, fields)``."""
    return [(r.levelno, dict(zip(FIELDS, r.args))) for r in caplog.records
            if r.name == "channels.telegram" and r.msg == SUMMONS]


def _one(caplog, *, level=logging.INFO, caller=OPERATOR) -> dict:
    [(lvl, fields)] = _summons_lines(caplog)
    assert lvl == level
    assert fields["role"] == "finance" and fields["caller"] == caller
    assert fields["chat"] == GROUP and fields["title"] == TITLE
    assert isinstance(fields["ms"], int) and fields["ms"] >= 0
    return fields


async def _summon(w):
    """The operator's summons, through the handler; the turn it queued."""
    await w.ch._on_guest_message(_guest(), None)
    [turn] = _turns(w.bus, "finance")
    return turn


def _answers(text: str):
    async def _proc(msg, on_token=None, turn_report=None, **kw):
        return text
    return _proc


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------

async def test_a_summons_is_answered_in_the_group(summoned, monkeypatch, caplog):
    w = summoned
    ticks = _Ticks(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="channels.telegram"):
        turn = await _summon(w)
        # the placeholder answered the guest query
        [answer] = w.bot.answers
        assert answer["guest_query_id"] == "q1"
        assert answer["result"].input_message_content.message_text == "Judge · thinking…"
        assert _summons_lines(caplog) == []            # nothing to say until it ends

        await ticks.tick()
        assert [e["text"] for e in w.bot.edits] == [f"Judge · {THINKING_WORDS[1]}…"]

        with patch.object(w.agent, "_process", _answers("The rate is 1.08.")):
            await w.agent.handle_message(turn)

    assert [e["text"] for e in w.bot.edits] == [
        f"Judge · {THINKING_WORDS[1]}…", "The rate is 1.08."]
    assert {e["inline_message_id"] for e in w.bot.edits} == {"inl-1"}
    assert w.bot.sent == []                            # nothing in the own chat
    assert w.ch._guest_replies == {}
    assert _one(caplog)["outcome"] == "operator_answered"


async def test_a_silent_turn_declines_in_the_group(summoned, caplog):
    w = summoned
    with caplog.at_level(logging.INFO, logger="channels.telegram"):
        turn = await _summon(w)
        with patch.object(w.agent, "_process", _answers("")):
            await w.agent.handle_message(turn)
    assert w.bot.edits[-1]["text"] == DECLINE
    assert w.ch._guest_replies == {}
    assert _one(caplog)["outcome"] == "operator_declined"


async def test_a_question_asked_in_the_chat_is_logged_as_asked_elsewhere(summoned, broker,
                                                                         caplog):
    w = summoned
    with caplog.at_level(logging.INFO, logger="channels.telegram"):
        turn = await _summon(w)
        with patch.object(w.agent, "_process", _turn_that(_ask, then="")):
            await w.agent.handle_message(turn)
    assert [e["text"] for e in w.bot.edits][-2:] == [WAITING, ASKED_ELSEWHERE]
    assert _one(caplog)["outcome"] == "operator_asked_elsewhere"


async def test_a_turn_that_errors_is_logged_as_failed(summoned, caplog):
    w = summoned

    async def _boom(msg, on_token=None, turn_report=None, **kw):
        raise RuntimeError("the model went away")
    with caplog.at_level(logging.INFO, logger="channels.telegram"):
        turn = await _summon(w)
        with patch.object(w.agent, "_process", _boom):
            await w.agent.handle_message(turn)
    assert w.bot.edits[-1]["text"] == FAIL
    assert _one(caplog)["outcome"] == "operator_failed"


async def test_an_edit_that_never_lands_is_logged_as_failed(summoned, caplog):
    from telegram.error import BadRequest
    w = summoned
    w.bot.script = [BadRequest("Message to edit not found")]
    with caplog.at_level(logging.INFO, logger="channels.telegram"):
        turn = await _summon(w)
        with patch.object(w.agent, "_process", _answers("The rate is 1.08.")):
            await w.agent.handle_message(turn)
    assert _one(caplog)["outcome"] == "operator_failed"


# ---------------------------------------------------------------------------
# a summons that never becomes a turn
# ---------------------------------------------------------------------------

async def test_a_refused_placeholder_is_logged_as_failed(world, opening, caplog):
    ch, bus, bot = _endpoint()
    bot.answer_error = RuntimeError("query expired")
    with caplog.at_level(logging.INFO, logger="channels.telegram"):
        await ch._on_guest_message(_guest(), None)
    assert _turns(bus, "finance") == []
    assert _one(caplog)["outcome"] == "operator_failed"


async def test_a_refused_queue_is_logged_as_failed_once(world, opening, caplog):
    ch, bus, bot = _endpoint(register=False)
    with caplog.at_level(logging.INFO, logger="channels.telegram"):
        await ch._on_guest_message(_guest(), None)
    assert [e["text"] for e in bot.edits] == [FAIL]
    assert _one(caplog)["outcome"] == "operator_failed"


async def test_someone_elses_summons_is_logged_as_not_answered(world, caplog):
    ch, bus, bot = _endpoint()
    world.allowed = True
    with caplog.at_level(logging.INFO, logger="channels.telegram"):
        await ch._on_guest_message(_guest(FRIEND), None)
    assert bot.answers == [] and bot.edits == []
    assert _one(caplog, caller=FRIEND)["outcome"] == "guest_not_answered"


async def test_a_dropped_summons_is_logged_at_debug_only(world, caplog):
    ch, bus, bot = _endpoint()
    with caplog.at_level(logging.INFO, logger="channels.telegram"):
        await ch._on_guest_message(_guest(FRIEND), None)
    assert _summons_lines(caplog) == []                # nothing at INFO
    with caplog.at_level(logging.DEBUG, logger="channels.telegram"):
        await ch._on_guest_message(_guest(FRIEND), None)
    assert bot.answers == []
    assert _one(caplog, level=logging.DEBUG, caller=FRIEND)["outcome"] == "dropped"


async def test_a_summons_on_an_unbound_bot_is_dropped(world, caplog):
    ch, bus, bot = _endpoint()
    world.bound = False
    with caplog.at_level(logging.DEBUG, logger="channels.telegram"):
        await ch._on_guest_message(_guest(), None)
    assert bot.answers == []
    assert _one(caplog, level=logging.DEBUG)["outcome"] == "dropped"
