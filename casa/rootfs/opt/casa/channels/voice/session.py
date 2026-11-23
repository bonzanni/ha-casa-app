"""Voice session bookkeeping — pool + idle sweep.

Pool is process-local. Cleared on restart. Eviction touches no memory.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from session_registry import build_scoped_session_key

logger = logging.getLogger(__name__)


@dataclass
class VoiceSession:
    scope_id: str
    role: str
    session_key: str
    last_activity: float
    gate: asyncio.Semaphore
    in_flight: str | None = None


class VoiceSessionPool:
    """Keyed on (role, scope_id) — spec A2: two residents sharing one
    device/scope (e.g. butler + concierge on the same kitchen satellite)
    must never share a pool entry (and therefore never share an SDK
    session_key). ``role`` is keyword-only and required on every lookup —
    a caller with no role to offer (see the ``stt_start`` frame, which
    carries no ``agent_role``) must not touch the pool at all rather than
    guess or default one."""

    def __init__(self, idle_timeout: int, gate_slots: int = 10) -> None:
        self._idle_timeout = idle_timeout
        self._gate_slots = gate_slots
        self._sessions: dict[tuple[str, str], VoiceSession] = {}

    # --- lifecycle ----------------------------------------------------

    def ensure(self, scope_id: str, *, role: str) -> VoiceSession:
        key = (role, scope_id)
        sess = self._sessions.get(key)
        if sess is not None:
            return sess
        sess = VoiceSession(
            scope_id=scope_id,
            role=role,
            session_key=build_scoped_session_key("voice", role, scope_id),
            last_activity=time.monotonic(),
            gate=asyncio.Semaphore(self._gate_slots),
        )
        self._sessions[key] = sess
        return sess

    def get(self, scope_id: str, *, role: str) -> VoiceSession | None:
        return self._sessions.get((role, scope_id))

    def touch(self, scope_id: str, *, role: str) -> None:
        sess = self._sessions.get((role, scope_id))
        if sess is not None:
            sess.last_activity = time.monotonic()

    def sweep(self) -> list[str]:
        """Evict sessions idle longer than idle_timeout. Returns evicted scope_ids."""
        now = time.monotonic()
        evicted: list[str] = []
        for key, sess in list(self._sessions.items()):
            if now - sess.last_activity > self._idle_timeout:
                self._sessions.pop(key, None)
                evicted.append(sess.scope_id)
        if evicted:
            logger.info("Voice pool evicted %d idle session(s)", len(evicted))
        return evicted

    # --- background sweeper -------------------------------------------

    async def run_sweeper(self, interval: float = 30.0) -> None:
        """Long-lived task; cancel on shutdown."""
        try:
            while True:
                await asyncio.sleep(interval)
                self.sweep()
        except asyncio.CancelledError:
            pass
