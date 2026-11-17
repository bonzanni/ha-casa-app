"""Tests for Telegram routing: the operator's direct chat reaches the
assistant; any other chat follows the telegram_chat_id allowlist."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

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

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100)
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

        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100)
        ch._route_to_assistant = AsyncMock()

        u = _mk_update(chat_id=100, text="hi Ellen")
        # PTB calls handlers with (update, context). The bound method
        # therefore receives 3 positional args. handle_update MUST
        # accept the trailing context arg (used or not). Pre-fix this
        # raised TypeError: "takes 2 positional arguments but 3 were given".
        ptb_context = MagicMock(name="CallbackContext")
        await ch.handle_update(u, ptb_context)
        ch._route_to_assistant.assert_awaited_once()


class TestMessageHandlerWiring:
    """E-13: the PTB MessageHandler must dispatch to handle_update so that
    every chat is routed by it. Pre-fix, MessageHandler routed to
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
        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100)
        ch._route_to_assistant = AsyncMock()
        u = _mk_update(chat_id=31337, text="unlock the front door")
        await ch.handle_update(u)
        ch._route_to_assistant.assert_not_awaited()

    async def test_all_chats_accepted_when_chat_id_empty(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(bot=fake_telegram_bot, chat_id="")
        ch._route_to_assistant = AsyncMock()
        u = _mk_update(chat_id=31337, text="hello")
        await ch.handle_update(u)
        ch._route_to_assistant.assert_awaited_once()

    async def test_configured_chat_still_routed(self, fake_telegram_bot):
        from channels.telegram import TelegramChannel
        ch = TelegramChannel(bot=fake_telegram_bot, chat_id=100)
        ch._route_to_assistant = AsyncMock()
        u = _mk_update(chat_id=100, text="hi Ellen")
        await ch.handle_update(u)
        ch._route_to_assistant.assert_awaited_once()


