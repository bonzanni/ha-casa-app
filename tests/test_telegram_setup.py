"""Tests for Telegram channel engagement-setup helpers."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


class TestOpenEngagementTopic:
    async def test_creates_topic_with_name_and_icon(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel
        from channels.topic_icons import ROLE_CUSTOM_EMOJI_ID

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        thread_id = await ch.open_engagement_topic(
            name="🟢 Test task",
            role="finance",
        )
        sg = fake_telegram_bot._supergroups[-1001]
        assert thread_id in sg.topics
        assert sg.topics[thread_id].name == "🟢 Test task"
        # v0.37.1 D-1: bubble icon is the numeric custom_emoji_id from
        # the locked map, not a literal char.
        assert sg.topics[thread_id].icon_emoji == ROLE_CUSTOM_EMOJI_ID["finance"]

    async def test_unknown_role_falls_back_to_default_id(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel
        from channels.topic_icons import DEFAULT_ROLE_ID

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        thread_id = await ch.open_engagement_topic(name="x", role="bogus")
        sg = fake_telegram_bot._supergroups[-1001]
        assert sg.topics[thread_id].icon_emoji == DEFAULT_ROLE_ID


class TestSendToTopic:
    async def test_send_to_topic_uses_message_thread_id(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        await ch.send_to_topic(thread_id=555, text="hello from Alex")
        sg = fake_telegram_bot._supergroups[-1001]
        assert sg.messages_by_thread[555] == ["hello from Alex"]


class TestCloseTopic:
    async def test_close_topic_closes_thread(self, fake_telegram_bot):
        """v0.37.1 D-1: close_topic (renamed from close_topic_with_check)
        no longer flips the bubble icon — bubble stays as the role icon
        for the engagement's whole lifecycle; state lives in the title."""
        from channels.telegram import TelegramChannel
        from channels.topic_icons import ROLE_CUSTOM_EMOJI_ID

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        thread_id = await ch.open_engagement_topic(name="x", role="finance")
        await ch.close_topic(thread_id=thread_id)
        sg = fake_telegram_bot._supergroups[-1001]
        assert sg.topics[thread_id].closed is True
        # Bubble remains the role icon — not flipped.
        assert sg.topics[thread_id].icon_emoji == ROLE_CUSTOM_EMOJI_ID["finance"]


class TestEngagementSetup:
    async def test_register_engagement_commands(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        await ch.setup_engagement_features()
        sg = fake_telegram_bot._supergroups[-1001]
        # Two commands registered under the supergroup scope
        scope_key = next(iter(sg.my_commands_by_scope))
        names = [c["command"] for c in sg.my_commands_by_scope[scope_key]]
        assert set(names) == {"cancel", "complete"}

    async def test_missing_manage_topics_logs_error(self, fake_telegram_bot, caplog):
        from channels.telegram import TelegramChannel

        sg = fake_telegram_bot._require_supergroup(-1001)
        sg.bot_can_manage_topics = False

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        await ch.setup_engagement_features()
        assert ch.engagement_permission_ok is False
        assert any("can_manage_topics" in r.message for r in caplog.records)

    async def test_skip_when_supergroup_not_configured(self, fake_telegram_bot, caplog):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=None)
        await ch.setup_engagement_features()
        # No crash. No commands registered anywhere.
        assert fake_telegram_bot._supergroups == {}
