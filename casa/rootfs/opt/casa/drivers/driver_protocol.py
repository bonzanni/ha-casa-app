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
    out to have been clearance-clamped — the prompt in hand was rendered from
    pre-clamp materials and MUST NOT reach the fresh process.

    ``record_live`` (Terra diff-gate r5) tells the launcher what to do with
    the record: False = the clamp is still pending, nothing rebuilt, the
    launcher errors the record and aborts the topic; True = a clamp→rebuild
    cycle COMPLETED while this launch was suspended, the engagement is alive
    on its rebuilt floor session, and the launcher must abort only its own
    stale prompt — never the living engagement."""

    def __init__(self, message: str, *, record_live: bool = False) -> None:
        super().__init__(message)
        self.record_live = record_live


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
          and ``prompt`` is the INITIAL TURN — enqueued to the engagement's
          inbound spool, falling back to a direct FIFO write. It is NOT the
          workspace ``CLAUDE.md``: ``provision_workspace`` renders that from
          the executor's own template and the driver's own arguments.

        #583: that distinction is load-bearing, and this docstring had it
        backwards. Because the two are rendered separately, a value the
        engager interpolates into ``prompt`` reaches only the first turn, and
        a value the driver passes to ``provision_workspace`` reaches only
        ``CLAUDE.md``. A memory-enabled launch therefore fetches the
        prior-engagement archive on exactly one of those paths per driver —
        the driver's, for ``claude_code``. (An executor that does not opt into
        memory fetches on neither, and replay re-renders from the block the
        launch cached rather than fetching again.)
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
