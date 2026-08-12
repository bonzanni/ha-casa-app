"""#532 — `casa_core.operator_notify`: the honest operator-notice seam.

The pre-fix `_setup_notify` closure delegated to `send_response`, which
log-and-drops while the PTB app is not started — a FALSE SUCCESS that made
every notify-then-mark caller (event/callback removal notes, the event
exhaustion notice) mark a dropped notice as delivered. The seam now
RAISES when no deliverable channel exists, so observed-delivery callers
retry and advisory callers degrade to honest logging.
"""
from __future__ import annotations

import pytest

import casa_core

pytestmark = pytest.mark.unit


class _Channel:
    def __init__(self, ready: bool) -> None:
        self.is_ready = ready
        self.sent: list[tuple[str, dict]] = []

    async def send_response(self, text: str, context: dict) -> None:
        self.sent.append((text, context))


class _Manager:
    def __init__(self, channel) -> None:
        self._channel = channel

    def get(self, name):
        return self._channel if name == "telegram" else None


async def test_notify_raises_when_no_channel():
    with pytest.raises(RuntimeError):
        await casa_core.operator_notify(_Manager(None), "hello")


async def test_notify_raises_when_channel_manager_absent():
    with pytest.raises(RuntimeError):
        await casa_core.operator_notify(None, "hello")


async def test_notify_raises_when_channel_not_ready():
    """The #532 live window: channel object exists, PTB app not started —
    send_response would log-and-drop; the seam must refuse instead."""
    ch = _Channel(ready=False)
    with pytest.raises(RuntimeError):
        await casa_core.operator_notify(_Manager(ch), "hello")
    assert ch.sent == []


async def test_notify_sends_when_ready():
    ch = _Channel(ready=True)
    await casa_core.operator_notify(_Manager(ch), "hello")
    assert len(ch.sent) == 1 and ch.sent[0][0] == "hello"
