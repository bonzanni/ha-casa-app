"""#1283 red case (N2-1): a desk turn whose specialist narrates and then ends with a
closing <silent/> in its own message must never show the operator the literal
sentinel. Real handle_reply, real bounded runner, real _run_delegated_agent,
scripted specialist client (the harness of test_pending_approval_desk)."""
from __future__ import annotations

import pytest

import agent as agent_mod
import result_broker as rb
import specialist_desk as sd
from test_desk_turn import LABEL, OPERATOR, _reply, env  # noqa: F401
from test_pending_approval_desk import _Scripted, _Step, _text, desk  # noqa: F401

pytestmark = pytest.mark.asyncio

NARRATION = "The reading was posted to the operator with Apply/Cancel buttons."


@pytest.mark.parametrize("closing", [1, 2])
@pytest.mark.parametrize("posted", [True, False])
async def test_a_closing_silence_after_narration_never_shows_the_sentinel(desk, posted, closing):
    async def _plugin_post():
        turn = (agent_mod.origin_var.get(None) or {}).get("_delegation_id")
        rb.POSTS.record(turn, rb.PostEvent("c", "probe", "report", LABEL, 1, None))
    items = [_text(NARRATION)]
    if posted:
        items.append(_Step(_plugin_post))
    # the closing run: one sentinel-only message, or two (Astra, red-case
    # specify: the whole trailing run of sentinel-only messages is dropped)
    items.extend(_text("<silent/>") for _ in range(closing))
    _Scripted.load(*items)
    await _reply(desk, text="the Snelstart Software one is wrong")
    shown = [str(m) for m, _ctx in desk.channel.replies]
    print("REPLIES", shown, "NOTICES", desk.channel.notices, "ECHO", sd.prompt_prefix(OPERATOR))
    assert not any("<silent/>" in s for s in shown), shown
    # #1075 rule 2 only (N2-1): the earlier message is kept and only the
    # closing sentinel messages are dropped, whether or not the plugin posted —
    # one labelled reply, exactly the narration
    assert shown == [f"{LABEL}\n{NARRATION}"], shown
