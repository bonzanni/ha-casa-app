"""A job's cost and running time, shown to the operator and never used to
decide anything (INV-BGJOB-020): the start line, edited while the job runs;
the end paint on that line; and the end notice to the resident."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import background_jobs as bj
from channels.tg_address import TgAddress

pytestmark = pytest.mark.asyncio


class _Bot:
    def __init__(self):
        self.edits: list[dict] = []

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.edits.append({"message_id": message_id, "text": str(text),
                           "markup": kw.get("reply_markup")})
        return True


def _rec(status="active", **job):
    from engagement_registry import JOB_POST_START
    return SimpleNamespace(
        id="c" * 32, status=status, role_or_type="finance", task="t",
        started_at=1000.0, completed_at=None,
        origin={"chat_id": "100", "_tg_bot": 111,
                "job": {"title": "Accounting check", **job}},
        ui_posts=[(TgAddress(111, 100, 501), JOB_POST_START)])


@pytest.fixture
def channel(monkeypatch):
    import time
    import tools
    from channels.telegram import TelegramChannel
    monkeypatch.setattr(tools, "_display_name_for_role", lambda role: "Finance")
    monkeypatch.setattr(time, "time", lambda: 1000.0 + 40 * 60)
    bot = _Bot()
    return TelegramChannel(bot=bot, chat_id="100", known_bot_id=111), bot


async def test_the_start_line_shows_elapsed_time_then_the_exact_cost(channel):
    ch, bot = channel
    rec = _rec()
    await ch.refresh_job_start_line(rec)
    rec.origin["job"]["cost_usd"] = 2.4
    await ch.refresh_job_start_line(rec)
    assert [e["text"] for e in bot.edits] == [
        "📊 Finance started «Accounting check». · 40 min",
        "📊 Finance started «Accounting check». · 40 min · $2.40"]
    # the edit carries no button: the job's Stop is its key at the keyboard
    assert [b for row in bot.edits[-1]["markup"].inline_keyboard for b in row] == []


async def test_an_ended_job_s_start_line_is_not_refreshed(channel):
    ch, bot = channel
    await ch.refresh_job_start_line(_rec(status="completed"))
    assert bot.edits == []


async def test_the_end_paint_carries_time_and_cost(channel):
    from engagement_registry import JOB_POST_START
    ch, bot = channel
    rec = _rec(status="completed", cost_usd=3.9)
    rec.completed_at = 1000.0 + 65 * 60
    await ch._retire_job_post(rec, (TgAddress(111, 100, 501), JOB_POST_START))
    assert bot.edits[-1]["text"] == "✅ Finance «Accounting check» · 1 h 5 min · $3.90"


async def test_the_resident_s_end_notice_carries_time_and_cost():
    import tools
    frozen = {"started_at": 1000.0,
              "origin": {"job": {"title": "x", "cost_usd": 3.9}}}
    assert tools._with_job_cost("Done.", frozen, 1000.0 + 65 * 60) == (
        "Done.\n\nJob ran 1 h 5 min · $3.90")
    frozen["origin"]["job"].pop("cost_usd")
    assert tools._with_job_cost("Done.", frozen, 1000.0 + 60) == "Done.\n\nJob ran 1 min"
    assert tools._with_job_cost("Done.", {"started_at": 0, "origin": {}}, 5) == "Done."
