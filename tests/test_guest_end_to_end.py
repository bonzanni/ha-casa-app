"""A guest summons from the update to the group's last edit, and the one log
line each summons leaves behind.

The operator's summons reaches the specialist's real endpoint as an update;
the endpoint answers the guest query with the placeholder, the ticker edits
it while the agent works, and the agent's text — produced by the real
``Agent.handle_message`` over the real transport — is the final edit. A turn
that ends silent declines instead. A friend's summons in a group the
operator allowed takes the same path on a guest run of the real specialist;
past the cap of runs at once it is answered with the busy line and runs
nothing.

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
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import guest_run
from guest_groups import THINKING_WORDS
from output_boundary_testing import admitted
from test_guest_intake import FRIEND, GROUP, OPERATOR, _guest
from test_guest_options import LOOKUP, SEARCH
from test_guest_reply import ASKED_ELSEWHERE, DECLINE, FAIL, WAITING, _Ticks
from test_guest_run import _agent as _guest_agent
from test_guest_run import client  # noqa: F401 — fixture
from test_guest_summons import (  # noqa: F401 — fixtures
    _ask, _endpoint, _turn_that, _turns, broker, names, opening, summoned, world,
)
from two_endpoints import build_two_endpoints

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
    """A ``_process`` that answers *text*; ``handed`` keeps the token
    callback each turn was given."""
    async def _proc(msg, on_token=None, turn_report=None, **kw):
        _proc.handed.append(on_token)
        return text
    _proc.handed = []
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

        answers = _answers("The rate is 1.08.")
        with patch.object(w.agent, "_process", answers):
            await w.agent.handle_message(turn)

    assert [e["text"] for e in w.bot.edits] == [
        f"Judge · {THINKING_WORDS[1]}…", "The rate is 1.08."]
    assert {e["inline_message_id"] for e in w.bot.edits} == {"inl-1"}
    # the turn runs on the operator's own chat, where a reply would be
    # drafted while written; a summoned turn is given no draft stream
    assert answers.handed == [None]
    assert w.bot.sent == [] and w.bot.drafts == []     # nothing in the own chat
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


async def test_someone_elses_summons_is_logged_once_when_its_turn_ends(world, names, caplog):
    ch, bus, bot = _endpoint()
    world.allowed = True
    with caplog.at_level(logging.INFO, logger="channels.telegram"):
        await ch._on_guest_message(_guest(FRIEND), None)
        assert _summons_lines(caplog) == []            # nothing until it ends
        [turn] = _turns(bus, "finance")
        assert turn.channel == "telegram_guest"
        await ch.guest_finish(admitted("VAT is 21%."), None, turn.context)
    assert [e["text"] for e in bot.edits] == ["VAT is 21%."]
    assert _one(caplog, caller=FRIEND)["outcome"] == "guest_answered"


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


# ---------------------------------------------------------------------------
# a friend's summons, run by the real specialist
# ---------------------------------------------------------------------------
#
# Only Telegram (the recording bot) and the SDK client are doubles: the
# summons goes through the real endpoint, the real agent runs its guest turn
# on the locked-down options over the client double, and the text comes back
# through the real transport's ``guest_finish``.

@pytest.fixture
def friends(tmp_path, world, names, client):
    """The specialist (with a guest projection and two tools opened to
    guests) behind its real endpoint and the real transport; the group
    allowed for it."""
    world.allowed = True
    ch, bus, bot = _endpoint()
    agent = _guest_agent(tmp_path)
    agent._channel_manager.register(build_two_endpoints(finance=ch).transport)
    return SimpleNamespace(ch=ch, bus=bus, bot=bot, agent=agent, client=client)


async def _friend_summons(w, update_id=3):
    """A friend's summons, through the handler; the turns now queued."""
    await w.ch._on_guest_message(_guest(FRIEND, update_id=update_id), None)
    return _turns(w.bus, "finance")


def _outcome_line(caplog, outcome: str) -> dict:
    [fields] = [f for _lvl, f in _summons_lines(caplog) if f["outcome"] == outcome]
    return fields


async def test_a_friends_summons_is_answered_in_the_group(friends, monkeypatch, caplog):
    w = friends
    w.client.response_text = "VAT is 21%."
    ticks = _Ticks(monkeypatch)
    with caplog.at_level(logging.INFO, logger="channels.telegram"):
        [turn] = await _friend_summons(w)
        assert turn.channel == guest_run.GUEST_CHANNEL
        [answer] = w.bot.answers                      # the placeholder
        assert answer["guest_query_id"] == "q1"
        assert answer["result"].input_message_content.message_text == "Judge · thinking…"
        assert _summons_lines(caplog) == []           # nothing to say until it ends

        await ticks.tick()
        assert [e["text"] for e in w.bot.edits] == [f"Judge · {THINKING_WORDS[1]}…"]

        real_process = w.agent._process
        handed: list = []

        async def _watched(msg, on_token=None, **kw):
            handed.append(on_token)
            return await real_process(msg, on_token=on_token, **kw)
        with patch.object(w.agent, "_process", _watched):
            await w.agent.handle_message(turn)

    # the turn was given no draft stream, and the real guest run produced
    # the text on the locked-down options
    assert handed == [None]
    assert w.client.attempts == 1
    opts = w.client.captured_options
    assert opts.allowed_tools == [LOOKUP, SEARCH] and opts.mcp_servers == {}
    assert "mentioned you in the group «Test Group»" in opts.system_prompt
    [prompt] = w.client.prompts
    assert prompt.endswith("@FinanceBot how much?")

    assert [e["text"] for e in w.bot.edits] == [
        f"Judge · {THINKING_WORDS[1]}…", "VAT is 21%."]
    assert {e["inline_message_id"] for e in w.bot.edits} == {"inl-1"}
    assert w.bot.sent == [] and w.bot.drafts == []    # nothing in any chat
    assert w.ch._guest_replies == {}
    fields = _one(caplog, caller=FRIEND)
    assert fields["outcome"] == "guest_answered"


async def test_a_friends_silent_run_declines_in_the_group(friends, caplog):
    w = friends
    w.client.response_text = ""
    with caplog.at_level(logging.INFO, logger="channels.telegram"):
        [turn] = await _friend_summons(w)
        await w.agent.handle_message(turn)
    assert w.client.attempts == 1                     # the run happened, and said nothing
    assert [e["text"] for e in w.bot.edits] == [DECLINE]
    assert w.bot.sent == []
    assert w.ch._guest_replies == {}
    assert _one(caplog, caller=FRIEND)["outcome"] == "guest_declined"


async def test_a_friends_summons_past_the_cap_is_answered_busy(friends, monkeypatch, caplog):
    """With as many guest runs live as the cap allows, the next summons is
    answered with the busy line at once: no placeholder bound, no edit, no
    turn, no run. Once the live runs end, a summons is run again."""
    w = friends
    _Ticks(monkeypatch)                               # the tickers wait, never tick
    cap = guest_run.GUEST_RUNS_IN_FLIGHT
    with caplog.at_level(logging.INFO, logger="channels.telegram"):
        live = [turn for n in range(cap) for turn in await _friend_summons(w, 10 + n)]
        assert len(live) == cap and len(w.ch._guest_replies) == cap

        assert await _friend_summons(w, 20) == []     # no turn for the busy summons
        texts = [a["result"].input_message_content.message_text for a in w.bot.answers]
        assert texts == ["Judge · thinking…"] * cap + [guest_run.BUSY_LINE]
        assert w.bot.edits == []
        assert len(w.ch._guest_replies) == cap        # the busy reply is never bound
        assert w.client.attempts == 0                 # nothing ran
        busy = _outcome_line(caplog, "guest_busy")
        assert [f["outcome"] for _l, f in _summons_lines(caplog)] == ["guest_busy"]

        for turn in live:
            await w.agent.handle_message(turn)
        assert w.client.attempts == cap
        assert w.ch._guest_replies == {}
        assert [f["outcome"] for _l, f in _summons_lines(caplog)] == (
            ["guest_busy"] + ["guest_answered"] * cap)

        [again] = await _friend_summons(w, 30)        # a place is free again
        await w.agent.handle_message(again)
    assert w.bot.edits[-1]["text"] == "pong"

    assert busy["role"] == "finance" and busy["caller"] == FRIEND
    assert busy["chat"] == GROUP and busy["title"] == TITLE
    assert isinstance(busy["ms"], int) and busy["ms"] >= 0
