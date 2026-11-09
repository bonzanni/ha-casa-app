"""DriverProtocol — abstract base class for engagement drivers.

Implementation: in_casa_driver.InCasaDriver.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from engagement_registry import EngagementRecord


class DriverProtocol(ABC):
    """Lifecycle interface all engagement drivers must honour."""

    @abstractmethod
    async def open(
        self,
        engagement: EngagementRecord,
        options: Any,
    ) -> None:
        """Open the engagement's session.

        ``in_casa`` (the only driver): ``options`` is a
        ``ClaudeAgentOptions``; the session's first input is queued after it.
        """

    @abstractmethod
    async def cancel(self, engagement: EngagementRecord) -> None:
        """Tear down the underlying client. Idempotent."""

    @abstractmethod
    async def resume(
        self,
        engagement: EngagementRecord,
        session_id: str,
    ) -> None:
        """Rehydrate a suspended engagement by re-opening the client with
        ``resume=session_id``. Raises on failure; caller decides retry."""

    @abstractmethod
    def is_alive(self, engagement: EngagementRecord) -> bool:
        """Return True when the driver has a live client for this engagement."""
