"""#1342 red case: a desk reply made of words followed by ``<silent/>`` in the
SAME message (the live m2622 shape) never shows the operator the tag; its words
are still the reply, as #1283 keeps a reply with real content. Real
handle_reply, real bounded runner, real _run_delegated_agent, scripted
specialist client (the harness of test_pending_approval_desk)."""
from __future__ import annotations

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

import agent as agent_mod
import result_broker as rb
from test_desk_turn import LABEL, _reply, env  # noqa: F401
from test_pending_approval_desk import _Scripted, _Step, _text, desk  # noqa: F401

pytestmark = pytest.mark.asyncio

NARRATION = "The reading was posted with Apply and Cancel for your review."


def _replies(env):
    return [str(m) for m, _ctx in env.channel.replies]


@pytest.mark.parametrize("posted", [True, False])
async def test_words_then_a_sentinel_in_one_message_post_the_words_only(desk, posted):
    async def _plugin_post():
        turn = (agent_mod.origin_var.get(None) or {}).get("_delegation_id")
        rb.POSTS.record(turn, rb.PostEvent("c", "probe", "report", LABEL, 1, None))
    items = [_Step(_plugin_post)] if posted else []
    items.append(_text(NARRATION + "\n\n<silent/>"))
    _Scripted.load(*items)
    await _reply(desk, text="the Snelstart Software one is wrong")
    assert _replies(desk) == [f"{LABEL}\n{NARRATION}"]


async def test_a_sentinel_block_closing_a_message_with_words_is_dropped(desk):
    _Scripted.load(AssistantMessage(content=[TextBlock(text=NARRATION),
                                             TextBlock(text="<silent/>")], model="sonnet"))
    await _reply(desk, text="the Snelstart Software one is wrong")
    assert _replies(desk) == [f"{LABEL}\n{NARRATION}"]


async def test_words_after_a_sentinel_are_posted_without_it(desk):
    _Scripted.load(_text("<silent/>"), _text("Real text"))
    await _reply(desk, text="the Snelstart Software one is wrong")
    assert _replies(desk) == [f"{LABEL}\nReal text"]


async def test_a_crlf_closing_marker_is_dropped(desk):
    _Scripted.load(_text("Before\r\nAfter.\r\n<silent/>\r\n"))
    await _reply(desk, text="the Snelstart Software one is wrong")
    assert _replies(desk) == [f"{LABEL}\nBefore\r\nAfter."]


async def test_a_leading_marker_takes_its_blank_lines_and_keeps_the_indentation(desk):
    """x4 (Terra): whitespace-only lines after a leading marker go; the first
    line of words keeps its indentation."""
    _Scripted.load(_text("<silent/>\r\n\t \r\n    code()\r\nWords"))
    await _reply(desk, text="the Snelstart Software one is wrong")
    assert _replies(desk) == [f"{LABEL}\n    code()\r\nWords"]


@pytest.mark.parametrize("words", [
    "The marker is `<silent/>`; keep `a  b`.",
    "[<silent/>](https://example.com) real",
    "The marker is <silent\\/>.",
    "The marker is \\<silent/>",
    "    keep_this_code()",
    "```py\ns = \"\"\"a\n\n\nb\"\"\"\n```\nDone.",
])
async def test_only_the_marker_at_an_end_goes_and_nothing_else_is_touched(desk, words):
    """x1/x2 (Astra, Terra): a quoted, code-span, link or escaped sentinel is
    content, and the words before a closing marker keep their indentation,
    line endings and code whitespace exactly."""
    _Scripted.load(_text(words + "\n<silent/>"))
    await _reply(desk, text="the Snelstart Software one is wrong")
    assert _replies(desk) == [f"{LABEL}\n{words}"]


async def test_a_marker_between_words_is_left_as_written(desk):
    """Not covered, by design: a sentinel between two runs of words (a G-3
    recant inside the reply) is not at an end and is posted as written."""
    _Scripted.load(_text(NARRATION), _text("<silent/>"), _text("Real text"))
    await _reply(desk, text="the Snelstart Software one is wrong")
    assert _replies(desk) == [f"{LABEL}\n{NARRATION}\n\n<silent/>\n\nReal text"]
