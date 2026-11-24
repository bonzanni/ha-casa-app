"""Doubles the operator-delivery and proposal tests share: a settable clock and
the channel manager over Telegram's transport."""

from __future__ import annotations

from telegram_channel_double import make_transport


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _Manager(channel):
    """The channel manager with Telegram's transport over *channel*, Casa's
    endpoint on bot 111; ``None`` (no Telegram) for no channel."""
    return make_transport(channel) if channel is not None else None
