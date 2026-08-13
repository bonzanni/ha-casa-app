"""#513 — the operator hears about a rewrite of their placeholder-bearing
triggers.yaml, on-channel, instead of only in a log line nobody reads.

Built on #556's contract: the pending path is consumed ONLY on a confirmed
delivery. Draining before the send would reproduce, in this feature, exactly
the defect #556 reports about the plugin-health notice.
"""
from __future__ import annotations

import asyncio

import pytest

import casa_core
import reminders

pytestmark = pytest.mark.asyncio

PATH = "/config/agents/ellen/triggers.yaml"


class _FakeChannel:
    def __init__(self, outcome=None, raises=None):
        from channels import DeliveryOutcome

        self.is_ready = True
        self.sent = []
        self._outcome = outcome or DeliveryOutcome.DELIVERED
        self._raises = raises

    async def send(self, message, context):
        if self._raises is not None:
            raise self._raises
        self.sent.append(message)
        return self._outcome


class _FakeChannelManager:
    def __init__(self, channel):
        self._channel = channel

    def get(self, name):
        return self._channel if name == "telegram" else None


@pytest.fixture(autouse=True)
def _clean_state():
    reminders._placeholder_pending.clear()
    casa_core._placeholder_notified.clear()
    yield
    reminders._placeholder_pending.clear()
    casa_core._placeholder_notified.clear()


async def test_delivered_notice_names_the_file_and_clears_it():
    ch = _FakeChannel()
    reminders._placeholder_pending.add(PATH)

    await casa_core.notify_placeholder_rewrites(_FakeChannelManager(ch))

    assert len(ch.sent) == 1
    assert PATH in ch.sent[0]
    assert "${...}" in ch.sent[0]
    assert reminders.peek_placeholder_notices() == []


async def test_an_undelivered_notice_leaves_the_path_pending():
    """The bug #556 reports, guarded against inside its own first consumer:
    the reconnect window sends nothing, so nothing may be consumed."""
    from channels import DeliveryOutcome

    ch = _FakeChannel(outcome=DeliveryOutcome.NOT_DELIVERED)
    reminders._placeholder_pending.add(PATH)

    await casa_core.notify_placeholder_rewrites(_FakeChannelManager(ch))

    assert reminders.peek_placeholder_notices() == [PATH]
    assert PATH not in casa_core._placeholder_notified

    # The next rewrite's notifier pass delivers it — no intervening rewrite
    # needed, and the operator is told exactly once.
    ok = _FakeChannel()
    await casa_core.notify_placeholder_rewrites(_FakeChannelManager(ok))
    assert len(ok.sent) == 1
    assert reminders.peek_placeholder_notices() == []


async def test_a_raising_send_leaves_the_path_pending():
    """No finally clears it — retention is by construction, not by cleanup."""
    ch = _FakeChannel(raises=RuntimeError("telegram down"))
    reminders._placeholder_pending.add(PATH)

    with pytest.raises(RuntimeError):
        await casa_core.notify_placeholder_rewrites(_FakeChannelManager(ch))

    assert reminders.peek_placeholder_notices() == [PATH]


async def test_the_same_file_is_not_announced_twice():
    """#513 asks to inform, not nag: per file, per process."""
    ch = _FakeChannel()
    reminders._placeholder_pending.add(PATH)
    await casa_core.notify_placeholder_rewrites(_FakeChannelManager(ch))
    reminders._placeholder_pending.add(PATH)        # rewritten again
    await casa_core.notify_placeholder_rewrites(_FakeChannelManager(ch))

    assert len(ch.sent) == 1
    assert reminders.peek_placeholder_notices() == []


async def test_concurrent_notifiers_announce_exactly_once():
    """Sol r2 / Terra r2: check -> await send -> mark spans an await, so two
    concurrent removals in one file both passed the check and both sent."""
    from channels import DeliveryOutcome

    gate = asyncio.Event()

    class _SlowChannel(_FakeChannel):
        async def send(self, message, context):
            await gate.wait()
            self.sent.append(message)
            return DeliveryOutcome.DELIVERED

    ch = _SlowChannel()
    cm = _FakeChannelManager(ch)
    reminders._placeholder_pending.add(PATH)

    first = asyncio.create_task(casa_core.notify_placeholder_rewrites(cm))
    second = asyncio.create_task(casa_core.notify_placeholder_rewrites(cm))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(first, second)

    assert len(ch.sent) == 1


async def test_no_channel_defers_without_consuming():
    reminders._placeholder_pending.add(PATH)
    await casa_core.notify_placeholder_rewrites(_FakeChannelManager(None))
    assert reminders.peek_placeholder_notices() == [PATH]


async def test_nothing_pending_sends_nothing():
    ch = _FakeChannel()
    await casa_core.notify_placeholder_rewrites(_FakeChannelManager(ch))
    assert ch.sent == []
