"""A TelegramChannel over a fake bot, shared by the channel's delivery tests,
and the transport that registers such channels the way ``casa_core`` does."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def _mk_channel_with_fake_bot(supergroup_id: int = -1001):
    """Build a TelegramChannel with a fake _app.bot for testing."""
    from channels.telegram import TelegramChannel

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(
        return_value=MagicMock(message_id=12345)
    )
    fake_bot.edit_message_text = AsyncMock()

    fake_app = MagicMock()
    fake_app.bot = fake_bot

    ch = TelegramChannel(
        bot_token="x:y",
        chat_id=100,
        default_agent="assistant",
        engagement_supergroup_id=supergroup_id,
    )
    ch._app = fake_app
    return ch, fake_bot


class MemoryBotIdStore:
    """A ``CasaBotIdStore`` stand-in that keeps the observed id in memory and
    counts its writes, for tests that do not exercise the file itself."""

    def __init__(self, known: "int | None" = None) -> None:
        self._known = known
        self.writes: list[int] = []

    def known_id(self) -> "int | None":
        return self._known

    def observe(self, bot_id: int) -> None:
        if bot_id == self._known:
            return
        self._known = bot_id
        self.writes.append(bot_id)


def make_transport(*channels, casa=None, casa_bot_ids=None):
    """Register *channels* as endpoints of one ``TelegramTransport`` inside a
    ``ChannelManager``, the way ``casa_core`` builds it: the transport is the
    only ``"telegram"`` channel. *casa* names Casa's own endpoint (default:
    the first channel). Returns the manager; the transport is
    ``telegram_transport(manager)``."""
    from channels import ChannelManager
    from channels.telegram_transport import TelegramTransport

    if not channels:
        raise ValueError("make_transport needs at least one endpoint")
    casa = channels[0] if casa is None else casa
    transport = TelegramTransport(
        casa_bot_ids=casa_bot_ids if casa_bot_ids is not None else MemoryBotIdStore())
    transport.add_endpoint(casa, casa=True)
    for ch in channels:
        if ch is not casa:
            transport.add_endpoint(ch, casa=False)
    manager = ChannelManager()
    manager.register(transport)
    return manager
