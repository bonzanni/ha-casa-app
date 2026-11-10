"""Tests for Telegram routing: the operator's direct chat reaches the
assistant; the optional log group is read-only."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

REDIRECT = "This group is a read-only log. Talk to me in our direct chat."


class _SubmitDriver:
    """The job driver's synchronous seam entry, recorded: anything handed to
    a job goes through ``submit_operator``."""

    def __init__(self):
        self.submitted: list[tuple] = []

    def submit_operator(self, rec, text, *, tg_message_id=None, notice=None):
        self.submitted.append((rec, text, tg_message_id))
        return object()


def _wire_driver(ch) -> _SubmitDriver:
    drv = _SubmitDriver()
    ch._engagement_driver = drv
    return drv


def _mk_update(*, chat_id, text, thread_id=None, user_id=77, is_bot=False):
    u = MagicMock()
    u.message = MagicMock()
    u.message.chat = MagicMock()
    u.message.chat.id = chat_id
    u.message.text = text
    u.message.message_thread_id = thread_id
    u.message.from_user = MagicMock(id=user_id, is_bot=is_bot)
    u.message.message_id = 999
    return u


def _log_group_channel(bot, engagement_fixture=None):
    from channels.telegram import TelegramChannel

    ch = TelegramChannel(bot=bot, chat_id=100, engagement_supergroup_id=-1001)
    drv = _wire_driver(ch)
    ch._route_to_assistant = AsyncMock()
    if engagement_fixture is not None:
        ch._engagement_registry = engagement_fixture.registry
    return ch, drv


def _redirects(bot) -> list:
    """Every redirect line sent, as (thread or None, text)."""
    sg = bot._supergroups.get(-1001)
    out = [(None, t) for c, t in bot.messages if c == -1001]
    if sg is not None:
        out += [(tid, t) for tid, texts in sg.messages_by_thread.items()
                for t in texts]
    return out


class TestTheLogGroupIsReadOnly:
    """Anything a person writes in the log group — in General or in any
    topic, a message or a command, the operator or another member — draws one
    pointer to the direct chat per person per boot, in the thread they wrote
    in, and reaches nothing else: no job, no assistant turn."""

    async def test_general_draws_one_pointer_per_person(self, fake_telegram_bot):
        ch, drv = _log_group_channel(fake_telegram_bot)
        u = _mk_update(chat_id=-1001, text="hi", thread_id=None, user_id=7)
        await ch.handle_update(u)
        await ch.handle_update(u)
        assert _redirects(fake_telegram_bot) == [(None, REDIRECT)]
        assert drv.submitted == []
        ch._route_to_assistant.assert_not_awaited()

    async def test_a_topic_message_or_command_reaches_no_job(
            self, fake_telegram_bot, engagement_fixture):
        """Even in the thread an older job's topic used: the message, a
        /cancel and the operator's own words reach nothing."""
        ch, drv = _log_group_channel(fake_telegram_bot, engagement_fixture)
        rec = engagement_fixture.active_record
        old_topic = 555                                  # a thread from before
        for text in ("Alex please continue", "/cancel", "/help"):
            await ch.handle_update(_mk_update(
                chat_id=-1001, text=text, thread_id=old_topic, user_id=100))
        assert drv.submitted == []
        ch._route_to_assistant.assert_not_awaited()
        assert engagement_fixture.registry.get(rec.id).status == "active"
        assert _redirects(fake_telegram_bot) == [(555, REDIRECT)]

    async def test_each_person_is_told_once_in_their_thread(self, fake_telegram_bot):
        ch, _drv = _log_group_channel(fake_telegram_bot)
        await ch.handle_update(_mk_update(chat_id=-1001, text="a", thread_id=12,
                                          user_id=7))
        await ch.handle_update(_mk_update(chat_id=-1001, text="b", thread_id=13,
                                          user_id=7))
        await ch.handle_update(_mk_update(chat_id=-1001, text="c", thread_id=13,
                                          user_id=8))
        assert sorted(_redirects(fake_telegram_bot)) == [(12, REDIRECT),
                                                         (13, REDIRECT)]

    async def test_a_bot_is_ignored(self, fake_telegram_bot):
        ch, drv = _log_group_channel(fake_telegram_bot)
        await ch.handle_update(_mk_update(chat_id=-1001, text="beep",
                                          thread_id=12, is_bot=True))
        assert _redirects(fake_telegram_bot) == []
        assert drv.submitted == []

    async def test_a_file_in_the_group_draws_the_same_pointer(self, fake_telegram_bot):
        """The text path and the file path share one seen-set: a person who
        wrote and then sent a file is told once."""
        ch, drv = _log_group_channel(fake_telegram_bot)
        await ch.handle_update(_mk_update(chat_id=-1001, text="hi",
                                          thread_id=12, user_id=7))
        doc = SimpleNamespace(file_name="a.pdf", file_size=10,
                              mime_type="application/pdf", file_id="f")
        for user_id in (7, 8):
            msg = SimpleNamespace(
                chat=SimpleNamespace(id=-1001), message_thread_id=14,
                from_user=SimpleNamespace(id=user_id, is_bot=False),
                document=doc, photo=None, caption=None, message_id=5)
            await ch._on_non_text_message(SimpleNamespace(message=msg))
        assert sorted(_redirects(fake_telegram_bot)) == [(12, REDIRECT),
                                                         (14, REDIRECT)]
        assert drv.submitted == []


class TestThePointerIsDeletedWithTheLogs:
    """The pointer is sent directly, not through the log's writer, and is
    recorded with the log's posts so retention deletes it 44 hours on."""

    async def _channel(self, bot, tmp_path):
        from agent_logs import AgentLogs

        ch, _drv = _log_group_channel(bot)
        wall = [1_800_000_000.0]
        logs = AgentLogs(display_name=lambda role: role,
                         path=str(tmp_path / "log-topics.json"),
                         wall=lambda: wall[0])
        await logs.check_enabled(bot, -1001)
        ch._agent_logs = logs
        return ch, logs, wall

    async def test_the_pointer_is_deleted_after_44_hours(self, fake_telegram_bot,
                                                         tmp_path):
        bot = fake_telegram_bot
        bot.delete_messages = AsyncMock(return_value=True)
        ch, logs, wall = await self._channel(bot, tmp_path)
        await ch.handle_update(_mk_update(chat_id=-1001, text="hi", thread_id=12,
                                          user_id=7))
        assert _redirects(bot) == [(12, REDIRECT)]
        assert [p[0] for p in logs._posts] == [1]
        wall[0] += 44 * 3600 + 1
        await logs.retention_pass()
        bot.delete_messages.assert_awaited_once_with(chat_id=-1001, message_ids=[1])
        assert logs._posts == []

    async def test_a_failed_pointer_records_nothing(self, fake_telegram_bot,
                                                    tmp_path):
        bot = fake_telegram_bot
        ch, logs, _wall = await self._channel(bot, tmp_path)
        bot.send_message = AsyncMock(side_effect=RuntimeError("down"))
        await ch.handle_update(_mk_update(chat_id=-1001, text="hi", user_id=7))
        assert logs._posts == []


class TestOnlyANewMessageStartsATurn:
    """#1040: PTB delivers an edited message, a channel post and a business
    message with ``update.message`` unset and the content on another field
    (and on ``effective_message``). None of them may start a turn — an edit
    processed as a new message answers the same question twice. The
    registered filter already excludes them (pinned against real PTB in
    test_inbound_files); this pins the handler's own check behind it."""

    @staticmethod
    def _as(field, *, chat_id, thread_id=None):
        u = _mk_update(chat_id=chat_id, text="actually, make it 8pm",
                       thread_id=thread_id)
        msg = u.message
        u.message = None
        setattr(u, field, msg)
        u.effective_message = msg
        return u

    @pytest.mark.parametrize("field", ["edited_message", "channel_post",
                                       "business_message"])
    async def test_a_non_new_message_starts_no_turn(
            self, fake_telegram_bot, engagement_fixture, field):
        from channels.telegram import TelegramChannel

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        drv = _wire_driver(ch)
        ch._route_to_assistant = AsyncMock()
        ch._engagement_registry = engagement_fixture.registry

        await ch.handle_update(self._as(field, chat_id=100))
        await ch.handle_update(self._as(field, chat_id=-1001, thread_id=555))
        ch._route_to_assistant.assert_not_called()
        assert drv.submitted == []
        assert fake_telegram_bot.messages == []


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
        ch._route_to_assistant = AsyncMock()

        u = _mk_update(chat_id=100, text="hi Ellen")
        # PTB calls handlers with (update, context). The bound method
        # therefore receives 3 positional args. handle_update MUST
        # accept the trailing context arg (used or not). Pre-fix this
        # raised TypeError: "takes 2 positional arguments but 3 were given".
        ptb_context = MagicMock(name="CallbackContext")
        await ch.handle_update(u, ptb_context)
        ch._route_to_assistant.assert_awaited_once()

    async def test_handle_update_log_group_with_ptb_context(
        self, fake_telegram_bot,
    ):
        """Same regression coverage on the log-group branch, so a future
        signature change that only breaks one leg fails the test."""
        ch, drv = _log_group_channel(fake_telegram_bot)

        u = _mk_update(chat_id=-1001, text="continue please", thread_id=12)
        await ch.handle_update(u, MagicMock(name="CallbackContext"))
        assert _redirects(fake_telegram_bot) == [(12, REDIRECT)]
        assert drv.submitted == []


class TestMessageHandlerWiring:
    """E-13: the PTB MessageHandler must dispatch to handle_update so that
    every chat is routed by it (the log group's read-only path included). Pre-fix, MessageHandler routed to
    _handle, which is engagement-unaware and forwarded /cancel to Ellen
    (bug-review-2026-04-29-exploration.md § E-13).

    The full _rebuild() bring-up path is hard to mock cleanly without a
    real PTB Application; we assert the wiring at the source level.
    Behavioral coverage of handle_update lives in the classes above."""

    async def test_rebuild_registers_message_handler_with_handle_update(self):
        import inspect
        from channels.telegram import TelegramChannel

        source = inspect.getsource(TelegramChannel._rebuild)
        # The wiring must point at handle_update, not _handle. H5 (v0.52.0)
        # added block=False so a long engagement turn can't stall PTB's
        # sequential update fetcher.
        assert "MessageHandler(_text_filter(), self.handle_update, block=False)" in source, (
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
        ch._route_to_assistant = AsyncMock()
        u = _mk_update(chat_id=31337, text="unlock the front door")
        await ch.handle_update(u)
        ch._route_to_assistant.assert_not_awaited()

    async def test_all_chats_accepted_when_chat_id_empty(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(bot=fake_telegram_bot, chat_id="",
                             engagement_supergroup_id=-1001)
        ch._route_to_assistant = AsyncMock()
        u = _mk_update(chat_id=31337, text="hello")
        await ch.handle_update(u)
        ch._route_to_assistant.assert_awaited_once()

    async def test_configured_chat_still_routed(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100,
                             engagement_supergroup_id=-1001)
        ch._route_to_assistant = AsyncMock()
        u = _mk_update(chat_id=100, text="hi Ellen")
        await ch.handle_update(u)
        ch._route_to_assistant.assert_awaited_once()


