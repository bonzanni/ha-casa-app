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
    """Casa's endpoint: its id is not known yet, as on a first boot."""

    default_agent = "assistant"
    has_bot_id = False
    chat_id = "100"
    on_identity = None

    def __init__(self, ready: bool) -> None:
        self.is_ready = ready
        self.sent: list[tuple[str, dict]] = []

    async def send_response(self, text: str, context: dict) -> None:
        self.sent.append((text, context))


def _transport(channel):
    from channels.telegram_transport import TelegramTransport
    from telegram_channel_double import MemoryBotIdStore

    transport = TelegramTransport(casa_bot_ids=MemoryBotIdStore())
    transport.add_endpoint(channel, casa=True)
    return transport


class _Manager:
    """A manager that does not say whether its start completed, with the
    Telegram transport over *channel* (or no Telegram at all)."""

    def __init__(self, channel) -> None:
        self._channel = None if channel is None else _transport(channel)

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
    # To the operator's chat, through Casa's own endpoint: a notice names
    # the chat and no bot, so it needs no bot id.
    assert ch.sent[0][1] == {"chat_id": 100}


async def test_notify_goes_through_casa_endpoint_only():
    """With a second bot bound to another role, the notice still goes
    through Casa's endpoint and nothing reaches the other one."""
    from two_endpoints import CASA_BOT, FINANCE_BOT, build_two_endpoints, calls

    two = build_two_endpoints()
    await casa_core.operator_notify(two.manager, "hello")
    assert [c[0] for c in calls(two.ep[CASA_BOT])] == ["send_response"]
    assert calls(two.ep[FINANCE_BOT]) == []


# ---------------------------------------------------------------------------
# #930 — the not-ready raise is typed by whether channel start has completed
# ---------------------------------------------------------------------------

class _StartChannel:
    name = "telegram"
    default_agent = "assistant"
    has_bot_id = False
    chat_id = "100"
    on_identity = None

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.is_ready = False

    async def start(self) -> None:
        if self.fail:
            raise RuntimeError("start failed")

    async def stop(self) -> None:
        pass


async def test_930_start_completed_turns_true_only_when_start_all_returns():
    """Completed, not ready: a start that returns with the channel still
    not ready completes the start; stop_all never resets it."""
    from channels import ChannelManager

    mgr = ChannelManager()
    mgr.register(_transport(_StartChannel()))
    assert mgr.start_completed is False
    await mgr.start_all()
    assert mgr.start_completed is True
    await mgr.stop_all()
    assert mgr.start_completed is True


async def test_930_start_completed_stays_false_when_a_start_raises():
    from channels import ChannelManager

    mgr = ChannelManager()
    mgr.register(_transport(_StartChannel(fail=True)))
    with pytest.raises(RuntimeError, match="start failed"):
        await mgr.start_all()
    assert mgr.start_completed is False


async def test_930_not_ready_before_start_completed_raises_the_subclass():
    from channels import ChannelManager, OperatorNotifyBeforeStart

    mgr = ChannelManager()
    ch = _StartChannel()
    mgr.register(_transport(ch))
    with pytest.raises(OperatorNotifyBeforeStart):
        await casa_core.operator_notify(mgr, "hello")
    await mgr.start_all()
    with pytest.raises(RuntimeError) as exc:
        await casa_core.operator_notify(mgr, "hello")
    assert type(exc.value) is RuntimeError


@pytest.mark.parametrize("manager", ("none", "no_marker"))
async def test_930_unknown_start_state_raises_the_bare_error(manager):
    """No manager, or one that does not carry the marker: nothing
    establishes that start is pending, so the ordinary error."""
    mgr = None if manager == "none" else _Manager(_Channel(ready=False))
    with pytest.raises(RuntimeError) as exc:
        await casa_core.operator_notify(mgr, "hello")
    assert type(exc.value) is RuntimeError
