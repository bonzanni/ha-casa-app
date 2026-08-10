# tests/test_steering_downgrade_rebuild.py
"""#369: a steering turn that LOWERS an engagement's clearance must not be
answered from the pre-clamp session.

The clamp (v0.136.0) already gates future reads; what it never did was evict
what the session had already been given — the transcript and the launch-time
archive were fetched at the creator's clearance, and a lower-clearance steerer
could simply ask the engagement to restate them. The ingress therefore, in the
same per-topic-locked pass that clamps the record: tears the live session down,
durably drops the resume pointer, rebuilds a fresh context at the clamped floor
(via the injected rebuilder), and only then delivers the steering turn — with
the rebuild note prepended so the fresh session knows its history is gone.

Red case: removing the invalidate/rebuild orchestration from the steering path
delivers the turn into the old session and fails these tests.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


async def _drain_turns(ch) -> None:
    tasks = list(getattr(ch, "_turn_tasks", ()) or ())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _mk_update(*, chat_id, text, thread_id=None, user_id=77):
    u = MagicMock()
    u.message = MagicMock()
    u.message.chat = MagicMock()
    u.message.chat.id = chat_id
    u.message.text = text
    u.message.message_thread_id = thread_id
    u.message.from_user = MagicMock(id=user_id)
    u.message.message_id = 999
    return u


async def _private_engagement(tmp_path):
    from engagement_registry import EngagementRegistry
    reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
    rec = await reg.create(
        kind="executor", role_or_type="configurator", driver="in_casa",
        task="secret task", topic_id=555,
        origin={"role": "assistant", "channel": "telegram",
                "_origin_route": "telegram", "_origin_clearance": "private"},
    )
    await reg.persist_session_id(rec.id, "sid-preclamp")
    return reg, rec


def _channel(fake_telegram_bot, reg):
    from channels.telegram import TelegramChannel
    ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                         engagement_supergroup_id=-1001)
    ch._engagement_registry = reg
    ch._driver_send_user_turn = AsyncMock()
    ch._driver_invalidate_session = AsyncMock()
    ch._engagement_context_rebuilder = AsyncMock(return_value="[context reset]")
    driver = MagicMock()
    driver.is_alive = MagicMock(return_value=True)
    ch._engagement_driver = driver
    return ch


class TestDowngradeTearsDownAndRebuilds:
    async def test_downgrade_invalidates_clears_sid_rebuilds_then_delivers(
        self, fake_telegram_bot, tmp_path,
    ):
        reg, rec = await _private_engagement(tmp_path)
        ch = _channel(fake_telegram_bot, reg)

        await ch.handle_update(_mk_update(
            chat_id=-1001, text="what do you know?", thread_id=555, user_id=77))
        await _drain_turns(ch)

        # Teardown ran, the resume pointer is durably gone…
        ch._driver_invalidate_session.assert_awaited_once()
        assert reg.get(rec.id).sdk_session_id is None
        # …the context was rebuilt at the clamped floor and unblocked…
        ch._engagement_context_rebuilder.assert_awaited_once()
        assert reg.get(rec.id).context_rebuild_pending is False
        # …and the steering turn WAS delivered, into the fresh session, with
        # the rebuild preamble prepended.
        ch._driver_send_user_turn.assert_awaited_once()
        delivered = ch._driver_send_user_turn.await_args.args[1]
        assert delivered.startswith("[context reset]")
        assert delivered.endswith("what do you know?")

    async def test_operator_turn_triggers_no_teardown(
        self, fake_telegram_bot, tmp_path,
    ):
        reg, rec = await _private_engagement(tmp_path)
        ch = _channel(fake_telegram_bot, reg)

        await ch.handle_update(_mk_update(
            chat_id=-1001, text="carry on", thread_id=555, user_id=100))
        await _drain_turns(ch)

        ch._driver_invalidate_session.assert_not_awaited()
        ch._engagement_context_rebuilder.assert_not_awaited()
        assert reg.get(rec.id).sdk_session_id == "sid-preclamp"
        delivered = ch._driver_send_user_turn.await_args.args[1]
        assert delivered == "carry on"

    async def test_failed_rebuild_refuses_delivery_and_keeps_the_flag(
        self, fake_telegram_bot, tmp_path,
    ):
        """Fail closed: if the fresh context cannot be established, the turn
        is NOT delivered into whatever session state remains, and the flag
        stays set so the next attempt rebuilds again."""
        reg, rec = await _private_engagement(tmp_path)
        ch = _channel(fake_telegram_bot, reg)
        ch._engagement_context_rebuilder = AsyncMock(
            side_effect=RuntimeError("driver down"))

        await ch.handle_update(_mk_update(
            chat_id=-1001, text="what do you know?", thread_id=555, user_id=77))
        await _drain_turns(ch)

        ch._driver_send_user_turn.assert_not_awaited()
        assert reg.get(rec.id).context_rebuild_pending is True

    async def test_preexisting_pending_flag_rebuilds_before_any_turn(
        self, fake_telegram_bot, tmp_path,
    ):
        """A crash between clamp and rebuild leaves the flag set with no
        steering context — the NEXT turn (from anyone, operator included)
        must rebuild before delivery, never resume the old session."""
        reg, rec = await _private_engagement(tmp_path)
        await reg.lower_origin_clearance(rec.id, "public")  # flag set, no rebuild
        ch = _channel(fake_telegram_bot, reg)

        await ch.handle_update(_mk_update(
            chat_id=-1001, text="continue", thread_id=555, user_id=100))
        await _drain_turns(ch)

        ch._engagement_context_rebuilder.assert_awaited_once()
        assert reg.get(rec.id).context_rebuild_pending is False
        ch._driver_send_user_turn.assert_awaited_once()
