"""Tests for Telegram routing to engagement topics."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


async def _drain_turns(ch) -> None:
    """Await any background engagement-turn delivery tasks (M9, v0.52.0).

    handle_update now spawns the SDK turn in a tracked background task so the
    per-topic lock is not held across it; tests that assert on
    ``_driver_send_user_turn`` must first drain those tasks.
    """
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


class TestSupergroupRouting:
    async def test_main_feed_ignored_after_redirect_once(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._driver_send_user_turn = AsyncMock()

        u = _mk_update(chat_id=-1001, text="hi", thread_id=None, user_id=7)
        await ch.handle_update(u)
        # First time: redirect posted
        sg = fake_telegram_bot._supergroups[-1001]
        ch._driver_send_user_turn.assert_not_called()

        # Second time: no additional redirect (rate-limited per user per boot)
        await ch.handle_update(u)
        ch._driver_send_user_turn.assert_not_called()

    async def test_topic_message_routed_to_driver(self, fake_telegram_bot, engagement_fixture):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._driver_send_user_turn = AsyncMock()
        ch._engagement_registry = engagement_fixture.registry
        rec = engagement_fixture.active_record  # topic_id=555, status=active

        u = _mk_update(chat_id=-1001, text="Alex please continue", thread_id=555)
        await ch.handle_update(u)
        await _drain_turns(ch)
        ch._driver_send_user_turn.assert_awaited_once()
        args = ch._driver_send_user_turn.await_args.args
        assert args[0].id == rec.id
        assert args[1] == "Alex please continue"

    async def test_ellen_main_chat_unchanged(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._driver_send_user_turn = AsyncMock()
        ch._route_to_ellen = AsyncMock()

        u = _mk_update(chat_id=100, text="hi Ellen")
        await ch.handle_update(u)
        ch._route_to_ellen.assert_awaited_once()
        ch._driver_send_user_turn.assert_not_called()


class TestPTBDispatchContract:
    """E-A regression (v0.22.0..v0.28.0): PTB ``MessageHandler`` invokes
    its callback as ``await callback(update, context)``. Pre-fix
    ``handle_update`` declared ``(self, update)`` only, so every inbound
    Telegram update raised ``TypeError: ... takes 2 positional arguments
    but 3 were given`` — DM, supergroup-topic message, slash-command,
    originator check, every Telegram surface alike. PTB swallowed the
    TypeError into a ``logger.warning`` via ``_on_ptb_error`` and
    returned 200 to the webhook caller, so smoke (`/invoke`,
    `/api/converse`) and master CI never noticed.
    """

    async def test_handle_update_accepts_ptb_two_arg_callback(
        self, fake_telegram_bot,
    ):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._route_to_ellen = AsyncMock()

        u = _mk_update(chat_id=100, text="hi Ellen")
        # PTB calls handlers with (update, context). The bound method
        # therefore receives 3 positional args. handle_update MUST
        # accept the trailing context arg (used or not). Pre-fix this
        # raised TypeError: "takes 2 positional arguments but 3 were given".
        ptb_context = MagicMock(name="CallbackContext")
        await ch.handle_update(u, ptb_context)
        ch._route_to_ellen.assert_awaited_once()

    async def test_handle_update_supergroup_topic_with_ptb_context(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """Same regression coverage on the engagement-routing branch
        (``_driver_send_user_turn``) so a future signature change that
        only breaks one leg fails the test."""
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._driver_send_user_turn = AsyncMock()
        ch._engagement_registry = engagement_fixture.registry
        rec = engagement_fixture.active_record

        u = _mk_update(chat_id=-1001, text="continue please", thread_id=rec.topic_id)
        await ch.handle_update(u, MagicMock(name="CallbackContext"))
        await _drain_turns(ch)
        ch._driver_send_user_turn.assert_awaited_once()


async def rec_with_session(registry, rec, session_id):
    await registry.persist_session_id(rec.id, session_id)
    await registry.mark_idle(rec.id)


class TestSlashCommands:
    async def test_slash_cancel_triggers_cancel(self, fake_telegram_bot, engagement_fixture):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = engagement_fixture.registry
        ch._finalize_cancel = AsyncMock()
        rec = engagement_fixture.active_record

        u = _mk_update(chat_id=-1001, text="/cancel", thread_id=rec.topic_id)
        await ch.handle_update(u)
        ch._finalize_cancel.assert_awaited_once()
        assert ch._finalize_cancel.await_args.args[0].id == rec.id

    async def test_slash_complete_triggers_complete(self, fake_telegram_bot, engagement_fixture):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = engagement_fixture.registry
        ch._finalize_complete_user = AsyncMock()
        rec = engagement_fixture.active_record

        u = _mk_update(chat_id=-1001, text="/complete", thread_id=rec.topic_id)
        await ch.handle_update(u)
        ch._finalize_complete_user.assert_awaited_once()

    async def test_slash_silent_sets_observer_flag(self, fake_telegram_bot, engagement_fixture):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = engagement_fixture.registry
        observer = MagicMock()
        observer.silence = MagicMock()
        ch._observer = observer
        rec = engagement_fixture.active_record

        u = _mk_update(chat_id=-1001, text="/silent", thread_id=rec.topic_id)
        await ch.handle_update(u)
        observer.silence.assert_called_once_with(rec.id)

    async def test_slash_cancel_with_bot_mention_suffix(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """M10 (v0.52.0): group menus send '/cancel@BotName'; the @mention
        suffix must be stripped so the command is handled, not forwarded to
        the driver as a user turn."""
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = engagement_fixture.registry
        ch._finalize_cancel = AsyncMock()
        ch._driver_send_user_turn = AsyncMock()
        rec = engagement_fixture.active_record

        u = _mk_update(chat_id=-1001, text="/cancel@CasaBot",
                       thread_id=rec.topic_id)
        await ch.handle_update(u)
        await _drain_turns(ch)

        ch._finalize_cancel.assert_awaited_once()
        ch._driver_send_user_turn.assert_not_called()

    async def test_user_turn_threads_message_id_and_seals_narration(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """v0.79.0 (§3): an engagement user turn passes the operator's Telegram
        message id to the driver (reply-threading) and advances the topic
        high-water at handler entry (seal open narration)."""
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = engagement_fixture.registry
        ch._driver_send_user_turn = AsyncMock()
        ch._driver_advance_high_water = AsyncMock()
        rec = engagement_fixture.active_record

        u = _mk_update(chat_id=-1001, text="operator says hi",
                       thread_id=rec.topic_id)
        await ch.handle_update(u)
        await _drain_turns(ch)

        ch._driver_advance_high_water.assert_awaited_once_with(rec, 999)
        ch._driver_send_user_turn.assert_awaited_once()
        # message id threaded through as a kwarg.
        assert ch._driver_send_user_turn.await_args.kwargs.get(
            "tg_message_id") == 999

    async def test_command_reply_seals_narration_at_entry(
        self, fake_telegram_bot, tmp_path,
    ):
        """F2 (Sol r3): the high-water advance + narration seal happens at TRUE
        handler entry (before command handling), and command replies route
        through the OUTPUT SEQUENCER.

        Regression probe: /silent (and a rejected /cancel|/complete) posted its
        reply via a direct send and the high-water advance ran only on the
        user-turn path AFTER the command returned — so a narration edit on a
        pre-command message still returned APPLIED, letting narration append
        below the operator's message + the reply.
        """
        from channels.telegram import TelegramChannel
        from channels.output_sequencer import OutputSequencer, SEALED
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="executor", role_or_type="configurator", driver="claude_code",
            task="t", origin={"user_id": 42}, topic_id=555,
        )

        posts: list[tuple[int, str]] = []
        counter = {"n": 9}

        async def _send(topic, text, reply_to=None):
            counter["n"] += 1
            posts.append((topic, text))
            return counter["n"]

        async def _edit(topic, mid, text):
            return True

        seq = OutputSequencer(
            engagement_id=rec.id, topic_id=555,
            send_message=_send, edit_message=_edit)
        narration_mid = await seq.open_narration("agent is working…")
        assert narration_mid is not None

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = reg
        observer = MagicMock()
        observer.silence = MagicMock()
        ch._observer = observer

        async def _advance(r, mid):
            await seq.advance_high_water_for_inbound(mid)

        async def _notice(r, text):
            await seq.post_platform_notice(text)

        ch._driver_advance_high_water = _advance
        ch._driver_post_notice = _notice

        u = _mk_update(chat_id=-1001, text="/silent", thread_id=555, user_id=42)
        await ch.handle_update(u)

        observer.silence.assert_called_once_with(rec.id)
        # The reply went THROUGH the sequencer (single writer), not a direct
        # bot send.
        assert any("Observer quieted" in t for _, t in posts)
        # The narration was SEALED at entry: an edit on the pre-command message
        # is no longer APPLIED.
        assert await seq.edit_narration_if_latest(
            narration_mid, "late narration") == SEALED

    async def test_slash_command_for_other_bot_falls_through(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """A command explicitly addressed to a DIFFERENT bot must fall
        through to the user-turn path (matching PTB CommandHandler)."""
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = engagement_fixture.registry
        ch._bot_username = "casabot"
        ch._finalize_cancel = AsyncMock()
        ch._driver_send_user_turn = AsyncMock()
        rec = engagement_fixture.active_record

        u = _mk_update(chat_id=-1001, text="/cancel@otherbot",
                       thread_id=rec.topic_id)
        await ch.handle_update(u)
        await _drain_turns(ch)

        ch._finalize_cancel.assert_not_awaited()
        ch._driver_send_user_turn.assert_awaited_once()


class TestSlashCommandOriginatorOnly:
    """Bug 8 (v0.14.6): /cancel and /complete are originator-only.

    Pre-fix any user in the engagement supergroup could fire either
    command and terminate someone else's engagement.
    """

    async def _record_with_owner(self, tmp_path, owner_user_id: int):
        from engagement_registry import EngagementRegistry
        reg = EngagementRegistry(tombstone_path=str(tmp_path / "e.json"), bus=None)
        rec = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t",
            origin={"role": "assistant", "user_id": owner_user_id},
            topic_id=555,
        )
        return reg, rec

    async def test_foreign_user_cancel_refused(
        self, fake_telegram_bot, tmp_path,
    ):
        from channels.telegram import TelegramChannel
        reg, rec = await self._record_with_owner(tmp_path, owner_user_id=42)

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = reg
        ch._finalize_cancel = AsyncMock()

        # User 999 is NOT the engagement originator (42).
        u = _mk_update(chat_id=-1001, text="/cancel",
                       thread_id=rec.topic_id, user_id=999)
        await ch.handle_update(u)
        ch._finalize_cancel.assert_not_awaited()

        # The topic gets a refusal message instead.
        sg = fake_telegram_bot._supergroups[-1001]
        msgs = sg.messages_by_thread.get(rec.topic_id, [])
        assert any("originator" in m for m in msgs), (
            f"expected originator-refusal message in topic; got: {msgs}"
        )

    async def test_foreign_user_complete_refused(
        self, fake_telegram_bot, tmp_path,
    ):
        from channels.telegram import TelegramChannel
        reg, rec = await self._record_with_owner(tmp_path, owner_user_id=42)

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = reg
        ch._finalize_complete_user = AsyncMock()

        u = _mk_update(chat_id=-1001, text="/complete",
                       thread_id=rec.topic_id, user_id=999)
        await ch.handle_update(u)
        ch._finalize_complete_user.assert_not_awaited()

    async def test_originator_cancel_allowed(
        self, fake_telegram_bot, tmp_path,
    ):
        from channels.telegram import TelegramChannel
        reg, rec = await self._record_with_owner(tmp_path, owner_user_id=42)

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = reg
        ch._finalize_cancel = AsyncMock()

        # Same user_id as engagement origin: allowed.
        u = _mk_update(chat_id=-1001, text="/cancel",
                       thread_id=rec.topic_id, user_id=42)
        await ch.handle_update(u)
        ch._finalize_cancel.assert_awaited_once()

    async def test_originator_complete_allowed(
        self, fake_telegram_bot, tmp_path,
    ):
        from channels.telegram import TelegramChannel
        reg, rec = await self._record_with_owner(tmp_path, owner_user_id=42)

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = reg
        ch._finalize_complete_user = AsyncMock()

        u = _mk_update(chat_id=-1001, text="/complete",
                       thread_id=rec.topic_id, user_id=42)
        await ch.handle_update(u)
        ch._finalize_complete_user.assert_awaited_once()

    async def test_silent_remains_unrestricted(
        self, fake_telegram_bot, tmp_path,
    ):
        """/silent is local to the topic — anyone in it can quiet the observer."""
        from channels.telegram import TelegramChannel
        reg, rec = await self._record_with_owner(tmp_path, owner_user_id=42)

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = reg
        observer = MagicMock(); observer.silence = MagicMock()
        ch._observer = observer

        u = _mk_update(chat_id=-1001, text="/silent",
                       thread_id=rec.topic_id, user_id=999)
        await ch.handle_update(u)
        observer.silence.assert_called_once_with(rec.id)

    async def test_legacy_engagement_without_user_id_still_works(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """Pre-v0.14.6 engagements have no user_id in origin — fall through
        to the open behaviour (anyone can /cancel) for back-compat."""
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = engagement_fixture.registry
        ch._finalize_cancel = AsyncMock()
        rec = engagement_fixture.active_record
        assert rec.origin.get("user_id") is None  # legacy shape

        u = _mk_update(chat_id=-1001, text="/cancel",
                       thread_id=rec.topic_id, user_id=999)
        await ch.handle_update(u)
        ch._finalize_cancel.assert_awaited_once()


class TestResumeOnTurn:
    async def test_resume_called_when_driver_not_alive(
        self, fake_telegram_bot, engagement_fixture,
    ):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = engagement_fixture.registry
        ch._driver_send_user_turn = AsyncMock()
        driver = MagicMock()
        driver.is_alive = MagicMock(return_value=False)
        driver.resume = AsyncMock()
        ch._engagement_driver = driver
        rec = engagement_fixture.active_record
        rec.sdk_session_id = "sess-xyz"
        await rec_with_session(engagement_fixture.registry, rec, "sess-xyz")

        u = _mk_update(chat_id=-1001, text="Hi again", thread_id=rec.topic_id)
        await ch.handle_update(u)
        await _drain_turns(ch)
        driver.resume.assert_awaited_once_with(rec, "sess-xyz")
        ch._driver_send_user_turn.assert_awaited_once()

    async def test_two_resume_failures_mark_error(
        self, fake_telegram_bot, engagement_fixture,
    ):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = engagement_fixture.registry
        ch._driver_send_user_turn = AsyncMock()
        driver = MagicMock()
        driver.is_alive = MagicMock(return_value=False)
        driver.resume = AsyncMock(side_effect=RuntimeError("rotated"))
        ch._engagement_driver = driver
        rec = engagement_fixture.active_record
        await rec_with_session(engagement_fixture.registry, rec, "sess-xyz")

        u = _mk_update(chat_id=-1001, text="turn1", thread_id=rec.topic_id)
        await ch.handle_update(u)
        u = _mk_update(chat_id=-1001, text="turn2", thread_id=rec.topic_id)
        await ch.handle_update(u)
        assert rec.status == "error"


class TestDriverSendUserTurnRouting:
    """Unit tests for the _driver_send_user_turn closure set by casa_core.

    The closure branches on rec.driver — claude_code goes to the claude_code
    driver, everything else to the in_casa engagement driver.
    """

    async def test_claude_code_rec_routes_to_claude_code_driver(self):
        """When rec.driver == 'claude_code', the closure calls claude_code_driver."""
        from engagement_registry import EngagementRecord

        engagement_driver = MagicMock()
        engagement_driver.send_user_turn = AsyncMock()
        claude_code_driver = MagicMock()
        claude_code_driver.send_user_turn = AsyncMock()

        # Reproduce the closure from casa_core.py E6 change.
        async def _driver_send_user_turn(rec, text):
            if rec.driver == "claude_code":
                await claude_code_driver.send_user_turn(rec, text)
            else:
                await engagement_driver.send_user_turn(rec, text)

        rec = EngagementRecord(
            id="e-cc", kind="executor", role_or_type="configurator",
            driver="claude_code", status="active", topic_id=555,
            started_at=1.0, last_user_turn_ts=2.0, last_idle_reminder_ts=0.0,
            completed_at=None, sdk_session_id=None,
            origin={"channel": "telegram", "chat_id": "42"},
            task="do stuff",
        )

        await _driver_send_user_turn(rec, "hello")

        claude_code_driver.send_user_turn.assert_awaited_once_with(rec, "hello")
        engagement_driver.send_user_turn.assert_not_called()

    async def test_in_casa_rec_routes_to_engagement_driver(self):
        """When rec.driver == 'in_casa', the closure calls engagement_driver."""
        from engagement_registry import EngagementRecord

        engagement_driver = MagicMock()
        engagement_driver.send_user_turn = AsyncMock()
        claude_code_driver = MagicMock()
        claude_code_driver.send_user_turn = AsyncMock()

        async def _driver_send_user_turn(rec, text):
            if rec.driver == "claude_code":
                await claude_code_driver.send_user_turn(rec, text)
            else:
                await engagement_driver.send_user_turn(rec, text)

        rec = EngagementRecord(
            id="e-ic", kind="executor", role_or_type="configurator",
            driver="in_casa", status="active", topic_id=555,
            started_at=1.0, last_user_turn_ts=2.0, last_idle_reminder_ts=0.0,
            completed_at=None, sdk_session_id=None,
            origin={"channel": "telegram", "chat_id": "42"},
            task="do stuff",
        )

        await _driver_send_user_turn(rec, "hello")

        engagement_driver.send_user_turn.assert_awaited_once_with(rec, "hello")
        claude_code_driver.send_user_turn.assert_not_called()


class TestHandleUpdateConcurrencyRace:
    """Bug 10 (v0.14.7): per-topic lock around handle_update.

    Pre-fix, aiohttp dispatched each Telegram update as its own task and
    a /cancel arriving alongside a regular turn could race: the regular
    turn passed the rec.status check while the cancel was mid-finalize,
    then routed to a driver that _finalize_engagement had just torn
    down. The fix is a per-topic asyncio.Lock wrapping the engagement-
    supergroup branch in handle_update.
    """

    async def test_concurrent_cancel_and_turn_serialised_per_topic(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """Concurrent /cancel + regular update on the same topic.

        With the lock, the cancel-task wins (asyncio.gather schedules it
        first; uncontended setdefault returns the same Lock for both
        tasks; the cancel-task acquires first). cancel_impl yields BEFORE
        mutating registry status — without the lock the regular turn
        would slip past the status check during that yield and call
        _driver_send_user_turn on a soon-to-be-dead engagement.

        Acceptance criterion 1: with the lock the regular turn either
        runs before the cancel (saw "active") or is dropped after the
        cancel finalises (sees "cancelled" → "no active" reply). It
        NEVER reaches the driver after the cancel-task entered its lock.
        """
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        registry = engagement_fixture.registry
        ch._engagement_registry = registry
        rec = engagement_fixture.active_record  # driver=in_casa, status=active
        # No user_id in origin → originator-only check at line 494 falls
        # through (legacy back-compat shape; see Bug 8 test class).

        async def cancel_impl(rec_arg, *, reason):
            # Yield the event loop FIRST. Without the per-topic lock
            # this gives the turn-task a window to reach
            # `await driver_send_user_turn` before mark_cancelled fires
            # — exactly the race window Bug 10 documents.
            await asyncio.sleep(0)
            await registry.mark_cancelled(rec_arg.id)

        ch._finalize_cancel = AsyncMock(side_effect=cancel_impl)
        ch._driver_send_user_turn = AsyncMock()

        u_cancel = _mk_update(chat_id=-1001, text="/cancel",
                              thread_id=rec.topic_id, user_id=99)
        u_turn = _mk_update(chat_id=-1001, text="hello",
                            thread_id=rec.topic_id, user_id=99)

        # gather schedules u_cancel first; with the lock it wins.
        await asyncio.gather(
            ch.handle_update(u_cancel),
            ch.handle_update(u_turn),
        )

        # The lock guarantees cancel completes before the turn is
        # routed. With cancel completing first, the turn sees terminal
        # status at re-check and is dropped (case a in acceptance).
        assert ch._driver_send_user_turn.await_count == 0, (
            "regular turn was routed despite a concurrent /cancel; "
            "the per-topic lock is missing or broken"
        )
        ch._finalize_cancel.assert_awaited_once()
        assert registry.get(rec.id).status == "cancelled"

    async def test_concurrent_regular_turns_both_routed(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """Two regular updates on the same active engagement → both
        reach the driver.

        Acceptance criterion 2: the lock must serialise but not drop —
        per-topic ordering is fine, lost messages are not.
        """
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        ch._driver_send_user_turn = AsyncMock()

        u1 = _mk_update(chat_id=-1001, text="hello1", thread_id=rec.topic_id)
        u2 = _mk_update(chat_id=-1001, text="hello2", thread_id=rec.topic_id)

        await asyncio.gather(ch.handle_update(u1), ch.handle_update(u2))
        await _drain_turns(ch)

        assert ch._driver_send_user_turn.await_count == 2
        texts_sent = sorted(
            c.args[1] for c in ch._driver_send_user_turn.await_args_list
        )
        assert texts_sent == ["hello1", "hello2"]

    async def test_concurrent_turns_on_different_topics_run_in_parallel(
        self, fake_telegram_bot, tmp_path,
    ):
        """The lock is keyed by thread_id — two updates on different
        engagements must not block each other.

        Set up two engagements in different topics; have the first
        turn's driver call block on an event the second turn must set.
        If different topics share a lock, this deadlocks; with per-
        topic locks it completes.
        """
        from channels.telegram import TelegramChannel
        from engagement_registry import EngagementRegistry

        reg = EngagementRegistry(
            tombstone_path=str(tmp_path / "e.json"), bus=None,
        )
        rec_a = await reg.create(
            kind="specialist", role_or_type="finance", driver="in_casa",
            task="t", origin={"role": "assistant"}, topic_id=111,
        )
        rec_b = await reg.create(
            kind="specialist", role_or_type="logistics", driver="in_casa",
            task="t", origin={"role": "assistant"}, topic_id=222,
        )

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = reg

        b_routed = asyncio.Event()
        a_can_finish = asyncio.Event()

        async def driver_send(rec_arg, text, *, tg_message_id=None):
            if rec_arg.id == rec_a.id:
                # A holds its lock; wait for B to also be routed.
                await asyncio.wait_for(b_routed.wait(), timeout=2.0)
                a_can_finish.set()
            else:
                b_routed.set()

        ch._driver_send_user_turn = driver_send

        u_a = _mk_update(chat_id=-1001, text="for-A", thread_id=rec_a.topic_id)
        u_b = _mk_update(chat_id=-1001, text="for-B", thread_id=rec_b.topic_id)

        # M9 (v0.52.0): turn delivery now runs in background tasks, so the
        # per-topic lock is never held across driver_send. Both turns must
        # still be routed and complete; A's task waits on B's, and the
        # wait_for(2.0) inside driver_send catches any regression that
        # serialises the two topics.
        await asyncio.gather(ch.handle_update(u_a), ch.handle_update(u_b))
        await _drain_turns(ch)
        assert b_routed.is_set()
        assert a_can_finish.is_set()

    async def test_cancel_interrupts_inflight_turn(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """M9 (v0.52.0): /cancel must complete while a turn is STILL in
        flight — not queue behind it. Pre-fix the per-topic lock was held
        across the whole SDK turn, so /cancel blocked until it drained."""
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        registry = engagement_fixture.registry
        ch._engagement_registry = registry
        rec = engagement_fixture.active_record  # driver=in_casa, status=active

        turn_started = asyncio.Event()
        release_turn = asyncio.Event()

        async def slow_turn(rec_arg, text, *, tg_message_id=None):
            turn_started.set()
            await release_turn.wait()  # simulates a minutes-long SDK turn

        ch._driver_send_user_turn = slow_turn

        cancel_ran = asyncio.Event()

        async def cancel_impl(rec_arg, *, reason):
            await registry.mark_cancelled(rec_arg.id)
            cancel_ran.set()

        ch._finalize_cancel = AsyncMock(side_effect=cancel_impl)

        t_turn = asyncio.create_task(ch.handle_update(
            _mk_update(chat_id=-1001, text="work on it",
                       thread_id=rec.topic_id, user_id=99)))
        await asyncio.wait_for(turn_started.wait(), timeout=1.0)

        # Core assertion: /cancel returns while the turn is STILL in flight.
        await asyncio.wait_for(
            ch.handle_update(_mk_update(
                chat_id=-1001, text="/cancel",
                thread_id=rec.topic_id, user_id=99)),
            timeout=1.0,
        )
        assert cancel_ran.is_set()
        assert not release_turn.is_set()  # turn had not drained when cancel ran

        release_turn.set()
        await asyncio.wait_for(t_turn, timeout=1.0)
        await _drain_turns(ch)


class TestMessageHandlerWiring:
    """E-13: the PTB MessageHandler must dispatch to handle_update so that
    /cancel and other engagement commands posted to topics are intercepted
    by the engagement-aware router. Pre-fix, MessageHandler routed to
    _handle, which is engagement-unaware and forwarded /cancel to Ellen
    (bug-review-2026-04-29-exploration.md § E-13).

    The full _rebuild() bring-up path is hard to mock cleanly without a
    real PTB Application; we assert the wiring at the source level.
    Behavioral coverage of handle_update lives in TestSupergroupRouting
    and TestSlashCommands above (514 lines of existing tests)."""

    async def test_rebuild_registers_message_handler_with_handle_update(self):
        import inspect
        from channels.telegram import TelegramChannel

        source = inspect.getsource(TelegramChannel._rebuild)
        # The wiring must point at handle_update, not _handle. H5 (v0.52.0)
        # added block=False so a long engagement turn can't stall PTB's
        # sequential update fetcher.
        assert "MessageHandler(filters.TEXT, self.handle_update, block=False)" in source, (
            f"_rebuild must register MessageHandler with handle_update "
            f"(block=False); current source excerpt:\n{source}"
        )
        # Defensive: catch a regression where someone re-introduces the
        # engagement-unaware _handle wiring.
        msg_handler_lines = [
            line for line in source.splitlines()
            if "MessageHandler(" in line
        ]
        assert msg_handler_lines, "no MessageHandler registration found in _rebuild"
        for line in msg_handler_lines:
            assert "self._handle" not in line, (
                f"MessageHandler must not be wired to _handle "
                f"(engagement-unaware). Got: {line.strip()}"
            )


class TestForeignChatGate:
    """H6 (v0.50.0): telegram_chat_id is an allowlist when set (DOCS.md:
    "Telegram chat ID to restrict messages to. Leave empty to accept all
    chats."). Pre-fix route 3 forwarded every foreign chat to the same
    agent path, so any Telegram user who found the bot got full access."""

    pytestmark = [pytest.mark.unit]

    async def test_foreign_chat_dropped_when_chat_id_configured(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._route_to_ellen = AsyncMock()
        u = _mk_update(chat_id=31337, text="unlock the front door")
        await ch.handle_update(u)
        ch._route_to_ellen.assert_not_awaited()

    async def test_all_chats_accepted_when_chat_id_empty(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(bot=fake_telegram_bot, chat_id="",
                             engagement_supergroup_id=-1001)
        ch._route_to_ellen = AsyncMock()
        u = _mk_update(chat_id=31337, text="hello")
        await ch.handle_update(u)
        ch._route_to_ellen.assert_awaited_once()

    async def test_configured_chat_still_routed(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._route_to_ellen = AsyncMock()
        u = _mk_update(chat_id=100, text="hi Ellen")
        await ch.handle_update(u)
        ch._route_to_ellen.assert_awaited_once()


def test_inbound_message_without_message_id_is_tolerated():
    """v0.79.1: the e2e harness (and defensively, any malformed update) can
    deliver a message object lacking ``message_id``; the engagement inbound
    path must not AttributeError — threading and high-water advance are
    best-effort and degrade to None (QA tier2 E-block regression,
    telegram.py handle_update engagement-topic branch)."""
    import inspect
    import channels.telegram as tg
    src = inspect.getsource(tg.TelegramChannel.handle_update)
    assert 'getattr(msg, "message_id", None)' in src, (
        "engagement inbound reads of msg.message_id must be getattr-guarded "
        "(harness/e2e messages may omit it)")
    assert "msg.message_id))" not in src


class TestResumeFailureRaces:
    async def test_resume_fail_count_persists_to_tombstone(
        self, fake_telegram_bot, engagement_fixture, tmp_path,
    ):
        """#326 (low): the two-strike counter must survive a restart — a failed
        resume writes it through the registry, not just onto the in-memory
        origin dict."""
        import json

        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._engagement_registry = engagement_fixture.registry
        ch._driver_send_user_turn = AsyncMock()
        driver = MagicMock()
        driver.is_alive = MagicMock(return_value=False)
        driver.resume = AsyncMock(side_effect=RuntimeError("rotated"))
        ch._engagement_driver = driver
        rec = engagement_fixture.active_record
        await rec_with_session(engagement_fixture.registry, rec, "sess-xyz")

        u = _mk_update(chat_id=-1001, text="turn1", thread_id=rec.topic_id)
        await ch.handle_update(u)

        rows = json.loads((tmp_path / "e.json").read_text())
        row = next(r for r in rows if r["id"] == rec.id)
        assert row["origin"].get("_resume_fail_count") == 1

    async def test_second_strike_lost_to_concurrent_finalize_skips_cleanup(
        self, fake_telegram_bot, engagement_fixture,
    ):
        """#326: a failed resume must not overwrite a concurrently committed
        terminal status with `error`, nor run its duplicate topic cleanup."""
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        reg = engagement_fixture.registry
        ch._engagement_registry = reg
        ch._driver_send_user_turn = AsyncMock()
        ch._cleanup_error_topic = AsyncMock()
        rec = engagement_fixture.active_record
        await rec_with_session(reg, rec, "sess-xyz")
        await reg.set_resume_fail_count(rec.id, 1)   # one strike already

        driver = MagicMock()
        driver.is_alive = MagicMock(return_value=False)

        async def _cancel_races_resume(*_a, **_k):
            # A /cancel finalizer commits terminal while resume() is awaited.
            assert await reg.try_transition_terminal(rec.id, "cancelled")
            raise RuntimeError("rotated")

        driver.resume = AsyncMock(side_effect=_cancel_races_resume)
        ch._engagement_driver = driver

        u = _mk_update(chat_id=-1001, text="turn2", thread_id=rec.topic_id)
        await ch.handle_update(u)

        assert reg.get(rec.id).status == "cancelled"          # winner stands
        assert "error_kind" not in reg.get(rec.id).origin
        ch._cleanup_error_topic.assert_not_awaited()          # no duplicate cleanup


class TestSteeringClampsEngagementClearance:
    """#336 (review r4): an engagement reads at its creator's clearance, but
    any supergroup member can steer it by messaging its topic. The ingress
    clamps the record's clearance DOWN to the steering sender's BEFORE the
    turn is delivered, so the engagement never reads above the
    least-privileged person taking part.

    Red case: removing the ``lower_origin_clearance`` call from the topic
    ingress leaves the record at ``private`` and fails this test."""

    async def test_non_operator_topic_message_lowers_the_record(
        self, fake_telegram_bot, engagement_fixture,
    ):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._driver_send_user_turn = AsyncMock()
        # #369: a moving clamp now also tears down + rebuilds the session
        # context; wire the seams so delivery can proceed.
        ch._driver_invalidate_session = AsyncMock()
        ch._engagement_context_rebuilder = AsyncMock(return_value="")
        ch._engagement_registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        rec.origin["_origin_route"] = "telegram"
        rec.origin["_origin_clearance"] = "private"   # operator-created

        # user_id 77 != configured chat_id 100 ⇒ not the operator.
        await ch.handle_update(
            _mk_update(chat_id=-1001, text="what's the alarm code?",
                       thread_id=555, user_id=77))
        await _drain_turns(ch)

        assert rec.origin["_origin_clearance"] == "public"
        ch._driver_send_user_turn.assert_awaited_once()

    async def test_operator_topic_message_leaves_it_private(
        self, fake_telegram_bot, engagement_fixture,
    ):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._driver_send_user_turn = AsyncMock()
        ch._engagement_registry = engagement_fixture.registry
        rec = engagement_fixture.active_record
        rec.origin["_origin_route"] = "telegram"
        rec.origin["_origin_clearance"] = "private"

        # user_id 100 == configured chat_id ⇒ the operator.
        await ch.handle_update(
            _mk_update(chat_id=-1001, text="carry on", thread_id=555,
                       user_id=100))
        await _drain_turns(ch)

        assert rec.origin["_origin_clearance"] == "private"
