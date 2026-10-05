"""#1283 controls (#1075 rule 2 on the specialist desk): the closing-silence
rule reads the runner's OWN message list against the words shown, so a
single message that carries prose and a sentinel stays under the whole-text
rule, prose after a sentinel is delivered whole, the #1252 cut composes first,
a truncated reply and a list-less double keep the base reply, and the other
two entry points — an approval continuation and a file turn — reach the same
rule. The real ``handle_reply`` over the real bounded runner and
``_run_delegated_agent`` (``test_pending_approval_desk``'s scripted client),
except where a double is the point.
"""
from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

import specialist_limits
import tools as tools_mod
from test_desk_turn import LABEL, _reply, env  # noqa: F401
from test_pending_approval_desk import (  # noqa: F401
    TOOL, _Scripted, _ask, _call, _result, _text, desk,
)

pytestmark = pytest.mark.asyncio

NARRATION = "The reading was posted to the operator with Apply/Cancel buttons."
TAIL = "WORDS-AFTER-THE-PENDING-CALL"


def _replies(env):
    return [str(m) for m, _ctx in env.channel.replies]


async def test_one_message_with_prose_and_a_sentinel_is_judged_whole(desk):
    """Astra (red-case specify) / N2-2: the same joined text from ONE message
    is not a closing run — the reply is verbatim, as at the base; a list
    re-derived by splitting the text on blank lines would drop the tag."""
    _Scripted.load(_text(NARRATION + "\n\n<silent/>"))
    await _reply(desk, text="the Snelstart Software one is wrong")
    assert _replies(desk) == [f"{LABEL}\n{NARRATION}\n\n<silent/>"]


async def test_two_text_blocks_of_one_message_are_one_message(desk):
    """R8-6: a message's TextBlocks join as written, one list entry per
    message (the resident's granularity), so a sentinel block closing a
    message that also holds prose is not a closing message (measured)."""
    _Scripted.load(AssistantMessage(content=[TextBlock(text=NARRATION),
                                             TextBlock(text="<silent/>")], model="sonnet"))
    await _reply(desk, text="the Snelstart Software one is wrong")
    assert _replies(desk) == [f"{LABEL}\n{NARRATION}<silent/>"]


async def test_prose_after_a_sentinel_message_is_delivered_whole(desk):
    """N2-3 (G-3): only a CLOSING run is dropped; an earlier sentinel stays."""
    _Scripted.load(_text("<silent/>"), _text("Real text"))
    await _reply(desk, text="the Snelstart Software one is wrong")
    assert _replies(desk) == [f"{LABEL}\n<silent/>\n\nReal text"]


async def _cut_reply(desk, *items):
    task = asyncio.create_task(_reply(desk, text="what is the March total?"))
    await asyncio.wait_for(desk.hook_returned.wait(), 10)
    desk.release_result.set()
    await asyncio.wait_for(task, 10)
    return _replies(desk)


async def test_words_after_a_pending_call_never_reach_the_operator_through_the_rule(desk):
    """N2-5 / R8-2: the rule's text is the post-cut ``shown``. A run whose
    FULL list ends in a sentinel after the words written past the pending
    call: rule 2 over the full text would post those words."""
    _Scripted.load(_text(NARRATION), _call("deny-1"), _ask(desk, "deny-1"), _result("deny-1"),
                   _text(TAIL), _text("<silent/>"))
    replies = await _cut_reply(desk)
    assert replies == [f"{LABEL}\n{NARRATION}"]
    assert not any(TAIL in r for r in replies)


async def test_a_closing_sentinel_before_the_pending_call_is_dropped_from_the_kept_words(desk):
    """N2-5, branch 1 (approval_kept joins whole messages): the published list
    is cut at the same index, so rule 2 applies to the kept words."""
    _Scripted.load(_text(NARRATION), _text("<silent/>"), _call("deny-1"), _ask(desk, "deny-1"),
                   _result("deny-1"), _text(TAIL))
    replies = await _cut_reply(desk)
    assert replies == [f"{LABEL}\n{NARRATION}"]


async def test_a_truncated_reply_keeps_the_base_handling(desk, monkeypatch):
    """N2-5 / F-N2: a reply cut at the output cap is not the join of the list,
    so the rule does not apply and the truncated text is posted as before."""
    cap = len(NARRATION) + 6
    monkeypatch.setattr(specialist_limits, "_MAX_OUTPUT_CHARS", cap)
    _Scripted.load(_text(NARRATION), _text("<silent/>"))
    await _reply(desk, text="the Snelstart Software one is wrong")
    assert _replies(desk) == [f"{LABEL}\n{(NARRATION + chr(10) * 2 + '<silent/>')[:cap]}"]


async def test_a_double_that_reports_no_message_list_keeps_the_base_reply(env):
    """N2-6: a DelegatedOutput without the field is judged on its whole text."""
    async def respond(call):
        return tools_mod.DelegatedOutput(text=NARRATION + "\n\n<silent/>")
    env.respond = respond
    await _reply(env)
    assert _replies(env) == [f"{LABEL}\n{NARRATION}\n\n<silent/>"]


async def test_an_approval_continuation_drops_the_closing_sentinel(desk):
    """N2-7: the continuation entry point reaches the same rule."""
    _Scripted.load(_text(NARRATION), _text("<silent/>"))
    await _reply(desk, text="[casa] the operator approved", continuation=True)
    assert _replies(desk) == [f"{LABEL}\n{NARRATION}"]


async def test_a_file_turn_drops_the_closing_sentinel(desk):
    """N2-7: the file desk turn reaches the same rule."""
    _Scripted.load(_text(NARRATION), _text("<silent/>"))
    await _reply(desk, text="[casa] the operator sent you a file", file_name="march.pdf")
    assert _replies(desk) == [f"{LABEL}\n{NARRATION}"]
