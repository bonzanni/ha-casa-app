"""#1433 (operator ruling 2026-10-11, the #1075 rule 1 on the desk): a desk
turn that posted its card and then closes with a ``<silent/>`` message shows
the operator only the card — the words it wrote earlier in the turn are not
posted, and the resident still hears of the post through its echo line. A turn
that does not close in silence still posts its words, card or not. Real
handle_reply, real bounded runner, real _run_delegated_agent, scripted
specialist client (the harness of test_pending_approval_desk)."""
from __future__ import annotations

import pytest

import agent as agent_mod
import result_broker as rb
import specialist_desk as sd
from test_desk_turn import LABEL, OPERATOR, _reply, env  # noqa: F401
from test_pending_approval_desk import _Scripted, _Step, _text, desk  # noqa: F401

pytestmark = pytest.mark.asyncio

NARRATION = "Hanabi is pid 92."


async def _plugin_post():
    turn = (agent_mod.origin_var.get(None) or {}).get("_delegation_id")
    rb.POSTS.record(turn, rb.PostEvent("c", "probe", "report", LABEL, 1, None))


def _replies(desk):
    return [str(m) for m, _ctx in desk.channel.replies]


@pytest.mark.parametrize("before_post", [True, False])
async def test_a_card_then_a_closing_silence_posts_only_the_card(desk, before_post):
    items = [_text(NARRATION), _Step(_plugin_post)] if before_post else \
        [_Step(_plugin_post), _text(NARRATION)]
    _Scripted.load(*items, _text("<silent/>"))
    await _reply(desk, text="Hanabi doesn't fit")
    assert _replies(desk) == []
    assert desk.channel.notices == []
    # the resident's echo names the post, never the dropped words
    echo = sd.prompt_prefix(OPERATOR)
    assert "posted to your chat" in echo and NARRATION not in echo
    assert desk.desk.log[-1].text == sd.POSTED_VIEW


async def test_a_card_and_words_without_a_closing_silence_post_the_words(desk):
    _Scripted.load(_Step(_plugin_post), _text(NARRATION))
    await _reply(desk, text="Hanabi doesn't fit")
    assert _replies(desk) == [f"{LABEL}\n{NARRATION}"]
