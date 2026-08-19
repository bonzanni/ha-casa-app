"""#648 — the boot-window half of the on-delivery displacement.

`displace_scheduled_for_chat` is `cancel_for_chat` minus the boot-window
revocation marker, and this is the difference that makes. Between process start
and `reconcile_at_boot`, the live map is empty and the durable records are still
on disk: a displacement landing there selects nothing, edits nothing and tells
nobody, so marking those records revoked would assert an event that did not
happen — and the reconciler would settle them with a hardcoded `trigger_changed`
reason that no caller passed. Left unmarked, the reconciler decides truthfully on
its own: the delivered human ask holds the lane, so `require_idle` refuses and
the record settles `operator_busy`.

Separate module on purpose: the accepted red case in
`test_scheduled_ask_attention_lane.py` is frozen.
"""
from __future__ import annotations

import pytest

import scheduled_asks
import verdict_broker
from verdict_broker import VerdictBroker

from test_scheduled_ask_attention_lane import _ask_on, _dm_origin
from test_scheduled_ask_user import LABEL, OPERATOR, _FakeChannel

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_broker(monkeypatch):
    fresh = VerdictBroker()
    monkeypatch.setattr(verdict_broker, "BROKER", fresh)
    return fresh


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduled_asks, "_ROLE_EPOCHS", {})
    monkeypatch.setattr(scheduled_asks, "_TRIGGER_EPOCHS", {})
    monkeypatch.setattr(scheduled_asks, "_BOOT_REVOCATIONS", [])
    monkeypatch.setattr(scheduled_asks, "_BOOT_RECONCILED", False)
    store = scheduled_asks.ScheduledAskStore(str(tmp_path / "scheduled_asks.json"))
    monkeypatch.setattr(scheduled_asks, "STORE", store)
    return store


async def test_boot_window_displacement_leaves_the_record_to_the_reconciler(
    _fresh_broker, _fresh_store,
):
    """A human ask delivered BEFORE `reconcile_at_boot` must not settle the
    previous process's record from a marker: the reconciler's own lane check
    settles it `operator_busy`, which is what actually happened."""
    await _fresh_store.put({
        "rid": "from-previous-process", "state": scheduled_asks.STATE_LIVE,
        "role": "assistant", "session_scope": LABEL,
        "scope": f"dm:{OPERATOR}", "chat_id": OPERATOR,
        "operator_id": OPERATOR, "message_id": 77,
        "options": ["Confirm", "Wrong"], "body": "Send the invoice?",
        "epoch": 0, "created_at": 0.0, "expires_at": 1_000.0,
    })

    channel = _FakeChannel()
    human = await _ask_on(channel, _dm_origin(), "Celsius?")
    assert human["status"] == "awaiting_user"

    counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)
    await _fresh_broker.drain_hooks()

    assert counts.get("operator_busy", 0) == 1
    assert counts.get("revoked_before_reconcile", 0) == 0
    assert counts.get("restored", 0) == 0
    assert len(channel.scheduled_dispatches) == 1
    assert sum("operator_busy" in d["text"]
               for d in channel.scheduled_dispatches) == 1
    assert sum("trigger_changed" in d["text"]
               for d in channel.scheduled_dispatches) == 0
    assert len(_fresh_store.all()) == 0
    # Last, and a diagnostic rather than the outcome: the mechanism by which
    # the above is true is that no boot marker was recorded.
    assert len(scheduled_asks._BOOT_REVOCATIONS) == 0
