"""Registering a hand-written channel double as a Telegram endpoint.

Production registers exactly one ``"telegram"`` channel: the transport, which
resolves every call to one bot endpoint. Tests that only need a small
duck-typed channel (one that records what it was asked to post) give it the
identity an endpoint carries and register it through a real
``TelegramTransport``, so the code under test resolves it by route or by bot id
the way it resolves a real endpoint.
"""

from __future__ import annotations

from typing import Any

CASA_BOT = 111


def as_endpoint(ch: Any, *, bot_id: int = CASA_BOT, role: str = "assistant",
                chat_id: Any = None) -> Any:
    """Give *ch* the identity a ready endpoint has: its bot id, its role and
    (when given) the operator chat it serves. Returns *ch*."""
    ch.bot_id = bot_id
    ch.has_bot_id = True
    ch.default_agent = role
    if chat_id is not None or not hasattr(ch, "chat_id"):
        ch.chat_id = chat_id
    ch.on_identity = None
    return ch


def endpoint_manager(*channels: Any, casa: Any = None) -> Any:
    """A ``ChannelManager`` whose ``"telegram"`` channel is a transport over
    *channels*, each already carrying a bot id (see :func:`as_endpoint`).
    *casa* defaults to the first one."""
    from telegram_channel_double import MemoryBotIdStore, make_transport

    casa = channels[0] if casa is None else casa
    return make_transport(*channels, casa=casa,
                          casa_bot_ids=MemoryBotIdStore(casa.bot_id))


def seam_behind_transport(seam: Any) -> Any:
    """Re-register the shared ``operator_seam``'s real ``TelegramChannel`` as
    Casa's endpoint behind a ``TelegramTransport``, the one ``"telegram"``
    channel ``casa_core`` registers, so ``operator_notify`` and
    ``start_all`` reach it the way they do in production. Returns *seam*."""
    import functools

    import casa_core
    from telegram_channel_double import MemoryBotIdStore, make_transport

    seam.manager = make_transport(seam.channel, casa_bot_ids=MemoryBotIdStore())
    seam.notify = functools.partial(casa_core.operator_notify, seam.manager)
    assert seam.manager.get("telegram").casa_endpoint() is seam.channel
    assert seam.manager.start_completed is False
    return seam
