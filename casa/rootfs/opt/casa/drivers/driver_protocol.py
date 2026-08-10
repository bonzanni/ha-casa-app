"""DriverProtocol — abstract base class for engagement drivers.

Two implementations:
- in_casa_driver.InCasaDriver
- claude_code_driver.ClaudeCodeDriver
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from engagement_registry import EngagementRecord


class StaleLaunchError(RuntimeError):
    """#369: raised by a driver's ``start()`` when, at the last suspension
    point before the initial prompt is enqueued, the engagement record turns
    out to have been clearance-clamped (``context_rebuild_pending``) — the
    prompt in hand was rendered from pre-clamp materials and MUST NOT reach
    the fresh process. The launcher aborts the engagement; the clamp landing
    mid-launch is rare enough that "abort, let the caller retry" beats
    re-rendering a floor prompt nobody asked for."""


class DriverProtocol(ABC):
    """Lifecycle interface all engagement drivers must honour."""

    @abstractmethod
    async def start(
        self,
        engagement: EngagementRecord,
        prompt: str,
        options: Any,
    ) -> None:
        """Spin up the engaged agent.

        - ``in_casa`` driver: ``options`` is a ``ClaudeAgentOptions`` and
          ``prompt`` is the first user turn.
        - ``claude_code`` driver: ``options`` is the ``ExecutorDefinition``
          and ``prompt`` is the system-prompt body (will be written to
          ``CLAUDE.md``, not passed as a turn).
        """

    @abstractmethod
    async def send_user_turn(
        self,
        engagement: EngagementRecord,
        text: str,
    ) -> None:
        """Feed a user turn into the engaged agent and stream its reply
        out via the engagement's topic channel."""

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
