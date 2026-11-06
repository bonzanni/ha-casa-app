"""Tests for channels.topic_icons — locked Telegram custom_emoji_id map.

Live evidence: 2026-05-13 spike against the production box's bundled PTB inside
addon_91d4d4c8_casa dumped get_forum_topic_icon_stickers() and
returned 111 stickers; these IDs are the curated "Topics" set. None of
the original ROLE_EMOJI executor icons (⚙️ 🛠 🏗 🔁) are in that set,
so the map picks visually equivalent stickers that ARE.
"""
from __future__ import annotations

import pytest


class TestIconIdForRole:
    def test_configurator_maps_to_folder_id(self):
        from channels.topic_icons import icon_id_for_role
        assert icon_id_for_role("configurator") == "5357315181649076022"

    def test_plugin_developer_maps_to_laptop_id(self):
        from channels.topic_icons import icon_id_for_role
        assert icon_id_for_role("plugin-developer") == "5350554349074391003"

    def test_finance_maps_to_money_bag_id(self):
        from channels.topic_icons import icon_id_for_role
        assert icon_id_for_role("finance") == "5350452584119279096"

    def test_unknown_role_returns_default_robot_id(self):
        from channels.topic_icons import icon_id_for_role, DEFAULT_ROLE_ID
        assert icon_id_for_role("totally-unknown-role") == DEFAULT_ROLE_ID
        assert DEFAULT_ROLE_ID == "5309832892262654231"

    def test_empty_string_role_returns_default(self):
        from channels.topic_icons import icon_id_for_role, DEFAULT_ROLE_ID
        assert icon_id_for_role("") == DEFAULT_ROLE_ID


class _FakeSticker:
    def __init__(self, custom_emoji_id, emoji=""):
        self.custom_emoji_id = custom_emoji_id
        self.emoji = emoji


class _FakeBot:
    def __init__(self, stickers, raise_exc=None):
        self._stickers = stickers
        self._raise = raise_exc

    async def get_forum_topic_icon_stickers(self):
        if self._raise is not None:
            raise self._raise
        return self._stickers


class TestVerifyAgainstTelegram:
    @pytest.mark.asyncio
    async def test_all_present_logs_info(self, caplog):
        from channels.topic_icons import (
            verify_against_telegram, ROLE_CUSTOM_EMOJI_ID, DEFAULT_ROLE_ID,
        )
        stickers = [
            _FakeSticker(eid) for eid in
            set(ROLE_CUSTOM_EMOJI_ID.values()) | {DEFAULT_ROLE_ID}
        ]
        bot = _FakeBot(stickers)
        with caplog.at_level("INFO", logger="channels.topic_icons"):
            await verify_against_telegram(bot)
        assert any("verified" in r.message for r in caplog.records)
        assert not any(r.levelname == "WARNING" for r in caplog.records)

    @pytest.mark.asyncio
    async def test_one_missing_logs_warning(self, caplog):
        from channels.topic_icons import (
            verify_against_telegram, ROLE_CUSTOM_EMOJI_ID, DEFAULT_ROLE_ID,
        )
        # Leave out configurator's ID; everything else present.
        configurator_id = ROLE_CUSTOM_EMOJI_ID["configurator"]
        stickers = [
            _FakeSticker(eid) for eid in
            (set(ROLE_CUSTOM_EMOJI_ID.values()) | {DEFAULT_ROLE_ID})
            - {configurator_id}
        ]
        bot = _FakeBot(stickers)
        with caplog.at_level("WARNING", logger="channels.topic_icons"):
            await verify_against_telegram(bot)
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("configurator" in r.message for r in warnings)

    @pytest.mark.asyncio
    async def test_bot_raises_logged_info_not_warning(self, caplog):
        from channels.topic_icons import verify_against_telegram
        bot = _FakeBot([], raise_exc=RuntimeError("synthetic"))
        with caplog.at_level("INFO", logger="channels.topic_icons"):
            await verify_against_telegram(bot)
        # No WARNING on bot failure; only an INFO note.
        assert not any(r.levelname == "WARNING" for r in caplog.records)
        assert any(
            "synthetic" in r.message or "failed" in r.message
            for r in caplog.records
        )
