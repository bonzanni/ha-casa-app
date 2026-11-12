"""A job's cost and running time, shown to the operator and never used to
decide anything (INV-BGJOB-020): on the line that ends the job, in reply to
its start line (which carries the Stop key and is never edited), and on the
end notice to the resident."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from channels.tg_address import TgAddress

pytestmark = pytest.mark.asyncio


class _Bot:
    def __init__(self):
        self.edits: list[dict] = []
        self.sent: list[dict] = []

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.edits.append({"message_id": message_id, "text": str(text)})
        return True

    async def send_message(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(message_id=900 + len(self.sent))


def _rec(status="completed", **job):
    from engagement_registry import JOB_POST_START
    return SimpleNamespace(
        id="c" * 32, status=status, role_or_type="finance", task="t",
        started_at=1000.0, completed_at=1000.0 + 65 * 60,
        origin={"chat_id": "100", "_tg_bot": 111,
                "job": {"title": "Accounting check", **job}},
        ui_posts=[(TgAddress(111, 100, 501), JOB_POST_START)])


@pytest.fixture
def channel(monkeypatch):
    import tools
    from channels.telegram import TelegramChannel
    monkeypatch.setattr(tools, "_display_name_for_role", lambda role: "Finance")
    bot = _Bot()
    return TelegramChannel(bot=bot, chat_id="100", known_bot_id=111), bot


@pytest.mark.parametrize("status,line", [
    ("completed", "✅ Finance finished «Accounting check»"),
    ("cancelled", "🛑 Stopped Finance's «Accounting check»"),
    ("error", "❌ Finance's «Accounting check» failed"),
])
@pytest.mark.parametrize("cost,suffix", [(3.9, " · 1 h 5 min · $3.90"),
                                         (None, " · 1 h 5 min")])
async def test_the_end_line_carries_outcome_time_and_cost(channel, status, line,
                                                          cost, suffix):
    from engagement_registry import JOB_POST_START
    ch, bot = channel
    rec = _rec(status=status, **({} if cost is None else {"cost_usd": cost}))
    await ch._retire_job_post(rec, (TgAddress(111, 100, 501), JOB_POST_START))
    assert bot.edits == []
    (end,) = bot.sent
    assert end["text"] == line + suffix
    assert end["reply_parameters"].message_id == 501


async def test_the_resident_s_end_notice_carries_time_and_cost():
    import tools
    frozen = {"started_at": 1000.0,
              "origin": {"job": {"title": "x", "cost_usd": 3.9}}}
    assert tools._with_job_cost("Done.", frozen, 1000.0 + 65 * 60) == (
        "Done.\n\nJob ran 1 h 5 min · $3.90")
    frozen["origin"]["job"].pop("cost_usd")
    assert tools._with_job_cost("Done.", frozen, 1000.0 + 60) == "Done.\n\nJob ran 1 min"
    assert tools._with_job_cost("Done.", {"started_at": 0, "origin": {}}, 5) == "Done."
