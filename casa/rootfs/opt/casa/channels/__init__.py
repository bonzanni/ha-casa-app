"""Channel abstraction for Casa agent I/O."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from enum import Enum

logger = logging.getLogger(__name__)


class DeliveryOutcome(Enum):
    """Whether a delivery actually reached the transport (#556).

    A one-shot operator notice rides at the HEAD of the text, so the outcome
    keys on the first unit of output, not on total success: a multi-page reply
    whose page 1 landed HAS shown the notice, whatever happens to page 3.

    ``UNKNOWN`` is what a channel that has not opted into the contract yields
    (it returns ``None``, which callers coerce). Those callers keep today's
    behavior EXPLICITLY rather than by omission — the distinction matters,
    because treating "channel is off the contract" as "delivery failed" would
    re-offer a notice forever on a channel that cannot report.
    """

    DELIVERED = "delivered"
    NOT_DELIVERED = "not_delivered"
    UNKNOWN = "unknown"


class Channel(ABC):
    """Base class for all communication channels."""

    name: str
    default_agent: str

    @abstractmethod
    async def start(self) -> None:
        """Start listening for incoming messages."""

    @abstractmethod
    async def send(self, message: str, context: dict) -> None:
        """Send a message through the channel."""

    async def send_media(
        self, content: bytes, kind: str, filename: str, context: dict,
        *, caption: str | None = None,
    ) -> None:
        """Deliver a media file. Concrete (not abstract): channels that can't
        deliver media inherit this NotImplementedError, which the send_media
        tool catches and maps to ``unsupported_channel``."""
        raise NotImplementedError(f"{self.name} channel cannot deliver media")

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel and clean up resources."""


class ChannelManager:
    """Registry and lifecycle manager for channels."""

    def __init__(self) -> None:
        self._channels: dict[str, Channel] = {}

    def register(self, channel: Channel) -> None:
        """Register a channel by its name."""
        self._channels[channel.name] = channel

    def get(self, name: str) -> Channel | None:
        """Return the channel with *name*, or ``None``."""
        return self._channels.get(name)

    async def start_all(self) -> None:
        """Start all registered channels."""
        for ch in self._channels.values():
            await ch.start()

    async def stop_all(self) -> None:
        """Stop all registered channels, swallowing errors."""
        for ch in self._channels.values():
            try:
                await ch.stop()
            except Exception:
                logger.exception("Error stopping channel %s", ch.name)
