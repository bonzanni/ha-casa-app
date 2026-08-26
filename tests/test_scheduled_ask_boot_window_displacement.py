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

import asyncio

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


# ---------------------------------------------------------------------------
# #680 — the two boot-window arms of the challenge's on-delivery displacement
# ---------------------------------------------------------------------------


class _BlockingPostChannel(_FakeChannel):
    """`post_dm_keyboard` blocks until released, then returns `release_to` —
    so a test can run `reconcile_at_boot` WHILE a challenge post is in flight."""

    def __init__(self):
        super().__init__()
        self.gate = None
        self.release_to = None

    async def post_dm_keyboard(self, *, chat_id, request_id, text, options,
                               short_labels=False):
        self.posts.append((chat_id, request_id, text, tuple(options)))
        self.calls.append("post")
        if self.gate is not None:
            await self.gate.wait()
            return self.release_to
        return self._post_result


async def _seed_previous_process_record(store):
    await store.put({
        "rid": "from-previous-process", "state": scheduled_asks.STATE_LIVE,
        "role": "assistant", "session_scope": LABEL,
        "scope": f"dm:{OPERATOR}", "chat_id": OPERATOR,
        "operator_id": OPERATOR, "message_id": 77,
        "options": ["Confirm", "Wrong"], "body": "Send the invoice?",
        "epoch": 0, "created_at": 0.0, "expires_at": 1_000.0,
    })


async def test_a_challenge_that_failed_to_post_lets_the_reconciler_restore(
    _fresh_broker, _fresh_store,
):
    """#680, the arm the fix CLOSES in the boot window.

    A challenge is raised before `reconcile_at_boot` and its keyboard fails to
    post. Because the challenge no longer marks anything at admission and the
    broker unregisters a failed post, the authz lane is idle by the time the
    reconciler looks — so the previous process's question is RESTORED and is
    still answerable, rather than settled for a challenge nobody ever saw.

    Pre-fix this was `revoked_before_reconcile == 1`: admission wrote a boot
    marker unconditionally, so the record was settled `trigger_changed` for a
    challenge that never reached the screen.
    """
    import authz_grants

    await _seed_previous_process_record(_fresh_store)
    channel = _BlockingPostChannel()
    channel._post_result = None          # the challenge post fails

    coordinator = authz_grants.ChallengeCoordinator()
    handle = coordinator.register_challenge(
        ("tool", "args"), chat_id=OPERATOR, operator_id=OPERATOR,
        channel=channel, challenge_text="Approve send_email?",
    )
    assert await handle.settled_post() == "delivery_failed"
    await _fresh_broker.drain_hooks()

    counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)
    await _fresh_broker.drain_hooks()

    assert counts.get("restored", 0) == 1
    assert counts.get("revoked_before_reconcile", 0) == 0
    assert counts.get("operator_busy", 0) == 0
    live = _fresh_broker.pending(namespace="resident_ask", scope=f"dm:{OPERATOR}")
    assert len(live) == 1
    assert sum(r == "from-previous-process" for r in live) == 1
    assert len(channel.scheduled_dispatches) == 0
    assert len(channel.edits) == 0
    assert len(_fresh_store.all()) == 1
    assert len(scheduled_asks._BOOT_REVOCATIONS) == 0


async def test_a_delivered_challenge_lets_the_reconciler_settle_operator_busy(
    _fresh_broker, _fresh_store,
):
    """The other side of the same coin: a challenge that IS on screen holds the
    lane, so `require_idle` refuses the restore and the record settles
    `operator_busy` — the truthful outcome, reached without a marker."""
    import authz_grants

    await _seed_previous_process_record(_fresh_store)
    channel = _BlockingPostChannel()

    coordinator = authz_grants.ChallengeCoordinator()
    handle = coordinator.register_challenge(
        ("tool", "args"), chat_id=OPERATOR, operator_id=OPERATOR,
        channel=channel, challenge_text="Approve send_email?",
    )
    assert await handle.settled_post() == "posted"
    await _fresh_broker.drain_hooks()

    counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)
    await _fresh_broker.drain_hooks()

    assert counts.get("operator_busy", 0) == 1
    assert counts.get("restored", 0) == 0
    assert counts.get("revoked_before_reconcile", 0) == 0
    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"dm:{OPERATOR}")) == 0
    assert len(channel.scheduled_dispatches) == 1
    assert sum("operator_busy" in d["text"]
               for d in channel.scheduled_dispatches) == 1
    assert len(_fresh_store.all()) == 0
    assert len(scheduled_asks._BOOT_REVOCATIONS) == 0


async def test_reconcile_during_an_in_flight_challenge_post_is_still_a_loss(
    _fresh_broker, _fresh_store,
):
    """CHARACTERISATION of a residual #680 does NOT close, pinned so nobody
    reads the fix as covering it. **This is not a declared invariant** and is
    deliberately bound to none.

    `reconcile_at_boot` runs strictly BETWEEN the challenge's registration and
    its post settling. `require_idle` reads raw `authz:{chat}` occupancy and
    cannot tell a POSTING challenge from a DELIVERED one, so it settles the
    durable record `operator_busy`; the post then fails and the operator is
    left with neither question. The loss is taken by the RECONCILER, not by the
    challenge's ordering, so no ordering change in `authz_grants.py` can close
    it — closing it means changing what counts as lane occupancy, which is the
    `require_idle` half of INV-JOB-008 and a different mechanism.

    Measured identically on the pre-fix tree, where the loss ran through the
    admission-time boot marker instead (`revoked_before_reconcile == 1`,
    continuation `trigger_changed`). The fix neither introduces nor widens it.
    """
    import authz_grants

    await _seed_previous_process_record(_fresh_store)
    channel = _BlockingPostChannel()
    channel.gate = asyncio.Event()
    channel.release_to = None            # the post will fail, once released

    coordinator = authz_grants.ChallengeCoordinator()
    handle = coordinator.register_challenge(
        ("tool", "args"), chat_id=OPERATOR, operator_id=OPERATOR,
        channel=channel, challenge_text="Approve send_email?",
    )
    while len(channel.posts) < 1:
        await asyncio.sleep(0)

    counts = await scheduled_asks.reconcile_at_boot(channel, now=0.0)

    channel.gate.set()
    assert await handle.settled_post() == "delivery_failed"
    await _fresh_broker.drain_hooks()

    assert counts.get("operator_busy", 0) == 1
    assert counts.get("restored", 0) == 0
    assert counts.get("revoked_before_reconcile", 0) == 0
    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"dm:{OPERATOR}")) == 0
    assert len(_fresh_broker.pending(
        namespace="resident_ask", scope=f"authz:{OPERATOR}")) == 0
    assert len(channel.edits) == 1
    assert len(channel.scheduled_dispatches) == 1
    assert len(_fresh_store.all()) == 0
    assert len(scheduled_asks._BOOT_REVOCATIONS) == 0
