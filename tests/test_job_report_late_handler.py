"""#1033's stated residual: the batch a progress report counts for is read
when its handler runs, not when the CLI issued the call.

The pinned SDK hands the handler only its arguments, so a report whose handler
first runs after the next batch's turn holds the engagement's turn lock (the CLI
issued the call, cancelled it and ended its turn before the reader-dispatched
handler task ran) reads that batch and is credited to it. The release text and
``background-jobs.md`` state this exception; this test keeps them honest. A fix
that closes the residual flips it, and the stated exception goes with it.
"""
from __future__ import annotations

import asyncio

import pytest

from test_background_jobs_loop import (  # noqa: F401 — harness is a fixture
    harness, result, text_frame)
from test_job_turn_identity import Detached, ReaderClient, batch_lines, report

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


async def test_a_reply_report_handled_in_the_next_batch_counts_for_it(harness):
    h = harness
    h.client = ReaderClient()
    h.channel.chat_id = 77  # the operator's own message: no clearance clamp
    entered, release, batch_two_runs = (asyncio.Event(), asyncio.Event(),
                                        asyncio.Event())
    replies, seen = [], {}

    async def stalled():
        replies.append(await report("batch stalled", False))

    async def hold():
        entered.set()
        await release.wait()

    async def late_reply_report():
        # Dispatched in the reply's turn, but first runs only once batch 2
        # holds the turn lock.
        await batch_two_runs.wait()
        replies.append(await report("reply moved", True))

    async def batch_two():
        batch_two_runs.set()
        await asyncio.gather(*h.client.detached)

    async def capture():
        seen.update(h.rec.origin["job"])
    h.client.scripts = [
        [text_frame("Working"), stalled, hold, result()],
        [text_frame("Answer"), Detached(late_reply_report), result()],
        [batch_two, result()],
        [capture, h.complete, result()]]
    await h.start()
    await asyncio.wait_for(entered.wait(), 5)
    await h.operator("How is it going?")
    release.set()
    await h.drain()
    assert [r.get("ok") for r in replies] == [True, True], replies
    assert seen["started"] == 3, seen
    assert seen["stuck"] == 0, seen
    assert batch_lines(h, "📊 Batch 2: reply moved") == 1
    assert "Turn failed" not in h.topic()
