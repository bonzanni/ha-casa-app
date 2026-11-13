"""A job's cost and running time, shown to the operator and never used to
decide anything (INV-BGJOB-020): on the end notice to the job's requester,
whose answer is the one message the job's end leaves. The job posts no end
line of its own."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from channels.tg_address import TgAddress

pytestmark = pytest.mark.asyncio


class _Bot:
    def __init__(self):
        self.markups: list[dict] = []
        self.sent: list[dict] = []

    async def edit_message_reply_markup(self, **kw):
        self.markups.append(kw)
        return True

    async def send_message(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(message_id=900 + len(self.sent))


@pytest.mark.parametrize("status", ["completed", "cancelled", "error"])
@pytest.mark.parametrize("kind", ["question", "parked"])
async def test_a_jobs_end_posts_no_line_of_its_own(status, kind):
    from channels.telegram import TelegramChannel
    bot = _Bot()
    ch = TelegramChannel(bot=bot, chat_id="100", known_bot_id=111)
    rec = SimpleNamespace(
        id="c" * 32, status=status, role_or_type="finance", task="t",
        started_at=1000.0, completed_at=1000.0 + 65 * 60,
        origin={"chat_id": "100", "_tg_bot": 111,
                "job": {"title": "Accounting check", "cost_usd": 3.9}})
    await ch._retire_job_post(rec, (TgAddress(111, 100, 501), kind))
    assert bot.sent == []
    assert [m["message_id"] for m in bot.markups] == [501]


async def test_the_resident_s_end_notice_carries_time_and_cost():
    import tools
    frozen = {"started_at": 1000.0,
              "origin": {"job": {"title": "x", "cost_usd": 3.9}}}
    assert tools._with_job_cost("Done.", frozen, 1000.0 + 65 * 60) == (
        "Done.\n\nJob ran 1 h 5 min · $3.90 " + tools.JOB_COST_NOTE)
    frozen["origin"]["job"].pop("cost_usd")
    assert tools._with_job_cost("Done.", frozen, 1000.0 + 60) == (
        "Done.\n\nJob ran 1 min " + tools.JOB_COST_NOTE)
    assert tools._with_job_cost("Done.", {"started_at": 0, "origin": {}}, 5) == "Done."


async def test_the_end_notice_asks_the_narration_to_mention_time_and_cost():
    import tools
    assert tools.JOB_COST_NOTE == (
        "(Casa: mention how long the job ran and what it cost, in your own words.)")
