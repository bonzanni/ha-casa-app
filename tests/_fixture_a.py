"""Registering recording channel doubles as Telegram endpoints.

Casa reaches every Telegram bot through one ``TelegramTransport`` (the only
``"telegram"`` channel): an identity's bot is read from the endpoint a route
resolves to, and a card is posted through ``transport.endpoint(bot)``. The
tests here keep their small recording doubles, and these helpers give each
double the identity a transport reads (``bot_id``, ``has_bot_id``,
``default_agent``, the operator ``chat_id``) and register it, the way
``casa_core`` registers a real endpoint.
"""

from __future__ import annotations

from typing import Any

from telegram_channel_double import make_transport

CASA_BOT = 111
OPERATOR = 100


def as_endpoint(fake: Any, *, bot_id: int = CASA_BOT, role: str | None = None,
                operator: int | None = None) -> Any:
    """Give *fake* the identity attributes ``TelegramTransport.add_endpoint``
    reads. Attributes the double already defines are kept (a ``chat_id`` the
    double configures is the operator chat the transport reads), except
    ``bot_id`` and ``has_bot_id``, which are always set."""
    fake.bot_id = bot_id
    fake.has_bot_id = True
    if role is not None or not hasattr(fake, "default_agent"):
        fake.default_agent = role or "assistant"
    if operator is not None or not hasattr(fake, "chat_id"):
        fake.chat_id = OPERATOR if operator is None else operator
    return fake


def transport_for(*fakes: Any, casa: Any = None) -> Any:
    """A real ``TelegramTransport`` with *fakes* registered as its endpoints
    (the first is Casa's unless *casa* names another). Each fake must already
    carry its identity, or be the only one (it is then made bot 111)."""
    from channels.telegram_transport import telegram_transport

    if len(fakes) == 1 and not getattr(fakes[0], "has_bot_id", False):
        as_endpoint(fakes[0])
    return telegram_transport(make_transport(*fakes, casa=casa))


def install_transport(transport: Any, monkeypatch: Any = None) -> Any:
    """Make *transport* the ``"telegram"`` channel ``tools`` resolves. With
    *monkeypatch* the previous manager comes back at teardown; without it the
    conftest's restore of ``tools``' init globals does that."""
    from channels import ChannelManager
    import tools as tools_mod

    manager = ChannelManager()
    manager.register(transport)
    if monkeypatch is not None:
        monkeypatch.setattr(tools_mod, "_channel_manager", manager, raising=False)
    else:
        tools_mod._channel_manager = manager
    return transport


class BareEndpoint:
    """An endpoint that only has an identity: enough for a route to resolve
    to bot 111 when nothing is posted through it."""

    def __init__(self, *, bot_id: int = CASA_BOT, role: str = "assistant",
                 operator: int = OPERATOR) -> None:
        as_endpoint(self, bot_id=bot_id, role=role, operator=operator)

    def _user_id_is_operator(self, user_id: Any) -> bool:
        return user_id == self.chat_id


def install_casa_bot(monkeypatch: Any) -> Any:
    """Install a transport whose Casa endpoint is bot 111 (a
    :class:`BareEndpoint`) and return it."""
    return install_transport(transport_for(BareEndpoint()), monkeypatch)
